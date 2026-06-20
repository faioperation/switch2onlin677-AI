"""
ai/prompt_manager.py
====================
System prompt loading, caching, and knowledge injection.

Cache strategy (mtime-based invalidation)
------------------------------------------
The full system prompt (base + company knowledge) is expensive to build on
every request: it involves multiple disk reads (system_prompt.txt +
knowledge_base/index.json + N knowledge files).

Instead we cache the result in memory and invalidate ONLY when the underlying
files change — detected via OS file modification timestamps (mtime). This gives
us:
  - Zero stale content: any file change immediately invalidates the cache.
  - One disk stat per request (cheap) instead of N full reads.
  - Thread-safe via threading.Lock (FastAPI sync endpoints run in a thread pool).

Atomic writes
-------------
`write_system_prompt()` writes to a temp file then renames it. On POSIX this
is a single atomic syscall; on Windows it is best-effort. This prevents a
concurrent request from reading a partially written file.

Explicit invalidation
---------------------
Call `invalidate_prompt_cache()` after any operation that changes the inputs:
  - Uploading or deleting a knowledge file.
  - The cache will also self-invalidate on the next request via mtime checks.

RAG upgrade path
----------------
Replace `load_company_knowledge()` with a call to
`ai.rag_service.retrieve_relevant_chunks(db, query)` to inject only the most
relevant passages instead of stuffing the full knowledge base.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _atomic_replace(src: Path, dst: Path) -> None:
    """
    Rename src → dst atomically.

    On POSIX: os.replace() is a single atomic syscall — the retry path is
    never reached.

    On Windows: os.replace() raises PermissionError when the destination is
    momentarily held open by a concurrent reader (AV scanner, other thread).
    We retry up to 5 times with exponential backoff (1 ms → 16 ms, total ≤ 31 ms).
    Concurrent readers release the handle within microseconds, so retries
    almost always succeed on the first or second attempt.
    """
    delay = 0.001           # initial delay: 1 ms
    last_exc: Exception
    for _ in range(5):
        try:
            src.replace(dst)
            return
        except PermissionError as exc:
            last_exc = exc
            _time.sleep(delay)
            delay *= 2      # 1 ms → 2 ms → 4 ms → 8 ms → 16 ms
    raise last_exc          # all 5 attempts exhausted — propagate

logger = logging.getLogger(__name__)

# ── File paths ─────────────────────────────────────────────────────────────────

_BASE_DIR             = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_FILE    = _BASE_DIR / "system_prompt.txt"
KNOWLEDGE_BASE_DIR    = _BASE_DIR / "knowledge_base"
_KNOWLEDGE_INDEX_FILE = KNOWLEDGE_BASE_DIR / "index.json"

# ── Fixed branded strings (interpolated into system_prompt.txt) ────────────────

FIXED_WELCOME_EN = (
    "✨ Welcome to DhifafBot, your personal premium concierge. "
    "I'm here to help you discover the finest beauty, cosmetics, and personal care products from our catalog. "
    "How may I elevate your shopping experience today? 🛍️"
)
FIXED_WELCOME_AR = (
    "✨ أهلاً بك في ضفاف بوت، مساعدك الشخصي للتسوق. "
    "أنا هنا لمساعدتك في اكتشاف أفضل منتجات التجميل والعناية الشخصية من تشكيلتنا المميزة. "
    "كيف يمكنني مساعدتك اليوم؟ 🛍️"
)
FIXED_GOODBYE_EN = (
    "🌟 Thank you for visiting DhifafBot. It has been a pleasure assisting you. "
    "If you need further recommendations or help with your orders, I'm always here to help. "
    "Have a beautiful day! ✨"
)
FIXED_GOODBYE_AR = (
    "🌟 شكراً لزيارتك ضفاف بوت. سعدت بمساعدتك اليوم. "
    "إذا كنت بحاجة إلى أي توصيات أخرى أو مساعدة في طلباتك لاحقاً، فأنا متواجد دائماً لخدمتك. "
    "أتمنى لك يوماً جميلاً! ✨"
)


# ── Cache implementation ───────────────────────────────────────────────────────

@dataclass
class _PromptCacheState:
    """Holds cached full system prompt with the mtimes that produced it."""
    content:           str           = ""
    prompt_mtime:      Optional[float] = None
    knowledge_mtime:   Optional[float] = None
    # Incremented on every successful cache miss (build).  Used for log tracing.
    version:           int           = 0


_cache  = _PromptCacheState()
_lock   = threading.Lock()


def _get_mtime(path: Path) -> Optional[float]:
    """Return file mtime or None if file does not exist."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _cache_is_valid() -> bool:
    """
    Return True when the cached prompt matches the current state of all
    source files.  Cheap: only OS stat calls, no file reads.
    """
    prompt_mtime    = _get_mtime(SYSTEM_PROMPT_FILE)
    knowledge_mtime = _get_mtime(_KNOWLEDGE_INDEX_FILE)
    return (
        _cache.content
        and _cache.prompt_mtime    == prompt_mtime
        and _cache.knowledge_mtime == knowledge_mtime
    )


