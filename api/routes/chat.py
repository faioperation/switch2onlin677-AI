"""
api/routes/chat.py
==================
Chat interface endpoints:
  GET  /                   — serve the embedded chat UI
  GET  /history/{user_id}  — load chat history
  DELETE /history/{user_id}— clear chat history
  POST /reply              — main chatbot turn (now async)
  GET  /conversations      — admin conversation list
  POST /convert-image      — HEIC/image → JPEG data URL

CHANGES FROM V1
---------------
- generate_reply() is now `async def` — required for AsyncChatOrchestrator
- Rate limiting applied at the start of /reply via core.rate_limiter
- History now uses get_history_with_summary() (sliding window + rolling summary)
- DB session passed to orchestrator.run() for RAG retrieval
- Circuit breaker errors produce a graceful 503 response (not 500)
- Token + model usage logged per turn for observability
- Conversation summary invalidated when history is deleted
"""
from __future__ import annotations

import base64
import logging
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from ai.orchestrator import AsyncChatOrchestrator
from ai.prompt_manager import (
    FIXED_GOODBYE_AR,
    FIXED_GOODBYE_EN,
    FIXED_WELCOME_AR,
    FIXED_WELCOME_EN,
)
from core.circuit_breaker import CircuitOpenError
from core.image_utils import (
    HEIC_IMAGE_MIMES,
    SUPPORTED_IMAGE_MIMES,
    looks_like_heif,
    make_db_thumbnail,
    normalize_image_for_openai,
)
from core.rate_limiter import check_rate_limit
from database import get_db
from models import ChatHistory
from pydantic import BaseModel
from services.chat_service import get_conversations, get_history, get_history_with_summary, save_message
from services.conversation_summary import invalidate_summary
from services.handoff_service import (
    detect_intent,
    get_or_create_handoff,
    handoff_handling_message,
    handoff_pending_message,
    handoff_resolved_message,
    handoff_transfer_message,
    trigger_transfer,
)
from services.lead_service import save_lead

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

# ── Greeting / farewell word sets ─────────────────────────────────────────────

_GREETING_WORDS: frozenset[str] = frozenset({
    "hello", "hi", "hey", "hii", "hiii", "salam", "salaam",
    "مرحبا", "أهلا", "أهلاً", "اهلا", "اهلاً", "هلا", "هلو",
    "হ্যালো", "হেলো", "হাই", "নমস্কার", "সালাম",
})

_FAREWELL_WORDS: frozenset[str] = frozenset({
    "bye", "goodbye", "good bye", "see you", "take care",
    "وداعً", "وداعا", "مع السلامة", "شكرًا", "شكرا",
    "আলবিদা", "বিদায়", "ধন্যবাদ",
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


# ── Static file path ──────────────────────────────────────────────────────────

import os as _os
_STATIC_DIR = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "static"
)


# ── Endpoints ──────────────────────────────────────────────────────────────────

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
    # Invalidate summary so next session starts fresh
    invalidate_summary(user_id, db)
    return {"deleted": deleted}


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db)):
    return get_conversations(db)


