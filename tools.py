"""
tools.py — backward-compatibility shim
=======================================
All symbols have moved to ai/tools/.
This file re-exports everything so that existing imports continue to work:
    from tools import search_products, format_products, convert_to_iqd, ...

Do not add new logic here. Use ai/tools/ directly for new code.
"""
from ai.tools import (  # noqa: F401
    BASE_URL,
    CURRENCY_SYMBOL,
    ORDER_BASE_URL,
    RATE_FILE,
    check_availability,
    convert_to_iqd,
    format_products,
    get_iqd_rate,
    get_product_details,
    is_valid_raw_price,
    search_product_index,
    search_products,
    sort_products,
)
