"""SLA clock service for support tickets."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.support import Ticket, TicketStatus
from app.models.ticket_workflow import (
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    SlaPolicy,
    WorkflowEntityType,
)
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError

logger = logging.getLogger(__name__)

DEFAULT_TICKET_SLA_POLICY_NAME = "Ticket Resolution SLA"

SLA_COMPLETE_STATUSES = frozenset(
    {
        TicketStatus.resolved.value,
        TicketStatus.pending_confirmation.value,
        TicketStatus.closed.value,
        TicketStatus.canceled.value,
        TicketStatus.merged.value,
    }
)
SLA_APPLICABLE_STATUSES = frozenset(
    {
        TicketStatus.new.value,
        TicketStatus.open.value,
        TicketStatus.pending.value,
        TicketStatus.lastmile_rerun.value,
        TicketStatus.waiting_on_customer.value,
        TicketStatus.on_hold.value,
        TicketStatus.site_under_construction.value,
    }
)


class TicketSlaClockError(DomainError, ValueError):
    """Stable rejection from the support ticket SLA clock owner."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> TicketSlaClockError:
    return TicketSlaClockError(
        code=f"support.ticket_sla_clock.{suffix}",
        message=message,
        details=details,
    )


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_sla_policy(db: Session, ticket: Ticket) -> SlaPolicy | None:
    """Find the explicit default ticket SLA policy for a ticket."""
    policy = (
        db.query(SlaPolicy)
        .filter(SlaPolicy.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaPolicy.is_active.is_(True))
        .filter(func.lower(SlaPolicy.name) == DEFAULT_TICKET_SLA_POLICY_NAME.lower())
        .first()
    )
    if policy:
        return policy

    logger.warning(
        "ticket_sla_policy_not_found ticket_id=%s expected_policy_name=%s",
        getattr(ticket, "id", None),
        DEFAULT_TICKET_SLA_POLICY_NAME,
    )
    return None


def ticket_type_sla_target_minutes(
    db: Session,
    ticket_type: str | None,
) -> int | None:
    """Return the UI-configured resolution target for a ticket type."""
    normalized = " ".join(str(ticket_type or "").strip().lower().split())
    if not normalized:
        return None
    policy = support_ticket_settings_service.ticket_type_sla_policy(db)
    resolution_hours = next(
        (
            hours
            for configured_type, hours in policy.items()
            if " ".join(configured_type.strip().lower().split()) == normalized
        ),
        0,
    )
    return resolution_hours * 60 if resolution_hours > 0 else None


def priority_sla_target_minutes(db: Session, priority: str | None) -> int | None:
    """Return configured support resolution target minutes for a priority."""
    normalized = str(priority or "").strip().lower()
    if not normalized:
        return None
    policy = support_ticket_settings_service.sla_policy(db).get(normalized, {})
    resolution_hours = int(policy.get("resolution_hours") or 0)
    if resolution_hours <= 0:
        return None
    return resolution_hours * 60


def resolve_ticket_sla_target_minutes(
    db: Session,
    *,
    priority: str | None,
    ticket_type: str | None,
) -> int | None:
    """Resolve a ticket SLA target, preferring explicit operational windows."""
    return ticket_type_sla_target_minutes(
        db, ticket_type
    ) or priority_sla_target_minutes(db, priority)


