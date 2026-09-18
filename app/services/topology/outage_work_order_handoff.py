"""Issue one field work order for a shared infrastructure outage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentWorkOrderLink,
    OutageScopeRevision,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.network import (
    InfrastructureWorkOrderHeaderCreate,
    InfrastructureWorkOrderIssueRequest,
)
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.topology.outage import infrastructure_link_for, latest_scope_revision
from app.services.ui_contracts import Action

IssueErrorKind = Literal["invalid", "forbidden", "not_found", "conflict"]
ISSUE_SCOPE = "network.outage_work_order:issue"
REQUIRED_PERMISSIONS = frozenset({"monitoring:write", "operations:dispatch:write"})
_DEFINITION = OwnerCommandDefinition(
    owner="network.outage_work_order_handoff",
    concern="shared-outage work-order issuance eligibility",
    name="issue_infrastructure_work_order",
)
_ISSUABLE_STATUSES = frozenset({"open", "confirmed"})


class HandoffActorType(StrEnum):
    SYSTEM_USER = "system_user"
    API_KEY = "api_key"
    SERVICE = "service"


@dataclass(frozen=True)
class OutageWorkOrderIssueCommand:
    incident_id: UUID
    request: InfrastructureWorkOrderIssueRequest
    actor_id: UUID
    actor_type: HandoffActorType
    permissions: frozenset[str]
    context: CommandContext
    request_id: str | None = None


class OutageWorkOrderHandoffError(DomainError):
    def __init__(self, code: str, message: str, *, kind: IssueErrorKind = "conflict"):
        super().__init__(code=code, message=message, details={"kind": kind})
        self.kind = kind


@dataclass(frozen=True)
class OutageWorkOrderIssueResult:
    work_order: WorkOrder
    link: OutageIncidentWorkOrderLink
    replayed: bool


def _validate_team_member(db: Session, ticket: Ticket, *, actor_id: object) -> UUID:
    try:
        actor_uuid = coerce_uuid(actor_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise OutageWorkOrderHandoffError(
            "team_membership_required",
            "An active member of the assigned team must issue field work",
            kind="forbidden",
        ) from exc
    if actor_uuid is None or ticket.service_team_id is None:
        raise OutageWorkOrderHandoffError(
            "team_membership_required",
            "An active member of the assigned team must issue field work",
            kind="forbidden",
        )
    team = db.get(ServiceTeam, ticket.service_team_id)
    member = (
        db.query(ServiceTeamMember)
        .join(SystemUser, SystemUser.person_party_id == ServiceTeamMember.person_id)
        .filter(ServiceTeamMember.team_id == ticket.service_team_id)
        .filter(ServiceTeamMember.is_active.is_(True))
        .filter(SystemUser.id == actor_uuid)
        .filter(SystemUser.is_active.is_(True))
        .one_or_none()
    )
    if team is None or not team.is_active or member is None:
        raise OutageWorkOrderHandoffError(
            "assigned_team_membership_required",
            "Only an active member of the assigned team may issue field work",
            kind="forbidden",
        )
    return actor_uuid


def _validate_incident(
    db: Session, incident: OutageIncident, *, actor_id: object
) -> tuple[Ticket, OutageScopeRevision, UUID]:
    if incident.status not in _ISSUABLE_STATUSES:
        raise OutageWorkOrderHandoffError(
            "incident_not_issuable",
            "Only open or confirmed outages can receive field work",
            kind="invalid",
        )
    if not any(
        (incident.basestation_id, incident.fdh_cabinet_id, incident.root_node_id)
    ):
        raise OutageWorkOrderHandoffError(
            "incident_target_required",
            "The outage must have a resolved infrastructure target",
            kind="invalid",
        )
    link = infrastructure_link_for(db, incident.id)
    if link is None:
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_required",
            "Link the canonical infrastructure ticket before issuing field work",
            kind="invalid",
        )
    ticket = db.get(Ticket, link.ticket_id)
    if ticket is None or not ticket.is_active:
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_missing",
            "The canonical infrastructure ticket is unavailable",
            kind="conflict",
        )
    if ticket.merged_into_ticket_id or str(ticket.status) in {
        "TicketStatus.closed",
        "TicketStatus.canceled",
        "closed",
        "canceled",
        "merged",
    }:
        raise OutageWorkOrderHandoffError(
            "infrastructure_ticket_terminal",
            "The canonical infrastructure ticket is already closed",
            kind="invalid",
        )
    revision = latest_scope_revision(db, incident.id)
    if revision is None:
        raise OutageWorkOrderHandoffError(
            "scope_revision_required",
            "The outage audience must be calculated before field work is issued",
            kind="conflict",
        )
    return ticket, revision, _validate_team_member(db, ticket, actor_id=actor_id)


def issue_action(
    db: Session, incident: OutageIncident, *, actor_id: object | None
) -> Action:
    try:
        _validate_incident(db, incident, actor_id=actor_id)
    except OutageWorkOrderHandoffError as exc:
        return Action(
            key="issue_infrastructure_work_order",
            label="Create infrastructure work order",
            allowed=False,
            reason=exc.message,
            permission="operations:dispatch:write",
        )
    return Action(
        key="issue_infrastructure_work_order",
        label="Create infrastructure work order",
        allowed=True,
        permission="operations:dispatch:write",
    )


def list_for_incident(db: Session, incident_id: object) -> list[WorkOrder]:
    return (
        db.query(WorkOrder)
        .join(
            OutageIncidentWorkOrderLink,
            OutageIncidentWorkOrderLink.work_order_id == WorkOrder.id,
        )
        .filter(OutageIncidentWorkOrderLink.incident_id == coerce_uuid(incident_id))
        .order_by(WorkOrder.created_at.asc(), WorkOrder.id.asc())
        .all()
    )


def issue_work_order(
    db: Session, command: OutageWorkOrderIssueCommand
) -> OutageWorkOrderIssueResult:
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)
    return execute_owner_command(
        db,
        definition=_DEFINITION,
        context=command.context,
        operation=lambda: _issue_work_order(db, command),
    )


def _issue_work_order(
    db: Session, command: OutageWorkOrderIssueCommand
) -> OutageWorkOrderIssueResult:
    if command.context.scope != ISSUE_SCOPE:
        raise OutageWorkOrderHandoffError(
            "invalid_command_scope",
            "Infrastructure work-order scope is invalid",
            kind="forbidden",
        )
    missing = REQUIRED_PERMISSIONS - command.permissions
    if missing:
        raise OutageWorkOrderHandoffError(
            "permission_required",
            "Monitoring and dispatch write permissions are required",
            kind="forbidden",
        )
    key = str(command.context.idempotency_key or "").strip()
    if not key:
        raise OutageWorkOrderHandoffError(
            "idempotency_key_required", "Idempotency-Key is required", kind="invalid"
        )
    incident = (
        db.query(OutageIncident)
        .filter(OutageIncident.id == command.incident_id)
        .with_for_update()
        .one_or_none()
    )
    if incident is None:
        raise OutageWorkOrderHandoffError(
            "incident_not_found", "Outage not found", kind="not_found"
        )
    ticket, revision, actor_uuid = _validate_incident(
        db, incident, actor_id=command.actor_id
    )
    payload = command.request
    if (
        payload.expected_scope_revision_sequence is not None
        and payload.expected_scope_revision_sequence != revision.sequence
    ):
        raise OutageWorkOrderHandoffError(
            "scope_revision_stale",
            "The outage audience changed; refresh the page and try again",
            kind="conflict",
        )
    target_type = (
        "basestation"
        if incident.basestation_id
        else (
            "fdh-cabinet"
            if incident.fdh_cabinet_id
            else "node"
            if incident.root_node_id
            else "unresolved"
        )
    )
    target_id = (
        incident.basestation_id or incident.fdh_cabinet_id or incident.root_node_id
    )
    command_key = f"outage-work-order:{incident.id}:{key}"
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "incident_id": str(incident.id),
                "revision": revision.sequence,
                **payload.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    existing = (
        db.query(OutageIncidentWorkOrderLink)
        .filter(
            OutageIncidentWorkOrderLink.incident_id == incident.id,
            OutageIncidentWorkOrderLink.idempotency_key == command_key,
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.command_fingerprint != fingerprint:
            raise OutageWorkOrderHandoffError(
                "idempotency_conflict",
                "This work-order request key was already used",
                kind="conflict",
            )
        work_order = db.get(WorkOrder, existing.work_order_id)
        if work_order is None:
            raise OutageWorkOrderHandoffError(
                "linked_work_order_missing",
                "The linked work order is missing",
                kind="conflict",
            )
        return OutageWorkOrderIssueResult(
            work_order=work_order, link=existing, replayed=True
        )

    description = f"Issuance reason: {payload.reason}"
    if payload.description:
        description += f"\n\n{payload.description}"
    from app.services import work_order_commands

    work_order = (
        work_order_commands.work_order_commands.stage_infrastructure_work_order(
            db,
            InfrastructureWorkOrderHeaderCreate(
                title=payload.title,
                description=description,
                status=payload.status,
                priority=payload.priority,
                work_type=payload.work_type,
                address=payload.address,
                scheduled_start=payload.scheduled_start,
                scheduled_end=payload.scheduled_end,
                estimated_duration_minutes=payload.estimated_duration_minutes,
                required_skills=payload.required_skills,
                tags=payload.tags,
                access_notes=payload.access_notes,
                requires_as_built_evidence=payload.requires_as_built_evidence,
            ),
            origin_ticket_id=ticket.id,
            auth={
                "principal_type": command.actor_type.value,
                "principal_id": str(actor_uuid),
            },
            request_id=command.request_id or str(command.context.command_id),
            idempotency_key=command_key,
        )
    )
    link = OutageIncidentWorkOrderLink(
        incident_id=incident.id,
        work_order_id=work_order.id,
        scope_revision_id=revision.id,
        scope_revision_sequence=revision.sequence,
        membership_token=revision.membership_token,
        target_type=target_type,
        target_id=target_id,
        idempotency_key=command_key,
        command_fingerprint=fingerprint,
        created_by=str(actor_uuid),
    )
    db.add(link)
    stage_audit_event(
        db,
        action="outage.infrastructure_work_order_issued",
        entity_type="outage_incident",
        entity_id=str(incident.id),
        actor=AuditActor(actor_type=AuditActorType.user, actor_id=str(actor_uuid)),
        request_id=command.request_id or str(command.context.command_id),
        metadata={
            "owner": "network.outage_work_order_handoff",
            "work_order_id": work_order.public_id,
            "ticket_id": str(ticket.id),
            "scope_revision_sequence": revision.sequence,
            "membership_token": revision.membership_token,
            "target_type": target_type,
        },
    )
    emit_event(
        db,
        EventType.outage_infrastructure_work_order_issued,
        {
            "incident_id": str(incident.id),
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
            "ticket_id": str(ticket.id),
            "scope_revision_sequence": revision.sequence,
            "membership_token": revision.membership_token,
        },
        actor=str(actor_uuid),
    )
    db.flush()
    return OutageWorkOrderIssueResult(work_order=work_order, link=link, replayed=False)
