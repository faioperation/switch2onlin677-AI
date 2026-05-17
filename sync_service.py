"""
sync_service.py
===============
Bi-daily (06:00 / 18:00 Asia/Baghdad) SAP price/stock sync.
Updates products.price, products.available_qty, products.sap_product_id,
and products.last_synced_sap from the SAP API.
"""
import os
import httpx
import logging
import json
from datetime import datetime
from database import SessionLocal
from models import Product
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("sap_sync.log"), logging.StreamHandler()]
)
logger = logging.getLogger("SAPSync")

SAP_API_URL = os.getenv("SAP_API_URL")


async def sync_sap_data():
    """
    Fetch all commerce data in ONE batch to avoid 429 Too Many Requests error.
    """
    logger.info("Starting Batch SAP Sync...")

    if not SAP_API_URL:
        logger.error("SAP_API_URL not found in .env")
        return

    try:
        # Determine the final URL (avoid duplicating /getItems)
        final_url = SAP_API_URL
        if not final_url.endswith("/getItems"):
            final_url = f"{final_url.rstrip('/')}/getItems"

        # 1. Try to fetch all data in a single request
        async with httpx.AsyncClient(verify=False) as client:
            logger.info(f"Connecting to: {final_url}")
            response = await client.get(final_url, timeout=30.0)

            if response.status_code == 429:
                logger.error("Rate limit hit (429). Switching to Local Cache if available.")
                # Fallback logic could go here
                return

            if response.status_code != 200:
                logger.error(f"API Error: {response.status_code}")
                return

            sap_data = response.json()
            # If the response is a list or contains a 'value' key (standard SAP OData)
            items = sap_data.get('value', sap_data) if isinstance(sap_data, dict) else sap_data

        if not items:
            logger.warning("No items received from SAP API.")
            return

        # 2. Update Database in a single Transaction
        db = SessionLocal()
        update_count = 0

        logger.info(f"Processing {len(items)} items from API...")

        for item in items:
            # Match keys based on your API response
            # Normalize barcode from API (strip leading zeros for matching)
            barcode = str(item.get("ItemBarcode", "")).strip().lstrip('0')
            price = item.get("ItemPrice")
            stock = item.get("ItemAvaliableQty")

            # SAP product ID — multiple possible field names; use whichever is present
            sap_pid = (
                item.get("ItemNo")
                or item.get("ItemCode")
                or item.get("SapProductId")
                or item.get("ItemId")
            )
            sap_product_id = str(sap_pid).strip() if sap_pid else None

            if not barcode:
                continue

            # Update only if barcode matches
            result = db.query(Product).filter(Product.barcode == barcode).update({
                "price": float(price) if price is not None else 0.0,
                "available_qty": int(stock) if stock is not None else 0,
                "sap_product_id": sap_product_id,
                "last_synced_sap": datetime.now()
            })

            if result > 0:
                update_count += 1

        db.commit()
        db.close()
        logger.info(f"Batch Sync Complete. Updated {update_count} products.")

    except Exception as e:
        logger.error(f"Sync Exception: {str(e)}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(sync_sap_data())
