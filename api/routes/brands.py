"""
api/routes/brands.py
====================
GET  /brands          — list brands with filtering, sorting, counts, pagination
GET  /brands/{id}     — brand detail with product_count and category_breakdown
POST /brands          — create a new brand
PUT  /brands/{id}     — update brand fields
DELETE /brands/{id}   — soft-delete a brand

Response contract (backward-compatible with existing tests and frontend)
------------------------------------------------------------------------
List:
  {"success": true, "data": {"brands": [...], "pagination": {
      "total": N, "page": P, "limit": L,
      "total_pages": T, "has_prev": bool, "has_next": bool}}}

Detail:
  {"success": true, "data": {
      "id", "name", "name_ar", "is_active", "created_at",
      "product_count": N,
      "category_breakdown": [{"name": "...", "product_count": N}, ...]}}

Validation errors:
  HTTP 400 → {"detail": {"success": false, "error": {"code": "VALIDATION_ERROR", "message": "..."}}}

Not found:
  HTTP 404 → {"detail": {"success": false, "error": {"code": "NOT_FOUND", "message": "..."}}}
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db
from models import Brand, Category, Product

router = APIRouter(prefix="", tags=["Brands"])

# ── Constants ──────────────────────────────────────────────────────────────────

_VALID_SORT_BY    = frozenset({"name", "product_count"})
_VALID_SORT_ORDER = frozenset({"asc", "desc"})
_LIMIT_MIN        = 1
_LIMIT_MAX        = 100


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class BrandCreate(BaseModel):
    name:      str
    name_ar:   Optional[str]  = None
    is_active: Optional[bool] = True


class BrandUpdate(BaseModel):
    name:      Optional[str]  = None
    name_ar:   Optional[str]  = None
    is_active: Optional[bool] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_name(value: str) -> str:
    return value.strip()


def _validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"success": False, "error": {"code": "VALIDATION_ERROR", "message": message}},
    )


def _not_found(message: str = "Brand not found.") -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"success": False, "error": {"code": "NOT_FOUND", "message": message}},
    )


def _build_pagination(total: int, page: int, limit: int) -> dict:
    total_pages = (total + limit - 1) // limit if total > 0 else 0
    return {
        "total":       total,
        "page":        page,
        "limit":       limit,
        "total_pages": total_pages,
        "has_prev":    page > 1,
        "has_next":    page < total_pages,
    }


def _validate_list_params(limit: int, sort_by: str, sort_order: str, page: int) -> None:
    if limit < _LIMIT_MIN or limit > _LIMIT_MAX:
        raise _validation_error(f"limit must be between {_LIMIT_MIN} and {_LIMIT_MAX}.")
    if sort_by not in _VALID_SORT_BY:
        raise _validation_error(
            f"sort_by must be one of: {', '.join(sorted(_VALID_SORT_BY))}."
        )
    if sort_order not in _VALID_SORT_ORDER:
        raise _validation_error("sort_order must be 'asc' or 'desc'.")
    if page < 1:
        raise _validation_error("page must be >= 1.")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/brands")
def list_brands(
    search:         Optional[str]  = None,
    is_active:      Optional[bool] = None,
    category_id:    Optional[int]  = None,
    include_counts: bool           = False,
    sort_by:        str            = "name",
    sort_order:     str            = "asc",
    page:           int            = 1,
    limit:          int            = 100,
    db:             Session        = Depends(get_db),
):
    """List brands with optional filtering, sorting, count aggregation, and pagination."""
    _validate_list_params(limit, sort_by, sort_order, page)

    # Product count subquery — used for both sort_by=product_count and include_counts
    cnt_sub = (
        db.query(Product.brand_id, func.count(Product.barcode).label("cnt"))
        .filter(Product.brand_id.isnot(None))
        .group_by(Product.brand_id)
        .subquery()
    )

    # Base query: Brand LEFT JOIN product counts
    q = (
        db.query(Brand, func.coalesce(cnt_sub.c.cnt, 0).label("product_count"))
        .outerjoin(cnt_sub, Brand.id == cnt_sub.c.brand_id)
    )

    # Filters
    if is_active is not None:
        q = q.filter(Brand.is_active == (1 if is_active else 0))
    if search:
        q = q.filter(
            or_(
                Brand.name.ilike(f"%{search}%"),
                Brand.name_ar.ilike(f"%{search}%"),
            )
        )
    if category_id is not None:
        # Keep only brands that have at least one product in this category
        brand_ids_in_cat = (
            db.query(Product.brand_id)
            .filter(
                Product.category_id == category_id,
                Product.brand_id.isnot(None),
            )
            .distinct()
        )
        q = q.filter(Brand.id.in_(brand_ids_in_cat))

    total = q.count()

    # Sort
    cnt_col = func.coalesce(cnt_sub.c.cnt, 0)
    if sort_by == "product_count":
        primary = cnt_col.desc() if sort_order == "desc" else cnt_col.asc()
        q = q.order_by(primary, Brand.name.asc())
    else:
        primary = Brand.name.desc() if sort_order == "desc" else Brand.name.asc()
        q = q.order_by(primary)

    # Pagination
    offset = (page - 1) * limit
    rows   = q.offset(offset).limit(limit).all()

    brands = []
    for brand, product_count in rows:
        item: dict = {
            "id":         brand.id,
            "name":       brand.name,
            "name_ar":    brand.name_ar,
            "is_active":  bool(brand.is_active),
            "created_at": brand.created_at.isoformat() if brand.created_at else None,
        }
        if include_counts:
            item["product_count"] = product_count
        brands.append(item)

    return {
        "success": True,
        "data": {
            "brands":     brands,
            "pagination": _build_pagination(total, page, limit),
        },
    }


@router.get("/brands/{id}")
def get_brand(id: int, db: Session = Depends(get_db)):
    """Brand detail including product_count and per-category breakdown."""
    brand = db.query(Brand).filter(Brand.id == id).first()
    if not brand:
        raise _not_found()

    # Product count for this brand
    product_count = (
        db.query(func.count(Product.barcode))
        .filter(Product.brand_id == brand.id)
        .scalar()
    ) or 0

    # Category breakdown: [{name, product_count}, ...] sorted by count desc
    breakdown_rows = (
        db.query(Category.name, func.count(Product.barcode).label("cnt"))
        .join(Product, Product.category_id == Category.id)
        .filter(Product.brand_id == brand.id)
        .group_by(Category.id, Category.name)
        .order_by(func.count(Product.barcode).desc())
        .all()
    )
    category_breakdown = [
        {"name": row.name, "product_count": row.cnt}
        for row in breakdown_rows
    ]

    return {
        "success": True,
        "data": {
            "id":                 brand.id,
            "name":               brand.name,
            "name_ar":            brand.name_ar,
            "is_active":          bool(brand.is_active),
            "created_at":         brand.created_at.isoformat() if brand.created_at else None,
            "product_count":      product_count,
            "category_breakdown": category_breakdown,
        },
    }


@router.post("/brands", status_code=201)
def create_brand(payload: BrandCreate, db: Session = Depends(get_db)):
    """Create a new brand with case-insensitive duplicate detection."""
    raw_name = payload.name
    if raw_name is None or not str(raw_name).strip():
        return JSONResponse(status_code=422, content={"success": False, "error": "name is required"})

    name = _normalize_name(str(raw_name))
    if not name:
        return JSONResponse(status_code=422, content={"success": False, "error": "name is required"})
    if len(name) > 255:
        return JSONResponse(status_code=422, content={"success": False, "error": "name must be 255 characters or fewer"})

    existing = db.query(Brand).filter(func.lower(Brand.name) == name.lower()).first()
    if existing:
        return JSONResponse(status_code=409, content={"success": False, "error": f"Brand '{name}' already exists."})

    name_ar: Optional[str] = None
    name_ar_raw = payload.name_ar
    if name_ar_raw is not None and str(name_ar_raw).strip():
        name_ar = _normalize_name(str(name_ar_raw))
        if len(name_ar) > 255:
            return JSONResponse(status_code=422, content={"success": False, "error": "name_ar must be 255 characters or fewer"})
        existing_ar = db.query(Brand).filter(func.lower(Brand.name_ar) == name_ar.lower()).first()
        if existing_ar:
            return JSONResponse(status_code=409, content={"success": False, "error": f"Brand with Arabic name '{name_ar}' already exists."})

    try:
        brand = Brand(name=name, name_ar=name_ar, is_active=1 if payload.is_active else 0)
        db.add(brand)
        db.commit()
        db.refresh(brand)
        return {
            "success": True,
            "message": "Brand created successfully",
            "data": {
                "id":         brand.id,
                "name":       brand.name,
                "name_ar":    brand.name_ar,
                "is_active":  bool(brand.is_active),
                "created_at": brand.created_at.isoformat() if brand.created_at else None,
            },
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create brand: {exc}")
