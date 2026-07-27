from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.realtime_platform import (
    STAFF_AUDIENCE_TOPIC,
    EventType,
    conversation_topic,
    publish_topic_event,
)
from app.services.session_hooks import run_after_commit

logger = logging.getLogger(__name__)


def message_event_payload(
    *,
    conversation_id: str,
    message_id: str,
    body: str | None,
    direction: str,
    channel_type: str,
    created_at: datetime | None,
    author_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "id": message_id,
        "body": body,
        "direction": direction,
        "channel_type": channel_type,
        "created_at": created_at.isoformat() if created_at else None,
    }
    if author_name:
        payload["author_name"] = author_name
    if extra:
        payload.update(extra)
    return payload


async def broadcast_conversation_event(
    conversation_id: str,
    *,
    event_type: EventType,
    payload: dict[str, Any],
) -> None:
    await asyncio.to_thread(
        publish_topic_event,
        conversation_topic(conversation_id),
        event_type=event_type,
        payload=payload,
    )


def publish_conversation_event(
    db: Session,
    conversation_id: str,
    *,
    event_type: EventType,
    payload: dict[str, Any],
) -> None:
    """Best-effort projection after the inbox owner commits durable state."""

    event_payload = dict(payload)

    def publish(_callback_db: Session) -> None:
        try:
            publish_topic_event(
                conversation_topic(conversation_id),
                event_type=event_type,
                payload=event_payload,
            )
        except Exception:
            logger.debug("team_inbox_realtime_publish_failed", exc_info=True)

    run_after_commit(db, publish)


def publish_queue_event(
    db: Session,
    *,
    conversation_id: str,
    created: bool,
) -> None:
    """Tell every connected operator that the queue changed.

    Conversation topics only reach clients already subscribed to that thread,
    and a client subscribes to what is on screen — so a brand-new conversation
    had no subscriber and reached nobody. The staff audience topic is
    subscribed at connection time, which is what makes newly arrived work
    visible without a reload.
    """

    payload = {"conversation_id": conversation_id, "created": created}

    def publish(_callback_db: Session) -> None:
        try:
            publish_topic_event(
                STAFF_AUDIENCE_TOPIC,
                event_type=EventType.CONVERSATION_UPDATED,
                payload=payload,
            )
        except Exception:
            logger.debug("team_inbox_queue_publish_failed", exc_info=True)

    run_after_commit(db, publish)


def rebuild_conversation_projection(db: Session, conversation_id: str) -> bool:
    """Idempotently replace the best-effort topic with current durable state."""

    from app.services import team_inbox_read

    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        return False
    publish_topic_event(
        conversation_topic(conversation_id),
        event_type=EventType.CONVERSATION_UPDATED,
        payload={
            "conversation_id": timeline.id,
            "status": timeline.status,
            "priority": timeline.priority,
            "last_message_at": (
                timeline.last_message_at.isoformat()
                if timeline.last_message_at is not None
                else None
            ),
            "message_count": len(timeline.messages),
            "projection_rebuilt": True,
        },
    )
    return True
