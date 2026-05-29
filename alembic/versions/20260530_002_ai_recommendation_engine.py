"""ai_recommendation_engine

Revision ID: 20260530002a
Revises: 20260530001a
Create Date: 2026-05-30

What this adds
--------------

1. products.embedding          VECTOR(1536)
   Stores the OpenAI text-embedding-3-small vector for each product.
   NULL until the background embedding job runs.

2. products.embedding_text     TEXT
   The concatenated input text that was embedded.  Stored for debugging,
   cache validation, and re-embedding detection (if text changes → re-embed).

3. products.embedding_updated_at  DATETIME
   Timestamp of the last successful embedding.  The background job uses this
   to find stale entries (embedding_text changed since embedding_updated_at).

4. products.ai_score           NUMERIC(8,6)
   Cached composite AI score (0–1).  Updated by the scoring pipeline after
   each recommendation query that touches the product.  Used as a tiebreaker
   in recommendation queries to avoid recalculating every time.

5. product_events              (behavioral feedback loop)
   Append-only event stream — one row per user interaction:
     - view, click, purchase, recommendation_accepted, recommendation_rejected

6. user_preference_profiles    (per-user preference state)
   One row per user_id.  Stores:
     - embedding   VECTOR(1536)  — mean of all products the user interacted with
     - preferred_categories / brands / price_tiers  JSONB frequency maps
     - last_updated

7. HNSW vector index on products.embedding
   ivfflat works for < 1M rows; use HNSW from pgvector >= 0.5.0.
   ef_construction=64, m=16 are solid defaults for 100k product catalogs.
   Allows sub-millisecond approximate nearest-neighbour queries.

8. IVFFlat fallback index note
   If pgvector < 0.5.0, swap the CREATE INDEX statement in upgrade() below
   to use:
     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
   and run   ANALYZE products;   after loading > 10k rows.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision      = "20260530002a"
down_revision = "20260530001a"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── 1. Embedding columns on products ──────────────────────────────────────
    # VECTOR type is registered by pgvector — we emit raw SQL so Alembic
    # doesn't need to know about UserDefinedType.
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS embedding VECTOR(1536)")
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS embedding_text TEXT")
    op.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMP")
    op.execute(
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
        "ai_score NUMERIC(8,6) DEFAULT 0.0"
    )

    # Index for finding un-embedded / stale products efficiently
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_embedding_null "
        "ON products (barcode) "
        "WHERE embedding IS NULL"
    )

    # ── 2. HNSW vector index ──────────────────────────────────────────────────
    # Requires pgvector >= 0.5.0.  Safe to call even if no rows exist yet.
    # This index enables cosine-similarity nearest-neighbour queries:
    #   ORDER BY embedding <=> query_vector LIMIT k
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_embedding_hnsw "
        "ON products USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # ── 3. product_events ─────────────────────────────────────────────────────
    op.create_table(
        "product_events",
        sa.Column("id",           sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column("user_id",      sa.String(255),   nullable=True,  index=True),
        sa.Column("session_id",   sa.String(255),   nullable=True,  index=True),
        sa.Column("barcode",      sa.String(100),   nullable=False, index=True),
        sa.Column("event_type",   sa.String(50),    nullable=False),
        # view | click | purchase | recommendation_accepted | recommendation_rejected
        sa.Column("source",       sa.String(50),    nullable=True),
        # where the event came from: chatbot | api | frontend | recommendation
        sa.Column("position",     sa.Integer(),     nullable=True),
        # rank position in the recommendation list (for CTR analysis)
        sa.Column("metadata",     postgresql.JSONB(), nullable=True),
        # extra context: query text, recommendation type, ab_group, etc.
        sa.Column("created_at",   sa.DateTime(),    nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_events_user_barcode "
        "ON product_events (user_id, barcode, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_events_barcode_type "
        "ON product_events (barcode, event_type, created_at DESC)"
    )

    # ── 4. user_preference_profiles ──────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_preference_profiles (
            user_id              VARCHAR(255) PRIMARY KEY,
            embedding            VECTOR(1536),
            preferred_categories JSONB DEFAULT '{}',
            preferred_brands     JSONB DEFAULT '{}',
            preferred_price_tiers JSONB DEFAULT '{}',
            preferred_skin_types JSONB DEFAULT '{}',
            total_events         INTEGER DEFAULT 0,
            last_updated         TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_prefs_embedding "
        "ON user_preference_profiles USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_user_prefs_embedding")
    op.execute("DROP TABLE IF EXISTS user_preference_profiles")
    op.execute("DROP INDEX IF EXISTS idx_product_events_barcode_type")
    op.execute("DROP INDEX IF EXISTS idx_product_events_user_barcode")
    op.drop_table("product_events")
    op.execute("DROP INDEX IF EXISTS idx_products_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_products_embedding_null")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS ai_score")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS embedding_updated_at")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS embedding_text")
    op.execute("ALTER TABLE products DROP COLUMN IF EXISTS embedding")
