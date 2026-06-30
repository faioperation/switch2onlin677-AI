"""
services/upload.py
==================
Bulk CSV / XLSX product upload — async background-job design.

Architecture
------------
  POST /products/upload
    1. validate_upload_file()     → file-level checks (size, MIME, extension)
    2. create_upload_job()        → writes UploadJob row, returns job_id
    3. FastAPI BackgroundTasks    → schedules process_upload_job()
    4. Caller gets job_id immediately; polls GET /products/uploads/{job_id}

  process_upload_job()            (runs in background thread)
    1. parse file → DataFrame
    2. validate columns
    3. Pydantic-validate ALL rows → split into valid/invalid
    4. preload_entity_caches()    → 3 DB round-trips for all brands/cats/subcats
    5. create_missing_entities()  → batch-create any new brands/cats/subcats
    6. for each batch of BATCH_SIZE rows:
         a. determine insert vs update with one IN query
         b. bulk_insert_mappings for new products
         c. bulk_update_mappings for existing products
         d. batch-upsert ProductSearchIndex
         e. commit batch + update job progress
    7. On dry_run=True → rollback full transaction

Performance characteristics (10 k-row file)
---------------------------------------------
  Old: ~6 DB round-trips per row  = 60 000 queries
  New: ~10 DB round-trips total   = 10 queries

Key behaviours preserved from previous implementation
------------------------------------------------------
- barcode is the ONLY required column; all others optional.
- Empty / NaN cells leave existing DB value untouched (no overwrite).
- SAP-owned fields (price, available_qty) may be seeded on first upload
  but will be overwritten by the next SAP sync.
- Recommendation flags are ONLY written when the cell has a value.
- Brand / Category / Subcategory are created automatically if not found,
  names are normalised through NameNormalizer before every DB lookup.

Bundle deprecation
------------------
bundle_group and bundle_discount_percent have been REMOVED from the upload
column registry.  Bundles are managed exclusively through the Bundle API
(/bundles/*).  If these columns appear in an uploaded file they are silently
ignored (no error, no data written).
"""

from __future__ import annotations

import io
import logging
import math
import time
import uuid
from datetime import datetime
from typing import Any

import pandas as pd
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import re

from database import SessionLocal
from models import Brand, Category, Product, ProductSearchIndex, Subcategory, UploadJob
from schemas.export_columns import (
    ALL_UPLOAD_COLUMNS,
    DEPRECATED_UPLOAD_COLUMNS,
    REQUIRED_UPLOAD_COLUMNS,
    TAG_ALIAS_COLUMNS,
)
from schemas.upload import ProductUploadRow, UploadResult, UploadRowError
from services.normalization import get_normalizer

logger = logging.getLogger(__name__)

# ── Upload limits ─────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024  # 100 MB
MAX_UPLOAD_ROWS:  int = 100_000
BATCH_SIZE:       int = 500                 # rows per DB commit cycle

# Accepted MIME types — browsers and OS differ, so we accept a broad set
# and rely on extension + actual content for real validation.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".csv"})
ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/excel",
    "text/csv",
    "text/plain",
    "application/csv",
    "application/octet-stream",   # generic fallback some clients send
    "binary/octet-stream",
})

# ── Column registry (single source of truth: schemas/export_columns.py) ───────
#
# Column lists are imported from schemas/export_columns.py so the export
# generator and the upload validator stay permanently synchronised.
# Do NOT re-declare these lists here — add new columns in export_columns.py.

REQUIRED_PRODUCT_UPLOAD_COLUMNS: list[str] = REQUIRED_UPLOAD_COLUMNS
ALL_PRODUCT_UPLOAD_COLUMNS:      list[str] = ALL_UPLOAD_COLUMNS
TAG_ALIASES:                     list[str] = TAG_ALIAS_COLUMNS
DEPRECATED_COLUMNS:        frozenset[str] = DEPRECATED_UPLOAD_COLUMNS


