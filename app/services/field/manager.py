"""Manager-mode reads and dispatch writes over the native field domain.

The CRM field-manager contract is implemented through Sub-owned technician
profiles and presence for the live technician board, authoritative work orders
and their assignment queue for the jobs board, and native expense requests for
approvals. All writes remain inside their declared Sub owners.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_expense import FieldExpenseRequest
from app.models.field_location import FieldTechPresence
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import (
    FieldLiveMapFeedQuery,
    FieldManagerTechnician,
    FieldManagerTechniciansQuery,
    FieldManagerTechniciansResponse,
)
from app.schemas.field import (
    FieldManagerActiveWorkOrder as ActiveWorkOrderOutcome,
)
from app.services import field_maps
from app.services.common import apply_pagination, coerce_uuid
from app.services.field.jobs import (
    OPEN_STATUSES,
    _location,
    _subscriber_name,
    _system_user,
    _technician_name,
)
from app.services.status_presentation import work_order_status_presentation
from app.services.work_order_commands import work_order_commands

DEFAULT_STALE_AFTER_SECONDS = 120


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _manager_name(db: Session, principal: dict[str, Any]) -> str:
    principal_id = principal.get("principal_id")
    if principal.get("principal_type") == "system_user" and principal_id:
        try:
            user = db.get(SystemUser, coerce_uuid(str(principal_id)))
        except ValueError:
            user = None
        if user is not None:
            name = user.display_name or f"{user.first_name} {user.last_name}".strip()
            if name:
                return name
    return "Manager"


def _subscriber_label(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    label = _subscriber_name(subscriber)
    account = subscriber.account_number
    if label and account and account != label:
        return f"{label} ({account})"
    return label or account


def _active_orders_by_technician(
    db: Session, profiles: list[TechnicianProfile]
) -> dict[Any, WorkOrder]:
    """First open work order per technician profile id."""
    if not profiles:
        return {}
    open_orders = (
        db.query(WorkOrder)
        .filter(WorkOrder.is_active.is_(True))
        .filter(WorkOrder.status.in_(OPEN_STATUSES))
        .order_by(WorkOrder.updated_at.desc())
        .all()
    )
    by_crm_person: dict[str, list[WorkOrder]] = {}
    for order in open_orders:
        if order.assigned_to_crm_person_id:
            by_crm_person.setdefault(order.assigned_to_crm_person_id, []).append(order)

    order_ids = [order.id for order in open_orders]
    queue_by_mirror: dict[Any, Any] = {}
    if order_ids:
        queue_rows = (
            db.query(WorkOrderAssignmentQueue)
            .filter(WorkOrderAssignmentQueue.work_order_mirror_id.in_(order_ids))
            .filter(WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned)
            .filter(WorkOrderAssignmentQueue.assigned_technician_id.isnot(None))
            .order_by(WorkOrderAssignmentQueue.created_at.asc())
            .all()
        )
        for entry in queue_rows:
            queue_by_mirror[entry.work_order_mirror_id] = entry.assigned_technician_id

    result: dict[Any, WorkOrder] = {}
    for profile in profiles:
        if profile.crm_person_id and profile.crm_person_id in by_crm_person:
            result[profile.id] = by_crm_person[profile.crm_person_id][0]
    for order in open_orders:
        technician_id = queue_by_mirror.get(order.id)
        if technician_id is not None:
            result.setdefault(technician_id, order)
    return result


class FieldManager:
    @staticmethod
    def me(db: Session, principal: dict[str, Any]) -> dict:
        permission_keys = principal.get("permission_keys") or principal.get("scopes")
        return {
            "person_id": str(principal.get("principal_id") or ""),
            "name": _manager_name(db, principal),
            "roles": list(principal.get("roles") or []),
            "permissions": sorted(str(key) for key in permission_keys or []),
            "is_manager": True,
        }

    @staticmethod
    def list_technicians(
        db: Session,
        query: FieldManagerTechniciansQuery,
    ) -> FieldManagerTechniciansResponse:
        map_feed = field_maps.list_technician_positions(
            db=db,
            query=FieldLiveMapFeedQuery(
                stale_after_seconds=query.stale_after_seconds,
                limit=2000,
            ),
        )
        map_position_by_technician_id = {
            position.technician_id: position for position in map_feed.items
        }

        rows = (
            db.query(TechnicianProfile, FieldTechPresence)
            .outerjoin(
                FieldTechPresence,
                FieldTechPresence.technician_id == TechnicianProfile.id,
            )
            .filter(TechnicianProfile.is_active.is_(True))
            .order_by(TechnicianProfile.created_at.asc())
            .limit(query.limit)
            .all()
        )
        profiles = [profile for profile, _presence in rows]
        active_orders = _active_orders_by_technician(db, profiles)

        items: list[FieldManagerTechnician] = []
        for profile, presence in rows:
            map_position = map_position_by_technician_id.get(profile.id)
            order = active_orders.get(profile.id)
            items.append(
                FieldManagerTechnician(
                    technician_id=profile.id,
                    person_id=profile.person_id,
                    person_label=_technician_name(profile, _system_user(db, profile)),
                    title=profile.title,
                    region=profile.region,
                    status=presence.status if presence else "off_shift",
                    location_sharing_enabled=bool(
                        presence and presence.location_sharing_enabled
                    ),
                    is_live=map_position.is_live if map_position is not None else False,
                    last_seen_at=(_as_utc(presence.last_seen_at) if presence else None),
                    active_work_order=(
                        ActiveWorkOrderOutcome(
                            id=order.public_id,
                            title=order.title,
                            status=order.status,
                            status_presentation=work_order_status_presentation(
                                order.status
                            ),
                        )
                        if order is not None
                        else None
                    ),
                )
            )
        return FieldManagerTechniciansResponse(
            items=items,
            count=len(items),
            live_count=sum(1 for item in items if item.is_live),
            sharing_count=sum(1 for item in items if item.location_sharing_enabled),
            limit=query.limit,
            offset=0,
        )

    @staticmethod
    def summary(
        db: Session,
        *,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    ) -> dict:
        technicians = FieldManager.list_technicians(
            db,
            FieldManagerTechniciansQuery(
                stale_after_seconds=stale_after_seconds,
            ),
        )
        open_query = (
            db.query(WorkOrder)
            .filter(WorkOrder.is_active.is_(True))
            .filter(WorkOrder.status.in_(OPEN_STATUSES))
        )
        open_jobs = open_query.count()
        assigned_mirror_ids = select(
            WorkOrderAssignmentQueue.work_order_mirror_id
        ).filter(
            WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
            WorkOrderAssignmentQueue.assigned_technician_id.isnot(None),
        )
        unassigned_jobs = (
            open_query.filter(WorkOrder.assigned_to_crm_person_id.is_(None))
            .filter(WorkOrder.id.notin_(assigned_mirror_ids))
            .count()
        )
        pending_expenses = (
            db.query(FieldExpenseRequest)
            .filter(FieldExpenseRequest.is_active.is_(True))
            .filter(FieldExpenseRequest.status == "submitted")
            .count()
        )
        return {
            "technicians_total": technicians.count,
            "technicians_live": technicians.live_count,
            "technicians_sharing": technicians.sharing_count,
            "open_jobs": open_jobs,
            "unassigned_jobs": unassigned_jobs,
            "pending_expenses": pending_expenses,
        }

    @staticmethod
    def list_jobs(
        db: Session,
        *,
        status: str | None = None,
        assigned_to_person_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        query = db.query(WorkOrder).filter(WorkOrder.is_active.is_(True))
        if status:
            query = query.filter(WorkOrder.status == status)
        else:
            query = query.filter(WorkOrder.status.in_(OPEN_STATUSES))
        if assigned_to_person_id:
            profile = _technician_by_person_id(db, assigned_to_person_id)
            query = _filter_assigned_to(db, query, profile)
        query = query.order_by(
            WorkOrder.scheduled_start.asc().nullslast(),
            WorkOrder.created_at.desc(),
        )
        rows = apply_pagination(query, limit, offset).all()
        return [FieldManager._job_payload(db, row) for row in rows]

    @staticmethod
    def assign_job(
        db: Session,
        crm_work_order_id: str,
        *,
        person_id: str,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
        status: str | None = None,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict:
        row = (
            db.query(WorkOrder)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .filter(WorkOrder.is_active.is_(True))
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        profile = _technician_by_person_id(db, person_id)
        work_order_commands.assign(
            db,
            row.public_id,
            technician_id=profile.id,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            status=(status or "dispatched"),
            reason="manager_assign",
            auth=auth,
            request_id=request_id,
        )
        db.refresh(row)
        return FieldManager._job_payload(db, row)

    @staticmethod
    def _job_payload(db: Session, row: WorkOrder) -> dict:
        location = _location(row)
        subscriber = db.get(Subscriber, row.subscriber_id)
        assignment = _current_assignment(db, row)
        profile = _assigned_profile(db, row)
        assigned_label = row.technician_name or row.assigned_to_name
        if assigned_label is None and profile is not None:
            assigned_label = _technician_name(profile, _system_user(db, profile))
        return {
            "id": row.public_id,
            "work_order_mirror_id": row.id,
            "assignment_queue_id": assignment.id if assignment is not None else None,
            "title": row.title,
            "description": row.description,
            "status": row.status,
            "status_presentation": work_order_status_presentation(row.status),
            "priority": row.priority,
            "work_type": row.work_type,
            "scheduled_start": row.scheduled_start,
            "scheduled_end": row.scheduled_end,
            "assigned_to_person_id": profile.person_id if profile else None,
            "assigned_to_label": assigned_label,
            "subscriber_label": _subscriber_label(subscriber),
            "address_text": location.address_text,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "location_source": location.source,
        }


def _technician_by_person_id(db: Session, person_id: str) -> TechnicianProfile:
    try:
        person_uuid = coerce_uuid(str(person_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid person_id") from exc
    profile = (
        db.query(TechnicianProfile)
        .filter(TechnicianProfile.is_active.is_(True))
        .filter(
            (TechnicianProfile.person_id == person_uuid)
            | (TechnicianProfile.system_user_id == person_uuid)
        )
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Technician not found")
    return profile


def _assigned_profile(db: Session, row: WorkOrder) -> TechnicianProfile | None:
    if row.assigned_to_crm_person_id:
        profile = (
            db.query(TechnicianProfile)
            .filter(TechnicianProfile.crm_person_id == row.assigned_to_crm_person_id)
            .filter(TechnicianProfile.is_active.is_(True))
            .first()
        )
        if profile is not None:
            return profile
    entry = _current_assignment(db, row)
    if entry is None:
        return None
    return db.get(TechnicianProfile, entry.assigned_technician_id)


def _current_assignment(db: Session, row: WorkOrder) -> WorkOrderAssignmentQueue | None:
    return (
        db.query(WorkOrderAssignmentQueue)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id == row.id)
        .filter(WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned)
        .filter(WorkOrderAssignmentQueue.assigned_technician_id.isnot(None))
        .order_by(WorkOrderAssignmentQueue.created_at.desc())
        .first()
    )


def _filter_assigned_to(db: Session, query, profile: TechnicianProfile):
    assignment_ids = select(WorkOrderAssignmentQueue.work_order_mirror_id).filter(
        WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
        WorkOrderAssignmentQueue.assigned_technician_id == profile.id,
    )
    clauses: list[Any] = [WorkOrder.id.in_(assignment_ids)]
    if profile.crm_person_id:
        clauses.append(WorkOrder.assigned_to_crm_person_id == profile.crm_person_id)
    return query.filter(or_(*clauses))


field_manager = FieldManager()
