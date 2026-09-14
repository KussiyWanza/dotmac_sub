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
from app.models.support import Ticket
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


def _bucket_rows(rows, *, none_key: str = "unknown") -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    for key, total, breached in rows:
        total_count = int(total or 0)
        breached_count = int(breached or 0)
        buckets.append(
            {
                "key": str(getattr(key, "value", key) or none_key),
                "total": total_count,
                "breached": breached_count,
                "breach_rate": round(
                    float(breached_count) / float(total_count) if total_count else 0.0,
                    4,
                ),
            }
        )
    return buckets


def _labeled_bucket_rows(
    rows, *, none_key: str = "unknown", none_label: str = "Unassigned"
) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    for key, label, total, breached in rows:
        total_count = int(total or 0)
        breached_count = int(breached or 0)
        bucket_key = str(getattr(key, "value", key) or none_key)
        buckets.append(
            {
                "key": bucket_key,
                "label": str(label or none_label),
                "total": total_count,
                "breached": breached_count,
                "breach_rate": round(
                    float(breached_count) / float(total_count) if total_count else 0.0,
                    4,
                ),
            }
        )
    return buckets


def _apply_ticket_window(query, start_at: datetime | None, end_at: datetime | None):
    if start_at:
        query = query.filter(Ticket.created_at >= start_at)
    if end_at:
        query = query.filter(Ticket.created_at <= end_at)
    return query


def _ticket_buckets(rows, *, region: bool = False, none_key: str, none_label: str):
    buckets: list[dict[str, Any]] = []
    for row in rows:
        if region:
            key, total, breached, closed = row
            label = key or none_label
        else:
            key, label, total, breached, closed = row
        total_count = int(total or 0)
        breached_count = int(breached or 0)
        buckets.append(
            {
                "key": str(key or none_key),
                "label": str(label or none_label),
                "total": total_count,
                "breached": breached_count,
                "closed_breached": int(closed or 0),
                "breach_rate": round(breached_count / total_count, 4)
                if total_count
                else 0.0,
            }
        )
    return sorted(
        buckets,
        key=lambda item: (-item["breached"], -item["total"], item["label"].lower()),
    )


def summary(
    db: Session, start_at: datetime | None = None, end_at: datetime | None = None
) -> dict[str, Any]:
    """Summarize SLA clocks and distinct created tickets by ownership dimension."""
    base = _apply_clock_window(_ticket_clock_query(db), start_at, end_at)
    total_clocks = int(base.count())
    breach_filter = (
        SlaClock.status == SlaClockStatus.breached.value
    ) | SlaClock.breached_at.is_not(None)
    total_breaches = int(base.filter(breach_filter).count())
    breached_expr = case((breach_filter, 1), else_=0)
    by_status = (
        base.with_entities(
            SlaClock.status, func.count(SlaClock.id), func.sum(breached_expr)
        )
        .group_by(SlaClock.status)
        .all()
    )
    ticket_base = _apply_ticket_window(
        db.query(Ticket).filter(Ticket.is_active.is_(True)), start_at, end_at
    )
    clock_join = (SlaClock.entity_id == Ticket.id) & (
        SlaClock.entity_type == WorkflowEntityType.ticket.value
    )
    ticket_breach_id = case((breach_filter, Ticket.id), else_=None)
    closed_breach_id = case(
        (breach_filter & (Ticket.status == "closed"), Ticket.id), else_=None
    )
    by_team = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .outerjoin(ServiceTeam, ServiceTeam.id == Ticket.service_team_id)
        .with_entities(
            Ticket.service_team_id,
            ServiceTeam.name,
            func.count(distinct(Ticket.id)),
            func.count(distinct(ticket_breach_id)),
            func.count(distinct(closed_breach_id)),
        )
        .group_by(Ticket.service_team_id, ServiceTeam.name)
        .all()
    )
    by_region = (
        ticket_base.outerjoin(SlaClock, clock_join)
        .with_entities(
            Ticket.region,
            func.count(distinct(Ticket.id)),
            func.count(distinct(ticket_breach_id)),
            func.count(distinct(closed_breach_id)),
        )
        .group_by(Ticket.region)
        .all()
    )
    total_tickets = int(ticket_base.count())
    total_breached_tickets = int(
        ticket_base.outerjoin(SlaClock, clock_join)
        .filter(breach_filter)
        .distinct(Ticket.id)
        .count()
    )
    by_assignee = (
        base.outerjoin(SystemUser, SystemUser.id == Ticket.assigned_to_person_id)
        .with_entities(
            Ticket.assigned_to_person_id,
            SystemUser.display_name,
            func.count(SlaClock.id),
            func.sum(breached_expr),
        )
        .group_by(Ticket.assigned_to_person_id, SystemUser.display_name)
        .all()
    )
    return {
        "total_clocks": total_clocks,
        "total_breaches": total_breaches,
        "breach_rate": round(total_breaches / total_clocks, 4) if total_clocks else 0.0,
        "total_tickets": total_tickets,
        "total_breached_tickets": total_breached_tickets,
        "ticket_breach_rate": round(total_breached_tickets / total_tickets, 4)
        if total_tickets
        else 0.0,
        "by_status": _bucket_rows(by_status),
        "by_service_team": _ticket_buckets(
            by_team, none_key="unassigned_team", none_label="Unassigned Team"
        ),
        "by_region": _ticket_buckets(
            by_region,
            region=True,
            none_key="unassigned_region",
            none_label="Unassigned Region",
        ),
        "by_assignee": _labeled_bucket_rows(
            by_assignee, none_key="unassigned_person", none_label="Unassigned Person"
        ),
    }


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
