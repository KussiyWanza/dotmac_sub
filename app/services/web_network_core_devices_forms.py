"""Form, validation, probe, and detail helpers for core-network devices."""

from __future__ import annotations

import difflib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import FormData

from app.models.catalog import ConfigBackupMethod, NasConfigBackup, NasDevice
from app.models.network_monitoring import (
    Alert,
    AlertStatus,
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    MetricType,
    NetworkDevice,
    NetworkDeviceBandwidthGraph,
    NetworkDeviceBandwidthGraphSource,
    NetworkDeviceLifecycleState,
    NetworkDeviceSnmpOid,
    PopSite,
)
from app.models.radius import RadiusClient
from app.schemas.catalog import NasDeviceUpdate
from app.services import core_device_archive
from app.services import nas as nas_service
from app.services import network as network_service
from app.services import ping as ping_service
from app.services.brand_theme import DEFAULT_SEMANTIC_COLORS
from app.services.device_operational_status import (
    NOT_WORKING,
    DeviceOperationalState,
    annotate_operational_status,
)
from app.services.network_device_status_presentation import (
    NetworkDeviceListStatusContext,
    network_device_list_status_presentation,
)
from app.services.network_monitoring import set_network_device_active


def _format_uptime_short(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 0:
        return None
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value.strip() if isinstance(value, str) else default


@dataclass
class CoreDeviceSubmitResult:
    """Result of create/update flow for core device forms."""

    device: NetworkDevice | None
    error: str | None = None
    warning: str | None = None
    snapshot: SimpleNamespace | None = None


def build_ping_command(host: str) -> list[str]:
    """Build a cross-platform ping command."""
    return ping_service.build_ping_command(host)


def integrity_error_message(exc: Exception) -> str:
    """Map DB constraint errors to user-facing messages."""
    message = str(exc)
    if "uq_network_devices_hostname" in message:
        return "Hostname already exists"
    if "uq_network_devices_mgmt_ip" in message:
        return "Management IP already exists"
    return "Device could not be saved due to a data conflict"


def pop_sites_for_forms(db: Session) -> list[PopSite]:
    """Return active POP sites for create/edit forms."""
    return list(
        db.scalars(
            select(PopSite).where(PopSite.is_active.is_(True)).order_by(PopSite.name)
        ).all()
    )


def _descendant_ids(db: Session, root_device_id: UUID) -> set[UUID]:
    """Return all descendant ids for a device."""
    descendants: set[UUID] = set()
    frontier: list[UUID] = [root_device_id]
    while frontier:
        parent_ids = frontier
        frontier = []
        children = list(
            db.scalars(
                select(NetworkDevice.id)
                .where(NetworkDevice.parent_device_id.in_(parent_ids))
                .where(NetworkDevice.is_active.is_(True))
            ).all()
        )
        for child_id in children:
            if child_id in descendants:
                continue
            descendants.add(child_id)
            frontier.append(child_id)
    return descendants


def parent_devices_for_forms(
    db: Session,
    *,
    current_device_id: UUID | None = None,
    pop_site_id: UUID | None = None,
) -> list[NetworkDevice]:
    """Return potential parent devices for create/edit forms."""
    stmt = (
        select(NetworkDevice)
        .where(NetworkDevice.is_active.is_(True))
        .order_by(NetworkDevice.name)
    )
    if pop_site_id:
        stmt = stmt.where(NetworkDevice.pop_site_id == pop_site_id)
    if current_device_id:
        excluded_ids = _descendant_ids(db, current_device_id) | {current_device_id}
        stmt = stmt.where(NetworkDevice.id.notin_(excluded_ids))
    return list(db.scalars(stmt).all())


def get_device(db: Session, device_id: str) -> NetworkDevice | None:
    """Return core device by id."""
    return db.scalars(
        select(NetworkDevice).where(NetworkDevice.id == device_id)
    ).first()


def _mutable_device(
    db: Session,
    *,
    device_id: str,
    mutation: core_device_archive.CoreDeviceMutation,
) -> tuple[NetworkDevice | None, str | None]:
    """Resolve a legacy mutation target through the archive policy owner."""
    try:
        normalized_id = UUID(str(device_id))
    except (TypeError, ValueError):
        return None, "Device not found."
    try:
        core_device_archive.require_core_device_mutable(
            db,
            core_device_archive.RequireCoreDeviceMutableRequest(
                device_id=normalized_id,
                mutation=mutation,
            ),
        )
    except core_device_archive.CoreDeviceArchiveError as exc:
        return None, exc.message
    return db.get(NetworkDevice, normalized_id), None


def build_form_context(
    *,
    device: NetworkDevice | SimpleNamespace | None,
    pop_sites: list[PopSite],
    parent_devices: list[NetworkDevice],
    selected_pop_site_id: str | None,
    current_device_id: str | None,
    action_url: str,
    error: str | None = None,
) -> dict[str, object]:
    """Build shared template context for core-device form."""
    context: dict[str, object] = {
        "device": device,
        "pop_sites": pop_sites,
        "parent_devices": parent_devices,
        "selected_pop_site_id": selected_pop_site_id,
        "current_device_id": current_device_id,
        "action_url": action_url,
    }
    if error:
        context["error"] = error
    return context


def parse_form_values(form: FormData) -> dict[str, object]:
    """Parse core-device form values into normalized strings/bools."""
    return {
        "name": _form_str(form, "name"),
        "hostname": (_form_str(form, "hostname") or None),
        "mgmt_ip": (_form_str(form, "mgmt_ip") or None),
        "role_value": _form_str(form, "role"),
        "device_type_value": _form_str(form, "device_type"),
        "pop_site_id": (_form_str(form, "pop_site_id") or None),
        "parent_device_id": (_form_str(form, "parent_device_id") or None),
        "ping_enabled": _form_str(form, "ping_enabled") == "true",
        "snmp_enabled": _form_str(form, "snmp_enabled") == "true",
        "send_notifications": _form_str(form, "send_notifications") == "true",
        "notification_delay_value": _form_str(form, "notification_delay_minutes"),
        "snmp_port_value": _form_str(form, "snmp_port"),
        "vendor": (_form_str(form, "vendor") or None),
        "model": (_form_str(form, "model") or None),
        "serial_number": (_form_str(form, "serial_number") or None),
        "snmp_version": (_form_str(form, "snmp_version") or None),
        "snmp_community": (_form_str(form, "snmp_community") or None),
        "snmp_username": (_form_str(form, "snmp_username") or None),
        "snmp_auth_protocol": (_form_str(form, "snmp_auth_protocol") or None),
        "snmp_auth_secret": (_form_str(form, "snmp_auth_secret") or None),
        "snmp_priv_protocol": (_form_str(form, "snmp_priv_protocol") or None),
        "snmp_priv_secret": (_form_str(form, "snmp_priv_secret") or None),
        "notes": (_form_str(form, "notes") or None),
        "is_active": _form_str(form, "is_active") == "true",
    }


def validate_values(
    db: Session,
    values: dict[str, object],
    *,
    current_device: NetworkDevice | None = None,
) -> tuple[dict[str, object] | None, str | None]:
    """Validate and normalize core-device form fields."""
    name = str(values.get("name") or "")
    hostname = values.get("hostname")
    mgmt_ip = values.get("mgmt_ip")
    role_value = str(values.get("role_value") or "")
    device_type_value = str(values.get("device_type_value") or "")
    pop_site_id = values.get("pop_site_id")
    parent_device_id = values.get("parent_device_id")
    ping_enabled = bool(values.get("ping_enabled"))
    snmp_enabled = bool(values.get("snmp_enabled"))
    send_notifications = bool(values.get("send_notifications"))
    notification_delay_value = str(values.get("notification_delay_value") or "")
    snmp_port_value = str(values.get("snmp_port_value") or "")

    if not name:
        return None, "Device name is required"

    try:
        role = DeviceRole(role_value)
    except ValueError:
        return None, "Invalid device role"

    try:
        device_type = DeviceType(device_type_value) if device_type_value else None
    except ValueError:
        return None, "Invalid device type"

    snmp_port: int | None
    if snmp_port_value:
        try:
            snmp_port = int(snmp_port_value)
        except ValueError:
            return None, "SNMP port must be a valid number"
    else:
        snmp_port = 161 if snmp_enabled else None

    notification_delay_minutes: int
    if notification_delay_value:
        try:
            notification_delay_minutes = int(notification_delay_value)
        except ValueError:
            return None, "Notification delay must be a valid number of minutes"
    else:
        notification_delay_minutes = 0
    if notification_delay_minutes < 0:
        return None, "Notification delay must be zero or greater"
    if notification_delay_minutes > 10080:
        return None, "Notification delay cannot exceed 10080 minutes (7 days)"
    if not send_notifications and notification_delay_minutes:
        notification_delay_minutes = 0

    if pop_site_id:
        pop_site = db.scalars(select(PopSite).where(PopSite.id == pop_site_id)).first()
        if not pop_site:
            return None, "Selected POP site was not found"

    parent_uuid: UUID | None = None
    if parent_device_id:
        try:
            parent_uuid = UUID(str(parent_device_id))
        except ValueError:
            return None, "Invalid parent device selected"
        parent_device = db.get(NetworkDevice, parent_uuid)
        if not parent_device:
            return None, "Selected parent device was not found"
        if current_device and parent_uuid == current_device.id:
            return None, "A device cannot be its own parent"
        if current_device:
            ancestor = parent_device
            visited: set[UUID] = set()
            while ancestor and ancestor.id not in visited:
                visited.add(ancestor.id)
                if ancestor.id == current_device.id:
                    return (
                        None,
                        "Invalid parent selection: would create a hierarchy cycle",
                    )
                ancestor = ancestor.parent_device

    if hostname:
        hostname_stmt = select(NetworkDevice).where(NetworkDevice.hostname == hostname)
        if current_device:
            if db.scalars(
                hostname_stmt.where(NetworkDevice.id != current_device.id)
            ).first():
                return None, "Hostname already exists"
        elif db.scalars(hostname_stmt).first():
            return None, "Hostname already exists"

    if mgmt_ip:
        mgmt_ip_stmt = select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        if current_device:
            if db.scalars(
                mgmt_ip_stmt.where(NetworkDevice.id != current_device.id)
            ).first():
                return None, "Management IP already exists"
        elif db.scalars(mgmt_ip_stmt).first():
            return None, "Management IP already exists"

    host = mgmt_ip or hostname
    if ping_enabled and not host:
        return None, "Management IP or hostname is required for ping checks."
    if snmp_enabled and not host:
        return None, "Management IP or hostname is required for SNMP collection."

    normalized = dict(values)
    normalized.update(
        {
            "role": role,
            "device_type": device_type,
            "snmp_port": snmp_port,
            "notification_delay_minutes": notification_delay_minutes,
            "host": host,
            "parent_device_id": parent_uuid,
        }
    )
    return normalized, None


def snapshot_for_form(
    values: dict[str, object],
    *,
    status: DeviceStatus | None = None,
    device_id: str | None = None,
) -> SimpleNamespace:
    """Build a template-friendly device snapshot for validation errors."""
    return SimpleNamespace(
        id=device_id,
        name=values.get("name"),
        hostname=values.get("hostname"),
        mgmt_ip=values.get("mgmt_ip"),
        role=values.get("role") or DeviceRole.edge,
        status=status or DeviceStatus.offline,
        pop_site_id=values.get("pop_site_id"),
        parent_device_id=values.get("parent_device_id"),
        vendor=values.get("vendor"),
        model=values.get("model"),
        serial_number=values.get("serial_number"),
        device_type=values.get("device_type"),
        ping_enabled=bool(values.get("ping_enabled")),
        snmp_enabled=bool(values.get("snmp_enabled")),
        send_notifications=bool(values.get("send_notifications")),
        notification_delay_minutes=values.get("notification_delay_minutes")
        or values.get("notification_delay_value")
        or 0,
        snmp_port=values.get("snmp_port"),
        snmp_version=values.get("snmp_version"),
        snmp_community=values.get("snmp_community"),
        snmp_username=values.get("snmp_username"),
        snmp_auth_protocol=values.get("snmp_auth_protocol"),
        snmp_auth_secret=values.get("snmp_auth_secret"),
        snmp_priv_protocol=values.get("snmp_priv_protocol"),
        snmp_priv_secret=values.get("snmp_priv_secret"),
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )


def run_ping_probe(host: str) -> bool:
    """Run ping probe for host reachability validation."""
    success, _latency_ms = ping_service.run_ping(host, timeout_seconds=4)
    return success


def create_device(db: Session, values: dict[str, object]) -> CoreDeviceSubmitResult:
    """Create a core device with optional ping probe and monitoring fields."""
    status = DeviceStatus.offline
    warnings: list[str] = []
    device = NetworkDevice(
        name=values["name"],
        hostname=values.get("hostname"),
        mgmt_ip=values.get("mgmt_ip"),
        role=values["role"],
        status=status,
        pop_site_id=values.get("pop_site_id"),
        parent_device_id=values.get("parent_device_id"),
        vendor=values.get("vendor"),
        model=values.get("model"),
        serial_number=values.get("serial_number"),
        device_type=values.get("device_type"),
        ping_enabled=bool(values.get("ping_enabled")),
        snmp_enabled=bool(values.get("snmp_enabled")),
        send_notifications=bool(values.get("send_notifications")),
        notification_delay_minutes=cast(
            int, values.get("notification_delay_minutes") or 0
        ),
        snmp_port=values.get("snmp_port"),
        snmp_version=values.get("snmp_version"),
        snmp_community=values.get("snmp_community"),
        snmp_username=values.get("snmp_username"),
        snmp_auth_protocol=values.get("snmp_auth_protocol"),
        snmp_auth_secret=values.get("snmp_auth_secret"),
        snmp_priv_protocol=values.get("snmp_priv_protocol"),
        snmp_priv_secret=values.get("snmp_priv_secret"),
        notes=values.get("notes"),
        is_active=bool(values.get("is_active")),
    )

    host = values.get("host")
    if values.get("ping_enabled") and host:
        if not run_ping_probe(str(host)):
            warnings.append("Ping probe failed. Check the management IP/hostname.")
            device.last_ping_at = datetime.now(UTC)
            device.last_ping_ok = False
        else:
            device.last_ping_at = datetime.now(UTC)
            device.last_ping_ok = True

    db.add(device)
    try:
        db.commit()
        db.refresh(device)
    except IntegrityError as exc:
        db.rollback()
        return CoreDeviceSubmitResult(
            None,
            error=integrity_error_message(exc),
            snapshot=snapshot_for_form(values, status=status),
        )
    return CoreDeviceSubmitResult(
        device=device,
        warning=" ".join(warnings) if warnings else None,
    )


def update_device(
    db: Session, device: NetworkDevice, values: dict[str, object]
) -> CoreDeviceSubmitResult:
    """Update a core device with optional ping probe and monitoring fields."""
    try:
        core_device_archive.require_core_device_mutable(
            db,
            core_device_archive.RequireCoreDeviceMutableRequest(
                device_id=device.id,
                mutation=core_device_archive.CoreDeviceMutation.EDIT,
            ),
        )
    except core_device_archive.CoreDeviceArchiveError as exc:
        return CoreDeviceSubmitResult(device=None, error=exc.message)
    previous_mgmt_ip = (device.mgmt_ip or "").strip() or None
    warnings: list[str] = []

    if values.get("ping_enabled") and values.get("host"):
        if not run_ping_probe(str(values["host"])):
            warnings.append("Ping probe failed. Check the management IP/hostname.")
            device.last_ping_at = datetime.now(UTC)
            device.last_ping_ok = False
        else:
            device.last_ping_at = datetime.now(UTC)
            device.last_ping_ok = True

    device.name = cast(str, values["name"])
    device.hostname = cast(str | None, values.get("hostname"))
    device.mgmt_ip = cast(str | None, values.get("mgmt_ip"))
    device.role = cast(DeviceRole, values["role"])
    device.pop_site_id = cast(UUID | None, values.get("pop_site_id"))
    device.parent_device_id = cast(UUID | None, values.get("parent_device_id"))
    device.vendor = cast(str | None, values.get("vendor"))
    device.model = cast(str | None, values.get("model"))
    device.serial_number = cast(str | None, values.get("serial_number"))
    device.device_type = cast(DeviceType | None, values.get("device_type"))
    device.ping_enabled = bool(values.get("ping_enabled"))
    device.snmp_enabled = bool(values.get("snmp_enabled"))
    device.send_notifications = bool(values.get("send_notifications"))
    device.notification_delay_minutes = cast(
        int, values.get("notification_delay_minutes") or 0
    )
    device.snmp_port = cast(int | None, values.get("snmp_port"))
    device.snmp_version = cast(str | None, values.get("snmp_version"))
    device.snmp_community = cast(str | None, values.get("snmp_community"))
    device.snmp_username = cast(str | None, values.get("snmp_username"))
    device.snmp_auth_protocol = cast(str | None, values.get("snmp_auth_protocol"))
    device.snmp_auth_secret = cast(str | None, values.get("snmp_auth_secret"))
    device.snmp_priv_protocol = cast(str | None, values.get("snmp_priv_protocol"))
    device.snmp_priv_secret = cast(str | None, values.get("snmp_priv_secret"))
    device.notes = cast(str | None, values.get("notes"))
    # Admission goes through the owning transition so an operator deactivating
    # from this form also decays the stale reachability cache.
    set_network_device_active(
        db, device, bool(values.get("is_active")), reason="core_device_form"
    )
    _sync_linked_nas_device_ip_state(db, device, previous_mgmt_ip=previous_mgmt_ip)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        return CoreDeviceSubmitResult(
            None,
            error=integrity_error_message(exc),
            snapshot=snapshot_for_form(
                values, device_id=str(device.id), status=device.status
            ),
        )
    return CoreDeviceSubmitResult(
        device=device,
        warning=" ".join(warnings) if warnings else None,
    )


def _sync_linked_nas_device_ip_state(
    db: Session,
    device: NetworkDevice,
    *,
    previous_mgmt_ip: str | None,
) -> None:
    nas_device = _core_mapped_nas_device(db, device)
    if not nas_device:
        return

    normalized_ip = (device.mgmt_ip or "").strip() or None
    previous_nas_management_ip = (nas_device.management_ip or "").strip() or None
    previous_nas_ip = (nas_device.ip_address or "").strip() or None
    previous_nas_radius_ip = (nas_device.nas_ip or "").strip() or None

    nas_device.management_ip = normalized_ip

    # Keep the NAS IP copy aligned when it was mirroring the core device IP.
    mirrored_ip_values = {
        value for value in (previous_mgmt_ip, previous_nas_management_ip) if value
    }
    if previous_nas_ip in mirrored_ip_values or previous_nas_ip is None:
        nas_device.ip_address = normalized_ip
    if previous_nas_radius_ip in mirrored_ip_values or previous_nas_radius_ip is None:
        nas_device.nas_ip = normalized_ip

    if not normalized_ip:
        return

    old_radius_ips = {
        value
        for value in (
            previous_mgmt_ip,
            previous_nas_management_ip,
            previous_nas_ip,
            previous_nas_radius_ip,
        )
        if value
    }
    if not old_radius_ips:
        return

    radius_clients = list(
        db.scalars(
            select(RadiusClient).where(RadiusClient.nas_device_id == nas_device.id)
        ).all()
    )
    for client in radius_clients:
        if client.client_ip in old_radius_ips:
            client.client_ip = normalized_ip


def list_page_data(
    db: Session,
    role: str | None,
    status: str | None,
    device_type: str | None = None,
    pop_site_id: str | None = None,
    search: str | None = None,
) -> dict[str, object]:
    """Return data payload for core-device index."""
    stmt = select(NetworkDevice)
    if role:
        try:
            role_enum = DeviceRole(role)
            stmt = stmt.where(NetworkDevice.role == role_enum)
        except ValueError:
            pass
    device_type_filter = (device_type or "").strip().lower()
    if device_type_filter:
        try:
            type_enum = DeviceType(device_type_filter)
            stmt = stmt.where(NetworkDevice.device_type == type_enum)
        except ValueError:
            device_type_filter = ""

    status_filter = (status or "all").strip().lower()
    if status_filter == "active":
        stmt = stmt.where(NetworkDevice.is_active.is_(True))
    elif status_filter == "inactive":
        stmt = stmt.where(NetworkDevice.is_active.is_(False))

    pop_site_filter: str | None = None
    if pop_site_id and pop_site_id != "all":
        try:
            pop_site_uuid = UUID(pop_site_id)
            stmt = stmt.where(NetworkDevice.pop_site_id == pop_site_uuid)
            pop_site_filter = pop_site_id
        except ValueError:
            pop_site_filter = None

    search_filter = (search or "").strip()
    if search_filter:
        pattern = f"%{search_filter}%"
        stmt = stmt.where(
            NetworkDevice.name.ilike(pattern)
            | NetworkDevice.hostname.ilike(pattern)
            | NetworkDevice.mgmt_ip.ilike(pattern)
            | NetworkDevice.vendor.ilike(pattern)
            | NetworkDevice.model.ilike(pattern)
        )

    devices = list(db.scalars(stmt.order_by(NetworkDevice.name).limit(200)).all())
    annotate_operational_status(devices)
    device_ids = [device.id for device in devices]
    pop_sites = pop_sites_for_forms(db)
    child_impacts: dict[str, dict[str, int | bool]] = {}
    parent_ids = [device.id for device in devices]
    if parent_ids:
        children = list(
            db.scalars(
                select(NetworkDevice)
                .where(NetworkDevice.parent_device_id.in_(parent_ids))
                .where(NetworkDevice.is_active.is_(True))
            ).all()
        )
        annotate_operational_status(children)
        for child in children:
            if not child.parent_device_id:
                continue
            key = str(child.parent_device_id)
            bucket = child_impacts.setdefault(
                key,
                {
                    "total": 0,
                    "not_working": 0,
                    "impaired": 0,
                    "impacted": False,
                },
            )
            bucket["total"] = int(bucket["total"]) + 1
            if child.operational.status == NOT_WORKING:
                bucket["not_working"] = int(bucket["not_working"]) + 1
            if child.operational.impaired:
                bucket["impaired"] = int(bucket["impaired"]) + 1
            bucket["impacted"] = bool(
                int(bucket["not_working"]) > 0 or int(bucket["impaired"]) > 0
            )

    uptime_map: dict[str, str | None] = {}
    ping_history_map: dict[str, list[dict[str, object]]] = {}
    backup_map: dict[str, dict[str, object | None]] = {}
    if device_ids:
        latest_uptime_subq = (
            select(
                DeviceMetric.device_id,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .where(DeviceMetric.device_id.in_(device_ids))
            .where(DeviceMetric.metric_type == MetricType.uptime)
            .group_by(DeviceMetric.device_id)
            .subquery()
        )
        latest_uptime_metrics = db.scalars(
            select(DeviceMetric)
            .join(
                latest_uptime_subq,
                and_(
                    DeviceMetric.device_id == latest_uptime_subq.c.device_id,
                    DeviceMetric.recorded_at == latest_uptime_subq.c.latest,
                ),
            )
            .where(DeviceMetric.metric_type == MetricType.uptime)
        ).all()
        for metric in latest_uptime_metrics:
            uptime_map[str(metric.device_id)] = _format_uptime_short(metric.value)

        recent_ping_metrics = db.scalars(
            select(DeviceMetric)
            .where(DeviceMetric.device_id.in_(device_ids))
            .where(DeviceMetric.metric_type == MetricType.custom)
            .where(DeviceMetric.unit.in_(["ping_ms", "ping_timeout"]))
            .order_by(DeviceMetric.recorded_at.desc())
            .limit(2000)
        ).all()
        for metric in recent_ping_metrics:
            key = str(metric.device_id)
            bucket = ping_history_map.setdefault(key, [])
            if len(bucket) >= 5:
                continue
            ok = metric.unit == "ping_ms" and metric.value >= 0
            bucket.append(
                {
                    "ok": ok,
                    "label": f"{metric.value}ms" if ok else "Timeout",
                    "recorded_at": metric.recorded_at,
                }
            )

        nas_devices = db.scalars(
            select(NasDevice).where(NasDevice.network_device_id.in_(device_ids))
        ).all()
        nas_by_network_id = {
            str(n.network_device_id): n for n in nas_devices if n.network_device_id
        }
        nas_ids = [n.id for n in nas_devices]
        latest_backup_by_nas_id: dict[UUID, NasConfigBackup] = {}
        if nas_ids:
            latest_backup_subq = (
                select(
                    NasConfigBackup.nas_device_id,
                    func.max(NasConfigBackup.created_at).label("latest"),
                )
                .where(NasConfigBackup.nas_device_id.in_(nas_ids))
                .group_by(NasConfigBackup.nas_device_id)
                .subquery()
            )
            latest_backups = db.scalars(
                select(NasConfigBackup).join(
                    latest_backup_subq,
                    and_(
                        NasConfigBackup.nas_device_id
                        == latest_backup_subq.c.nas_device_id,
                        NasConfigBackup.created_at == latest_backup_subq.c.latest,
                    ),
                )
            ).all()
            latest_backup_by_nas_id = {
                backup.nas_device_id: backup for backup in latest_backups
            }

        for device in devices:
            device_key = str(device.id)
            nas_device = nas_by_network_id.get(device_key)
            latest_backup = (
                latest_backup_by_nas_id.get(nas_device.id) if nas_device else None
            )
            status = "none"
            if latest_backup:
                notes = (latest_backup.notes or "").lower()
                status = (
                    "failed" if ("fail" in notes or "error" in notes) else "success"
                )
            elif nas_device and nas_device.backup_enabled:
                status = "stale"
            backup_map[device_key] = {
                "status": status,
                "last_backup_at": latest_backup.created_at if latest_backup else None,
            }
    stats = {
        "total": len(devices),
        "core": sum(1 for d in devices if d.role.value == "core"),
        "distribution": sum(1 for d in devices if d.role.value == "distribution"),
        "access": sum(1 for d in devices if d.role.value == "access"),
        "aggregation": sum(1 for d in devices if d.role.value == "aggregation"),
        "edge": sum(1 for d in devices if d.role.value == "edge"),
    }
    return {
        "devices": devices,
        "stats": stats,
        "role_filter": role,
        "status_filter": status_filter,
        "pop_sites": pop_sites,
        "pop_site_filter": pop_site_filter or "all",
        "search_filter": search_filter,
        "device_type_filter": device_type_filter,
        "device_type_options": [item.value for item in DeviceType],
        "child_impacts": child_impacts,
        "uptime_map": uptime_map,
        "ping_history_map": ping_history_map,
        "backup_map": backup_map,
    }


def detail_page_data(
    db: Session,
    device_id: str,
    interface_id: str | None,
    *,
    format_duration: Callable[[float | int | None], str],
    format_bps: Callable[[float | int | None], str],
    message: str | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    """Return full payload for core-device detail page."""
    device = get_device(db, device_id)
    if not device:
        return None
    child_devices = list(
        db.scalars(
            select(NetworkDevice)
            .where(NetworkDevice.parent_device_id == device.id)
            .order_by(NetworkDevice.name)
        ).all()
    )
    annotate_operational_status([device, *child_devices])

    lineage: list[NetworkDevice] = []
    ancestor = device.parent_device
    visited_ids: set[UUID] = set()
    while ancestor and ancestor.id not in visited_ids:
        lineage.append(ancestor)
        visited_ids.add(ancestor.id)
        ancestor = ancestor.parent_device
    lineage.reverse()
    child_status_summary = {
        "total": len(child_devices),
        "not_working": sum(
            1 for c in child_devices if c.operational.status == NOT_WORKING
        ),
        "impaired": sum(1 for c in child_devices if c.operational.impaired),
    }
    child_status_summary["impacted"] = (
        child_status_summary["not_working"] + child_status_summary["impaired"]
    ) > 0
    descendants_by_parent: dict[UUID, list[NetworkDevice]] = {}
    descendant_devices: list[NetworkDevice] = []
    descendants_frontier = [device.id]
    descendants_visited: set[UUID] = set()
    while descendants_frontier:
        children = list(
            db.scalars(
                select(NetworkDevice)
                .where(NetworkDevice.parent_device_id.in_(descendants_frontier))
                .where(NetworkDevice.is_active.is_(True))
                .order_by(NetworkDevice.name)
            ).all()
        )
        descendants_frontier = []
        for child in children:
            if child.id in descendants_visited:
                continue
            descendants_visited.add(child.id)
            descendant_devices.append(child)
            if child.parent_device_id:
                descendants_by_parent.setdefault(child.parent_device_id, []).append(
                    child
                )
            descendants_frontier.append(child.id)

    annotate_operational_status(descendant_devices)

    def _tree(parent_id: UUID, depth: int = 0) -> list[dict[str, object]]:
        nodes: list[dict[str, object]] = []
        for child in descendants_by_parent.get(parent_id, []):
            nodes.append(
                {
                    "device": child,
                    "depth": depth,
                    "children": _tree(child.id, depth + 1),
                }
            )
        return nodes

    descendant_tree = _tree(device.id)

    interfaces = db.scalars(
        select(DeviceInterface)
        .where(DeviceInterface.device_id == device.id)
        .order_by(DeviceInterface.name)
    ).all()
    selected_interface = None
    if interface_id:
        selected_interface = db.scalars(
            select(DeviceInterface).where(
                DeviceInterface.id == interface_id,
                DeviceInterface.device_id == device.id,
            )
        ).first()

    alerts = db.scalars(
        select(Alert)
        .where(
            Alert.device_id == device.id,
            Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]),
        )
        .order_by(Alert.triggered_at.desc())
        .limit(10)
    ).all()

    metric_types = [MetricType.cpu, MetricType.memory, MetricType.uptime]
    if not selected_interface:
        metric_types.extend([MetricType.rx_bps, MetricType.tx_bps])

    latest_metrics_subq = (
        select(
            DeviceMetric.metric_type,
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .where(DeviceMetric.device_id == device.id)
        .where(DeviceMetric.metric_type.in_(metric_types))
        .group_by(DeviceMetric.metric_type)
        .subquery()
    )
    latest_metrics = db.scalars(
        select(DeviceMetric)
        .join(
            latest_metrics_subq,
            and_(
                DeviceMetric.metric_type == latest_metrics_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_metrics_subq.c.latest,
            ),
        )
        .where(DeviceMetric.device_id == device.id)
    ).all()
    metrics_by_type = {metric.metric_type: metric for metric in latest_metrics}

    cpu_metric = metrics_by_type.get(MetricType.cpu)
    mem_metric = metrics_by_type.get(MetricType.memory)
    uptime_metric = metrics_by_type.get(MetricType.uptime)
    if selected_interface:
        interface_metric_types = [MetricType.rx_bps, MetricType.tx_bps]
        interface_metrics_subq = (
            select(
                DeviceMetric.metric_type,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .where(DeviceMetric.device_id == device.id)
            .where(DeviceMetric.interface_id == selected_interface.id)
            .where(DeviceMetric.metric_type.in_(interface_metric_types))
            .group_by(DeviceMetric.metric_type)
            .subquery()
        )
        interface_metrics = db.scalars(
            select(DeviceMetric)
            .join(
                interface_metrics_subq,
                and_(
                    DeviceMetric.metric_type == interface_metrics_subq.c.metric_type,
                    DeviceMetric.recorded_at == interface_metrics_subq.c.latest,
                ),
            )
            .where(DeviceMetric.device_id == device.id)
            .where(DeviceMetric.interface_id == selected_interface.id)
        ).all()
        interface_metrics_by_type = {
            metric.metric_type: metric for metric in interface_metrics
        }
        rx_metric = interface_metrics_by_type.get(MetricType.rx_bps)
        tx_metric = interface_metrics_by_type.get(MetricType.tx_bps)
    else:
        rx_metric = metrics_by_type.get(MetricType.rx_bps)
        tx_metric = metrics_by_type.get(MetricType.tx_bps)

    ping_history_metrics = db.scalars(
        select(DeviceMetric)
        .where(DeviceMetric.device_id == device.id)
        .where(DeviceMetric.metric_type == MetricType.custom)
        .where(DeviceMetric.unit.in_(["ping_ms", "ping_timeout"]))
        .order_by(DeviceMetric.recorded_at.desc())
        .limit(10)
    ).all()
    ping_history = [
        {
            "ok": metric.unit == "ping_ms" and metric.value >= 0,
            "label": f"{metric.value}ms"
            if (metric.unit == "ping_ms" and metric.value >= 0)
            else "Timeout",
            "recorded_at": metric.recorded_at,
        }
        for metric in ping_history_metrics
    ]
    nas_device = _core_mapped_nas_device(db, device)
    lifecycle_state = (
        NetworkDeviceLifecycleState.ARCHIVED
        if device.archived_at is not None
        else (
            NetworkDeviceLifecycleState.ACTIVE
            if device.is_active
            else NetworkDeviceLifecycleState.INACTIVE
        )
    )
    status_presentation = network_device_list_status_presentation(
        NetworkDeviceListStatusContext(
            operational_status=DeviceOperationalState(device.operational.status),
            lifecycle_state=lifecycle_state,
        )
    )

    return {
        "device": device,
        "status_presentation": status_presentation,
        "nas_device": nas_device,
        "interfaces": interfaces,
        "selected_interface": selected_interface,
        "child_devices": child_devices,
        "child_status_summary": child_status_summary,
        "descendant_tree": descendant_tree,
        "device_lineage": lineage,
        "alerts": alerts,
        "device_health": {
            "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
            "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
            "uptime": format_duration(uptime_metric.value if uptime_metric else None),
            "rx": format_bps(rx_metric.value) if rx_metric else "--",
            "tx": format_bps(tx_metric.value) if tx_metric else "--",
            "last_seen": device.last_ping_at or device.last_snmp_at,
        },
        "ping_history": ping_history,
        "message": message,
        "error": error,
    }


def update_provisioning_access_for_device(
    db: Session,
    *,
    device_id: str,
    ssh_username: str,
    ssh_password: str | None,
    shared_secret: str | None,
) -> tuple[bool, str]:
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.PROVISIONING_ACCESS,
    )
    if not device:
        return False, guard_error or "Device not found."
    nas_device = _core_mapped_nas_device(db, device)
    if not nas_device:
        return False, "No linked NAS device for this core device."

    normalized_ssh_username = (ssh_username or "").strip() or None
    normalized_ssh_password = (ssh_password or "").strip()
    normalized_shared_secret = (shared_secret or "").strip()
    nas_service.NasDevices.update(
        db,
        str(nas_device.id),
        NasDeviceUpdate(
            ssh_username=normalized_ssh_username,
            ssh_password=normalized_ssh_password or nas_device.ssh_password,
            shared_secret=normalized_shared_secret or nas_device.shared_secret,
        ),
    )
    return True, "Provisioning access settings saved."


def toggle_interface_monitored(
    db: Session, *, device_id: str, interface_id: str, monitored: bool
) -> tuple[bool, str]:
    """Toggle whether an interface is included in bandwidth monitoring."""
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.INTERFACE_MONITORING,
    )
    if not device:
        return False, guard_error or "Device not found."
    item = db.get(DeviceInterface, interface_id)
    if not item or str(item.device_id) != str(device.id):
        return False, "Interface not found."
    item.monitored = monitored
    db.commit()
    return True, "Interface monitoring updated."


def _graph_view_url(graph: NetworkDeviceBandwidthGraph) -> str | None:
    if not graph.public_token:
        return None
    return f"/network/graphs/{graph.public_token}"


def bandwidth_graphs_page_data(
    db: Session,
    device_id: str,
    *,
    message: str | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    device = get_device(db, device_id)
    if not device:
        return None

    graphs = list(
        db.scalars(
            select(NetworkDeviceBandwidthGraph)
            .where(NetworkDeviceBandwidthGraph.device_id == device.id)
            .options(
                joinedload(NetworkDeviceBandwidthGraph.sources).joinedload(
                    NetworkDeviceBandwidthGraphSource.source_device
                ),
                joinedload(NetworkDeviceBandwidthGraph.sources).joinedload(
                    NetworkDeviceBandwidthGraphSource.snmp_oid
                ),
            )
            .order_by(NetworkDeviceBandwidthGraph.created_at.desc())
        )
        .unique()
        .all()
    )
    all_devices = list(
        db.scalars(
            select(NetworkDevice)
            .where(NetworkDevice.is_active.is_(True))
            .order_by(NetworkDevice.name)
        ).all()
    )
    all_oids = list(
        db.scalars(
            select(NetworkDeviceSnmpOid)
            .where(NetworkDeviceSnmpOid.is_enabled.is_(True))
            .order_by(NetworkDeviceSnmpOid.title)
        ).all()
    )
    return {
        "device": device,
        "graphs": graphs,
        "graph_view_urls": {str(graph.id): _graph_view_url(graph) for graph in graphs},
        "all_devices": all_devices,
        "all_oids": all_oids,
        "message": message,
        "error": error,
    }


def create_bandwidth_graph_for_device(
    db: Session,
    *,
    device_id: str,
    title: str,
    vertical_axis_title: str,
    height_px: int,
    is_public: bool,
) -> tuple[bool, str]:
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.GRAPH_CONFIGURATION,
    )
    if not device:
        return False, guard_error or "Device not found."
    if not title.strip():
        return False, "Title is required."

    graph = NetworkDeviceBandwidthGraph(
        device_id=device.id,
        title=title.strip(),
        vertical_axis_title=(vertical_axis_title.strip() or "Bandwidth"),
        height_px=max(80, min(480, int(height_px or 150))),
        is_public=is_public,
        public_token=uuid.uuid4().hex if is_public else None,
    )
    db.add(graph)
    db.commit()
    return True, "Graph created."


def add_bandwidth_graph_source(
    db: Session,
    *,
    device_id: str,
    graph_id: str,
    source_device_id: str,
    snmp_oid_id: str,
    factor: float,
    color_hex: str,
    draw_type: str,
    stack_enabled: bool,
    value_unit: str,
) -> tuple[bool, str]:
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.GRAPH_CONFIGURATION,
    )
    if not device:
        return False, guard_error or "Device not found."
    graph = db.get(NetworkDeviceBandwidthGraph, graph_id)
    if not graph or str(graph.device_id) != str(device.id):
        return False, "Graph not found."
    source_device = db.get(NetworkDevice, source_device_id)
    snmp_oid = db.get(NetworkDeviceSnmpOid, snmp_oid_id)
    if not source_device or not snmp_oid:
        return False, "Source device or OID not found."
    try:
        core_device_archive.require_core_device_mutable(
            db,
            core_device_archive.RequireCoreDeviceMutableRequest(
                device_id=source_device.id,
                mutation=core_device_archive.CoreDeviceMutation.GRAPH_CONFIGURATION,
            ),
        )
    except core_device_archive.CoreDeviceArchiveError as exc:
        return False, exc.message
    if str(snmp_oid.device_id) != str(source_device.id):
        return False, "Selected OID does not belong to selected source device."

    normalized_draw = draw_type.strip().upper()
    if normalized_draw not in {"LINE1", "AREA"}:
        return False, "Draw type must be LINE1 or AREA."
    normalized_unit = value_unit.strip()
    if normalized_unit not in {"Bps", "bps", "pps"}:
        return False, "Value unit must be Bps, bps, or pps."
    default_graph_color = DEFAULT_SEMANTIC_COLORS["positive"]
    normalized_color = color_hex.strip() or default_graph_color
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", normalized_color):
        return False, f"Color must be a hex value like {default_graph_color}."

    next_order = (
        db.scalar(
            select(
                func.coalesce(func.max(NetworkDeviceBandwidthGraphSource.sort_order), 0)
            ).where(NetworkDeviceBandwidthGraphSource.graph_id == graph.id)
        )
        or 0
    )
    row = NetworkDeviceBandwidthGraphSource(
        graph_id=graph.id,
        source_device_id=source_device.id,
        snmp_oid_id=snmp_oid.id,
        factor=float(factor or 1.0),
        color_hex=normalized_color,
        draw_type=normalized_draw,
        stack_enabled=stack_enabled,
        value_unit=normalized_unit,
        sort_order=int(next_order) + 1,
    )
    db.add(row)
    db.commit()
    return True, "Graph data source added."


