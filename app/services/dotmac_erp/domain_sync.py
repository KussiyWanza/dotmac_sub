"""Watermarked Sub -> ERP operational context feed.

ERP needs project/ticket/project-task/work-order context for expense reporting,
but must no longer pull it from CRM. This feed is idempotent at ERP and advances
each local cursor only when the complete bulk request succeeds without errors.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.erp_domain_sync import ErpDomainSyncCursor, ErpOperationalSyncState
from app.models.project import Project, ProjectTask
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services.dotmac_erp.client import DotMacERPError, DotMacERPTransientError
from app.services.dotmac_erp.operational_contracts import (
    ErpOperationalSyncCommand,
    ErpOperationalSyncOutcome,
    ErpProjectProjection,
    ErpProjectTaskProjection,
    ErpTicketProjection,
    ErpWorkOrderProjection,
    OperationalSyncExecution,
    OperationalSyncRunOutcome,
)
from app.services.events import EventType, emit_event
from app.services.integrations import installations
from app.services.integrations.backoffice_contracts import (
    ERP_OPERATIONAL_SYNC_CAPABILITY,
)
from app.services.integrations.diagnostics import OperationDiagnostic, safe_diagnostic
from app.services.integrations.erp_capability import capability_client
from app.services.integrations.runtime_execution import RuntimeExecutionError

logger = logging.getLogger(__name__)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    current_command_context,
    execute_owner_command,
)

_DOMAINS = ("projects", "project_tasks", "tickets", "work_orders")
_SYNC_COMMAND = OwnerCommandDefinition(
    owner="integration.dotmac_erp_operational_context_adapter",
    concern="per-domain ERP operational-context delivery watermarks and retry admission",
    name="sync_operational_domains",
)


class OperationalSyncClient(Protocol):
    def sync_operational_domains(
        self, command: ErpOperationalSyncCommand
    ) -> ErpOperationalSyncOutcome: ...

    def close(self) -> None: ...


def _cursor(db: Session, domain: str) -> ErpDomainSyncCursor:
    row = db.scalar(
        select(ErpDomainSyncCursor)
        .where(ErpDomainSyncCursor.domain == domain)
        .with_for_update()
    )
    if row is None:
        row = ErpDomainSyncCursor(domain=domain)
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


def _project_payload(row: Project) -> ErpProjectProjection:
    subscriber = row.subscriber
    customer_name = None
    if subscriber:
        customer_name = (
            f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        )
    return ErpProjectProjection(
        source_id=row.id,
        name=row.name,
        code=row.code,
        project_type=row.project_type,
        status=row.status,
        priority=row.priority,
        region=row.region,
        description=row.description,
        start_at=row.start_at,
        due_at=row.due_at,
        customer_name=customer_name or None,
        customer_source_reference=row.subscriber_id,
        metadata={"source_system": "dotmac_sub", **(row.metadata_ or {})},
        service_team_name=row.service_team.name if row.service_team else None,
    )


def _ticket_payload(row: Ticket) -> ErpTicketProjection:
    channel = getattr(row.channel, "value", row.channel)
    return ErpTicketProjection(
        source_id=row.id,
        subject=row.title,
        ticket_number=row.number,
        ticket_type=row.ticket_type,
        status=row.status,
        priority=row.priority,
        description=row.description,
        customer_source_reference=row.subscriber_id,
        metadata={
            "source_system": "dotmac_sub",
            "channel": str(channel) if channel else None,
            **(row.metadata_ or {}),
        },
    )


def _project_task_payload(row: ProjectTask) -> ErpProjectTaskProjection:
    return ErpProjectTaskProjection(
        source_id=row.id,
        project_source_id=row.project_id,
        parent_task_source_id=row.parent_task_id,
        ticket_source_id=row.ticket_id,
        title=row.title,
        number=row.number,
        description=row.description,
        status=row.status,
        priority=row.priority,
        start_at=row.start_at,
        due_at=row.due_at,
        completed_at=row.completed_at,
        effort_hours=row.effort_hours,
        metadata={"source_system": "dotmac_sub", **(row.metadata_ or {})},
    )


def _work_order_payload(row: WorkOrder) -> ErpWorkOrderProjection:
    metadata = dict(row.metadata_ or {})
    metadata.update(
        {
            "source_system": "dotmac_sub",
            "address": row.address,
            "subscriber_id": str(row.subscriber_id),
        }
    )
    return ErpWorkOrderProjection(
        source_id=row.id,
        title=row.title,
        work_type=row.work_type,
        status=row.status,
        priority=row.priority,
        project_source_reference=(
            str(row.project_id) if row.project_id else row.crm_project_id
        ),
        ticket_source_reference=(
            str(row.origin_ticket_id) if row.origin_ticket_id else row.crm_ticket_id
        ),
        scheduled_start=row.scheduled_start,
        scheduled_end=row.scheduled_end,
        metadata=metadata,
    )


def _advance(db: Session, cursor: ErpDomainSyncCursor, rows: list) -> None:
    if not rows:
        return
    last = max(rows, key=lambda row: (row.updated_at, row.id))
    cursor.watermark_at = last.updated_at
    cursor.watermark_id = last.id
    cursor.updated_at = datetime.now(UTC)
    db.add(cursor)


def sync_operational_domains(
    db: Session,
    *,
    command: OperationalSyncExecution,
    context: CommandContext,
    client: OperationalSyncClient | None = None,
) -> OperationalSyncRunOutcome:
    """Execute one contracted watermark-owning sync command."""
    return execute_owner_command(
        db,
        definition=_SYNC_COMMAND,
        context=context,
        operation=lambda: _sync_operational_domains(db, command, client),
    )


def _sync_operational_domains(
    db: Session,
    command: OperationalSyncExecution,
    client: OperationalSyncClient | None,
) -> OperationalSyncRunOutcome:
    state = db.scalar(
        select(ErpOperationalSyncState)
        .where(ErpOperationalSyncState.id == 1)
        .with_for_update(skip_locked=True)
    )
    if state is None:
        if db.get(ErpOperationalSyncState, 1) is None:
            raise RuntimeError(
                "ERP sync admission state is missing; apply migration 579"
            )
        return OperationalSyncRunOutcome(
            projects=0,
            tickets=0,
            project_tasks=0,
            work_orders=0,
            status="already_running",
            skipped="already_running",
        )
    now = datetime.now(UTC)
    configuration = None
    configuration_error = False
    binding = None
    if client is None:
        try:
            binding = installations.require_enabled_capability_binding(
                db,
                capability_id=ERP_OPERATIONAL_SYNC_CAPABILITY,
            )
        except installations.InstallationError:
            configuration_error = True
        if binding is not None:
            configuration = {
                "binding": str(binding.id),
                "revision": str(binding.installation.current_config_revision_id),
                "manifest": binding.installation.manifest_digest,
                "scope": binding.scope_json,
                "policy": binding.policy_json,
            }
    fingerprint = hashlib.sha256(
        json.dumps(configuration, sort_keys=True).encode()
    ).hexdigest()
    retry_at = state.next_attempt_at
    if retry_at is not None and retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    if state.configuration_fingerprint == fingerprint and retry_at and now < retry_at:
        return OperationalSyncRunOutcome(
            projects=0,
            tickets=0,
            project_tasks=0,
            work_orders=0,
            status="retryable" if state.status == "retryable" else "blocked",
            skipped="retry_not_due",
            next_attempt_at=retry_at,
            diagnostic=OperationDiagnostic.model_validate(state.diagnostic)
            if state.diagnostic
            else None,
        )
    if state.configuration_fingerprint != fingerprint:
        state.failure_count = 0
    state.configuration_fingerprint = fingerprint
    state.last_attempt_at = now
    if configuration_error:
        return _record_failure(
            db, state, safe_diagnostic(code="configuration_unavailable"), now=now
        )
    if binding is not None:
        try:
            command = OperationalSyncExecution.model_validate(
                {
                    "domains": (binding.scope_json or {}).get("domains") or _DOMAINS,
                    "batch_size": (binding.policy_json or {}).get("batch_size") or 100,
                }
            )
        except ValidationError:
            return _record_failure(
                db, state, safe_diagnostic(code="configuration_unavailable"), now=now
            )
    limit = command.batch_size
    selected = tuple(dict.fromkeys(command.domains))
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
        state.status = "ready"
        state.failure_count = 0
        state.next_attempt_at = None
        state.diagnostic = None
        return OperationalSyncRunOutcome(
            projects=0, project_tasks=0, tickets=0, work_orders=0
        )

    try:
        outbound_command = ErpOperationalSyncCommand(
            projects=tuple(_project_payload(row) for row in projects),
            project_tasks=tuple(_project_task_payload(row) for row in project_tasks),
            tickets=tuple(_ticket_payload(row) for row in tickets),
            work_orders=tuple(_work_order_payload(row) for row in work_orders),
        )
    except ValidationError:
        return _record_failure(
            db, state, safe_diagnostic(code="validation_error"), now=now
        )
    owned_client = client or capability_client(db)
    created_client = client is None
    try:
        response = owned_client.sync_operational_domains(outbound_command)
    except DotMacERPError as exc:
        return _record_failure(
            db,
            state,
            exc.diagnostic,
            now=now,
            transient=isinstance(exc, DotMacERPTransientError),
        )
    except (installations.InstallationError, RuntimeExecutionError, ValidationError):
        return _record_failure(
            db,
            state,
            safe_diagnostic(code="configuration_unavailable"),
            now=now,
        )
    finally:
        if created_client:
            owned_client.close()
    errors = response.errors
    if errors:
        return _record_failure(
            db, state, safe_diagnostic(code="item_rejected"), now=now
        )
    state.status = "ready"
    state.failure_count = 0
    state.next_attempt_at = None
    state.diagnostic = None
    if "projects" in cursors:
        _advance(db, cursors["projects"], projects)
    if "project_tasks" in cursors:
        _advance(db, cursors["project_tasks"], project_tasks)
    if "tickets" in cursors:
        _advance(db, cursors["tickets"], tickets)
    if "work_orders" in cursors:
        _advance(db, cursors["work_orders"], work_orders)
    emit_event(
        db,
        EventType.erp_operational_context_watermark_advanced,
        {
            "domains": list(selected),
            "projects": len(projects),
            "project_tasks": len(project_tasks),
            "tickets": len(tickets),
            "work_orders": len(work_orders),
        },
        actor=current_command_context(db).actor,
        dispatch_after_commit=False,
    )
    return OperationalSyncRunOutcome(
        projects=len(projects),
        project_tasks=len(project_tasks),
        tickets=len(tickets),
        work_orders=len(work_orders),
    )


def _record_failure(
    db: Session,
    state: ErpOperationalSyncState,
    diagnostic: OperationDiagnostic,
    *,
    now: datetime,
    transient: bool = False,
) -> OperationalSyncRunOutcome:
    context = current_command_context(db)
    diagnostic = diagnostic.model_copy(
        update={
            "operation": "sync_operational_domains",
            "operation_id": diagnostic.operation_id or context.command_id,
            "correlation_id": diagnostic.correlation_id or context.correlation_id,
        }
    )
    state.failure_count += 1
    state.status = "retryable" if transient else "blocked"
    delay = (
        min(3600, 300 * 2 ** min(state.failure_count - 1, 4)) if transient else 21600
    )
    delay = max(delay, diagnostic.retry_after_seconds or 0)
    state.next_attempt_at = now + timedelta(seconds=delay)
    state.diagnostic = diagnostic.model_dump(mode="json")
    emit_event(
        db,
        EventType.erp_operational_context_retry_deferred,
        {
            "status": state.status,
            "next_attempt_at": state.next_attempt_at.isoformat(),
            "diagnostic": state.diagnostic,
        },
        actor=context.actor,
        dispatch_after_commit=False,
    )
    db.flush()
    logger.warning(
        "erp_operational_sync_blocked",
        extra={
            "sync_status": state.status,
            "next_attempt_at": state.next_attempt_at.isoformat(),
            "diagnostic": state.diagnostic,
        },
    )
    return OperationalSyncRunOutcome(
        projects=0,
        tickets=0,
        project_tasks=0,
        work_orders=0,
        status="retryable" if transient else "blocked",
        next_attempt_at=state.next_attempt_at,
        diagnostic=diagnostic,
    )


def run_sync_operational_domains() -> OperationalSyncRunOutcome:
    """Own the background session for operational-domain ERP synchronization."""
    from app.services.db_session_adapter import db_session_adapter

    with db_session_adapter.owner_command_session() as db:
        return sync_operational_domains(
            db,
            command=OperationalSyncExecution(),
            context=CommandContext.system(
                actor="erp-operational-sync-task",
                scope="erp-operational-context",
                reason="scheduled operational-context projection",
            ),
        )
