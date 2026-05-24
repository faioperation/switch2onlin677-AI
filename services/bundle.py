"""
services/bundle.py
==================
Bundle service — reads from the new bundles + bundle_items tables.

Public API
----------
list_bundles(db, active_only=True)
    Summary list of bundles: code, name, discount, item count, image.
    Used for GET /bundles.

get_bundle_detail(db, bundle_code)
    Full bundle: metadata + all products with pricing (IQD-converted).
    Used for GET /bundles/{bundle_code}.

get_bundles_for_product(db, barcode)
    All active bundles that contain a given product barcode.
    Useful for product detail pages ("Also available as part of…").

Architecture notes
------------------
- No FK constraints (loose coupling — consistent with rest of codebase).
- All queries filter is_active=True by default; admin paths can override.
- Products are joined via a single IN-query on barcodes (no ORM relationship
  needed — avoids N+1 and keeps the service self-contained).
- Pricing uses convert_to_iqd() + is_valid_raw_price() from tools.py.
- The old products.bundle_group column is NOT read here; this service
  works entirely with the new bundles / bundle_items tables.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models import Bundle, BundleItem, Product, ProductSearchIndex
from tools import convert_to_iqd, is_valid_raw_price

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_scheduled_active(bundle: Bundle) -> bool:
    """Return False if the bundle is outside its valid_from / valid_until window."""
    now = _now_utc()
    if bundle.valid_from and now < bundle.valid_from:
        return False
    if bundle.valid_until and now > bundle.valid_until:
        return False
    return True


def _product_map(db: Session, barcodes: list[str]) -> dict[str, Product]:
    """Fetch Product rows for a list of barcodes in one query."""
    if not barcodes:
        return {}
    rows = (
        db.query(Product)
        .filter(Product.barcode.in_(barcodes))
        .all()
    )
    return {r.barcode: r for r in rows}


def _psi_map(db: Session, barcodes: list[str]) -> dict[str, ProductSearchIndex]:
    """Fetch ProductSearchIndex rows for barcodes in one query."""
    if not barcodes:
        return {}
    rows = (
        db.query(ProductSearchIndex)
        .filter(ProductSearchIndex.product_id.in_(barcodes))
        .all()
    )
    return {r.product_id: r for r in rows}


def _format_product(
    item:    BundleItem,
    product: Product,
    psi:     Optional[ProductSearchIndex],
) -> dict:
    """Serialize a bundle item + its product into the API dict shape."""
    raw_price = float(product.price) if product.price else None
    iqd_price = convert_to_iqd(raw_price) if raw_price and is_valid_raw_price(raw_price) else None

    return {
        "barcode":       product.barcode,
        "item_code":     product.item_code,
        "item_name":     product.item_name or "",
        "description":   product.description or "",
        "image_url":     product.image_url,
        "price_usd":     raw_price,
        "price":         iqd_price,          # IQD display price
        "available_qty": product.available_qty or 0,
        "skin_type":     product.skin_type,
        "price_tier":    str(product.price_tier) if product.price_tier else None,
        "brand_family":  product.brand_family,
        # Denormalized names from search index
        "brand_name":    psi.brand_name    if psi else None,
        "category_name": psi.category_name if psi else None,
        "subcategory_name": psi.subcategory_name if psi else None,
        # Bundle-item metadata
        "quantity":      item.quantity,
        "sort_order":    item.sort_order,
        "is_anchor":     item.is_anchor,
    }


def _bundle_summary(bundle: Bundle, item_count: int) -> dict:
    """Lightweight summary dict for the list endpoint."""
    return {
        "bundle_code":      bundle.bundle_code,
        "name":             bundle.name,
        "name_ar":          bundle.name_ar,
        "description":      bundle.description,
        "image_url":        bundle.image_url,
        "discount_percent": float(bundle.discount_percent) if bundle.discount_percent else None,
        "sort_order":       bundle.sort_order,
        "item_count":       item_count,
        "valid_from":       bundle.valid_from.isoformat() if bundle.valid_from else None,
        "valid_until":      bundle.valid_until.isoformat() if bundle.valid_until else None,
    }


def _compute_pricing(
    products:        list[Product],
    discount_percent: Optional[float],
) -> dict:
    """
    Return a pricing summary for the bundle:
      original_total   — sum of individual product prices (IQD)
      discounted_total — after bundle discount (IQD), or None if no discount
      savings          — difference (IQD), or None
    """
    valid_prices = [
        float(p.price) for p in products
        if p.price and is_valid_raw_price(p.price)
    ]

    if not valid_prices:
        return {
            "original_total":   None,
            "discounted_total": None,
            "savings":          None,
        }

    raw_total = sum(valid_prices)
    iqd_total = convert_to_iqd(raw_total)

    if discount_percent:
        raw_discounted = round(raw_total * (1 - discount_percent / 100), 4)
        iqd_discounted = convert_to_iqd(raw_discounted)
        iqd_savings    = convert_to_iqd(round(raw_total - raw_discounted, 4))
    else:
        iqd_discounted = None
        iqd_savings    = None

    return {
        "original_total":   iqd_total,
        "discounted_total": iqd_discounted,
        "savings":          iqd_savings,
    }


# ── Public service functions ──────────────────────────────────────────────────

def list_bundles(db: Session, active_only: bool = True) -> dict:
    """
    Return a summary list of bundles, ordered by sort_order ASC.

    Each entry includes: bundle_code, name, name_ar, description, image_url,
    discount_percent, item_count, valid_from, valid_until.

    active_only=True (default) filters to is_active=True AND within the
    valid_from/valid_until scheduling window.
    """
    q = db.query(Bundle)
    if active_only:
        q = q.filter(Bundle.is_active == True)
    bundles = q.order_by(Bundle.sort_order.asc(), Bundle.id.asc()).all()

    if active_only:
        bundles = [b for b in bundles if _is_scheduled_active(b)]

    if not bundles:
        return {
            "found":   False,
            "message": "No bundles available right now.",
            "bundles": [],
            "total":   0,
        }

    # Count items per bundle in one query
    bundle_ids = [b.id for b in bundles]
    item_counts_raw = (
        db.query(BundleItem.bundle_id)
        .filter(BundleItem.bundle_id.in_(bundle_ids))
        .all()
    )
    counts: dict[int, int] = {}
    for (bid,) in item_counts_raw:
        counts[bid] = counts.get(bid, 0) + 1

    result = [_bundle_summary(b, counts.get(b.id, 0)) for b in bundles]

    return {
        "found":   True,
        "total":   len(result),
        "bundles": result,
    }


def get_bundle_detail(db: Session, bundle_code: str) -> dict:
    """
    Full bundle detail: metadata + all products with IQD pricing.

    Returns {"found": False, ...} when the bundle doesn't exist, is inactive,
    or is outside its scheduling window — so the router can raise 404.

    Products that are inactive or out of stock are excluded from the item list
    (the bundle still "exists" but shows only available items).
    """
    bundle = (
        db.query(Bundle)
        .filter(Bundle.bundle_code == bundle_code)
        .first()
    )

    if not bundle:
        return {
            "found":   False,
            "message": f"Bundle '{bundle_code}' not found.",
        }

    if not bundle.is_active or not _is_scheduled_active(bundle):
        return {
            "found":   False,
            "message": f"Bundle '{bundle_code}' is not currently available.",
        }

    # Load items in display order
    items: list[BundleItem] = (
        db.query(BundleItem)
        .filter(BundleItem.bundle_id == bundle.id)
        .order_by(BundleItem.sort_order.asc(), BundleItem.id.asc())
        .all()
    )

    if not items:
        return {
            "found":   False,
            "message": f"Bundle '{bundle_code}' has no products configured.",
        }

    barcodes   = [item.barcode for item in items]
    prod_map   = _product_map(db, barcodes)
    search_map = _psi_map(db, barcodes)

    # Only include active, in-stock products
    formatted_items: list[dict] = []
    available_products: list[Product] = []

    for item in items:
        product = prod_map.get(item.barcode)
        if not product:
            logger.warning(
                "BundleItem barcode=%s not found in products table (bundle=%s)",
                item.barcode, bundle_code,
            )
            continue
        if product.product_status and str(product.product_status) != "active":
            continue
        if (product.available_qty or 0) <= 0:
            continue

        psi = search_map.get(item.barcode)
        formatted_items.append(_format_product(item, product, psi))
        available_products.append(product)

    if not formatted_items:
        return {
            "found":   False,
            "message": f"Bundle '{bundle_code}' has no available products right now.",
        }

    discount_pct = float(bundle.discount_percent) if bundle.discount_percent else None
    pricing      = _compute_pricing(available_products, discount_pct)

    return {
        "found":            True,
        "bundle_code":      bundle.bundle_code,
        "name":             bundle.name,
        "name_ar":          bundle.name_ar,
        "description":      bundle.description,
        "image_url":        bundle.image_url,
        "discount_percent": discount_pct,
        "sort_order":       bundle.sort_order,
        "valid_from":       bundle.valid_from.isoformat()  if bundle.valid_from  else None,
        "valid_until":      bundle.valid_until.isoformat() if bundle.valid_until else None,
        "item_count":       len(formatted_items),
        "pricing":          pricing,
        "products":         formatted_items,
    }


def get_bundles_for_product(db: Session, barcode: str) -> dict:
    """
    Find all active bundles that contain a given product barcode.

    Useful for product detail pages: "Also available as part of these bundles…"
    Returns lightweight bundle summaries (code, name, discount_percent, item_count).
    """
    # Find bundle IDs that contain this barcode
    membership = (
        db.query(BundleItem.bundle_id)
        .filter(BundleItem.barcode == barcode)
        .all()
    )
    bundle_ids = [row[0] for row in membership]

    if not bundle_ids:
        return {
            "found":   False,
            "barcode": barcode,
            "bundles": [],
        }

    bundles = (
        db.query(Bundle)
        .filter(
            Bundle.id.in_(bundle_ids),
            Bundle.is_active == True,
        )
        .order_by(Bundle.sort_order.asc())
        .all()
    )
    bundles = [b for b in bundles if _is_scheduled_active(b)]

    if not bundles:
        return {
            "found":   False,
            "barcode": barcode,
            "bundles": [],
        }

    # Count items per bundle
    active_ids = [b.id for b in bundles]
    raw_counts = (
        db.query(BundleItem.bundle_id)
        .filter(BundleItem.bundle_id.in_(active_ids))
        .all()
    )
    counts: dict[int, int] = {}
    for (bid,) in raw_counts:
        counts[bid] = counts.get(bid, 0) + 1

    return {
        "found":   True,
        "barcode": barcode,
        "bundles": [_bundle_summary(b, counts.get(b.id, 0)) for b in bundles],
    }
