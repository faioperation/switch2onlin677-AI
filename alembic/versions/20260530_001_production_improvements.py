"""production_improvements

Revision ID: 20260530001a
Revises: 20260525003a
Create Date: 2026-05-30

What this adds
--------------
1. upload_jobs
   Tracks every async product-upload job: status, progress, row counts, errors,
   timing.  One row per POST /products/upload call.

2. sap_sync_audit_log
   Immutable record of every SAP sync run: item counts, price-protected count,
   duration, success/failure.

3. products.deleted_at  (soft delete)
   NULL = live product.  Non-NULL = tombstone timestamp.  Hard DELETE is gone;
   restore sets deleted_at back to NULL.  A partial index covers the rare query
   that looks up deleted products.

4. products.price_source_override  (SAP price protection)
   When TRUE, the bi-daily SAP sync skips updating price for that product,
   letting the manually uploaded or API-set price persist across sync cycles.

No existing columns are dropped or modified.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision      = "20260530001a"
down_revision = "20260525003a"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── 1. upload_jobs ────────────────────────────────────────────────────────
    op.create_table(
        "upload_jobs",
        sa.Column("id",              sa.String(36),      primary_key=True),
        sa.Column("filename",        sa.String(500),     nullable=False),
        sa.Column("status",          sa.String(20),      nullable=False, server_default=sa.text("'queued'")),
        sa.Column("dry_run",         sa.Boolean(),       nullable=False, server_default=sa.text("false")),
        sa.Column("total_rows",      sa.Integer(),       nullable=True),
        sa.Column("processed_rows",  sa.Integer(),       nullable=False, server_default=sa.text("0")),
        sa.Column("created_count",   sa.Integer(),       nullable=False, server_default=sa.text("0")),
        sa.Column("updated_count",   sa.Integer(),       nullable=False, server_default=sa.text("0")),
        sa.Column("skipped_count",   sa.Integer(),       nullable=False, server_default=sa.text("0")),
        sa.Column("error_count",     sa.Integer(),       nullable=False, server_default=sa.text("0")),
        sa.Column("error_details",   postgresql.JSONB(), nullable=True),
        sa.Column("error_message",   sa.Text(),          nullable=True),
        sa.Column("started_at",      sa.DateTime(),      nullable=True),
        sa.Column("completed_at",    sa.DateTime(),      nullable=True),
        sa.Column("execution_seconds", sa.Numeric(10, 2), nullable=True),
        sa.Column("created_at",      sa.DateTime(),      nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_upload_jobs_status",     "upload_jobs", ["status"])
    op.create_index("idx_upload_jobs_created_at", "upload_jobs", ["created_at"])

    # ── 2. sap_sync_audit_log ─────────────────────────────────────────────────
    op.create_table(
        "sap_sync_audit_log",
        sa.Column("id",                    sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("synced_at",             sa.DateTime(),   nullable=False, server_default=sa.func.now()),
        sa.Column("status",                sa.String(20),   nullable=False),
        sa.Column("items_received",        sa.Integer(),    nullable=False, server_default=sa.text("0")),
        sa.Column("items_updated",         sa.Integer(),    nullable=False, server_default=sa.text("0")),
        sa.Column("items_not_found",       sa.Integer(),    nullable=False, server_default=sa.text("0")),
        sa.Column("items_skipped",         sa.Integer(),    nullable=False, server_default=sa.text("0")),
        sa.Column("items_price_protected", sa.Integer(),    nullable=False, server_default=sa.text("0")),
        sa.Column("duration_seconds",      sa.Numeric(10, 2), nullable=True),
        sa.Column("error_message",         sa.Text(),       nullable=True),
    )
    op.create_index("idx_sap_sync_audit_synced_at", "sap_sync_audit_log", ["synced_at"])
    op.create_index("idx_sap_sync_audit_status",    "sap_sync_audit_log", ["status"])

    # ── 3. products.deleted_at ────────────────────────────────────────────────
    op.add_column("products", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    # Partial index only on non-NULL rows (tombstones) — saves space since
    # the overwhelming majority of rows have deleted_at = NULL.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_deleted_at "
        "ON products (deleted_at) "
        "WHERE deleted_at IS NOT NULL"
    )

    # ── 4. products.price_source_override ─────────────────────────────────────
    op.add_column(
        "products",
        sa.Column(
            "price_source_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_price_override "
        "ON products (price_source_override) "
        "WHERE price_source_override = true"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_products_price_override")
    op.drop_column("products", "price_source_override")
    op.execute("DROP INDEX IF EXISTS idx_products_deleted_at")
    op.drop_column("products", "deleted_at")
    op.drop_index("idx_sap_sync_audit_status",    "sap_sync_audit_log")
    op.drop_index("idx_sap_sync_audit_synced_at", "sap_sync_audit_log")
    op.drop_table("sap_sync_audit_log")
    op.drop_index("idx_upload_jobs_created_at", "upload_jobs")
    op.drop_index("idx_upload_jobs_status",     "upload_jobs")
    op.drop_table("upload_jobs")
