"""
ai/orchestrator.py
==================
ChatOrchestrator: stateless AI conversation pipeline.

Extracted from main.py to isolate AI orchestration from HTTP routing.

Pipeline per turn:
  1. Build system prompt (base + knowledge injection)
  2. Construct messages array (history + user message + optional image)
  3. Call GPT with all 8 tool definitions
  4. Execute any requested tools (with recommendation diversity applied)
  5. Loop until GPT returns a plain text response
  6. Return ChatResult for the route handler to persist and respond with
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from ai.message_builder import build_messages
from ai.prompt_manager import build_full_system_prompt
from ai.tool_registry import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

_GPT_MODEL       = "gpt-4o"
_GPT_TEMPERATURE = 0.7
_GPT_MAX_TOKENS  = 1000
_MAX_TOOL_LOOPS  = 6   # safety cap on recursive tool call iterations

# Tools that return a list of products for the UI card renderer
_PRODUCT_LIST_TOOLS = frozenset({
    "search_products",
    "get_recommendations",
    "get_best_selling",
    "get_new_arrivals",
    "get_featured_products",
    "get_similar_products",
})

# Invalid price sentinel values to strip before rendering product cards
_INVALID_PRICES = {"", "n/a", "na", "none", "null", "0", "0.0", "not available"}


@dataclass
class ChatResult:
    reply:     str
    image_url: str | None       = None
    products:  list[dict]       = field(default_factory=list)


class ChatOrchestrator:
    """
    Stateless orchestrator for a single chatbot conversation turn.

    The OpenAI client is injected so it can be swapped in tests.
    All user-specific state is passed per call (no instance state).
    """

    def __init__(self, openai_client: OpenAI) -> None:
        self._client = openai_client

    def run(
        self,
        user_id:       str,
        user_message:  str,
        history:       list[dict],
        image_data_url: str | None = None,
    ) -> ChatResult:
        """
        Execute one chatbot turn.

        Returns a ChatResult with the assistant reply, optional image URL,
        and the list of product cards to render in the UI.
        """
        system_prompt = build_full_system_prompt()
        messages      = build_messages(system_prompt, history, user_message, image_data_url)

        image_url: str | None  = None
        products:  list[dict]  = []
        loop_count = 0

        while loop_count < _MAX_TOOL_LOOPS:
            loop_count += 1

            response   = self._call_gpt(messages)
            ai_message = response.choices[0].message

            if not ai_message.tool_calls:
                # GPT is done — final text response
                return ChatResult(
                    reply     = ai_message.content or "",
                    image_url = image_url,
                    products  = products,
                )

            # Append the assistant turn so GPT sees the full context next loop
            messages.append(ai_message)

            for tool_call in ai_message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                tool_result_str = execute_tool(tool_name, tool_args, user_id)
                tool_result     = json.loads(tool_result_str)

                # Extract product cards and image from the tool result
                tool_result_str, image_url, new_products = self._extract_ui_data(
                    tool_name, tool_result, image_url,
                )
                if new_products:
                    products = new_products

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tool_call.id,
                    "content":      tool_result_str,
                })

        # Safety fallback: loop limit exceeded — ask GPT to respond with what it has
        logger.warning("tool_loop_limit_exceeded user=%s loops=%d", user_id, loop_count)
        response = self._call_gpt(messages)
        return ChatResult(
            reply     = response.choices[0].message.content or "",
            image_url = image_url,
            products  = products,
        )

    def _call_gpt(self, messages: list[dict[str, Any]]):
        """Single OpenAI API call with consistent parameters."""
        return self._client.chat.completions.create(
            model       = _GPT_MODEL,
            messages    = messages,
            tools       = TOOL_DEFINITIONS,
            tool_choice = "auto",
            temperature = _GPT_TEMPERATURE,
            max_tokens  = _GPT_MAX_TOKENS,
        )

    def _extract_ui_data(
        self,
        tool_name:         str,
        tool_result:       dict,
        current_image_url: str | None,
    ) -> tuple[str, str | None, list[dict]]:
        """
        Extract UI product cards and the hero image from a tool result.

        Strips zero-price products before they reach GPT or the frontend.
        Returns (cleaned_json_str, image_url, ui_product_cards).
        """
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
        """
        Split products into (valid_for_gpt, ui_cards).
        Products with missing/zero prices are excluded from both.
        """
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
