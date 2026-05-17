"""
routers/brands.py
=================
POST /brands  —  Create a new brand from the admin dashboard.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from pydantic import BaseModel

from database import get_db
from models import Brand

router = APIRouter(prefix="", tags=["Brands"])


# ── Pydantic Request Schema ────────────────────────────────────────────────────

class BrandCreate(BaseModel):
    name: str
    name_ar: Optional[str] = None
    is_active: Optional[bool] = True


# ── Helper ──────────────────────────────────────────────────────────────────────

def _normalize_name(value: str) -> str:
    return value.strip()


# ── Endpoints ───────────────────────────────────────────────────────────────────

@router.get("/brands")
def list_brands(
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    page: int = 1,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """List all brands with search, active filter, and pagination."""
    query = db.query(Brand)
    if is_active is not None:
        query = query.filter(Brand.is_active == (1 if is_active else 0))
    if search:
        query = query.filter(
            or_(
                Brand.name.ilike(f"%{search}%"),
                Brand.name_ar.ilike(f"%{search}%")
            )
        )
    
    total = query.count()
    offset = (page - 1) * limit
    items = query.order_by(Brand.name.asc()).offset(offset).limit(limit).all()
    
    return {
        "success": True,
        "data": [
            {
                "id": b.id,
                "name": b.name,
                "name_ar": b.name_ar,
                "is_active": bool(b.is_active),
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in items
        ],
        "pagination": {
            "total": total,
            "page": page,
            "limit": limit
        }
    }


@router.get("/brands/{id}")
def get_brand(id: int, db: Session = Depends(get_db)):
    """Get a single brand by ID."""
    brand = db.query(Brand).filter(Brand.id == id).first()
    if not brand:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "Brand not found"}
        )
    return {
        "success": True,
        "data": {
            "id": brand.id,
            "name": brand.name,
            "name_ar": brand.name_ar,
            "is_active": bool(brand.is_active),
            "created_at": brand.created_at.isoformat() if brand.created_at else None,
        }
    }


@router.post("/brands", status_code=201)
def create_brand(payload: BrandCreate, db: Session = Depends(get_db)):
    """Create a new brand with case-insensitive duplicate detection."""
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
        db.query(Brand)
        .filter(func.lower(Brand.name) == name.lower())
        .first()
    )
    if existing:
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": f"Brand '{name}' already exists."},
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
            db.query(Brand)
            .filter(func.lower(Brand.name_ar) == name_ar.lower())
            .first()
        )
        if existing_ar:
            return JSONResponse(
                status_code=409,
                content={"success": False, "error": f"Brand with Arabic name '{name_ar}' already exists."},
            )
    else:
        name_ar = None

    try:
        brand = Brand(
            name=name,
            name_ar=name_ar,
            is_active=1 if payload.is_active else 0,
        )
        db.add(brand)
        db.commit()
        db.refresh(brand)

        return {
            "success": True,
            "message": "Brand created successfully",
            "data": {
                "id": brand.id,
                "name": brand.name,
                "name_ar": brand.name_ar,
                "is_active": bool(brand.is_active),
                "created_at": brand.created_at.isoformat() if brand.created_at else None,
            },
        }

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create brand: {str(exc)}",
        )
