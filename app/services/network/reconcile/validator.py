"""Validate a proposed mutation of ``OntDesiredState``.

Validation runs at the boundary — UI write endpoints, external webhooks, and the
top of ``reconcile_ont`` itself. Internal reconciler code (planner, applier,
readers) does **not** re-validate. The current ``OntDesiredState`` is assumed
valid because it had to pass validation when it was first committed.

Rules (cross-referenced from the design discussion):

1. Identity fields (``ont_unit_id``, ``serial_number``, ``olt_id``,
   ``olt_ont_id``) are immutable post-creation. Cross-OLT moves go through a
   separate ``migrate_ont`` endpoint, not ``reconcile_ont``.
2. Service-port indices (``mgmt_service_port_index``, ``wan_service_port_index``)
   are allocator outputs and immutable after first allocation.
3. ``wan_mode=bridge`` is incompatible with ``nat_enabled=True``.
4. ``wan_mode=pppoe`` requires ``mgmt_vlan`` to be set (otherwise the ONT auths
   on the OLT but never reaches the ACS).
5. ``wan_pppoe_provisioning_method="omci"`` requires a positive integer
   ``wan_config_profile_id`` (profile-id 0 is silently a no-op on Huawei OLTs).
6. All VLAN IDs lie in ``[1, 4094]``.
7. ``mgmt_ip`` and ``mgmt_gateway`` lie within the network defined by
   ``mgmt_ip/mgmt_subnet_mask`` and are distinct addresses.
8. ``dhcp_pool_min`` and ``dhcp_pool_max`` lie within the network defined by
   ``dhcp_pool_min/dhcp_subnet_mask`` with ``min <= max``.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import AddressValueError, IPv4Address, IPv4Network

from app.services.credential_crypto import is_encrypted
from app.services.secrets import is_secret_ref

from .state import OntDesiredState


@dataclass(frozen=True)
class Validation:
    """Outcome of a validation pass.

    ``ok`` is the only field callers should branch on. ``reason`` is a short
    operator-readable string safe to surface in API error responses (e.g.
    ``"bridge mode is incompatible with nat_enabled=True"``).
    """

    ok: bool
    reason: str | None = None


def validate_desired(
    target: OntDesiredState,
    current: OntDesiredState,
) -> Validation:
    """Check whether ``target`` is a legal mutation of ``current``.

    Returns the first rule violation found, so error messages are deterministic
    and reflect the highest-priority rule rather than a noisy aggregate.
    """
    immutable = _check_immutable_identity(target, current)
    if not immutable.ok:
        return immutable

    sp_check = _check_service_port_immutability(target, current)
    if not sp_check.ok:
        return sp_check

    if target.tr069_profile_id <= 0:
        return Validation(False, "tr069_profile_id must be a positive integer")

    wifi_check = _check_wifi(target, current)
    if not wifi_check.ok:
        return wifi_check

    contradiction = _check_mode_contradictions(target)
    if not contradiction.ok:
        return contradiction

    vlan_check = _check_vlan_ranges(target)
    if not vlan_check.ok:
        return vlan_check

    mgmt_check = _check_mgmt_subnet(target)
    if not mgmt_check.ok:
        return mgmt_check

    dhcp_check = _check_dhcp_pool(target)
    if not dhcp_check.ok:
        return dhcp_check

    return Validation(True)


# ── Rule implementations ────────────────────────────────────────────────────


def _check_immutable_identity(
    target: OntDesiredState, current: OntDesiredState
) -> Validation:
    if target.ont_unit_id != current.ont_unit_id:
        return Validation(False, "ont_unit_id is immutable")
    if target.serial_number != current.serial_number:
        return Validation(False, "serial_number is immutable")
    if target.olt_id != current.olt_id:
        return Validation(
            False, "olt_id is immutable; use migrate_ont for cross-OLT moves"
        )
    if target.olt_ont_id != current.olt_ont_id:
        return Validation(False, "olt_ont_id is immutable post-creation")
    return Validation(True)


def _check_service_port_immutability(
    target: OntDesiredState, current: OntDesiredState
) -> Validation:
    if (
        current.mgmt_service_port_index is not None
        and target.mgmt_service_port_index != current.mgmt_service_port_index
    ):
        return Validation(False, "mgmt_service_port_index is immutable post-allocation")
    if (
        current.wan_service_port_index is not None
        and target.wan_service_port_index != current.wan_service_port_index
    ):
        return Validation(False, "wan_service_port_index is immutable post-allocation")
    return Validation(True)


def _check_wifi(target: OntDesiredState, current: OntDesiredState) -> Validation:
    if target.wifi_ssid and len(target.wifi_ssid) > 32:
        return Validation(False, "wifi_ssid must be at most 32 characters")
    if (
        target.wifi_password_ref != current.wifi_password_ref
        and not is_encrypted(target.wifi_password_ref)
        and not is_secret_ref(target.wifi_password_ref)
        and not 8 <= len(target.wifi_password_ref) <= 63
    ):
        return Validation(False, "WiFi password must be 8-63 characters")
    if target.wifi_channel is not None and not 0 <= target.wifi_channel <= 196:
        return Validation(False, "wifi_channel must be between 0 and 196")
    if target.wifi_security_mode and len(target.wifi_security_mode) > 40:
        return Validation(False, "wifi_security_mode must be at most 40 characters")
    return Validation(True)


def _check_mode_contradictions(target: OntDesiredState) -> Validation:
    if target.wan_mode == "bridge" and target.nat_enabled:
        return Validation(False, "bridge mode is incompatible with nat_enabled=True")
    if target.wan_mode == "pppoe" and target.mgmt_vlan is None:
        return Validation(False, "wan_mode=pppoe requires a management VLAN")
    if target.wan_mode == "pppoe" and target.wan_pppoe_provisioning_method == "omci":
        if target.wan_config_profile_id is None or target.wan_config_profile_id <= 0:
            return Validation(
                False,
                "wan_pppoe_provisioning_method=omci requires "
                "wan_config_profile_id to be a positive integer",
            )
    if target.wan_mode in {"dhcp", "static"} and target.wan_vlan is None:
        return Validation(False, f"wan_mode={target.wan_mode} requires a WAN VLAN")
    if target.wan_mode == "static":
        if not (
            target.wan_static_ip
            and target.wan_static_subnet
            and target.wan_static_gateway
        ):
            return Validation(
                False,
                "wan_mode=static requires IP address, subnet mask, and gateway",
            )
        if target.wan_static_ip_is_public is True and target.nat_enabled:
            return Validation(False, "public static WAN requires nat_enabled=False")
        if target.wan_static_ip_is_public is False and not target.nat_enabled:
            return Validation(False, "private static WAN requires nat_enabled=True")
    if (
        target.wan_mode in {"dhcp", "static"}
        and target.tr069_data_model_root == "Device"
        and target.tr181_wan_paths is None
    ):
        return Validation(
            False,
            "TR-181 routed WAN requires a supported vendor/model parameter map",
        )
    return Validation(True)


def _check_vlan_ranges(target: OntDesiredState) -> Validation:
    for label, value in (
        ("mgmt_vlan", target.mgmt_vlan),
        ("wan_vlan", target.wan_vlan),
    ):
        if value is None:
            continue
        if not (1 <= value <= 4094):
            return Validation(False, f"{label} {value} outside [1, 4094]")
    return Validation(True)


def _check_mgmt_subnet(target: OntDesiredState) -> Validation:
    """Mgmt IP and gateway must both lie within the mgmt network."""
    if target.mgmt_ip is None:
        # No mgmt IP set — that's only legitimate when mgmt_vlan is also unset,
        # which the mode-contradiction check already validates for pppoe.
        return Validation(True)
    if target.mgmt_subnet_mask is None or target.mgmt_gateway is None:
        return Validation(
            False,
            "mgmt_ip requires mgmt_subnet_mask and mgmt_gateway to be set",
        )
    return _validate_subnet_membership(
        target.mgmt_ip, target.mgmt_gateway, target.mgmt_subnet_mask
    )


def _check_dhcp_pool(target: OntDesiredState) -> Validation:
    if not target.dhcp_enabled:
        return Validation(True)
    return _validate_dhcp_pool_membership(
        target.dhcp_pool_min, target.dhcp_pool_max, target.dhcp_subnet_mask
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _validate_subnet_membership(ip: str, gateway: str, subnet_mask: str) -> Validation:
    try:
        ip_addr = IPv4Address(ip)
        gw_addr = IPv4Address(gateway)
        prefix = _mask_to_prefix(subnet_mask)
        network = IPv4Network(f"{ip}/{prefix}", strict=False)
    except (AddressValueError, ValueError) as exc:
        return Validation(False, f"invalid IPv4 address or mask: {exc}")
    if ip_addr not in network:
        return Validation(False, f"mgmt_ip {ip} not in network {network}")
    if gw_addr not in network:
        return Validation(False, f"mgmt_gateway {gateway} not in network {network}")
    if ip_addr == gw_addr:
        return Validation(False, "mgmt_ip cannot equal mgmt_gateway")
    return Validation(True)


def _validate_dhcp_pool_membership(
    min_addr: str, max_addr: str, subnet_mask: str
) -> Validation:
    try:
        min_ip = IPv4Address(min_addr)
        max_ip = IPv4Address(max_addr)
        prefix = _mask_to_prefix(subnet_mask)
        network = IPv4Network(f"{min_addr}/{prefix}", strict=False)
    except (AddressValueError, ValueError) as exc:
        return Validation(False, f"invalid DHCP pool address or mask: {exc}")
    if min_ip not in network:
        return Validation(False, f"dhcp_pool_min {min_addr} not in network {network}")
    if max_ip not in network:
        return Validation(False, f"dhcp_pool_max {max_addr} not in network {network}")
    if int(max_ip) < int(min_ip):
        return Validation(False, "dhcp_pool_max must be >= dhcp_pool_min")
    return Validation(True)


def _mask_to_prefix(mask: str) -> int:
    """Convert a dotted-decimal mask (e.g. 255.255.255.0) to a prefix length.

    Rejects non-contiguous masks (e.g. 255.255.0.255), which are technically
    representable in IPv4 but never legal in modern networks.
    """
    try:
        as_int = int(IPv4Address(mask))
    except (AddressValueError, ValueError) as exc:
        raise ValueError(f"invalid subnet mask: {mask}") from exc
    binary = bin(as_int)[2:].rjust(32, "0")
    if "01" in binary:
        raise ValueError(f"non-contiguous subnet mask: {mask}")
    return binary.count("1")
