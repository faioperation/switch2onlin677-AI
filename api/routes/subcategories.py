"""
api/routes/subcategories.py
============================
GET  /subcategories        — list subcategories with filtering, sorting, counts, pagination
GET  /subcategories/{id}   — subcategory detail with nested parent category and product_count
POST /subcategories        — create a new subcategory

Response contract (backward-compatible with existing tests and frontend)
------------------------------------------------------------------------
List:
  {"success": true, "data": {"subcategories": [...], "pagination": {
      "total": N, "page": P, "limit": L,
      "total_pages": T, "has_prev": bool, "has_next": bool}}}

  Each subcategory (base): {id, name, name_ar, is_active, created_at,
                             "category": {"id": N, "name": "..."}}
  With include_counts=true adds: {product_count}

Detail:
  {"success": true, "data": {
      id, name, name_ar, is_active, created_at, product_count,
      "category": {"id": N, "name": "..." | null}}}

  When category_id refers to a non-existent parent:
      "category": {"id": <category_id>, "name": null}

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

router = APIRouter(prefix="", tags=["Subcategories"])

# ── Constants ──────────────────────────────────────────────────────────────────

_VALID_SORT_BY    = frozenset({"name", "product_count"})
_VALID_SORT_ORDER = frozenset({"asc", "desc"})
_LIMIT_MIN        = 1
_LIMIT_MAX        = 100


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class SubcategoryCreate(BaseModel):
    name:        str
    name_ar:     Optional[str] = None
    category_id: Optional[int] = None
    is_active:   Optional[bool] = True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_name(value: str) -> str:
    return value.strip()


def _validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"success": False, "error": {"code": "VALIDATION_ERROR", "message": message}},
    )


def _not_found(message: str = "Subcategory not found.") -> HTTPException:
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

@router.get("/subcategories")
def list_subcategories(
    category_id:    Optional[int]  = None,
    search:         Optional[str]  = None,
    is_active:      Optional[bool] = None,
    include_counts: bool           = False,
    sort_by:        str            = "name",
    sort_order:     str            = "asc",
    page:           int            = 1,
    limit:          int            = 100,
    db:             Session        = Depends(get_db),
):
    """List subcategories with filtering, sorting, count aggregation, and pagination."""
    _validate_list_params(limit, sort_by, sort_order, page)

    # Product count subquery per subcategory
    prod_cnt_sub = (
        db.query(Product.subcategory_id, func.count(Product.barcode).label("prod_cnt"))
        .filter(Product.subcategory_id.isnot(None))
        .group_by(Product.subcategory_id)
        .subquery()
    )

    # Base query: Subcategory LEFT JOIN product counts
    q = (
        db.query(Subcategory, func.coalesce(prod_cnt_sub.c.prod_cnt, 0).label("product_count"))
        .outerjoin(prod_cnt_sub, Subcategory.id == prod_cnt_sub.c.subcategory_id)
    )

    # Filters
    if category_id is not None:
        q = q.filter(Subcategory.category_id == category_id)
    if is_active is not None:
        q = q.filter(Subcategory.is_active == (1 if is_active else 0))
    if search:
        q = q.filter(
            or_(
                Subcategory.name.ilike(f"%{search}%"),
                Subcategory.name_ar.ilike(f"%{search}%"),
            )
        )

    total = q.count()

    # Sort
    prod_col = func.coalesce(prod_cnt_sub.c.prod_cnt, 0)
    if sort_by == "product_count":
        primary = prod_col.desc() if sort_order == "desc" else prod_col.asc()
        q = q.order_by(primary, Subcategory.name.asc())
    else:
        primary = Subcategory.name.desc() if sort_order == "desc" else Subcategory.name.asc()
        q = q.order_by(primary)

    # Pagination
    offset = (page - 1) * limit
    rows   = q.offset(offset).limit(limit).all()

    # Pre-fetch parent categories for the returned page
    cat_ids = {sub.category_id for sub, _ in rows if sub.category_id is not None}
    cat_map: dict[int, str] = {}
    if cat_ids:
        cat_map = {
            c.id: c.name
            for c in db.query(Category).filter(Category.id.in_(cat_ids)).all()
        }

    subcategories = []
    for sub, product_count in rows:
        item: dict = {
            "id":         sub.id,
            "name":       sub.name,
            "name_ar":    sub.name_ar,
            "is_active":  bool(sub.is_active),
            "created_at": sub.created_at.isoformat() if sub.created_at else None,
            "category": {
                "id":   sub.category_id,
                "name": cat_map.get(sub.category_id) if sub.category_id is not None else None,
            },
        }
        if include_counts:
            item["product_count"] = product_count
        subcategories.append(item)

    return {
        "success": True,
        "data": {
            "subcategories": subcategories,
            "pagination":    _build_pagination(total, page, limit),
        },
    }


@router.get("/subcategories/{id}")
def get_subcategory(id: int, db: Session = Depends(get_db)):
    """Subcategory detail with nested parent category and product_count."""
    sub = db.query(Subcategory).filter(Subcategory.id == id).first()
    if not sub:
        raise _not_found()

    # Resolve parent category (name=None when orphaned / FK missing)
    cat_name: Optional[str] = None
    if sub.category_id is not None:
        parent = db.query(Category).filter(Category.id == sub.category_id).first()
        cat_name = parent.name if parent else None

    # Product count for this subcategory
    product_count = (
        db.query(func.count(Product.barcode))
        .filter(Product.subcategory_id == sub.id)
        .scalar()
    ) or 0

    return {
        "success": True,
        "data": {
            "id":            sub.id,
            "name":          sub.name,
            "name_ar":       sub.name_ar,
            "is_active":     bool(sub.is_active),
            "created_at":    sub.created_at.isoformat() if sub.created_at else None,
            "product_count": product_count,
            "category": {
                "id":   sub.category_id,
                "name": cat_name,
            },
        },
    }


@router.post("/subcategories", status_code=201)
def create_subcategory(payload: SubcategoryCreate, db: Session = Depends(get_db)):
    """Create a new subcategory under an existing category."""
    raw_name = payload.name
    if raw_name is None or not str(raw_name).strip():
        return JSONResponse(status_code=422, content={"success": False, "error": "name is required"})

    name = _normalize_name(str(raw_name))
    if not name:
        return JSONResponse(status_code=422, content={"success": False, "error": "name is required"})
    if len(name) > 255:
        return JSONResponse(status_code=422, content={"success": False, "error": "name must be 255 characters or fewer"})

    cat_id = payload.category_id
    if cat_id is None:
        return JSONResponse(status_code=422, content={"success": False, "error": "category_id is required"})
    try:
        cat_id = int(cat_id)
    except (TypeError, ValueError):
        return JSONResponse(status_code=422, content={"success": False, "error": "category_id must be a valid integer"})

    parent = db.query(Category).filter(Category.id == cat_id).first()
    if not parent:
        return JSONResponse(status_code=422, content={"success": False, "error": f"category_id {cat_id} does not exist in categories table"})

    existing = (
        db.query(Subcategory)
        .filter(Subcategory.category_id == cat_id, func.lower(Subcategory.name) == name.lower())
        .first()
    )
    if existing:
        return JSONResponse(status_code=409, content={"success": False, "error": f"Subcategory '{name}' already exists under this category."})

    name_ar: Optional[str] = None
    name_ar_raw = payload.name_ar
    if name_ar_raw is not None and str(name_ar_raw).strip():
        name_ar = _normalize_name(str(name_ar_raw))
        if len(name_ar) > 255:
            return JSONResponse(status_code=422, content={"success": False, "error": "name_ar must be 255 characters or fewer"})

    try:
        sub = Subcategory(name=name, name_ar=name_ar, category_id=cat_id, is_active=1 if payload.is_active else 0)
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return {
            "success": True,
            "message": "Subcategory created successfully",
            "data": {
                "id":            sub.id,
                "name":          sub.name,
                "name_ar":       sub.name_ar,
                "category_id":   sub.category_id,
                "category_name": parent.name,
                "is_active":     bool(sub.is_active),
                "created_at":    sub.created_at.isoformat() if sub.created_at else None,
            },
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create subcategory: {exc}")
