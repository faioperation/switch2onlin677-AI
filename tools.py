import json
import os
from typing import List, Dict, Optional

from sqlalchemy import text, or_

from database import SessionLocal
from models import Product, ProductSearchIndex, Brand, Category

BASE_URL = os.getenv("SAP_API_URL", "https://dbc-online.free.beeceptor.com")
ORDER_BASE_URL = os.getenv("ORDER_BASE_URL", "https://yoursite.com/order")

RATE_FILE = os.path.join(os.path.dirname(__file__), "rate.json")


def get_iqd_rate() -> float:
    if not os.path.exists(RATE_FILE):
        return 1310
    try:
        with open(RATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("iqd_rate", 1310))
    except Exception:
        return 1310


CURRENCY_SYMBOL = "IQD"


def convert_to_iqd(price_usd: float) -> str:
    if not price_usd or price_usd == 0:
        return "N/A"
    iqd_price = int(price_usd * get_iqd_rate())
    return f"{iqd_price:,} {CURRENCY_SYMBOL}"


def sort_products(products: List[Dict], sort_by: str = "item_name") -> List[Dict]:
    if sort_by == "price_asc":
        return sorted(products, key=lambda x: x.get("price", 0))
    elif sort_by == "price_desc":
        return sorted(products, key=lambda x: x.get("price", 0), reverse=True)
    else:
        return sorted(products, key=lambda x: x.get("item_name", "").lower())


def format_products(products: List[Dict], limit: int = 4) -> List[Dict]:
    """Shape raw product dicts into the structure expected by the frontend and AI."""
    if not isinstance(products, list):
        return []

    formatted = []
    for p in products[:limit]:
        if not isinstance(p, dict):
            continue

        raw_price = p.get("price", 0)
        barcode = p.get("barcode", "Unknown")
        img_url = str(p.get("image_url")).strip() if p.get("image_url") else None
        if img_url and img_url.lower() == "not found":
            img_url = None

        formatted.append({
            "id":          barcode,
            "name":        p.get("item_name", "Product"),
            "price":       convert_to_iqd(raw_price),
            "raw_price":   raw_price,
            "description": p.get("description", ""),
            "category":    p.get("category_name", "Beauty & Personal Care"),
            "brand":       p.get("brand_name", ""),
            "image_url":   img_url,
            "order_link":  f"{ORDER_BASE_URL}/{barcode}",
        })
    return formatted


def search_product_index(query: str, limit: int = 20) -> List[str]:
    """Raw index helper — returns list of product_ids (barcodes) matching query."""
    db = SessionLocal()
    try:
        q = query.strip().lower()
        rows = (
            db.query(ProductSearchIndex)
            .filter(
                or_(
                    ProductSearchIndex.product_id.ilike(f"%{q}%"),
                    ProductSearchIndex.item_code.ilike(f"%{q}%"),
                    ProductSearchIndex.barcode.ilike(f"%{q}%"),
                    ProductSearchIndex.item_name.ilike(f"%{q}%"),
                    ProductSearchIndex.brand_name.ilike(f"%{q}%"),
                    ProductSearchIndex.category_name.ilike(f"%{q}%"),
                    ProductSearchIndex.subcategory_name.ilike(f"%{q}%"),
                    ProductSearchIndex.search_text.ilike(f"%{q}%"),
                )
            )
            .limit(limit)
            .all()
        )
        return [str(row.product_id) for row in rows]
    finally:
        db.close()