# ── File-level validation ─────────────────────────────────────────────────────

def validate_upload_file(
    filename: str,
    content: bytes,
    content_type: str | None = None,
) -> None:
    """
    Raise ValueError with a user-facing message if the file fails any
    file-level check.  Call this BEFORE parsing — fast, no DB access.

    Checks
    ------
    1. Non-empty filename + allowed extension
    2. File size ≤ MAX_UPLOAD_BYTES
    3. Non-empty content
    4. MIME type in ALLOWED_MIME_TYPES (permissive — see constant above)
    5. Corrupted file detection (attempt to open with pandas)
    """
    if not filename:
        raise ValueError("Filename is required.")

    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
        )

    if not content:
        raise ValueError("Uploaded file is empty.")

    if len(content) > MAX_UPLOAD_BYTES:
        size_mb = len(content) / (1024 * 1024)
        raise ValueError(
            f"File is too large ({size_mb:.1f} MB). "
            f"Maximum allowed size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    mime = (content_type or "").strip().lower().split(";")[0].strip()
    if mime and mime not in ALLOWED_MIME_TYPES:
        raise ValueError(
            f"Unsupported content type '{mime}'. "
            "Please upload an Excel (.xlsx) or CSV (.csv) file."
        )

    # Try to open the file to catch corrupted/truncated uploads early
    try:
        if ext == ".xlsx":
            pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=str, nrows=1)
        else:
            pd.read_csv(io.BytesIO(content), dtype=str, nrows=1)
    except Exception as exc:
        raise ValueError(
            f"File could not be parsed — it may be corrupted or password-protected. "
            f"Details: {exc}"
        )


# ── Low-level value cleaners ──────────────────────────────────────────────────

def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    return None if s.lower() in {"", "nan", "none", "null", "n/a", "na"} else s


def _clean_number(value: Any) -> float | None:
    s = _clean(value)
    if s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _clean_integer(value: Any) -> int | None:
    s = _clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


# ── Multi-value field parsers ─────────────────────────────────────────────────

def parse_concerns(value: Any) -> list[str]:
    raw = _clean(value)
    if not raw:
        return []
    for sep in ("|", ","):
        if sep in raw:
            return [p.strip() for p in raw.split(sep) if p.strip()]
    return [raw]


def parse_tags(*tag_values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for val in tag_values:
        raw = _clean(val)
        if not raw:
            continue
        for part in raw.split(","):
            part = part.strip()
            if part and part.lower() not in seen:
                seen.add(part.lower())
                result.append(part)
    return result


# ── File reading ──────────────────────────────────────────────────────────────

def read_product_upload_file(filename: str, content: bytes) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=str)
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content), dtype=str)
    raise ValueError("Only .xlsx and .csv files are supported.")


# ── Column normalisation ──────────────────────────────────────────────────────

def _normalize_col_name(name: str) -> str:
    """
    Canonicalise a column name for case- and whitespace-insensitive matching.

    Rules (applied in order):
      1. Strip leading/trailing whitespace
      2. Lowercase
      3. Replace one-or-more whitespace characters with a single underscore

    Examples
    --------
    "Barcode"      → "barcode"
    "Item Code"    → "item_code"
    "BRAND_NAME"   → "brand_name"
    "  barcode  "  → "barcode"
    "item  code"   → "item__code"  (double space → double underscore, still matches)
    """
    return re.sub(r"\s+", "_", name.strip().lower())


# ── Column presence check ─────────────────────────────────────────────────────

