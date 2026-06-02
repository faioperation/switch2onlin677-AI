"""
routers/subcategories.py
========================
POST /subcategories  —  Create a new subcategory from the admin dashboard.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from pydantic import BaseModel

from database import get_db
from models import Subcategory, Category

router = APIRouter(prefix="", tags=["Subcategories"])


# ── Pydantic Request Schema ────────────────────────────────────────────────────

class SubcategoryCreate(BaseModel):
    name: str
    name_ar: Optional[str] = None
    category_id: Optional[int] = None
    is_active: Optional[bool] = True


# ── Helper ──────────────────────────────────────────────────────────────────────

def _normalize_name(value: str) -> str:
    return value.strip()


# ── Endpoints ───────────────────────────────────────────────────────────────────

@router.get("/subcategories")
def list_subcategories(
    category_id: Optional[int] = None,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    page: int = 1,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """List subcategories with parent category filtering, search, and pagination."""
    query = db.query(Subcategory)
    if category_id is not None:
        query = query.filter(Subcategory.category_id == category_id)
    if is_active is not None:
        query = query.filter(Subcategory.is_active == (1 if is_active else 0))
    if search:
        query = query.filter(
            or_(
                Subcategory.name.ilike(f"%{search}%"),
                Subcategory.name_ar.ilike(f"%{search}%")
            )
        )
        
    total = query.count()
    offset = (page - 1) * limit
    items = query.order_by(Subcategory.name.asc()).offset(offset).limit(limit).all()
    
    # Pre-fetch parent categories for the list to return category_name
    category_ids = {s.category_id for s in items if s.category_id is not None}
    cats = {c.id: c.name for c in db.query(Category).filter(Category.id.in_(category_ids)).all()} if category_ids else {}
    
    return {
        "success": True,
        "data": [
            {
                "id": s.id,
                "name": s.name,
                "name_ar": s.name_ar,
                "category_id": s.category_id,
                "category_name": cats.get(s.category_id),
                "is_active": bool(s.is_active),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in items
        ],
        "pagination": {
            "total": total,
            "page": page,
            "limit": limit
        }
    }


@router.get("/subcategories/{id}")
def get_subcategory(id: int, db: Session = Depends(get_db)):
    """Get a single subcategory by ID."""
    subcategory = db.query(Subcategory).filter(Subcategory.id == id).first()
    if not subcategory:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "Subcategory not found"}
        )
    cat_name = None
    if subcategory.category_id:
        parent = db.query(Category).filter(Category.id == subcategory.category_id).first()
        if parent:
            cat_name = parent.name
            
    return {
        "success": True,
        "data": {
            "id": subcategory.id,
            "name": subcategory.name,
            "name_ar": subcategory.name_ar,
            "category_id": subcategory.category_id,
            "category_name": cat_name,
            "is_active": bool(subcategory.is_active),
            "created_at": subcategory.created_at.isoformat() if subcategory.created_at else None,
        }
    }


@router.post("/subcategories", status_code=201)
def create_subcategory(payload: SubcategoryCreate, db: Session = Depends(get_db)):
    """Create a new subcategory under an existing category.

    Duplicate check is scoped to the parent category — the same subcategory
    name can coexist under different categories.
    """
    # ── Required field guard: name ──
    raw_name = payload.name
    if raw_name is None or not str(raw_name).strip():
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "name is required"},
        )

    name = _normalize_name(str(raw_name))

    if len(name) == 0:
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "name is required"},
        )

    if len(name) > 255:
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "name must be 255 characters or fewer"},
        )

    # ── Required field guard: category_id ──
    category_id = payload.category_id
    if category_id is None:
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "category_id is required"},
        )

    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "category_id must be a valid integer"},
        )

    # ── Loose coupling: validate category exists via SELECT (no FK constraint) ──
    parent = (
        db.query(Category)
        .filter(Category.id == category_id)
        .first()
    )
    if not parent:
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": f"category_id {category_id} does not exist in categories table"},
        )

    # ── Duplicate check scoped to the parent category ──
    existing = (
        db.query(Subcategory)
        .filter(
            Subcategory.category_id == category_id,
            func.lower(Subcategory.name) == name.lower(),
        )
        .first()
    )
    if existing:
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": f"Subcategory '{name}' already exists under this category."},
        )

    # ── Optional name_ar ──
    name_ar_raw = payload.name_ar
    if name_ar_raw is not None and str(name_ar_raw).strip():
        name_ar = _normalize_name(str(name_ar_raw))
        if len(name_ar) > 255:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "name_ar must be 255 characters or fewer"},
            )
    else:
        name_ar = None

    try:
        subcategory = Subcategory(
            name=name,
            name_ar=name_ar,
            category_id=category_id,
            is_active=1 if payload.is_active else 0,
        )
        db.add(subcategory)
        db.commit()
        db.refresh(subcategory)

        parent_name = parent.name

        return {
            "success": True,
            "message": "Subcategory created successfully",
            "data": {
                "id": subcategory.id,
                "name": subcategory.name,
                "name_ar": subcategory.name_ar,
                "category_id": subcategory.category_id,
                "category_name": parent_name,
                "is_active": bool(subcategory.is_active),
                "created_at": subcategory.created_at.isoformat() if subcategory.created_at else None,
            },
        }

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create subcategory: {str(exc)}",
        )
