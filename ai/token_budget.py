"""
ai/token_budget.py
==================
Token counting, budget enforcement, and prompt overflow prevention.

WHY THIS EXISTS
---------------
The OpenAI GPT-4o context window is 128,000 tokens. Without explicit counting:
  - Full knowledge injection (40–50k tokens) + 70-message history (12–15k) +
    tool results (1–4k each × 6 loops) can silently overflow the context window.
  - Overflow causes OpenAI to return a context-length error (400) or silently
    truncate the oldest messages — leading to lost conversation context.
  - Without counting, we have no visibility into per-turn token spend.

STRATEGY
---------
  Total budget: 90,000 tokens per turn  (leaves 38k headroom for tools + response)
  ┌─────────────────────────────────────────────┐
  │  System prompt (base only)    ~4,000  tokens │  fixed
  │  RAG knowledge chunks         ≤3,000  tokens │  capped in rag_service.py
  │  Conversation history         ≤20,000 tokens │  sliding window applied here
  │  Current user message         ≤2,000  tokens │  validated here
  │  Tool results (per loop)      ≤2,000  tokens │  compressed in tool_registry.py
  │  GPT response (max_tokens)     1,000  tokens │  fixed in orchestrator
  └─────────────────────────────────────────────┘
  Hard ceiling (context limit)   128,000  tokens

ENCODING
---------
  We use gpt-4o's cl100k_base tokenizer via tiktoken.
  tiktoken is already required by rag_service.py — no new dependency.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# ── Budget constants ─────────────────────────────────────────────────────────

CONTEXT_WINDOW      = 128_000   # GPT-4o hard limit
RESPONSE_RESERVE    = 1_200     # max_tokens sent to OpenAI + small buffer
TOOL_LOOP_RESERVE   = 12_000    # headroom for up to 6 tool result injections
SYSTEM_PROMPT_SHARE = 7_500     # base prompt + RAG chunks combined ceiling
HISTORY_BUDGET      = 20_000    # tokens allowed for conversation history
USER_MSG_MAX        = 2_000     # truncate runaway user messages

# Total prompt budget: context window minus reserves
TOTAL_PROMPT_BUDGET = CONTEXT_WINDOW - RESPONSE_RESERVE - TOOL_LOOP_RESERVE
# = 128,000 - 1,200 - 12,000 = 114,800 tokens


# ── Tokeniser (cached — tiktoken loads a vocab file on first call) ───────────

@lru_cache(maxsize=1)
def _get_encoder():
    """Return the cl100k_base encoder (used by gpt-4o / gpt-4o-mini)."""
    try:
        import tiktoken
        return tiktoken.encoding_for_model("gpt-4o")
    except Exception as exc:
        logger.warning("tiktoken unavailable: %s — token counting disabled", exc)
        return None


def count_tokens(text: str) -> int:
    """Return the number of tokens in *text*. Returns 0 if tiktoken is unavailable."""
    enc = _get_encoder()
    if enc is None:
        return 0
    try:
        return len(enc.encode(text))
    except Exception:
        return 0


def count_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """
    Approximate token count for an OpenAI messages array.

    Uses the per-message overhead formula from OpenAI's cookbook:
      4 tokens overhead per message + role token + content tokens.
    Multimodal (image) content items count only the text parts.
    """
    enc = _get_encoder()
    if enc is None:
        return 0

    total = 3   # every messages array has 3-token reply primer
    for msg in messages:
        total += 4  # per-message overhead
        role = msg.get("role", "")
        total += len(enc.encode(role))

        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(enc.encode(content))
        elif isinstance(content, list):
            # Multimodal: count text items only
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(enc.encode(part.get("text", "")))
                # image_url parts use a flat 765-token fee (OpenAI's approximation)
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    total += 765
    return total


def truncate_to_token_limit(text: str, max_tokens: int) -> str:
    """
    Hard-truncate *text* to at most *max_tokens* tokens.

    Used to clamp runaway user messages or oversized tool results
    before they are injected into the messages array.
    """
    enc = _get_encoder()
    if enc is None:
        return text   # can't count — pass through unchanged

    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text

    truncated = enc.decode(tokens[:max_tokens])
    logger.debug("text_truncated original_tokens=%d max=%d", len(tokens), max_tokens)
    return truncated


# ── Sliding-window history trimmer ───────────────────────────────────────────

def trim_history_to_budget(
    history: list[dict],
    budget: int = HISTORY_BUDGET,
) -> list[dict]:
    """
    Return the most recent messages from *history* that fit within *budget* tokens.

    Strategy:
      1. Walk backward (newest → oldest).
      2. Accumulate messages until adding the next one would exceed *budget*.
      3. Always keep the most recent exchange (last user+assistant pair) even if
         it alone exceeds the budget — prevents a completely empty history.

    Preserves message order (oldest → newest) in the returned list.
    """
    enc = _get_encoder()
    if enc is None:
        return history   # no tiktoken — return as-is

    total    = 0
    selected = []

    for msg in reversed(history):
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        tokens = len(enc.encode(content)) + 4   # 4 = per-message overhead

        if total + tokens > budget and selected:
            # Budget exceeded and we have at least one message — stop
            break
        total += tokens
        selected.append(msg)

    selected.reverse()   # restore chronological order
    trimmed_count = len(history) - len(selected)
    if trimmed_count > 0:
        logger.info(
            "history_trimmed dropped=%d kept=%d tokens_used=%d budget=%d",
            trimmed_count, len(selected), total, budget,
        )
    return selected


# ── Full prompt budget validator ─────────────────────────────────────────────

def validate_prompt_budget(messages: list[dict[str, Any]]) -> dict:
    """
    Count tokens in the full messages array and return a budget report.

    Used for observability/logging — does NOT modify messages.
    Returns a dict with token counts and a warning if near the ceiling.
    """
    total = count_messages_tokens(messages)
    over_budget = total > TOTAL_PROMPT_BUDGET
    near_limit  = total > TOTAL_PROMPT_BUDGET * 0.85

    if over_budget:
        logger.error(
            "prompt_over_budget total_tokens=%d budget=%d overflow=%d",
            total, TOTAL_PROMPT_BUDGET, total - TOTAL_PROMPT_BUDGET,
        )
    elif near_limit:
        logger.warning(
            "prompt_near_limit total_tokens=%d budget=%d pct=%.0f%%",
            total, TOTAL_PROMPT_BUDGET, 100 * total / TOTAL_PROMPT_BUDGET,
        )

    return {
        "total_tokens":   total,
        "budget":         TOTAL_PROMPT_BUDGET,
        "over_budget":    over_budget,
        "near_limit":     near_limit,
        "remaining":      max(0, TOTAL_PROMPT_BUDGET - total),
    }
