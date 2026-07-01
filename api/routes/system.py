"""
api/routes/system.py
====================
Operational and admin endpoints:
  GET  /health          — liveness probe
  GET  /ready           — readiness probe (DB + SAP sync age)
  POST /sap/sync-now    — manual SAP sync trigger
  GET  /rate            — current IQD exchange rate
  POST /rate            — update IQD exchange rate
  GET  /leads           — view saved lead records
  GET  /prompt          — read system prompt
  PUT  /prompt          — update system prompt
"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from ai.prompt_manager import (
    SYSTEM_PROMPT_FILE,
    render_prompt_template,
    write_system_prompt,
)
from core.database import SessionLocal
from models import SapSyncAuditLog
from services.knowledge_service import load_iqd_rate, load_leads, save_iqd_rate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class RateRequest(BaseModel):
    iqd_rate: float


class PromptUpdateRequest(BaseModel):
    prompt: str


class PromptResponse(BaseModel):
    prompt:          str
    rendered_prompt: str | None = None


# ── Health & readiness ─────────────────────────────────────────────────────────

@router.get("/health")
def health_check():
    """Liveness probe — always 200 if the process is alive."""
    from core.circuit_breaker import openai_circuit_breaker
    from services.cache_service import get_cache_stats, is_available as redis_ok
    return {
        "status":         "healthy",
        "timestamp":      datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "openai_circuit": openai_circuit_breaker.get_stats(),
        "redis":          {"available": redis_ok(), **get_cache_stats()},
    }


@router.get("/ready")
def readiness_check():
    """Readiness probe — checks DB connectivity and SAP sync recency."""
    checks:     dict = {}
    overall_ok: bool = True

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok"}
    except Exception as exc:
        checks["database"] = {"status": "error", "detail": str(exc)}
        overall_ok = False
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        latest = (
            db2.query(SapSyncAuditLog)
            .order_by(SapSyncAuditLog.synced_at.desc())
            .first()
        )
        if latest is None:
            checks["sap_sync"] = {"status": "warning", "detail": "No sync run recorded yet"}
        else:
            age_hours = (
                datetime.datetime.now() - latest.synced_at
            ).total_seconds() / 3600
            checks["sap_sync"] = {
                "status":    "warning" if age_hours > 25 else latest.status,
                "last_sync": latest.synced_at.isoformat(),
                "age_hours": round(age_hours, 1),
            }
            if age_hours > 25:
                checks["sap_sync"]["detail"] = "Last SAP sync is older than 25 hours"
    except Exception as exc:
        checks["sap_sync"] = {"status": "error", "detail": str(exc)}
    finally:
        db2.close()

    return JSONResponse(
        status_code=200 if overall_ok else 503,
        content={
            "ready":     overall_ok,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "checks":    checks,
        },
    )


# ── SAP sync ───────────────────────────────────────────────────────────────────

@router.post("/sap/sync-now")
async def sync_sap_now():
    from services.sync import sync_sap_data
    from core.config import settings

    result = await sync_sap_data()
    ok = result.get("status") == "success"
    return {
        "success":   ok,
        "message":   "SAP sync completed." if ok else f"SAP sync failed: {result.get('error_message')}",
        "synced_at": datetime.datetime.now(settings.IRAQ_TIMEZONE).isoformat(),
        "timezone":  "Asia/Baghdad",
        "summary":   result,
    }


# ── Rate management ────────────────────────────────────────────────────────────

@router.get("/rate")
def get_rate():
    return {"iqd_rate": load_iqd_rate()}


@router.post("/rate")
def update_rate(data: RateRequest):
    """
    Update the IQD exchange rate.

    Uses an atomic file write + immediate in-process cache update so that all
    subsequent product price conversions (in /products, /reply, /recommendations)
    use the new rate without delay.

    The response includes `rate_updated_at` so the dashboard/frontend can
    detect the change and re-fetch product lists to show updated IQD prices.
    """
    save_iqd_rate(data.iqd_rate)
    return {
        "success":         True,
        "iqd_rate":        data.iqd_rate,
        "rate_updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# ── Leads ──────────────────────────────────────────────────────────────────────

@router.get("/leads")
def get_leads():
    return load_leads()


# ── Prompt management ──────────────────────────────────────────────────────────

@router.get("/prompt", response_model=PromptResponse)
def get_prompt(render: bool = False):
    if not SYSTEM_PROMPT_FILE.exists():
        raise HTTPException(status_code=404, detail="system_prompt.txt was not found.")
    prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return PromptResponse(
        prompt=prompt,
        rendered_prompt=render_prompt_template(prompt) if render else None,
    )


@router.put("/prompt", response_model=PromptResponse)
def update_prompt(data: PromptUpdateRequest):
    """
    Update the system prompt.

    Uses an atomic file write (temp + rename) to prevent concurrent requests
    from reading a partially-written file.  The in-process cache is updated
    immediately so the very next /reply call uses the new prompt.
    """
    if not data.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")

    # Validate template variables before touching the file
    try:
        rendered = render_prompt_template(data.prompt)
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown prompt variable: {exc}. "
                "Allowed: FIXED_WELCOME_EN, FIXED_WELCOME_AR, FIXED_GOODBYE_EN, FIXED_GOODBYE_AR"
            ),
        )

    # Atomic write + immediate cache update (no stale window)
    write_system_prompt(data.prompt)
    return PromptResponse(prompt=data.prompt, rendered_prompt=rendered)
