"""
schemas/product.py
==================
Pydantic v2 schemas for Product read responses and dashboard update requests.

Separation of concerns:
  - ProductResponse   → API output (GET /products, GET /products/{barcode})
  - ProductUpdateRequest → Dashboard manual field updates (PATCH /products/{barcode})
  - PriceTierEnum / ProductStatusEnum → shared enums reused by upload schema
"""

from __future__ import annotations

import enum
from decimal import Decimal
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Shared Enums ──────────────────────────────────────────────────────────────

class PriceTierEnum(str, enum.Enum):
    budget  = "Budget"
    mid     = "Mid"
    premium = "Premium"
    luxury  = "Luxury"


class ProductStatusEnum(str, enum.Enum):
    active   = "active"
    inactive = "inactive"
    draft    = "draft"


# ── Read Schema (API response) ────────────────────────────────────────────────

class ProductResponse(BaseModel):
    """Returned by GET /products and GET /products/{barcode}."""

    # Identity
    barcode:        str
    item_code:      Optional[str]      = None
    sap_product_id: Optional[str]      = None

    # Display
    item_name:   Optional[str]         = None
    description: Optional[str]         = None
    image_url:   Optional[str]         = None

    # Relations
    brand_id:       Optional[int]      = None
    category_id:    Optional[int]      = None
    subcategory_id: Optional[int]      = None

    # AI/Search
    skin_type: Optional[str]           = None
    concerns:  Optional[list[str]]     = None
    tags:      Optional[list[str]]     = None

    # Pricing — from SAP
    price:         Optional[Decimal]   = None
    available_qty: Optional[int]       = None

    # Classification
    price_tier:     Optional[PriceTierEnum]    = None
    brand_family:   Optional[str]              = None
    product_status: ProductStatusEnum          = ProductStatusEnum.active

    # Recommendation flags
    is_best_selling:               Optional[bool]    = None
    is_new_arrival:                Optional[bool]    = None
    is_recommended:                Optional[bool]    = None
    is_cod_recommended:            Optional[bool]    = None
    recommendation_priority:       Optional[int]     = None
    recommendation_score_override: Optional[Decimal] = None

    # Legacy
    best_selling_scope: Optional[str] = None
    sales_rank:         Optional[int] = None

    # Bundle
    bundle_group:            Optional[str]     = None
    bundle_discount_percent: Optional[Decimal] = None

    # SAP Sync
    last_synced_sap: Optional[datetime] = None

    # Timestamps
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Dashboard Update Schema (PATCH /products/{barcode}) ───────────────────────

class ProductUpdateRequest(BaseModel):
    """
    Used by the admin dashboard to manually update recommendation flags
    and classification fields.

    SAP-owned fields (price, available_qty) are intentionally excluded —
    those can only be updated by the SAP sync job.

    All fields are Optional: only provided fields are updated (PATCH semantics).
    """

    # Display — editable from dashboard
    item_name:   Optional[str] = None
    description: Optional[str] = None
    image_url:   Optional[str] = None

    # Classification
    price_tier:     Optional[PriceTierEnum]   = None
    brand_family:   Optional[str]             = Field(None, max_length=100)
    product_status: Optional[ProductStatusEnum] = None

    # Recommendation flags — NEVER touched by SAP sync
    is_best_selling:               Optional[bool]               = None
    is_new_arrival:                Optional[bool]               = None
    is_recommended:                Optional[bool]               = None
    is_cod_recommended:            Optional[bool]               = None
    recommendation_priority:       Optional[int]                = Field(None, ge=0, le=9999)
    recommendation_score_override: Optional[Decimal]            = Field(None, ge=0, le=999)

    # Legacy
    best_selling_scope: Optional[str] = Field(None, max_length=100)
    sales_rank:         Optional[int] = Field(None, ge=0)

    # Bundle
    bundle_group:            Optional[str]     = Field(None, max_length=100)
    bundle_discount_percent: Optional[Decimal] = Field(None, ge=0, le=100)

    # AI/Search — editable from dashboard
    skin_type: Optional[str]       = Field(None, max_length=100)
    concerns:  Optional[list[str]] = None
    tags:      Optional[list[str]] = None

    @field_validator("brand_family", "bundle_group", "best_selling_scope", mode="before")
    @classmethod
    def strip_strings(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        return v if v else None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "ProductUpdateRequest":
        provided = {
            k for k, v in self.model_dump(exclude_none=True).items()
            if v is not None
        }
        if not provided:
            raise ValueError("At least one field must be provided for update.")
        return self

    model_config = {"from_attributes": True}
