"""Network parameter actions for ONTs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.models.network import CPEDevice
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.credential_crypto import decrypt_credential
from app.services.genieacs_client import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    genieacs_error_result,
    get_ont_client_or_error,
    persist_data_model_root,
    read_param_from_cache,
    set_and_verify,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

_PPP_ADD_OBJECT_VERIFY_DELAY_SECONDS = 60
_PPP_ADD_OBJECT_PENDING_TTL_SECONDS = 10 * 60

_IGD_CONNECTION_TYPE_BY_MODE = {
    "pppoe": "IP_Routed",
    "dhcp": "IP_Routed",
    "static": "IP_Routed",
    "bridge": "IP_Bridged",
}

_LAN_CONFIG_PATHS = {
    "Device": {
        "ip_address": "IP.Interface.2.IPv4Address.1.IPAddress",
        "subnet_mask": "IP.Interface.2.IPv4Address.1.SubnetMask",
        "dhcp_enabled": "DHCPv4.Server.Enable",
        "dhcp_min_address": "DHCPv4.Server.Pool.1.MinAddress",
        "dhcp_max_address": "DHCPv4.Server.Pool.1.MaxAddress",
        "refresh": "IP.Interface.2.",
    },
    "InternetGatewayDevice": {
        "ip_address": "LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
        "subnet_mask": "LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
        "dhcp_enabled": "LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
        "dhcp_min_address": "LANDevice.1.LANHostConfigManagement.MinAddress",
        "dhcp_max_address": "LANDevice.1.LANHostConfigManagement.MaxAddress",
        "refresh": "LANDevice.1.LANHostConfigManagement.",
    },
}

_LAN_PORT_PATHS = {
    "Device": "Ethernet.Interface.{port}.Enable",
    "InternetGatewayDevice": "LANDevice.1.LANEthernetInterfaceConfig.{port}.Enable",
}


def toggle_lan_port(
    db: Session,
    ont_id: str,
    port: int,
    enabled: bool,
) -> ActionResult:
    """Compatibility writer for LAN ports pending their reconciler slice."""
    if port < 1 or port > 4:
        return ActionResult(
            success=False, message="Port number must be between 1 and 4."
        )
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error or resolved is None:
        return error or ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    path = _LAN_PORT_PATHS[root].format(port=port)
    params = build_tr069_params(root, {path: "true" if enabled else "false"})
    try:
        result = set_and_verify(client, device_id, params)
    except GenieACSError as exc:
        return genieacs_error_result(exc, "Failed to update LAN port")
    action_word = "enabled" if enabled else "disabled"
    logger.info("LAN port %d %s on ONT %s", port, action_word, ont.serial_number)
    return ActionResult(
        success=True,
        message=f"LAN port {port} {action_word}.",
        data=result,
    )


def _igd_ip_wan_details(
    client: Any,
    device: dict[str, Any],
    root: str,
    instance_index: int,
) -> dict[str, Any]:
    ip_base = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}.WANIPConnection.1"
    )
    return {
        "detected_wan_name": client.extract_parameter_value(device, f"{ip_base}.Name"),
        "detected_wan_status": client.extract_parameter_value(
            device, f"{ip_base}.ConnectionStatus"
        ),
        "detected_wan_ip": client.extract_parameter_value(
            device, f"{ip_base}.ExternalIPAddress"
        ),
        "detected_wan_service": client.extract_parameter_value(
            device, f"{ip_base}.X_HW_SERVICELIST"
        ),
        "detected_wan_vlan": client.extract_parameter_value(
            device, f"{ip_base}.X_HW_VLAN"
        ),
    }


def _igd_ppp_wan_details(
    client: Any,
    device: dict[str, Any],
    root: str,
    instance_index: int,
) -> dict[str, Any]:
    ppp_base = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}.WANPPPConnection.1"
    )
    return {
        "detected_ppp_name": client.extract_parameter_value(device, f"{ppp_base}.Name"),
        "detected_ppp_status": client.extract_parameter_value(
            device, f"{ppp_base}.ConnectionStatus"
        ),
        "detected_ppp_ip": client.extract_parameter_value(
            device, f"{ppp_base}.ExternalIPAddress"
        ),
        "detected_ppp_service": client.extract_parameter_value(
            device, f"{ppp_base}.X_HW_SERVICELIST"
        ),
        "detected_ppp_vlan": client.extract_parameter_value(
            device, f"{ppp_base}.X_HW_VLAN"
        ),
        "detected_ppp_username": client.extract_parameter_value(
            device, f"{ppp_base}.Username"
        ),
    }


from app.services.network._util import first_present as _first_present


def _igd_wan_details(
    client: Any,
    device: dict[str, Any],
    root: str,
    instance_index: int,
) -> dict[str, Any]:
    ip_details = _igd_ip_wan_details(client, device, root, instance_index)
    ppp_details = _igd_ppp_wan_details(client, device, root, instance_index)
    return {
        **ip_details,
        **ppp_details,
        "detected_wan_name": _first_present(
            ip_details.get("detected_wan_name"),
            ppp_details.get("detected_ppp_name"),
        ),
        "detected_wan_status": _first_present(
            ip_details.get("detected_wan_status"),
            ppp_details.get("detected_ppp_status"),
        ),
        "detected_wan_ip": _first_present(
            ip_details.get("detected_wan_ip"),
            ppp_details.get("detected_ppp_ip"),
        ),
        "detected_wan_service": _first_present(
            ip_details.get("detected_wan_service"),
            ppp_details.get("detected_ppp_service"),
        ),
        "detected_wan_vlan": _first_present(
            ip_details.get("detected_wan_vlan"),
            ppp_details.get("detected_ppp_vlan"),
        ),
    }


def _snapshot_value(client: Any, device: dict[str, Any], path: str) -> Any:
    return client.extract_parameter_value(device, path)


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _runtime_capabilities(ont: Any) -> dict[str, Any]:
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        snapshot = {}
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    return capabilities


def _persist_runtime_capabilities(ont: Any, capabilities: dict[str, Any]) -> None:
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        snapshot = {}
    else:
        snapshot = dict(snapshot)
    snapshot["capabilities"] = capabilities
    ont.tr069_last_snapshot = snapshot
    flag_modified(ont, "tr069_last_snapshot")
    ont.tr069_last_snapshot_at = datetime.now(UTC)


def _mark_ppp_add_object_pending(
    ont: Any,
    *,
    root: str,
    instance_index: int,
    wan_vlan: int | None,
    object_path: str,
) -> None:
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.setdefault("pending_actions", {})
    pending["add_ppp_wan"] = {
        "state": "pending_verification",
        "requested_at": _iso_now(),
        "root": root,
        "instance_index": instance_index,
        "wan_vlan": wan_vlan,
        "object_path": object_path,
    }
    wan = capabilities.setdefault("wan", {})
    wan["supports_tr069_add_ppp_wan"] = "pending_verification"
    _persist_runtime_capabilities(ont, capabilities)


def _pending_ppp_add_object(
    ont: Any, instance_index: int, wan_vlan: int | None
) -> bool:
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.get("pending_actions")
    if not isinstance(pending, dict):
        return False
    add_ppp = pending.get("add_ppp_wan")
    if not isinstance(add_ppp, dict):
        return False
    if add_ppp.get("state") != "pending_verification":
        return False
    if int(add_ppp.get("instance_index") or 0) != instance_index:
        return False
    if str(add_ppp.get("wan_vlan") or "") != str(wan_vlan or ""):
        return False
    requested_at = _parse_iso(add_ppp.get("requested_at"))
    if requested_at is None:
        return False
    return datetime.now(UTC) - requested_at < timedelta(
        seconds=_PPP_ADD_OBJECT_PENDING_TTL_SECONDS
    )


def probe_wan_capabilities(
    db: Session,
    ont_id: str,
) -> ActionResult:
    """Inspect and cache the ONT's observed WAN provisioning capabilities."""
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(success=False, message=f"Failed to fetch device: {exc}")

    capabilities = _runtime_capabilities(ont)
    wan_capabilities: dict[str, Any] = {
        "data_model": root,
        "probed_at": _iso_now(),
        "has_ppp_wan": False,
        "supports_tr069_set_ppp_credentials": False,
        "requires_precreated_ppp_wan": False,
    }

    if root == "InternetGatewayDevice":
        count_path = f"{root}.WANDevice.1.WANConnectionDeviceNumberOfEntries"
        count = _int_value(_snapshot_value(client, device, count_path))
        indexes = range(1, max(count, 4) + 1)
        connections: list[dict[str, Any]] = []
        for idx in indexes:
            base = f"{root}.WANDevice.1.WANConnectionDevice.{idx}"
            ip_count = _int_value(
                _snapshot_value(
                    client, device, f"{base}.WANIPConnectionNumberOfEntries"
                )
            )
            ppp_count = _int_value(
                _snapshot_value(
                    client, device, f"{base}.WANPPPConnectionNumberOfEntries"
                )
            )
            details = _igd_wan_details(client, device, root, idx)
            declared = idx <= count
            if (
                not declared
                and ip_count <= 0
                and ppp_count <= 0
                and not any(details.values())
            ):
                continue
            connections.append(
                {
                    "index": idx,
                    "declared": declared,
                    "ip_entries": ip_count,
                    "ppp_entries": ppp_count,
                    **details,
                }
            )
            if ppp_count > 0:
                wan_capabilities["has_ppp_wan"] = True
                wan_capabilities["supports_tr069_set_ppp_credentials"] = True
        wan_capabilities["connections"] = connections
        wan_capabilities["requires_precreated_ppp_wan"] = not bool(
            wan_capabilities["has_ppp_wan"]
        )
    else:
        ppp_indexes: list[int] = []
        ip_indexes: list[int] = []
        vlan_indexes: list[int] = []
        for idx in range(1, 9):
            if (
                _snapshot_value(client, device, f"Device.PPP.Interface.{idx}.Enable")
                is not None
            ):
                ppp_indexes.append(idx)
            if (
                _snapshot_value(client, device, f"Device.IP.Interface.{idx}.Enable")
                is not None
            ):
                ip_indexes.append(idx)
            if (
                _snapshot_value(
                    client, device, f"Device.Ethernet.VLANTermination.{idx}.VLANID"
                )
                is not None
            ):
                vlan_indexes.append(idx)
        wan_capabilities.update(
            {
                "ppp_interfaces": ppp_indexes,
                "ip_interfaces": ip_indexes,
                "vlan_terminations": vlan_indexes,
                "has_ppp_wan": bool(ppp_indexes),
                "supports_tr069_set_ppp_credentials": bool(ppp_indexes),
                "requires_precreated_ppp_wan": not bool(ppp_indexes),
            }
        )

    capabilities["wan"] = wan_capabilities
    _persist_runtime_capabilities(ont, capabilities)
    db.flush()
    return ActionResult(
        success=True,
        message="WAN capabilities probed.",
        data={"capabilities": capabilities},
    )


