"""Canonical Team Inbox provider reply-window policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
)

OWNER = "communications.team_inbox_reply_window"
WINDOW_HOURS = 24


class ReplyWindowStatus(StrEnum):
    open = "open"
    expired = "expired"
    unavailable = "unavailable"
    not_applicable = "not_applicable"


META_FREE_FORM_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
    }
)


@dataclass(frozen=True, slots=True)
class ReplyWindowDecision:
    channel_type: str
    free_form_allowed: bool
    last_qualifying_inbound_at: datetime | None
    expires_at: datetime | None
    server_time: datetime
    status: ReplyWindowStatus
    reason: str | None
    whatsapp_template_available: bool
    unknown: bool = False

    @property
    def blocks_free_form(self) -> bool:
        return (
            self.channel_type in META_FREE_FORM_CHANNELS and not self.free_form_allowed
        )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _message_time() -> object:
    return func.coalesce(InboxMessage.received_at, InboxMessage.created_at)


def coerce_conversation_id(value: str | UUID | None) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def latest_qualifying_inbound_at(
    db: Session,
    *,
    conversation_id: UUID,
) -> datetime | None:
    """Return the latest customer message that can open a Meta reply window.

    Internal notes, staff replies, receipts, scheduled rows, comments, and audit
    events are excluded by using only authoritative inbound Inbox messages.
    """

    rows = (
        db.query(InboxMessage.received_at, InboxMessage.created_at)
        .filter(InboxMessage.conversation_id == conversation_id)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .filter(
            or_(
                InboxMessage.metadata_["reply_window_qualifying"]
                .as_boolean()
                .isnot(False),
                InboxMessage.metadata_["reply_window_qualifying"].is_(None),
            )
        )
        .all()
    )
    timestamps = [_aware(received_at or created_at) for received_at, created_at in rows]
    return max((value for value in timestamps if value is not None), default=None)


def decide_reply_window(
    db: Session,
    *,
    conversation: InboxConversation,
    now: datetime | None = None,
    whatsapp_template_available: bool | None = None,
) -> ReplyWindowDecision:
    server_time = _aware(now) or datetime.now(UTC)
    channel_type = str(conversation.channel_type or "")
    template_available = (
        channel_type == InboxChannelType.whatsapp.value
        if whatsapp_template_available is None
        else bool(whatsapp_template_available)
    )
    if channel_type not in META_FREE_FORM_CHANNELS:
        return ReplyWindowDecision(
            channel_type=channel_type,
            free_form_allowed=True,
            last_qualifying_inbound_at=None,
            expires_at=None,
            server_time=server_time,
            status=ReplyWindowStatus.not_applicable,
            reason=None,
            whatsapp_template_available=False,
        )

    last_inbound = latest_qualifying_inbound_at(db, conversation_id=conversation.id)
    if last_inbound is None:
        return ReplyWindowDecision(
            channel_type=channel_type,
            free_form_allowed=False,
            last_qualifying_inbound_at=None,
            expires_at=None,
            server_time=server_time,
            status=ReplyWindowStatus.unavailable,
            reason=(
                "Reply availability could not be confirmed. Free-form messaging is "
                "disabled to prevent a provider rejection."
            ),
            whatsapp_template_available=template_available,
            unknown=True,
        )
    expires_at = last_inbound + timedelta(hours=WINDOW_HOURS)
    allowed = server_time < expires_at
    return ReplyWindowDecision(
        channel_type=channel_type,
        free_form_allowed=allowed,
        last_qualifying_inbound_at=last_inbound,
        expires_at=expires_at,
        server_time=server_time,
        status=ReplyWindowStatus.open if allowed else ReplyWindowStatus.expired,
        reason=None
        if allowed
        else "The 24-hour reply window has expired. A free-form reply cannot be sent until the customer messages again.",
        whatsapp_template_available=template_available,
    )


def is_whatsapp_template_payload(metadata: dict | None) -> bool:
    template = (metadata or {}).get("whatsapp_template")
    return isinstance(template, dict) and bool(str(template.get("name") or "").strip())