def toggle_bandwidth_graph_public(
    db: Session,
    *,
    device_id: str,
    graph_id: str,
    is_public: bool,
) -> tuple[bool, str]:
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.GRAPH_CONFIGURATION,
    )
    if not device:
        return False, guard_error or "Device not found."
    graph = db.get(NetworkDeviceBandwidthGraph, graph_id)
    if not graph or str(graph.device_id) != str(device.id):
        return False, "Graph not found."
    graph.is_public = is_public
    if is_public and not graph.public_token:
        graph.public_token = uuid.uuid4().hex
    if not is_public:
        graph.public_token = None
    db.commit()
    return True, "Graph visibility updated."


def clone_bandwidth_graph_for_device(
    db: Session,
    *,
    device_id: str,
    graph_id: str,
) -> tuple[bool, str]:
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.GRAPH_CONFIGURATION,
    )
    if not device:
        return False, guard_error or "Device not found."
    graph = db.scalars(
        select(NetworkDeviceBandwidthGraph)
        .where(NetworkDeviceBandwidthGraph.id == graph_id)
        .options(joinedload(NetworkDeviceBandwidthGraph.sources))
    ).first()
    if not graph or str(graph.device_id) != str(device.id):
        return False, "Graph not found."

    cloned = NetworkDeviceBandwidthGraph(
        device_id=device.id,
        title=f"{graph.title} (Copy)",
        vertical_axis_title=graph.vertical_axis_title,
        height_px=graph.height_px,
        is_public=False,
        public_token=None,
    )
    db.add(cloned)
    db.flush()

    for source in graph.sources:
        db.add(
            NetworkDeviceBandwidthGraphSource(
                graph_id=cloned.id,
                source_device_id=source.source_device_id,
                snmp_oid_id=source.snmp_oid_id,
                factor=source.factor,
                color_hex=source.color_hex,
                draw_type=source.draw_type,
                stack_enabled=source.stack_enabled,
                value_unit=source.value_unit,
                sort_order=source.sort_order,
            )
        )
    db.commit()
    return True, "Graph cloned."


