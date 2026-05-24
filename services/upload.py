"""
services/upload.py
==================
Handles bulk CSV/XLSX product uploads.

Key behaviours:
  - barcode is the only required column/field; all others are optional.
  - Each row is validated through ProductUploadRow (Pydantic v2) before any
    DB operation, giving structured per-row error messages.
  - Each row runs inside its own DB savepoint so a single failure never
    corrupts the session for subsequent rows.
  - Recommendation flags (is_best_selling, is_new_arrival, is_recommended,
    is_cod_recommended, recommendation_priority, recommendation_score_override)
    are ONLY written when the Excel cell has an explicit value.
    Empty / NaN cells leave the existing DB value untouched.
  - SAP-owned fields (price, available_qty) are written from the Excel only
    when present; the SAP sync job will overwrite them on its next run.
  - Brand / Category / Subcategory are looked up or created with an
    in-memory cache to avoid repeated DB round-trips per upload batch.
"""

from __future__ import annotations

import io
import math
from typing import Any

import pandas as pd
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Brand, Category, Product, ProductSearchIndex, Subcategory
from schemas.upload import ProductUploadRow, UploadResult, UploadRowError

# ── Column registry ───────────────────────────────────────────────────────────

REQUIRED_PRODUCT_UPLOAD_COLUMNS: list[str] = [
    "barcode",
]

OPTIONAL_PRODUCT_UPLOAD_COLUMNS: list[str] = [
    # Identity
    "item_code",
    "item_name",
    "sap_product_id",
    # Relations
    "brand_name",
    "category_name",
    "subcategory_name",
    # Display
    "description",
    "image_url",
    # AI / Search
    "skin_type",
    "concerns",
    "tags",
    # Pricing (SAP owns these — included for initial seed only)
    "price",
    "available_qty",
    # Classification (new)
    "price_tier",
    "brand_family",
    "product_status",
    # Recommendation flags (new)
    "is_best_selling",
    "is_new_arrival",
    "is_recommended",
    "is_cod_recommended",
    "recommendation_priority",
    "recommendation_score_override",
    # Bundle (new)
    "bundle_group",
    "bundle_discount_percent",
    # Legacy
    "best_selling_scope",
    "sales_rank",
]

ALL_PRODUCT_UPLOAD_COLUMNS: list[str] = (
    REQUIRED_PRODUCT_UPLOAD_COLUMNS + OPTIONAL_PRODUCT_UPLOAD_COLUMNS
)

# Alternative tag column names found in some catalog exports.
TAG_ALIASES: list[str] = ["Tag_EN", "Tag_MSA", "Tag_IRQ"]


# ── Low-level value cleaners (used inside the service and by helpers) ─────────

