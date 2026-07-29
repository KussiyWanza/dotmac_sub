"""Inventory and list/filter helpers for network devices pages."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.models.network_monitoring import DeviceInterface, NetworkDevice, PopSite
from app.schemas.status_presentation import StatusTone
from app.services import device_projection_views
from app.services import network as network_service
from app.services.device_operational_status import (
    NOT_WORKING,
    WORKING,
    annotate_operational_status,
    derive_nas_operational_status,
    derive_olt_operational_status,
    derive_ont_operational_status,
    derive_router_operational_status,
    warmer_is_stale,
)
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
)
from app.services.nas import NasDevices
from app.services.network import cpe as cpe_service
from app.services.network.imported_service_ports import imported_service_port_summary
from app.services.router_management.inventory import RouterInventory
from app.services.status_presentation import device_operational_status_presentation
from app.services.ui_contracts import Action, Kpi, StateValue

# UI page contract for the admin network-device list. The projection-boundary
# owner: it declares the searchable/filterable/sortable fields, default order and
# page sizes. The list reads the materialised device_projections table (via
# device_projection_views) — the SQL-paginated read model — instead of loading
# every device and filtering in memory. Projection age drives repair; it never
# changes the owner-resolved binary operational outcome shown by readers.
NETWORK_DEVICE_LIST_DEFINITION = ListDefinition(
    key="network_devices",
    fields=(
        ListFieldDefinition("search", "Search", searchable=True),
        ListFieldDefinition("type", "Type", filterable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("vendor", "Vendor", filterable=True),
        ListFieldDefinition("name", "Name", sortable=True),
        ListFieldDefinition("last_seen", "Last seen", sortable=True),
    ),
    default_sort="name",
    default_sort_dir="asc",
    default_per_page=25,
)


def build_network_device_list_query(
    *,
    device_type: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    search: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalise loose device-list request params through the page contract."""
    return NETWORK_DEVICE_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "type": device_type,
            "status": status,
            "vendor": vendor,
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )


if TYPE_CHECKING:
    from app.models.network import Port

logger = logging.getLogger(__name__)


def _network_device_is_olt_candidate(device: NetworkDevice) -> bool:
    """Treat monitoring devices with names ending in OLT as OLT list members."""
    return device.name.strip().lower().endswith("olt")


def resolve_olt_device_for_network_device(
    db: Session, device: NetworkDevice
) -> OLTDevice:
    """Return a dedicated OLTDevice for a promoted NetworkDevice, creating one when missing."""
    if device.mgmt_ip:
        matched = db.scalars(
            select(OLTDevice).where(OLTDevice.mgmt_ip == device.mgmt_ip)
        ).first()
        if matched:
            return matched
    if device.hostname:
        matched = db.scalars(
            select(OLTDevice).where(OLTDevice.hostname == device.hostname)
        ).first()
        if matched:
            return matched

    matched = db.scalars(select(OLTDevice).where(OLTDevice.name == device.name)).first()
    if matched:
        return matched

    payload = {
        "name": device.name,
        "hostname": device.hostname,
        "mgmt_ip": device.mgmt_ip,
        "vendor": device.vendor,
        "model": device.model,
        "serial_number": device.serial_number,
        "notes": _promoted_olt_notes(device.notes),
        "is_active": False,
    }
    try:
        return network_service.olt_devices.create(db=db, payload=payload)
    except IntegrityError:
        db.rollback()
        if device.mgmt_ip:
            matched = db.scalars(
                select(OLTDevice).where(OLTDevice.mgmt_ip == device.mgmt_ip)
            ).first()
            if matched:
                return matched
        if device.hostname:
            matched = db.scalars(
                select(OLTDevice).where(OLTDevice.hostname == device.hostname)
            ).first()
            if matched:
                return matched
        matched = db.scalars(
            select(OLTDevice).where(OLTDevice.name == device.name)
        ).first()
        if matched:
            return matched
        raise


def _promoted_olt_notes(notes: str | None) -> str:
    marker = (
        "Auto-created from monitoring inventory. Complete the OLT config pack "
        "and activate before provisioning."
    )
    existing = str(notes or "").strip()
    if not existing:
        return marker
    if marker in existing:
        return existing
    return f"{existing}\n\n{marker}"


def _status_presenter(raw_status: str | None) -> tuple[str, str]:
    status = (raw_status or "").strip().lower()
    if status == "online":
        return "Online", "online"
    if status == "offline":
        return "Offline", "offline"
    if status == "degraded":
        return "Degraded", "warning"
    if status:
        return status.replace("_", " ").title(), "default"
    return "Unknown", "default"


