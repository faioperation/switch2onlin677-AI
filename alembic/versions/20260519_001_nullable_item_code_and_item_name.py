"""nullable_item_code_and_item_name

Revision ID: 20260519001a
Revises: 20260518001a
Create Date: 2026-05-19

Makes products.item_code and products.item_name nullable so that Excel
uploads can store NULL for those fields when only barcode is provided.
"""

from alembic import op

revision = "20260519001a"
down_revision = "20260518001a"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE products ALTER COLUMN item_code DROP NOT NULL")
    op.execute("ALTER TABLE products ALTER COLUMN item_name DROP NOT NULL")


def downgrade():
    op.execute("ALTER TABLE products ALTER COLUMN item_code SET NOT NULL")
    op.execute("ALTER TABLE products ALTER COLUMN item_name SET NOT NULL")
