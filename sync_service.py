"""
sync_service.py — compatibility shim
=====================================
This module has moved to services/sync.py.
This file re-exports everything so existing imports keep working.
"""
from services.sync import (  # noqa: F401
    sync_sap_data,
    SAP_UPDATABLE_FIELDS,
    SAP_PROTECTED_FIELDS,
    _parse_barcode,
    _parse_price,
    _parse_stock,
    _build_sap_update,
    _extract_sap_product_id,
)
