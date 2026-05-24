"""
schemas/status.py
=================
Pydantic request/response schemas for the product status and
editorial flags endpoints.

StatusChangeRequest       PATCH /products/{barcode}/status
BulkStatusChangeRequest   POST  /products/bulk/status
FlagsUpdateRequest        PATCH /products/{barcode}/flags
BulkFlagsUpdateRequest    POST  /products/bulk/flags
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

# ── Status ────────────────────────────────────────────────────────────────────

StatusLiteral = Literal["active", "inactive", "draft"]


class StatusChangeRequest(BaseModel):
    """Single-product status transition."""

    status:     StatusLiteral
    changed_by: Optional[str] = Field(
        None,
        description="User ID or system label. Stored in audit log.",
        max_length=255,
    )
    reason: Optional[str] = Field(
        None,
        description="Optional note recorded in the audit log.",
    )


class BulkStatusChangeRequest(BaseModel):
    """Bulk status transition for up to 500 products."""

    barcodes: Annotated[list[str], Field(min_length=1, max_length=500)]
    status:     StatusLiteral
    changed_by: Optional[str] = Field(None, max_length=255)
    reason:     Optional[str] = None


# ── Editorial flags ───────────────────────────────────────────────────────────

class FlagsUpdateRequest(BaseModel):
    """
    Editorial flags for a single product.

    All fields are optional — only supplied fields are updated.
    SAP sync will never overwrite these fields (enforced in services/sync.py).
    """

    is_recommended:              Optional[bool]  = None
    is_new_arrival:              Optional[bool]  = None
    is_best_selling:             Optional[bool]  = None
    is_cod_recommended:          Optional[bool]  = None
    recommendation_priority:     Optional[int]   = Field(None, ge=0, le=9999)
    recommendation_score_override: Optional[float] = Field(None, ge=0, le=999)
    price_tier:    Optional[Literal["Budget", "Mid", "Premium", "Luxury"]] = None
    brand_family:  Optional[str] = Field(None, max_length=100)
    best_selling_scope: Optional[Literal["global", "category", "brand", "subcategory"]] = None


class BulkFlagsUpdateRequest(BaseModel):
    """
    Apply the same editorial flags to up to 500 products at once.

    Example use: mark all perfumes in a new collection as 'is_new_arrival=True'
    and set them to price_tier='Premium'.
    """

    barcodes: Annotated[list[str], Field(min_length=1, max_length=500)]
    flags:    FlagsUpdateRequest
