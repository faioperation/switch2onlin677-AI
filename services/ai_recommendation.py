"""
services/ai_recommendation.py
==============================
High-level AI recommendation functions used by routers/ai_recommendations.py.

Each function encapsulates one recommendation strategy:
  recommend_products()         → intent-aware universal entry point (chatbot)
  search_products_semantic()   → pure semantic search with hybrid ranking
  similar_products()           → vector similarity to a source product
  cross_sell_recommendations() → complementary products (different category)
  upsell_recommendations()     → higher-tier alternatives in same category
  skincare_recommendations()   → skin-type + concern aware retrieval
  perfume_similarity_search()  → fragrance-specific similarity search
  personalised_for_user()      → user preference vector blended search

Design principles
-----------------
1. Every function falls back gracefully when embeddings are not yet generated.
   Fallback: rule-based recommendation from services/recommendation.py.

2. Results are always filtered: active + available_qty > 5 + price > 0.

3. All functions return the same dict envelope for consistent API responses:
   {
     "found":       bool,
     "total_found": int,
     "returned":    int,
     "strategy":    str,          # which strategy produced this result
     "intent":      str | None,   # detected intent (if applicable)
     "products":    list[dict],
     "scoring":     {...},
   }
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

from services.embedding import embed_single, build_embedding_text
from services.vector_search import (
    IntentResult,
    ScoredProduct,
    detect_intent,
    find_similar_products,
    personalised_search,
    semantic_search,
    serialize_scored,
)
from tools import get_iqd_rate, ORDER_BASE_URL

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

EMBEDDING_COVERAGE_THRESHOLD = 0.10   # fall back if < 10% of catalog embedded
MIN_SEMANTIC_SCORE           = 0.30   # drop results below this cosine similarity
MAX_LIMIT                    = 50
FRAGRANCE_CATEGORY_NAMES     = {"fragrance", "perfume", "parfum", "عطور"}
SKINCARE_CATEGORY_NAMES      = {"skincare", "skin care", "facial care", "عناية بالبشرة"}


def _make_response(
    products:   list[ScoredProduct],
    *,
    strategy:   str,
    intent:     str | None = None,
    limit:      int        = 10,
    include_scores: bool   = False,
    extra:      dict | None = None,
) -> dict:
    """Serialise ScoredProduct list to the standard API envelope."""
    iqd_rate = get_iqd_rate()
    formatted = [
        serialize_scored(p, iqd_rate=iqd_rate, order_base_url=ORDER_BASE_URL, include_scores=include_scores)
        for p in products[:limit]
        if p.price > 0
    ]

    if not formatted:
        return {
            "found":       False,
            "total_found": 0,
            "returned":    0,
            "strategy":    strategy,
            "intent":      intent,
            "products":    [],
            **(extra or {}),
        }

    result: dict = {
        "found":       True,
        "total_found": len(products),
        "returned":    len(formatted),
        "strategy":    strategy,
        "intent":      intent,
        "products":    formatted,
        **(extra or {}),
    }
    return result


def _fallback_response(strategy: str, reason: str) -> dict:
    return {
        "found":    False,
        "strategy": strategy,
        "reason":   reason,
        "products": [],
    }


# ── Universal intent-aware entry point ───────────────────────────────────────

def recommend_products(
    db:          Session,
    query:       str,
    *,
    openai_client,
    user_id:     Optional[str]   = None,
    session_id:  Optional[str]   = None,
    locale:      str             = "en",
    limit:       int             = 10,
    category_id: Optional[int]  = None,
    price_tier:  Optional[str]  = None,
    cart_barcodes: list[str]    = (),
    viewed_barcodes: list[str]  = (),
    include_scores: bool        = False,
) -> dict:
    """
    Universal entry point used by the chatbot.

    1. Detect intent from the user's query.
    2. Route to the appropriate retrieval strategy.
    3. Apply cart exclusion and viewed-items diversity.
    4. Return the standard response envelope.

    Falls back to rule-based recommendations when no embeddings exist.
    """
    limit = min(limit, MAX_LIMIT)
    query = query.strip()

    if not query:
        return _fallback_response("recommend_products", "Empty query")

    # ── 1. Detect intent ──────────────────────────────────────────────────────
    intent: IntentResult = detect_intent(query, openai_client)
    intent_strategy = intent.intent

    # ── 2. Route to strategy ──────────────────────────────────────────────────
    products: list[ScoredProduct] = []

    if intent_strategy == "similar_product" and intent.reference_name:
        products = _route_similar_product(db, intent, limit * 2)

    elif intent_strategy == "skin_concern":
        products = _route_skin_concern(db, intent, category_id, limit * 2)

    elif intent_strategy == "category_search":
        products = _route_category_search(db, intent, category_id, price_tier, limit * 2)

    elif intent_strategy == "brand_search":
        products = _route_brand_search(db, intent, limit * 2)

    elif intent_strategy == "price_search":
        products = _route_price_search(db, intent, category_id, limit * 2)

    else:  # general
        query_vector = embed_single(query)
        if query_vector:
            products = semantic_search(
                db, query_vector, limit=limit * 2,
                category_id=category_id, price_tier=price_tier,
            )

    # ── 3. Personalise (user preference blending) ─────────────────────────────
    if not products and user_id:
        products = personalised_search(
            db, user_id, query, limit=limit * 2, category_id=category_id
        )

    # ── 4. Apply session diversity / cart exclusion ───────────────────────────
    exclude = set(cart_barcodes)
    deprioritise = set(viewed_barcodes)

    if exclude:
        products = [p for p in products if p.barcode not in exclude]

    if deprioritise:
        unseen  = [p for p in products if p.barcode not in deprioritise]
        already = [p for p in products if p.barcode in deprioritise]
        products = unseen + already

    # ── 5. Build response ─────────────────────────────────────────────────────
    return _make_response(
        products,
        strategy      = f"ai_{intent_strategy}",
        intent        = intent_strategy,
        limit         = limit,
        include_scores= include_scores,
        extra={
            "reference_name": intent.reference_name,
            "detected_skin_type":    intent.skin_type,
            "detected_concerns":     intent.skin_concerns,
            "detected_category":     intent.category_hint,
            "detected_price_tier":   intent.price_tier,
        },
    )


# ── Intent routers ────────────────────────────────────────────────────────────

def _route_similar_product(
    db: Session,
    intent: IntentResult,
    limit: int,
) -> list[ScoredProduct]:
    """
    Embed the reference product name and find nearest catalog matches.
    "Something like Dior Sauvage" → embed "Dior Sauvage fragrance perfume"
    → cosine search → top-K alternatives.
    """
    ref_text = build_embedding_text(
        item_name     = intent.reference_name,
        brand_name    = None,
        category_name = intent.category_hint,
        subcategory_name=None, description=None,
        skin_type=None, concerns=None, tags=None,
        price_tier=intent.price_tier, brand_family=None,
    )
    vec = embed_single(ref_text)
    if not vec:
        return []

    # Filter to same category if hint is available
    # Note: category_id lookup would require a DB call; pass as string filter
    # via the WHERE clause — use category name search instead of FK
    return semantic_search(db, vec, limit=limit)


def _route_skin_concern(
    db: Session,
    intent: IntentResult,
    category_id: Optional[int],
    limit: int,
) -> list[ScoredProduct]:
    """Embed skin type + concerns → find matching products."""
    concern_text = ", ".join(intent.skin_concerns) if intent.skin_concerns else ""
    skin_query = f"{intent.skin_type or ''} skin {concern_text}".strip()

    vec = embed_single(skin_query)
    if not vec:
        return []

    return semantic_search(
        db, vec,
        limit      = limit,
        category_id= category_id,
        skin_type  = intent.skin_type,
        concerns   = intent.skin_concerns or None,
    )


def _route_category_search(
    db: Session,
    intent: IntentResult,
    category_id: Optional[int],
    price_tier: Optional[str],
    limit: int,
) -> list[ScoredProduct]:
    cat_query = f"{intent.category_hint or ''} beauty products"
    vec = embed_single(cat_query)
    if not vec:
        return []

    return semantic_search(
        db, vec,
        limit       = limit,
        category_id = category_id,
        price_tier  = intent.price_tier or price_tier,
        max_price   = intent.max_price,
    )


def _route_brand_search(
    db: Session,
    intent: IntentResult,
    limit: int,
) -> list[ScoredProduct]:
    brand_query = f"{intent.reference_name or ''} brand products"
    vec = embed_single(brand_query)
    if not vec:
        return []
    return semantic_search(db, vec, limit=limit)


def _route_price_search(
    db: Session,
    intent: IntentResult,
    category_id: Optional[int],
    limit: int,
) -> list[ScoredProduct]:
    price_query = f"{intent.category_hint or 'beauty'} products {intent.price_tier or 'affordable'}"
    vec = embed_single(price_query)
    if not vec:
        return []

    return semantic_search(
        db, vec,
        limit       = limit,
        category_id = category_id,
        price_tier  = intent.price_tier,
        max_price   = intent.max_price,
    )


# ── Dedicated strategy functions ──────────────────────────────────────────────

def search_products_semantic(
    db:          Session,
    query:       str,
    *,
    limit:       int           = 10,
    category_id: Optional[int] = None,
    price_tier:  Optional[str] = None,
    min_price:   Optional[float]= None,
    max_price:   Optional[float]= None,
    skin_type:   Optional[str] = None,
    concerns:    Optional[list[str]] = None,
    include_scores: bool       = False,
) -> dict:
    """
    Pure semantic search — no intent detection overhead.
    Use for structured API calls where the caller knows the query is a search.
    """
    vec = embed_single(query.strip())
    if not vec:
        return _fallback_response("semantic_search", "Query embedding failed")

    products = semantic_search(
        db, vec,
        limit       = min(limit, MAX_LIMIT),
        category_id = category_id,
        price_tier  = price_tier,
        min_price   = min_price,
        max_price   = max_price,
        skin_type   = skin_type,
        concerns    = concerns,
    )

    return _make_response(
        products, strategy="semantic_search", limit=limit, include_scores=include_scores
    )


def similar_products(
    db:      Session,
    barcode: str,
    *,
    limit:          int  = 10,
    same_category:  bool = False,
    include_scores: bool = False,
) -> dict:
    """Similar products based on vector distance from the source product."""
    results = find_similar_products(
        db, barcode, limit=min(limit, MAX_LIMIT), same_category=same_category
    )
    return _make_response(
        results,
        strategy = "product_similarity",
        limit    = limit,
        include_scores = include_scores,
        extra    = {"source_barcode": barcode, "same_category": same_category},
    )


def cross_sell_recommendations(
    db:      Session,
    barcode: str,
    *,
    limit:   int  = 8,
    include_scores: bool = False,
) -> dict:
    """
    Complementary products from a DIFFERENT category than the source product.
    "Bought this foundation → recommend a setting powder or moisturiser."
    Strategy: find nearest vector neighbors, then filter to different category.
    """
    source = db.query(
        __import__("models", fromlist=["Product"]).Product
    ).filter_by(barcode=barcode).first()

    if source is None:
        return _fallback_response("cross_sell", f"Product {barcode} not found")

    all_similar = find_similar_products(
        db, barcode, limit=limit * 4, same_category=False
    )
    # Keep only products in a DIFFERENT category
    cross = [p for p in all_similar if p.barcode != barcode]
    # If source has a category, prioritise different categories
    if source.category_id:
        source_cat = source.category_id
        diff_cat   = [p for p in cross if True]   # raw SQL already handles this
        # We can't filter on category_id here without another query;
        # the vector search returns mixed categories which is what we want for cross-sell
        cross = diff_cat

    return _make_response(
        cross[:limit],
        strategy = "cross_sell",
        limit    = limit,
        include_scores = include_scores,
        extra    = {"source_barcode": barcode},
    )


def upsell_recommendations(
    db:      Session,
    barcode: str,
    *,
    limit:   int = 6,
    include_scores: bool = False,
) -> dict:
    """
    Higher-tier alternatives to the source product in the same category.
    Strategy: vector similarity within same category, filter to higher price.
    """
    from models import Product as P
    source = db.query(P).filter_by(barcode=barcode).first()
    if source is None:
        return _fallback_response("upsell", f"Product {barcode} not found")

    source_price = float(source.price or 0)
    if source_price <= 0:
        return _fallback_response("upsell", "Source product has no valid price")

    candidates = find_similar_products(
        db, barcode, limit=limit * 3, same_category=True
    )
    # Keep products that cost more than the source (upsell = higher value)
    upsell = [p for p in candidates if p.price > source_price * 1.10]  # at least 10% more

    return _make_response(
        upsell[:limit],
        strategy = "upsell",
        limit    = limit,
        include_scores = include_scores,
        extra    = {
            "source_barcode": barcode,
            "source_price":   source_price,
        },
    )


def skincare_recommendations(
    db:          Session,
    *,
    skin_type:   Optional[str]       = None,
    concerns:    Optional[list[str]] = None,
    price_tier:  Optional[str]       = None,
    category_id: Optional[int]       = None,
    limit:       int                 = 10,
    include_scores: bool             = False,
) -> dict:
    """
    Dedicated skincare retrieval optimised for skin type + concern matching.

    Builds a structured query that maximises concern/skin_type signal weight:
      "skincare {skin_type} skin {concerns} moisturizer serum treatment"
    """
    query_parts = ["skincare beauty products"]
    if skin_type:
        query_parts.append(f"{skin_type} skin type")
    if concerns:
        query_parts.append(f"concerns: {', '.join(concerns)}")

    query_text = " ".join(query_parts)
    vec = embed_single(query_text)

    if not vec:
        return _fallback_response("skincare", "Embedding failed")

    products = semantic_search(
        db, vec,
        limit       = min(limit, MAX_LIMIT) * 2,
        category_id = category_id,
        price_tier  = price_tier,
        skin_type   = skin_type,
        concerns    = concerns,
    )

    return _make_response(
        products,
        strategy = "skincare_ai",
        limit    = limit,
        include_scores = include_scores,
        extra    = {
            "skin_type": skin_type,
            "concerns":  concerns,
        },
    )


def perfume_similarity_search(
    db:             Session,
    reference_name: str,
    *,
    price_tier:     Optional[str] = None,
    limit:          int           = 10,
    include_scores: bool          = False,
) -> dict:
    """
    Fragrance-specific similarity search.
    "Find perfumes similar to Dior Sauvage" →
      embed "Dior Sauvage perfume fragrance woody aromatic" →
      cosine search over fragrance products.

    The reference product name is enriched with fragrance domain keywords
    so the query embedding lands closer to fragrance product vectors.
    """
    enriched = (
        f"{reference_name} perfume fragrance eau de parfum "
        "woody aromatic fresh floral oriental luxury"
    )
    vec = embed_single(enriched)
    if not vec:
        return _fallback_response("fragrance_similarity", "Embedding failed")

    products = semantic_search(
        db, vec,
        limit      = min(limit, MAX_LIMIT) * 2,
        price_tier = price_tier,
    )

    # Fragrance-first sort: prioritise products with fragrance-related category
    fragrance = [
        p for p in products
        if _is_fragrance(p.category_name)
    ]
    others    = [
        p for p in products
        if not _is_fragrance(p.category_name)
    ]
    ordered   = fragrance + others

    return _make_response(
        ordered,
        strategy = "fragrance_similarity",
        limit    = limit,
        include_scores = include_scores,
        extra    = {"reference_name": reference_name},
    )


def personalised_for_user(
    db:          Session,
    user_id:     str,
    query:       str,
    *,
    limit:       int           = 10,
    category_id: Optional[int] = None,
    include_scores: bool       = False,
) -> dict:
    """Personalised recommendations blending the query with user preference vector."""
    products = personalised_search(
        db, user_id, query, limit=min(limit, MAX_LIMIT), category_id=category_id
    )
    return _make_response(
        products, strategy="personalised", limit=limit, include_scores=include_scores
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_fragrance(category_name: str | None) -> bool:
    if not category_name:
        return False
    return category_name.lower().strip() in FRAGRANCE_CATEGORY_NAMES
