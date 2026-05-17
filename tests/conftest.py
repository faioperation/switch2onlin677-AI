"""
tests/conftest.py
=================
Shared pytest fixtures for catalog API tests.

Strategy
--------
- A fresh SQLite in-memory database is created per test function — guarantees
  total isolation with zero cleanup boilerplate.
- JSONB and pgvector.Vector are patched to TEXT for SQLite compatibility
  (registered before any model import so they take effect at CREATE TABLE time).
- Tests do NOT import main.py, avoiding the module-level
  `Base.metadata.create_all(bind=engine)` call that would fail in SQLite for
  the KnowledgeChunk/Vector table.

Running
-------
    pip install pytest httpx
    pytest tests/ -v
"""

# ── SQLite compatibility patches (must run before any model import) ────────────
from sqlalchemy.ext.compiler import compiles as _compiles
from sqlalchemy.dialects.postgresql import JSONB as _JSONB


@_compiles(_JSONB, "sqlite")
def _jsonb_as_text(type_, compiler, **kw):
    """Render JSONB as TEXT for SQLite test databases."""
    return "TEXT"


try:
    from pgvector.sqlalchemy import Vector as _Vector

    @_compiles(_Vector, "sqlite")
    def _vector_as_text(type_, compiler, **kw):
        """Render pgvector Vector as TEXT for SQLite test databases."""
        return "TEXT"

except Exception:
    pass  # pgvector not installed or already patched

# ── Imports ───────────────────────────────────────────────────────────────────

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Import database.py for Base + get_db
from database import Base, get_db

# Import models to register all ORM classes with Base.metadata
import models  # noqa: F401

# Import the three new routers under test
from routers.categories import router as categories_router
from routers.brands import router as brands_router
from routers.subcategories import router as subcategories_router

# ── Tables to create in SQLite (excludes KnowledgeChunk which uses Vector) ────
_CATALOG_TABLES = [
    models.Brand.__table__,
    models.Category.__table__,
    models.Subcategory.__table__,
    models.Product.__table__,
]

# ── Minimal FastAPI app (avoids main.py's module-level create_all) ────────────
_test_app = FastAPI()
_test_app.include_router(categories_router, prefix="/api/v1")
_test_app.include_router(brands_router, prefix="/api/v1")
_test_app.include_router(subcategories_router, prefix="/api/v1")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def db():
    """
    Provide a SQLAlchemy Session backed by a fresh in-memory SQLite database.

    A new engine + database is created for every test function, giving
    complete isolation without needing explicit cleanup.

    StaticPool is required: FastAPI runs endpoint handlers in worker threads.
    Without StaticPool, each thread opens a fresh SQLite connection that
    doesn't see the tables created on the setup thread's connection.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=_CATALOG_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="function")
def client(db):
    """
    Provide a TestClient with the get_db dependency overridden to use the
    in-memory SQLite session from the `db` fixture.
    """

    def _override():
        yield db

    _test_app.dependency_overrides[get_db] = _override
    with TestClient(_test_app) as c:
        yield c
    _test_app.dependency_overrides.clear()
