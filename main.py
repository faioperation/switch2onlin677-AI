"""
main.py
=======
DhifafBot FastAPI application entry point.

Responsibilities (ONLY):
  - App and middleware initialization
  - Static files mount
  - Exception handler registration
  - Router registration
  - Startup / shutdown lifecycle events
  - Scheduler setup (SAP sync + embedding pipeline)

All business logic lives in api/routes/, services/, and ai/.
"""
from __future__ import annotations

import logging
import os as _os

from dotenv import load_dotenv

load_dotenv()  # Must run before any module that reads env vars

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI, OpenAI
from starlette.exceptions import HTTPException as StarletteHTTPException
from zoneinfo import ZoneInfo

from ai.orchestrator import AsyncChatOrchestrator, ChatOrchestrator
from core.config import settings
from core.exceptions import AppError
from core.logging_config import setup_logging
from core.database import engine, Base

setup_logging()

_log = logging.getLogger(__name__)

# ── Safe table creation ────────────────────────────────────────────────────────
# Creates all tables except knowledge_chunks which requires the pgvector extension.
# knowledge_chunks is attempted separately with a broad except so startup never
# fails if pgvector is not installed.

from sqlalchemy import MetaData as _MetaData
import models as _models  # noqa: F401 — registers all ORM classes with Base.metadata


def _safe_create_all() -> None:
    # Tables that require PostgreSQL extensions not guaranteed to be present:
    #   knowledge_chunks       — requires pgvector (text-embedding-3-small vectors)
    #   user_preference_profiles — requires pgvector (user embedding column)
    # Both are created separately below so startup never fails without the extension.
    _PGVECTOR_TABLES = {"knowledge_chunks", "user_preference_profiles"}

    all_tables  = Base.metadata.tables
    core_tables = {k: v for k, v in all_tables.items() if k not in _PGVECTOR_TABLES}
    if core_tables:
        core_meta = _MetaData()
        for tbl in core_tables.values():
            tbl.to_metadata(core_meta)
        core_meta.create_all(bind=engine)

    for tbl_name in _PGVECTOR_TABLES:
        tbl = all_tables.get(tbl_name)
        if tbl is None:
            continue
        try:
            single_meta = _MetaData()
            tbl.to_metadata(single_meta)
            single_meta.create_all(bind=engine)
            _log.info("pgvector_table_ready table=%s", tbl_name)
        except Exception as exc:
            _log.warning(
                "pgvector_table_skipped table=%s reason=pgvector_not_installed error=%s",
                tbl_name, exc,
            )


_safe_create_all()

# ── FastAPI application ────────────────────────────────────────────────────────

app = FastAPI(
    title       = "DhifafBot AI Service",
    description = "AI-powered beauty commerce chatbot with OpenAI GPT-4o",
    version     = "2.0.0",
)

# ── Exception handlers ─────────────────────────────────────────────────────────

@app.exception_handler(AppError)
async def _app_error(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.message},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": str(detail)},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if errors:
        first  = errors[0]
        loc    = " → ".join(str(p) for p in first.get("loc", []) if p != "body")
        msg    = first.get("msg", "Validation error")
        detail = f"{loc}: {msg}" if loc else msg
    else:
        detail = "Request validation failed."
    return JSONResponse(status_code=422, content={"success": False, "error": detail})


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    _log.error(
        "unhandled_error method=%s path=%s error=%s",
        request.method, request.url.path, exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "An unexpected error occurred. Please try again later."},
    )


# ── Request logging middleware ─────────────────────────────────────────────────

@app.middleware("http")
async def _log_requests(request: Request, call_next):
    import time
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000)
    _log.info(
        "http_request method=%s path=%s status=%d duration_ms=%d",
        request.method, request.url.path, response.status_code, duration_ms,
    )
    return response


# ── Static files ───────────────────────────────────────────────────────────────

import os as _os
app.mount(
    "/static",
    StaticFiles(directory=_os.path.join(_os.path.dirname(__file__), "static")),
    name="static",
)

# ── Router registration ────────────────────────────────────────────────────────

