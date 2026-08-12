"""Watermarked Sub -> ERP operational context feed.

ERP needs project/ticket/work-order context for expense and finance reporting,
but must no longer pull it from CRM. This feed is idempotent at ERP and advances
each local cursor only when the complete bulk request succeeds without errors.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.erp_domain_sync import ErpDomainSyncCursor
from app.models.project import Project, ProjectTask
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services.dotmac_erp.client import DotMacERPClient
from app.services.integrations.erp_capability import capability_client

_DOMAINS = ("projects", "project_tasks", "tickets", "work_orders")


def _cursor(db: Session, domain: str) -> ErpDomainSyncCursor:
    row = db.get(ErpDomainSyncCursor, domain)
    if row is None:
        row = ErpDomainSyncCursor(domain=domain)
        db.add(row)
        db.flush()
    return row


def _after_cursor(query, model, cursor: ErpDomainSyncCursor):
    if cursor.watermark_at is None:
        return query
    return query.filter(
        or_(
            model.updated_at > cursor.watermark_at,
            and_(
                model.updated_at == cursor.watermark_at,
                model.id > cursor.watermark_id,
            ),
        )
    )


def _projects(db: Session, cursor: ErpDomainSyncCursor, limit: int):
    return (
        _after_cursor(db.query(Project), Project, cursor)
        .order_by(Project.updated_at.asc(), Project.id.asc())
        .limit(limit)
        .all()
    )


def _tickets(db: Session, cursor: ErpDomainSyncCursor, limit: int):
    return (
        _after_cursor(db.query(Ticket), Ticket, cursor)
        .order_by(Ticket.updated_at.asc(), Ticket.id.asc())
        .limit(limit)
        .all()
    )


def _project_tasks(db: Session, cursor: ErpDomainSyncCursor, limit: int):
    rows = (
        _after_cursor(db.query(ProjectTask), ProjectTask, cursor)
        .order_by(ProjectTask.updated_at.asc(), ProjectTask.id.asc())
        .limit(limit)
        .all()
    )
    by_id = {row.id: row for row in rows}

    # A child can be newer than (or fall into a different page from) its parent.
    # Replay missing ancestors so ERP can create the hierarchy atomically. Cursor
    # advancement still uses the newest row, so replaying an old parent is safe.
    pending = list(rows)
    while pending:
        row = pending.pop()
        if row.parent_task_id and row.parent_task_id not in by_id:
            parent = db.get(ProjectTask, row.parent_task_id)
            if parent is not None:
                by_id[parent.id] = parent
                pending.append(parent)
    ordered: list[ProjectTask] = []
    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(row: ProjectTask) -> None:
        if row.id in visited:
            return
        if row.id in visiting:
            raise ValueError("project task hierarchy contains a cycle")
        visiting.add(row.id)
        parent = by_id.get(row.parent_task_id)
        if parent is not None:
            visit(parent)
        visiting.remove(row.id)
        visited.add(row.id)
        ordered.append(row)

    for row in by_id.values():
        visit(row)
    return ordered


def _work_orders(db: Session, cursor: ErpDomainSyncCursor, limit: int):
    return (
        _after_cursor(db.query(WorkOrder), WorkOrder, cursor)
        .order_by(WorkOrder.updated_at.asc(), WorkOrder.id.asc())
        .limit(limit)
        .all()
    )


def _project_payload(row: Project) -> dict:
    subscriber = row.subscriber
    customer_name = None
    if subscriber:
        customer_name = (
            f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        )
    return {
        "source_id": str(row.id),
        "name": row.name,
        "code": row.code,
        "project_type": row.project_type,
        "status": row.status,
        "priority": row.priority,
        "region": row.region,
        "description": row.description,
        "start_at": row.start_at.isoformat() if row.start_at else None,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "customer_name": customer_name or None,
        "customer_crm_id": str(row.subscriber_id) if row.subscriber_id else None,
        "metadata": {"source_system": "dotmac_sub", **(row.metadata_ or {})},
        "service_team_name": row.service_team.name if row.service_team else None,
    }


def _ticket_payload(row: Ticket) -> dict:
    channel = getattr(row.channel, "value", row.channel)
    return {
        "source_id": str(row.id),
        "subject": row.title,
        "ticket_number": row.number,
        "ticket_type": row.ticket_type,
        "status": row.status,
        "priority": row.priority,
        "description": row.description,
        "customer_crm_id": str(row.subscriber_id) if row.subscriber_id else None,
        "metadata": {
            "source_system": "dotmac_sub",
            "channel": str(channel) if channel else None,
            **(row.metadata_ or {}),
        },
        "comments": [],
        "activity_log": [],
    }


def _project_task_payload(row: ProjectTask) -> dict:
    return {
        "source_id": str(row.id),
        "project_source_id": str(row.project_id),
        "parent_task_source_id": str(row.parent_task_id)
        if row.parent_task_id
        else None,
        "ticket_source_id": str(row.ticket_id) if row.ticket_id else None,
        "title": row.title,
        "number": row.number,
        "description": row.description,
        "status": row.status,
        "priority": row.priority,
        "start_at": row.start_at.isoformat() if row.start_at else None,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "effort_hours": row.effort_hours,
        "metadata": {"source_system": "dotmac_sub", **(row.metadata_ or {})},
    }


def _work_order_payload(row: WorkOrder) -> dict:
    metadata = dict(row.metadata_ or {})
    metadata.update(
        {
            "source_system": "dotmac_sub",
            "address": row.address,
            "subscriber_id": str(row.subscriber_id),
        }
    )
    return {
        "source_id": str(row.id),
        "title": row.title,
        "work_type": row.work_type,
        "status": row.status,
        "priority": row.priority,
        "project_crm_id": str(row.project_id) if row.project_id else row.crm_project_id,
        "ticket_crm_id": (
            str(row.origin_ticket_id) if row.origin_ticket_id else row.crm_ticket_id
        ),
        "scheduled_start": (
            row.scheduled_start.isoformat() if row.scheduled_start else None
        ),
        "scheduled_end": row.scheduled_end.isoformat() if row.scheduled_end else None,
        "metadata": metadata,
    }


def _advance(cursor: ErpDomainSyncCursor, rows: list) -> None:
    if not rows:
        return
    last = max(rows, key=lambda row: (row.updated_at, row.id))
    cursor.watermark_at = last.updated_at
    cursor.watermark_id = last.id
    cursor.updated_at = datetime.now(UTC)


def sync_operational_domains(
    db: Session,
    *,
    client: DotMacERPClient | None = None,
    batch_size: int = 100,
    domains: tuple[str, ...] | None = None,
) -> dict:
    limit = max(1, min(int(batch_size or 100), 500))
    selected = tuple(dict.fromkeys(domains or _DOMAINS))
    if not selected:
        raise ValueError("at least one ERP operational domain is required")
    unknown = set(selected) - set(_DOMAINS)
    if unknown:
        raise ValueError(f"unsupported ERP operational domains: {sorted(unknown)}")
    cursors = {domain: _cursor(db, domain) for domain in selected}
    projects = (
        _projects(db, cursors["projects"], limit) if "projects" in selected else []
    )
    project_tasks = (
        _project_tasks(db, cursors["project_tasks"], limit)
        if "project_tasks" in selected
        else []
    )
    tickets = _tickets(db, cursors["tickets"], limit) if "tickets" in selected else []
    dependencies_catching_up = len(projects) >= limit or len(tickets) >= limit
    work_orders = (
        _work_orders(db, cursors["work_orders"], limit)
        if "work_orders" in selected and not dependencies_catching_up
        else []
    )
    if dependencies_catching_up:
        project_tasks = []
    if not projects and not project_tasks and not tickets and not work_orders:
        db.commit()
        return {
            "projects": 0,
            "project_tasks": 0,
            "tickets": 0,
            "work_orders": 0,
            "errors": [],
        }

    payload = {
        "projects": [_project_payload(row) for row in projects],
        "project_tasks": [_project_task_payload(row) for row in project_tasks],
        "tickets": [_ticket_payload(row) for row in tickets],
        "work_orders": [_work_order_payload(row) for row in work_orders],
    }
    owned_client = client or capability_client(db)
    created_client = client is None
    try:
        response = owned_client.sync_operational_domains(payload)
    finally:
        if created_client:
            owned_client.close()
    errors = response.get("errors") or []
    if project_tasks and response.get("contract_version") != 2:
        errors = [
            {
                "entity_type": "project_task",
                "error": "ERP bulk contract v2 is required for project task sync",
            }
        ]
    if errors:
        db.rollback()
        return {
            "projects": 0,
            "project_tasks": 0,
            "tickets": 0,
            "work_orders": 0,
            "errors": errors,
        }
    if "projects" in cursors:
        _advance(cursors["projects"], projects)
    if "project_tasks" in cursors:
        _advance(cursors["project_tasks"], project_tasks)
    if "tickets" in cursors:
        _advance(cursors["tickets"], tickets)
    if "work_orders" in cursors:
        _advance(cursors["work_orders"], work_orders)
    db.commit()
    return {
        "projects": len(projects),
        "project_tasks": len(project_tasks),
        "tickets": len(tickets),
        "work_orders": len(work_orders),
        "errors": [],
    }


def run_sync_operational_domains() -> dict[str, object]:
    """Own the background session for operational-domain ERP synchronization."""
    from app.db import task_session
    from app.models.integration_platform import IntegrationCapabilityBinding
    from app.services.integrations.backoffice_contracts import (
        ERP_OPERATIONAL_SYNC_CAPABILITY,
    )

    with task_session() as db:
        binding = (
            db.query(IntegrationCapabilityBinding)
            .filter(
                IntegrationCapabilityBinding.capability_id
                == ERP_OPERATIONAL_SYNC_CAPABILITY,
                IntegrationCapabilityBinding.state == "enabled",
            )
            .one_or_none()
        )
        if binding is None:
            return {
                "projects": 0,
                "project_tasks": 0,
                "tickets": 0,
                "work_orders": 0,
                "errors": [],
                "skipped": "capability_disabled",
            }
        domains = tuple((binding.scope_json or {}).get("domains") or _DOMAINS)
        batch_size = int((binding.policy_json or {}).get("batch_size") or 100)
        return sync_operational_domains(
            db,
            domains=domains,
            batch_size=batch_size,
        )
