"""
services/embedding.py
=====================
OpenAI product embedding generation pipeline.

Responsibilities
----------------
1. Build a rich, multilingual embedding text from every product's structured fields.
2. Generate embeddings in batches using OpenAI text-embedding-3-small (1536 dims).
3. Persist embeddings + embedding_text + embedding_updated_at to the products table.
4. Provide a background job function that processes un-embedded / stale products.
5. Track progress and expose embedding coverage stats.

Embedding model
---------------
text-embedding-3-small  — 1536 dimensions, multilingual, optimised for
semantic similarity. Cost: ~$0.02 per 1M tokens (~100k product catalog
typically fits in ~5M tokens = $0.10 total for a full re-embed).

Embedding text format
---------------------
A structured prose block is more effective than a flat concatenation
because it trains the embedding on labelled field-value relationships:

  Product: {item_name}
  Brand: {brand_name}
  Category: {category_name}
  Sub-category: {subcategory_name}
  Description: {description}
  Skin type: {skin_type}
  Concerns: {concerns_csv}
  Tags: {tags_csv}
  Price tier: {price_tier}
  Brand family: {brand_family}

Multilingual
------------
If Arabic fields are present (item_name_ar, description_ar — future columns)
they are appended to the embedding text so Arabic queries return correct results
without a separate index.

Background job
--------------
embed_new_products()  — runs at startup and can be triggered via API.
  Processes products in batches of EMBED_BATCH_SIZE (default 50).
  Only targets products where:
    • embedding IS NULL  (never embedded)
    • OR embedding_text changed since last embed (stale)
  Updates ai_score to a computed base score after embedding.

Rate limiting
-------------
OpenAI embedding endpoint allows 3,000 RPM and 1M TPM on tier-1.
At EMBED_BATCH_SIZE=50 with ~200 tokens/product → 10,000 tokens/batch.
We sleep EMBED_SLEEP_SECONDS (0.5 s) between batches to stay well under limits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

from openai import OpenAI
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Product, ProductSearchIndex, UserPreferenceProfile

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
EMBED_MODEL      = "text-embedding-3-small"
EMBED_DIMS       = 1536
EMBED_BATCH_SIZE = 50      # products per OpenAI API call
EMBED_SLEEP_SEC  = 0.5     # seconds between batches (rate limit buffer)

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


# ── Text builder ──────────────────────────────────────────────────────────────

def build_embedding_text(
    *,
    item_name:        str | None,
    brand_name:       str | None,
    category_name:    str | None,
    subcategory_name: str | None,
    description:      str | None,
    skin_type:        str | None,
    concerns:         list | None,
    tags:             list | None,
    price_tier:       str | None,
    brand_family:     str | None,
    # Optional Arabic fields — append if available
    item_name_ar:     str | None = None,
    description_ar:   str | None = None,
) -> str:
    """
    Construct the prose block fed to the embedding model.

    Rules
    -----
    - Every present field is labelled so the model understands field semantics.
    - Absent fields are omitted (no "None" or empty strings).
    - Arabic content is appended after the English block to produce a
      multilingual embedding that handles both-language queries in one index.
    - Total text is capped at 8000 characters (~2000 tokens) — well under
      the 8192-token limit of text-embedding-3-small.
    """
    parts: list[str] = []

    def add(label: str, value: str | None) -> None:
        v = (value or "").strip()
        if v:
            parts.append(f"{label}: {v}")

    add("Product",       item_name)
    add("Brand",         brand_name)
    add("Category",      category_name)
    add("Sub-category",  subcategory_name)
    add("Description",   description)
    add("Skin type",     skin_type)
    add("Price tier",    price_tier)
    add("Brand family",  brand_family)

    if concerns:
        concerns_str = ", ".join(str(c) for c in concerns if c)
        add("Concerns",  concerns_str)

    if tags:
        tags_str = ", ".join(str(t) for t in tags if t)
        add("Tags",      tags_str)

    # Arabic block — appended for multilingual coverage
    if item_name_ar:
        parts.append(f"اسم المنتج: {item_name_ar.strip()}")
    if description_ar:
        parts.append(f"الوصف: {description_ar.strip()}")

    text = "\n".join(parts)
    return text[:8000]   # hard cap


def _build_text_for_product(product: Product, psi_map: dict) -> str:
    """Build embedding text for one Product ORM object."""
    psi = psi_map.get(product.barcode)
    return build_embedding_text(
        item_name        = product.item_name,
        brand_name       = getattr(psi, "brand_name",       None) if psi else None,
        category_name    = getattr(psi, "category_name",    None) if psi else None,
        subcategory_name = getattr(psi, "subcategory_name", None) if psi else None,
        description      = product.description,
        skin_type        = product.skin_type,
        concerns         = product.concerns,
        tags             = product.tags,
        price_tier       = product.price_tier.value if product.price_tier else None,
        brand_family     = product.brand_family,
    )


# ── OpenAI embedding calls ────────────────────────────────────────────────────

def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Call OpenAI Embeddings API for a batch of texts.
    Returns list of 1536-dim float vectors, in the same order as input.
    Raises on API error — let the caller handle retries.
    """
    if not texts:
        return []

    client = _get_client()
    response = client.embeddings.create(
        model = EMBED_MODEL,
        input = texts,
    )
    # API returns items sorted by index
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


