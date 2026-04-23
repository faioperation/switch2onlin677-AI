import json
import os
import time
from sqlalchemy.orm import Session
from sqlalchemy import text
from database import SessionLocal, engine
from models import Product, Base
from dotenv import load_dotenv

load_dotenv()

def sync_from_json():
    """Load products from getItems.json and sync to the database."""
    json_path = os.path.join(os.path.dirname(__file__), "getItems.json")
    
    if not os.path.exists(json_path):
        print(f"Error: {json_path} not found. Cannot sync.")
        return

    print(f"Starting synchronization from {json_path}...")
    
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            items = json.load(f)
            
        if not isinstance(items, list):
            print("Error: JSON content is not a list.")
            return

        db = SessionLocal()
        
        # 1. Clear existing products to ensure 100% match with JSON
        print("Clearing existing products table...")
        db.execute(text("DELETE FROM products"))
        db.commit()

        batch = []
        batch_size = 1000
        total_synced = 0
        
        # SQL for bulk insert
        insert_sql = text("""
            INSERT INTO products (item_code, item_name, barcode, price, available_qty, last_updated)
            VALUES (:item_code, :item_name, :barcode, :price, :available_qty, CURRENT_TIMESTAMP)
        """)
        
        print(f"Syncing {len(items)} products...")
        
        for item in items:
            # Map JSON fields to DB columns with null safety
            raw_price = item.get("ItemPrice")
            raw_qty = item.get("ItemAvaliableQty")
            
            batch.append({
                "item_code": str(item.get("ItemCode", "")).strip(),
                "item_name": str(item.get("ItemDesc", "Unknown Product")).strip(),
                "barcode": str(item.get("ItemBarcode", "")).strip(),
                "price": float(raw_price) if raw_price is not None else 0.0,
                "available_qty": int(raw_qty) if raw_qty is not None else 0
            })
            
            if len(batch) >= batch_size:
                db.execute(insert_sql, batch)
                db.commit()
                total_synced += len(batch)
                print(f"Synced {total_synced} products...")
                batch = []
                
        if batch:
            db.execute(insert_sql, batch)
            db.commit()
            total_synced += len(batch)
            
        print(f"Sync complete! Total products from JSON: {total_synced}")
        
    except Exception as e:
        print(f"JSON sync failed: {e}")
    finally:
        db.close()

def sync_products():
    """Main sync entry point."""
    sync_from_json()

if __name__ == "__main__":
    # Ensure tables exist
    Base.metadata.create_all(bind=engine)
    sync_products()