def _missing_igd_ppp_service_result(
    *,
    client: Any,
    device: dict[str, Any],
    root: str,
    instance_index: int,
    wan_vlan: int | None = None,
    message_prefix: str,
) -> ActionResult:
    details = _igd_wan_details(client, device, root, instance_index)
    detected_name = details.get("detected_wan_name")
    detected_status = details.get("detected_wan_status")
    detected_ip = details.get("detected_wan_ip")
    detected_service = details.get("detected_wan_service")
    detected_vlan = details.get("detected_wan_vlan")
    return ActionResult(
        success=False,
        message=(
            f"{message_prefix} Detected current WAN service: "
            f"{detected_name or detected_service or 'unknown'} "
            f"status={detected_status or 'unknown'} ip={detected_ip or 'none'} "
            f"vlan={detected_vlan or 'unknown'}."
        ),
        data={
            "missing_ppp_wan_service": True,
            "required_step": "provision_wan_service_instance",
            "wan_instance": instance_index,
            "wan_vlan": wan_vlan,
            **details,
        },
    )


def _waiting_for_igd_ppp_service_result(
    *,
    client: Any,
    device: dict[str, Any],
    root: str,
    instance_index: int,
    wan_vlan: int | None,
    message_prefix: str,
) -> ActionResult:
    """Return a hard failure for PPPoE WAN creation verification.

    Under sync-only provisioning semantics, we fail hard rather than waiting
    for the next inform/refresh cycle. The caller should retry provisioning
    once the device state has been refreshed.
    """
    details = _igd_wan_details(client, device, root, instance_index)
    return ActionResult(
        success=False,
        waiting=False,  # Sync-only: fail hard, no deferred verification
        message=(
            f"{message_prefix} PPPoE WAN service not visible after creation "
            "request. Retry provisioning once device state has refreshed."
        ),
        data={
            "failure_reason": "ppp_wan_add_object_not_visible",
            "retry_after_seconds": _PPP_ADD_OBJECT_VERIFY_DELAY_SECONDS,
            "missing_ppp_wan_service": True,
            "required_step": "verify_ppp_wan_add_object",
            "wan_instance": instance_index,
            "wan_vlan": wan_vlan,
            **details,
        },
    )


