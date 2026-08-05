"""Authoritative handoff between support incidents and field work orders.

Support triage assigns a ticket to a service team. An active member of that
team may explicitly issue one or more field-action scopes. Native work-order
creation remains owned by ``operations.work_order_commands``; this service owns
handoff eligibility, idempotency, provenance, and the completion projection
back to the ticket timeline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.project import Project, ProjectTask
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.support import Ticket, TicketStatus
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.dispatch import WorkOrderHeaderCreate
from app.schemas.support import TicketWorkOrderIssueRequest
from app.services import service_address as service_address_service
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.ui_contracts import Action

HandoffErrorKind = Literal["invalid", "forbidden", "not_found", "conflict"]
_NON_ISSUABLE_STATUSES = frozenset(
    {
        TicketStatus.resolved.value,
        TicketStatus.closed.value,
        TicketStatus.canceled.value,
        TicketStatus.merged.value,
    }
)
WORK_ORDER_ISSUE_SCOPE = "support.ticket_work_order:issue"
_REQUIRED_PERMISSIONS = frozenset(
    {"support:ticket:update", "operations:dispatch:write"}
)
_ISSUE_DEFINITION = OwnerCommandDefinition(
    owner="support.ticket_work_order_handoff",
    concern="ticket-to-work-order issuance eligibility",
    name="issue_ticket_work_order",
)


class HandoffActorType(StrEnum):
    SYSTEM_USER = "system_user"
    API_KEY = "api_key"
    SERVICE = "service"


@dataclass(frozen=True)
class TicketWorkOrderIssueCommand:
    ticket_id: UUID
    request: TicketWorkOrderIssueRequest
    actor_id: UUID
    actor_type: HandoffActorType
    permissions: frozenset[str]
    context: CommandContext
    request_id: str | None = None


class TicketWorkOrderHandoffError(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: HandoffErrorKind = "conflict",
    ) -> None:
        super().__init__(code=code, message=message, details={"kind": kind})
        self.kind = kind


@dataclass(frozen=True)
class TicketWorkOrderIssueResult:
    work_order: WorkOrder
    replayed: bool


def _actor_type(actor_type: HandoffActorType) -> AuditActorType:
    principal_type = actor_type.value
    return {
        "api_key": AuditActorType.api_key,
        "service": AuditActorType.service,
        "system_user": AuditActorType.user,
        "subscriber": AuditActorType.user,
    }.get(principal_type, AuditActorType.system)


def _normalize_actor(actor_id: object | None) -> UUID:
    try:
        actor_uuid = coerce_uuid(actor_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise TicketWorkOrderHandoffError(
            "team_membership_required",
            "An authenticated member of the assigned team must issue field work",
            kind="forbidden",
        ) from exc
    if actor_uuid is None:
        raise TicketWorkOrderHandoffError(
            "team_membership_required",
            "An authenticated member of the assigned team must issue field work",
            kind="forbidden",
        )
    return actor_uuid


def _validate_issue_eligibility(
    db: Session,
    ticket: Ticket,
    *,
    actor_id: object | None,
) -> UUID:
    if not ticket.is_active:
        raise TicketWorkOrderHandoffError(
            "ticket_inactive", "Inactive tickets cannot issue field work"
        )
    if ticket.merged_into_ticket_id or ticket.status in _NON_ISSUABLE_STATUSES:
        raise TicketWorkOrderHandoffError(
            "ticket_terminal",
            f"Tickets in {ticket.status} status cannot issue field work",
        )
    if ticket.subscriber_id is None:
        raise TicketWorkOrderHandoffError(
            "ticket_subscriber_required",
            "Assign a subscriber before issuing field work",
            kind="invalid",
        )
    if ticket.service_team_id is None:
        raise TicketWorkOrderHandoffError(
            "ticket_team_required",
            "Assign a service team before issuing field work",
            kind="invalid",
        )
    team = db.get(ServiceTeam, ticket.service_team_id)
    if team is None or not team.is_active:
        raise TicketWorkOrderHandoffError(
            "ticket_team_inactive",
            "The assigned service team is not active",
            kind="conflict",
        )
    actor_uuid = _normalize_actor(actor_id)
    member = (
        db.query(ServiceTeamMember)
        .join(
            SystemUser,
            SystemUser.person_party_id == ServiceTeamMember.person_id,
        )
        .filter(ServiceTeamMember.team_id == ticket.service_team_id)
        .filter(ServiceTeamMember.is_active.is_(True))
        .filter(SystemUser.id == actor_uuid)
        .filter(SystemUser.is_active.is_(True))
        .one_or_none()
    )
    if member is None:
        raise TicketWorkOrderHandoffError(
            "assigned_team_membership_required",
            "Only an active member of the ticket's assigned team may issue field work",
            kind="forbidden",
        )
    return actor_uuid


def issue_action(
    db: Session,
    ticket: Ticket,
    *,
    actor_id: object | None,
) -> Action:
    try:
        _validate_issue_eligibility(db, ticket, actor_id=actor_id)
    except TicketWorkOrderHandoffError as exc:
        return Action(
            key="issue_work_order",
            label="Issue field work",
            allowed=False,
            reason=exc.message,
            permission="support:ticket:update",
        )
    return Action(
        key="issue_work_order",
        label="Issue field work",
        allowed=True,
        permission="support:ticket:update",
    )


def list_for_ticket(
    db: Session,
    ticket_id: object,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[WorkOrder]:
    query = (
        db.query(WorkOrder)
        .filter(WorkOrder.origin_ticket_id == coerce_uuid(ticket_id))
        .order_by(WorkOrder.created_at.asc(), WorkOrder.id.asc())
    )
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def issue_work_order(
    db: Session, command: TicketWorkOrderIssueCommand
) -> TicketWorkOrderIssueResult:
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)
    return execute_owner_command(
        db,
        definition=_ISSUE_DEFINITION,
        context=command.context,
        operation=lambda: _issue_work_order(db, command),
    )


def _issue_work_order(
    db: Session, command: TicketWorkOrderIssueCommand
) -> TicketWorkOrderIssueResult:
    if command.context.scope != WORK_ORDER_ISSUE_SCOPE:
        raise TicketWorkOrderHandoffError(
            "invalid_command_scope",
            "Ticket field-work issuance scope is invalid",
            kind="forbidden",
        )
    missing_permissions = _REQUIRED_PERMISSIONS - command.permissions
    if missing_permissions:
        raise TicketWorkOrderHandoffError(
            "permission_required",
            "Ticket update and dispatch write permissions are required",
            kind="forbidden",
        )
    key = str(command.context.idempotency_key or "").strip()
    if not key:
        raise TicketWorkOrderHandoffError(
            "idempotency_key_required",
            "Idempotency-Key is required",
            kind="invalid",
        )
    ticket = (
        db.query(Ticket)
        .filter(Ticket.id == command.ticket_id)
        .with_for_update()
        .one_or_none()
    )
    if ticket is None:
        raise TicketWorkOrderHandoffError(
            "ticket_not_found", "Ticket not found", kind="not_found"
        )
    actor_uuid = _validate_issue_eligibility(db, ticket, actor_id=command.actor_id)
    subscriber_id = ticket.subscriber_id
    if subscriber_id is None:  # Defensive narrowing; eligibility rejects this above.
        raise TicketWorkOrderHandoffError(
            "ticket_subscriber_required",
            "Assign a subscriber before issuing field work",
            kind="invalid",
        )
    command_key = f"ticket-work-order:{ticket.id}:{key}"
    stable_request_id = (
        f"ticket-wo-{str(ticket.id)[:8]}-"
        f"{hashlib.sha256(command_key.encode()).hexdigest()[:24]}"
    )
    payload = command.request
    description = payload.description or ticket.description
    scope_description = f"Issuance reason: {payload.reason}"
    if description:
        scope_description = f"{scope_description}\n\n{description}"

    from app.services import work_order_commands

    project_id = payload.project_id
    if payload.project_task_id is not None:
        project_task = db.get(ProjectTask, payload.project_task_id)
        if project_task is None or not project_task.is_active:
            raise TicketWorkOrderHandoffError(
                "project_task_not_found", "Project task not found", kind="not_found"
            )
        if project_task.ticket_id != ticket.id:
            raise TicketWorkOrderHandoffError(
                "project_task_ticket_mismatch",
                "Link this ticket to the project task before issuing field work",
                kind="invalid",
            )
        if project_id is not None and project_id != project_task.project_id:
            raise TicketWorkOrderHandoffError(
                "project_task_project_mismatch",
                "Project task does not belong to the selected project",
                kind="invalid",
            )
        project_id = project_task.project_id

    address = payload.address
    if not str(address or "").strip() and project_id is not None:
        project = db.get(Project, project_id)
        if project is not None and str(project.customer_address or "").strip():
            address = str(project.customer_address).strip()
    if not str(address or "").strip():
        address = service_address_service.format_address(
            service_address_service.service_address(db, subscriber_id)
        )

    work_order = work_order_commands.work_order_commands.create(
        db,
        WorkOrderHeaderCreate(
            title=payload.title or f"Field action — {ticket.title}"[:200],
            subscriber_id=subscriber_id,
            project_id=project_id,
            project_task_id=payload.project_task_id,
            requires_as_built_evidence=payload.requires_as_built_evidence,
            description=scope_description,
            status="draft",
            priority=payload.priority or ticket.priority,
            work_type=payload.work_type,
            address=address,
            scheduled_start=payload.scheduled_start,
            scheduled_end=payload.scheduled_end,
            estimated_duration_minutes=payload.estimated_duration_minutes,
            required_skills=payload.required_skills,
            tags=payload.tags,
            access_notes=payload.access_notes,
        ),
        auth={
            "principal_type": command.actor_type.value,
            "principal_id": str(command.actor_id),
        },
        request_id=stable_request_id,
        idempotency_key=command_key,
        origin_ticket_id=ticket.id,
        commit=False,
    )
    existing_audit = (
        db.query(AuditEvent)
        .filter(AuditEvent.action == "ticket.work_order_issued")
        .filter(AuditEvent.entity_type == "support_ticket")
        .filter(AuditEvent.entity_id == str(ticket.id))
        .filter(AuditEvent.request_id == stable_request_id)
        .one_or_none()
    )
    if existing_audit is not None:
        return TicketWorkOrderIssueResult(work_order=work_order, replayed=True)

    stage_audit_event(
        db,
        action="ticket.work_order_issued",
        entity_type="support_ticket",
        entity_id=str(ticket.id),
        actor_type=_actor_type(command.actor_type),
        actor_id=str(actor_uuid),
        request_id=stable_request_id,
        metadata={
            "owner": "support.ticket_work_order_handoff",
            "work_order_id": work_order.public_id,
            "project_id": str(work_order.project_id) if work_order.project_id else None,
            "project_task_id": str(work_order.project_task_id)
            if work_order.project_task_id
            else None,
            "assigned_team_id": str(ticket.service_team_id),
            "reason": payload.reason,
            "transport_request_id": command.request_id,
        },
    )
    from app.services.events import EventType, emit_event

    emit_event(
        db,
        EventType.ticket_work_order_issued,
        {
            "ticket_id": str(ticket.id),
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
            "assigned_team_id": str(ticket.service_team_id),
            "issued_by": str(actor_uuid),
        },
        actor=str(actor_uuid),
        subscriber_id=ticket.subscriber_id,
    )
    return TicketWorkOrderIssueResult(work_order=work_order, replayed=False)


def stage_field_outcome(
    db: Session,
    *,
    work_order: WorkOrder,
    field_event_id: UUID,
    event: str,
    occurred_at,
    note: str | None,
    actor_id: object | None,
) -> None:
    if work_order.origin_ticket_id is None:
        return
    ticket = db.get(Ticket, work_order.origin_ticket_id)
    if ticket is None:
        raise TicketWorkOrderHandoffError(
            "origin_ticket_not_found",
            "The work order's origin ticket no longer exists",
            kind="conflict",
        )
    outcome = "completed" if event == "complete" else "could not be completed"
    message = (
        f"Field work order {work_order.public_id} {outcome} at "
        f"{occurred_at.isoformat()}."
    )
    if note:
        message += f"\n\nField note: {note.strip()}"
    message += "\n\nSupport verification is required before resolving this ticket."

    from app.services import support as support_service

    support_service.ticket_comments.stage_system_projection(
        db,
        ticket=ticket,
        body=message,
        source="work_order_field_outcome",
        actor_id=str(actor_id) if actor_id else None,
        metadata={
            "work_order_id": work_order.public_id,
            "field_event_id": str(field_event_id),
            "outcome": event,
        },
    )


# --- receipted lifecycle-output consumption --------------------------------

_CONSUME_FIELD_OUTCOME_DEFINITION = OwnerCommandDefinition(
    owner="support.ticket_work_order_handoff",
    concern="committed field outcome consumption",
    name="consume_field_outcome",
)


def consume_field_outcome(
    db: Session,
    *,
    work_order_id: UUID,
    field_event_id: UUID,
    outcome: str,
    occurred_at,
    note: str | None,
    actor_id: object | None,
    event_id: UUID,
    context: CommandContext,
) -> str | None:
    """Receipt one committed field outcome into the ticket timeline.

    The projection and its unique ``(consumer, event_id)`` receipt commit
    atomically; a redelivery is an exact no-op. Field completion never
    changes ticket status — support verifies and resolves separately.
    """
    from app.services.events.owner_outputs import consume_owner_output

    def _effect() -> str:
        work_order = db.get(WorkOrder, work_order_id)
        if work_order is None:
            return "skipped_missing"
        stage_field_outcome(
            db,
            work_order=work_order,
            field_event_id=field_event_id,
            event=outcome,
            occurred_at=occurred_at,
            note=note,
            actor_id=actor_id,
        )
        return "applied"

    return execute_owner_command(
        db,
        definition=_CONSUME_FIELD_OUTCOME_DEFINITION,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer="support.ticket_work_order_handoff",
            event_id=event_id,
            event_type="work_order.field_outcome_recorded",
            producer_owner="operations.field_completion",
            context=context,
            operation=_effect,
        )[0],
    )
