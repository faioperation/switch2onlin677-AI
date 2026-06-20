"""
services/cache_service.py
=========================
Redis-backed cache layer for expensive, frequently-repeated read operations.

WHY THIS EXISTS
---------------
Every product recommendation query (best_sellers, new_arrivals, featured)
hits PostgreSQL directly, even though these lists change at most once per hour
(when the SAP sync runs). At 100 daily users × 10 turns × 2 tool calls per
turn = 2,000 identical DB queries per day for data that changes twice a day.

This module caches those results in Redis so only the first request in each
TTL window hits the database.

CACHE KEYS AND TTL
------------------
  products:best_sellers:{cat}:{tier}    → JSON list   TTL: 30 min
  products:new_arrivals:{cat}           → JSON list   TTL: 30 min
  products:featured:{limit}             → JSON list   TTL: 30 min
  products:recommended:{cat}:{tier}     → JSON list   TTL: 30 min
  products:detail:{barcode}             → JSON dict   TTL: 60 min
  availability:{query_hash}             → JSON bool   TTL: 15 min
  handoff:state:{user_id}               → JSON str    TTL: 10 min

INVALIDATION STRATEGY
---------------------
  - SAP sync completion: call invalidate_product_lists() to flush product lists.
  - Handoff state: call invalidate_handoff(user_id) after state transitions.
  - Product CRUD: call invalidate_product(barcode) after add/update/delete.
  - All invalidation uses key-pattern deletion (SCAN + DEL) to be safe.

FAILURE HANDLING
----------------
All public functions catch Redis exceptions and return None on cache miss.
Callers treat None as "cache miss → run the original query".
Never raises. Never blocks the request on Redis failure.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── TTL constants (seconds) ───────────────────────────────────────────────────

TTL_PRODUCT_LIST   = int(os.getenv("CACHE_TTL_PRODUCT_LIST",   str(30 * 60)))   # 30 min
TTL_PRODUCT_DETAIL = int(os.getenv("CACHE_TTL_PRODUCT_DETAIL", str(60 * 60)))   # 60 min
TTL_AVAILABILITY   = int(os.getenv("CACHE_TTL_AVAILABILITY",   str(15 * 60)))   # 15 min
TTL_HANDOFF_STATE  = int(os.getenv("CACHE_TTL_HANDOFF_STATE",  str(10 * 60)))   # 10 min

_PREFIX = "dhifaf"   # namespace for all keys


# ── Redis client singleton ────────────────────────────────────────────────────

_redis_client = None
_redis_available: Optional[bool] = None   # None = not yet checked


def _get_redis():
    """
    Return a live Redis client, or None if Redis is unavailable.
    Connection is attempted once at startup; subsequent calls use the cached
    result. On failure, all cache operations degrade silently.
    """
    global _redis_client, _redis_available

    if _redis_available is False:
        return None
    if _redis_client is not None:
        return _redis_client

    redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        _redis_available = False
        logger.info("cache_service redis_disabled REDIS_URL not set")
        return None

    try:
        import redis as _redis
        _redis_client = _redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=1,
            retry_on_timeout=False,
        )
        _redis_client.ping()
        _redis_available = True
        logger.info("cache_service redis_connected url=%s", redis_url)
    except Exception as exc:
        _redis_available = False
        logger.warning("cache_service redis_unavailable error=%s — caching disabled", exc)
        _redis_client = None

    return _redis_client


def is_available() -> bool:
    """Return True if Redis is reachable and caching is active."""
    return _get_redis() is not None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _key(*parts: str) -> str:
    """Build a namespaced Redis key from parts, replacing None with '_'."""
    safe = [str(p) if p is not None else "_" for p in parts]
    return f"{_PREFIX}:" + ":".join(safe)


def _query_hash(query: str) -> str:
    """Short, stable hash of a query string for use in cache keys."""
    return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]


def _get(key: str) -> Optional[Any]:
    r = _get_redis()
    if r is None:
        return None
    try:
        raw = r.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception as exc:
        logger.debug("cache_get_error key=%s error=%s", key, exc)
        return None


def _set(key: str, value: Any, ttl: int) -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
    except Exception as exc:
        logger.debug("cache_set_error key=%s error=%s", key, exc)


def _delete_pattern(pattern: str) -> int:
    """Delete all keys matching *pattern* via SCAN (non-blocking)."""
    r = _get_redis()
    if r is None:
        return 0
    deleted = 0
    try:
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match=pattern, count=100)
            if keys:
                r.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
    except Exception as exc:
        logger.debug("cache_delete_pattern_error pattern=%s error=%s", pattern, exc)
    return deleted


# ── Product list cache ────────────────────────────────────────────────────────

def get_best_sellers(category: Optional[str], price_tier: Optional[str]) -> Optional[list]:
    return _get(_key("products", "best_sellers", str(category), str(price_tier)))


def set_best_sellers(
    category: Optional[str],
    price_tier: Optional[str],
    products: list,
) -> None:
    _set(_key("products", "best_sellers", str(category), str(price_tier)), products, TTL_PRODUCT_LIST)


def get_new_arrivals(category: Optional[str]) -> Optional[list]:
    return _get(_key("products", "new_arrivals", str(category)))


def set_new_arrivals(category: Optional[str], products: list) -> None:
    _set(_key("products", "new_arrivals", str(category)), products, TTL_PRODUCT_LIST)


def get_featured(limit: int) -> Optional[list]:
    return _get(_key("products", "featured", str(limit)))


def set_featured(limit: int, products: list) -> None:
    _set(_key("products", "featured", str(limit)), products, TTL_PRODUCT_LIST)


def get_recommended(category: Optional[str], price_tier: Optional[str]) -> Optional[list]:
    return _get(_key("products", "recommended", str(category), str(price_tier)))


def set_recommended(
    category: Optional[str],
    price_tier: Optional[str],
    products: list,
) -> None:
    _set(_key("products", "recommended", str(category), str(price_tier)), products, TTL_PRODUCT_LIST)


# ── Product detail cache ──────────────────────────────────────────────────────

def get_product_detail(barcode: str) -> Optional[dict]:
    return _get(_key("products", "detail", barcode))


def set_product_detail(barcode: str, detail: dict) -> None:
    _set(_key("products", "detail", barcode), detail, TTL_PRODUCT_DETAIL)


# ── Availability cache ────────────────────────────────────────────────────────

def get_availability(query: str) -> Optional[dict]:
    return _get(_key("availability", _query_hash(query)))


def set_availability(query: str, result: dict) -> None:
    _set(_key("availability", _query_hash(query)), result, TTL_AVAILABILITY)


# ── Handoff state cache ───────────────────────────────────────────────────────

def get_handoff_state(user_id: str) -> Optional[dict]:
    """Return cached handoff state dict or None."""
    return _get(_key("handoff", "state", user_id))


def set_handoff_state(user_id: str, state: dict) -> None:
    """Cache handoff state for a user."""
    _set(_key("handoff", "state", user_id), state, TTL_HANDOFF_STATE)


def invalidate_handoff(user_id: str) -> None:
    """Remove cached handoff state (call on every state transition)."""
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(_key("handoff", "state", user_id))
    except Exception:
        pass


# ── Bulk invalidation ─────────────────────────────────────────────────────────

def invalidate_product_lists() -> int:
    """
    Flush all product list caches (best_sellers, new_arrivals, featured, recommended).
    Call this after SAP sync completes to ensure fresh data is served.
    """
    count = _delete_pattern(f"{_PREFIX}:products:*")
    logger.info("cache_invalidate_product_lists deleted=%d", count)
    return count


def invalidate_product(barcode: str) -> None:
    """
    Remove cached detail for a specific product.
    Call after product create/update/delete.
    """
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(_key("products", "detail", barcode))
    except Exception:
        pass


def get_cache_stats() -> dict:
    """Return basic Redis cache statistics for the health endpoint."""
    r = _get_redis()
    if r is None:
        return {"available": False, "keys": 0}
    try:
        info = r.info("keyspace")
        db_key = f"db{os.getenv('REDIS_DB', '0')}"
        db_info = info.get(db_key, {})
        return {
            "available": True,
            "keys":      db_info.get("keys", 0),
            "expires":   db_info.get("expires", 0),
        }
    except Exception:
        return {"available": True, "keys": "unknown"}
