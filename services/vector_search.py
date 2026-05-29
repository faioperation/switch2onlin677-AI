"""
services/vector_search.py
=========================
pgvector cosine similarity search + hybrid ranking engine.

Responsibilities
----------------
1. Semantic product search: embed a query → find nearest product vectors.
2. Product similarity: find products nearest to a given product's vector.
3. Hybrid ranking: blend semantic similarity with editorial + stock signals.
4. Intent detection: classify a natural-language query into structured intent.
5. User-personalized re-ranking using stored preference embeddings.

Vector query syntax (pgvector)
------------------------------
<=>  cosine distance   (1 - cosine_similarity)   — used here
<->  L2 distance
<#>  negative inner product

We use cosine distance because product embeddings are unit-normalised by
text-embedding-3-small.  Lower <=> value = more similar.

Hybrid scoring formula
----------------------
final_score = (
    W_SEM  × semantic_similarity      # 1 - cosine_distance
  + W_ED   × editorial_score          # priority + override + flags
  + W_POP  × popularity_score         # best_selling + sales_rank
  + W_STK  × availability_score       # stock / 100, capped at 1.0
  + W_FRESH× freshness_score          # new_arrival recency bonus
)

Weights are environment-configurable for A/B testing.

Intent detection
----------------
Uses GPT-4o-mini to classify the user's query into one of:
  similar_product   → vector similarity to a reference product
  skin_concern      → match skin_type + concerns fields
  category_search   → broad category/type filter
  brand_search      → specific brand query
  price_search      → budget-constrained query
  general           → fallback to hybrid semantic search

Returns a structured IntentResult dataclass used by the AI recommendation
service to route the query to the right retrieval strategy.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import Product, ProductSearchIndex, UserPreferenceProfile
from services.embedding import embed_single, embed_texts

logger = logging.getLogger(__name__)

# ── Hybrid weights (environment-overridable for A/B testing) ──────────────────

W_SEM   = float(os.getenv("SCORE_W_SEMANTIC",     "0.40"))
W_ED    = float(os.getenv("SCORE_W_EDITORIAL",    "0.22"))
W_POP   = float(os.getenv("SCORE_W_POPULARITY",   "0.15"))
W_STK   = float(os.getenv("SCORE_W_STOCK",        "0.13"))
W_FRESH = float(os.getenv("SCORE_W_FRESHNESS",    "0.10"))

MIN_STOCK_THRESHOLD = 5      # products below this are excluded
CANDIDATE_MULTIPLIER = 5     # fetch N × limit candidates before re-ranking


# ── Intent result ─────────────────────────────────────────────────────────────

@dataclass
class IntentResult:
    intent:          str             # similar_product | skin_concern | category_search | brand_search | price_search | general
    query:           str             # original user query
    reference_name:  str | None      # product/brand name mentioned ("Dior Sauvage")
    category_hint:   str | None      # detected category ("fragrance", "skincare")
    skin_concerns:   list[str]       # ["acne", "dryness", "hyperpigmentation"]
    skin_type:       str | None      # "dry" | "oily" | "sensitive" | "combination" | "normal"
    price_tier:      str | None      # Budget | Mid | Premium | Luxury
    max_price:       float | None    # explicit price ceiling from user
    language:        str             # "en" | "ar"
    embedding:       list[float] | None   # pre-computed query embedding


# ── Scored product ────────────────────────────────────────────────────────────

@dataclass
class ScoredProduct:
    barcode:          str
    item_name:        str | None
    brand_name:       str | None
    category_name:    str | None
    subcategory_name: str | None
    description:      str | None
    image_url:        str | None
    price:            float
    available_qty:    int
    skin_type:        str | None
    concerns:         list
    tags:             list
    price_tier:       str | None
    brand_family:     str | None
    is_best_selling:  bool
    is_new_arrival:   bool
    recommendation_priority: int | None
    recommendation_score_override: float | None
    sales_rank:       int | None
    ai_score:         float

    # Scoring breakdown (attached by _hybrid_score)
    semantic_score:   float = 0.0
    editorial_score:  float = 0.0
    popularity_score: float = 0.0
    stock_score:      float = 0.0
    freshness_score:  float = 0.0
    final_score:      float = 0.0
    score_reason:     str = "hybrid"


# ── Intent detection ──────────────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """
You are an intent classifier for a beauty & cosmetics recommendation system.
Analyse the user query and return JSON with exactly these keys:

