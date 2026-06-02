"""
routers/bundles.py
==================
Bundle API — reads from the new bundles + bundle_items tables.

These endpoints replace the legacy GET /recommendations/bundle/{bundle_group}
endpoint (which reads the flat bundle_group column on products). Both routes
coexist; the old one is deprecated.

Endpoints
---------
GET /bundles
    List all active bundles (with scheduling window enforcement).
    Returns lightweight summaries: code, name, discount, item_count, image.

GET /bundles/{bundle_code}
    Full bundle detail: metadata + product list with IQD pricing.
    Raises 404 when the bundle doesn't exist, is inactive, or is expired.

GET /bundles/for-product/{barcode}
    All active bundles that contain a given product barcode.
    Used on product detail pages ("Also available as part of…").

Error handling
--------------
404  NotFoundError  — bundle not found / inactive / expired / no items
422  AppValidationError — malformed params (empty code / barcode)
All other errors propagate to the global handler in main.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.exceptions import AppValidationError, NotFoundError
from database import get_db
from services.bundle import get_bundle_detail, get_bundles_for_product, list_bundles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bundles", tags=["Bundles"])


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("")
def bundles_list(db: Session = Depends(get_db)):
    """
    List all currently active bundles.

    Bundles are returned in sort_order ASC order. The scheduling window
    (valid_from / valid_until) is enforced server-side — expired or
    not-yet-active bundles are excluded automatically.

    Each entry includes:
      bundle_code, name, name_ar, description, image_url,
      discount_percent, sort_order, item_count, valid_from, valid_until
    """
    return list_bundles(db, active_only=True)


@router.get("/for-product/{barcode}")
def bundles_for_product(barcode: str, db: Session = Depends(get_db)):
    """
    Return all active bundles that contain a given product barcode.

    Used on product detail pages to surface "Also available as part of
    the Ramadan Kit / Summer Bundle…" messaging.

    Returns an empty list (not 404) when the product exists but is in
    no active bundles — this is a normal state, not an error.
    """
    barcode = barcode.strip()
    if not barcode:
        raise AppValidationError("barcode path parameter cannot be empty.")
    return get_bundles_for_product(db, barcode=barcode)


@router.get("/{bundle_code}")
def bundle_detail(bundle_code: str, db: Session = Depends(get_db)):
    """
    Full bundle detail with products and IQD pricing.

    Response shape:
    {
      "found": true,
      "bundle_code": "ramadan-kit-2026",
      "name": "Ramadan Skincare Kit",
      "name_ar": "...",
      "description": "...",
      "image_url": "...",
      "discount_percent": 15.0,
      "valid_from": "2026-03-01T00:00:00",
      "valid_until": "2026-04-01T00:00:00",
      "item_count": 3,
      "pricing": {
        "original_total": "75,000 IQD",
        "discounted_total": "63,750 IQD",
        "savings": "11,250 IQD"
      },
      "products": [
        {
          "barcode": "...",
          "item_name": "...",
          "price": "25,000 IQD",
          "quantity": 1,
          "sort_order": 0,
          "is_anchor": true,
          ...
        }
      ]
    }

    Raises 404 when:
      - bundle_code doesn't exist
      - bundle is inactive (is_active=false)
      - bundle is outside its scheduling window
      - bundle has no available products
    """
    bundle_code = bundle_code.strip()
    if not bundle_code:
        raise AppValidationError("bundle_code path parameter cannot be empty.")

    result = get_bundle_detail(db, bundle_code=bundle_code)

    if not result.get("found"):
        raise NotFoundError(result.get("message", f"Bundle '{bundle_code}' not found."))

    return result
