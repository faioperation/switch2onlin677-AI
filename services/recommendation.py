"""
services/recommendation.py
===========================
Curated product recommendation queries built on the new product fields.

All functions enforce two hard filters:
  1. product_status = 'active'   — inactive / draft products never appear
  2. available_qty  > 5          — low-stock products are excluded

Output is shaped by tools.format_products() so the AI pipeline and
frontend receive the exact same structure as search_products().

Public API
----------
get_best_selling(db, category_id, price_tier, limit)
get_new_arrivals(db, category_id, price_tier, limit)
get_recommended(db, category_id, price_tier, brand_family, limit)
get_cod_recommended(db, category_id, limit)
get_by_price_tier(db, price_tier, category_id, limit)
get_by_brand_family(db, brand_family, category_id, limit)
get_bundle(db, bundle_group)
get_featured(db, limit)          ← best-selling + new arrivals combined
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from models import Product, ProductSearchIndex, ProductStatus
from tools import convert_to_iqd, format_products, is_valid_raw_price, ORDER_BASE_URL

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

LOW_STOCK_THRESHOLD = 5   # available_qty must be > this value


# ── Base filter helper ────────────────────────────────────────────────────────

def _active_in_stock(query, db: Session):
    """
    Apply the two mandatory filters to any Product query:
      - product_status = active
      - available_qty  > LOW_STOCK_THRESHOLD
    """
    return query.filter(
        Product.product_status == ProductStatus.active.value,
        Product.available_qty  >  LOW_STOCK_THRESHOLD,
        Product.price.isnot(None),
        Product.price          >  0,
    )


# ── Row → formatted dict ──────────────────────────────────────────────────────

def _to_dict(product: Product, psi_map: dict[str, object]) -> dict:
    """
    Convert a Product ORM object to the dict shape expected by format_products().
    psi_map: barcode → ProductSearchIndex row (pre-fetched for performance).
    """
    psi = psi_map.get(product.barcode)
    raw_price = float(product.price) if product.price else 0.0

    return {
        "barcode":          product.barcode,
        "item_code":        product.item_code,
        "item_name":        product.item_name or "",
        "price":            raw_price,
        "available_qty":    product.available_qty or 0,
        "description":      product.description or "",
        "image_url":        product.image_url,
        "skin_type":        product.skin_type,
        "concerns":         product.concerns,
        "price_tier":       product.price_tier,
        "brand_family":     product.brand_family,
        "is_best_selling":  product.is_best_selling,
        "is_new_arrival":   product.is_new_arrival,
        "recommendation_priority": product.recommendation_priority,
        "bundle_group":     product.bundle_group,
        "bundle_discount_percent": (
            float(product.bundle_discount_percent)
            if product.bundle_discount_percent else None
        ),
        # From denormalised index
        "brand_name":       getattr(psi, "brand_name", None) if psi else None,
        "category_name":    getattr(psi, "category_name", None) if psi else None,
        "subcategory_name": getattr(psi, "subcategory_name", None) if psi else None,
    }


def _build_psi_map(db: Session, barcodes: list[str]) -> dict[str, object]:
    """Fetch ProductSearchIndex rows for a list of barcodes in one query."""
    if not barcodes:
        return {}
    rows = (
        db.query(ProductSearchIndex)
        .filter(ProductSearchIndex.product_id.in_(barcodes))
        .all()
    )
    return {r.product_id: r for r in rows}


def _shape_response(
    products: list[Product],
    db: Session,
    limit: int,
    label: str,
) -> dict:
    """Convert ORM rows to the standard API response envelope."""
    if not products:
        return {
            "found":    False,
            "message":  f"No {label} products available right now.",
            "products": [],
        }

    barcodes  = [p.barcode for p in products]
    psi_map   = _build_psi_map(db, barcodes)
    raw_dicts = [_to_dict(p, psi_map) for p in products]
    formatted = format_products(raw_dicts, limit=limit)

    if not formatted:
        return {
            "found":    False,
            "message":  f"No {label} products with valid prices right now.",
            "products": [],
        }

    return {
        "found":       True,
        "total_found": len(formatted),
        "returned":    len(formatted),
        "label":       label,
        "products":    formatted,
    }


# ── Optional filter helpers ───────────────────────────────────────────────────

def _filter_category(query, category_id: Optional[int]):
    return query.filter(Product.category_id == category_id) if category_id else query


def _filter_price_tier(query, price_tier: Optional[str]):
    return query.filter(Product.price_tier == price_tier) if price_tier else query


def _filter_brand_family(query, brand_family: Optional[str]):
    return (
        query.filter(Product.brand_family == brand_family)
        if brand_family else query
    )


# ── Public recommendation functions ──────────────────────────────────────────

def get_best_selling(
    db: Session,
    category_id:  Optional[int] = None,
    price_tier:   Optional[str] = None,
    limit:        int = 10,
) -> dict:
    """
    Products flagged as best-selling, ordered by recommendation_priority.
    """
    q = db.query(Product).filter(Product.is_best_selling == True)
    q = _active_in_stock(q, db)
    q = _filter_category(q, category_id)
    q = _filter_price_tier(q, price_tier)
    q = q.order_by(
        Product.recommendation_priority.asc().nullslast(),
        Product.recommendation_score_override.desc().nullslast(),
        Product.sales_rank.asc().nullslast(),
    ).limit(limit)

    return _shape_response(q.all(), db, limit, "best-selling")


def get_new_arrivals(
    db: Session,
    category_id: Optional[int] = None,
    price_tier:  Optional[str] = None,
    limit:       int = 10,
) -> dict:
    """
    Products flagged as new arrivals, ordered by creation date (newest first).
    """
    q = db.query(Product).filter(Product.is_new_arrival == True)
    q = _active_in_stock(q, db)
    q = _filter_category(q, category_id)
    q = _filter_price_tier(q, price_tier)
    q = q.order_by(
        Product.created_at.desc().nullslast(),
        Product.recommendation_priority.asc().nullslast(),
    ).limit(limit)

    return _shape_response(q.all(), db, limit, "new arrivals")


def get_recommended(
    db: Session,
    category_id:  Optional[int] = None,
    price_tier:   Optional[str] = None,
    brand_family: Optional[str] = None,
    limit:        int = 10,
) -> dict:
    """
    Curated recommended products ordered by:
      1. recommendation_priority  (lower = higher rank)
      2. recommendation_score_override (higher = better)
    """
    q = db.query(Product).filter(Product.is_recommended == True)
    q = _active_in_stock(q, db)
    q = _filter_category(q, category_id)
    q = _filter_price_tier(q, price_tier)
    q = _filter_brand_family(q, brand_family)
    q = q.order_by(
        Product.recommendation_priority.asc().nullslast(),
        Product.recommendation_score_override.desc().nullslast(),
    ).limit(limit)

    return _shape_response(q.all(), db, limit, "recommended")


def get_cod_recommended(
    db: Session,
    category_id: Optional[int] = None,
    limit:       int = 10,
) -> dict:
    """
    Products suitable for Cash-on-Delivery recommendations.
    """
    q = db.query(Product).filter(Product.is_cod_recommended == True)
    q = _active_in_stock(q, db)
    q = _filter_category(q, category_id)
    q = q.order_by(
        Product.recommendation_priority.asc().nullslast(),
        Product.recommendation_score_override.desc().nullslast(),
    ).limit(limit)

    return _shape_response(q.all(), db, limit, "COD recommended")


def get_by_price_tier(
    db: Session,
    price_tier:  str,
    category_id: Optional[int] = None,
    limit:       int = 10,
) -> dict:
    """
    All active in-stock products in a given price tier.
    Valid tiers: Budget | Mid | Premium | Luxury
    """
    q = db.query(Product).filter(Product.price_tier == price_tier)
    q = _active_in_stock(q, db)
    q = _filter_category(q, category_id)
    q = q.order_by(
        Product.recommendation_priority.asc().nullslast(),
        Product.price.asc(),
    ).limit(limit)

    return _shape_response(q.all(), db, limit, f"{price_tier} tier")


def get_by_brand_family(
    db: Session,
    brand_family: str,
    category_id:  Optional[int] = None,
    limit:        int = 10,
) -> dict:
    """
    All active in-stock products from a given brand family.
    Example brand families: 'Italian Niche', 'French Designer', 'Local'
    """
    q = db.query(Product).filter(Product.brand_family == brand_family)
    q = _active_in_stock(q, db)
    q = _filter_category(q, category_id)
    q = q.order_by(
        Product.recommendation_priority.asc().nullslast(),
        Product.recommendation_score_override.desc().nullslast(),
    ).limit(limit)

    return _shape_response(q.all(), db, limit, f"{brand_family} brand family")


def get_bundle(db: Session, bundle_group: str) -> dict:
    """
    All products in a bundle group with discount information.
    Returns bundle metadata alongside the product list.
    """
    q = db.query(Product).filter(
        Product.bundle_group == bundle_group,
        Product.product_status == ProductStatus.active.value,
        Product.available_qty  >  LOW_STOCK_THRESHOLD,
    ).order_by(Product.price.asc())

    products = q.all()

    if not products:
        return {
            "found":        False,
            "bundle_group": bundle_group,
            "message":      f"Bundle '{bundle_group}' not found or unavailable.",
            "products":     [],
        }

    barcodes  = [p.barcode for p in products]
    psi_map   = _build_psi_map(db, barcodes)
    raw_dicts = [_to_dict(p, psi_map) for p in products]

    # Bundle-level discount (use first product's value — shared across group)
    discount = (
        float(products[0].bundle_discount_percent)
        if products[0].bundle_discount_percent else None
    )

    # Calculate bundle total and discounted total
    valid_prices = [
        float(p.price) for p in products
        if p.price and is_valid_raw_price(p.price)
    ]
    original_total   = sum(valid_prices) if valid_prices else None
    discounted_total = (
        round(original_total * (1 - discount / 100), 2)
        if original_total and discount else None
    )

    return {
        "found":            True,
        "bundle_group":     bundle_group,
        "discount_percent": discount,
        "original_total":   convert_to_iqd(original_total) if original_total else None,
        "discounted_total": convert_to_iqd(discounted_total) if discounted_total else None,
        "item_count":       len(products),
        "products":         raw_dicts,
    }


def get_featured(db: Session, limit: int = 20) -> dict:
    """
    Combined featured section: best-selling + new arrivals (deduplicated).
    Used for homepage / chatbot cold-start recommendations.
    """
    half = limit // 2

    best_result = get_best_selling(db, limit=half)
    new_result  = get_new_arrivals(db, limit=half)

    best_products = best_result.get("products", [])
    new_products  = new_result.get("products", [])

    # Deduplicate by barcode — preserve best-selling order first
    seen:   set[str] = set()
    merged: list     = []

    for p in best_products + new_products:
        pid = p.get("id") or p.get("barcode")
        if pid and pid not in seen:
            seen.add(pid)
            merged.append(p)

    if not merged:
        return {
            "found":    False,
            "message":  "No featured products available right now.",
            "products": [],
        }

    return {
        "found":       True,
        "total_found": len(merged),
        "returned":    len(merged[:limit]),
        "label":       "featured",
        "products":    merged[:limit],
    }
