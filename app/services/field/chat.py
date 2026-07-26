"""Job-scoped technician↔customer chat, backed by the team inbox.

This used to persist to ``field_job_chat_messages``, a second store for
customer↔staff messaging that only ever held one side of it: every writer set
``direction="staff"``, both endpoints were staff-authenticated, and no customer
surface read it. It was a technician note pad wearing a chat's clothes.

The thread is now an ``InboxConversation`` on the ``field_job`` channel, owned
by ``team_inbox_field_job``, which opens it when the technician departs and
closes it when the visit ends. Sending goes through the same outbound path as
every other reply, so the inbox stays the single owner of customer↔staff
messaging. This module is a thin adapter keeping the field app's endpoints in
their existing shape.
"""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.team_inbox import (
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
)
from app.models.work_order import WorkOrder
from app.services import team_inbox_field_job, team_inbox_outbound
from app.services.field.jobs import (
    _profile_from_principal,
    _scoped_query,
    _subscriber_name,
    _system_user,
    _technician_name,
)

MAX_CHAT_MESSAGES = 200

_EXCHANGED = (
    InboxMessageDirection.inbound.value,
    InboxMessageDirection.outbound.value,
)


def _serialize(message: InboxMessage) -> dict:
    return {
        "id": message.id,
        "body": message.body,
        # The field app's vocabulary: inbound is the customer, outbound is the
        # technician.
        "direction": (
            "customer"
            if message.direction == InboxMessageDirection.inbound.value
            else "staff"
        ),
        "author_name": (message.metadata_ or {}).get("author_name"),
        "created_at": message.created_at,
        # The inbox keeps a read cursor per operator on the conversation rather
        # than a timestamp per message. Nothing ever populated this on the old
        # store either, so it stays absent rather than becoming a guess.
        "read_at": None,
    }


def _scoped_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str,
) -> WorkOrder:
    profile = _profile_from_principal(db, principal)
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrder.public_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _customer_name(db: Session, row: WorkOrder) -> str | None:
    subscriber = db.get(Subscriber, row.subscriber_id)
    if subscriber is None:
        return None
    return _subscriber_name(subscriber)


def _messages(
    db: Session, conversation: InboxConversation, *, limit: int
) -> list[InboxMessage]:
    rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction.in_(_EXCHANGED))
        .order_by(InboxMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return rows


class FieldJobChat:
    @staticmethod
    def get_thread(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        limit: int = 50,
    ) -> dict:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        safe_limit = max(1, min(int(limit or 50), MAX_CHAT_MESSAGES))
        conversation = team_inbox_field_job.conversation_for(db, row)
        if conversation is None:
            # No conversation means this technician has not departed for the
            # job yet. The history is genuinely empty, not withheld.
            return {
                "available": False,
                "can_send": False,
                "conversation_id": None,
                "customer_name": _customer_name(db, row),
                "messages": [],
            }
        return {
            "available": True,
            "can_send": team_inbox_field_job.is_open(conversation),
            "conversation_id": str(conversation.id),
            "customer_name": _customer_name(db, row),
            "messages": [
                _serialize(message)
                for message in _messages(db, conversation, limit=safe_limit)
            ],
        }

    @staticmethod
    def send_message(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        body: str,
    ) -> dict:
        text = (body or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="Message body is required")
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")

        conversation = team_inbox_field_job.conversation_for(db, row)
        if conversation is None or not team_inbox_field_job.is_open(conversation):
            raise HTTPException(
                status_code=409, detail="Field chat is not active for this job"
            )

        user = _system_user(db, profile)
        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.InboxReplyPayload(
                body_html=escape(text),
                body_text=text,
                metadata={"author_name": _technician_name(profile, user)},
            ),
        )
        if result.kind != "queued" or result.message_id is None:
            raise HTTPException(
                status_code=409,
                detail=result.reason or "Message could not be sent",
            )
        db.commit()
        message = db.get(InboxMessage, result.message_id)
        if message is None:  # pragma: no cover - defensive
            raise HTTPException(status_code=409, detail="Message could not be sent")
        return _serialize(message)


field_job_chat = FieldJobChat()
