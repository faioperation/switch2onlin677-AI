"""
ai/tool_registry.py
====================
Central registry for all GPT tool definitions and their execution logic.

Tools and when GPT should use each one:
  search_products       -- user names a specific product, brand, or concern
  get_product_details   -- user wants full details on one product (by code)
  check_availability    -- broad "do you carry X?" questions
  get_recommendations   -- "recommend me something", general suggestions
  get_best_selling      -- "what's popular / best-selling?"
  get_new_arrivals      -- "what's new / latest arrivals?"
  get_featured_products -- cold-start discovery, "show me what you have"
  get_similar_products  -- "something like X", alternatives to a product
"""
from __future__ import annotations

import json
import logging
from typing import Any

from database import SessionLocal
from services.recommendation import (
    get_best_selling   as _svc_best_selling,
    get_featured       as _svc_featured,
    get_new_arrivals   as _svc_new_arrivals,
    get_recommended    as _svc_recommended,
)
from services.recommendation_cache import apply_diversity, record_recommendations

logger = logging.getLogger(__name__)


# ── Tool definitions passed to the OpenAI API ─────────────────────────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Search products by keyword, ItemCode, brand name, or skin concern. "
                "Use when the user mentions a specific product name or asks to find or search for something. "
                "Always translate the search keyword to English for best results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "English search keyword (e.g. 'vitamin c serum', 'Dior perfume', 'acne cleanser')",
                    },
                    "max_price":  {"type": "number",  "description": "Maximum price filter"},
                    "min_price":  {"type": "number",  "description": "Minimum price filter"},
                    "in_stock":   {"type": "boolean", "description": "Return only in-stock items when true"},
                    "category": {
                        "type": "string",
                        "description": "Category filter (e.g. 'Perfume', 'Skincare', 'Hair care', 'Makeup')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 8). Use 4-5 for recommendations, up to 10 for broad searches.",
                    },
                    "skip":    {"type": "integer", "description": "Pagination offset (default 0)"},
                    "sort_by": {"type": "string",  "description": "Sort: 'name', 'price_asc', 'price_desc'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Get full details of a specific product by its barcode or ItemCode. Use after a search to get more info on one item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Product barcode or ItemCode from search results",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": (
                "Check whether a brand, product type, or concern exists in the catalog "
                "without showing items. Use for broad questions like 'do you have NYX?' "
                "or 'do you carry acne products?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to check (e.g. 'NYX', 'acne', 'cleanser')"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recommendations",
            "description": (
                "Get curated editorial recommendations (is_recommended=true products). "
                "Use when the user asks for general suggestions: 'recommend me a perfume', "
                "'what do you suggest?', 'what should I try?'. "
                "Prefer this over search_products for open-ended recommendation requests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional category (e.g. 'Perfume', 'Skincare', 'Makeup')",
                    },
                    "price_tier": {
                        "type": "string",
                        "description": "Optional tier: 'Budget', 'Mid', 'Premium', or 'Luxury'",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 6)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_best_selling",
            "description": (
                "Get the best-selling or most popular products. "
                "Use when the user asks about 'best sellers', 'most popular', "
                "'what everyone loves', 'top products', or 'what is trending'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional category filter (e.g. 'Perfume', 'Skincare')",
                    },
                    "price_tier": {
                        "type": "string",
                        "description": "Optional tier: 'Budget', 'Mid', 'Premium', 'Luxury'",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 6)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_new_arrivals",
            "description": (
                "Get the latest newly arrived products. "
                "Use when the user asks 'what is new?', 'latest arrivals', "
                "'recent additions', 'just arrived'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional category filter",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 6)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_featured_products",
            "description": (
                "Get a curated mix of best-sellers and new arrivals. "
                "Use for cold-start discovery, 'show me what you have', "
                "or when the user has no specific request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 8)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_similar_products",
            "description": (
                "Find products similar to a specific item using AI vector similarity. "
                "Use when the user says 'something like X', 'similar to this', "
                "'alternatives to [product name]', 'what else is like this?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "Barcode or ItemCode of the reference product",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["product_id"],
            },
        },
    },
]


# ── Helper: category name → DB integer ID ─────────────────────────────────────

def _resolve_category_id(db, name: str | None) -> int | None:
    if not name:
        return None
    from models import Category
    row = (
        db.query(Category)
        .filter(Category.name.ilike(f"%{name}%"), Category.is_active == 1)
        .first()
    )
    return row.id if row else None


# ── Helper: apply diversity + record shown products ────────────────────────────

def _with_diversity(result: dict, user_id: str | None) -> dict:
    """Apply session diversity filtering and record shown barcodes."""
    if not result.get("found") or not result.get("products"):
        return result

    products = apply_diversity(result["products"], user_id)
    result["products"] = products
    result["returned"]  = len(products)

    if user_id:
        barcodes = [p.get("id") or p.get("barcode") for p in products]
        record_recommendations(user_id, [b for b in barcodes if b])

    return result


# ── Tool executors ─────────────────────────────────────────────────────────────