def _probe_state_presenter(*, enabled: bool, last_ok: bool | None) -> tuple[str, str]:
    """Map probe config/result to a user-facing label + tone."""
    if not enabled:
        return "Disabled", "disabled"
    if last_ok is True:
        return "OK", "ok"
    if last_ok is False:
        return "Fail", "fail"
    return "Unknown", "unknown"


def _monitoring_retired_probe_status() -> dict[str, str | None]:
    # The legacy per-device ICMP/SNMP probe source (Zabbix) was retired with
    # the native monitoring cutover; native reachability lives on the poll
    # columns / operational status instead.
    reason = "Legacy probe source is not configured"
    return {
        "ping_label": "Not working",
        "ping_state": "fail",
        "ping_reason": reason,
        "snmp_label": "Not working",
        "snmp_state": "fail",
        "snmp_reason": reason,
    }


def _build_legacy_probe_statuses(
    devices: list[dict[str, Any]],
) -> dict[str, dict[str, str | None]]:
    """Degraded ICMP/SNMP probe states for Network Devices core rows.

    The Zabbix probe source was retired with the native monitoring cutover, so
    every row reports that the retired probe is not working until native
    reachability data is available.
    """
    return {
        device_id: _monitoring_retired_probe_status()
        for device in devices
        if (device_id := str(device.get("id") or "").strip())
    }


def _find_linked_monitoring_status(
    *,
    olt: OLTDevice,
    by_mgmt_ip: dict[str, NetworkDevice],
    by_hostname: dict[str, NetworkDevice],
    by_name: dict[str, NetworkDevice],
) -> tuple[str, str]:
    linked: NetworkDevice | None = None
    if olt.mgmt_ip:
        linked = by_mgmt_ip.get(olt.mgmt_ip)
    if linked is None and olt.hostname:
        linked = by_hostname.get(olt.hostname)
    if linked is None and olt.name:
        linked = by_name.get(olt.name)
    status_raw = linked.status.value if (linked and linked.status) else None
    return _status_presenter(status_raw)


def get_cpe_ports(db: Session, cpe_id: object) -> list[Port]:
    """Return ports for a CPE device."""
    from sqlalchemy import select

    from app.models.network import Port

    return list(db.scalars(select(Port).where(Port.device_id == cpe_id)).all())


LIFECYCLE_ACTIVE = "active"
LIFECYCLE_INACTIVE = "inactive"


def _lifecycle_state(device: object) -> str:
    """Admission state of a source device, as projected onto its device row.

    The explicit marker that lets an inactive device stay visible in the staff
    ledger instead of disappearing from it. Devices with no admission flag at
    all (types listed active-only upstream) read ``active``.
    """
    return (
        LIFECYCLE_ACTIVE
        if getattr(device, "is_active", True) is not False
        else LIFECYCLE_INACTIVE
    )


