import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
del _os, _sys

# -*- coding: utf-8 -*-
"""
generate_descriptions.py
────────────────────────
Batch-generate AI product descriptions for all products in the DB
that currently have NULL or empty description fields.

Features:
  - Processes products in batches of 20 (one GPT call per batch → cheap)
  - Resume support: skips already-processed barcodes tracked in desc_progress.json
  - Rate-limit safe: 0.5 s delay between batches
  - Dry-run mode: use --dry-run to preview without writing to DB

Usage:
  python generate_descriptions.py            # run normally
  python generate_descriptions.py --dry-run  # preview only
  python generate_descriptions.py --limit 50 # process only first 50 products
"""

import argparse
import json
import os
import sys
import time
import io

# Force UTF-8 output on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy.orm import Session

load_dotenv()

# ── project imports ────────────────────────────────────────────────────────────
from database import SessionLocal
from models import Brand, Category, Product

# ── config ────────────────────────────────────────────────────────────────────
PROGRESS_FILE = Path(__file__).parent / "desc_progress.json"
BATCH_SIZE    = 20           # products per GPT call
SLEEP_BETWEEN = 0.5          # seconds between batches
MODEL         = "gpt-4o-mini"  # cheapest capable model
MAX_DESC_LEN  = 120          # target description length in words

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── helpers ───────────────────────────────────────────────────────────────────

def load_progress() -> set:
    if PROGRESS_FILE.exists():
        try:
            return set(json.loads(PROGRESS_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_progress(done: set):
    PROGRESS_FILE.write_text(json.dumps(sorted(done), indent=2))


def fetch_products_without_description(db: Session, limit: int | None = None):
    """Return products where description is NULL or blank."""
    q = (
        db.query(Product)
        .filter(
            (Product.description == None) |  # noqa: E711
            (Product.description == "")
        )
        .order_by(Product.barcode)
    )
    if limit:
        q = q.limit(limit)
    return q.all()


def resolve_brand(db: Session, brand_id: int | None) -> str:
    if not brand_id:
        return ""
    b = db.query(Brand).filter(Brand.id == brand_id).first()
    return b.name if b else ""


def resolve_category(db: Session, category_id: int | None) -> str:
    if not category_id:
        return ""
    c = db.query(Category).filter(Category.id == category_id).first()
    return c.name if c else ""


def build_batch_prompt(products: list[dict]) -> str:
    """
    Ask GPT to return a JSON object mapping barcode → description.
    Each description should be 1-2 sentences, professional, suitable for
    a beauty/personal-care e-commerce chatbot.
    """
    items_text = "\n".join(
        f'- barcode: "{p["barcode"]}" | name: "{p["name"]}" | brand: "{p["brand"]}" | category: "{p["category"]}"'
        for p in products
    )

    return f"""You are a professional beauty & personal care product copywriter.

For each product below write a concise 1-2 sentence description (max {MAX_DESC_LEN} words each).
The description must:
- Be accurate to the product type / brand
- Sound premium and inviting
- Be in English
- NOT mention price or availability

Return ONLY a valid JSON object with barcodes as keys and descriptions as values.
Example: {{"BC001": "A luxurious moisturiser...", "BC002": "..."}}

Products:
{items_text}
"""


def generate_descriptions_batch(products: list[dict]) -> dict[str, str]:
    """Call GPT and return {{barcode: description}} mapping."""
    prompt = build_batch_prompt(products)
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        response_format={"type": "json_object"},
        max_tokens=BATCH_SIZE * 80,   # ~80 tokens per description
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ⚠️  JSON parse error: {e}")
        return {}


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Batch-generate product descriptions via GPT")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--limit",   type=int, default=None, help="Max products to process")
    parser.add_argument("--reset",   action="store_true",    help="Ignore previous progress and start fresh")
    args = parser.parse_args()

    db = SessionLocal()
    done_barcodes = set() if args.reset else load_progress()

    print("=" * 60)
    print("  [AI]  Product Description Generator")
    print("=" * 60)
    if args.dry_run:
        print("  [DRY-RUN] mode -- no DB writes")
    if done_barcodes:
        print(f"  [RESUME] {len(done_barcodes)} barcodes already done")
    print()

    try:
        all_products = fetch_products_without_description(db, limit=args.limit)

        # filter out already processed ones
        pending = [p for p in all_products if p.barcode not in done_barcodes]

        print(f"  [INFO] Products without description : {len(all_products)}")
        print(f"  [INFO] Pending this run             : {len(pending)}")
        print()

        if not pending:
            print("  [DONE] Nothing to do. All products already have descriptions.")
            return

        # enrich with brand/category names (batch lookup to avoid N+1)
        brand_cache: dict[int, str] = {}
        cat_cache:   dict[int, str] = {}

        def get_brand(bid):
            if bid is None:
                return ""
            if bid not in brand_cache:
                brand_cache[bid] = resolve_brand(db, bid)
            return brand_cache[bid]

        def get_cat(cid):
            if cid is None:
                return ""
            if cid not in cat_cache:
                cat_cache[cid] = resolve_category(db, cid)
            return cat_cache[cid]

        enriched = [
            {
                "barcode":  p.barcode,
                "name":     p.item_name,
                "brand":    get_brand(p.brand_id),
                "category": get_cat(p.category_id),
            }
            for p in pending
        ]

        # ── batch loop ────────────────────────────────────────────────────────
        total         = len(enriched)
        success_count = 0
        fail_count    = 0

        for batch_start in range(0, total, BATCH_SIZE):
            batch = enriched[batch_start : batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

            print(f"  Batch {batch_num}/{total_batches}  ({len(batch)} products)  ...", end="", flush=True)

            try:
                result = generate_descriptions_batch(batch)
            except Exception as e:
                print(f"  [ERROR] API error: {e}")
                fail_count += len(batch)
                time.sleep(2)
                continue

            written = 0
            for item in batch:
                bc   = item["barcode"]
                desc = result.get(bc, "").strip()
                if not desc:
                    # fallback: try matching by name key (GPT sometimes uses name)
                    desc = result.get(item["name"], "").strip()

                if desc:
                    if not args.dry_run:
                        db.query(Product).filter(Product.barcode == bc).update(
                            {"description": desc}
                        )
                    done_barcodes.add(bc)
                    written += 1
                    success_count += 1
                else:
                    fail_count += 1

            if not args.dry_run:
                db.commit()
                save_progress(done_barcodes)

            print(f"  [OK] {written}/{len(batch)} written")

            if batch_start + BATCH_SIZE < total:
                time.sleep(SLEEP_BETWEEN)

        # ── summary ───────────────────────────────────────────────────────────
        print()
        print("=" * 60)
        print(f"  [SUCCESS] : {success_count}")
        print(f"  [FAILED]  : {fail_count}")
        if args.dry_run:
            print("  [DRY-RUN] -- no changes written to DB")
        else:
            print(f"  [SAVED] Progress saved -> {PROGRESS_FILE.name}")
        print("=" * 60)

    finally:
        db.close()


if __name__ == "__main__":
    main()
