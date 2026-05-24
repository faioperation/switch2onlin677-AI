"""
repositories/product.py
=======================
Data-access layer for the Product model.

Rules
-----
- One Session injected at construction time — NEVER opens SessionLocal internally.
- No HTTP concerns (no HTTPException, no JSONResponse) — raises plain Python
  exceptions or returns None/False so the router decides the HTTP response.
- Every method is independently testable with a mock/test session.

Public API
----------
ProductRepository(db)
  .get_by_barcode(barcode)               → dict | None
  .list_products(**filters)              → {"products": [...], "pagination": {...}}
  .update(barcode, payload)              → {"product": dict, "warnings": [str]}
                                           raises ValueError on bad input
                                           raises LookupError if not found
  .delete(barcode)                       → {"barcode": str, "item_name": str}
                                           raises LookupError if not found
  .get_filter_options()                  → {"brands": [...], "categories": [...], "subcategories": [...]}
"""

from __future__ import annotations

import logging
import os
import json
import re
from typing import Any, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from models import Brand, Category, Product, ProductSearchIndex, Subcategory
from core.exceptions import AppValidationError, ConflictError, ForbiddenError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

RATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "rate.json")
BARCODE_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
VALID_BEST_SELLING_SCOPES = {"global", "category", "brand"}
PROTECTED_FIELDS = {"last_synced_sap", "created_at"}

VALID_SORT_COLUMNS: dict[str, tuple[str, str]] = {
    "name_asc":    ("item_name",    "ASC"),
    "name_desc":   ("item_name",    "DESC"),
    "price_asc":   ("price",        "ASC"),
    "price_desc":  ("price",        "DESC"),
    "stock_asc":   ("available_qty","ASC"),
    "stock_desc":  ("available_qty","DESC"),
    "created_asc": ("created_at",   "ASC"),
    "created_desc":("created_at",   "DESC"),
}

_SORT_ATTR_MAP = {
    "item_name":     Product.item_name,
    "price":         Product.price,
    "available_qty": Product.available_qty,
    "created_at":    Product.created_at,
}

UPDATABLE_FIELDS = {
    "item_name", "description", "image_url",
    "brand_id", "category_id", "subcategory_id",
    "skin_type", "concerns", "tags",
    "price", "available_qty",
    "is_best_selling", "best_selling_scope", "sales_rank",
    "sap_product_id",
}


# ── Helpers (module-level, no DB dependency) ──────────────────────────────────

def _get_iqd_rate() -> float:
    if not os.path.exists(RATE_FILE):
        return 1310.0
    try:
        with open(RATE_FILE, "r", encoding="utf-8") as f:
            return float(json.load(f).get("iqd_rate", 1310))
    except Exception:
        return 1310.0


def _price_iqd(price: Any) -> float:
    if price is None:
        return 0.0
    return round(float(price) * _get_iqd_rate(), 0)


def _stock_status(available_qty: Any) -> str:
    if available_qty is None:
        return "out_of_stock"
    return "in_stock" if available_qty > 0 else "out_of_stock"


def _build_search_text(*parts: Optional[str]) -> str:
    return " ".join(s for s in parts if s).lower()


# ── Repository class ──────────────────────────────────────────────────────────

