"""
product_upload_service.py
==========================
Handles bulk CSV/XLSX product uploads with smart Brand/Category/Subcategory
lookup-or-create logic, full-field mapping, and productsearchindex synchronization.
"""
import io
import math
from sqlalchemy import func

import pandas as pd
from sqlalchemy.orm import Session

from models import (
    Brand,
    Category,
    Subcategory,
    Product,
    ProductSearchIndex,
)

# ── Column definitions ──────────────────────────────────────────────────────────

REQUIRED_PRODUCT_UPLOAD_COLUMNS = [
    "barcode",
]

OPTIONAL_PRODUCT_UPLOAD_COLUMNS = [
    "item_code",
    "item_name",
    "brand_name",
    "category_name",
    "subcategory_name",
    "sap_product_id",
    "description",
    "image_url",
    "skin_type",
    "concerns",
    "tags",
    "price",
    "available_qty",
    "is_best_selling",
    "best_selling_scope",
    "sales_rank",
]

ALL_PRODUCT_UPLOAD_COLUMNS = (
    REQUIRED_PRODUCT_UPLOAD_COLUMNS + OPTIONAL_PRODUCT_UPLOAD_COLUMNS
)

# Alternative tag columns found in some catalog files.
TAG_ALIASES = ["Tag_EN", "Tag_MSA", "Tag_IRQ"]


# ── Cleanup helpers ─────────────────────────────────────────────────────────────

def clean_value(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def clean_number(value, default=None):
    value = clean_value(value)
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def clean_integer(value, default=None):
    value = clean_value(value)
    if value is None:
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def parse_concerns(value) -> list:
    """Split pipe-separated | or comma-separated concerns into a JSON array."""
    raw = clean_value(value)
    if not raw:
        return []
    parts = []
    for sep in ("|", ","):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep) if p.strip()]
            break
    else:
        parts = [raw] if raw else []
    return parts


def parse_tags(*tag_values) -> list:
    """Merge multiple tag columns into a deduplicated list."""
    merged = []
    for val in tag_values:
        raw = clean_value(val)
        if not raw:
            continue
        for part in raw.split(","):
            part = part.strip()
            if part:
                merged.append(part)
    # Deduplicate preserving order
    seen = set()
    result = []
    for tag in merged:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result


def normalize_barcode(value):
    """Strip trailing .0 added by Excel when a numeric barcode is read as float."""
    value = clean_value(value)
    if not value:
        return None
    if value.endswith(".0"):
        value = value[:-2]
    return value.strip()


def parse_best_selling(value):
    """Convert truthy/falsy strings to integer 0/1, or None if absent."""
    raw = clean_value(value)
    if not raw:
        return None
    return 1 if str(raw).strip() in {"1", "true", "True", "YES", "yes", "Y", "y"} else 0


# ── File reading ────────────────────────────────────────────────────────────────

def read_product_upload_file(filename: str, content: bytes) -> pd.DataFrame:
    lower_name = filename.lower()
    if lower_name.endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(content), sheet_name=0)
    if lower_name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content))
    raise ValueError("Only .xlsx and .csv files are supported.")


# ── Column validation ───────────────────────────────────────────────────────────

def validate_product_upload_columns(df: pd.DataFrame) -> dict:
    normalized_columns = {
        str(column).strip(): column
        for column in df.columns
    }
    missing = [
        col for col in REQUIRED_PRODUCT_UPLOAD_COLUMNS
        if col not in normalized_columns
    ]
    if missing:
        raise ValueError(
            "Missing required columns: " + ", ".join(missing)
        )
    return normalized_columns


# ── Smart get-or-create helpers (with per-upload in-memory cache) ────────────────

def get_or_create_brand(
    db: Session, name: str, cache: dict
) -> int | None:
    if not name or pd.isna(name):
        return None
    name = str(name).strip()
    key = name.lower()
    if key in cache:
        return cache[key]
    existing = db.query(Brand).filter(func.lower(Brand.name) == key).first()
    if existing:
        cache[key] = existing.id
        return existing.id
    new_brand = Brand(name=name)
    db.add(new_brand)
    db.flush()
    cache[key] = new_brand.id
    return new_brand.id


def get_or_create_category(
    db: Session, name: str, cache: dict
) -> int | None:
    if not name or pd.isna(name):
        return None
    name = str(name).strip()
    key = name.lower()
    if key in cache:
        return cache[key]
    existing = db.query(Category).filter(func.lower(Category.name) == key).first()
    if existing:
        cache[key] = existing.id
        return existing.id
    new_category = Category(name=name)
    db.add(new_category)
    db.flush()
    cache[key] = new_category.id
    return new_category.id


def get_or_create_subcategory(
    db: Session, name: str, category_id: int | None, cache: dict
) -> int | None:
    if not name or pd.isna(name):
        return None
    name = str(name).strip()
    key = (name.lower(), category_id)
    if key in cache:
        return cache[key]
    query = db.query(Subcategory).filter(func.lower(Subcategory.name) == name.lower())
    if category_id is not None:
        query = query.filter(Subcategory.category_id == category_id)
    existing = query.first()
    if existing:
        cache[key] = existing.id
        return existing.id
    new_subcategory = Subcategory(name=name, category_id=category_id)
    db.add(new_subcategory)
    db.flush()
    cache[key] = new_subcategory.id
    return new_subcategory.id


# ── Search-text builder ─────────────────────────────────────────────────────────

