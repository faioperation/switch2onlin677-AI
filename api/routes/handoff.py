"""
api/routes/handoff.py
=====================
Human-agent handoff management endpoints.

All endpoints are intended for the admin dashboard / agent panel.
No authentication middleware exists yet in this project (tracked as
# TODO(auth) throughout the codebase) — these endpoints are currently
public.  When JWT auth is added, restrict to staff/agent roles.

Endpoints
---------
GET  /handoff/pending              — queue of conversations awaiting an agent
GET  /handoff/stats                — aggregate analytics for the dashboard
POST /handoff/transfer             — manually trigger ai → pending_human
POST /handoff/assign-agent         — assign an agent (pending → human_handling)
POST /handoff/resolve              — mark a conversation resolved
POST /handoff/resume-ai            — re-enable AI (any state → ai_active)
POST /handoff/agent-message        — agent sends a message to the user
GET  /handoff/conversation/{user_id} — full conversation thread for an agent

Response envelope
-----------------
Success: {"success": true, "data": {...}}
Error:   {"detail": {"success": false, "error": {"code": "...", "message": "..."}}}
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import ConversationHandoff, HandoffStatus
from services.chat_service import get_history, save_message
from services.handoff_service import (
    assign_agent,
    get_or_create_handoff,
    get_pending,
    get_handoff_stats,
    resolve_conversation,
    resume_ai,
    trigger_transfer,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/handoff", tags=["Handoff"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _not_found(msg: str = "Conversation not found.") -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"success": False, "error": {"code": "NOT_FOUND", "message": msg}},
    )


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"success": False, "error": {"code": "BAD_REQUEST", "message": msg}},
    )


def _handoff_to_dict(row: ConversationHandoff) -> dict:
    return {
        "user_id":            row.user_id,
        "status":             row.status.value,
        "ai_disabled":        row.ai_disabled,
        "assigned_agent_id":  row.assigned_agent_id,
        "transfer_reason":    row.transfer_reason,
        "ai_confidence_score": float(row.ai_confidence_score) if row.ai_confidence_score else None,
        "transferred_at":     row.transferred_at.isoformat() if row.transferred_at else None,
        "resolved_at":        row.resolved_at.isoformat()    if row.resolved_at    else None,
        "created_at":         row.created_at.isoformat()     if row.created_at     else None,
    }


# ── Pydantic request schemas ───────────────────────────────────────────────────

class TransferRequest(BaseModel):
    user_id: str
    reason:  Optional[str] = "manual_admin_transfer"


class AssignAgentRequest(BaseModel):
    user_id:  str
    agent_id: str


class ResolveRequest(BaseModel):
    user_id: str


class ResumeAIRequest(BaseModel):
    user_id: str


class AgentMessageRequest(BaseModel):
    user_id:  str
    agent_id: str
    message:  str


# ── GET /handoff/pending ───────────────────────────────────────────────────────

@router.get("/pending")
def list_pending_conversations(db: Session = Depends(get_db)):
    """
    Return all conversations in pending_human or human_handling state,
    sorted by transfer time (oldest first — FIFO agent queue).

    Each entry includes the last user message and total message count so
    agents can triage without loading the full thread.
    """
    items = get_pending(db)
    return {
        "success": True,
        "data": {
            "conversations": items,
            "total":         len(items),
        },
    }


# ── GET /handoff/stats ─────────────────────────────────────────────────────────

@router.get("/stats")
def handoff_statistics(db: Session = Depends(get_db)):
    """
    Aggregate handoff analytics for the admin dashboard.

    Returns status distribution counts and the overall transfer rate
    (transferred conversations ÷ total users with chat history).
    """
    stats = get_handoff_stats(db)
    return {"success": True, "data": stats}


# ── GET /handoff/conversation/{user_id} ───────────────────────────────────────

@router.get("/conversation/{user_id}")
def get_conversation_thread(user_id: str, db: Session = Depends(get_db)):
    """
    Full conversation thread for a given user_id.

    Returns the handoff state + last 70 messages so the agent panel can
    display the entire context without a separate history call.
    """
    handoff = (
        db.query(ConversationHandoff)
        .filter(ConversationHandoff.user_id == user_id)
        .first()
    )
    history = get_history(user_id, db)
    return {
        "success": True,
        "data": {
            "handoff": _handoff_to_dict(handoff) if handoff else None,
            "history": history,
        },
    }


# ── POST /handoff/transfer ─────────────────────────────────────────────────────

@router.post("/transfer")
def manual_transfer(payload: TransferRequest, db: Session = Depends(get_db)):
    """
    Manually trigger a handoff for a user (admin override).

    Creates the handoff row if it doesn't exist, then transitions
    the conversation to pending_human regardless of current state.

    Typical use: admin notices a stuck conversation and forces escalation.
    """
    # Ensure row exists before transitioning
    get_or_create_handoff(payload.user_id, db)

    row = trigger_transfer(
        user_id    = payload.user_id,
        reason     = payload.reason or "manual_admin_transfer",
        confidence = 1.0,
        db         = db,
    )
    if row is None:
        raise _not_found()

    logger.info(
        "manual_transfer user=%s reason=%s",
        payload.user_id, payload.reason,
    )
    return {
        "success": True,
        "message": "Conversation transferred to human queue.",
        "data":    _handoff_to_dict(row),
    }


# ── POST /handoff/assign-agent ─────────────────────────────────────────────────

@router.post("/assign-agent")
def assign_conversation_to_agent(
    payload: AssignAgentRequest,
    db:      Session = Depends(get_db),
):
    """
    Assign a human agent to a pending conversation.

    Transitions: pending_human → human_handling.
    Once assigned the AI remains disabled and the agent owns all replies.
    """
    if not payload.agent_id.strip():
        raise _bad_request("agent_id must not be empty.")

    row = assign_agent(
        user_id  = payload.user_id,
        agent_id = payload.agent_id.strip(),
        db       = db,
    )
    if row is None:
        raise _not_found(f"No handoff record found for user '{payload.user_id}'.")

    logger.info(
        "agent_assignment user=%s agent=%s",
        payload.user_id, payload.agent_id,
    )
    return {
        "success": True,
        "message": f"Agent '{payload.agent_id}' assigned to conversation.",
        "data":    _handoff_to_dict(row),
    }


# ── POST /handoff/resolve ──────────────────────────────────────────────────────

@router.post("/resolve")
def resolve(payload: ResolveRequest, db: Session = Depends(get_db)):
    """
    Mark a conversation as resolved.

    The AI remains disabled after resolving; call /resume-ai to re-enable
    it.  Use this endpoint when the agent has completed the order/support
    interaction and no further human handling is needed.
    """
    row = resolve_conversation(payload.user_id, db)
    if row is None:
        raise _not_found(f"No handoff record found for user '{payload.user_id}'.")

    return {
        "success": True,
        "message": "Conversation marked as resolved.",
        "data":    _handoff_to_dict(row),
    }


# ── POST /handoff/resume-ai ────────────────────────────────────────────────────

@router.post("/resume-ai")
def resume_ai_mode(payload: ResumeAIRequest, db: Session = Depends(get_db)):
    """
    Re-enable the AI for a conversation (any state → ai_active).

    Clears all handoff fields so the next chat turn is processed by
    GPT normally.  Use after a resolved conversation where the customer
    has follow-up product questions.
    """
    row = resume_ai(payload.user_id, db)
    if row is None:
        # Row might not exist if the user never chatted — create it
        row = get_or_create_handoff(payload.user_id, db)

    return {
        "success": True,
        "message": "AI re-enabled for this conversation.",
        "data":    _handoff_to_dict(row),
    }


# ── POST /handoff/agent-message ────────────────────────────────────────────────

@router.post("/agent-message")
def send_agent_message(
    payload: AgentMessageRequest,
    db:      Session = Depends(get_db),
):
    """
    Send a message from a human agent to the user.

    The message is saved to ChatHistory as role='assistant' with
    metadata type='agent_message' so:
      - The chat frontend renders it in the assistant bubble.
      - If AI is ever resumed the message appears naturally in context.
      - The agent_id is preserved in metadata for audit purposes.

    The conversation must be in human_handling state; the endpoint
    rejects agent messages for ai_active conversations to prevent
    agents accidentally hijacking an AI-handled session.
    """
    if not payload.message.strip():
        raise _bad_request("message must not be empty.")

    handoff = (
        db.query(ConversationHandoff)
        .filter(ConversationHandoff.user_id == payload.user_id)
        .first()
    )

    if handoff is None or handoff.status not in (
        HandoffStatus.human_handling,
        HandoffStatus.pending_human,
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_STATE",
                    "message": (
                        "Agent messages can only be sent when the conversation "
                        "is in pending_human or human_handling state."
                    ),
                },
            },
        )

    msg_id = save_message(
        user_id  = payload.user_id,
        role     = "assistant",
        content  = payload.message.strip(),
        db       = db,
        metadata = {
            "type":     "agent_message",
            "agent_id": payload.agent_id,
        },
    )

    logger.info(
        "agent_message_sent user=%s agent=%s msg_id=%d",
        payload.user_id, payload.agent_id, msg_id,
    )
    return {
        "success":    True,
        "message":    "Agent message saved.",
        "message_id": msg_id,
    }
