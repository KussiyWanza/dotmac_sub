"""ONT inventory lifecycle services."""

from __future__ import annotations

import logging
import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.compensation_failure import CompensationFailure
from app.models.network import (
    IPv4Address,
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
)
from app.services import network as network_service
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.cpe import ensure_cpe_for_ont
from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    HuaweiCliResource,
    classify_huawei_cli_response,
    is_huawei_resource_absent,
)
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_desired_config import clear_desired_config
from app.services.network.ont_inventory_release import (
    release_ont_electronic_identity,
)
from app.services.network.ont_status import (
    clear_authorization_status,
    reset_status_for_inventory,
    set_provisioning_status,
)

logger = logging.getLogger(__name__)


class _ReturnToInventoryStopped(Exception):
    """Internal control-flow exception used to rollback the return savepoint."""


def _normalize_fsp(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw.lower().startswith("pon-"):
        raw = raw[4:].strip()
    return raw or None


def _parse_ont_id_on_olt(external_id: str | None) -> int | None:
    ext = (external_id or "").strip()
    if ext.isdigit():
        return int(ext)
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    if ":" in ext:
        suffix = ext.rsplit(":", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


#: Failures that never reached the Huawei command grammar. Nothing to confirm.
_TRANSPORT_FAILURE_CODES = frozenset(
    {HuaweiCliErrorCode.CONNECTION_ERROR, HuaweiCliErrorCode.TIMEOUT}
)


def _is_ont_already_absent(message: str | None) -> bool:
    return is_huawei_resource_absent(message, HuaweiCliResource.ONT)


def _is_service_port_already_absent(message: str | None) -> bool:
    return is_huawei_resource_absent(message, HuaweiCliResource.SERVICE_PORT)


def _verify_service_port_absent_on_olt(
    olt: OLTDevice, service_port_index: int
) -> tuple[bool, str]:
    from app.services.network.olt_ssh_service_ports import get_service_port_by_index

    ok, message, entry = get_service_port_by_index(olt, service_port_index)
    if ok and entry is None:
        return True, message
    if not ok and _is_service_port_already_absent(message):
        return True, message
    if ok and entry is not None:
        return False, f"Service-port {service_port_index} still exists on the OLT"
    return False, message


def _should_reconcile_service_port_delete_failure(result: object) -> bool:
    """Whether a failed delete is worth confirming against the device.

    A *device* rejection may still mean the port is gone (or was never there),
    so it earns a readback. A transport failure does not. This used to sniff
    the ``"olt rejected"`` prefix out of the message, which coupled the
    decision to one call site's wording; it now reads the typed verdict.
    """
    code = getattr(result, "response_code", None)
    if code is None:
        code = classify_huawei_cli_response(getattr(result, "message", None)).error_code
    if code in _TRANSPORT_FAILURE_CODES:
        return False
    return code is not HuaweiCliErrorCode.NONE


def _resolve_return_olt_context(
    db: Session, ont_id: str
) -> tuple[object | None, OLTDevice | None, str | None, int | None]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    olt = db.get(OLTDevice, str(ont.olt_device_id)) if ont.olt_device_id else None
    board = (ont.board or "").strip()
    port = (ont.port or "").strip()
    fsp = _normalize_fsp(f"{board}/{port}") if board and port else None
    ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)
    return ont, olt, fsp, ont_id_on_olt


def cleanup_olt_state_for_return(
    db: Session, ont_id: str
) -> tuple[bool, list[str], list[str]]:
    """Remove OLT-side service ports and deauthorize an ONT before inventory return."""
    from app.services.network.imported_service_ports import (
        ImportedServicePortStateMissing,
        delete_imported_service_port,
        list_imported_service_ports,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.service_port_allocator import release_all_for_ont

    completed: list[str] = []
    errors: list[str] = []

    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return False, completed, ["ONT not found"]
    if not olt or not fsp or olt_ont_id is None:
        return True, completed, errors

    adapter = get_protocol_adapter(olt)
    service_ports_by_index = {}
    try:
        imported_ports = list_imported_service_ports(
            db,
            olt_id=olt.id,
            fsp=fsp,
            ont_id_on_olt=olt_ont_id,
        )
        for service_port in imported_ports:
            service_ports_by_index[service_port.index] = service_port
    except ImportedServicePortStateMissing as exc:
        completed.append(f"Imported service-port state unavailable: {exc}")

    live_ports_result = adapter.get_service_ports_for_ont(fsp, olt_ont_id)
    if live_ports_result.success:
        for service_port in (live_ports_result.data or {}).get("service_ports", []):
            service_ports_by_index[service_port.index] = service_port
    elif not service_ports_by_index:
        errors.append(
            "Failed to read live service-ports and no imported service-port "
            f"state is available: {live_ports_result.message}"
        )
        return False, completed, errors

    deleted_service_ports = []
    for service_port in service_ports_by_index.values():
        delete_result = adapter.delete_service_port(service_port.index)
        if not delete_result.success:
            verify_message = "not checked"
            if _should_reconcile_service_port_delete_failure(delete_result):
                absent, verify_message = _verify_service_port_absent_on_olt(
                    olt, service_port.index
                )
                if absent:
                    delete_imported_service_port(
                        db,
                        olt_id=olt.id,
                        port_index=service_port.index,
                    )
                    db.flush()
                    completed.append(
                        f"Service-port {service_port.index} already absent from OLT; "
                        "removed stale imported row"
                    )
                    try:
                        emit_event(
                            db,
                            EventType.ont_service_port_deleted,
                            {
                                "ont_id": ont_id,
                                "ont_serial": getattr(ont, "serial_number", None),
                                "olt_id": str(olt.id),
                                "olt_name": olt.name,
                                "service_port_index": service_port.index,
                                "already_absent": True,
                            },
                            actor="system",
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to emit ont_service_port_deleted event: %s", exc
                        )
                    continue
            errors.append(
                f"Failed to remove service-port {service_port.index}: "
                f"{delete_result.message}; live check: {verify_message}"
            )
            return False, completed, errors
        delete_imported_service_port(
            db,
            olt_id=olt.id,
            port_index=service_port.index,
        )
        db.flush()
        deleted_service_ports.append(service_port)
        completed.append(f"Removed service-port {service_port.index}")
        try:
            emit_event(
                db,
                EventType.ont_service_port_deleted,
                {
                    "ont_id": ont_id,
                    "ont_serial": getattr(ont, "serial_number", None),
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "service_port_index": service_port.index,
                },
                actor="system",
            )
        except Exception as exc:
            logger.warning("Failed to emit ont_service_port_deleted event: %s", exc)

    deauth_result = adapter.deauthorize_ont(fsp, olt_ont_id)
    if not deauth_result.success:
        if _is_ont_already_absent(deauth_result.message):
            completed.append("ONT already absent from OLT")
        else:
            rollback_messages = []
            for service_port in reversed(deleted_service_ports):
                create_result = adapter.create_service_port(
                    fsp,
                    olt_ont_id,
                    gem_index=service_port.gem_index,
                    vlan_id=service_port.vlan_id,
                    user_vlan=service_port.flow_para or service_port.vlan_id,
                    tag_transform=service_port.tag_transform or "translate",
                    port_index=service_port.index,
                )
                if create_result.success:
                    rollback_messages.append(
                        f"restored service-port {service_port.index}"
                    )
                else:
                    rollback_messages.append(
                        "failed to restore service-port "
                        f"{service_port.index}: {create_result.message}"
                    )
            errors.append(f"Failed to deauthorize ONT: {deauth_result.message}")
            if rollback_messages:
                errors.append("Rollback: " + "; ".join(rollback_messages))
            return False, completed, errors
    else:
        completed.append("Deauthorized ONT from OLT")

    released_allocations = release_all_for_ont(db, ont_id)
    if released_allocations:
        completed.append(f"Released {released_allocations} service-port allocation(s)")

    if not deauth_result.success:
        return True, completed, errors
    try:
        emit_event(
            db,
            EventType.ont_deauthorized,
            {
                "ont_id": ont_id,
                "ont_serial": getattr(ont, "serial_number", None),
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                "fsp": fsp,
                "ont_id_on_olt": olt_ont_id,
            },
            actor="system",
        )
    except Exception as exc:
        logger.warning("Failed to emit ont_deauthorized event: %s", exc)

    return True, completed, errors


def cleanup_acs_state_for_return(db: Session, ont) -> tuple[bool, list[str], list[str]]:
    """Delete GenieACS device records linked to an ONT before inventory return."""
    from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
    from app.services.genieacs_client import GenieACSError, create_genieacs_client
    from app.services.network._resolve import (
        _normalized_serial_expr,
        _serial_search_candidates,
    )

    completed: list[str] = []
    errors: list[str] = []
    deleted: set[tuple[str, str]] = set()
    clients: dict[str, object] = {}

    def _client_for(server: Tr069AcsServer):
        key = str(server.id)
        if key not in clients:
            clients[key] = create_genieacs_client(server.base_url)
        return clients[key]

    def _delete_device(server: Tr069AcsServer, genieacs_device_id: str) -> bool:
        key = (str(server.id), genieacs_device_id)
        if key in deleted:
            return True
        if not getattr(server, "base_url", None):
            errors.append(
                f"Cannot delete ACS device {genieacs_device_id}: ACS server is missing."
            )
            return False
        try:
            _client_for(server).delete_device(genieacs_device_id)
            deleted.add(key)
            completed.append(f"Deleted ACS device {genieacs_device_id}")
            return True
        except GenieACSError as exc:
            if "404" in str(exc):
                deleted.add(key)
                completed.append(f"ACS device {genieacs_device_id} was already absent")
                return True
            errors.append(f"Failed to delete ACS device {genieacs_device_id}: {exc}")
            return False

    def _clear_local_acs_identity(device: Tr069CpeDevice) -> bool:
        changed = False
        if getattr(device, "genieacs_device_id", None):
            device.genieacs_device_id = None
            changed = True
        if getattr(device, "connection_request_url", None):
            device.connection_request_url = None
            changed = True
        return changed

    linked_devices = db.scalars(
        select(Tr069CpeDevice).where(Tr069CpeDevice.ont_unit_id == ont.id)
    ).all()
    local_devices_to_clear = {device.id: device for device in linked_devices}
    for device in linked_devices:
        genieacs_device_id = str(
            getattr(device, "genieacs_device_id", "") or ""
        ).strip()
        if not genieacs_device_id:
            continue
        server = db.get(Tr069AcsServer, device.acs_server_id)
        if server is None:
            errors.append(
                f"Cannot delete ACS device {genieacs_device_id}: ACS server is missing."
            )
            return False, completed, errors
        if not _delete_device(server, genieacs_device_id):
            return False, completed, errors

    serial_candidates = _serial_search_candidates(getattr(ont, "serial_number", None))
    if serial_candidates:
        normalized_candidates = [
            re.sub(r"[^A-Za-z0-9]+", "", candidate).upper()
            for candidate in serial_candidates
        ]
        normalized_candidates = [
            candidate for candidate in dict.fromkeys(normalized_candidates) if candidate
        ]
        local_conditions = [
            _normalized_serial_expr(Tr069CpeDevice.serial_number).in_(
                normalized_candidates
            )
        ]
        for candidate in normalized_candidates:
            local_conditions.append(
                Tr069CpeDevice.genieacs_device_id.ilike(f"%-{candidate}")
            )
        matching_local_devices = db.scalars(
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.is_active.is_(True))
            .where(or_(*local_conditions))
        ).all()
        for device in matching_local_devices:
            local_devices_to_clear[device.id] = device

    cleared_local_identities = 0
    for device in local_devices_to_clear.values():
        if _clear_local_acs_identity(device):
            cleared_local_identities += 1
    if cleared_local_identities:
        completed.append(
            "Cleared local ACS identity"
            if cleared_local_identities == 1
            else f"Cleared {cleared_local_identities} local ACS identities"
        )

    if not serial_candidates:
        return True, completed, errors

    active_servers = list(
        db.scalars(
            select(Tr069AcsServer)
            .where(Tr069AcsServer.is_active.is_(True))
            .order_by(Tr069AcsServer.name)
        ).all()
    )
    linked_servers = [
        db.get(Tr069AcsServer, device.acs_server_id) for device in linked_devices
    ]
    servers = []
    seen_server_ids = set()
    for server in [*linked_servers, *active_servers]:
        if server is None or str(server.id) in seen_server_ids:
            continue
        seen_server_ids.add(str(server.id))
        servers.append(server)

    for server in servers:
        if not getattr(server, "base_url", None):
            continue
        client = _client_for(server)
        for candidate in serial_candidates:
            try:
                devices = client.list_devices(
                    query={
                        "$or": [
                            {"_id": {"$regex": f".*-{re.escape(candidate)}$"}},
                            {"_deviceId._SerialNumber": candidate},
                            {"_deviceId.SerialNumber": candidate},
                            {"Device.DeviceInfo.SerialNumber._value": candidate},
                            {
                                "InternetGatewayDevice.DeviceInfo.SerialNumber._value": (
                                    candidate
                                )
                            },
                        ]
                    },
                    projection={"_id": 1},
                )
            except GenieACSError as exc:
                errors.append(
                    f"Failed to search ACS server {server.name} for serial {candidate}: {exc}"
                )
                return False, completed, errors
            for row in devices:
                device_id = str(row.get("_id") or "").strip()
                if not device_id:
                    continue
                if not _delete_device(server, device_id):
                    return False, completed, errors
    return True, completed, errors


def _record_return_to_inventory_compensation(
    db: Session,
    *,
    ont,
    olt_id: object | None,
    fsp: str | None,
    description: str,
    error_message: str,
) -> None:
    failure = CompensationFailure(
        ont_unit_id=getattr(ont, "id", None),
        olt_device_id=olt_id,
        operation_type="return_to_inventory",
        step_name="manual_return_cleanup_review",
        undo_commands=[],
        description=description,
        resource_id=str(getattr(ont, "id", "")),
        interface_path=fsp,
        error_message=error_message,
    )
    db.add(failure)
    db.commit()


def _release_management_ip_for_inventory_return(
    db: Session,
    *,
    ont,
    assignments: list[OntAssignment],
) -> list[str]:
    """Release management IP reservations held by the returned ONT."""
    released: list[str] = []
    ont_id = str(getattr(ont, "id", "") or "")
    reservation_notes = {
        f"ont:{ont_id}",
        f"Reserved for ONT {ont_id}",
    }
    all_assignments = list(
        db.scalars(
            select(OntAssignment).where(OntAssignment.ont_unit_id == ont.id)
        ).all()
    )
    by_id = {
        assignment.id: assignment for assignment in [*assignments, *all_assignments]
    }
    assignments_to_clear = list(by_id.values())
    assignment_ips = {
        str(assignment.mgmt_ip_address).strip()
        for assignment in assignments_to_clear
        if getattr(assignment, "mgmt_ip_address", None)
    }

    records: list[IPv4Address] = []
    if assignment_ips:
        records.extend(
            db.scalars(
                select(IPv4Address).where(IPv4Address.address.in_(assignment_ips))
            ).all()
        )
    if ont_id:
        records.extend(
            db.scalars(
                select(IPv4Address).where(
                    (IPv4Address.ont_unit_id == ont.id)
                    | (IPv4Address.notes.in_(reservation_notes))
                )
            ).all()
        )

    seen_record_ids = set()
    for record in records:
        if record.id in seen_record_ids:
            continue
        seen_record_ids.add(record.id)
        if getattr(record, "assignment", None):
            continue
        record_owner = getattr(record, "ont_unit_id", None)
        if record_owner is not None and str(record_owner) != ont_id:
            continue
        record_notes = str(getattr(record, "notes", "") or "").strip()
        if record_notes and record_notes not in reservation_notes:
            continue
        allocation_type = str(getattr(record, "allocation_type", "") or "").strip()
        if allocation_type and allocation_type != "management":
            continue
        released.append(record.address)
        record.is_reserved = False
        record.notes = None
        record.ont_unit_id = None
        record.allocation_type = None

    for assignment in assignments_to_clear:
        assignment.mgmt_ip_address = None
        assignment.mgmt_ip_mode = MgmtIpMode.inactive
        assignment.mgmt_subnet = None
        assignment.mgmt_gateway = None

    return released


def _configured_wan_static_ips_for_inventory_return(
    *, ont, assignments: list[OntAssignment]
) -> set[str]:
    """Collect WAN static IPs tied to the returned ONT's saved service config."""
    static_ips = {
        str(assignment.static_ip).strip()
        for assignment in assignments
        if getattr(assignment, "static_ip", None)
    }
    desired_config = getattr(ont, "desired_config", None)
    if isinstance(desired_config, dict):
        wan_config = desired_config.get("wan")
        if isinstance(wan_config, dict) and wan_config.get("static_ip"):
            static_ips.add(str(wan_config["static_ip"]).strip())
        if desired_config.get("static_ip"):
            static_ips.add(str(desired_config["static_ip"]).strip())
    return {ip for ip in static_ips if ip}


def _release_wan_static_ip_for_inventory_return(
    db: Session,
    *,
    ont,
    assignments: list[OntAssignment],
) -> list[str]:
    """Deactivate subscriber WAN IPAM assignments owned by the returned ONT."""
    all_assignments = list(
        db.scalars(
            select(OntAssignment).where(OntAssignment.ont_unit_id == ont.id)
        ).all()
    )
    by_id = {
        assignment.id: assignment for assignment in [*assignments, *all_assignments]
    }
    assignments_to_release = list(by_id.values())
    subscriber_ids = {
        str(assignment.subscriber_id)
        for assignment in assignments_to_release
        if getattr(assignment, "subscriber_id", None)
    }
    service_address_ids = {
        str(assignment.service_address_id)
        for assignment in assignments_to_release
        if getattr(assignment, "service_address_id", None)
    }
    candidate_ips = _configured_wan_static_ips_for_inventory_return(
        ont=ont,
        assignments=assignments_to_release,
    )
    if not candidate_ips or (not subscriber_ids and not service_address_ids):
        return []

    records = db.scalars(
        select(IPv4Address).where(IPv4Address.address.in_(candidate_ips))
    ).all()
    released: list[str] = []
    for record in records:
        assignment = getattr(record, "assignment", None)
        if assignment is None or not getattr(assignment, "is_active", False):
            continue
        assignment_subscriber_id = str(assignment.subscriber_id)
        assignment_service_address_id = (
            str(assignment.service_address_id)
            if getattr(assignment, "service_address_id", None)
            else None
        )
        if service_address_ids:
            if assignment_service_address_id not in service_address_ids:
                continue
        elif assignment_subscriber_id not in subscriber_ids:
            continue
        if str(getattr(record, "allocation_type", "") or "").strip() != "wan":
            continue
        assignment.is_active = False
        record.allocation_type = None
        released.append(record.address)

    return released


def _clear_assignment_service_config_for_inventory_return(
    db: Session,
    *,
    ont,
    assignments: list[OntAssignment],
) -> int:
    """Clear non-identity service configuration from returned ONT assignments."""
    all_assignments = list(
        db.scalars(
            select(OntAssignment).where(OntAssignment.ont_unit_id == ont.id)
        ).all()
    )
    by_id = {
        assignment.id: assignment for assignment in [*assignments, *all_assignments]
    }
    assignments_to_clear = list(by_id.values())

    for assignment in assignments_to_clear:
        assignment.wan_mode = None
        assignment.ip_mode = MgmtIpMode.inactive
        assignment.static_ip = None
        assignment.static_gateway = None
        assignment.static_subnet = None
        assignment.static_dns = None
        assignment.pppoe_username = None
        assignment.pppoe_password = None
        assignment.wifi_ssid = None
        assignment.wifi_password = None
        assignment.lan_ip = None
        assignment.lan_subnet = None
        assignment.lan_dhcp_enabled = None
        assignment.lan_dhcp_start = None
        assignment.lan_dhcp_end = None
        assignment.wifi_enabled = None
        assignment.wifi_security_mode = None
        assignment.wifi_channel = None

    return len(assignments_to_clear)


def _assert_inventory_return_links_cleared(db: Session, *, ont) -> None:
    """Fail the return if reusable inventory still has subscriber/topology links."""
    stale_assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(
            or_(
                OntAssignment.active.is_(True),
                OntAssignment.subscription_id.is_not(None),
                OntAssignment.subscriber_id.is_not(None),
                OntAssignment.service_address_id.is_not(None),
                OntAssignment.pon_port_id.is_not(None),
            )
        )
        .limit(1)
    ).first()
    if stale_assignment is not None:
        raise RuntimeError(
            "Return-to-inventory invariant failed: assignment links remain"
        )


