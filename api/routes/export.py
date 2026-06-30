"""
api/routes/export.py
====================
Product catalog Excel export endpoint.

  GET /products/export

Returns a downloadable .xlsx file containing all product fields for every
product matching the supplied filters.  Pagination is bypassed — the full
matching result set is exported regardless of size.

Query parameters mirror GET /products exactly so the frontend can forward
its active filter state without any transformation.

Authentication
--------------
No FastAPI-level auth is implemented in this service (Django handles auth
before proxying requests here).  Add a Depends() guard here if the FastAPI
service is ever exposed directly to clients.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from core.exceptions import ServiceError
from database import get_db
from services.excel_export_service import generate_export_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["Export"])

_BAGHDAD_TZ = ZoneInfo("Asia/Baghdad")
_XLSX_MIME  = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get(
    "/products/export",
    summary="Export product catalog to Excel (.xlsx)",
    response_description="Downloadable XLSX file with all product fields",
    responses={
        200: {"content": {_XLSX_MIME: {}}, "description": "Excel workbook download"},
        500: {"description": "Export generation failed"},
    },
)
def export_products(
    # ── Filters (identical to GET /products) ─────────────────────────────────
    q:               Optional[str]   = None,
    brand_id:        Optional[int]   = None,
    category_id:     Optional[int]   = None,
    subcategory_id:  Optional[int]   = None,
    is_best_selling: Optional[int]   = None,
    in_stock:        Optional[bool]  = None,
    min_price:       Optional[float] = None,
    max_price:       Optional[float] = None,
    product_status:  Optional[str]   = None,
    sort_by:         Optional[str]   = "created_desc",
    db: Session = Depends(get_db),
):
    """
    Export all matching products as a professionally formatted Excel workbook.

    The workbook includes:
    - Report header: company name, generation timestamp, total count, active filters
    - 35 columns covering every product field (identity, AI, SAP, pricing, flags …)
    - Professional styling: dark navy headers, alternating row stripes, conditional
      stock coloring (green/amber/red), frozen header row, precise column widths
    - Currency formatting for USD and IQD prices
    - All filters from the product list view are supported and preserved

    Example requests
    ----------------
    # Full catalog
    GET /products/export

    # Filtered export (YSL Perfumes in stock)
    GET /products/export?brand_id=3&category_id=1&in_stock=true

    # Search with price range
    GET /products/export?q=rose&min_price=20&max_price=200&sort_by=price_asc
    """
    try:
        buf, total = generate_export_bytes(
            db,
            q=q,
            brand_id=brand_id,
            category_id=category_id,
            subcategory_id=subcategory_id,
            is_best_selling=is_best_selling,
            in_stock=in_stock,
            min_price=min_price,
            max_price=max_price,
            product_status=product_status,
            sort_by=sort_by,
        )
    except Exception as exc:
        logger.error("product_export_failed error=%s", exc, exc_info=True)
        raise ServiceError("Export generation failed. Please try again or contact support.")

    ts = datetime.now(tz=timezone.utc).astimezone(_BAGHDAD_TZ).strftime("%Y%m%d_%H%M%S")
    filename = f"Dhifaf_Products_{ts}.xlsx"

    logger.info("product_export_served filename=%s total=%d", filename, total)

    return StreamingResponse(
        content=buf,
        media_type=_XLSX_MIME,
        headers={
            "Content-Disposition":       f'attachment; filename="{filename}"',
            "X-Export-Total-Products":   str(total),
            "X-Export-Filename":         filename,
            "Cache-Control":             "no-store, no-cache, must-revalidate",
        },
    )
