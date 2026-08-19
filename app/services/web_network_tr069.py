"""Service helpers for admin TR-069 web routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from typing import Any
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from starlette.requests import Request

from app.config import settings
from app.models.network import CPEDevice, OntAssignment, OntUnit
from app.models.tr069 import Tr069CpeDevice, Tr069JobStatus
from app.schemas.network import OntUnitCreate
from app.schemas.tr069 import (
    Tr069AcsServerCreate,
    Tr069AcsServerUpdate,
    Tr069CpeDeviceUpdate,
)
from app.services import network as network_service
from app.services import tr069 as tr069_service
from app.services.common import coerce_uuid
from app.services.genieacs_client import (
    GenieACSError,
    create_genieacs_client,
    normalize_tr069_serial,
)
from app.services.network import cpe as cpe_service
from app.services.network._common import decode_huawei_hex_serial
from app.services.network.tr069_inventory_links import onts_by_normalized_serial
from app.services.network.tr069_job_commands import (
    Tr069AdmissionOutcome,
    Tr069CommandKind,
    Tr069CommandRequest,
    Tr069Download,
    Tr069ParameterValue,
    request_tr069_command,
)
from app.services.owner_commands import CommandContext
from app.services.tr069_web_audit import (
    actor_id_from_request,
    log_tr069_audit_event,
)

logger = logging.getLogger(__name__)


def _normalized_serial_expr(column):
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


@dataclass
class JobAction:
    label: str
    command: str
    payload: dict | None = None


@dataclass(frozen=True)
class Tr069BulkAdmissionOutcome:
    accepted: tuple[Tr069AdmissionOutcome, ...]
    rejected: tuple[tuple[str, str], ...]


_JOB_ACTIONS: dict[str, JobAction] = {
    "refresh": JobAction(
        label="Refresh Parameters",
        command="refreshObject",
        payload={"objectName": "InternetGatewayDevice."},
    ),
    "reboot": JobAction(label="Reboot Device", command="reboot"),
    "factory_reset": JobAction(label="Factory Reset", command="factoryReset"),
}


def _tr069_command_context(
    *,
    request: Request | None,
    device_id: str,
    action: str,
) -> CommandContext:
    actor_id = actor_id_from_request(request)
    return CommandContext.system(
        actor=f"admin:{actor_id}" if actor_id else "admin:unknown",
        scope=f"network:tr069:{device_id}",
        reason=f"TR-069 admin action: {action}",
        command_id=uuid4(),
    )


def _admit_tr069_command(
    db: Session,
    *,
    request: Request | None,
    device_id: str,
    name: str,
    kind: Tr069CommandKind,
    object_name: str | None = None,
    parameter_values: tuple[Tr069ParameterValue, ...] = (),
    download: Tr069Download | None = None,
) -> Tr069AdmissionOutcome:
    parsed_device_id = coerce_uuid(device_id)
    return request_tr069_command(
        db,
        Tr069CommandRequest(
            context=_tr069_command_context(
                request=request,
                device_id=str(parsed_device_id),
                action=kind.value,
            ),
            device_id=parsed_device_id,
            name=name,
            kind=kind,
            object_name=object_name,
            parameter_values=parameter_values,
            download=download,
        ),
    )


# Config push actions use setParameterValues - need parameter values at runtime
@dataclass
class ConfigAction:
    """Defines a config push action with its TR-069 parameter mappings."""

    label: str
    description: str
    parameters: list[str]  # List of parameter paths that can be set


_CONFIG_ACTIONS: dict[str, ConfigAction] = {
    "wifi_ssid": ConfigAction(
        label="Set WiFi SSID",
        description="Change the WiFi network name (SSID)",
        parameters=[
            "Device.WiFi.SSID.1.SSID",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
        ],
    ),
    "wifi_password": ConfigAction(
        label="Set WiFi Password",
        description="Change the WiFi password (WPA key)",
        parameters=[
            "Device.WiFi.AccessPoint.1.Security.KeyPassphrase",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase",
        ],
    ),
    "wifi_enable": ConfigAction(
        label="Enable WiFi",
        description="Turn on the WiFi radio",
        parameters=[
            "Device.WiFi.Radio.1.Enable",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
        ],
    ),
    "wifi_disable": ConfigAction(
        label="Disable WiFi",
        description="Turn off the WiFi radio",
        parameters=[
            "Device.WiFi.Radio.1.Enable",
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
        ],
    ),
    "pppoe_username": ConfigAction(
        label="Set PPPoE Username",
        description="Change the PPPoE username for WAN connection",
        parameters=[
            "Device.PPP.Interface.1.Username",
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
        ],
    ),
    "pppoe_password": ConfigAction(
        label="Set PPPoE Password",
        description="Change the PPPoE password for WAN connection",
        parameters=[
            "Device.PPP.Interface.1.Password",
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        ],
    ),
    "lan_ip": ConfigAction(
        label="Set LAN IP Address",
        description="Change the LAN gateway IP address",
        parameters=[
            "Device.IP.Interface.2.IPv4Address.1.IPAddress",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
        ],
    ),
    "lan_subnet": ConfigAction(
        label="Set LAN Subnet Mask",
        description="Change the LAN subnet mask",
        parameters=[
            "Device.IP.Interface.2.IPv4Address.1.SubnetMask",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
        ],
    ),
    "dhcp_enable": ConfigAction(
        label="Enable DHCP Server",
        description="Turn on the DHCP server for LAN clients",
        parameters=[
            "Device.DHCPv4.Server.Enable",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
        ],
    ),
    "dhcp_disable": ConfigAction(
        label="Disable DHCP Server",
        description="Turn off the DHCP server",
        parameters=[
            "Device.DHCPv4.Server.Enable",
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
        ],
    ),
}


def parse_acs_form(form) -> dict[str, object]:
    # Parse periodic inform interval with validation
    default_interval = settings.tr069_periodic_inform_interval
    interval_str = str(
        form.get("periodic_inform_interval") or str(default_interval)
    ).strip()
    try:
        periodic_inform_interval = max(60, min(86400, int(interval_str)))
    except ValueError:
        periodic_inform_interval = default_interval

    return {
        "name": str(form.get("name") or "").strip(),
        "cwmp_url": str(form.get("cwmp_url") or "").strip(),
        "cwmp_username": str(form.get("cwmp_username") or "").strip(),
        "cwmp_password": str(form.get("cwmp_password") or "").strip(),
        "connection_request_username": str(
            form.get("connection_request_username") or ""
        ).strip(),
        "connection_request_password": str(
            form.get("connection_request_password") or ""
        ).strip(),
        "base_url": str(form.get("base_url") or "").strip(),
        "periodic_inform_interval": periodic_inform_interval,
        "is_active": str(form.get("is_active") or "true").strip().lower()
        in ("1", "true", "on", "yes"),
        "notes": str(form.get("notes") or "").strip() or None,
    }


def validate_acs_values(values: dict[str, object]) -> str | None:
    if not values.get("name"):
        return "ACS server name is required."
    if not values.get("cwmp_url"):
        return "CWMP URL is required."
    if not values.get("cwmp_username"):
        return "CWMP username is required."
    if not values.get("cwmp_password"):
        return "CWMP password is required."
    if not values.get("connection_request_username"):
        return "Connection request username is required."
    if not values.get("connection_request_password"):
        return "Connection request password is required."
    if not values.get("base_url"):
        return "ACS base URL is required."
    return None


def validate_acs_connection(values: dict[str, object]) -> str | None:
    base_url = str(values.get("base_url") or "").strip()
    if not base_url:
        return "ACS base URL is required."

    try:
        # Lightweight reachability/auth check against the configured ACS NBI.
        create_genieacs_client(base_url, timeout=5.0).count_devices()
    except GenieACSError as exc:
        return f"Failed to connect to GenieACS ({base_url}): {exc}"
    return None


def acs_form_snapshot(
    values: dict[str, object], *, acs_id: str | None = None
) -> dict[str, object]:
    data = dict(values)
    if acs_id:
        data["id"] = acs_id
    return data


def acs_form_snapshot_from_model(server) -> dict[str, object]:
    return {
        "id": str(server.id),
        "name": server.name,
        "cwmp_url": server.cwmp_url,
        "cwmp_username": server.cwmp_username,
        "cwmp_password": "",  # nosec
        "connection_request_username": server.connection_request_username,
        "connection_request_password": "",  # nosec
        "base_url": server.base_url,
        "periodic_inform_interval": getattr(
            server, "periodic_inform_interval", settings.tr069_periodic_inform_interval
        ),
        "is_active": bool(server.is_active),
        "notes": server.notes or "",
    }


def create_acs_server(
    db: Session, values: dict[str, object], *, request: Request | None = None
):
    connection_error = validate_acs_connection(values)
    if connection_error:
        raise ValueError(connection_error)
    payload = Tr069AcsServerCreate.model_validate(values)
    server = tr069_service.acs_servers.create(db=db, payload=payload)
    log_tr069_audit_event(
        db,
        request=request,
        action="create",
        entity_type="tr069_acs_server",
        entity_id=server.id,
        metadata={
            "name": server.name,
            "base_url": server.base_url,
            "cwmp_url": server.cwmp_url,
        },
    )
    return server


def update_acs_server(
    db: Session,
    *,
    acs_id: str,
    values: dict[str, object],
    request: Request | None = None,
):
    existing = get_acs_server(db, acs_id=acs_id)
    if not str(values.get("cwmp_password") or "").strip():
        values["cwmp_password"] = existing.cwmp_password
    if not str(values.get("connection_request_password") or "").strip():
        values["connection_request_password"] = existing.connection_request_password
    connection_error = validate_acs_connection(values)
    if connection_error:
        raise ValueError(connection_error)
    payload = Tr069AcsServerUpdate.model_validate(values)
    server = tr069_service.acs_servers.update(db=db, server_id=acs_id, payload=payload)
    log_tr069_audit_event(
        db,
        request=request,
        action="update",
        entity_type="tr069_acs_server",
        entity_id=server.id,
        metadata={
            "name": server.name,
            "base_url": server.base_url,
            "cwmp_url": server.cwmp_url,
        },
    )
    return server


def get_acs_server(db: Session, *, acs_id: str):
    return tr069_service.acs_servers.get(db=db, server_id=acs_id)


def _active_acs_servers(db: Session):
    return tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )


def _selected_acs_server(db: Session, acs_server_id: str | None):
    servers = _active_acs_servers(db)
    selected_server_id = str(acs_server_id or "").strip()
    if not selected_server_id and servers:
        selected_server_id = str(servers[0].id)
    server = (
        tr069_service.acs_servers.get(db=db, server_id=selected_server_id)
        if selected_server_id
        else None
    )
    return servers, selected_server_id, server


def _task_parameters(task: dict[str, Any]) -> list[str]:
    if isinstance(task.get("parameterNames"), list):
        return [str(item) for item in task.get("parameterNames") or []]
    if isinstance(task.get("parameterValues"), list):
        names: list[str] = []
        for item in task.get("parameterValues") or []:
            if isinstance(item, (list, tuple)) and item:
                names.append(str(item[0]))
        return names
    return []


def _task_timestamp(task: dict[str, Any]) -> str:
    for key in ("timestamp", "created", "createdAt", "created_at"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    return ""


def _parse_task_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _task_age_label(age_seconds: int | None) -> str:
    if age_seconds is None:
        return ""
    if age_seconds < 60:
        return f"{age_seconds}s"
    minutes = age_seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _is_broad_refresh_task(task: dict[str, Any]) -> bool:
    if task.get("name") != "refreshObject":
        return False
    object_name = str(task.get("objectName") or "").strip().rstrip(".")
    return object_name in {"Device", "InternetGatewayDevice"}


def acs_task_console_data(
    db: Session,
    *,
    acs_server_id: str | None = None,
) -> dict[str, object]:
    """Return pending GenieACS task data for the operator console."""
    servers, selected_server_id, server = _selected_acs_server(db, acs_server_id)
    tasks: list[dict[str, Any]] = []
    error: str | None = None

    if server:
        try:
            client = create_genieacs_client(str(server.base_url))
            tasks = client.list_tasks()
        except Exception as exc:
            error = str(exc)

    device_ids = {
        str(task.get("device") or "").strip()
        for task in tasks
        if str(task.get("device") or "").strip()
    }
    local_by_genieacs_id: dict[str, Tr069CpeDevice] = {}
    if selected_server_id and device_ids:
        local_devices = (
            db.query(Tr069CpeDevice)
            .filter(Tr069CpeDevice.acs_server_id == coerce_uuid(selected_server_id))
            .filter(Tr069CpeDevice.genieacs_device_id.in_(device_ids))
            .all()
        )
        local_by_genieacs_id = {
            str(device.genieacs_device_id): device for device in local_devices
        }

    now = datetime.now(UTC)
    stale_after = timedelta(minutes=15)
    device_task_counts: dict[str, int] = {}
    for task in tasks:
        device_id = str(task.get("device") or "").strip()
        if device_id:
            device_task_counts[device_id] = device_task_counts.get(device_id, 0) + 1

    rows: list[dict[str, object]] = []
    for task in tasks:
        task_id = str(task.get("_id") or "").strip()
        device_id = str(task.get("device") or "").strip()
        local_device = local_by_genieacs_id.get(device_id)
        parameters = _task_parameters(task)
        timestamp = _task_timestamp(task)
        parsed_timestamp = _parse_task_timestamp(timestamp)
        age_seconds = (
            max(0, int((now - parsed_timestamp).total_seconds()))
            if parsed_timestamp
            else None
        )
        is_stale = bool(
            age_seconds is not None and age_seconds >= stale_after.total_seconds()
        )
        is_broad_refresh = _is_broad_refresh_task(task)
        device_task_count = device_task_counts.get(device_id, 0) if device_id else 0
        rows.append(
            {
                "id": task_id,
                "device_id": device_id,
                "device_label": (
                    _display_serial_number(local_device.serial_number)
                    if local_device and local_device.serial_number
                    else device_id
                ),
                "local_device_id": str(local_device.id) if local_device else "",
                "name": str(task.get("name") or "task"),
                "timestamp": timestamp,
                "age_label": _task_age_label(age_seconds),
                "age_seconds": age_seconds,
                "is_stale": is_stale,
                "is_broad_refresh": is_broad_refresh,
                "device_task_count": device_task_count,
                "is_inform_blocking_suspect": bool(
                    is_stale or is_broad_refresh or device_task_count > 1
                ),
                "parameters": parameters,
                "parameter_count": len(parameters),
                "raw": task,
            }
        )

    return {
        "servers": servers,
        "selected_server_id": selected_server_id,
        "selected_server": server,
        "tasks": rows,
        "task_count": len(rows),
        "stale_task_count": sum(1 for row in rows if row["is_stale"]),
        "suspect_task_count": sum(
            1 for row in rows if row["is_inform_blocking_suspect"]
        ),
        "devices_with_backlog": sum(
            1 for count in device_task_counts.values() if count > 1
        ),
        "error": error,
    }


def delete_acs_task(
    db: Session,
    *,
    acs_server_id: str,
    task_id: str,
    request: Request | None = None,
) -> None:
    _, _, server = _selected_acs_server(db, acs_server_id)
    if not server:
        raise ValueError("ACS server not found")
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        raise ValueError("Task ID is required")
    client = create_genieacs_client(str(server.base_url))
    client.delete_task(clean_task_id)
    log_tr069_audit_event(
        db,
        request=request,
        action="delete_acs_task",
        entity_type="genieacs_task",
        entity_id=clean_task_id,
        metadata={"acs_server_id": str(server.id), "server_name": server.name},
    )


def clear_acs_tasks(
    db: Session,
    *,
    acs_server_id: str,
    request: Request | None = None,
) -> dict[str, object]:
    _, _, server = _selected_acs_server(db, acs_server_id)
    if not server:
        raise ValueError("ACS server not found")
    client = create_genieacs_client(str(server.base_url))
    tasks = client.list_tasks()
    deleted = 0
    errors: list[dict[str, str]] = []
    for task in tasks:
        task_id = str(task.get("_id") or "").strip()
        if not task_id:
            continue
        try:
            client.delete_task(task_id)
            deleted += 1
        except Exception as exc:
            errors.append({"task_id": task_id, "error": str(exc)})

    log_tr069_audit_event(
        db,
        request=request,
        action="clear_acs_tasks",
        entity_type="tr069_acs_server",
        entity_id=str(server.id),
        metadata={
            "server_name": server.name,
            "task_count": len(tasks),
            "deleted": deleted,
            "errors": errors[:10],
        },
    )
    return {"deleted": deleted, "errors": errors, "attempted": len(tasks)}


def _device_registered_in_acs(device: Tr069CpeDevice) -> bool:
    return bool(str(getattr(device, "genieacs_device_id", "") or "").strip())


def queue_device_job(
    db: Session,
    *,
    tr069_device_id: str,
    action: str,
    request: Request | None = None,
) -> Tr069AdmissionOutcome:
    selected = _JOB_ACTIONS.get(action)
    if selected is None:
        raise ValueError("Unsupported TR-069 action.")
    kind = Tr069CommandKind(selected.command)
    object_name = (
        str((selected.payload or {}).get("objectName") or "")
        if kind is Tr069CommandKind.refresh_object
        else None
    )
    return _admit_tr069_command(
        db,
        request=request,
        device_id=tr069_device_id,
        name=selected.label,
        kind=kind,
        object_name=object_name,
    )


def link_tr069_device_to_cpe(
    db: Session,
    *,
    tr069_device_id: str,
    cpe_device_id: str | None,
):
    payload = Tr069CpeDeviceUpdate(
        cpe_device_id=coerce_uuid(cpe_device_id) if cpe_device_id else None,
    )
    return tr069_service.cpe_devices.update(
        db=db,
        device_id=tr069_device_id,
        payload=payload,
    )


def _display_serial_number(value: str | None) -> str | None:
    serial = str(value or "").strip()
    if not serial:
        return None
    return decode_huawei_hex_serial(serial) or serial


def _serial_search_values(value: str | None) -> list[str]:
    serial = str(value or "").strip()
    if not serial:
        return []
    values = [serial]
    display_serial = _display_serial_number(serial)
    if display_serial and display_serial not in values:
        values.append(display_serial)
    normalized = normalize_tr069_serial(serial)
    if normalized and normalized not in values:
        values.append(normalized)
    normalized_display = normalize_tr069_serial(display_serial)
    if normalized_display and normalized_display not in values:
        values.append(normalized_display)
    return values


def tr069_dashboard_data(
    db: Session,
    *,
    acs_server_id: str | None = None,
    search: str | None = None,
    only_unlinked: bool = False,
) -> dict[str, object]:
    inventory_subscriber_id = cpe_service.get_inventory_subscriber_id(db)

    def _cpe_primary_label(cpe: CPEDevice) -> str:
        if (
            cpe.subscriber
            and getattr(cpe.subscriber, "full_name", None)
            and getattr(cpe, "subscriber_id", None) != inventory_subscriber_id
        ):
            return str(cpe.subscriber.full_name)
        if cpe.serial_number:
            return str(cpe.serial_number)
        if cpe.model:
            return str(cpe.model)
        if cpe.mac_address:
            return str(cpe.mac_address)
        return f"CPE {str(cpe.id)[:8]}"

    def _cpe_search_label(cpe: CPEDevice) -> str:
        parts = [_cpe_primary_label(cpe)]
        if cpe.serial_number:
            parts.append(f"SN:{cpe.serial_number}")
        if cpe.model:
            parts.append(f"Model:{cpe.model}")
        if cpe.mac_address:
            parts.append(f"MAC:{cpe.mac_address}")
        parts.append(f"[{str(cpe.id)[:8]}]")
        return " | ".join(parts)

    servers = tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    selected_server_id = str(acs_server_id or "").strip() or None
    if not selected_server_id and servers:
        selected_server_id = str(servers[0].id)

    devices = (
        tr069_service.cpe_devices.list(
            db=db,
            acs_server_id=selected_server_id,
            is_active=None,
            order_by="serial_number",
            order_dir="asc",
            limit=5000,
            offset=0,
        )
        if selected_server_id
        else []
    )

    search_q = str(search or "").strip().lower()
    if search_q:
        devices = [
            item
            for item in devices
            if search_q
            in " ".join(
                _serial_search_values(item.serial_number)
                + [
                    str(item.oui or ""),
                    str(item.product_class or ""),
                    str(item.connection_request_url or ""),
                ]
            ).lower()
        ]

    if only_unlinked:
        devices = [item for item in devices if not item.cpe_device_id]

    linked_cpe_ids = [item.cpe_device_id for item in devices if item.cpe_device_id]
    linked_cpes = (
        db.query(CPEDevice)
        .options(joinedload(CPEDevice.subscriber))
        .filter(CPEDevice.id.in_(linked_cpe_ids))
        .all()
        if linked_cpe_ids
        else []
    )
    cpe_by_id = {str(cpe.id): cpe for cpe in linked_cpes}
    normalized_serials = {
        normalize_tr069_serial(
            _display_serial_number(item.serial_number) or item.serial_number or ""
        )
        for item in devices
        if item.serial_number
    }
    ont_by_normalized_serial = onts_by_normalized_serial(db, normalized_serials)

    for device in devices:
        device.is_registered_in_acs = _device_registered_in_acs(device)
        if device.is_registered_in_acs:
            device.registration_state_label = "Registered"
        elif device.ont_unit_id:
            device.registration_state_label = "Expected"
        else:
            device.registration_state_label = "Unmatched"
        device.linked_cpe = (
            cpe_by_id.get(str(device.cpe_device_id)) if device.cpe_device_id else None
        )
        device.linked_cpe_display_label = (
            _cpe_primary_label(device.linked_cpe) if device.linked_cpe else None
        )
        device.display_serial_number = _display_serial_number(device.serial_number)
        normalized_serial = normalize_tr069_serial(
            device.display_serial_number or device.serial_number or ""
        )
        device.linked_ont = ont_by_normalized_serial.get(normalized_serial)
        active_assignment = next(
            (
                assignment
                for assignment in getattr(device.linked_ont, "assignments", [])
                if getattr(assignment, "active", False)
            ),
            None,
        )
        device.linked_pon_port_name = (
            getattr(getattr(active_assignment, "pon_port", None), "name", None) or None
        )
        device.linked_olt_name = (
            getattr(getattr(device.linked_ont, "olt_device", None), "name", None)
            or None
        )

    unconfigured_devices = [item for item in devices if not item.cpe_device_id]
    configured_devices = [item for item in devices if item.cpe_device_id]

    jobs = tr069_service.jobs.list(
        db=db,
        device_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )

    managed_cpes = (
        db.query(CPEDevice)
        .options(joinedload(CPEDevice.subscriber))
        .order_by(CPEDevice.created_at.desc())
        .limit(1000)
        .all()
    )
    if inventory_subscriber_id is not None:
        managed_cpes = [
            cpe
            for cpe in managed_cpes
            if getattr(cpe, "subscriber_id", None) != inventory_subscriber_id
        ]
    cpe_typeahead_map = {_cpe_search_label(cpe): str(cpe.id) for cpe in managed_cpes}
    cpe_display_by_id = {str(cpe.id): _cpe_primary_label(cpe) for cpe in managed_cpes}
    cpe_search_by_id = {str(cpe.id): _cpe_search_label(cpe) for cpe in managed_cpes}

    now = datetime.now(UTC)
    seen_window = now - timedelta(hours=24)
    registered_devices = [
        item for item in devices if getattr(item, "is_registered_in_acs", False)
    ]
    expected_devices = [
        item
        for item in devices
        if not getattr(item, "is_registered_in_acs", False) and item.ont_unit_id
    ]
    unmatched_devices = [
        item
        for item in devices
        if not getattr(item, "is_registered_in_acs", False) and not item.ont_unit_id
    ]

    def _seen_recently(item: Tr069CpeDevice) -> bool:
        informed_at = item.last_inform_at
        if informed_at is None:
            return False
        if informed_at.tzinfo is None:
            informed_at = informed_at.replace(tzinfo=UTC)
        return informed_at >= seen_window

    return {
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "devices": devices,
        "configured_devices": configured_devices,
        "unconfigured_devices": unconfigured_devices,
        "recent_jobs": jobs,
        "managed_cpes": managed_cpes,
        "job_actions": _JOB_ACTIONS,
        "config_actions": _CONFIG_ACTIONS,
        "cpe_typeahead_map": cpe_typeahead_map,
        "cpe_typeahead_labels": list(cpe_typeahead_map.keys()),
        "cpe_display_by_id": cpe_display_by_id,
        "cpe_search_by_id": cpe_search_by_id,
        "stats": {
            "servers": len(servers),
            "devices": len(devices),
            "registered": len(registered_devices),
            "expected": len(expected_devices),
            "unmatched": len(unmatched_devices),
            "unlinked": len(unconfigured_devices),
            "configured": len(configured_devices),
            "unconfigured": len(unconfigured_devices),
            "seen_24h": sum(1 for item in devices if _seen_recently(item)),
            "jobs_failed": sum(
                1 for item in jobs if item.status == Tr069JobStatus.failed
            ),
            "jobs_pending": sum(
                1
                for item in jobs
                if item.status
                in {
                    Tr069JobStatus.queued,
                    Tr069JobStatus.running,
                    Tr069JobStatus.pending,
                }
            ),
            "jobs_unverified": sum(
                1 for item in jobs if item.status == Tr069JobStatus.unverified
            ),
        },
        "filters": {
            "search": str(search or "").strip(),
            "only_unlinked": bool(only_unlinked),
        },
    }


def sync_server(db: Session, *, acs_server_id: str) -> dict[str, int]:
    return tr069_service.cpe_devices.sync_from_genieacs(
        db=db, acs_server_id=acs_server_id
    )


def create_ont_from_tr069_device(
    db: Session, *, tr069_device_id: str, request: Request | None = None
) -> tuple[OntUnit, bool]:
    """Create or resolve an ONT inventory record from a synced TR-069 device.

    Returns (ont, created_new).
    """
    device = db.get(Tr069CpeDevice, coerce_uuid(tr069_device_id))
    if not device:
        raise ValueError("TR-069 device not found")

    display_serial = (
        _display_serial_number(device.serial_number)
        or str(device.serial_number or "").strip()
    )
    if not display_serial:
        raise ValueError("TR-069 device has no usable serial number")

    normalized_serial = normalize_tr069_serial(display_serial)
    candidates = (
        db.query(OntUnit)
        .filter(_normalized_serial_expr(OntUnit.serial_number) == normalized_serial)
        .all()
    )
    assignment_ids = {
        str(row[0])
        for row in db.query(OntAssignment.ont_unit_id)
        .filter(OntAssignment.active.is_(True))
        .filter(
            OntAssignment.ont_unit_id.in_([candidate.id for candidate in candidates])
        )
        .all()
    }
    existing = next(
        iter(
            sorted(
                candidates,
                key=lambda candidate: (
                    str(candidate.id) not in assignment_ids,
                    candidate.olt_device_id is None,
                    not candidate.is_active,
                    candidate.created_at,
                ),
            )
        ),
        None,
    )
    if existing:
        if not getattr(existing, "olt_device_id", None) and not getattr(
            existing, "tr069_acs_server_id", None
        ):
            existing.tr069_acs_server_id = device.acs_server_id
        tr069_service.link_tr069_device_to_ont(
            db,
            device,
            existing,
            acs_server_id=device.acs_server_id,
        )
        db.commit()
        db.refresh(existing)
        log_tr069_audit_event(
            db,
            request=request,
            action="create",
            entity_type="ont",
            entity_id=existing.id,
            metadata={
                "source": "tr069_device",
                "tr069_device_id": tr069_device_id,
                "created_new": False,
                "serial_number": existing.serial_number,
            },
        )
        return existing, False

    payload = OntUnitCreate(
        serial_number=display_serial,
        vendor="Huawei"
        if str(device.oui or "").upper().startswith(("48575443", "HWTC", "HWTT"))
        else None,
        model=device.product_class or None,
        is_active=False,
        name=display_serial,
        notes=f"Imported from TR-069 device {device.id}",
    )
    ont = network_service.ont_units.create(db=db, payload=payload)
    ont.tr069_acs_server_id = device.acs_server_id
    tr069_service.link_tr069_device_to_ont(
        db,
        device,
        ont,
        acs_server_id=device.acs_server_id,
    )
    db.commit()
    db.refresh(ont)
    log_tr069_audit_event(
        db,
        request=request,
        action="create",
        entity_type="ont",
        entity_id=ont.id,
        metadata={
            "source": "tr069_device",
            "tr069_device_id": tr069_device_id,
            "created_new": True,
            "serial_number": ont.serial_number,
        },
    )
    return ont, True


def get_config_actions() -> dict[str, ConfigAction]:
    """Return available config push actions."""
    return _CONFIG_ACTIONS


def create_config_push_job(
    db: Session,
    *,
    tr069_device_id: str,
    action_key: str,
    value: str,
    request: Request | None = None,
) -> Tr069AdmissionOutcome:
    """Create a config push job (setParameterValues) for a TR-069 device.

    Args:
        db: Database session
        tr069_device_id: TR-069 CPE device ID
        action_key: Config action key (e.g., 'wifi_ssid', 'pppoe_username')
        value: Value to set

    Returns:
        Created job
    """
    config_action = _CONFIG_ACTIONS.get(action_key)
    if config_action is None:
        raise ValueError(f"Unknown config action: {action_key}")

    # Build parameter values - try TR-181 path first, fallback to TR-098
    # Device. paths are TR-181, InternetGatewayDevice. paths are TR-098
    parameter_path = config_action.parameters[0]  # Primary path
    parameter_value: str | bool = value
    parameter_type = "xsd:string"

    # Handle boolean toggle actions
    if action_key in {"wifi_enable", "dhcp_enable"}:
        parameter_value = True
        parameter_type = "xsd:boolean"
    elif action_key in {"wifi_disable", "dhcp_disable"}:
        parameter_value = False
        parameter_type = "xsd:boolean"

    outcome = _admit_tr069_command(
        db,
        request=request,
        device_id=tr069_device_id,
        name=config_action.label,
        kind=Tr069CommandKind.set_parameter_values,
        parameter_values=(
            Tr069ParameterValue(
                path=parameter_path,
                value=parameter_value,
                value_type=parameter_type,
            ),
        ),
    )
    log_tr069_audit_event(
        db,
        request=request,
        action="config_push",
        entity_type="tr069_device",
        entity_id=tr069_device_id,
        metadata={
            "config_action": action_key,
            "job_id": str(outcome.job_id),
            "operation_id": str(outcome.operation_id),
        },
    )
    return outcome


def create_firmware_download_job(
    db: Session,
    *,
    tr069_device_id: str,
    firmware_url: str,
    filename: str | None = None,
    request: Request | None = None,
) -> Tr069AdmissionOutcome:
    """Create a firmware download job for a TR-069 device.

    Args:
        db: Database session
        tr069_device_id: TR-069 CPE device ID
        firmware_url: URL to download firmware from
        filename: Optional filename for the firmware

    Returns:
        Created job
    """
    if not firmware_url or not firmware_url.strip():
        raise ValueError("Firmware URL is required")

    outcome = _admit_tr069_command(
        db,
        request=request,
        device_id=tr069_device_id,
        name="Firmware Update",
        kind=Tr069CommandKind.download,
        download=Tr069Download(
            url=firmware_url.strip(),
            filename=filename.strip() if filename and filename.strip() else None,
        ),
    )
    log_tr069_audit_event(
        db,
        request=request,
        action="firmware_update",
        entity_type="tr069_device",
        entity_id=tr069_device_id,
        metadata={
            "filename": filename,
            "firmware_url": "[redacted]",
            "job_id": str(outcome.job_id),
            "operation_id": str(outcome.operation_id),
        },
    )
    return outcome


def queue_bulk_action(
    db: Session,
    device_ids: list[str],
    action: str | None = None,
    params: dict | None = None,
    request: Request | None = None,
) -> Tr069BulkAdmissionOutcome:
    """Admit each bulk action through the durable lifecycle owner.

    Args:
        device_ids: List of TR-069 device UUIDs
        action: Action to execute (refresh, reboot, factory_reset, config_push, firmware)
        params: Additional parameters for certain actions

    Each device is its own atomic operation. The returned result makes partial
    admission explicit instead of hiding it behind a non-durable Celery
    envelope.
    """
    if action is None:
        raise ValueError("No action selected")
    if not device_ids:
        raise ValueError("No devices selected for bulk action")

    valid_actions = {"refresh", "reboot", "factory_reset", "config_push", "firmware"}
    if action not in valid_actions:
        raise ValueError(f"Invalid bulk action: {action}")

    accepted: list[Tr069AdmissionOutcome] = []
    rejected: list[tuple[str, str]] = []
    action_params = params or {}
    for device_id in device_ids:
        try:
            if action in _JOB_ACTIONS:
                accepted.append(
                    queue_device_job(
                        db,
                        tr069_device_id=device_id,
                        action=action,
                        request=request,
                    )
                )
            elif action == "config_push":
                parameter_path = str(action_params.get("parameter_path") or "").strip()
                if not parameter_path:
                    raise ValueError("config_push requires parameter_path")
                accepted.append(
                    _admit_tr069_command(
                        db,
                        request=request,
                        device_id=device_id,
                        name="Config Push",
                        kind=Tr069CommandKind.set_parameter_values,
                        parameter_values=(
                            Tr069ParameterValue(
                                path=parameter_path,
                                value=str(action_params.get("parameter_value") or ""),
                                value_type="xsd:string",
                            ),
                        ),
                    )
                )
            else:
                firmware_url = str(action_params.get("firmware_url") or "").strip()
                if not firmware_url:
                    raise ValueError("firmware requires firmware_url")
                accepted.append(
                    _admit_tr069_command(
                        db,
                        request=request,
                        device_id=device_id,
                        name="Firmware Update",
                        kind=Tr069CommandKind.download,
                        download=Tr069Download(
                            url=firmware_url,
                            filename=(
                                str(action_params.get("filename") or "").strip() or None
                            ),
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            rejected.append((device_id, str(exc)))

    if not accepted:
        details = rejected[0][1] if rejected else "No commands were accepted"
        raise ValueError(details)

    outcome = Tr069BulkAdmissionOutcome(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )
    logger.info(
        "tr069_bulk_action_admitted",
        extra={
            "event": "tr069_bulk_action",
            "action": action,
            "accepted_count": len(outcome.accepted),
            "rejected_count": len(outcome.rejected),
        },
    )
    log_tr069_audit_event(
        db,
        request=request,
        action="bulk_action",
        entity_type="tr069_devices",
        entity_id=outcome.accepted[0].operation_id,
        metadata={
            "action": action,
            "device_count": len(device_ids),
            "accepted_count": len(outcome.accepted),
            "rejected_count": len(outcome.rejected),
            "operation_ids": [str(item.operation_id) for item in outcome.accepted],
        },
    )
    return outcome


def create_nat_port_forward_job(
    db: Session,
    *,
    tr069_device_id: str,
    external_port: int,
    internal_ip: str,
    internal_port: int,
    protocol: str,
    description: str | None = None,
    request: Request | None = None,
) -> Tr069AdmissionOutcome:
    """Create a NAT port forwarding rule on a TR-069 device.

    Args:
        db: Database session
        tr069_device_id: TR-069 CPE device ID
        external_port: External/WAN port number
        internal_ip: Internal/LAN IP address to forward to
        internal_port: Internal/LAN port number
        protocol: Protocol (TCP, UDP, or TCP/UDP)
        description: Optional description for the rule

    Returns:
        Created job
    """
    # Validate inputs
    if not (1 <= external_port <= 65535):
        raise ValueError("External port must be between 1 and 65535")
    if not (1 <= internal_port <= 65535):
        raise ValueError("Internal port must be between 1 and 65535")

    protocol_upper = protocol.upper()
    if protocol_upper not in {"TCP", "UDP", "TCP/UDP"}:
        raise ValueError("Protocol must be TCP, UDP, or TCP/UDP")

    try:
        normalized_internal_ip = str(IPv4Address(internal_ip))
    except ValueError as exc:
        raise ValueError("Invalid internal IP address format") from exc

    # Build port mapping parameters using TR-181 paths
    # GenieACS uses addObject to create new instances
    rule_description = (
        description
        or f"Port {external_port} to {normalized_internal_ip}:{internal_port}"
    )

    # First, we need to add an object, then set its values
    # This is a two-step process in TR-069, but GenieACS can handle it with
    # setParameterValues on a new index if we use the right approach
    # For simplicity, we'll use a provision script approach or direct SPV

    # TR-181 PortMapping structure
    # Device.NAT.PortMapping.{i}.Enable
    # Device.NAT.PortMapping.{i}.Protocol
    # Device.NAT.PortMapping.{i}.ExternalPort
    # Device.NAT.PortMapping.{i}.InternalClient
    # Device.NAT.PortMapping.{i}.InternalPort
    # Device.NAT.PortMapping.{i}.Description

    # Use setParameterValues with a new instance indicator
    # GenieACS handles addObject internally when setting on non-existent instance
    parameter_values = (
        Tr069ParameterValue("Device.NAT.PortMapping.*.Enable", True, "xsd:boolean"),
        Tr069ParameterValue(
            "Device.NAT.PortMapping.*.Protocol", protocol_upper, "xsd:string"
        ),
        Tr069ParameterValue(
            "Device.NAT.PortMapping.*.ExternalPort",
            external_port,
            "xsd:unsignedInt",
        ),
        Tr069ParameterValue(
            "Device.NAT.PortMapping.*.InternalClient",
            normalized_internal_ip,
            "xsd:string",
        ),
        Tr069ParameterValue(
            "Device.NAT.PortMapping.*.InternalPort",
            internal_port,
            "xsd:unsignedInt",
        ),
        Tr069ParameterValue(
            "Device.NAT.PortMapping.*.Description",
            rule_description,
            "xsd:string",
        ),
    )

    outcome = _admit_tr069_command(
        db,
        request=request,
        device_id=tr069_device_id,
        name=(
            f"NAT Forward: {external_port} → {normalized_internal_ip}:{internal_port}"
        ),
        kind=Tr069CommandKind.set_parameter_values,
        parameter_values=parameter_values,
    )
    log_tr069_audit_event(
        db,
        request=request,
        action="nat_port_forward",
        entity_type="tr069_device",
        entity_id=tr069_device_id,
        metadata={
            "external_port": external_port,
            "internal_ip": normalized_internal_ip,
            "internal_port": internal_port,
            "protocol": protocol_upper,
            "job_id": str(outcome.job_id),
            "operation_id": str(outcome.operation_id),
        },
    )
    return outcome


def push_acs_enforcement_preset(
    db: Session,
    acs_id: str,
    *,
    on_bootstrap: bool,
    on_boot: bool,
    on_periodic: bool,
    precondition: str,
    request: Request | None = None,
) -> dict[str, object]:
    result = tr069_service.push_acs_enforcement_preset(
        db,
        acs_id,
        on_bootstrap=on_bootstrap,
        on_boot=on_boot,
        on_periodic=on_periodic,
        precondition=precondition,
    )
    log_tr069_audit_event(
        db,
        request=request,
        action="create_enforcement_preset",
        entity_type="tr069_acs_server",
        entity_id=acs_id,
        metadata={
            "preset_id": result.get("preset_id"),
            "provision_id": result.get("provision_id"),
            "cwmp_url": result.get("cwmp_url"),
        },
    )
    return result


def remove_acs_enforcement_preset(
    db: Session, acs_id: str, *, request: Request | None = None
) -> dict[str, object]:
    result = tr069_service.remove_acs_enforcement_preset(db, acs_id)
    log_tr069_audit_event(
        db,
        request=request,
        action="remove_enforcement_preset",
        entity_type="tr069_acs_server",
        entity_id=acs_id,
        metadata={
            "preset_id": result.get("preset_id"),
            "provision_id": result.get("provision_id"),
            "removed": result.get("removed"),
        },
    )
    return result
