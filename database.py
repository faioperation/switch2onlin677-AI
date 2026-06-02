"""
database.py — backward-compatibility shim
==========================================
Engine, session, and Base have moved to core/database.py.
This file re-exports them so that all existing imports continue to work:
    from database import engine, SessionLocal, Base, get_db

Alembic's env.py also imports `from database import Base` — this shim
ensures that works without touching the migration configuration.
"""
from core.database import Base, SessionLocal, engine, get_db  # noqa: F401
