"""
ai/rag_service.py
=================
Production-grade RAG engine for DhifafBot's knowledge base.

Uses pgvector (PostgreSQL) + OpenAI text-embedding-3-small for semantic retrieval.
Implements a startup self-check: if the `vector` extension is not installed in
PostgreSQL, a clear error message is printed once and all retrieval/embedding
functions return empty/sentinel results instead of crashing the chat endpoint.

COST COMPARISON:
──────────────────────────────────────────────────────────────────────────────
  OLD SYSTEM (full-context injection per request):
    10 PDFs = ~50,000 tokens/req  ×  1,000 reqs  ×  $2.50/M  =  $125

  NEW SYSTEM (RAG top-5 chunks per request):
    Any no. of PDFs = ~2,000 tokens/req  ×  1,000 reqs  ×  $2.50/M  =  $5
    Embedding one-time cost (10 PDFs ≈ 50K tokens):
      50,000 tokens  ×  $0.02/M  =  $0.001  (negligible)

  SAVINGS: ~96% reduction in knowledge-base token cost at production scale.
──────────────────────────────────────────────────────────────────────────────
"""
import os
import json
import logging
from typing import Optional

from openai import OpenAI
from sqlalchemy.orm import Session
from sqlalchemy import text, func as sqlfunc, inspect as sa_inspect

from models import KnowledgeChunk

logger = logging.getLogger("RAG")

# ── Constants ──────────────────────────────────────────────────────────────────

EMBEDDING_MODEL  = "text-embedding-3-small"   # OpenAI — best quality/cost ratio
CHUNK_SIZE       = 400                         # tokens per chunk  (~3,000 chars)
CHUNK_OVERLAP    = 50                          # overlapping tokens between chunks
TOP_K_CHUNKS     = 5                           # best-matching chunks per query
MAX_CONTEXT_TOKENS = 3000                      # hard cap on injected knowledge
MIN_SIMILARITY   = 0.30                        # cosine — below this = irrelevant
RAG_OPENAI_ENABLED = os.getenv("OPENAI_API_KEY") is not None

# ── Singleton clients / state ──────────────────────────────────────────────────

_openai_client: Optional[OpenAI] = None
_checksum_label = "[RAG]"


def get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


def _pgvector_ready() -> bool:
    """Return True only when the `vector` extension is registered in PostgreSQL."""
    db: Optional[Session] = None
    try:
        from database import SessionLocal
        db = SessionLocal()
        rows = db.execute(
            text(
                "SELECT 1 FROM pg_extension "
                "WHERE extname = 'vector' LIMIT 1"
            )
        ).fetchall()
        return bool(rows)
    except Exception:
        return False
    finally:
        if db is not None:
            db.close()


def _dummy_embedding(dim: int = 1536) -> list[float]:
    """Return a zero-vector used as a harmless placeholder when embeddings are off."""
    return [0.0] * dim


# ── Tokenisation helpers ───────────────────────────────────────────────────────

def _get_encoder():
    import tiktoken
    return tiktoken.encoding_for_model("gpt-4o")


# ── Startup self-check ─────────────────────────────────────────────────────────

_checked = False
_vector_available = False


def startup_check(db: Session) -> None:
    """Print a single one-line bootstrap message and set global availability flag."""
    global _checked, _vector_available
    if _checked:
        return
    _checked = True

    if not _pgvector_ready():
        print(
            f"{_checksum_label} WARNING: pgvector extension not found in PostgreSQL.\n"
            "  RAG retrieval will return empty. No chat functionality is affected.\n"
            "  Install: pip install pgvector  |  CREATE EXTENSION vector;\n"
        )
        _vector_available = False
        return

    # Try to create pgvector_type column on knowledge_chunks
    try:
        db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS knowledge_chunks (\n"
                "    id                  SERIAL PRIMARY KEY,\n"
                "    knowledge_id        VARCHAR(100) NOT NULL,\n"
                "    original_filename   VARCHAR(500) NOT NULL,\n"
                "    chunk_index         INTEGER NOT NULL,\n"
                "    chunk_text          TEXT NOT NULL,\n"
                "    token_count         INTEGER,\n"
                "    embedding           VECTOR(1536),\n"
                "    created_at          TIMESTAMP DEFAULT now()\n"
                ")"
            )
        )
        db.commit()
        print(f"{_checksum_label} knowledge_chunks table bootstrapped.")
    except Exception as exc:
        print(f"{_checksum_label} WARNING: could not bootstrap knowledge_chunks: {exc}")

    _vector_available = True


