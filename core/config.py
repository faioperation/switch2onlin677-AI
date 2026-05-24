"""
core/config.py
==============
Single source of truth for all environment variables and path constants.

Usage
-----
    from core.config import settings

    url = settings.SAP_API_URL
    key = settings.OPENAI_API_KEY

All values are read once at import time after load_dotenv() has been called
in main.py.  No module should call os.getenv() directly — import from here.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Project root ──────────────────────────────────────────────────────────────
# core/config.py lives at  <root>/core/config.py  →  parent.parent = root
BASE_DIR: Path = Path(__file__).resolve().parent.parent


class _Settings:
    """
    Lazy-read settings container.
    All attributes are properties so they re-read from os.environ on first access
    (load_dotenv() must have been called before accessing them).
    """

    # ── External API keys / URLs ──────────────────────────────────────────────

    @property
    def OPENAI_API_KEY(self) -> str | None:
        return os.getenv("OPENAI_API_KEY")

    @property
    def SAP_API_URL(self) -> str | None:
        return os.getenv("SAP_API_URL")

    @property
    def LEADS_API_URL(self) -> str | None:
        return os.getenv("LEADS_API_URL")

    @property
    def DATABASE_URL(self) -> str | None:
        return os.getenv("DATABASE_URL")

    # ── File paths ────────────────────────────────────────────────────────────

    @property
    def RATE_FILE(self) -> Path:
        return BASE_DIR / "rate.json"

    @property
    def SYSTEM_PROMPT_FILE(self) -> Path:
        return BASE_DIR / "system_prompt.txt"

    @property
    def KNOWLEDGE_BASE_DIR(self) -> Path:
        path = BASE_DIR / "knowledge_base"
        path.mkdir(exist_ok=True)
        return path

    @property
    def KNOWLEDGE_INDEX_FILE(self) -> Path:
        return self.KNOWLEDGE_BASE_DIR / "index.json"

    @property
    def LEADS_FILE(self) -> Path:
        return BASE_DIR / "leads.json"

    # ── App behaviour constants ───────────────────────────────────────────────

    MAX_CHAT_HISTORY: int = 70
    LOW_STOCK_THRESHOLD: int = 5
    MAX_KNOWLEDGE_UPLOAD_MB: int = 20
    IRAQ_TIMEZONE: ZoneInfo = ZoneInfo("Asia/Baghdad")

    # ── Knowledge file types ──────────────────────────────────────────────────

    ALLOWED_KNOWLEDGE_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".txt"})


# Singleton — import this everywhere
settings = _Settings()
