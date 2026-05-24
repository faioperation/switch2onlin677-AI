"""add_bundle_tables

Revision ID: 20260525002a
Revises: 20260525001a
Create Date: 2026-05-25

What this adds
--------------
Replaces the flat bundle_group / bundle_discount_percent columns on products
(which are kept for backward compat) with a proper two-table design:

  bundles
  -------
  The canonical record for each bundle.

  Columns
    id               SERIAL PRIMARY KEY
    bundle_code      VARCHAR(100) UNIQUE NOT NULL     URL-safe slug for API URLs
    name             VARCHAR(255) NOT NULL
    name_ar          VARCHAR(255)
    description      TEXT
    image_url        VARCHAR(500)
    discount_percent NUMERIC(5,2)                    NULL = no bundle discount
    is_active        BOOLEAN NOT NULL DEFAULT TRUE
    sort_order       INTEGER NOT NULL DEFAULT 0       lower = shown first
    valid_from       TIMESTAMP                        NULL = always valid
    valid_until      TIMESTAMP                        NULL = no expiry
    created_at       TIMESTAMP DEFAULT now()
    updated_at       TIMESTAMP

  bundle_items
  ------------
  Join table between bundles and products (barcode).

  Columns
    id          SERIAL PRIMARY KEY
    bundle_id   INTEGER NOT NULL                     → bundles.id (no FK, loose coupling)
    barcode     VARCHAR(100) NOT NULL                → products.barcode (no FK)
    quantity    INTEGER NOT NULL DEFAULT 1           units of product in bundle
    sort_order  INTEGER NOT NULL DEFAULT 0           display order within bundle
    is_anchor   BOOLEAN NOT NULL DEFAULT FALSE       hero product of the bundle
    created_at  TIMESTAMP DEFAULT now()

  Indexes (all IF NOT EXISTS for safe re-runs)
  --------------------------------------------
  idx_bundles_bundle_code        bundles(bundle_code)           already implicit on UNIQUE
  idx_bundles_active_sort        bundles(is_active, sort_order)
  idx_bundle_items_bundle_sort   bundle_items(bundle_id, sort_order)
  idx_bundle_items_bundle_barcode bundle_items(bundle_id, barcode) UNIQUE
  idx_bundle_items_barcode       bundle_items(barcode)

Downgrade
---------
Drops both tables and all their indexes.
The products.bundle_group and products.bundle_discount_percent columns are NOT
touched — they remain for backward compatibility with the old recommendation
endpoint.
"""

from alembic import op
import sqlalchemy as sa

revision      = "20260525002a"
down_revision = "20260525001a"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── bundles ───────────────────────────────────────────────────────────────
    op.create_table(
        "bundles",
        sa.Column("id",               sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("bundle_code",      sa.String(100),    nullable=False),
        sa.Column("name",             sa.String(255),    nullable=False),
        sa.Column("name_ar",          sa.String(255),    nullable=True),
        sa.Column("description",      sa.Text(),         nullable=True),
        sa.Column("image_url",        sa.String(500),    nullable=True),
        sa.Column("discount_percent", sa.Numeric(5, 2),  nullable=True),
        sa.Column("is_active",        sa.Boolean(),      nullable=False, server_default=sa.text("TRUE")),
        sa.Column("sort_order",       sa.Integer(),      nullable=False, server_default=sa.text("0")),
        sa.Column("valid_from",       sa.DateTime(),     nullable=True),
        sa.Column("valid_until",      sa.DateTime(),     nullable=True),
        sa.Column("created_at",       sa.DateTime(),     server_default=sa.func.now()),
        sa.Column("updated_at",       sa.DateTime(),     onupdate=sa.func.now()),
    )

    op.execute(
        "ALTER TABLE bundles ADD CONSTRAINT uq_bundles_bundle_code UNIQUE (bundle_code)"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundles_active_sort "
        "ON bundles (is_active, sort_order)"
    )

    # ── bundle_items ──────────────────────────────────────────────────────────
    op.create_table(
        "bundle_items",
        sa.Column("id",         sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("bundle_id",  sa.Integer(),    nullable=False),
        sa.Column("barcode",    sa.String(100),  nullable=False),
        sa.Column("quantity",   sa.Integer(),    nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer(),    nullable=False, server_default=sa.text("0")),
        sa.Column("is_anchor",  sa.Boolean(),    nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.DateTime(),   server_default=sa.func.now()),
    )

    # Primary access: items of a bundle in display order
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundle_items_bundle_sort "
        "ON bundle_items (bundle_id, sort_order)"
    )

    # Uniqueness: a product appears at most once per bundle
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bundle_items_bundle_barcode "
        "ON bundle_items (bundle_id, barcode)"
    )

    # Reverse lookup: all bundles a product belongs to
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundle_items_barcode "
        "ON bundle_items (barcode)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_bundle_items_barcode")
    op.execute("DROP INDEX IF EXISTS idx_bundle_items_bundle_barcode")
    op.execute("DROP INDEX IF EXISTS idx_bundle_items_bundle_sort")
    op.drop_table("bundle_items")

    op.execute("DROP INDEX IF EXISTS idx_bundles_active_sort")
    op.drop_table("bundles")
