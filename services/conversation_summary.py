"""
services/conversation_summary.py
==================================
Rolling conversation summarizer and sliding history window.

WHY THIS EXISTS
---------------
Sending 70 messages of history to GPT every turn costs ~12,000–15,000 tokens
and often includes content from sessions weeks ago that is irrelevant to the
current intent. This module:

  1. Keeps only the last ACTIVE_WINDOW (20) messages as literal history.
  2. Older messages are condensed into a single compact summary.
  3. The summary is injected as the first system-role message so GPT has
     background context without the full token cost.

TOKEN SAVINGS
-------------
  Before: 70 messages × ~200 tokens avg = 14,000 tokens/turn
  After:  20 messages × ~200 tokens avg = 4,000 tokens +
          1 summary × ~150 tokens        = 150  tokens
  Saving: ~70% on history tokens (~10,000 tokens per turn)

SUMMARY STORAGE
---------------
Summaries are stored in the `conversation_summaries` table (see models.py).
One row per user, updated whenever we need to roll the window.

Redis fast-path:
  If Redis is available, the summary string is also cached in Redis with a
  1-hour TTL to avoid a DB read on every request for active users.

SUMMARY GENERATION
------------------
  - We generate a summary using GPT-4o-mini (cheap, fast).
  - Summary prompt instructs GPT to extract: user preferences, products
    discussed, concerns mentioned, decisions made — not to recap every turn.
  - Generation is triggered when:
      • History length first exceeds SUMMARY_TRIGGER (35 messages)
      • OR the existing summary is older than SUMMARY_MAX_AGE_HOURS (4 hours)

  If summary generation fails, we fall back gracefully: use the last 20
  messages without a summary header. Conversation quality is preserved.

BACKWARD COMPATIBILITY
----------------------
  chat_service.get_history() still returns raw history.
  get_windowed_history() is the new function used by the orchestrator.
  All existing endpoints (/history/{user_id}) are unaffected.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

ACTIVE_WINDOW         = int(os.getenv("HISTORY_ACTIVE_WINDOW",    "20"))   # messages kept verbatim
SUMMARY_TRIGGER       = int(os.getenv("HISTORY_SUMMARY_TRIGGER",  "35"))   # messages before summarising
SUMMARY_MAX_AGE_HOURS = int(os.getenv("SUMMARY_MAX_AGE_HOURS",     "4"))   # hours before re-summarising
SUMMARY_MODEL         = "gpt-4o-mini"                                       # always use cheap model
_REDIS_SUMMARY_TTL    = 3600                                                 # 1 hour


# ── Redis fast-path helpers ───────────────────────────────────────────────────

def _redis_key(user_id: str) -> str:
    return f"dhifaf:summary:{user_id}"


def _redis_get(user_id: str) -> Optional[str]:
    try:
        from services.cache_service import _get_redis
        r = _get_redis()
        if r is None:
            return None
        val = r.get(_redis_key(user_id))
        return val if isinstance(val, str) else None
    except Exception:
        return None


def _redis_set(user_id: str, summary: str) -> None:
    try:
        from services.cache_service import _get_redis
        r = _get_redis()
        if r is not None:
            r.set(_redis_key(user_id), summary, ex=_REDIS_SUMMARY_TTL)
    except Exception:
        pass


def _redis_delete(user_id: str) -> None:
    try:
        from services.cache_service import _get_redis
        r = _get_redis()
        if r is not None:
            r.delete(_redis_key(user_id))
    except Exception:
        pass


# ── DB helpers (lazy table creation — no migration required) ──────────────────

_TABLE_CREATED = False

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS conversation_summaries (
    user_id      VARCHAR(255) PRIMARY KEY,
    summary      TEXT         NOT NULL,
    message_count INTEGER     NOT NULL DEFAULT 0,
    updated_at   TIMESTAMP    NOT NULL DEFAULT NOW()
)
"""


def _ensure_table(db: Session) -> None:
    global _TABLE_CREATED
    if _TABLE_CREATED:
        return
    try:
        db.execute(text(_CREATE_TABLE_SQL))
        db.commit()
        _TABLE_CREATED = True
    except Exception as exc:
        logger.debug("conversation_summaries table init: %s", exc)
        _TABLE_CREATED = True   # don't retry on every request


