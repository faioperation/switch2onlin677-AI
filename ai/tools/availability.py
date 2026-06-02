"""
ai/tools/availability.py
========================
Availability check tool — answers "do you carry X?" without listing products.
"""
from __future__ import annotations

from ai.tools.product_search import search_products


def check_availability(query: str) -> dict:
    """Check if a brand, product type, or concern exists without displaying items."""
    result = search_products(query, limit=5)
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
