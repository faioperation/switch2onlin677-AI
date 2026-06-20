"""
core/rate_limiter.py
====================
Per-user rate limiting for the /reply endpoint.

WHY THIS EXISTS
---------------
A single user sending 30 rapid messages in a minute could:
  1. Consume 30× the token budget in 60 seconds
  2. Trigger OpenAI 429 rate-limit errors
  3. Starve other users of GPT quota

Rate limiting protects GPT quota, controls cost, and ensures fair access
across all concurrent users.

STRATEGY
---------
  Sliding window counter (token bucket algorithm):
    - Each user_id gets a request counter with a 60-second rolling window
    - Default: 15 requests per 60 seconds per user_id
    - Exceeding the limit → HTTP 429 with Retry-After header

  Storage:
    - Redis (preferred): cross-worker, accurate across all Gunicorn workers
    - In-memory fallback: per-worker, degrades gracefully if Redis is down

  Configuration (env vars):
    RATE_LIMIT_REQUESTS  = 15   max requests per window (default: 15)
    RATE_LIMIT_WINDOW    = 60   window size in seconds (default: 60)

USAGE
------
  In api/routes/chat.py:

    from core.rate_limiter import check_rate_limit

    @router.post("/reply")
    async def generate_reply(data: ChatRequest, request: Request, ...):
        check_rate_limit(data.user_id)   # raises HTTP 429 if exceeded
        ...

  The function is intentionally synchronous — Redis calls are fast (<1ms)
  and the overhead is negligible compared to a GPT API call (~1–3 seconds).
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from threading import Lock
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

MAX_REQUESTS  = int(os.getenv("RATE_LIMIT_REQUESTS", "15"))
WINDOW_SECS   = int(os.getenv("RATE_LIMIT_WINDOW",   "60"))
_PREFIX       = "dhifaf:ratelimit"


# ── Redis-backed limiter ──────────────────────────────────────────────────────

def _redis_check(user_id: str) -> tuple[bool, int]:
    """
    Check rate limit via Redis using a sliding window counter.

    Returns (is_allowed, requests_remaining).
    Uses INCR + EXPIRE (atomic via pipeline) for cross-worker safety.
    """
    try:
        from services.cache_service import _get_redis
        r = _get_redis()
        if r is None:
            return True, MAX_REQUESTS   # Redis unavailable — allow through

        key = f"{_PREFIX}:{user_id}"
        pipe = r.pipeline(transaction=True)
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()

        # Set expiry on first request in window
        if ttl == -1:   # key has no expiry (INCR just created it)
            r.expire(key, WINDOW_SECS)

        remaining = max(0, MAX_REQUESTS - count)
        return count <= MAX_REQUESTS, remaining

    except Exception as exc:
        logger.debug("rate_limit_redis_error user=%s error=%s", user_id, exc)
        return True, MAX_REQUESTS   # fail open on Redis error


# ── In-memory fallback ────────────────────────────────────────────────────────

_mem_store: dict[str, list[float]] = defaultdict(list)
_mem_lock  = Lock()


def _memory_check(user_id: str) -> tuple[bool, int]:
    """
    Check rate limit using an in-memory sliding window.
    Less accurate under multiple workers but safe and zero-dependency.
    """
    now    = time.monotonic()
    cutoff = now - WINDOW_SECS

    with _mem_lock:
        timestamps = _mem_store[user_id]
        # Evict timestamps outside window
        timestamps[:] = [ts for ts in timestamps if ts > cutoff]
        timestamps.append(now)
        count = len(timestamps)

        # Trim to prevent unbounded growth
        if len(timestamps) > MAX_REQUESTS * 3:
            _mem_store[user_id] = timestamps[-MAX_REQUESTS:]

    remaining = max(0, MAX_REQUESTS - count)
    return count <= MAX_REQUESTS, remaining


# ── Public API ────────────────────────────────────────────────────────────────

def check_rate_limit(user_id: str) -> None:
    """
    Check if *user_id* is within their rate limit.

    Raises fastapi.HTTPException(429) with a Retry-After header if exceeded.
    Does nothing (returns None) if the request is within limits.

    Tries Redis first, falls back to in-memory on Redis unavailability.
    """
    # Try Redis first
    from services.cache_service import is_available as redis_available
    if redis_available():
        allowed, remaining = _redis_check(user_id)
    else:
        allowed, remaining = _memory_check(user_id)

    if not allowed:
        logger.warning(
            "rate_limit_exceeded user=%s limit=%d window=%ds",
            user_id, MAX_REQUESTS, WINDOW_SECS,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error":       "Too many requests. Please slow down.",
                "retry_after": WINDOW_SECS,
                "limit":       MAX_REQUESTS,
                "window":      WINDOW_SECS,
            },
            headers={"Retry-After": str(WINDOW_SECS)},
        )


def get_rate_limit_status(user_id: str) -> dict:
    """
    Return rate limit status for a user (for admin/debug endpoints).
    Does NOT increment the counter.
    """
    from services.cache_service import _get_redis
    r = _get_redis()
    if r is not None:
        try:
            key   = f"{_PREFIX}:{user_id}"
            count = int(r.get(key) or 0)
            ttl   = r.ttl(key)
            return {
                "user_id":   user_id,
                "requests":  count,
                "limit":     MAX_REQUESTS,
                "remaining": max(0, MAX_REQUESTS - count),
                "resets_in": max(0, ttl),
                "backend":   "redis",
            }
        except Exception:
            pass

    now    = time.monotonic()
    cutoff = now - WINDOW_SECS
    with _mem_lock:
        count = sum(1 for ts in _mem_store.get(user_id, []) if ts > cutoff)

    return {
        "user_id":   user_id,
        "requests":  count,
        "limit":     MAX_REQUESTS,
        "remaining": max(0, MAX_REQUESTS - count),
        "resets_in": WINDOW_SECS,
        "backend":   "memory",
    }