def validate_upload_columns(df: pd.DataFrame) -> dict[str, str]:
    """
    Build a mapping of  normalised_column_name → actual_column_name_in_df
    and raise a descriptive ValueError if any required column is absent.

    Normalisation makes the validator accept:
      "barcode", "Barcode", "BARCODE", "Bar Code"  — all map to "barcode"
      "item_name", "Item Name", "ITEM_NAME"         — all map to "item_name"
      "brand_name", "Brand Name", "Brand_Name"      — all map to "brand_name"

    This means exported files (whose headers are exact upload column names)
    AND hand-crafted spreadsheets with Title-Case headers are both accepted.

    Returns
    -------
    dict[str, str]
        Keys are normalised column names; values are the original df column
        names.  Use this dict to access rows via  row[col_map["barcode"]].

    Raises
    ------
    ValueError
        If any column in REQUIRED_PRODUCT_UPLOAD_COLUMNS is absent after
        normalisation.  The error message lists every missing column and
        shows what columns were actually found — makes debugging easy.
    """
    # Build normalised → original mapping
    col_map: dict[str, str] = {}
    for col in df.columns:
        norm = _normalize_col_name(str(col))
        if norm not in col_map:          # first column wins on duplicates
            col_map[norm] = str(col)

    # Required column check with detailed diagnostics
    missing = [c for c in REQUIRED_PRODUCT_UPLOAD_COLUMNS if c not in col_map]
    if missing:
        found_cols = sorted(col_map.keys())
        raise ValueError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"File contains {len(found_cols)} column(s): "
            f"{', '.join(found_cols[:20])}"
            + (" …" if len(found_cols) > 20 else "") + ". "
            "Column names are matched case-insensitively; spaces are treated as "
            "underscores.  Make sure the file has a 'barcode' column."
        )

    # Deprecated column warning (normalised names match)
    found_deprecated = DEPRECATED_COLUMNS & set(col_map.keys())
    if found_deprecated:
        logger.warning(
            "deprecated_columns_in_upload cols=%s hint=%s",
            sorted(found_deprecated),
            "bundle_group / bundle_discount_percent are no longer processed "
            "via upload. Use the /bundles/* API instead.",
        )

    return col_map


# ── Search-text builder ───────────────────────────────────────────────────────

def build_search_text(
    item_code:        str | None,
    item_name:        str | None,
    brand_name:       str | None,
    category_name:    str | None,
    subcategory_name: str | None,
) -> str:
    parts = [s for s in [item_code, brand_name, category_name, subcategory_name, item_name] if s]
    return " ".join(parts).lower()


# ── Row → raw dict helper ─────────────────────────────────────────────────────

def _row_to_raw(row: pd.Series, col_map: dict[str, str]) -> dict[str, Any]:
    def get(logical: str) -> Any:
        actual = col_map.get(logical)
        return row.get(actual) if actual is not None else None

    tag_col_actuals = (
        [col_map[c] for c in TAG_ALIASES if c in col_map]
        + ([col_map["tags"]] if "tags" in col_map else [])
    )
    merged_tags = parse_tags(*[row.get(c) for c in tag_col_actuals])

    return {
        "barcode":                       get("barcode"),
        "item_code":                     get("item_code"),
        "item_name":                     get("item_name"),
        "sap_product_id":                get("sap_product_id"),
        "brand_name":                    get("brand_name"),
        "category_name":                 get("category_name"),
        "subcategory_name":              get("subcategory_name"),
        "description":                   get("description"),
        "image_url":                     get("image_url"),
        "skin_type":                     get("skin_type"),
        "concerns":                      get("concerns"),
        "tags":                          ", ".join(merged_tags) if merged_tags else None,
        "price_tier":                    get("price_tier"),
        "brand_family":                  get("brand_family"),
        "product_status":                get("product_status"),
        "is_best_selling":               get("is_best_selling"),
        "is_new_arrival":                get("is_new_arrival"),
        "is_recommended":                get("is_recommended"),
        "is_cod_recommended":            get("is_cod_recommended"),
        "recommendation_priority":       get("recommendation_priority"),
        "recommendation_score_override": get("recommendation_score_override"),
        # Raw price / qty stored separately — Pydantic schema doesn't hold them
        "_price_raw":                    get("price"),
        "_qty_raw":                      get("available_qty"),
        "_best_selling_scope_raw":       get("best_selling_scope"),
        "_sales_rank_raw":               get("sales_rank"),
    }