def collect_devices(db: Session) -> list[dict]:
    """Collect all device types into a unified list of dicts.

    Every dict carries ``lifecycle_state`` (``active``/``inactive``). Inactive
    devices are included on purpose — see the core-device branch below.
    """
    devices: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    monitoring_devices = list(
        db.scalars(select(NetworkDevice).order_by(NetworkDevice.name.asc())).all()
    )
    by_mgmt_ip = {d.mgmt_ip: d for d in monitoring_devices if d.mgmt_ip}
    by_hostname = {d.hostname: d for d in monitoring_devices if d.hostname}
    by_name = {d.name: d for d in monitoring_devices if d.name}
    by_device_id = {str(d.id): d for d in monitoring_devices}
    site_name_by_id = {
        str(site_id): site_name
        for site_id, site_name in db.execute(select(PopSite.id, PopSite.name)).all()
    }
    warm_stale = warmer_is_stale()

    def _linked_monitoring(device: object) -> NetworkDevice | None:
        mgmt_ip = getattr(device, "mgmt_ip", None)
        hostname = getattr(device, "hostname", None)
        name = getattr(device, "name", None)
        return (
            (by_mgmt_ip.get(mgmt_ip) if mgmt_ip else None)
            or (by_hostname.get(hostname) if hostname else None)
            or (by_name.get(name) if name else None)
        )

    def _add_seen(kind: str, value: object | None) -> None:
        text = str(value or "").strip().lower()
        if text:
            seen_keys.add((kind, text))

    def _seen(kind: str, value: object | None) -> bool:
        text = str(value or "").strip().lower()
        return bool(text and (kind, text) in seen_keys)

    def _enum(value: object) -> object:
        return getattr(value, "value", value)

    def _dedup_linked(linked: NetworkDevice | None) -> None:
        if linked is not None:
            _add_seen("mgmt_ip", getattr(linked, "mgmt_ip", None))
            _add_seen("hostname", getattr(linked, "hostname", None))
            _add_seen("name", getattr(linked, "name", None))

    olts = network_service.olt_devices.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
    )
    for olt in olts:
        linked = _linked_monitoring(olt)
        linked_live_status = getattr(linked, "live_status", None)
        linked_live_status = getattr(linked_live_status, "value", linked_live_status)
        operational = derive_olt_operational_status(
            olt,
            linked_live_status=linked_live_status,
            warm_stale=warm_stale,
        )
        devices.append(
            {
                "id": str(olt.id),
                "name": olt.name,
                "type": "olt",
                "serial_number": getattr(olt, "serial_number", None),
                "ip_address": getattr(olt, "mgmt_ip", None),
                "vendor": olt.vendor,
                "model": olt.model,
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "lifecycle_state": _lifecycle_state(olt),
                "last_seen": getattr(olt, "last_seen", None),
                "subscriber": None,
                "class_facts": {
                    "software_version": getattr(olt, "software_version", None),
                    "firmware_version": getattr(olt, "firmware_version", None),
                    "pon_types": getattr(olt, "supported_pon_types", None),
                },
            }
        )
        _add_seen("id", olt.id)
        _add_seen("mgmt_ip", getattr(olt, "mgmt_ip", None))
        _add_seen("hostname", getattr(olt, "hostname", None))
        _add_seen("name", getattr(olt, "name", None))

    nas_devices = NasDevices.list(db, is_active=True, limit=1000)
    for nas in nas_devices:
        if _seen("mgmt_ip", getattr(nas, "management_ip", None)) or _seen(
            "name", getattr(nas, "name", None)
        ):
            continue
        nas_link_id = getattr(nas, "network_device_id", None)
        linked = by_device_id.get(str(nas_link_id)) if nas_link_id else None
        operational = derive_nas_operational_status(
            nas, linked_device=linked, warm_stale=warm_stale
        )
        devices.append(
            {
                "id": str(nas.id),
                "name": nas.name,
                "type": "nas",
                "serial_number": getattr(nas, "serial_number", None),
                "ip_address": getattr(nas, "management_ip", None),
                "vendor": _enum(getattr(nas, "vendor", None)),
                "model": getattr(nas, "model", None),
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "lifecycle_state": _lifecycle_state(nas),
                "last_seen": getattr(nas, "last_seen_at", None),
                "subscriber": None,
                "class_facts": {
                    "health_status": _enum(getattr(nas, "health_status", None)),
                    "site_name": site_name_by_id.get(
                        str(getattr(nas, "pop_site_id", None))
                    ),
                },
            }
        )
        _add_seen("id", nas.id)
        _add_seen("mgmt_ip", getattr(nas, "management_ip", None))
        _add_seen("name", nas.name)
        _dedup_linked(linked)

    routers = RouterInventory.list(db, limit=1000)
    for router in routers:
        if (
            _seen("mgmt_ip", getattr(router, "management_ip", None))
            or _seen("hostname", getattr(router, "hostname", None))
            or _seen("name", getattr(router, "name", None))
            or _seen("id", getattr(router, "nas_device_id", None))
        ):
            continue
        operational = derive_router_operational_status(router)
        devices.append(
            {
                "id": str(router.id),
                "name": router.name,
                "type": "router",
                "serial_number": getattr(router, "serial_number", None),
                "ip_address": getattr(router, "management_ip", None),
                "vendor": "mikrotik",
                "model": getattr(router, "board_name", None),
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "lifecycle_state": _lifecycle_state(router),
                "last_seen": getattr(router, "last_seen_at", None),
                "subscriber": None,
                "class_facts": {
                    "routeros_version": getattr(router, "routeros_version", None),
                    "location": getattr(router, "location", None),
                },
            }
        )
        _add_seen("id", router.id)
        _add_seen("mgmt_ip", getattr(router, "management_ip", None))
        _add_seen("hostname", getattr(router, "hostname", None))
        _add_seen("name", router.name)
        router_link_id = getattr(router, "network_device_id", None)
        _dedup_linked(by_device_id.get(str(router_link_id)) if router_link_id else None)

    # Inactive core devices STAY in the ledger. Filtering them out here made
    # deactivation erase the device: the projection reconciler deletes any row
    # this function stops returning, so a soft-deleted device silently vanished
    # from the staff device list (the "one device missing from every base
    # station" symptom). They are returned with an explicit inactive marker
    # instead, and network.device_state forces them to not_working.
    core_devices = list(monitoring_devices)
    annotate_operational_status(core_devices)
    for device in core_devices:
        if _network_device_is_olt_candidate(device):
            continue
        if (
            _seen("mgmt_ip", getattr(device, "mgmt_ip", None))
            or _seen("hostname", getattr(device, "hostname", None))
            or _seen("name", getattr(device, "name", None))
        ):
            continue
        operational = cast(Any, device).operational
        devices.append(
            {
                "id": str(device.id),
                "name": device.name,
                "type": "core",
                "serial_number": device.serial_number,
                "ip_address": device.mgmt_ip,
                "vendor": device.vendor,
                "model": device.model,
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "lifecycle_state": _lifecycle_state(device),
                "last_seen": device.last_ping_at or device.last_snmp_at,
                "subscriber": None,
                "class_facts": {
                    "role": _enum(getattr(device, "role", None)),
                    "site_name": site_name_by_id.get(
                        str(getattr(device, "pop_site_id", None))
                    ),
                    "lifecycle_state": _lifecycle_state(device),
                },
            }
        )
        _add_seen("id", device.id)
        _add_seen("mgmt_ip", device.mgmt_ip)
        _add_seen("hostname", device.hostname)
        _add_seen("name", device.name)

    onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    for ont in onts:
        operational = derive_ont_operational_status(ont)
        ont_signal_at = getattr(ont, "signal_updated_at", None)
        devices.append(
            {
                "id": str(ont.id),
                "name": getattr(ont, "name", None) or ont.serial_number,
                "type": "ont",
                "serial_number": ont.serial_number,
                "ip_address": getattr(ont, "ip_address", None),
                "vendor": ont.vendor,
                "model": ont.model,
                "status": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "lifecycle_state": _lifecycle_state(ont),
                "last_seen": getattr(ont, "last_seen", None),
                "subscriber": None,
                "class_facts": {
                    "onu_rx_dbm": getattr(ont, "onu_rx_signal_dbm", None),
                    "olt_rx_dbm": getattr(ont, "olt_rx_signal_dbm", None),
                    "onu_tx_dbm": getattr(ont, "onu_tx_signal_dbm", None),
                    "signal_updated_at": ont_signal_at.isoformat()
                    if ont_signal_at
                    else None,
                },
            }
        )

    cpes = network_service.cpe_devices.list(
        db=db,
        subscriber_id=None,
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    inventory_subscriber_id = cpe_service.get_inventory_subscriber_id(db)
    for cpe in cpes:
        if (
            inventory_subscriber_id is not None
            and getattr(cpe, "subscriber_id", None) == inventory_subscriber_id
        ):
            continue
        devices.append(
            {
                "id": str(cpe.id),
                "name": getattr(cpe, "name", None)
                or getattr(cpe, "serial_number", str(cpe.id)[:8]),
                "type": "cpe",
                "serial_number": getattr(cpe, "serial_number", None),
                "ip_address": getattr(cpe, "ip_address", None),
                "vendor": getattr(cpe, "vendor", None),
                "model": getattr(cpe, "model", None),
                "status": NOT_WORKING,
                "operational_reason": "verification_not_configured",
                "status_presentation": device_operational_status_presentation(
                    NOT_WORKING
                ),
                "lifecycle_state": _lifecycle_state(cpe),
                "last_seen": getattr(cpe, "last_seen", None),
                "subscriber": None,
                "class_facts": {
                    "firmware_version": getattr(cpe, "firmware_version", None),
                    "mac_address": getattr(cpe, "mac_address", None),
                },
            }
        )

    return devices


def _device_matches_search(device: dict, term: str) -> bool:
    """Check if any device field matches the search term."""
    haystack = [
        device.get("name"),
        device.get("serial_number"),
        device.get("ip_address"),
        device.get("vendor"),
        device.get("model"),
        device.get("type"),
    ]
    return any((value or "").lower().find(term) != -1 for value in haystack)


def filter_devices(
    devices: list[dict],
    *,
    device_type: str | None = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
) -> list[dict]:
    """Apply optional filters to a device list."""
    if device_type and device_type != "all":
        devices = [d for d in devices if d["type"] == device_type]

    term = (search or "").strip().lower()
    if term:
        devices = [d for d in devices if _device_matches_search(d, term)]

    status_filter = (status or "").strip().lower()
    if status_filter:
        devices = [
            d for d in devices if (d.get("status") or "").lower() == status_filter
        ]

    vendor_filter = (vendor or "").strip().lower()
    if vendor_filter:
        devices = [
            d for d in devices if (d.get("vendor") or "").lower() == vendor_filter
        ]

    return devices


def compute_device_stats(devices: list[dict]) -> dict[str, int]:
    """Compute summary stats for a filtered device list."""
    return {
        "total": len(devices),
        "core": sum(1 for d in devices if d["type"] == "core"),
        "olt": sum(1 for d in devices if d["type"] == "olt"),
        "ont": sum(1 for d in devices if d["type"] == "ont"),
        "cpe": sum(1 for d in devices if d["type"] == "cpe"),
        "working": sum(1 for d in devices if d["status"] == WORKING),
        "not_working": sum(1 for d in devices if d["status"] == NOT_WORKING),
    }


# Device-count summary tiles as KPI contracts. Each drills into the exact
# cohort it counts: type tiles narrow by ``type`` (across every status), status
# tiles by ``status`` (across every type), and every tile carries the surface's
# active vendor/search. Tile counts are an overview computed independent of the
# page status/type filter, so a headline and the list it links to can never
# diverge and a tile never shrinks because the table below it was filtered
# (KPI-parity rule).
_TYPE_KPI_LABELS = {
    "total": "All Devices",
    "core": "Core",
    "olt": "OLT",
    "ont": "ONT",
    "cpe": "CPE",
}
_STATUS_KPI_LABELS = {
    "working": "Working",
    "not_working": "Not working",
}
_STATUS_KPI_TONES = {
    "working": StatusTone.positive,
    "not_working": StatusTone.negative,
}
# Device types that expose an operator-driven reboot; CPE/unknown rows do not.
_REBOOTABLE_DEVICE_TYPES = {"core", "olt", "ont"}


def _device_cohort_url(
    list_query: ListQuery,
    *,
    device_type: str | None = None,
    status: str | None = None,
) -> str:
    """Drill-down URL to the device list filtered to exactly a KPI's cohort.

    A tile carries ONLY its own narrowing dimension (``device_type`` for the
    type/total tiles, ``status`` for the status tiles) plus the surface's active
    vendor/search. It deliberately does NOT inherit the page's active status/type
    filter, so a type tile drills across every status and a status tile across
    every type — matching the overview count the tile displays (KPI-parity rule).
    """
    params = {
        "type": device_type,
        "status": status,
        "vendor": list_query.filter_value("vendor"),
        "search": list_query.search,
    }
    query = urlencode({key: value for key, value in params.items() if value})
    return "/admin/network/devices" + (f"?{query}" if query else "")


def _device_stat_kpis(
    stats: dict[str, int],
    list_query: ListQuery,
) -> dict[str, Kpi]:
    """Wrap the canonical binary projection counts as KPI contracts."""

    def _count(key: str) -> StateValue:
        return StateValue.present(int(stats.get(key, 0)))

    kpis: dict[str, Kpi] = {
        "total": Kpi(
            label=_TYPE_KPI_LABELS["total"],
            value=_count("total"),
            # "All devices" drills across every type and status; only the
            # active vendor/search narrow the cohort.
            cohort_url=_device_cohort_url(list_query, device_type="all"),
        )
    }
    for key in ("core", "olt", "ont", "cpe"):
        kpis[key] = Kpi(
            label=_TYPE_KPI_LABELS[key],
            value=_count(key),
            cohort_url=_device_cohort_url(list_query, device_type=key),
        )
    for key, label in _STATUS_KPI_LABELS.items():
        kpis[key] = Kpi(
            label=label,
            value=_count(key),
            cohort_url=_device_cohort_url(list_query, status=key),
            tone=_STATUS_KPI_TONES[key],
        )
    return kpis


def _device_row_actions(device: dict) -> dict[str, Action]:
    """Per-row management actions with eligibility owned here, not the template.

    Ping/reboot eligibility is a data-availability fact (a reachable management
    IP, a device type that can be rebooted), computed once so the template hides
    or disables what cannot run instead of re-deriving it from a status string.
    """
    has_ip = bool(str(device.get("ip_address") or "").strip())
    device_type = str(device.get("type") or "").strip().lower()
    rebootable = device_type in _REBOOTABLE_DEVICE_TYPES
    can_ping = has_ip
    can_reboot = rebootable and has_ip
    device_id = str(device.get("id") or "").strip()
    return {
        "view": Action(
            key="view",
            label="View Details",
            allowed=True,
            permission="network:device:read",
        ),
        "ping": Action(
            key="ping",
            label="Ping Device",
            allowed=can_ping,
            reason=None if can_ping else "No management IP on record",
            permission="network:device:write",
            tone=StatusTone.positive,
        ),
        "reboot": Action(
            key="reboot",
            label="Reboot Device",
            allowed=can_reboot,
            reason=None
            if can_reboot
            else (
                "No management IP on record"
                if rebootable
                else "Reboot is not available for this device type"
            ),
            permission="network:device:write",
            preview_url=(
                f"/admin/network/devices/{device_id}/reboot/preview"
                if can_reboot and device_id
                else None
            ),
            affected=1 if can_reboot else 0,
            tone=StatusTone.warning,
            requires_confirmation=can_reboot and bool(device_id),
        ),
        "delete": Action(
            key="delete",
            label="Remove Device",
            allowed=False,
            reason="Removal is not supported from this inventory",
            permission="network:device:write",
            tone=StatusTone.negative,
        ),
    }


def _query_page(db: Session, list_query: ListQuery) -> tuple[list[dict], int]:
    return device_projection_views.query_device_projections(
        db,
        device_type=list_query.filter_value("type"),
        status=list_query.filter_value("status"),
        vendor=list_query.filter_value("vendor"),
        search=list_query.search,
        sort_by=list_query.sort_by,
        sort_dir=list_query.sort_dir,
        offset=list_query.offset,
        limit=list_query.per_page,
    )


def devices_list_page_data(db: Session, list_query: ListQuery) -> dict[str, object]:
    """Return full payload for the devices index page.

    Reads the materialised device_projections table (SQL search/filter/sort/
    paginate) via device_projection_views — the canonical read model — instead of
    aggregating and filtering every device in memory. Projection repair age is
    internal and never becomes a third operational state.
    """
    devices, total = _query_page(db, list_query)
    for device in devices:
        device["actions"] = _device_row_actions(device)
    stats = device_projection_views.device_projection_stats(
        db,
        device_type=list_query.filter_value("type"),
        status=list_query.filter_value("status"),
        vendor=list_query.filter_value("vendor"),
        search=list_query.search,
    )
    # KPI tiles are a fixed overview: each tile counts its own cohort across
    # every status and type, so the headline number never shrinks because the
    # operator filtered the table below it. The counts drop the page status/type
    # filter (keeping only vendor/search, which each tile's cohort_url also
    # carries) so a tile's value equals the count at the cohort it links to.
    overview_stats = device_projection_views.device_projection_stats(
        db,
        device_type=None,
        status=None,
        vendor=list_query.filter_value("vendor"),
        search=list_query.search,
    )
    per_page = list_query.per_page
    total_pages = (total + per_page - 1) // per_page if total else 1
    device_type = list_query.filter_value("type")
    return {
        "devices": devices,
        "stats": stats,
        "device_kpis": _device_stat_kpis(overview_stats, list_query),
        "device_type": device_type,
        "type": device_type,
        "search": list_query.search or "",
        "status": list_query.filter_value("status") or "",
        "vendor": list_query.filter_value("vendor") or "",
        # Pagination context consumed by components/data/table_pagination.html.
        "pagination": total > per_page,
        "offset": list_query.offset,
        "limit": per_page,
        "total": total,
        "total_pages": total_pages,
        "page": list_query.page,
        "per_page": per_page,
        "htmx_url": "/admin/network/devices/filter",
        "htmx_target": "devices-table-body",
    }


def devices_search_data(db: Session, list_query: ListQuery) -> list[dict]:
    """Return one page of matching devices for the HTMX search/filter partial."""
    devices, _total = _query_page(db, list_query)
    for device in devices:
        device["actions"] = _device_row_actions(device)
    return devices


def devices_filter_data(db: Session, list_query: ListQuery) -> list[dict]:
    """Return one page of filtered devices for the HTMX filter partial."""
    devices, _total = _query_page(db, list_query)
    for device in devices:
        device["actions"] = _device_row_actions(device)
    return devices


def olts_list_page_data(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return OLT list payload with per-OLT stats."""
    per_page = min(max(int(per_page or 50), 10), 200)
    raw_olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name.asc())
        ).all()
    )

    olt_stats = {}
    for olt in raw_olts:
        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=str(olt.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        olt_stats[str(olt.id)] = {"pon_ports": len(pon_ports)}

    promoted_olts = [
        device
        for device in db.scalars(
            select(NetworkDevice).order_by(NetworkDevice.name)
        ).all()
        if device.is_active and _network_device_is_olt_candidate(device)
    ]
    promoted_olt_records = [
        resolve_olt_device_for_network_device(db, device) for device in promoted_olts
    ]
    all_olts_by_id = {str(olt.id): olt for olt in raw_olts}
    for olt in promoted_olt_records:
        all_olts_by_id[str(olt.id)] = olt

    all_olts = sorted(
        all_olts_by_id.values(), key=lambda olt: str(olt.name or "").lower()
    )
    monitoring_devices = list(db.scalars(select(NetworkDevice)).all())

    by_mgmt_ip = {d.mgmt_ip: d for d in monitoring_devices if d.mgmt_ip}
    by_hostname = {d.hostname: d for d in monitoring_devices if d.hostname}
    by_name = {d.name: d for d in monitoring_devices if d.name}

    linked_monitoring_by_olt_id: dict[str, NetworkDevice] = {}
    for olt in all_olts:
        linked = None
        if olt.mgmt_ip:
            linked = by_mgmt_ip.get(olt.mgmt_ip)
        if linked is None and olt.hostname:
            linked = by_hostname.get(olt.hostname)
        if linked is None and olt.name:
            linked = by_name.get(olt.name)
        if linked is not None:
            linked_monitoring_by_olt_id[str(olt.id)] = linked

    warm_stale = warmer_is_stale()

    linked_ids = [d.id for d in linked_monitoring_by_olt_id.values() if d.id]
    interfaces_by_device_id: dict[str, list[DeviceInterface]] = {}
    if linked_ids:
        interfaces = list(
            db.scalars(
                select(DeviceInterface).where(DeviceInterface.device_id.in_(linked_ids))
            ).all()
        )
        for iface in interfaces:
            interfaces_by_device_id.setdefault(str(iface.device_id), []).append(iface)

    # Apply detail-page style fallback: if no modeled PON ports exist for an OLT,
    # use discovered SNMP interfaces with PON-like naming.
    for olt in all_olts:
        olt_id = str(olt.id)
        db_count = int(olt_stats.get(olt_id, {}).get("pon_ports", 0))
        linked = linked_monitoring_by_olt_id.get(olt_id)
        snmp_count = 0
        if linked and linked.id:
            iface_rows = interfaces_by_device_id.get(str(linked.id), [])
            snmp_count = sum(
                1
                for iface in iface_rows
                if any(
                    token in f"{iface.name or ''} {iface.description or ''}".lower()
                    for token in ("pon", "gpon", "epon", "xgpon", "xgs")
                )
            )
        resolved_count = db_count if db_count > 0 else snmp_count
        olt_stats[olt_id] = {"pon_ports": resolved_count}

    olts = []
    for olt in all_olts:
        service_port_summary = imported_service_port_summary(db, olt_id=olt.id)
        linked = linked_monitoring_by_olt_id.get(str(olt.id))
        status_label, status_variant = _find_linked_monitoring_status(
            olt=olt,
            by_mgmt_ip=by_mgmt_ip,
            by_hostname=by_hostname,
            by_name=by_name,
        )
        ping_label, ping_state = _probe_state_presenter(
            enabled=bool(linked and linked.ping_enabled),
            last_ok=(linked.last_ping_ok if linked else None),
        )
        snmp_label, snmp_state = _probe_state_presenter(
            enabled=bool(linked and linked.snmp_enabled),
            last_ok=(linked.last_snmp_ok if linked else None),
        )
        linked_live_status = getattr(linked, "live_status", None)
        linked_live_status = getattr(linked_live_status, "value", linked_live_status)
        operational = derive_olt_operational_status(
            olt,
            linked_live_status=linked_live_status,
            warm_stale=warm_stale,
        )
        if operational.alarming or operational.impaired:
            health_state = "attention"
            health_label = "Attention"
            health_reason = operational.reason.replace("_", " ").capitalize()
        elif operational.status == NOT_WORKING:
            health_state = NOT_WORKING
            health_label = "Not working"
            health_reason = operational.reason.replace("_", " ").capitalize()
        else:
            health_state = WORKING
            health_label = "Working"
            health_reason = "Verification confirms operation"

        # The legacy runtime-health overlay (Zabbix telemetry) was retired
        # with the native monitoring cutover: rows keep their local monitoring
        # values, matching the empty overlay the unconfigured path produced.
        runtime_health: dict[str, object] = {}

        olts.append(
            {
                "id": str(olt.id),
                "name": olt.name,
                "hostname": olt.hostname,
                "vendor": olt.vendor,
                "model": olt.model,
                "mgmt_ip": olt.mgmt_ip,
                "is_active": bool(olt.is_active),
                "runtime_status_label": status_label,
                "runtime_status_variant": status_variant,
                "runtime_health_label": health_label,
                "runtime_health_state": health_state,
                "runtime_health_reason": health_reason,
                "operational_status": operational.status,
                "runtime_operational_reason": operational.reason,
                "status_presentation": operational.presentation,
                "runtime_ping_label": ping_label,
                "runtime_ping_state": ping_state,
                "runtime_snmp_label": snmp_label,
                "runtime_snmp_state": snmp_state,
                "runtime_source": runtime_health.get("runtime_source", "Local"),
                "runtime_last_seen_at": runtime_health.get("runtime_last_seen_at"),
                "runtime_trigger_summary": runtime_health.get(
                    "runtime_trigger_summary"
                ),
                "runtime_ont_online": runtime_health.get("runtime_ont_online"),
                "runtime_ont_offline": runtime_health.get("runtime_ont_offline"),
                "runtime_ont_total": runtime_health.get("runtime_ont_total"),
                "runtime_ont_online_pct": runtime_health.get("runtime_ont_online_pct"),
                "runtime_low_signal": runtime_health.get("runtime_low_signal"),
                "runtime_pon_up": runtime_health.get("runtime_pon_up"),
                "runtime_pon_total": runtime_health.get("runtime_pon_total"),
                "pon_ports": olt_stats.get(str(olt.id), {}).get("pon_ports", 0),
                "imported_service_ports": service_port_summary["count"],
                "imported_service_ports_at": service_port_summary["last_imported_at"],
                "detail_url": f"/admin/network/olts/{olt.id}",
                "edit_url": f"/admin/network/olts/{olt.id}/edit",
            }
        )

    term = (search or "").strip().lower()
    filtered_olts = olts
    if term:
        filtered_olts = [
            item
            for item in filtered_olts
            if term in str(item.get("name") or "").lower()
            or term in str(item.get("hostname") or "").lower()
            or term in str(item.get("vendor") or "").lower()
            or term in str(item.get("model") or "").lower()
            or term in str(item.get("mgmt_ip") or "").lower()
        ]

    normalized_status = (status or "").strip().lower()
    if normalized_status == "attention":
        filtered_olts = [
            item
            for item in filtered_olts
            if item.get("runtime_health_state") == "attention"
        ]
    elif normalized_status in {"up", "online", "healthy", WORKING}:
        filtered_olts = [
            item for item in filtered_olts if item.get("operational_status") == WORKING
        ]
    elif normalized_status in {"down", "offline", NOT_WORKING}:
        filtered_olts = [
            item
            for item in filtered_olts
            if item.get("operational_status") == NOT_WORKING
        ]

    filtered_total = len(filtered_olts)
    total_pages = max(1, (filtered_total + per_page - 1) // per_page)
    current_page = min(max(page, 1), total_pages)
    page_start = (current_page - 1) * per_page
    paged_olts = filtered_olts[page_start : page_start + per_page]

    attention_items = [
        item for item in olts if item.get("runtime_health_state") == "attention"
    ]
    working_count = sum(1 for item in olts if item.get("operational_status") == WORKING)
    not_working_count = sum(
        1 for item in olts if item.get("operational_status") == NOT_WORKING
    )
    total_pon_ports = sum(int(item.get("pon_ports") or 0) for item in olts)  # type: ignore[call-overload]

    stats = {
        "total": filtered_total,
        "fleet_total": len(olts),
        "active": sum(1 for o in filtered_olts if o["is_active"]),
        "attention": len(attention_items),
        "working": working_count,
        "not_working": not_working_count,
        "total_pon_ports": total_pon_ports,
    }

    attention_summary = attention_items[:6]

    return {
        "olts": paged_olts,
        "olt_stats": olt_stats,
        "stats": stats,
        "attention_summary": attention_summary,
        "filters": {
            "search": search or "",
            "status": normalized_status,
        },
        "pagination": {
            "page": current_page,
            "per_page": per_page,
            "total": filtered_total,
            "total_pages": total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < total_pages,
        },
    }
