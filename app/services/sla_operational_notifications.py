"""Shared SLA notification helpers for tickets, projects, and project tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.operational_escalation import OperationalEntityType
from app.models.project import Project, ProjectTask
from app.models.support import Ticket
from app.models.ticket_workflow import SlaClock
from app.services import operational_escalation

_DEFAULT_NEAR_BREACH_MINUTES = 30


def near_breach_window_seconds(
    db: Session,
    *,
    entity_type: str,
    trigger: str,
    severity: str | None = None,
) -> int | None:
    """Return the smallest active policy warning window for a near-breach event."""

    windows: list[int] = []
    for policy in operational_escalation.matching_policies(
        db,
        entity_type=entity_type,
        trigger=trigger,
        severity=severity,
    ):
        metadata = policy.metadata_ if isinstance(policy.metadata_, dict) else {}
        raw = metadata.get("near_breach_seconds") or metadata.get(
            "near_breach_window_seconds"
        )
        raw_minutes = metadata.get("near_breach_minutes")
        if raw is None:
            if raw_minutes is None:
                seconds = _DEFAULT_NEAR_BREACH_MINUTES * 60
            else:
                try:
                    seconds = int(raw_minutes) * 60
                except (TypeError, ValueError):
                    continue
        else:
            try:
                seconds = int(raw)
            except (TypeError, ValueError):
                continue
        if seconds > 0:
            windows.append(seconds)
    return min(windows) if windows else None


def near_breach_due_at(
    due_at: datetime,
    *,
    window_seconds: int,
) -> datetime | None:
    due = _as_aware_utc(due_at)
    if due is None:
        return None
    return due - timedelta(seconds=window_seconds)


def emit_ticket_created(db: Session, ticket: Ticket) -> None:
    _add_ticket_watchers(db, ticket)
    _emit(
        db,
        entity_type=OperationalEntityType.ticket,
        entity_id=ticket.id,
        trigger="ticket.created",
        severity=str(ticket.priority or "") or None,
        metadata=_ticket_metadata(ticket, title=f"Ticket created: {ticket.title}"),
    )


def emit_ticket_status_changed(
    db: Session,
    ticket: Ticket,
    *,
    previous_status: str | None,
) -> None:
    if previous_status == ticket.status:
        return
    _add_ticket_watchers(db, ticket)
    metadata = _ticket_metadata(
        ticket,
        title=f"Ticket status changed: {ticket.title}",
        body=(
            f"Ticket {_ticket_ref(ticket)} changed from "
            f"{previous_status or 'unknown'} to {ticket.status or 'unknown'}."
        ),
    )
    metadata["previous_status"] = previous_status
    _emit(
        db,
        entity_type=OperationalEntityType.ticket,
        entity_id=ticket.id,
        trigger="ticket.status_changed",
        severity=str(ticket.priority or "") or None,
        metadata=metadata,
    )


def emit_ticket_sla_near_breach(db: Session, ticket: Ticket, clock: SlaClock) -> None:
    _add_ticket_watchers(db, ticket)
    _emit(
        db,
        entity_type=OperationalEntityType.ticket,
        entity_id=ticket.id,
        trigger="ticket.sla_near_breach",
        severity=str(ticket.priority or "") or None,
        metadata=_ticket_metadata(
            ticket,
            title=f"Ticket SLA approaching breach: {ticket.title}",
            body=(
                f"Ticket {_ticket_ref(ticket)} is approaching its configured "
                "SLA deadline."
            ),
            clock=clock,
        ),
        triggered_at=datetime.now(UTC),
    )


def emit_project_created(db: Session, project: Project) -> None:
    _add_project_watchers(db, project)
    _emit(
        db,
        entity_type=OperationalEntityType.project,
        entity_id=project.id,
        trigger="project.created",
        severity=str(project.priority or project.project_type or "") or None,
        metadata=_project_metadata(project, title=f"Project created: {project.name}"),
    )


def emit_project_status_changed(
    db: Session,
    project: Project,
    *,
    previous_status: str | None,
) -> None:
    if previous_status == project.status:
        return
    _add_project_watchers(db, project)
    metadata = _project_metadata(
        project,
        title=f"Project status changed: {project.name}",
        body=(
            f"Project {_project_ref(project)} changed from "
            f"{previous_status or 'unknown'} to {project.status or 'unknown'}."
        ),
    )
    metadata["previous_status"] = previous_status
    _emit(
        db,
        entity_type=OperationalEntityType.project,
        entity_id=project.id,
        trigger="project.status_changed",
        severity=str(project.priority or project.project_type or "") or None,
        metadata=metadata,
    )


def emit_project_sla_near_breach(
    db: Session, project: Project, clock: SlaClock
) -> None:
    _add_project_watchers(db, project)
    _emit(
        db,
        entity_type=OperationalEntityType.project,
        entity_id=project.id,
        trigger="project.sla_near_breach",
        severity=str(project.priority or project.project_type or "") or None,
        metadata=_project_metadata(
            project,
            title=f"Project SLA approaching breach: {project.name}",
            body=(
                f"Project {_project_ref(project)} is approaching its configured "
                "SLA deadline."
            ),
            clock=clock,
        ),
        triggered_at=datetime.now(UTC),
    )


def emit_project_sla_breached(db: Session, project: Project, clock: SlaClock) -> None:
    _add_project_watchers(db, project)
    due_at = _as_aware_utc(clock.breached_at or clock.due_at) or datetime.now(UTC)
    _emit(
        db,
        entity_type=OperationalEntityType.project,
        entity_id=project.id,
        trigger="project.sla_breached",
        severity=str(project.priority or project.project_type or "") or None,
        metadata=_project_metadata(
            project,
            title=f"Project SLA breached: {project.name}",
            body=f"Project {_project_ref(project)} passed its configured SLA due time.",
            clock=clock,
        ),
        triggered_at=due_at,
    )


def emit_project_task_sla_near_breach(
    db: Session, task: ProjectTask, project: Project, clock: SlaClock
) -> None:
    _add_project_task_watchers(db, task, project)
    _emit(
        db,
        entity_type=OperationalEntityType.project_task,
        entity_id=task.id,
        trigger="project_task.sla_near_breach",
        severity=str(task.priority or "") or None,
        metadata=_project_task_metadata(
            task,
            project,
            title=f"Project task SLA approaching breach: {task.title}",
            body=(
                f"Task {_task_ref(task)} in project {_project_ref(project)} is "
                "approaching its configured SLA deadline."
            ),
            clock=clock,
        ),
        triggered_at=datetime.now(UTC),
    )


def _emit(
    db: Session,
    *,
    entity_type: str,
    entity_id: UUID,
    trigger: str,
    severity: str | None,
    metadata: dict[str, Any],
    triggered_at: datetime | None = None,
) -> None:
    policies = operational_escalation.matching_policies(
        db,
        entity_type=entity_type,
        trigger=trigger,
        severity=severity,
    )
    if not policies:
        return
    operational_escalation.emit_sla_event(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        trigger=trigger,
        severity=severity,
        metadata=metadata,
        triggered_at=triggered_at,
        policies=policies,
    )


def _add_ticket_watchers(db: Session, ticket: Ticket) -> None:
    if ticket.service_team_id:
        operational_escalation.add_watcher(
            db,
            entity_type=OperationalEntityType.ticket,
            entity_id=ticket.id,
            service_team_id=ticket.service_team_id,
            source="ticket_lifecycle",
            reason="Ticket service team",
        )
    for person_id in (
        ticket.assigned_to_person_id,
        ticket.ticket_manager_person_id,
        ticket.site_coordinator_person_id,
    ):
        if person_id:
            operational_escalation.add_watcher(
                db,
                entity_type=OperationalEntityType.ticket,
                entity_id=ticket.id,
                person_id=person_id,
                source="ticket_lifecycle",
                reason="Ticket operational assignee",
            )


def _add_project_watchers(db: Session, project: Project) -> None:
    team_id = getattr(project, "service_team_id", None)
    if team_id:
        operational_escalation.add_watcher(
            db,
            entity_type=OperationalEntityType.project,
            entity_id=project.id,
            service_team_id=team_id,
            source="project_lifecycle",
            reason="Project service team",
        )
    for person_id in (
        project.project_manager_person_id,
        project.assistant_manager_person_id,
        project.manager_person_id,
    ):
        if person_id:
            operational_escalation.add_watcher(
                db,
                entity_type=OperationalEntityType.project,
                entity_id=project.id,
                person_id=person_id,
                source="project_lifecycle",
                reason="Project delivery lead",
            )


def _add_project_task_watchers(
    db: Session, task: ProjectTask, project: Project
) -> None:
    team_id = getattr(project, "service_team_id", None)
    if team_id:
        operational_escalation.add_watcher(
            db,
            entity_type=OperationalEntityType.project_task,
            entity_id=task.id,
            service_team_id=team_id,
            source="project_task_lifecycle",
            reason="Project service team",
        )
    for person_id in (
        task.assigned_to_person_id,
        project.project_manager_person_id,
        project.assistant_manager_person_id,
        project.manager_person_id,
    ):
        if person_id:
            operational_escalation.add_watcher(
                db,
                entity_type=OperationalEntityType.project_task,
                entity_id=task.id,
                person_id=person_id,
                source="project_task_lifecycle",
                reason="Project delivery lead",
            )


def _ticket_metadata(
    ticket: Ticket,
    *,
    title: str,
    body: str | None = None,
    clock: SlaClock | None = None,
) -> dict[str, Any]:
    due_at = _as_aware_utc(clock.due_at if clock else ticket.due_at)
    return {
        "title": title,
        "body": body
        or f"Ticket {_ticket_ref(ticket)} needs Service Delivery attention.",
        "target_url": f"/admin/support/tickets/{ticket.id}",
        "category": "support",
        "ticket_id": str(ticket.id),
        "ticket_ref": _ticket_ref(ticket),
        "ticket_title": ticket.title,
        "status": ticket.status,
        "sla_deadline": due_at.isoformat() if due_at else None,
        "service_team_id": str(ticket.service_team_id)
        if ticket.service_team_id
        else None,
    }


def _project_metadata(
    project: Project,
    *,
    title: str,
    body: str | None = None,
    clock: SlaClock | None = None,
) -> dict[str, Any]:
    due_at = _as_aware_utc(clock.due_at if clock else project.due_at)
    return {
        "title": title,
        "body": body
        or f"Project {_project_ref(project)} needs Service Delivery attention.",
        "target_url": f"/admin/projects/{project.id}",
        "category": "operations",
        "project_id": str(project.id),
        "project_ref": _project_ref(project),
        "project_title": project.name,
        "status": project.status,
        "sla_deadline": due_at.isoformat() if due_at else None,
    }


def _project_task_metadata(
    task: ProjectTask,
    project: Project,
    *,
    title: str,
    body: str,
    clock: SlaClock | None = None,
) -> dict[str, Any]:
    metadata = _project_metadata(project, title=title, body=body, clock=clock)
    metadata.update(
        {
            "project_task_id": str(task.id),
            "project_task_ref": _task_ref(task),
            "project_task_title": task.title,
            "task_status": task.status,
        }
    )
    return metadata


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _ticket_ref(ticket: Ticket) -> str:
    return ticket.number or str(ticket.id)


def _project_ref(project: Project) -> str:
    return project.number or str(project.id)


def _task_ref(task: ProjectTask) -> str:
    return task.number or str(task.id)
