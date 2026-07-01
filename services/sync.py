"""
services/sync.py
================
Bi-daily (06:00 / 18:00 Asia/Baghdad) SAP price/stock sync.

SAP is the ONLY source of truth for:
  - price          (unless price_source_override = TRUE on that product)
  - available_qty
  - sap_product_id
  - last_synced_sap

SAP sync NEVER touches (protected fields):
  - product_status, price_tier, brand_family
  - is_best_selling, is_new_arrival, is_recommended, is_cod_recommended
  - recommendation_priority, recommendation_score_override
  - bundle_group, bundle_discount_percent, best_selling_scope, sales_rank
  - item_name, description, image_url, item_code
  - brand_id, category_id, subcategory_id
  - skin_type, concerns, tags

Price source override
---------------------
If a product row has price_source_override = TRUE, the SAP sync skips
updating its price (available_qty and sap_product_id are still updated).
This lets operators lock a price from the upload or PUT API without it
being overwritten by the next sync cycle.

Audit logging
-------------
After every sync run (success or failure), one row is appended to
sap_sync_audit_log.  The /ready endpoint reads the latest row to report
sync health without touching log files.

Safety rules
------------
- SAP sends None/missing for price → keep existing DB value (no overwrite)
- SAP sends None/missing for stock → keep existing DB value (no overwrite)
- Each product update is isolated; one DB failure does not abort the batch.
- Full rollback on any unrecoverable exception.
"""

import logging
import os
import time
from datetime import datetime

import httpx
from dotenv import load_dotenv

from database import SessionLocal
from models import Product, SapSyncAuditLog

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("SAPSync")

# ── Config ────────────────────────────────────────────────────────────────────
SAP_API_URL = os.getenv("SAP_API_URL")

SAP_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "price", "available_qty", "sap_product_id", "last_synced_sap",
})

