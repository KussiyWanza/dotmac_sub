"""Ticket SLA reporting helpers."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, distinct, func
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.support import Ticket, TicketStatus
from app.models.system_user import SystemUser
from app.models.ticket_workflow import (
    SlaBreach,
    SlaBreachStatus,
    SlaClock,
    SlaClockStatus,
    WorkflowEntityType,
)


@dataclass(frozen=True, slots=True)
class TicketSlaExportQuery:
    start_at: datetime | None = None
    end_at: datetime | None = None
    open_only: bool = False


@dataclass(frozen=True, slots=True)
class TicketSlaViolationPageQuery:
    start_at: datetime | None = None
    end_at: datetime | None = None
    open_only: bool = False
    page: int = 1
    per_page: int = 15


@dataclass(frozen=True, slots=True)
class TicketSlaViolationRecord:
    ticket_id: str
    ticket_reference: str
    ticket_url: str
    title: str
    status: str
    priority: str
    region: str
    service_team_id: str | None
    service_team: str
    assignee_person_id: str | None
    assignee: str
    sla_status: str
    started_at: datetime
    due_at: datetime
    breached_at: datetime
    breach_minutes: int
    breach_duration: str


@dataclass(frozen=True, slots=True)
class TicketSlaViolationPage:
    rows: tuple[TicketSlaViolationRecord, ...]
    page: int
    per_page: int
    total_count: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


@dataclass(frozen=True, slots=True)
class TicketSlaSummaryQuery:
    """Current operational ticket scope, optionally bounded by creation time."""

    start_at: datetime | None = None
    end_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TicketSlaMetricBucket:
    key: str
    label: str
    open_tickets: int
    currently_breaching: int
    breach_rate: float


@dataclass(frozen=True, slots=True)
class TicketSlaSummary:
    generated_at: datetime
    total_open_tickets: int
    total_currently_breaching: int
    current_breach_rate: float
    by_status: tuple[TicketSlaMetricBucket, ...]
    by_service_team: tuple[TicketSlaMetricBucket, ...]
    by_region: tuple[TicketSlaMetricBucket, ...]
    by_assignee: tuple[TicketSlaMetricBucket, ...]

    def as_serializable(self) -> dict[str, object]:
        def bucket_values(
            buckets: tuple[TicketSlaMetricBucket, ...],
        ) -> list[dict[str, object]]:
            return [
                {
                    "key": bucket.key,
                    "label": bucket.label,
                    "open_tickets": bucket.open_tickets,
                    "currently_breaching": bucket.currently_breaching,
                    "breach_rate": bucket.breach_rate,
                }
                for bucket in buckets
            ]

        return {
            "generated_at": self.generated_at.isoformat(),
            "total_open_tickets": self.total_open_tickets,
            "total_currently_breaching": self.total_currently_breaching,
            "current_breach_rate": self.current_breach_rate,
            "by_status": bucket_values(self.by_status),
            "by_service_team": bucket_values(self.by_service_team),
            "by_region": bucket_values(self.by_region),
            "by_assignee": bucket_values(self.by_assignee),
        }


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _duration_minutes(started_at: datetime | None, ended_at: datetime | None) -> int:
    start_value = _as_aware_utc(started_at)
    if start_value is None:
        return 0
    end_value = _as_aware_utc(ended_at) or datetime.now(UTC)
    return max(int((end_value - start_value).total_seconds() // 60), 0)


def _duration_label(minutes: int) -> str:
    if minutes <= 0:
        return "0m"
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def _ticket_clock_query(db: Session):
    return (
        db.query(SlaClock)
        .join(Ticket, Ticket.id == SlaClock.entity_id)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(Ticket.is_active.is_(True))
    )


def _apply_clock_window(query, start_at: datetime | None, end_at: datetime | None):
    if start_at:
        query = query.filter(SlaClock.started_at >= start_at)
    if end_at:
        query = query.filter(SlaClock.started_at <= end_at)
    return query


def _apply_ticket_window(query, start_at: datetime | None, end_at: datetime | None):
    if start_at:
        query = query.filter(Ticket.created_at >= start_at)
    if end_at:
        query = query.filter(Ticket.created_at <= end_at)
    return query


def _current_ticket_buckets(
    rows: list[tuple[object, object, int, int]],
    *,
    none_key: str,
    none_label: str,
) -> tuple[TicketSlaMetricBucket, ...]:
    buckets: list[TicketSlaMetricBucket] = []
    for key, label, open_tickets, currently_breaching in rows:
        open_count = int(open_tickets or 0)
        breach_count = int(currently_breaching or 0)
        buckets.append(
            TicketSlaMetricBucket(
                key=str(getattr(key, "value", key) or none_key),
                label=str(getattr(label, "value", label) or none_label),
                open_tickets=open_count,
                currently_breaching=breach_count,
                breach_rate=(
                    round(breach_count / open_count, 4) if open_count else 0.0
                ),
            )
        )
    return tuple(
        sorted(
            buckets,
            key=lambda item: (
                -item.currently_breaching,
                -item.open_tickets,
                item.label.lower(),
            ),
        )
    )


def summary(db: Session, *, query: TicketSlaSummaryQuery) -> TicketSlaSummary:
    """Project currently breaching tickets over the current open workload."""

    excluded_statuses = (
        TicketStatus.closed.value,
        TicketStatus.canceled.value,
        "merged",
    )
    ticket_base = _apply_ticket_window(
        db.query(Ticket)
        .filter(Ticket.is_active.is_(True))
        .filter(Ticket.status.notin_(excluded_statuses)),
        query.start_at,
        query.end_at,
    )
    clock_join = (SlaClock.entity_id == Ticket.id) & (
        SlaClock.entity_type == WorkflowEntityType.ticket.value
    )
    current_breach_filter = SlaClock.status == SlaClockStatus.breached.value
    current_breach_ticket_id = case((current_breach_filter, Ticket.id), else_=None)
    by_status = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .with_entities(
            Ticket.status,
            Ticket.status,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.status)
        .all()
    )
    by_team = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .outerjoin(ServiceTeam, ServiceTeam.id == Ticket.service_team_id)
        .with_entities(
            Ticket.service_team_id,
            ServiceTeam.name,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.service_team_id, ServiceTeam.name)
        .all()
    )
    by_region = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .with_entities(
            Ticket.region,
            Ticket.region,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.region)
        .all()
    )
    total_open_tickets = int(ticket_base.count())
    total_currently_breaching = int(
        ticket_base.outerjoin(SlaClock, clock_join)
        .with_entities(func.count(distinct(Ticket.id)))
        .filter(current_breach_filter)
        .scalar()
        or 0
    )
    by_assignee = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .outerjoin(SystemUser, SystemUser.id == Ticket.assigned_to_person_id)
        .with_entities(
            Ticket.assigned_to_person_id,
            SystemUser.display_name,
            func.count(distinct(Ticket.id)),
            func.count(distinct(current_breach_ticket_id)),
        )
        .group_by(Ticket.assigned_to_person_id, SystemUser.display_name)
        .all()
    )
    return TicketSlaSummary(
        generated_at=datetime.now(UTC),
        total_open_tickets=total_open_tickets,
        total_currently_breaching=total_currently_breaching,
        current_breach_rate=(
            round(total_currently_breaching / total_open_tickets, 4)
            if total_open_tickets
            else 0.0
        ),
        by_status=_current_ticket_buckets(
            by_status, none_key="unknown_status", none_label="Unknown Status"
        ),
        by_service_team=_current_ticket_buckets(
            by_team, none_key="unassigned_team", none_label="Unassigned Team"
        ),
        by_region=_current_ticket_buckets(
            by_region,
            none_key="unassigned_region",
            none_label="Unassigned Region",
        ),
        by_assignee=_current_ticket_buckets(
            by_assignee, none_key="unassigned_person", none_label="Unassigned Person"
        ),
    )


def trend_daily(
    db: Session, start_at: datetime | None = None, end_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Group ticket SLA clocks by start day."""
    base = _apply_clock_window(_ticket_clock_query(db), start_at, end_at)
    breached_expr = case(
        (
            (SlaClock.status == SlaClockStatus.breached.value)
            | SlaClock.breached_at.is_not(None),
            1,
        ),
        else_=0,
    )
    rows = (
        base.with_entities(
            func.date(SlaClock.started_at),
            func.count(SlaClock.id),
            func.sum(breached_expr),
        )
        .group_by(func.date(SlaClock.started_at))
        .order_by(func.date(SlaClock.started_at).asc())
        .all()
    )
    points: list[dict[str, Any]] = []
    for day_value, total, breached in rows:
        total_count = int(total or 0)
        breached_count = int(breached or 0)
        points.append(
            {
                "date": str(day_value),
                "total": total_count,
                "breached": breached_count,
                "breach_rate": round(
                    float(breached_count) / float(total_count) if total_count else 0.0,
                    4,
                ),
            }
        )
    return points