def invalidate_prompt_cache() -> None:
    """
    Force the next call to build_full_system_prompt() to rebuild from disk.
    Call after any operation that changes system_prompt.txt or knowledge files.
    """
    with _lock:
        _cache.content         = ""
        _cache.prompt_mtime    = None
        _cache.knowledge_mtime = None
    logger.info("prompt_cache_invalidated")


# ── Template rendering ─────────────────────────────────────────────────────────

def render_prompt_template(template: str) -> str:
    """Substitute branded placeholder variables into a prompt template string."""
    return template.format(
        FIXED_WELCOME_EN=FIXED_WELCOME_EN,
        FIXED_WELCOME_AR=FIXED_WELCOME_AR,
        FIXED_GOODBYE_EN=FIXED_GOODBYE_EN,
        FIXED_GOODBYE_AR=FIXED_GOODBYE_AR,
    )


# ── Disk access (uncached, used only on cache miss) ────────────────────────────

def load_system_prompt() -> str:
    """Read and render system_prompt.txt directly from disk (no cache)."""
    if not SYSTEM_PROMPT_FILE.exists():
        raise FileNotFoundError("system_prompt.txt not found.")
    return render_prompt_template(SYSTEM_PROMPT_FILE.read_text(encoding="utf-8"))


def _load_knowledge_index() -> list[dict]:
    if not _KNOWLEDGE_INDEX_FILE.exists():
        return []
    try:
        return json.loads(_KNOWLEDGE_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("knowledge_index_parse_error — using empty knowledge")
        return []


def load_company_knowledge() -> str:
    """
    Concatenate all uploaded knowledge files into one string (no cache).

    RAG upgrade path: replace this with ai.rag_service.retrieve_relevant_chunks()
    to inject only the semantically relevant passages instead of all files.
    """
    parts: list[str] = []
    for item in _load_knowledge_index():
        text_path = KNOWLEDGE_BASE_DIR / item.get("text_filename", "")
        if text_path.exists():
            text = text_path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                parts.append(
                    f"SOURCE: {item.get('original_filename', text_path.name)}\n{text}"
                )
    return "\n\n".join(parts)


def _build_full_prompt_uncached() -> str:
    """Build the full prompt from disk. Called only on cache miss."""
    base_prompt = load_system_prompt()
    knowledge   = load_company_knowledge()

    if not knowledge:
        return base_prompt

    return base_prompt + f"""

    COMPANY KNOWLEDGE:
    Use this information when users ask about Dhifaf Baghdad, DBC, company profile, branches, brands, offices, app, partners, or company background.

    {knowledge}

    Rules:
    - Answer company questions using this company knowledge.
    - If the user asks in Arabic, answer in Arabic.
    - If the user asks in English, answer in English.
    - Do not invent company facts not listed here.
    """


# ── Public API ─────────────────────────────────────────────────────────────────

def build_base_system_prompt() -> str:
    """
    Return only the base system prompt (personality + instructions) WITHOUT
    company knowledge injected.

    Used by the optimized RAG-aware orchestrator, which retrieves only the
    relevant knowledge chunks per query via ai.rag_service instead of
    injecting the entire knowledge base.

    This function has its own cache keyed on system_prompt.txt mtime only.
    """
    prompt_mtime = _get_mtime(SYSTEM_PROMPT_FILE)
    with _lock:
        # Re-use the full-prompt cache if it was built with the same base file
        if (
            _cache.content
            and _cache.prompt_mtime == prompt_mtime
        ):
            # Full cache is valid — extract just the base portion.
            # Since _build_full_prompt_uncached() = base + "\n\n    COMPANY KNOWLEDGE: ..."
            # we split on the section header to return the base only.
            content = _cache.content
            sep = "\n\n    COMPANY KNOWLEDGE:"
            return content.split(sep)[0] if sep in content else content

    # Cache miss or stale — read base from disk directly
    try:
        return load_system_prompt()
    except FileNotFoundError:
        logger.error("system_prompt_file_missing")
        raise


def build_full_system_prompt() -> str:
    """
    Return the complete system prompt (base + company knowledge).

    Uses an mtime-based in-memory cache: the prompt is rebuilt from disk only
    when system_prompt.txt or the knowledge index file has changed since the
    last build.  Every call still performs two cheap OS stat() calls to detect
    changes — no disk reads unless the cache is stale.

    Thread-safe: protected by a module-level threading.Lock().

    DEPRECATED PATH: prefer build_base_system_prompt() + RAG retrieval.
    This function is kept for backward compatibility and for deployments
    where pgvector / RAG is not yet available.
    """
    # Fast path: check outside the lock first (read-only, no mutation)
    prompt_mtime    = _get_mtime(SYSTEM_PROMPT_FILE)
    knowledge_mtime = _get_mtime(_KNOWLEDGE_INDEX_FILE)

    with _lock:
        if (
            _cache.content
            and _cache.prompt_mtime    == prompt_mtime
            and _cache.knowledge_mtime == knowledge_mtime
        ):
            return _cache.content   # ← cache hit: zero disk reads

        # Cache miss: rebuild from disk
        try:
            content = _build_full_prompt_uncached()
        except FileNotFoundError:
            logger.error("system_prompt_file_missing")
            raise

        _cache.content         = content
        _cache.prompt_mtime    = prompt_mtime
        _cache.knowledge_mtime = knowledge_mtime
        _cache.version        += 1

        logger.info(
            "prompt_cache_rebuilt version=%d prompt_mtime=%s knowledge_mtime=%s",
            _cache.version, prompt_mtime, knowledge_mtime,
        )
        return content


def write_system_prompt(new_content: str) -> None:
    """
    Atomically write a new system prompt to disk and immediately update the
    in-memory cache so the very next request sees the new prompt without
    needing a disk re-read.

    Uses write-to-temp + rename for atomicity (prevents partially-written
    files from being read by concurrent requests).
    """
    if not new_content.strip():
        raise ValueError("System prompt content cannot be empty.")

    # Validate template placeholders BEFORE touching any file.
    render_prompt_template(new_content)

    # Write atomically: temp file in same directory → rename.
    dir_     = SYSTEM_PROMPT_FILE.parent
    tmp_path = None
    try:
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp", prefix=".prompt_")
        tmp_path = Path(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        _atomic_replace(tmp_path, SYSTEM_PROMPT_FILE)
        tmp_path = None
    except Exception:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    # Immediately rebuild the FULL prompt (base + knowledge) and cache it.
    # This prevents a window where the next request would get an empty-knowledge
    # prompt because write_system_prompt() only stored the rendered base.
    new_mtime = _get_mtime(SYSTEM_PROMPT_FILE)
    try:
        full_prompt = _build_full_prompt_uncached()
    except Exception:
        # If rebuild fails, force cache miss so the next request retries.
        full_prompt = ""

    with _lock:
        _cache.content         = full_prompt
        _cache.prompt_mtime    = new_mtime
        _cache.knowledge_mtime = _get_mtime(_KNOWLEDGE_INDEX_FILE)
        _cache.version        += 1

    logger.info(
        "system_prompt_written version=%d mtime=%s chars=%d",
        _cache.version, new_mtime, len(new_content),
    )
