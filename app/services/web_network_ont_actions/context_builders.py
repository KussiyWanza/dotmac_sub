"""Context builders for ONT web action UI tabs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.network import OntAssignment
from app.models.tr069 import Tr069CpeDevice
from app.services import network as network_service
from app.services.catalog.ip_block_choices import (
    IpBlockPrefix,
    active_catalog_ip_block_choices,
    subscriber_ip_block_entitlements,
)
from app.services.network._util import first_present as _first_present
from app.services.network.effective_ont_config import (
    internet_wcd_index_from_effective_values,
    resolve_effective_ont_config,
)
from app.services.service_intent_ui_adapter import service_intent_ui_adapter
from app.services.web_network_ont_actions._common import (
    _display_olt_value,
)
from app.services.web_network_onts import management_ip_choices_for_ont

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OntConfigureReadiness:
    """Read-only prerequisites disclosed by the admin Configuration tab."""

    olt_assigned: bool
    config_pack_ready: bool
    acs_registered: bool


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return str(raw) if raw not in (None, "") else None


def _plan_section(ont_plan: dict[str, object], step_name: str) -> dict[str, object]:
    value = ont_plan.get(step_name)
    return value if isinstance(value, dict) else {}


def _vlan_tag(vlan: object | None) -> str:
    tag = getattr(vlan, "tag", None)
    return str(tag) if tag not in (None, "") else ""


def _desired_config_context(
    db: Session,
    ont: object,
    *,
    ont_plan: dict[str, object],
    initial_iphost_form: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return durable desired config values for ONT config partials."""
    effective = resolve_effective_ont_config(db, ont)
    values = effective["values"]

    wan_mode = _first_present(
        values.get("wan_mode"),
    )
    wan_vlan = _first_present(
        values.get("wan_vlan"),
    )
    internet_wcd_index = internet_wcd_index_from_effective_values(values)

    mgmt_mode = _first_present(
        values.get("mgmt_ip_mode"),
    )
    if mgmt_mode == "static_ip":
        mgmt_mode = "static"
    mgmt_vlan = _first_present(
        values.get("mgmt_vlan"),
    )
    mgmt_ip = _first_present(
        values.get("mgmt_ip_address"),
    )

    return {
        "desired_mgmt_config": {
            "ip_mode": mgmt_mode or "",
            "vlan_id": str(mgmt_vlan or ""),
            "ip_address": str(mgmt_ip or ""),
            "subnet": str(values.get("mgmt_subnet") or ""),
            "gateway": str(values.get("mgmt_gateway") or ""),
        },
        "desired_wan_config": {
            "wan_mode": wan_mode or "",
            "ip_protocol": str(values.get("ip_protocol") or ""),
            "wan_vlan": str(wan_vlan or ""),
            "ip_address": str(values.get("wan_static_ip") or ""),
            "subnet_mask": str(values.get("wan_static_subnet") or ""),
            "gateway": str(values.get("wan_static_gateway") or ""),
            "dns_servers": str(values.get("wan_static_dns") or ""),
            "instance_index": internet_wcd_index,
            "pppoe_username": str(values.get("pppoe_username") or ""),
        },
        "desired_lan_config": {
            "lan_ip": str(values.get("lan_ip") or ""),
            "lan_subnet": str(values.get("lan_subnet") or ""),
            "dhcp_enabled": values.get("lan_dhcp_enabled"),
            "dhcp_start": str(values.get("lan_dhcp_start") or ""),
            "dhcp_end": str(values.get("lan_dhcp_end") or ""),
        },
        "desired_wifi_config": {
            "enabled": values.get("wifi_enabled"),
            "ssid": str(values.get("wifi_ssid") or ""),
            "channel": str(values.get("wifi_channel") or ""),
            "security_mode": str(values.get("wifi_security_mode") or ""),
        },
        "config_resolution": {
            "config_pack": effective.get("config_pack"),
            "desired_config_keys": effective.get("desired_config_keys", []),
        },
        "config_pack": effective.get("config_pack"),
        "effective_config": effective,
    }


def _empty_observed_read_result(*, message: str, data: object) -> object:
    from app.services.olt_observed_state_adapter import ObservedReadResult

    return ObservedReadResult(
        ok=True,
        message=message,
        data=data,
        source="db",
        fetched_at=None,
        stale=True,
    )


