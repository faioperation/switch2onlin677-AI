"""
services/sync.py
================
Bi-daily (06:00 / 18:00 Asia/Baghdad) SAP price/stock sync.

SAP is the ONLY source of truth for:
  - price
  - available_qty
  - sap_product_id
  - last_synced_sap

SAP sync NEVER touches (protected fields):
  - product_status
  - price_tier
  - brand_family
  - is_best_selling
  - is_new_arrival
  - is_recommended
  - is_cod_recommended
  - recommendation_priority
  - recommendation_score_override
  - bundle_group
  - bundle_discount_percent
  - best_selling_scope
  - sales_rank
  - item_name / description / image_url (display fields)

Safety rules:
  - If SAP sends None/missing for price → keep existing DB value (no overwrite)
  - If SAP sends None/missing for stock → keep existing DB value (no overwrite)
  - Each product update is isolated; one failure does not abort the batch.
  - Full rollback on any unrecoverable exception.
"""

import logging
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv

from database import SessionLocal
from models import Product

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("sap_sync.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("SAPSync")

# ── Config ────────────────────────────────────────────────────────────────────

SAP_API_URL = os.getenv("SAP_API_URL")

# Fields that SAP is ALLOWED to update — explicit allowlist.
# Anything not in this set is protected and will NEVER be touched.
SAP_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "price",
    "available_qty",
    "sap_product_id",
    "last_synced_sap",
})

# Fields that SAP must NEVER overwrite — for documentation and runtime guard.
SAP_PROTECTED_FIELDS: frozenset[str] = frozenset({
    "product_status",
    "price_tier",
    "brand_family",
    "is_best_selling",
    "is_new_arrival",
    "is_recommended",
    "is_cod_recommended",
    "recommendation_priority",
    "recommendation_score_override",
    "bundle_group",
    "bundle_discount_percent",
    "best_selling_scope",
    "sales_rank",
    "item_name",
    "description",
    "image_url",
    "item_code",
    "brand_id",
    "category_id",
    "subcategory_id",
    "skin_type",
    "concerns",
    "tags",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_barcode(raw: str) -> str | None:
    """Normalize a SAP barcode string. Returns None if empty."""
    barcode = str(raw).strip()
    return barcode if barcode else None


def _parse_price(raw) -> float | None:
    """
    Convert SAP price value to float.
    Returns None (not 0) when the value is missing or invalid
    so the existing DB price is preserved.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def _parse_stock(raw) -> int | None:
    """
    Convert SAP stock value to int.
    Returns None (not 0) when the value is missing or invalid
    so the existing DB quantity is preserved.
    """
    if raw is None:
        return None
    try:
        value = int(float(raw))
        return max(value, 0)
    except (TypeError, ValueError):
        return None


def _build_sap_update(
    price: float | None,
    stock: int | None,
    sap_product_id: str | None,
) -> dict:
    """
    Build the update dict containing ONLY SAP-owned fields.
    Fields with None values are excluded so the DB value is kept.
    last_synced_sap is always set.
    """
    update: dict = {"last_synced_sap": datetime.now()}

    if price is not None:
        update["price"] = price
    if stock is not None:
        update["available_qty"] = stock
    if sap_product_id is not None:
        update["sap_product_id"] = sap_product_id

    # Runtime safety guard — raise immediately if anything slipped in
    illegal = set(update.keys()) - SAP_UPDATABLE_FIELDS
    if illegal:
        raise RuntimeError(
            f"SAP sync attempted to update protected fields: {illegal}"
        )

    return update


def _extract_sap_product_id(item: dict) -> str | None:
    """Try multiple SAP field names for the internal product ID."""
    for key in ("ItemNo", "ItemCode", "SapProductId", "ItemId"):
        val = item.get(key)
        if val is not None:
            return str(val).strip() or None
    return None


# ── Main sync function ────────────────────────────────────────────────────────

async def sync_sap_data() -> None:
    """
    Fetch all items from SAP in one batch request and update
    only price / stock / sap_product_id / last_synced_sap in the DB.

    Recommendation flags and all other fields are never modified.
    """
    logger.info("SAP Sync started.")

    if not SAP_API_URL:
        logger.error("SAP_API_URL not configured in .env — aborting sync.")
        return

    # ── 1. Fetch from SAP ─────────────────────────────────────────────────────
    final_url = SAP_API_URL.rstrip("/")
    if not final_url.endswith("/getItems"):
        final_url = f"{final_url}/getItems"

    try:
        async with httpx.AsyncClient(verify=False) as client:
            logger.info(f"Fetching from SAP: {final_url}")
            response = await client.get(final_url, timeout=30.0)

            if response.status_code == 429:
                logger.error("SAP rate limit (429) — sync aborted.")
                return
            if response.status_code != 200:
                logger.error(
                    f"SAP API returned {response.status_code} — sync aborted."
                )
                return

            sap_data = response.json()

    except httpx.TimeoutException:
        logger.error("SAP API timed out — sync aborted.")
        return
    except Exception as exc:
        logger.error(f"SAP API fetch failed: {exc}")
        return

    # ── 2. Normalise item list ────────────────────────────────────────────────
    items: list[dict] = (
        sap_data.get("value", sap_data)
        if isinstance(sap_data, dict)
        else sap_data
    )

    if not items:
        logger.warning("SAP returned zero items — nothing to sync.")
        return

    logger.info(f"Received {len(items)} items from SAP.")

    # ── 3. Apply updates ──────────────────────────────────────────────────────
    db = SessionLocal()
    updated_count   = 0
    skipped_count   = 0
    not_found_count = 0

    try:
        for item in items:
            barcode        = _parse_barcode(item.get("ItemBarcode", ""))
            price          = _parse_price(item.get("ItemPrice"))
            stock          = _parse_stock(item.get("ItemAvaliableQty"))
            sap_product_id = _extract_sap_product_id(item)

            if not barcode:
                skipped_count += 1
                continue

            # Build update dict — only SAP-owned fields, only non-None values
            update_data = _build_sap_update(price, stock, sap_product_id)

            rows_affected = (
                db.query(Product)
                .filter(Product.barcode == barcode)
                .update(update_data, synchronize_session=False)
            )

            if rows_affected > 0:
                updated_count += 1
                logger.debug(
                    f"[OK] barcode={barcode} price={price} qty={stock}"
                )
            else:
                not_found_count += 1
                logger.debug(f"[MISS] barcode={barcode} not in products table.")

        db.commit()
        logger.info(
            f"SAP Sync complete — updated: {updated_count}, "
            f"not found: {not_found_count}, "
            f"skipped (no barcode): {skipped_count}."
        )

    except Exception as exc:
        db.rollback()
        logger.error(f"SAP Sync DB error — rolled back. Reason: {exc}")

    finally:
        db.close()


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    asyncio.run(sync_sap_data())
