"""human_agent_handoff

Revision ID: 20260608001a
Revises: 20260531001a
Create Date: 2026-06-08

What this adds
--------------
conversation_handoffs — one row per user_id, tracks the lifecycle state
of the AI-to-human transfer pipeline.

State machine
    ai_active → pending_human → human_handling → resolved
                                               ↘ ai_active  (resume_ai)

Columns
-------
id                  — surrogate PK
user_id             — unique: one active handoff record per user
status              — handoff_status_enum value (see below)
assigned_agent_id   — agent who accepted the conversation (NULL until assigned)
transfer_reason     — keyword / reason code logged at transfer time
ai_confidence_score — intent confidence score at moment of transfer (0.0–1.0)
ai_disabled         — denormalised flag: True when status != ai_active
transferred_at      — when the conversation was first escalated
resolved_at         — when the conversation was marked resolved
created_at          — row creation timestamp
updated_at          — last update timestamp

Enum
----
handoff_status_enum — PostgreSQL ENUM type with values:
    ai_active | pending_human | human_handling | resolved

Indexes
-------
idx_handoff_status        — filter by status (agent queue queries)
idx_handoff_agent         — lookup conversations by agent_id
idx_handoff_user_status   — composite (user_id, status) for hot-path check

Safety
------
All DDL statements are idempotent.  The enum and table are guarded with
IF NOT EXISTS / DO-$$ blocks so re-running the migration (e.g. on a
database where manual hotfixes were applied) does not fail.
"""

from alembic import op
import sqlalchemy as sa

revision      = "20260608001a"
down_revision = "20260531001a"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── 1. Create the ENUM type ───────────────────────────────────────────────
    # create_type=False on the ORM model means we create it here manually.
    # The DO-$$ block makes it idempotent.
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE handoff_status_enum
                AS ENUM ('ai_active', 'pending_human', 'human_handling', 'resolved');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # ── 2. Create conversation_handoffs table ─────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS conversation_handoffs (
            id                  SERIAL          PRIMARY KEY,
            user_id             VARCHAR(255)    NOT NULL,
            status              handoff_status_enum NOT NULL DEFAULT 'ai_active',
            assigned_agent_id   VARCHAR(255),
            transfer_reason     VARCHAR(500),
            ai_confidence_score NUMERIC(5, 4),
            ai_disabled         BOOLEAN         NOT NULL DEFAULT FALSE,
            transferred_at      TIMESTAMP,
            resolved_at         TIMESTAMP,
            created_at          TIMESTAMP       NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMP,
            CONSTRAINT uq_handoff_user_id UNIQUE (user_id)
        );
    """)

    # ── 3. Indexes ────────────────────────────────────────────────────────────
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_handoff_status "
        "ON conversation_handoffs (status);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_handoff_agent "
        "ON conversation_handoffs (assigned_agent_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_handoff_user_status "
        "ON conversation_handoffs (user_id, status);"
    )

    # ── 4. updated_at trigger (PostgreSQL) ────────────────────────────────────
    # Automatically sets updated_at on every UPDATE so we don't depend on
    # SQLAlchemy's onupdate= (which only fires through the ORM, not raw SQL).
    op.execute("""
        CREATE OR REPLACE FUNCTION _set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_handoff_updated_at'
            ) THEN
                CREATE TRIGGER trg_handoff_updated_at
                    BEFORE UPDATE ON conversation_handoffs
                    FOR EACH ROW EXECUTE FUNCTION _set_updated_at();
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Drop trigger first, then table, then enum
    op.execute(
        "DROP TRIGGER IF EXISTS trg_handoff_updated_at ON conversation_handoffs;"
    )
    op.execute("DROP TABLE IF EXISTS conversation_handoffs;")
    op.execute("DROP TYPE  IF EXISTS handoff_status_enum;")
    # Note: _set_updated_at() function is shared — do NOT drop it here as
    # other tables may add triggers to it in future migrations.
