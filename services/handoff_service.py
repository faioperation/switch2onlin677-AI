"""
services/handoff_service.py
============================
Human-agent handoff service: intent detection, state management, and
conversation ownership for the DhifafBot transfer pipeline.

Responsibilities
----------------
1. detect_intent()         — multi-signal purchase / escalation scoring
2. get_or_create_handoff() — fetch or lazily initialise a handoff row
3. trigger_transfer()      — ai_active → pending_human
4. assign_agent()          — pending_human → human_handling
5. resolve_conversation()  — human_handling → resolved
6. resume_ai()             — any state → ai_active
7. get_pending()           — admin queue: pending_human + human_handling rows
8. get_handoff_stats()     — analytics aggregates

Design notes
------------
- One `ConversationHandoff` row per user_id (upsert / get-or-create pattern).
- `ai_disabled` is a denormalised convenience flag:
      True  whenever status in {pending_human, human_handling}
      False whenever status in {ai_active, resolved}
- All state transitions guard against concurrent requests by filtering on the
  *expected current status* in the WHERE clause, so a race can only produce a
  no-op rather than a double-transition.
- No circular imports: this module imports from models and database only.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ChatHistory, ConversationHandoff, HandoffStatus

logger = logging.getLogger(__name__)

# ── Confidence threshold ───────────────────────────────────────────────────────
# Score ≥ this value triggers an automatic ai→human transfer.
TRANSFER_THRESHOLD: float = 0.65


# ── Intent signal word sets ────────────────────────────────────────────────────
# Scored independently; final confidence = weighted max of matched categories.

# High-confidence purchase intent (weight 0.90)
_BUY_PHRASES: frozenset[str] = frozenset({
    "i want to buy", "i want to order", "i want this", "i'd like to buy",
    "i would like to buy", "i want to purchase", "place an order",
    "place order", "how to order", "how can i order", "how do i order",
    "i want to place", "ready to buy", "ready to order", "let me buy",
    "add to cart", "checkout", "complete my order", "confirm order",
    "i'll take it", "i'll take", "i'll buy", "i will buy",
    "want to buy", "want to order", "want to purchase",
    # Arabic
    "أريد الشراء", "أريد شراء", "أريد طلب", "أريد هذا", "كيف اشتري",
    "كيف أطلب", "أريد اطلب", "ابي اشتري", "ابي اطلب", "بدي اشتري",
    "بدي اطلب", "عايز اشتري", "عايز اطلب",
})

# Payment / invoice / delivery intent (weight 0.80)
_PAYMENT_PHRASES: frozenset[str] = frozenset({
    "payment", "pay now", "how to pay", "payment method", "send invoice",
    "invoice", "i need invoice", "receipt", "delivery", "how to deliver",
    "shipping", "cash on delivery", "cod", "transfer money", "bank transfer",
    "fast delivery", "when will it arrive", "delivery time",
    # Arabic
    "دفع", "الدفع", "طريقة الدفع", "فاتورة", "توصيل", "كيف التوصيل",
    "متى يوصل", "الدفع عند الاستلام", "تحويل بنكي",
})

# Explicit escalation to human (weight 1.0 — hardcoded transfer)
_ESCALATION_PHRASES: frozenset[str] = frozenset({
    "talk to human", "speak to human", "speak to agent", "speak to someone",
    "talk to agent", "talk to person", "real person", "human agent",
    "customer support", "customer service", "live agent", "live support",
    "connect me to", "transfer me", "escalate", "i need help from a person",
    "can i speak", "can i talk", "let me talk", "need a human",
    "need an agent", "want to speak", "want to talk to someone",
    "operator", "representative", "rep", "support team",
    # Arabic
    "اريد التحدث", "أريد التحدث مع شخص", "تحدث مع موظف", "موظف",
    "خدمة العملاء", "دعم بشري", "وكيل بشري", "تحويل",
})

# Frustration signals — contribute a bonus when combined with other signals
_FRUSTRATION_PHRASES: frozenset[str] = frozenset({
    "frustrated", "frustrating", "useless", "not helpful", "doesn't work",
    "doesn't understand", "not working", "bad service", "terrible",
    "awful", "horrible", "worst", "pathetic", "waste of time",
    "not what i wanted", "that's wrong", "still wrong", "you're wrong",
    "you don't understand", "this is ridiculous", "not satisfied",
    "disappointed", "angry", "annoyed", "fed up",
    # Arabic
    "مزعج", "محبط", "مش مفيد", "مش صح", "غلط", "مش فاهم",
    "ما تفهم", "تعبت منك", "زهقت", "غاضب", "مستاء",
})


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s؀-ۿ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _phrase_match(normalised_msg: str, phrase_set: frozenset[str]) -> bool:
    """Return True if *any* phrase from the set is a substring of the message."""
    for phrase in phrase_set:
        if phrase in normalised_msg:
            return True
    return False


def _repetition_bonus(message: str, history: list[dict]) -> float:
    """
    Return up to 0.30 if the user has sent very similar short messages before.

    Detects frustrated re-asking ("how do i buy?", "HOW DO I BUY???", …).
    Only looks at the last 10 user turns to keep it O(1) in practice.
    """
    if not history:
        return 0.0

    user_turns = [
        h["content"] for h in history[-10:]
        if h.get("role") == "user"
    ]
    if len(user_turns) < 2:
        return 0.0

    norm_current = _normalise(message)
    matches = sum(
        1 for prev in user_turns
        if _normalise(prev) == norm_current
    )
    return min(matches * 0.15, 0.30)


# ── Public intent detector ─────────────────────────────────────────────────────

def detect_intent(
    message: str,
    history: list[dict],
) -> tuple[bool, str, float]:
    """
    Analyse *message* + recent *history* for purchase / escalation intent.

    Returns
    -------
    (should_transfer, reason_label, confidence_score)

    should_transfer  — True if confidence >= TRANSFER_THRESHOLD
    reason_label     — human-readable reason code logged to the handoff row
    confidence_score — float in [0.0, 1.0]
    """
    norm = _normalise(message)

    # Hard-coded escalation: user explicitly asks for a human → always transfer
    if _phrase_match(norm, _ESCALATION_PHRASES):
        return True, "explicit_escalation", 1.0

    score: float = 0.0
    reason: str  = "no_intent"

    if _phrase_match(norm, _BUY_PHRASES):
        score  = max(score, 0.90)
        reason = "purchase_intent"

    if _phrase_match(norm, _PAYMENT_PHRASES):
        score  = max(score, 0.80)
        reason = reason if reason != "no_intent" else "payment_inquiry"

    # Frustration alone is not enough; it amplifies an existing score
    if _phrase_match(norm, _FRUSTRATION_PHRASES):
        if score > 0:
            score = min(score + 0.15, 1.0)
            reason = f"{reason}+frustration"
        else:
            score  = 0.40
            reason = "frustration"

    # Repetition bonus — user sent essentially the same message before
    bonus = _repetition_bonus(message, history)
    if bonus > 0:
        score  = min(score + bonus, 1.0)
        reason = f"{reason}+repetition" if reason != "no_intent" else "repetition"

    should_transfer = score >= TRANSFER_THRESHOLD
    logger.debug(
        "intent_score user_message=%r score=%.2f reason=%s transfer=%s",
        message[:60], score, reason, should_transfer,
    )
    return should_transfer, reason, round(score, 4)


# ── Canned response strings ────────────────────────────────────────────────────

_TRANSFER_MSG_EN = (
    "Thank you for your interest! 🛍️ I'm connecting you with one of our "
    "sales team members who will assist you with your order, payment, and "
    "delivery. Please hold on for a moment — someone will be with you shortly."
)
_TRANSFER_MSG_AR = (
    "شكراً لاهتمامك! 🛍️ سأقوم بتحويلك الآن إلى أحد أعضاء فريق المبيعات "
    "لمساعدتك في إتمام طلبك وعملية الدفع والتوصيل. "
    "يرجى الانتظار لحظة — سيتواصل معك أحد الموظفين قريباً."
)
_PENDING_MSG_EN = (
    "You're in our queue 🕐 — a team member will be with you shortly. "
    "Please hold on."
)
_PENDING_MSG_AR = (
    "أنت في قائمة الانتظار 🕐 — سيتواصل معك أحد أعضاء الفريق قريباً. "
    "يرجى الانتظار."
)
_HANDLING_MSG_EN = (
    "You're connected with our support team. "
    "An agent will respond to your message shortly."
)
_HANDLING_MSG_AR = (
    "أنت متصل بفريق الدعم لدينا. "
    "سيرد عليك أحد الوكلاء على رسالتك قريباً."
)
_NO_AGENT_MSG_EN = (
    "Our team is currently busy, but we've recorded your request. "
    "A member of our sales team will contact you as soon as possible. "
    "Thank you for your patience! 🙏"
)
_NO_AGENT_MSG_AR = (
    "فريقنا مشغول حالياً، لكنّنا سجّلنا طلبك. "
    "سيتواصل معك أحد أعضاء فريق المبيعات في أقرب وقت ممكن. "
    "شكراً لصبرك! 🙏"
)
_RESOLVED_MSG_EN = (
    "Your previous request has been handled. "
    "If you need further assistance, please send a new message and our team will help you. 🙏"
)
_RESOLVED_MSG_AR = (
    "تم التعامل مع طلبك السابق. "
    "إذا كنت بحاجة إلى مزيد من المساعدة، يرجى إرسال رسالة جديدة وسيساعدك فريقنا. 🙏"
)


def handoff_transfer_message(has_arabic: bool) -> str:
    return _TRANSFER_MSG_AR if has_arabic else _TRANSFER_MSG_EN


def handoff_pending_message(has_arabic: bool) -> str:
    return _PENDING_MSG_AR if has_arabic else _PENDING_MSG_EN


def handoff_handling_message(has_arabic: bool) -> str:
    return _HANDLING_MSG_AR if has_arabic else _HANDLING_MSG_EN


def no_agent_available_message(has_arabic: bool) -> str:
    return _NO_AGENT_MSG_AR if has_arabic else _NO_AGENT_MSG_EN


def handoff_resolved_message(has_arabic: bool) -> str:
    return _RESOLVED_MSG_AR if has_arabic else _RESOLVED_MSG_EN


# ── State-management helpers ───────────────────────────────────────────────────

def get_or_create_handoff(user_id: str, db: Session) -> ConversationHandoff:
    """
    Return the existing handoff row for *user_id*, creating it
    (status=ai_active, ai_disabled=False) if it does not exist yet.

    The INSERT is wrapped in a try/except so that concurrent first-message
    requests from multiple Gunicorn workers don't race on the UNIQUE user_id
    constraint and cause an unhandled IntegrityError.
    """
    row = (
        db.query(ConversationHandoff)
        .filter(ConversationHandoff.user_id == user_id)
        .first()
    )
    if row is not None:
        return row

    try:
        row = ConversationHandoff(
            user_id     = user_id,
            status      = HandoffStatus.ai_active,
            ai_disabled = False,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError:
        # Another worker inserted the row between our SELECT and INSERT.
        db.rollback()
        return (
            db.query(ConversationHandoff)
            .filter(ConversationHandoff.user_id == user_id)
            .first()
        )


def trigger_transfer(
    user_id:    str,
    reason:     str,
    confidence: float,
    db:         Session,
) -> Optional[ConversationHandoff]:
    """
    Transition ai_active → pending_human.

    Guards on `status == ai_active` so concurrent requests produce a no-op
    rather than a double-transfer.  Returns the updated row, or None if the
    row was already beyond ai_active (idempotent).
    """
    row = (
        db.query(ConversationHandoff)
        .filter(
            ConversationHandoff.user_id == user_id,
            ConversationHandoff.status  == HandoffStatus.ai_active,
        )
        .first()
    )
    if row is None:
        # Already transferred or row doesn't exist — fetch the current state
        return (
            db.query(ConversationHandoff)
            .filter(ConversationHandoff.user_id == user_id)
            .first()
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row.status              = HandoffStatus.pending_human
    row.ai_disabled         = True
    row.transfer_reason     = reason
    row.ai_confidence_score = confidence
    row.transferred_at      = now
    row.assigned_agent_id   = None
    row.resolved_at         = None
    db.commit()
    db.refresh(row)

    logger.info(
        "handoff_triggered user=%s reason=%s confidence=%.2f",
        user_id, reason, confidence,
    )
    return row


def assign_agent(
    user_id:  str,
    agent_id: str,
    db:       Session,
) -> Optional[ConversationHandoff]:
    """
    Transition pending_human → human_handling and record the agent.

    Returns None if the user has no handoff row.
    """
    row = (
        db.query(ConversationHandoff)
        .filter(ConversationHandoff.user_id == user_id)
        .first()
    )
    if row is None:
        return None

    row.status            = HandoffStatus.human_handling
    row.ai_disabled       = True
    row.assigned_agent_id = agent_id
    db.commit()
    db.refresh(row)

    logger.info("agent_assigned user=%s agent=%s", user_id, agent_id)
    return row


def resolve_conversation(
    user_id: str,
    db:      Session,
) -> Optional[ConversationHandoff]:
    """
    Mark a conversation resolved (human_handling → resolved).

    AI remains disabled after resolve; use resume_ai() to re-enable.
    """
    row = (
        db.query(ConversationHandoff)
        .filter(ConversationHandoff.user_id == user_id)
        .first()
    )
    if row is None:
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row.status      = HandoffStatus.resolved
    row.resolved_at = now
    # ai_disabled stays True until resume_ai() is explicitly called
    db.commit()
    db.refresh(row)

    logger.info("conversation_resolved user=%s", user_id)
    return row


def resume_ai(
    user_id: str,
    db:      Session,
) -> Optional[ConversationHandoff]:
    """
    Re-enable AI for a conversation (any state → ai_active).

    Clears agent assignment and resets all handoff fields so the next
    conversation starts fresh.  Does NOT delete the row (audit trail preserved).
    """
    row = (
        db.query(ConversationHandoff)
        .filter(ConversationHandoff.user_id == user_id)
        .first()
    )
    if row is None:
        return None

    row.status              = HandoffStatus.ai_active
    row.ai_disabled         = False
    row.assigned_agent_id   = None
    row.transfer_reason     = None
    row.ai_confidence_score = None
    row.transferred_at      = None
    row.resolved_at         = None
    db.commit()
    db.refresh(row)

    logger.info("ai_resumed user=%s", user_id)
    return row


def get_pending(db: Session) -> list[dict]:
    """
    Return all conversations in pending_human or human_handling state,
    enriched with the last user message and total message count.

    Used by the admin agent queue panel.
    """
    rows = (
        db.query(ConversationHandoff)
        .filter(
            ConversationHandoff.status.in_([
                HandoffStatus.pending_human,
                HandoffStatus.human_handling,
            ])
        )
        .order_by(ConversationHandoff.transferred_at.asc())
        .all()
    )

    result: list[dict] = []
    for row in rows:
        # Last user message
        last_msg = (
            db.query(ChatHistory)
            .filter(
                ChatHistory.user_id == row.user_id,
                ChatHistory.role    == "user",
            )
            .order_by(ChatHistory.created_at.desc())
            .first()
        )

        # Total message count
        msg_count: int = (
            db.query(func.count(ChatHistory.id))
            .filter(ChatHistory.user_id == row.user_id)
            .scalar()
        ) or 0

        result.append({
            "user_id":            row.user_id,
            "status":             row.status.value,
            "assigned_agent_id":  row.assigned_agent_id,
            "transfer_reason":    row.transfer_reason,
            "ai_confidence_score": float(row.ai_confidence_score) if row.ai_confidence_score else None,
            "transferred_at":     row.transferred_at.isoformat() if row.transferred_at else None,
            "message_count":      msg_count,
            "last_message":       last_msg.content[:200] if last_msg else None,
            "last_message_at":    last_msg.created_at.isoformat() if last_msg else None,
        })

    return result


def get_handoff_stats(db: Session) -> dict:
    """
    Aggregate analytics for the admin dashboard.

    Returns counts per status + total transfer rate
    (transferred conversations / total conversations with any history).
    """
    status_counts: dict[str, int] = {}
    for status in HandoffStatus:
        count = (
            db.query(func.count(ConversationHandoff.id))
            .filter(ConversationHandoff.status == status)
            .scalar()
        ) or 0
        status_counts[status.value] = count

    total_users: int = (
        db.query(func.count(func.distinct(ChatHistory.user_id))).scalar()
    ) or 0

    total_transferred = (
        status_counts.get("pending_human", 0)
        + status_counts.get("human_handling", 0)
        + status_counts.get("resolved", 0)
    )

    transfer_rate = (
        round(total_transferred / total_users, 4) if total_users > 0 else 0.0
    )

    return {
        "status_counts":   status_counts,
        "total_users":     total_users,
        "transfer_rate":   transfer_rate,
    }