# ── Entity cache pre-loading ──────────────────────────────────────────────────

def _preload_entity_caches(db: Session) -> tuple[dict, dict, dict]:
    """
    Load ALL active brands / categories / subcategories into memory at once.
    Returns three dicts keyed by lowercase name (and (name, cat_id) for sub).

    Replaces the old per-row _get_or_create_* calls (which caused N round-trips)
    with 3 total queries regardless of upload size.
    """
    brand_cache: dict[str, int] = {
        b.name.strip().lower(): b.id
        for b in db.query(Brand.id, Brand.name).all()
    }
    category_cache: dict[str, int] = {
        c.name.strip().lower(): c.id
        for c in db.query(Category.id, Category.name).all()
    }
    subcategory_cache: dict[tuple, int] = {
        (s.name.strip().lower(), s.category_id): s.id
        for s in db.query(Subcategory.id, Subcategory.name, Subcategory.category_id).all()
    }
    return brand_cache, category_cache, subcategory_cache


def _ensure_brand(db: Session, name: str | None, cache: dict) -> int | None:
    if not name:
        return None
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    obj = Brand(name=name.strip())
    db.add(obj)
    db.flush()
    cache[key] = obj.id
    logger.info("brand_auto_created", extra={"brand_name": name.strip()})
    return obj.id


def _ensure_category(db: Session, name: str | None, cache: dict) -> int | None:
    if not name:
        return None
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    obj = Category(name=name.strip())
    db.add(obj)
    db.flush()
    cache[key] = obj.id
    logger.info("category_auto_created", extra={"category_name": name.strip()})
    return obj.id


def _ensure_subcategory(
    db: Session,
    name: str | None,
    category_id: int | None,
    cache: dict,
) -> int | None:
    if not name:
        return None
    key = (name.strip().lower(), category_id)
    if key in cache:
        return cache[key]
    obj = Subcategory(name=name.strip(), category_id=category_id)
    db.add(obj)
    db.flush()
    cache[key] = obj.id
    logger.info("subcategory_auto_created", extra={"subcategory_name": name.strip(), "category_id": category_id})
    return obj.id


# ── Job helpers ───────────────────────────────────────────────────────────────

def create_upload_job(filename: str, dry_run: bool) -> str:
    """
    Insert a new UploadJob row and return its UUID job_id.
    Uses its own short-lived DB session so it is independent of the request session.
    """
    job_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(UploadJob(
            id=job_id, filename=filename, status="queued", dry_run=dry_run,
        ))
        db.commit()
    finally:
        db.close()
    return job_id


def _update_job(db: Session, job_id: str, **kwargs: Any) -> None:
    """Partial update on the UploadJob row — call db.commit() after."""
    db.query(UploadJob).filter(UploadJob.id == job_id).update(
        kwargs, synchronize_session=False
    )


def _commit_job_progress(job_id: str, **kwargs: Any) -> None:
    """Write progress to UploadJob using a *separate* short-lived session.

    This lets progress updates be visible to polling clients even while
    the main processing session is mid-transaction.
    """
    db = SessionLocal()
    try:
        db.query(UploadJob).filter(UploadJob.id == job_id).update(
            kwargs, synchronize_session=False
        )
        db.commit()
    except Exception:
        pass   # progress update failure must never kill the upload
    finally:
        db.close()


# ── Background processor ──────────────────────────────────────────────────────

