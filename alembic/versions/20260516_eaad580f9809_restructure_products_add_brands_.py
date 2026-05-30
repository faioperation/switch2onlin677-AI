"""restructure_products_add_brands_categories_drop_orders

Revision ID: eaad580f9809
Revises: b4f3a1d2e891
Create Date: 2026-05-16 23:04:54

This migration restructures the product catalog architecture by:

- Adding normalized brand/category/subcategory references
- Extending products table with sales + SAP fields
- Updating product_search_index in-place
- Removing obsolete orders table
- Preserving all existing searchable data
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "eaad580f9809"
down_revision: Union[str, None] = "b4f3a1d2e891"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ==========================================================================
    # BRANDS / CATEGORIES / SUBCATEGORIES — CREATE TABLES IF NOT EXISTS
    # These were created by init_db_fuzzy.py on the original DB but are absent
    # on a fresh database. We use raw SQL with IF NOT EXISTS so this is safe
    # to run whether or not the tables already exist.
    # ==========================================================================

    op.execute("""
        CREATE TABLE IF NOT EXISTS brands (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(255) NOT NULL UNIQUE,
            name_ar     VARCHAR(255),
            description TEXT,
            image_url   VARCHAR(500),
            is_active   INTEGER DEFAULT 1,
            created_at  TIMESTAMP DEFAULT now(),
            updated_at  TIMESTAMP
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(255) NOT NULL UNIQUE,
            name_ar     VARCHAR(255),
            description TEXT,
            image_url   VARCHAR(500),
            is_active   INTEGER DEFAULT 1,
            created_at  TIMESTAMP DEFAULT now(),
            updated_at  TIMESTAMP
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS subcategories (
            id          SERIAL PRIMARY KEY,
            category_id INTEGER,
            name        VARCHAR(255) NOT NULL,
            name_ar     VARCHAR(255),
            description TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TIMESTAMP DEFAULT now(),
            updated_at  TIMESTAMP
        )
    """)

    # ==========================================================================
    # PRODUCTS — GIN / TRIGRAM INDEXES
    # ==========================================================================

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

    # ==========================================================================
    # PRODUCT SEARCH INDEX — FULL TEXT SEARCH INDEX
    # ==========================================================================

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_search_index_fts "
        "ON product_search_index "
        "USING gin (to_tsvector('english', search_text))"
    )

    # ==========================================================================
    # BRANDS / CATEGORIES / SUBCATEGORIES INDEXES
    # ==========================================================================

    op.create_index(
        "idx_brands_name",
        "brands",
        ["name"],
        unique=False,
    )

    op.create_index(
        "idx_categories_name",
        "categories",
        ["name"],
        unique=False,
    )

    op.create_index(
        "idx_subcategories_category_id",
        "subcategories",
        ["category_id"],
        unique=False,
    )

    # ==========================================================================
    # PRODUCTS — REMOVE OLD STRING COLUMNS
    # ==========================================================================

    op.drop_index("ix_products_brand", table_name="products")
    op.drop_index("ix_products_category", table_name="products")

    # ==========================================================================
    # PRODUCTS — ADD NEW NORMALIZED COLUMNS
    # ==========================================================================

    op.add_column(
        "products",
        sa.Column("brand_id", sa.Integer(), nullable=True),
    )

    op.add_column(
        "products",
        sa.Column("category_id", sa.Integer(), nullable=True),
    )

    op.add_column(
        "products",
        sa.Column("subcategory_id", sa.Integer(), nullable=True),
    )

    op.add_column(
        "products",
        sa.Column("sap_product_id", sa.String(length=100), nullable=True),
    )

    op.add_column(
        "products",
        sa.Column(
            "is_best_selling",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.add_column(
        "products",
        sa.Column(
            "best_selling_scope",
            sa.String(length=100),
            nullable=True,
        ),
    )

    op.add_column(
        "products",
        sa.Column("sales_rank", sa.Integer(), nullable=True),
    )

    op.drop_column("products", "brand")
    op.drop_column("products", "category")

    # ==========================================================================
    # PRODUCTS — NEW INDEXES
    # ==========================================================================

    op.create_index(
        "idx_products_brand_id",
        "products",
        ["brand_id"],
        unique=False,
    )

    op.create_index(
        "idx_products_category_id",
        "products",
        ["category_id"],
        unique=False,
    )

    op.create_index(
        "idx_products_subcategory_id",
        "products",
        ["subcategory_id"],
        unique=False,
    )

    op.create_index(
        "idx_products_sap_product_id",
        "products",
        ["sap_product_id"],
        unique=False,
    )

    # ==========================================================================
    # PRODUCT_SEARCH_INDEX — UPDATE EXISTING TABLE IN-PLACE
    # ==========================================================================

    with op.batch_alter_table("product_search_index") as batch_op:

        batch_op.add_column(
            sa.Column("brand_id", sa.Integer(), nullable=True)
        )

        batch_op.add_column(
            sa.Column("category_id", sa.Integer(), nullable=True)
        )

        batch_op.add_column(
            sa.Column("subcategory_id", sa.Integer(), nullable=True)
        )

        batch_op.create_index(
            "idx_product_search_index_brand_id",
            ["brand_id"],
            unique=False,
        )

        batch_op.create_index(
            "idx_product_search_index_category_id",
            ["category_id"],
            unique=False,
        )

        batch_op.create_index(
            "idx_product_search_index_subcategory_id",
            ["subcategory_id"],
            unique=False,
        )

    # ==========================================================================
    # REMOVE LEGACY ORDERS TABLE
    # ==========================================================================

    op.drop_index("ix_orders_id", table_name="orders")

    op.drop_index("ix_orders_order_id", table_name="orders")

    op.drop_table("orders")


def downgrade() -> None:

    # ==========================================================================
    # RECREATE ORDERS TABLE
    # ==========================================================================

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("customer_name", sa.String(), nullable=False),
        sa.Column("customer_email", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),
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

    # ==========================================================================
    # PRODUCT_SEARCH_INDEX — REMOVE NEW COLUMNS
    # ==========================================================================

    with op.batch_alter_table("product_search_index") as batch_op:

        batch_op.drop_index(
            "idx_product_search_index_subcategory_id"
        )

        batch_op.drop_index(
            "idx_product_search_index_category_id"
        )

        batch_op.drop_index(
            "idx_product_search_index_brand_id"
        )

        batch_op.drop_column("subcategory_id")

        batch_op.drop_column("category_id")

        batch_op.drop_column("brand_id")

    # ==========================================================================
    # PRODUCTS — RESTORE OLD COLUMNS
    # ==========================================================================

    op.add_column(
        "products",
        sa.Column("brand", sa.String(), nullable=True),
    )

    op.add_column(
        "products",
        sa.Column("category", sa.String(), nullable=True),
    )

    # ==========================================================================
    # DROP NEW INDEXES
    # ==========================================================================

    op.drop_index(
        "idx_products_subcategory_id",
        table_name="products",
    )

    op.drop_index(
        "idx_products_sap_product_id",
        table_name="products",
    )

    op.drop_index(
        "idx_products_category_id",
        table_name="products",
    )

    op.drop_index(
        "idx_products_brand_id",
        table_name="products",
    )

    # ==========================================================================
    # RECREATE OLD INDEXES
    # ==========================================================================

    op.create_index(
        "ix_products_brand",
        "products",
        ["brand"],
        unique=False,
    )

    op.create_index(
        "ix_products_category",
        "products",
        ["category"],
        unique=False,
    )

    # ==========================================================================
    # RECREATE GIN INDEXES
    # ==========================================================================

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

    # ==========================================================================
    # REMOVE NEW COLUMNS
    # ==========================================================================

    op.drop_column("products", "sales_rank")

    op.drop_column("products", "best_selling_scope")

    op.drop_column("products", "is_best_selling")

    op.drop_column("products", "subcategory_id")

    op.drop_column("products", "category_id")

    op.drop_column("products", "brand_id")

    op.drop_column("products", "sap_product_id")

    # ==========================================================================
    # DROP EXTRA INDEXES
    # ==========================================================================

    op.execute("DROP INDEX IF EXISTS idx_brands_name")

    op.execute("DROP INDEX IF EXISTS idx_categories_name")

    op.execute("DROP INDEX IF EXISTS idx_subcategories_category_id")

    op.execute("DROP INDEX IF EXISTS idx_product_search_index_fts")