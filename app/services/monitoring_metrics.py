"""Device monitoring metric collection and VictoriaMetrics push services.

Handles:
- Pushing device/ONU metrics to VictoriaMetrics
- Subscriber impact counting on device outages
- Device metrics TTL cleanup
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    DeviceMetric,
    DeviceStatus,
    MetricType,
    NetworkDevice,
)

logger = logging.getLogger(__name__)

_VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")


# ── VictoriaMetrics Push ─────────────────────────────────────────────────


def push_metrics_to_victoriametrics(metrics_lines: list[str]) -> bool:
    """Push Prometheus-format metric lines to VictoriaMetrics.

    Args:
        metrics_lines: List of Prometheus text format lines, e.g.:
            'cpu_usage{device_id="abc",device_name="Router1"} 42.5'

    Returns:
        True if push succeeded, False otherwise.
    """
    if not metrics_lines:
        return True

    payload = "\n".join(metrics_lines) + "\n"
    try:
        resp = httpx.post(
            f"{_VICTORIAMETRICS_URL}/api/v1/import/prometheus",
            content=payload,
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return True
        logger.warning(
            "VictoriaMetrics push returned %d: %s", resp.status_code, resp.text[:200]
        )
        return False
    except Exception as exc:
        logger.warning("VictoriaMetrics push failed: %s", exc)
        return False


def push_device_health_metrics(
    device: NetworkDevice, metrics: dict[str, float]
) -> None:
    """Push a device's health metrics to VictoriaMetrics.

    Args:
        device: The network device.
        metrics: Dict of {metric_name: value}, e.g. {"cpu": 42.5, "memory": 65.2}
    """
    lines = []
    device_name = (device.name or "").replace('"', '\\"')
    for name, value in metrics.items():
        lines.append(
            f'device_{name}{{device_id="{device.id}",device_name="{device_name}"}} {value}'
        )
    push_metrics_to_victoriametrics(lines)


# ── Subscriber Impact ────────────────────────────────────────────────────


def count_affected_subscribers(db: Session, device: NetworkDevice) -> int:
    """Count subscribers potentially affected by a device outage.

    Checks:
    1. Active RADIUS accounting sessions with NAS IP matching device mgmt_ip
    2. ONT assignments on OLTs linked to this device
    """
    count = 0

    linked_nas = getattr(device, "nas_device", None)
    if linked_nas is not None:
        # Count active RADIUS sessions on this NAS
        try:
            from app.models.usage import AccountingStatus, RadiusAccountingSession

            count = (
                db.scalar(
                    select(func.count())
                    .select_from(RadiusAccountingSession)
                    .where(
                        RadiusAccountingSession.nas_device_id == linked_nas.id,
                        RadiusAccountingSession.session_end.is_(None),
                        RadiusAccountingSession.status_type != AccountingStatus.stop,
                    )
                )
                or 0
            )
        except Exception as exc:
            logger.warning(
                "Could not count RADIUS sessions for device %s: %s", device.name, exc
            )

    return count


def update_device_subscriber_count(db: Session, device: NetworkDevice) -> int:
    """Update the subscriber count on a device and return the count."""
    count = count_affected_subscribers(db, device)
    device.current_subscriber_count = count
    return count


# ── Metrics Cleanup ──────────────────────────────────────────────────────


def cleanup_old_device_metrics(db: Session, retention_days: int = 90) -> int:
    """Delete device metrics older than retention_days.

    Deletes in batches to avoid long table locks.

    Returns:
        Total number of deleted records.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    total_deleted = 0
    batch_size = 10_000

    while True:
        # Use a subquery to limit the delete to batch_size rows
        subq = (
            select(DeviceMetric.id)
            .where(DeviceMetric.recorded_at < cutoff)
            .limit(batch_size)
            .subquery()
        )
        result = cast(
            CursorResult[Any],
            db.execute(
                delete(DeviceMetric).where(DeviceMetric.id.in_(select(subq.c.id)))
            ),
        )
        deleted = result.rowcount
        db.commit()
        total_deleted += deleted

        if deleted < batch_size:
            break

    if total_deleted > 0:
        logger.info(
            "Cleaned up %d device metrics older than %d days",
            total_deleted,
            retention_days,
        )

    return total_deleted