def _igd_ppp_container_conflict(
    *,
    details: dict[str, Any],
    wan_vlan: int | None,
) -> str | None:
    service = str(details.get("detected_wan_service") or "").upper()
    detected_vlan = str(details.get("detected_wan_vlan") or "").strip()
    requested_vlan = str(wan_vlan or "").strip()
    if service == "TR069":
        return "the selected WANConnectionDevice is the TR-069 management WAN"
    if requested_vlan and detected_vlan and detected_vlan != requested_vlan:
        return (
            f"the selected WANConnectionDevice is VLAN {detected_vlan}, "
            f"not requested PPPoE VLAN {requested_vlan}"
        )
    return None


def _igd_wan_container_is_blank(
    *,
    details: dict[str, Any],
    ip_count: int,
    ppp_count: int,
) -> bool:
    if ip_count > 0 or ppp_count > 0:
        return False
    observed = (
        "detected_wan_name",
        "detected_wan_status",
        "detected_wan_ip",
        "detected_wan_service",
        "detected_wan_vlan",
        "detected_ppp_name",
        "detected_ppp_status",
        "detected_ppp_ip",
        "detected_ppp_service",
        "detected_ppp_vlan",
        "detected_ppp_username",
    )
    return not any(details.get(key) not in (None, "") for key in observed)


