"""DB ↔ in-memory dataclass adapters for the reconciler.

Bridges the typed ``OntDesiredState`` / ``OntObservedState`` dataclasses (see
``state.py``) and the existing SQLAlchemy ORM (``OntUnit``, ``OntObservation``,
plus the resolved-effective-config helper in
``app.services.network.effective_ont_config``).

Four functions:

* ``desired_from_ont_unit(db, ont)`` — read current desired state.
* ``apply_proposed_change(ont, target)`` — write a successful reconcile's
  desired-state mutation back to ``OntUnit`` (structural columns + the
  ``desired_config`` JSON blob accessor properties already on the model).
* ``observed_from_ont_observation(obs)`` — read the 1:1 observation row.
* ``upsert_ont_observation(db, ont_unit_id, observed)`` — persist a fresh
  ``OntObservedState`` (INSERT or UPDATE).

Field provenance — most config knobs come from the existing
``resolve_effective_ont_config`` (which already composes ``OntUnit.desired_config``
with ``OltConfigPack`` defaults and per-OLT-equipment profile mappings).
Identity, reconciler bookkeeping, and ACS-server FK come from typed columns on
``OntUnit`` directly. A handful of fields are not yet sourced from the existing
model and use sensible defaults — they're marked with ``DEFAULT:`` comments
and will be filled in by follow-up commits when their sources land.

This module is read by the reader/planner/applier code in subsequent commits;
no production code path calls it yet.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.models.ont_observation import OntObservation
from app.services.control_plane_intent import (
    DesiredValueProvenance,
    has_executable_desired_provenance,
)
from app.services.credential_crypto import encrypt_credential
from app.services.network.acs_resolution import resolve_acs_for_ont
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_provisioning_defaults import (
    default_pppoe_instance_index,
)
from app.services.network.serial_utils import parse_ont_id_on_olt
from app.services.network.tr069_paths import Tr069PathError, tr069_path_resolver
from app.services.network.vendor_capabilities import VendorCapabilities

from .state import (
    AcsObservedFields,
    OltObservedFields,
    OntDesiredState,
    OntObservedState,
    Tr069RemoteAccessParameterPaths,
    Tr069WifiParameterPaths,
    Tr181WanParameterPaths,
)

_FSP_RE = re.compile(r"^\d+/\d+/\d+$")


# ── Desired state ───────────────────────────────────────────────────────────


def desired_from_ont_unit(db: Session, ont: OntUnit) -> OntDesiredState:
    """Materialise the current ``OntDesiredState`` for one ONT.

    The bulk of the configuration comes from
    ``resolve_effective_ont_config(db, ont)``, which composes
    ``OntUnit.desired_config`` (the per-ONT JSON blob with accessor properties)
    with ``OltConfigPack`` defaults and the per-OLT-equipment
    ``OltOnuTypeProfileMapping``. Identity and reconciler-bookkeeping fields
    come from typed columns on ``OntUnit``.

    A few fields have ``DEFAULT:`` placeholder values because their sources are
    not yet wired into the effective-config helper — those land in follow-up
    commits as the readers / planner need them.
    """
    effective = resolve_effective_ont_config(db, ont)
    values: dict[str, Any] = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    desired_config_keys = frozenset(
        str(key)
        for key in (
            effective.get("desired_config_keys", ())
            if isinstance(effective, dict)
            else ()
        )
    )
    raw_ip_protocol = values.get("ip_protocol")
    ip_protocol = (
        str(raw_ip_protocol).strip().lower() if raw_ip_protocol is not None else ""
    )
    ipv6_provenance = (
        DesiredValueProvenance.explicit
        if "wan.ip_protocol" in desired_config_keys
        and ip_protocol in {"ipv4", "dual_stack"}
        else DesiredValueProvenance.unknown
    )

    fsp = _fsp_from_ont(ont)
    olt_ont_id = parse_ont_id_on_olt(ont.external_id) or 0
    # The effective-config helper exposes two related fields: ``wan_mode`` is
    # the IP-mode (``pppoe``/``bridge``/``static_ip``/...) and ``onu_mode`` is
    # the ONU operating mode (``routing``/``bridging``). Either being "bridge"
    # or "bridging" means the reconciler treats the WAN as bridged.
    wan_mode = _normalise_wan_mode(values.get("wan_mode"), values.get("onu_mode"))
    acs_resolution = resolve_acs_for_ont(db, ont)
    acs_server = acs_resolution.server
    tr181_wan_paths = _resolve_tr181_wan_paths(db, ont, wan_mode, values)
    tr069_data_model_root = _resolve_tr069_root(db, ont, tr181_wan_paths)
    wifi_paths = _resolve_wifi_paths(db, ont, tr069_data_model_root)
    remote_access_paths = _resolve_remote_access_paths(db, ont, tr069_data_model_root)
    access = dict((ont.desired_config or {}).get("access") or {})
    remote_expires_at = _datetime_or_none(access.get("wan_remote_expires_at"))
    remote_requested = _bool_or_default(
        access.get("wan_remote", values.get("wan_remote_access")), default=False
    )
    remote_enabled = remote_requested and (
        remote_expires_at is not None and remote_expires_at > datetime.now(UTC)
    )

    return OntDesiredState(
        ont_unit_id=str(ont.id),
        serial_number=ont.serial_number,
        olt_id=str(ont.olt_device_id) if ont.olt_device_id else "",
        fsp=fsp,
        olt_ont_id=int(olt_ont_id),
        line_profile_id=int(values.get("authorization_line_profile_id") or 0),
        service_profile_id=int(values.get("authorization_service_profile_id") or 0),
        # DEFAULT: description isn't computed by effective_ont_config; the
        # reconciler's initial value falls back to a serial-stamped stub
        # (the same one Fix #7 emits at ont add time). When subscriber
        # binding lands the reconciler will recompute on assignment changes.
        description=_default_description(ont),
        mgmt_vlan=_int_or_none(values.get("mgmt_vlan")),
        mgmt_ip=values.get("mgmt_ip_address"),
        mgmt_subnet_mask=values.get("mgmt_subnet"),
        mgmt_gateway=values.get("mgmt_gateway"),
        # DEFAULT: DNS isn't carried through effective_ont_config; fleet uses
        # 8.8.8.8 / 4.2.2.2 (per reference_ont_provisioning).
        mgmt_dns_primary="8.8.8.8",
        mgmt_dns_secondary="4.2.2.2",
        # DEFAULT: priority isn't resolved here; resolver lives in
        # iphost_priority.resolve_management_iphost_priority and is plan-time.
        mgmt_iphost_priority=2,
        tr069_profile_id=int(values.get("tr069_olt_profile_id") or 0),
        acs_server_id=acs_resolution.server_id,
        cr_username=values.get("cr_username") or "",
        cr_password_ref=values.get("cr_password") or "",
        periodic_inform_interval_sec=int(
            getattr(acs_server, "periodic_inform_interval", None) or 300
        ),
        wan_mode=wan_mode,
        wan_vlan=_int_or_none(values.get("wan_vlan")),
        wan_gem_index=_int_or_none(values.get("wan_gem_index")),
        wan_pppoe_username=values.get("pppoe_username"),
        wan_pppoe_password_ref=values.get("pppoe_password"),
        # DEFAULT: provisioning method comes from a global domain_settings
        # row read elsewhere; until the reader pass loads it, default to "auto".
        wan_pppoe_provisioning_method="auto",
        wan_pppoe_wcd_index=_int_or_none(values.get("pppoe_wcd_index")),
        wan_pppoe_instance_index=int(
            values.get("wan_instance_index") or default_pppoe_instance_index()
        ),
        wan_config_profile_id=_int_or_none(values.get("wan_config_profile_id")),
        wan_internet_config_ip_index=_int_or_none(
            values.get("internet_config_ip_index")
        ),
        nat_enabled=_resolve_nat_enabled(wan_mode, values),
        ipv6_enabled=ip_protocol == "dual_stack",
        dhcp_enabled=_bool_or_default(values.get("lan_dhcp_enabled"), default=True),
        dhcp_pool_min=values.get("lan_dhcp_start") or "192.168.100.2",
        dhcp_pool_max=values.get("lan_dhcp_end") or "192.168.100.254",
        dhcp_subnet_mask=_subnet_mask_from_lan_subnet(values.get("lan_subnet")),
        lan_gateway_ip=values.get("lan_ip"),
        wifi_ssid=values.get("wifi_ssid") or "",
        wifi_password_ref=values.get("wifi_password") or "",
        # DEFAULT: push tracking lands when the applier records WiFi pushes.
        wifi_password_pushed_at=None,
        # Per-ONT service-port index overrides (operator-set via the
        # configure form, written to ``desired_config`` under the ``olt.*``
        # section). When None, the planner allocates fresh indices on first
        # provision. The validator rejects post-allocation changes
        # (validator.py:107) so the form should disable these once the OLT
        # observation shows real indices in use.
        mgmt_service_port_index=_int_or_none(values.get("mgmt_service_port_index")),
        wan_service_port_index=_int_or_none(values.get("wan_service_port_index")),
        # DEFAULT: forward-compat for legacy-BSS -> in-app Subscriber migration.
        subscriber_external_id=ont.external_id,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
        tr069_data_model_root=tr069_data_model_root,
        wan_static_ip=values.get("wan_static_ip"),
        wan_static_subnet=values.get("wan_static_subnet"),
        wan_static_gateway=values.get("wan_static_gateway"),
        wan_static_dns=values.get("wan_static_dns"),
        wan_static_ip_is_public=_static_ip_is_public(values.get("wan_static_ip")),
        tr181_wan_paths=tr181_wan_paths,
        acs_url=getattr(acs_server, "cwmp_url", None),
        acs_username=getattr(acs_server, "cwmp_username", None),
        acs_password_ref=getattr(acs_server, "cwmp_password", None),
        wifi_enabled=_bool_or_none(values.get("wifi_enabled")),
        wifi_channel=_wifi_channel_or_none(values.get("wifi_channel")),
        wifi_security_mode=_str_or_none(values.get("wifi_security_mode")),
        wifi_paths=wifi_paths,
        wan_remote_access_enabled=remote_enabled,
        wan_remote_access_expires_at=remote_expires_at,
        wan_remote_access_source_cidrs=tuple(
            str(value) for value in access.get("wan_remote_source_cidrs") or ()
        ),
        wan_remote_access_ssh_port=22,
        remote_access_paths=remote_access_paths,
        ipv6_enabled_provenance=ipv6_provenance,
        acs_device_id=_resolve_acs_device_id(db, ont),
    )


def _resolve_acs_device_id(db: Session, ont: OntUnit) -> str | None:
    """Read this ONT's recorded GenieACS ``_id`` from its CPE record.

    ``Tr069CpeDevice`` is written by the TR-069 Inform handler
    (``app.services.tr069``) and is the authoritative record of a CPE's ACS
    identity; ``genieacs_device_id`` is uniquely indexed per active row.

    The composed fallback uses only values the Inform handler persisted
    (``oui``/``product_class``/``serial_number``) and requires all three. It is
    NOT a guess: there is no default OUI and no default ProductClass here. When
    the record is missing or incomplete this returns ``None`` and the planner
    fails closed rather than inventing an identifier.
    """
    from app.models.tr069 import Tr069CpeDevice

    row = db.scalars(
        select(Tr069CpeDevice)
        .where(Tr069CpeDevice.ont_unit_id == ont.id)
        .where(Tr069CpeDevice.is_active.is_(True))
        .limit(1)
    ).first()
    if row is None:
        return None
    recorded = (row.genieacs_device_id or "").strip()
    if recorded:
        return recorded
    oui = (row.oui or "").strip()
    product_class = (row.product_class or "").strip()
    serial = (row.serial_number or "").strip()
    if oui and product_class and serial:
        return f"{oui}-{product_class}-{serial}"
    return None


def _resolve_nat_enabled(wan_mode: str, values: dict[str, Any]) -> bool:
    if wan_mode == "bridge":
        return False
    explicit = values.get("nat_enabled")
    if explicit is not None:
        return _bool_or_default(explicit, default=True)
    return True


def _static_ip_is_public(value: object) -> bool | None:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    return address.is_global if isinstance(address, ipaddress.IPv4Address) else None


def _resolve_tr181_wan_paths(
    db: Session,
    ont: OntUnit,
    wan_mode: str,
    values: dict[str, Any],
) -> Tr181WanParameterPaths | None:
    if wan_mode not in {"dhcp", "static"}:
        return None
    vendor = str(getattr(ont, "vendor", "") or "").strip()
    model = str(getattr(ont, "model", "") or "").strip()
    firmware = str(
        getattr(ont, "firmware_version", None)
        or getattr(ont, "software_version", None)
        or ""
    ).strip()
    if not vendor or not model:
        return None
    capability = VendorCapabilities.resolve_capability(
        db,
        vendor=vendor,
        model=model,
        firmware=firmware or None,
    )
    explicit_root = getattr(ont, "tr069_data_model", None)
    if explicit_root not in (None, "Device"):
        return None
    if capability is None or capability.tr069_root != "Device":
        return None

    instance_index = int(values.get("wan_instance_index") or 1)
    canonical = {
        "ip_enable": "wan.ip.enable",
        "dhcp_enable": "wan.dhcp.enable",
        "ip_address": "wan.ip.static_address",
        "subnet_mask": "wan.ip.static_subnet",
        "gateway": "wan.gateway",
        "dns_primary": "wan.dns_primary",
        "dns_secondary": "wan.dns_secondary",
        "nat_enable": "wan.nat.enable",
        "vlan_enable": "wan.vlan.enable",
        "vlan_id": "wan.vlan.id",
    }
    try:
        resolved = {
            field: tr069_path_resolver.resolve(
                "Device",
                name,
                db=db,
                vendor=vendor,
                model=model,
                firmware=firmware or None,
                instance_index=instance_index,
            )
            for field, name in canonical.items()
        }
    except Tr069PathError:
        return None
    return Tr181WanParameterPaths(**resolved)


def _resolve_tr069_root(
    db: Session,
    ont: OntUnit,
    tr181_wan_paths: Tr181WanParameterPaths | None,
) -> str:
    explicit = str(getattr(ont, "tr069_data_model", None) or "").strip()
    if explicit in {"Device", "InternetGatewayDevice"}:
        return explicit
    if explicit.lower() == "tr181":
        return "Device"
    if explicit.lower() == "tr098":
        return "InternetGatewayDevice"
    if tr181_wan_paths is not None:
        return "Device"

    vendor = str(getattr(ont, "vendor", "") or "").strip()
    model = str(getattr(ont, "model", "") or "").strip()
    firmware = _str_or_none(
        getattr(ont, "firmware_version", None) or getattr(ont, "software_version", None)
    )
    if vendor and model:
        capability = VendorCapabilities.resolve_capability(
            db,
            vendor=vendor,
            model=model,
            firmware=firmware,
        )
        if capability and capability.tr069_root in {
            "Device",
            "InternetGatewayDevice",
        }:
            return str(capability.tr069_root)
    # Existing Huawei fleet is TR-098 unless device inventory or the model
    # catalog says otherwise. Keeping this explicit preserves current behavior.
    return "InternetGatewayDevice"


def _resolve_wifi_paths(
    db: Session,
    ont: OntUnit,
    root: str,
) -> Tr069WifiParameterPaths | None:
    vendor = _str_or_none(getattr(ont, "vendor", None))
    model = _str_or_none(getattr(ont, "model", None))
    firmware = _str_or_none(
        getattr(ont, "firmware_version", None) or getattr(ont, "software_version", None)
    )
    canonical = {
        "enabled": "wifi.enabled",
        "ssid": "wifi.ssid",
        "psk_path": "wifi.psk",
        "channel": "wifi.channel",
        "security_mode": "wifi.security_mode",
    }
    try:
        resolved = {
            field: tr069_path_resolver.resolve(
                root,
                name,
                db=db,
                vendor=vendor,
                model=model,
                firmware=firmware,
                instance_index=1,
            )
            for field, name in canonical.items()
        }
    except Tr069PathError:
        return None
    return Tr069WifiParameterPaths(**resolved)


def _resolve_remote_access_paths(
    db: Session,
    ont: OntUnit,
    root: str,
) -> Tr069RemoteAccessParameterPaths | None:
    vendor = _str_or_none(getattr(ont, "vendor", None))
    model = _str_or_none(getattr(ont, "model", None))
    firmware = _str_or_none(
        getattr(ont, "firmware_version", None) or getattr(ont, "software_version", None)
    )
    canonical = {
        "ssh_enabled": "remote_access.ssh_enabled",
        "ssh_port": "remote_access.ssh_port",
        "telnet_enabled": "remote_access.telnet_enabled",
        "telnet_port": "remote_access.telnet_port",
    }
    try:
        resolved = {
            field: tr069_path_resolver.resolve(
                root,
                name,
                db=db,
                vendor=vendor,
                model=model,
                firmware=firmware,
            )
            for field, name in canonical.items()
        }
    except Tr069PathError:
        return None
    return Tr069RemoteAccessParameterPaths(**resolved)


def apply_proposed_change(
    ont: OntUnit,
    target: OntDesiredState,
    *,
    changed_fields: frozenset[str] = frozenset(),
) -> None:
    """Write a successful proposed_change back to ``OntUnit``.

    Identity fields are not mutated (validator rejects identity changes before
    this is called). Configuration fields route through the existing
    ``desired_config`` JSON sections via the model's accessor properties so the
    storage layout stays consistent with the rest of the codebase.

    This is a side-effecting write on the passed ``OntUnit`` instance — the
    caller is responsible for the surrounding ``db.commit()``.
    """

    def changed(field: str) -> bool:
        # Empty keeps the historical direct-helper behavior used by legacy
        # callers. Reconciler calls always provide the validated proposal
        # fields and must persist only that explicit scope.
        return not changed_fields or field in changed_fields

    # Configuration knobs that live in desired_config JSON sections
    if changed("wan_pppoe_username"):
        ont.pppoe_username = target.wan_pppoe_username
    if changed("wan_pppoe_password_ref"):
        ont.pppoe_password = target.wan_pppoe_password_ref

    if changed("mgmt_ip") and hasattr(ont, "mgmt_ip_address"):
        ont.mgmt_ip_address = target.mgmt_ip
    if "tr069_profile_id" in changed_fields:
        ont.desired_tr069_profile_id = target.tr069_profile_id

    if changed("wifi_ssid"):
        _set_desired_value(ont, "wifi", "ssid", target.wifi_ssid)
    if changed("wifi_password_ref"):
        protected_wifi_password = encrypt_credential(target.wifi_password_ref)
        _set_desired_value(ont, "wifi", "password", protected_wifi_password or "")
    existing_wifi = dict((ont.desired_config or {}).get("wifi") or {})
    for key, field, value in (
        ("enabled", "wifi_enabled", target.wifi_enabled),
        ("channel", "wifi_channel", target.wifi_channel),
        ("security_mode", "wifi_security_mode", target.wifi_security_mode),
    ):
        if field in changed_fields if changed_fields else key in existing_wifi:
            _set_desired_value(ont, "wifi", key, value)

    existing_access = dict((ont.desired_config or {}).get("access") or {})
    access_values: tuple[tuple[str, str, Any], ...] = (
        ("wan_remote", "wan_remote_access_enabled", target.wan_remote_access_enabled),
        (
            "wan_remote_expires_at",
            "wan_remote_access_expires_at",
            (
                target.wan_remote_access_expires_at.isoformat()
                if target.wan_remote_access_expires_at
                else None
            ),
        ),
        (
            "wan_remote_source_cidrs",
            "wan_remote_access_source_cidrs",
            list(target.wan_remote_access_source_cidrs),
        ),
    )
    for key, field, value in access_values:
        if field in changed_fields if changed_fields else key in existing_access:
            _set_desired_value(ont, "access", key, value)

    if changed("dhcp_enabled"):
        _set_desired_value(ont, "lan", "dhcp_enabled", target.dhcp_enabled)
    if changed("dhcp_pool_min"):
        _set_desired_value(ont, "lan", "dhcp_start", target.dhcp_pool_min)
    if changed("dhcp_pool_max"):
        _set_desired_value(ont, "lan", "dhcp_end", target.dhcp_pool_max)
    if changed("ipv6_enabled") and (
        "ipv6_enabled" in changed_fields
        or has_executable_desired_provenance(target.ipv6_enabled_provenance)
    ):
        _set_desired_value(
            ont,
            "wan",
            "ip_protocol",
            "dual_stack" if target.ipv6_enabled else "ipv4",
        )
    for key, field, value in (
        ("static_ip", "wan_static_ip", target.wan_static_ip),
        ("static_subnet", "wan_static_subnet", target.wan_static_subnet),
        ("static_gateway", "wan_static_gateway", target.wan_static_gateway),
        ("static_dns", "wan_static_dns", target.wan_static_dns),
    ):
        if changed(field):
            _set_desired_value(ont, "wan", key, value)
    if changed("dhcp_subnet_mask"):
        _set_desired_value(ont, "lan", "subnet", target.dhcp_subnet_mask)

    if changed("wan_mode"):
        _set_desired_value(ont, "wan", "onu_mode", target.wan_mode)
    if changed("wan_pppoe_instance_index"):
        _set_desired_value(
            ont, "wan", "instance_index", target.wan_pppoe_instance_index
        )

    if changed("mgmt_subnet_mask"):
        _set_desired_value(ont, "management", "subnet", target.mgmt_subnet_mask)
    if changed("mgmt_gateway"):
        _set_desired_value(ont, "management", "gateway", target.mgmt_gateway)


# ── ACS delivery-fault bookkeeping ──────────────────────────────────────────

# Reasons that mean "this ONT's ACS half did not get written". They are stored
# under ``desired_config['delivery']`` beside the existing ``pending_apply``
# flag so the fleet UI and the alert path read one place.
ACS_DELIVERY_FAULT_REASONS = frozenset(
    {
        "acs_write_faulted",
        "acs_cr_failed",
        "acs_identity_unresolved",
        "ont_not_informing",
        "acs_unreachable",
    }
)


def record_acs_delivery_fault(ont: OntUnit, reason: str, message: str) -> int:
    """Increment and return this ONT's consecutive ACS-delivery fault streak.

    The reconcile sweep re-picks the least-recently-reconciled ONT forever, so
    a permanent fault (an unresolvable device identity, a CPE that never
    informs) otherwise retries silently and indefinitely. Persisting the streak
    turns that into something an operator and an alert can see.

    ``message`` is truncated and stored for the UI; it is produced by the
    reconciler and never contains credential values.
    """
    delivery = dict((ont.desired_config or {}).get("delivery") or {})
    previous_reason = str(delivery.get("acs_fault_reason") or "")
    streak = int(delivery.get("acs_fault_streak") or 0)
    streak = streak + 1 if previous_reason == reason else 1
    now = datetime.now(UTC).isoformat()
    _set_desired_value(ont, "delivery", "acs_fault_streak", streak)
    _set_desired_value(ont, "delivery", "acs_fault_reason", reason)
    _set_desired_value(ont, "delivery", "acs_fault_last_at", now)
    _set_desired_value(ont, "delivery", "acs_fault_detail", (message or "")[:400])
    if streak == 1:
        _set_desired_value(ont, "delivery", "acs_fault_since", now)
    return streak


def clear_acs_delivery_fault(ont: OntUnit) -> int:
    """Clear the streak after a converged reconcile; return the prior value."""
    delivery = dict((ont.desired_config or {}).get("delivery") or {})
    previous = int(delivery.get("acs_fault_streak") or 0)
    if previous:
        for key in (
            "acs_fault_streak",
            "acs_fault_reason",
            "acs_fault_last_at",
            "acs_fault_detail",
            "acs_fault_since",
        ):
            _set_desired_value(ont, "delivery", key, None)
    return previous


# ── Observed state ──────────────────────────────────────────────────────────


def observed_from_ont_observation(
    obs: OntObservation | None,
) -> OntObservedState | None:
    """Build an in-memory ``OntObservedState`` from a 1:1 observation row.

    Returns ``None`` for ONTs that have never been reconciled (no observation
    row exists yet).
    """
    if obs is None:
        return None
    return OntObservedState(
        last_reconciled_at=obs.last_reconciled_at,
        last_reconcile_duration_ms=obs.last_reconcile_duration_ms,
        mgmt_ip_pingable=obs.mgmt_ip_pingable,
        # consecutive_sweep_unreachable lives on OntUnit (the counter is read
        # by sync reconciles too, not just the sweeper); adapter callers fill
        # it from the ONT row when materialising the full observed state.
        consecutive_sweep_unreachable=0,
        olt=OltObservedFields(
            olt_present=obs.olt_present,
            # The DB row stores these as plain strings; the dataclass narrows
            # to ``Literal[...]``. Validation happens in the reader via
            # ``_normalise_state`` before the value is persisted, so the cast
            # here is sound — if a row carries an unrecognised state it'll
            # surface as a downstream planner mismatch rather than a type
            # crash.
            olt_match_state=cast(
                "Literal['match', 'mismatch', 'initial'] | None",
                obs.olt_match_state,
            ),
            olt_run_state=cast(
                "Literal['online', 'offline', 'los'] | None",
                obs.olt_run_state,
            ),
            olt_distance_m=obs.olt_distance_m,
            olt_rx_dbm=obs.olt_rx_dbm,
            olt_tx_dbm=obs.olt_tx_dbm,
            olt_temperature_c=obs.olt_temperature_c,
            olt_description=obs.olt_description,
            olt_mgmt_ip=obs.olt_mgmt_ip,
            olt_mgmt_vlan=obs.olt_mgmt_vlan,
            olt_line_profile_id=obs.olt_line_profile_id,
            olt_service_profile_id=obs.olt_service_profile_id,
            olt_tr069_profile_id=obs.olt_tr069_profile_id,
            olt_service_ports=tuple(obs.olt_service_ports or ()),
        ),
        acs=AcsObservedFields(
            acs_present=obs.acs_present,
            acs_last_inform_at=obs.acs_last_inform_at,
            acs_last_boot_at=obs.acs_last_boot_at,
            acs_last_bootstrap_at=obs.acs_last_bootstrap_at,
            acs_observed_software_version=obs.acs_observed_software_version,
            acs_observed_pppoe_username=obs.acs_observed_pppoe_username,
            acs_observed_pppoe_enable=obs.acs_observed_pppoe_enable,
            acs_observed_wan_vlan=obs.acs_observed_wan_vlan,
            acs_observed_wan_external_ip=obs.acs_observed_wan_external_ip,
            acs_observed_wan_connection_status=obs.acs_observed_wan_connection_status,
            acs_observed_nat_enabled=obs.acs_observed_nat_enabled,
            acs_observed_dhcp_enabled=obs.acs_observed_dhcp_enabled,
            acs_observed_ssid=obs.acs_observed_ssid,
            acs_observed_wifi_enabled=obs.acs_observed_wifi_enabled,
            acs_observed_wifi_channel=obs.acs_observed_wifi_channel,
            acs_observed_wifi_security_mode=obs.acs_observed_wifi_security_mode,
            acs_observed_wifi_instance_index=obs.acs_observed_wifi_instance_index,
            acs_observed_remote_ssh_enabled=(obs.acs_observed_remote_ssh_enabled),
            acs_observed_remote_ssh_port=obs.acs_observed_remote_ssh_port,
            acs_observed_remote_telnet_enabled=(obs.acs_observed_remote_telnet_enabled),
            acs_observed_remote_telnet_port=obs.acs_observed_remote_telnet_port,
            acs_observed_periodic_inform_interval_sec=(
                obs.acs_observed_periodic_inform_interval_sec
            ),
            acs_observed_cr_username=None,
            acs_observed_cr_username_set=obs.acs_observed_cr_username_set,
            acs_observed_cr_password_set=obs.acs_observed_cr_password_set,
            acs_observed_wan_wcd_index=obs.acs_observed_wan_wcd_index,
            acs_observed_wan_instance_index=obs.acs_observed_wan_instance_index,
            acs_observed_wan_ppp_locations=(),
            acs_data_model_root=obs.acs_data_model_root,
            acs_observed_ipv6_enabled=obs.acs_observed_ipv6_enabled,
            acs_observed_wan_ip_enable=obs.acs_observed_wan_ip_enable,
            acs_observed_wan_addressing_type=obs.acs_observed_wan_addressing_type,
            acs_observed_wan_ip_address=obs.acs_observed_wan_ip_address,
            acs_observed_wan_subnet_mask=obs.acs_observed_wan_subnet_mask,
            acs_observed_wan_gateway=obs.acs_observed_wan_gateway,
            acs_observed_wan_dns_servers=obs.acs_observed_wan_dns_servers,
            acs_observed_dhcpv6_enabled=obs.acs_observed_dhcpv6_enabled,
            acs_observed_dhcpv6_request_prefixes=(
                obs.acs_observed_dhcpv6_request_prefixes
            ),
            acs_observed_ra_enabled=obs.acs_observed_ra_enabled,
            # ManagementServer endpoint fields are read live on every pass;
            # the current observation schema does not persist credentials.
            acs_observed_url=None,
            acs_observed_username=None,
            acs_observed_password_set=None,
        ),
    )


def upsert_ont_observation(
    db: Session,
    ont_unit_id: uuid.UUID | str,
    observed: OntObservedState,
) -> OntObservation:
    """Insert or update the 1:1 observation row for an ONT.

    The caller is responsible for ``db.commit()``. Returns the persisted ORM
    instance so callers can inspect the assigned UUID / timestamps.
    """
    ont_uuid = _coerce_uuid(ont_unit_id)
    existing = db.scalars(
        select(OntObservation).where(OntObservation.ont_unit_id == ont_uuid)
    ).first()

    if existing is None:
        row = OntObservation(ont_unit_id=ont_uuid)
        db.add(row)
    else:
        row = existing

    row.last_reconciled_at = observed.last_reconciled_at
    row.last_reconcile_duration_ms = observed.last_reconcile_duration_ms
    row.mgmt_ip_pingable = observed.mgmt_ip_pingable
    row.olt_present = observed.olt.olt_present
    row.olt_match_state = observed.olt.olt_match_state
    row.olt_run_state = observed.olt.olt_run_state
    row.olt_distance_m = observed.olt.olt_distance_m
    row.olt_rx_dbm = observed.olt.olt_rx_dbm
    row.olt_tx_dbm = observed.olt.olt_tx_dbm
    row.olt_temperature_c = observed.olt.olt_temperature_c
    row.olt_description = observed.olt.olt_description
    row.olt_mgmt_ip = observed.olt.olt_mgmt_ip
    row.olt_mgmt_vlan = observed.olt.olt_mgmt_vlan
    row.olt_line_profile_id = observed.olt.olt_line_profile_id
    row.olt_service_profile_id = observed.olt.olt_service_profile_id
    row.olt_tr069_profile_id = observed.olt.olt_tr069_profile_id
    row.olt_service_ports = list(observed.olt.olt_service_ports)

    row.acs_present = observed.acs.acs_present
    row.acs_last_inform_at = observed.acs.acs_last_inform_at
    row.acs_last_boot_at = observed.acs.acs_last_boot_at
    row.acs_last_bootstrap_at = observed.acs.acs_last_bootstrap_at
    row.acs_observed_software_version = observed.acs.acs_observed_software_version
    row.acs_observed_pppoe_username = observed.acs.acs_observed_pppoe_username
    row.acs_observed_pppoe_enable = observed.acs.acs_observed_pppoe_enable
    row.acs_observed_wan_vlan = observed.acs.acs_observed_wan_vlan
    row.acs_observed_wan_external_ip = observed.acs.acs_observed_wan_external_ip
    row.acs_observed_wan_connection_status = (
        observed.acs.acs_observed_wan_connection_status
    )
    row.acs_observed_nat_enabled = observed.acs.acs_observed_nat_enabled
    row.acs_observed_dhcp_enabled = observed.acs.acs_observed_dhcp_enabled
    row.acs_observed_ssid = observed.acs.acs_observed_ssid
    row.acs_observed_wifi_enabled = observed.acs.acs_observed_wifi_enabled
    row.acs_observed_wifi_channel = observed.acs.acs_observed_wifi_channel
    row.acs_observed_wifi_security_mode = observed.acs.acs_observed_wifi_security_mode
    row.acs_observed_wifi_instance_index = observed.acs.acs_observed_wifi_instance_index
    row.acs_observed_remote_ssh_enabled = observed.acs.acs_observed_remote_ssh_enabled
    row.acs_observed_remote_ssh_port = observed.acs.acs_observed_remote_ssh_port
    row.acs_observed_remote_telnet_enabled = (
        observed.acs.acs_observed_remote_telnet_enabled
    )
    row.acs_observed_remote_telnet_port = observed.acs.acs_observed_remote_telnet_port
    row.acs_observed_periodic_inform_interval_sec = (
        observed.acs.acs_observed_periodic_inform_interval_sec
    )
    row.acs_observed_cr_username_set = observed.acs.acs_observed_cr_username_set
    row.acs_observed_cr_password_set = observed.acs.acs_observed_cr_password_set
    row.acs_observed_wan_wcd_index = observed.acs.acs_observed_wan_wcd_index
    row.acs_observed_wan_instance_index = observed.acs.acs_observed_wan_instance_index
    row.acs_data_model_root = observed.acs.acs_data_model_root
    row.acs_observed_ipv6_enabled = observed.acs.acs_observed_ipv6_enabled
    row.acs_observed_wan_ip_enable = observed.acs.acs_observed_wan_ip_enable
    row.acs_observed_wan_addressing_type = observed.acs.acs_observed_wan_addressing_type
    row.acs_observed_wan_ip_address = observed.acs.acs_observed_wan_ip_address
    row.acs_observed_wan_subnet_mask = observed.acs.acs_observed_wan_subnet_mask
    row.acs_observed_wan_gateway = observed.acs.acs_observed_wan_gateway
    row.acs_observed_wan_dns_servers = observed.acs.acs_observed_wan_dns_servers
    row.acs_observed_dhcpv6_enabled = observed.acs.acs_observed_dhcpv6_enabled
    row.acs_observed_dhcpv6_request_prefixes = (
        observed.acs.acs_observed_dhcpv6_request_prefixes
    )
    row.acs_observed_ra_enabled = observed.acs.acs_observed_ra_enabled

    db.flush()  # Establish row.id before the caller commits.
    return row


# ── Internal helpers ────────────────────────────────────────────────────────


def _fsp_from_ont(ont: OntUnit) -> str:
    """Build the canonical f/s/p string from ``OntUnit.board`` + ``OntUnit.port``.

    Matches ``_scanned_fsp_from_ont`` in ``ont_olt_context.py`` — duplicated
    here to keep ``adapters.py`` self-contained (no cross-module dependency on
    a write-side context resolver). Returns the empty string when board/port
    don't form a valid f/s/p.
    """
    board = str(getattr(ont, "board", "") or "").strip()
    port = str(getattr(ont, "port", "") or "").strip()
    if not board or not port:
        return ""
    fsp = f"{board}/{port}"
    return fsp if _FSP_RE.fullmatch(fsp) else ""


def _default_description(ont: OntUnit) -> str:
    """Stub description used when no explicit one is bound.

    Mirrors ``_build_initial_ont_description`` in ``ont_authorization.py`` —
    serial + auth date. Subscribed-aware descriptions are introduced when the
    in-app Subscriber model lands.
    """
    return f"{ont.serial_number}_authd_{datetime.now(UTC).strftime('%Y%m%d')}"


def _normalise_wan_mode(
    ip_mode: Any, onu_mode: Any
) -> Literal["pppoe", "dhcp", "static", "bridge"]:
    """Reduce the effective config's two related fields to the reconciler's
    single ``pppoe`` / ``bridge`` contract.

    ``ip_mode`` is the WAN IP mode (``pppoe``/``bridge``/``static_ip``/...).
    ``onu_mode`` is the ONU operating mode (``routing``/``bridging``).
    A bridge signal in either field wins.
    """
    bridge_signals = {"bridge", "bridged", "bridging", "setup_via_onu"}
    if str(ip_mode or "").strip().lower() in bridge_signals:
        return "bridge"
    if str(onu_mode or "").strip().lower() in bridge_signals:
        return "bridge"
    normalized = str(ip_mode or "").strip().lower()
    if normalized == "dhcp":
        return "dhcp"
    if normalized in {"static", "static_ip"}:
        return "static"
    return "pppoe"


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_default(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return _bool_or_default(value, default=False)


def _wifi_channel_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if str(value).strip().lower() == "auto":
        return 0
    return _int_or_none(value)


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _datetime_or_none(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _subnet_mask_from_lan_subnet(raw: Any) -> str:
    """Effective config emits ``lan_subnet`` as CIDR or plain mask. The
    reconciler stores a dotted-decimal subnet mask; normalize here so the
    validator's mask check sees a consistent shape. Defaults to /24 on
    unparseable input."""
    if raw is None:
        return "255.255.255.0"
    text = str(raw).strip()
    if "/" in text:
        try:
            import ipaddress

            return str(ipaddress.ip_network(text, strict=False).netmask)
        except ValueError:
            return "255.255.255.0"
    return text or "255.255.255.0"


def _set_desired_value(ont: OntUnit, section: str, key: str, value: Any) -> None:
    """Write into ``OntUnit.desired_config[section][key]`` via the model's
    accessor pattern. None/empty removes the key. Mirrors the private
    ``OntUnit._set_desired_value`` helper but called from outside the model so
    we don't depend on a private API."""
    config = dict(ont.desired_config or {})
    section_values = dict(config.get(section) or {})
    if value in (None, ""):
        section_values.pop(key, None)
    else:
        section_values[key] = value
    if section_values:
        config[section] = section_values
    else:
        config.pop(section, None)
    ont.desired_config = config


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
