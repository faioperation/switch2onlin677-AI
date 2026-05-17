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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text, or_, func

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
        # ORM object
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
        # Row proxy / Row object from SQLAlchemy Core query
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

    # Inline session for name resolution when the row's name strings are absent
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
    """
    Product Inventory page — list, search, filter, paginate.

    Never loads all rows into memory. Every read path uses LIMIT/OFFSET.
    Total count is fetched in a separate COUNT(*) query with the same filters.
    """
    # ── Clamp & validate pagination ──
    cap = 500
    if limit < 1:
        limit = 10
    if limit > cap:
        limit = cap
    if page < 1:
        page = 1
    offset = (page - 1) * limit

    # ── Resolve sort clause ──
    sort_col, sort_dir = VALID_SORT_COLUMNS.get(
        sort_by or "created_desc", ("created_at", "DESC")
    )

    # ── Whether we need the productsearchindex JOIN ──
    needs_search_index = bool(q)

    # ── Build base SELECT ──
    select_cols = [
        Product.barcode,
        Product.item_code,
        Product.item_name,
        Product.description,
        Product.image_url,
        Product.skin_type,
        Product.concerns,
        Product.tags,
        Product.price,
        Product.available_qty,
        Product.is_best_selling,
        Product.best_selling_scope,
        Product.sales_rank,
        Product.sap_product_id,
        Product.last_synced_sap,
        Product.created_at,
        Product.updated_at,
        Product.brand_id,
        Product.category_id,
        Product.subcategory_id,
        Brand.name.label("brand_name"),
        Category.name.label("category_name"),
        Subcategory.name.label("subcategory_name"),
    ]

    query = db.query(*select_cols).join(
        Brand, Brand.id == Product.brand_id, isouter=True
    ).join(
        Category, Category.id == Product.category_id, isouter=True
    ).join(
        Subcategory, Subcategory.id == Product.subcategory_id, isouter=True
    )

    # ── Search filter ──
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

    # ── Filters ──
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

    # ── Count first (same WHERE conditions, no LIMIT) ──
    total_query = query.statement.with_only_columns(func.count()).order_by(None)
    total = db.execute(total_query).scalar() or 0

    # ── Apply sort + pagination ──
    sort_col_attr = getattr(Product, sort_col.replace(".", "_").replace("(", "").replace(")", ""))
    # Map sort column names to actual SQLAlchemy column attributes
    col_map = {
        "item_name": Product.item_name,
        "price":     Product.price,
        "available_qty": Product.available_qty,
        "created_at": Product.created_at,
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
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1,
            },
            "filters_applied": {
                "q": q,
                "brand_id": brand_id,
                "category_id": category_id,
                "subcategory_id": subcategory_id,
                "is_best_selling": is_best_selling,
                "in_stock": in_stock,
                "sort_by": sort_by,
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
        .join(Brand,    Brand.id    == Product.brand_id,    isouter=True)
        .join(Category, Category.id == Product.category_id, isouter=True)
        .join(Subcategory, Subcategory.id == Product.subcategory_id, isouter=True)
        .filter(Product.barcode == barcode)
        .first()
    )

    if not product:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "Product not found", "barcode": barcode},
        )

    # Names resolved via a fresh sub-query (Product has no FK relationship attributes)
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
            "id":    product.brand_id,
            "name":  brand_name,
            "name_ar": brand_name_ar,
        },
        "category": {
            "id":    product.category_id,
            "name":  category_name,
            "name_ar": cat_name_ar,
        },
        "subcategory": {
            "id":       product.subcategory_id,
            "name":     subcategory_name,
            "name_ar":  sub_name_ar,
        },
    }

    return {"success": True, "data": data}


# ═══════════════════════════════════════════════════════════════════════════════
# API 3  —  PUT /products/{barcode}
# ═══════════════════════════════════════════════════════════════════════════════

PROTECTED_FIELDS = {"barcode", "item_code", "last_synced_sap", "created_at"}


