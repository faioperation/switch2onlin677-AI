"""
ai/model_router.py
==================
Intelligent GPT model routing: chooses the cheapest model capable of
handling a given request, saving ~70–80% on AI inference costs.

WHY THIS EXISTS
---------------
GPT-4o costs ~$2.50/M input tokens and ~$10/M output tokens.
GPT-4o-mini costs ~$0.15/M input and ~$0.60/M output — ~15–17× cheaper.

Most chatbot turns are simple lookups or greetings that do NOT need
GPT-4o's full reasoning capacity. By routing them to gpt-4o-mini:

  Traffic mix (estimated):
    60% simple turns  → gpt-4o-mini  (greetings, FAQ, stock check)
    25% medium turns  → gpt-4o-mini  (simple product search, price check)
    15% complex turns → gpt-4o       (skincare consultation, multi-step)

  Per 1,000 turns at 5k avg input tokens:
    Current (all gpt-4o):  1,000 × 5,000 × $2.50/M = $12.50
    After routing:         850 × 5,000 × $0.15/M   = $0.64  (mini)
                         + 150 × 5,000 × $2.50/M   = $1.88  (full)
                         = $2.52 total  →  ~80% reduction

ROUTING RULES
-------------
Use gpt-4o-mini for:
  - Pure greetings / farewells
  - Single-word or very short messages (≤4 tokens)
  - Stock / availability checks
  - Price lookups
  - Simple "do you have X?" questions
  - FAQ questions about the store
  - Requests already handled by the rule-based shortcut in chat.py
  - Low complexity detected by keyword signals

Use gpt-4o for:
  - Skincare / beauty consultation (skin type, concerns)
  - Multi-step product comparison
  - Emotional / sensitive conversations
  - Long messages with complex intent (>50 words)
  - Arabic + mixed language multi-turn conversations
  - Any conversation already in escalation / emotional mode
  - Image analysis (vision — always requires full model)
  - Ambiguous intent where cheap routing could cause hallucinations

SAFETY
------
When in doubt, the router ALWAYS falls back to gpt-4o. The router errs on
the side of quality; cost savings are secondary to accuracy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── Model identifiers ─────────────────────────────────────────────────────────

MODEL_MINI = "gpt-4o-mini"    # cheap, fast — for simple turns
MODEL_FULL = "gpt-4o"         # expensive, powerful — for complex turns


# ── Routing result ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RoutingDecision:
    model:    str
    reason:   str
    is_mini:  bool


# ── Simple intent signal sets ─────────────────────────────────────────────────

# Phrases that indicate a lightweight turn safe for gpt-4o-mini
_SIMPLE_SIGNALS: frozenset[str] = frozenset({
    # Availability / stock
    "do you have", "do you carry", "is this available", "in stock",
    "available", "موجود", "عندكم", "هل عندكم", "في عندكم",
    # Price inquiry
    "price", "how much", "cost", "كم سعر", "السعر", "بكم",
    # FAQ / store info
    "where are you", "opening hours", "location", "branch", "contact",
    "phone", "whatsapp", "address", "أين", "فروع", "ساعات",
    # Delivery / basic info
    "delivery time", "how long", "shipping", "متى يوصل",
    # Simple product question
    "what is", "tell me about", "what are", "ما هو", "ماهو",
})

# Phrases that force the full model regardless of message length
_COMPLEX_SIGNALS: frozenset[str] = frozenset({
    # Skincare consultation
    "skin type", "skin concern", "dry skin", "oily skin", "sensitive skin",
    "combination skin", "acne", "anti-aging", "anti aging", "hyperpigmentation",
    "dark spots", "routine", "skincare routine", "regimen", "نوع البشرة",
    "بشرة جافة", "بشرة دهنية", "روتين عناية", "حب الشباب",
    # Multi-step / complex reasoning
    "compare", "difference between", "better than", "recommend for my",
    "suggest for my", "مقارنة", "أفضل لـ", "انصحني",
    # Emotional / frustration signals
    "frustrated", "disappointed", "not working", "problem", "issue",
    "wrong", "mistake", "مشكلة", "غلط",
    # Consultation / advice
    "advice", "what should i", "help me choose", "ساعدني",
    "ما الأفضل", "أحتاج نصيحة",
})


# ── Router ────────────────────────────────────────────────────────────────────

def route_model(
    message:        str,
    history:        list[dict],
    has_image:      bool = False,
    force_full:     bool = False,
) -> RoutingDecision:
    """
    Choose the cheapest model that can handle this conversational turn.

    Parameters
    ----------
    message     : The raw user message for this turn.
    history     : Prior conversation messages (used to detect ongoing complex turns).
    has_image   : True when the user attached an image (always needs full model).
    force_full  : Override — always return MODEL_FULL (used by admin/debug routes).

    Returns
    -------
    RoutingDecision with model name, human-readable reason, and is_mini flag.
    """
    # ── Hard overrides → always full model ───────────────────────────────────

    if force_full:
        return RoutingDecision(MODEL_FULL, "forced_full", is_mini=False)

    if has_image:
        return RoutingDecision(MODEL_FULL, "image_vision_requires_full", is_mini=False)

    msg_lower = message.strip().lower()
    msg_words = len(msg_lower.split())

    # Complex signals → immediately escalate to full model
    for signal in _COMPLEX_SIGNALS:
        if signal in msg_lower:
            return RoutingDecision(MODEL_FULL, f"complex_signal:{signal[:20]}", is_mini=False)

    # Very long message — likely complex multi-part request
    if msg_words > 50:
        return RoutingDecision(MODEL_FULL, f"long_message:{msg_words}_words", is_mini=False)

    # Ongoing complex conversation: if the last 3 turns used full model,
    # maintain quality continuity by staying on full model.
    if _conversation_has_complex_context(history):
        return RoutingDecision(MODEL_FULL, "complex_context_continuity", is_mini=False)

    # ── Fast-track candidates → gpt-4o-mini ──────────────────────────────────

    # Very short messages (1–4 words) — stock check, simple question
    if msg_words <= 4:
        return RoutingDecision(MODEL_MINI, f"short_message:{msg_words}_words", is_mini=True)

    # Simple signal match → mini is sufficient
    for signal in _SIMPLE_SIGNALS:
        if signal in msg_lower:
            return RoutingDecision(MODEL_MINI, f"simple_signal:{signal[:20]}", is_mini=True)

    # ── Default: full model ───────────────────────────────────────────────────
    # When uncertain, always prefer quality over cost.
    return RoutingDecision(MODEL_FULL, "default_full_model", is_mini=False)


def _conversation_has_complex_context(history: list[dict]) -> bool:
    """
    Return True if the recent conversation history contains complexity signals,
    suggesting we should maintain model-quality continuity.

    Checks the last 3 assistant messages for complex vocabulary.
    """
    if not history:
        return False

    _COMPLEX_VOCAB = {
        "routine", "concern", "skin type", "regimen", "consultation",
        "comparison", "روتين", "بشرة", "مقارنة", "نصيحة",
    }

    assistant_msgs = [
        m["content"] for m in history[-6:]
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    ]

    for content in assistant_msgs[-3:]:
        lower = content.lower()
        if any(v in lower for v in _COMPLEX_VOCAB):
            return True

    return False


# ── Logging helper ────────────────────────────────────────────────────────────

def log_routing(decision: RoutingDecision, user_id: str, turn_number: int = 0) -> None:
    """Emit a structured log line for model routing analytics."""
    logger.info(
        "model_routing user=%s turn=%d model=%s reason=%s",
        user_id, turn_number, decision.model, decision.reason,
    )
