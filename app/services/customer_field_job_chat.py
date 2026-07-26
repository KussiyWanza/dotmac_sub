"""The customer's half of the job chat, from the selfcare portal.

Scoped to the subscriber's own work order on every call, exactly as
``customer_work_order_selfcare`` scopes the technician's location. A customer
message is an ordinary inbound ``InboxMessage`` on the job conversation, so the
technician sees it through the same conversation as everything else and it
counts as "awaiting reply" for the completion fallback.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
)
from app.models.work_order import WorkOrder
from app.services import (
    team_inbox_commands,
    team_inbox_field_job,
    team_inbox_realtime,
)
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.field.jobs import _subscriber_name

MAX_CHAT_MESSAGES = 200
MAX_BODY_CHARS = 2000


class FieldJobChatError(ValueError):
    """Raised when the customer cannot chat on this job."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _owned_work_order(
    db: Session, subscriber_id: str, work_order_public_id: str
) -> WorkOrder | None:
    try:
        subscriber_uuid = coerce_uuid(subscriber_id)
    except (TypeError, ValueError):
        return None
    return db.scalar(
        select(WorkOrder).where(
            WorkOrder.subscriber_id == subscriber_uuid,
            WorkOrder.public_id == work_order_public_id,
        )
    )


def _serialize(message: InboxMessage) -> dict:
    from_customer = message.direction == InboxMessageDirection.inbound.value
    return {
        "id": str(message.id),
        "body": message.body,
        "from_customer": from_customer,
        "author_name": (
            None if from_customer else (message.metadata_ or {}).get("author_name")
        ),
        "created_at": message.created_at,
    }


def _thread_payload(
    db: Session, conversation: InboxConversation, *, limit: int
) -> list[dict]:
    rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(
            InboxMessage.direction.in_(
                (
                    InboxMessageDirection.inbound.value,
                    InboxMessageDirection.outbound.value,
                )
            )
        )
        .order_by(InboxMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [_serialize(message) for message in rows]


def get_thread(
    db: Session,
    subscriber_id: str,
    work_order_public_id: str,
    *,
    limit: int = 50,
) -> dict:
    row = _owned_work_order(db, subscriber_id, work_order_public_id)
    if row is None:
        return {"available": False, "reason": "not_found", "messages": []}

    conversation = team_inbox_field_job.conversation_for(db, row)
    if conversation is None:
        # The chat exists from the moment the technician sets off, not from
        # the moment the job is booked.
        return {
            "available": False,
            "reason": "not_departed",
            "work_order_id": row.public_id,
            "messages": [],
        }

    safe_limit = max(1, min(int(limit or 50), MAX_CHAT_MESSAGES))
    return {
        "available": True,
        "can_send": team_inbox_field_job.is_open(conversation),
        "work_order_id": row.public_id,
        "conversation_id": str(conversation.id),
        "technician_name": row.technician_name,
        "messages": _thread_payload(db, conversation, limit=safe_limit),
    }


def send_message(
    db: Session,
    subscriber_id: str,
    work_order_public_id: str,
    *,
    body: str,
) -> dict:
    text = (body or "").strip()
    if not text:
        raise FieldJobChatError("empty_body", "A message is required")
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS]

    row = _owned_work_order(db, subscriber_id, work_order_public_id)
    if row is None:
        raise FieldJobChatError("not_found", "Visit not found")

    conversation = team_inbox_field_job.conversation_for(db, row)
    if conversation is None:
        raise FieldJobChatError(
            "not_departed", "You can message your technician once they set off"
        )
    if not team_inbox_field_job.is_open(conversation):
        raise FieldJobChatError("closed", "This visit's chat has closed")

    subscriber = db.get(Subscriber, row.subscriber_id)
    author_name = _subscriber_name(subscriber) if subscriber is not None else None

    # Everything above was a read, which leaves the session inside a
    # transaction; an owner command refuses to start in one. Capture what the
    # command needs as plain values, then hand it a clean session.
    public_id = str(row.public_id)
    db_session_adapter.release_read_transaction(db)

    written = team_inbox_commands.record_field_job_customer_message(
        db,
        work_order_public_id=public_id,
        body=text,
        author_name=author_name,
    )

    team_inbox_realtime.publish_conversation_event(
        db,
        written["conversation_id"],
        event_type=team_inbox_realtime.EventType.MESSAGE_NEW,
        payload=team_inbox_realtime.message_event_payload(
            conversation_id=written["conversation_id"],
            message_id=written["id"],
            body=written["body"],
            direction=InboxMessageDirection.inbound.value,
            channel_type=InboxChannelType.field_job.value,
            created_at=written["created_at"],
            author_name=author_name,
            extra={"sender_type": "customer", "from_customer": True},
        ),
    )
    return {
        "id": written["id"],
        "body": written["body"],
        "from_customer": True,
        "author_name": None,
        "created_at": written["created_at"],
    }
