"""
ai/tools/product_search.py
==========================
Product search and detail lookup tools for the GPT tool-call pipeline.
Implements hybrid weighted search (exact → name → brand → FTS → trigram).

Session injection
-----------------
`search_products` and `get_product_details` accept an optional `db` parameter.
When `db` is provided (e.g. from the tool dispatcher), no extra session is
opened and the caller owns the lifecycle.  When `db` is None (backward-compat
callers, tests, standalone scripts) each function opens and closes its own
session via SessionLocal().
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy import text, or_
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Brand, Category, Product, ProductSearchIndex
from ai.tools.formatters import (
    ORDER_BASE_URL,
    convert_to_iqd,
    format_products,
    is_valid_raw_price,
    sort_products,
)

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("SAP_API_URL", "https://dbc-online.free.beeceptor.com")


# ── Internal helpers ───────────────────────────────────────────────────────────

def search_product_index(query: str, limit: int = 20) -> list[str]:
    """Return a list of product barcodes matching *query* via the search index."""
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


# ── Public tool functions ──────────────────────────────────────────────────────

def search_products(
    query:     str,
    max_price: Optional[float] = None,
    min_price: Optional[float] = None,
    in_stock:  Optional[bool]  = None,
    category:  Optional[str]   = None,
    sort_by:   str = "item_name",
    limit:     int = 8,
    skip:      int = 0,
    db:        Optional[Session] = None,
) -> dict:
    """
    Hybrid weighted search: exact match > name match > brand match >
    full-text (ts_rank) > trigram similarity.

    Enforces hard filters: product_status=active, available_qty>5, deleted_at IS NULL.

    Session injection: pass `db` to reuse the caller's session and avoid opening
    a second connection.  When `db` is None a fresh SessionLocal() is used.
    """
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        query_cleaned = query.strip()
        tokens        = [t.strip() for t in query_cleaned.split() if len(t.strip()) > 1]
        token_query   = " | ".join(tokens) if tokens else query_cleaned

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
                    AND p.product_status = 'active'
                    AND p.available_qty  > 5
                    AND p.deleted_at     IS NULL
                    AND p.price IS NOT NULL
                    AND p.price > 0
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

        products = [
            {
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
            }
            for row in result
        ]

        if not products:
            return {
                "found":   False,
                "message": f"No items matching '{query_cleaned}' were found in the catalog.",
            }

        sorted_products = sort_products(products, sort_by)
        formatted       = format_products(sorted_products, limit)

        if not formatted:
            return {
                "found":   False,
                "message": f"Matching items for '{query_cleaned}' exist, but none have valid prices right now.",
            }

        return {
            "found":       True,
            "total_found": len(formatted),
            "returned":    len(formatted),
            "products":    formatted,
        }

    except Exception as exc:
        logger.error("search_products error query=%r: %s", query, exc, exc_info=True)
        return {"found": False, "message": "Search temporarily unavailable."}
    finally:
        if _own_db:
            db.close()


def get_product_details(
    product_id: str,
    db: Optional[Session] = None,
) -> dict:
    """
    Full product detail lookup by barcode. Resolves brand/category names.

    Session injection: pass `db` to reuse the caller's session.
    When `db` is None a fresh SessionLocal() is used.
    """
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.barcode == product_id).first()
        if not product:
            return {"found": False, "message": "Product not found."}

        brand_name = category_name = None
        if product.brand_id is not None:
            brand = db.query(Brand).filter(Brand.id == product.brand_id).first()
            brand_name = brand.name if brand else None
        if product.category_id is not None:
            cat = db.query(Category).filter(Category.id == product.category_id).first()
            category_name = cat.name if cat else None

        raw_price = float(product.price) if product.price is not None else 0.0

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
    except Exception as exc:
        logger.error("get_product_details error id=%r: %s", product_id, exc, exc_info=True)
        return {"found": False, "message": f"Details error: {exc}"}
    finally:
        if _own_db:
            db.close()