intent         : one of: similar_product | skin_concern | category_search | brand_search | price_search | general
reference_name : the product or brand name the user mentioned, or null
category_hint  : the product category if identifiable (fragrance | skincare | makeup | haircare | body_care | sun_care), or null
skin_concerns  : array of skin concerns mentioned (acne | dryness | oiliness | sensitivity | aging | hyperpigmentation | dark_circles | pores | redness | eczema), or []
skin_type      : dry | oily | sensitive | combination | normal, or null
price_tier     : Budget | Mid | Premium | Luxury, or null
max_price      : numeric price ceiling in IQD if mentioned, or null
language       : "en" if English, "ar" if Arabic

Examples
--------
"I need a perfume similar to Dior Sauvage" →
  {"intent":"similar_product","reference_name":"Dior Sauvage","category_hint":"fragrance",...}

"بشرتي جافة وحساسة" →
  {"intent":"skin_concern","reference_name":null,"skin_type":"dry","skin_concerns":["dryness","sensitivity"],"language":"ar",...}

"Show me luxury face creams" →
  {"intent":"category_search","category_hint":"skincare","price_tier":"Luxury",...}

Return ONLY valid JSON, no prose.
""".strip()


def detect_intent(query: str, openai_client) -> IntentResult:
    """
    Use GPT-4o-mini to classify the user's natural-language query into a
    structured IntentResult.  Falls back to IntentResult(intent='general')
    on any parse error.
    """
    try:
        response = openai_client.chat.completions.create(
            model       = "gpt-4o-mini",
            messages    = [
                {"role": "system",  "content": INTENT_SYSTEM_PROMPT},
                {"role": "user",    "content": query},
            ],
            temperature = 0.0,
            max_tokens  = 200,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
    except Exception as exc:
        logger.warning("intent_detection_failed", extra={"error": str(exc), "query": query})
        parsed = {}

    return IntentResult(
        intent         = parsed.get("intent", "general"),
        query          = query,
        reference_name = parsed.get("reference_name"),
        category_hint  = parsed.get("category_hint"),
        skin_concerns  = parsed.get("skin_concerns") or [],
        skin_type      = parsed.get("skin_type"),
        price_tier     = parsed.get("price_tier"),
        max_price      = parsed.get("max_price"),
        language       = parsed.get("language", "en"),
        embedding      = None,
    )


# ── Core vector search ────────────────────────────────────────────────────────

def semantic_search(
    db:             Session,
    query_vector:   list[float],
    *,
    limit:          int           = 10,
    category_id:    Optional[int] = None,
    price_tier:     Optional[str] = None,
    min_price:      Optional[float] = None,
    max_price:      Optional[float] = None,
    skin_type:      Optional[str] = None,
    concerns:       Optional[list[str]] = None,
    exclude_barcodes: Optional[list[str]] = None,
    candidate_limit: Optional[int] = None,
) -> list[ScoredProduct]:
    """
    Find products most similar to query_vector using pgvector cosine distance.

    Fetches candidate_limit candidates (default = limit × CANDIDATE_MULTIPLIER)
    from PostgreSQL via the HNSW index, then re-ranks them with the hybrid
    scoring formula in Python for maximum control.

    Filters are applied at the DB level to keep the candidate set small.
    """
    if not query_vector:
        return []

    n_candidates = candidate_limit or (limit * CANDIDATE_MULTIPLIER)
    vector_literal = f"[{','.join(str(v) for v in query_vector)}]"

    # Build parameterised WHERE clause additions
    where_clauses = [
        "p.embedding IS NOT NULL",
        "p.product_status = 'active'",
        "p.available_qty > :min_stock",
        "p.price IS NOT NULL",
        "p.price > 0",
        "p.deleted_at IS NULL",
    ]
    params: dict[str, Any] = {"min_stock": MIN_STOCK_THRESHOLD, "n_candidates": n_candidates}

    if category_id is not None:
        where_clauses.append("p.category_id = :category_id")
        params["category_id"] = category_id

    if price_tier:
        where_clauses.append("p.price_tier = :price_tier")
        params["price_tier"] = price_tier

    if min_price is not None:
        where_clauses.append("p.price >= :min_price")
        params["min_price"] = min_price

    if max_price is not None:
        where_clauses.append("p.price <= :max_price")
        params["max_price"] = max_price

    if skin_type:
        where_clauses.append("(p.skin_type ILIKE :skin_type OR p.skin_type IS NULL)")
        params["skin_type"] = f"%{skin_type}%"

    if exclude_barcodes:
        where_clauses.append("p.barcode != ALL(:excl)")
        params["excl"] = exclude_barcodes

    where_sql = " AND ".join(where_clauses)

    # pgvector distance operator: <=> = cosine distance (lower = more similar)
    sql = text(f"""
        SELECT
            p.barcode, p.item_name, p.description, p.image_url,
            p.price, p.available_qty, p.skin_type, p.concerns, p.tags,
            p.price_tier, p.brand_family,
            p.is_best_selling, p.is_new_arrival,
            p.recommendation_priority, p.recommendation_score_override,
            p.sales_rank, p.ai_score,
            psi.brand_name, psi.category_name, psi.subcategory_name,
            (p.embedding <=> '{vector_literal}'::vector) AS cosine_distance
        FROM products p
        LEFT JOIN productsearchindex psi ON psi.product_id = p.barcode
        WHERE {where_sql}
        ORDER BY p.embedding <=> '{vector_literal}'::vector
        LIMIT :n_candidates
    """)

    try:
        rows = db.execute(sql, params).fetchall()
    except Exception as exc:
        logger.error("vector_search_error", extra={"error": str(exc)})
        return []

    # ── Concern filtering (Python-side, JSONB array overlap) ──────────────────
    if concerns:
        concern_set = {c.lower() for c in concerns}
        rows = [
            r for r in rows
            if _product_matches_concerns(r.concerns, concern_set)
        ]

    # ── Build ScoredProduct objects and apply hybrid ranking ──────────────────
    candidates = []
    for r in rows:
        semantic_sim = max(0.0, 1.0 - float(r.cosine_distance))
        sp = ScoredProduct(
            barcode          = r.barcode,
            item_name        = r.item_name,
            brand_name       = r.brand_name,
            category_name    = r.category_name,
            subcategory_name = r.subcategory_name,
            description      = r.description,
            image_url        = r.image_url,
            price            = float(r.price or 0),
            available_qty    = r.available_qty or 0,
            skin_type        = r.skin_type,
            concerns         = r.concerns or [],
            tags             = r.tags or [],
            price_tier       = r.price_tier,
            brand_family     = r.brand_family,
            is_best_selling  = bool(r.is_best_selling),
            is_new_arrival   = bool(r.is_new_arrival),
            recommendation_priority       = r.recommendation_priority,
            recommendation_score_override = float(r.recommendation_score_override or 0),
            sales_rank       = r.sales_rank,
            ai_score         = float(r.ai_score or 0),
            semantic_score   = semantic_sim,
        )
        _apply_hybrid_score(sp)
        candidates.append(sp)

    # Sort by final_score descending, return top limit
    candidates.sort(key=lambda x: x.final_score, reverse=True)
    return candidates[:limit]


def _product_matches_concerns(product_concerns: list | None, target: set[str]) -> bool:
    """Return True if ANY target concern appears in the product's concern list."""
    if not product_concerns or not target:
        return True  # no filter if either side is empty
    pc_lower = {str(c).lower() for c in product_concerns}
    return bool(pc_lower & target)


