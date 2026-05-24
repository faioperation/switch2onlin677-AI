"""add_product_status_log

Revision ID: 20260525003a
Revises: 20260525002a
Create Date: 2026-05-25

What this adds
--------------
product_status_log
  Append-only audit table for every product_status transition.

  One row is inserted whenever a product's status changes
  (draft → active, active → inactive, inactive → active, etc.).
  Rows are NEVER updated or deleted.

  Columns
    id          SERIAL PRIMARY KEY
    barcode     VARCHAR(100) NOT NULL    → products.barcode (loose coupling)
    from_status VARCHAR(20)              NULL = first-ever assignment
    to_status   VARCHAR(20)  NOT NULL
    changed_by  VARCHAR(255) NOT NULL    user ID, API key label, or "system"
    reason      TEXT                     optional free-text note
    changed_at  TIMESTAMP    NOT NULL DEFAULT now()

  Indexes
    idx_psl_barcode_changed_at    product_status_log(barcode, changed_at)
        Primary query: full history for one product, newest first.

    idx_psl_to_status_changed_at  product_status_log(to_status, changed_at)
        Dashboard query: all deactivations today / all publishes this week.

No existing columns are modified.
The products table's product_status column is unchanged; this migration
only adds the log table.
"""

from alembic import op
import sqlalchemy as sa

revision      = "20260525003a"
down_revision = "20260525002a"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.create_table(
        "product_status_log",
        sa.Column("id",          sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column("barcode",     sa.String(100),   nullable=False),
        sa.Column("from_status", sa.String(20),    nullable=True),
        sa.Column("to_status",   sa.String(20),    nullable=False),
        sa.Column("changed_by",  sa.String(255),   nullable=False, server_default=sa.text("'system'")),
        sa.Column("reason",      sa.Text(),         nullable=True),
        sa.Column("changed_at",  sa.DateTime(),     nullable=False, server_default=sa.func.now()),
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_psl_barcode_changed_at "
        "ON product_status_log (barcode, changed_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_psl_to_status_changed_at "
        "ON product_status_log (to_status, changed_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_psl_to_status_changed_at")
    op.execute("DROP INDEX IF EXISTS idx_psl_barcode_changed_at")
    op.drop_table("product_status_log")
