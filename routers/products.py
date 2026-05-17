"""
routers/products.py
===================
Product Management API — 5 endpoints:

  GET  /products              list + search + filter + pagination
  GET  /products/{barcode}    single product details
  PUT  /products/{barcode}    edit product
  DELETE /products/{barcode}  delete product
  GET  /products/filters      dropdown data for frontend
"""
import os
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, or_, func
from pydantic import BaseModel, Field

from database import get_db, engine
from models import Product, Brand, Category, Subcategory, ProductSearchIndex

router = APIRouter(prefix="", tags=["Products"])

# ── IQD rate ──────────────────────────────────────────────────────────────────

RATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "rate.json")


def get_iqd_rate() -> float:
    if not os.path.exists(RATE_FILE):
        return 1310
    try:
        with open(RATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("iqd_rate", 1310))
    except Exception:
        return 1310


# ── Barcode / ItemCode format constants ────────────────────────────────────────

BARCODE_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
VALID_BEST_SELLING_SCOPES = {"global", "category", "brand"}

# ── Protected fields — only system-controlled fields blocked here.
PROTECTED_FIELDS = {"last_synced_sap", "created_at"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def price_iqd(price) -> float:
    """Convert USD price to IQD integer."""
    if price is None:
        return 0.0
    return round(float(price) * get_iqd_rate(), 0)


def product_status(available_qty) -> str:
    if available_qty is None:
        return "out_of_stock"
    return "in_stock" if available_qty > 0 else "out_of_stock"


def serialize_product(row) -> dict:
    """Convert a SQLAlchemy result row (or ORM object) to the frontend dict."""
    if hasattr(row, "barcode"):
        barcode = row.barcode
        item_code = row.item_code
        item_name = row.item_name
        description = row.description
        image_url = row.image_url
        skin_type = row.skin_type
        concerns = row.concerns or []
        tags = row.tags or []
        price = float(row.price) if row.price is not None else 0.0
        available_qty = row.available_qty or 0
        is_best_selling = row.is_best_selling or 0
        sales_rank = row.sales_rank
        sap_product_id = row.sap_product_id
        last_synced_sap = row.last_synced_sap
        brand_id = row.brand_id
        category_id = row.category_id
        subcategory_id = row.subcategory_id
        created_at = row.created_at
        updated_at = row.updated_at
        brand_name = getattr(row, "brand_name", None)
        category_name = getattr(row, "category_name", None)
        subcategory_name = getattr(row, "subcategory_name", None)
    else:
        barcode = row.barcode
        item_code = row.item_code
        item_name = row.item_name
        description = row.description
        image_url = row.image_url
        skin_type = row.skin_type
        concerns = row.concerns or []
        tags = row.tags or []
        price = float(row.price) if row.price is not None else 0.0
        available_qty = row.available_qty or 0
        is_best_selling = row.is_best_selling or 0
        sales_rank = row.sales_rank
        sap_product_id = row.sap_product_id
        last_synced_sap = row.last_synced_sap
        brand_id = row.brand_id
        category_id = row.category_id
        subcategory_id = row.subcategory_id
        created_at = row.created_at
        updated_at = row.updated_at
        brand_name = getattr(row, "brand_name", None)
        category_name = getattr(row, "category_name", None)
        subcategory_name = getattr(row, "subcategory_name", None)

    def _resolve(obj_id, name_from_row, model_cls, name_key="name"):
        if name_from_row:
            return {"id": obj_id, "name": name_from_row}
        if obj_id is not None:
            db_obj = model_cls.__func__ if callable(getattr(model_cls, "__func__", None)) else None
            from database import SessionLocal as _sl
            local_db = _sl()
            try:
                entity = local_db.query(model_cls).filter(model_cls.id == obj_id).first()
                return {"id": obj_id, "name": getattr(entity, name_key, "Unknown")} if entity else {"id": obj_id, "name": None}
            except Exception:
                return {"id": obj_id, "name": None}
            finally:
                local_db.close()
        return {"id": None, "name": None}

    from database import SessionLocal as _sl
    _db = _sl()
    try:
        brand_ent = _db.query(Brand).filter(Brand.id == brand_id).first() if brand_id else None
        cat_ent = _db.query(Category).filter(Category.id == category_id).first() if category_id else None
        sub_ent = _db.query(Subcategory).filter(Subcategory.id == subcategory_id).first() if subcategory_id else None
    except Exception:
        brand_ent = cat_ent = sub_ent = None
    finally:
        _db.close()

    return {
        "barcode": barcode,
        "item_code": item_code,
        "item_name": item_name,
        "description": description,
        "image_url": image_url,
        "skin_type": skin_type,
        "concerns": concerns,
        "tags": tags,
        "price": price,
        "price_iqd": price_iqd(price),
        "available_qty": available_qty,
        "status": product_status(available_qty),
        "is_best_selling": is_best_selling,
        "best_selling_scope": getattr(row, "best_selling_scope", None) if not hasattr(row, "best_selling_scope") else row.best_selling_scope if hasattr(row, "best_selling_scope") else None,
        "sales_rank": sales_rank,
        "sap_product_id": sap_product_id,
        "last_synced_sap": last_synced_sap.isoformat() if last_synced_sap else None,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "brand": {
            "id": brand_id,
            "name": brand_name or (brand_ent.name if brand_ent else None),
        },
        "category": {
            "id": category_id,
            "name": category_name or (cat_ent.name if cat_ent else None),
        },
        "subcategory": {
            "id": subcategory_id,
            "name": subcategory_name or (sub_ent.name if sub_ent else None),
        },
    }


# ── VALID SORT KEYS ────────────────────────────────────────────────────────────

VALID_SORT_COLUMNS = {
    "name_asc":    ("item_name", "ASC"),
    "name_desc":   ("item_name", "DESC"),
    "price_asc":   ("price",     "ASC"),
    "price_desc":  ("price",     "DESC"),
    "stock_asc":   ("available_qty", "ASC"),
    "stock_desc":  ("available_qty", "DESC"),
    "created_asc": ("created_at", "ASC"),
    "created_desc":("created_at", "DESC"),
}


# ═══════════════════════════════════════════════════════════════════════════════
# API 5  —  GET /products/filters
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/products/filters")
def get_filters(db: Session = Depends(get_db)):
    """Dropdown data for frontend filter menus."""
    brands = (
        db.query(Brand)
        .filter(Brand.is_active == 1)
        .order_by(Brand.name.asc())
        .all()
    )
    categories = (
        db.query(Category)
        .filter(Category.is_active == 1)
        .order_by(Category.name.asc())
        .all()
    )
    subcategories = (
        db.query(Subcategory)
        .filter(Subcategory.is_active == 1)
        .order_by(Subcategory.name.asc())
        .all()
    )
    return {
        "success": True,
        "data": {
            "brands": [{"id": b.id, "name": b.name} for b in brands],
            "categories": [{"id": c.id, "name": c.name} for c in categories],
            "subcategories": [
                {"id": s.id, "category_id": s.category_id, "name": s.name}
                for s in subcategories
            ],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# API 1  —  GET /products
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/products")
def list_products(
    q: Optional[str] = None,
    brand_id: Optional[int] = None,
    category_id: Optional[int] = None,
    subcategory_id: Optional[int] = None,
    is_best_selling: Optional[int] = None,
    in_stock: Optional[bool] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    page: int = 1,
    limit: int = 10,
    sort_by: Optional[str] = "created_desc",
    db: Session = Depends(get_db),
):
    """Product Inventory page — list, search, filter, paginate."""
    cap = 500
    if limit < 1:
        limit = 10
    if limit > cap:
        limit = cap
    if page < 1:
        page = 1
    offset = (page - 1) * limit

    sort_col, sort_dir = VALID_SORT_COLUMNS.get(
        sort_by or "created_desc", ("created_at", "DESC")
    )
    needs_search_index = bool(q)

    select_cols = [
        Product.barcode, Product.item_code, Product.item_name,
        Product.description, Product.image_url, Product.skin_type,
        Product.concerns, Product.tags, Product.price, Product.available_qty,
        Product.is_best_selling, Product.best_selling_scope, Product.sales_rank,
        Product.sap_product_id, Product.last_synced_sap,
        Product.created_at, Product.updated_at,
        Product.brand_id, Product.category_id, Product.subcategory_id,
        Brand.name.label("brand_name"),
        Category.name.label("category_name"),
        Subcategory.name.label("subcategory_name"),
    ]

    query = db.query(*select_cols).join(
        Brand,    Brand.id      == Product.brand_id,      isouter=True
    ).join(
        Category, Category.id   == Product.category_id,   isouter=True
    ).join(
        Subcategory, Subcategory.id == Product.subcategory_id, isouter=True
    )

    if needs_search_index:
        query = query.join(
            ProductSearchIndex,
            ProductSearchIndex.product_id == Product.barcode,
            isouter=True,
        )
        q_like = f"%{q.strip().lower()}%"
        search_condition = or_(
            ProductSearchIndex.search_text.ilike(q_like),
            func.to_tsvector("english", ProductSearchIndex.search_text).op("@@")(
                func.plainto_tsquery("english", q.strip())
            ),
        )
        query = query.filter(search_condition)

    if brand_id is not None:
        query = query.filter(Product.brand_id == brand_id)
    if category_id is not None:
        query = query.filter(Product.category_id == category_id)
    if subcategory_id is not None:
        query = query.filter(Product.subcategory_id == subcategory_id)
    if is_best_selling is not None:
        query = query.filter(Product.is_best_selling == is_best_selling)
    if in_stock:
        query = query.filter(Product.available_qty > 0)
    if min_price is not None:
        query = query.filter(Product.price >= min_price)
    if max_price is not None:
        query = query.filter(Product.price <= max_price)

    total_query = query.statement.with_only_columns(func.count()).order_by(None)
    total = db.execute(total_query).scalar() or 0

    col_map = {
        "item_name":    Product.item_name,
        "price":        Product.price,
        "available_qty": Product.available_qty,
        "created_at":   Product.created_at,
    }
    sort_attr = col_map.get(sort_col, Product.created_at)

    if sort_dir.upper() == "DESC":
        query = query.order_by(sort_attr.desc())
    else:
        query = query.order_by(sort_attr.asc())

    rows = query.offset(offset).limit(limit).all()
    products = [serialize_product(r) for r in rows]
    total_pages = (total + limit - 1) // limit if limit > 0 else 0

    return {
        "success": True,
        "data": {
            "products": products,
            "pagination": {
                "total": total, "page": page, "limit": limit,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1,
            },
            "filters_applied": {
                "q": q, "brand_id": brand_id, "category_id": category_id,
                "subcategory_id": subcategory_id, "is_best_selling": is_best_selling,
                "in_stock": in_stock, "sort_by": sort_by,
            },
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# API 2  —  GET /products/{barcode}
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/products/{barcode}")
def get_product(barcode: str, db: Session = Depends(get_db)):
    """Single product full details page."""
    product = (
        db.query(Product)
        .join(Brand,       Brand.id       == Product.brand_id,      isouter=True)
        .join(Category,    Category.id    == Product.category_id,   isouter=True)
        .join(Subcategory, Subcategory.id == Product.subcategory_id, isouter=True)
        .filter(Product.barcode == barcode)
        .first()
    )

    if not product:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "Product not found", "barcode": barcode},
        )

    brand_ent = db.query(Brand).filter(Brand.id == product.brand_id).first()
    cat_ent   = db.query(Category).filter(Category.id == product.category_id).first()
    sub_ent   = db.query(Subcategory).filter(Subcategory.id == product.subcategory_id).first()

    brand_name       = brand_ent.name       if brand_ent else None
    category_name    = cat_ent.name         if cat_ent   else None
    subcategory_name = sub_ent.name         if sub_ent   else None
    brand_name_ar    = brand_ent.name_ar    if brand_ent else None
    cat_name_ar      = cat_ent.name_ar      if cat_ent   else None
    sub_name_ar      = sub_ent.name_ar      if sub_ent   else None

    price = float(product.price) if product.price is not None else 0.0

    data = {
        "barcode":          product.barcode,
        "item_code":        product.item_code,
        "sap_product_id":   product.sap_product_id,
        "item_name":        product.item_name,
        "description":      product.description,
        "image_url":        product.image_url,
        "skin_type":        product.skin_type,
        "concerns":         product.concerns or [],
        "tags":             product.tags or [],
        "price":            price,
        "price_iqd":        price_iqd(price),
        "available_qty":    product.available_qty or 0,
        "status":           product_status(product.available_qty),
        "is_best_selling":  product.is_best_selling or 0,
        "best_selling_scope": product.best_selling_scope,
        "sales_rank":       product.sales_rank,
        "last_synced_sap":  product.last_synced_sap.isoformat() if product.last_synced_sap else None,
        "created_at":       product.created_at.isoformat() if product.created_at else None,
        "updated_at":       product.updated_at.isoformat() if product.updated_at else None,
        "brand": {
            "id": product.brand_id, "name": brand_name, "name_ar": brand_name_ar,
        },
        "category": {
            "id": product.category_id, "name": category_name, "name_ar": cat_name_ar,
        },
        "subcategory": {
            "id": product.subcategory_id, "name": subcategory_name, "name_ar": sub_name_ar,
        },
    }
    return {"success": True, "data": data}


# ═══════════════════════════════════════════════════════════════════════════════
# API 3  —  PUT /products/{barcode}
# ═══════════════════════════════════════════════════════════════════════════════

@router.put("/products/{barcode}")
def update_product(
    barcode: str,
    payload: dict,
    db: Session = Depends(get_db),
):
    """Edit an existing product (partial update).

    barcode and item_code are now editable with strict duplicate validation.
    last_synced_sap and created_at remain fully protected.
    All barcode changes are cascaded into productsearchindex within one session.
    """
    old_barcode = barcode
    old_item_code = None
    updated_barcode = False
    updated_item_code = False
    warnings = []

    product = db.query(Product).filter(Product.barcode == barcode).first()

    if not product:
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": "Product not found"},
        )

    # ── Reject attempts to touch system-controlled fields ──
    forbidden = PROTECTED_FIELDS & set(payload.keys())
    if forbidden:
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": (
                    f"Cannot modify protected field(s): {', '.join(sorted(forbidden))}. "
                    "These fields are controlled by the system."
                ),
            },
        )

    # ── BARCODE — format validation + duplicate check ───────────────────────────
    if "barcode" in payload:
        new_barcode = str(payload["barcode"]).strip()

        if not new_barcode:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "barcode must be 6–32 alphanumeric characters"},
            )
        if len(new_barcode) < 6 or len(new_barcode) > 32:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "barcode must be 6–32 alphanumeric characters"},
            )
        if not BARCODE_PATTERN.match(new_barcode):
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "barcode must be 6–32 alphanumeric characters"},
            )

        if new_barcode != old_barcode:
            dup = (
                db.query(Product)
                .filter(
                    Product.barcode == new_barcode,
                    Product.barcode != old_barcode,
                )
                .first()
            )
            if dup:
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": f"Barcode '{new_barcode}' already exists. Each barcode must be unique.",
                    },
                )

            logging.warning(
                "PK_CHANGE: barcode changed '%s' -> '%s'",
                old_barcode, new_barcode,
            )
            updated_barcode = True

    # ── ITEM_CODE — format validation + duplicate check ─────────────────────────
    if "item_code" in payload:
        old_item_code = product.item_code
        new_item_code = str(payload["item_code"]).strip()

        if not new_item_code:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "item_code is required"},
            )
        if len(new_item_code) < 1 or len(new_item_code) > 50:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "item_code must be 1–50 characters"},
            )

        if new_item_code != old_item_code:
            dup = (
                db.query(Product)
                .filter(
                    Product.item_code == new_item_code,
                    Product.barcode    != old_barcode,
                )
                .first()
            )
            if dup:
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "error": f"item_code '{new_item_code}' already used by another product.",
                    },
                )

            updated_item_code = True

    # ── FK validations ───────────────────────────────────────────────────────────
    for field, model_cls in [
        ("brand_id",       Brand),
        ("category_id",    Category),
        ("subcategory_id", Subcategory),
    ]:
        if field in payload and payload[field] is not None:
            exists = db.query(model_cls).filter(model_cls.id == payload[field]).first()
            if not exists:
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": (
                            f"{field} {payload[field]} does not exist in "
                            f"{model_cls.__tablename__} table"
                        ),
                    },
                )

    # ── Range / enum validations ─────────────────────────────────────────────────
    if "price" in payload and payload["price"] is not None:
        if float(payload["price"]) < 0:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "price must be >= 0"},
            )

    if "available_qty" in payload and payload["available_qty"] is not None:
        if int(payload["available_qty"]) < 0:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "available_qty must be >= 0"},
            )

    if "sales_rank" in payload and payload["sales_rank"] is not None:
        if int(payload["sales_rank"]) < 1:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "sales_rank must be >= 1"},
            )

    if "is_best_selling" in payload and payload["is_best_selling"] is not None:
        val = int(payload["is_best_selling"])
        if val not in (0, 1):
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "is_best_selling must be 0 or 1"},
            )

    if "best_selling_scope" in payload and payload["best_selling_scope"] is not None:
        scope = str(payload["best_selling_scope"]).strip().lower()
        if scope and scope not in VALID_BEST_SELLING_SCOPES:
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": (
                        f"best_selling_scope '{scope}' is invalid. "
                        f"Allowed values: {', '.join(sorted(VALID_BEST_SELLING_SCOPES))}."
                    ),
                },
            )

    # ── ALL validations pass — apply updates within a single flush/commit batch ──
    # Use manual try/except + commit instead of db.begin() context manager
    # because FastAPI's get_db dependency autobegins the session, and calling
    # db.begin() as a nested context raises "A transaction is already begun."
    # All flush()/commit() calls here share the outer autobegun transaction.
    try:
        # STEP 1: Cascade productsearchindex PK rename FIRST (before the PK itself
        #         is changed on the products row, so the index mirrors stay consistent).
        if updated_barcode:
            new_bc = str(payload["barcode"]).strip()
            db.execute(
                text(
                    "UPDATE productsearchindex "
                    "SET product_id = :new_barcode, barcode = :new_barcode "
                    "WHERE product_id = :old_barcode"
                ),
                {"new_barcode": new_bc, "old_barcode": old_barcode},
            )

        # STEP 2: Apply all field updates on the Product row
        if "item_code" in payload:
            product.item_code = str(payload["item_code"]).strip()
        if updated_barcode:
            product.barcode = str(payload["barcode"]).strip()

        updatable = {
            "item_name", "description", "image_url",
            "brand_id", "category_id", "subcategory_id",
            "skin_type", "concerns", "tags",
            "price", "available_qty",
            "is_best_selling", "best_selling_scope", "sales_rank",
            "sap_product_id",
        }
        for key in updatable:
            if key in payload:
                setattr(product, key, payload[key])

        db.flush()   # push product row changes into the transaction

        # STEP 3: Rebuild productsearchindex search columns from the updated Product
        effective_barcode = str(product.barcode)
        brand_ent = db.query(Brand).filter(Brand.id == product.brand_id).first()
        cat_ent   = db.query(Category).filter(Category.id == product.category_id).first()
        sub_ent   = db.query(Subcategory).filter(Subcategory.id == product.subcategory_id).first()

        new_brand_name  = brand_ent.name  if brand_ent else None
        new_cat_name    = cat_ent.name    if cat_ent   else None
        new_sub_name    = sub_ent.name    if sub_ent   else None

        search_text = " ".join(filter(None, [
            str(product.item_code or ""),
            str(new_brand_name or ""),
            str(new_cat_name or ""),
            str(new_sub_name or ""),
            str(product.item_name or ""),
        ])).lower()

        si = (
            db.query(ProductSearchIndex)
            .filter(ProductSearchIndex.product_id == effective_barcode)
            .first()
        )
        if si:
            si.product_id       = effective_barcode
            si.item_code        = product.item_code
            si.barcode          = effective_barcode
            si.item_name        = product.item_name
            si.brand_name       = new_brand_name
            si.category_name    = new_cat_name
            si.subcategory_name = new_sub_name
            si.search_text      = search_text
        else:
            si = ProductSearchIndex(
                product_id       = effective_barcode,
                item_code        = product.item_code,
                barcode          = effective_barcode,
                item_name        = product.item_name,
                brand_name       = new_brand_name,
                category_name    = new_cat_name,
                subcategory_name = new_sub_name,
                search_text      = search_text,
            )
            db.add(si)

        db.flush()

    except Exception as exc:
        db.rollback()
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Update failed, transaction rolled back: {str(exc)}",
            },
        )

    # Commit the entire outer transaction (product + search_index as one unit)
    db.commit()

    # ── Warnings ────────────────────────────────────────────────────────────────
    if updated_barcode:
        warnings.append(
            f"Primary key (barcode) was changed from '{old_barcode}' to "
            f"'{product.barcode}'. Any external system referencing the old "
            "barcode must be updated."
        )
    if updated_item_code:
        warnings.append(
            f"item_code was changed from '{old_item_code}' to "
            f"'{product.item_code}'."
        )

    # ── Build response ──────────────────────────────────────────────────────────
    price = float(product.price) if product.price is not None else 0.0
    response = {
        "success": True,
        "message": "Product updated successfully",
        "data": {
            "barcode":         effective_barcode,
            "item_code":       product.item_code,
            "item_name":       product.item_name,
            "description":     product.description,
            "image_url":       product.image_url,
            "skin_type":       product.skin_type,
            "concerns":        product.concerns or [],
            "tags":            product.tags or [],
            "price":           price,
            "price_iqd":       price_iqd(price),
            "available_qty":   product.available_qty,
            "is_best_selling": product.is_best_selling or 0,
            "sales_rank":      product.sales_rank,
            "sap_product_id":  product.sap_product_id,
            "brand": {
                "id": product.brand_id, "name": new_brand_name,
            },
            "category": {
                "id": product.category_id, "name": new_cat_name,
            },
            "subcategory": {
                "id": product.subcategory_id, "name": new_sub_name,
            },
            "updated_at": product.updated_at.isoformat() if product.updated_at else None,
        },
    }
    if warnings:
        response["warnings"] = warnings

    return response


# ═══════════════════════════════════════════════════════════════════════════════
# API 4  —  DELETE /products/{barcode}
# ═══════════════════════════════════════════════════════════════════════════════

@router.delete("/products/{barcode}")
def delete_product(barcode: str, db: Session = Depends(get_db)):
    """Delete a product and its search index entry."""
    product = db.query(Product).filter(Product.barcode == barcode).first()

    if not product:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "Product not found"},
        )

    item_name = product.item_name

    db.query(ProductSearchIndex).filter(
        ProductSearchIndex.product_id == barcode
    ).delete(synchronize_session=False)

    db.query(Product).filter(Product.barcode == barcode).delete(synchronize_session=False)
    db.commit()

    return {
        "success": True,
        "message": "Product deleted successfully",
        "barcode": barcode,
        "item_name": item_name,
    }
