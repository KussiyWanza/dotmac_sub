"""Read-only manager AI assistant for Team Inbox conversation insight."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.team_inbox import InboxConversation, InboxMessage
from app.services import control_registry
from app.services.ai.client import AIClientError
from app.services.ai.gateway import ai_gateway
from app.services.ai.security import redact_secret_text

MANAGER_AI_PERMISSION = "support:inbox_ai:read"
_MAX_QUESTION_CHARS = 2000
_MAX_CONTEXT_MESSAGES = 40


@dataclass(frozen=True, slots=True)
class ManagerChatConversationOption:
    id: UUID
    label: str
    status: str
    channel_type: str
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class ManagerChatPageState:
    conversations: tuple[ManagerChatConversationOption, ...]
    selected_conversation_id: UUID | None
    question: str
    answer: str | None
    error: str | None
    provider_enabled: bool
    generation_enabled: bool


def _coerce_uuid(value: object) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _conversation_label(conversation: InboxConversation) -> str:
    parts = [
        conversation.subject,
        conversation.contact_address,
        conversation.channel_type,
    ]
    label = " - ".join(str(part).strip() for part in parts if str(part or "").strip())
    return label[:180] or str(conversation.id)


def recent_conversation_options(
    db: Session, *, selected_conversation_id: UUID | None = None, limit: int = 50
) -> tuple[ManagerChatConversationOption, ...]:
    rows = (
        db.execute(
            select(InboxConversation)
            .where(InboxConversation.is_active.is_(True))
            .order_by(
                InboxConversation.last_message_at.desc().nullslast(),
                InboxConversation.created_at.desc(),
            )
            .limit(max(1, min(limit, 100)))
        )
        .scalars()
        .all()
    )
    if selected_conversation_id and all(
        row.id != selected_conversation_id for row in rows
    ):
        selected = db.get(InboxConversation, selected_conversation_id)
        if selected is not None:
            rows = [selected, *rows]
    return tuple(
        ManagerChatConversationOption(
            id=row.id,
            label=_conversation_label(row),
            status=row.status,
            channel_type=row.channel_type,
            last_message_at=row.last_message_at,
        )
        for row in rows
    )


def _message_payload(message: InboxMessage) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "direction": message.direction,
        "channel_type": message.channel_type,
        "subject": message.subject,
        "body": (message.body or "")[:2000],
        "created_at": message.created_at.isoformat()
        if message.created_at is not None
        else None,
        "received_at": message.received_at.isoformat()
        if message.received_at is not None
        else None,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
    }


def _conversation_payload(
    db: Session, conversation_id: UUID | None
) -> dict[str, Any] | None:
    if conversation_id is None:
        return None
    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None:
        return None
    messages = (
        db.execute(
            select(InboxMessage)
            .where(InboxMessage.conversation_id == conversation.id)
            .order_by(InboxMessage.created_at.desc())
            .limit(_MAX_CONTEXT_MESSAGES)
        )
        .scalars()
        .all()
    )
    metadata = (
        conversation.metadata_ if isinstance(conversation.metadata_, dict) else {}
    )
    return {
        "id": str(conversation.id),
        "subscriber_id": str(conversation.subscriber_id)
        if conversation.subscriber_id is not None
        else None,
        "primary_service_team_id": str(conversation.primary_service_team_id)
        if conversation.primary_service_team_id is not None
        else None,
        "channel_type": conversation.channel_type,
        "status": conversation.status,
        "priority": conversation.priority,
        "subject": conversation.subject,
        "contact_address_present": bool(conversation.contact_address),
        "first_message_at": conversation.first_message_at.isoformat()
        if conversation.first_message_at is not None
        else None,
        "last_message_at": conversation.last_message_at.isoformat()
        if conversation.last_message_at is not None
        else None,
        "ai_intake": metadata.get("ai_intake"),
        "messages": [_message_payload(message) for message in reversed(messages)],
    }


def _queue_payload(db: Session) -> dict[str, Any]:
    return {
        "recent_conversations": [
            {
                "id": str(row.id),
                "label": row.label,
                "status": row.status,
                "channel_type": row.channel_type,
                "last_message_at": row.last_message_at.isoformat()
                if row.last_message_at is not None
                else None,
            }
            for row in recent_conversation_options(db, limit=20)
        ]
    }


def _system_prompt() -> str:
    return (
        "You are a manager-only Team Inbox analyst. Answer from the provided "
        "inbox context only. Give operational insight about customer intent, "
        "urgency, missing information, risk, and recommended manager follow-up. "
        "Do not claim you performed assignments, replies, refunds, profile "
        "updates, or service changes. If context is insufficient, say what is "
        "missing. Keep the answer concise."
    )


def answer_manager_question(
    db: Session, *, question: str, conversation_id: UUID | str | None = None
) -> str:
    clean_question = str(question or "").strip()
    if not clean_question:
        raise ValueError("Question is required.")
    if len(clean_question) > _MAX_QUESTION_CHARS:
        raise ValueError("Question is too long.")
    if not control_registry.is_enabled(db, "ai.generation"):
        raise AIClientError("AI generation is disabled", failure_type="ai_disabled")
    if not ai_gateway.enabled(db):
        raise AIClientError("AI provider is disabled", failure_type="ai_disabled")

    selected_id = _coerce_uuid(conversation_id)
    payload = {
        "question": clean_question,
        "conversation": _conversation_payload(db, selected_id),
        "queue": _queue_payload(db) if selected_id is None else None,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    if selected_id is not None and payload["conversation"] is None:
        raise ValueError("Conversation was not found.")

    response, _routing = ai_gateway.generate_with_fallback(
        db,
        system=_system_prompt(),
        prompt=json.dumps(payload, sort_keys=True, default=str),
        max_tokens=900,
    )
    return (response.content or "").strip() or "No answer was generated."


def build_page_state(
    db: Session,
    *,
    conversation_id: UUID | str | None = None,
    question: str | None = None,
    answer: str | None = None,
    error: str | None = None,
) -> ManagerChatPageState:
    selected_id = _coerce_uuid(conversation_id)
    return ManagerChatPageState(
        conversations=recent_conversation_options(
            db, selected_conversation_id=selected_id
        ),
        selected_conversation_id=selected_id,
        question=str(question or ""),
        answer=answer,
        error=redact_secret_text(error) if error else None,
        provider_enabled=ai_gateway.enabled(db),
        generation_enabled=control_registry.is_enabled(db, "ai.generation"),
    )
