"""Canonical read of the materialised device projection (``device_projections``).

The reconciler ``network.device_projection`` owns writing the table; this module
owns reading it for the admin device list — translating list request state into
SQL search / filter / sort / pagination and computing summary stats, so the web
layer never loads every device and filters in memory.

Kept separate from ``device_projection_reconcile`` (which imports the device
derivation) so the read path carries no dependency on ``collect_devices``.

The projected ``operational_status`` is the binary outcome owned by
``network.device_state``. Observation age is consumed by that owner when it
decides whether verification is due; callers never turn age into a third state.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceProjection, NetworkDeviceLifecycleState
from app.services.device_operational_status import DeviceOperationalState
from app.services.network_device_status_presentation import (
    NetworkDeviceListStatusContext,
    network_device_list_status_presentation,
)

# Fields the free-text search matches (mirrors the legacy in-memory search).
_SEARCH_COLUMNS = (
    DeviceProjection.name,
    DeviceProjection.serial_number,
    DeviceProjection.ip_address,
    DeviceProjection.vendor,
    DeviceProjection.model,
)
_SORT_COLUMNS = {
    "name": DeviceProjection.name,
    "last_seen": DeviceProjection.last_seen,
}


def _apply_filters(
    stmt,
    *,
    device_type: str | None,
    status: str | None,
    vendor: str | None,
    search: str | None,
    lifecycle: str | None,
):
    if device_type and device_type != "all":
        stmt = stmt.where(DeviceProjection.device_type == device_type)
    if status:
        stmt = stmt.where(
            func.lower(DeviceProjection.operational_status) == status.strip().lower()
        )
    if vendor:
        stmt = stmt.where(
            func.lower(func.coalesce(DeviceProjection.vendor, ""))
            == vendor.strip().lower()
        )
    normalized_lifecycle = (lifecycle or "current").strip().lower()
    if normalized_lifecycle == "archived":
        stmt = stmt.where(DeviceProjection.lifecycle_state == "archived")
    elif normalized_lifecycle in {"active", "inactive"}:
        stmt = stmt.where(DeviceProjection.lifecycle_state == normalized_lifecycle)
    else:
        stmt = stmt.where(DeviceProjection.lifecycle_state != "archived")
    term = (search or "").strip().lower()
    if term:
        like = f"%{term}%"
        clauses = [
            func.lower(func.coalesce(col, "")).like(like) for col in _SEARCH_COLUMNS
        ]
        clauses.append(func.lower(DeviceProjection.device_type).like(like))
        stmt = stmt.where(or_(*clauses))
    return stmt


def _row_to_dict(row: DeviceProjection) -> dict:
    return {
        "id": row.source_id,
        "name": row.name,
        "type": row.device_type,
        "serial_number": row.serial_number,
        "ip_address": row.ip_address,
        "vendor": row.vendor,
        "model": row.model,
        "status": row.operational_status,
        "operational_reason": row.operational_reason,
        "status_presentation": network_device_list_status_presentation(
            NetworkDeviceListStatusContext(
                operational_status=DeviceOperationalState(row.operational_status),
                lifecycle_state=NetworkDeviceLifecycleState(row.lifecycle_state),
            )
        ),
        "last_seen": row.last_seen,
        "subscriber": row.subscriber_id,
        "class_facts": row.class_facts,
        "lifecycle_state": row.lifecycle_state,
        "is_inactive": row.lifecycle_state != "active",
        "is_archived": row.lifecycle_state == "archived",
    }


def query_device_projections(
    db: Session,
    *,
    device_type: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    search: str | None = None,
    lifecycle: str | None = None,
    sort_by: str = "name",
    sort_dir: str = "asc",
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[dict], int]:
    """Return one filtered/sorted page of projected devices and the total count."""
    filtered = _apply_filters(
        select(DeviceProjection),
        device_type=device_type,
        status=status,
        vendor=vendor,
        search=search,
        lifecycle=lifecycle,
    )
    total = db.scalar(select(func.count()).select_from(filtered.subquery())) or 0
    column = _SORT_COLUMNS.get(sort_by, DeviceProjection.name)
    ordering = column.desc() if sort_dir == "desc" else column.asc()
    rows = (
        db.execute(
            filtered.order_by(ordering, DeviceProjection.id).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )
    return [_row_to_dict(row) for row in rows], int(total)


def device_projection_stats(
    db: Session,
    *,
    device_type: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    search: str | None = None,
    lifecycle: str | None = None,
) -> dict[str, int]:
    """Summary counts by type and status over the filtered set (SQL-aggregated)."""
    filtered = _apply_filters(
        select(
            DeviceProjection.device_type,
            DeviceProjection.operational_status,
            DeviceProjection.lifecycle_state,
            func.count().label("n"),
        ),
        device_type=device_type,
        status=status,
        vendor=vendor,
        search=search,
        lifecycle=lifecycle,
    ).group_by(
        DeviceProjection.device_type,
        DeviceProjection.operational_status,
        DeviceProjection.lifecycle_state,
    )

    stats = {
        "total": 0,
        "core": 0,
        "olt": 0,
        "ont": 0,
        "cpe": 0,
        "nas": 0,
        "router": 0,
        "working": 0,
        "not_working": 0,
        "inactive": 0,
        "archived": 0,
    }
    # Inactive devices stay in the projection (and in the device list) but are
    # counted separately and excluded from every in-service total. Folding them
    # into ``not_working`` would drag the operating percentage down with
    # equipment that was deliberately taken out of service, and folding them
    # into ``total`` alone would break ``working + not_working == total``.
    requested_lifecycle = (lifecycle or "current").strip().lower()
    for dtype, dstatus, row_lifecycle, count in db.execute(filtered).all():
        count = int(count or 0)
        if str(row_lifecycle or "active") == "archived":
            stats["archived"] += count
            if requested_lifecycle == "archived":
                stats["total"] += count
                if dtype in stats:
                    stats[dtype] += count
            continue
        if str(row_lifecycle or "active") != "active":
            stats["inactive"] += count
            continue
        stats["total"] += count
        if dtype in stats:
            stats[dtype] += count
        key = str(dstatus or "not_working").lower()
        if key in stats:
            stats[key] += count
    return stats


def latest_refreshed_at(db: Session) -> datetime | None:
    """Most recent projection-repair stamp for audit and drift diagnostics."""
    return db.scalar(select(func.max(DeviceProjection.refreshed_at)))
