"""add_is_active_indexes_on_brands_and_categories

Revision ID: 20260518001a
Revises: 013065484c78
Create Date: 2026-05-18

Adds indexes on the `is_active` column for brands and categories to support
efficient filtering in the catalog list endpoints:

  GET /api/v1/categories?is_active=true
  GET /api/v1/brands?is_active=true

These are the only missing production-scale indexes required by the new
catalog API endpoints. All other needed indexes already exist:

  ✓ idx_brands_name           (brands.name)
  ✓ idx_categories_name       (categories.name)
  ✓ idx_subcategories_category_id
  ✓ idx_products_brand_id
  ✓ idx_products_category_id
  ✓ idx_products_subcategory_id

Note: Composite (business_id, is_active) / (business_id, name) indexes are
deferred until business_id is added to the schema as part of multi-tenancy work.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260518001a"
down_revision: Union[str, None] = "013065484c78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_categories_is_active",
        "categories",
        ["is_active"],
        unique=False,
    )
    op.create_index(
        "idx_brands_is_active",
        "brands",
        ["is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_brands_is_active", table_name="brands")
    op.drop_index("idx_categories_is_active", table_name="categories")