from api.routes.chat          import router as chat_router
from api.routes.knowledge     import router as knowledge_router
from api.routes.system        import router as system_router
from api.routes.uploads       import router as uploads_router
from api.routes.products      import router as products_router
from api.routes.ai_recommendations import router as ai_rec_router
from api.routes.categories    import router as categories_router
from api.routes.brands        import router as brands_router
from api.routes.subcategories import router as subcategories_router
from api.routes.recommendations import router as recommendations_router
from api.routes.bundles       import router as bundles_router
from api.routes.handoff       import router as handoff_router
from api.routes.export        import router as export_router

app.include_router(uploads_router)
app.include_router(export_router)
app.include_router(products_router)
app.include_router(ai_rec_router)
app.include_router(categories_router)
app.include_router(brands_router)
app.include_router(subcategories_router)
app.include_router(recommendations_router)
app.include_router(bundles_router)
app.include_router(handoff_router)
app.include_router(chat_router)
app.include_router(knowledge_router)
app.include_router(system_router)

# ── Startup lifecycle ──────────────────────────────────────────────────────────

_IRAQ_TZ = ZoneInfo("Asia/Baghdad")


@app.on_event("startup")
async def _startup() -> None:
    # ── Async OpenAI client (non-blocking GPT calls) ──────────────────────────
    async_client           = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    app.state.openai_client  = async_client   # shared for summary generation in chat.py
    app.state.orchestrator   = AsyncChatOrchestrator(async_client)

    # ── Redis connectivity check (non-fatal) ──────────────────────────────────
    try:
        from services.cache_service import _get_redis, is_available
        _get_redis()   # triggers connection + ping
        _log.info("redis_status available=%s", is_available())
    except Exception as exc:
        _log.warning("redis_startup_check_failed error=%s — caching disabled", exc)

    # ── Background scheduler ──────────────────────────────────────────────────
    # Guard: only start the scheduler in the FIRST Gunicorn worker to prevent
    # duplicate job execution. We use an environment variable set by Gunicorn's
    # post_fork hook, or fall back to always starting (safe with max_instances=1).
    _worker_id = int(_os.getenv("APP_WORKER_ID", "0"))
    _should_schedule = (_worker_id == 0)

    if _should_schedule:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from services.embedding import embed_new_products

        scheduler = AsyncIOScheduler(timezone=_IRAQ_TZ)
        scheduler.add_job(
            _run_sap_sync, trigger="cron", hour="6,18", minute=0,
            timezone=_IRAQ_TZ, id="sap_sync", replace_existing=True,
            max_instances=1, coalesce=True,
        )
        scheduler.add_job(
            embed_new_products, trigger="cron", hour=3, minute=0,
            timezone=_IRAQ_TZ, id="embedding_pipeline", replace_existing=True,
            max_instances=1, coalesce=True, kwargs={"limit": 0, "force_all": False},
        )
        # Flush stale product caches after every SAP sync
        scheduler.add_job(
            _invalidate_product_caches, trigger="cron", hour="6,18", minute=5,
            timezone=_IRAQ_TZ, id="cache_invalidation", replace_existing=True,
            max_instances=1, coalesce=True,
        )
        scheduler.start()
        app.state.scheduler = scheduler
        _log.info("scheduler_started worker_id=%d", _worker_id)
    else:
        _log.info("scheduler_skipped worker_id=%d (only worker 0 schedules)", _worker_id)

    _log.info(
        "startup_complete worker=%d sap_sync='06:00+18:00 IQ' embedding='03:00 IQ'",
        _worker_id,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
    _log.info("shutdown_complete")


async def _run_sap_sync() -> None:
    from services.sync import sync_sap_data
    await sync_sap_data()


async def _invalidate_product_caches() -> None:
    """Flush Redis product list caches after SAP sync completes."""
    try:
        from services.cache_service import invalidate_product_lists
        count = invalidate_product_lists()
        _log.info("post_sync_cache_flush deleted_keys=%d", count)
    except Exception as exc:
        _log.warning("post_sync_cache_flush_failed error=%s", exc)
