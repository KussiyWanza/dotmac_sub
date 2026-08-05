"""Campaign-sourced inbox writes — inside the team-inbox owner family.

Campaigns decide audience, sequence, and content (`comms_campaigns`); the
inbox owns conversation and message rows. When a campaign send needs to
materialize in the inbox, it requests it here instead of writing inbox ORM
rows itself (SOT map §Communications: inbox rows have no writer outside the
``team_inbox_*`` family; enforced by
``tests/architecture/test_team_inbox_boundaries.py``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamRole,
    InboxTeamSource,
)
from app.services import team_inbox_status


def ensure_campaign_conversation(
    db: Session,
    *,
    subscriber_id: uuid.UUID,
    channel_type: str,
    campaign_id: uuid.UUID,
    campaign_recipient_id: uuid.UUID,
    subject: str,
    contact_address: str | None,
    service_team_id: uuid.UUID | None,
    now: datetime,
) -> InboxConversation:
    """One conversation per (campaign, recipient), reopened if resolved."""
    external_thread_id = f"campaign:{campaign_id}:{subscriber_id}"
    conversation = (
        db.query(InboxConversation)
        .filter(InboxConversation.channel_type == channel_type)
        .filter(InboxConversation.external_thread_id == external_thread_id)
        .one_or_none()
    )
    if conversation is not None:
        if conversation.status == InboxConversationStatus.resolved.value:
            team_inbox_status.apply_status_transition(
                db,
                conversation=conversation,
                status=InboxConversationStatus.open,
                actor_person_id=None,
                reason=team_inbox_status.InboxStatusReason.campaign_reopen,
                source_id=f"campaign-reopen:{campaign_recipient_id}",
                occurred_at=now,
            )
        return conversation

    conversation = InboxConversation(
        subscriber_id=subscriber_id,
        primary_service_team_id=service_team_id,
        channel_type=channel_type,
        status=InboxConversationStatus.open.value,
        subject=subject,
        contact_address=contact_address,
        external_thread_id=external_thread_id,
        first_message_at=now,
        last_message_at=now,
        metadata_={
            "source": "native_campaign",
            "campaign_id": str(campaign_id),
            "campaign_recipient_id": str(campaign_recipient_id),
        },
    )
    db.add(conversation)
    db.flush()
    if service_team_id is not None:
        db.add(
            InboxConversationTeam(
                conversation_id=conversation.id,
                service_team_id=service_team_id,
                role=InboxTeamRole.owner.value,
                source=InboxTeamSource.manual.value,
                metadata_={"source": "native_campaign"},
            )
        )
    db.flush()
    return conversation


def record_campaign_message(
    db: Session,
    *,
    conversation: InboxConversation,
    channel_type: str,
    notification_id: uuid.UUID,
    subject: str | None,
    body: str | None,
    from_address: str | None,
    to_address: str,
    metadata: dict[str, Any],
) -> InboxMessage:
    """The outbound campaign message row for a queued send."""
    message = InboxMessage(
        conversation_id=conversation.id,
        notification_id=notification_id,
        channel_type=channel_type,
        direction=InboxMessageDirection.outbound.value,
        subject=subject,
        body=body,
        external_thread_id=conversation.external_thread_id,
        from_address=from_address,
        to_addresses=[to_address],
        cc_addresses=[],
        metadata_={**metadata, "delivery_status": "queued"},
    )
    db.add(message)
    db.flush()
    return message
