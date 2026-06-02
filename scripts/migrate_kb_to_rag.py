import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
del _os, _sys

"""
migrate_kb_to_rag.py
====================
One-time script to embed all existing knowledge-base files into the RAG
knowledge_chunks table.

Run once after deployment (after pgvector is installed):
    python migrate_kb_to_rag.py

The script is idempotent: re-running it re-embeds all files and replaces
existing chunks, so it is safe to re-run manually.
"""
import json
import os
import sys
import logging

from database import SessionLocal
from ai.rag_service import embed_and_store_knowledge, startup_check, _vector_available

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("migrate_kb")


def migrate() -> None:
    db = SessionLocal()

    # ── Pre-flight check ───────────────────────────────────────────────────────
    try:
        startup_check(db)
    except Exception as exc:
        logger.error("Startup check failed: %s", exc)
        db.close()
        return

    if not _vector_available:
        logger.error(
            "Cannot run migration: pgvector extension is not installed.\n"
            "  Install pgvector first, then re-run this script.\n"
            "  pip install pgvector\n"
            "  CREATE EXTENSION vector;  # in PostgreSQL"
        )
        db.close()
        return

    # ── Load knowledge-base index ───────────────────────────────────────────────
    index_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "knowledge_base", "index.json",
    )

    if not os.path.exists(index_path):
        logger.warning("No knowledge_base/index.json found — nothing to migrate.")
        db.close()
        return

    with open(index_path, encoding="utf-8") as fh:
        kb_index = json.load(fh)

    logger.info("Found %d knowledge file(s) to migrate.", len(kb_index))

    # ── Embed each file ────────────────────────────────────────────────────────
    total_chunks = 0
    total_files = 0

    base_dir = os.path.dirname(os.path.abspath(__file__))

    for entry in kb_index:
        knowledge_id = entry["id"]
        original_filename = entry["original_filename"]
        text_filename = entry.get("text_filename", "")

        if not text_filename:
            logger.warning(
                "Skipping '%s': no text_filename in index.", original_filename
            )
            continue

        text_path = os.path.join(base_dir, "knowledge_base", text_filename)
        if not os.path.exists(text_path):
            logger.warning(
                "Skipping '%s': text file not found at %s",
                original_filename, text_path,
            )
            continue

        with open(text_path, encoding="utf-8") as fh:
            text = fh.read()

        if not text.strip():
            logger.warning("Skipping '%s': extracted text is empty.", original_filename)
            continue

        logger.info("Embedding: %s ...", original_filename)

        try:
            stored = embed_and_store_knowledge(
                db=db,
                knowledge_id=knowledge_id,
                original_filename=original_filename,
                text=text,
            )
            logger.info("  -> %d chunks stored.", stored)
            total_chunks += stored
            total_files += 1
        except Exception as exc:
            logger.error("  FAILED to embed '%s': %s", original_filename, exc)

    db.close()
    logger.info(
        "Migration complete: %d file(s), %d chunk(s) stored.",
        total_files, total_chunks,
    )


if __name__ == "__main__":
    migrate()