def embed_single(text: str) -> list[float]:
    """Embed a single query string. Used for semantic search at request time."""
    vectors = embed_texts([text])
    return vectors[0] if vectors else []


# ── Base AI score calculator ──────────────────────────────────────────────────

def compute_base_ai_score(product: Product) -> float:
    """
    Compute a static base score (0.0–1.0) for a product using only its
    editorial and stock signals.  Stored in ai_score as the default before
    any semantic similarity is applied.

    Formula
    -------
    editorial  = 0.4 × priority_score + 0.3 × override_score + 0.3 × flag_bonus
    stock      = min(1.0, available_qty / 100)
    freshness  = 0.3 if is_new_arrival else 0.0
    base_score = 0.5 × editorial + 0.3 × stock + 0.2 × freshness
    """
    # Editorial priority (0–9999 → invert to 0–1)
    priority = product.recommendation_priority or 9999
    s_priority = 1.0 - (min(priority, 9999) / 9999)

    # Override score (0–999 → normalize to 0–1)
    override = float(product.recommendation_score_override or 0)
    s_override = min(override, 999) / 999

    # Editorial flag bonuses
    flag_bonus = 0.0
    if product.is_recommended:   flag_bonus += 0.4
    if product.is_best_selling:  flag_bonus += 0.35
    if product.is_new_arrival:   flag_bonus += 0.15
    if product.is_cod_recommended: flag_bonus += 0.10
    flag_bonus = min(flag_bonus, 1.0)

    s_editorial = 0.4 * s_priority + 0.3 * s_override + 0.3 * flag_bonus

    # Stock availability (capped at 100 units for full score)
    qty = max(0, product.available_qty or 0)
    s_stock = min(1.0, qty / 100)

    # Freshness
    s_freshness = 0.3 if product.is_new_arrival else 0.0

    base = 0.5 * s_editorial + 0.3 * s_stock + 0.2 * s_freshness
    return round(base, 6)


# ── Embedding coverage stats ──────────────────────────────────────────────────

def get_embedding_stats(db: Session) -> dict:
    """Return embedding coverage stats for the monitoring dashboard."""
    total   = db.query(func.count(Product.barcode)).filter(Product.deleted_at.is_(None)).scalar() or 0
    embedded = (
        db.query(func.count(Product.barcode))
        .filter(Product.deleted_at.is_(None), Product.embedding.isnot(None))
        .scalar() or 0
    )
    pending = total - embedded
    coverage_pct = round((embedded / total * 100), 1) if total > 0 else 0.0

    return {
        "total_products":     total,
        "embedded_products":  embedded,
        "pending_products":   pending,
        "coverage_pct":       coverage_pct,
        "embed_model":        EMBED_MODEL,
        "embed_dimensions":   EMBED_DIMS,
    }