def _ensure_igd_ppp_wan_service(
    client: Any,
    device_id: str,
    *,
    ont: Any,
    root: str,
    instance_index: int,
    wan_vlan: int | None,
) -> ActionResult | None:
    device = client.get_device(device_id)
    entries_path = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}."
        "WANPPPConnectionNumberOfEntries"
    )
    ip_entries_path = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}."
        "WANIPConnectionNumberOfEntries"
    )
    ppp_count = _int_value(client.extract_parameter_value(device, entries_path))
    ip_count = _int_value(client.extract_parameter_value(device, ip_entries_path))
    details = _igd_wan_details(client, device, root, instance_index)
    if ppp_count >= 1:
        conflict = _igd_ppp_container_conflict(details=details, wan_vlan=wan_vlan)
        if conflict:
            return _missing_igd_ppp_service_result(
                client=client,
                device=device,
                root=root,
                instance_index=instance_index,
                wan_vlan=wan_vlan,
                message_prefix=(
                    f"Refusing to push PPPoE credentials because {conflict}."
                ),
            )
        return None
    if wan_vlan is None:
        return _missing_igd_ppp_service_result(
            client=client,
            device=device,
            root=root,
            instance_index=instance_index,
            wan_vlan=wan_vlan,
            message_prefix=(
                "No PPP WAN service exists on this ONT and no Internet VLAN was "
                "provided. Configure the PPPoE WAN service with its VLAN first, "
                "then push credentials."
            ),
        )
    conflict = _igd_ppp_container_conflict(details=details, wan_vlan=wan_vlan)
    if conflict or not _igd_wan_container_is_blank(
        details=details,
        ip_count=ip_count,
        ppp_count=ppp_count,
    ):
        reason = conflict or "the selected WANConnectionDevice is not empty"
        return _missing_igd_ppp_service_result(
            client=client,
            device=device,
            root=root,
            instance_index=instance_index,
            wan_vlan=wan_vlan,
            message_prefix=(
                "No PPP WAN service exists on this ONT. Refusing to create one "
                f"because {reason}."
            ),
        )

    object_path = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}.WANPPPConnection."
    )
    if _pending_ppp_add_object(ont, instance_index, wan_vlan):
        refresh = getattr(client, "refresh_object", None)
        if callable(refresh):
            try:
                refresh(device_id, object_path)
            except GenieACSError:
                logger.debug(
                    "PPP WAN pending verification refresh failed for %s",
                    device_id,
                    exc_info=True,
                )
        return _waiting_for_igd_ppp_service_result(
            client=client,
            device=device,
            root=root,
            instance_index=instance_index,
            wan_vlan=wan_vlan,
            message_prefix=(
                "A PPPoE WAN creation task is already pending for this ONT."
            ),
        )

    try:
        client.add_object(device_id, object_path)
        _mark_ppp_add_object_pending(
            ont,
            root=root,
            instance_index=instance_index,
            wan_vlan=wan_vlan,
            object_path=object_path,
        )
        refresh = getattr(client, "refresh_object", None)
        if callable(refresh):
            refresh(device_id, object_path)
        refreshed = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(
            success=False,
            message=f"Failed to create PPPoE WAN service on ONT: {exc}",
            data={
                "missing_ppp_wan_service": True,
                "required_step": "provision_wan_service_instance",
                "wan_instance": instance_index,
                "wan_vlan": wan_vlan,
            },
        )

    refreshed_entries = client.extract_parameter_value(refreshed, entries_path)
    try:
        refreshed_count = int(refreshed_entries or 0)
    except (TypeError, ValueError):
        refreshed_count = 0
    if refreshed_count < 1:
        return _waiting_for_igd_ppp_service_result(
            client=client,
            device=refreshed,
            root=root,
            instance_index=instance_index,
            wan_vlan=wan_vlan,
            message_prefix=(
                "GenieACS accepted the PPPoE WAN service creation task, but no "
                "PPP WAN service was visible on immediate readback."
            ),
        )
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.get("pending_actions")
    if isinstance(pending, dict):
        pending.pop("add_ppp_wan", None)
    wan = capabilities.setdefault("wan", {})
    wan["has_ppp_wan"] = True
    wan["supports_tr069_add_ppp_wan"] = True
    wan["supports_tr069_set_ppp_credentials"] = True
    wan["requires_precreated_ppp_wan"] = False
    _persist_runtime_capabilities(ont, capabilities)
    return None