def bandwidth_dashboard_page_data(
    db: Session,
    *,
    search: str | None = None,
) -> dict[str, object]:
    stmt = (
        select(NetworkDeviceBandwidthGraph)
        .options(joinedload(NetworkDeviceBandwidthGraph.device))
        .order_by(NetworkDeviceBandwidthGraph.created_at.desc())
    )
    graphs = list(db.scalars(stmt).all())
    if search:
        needle = search.strip().lower()
        graphs = [
            item
            for item in graphs
            if needle in item.title.lower()
            or (item.device and needle in (item.device.name or "").lower())
        ]
    return {
        "graphs": graphs,
        "search": search or "",
    }


def get_public_bandwidth_graph(
    db: Session,
    *,
    token: str,
) -> NetworkDeviceBandwidthGraph | None:
    if not token:
        return None
    return db.scalars(
        select(NetworkDeviceBandwidthGraph)
        .where(NetworkDeviceBandwidthGraph.public_token == token)
        .where(NetworkDeviceBandwidthGraph.is_public.is_(True))
        .options(
            joinedload(NetworkDeviceBandwidthGraph.device),
            joinedload(NetworkDeviceBandwidthGraph.sources).joinedload(
                NetworkDeviceBandwidthGraphSource.snmp_oid
            ),
        )
    ).first()


