"""
ai/tools/formatters.py
======================
Currency conversion and product list formatting utilities.
Shared by services/recommendation.py and the AI tool layer.

IQD Rate Cache (mtime-based)
-----------------------------
`get_iqd_rate()` is called once per product inside `convert_to_iqd()`.
For a product list of 50 items this was 50 disk reads of rate.json per
request — functionally correct but inefficient, and susceptible to race
conditions between reads and writes.

The fix: cache the rate in memory, keyed by the file's mtime.
  - Hit: one cheap OS stat() per request; no disk read.
  - Miss: one disk read (file changed since last call).

`update_iqd_rate(rate)` writes the file atomically AND immediately updates
the cache so the very next call to `get_iqd_rate()` returns the new value
without waiting for a disk re-read.

Thread-safety: threading.Lock() protects all cache mutations.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time as _time
from pathlib import Path
from typing import Dict, List, Optional


def _atomic_replace(src: Path, dst: Path) -> None:
    """
    Rename src → dst atomically.

    On POSIX: os.replace() is a single atomic syscall — the retry path is
    never reached.

    On Windows: retries up to 5 times with exponential backoff (1 ms → 16 ms,
    total ≤ 31 ms) to survive transient PermissionError from concurrent readers.
    """
    delay = 0.001
    last_exc: Exception
    for _ in range(5):
        try:
            src.replace(dst)
            return
        except PermissionError as exc:
            last_exc = exc
            _time.sleep(delay)
            delay *= 2
    raise last_exc

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_BASE_DIR       = Path(__file__).resolve().parent.parent.parent
RATE_FILE       = _BASE_DIR / "rate.json"
ORDER_BASE_URL  = os.getenv("ORDER_BASE_URL", "https://yoursite.com/order")
CURRENCY_SYMBOL = "IQD"
_DEFAULT_RATE   = 1310.0


# ── Rate cache ─────────────────────────────────────────────────────────────────

_rate_lock  = threading.Lock()
_rate_cache: dict = {
    "value": None,    # float | None
    "mtime": None,    # float | None  — mtime of rate.json at time of last read
}


def _rate_file_mtime() -> Optional[float]:
    try:
        return RATE_FILE.stat().st_mtime
    except FileNotFoundError:
        return None


def get_iqd_rate() -> float:
    """
    Return the current IQD/USD exchange rate.

    Reads from disk only when rate.json has been modified since the last call.
    Every call performs one cheap OS stat() — no disk read on a cache hit.

    Thread-safe: file reads are protected by _rate_lock.
    Concurrent-rename safe: if read_text() fails while os.replace() holds
    an exclusive lock on the file (Windows), the previous cached value is
    returned rather than falling back to the 1310 default.
    """
    mtime = _rate_file_mtime()

    with _rate_lock:
        if _rate_cache["mtime"] == mtime and _rate_cache["value"] is not None:
            return _rate_cache["value"]   # ← cache hit

        # File not accessible (first startup or file genuinely missing)
        if mtime is None:
            cached = _rate_cache["value"]
            if cached is not None:
                return cached             # ← keep previous value; don't corrupt cache
            _rate_cache["value"] = _DEFAULT_RATE
            _rate_cache["mtime"] = None
            return _DEFAULT_RATE

        try:
            data  = json.loads(RATE_FILE.read_text(encoding="utf-8"))
            value = float(data.get("iqd_rate", _DEFAULT_RATE))
        except Exception:
            # File exists (mtime valid) but read failed — likely locked during
            # a concurrent os.replace() on Windows.  Serve the cached value
            # and leave the cache entry unchanged so the next call retries.
            cached = _rate_cache["value"]
            if cached is not None:
                logger.debug("rate_file_locked — serving cached value %s", cached)
                return cached
            logger.warning("rate_file_read_error — using default %s", _DEFAULT_RATE)
            return _DEFAULT_RATE

        _rate_cache["value"] = value
        _rate_cache["mtime"] = mtime
        logger.debug("rate_cache_refreshed value=%s mtime=%s", value, mtime)
        return value


def update_iqd_rate(rate: float) -> None:
    """
    Atomically write a new IQD rate to disk and immediately update the
    in-memory cache so the very next `get_iqd_rate()` call returns the
    new value without a disk re-read.

    Uses write-to-temp + rename for atomicity.
    """
    payload = json.dumps({"iqd_rate": rate}, ensure_ascii=False, indent=2)
    dir_    = RATE_FILE.parent
    tmp_path: Optional[Path] = None

    try:
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp", prefix=".rate_")
        tmp_path = Path(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        _atomic_replace(tmp_path, RATE_FILE)
        tmp_path = None
    except Exception:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    new_mtime = _rate_file_mtime()
    with _rate_lock:
        _rate_cache["value"] = rate
        _rate_cache["mtime"] = new_mtime

    logger.info("iqd_rate_updated value=%s mtime=%s", rate, new_mtime)


def invalidate_rate_cache() -> None:
    """Force next get_iqd_rate() call to re-read from disk."""
    with _rate_lock:
        _rate_cache["value"] = None
        _rate_cache["mtime"] = None


# ── Price helpers ──────────────────────────────────────────────────────────────

def is_valid_raw_price(value) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def convert_to_iqd(price_usd: float) -> str:
    """Convert a USD price to a formatted IQD string using the cached rate."""
    if not is_valid_raw_price(price_usd):
        return "N/A"
    iqd_price = int(float(price_usd) * get_iqd_rate())
    return f"{iqd_price:,} {CURRENCY_SYMBOL}"


# ── Product list helpers ───────────────────────────────────────────────────────

def sort_products(products: List[Dict], sort_by: str = "item_name") -> List[Dict]:
    if sort_by == "price_asc":
        return sorted(products, key=lambda x: x.get("price", 0))
    if sort_by == "price_desc":
        return sorted(products, key=lambda x: x.get("price", 0), reverse=True)
    return sorted(products, key=lambda x: x.get("item_name", "").lower())


def format_products(products: List[Dict], limit: int = 4) -> List[Dict]:
    """
    Shape raw product dicts into the structure expected by the AI and frontend.
    Filters out products with invalid prices.

    IQD conversion is done via `convert_to_iqd()` which uses the cached rate.
    The rate is read from disk at most ONCE per unique mtime, not once per product.
    """
    if not isinstance(products, list):
        return []

    formatted: List[Dict] = []
    for p in products:
        if len(formatted) >= limit:
            break
        if not isinstance(p, dict):
            continue

        raw_price = p.get("price", 0)
        if not is_valid_raw_price(raw_price):
            continue

        barcode = p.get("barcode", "Unknown")
        img_url = str(p.get("image_url")).strip() if p.get("image_url") else None
        if img_url and img_url.lower() == "not found":
            img_url = None

        formatted.append({
            "id":            barcode,
            "name":          p.get("item_name", "Product"),
            "price":         convert_to_iqd(raw_price),
            "raw_price":     float(raw_price),
            "available_qty": p.get("available_qty", 0),
            "description":   p.get("description", ""),
            "category":      p.get("category_name", "Beauty & Personal Care"),
            "brand":         p.get("brand_name", ""),
            "image_url":     img_url,
            "order_link":    f"{ORDER_BASE_URL}/{barcode}",
        })

    return formatted
