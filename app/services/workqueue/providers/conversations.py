"""Team-inbox conversation provider — the operational inbox in the workqueue.

Team inbox has no SLA policy table, so a conversation's SLA is derived: once the
last message on a thread is *inbound*, a reply is due
``conversation_response_target_seconds`` after it landed. That due instant is fed
through the same SLA bands as every other source, so an inbox thread about to
breach outranks a merely-old ticket.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import ai_conversation_ownership
from app.services.workqueue.providers import register
from app.services.workqueue.providers.common import as_utc, score_item, seconds_until
from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.scoring_config import WorkqueueScoringConfig
from app.services.workqueue.types import ActionKind, ItemKind, WorkqueueItem


class ConversationProvider:
    kind = ItemKind.conversation

    def fetch(
        self,
        db: Session,
        *,
        scope: WorkqueueScope,
        config: WorkqueueScoringConfig,
        snoozed_ids: set[UUID],
        now: datetime,
        limit: int,
    ) -> list[WorkqueueItem]:
        assignment = InboxConversationAssignment
        query = (
            db.query(InboxConversation, assignment)
            .outerjoin(
                assignment,
                (assignment.conversation_id == InboxConversation.id)
                & (assignment.is_active.is_(True)),
            )
            .filter(InboxConversation.is_active.is_(True))
            .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
            .filter(~ai_conversation_ownership.ai_owned_conversation_clause())
        )

        # Team inbox has its own per-conversation snooze; respect it.
        query = query.filter(
            or_(
                InboxConversation.snoozed_until.is_(None),
                InboxConversation.snoozed_until <= now,
            )
        )

        if scope.is_self_audience:
            query = query.filter(
                or_(
                    assignment.person_id == scope.person_id,
                    assignment.person_id.is_(None),
                )
            )

        if not scope.is_org_wide:
            team_ids = scope.team_ids_for_query()
            visibility = [assignment.person_id == scope.person_id]
            if team_ids:
                visibility.append(
                    InboxConversation.primary_service_team_id.in_(team_ids)
                )
            query = query.filter(or_(*visibility))
        elif scope.service_team_filter is not None:
            query = query.filter(
                InboxConversation.primary_service_team_id == scope.service_team_filter
            )

        if snoozed_ids:
            query = query.filter(InboxConversation.id.notin_(snoozed_ids))

        rows = (
            query.order_by(
                InboxConversation.priority.asc(),
                InboxConversation.last_message_at.desc().nullslast(),
            )
            .limit(limit)
            .all()
        )
        if not rows:
            return []

        last_inbound = _last_inbound_at(
            db, [conversation.id for conversation, _ in rows]
        )
        return [
            self._to_item(conversation, assigned, last_inbound, config, now, scope)
            for conversation, assigned in rows
        ]

    def _to_item(
        self,
        conversation: InboxConversation,
        assigned: InboxConversationAssignment | None,
        last_inbound: dict[UUID, datetime],
        config: WorkqueueScoringConfig,
        now: datetime,
        scope: WorkqueueScope,
    ) -> WorkqueueItem:
        assignee_id = assigned.person_id if assigned is not None else None
        awaiting_since = last_inbound.get(conversation.id)

        candidates: list[tuple[int, str]] = []
        due_at: datetime | None = None
        if awaiting_since is not None:
            due_at = awaiting_since + _target_delta(config)
            candidates.append(
                (config.conversation_scores["awaiting_reply"], "awaiting_reply")
            )
            band = config.conversation_sla.band(seconds_until(due_at, now) or 0)
            if band is not None:
                reason, score = band
                candidates.append((score, reason))

        if conversation.priority <= config.conversation_high_priority_at:
            candidates.append(
                (config.conversation_scores["priority_high"], "priority_high")
            )
        if assignee_id is None:
            candidates.append((config.conversation_scores["unassigned"], "unassigned"))
        candidates.append((config.conversation_scores["in_inbox"], "in_inbox"))

        score, reason, urgency = score_item(candidates, config)
        last_activity = as_utc(conversation.last_message_at) or as_utc(
            conversation.created_at
        )

        actions = [ActionKind.open, ActionKind.snooze, ActionKind.complete]
        if assignee_id is None:
            actions.insert(2, ActionKind.claim)

        return WorkqueueItem(
            item_kind=ItemKind.conversation,
            item_id=conversation.id,
            title=conversation.subject or "Inbox conversation",
            subtitle=conversation.contact_address,
            status=conversation.status,
            priority=conversation.priority,
            score=score,
            reason=reason,
            urgency=urgency,
            happened_at=last_activity or now,
            due_at=due_at,
            last_activity_at=last_activity,
            subscriber_id=conversation.subscriber_id,
            service_team_id=conversation.primary_service_team_id,
            assigned_person_id=assignee_id,
            url=f"/admin/inbox/{conversation.id}",
            actions=tuple(actions),
            metadata={
                "channel_type": conversation.channel_type,
                "audience": scope.audience.value,
                "awaiting_reply_since": awaiting_since.isoformat()
                if awaiting_since
                else None,
            },
        )


def _target_delta(config: WorkqueueScoringConfig) -> timedelta:
    return timedelta(seconds=config.conversation_response_target_seconds)


def _last_inbound_at(db: Session, conversation_ids: list[UUID]) -> dict[UUID, datetime]:
    """Conversations whose newest message is inbound -> when it landed.

    A thread whose last message is outbound/internal has been answered and is not
    on an SLA clock.
    """
    if not conversation_ids:
        return {}

    newest = (
        db.query(
            InboxMessage.conversation_id.label("conversation_id"),
            func.max(InboxMessage.created_at).label("created_at"),
        )
        .filter(InboxMessage.conversation_id.in_(conversation_ids))
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )
    rows = (
        db.query(InboxMessage.conversation_id, InboxMessage.created_at)
        .join(
            newest,
            (InboxMessage.conversation_id == newest.c.conversation_id)
            & (InboxMessage.created_at == newest.c.created_at),
        )
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .all()
    )
    awaiting: dict[UUID, datetime] = {}
    for conversation_id, created_at in rows:
        landed = as_utc(created_at)
        if landed is None:
            continue
        current = awaiting.get(conversation_id)
        if current is None or landed > current:
            awaiting[conversation_id] = landed
    return awaiting


conversation_provider = register(ConversationProvider())
