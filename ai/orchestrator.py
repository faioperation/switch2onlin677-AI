"""
ai/orchestrator.py
==================
AsyncChatOrchestrator: production-grade async AI conversation pipeline.

WHAT CHANGED FROM V1
---------------------
V1 problems:
  1. Synchronous OpenAI client → blocked the event loop on every GPT call
  2. Full company knowledge injected every turn → 40–50k tokens/request
  3. GPT-4o used for ALL turns → ~15× more expensive than necessary
  4. No token budget enforcement → silent context overflow
  5. No retry on transient failures → single 429 = user sees error
  6. No circuit breaker → OpenAI outage cascades to all requests

V2 solutions:
  1. AsyncOpenAI client → non-blocking, proper async/await throughout
  2. RAG retrieval (top-5 chunks) → ~2k tokens for knowledge vs 40–50k
  3. Model router → gpt-4o-mini for simple turns, gpt-4o for complex
  4. Token budget manager → validates prompt fits before calling GPT
  5. Exponential backoff retry → 3 attempts on 429/500/timeout
  6. Circuit breaker → fails fast when OpenAI is consistently down

BACKWARD COMPATIBILITY
----------------------
  - ChatResult dataclass: UNCHANGED (same fields as V1)
  - The old sync ChatOrchestrator is still exported at the bottom for any
    tests or admin tools that import it directly. It delegates to
    AsyncChatOrchestrator via asyncio.run().
  - orchestrator.run() is now async. chat.py's /reply handler is updated
    to await it. No other callers exist.

RAG FALLBACK STRATEGY
---------------------
  If pgvector is unavailable (e.g. SQLite dev environment):
    → Falls back to full knowledge injection (V1 behaviour)
  If RAG returns empty chunks (no matching knowledge):
    → Base prompt only (no knowledge section added)
  The fallback is logged but transparent to the user.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from openai import AsyncOpenAI

from ai.message_builder import build_messages
from ai.model_router import MODEL_FULL, MODEL_MINI, RoutingDecision, log_routing, route_model
from ai.prompt_manager import build_base_system_prompt, build_full_system_prompt
from ai.token_budget import (
    HISTORY_BUDGET,
    trim_history_to_budget,
    truncate_to_token_limit,
    validate_prompt_budget,
)
from ai.tool_registry import TOOL_DEFINITIONS, execute_tool_with_db
from core.circuit_breaker import CircuitOpenError, openai_circuit_breaker

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_GPT_TEMPERATURE  = float(os.getenv("GPT_TEMPERATURE", "0.7"))
_GPT_MAX_TOKENS   = int(os.getenv("GPT_MAX_TOKENS",    "1000"))
_MAX_TOOL_LOOPS   = int(os.getenv("GPT_MAX_TOOL_LOOPS", "6"))
_TOOL_RESULT_LIMIT = 3000    # tokens — cap individual tool result size

# Retry configuration
_MAX_RETRIES     = int(os.getenv("GPT_MAX_RETRIES", "3"))
_RETRY_BASE_DELAY = 1.5      # seconds; doubles each attempt

# Tools that return product lists (for UI card extraction)
_PRODUCT_LIST_TOOLS = frozenset({
    "search_products",
    "get_recommendations",
    "get_best_selling",
    "get_new_arrivals",
    "get_featured_products",
    "get_similar_products",
})

_INVALID_PRICES = {"", "n/a", "na", "none", "null", "0", "0.0", "not available"}

# RAG context section header (matches what build_full_system_prompt injects)
_RAG_SECTION_HEADER = """

RELEVANT KNOWLEDGE:
Use the following retrieved information to answer company-specific questions.
Only cite facts that appear below — do not invent company details.

{chunks}

