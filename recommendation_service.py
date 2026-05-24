"""
recommendation_service.py — compatibility shim
===============================================
This module has moved to services/recommendation.py.
This file re-exports everything so existing imports keep working.
"""
from services.recommendation import (  # noqa: F401
    get_best_selling,
    get_new_arrivals,
    get_recommended,
    get_cod_recommended,
    get_by_price_tier,
    get_by_brand_family,
    get_bundle,
    get_featured,
    LOW_STOCK_THRESHOLD,
)
