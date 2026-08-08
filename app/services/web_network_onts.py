"""Web service helpers for ONT form dropdowns and context."""

from __future__ import annotations

import logging
import re
from ipaddress import IPv4Network, ip_address, ip_network
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import Subscription
from app.models.network import (
    IpPool,
    IPVersion,
    OLTDevice,
    OntUnit,
    PonType,
    Splitter,
    Vlan,
    VlanPurpose,
)
from app.models.tr069 import Tr069AcsServer
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_status import resolve_effective_ont_status
from app.services.network.onu_types import onu_types
from app.services.network.speed_profiles import speed_profiles
from app.services.network.zones import network_zones

logger = logging.getLogger(__name__)


_OLT_MANAGEMENT_NETWORKS_BY_MGMT_IP: dict[str, str] = {
    "172.16.201.2/24": "172.16.201.0/24",
    "172.20.100.9/30": "172.20.100.8/30",
    "172.16.205.1/24": "172.16.205.0/24",
    "172.16.203.1/24": "172.16.203.0/24",
    "172.16.204.1/24": "172.16.204.0/24",
    "172.16.207.1/24": "172.16.207.0/24",
    "172.16.210.1/24": "172.16.210.0/24",
}

_OLT_MANAGEMENT_NETWORKS_BY_NAME: dict[str, list[str]] = {
    "garki": ["172.16.201.0/24"],
    "garki huawei olt": ["172.16.201.0/24"],
    "garki olt 1": ["172.16.201.0/24"],
    "karasana": ["172.16.203.0/24"],
    "karasana olt 1": ["172.16.203.0/24"],
    "boi": ["172.20.100.8/30"],
    "boi huawei olt": ["172.20.100.8/30"],
    "boi asokoro olt 1": ["172.20.100.8/30"],
    "boi olt 1": ["172.20.100.8/30"],
    "gudu": ["172.16.205.0/24"],
    "gudu huawei olt": ["172.16.205.0/24"],
    "gudu olt": ["172.16.205.0/24"],
    "karsana": ["172.16.203.0/24"],
    "karsana huawei olt": ["172.16.203.0/24"],
    "karsana huawei olt 1": ["172.16.203.0/24"],
    "karsana olt": ["172.16.203.0/24"],
    "jabi": ["172.16.204.0/24"],
    "jabi huawei olt": ["172.16.204.0/24"],
    "jabi olt-1": ["172.16.204.0/24"],
    "jabi olt 1": ["172.16.204.0/24"],
    "jabi olt": ["172.16.204.0/24"],
    "gwarimpa": ["172.16.207.0/24"],
    "gwarimpa huawei olt": ["172.16.207.0/24"],
    "gwarimpa huawei olt 2": ["172.16.207.0/24"],
    "gwarimpa olt 2": ["172.16.207.0/24"],
    "spdc": ["172.16.210.0/24"],
    "spdc huawei olt": ["172.16.210.0/24"],
    "spdc olt": ["172.16.210.0/24"],
    "spdc olt 1": ["172.16.210.0/24"],
}


def get_onu_types(db: Session) -> list[Any]:
    """Fetch active ONU types for form dropdowns."""
    return onu_types.list(db, is_active=True)


def get_olt_devices(db: Session) -> list[OLTDevice]:
    """Fetch active OLT devices for form dropdowns."""
    stmt = (
        select(OLTDevice).where(OLTDevice.is_active.is_(True)).order_by(OLTDevice.name)
    )
    return list(db.scalars(stmt).all())


def get_vlans(db: Session) -> list[Vlan]:
    """Fetch VLANs for form dropdowns."""
    stmt = select(Vlan).order_by(Vlan.tag)
    return list(db.scalars(stmt).all())