def process_upload_job(job_id: str, filename: str, content: bytes, dry_run: bool) -> None:
    """
    Entry point for FastAPI BackgroundTasks.  Runs the entire upload pipeline
    in a dedicated DB session and updates the UploadJob row throughout.

    Batch strategy
    --------------
    - dry_run=True  : one transaction, rolled back at the very end.
    - dry_run=False : commits every BATCH_SIZE rows so callers can see progress.
                      A failed batch rolls back ONLY that batch; prior batches
                      are committed.  Row-level errors within a batch use
                      per-row savepoints (same as the old implementation).
    """
    start = time.monotonic()
    db    = SessionLocal()

    try:
        _commit_job_progress(job_id, status="processing", started_at=datetime.now())
        logger.info("upload_job_started", extra={"job_id": job_id, "upload_filename": filename})

        # ── 1. Parse file ─────────────────────────────────────────────────────
        try:
            df = read_product_upload_file(filename, content)
        except Exception as exc:
            _fail_job(job_id, f"File parse error: {exc}", start)
            return

        # ── 2. Validate columns ───────────────────────────────────────────────
        try:
            col_map = validate_upload_columns(df)
        except ValueError as exc:
            _fail_job(job_id, str(exc), start)
            return

        total_rows = len(df)
        if total_rows > MAX_UPLOAD_ROWS:
            _fail_job(
                job_id,
                f"File has {total_rows} rows which exceeds the limit of {MAX_UPLOAD_ROWS}. "
                "Split the file into smaller batches.",
                start,
            )
            return

        _commit_job_progress(job_id, total_rows=total_rows)

        # ── 3. Pydantic-validate ALL rows first ───────────────────────────────
        valid_rows:   list[tuple[int, ProductUploadRow, dict]] = []   # (1-based row, data, raw)
        row_errors:   list[UploadRowError] = []

        normalizer = get_normalizer()

        for index, row in df.iterrows():
            row_number = int(index) + 2
            raw = _row_to_raw(row, col_map)

            # Rows without a barcode (blank/header rows in Excel) are silently skipped.
            if not _clean(raw.get("barcode")):
                continue

            # Normalise entity names — guard against NaN / unexpected types
            try:
                raw["brand_name"]       = normalizer.resolve_brand(raw.get("brand_name"))
                raw["category_name"]    = normalizer.resolve_category(raw.get("category_name"))
                raw["subcategory_name"] = normalizer.resolve_subcategory(raw.get("subcategory_name"))
            except Exception as exc:
                logger.warning(
                    "normalization_error_row_skipped",
                    extra={"row": row_number, "error": str(exc)},
                )
                continue

            try:
                data = ProductUploadRow(**raw)
                valid_rows.append((row_number, data, raw))
            except ValidationError as exc:
                row_errors.append(UploadRowError(
                    row=row_number,
                    barcode=_clean(raw.get("barcode")),
                    error="; ".join(
                        f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                        for e in exc.errors()
                    ),
                ))

        # ── 4. Preload entity caches (3 DB queries total) ─────────────────────
        brand_cache, category_cache, subcategory_cache = _preload_entity_caches(db)

        # ── 5. Process in batches ─────────────────────────────────────────────
        created_count = 0
        updated_count = 0
        skipped_count = len(df) - len(valid_rows)   # rows that failed Pydantic
        processed_so_far = skipped_count

        for batch_start in range(0, len(valid_rows), BATCH_SIZE):
            batch = valid_rows[batch_start : batch_start + BATCH_SIZE]

            if dry_run:
                savepoint = db.begin_nested()

            b_created, b_updated, b_skipped, b_errors = _process_batch(
                db, batch, brand_cache, category_cache, subcategory_cache
            )

            if dry_run:
                savepoint.rollback()
            else:
                try:
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.error("batch_commit_failed", extra={"job_id": job_id, "error": str(exc)})
                    # Treat entire batch as skipped
                    b_skipped += b_created + b_updated
                    b_created = b_updated = 0
                    b_errors.append(UploadRowError(
                        row=batch_start + 2,
                        barcode=None,
                        error=f"Batch commit failed: {exc}",
                    ))

            created_count  += b_created
            updated_count  += b_updated
            skipped_count  += b_skipped
            row_errors     += b_errors
            processed_so_far += len(batch)

            # Update progress after each batch (visible to GET /uploads/{job_id})
            _commit_job_progress(
                job_id,
                processed_rows=processed_so_far,
                created_count=created_count,
                updated_count=updated_count,
                skipped_count=skipped_count,
                error_count=len(row_errors),
            )

        duration = round(time.monotonic() - start, 2)
        error_details = [e.model_dump() for e in row_errors[:100]]

        _commit_job_progress(
            job_id,
            status="completed",
            created_count=created_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            error_count=len(row_errors),
            error_details=error_details,
            processed_rows=total_rows,
            completed_at=datetime.now(),
            execution_seconds=duration,
        )

        logger.info(
            "upload_job_completed",
            extra={
                "job_id":        job_id,
                "created_count": created_count,
                "updated_count": updated_count,
                "skipped_count": skipped_count,
                "error_count":   len(row_errors),
                "duration":      duration,
                "dry_run":       dry_run,
            },
        )

    except Exception as exc:
        db.rollback()
        logger.error("upload_job_failed", extra={"job_id": job_id, "error": str(exc)}, exc_info=True)
        _fail_job(job_id, str(exc), start)

    finally:
        db.close()


