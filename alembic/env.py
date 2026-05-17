import os
from logging.config import fileConfig
from typing import Optional

from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv

# ── 1. Load .env FIRST ────────────────────────────────────────────────────────
# Must happen before `from database import Base` so that database.py picks up
# DATABASE_URL from the environment instead of falling back to SQLite.
load_dotenv()

database_url: Optional[str] = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set.\n"
        "Alembic requires a PostgreSQL connection. Example:\n"
        "  export DATABASE_URL=postgresql://user:password@localhost/dhifafbot"
    )

# ── 2. Alembic config object ──────────────────────────────────────────────────
config = context.config

# Override the (intentionally empty) sqlalchemy.url in alembic.ini
config.set_main_option("sqlalchemy.url", database_url)

# Wire up Python logging from alembic.ini [loggers] / [handlers] / [formatters]
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── 3. Project imports — AFTER dotenv is loaded ───────────────────────────────
# Importing database.py triggers create_engine(); it will now see DATABASE_URL.
# Importing models registers all ORM classes with Base.metadata so that
# `alembic revision --autogenerate` can diff against the live schema.
from database import Base   # noqa: E402
import models               # noqa: E402, F401 — side-effect: registers ORM models

target_metadata = Base.metadata


# ── 4. Migration runners ──────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL to stdout).

    Useful for reviewing SQL before applying:
        alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Include schema-level objects in autogenerate comparisons
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: each migration run gets its own connection; no pool kept open.
        # Correct for one-shot CLI usage; do NOT use NullPool in the app server.
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
