"""
ai/message_builder.py
=====================
Constructs the messages list passed to the OpenAI API.
Handles history injection and multimodal (image) user messages.
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Any

logger = logging.getLogger(__name__)


def build_messages(
    system_prompt:   str,
    history:         list[dict],
    user_message:    str,
    image_data_url:  str | None = None,
) -> list[dict[str, Any]]:
    """
    Build the OpenAI messages array from history + current turn.

    Product metadata from prior assistant turns is injected as hidden
    text so GPT can reference barcodes/names from earlier in the session.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    for msg in history:
        content = msg["content"]
        # Inject product context from prior turns so GPT can reference them
        if msg.get("products"):
            meta_lines = [
                f"[METADATA: Name: {p.get('name')}, ItemCode: {p.get('id')}, Barcode: {p.get('barcode')}]"
                for p in msg["products"]
            ]
            content = content + "\n" + "\n".join(meta_lines)
        messages.append({"role": msg["role"], "content": content})

    if image_data_url:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text",      "text": user_message},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        })
    else:
        messages.append({"role": "user", "content": user_message})

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