def _db_get_summary(user_id: str, db: Session) -> Optional[tuple[str, datetime]]:
    """Return (summary_text, updated_at) or None."""
    try:
        _ensure_table(db)
        row = db.execute(
            text("SELECT summary, updated_at FROM conversation_summaries WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        if row:
            return row.summary, row.updated_at
    except Exception as exc:
        logger.debug("summary_db_get user=%s error=%s", user_id, exc)
    return None


def _db_upsert_summary(user_id: str, summary: str, message_count: int, db: Session) -> None:
    try:
        _ensure_table(db)
        db.execute(
            text("""
                INSERT INTO conversation_summaries (user_id, summary, message_count, updated_at)
                VALUES (:uid, :s, :mc, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET summary       = EXCLUDED.summary,
                        message_count = EXCLUDED.message_count,
                        updated_at    = NOW()
            """),
            {"uid": user_id, "s": summary, "mc": message_count},
        )
        db.commit()
    except Exception as exc:
        logger.warning("summary_db_upsert user=%s error=%s", user_id, exc)


def _db_delete_summary(user_id: str, db: Session) -> None:
    try:
        db.execute(
            text("DELETE FROM conversation_summaries WHERE user_id = :uid"),
            {"uid": user_id},
        )
        db.commit()
    except Exception:
        pass


# ── Summary generation ────────────────────────────────────────────────────────

_SUMMARY_SYSTEM_PROMPT = """You are a concise conversation analyst for DhifafBot, a beauty commerce chatbot.

Given the conversation history below, write a COMPACT summary (≤120 words) that captures:
- Products the user asked about or was shown (with names only, no prices)
- User's skin type or beauty concerns if mentioned
- User's price range or brand preferences if mentioned
- Any decisions or conclusions reached
- The overall topic/intent of the session

Write in third person (e.g. "User asked about...").
Do NOT include greetings, farewells, or filler content.
If the user wrote in Arabic, write the summary in Arabic.
"""

_SUMMARY_USER_TEMPLATE = """Summarise this conversation history:

{history_text}

Keep it under 120 words. Focus on preferences, products, and decisions."""


def _generate_summary(history: list[dict], openai_client) -> Optional[str]:
    """
    Call GPT-4o-mini to produce a compact summary of *history*.
    Returns None on any failure.
    """
    if not history:
        return None

    # Format history as readable text
    lines = []
    for msg in history:
        role    = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "")
        if isinstance(content, str):
            lines.append(f"{role}: {content[:300]}")   # cap each message at 300 chars

    history_text = "\n".join(lines)

    try:
        response = openai_client.chat.completions.create(
            model       = SUMMARY_MODEL,
            messages    = [
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user",   "content": _SUMMARY_USER_TEMPLATE.format(
                    history_text=history_text
                )},
            ],
            temperature = 0.3,
            max_tokens  = 200,
        )
        summary = response.choices[0].message.content or ""
        return summary.strip() if summary.strip() else None
    except Exception as exc:
        logger.warning("summary_generation_failed error=%s", exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def get_windowed_history(
    user_id:       str,
    full_history:  list[dict],
    db:            Session,
    openai_client  = None,
) -> tuple[list[dict], Optional[str]]:
    """
    Return (active_window, summary_text) for building the messages array.

    active_window : The last ACTIVE_WINDOW messages (verbatim, for GPT context).
    summary_text  : Compact summary of older messages, or None if not needed.

    The orchestrator injects summary_text as a hidden system note before
    the active window, giving GPT awareness of the full session at low cost.

    Parameters
    ----------
    user_id       : User identifier.
    full_history  : Complete history list from get_history() (up to 70 items).
    db            : Active SQLAlchemy session (for summary storage).
    openai_client : OpenAI client for summary generation (optional; if None,
                    summary generation is skipped and cached summary is used).
    """
    total = len(full_history)

    # Short conversation — no summary needed
    if total <= ACTIVE_WINDOW:
        return full_history, None

    active_window    = full_history[-ACTIVE_WINDOW:]
    older_messages   = full_history[:-ACTIVE_WINDOW]
    summary: Optional[str] = None

    # ── Fast path: Redis cache ────────────────────────────────────────────────
    cached_summary = _redis_get(user_id)
    if cached_summary:
        logger.debug("summary_cache_hit user=%s", user_id)
        return active_window, cached_summary

    # ── Check DB for existing summary ─────────────────────────────────────────
    db_result = _db_get_summary(user_id, db)
    if db_result:
        existing_summary, updated_at = db_result
        age_hours = (
            datetime.now(timezone.utc) - updated_at.replace(tzinfo=timezone.utc)
        ).total_seconds() / 3600

        if age_hours < SUMMARY_MAX_AGE_HOURS:
            # Summary is fresh enough — use it
            _redis_set(user_id, existing_summary)   # warm Redis for next request
            logger.debug(
                "summary_db_hit user=%s age_hours=%.1f", user_id, age_hours
            )
            return active_window, existing_summary

    # ── Generate a new summary ────────────────────────────────────────────────
    if openai_client is not None and total >= SUMMARY_TRIGGER:
        summary = _generate_summary(older_messages, openai_client)
        if summary:
            _db_upsert_summary(user_id, summary, total, db)
            _redis_set(user_id, summary)
            logger.info(
                "summary_generated user=%s older_msgs=%d summary_chars=%d",
                user_id, len(older_messages), len(summary),
            )
        else:
            logger.warning("summary_generation_skipped user=%s", user_id)

    return active_window, summary


def invalidate_summary(user_id: str, db: Optional[Session] = None) -> None:
    """
    Remove cached summary for a user (e.g., after history is cleared).
    """
    _redis_delete(user_id)
    if db is not None:
        _db_delete_summary(user_id, db)
    logger.debug("summary_invalidated user=%s", user_id)
