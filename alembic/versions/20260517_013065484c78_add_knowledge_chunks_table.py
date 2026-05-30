"""add_knowledge_chunks_table

Revision ID: 013065484c78
Revises: eaad580f9809
Create Date: 2026-05-17 00:15:15.872122+00:00

Rewritten to be safe on both fresh and pre-existing databases.

Changes:
- Creates knowledge_chunks table (pgvector or TEXT fallback)
- Cleans up temporary brand_id/category_id/subcategory_id columns
  from product_search_index (added by eaad580f9809, no longer needed)
- Renames product_search_index → productsearchindex to match ORM model
- Reorganises products indexes to use ix_ naming convention
- Removes server_default from products.updated_at and is_best_selling
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

try:
    from pgvector.sqlalchemy import Vector as PgVector
except ImportError:
    PgVector = None

revision: str = '013065484c78'
down_revision: Union[str, None] = 'eaad580f9809'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. knowledge_chunks ───────────────────────────────────────────────────
    has_vector = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).fetchone() is not None

    if has_vector:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        embedding_col = "embedding vector(1536)"
    else:
        embedding_col = "embedding TEXT"

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id                SERIAL NOT NULL,
            knowledge_id      VARCHAR(100) NOT NULL,
            original_filename VARCHAR(500) NOT NULL,
            chunk_index       INTEGER NOT NULL,
            chunk_text        TEXT NOT NULL,
            token_count       INTEGER,
            {embedding_col},
            created_at        TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            PRIMARY KEY (id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_knowledge_id "
        "ON knowledge_chunks (knowledge_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_knowledge_id "
        "ON knowledge_chunks (knowledge_id)"
    )

    # ── 2. Drop old brands / categories / subcategories indexes ──────────────
    # These were created in eaad580f9809 and are being replaced.
    op.execute("DROP INDEX IF EXISTS idx_brands_name")
    op.execute("DROP INDEX IF EXISTS idx_categories_name")
    op.execute("DROP INDEX IF EXISTS idx_subcategories_category_id")

    # ── 3. Alter products columns ─────────────────────────────────────────────
    # Make item_code NOT NULL (will be reversed by 20260519001a immediately after).
    op.alter_column('products', 'item_code',
                    existing_type=sa.VARCHAR(),
                    nullable=False)

    # Remove server_default from is_best_selling (keep nullable=True).
    op.alter_column('products', 'is_best_selling',
                    existing_type=sa.INTEGER(),
                    server_default=None,
                    nullable=True)

    # Remove server_default from updated_at.
    op.alter_column('products', 'updated_at',
                    existing_type=postgresql.TIMESTAMP(),
                    server_default=None,
                    existing_nullable=True)

    # ── 4. Reorganise products indexes ────────────────────────────────────────
    # Drop old indexes (created in baseline or eaad580f9809) using IF EXISTS.
    op.execute("DROP INDEX IF EXISTS idx_products_brand_id")
    op.execute("DROP INDEX IF EXISTS idx_products_category_id")
    op.execute("DROP INDEX IF EXISTS idx_products_concerns")
    op.execute("DROP INDEX IF EXISTS idx_products_item_name_trgm")
    op.execute("DROP INDEX IF EXISTS idx_products_sap_product_id")
    op.execute("DROP INDEX IF EXISTS idx_products_subcategory_id")
    op.execute("DROP INDEX IF EXISTS idx_products_tags")
    op.execute("DROP INDEX IF EXISTS ix_products_barcode")

    # Create replacements with ix_ naming convention.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_brand_id "
        "ON products (brand_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_category_id "
        "ON products (category_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_sap_product_id "
        "ON products (sap_product_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_products_subcategory_id "
        "ON products (subcategory_id)"
    )

    # ── 5. Clean up product_search_index ─────────────────────────────────────
    # eaad580f9809 added brand_id / category_id / subcategory_id columns and
    # their indexes to product_search_index. These are not in the ORM model
    # and must be removed before we rename the table.
    op.execute("DROP INDEX IF EXISTS idx_product_search_index_brand_id")
    op.execute("DROP INDEX IF EXISTS idx_product_search_index_category_id")
    op.execute("DROP INDEX IF EXISTS idx_product_search_index_subcategory_id")

    # Drop the columns only if they exist (safe on both fresh and old DBs).
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'product_search_index'
                  AND column_name = 'brand_id'
            ) THEN
                ALTER TABLE product_search_index DROP COLUMN brand_id;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'product_search_index'
                  AND column_name = 'category_id'
            ) THEN
                ALTER TABLE product_search_index DROP COLUMN category_id;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'product_search_index'
                  AND column_name = 'subcategory_id'
            ) THEN
                ALTER TABLE product_search_index DROP COLUMN subcategory_id;
            END IF;
        END $$;
    """)

    # ── 6. Rename product_search_index → productsearchindex ──────────────────
    # The ORM model uses __tablename__ = "productsearchindex".
    # All migrations after this one reference productsearchindex.
    # Safe to run even if the table was already renamed.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'product_search_index'
                  AND table_schema = current_schema()
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'productsearchindex'
                  AND table_schema = current_schema()
            ) THEN
                ALTER TABLE product_search_index RENAME TO productsearchindex;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Rename back
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'productsearchindex'
                  AND table_schema = current_schema()
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'product_search_index'
                  AND table_schema = current_schema()
            ) THEN
                ALTER TABLE productsearchindex RENAME TO product_search_index;
            END IF;
        END $$;
    """)

    op.execute("DROP INDEX IF EXISTS ix_products_subcategory_id")
    op.execute("DROP INDEX IF EXISTS ix_products_sap_product_id")
    op.execute("DROP INDEX IF EXISTS ix_products_category_id")
    op.execute("DROP INDEX IF EXISTS ix_products_brand_id")

    op.execute("CREATE INDEX IF NOT EXISTS ix_products_barcode ON products (barcode)")

    op.alter_column('products', 'updated_at',
                    existing_type=postgresql.TIMESTAMP(),
                    server_default=sa.text('now()'),
                    existing_nullable=True)
    op.alter_column('products', 'is_best_selling',
                    existing_type=sa.INTEGER(),
                    server_default=sa.text('0'),
                    nullable=False)
    op.alter_column('products', 'item_code',
                    existing_type=sa.VARCHAR(),
                    nullable=True)

    op.execute("CREATE INDEX IF NOT EXISTS idx_categories_name ON categories (name)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_brands_name ON brands (name)")

    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_knowledge_id")
    op.execute("DROP INDEX IF EXISTS idx_knowledge_chunks_knowledge_id")
    op.drop_table("knowledge_chunks")
