"""Technician-scoped field job reads over Sub-owned work orders.

CRM can hydrate legacy work-order headers during migration
(``crm_work_order_id`` provenance). Native field execution activity is
authored in sub and recorded on ``work_order`` as sub-authoritative metadata.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.subscriber import Subscriber
from app.models.support import canonical_ticket_status_value
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import (
    FieldAttachmentRead,
    FieldCustomer,
    FieldCustomerExperienceContext,
    FieldEquipmentRead,
    FieldExpenseRequestRead,
    FieldJobDetail,
    FieldJobEventRead,
    FieldJobLocation,
    FieldJobSummary,
    FieldMaterialRead,
    FieldMaterialRequestRead,
    FieldMeResponse,
    FieldMovementRead,
    FieldNoteRead,
    FieldProjectContext,
    FieldProjectTaskContext,
    FieldTicketContext,
    FieldWorkLogRead,
)
from app.services.common import apply_pagination, coerce_uuid
from app.services.field.map_assets import field_map_assets
from app.services.field.source import mark_sub_authoritative
from app.services.field.work_order_status import (
    FIELD_OPEN_WORK_ORDER_STATUSES,
    WORK_ORDER_TERMINAL_VALUES,
)
from app.services.status_presentation import (
    project_status_presentation,
    project_task_status_presentation,
    ticket_status_presentation,
    work_order_status_presentation,
)

TERMINAL_STATUSES = WORK_ORDER_TERMINAL_VALUES
OPEN_STATUSES = FIELD_OPEN_WORK_ORDER_STATUSES
FieldJobSummaries = list[FieldJobSummary]
FieldJobDestinationPayload = dict[str, Any]
FieldJobDestinationPayloads = list[FieldJobDestinationPayload]


def _annotate_vendor_membership(
    db: Session, profile: TechnicianProfile
) -> TechnicianProfile:
    if profile.system_user_id is None:
        return profile
    membership = (
        db.query(FieldVendorUser)
        .join(FieldVendor, FieldVendor.id == FieldVendorUser.vendor_id)
        .filter(FieldVendorUser.system_user_id == profile.system_user_id)
        .filter(FieldVendorUser.is_active.is_(True))
        .filter(FieldVendor.is_active.is_(True))
        .order_by(FieldVendorUser.created_at.desc())
        .first()
    )
    if membership is None:
        return profile
    annotated: Any = profile
    annotated._field_vendor_id = str(membership.vendor_id)
    annotated._field_vendor_user_id = str(membership.id)
    annotated._field_crm_vendor_user_id = membership.crm_vendor_user_id
    if membership.vendor is not None:
        annotated._field_crm_vendor_id = membership.vendor.crm_vendor_id
    return profile


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _technician_name(profile: TechnicianProfile, user: SystemUser | None) -> str:
    if user is not None:
        display_name = (
            user.display_name or f"{user.first_name} {user.last_name}".strip()
        )
        if display_name:
            return display_name
    metadata = profile.metadata_ or {}
    for key in ("name", "display_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return profile.crm_person_id or str(profile.person_id)


def _system_user(db: Session, profile: TechnicianProfile) -> SystemUser | None:
    if profile.system_user_id is None:
        return None
    return db.get(SystemUser, profile.system_user_id)


def _profile_from_principal(
    db: Session, principal: dict[str, Any]
) -> TechnicianProfile:
    ids = {
        str(value)
        for value in (
            principal.get("principal_id"),
            principal.get("person_id"),
            principal.get("subscriber_id"),
        )
        if value
    }
    uuid_ids: list[UUID] = []
    for value in ids:
        try:
            uuid_ids.append(coerce_uuid(value))
        except ValueError:
            continue

    query = db.query(TechnicianProfile).filter(TechnicianProfile.is_active.is_(True))
    if uuid_ids:
        profile = query.filter(
            or_(
                TechnicianProfile.system_user_id.in_(uuid_ids),
                TechnicianProfile.person_id.in_(uuid_ids),
            )
        ).first()
        if profile is not None:
            return _annotate_vendor_membership(db, profile)

    crm_person_id = principal.get("crm_person_id")
    if crm_person_id:
        profile = query.filter(
            TechnicianProfile.crm_person_id == str(crm_person_id)
        ).first()
        if profile is not None:
            return _annotate_vendor_membership(db, profile)

    raise HTTPException(status_code=404, detail="Technician profile not found")


def _metadata_text_match(key: str, value: str):
    return WorkOrder.metadata_[key].as_string() == value


def _scoped_query(db: Session, profile: TechnicianProfile):
    assignment_ids = select(WorkOrderAssignmentQueue.work_order_mirror_id).filter(
        WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
        WorkOrderAssignmentQueue.assigned_technician_id == profile.id,
    )
    query = db.query(WorkOrder).filter(WorkOrder.is_active.is_(True))
    clauses: list[Any] = [WorkOrder.id.in_(assignment_ids)]
    if profile.crm_person_id:
        clauses.append(WorkOrder.assigned_to_crm_person_id == profile.crm_person_id)
    vendor_id = getattr(profile, "_field_vendor_id", None)
    if vendor_id:
        clauses.extend(
            [
                _metadata_text_match("assigned_vendor_id", vendor_id),
                _metadata_text_match("vendor_id", vendor_id),
                WorkOrder.metadata_["assigned_vendor"]["id"].as_string() == vendor_id,
                WorkOrder.metadata_["vendor"]["id"].as_string() == vendor_id,
            ]
        )
    vendor_user_id = getattr(profile, "_field_vendor_user_id", None)
    if vendor_user_id:
        clauses.extend(
            [
                _metadata_text_match("assigned_vendor_user_id", vendor_user_id),
                _metadata_text_match("vendor_user_id", vendor_user_id),
            ]
        )
    crm_vendor_id = getattr(profile, "_field_crm_vendor_id", None)
    if crm_vendor_id:
        clauses.extend(
            [
                _metadata_text_match("crm_assigned_vendor_id", crm_vendor_id),
                _metadata_text_match("crm_vendor_id", crm_vendor_id),
            ]
        )
    crm_vendor_user_id = getattr(profile, "_field_crm_vendor_user_id", None)
    if crm_vendor_user_id:
        clauses.extend(
            [
                _metadata_text_match("crm_assigned_vendor_user_id", crm_vendor_user_id),
                _metadata_text_match("crm_vendor_user_id", crm_vendor_user_id),
            ]
        )
    return query.filter(or_(*clauses))


def _subscriber_name(subscriber: Subscriber) -> str | None:
    full = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    return (
        subscriber.company_name or full or subscriber.email or subscriber.account_number
    )


def _customer(row: WorkOrder, subscriber: Subscriber | None) -> FieldCustomer | None:
    if subscriber is None:
        return None
    status = getattr(subscriber, "status", None)
    return FieldCustomer(
        subscriber_id=subscriber.id,
        name=_subscriber_name(subscriber),
        phone=subscriber.phone,
        email=subscriber.email,
        address_text=row.address,
        service_plan=getattr(subscriber, "service_plan", None),
        account_number=subscriber.account_number,
        status=(getattr(status, "value", None) or str(status)) if status else None,
    )


def _location(row: WorkOrder) -> FieldJobLocation:
    metadata = row.metadata_ or {}
    latitude = _first_present(metadata, "latitude", "lat")
    longitude = _first_present(metadata, "longitude", "lng")
    source = "cached"
    if isinstance(metadata.get("location"), dict):
        location = metadata["location"]
        latitude = (
            latitude
            if latitude is not None
            else _first_present(location, "latitude", "lat")
        )
        longitude = (
            longitude
            if longitude is not None
            else _first_present(location, "longitude", "lng")
        )
        source = str(location.get("source") or source)
    parsed_latitude = latitude if isinstance(latitude, int | float) else None
    parsed_longitude = longitude if isinstance(longitude, int | float) else None
    return FieldJobLocation(
        latitude=parsed_latitude,
        longitude=parsed_longitude,
        address_text=row.address,
        source=source
        if parsed_latitude is not None and parsed_longitude is not None
        else "address_only",
    )


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


_ASSET_DESTINATION_TYPES = {
    "fdh_cabinet": "cabinet",
    "splice_closure": "closure",
    "fiber_access_point": "fiber_access_point",
    "service_building": "service_building",
    "wireless_mast": "wireless_mast",
}


def _summary(row: WorkOrder) -> FieldJobSummary:
    return FieldJobSummary(
        id=row.public_id,
        work_order_mirror_id=row.id,
        title=row.title,
        description=row.description,
        status=row.status,
        status_presentation=work_order_status_presentation(row.status),
        priority=row.priority,
        work_type=row.work_type,
        scheduled_start=row.scheduled_start,
        scheduled_end=row.scheduled_end,
        estimated_duration_minutes=row.estimated_duration_minutes,
        estimated_arrival_at=row.estimated_arrival_at,
        started_at=row.started_at,
        paused_at=row.paused_at,
        resumed_at=row.resumed_at,
        completed_at=row.completed_at,
        total_active_seconds=row.total_active_seconds,
        technician_name=row.technician_name or row.assigned_to_name,
        technician_phone=row.technician_phone,
        address=row.address,
        tags=_string_list(row.tags),
    )


def _customer_experience(row: WorkOrder) -> FieldCustomerExperienceContext:
    project = row.project
    project_task = row.project_task
    origin_ticket = row.origin_ticket
    task_ticket = project_task.ticket if project_task is not None else None
    return FieldCustomerExperienceContext(
        project=FieldProjectContext(
            id=project.id,
            number=project.number,
            name=project.name,
            status=project.status,
            status_presentation=project_status_presentation(project.status),
        )
        if project is not None
        else None,
        project_task=FieldProjectTaskContext(
            id=project_task.id,
            number=project_task.number,
            title=project_task.title,
            status=project_task.status,
            status_presentation=project_task_status_presentation(project_task.status),
        )
        if project_task is not None
        else None,
        origin_ticket=FieldTicketContext(
            id=origin_ticket.id,
            number=origin_ticket.number,
            title=origin_ticket.title,
            status=canonical_ticket_status_value(origin_ticket.status),
            status_presentation=ticket_status_presentation(origin_ticket.status),
        )
        if origin_ticket is not None
        else None,
        project_task_ticket=FieldTicketContext(
            id=task_ticket.id,
            number=task_ticket.number,
            title=task_ticket.title,
            status=canonical_ticket_status_value(task_ticket.status),
            status_presentation=ticket_status_presentation(task_ticket.status),
        )
        if task_ticket is not None
        else None,
    )


class FieldJobs:
    @staticmethod
    def me(db: Session, principal: dict[str, Any]) -> FieldMeResponse:
        profile = _profile_from_principal(db, principal)
        user = _system_user(db, profile)
        today = datetime.now(UTC).date()
        scoped = _scoped_query(db, profile)
        open_jobs = [
            row
            for row in scoped.filter(WorkOrder.status.in_(OPEN_STATUSES)).all()
            if row.scheduled_start is None or row.scheduled_start.date() <= today
        ]
        completed_today = [
            row
            for row in scoped.filter(WorkOrder.status == "completed")
            .filter(WorkOrder.completed_at.isnot(None))
            .all()
            if row.completed_at and row.completed_at.date() == today
        ]
        return FieldMeResponse(
            person_id=profile.person_id,
            name=_technician_name(profile, user),
            email=user.email if user else None,
            technician_title=profile.title,
            region=profile.region,
            open_jobs=len(open_jobs),
            completed_today=len(completed_today),
        )

    @staticmethod
    def list(
        db: Session,
        principal: dict[str, Any],
        *,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> FieldJobSummaries:
        profile = _profile_from_principal(db, principal)
        query = _scoped_query(db, profile)
        if status:
            query = query.filter(WorkOrder.status == status)
        if date_from:
            query = query.filter(
                or_(
                    WorkOrder.scheduled_start.is_(None),
                    WorkOrder.scheduled_start >= date_from,
                )
            )
        if date_to:
            query = query.filter(
                or_(
                    WorkOrder.scheduled_start.is_(None),
                    WorkOrder.scheduled_start <= date_to,
                )
            )
        query = query.order_by(
            WorkOrder.scheduled_start.asc().nullslast(),
            WorkOrder.created_at.asc(),
        )
        return [_summary(row) for row in apply_pagination(query, limit, offset).all()]

    @staticmethod
    def get_detail(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> FieldJobDetail:
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        subscriber = db.get(Subscriber, row.subscriber_id)
        from app.services.field.attachments import field_attachments
        from app.services.field.equipment import field_equipment
        from app.services.field.expense_requests import field_expense_requests
        from app.services.field.material_requests import field_material_requests
        from app.services.field.materials import field_materials
        from app.services.field.movements import list_for_job as list_movements
        from app.services.field.notes import field_notes
        from app.services.field.transitions import field_transitions
        from app.services.field.worklogs import field_worklogs

        materials = field_materials.list_for_job(db, principal, crm_work_order_id)
        material_requests = field_material_requests.list_mine(
            db,
            principal,
            crm_work_order_id=crm_work_order_id,
            limit=50,
            offset=0,
        )
        expense_requests = field_expense_requests.list_mine(
            db,
            principal,
            crm_work_order_id=crm_work_order_id,
            limit=50,
            offset=0,
        )
        notes = field_notes.list_for_job(db, principal, crm_work_order_id)
        attachments = field_attachments.list(
            db, principal, crm_work_order_id=crm_work_order_id
        )
        worklogs = field_worklogs.list_for_job(db, principal, crm_work_order_id)
        events = field_transitions.list_for_job(db, principal, crm_work_order_id)
        movements = list_movements(db, row)
        equipment = field_equipment.current_for_job(db, principal, crm_work_order_id)

        return FieldJobDetail(
            job=_summary(row),
            completion_requirements=field_transitions.completion_requirements(db),
            customer=_customer(row, subscriber),
            location=_location(row),
            customer_experience=_customer_experience(row),
            access_notes=row.access_notes,
            materials=[FieldMaterialRead.model_validate(item) for item in materials],
            material_requests=[
                FieldMaterialRequestRead.model_validate(item)
                for item in material_requests
            ],
            expense_requests=[
                FieldExpenseRequestRead.model_validate(item)
                for item in expense_requests
            ],
            notes=[FieldNoteRead.model_validate(item) for item in notes],
            attachments=[
                FieldAttachmentRead.model_validate(item) for item in attachments
            ],
            worklogs=[FieldWorkLogRead.model_validate(item) for item in worklogs],
            events=[FieldJobEventRead.model_validate(item) for item in events],
            movements=[FieldMovementRead.model_validate(item) for item in movements],
            equipment=FieldEquipmentRead.model_validate(equipment)
            if equipment is not None
            else None,
            history=[],
        )

    @staticmethod
    def list_destinations(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> FieldJobDestinationPayloads:
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")

        location = _location(row)
        items: FieldJobDestinationPayloads = [
            {
                "destination_type": "customer",
                "destination_id": str(row.subscriber_id) if row.subscriber_id else None,
                "label": "Customer site",
                "latitude": location.latitude,
                "longitude": location.longitude,
                "address_text": location.address_text,
            }
        ]

        if location.latitude is not None and location.longitude is not None:
            assets = field_map_assets.nearby(
                db,
                latitude=location.latitude,
                longitude=location.longitude,
                radius_m=750,
                asset_types=builtins.list(_ASSET_DESTINATION_TYPES),
                limit=20,
            )
            for asset in assets:
                items.append(
                    {
                        "destination_type": _ASSET_DESTINATION_TYPES[asset["type"]],
                        "destination_id": str(asset["id"]),
                        "label": asset["title"],
                        "latitude": asset["latitude"],
                        "longitude": asset["longitude"],
                        "address_text": asset.get("subtitle"),
                    }
                )

        items.append(
            {
                "destination_type": "other",
                "destination_id": None,
                "label": "Other location",
                "latitude": None,
                "longitude": None,
                "address_text": None,
            }
        )
        return items

    @staticmethod
    def update_location(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        latitude: float,
        longitude: float,
    ) -> FieldJobLocation:
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")

        metadata = dict(row.metadata_ or {})
        metadata["location"] = {
            "lat": float(latitude),
            "lng": float(longitude),
            "latitude": float(latitude),
            "longitude": float(longitude),
            "address_text": row.address,
            "source": "manual",
        }
        row.metadata_ = metadata
        mark_sub_authoritative(
            row,
            "location",
            details={"latitude": float(latitude), "longitude": float(longitude)},
        )
        db.commit()
        db.refresh(row)
        return _location(row)


field_jobs = FieldJobs()
