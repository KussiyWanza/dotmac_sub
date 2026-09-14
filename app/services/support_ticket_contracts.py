"""Typed cross-owner contracts for authoritative support-ticket facts."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class InternalOperationalTicketSource(str, Enum):
    """Approved internal sources allowed to request silent Ticket creation."""

    unmatched_radio_queue = "unmatched_radio_queue"


class SupportTicketCommentRealtimeChange(str, Enum):
    """Customer-visible comment changes that require an authoritative refetch."""

    comment_created = "comment_created"
    comment_updated = "comment_updated"
    comment_deleted = "comment_deleted"
    comment_visibility_changed = "comment_visibility_changed"


class SupportTicketCommentRealtimeHint(BaseModel):
    """Identifier-only hint carried by the best-effort realtime projection."""

    model_config = ConfigDict(frozen=True)

    ticket_id: UUID
    change: SupportTicketCommentRealtimeChange
    comment_id: UUID | None = None