def reset_ont_service_state(db: Session, ont, *, reason: str = "service_reset") -> None:
    """Clear desired-state and runtime cache for a reusable ONT.

    Args:
        db: Database session
        ont: ONT unit to reset
        reason: Reason for the reset. Recorded as the retirement reason on every
            WAN service intent this reset closes, so the history says why.
    """
    from app.services.network.ont_wan_service_intent import (
        retire_ont_intents_in_transaction,
    )
    from app.services.owner_commands import CommandContext

    clear_desired_config(ont)

    # Retired, not deleted. These rows are the record of what a service was
    # declared to be; a later adjudication of "did this ONT ever legitimately
    # terminate PPP for this subscription?" has nothing to read if the reset
    # erased them. The owner also keeps lifecycle_state and is_active in step,
    # which the previous bulk delete had no way to express.
    retire_ont_intents_in_transaction(
        db,
        ont_id=ont.id,
        context=CommandContext.system(
            actor="system:ont_inventory.reset_ont_service_state",
            reason=reason,
            scope=f"ont:{ont.id}",
        ),
    )

    set_provisioning_status(ont, OntProvisioningStatus.unprovisioned, strict=False)
    ont.last_provisioned_at = None
    clear_authorization_status(ont, reason="inventory_reset")
    ont.mac_address = None
    ont.observed_wan_ip = None
    ont.observed_pppoe_status = None
    ont.observed_lan_mode = None
    ont.observed_wifi_clients = None
    ont.observed_lan_hosts = None
    ont.observed_runtime_updated_at = None
    ont.tr069_last_snapshot = {}
    ont.tr069_last_snapshot_at = None
    ont.olt_observed_snapshot = {}
    ont.olt_observed_snapshot_at = None
    ont.tr069_acs_server_id = None
    ont.desired_tr069_profile_id = None
    ont.voip_enabled = False
    ont.provisioning_steps_completed = None
    reset_status_for_inventory(ont)
    ont.onu_rx_signal_dbm = None
    ont.olt_rx_signal_dbm = None
    ont.onu_tx_signal_dbm = None
    ont.ont_temperature_c = None
    ont.ont_voltage_v = None
    ont.ont_bias_current_ma = None
    ont.distance_meters = None
    ont.signal_updated_at = None

    return None