def _normalized_serial_expr(column):
    expr = func.upper(column)
    for token in ("-", " ", ":", ".", "_", "/"):
        expr = func.replace(expr, token, "")
    return expr


def _normalize_serial(value: str | None) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _validate_ipv4(value: str, field_name: str) -> str | ActionResult:
    try:
        return str(IPv4Address(value))
    except ValueError:
        return ActionResult(
            success=False, message=f"{field_name} must be a valid IPv4 address."
        )


def _validate_subnet_mask(value: str) -> str | ActionResult:
    try:
        IPv4Network(f"0.0.0.0/{value}")
    except ValueError:
        return ActionResult(success=False, message="LAN subnet mask must be valid.")
    return str(IPv4Address(value))


def _request_lan_refresh(client: Any, device_id: str, root: str) -> None:
    refresh = getattr(client, "refresh_object", None)
    if not callable(refresh):
        return
    path = _LAN_CONFIG_PATHS[root]["refresh"]
    try:
        refresh(device_id, f"{root}.{path}", connection_request=True)
    except Exception:
        logger.debug(
            "Runtime refresh request failed for device %s after LAN config update",
            device_id,
            exc_info=True,
        )


def _split_dns_servers(value: Any) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    normalized = raw.replace(";", ",").replace(" ", ",")
    return [part.strip().lower() for part in normalized.split(",") if part.strip()]


def _dns_values_equal(cache_value: Any, requested: str) -> bool:
    return _split_dns_servers(cache_value) == _split_dns_servers(requested)


