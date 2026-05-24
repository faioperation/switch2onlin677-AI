import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import UserDefinedType

from database import Base


# ── Enums ─────────────────────────────────────────────────────────────────────

class PriceTier(str, enum.Enum):
    budget  = "Budget"
    mid     = "Mid"
    premium = "Premium"
    luxury  = "Luxury"


class ProductStatus(str, enum.Enum):
    active   = "active"
    inactive = "inactive"
    draft    = "draft"


# ── Normalized Entity Tables ──────────────────────────────────────────────────

class Brand(Base):
    __tablename__ = "brands"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(255), nullable=False, unique=True, index=True)
    name_ar     = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    image_url   = Column(String(500), nullable=True)
    is_active   = Column(Integer, default=1)
    created_at  = Column(DateTime, server_default=func.now())
    updated_at  = Column(DateTime, onupdate=func.now())


class Category(Base):
    __tablename__ = "categories"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    name        = Column(String(255), nullable=False, unique=True, index=True)
    name_ar     = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    image_url   = Column(String(500), nullable=True)
    is_active   = Column(Integer, default=1)
    created_at  = Column(DateTime, server_default=func.now())
    updated_at  = Column(DateTime, onupdate=func.now())


class Subcategory(Base):
    __tablename__ = "subcategories"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    category_id = Column(Integer, nullable=True, index=True)
    name        = Column(String(255), nullable=False, index=True)
    name_ar     = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    is_active   = Column(Integer, default=1)
    created_at  = Column(DateTime, server_default=func.now())
    updated_at  = Column(DateTime, onupdate=func.now())


# ── Conversation & History ────────────────────────────────────────────────────

class ChatHistory(Base):
    __tablename__ = "chat_history"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(String, index=True, nullable=False)
    role          = Column(String, nullable=False)       # 'user' | 'assistant'
    content       = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)          # JSON: {products, image_url}
    created_at    = Column(DateTime, server_default=func.now())


# ── Product Catalog ───────────────────────────────────────────────────────────

