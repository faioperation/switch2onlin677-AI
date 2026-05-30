"""rename_productsearchindex_brand_category_subcategory_columns

Revision ID: 20260531001a
Revises: 20260530002a
Create Date: 2026-05-31

Problem fixed
-------------
Migration 013065484c78 renamed the table `product_search_index` →
`productsearchindex` to match the ORM model, but never renamed the three
denormalized string columns to match the model attributes:

  DB column   →  ORM attribute
  brand       →  brand_name
  category    →  category_name
  subcategory →  subcategory_name

Every query that touches ProductSearchIndex (upload, search, recommendation)
raises:
    psycopg2.errors.UndefinedColumn: column productsearchindex.brand_name does not exist

Fix
---
Rename the three columns in-place.  No data is lost.  All indexes that
reference the old column names are dropped and recreated.  The rename is
guarded with IF EXISTS / IF NOT EXISTS so it is safe to run on a database
that was already partially patched by hand.
"""

from alembic import op

revision      = "20260531001a"
down_revision = "20260530002a"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # Drop old indexes that reference the old column names before renaming.
    # IF NOT EXISTS / IF EXISTS guards make each step idempotent.
    op.execute("DROP INDEX IF EXISTS ix_productsearchindex_brand_name")
    op.execute("DROP INDEX IF EXISTS ix_productsearchindex_category_name")
    op.execute("DROP INDEX IF EXISTS ix_productsearchindex_subcategory_name")

    # Rename brand → brand_name (only if the old column still exists)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'brand'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'brand_name'
            ) THEN
                ALTER TABLE productsearchindex RENAME COLUMN brand TO brand_name;
            END IF;
        END $$;
    """)

    # Rename category → category_name
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'category'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'category_name'
            ) THEN
                ALTER TABLE productsearchindex RENAME COLUMN category TO category_name;
            END IF;
        END $$;
    """)

    # Rename subcategory → subcategory_name
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'subcategory'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'subcategory_name'
            ) THEN
                ALTER TABLE productsearchindex RENAME COLUMN subcategory TO subcategory_name;
            END IF;
        END $$;
    """)

    # Recreate indexes with the new column names.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_productsearchindex_brand_name "
        "ON productsearchindex (brand_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_productsearchindex_category_name "
        "ON productsearchindex (category_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_productsearchindex_subcategory_name "
        "ON productsearchindex (subcategory_name)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_productsearchindex_brand_name")
    op.execute("DROP INDEX IF EXISTS ix_productsearchindex_category_name")
    op.execute("DROP INDEX IF EXISTS ix_productsearchindex_subcategory_name")

    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'brand_name'
            ) THEN
                ALTER TABLE productsearchindex RENAME COLUMN brand_name TO brand;
            END IF;
        END $$;
    """)

    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'category_name'
            ) THEN
                ALTER TABLE productsearchindex RENAME COLUMN category_name TO category;
            END IF;
        END $$;
    """)

    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name   = 'productsearchindex'
                  AND column_name  = 'subcategory_name'
            ) THEN
                ALTER TABLE productsearchindex RENAME COLUMN subcategory_name TO subcategory;
            END IF;
        END $$;
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_productsearchindex_brand "
        "ON productsearchindex (brand)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_productsearchindex_category "
        "ON productsearchindex (category)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_productsearchindex_subcategory "
        "ON productsearchindex (subcategory)"
    )
