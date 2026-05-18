"""
routers/categories.py
=====================
POST /categories  —  Create a new category from the admin dashboard.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from pydantic import BaseModel

from database import get_db
from models import Category

router = APIRouter(prefix="", tags=["Categories"])


# ── Pydantic Request Schema ────────────────────────────────────────────────────

class CategoryCreate(BaseModel):
    name: str
    name_ar: Optional[str] = None
    is_active: Optional[bool] = True


# ── Helper ──────────────────────────────────────────────────────────────────────

def _normalize_name(value: str) -> str:
    return value.strip()


# ── Endpoints ───────────────────────────────────────────────────────────────────

@router.get("/categories")
def list_categories(
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    page: int = 1,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """List all categories with search, active filter, and pagination."""
    query = db.query(Category)
    if is_active is not None:
        query = query.filter(Category.is_active == (1 if is_active else 0))
    if search:
        query = query.filter(
            or_(
                Category.name.ilike(f"%{search}%"),
                Category.name_ar.ilike(f"%{search}%")
            )
        )
    
    total = query.count()
    offset = (page - 1) * limit
    items = query.order_by(Category.name.asc()).offset(offset).limit(limit).all()
    
    return {
        "success": True,
        "data": [
            {
                "id": c.id,
                "name": c.name,
                "name_ar": c.name_ar,
                "is_active": bool(c.is_active),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in items
        ],
        "pagination": {
            "total": total,
            "page": page,
            "limit": limit
        }
    }


@router.get("/categories/{id}")
def get_category(id: int, db: Session = Depends(get_db)):
    """Get a single category by ID."""
    category = db.query(Category).filter(Category.id == id).first()
    if not category:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "Category not found"}
        )
    return {
        "success": True,
        "data": {
            "id": category.id,
            "name": category.name,
            "name_ar": category.name_ar,
            "is_active": bool(category.is_active),
            "created_at": category.created_at.isoformat() if category.created_at else None,
        }
    }


@router.post("/categories", status_code=201)
def create_category(payload: CategoryCreate, db: Session = Depends(get_db)):
    """Create a new category with case-insensitive duplicate detection."""
    # ── Required field guard ──
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
            content={"success": False, "error": "name must be 100 characters or fewer"},
        )

    # ── Case-insensitive duplicate check on `name` ──
    existing = (
        db.query(Category)
        .filter(func.lower(Category.name) == name.lower())
        .first()
    )
    if existing:
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": f"Category '{name}' already exists."},
        )

    # ── Case-insensitive duplicate check on `name_ar` (if provided) ──
    name_ar_raw = payload.name_ar
    if name_ar_raw is not None and str(name_ar_raw).strip():
        name_ar = _normalize_name(str(name_ar_raw))
        if len(name_ar) > 255:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "name_ar must be 100 characters or fewer"},
            )
        existing_ar = (
            db.query(Category)
            .filter(func.lower(Category.name_ar) == name_ar.lower())
            .first()
        )
        if existing_ar:
            return JSONResponse(
                status_code=409,
                content={"success": False, "error": f"Category with Arabic name '{name_ar}' already exists."},
            )
    else:
        name_ar = None

    try:
        category = Category(
            name=name,
            name_ar=name_ar,
            is_active=1 if payload.is_active else 0,
        )
        db.add(category)
        db.commit()
        db.refresh(category)

        return {
            "success": True,
            "message": "Category created successfully",
            "data": {
                "id": category.id,
                "name": category.name,
                "name_ar": category.name_ar,
                "is_active": bool(category.is_active),
                "created_at": category.created_at.isoformat() if category.created_at else None,
            },
        }

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create category: {str(exc)}",
        )
