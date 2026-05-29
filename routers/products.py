"""
routers/products.py
===================
Product Management API — 11 endpoints.

  GET    /products/filters           dropdown data for frontend filter menus
  GET    /products                   paginated list with search + filter + sort
  GET    /products/{barcode}         single product full details
  PUT    /products/{barcode}         partial update (inventory / content fields)
  DELETE /products/{barcode}         hard delete

  ── Status management (Step 15) ──────────────────────────────────────────────
  PATCH  /products/{barcode}/status  status transition with audit log
  PATCH  /products/{barcode}/flags   editorial / recommendation flag update
  POST   /products/bulk/status       bulk status transition (up to 500 products)
  POST   /products/bulk/flags        bulk editorial flag update (up to 500 products)

Error handling
--------------
AppError subclasses (NotFoundError, ConflictError, AppValidationError, …)
propagate to the global handler registered in main.py, which converts them to
{"success": false, "error": "<message>"} with the correct status code.

The only local try/except blocks are around db.commit() in write endpoints,
so unexpected DB write failures can be rolled back cleanly before raising
ServiceError (500).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.exceptions import ServiceError
from database import get_db
from repositories.product import ProductRepository
from schemas.status import (
    BulkFlagsUpdateRequest,
    BulkStatusChangeRequest,
    FlagsUpdateRequest,
    StatusChangeRequest,
)
from services.status import ProductStatusService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["Products"])


# ── Dependencies ─────────────────────────────────────────────────────────────

def get_repo(db: Session = Depends(get_db)) -> ProductRepository:
    return ProductRepository(db)


def get_status_service(db: Session = Depends(get_db)) -> ProductStatusService:
    return ProductStatusService(db)


# ═══════════════════════════════════════════════════════════════════════════════
# GET /products/filters
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/products/filters")
def get_filters(repo: ProductRepository = Depends(get_repo)):
    """Dropdown data for frontend filter menus (brands, categories, subcategories)."""
    return {"success": True, "data": repo.get_filter_options()}


# ═══════════════════════════════════════════════════════════════════════════════
# GET /products
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/products")
def list_products(
    q:               Optional[str]   = None,
    brand_id:        Optional[int]   = None,
    category_id:     Optional[int]   = None,
    subcategory_id:  Optional[int]   = None,
    is_best_selling: Optional[int]   = None,
    in_stock:        Optional[bool]  = None,
    min_price:       Optional[float] = None,
    max_price:       Optional[float] = None,
    page:            int             = 1,
    limit:           int             = 10,
    sort_by:         Optional[str]   = "created_desc",
    repo: ProductRepository = Depends(get_repo),
):
    """Product inventory list — supports search, filter, sort, pagination."""
    result = repo.list_products(
        q=q, brand_id=brand_id, category_id=category_id,
        subcategory_id=subcategory_id, is_best_selling=is_best_selling,
        in_stock=in_stock, min_price=min_price, max_price=max_price,
        page=page, limit=limit, sort_by=sort_by,
    )
    return {"success": True, "data": result}


# ═══════════════════════════════════════════════════════════════════════════════
# GET /products/{barcode}
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/products/{barcode}")
def get_product(barcode: str, repo: ProductRepository = Depends(get_repo)):
    """Single product full details.
    Raises NotFoundError (→ 404) if barcode does not exist.
    """
    data = repo.get_by_barcode(barcode)
    if data is None:
        from core.exceptions import NotFoundError
        raise NotFoundError(f"Product '{barcode}' not found.")
    return {"success": True, "data": data}


# ═══════════════════════════════════════════════════════════════════════════════
# PUT /products/{barcode}
# ═══════════════════════════════════════════════════════════════════════════════

@router.put("/products/{barcode}")
def update_product(
    barcode: str,
    payload: dict,
    db: Session = Depends(get_db),
    repo: ProductRepository = Depends(get_repo),
):
    """Partial update.

    repo.update() raises typed AppError subclasses for all validation and
    lookup failures — those propagate to the global handler automatically.

    Only the db.commit() is wrapped locally so an unexpected DB write failure
    can be rolled back cleanly before raising ServiceError (500).
    """
    result = repo.update(barcode, payload)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB commit failed for PUT /products/%s: %s", barcode, exc, exc_info=True)
        raise ServiceError("Update failed due to a database error. Please try again.")

    response: dict = {
        "success": True,
        "message": "Product updated successfully",
        "data":    result["product"],
    }
    if result["warnings"]:
        response["warnings"] = result["warnings"]
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE /products/{barcode}
# ═══════════════════════════════════════════════════════════════════════════════

@router.delete("/products/{barcode}")
def delete_product(barcode: str, repo: ProductRepository = Depends(get_repo)):
    """
    Soft-delete a product (sets deleted_at timestamp).

    The product is excluded from all public-facing queries but its row is
    retained in the DB.  Restore with POST /products/{barcode}/restore.
    Raises NotFoundError (→ 404) if barcode does not exist or is already deleted.
    """
    result = repo.delete(barcode)
    return {
        "success":    True,
        "message":    "Product soft-deleted. Use POST /products/{barcode}/restore to undo.",
        "barcode":    result["barcode"],
        "item_name":  result["item_name"],
        "deleted_at": result["deleted_at"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# POST /products/{barcode}/restore
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/products/{barcode}/restore")
def restore_product(
    barcode: str,
    db:      Session              = Depends(get_db),
    repo:    ProductRepository    = Depends(get_repo),
):
    """
    Restore a soft-deleted product by clearing its deleted_at timestamp.

    Raises NotFoundError (→ 404)  if the product row does not exist.
    Raises AppValidationError (→ 422) if the product is not currently deleted.
    """
    result = repo.restore(barcode)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB commit failed for POST /products/%s/restore: %s", barcode, exc, exc_info=True)
        raise ServiceError("Restore failed due to a database error. Please try again.")

    return {
        "success": True,
        "message": "Product restored successfully.",
        "data":    result,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH /products/{barcode}/status   (Step 15 — status management)
# ═══════════════════════════════════════════════════════════════════════════════

@router.patch("/products/{barcode}/status")
def change_product_status(
    barcode: str,
    body:    StatusChangeRequest,
    db:      Session               = Depends(get_db),
    svc:     ProductStatusService  = Depends(get_status_service),
):
    """
    Transition a product to a new status with full audit logging.

    Allowed transitions
    -------------------
    draft    → active, inactive
    active   → draft, inactive
    inactive → active
    inactive → draft  ✗  (blocked — re-activate first)

    Body
    ----
    {
      "status":     "active",          # required
      "changed_by": "admin_user_id",   # optional; defaults to "system"
      "reason":     "seasonal launch"  # optional note in audit log
    }
    """
    result = svc.change_status(
        barcode    = barcode,
        new_status = body.status,
        changed_by = body.changed_by,
        reason     = body.reason,
    )
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB commit failed for PATCH /products/%s/status: %s", barcode, exc, exc_info=True)
        raise ServiceError("Status update failed due to a database error. Please try again.")

    return {"success": True, "data": result}


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH /products/{barcode}/flags   (Step 15 — editorial flags)
# ═══════════════════════════════════════════════════════════════════════════════

@router.patch("/products/{barcode}/flags")
def update_product_flags(
    barcode: str,
    body:    FlagsUpdateRequest,
    db:      Session               = Depends(get_db),
    svc:     ProductStatusService  = Depends(get_status_service),
):
    """
    Update editorial / recommendation flags for a single product.

    All fields are optional — only the ones you include are changed.
    SAP sync will never overwrite these fields.

    Updatable flags
    ---------------
    is_recommended, is_new_arrival, is_best_selling, is_cod_recommended,
    recommendation_priority (0–9999, lower = higher rank),
    recommendation_score_override (0–999),
    price_tier (Budget | Mid | Premium | Luxury),
    brand_family (e.g. "Italian Niche"),
    best_selling_scope (global | category | brand | subcategory)

    Body example
    ------------
    {
      "is_recommended": true,
      "price_tier": "Premium",
      "recommendation_priority": 5
    }
    """
    result = svc.update_flags(barcode=barcode, flags=body)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB commit failed for PATCH /products/%s/flags: %s", barcode, exc, exc_info=True)
        raise ServiceError("Flags update failed due to a database error. Please try again.")

    return {"success": True, "data": result}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /products/bulk/status   (Step 15 — bulk status)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/products/bulk/status")
def bulk_change_status(
    body: BulkStatusChangeRequest,
    db:   Session              = Depends(get_db),
    svc:  ProductStatusService = Depends(get_status_service),
):
    """
    Transition up to 500 products to a new status in one request.

    Valid + invalid products are processed independently — an invalid
    transition for one product does not block the others.

    Response includes three lists: updated, skipped (already in state),
    errors (not found or illegal transition).

    Body example
    ------------
    {
      "barcodes":   ["BC001", "BC002", "BC003"],
      "status":     "inactive",
      "changed_by": "admin_user_id",
      "reason":     "End of season — Q2 2026"
    }
    """
    result = svc.bulk_change_status(
        barcodes   = body.barcodes,
        new_status = body.status,
        changed_by = body.changed_by,
        reason     = body.reason,
    )
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB commit failed for POST /products/bulk/status: %s", exc, exc_info=True)
        raise ServiceError("Bulk status update failed due to a database error. Please try again.")

    return {"success": True, "data": result}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /products/bulk/flags   (Step 15 — bulk editorial flags)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/products/bulk/flags")
def bulk_update_flags(
    body: BulkFlagsUpdateRequest,
    db:   Session              = Depends(get_db),
    svc:  ProductStatusService = Depends(get_status_service),
):
    """
    Apply the same editorial flags to up to 500 products at once.

    Only the flag fields you include in 'flags' are updated.
    Products not found are listed in 'not_found' but don't fail the request.

    Body example
    ------------
    {
      "barcodes": ["BC001", "BC002"],
      "flags": {
        "is_new_arrival": true,
        "price_tier":     "Premium"
      }
    }
    """
    result = svc.bulk_update_flags(barcodes=body.barcodes, flags=body.flags)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("DB commit failed for POST /products/bulk/flags: %s", exc, exc_info=True)
        raise ServiceError("Bulk flags update failed due to a database error. Please try again.")

    return {"success": True, "data": result}