def _core_mapped_nas_device(db: Session, device: NetworkDevice) -> NasDevice | None:
    return db.scalars(
        select(NasDevice).where(NasDevice.network_device_id == device.id)
    ).first()


def _tag_get(tags: list[str] | None, prefix: str) -> str | None:
    for tag in tags or []:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _tag_set(tags: list[str] | None, prefix: str, value: str | None) -> list[str]:
    base = [tag for tag in (tags or []) if not tag.startswith(prefix)]
    if value is not None and str(value).strip():
        base.append(f"{prefix}{value}")
    return base


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def backup_page_data(
    db: Session,
    device_id: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    device = get_device(db, device_id)
    if not device:
        return None
    nas_device = _core_mapped_nas_device(db, device)
    if not nas_device:
        return {
            "device": device,
            "nas_device": None,
            "backups": [],
            "date_from": date_from or "",
            "date_to": date_to or "",
            "backup_config": {},
            "message": message,
            "error": error or "No linked NAS device found for this core device.",
        }

    backups = list(
        db.scalars(
            select(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == nas_device.id)
            .order_by(NasConfigBackup.created_at.desc())
        ).all()
    )

    from_d: date | None = None
    to_d: date | None = None
    try:
        if date_from:
            from_d = date.fromisoformat(date_from)
    except ValueError:
        error = "Invalid from date format."
    try:
        if date_to:
            to_d = date.fromisoformat(date_to)
    except ValueError:
        error = "Invalid to date format."

    if from_d:
        backups = [
            item
            for item in backups
            if _ensure_utc(item.created_at)
            and _ensure_utc(item.created_at)
            >= datetime.combine(from_d, time.min).replace(tzinfo=UTC)
        ]
    if to_d:
        backups = [
            item
            for item in backups
            if _ensure_utc(item.created_at)
            and _ensure_utc(item.created_at)
            <= datetime.combine(to_d, time.max).replace(tzinfo=UTC)
        ]

    tags = nas_device.tags or []
    backup_config = {
        "enabled": bool(nas_device.backup_enabled),
        "ssh_username": nas_device.ssh_username or "",
        "ssh_port": nas_device.management_port or 120,
        "backup_type": _tag_get(tags, "backup_type:") or "commands",
        "backup_commands": _tag_get(tags, "backup_commands:") or "export",
        "hours_csv": _tag_get(tags, "backup_hours:") or "",
        "schedule": nas_device.backup_schedule or "",
    }
    return {
        "device": device,
        "nas_device": nas_device,
        "backups": backups,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "backup_config": backup_config,
        "message": message,
        "error": error,
    }


def update_backup_settings_for_device(
    db: Session,
    *,
    device_id: str,
    enabled: bool,
    ssh_username: str,
    ssh_password: str | None,
    ssh_port: int,
    backup_type: str,
    backup_commands: str | None,
    hours_csv: str | None,
) -> tuple[bool, str]:
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.BACKUP_SETTINGS,
    )
    if not device:
        return False, guard_error or "Device not found."
    nas_device = _core_mapped_nas_device(db, device)
    if not nas_device:
        return False, "No linked NAS device for this core device."

    normalized_type = (backup_type or "").strip().lower()
    if normalized_type not in {"commands", "scp", "tftp"}:
        return False, "Backup type must be Commands, SCP, or TFTP."

    hours_values: list[str] = []
    if hours_csv and hours_csv.strip():
        for part in hours_csv.split(","):
            item = part.strip()
            if not item:
                continue
            if not item.isdigit():
                return False, "Backup hours must be comma-separated numbers (0-23)."
            hour = int(item)
            if hour < 0 or hour > 23:
                return False, "Backup hours must be between 0 and 23."
            hours_values.append(str(hour))
    hours_values = sorted(set(hours_values), key=int)
    schedule = f"0 {','.join(hours_values)} * * *" if hours_values else None

    tags = nas_device.tags or []
    tags = _tag_set(tags, "backup_type:", normalized_type)
    tags = _tag_set(tags, "backup_commands:", (backup_commands or "").strip())
    tags = _tag_set(tags, "backup_hours:", ",".join(hours_values))

    nas_service.NasDevices.update(
        db,
        str(nas_device.id),
        NasDeviceUpdate(
            backup_enabled=enabled,
            backup_method=ConfigBackupMethod.ssh,
            backup_schedule=schedule,
            ssh_username=(ssh_username or "").strip() or None,
            ssh_password=(ssh_password or "").strip() or None,
            management_port=max(1, int(ssh_port or 120)),
            tags=tags,
        ),
    )
    return True, "Backup configuration saved."