def search_products(
    query: str,
    max_price: Optional[float] = None,
    min_price: Optional[float] = None,
    in_stock: Optional[bool] = None,
    category: Optional[str] = None,
    sort_by: str = "item_name",
    limit: int = 10,
    skip: int = 0,
) -> dict:
    """
    Hybrid weighted search: exact match > name match > brand match >
    full-text (ts_rank) > trigram similarity.

    JOINs productsearchindex to get denormalized brand_name / category_name
    without touching the brands / categories entity tables at query time.
    """
    db = SessionLocal()
    try:
        query_cleaned = query.strip()
        tokens = [t.strip() for t in query_cleaned.split() if len(t.strip()) > 1]
        token_query = " | ".join(tokens) if tokens else query_cleaned

        sql = text("""
            WITH search_results AS (
                SELECT
                    p.barcode,
                    p.item_code,
                    p.item_name,
                    p.price,
                    p.available_qty,
                    p.description,
                    p.image_url,
                    p.skin_type,
                    p.concerns,
                    psi.brand_name,
                    psi.category_name,
                    psi.subcategory_name,
                    (
                        CASE
                            WHEN p.barcode = :query OR p.item_code = :query THEN 15.0
                            WHEN LOWER(p.item_name) = LOWER(:query)         THEN 12.0
                            WHEN p.item_name ILIKE :query_exact              THEN 8.0
                            WHEN psi.brand_name ILIKE :query_exact           THEN 7.0
                            WHEN p.item_name ILIKE :query_like               THEN 3.0
                            ELSE 0.0
                        END
                        + ts_rank(
                            to_tsvector('english',
                                p.item_name || ' ' ||
                                COALESCE(psi.brand_name, '') || ' ' ||
                                COALESCE(psi.category_name, '')
                            ),
                            to_tsquery('english', :token_query)
                          ) * 4.0
                        + similarity(p.item_name, :query) * 2.0
                    ) AS score
                FROM products p
                LEFT JOIN productsearchindex psi ON psi.product_id = p.barcode
                WHERE
                    (
                        to_tsvector('english',
                            p.item_name || ' ' ||
                            COALESCE(psi.brand_name, '') || ' ' ||
                            COALESCE(psi.category_name, '')
                        ) @@ to_tsquery('english', :token_query)
                        OR p.item_name    ILIKE :query_like
                        OR p.barcode      ILIKE :query_like
                        OR psi.brand_name ILIKE :query_like
                        OR p.item_name    %     :query
                    )
                    AND (:min_price IS NULL OR p.price >= :min_price)
                    AND (:max_price IS NULL OR p.price <= :max_price)
                    AND (:in_stock  IS FALSE OR p.available_qty > 0)
                    AND (
                        :category IS NULL
                        OR LOWER(psi.category_name) = LOWER(:category)
                        OR p.item_name ILIKE :category_like
                    )
            )
            SELECT * FROM search_results
            WHERE score > 0.05
            ORDER BY
                CASE WHEN :sort_by = 'price_asc'  THEN price END ASC,
                CASE WHEN :sort_by = 'price_desc' THEN price END DESC,
                score DESC,
                item_name ASC
            LIMIT :limit OFFSET :skip
        """)

        result = db.execute(sql, {
            "query":         query_cleaned,
            "token_query":   token_query,
            "query_exact":   query_cleaned,
            "query_like":    f"%{query_cleaned}%",
            "category":      category,
            "category_like": f"%{category}%" if category else None,
            "min_price":     min_price,
            "max_price":     max_price,
            "in_stock":      True if in_stock else False,
            "limit":         limit,
            "skip":          skip,
            "sort_by":       sort_by,
        })

        products = []
        for row in result:
            products.append({
                "barcode":          row.barcode,
                "item_code":        row.item_code,
                "item_name":        row.item_name,
                "price":            float(row.price) if row.price else 0.0,
                "available_qty":    row.available_qty,
                "brand_name":       row.brand_name,
                "category_name":    row.category_name,
                "subcategory_name": row.subcategory_name,
                "description":      row.description,
                "image_url":        row.image_url,
                "score":            row.score,
            })

        if not products:
            return {
                "found":   False,
                "message": f"No items matching '{query_cleaned}' were found in the catalog.",
            }

        sorted_products = sort_products(products, sort_by)
        formatted = format_products(sorted_products, limit)

        return {
            "found":       True,
            "total_found": len(products),
            "products":    formatted,
        }

    finally:
        db.close()


def get_product_details(product_id: str) -> dict:
    """Full product detail lookup by barcode. Resolves brand and category names
    via the entity tables (Brand, Category)."""
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.barcode == product_id).first()
        if not product:
            return {"found": False, "message": "Product not found."}

        brand_name = None
        category_name = None

        if product.brand_id is not None:
            brand = db.query(Brand).filter(Brand.id == product.brand_id).first()
            brand_name = brand.name if brand else None

        if product.category_id is not None:
            category = db.query(Category).filter(Category.id == product.category_id).first()
            category_name = category.name if category else None

        price_val = product.price
        raw_price = float(price_val) if price_val is not None else 0.0  # type: ignore[arg-type]

        return {
            "found":       True,
            "id":          product.barcode,
            "item_code":   product.item_code,
            "item_name":   product.item_name,
            "brand":       brand_name,
            "price":       convert_to_iqd(raw_price),
            "raw_price":   raw_price,
            "description": product.description or f"Product: {product.item_name}",
            "category":    category_name or "Beauty & Personal Care",
            "image_url":   product.image_url,
            "skin_type":   product.skin_type,
            "concerns":    product.concerns,
            "order_link":  f"{ORDER_BASE_URL}/{product.barcode}",
        }
    except Exception as e:
        return {"found": False, "message": f"Details error: {str(e)}"}
    finally:
        db.close()


def check_availability(query: str) -> dict:
    """Check if a brand, product type, or concern exists. Returns summary only."""
    result = search_products(query, limit=5)
    if result.get("found"):
        return {
            "found":        True,
            "count":        result.get("total_found", 0),
            "summary":      f"Matching products found for '{query}'.",
            "search_query": query,
        }
    return {
        "found":   False,
        "message": f"No products matching '{query}' were found in the catalog.",
    }