def _clean(value: Any) -> str | None:
    """Return stripped string or None for empty / NaN / null-like values."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    return None if s.lower() in {"", "nan", "none", "null", "n/a", "na"} else s


def _clean_number(value: Any) -> float | None:
    s = _clean(value)
    if s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _clean_integer(value: Any) -> int | None:
    s = _clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


# ── Parsers for multi-value fields ────────────────────────────────────────────

def parse_concerns(value: Any) -> list[str]:
    """Split pipe- or comma-separated concerns string into a list."""
    raw = _clean(value)
    if not raw:
        return []
    for sep in ("|", ","):
        if sep in raw:
            return [p.strip() for p in raw.split(sep) if p.strip()]
    return [raw]


def parse_tags(*tag_values: Any) -> list[str]:
    """Merge multiple tag columns into a deduplicated list."""
    seen: set[str] = set()
    result: list[str] = []
    for val in tag_values:
        raw = _clean(val)
        if not raw:
            continue
        for part in raw.split(","):
            part = part.strip()
            if part and part.lower() not in seen:
                seen.add(part.lower())
                result.append(part)
    return result


# ── File reading ──────────────────────────────────────────────────────────────

def read_product_upload_file(filename: str, content: bytes) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=str)
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content), dtype=str)
    raise ValueError("Only .xlsx and .csv files are supported.")


# ── Column presence check ─────────────────────────────────────────────────────

def validate_upload_columns(df: pd.DataFrame) -> dict[str, str]:
    """
    Returns a mapping of logical_name → actual_column_name found in df.
    Raises ValueError if any REQUIRED column is missing.
    """
    normalized = {str(col).strip(): col for col in df.columns}
    missing = [c for c in REQUIRED_PRODUCT_UPLOAD_COLUMNS if c not in normalized]
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))
    return normalized


# ── Relation cache helpers ────────────────────────────────────────────────────

def _get_or_create_brand(db: Session, name: str | None, cache: dict) -> int | None:
    if not name:
        return None
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    obj = db.query(Brand).filter(func.lower(Brand.name) == key).first()
    if not obj:
        obj = Brand(name=name.strip())
        db.add(obj)
        db.flush()
    cache[key] = obj.id
    return obj.id


def _get_or_create_category(db: Session, name: str | None, cache: dict) -> int | None:
    if not name:
        return None
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    obj = db.query(Category).filter(func.lower(Category.name) == key).first()
    if not obj:
        obj = Category(name=name.strip())
        db.add(obj)
        db.flush()
    cache[key] = obj.id
    return obj.id


def _get_or_create_subcategory(
    db: Session,
    name: str | None,
    category_id: int | None,
    cache: dict,
) -> int | None:
    if not name:
        return None
    key = (name.strip().lower(), category_id)
    if key in cache:
        return cache[key]
    q = db.query(Subcategory).filter(func.lower(Subcategory.name) == name.strip().lower())
    if category_id is not None:
        q = q.filter(Subcategory.category_id == category_id)
    obj = q.first()
    if not obj:
        obj = Subcategory(name=name.strip(), category_id=category_id)
        db.add(obj)
        db.flush()
    cache[key] = obj.id
    return obj.id


# ── Search-text builder ───────────────────────────────────────────────────────

def build_search_text(
    item_code: str | None,
    item_name: str | None,
    brand_name: str | None,
    category_name: str | None,
    subcategory_name: str | None,
) -> str:
    parts = [
        s for s in [item_code, brand_name, category_name, subcategory_name, item_name]
        if s
    ]
    return " ".join(parts).lower()


# ── Row → raw dict helper ─────────────────────────────────────────────────────

def _row_to_raw(row: pd.Series, col_map: dict[str, str]) -> dict[str, Any]:
    """Extract every known column from a DataFrame row into a plain dict."""

    def get(logical: str) -> Any:
        actual = col_map.get(logical)
        return row.get(actual) if actual is not None else None

    # Tags: merge TAG_ALIASES + "tags" column
    tag_col_actuals = [
        col_map[c] for c in TAG_ALIASES if c in col_map
    ] + ([col_map["tags"]] if "tags" in col_map else [])
    merged_tags = parse_tags(*[row.get(c) for c in tag_col_actuals])

    return {
        "barcode":                      get("barcode"),
        "item_code":                    get("item_code"),
        "item_name":                    get("item_name"),
        "sap_product_id":               get("sap_product_id"),
        "brand_name":                   get("brand_name"),
        "category_name":                get("category_name"),
        "subcategory_name":             get("subcategory_name"),
        "description":                  get("description"),
        "image_url":                    get("image_url"),
        "skin_type":                    get("skin_type"),
        "concerns":                     get("concerns"),
        "tags":                         ", ".join(merged_tags) if merged_tags else None,
        "price_tier":                   get("price_tier"),
        "brand_family":                 get("brand_family"),
        "product_status":               get("product_status"),
        "is_best_selling":              get("is_best_selling"),
        "is_new_arrival":               get("is_new_arrival"),
        "is_recommended":               get("is_recommended"),
        "is_cod_recommended":           get("is_cod_recommended"),
        "recommendation_priority":      get("recommendation_priority"),
        "recommendation_score_override":get("recommendation_score_override"),
        "bundle_group":                 get("bundle_group"),
        "bundle_discount_percent":      get("bundle_discount_percent"),
    }


# ── Main upsert entry-point ───────────────────────────────────────────────────

def upsert_product_upload(
    db: Session,
    filename: str,
    content: bytes,
    dry_run: bool = False,
) -> UploadResult:
    """
    Parse, validate and bulk-upsert products from an Excel / CSV file.

    Returns an UploadResult with created/updated/skipped counts and
    per-row error details.
    """
    df = read_product_upload_file(filename, content)
    col_map = validate_upload_columns(df)

    created_count = 0
    updated_count = 0
    skipped_count = 0
    row_errors: list[UploadRowError] = []

    brand_cache:       dict = {}
    category_cache:    dict = {}
    subcategory_cache: dict = {}

    for index, row in df.iterrows():
        row_number: int = int(index) + 2   # +1 header, +1 for 1-based row number

        # ── 1. Extract raw values from the DataFrame row ──────────────────────
        raw = _row_to_raw(row, col_map)

        # ── 2. Pydantic validation — gives clean, typed values ────────────────
        try:
            data = ProductUploadRow(**raw)
        except ValidationError as exc:
            skipped_count += 1
            row_errors.append(UploadRowError(
                row=row_number,
                barcode=_clean(raw.get("barcode")),
                error="; ".join(
                    f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                    for e in exc.errors()
                ),
            ))
            continue

        # ── 3. Each row runs in its own savepoint ─────────────────────────────
        savepoint = db.begin_nested()
        row_action: str | None = None

        try:
            brand_id = _get_or_create_brand(db, data.brand_name, brand_cache)
            category_id = _get_or_create_category(db, data.category_name, category_cache)
            subcategory_id = _get_or_create_subcategory(
                db, data.subcategory_name, category_id, subcategory_cache
            )

            existing = (
                db.query(Product)
                .filter(Product.barcode == data.barcode)
                .first()
            )

            if existing:
                product = existing
                row_action = "updated"
                updated_count += 1
            else:
                product = Product(barcode=data.barcode)
                db.add(product)
                row_action = "created"
                created_count += 1

            # ── 4. Apply all non-None field values ────────────────────────────

            # Identity / display
            if data.item_code      is not None: product.item_code      = data.item_code
            if data.item_name      is not None: product.item_name      = data.item_name
            if data.sap_product_id is not None: product.sap_product_id = data.sap_product_id
            if data.description    is not None: product.description    = data.description
            if data.image_url      is not None: product.image_url      = data.image_url

            # Relations
            if brand_id       is not None: product.brand_id       = brand_id
            if category_id    is not None: product.category_id    = category_id
            if subcategory_id is not None: product.subcategory_id = subcategory_id

            # AI / Search
            if data.skin_type is not None:
                product.skin_type = data.skin_type
            concerns_parsed = parse_concerns(data.concerns)
            if concerns_parsed:
                product.concerns = concerns_parsed
            tags_parsed = parse_tags(data.tags) if data.tags else []
            if tags_parsed:
                product.tags = tags_parsed

            # Pricing — SAP will overwrite on next sync, but allow seed via upload
            price_val = _clean_number(
                row.get(col_map["price"]) if "price" in col_map else None
            )
            qty_val = _clean_integer(
                row.get(col_map["available_qty"]) if "available_qty" in col_map else None
            )
            if price_val is not None: product.price         = price_val
            if qty_val   is not None: product.available_qty = qty_val

            # Classification (new)
            if data.price_tier     is not None:
                product.price_tier     = data.price_tier.value
            if data.brand_family   is not None:
                product.brand_family   = data.brand_family
            if data.product_status is not None:
                product.product_status = data.product_status.value

            # ── 5. Recommendation flags — only write when cell had a value ────
            #       None means "cell was empty" → leave existing DB value alone.
            if data.is_best_selling               is not None:
                product.is_best_selling               = data.is_best_selling
            if data.is_new_arrival                is not None:
                product.is_new_arrival                = data.is_new_arrival
            if data.is_recommended                is not None:
                product.is_recommended                = data.is_recommended
            if data.is_cod_recommended            is not None:
                product.is_cod_recommended            = data.is_cod_recommended
            if data.recommendation_priority       is not None:
                product.recommendation_priority       = data.recommendation_priority
            if data.recommendation_score_override is not None:
                product.recommendation_score_override = data.recommendation_score_override

            # Bundle (new)
            if data.bundle_group            is not None:
                product.bundle_group            = data.bundle_group
            if data.bundle_discount_percent  is not None:
                product.bundle_discount_percent = data.bundle_discount_percent

            # Legacy
            legacy_scope = _clean(
                row.get(col_map["best_selling_scope"]) if "best_selling_scope" in col_map else None
            )
            legacy_rank = _clean_integer(
                row.get(col_map["sales_rank"]) if "sales_rank" in col_map else None
            )
            if legacy_scope is not None: product.best_selling_scope = legacy_scope
            if legacy_rank  is not None: product.sales_rank         = legacy_rank

            # ── 6. Sync ProductSearchIndex ────────────────────────────────────
            search_text = build_search_text(
                item_code=data.item_code,
                item_name=data.item_name,
                brand_name=data.brand_name,
                category_name=data.category_name,
                subcategory_name=data.subcategory_name,
            )
            idx = (
                db.query(ProductSearchIndex)
                .filter(ProductSearchIndex.product_id == data.barcode)
                .first()
            )
            if not idx:
                idx = ProductSearchIndex(product_id=data.barcode)
                db.add(idx)

            idx.item_code        = data.item_code
            idx.barcode          = data.barcode
            idx.item_name        = data.item_name
            idx.brand_name       = data.brand_name
            idx.category_name    = data.category_name
            idx.subcategory_name = data.subcategory_name
            idx.search_text      = search_text

            savepoint.commit()

        except Exception as exc:
            savepoint.rollback()
            if row_action == "updated":
                updated_count -= 1
            elif row_action == "created":
                created_count -= 1
            skipped_count += 1
            row_errors.append(UploadRowError(
                row=row_number,
                barcode=data.barcode,
                error=str(exc),
            ))

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return UploadResult(
        filename=filename,
        total_rows=len(df),
        created=created_count,
        updated=updated_count,
        skipped=skipped_count,
        dry_run=dry_run,
        errors=row_errors[:100],
    )