def trigger_backup_for_core_device(
    db: Session, *, device_id: str, triggered_by: str = "web"
) -> tuple[bool, str]:
    device, guard_error = _mutable_device(
        db,
        device_id=device_id,
        mutation=core_device_archive.CoreDeviceMutation.BACKUP_TRIGGER,
    )
    if not device:
        return False, guard_error or "Device not found."
    nas_device = _core_mapped_nas_device(db, device)
    if not nas_device:
        return False, "No linked NAS device for this core device."
    result = nas_service.trigger_backup_for_device(
        db, device_id=str(nas_device.id), triggered_by=triggered_by
    )
    if result["ok"]:
        return True, "Backup triggered successfully."
    return False, str(result["error"] or "Backup trigger failed.")


def backup_detail_page_data(
    db: Session,
    *,
    device_id: str,
    backup_id: str,
) -> dict[str, object] | None:
    device = get_device(db, device_id)
    if not device:
        return None
    nas_device = _core_mapped_nas_device(db, device)
    if not nas_device:
        return None
    backup = db.get(NasConfigBackup, backup_id)
    if not backup or str(backup.nas_device_id) != str(nas_device.id):
        return None
    return {
        "device": device,
        "nas_device": nas_device,
        "backup": backup,
    }


def backup_compare_page_data(
    db: Session,
    *,
    device_id: str,
    backup_id_1: str,
    backup_id_2: str,
) -> dict[str, object] | None:
    device = get_device(db, device_id)
    if not device:
        return None
    nas_device = _core_mapped_nas_device(db, device)
    if not nas_device:
        return None

    backup1 = db.get(NasConfigBackup, backup_id_1)
    backup2 = db.get(NasConfigBackup, backup_id_2)
    if (
        not backup1
        or not backup2
        or str(backup1.nas_device_id) != str(nas_device.id)
        or str(backup2.nas_device_id) != str(nas_device.id)
    ):
        return None

    lines_1 = (backup1.config_content or "").splitlines()
    lines_2 = (backup2.config_content or "").splitlines()
    diff_lines = list(
        difflib.unified_diff(
            lines_1,
            lines_2,
            fromfile=f"{backup1.created_at}",
            tofile=f"{backup2.created_at}",
            lineterm="",
        )
    )
    added_lines = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed_lines = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )

    return {
        "device": device,
        "nas_device": nas_device,
        "backup1": backup1,
        "backup2": backup2,
        "diff": {
            "unified_diff": "\n".join(diff_lines),
            "added_lines": added_lines,
            "removed_lines": removed_lines,
        },
    }


def resolve_device_redirect(db: Session, device_id: str) -> str | None:
    """Find a device across various tables and return its detail URL."""
    device = db.scalars(
        select(NetworkDevice).where(NetworkDevice.id == device_id)
    ).first()
    if device:
        return f"/admin/network/core-devices/{device_id}"

    try:
        olt = network_service.olt_devices.get(db=db, device_id=device_id)
        if olt:
            return f"/admin/network/olts/{device_id}"
    except HTTPException:
        pass

    try:
        ont = network_service.ont_units.get(db=db, unit_id=device_id)
        if ont:
            return f"/admin/network/onts/{device_id}"
    except HTTPException:
        pass

    try:
        cpe = network_service.cpe_devices.get(db=db, device_id=device_id)
        if cpe:
            return f"/admin/network/cpes/{device_id}"
    except HTTPException:
        pass

    return None
