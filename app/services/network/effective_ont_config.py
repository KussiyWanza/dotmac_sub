"""Resolve ONT desired state from OLT config pack plus ONT desired config.

Architecture:
- Network config (VLANs, profiles, GEM indices, TR-069) comes from OLT Config Pack
- Service config (WAN mode, IP mode, PPPoE, LAN, WiFi) comes from OntUnit.desired_config
"""

from __future__ import annotations

import ipaddress
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    IPv4Address,
    OLTDevice,
    OltLineProfileGemMapping,
    OltOnuTypeProfileMapping,
    OntAssignment,
    OntUnit,
    OntWanServiceInstance,
    WanServiceType,
)
from app.services.network.equipment_identity import normalize_ont_equipment_id
from app.services.network.olt_config_pack import OltConfigPack, resolve_olt_config_pack
from app.services.network.ont_desired_config import (
    desired_config,
    get_desired_config_value,
)
from app.services.network.ont_management_ipam import get_ont_management_ip_record
from app.services.network.ont_provisioning_defaults import (
    default_pppoe_instance_index,
)
from app.services.network.profile_sync import (
    resolve_profile_bundle_for_active_subscription,
)


def _resolve_config_pack(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> OltConfigPack | None:
    if olt is not None:
        return resolve_olt_config_pack(db, olt.id)
    olt_id = getattr(ont, "olt_device_id", None)
    if olt_id is None:
        return None
    return resolve_olt_config_pack(db, olt_id)


def _get_active_assignment(ont: OntUnit) -> OntAssignment | None:
    """Get the active assignment for this ONT, if any."""
    for assignment in getattr(ont, "assignments", []):
        if getattr(assignment, "active", False):
            return assignment
    return None


def _resolve_primary_internet_wan_service(
    db: Session, ont: OntUnit | None
) -> OntWanServiceInstance | None:
    if ont is None or getattr(ont, "id", None) is None:
        return None
    return db.scalars(
        select(OntWanServiceInstance)
        .where(OntWanServiceInstance.ont_id == ont.id)
        .where(OntWanServiceInstance.is_active.is_(True))
        .where(OntWanServiceInstance.service_type == WanServiceType.internet)
        .order_by(OntWanServiceInstance.priority, OntWanServiceInstance.created_at)
        .limit(1)
    ).first()


def _resolve_imported_profile_mapping(
    db: Session,
    ont: OntUnit | None,
    olt_id: object | None,
) -> OltOnuTypeProfileMapping | None:
    if ont is None or olt_id is None:
        return None

    equipment_id = normalize_ont_equipment_id(getattr(ont, "model", None))
    if not equipment_id:
        onu_type = getattr(ont, "onu_type", None)
        equipment_id = normalize_ont_equipment_id(getattr(onu_type, "name", None))
    if not equipment_id:
        return None

    return db.scalars(
        select(OltOnuTypeProfileMapping)
        .where(OltOnuTypeProfileMapping.olt_id == olt_id)
        .where(OltOnuTypeProfileMapping.equipment_id == equipment_id)
        .limit(1)
    ).first()


def _resolve_imported_line_profile_id(
    db: Session,
    ont: OntUnit | None,
    olt_id: object | None,
) -> tuple[int | None, int | None]:
    mapping = _resolve_imported_profile_mapping(db, ont, olt_id)
    if mapping is None:
        return None, None
    return int(mapping.line_profile_id), int(mapping.service_profile_id)


def _mapping_value(mapping: OltOnuTypeProfileMapping | None, field: str) -> Any:
    if mapping is None:
        return None
    return getattr(mapping, field, None)


def _coalesce_mapping_config(
    mapping: OltOnuTypeProfileMapping | None,
    config_pack: OltConfigPack | None,
    field: str,
    default: Any = None,
) -> Any:
    mapping_override = _mapping_value(mapping, field)
    if mapping_override is not None:
        return mapping_override
    if config_pack is None:
        return default
    return getattr(config_pack, field, default)


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _nonzero_int_or_none(value: Any) -> int | None:
    """Return int(value) only if it's a positive non-zero integer.

    Used for fields where the stored value 0 is operationally equivalent to "not
    set" (e.g. Huawei `ont wan-config profile-id 0` is a silent no-op).
    """
    parsed = _int_or_none(value)
    return parsed if parsed and parsed > 0 else None


def _wan_config_profile_id_or_none(
    value: Any, *, allow_zero_profile_id: bool = False
) -> int | None:
    """Resolve WAN profile IDs, optionally preserving profile-id 0.

    Most OLTs should treat 0 as unset, but some imported Huawei configs define
    and use profile-id 0 as a real routed/NAT WAN profile. Those OLTs must opt in
    through the config pack.
    """
    parsed = _int_or_none(value)
    if parsed is None:
        return None
    if parsed == 0 and not allow_zero_profile_id:
        return None
    return parsed if parsed >= 0 else None


def internet_wcd_index_from_effective_values(
    values: dict[str, Any],
    *,
    default: int = 1,
) -> int:
    """Return the TR-098 WANConnectionDevice index for subscriber internet."""
    pppoe_wcd_index = _int_or_none(values.get("pppoe_wcd_index"))
    if pppoe_wcd_index is not None and pppoe_wcd_index > 0:
        return pppoe_wcd_index

    wan_instance_index = _int_or_none(values.get("wan_instance_index"))
    if wan_instance_index is not None and wan_instance_index > 0:
        return wan_instance_index

    return default


def resolve_internet_wcd_index(
    db: Session,
    ont: OntUnit,
    *,
    default: int = 1,
) -> int:
    """Resolve the ONT's effective TR-098 internet WANConnectionDevice index."""
    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    if not isinstance(values, dict):
        return default
    return internet_wcd_index_from_effective_values(values, default=default)


def _resolve_imported_vlan_gem_index(
    db: Session,
    *,
    olt_id: object | None,
    line_profile_id: int | None,
    vlan_tag: int | None,
) -> int | None:
    if olt_id is None or line_profile_id is None or vlan_tag is None:
        return None
    mapping = db.scalars(
        select(OltLineProfileGemMapping)
        .where(OltLineProfileGemMapping.olt_id == olt_id)
        .where(OltLineProfileGemMapping.line_profile_id == line_profile_id)
        .where(OltLineProfileGemMapping.vlan_id == vlan_tag)
        .order_by(
            OltLineProfileGemMapping.source.desc(),
            OltLineProfileGemMapping.usage_count.desc(),
            OltLineProfileGemMapping.gem_index,
        )
        .limit(1)
    ).first()
    if mapping is None:
        return None
    return int(mapping.gem_index)


def _values_from_assignment(
    db: Session,
    config_pack: OltConfigPack | None,
    assignment: OntAssignment | None = None,
    config: dict[str, Any] | None = None,
    ont: OntUnit | None = None,
) -> dict[str, Any]:
    """Build effective config values from desired_config plus OLT defaults."""
    _ = assignment
    config = config or {}

    def cfg(*path: str, default: Any = None) -> Any:
        return get_desired_config_value(config, *path, default=default)

    asn_wan_mode = cfg("wan", "onu_mode")
    asn_ip_mode = cfg("wan", "mode")
    asn_static_ip = cfg("wan", "static_ip")
    asn_static_gateway = cfg("wan", "static_gateway")
    asn_static_subnet = cfg("wan", "static_subnet")
    asn_static_dns = cfg("wan", "static_dns")
    asn_pppoe_username = cfg("wan", "pppoe_username")
    asn_pppoe_password = cfg("wan", "pppoe_password")
    asn_wifi_ssid = cfg("wifi", "ssid")
    asn_wifi_password = cfg("wifi", "password")

    if asn_wan_mode is None and asn_ip_mode:
        asn_wan_mode = (
            "bridging" if str(asn_ip_mode) in {"bridge", "setup_via_onu"} else "routing"
        )

    asn_mgmt_ip_mode = cfg("management", "ip_mode")
    asn_mgmt_ip_address = cfg("management", "ip_address")
    asn_mgmt_subnet = cfg("management", "subnet")
    asn_mgmt_gateway = cfg("management", "gateway")
    ipam_management_record = get_ont_management_ip_record(db, ont) if ont else None
    if ipam_management_record is not None:
        asn_mgmt_ip_mode = "static_ip"
        asn_mgmt_ip_address = str(ipam_management_record.address)
        pool = getattr(ipam_management_record, "pool", None)
        if pool is not None:
            try:
                asn_mgmt_subnet = str(
                    ipaddress.ip_network(str(pool.cidr), strict=False).netmask
                )
            except ValueError:
                pass
            asn_mgmt_gateway = str(getattr(pool, "gateway", "") or "").strip() or None
    if asn_mgmt_ip_address and (not asn_mgmt_subnet or not asn_mgmt_gateway):
        pool_subnet, pool_gateway = _resolve_management_pool_network(
            db,
            str(asn_mgmt_ip_address),
        )
        asn_mgmt_subnet = asn_mgmt_subnet or pool_subnet
        asn_mgmt_gateway = asn_mgmt_gateway or pool_gateway

    asn_lan_ip = cfg("lan", "ip")
    asn_lan_subnet = cfg("lan", "subnet")
    asn_lan_dhcp_enabled = cfg("lan", "dhcp_enabled")
    asn_lan_dhcp_start = cfg("lan", "dhcp_start")
    asn_lan_dhcp_end = cfg("lan", "dhcp_end")
    asn_wifi_enabled = cfg("wifi", "enabled")
    asn_wifi_channel = cfg("wifi", "channel")
    asn_wifi_security_mode = cfg("wifi", "security_mode")
    access_wan_remote = cfg("access", "wan_remote")
    access_mgmt_remote = cfg("access", "mgmt_remote")
    access_http_management = cfg("access", "http_management")
    # wifi_enabled: explicit setting takes precedence, else True if SSID is set
    wifi_enabled = (
        asn_wifi_enabled
        if asn_wifi_enabled is not None
        else (True if asn_wifi_ssid else None)
    )

    # Extract VLAN info (both ID and tag for compatibility)
    wan_vlan = config_pack.internet_vlan if config_pack else None
    mgmt_vlan = config_pack.management_vlan if config_pack else None
    olt_id = config_pack.olt_id if config_pack else getattr(ont, "olt_device_id", None)
    active_assignment = (
        assignment
        if assignment is not None
        else (_get_active_assignment(ont) if ont else None)
    )
    profile_bundle = (
        resolve_profile_bundle_for_active_subscription(
            db,
            olt_id=olt_id,
            subscriber_id=getattr(active_assignment, "subscriber_id", None),
            subscription_id=getattr(active_assignment, "subscription_id", None),
        )
        if active_assignment is not None
        else None
    )
    profile_mapping = _resolve_imported_profile_mapping(db, ont, olt_id)
    primary_wan_instance = _resolve_primary_internet_wan_service(db, ont)
    imported_line_profile_id = (
        int(profile_mapping.line_profile_id) if profile_mapping else None
    )
    imported_service_profile_id = (
        int(profile_mapping.service_profile_id) if profile_mapping else None
    )
    imported_wan_gem_index = _resolve_imported_vlan_gem_index(
        db,
        olt_id=olt_id,
        line_profile_id=imported_line_profile_id,
        vlan_tag=wan_vlan.tag if wan_vlan else None,
    )
    imported_mgmt_gem_index = _resolve_imported_vlan_gem_index(
        db,
        olt_id=olt_id,
        line_profile_id=imported_line_profile_id,
        vlan_tag=mgmt_vlan.tag if mgmt_vlan else None,
    )
    authorization_line_profile_id = (
        int(profile_bundle.line_profile_id)
        if profile_bundle is not None
        else imported_line_profile_id
    )
    authorization_service_profile_id = (
        int(profile_bundle.service_profile_id)
        if profile_bundle is not None
        else imported_service_profile_id
    )
    internet_gem_index = (
        int(profile_bundle.gem_id)
        if profile_bundle is not None
        else imported_wan_gem_index
    )
    internet_vlan_tag = (
        int(profile_bundle.vlan_id)
        if profile_bundle is not None
        else (wan_vlan.tag if wan_vlan else None)
    )

    values = {
        "config_method": None,
        "onu_mode": asn_wan_mode,
        # Missing protocol intent stays unknown. The reconciler carries its
        # provenance separately so absence cannot be turned into an IPv4
        # device write.
        "ip_protocol": cfg("wan", "ip_protocol"),
        "wan_mode": asn_ip_mode,
        "wan_vlan": internet_vlan_tag,
        "wan_vlan_id": str(wan_vlan.id) if wan_vlan and wan_vlan.id else None,
        "pppoe_username": asn_pppoe_username,
        "pppoe_password": asn_pppoe_password,
        "wan_static_ip": asn_static_ip,
        "wan_static_subnet": asn_static_subnet,
        "wan_static_gateway": asn_static_gateway,
        "wan_static_dns": asn_static_dns,
        # The instantiated primary Internet service is the order-resolved SOT
        # for ONT-side NAT. Additional routed IPs stay BNG-owned and never
        # become ONT WAN addresses.
        "nat_enabled": (
            bool(primary_wan_instance.nat_enabled)
            if primary_wan_instance is not None
            else cfg("wan", "nat_enabled")
        ),
        "wan_instance_index": cfg(
            "wan",
            "instance_index",
            default=default_pppoe_instance_index(),
        ),
        "wan_gem_index": internet_gem_index,
        "mgmt_ip_mode": asn_mgmt_ip_mode,
        "mgmt_vlan": mgmt_vlan.tag if mgmt_vlan else None,
        "mgmt_vlan_id": str(mgmt_vlan.id) if mgmt_vlan and mgmt_vlan.id else None,
        "mgmt_ip_address": asn_mgmt_ip_address,
        "mgmt_subnet": asn_mgmt_subnet,
        "mgmt_gateway": asn_mgmt_gateway,
        "mgmt_gem_index": imported_mgmt_gem_index,
        "lan_ip": asn_lan_ip,
        "lan_subnet": asn_lan_subnet,
        "lan_dhcp_enabled": asn_lan_dhcp_enabled,
        "lan_dhcp_start": asn_lan_dhcp_start,
        "lan_dhcp_end": asn_lan_dhcp_end,
        "wifi_enabled": wifi_enabled,
        "wifi_ssid": asn_wifi_ssid,
        "wifi_password": asn_wifi_password,
        "wifi_channel": asn_wifi_channel,
        "wifi_security_mode": asn_wifi_security_mode,
        "wan_remote_access": (
            bool(access_wan_remote) if access_wan_remote is not None else False
        ),
        "mgmt_remote_access": (
            bool(access_mgmt_remote) if access_mgmt_remote is not None else False
        ),
        "http_management": (
            bool(access_http_management)
            if access_http_management is not None
            else False
        ),
        "tr069_acs_server_id": config_pack.tr069_acs_server_id if config_pack else None,
        "tr069_olt_profile_id": (
            ont.desired_tr069_profile_id
            if ont is not None and ont.desired_tr069_profile_id is not None
            else config_pack.tr069_olt_profile_id
            if config_pack
            else None
        ),
        "cr_username": config_pack.cr_username if config_pack else None,
        "cr_password": config_pack.cr_password if config_pack else None,
        "internet_config_ip_index": _coalesce_mapping_config(
            profile_mapping, config_pack, "internet_config_ip_index"
        ),
        # profile-id 0 is normally treated as unset, unless the OLT config pack
        # explicitly marks it as a valid live WAN profile.
        "wan_config_profile_id": _wan_config_profile_id_or_none(
            _coalesce_mapping_config(
                profile_mapping, config_pack, "wan_config_profile_id"
            ),
            allow_zero_profile_id=bool(
                getattr(config_pack, "allow_zero_wan_config_profile_id", False)
            ),
        ),
        "wan_provisioning_mode": _coalesce_mapping_config(
            profile_mapping, config_pack, "wan_provisioning_mode", "omci_wan_config"
        ),
        "supports_ont_home_gateway_config": (
            config_pack.supports_ont_home_gateway_config if config_pack else False
        ),
        "pppoe_omci_vlan": None,
        # TR-069 WCD indices (OLT-provisioning-specific, determines WANConnectionDevice.{i}).
        # Per-ONT override (desired_config) wins over per-OLT default
        # (profile mapping / config pack). The defaults are also surfaced
        # under ``*_wcd_index_default`` so the form can render an "inherit"
        # label distinct from an explicit operator override.
        "pppoe_wcd_index_default": _coalesce_mapping_config(
            profile_mapping, config_pack, "pppoe_wcd_index"
        ),
        "mgmt_wcd_index_default": _coalesce_mapping_config(
            profile_mapping, config_pack, "mgmt_wcd_index"
        ),
        "voip_wcd_index_default": _coalesce_mapping_config(
            profile_mapping, config_pack, "voip_wcd_index"
        ),
        "pppoe_wcd_index_override": _int_or_none(cfg("wan", "pppoe_wcd_index")),
        "mgmt_wcd_index_override": _int_or_none(cfg("management", "wcd_index")),
        "voip_wcd_index_override": _int_or_none(cfg("voip", "wcd_index")),
        "pppoe_wcd_index": _int_or_none(cfg("wan", "pppoe_wcd_index"))
        or _coalesce_mapping_config(profile_mapping, config_pack, "pppoe_wcd_index"),
        "mgmt_wcd_index": _int_or_none(cfg("management", "wcd_index"))
        or _coalesce_mapping_config(profile_mapping, config_pack, "mgmt_wcd_index"),
        "voip_wcd_index": _int_or_none(cfg("voip", "wcd_index"))
        or _coalesce_mapping_config(profile_mapping, config_pack, "voip_wcd_index"),
        # Per-ONT OLT service-port indices. Currently there's no per-OLT
        # default — the reconciler allocates on first provision when these
        # are None, and the validator rejects post-allocation changes.
        "mgmt_service_port_index": _int_or_none(cfg("olt", "mgmt_service_port_index")),
        "wan_service_port_index": _int_or_none(cfg("olt", "wan_service_port_index")),
        "authorization_line_profile_id": authorization_line_profile_id,
        "authorization_service_profile_id": authorization_service_profile_id,
        "profile_bundle_id": str(profile_bundle.id) if profile_bundle else None,
        "primary_wan_service": _mapping_value(profile_mapping, "primary_wan_service")
        or cfg("wan", "primary_service"),
    }
    values["internet_ip_index"] = values["internet_config_ip_index"]
    return values


def _resolve_management_pool_network(
    db: Session,
    address: str,
) -> tuple[str | None, str | None]:
    record = db.scalars(
        select(IPv4Address).where(IPv4Address.address == address).limit(1)
    ).first()
    pool = getattr(record, "pool", None) if record is not None else None
    if pool is None:
        return None, None

    subnet = None
    cidr = getattr(pool, "cidr", None)
    if cidr:
        try:
            subnet = str(ipaddress.ip_network(str(cidr), strict=False).netmask)
        except ValueError:
            subnet = None

    gateway = str(getattr(pool, "gateway", "") or "").strip() or None
    return subnet, gateway


def _explicit_keys(config: dict[str, Any]) -> list[str]:
    keys: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for child_key, child_value in sorted(value.items()):
                walk(f"{prefix}.{child_key}" if prefix else child_key, child_value)
            return
        keys.append(prefix)

    walk("", config)
    return keys


def resolve_effective_ont_config(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> dict[str, Any]:
    """Return ONT desired config resolved from OLT config pack and desired_config.

    Service config (WAN mode, IP mode, PPPoE, LAN, WiFi) is read from
    ``OntUnit.desired_config``.

    Network config (VLANs, profiles, GEM indices, TR-069) comes from OltConfigPack.
    """
    config = desired_config(ont)
    config_pack = _resolve_config_pack(db, ont, olt=olt)
    assignment = _get_active_assignment(ont)
    return {
        "config_pack": config_pack,
        "assignment": assignment,
        "desired_config_keys": _explicit_keys(config),
        "values": _values_from_assignment(db, config_pack, assignment, config, ont),
    }


def get_effective_value(
    db: Session,
    ont: OntUnit,
    key: str,
    *,
    olt: OLTDevice | None = None,
    default: Any = None,
) -> Any:
    """Convenience accessor for callers that need one resolved value."""
    resolved = resolve_effective_ont_config(db, ont, olt=olt)
    return resolved.get("values", {}).get(key, default)