def build_search_text(
    item_code: str | None,
    item_name: str | None,
    brand_name: str | None,
    category_name: str | None,
    subcategory_name: str | None,
) -> str:
    """Concatenate searchable name fields into the denormalized search_text blob."""
    parts = list(filter(None, [
        str(item_code) if item_code else "",
        str(brand_name) if brand_name else "",
        str(category_name) if category_name else "",
        str(subcategory_name) if subcategory_name else "",
        str(item_name) if item_name else "",
    ]))
    return " ".join(parts).lower()


# ── Main upsert entry-point ─────────────────────────────────────────────────────

def upsert_product_upload(
    db: Session,
    filename: str,
    content: bytes,
    dry_run: bool = False,
) -> dict:
    df = read_product_upload_file(filename, content)
    column_map = validate_product_upload_columns(df)

    created_count = 0
    updated_count = 0
    skipped_count = 0
    errors: list[dict] = []

    brand_cache: dict = {}
    category_cache: dict = {}
    subcategory_cache: dict = {}

    for index, row in df.iterrows():
        row_number = index + 2

        # ── Parse all values before touching the DB ──
        item_code = clean_value(row.get(column_map.get("item_code")))
        item_name = clean_value(row.get(column_map.get("item_name")))
        brand_name_raw = clean_value(row.get(column_map.get("brand_name")))
        category_name_raw = clean_value(row.get(column_map.get("category_name")))
        subcategory_name_raw = clean_value(row.get(column_map.get("subcategory_name")))
        sap_product_id = clean_value(row.get(column_map.get("sap_product_id")))
        barcode = normalize_barcode(row.get(column_map.get("barcode")))
        description = clean_value(row.get(column_map.get("description")))
        image_url = clean_value(row.get(column_map.get("image_url")))
        skin_type = clean_value(row.get(column_map.get("skin_type")))
        concerns_raw = row.get(column_map.get("concerns"))
        concerns = parse_concerns(concerns_raw if concerns_raw is not None else None)
        tag_cols = [
            column_map.get(c) for c in TAG_ALIASES
            if column_map.get(c) is not None
        ] + [column_map.get("tags")]
        tag_cols = [c for c in tag_cols if c is not None]
        tags = parse_tags(*tuple(row.get(c) for c in tag_cols))
        price = clean_number(row.get(column_map.get("price")))
        available_qty = clean_integer(row.get(column_map.get("available_qty")))
        is_best_selling = parse_best_selling(row.get(column_map.get("is_best_selling")))
        best_selling_scope = clean_value(row.get(column_map.get("best_selling_scope")))
        sales_rank = clean_integer(row.get(column_map.get("sales_rank")))

        if not barcode:
            skipped_count += 1
            errors.append({"row": row_number, "error": "barcode is required — row skipped."})
            continue

        # ── Each row runs inside its own savepoint so a failure here
        #    rolls back only this row, keeping the session healthy. ──
        savepoint = db.begin_nested()
        row_action = None  # "created" | "updated" — tracked for count rollback
        try:
            brand_id = get_or_create_brand(db, brand_name_raw, brand_cache)
            category_id = get_or_create_category(db, category_name_raw, category_cache)
            subcategory_id = None
            if subcategory_name_raw or category_id:
                subcategory_id = get_or_create_subcategory(
                    db, subcategory_name_raw, category_id, subcategory_cache
                )

            # Use the raw names directly — no extra DB lookup needed
            brand_str = brand_name_raw
            category_str = category_name_raw
            subcategory_str = subcategory_name_raw

            existing_product = (
                db.query(Product).filter(Product.barcode == barcode).first()
            )

            if existing_product:
                product = existing_product
                row_action = "updated"
                updated_count += 1
            else:
                product = Product(barcode=barcode)
                db.add(product)
                row_action = "created"
                created_count += 1

            product.item_code = item_code
            product.item_name = item_name
            product.brand_id = brand_id
            product.category_id = category_id
            product.subcategory_id = subcategory_id
            product.sap_product_id = sap_product_id
            product.description = description
            product.image_url = image_url
            product.skin_type = skin_type
            product.concerns = concerns if concerns else None
            product.tags = tags if tags else None
            product.price = price
            product.available_qty = available_qty
            product.is_best_selling = is_best_selling
            product.best_selling_scope = best_selling_scope
            product.sales_rank = sales_rank

            search_text = build_search_text(
                item_code=item_code,
                item_name=item_name,
                brand_name=brand_str,
                category_name=category_str,
                subcategory_name=subcategory_str,
            )

            existing_index = (
                db.query(ProductSearchIndex)
                .filter(ProductSearchIndex.product_id == barcode)
                .first()
            )
            if existing_index:
                search_index = existing_index
            else:
                search_index = ProductSearchIndex(product_id=barcode)
                db.add(search_index)

            search_index.item_code = item_code
            search_index.barcode = barcode
            search_index.item_name = item_name
            search_index.brand_name = brand_str
            search_index.category_name = category_str
            search_index.subcategory_name = subcategory_str
            search_index.search_text = search_text

            savepoint.commit()

        except Exception as e:
            savepoint.rollback()
            if row_action == "updated":
                updated_count -= 1
            elif row_action == "created":
                created_count -= 1
            skipped_count += 1
            errors.append({"row": row_number, "error": str(e)})

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "filename": filename,
        "total_rows": len(df),
        "created": created_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "dry_run": dry_run,
        "errors": errors[:50],
    }
