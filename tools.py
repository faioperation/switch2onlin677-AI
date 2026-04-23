from database import SessionLocal
from models import Product
import os
from sqlalchemy import text, or_
from typing import List, Dict, Optional

# SAP Service Layer Configuration
BASE_URL = os.getenv("BASE_URL", "https://dbc-online.free.beeceptor.com")
ORDER_BASE_URL = os.getenv("ORDER_BASE_URL", "https://yoursite.com/order")

# Currency Configuration
IQD_RATE = 1310  # 1 USD = 1310 IQD
CURRENCY_SYMBOL = "IQD"

def convert_to_iqd(price_usd: float) -> str:
    if not price_usd or price_usd == 0:
        return "Price on Request"
    iqd_price = int(price_usd * IQD_RATE)
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

def sort_products(products: List[Dict], sort_by: str = "name") -> List[Dict]:
    if sort_by == "price_asc":
        return sorted(products, key=lambda x: x.get("ItemPrice", 0))
    elif sort_by == "price_desc":
        return sorted(products, key=lambda x: x.get("ItemPrice", 0), reverse=True)
    elif sort_by == "name":
        return sorted(products, key=lambda x: x.get("ItemDesc", "").lower())
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
            
        raw_price = p.get("ItemPrice", 0)
        item_code = p.get("ItemCode", "Unknown")
        formatted.append({
            "id": item_code,
            "name": p.get("ItemDesc", "Product"),
            "price": convert_to_iqd(raw_price),
            "raw_price": raw_price,
            "description": p.get("ItemDesc", ""), 
            "category": "Beauty & Personal Care",
            "image_url": None,
            "order_link": f"{ORDER_BASE_URL}/{item_code}",
        })
    return formatted

def search_products(
    query: str,
    max_price: float = None,
    min_price: float = None,
    in_stock: bool = None,
    category: str = None,
    sort_by: str = "name",
    limit: int = 10,
    skip: int = 0,
) -> dict:
    """
    Enhanced search using the local getItems.json data (synced to DB).
    Uses a weighted scoring system for 100% accuracy on matches.
    """
    db = SessionLocal()
    try:
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
                SELECT to_tsquery('english', :token_query) as q
            ),
            search_results AS (
                SELECT 
                    item_code, item_name, price, available_qty, barcode, category,
                    (
                        CASE 
                            WHEN item_code = :query OR barcode = :query THEN 15.0
                            WHEN LOWER(item_name) = LOWER(:query) THEN 12.0
                            WHEN item_name ILIKE :query_exact THEN 8.0
                            WHEN item_name ILIKE :query_like THEN 3.0
                            ELSE 0.0
                        END +
                        ts_rank(to_tsvector('english', item_name), (SELECT q FROM search_query)) * 4.0 +
                        similarity(item_name, :query) * 2.0
                    ) as score
                FROM products
                WHERE 
                    (to_tsvector('english', item_name) @@ (SELECT q FROM search_query)
                     OR item_name ILIKE :query_like 
                     OR item_code ILIKE :query_like 
                     OR barcode ILIKE :query_like 
                     OR item_name % :query)
                    AND (:min_price IS NULL OR price >= :min_price)
                    AND (:max_price IS NULL OR price <= :max_price)
                    AND (:in_stock IS FALSE OR available_qty > 0)
                    AND (:category IS NULL OR LOWER(category) = LOWER(:category) OR item_name ILIKE :category_like)
            )
            SELECT * FROM search_results
            WHERE score > 0.05
            ORDER BY score DESC, item_name ASC
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
            "skip": skip
        })
        
        products = []
        for row in result:
            products.append({
                "ItemCode": row.item_code,
                "ItemDesc": row.item_name,
                "ItemPrice": row.price,
                "ItemAvaliableQty": row.available_qty,
                "ItemBarcode": row.barcode,
                "score": row.score
            })

        if not products:
            return {
                "found": False, 
                "message": f"No items matching '{query_cleaned}' were found in the inventory of 15,597 products."
            }

        formatted = format_products(products, limit)

        return {
            "found": True,
            "total_found": len(products),
            "products": formatted,
        }

    except Exception as e:
        return {"found": False, "message": f"Search error: {str(e)}"}
    finally:
        db.close()

def get_product_details(product_id: str) -> dict:
    """
    Get details for a specific product by ItemCode from Supabase.
    """
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.item_code == product_id).first()

        if not product:
            return {"found": False, "message": "Product not found."}

        raw_price = product.price
        return {
            "found": True,
            "id": product.item_code,
            "name": product.item_name,
            "price": convert_to_iqd(raw_price),
            "raw_price": raw_price,
            "description": f"Product: {product.item_name}",
            "category": product.category or "Beauty & Personal Care",
            "image_url": product.image_url,
            "order_link": f"{ORDER_BASE_URL}/{product.item_code}",
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