def create_sla_clock_for_ticket(db: Session, ticket: Ticket) -> SlaClock | None:
    """Create an SLA clock for a ticket when a support SLA policy applies."""
    policy = resolve_sla_policy(db, ticket)
    if not policy:
        return None
    if str(ticket.status or "") not in SLA_APPLICABLE_STATUSES:
        return None

    explicit_type_target = ticket_type_sla_target_minutes(db, ticket.ticket_type)
    target_minutes = explicit_type_target or priority_sla_target_minutes(
        db, ticket.priority
    )
    if target_minutes is None:
        return None

    existing = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id == ticket.id)
        .filter(SlaClock.policy_id == policy.id)
        .first()
    )
    if existing:
        return existing

    started_at = _as_aware_utc(ticket.created_at) or datetime.now(UTC)
    due_at = (
        started_at + timedelta(minutes=target_minutes)
        if explicit_type_target is not None or ticket.due_at is None
        else _as_aware_utc(ticket.due_at)
    )
    if due_at is None:
        due_at = started_at + timedelta(minutes=target_minutes)
    clock = SlaClock(
        policy_id=policy.id,
        entity_type=WorkflowEntityType.ticket.value,
        entity_id=ticket.id,
        priority=str(ticket.priority or "") or None,
        status=SlaClockStatus.running.value,
        started_at=started_at,
        due_at=due_at,
    )
    db.add(clock)
    db.flush()
    # The breach deadline is a durable per-clock timer staged atomically
    # with the clock (ADR 0007 §7); its fired trigger drives the receipted
    # breach consumer. A paused, completed, or already-breached clock makes
    # a stale firing a state-guarded no-op.
    from app.services.owner_commands import CommandContext, owner_command_active
    from app.services.runtime_durable_timers import (
        ScheduleTimerCommand,
        schedule_timer,
    )

    if not owner_command_active(db):
        # Direct participant callers (tests, migrations) create the clock
        # without the owning ticket command; the timer is staged only where
        # the transition owner can commit it atomically.
        return clock
    schedule_timer(
        db,
        ScheduleTimerCommand(
            owner="support.ticket_lifecycle",
            entity_kind="sla_clock",
            entity_id=clock.id,
            purpose="sla_breach_due",
            due_at=due_at,
            output_event_type="support.ticket_sla_breach_due",
        ),
        context=CommandContext.system(
            actor="support.ticket_sla_clock",
            scope=str(clock.id),
            reason="ticket SLA breach deadline",
            idempotency_key=f"sla-breach:{clock.id}:{due_at.isoformat()}",
        ),
    )
    return clock


def latest_ticket_sla_clocks(
    db: Session, ticket_ids: Iterable[UUID | str]
) -> dict[str, SlaClock]:
    """Return the newest SLA clock per ticket id."""
    ids = [coerce_uuid(str(ticket_id)) for ticket_id in ticket_ids if ticket_id]
    ids = [ticket_id for ticket_id in ids if ticket_id is not None]
    if not ids:
        return {}
    clocks = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id.in_(ids))
        .order_by(SlaClock.entity_id.asc(), SlaClock.created_at.desc())
        .all()
    )
    latest: dict[str, SlaClock] = {}
    for clock in clocks:
        latest.setdefault(str(clock.entity_id), clock)
    return latest