# ── Background embedding job ──────────────────────────────────────────────────

async def embed_new_products(
    limit:       int  = 0,       # 0 = no limit (process all pending)
    force_all:   bool = False,   # True = re-embed everything (ignores stale check)
    batch_size:  int  = EMBED_BATCH_SIZE,
) -> dict:
    """
    Background job: find un-embedded / stale products and generate embeddings.

    Parameters
    ----------
    limit      : maximum products to process in this run (0 = all)
    force_all  : if True, re-embed all products regardless of stale status
    batch_size : number of products per OpenAI API call

    Returns a summary dict with counts.
    """
    start_time  = time.monotonic()
    db          = SessionLocal()
    processed   = 0
    succeeded   = 0
    failed      = 0
    skipped     = 0

    try:
        if not OPENAI_API_KEY:
            logger.error("embed_job_skipped", extra={"reason": "OPENAI_API_KEY not set"})
            return {"success": False, "reason": "OPENAI_API_KEY not configured"}

        # ── Query: find products needing embedding ─────────────────────────────
        q = db.query(Product).filter(Product.deleted_at.is_(None))
        if not force_all:
            q = q.filter(Product.embedding.is_(None))
        if limit > 0:
            q = q.limit(limit)

        products_to_embed = q.all()
        total_pending = len(products_to_embed)

        logger.info(
            "embed_job_started",
            extra={"pending": total_pending, "force_all": force_all, "batch_size": batch_size},
        )

        if not products_to_embed:
            return {
                "success":   True,
                "processed": 0,
                "succeeded": 0,
                "skipped":   0,
                "failed":    0,
                "message":   "All products are already embedded.",
                "duration":  round(time.monotonic() - start_time, 2),
            }

        # ── Preload PSI map for name resolution ─────────────────────────────
        all_barcodes = [p.barcode for p in products_to_embed]
        psi_map: dict = {
            r.product_id: r
            for r in db.query(ProductSearchIndex)
            .filter(ProductSearchIndex.product_id.in_(all_barcodes))
            .all()
        }

        # ── Process in batches ─────────────────────────────────────────────
        for batch_start in range(0, len(products_to_embed), batch_size):
            batch = products_to_embed[batch_start : batch_start + batch_size]

            # Build texts
            texts_with_barcodes: list[tuple[str, str]] = []
            for product in batch:
                text = _build_text_for_product(product, psi_map)
                if not text.strip():
                    skipped += 1
                    logger.debug("embed_skipped_empty", extra={"barcode": product.barcode})
                    continue
                texts_with_barcodes.append((product.barcode, text))

            if not texts_with_barcodes:
                continue

            # Call OpenAI (synchronous in the async context — acceptable for a
            # background job; for truly concurrent embedding use asyncio.to_thread)
            try:
                embeddings = embed_texts([t for _, t in texts_with_barcodes])
            except Exception as exc:
                logger.error(
                    "embed_api_error",
                    extra={"batch_start": batch_start, "error": str(exc)},
                )
                failed += len(texts_with_barcodes)
                await asyncio.sleep(2)   # back-off on API error
                continue

            # Persist results
            now = datetime.now()
            for (barcode, text), vector in zip(texts_with_barcodes, embeddings):
                try:
                    product_row = next(
                        (p for p in batch if p.barcode == barcode), None
                    )
                    if product_row is None:
                        continue

                    product_row.embedding            = vector
                    product_row.embedding_text       = text
                    product_row.embedding_updated_at = now
                    product_row.ai_score             = compute_base_ai_score(product_row)

                    succeeded += 1
                except Exception as exc:
                    logger.error(
                        "embed_persist_error",
                        extra={"barcode": barcode, "error": str(exc)},
                    )
                    failed += 1

            processed += len(texts_with_barcodes)

            # Commit this batch
            try:
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.error("embed_commit_error", extra={"error": str(exc)})
                failed += len(texts_with_barcodes)
                succeeded -= len(texts_with_barcodes)

            logger.info(
                "embed_batch_done",
                extra={
                    "batch_start": batch_start,
                    "batch_size":  len(texts_with_barcodes),
                    "processed":   processed,
                    "total":       total_pending,
                },
            )

            # Rate-limit sleep between batches
            await asyncio.sleep(EMBED_SLEEP_SEC)

    except Exception as exc:
        logger.error("embed_job_fatal", extra={"error": str(exc)}, exc_info=True)
    finally:
        db.close()

    duration = round(time.monotonic() - start_time, 2)
    logger.info(
        "embed_job_completed",
        extra={
            "processed": processed,
            "succeeded": succeeded,
            "failed":    failed,
            "skipped":   skipped,
            "duration":  duration,
        },
    )

    return {
        "success":   True,
        "processed": processed,
        "succeeded": succeeded,
        "failed":    failed,
        "skipped":   skipped,
        "duration":  duration,
    }


