"""
services/normalization.py
=========================
Brand and category name normalization.

Problems solved
---------------
1. Duplicate entity creation due to case / whitespace / punctuation differences.
   "NYX", "nyx", "Nyx " all resolve to the same canonical record.

2. Synonym collapse — a configurable mapping lets common alternate names resolve
   to a single canonical name before the DB lookup.
   e.g. "L'Oreal", "L'Oréal", "loreal" → "L'Oréal Paris"

3. Structural logging — every resolution decision is logged so operators can
   review and extend the synonym map without code changes.

Usage
-----
    from services.normalization import NameNormalizer

    normalizer = NameNormalizer()
    canonical = normalizer.resolve_brand("loreal")    # → "L'Oréal Paris"
    canonical = normalizer.resolve_category("skincare") # → "Skincare"
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

logger = logging.getLogger(__name__)


# ── Synonym dictionaries ──────────────────────────────────────────────────────
# Keys are lowercase-normalised versions of alternate names.
# Values are the canonical display names stored in the DB.
#
# Extend these dictionaries as you encounter new catalog exports — no code
# change needed, just add a key → canonical pair.

BRAND_SYNONYMS: dict[str, str] = {
    # L'Oréal family
    "loreal":                     "L'Oréal Paris",
    "l'oreal":                    "L'Oréal Paris",
    "l'oréal":                    "L'Oréal Paris",
    "loreal paris":               "L'Oréal Paris",
    # Estée Lauder
    "estee lauder":               "Estée Lauder",
    "estée lauder":               "Estée Lauder",
    # MAC
    "mac cosmetics":              "MAC",
    "m.a.c":                      "MAC",
    "m.a.c.":                     "MAC",
    # NYX
    "nyx professional makeup":    "NYX",
    "nyx cosmetics":              "NYX",
    # Maybelline
    "maybelline new york":        "Maybelline",
    "maybelline ny":              "Maybelline",
    # Garnier
    "garnier skin active":        "Garnier",
    # Revlon
    "revlon professional":        "Revlon",
    # Nivea
    "nivea skin":                 "Nivea",
    # Neutrogena
    "neutrogena":                 "Neutrogena",
}

CATEGORY_SYNONYMS: dict[str, str] = {
    # Skincare variants
    "skin care":                  "Skincare",
    "skin-care":                  "Skincare",
    "facial care":                "Skincare",
    "face care":                  "Skincare",
    # Haircare variants
    "hair care":                  "Haircare",
    "hair-care":                  "Haircare",
    "hair products":              "Haircare",
    # Makeup variants
    "make up":                    "Makeup",
    "make-up":                    "Makeup",
    "cosmetics":                  "Makeup",
    # Fragrance variants
    "perfume":                    "Fragrance",
    "perfumes":                   "Fragrance",
    "parfum":                     "Fragrance",
    "cologne":                    "Fragrance",
    # Body care variants
    "body care":                  "Body Care",
    "bodycare":                   "Body Care",
    # Suncare
    "sun care":                   "Sun Care",
    "suncare":                    "Sun Care",
    "sunscreen":                  "Sun Care",
}

SUBCATEGORY_SYNONYMS: dict[str, str] = {
    "moisturiser":                "Moisturizer",
    "moisturisers":               "Moisturizer",
    "moisturizers":               "Moisturizer",
    "eye cream":                  "Eye Creams",
    "lip gloss":                  "Lip Glosses",
    "lip liner":                  "Lip Liners",
    "foundation":                 "Foundations",
    "bb cream":                   "BB Creams",
    "cc cream":                   "CC Creams",
    "face mask":                  "Face Masks",
    "sheet mask":                 "Sheet Masks",
    "face serum":                 "Face Serums",
    "face serums":                "Face Serums",
    "toner":                      "Toners",
    "cleanser":                   "Cleansers",
    "face wash":                  "Cleansers",
    "micellar water":             "Cleansers",
    "face oil":                   "Face Oils",
    "face oils":                  "Face Oils",
}


# ── Normaliser class ──────────────────────────────────────────────────────────

class NameNormalizer:
    """
    Resolves raw entity names (brand / category / subcategory) from catalog
    exports to their canonical display names.

    Resolution order
    ----------------
    1. Strip + collapse whitespace
    2. Synonym lookup (case-insensitive key lookup in synonym dict)
    3. If no synonym match → Title-case the cleaned string

    All three entity types share the same interface:
      normalizer.resolve_brand(raw)        → canonical str | None
      normalizer.resolve_category(raw)     → canonical str | None
      normalizer.resolve_subcategory(raw)  → canonical str | None
    """

    def __init__(
        self,
        brand_synonyms:       Optional[dict[str, str]] = None,
        category_synonyms:    Optional[dict[str, str]] = None,
        subcategory_synonyms: Optional[dict[str, str]] = None,
    ) -> None:
        # Merge caller-supplied synonyms on top of built-in defaults
        self._brands       = {**BRAND_SYNONYMS,       **(brand_synonyms or {})}
        self._categories   = {**CATEGORY_SYNONYMS,    **(category_synonyms or {})}
        self._subcategories= {**SUBCATEGORY_SYNONYMS, **(subcategory_synonyms or {})}

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _clean(raw) -> str | None:
        """Strip, collapse internal whitespace, return None if empty or NaN."""
        if raw is None:
            return None
        # pandas NaN comes in as float — treat as empty
        if isinstance(raw, float):
            import math
            if math.isnan(raw):
                return None
            raw = str(raw)
        elif not isinstance(raw, str):
            raw = str(raw)
        cleaned = re.sub(r"\s+", " ", raw.strip())
        return cleaned if cleaned and cleaned.lower() not in {"nan", "none", "null", "n/a", "na"} else None

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Return the lowercase-normalised key used for synonym lookups."""
        # Normalise unicode (e.g. é → e for comparison) then lowercase
        nfkd = unicodedata.normalize("NFKD", name)
        ascii_approx = "".join(c for c in nfkd if not unicodedata.combining(c))
        return ascii_approx.lower().strip()

    def _resolve(
        self,
        raw: str | None,
        synonyms: dict[str, str],
        entity_type: str,
    ) -> str | None:
        cleaned = self._clean(raw)
        if not cleaned:
            return None

        key = self._lookup_key(cleaned)

        if key in synonyms:
            canonical = synonyms[key]
            if canonical.lower() != cleaned.lower():
                logger.info(
                    "name_normalized",
                    extra={
                        "entity":    entity_type,
                        "raw":       cleaned,
                        "canonical": canonical,
                    },
                )
            return canonical

        # No synonym → Title-case
        titlecased = cleaned.title()
        if titlecased != cleaned:
            logger.debug(
                "name_titlecased",
                extra={"entity": entity_type, "raw": cleaned, "result": titlecased},
            )
        return titlecased

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve_brand(self, raw: str | None) -> str | None:
        return self._resolve(raw, self._brands, "brand")

    def resolve_category(self, raw: str | None) -> str | None:
        return self._resolve(raw, self._categories, "category")

    def resolve_subcategory(self, raw: str | None) -> str | None:
        return self._resolve(raw, self._subcategories, "subcategory")


# ── Module-level singleton — import and use directly ──────────────────────────
# Services that call _get_or_create_brand / _get_or_create_category
# import this singleton to normalise names before DB lookups.

_default_normalizer: Optional[NameNormalizer] = None


def get_normalizer() -> NameNormalizer:
    global _default_normalizer
    if _default_normalizer is None:
        _default_normalizer = NameNormalizer()
    return _default_normalizer