# ── NAS → Monitoring Sync ────────────────────────────────────────────────

# Map NAS vendor to monitoring DeviceType
_VENDOR_TO_DEVICE_TYPE = {
    "mikrotik": "router",
    "huawei": "switch",
    "cisco": "router",
    "juniper": "router",
    "ubiquiti": "access_point",
    "cambium": "access_point",
    "nokia": "switch",
    "zte": "switch",
    "other": "other",
}


def sync_nas_to_monitoring(db: Session, nas_id: str) -> NetworkDevice:
    """Create or update a NetworkDevice record from a NasDevice.

    Links the NAS device to the monitoring system by:
    1. Creating a NetworkDevice if one doesn't exist
    2. Copying network config (IP, SNMP community, vendor, model)
    3. Enabling ping and SNMP monitoring
    4. Setting the NasDevice.network_device_id FK

    Returns the NetworkDevice record.
    """
    from app.models.catalog import NasDevice
    from app.models.network_monitoring import DeviceRole, DeviceType

    nas = db.get(NasDevice, nas_id)
    if not nas:
        raise ValueError(f"NAS device {nas_id} not found")

    mgmt_ip = nas.management_ip or nas.ip_address

    # Check if already linked
    if nas.network_device_id:
        existing = db.get(NetworkDevice, nas.network_device_id)
        if existing:
            if mgmt_ip and existing.mgmt_ip != mgmt_ip:
                by_ip = db.scalars(
                    select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
                ).first()
                if by_ip and by_ip.id != existing.id:
                    nas.network_device_id = by_ip.id
                    _sync_nas_fields_to_device(nas, by_ip)
                    db.flush()
                    return by_ip
            # Update fields from NAS
            _sync_nas_fields_to_device(nas, existing)
            db.flush()
            return existing

    # Check if a device already exists with this mgmt IP (dedup)
    if mgmt_ip:
        existing = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        ).first()
        if existing:
            nas.network_device_id = existing.id
            _sync_nas_fields_to_device(nas, existing)
            db.flush()
            return existing

    # Create new NetworkDevice
    vendor_str = nas.vendor.value if nas.vendor else "other"
    device_type_str = _VENDOR_TO_DEVICE_TYPE.get(vendor_str, "other")

    device = NetworkDevice(
        name=nas.name,
        hostname=nas.name,
        mgmt_ip=mgmt_ip,
        vendor=nas.vendor.value if nas.vendor else None,
        model=nas.model,
        serial_number=nas.serial_number,
        device_type=DeviceType(device_type_str),
        role=DeviceRole.access,
        ping_enabled=True,
        snmp_enabled=bool(nas.snmp_community),
        snmp_community=nas.snmp_community,
        snmp_version=nas.snmp_version or "2c",
        snmp_port=nas.snmp_port or 161,
        pop_site_id=nas.pop_site_id,
        max_concurrent_subscribers=nas.max_concurrent_subscribers,
        notes=f"Auto-created from NAS device: {nas.name}",
    )
    db.add(device)
    db.flush()

    # Link back
    nas.network_device_id = device.id
    db.flush()

    logger.info(
        "Created monitoring device %s from NAS %s (%s)", device.id, nas.name, mgmt_ip
    )
    return device


def _sync_nas_fields_to_device(nas, device: NetworkDevice) -> None:
    """Copy relevant fields from NAS to NetworkDevice."""
    device.name = nas.name
    device.mgmt_ip = nas.management_ip or nas.ip_address
    device.vendor = nas.vendor.value if nas.vendor else device.vendor
    device.model = nas.model or device.model
    device.serial_number = nas.serial_number or device.serial_number
    if nas.snmp_community and not device.snmp_community:
        device.snmp_community = nas.snmp_community
        device.snmp_enabled = True
    if nas.pop_site_id:
        device.pop_site_id = nas.pop_site_id
    if nas.max_concurrent_subscribers:
        device.max_concurrent_subscribers = nas.max_concurrent_subscribers


