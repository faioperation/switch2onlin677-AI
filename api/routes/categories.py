"""
api/routes/categories.py
========================
GET  /categories        — list categories with filtering, sorting, counts, pagination
GET  /categories/{id}   — category detail with nested subcategories and product_count
POST /categories        — create a new category

Response contract (backward-compatible with existing tests and frontend)
------------------------------------------------------------------------
List:
  {"success": true, "data": {"categories": [...], "pagination": {
      "total": N, "page": P, "limit": L,
      "total_pages": T, "has_prev": bool, "has_next": bool}}}

  Each category (base):  {id, name, name_ar, is_active, created_at}
  With include_counts=true adds: {product_count, subcategory_count}

Detail:
  {"success": true, "data": {
      id, name, name_ar, is_active, created_at, product_count,
      "subcategories": [{"id", "name", "slug": null}, ...]}}

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
from models import Category, Product, Subcategory

router = APIRouter(prefix="", tags=["Categories"])

# ── Constants ──────────────────────────────────────────────────────────────────

_VALID_SORT_BY    = frozenset({"name", "product_count"})
_VALID_SORT_ORDER = frozenset({"asc", "desc"})
_LIMIT_MIN        = 1
_LIMIT_MAX        = 100


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class CategoryCreate(BaseModel):
    name:      str
    name_ar:   Optional[str]  = None
    is_active: Optional[bool] = True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_name(value: str) -> str:
    return value.strip()


def _validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"success": False, "error": {"code": "VALIDATION_ERROR", "message": message}},
    )


def _not_found(message: str = "Category not found.") -> HTTPException:
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

@router.get("/categories")
def list_categories(
    search:         Optional[str]  = None,
    is_active:      Optional[bool] = None,
    include_counts: bool           = False,
    sort_by:        str            = "name",
    sort_order:     str            = "asc",
    page:           int            = 1,
    limit:          int            = 100,
    db:             Session        = Depends(get_db),
):
    """List categories with optional filtering, sorting, count aggregation, and pagination."""
    _validate_list_params(limit, sort_by, sort_order, page)

    # Product count subquery per category
    prod_cnt_sub = (
        db.query(Product.category_id, func.count(Product.barcode).label("prod_cnt"))
        .filter(Product.category_id.isnot(None))
        .group_by(Product.category_id)
        .subquery()
    )

    # Subcategory count subquery per category
    sub_cnt_sub = (
        db.query(Subcategory.category_id, func.count(Subcategory.id).label("sub_cnt"))
        .filter(Subcategory.category_id.isnot(None))
        .group_by(Subcategory.category_id)
        .subquery()
    )

    # Base query with both counts joined
    q = (
        db.query(
            Category,
            func.coalesce(prod_cnt_sub.c.prod_cnt, 0).label("product_count"),
            func.coalesce(sub_cnt_sub.c.sub_cnt,  0).label("subcategory_count"),
        )
        .outerjoin(prod_cnt_sub, Category.id == prod_cnt_sub.c.category_id)
        .outerjoin(sub_cnt_sub,  Category.id == sub_cnt_sub.c.category_id)
    )

    # Filters
    if is_active is not None:
        q = q.filter(Category.is_active == (1 if is_active else 0))
    if search:
        q = q.filter(
            or_(
                Category.name.ilike(f"%{search}%"),
                Category.name_ar.ilike(f"%{search}%"),
            )
        )

    total = q.count()

    # Sort
    prod_col = func.coalesce(prod_cnt_sub.c.prod_cnt, 0)
    if sort_by == "product_count":
        primary = prod_col.desc() if sort_order == "desc" else prod_col.asc()
        q = q.order_by(primary, Category.name.asc())
    else:
        primary = Category.name.desc() if sort_order == "desc" else Category.name.asc()
        q = q.order_by(primary)

    # Pagination
    offset = (page - 1) * limit
    rows   = q.offset(offset).limit(limit).all()

    categories = []
    for cat, product_count, subcategory_count in rows:
        item: dict = {
            "id":         cat.id,
            "name":       cat.name,
            "name_ar":    cat.name_ar,
            "is_active":  bool(cat.is_active),
            "created_at": cat.created_at.isoformat() if cat.created_at else None,
        }
        if include_counts:
            item["product_count"]    = product_count
            item["subcategory_count"] = subcategory_count
        categories.append(item)

    return {
        "success": True,
        "data": {
            "categories": categories,
            "pagination":  _build_pagination(total, page, limit),
        },
    }


@router.get("/categories/{id}")
def get_category(id: int, db: Session = Depends(get_db)):
    """Category detail with nested subcategories (sorted A→Z) and product_count."""
    category = db.query(Category).filter(Category.id == id).first()
    if not category:
        raise _not_found()

    # Product count for this category
    product_count = (
        db.query(func.count(Product.barcode))
        .filter(Product.category_id == category.id)
        .scalar()
    ) or 0

    # Subcategories sorted alphabetically; slug is null (no slug column yet)
    subs = (
        db.query(Subcategory)
        .filter(Subcategory.category_id == category.id)
        .order_by(Subcategory.name.asc())
        .all()
    )
    subcategories = [
        {"id": s.id, "name": s.name, "slug": None}
        for s in subs
    ]

    return {
        "success": True,
        "data": {
            "id":            category.id,
            "name":          category.name,
            "name_ar":       category.name_ar,
            "is_active":     bool(category.is_active),
            "created_at":    category.created_at.isoformat() if category.created_at else None,
            "product_count": product_count,
            "subcategories": subcategories,
        },
    }


@router.post("/categories", status_code=201)
def create_category(payload: CategoryCreate, db: Session = Depends(get_db)):
    """Create a new category with case-insensitive duplicate detection."""
    raw_name = payload.name
    if raw_name is None or not str(raw_name).strip():
        return JSONResponse(status_code=422, content={"success": False, "error": "name is required"})

    name = _normalize_name(str(raw_name))
    if not name:
        return JSONResponse(status_code=422, content={"success": False, "error": "name is required"})
    if len(name) > 255:
        return JSONResponse(status_code=422, content={"success": False, "error": "name must be 255 characters or fewer"})

    existing = db.query(Category).filter(func.lower(Category.name) == name.lower()).first()
    if existing:
        return JSONResponse(status_code=409, content={"success": False, "error": f"Category '{name}' already exists."})

    name_ar: Optional[str] = None
    name_ar_raw = payload.name_ar
    if name_ar_raw is not None and str(name_ar_raw).strip():
        name_ar = _normalize_name(str(name_ar_raw))
        if len(name_ar) > 255:
            return JSONResponse(status_code=422, content={"success": False, "error": "name_ar must be 255 characters or fewer"})
        existing_ar = db.query(Category).filter(func.lower(Category.name_ar) == name_ar.lower()).first()
        if existing_ar:
            return JSONResponse(status_code=409, content={"success": False, "error": f"Category with Arabic name '{name_ar}' already exists."})

    try:
        category = Category(name=name, name_ar=name_ar, is_active=1 if payload.is_active else 0)
        db.add(category)
        db.commit()
        db.refresh(category)
        return {
            "success": True,
            "message": "Category created successfully",
            "data": {
                "id":         category.id,
                "name":       category.name,
                "name_ar":    category.name_ar,
                "is_active":  bool(category.is_active),
                "created_at": category.created_at.isoformat() if category.created_at else None,
            },
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create category: {exc}")
