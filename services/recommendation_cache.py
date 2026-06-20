"""
services/recommendation_cache.py
=================================
Session-level recommendation diversity cache.

Architecture — adapter pattern
-------------------------------
The cache backend is selected by the RECOMMENDATION_CACHE_BACKEND env var:

  memory  (default)  — in-process TTL dict, thread-safe.
                        Fast and zero-dependency.
                        NOT shared across Gunicorn workers; each worker
                        maintains its own session history.

  redis              — cross-worker Redis backend (placeholder).
                        Set REDIS_URL env var and implement the three
                        abstract methods to activate.

Public API (call signatures unchanged from previous version)
------------------------------------------------------------
  record_recommendations(user_id, barcodes)
  get_recent_barcodes(user_id)           → set[str]
  apply_diversity(products, user_id)     → list[dict]
  clear_user_history(user_id)
"""
from __future__ import annotations

import abc
import logging
import os
import time
from collections import defaultdict
from threading import Lock

logger = logging.getLogger(__name__)

_TTL_SECONDS: float = 3600.0   # 1 hour before a product can resurface
_MAX_TRACKED: int   = 40       # max barcodes remembered per user (memory backend)


# ── Abstract adapter interface ─────────────────────────────────────────────────

class _CacheAdapter(abc.ABC):
    """Abstract base for recommendation cache backends."""

    @abc.abstractmethod
    def record(self, user_id: str, barcodes: list[str]) -> None:
        """Record that these products were shown to this user."""

    @abc.abstractmethod
    def get_recent(self, user_id: str, ttl_seconds: float) -> set[str]:
        """Return barcodes shown to this user within *ttl_seconds*."""

    @abc.abstractmethod
    def clear(self, user_id: str) -> None:
        """Remove all tracking data for a user."""


# ── Memory adapter (default) ───────────────────────────────────────────────────

class _MemoryCacheAdapter(_CacheAdapter):
    """
    In-process TTL dict with thread-safe access.

    Adequate for single-process deployments or development environments.
    With multiple Gunicorn workers each process maintains its own history,
    so diversity filtering is per-worker rather than per-user globally.
    Upgrade to _RedisCacheAdapter when cross-worker consistency is required.
    """

    def __init__(self) -> None:
        # {user_id: [(barcode, monotonic_timestamp), ...]}
        self._store: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self._lock = Lock()

    def record(self, user_id: str, barcodes: list[str]) -> None:
        if not user_id or not barcodes:
            return
        now = time.monotonic()
        with self._lock:
            history = self._store[user_id]
            for b in barcodes:
                history.append((b, now))
            # Keep only the most recent _MAX_TRACKED entries
            self._store[user_id] = history[-_MAX_TRACKED:]

    def get_recent(self, user_id: str, ttl_seconds: float) -> set[str]:
        if not user_id:
            return set()
        cutoff = time.monotonic() - ttl_seconds
        with self._lock:
            return {b for b, ts in self._store.get(user_id, []) if ts >= cutoff}

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._store.pop(user_id, None)


# ── Redis adapter placeholder ──────────────────────────────────────────────────