def _resolve_linked_tr069_device(db: Session, ont: object) -> object | None:
    return (
        db.execute(
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.ont_unit_id == ont.id)
            .where(Tr069CpeDevice.is_active.is_(True))
            .order_by(Tr069CpeDevice.updated_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _load_unified_observed_state(db: Session, ont: object) -> dict[str, object]:
    from app.services import web_network_onts as web_network_onts_service
    from app.services.network import ont_web_forms as ont_web_forms_service
    from app.services.olt_observed_state_adapter import (
        get_cached_iphost_config,
        get_cached_tr069_profiles_for_olt,
    )

    iphost_result = get_cached_iphost_config(ont) or _empty_observed_read_result(
        message="No cached IPHOST configuration.",
        data={},
    )
    iphost_config = dict(getattr(iphost_result, "data", {}) or {})
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    olt = web_network_onts_service.resolve_ont_connected_olt(db, ont)
    profiles_result = (
        get_cached_tr069_profiles_for_olt(olt)
        if olt is not None
        else _empty_observed_read_result(
            message="No OLT is assigned to this ONT.",
            data=[],
        )
    )
    initial_form = ont_web_forms_service.initial_iphost_form(ont, iphost_config)
    return {
        "iphost_result": iphost_result,
        "iphost_config": iphost_config,
        "vlans": vlans,
        "profiles_result": profiles_result,
        "tr069_profiles": list(getattr(profiles_result, "data", []) or []),
        "tr069_profiles_error": (
            profiles_result.message
            if (not profiles_result.ok or profiles_result.stale)
            else None
        ),
        "initial_form": initial_form,
    }


def _resolve_cached_tr069_profile(
    db: Session,
    ont: object,
    profiles: list[object],
    *,
    effective: dict[str, object] | None = None,
) -> object | None:
    """Resolve the effective TR-069 profile from cached OLT profiles only."""
    if not profiles:
        return None

    effective = effective or resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    planned_profile_id = values.get("tr069_olt_profile_id")
    if planned_profile_id is not None:
        planned_profile_id_str = str(planned_profile_id).strip()
        for profile in profiles:
            profile_id = getattr(profile, "profile_id", None)
            if (
                profile_id is not None
                and str(profile_id).strip() == planned_profile_id_str
            ):
                return profile

    return profiles[0] if len(profiles) == 1 else None


def _load_subscriber_info(db: Session, ont: object) -> dict[str, object]:
    assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()
    if not assignment or not assignment.subscriber_id:
        return {}
    if assignment.subscriber:
        return {
            "name": str(
                getattr(assignment.subscriber, "display_name", "")
                or getattr(assignment.subscriber, "full_name", "")
                or ""
            ).strip()
        }
    return {}


def _load_ont_detail_config_state(
    db: Session,
    ont_id: str,
    detail_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    linked_tr069 = _resolve_linked_tr069_device(db, ont)
    observed_state = _load_unified_observed_state(db, ont)
    if detail_payload is None:
        from app.services import web_network_core_devices_views as core_devices_views

        detail_payload = core_devices_views.ont_detail_page_data(db, ont_id)
    detail_payload = detail_payload if isinstance(detail_payload, dict) else {}
    subscriber_info = detail_payload.get("subscriber_info", {})
    if not isinstance(subscriber_info, dict) or not subscriber_info:
        subscriber_info = _load_subscriber_info(db, ont)
    ont_plan = service_intent_ui_adapter.load_ont_plan_for_ont(db, ont_id=ont_id)
    acs_observed_intent = _build_db_observed_service_intent(db, linked_tr069, ont)
    return {
        "ont": ont,
        "linked_tr069": linked_tr069,
        "observed_state": observed_state,
        "detail_payload": detail_payload,
        "subscriber_info": subscriber_info,
        "ont_plan": ont_plan,
        "acs_observed_intent": acs_observed_intent,
    }


def _build_db_observed_service_intent(
    db: Session, linked_tr069: object, ont: object
) -> dict[str, object]:
    # DB-backed observed intent is read-only display; desired config remains separate.
    return service_intent_ui_adapter.build_acs_observed_service_intent(
        SimpleNamespace(
            available=bool(linked_tr069),
            source="db",
            fetched_at=getattr(ont, "observed_runtime_updated_at", None),
            system={
                "SerialNumber": getattr(ont, "serial_number", None),
                "MACAddress": getattr(ont, "mac_address", None),
            },
            wan={
                "WAN IP": getattr(ont, "observed_wan_ip", None),
                "Status": getattr(ont, "observed_pppoe_status", None),
            },
            lan={},
            wireless={},
            ethernet_ports=[],
            lan_hosts=[],
        )
    )


def _unified_summary_context(
    *,
    desired_mgmt: dict[str, object],
    desired_wan: dict[str, object],
    desired_wifi: dict[str, object],
    acs_observed_intent: dict[str, object],
) -> dict[str, object]:
    observed = acs_observed_intent.get("observed", {})
    observed_wan = observed.get("wan", {}) if isinstance(observed, dict) else {}
    observed_wifi = observed.get("wifi", {}) if isinstance(observed, dict) else {}
    wan_status = (
        str(observed_wan.get("status") or "").strip().lower()
        if isinstance(observed_wan, dict)
        else ""
    )
    return {
        "mgmt_ip_summary": {
            "mode": desired_mgmt.get("ip_mode"),
            "vlan": desired_mgmt.get("vlan_id"),
            "ip": (
                desired_mgmt.get("ip_address")
                if desired_mgmt.get("ip_mode") == "static"
                else None
            ),
        },
        "wan_summary": {
            "ip_protocol": desired_wan.get("ip_protocol"),
            "pppoe_user": (
                observed_wan.get("pppoe_username")
                if isinstance(observed_wan, dict)
                else None
            )
            or desired_wan.get("pppoe_username"),
            "wan_ip": (
                observed_wan.get("wan_ip") if isinstance(observed_wan, dict) else None
            ),
            "status": wan_status or None,
        },
        "wifi_summary": {
            "ssid": (
                observed_wifi.get("ssid") if isinstance(observed_wifi, dict) else None
            )
            or desired_wifi.get("ssid")
        },
    }


def _configure_form_context_from_state(
    db: Session,
    ont: object,
    ont_id: str,
    *,
    effective: dict[str, object],
    linked_tr069: object | None = None,
) -> dict[str, object]:
    values = effective["values"]
    config_pack = effective.get("config_pack")

    lan_gateway = values.get("lan_ip")
    lan_subnet = values.get("lan_subnet")
    lan_dhcp_enabled = values.get("lan_dhcp_enabled")
    lan_dhcp_start = values.get("lan_dhcp_start")
    lan_dhcp_end = values.get("lan_dhcp_end")

    wifi_ssid = values.get("wifi_ssid")
    wifi_enabled = values.get("wifi_enabled")
    wifi_channel = values.get("wifi_channel")
    wifi_security = values.get("wifi_security_mode")

    mgmt_mode = values.get("mgmt_ip_mode")
    mgmt_ip = values.get("mgmt_ip_address")
    mgmt_mode_value = _enum_value(mgmt_mode) or ""
    mgmt_remote_access = bool(values.get("mgmt_remote_access"))
    if mgmt_mode_value in {"dhcp", "static_ip"}:
        mgmt_remote_access = True

    tr069_profile_id = values.get("tr069_olt_profile_id")
    tr069_profile_name = None
    if config_pack:
        tr069_profile_name = getattr(config_pack, "tr069_profile_name", None)

    if linked_tr069 is None:
        linked_tr069 = _resolve_linked_tr069_device(db, ont)
    has_tr069 = bool(
        linked_tr069 and str(getattr(linked_tr069, "genieacs_device_id", "") or "")
    )
    acs_last_inform = (
        getattr(linked_tr069, "last_inform_at", None) if linked_tr069 else None
    )

    config_pack_name = None
    config_pack_olt_id = None
    if config_pack:
        config_pack_name = getattr(config_pack, "name", None)
        config_pack_olt_id = getattr(config_pack, "olt_id", None)

    mgmt_ip_pool_ctx = management_ip_choices_for_ont(db, ont)
    active_assignment = db.scalar(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
    )
    ip_block_choices = active_catalog_ip_block_choices(db)
    ip_block_entitlements = (
        subscriber_ip_block_entitlements(db, active_assignment.subscriber_id)
        if active_assignment is not None and active_assignment.subscriber_id is not None
        else ()
    )
    current_block_prefix = IpBlockPrefix.from_mask(str(lan_subnet or ""))
    if current_block_prefix is None and len(ip_block_entitlements) == 1:
        current_block_prefix = ip_block_entitlements[0].prefix
    return {
        "ont": ont,
        "ont_id": ont_id,
        "wan_mode": values.get("wan_mode"),
        "ip_protocol": values.get("ip_protocol"),
        "wan_static_ip": str(values.get("wan_static_ip") or ""),
        "wan_static_subnet": str(values.get("wan_static_subnet") or ""),
        "wan_static_gateway": str(values.get("wan_static_gateway") or ""),
        "wan_static_dns": str(values.get("wan_static_dns") or ""),
        "pppoe_username": str(values.get("pppoe_username") or ""),
        "wan_vlan": values.get("wan_vlan"),
        "mgmt_ip_mode": mgmt_mode_value,
        "mgmt_ip_address": str(mgmt_ip or ""),
        "mgmt_remote_access": mgmt_remote_access,
        "mgmt_vlan": values.get("mgmt_vlan"),
        "lan_gateway_ip": str(lan_gateway or ""),
        "lan_subnet_mask": str(lan_subnet or ""),
        "lan_block_prefix": current_block_prefix.value if current_block_prefix else "",
        "ip_block_choices": ip_block_choices,
        "ip_block_entitlements": ip_block_entitlements,
        "entitled_ip_block_prefixes": tuple(
            entitlement.prefix.value for entitlement in ip_block_entitlements
        ),
        "lan_dhcp_enabled": lan_dhcp_enabled,
        "lan_dhcp_start": str(lan_dhcp_start or ""),
        "lan_dhcp_end": str(lan_dhcp_end or ""),
        "wifi_enabled": wifi_enabled,
        "wifi_ssid": str(wifi_ssid or ""),
        "wifi_channel": str(wifi_channel or ""),
        "wifi_security_mode": str(wifi_security or ""),
        "config_pack_name": config_pack_name,
        "config_pack_olt_id": config_pack_olt_id,
        "tr069_profile_id": tr069_profile_id,
        "tr069_profile_name": tr069_profile_name,
        "has_tr069": has_tr069,
        "acs_last_inform": acs_last_inform,
        "configure_readiness": OntConfigureReadiness(
            olt_assigned=bool(getattr(ont, "olt_device_id", None)),
            config_pack_ready=config_pack is not None,
            acs_registered=has_tr069,
        ),
        "mgmt_ip_pool": mgmt_ip_pool_ctx.get("mgmt_ip_pool"),
        "available_mgmt_ips": mgmt_ip_pool_ctx.get("available_mgmt_ips", []),
        "mgmt_ip_choice_message": mgmt_ip_pool_ctx.get("mgmt_ip_choice_message"),
        # WANConnectionDevice index overrides — surface both the resolved
        # value (effective) and the inherited default so the template can
        # render "(inherit: N)" labels on the dropdown.
        "pppoe_wcd_index": values.get("pppoe_wcd_index"),
        "mgmt_wcd_index": values.get("mgmt_wcd_index"),
        "voip_wcd_index": values.get("voip_wcd_index"),
        "pppoe_wcd_index_default": values.get("pppoe_wcd_index_default"),
        "mgmt_wcd_index_default": values.get("mgmt_wcd_index_default"),
        "voip_wcd_index_default": values.get("voip_wcd_index_default"),
        "pppoe_wcd_index_override": values.get("pppoe_wcd_index_override"),
        "mgmt_wcd_index_override": values.get("mgmt_wcd_index_override"),
        "voip_wcd_index_override": values.get("voip_wcd_index_override"),
        # OLT service-port indices — operator override (None = allocator
        # picks at first provision; immutable post-allocation per validator).
        "mgmt_service_port_index": values.get("mgmt_service_port_index"),
        "wan_service_port_index": values.get("wan_service_port_index"),
    }


def _observed_config_freshness(
    observed_intent: dict[str, object],
) -> dict[str, object] | None:
    if not observed_intent:
        return None
    fetched_at = observed_intent.get("fetched_at")
    source = str(observed_intent.get("source") or "db")
    return {
        "source": source,
        "fetched_at": fetched_at,
        "stale": source != "live",
    }


def _service_port_value(port: object, field: str) -> object | None:
    if isinstance(port, dict):
        return port.get(field)
    return getattr(port, field, None)


def _tr069_value(node: object, *path: str) -> object | None:
    current = node
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, dict) and "_value" in current:
        return current.get("_value")
    return current


def _first_wan_ppp(raw_device: dict[str, object]) -> dict[str, object] | None:
    wcd_root = _tr069_value(
        raw_device,
        "InternetGatewayDevice",
        "WANDevice",
        "1",
        "WANConnectionDevice",
    )
    if not isinstance(wcd_root, dict):
        return None
    for wcd_key in sorted(str(key) for key in wcd_root if str(key).isdigit()):
        wcd = wcd_root.get(wcd_key)
        if not isinstance(wcd, dict):
            continue
        ppp_root = wcd.get("WANPPPConnection")
        if not isinstance(ppp_root, dict):
            continue
        for ppp_key in sorted(str(key) for key in ppp_root if str(key).isdigit()):
            ppp = ppp_root.get(ppp_key)
            if isinstance(ppp, dict):
                return {"wcd_index": wcd_key, "ppp_index": ppp_key, "data": ppp}
    return None


def _truthy_acs_int(value: object) -> bool:
    try:
        return int(str(value or "0")) > 0
    except (TypeError, ValueError):
        return False


def _values_match(left: object, right: object) -> bool:
    return str(left or "").strip() == str(right or "").strip()


def _recovery_row(
    label: str,
    status: str,
    message: str,
    *,
    detail: str | None = None,
) -> dict[str, str | None]:
    classes = {
        "ok": {
            "dot": "bg-emerald-500",
            "badge": "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300",
        },
        "warn": {
            "dot": "bg-amber-500",
            "badge": "bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300",
        },
        "fail": {
            "dot": "bg-rose-500",
            "badge": "bg-rose-50 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300",
        },
        "pending": {
            "dot": "bg-slate-400",
            "badge": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
        },
    }
    style = classes.get(status, classes["pending"])
    return {
        "label": label,
        "status": status,
        "status_label": status.title(),
        "message": message,
        "detail": detail,
        "dot_class": style["dot"],
        "badge_class": style["badge"],
    }


def _drift_row(
    label: str,
    saved_value: object,
    device_value: object,
    *,
    pending_message: str,
) -> dict[str, object]:
    saved = str(saved_value or "").strip()
    device = str(device_value or "").strip()
    if saved and device and _values_match(saved, device):
        status = "ok"
        message = "Saved value matches the device."
    elif saved and device:
        status = "warn"
        message = "Saved value does not match the device."
    elif saved and not device:
        status = "pending"
        message = pending_message
    elif device:
        status = "warn"
        message = "Device has a value, but the app has no saved value."
    else:
        status = "pending"
        message = "No saved value or device value is available yet."
    return {
        **_recovery_row(label, status, message),
        "saved_value": saved or "-",
        "device_value": device or "-",
    }


def _drift_state_row(
    label: str,
    status: str,
    message: str,
    *,
    saved_value: str,
    device_value: str,
    detail: str | None = None,
) -> dict[str, object]:
    return {
        **_recovery_row(label, status, message, detail=detail),
        "saved_value": saved_value,
        "device_value": device_value,
    }


def _service_recovery_context(
    db: Session,
    ont: object,
    *,
    desired_wan: dict[str, object],
    service_ports_context: dict[str, object],
    olt_status: dict[str, object],
    has_tr069_device: bool,
) -> dict[str, object]:
    """Build a compact operator checklist for ONT internet recovery."""
    from app.models.radius_active_session import RadiusActiveSession

    expected_username = str(desired_wan.get("pppoe_username") or "").strip()
    expected_wan_vlan = str(desired_wan.get("wan_vlan") or "").strip()
    rows: list[dict[str, str | None]] = []

    entry = olt_status.get("entry") or {}
    run_state = str(entry.get("run_state") or entry.get("olt_status") or "").lower()
    config_state = str(entry.get("config_state") or "").lower()
    match_state = str(entry.get("match_state") or "").lower()
    if bool(olt_status.get("deferred")):
        rows.append(
            _recovery_row(
                "OLT registration",
                "pending",
                "Live OLT registration was not checked on this page.",
                detail="Open OLT Status to run the live OLT check; this pending state does not mean the OLT is down.",
            )
        )
    elif run_state == "online" and config_state == "normal" and match_state == "match":
        rows.append(
            _recovery_row(
                "OLT registration",
                "ok",
                "ONT is online, config normal, and profile matched.",
            )
        )
    else:
        rows.append(
            _recovery_row(
                "OLT registration",
                "fail" if run_state and run_state != "online" else "warn",
                "OLT registration is not fully healthy.",
                detail=f"run={run_state or 'unknown'}, config={config_state or 'unknown'}, match={match_state or 'unknown'}",
            )
        )

    ports = list(service_ports_context.get("service_ports") or [])
    ports_deferred = bool(service_ports_context.get("deferred"))
    port_error = str(service_ports_context.get("error") or "").strip()
    observed_vlans = {
        str(_service_port_value(port, "vlan_id") or "")
        for port in ports
        if _service_port_value(port, "vlan_id") not in (None, "")
    }
    if ports_deferred:
        rows.append(
            _recovery_row(
                "Internet service-port",
                "pending",
                "Live service-port data was not checked on this page.",
                detail="Open Service Ports to read VLAN/GEM state from the OLT; this pending state does not mean the path is missing.",
            )
        )
    elif port_error:
        rows.append(_recovery_row("Internet service-port", "fail", port_error))
    elif expected_wan_vlan and expected_wan_vlan in observed_vlans:
        rows.append(
            _recovery_row(
                "Internet service-port",
                "ok",
                f"Expected internet VLAN {expected_wan_vlan} is present.",
            )
        )
    else:
        rows.append(
            _recovery_row(
                "Internet service-port",
                "fail",
                "Expected internet VLAN is missing from observed service-ports.",
                detail=f"Expected VLAN {expected_wan_vlan or 'not set'}.",
            )
        )

    rows.append(
        _recovery_row(
            "ACS inform",
            "ok" if has_tr069_device else "warn",
            "TR-069 device has informed."
            if has_tr069_device
            else "No ACS device has informed yet.",
        )
    )

    snapshot = getattr(ont, "tr069_last_snapshot", None)
    raw_device = snapshot.get("raw_device") if isinstance(snapshot, dict) else None
    raw_device = raw_device if isinstance(raw_device, dict) else {}
    ppp = _first_wan_ppp(raw_device)
    ppp_data = ppp.get("data") if isinstance(ppp, dict) else None
    ppp_data = ppp_data if isinstance(ppp_data, dict) else {}
    ppp_status = str(_tr069_value(ppp_data, "ConnectionStatus") or "").strip()
    ppp_ip = str(_tr069_value(ppp_data, "ExternalIPAddress") or "").strip()
    ppp_username = str(_tr069_value(ppp_data, "Username") or "").strip()
    ppp_vlan = str(_tr069_value(ppp_data, "X_HW_VLAN") or "").strip()
    ppp_connected = ppp_status.lower() == "connected" and bool(ppp_ip)
    ppp_object_present = bool(ppp_data)
    drift_rows = [
        _drift_row(
            "PPPoE username",
            expected_username,
            ppp_username,
            pending_message="ACS has not shown the PPPoE username yet.",
        ),
        _drift_row(
            "Internet VLAN",
            expected_wan_vlan,
            ppp_vlan,
            pending_message="ACS has not shown the WAN VLAN yet.",
        ),
    ]
    if bool(olt_status.get("deferred")):
        drift_rows.append(
            _drift_state_row(
                "Live OLT state",
                "pending",
                "This page is using cached OLT data until you open the live OLT view.",
                saved_value="Cached",
                device_value="Not loaded",
            )
        )
    elif match_state and match_state != "match":
        drift_rows.append(
            _drift_state_row(
                "Live OLT state",
                "warn",
                "OLT says the ONT config does not match the expected profile.",
                saved_value="Expected match",
                device_value=match_state or "Mismatch",
                detail="Resolve the OLT mismatch before replacing the ONT.",
            )
        )
    else:
        drift_rows.append(
            _drift_state_row(
                "Live OLT state",
                "ok",
                "OLT state matches the app view.",
                saved_value="Expected match",
                device_value="Match",
            )
        )

    if ppp_connected:
        rows.append(
            _recovery_row(
                "PPP WAN",
                "ok",
                f"PPPoE is connected with IP {ppp_ip}.",
                detail=f"user={ppp_username or 'unknown'}, vlan={ppp_vlan or 'unknown'}",
            )
        )
    elif ppp_data:
        rows.append(
            _recovery_row(
                "PPP WAN",
                "warn",
                "PPP WAN exists but is not connected.",
                detail=f"status={ppp_status or 'unknown'}, ip={ppp_ip or '-'}",
            )
        )
    else:
        rows.append(
            _recovery_row(
                "PPP WAN",
                "fail",
                "No WANPPPConnection is visible from ACS.",
                detail="Create or activate the PPPoE internet WAN before replacing hardware.",
            )
        )

    radius_stmt = select(RadiusActiveSession).where(False)
    if expected_username:
        radius_stmt = select(RadiusActiveSession).where(
            RadiusActiveSession.username == expected_username
        )
    active_radius = db.scalars(
        radius_stmt.order_by(
            RadiusActiveSession.last_update.desc().nullslast(),
            RadiusActiveSession.session_start.desc(),
        )
    ).first()
    if active_radius:
        counters = int(active_radius.bytes_in or 0) + int(active_radius.bytes_out or 0)
        radius_detail = (
            "No traffic counters yet; check LAN/WiFi bind and customer device. "
            "A ping timeout alone does not prove the customer cannot browse."
            if counters == 0
            else (
                f"{counters} bytes counted. PPP/RADIUS is online; a ping timeout "
                "can be caused by ICMP being blocked and does not by itself mean browsing is down."
            )
        )
        rows.append(
            _recovery_row(
                "RADIUS session",
                "ok" if counters > 0 else "warn",
                f"Active session on {active_radius.framed_ip_address or 'unknown IP'}.",
                detail=radius_detail,
            )
        )
    else:
        rows.append(
            _recovery_row(
                "RADIUS session",
                "fail",
                "No active PPPoE/RADIUS session for the expected username.",
                detail=f"Expected username {expected_username or 'not set'}.",
            )
        )

    lanbind = ppp_data.get("X_HW_LANBIND") if isinstance(ppp_data, dict) else None
    lanbind = lanbind if isinstance(lanbind, dict) else {}
    bind_labels = ["Lan1", "Lan2", "Lan3", "Lan4", "SSID1", "SSID2", "SSID3", "SSID4"]
    enabled_binds = [
        label
        for label in bind_labels
        if _truthy_acs_int(_tr069_value(lanbind, f"{label}Enable"))
    ]
    if not ppp_data:
        rows.append(
            _recovery_row(
                "LAN/WiFi bind",
                "pending",
                "Binding cannot be checked yet because the internet WAN is missing.",
                detail="Create or apply the PPPoE WAN first. Bind Internet WAN cannot create it.",
            )
        )
    elif enabled_binds:
        rows.append(
            _recovery_row(
                "LAN/WiFi bind",
                "ok",
                "PPP WAN is bound to customer-facing interfaces.",
                detail=", ".join(enabled_binds),
            )
        )
    else:
        rows.append(
            _recovery_row(
                "LAN/WiFi bind",
                "warn",
                "PPP WAN is not bound to LAN ports or SSIDs.",
                detail="Bind the internet WAN to SSID1 and required LAN ports.",
            )
        )

    severity_rank = {"fail": 3, "warn": 2, "pending": 1, "ok": 0}
    worst = max((str(row["status"]) for row in rows), key=lambda s: severity_rank[s])
    drift_worst = max(
        (str(row["status"]) for row in drift_rows),
        key=lambda s: severity_rank[s],
    )
    bind_action_enabled = ppp_connected
    bind_action_reason = "Select SSID/LAN ports, then bind the connected internet WAN."
    recovery_stage = {
        "title": "No recovery action needed",
        "message": "The checklist does not see a service recovery blocker.",
        "action_label": "",
        "action_hint": "",
        "tone": "ok",
    }

    if not ppp_object_present:
        bind_action_enabled = False
        bind_action_reason = (
            "Disabled because the ONT does not show an internet WAN object yet."
        )
        recovery_stage = {
            "title": "Internet WAN object is missing",
            "message": (
                "The ONT does not currently show a PPPoE internet WAN service. "
                "There is nothing for the Bind button to attach to WiFi or LAN."
            ),
            "action_label": "Apply WAN",
            "action_hint": (
                "Use Apply WAN in the WAN section above, then wait for ACS to refresh "
                "until the PPP WAN appears."
            ),
            "tone": "fail",
        }
    elif not ppp_connected:
        bind_action_enabled = False
        bind_action_reason = "Disabled because the PPP WAN exists but is not connected."
        recovery_stage = {
            "title": "Internet WAN exists but has not connected",
            "message": (
                "The ONT has a PPPoE WAN object, but it has not dialed successfully yet. "
                "Binding ports now will not bring the customer online."
            ),
            "action_label": "Check PPPoE/RADIUS",
            "action_hint": (
                "Confirm PPPoE username, password, VLAN, RADIUS account, and ACS refresh state."
            ),
            "tone": "warn",
        }
    elif not enabled_binds:
        recovery_stage = {
            "title": "Internet WAN is connected but not attached to customer ports",
            "message": (
                "The ONT has an active internet WAN. Bind it to SSID1 and the required "
                "LAN ports so customer devices can pass traffic."
            ),
            "action_label": "Bind Internet WAN",
            "action_hint": "Select the customer-facing WiFi/LAN ports below, then click Bind Internet WAN.",
            "tone": "warn",
        }

    common_issues = [
        {
            "label": "No ACS inform",
            "meaning": "The ONT has not checked in, so TR-069 changes may not reach it yet.",
        },
        {
            "label": "Missing service-port",
            "meaning": "The OLT path for the internet VLAN is not present, so traffic cannot pass.",
        },
        {
            "label": "WAN object missing",
            "meaning": "The ONT has no PPPoE internet service object; Apply WAN must run first.",
        },
        {
            "label": "PPP not connected",
            "meaning": "The WAN exists, but PPPoE/RADIUS has not accepted the session.",
        },
        {
            "label": "WAN not bound",
            "meaning": "Internet is active on the ONT but not attached to WiFi or Ethernet ports.",
        },
        {
            "label": "Ping timeout",
            "meaning": "Many CPEs block ICMP. Use RADIUS/PPP state and traffic counters before deciding the customer is offline.",
        },
        {
            "label": "App/device drift",
            "meaning": "The saved config differs from what ACS or the OLT last saw on the device.",
        },
    ]

    next_action = "No recovery action needed from this checklist."
    if worst == "fail":
        failed = next(row for row in rows if row["status"] == "fail")
        next_action = str(failed["message"])
    elif worst == "warn":
        warning = next(
            (
                row
                for row in rows
                if row["status"] == "warn" and row["label"] == "LAN/WiFi bind"
            ),
            None,
        ) or next(row for row in rows if row["status"] == "warn")
        next_action = str(warning["message"])
    elif worst == "pending":
        next_action = "Load deferred OLT/ACS reads before deciding on replacement."

    return {
        "service_recovery": {
            "rows": rows,
            "status": worst,
            "next_action": next_action,
            "recovery_stage": recovery_stage,
            "common_issues": common_issues,
            "bind_action_enabled": bind_action_enabled,
            "bind_action_reason": bind_action_reason,
            "ppp_object_present": ppp_object_present,
            "ppp_connected": ppp_connected,
            "drift_rows": drift_rows,
            "drift_status": drift_worst,
            "pppoe_username": expected_username,
            "wan_vlan": expected_wan_vlan,
        }
    }


def _operator_summary_context(
    *,
    desired_mgmt: dict[str, object],
    desired_wan: dict[str, object],
    service_ports_context: dict[str, object],
    olt_status: dict[str, object],
    has_tr069_device: bool,
    current_tr069_profile: str | None,
) -> dict[str, object]:
    ports = list(service_ports_context.get("service_ports") or [])
    port_rows: list[dict[str, object]] = []
    observed_vlans: set[str] = set()
    up_count = 0
    for port in ports:
        vlan_id = _service_port_value(port, "vlan_id")
        gem_index = _service_port_value(port, "gem_index")
        flow_type = str(_service_port_value(port, "flow_type") or "").strip()
        flow_para = str(_service_port_value(port, "flow_para") or "").strip()
        state = str(_service_port_value(port, "state") or "").strip().lower()
        if vlan_id not in (None, ""):
            observed_vlans.add(str(vlan_id))
        if state == "up":
            up_count += 1
        port_rows.append(
            {
                "index": _service_port_value(port, "index") or "—",
                "vlan_id": str(vlan_id or "—"),
                "gem_index": str(gem_index or "—"),
                "flow_label": " ".join(
                    part for part in [flow_type, flow_para] if part
                ).strip()
                or "—",
                "state": state or "unknown",
            }
        )

    entry = olt_status.get("entry") or {}
    blockers: list[dict[str, str]] = []
    service_ports_error = str(service_ports_context.get("error") or "").strip()
    service_ports_deferred = bool(service_ports_context.get("deferred"))
    service_ports_loaded = not service_ports_deferred and not service_ports_error
    if service_ports_error and not service_ports_deferred:
        blockers.append(
            {
                "severity": "critical",
                "message": f"Service-port read failed: {service_ports_error}",
            }
        )
    elif service_ports_loaded and not port_rows:
        blockers.append(
            {
                "severity": "critical",
                "message": "No service-ports are present on the OLT for this ONT. Management and subscriber VLAN paths cannot pass traffic.",
            }
        )

    desired_mgmt_vlan = str(desired_mgmt.get("vlan_id") or "").strip()
    desired_wan_vlan = str(desired_wan.get("wan_vlan") or "").strip()
    if (
        service_ports_loaded
        and desired_mgmt_vlan
        and desired_mgmt_vlan not in observed_vlans
    ):
        blockers.append(
            {
                "severity": "critical",
                "message": f"Expected management VLAN {desired_mgmt_vlan} is missing from OLT service-ports.",
            }
        )
    if (
        service_ports_loaded
        and desired_wan_vlan
        and desired_wan_vlan not in observed_vlans
    ):
        blockers.append(
            {
                "severity": "critical",
                "message": f"Expected internet VLAN {desired_wan_vlan} is missing from OLT service-ports.",
            }
        )

    match_state = str(entry.get("match_state") or "").strip().lower()
    if match_state and match_state != "match":
        blockers.append(
            {
                "severity": "warning",
                "message": f"OLT reports match state '{entry.get('match_state')}', which indicates config drift on the ONT.",
            }
        )

    if not has_tr069_device:
        blockers.append(
            {
                "severity": "warning",
                "message": "No ACS device has informed yet. If management path is expected, treat this as incomplete provisioning until first Inform arrives.",
            }
        )

    if not current_tr069_profile:
        blockers.append(
            {
                "severity": "warning",
                "message": "No effective TR-069 profile is resolved for this ONT.",
            }
        )

    if bool(olt_status.get("deferred")):
        olt_status_rows = [
            ("F/S/P", entry.get("fsp") or "—"),
            ("ONT-ID", entry.get("ont_id") or "—"),
            ("Description", entry.get("description") or "—"),
            ("TR-069 Profile", current_tr069_profile or "—"),
            ("ACS Device", "Informed" if has_tr069_device else "Not informed"),
        ]
    else:
        olt_status_rows = [
            ("Run State", entry.get("run_state") or entry.get("olt_status") or "—"),
            ("Config State", entry.get("config_state") or "—"),
            ("Match State", entry.get("match_state") or "—"),
            ("F/S/P", entry.get("fsp") or "—"),
            ("ONT-ID", entry.get("ont_id") or "—"),
            ("Description", entry.get("description") or "—"),
            ("TR-069 Profile", current_tr069_profile or "—"),
            ("ACS Device", "Informed" if has_tr069_device else "Not informed"),
        ]

    return {
        "operator_summary": {
            "blockers": blockers,
            "service_ports": port_rows,
            "service_ports_error": service_ports_error or None,
            "service_ports_count": len(port_rows),
            "service_ports_up_count": up_count,
            "service_ports_deferred": service_ports_deferred,
            "observed_vlans": sorted(observed_vlans),
            "olt_status_rows": olt_status_rows,
        }
    }


def unified_config_context(
    db: Session,
    ont_id: str,
    detail_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build context for the unified ONT configuration partial from DB state."""
    shared = _load_ont_detail_config_state(db, ont_id, detail_payload=detail_payload)
    ont = shared["ont"]
    linked_tr069 = shared["linked_tr069"]
    observed_state = shared["observed_state"]
    ont_plan = shared["ont_plan"]
    acs_observed_intent = shared["acs_observed_intent"]
    service_ports_context = {
        "ont": ont,
        "olt": None,
        "fsp": None,
        "olt_ont_id": None,
        "service_ports": [],
        "vlan_chain": None,
        "reference_onts": [],
        "service_port_intent": {},
        "vlans": observed_state["vlans"],
        "error": "Live OLT service-port read deferred. Open the dedicated service-port view to load current OLT state.",
        "deferred": True,
    }
    olt_status = {
        "result": {
            "success": False,
            "message": "Live OLT status read deferred during detail page render.",
        },
        "entry": {
            "fsp": f"{getattr(ont, 'board', '')}/{getattr(ont, 'port', '')}".strip("/"),
            "ont_id": getattr(ont, "external_id", None) or "—",
            "serial_number": getattr(ont, "serial_number", None) or "—",
            "description": getattr(ont, "name", None)
            or getattr(ont, "description", None)
            or "—",
        },
        "run_state": "",
        "rows": [
            ("Run State", "Deferred"),
            ("Config State", "Deferred"),
            ("Match State", "Deferred"),
            ("Serial", getattr(ont, "serial_number", None) or "—"),
            (
                "F/S/P",
                f"{getattr(ont, 'board', '')}/{getattr(ont, 'port', '')}".strip("/")
                or "—",
            ),
            ("ONT-ID", getattr(ont, "external_id", None) or "—"),
            ("Last Down Cause", "Deferred"),
            ("Last Down Time", "Deferred"),
            ("Last Up Time", "Deferred"),
            (
                "Description",
                getattr(ont, "name", None) or getattr(ont, "description", None) or "—",
            ),
        ],
        "deferred": True,
    }
    desired_context = _desired_config_context(
        db,
        ont,
        ont_plan=ont_plan,
        initial_iphost_form=observed_state["initial_form"],
    )
    current_profile = _resolve_cached_tr069_profile(
        db,
        ont,
        list(observed_state["tr069_profiles"]),
        effective=desired_context["effective_config"],
    )
    current_profile_name = getattr(current_profile, "profile_name", None) or getattr(
        current_profile, "name", None
    )
    observed = acs_observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    wan = observed.get("wan", {}) if isinstance(observed.get("wan"), dict) else {}
    wireless = (
        observed.get("wifi", {}) if isinstance(observed.get("wifi"), dict) else {}
    )
    lan = observed.get("lan", {}) if isinstance(observed.get("lan"), dict) else {}

    context = {
        "ont_id": ont_id,
        "ont": ont,
        "ont_plan": ont_plan,
        "tr069_available": bool(acs_observed_intent.get("available")),
        "iphost_config": observed_state["iphost_config"],
        "iphost_ok": True,
        "iphost_msg": observed_state["iphost_result"].message,
        "iphost_freshness": observed_state["iphost_result"].freshness,
        "initial_iphost_form": observed_state["initial_form"],
        "vlans": observed_state["vlans"],
        "tr069_profiles": observed_state["tr069_profiles"],
        "tr069_profiles_error": observed_state["tr069_profiles_error"],
        "tr069_profiles_freshness": observed_state["profiles_result"].freshness,
        "has_tr069": bool(
            linked_tr069 and str(getattr(linked_tr069, "genieacs_device_id", "") or "")
        ),
        "wan_info": wan,
        "wireless_info": wireless,
        "lan_info": lan,
        "ethernet_ports": observed.get("ethernet_ports", []),
        "lan_hosts": observed.get("lan_hosts", []),
        "observed_config_freshness": _observed_config_freshness(acs_observed_intent),
        "current_pppoe_user": wan.get("pppoe_username"),
        "current_ssid": wireless.get("ssid"),
        "current_profile": current_profile_name,
        "current_profile_id": getattr(current_profile, "profile_id", None),
        "current_tr069_profile": current_profile_name,
        "tr069_periodic_inform_interval": settings.tr069_periodic_inform_interval,
        "service_ports_context": service_ports_context,
        "olt_status": olt_status,
    }
    context.update(desired_context)

    effective_config = context.get("effective_config", {})
    context.update(
        _configure_form_context_from_state(
            db,
            ont,
            ont_id,
            effective=effective_config,
            linked_tr069=linked_tr069,
        )
    )
    context["effective_values"] = (
        effective_config.get("values", {}) if isinstance(effective_config, dict) else {}
    )
    context.update(
        _unified_summary_context(
            desired_mgmt=context["desired_mgmt_config"],
            desired_wan=context["desired_wan_config"],
            desired_wifi=context["desired_wifi_config"],
            acs_observed_intent=acs_observed_intent,
        )
    )
    context.update(
        _operator_summary_context(
            desired_mgmt=context["desired_mgmt_config"],
            desired_wan=context["desired_wan_config"],
            service_ports_context=service_ports_context,
            olt_status=olt_status,
            has_tr069_device=context["has_tr069"],
            current_tr069_profile=current_profile_name,
        )
    )
    context.update(
        _service_recovery_context(
            db,
            ont,
            desired_wan=context["desired_wan_config"],
            service_ports_context=service_ports_context,
            olt_status=olt_status,
            has_tr069_device=context["has_tr069"],
        )
    )
    desired_wan = context["desired_wan_config"]
    desired_wifi = context["desired_wifi_config"]
    context["current_pppoe_user"] = context["current_pppoe_user"] or desired_wan.get(
        "pppoe_username"
    )
    context["current_ssid"] = context["current_ssid"] or desired_wifi.get("ssid")
    return context


def configure_form_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build context for the ONT service configure form."""
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    effective = resolve_effective_ont_config(db, ont)
    return _configure_form_context_from_state(
        db,
        ont,
        ont_id,
        effective=effective,
    )


def olt_side_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for OLT-side ONT config."""
    from app.services.web_network_ont_actions.operational import fetch_olt_side_config

    result = fetch_olt_side_config(db, ont_id)
    section_labels = {
        "ont_info": "ONT Info",
        "ont_wan": "WAN Info",
        "service_ports": "Service Ports",
    }
    sections = []
    for key, label in section_labels.items():
        content = (result.data or {}).get(key) if result.success else None
        if content:
            sections.append({"key": key, "label": label, "content": content})
    return {"result": result, "sections": sections}


def olt_status_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for OLT-side ONT status."""
    from app.services.web_network_ont_actions.operational import fetch_olt_status

    result = fetch_olt_status(db, ont_id)
    entry = result.get("entry") or {}
    raw_run_state = str(entry.get("run_state") or entry.get("olt_status") or "").lower()
    run_state = "" if raw_run_state == "unknown" else raw_run_state
    rows = [
        (
            "Run State",
            _display_olt_value(entry.get("run_state") or entry.get("olt_status")),
        ),
        ("Config State", _display_olt_value(entry.get("config_state"))),
        ("Match State", _display_olt_value(entry.get("match_state"))),
        ("Serial", _display_olt_value(entry.get("serial_number"))),
        ("F/S/P", entry.get("fsp") or "—"),
        ("ONT-ID", entry.get("ont_id") or "—"),
        ("Last Down Cause", entry.get("last_down_cause") or "—"),
        ("Last Down Time", entry.get("last_down_time") or "—"),
        ("Last Up Time", entry.get("last_up_time") or "—"),
        ("Description", entry.get("description") or "—"),
    ]
    return {
        "result": result,
        "entry": entry,
        "run_state": run_state,
        "rows": rows,
    }
