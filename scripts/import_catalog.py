import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
del _os, _sys

"""
import_catalog.py
=================
Bulk-imports a product catalog from Excel into `products` using
SQLAlchemy Core INSERT … ON CONFLICT UPDATE for maximum throughput
while resolving brand/category/subcategory via get-or-create lookups.
"""
import pandas as pd
import os
import argparse
import math
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func
from database import SessionLocal, engine
from models import (
    Brand,
    Category,
    Subcategory,
    Product,
    Base,
    ProductSearchIndex,
)
from dotenv import load_dotenv

load_dotenv()


# ── Cleanup helpers ────────────────────────────────────────────────────────────

def clean_value(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def clean_tags(row):
    """Combine Tag_EN, Tag_MSA, and Tag_IRQ into a single deduplicated list."""
    tags = []
    for col in ["Tag_EN", "Tag_MSA", "Tag_IRQ"]:
        val = row.get(col)
        if pd.notna(val) and str(val).strip() != "":
            parts = [p.strip() for p in str(val).split(",") if p.strip()]
            tags.extend(parts)
    # Deduplicate preserving order
    seen = set()
    result = []
    for tag in tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result


# ── Smart get-or-create helpers ─────────────────────────────────────────────────

def get_or_create_brand(db, name: str):
    """Returns (brand_id, brand_name_string)."""
    if not name or pd.isna(name) or str(name).strip() == "":
        return None, None
    name = str(name).strip()
    existing = db.query(Brand).filter(
        func.lower(Brand.name) == name.lower()
    ).first()
    if existing:
        return existing.id, existing.name
    new_brand = Brand(name=name)
    db.add(new_brand)
    db.flush()
    return new_brand.id, new_brand.name


def get_or_create_category(db, name: str):
    """Returns (category_id, category_name_string)."""
    if not name or pd.isna(name) or str(name).strip() == "":
        return None, None
    name = str(name).strip()
    existing = db.query(Category).filter(
        func.lower(Category.name) == name.lower()
    ).first()
    if existing:
        return existing.id, existing.name
    new_category = Category(name=name)
    db.add(new_category)
    db.flush()
    return new_category.id, new_category.name


def get_or_create_subcategory(db, name: str, category_id):
    """Returns (subcategory_id, subcategory_name_string)."""
    if (not name and name != 0) or (pd.isna(name) if name is not None else True):
        return None, None
    try:
        name_str = str(name).strip()
    except Exception:
        return None, None
    if name_str == "":
        return None, None
    query = db.query(Subcategory).filter(
        func.lower(Subcategory.name) == name_str.lower()
    )
    if category_id is not None:
        query = query.filter(Subcategory.category_id == category_id)
    existing = query.first()
    if existing:
        return existing.id, existing.name
    new_sub = Subcategory(name=name_str, category_id=category_id)
    db.add(new_sub)
    db.flush()
    return new_sub.id, new_sub.name


# ── Main import ────────────────────────────────────────────────────────────────

def import_catalog(file_path):
    if not os.path.exists(file_path):
        print(f"Error: File not found at {file_path}")
        return

    print(f"Loading catalog from: {file_path}")
    try:
        df = pd.read_excel(file_path)
    except Exception as e:
        print(f"Error reading Excel file: {e}")
        return

    # ── Detect columns ──
    price_col = next((c for c in df.columns if "price" in c.lower()), None)
    if price_col:
        print(f"Detected Price column: '{price_col}'")
    else:
        print("No Price column detected in Excel.")

    brand_col = next((c for c in df.columns if "brand" in c.lower()), None)
    cat_col = next((c for c in df.columns if "category" in c.lower()), None)
    subcat_col = next(
        (c for c in df.columns if "subcat" in c.lower()),
        None,
    )
    sap_id_col = next(
        (c for c in df.columns if "sap" in c.lower()),
        None,
    )
    desc_col = next(
        (c for c in df.columns if "desc" in c.lower() and "tag" not in c.lower()),
        None,
    )
    image_col = next(
        (c for c in df.columns if "image" in c.lower() and "url" in c.lower()),
        None,
    )

    print(f"  brand col      : {brand_col}")
    print(f"  category col   : {cat_col}")
    print(f"  subcategory col: {subcat_col}")
    print(f"  sap_id col     : {sap_id_col}")

    print(f"Total records found in file: {len(df)}")

    db = SessionLocal()
    success_count = 0
    error_count = 0

    # Create productsearchindex table if it does not yet exist
    Base.metadata.create_all(bind=engine, tables=[ProductSearchIndex.__table__])

    print("Starting Upsert process...")

    for index, row in df.iterrows():
        try:
            # ── Barcode normalisation ──
            barcode_raw = row.get("Bar Code")
            if pd.isna(barcode_raw) or str(barcode_raw).strip() == "" or str(barcode_raw).strip().lower() == "nan":
                error_count += 1
                continue

            barcode = str(barcode_raw).strip().lstrip("0")
            if barcode == "":
                barcode = "0"

            item_code_raw = row.get("Item No.")
            item_code = str(item_code_raw).strip() if pd.notna(item_code_raw) else barcode

            item_name_raw = row.get("Item Description")
            item_name = str(item_name_raw).strip() if pd.notna(item_name_raw) else "Unknown Product"

            # ── Resolution of relation lookups ──
            brand_name_val = str(row.get(brand_col)).strip() if brand_col and pd.notna(row.get(brand_col)) else ""
            cat_name_val   = str(row.get(cat_col)).strip()   if cat_col   and pd.notna(row.get(cat_col))   else ""
            subcat_name_val = str(row.get(subcat_col)).strip() if subcat_col and pd.notna(row.get(subcat_col)) else ""

            brand_id, brand_str     = get_or_create_brand(db,    brand_name_val)
            cat_id,   cat_str       = get_or_create_category(db,  cat_name_val)
            subcat_id, subcat_str   = get_or_create_subcategory(db, subcat_name_val, cat_id)

            # ── Price ──
            raw_price = row.get(price_col) if price_col else 0.0
            try:
                price_val = float(raw_price) if pd.notna(raw_price) else 0.0
            except Exception:
                price_val = 0.0

            # ── Other fields ──
            description = str(row.get(desc_col)).strip() if desc_col and pd.notna(row.get(desc_col)) else item_name
            image_url   = str(row.get(image_col)).strip() if image_col and pd.notna(row.get(image_col)) else None
            sap_product_id = clean_value(
                str(row.get(sap_id_col)).strip() if sap_id_col and pd.notna(row.get(sap_id_col)) else None
            )

            tags = clean_tags(row)
            concerns = []
            skin_type = None

            # ── Build search text for productsearchindex ──
            search_text = " ".join(filter(None, [item_code, item_name, brand_str, cat_str, subcat_str])).lower()

            # ── Bulk upsert via SQLAlchemy Core on_conflict ──
            upsert_data = {
                "barcode":           barcode,
                "item_code":         item_code,
                "item_name":         item_name,
                "brand_id":          brand_id,
                "category_id":       cat_id,
                "subcategory_id":    subcat_id,
                "sap_product_id":    sap_product_id,
                "description":       description,
                "image_url":         image_url,
                "skin_type":         skin_type,
                "concerns":          concerns,
                "tags":              tags,
                "price":             price_val,
                "available_qty":     0,
                "is_best_selling":   0,
                "best_selling_scope": None,
                "sales_rank":        None,
            }

            stmt = insert(Product).values(**upsert_data)
            update_cols = {c: upsert_data[c] for c in upsert_data if c != "barcode"}
            stmt = stmt.on_conflict_do_update(
                index_elements=["barcode"],
                set_=update_cols,
            )
            db.execute(stmt)

            # ── Upsert productsearchindex (name strings, not IDs) ──
            idx_upsert = {
                "product_id":       barcode,
                "item_code":        item_code,
                "barcode":          barcode,
                "item_name":        item_name,
                "brand_name":       brand_str,
                "category_name":    cat_str,
                "subcategory_name": subcat_str,
                "search_text":      search_text,
            }
            idx_stmt = insert(ProductSearchIndex).values(**idx_upsert)
            idx_update_keys = {
                c: idx_upsert[c] for c in idx_upsert if c != "product_id"
            }
            idx_stmt = idx_stmt.on_conflict_do_update(
                index_elements=["product_id"],
                set_=idx_update_keys,
            )
            db.execute(idx_stmt)

            success_count += 1

            if success_count % 1000 == 0:
                print(f"Processed {success_count} items...")

        except Exception as e:
            print(f"Error at row {index + 2}: {str(e)}")
            error_count += 1

    db.commit()
    db.close()
    print(f"\nImport Complete! Success: {success_count}, Failed/Skipped: {error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Master Catalog from Excel")
    parser.add_argument("--file", required=True, help="Path to the Excel file")
    args = parser.parse_args()
    Base.metadata.create_all(bind=engine)
    import_catalog(args.file)
