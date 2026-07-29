"""ACS-side reader — GenieACS NBI query into ``AcsObservedFields``.

One ``list_devices`` query per ONT with a focused projection. The reader
trusts the GenieACS CWMP cache — staleness is bounded by the device's
PeriodicInformInterval, and post-write ``VERIFICATION_MISMATCH`` catches the
rare stale-cache case. No ``refreshObject`` round-trip on every read.

The device-id format is ``{OUI}-{ProductClass}-{SerialNumber}``. ``OntUnit``
does not carry OUI/ProductClass, so the *query* uses a trailing-serial regex
match — the same pattern ``GenieACSClient.get_device`` falls back to.

The matched document's ``_id`` is then **observed** into
``AcsObservedFields.acs_observed_device_id`` verbatim, together with the number
of documents that matched. Those two fields are the only ACS-sourced identity
the reconciler is allowed to use; nothing in this package may compose a device
id from a guessed OUI or ProductClass. The fleet is not one model (HG8546M and
EG8145V5 both ship today) and a fabricated ``_id`` is a permanent NBI 404.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from app.services.genieacs_client import GenieACSError

from ..state import AcsObservedFields, OntDesiredState
from ..wifi_paths import wifi_paths_for_instance
from ._types import ReadResult

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


# Projection paths read from the device document. Kept here (rather than
# hardcoded in the parser) so adding a field is a single-line change.
_PROJECTION_PATHS: tuple[str, ...] = (
    "_lastInform",
    "_lastBoot",
    "_lastBootstrap",
    # TR-098 (InternetGatewayDevice) — the HG8546M / EG8145V5 fleet.
    "InternetGatewayDevice.DeviceInfo.SoftwareVersion",
    "InternetGatewayDevice.ManagementServer.PeriodicInformInterval",
    "InternetGatewayDevice.ManagementServer.URL",
    "InternetGatewayDevice.ManagementServer.Username",
    "InternetGatewayDevice.ManagementServer.Password",
    "InternetGatewayDevice.ManagementServer.ConnectionRequestUsername",
    "InternetGatewayDevice.ManagementServer.ConnectionRequestPassword",
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice",
    "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Channel",
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType",
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration",
    "InternetGatewayDevice.X_HW_UserInterface.SSHEnable",
    "InternetGatewayDevice.X_HW_UserInterface.SSHPort",
    "InternetGatewayDevice.X_HW_UserInterface.TelnetEnable",
    "InternetGatewayDevice.X_HW_UserInterface.TelnetPort",
    # TR-181 (Device) — for any future ONTs on that data model.
    "Device.DeviceInfo.SoftwareVersion",
    "Device.ManagementServer.PeriodicInformInterval",
    "Device.ManagementServer.URL",
    "Device.ManagementServer.Username",
    "Device.ManagementServer.Password",
    "Device.ManagementServer.ConnectionRequestUsername",
    "Device.ManagementServer.ConnectionRequestPassword",
    "Device.IP.Interface",
    "Device.DHCPv4.Client",
    "Device.DHCPv6.Client",
    "Device.Routing.Router",
    "Device.DNS.Client.Server",
    "Device.NAT.InterfaceSetting",
    "Device.Ethernet.VLANTermination",
    "Device.RouterAdvertisement.InterfaceSettings",
    "Device.WiFi.SSID.1.Enable",
    "Device.WiFi.SSID.1.SSID",
    "Device.WiFi.Radio.1.Channel",
    "Device.WiFi.AccessPoint.1.Security.ModeEnabled",
    "Device.WiFi",
    "Device.X_HW_UserInterface.SSHEnable",
    "Device.X_HW_UserInterface.SSHPort",
    "Device.X_HW_UserInterface.TelnetEnable",
    "Device.X_HW_UserInterface.TelnetPort",
)


def read_acs_state(
    client: Any,
    desired: OntDesiredState,
    *,
    deadline: datetime | None = None,
) -> ReadResult[AcsObservedFields]:
    """Read the ACS-observed fields for one ONT.

    Args:
        client: A ``GenieACSClient`` (or any object with a ``list_devices``
            method returning a list of device dicts).
        desired: The ONT's desired state. Used to construct the device-id
            match.
        deadline: Optional cutoff. The underlying HTTP client has its own
            timeout; ``reconcile_ont`` enforces the outer budget.

    Returns:
        ``ReadResult[AcsObservedFields]``. When the device hasn't yet
        bootstrapped to ACS, returns ``success=True`` with an
        ``acs_present=False`` observation. That is a clean read, not a
        transport failure: ``compute_plan`` emits no ACS actions for it and
        marks the plan ``acs_wait_reason=ont_not_informing`` so the OLT-side
        mgmt config is still pushed and the ACS half waits for the next Inform.
    """
    query = _query_for_serial(desired.serial_number)
    projection_paths = list(_PROJECTION_PATHS)
    if desired.tr181_wan_paths is not None:
        projection_paths.extend(vars(desired.tr181_wan_paths).values())
    if desired.wifi_paths is not None:
        projection_paths.extend(
            (
                desired.wifi_paths.enabled,
                desired.wifi_paths.ssid,
                desired.wifi_paths.channel,
                desired.wifi_paths.security_mode,
            )
        )
    if desired.remote_access_paths is not None:
        projection_paths.extend(vars(desired.remote_access_paths).values())
    projection = ",".join(dict.fromkeys(projection_paths))

    try:
        devices = client.list_devices(query=query, projection=projection)
    except GenieACSError as exc:
        # The client raises GenieACSError for any non-2xx; treat as
        # unreachable so the precondition layer fast-fails before writes.
        return ReadResult(
            success=False,
            unreachable=True,
            observed=None,
            error=str(exc),
        )
    except Exception as exc:
        # Defensive: log and report unreachable. We don't want a misshaped
        # NBI response to abort an entire reconcile when the cleaner outcome
        # is "OLT side still readable, ACS side blocked, fast-fail".
        logger.warning(
            "acs_reader_unexpected_error",
            extra={"error": str(exc), "serial": desired.serial_number},
        )
        return ReadResult(
            success=False,
            unreachable=True,
            observed=None,
            error=str(exc),
        )

    if not devices:
        # No device matched. That's a clean read; the ONT hasn't informed yet.
        return ReadResult(
            success=True,
            unreachable=False,
            observed=_absent_fields(),
            error=None,
        )

    device = devices[0]
    observed = _parse_device(device, desired, match_count=len(devices))
    if len(devices) > 1:
        # Ambiguous identity. The planner fails closed on this; log once with
        # the ids so an operator can retire the stale ACS document.
        logger.warning(
            "acs_reader_ambiguous_device_match",
            extra={
                "serial": desired.serial_number,
                "match_count": len(devices),
                "device_ids": [str(item.get("_id") or "") for item in devices],
            },
        )

    # Ghost-instance recovery. ACS may have cached ``setParameterValues``
    # writes against a ``WANPPPConnection.<n>`` path that never existed on
    # the device (HG8546M V5R019C10S100 silently no-ops these — no fault is
    # raised). Subsequent reconciles then see Username/Enable/etc. populated
    # and skip the addObject, so PPP never dials. Tell-tale: instance index
    # resolved but ``ConnectionStatus`` has no reported ``_value``. Force a
    # narrow ``refreshObject`` on the affected WCD and re-parse once.
    if _looks_like_ghost_wan_instance(observed) and hasattr(client, "refresh_object"):
        observed = _refresh_and_reparse(
            client, device, observed, query, projection, desired
        )

    return ReadResult(
        success=True,
        unreachable=False,
        observed=observed,
        error=None,
    )


def _looks_like_ghost_wan_instance(observed: AcsObservedFields) -> bool:
    """The reader resolved a WAN PPP instance from the cache, but
    ``ConnectionStatus`` has no ``_value`` — the device never reported PPP
    state for that instance. Strongest single signal that the cached
    parameter values landed on a non-existent CWMP path.
    """
    return (
        observed.acs_observed_wan_instance_index is not None
        and observed.acs_observed_wan_connection_status is None
    )


def _refresh_and_reparse(
    client: Any,
    device: dict[str, Any],
    observed: AcsObservedFields,
    query: dict[str, Any],
    projection: str,
    desired: OntDesiredState,
) -> AcsObservedFields:
    """One-shot refresh of the WCD subtree, then re-fetch + re-parse. Any
    failure falls through with the original observation — the planner has
    its own safety nets and we don't want to break sweeps on a flaky ACS.
    """
    device_id = str(device.get("_id") or "").strip()
    wcd = observed.acs_observed_wan_wcd_index
    if not device_id or wcd is None:
        return observed
    refresh_path = f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{wcd}"
    try:
        client.refresh_object(
            device_id,
            refresh_path,
            allow_when_pending=True,
        )
        refreshed = client.list_devices(query=query, projection=projection)
    except Exception as exc:
        logger.info(
            "acs_reader_ghost_refresh_failed",
            extra={
                "error": str(exc),
                "device_id": device_id,
                "refresh_path": refresh_path,
            },
        )
        return observed
    if not refreshed:
        return observed
    return _parse_device(refreshed[0], desired, match_count=len(refreshed))


# ── Query / parse helpers ───────────────────────────────────────────────────


def _query_for_serial(serial_number: str) -> dict[str, Any]:
    """Build the GenieACS query that matches any device whose ``_id`` ends in
    the given serial. Mirrors ``GenieACSClient.get_device``'s fallback path.
    """
    escaped = re.escape(serial_number)
    return {"_id": {"$regex": f".*-{escaped}$"}}


def _parse_device(
    device: dict[str, Any],
    desired: OntDesiredState | None = None,
    *,
    match_count: int = 1,
) -> AcsObservedFields:
    """Map a GenieACS device document to ``AcsObservedFields``.

    The document is a nested dict where leaves have a ``_value`` key plus
    optional ``_timestamp`` / ``_type``. We try both TR-098 and TR-181 roots
    so the reader works regardless of which data model the device uses.
    """
    igd = device.get("InternetGatewayDevice") or {}
    dev_root = device.get("Device") or {}
    igd_ms = _path(igd, "ManagementServer")
    dev_ms = _path(dev_root, "ManagementServer")
    igd_info = _path(igd, "DeviceInfo")
    dev_info = _path(dev_root, "DeviceInfo")

    wan_root = _path(igd, "WANDevice", "1", "WANConnectionDevice") or {}
    wcd_index, instance_index, wan_ppp, wan_ppp_locations = _resolve_wan_ppp(wan_root)
    _ip_wcd_index, _ip_instance_index, wan_ip = _resolve_wan_ip(wan_root)
    data_model_root = "Device" if dev_root else "InternetGatewayDevice"
    tr181_interface_index = instance_index or 1
    tr181_paths = (
        desired.tr181_wan_paths
        if desired is not None and data_model_root == "Device"
        else None
    )
    tr181_dhcp_enabled = (
        _path_value_bool(device, tr181_paths.dhcp_enable) if tr181_paths else None
    )
    tr181_ip_address = (
        _path_value(device, tr181_paths.ip_address) if tr181_paths else None
    )
    tr181_dns = (
        _join_dns_servers(
            _path_value(device, tr181_paths.dns_primary),
            _path_value(device, tr181_paths.dns_secondary),
        )
        if tr181_paths
        else None
    )
    wifi_instance_index = _detect_wifi_instance(device, data_model_root)
    wifi_paths = desired.wifi_paths if desired is not None else None
    if wifi_paths is not None:
        wifi_paths = wifi_paths_for_instance(
            wifi_paths,
            data_model_root,
            wifi_instance_index,
        )
    wifi_index = str(wifi_instance_index)
    igd_wifi = _path(igd, "LANDevice", "1", "WLANConfiguration", wifi_index)
    dev_wifi_ssid = _path(dev_root, "WiFi", "SSID", wifi_index)
    dev_wifi_radio = _path(dev_root, "WiFi", "Radio", wifi_index)
    dev_wifi_security = _path(dev_root, "WiFi", "AccessPoint", wifi_index, "Security")
    remote_paths = desired.remote_access_paths if desired is not None else None
    remote_root = _first_not_none(
        _path(igd, "X_HW_UserInterface"),
        _path(dev_root, "X_HW_UserInterface"),
    )

    return AcsObservedFields(
        acs_present=True,
        acs_observed_device_id=(str(device.get("_id") or "").strip() or None),
        acs_observed_device_match_count=match_count,
        acs_last_inform_at=_parse_timestamp(device.get("_lastInform")),
        acs_last_boot_at=_parse_timestamp(device.get("_lastBoot")),
        acs_last_bootstrap_at=_parse_timestamp(device.get("_lastBootstrap")),
        acs_observed_software_version=_first_not_none(
            _value(igd_info, "SoftwareVersion"),
            _value(dev_info, "SoftwareVersion"),
        ),
        acs_observed_pppoe_username=_value(wan_ppp, "Username"),
        acs_observed_pppoe_enable=_value_bool(wan_ppp, "Enable"),
        acs_observed_wan_vlan=(
            _path_value_int(device, tr181_paths.vlan_id)
            if tr181_paths
            else _first_not_none(
                _value_int(wan_ip, "X_HW_VLAN"),
                _value_int(wan_ppp, "X_HW_VLAN"),
            )
        ),
        acs_observed_wan_external_ip=_value(wan_ppp, "ExternalIPAddress"),
        acs_observed_wan_connection_status=_value(wan_ppp, "ConnectionStatus"),
        acs_observed_nat_enabled=(
            _path_value_bool(device, tr181_paths.nat_enable)
            if tr181_paths
            else _first_not_none(
                _value_bool(wan_ip, "NATEnabled"),
                _value_bool(wan_ppp, "NATEnabled"),
            )
        ),
        acs_observed_dhcp_enabled=_value_bool(
            _path(igd, "LANDevice", "1", "LANHostConfigManagement"),
            "DHCPServerEnable",
        ),
        acs_observed_ssid=(
            _path_value(device, wifi_paths.ssid)
            if wifi_paths
            else _first_not_none(
                _value(igd_wifi, "SSID"),
                _value(dev_wifi_ssid, "SSID"),
            )
        ),
        acs_observed_wifi_enabled=(
            _path_value_bool(device, wifi_paths.enabled)
            if wifi_paths
            else _first_not_none(
                _value_bool(igd_wifi, "Enable"),
                _value_bool(dev_wifi_ssid, "Enable"),
            )
        ),
        acs_observed_wifi_channel=(
            _path_value_int(device, wifi_paths.channel)
            if wifi_paths
            else _first_not_none(
                _value_int(igd_wifi, "Channel"),
                _value_int(dev_wifi_radio, "Channel"),
            )
        ),
        acs_observed_wifi_security_mode=(
            _path_value(device, wifi_paths.security_mode)
            if wifi_paths
            else _first_not_none(
                _value(igd_wifi, "BeaconType"),
                _value(dev_wifi_security, "ModeEnabled"),
            )
        ),
        acs_observed_wifi_instance_index=wifi_instance_index,
        acs_observed_remote_ssh_enabled=(
            _path_value_bool(device, remote_paths.ssh_enabled)
            if remote_paths
            else _value_bool(remote_root, "SSHEnable")
        ),
        acs_observed_remote_ssh_port=(
            _path_value_int(device, remote_paths.ssh_port)
            if remote_paths
            else _value_int(remote_root, "SSHPort")
        ),
        acs_observed_remote_telnet_enabled=(
            _path_value_bool(device, remote_paths.telnet_enabled)
            if remote_paths
            else _value_bool(remote_root, "TelnetEnable")
        ),
        acs_observed_remote_telnet_port=(
            _path_value_int(device, remote_paths.telnet_port)
            if remote_paths
            else _value_int(remote_root, "TelnetPort")
        ),
        acs_observed_periodic_inform_interval_sec=_first_not_none(
            _value_int(igd_ms, "PeriodicInformInterval"),
            _value_int(dev_ms, "PeriodicInformInterval"),
        ),
        acs_observed_cr_username=_first_not_none(
            _value(igd_ms, "ConnectionRequestUsername"),
            _value(dev_ms, "ConnectionRequestUsername"),
        ),
        acs_observed_cr_username_set=_first_not_none(
            _value_present(igd_ms, "ConnectionRequestUsername"),
            _value_present(dev_ms, "ConnectionRequestUsername"),
        ),
        acs_observed_cr_password_set=_first_not_none(
            _value_present(igd_ms, "ConnectionRequestPassword"),
            _value_present(dev_ms, "ConnectionRequestPassword"),
        ),
        acs_observed_wan_wcd_index=wcd_index,
        acs_observed_wan_instance_index=instance_index,
        acs_observed_wan_ppp_locations=wan_ppp_locations,
        acs_data_model_root=data_model_root,
        acs_observed_ipv6_enabled=(
            _value_bool(
                _path(dev_root, "IP", "Interface", str(tr181_interface_index)),
                "IPv6Enable",
            )
            if data_model_root == "Device"
            else _value_bool(wan_ppp, "X_IPv6Enabled")
        ),
        acs_observed_wan_ip_enable=(
            _path_value_bool(device, tr181_paths.ip_enable)
            if tr181_paths
            else _value_bool(wan_ip, "Enable")
        ),
        acs_observed_wan_addressing_type=(
            ("DHCP" if tr181_dhcp_enabled else "Static" if tr181_ip_address else None)
            if tr181_paths
            else _value(wan_ip, "AddressingType")
        ),
        acs_observed_wan_ip_address=(
            tr181_ip_address if tr181_paths else _value(wan_ip, "ExternalIPAddress")
        ),
        acs_observed_wan_subnet_mask=(
            _path_value(device, tr181_paths.subnet_mask)
            if tr181_paths
            else _value(wan_ip, "SubnetMask")
        ),
        acs_observed_wan_gateway=(
            _path_value(device, tr181_paths.gateway)
            if tr181_paths
            else _value(wan_ip, "DefaultGateway")
        ),
        acs_observed_wan_dns_servers=(
            tr181_dns if tr181_paths else _value(wan_ip, "DNSServers")
        ),
        acs_observed_dhcpv6_enabled=_value_bool(
            _path(dev_root, "DHCPv6", "Client", str(tr181_interface_index)),
            "Enable",
        ),
        acs_observed_dhcpv6_request_prefixes=_value_bool(
            _path(dev_root, "DHCPv6", "Client", str(tr181_interface_index)),
            "RequestPrefixes",
        ),
        acs_observed_ra_enabled=_value_bool(
            _path(
                dev_root,
                "RouterAdvertisement",
                "InterfaceSettings",
                str(tr181_interface_index),
            ),
            "Enable",
        ),
        acs_observed_url=_first_not_none(
            _value(igd_ms, "URL"),
            _value(dev_ms, "URL"),
        ),
        acs_observed_username=_first_not_none(
            _value(igd_ms, "Username"),
            _value(dev_ms, "Username"),
        ),
        acs_observed_password_set=_first_not_none(
            _value_present(igd_ms, "Password"),
            _value_present(dev_ms, "Password"),
        ),
    )


def _absent_fields() -> AcsObservedFields:
    return AcsObservedFields(
        acs_present=False,
        acs_last_inform_at=None,
        acs_last_boot_at=None,
        acs_last_bootstrap_at=None,
        acs_observed_software_version=None,
        acs_observed_pppoe_username=None,
        acs_observed_pppoe_enable=None,
        acs_observed_wan_vlan=None,
        acs_observed_wan_external_ip=None,
        acs_observed_wan_connection_status=None,
        acs_observed_nat_enabled=None,
        acs_observed_dhcp_enabled=None,
        acs_observed_ssid=None,
        acs_observed_wifi_enabled=None,
        acs_observed_wifi_channel=None,
        acs_observed_wifi_security_mode=None,
        acs_observed_wifi_instance_index=None,
        acs_observed_remote_ssh_enabled=None,
        acs_observed_remote_ssh_port=None,
        acs_observed_remote_telnet_enabled=None,
        acs_observed_remote_telnet_port=None,
        acs_observed_periodic_inform_interval_sec=None,
        acs_observed_cr_username=None,
        acs_observed_cr_username_set=None,
        acs_observed_cr_password_set=None,
        acs_observed_wan_wcd_index=None,
        acs_observed_wan_instance_index=None,
        acs_observed_wan_ppp_locations=(),
        acs_data_model_root=None,
        acs_observed_ipv6_enabled=None,
        acs_observed_wan_ip_enable=None,
        acs_observed_wan_addressing_type=None,
        acs_observed_wan_ip_address=None,
        acs_observed_wan_subnet_mask=None,
        acs_observed_wan_gateway=None,
        acs_observed_wan_dns_servers=None,
        acs_observed_dhcpv6_enabled=None,
        acs_observed_dhcpv6_request_prefixes=None,
        acs_observed_ra_enabled=None,
        acs_observed_url=None,
        acs_observed_username=None,
        acs_observed_password_set=None,
        acs_observed_device_id=None,
        acs_observed_device_match_count=0,
    )


def _resolve_wan_ppp(
    wan_connection_device: dict[str, Any],
) -> tuple[
    int | None,
    int | None,
    dict[str, Any] | None,
    tuple[tuple[int, int], ...],
]:
    """Locate the first WANPPPConnection instance under any WCD slot.

    Returns ``(wcd_index, instance_index, wan_ppp_dict, locations)``. The fleet's
    HG8546M devices typically expose ``WANConnectionDevice.1.WANPPPConnection.1``
    but provisioned ONTs sometimes have it on ``.2`` (e.g. UnitedAbuja). This
    helper scans the live tree for whichever slot actually carries a
    ``WANPPPConnection`` so the rest of the parser doesn't hardcode ``.1``.
    """
    locations = _wan_ppp_locations(wan_connection_device)
    if not locations:
        return None, None, None, ()
    for wcd_key, wcd_val in (wan_connection_device or {}).items():
        if not wcd_key.isdigit() or not isinstance(wcd_val, dict):
            continue
        wan_ppp_root = wcd_val.get("WANPPPConnection")
        if not isinstance(wan_ppp_root, dict):
            continue
        for ppp_key, ppp_val in wan_ppp_root.items():
            if not ppp_key.isdigit() or not isinstance(ppp_val, dict):
                continue
            return int(wcd_key), int(ppp_key), ppp_val, locations
    return None, None, None, locations


def _resolve_wan_ip(
    wan_connection_device: dict[str, Any],
) -> tuple[int | None, int | None, dict[str, Any] | None]:
    """Locate the internet WANIPConnection, avoiding the TR-069 management WAN."""
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for wcd_key, wcd_val in (wan_connection_device or {}).items():
        if not wcd_key.isdigit() or not isinstance(wcd_val, dict):
            continue
        wan_ip_root = wcd_val.get("WANIPConnection")
        if not isinstance(wan_ip_root, dict):
            continue
        for ip_key, ip_val in wan_ip_root.items():
            if ip_key.isdigit() and isinstance(ip_val, dict):
                candidates.append((int(wcd_key), int(ip_key), ip_val))
    for candidate in candidates:
        service_leaf = candidate[2].get("X_HW_SERVICELIST") or {}
        service = str(service_leaf.get("_value") or "").upper()
        if "INTERNET" in service:
            return candidate
    for candidate in candidates:
        service_leaf = candidate[2].get("X_HW_SERVICELIST") or {}
        service = str(service_leaf.get("_value") or "").upper()
        if "TR069" not in service:
            return candidate
    return None, None, None


def _wan_ppp_locations(
    wan_connection_device: dict[str, Any],
) -> tuple[tuple[int, int], ...]:
    locations: list[tuple[int, int]] = []
    for wcd_key, wcd_val in (wan_connection_device or {}).items():
        if not wcd_key.isdigit() or not isinstance(wcd_val, dict):
            continue
        wan_ppp_root = wcd_val.get("WANPPPConnection")
        if not isinstance(wan_ppp_root, dict):
            continue
        for ppp_key, ppp_val in wan_ppp_root.items():
            if not ppp_key.isdigit() or not isinstance(ppp_val, dict):
                continue
            locations.append((int(wcd_key), int(ppp_key)))
    return tuple(sorted(locations))


def _first_not_none(*values):
    """Return the first non-None value. Used in place of ``a or b`` when
    falsy-but-not-None values (empty strings, False, 0) are meaningful."""
    for value in values:
        if value is not None:
            return value
    return None


def _detect_wifi_instance(device: dict[str, Any], root: str) -> int:
    """Select the active customer-facing WLAN object from the ACS cache."""
    if root == "Device":
        instances = _path(device, "Device", "WiFi", "SSID") or {}
    else:
        instances = (
            _path(
                device,
                "InternetGatewayDevice",
                "LANDevice",
                "1",
                "WLANConfiguration",
            )
            or {}
        )

    candidates: list[tuple[int, int]] = []
    for key, value in instances.items():
        if not str(key).isdigit() or not isinstance(value, dict):
            continue
        index = int(key)
        ssid = _value(value, "SSID")
        enabled = _value_bool(value, "Enable")
        status = str(_value(value, "Status") or "").strip().lower()
        if ssid is None and enabled is None and not status:
            continue
        score = 0
        if ssid not in (None, ""):
            score += 2
        if enabled is True:
            score += 4
        if status == "up":
            score += 3
        if index == 1:
            score += 1
        candidates.append((score, index))
    return max(candidates)[1] if candidates else 1


def _path(node: dict[str, Any] | None, *keys: str) -> dict[str, Any] | None:
    current: Any = node
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _path_value(device: dict[str, Any], full_path: str) -> str | None:
    keys = tuple(part for part in full_path.split(".") if part)
    if not keys:
        return None
    leaf = _path(device, *keys)
    if not isinstance(leaf, dict):
        return None
    raw = leaf.get("_value")
    return str(raw) if raw is not None else None


def _path_value_bool(device: dict[str, Any], full_path: str) -> bool | None:
    raw = _path_value(device, full_path)
    if raw is None:
        return None
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _path_value_int(device: dict[str, Any], full_path: str) -> int | None:
    raw = _path_value(device, full_path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _join_dns_servers(primary: str | None, secondary: str | None) -> str | None:
    servers = [server for server in (primary, secondary) if server]
    return ",".join(servers) or None


def _value(node: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(node, dict):
        return None
    leaf = node.get(key)
    if not isinstance(leaf, dict):
        return None
    raw = leaf.get("_value")
    return str(raw) if raw is not None else None


def _value_present(node: dict[str, Any] | None, key: str) -> bool | None:
    """Distinguish "field not exposed" (None) from "field present but empty" (False)
    and "field has a value" (True). Used for write-only credentials where the
    actual value isn't readable, only presence."""
    if not isinstance(node, dict):
        return None
    leaf = node.get(key)
    if not isinstance(leaf, dict):
        return None
    raw = leaf.get("_value")
    if raw is None:
        return None
    return bool(str(raw))


def _value_bool(node: dict[str, Any] | None, key: str) -> bool | None:
    raw = _value(node, key)
    if raw is None:
        return None
    return str(raw).strip().lower() in {"true", "1", "yes", "on"}


def _value_int(node: dict[str, Any] | None, key: str) -> int | None:
    raw = _value(node, key)
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: Any) -> datetime | None:
    """GenieACS emits ISO-8601 timestamps with trailing ``Z``. Tolerate both."""
    if raw is None:
        return None
    from datetime import datetime as _dt

    text = str(raw)
    try:
        if text.endswith("Z"):
            return _dt.fromisoformat(text[:-1] + "+00:00")
        return _dt.fromisoformat(text)
    except ValueError:
        return None