class _RedisCacheAdapter(_CacheAdapter):
    """
    Cross-worker Redis recommendation diversity cache.

    Uses Redis Sorted Sets (ZADD / ZRANGEBYSCORE / DEL) so that product
    diversity filtering is consistent across all Gunicorn worker processes.

    Key schema:
      rec:{user_id}   → Sorted Set  { member: barcode, score: unix_timestamp }
      TTL: auto-refreshed on every record() call (key-level EXPIRE = _TTL_SECONDS + 60s)

    Activation:
      1. pip install redis
      2. Set RECOMMENDATION_CACHE_BACKEND=redis
      3. Set REDIS_URL=redis://<host>:<port>/<db>   (default: redis://localhost:6379/0)

    Failure handling:
      All methods catch Redis exceptions and log warnings rather than crashing.
      The caller (apply_diversity) degrades gracefully to "no diversity filter".
    """

    _KEY_PREFIX = "rec"
    _EXPIRE_BUFFER = 120   # seconds beyond TTL before key expires (safety margin)

    def __init__(self, redis_url: str) -> None:
        try:
            import redis as _redis
            self._client = _redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=True,
            )
            # Verify connection at startup
            self._client.ping()
            logger.info("recommendation_cache redis_connected url=%s", redis_url)
        except ImportError:
            raise RuntimeError(
                "redis package not installed. Run: pip install redis"
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not connect to Redis at {redis_url!r}: {exc}"
            )

    def _key(self, user_id: str) -> str:
        return f"{self._KEY_PREFIX}:{user_id}"

    def record(self, user_id: str, barcodes: list[str]) -> None:
        if not user_id or not barcodes:
            return
        import time
        now = time.time()
        key = self._key(user_id)
        try:
            pipe = self._client.pipeline(transaction=False)
            for barcode in barcodes:
                pipe.zadd(key, {barcode: now})
            # Keep key alive for TTL + buffer
            pipe.expire(key, int(_TTL_SECONDS + self._EXPIRE_BUFFER))
            # Prune entries older than 2× TTL (housekeeping)
            cutoff = now - (_TTL_SECONDS * 2)
            pipe.zremrangebyscore(key, "-inf", cutoff)
            pipe.execute()
        except Exception as exc:
            logger.warning("rec_cache_redis_record_error user=%s error=%s", user_id, exc)

    def get_recent(self, user_id: str, ttl_seconds: float) -> set[str]:
        if not user_id:
            return set()
        import time
        cutoff = time.time() - ttl_seconds
        try:
            members = self._client.zrangebyscore(
                self._key(user_id), cutoff, "+inf"
            )
            return set(members)
        except Exception as exc:
            logger.warning("rec_cache_redis_get_error user=%s error=%s", user_id, exc)
            return set()

    def clear(self, user_id: str) -> None:
        try:
            self._client.delete(self._key(user_id))
        except Exception as exc:
            logger.warning("rec_cache_redis_clear_error user=%s error=%s", user_id, exc)


# ── Adapter factory ────────────────────────────────────────────────────────────

def _build_adapter() -> _CacheAdapter:
    backend = os.getenv("RECOMMENDATION_CACHE_BACKEND", "memory").strip().lower()
    if backend == "redis":
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            adapter = _RedisCacheAdapter(redis_url)
            logger.info("recommendation_cache backend=redis url=%s", redis_url)
            return adapter
        except Exception as exc:
            logger.warning(
                "recommendation_cache redis_unavailable error=%s — falling back to memory", exc
            )
    elif backend != "memory":
        logger.warning(
            "recommendation_cache unknown backend=%r — falling back to memory", backend
        )
    logger.info("recommendation_cache backend=memory")
    return _MemoryCacheAdapter()


# Module-level singleton — instantiated once at import time.
_adapter: _CacheAdapter = _build_adapter()


# ── Public API (call signatures identical to the previous version) ─────────────

def record_recommendations(user_id: str, barcodes: list[str]) -> None:
    """Record that these products were shown to this user."""
    _adapter.record(user_id, barcodes)


def get_recent_barcodes(user_id: str) -> set[str]:
    """Return barcodes shown to this user within the TTL window."""
    return _adapter.get_recent(user_id, _TTL_SECONDS)


def apply_diversity(
    products: list[dict],
    user_id:  str | None,
    *,
    id_key: str = "id",
) -> list[dict]:
    """
    Reorder products so recently-seen items appear last.

    Never removes products entirely — ensures the user still gets results
    even when the catalog is small and all top items were seen recently.
    """
    if not user_id:
        return products

    recent = get_recent_barcodes(user_id)
    if not recent:
        return products

    fresh = [p for p in products if p.get(id_key) not in recent]
    seen  = [p for p in products if p.get(id_key) in recent]
    return fresh + seen


def clear_user_history(user_id: str) -> None:
    """Remove all tracking data for a user (e.g. on session reset)."""
    _adapter.clear(user_id)
