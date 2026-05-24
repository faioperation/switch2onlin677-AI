"""
schemas/upload.py
=================
Pydantic v2 schemas for the Excel/CSV bulk product upload flow.

ProductUploadRow  → validates one parsed row before DB upsert
UploadRowError    → structured per-row error log
UploadResult      → final summary returned by POST /products/upload
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from schemas.product import PriceTierEnum, ProductStatusEnum


# ── Per-Row Upload Schema ─────────────────────────────────────────────────────

class ProductUploadRow(BaseModel):
    """
    Represents one validated row from the Excel/CSV upload.

    Rules:
      - barcode is the only required field; all others are optional.
      - Empty / NaN / "null" / "none" strings → None (handled by pre-validators).
      - Enum fields with invalid values → None stored, warning logged.
      - Boolean fields accept: 1/0, true/false, yes/no, y/n (case-insensitive).
      - Numeric fields with non-numeric values → None stored, warning logged.
    """

    # ── Required ──────────────────────────────────────────────────────────────
    barcode: str = Field(..., min_length=1)

    # ── Identity ──────────────────────────────────────────────────────────────
    item_code:      Optional[str] = None
    item_name:      Optional[str] = None
    sap_product_id: Optional[str] = None

    # ── Relations ─────────────────────────────────────────────────────────────
    brand_name:       Optional[str] = None
    category_name:    Optional[str] = None
    subcategory_name: Optional[str] = None

    # ── Display ───────────────────────────────────────────────────────────────
    description: Optional[str] = None
    image_url:   Optional[str] = None

    # ── AI/Search ─────────────────────────────────────────────────────────────
    skin_type: Optional[str] = None
    concerns:  Optional[str] = None   # raw string — parsed by service (pipe/comma sep)
    tags:      Optional[str] = None   # raw string — parsed by service

    # ── Classification (new) ──────────────────────────────────────────────────
    price_tier:     Optional[PriceTierEnum]    = None
    brand_family:   Optional[str]              = Field(None, max_length=100)
    product_status: Optional[ProductStatusEnum]= ProductStatusEnum.active

    # ── Recommendation Flags (new) — only written if cell has a value ─────────
    is_best_selling:               Optional[bool]    = None
    is_new_arrival:                Optional[bool]    = None
    is_recommended:                Optional[bool]    = None
    is_cod_recommended:            Optional[bool]    = None
    recommendation_priority:       Optional[int]     = Field(None, ge=0, le=9999)
    recommendation_score_override: Optional[Decimal] = Field(None, ge=0, le=999)

    # ── Bundle (new) ──────────────────────────────────────────────────────────
    bundle_group:            Optional[str]     = Field(None, max_length=100)
    bundle_discount_percent: Optional[Decimal] = Field(None, ge=0, le=100)

    # ─────────────────────────────────────────────────────────────────────────
    # Validators
    # ─────────────────────────────────────────────────────────────────────────

    @field_validator(
        "barcode", "item_code", "item_name", "sap_product_id",
        "brand_name", "category_name", "subcategory_name",
        "description", "image_url", "skin_type", "concerns", "tags",
        "brand_family", "bundle_group",
        mode="before",
    )
    @classmethod
    def clean_string(cls, v: Any) -> Optional[str]:
        """Strip whitespace; convert NaN / empty / null-like strings to None."""
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in {"", "nan", "none", "null", "n/a", "na"}:
            return None
        return s

    @field_validator("price_tier", mode="before")
    @classmethod
    def validate_price_tier(cls, v: Any) -> Optional[PriceTierEnum]:
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in {"", "nan", "none", "null"}:
            return None
        # Case-insensitive match
        mapping = {e.value.lower(): e for e in PriceTierEnum}
        result = mapping.get(s.lower())
        if result is None:
            # Return None and let the service log a warning — don't hard-fail the row
            return None
        return result

    @field_validator("product_status", mode="before")
    @classmethod
    def validate_product_status(cls, v: Any) -> ProductStatusEnum:
        if v is None:
            return ProductStatusEnum.active
        s = str(v).strip().lower()
        if s in {"", "nan", "none", "null"}:
            return ProductStatusEnum.active
        mapping = {e.value: e for e in ProductStatusEnum}
        return mapping.get(s, ProductStatusEnum.active)

    @field_validator(
        "is_best_selling", "is_new_arrival",
        "is_recommended", "is_cod_recommended",
        mode="before",
    )
    @classmethod
    def validate_bool(cls, v: Any) -> Optional[bool]:
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in {"", "nan", "none", "null"}:
            return None
        if s in {"1", "true", "yes", "y"}:
            return True
        if s in {"0", "false", "no", "n"}:
            return False
        return None     # unrecognised → treat as not provided

    @field_validator("recommendation_priority", mode="before")
    @classmethod
    def validate_priority(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in {"", "nan", "none", "null"}:
            return None
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None

    @field_validator(
        "recommendation_score_override",
        "bundle_discount_percent",
        mode="before",
    )
    @classmethod
    def validate_decimal(cls, v: Any) -> Optional[Decimal]:
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in {"", "nan", "none", "null"}:
            return None
        try:
            return Decimal(s)
        except Exception:
            return None

    @field_validator("barcode", mode="after")
    @classmethod
    def normalize_barcode(cls, v: str) -> str:
        """Strip trailing .0 that Excel adds when reading numeric barcodes as float."""
        if v.endswith(".0"):
            v = v[:-2]
        return v.strip()

    model_config = {"from_attributes": True}


# ── Per-Row Error Log ─────────────────────────────────────────────────────────

class UploadRowError(BaseModel):
    """Structured error entry for a single skipped row."""
    row:     int
    barcode: Optional[str] = None
    error:   str


# ── Upload Result (API response) ──────────────────────────────────────────────

class UploadResult(BaseModel):
    """
    Returned by POST /products/upload.

    created  → new products inserted
    updated  → existing products updated
    skipped  → rows skipped (missing barcode or DB error)
    errors   → per-row structured error list (capped at 100)
    """
    filename:   str
    total_rows: int
    created:    int
    updated:    int
    skipped:    int
    dry_run:    bool
    errors:     list[UploadRowError] = Field(default_factory=list)