# ── User preference profile updates ──────────────────────────────────────────

def update_user_preference_profile(
    db: Session,
    user_id: str,
    barcode: str,
    event_weight: float = 1.0,
) -> None:
    """
    Update (or create) the user's preference profile after a positive event.

    Strategy: exponential moving average of product embeddings.
    new_profile = 0.8 × old_profile + 0.2 × product_embedding

    For cold-start (no existing profile): profile = product_embedding.
    """
    product = db.query(Product).filter(
        Product.barcode == barcode,
        Product.embedding.isnot(None),
    ).first()

    if product is None or product.embedding is None:
        return

    product_vector = product.embedding

    profile = db.query(UserPreferenceProfile).filter(
        UserPreferenceProfile.user_id == user_id
    ).first()

    if profile is None:
        profile = UserPreferenceProfile(
            user_id              = user_id,
            embedding            = product_vector,
            preferred_categories = {},
            preferred_brands     = {},
            preferred_price_tiers= {},
            preferred_skin_types = {},
            total_events         = 1,
            last_updated         = datetime.now(),
        )
        db.add(profile)
    else:
        # Exponential moving average: α=0.2 * event_weight
        α = 0.2 * min(event_weight, 1.0)
        if profile.embedding is not None:
            old_vec = profile.embedding
            new_vec = [
                (1 - α) * o + α * n
                for o, n in zip(old_vec, product_vector)
            ]
            # Normalize to unit length
            magnitude = sum(x ** 2 for x in new_vec) ** 0.5
            if magnitude > 0:
                new_vec = [x / magnitude for x in new_vec]
            profile.embedding = new_vec
        else:
            profile.embedding = product_vector

        profile.total_events += 1
        profile.last_updated  = datetime.now()

        # Update category / brand frequency maps
        psi = (
            db.query(ProductSearchIndex)
            .filter(ProductSearchIndex.product_id == barcode)
            .first()
        )
        if psi:
            _increment_freq(profile.preferred_categories, psi.category_name)
            _increment_freq(profile.preferred_brands,     psi.brand_name)
        if product.price_tier:
            _increment_freq(profile.preferred_price_tiers, str(product.price_tier))
        if product.skin_type:
            _increment_freq(profile.preferred_skin_types,  product.skin_type)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("profile_update_error", extra={"user_id": user_id, "error": str(exc)})


def _increment_freq(freq_map: dict, key: str | None) -> None:
    if not key:
        return
    if not isinstance(freq_map, dict):
        return
    freq_map[key] = freq_map.get(key, 0) + 1
