"""
core/database.py
================
SQLAlchemy engine, session factory, and FastAPI dependency.

Supports PostgreSQL (production) and SQLite (local dev fallback).
Imported by: main.py, all routers, services, repositories, alembic/env.py (via shim).
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

_database_url = os.getenv("DATABASE_URL")

if _database_url:
    engine = create_engine(_database_url)
    logger.info("database_ready engine=postgresql")
else:
    engine = create_engine(
        "sqlite:///./shopbot.db",
        connect_args={"check_same_thread": False},
    )
    logger.warning("database_ready engine=sqlite path=./shopbot.db")

SessionLocal = sessionmaker(bind=engine)
Base         = declarative_base()


def get_db():
    """FastAPI dependency — yields a scoped DB session and closes it after the request."""
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