def resolve_ont_connected_olt(
    db: Session, ont: OntUnit | Any | None
) -> OLTDevice | None:
    """Resolve the OLT an ONT is connected to via direct FK or active assignment."""
    if ont is None:
        return None

    from app.models.network import PonPort

    explicit_olt = getattr(ont, "olt_device", None)
    if explicit_olt is not None:
        return explicit_olt

    olt_device_id = getattr(ont, "olt_device_id", None)
    if olt_device_id:
        explicit_db_olt = db.get(OLTDevice, olt_device_id)
        if explicit_db_olt is not None:
            return explicit_db_olt

    # Resolve from an explicit active assignment only. Avoid using inactive,
    # potentially stale assignments for UI scoping.
    for assignment in getattr(ont, "assignments", []):
        pon_port_id = getattr(assignment, "pon_port_id", None)
        if not pon_port_id:
            continue
        pon_port = getattr(assignment, "pon_port", None) or db.get(PonPort, pon_port_id)
        olt_id = getattr(pon_port, "olt_id", None) if pon_port else None
        if not olt_id:
            continue
        resolved_olt = getattr(pon_port, "olt", None) or db.get(OLTDevice, olt_id)
        if not resolved_olt:
            continue
        if getattr(assignment, "active", False):
            return resolved_olt

    return None


def _olt_scope_tokens(olt: OLTDevice | Any | None) -> set[str]:
    if olt is None:
        return set()
    generic = {
        "device",
        "gpon",
        "huawei",
        "ma5600",
        "ma5800",
        "olt",
        "zte",
    }
    text = " ".join(
        str(value or "")
        for value in [
            getattr(olt, "name", None),
            getattr(olt, "hostname", None),
            getattr(olt, "site_name", None),
            getattr(olt, "location", None),
        ]
    ).lower()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if len(token) >= 3 and token not in generic
    }


def _normalize_olt_identity_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _normalize_olt_identity_tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _extract_mgmt_address_from_identity(value: Any) -> str:
    text = str(value or "")
    match = re.search(
        r"\b((?:\d{1,3}\.){3}\d{1,3})(?:/\d{1,2})?\b", text, flags=re.ASCII
    )
    if not match:
        return ""
    return match.group(1)


def _expected_management_networks_for_olt(
    olt: OLTDevice | Any | None,
) -> set[IPv4Network]:
    if olt is None:
        return set()

    candidates: set[IPv4Network] = set()
    mgmt_value = str(getattr(olt, "mgmt_ip", "") or "").strip()
    if not mgmt_value:
        mgmt_value = _extract_mgmt_address_from_identity(
            " ".join(
                str(value or "")
                for value in [
                    getattr(olt, "name", None),
                    getattr(olt, "hostname", None),
                    getattr(olt, "site_name", None),
                    getattr(olt, "location", None),
                ]
            )
        )

    if mgmt_value:
        mgmt_host = mgmt_value.split("/")[0]
        try:
            mgmt_address = ip_address(mgmt_host)
        except ValueError:
            mgmt_address = None
        try:
            declared_network = ip_network(mgmt_value, strict=False)
        except ValueError:
            declared_network = None

        for mgmt_key, cidr in _OLT_MANAGEMENT_NETWORKS_BY_MGMT_IP.items():
            try:
                known_network = ip_network(mgmt_key, strict=False)
            except ValueError:
                continue
            if mgmt_address is not None and mgmt_address in known_network:
                try:
                    network = ip_network(cidr, strict=False)
                    if isinstance(network, IPv4Network):
                        candidates.add(network)
                except ValueError:
                    continue
            if declared_network is not None and declared_network.overlaps(
                known_network
            ):
                try:
                    network = ip_network(cidr, strict=False)
                    if isinstance(network, IPv4Network):
                        candidates.add(network)
                except ValueError:
                    continue

    if not candidates:
        identity_tokens = _normalize_olt_identity_tokens(
            " ".join(
                str(value or "")
                for value in [
                    getattr(olt, "name", None),
                    getattr(olt, "hostname", None),
                    getattr(olt, "site_name", None),
                    getattr(olt, "location", None),
                ]
            )
        )
        identity = _normalize_olt_identity_text(
            " ".join(
                str(value or "")
                for value in [
                    getattr(olt, "name", None),
                    getattr(olt, "hostname", None),
                    getattr(olt, "site_name", None),
                    getattr(olt, "location", None),
                ]
            )
        )
        for key, cidrs in _OLT_MANAGEMENT_NETWORKS_BY_NAME.items():
            key_tokens = set(key.split())
            if key in identity or key_tokens.issubset(identity_tokens):
                for cidr in cidrs:
                    try:
                        network = ip_network(cidr, strict=False)
                        if isinstance(network, IPv4Network):
                            candidates.add(network)
                    except ValueError:
                        continue

    return candidates