class Product(Base):
    __tablename__ = "products"

    # ── Identity ──────────────────────────────────────────────────────────────
    barcode        = Column(String(100), primary_key=True)
                     # SAP master linking key — NEVER change this PK type
    item_code      = Column(String(100), unique=True, index=True, nullable=True)
    sap_product_id = Column(String(100), nullable=True, index=True)

    # ── Display ───────────────────────────────────────────────────────────────
    item_name   = Column(Text, nullable=True, index=True)
    description = Column(Text, nullable=True)
    image_url   = Column(String(500), nullable=True)

    # ── Normalized Relations (loose coupling — no FK constraints) ─────────────
    brand_id       = Column(Integer, nullable=True, index=True)
    category_id    = Column(Integer, nullable=True, index=True)
    subcategory_id = Column(Integer, nullable=True, index=True)

    # ── AI / Search Attributes ────────────────────────────────────────────────
    skin_type = Column(String(100), nullable=True)
    concerns  = Column(JSONB, nullable=True)             # ["acne","dryness"]
    tags      = Column(JSONB, nullable=True)             # ["bestseller","new"]

    # ── Pricing — SAP is the source of truth, overwritten every 12 h ─────────
    price         = Column(Numeric(12, 2), nullable=True)
    available_qty = Column(Integer, default=0)

    # ── Classification (set via Excel upload / dashboard) ─────────────────────
    price_tier   = Column(
        SAEnum(PriceTier, name="price_tier_enum", create_type=False),
        nullable=True,
        index=True,
    )
    brand_family = Column(String(100), nullable=True, index=True)
                   # e.g. "Italian Niche", "French Designer", "Local"

    product_status = Column(
        SAEnum(ProductStatus, name="product_status_enum", create_type=False),
        nullable=False,
        default=ProductStatus.active,
        index=True,
    )

    # ── Recommendation Flags — NEVER overwritten by SAP sync ──────────────────
    is_best_selling               = Column(Boolean, nullable=True, default=False)
    is_new_arrival                = Column(Boolean, nullable=True, default=False)
    is_recommended                = Column(Boolean, nullable=True, default=False)
    is_cod_recommended            = Column(Boolean, nullable=True, default=False)
    recommendation_priority       = Column(Integer,  nullable=True, default=0, index=True)
                                    # lower number = higher priority
    recommendation_score_override = Column(Numeric(5, 2), nullable=True)
                                    # manual override for AI scoring

    # ── Legacy Sales Intelligence (kept for backward compatibility) ───────────
    best_selling_scope = Column(String(100), nullable=True)
                         # "global" | "category" | "brand" | "subcategory"
    sales_rank         = Column(Integer, nullable=True)

    # ── Bundle ────────────────────────────────────────────────────────────────
    bundle_group           = Column(String(100), nullable=True, index=True)
                             # products sharing same bundle_group form a bundle
    bundle_discount_percent= Column(Numeric(5, 2), nullable=True)
                             # discount applied when bought as bundle

    # ── SAP Sync Tracking ─────────────────────────────────────────────────────
    last_synced_sap = Column(DateTime, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    __table_args__ = (
        # ── Recommendation engine — partial composite indexes ─────────────────
        # All use WHERE product_status = 'active' AND available_qty > 5 so the
        # planner only indexes the rows that recommendation queries actually touch.

        Index(
            "idx_products_recommendation_filter",
            "product_status", "is_recommended", "recommendation_priority",
            postgresql_where="product_status = 'active' AND available_qty > 5",
        ),
        Index(
            "idx_products_best_selling_active",
            "is_best_selling", "category_id",
            postgresql_where="is_best_selling = TRUE AND product_status = 'active'",
        ),
        Index(
            "idx_products_new_arrival_active",
            "is_new_arrival", "created_at",
            postgresql_where="is_new_arrival = TRUE AND product_status = 'active'",
        ),
        Index(
            "idx_products_tier_category",
            "price_tier", "category_id",
            postgresql_where="product_status = 'active'",
        ),
        Index(
            "idx_products_cod_recommended_active",
            "is_cod_recommended", "category_id", "recommendation_priority",
            postgresql_where=(
                "is_cod_recommended = TRUE AND product_status = 'active' AND available_qty > 5"
            ),
        ),
        Index(
            "idx_products_brand_family_active",
            "brand_family", "category_id", "recommendation_priority",
            postgresql_where="product_status = 'active' AND available_qty > 5",
        ),

        # ── Sort / range filter indexes ───────────────────────────────────────
        # created_at: default sort on every GET /products list call.
        # price: min_price/max_price range filters + price_asc/price_desc sort.
        # available_qty: in_stock=true filter (available_qty > 0).
        # sales_rank: ORDER BY in get_best_selling().

        Index("idx_products_created_at",    "created_at"),
        Index("idx_products_price",         "price"),
        Index("idx_products_available_qty", "available_qty"),
        Index("idx_products_sales_rank",    "sales_rank"),
    )


# ── Denormalized Search Mirror ────────────────────────────────────────────────

class ProductSearchIndex(Base):
    """Denormalized search mirror of products.

    search_text = item_code + item_name + brand_name + category_name +
                  subcategory_name — concatenated for full-text and trigram queries.

    brand_name / category_name / subcategory_name are stored as strings here
    (not IDs) so search queries never need to JOIN entity tables for performance.

    product_id → products.barcode (loose coupling — no FK constraint by design).
    """
    __tablename__ = "productsearchindex"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    product_id       = Column(String(100), unique=True, index=True)
    item_code        = Column(String(100), index=True, nullable=True)
    barcode          = Column(String(100), index=True, nullable=True)
    item_name        = Column(Text, index=True, nullable=True)
    brand_name       = Column(String(255), nullable=True, index=True)
    category_name    = Column(String(255), nullable=True, index=True)
    subcategory_name = Column(String(255), nullable=True, index=True)
    search_text      = Column(Text, nullable=True)
    updated_at       = Column(DateTime, onupdate=func.now())

    __table_args__ = (
        # GIN full-text index — powers to_tsvector('english', search_text) @@ plainto_tsquery(...)
        # Created via raw SQL in migrations (Alembic cannot auto-detect GIN indexes).
        # Declared here for documentation; the actual index is idx_product_search_index_fts.

        # GIN trigram index — powers the ILIKE '%...%' branch of the search OR-query.
        # idx_psi_search_text_trgm (added in migration 20260525001a).
        # Requires: pg_trgm extension (enabled in baseline migration b4f3a1d2e891).
    )


# ── RAG Knowledge Chunks (requires pgvector C extension) ─────────────────────

try:
    from pgvector.sqlalchemy import Vector  # noqa: E402
except ModuleNotFoundError:
    class Vector(UserDefinedType):
        def __init__(self, dimensions):
            self.dimensions = dimensions

        def get_col_spec(self, **kwargs):
            return f"VECTOR({self.dimensions})"


class KnowledgeChunk(Base):
    """Chunked, embedded segments of knowledge-base files.

    Each PDF/TXT uploaded per /knowledge/upload is split into ~400-token
    overlapping chunks, each embedded with OpenAI text-embedding-3-small and
    stored here with its 1536-dim vector for cosine similarity search.
    """

    __tablename__ = "knowledge_chunks"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id      = Column(String(100), nullable=False, index=True)
    original_filename = Column(String(500), nullable=False)
    chunk_index       = Column(Integer, nullable=False)
    chunk_text        = Column(Text, nullable=False)
    token_count       = Column(Integer, nullable=True)
    embedding         = Column(Vector(1536), nullable=True)
    created_at        = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("idx_knowledge_chunks_knowledge_id", "knowledge_id"),
    )


