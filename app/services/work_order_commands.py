"""Authoritative native work-order and dispatch command owner.

Adapters may authorize and shape requests, but native work-order creation,
header mutation, assignment projection, and assignment-queue transitions are
decided and committed here. CRM mirror ingest remains a separate observation
boundary and field execution status transitions remain owned by
``app.services.field.transitions``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.dispatch import (
    DispatchQueueStatus,
    DispatchRule,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.project import Project, ProjectTask
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.dispatch import (
    WorkOrderAssignmentQueueCreate,
    WorkOrderAssignmentQueueUpdate,
    WorkOrderHeaderCreate,
    WorkOrderHeaderUpdate,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.field.source import mark_sub_authoritative
from app.services.field.work_order_status import (
    TERMINAL_WORK_ORDER_STATUSES,
    WORK_ORDER_STATUSES,
    WorkOrderStatus,
)
from app.services.work_order_errors import WorkOrderCommandError

_CREATE_ID_NAMESPACE = uuid.UUID("cbf90ef0-a977-49fb-a2ac-a636eb3b2342")
_QUEUE_STATUSES = frozenset(
    {
        DispatchQueueStatus.queued,
        DispatchQueueStatus.assigned,
        DispatchQueueStatus.skipped,
    }
)
_INITIAL_STATUSES = frozenset(
    {WorkOrderStatus.draft.value, WorkOrderStatus.scheduled.value}
)
_FIELD_EXECUTION_STATUSES = frozenset(
    {
        WorkOrderStatus.in_progress.value,
        WorkOrderStatus.paused.value,
        WorkOrderStatus.completed.value,
    }
)
_ASSIGNMENT_HEADER_FIELDS = frozenset(
    {
        "assigned_to_crm_person_id",
        "assigned_to_name",
        "technician_name",
        "technician_phone",
    }
)


@dataclass(frozen=True)
class AutomatedProjectTaskWorkOrderCommand:
    """Typed participant command for configured ProjectTask field work."""

    project_id: uuid.UUID
    project_task_id: uuid.UUID
    subscriber_id: uuid.UUID
    title: str
    description: str | None
    priority: str | None
    address: str | None
    requires_as_built_evidence: bool
    idempotency_key: str


def _data(payload: Any, *, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=exclude_unset)
    return dict(payload)


def _fingerprint(data: dict[str, Any]) -> str:
    encoded = json.dumps(
        data,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _actor(auth: dict[str, Any] | None) -> tuple[AuditActorType, str | None]:
    if not auth:
        return AuditActorType.system, None
    principal_type = str(auth.get("principal_type") or "").strip().lower()
    actor_type = {
        "api_key": AuditActorType.api_key,
        "service": AuditActorType.service,
        "system_user": AuditActorType.user,
        "subscriber": AuditActorType.user,
        "reseller_user": AuditActorType.user,
    }.get(principal_type, AuditActorType.system)
    actor_id = auth.get("principal_id") or auth.get("actor_id")
    return actor_type, str(actor_id) if actor_id else None


def _audit(
    db: Session,
    *,
    action: str,
    work_order: WorkOrder,
    auth: dict[str, Any] | None,
    request_id: str | None,
    metadata: dict[str, object],
) -> None:
    actor_type, actor_id = _actor(auth)
    stage_audit_event(
        db,
        action=action,
        entity_type="work_order",
        entity_id=work_order.public_id,
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=request_id,
        metadata=jsonable_encoder(metadata),
    )


def _validate_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in WORK_ORDER_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported work-order status: {normalized or value}",
        )
    return normalized


def _validate_queue_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _QUEUE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported assignment-queue status: {normalized or value}",
        )
    return normalized


def _validate_schedule(
    start: datetime | None,
    end: datetime | None,
) -> None:
    if start is not None and end is not None and end <= start:
        raise HTTPException(
            status_code=422,
            detail="scheduled_end must be after scheduled_start",
        )


def _get_work_order(db: Session, public_id: str, *, lock: bool = False) -> WorkOrder:
    query = db.query(WorkOrder).filter(WorkOrder.public_id == str(public_id))
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    return row


def _resolve_work_order(
    db: Session,
    payload: WorkOrderAssignmentQueueCreate,
    *,
    lock: bool,
) -> WorkOrder:
    query = db.query(WorkOrder)
    if payload.work_order_mirror_id is not None:
        query = query.filter(WorkOrder.id == coerce_uuid(payload.work_order_mirror_id))
    else:
        query = query.filter(WorkOrder.public_id == payload.crm_work_order_id)
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    return row


def _get_technician(db: Session, technician_id: object) -> TechnicianProfile:
    try:
        row = db.get(TechnicianProfile, coerce_uuid(technician_id))
    except (TypeError, ValueError):
        row = None
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Technician not found")
    return row


def _get_rule(db: Session, rule_id: object) -> DispatchRule:
    try:
        row = db.get(DispatchRule, coerce_uuid(rule_id))
    except (TypeError, ValueError):
        row = None
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Dispatch rule not found")
    return row


def _technician_name(db: Session, profile: TechnicianProfile) -> str:
    user = (
        db.get(SystemUser, profile.system_user_id)
        if profile.system_user_id is not None
        else None
    )
    if user is not None:
        name = user.display_name or f"{user.first_name} {user.last_name}".strip()
        if name:
            return name
    return profile.title or str(profile.person_id)


def _latest_queue_entry(
    db: Session,
    work_order_id: uuid.UUID,
) -> WorkOrderAssignmentQueue | None:
    return (
        db.query(WorkOrderAssignmentQueue)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id == work_order_id)
        .order_by(
            WorkOrderAssignmentQueue.updated_at.desc(),
            WorkOrderAssignmentQueue.created_at.desc(),
        )
        .first()
    )


def _same_queue_command(
    row: WorkOrderAssignmentQueue,
    data: dict[str, Any],
) -> bool:
    return all(
        getattr(row, key) == value
        for key, value in data.items()
        if key
        in {
            "status",
            "reason",
            "dispatch_rule_id",
            "assigned_technician_id",
        }
    )


class WorkOrderCommands:
    """Single native decision and transaction boundary for work-order commands."""

    @staticmethod
    def validate_subscriber_target(db: Session, subscriber_id: object) -> uuid.UUID:
        """Validate and normalize the subscriber target for a native job."""

        normalized = coerce_uuid(subscriber_id)
        if db.get(Subscriber, normalized) is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        return normalized

    @staticmethod
    def validate_project_target(
        db: Session,
        project_id: object,
        *,
        subscriber_id: object,
    ) -> uuid.UUID:
        """Validate the native project binding owned by the work order."""

        normalized = coerce_uuid(project_id)
        project = db.get(Project, normalized)
        if project is None or not project.is_active:
            raise WorkOrderCommandError(
                "project_not_found", "Project not found", kind="not_found"
            )
        subscriber_uuid = coerce_uuid(subscriber_id)
        if project.subscriber_id is None:
            raise WorkOrderCommandError(
                "project_subscriber_missing",
                "Link a subscriber to the project before creating field work",
                kind="invalid",
            )
        if project.subscriber_id != subscriber_uuid:
            raise WorkOrderCommandError(
                "project_subscriber_mismatch",
                "Work order and project must belong to the same subscriber",
                kind="invalid",
            )
        return normalized

    @staticmethod
    def validate_project_task_target(
        db: Session,
        project_task_id: object,
        *,
        subscriber_id: object,
        project_id: object | None = None,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Validate a native execution scope and return task/project ids."""

        normalized = coerce_uuid(project_task_id)
        task = db.get(ProjectTask, normalized)
        if task is None or not task.is_active:
            raise WorkOrderCommandError(
                "project_task_not_found", "Project task not found", kind="not_found"
            )
        if project_id is not None and coerce_uuid(project_id) != task.project_id:
            raise WorkOrderCommandError(
                "project_task_project_mismatch",
                "Project task does not belong to the selected project",
                kind="invalid",
            )
        WorkOrderCommands.validate_project_target(
            db,
            task.project_id,
            subscriber_id=subscriber_id,
        )
        return normalized, task.project_id

    @staticmethod
    def create(
        db: Session,
        payload: WorkOrderHeaderCreate,
        *,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        owner_metadata: dict[str, Any] | None = None,
        origin_ticket_id: object | None = None,
        commit: bool = True,
    ) -> WorkOrder:
        data = _data(payload)
        requested_public_id = str(data.pop("public_id", None) or "").strip()
        if requested_public_id:
            public_id = requested_public_id
        elif idempotency_key:
            public_id = f"sub-{uuid.uuid5(_CREATE_ID_NAMESPACE, idempotency_key).hex}"
        else:
            public_id = f"sub-{uuid.uuid4().hex}"

        status = _validate_status(data.get("status") or WorkOrderStatus.draft.value)
        if status not in _INITIAL_STATUSES:
            raise HTTPException(
                status_code=422,
                detail="Native work orders must be created as draft or scheduled",
            )
        data["status"] = status
        if any(data.get(field) is not None for field in _ASSIGNMENT_HEADER_FIELDS):
            raise HTTPException(
                status_code=422,
                detail="Create the work order first, then use the assignment command",
            )
        _validate_schedule(data.get("scheduled_start"), data.get("scheduled_end"))

        data["subscriber_id"] = WorkOrderCommands.validate_subscriber_target(
            db, data["subscriber_id"]
        )
        if data.get("project_task_id") is not None:
            normalized_task_id, task_project_id = (
                WorkOrderCommands.validate_project_task_target(
                    db,
                    data["project_task_id"],
                    subscriber_id=data["subscriber_id"],
                    project_id=data.get("project_id"),
                )
            )
            data["project_task_id"] = normalized_task_id
            data["project_id"] = task_project_id
        elif data.get("project_id") is not None:
            data["project_id"] = WorkOrderCommands.validate_project_target(
                db,
                data["project_id"],
                subscriber_id=data["subscriber_id"],
            )
        normalized_origin_ticket_id: uuid.UUID | None = None
        if origin_ticket_id is not None:
            normalized_origin_ticket_id = coerce_uuid(origin_ticket_id)
            ticket = db.get(Ticket, normalized_origin_ticket_id)
            if ticket is None or not ticket.is_active:
                raise WorkOrderCommandError(
                    "origin_ticket_not_found",
                    "Origin ticket not found",
                    kind="not_found",
                )
            if ticket.subscriber_id != data["subscriber_id"]:
                raise WorkOrderCommandError(
                    "origin_ticket_subscriber_mismatch",
                    "Work order and origin ticket must belong to the same subscriber",
                    kind="invalid",
                )

        supplied_metadata = dict(data.pop("metadata_", None) or {})
        supplied_metadata.pop("fiber_field_verification_plan", None)
        owned_metadata = dict(owner_metadata or {})
        unsupported_owned_keys = set(owned_metadata) - {"fiber_field_verification_plan"}
        if unsupported_owned_keys:
            raise HTTPException(
                status_code=422,
                detail="Unsupported owner-managed work-order metadata",
            )
        supplied_metadata.update(owned_metadata)
        command_fingerprint = _fingerprint(
            {
                "public_id": public_id,
                **data,
                "origin_ticket_id": normalized_origin_ticket_id,
                "metadata": supplied_metadata,
            }
        )
        existing = (
            db.query(WorkOrder).filter(WorkOrder.public_id == public_id).one_or_none()
        )
        if existing is not None:
            existing_metadata = dict(existing.metadata_ or {})
            if (
                existing_metadata.get("native_create_fingerprint")
                == command_fingerprint
            ):
                return existing
            raise HTTPException(status_code=409, detail="Work order id already exists")

        metadata = supplied_metadata
        metadata["native_source"] = "sub"
        metadata["native_create_fingerprint"] = command_fingerprint
        row = WorkOrder(
            public_id=public_id,
            # Native work orders have no CRM provenance. Compatibility output
            # derives from public_id at the read boundary.
            metadata_=metadata,
            origin_ticket_id=normalized_origin_ticket_id,
            work_order_created_at=data.get("work_order_created_at"),
            **data,
        )
        db.add(row)
        try:
            db.flush()
            _audit(
                db,
                action="work_order.created",
                work_order=row,
                auth=auth,
                request_id=request_id or idempotency_key,
                metadata={
                    "owner": "operations.work_order_commands",
                    "result": {
                        "public_id": row.public_id,
                        "status": row.status,
                        "subscriber_id": str(row.subscriber_id),
                        "origin_ticket_id": str(row.origin_ticket_id)
                        if row.origin_ticket_id
                        else None,
                        "project_id": str(row.project_id) if row.project_id else None,
                        "project_task_id": str(row.project_task_id)
                        if row.project_task_id
                        else None,
                    },
                },
            )
            if commit:
                db.commit()
                db.refresh(row)
        except IntegrityError as exc:
            if not commit:
                # Participant callers do not own transaction completion. The
                # root owner rolls back and retries the whole command so its
                # state, provenance, and audit evidence remain atomic.
                raise
            # Concurrent replays converge through the public-id unique key.
            db.rollback()
            existing = (
                db.query(WorkOrder)
                .filter(WorkOrder.public_id == public_id)
                .one_or_none()
            )
            if (
                existing is not None
                and dict(existing.metadata_ or {}).get("native_create_fingerprint")
                == command_fingerprint
            ):
                return existing
            raise HTTPException(
                status_code=409,
                detail="Work order id already exists",
            ) from exc
        return row

    @staticmethod
    def stage_automated_project_task_work_order(
        db: Session, command: AutomatedProjectTaskWorkOrderCommand
    ) -> WorkOrder:
        """Stage a configured template-task WorkOrder without committing."""

        try:
            return WorkOrderCommands.create(
                db,
                WorkOrderHeaderCreate(
                    title=command.title,
                    description=command.description,
                    subscriber_id=command.subscriber_id,
                    project_id=command.project_id,
                    project_task_id=command.project_task_id,
                    priority=command.priority or "normal",
                    work_type="install",
                    address=command.address,
                    requires_as_built_evidence=command.requires_as_built_evidence,
                ),
                request_id=command.idempotency_key,
                idempotency_key=command.idempotency_key,
                commit=False,
            )
        except HTTPException as exc:
            raise WorkOrderCommandError(
                "automated_project_task_rejected",
                str(exc.detail or "Configured ProjectTask WorkOrder was rejected"),
                kind="invalid" if exc.status_code == 422 else "conflict",
            ) from exc

    @staticmethod
    def update_header(
        db: Session,
        public_id: str,
        payload: WorkOrderHeaderUpdate,
        *,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> WorkOrder:
        row = _get_work_order(db, public_id, lock=True)
        data = _data(payload, exclude_unset=True)
        project_supplied = "project_id" in data
        project_task_supplied = "project_task_id" in data
        direct_assignment_fields = sorted(_ASSIGNMENT_HEADER_FIELDS.intersection(data))
        if direct_assignment_fields:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Assignment fields are written by the assignment command: "
                    + ", ".join(direct_assignment_fields)
                ),
            )

        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, coerce_uuid(data["subscriber_id"]))
            if subscriber is None:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if "project_id" in data:
            requested_project_id = data["project_id"]
            if requested_project_id is None and row.project_id is not None:
                raise WorkOrderCommandError(
                    "project_binding_immutable",
                    "A native work-order project binding cannot be removed",
                )
            if (
                row.project_id is not None
                and requested_project_id is not None
                and coerce_uuid(requested_project_id) != row.project_id
            ):
                raise WorkOrderCommandError(
                    "project_binding_immutable",
                    "A native work-order project binding cannot be changed",
                )
        if "project_task_id" in data:
            requested_task_id = data["project_task_id"]
            if requested_task_id is None and row.project_task_id is not None:
                raise WorkOrderCommandError(
                    "project_task_binding_immutable",
                    "A native work-order project-task binding cannot be removed",
                )
            if (
                row.project_task_id is not None
                and requested_task_id is not None
                and coerce_uuid(requested_task_id) != row.project_task_id
            ):
                raise WorkOrderCommandError(
                    "project_task_binding_immutable",
                    "A native work-order project-task binding cannot be changed",
                )
        if (
            "requires_as_built_evidence" in data
            and data["requires_as_built_evidence"] is None
        ):
            raise WorkOrderCommandError(
                "invalid_evidence_policy",
                "requires_as_built_evidence cannot be null",
                kind="invalid",
            )
        effective_project_id = data.get("project_id", row.project_id)
        effective_subscriber_id = data.get("subscriber_id", row.subscriber_id)
        effective_project_task_id = data.get("project_task_id", row.project_task_id)
        if effective_project_task_id is not None:
            normalized_task_id, task_project_id = (
                WorkOrderCommands.validate_project_task_target(
                    db,
                    effective_project_task_id,
                    subscriber_id=effective_subscriber_id,
                    project_id=effective_project_id,
                )
            )
            if project_task_supplied:
                data["project_task_id"] = normalized_task_id
            if effective_project_id is None:
                data["project_id"] = task_project_id
                effective_project_id = task_project_id
        if effective_project_id is not None:
            normalized_project_id = WorkOrderCommands.validate_project_target(
                db,
                effective_project_id,
                subscriber_id=effective_subscriber_id,
            )
            if project_supplied:
                data["project_id"] = normalized_project_id
        if "status" in data and data["status"] is not None:
            status = _validate_status(data["status"])
            if row.status in TERMINAL_WORK_ORDER_STATUSES and status != row.status:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot reopen a work order in status {row.status}",
                )
            if status in _FIELD_EXECUTION_STATUSES and status != row.status:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Status {status} is written by the field transition owner"
                    ),
                )
            if status == WorkOrderStatus.dispatched.value and status != row.status:
                raise HTTPException(
                    status_code=422,
                    detail="Dispatched status is written by the assignment command",
                )
            if (
                row.status == WorkOrderStatus.dispatched.value
                and status in _INITIAL_STATUSES
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "End an active assignment through its assignment-queue "
                        "transition"
                    ),
                )
            data["status"] = status

        _validate_schedule(
            data.get("scheduled_start", row.scheduled_start),
            data.get("scheduled_end", row.scheduled_end),
        )
        previous = {
            key: str(getattr(row, key)) if getattr(row, key) is not None else None
            for key in data
            if key != "metadata_"
        }
        if "metadata_" in data:
            metadata = dict(row.metadata_ or {})
            incoming = dict(data.pop("metadata_") or {})
            for reserved in (
                "native_source",
                "native_create_fingerprint",
                "native_field_source",
                "native_field_activity",
                "fiber_field_verification_plan",
            ):
                incoming.pop(reserved, None)
            metadata.update(incoming)
            metadata["native_source"] = "sub"
            row.metadata_ = metadata
        for key, value in data.items():
            setattr(row, key, value)
        mark_sub_authoritative(
            row, "work_order_update", details={"fields": sorted(data)}
        )
        _audit(
            db,
            action="work_order.updated",
            work_order=row,
            auth=auth,
            request_id=request_id,
            metadata={
                "owner": "operations.work_order_commands",
                "previous": previous,
                "result": {
                    key: str(getattr(row, key))
                    if getattr(row, key) is not None
                    else None
                    for key in data
                },
            },
        )
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def preview_assignment(
        db: Session,
        public_id: str,
        *,
        technician_id: object,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
        status: str = WorkOrderStatus.dispatched.value,
    ) -> dict[str, object]:
        row = _get_work_order(db, public_id)
        if not row.is_active:
            raise HTTPException(status_code=409, detail="Work order is inactive")
        if row.status in TERMINAL_WORK_ORDER_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot assign a work order in status {row.status}",
            )
        profile = _get_technician(db, technician_id)
        target_status = _validate_status(status)
        if (
            row.status
            in {WorkOrderStatus.in_progress.value, WorkOrderStatus.paused.value}
            and target_status == WorkOrderStatus.dispatched.value
        ):
            # Reassignment must not rewind a field-execution lifecycle.
            target_status = row.status
        allowed_statuses = {
            WorkOrderStatus.scheduled.value,
            WorkOrderStatus.dispatched.value,
        }
        if row.status in {
            WorkOrderStatus.in_progress.value,
            WorkOrderStatus.paused.value,
        }:
            allowed_statuses.add(row.status)
        if target_status not in allowed_statuses:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported assignment status: {target_status}",
            )
        start = scheduled_start if scheduled_start is not None else row.scheduled_start
        end = scheduled_end if scheduled_end is not None else row.scheduled_end
        _validate_schedule(start, end)
        latest = _latest_queue_entry(db, row.id)
        return {
            "work_order_id": row.public_id,
            "previous": {
                "status": row.status,
                "technician_id": (
                    str(latest.assigned_technician_id)
                    if latest is not None
                    and latest.status == DispatchQueueStatus.assigned
                    and latest.assigned_technician_id is not None
                    else None
                ),
                "scheduled_start": row.scheduled_start,
                "scheduled_end": row.scheduled_end,
            },
            "result": {
                "status": target_status,
                "technician_id": str(profile.id),
                "person_id": str(profile.person_id),
                "technician_name": _technician_name(db, profile),
                "scheduled_start": start,
                "scheduled_end": end,
            },
        }

    @staticmethod
    def assign(
        db: Session,
        public_id: str,
        *,
        technician_id: object,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
        status: str = WorkOrderStatus.dispatched.value,
        reason: str | None = None,
        dispatch_rule_id: object | None = None,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
        commit: bool = True,
    ) -> WorkOrderAssignmentQueue:
        preview = WorkOrderCommands.preview_assignment(
            db,
            public_id,
            technician_id=technician_id,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            status=status,
        )
        row = _get_work_order(db, public_id, lock=True)
        profile = _get_technician(db, technician_id)
        rule = _get_rule(db, dispatch_rule_id) if dispatch_rule_id is not None else None
        latest = _latest_queue_entry(db, row.id)
        normalized_reason = str(reason or "").strip() or None
        queue_data = {
            "status": DispatchQueueStatus.assigned,
            "reason": normalized_reason,
            "assigned_technician_id": profile.id,
            "dispatch_rule_id": rule.id if rule is not None else None,
        }
        target = preview["result"]
        assert isinstance(target, dict)
        is_replay = (
            latest is not None
            and _same_queue_command(latest, queue_data)
            and row.status == target["status"]
            and row.scheduled_start == target["scheduled_start"]
            and row.scheduled_end == target["scheduled_end"]
        )
        if is_replay and latest is not None:
            return latest

        if latest is None:
            latest = WorkOrderAssignmentQueue(
                work_order_mirror_id=row.id,
            )
            db.add(latest)
        latest.status = DispatchQueueStatus.assigned
        latest.reason = normalized_reason
        latest.assigned_technician_id = profile.id
        latest.dispatch_rule_id = rule.id if rule is not None else None

        name = str(target["technician_name"])
        row.assigned_to_crm_person_id = profile.crm_person_id
        row.assigned_to_name = name
        row.technician_name = name
        row.scheduled_start = target["scheduled_start"]
        row.scheduled_end = target["scheduled_end"]
        row.status = str(target["status"])
        mark_sub_authoritative(
            row,
            "assignment",
            details={
                "technician_id": str(profile.id),
                "person_id": str(profile.person_id),
                "status": row.status,
            },
        )
        db.flush()
        _audit(
            db,
            action="work_order.assigned",
            work_order=row,
            auth=auth,
            request_id=request_id,
            metadata={
                "owner": "operations.work_order_commands",
                "queue_id": str(latest.id),
                "dispatch_rule_id": str(rule.id) if rule is not None else None,
                "previous": preview["previous"],
                "result": target,
            },
        )
        if commit:
            db.commit()
            db.refresh(latest)
        return latest

    @staticmethod
    def create_queue_entry(
        db: Session,
        payload: WorkOrderAssignmentQueueCreate,
        *,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> WorkOrderAssignmentQueue:
        row = _resolve_work_order(db, payload, lock=True)
        data = _data(payload)
        data.pop("work_order_mirror_id", None)
        data.pop("crm_work_order_id", None)
        data["status"] = _validate_queue_status(data.get("status") or "queued")
        data["reason"] = str(data.get("reason") or "").strip() or None
        if data.get("dispatch_rule_id") is not None:
            data["dispatch_rule_id"] = _get_rule(db, data["dispatch_rule_id"]).id
        if data.get("assigned_technician_id") is not None:
            data["assigned_technician_id"] = _get_technician(
                db, data["assigned_technician_id"]
            ).id
        if (
            data["status"] == DispatchQueueStatus.assigned
            and data.get("assigned_technician_id") is None
        ):
            raise HTTPException(
                status_code=422,
                detail="Assigned queue state requires a technician",
            )
        if data["status"] == DispatchQueueStatus.assigned:
            return WorkOrderCommands.assign(
                db,
                row.public_id,
                technician_id=data["assigned_technician_id"],
                reason=data["reason"],
                dispatch_rule_id=data.get("dispatch_rule_id"),
                auth=auth,
                request_id=request_id,
            )

        latest = _latest_queue_entry(db, row.id)
        if latest is not None and _same_queue_command(latest, data):
            return latest
        entry = WorkOrderAssignmentQueue(
            work_order_mirror_id=row.id,
            **data,
        )
        db.add(entry)
        db.flush()
        _audit(
            db,
            action="work_order.assignment_queued",
            work_order=row,
            auth=auth,
            request_id=request_id,
            metadata={
                "owner": "operations.work_order_commands",
                "queue_id": str(entry.id),
                "result": {
                    "status": entry.status,
                    "technician_id": (
                        str(entry.assigned_technician_id)
                        if entry.assigned_technician_id
                        else None
                    ),
                    "dispatch_rule_id": (
                        str(entry.dispatch_rule_id) if entry.dispatch_rule_id else None
                    ),
                },
            },
        )
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def update_queue_entry(
        db: Session,
        queue_id: str,
        payload: WorkOrderAssignmentQueueUpdate,
        *,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> WorkOrderAssignmentQueue:
        try:
            queue_uuid = coerce_uuid(queue_id)
        except (TypeError, ValueError):
            queue_uuid = None
        entry = (
            db.query(WorkOrderAssignmentQueue)
            .filter(WorkOrderAssignmentQueue.id == queue_uuid)
            .with_for_update()
            .one_or_none()
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="Queue item not found")
        row = (
            db.query(WorkOrder)
            .filter(WorkOrder.id == entry.work_order_mirror_id)
            .with_for_update()
            .one()
        )
        latest_before = _latest_queue_entry(db, row.id)
        previous_queue_status = entry.status
        previous_projection = {
            "status": row.status,
            "technician_id": (
                str(entry.assigned_technician_id)
                if entry.assigned_technician_id is not None
                else None
            ),
            "assigned_to_crm_person_id": row.assigned_to_crm_person_id,
            "assigned_to_name": row.assigned_to_name,
        }
        data = _data(payload, exclude_unset=True)
        if "status" in data and data["status"] is not None:
            data["status"] = _validate_queue_status(data["status"])
        if "reason" in data:
            data["reason"] = str(data["reason"] or "").strip() or None
        if data.get("dispatch_rule_id") is not None:
            data["dispatch_rule_id"] = _get_rule(db, data["dispatch_rule_id"]).id
        if data.get("assigned_technician_id") is not None:
            data["assigned_technician_id"] = _get_technician(
                db, data["assigned_technician_id"]
            ).id
        resulting_status = data.get("status", entry.status)
        resulting_technician = data.get(
            "assigned_technician_id", entry.assigned_technician_id
        )
        if (
            resulting_status == DispatchQueueStatus.assigned
            and resulting_technician is None
        ):
            raise HTTPException(
                status_code=422,
                detail="Assigned queue state requires a technician",
            )
        if _same_queue_command(entry, data):
            return entry
        if resulting_status == DispatchQueueStatus.assigned:
            return WorkOrderCommands.assign(
                db,
                row.public_id,
                technician_id=resulting_technician,
                reason=data.get("reason", entry.reason),
                dispatch_rule_id=data.get("dispatch_rule_id", entry.dispatch_rule_id),
                auth=auth,
                request_id=request_id,
            )

        previous = {
            key: str(getattr(entry, key)) if getattr(entry, key) is not None else None
            for key in data
        }
        for key, value in data.items():
            setattr(entry, key, value)
        removed_current_assignment = (
            latest_before is not None
            and latest_before.id == entry.id
            and previous_queue_status == DispatchQueueStatus.assigned
            and entry.status != DispatchQueueStatus.assigned
        )
        if removed_current_assignment:
            row.assigned_to_crm_person_id = None
            row.assigned_to_name = None
            row.technician_name = None
            if row.status == WorkOrderStatus.dispatched.value:
                row.status = WorkOrderStatus.scheduled.value
            mark_sub_authoritative(
                row,
                "assignment_removed",
                details={
                    "queue_id": str(entry.id),
                    "queue_status": entry.status,
                },
            )
        db.flush()
        _audit(
            db,
            action="work_order.assignment_queue_transitioned",
            work_order=row,
            auth=auth,
            request_id=request_id,
            metadata={
                "owner": "operations.work_order_commands",
                "queue_id": str(entry.id),
                "previous": previous,
                "result": {
                    key: str(getattr(entry, key))
                    if getattr(entry, key) is not None
                    else None
                    for key in data
                },
                "work_order_projection": {
                    "previous": previous_projection,
                    "result": {
                        "status": row.status,
                        "technician_id": (
                            str(entry.assigned_technician_id)
                            if entry.status == DispatchQueueStatus.assigned
                            and entry.assigned_technician_id is not None
                            else None
                        ),
                        "assigned_to_crm_person_id": row.assigned_to_crm_person_id,
                        "assigned_to_name": row.assigned_to_name,
                    },
                },
            },
        )
        db.commit()
        db.refresh(entry)
        return entry


work_order_commands = WorkOrderCommands()