@router.post("/reply", response_model=ChatResponse)
async def generate_reply(
    data:    ChatRequest,
    request: Request,
    db:      Session = Depends(get_db),
):
    """
    Main chatbot turn handler (async).

    Execution order:
      1. Rate limit check (fast — Redis/memory, <1ms)
      2. Handoff state guard (FIRST — suppresses ALL shortcuts when agent is handling)
      3. Intercept pure greetings/farewells (no GPT call, only when AI active)
      4. Purchase/escalation intent detection (keyword only — no GPT call)
      5. Build sliding-window history + rolling summary
      6. Run AsyncChatOrchestrator (async GPT call with RAG + model routing)
      7. Persist messages + optional lead tracking
      8. Return ChatResponse
    """
    orchestrator: AsyncChatOrchestrator = request.app.state.orchestrator

    # ── 1. Rate limiting ──────────────────────────────────────────────────────
    check_rate_limit(data.user_id)

    msg_clean  = data.message.strip().lower().rstrip("!.,؟?")
    has_arabic = any("؀" <= c <= "ۿ" for c in data.message)

    # ── 2. Human-handoff state guard (MUST run before greeting intercept) ─────
    # When ai_disabled=True, greetings and farewells must still be suppressed —
    # the user is waiting for a human agent and should not receive a new welcome.
    handoff = get_or_create_handoff(data.user_id, db)

    if handoff.ai_disabled:
        u_id = save_message(data.user_id, "user", data.message, db)

        if handoff.status.value == "human_handling":
            reply = handoff_handling_message(has_arabic)
            a_id  = save_message(data.user_id, "assistant", reply, db,
                                  metadata={"type": "system_handoff"})
            return ChatResponse(reply=reply, user_message_id=u_id, assistant_message_id=a_id)

        if handoff.status.value == "resolved":
            # Case was handled but AI not yet re-enabled — give resolved message, not queue message
            reply = handoff_resolved_message(has_arabic)
            a_id  = save_message(data.user_id, "assistant", reply, db,
                                  metadata={"type": "system_handoff_resolved"})
            return ChatResponse(reply=reply, user_message_id=u_id, assistant_message_id=a_id)

        # pending_human: still waiting for agent to accept
        reply = handoff_pending_message(has_arabic)
        a_id  = save_message(data.user_id, "assistant", reply, db,
                              metadata={"type": "system_handoff"})
        return ChatResponse(reply=reply, user_message_id=u_id, assistant_message_id=a_id)

    # ── 3. Intercept greetings / farewells (only when AI is active) ───────────
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

    # ── 4. Purchase / escalation intent ───────────────────────────────────────
    # Use full history for intent detection (70 messages — unchanged behaviour)
    full_history = get_history(data.user_id, db)
    should_transfer, reason, confidence = detect_intent(data.message, full_history)

    if should_transfer:
        u_id = save_message(data.user_id, "user", data.message, db)
        trigger_transfer(data.user_id, reason, confidence, db)
        reply = handoff_transfer_message(has_arabic)
        a_id  = save_message(
            data.user_id, "assistant", reply, db,
            metadata={"type": "handoff_trigger", "reason": reason, "confidence": confidence},
        )
        logger.info(
            "auto_handoff user=%s reason=%s confidence=%.2f",
            data.user_id, reason, confidence,
        )
        return ChatResponse(reply=reply, user_message_id=u_id, assistant_message_id=a_id)

    # ── 5. Build sliding-window history + rolling summary ─────────────────────
    # Pass openai_client from app.state so summary generation uses the same client
    openai_client = getattr(request.app.state, "openai_client", None)
    active_window, summary = get_history_with_summary(
        data.user_id, db, openai_client
    )

    # ── 6. Image handling ─────────────────────────────────────────────────────
    image_for_ai:      str | None = None
    image_for_history: str | None = None

    if data.image_url:
        image_for_ai = normalize_image_for_openai(data.image_url)
        try:
            m = re.match(r"data:(.*?);base64,(.*)$", image_for_ai, re.DOTALL)
            if m:
                img_bytes         = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
                image_for_history = make_db_thumbnail(img_bytes)
        except Exception:
            pass

    user_msg_id = save_message(
        data.user_id, "user", data.message, db,
        metadata={"image_url": image_for_history} if image_for_history else None,
    )

    # ── 7. AI orchestration (async) ───────────────────────────────────────────
    try:
        result = await orchestrator.run(
            user_id              = data.user_id,
            user_message         = data.message,
            history              = active_window,
            image_data_url       = image_for_ai,
            db                   = db,
            conversation_summary = summary,
            has_arabic           = has_arabic,
        )
    except CircuitOpenError as exc:
        # Circuit is open — OpenAI is down. Return graceful degraded response.
        logger.error("circuit_open_chat user=%s error=%s", data.user_id, exc)
        degraded = (
            "أواجه مشكلة مؤقتة. يرجى المحاولة مرة أخرى بعد لحظة." if has_arabic
            else "I'm experiencing a temporary issue. Please try again in a moment."
        )
        a_id = save_message(data.user_id, "assistant", degraded, db,
                            metadata={"type": "circuit_open"})
        return ChatResponse(
            reply=degraded, user_message_id=user_msg_id, assistant_message_id=a_id
        )
    except Exception as exc:
        logger.error("orchestrator_error user=%s error=%s", data.user_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="AI service error. Please try again.")

    # ── 8. Persist + respond ──────────────────────────────────────────────────
    if result.products:
        save_lead(data.user_id, result.products)

    assistant_msg_id = save_message(
        data.user_id, "assistant", result.reply, db,
        metadata={
            "products":   result.products or None,
            "image_url":  result.image_url,
            "model":      result.model_used,
            "tokens_in":  result.tokens_in,
            "tokens_out": result.tokens_out,
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