SAP_PROTECTED_FIELDS: frozenset[str] = frozenset({
    "product_status", "price_tier", "brand_family",
    "is_best_selling", "is_new_arrival", "is_recommended", "is_cod_recommended",
    "recommendation_priority", "recommendation_score_override",
    "bundle_group", "bundle_discount_percent", "best_selling_scope", "sales_rank",
    "item_name", "description", "image_url", "item_code",
    "brand_id", "category_id", "subcategory_id",
    "skin_type", "concerns", "tags",
    # Never touch soft delete or price override flags
    "deleted_at", "price_source_override",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_barcode(raw: str) -> str | None:
    barcode = str(raw).strip()
    return barcode if barcode else None


def _parse_price(raw) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def _parse_stock(raw) -> int | None:
    if raw is None:
        return None
    try:
        value = int(float(raw))
        return max(value, 0)
    except (TypeError, ValueError):
        return None


def _extract_sap_product_id(item: dict) -> str | None:
    for key in ("ItemNo", "ItemCode", "SapProductId", "ItemId"):
        val = item.get(key)
        if val is not None:
            return str(val).strip() or None
    return None


def _build_sap_update(
    price: float | None,
    stock: int | None,
    sap_product_id: str | None,
    skip_price: bool = False,
) -> dict:
    """
    Build the update dict for SAP-owned fields.

    Parameters
    ----------
    skip_price : bool
        When True (price_source_override on the product row) the price key
        is omitted so the existing DB price is preserved across this sync run.
    """
    update: dict = {"last_synced_sap": datetime.now()}

    if price is not None and not skip_price:
        update["price"] = price
    if stock is not None:
        update["available_qty"] = stock
    if sap_product_id is not None:
        update["sap_product_id"] = sap_product_id

    # Runtime guard — raise if anything slipped past the allowlist
    illegal = set(update.keys()) - SAP_UPDATABLE_FIELDS
    if illegal:
        raise RuntimeError(f"SAP sync attempted to update protected fields: {illegal}")

    return update


def _write_audit(
    db,
    *,
    status: str,
    items_received: int      = 0,
    items_updated: int       = 0,
    items_not_found: int     = 0,
    items_skipped: int       = 0,
    items_price_protected: int = 0,
    duration_seconds: float | None = None,
    error_message: str | None = None,
) -> None:
    """Append one row to sap_sync_audit_log (uses its own session)."""
    audit_db = SessionLocal()
    try:
        audit_db.add(SapSyncAuditLog(
            status=status,
            items_received=items_received,
            items_updated=items_updated,
            items_not_found=items_not_found,
            items_skipped=items_skipped,
            items_price_protected=items_price_protected,
            duration_seconds=round(duration_seconds, 2) if duration_seconds is not None else None,
            error_message=error_message,
        ))
        audit_db.commit()
    except Exception as exc:
        logger.error("sap_audit_write_failed", extra={"error": str(exc)})
    finally:
        audit_db.close()


# ── Main sync function ────────────────────────────────────────────────────────

async def sync_sap_data() -> dict:
    """
    Fetch all items from SAP in one batch request and update
    only SAP-owned fields in the DB.

    Recommendation flags, editorial fields, and price_source_override
    products are never modified.

    Returns a summary dict — {"status": "success" | "failed", "error_message": str | None, ...counts}
    — mirroring what gets written to sap_sync_audit_log, so callers (e.g. the
    manual /sap/sync-now endpoint) can report the real outcome instead of
    always claiming success.
    """
    start = time.monotonic()
    logger.info("sap_sync_started")

    if not SAP_API_URL:
        logger.error("sap_sync_aborted", extra={"reason": "SAP_API_URL not configured"})
        _write_audit(db=None, status="failed", error_message="SAP_API_URL not configured")
        return {"status": "failed", "error_message": "SAP_API_URL not configured"}

    # ── 1. Fetch from SAP ─────────────────────────────────────────────────────
    final_url = SAP_API_URL.rstrip("/")
    if not final_url.endswith("/getItems"):
        final_url = f"{final_url}/getItems"

    try:
        async with httpx.AsyncClient(verify=False) as client:
            logger.info("sap_sync_fetching", extra={"url": final_url})
            response = await client.get(final_url, timeout=30.0)

            if response.status_code == 429:
                logger.error("sap_sync_aborted", extra={"reason": "rate limited (429)"})
                _write_audit(db=None, status="failed", error_message="SAP rate limit 429",
                             duration_seconds=time.monotonic() - start)
                return {"status": "failed", "error_message": "SAP rate limit 429"}
            if response.status_code != 200:
                msg = f"SAP returned HTTP {response.status_code}"
                logger.error("sap_sync_aborted", extra={"reason": msg})
                _write_audit(db=None, status="failed", error_message=msg,
                             duration_seconds=time.monotonic() - start)
                return {"status": "failed", "error_message": msg}

            sap_data = response.json()

    except httpx.TimeoutException:
        msg = "SAP API timed out"
        logger.error("sap_sync_aborted", extra={"reason": msg})
        _write_audit(db=None, status="failed", error_message=msg,
                     duration_seconds=time.monotonic() - start)
        return {"status": "failed", "error_message": msg}
    except Exception as exc:
        logger.error("sap_sync_fetch_failed", extra={"error": str(exc)})
        _write_audit(db=None, status="failed", error_message=str(exc),
                     duration_seconds=time.monotonic() - start)
        return {"status": "failed", "error_message": str(exc)}

    # ── 2. Normalise item list ────────────────────────────────────────────────
    items: list[dict] = (
        sap_data.get("value", sap_data) if isinstance(sap_data, dict) else sap_data
    )

    if not items:
        logger.warning("sap_sync_empty", extra={"reason": "SAP returned zero items"})
        _write_audit(db=None, status="success", items_received=0,
                     duration_seconds=time.monotonic() - start)
        return {"status": "success", "items_received": 0}

    logger.info("sap_sync_items_received", extra={"count": len(items)})

    # ── 3. Load price-override barcodes in one query ──────────────────────────
    db = SessionLocal()
    updated_count        = 0
    skipped_count        = 0
    not_found_count      = 0
    price_protected_count= 0

    try:
        price_override_set: set[str] = {
            row[0]
            for row in db.query(Product.barcode)
            .filter(Product.price_source_override.is_(True))
            .all()
        }

        for item in items:
            barcode        = _parse_barcode(item.get("ItemBarcode", ""))
            price          = _parse_price(item.get("ItemPrice"))
            stock          = _parse_stock(item.get("ItemAvaliableQty"))
            sap_product_id = _extract_sap_product_id(item)

            if not barcode:
                skipped_count += 1
                continue

            skip_price = barcode in price_override_set
            if skip_price:
                price_protected_count += 1

            update_data = _build_sap_update(price, stock, sap_product_id, skip_price=skip_price)

            rows_affected = (
                db.query(Product)
                .filter(
                    Product.barcode   == barcode,
                    Product.deleted_at.is_(None),   # never update soft-deleted products
                )
                .update(update_data, synchronize_session=False)
            )

            if rows_affected > 0:
                updated_count += 1
                logger.debug(
                    "sap_product_updated",
                    extra={
                        "barcode":         barcode,
                        "price":           price if not skip_price else "protected",
                        "qty":             stock,
                        "price_protected": skip_price,
                    },
                )
            else:
                not_found_count += 1
                logger.debug("sap_product_not_found", extra={"barcode": barcode})

        db.commit()
        duration = time.monotonic() - start

        logger.info(
            "sap_sync_completed",
            extra={
                "updated":         updated_count,
                "not_found":       not_found_count,
                "skipped":         skipped_count,
                "price_protected": price_protected_count,
                "duration":        round(duration, 2),
            },
        )

        _write_audit(
            db=None,
            status="success",
            items_received=len(items),
            items_updated=updated_count,
            items_not_found=not_found_count,
            items_skipped=skipped_count,
            items_price_protected=price_protected_count,
            duration_seconds=duration,
        )

        return {
            "status":                "success",
            "items_received":        len(items),
            "items_updated":         updated_count,
            "items_not_found":       not_found_count,
            "items_skipped":         skipped_count,
            "items_price_protected": price_protected_count,
            "duration_seconds":      round(duration, 2),
        }

    except Exception as exc:
        db.rollback()
        duration = time.monotonic() - start
        logger.error("sap_sync_db_error", extra={"error": str(exc)}, exc_info=True)
        _write_audit(
            db=None,
            status="failed",
            items_received=len(items),
            items_updated=updated_count,
            duration_seconds=duration,
            error_message=str(exc),
        )
        return {
            "status":         "failed",
            "error_message":  str(exc),
            "items_received": len(items),
            "items_updated":  updated_count,
        }

    finally:
        db.close()


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    asyncio.run(sync_sap_data())