def _verify_dns_readback(
    client: Any,
    device_id: str,
    expected: dict[str, str],
) -> None:
    mismatches: list[str] = []
    for path, want in expected.items():
        got, _ = read_param_from_cache(client, device_id, path)
        if _dns_values_equal(got, want):
            continue
        mismatches.append(f"{path}: expected={want!r} got={got!r}")
    if mismatches:
        raise GenieACSError(
            "Device did not apply DNS setParameterValues: " + "; ".join(mismatches)
        )


def _validate_tr181_pppoe_stack(
    client: Any,
    device_id: str,
    *,
    instance_index: int,
    wan_vlan: int | None = None,
) -> ActionResult | None:
    device = client.get_device(device_id)
    ppp_prefix = f"Device.PPP.Interface.{instance_index}"
    ip_prefix = f"Device.IP.Interface.{instance_index}"
    vlan_prefix = f"Device.Ethernet.VLANTermination.{instance_index}"
    ppp_lower_expected = f"Ethernet.VLANTermination.{instance_index}"
    ip_lower_expected = f"PPP.Interface.{instance_index}"

    ppp_enable = client.extract_parameter_value(device, f"{ppp_prefix}.Enable")
    ppp_lower = client.extract_parameter_value(device, f"{ppp_prefix}.LowerLayers")
    ip_enable = client.extract_parameter_value(device, f"{ip_prefix}.Enable")
    ip_lower = client.extract_parameter_value(device, f"{ip_prefix}.LowerLayers")
    vlan_id = client.extract_parameter_value(device, f"{vlan_prefix}.VLANID")

    ppp_exists = ppp_enable is not None or ppp_lower is not None
    ip_exists = ip_enable is not None or ip_lower is not None
    vlan_exists = vlan_id is not None
    if not ppp_exists or not ip_exists or not vlan_exists:
        return ActionResult(
            success=False,
            message=(
                "No complete TR-181 routed PPPoE WAN stack exists on this ONT. "
                "Create the PPPoE WAN service first using the OLT/OMCI "
                "provisioning step, then push credentials."
            ),
            data={
                "missing_ppp_wan_service": True,
                "required_step": "push_pppoe_omci",
                "wan_instance": instance_index,
                "wan_vlan": wan_vlan,
                "ppp_interface_exists": ppp_exists,
                "ip_interface_exists": ip_exists,
                "vlan_termination_exists": vlan_exists,
            },
        )
    if wan_vlan is not None and str(vlan_id).strip() != str(wan_vlan):
        return ActionResult(
            success=False,
            message=(
                "TR-181 WAN VLAN does not match the requested PPPoE service: "
                f"expected VLAN {wan_vlan}, got {vlan_id}."
            ),
            data={
                "wan_instance": instance_index,
                "topology_mismatch": True,
                "expected_vlan": wan_vlan,
                "actual_vlan": vlan_id,
            },
        )

    if ppp_lower and str(ppp_lower).strip() != ppp_lower_expected:
        return ActionResult(
            success=False,
            message=(
                "TR-181 PPP interface lower-layer topology does not match the "
                f"requested WAN instance: expected {ppp_lower_expected}, got {ppp_lower}."
            ),
            data={"wan_instance": instance_index, "topology_mismatch": True},
        )
    if ip_lower and str(ip_lower).strip() != ip_lower_expected:
        return ActionResult(
            success=False,
            message=(
                "TR-181 IP interface lower-layer topology does not match the "
                f"requested WAN instance: expected {ip_lower_expected}, got {ip_lower}."
            ),
            data={"wan_instance": instance_index, "topology_mismatch": True},
        )
    return None


