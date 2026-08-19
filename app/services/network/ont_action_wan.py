"""WAN configuration actions for ONTs via TR-069.

This module handles WAN-related TR-069 actions including:
- PPPoE credential configuration
- WAN mode switching (PPPoE, DHCP, Static, Bridge)
- WAN instance creation via addObject
- IPv6 configuration
- HTTP management toggle

Factory-fresh ONTs lack WANPPPConnection instances. setParameterValues silently
no-ops when the target object doesn't exist. This module calls addObject first
to create instances before configuring them.

Usage::

    from app.services.network.ont_action_wan import set_pppoe_credentials

    result = set_pppoe_credentials(
        db, ont_id,
        username="100014919",
        password="secret123",
        instance_index=1,
        wan_vlan=100,
    )
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.services.genieacs_client import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    detect_data_model_root,
    genieacs_error_result,
    get_ont_client_or_error,
    persist_data_model_root,
    set_and_verify,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTERNET_PPP_CONNECTION_NAME = "DOTMAC_INTERNET_PPP"

# Time to wait before verifying addObject result
_WAN_ADD_OBJECT_VERIFY_DELAY_SECONDS = 60
# How long to consider a pending addObject operation valid
_WAN_ADD_OBJECT_PENDING_TTL_SECONDS = 10 * 60

# WAN connection type mappings for TR-098
_IGD_CONNECTION_TYPE_BY_MODE = {
    "pppoe": "IP_Routed",
    "dhcp": "IP_Routed",
    "static": "IP_Routed",
    "bridge": "IP_Bridged",
}

# Object container paths for addObject operations
_WAN_OBJECT_CONTAINERS = {
    "InternetGatewayDevice": {
        "ppp": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.",
        "ip": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.",
    },
    "Device": {
        "ppp": "PPP.Interface.",
        "ip": "IP.Interface.",
        "vlan": "Ethernet.VLANTermination.",
    },
}

# TR-098 WAN parameter paths
_IGD_WAN_PATHS = {
    "ppp.username": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Username",
    "ppp.password": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Password",
    "ppp.enable": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Enable",
    "ppp.nat_enabled": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.NATEnabled",
    "ppp.connection_type": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.ConnectionType",
    "ppp.vlan": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.X_HW_VLAN",
    "ppp.service_list": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.X_HW_SERVICELIST",
    "ppp.name": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Name",
    "ppp.num_entries": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnectionNumberOfEntries",
    "ip.enable": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.Enable",
    "ip.address": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.ExternalIPAddress",
    "ip.subnet": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.SubnetMask",
    "ip.gateway": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.DefaultGateway",
    "ip.dns": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.DNSServers",
    "ip.nat_enabled": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.NATEnabled",
    "ip.connection_type": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.ConnectionType",
    "ip.addressing_type": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.AddressingType",
    "ip.num_entries": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnectionNumberOfEntries",
}

# TR-181 WAN parameter paths
_TR181_WAN_PATHS = {
    "ppp.username": "PPP.Interface.{i}.Username",
    "ppp.password": "PPP.Interface.{i}.Password",
    "ppp.enable": "PPP.Interface.{i}.Enable",
    "ppp.connection_trigger": "PPP.Interface.{i}.ConnectionTrigger",
    "ppp.lower_layers": "PPP.Interface.{i}.LowerLayers",
    "ip.enable": "IP.Interface.{i}.Enable",
    "ip.type": "IP.Interface.{i}.Type",
    "ip.lower_layers": "IP.Interface.{i}.LowerLayers",
    "ip.ipv4_enable": "IP.Interface.{i}.IPv4Enable",
    "ip.ipv6_enable": "IP.Interface.{i}.IPv6Enable",
    "ip.static_address": "IP.Interface.{i}.IPv4Address.1.IPAddress",
    "ip.static_subnet": "IP.Interface.{i}.IPv4Address.1.SubnetMask",
    "vlan.id": "Ethernet.VLANTermination.{i}.VLANID",
    "vlan.enable": "Ethernet.VLANTermination.{i}.Enable",
    "dhcp.enable": "DHCPv4.Client.{i}.Enable",
    "dhcpv6.enable": "DHCPv6.Client.{i}.Enable",
    "dhcpv6.request_prefixes": "DHCPv6.Client.{i}.RequestPrefixes",
}

# HTTP/Web management paths (vendor-specific, typically Huawei)
_HTTP_MGMT_PATHS = {
    "Device": {
        "enable": "X_HW_UserInterface.WebUIEnable",
        "port": "X_HW_UserInterface.WebUIPort",
    },
    "InternetGatewayDevice": {
        "enable": "X_HW_UserInterface.WebUIEnable",
        "port": "X_HW_UserInterface.WebUIPort",
    },
}


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    """Parse ISO 8601 timestamp string."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int_value(value: Any) -> int:
    """Convert value to int, defaulting to 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _runtime_capabilities(ont: Any) -> dict[str, Any]:
    """Get runtime capabilities from ONT snapshot."""
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        snapshot = {}
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    return capabilities


def _persist_runtime_capabilities(ont: Any, capabilities: dict[str, Any]) -> None:
    """Persist runtime capabilities to ONT snapshot."""
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        snapshot = {}
    else:
        snapshot = dict(snapshot)
    snapshot["capabilities"] = capabilities
    ont.tr069_last_snapshot = snapshot
    flag_modified(ont, "tr069_last_snapshot")
    ont.tr069_last_snapshot_at = datetime.now(UTC)


def _resolve_wan_path(
    root: str,
    path_key: str,
    instance_index: int = 1,
) -> str:
    """Resolve a WAN parameter path for the given data model root."""
    if root == "Device":
        path_map = _TR181_WAN_PATHS
    else:
        path_map = _IGD_WAN_PATHS

    template = path_map.get(path_key)
    if not template:
        raise ValueError(f"Unknown WAN path key: {path_key}")

    suffix = template.replace("{i}", str(instance_index))
    return f"{root}.{suffix}"


def _get_wan_object_container(
    root: str,
    wan_type: str,
    instance_index: int = 1,
) -> str:
    """Get the object container path for addObject."""
    containers = _WAN_OBJECT_CONTAINERS.get(root, {})
    template = containers.get(wan_type)
    if not template:
        raise ValueError(f"Unknown WAN type: {wan_type} for root: {root}")
    return f"{root}.{template.replace('{i}', str(instance_index))}"


def _pending_wan_add_object(
    ont: Any,
    instance_index: int,
    wan_type: str,
    wan_vlan: int | None,
) -> bool:
    """Check if a WAN addObject operation is pending verification."""
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.get("pending_actions")
    if not isinstance(pending, dict):
        return False

    pending_key = f"add_{wan_type}_wan"
    add_action = pending.get(pending_key)
    if not isinstance(add_action, dict):
        return False

    if add_action.get("state") != "pending_verification":
        return False
    if int(add_action.get("instance_index") or 0) != instance_index:
        return False
    if str(add_action.get("wan_vlan") or "") != str(wan_vlan or ""):
        return False

    requested_at = _parse_iso(add_action.get("requested_at"))
    if requested_at is None:
        return False

    return datetime.now(UTC) - requested_at < timedelta(
        seconds=_WAN_ADD_OBJECT_PENDING_TTL_SECONDS
    )


def _mark_wan_add_object_pending(
    ont: Any,
    *,
    wan_type: str,
    root: str,
    instance_index: int,
    wan_vlan: int | None,
    object_path: str,
) -> None:
    """Mark a WAN addObject operation as pending verification."""
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.setdefault("pending_actions", {})
    pending_key = f"add_{wan_type}_wan"
    pending[pending_key] = {
        "state": "pending_verification",
        "requested_at": _iso_now(),
        "root": root,
        "instance_index": instance_index,
        "wan_vlan": wan_vlan,
        "object_path": object_path,
    }
    wan = capabilities.setdefault("wan", {})
    wan[f"supports_tr069_add_{wan_type}_wan"] = "pending_verification"
    _persist_runtime_capabilities(ont, capabilities)


def _clear_wan_add_object_pending(
    ont: Any,
    wan_type: str,
    *,
    success: bool = True,
) -> None:
    """Clear pending WAN addObject state after verification."""
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.get("pending_actions")
    if isinstance(pending, dict):
        pending.pop(f"add_{wan_type}_wan", None)
        if not pending:
            capabilities.pop("pending_actions", None)

    wan = capabilities.setdefault("wan", {})
    if success:
        wan[f"has_{wan_type}_wan"] = True
        wan[f"supports_tr069_add_{wan_type}_wan"] = True
        wan[f"supports_tr069_set_{wan_type}_credentials"] = True
        wan[f"requires_precreated_{wan_type}_wan"] = False
    else:
        wan[f"supports_tr069_add_{wan_type}_wan"] = False
        wan[f"requires_precreated_{wan_type}_wan"] = True

    _persist_runtime_capabilities(ont, capabilities)


def _igd_wan_details(
    client: Any,
    device: dict[str, Any],
    root: str,
    instance_index: int,
) -> dict[str, Any]:
    """Extract WAN details from an IGD device."""
    ppp_base = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}.WANPPPConnection.1"
    )
    ip_base = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}.WANIPConnection.1"
    )

    def _val(path: str) -> Any:
        return client.extract_parameter_value(device, path)

    return {
        "ppp_name": _val(f"{ppp_base}.Name"),
        "ppp_status": _val(f"{ppp_base}.ConnectionStatus"),
        "ppp_ip": _val(f"{ppp_base}.ExternalIPAddress"),
        "ppp_username": _val(f"{ppp_base}.Username"),
        "ppp_service": _val(f"{ppp_base}.X_HW_SERVICELIST"),
        "ppp_vlan": _val(f"{ppp_base}.X_HW_VLAN"),
        "ip_name": _val(f"{ip_base}.Name"),
        "ip_status": _val(f"{ip_base}.ConnectionStatus"),
        "ip_address": _val(f"{ip_base}.ExternalIPAddress"),
        "ip_service": _val(f"{ip_base}.X_HW_SERVICELIST"),
        "ip_vlan": _val(f"{ip_base}.X_HW_VLAN"),
    }


def _igd_ppp_container_conflict(
    details: dict[str, Any],
    wan_vlan: int | None,
) -> str | None:
    """Check if the selected WAN container conflicts with the requested config."""
    service = str(details.get("ppp_service") or details.get("ip_service") or "").upper()
    detected_vlan = str(details.get("ppp_vlan") or details.get("ip_vlan") or "").strip()
    requested_vlan = str(wan_vlan or "").strip()

    if service == "TR069":
        return "the selected WANConnectionDevice is the TR-069 management WAN"
    if (
        requested_vlan
        and detected_vlan
        and detected_vlan != requested_vlan
        and service not in {"", "INTERNET"}
    ):
        return (
            f"the selected WANConnectionDevice is VLAN {detected_vlan}, "
            f"not requested PPPoE VLAN {requested_vlan}"
        )
    return None


def _resolve_igd_wcd_index(device: dict[str, Any], requested_index: int) -> int:
    """Use the requested WCD when present, otherwise use ACS's live WCD.

    Huawei config packs describe the intended layout, but the ONT's GenieACS
    document is authoritative for the object path that can receive a write.
    This matters when a device exposes management and Internet objects under a
    different WCD index than the OLT profile default.
    """
    wan_device = (
        device.get("InternetGatewayDevice", {}).get("WANDevice", {})
        if isinstance(device.get("InternetGatewayDevice"), dict)
        else {}
    )
    if not isinstance(wan_device, dict):
        return requested_index
    wcd_indexes: list[int] = []
    for wan_device_value in wan_device.values():
        if not isinstance(wan_device_value, dict):
            continue
        wcd = wan_device_value.get("WANConnectionDevice", {})
        if not isinstance(wcd, dict):
            continue
        for key, value in wcd.items():
            if not str(key).isdigit() or not isinstance(value, dict):
                continue
            if value.get("_object") is True or "WANPPPConnection" in value:
                wcd_indexes.append(int(key))
    if requested_index in wcd_indexes:
        return requested_index
    return min(wcd_indexes) if wcd_indexes else requested_index


def _igd_wan_container_is_blank(
    details: dict[str, Any],
    *,
    ip_count: int,
    ppp_count: int,
) -> bool:
    """Return True when the target WCD slot is safe to populate with PPP."""
    if ip_count > 0 or ppp_count > 0:
        return False
    observed = (
        "ppp_name",
        "ppp_status",
        "ppp_ip",
        "ppp_username",
        "ppp_service",
        "ppp_vlan",
        "ip_name",
        "ip_status",
        "ip_address",
        "ip_service",
        "ip_vlan",
    )
    return not any(details.get(key) not in (None, "") for key in observed)


def _get_igd_ppp_instance_indexes(
    device: dict[str, Any],
    root: str,
    wcd_index: int,
) -> list[int]:
    """Return discovered WANPPPConnection child indexes under one WCD slot."""
    node: Any = device
    for segment in (
        root,
        "WANDevice",
        "1",
        "WANConnectionDevice",
        str(wcd_index),
        "WANPPPConnection",
    ):
        if not isinstance(node, dict):
            return []
        node = node.get(segment)
    ppp_root = node
    if not isinstance(ppp_root, dict):
        return []
    return sorted(int(key) for key in ppp_root if str(key).isdigit())


def _get_existing_igd_ppp_instance_index(
    device: dict[str, Any],
    root: str,
    wcd_index: int,
) -> int | None:
    """Prefer the primary/lowest discovered PPP child for existing layouts."""
    digit_keys = _get_igd_ppp_instance_indexes(device, root, wcd_index)
    return min(digit_keys) if digit_keys else None


def _get_newest_igd_ppp_instance_index(
    device: dict[str, Any],
    root: str,
    wcd_index: int,
) -> int | None:
    """Resolve the child created most recently after addObject refresh."""
    digit_keys = _get_igd_ppp_instance_indexes(device, root, wcd_index)
    return max(digit_keys) if digit_keys else None


def _ensure_igd_ppp_wan_service(
    *,
    ont: Any,
    client: Any,
    device_id: str,
    root: str,
    wcd_index: int,
    wan_vlan: int | None,
) -> tuple[int | None, ActionResult | None]:
    """Ensure a writable WANPPPConnection exists for IGD devices.

    Returns the discovered PPP child instance index on success. When creation
    is not possible or the new object is still not visible after refresh, an
    actionable ActionResult is returned instead.
    """
    device = client.get_device(device_id)
    entries_path = (
        f"{root}.WANDevice.1.WANConnectionDevice.{wcd_index}."
        "WANPPPConnectionNumberOfEntries"
    )
    ip_entries_path = (
        f"{root}.WANDevice.1.WANConnectionDevice.{wcd_index}."
        "WANIPConnectionNumberOfEntries"
    )
    ppp_count = _int_value(client.extract_parameter_value(device, entries_path))
    ip_count = _int_value(client.extract_parameter_value(device, ip_entries_path))
    details = _igd_wan_details(client, device, root, wcd_index)
    existing_ppp_index = _get_existing_igd_ppp_instance_index(
        device,
        root,
        wcd_index,
    )

    if ppp_count >= 1 or existing_ppp_index is not None:
        conflict = _igd_ppp_container_conflict(details=details, wan_vlan=wan_vlan)
        if conflict:
            return None, ActionResult(
                success=False,
                message=(f"Refusing to push PPPoE credentials because {conflict}."),
                data={
                    "missing_ppp_wan_service": False,
                    "wan_connection_device_index": wcd_index,
                    "wan_instance": existing_ppp_index,
                    **details,
                },
            )
        _clear_wan_add_object_pending(ont, "ppp", success=True)
        return existing_ppp_index or 1, None

    if wan_vlan is None:
        return None, ActionResult(
            success=False,
            message=(
                "No PPP WAN service exists on this ONT and no Internet VLAN "
                "was provided. Configure the PPPoE WAN service with its VLAN "
                "first, then push credentials."
            ),
            data={
                "missing_ppp_wan_service": True,
                "required_step": "provision_wan_service_instance",
                "wan_connection_device_index": wcd_index,
                "wan_vlan": wan_vlan,
                **details,
            },
        )

    conflict = _igd_ppp_container_conflict(details=details, wan_vlan=wan_vlan)
    if conflict or not _igd_wan_container_is_blank(
        details,
        ip_count=ip_count,
        ppp_count=ppp_count,
    ):
        reason = conflict or "the selected WANConnectionDevice is not empty"
        return None, ActionResult(
            success=False,
            message=(
                "No PPP WAN service exists on this ONT. Refusing to create one "
                f"because {reason}."
            ),
            data={
                "missing_ppp_wan_service": True,
                "required_step": "provision_wan_service_instance",
                "wan_connection_device_index": wcd_index,
                "wan_vlan": wan_vlan,
                **details,
            },
        )

    object_path = _get_wan_object_container(root, "ppp", wcd_index)
    try:
        client.add_object(device_id, object_path)
        _mark_wan_add_object_pending(
            ont,
            wan_type="ppp",
            root=root,
            instance_index=wcd_index,
            wan_vlan=wan_vlan,
            object_path=object_path,
        )
    except GenieACSError as exc:
        return None, ActionResult(
            success=False,
            message=f"Failed to create PPPoE WAN service on ONT: {exc}",
            data={
                "missing_ppp_wan_service": True,
                "required_step": "provision_wan_service_instance",
                "wan_connection_device_index": wcd_index,
                "wan_vlan": wan_vlan,
            },
        )

    refresh = getattr(client, "refresh_object", None)
    for attempt in range(4):
        if callable(refresh):
            try:
                refresh(device_id, object_path, allow_when_pending=True)
            except GenieACSError:
                logger.debug(
                    "PPP WAN refresh failed for %s on attempt %d",
                    device_id,
                    attempt + 1,
                    exc_info=True,
                )
        refreshed = client.get_device(device_id)
        discovered_index = _get_newest_igd_ppp_instance_index(
            refreshed,
            root,
            wcd_index,
        )
        if discovered_index is not None:
            _clear_wan_add_object_pending(ont, "ppp", success=True)
            return discovered_index, None
        if attempt < 3:
            time.sleep(2)

    _clear_wan_add_object_pending(ont, "ppp", success=False)
    return None, ActionResult(
        success=False,
        message=(
            "GenieACS accepted the PPPoE WAN service creation task, but the "
            "new WANPPPConnection was not visible after refresh. Retry "
            "provisioning once device state has refreshed."
        ),
        data={
            "failure_reason": "ppp_wan_add_object_not_visible",
            "retry_after_seconds": _WAN_ADD_OBJECT_VERIFY_DELAY_SECONDS,
            "missing_ppp_wan_service": True,
            "required_step": "verify_ppp_wan_add_object",
            "wan_connection_device_index": wcd_index,
            "wan_vlan": wan_vlan,
        },
    )


# ---------------------------------------------------------------------------
# PPPoE Configuration
# ---------------------------------------------------------------------------


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    instance_index: int = 1,
    wan_vlan: int | None = None,
    connection_name: str = INTERNET_PPP_CONNECTION_NAME,
) -> ActionResult:
    """Set PPPoE credentials on an ONT via TR-069.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        username: PPPoE username.
        password: PPPoE password.
        instance_index: WAN instance index (default 1).
        wan_vlan: Optional VLAN for service tagging.

    Returns:
        ActionResult indicating success/failure.
    """
    if not username:
        return ActionResult(success=False, message="PPPoE username is required.")
    if not password:
        return ActionResult(success=False, message="PPPoE password is required.")

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    params: dict[str, str] = {}

    if root == "InternetGatewayDevice":
        if hasattr(client, "get_device"):
            instance_index = _resolve_igd_wcd_index(
                client.get_device(device_id), instance_index
            )
        ppp_child_index, ensure_error = _ensure_igd_ppp_wan_service(
            ont=ont,
            client=client,
            device_id=device_id,
            root=root,
            wcd_index=instance_index,
            wan_vlan=wan_vlan,
        )
        if ensure_error is not None:
            return ensure_error
        ppp_index = ppp_child_index or 1
        ppp_base = (
            f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}."
            f"WANPPPConnection.{ppp_index}"
        )
        params = {
            f"{ppp_base}.Username": username,
            f"{ppp_base}.Password": password,
            f"{ppp_base}.Enable": "true",
            f"{ppp_base}.NATEnabled": "true",
            f"{ppp_base}.ConnectionType": "IP_Routed",
        }

        if wan_vlan is not None:
            params[f"{ppp_base}.X_HW_VLAN"] = str(wan_vlan)
            params[f"{ppp_base}.X_HW_SERVICELIST"] = "INTERNET"

        if connection_name:
            params[f"{ppp_base}.Name"] = connection_name
    else:
        # Build TR-181 parameter paths
        username_path = _resolve_wan_path(root, "ppp.username", instance_index)
        password_path = _resolve_wan_path(root, "ppp.password", instance_index)
        enable_path = _resolve_wan_path(root, "ppp.enable", instance_index)
        params = {
            username_path: username,
            password_path: password,
            enable_path: "true",
        }

    # Expected values for verification (exclude password - it's write-only)
    expected = {
        path: value for path, value in params.items() if not path.endswith(".Password")
    }

    try:
        result = set_and_verify(client, device_id, params, expected=expected)
        logger.info(
            "PPPoE credentials set on ONT %s (user: %s, instance: %d, root: %s)",
            ont.serial_number,
            username,
            instance_index,
            root,
        )
        return ActionResult(
            success=True,
            message=f"PPPoE credentials set on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "username": username,
                "instance_index": instance_index,
                "connection_name": (
                    connection_name if root == "InternetGatewayDevice" else None
                ),
                "root": root,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set PPPoE credentials failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return genieacs_error_result(exc, "Failed to set PPPoE credentials")


# ---------------------------------------------------------------------------
# DHCP WAN Configuration
# ---------------------------------------------------------------------------


def set_wan_dhcp(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    wan_vlan: int | None = None,
) -> ActionResult:
    """Configure WAN interface for DHCP mode.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        instance_index: WAN instance index (default 1).
        wan_vlan: Optional VLAN to apply.

    Returns:
        ActionResult indicating success/failure.
    """
    _ = wan_vlan  # Reserved for future VLAN tagging support
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    params: dict[str, str] = {}

    if root == "InternetGatewayDevice":
        enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        nat_path = _resolve_wan_path(root, "ip.nat_enabled", instance_index)
        conn_type_path = _resolve_wan_path(root, "ip.connection_type", instance_index)
        addr_type_path = _resolve_wan_path(root, "ip.addressing_type", instance_index)

        params = {
            enable_path: "true",
            nat_path: "true",
            conn_type_path: "IP_Routed",
            addr_type_path: "DHCP",
        }
    else:
        # TR-181
        ip_enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        dhcp_enable_path = _resolve_wan_path(root, "dhcp.enable", instance_index)

        params = {
            ip_enable_path: "true",
            dhcp_enable_path: "true",
        }

    try:
        result = set_and_verify(client, device_id, params)
        logger.info(
            "WAN DHCP configured on ONT %s (instance: %d, root: %s)",
            ont.serial_number,
            instance_index,
            root,
        )
        return ActionResult(
            success=True,
            message=f"WAN DHCP configured on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "instance_index": instance_index,
                "root": root,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set WAN DHCP failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return genieacs_error_result(exc, "Failed to set WAN DHCP")


# ---------------------------------------------------------------------------
# Static IP WAN Configuration
# ---------------------------------------------------------------------------


def set_wan_static(
    db: Session,
    ont_id: str,
    *,
    ip_address: str,
    subnet_mask: str,
    gateway: str,
    dns_servers: str | None = None,
    instance_index: int = 1,
) -> ActionResult:
    """Configure WAN interface with static IP.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ip_address: Static IP address.
        subnet_mask: Subnet mask.
        gateway: Default gateway.
        dns_servers: Optional comma-separated DNS servers.
        instance_index: WAN instance index (default 1).

    Returns:
        ActionResult indicating success/failure.
    """
    if not ip_address:
        return ActionResult(success=False, message="IP address is required.")
    if not subnet_mask:
        return ActionResult(success=False, message="Subnet mask is required.")
    if not gateway:
        return ActionResult(success=False, message="Gateway is required.")

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    params: dict[str, str] = {}

    if root == "InternetGatewayDevice":
        enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        address_path = _resolve_wan_path(root, "ip.address", instance_index)
        subnet_path = _resolve_wan_path(root, "ip.subnet", instance_index)
        gateway_path = _resolve_wan_path(root, "ip.gateway", instance_index)
        nat_path = _resolve_wan_path(root, "ip.nat_enabled", instance_index)
        conn_type_path = _resolve_wan_path(root, "ip.connection_type", instance_index)
        addr_type_path = _resolve_wan_path(root, "ip.addressing_type", instance_index)

        params = {
            enable_path: "true",
            address_path: ip_address,
            subnet_path: subnet_mask,
            gateway_path: gateway,
            nat_path: "true",
            conn_type_path: "IP_Routed",
            addr_type_path: "Static",
        }

        if dns_servers:
            dns_path = _resolve_wan_path(root, "ip.dns", instance_index)
            params[dns_path] = dns_servers
    else:
        # TR-181
        ip_enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        address_path = _resolve_wan_path(root, "ip.static_address", instance_index)
        subnet_path = _resolve_wan_path(root, "ip.static_subnet", instance_index)

        params = {
            ip_enable_path: "true",
            address_path: ip_address,
            subnet_path: subnet_mask,
        }

    try:
        result = set_and_verify(client, device_id, params)
        logger.info(
            "WAN static IP configured on ONT %s (ip: %s, instance: %d, root: %s)",
            ont.serial_number,
            ip_address,
            instance_index,
            root,
        )
        return ActionResult(
            success=True,
            message=f"WAN static IP ({ip_address}) configured on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "ip_address": ip_address,
                "instance_index": instance_index,
                "root": root,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set WAN static IP failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return genieacs_error_result(exc, "Failed to set WAN static IP")


# ---------------------------------------------------------------------------
# Unified WAN Configuration
# ---------------------------------------------------------------------------


def set_wan_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str,
    instance_index: int = 1,
    wan_vlan: int | None = None,
    pppoe_username: str | None = None,
    pppoe_password: str | None = None,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
    dns_servers: str | None = None,
) -> ActionResult:
    """Unified entry point for WAN configuration.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        wan_mode: WAN mode ("pppoe", "dhcp", "static", "bridge").
        instance_index: WAN instance index (default 1).
        wan_vlan: Optional VLAN to apply.
        pppoe_username: PPPoE username (required for pppoe mode).
        pppoe_password: PPPoE password (required for pppoe mode).
        ip_address: Static IP (required for static mode).
        subnet_mask: Subnet mask (required for static mode).
        gateway: Gateway (required for static mode).
        dns_servers: Optional DNS servers.

    Returns:
        ActionResult indicating success/failure.
    """
    wan_mode = wan_mode.lower().strip()

    if wan_mode == "pppoe":
        if not pppoe_username or not pppoe_password:
            return ActionResult(
                success=False,
                message="PPPoE username and password are required for PPPoE mode.",
            )
        return set_pppoe_credentials(
            db,
            ont_id,
            username=pppoe_username,
            password=pppoe_password,
            instance_index=instance_index,
            wan_vlan=wan_vlan,
        )

    elif wan_mode == "dhcp":
        return set_wan_dhcp(
            db,
            ont_id,
            instance_index=instance_index,
            wan_vlan=wan_vlan,
        )

    elif wan_mode == "static":
        if not ip_address or not subnet_mask or not gateway:
            return ActionResult(
                success=False,
                message="IP address, subnet mask, and gateway are required for static mode.",
            )
        return set_wan_static(
            db,
            ont_id,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
            dns_servers=dns_servers,
            instance_index=instance_index,
        )

    elif wan_mode == "bridge":
        # Bridge mode typically doesn't need TR-069 WAN config
        return ActionResult(
            success=True,
            message="Bridge mode does not require TR-069 WAN configuration.",
            data={"wan_mode": "bridge", "skipped": True},
        )

    else:
        return ActionResult(
            success=False,
            message=f"Unknown WAN mode: {wan_mode}. Use pppoe, dhcp, static, or bridge.",
        )


# ---------------------------------------------------------------------------
# IPv6 Configuration
# ---------------------------------------------------------------------------


def set_wan_ipv6_config(
    db: Session,
    ont_id: str,
    *,
    ipv6_enabled: bool = True,
    dhcpv6_pd_enabled: bool = True,
    instance_index: int = 1,
) -> ActionResult:
    """Configure IPv6 settings on WAN interface.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ipv6_enabled: Enable IPv6 on the interface.
        dhcpv6_pd_enabled: Enable DHCPv6 prefix delegation.
        instance_index: WAN instance index (default 1).

    Returns:
        ActionResult indicating success/failure.
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    params: dict[str, str] = {}

    if root == "Device":
        # TR-181 has proper IPv6 support
        ipv6_enable_path = _resolve_wan_path(root, "ip.ipv6_enable", instance_index)
        dhcpv6_enable_path = _resolve_wan_path(root, "dhcpv6.enable", instance_index)
        dhcpv6_pd_path = _resolve_wan_path(
            root, "dhcpv6.request_prefixes", instance_index
        )

        params = {
            ipv6_enable_path: "true" if ipv6_enabled else "false",
            dhcpv6_enable_path: "true" if ipv6_enabled else "false",
            dhcpv6_pd_path: "true" if dhcpv6_pd_enabled else "false",
        }
    else:
        # TR-098 IPv6 support is vendor-specific
        return ActionResult(
            success=False,
            message="IPv6 configuration via TR-069 is not supported on TR-098 devices.",
            data={"root": root, "unsupported": True},
        )

    try:
        result = set_and_verify(client, device_id, params)
        state = "enabled" if ipv6_enabled else "disabled"
        logger.info(
            "IPv6 %s on ONT %s (instance: %d)",
            state,
            ont.serial_number,
            instance_index,
        )
        return ActionResult(
            success=True,
            message=f"IPv6 {state} on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "ipv6_enabled": ipv6_enabled,
                "dhcpv6_pd_enabled": dhcpv6_pd_enabled,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set IPv6 config failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set IPv6 config: {exc}",
        )


# ---------------------------------------------------------------------------
# HTTP Management Toggle
# ---------------------------------------------------------------------------


def set_http_management(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    port: int = 80,
) -> ActionResult:
    """Enable or disable HTTP management interface on ONT.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        enabled: Enable or disable HTTP management.
        port: HTTP port (default 80).

    Returns:
        ActionResult indicating success/failure.
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    http_paths = _HTTP_MGMT_PATHS.get(root, {})
    if not http_paths:
        return ActionResult(
            success=False,
            message="HTTP management paths not configured for this device type.",
        )

    enable_path = f"{root}.{http_paths['enable']}"
    port_path = f"{root}.{http_paths['port']}"

    params = {
        enable_path: "true" if enabled else "false",
        port_path: str(port),
    }

    try:
        result = set_and_verify(client, device_id, params)
        state = "enabled" if enabled else "disabled"
        logger.info(
            "HTTP management %s on ONT %s (port: %d)",
            state,
            ont.serial_number,
            port,
        )
        return ActionResult(
            success=True,
            message=f"HTTP management {state} on {ont.serial_number} (port {port}).",
            data={
                "device_id": device_id,
                "http_enabled": enabled,
                "http_port": port,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set HTTP management failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set HTTP management: {exc}",
        )
