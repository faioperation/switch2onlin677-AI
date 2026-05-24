"""
product_upload_service.py — compatibility shim
===============================================
This module has moved to services/upload.py.
This file re-exports everything so existing imports keep working.
"""
from services.upload import (  # noqa: F401
    upsert_product_upload,
    read_product_upload_file,
    validate_upload_columns,
    build_search_text,
    parse_concerns,
    parse_tags,
    REQUIRED_PRODUCT_UPLOAD_COLUMNS,
    OPTIONAL_PRODUCT_UPLOAD_COLUMNS,
    ALL_PRODUCT_UPLOAD_COLUMNS,
    TAG_ALIASES,
)