def ticket_sla_status(
    ticket: Ticket,
    *,
    clock: SlaClock | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Build the read model used by support ticket UI/reporting."""
    current = _as_aware_utc(now) or datetime.now(UTC)
    due_at = _as_aware_utc(clock.due_at if clock else ticket.due_at)
    created_at = _as_aware_utc(ticket.created_at)
    terminal = str(ticket.status or "") in SLA_COMPLETE_STATUSES
    clock_breached = bool(
        clock
        and (
            clock.status == SlaClockStatus.breached.value
            or clock.breached_at is not None
        )
    )
    overdue = bool(due_at and due_at < current and not terminal)
    minutes_remaining = (
        int((due_at - current).total_seconds() // 60) if due_at is not None else None
    )
    return {
        "status": clock.status if clock else None,
        "priority": clock.priority if clock else ticket.priority,
        "started_at": clock.started_at if clock else created_at,
        "due_at": due_at,
        "breached": clock_breached or overdue,
        "breached_at": clock.breached_at if clock else None,
        "minutes_remaining": minutes_remaining,
        "age_hours": int((current - created_at).total_seconds() // 3600)
        if created_at
        else 0,
        "terminal": terminal,
    }


def update_sla_clocks_for_status_change(
    db: Session,
    ticket: Ticket,
    old_status: str | None,
    new_status: str,
) -> None:
    """Update SLA clocks when a ticket status changes."""
    del old_status
    clocks = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id == ticket.id)
        .filter(
            SlaClock.status.in_(
                [
                    SlaClockStatus.running.value,
                    SlaClockStatus.paused.value,
                    SlaClockStatus.breached.value,
                ]
            )
        )
        .all()
    )
    if not clocks:
        return

    now = datetime.now(UTC)
    for clock in clocks:
        if new_status in SLA_COMPLETE_STATUSES:
            clock.status = SlaClockStatus.completed.value
            clock.completed_at = now
            clock.paused_at = None
            open_breaches = (
                db.query(SlaBreach)
                .filter(SlaBreach.clock_id == clock.id)
                .filter(SlaBreach.status != SlaBreachStatus.resolved.value)
                .all()
            )
            for breach in open_breaches:
                breach.status = SlaBreachStatus.resolved.value
        elif new_status in SLA_APPLICABLE_STATUSES:
            clock.completed_at = None
            clock.paused_at = None
            if clock.status == SlaClockStatus.paused.value:
                clock.status = SlaClockStatus.running.value
    if new_status in SLA_COMPLETE_STATUSES:
        from app.models.operational_escalation import OperationalEntityType
        from app.services import operational_escalation

        operational_escalation.cancel_entity_events(
            db,
            entity_type=OperationalEntityType.ticket,
            entity_id=ticket.id,
            reason="ticket_sla_completed",
        )


def check_sla_breaches(db: Session, ticket_id) -> list[SlaClock]:
    """Check for SLA breaches on a ticket's running clocks."""
    now = datetime.now(UTC)
    normalized_ticket_id = coerce_uuid(str(ticket_id))
    if normalized_ticket_id is None:
        raise _error(
            "invalid_ticket_id",
            "A valid ticket identifier is required for SLA breach evaluation.",
            ticket_id=str(ticket_id),
        )
    ticket = db.get(Ticket, normalized_ticket_id)
    if not ticket or str(ticket.status or "") not in SLA_APPLICABLE_STATUSES:
        return []
    clocks = (
        db.query(SlaClock)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(SlaClock.entity_id == ticket.id)
        .filter(SlaClock.status == SlaClockStatus.running.value)
        .filter(SlaClock.due_at < now)
        .filter(SlaClock.breached_at.is_(None))
        .all()
    )

    breached = []
    for clock in clocks:
        due_at = (
            clock.due_at if clock.due_at.tzinfo else clock.due_at.replace(tzinfo=UTC)
        )
        clock.status = SlaClockStatus.breached.value
        clock.breached_at = due_at
        db.add(
            SlaBreach(
                clock_id=clock.id, status=SlaBreachStatus.open.value, breached_at=due_at
            )
        )
        from app.models.operational_escalation import OperationalEntityType
        from app.services import operational_escalation

        policies = operational_escalation.matching_policies(
            db,
            entity_type=OperationalEntityType.ticket,
            trigger="ticket.sla_breached",
            severity=str(ticket.priority or "") or None,
        )
        if policies:
            if ticket.assigned_to_person_id:
                operational_escalation.add_watcher(
                    db,
                    entity_type=OperationalEntityType.ticket,
                    entity_id=ticket.id,
                    person_id=ticket.assigned_to_person_id,
                    source="ticket_sla",
                    reason="Assigned ticket owner",
                )
            if ticket.service_team_id:
                operational_escalation.add_watcher(
                    db,
                    entity_type=OperationalEntityType.ticket,
                    entity_id=ticket.id,
                    service_team_id=ticket.service_team_id,
                    source="ticket_sla",
                    reason="Assigned ticket team",
                )
            operational_escalation.emit_sla_event(
                db,
                entity_type=OperationalEntityType.ticket,
                entity_id=ticket.id,
                trigger="ticket.sla_breached",
                severity=str(ticket.priority or "") or None,
                metadata={
                    "title": f"Ticket SLA breached: {ticket.title}",
                    "body": f"Ticket {ticket.id} passed its configured SLA due time.",
                    "target_url": f"/admin/support/tickets/{ticket.id}",
                    "category": "support",
                },
                triggered_at=due_at,
                policies=policies,
            )
        breached.append(clock)

    return breached