# ── Hybrid scoring ────────────────────────────────────────────────────────────

def _apply_hybrid_score(sp: ScoredProduct) -> None:
    """Compute and set final_score on a ScoredProduct in-place."""

    # Semantic (already computed: cosine similarity 0–1)
    s_sem = sp.semantic_score

    # Editorial score
    priority = sp.recommendation_priority or 9999
    s_priority = 1.0 - (min(priority, 9999) / 9999)
    s_override = min(float(sp.recommendation_score_override or 0), 999) / 999
    flag_bonus = 0.0
    if sp.is_best_selling:  flag_bonus += 0.5
    if sp.is_new_arrival:   flag_bonus += 0.3
    flag_bonus = min(flag_bonus, 1.0)
    s_editorial = 0.4 * s_priority + 0.3 * s_override + 0.3 * flag_bonus

    # Popularity score
    s_popularity = 0.7 if sp.is_best_selling else 0.0
    if sp.sales_rank:
        if   sp.sales_rank <= 10:  s_popularity = max(s_popularity, 1.0)
        elif sp.sales_rank <= 50:  s_popularity = max(s_popularity, 0.7)
        elif sp.sales_rank <= 200: s_popularity = max(s_popularity, 0.4)
        else:                      s_popularity = max(s_popularity, 0.1)

    # Stock availability
    s_stock = min(1.0, max(0.0, sp.available_qty) / 100)

    # Freshness
    s_freshness = 0.8 if sp.is_new_arrival else 0.0

    final = (
        W_SEM   * s_sem       +
        W_ED    * s_editorial +
        W_POP   * s_popularity+
        W_STK   * s_stock     +
        W_FRESH * s_freshness
    )

    sp.editorial_score  = round(s_editorial,  4)
    sp.popularity_score = round(s_popularity, 4)
    sp.stock_score      = round(s_stock,      4)
    sp.freshness_score  = round(s_freshness,  4)
    sp.final_score      = round(final,        6)
    sp.score_reason     = "hybrid_semantic"