class ProductRepository:
    """
    All Product DB access in one place.
    Constructed with the request-scoped Session from FastAPI's get_db dependency.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Private helpers ───────────────────────────────────────────────────────

    def _lookup_relations(
        self, brand_id, category_id, subcategory_id
    ) -> tuple[Any, Any, Any]:
        """
        Fetch Brand / Category / Subcategory entities using the injected session.
        Returns (brand_ent, cat_ent, sub_ent) — any may be None.
        """
        brand_ent = (
            self.db.query(Brand).filter(Brand.id == brand_id).first()
            if brand_id else None
        )
        cat_ent = (
            self.db.query(Category).filter(Category.id == category_id).first()
            if category_id else None
        )
        sub_ent = (
            self.db.query(Subcategory).filter(Subcategory.id == subcategory_id).first()
            if subcategory_id else None
        )
        return brand_ent, cat_ent, sub_ent

    def _serialize(
        self,
        row,
        *,
        brand_name:       Optional[str] = None,
        category_name:    Optional[str] = None,
        subcategory_name: Optional[str] = None,
        brand_name_ar:    Optional[str] = None,
        cat_name_ar:      Optional[str] = None,
        sub_name_ar:      Optional[str] = None,
    ) -> dict:
        """
        Convert a Product ORM row to the standard frontend dict.

        Name fields passed explicitly (from JOIN labels or a prior lookup) take
        priority.  If they are None the caller is expected to have called
        _lookup_relations() and passed the names in.
        """
        price = float(row.price) if row.price is not None else 0.0

        return {
            "barcode":           row.barcode,
            "item_code":         row.item_code,
            "item_name":         row.item_name,
            "description":       row.description,
            "image_url":         row.image_url,
            "skin_type":         row.skin_type,
            "concerns":          row.concerns or [],
            "tags":              row.tags or [],
            "price":             price,
            "price_iqd":         _price_iqd(price),
            "available_qty":     row.available_qty or 0,
            "status":            _stock_status(row.available_qty),
            "is_best_selling":   bool(row.is_best_selling) if row.is_best_selling is not None else False,
            "best_selling_scope":getattr(row, "best_selling_scope", None),
            "sales_rank":        getattr(row, "sales_rank", None),
            "sap_product_id":    row.sap_product_id,
            "last_synced_sap":   row.last_synced_sap.isoformat() if row.last_synced_sap else None,
            "created_at":        row.created_at.isoformat() if row.created_at else None,
            "updated_at":        row.updated_at.isoformat() if row.updated_at else None,
            "brand": {
                "id":   row.brand_id,
                "name": brand_name,
            },
            "category": {
                "id":   row.category_id,
                "name": category_name,
            },
            "subcategory": {
                "id":   row.subcategory_id,
                "name": subcategory_name,
            },
            # AR names only included for single-product detail view
            **({"brand_name_ar": brand_name_ar} if brand_name_ar is not None else {}),
            **({"category_name_ar": cat_name_ar} if cat_name_ar is not None else {}),
            **({"subcategory_name_ar": sub_name_ar} if sub_name_ar is not None else {}),
        }

    # ── Public methods ────────────────────────────────────────────────────────

    def get_by_barcode(self, barcode: str) -> dict | None:
        """
        Return full product detail dict for a single barcode, or None.
        Brand / Category / Subcategory are fetched via the injected session
        (no new session opened).
        """
        product = (
            self.db.query(Product)
            .filter(Product.barcode == barcode)
            .first()
        )
        if not product:
            return None

        brand_ent, cat_ent, sub_ent = self._lookup_relations(
            product.brand_id, product.category_id, product.subcategory_id
        )

        return self._serialize(
            product,
            brand_name       = brand_ent.name    if brand_ent else None,
            category_name    = cat_ent.name      if cat_ent   else None,
            subcategory_name = sub_ent.name      if sub_ent   else None,
            brand_name_ar    = getattr(brand_ent, "name_ar", None) if brand_ent else None,
            cat_name_ar      = getattr(cat_ent,   "name_ar", None) if cat_ent   else None,
            sub_name_ar      = getattr(sub_ent,   "name_ar", None) if sub_ent   else None,
        )

    def list_products(
        self,
        *,
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
    ) -> dict:
        """
        Paginated product list with search, filter, and sort.
        Returns {"products": [...], "pagination": {...}, "filters_applied": {...}}.
        """
        # ── Sanitise pagination ───────────────────────────────────────────────
        limit = max(1, min(limit, 500))
        page  = max(1, page)
        offset = (page - 1) * limit

        sort_col, sort_dir = VALID_SORT_COLUMNS.get(
            sort_by or "created_desc", ("created_at", "DESC")
        )

        # ── Base query with name JOINs (avoids extra per-row DB calls) ────────
        select_cols = [
            Product.barcode,    Product.item_code,    Product.item_name,
            Product.description,Product.image_url,    Product.skin_type,
            Product.concerns,   Product.tags,         Product.price,
            Product.available_qty, Product.is_best_selling,
            Product.best_selling_scope, Product.sales_rank,
            Product.sap_product_id, Product.last_synced_sap,
            Product.created_at, Product.updated_at,
            Product.brand_id,   Product.category_id,  Product.subcategory_id,
            Brand.name.label("brand_name"),
            Category.name.label("category_name"),
            Subcategory.name.label("subcategory_name"),
        ]

        query = (
            self.db.query(*select_cols)
            .join(Brand,       Brand.id       == Product.brand_id,       isouter=True)
            .join(Category,    Category.id    == Product.category_id,    isouter=True)
            .join(Subcategory, Subcategory.id == Product.subcategory_id, isouter=True)
        )

        # ── Full-text search via ProductSearchIndex ───────────────────────────
        if q:
            query = query.join(
                ProductSearchIndex,
                ProductSearchIndex.product_id == Product.barcode,
                isouter=True,
            )
            q_like = f"%{q.strip().lower()}%"
            query = query.filter(
                or_(
                    ProductSearchIndex.search_text.ilike(q_like),
                    func.to_tsvector("english", ProductSearchIndex.search_text).op("@@")(
                        func.plainto_tsquery("english", q.strip())
                    ),
                )
            )

        # ── Column filters ────────────────────────────────────────────────────
        if brand_id        is not None: query = query.filter(Product.brand_id        == brand_id)
        if category_id     is not None: query = query.filter(Product.category_id     == category_id)
        if subcategory_id  is not None: query = query.filter(Product.subcategory_id  == subcategory_id)
        if is_best_selling is not None: query = query.filter(Product.is_best_selling == is_best_selling)
        if in_stock:                    query = query.filter(Product.available_qty    >  0)
        if min_price       is not None: query = query.filter(Product.price            >= min_price)
        if max_price       is not None: query = query.filter(Product.price            <= max_price)

        # ── Count (before pagination) ─────────────────────────────────────────
        total = self.db.execute(
            query.statement.with_only_columns(func.count()).order_by(None)
        ).scalar() or 0

        # ── Sort ──────────────────────────────────────────────────────────────
        sort_attr = _SORT_ATTR_MAP.get(sort_col, Product.created_at)
        query = query.order_by(
            sort_attr.desc() if sort_dir.upper() == "DESC" else sort_attr.asc()
        )

        rows = query.offset(offset).limit(limit).all()

        products = [
            self._serialize(
                r,
                brand_name       = getattr(r, "brand_name",       None),
                category_name    = getattr(r, "category_name",    None),
                subcategory_name = getattr(r, "subcategory_name", None),
            )
            for r in rows
        ]

        total_pages = (total + limit - 1) // limit if limit > 0 else 0

        return {
            "products": products,
            "pagination": {
                "total":       total,
                "page":        page,
                "limit":       limit,
                "total_pages": total_pages,
                "has_next":    page < total_pages,
                "has_prev":    page > 1,
            },
            "filters_applied": {
                "q":              q,
                "brand_id":       brand_id,
                "category_id":    category_id,
                "subcategory_id": subcategory_id,
                "is_best_selling":is_best_selling,
                "in_stock":       in_stock,
                "sort_by":        sort_by,
            },
        }

    def update(self, barcode: str, payload: dict) -> dict:
        """
        Partial update for a product.

        Raises
        ------
        LookupError   — product not found
        ValueError    — validation failure (message is the user-facing error string)
                        carry 'status_code' attribute: 409 for duplicates, 422 otherwise

        Returns
        -------
        {"product": dict, "warnings": [str]}
        """
        product = self.db.query(Product).filter(Product.barcode == barcode).first()
        if not product:
            raise NotFoundError(f"Product '{barcode}' not found.")

        old_barcode   = barcode
        old_item_code = product.item_code
        updated_barcode   = False
        updated_item_code = False
        warnings: list[str] = []

        # ── Reject protected fields ───────────────────────────────────────────
        forbidden = PROTECTED_FIELDS & set(payload.keys())
        if forbidden:
            raise ForbiddenError(
                f"Cannot modify protected field(s): {', '.join(sorted(forbidden))}. "
                "These fields are controlled by the system."
            )

        # ── Barcode validation ────────────────────────────────────────────────
        if "barcode" in payload:
            new_barcode = str(payload["barcode"]).strip()
            if not new_barcode or len(new_barcode) < 6 or len(new_barcode) > 32 or not BARCODE_PATTERN.match(new_barcode):
                raise AppValidationError("barcode must be 6–32 alphanumeric characters")
            if new_barcode != old_barcode:
                dup = self.db.query(Product).filter(
                    Product.barcode == new_barcode,
                    Product.barcode != old_barcode,
                ).first()
                if dup:
                    raise ConflictError(f"Barcode '{new_barcode}' already exists. Each barcode must be unique.")
                logger.warning("PK_CHANGE: barcode '%s' → '%s'", old_barcode, new_barcode)
                updated_barcode = True

        # ── item_code validation ──────────────────────────────────────────────
        if "item_code" in payload:
            new_item_code = str(payload["item_code"]).strip()
            if not new_item_code or len(new_item_code) > 50:
                raise AppValidationError("item_code must be 1–50 characters")
            if new_item_code != old_item_code:
                dup = self.db.query(Product).filter(
                    Product.item_code == new_item_code,
                    Product.barcode   != old_barcode,
                ).first()
                if dup:
                    raise ConflictError(f"item_code '{new_item_code}' already used by another product.")
                updated_item_code = True

        # ── FK existence checks ───────────────────────────────────────────────
        for field, model_cls in [
            ("brand_id",       Brand),
            ("category_id",    Category),
            ("subcategory_id", Subcategory),
        ]:
            if field in payload and payload[field] is not None:
                if not self.db.query(model_cls).filter(model_cls.id == payload[field]).first():
                    raise AppValidationError(
                        f"{field} {payload[field]} does not exist in {model_cls.__tablename__} table"
                    )

        # ── Range / enum validations ──────────────────────────────────────────
        if "price" in payload and payload["price"] is not None:
            if float(payload["price"]) < 0:
                raise AppValidationError("price must be >= 0")

        if "available_qty" in payload and payload["available_qty"] is not None:
            if int(payload["available_qty"]) < 0:
                raise AppValidationError("available_qty must be >= 0")

        if "sales_rank" in payload and payload["sales_rank"] is not None:
            if int(payload["sales_rank"]) < 1:
                raise AppValidationError("sales_rank must be >= 1")

        if "best_selling_scope" in payload and payload["best_selling_scope"] is not None:
            scope = str(payload["best_selling_scope"]).strip().lower()
            if scope and scope not in VALID_BEST_SELLING_SCOPES:
                raise AppValidationError(
                    f"best_selling_scope '{scope}' is invalid. "
                    f"Allowed: {', '.join(sorted(VALID_BEST_SELLING_SCOPES))}."
                )

        # ── Apply updates ─────────────────────────────────────────────────────
        # Cascade search index PK rename BEFORE changing the PK on Product
        if updated_barcode:
            new_bc = str(payload["barcode"]).strip()
            self.db.execute(
                text(
                    "UPDATE productsearchindex "
                    "SET product_id = :new_bc, barcode = :new_bc "
                    "WHERE product_id = :old_bc"
                ),
                {"new_bc": new_bc, "old_bc": old_barcode},
            )

        if "item_code" in payload:
            product.item_code = str(payload["item_code"]).strip()
        if updated_barcode:
            product.barcode = str(payload["barcode"]).strip()

        for key in UPDATABLE_FIELDS:
            if key in payload:
                setattr(product, key, payload[key])

        self.db.flush()

        # ── Rebuild search index ──────────────────────────────────────────────
        effective_barcode = str(product.barcode)
        brand_ent, cat_ent, sub_ent = self._lookup_relations(
            product.brand_id, product.category_id, product.subcategory_id
        )
        new_brand_name  = brand_ent.name if brand_ent else None
        new_cat_name    = cat_ent.name   if cat_ent   else None
        new_sub_name    = sub_ent.name   if sub_ent   else None

        search_text = _build_search_text(
            product.item_code, new_brand_name,
            new_cat_name, new_sub_name, product.item_name,
        )

        si = (
            self.db.query(ProductSearchIndex)
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
            self.db.add(ProductSearchIndex(
                product_id       = effective_barcode,
                item_code        = product.item_code,
                barcode          = effective_barcode,
                item_name        = product.item_name,
                brand_name       = new_brand_name,
                category_name    = new_cat_name,
                subcategory_name = new_sub_name,
                search_text      = search_text,
            ))

        self.db.flush()

        # ── Build warnings ────────────────────────────────────────────────────
        if updated_barcode:
            warnings.append(
                f"Primary key (barcode) changed '{old_barcode}' → '{effective_barcode}'. "
                "Any external system referencing the old barcode must be updated."
            )
        if updated_item_code:
            warnings.append(f"item_code changed '{old_item_code}' → '{product.item_code}'.")

        price = float(product.price) if product.price is not None else 0.0

        return {
            "product": {
                "barcode":         effective_barcode,
                "item_code":       product.item_code,
                "item_name":       product.item_name,
                "description":     product.description,
                "image_url":       product.image_url,
                "skin_type":       product.skin_type,
                "concerns":        product.concerns or [],
                "tags":            product.tags or [],
                "price":           price,
                "price_iqd":       _price_iqd(price),
                "available_qty":   product.available_qty,
                "is_best_selling": bool(product.is_best_selling) if product.is_best_selling is not None else False,
                "sales_rank":      product.sales_rank,
                "sap_product_id":  product.sap_product_id,
                "brand":           {"id": product.brand_id,       "name": new_brand_name},
                "category":        {"id": product.category_id,    "name": new_cat_name},
                "subcategory":     {"id": product.subcategory_id, "name": new_sub_name},
                "updated_at":      product.updated_at.isoformat() if product.updated_at else None,
            },
            "warnings": warnings,
        }

    def delete(self, barcode: str) -> dict:
        """
        Delete a product and its search index entry.

        Raises LookupError if not found.
        Returns {"barcode": str, "item_name": str | None}.
        """
        product = self.db.query(Product).filter(Product.barcode == barcode).first()
        if not product:
            raise NotFoundError(f"Product '{barcode}' not found.")

        item_name = product.item_name

        self.db.query(ProductSearchIndex).filter(
            ProductSearchIndex.product_id == barcode
        ).delete(synchronize_session=False)

        self.db.query(Product).filter(
            Product.barcode == barcode
        ).delete(synchronize_session=False)

        self.db.commit()

        return {"barcode": barcode, "item_name": item_name}

    def get_filter_options(self) -> dict:
        """Return brand / category / subcategory dropdown data for filter menus."""
        brands = (
            self.db.query(Brand)
            .filter(Brand.is_active == 1)
            .order_by(Brand.name.asc())
            .all()
        )
        categories = (
            self.db.query(Category)
            .filter(Category.is_active == 1)
            .order_by(Category.name.asc())
            .all()
        )
        subcategories = (
            self.db.query(Subcategory)
            .filter(Subcategory.is_active == 1)
            .order_by(Subcategory.name.asc())
            .all()
        )
        return {
            "brands":        [{"id": b.id, "name": b.name} for b in brands],
            "categories":    [{"id": c.id, "name": c.name} for c in categories],
            "subcategories": [
                {"id": s.id, "category_id": s.category_id, "name": s.name}
                for s in subcategories
            ],
        }
