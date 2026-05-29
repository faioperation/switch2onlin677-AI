"""
routers/ai_recommendations.py
==============================
AI-powered recommendation API — 12 endpoints.

  POST /ai/recommend               → intent-aware universal entry point
  POST /ai/search                  → pure semantic search
  GET  /ai/similar/{barcode}       → product vector similarity
  GET  /ai/cross-sell/{barcode}    → complementary products (different category)
  GET  /ai/upsell/{barcode}        → higher-tier alternatives
  POST /ai/skincare                → skin-type + concern matching
  POST /ai/fragrance               → perfume similarity search
  GET  /ai/personalised            → user preference vector blended search

  Behavioral feedback loop:
  POST /events/product/{barcode}   → log view / click / purchase event

  Embedding management:
  GET  /ai/embeddings/status       → coverage stats
  POST /ai/embeddings/trigger      → trigger background embedding job
  PATCH /products/{barcode}/embedding/refresh  → re-embed single product

All endpoints follow the standard response envelope:
  {"found": bool, "total_found": int, "returned": int,
   "strategy": str, "intent": str | null, "products": [...]}

Error handling
--------------
AppValidationError (422) for bad parameters.
NotFoundError (404) for missing products.
ServiceError (500) for embedding or DB failures.
All propagate to the global handler in main.py.

Graceful degradation
--------------------
When a product has no embedding yet, vector-based endpoints fall back to
the rule-based recommendation service rather than returning an error.
This ensures the system works from day one even with 0% embedding coverage.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from openai import OpenAI
from sqlalchemy.orm import Session

from core.exceptions import AppValidationError, NotFoundError, ServiceError
from core.recommendation_context import RecommendationContext
from database import get_db
from models import ProductEvent
from schemas.ai_recommendation import (
    EmbedTriggerRequest,
    EmbedTriggerResponse,
    EmbeddingStatusResponse,
    FragranceRequest,
    ProductEventRequest,
    RecommendRequest,
    SemanticSearchRequest,
    SkincareRequest,
)
from services.ai_recommendation import (
    cross_sell_recommendations,
    perfume_similarity_search,
    personalised_for_user,
    recommend_products,
    search_products_semantic,
    similar_products,
    skincare_recommendations,
    upsell_recommendations,
)
from services.embedding import (
    embed_new_products,
    embed_single,
    get_embedding_stats,
    update_user_preference_profile,
)
from services.recommendation import get_featured

import os

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["AI Recommendations"])

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


# ── Shared dependencies ───────────────────────────────────────────────────────

def get_openai() -> OpenAI:
    return OpenAI(api_key=OPENAI_API_KEY)


def _context_from_request(
    user_id:    Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    locale:     str           = Query("en"),
) -> RecommendationContext:
    return RecommendationContext.from_params(
        user_id=user_id, session_id=session_id, locale=locale
    )


VALID_PRICE_TIERS = {"Budget", "Mid", "Premium", "Luxury"}


def _assert_price_tier(price_tier: Optional[str]) -> None:
    if price_tier and price_tier not in VALID_PRICE_TIERS:
        raise AppValidationError(
            f"Invalid price_tier '{price_tier}'. Valid: {sorted(VALID_PRICE_TIERS)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 1. POST /ai/recommend   — universal intent-aware chatbot entry point
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/ai/recommend")
def ai_recommend(
    body:   RecommendRequest,
    db:     Session  = Depends(get_db),
    openai: OpenAI   = Depends(get_openai),
):
    """
    Universal AI recommendation endpoint — for chatbot and API clients.

    Detects the user's intent, routes to the optimal retrieval strategy,
    applies hybrid scoring, and returns personalised results.

    Intent routing
    --------------
    similar_product   → vector similarity to a reference product/brand
    skin_concern      → embedding + metadata matching for skin problems
    category_search   → semantic category/type search
    brand_search      → brand-focused semantic search
    price_search      → budget-constrained recommendation
    general           → hybrid semantic search

    Request example
    ---------------
    { "query": "I need a perfume similar to Dior Sauvage",
      "user_id": "usr_123", "locale": "en", "limit": 8 }
    """
    _assert_price_tier(body.price_tier)

    try:
        result = recommend_products(
            db,
            body.query,
            openai_client    = openai,
            user_id          = body.user_id,
            session_id       = body.session_id,
            locale           = body.locale,
            limit            = body.limit,
            category_id      = body.category_id,
            price_tier       = body.price_tier,
            cart_barcodes    = body.cart_barcodes,
            viewed_barcodes  = body.viewed_barcodes,
            include_scores   = body.include_scores,
        )
    except Exception as exc:
        logger.error("ai_recommend_error", extra={"error": str(exc)}, exc_info=True)
        raise ServiceError("AI recommendation failed. Please try again.")

    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 2. POST /ai/search   — pure semantic search
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/ai/search")
def ai_semantic_search(
    body: SemanticSearchRequest,
    db:   Session = Depends(get_db),
):
    """
    Semantic product search — embeds the query and finds nearest catalog matches.
    No intent detection overhead.  Use when the caller knows it's a search query.

    Supports all standard filters: category, price_tier, price range, skin_type, concerns.
    """
    _assert_price_tier(body.price_tier)

    result = search_products_semantic(
        db,
        body.query,
        limit          = body.limit,
        category_id    = body.category_id,
        price_tier     = body.price_tier,
        min_price      = body.min_price,
        max_price      = body.max_price,
        skin_type      = body.skin_type,
        concerns       = body.concerns or None,
        include_scores = body.include_scores,
    )
    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 3. GET /ai/similar/{barcode}   — product vector similarity
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ai/similar/{barcode}")
def ai_similar(
    barcode:       str,
    limit:         int  = Query(10, ge=1, le=50),
    same_category: bool = Query(False, description="Restrict to same product category"),
    include_scores:bool = Query(False),
    db:  Session        = Depends(get_db),
):
    """
    Products most similar to the given barcode's embedding vector.

    Falls back to featured products if the source product has no embedding yet.
    """
    result = similar_products(
        db, barcode, limit=limit, same_category=same_category, include_scores=include_scores
    )

    # Graceful fallback: if no embedding → return featured products
    if not result.get("found"):
        fallback = get_featured(db, limit=limit)
        fallback["strategy"]     = "similar_fallback_featured"
        fallback["source_barcode"]= barcode
        return {"success": True, "data": fallback, "fallback": True}

    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 4. GET /ai/cross-sell/{barcode}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ai/cross-sell/{barcode}")
def ai_cross_sell(
    barcode: str,
    limit:   int  = Query(8, ge=1, le=30),
    include_scores: bool = Query(False),
    db:      Session    = Depends(get_db),
):
    """
    Complementary products (different category) for cross-sell placement.
    "Customers who bought this also bought ..."
    """
    result = cross_sell_recommendations(
        db, barcode, limit=limit, include_scores=include_scores
    )
    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 5. GET /ai/upsell/{barcode}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ai/upsell/{barcode}")
def ai_upsell(
    barcode: str,
    limit:   int  = Query(6, ge=1, le=20),
    include_scores: bool = Query(False),
    db:      Session    = Depends(get_db),
):
    """
    Higher-tier alternatives in the same category (upsell placement).
    "Upgrade to a premium version ..."
    """
    result = upsell_recommendations(
        db, barcode, limit=limit, include_scores=include_scores
    )
    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 6. POST /ai/skincare   — skin type + concern matching
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/ai/skincare")
def ai_skincare(
    body: SkincareRequest,
    db:   Session = Depends(get_db),
):
    """
    Skincare product recommendations tailored to skin type and concerns.

    Examples
    --------
    { "skin_type": "dry", "concerns": ["dryness", "sensitivity"], "price_tier": "Mid" }
    { "skin_type": "oily", "concerns": ["acne", "pores"] }
    """
    _assert_price_tier(body.price_tier)

    result = skincare_recommendations(
        db,
        skin_type      = body.skin_type,
        concerns       = body.concerns or None,
        price_tier     = body.price_tier,
        category_id    = body.category_id,
        limit          = body.limit,
        include_scores = body.include_scores,
    )
    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 7. POST /ai/fragrance   — perfume similarity search
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/ai/fragrance")
def ai_fragrance(
    body: FragranceRequest,
    db:   Session = Depends(get_db),
):
    """
    Find perfumes similar to a named reference fragrance.

    Example
    -------
    { "reference_name": "Dior Sauvage", "price_tier": "Premium", "limit": 8 }
    """
    _assert_price_tier(body.price_tier)

    result = perfume_similarity_search(
        db,
        body.reference_name,
        price_tier     = body.price_tier,
        limit          = body.limit,
        include_scores = body.include_scores,
    )
    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 8. GET /ai/personalised   — user preference vector blended search
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ai/personalised")
def ai_personalised(
    user_id:     str            = Query(..., description="User ID"),
    q:           str            = Query("beauty products"),
    category_id: Optional[int]  = Query(None),
    limit:       int            = Query(10, ge=1, le=50),
    include_scores: bool        = Query(False),
    db:          Session        = Depends(get_db),
):
    """
    Personalised recommendations that blend the query embedding with the
    user's stored preference embedding (built from past clicks/purchases).

    Falls back to semantic search if no preference profile exists yet.
    """
    result = personalised_for_user(
        db, user_id, q, limit=limit, category_id=category_id, include_scores=include_scores
    )
    return {"success": True, "data": result}


# ══════════════════════════════════════════════════════════════════════════════
# 9. POST /events/product/{barcode}   — behavioral feedback
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/events/product/{barcode}")
def log_product_event(
    barcode:          str,
    body:             ProductEventRequest,
    background_tasks: BackgroundTasks,
    db:               Session = Depends(get_db),
):
    """
    Log a behavioral event (view / click / purchase / recommendation_accepted).

    Events are used to:
      1. Update the user's preference embedding (async background task).
      2. Feed into future collaborative-filtering models.
      3. Power the ai_score recalculation pipeline.

    A purchase or recommendation_accepted event has higher weight (1.0)
    than a view (0.3) or click (0.7).
    """
    # Persist the event
    event = ProductEvent(
        user_id    = body.user_id,
        session_id = body.session_id,
        barcode    = barcode,
        event_type = body.event_type,
        source     = body.source,
        position   = body.position,
        event_metadata = body.metadata,
    )
    db.add(event)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("event_persist_error", extra={"error": str(exc)})
        raise ServiceError("Failed to log product event.")

    # Update user preference profile asynchronously
    if body.user_id and body.event_type in {
        "purchase", "recommendation_accepted", "click"
    }:
        weight_map = {
            "purchase":                1.0,
            "recommendation_accepted": 0.9,
            "click":                   0.7,
        }
        weight = weight_map.get(body.event_type, 0.5)
        background_tasks.add_task(
            update_user_preference_profile,
            db      = db,
            user_id = body.user_id,
            barcode = barcode,
            event_weight = weight,
        )

    return {
        "success": True,
        "message": "Event logged.",
        "event_type": body.event_type,
        "barcode":    barcode,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 10. GET /ai/embeddings/status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ai/embeddings/status", response_model=None)
def embedding_status(db: Session = Depends(get_db)):
    """
    Embedding coverage statistics.
    Use this to decide when to activate RECOMMENDATION_SCORER=hybrid_ai.
    """
    stats = get_embedding_stats(db)
    return {"success": True, "data": stats}


# ══════════════════════════════════════════════════════════════════════════════
# 11. POST /ai/embeddings/trigger   — trigger background embedding pipeline
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/ai/embeddings/trigger")
async def trigger_embedding(
    body:             EmbedTriggerRequest = EmbedTriggerRequest(),
    background_tasks: BackgroundTasks     = BackgroundTasks(),
):
    """
    Trigger the product embedding background job.

    By default processes all un-embedded products in batches of 50.
    Use force_all=true to re-embed the entire catalog (e.g. after prompt change).
    Use limit=N to process only N products (for testing).

    The job runs asynchronously — this endpoint returns immediately.
    Check /ai/embeddings/status for progress.
    """
    background_tasks.add_task(
        embed_new_products,
        limit      = body.limit,
        force_all  = body.force_all,
        batch_size = body.batch_size,
    )

    return {
        "success": True,
        "message": (
            f"Embedding job queued. Processing "
            f"{'all products' if body.limit == 0 else f'up to {body.limit} products'}. "
            f"Poll GET /ai/embeddings/status for progress."
        ),
        "force_all":  body.force_all,
        "batch_size": body.batch_size,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 12. PATCH /products/{barcode}/embedding/refresh   — single product re-embed
# ══════════════════════════════════════════════════════════════════════════════

@router.patch("/products/{barcode}/embedding/refresh")
async def refresh_product_embedding(
    barcode: str,
    db:      Session = Depends(get_db),
):
    """
    Immediately re-generate the embedding for a single product.
    Use after editing a product's name, description, or tags.
    Runs synchronously — returns the new embedding_updated_at timestamp.
    """
    from models import Product, ProductSearchIndex
    from services.embedding import _build_text_for_product, compute_base_ai_score
    from datetime import datetime

    product = db.query(Product).filter(
        Product.barcode == barcode,
        Product.deleted_at.is_(None),
    ).first()

    if not product:
        raise NotFoundError(f"Product '{barcode}' not found.")

    psi = db.query(ProductSearchIndex).filter(
        ProductSearchIndex.product_id == barcode
    ).first()
    psi_map = {barcode: psi} if psi else {}

    text = _build_text_for_product(product, psi_map)
    if not text.strip():
        raise AppValidationError(f"Product '{barcode}' has no text fields to embed.")

    try:
        vector = embed_single(text)
    except Exception as exc:
        raise ServiceError(f"Embedding generation failed: {exc}")

    if not vector:
        raise ServiceError("OpenAI returned an empty embedding vector.")

    product.embedding            = vector
    product.embedding_text       = text
    product.embedding_updated_at = datetime.now()
    product.ai_score             = compute_base_ai_score(product)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise ServiceError(f"Failed to persist embedding: {exc}")

    return {
        "success":             True,
        "barcode":             barcode,
        "embedding_updated_at":product.embedding_updated_at.isoformat(),
        "ai_score":            float(product.ai_score or 0),
        "embedding_text_len":  len(text),
    }