def _resolve_ont_connection_request_auth(
    db: Session,
    serial_number: str | None,
) -> tuple[str, str] | None:
    server: Tr069AcsServer | None = None

    cpe = None
    serial = str(serial_number or "").strip()
    if serial:
        normalized_serial = _normalize_serial(serial)
        cpe = db.scalars(
            select(CPEDevice)
            .where(
                _normalized_serial_expr(CPEDevice.serial_number) == normalized_serial
            )
            .order_by(CPEDevice.updated_at.desc(), CPEDevice.created_at.desc())
            .limit(1)
        ).first()
    linked = None
    if cpe is not None:
        linked = db.scalars(
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.cpe_device_id == cpe.id)
            .where(Tr069CpeDevice.is_active.is_(True))
            .order_by(
                Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc()
            )
            .limit(1)
        ).first()
    if linked is None and serial:
        normalized_serial = _normalize_serial(serial)
        linked = db.scalars(
            select(Tr069CpeDevice)
            .where(
                _normalized_serial_expr(Tr069CpeDevice.serial_number)
                == normalized_serial
            )
            .where(Tr069CpeDevice.is_active.is_(True))
            .order_by(
                Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc()
            )
            .limit(1)
        ).first()
    if linked and linked.acs_server_id:
        server = db.get(Tr069AcsServer, linked.acs_server_id)
    if server is None:
        default_server_id = resolve_value(
            db, SettingDomain.tr069, "default_acs_server_id"
        )
        if default_server_id:
            server = db.get(Tr069AcsServer, str(default_server_id))
    if not server:
        return None
    username = str(server.connection_request_username or "").strip()
    password = decrypt_credential(server.connection_request_password) or ""
    if not username and not password:
        return None
    return username, password


def set_lan_config(
    db: Session,
    ont_id: str,
    *,
    lan_ip: str | None = None,
    lan_subnet: str | None = None,
    dhcp_enabled: bool | None = None,
    dhcp_start: str | None = None,
    dhcp_end: str | None = None,
) -> ActionResult:
    """Set ONT LAN gateway and DHCP server settings via TR-069."""
    if (
        not lan_ip
        and not lan_subnet
        and dhcp_enabled is None
        and not dhcp_start
        and not dhcp_end
    ):
        return ActionResult(
            success=False,
            message="At least one LAN or DHCP setting is required.",
        )

    params_to_set: dict[str, str] = {}
    if lan_ip:
        normalized_ip = _validate_ipv4(str(lan_ip).strip(), "LAN IP address")
        if isinstance(normalized_ip, ActionResult):
            return normalized_ip
        params_to_set["ip_address"] = normalized_ip
    if lan_subnet:
        normalized_mask = _validate_subnet_mask(str(lan_subnet).strip())
        if isinstance(normalized_mask, ActionResult):
            return normalized_mask
        params_to_set["subnet_mask"] = normalized_mask
    if dhcp_enabled is not None:
        params_to_set["dhcp_enabled"] = "true" if dhcp_enabled else "false"
    if dhcp_start:
        normalized_start = _validate_ipv4(str(dhcp_start).strip(), "DHCP start address")
        if isinstance(normalized_start, ActionResult):
            return normalized_start
        params_to_set["dhcp_min_address"] = normalized_start
    if dhcp_end:
        normalized_end = _validate_ipv4(str(dhcp_end).strip(), "DHCP end address")
        if isinstance(normalized_end, ActionResult):
            return normalized_end
        params_to_set["dhcp_max_address"] = normalized_end

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    path_map = _LAN_CONFIG_PATHS[root]
    tr069_params = build_tr069_params(
        root,
        {path_map[key]: value for key, value in params_to_set.items()},
    )

    try:
        result = set_and_verify(client, device_id, tr069_params)
        _request_lan_refresh(client, device_id, root)
        logger.info(
            "LAN config set on ONT %s (root=%s, params=%s)",
            ont.serial_number,
            root,
            sorted(tr069_params),
        )
        changed = []
        if "ip_address" in params_to_set:
            changed.append(f"IP {params_to_set['ip_address']}")
        if "subnet_mask" in params_to_set:
            changed.append(f"subnet {params_to_set['subnet_mask']}")
        if "dhcp_enabled" in params_to_set:
            changed.append(
                "DHCP enabled"
                if params_to_set["dhcp_enabled"] == "true"
                else "DHCP disabled"
            )
        if "dhcp_min_address" in params_to_set:
            changed.append(f"DHCP start {params_to_set['dhcp_min_address']}")
        if "dhcp_max_address" in params_to_set:
            changed.append(f"DHCP end {params_to_set['dhcp_max_address']}")
        return ActionResult(
            success=True,
            message=f"LAN config updated on {ont.serial_number}: {', '.join(changed)}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Set LAN config failed for ONT %s: %s", ont.serial_number, exc)
        return genieacs_error_result(exc, "Failed to set LAN config")


