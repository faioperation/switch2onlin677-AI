import io
import math
import pandas as pd
from sqlalchemy.orm import Session

from models import Product, ProductSearchIndex


REQUIRED_PRODUCT_UPLOAD_COLUMNS = [
    "item_code",
    "item_name",
    "brand_name",
    "category_name",
]

OPTIONAL_PRODUCT_UPLOAD_COLUMNS = [
    "barcode",
    "subcategory_name",
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
    "sap_product_id",
]

ALL_PRODUCT_UPLOAD_COLUMNS = (
    REQUIRED_PRODUCT_UPLOAD_COLUMNS + OPTIONAL_PRODUCT_UPLOAD_COLUMNS
)


def clean_value(value):
    if value is None:
        return None

    if isinstance(value, float) and math.isnan(value):
        return None

    text = str(value).strip()

    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None

    return text


def clean_number(value, default=0):
    value = clean_value(value)

    if value is None:
        return default

    try:
        return float(value)
    except Exception:
        return default


def clean_integer(value, default=0):
    value = clean_value(value)

    if value is None:
        return default

    try:
        return int(float(value))
    except Exception:
        return default


def split_csv_value(value):
    value = clean_value(value)

    if not value:
        return []

    return [
        part.strip()
        for part in value.split(",")
        if part.strip()
    ]


def normalize_barcode(value):
    value = clean_value(value)

    if not value:
        return None

    # Excel sometimes turns barcode into 12345.0
    if value.endswith(".0"):
        value = value[:-2]

    return value.strip()


def read_product_upload_file(filename: str, content: bytes) -> pd.DataFrame:
    lower_name = filename.lower()

    if lower_name.endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(content), sheet_name=0)

    if lower_name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content))

    raise ValueError("Only .xlsx and .csv files are supported.")


def validate_product_upload_columns(df: pd.DataFrame):
    normalized_columns = {
        str(column).strip(): column
        for column in df.columns
    }

    missing = [
        column
        for column in REQUIRED_PRODUCT_UPLOAD_COLUMNS
        if column not in normalized_columns
    ]

    if missing:
        raise ValueError(
            "Missing required columns: " + ", ".join(missing)
        )

    return normalized_columns


def build_search_text(
    item_code,
    barcode,
    item_name,
    brand,
    category,
    subcategory,
    description,
    concerns,
    tags,
):
    parts = [
        item_code,
        barcode,
        item_name,
        brand,
        category,
        subcategory,
        description,
        " ".join(concerns or []),
        " ".join(tags or []),
    ]

    return " ".join([
        str(part)
        for part in parts
        if part
    ]).lower()


def upsert_product_upload(
    db: Session,
    filename: str,
    content: bytes,
    dry_run: bool = False,
):
    df = read_product_upload_file(filename, content)
    column_map = validate_product_upload_columns(df)

    created_count = 0
    updated_count = 0
    skipped_count = 0
    errors = []

    for index, row in df.iterrows():
        row_number = index + 2

        try:
            item_code = clean_value(row.get(column_map.get("item_code")))
            item_name = clean_value(row.get(column_map.get("item_name")))
            brand = clean_value(row.get(column_map.get("brand_name")))
            category = clean_value(row.get(column_map.get("category_name")))

            barcode = normalize_barcode(row.get(column_map.get("barcode")))
            subcategory = clean_value(row.get(column_map.get("subcategory_name")))
            description = clean_value(row.get(column_map.get("description")))
            image_url = clean_value(row.get(column_map.get("image_url")))
            skin_type = clean_value(row.get(column_map.get("skin_type")))

            concerns = split_csv_value(row.get(column_map.get("concerns")))
            tags = split_csv_value(row.get(column_map.get("tags")))

            price = clean_number(row.get(column_map.get("price")), default=0)
            available_qty = clean_integer(
                row.get(column_map.get("available_qty")),
                default=0
            )

            if not item_code:
                skipped_count += 1
                errors.append({
                    "row": row_number,
                    "error": "item_code is required."
                })
                continue

            if not item_name:
                skipped_count += 1
                errors.append({
                    "row": row_number,
                    "error": "item_name is required."
                })
                continue

            # Your current Product table uses barcode as the primary key.
            # If barcode is empty, use item_code as fallback.
            product_barcode = barcode or item_code

            existing_product = (
                db.query(Product)
                .filter(Product.barcode == product_barcode)
                .first()
            )

            if existing_product:
                product = existing_product
                updated_count += 1
            else:
                product = Product(barcode=product_barcode)
                db.add(product)
                created_count += 1

            product.item_code = item_code
            product.item_name = item_name
            product.brand = brand
            product.category = category
            product.description = description or item_name
            product.image_url = image_url
            product.skin_type = skin_type
            product.concerns = concerns
            product.tags = tags
            product.price = price
            product.available_qty = available_qty

            search_text = build_search_text(
                item_code=item_code,
                barcode=product_barcode,
                item_name=item_name,
                brand=brand,
                category=category,
                subcategory=subcategory,
                description=description,
                concerns=concerns,
                tags=tags,
            )

            existing_index = (
                db.query(ProductSearchIndex)
                .filter(ProductSearchIndex.product_id == product_barcode)
                .first()
            )

            if existing_index:
                search_index = existing_index
            else:
                search_index = ProductSearchIndex(product_id=product_barcode)
                db.add(search_index)

            search_index.item_code = item_code
            search_index.barcode = product_barcode
            search_index.item_name = item_name
            search_index.brand = brand
            search_index.category = category
            search_index.subcategory = subcategory
            search_index.search_text = search_text

        except Exception as e:
            skipped_count += 1
            errors.append({
                "row": row_number,
                "error": str(e)
            })

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