def _pool_in_management_networks(pool: IpPool, networks: set[IPv4Network]) -> bool:
    if not networks:
        return False
    try:
        pool_network = ip_network(str(pool.cidr), strict=False)
    except ValueError:
        return False
    try:
        return any(pool_network.overlaps(network) for network in networks)
    except ValueError:
        return False


def _ip_pool_matches_olt(
    db: Session, pool: IpPool | Any | None, olt_device_id: Any | None
) -> bool:
    if pool is None or not olt_device_id:
        return False
    if getattr(pool, "olt_device_id", None) == olt_device_id:
        return True
    vlan_id = getattr(pool, "vlan_id", None)
    if not vlan_id:
        return False
    vlan = getattr(pool, "vlan", None) or db.get(Vlan, vlan_id)
    return getattr(vlan, "olt_device_id", None) == olt_device_id


def get_vlans_for_olt(
    db: Session,
    olt_device_id: str | None,
    *,
    include_vlan_ids: list[str] | None = None,
    include_global: bool = False,
) -> list[Vlan]:
    """Fetch VLANs scoped for an OLT."""
    include_ids = [v for v in (include_vlan_ids or []) if v]
    if not olt_device_id and not include_ids:
        return []

    stmt = select(Vlan)
    if olt_device_id:
        stmt = stmt.where(Vlan.olt_device_id == olt_device_id)
    elif include_ids:
        stmt = stmt.where(Vlan.id.in_(include_ids))
    else:
        stmt = stmt.where(Vlan.is_active.is_(True))

    stmt = stmt.order_by(Vlan.tag)
    return list(db.scalars(stmt).all())


