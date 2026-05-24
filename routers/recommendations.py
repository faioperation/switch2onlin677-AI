"""
routers/recommendations.py
==========================
Recommendation API — 8 read-only (GET) endpoints.

Every endpoint enforces (via recommendation_service):
  • product_status = 'active'
  • available_qty  > 5

Scoring pipeline
----------------
Every endpoint builds a RecommendationContext from the caller-supplied query
params and passes the service result through ScoringPipeline.apply().

Phase 1 (now):   RuleBasedScorer — no-op on order; adds scoring metadata.
Phase 2 (next):  PersonalizationScorer — diversity + ML re-ranking when
                 RECOMMENDATION_SCORER=personalization is set in env.

Optional context params (accepted by all endpoints):
  user_id         — links to user preference profile / embedding store
  session_id      — multi-turn session affinity
  viewed_barcodes — comma-separated barcodes seen this session
  cart_barcodes   — comma-separated barcodes in cart (excluded)
  locale          — "en" | "ar"

Error handling
--------------
AppValidationError (422) is raised directly for invalid enum values.
All other errors propagate to the global handler in main.py.
No local try/except needed — the service layer returns empty-list results,
not exceptions, for "no products found" cases.

Endpoints
---------
GET /recommendations/best-selling
GET /recommendations/new-arrivals
GET /recommendations/featured
GET /recommendations/recommended
GET /recommendations/cod
GET /recommendations/by-tier/{price_tier}
GET /recommendations/by-brand/{brand_family}
GET /recommendations/bundle/{bundle_group}
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.exceptions import AppValidationError, NotFoundError
from core.recommendation_context import RecommendationContext
from database import get_db
from services.recommendation import (
    get_best_selling,
    get_new_arrivals,
    get_featured,
    get_recommended,
    get_cod_recommended,
    get_by_price_tier,
    get_by_brand_family,
    get_bundle,
)
from services.scoring import ScoringPipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])

VALID_PRICE_TIERS = {"Budget", "Mid", "Premium", "Luxury"}
MAX_LIMIT = 50


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _assert_price_tier(price_tier: Optional[str]) -> None:
    """Raise AppValidationError (→ 422) for unrecognised tier strings."""
    if price_tier and price_tier not in VALID_PRICE_TIERS:
        raise AppValidationError(
            f"Invalid price_tier '{price_tier}'. "
            f"Valid values: {sorted(VALID_PRICE_TIERS)}"
        )


def _parse_barcodes(raw: Optional[str]) -> list[str]:
    """Split a comma-separated barcode string into a clean list."""
    if not raw:
        return []
    return [b.strip() for b in raw.split(",") if b.strip()]


# ── Shared context params (injected into every endpoint) ─────────────────────

def _context_params(
    user_id:         Optional[str] = Query(None, description="User ID for personalized scoring"),
    session_id:      Optional[str] = Query(None, description="Session token for multi-turn affinity"),
    viewed_barcodes: Optional[str] = Query(None, description="Comma-separated barcodes viewed this session"),
    cart_barcodes:   Optional[str] = Query(None, description="Comma-separated barcodes in cart (excluded from results)"),
    locale:          str           = Query("en", description="'en' | 'ar'"),
) -> RecommendationContext:
    """FastAPI dependency: build a RecommendationContext from query params."""
    return RecommendationContext.from_params(
        user_id         = user_id,
        session_id      = session_id,
        viewed_barcodes = _parse_barcodes(viewed_barcodes),
        cart_barcodes   = _parse_barcodes(cart_barcodes),
        locale          = locale,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/best-selling")
def best_selling(
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    price_tier:  Optional[str] = Query(None, description="Budget | Mid | Premium | Luxury"),
    limit:       int           = Query(10, ge=1, le=MAX_LIMIT),
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """Products flagged as best-selling, ordered by recommendation_priority."""
    _assert_price_tier(price_tier)
    result = get_best_selling(db, category_id=category_id, price_tier=price_tier, limit=_clamp(limit))
    return ScoringPipeline().apply(result, context)


@router.get("/new-arrivals")
def new_arrivals(
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    price_tier:  Optional[str] = Query(None, description="Budget | Mid | Premium | Luxury"),
    limit:       int           = Query(10, ge=1, le=MAX_LIMIT),
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """Products flagged as new arrivals, ordered by creation date (newest first)."""
    _assert_price_tier(price_tier)
    result = get_new_arrivals(db, category_id=category_id, price_tier=price_tier, limit=_clamp(limit))
    return ScoringPipeline().apply(result, context)


@router.get("/featured")
def featured(
    limit:   int = Query(20, ge=1, le=MAX_LIMIT, description="Split evenly between best-selling and new arrivals"),
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """Combined: best-selling + new arrivals (deduplicated). Used for homepage cold-start."""
    result = get_featured(db, limit=_clamp(limit))
    return ScoringPipeline().apply(result, context)


@router.get("/recommended")
def recommended(
    category_id:  Optional[int] = Query(None, description="Filter by category ID"),
    price_tier:   Optional[str] = Query(None, description="Budget | Mid | Premium | Luxury"),
    brand_family: Optional[str] = Query(None, description="e.g. 'Italian Niche', 'French Designer'"),
    limit:        int           = Query(10, ge=1, le=MAX_LIMIT),
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """Curated recommended products (is_recommended=true), priority-ordered."""
    _assert_price_tier(price_tier)
    result = get_recommended(
        db,
        category_id=category_id,
        price_tier=price_tier,
        brand_family=brand_family,
        limit=_clamp(limit),
    )
    return ScoringPipeline().apply(result, context)


@router.get("/cod")
def cod_recommended(
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    limit:       int           = Query(10, ge=1, le=MAX_LIMIT),
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """Cash-on-Delivery recommended products (is_cod_recommended=true)."""
    result = get_cod_recommended(db, category_id=category_id, limit=_clamp(limit))
    return ScoringPipeline().apply(result, context)


@router.get("/by-tier/{price_tier}")
def by_price_tier(
    price_tier:  str,
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    limit:       int           = Query(10, ge=1, le=MAX_LIMIT),
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """All active in-stock products in a given price tier (Budget|Mid|Premium|Luxury)."""
    normalized = price_tier.strip().title()
    _assert_price_tier(normalized)
    result = get_by_price_tier(db, price_tier=normalized, category_id=category_id, limit=_clamp(limit))
    return ScoringPipeline().apply(result, context)


@router.get("/by-brand/{brand_family}")
def by_brand_family(
    brand_family: str,
    category_id:  Optional[int] = Query(None, description="Filter by category ID"),
    limit:        int           = Query(10, ge=1, le=MAX_LIMIT),
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """All active in-stock products from a given brand family."""
    brand_family = brand_family.strip()
    if not brand_family:
        raise AppValidationError("brand_family path parameter cannot be empty.")
    result = get_by_brand_family(
        db, brand_family=brand_family, category_id=category_id, limit=_clamp(limit)
    )
    return ScoringPipeline().apply(result, context)


@router.get("/bundle/{bundle_group}")
def bundle(
    bundle_group: str,
    db:      Session               = Depends(get_db),
    context: RecommendationContext = Depends(_context_params),
):
    """
    All products in a named bundle group with discount and pricing summary.
    Returns 404 if the bundle doesn't exist or has no active in-stock items.
    """
    bundle_group = bundle_group.strip()
    if not bundle_group:
        raise AppValidationError("bundle_group path parameter cannot be empty.")

    result = get_bundle(db, bundle_group=bundle_group)

    if not result.get("found"):
        raise NotFoundError(result.get("message", f"Bundle '{bundle_group}' not found."))

    # Scoring pipeline runs even on bundles — diversity/cart exclusion still applies.
    return ScoringPipeline().apply(result, context)