# ── Public API ─────────────────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    """Split *text* into overlapping token chunks using tiktoken."""
    encoder = _get_encoder()
    tokens = encoder.encode(text)
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = start + CHUNK_SIZE
        chunks.append(encoder.decode(tokens[start:end]))
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def get_embedding(text: str) -> list[float]:
    """Return the text-embedding-3-small vector for *text*."""
    if not RAG_OPENAI_ENABLED:
        logger.warning("RAG disabled: OPENAI_API_KEY not set. Returning zero-vector.")
        return _dummy_embedding()
    response = get_openai_client().embeddings.create(
        model=EMBEDDING_MODEL,
        input=text.strip(),
    )
    return response.data[0].embedding


def embed_and_store_knowledge(
    db: Session,
    knowledge_id: str,
    original_filename: str,
    text: str,
) -> int:
    """Embed *text* and store chunks; returns number of chunks successfully saved."""
    # Remove any pre-existing chunks for this knowledge_id
    db.query(KnowledgeChunk).filter(
        KnowledgeChunk.knowledge_id == knowledge_id
    ).delete(synchronize_session=False)
    db.commit()

    chunks = chunk_text(text)
    stored = 0

    for i, chunk in enumerate(chunks):
        chunk_stripped = chunk.strip()
        if not chunk_stripped:
            continue

        try:
            embedding = get_embedding(chunk_stripped)
            token_count = len(_get_encoder().encode(chunk_stripped))

            record = KnowledgeChunk(
                knowledge_id     = knowledge_id,
                original_filename= original_filename,
                chunk_index      = i,
                chunk_text       = chunk_stripped,
                token_count      = token_count,
                embedding        = embedding,
            )
            db.add(record)
            stored += 1
        except Exception as exc:
            logger.error("RAG: failed to embed chunk %d for '%s': %s", i, original_filename, exc)

    db.commit()
    return stored


def retrieve_relevant_chunks(
    db: Session,
    query: str,
    top_k: int = TOP_K_CHUNKS,
) -> str:
    """Return the concatenated text of the top-*top_k* most relevant chunks for *query*, or empty string."""
    # ── Guard: pgvector extension ──────────────────────────────────────────────
    if not _vector_available:
        return ""

    # ── Guard: OPENAI key ─────────────────────────────────────────────────────
    if not RAG_OPENAI_ENABLED:
        logger.warning("RAG: OPENAI_API_KEY not set — skipping retrieval.")
        return ""

    query_embedding: Optional[list[float]] = None

    try:
        query_embedding = get_embedding(query)
    except Exception as exc:
        logger.error("RAG: embedding query failed: %s", exc)
        return ""

    if query_embedding is None:
        return ""

    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    try:
        rows = db.execute(
            text(
                "SELECT chunk_text, original_filename, chunk_index, token_count, "
                "1 - (embedding <=> CAST(:q AS vector)) AS similarity "
                "FROM knowledge_chunks "
                "WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:q AS vector) "
                "LIMIT :top_k"
            ),
            {"q": vec_str, "top_k": top_k},
        ).fetchall()
    except Exception as exc:
        logger.error("RAG: pgvector query failed: %s", exc)
        return ""

    if not rows:
        return ""

    relevant = [r for r in rows if (r.similarity or 0.0) >= MIN_SIMILARITY]
    if not relevant:
        return ""

    parts: list[str] = []
    total_tokens = 0

    for r in relevant:
        chunk_tokens = r.token_count or 100
        if total_tokens + chunk_tokens > MAX_CONTEXT_TOKENS:
            break
        parts.append(f"[From: {r.original_filename}]\n{r.chunk_text}")
        total_tokens += chunk_tokens

    return "\n\n---\n\n".join(parts)


def delete_knowledge_chunks(db: Session, knowledge_id: str) -> int:
    """Delete all chunks for a given knowledge file. Returns number of rows deleted."""
    deleted = db.query(KnowledgeChunk).filter(
        KnowledgeChunk.knowledge_id == knowledge_id
    ).delete(synchronize_session=False)
    db.commit()
    return deleted


def get_knowledge_stats(db: Session) -> list[dict]:
    """Per-file chunk/token stats for the admin dashboard."""
    rows = db.execute(
        text(
            "SELECT knowledge_id, original_filename, "
            "       COUNT(*) AS chunk_count, "
            "       SUM(token_count) AS total_tokens, "
            "       MIN(created_at) AS created_at "
            "FROM knowledge_chunks "
            "GROUP BY knowledge_id, original_filename "
            "ORDER BY created_at DESC"
        )
    ).fetchall()

    return [
        {
            "knowledge_id":  r.knowledge_id,
            "filename":      r.original_filename,
            "chunks":        r.chunk_count,
            "total_tokens":  r.total_tokens,
            "created_at":    str(r.created_at) if r.created_at else None,
        }
        for r in rows
    ]
