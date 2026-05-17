"""Baseline schema — captures every table, column, and index
created by init_db_fuzzy.py so that future migrations have a
well-defined starting point.

Revision ID: b4f3a1d2e891
Revises:
Create Date: 2026-05-17

IMPORTANT — PRODUCTION WORKFLOW
================================
If your PostgreSQL database was already created by init_db_fuzzy.py,
do NOT run `alembic upgrade head`. Instead, stamp the existing database
so Alembic considers it up-to-date:

    alembic stamp b4f3a1d2e891

Then all future `alembic upgrade head` calls will only apply new migrations.

FRESH DATABASE WORKFLOW
========================
On a brand-new (empty) database:

    alembic upgrade head

This will run upgrade() below, creating all tables and indexes from scratch.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b4f3a1d2e891"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── PostgreSQL extensions ─────────────────────────────────────────────────
    # Required for trigram (fuzzy) search across item_name and search_text.
    # Safe to run on a DB that already has the extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── chat_history ──────────────────────────────────────────────────────────
    # Stores every turn of every user conversation.
    # user_id is a client-generated UUID — no server-side session table yet.
    op.create_table(
        "chat_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),       # 'user' | 'assistant'
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True), # JSON: {products, image_url, order_link}
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_history_id", "chat_history", ["id"])
    op.create_index("ix_chat_history_user_id", "chat_history", ["user_id"])

    # ── orders ────────────────────────────────────────────────────────────────
    # One row per line-item. Multiple rows share the same order_id when a single
    # place_order tool call contains multiple items.
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(), nullable=True),    # ORD-{random_hex}
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("customer_name", sa.String(), nullable=False),
        sa.Column("customer_email", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False), # barcode or item_code
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),   # Python-side default=1
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_id", "orders", ["id"])
    op.create_index("ix_orders_order_id", "orders", ["order_id"])

    # ── products ──────────────────────────────────────────────────────────────
    # Master product catalog. barcode is the primary key because SAP uses it as
    # its unique item identifier. price and available_qty are overwritten by the
    # bi-daily SAP sync job — do not treat them as authoritative between syncs.
    op.create_table(
        "products",
        sa.Column("barcode", sa.String(), nullable=False),   # SAP master key — NEVER change PK type
        sa.Column("item_code", sa.String(), nullable=True),
        sa.Column("item_name", sa.Text(), nullable=False),
        sa.Column("brand", sa.String(), nullable=True),      # plain string; no brands table yet
        sa.Column("category", sa.String(), nullable=True),   # plain string; no categories table yet
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("skin_type", sa.String(), nullable=True),
        sa.Column(
            "concerns",
            postgresql.JSONB(),
            nullable=True,                                   # e.g. ["acne", "dryness"]
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(),
            nullable=True,                                   # e.g. ["bestseller", "new"]
        ),
        sa.Column(
            "price",
            sa.Numeric(precision=12, scale=2),
            nullable=True,                                   # Python-side default=0.00
        ),
        sa.Column("available_qty", sa.Integer(), nullable=True),  # Python-side default=0
        sa.Column("last_synced_sap", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
            # onupdate is handled by SQLAlchemy ORM (Python-side), not a DB trigger.
        ),
        sa.PrimaryKeyConstraint("barcode"),
    )
    # Standard B-tree indexes (created by SQLAlchemy index=True)
    op.create_index("ix_products_barcode", "products", ["barcode"])
    op.create_index("ix_products_item_code", "products", ["item_code"], unique=True)
    op.create_index("ix_products_item_name", "products", ["item_name"])
    op.create_index("ix_products_brand", "products", ["brand"])
    op.create_index("ix_products_category", "products", ["category"])

    # GIN indexes — Alembic autogenerate cannot detect these; always write raw SQL.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_item_name_trgm "
        "ON products USING gin (item_name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_concerns "
        "ON products USING gin (concerns)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_tags "
        "ON products USING gin (tags)"
    )

    # ── product_search_index ──────────────────────────────────────────────────
    # Denormalized search mirror of products. search_text concatenates all
    # searchable fields so full-text and trigram queries avoid joining products.
    # product_id → products.barcode (loose coupling — no FK constraint by design).
    op.create_table(
        "product_search_index",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),  # → products.barcode
        sa.Column("item_code", sa.String(), nullable=True),
        sa.Column("barcode", sa.String(), nullable=True),
        sa.Column("item_name", sa.Text(), nullable=False),
        sa.Column("brand", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("subcategory", sa.String(), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=False),   # concat blob for FTS
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_search_index_id", "product_search_index", ["id"])
    op.create_index("ix_product_search_index_product_id", "product_search_index", ["product_id"], unique=True)
    op.create_index("ix_product_search_index_item_code", "product_search_index", ["item_code"])
    op.create_index("ix_product_search_index_barcode", "product_search_index", ["barcode"])
    op.create_index("ix_product_search_index_item_name", "product_search_index", ["item_name"])
    op.create_index("ix_product_search_index_brand", "product_search_index", ["brand"])
    op.create_index("ix_product_search_index_category", "product_search_index", ["category"])
    op.create_index("ix_product_search_index_subcategory", "product_search_index", ["subcategory"])
    op.create_index("ix_product_search_index_search_text", "product_search_index", ["search_text"])

    # Full-text search GIN index — powers ts_rank() queries in tools.py
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_search_index_fts "
        "ON product_search_index USING gin (to_tsvector('english', search_text))"
    )


def downgrade() -> None:
    # ── Drop GIN indexes first (not tracked by op.create_index) ──────────────
    op.execute("DROP INDEX IF EXISTS idx_product_search_index_fts")
    op.execute("DROP INDEX IF EXISTS idx_products_tags")
    op.execute("DROP INDEX IF EXISTS idx_products_concerns")
    op.execute("DROP INDEX IF EXISTS idx_products_item_name_trgm")

    # ── Drop tables in reverse dependency order ───────────────────────────────
    op.drop_table("product_search_index")
    op.drop_table("products")
    op.drop_table("orders")
    op.drop_table("chat_history")

    # pg_trgm extension is intentionally NOT dropped — it may be used by other
    # schemas on the same PostgreSQL instance.
