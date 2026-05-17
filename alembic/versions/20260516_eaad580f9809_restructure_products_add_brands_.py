"""restructure_products_add_brands_categories_drop_orders

Revision ID: eaad580f9809
Revises: b4f3a1d2e891
Create Date: 2026-05-16 23:04:54

NOTE: This file was originally auto-generated but has been manually corrected.
The auto-generated version had wrong column types, missing GIN / B-tree indexes,
and an incomplete downgrade. Review before running in production.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision: str = "eaad580f9809"
down_revision: Union[str, None] = "b4f3a1d2e891"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── GIN / trigram indexes ─────────────────────────────────────────────────────
    # op.execute() is mandatory — Alembic autogenerate is blind to these.
    # We (re)create products GIN indexes first so they are always fresh.
    # ──────────────────────────────────────────────────────────────────────────────
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_text_gin "
        "ON productsearchindex USING gin (to_tsvector('english', search_text))"
    )

    # ── brands / categories / subcategories ──────────────────────────────────────
    # Tables were already created by a preceding migration (or manual run).
    # Indexes below are added here only if the migration is applied to a fresh
    # database (where the tables exist but lack the indexes below).
    # ──────────────────────────────────────────────────────────────────────────────
    op.create_index("idx_brands_name", "brands", ["name"])
    op.create_index("idx_categories_name", "categories", ["name"])
    op.create_index("idx_subcategories_category_id", "subcategories", ["category_id"])

    # ── products — drop old String brand/category columns ─────────────────────────
    op.drop_index(op.f("idx_products_brand"), table_name="products")
    op.drop_index(op.f("idx_products_category"), table_name="products")

    # ── products — add new Integer-ID + pricing/sales columns ─────────────────────
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
    # sap_product_id: String(100) matches the model declaration
    op.add_column(
        "products",
        sa.Column("sap_product_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("is_best_selling", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "products",
        sa.Column("best_selling_scope", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("sales_rank", sa.Integer(), nullable=True),
    )
    op.drop_column("products", "brand")
    op.drop_column("products", "category")

    # ── products — new B-tree indexes ─────────────────────────────────────────────
    op.create_index(
        "idx_products_brand_id", "products", ["brand_id"], unique=False
    )
    op.create_index(
        "idx_products_category_id", "products", ["category_id"], unique=False
    )
    op.create_index(
        "idx_products_subcategory_id", "products", ["subcategory_id"], unique=False
    )
    op.create_index(
        "idx_products_sap_product_id", "products", ["sap_product_id"], unique=False
    )

    # ── Drop old product_search_index (old schema: brand/category as strings) ──────
    # Its data has already been migrated into productsearchindex (new schema)
    # by the import_catalog.py / product_upload_service.py layer.
    # Drop individual indexes FIRST (inside batch context),
    # then drop the table AFTER closing the batch block.
    with op.batch_alter_table("product_search_index") as batch_op:
        batch_op.drop_index(op.f("ix_product_search_index_barcode"))
        batch_op.drop_index(op.f("ix_product_search_index_brand"))
        batch_op.drop_index(op.f("ix_product_search_index_category"))
        batch_op.drop_index(op.f("ix_product_search_index_id"))
        batch_op.drop_index(op.f("ix_product_search_index_item_code"))
        batch_op.drop_index(op.f("ix_product_search_index_item_name"))
        batch_op.drop_index(op.f("ix_product_search_index_product_id"))
        batch_op.drop_index(op.f("ix_product_search_index_search_text"))
        batch_op.drop_index(op.f("ix_product_search_index_subcategory"))
    # drop_table must be called OUTSIDE the batch_alter_table context
    op.drop_table("product_search_index")

    # ── productsearchindex — add FK-style loose-coupling columns (not FK constraints)
    # ─────────────────────────────────────────────────────────────────────────────
    with op.batch_alter_table("productsearchindex") as batch_op:
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
            "idx_productsearchindex_brand_id",
            ["brand_id"],
            unique=False,
        )
        batch_op.create_index(
            "idx_productsearchindex_category_id",
            ["category_id"],
            unique=False,
        )
        batch_op.create_index(
            "idx_productsearchindex_subcategory_id",
            ["subcategory_id"],
            unique=False,
        )

    # ── Drop orders table ─────────────────────────────────────────────────────────
    op.drop_index(op.f("ix_orders_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_order_id"), table_name="orders")
    op.drop_table("orders")


def downgrade() -> None:
    # ── Recreate orders table ────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(), nullable=True),      # ORD-{random_hex}
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("customer_name", sa.String(), nullable=False),
        sa.Column("customer_email", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),   # barcode or item_code
        sa.Column("product_name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),     # Python-side default=1
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

    # ── productsearchindex — drop FK-style loose-coupling columns ─────────────────
    with op.batch_alter_table("productsearchindex") as batch_op:
        batch_op.drop_index("idx_productsearchindex_subcategory_id")
        batch_op.drop_index("idx_productsearchindex_category_id")
        batch_op.drop_index("idx_productsearchindex_brand_id")
        batch_op.drop_column("subcategory_id")
        batch_op.drop_column("category_id")
        batch_op.drop_column("brand_id")

    # ── Recreate product_search_index (old schema) ──────────────────────────────
    op.create_table(
        "product_search_index",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),   # → products.barcode
        sa.Column("item_code", sa.String(), nullable=True),
        sa.Column("barcode", sa.String(), nullable=True),
        sa.Column("item_name", sa.Text(), nullable=False),
        sa.Column("brand", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("subcategory", sa.String(), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=False),    # concat blob for FTS
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_search_index_id", "product_search_index", ["id"])
    op.create_index(
        "ix_product_search_index_product_id",
        "product_search_index",
        ["product_id"],
        unique=True,
    )
    op.create_index(
        "ix_product_search_index_item_code",
        "product_search_index",
        ["item_code"],
    )
    op.create_index("ix_product_search_index_barcode", "product_search_index", ["barcode"])
    op.create_index("ix_product_search_index_item_name", "product_search_index", ["item_name"])
    op.create_index("ix_product_search_index_brand", "product_search_index", ["brand"])
    op.create_index("ix_product_search_index_category", "product_search_index", ["category"])
    op.create_index("ix_product_search_index_subcategory", "product_search_index", ["subcategory"])
    op.create_index("ix_product_search_index_search_text", "product_search_index", ["search_text"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_search_index_fts "
        "ON product_search_index USING gin (to_tsvector('english', search_text))"
    )

    # ── products — restore old columns ───────────────────────────────────────────
    op.add_column(
        "products",
        sa.Column("brand", sa.String(), autoincrement=False, nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("category", sa.String(), autoincrement=False, nullable=True),
    )
    op.drop_index(op.f("ix_products_subcategory_id"), table_name="products")
    op.drop_index(op.f("ix_products_sap_product_id"), table_name="products")
    op.drop_index(op.f("ix_products_category_id"), table_name="products")
    op.drop_index(op.f("ix_products_brand_id"), table_name="products")
    op.create_index("ix_products_brand", "products", ["brand"], unique=False)
    op.create_index("ix_products_category", "products", ["category"], unique=False)
    # Recreate GIN / trigram indexes on products that was dropped in upgrade()
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
    op.drop_column("products", "sales_rank")
    op.drop_column("products", "best_selling_scope")
    op.drop_column("products", "is_best_selling")
    op.drop_column("products", "subcategory_id")
    op.drop_column("products", "category_id")
    op.drop_column("products", "brand_id")
    op.drop_column("products", "sap_product_id")

    # ── brands / categories / subcategories indexes (non-fatal if already dropped) ─
    for idx_name, table, col in [
        ("idx_brands_name",              "brands",              ["name"]),
        ("idx_categories_name",          "categories",          ["name"]),
        ("idx_subcategories_category_id","subcategories",       ["category_id"]),
    ]:
        op.execute(f"DROP INDEX IF EXISTS {idx_name}")

    # ── Drop search_text GIN (non-fatal) ─────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_search_text_gin")
