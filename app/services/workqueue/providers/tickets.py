"""Support-ticket provider.

SLA comes from the ticket's running SLA clock (``sla_clocks``, owned by
``app.services.sla_assignment``) when one exists, falling back to the ticket's
own ``due_at``. Priority and triage state only matter when no SLA band fires.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.support import Ticket, TicketPriority, TicketStatus
from app.models.ticket_workflow import SlaClock, SlaClockStatus, WorkflowEntityType
from app.services.workqueue.providers import register
from app.services.workqueue.providers.common import (
    as_utc,
    legacy_priority,
    score_item,
    seconds_until,
)
from app.services.workqueue.scope import WorkqueueScope
from app.services.workqueue.scoring_config import WorkqueueScoringConfig
from app.services.workqueue.types import ActionKind, ItemKind, WorkqueueItem

CLOSED_TICKET_STATUSES = (
    TicketStatus.closed.value,
    TicketStatus.canceled.value,
    TicketStatus.merged.value,
)

TRIAGE_STATUSES = frozenset({TicketStatus.new.value, TicketStatus.open.value})


class TicketProvider:
    kind = ItemKind.ticket

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
        query = (
            db.query(Ticket)
            .filter(Ticket.is_active.is_(True))
            .filter(Ticket.status.notin_(CLOSED_TICKET_STATUSES))
        )

        if scope.is_self_audience:
            # My work, plus anything unclaimed I am allowed to pull.
            query = query.filter(
                or_(
                    Ticket.assigned_to_person_id == scope.person_id,
                    Ticket.assigned_to_person_id.is_(None),
                )
            )

        if not scope.is_org_wide:
            team_ids = scope.team_ids_for_query()
            visibility = [Ticket.assigned_to_person_id == scope.person_id]
            if team_ids:
                visibility.append(Ticket.service_team_id.in_(team_ids))
            query = query.filter(or_(*visibility))
        elif scope.service_team_filter is not None:
            query = query.filter(Ticket.service_team_id == scope.service_team_filter)

        if snoozed_ids:
            query = query.filter(Ticket.id.notin_(snoozed_ids))

        rows = (
            query.order_by(
                Ticket.due_at.asc().nullslast(),
                Ticket.updated_at.desc(),
            )
            .limit(limit)
            .all()
        )
        if not rows:
            return []

        sla_due = _sla_due_by_ticket(db, [ticket.id for ticket in rows])
        return [self._to_item(ticket, sla_due, config, now, scope) for ticket in rows]

    def _to_item(
        self,
        ticket: Ticket,
        sla_due: dict[UUID, tuple[datetime | None, bool]],
        config: WorkqueueScoringConfig,
        now: datetime,
        scope: WorkqueueScope,
    ) -> WorkqueueItem:
        clock_due, breached = sla_due.get(ticket.id, (None, False))
        due_at = clock_due or as_utc(ticket.due_at)

        candidates: list[tuple[int, str]] = [
            (config.ticket_scores["in_queue"], "in_queue")
        ]
        if breached:
            candidates.append((config.ticket_sla.breach_score, "sla_breach"))
        remaining = seconds_until(due_at, now)
        if remaining is not None:
            band = config.ticket_sla.band(remaining)
            if band is not None:
                reason, score = band
                candidates.append((score, reason))

        priority = str(ticket.priority or "").lower()
        if priority == TicketPriority.urgent.value:
            candidates.append(
                (config.ticket_scores["priority_urgent"], "priority_urgent")
            )
        elif priority == TicketPriority.high.value:
            candidates.append((config.ticket_scores["priority_high"], "priority_high"))

        if ticket.assigned_to_person_id is None and ticket.status in TRIAGE_STATUSES:
            candidates.append(
                (config.ticket_scores["awaiting_triage"], "awaiting_triage")
            )

        score, reason, urgency = score_item(candidates, config)
        last_activity = as_utc(ticket.updated_at) or as_utc(ticket.created_at)

        actions = [ActionKind.open, ActionKind.snooze, ActionKind.complete]
        if ticket.assigned_to_person_id is None:
            actions.insert(2, ActionKind.claim)

        return WorkqueueItem(
            item_kind=ItemKind.ticket,
            item_id=ticket.id,
            title=ticket.title,
            subtitle=ticket.number,
            status=ticket.status,
            priority=legacy_priority(ticket.priority),
            score=score,
            reason=reason,
            urgency=urgency,
            happened_at=last_activity or now,
            due_at=due_at,
            last_activity_at=last_activity,
            subscriber_id=ticket.subscriber_id,
            service_team_id=ticket.service_team_id,
            assigned_person_id=ticket.assigned_to_person_id,
            url=f"/admin/support/tickets/{ticket.id}",
            actions=tuple(actions),
            metadata={
                "ticket_type": ticket.ticket_type,
                "audience": scope.audience.value,
                "sla_due_at": due_at.isoformat() if due_at else None,
            },
        )


def _sla_due_by_ticket(
    db: Session, ticket_ids: list[UUID]
) -> dict[UUID, tuple[datetime | None, bool]]:
    """Map ticket id -> (sla due_at, breached) from its live SLA clock."""
    if not ticket_ids:
        return {}
    rows = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id.in_(ticket_ids))
        .filter(
            SlaClock.status.in_(
                (SlaClockStatus.running.value, SlaClockStatus.breached.value)
            )
        )
        .all()
    )
    due: dict[UUID, tuple[datetime | None, bool]] = {}
    for clock in rows:
        breached = (
            clock.status == SlaClockStatus.breached.value
            or clock.breached_at is not None
        )
        current = due.get(clock.entity_id)
        clock_due = as_utc(clock.due_at)
        if current is None:
            due[clock.entity_id] = (clock_due, breached)
            continue
        # Multiple clocks on one ticket: the tightest deadline wins.
        existing_due, existing_breached = current
        tighter = (
            clock_due
            if existing_due is None or (clock_due and clock_due < existing_due)
            else existing_due
        )
        due[clock.entity_id] = (tighter, existing_breached or breached)
    return due


ticket_provider = register(TicketProvider())