def violation_records(
    db: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    open_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List ticket SLA breach records for operational cleanup."""
    query = (
        db.query(SlaBreach, SlaClock, Ticket, ServiceTeam, SystemUser)
        .join(SlaClock, SlaClock.id == SlaBreach.clock_id)
        .join(Ticket, Ticket.id == SlaClock.entity_id)
        .outerjoin(ServiceTeam, ServiceTeam.id == Ticket.service_team_id)
        .outerjoin(SystemUser, SystemUser.id == Ticket.assigned_to_person_id)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(Ticket.is_active.is_(True))
    )
    if start_at:
        query = query.filter(SlaBreach.breached_at >= start_at)
    if end_at:
        query = query.filter(SlaBreach.breached_at <= end_at)
    if open_only:
        query = query.filter(SlaBreach.status != SlaBreachStatus.resolved.value)

    rows = (
        query.order_by(SlaBreach.breached_at.desc(), SlaBreach.created_at.desc())
        .limit(max(int(limit), 0))
        .all()
    )
    records: list[dict[str, Any]] = []
    for breach, clock, ticket, team, assignee in rows:
        ended_at = (
            clock.completed_at
            if breach.status == SlaBreachStatus.resolved.value
            else None
        )
        minutes = _duration_minutes(breach.breached_at, ended_at)
        reference = ticket.number or str(ticket.id)
        assignee_name = (
            assignee.display_name
            or " ".join(
                part for part in [assignee.first_name, assignee.last_name] if part
            ).strip()
            if assignee
            else ""
        )
        records.append(
            {
                "ticket_id": str(ticket.id),
                "ticket_reference": reference,
                "ticket_url": f"/admin/support/tickets/{reference}",
                "title": ticket.title,
                "status": ticket.status,
                "priority": ticket.priority,
                "region": ticket.region or "Unassigned",
                "service_team_id": str(ticket.service_team_id)
                if ticket.service_team_id
                else None,
                "service_team": team.name if team else "Unassigned",
                "assignee_person_id": str(ticket.assigned_to_person_id)
                if ticket.assigned_to_person_id
                else None,
                "assignee": assignee_name or "Unassigned",
                "sla_status": breach.status,
                "started_at": clock.started_at,
                "due_at": clock.due_at,
                "breached_at": breach.breached_at,
                "breach_minutes": minutes,
                "breach_duration": _duration_label(minutes),
            }
        )
    return records


def violation_page(
    db: Session, *, query: TicketSlaViolationPageQuery
) -> TicketSlaViolationPage:
    """Return one bounded page of ticket SLA breaches for the admin queue."""

    base = (
        db.query(SlaBreach, SlaClock, Ticket, ServiceTeam, SystemUser)
        .join(SlaClock, SlaClock.id == SlaBreach.clock_id)
        .join(Ticket, Ticket.id == SlaClock.entity_id)
        .outerjoin(ServiceTeam, ServiceTeam.id == Ticket.service_team_id)
        .outerjoin(SystemUser, SystemUser.id == Ticket.assigned_to_person_id)
        .filter(SlaClock.entity_type == WorkflowEntityType.ticket.value)
        .filter(Ticket.is_active.is_(True))
    )
    if query.start_at:
        base = base.filter(SlaBreach.breached_at >= query.start_at)
    if query.end_at:
        base = base.filter(SlaBreach.breached_at <= query.end_at)
    if query.open_only:
        base = base.filter(SlaBreach.status != SlaBreachStatus.resolved.value)

    per_page = max(int(query.per_page), 1)
    total_count = int(base.count())
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(max(int(query.page), 1), total_pages)
    raw_rows = (
        base.order_by(SlaBreach.breached_at.desc(), SlaBreach.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    records: list[TicketSlaViolationRecord] = []
    for breach, clock, ticket, team, assignee in raw_rows:
        ended_at = (
            clock.completed_at
            if breach.status == SlaBreachStatus.resolved.value
            else None
        )
        minutes = _duration_minutes(breach.breached_at, ended_at)
        reference = ticket.number or str(ticket.id)
        assignee_name = (
            assignee.display_name
            or " ".join(
                part for part in [assignee.first_name, assignee.last_name] if part
            ).strip()
            if assignee
            else ""
        )
        records.append(
            TicketSlaViolationRecord(
                ticket_id=str(ticket.id),
                ticket_reference=reference,
                ticket_url=f"/admin/support/tickets/{reference}",
                title=ticket.title,
                status=ticket.status,
                priority=ticket.priority,
                region=ticket.region or "Unassigned",
                service_team_id=(
                    str(ticket.service_team_id) if ticket.service_team_id else None
                ),
                service_team=team.name if team else "Unassigned",
                assignee_person_id=(
                    str(ticket.assigned_to_person_id)
                    if ticket.assigned_to_person_id
                    else None
                ),
                assignee=assignee_name or "Unassigned",
                sla_status=breach.status,
                started_at=clock.started_at,
                due_at=clock.due_at,
                breached_at=breach.breached_at,
                breach_minutes=minutes,
                breach_duration=_duration_label(minutes),
            )
        )
    return TicketSlaViolationPage(
        rows=tuple(records),
        page=page,
        per_page=per_page,
        total_count=total_count,
        total_pages=total_pages,
    )


def build_violation_export_csv(db: Session, query: TicketSlaExportQuery) -> str:
    """Export the complete matching SLA violation projection."""

    records = violation_records(
        db,
        start_at=query.start_at,
        end_at=query.end_at,
        open_only=query.open_only,
        limit=10000,
    )
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "ticket_reference",
            "title",
            "status",
            "service_team",
            "assignee",
            "due_at",
            "breached_at",
            "breach_duration",
            "priority",
        ),
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(
        {
            key: (
                getattr(value, "value", value).isoformat()
                if isinstance(getattr(value, "value", value), datetime)
                else getattr(value, "value", value)
            )
            for key, value in record.items()
        }
        for record in records
    )
    return output.getvalue()
