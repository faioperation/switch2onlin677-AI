"""
services/chat_service.py
========================
Chat history persistence and retrieval.
Extracted from main.py to give the chat router a clean service layer.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from models import ChatHistory

logger = logging.getLogger(__name__)

MAX_HISTORY: int = 70   # DB fetch limit (unchanged — all history is available)
ACTIVE_WINDOW: int = 20  # messages injected verbatim into the prompt (optimized)


def get_history(user_id: str, db: Session) -> list[dict]:
    """Return the last MAX_HISTORY messages for *user_id*, oldest first."""
    rows = (
        db.query(ChatHistory)
        .filter(ChatHistory.user_id == user_id)
        .order_by(ChatHistory.created_at.desc())
        .limit(MAX_HISTORY)
        .all()
    )
    rows.reverse()

    history: list[dict] = []
    for r in rows:
        item: dict = {"role": r.role, "content": r.content}
        if r.metadata_json:
            try:
                extra = json.loads(r.metadata_json)
                if extra.get("products"):
                    item["products"] = extra["products"]
                if extra.get("image_url"):
                    item["image_url"] = extra["image_url"]
                if extra.get("order_link"):
                    item["order_link"] = extra["order_link"]
            except Exception:
                pass
        history.append(item)
    return history


def save_message(
    user_id:  str,
    role:     str,
    content:  str,
    db:       Session,
    metadata: Optional[dict] = None,
) -> int:
    """Persist a single chat message and return its auto-generated ID."""
    record = ChatHistory(user_id=user_id, role=role, content=content)
    if metadata:
        record.metadata_json = json.dumps(metadata, ensure_ascii=False)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record.id


def get_history_with_summary(
    user_id:       str,
    db:            Session,
    openai_client  = None,
) -> tuple[list[dict], str | None]:
    """
    Return (active_window, summary) optimized for prompt injection.

    - active_window : Last ACTIVE_WINDOW messages (verbatim for GPT).
    - summary       : Compact summary of older messages, or None.

    This is the preferred function for the chat orchestrator.
    get_history() is unchanged and still used by the /history/{user_id} endpoint.
    """
    full_history = get_history(user_id, db)

    if len(full_history) <= ACTIVE_WINDOW:
        return full_history, None

    from services.conversation_summary import get_windowed_history
    return get_windowed_history(user_id, full_history, db, openai_client)


def get_conversations(db: Session) -> list[dict]:
    """Return one summary entry per user, sorted by most recent activity."""
    from sqlalchemy import func, and_

    subq = (
        db.query(
            ChatHistory.user_id,
            func.min(ChatHistory.created_at).label("first_time"),
        )
        .filter(ChatHistory.role == "user")
        .group_by(ChatHistory.user_id)
        .subquery()
    )
    first_msgs = (
        db.query(ChatHistory)
        .join(
            subq,
            and_(
                ChatHistory.user_id == subq.c.user_id,
                ChatHistory.created_at == subq.c.first_time,
            ),
        )
        .all()
    )

    latest_times = (
        db.query(
            ChatHistory.user_id,
            func.max(ChatHistory.created_at).label("last_time"),
        )
        .group_by(ChatHistory.user_id)
        .all()
    )
    time_map = {t.user_id: t.last_time for t in latest_times}

    conversations = [
        {
            "user_id":      msg.user_id,
            "title":        msg.content[:50],
            "last_updated": time_map.get(msg.user_id, msg.created_at).isoformat(),
        }
        for msg in first_msgs
    ]
    conversations.sort(key=lambda x: x["last_updated"], reverse=True)
    return conversations
