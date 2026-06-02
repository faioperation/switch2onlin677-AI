"""
services/recommendation_cache.py
=================================
Session-level recommendation diversity cache.

Tracks recently recommended product barcodes per user so the chatbot
avoids showing the same items repeatedly within a session.

Implementation: in-memory TTL dict with thread-safe access.
Future: swap for Redis when running multiple app instances.
"""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

# {user_id: [(barcode, timestamp_float), ...]}
_cache: dict[str, list[tuple[str, float]]] = defaultdict(list)
_lock  = Lock()

_MAX_TRACKED   = 40    # max barcodes remembered per user
_TTL_SECONDS   = 3600  # 1 hour before a product can resurface


def record_recommendations(user_id: str, barcodes: list[str]) -> None:
    """Record that these products were shown to this user."""
    if not user_id or not barcodes:
        return
    now = time.monotonic()
    with _lock:
        history = _cache[user_id]
        for b in barcodes:
            history.append((b, now))
        _cache[user_id] = history[-_MAX_TRACKED:]


def get_recent_barcodes(user_id: str) -> set[str]:
    """Return barcodes shown to this user within the TTL window."""
    if not user_id:
        return set()
    cutoff = time.monotonic() - _TTL_SECONDS
    with _lock:
        return {b for b, ts in _cache.get(user_id, []) if ts >= cutoff}


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
    with _lock:
        _cache.pop(user_id, None)