def sync_all_nas_to_monitoring(db: Session) -> dict[str, int]:
    """Sync all active NAS devices to the monitoring system.

    Returns:
        {synced, skipped, errors}
    """
    from app.models.catalog import NasDevice

    nas_devices = list(
        db.scalars(select(NasDevice).where(NasDevice.is_active.is_(True))).all()
    )

    synced = 0
    skipped = 0
    errors = 0

    for nas in nas_devices:
        # Skip NAS devices without a usable IP
        mgmt_ip = nas.management_ip or nas.ip_address
        if not mgmt_ip:
            skipped += 1
            continue

        try:
            with db.begin_nested():  # savepoint — isolates per-NAS
                sync_nas_to_monitoring(db, str(nas.id))
            synced += 1
        except Exception as exc:
            errors += 1
            logger.warning("Failed to sync NAS %s to monitoring: %s", nas.name, exc)

    db.commit()
    logger.info(
        "NAS monitoring sync complete: synced=%d skipped=%d errors=%d",
        synced,
        skipped,
        errors,
    )
    return {"synced": synced, "skipped": skipped, "errors": errors}


# ── RouterOS → Monitoring Sync ───────────────────────────────────────────


def _router_status_to_monitoring_status(router_status) -> DeviceStatus:
    """Map RouterOS inventory status to NetworkDevice status."""
    from app.models.router_management import RouterStatus

    if router_status == RouterStatus.online:
        return DeviceStatus.online
    if router_status == RouterStatus.degraded:
        return DeviceStatus.degraded
    if router_status == RouterStatus.maintenance:
        return DeviceStatus.maintenance
    return DeviceStatus.offline


def sync_router_to_monitoring(db: Session, router_id: str) -> NetworkDevice:
    """Create or update a NetworkDevice record from a RouterOS Router row."""
    from app.models.network_monitoring import DeviceRole, DeviceType
    from app.models.router_management import Router

    router = db.get(Router, router_id)
    if not router:
        raise ValueError(f"Router {router_id} not found")

    mgmt_ip = (router.management_ip or "").strip()

    if router.network_device_id:
        existing = db.get(NetworkDevice, router.network_device_id)
        if existing:
            if mgmt_ip and existing.mgmt_ip != mgmt_ip:
                by_ip = db.scalars(
                    select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
                ).first()
                if by_ip and by_ip.id != existing.id:
                    router.network_device_id = by_ip.id
                    _sync_router_fields_to_device(db, router, by_ip)
                    db.flush()
                    return by_ip
            _sync_router_fields_to_device(db, router, existing)
            db.flush()
            return existing

    if mgmt_ip:
        existing = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        ).first()
        if existing:
            router.network_device_id = existing.id
            _sync_router_fields_to_device(db, router, existing)
            db.flush()
            return existing

    device = NetworkDevice(
        name=router.name,
        hostname=router.hostname,
        mgmt_ip=mgmt_ip or None,
        vendor="mikrotik",
        model=router.board_name,
        serial_number=router.serial_number,
        device_type=DeviceType.router,
        role=DeviceRole.edge,
        status=_router_status_to_monitoring_status(router.status),
        ping_enabled=True,
        snmp_enabled=False,
        notes=f"Auto-created from RouterOS device: {router.name}",
        is_active=router.is_active,
    )
    db.add(device)
    db.flush()

    router.network_device_id = device.id
    db.flush()

    logger.info(
        "Created monitoring device %s from router %s (%s)",
        device.id,
        router.name,
        mgmt_ip,
    )
    return device


def _sync_router_fields_to_device(db: Session, router, device: NetworkDevice) -> None:
    """Copy non-secret RouterOS inventory fields to NetworkDevice.

    Ownership: this is a field projection from router inventory into monitoring
    inventory, and admission is deliberately NOT one of the copied fields.
    ``device.is_active = router.is_active`` used to sit here — a
    ``router_management`` writer performing a ``network.monitoring_inventory``
    lifecycle transition through a raw attribute assignment, which skipped the
    reachability-cache decay and left the device frozen at its last state.

    The boundary decision (recorded in the SOT map): a monitoring device
    auto-created from a router has no independent existence, so the router
    remains the authoritative *input* to its admission — but the transition is
    requested from the owner rather than written here.
    """
    from app.models.network_monitoring import DeviceType
    from app.services.network_monitoring import set_network_device_active

    device.name = router.name
    device.hostname = router.hostname or device.hostname
    device.mgmt_ip = (router.management_ip or "").strip() or device.mgmt_ip
    device.vendor = device.vendor or "mikrotik"
    device.model = router.board_name or device.model
    device.serial_number = router.serial_number or device.serial_number
    device.device_type = device.device_type or DeviceType.router
    device.status = _router_status_to_monitoring_status(router.status)
    device.ping_enabled = True
    if router.location and not device.notes:
        device.notes = router.location
    set_network_device_active(
        db, device, bool(router.is_active), reason="router_inventory_sync"
    )