def _fail_job(job_id: str, reason: str, start: float | None = None) -> None:
    duration = round(time.monotonic() - start, 2) if start is not None else None
    _commit_job_progress(
        job_id,
        status="failed",
        error_message=reason,
        completed_at=datetime.now(),
        execution_seconds=duration,
    )
    logger.error("upload_job_failed", extra={"job_id": job_id, "reason": reason})


# ── Batch processor ───────────────────────────────────────────────────────────

def _process_batch(
    db: Session,
    batch: list[tuple[int, ProductUploadRow, dict]],
    brand_cache:       dict,
    category_cache:    dict,
    subcategory_cache: dict,
) -> tuple[int, int, int, list[UploadRowError]]:
    """
    Process one batch of validated rows.
    Returns (created, updated, skipped, errors).

    Strategy
    --------
    1. Resolve all entity IDs for the batch (cache-first, create if missing).
    2. Identify which barcodes already exist with one IN query.
    3. Apply field values per-row inside a per-row savepoint for isolation.
    4. Batch-upsert ProductSearchIndex after all products are written.
    """
    created = updated = skipped = 0
    errors: list[UploadRowError] = []

    # ── Resolve entities for all rows in batch (cache-first) ──────────────────
    row_entities: list[tuple[int | None, int | None, int | None]] = []
    for _, data, _ in batch:
        brand_id    = _ensure_brand(db, data.brand_name, brand_cache)
        category_id = _ensure_category(db, data.category_name, category_cache)
        sub_id      = _ensure_subcategory(db, data.subcategory_name, category_id, subcategory_cache)
        row_entities.append((brand_id, category_id, sub_id))

    # ── One IN query to find existing barcodes ─────────────────────────────────
    barcodes_in_batch = [data.barcode for _, data, _ in batch]
    existing_barcodes: set[str] = {
        row[0]
        for row in db.query(Product.barcode)
        .filter(Product.barcode.in_(barcodes_in_batch))
        .all()
    }

    # ── Per-row upsert with savepoint isolation ────────────────────────────────
    search_index_updates: list[dict] = []

    for (row_number, data, raw), (brand_id, category_id, sub_id) in zip(batch, row_entities):
        sp = db.begin_nested()
        row_action: str | None = None

        try:
            if data.barcode in existing_barcodes:
                product = db.query(Product).filter(Product.barcode == data.barcode).first()
                if product is None:
                    raise RuntimeError(f"Race condition: barcode {data.barcode} vanished")
                row_action = "updated"
            else:
                product = Product(barcode=data.barcode)
                db.add(product)
                row_action = "created"

            _apply_fields(product, data, raw, brand_id, category_id, sub_id)

            search_index_updates.append({
                "barcode":         data.barcode,
                "item_code":       data.item_code,
                "item_name":       data.item_name,
                "brand_name":      data.brand_name,
                "category_name":   data.category_name,
                "subcategory_name":data.subcategory_name,
            })

            sp.commit()
            if row_action == "created":
                created += 1
                existing_barcodes.add(data.barcode)
            else:
                updated += 1

        except Exception as exc:
            sp.rollback()
            skipped += 1
            errors.append(UploadRowError(
                row=row_number, barcode=data.barcode, error=str(exc)
            ))

    # ── Batch upsert ProductSearchIndex ────────────────────────────────────────
    if search_index_updates:
        _batch_upsert_search_index(db, search_index_updates)

    return created, updated, skipped, errors


