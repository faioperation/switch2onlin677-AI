"""
ai/tools/
=========
AI tool functions for the GPT tool-call pipeline.
Re-exports all public symbols so callers can do:
    from ai.tools import search_products, format_products, ...
"""
from ai.tools.formatters import (
    CURRENCY_SYMBOL,
    ORDER_BASE_URL,
    RATE_FILE,
    convert_to_iqd,
    format_products,
    get_iqd_rate,
    is_valid_raw_price,
    sort_products,
)
from ai.tools.product_search import (
    BASE_URL,
    get_product_details,
    search_product_index,
    search_products,
)
from ai.tools.availability import check_availability

__all__ = [
    # Formatters
    "get_iqd_rate", "is_valid_raw_price", "convert_to_iqd",
    "sort_products", "format_products",
    "RATE_FILE", "ORDER_BASE_URL", "CURRENCY_SYMBOL",
    # Product search
    "search_product_index", "search_products", "get_product_details", "BASE_URL",
    # Availability
    "check_availability",
]
