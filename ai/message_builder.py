"""
ai/message_builder.py
=====================
Constructs the messages list passed to the OpenAI API.

OPTIMIZATIONS IN THIS VERSION
------------------------------
1. Summary injection: if the orchestrator supplies a conversation summary
   (from services.conversation_summary), it is injected as a system-role
   context note BEFORE the history window. This gives GPT awareness of the
   full session without sending every old message.

2. Token trimming: the user message is hard-capped at USER_MSG_MAX tokens
   before being injected, preventing runaway prompts from user copy-pastes.

3. Metadata compaction: product metadata injected into prior assistant turns
   is now compacted to barcode+name only (removed ItemCode duplication).

BACKWARD COMPATIBILITY
----------------------
build_messages() signature is unchanged — new parameters are optional keyword
arguments with safe defaults.
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Any, Optional

from ai.token_budget import USER_MSG_MAX, truncate_to_token_limit

logger = logging.getLogger(__name__)

# Injected before history window when a rolling summary exists
_SUMMARY_HEADER = (
    "\n[CONVERSATION CONTEXT — earlier session summary]\n"
    "{summary}\n"
    "[END CONTEXT — current conversation continues below]\n"
)


def build_messages(
    system_prompt:    str,
    history:          list[dict],
    user_message:     str,
    image_data_url:   str | None = None,
    *,
    conversation_summary: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Build the OpenAI messages array from history + current turn.

    Parameters
    ----------
    system_prompt        : The base system prompt (base only when using RAG;
                           full prompt when RAG is unavailable).
    history              : Active window of conversation messages (already
                           trimmed by the orchestrator's sliding window).
    user_message         : The current user input (token-capped before injection).
    image_data_url       : Optional base64 image data URL for multimodal turns.
    conversation_summary : Optional summary of older messages outside the window.
                           Injected as a system context note before the window.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    # Inject rolling summary before history window
    if conversation_summary:
        summary_note = _SUMMARY_HEADER.format(summary=conversation_summary.strip())
        messages.append({"role": "system", "content": summary_note})

    for msg in history:
        content = msg["content"]
        if not isinstance(content, str):
            content = str(content)

        # Compact product metadata: barcode + name only (saves ~30 tokens/message)
        if msg.get("products"):
            meta_lines = [
                f"[Product: {p.get('name', '')} | Barcode: {p.get('id') or p.get('barcode', '')}]"
                for p in msg["products"][:8]   # cap at 8 products in metadata
            ]
            content = content + "\n" + "\n".join(meta_lines)

        messages.append({"role": msg["role"], "content": content})

    # Cap user message length before injection
    safe_user_msg = truncate_to_token_limit(user_message, USER_MSG_MAX)
    if len(safe_user_msg) < len(user_message):
        logger.warning(
            "user_message_truncated original_len=%d capped_at=%d_tokens",
            len(user_message), USER_MSG_MAX,
        )

    if image_data_url:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text",      "text": safe_user_msg},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        })
    else:
        messages.append({"role": "user", "content": safe_user_msg})

    return messages


def make_thumbnail(image_bytes: bytes, max_size: int = 300) -> str | None:
    """
    Compress an image into a small JPEG data-URL suitable for DB storage.
    Returns None if the image cannot be processed.
    """
    try:
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=75)
        return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception as exc:
        logger.debug("Thumbnail generation failed: %s", exc)
        return None