def _apply_fields(
    product:      Product,
    data:         ProductUploadRow,
    raw:          dict,
    brand_id:     int | None,
    category_id:  int | None,
    sub_id:       int | None,
) -> None:
    """Apply all upload fields onto the Product ORM object."""
    # Identity / display
    if data.item_code      is not None: product.item_code      = data.item_code
    if data.item_name      is not None: product.item_name      = data.item_name
    if data.sap_product_id is not None: product.sap_product_id = data.sap_product_id
    if data.description    is not None: product.description    = data.description
    if data.image_url      is not None: product.image_url      = data.image_url

    # Relations
    if brand_id    is not None: product.brand_id       = brand_id
    if category_id is not None: product.category_id    = category_id
    if sub_id      is not None: product.subcategory_id = sub_id

    # AI / Search
    if data.skin_type is not None: product.skin_type = data.skin_type
    concerns_parsed = parse_concerns(data.concerns)
    if concerns_parsed:  product.concerns = concerns_parsed
    tags_parsed = parse_tags(data.tags) if data.tags else []
    if tags_parsed:      product.tags     = tags_parsed

    # Pricing (SAP owns, but allow initial seed via upload)
    price_val = _clean_number(raw.get("_price_raw"))
    qty_val   = _clean_integer(raw.get("_qty_raw"))
    if price_val is not None: product.price         = price_val
    if qty_val   is not None: product.available_qty = qty_val

    # Classification
    if data.price_tier is not None:
        # Normalize to DB enum values regardless of how Pydantic stored the member.
        # str(PriceTierEnum.mid) can return 'mid' (name) or 'Mid' (value) depending on
        # Python/Pydantic version, so we normalize via lookup table.
        _TIER = {"budget": "Budget", "mid": "Mid", "premium": "Premium", "luxury": "Luxury"}
        raw_tier = getattr(data.price_tier, "value", str(data.price_tier))
        product.price_tier = _TIER.get(str(raw_tier).lower())
    if data.brand_family   is not None: product.brand_family   = data.brand_family
    if data.product_status is not None:
        _STATUS = {"active": "active", "inactive": "inactive", "draft": "draft"}
        raw_status = getattr(data.product_status, "value", str(data.product_status))
        product.product_status = _STATUS.get(str(raw_status).lower(), "active")

    # Recommendation flags — only write when cell had a value
    if data.is_best_selling               is not None:
        product.is_best_selling               = data.is_best_selling
    if data.is_new_arrival                is not None:
        product.is_new_arrival                = data.is_new_arrival
    if data.is_recommended                is not None:
        product.is_recommended                = data.is_recommended
    if data.is_cod_recommended            is not None:
        product.is_cod_recommended            = data.is_cod_recommended
    if data.recommendation_priority       is not None:
        product.recommendation_priority       = data.recommendation_priority
    if data.recommendation_score_override is not None:
        product.recommendation_score_override = data.recommendation_score_override

    # Legacy
    scope = _clean(raw.get("_best_selling_scope_raw"))
    rank  = _clean_integer(raw.get("_sales_rank_raw"))
    if scope is not None: product.best_selling_scope = scope
    if rank  is not None: product.sales_rank         = rank


