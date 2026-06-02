"""
api/routes/knowledge.py
========================
Knowledge file management endpoints:
  GET    /knowledge               — list uploaded knowledge files
  POST   /knowledge/upload        — upload a PDF or TXT knowledge file
  DELETE /knowledge/{knowledge_id}— remove a knowledge file
"""
from __future__ import annotations

import datetime
import logging
import random
import string

from fastapi import APIRouter, File, HTTPException, UploadFile

from ai.prompt_manager import invalidate_prompt_cache
from services.knowledge_service import (
    ALLOWED_KNOWLEDGE_EXTENSIONS,
    KNOWLEDGE_BASE_DIR,
    MAX_KNOWLEDGE_UPLOAD_MB,
    extract_text_from_upload,
    load_knowledge_index,
    safe_upload_name,
    save_knowledge_index,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Knowledge"])


@router.get("/knowledge")
def list_knowledge_files():
    legacy = (KNOWLEDGE_BASE_DIR.parent / "company_knowledge.txt").exists()
    return {
        "legacy_company_knowledge_exists": legacy,
        "files": load_knowledge_index(),
    }


@router.post("/knowledge/upload")
async def upload_knowledge_file(file: UploadFile = File(...)):
    original_name = file.filename or "knowledge_file"
    suffix        = original_name.rsplit(".", 1)[-1].lower()
    if f".{suffix}" not in ALLOWED_KNOWLEDGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PDF and TXT files are supported.")

    content   = await file.read()
    max_bytes = MAX_KNOWLEDGE_UPLOAD_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File is too large. Max size is {MAX_KNOWLEDGE_UPLOAD_MB} MB.",
        )

    timestamp    = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    random_part  = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    knowledge_id = f"{timestamp}_{random_part}"
    safe_name    = safe_upload_name(original_name)

    stored_filename = f"{knowledge_id}_{safe_name}"
    stored_path     = KNOWLEDGE_BASE_DIR / stored_filename
    stored_path.write_bytes(content)

    extracted_text = extract_text_from_upload(stored_path)
    if not extracted_text:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="No readable text was found in the uploaded file.",
        )

    text_filename = f"{stored_filename}.txt"
    (KNOWLEDGE_BASE_DIR / text_filename).write_text(extracted_text, encoding="utf-8")

    record = {
        "id":                knowledge_id,
        "original_filename": original_name,
        "stored_filename":   stored_filename,
        "text_filename":     text_filename,
        "content_type":      file.content_type,
        "uploaded_at":       datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z",
        "characters":        len(extracted_text),
    }

    items = load_knowledge_index()
    items.append(record)
    save_knowledge_index(items)

    # Knowledge content is injected into the system prompt — invalidate the
    # prompt cache so the very next /reply request includes this new file.
    invalidate_prompt_cache()

    logger.info(
        "knowledge_uploaded id=%s file=%s chars=%d",
        knowledge_id, original_name, len(extracted_text),
    )
    return {"success": True, "file": record}


@router.delete("/knowledge/{knowledge_id}")
def delete_knowledge_file(knowledge_id: str):
    items = load_knowledge_index()
    match = next((item for item in items if item.get("id") == knowledge_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Knowledge file not found.")

    for key in ("stored_filename", "text_filename"):
        filename = match.get(key)
        if filename:
            (KNOWLEDGE_BASE_DIR / filename).unlink(missing_ok=True)

    save_knowledge_index([item for item in items if item.get("id") != knowledge_id])

    # Invalidate prompt cache so deleted knowledge is no longer injected.
    invalidate_prompt_cache()

    logger.info("knowledge_deleted id=%s", knowledge_id)
    return {"success": True, "deleted": knowledge_id}