def return_ont_to_inventory(db: Session, ont_id: str) -> ActionResult:
    """Return an ONT to reusable inventory, closing assignments and service state."""
    from app.services.web_network_ont_autofind import (
        build_unconfigured_onts_redirect_url,
        ensure_returned_inventory_candidate,
        restore_candidate_by_serial,
    )

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    previous_olt_db_id = getattr(ont, "olt_device_id", None)
    previous_olt_id = str(previous_olt_db_id) if previous_olt_db_id else None
    previous_fsp = None
    if getattr(ont, "board", None) and getattr(ont, "port", None):
        previous_fsp = f"{ont.board}/{ont.port}"

    active_assignments = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.created_at.desc())
    ).all()
    active_assignment = active_assignments[0] if active_assignments else None

    needs_olt_cleanup = bool(
        (active_assignment is not None and active_assignment.pon_port_id)
        or ont.olt_device_id
        or ont.board
        or ont.port
        or ont.external_id
    )
    cpe = None
    completed: list[str] = []
    acs_completed: list[str] = []
    try:
        with db.begin_nested():
            if needs_olt_cleanup:
                ok, completed, errors = cleanup_olt_state_for_return(db, ont_id)
                if not ok:
                    details = ", ".join(completed + errors)
                    raise _ReturnToInventoryStopped(
                        f"Return to inventory stopped before local cleanup: {details}."
                    )

            acs_ok, acs_completed, acs_errors = cleanup_acs_state_for_return(db, ont)
            if not acs_ok:
                details = ", ".join(completed + acs_completed + acs_errors)
                raise _ReturnToInventoryStopped(
                    f"Return to inventory stopped before local cleanup: {details}."
                )

            released_management_ips = _release_management_ip_for_inventory_return(
                db,
                ont=ont,
                assignments=list(active_assignments),
            )
            released_wan_static_ips = _release_wan_static_ip_for_inventory_return(
                db,
                ont=ont,
                assignments=list(active_assignments),
            )
            identity_release = release_ont_electronic_identity(
                db,
                ont_unit_id=ont.id,
            )
            released_assignment_count = len(identity_release.deactivated_assignment_ids)
            cleared_assignment_links = (
                _clear_assignment_service_config_for_inventory_return(
                    db,
                    ont=ont,
                    assignments=list(active_assignments),
                )
            )
            ont.is_active = True
            reset_ont_service_state(db, ont)

            try:
                from app.models.tr069 import Tr069CpeDevice

                linked_devices = db.scalars(
                    select(Tr069CpeDevice).where(Tr069CpeDevice.ont_unit_id == ont.id)
                ).all()
                for device in linked_devices:
                    device.ont_unit_id = None
            except Exception as exc:
                logger.warning("Failed to clear TR-069 device association: %s", exc)

            db.flush()
            cpe = ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)
            if cpe is not None:
                logger.info(
                    "Moved CPE %s to inventory for returned ONT %s", cpe.id, ont.id
                )
            _assert_inventory_return_links_cleared(db, ont=ont)
    except _ReturnToInventoryStopped as exc:
        if completed:
            try:
                _record_return_to_inventory_compensation(
                    db,
                    ont=ont,
                    olt_id=previous_olt_db_id,
                    fsp=previous_fsp,
                    description=(
                        "OLT device state was removed, but return-to-inventory stopped "
                        "before ACS/local cleanup completed. Operator review is required."
                    ),
                    error_message=str(exc),
                )
            except Exception:
                logger.exception(
                    "Failed to record return-to-inventory compensation for ONT %s",
                    ont_id,
                )
        return ActionResult(success=False, message=str(exc))
    except Exception as exc:
        logger.exception(
            "Failed to update DB state during return-to-inventory for ONT %s", ont_id
        )
        if completed or acs_completed:
            try:
                _record_return_to_inventory_compensation(
                    db,
                    ont=ont,
                    olt_id=previous_olt_db_id,
                    fsp=previous_fsp,
                    description=(
                        "OLT/ACS device state may have been removed, but local "
                        "inventory cleanup failed. Operator review is required."
                    ),
                    error_message=str(exc),
                )
            except Exception:
                logger.exception(
                    "Failed to record return-to-inventory compensation for ONT %s",
                    ont_id,
                )
        return ActionResult(
            success=False,
            message=(
                "OLT cleanup completed but DB inventory update failed: "
                f"{exc}. Retry return-to-inventory to finish local cleanup."
            ),
        )

    db.commit()

    parts = []
    if active_assignment is not None and getattr(
        active_assignment, "pon_port_id", None
    ):
        parts.append("OLT service state removed")
    if "released_assignment_count" in locals() and released_assignment_count:
        assignment_count = released_assignment_count
        parts.append(
            "assignment closed"
            if assignment_count == 1
            else f"{assignment_count} assignments closed"
        )
    if "cleared_assignment_links" in locals() and cleared_assignment_links:
        parts.append("subscriber assignment links cleared")
    if cpe is not None:
        parts.append("CPE moved to inventory")
    if "released_management_ips" in locals() and released_management_ips:
        count = len(released_management_ips)
        parts.append(
            "management IP released"
            if count == 1
            else f"{count} management IPs released"
        )
    if "released_wan_static_ips" in locals() and released_wan_static_ips:
        count = len(released_wan_static_ips)
        parts.append(
            "static WAN IP released"
            if count == 1
            else f"{count} static WAN IPs released"
        )
    if "acs_completed" in locals() and acs_completed:
        parts.append("ACS device state removed")
    parts.append("identity cleared for rediscovery")
    parts.append("service state cleared")

    db.refresh(ont)

    candidate_ready = False
    candidate_message = ""
    if previous_olt_id and previous_fsp:
        try:
            candidate_ready, candidate_message = ensure_returned_inventory_candidate(
                db,
                olt_id=previous_olt_id,
                fsp=previous_fsp,
                serial_number=getattr(ont, "serial_number", None),
                ont_unit_id=getattr(ont, "id", None),
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            candidate_message = str(exc)
            logger.warning(
                "Failed to restore returned ONT inventory candidate: %s", exc
            )
    else:
        # OLT binding was already cleared; try to restore candidate by serial lookup
        try:
            candidate_ready, candidate_message = restore_candidate_by_serial(
                db,
                serial_number=getattr(ont, "serial_number", None),
                ont_unit_id=getattr(ont, "id", None),
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            candidate_message = str(exc)
            logger.warning(
                "Failed to restore returned ONT inventory candidate by serial: %s", exc
            )

    if candidate_ready:
        parts.append("unconfigured candidate ready")
    elif candidate_message:
        parts.append(f"unconfigured candidate restore failed: {candidate_message}")

    return ActionResult(
        success=True,
        message=(
            f"ONT returned to inventory: {', '.join(parts)}. "
            "The ONT is available in Unconfigured ONTs for reauthorization."
        ),
        data={
            "olt_id": previous_olt_id,
            "fsp": previous_fsp,
            "serial_number": ont.serial_number,
            "unconfigured_candidate_ready": candidate_ready,
            "unconfigured_url": build_unconfigured_onts_redirect_url(
                search=getattr(ont, "serial_number", None),
                olt_id=previous_olt_id,
            ),
        },
    )