def _batch_upsert_search_index(db: Session, updates: list[dict]) -> None:
    """
    Upsert ProductSearchIndex using PostgreSQL INSERT ... ON CONFLICT DO UPDATE.

    Atomic: no prior SELECT needed, no race condition, handles duplicates
    within the same batch (last row wins per barcode).
    """
    if not updates:
        return

    # Deduplicate by barcode — last writer wins within a batch
    deduped: dict[str, dict] = {}
    for u in updates:
        deduped[u["barcode"]] = u

    rows = [
        {
            "product_id":       barcode,
            "item_code":        u.get("item_code"),
            "barcode":          barcode,
            "item_name":        u.get("item_name"),
            "brand_name":       u.get("brand_name"),
            "category_name":    u.get("category_name"),
            "subcategory_name": u.get("subcategory_name"),
            "search_text":      build_search_text(
                u.get("item_code"),
                u.get("item_name"),
                u.get("brand_name"),
                u.get("category_name"),
                u.get("subcategory_name"),
            ),
            "updated_at": None,
        }
        for barcode, u in deduped.items()
    ]

    stmt = pg_insert(ProductSearchIndex).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["product_id"],
        set_={
            "item_code":        stmt.excluded.item_code,
            "barcode":          stmt.excluded.barcode,
            "item_name":        stmt.excluded.item_name,
            "brand_name":       stmt.excluded.brand_name,
            "category_name":    stmt.excluded.category_name,
            "subcategory_name": stmt.excluded.subcategory_name,
            "search_text":      stmt.excluded.search_text,
            "updated_at":       func.now(),
        },
    )
    db.execute(stmt)


# ── Synchronous upsert (kept for backward compat / small files / dry-run) ─────

def upsert_product_upload(
    db: Session,
    filename: str,
    content: bytes,
    dry_run: bool = False,
) -> UploadResult:
    """
    Synchronous entry-point — used by the legacy inline upload endpoint and tests.

    For files larger than a few hundred rows, prefer the async path via
    process_upload_job() which is scheduled through FastAPI BackgroundTasks.
    """
    df      = read_product_upload_file(filename, content)
    col_map = validate_upload_columns(df)

    normalizer = get_normalizer()

    valid_rows:  list[tuple[int, ProductUploadRow, dict]] = []
    row_errors:  list[UploadRowError] = []

    for index, row in df.iterrows():
        row_number = int(index) + 2
        raw = _row_to_raw(row, col_map)

        if not _clean(raw.get("barcode")):
            continue

        try:
            raw["brand_name"]       = normalizer.resolve_brand(raw.get("brand_name"))
            raw["category_name"]    = normalizer.resolve_category(raw.get("category_name"))
            raw["subcategory_name"] = normalizer.resolve_subcategory(raw.get("subcategory_name"))
        except Exception as exc:
            continue

        try:
            data = ProductUploadRow(**raw)
            valid_rows.append((row_number, data, raw))
        except ValidationError as exc:
            row_errors.append(UploadRowError(
                row=row_number,
                barcode=_clean(raw.get("barcode")),
                error="; ".join(
                    f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                    for e in exc.errors()
                ),
            ))

    brand_cache, category_cache, subcategory_cache = _preload_entity_caches(db)

    created = updated = 0
    skipped = len(df) - len(valid_rows)

    for batch_start in range(0, len(valid_rows), BATCH_SIZE):
        batch = valid_rows[batch_start : batch_start + BATCH_SIZE]
        sp    = db.begin_nested()
        b_created, b_updated, b_skipped, b_errors = _process_batch(
            db, batch, brand_cache, category_cache, subcategory_cache
        )
        sp.commit()
        created  += b_created
        updated  += b_updated
        skipped  += b_skipped
        row_errors += b_errors

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return UploadResult(
        filename=filename,
        total_rows=len(df),
        created=created,
        updated=updated,
        skipped=skipped,
        dry_run=dry_run,
        errors=row_errors[:100],
    )
