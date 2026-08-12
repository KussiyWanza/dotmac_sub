"""Native field material requests."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_material import (
    FIELD_MATERIAL_REQUEST_PRIORITIES,
    FIELD_MATERIAL_REQUEST_STATUSES,
    FieldInventoryItem,
    FieldInventoryWarehouse,
    FieldMaterialRequest,
    FieldMaterialRequestItem,
    FieldWorkOrderMaterial,
)
from app.models.project import Project, ProjectTask
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services.common import apply_pagination, coerce_uuid
from app.services.domain_errors import DomainError
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.field.source import (
    mark_sub_authoritative as _mark_source_authoritative,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

_BACKOFFICE_ISSUED_STATUSES = frozenset(
    {"issued", "fulfilled", "complete", "completed"}
)
_BACKOFFICE_REFUSED_STATUSES = frozenset(
    {"cancelled", "canceled", "rejected", "declined", "denied"}
)


class MaterialRequestStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    ACCEPTED_BY_ERP = "accepted_by_erp"
    PENDING_STOCK = "pending_stock"
    SYNC_FAILED = "sync_failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    ISSUED = "issued"
    FULFILLED = "fulfilled"
    CANCELED = "canceled"


class MaterialRequestPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class MaterialRequestFulfillmentChannel(StrEnum):
    MANUAL = "manual"
    ERP = "erp"


@dataclass(frozen=True, slots=True)
class MaterialRequestLineInput:
    item_id: UUID
    quantity: int
    notes: str | None = None
    serial_numbers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CreateStaffMaterialRequest:
    context: CommandContext
    work_order_id: UUID | None
    request_id: UUID
    priority: MaterialRequestPriority
    source_warehouse_code: str
    notes: str | None
    items: tuple[MaterialRequestLineInput, ...]
    scope: MaterialRequestScope | None = None
    fulfillment_channel: MaterialRequestFulfillmentChannel = (
        MaterialRequestFulfillmentChannel.ERP
    )
    work_order_public_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewMaterialRequest:
    context: CommandContext
    request_id: UUID
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ObserveErpMaterialStatus:
    context: CommandContext
    request_id: UUID
    provider_request_id: str
    provider_status: str
    observed_at: datetime
    serial_numbers_by_sequence: tuple[tuple[int, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class MaterialRequestItemView:
    id: UUID
    item_id: UUID
    sku: str | None
    name: str
    unit: str | None
    quantity: int
    notes: str | None
    serial_numbers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MaterialRequestView:
    id: UUID
    ticket_id: UUID | None
    project_id: UUID | None
    project_task_id: UUID | None
    work_order_id: UUID | None
    work_order_public_id: str | None
    work_order_title: str | None
    context_label: str
    requested_by_person_id: UUID
    requested_by_system_user_id: UUID | None
    status: MaterialRequestStatus
    priority: MaterialRequestPriority
    notes: str | None
    source_warehouse_code: str | None
    fulfillment_channel: MaterialRequestFulfillmentChannel
    support_system: str | None
    support_reference: str | None
    support_status: str | None
    submitted_at: datetime | None
    approved_at: datetime | None
    rejected_at: datetime | None
    fulfilled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    rejection_reason: str | None
    items: tuple[MaterialRequestItemView, ...]


@dataclass(frozen=True, slots=True)
class MaterialRequestPage:
    items: tuple[MaterialRequestView, ...]
    total: int
    page: int
    per_page: int


@dataclass(frozen=True, slots=True)
class MaterialRequestWorkOrderOption:
    id: UUID
    public_id: str
    title: str
    technician_id: UUID
    technician_label: str


@dataclass(frozen=True, slots=True)
class MaterialRequestWarehouseOption:
    code: str
    label: str


@dataclass(frozen=True, slots=True)
class MaterialRequestFormOptions:
    work_orders: tuple[MaterialRequestWorkOrderOption, ...]
    has_eligible_inventory_items: bool
    warehouses: tuple[MaterialRequestWarehouseOption, ...]
    context_label: str


@dataclass(frozen=True, slots=True)
class MaterialRequestScope:
    """Native navigation scope for the work order that owns a request."""

    ticket_id: UUID | None = None
    project_id: UUID | None = None
    project_task_id: UUID | None = None


class MaterialRequestError(DomainError):
    """Transport-neutral material dependency decision failure."""


_MATERIAL_APPROVAL_COMMAND = OwnerCommandDefinition(
    owner="operations.material_dependencies",
    concern="service-work-order material need and operational approval",
    name="approve_material_request",
)
_MATERIAL_REJECTION_COMMAND = OwnerCommandDefinition(
    owner="operations.material_dependencies",
    concern="service-work-order material need and operational approval",
    name="reject_material_request",
)
_MATERIAL_CANCELLATION_COMMAND = OwnerCommandDefinition(
    owner="operations.material_dependencies",
    concern="service-work-order material need and operational approval",
    name="cancel_material_request",
)
_MATERIAL_CREATE_COMMAND = OwnerCommandDefinition(
    owner="operations.material_dependencies",
    concern="contextual material need and ERP submission",
    name="create_staff_material_request",
)
_MATERIAL_OBSERVATION_COMMAND = OwnerCommandDefinition(
    owner="operations.material_dependencies",
    concern="ERP material status observation",
    name="observe_erp_material_status",
)


def _material_error(
    suffix: str, message: str, **details: object
) -> MaterialRequestError:
    return MaterialRequestError(
        code=f"operations.material_dependencies.{suffix}",
        message=message,
        details=details,
    )


def _context_label(request: FieldMaterialRequest) -> str:
    if request.project_task is not None:
        project = request.project
        project_label = (
            (project.number or project.code or project.name) if project else "Project"
        )
        return f"{project_label} → {request.project_task.title}"
    if request.ticket is not None:
        return f"Ticket {request.ticket.number or request.ticket.title}"
    if request.project is not None:
        return str(
            request.project.number or request.project.code or request.project.name
        )
    if request.work_order_mirror is not None:
        return f"Work order {request.work_order_mirror.public_id}"
    return "Material request"


def _request_view(request: FieldMaterialRequest) -> MaterialRequestView:
    metadata = request.metadata_ if isinstance(request.metadata_, dict) else {}
    return MaterialRequestView(
        id=request.id,
        ticket_id=request.ticket_id,
        project_id=request.project_id,
        project_task_id=request.project_task_id,
        work_order_id=request.work_order_mirror_id,
        work_order_public_id=(
            request.work_order_mirror.public_id if request.work_order_mirror else None
        ),
        work_order_title=(
            request.work_order_mirror.title if request.work_order_mirror else None
        ),
        context_label=_context_label(request),
        requested_by_person_id=request.requested_by_person_id,
        requested_by_system_user_id=request.requested_by_system_user_id,
        status=MaterialRequestStatus(request.status),
        priority=MaterialRequestPriority(request.priority),
        notes=request.notes,
        source_warehouse_code=request.source_warehouse_code,
        fulfillment_channel=MaterialRequestFulfillmentChannel(
            request.fulfillment_channel
        ),
        support_system=request.support_system,
        support_reference=request.support_reference,
        support_status=request.support_status,
        submitted_at=request.submitted_at,
        approved_at=request.approved_at,
        rejected_at=request.rejected_at,
        fulfilled_at=request.fulfilled_at,
        created_at=request.created_at,
        updated_at=request.updated_at,
        rejection_reason=(
            str(metadata["rejection_reason"])
            if metadata.get("rejection_reason")
            else None
        ),
        items=tuple(
            MaterialRequestItemView(
                id=line.id,
                item_id=line.item_id,
                sku=line.sku_snapshot or (line.item.sku if line.item else None),
                name=line.name_snapshot
                or (line.item.name if line.item else "Unavailable item"),
                unit=line.unit_snapshot or (line.item.unit if line.item else None),
                quantity=line.quantity,
                notes=line.notes,
                serial_numbers=tuple(
                    str(value) for value in (line.serial_numbers or ())
                ),
            )
            for line in request.items
        ),
    )


def _staff_request_query(db: Session):
    return (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.work_order_mirror),
            selectinload(FieldMaterialRequest.ticket),
            selectinload(FieldMaterialRequest.project),
            selectinload(FieldMaterialRequest.project_task),
            selectinload(FieldMaterialRequest.ticket),
            selectinload(FieldMaterialRequest.project),
            selectinload(FieldMaterialRequest.project_task),
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            ),
        )
        .filter(FieldMaterialRequest.is_active.is_(True))
    )


def list_staff_material_requests(
    db: Session,
    *,
    status: MaterialRequestStatus | None = None,
    work_order_public_id: str | None = None,
    scope: MaterialRequestScope | None = None,
    page: int = 1,
    per_page: int = 25,
) -> MaterialRequestPage:
    query = _staff_request_query(db)
    if status is not None:
        query = query.filter(FieldMaterialRequest.status == status.value)
    if work_order_public_id:
        query = query.join(FieldMaterialRequest.work_order_mirror).filter(
            WorkOrder.public_id == work_order_public_id.strip()
        )
    if scope is not None and any(
        (scope.ticket_id, scope.project_id, scope.project_task_id)
    ):
        if scope.ticket_id is not None:
            query = query.filter(
                or_(
                    FieldMaterialRequest.ticket_id == scope.ticket_id,
                    FieldMaterialRequest.work_order_mirror.has(
                        or_(
                            WorkOrder.origin_ticket_id == scope.ticket_id,
                            WorkOrder.project_task.has(
                                ProjectTask.ticket_id == scope.ticket_id
                            ),
                        )
                    ),
                )
            )
        if scope.project_id is not None:
            query = query.filter(
                or_(
                    FieldMaterialRequest.project_id == scope.project_id,
                    FieldMaterialRequest.work_order_mirror.has(
                        WorkOrder.project_id == scope.project_id
                    ),
                )
            )
        if scope.project_task_id is not None:
            query = query.filter(
                or_(
                    FieldMaterialRequest.project_task_id == scope.project_task_id,
                    FieldMaterialRequest.work_order_mirror.has(
                        WorkOrder.project_task_id == scope.project_task_id
                    ),
                )
            )
    total = int(query.with_entities(func.count(FieldMaterialRequest.id)).scalar() or 0)
    safe_page = max(1, page)
    safe_per_page = min(100, max(10, per_page))
    rows = (
        query.order_by(FieldMaterialRequest.created_at.desc())
        .offset((safe_page - 1) * safe_per_page)
        .limit(safe_per_page)
        .all()
    )
    return MaterialRequestPage(
        items=tuple(_request_view(row) for row in rows),
        total=total,
        page=safe_page,
        per_page=safe_per_page,
    )


def get_staff_material_request(db: Session, request_id: UUID) -> MaterialRequestView:
    row = (
        _staff_request_query(db)
        .filter(FieldMaterialRequest.id == request_id)
        .one_or_none()
    )
    if row is None:
        raise _material_error("request_not_found", "Material request was not found.")
    return _request_view(row)


def _technician_label(profile: TechnicianProfile) -> str:
    if profile.system_user is not None:
        label = str(profile.system_user.display_name or "").strip()
        if label:
            return label
        name = (
            f"{profile.system_user.first_name} {profile.system_user.last_name}".strip()
        )
        if name:
            return name
    return profile.crm_person_id or str(profile.person_id)


def staff_material_request_form_options(
    db: Session, *, scope: MaterialRequestScope | None = None
) -> MaterialRequestFormOptions:
    ticket_task_ids = (
        {
            row[0]
            for row in db.query(ProjectTask.id)
            .filter(
                ProjectTask.ticket_id == scope.ticket_id,
                ProjectTask.is_active.is_(True),
            )
            .all()
        }
        if scope is not None and scope.ticket_id is not None
        else set()
    )
    work_order_query = db.query(WorkOrder).filter(WorkOrder.is_active.is_(True))
    if scope is not None and scope.ticket_id is not None:
        work_order_query = work_order_query.filter(
            or_(
                WorkOrder.origin_ticket_id == scope.ticket_id,
                WorkOrder.project_task_id.in_(ticket_task_ids),
            )
        )
    if scope is not None and scope.project_id is not None:
        work_order_query = work_order_query.filter(
            WorkOrder.project_id == scope.project_id
        )
    if scope is not None and scope.project_task_id is not None:
        work_order_query = work_order_query.filter(
            WorkOrder.project_task_id == scope.project_task_id
        )
    available_work_orders = work_order_query.order_by(WorkOrder.created_at.desc()).all()
    work_orders: list[MaterialRequestWorkOrderOption] = []
    seen: set[UUID] = set()
    for work_order in available_work_orders:
        if work_order.id in seen:
            continue
        seen.add(work_order.id)
        work_orders.append(
            MaterialRequestWorkOrderOption(
                id=work_order.id,
                public_id=work_order.public_id,
                title=work_order.title,
                technician_id=work_order.id,
                technician_label="Optional context",
            )
        )
    has_eligible_inventory_items = (
        db.query(FieldInventoryItem.id)
        .filter(
            FieldInventoryItem.is_active.is_(True),
            FieldInventoryItem.source_is_active.is_(True),
            FieldInventoryItem.field_request_eligible.is_(True),
        )
        .limit(1)
        .first()
        is not None
    )
    return MaterialRequestFormOptions(
        work_orders=tuple(work_orders),
        has_eligible_inventory_items=has_eligible_inventory_items,
        warehouses=tuple(
            MaterialRequestWarehouseOption(
                code=row.code,
                label=f"{row.name} ({row.code})" if row.name != row.code else row.code,
            )
            for row in db.query(FieldInventoryWarehouse)
            .filter(
                FieldInventoryWarehouse.is_active.is_(True),
                FieldInventoryWarehouse.source_is_active.is_(True),
            )
            .order_by(FieldInventoryWarehouse.name.asc())
            .all()
        ),
        context_label=_scope_label(db, scope),
    )


def _scope_label(db: Session, scope: MaterialRequestScope | None) -> str:
    if scope is None:
        return "Choose a ticket, project, project task, or optional work order context."
    if scope.project_task_id:
        task = db.get(ProjectTask, scope.project_task_id)
        if task:
            project = db.get(Project, task.project_id)
            return f"{(project.number or project.code or project.name) if project else 'Project'} → {task.title}"
    if scope.ticket_id:
        ticket = db.get(Ticket, scope.ticket_id)
        if ticket:
            return f"Ticket {ticket.number or ticket.title}"
    if scope.project_id:
        project = db.get(Project, scope.project_id)
        if project:
            return str(project.number or project.code or project.name)
    return "Material request context"


def _command_fingerprint(command: CreateStaffMaterialRequest) -> str:
    scope = command.scope or MaterialRequestScope()
    payload = {
        "work_order_id": str(command.work_order_id) if command.work_order_id else None,
        "work_order_public_id": command.work_order_public_id,
        "ticket_id": str(scope.ticket_id) if scope.ticket_id else None,
        "project_id": str(scope.project_id) if scope.project_id else None,
        "project_task_id": str(scope.project_task_id)
        if scope.project_task_id
        else None,
        "fulfillment_channel": command.fulfillment_channel.value,
        "priority": command.priority.value,
        "source_warehouse_code": command.source_warehouse_code.strip(),
        "notes": (command.notes or "").strip(),
        "items": [
            {
                "item_id": str(line.item_id),
                "quantity": line.quantity,
                "notes": (line.notes or "").strip(),
                "serial_numbers": list(line.serial_numbers),
            }
            for line in command.items
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _locked_assigned_technician(
    db: Session, work_order_id: UUID
) -> tuple[WorkOrder, TechnicianProfile]:
    work_order = db.execute(
        select(WorkOrder)
        .where(WorkOrder.id == work_order_id, WorkOrder.is_active.is_(True))
        .with_for_update()
    ).scalar_one_or_none()
    if work_order is None:
        raise _material_error("work_order_not_found", "Work order was not found.")
    assignment = db.execute(
        select(WorkOrderAssignmentQueue)
        .where(
            WorkOrderAssignmentQueue.work_order_mirror_id == work_order.id,
            WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
            WorkOrderAssignmentQueue.assigned_technician_id.is_not(None),
        )
        .order_by(WorkOrderAssignmentQueue.updated_at.desc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    technician = (
        db.get(TechnicianProfile, assignment.assigned_technician_id)
        if assignment is not None
        else None
    )
    if technician is None or not technician.is_active:
        raise _material_error(
            "assignment_required",
            "Assign an active technician before requesting materials.",
            work_order_id=str(work_order.id),
        )
    return work_order, technician


def _system_user_id_for_actor(db: Session, context: CommandContext) -> UUID | None:
    raw = context.actor.rsplit(":", 1)[-1]
    try:
        actor_id = UUID(raw)
    except ValueError:
        return None
    from app.models.system_user import SystemUser

    return actor_id if db.get(SystemUser, actor_id) is not None else None


def _resolved_context(
    db: Session, command: CreateStaffMaterialRequest
) -> tuple[UUID | None, UUID | None, UUID | None, WorkOrder | None]:
    scope = command.scope or MaterialRequestScope()
    ticket_id = scope.ticket_id
    project_id = scope.project_id
    task_id = scope.project_task_id
    task = db.get(ProjectTask, task_id) if task_id else None
    if task_id and (task is None or not task.is_active):
        raise _material_error("context_not_found", "Project task was not found.")
    if task is not None:
        project_id = task.project_id
        ticket_id = ticket_id or task.ticket_id
    project = db.get(Project, project_id) if project_id else None
    if project_id and (project is None or not project.is_active):
        raise _material_error("context_not_found", "Project was not found.")
    ticket = db.get(Ticket, ticket_id) if ticket_id else None
    if ticket_id and (ticket is None or not ticket.is_active):
        raise _material_error("context_not_found", "Ticket was not found.")
    work_order = (
        db.get(WorkOrder, command.work_order_id) if command.work_order_id else None
    )
    if work_order is None and command.work_order_public_id:
        work_order = db.execute(
            select(WorkOrder)
            .where(
                WorkOrder.public_id == command.work_order_public_id,
                WorkOrder.is_active.is_(True),
            )
            .with_for_update()
        ).scalar_one_or_none()
    if (command.work_order_id or command.work_order_public_id) and (
        work_order is None or not work_order.is_active
    ):
        raise _material_error("work_order_not_found", "Work order was not found.")
    if work_order is not None:
        project_id = project_id or work_order.project_id
        task_id = task_id or work_order.project_task_id
        ticket_id = ticket_id or work_order.origin_ticket_id
    if not any((ticket_id, project_id, task_id, work_order)):
        raise _material_error(
            "context_required",
            "Start the material request from a ticket, project, project task, or work order.",
        )
    return ticket_id, project_id, task_id, work_order


def create_staff_material_request(
    db: Session, command: CreateStaffMaterialRequest
) -> MaterialRequestView:
    fingerprint = _command_fingerprint(command)

    def operation() -> MaterialRequestView:
        replay = (
            _staff_request_query(db)
            .filter(FieldMaterialRequest.client_ref == command.request_id)
            .one_or_none()
        )
        if replay is not None:
            metadata = replay.metadata_ if isinstance(replay.metadata_, dict) else {}
            if metadata.get("command_fingerprint") != fingerprint:
                raise _material_error(
                    "idempotency_conflict",
                    "Request identity was already used with different material details.",
                )
            return _request_view(replay)
        if not command.items:
            raise _material_error("invalid_request", "At least one item is required.")
        warehouse = command.source_warehouse_code.strip()
        if not warehouse:
            raise _material_error(
                "invalid_request", "Source warehouse code is required."
            )
        ticket_id, project_id, task_id, work_order = _resolved_context(db, command)
        system_user_id = _system_user_id_for_actor(db, command.context)
        if system_user_id is None:
            raise _material_error(
                "requester_required", "A local staff user is required."
            )
        from app.models.system_user import SystemUser

        system_user = db.get(SystemUser, system_user_id)
        if system_user is None:
            raise _material_error(
                "requester_required", "The requesting staff user no longer exists."
            )
        seen_items: set[UUID] = set()
        planned: list[tuple[FieldInventoryItem, MaterialRequestLineInput]] = []
        for line in command.items:
            if line.quantity <= 0:
                raise _material_error(
                    "invalid_request", "Item quantity must be greater than zero."
                )
            if line.item_id in seen_items:
                raise _material_error(
                    "invalid_request", "Each material item may appear only once."
                )
            seen_items.add(line.item_id)
            item = db.get(FieldInventoryItem, line.item_id)
            if (
                item is None
                or not item.is_active
                or not item.source_is_active
                or not item.field_request_eligible
            ):
                raise _material_error(
                    "material_item_not_found",
                    "A selected material item is unavailable.",
                    item_id=str(line.item_id),
                )
            serials = tuple(
                value.strip() for value in line.serial_numbers if value.strip()
            )
            if len(serials) != len(set(serials)):
                raise _material_error(
                    "invalid_request", "Serial numbers must be unique per item."
                )
            planned.append((item, line))
        now = datetime.now(UTC)
        request = FieldMaterialRequest(
            work_order_mirror_id=work_order.id if work_order else None,
            ticket_id=ticket_id,
            project_id=project_id,
            project_task_id=task_id,
            requested_by_technician_id=None,
            requested_by_person_id=(system_user.person_party_id or system_user.id),
            requested_by_system_user_id=system_user_id,
            status=MaterialRequestStatus.SUBMITTED.value,
            priority=command.priority.value,
            notes=(command.notes or "").strip() or None,
            source_warehouse_code=warehouse[:100],
            fulfillment_channel=command.fulfillment_channel.value,
            client_ref=command.request_id,
            submitted_at=now,
            metadata_={
                "command_fingerprint": fingerprint,
                "manager_events": [
                    {
                        "event": "staff_requested",
                        "occurred_at": now.isoformat(),
                        "actor": command.context.actor,
                        "command_id": str(command.context.command_id),
                    }
                ],
            },
        )
        db.add(request)
        db.flush()
        for item_row, line in planned:
            request.items.append(
                FieldMaterialRequestItem(
                    item=item_row,
                    source_item_id_snapshot=item_row.source_item_id,
                    sku_snapshot=item_row.sku,
                    name_snapshot=item_row.name,
                    unit_snapshot=item_row.unit,
                    quantity=line.quantity,
                    notes=(line.notes or "").strip() or None,
                    serial_numbers=[
                        value.strip() for value in line.serial_numbers if value.strip()
                    ],
                )
            )
        if work_order is not None:
            _mark_sub_authoritative(work_order)
        if command.fulfillment_channel == MaterialRequestFulfillmentChannel.ERP:
            from app.services.events import EventType, emit_event

            emit_event(
                db,
                EventType.field_material_request_approved,
                {
                    "material_request_id": str(request.id),
                    "work_order_mirror_id": str(request.work_order_mirror_id)
                    if request.work_order_mirror_id
                    else None,
                    "client_ref": str(request.client_ref),
                    "source_warehouse_code": request.source_warehouse_code,
                    "submitted_at": now.isoformat(),
                    "submission_mode": "immediate_erp",
                },
                actor=command.context.actor,
            )
        db.flush()
        return _request_view(request)

    return execute_owner_command(
        db,
        definition=_MATERIAL_CREATE_COMMAND,
        context=command.context,
        operation=operation,
    )


def _locked_request(db: Session, request_id: UUID) -> FieldMaterialRequest:
    request = db.execute(
        select(FieldMaterialRequest)
        .where(
            FieldMaterialRequest.id == request_id,
            FieldMaterialRequest.is_active.is_(True),
        )
        .options(
            selectinload(FieldMaterialRequest.work_order_mirror),
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            ),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise _material_error("request_not_found", "Material request was not found.")
    return request


def _is_command_replay(
    request: FieldMaterialRequest, *, event: str, command_id: UUID
) -> bool:
    metadata = request.metadata_ if isinstance(request.metadata_, dict) else {}
    events = metadata.get("manager_events")
    if not isinstance(events, list):
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("event") == event
        and entry.get("command_id") == str(command_id)
        for entry in events
    )


def approve_material_request(
    db: Session, command: ReviewMaterialRequest
) -> MaterialRequestView:
    def operation() -> MaterialRequestView:
        request = _locked_request(db, command.request_id)
        if (
            request.status == MaterialRequestStatus.APPROVED.value
            and _is_command_replay(
                request,
                event="approved",
                command_id=command.context.command_id,
            )
        ):
            return _request_view(request)
        if request.status != MaterialRequestStatus.SUBMITTED.value:
            raise _material_error(
                "invalid_transition", "Only submitted requests can be approved."
            )
        if not str(request.source_warehouse_code or "").strip():
            raise _material_error(
                "invalid_request", "Source warehouse code is required before approval."
            )
        request.status = MaterialRequestStatus.APPROVED.value
        request.approved_at = datetime.now(UTC)
        _note_request_event(
            request,
            "approved",
            actor=command.context.actor,
            command_id=command.context.command_id,
        )
        _mark_sub_authoritative(request.work_order_mirror)
        from app.services.events import EventType, emit_event

        emit_event(
            db,
            EventType.field_material_request_approved,
            {
                "material_request_id": str(request.id),
                "work_order_mirror_id": str(request.work_order_mirror_id),
                "client_ref": str(request.client_ref) if request.client_ref else None,
                "source_warehouse_code": request.source_warehouse_code,
                "approved_at": request.approved_at.isoformat(),
            },
            actor=command.context.actor,
        )
        db.flush()
        return _request_view(request)

    return execute_owner_command(
        db,
        definition=_MATERIAL_APPROVAL_COMMAND,
        context=command.context,
        operation=operation,
    )


def reject_material_request(
    db: Session, command: ReviewMaterialRequest
) -> MaterialRequestView:
    def operation() -> MaterialRequestView:
        request = _locked_request(db, command.request_id)
        if (
            request.status == MaterialRequestStatus.REJECTED.value
            and _is_command_replay(
                request,
                event="rejected",
                command_id=command.context.command_id,
            )
        ):
            return _request_view(request)
        if request.status != MaterialRequestStatus.SUBMITTED.value:
            raise _material_error(
                "invalid_transition", "Only submitted requests can be rejected."
            )
        reason = str(command.reason or "").strip()
        if not reason:
            raise _material_error("invalid_request", "A rejection reason is required.")
        request.status = MaterialRequestStatus.REJECTED.value
        request.rejected_at = datetime.now(UTC)
        _note_request_event(
            request,
            "rejected",
            reason=reason[:500],
            actor=command.context.actor,
            command_id=command.context.command_id,
        )
        _mark_sub_authoritative(request.work_order_mirror)
        db.flush()
        return _request_view(request)

    return execute_owner_command(
        db,
        definition=_MATERIAL_REJECTION_COMMAND,
        context=command.context,
        operation=operation,
    )


def cancel_material_request(
    db: Session, command: ReviewMaterialRequest
) -> MaterialRequestView:
    def operation() -> MaterialRequestView:
        request = _locked_request(db, command.request_id)
        if (
            request.status == MaterialRequestStatus.CANCELED.value
            and _is_command_replay(
                request,
                event="canceled",
                command_id=command.context.command_id,
            )
        ):
            return _request_view(request)
        if request.status not in {
            MaterialRequestStatus.DRAFT.value,
            MaterialRequestStatus.SUBMITTED.value,
        }:
            raise _material_error(
                "invalid_transition",
                "Only draft or submitted requests can be canceled.",
            )
        reason = str(command.reason or "").strip()
        if not reason:
            raise _material_error(
                "invalid_request", "A cancellation reason is required."
            )
        request.status = MaterialRequestStatus.CANCELED.value
        _note_request_event(
            request,
            "canceled",
            reason=reason[:500],
            actor=command.context.actor,
            command_id=command.context.command_id,
        )
        _mark_sub_authoritative(request.work_order_mirror)
        db.flush()
        return _request_view(request)

    return execute_owner_command(
        db,
        definition=_MATERIAL_CANCELLATION_COMMAND,
        context=command.context,
        operation=operation,
    )


def observe_erp_material_status(
    db: Session, command: ObserveErpMaterialStatus
) -> MaterialRequestView:
    def operation() -> MaterialRequestView:
        request = _locked_request(db, command.request_id)
        if request.fulfillment_channel != MaterialRequestFulfillmentChannel.ERP.value:
            raise _material_error(
                "manual_delivery_conflict",
                "A manual material request cannot accept an ERP issuance outcome.",
            )
        serials = dict(command.serial_numbers_by_sequence)
        for sequence, line in enumerate(request.items, start=1):
            if sequence in serials:
                line.serial_numbers = list(serials[sequence])
        field_material_requests.apply_backoffice_outcome(
            db,
            request,
            support_system="dotmac_erp",
            support_reference=command.provider_request_id,
            support_status=command.provider_status,
        )
        request.last_reconciled_at = command.observed_at
        db.flush()
        return _request_view(request)

    return execute_owner_command(
        db,
        definition=_MATERIAL_OBSERVATION_COMMAND,
        context=command.context,
        operation=operation,
    )


def serialize_material_request(request: FieldMaterialRequest) -> dict:
    return {
        "id": request.id,
        "work_order_id": (
            request.work_order_mirror.public_id if request.work_order_mirror else None
        ),
        "crm_material_request_id": request.crm_material_request_id,
        "requested_by_person_id": request.requested_by_person_id,
        "requested_by_system_user_id": request.requested_by_system_user_id,
        "status": request.status,
        "priority": request.priority,
        "notes": request.notes,
        "source_warehouse_code": request.source_warehouse_code,
        "support_system": request.support_system,
        "support_reference": request.support_reference,
        "support_status": request.support_status,
        "client_ref": request.client_ref,
        "submitted_at": request.submitted_at,
        "approved_at": request.approved_at,
        "rejected_at": request.rejected_at,
        "fulfilled_at": request.fulfilled_at,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
        "items": [
            {
                "id": item.id,
                "item_id": item.item_id,
                "sku": item.item.sku if item.item else None,
                "name": item.item.name if item.item else None,
                "unit": item.item.unit if item.item else None,
                "quantity": item.quantity,
                "notes": item.notes,
                "serial_numbers": item.serial_numbers or [],
            }
            for item in request.items
        ],
    }


class FieldMaterialRequests:
    @staticmethod
    def list_mine(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        profile = _profile_from_principal(db, principal)
        scoped = _scoped_query(db, profile)
        if crm_work_order_id:
            scoped = scoped.filter(WorkOrder.public_id == crm_work_order_id)
        scoped_ids = scoped.with_entities(WorkOrder.id)
        query = (
            db.query(FieldMaterialRequest)
            .options(
                selectinload(FieldMaterialRequest.items).selectinload(
                    FieldMaterialRequestItem.item
                )
            )
            .filter(FieldMaterialRequest.work_order_mirror_id.in_(scoped_ids))
            .filter(FieldMaterialRequest.is_active.is_(True))
            .order_by(FieldMaterialRequest.created_at.desc())
        )
        if status:
            query = query.filter(FieldMaterialRequest.status == _status(status))
        return [
            serialize_material_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def get(
        db: Session,
        principal: dict[str, Any],
        material_request_id: str,
    ) -> dict:
        request = _get_scoped_request(db, principal, material_request_id)
        return serialize_material_request(request)

    @staticmethod
    def create(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str,
        priority: str,
        notes: str | None,
        items: list[dict[str, Any]],
        source_warehouse_code: str | None = None,
    ) -> dict:
        if not items:
            raise HTTPException(status_code=422, detail="At least one item is required")
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        planned_items = _validate_items(db, items)
        request = FieldMaterialRequest(
            work_order_mirror_id=row.id,
            requested_by_technician_id=profile.id,
            requested_by_person_id=profile.person_id,
            requested_by_system_user_id=profile.system_user_id,
            status="draft",
            priority=_priority(priority),
            notes=(notes or "").strip() or None,
            source_warehouse_code=(source_warehouse_code or "").strip() or None,
        )
        db.add(request)
        db.flush()
        for item, quantity, notes, serial_numbers in planned_items:
            request.items.append(
                FieldMaterialRequestItem(
                    item_id=item.id,
                    quantity=quantity,
                    notes=notes,
                    serial_numbers=serial_numbers,
                )
            )
        _mark_sub_authoritative(row)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def submit(
        db: Session,
        principal: dict[str, Any],
        material_request_id: str,
    ) -> dict:
        request = _get_scoped_request(db, principal, material_request_id)
        profile = _profile_from_principal(db, principal)
        if request.requested_by_technician_id != profile.id:
            raise HTTPException(status_code=404, detail="Material request not found")
        if request.status != "draft":
            raise HTTPException(status_code=409, detail="Only draft requests submit")
        request.status = "submitted"
        request.submitted_at = datetime.now(UTC)
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def list_all(
        db: Session,
        *,
        status: str | None = None,
        crm_work_order_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Manager view: material requests across all technicians."""
        query = (
            db.query(FieldMaterialRequest)
            .options(
                selectinload(FieldMaterialRequest.items).selectinload(
                    FieldMaterialRequestItem.item
                )
            )
            .filter(FieldMaterialRequest.is_active.is_(True))
            .order_by(FieldMaterialRequest.created_at.desc())
        )
        if status:
            query = query.filter(FieldMaterialRequest.status == _status(status))
        if crm_work_order_id:
            work_order_ids = db.query(WorkOrder.id).filter(
                WorkOrder.public_id == crm_work_order_id
            )
            query = query.filter(
                FieldMaterialRequest.work_order_mirror_id.in_(work_order_ids)
            )
        return [
            serialize_material_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def approve(db: Session, material_request_id: str) -> dict:
        """Approve a submitted request and stage configured back-office support.

        The Sub decision remains valid if the replaceable back-office provider
        is unavailable. When an adapter is available, its outbox intent is
        committed atomically with this source transition.
        """
        request = _get_request(db, material_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests approve"
            )
        request.status = "approved"
        request.approved_at = datetime.now(UTC)
        _note_request_event(request, "approved")
        _mark_sub_authoritative(request.work_order_mirror)
        # The approval's committed output drives the receipted ERP-issue
        # request through the materials lifecycle projection handler; a
        # failed enqueue stays a failed retryable delivery instead of a
        # metadata breadcrumb.
        from app.services.events import EventType, emit_event

        emit_event(
            db,
            EventType.field_material_request_approved,
            {
                "material_request_id": str(request.id),
                "work_order_mirror_id": str(request.work_order_mirror_id),
                "client_ref": request.client_ref,
                "source_warehouse_code": request.source_warehouse_code,
                "approved_at": request.approved_at.isoformat(),
            },
            actor="operations.material_dependencies",
        )
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def reject(db: Session, material_request_id: str, reason: str) -> dict:
        request = _get_request(db, material_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests reject"
            )
        cleaned = (reason or "").strip()
        if not cleaned:
            raise HTTPException(status_code=422, detail="reason is required")
        request.status = "rejected"
        request.rejected_at = datetime.now(UTC)
        _note_request_event(request, "rejected", reason=cleaned[:500])
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def issue(db: Session, material_request_id: str) -> dict:
        _require_legacy_material_fulfilment(db)
        request = _get_request(db, material_request_id)
        if request.status != "approved":
            raise HTTPException(status_code=409, detail="Only approved requests issue")
        _sync_work_order_materials(db, request, status="reserved")
        request.status = "issued"
        _note_request_event(request, "issued")
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def fulfill(db: Session, material_request_id: str) -> dict:
        _require_legacy_material_fulfilment(db)
        request = _get_request(db, material_request_id)
        if request.status not in {"approved", "issued"}:
            raise HTTPException(
                status_code=409, detail="Only approved or issued requests fulfill"
            )
        _sync_work_order_materials(db, request, status="reserved")
        request.status = "fulfilled"
        request.fulfilled_at = datetime.now(UTC)
        _note_request_event(request, "fulfilled")
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def apply_backoffice_outcome(
        db: Session,
        request: FieldMaterialRequest,
        *,
        support_system: str,
        support_reference: str | None,
        support_status: str | None,
    ) -> bool:
        """Project one back-office material outcome into the service workflow.

        The configured provider decides stock availability, serial allocation,
        issue, and cancellation. This resolver is Sub's only writer for the
        resulting material-dependency state.
        """
        changed = False
        normalized_status = _normalize_backoffice_status(support_status)
        normalized_system = str(support_system or "").strip().lower()[:40]
        if not normalized_system:
            raise ValueError("Back-office support outcome requires a source system")

        if request.support_system not in {None, normalized_system}:
            raise ValueError(
                "Back-office support system changed for "
                f"{request.id}: {request.support_system} -> {normalized_system}"
            )
        if request.support_system != normalized_system:
            request.support_system = normalized_system
            changed = True

        if support_reference:
            normalized_id = str(support_reference)[:120]
            if request.support_reference not in {None, normalized_id}:
                raise ValueError(
                    "Back-office material request identity changed for "
                    f"{request.id}: {request.support_reference} -> {normalized_id}"
                )
            if request.support_reference != normalized_id:
                request.support_reference = normalized_id
                changed = True

        if normalized_status and request.support_status != normalized_status:
            request.support_status = normalized_status
            changed = True

        if (
            normalized_status in {"draft", "submitted", "accepted"}
            and request.status == "submitted"
        ):
            request.status = "accepted_by_erp"
            changed = True
        elif normalized_status in {
            "pending_stock",
            "partially_ordered",
            "ordered",
        } and request.status in {"submitted", "accepted_by_erp"}:
            request.status = "pending_stock"
            changed = True

        if normalized_status in _BACKOFFICE_ISSUED_STATUSES:
            _sync_work_order_materials(db, request, status="reserved")
            if request.status in {
                "submitted",
                "approved",
                "accepted_by_erp",
                "pending_stock",
            }:
                request.status = "issued"
                request.issued_at = request.issued_at or datetime.now(UTC)
                _note_request_event(request, "backoffice_material_issued")
                from app.services.events import EventType, emit_event

                emit_event(
                    db,
                    EventType.field_material_request_fulfilled,
                    {
                        "material_request_id": str(request.id),
                        "work_order_mirror_id": str(request.work_order_mirror_id)
                        if request.work_order_mirror_id
                        else None,
                        "support_system": request.support_system,
                        "support_reference": request.support_reference,
                        "support_status": request.support_status,
                    },
                    actor="operations.material_dependencies",
                )
                changed = True
        elif normalized_status in _BACKOFFICE_REFUSED_STATUSES:
            if request.status in {
                "submitted",
                "approved",
                "accepted_by_erp",
                "pending_stock",
            }:
                request.status = "canceled"
                _note_request_event(
                    request,
                    "backoffice_material_refused",
                    reason=f"Back-office outcome: {normalized_status}",
                )
                changed = True

        if changed:
            request.last_reconciled_at = datetime.now(UTC)
            if request.work_order_mirror is not None:
                _mark_sub_authoritative(request.work_order_mirror)
        return changed


def _get_scoped_request(
    db: Session,
    principal: dict[str, Any],
    material_request_id: str,
) -> FieldMaterialRequest:
    profile = _profile_from_principal(db, principal)
    scoped_ids = _scoped_query(db, profile).with_entities(WorkOrder.id)
    request = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.id == coerce_uuid(material_request_id))
        .filter(FieldMaterialRequest.work_order_mirror_id.in_(scoped_ids))
        .filter(FieldMaterialRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Material request not found")
    return request


def _get_request(db: Session, material_request_id: str) -> FieldMaterialRequest:
    request = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.id == coerce_uuid(material_request_id))
        .filter(FieldMaterialRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Material request not found")
    return request


def _note_request_event(
    request: FieldMaterialRequest,
    event: str,
    *,
    reason: str | None = None,
    actor: str | None = None,
    command_id: UUID | None = None,
) -> None:
    metadata = dict(request.metadata_ or {})
    events = list(metadata.get("manager_events") or [])
    event_payload: dict[str, Any] = {
        "event": event,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if reason:
        event_payload["reason"] = reason
    if actor:
        event_payload["actor"] = actor
    if command_id:
        event_payload["command_id"] = str(command_id)
    events.append(event_payload)
    metadata["manager_events"] = events[-20:]
    if reason:
        metadata["rejection_reason"] = reason
    request.metadata_ = metadata


def _sync_work_order_materials(
    db: Session, request: FieldMaterialRequest, *, status: str
) -> None:
    if request.work_order_mirror_id is None:
        return
    existing = {
        row.item_id: row
        for row in db.query(FieldWorkOrderMaterial)
        .filter(
            FieldWorkOrderMaterial.work_order_mirror_id == request.work_order_mirror_id
        )
        .filter(FieldWorkOrderMaterial.is_active.is_(True))
        .all()
    }
    for requested_item in request.items:
        row = existing.get(requested_item.item_id)
        if row is None:
            row = FieldWorkOrderMaterial(
                work_order_mirror_id=request.work_order_mirror_id,
                item_id=requested_item.item_id,
                allocated_quantity=requested_item.quantity,
                consumed_quantity=0,
                status=status,
                notes=requested_item.notes,
                metadata_={"material_request_id": str(request.id)},
            )
            db.add(row)
            continue
        row.allocated_quantity = max(row.allocated_quantity, requested_item.quantity)
        row.status = (
            "used" if row.consumed_quantity >= row.allocated_quantity else status
        )
        if requested_item.notes:
            row.notes = requested_item.notes
        metadata = dict(row.metadata_ or {})
        metadata["material_request_id"] = str(request.id)
        row.metadata_ = metadata


def _item(db: Session, item_id) -> FieldInventoryItem:
    item = db.get(FieldInventoryItem, item_id)
    if item is None or not item.is_active:
        raise HTTPException(status_code=404, detail="Material item not found")
    return item


def _validate_items(
    db: Session, items: list[dict[str, Any]]
) -> list[tuple[FieldInventoryItem, int, str | None, list[str]]]:
    planned: list[tuple[FieldInventoryItem, int, str | None, list[str]]] = []
    seen: set[str] = set()
    for entry in items:
        item = _item(db, entry.get("item_id"))
        item_key = str(item.id)
        if item_key in seen:
            raise HTTPException(status_code=422, detail="Duplicate item_id in request")
        seen.add(item_key)
        serial_numbers = [
            str(value).strip()
            for value in (entry.get("serial_numbers") or [])
            if str(value).strip()
        ]
        if len(serial_numbers) != len(set(serial_numbers)):
            raise HTTPException(status_code=422, detail="Duplicate serial number")
        planned.append(
            (
                item,
                _quantity(entry.get("quantity")),
                (entry.get("notes") or "").strip() or None,
                serial_numbers,
            )
        )
    return planned


def _quantity(value) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="quantity must be an integer"
        ) from exc
    if quantity <= 0:
        raise HTTPException(
            status_code=422, detail="quantity must be greater than zero"
        )
    return quantity


def _priority(value: str) -> str:
    priority = (value or "medium").strip().lower()
    if priority not in FIELD_MATERIAL_REQUEST_PRIORITIES:
        raise HTTPException(status_code=422, detail=f"Unsupported priority: {value}")
    return priority


def _status(value: str) -> str:
    status = (value or "").strip().lower()
    if status not in FIELD_MATERIAL_REQUEST_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unsupported status: {value}")
    return status


def _normalize_backoffice_status(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized[:40] or None


def _require_legacy_material_fulfilment(db: Session) -> None:
    """Keep the old local transition available only before explicit cutover."""
    from app.services.backoffice import external_material_fulfilment_active

    if external_material_fulfilment_active(db):
        raise HTTPException(
            status_code=409,
            detail=(
                "The configured back-office system owns material issue and "
                "fulfilment after cutover; reconcile its outcome instead"
            ),
        )


def _mark_sub_authoritative(row: WorkOrder | None) -> None:
    if row is None:
        return
    _mark_source_authoritative(row, "material_requests")


# --- receipted lifecycle-output consumption --------------------------------


def consume_material_request_approved(
    db: Session,
    *,
    material_request_id: str,
    event_id,
    context,
) -> str | None:
    """Receipt one committed approval into the ERP-issue request.

    The outbox intent and its unique ``(consumer, event_id)`` receipt commit
    atomically; a redelivery is an exact no-op, and a failed enqueue stays a
    failed retryable delivery. Sub never infers issuance — the ERP outcome
    returns through the durable outbox observation projection.
    """
    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import (
        OwnerCommandDefinition,
        execute_owner_command,
    )

    definition = OwnerCommandDefinition(
        owner="operations.material_dependencies",
        concern="committed material output consumption",
        name="consume_material_request_approved",
    )

    def _effect() -> str:
        from app.services.backoffice import enqueue_material_request_outbox

        request = db.get(FieldMaterialRequest, coerce_uuid(material_request_id))
        if request is None:
            return "skipped_missing"
        if request.status not in {"submitted", "approved"}:
            # Stale replay after refusal, fulfilment, or cancellation.
            return "skipped_state"
        if request.fulfillment_channel != MaterialRequestFulfillmentChannel.ERP.value:
            return "skipped_manual"
        event = enqueue_material_request_outbox(db, request)
        return "enqueued" if event is not None else "skipped_not_owned"

    return execute_owner_command(
        db,
        definition=definition,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer="operations.material_dependencies",
            event_id=event_id,
            event_type="field_material_request.approved",
            producer_owner="operations.material_dependencies",
            context=context,
            operation=_effect,
        )[0],
    )


field_material_requests = FieldMaterialRequests()
