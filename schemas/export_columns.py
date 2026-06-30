"""
schemas/export_columns.py
==========================
Single source of truth for the product import/export column schema.

Both the Excel export generator (services/excel_export_service.py) and
the CSV/XLSX upload validator (services/upload.py) import from this module
so they stay permanently synchronized.

Column lifecycle
----------------
import_compatible=True   → column appears in exported XLSX and is processed
                           by the upload validator (barcode, item_name, price …)
import_compatible=False  → export-only / informational column; uploaded files
                           may contain it but the upload validator ignores it
                           silently (price_iqd, ai_score, stock_status …)

The column `name` field is the EXACT string used as the Excel column header.
This string must match what the upload validator normalises to — see
services/upload.py::_normalize_col_name() for the normalisation rules.

Adding a new import-compatible column
--------------------------------------
1. Add an ExportColumn entry here with import_compatible=True.
2. Add the field to ProductUploadRow (schemas/upload.py) if not already there.
3. Handle it in services/upload.py::_row_to_raw() and _apply_fields().
4. Add it to the SELECT list in services/excel_export_service.py::_build_export_query().
5. Update _row_to_dict() if a new DB field is needed.
"""
from __future__ import annotations

from typing import Any, Callable, NamedTuple, Optional


class ExportColumn(NamedTuple):
    """
    Describes one column in the import/export schema.

    name              : Exact Excel header string AND the upload column name.
    extractor         : How to pull the value from a serialised product dict —
                        "__row_num__" | dict-key string | callable(row_dict)->Any
    width             : Excel column width (character units).
    num_fmt           : openpyxl number format string, or None for General.
    import_compatible : False = export-only; upload validator ignores these.
    """
    name:              str
    extractor:         Any             # str | Callable — not typed strictly to allow lambda
    width:             int
    num_fmt:           Optional[str]
    import_compatible: bool = True


# ── Number format constants ───────────────────────────────────────────────────
# Defined here (not imported from excel_styles) to avoid a circular dependency.
FMT_TEXT    = "@"
FMT_INT     = "#,##0"
FMT_USD     = '"$"#,##0.00'
FMT_IQD     = '#,##0" IQD"'
FMT_DECIMAL = "0.000000"
FMT_PCT     = "0.00"

# ── Master column list ────────────────────────────────────────────────────────

PRODUCT_IMPORT_EXPORT_COLUMNS: list[ExportColumn] = [
    # ── Row counter (export-only: "#" is not a valid Python field name) ───────
    ExportColumn("#",                         "__row_num__",                                 5,  FMT_INT,  False),

    # ── Required ─────────────────────────────────────────────────────────────
    ExportColumn("barcode",                   "barcode",                                     18, FMT_TEXT),

    # ── Identity ─────────────────────────────────────────────────────────────
    ExportColumn("item_code",                 "item_code",                                   15, FMT_TEXT),
    ExportColumn("item_name",                 "item_name",                                   36, FMT_TEXT),
    ExportColumn("sap_product_id",            "sap_product_id",                              18, FMT_TEXT),

    # ── Relations (flat names match upload registry: brand_name, not brand) ──
    ExportColumn("brand_name",                lambda r: (r.get("brand")       or {}).get("name"), 18, FMT_TEXT),
    ExportColumn("category_name",             lambda r: (r.get("category")    or {}).get("name"), 18, FMT_TEXT),
    ExportColumn("subcategory_name",          lambda r: (r.get("subcategory") or {}).get("name"), 18, FMT_TEXT),

    # ── Display ───────────────────────────────────────────────────────────────
    ExportColumn("description",               "description",                                 40, FMT_TEXT),
    ExportColumn("image_url",                 "image_url",                                   30, FMT_TEXT),

    # ── AI / Search ──────────────────────────────────────────────────────────
    ExportColumn("skin_type",                 "skin_type",                                   14, FMT_TEXT),
    ExportColumn("concerns",                  lambda r: ", ".join(r.get("concerns") or []), 22, FMT_TEXT),
    ExportColumn("tags",                      lambda r: ", ".join(r.get("tags")     or []), 20, FMT_TEXT),

    # ── Pricing ───────────────────────────────────────────────────────────────
    ExportColumn("price",                     "price",                                       14, FMT_USD),
    ExportColumn("available_qty",             "available_qty",                               14, FMT_INT),

    # ── Classification ────────────────────────────────────────────────────────
    ExportColumn("price_tier",                "price_tier",                                  12, FMT_TEXT),
    ExportColumn("brand_family",              "brand_family",                                18, FMT_TEXT),
    ExportColumn("product_status",            "product_status",                              14, FMT_TEXT),

    # ── Recommendation flags ──────────────────────────────────────────────────
    # "Yes"/"No" are accepted by validate_bool() after .lower() → "yes"/"no"
    ExportColumn("is_best_selling",    lambda r: "Yes" if r.get("is_best_selling")    else "No", 13, FMT_TEXT),
    ExportColumn("is_new_arrival",     lambda r: "Yes" if r.get("is_new_arrival")     else "No", 13, FMT_TEXT),
    ExportColumn("is_recommended",     lambda r: "Yes" if r.get("is_recommended")     else "No", 13, FMT_TEXT),
    ExportColumn("is_cod_recommended", lambda r: "Yes" if r.get("is_cod_recommended") else "No", 14, FMT_TEXT),
    ExportColumn("recommendation_priority",       "recommendation_priority",                 14, FMT_INT),
    ExportColumn("recommendation_score_override", "recommendation_score_override",           16, FMT_PCT),

    # ── Legacy ────────────────────────────────────────────────────────────────
    ExportColumn("best_selling_scope",        "best_selling_scope",                          16, FMT_TEXT),
    ExportColumn("sales_rank",                "sales_rank",                                  12, FMT_INT),

    # ── Export-only / informational (present in file, ignored by upload) ──────
    ExportColumn("price_iqd",                 "price_iqd",                                   16, FMT_IQD,     False),
    ExportColumn("stock_status",              "stock_status",                                 14, FMT_TEXT,    False),
    ExportColumn("last_synced_sap",           "last_synced_sap",                              20, FMT_TEXT,    False),
    ExportColumn("ai_score",                  "ai_score",                                     12, FMT_DECIMAL, False),
]

# ── Derived lists consumed by services/upload.py ──────────────────────────────

REQUIRED_UPLOAD_COLUMNS: list[str] = ["barcode"]

ALL_UPLOAD_COLUMNS: list[str] = [
    col.name
    for col in PRODUCT_IMPORT_EXPORT_COLUMNS
    if col.import_compatible and col.name not in ("#",)
]

# Column names that appear in some older catalog exports — treated as tag aliases
TAG_ALIAS_COLUMNS: list[str] = ["tag_en", "tag_msa", "tag_irq"]   # normalised lowercase

# Deprecated columns — silently ignored but logged as a warning
DEPRECATED_UPLOAD_COLUMNS: frozenset[str] = frozenset({
    "bundle_group", "bundle_discount_percent",
})
