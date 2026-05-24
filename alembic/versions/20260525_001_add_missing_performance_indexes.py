"""add_missing_performance_indexes

Revision ID: 20260525001a
Revises: 20260522001a
Create Date: 2026-05-25

What this adds
--------------
products table — 4 B-tree indexes for the list/sort/filter queries:

  idx_products_created_at
      products(created_at DESC)
      Every GET /products call sorts by created_at DESC (the default).
      Without this, every list page triggers a full table scan + sort.

  idx_products_price
      products(price)
      Used by min_price/max_price range filters and price_asc/price_desc sort.

  idx_products_available_qty
      products(available_qty)
      Used by the in_stock=true filter (available_qty > 0) in list_products.

  idx_products_sales_rank
      products(sales_rank)
      ORDER BY sales_rank ASC NULLS LAST in get_best_selling().

products table — 2 partial composite indexes for recommendation queries
that had no covering index:

  idx_products_cod_recommended_active
      products(is_cod_recommended, category_id, recommendation_priority)
      WHERE is_cod_recommended = TRUE AND product_status = 'active'
            AND available_qty > 5
      Powers get_cod_recommended() — previously fell back to the generic
      product_status index with a slow filter pass.

  idx_products_brand_family_active
      products(brand_family, category_id, recommendation_priority)
      WHERE product_status = 'active' AND available_qty > 5
      Powers get_by_brand_family() — the existing idx_products_brand_family
      (plain B-tree) has no awareness of the mandatory active/qty filters,
      so the planner had to scan all brand_family matches and filter after.

productsearchindex table — 1 GIN trigram index:

  idx_psi_search_text_trgm
      productsearchindex(search_text) USING gin (search_text gin_trgm_ops)
      The search query in list_products() uses an OR of:
        (a) search_text.ilike('%...%')   ← needs trigram
        (b) to_tsvector('english', search_text) @@ plainto_tsquery(...)
                                         ← already has idx_product_search_index_fts
      Without the trigram index branch (a) forces a sequential scan.
      Requires the pg_trgm extension (already enabled in baseline migration).

All indexes use CREATE INDEX IF NOT EXISTS so they are safe to re-run
on a database that may already have some created manually.
"""

from alembic import op

revision = "20260525001a"
down_revision = "20260522001a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── B-tree indexes for sort / range filters ───────────────────────────────

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_created_at "
        "ON products (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_price "
        "ON products (price)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_available_qty "
        "ON products (available_qty)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_sales_rank "
        "ON products (sales_rank ASC NULLS LAST)"
    )

    # ── Partial composite indexes for recommendation queries ──────────────────

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_products_cod_recommended_active
        ON products (is_cod_recommended, category_id, recommendation_priority)
        WHERE is_cod_recommended = TRUE
          AND product_status = 'active'
          AND available_qty > 5
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_products_brand_family_active
        ON products (brand_family, category_id, recommendation_priority)
        WHERE product_status = 'active'
          AND available_qty > 5
    """)

    # ── GIN trigram index on search_text (ILIKE acceleration) ─────────────────
    # pg_trgm was enabled in the baseline migration (b4f3a1d2e891).
    # This index turns ILIKE '%...%' queries from sequential scans into
    # bitmap index scans on the productsearchindex table.

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_psi_search_text_trgm
        ON productsearchindex USING gin (search_text gin_trgm_ops)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_psi_search_text_trgm")
    op.execute("DROP INDEX IF EXISTS idx_products_brand_family_active")
    op.execute("DROP INDEX IF EXISTS idx_products_cod_recommended_active")
    op.execute("DROP INDEX IF EXISTS idx_products_sales_rank")
    op.execute("DROP INDEX IF EXISTS idx_products_available_qty")
    op.execute("DROP INDEX IF EXISTS idx_products_price")
    op.execute("DROP INDEX IF EXISTS idx_products_created_at")
