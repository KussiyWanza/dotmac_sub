"""Native read views over Sub-owned work orders.

During migration, CRM can still hydrate legacy work-order headers into
``work_order`` (identified by native ``public_id`` with a root-only
``crm_work_order_id`` provenance reference). Native field
execution activity is authored in sub, and this module is the in-process read
layer admin, dispatch, and field APIs use without fanning out to CRM.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.work_order import WorkOrder
from app.services.common import coerce_uuid
from app.services.field.work_order_status import WORK_ORDER_TERMINAL_VALUES

TERMINAL_STATUSES = WORK_ORDER_TERMINAL_VALUES
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class WorkOrderListFilters:
    status: str | None = None
    priority: str | None = None
    work_type: str | None = None
    subscriber_id: str | None = None
    project_id: str | None = None
    project_task_id: str | None = None
    crm_ticket_id: str | None = None
    crm_project_id: str | None = None
    assigned_to_crm_person_id: str | None = None
    q: str | None = None
    is_active: bool | None = True
    # Non-terminal lifecycle filter (distinct from ``is_active`` soft-delete):
    # True keeps open work, False keeps only terminal ones. None applies neither.
    active: bool | None = None
    scheduled_from: datetime | None = None
    scheduled_to: datetime | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0


@dataclass(frozen=True, slots=True)
class LinkedWorkOrderSummary:
    """Typed native work-order identity projected into project UI surfaces."""

    id: UUID
    public_id: str
    title: str
    status: str
    project_id: UUID | None
    project_task_id: UUID | None


def _linked_summary(row: WorkOrder) -> LinkedWorkOrderSummary:
    return LinkedWorkOrderSummary(
        id=row.id,
        public_id=row.public_id,
        title=row.title,
        status=row.status,
        project_id=row.project_id,
        project_task_id=row.project_task_id,
    )


def list_project_work_order_summaries(
    db: Session, project_id: UUID
) -> tuple[LinkedWorkOrderSummary, ...]:
    """Return every active native work order linked to one project."""

    rows = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.is_active.is_(True),
            WorkOrder.project_id == project_id,
        )
        .order_by(WorkOrder.created_at.desc(), WorkOrder.id.asc())
        .all()
    )
    return tuple(_linked_summary(row) for row in rows)


def list_task_work_order_summaries_bulk(
    db: Session, project_task_ids: Collection[UUID]
) -> dict[UUID, tuple[LinkedWorkOrderSummary, ...]]:
    """Load active native work-order summaries for a task page in one query."""

    task_ids = tuple(dict.fromkeys(project_task_ids))
    if not task_ids:
        return {}
    grouped: dict[UUID, list[LinkedWorkOrderSummary]] = {
        task_id: [] for task_id in task_ids
    }
    rows = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.is_active.is_(True),
            WorkOrder.project_task_id.in_(task_ids),
        )
        .order_by(
            WorkOrder.project_task_id.asc(),
            WorkOrder.created_at.desc(),
            WorkOrder.id.asc(),
        )
        .all()
    )
    for row in rows:
        if row.project_task_id in grouped:
            grouped[row.project_task_id].append(_linked_summary(row))
    return {task_id: tuple(grouped[task_id]) for task_id in task_ids}


def list_task_work_order_summaries(
    db: Session, project_task_id: UUID
) -> tuple[LinkedWorkOrderSummary, ...]:
    """Return active native work orders linked to one project task."""

    return list_task_work_order_summaries_bulk(db, (project_task_id,)).get(
        project_task_id, ()
    )


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _subscriber_name(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    full = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    return (
        subscriber.company_name or full or subscriber.email or subscriber.account_number
    )


def _subscriber_snapshot(row: WorkOrder, subscriber: Subscriber | None) -> dict:
    return {
        "account_id": str(row.subscriber_id),
        "account_name": _subscriber_name(subscriber),
        "account_number": subscriber.account_number if subscriber else None,
        "account_email": subscriber.email if subscriber else None,
        "account_phone": subscriber.phone if subscriber else None,
    }


def row_to_item(
    row: WorkOrder,
    *,
    subscriber: Subscriber | None = None,
    include_internal: bool = True,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        # Compat window: id keeps its historical value (== public_id) while
        # old clients still key on it; public_id is the forward identity.
        "id": row.public_id,
        "public_id": row.public_id,
        "title": row.title,
        "status": row.status,
        "work_type": row.work_type,
        "priority": row.priority,
        "technician_name": row.technician_name or row.assigned_to_name,
        "technician_phone": row.technician_phone,
        "address": row.address,
        "scheduled_start": _dt(row.scheduled_start),
        "scheduled_end": _dt(row.scheduled_end),
        "estimated_arrival_at": _dt(row.estimated_arrival_at),
        "estimated_duration_minutes": row.estimated_duration_minutes,
        "started_at": _dt(row.started_at),
        "paused_at": _dt(row.paused_at),
        "resumed_at": _dt(row.resumed_at),
        "completed_at": _dt(row.completed_at),
        "total_active_seconds": row.total_active_seconds,
        "created_at": _dt(row.work_order_created_at),
    }
    if include_internal:
        item.update(
            {
                "description": row.description,
                "crm_ticket_id": row.crm_ticket_id,
                "crm_project_id": row.crm_project_id,
                "assigned_to_crm_person_id": row.assigned_to_crm_person_id,
                "assigned_to_name": row.assigned_to_name,
                "required_skills": row.required_skills or [],
                "tags": row.tags or [],
                "access_notes": row.access_notes,
                "is_active": row.is_active,
                "metadata": row.metadata_ or {},
            }
        )
    if subscriber is not None:
        item.update(_subscriber_snapshot(row, subscriber))
    return item


def _base_query(db: Session):
    return db.query(WorkOrder, Subscriber).join(
        Subscriber, Subscriber.id == WorkOrder.subscriber_id
    )


def _apply_filters(query, filters: WorkOrderListFilters):
    if filters.is_active is not None:
        query = query.filter(WorkOrder.is_active.is_(filters.is_active))
    if filters.active is not None:
        if filters.active:
            query = query.filter(WorkOrder.status.notin_(TERMINAL_STATUSES))
        else:
            query = query.filter(WorkOrder.status.in_(TERMINAL_STATUSES))
    if filters.status:
        query = query.filter(WorkOrder.status == filters.status)
    if filters.priority:
        query = query.filter(WorkOrder.priority == filters.priority)
    if filters.work_type:
        query = query.filter(WorkOrder.work_type == filters.work_type)
    if filters.subscriber_id:
        query = query.filter(
            WorkOrder.subscriber_id == coerce_uuid(filters.subscriber_id)
        )
    if filters.project_id:
        query = query.filter(WorkOrder.project_id == coerce_uuid(filters.project_id))
    if filters.project_task_id:
        query = query.filter(
            WorkOrder.project_task_id == coerce_uuid(filters.project_task_id)
        )
    if filters.crm_ticket_id:
        query = query.filter(WorkOrder.crm_ticket_id == filters.crm_ticket_id)
    if filters.crm_project_id:
        query = query.filter(WorkOrder.crm_project_id == filters.crm_project_id)
    if filters.assigned_to_crm_person_id:
        query = query.filter(
            WorkOrder.assigned_to_crm_person_id == filters.assigned_to_crm_person_id
        )
    if filters.scheduled_from:
        query = query.filter(WorkOrder.scheduled_start >= filters.scheduled_from)
    if filters.scheduled_to:
        query = query.filter(WorkOrder.scheduled_start <= filters.scheduled_to)
    q = (filters.q or "").strip()
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                WorkOrder.title.ilike(pattern),
                WorkOrder.address.ilike(pattern),
                WorkOrder.public_id.ilike(pattern),
                Subscriber.first_name.ilike(pattern),
                Subscriber.last_name.ilike(pattern),
                Subscriber.company_name.ilike(pattern),
                Subscriber.email.ilike(pattern),
                Subscriber.account_number.ilike(pattern),
            )
        )
    return query


_SORT_COLUMNS = {
    "status": WorkOrder.status,
    "priority": WorkOrder.priority,
    "scheduled_start": WorkOrder.scheduled_start,
    "created_at": WorkOrder.created_at,
}


def query_work_orders(
    db: Session,
    filters: WorkOrderListFilters | None = None,
    *,
    sort_by: str | None = None,
    sort_dir: str | None = None,
) -> tuple[list[tuple[WorkOrder, Subscriber]], int]:
    """Owner query for the work-order list.

    Returns filtered, sorted, paginated ``(WorkOrder, Subscriber)`` rows and
    the total. UI list projections declare the sort/filter/pagination contract and
    delegate the read here; they never rebuild this query. ``sort_by``/``sort_dir``
    default to the canonical schedule-then-created ordering when unset, so existing
    callers are unchanged.
    """
    filters = filters or WorkOrderListFilters()
    limit = min(max(int(filters.limit or DEFAULT_LIMIT), 1), MAX_LIMIT)
    offset = max(int(filters.offset or 0), 0)

    filtered = _apply_filters(_base_query(db), filters)
    total = int(filtered.with_entities(func.count(WorkOrder.id)).scalar() or 0)

    column = _SORT_COLUMNS.get(sort_by or "")
    if column is not None:
        primary = column.desc() if str(sort_dir).lower() == "desc" else column.asc()
        ordering = [primary.nullslast(), WorkOrder.id.asc()]
    else:
        ordering = [
            WorkOrder.scheduled_start.asc().nullslast(),
            WorkOrder.created_at.desc(),
            WorkOrder.id.asc(),
        ]
    rows = filtered.order_by(*ordering).limit(limit).offset(offset).all()
    return rows, total


def list_work_orders(db: Session, filters: WorkOrderListFilters | None = None) -> dict:
    filters = filters or WorkOrderListFilters()
    rows, total = query_work_orders(db, filters)
    items = [
        row_to_item(row, subscriber=subscriber, include_internal=True)
        for row, subscriber in rows
    ]
    return {
        "work_orders": items,
        "total": total,
        "limit": min(max(int(filters.limit or DEFAULT_LIMIT), 1), MAX_LIMIT),
        "offset": max(int(filters.offset or 0), 0),
        "summary": summary(db, filters),
    }


def get_work_order(db: Session, public_id: str) -> dict | None:
    pair = _base_query(db).filter(WorkOrder.public_id == public_id).first()
    if pair is None:
        return None
    row, subscriber = pair
    return row_to_item(row, subscriber=subscriber, include_internal=True)


def summary(db: Session, filters: WorkOrderListFilters | None = None) -> dict:
    filters = filters or WorkOrderListFilters()
    query = _apply_filters(_base_query(db), filters)
    rows = query.with_entities(WorkOrder.status, func.count(WorkOrder.id)).group_by(
        WorkOrder.status
    )
    by_status = {str(status): int(count) for status, count in rows}
    total = sum(by_status.values())
    terminal = sum(
        count for status, count in by_status.items() if status in TERMINAL_STATUSES
    )
    open_count = total - terminal
    now = datetime.now(UTC)
    overdue = int(
        _apply_filters(_base_query(db), filters)
        .filter(WorkOrder.scheduled_start < now)
        .filter(WorkOrder.status.notin_(TERMINAL_STATUSES))
        .with_entities(func.count(WorkOrder.id))
        .scalar()
        or 0
    )
    return {
        "total": total,
        "open": open_count,
        "terminal": terminal,
        "overdue": overdue,
        "by_status": by_status,
    }


def options(db: Session) -> dict[str, list[str]]:
    def values(column) -> list[str]:
        return [
            str(value)
            for value in db.scalars(
                select(column).where(column.isnot(None)).distinct().order_by(column)
            ).all()
            if value
        ]

    return {
        "statuses": values(WorkOrder.status),
        "priorities": values(WorkOrder.priority),
        "work_types": values(WorkOrder.work_type),
    }