def sync_all_routers_to_monitoring(db: Session) -> dict[str, int]:
    """Sync RouterOS inventory rows to the monitoring system.

    Scope deliberately includes inactive routers that still carry a linked
    monitoring device. Selecting only ``is_active`` routers made deactivation
    one-way: once a router was soft-deleted the sweep never looked at it again,
    so its monitoring device could never be revisited — neither to converge a
    deactivation that half-applied, nor to re-admit it if the router came back.
    A sweep that cannot repair in both directions is not a reconciler.
    """
    from app.models.router_management import Router

    routers = list(
        db.scalars(
            select(Router).where(
                or_(
                    Router.is_active.is_(True),
                    Router.network_device_id.isnot(None),
                )
            )
        ).all()
    )

    synced = 0
    skipped = 0
    errors = 0

    for router in routers:
        if not (router.management_ip or "").strip():
            skipped += 1
            continue

        try:
            with db.begin_nested():
                sync_router_to_monitoring(db, str(router.id))
            synced += 1
        except Exception as exc:
            errors += 1
            logger.warning(
                "Failed to sync router %s to monitoring: %s", router.name, exc
            )

    db.commit()
    logger.info(
        "Router monitoring sync complete: synced=%d skipped=%d errors=%d",
        synced,
        skipped,
        errors,
    )
    return {"synced": synced, "skipped": skipped, "errors": errors}


def sync_inventory_to_monitoring(db: Session) -> dict[str, dict[str, int]]:
    """Sync local device inventories that are mirrored into NetworkDevice."""
    return {
        "nas": sync_all_nas_to_monitoring(db),
        "routers": sync_all_routers_to_monitoring(db),
    }


def _latest_metric_value(
    db: Session, device_id, metric_type: MetricType
) -> float | None:
    """Get the most recent metric value for a device."""
    row = db.scalars(
        select(DeviceMetric.value)
        .where(
            DeviceMetric.device_id == device_id, DeviceMetric.metric_type == metric_type
        )
        .order_by(DeviceMetric.recorded_at.desc())
        .limit(1)
    ).first()
    return float(row) if row is not None else None


def poll_onu_signal_strength(
    db: Session,
    olt_device: NetworkDevice,
) -> dict[str, int]:
    """Query ONT signal status from persisted inventory columns."""
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.olt_polling import get_signal_thresholds

    host = str(olt_device.mgmt_ip or olt_device.hostname or "").strip()
    if not host:
        return {"polled": 0, "stored": 0, "low_signal": 0, "errors": 0}

    olt = db.scalars(
        select(OLTDevice).where(
            OLTDevice.is_active.is_(True),
            (OLTDevice.mgmt_ip == host) | (OLTDevice.hostname == host),
        )
    ).first()
    if not olt:
        logger.warning(
            "Monitoring device %s has no matching OLT inventory record",
            olt_device.name,
        )
        return {"polled": 0, "stored": 0, "low_signal": 0, "errors": 0}

    warning_threshold, _ = get_signal_thresholds(db, olt=olt)
    base_query = (
        select(func.count())
        .select_from(OntUnit)
        .where(OntUnit.is_active.is_(True))
        .where(OntUnit.olt_device_id == olt.id)
    )
    total = db.scalar(base_query) or 0
    stored = db.scalar(base_query.where(OntUnit.olt_rx_signal_dbm.is_not(None))) or 0
    low_signal = (
        db.scalar(
            base_query.where(OntUnit.olt_rx_signal_dbm.is_not(None)).where(
                OntUnit.olt_rx_signal_dbm < warning_threshold
            )
        )
        or 0
    )

    return {
        "polled": int(total),
        "stored": int(stored),
        "low_signal": int(low_signal),
        "errors": 0,
    }
