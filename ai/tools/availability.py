"""
ai/tools/availability.py
========================
Availability check tool — answers "do you carry X?" without listing products.

Session injection: accepts optional `db` parameter and forwards it to
search_products() to avoid opening a second database connection when called
from the tool dispatcher that already holds an open session.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ai.tools.product_search import search_products


def check_availability(
    query: str,
    db: Optional[Session] = None,
) -> dict:
    """
    Check if a brand, product type, or concern exists without displaying items.

    Pass `db` to reuse the caller's session (no extra connection opened).
    When `db` is None, search_products() opens its own session.
    """
    result = search_products(query, limit=5, db=db)
    if result.get("found"):
        return {
            "found":        True,
            "count":        result.get("total_found", 0),
            "summary":      f"Matching products found for '{query}'.",
            "search_query": query,
        }
    return {
        "found":   False,
        "message": f"No products matching '{query}' were found in the catalog.",
    }