def management_ip_choices_for_ont(
    db: Session,
    ont: OntUnit | Any | None,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Build management static IP choices from the ONT's effective profile pool."""
    if ont is None:
        return {
            "mgmt_ip_pool": None,
            "available_mgmt_ips": [],
            "mgmt_ip_choice_message": "No ONT selected.",
        }

    olt = resolve_ont_connected_olt(db, ont)
    olt_device_id = getattr(olt, "id", None)
    managed_networks = _expected_management_networks_for_olt(olt)
    pool = None
    if pool is not None:
        if not _ip_pool_matches_olt(db, pool, olt_device_id):
            pool = None
        elif managed_networks and not _pool_in_management_networks(
            pool, managed_networks
        ):
            pool = None

    from app.services.web_network_onts_provisioning import available_static_ipv4_choices

    pools: list[IpPool] = []
    if pool is not None:
        pools = [pool]
    elif olt_device_id:
        scoped_pools = list(
            db.scalars(
                select(IpPool)
                .outerjoin(Vlan, IpPool.vlan_id == Vlan.id)
                .where(IpPool.is_active.is_(True))
                .where(IpPool.ip_version == IPVersion.ipv4)
                .where(
                    or_(
                        IpPool.olt_device_id == olt_device_id,
                        Vlan.olt_device_id == olt_device_id,
                    )
                )
                .order_by(IpPool.name.asc())
            ).all()
        )
        if managed_networks:
            network_scoped_pools = [
                candidate
                for candidate in scoped_pools
                if _pool_in_management_networks(candidate, managed_networks)
            ]
            if network_scoped_pools:
                scoped_pools = network_scoped_pools
        if not scoped_pools:
            tokens = _olt_scope_tokens(olt)
            unscoped_pools = list(
                db.scalars(
                    select(IpPool)
                    .where(IpPool.is_active.is_(True))
                    .where(IpPool.ip_version == IPVersion.ipv4)
                    .where(IpPool.olt_device_id.is_(None))
                    .where(IpPool.vlan_id.is_(None))
                    .order_by(IpPool.name.asc())
                ).all()
            )
            scoped_pools = [
                candidate
                for candidate in unscoped_pools
                if tokens
                and any(
                    token
                    in " ".join(
                        str(value or "").lower()
                        for value in [candidate.name, candidate.notes]
                    )
                    for token in tokens
                )
            ]
            scoped_cidrs = {str(candidate.cidr) for candidate in scoped_pools}
            if scoped_cidrs:
                pool_ids = {candidate.id for candidate in scoped_pools}
                scoped_pools.extend(
                    candidate
                    for candidate in unscoped_pools
                    if candidate.id not in pool_ids
                    and str(candidate.cidr) in scoped_cidrs
                )
        if not scoped_pools and managed_networks:
            all_pools = list(
                db.scalars(
                    select(IpPool)
                    .where(IpPool.is_active.is_(True))
                    .where(IpPool.ip_version == IPVersion.ipv4)
                    .order_by(IpPool.name.asc())
                ).all()
            )
            scoped_pools = [
                candidate
                for candidate in all_pools
                if _pool_in_management_networks(candidate, managed_networks)
            ]
    else:
        scoped_pools = []

    if not pools:

        def looks_like_management_pool(candidate: IpPool) -> bool:
            vlan = getattr(candidate, "vlan", None)
            vlan_purpose = getattr(getattr(vlan, "purpose", None), "value", None)
            haystack = " ".join(
                str(value or "").lower()
                for value in [
                    candidate.name,
                    candidate.notes,
                    getattr(vlan, "name", None),
                    getattr(vlan, "description", None),
                ]
            )
            return (
                vlan_purpose == VlanPurpose.management.value
                or "management" in haystack
                or "mgmt" in haystack
            )

        pools = [
            candidate
            for candidate in scoped_pools
            if looks_like_management_pool(candidate)
        ]
        management_cidrs = {str(candidate.cidr) for candidate in pools}
        if management_cidrs:
            pool_ids = {candidate.id for candidate in pools}
            pools.extend(
                candidate
                for candidate in scoped_pools
                if candidate.id not in pool_ids
                and str(candidate.cidr) in management_cidrs
            )
        if not pools:
            pools = scoped_pools

    if not pools:
        return {
            "mgmt_ip_pool": None,
            "available_mgmt_ips": [],
            "mgmt_ip_choice_message": "No active IPv4 pools are available.",
        }

    choices: list[dict[str, Any]] = []
    per_pool_limit = max(1, min(limit, max(10, limit // max(len(pools), 1))))
    effective = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    selected_ip = effective_values.get("mgmt_ip_address") or getattr(
        ont, "mgmt_ip_address", None
    )
    for candidate_pool in pools:
        pool_version = getattr(
            getattr(candidate_pool, "ip_version", None),
            "value",
            candidate_pool.ip_version,
        )
        if pool_version != IPVersion.ipv4.value:
            continue
        state = available_static_ipv4_choices(
            db,
            pool_id=str(candidate_pool.id),
            ont_id=str(getattr(ont, "id", "") or ""),
            selected_ip=selected_ip,
            limit=per_pool_limit,
        )
        choice_values = state.get("choices", [])
        if not isinstance(choice_values, list):
            continue
        for choice in choice_values:
            if not isinstance(choice, dict):
                continue
            enriched = dict(choice)
            enriched["label"] = f"{choice.get('address')} - {candidate_pool.name}"
            choices.append(enriched)
            if len(choices) >= limit:
                break
        if len(choices) >= limit:
            break

    return {
        "mgmt_ip_pool": pools[0] if len(pools) == 1 else None,
        "available_mgmt_ips": choices,
        "mgmt_ip_choice_message": None
        if choices
        else "No available IPv4 addresses in management IP pools.",
    }


def get_vlans_for_ont(db: Session, ont: OntUnit | Any | None) -> list[Vlan]:
    """Fetch VLANs scoped to an ONT's assigned OLT."""
    if ont is None:
        return []

    olt = resolve_ont_connected_olt(db, ont)
    olt_device_id = getattr(olt, "id", None)
    return get_vlans_for_olt(
        db,
        str(olt_device_id) if olt_device_id else None,
        include_global=False,
    )


def get_tr069_profiles_for_ont(
    db: Session,
    ont: OntUnit | Any | None,
) -> tuple[list[Any], str | None]:
    """Fetch TR-069 server profiles for the ONT's assigned OLT."""
    result = get_tr069_profiles_for_ont_with_meta(db, ont)
    profiles = list(result.data or []) if result.ok else []
    error = None if result.ok and not result.stale else result.message
    return profiles, error


def get_tr069_profiles_for_ont_with_meta(
    db: Session,
    ont: OntUnit | Any | None,
):
    """Fetch TR-069 server profiles with source/freshness metadata."""
    from app.services.olt_observed_state_adapter import ObservedReadResult

    if ont is None:
        return ObservedReadResult(
            ok=True, message="", data=[], source="none", fetched_at=None
        )

    olt = resolve_ont_connected_olt(db, ont)
    if not olt:
        return ObservedReadResult(
            ok=False,
            message="No OLT is assigned to this ONT",
            data=[],
            source="none",
            fetched_at=None,
        )

    from app.services.olt_observed_state_adapter import get_tr069_profiles_for_olt

    return get_tr069_profiles_for_olt(db, olt)


def get_zones(db: Session) -> list[Any]:
    """Fetch active network zones for form dropdowns."""
    return network_zones.list(db, is_active=True)


def get_splitters(db: Session) -> list[Splitter]:
    """Fetch splitters for form dropdowns."""
    stmt = select(Splitter).where(Splitter.is_active.is_(True)).order_by(Splitter.name)
    return list(db.scalars(stmt).all())


def get_speed_profiles(db: Session, direction: str) -> list[Any]:
    """Fetch speed profiles for a given direction (download/upload)."""
    return speed_profiles.list(db, direction=direction, is_active=True)


def get_tr069_servers(db: Session) -> list[Tr069AcsServer]:
    """Fetch active TR069 ACS servers for form dropdowns."""
    stmt = (
        select(Tr069AcsServer)
        .where(Tr069AcsServer.is_active.is_(True))
        .order_by(Tr069AcsServer.name)
    )
    return list(db.scalars(stmt).all())


def ont_form_dependencies(
    db: Session, ont: OntUnit | Any | None = None
) -> dict[str, Any]:
    """Build all dropdown data needed by the ONT configuration form."""
    return {
        "onu_types": get_onu_types(db),
        "olt_devices": get_olt_devices(db),
        "vlans": get_vlans_for_ont(db, ont),
        "zones": get_zones(db),
        "splitters": get_splitters(db),
        "speed_profiles_download": get_speed_profiles(db, "download"),
        "speed_profiles_upload": get_speed_profiles(db, "upload"),
        "pon_types": [e.value for e in PonType],
    }


# ---------------------------------------------------------------------------
# Firmware Images
# ---------------------------------------------------------------------------


def get_active_firmware_images(
    db: Session,
    *,
    vendor_contains: str | None = None,
    limit: int | None = None,
) -> list:
    """Return active ONT firmware images for ONT UI workflows."""
    from sqlalchemy import select

    from app.models.network import OntFirmwareImage

    stmt = (
        select(OntFirmwareImage)
        .where(OntFirmwareImage.is_active.is_(True))
        .order_by(OntFirmwareImage.vendor, OntFirmwareImage.version.desc())
    )
    if vendor_contains:
        stmt = stmt.where(OntFirmwareImage.vendor.ilike(f"%{vendor_contains}%"))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Bulk ONT Operations
# ---------------------------------------------------------------------------

_BULK_ACTIONS = {
    "reboot",
    "refresh",
    "factory_reset",
    "firmware_upgrade",
    "return_to_inventory",
    "decommission",
}


def execute_bulk_action(
    db: Session,
    ont_ids: list[str],
    action: str,
    *,
    firmware_image_id: str | None = None,
) -> dict[str, Any]:
    """Execute a bulk action on multiple ONTs.

    Args:
        db: Database session.
        ont_ids: List of OntUnit IDs.
        action: One of 'reboot', 'refresh', 'factory_reset',
            'firmware_upgrade', 'return_to_inventory'.
        firmware_image_id: Required when action is 'firmware_upgrade'.

    Returns:
        Stats dict with succeeded/failed/skipped counts and per-ONT results.
    """
    from app.services.network.ont_actions import OntActions
    from app.services.network.ont_decommission import decommission_ont
    from app.services.network.ont_inventory import return_ont_to_inventory

    if action not in _BULK_ACTIONS:
        return {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "error": f"Invalid action: {action}",
            "results": [],
        }

    if not ont_ids:
        return {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "error": "No ONTs selected",
            "results": [],
        }

    # Cap at 50 to prevent accidental mass operations
    capped_ids = ont_ids[:50]
    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0

    for ont_id in capped_ids:
        try:
            if action == "reboot":
                result = OntActions.reboot(db, ont_id)
            elif action == "refresh":
                result = OntActions.refresh_status(db, ont_id)
            elif action == "factory_reset":
                result = OntActions.factory_reset(db, ont_id)
            elif action == "firmware_upgrade" and firmware_image_id:
                result = OntActions.firmware_upgrade(db, ont_id, firmware_image_id)
            elif action == "return_to_inventory":
                result = return_ont_to_inventory(db, ont_id)
            elif action == "decommission":
                result = decommission_ont(
                    db,
                    ont_id,
                    reason="hardware_fault",
                    confirm=True,
                    deauthorize_on_olt=True,
                    remove_from_acs=True,
                )
            else:
                continue

            if result.success:
                succeeded += 1
            else:
                failed += 1
            results.append(
                {
                    "ont_id": ont_id,
                    "success": result.success,
                    "message": result.message,
                }
            )
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "ont_id": ont_id,
                    "success": False,
                    "message": str(exc),
                }
            )
            logger.error("Bulk %s failed for ONT %s: %s", action, ont_id, exc)

    skipped = len(ont_ids) - len(capped_ids)
    logger.info(
        "Bulk %s: %d succeeded, %d failed, %d skipped (of %d requested)",
        action,
        succeeded,
        failed,
        skipped,
        len(ont_ids),
    )
    return {
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "total": len(capped_ids),
        "results": results,
    }


def bulk_action_summary_context(
    db: Session,
    ont_ids: list[str],
    action: str,
    *,
    firmware_image_id: str | None = None,
) -> dict[str, Any]:
    """Execute a bulk ONT action and return display-ready summary data."""
    stats = execute_bulk_action(
        db,
        ont_ids,
        action,
        firmware_image_id=firmware_image_id,
    )
    error = stats.get("error")
    skipped = int(stats.get("skipped", 0) or 0)
    result_rows = list(stats.get("results", []) or [])
    ont_uuid_map = {
        str(ont.id): ont.serial_number or str(ont.id)
        for ont in db.scalars(select(OntUnit).where(OntUnit.id.in_(ont_ids))).all()
    }
    failed_results: list[dict[str, Any]] = []
    for row in result_rows:
        row["serial_number"] = ont_uuid_map.get(
            str(row.get("ont_id") or ""),
            str(row.get("ont_id") or "Unknown ONT"),
        )
        if not row.get("success"):
            failed_results.append(row)
    return {
        "stats": stats,
        "action": action,
        "error": error,
        "skipped_text": f", {skipped} skipped (max 50)" if skipped else "",
        "failed_results": failed_results,
        "firmware_image_id": firmware_image_id,
    }


def provision_wizard_context(
    request: Any,
    db: Session,
    ont_id: str,
    *,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Build template context for the ONT provisioning wizard page."""
    from app.services import network as network_service
    from app.services import web_admin as web_admin_service
    from app.services.web_network_operations import get_provision_operation_progress
    from app.services.web_network_onts_provisioning import (
        preflight_result,
        validate_provision_form_fields,
    )

    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except Exception:
        return {"error": "ONT not found", "request": request}

    olt = getattr(ont, "olt_device", None)

    # Resolve config pack for this ONT's OLT
    config_pack = None
    config_pack_validation = None
    if olt:
        from app.services.network.olt_config_pack import (
            resolve_olt_config_pack,
            validate_config_pack_comprehensive,
        )

        config_pack = resolve_olt_config_pack(db, str(olt.id))
        config_pack_validation = validate_config_pack_comprehensive(db, str(olt.id))

    tr069_profile, tr069_error = resolve_effective_tr069_profile_for_ont(db, ont)
    tr069_profiles, tr069_profiles_error = get_tr069_profiles_for_ont(db, ont)
    vlans = get_vlans_for_ont(db, ont)
    ip_pools = list(
        db.scalars(
            select(IpPool)
            .where(IpPool.is_active.is_(True))
            .where(
                or_(
                    IpPool.olt_device_id == getattr(ont, "olt_device_id", None),
                    IpPool.olt_device_id.is_(None),
                )
            )
            .order_by(IpPool.name.asc())
        ).all()
    )
    vlan_nas_map = {
        str(vlan.id): str(vlan.olt_device_id)
        for vlan in vlans
        if getattr(vlan, "olt_device_id", None)
    }
    pool_nas_map = {
        str(pool.id): str(pool.olt_device_id)
        for pool in ip_pools
        if getattr(pool, "olt_device_id", None)
    }
    pool_vlan_map = {
        str(pool.id): str(pool.vlan_id)
        for pool in ip_pools
        if getattr(pool, "vlan_id", None)
    }
    effective_config = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective_config.get("values", {}) if isinstance(effective_config, dict) else {}
    )
    lan_intent = {
        "lan_ip": effective_values.get("lan_ip"),
        "lan_subnet": effective_values.get("lan_subnet"),
        "dhcp_enabled": effective_values.get("lan_dhcp_enabled"),
        "dhcp_start": effective_values.get("lan_dhcp_start"),
        "dhcp_end": effective_values.get("lan_dhcp_end"),
    }
    wifi_intent = {
        "enabled": effective_values.get("wifi_enabled"),
        "ssid": effective_values.get("wifi_ssid"),
        "channel": effective_values.get("wifi_channel"),
        "security_mode": effective_values.get("wifi_security_mode"),
    }
    mgmt_mode = str(effective_values.get("mgmt_ip_mode") or "").strip() or None
    wan_protocol = str(effective_values.get("wan_mode") or "").strip() or None
    if wan_protocol == "static_ip":
        wan_protocol = "static"
    elif wan_protocol == "bridge":
        wan_protocol = "bridged"

    provision_gate_issues = validate_provision_form_fields(
        onu_mode=str(effective_values.get("onu_mode") or "") or None,
        mgmt_ip_mode=mgmt_mode,
        mgmt_ip_address=str(effective_values.get("mgmt_ip_address") or "") or None,
        mgmt_subnet=None,
        mgmt_gateway=None,
        wan_protocol=wan_protocol,
        pppoe_username=str(effective_values.get("pppoe_username") or "") or None,
        static_ip_pool_id=None,
        static_ip=None,
        static_subnet=None,
        static_gateway=None,
        static_dns=None,
        lan_ip=str(lan_intent.get("lan_ip") or "") or None,
        lan_subnet=str(lan_intent.get("lan_subnet") or "") or None,
        dhcp_enabled=(
            bool(lan_intent.get("dhcp_enabled"))
            if lan_intent.get("dhcp_enabled") is not None
            else None
        ),
        dhcp_start=str(lan_intent.get("dhcp_start") or "") or None,
        dhcp_end=str(lan_intent.get("dhcp_end") or "") or None,
        wifi_enabled=(
            bool(wifi_intent.get("enabled"))
            if wifi_intent.get("enabled") is not None
            else None
        ),
        wifi_ssid=str(wifi_intent.get("ssid") or "") or None,
        wifi_password=None,
    )
    provision_preflight = preflight_result(
        db,
        ont_id=ont_id,
    )

    # Get the active assignment for this ONT (service config source)
    active_assignment = None
    assignment_subscriber = None
    assignment_subscription = None
    for assignment in getattr(ont, "assignments", []):
        if getattr(assignment, "active", False):
            active_assignment = assignment
            assignment_subscriber = getattr(assignment, "subscriber", None)
            break

    if active_assignment and assignment_subscriber is None:
        pppoe_username = str(effective_values.get("pppoe_username") or "").strip()
        if pppoe_username:
            assignment_subscription = db.scalars(
                select(Subscription)
                .options(
                    joinedload(Subscription.subscriber),
                    joinedload(Subscription.offer),
                )
                .where(Subscription.login == pppoe_username)
                .order_by(Subscription.updated_at.desc())
                .limit(1)
            ).first()
            assignment_subscriber = (
                getattr(assignment_subscription, "subscriber", None)
                if assignment_subscription
                else None
            )

    context: dict[str, Any] = {
        "request": request,
        "active_page": "onts",
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
        "ont": ont,
        "olt": olt,
        "assignment": active_assignment,
        "subscriber": assignment_subscriber,
        "tr069_profile": tr069_profile,
        "tr069_profile_error": tr069_error,
        "selected_tr069_profile_id": getattr(tr069_profile, "profile_id", None),
        "selected_tr069_profile_name": getattr(tr069_profile, "name", None)
        or getattr(tr069_profile, "profile_name", None),
        "resolved_tr069_profile_error": tr069_error,
        "tr069_profiles": tr069_profiles,
        "tr069_profiles_error": tr069_profiles_error,
        "profiles": [],
        "vlans": vlans,
        "ip_pools": ip_pools,
        "vlan_nas_map": vlan_nas_map,
        "pool_nas_map": pool_nas_map,
        "pool_vlan_map": pool_vlan_map,
        "tr069_servers": get_tr069_servers(db),
        "speed_profiles_download": get_speed_profiles(db, "download"),
        "speed_profiles_upload": get_speed_profiles(db, "upload"),
        "signal_info": {
            "olt_status": resolve_effective_ont_status(ont).status.value,
            "olt_rx_dbm": getattr(ont, "olt_rx_signal_dbm", None),
        },
        "pon_label": (
            f"{ont.board}/{ont.port}"
            if getattr(ont, "board", None) and getattr(ont, "port", None)
            else None
        ),
        "subscription": assignment_subscription,
        "acs_bound": bool(effective_values.get("tr069_acs_server_id")),
        "operational_acs_server_name": getattr(
            getattr(olt, "tr069_acs_server", None), "name", None
        ),
        "pppoe_username": effective_values.get("pppoe_username"),
        "ont_plan": {},
        "provision_gate_issues": provision_gate_issues,
        "provision_preflight": provision_preflight,
        "provision_operation": get_provision_operation_progress(
            db,
            ont_id=ont_id,
            operation_id=operation_id,
        ),
        "config_pack": config_pack.to_dict() if config_pack else None,
        "config_pack_validation": config_pack_validation.to_dict()
        if config_pack_validation
        else None,
    }
    return context


def resolve_effective_tr069_profile_for_ont(
    db: Session, ont: Any
) -> tuple[Any | None, str | None]:
    """Resolve the TR-069 OLT profile from desired config plus OLT defaults."""
    profiles, error = get_tr069_profiles_for_ont(db, ont)
    if not profiles:
        return None, error or "No TR-069 profile found for this ONT"

    effective = resolve_effective_ont_config(db, ont)
    effective_values = (
        effective.get("values", {}) if isinstance(effective, dict) else {}
    )
    planned_profile_id: Any = effective_values.get("tr069_olt_profile_id")

    if planned_profile_id is not None:
        planned_profile_id_str = str(planned_profile_id).strip()
        planned_profile_id_int = None
        if planned_profile_id_str.isdigit():
            try:
                planned_profile_id_int = int(planned_profile_id_str)
            except Exception:
                planned_profile_id_int = None
        for profile in profiles:
            candidate_profile_id = getattr(profile, "profile_id", None)
            if candidate_profile_id is None:
                continue
            candidate_profile_id_str = str(candidate_profile_id).strip()
            if candidate_profile_id_str == planned_profile_id_str:
                return profile, None
            if planned_profile_id_int is not None:
                try:
                    if int(candidate_profile_id) == planned_profile_id_int:
                        return profile, None
                except Exception:
                    pass

    return profiles[0], None
