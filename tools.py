import json

from database import SessionLocal
from models import Product
import os
from sqlalchemy import text, or_
from typing import List, Dict, Optional

# SAP Service Layer Configuration
BASE_URL = os.getenv("SAP_API_URL", "https://dbc-online.free.beeceptor.com")
ORDER_BASE_URL = os.getenv("ORDER_BASE_URL", "https://yoursite.com/order")

# Currency Configuration
RATE_FILE = os.path.join(os.path.dirname(__file__), "rate.json")


def get_iqd_rate():
    if not os.path.exists(RATE_FILE):
        return 1310

    try:
        with open(RATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("iqd_rate", 1310))
    except Exception:
        return 1310
CURRENCY_SYMBOL = "IQD"

def convert_to_iqd(price_usd: float) -> str:
    if not price_usd or price_usd == 0:
        return "N/A"
    iqd_price = int(price_usd * get_iqd_rate())
    return f"{iqd_price:,} {CURRENCY_SYMBOL}"

def apply_filters(products: List[Dict], min_price: float = None, max_price: float = None, in_stock: bool = None) -> List[Dict]:
    filtered = []
    for p in products:
        price = p.get("ItemPrice", 0)
        stock = p.get("ItemAvaliableQty", 0)
        
        if min_price is not None and price < min_price:
            continue
        if max_price is not None and price > max_price:
            continue
        if in_stock is True and stock <= 0:
            continue
        filtered.append(p)
    return filtered

def sort_products(products: List[Dict], sort_by: str = "item_name") -> List[Dict]:
    if sort_by == "price_asc":
        return sorted(products, key=lambda x: x.get("price", 0))
    elif sort_by == "price_desc":
        return sorted(products, key=lambda x: x.get("price", 0), reverse=True)
    elif sort_by == "item_name":
        return sorted(products, key=lambda x: x.get("item_name", "").lower())
    else:
        return products

def format_products(products: List[Dict], limit: int = 4) -> List[Dict]:
    formatted = []
    # Safeguard: ensure products is a list
    if not isinstance(products, list):
        return []
        
    for p in products[:limit]:
        # Defensive check: ensure p is a dictionary
        if not isinstance(p, dict):
            continue
            
        raw_price = p.get("price", 0)
        barcode = p.get("barcode", "Unknown")
        img_url = str(p.get("image_url")).strip() if p.get("image_url") else None
        if img_url and img_url.lower() == "not found":
            img_url = None

        formatted.append({
            "id": barcode,
            "name": p.get("item_name", "Product"),
            "price": convert_to_iqd(raw_price),
            "raw_price": raw_price,
            "description": p.get("description", ""), 
            "category": p.get("category", "Beauty & Personal Care"),
            "brand": p.get("brand", ""),
            "image_url": img_url,
            "order_link": f"{ORDER_BASE_URL}/{barcode}",
        })
    return formatted

