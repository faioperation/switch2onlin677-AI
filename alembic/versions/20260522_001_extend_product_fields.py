"""extend_product_fields_for_recommendation_and_bundling

Revision ID: 20260522001a
Revises: 20260519001a
Create Date: 2026-05-22

Adds to products table:
  - price_tier        (enum: Budget/Mid/Premium/Luxury)
  - brand_family      (string)
  - product_status    (enum: active/inactive/draft)
  - is_new_arrival    (boolean)
  - is_recommended    (boolean)
  - is_cod_recommended(boolean)
  - recommendation_priority       (integer)
  - recommendation_score_override (decimal)
  - bundle_group           (string)
  - bundle_discount_percent(decimal)

Also migrates:
  - is_best_selling: Integer(0/1) → Boolean
"""

from alembic import op
import sqlalchemy as sa

revision = "20260522001a"
down_revision = "20260519001a"
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Create enum types ───────────────────────────────────────────────────
    op.execute("CREATE TYPE price_tier_enum AS ENUM ('Budget', 'Mid', 'Premium', 'Luxury')")
    op.execute("CREATE TYPE product_status_enum AS ENUM ('active', 'inactive', 'draft')")

    # ── 2. Add new columns ────────────────────────────────────────────────────
    op.add_column("products", sa.Column(
        "price_tier",
        sa.Enum("Budget", "Mid", "Premium", "Luxury",
                name="price_tier_enum", create_type=False),
        nullable=True,
    ))
    op.add_column("products", sa.Column(
        "brand_family", sa.String(100), nullable=True,
    ))
    op.add_column("products", sa.Column(
        "product_status",
        sa.Enum("active", "inactive", "draft",
                name="product_status_enum", create_type=False),
        nullable=False,
        server_default="active",
    ))
    op.add_column("products", sa.Column(
        "is_new_arrival", sa.Boolean, nullable=True, server_default="false",
    ))
    op.add_column("products", sa.Column(
        "is_recommended", sa.Boolean, nullable=True, server_default="false",
    ))
    op.add_column("products", sa.Column(
        "is_cod_recommended", sa.Boolean, nullable=True, server_default="false",
    ))
    op.add_column("products", sa.Column(
        "recommendation_priority", sa.Integer, nullable=True, server_default="0",
    ))
    op.add_column("products", sa.Column(
        "recommendation_score_override", sa.Numeric(5, 2), nullable=True,
    ))
    op.add_column("products", sa.Column(
        "bundle_group", sa.String(100), nullable=True,
    ))
    op.add_column("products", sa.Column(
        "bundle_discount_percent", sa.Numeric(5, 2), nullable=True,
    ))

    # ── 3. Migrate is_best_selling: Integer → Boolean ─────────────────────────
    op.execute("""
        ALTER TABLE products
        ALTER COLUMN is_best_selling TYPE BOOLEAN
        USING CASE WHEN is_best_selling = 1 THEN TRUE ELSE FALSE END
    """)
    op.execute("ALTER TABLE products ALTER COLUMN is_best_selling SET DEFAULT FALSE")

    # ── 4. Indexes ────────────────────────────────────────────────────────────
    op.execute("CREATE INDEX idx_products_price_tier ON products (price_tier)")
    op.execute("CREATE INDEX idx_products_brand_family ON products (brand_family)")
    op.execute("CREATE INDEX idx_products_product_status ON products (product_status)")
    op.execute("CREATE INDEX idx_products_recommendation_priority ON products (recommendation_priority)")
    op.execute("CREATE INDEX idx_products_bundle_group ON products (bundle_group)")

    # Partial composite index — used by recommendation engine
    op.execute("""
        CREATE INDEX idx_products_recommendation_filter
        ON products (product_status, is_recommended, recommendation_priority)
        WHERE product_status = 'active' AND available_qty > 5
    """)
    op.execute("""
        CREATE INDEX idx_products_best_selling_active
        ON products (is_best_selling, category_id)
        WHERE is_best_selling = TRUE AND product_status = 'active'
    """)
    op.execute("""
        CREATE INDEX idx_products_new_arrival_active
        ON products (is_new_arrival, created_at)
        WHERE is_new_arrival = TRUE AND product_status = 'active'
    """)
    op.execute("""
        CREATE INDEX idx_products_tier_category
        ON products (price_tier, category_id)
        WHERE product_status = 'active'
    """)


def downgrade():
    # ── Drop indexes ──────────────────────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS idx_products_tier_category")
    op.execute("DROP INDEX IF EXISTS idx_products_new_arrival_active")
    op.execute("DROP INDEX IF EXISTS idx_products_best_selling_active")
    op.execute("DROP INDEX IF EXISTS idx_products_recommendation_filter")
    op.execute("DROP INDEX IF EXISTS idx_products_bundle_group")
    op.execute("DROP INDEX IF EXISTS idx_products_recommendation_priority")
    op.execute("DROP INDEX IF EXISTS idx_products_product_status")
    op.execute("DROP INDEX IF EXISTS idx_products_brand_family")
    op.execute("DROP INDEX IF EXISTS idx_products_price_tier")

    # ── Drop new columns ──────────────────────────────────────────────────────
    for col in [
        "bundle_discount_percent",
        "bundle_group",
        "recommendation_score_override",
        "recommendation_priority",
        "is_cod_recommended",
        "is_recommended",
        "is_new_arrival",
        "product_status",
        "brand_family",
        "price_tier",
    ]:
        op.drop_column("products", col)

    # ── Revert is_best_selling: Boolean → Integer ─────────────────────────────
    op.execute("""
        ALTER TABLE products
        ALTER COLUMN is_best_selling TYPE INTEGER
        USING CASE WHEN is_best_selling THEN 1 ELSE 0 END
    """)
    op.execute("ALTER TABLE products ALTER COLUMN is_best_selling SET DEFAULT 0")

    # ── Drop enum types ───────────────────────────────────────────────────────
    op.execute("DROP TYPE IF EXISTS product_status_enum")
    op.execute("DROP TYPE IF EXISTS price_tier_enum")
