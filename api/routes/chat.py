"""
api/routes/chat.py
==================
Chat interface endpoints:
  GET  /                   — serve the embedded chat UI
  GET  /history/{user_id}  — load chat history
  DELETE /history/{user_id}— clear chat history
  POST /reply              — main chatbot turn
  GET  /conversations      — admin conversation list
  POST /convert-image      — HEIC/image → JPEG data URL
"""
from __future__ import annotations

import base64
import logging
import re

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ai.orchestrator import ChatOrchestrator
from ai.prompt_manager import (
    FIXED_GOODBYE_AR,
    FIXED_GOODBYE_EN,
    FIXED_WELCOME_AR,
    FIXED_WELCOME_EN,
)
from core.image_utils import (
    HEIC_IMAGE_MIMES,
    SUPPORTED_IMAGE_MIMES,
    looks_like_heif,
    make_db_thumbnail,
    normalize_image_for_openai,
)
from database import get_db
from models import ChatHistory
from pydantic import BaseModel
from services.chat_service import get_conversations, get_history, save_message
from services.lead_service import save_lead

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

# ── Greeting / farewell word sets (module-level to avoid recreation per request)

_GREETING_WORDS: frozenset[str] = frozenset({
    "hello", "hi", "hey", "hii", "hiii", "salam", "salaam",
    "مرحبا",
    "أهلا",
    "أهلاً",
    "اهلا",
    "اهلاً",
    "هلا",
    "هلو",
    "হ্যালো",
    "হেলো",
    "হাই",
    "নমস্কার",
    "সালাম",
})

_FAREWELL_WORDS: frozenset[str] = frozenset({
    "bye", "goodbye", "good bye", "see you", "take care",
    "وداعً",
    "وداعا",
    "مع السلامة",
    "شكرًا",
    "شكرا",
    "آলবিদা",
    "বিদায়",
    "ধন্যবাদ",
})


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    user_id:   str
    message:   str
    image_url: str | None = None


class ChatResponse(BaseModel):
    reply:                str
    image_url:            str | None = None
    products:             list | None = None
    user_message_id:      int | None = None
    assistant_message_id: int | None = None


class ConvertImageResponse(BaseModel):
    data_url:     str
    original_mime: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

import os as _os
_STATIC_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "static")


@router.get("/", include_in_schema=False)
def chat_ui():
    return FileResponse(_os.path.join(_STATIC_DIR, "index.html"))


@router.get("/history/{user_id}")
def get_chat_history(user_id: str, db: Session = Depends(get_db)):
    return get_history(user_id, db)


@router.delete("/history/{user_id}")
def delete_chat_history(user_id: str, db: Session = Depends(get_db)):
    deleted = db.query(ChatHistory).filter(ChatHistory.user_id == user_id).delete()
    db.commit()
    return {"deleted": deleted}


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db)):
    return get_conversations(db)


@router.post("/reply", response_model=ChatResponse)
def generate_reply(data: ChatRequest, request: Request, db: Session = Depends(get_db)):
    orchestrator: ChatOrchestrator = request.app.state.orchestrator

    history    = get_history(data.user_id, db)
    msg_clean  = data.message.strip().lower().rstrip("!.,؟?")
    has_arabic = any("؀" <= c <= "ۿ" for c in data.message)

    # Intercept pure greetings / farewells — bypass AI for branded responses
    if msg_clean in _GREETING_WORDS:
        reply = FIXED_WELCOME_AR if has_arabic else FIXED_WELCOME_EN
        u_id  = save_message(data.user_id, "user",      data.message, db)
        a_id  = save_message(data.user_id, "assistant", reply,        db)
        return ChatResponse(reply=reply, user_message_id=u_id, assistant_message_id=a_id)

    if msg_clean in _FAREWELL_WORDS:
        reply = FIXED_GOODBYE_AR if has_arabic else FIXED_GOODBYE_EN
        u_id  = save_message(data.user_id, "user",      data.message, db)
        a_id  = save_message(data.user_id, "assistant", reply,        db)
        return ChatResponse(reply=reply, user_message_id=u_id, assistant_message_id=a_id)

    # Normalize image + build storage thumbnail
    image_for_ai:      str | None = None
    image_for_history: str | None = None

    if data.image_url:
        image_for_ai = normalize_image_for_openai(data.image_url)
        try:
            m = re.match(r"data:(.*?);base64,(.*)$", image_for_ai, re.DOTALL)
            if m:
                img_bytes = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
                image_for_history = make_db_thumbnail(img_bytes)
        except Exception:
            pass

    user_msg_id = save_message(
        data.user_id, "user", data.message, db,
        metadata={"image_url": image_for_history} if image_for_history else None,
    )

    result = orchestrator.run(
        user_id        = data.user_id,
        user_message   = data.message,
        history        = history,
        image_data_url = image_for_ai,
    )

    if result.products:
        save_lead(data.user_id, result.products)

    assistant_msg_id = save_message(
        data.user_id, "assistant", result.reply, db,
        metadata={
            "products":  result.products or None,
            "image_url": result.image_url,
        },
    )

    return ChatResponse(
        reply                = result.reply,
        image_url            = result.image_url,
        products             = result.products or None,
        user_message_id      = user_msg_id,
        assistant_message_id = assistant_msg_id,
    )


@router.post("/convert-image", response_model=ConvertImageResponse)
async def convert_image(file: UploadFile = File(...)):
    """Accept any image file (including HEIC/HEIF) and return a JPEG data URL."""
    content = await file.read()
    mime    = (file.content_type or "").lower()

    if mime in SUPPORTED_IMAGE_MIMES:
        b64 = base64.b64encode(content).decode()
        return ConvertImageResponse(
            data_url=f"data:{mime};base64,{b64}",
            original_mime=mime,
        )

    from fastapi import HTTPException
    from core.image_utils import _pil_to_jpeg_data_url

    is_heic = mime in HEIC_IMAGE_MIMES or looks_like_heif(content)
    try:
        data_url = _pil_to_jpeg_data_url(content)
        return ConvertImageResponse(data_url=data_url, original_mime=mime)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not convert image: {exc}. Please upload JPG, PNG, WEBP, or HEIC.",
        )