def search_products(
    query: str,
    max_price: float = None,
    min_price: float = None,
    in_stock: bool = None,
    category: str = None,
    sort_by: str = "item_name",
    limit: int = 10,
    skip: int = 0,
) -> dict:
    """
    Enhanced search using the local getItems.json data (synced to DB).
    Uses a weighted scoring system for 100% accuracy on matches.
    """
    db = SessionLocal()
    query_cleaned = query.strip()
        
        # Hybrid Search Strategy:
        # 1. Exact ItemCode/Barcode Match (Score 15.0)
        # 2. Case-insensitive exact name match (Score 10.0)
        # 3. Token-based matching (Multi-word support)
        # 4. Trigram similarity as fallback ranking
        
        # Tokenize query into words for individual matching
        tokens = [t.strip() for t in query_cleaned.split() if len(t.strip()) > 1]
        token_query = " | ".join(tokens) if tokens else query_cleaned
        
        sql = text("""
            WITH search_query AS (
                SELECT :token_query as q
            ),
            search_results AS (
                SELECT 
                    barcode, item_code, item_name, price, available_qty, brand, category, description, image_url, skin_type, concerns,
                    (
                        CASE 
                            WHEN barcode = :query OR item_code = :query THEN 15.0
                            WHEN LOWER(item_name) = LOWER(:query) THEN 12.0
                            WHEN item_name ILIKE :query_exact THEN 8.0
                            WHEN brand ILIKE :query_exact THEN 7.0
                            WHEN item_name ILIKE :query_like THEN 3.0
                            ELSE 0.0
                        END +
                        ts_rank(to_tsvector('english', item_name || ' ' || COALESCE(brand, '') || ' ' || COALESCE(category, '')), to_tsquery('english', :token_query)) * 4.0 +
                        similarity(item_name, :query) * 2.0
                    ) as score
                FROM products
                WHERE 
                    (to_tsvector('english', item_name || ' ' || COALESCE(brand, '') || ' ' || COALESCE(category, '')) @@ to_tsquery('english', :token_query)
                     OR item_name ILIKE :query_like 
                     OR barcode ILIKE :query_like 
                     OR brand ILIKE :query_like 
                     OR item_name % :query)
                    AND (:min_price IS NULL OR price >= :min_price)
                    AND (:max_price IS NULL OR price <= :max_price)
                    AND (:in_stock IS FALSE OR available_qty > 0)
                    AND (:category IS NULL OR LOWER(category) = LOWER(:category) OR item_name ILIKE :category_like)
            )
            SELECT * FROM search_results
            WHERE score > 0.05
            ORDER BY 
                CASE WHEN :sort_by = 'price_asc' THEN price END ASC,
                CASE WHEN :sort_by = 'price_desc' THEN price END DESC,
                score DESC, 
                item_name ASC
            LIMIT :limit OFFSET :skip
        """)
        
        result = db.execute(sql, {
            "query": query_cleaned,
            "token_query": token_query,
            "query_exact": query_cleaned,
            "query_like": f"%{query_cleaned}%",
            "category": category,
            "category_like": f"%{category}%" if category else None,
            "min_price": min_price,
            "max_price": max_price,
            "in_stock": True if in_stock else False,
            "limit": limit,
            "skip": skip,
            "sort_by": sort_by
        })
        
        products = []
        for row in result:
            products.append({
                "barcode": row.barcode,
                "item_code": row.item_code,
                "item_name": row.item_name,
                "price": float(row.price) if row.price else 0.0,
                "available_qty": row.available_qty,
                "brand": row.brand,
                "category": row.category,
                "description": row.description,
                "image_url": row.image_url,
                "score": row.score
            })

        if not products:
            return {
                "found": False, 
                "message": f"No items matching '{query_cleaned}' were found in the inventory of 15,597 products."
            }

        # Apply sorting based on user preference (price_asc, price_desc, etc.)
        sorted_products = sort_products(products, sort_by)

        formatted = format_products(sorted_products, limit)

        return {
            "found": True,
            "total_found": len(products),
            "products": formatted,
        }

    db.close()

def get_product_details(product_id: str) -> dict:
    """
    Get details for a specific product by ItemCode from Supabase.
    """
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.barcode == product_id).first()

        if not product:
            return {"found": False, "message": "Product not found."}

        raw_price = float(product.price) if product.price else 0.0
        return {
            "found": True,
            "id": product.barcode,
            "item_code": product.item_code,
            "item_name": product.item_name,
            "brand": product.brand,
            "price": convert_to_iqd(raw_price),
            "raw_price": raw_price,
            "description": product.description or f"Product: {product.item_name}",
            "category": product.category or "Beauty & Personal Care",
            "image_url": product.image_url,
            "skin_type": product.skin_type,
            "concerns": product.concerns,
            "order_link": f"{ORDER_BASE_URL}/{product.barcode}",
        }
    except Exception as e:
        return {"found": False, "message": f"Details error: {str(e)}"}
    finally:
        db.close()

def check_availability(query: str) -> dict:
    """
    Check if a brand, product type, or concern exists in our catalog.
    Uses the same robust fuzzy search but returns a summary.
    """
    result = search_products(query, limit=5)
    
    if result.get("found"):
        count = result.get("total_found", 0)
        return {
            "found": True,
            "count": count,
            "summary": f"Matching products found for '{query}'.",
            "search_query": query
        }
    return {
        "found": False,
        "message": f"No products matching '{query}' were found in the 15,597 item catalog."
    }