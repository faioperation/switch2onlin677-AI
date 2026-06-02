"""
routers/uploads.py
==================
Product upload management API.

  POST /products/upload
      Accept Excel or CSV file.
      Validate immediately (size, MIME, extension, row count, columns).
      Create an UploadJob record (status=queued).
      Schedule process_upload_job() as a FastAPI BackgroundTask.
      Return job_id immediately — caller polls the status endpoint.

  GET  /products/uploads/{job_id}
      Return full detail of one UploadJob (progress, counts, errors).

  GET  /products/uploads
      Paginated list of past upload jobs (newest first).
      Useful for the operations dashboard / audit trail.

  GET  /products/upload-template
      Return the column registry so the frontend can generate a template.

Design notes
------------
- File content is read into memory before scheduling the background task.
  For files up to MAX_UPLOAD_BYTES (10 MB) this is safe on a modern server.
- The background task opens its own DB session (process_upload_job does this).
  The request session is NOT shared across thread boundaries.
- Progress is persisted to the DB by the background task after each batch,
  so clients polling GET /uploads/{job_id} see live row counts.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from sqlalchemy.orm import Session

from core.exceptions import NotFoundError, ServiceError
from database import get_db
from models import UploadJob
from services.upload import (
    ALL_PRODUCT_UPLOAD_COLUMNS,
    REQUIRED_PRODUCT_UPLOAD_COLUMNS,
    create_upload_job,
    process_upload_job,
    validate_upload_file,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["Product Uploads"])


# ── POST /products/upload ─────────────────────────────────────────────────────

@router.post("/products/upload")
async def upload_products(
    background_tasks: BackgroundTasks,
    file:    UploadFile = File(...),
    dry_run: bool       = False,
):
    """
    Async product upload.

    Response (202 Accepted)
    -----------------------
    {
      "success": true,
      "job_id":  "d4f3a1b2-...",
      "message": "Upload queued. Poll GET /products/uploads/{job_id} for progress.",
      "dry_run": false
    }

    The job transitions through: queued → processing → completed | failed.
    Poll the status endpoint until status is 'completed' or 'failed'.
    """
    filename     = file.filename or ""
    content_type = file.content_type or ""
    content      = await file.read()

    # File-level validation — fast, no DB
    try:
        validate_upload_file(filename, content, content_type)
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc))

    # Create the job record (returns immediately)
    try:
        job_id = create_upload_job(filename=filename, dry_run=dry_run)
    except Exception as exc:
        logger.error("upload_job_create_failed", extra={"error": str(exc)}, exc_info=True)
        raise ServiceError("Failed to create upload job. Please try again.")

    # Schedule background processing
    background_tasks.add_task(
        process_upload_job,
        job_id   = job_id,
        filename = filename,
        content  = content,
        dry_run  = dry_run,
    )

    logger.info(
        "upload_job_queued",
        extra={"job_id": job_id, "upload_filename": filename, "dry_run": dry_run},
    )

    return {
        "success": True,
        "job_id":  job_id,
        "message": (
            "Dry-run upload queued. Poll GET /products/uploads/{job_id} for progress."
            if dry_run
            else "Upload queued. Poll GET /products/uploads/{job_id} for progress."
        ),
        "dry_run": dry_run,
    }


# ── GET /products/upload-template ────────────────────────────────────────────

@router.get("/products/upload-template")
def get_upload_template():
    """
    Return the column schema so the frontend can generate a download template.
    """
    return {
        "success": True,
        "data": {
            "required_columns":     REQUIRED_PRODUCT_UPLOAD_COLUMNS,
            "all_supported_columns":ALL_PRODUCT_UPLOAD_COLUMNS,
            "accepted_file_types":  [".xlsx", ".csv"],
            "limits": {
                "max_file_size_mb":  100,
                "max_rows":          100_000,
            },
            "notes": [
                "barcode is the only required column.",
                "First sheet will be used for Excel files.",
                "concerns and tags should be pipe- or comma-separated.",
                "Existing products are updated when barcode matches.",
                "Booleans accept: 1/0, true/false, yes/no (case-insensitive).",
                "bundle_group and bundle_discount_percent are no longer supported "
                "— use the /bundles/* API to manage bundles.",
            ],
        },
    }


# ── GET /products/uploads/{job_id} ───────────────────────────────────────────

@router.get("/products/uploads/{job_id}")
def get_upload_job(job_id: str, db: Session = Depends(get_db)):
    """
    Return the current state of an upload job.

    Response shape
    --------------
    {
      "success": true,
      "data": {
        "job_id":           "d4f3a1b2-...",
        "filename":         "products.xlsx",
        "status":           "processing",     // queued|processing|completed|failed
        "dry_run":          false,
        "total_rows":       5000,
        "processed_rows":   1500,             // updated after each 500-row batch
        "progress_pct":     30,
        "created_count":    800,
        "updated_count":    650,
        "skipped_count":    50,
        "error_count":      5,
        "error_details":    [...],            // up to 100 row-level errors
        "error_message":    null,             // top-level failure reason
        "started_at":       "2026-05-30T...",
        "completed_at":     null,
        "execution_seconds":null,
        "created_at":       "2026-05-30T..."
      }
    }
    """
    job: UploadJob | None = db.query(UploadJob).filter(UploadJob.id == job_id).first()
    if not job:
        raise NotFoundError(f"Upload job '{job_id}' not found.")

    return {"success": True, "data": _serialize_job(job)}


# ── GET /products/uploads ─────────────────────────────────────────────────────

@router.get("/products/uploads")
def list_upload_jobs(
    page:     int           = 1,
    limit:    int           = 20,
    status:   Optional[str] = None,
    db:       Session       = Depends(get_db),
):
    """
    Paginated upload history (newest first).

    Query parameters
    ----------------
    page    : page number (default 1)
    limit   : items per page (1–100, default 20)
    status  : filter by status (queued|processing|completed|failed)
    """
    limit  = max(1, min(limit, 100))
    page   = max(1, page)
    offset = (page - 1) * limit

    query = db.query(UploadJob)
    if status:
        query = query.filter(UploadJob.status == status)

    total = query.count()
    jobs  = query.order_by(UploadJob.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "success": True,
        "data": {
            "jobs": [_serialize_job(j) for j in jobs],
            "pagination": {
                "total":       total,
                "page":        page,
                "limit":       limit,
                "total_pages": (total + limit - 1) // limit if limit else 0,
                "has_next":    page * limit < total,
                "has_prev":    page > 1,
            },
        },
    }


# ── Serializer ────────────────────────────────────────────────────────────────

def _serialize_job(job: UploadJob) -> dict:
    total     = job.total_rows or 0
    processed = job.processed_rows or 0
    pct       = round((processed / total) * 100) if total > 0 else 0

    return {
        "job_id":            job.id,
        "filename":          job.filename,
        "status":            job.status,
        "dry_run":           job.dry_run,
        "total_rows":        total,
        "processed_rows":    processed,
        "progress_pct":      pct,
        "created_count":     job.created_count or 0,
        "updated_count":     job.updated_count or 0,
        "skipped_count":     job.skipped_count or 0,
        "error_count":       job.error_count or 0,
        "error_details":     job.error_details or [],
        "error_message":     job.error_message,
        "started_at":        job.started_at.isoformat()   if job.started_at   else None,
        "completed_at":      job.completed_at.isoformat() if job.completed_at else None,
        "execution_seconds": float(job.execution_seconds) if job.execution_seconds else None,
        "created_at":        job.created_at.isoformat()   if job.created_at   else None,
    }
