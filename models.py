from sqlalchemy import Column, Integer, String, Text, DateTime, Numeric, func, Index
from sqlalchemy.dialects.postgresql import JSONB
from database import Base


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
    role          = Column(String, nullable=False)        # 'user' | 'assistant'
    content       = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)           # JSON: {products, image_url}
    created_at    = Column(DateTime, server_default=func.now())


# ── Product Catalog ───────────────────────────────────────────────────────────

class Product(Base):
    __tablename__ = "products"

    # ── Identity ──────────────────────────────────────────────────────────────
    barcode        = Column(String(100), primary_key=True)
                     # SAP master linking key — NEVER change this PK type
    item_code      = Column(String(100), unique=True, index=True, nullable=False)
    sap_product_id = Column(String(100), nullable=True, index=True)
                     # SAP internal product ID (if different from barcode)

    # ── Display ───────────────────────────────────────────────────────────────
    item_name      = Column(Text, nullable=False, index=True)
    description    = Column(Text, nullable=True)
    image_url      = Column(String(500), nullable=True)

    # ── Normalized Relations (loose coupling — no FK constraints) ─────────────
    brand_id       = Column(Integer, nullable=True, index=True)
                     # references brands.id
    category_id    = Column(Integer, nullable=True, index=True)
                     # references categories.id
    subcategory_id = Column(Integer, nullable=True, index=True)
                     # references subcategories.id

    # ── AI / Search Attributes ────────────────────────────────────────────────
    skin_type      = Column(String(100), nullable=True)
    concerns       = Column(JSONB, nullable=True)         # ["acne","dryness"]
    tags           = Column(JSONB, nullable=True)         # ["bestseller","new"]

    # ── Pricing (overwritten by bi-daily SAP sync) ────────────────────────────
    price          = Column(Numeric(12, 2), nullable=True)
    available_qty  = Column(Integer, default=0)

    # ── Sales Intelligence ────────────────────────────────────────────────────
    is_best_selling    = Column(Integer, default=0)       # 1=yes, 0=no
    best_selling_scope = Column(String(100), nullable=True)
                         # "global" | "category" | "brand" | "subcategory"
    sales_rank         = Column(Integer, nullable=True)   # lower = higher rank

    # ── SAP Sync Tracking ─────────────────────────────────────────────────────
    last_synced_sap = Column(DateTime, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())


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

# ── RAG Knowledge Chunks (requires pgvector C extension) ─────────────────────
# Install pgvector before first run:
#   pip install pgvector
#   pip install pgvector  # Python SDK
#   CREATE EXTENSION vector;  # PostgreSQL (in PostgreSQL)
#
# Also add to postgresql.conf:
#   shared_preload_libraries = 'pgvector'

from pgvector.sqlalchemy import Vector  # noqa: E402


class KnowledgeChunk(Base):
    """Chunked, embedded segments of knowledge-base files.

    Each PDF/TXT uploaded per /knowledge/upload is split into ~400-token
    overlapping chunks, each embedded with OpenAI text-embedding-3-small and
    stored here with its 1536-dim vector for cosine similarity search.
    """

    __tablename__ = "knowledge_chunks"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    knowledge_id       = Column(String(100),  nullable=False, index=True)
    # e.g. "20260516044558_y8o440" — links to knowledge_base/index.json

    original_filename  = Column(String(500), nullable=False)
    # e.g. "Dhifaf_Baghdad_Presentation.pdf"

    chunk_index        = Column(Integer,     nullable=False)
    # 0, 1, 2, 3… within this file

    chunk_text         = Column(Text,         nullable=False)
    token_count        = Column(Integer,      nullable=True)
    embedding          = Column(Vector(1536), nullable=True)
    # 1536-dim vector from text-embedding-3-small
    # Cosine similarity: 1 - (embedding <=> :query)

    created_at         = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index(
            "idx_knowledge_chunks_knowledge_id",
            "knowledge_id",
        ),
    )