@router.put("/products/{barcode}")
def update_product(
    barcode: str,
    payload: dict,
    db: Session = Depends(get_db),
):
    """Edit an existing product. Partial update — only sent fields are modified."""
    product = db.query(Product).filter(Product.barcode == barcode).first()

    if not product:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "Product not found"},
        )

    # ── Reject attempts to touch immutable fields ──
    forbidden = PROTECTED_FIELDS & set(payload.keys())
    if forbidden:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot modify protected field(s): {', '.join(sorted(forbidden))}. "
                "These fields are controlled by the system."
            ),
        )

    # ── Validate FK references before applying ──
    if "brand_id" in payload and payload["brand_id"] is not None:
        brand_exists = db.query(Brand).filter(Brand.id == payload["brand_id"]).first()
        if not brand_exists:
            raise HTTPException(
                status_code=422,
                detail=f"brand_id {payload['brand_id']} does not exist in brands table",
            )

    if "category_id" in payload and payload["category_id"] is not None:
        cat_exists = db.query(Category).filter(Category.id == payload["category_id"]).first()
        if not cat_exists:
            raise HTTPException(
                status_code=422,
                detail=f"category_id {payload['category_id']} does not exist in categories table",
            )

    if "subcategory_id" in payload and payload["subcategory_id"] is not None:
        sub_exists = (
            db.query(Subcategory)
            .filter(Subcategory.id == payload["subcategory_id"])
            .first()
        )
        if not sub_exists:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"subcategory_id {payload['subcategory_id']} "
                    "does not exist in subcategories table"
                ),
            )

    # ── Numeric validation ──
    if "price" in payload and payload["price"] is not None:
        if float(payload["price"]) < 0:
            raise HTTPException(
                status_code=422,
                detail="price must be >= 0",
            )
    if "available_qty" in payload and payload["available_qty"] is not None:
        if int(payload["available_qty"]) < 0:
            raise HTTPException(
                status_code=422,
                detail="available_qty must be >= 0",
            )
    if "sales_rank" in payload and payload["sales_rank"] is not None:
        if int(payload["sales_rank"]) < 1:
            raise HTTPException(
                status_code=422,
                detail="sales_rank must be >= 1",
            )
    if "is_best_selling" in payload and payload["is_best_selling"] is not None:
        val = int(payload["is_best_selling"])
        if val not in (0, 1):
            raise HTTPException(
                status_code=422,
                detail="is_best_selling must be 0 or 1",
            )

    # ── Apply updates ──
    updatable = {
        "item_name",
        "description",
        "image_url",
        "brand_id",
        "category_id",
        "subcategory_id",
        "skin_type",
        "concerns",
        "tags",
        "price",
        "available_qty",
        "is_best_selling",
        "best_selling_scope",
        "sales_rank",
        "sap_product_id",
    }

    for key in updatable:
        if key in payload:
            setattr(product, key, payload[key])

    db.commit()
    db.refresh(product)

    # ── Sync productsearchindex ──
    brand_ent = db.query(Brand).filter(Brand.id == product.brand_id).first()
    cat_ent = db.query(Category).filter(Category.id == product.category_id).first()
    sub_ent = db.query(Subcategory).filter(Subcategory.id == product.subcategory_id).first()

    new_brand_name = brand_ent.name if brand_ent else None
    new_cat_name = cat_ent.name if cat_ent else None
    new_sub_name = sub_ent.name if sub_ent else None

    search_text = " ".join(filter(None, [
        str(product.item_code or ""),
        str(new_brand_name or ""),
        str(new_cat_name or ""),
        str(new_sub_name or ""),
        str(product.item_name or ""),
    ])).lower()

    si = (
        db.query(ProductSearchIndex)
        .filter(ProductSearchIndex.product_id == barcode)
        .first()
    )
    if si:
        si.brand_name = new_brand_name
        si.category_name = new_cat_name
        si.subcategory_name = new_sub_name
        si.search_text = search_text
        si.item_code = product.item_code
        si.barcode = barcode
        si.item_name = product.item_name
    else:
        si = ProductSearchIndex(
            product_id=barcode,
            item_code=product.item_code,
            barcode=barcode,
            item_name=product.item_name,
            brand_name=new_brand_name,
            category_name=new_cat_name,
            subcategory_name=new_sub_name,
            search_text=search_text,
        )
        db.add(si)

    db.commit()

    # ── Build response ──
    price = float(product.price) if product.price is not None else 0.0
    return {
        "success": True,
        "message": "Product updated successfully",
        "data": {
            "barcode":         barcode,
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
                "id":    product.brand_id,
                "name":  new_brand_name,
            },
            "category": {
                "id":    product.category_id,
                "name":  new_cat_name,
            },
            "subcategory": {
                "id":    product.subcategory_id,
                "name":  new_sub_name,
            },
            "updated_at": product.updated_at.isoformat() if product.updated_at else None,
        },
    }


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

    # Delete search index FIRST, then product
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