def set_connection_request_credentials(
    db: Session,
    ont_id: str,
    username: str,
    password: str,
    *,
    periodic_inform_interval: int = settings.tr069_periodic_inform_interval,
) -> ActionResult:
    """Set TR-069 Connection Request credentials and periodic inform interval."""
    if not username:
        return ActionResult(
            success=False, message="Connection request username is required."
        )
    if not password:
        return ActionResult(
            success=False, message="Connection request password is required."
        )

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)
    params = build_tr069_params(
        root,
        {
            "ManagementServer.ConnectionRequestUsername": username,
            "ManagementServer.ConnectionRequestPassword": password,
            "ManagementServer.PeriodicInformInterval": periodic_inform_interval,
        },
    )
    try:
        expected = {
            path: value
            for path, value in params.items()
            if not path.endswith(".ConnectionRequestPassword")
        }
        result = set_and_verify(client, device_id, params, expected=expected)
        logger.info(
            "Connection request credentials set on ONT %s (user: %s, root: %s)",
            ont.serial_number,
            username,
            root,
        )
        return ActionResult(
            success=True,
            message=f"Connection request credentials set on {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Set connection request credentials failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return genieacs_error_result(
            exc, "Failed to set connection request credentials"
        )


def send_connection_request(db: Session, ont_id: str) -> ActionResult:
    """Ask GenieACS to send a connection request to the ONT.

    The request must originate from the ACS routing namespace. Sending the HTTP
    request directly from the app container can fail for management VLANs that
    are only routed from the ACS/host network.
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(success=False, message=f"Failed to fetch device: {exc}")

    conn_url = client.extract_parameter_value(
        device, f"{root}.ManagementServer.ConnectionRequestURL"
    )
    if not conn_url:
        return ActionResult(
            success=False,
            message="No ConnectionRequestURL found on device — ONT may not have bootstrapped yet.",
        )

    # Keep this validation because GenieACS cannot initiate an on-demand session
    # until the CPE has reported a connection request URL.
    server_auth = _resolve_ont_connection_request_auth(db, ont.serial_number)
    if not server_auth:
        return ActionResult(
            success=False,
            message="No connection request credentials configured on ACS server.",
        )

    try:
        client.create_task_and_wait(
            device_id,
            {
                "name": "refreshObject",
                "objectName": f"{root}.ManagementServer.",
            },
            connection_request=True,
            timeout_sec=30,
            delete_on_timeout=True,
        )
        logger.info(
            "Connection request sent to ONT %s via GenieACS at %s",
            ont.serial_number,
            conn_url,
        )
        return ActionResult(
            success=True,
            message=f"Connection request sent to {ont.serial_number} via GenieACS.",
        )
    except GenieACSError as exc:
        logger.error("Connection request failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Connection request failed: {exc}")


def send_connection_request_tracked(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
) -> ActionResult:
    """Tracked wrapper around send_connection_request.

    Creates a NetworkOperation record to track the action lifecycle,
    then delegates to the existing function.
    """
    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    try:
        op = network_operations.start(
            db,
            NetworkOperationType.ont_send_conn_request,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=f"ont_conn_req:{ont_id}",
            initiated_by=initiated_by,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return ActionResult(
                success=False,
                message="A connection request is already in progress for this ONT.",
            )
        raise
    network_operations.mark_running(db, str(op.id))
    db.flush()

    try:
        result = send_connection_request(db, ont_id)
        try:
            if result.success:
                network_operations.mark_succeeded(
                    db, str(op.id), output_payload=result.data
                )
            else:
                network_operations.mark_failed(db, str(op.id), result.message)
        except Exception as track_err:
            logger.error(
                "Failed to record operation outcome for %s: %s", op.id, track_err
            )
        return result
    except Exception as exc:
        try:
            network_operations.mark_failed(db, str(op.id), str(exc))
        except Exception as track_err:
            logger.error(
                "Failed to record operation failure for %s: %s (original: %s)",
                op.id,
                track_err,
                exc,
            )
            db.rollback()
        raise