# ── Product similarity (barcode → N similar products) ────────────────────────

def find_similar_products(
    db:      Session,
    barcode: str,
    *,
    limit:           int           = 10,
    same_category:   bool          = False,
    exclude_self:    bool          = True,
) -> list[ScoredProduct]:
    """
    Find products most similar to the given barcode's embedding.
    Used for "Similar Products" cards and cross-sell recommendations.
    """
    source = db.query(Product).filter(
        Product.barcode == barcode,
        Product.embedding.isnot(None),
    ).first()

    if source is None:
        logger.warning("similar_products_no_embedding", extra={"barcode": barcode})
        return []

    exclude = [barcode] if exclude_self else []

    return semantic_search(
        db,
        query_vector     = source.embedding,
        limit            = limit,
        category_id      = source.category_id if same_category else None,
        exclude_barcodes = exclude,
    )


# ── User-personalised search ──────────────────────────────────────────────────

def personalised_search(
    db:       Session,
    user_id:  str,
    query:    str,
    *,
    limit:    int = 10,
    **filters,
) -> list[ScoredProduct]:
    """
    Semantic search with user preference blending.

    Combines the query embedding with the user's stored preference embedding
    at ratio ALPHA:BETA (default 0.7 query + 0.3 preference) to steer
    results towards the user's taste.
    """
    ALPHA = 0.7
    BETA  = 0.3

    query_vec = embed_single(query)
    if not query_vec:
        return []

    profile = db.query(UserPreferenceProfile).filter(
        UserPreferenceProfile.user_id == user_id,
        UserPreferenceProfile.embedding.isnot(None),
    ).first()

    if profile and profile.embedding:
        # Blend and renormalise
        user_vec = profile.embedding
        blended  = [ALPHA * q + BETA * u for q, u in zip(query_vec, user_vec)]
        magnitude = math.sqrt(sum(x ** 2 for x in blended))
        if magnitude > 0:
            blended = [x / magnitude for x in blended]
        query_vec = blended

    return semantic_search(db, query_vec, limit=limit, **filters)


# ── Serializer ────────────────────────────────────────────────────────────────

def serialize_scored(
    product: ScoredProduct,
    iqd_rate: float = 1310.0,
    order_base_url: str = "",
    include_scores: bool = False,
) -> dict:
    """Convert a ScoredProduct to the standard API dict."""
    iqd_price = int(product.price * iqd_rate) if product.price else 0
    base: dict = {
        "id":              product.barcode,
        "barcode":         product.barcode,
        "name":            product.item_name or "",
        "brand":           product.brand_name or "",
        "category":        product.category_name or "",
        "subcategory":     product.subcategory_name or "",
        "description":     product.description or "",
        "image_url":       product.image_url,
        "price":           f"{iqd_price:,} IQD",
        "raw_price":       product.price,
        "available_qty":   product.available_qty,
        "skin_type":       product.skin_type,
        "concerns":        product.concerns,
        "tags":            product.tags,
        "price_tier":      product.price_tier,
        "brand_family":    product.brand_family,
        "is_best_selling": product.is_best_selling,
        "is_new_arrival":  product.is_new_arrival,
        "order_link":      f"{order_base_url}/{product.barcode}" if order_base_url else None,
    }
    if include_scores:
        base["_scores"] = {
            "final":      product.final_score,
            "semantic":   product.semantic_score,
            "editorial":  product.editorial_score,
            "popularity": product.popularity_score,
            "stock":      product.stock_score,
            "freshness":  product.freshness_score,
            "reason":     product.score_reason,
        }
    return base