# ── Bundle Architecture ───────────────────────────────────────────────────────
#
# Two-table design that replaces the flat bundle_group / bundle_discount_percent
# columns on products (those columns are kept for backward compat but deprecated).
#
# bundles      — one row per bundle: name, code, discount, scheduling
# bundle_items — join table: which products belong to which bundle


class Bundle(Base):
    """
    A curated product bundle.

    Identified externally by bundle_code (URL-safe slug), e.g. "ramadan-kit-2026".
    Discount, scheduling, and display metadata live here — not spread across
    every product row.

    bundle_code is the stable public identifier used in API URLs.
    id is the internal FK used in bundle_items.
    """

    __tablename__ = "bundles"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    bundle_code    = Column(String(100), unique=True, nullable=False, index=True)
                     # URL-safe slug: "ramadan-kit-2026"
    name           = Column(String(255), nullable=False)
    name_ar        = Column(String(255), nullable=True)
    description    = Column(Text, nullable=True)
    image_url      = Column(String(500), nullable=True)

    discount_percent = Column(Numeric(5, 2), nullable=True)
                       # Bundle-wide discount percentage.
                       # None = no bundle discount (items sold at individual prices).

    is_active      = Column(Boolean, nullable=False, default=True)
    sort_order     = Column(Integer, nullable=False, default=0, index=True)
                     # Ascending: lower number appears first in bundle list.

    # ── Optional time-bound promotion window ──────────────────────────────────
    valid_from     = Column(DateTime, nullable=True)   # None = always valid
    valid_until    = Column(DateTime, nullable=True)   # None = no expiry

    created_at     = Column(DateTime, server_default=func.now())
    updated_at     = Column(DateTime, onupdate=func.now())

    __table_args__ = (
        Index("idx_bundles_active_sort", "is_active", "sort_order"),
    )


class BundleItem(Base):
    """
    A single product slot inside a bundle.

    One bundle → many items.
    A product (barcode) can appear in multiple bundles (many-to-many).

    Loose coupling: bundle_id and barcode are plain columns with no FK
    constraints — consistent with the rest of the codebase.

    is_anchor marks the hero product displayed first in bundle cards
    (e.g. the perfume in a "Perfume + Moisturiser" set).
    """

    __tablename__ = "bundle_items"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    bundle_id   = Column(Integer, nullable=False, index=True)
                  # → bundles.id (loose coupling — no FK constraint)
    barcode     = Column(String(100), nullable=False, index=True)
                  # → products.barcode

    quantity    = Column(Integer, nullable=False, default=1)
                  # How many units of this product are in the bundle.
    sort_order  = Column(Integer, nullable=False, default=0)
                  # Display order within the bundle (anchor item is typically 0).
    is_anchor   = Column(Boolean, nullable=False, default=False)
                  # True for the hero/featured product of the bundle.

    created_at  = Column(DateTime, server_default=func.now())

    __table_args__ = (
        # Primary access pattern: "give me all items in bundle X, sorted"
        Index("idx_bundle_items_bundle_sort", "bundle_id", "sort_order"),
        # Uniqueness: a product can only appear once per bundle
        Index("idx_bundle_items_bundle_barcode", "bundle_id", "barcode", unique=True),
        # Reverse lookup: "which bundles contain this product?"
        Index("idx_bundle_items_barcode", "barcode"),
    )


# ── Product Status Audit Log ──────────────────────────────────────────────────

class ProductStatusLog(Base):
    """
    Immutable audit record for every product_status change.

    One row is written each time a product's status transitions
    (draft → active, active → inactive, etc.).

    changed_by  — the user ID, API key label, or "system" that triggered the
                  change.  Never null; use "system" for automated transitions.
    reason      — optional free-text note (e.g. "seasonal deactivation Q3").
    from_status — NULL only for the very first status assignment (create path).
    to_status   — the new status value.

    This table is append-only: never UPDATE or DELETE rows.
    """

    __tablename__ = "product_status_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    barcode     = Column(String(100), nullable=False, index=True)
                  # → products.barcode (loose coupling — no FK constraint)
    from_status = Column(String(20), nullable=True)
                  # NULL = first-ever status assignment
    to_status   = Column(String(20), nullable=False)
    changed_by  = Column(String(255), nullable=False, default="system")
    reason      = Column(Text, nullable=True)
    changed_at  = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        # Primary query: "give me the full history for this product"
        Index("idx_psl_barcode_changed_at", "barcode", "changed_at"),
        # Dashboard query: "all transitions to inactive today"
        Index("idx_psl_to_status_changed_at", "to_status", "changed_at"),
    )