Rules:
- Answer company questions using ONLY this knowledge.
- If the user asks in Arabic, answer in Arabic.
- If the user asks in English, answer in English.
- If the knowledge does not contain the answer, say so honestly.
"""

# Graceful degradation message when circuit is open
_CIRCUIT_OPEN_REPLY_EN = (
    "I'm experiencing a temporary issue reaching my AI service. "
    "Please try again in a moment, or type 'human' to speak with our team."
)
_CIRCUIT_OPEN_REPLY_AR = (
    "أواجه مشكلة مؤقتة في الوصول إلى خدمة الذكاء الاصطناعي. "
    "يرجى المحاولة مرة أخرى بعد لحظة، أو اكتب 'موظف' للتحدث مع فريقنا."
)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ChatResult:
    reply:      str
    image_url:  str | None       = None
    products:   list[dict]       = field(default_factory=list)
    # Observability fields (not sent to frontend, used for logging/metrics)
    model_used: str              = MODEL_FULL
    tokens_in:  int              = 0
    tokens_out: int              = 0
    tool_loops: int              = 0
    rag_chunks: int              = 0


# ── Orchestrator ──────────────────────────────────────────────────────────────

class AsyncChatOrchestrator:
    """
    Async stateless orchestrator for a single chatbot conversation turn.

    The AsyncOpenAI client is injected so it can be swapped in tests.
    All user-specific state is passed per call (no instance state).
    """

    def __init__(self, openai_client: AsyncOpenAI) -> None:
        self._client = openai_client

    async def run(
        self,
        user_id:              str,
        user_message:         str,
        history:              list[dict],
        image_data_url:       str | None = None,
        db                    = None,
        conversation_summary: str | None = None,
        has_arabic:           bool = False,
    ) -> ChatResult:
        """
        Execute one chatbot turn (async).

        Parameters
        ----------
        user_id              : User identifier (for logging + diversity cache).
        user_message         : The current user message text.
        history              : Conversation history (already windowed — last N msgs).
        image_data_url       : Optional base64 image for multimodal analysis.
        db                   : SQLAlchemy session (needed for RAG retrieval).
        conversation_summary : Rolling summary from services.conversation_summary.
        has_arabic           : True if the message contains Arabic characters.
        """
        start_time = time.perf_counter()

        # ── Step 1: Determine model via routing ───────────────────────────────
        routing: RoutingDecision = route_model(
            message   = user_message,
            history   = history,
            has_image = bool(image_data_url),
        )
        log_routing(routing, user_id)
        model = routing.model

        # ── Step 2: Build system prompt (base + optional RAG chunks) ──────────
        system_prompt, rag_chunk_count = await self._build_system_prompt(
            user_message=user_message,
            db=db,
        )

        # ── Step 3: Trim history to token budget ──────────────────────────────
        windowed_history = trim_history_to_budget(history, HISTORY_BUDGET)

        # ── Step 4: Assemble messages array ───────────────────────────────────
        messages = build_messages(
            system_prompt        = system_prompt,
            history              = windowed_history,
            user_message         = user_message,
            image_data_url       = image_data_url,
            conversation_summary = conversation_summary,
        )

        # ── Step 5: Validate token budget ─────────────────────────────────────
        budget_report = validate_prompt_budget(messages)
        if budget_report["over_budget"]:
            # Emergency: trim history further until within budget
            messages = await self._emergency_trim(
                messages, windowed_history, system_prompt,
                conversation_summary, user_message, image_data_url,
            )

        # ── Step 6: Tool loop ─────────────────────────────────────────────────
        image_url:   str | None = None
        products:    list[dict] = []
        tokens_in  = 0
        tokens_out = 0
        loop_count = 0

        while loop_count < _MAX_TOOL_LOOPS:
            loop_count += 1
            try:
                response = await self._call_gpt_with_retry(messages, model)
            except CircuitOpenError:
                reply = _CIRCUIT_OPEN_REPLY_AR if has_arabic else _CIRCUIT_OPEN_REPLY_EN
                return ChatResult(
                    reply      = reply,
                    model_used = model,
                    tool_loops = loop_count,
                    rag_chunks = rag_chunk_count,
                )

            # Track token usage from response
            if response.usage:
                tokens_in  += response.usage.prompt_tokens     or 0
                tokens_out += response.usage.completion_tokens or 0

            ai_message = response.choices[0].message

            if not ai_message.tool_calls:
                elapsed_ms = round((time.perf_counter() - start_time) * 1000)
                logger.info(
                    "orchestrator_done user=%s model=%s loops=%d "
                    "tokens_in=%d tokens_out=%d rag_chunks=%d elapsed_ms=%d",
                    user_id, model, loop_count,
                    tokens_in, tokens_out, rag_chunk_count, elapsed_ms,
                )
                return ChatResult(
                    reply      = ai_message.content or "",
                    image_url  = image_url,
                    products   = products,
                    model_used = model,
                    tokens_in  = tokens_in,
                    tokens_out = tokens_out,
                    tool_loops = loop_count,
                    rag_chunks = rag_chunk_count,
                )

            # Append assistant turn (with tool_calls) to messages
            messages.append(ai_message)

            # Execute all tool calls in this turn
            for tool_call in ai_message.tool_calls:
                tool_name = tool_call.function.name
                try:
                    tool_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                tool_result_str = execute_tool_with_db(tool_name, tool_args, user_id, db)
                tool_result     = json.loads(tool_result_str)

                # Extract UI cards + compress the result before injecting back
                tool_result_str, image_url, new_products = self._extract_ui_data(
                    tool_name, tool_result, image_url,
                )
                # Compress tool result to avoid bloating the messages array
                tool_result_str = truncate_to_token_limit(tool_result_str, _TOOL_RESULT_LIMIT)

                if new_products:
                    products = new_products

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tool_call.id,
                    "content":      tool_result_str,
                })

        # Safety fallback after hitting loop cap
        logger.warning("tool_loop_limit_exceeded user=%s loops=%d", user_id, loop_count)
        try:
            response = await self._call_gpt_with_retry(messages, model)
        except CircuitOpenError:
            reply = _CIRCUIT_OPEN_REPLY_AR if has_arabic else _CIRCUIT_OPEN_REPLY_EN
            return ChatResult(reply=reply, model_used=model, tool_loops=loop_count)

        if response.usage:
            tokens_in  += response.usage.prompt_tokens     or 0
            tokens_out += response.usage.completion_tokens or 0

        return ChatResult(
            reply      = response.choices[0].message.content or "",
            image_url  = image_url,
            products   = products,
            model_used = model,
            tokens_in  = tokens_in,
            tokens_out = tokens_out,
            tool_loops = loop_count,
            rag_chunks = rag_chunk_count,
        )

    # ── System prompt builder ─────────────────────────────────────────────────

    async def _build_system_prompt(
        self,
        user_message: str,
        db,
    ) -> tuple[str, int]:
        """
        Build the system prompt using RAG if available, full injection otherwise.

        Returns (system_prompt_string, rag_chunk_count).
        rag_chunk_count = 0 means full-injection fallback was used.
        """
        # Try RAG path (requires pgvector + DB session)
        if db is not None:
            try:
                from ai.rag_service import _vector_available, retrieve_relevant_chunks
                if _vector_available:
                    base_prompt = build_base_system_prompt()
                    chunks      = retrieve_relevant_chunks(db, user_message)
                    if chunks:
                        rag_section = _RAG_SECTION_HEADER.format(chunks=chunks)
                        return base_prompt + rag_section, chunks.count("\n\n---\n\n") + 1
                    else:
                        # No relevant chunks — return base prompt only (no knowledge section)
                        return base_prompt, 0
            except Exception as exc:
                logger.warning("rag_retrieval_failed error=%s — falling back to full injection", exc)

        # Fallback: full knowledge injection (V1 behaviour)
        return build_full_system_prompt(), 0

    # ── GPT caller with retry + circuit breaker ───────────────────────────────

    async def _call_gpt_with_retry(self, messages: list, model: str):
        """
        Call the OpenAI Chat Completions API with:
          - Circuit breaker protection (fast-fail when OpenAI is down)
          - Exponential backoff retry (up to _MAX_RETRIES attempts)
          - Per-attempt timeout (30 seconds)

        Raises CircuitOpenError if the circuit is open.
        Raises the last exception if all retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        delay = _RETRY_BASE_DELAY

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await openai_circuit_breaker.call_async(
                    self._client.chat.completions.create,
                    model       = model,
                    messages    = messages,
                    tools       = TOOL_DEFINITIONS,
                    tool_choice = "auto",
                    temperature = _GPT_TEMPERATURE,
                    max_tokens  = _GPT_MAX_TOKENS,
                    timeout     = 30.0,
                )
                return response

            except CircuitOpenError:
                raise   # don't retry; circuit is open

            except Exception as exc:
                last_exc = exc
                exc_name = type(exc).__name__

                if attempt >= _MAX_RETRIES:
                    logger.error(
                        "gpt_all_retries_failed model=%s attempts=%d error=%s",
                        model, attempt, exc,
                    )
                    raise

                # Check if retryable (429, 5xx, timeout)
                is_429 = "429" in str(exc) or "rate_limit" in str(exc).lower()
                is_5xx = any(c in str(exc) for c in ("500", "502", "503"))
                is_timeout = "timeout" in str(exc).lower()

                if not (is_429 or is_5xx or is_timeout):
                    # Non-transient error (e.g. invalid API key) — don't retry
                    logger.error(
                        "gpt_non_retryable_error model=%s error=%s", model, exc
                    )
                    raise

                logger.warning(
                    "gpt_retry attempt=%d/%d model=%s error=%s sleeping=%.1fs",
                    attempt, _MAX_RETRIES, model, exc_name, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2   # exponential backoff: 1.5s → 3s → 6s

        raise last_exc  # unreachable but satisfies type checkers

    # ── UI data extractor ─────────────────────────────────────────────────────

    def _extract_ui_data(
        self,
        tool_name:         str,
        tool_result:       dict,
        current_image_url: str | None,
    ) -> tuple[str, str | None, list[dict]]:
        image_url = current_image_url
        ui_cards: list[dict] = []

        if tool_name in _PRODUCT_LIST_TOOLS and tool_result.get("products"):
            valid, ui_cards = self._filter_products(tool_result["products"])
            tool_result["products"]    = valid
            tool_result["total_found"] = len(valid)
            tool_result["returned"]    = len(valid)

            if valid and not image_url:
                image_url = valid[0].get("image_url")

        elif tool_name == "get_product_details":
            if not image_url:
                image_url = tool_result.get("image_url")

        return json.dumps(tool_result, ensure_ascii=False, default=str), image_url, ui_cards

    @staticmethod
    def _filter_products(products: list[dict]) -> tuple[list[dict], list[dict]]:
        valid:    list[dict] = []
        ui_cards: list[dict] = []

        for p in products:
            price_str = str(p.get("price", "")).strip().lower()
            if price_str in _INVALID_PRICES:
                continue
            valid.append(p)
            ui_cards.append({
                "id":          p.get("id", ""),
                "name":        p.get("name", ""),
                "price":       p.get("price", ""),
                "barcode":     p.get("id", ""),
                "description": p.get("description", ""),
                "image_url":   p.get("image_url", ""),
                "stock":       p.get("available_qty", 0),
            })

        return valid, ui_cards

    # ── Emergency token trimmer ───────────────────────────────────────────────

    async def _emergency_trim(
        self,
        messages:             list,
        windowed_history:     list[dict],
        system_prompt:        str,
        conversation_summary: str | None,
        user_message:         str,
        image_data_url:       str | None,
    ) -> list:
        """
        Progressively drop history messages until the prompt fits within budget.
        Last resort — should rarely trigger if normal trimming is working.
        """
        logger.warning(
            "emergency_token_trim triggered history_len=%d", len(windowed_history)
        )
        # Try progressively smaller history windows
        for keep in (10, 5, 2, 0):
            trimmed = windowed_history[-keep:] if keep else []
            msgs = build_messages(
                system_prompt        = system_prompt,
                history              = trimmed,
                user_message         = user_message,
                image_data_url       = image_data_url,
                conversation_summary = conversation_summary,
            )
            report = validate_prompt_budget(msgs)
            if not report["over_budget"]:
                logger.warning(
                    "emergency_trim_resolved kept=%d_messages tokens=%d",
                    keep, report["total_tokens"],
                )
                return msgs

        # Absolute fallback: system + user only
        return build_messages(
            system_prompt = system_prompt,
            history       = [],
            user_message  = user_message,
            image_data_url= image_data_url,
        )


# ── Backward-compatible sync wrapper ─────────────────────────────────────────
# Kept so that any existing sync callers (admin scripts, tests) continue to work.
# The /reply route handler is async and uses AsyncChatOrchestrator directly.

class ChatOrchestrator:
    """
    Sync wrapper around AsyncChatOrchestrator.
    Preserved for backward compatibility. New code should use AsyncChatOrchestrator.
    """

    def __init__(self, openai_client) -> None:
        # Accept both sync and async clients — wrap sync clients at call time
        self._async_client: Optional[AsyncOpenAI] = None
        self._raw_client = openai_client

    def _get_async_orchestrator(self) -> AsyncChatOrchestrator:
        if self._async_client is None:
            # Create an AsyncOpenAI from the sync client's API key
            api_key = getattr(self._raw_client, "api_key", None) or os.getenv("OPENAI_API_KEY")
            self._async_client = AsyncOpenAI(api_key=api_key)
        return AsyncChatOrchestrator(self._async_client)

    def run(self, user_id, user_message, history, image_data_url=None, **kwargs) -> ChatResult:
        """Sync entry point — runs the async orchestrator in a new event loop."""
        orch = self._get_async_orchestrator()
        return asyncio.run(
            orch.run(
                user_id        = user_id,
                user_message   = user_message,
                history        = history,
                image_data_url = image_data_url,
                **kwargs,
            )
        )
