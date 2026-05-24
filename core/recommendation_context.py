"""
core/recommendation_context.py
===============================
RecommendationContext — carries all signals known at request time.

This is the single object that flows from the router through the scoring
pipeline and (eventually) into personalization models.

Rules
-----
- Immutable after construction (frozen dataclass).
- All fields are optional; missing = "we don't know yet".
- No DB access here — the router populates it from query params / headers /
  session; the service layer uses it for post-ranking.

Fields
------
user_id              str | None    Caller-provided user identifier.
                                   None → anonymous / cold-start session.

session_id           str | None    Browser/device session token.
                                   Useful for multi-turn session affinity.

preferred_skin_type  str | None    Stated or inferred skin type.
                                   e.g. "oily", "dry", "sensitive"

preferred_price_tier str | None    Budget | Mid | Premium | Luxury
                                   Set from user profile or conversation.

preferred_brand_family str | None  e.g. "Italian Niche", "French Designer"

viewed_barcodes      list[str]     Products viewed in this session.
                                   Empty list = no history.

cart_barcodes        list[str]     Products currently in cart.
                                   Used to avoid recommending duplicates.

locale               str           "en" | "ar" — controls response language
                                   hints (not translation, just metadata).

is_bot               bool          True when the call comes from the AI
                                   pipeline (chat reply), not a human browser.
                                   Scorers may deprioritize novelty for bots.

Evolution path
--------------
Phase 1 (now): context is built but only user_id is used for response metadata.
Phase 2:       preferred_* fields power filter pre-selection in queries.
Phase 3:       viewed/cart barcodes feed collaborative-filtering re-ranking.
Phase 4:       full embedding-based personalization using a vector store.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecommendationContext:
    # ── Identity ──────────────────────────────────────────────────────────────
    user_id:   str | None = None
    session_id: str | None = None

    # ── Stated / inferred preferences ────────────────────────────────────────
    preferred_skin_type:    str | None = None
    preferred_price_tier:   str | None = None
    preferred_brand_family: str | None = None

    # ── Session signals ───────────────────────────────────────────────────────
    viewed_barcodes: tuple[str, ...] = field(default_factory=tuple)
    cart_barcodes:   tuple[str, ...] = field(default_factory=tuple)

    # ── Metadata ──────────────────────────────────────────────────────────────
    locale: str  = "en"
    is_bot: bool = False

    # ── Factory helpers ───────────────────────────────────────────────────────

    @classmethod
    def anonymous(cls) -> "RecommendationContext":
        """Cold-start context — no user signals at all."""
        return cls()

    @classmethod
    def from_params(
        cls,
        *,
        user_id:                str | None = None,
        session_id:             str | None = None,
        preferred_skin_type:    str | None = None,
        preferred_price_tier:   str | None = None,
        preferred_brand_family: str | None = None,
        viewed_barcodes:        list[str]  | None = None,
        cart_barcodes:          list[str]  | None = None,
        locale:                 str        = "en",
        is_bot:                 bool       = False,
    ) -> "RecommendationContext":
        """Build from router query params — converts lists to tuples for hashability."""
        return cls(
            user_id                 = user_id,
            session_id              = session_id,
            preferred_skin_type     = preferred_skin_type,
            preferred_price_tier    = preferred_price_tier,
            preferred_brand_family  = preferred_brand_family,
            viewed_barcodes         = tuple(viewed_barcodes or []),
            cart_barcodes           = tuple(cart_barcodes or []),
            locale                  = locale,
            is_bot                  = is_bot,
        )

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def is_personalized(self) -> bool:
        """True if we have enough signal to attempt personalization."""
        return self.user_id is not None

    @property
    def has_session_history(self) -> bool:
        """True if the user has viewed or carted items this session."""
        return bool(self.viewed_barcodes or self.cart_barcodes)