def execute_tool_with_db(
    tool_name: str,
    args:      dict[str, Any],
    user_id:   str | None = None,
    db         = None,
) -> str:
    """
    Dispatch a tool call using an EXISTING database session.

    WHY: The V1 execute_tool() opened a new session per tool call.  In a
    6-tool-loop turn that means 6 connection pool checkouts + commits + closes.
    Under concurrent load (50+ users) this exhausts the pool quickly.

    The async orchestrator now passes its own per-request DB session here so
    the entire tool loop shares one connection. The orchestrator is responsible
    for closing the session after the turn completes.

    Falls back to opening its own session if db=None (preserves backward
    compatibility with any direct callers).
    """
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        result = _dispatch(tool_name, args, user_id, db)

        # Cache popular tool results in Redis (best_sellers, new_arrivals, etc.)
        _maybe_cache_result(tool_name, args, result)

        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.error("tool_error tool=%s error=%s", tool_name, exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return json.dumps({"found": False, "error": f"Tool error: {exc}"})
    finally:
        if owns_session:
            db.close()


def execute_tool(
    tool_name: str,
    args:      dict[str, Any],
    user_id:   str | None = None,
) -> str:
    """
    Legacy sync entry point (opens its own DB session).
    Kept for backward compatibility. Prefer execute_tool_with_db().
    """
    return execute_tool_with_db(tool_name, args, user_id, db=None)


def _maybe_cache_result(tool_name: str, args: dict, result: dict) -> None:
    """Write tool results to Redis cache for repeated identical queries."""
    if not result.get("found"):
        return
    try:
        from services import cache_service as cs
        products = result.get("products", [])
        if not products:
            return

        cat   = args.get("category")
        tier  = args.get("price_tier")
        limit = args.get("limit", 6)

        if tool_name == "get_best_selling":
            cs.set_best_sellers(cat, tier, products)
        elif tool_name == "get_new_arrivals":
            cs.set_new_arrivals(cat, products)
        elif tool_name == "get_featured_products":
            cs.set_featured(limit, products)
        elif tool_name == "get_recommendations":
            cs.set_recommended(cat, tier, products)
    except Exception:
        pass   # cache write failure is never fatal


def _dispatch(tool_name: str, args: dict, user_id: str | None, db) -> dict:
    """Route tool calls to the correct service function.

    search_products, get_product_details, and check_availability receive the
    dispatcher's open session via `db=db` so they do not open a second
    connection per tool call.
    """
    from ai.tools.product_search import search_products, get_product_details
    from ai.tools.availability import check_availability

    if tool_name == "search_products":
        return search_products(
            query     = args.get("query", ""),
            max_price = args.get("max_price"),
            min_price = args.get("min_price"),
            in_stock  = args.get("in_stock"),
            category  = args.get("category"),
            limit     = args.get("limit", 8),
            skip      = args.get("skip", 0),
            sort_by   = args.get("sort_by", "item_name"),
            db        = db,
        )

    if tool_name == "get_product_details":
        return get_product_details(args["product_id"], db=db)

    if tool_name == "check_availability":
        return check_availability(args["query"], db=db)

    if tool_name == "get_recommendations":
        cat  = args.get("category")
        tier = args.get("price_tier")
        # Cache read first
        try:
            from services import cache_service as cs
            cached = cs.get_recommended(cat, tier)
            if cached is not None:
                return _with_diversity({"found": True, "products": cached, "returned": len(cached)}, user_id)
        except Exception:
            pass
        result = _svc_recommended(
            db          = db,
            category_id = _resolve_category_id(db, cat),
            price_tier  = tier,
            limit       = args.get("limit", 6),
        )
        return _with_diversity(result, user_id)

    if tool_name == "get_best_selling":
        cat  = args.get("category")
        tier = args.get("price_tier")
        try:
            from services import cache_service as cs
            cached = cs.get_best_sellers(cat, tier)
            if cached is not None:
                return _with_diversity({"found": True, "products": cached, "returned": len(cached)}, user_id)
        except Exception:
            pass
        result = _svc_best_selling(
            db          = db,
            category_id = _resolve_category_id(db, cat),
            price_tier  = tier,
            limit       = args.get("limit", 6),
        )
        return _with_diversity(result, user_id)

    if tool_name == "get_new_arrivals":
        cat = args.get("category")
        try:
            from services import cache_service as cs
            cached = cs.get_new_arrivals(cat)
            if cached is not None:
                return _with_diversity({"found": True, "products": cached, "returned": len(cached)}, user_id)
        except Exception:
            pass
        result = _svc_new_arrivals(
            db          = db,
            category_id = _resolve_category_id(db, cat),
            limit       = args.get("limit", 6),
        )
        return _with_diversity(result, user_id)

    if tool_name == "get_featured_products":
        limit = args.get("limit", 8)
        try:
            from services import cache_service as cs
            cached = cs.get_featured(limit)
            if cached is not None:
                return _with_diversity({"found": True, "products": cached, "returned": len(cached)}, user_id)
        except Exception:
            pass
        result = _svc_featured(db=db, limit=limit)
        return _with_diversity(result, user_id)

    if tool_name == "get_similar_products":
        return _similar_products(db, args, user_id)

    logger.warning("unknown_tool tool=%s", tool_name)
    return {"error": f"Unknown tool: {tool_name}"}


def _similar_products(db, args: dict, user_id: str | None) -> dict:
    """
    Vector-similarity lookup with graceful fallback to keyword search.
    Fallback activates when catalog embeddings are not yet generated.
    """
    try:
        from services.ai_recommendation import similar_products
        result = similar_products(db, args["product_id"], limit=args.get("limit", 5))
        return _with_diversity(result, user_id)
    except Exception as exc:
        logger.warning(
            "vector_similarity_fallback barcode=%s error=%s",
            args.get("product_id"), exc,
        )
        from tools import search_products
        return search_products(
            query = str(args.get("product_id", "")),
            limit = args.get("limit", 5),
        )
