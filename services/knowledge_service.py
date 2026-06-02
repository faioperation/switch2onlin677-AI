"""
services/knowledge_service.py
==============================
Knowledge file management: index CRUD, text extraction, and file utilities.
Extracted from main.py to give the knowledge router a clean service layer.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import HTTPException
from pypdf import PdfReader

from core.config import settings

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_DIR         = settings.KNOWLEDGE_BASE_DIR
KNOWLEDGE_INDEX_FILE       = settings.KNOWLEDGE_INDEX_FILE
ALLOWED_KNOWLEDGE_EXTENSIONS = settings.ALLOWED_KNOWLEDGE_EXTENSIONS
MAX_KNOWLEDGE_UPLOAD_MB    = settings.MAX_KNOWLEDGE_UPLOAD_MB


# ── Index helpers ──────────────────────────────────────────────────────────────

def load_knowledge_index() -> list[dict]:
    """Load the list of uploaded knowledge files from the JSON index."""
    if not KNOWLEDGE_INDEX_FILE.exists():
        return []
    try:
        return json.loads(KNOWLEDGE_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("knowledge_index_corrupt — returning empty list")
        return []


def save_knowledge_index(items: list[dict]) -> None:
    KNOWLEDGE_INDEX_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Text extraction ────────────────────────────────────────────────────────────

def extract_text_from_pdf(file_path: Path) -> str:
    reader = PdfReader(str(file_path))
    pages: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"\n--- Page {page_number} ---\n{text.strip()}")
    return "\n".join(pages).strip()


def extract_text_from_upload(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(file_path)
    if suffix == ".txt":
        return file_path.read_text(encoding="utf-8", errors="ignore").strip()
    raise HTTPException(
        status_code=400,
        detail="Only PDF and TXT knowledge files are supported.",
    )


# ── Filename sanitizer ─────────────────────────────────────────────────────────

def safe_upload_name(filename: str) -> str:
    """Strip unsafe characters from an upload filename."""
    safe = "".join(
        c if c.isalnum() or c in {"-", "_", "."} else "_"
        for c in filename
    )
    return safe or "knowledge_file"


# ── Rate file helpers ──────────────────────────────────────────────────────────

def load_iqd_rate() -> float:
    """Read the current IQD rate (uses the cached value from ai/tools/formatters)."""
    from ai.tools.formatters import get_iqd_rate
    return get_iqd_rate()


def save_iqd_rate(rate: float) -> None:
    """
    Write a new IQD rate and immediately update the in-process cache.
    Uses atomic file write (temp + rename) to prevent partial reads.
    """
    from ai.tools.formatters import update_iqd_rate
    update_iqd_rate(rate)


# ── Leads helper ───────────────────────────────────────────────────────────────

def load_leads() -> list:
    leads_file = settings.LEADS_FILE
    if not leads_file.exists():
        return []
    try:
        return json.loads(leads_file.read_text(encoding="utf-8"))
    except Exception:
        return []
