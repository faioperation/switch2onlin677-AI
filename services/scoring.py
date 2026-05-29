"""
services/scoring.py
===================
Recommendation scoring pipeline.

Architecture
------------
Scorer (Protocol)
  ├── RuleBasedScorer       — trusts DB ordering; adds score_reason="editorial"
  ├── PersonalizationScorer — diversity + session-history re-ranking
  └── HybridAIScorer        — blends editorial score with ai_score (vector-derived)
                              Active when RECOMMENDATION_SCORER=hybrid_ai

ScoringPipeline
  Wraps any Scorer.  Called by every recommendation router endpoint.
  Adds scoring metadata to the response envelope.

get_active_scorer()
  Reads RECOMMENDATION_SCORER env var:
    "rule_based"      → RuleBasedScorer  (default, safe for zero-embedding state)
    "personalization" → PersonalizationScorer
    "hybrid_ai"       → HybridAIScorer   (activate after embedding coverage > 50%)

HybridAIScorer
  Re-ranks results by blending editorial priority with the ai_score field
  (pre-computed by the embedding pipeline: a weighted mix of semantic
  similarity, popularity, stock, and freshness signals).

  final = α × ai_score + (1-α) × editorial_score
  Default α = 0.55 (SCORE_ALPHA env var).
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from core.recommendation_context import RecommendationContext

logger = logging.getLogger(__name__)

# Alpha for hybrid blending: ai_score weight (1-alpha = editorial weight)
_SCORE_ALPHA = float(os.getenv("SCORE_ALPHA", "0.55"))


# ── Scorer Protocol ───────────────────────────────────────────────────────────

@runtime_checkable
class Scorer(Protocol):
    """
    Any recommendation scorer must implement this interface.

    score() receives the already-formatted product list (output of
    format_products()) and may re-order, filter, or enrich it.
    It must return a list of dicts in the same format.

    Scorers must be stateless and thread-safe.
    """

    name: str

    def score(
        self,
        products: list[dict],
        context:  RecommendationContext,
    ) -> list[dict]:
        """Re-rank and/or enrich products. Returns the list in final order."""
        ...


# ── RuleBasedScorer ───────────────────────────────────────────────────────────

class RuleBasedScorer:
    """
    Production scorer (Phase 1).

    DB queries in services/recommendation.py already order results by:
      1. recommendation_priority  ASC NULLS LAST   (lower = higher rank)
      2. recommendation_score_override DESC NULLS LAST

    This scorer trusts that ordering and performs no re-ranking.
    It only annotates each product with score metadata for transparency.

    To activate: RECOMMENDATION_SCORER=rule_based (default)
    """

    name = "rule_based"

    def score(
        self,
        products: list[dict],
        context:  RecommendationContext,
    ) -> list[dict]:
        for product in products:
            product["_score_reason"] = "editorial"
        return products


# ── PersonalizationScorer ─────────────────────────────────────────────────────

class PersonalizationScorer:
    """
    Phase 3 ML scorer — placeholder implementation.

    When fully implemented this scorer will:
      1. Load the user preference vector from the embedding store
         (skin_type affinity, brand_family affinity, price_tier affinity).
      2. Score each product against the vector using cosine similarity.
      3. Blend: final_score = α × editorial_score + (1-α) × ml_score
      4. Re-rank products by final_score DESC.
      5. Apply diversity: if viewed_barcodes is set, push already-seen
         products to the end of the list.

    Until the embedding store is connected, falls back to RuleBasedScorer
    and logs a warning so the gap is visible in monitoring.

    To activate: RECOMMENDATION_SCORER=personalization
    """

    name = "personalization"
    _fallback = RuleBasedScorer()

    def score(
        self,
        products: list[dict],
        context:  RecommendationContext,
    ) -> list[dict]:
        # ── Diversity filter (safe to run now) ────────────────────────────────
        # Deprioritize products the user has already seen this session.
        if context.viewed_barcodes:
            seen = set(context.viewed_barcodes)
            unseen = [p for p in products if p.get("id") not in seen]
            already_seen = [p for p in products if p.get("id") in seen]
            products = unseen + already_seen   # seen items pushed to end

        # ── Cart exclusion (safe to run now) ──────────────────────────────────
        # Remove items already in cart from recommendations.
        if context.cart_barcodes:
            in_cart = set(context.cart_barcodes)
            products = [p for p in products if p.get("id") not in in_cart]

        # ── ML re-ranking (not yet implemented) ──────────────────────────────
        # TODO: replace this block when the embedding store is ready.
        logger.debug(
            "PersonalizationScorer: ML re-ranking not yet implemented "
            "for user_id=%s — using editorial order.",
            context.user_id,
        )

        for product in products:
            product["_score_reason"] = (
                "personalized_diversity" if context.has_session_history
                else "personalized_editorial"
            )
        return products


# ── HybridAIScorer ────────────────────────────────────────────────────────────

class HybridAIScorer:
    """
    Blends the DB-stored ai_score (computed by the embedding pipeline from
    semantic signals) with the editorial ordering already applied by the query.

    When activated (RECOMMENDATION_SCORER=hybrid_ai) this scorer:
      1. Reads each product's ai_score field (set by embedding pipeline).
      2. Computes an editorial_score from recommendation_priority /
         recommendation_score_override (if those fields are in the dict).
      3. Blends: final = SCORE_ALPHA × ai_score + (1-SCORE_ALPHA) × editorial
      4. Re-sorts the product list by final score descending.
      5. Applies session diversity (same as PersonalizationScorer).

    Activate once embedding coverage exceeds 50% of the catalog.
    """

    name = "hybrid_ai"

    def score(
        self,
        products: list[dict],
        context:  RecommendationContext,
    ) -> list[dict]:
        alpha    = _SCORE_ALPHA
        beta     = 1.0 - alpha

        # ── Session diversity ─────────────────────────────────────────────────
        if context.viewed_barcodes:
            seen   = set(context.viewed_barcodes)
            unseen = [p for p in products if p.get("id") not in seen]
            seen_p = [p for p in products if p.get("id") in seen]
            products = unseen + seen_p

        if context.cart_barcodes:
            in_cart  = set(context.cart_barcodes)
            products = [p for p in products if p.get("id") not in in_cart]

        # ── Compute blended score ─────────────────────────────────────────────
        for p in products:
            ai_s = float(p.get("ai_score") or 0.0)

            # Editorial score from priority (lower = better → invert to 0-1)
            priority  = p.get("recommendation_priority") or 9999
            s_priority = 1.0 - (min(priority, 9999) / 9999)
            override  = float(p.get("recommendation_score_override") or 0)
            s_override = min(override, 999) / 999
            ed_s = 0.6 * s_priority + 0.4 * s_override

            blended = round(alpha * ai_s + beta * ed_s, 6)
            p["_hybrid_score"] = blended
            p["_score_reason"] = "hybrid_ai"

        # Re-rank by blended score
        products.sort(key=lambda p: p.get("_hybrid_score", 0), reverse=True)
        return products


# ── Factory ───────────────────────────────────────────────────────────────────

def get_active_scorer() -> Scorer:
    """
    Return the scorer configured via RECOMMENDATION_SCORER env var.

    Values
    ------
    rule_based      (default) — editorial priority from DB (safe at 0% coverage)
    personalization           — diversity + session-history re-ranking
    hybrid_ai                 — blend ai_score + editorial (≥50% coverage recommended)
    """
    scorer_name = os.getenv("RECOMMENDATION_SCORER", "rule_based").strip().lower()
    if scorer_name == "personalization":
        return PersonalizationScorer()
    if scorer_name == "hybrid_ai":
        return HybridAIScorer()
    if scorer_name != "rule_based":
        logger.warning(
            "Unknown RECOMMENDATION_SCORER=%r — falling back to rule_based.",
            scorer_name,
        )
    return RuleBasedScorer()


# ── ScoringPipeline ───────────────────────────────────────────────────────────

class ScoringPipeline:
    """
    Orchestrates: context → scorer → result enrichment.

    Usage in routers:

        context = RecommendationContext.from_params(user_id=user_id)
        result  = get_best_selling(db, ...)
        return  ScoringPipeline().apply(result, context)

    The pipeline:
      1. Runs the active scorer on result["products"].
      2. Strips internal _score_reason / _scorer keys from the final
         response (they're for logging, not API consumers).
      3. Adds a "scoring" metadata block to the envelope.
    """

    def __init__(self, scorer: Scorer | None = None) -> None:
        self.scorer = scorer or get_active_scorer()

    def apply(
        self,
        result:  dict,
        context: RecommendationContext,
    ) -> dict:
        """
        Apply scoring to a recommendation result envelope.

        If result["found"] is False the pipeline is a no-op — empty results
        don't need to be ranked.
        """
        if not result.get("found"):
            return result

        # ── Run scorer ────────────────────────────────────────────────────────
        scored_products = self.scorer.score(
            list(result.get("products", [])),
            context,
        )

        # ── Extract internal metadata before stripping ─────────────────────
        score_reasons = [
            p.pop("_score_reason", None) for p in scored_products
        ]
        for p in scored_products:
            p.pop("_hybrid_score", None)   # strip HybridAIScorer internal key

        # ── Build scoring metadata block ──────────────────────────────────
        scoring_meta: dict = {
            "scorer": self.scorer.name,
            "personalized": context.is_personalized,
        }

        # Include user context only when there's a known user
        if context.is_personalized:
            scoring_meta["context"] = {
                "user_id":               context.user_id,
                "preferred_price_tier":  context.preferred_price_tier,
                "preferred_brand_family":context.preferred_brand_family,
                "preferred_skin_type":   context.preferred_skin_type,
                "has_session_history":   context.has_session_history,
            }

        # Log diversity/exclusion stats for monitoring
        original_count = result.get("total_found", len(result.get("products", [])))
        final_count    = len(scored_products)
        if final_count < original_count:
            logger.debug(
                "ScoringPipeline: %d → %d products after diversity/cart exclusion "
                "(user_id=%s)",
                original_count, final_count, context.user_id,
            )
            scoring_meta["excluded_from_cart_or_history"] = original_count - final_count

        result["products"] = scored_products
        result["returned"] = final_count
        result["scoring"]  = scoring_meta

        return result
