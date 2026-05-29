"""
schemas/ai_recommendation.py
=============================
Pydantic v2 request/response schemas for the AI recommendation API.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


# ── Request schemas ───────────────────────────────────────────────────────────

class RecommendRequest(BaseModel):
    """
    Universal intent-aware recommendation request.
    Used by the chatbot for any natural-language query.
    """
    query:           str            = Field(..., min_length=1, max_length=500, description="User's natural-language query")
    user_id:         Optional[str]  = Field(None, description="User ID for personalisation")
    session_id:      Optional[str]  = Field(None, description="Session token for multi-turn context")
    locale:          str            = Field("en", pattern="^(en|ar)$", description="Response language: en | ar")
    limit:           int            = Field(10, ge=1, le=50)
    category_id:     Optional[int]  = Field(None, description="Pre-filter by category FK")
    price_tier:      Optional[str]  = Field(None, description="Budget | Mid | Premium | Luxury")
    cart_barcodes:   list[str]      = Field(default_factory=list, description="Products in cart — excluded from results")
    viewed_barcodes: list[str]      = Field(default_factory=list, description="Products already seen — deprioritised")
    include_scores:  bool           = Field(False, description="Include internal scoring breakdown in response")


class SemanticSearchRequest(BaseModel):
    """Structured semantic search — no intent detection overhead."""
    query:       str            = Field(..., min_length=1, max_length=500)
    limit:       int            = Field(10, ge=1, le=50)
    category_id: Optional[int]  = None
    price_tier:  Optional[str]  = None
    min_price:   Optional[float]= None
    max_price:   Optional[float]= None
    skin_type:   Optional[str]  = None
    concerns:    list[str]      = Field(default_factory=list)
    include_scores: bool        = False


class SkincareRequest(BaseModel):
    """Skincare recommendation parameters."""
    skin_type:   Optional[str]      = Field(None, description="dry | oily | sensitive | combination | normal")
    concerns:    list[str]          = Field(default_factory=list, description="acne, dryness, aging, etc.")
    price_tier:  Optional[str]      = None
    category_id: Optional[int]      = None
    limit:       int                = Field(10, ge=1, le=50)
    include_scores: bool            = False


class FragranceRequest(BaseModel):
    """Fragrance similarity search parameters."""
    reference_name: str        = Field(..., min_length=1, max_length=200, description="Reference perfume name")
    price_tier:     Optional[str] = None
    limit:          int        = Field(10, ge=1, le=50)
    include_scores: bool       = False


class ProductEventRequest(BaseModel):
    """Behavioral event for the feedback loop."""
    user_id:    Optional[str] = None
    session_id: Optional[str] = None
    event_type: str           = Field(
        ...,
        pattern="^(view|click|purchase|recommendation_accepted|recommendation_rejected)$",
        description="view | click | purchase | recommendation_accepted | recommendation_rejected",
    )
    source:     Optional[str] = Field(None, description="chatbot | api | frontend | recommendation")
    position:   Optional[int] = Field(None, ge=0, description="Rank position in recommendation list")
    metadata:   Optional[dict[str, Any]] = None


class EmbedTriggerRequest(BaseModel):
    """Trigger the background embedding pipeline."""
    limit:      int  = Field(0,     ge=0, description="Max products to embed (0 = all pending)")
    force_all:  bool = Field(False,       description="Re-embed all products regardless of staleness")
    batch_size: int  = Field(50,    ge=1, le=200)


# ── Response schemas ──────────────────────────────────────────────────────────

class ProductScores(BaseModel):
    """Scoring breakdown returned when include_scores=true."""
    final:      float
    semantic:   float
    editorial:  float
    popularity: float
    stock:      float
    freshness:  float
    reason:     str


class ProductItem(BaseModel):
    """Single product in any recommendation response."""
    id:             str
    barcode:        str
    name:           str
    brand:          str
    category:       str
    subcategory:    str
    description:    str
    image_url:      Optional[str]
    price:          str            # formatted IQD string  e.g. "45,000 IQD"
    raw_price:      float
    available_qty:  int
    skin_type:      Optional[str]
    concerns:       list
    tags:           list
    price_tier:     Optional[str]
    brand_family:   Optional[str]
    is_best_selling: bool
    is_new_arrival:  bool
    order_link:     Optional[str]
    scores:         Optional[ProductScores] = None


class AIRecommendResponse(BaseModel):
    """Standard response envelope for all AI recommendation endpoints."""
    found:       bool
    total_found: int = 0
    returned:    int = 0
    strategy:    str
    intent:      Optional[str] = None
    products:    list[dict]    = Field(default_factory=list)
    # Optional enrichment fields (set per-endpoint)
    reference_name:          Optional[str] = None
    detected_skin_type:      Optional[str] = None
    detected_concerns:       Optional[list[str]] = None
    detected_category:       Optional[str] = None
    detected_price_tier:     Optional[str] = None
    source_barcode:          Optional[str] = None


class EmbeddingStatusResponse(BaseModel):
    """Response for GET /ai/embeddings/status."""
    total_products:    int
    embedded_products: int
    pending_products:  int
    coverage_pct:      float
    embed_model:       str
    embed_dimensions:  int


class EmbedTriggerResponse(BaseModel):
    """Response for POST /ai/embeddings/trigger."""
    success:   bool
    processed: int = 0
    succeeded: int = 0
    failed:    int = 0
    skipped:   int = 0
    duration:  Optional[float] = None
    message:   Optional[str]  = None
