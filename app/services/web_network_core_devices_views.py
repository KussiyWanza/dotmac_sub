"""OLT/ONT/detail/consolidated helpers for core-network device web routes."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, func, literal, not_, or_, select, union_all
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import CPEDevice, OLTDevice, OntUnit, PonPort
from app.models.network_monitoring import (
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DeviceStatus,
    MetricType,
    NetworkDevice,
)
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.router_management import Router, RouterStatus
from app.models.subscriber import SubscriberCategory
from app.models.tr069 import Tr069CpeDevice
from app.services import network as network_service
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.services.network._common import decode_huawei_hex_serial, encode_to_hex_serial
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.olt_polling_parsers import _decode_huawei_packed_fsp
from app.services.network.ont_service_configuration import (
    get_ont_service_configuration_projection,
)
from app.services.network.ont_status import resolve_effective_ont_status
from app.services.network.provisioning_events import (
    ProvisioningLifecycleScope,
    list_ont_provisioning_events,
)
from app.services.web_network_core_devices_inventory import (
    _build_legacy_probe_statuses,
    resolve_olt_device_for_network_device,
)
from app.services.web_network_pon_interfaces import parse_pon_port_notes

logger = logging.getLogger(__name__)

# Staleness threshold for auto-refresh (15 minutes)
_STALE_THRESHOLD_MINUTES = 15

BACKUP_OVERVIEW_LIST_DEFINITION = ListDefinition(
    key="admin.network.backups",
    fields=(
        ListFieldDefinition("device_name", "Device", searchable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("device_type", "Device type", filterable=True),
        ListFieldDefinition("stale_hours", "Stale threshold", filterable=True),
        ListFieldDefinition("last_backup", "Last backup", sortable=True),
    ),
    default_sort="last_backup",
    default_sort_dir="asc",
    default_per_page=25,
    per_page_options=(10, 25, 50, 100),
)

BackupOverviewStatus = Literal["success", "stale", "failed"]
BackupOverviewDeviceType = Literal["nas", "olt"]


@dataclass(frozen=True, slots=True)
class BackupOverviewQuery:
    list_query: ListQuery
    stale_hours: int

    @property
    def status(self) -> str:
        return self.list_query.filter_value("status") or "all"

    @property
    def device_type(self) -> str:
        return self.list_query.filter_value("device_type") or "all"

    @property
    def sort_filter(self) -> str:
        return f"last_backup_{self.list_query.sort_dir}"


@dataclass(frozen=True, slots=True)
class BackupOverviewRow:
    id: str
    device_id: str
    backup_id: str | None
    device_name: str
    device_type: BackupOverviewDeviceType
    group: str
    vendor: str | None
    model: str | None
    ip_address: str
    port: str
    last_backup_at: datetime | None
    last_message: str
    backup_status: BackupOverviewStatus
    device_url: str
    backup_url: str | None
    history_url: str


@dataclass(frozen=True, slots=True)
class BackupOverviewStats:
    total: int
    success: int
    stale: int
    failed: int
    nas: int
    olt: int


@dataclass(frozen=True, slots=True)
class BackupOverviewPage:
    rows: tuple[BackupOverviewRow, ...]
    stats: BackupOverviewStats
    query: BackupOverviewQuery
    page_meta: PageMeta


def build_backup_overview_query(
    *,
    status: str | None = None,
    device_type: str | None = None,
    search: str | None = None,
    stale_hours: int = 24,
    sort: str = "last_backup_asc",
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> BackupOverviewQuery:
    normalized_status = (
        str(status).strip().lower()
        if status and str(status).strip().lower() in {"success", "stale", "failed"}
        else None
    )
    normalized_device_type = (
        str(device_type).strip().lower()
        if device_type and str(device_type).strip().lower() in {"nas", "olt"}
        else None
    )
    normalized_stale_hours = min(max(int(stale_hours), 1), 720)
    normalized_sort_dir = (
        "desc"
        if sort == "last_backup_desc" or (sort == "last_backup" and sort_dir == "desc")
        else "asc"
    )
    list_query = BACKUP_OVERVIEW_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "status": normalized_status,
            "device_type": normalized_device_type,
            "stale_hours": str(normalized_stale_hours),
        },
        sort_by="last_backup",
        sort_dir=normalized_sort_dir,
        page=page,
        per_page=per_page,
    )
    return BackupOverviewQuery(
        list_query=list_query,
        stale_hours=normalized_stale_hours,
    )


_PROVISIONING_STATUS_META: dict[str, dict[str, str]] = {
    "unprovisioned": {
        "headline": "Not provisioned",
        "summary": "No provisioning apply has completed for this ONT yet.",
        "next_step": "Open Configure to start the OLT baseline and ACS setup.",
        "tone": "slate",
    },
    "partial": {
        "headline": "Provisioning partially applied",
        "summary": "Some provisioning work completed, but the ONT is not fully confirmed yet.",
        "next_step": "Review the stage results below before retrying.",
        "tone": "amber",
    },
    "pending_acs_registration": {
        "headline": "Waiting for ACS registration",
        "summary": "OLT-side provisioning completed, but ACS has not confirmed the device yet.",
        "next_step": "Wait for the next inform or check the management and TR-069 path.",
        "tone": "sky",
    },
    "pending_service_config": {
        "headline": "Waiting for service configuration",
        "summary": "The ONT reached ACS, but customer service settings are incomplete or still pending.",
        "next_step": "Complete or reapply the saved WAN, LAN, or WiFi service settings.",
        "tone": "amber",
    },
    "provisioned": {
        "headline": "Provisioned",
        "summary": "Provisioning completed successfully.",
        "next_step": "No operator action is required unless service drift is reported.",
        "tone": "emerald",
    },
    "drift_detected": {
        "headline": "Configuration drift detected",
        "summary": "The ONT no longer matches the intended configuration state.",
        "next_step": "Review drift and decide whether to reapply or investigate device-side changes.",
        "tone": "amber",
    },
    "failed": {
        "headline": "Provisioning failed",
        "summary": "Provisioning hit a hard failure that needs operator attention.",
        "next_step": "Review the failed stage below before retrying.",
        "tone": "rose",
    },
}

_PROVISIONING_TONE_CLASSES: dict[str, dict[str, str]] = {
    "slate": {
        "badge": "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
        "panel": "border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800",
        "accent": "text-slate-700 dark:text-slate-300",
        "dot": "bg-slate-400",
    },
    "sky": {
        "badge": "bg-sky-100 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300",
        "panel": "border-sky-200 bg-sky-50/50 dark:border-sky-900/40 dark:bg-sky-950/10",
        "accent": "text-sky-700 dark:text-sky-300",
        "dot": "bg-sky-500",
    },
    "amber": {
        "badge": "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300",
        "panel": "border-amber-200 bg-amber-50/40 dark:border-amber-900/40 dark:bg-amber-950/10",
        "accent": "text-amber-700 dark:text-amber-300",
        "dot": "bg-amber-500",
    },
    "emerald": {
        "badge": "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300",
        "panel": "border-emerald-200 bg-emerald-50/40 dark:border-emerald-900/40 dark:bg-emerald-950/10",
        "accent": "text-emerald-700 dark:text-emerald-300",
        "dot": "bg-emerald-500",
    },
    "rose": {
        "badge": "bg-rose-100 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300",
        "panel": "border-rose-200 bg-rose-50/40 dark:border-rose-900/40 dark:bg-rose-950/10",
        "accent": "text-rose-700 dark:text-rose-300",
        "dot": "bg-rose-500",
    },
}

_PROVISIONING_DOMAIN_LABELS: dict[str, str] = {
    "config_pack_resolution": "Config pack",
    "olt_l2_apply": "OLT service path",
    "management_path_apply": "Management path",
    "omci_wan_apply": "OMCI WAN apply",
    "tr069_bind_apply": "TR-069 bind",
    "olt_or_omci_readback_verify": "Device readback",
    "acs_bootstrap_verify": "ACS registration",
}

_PROVISIONING_DOMAIN_ORDER = [
    "config_pack_resolution",
    "olt_l2_apply",
    "management_path_apply",
    "omci_wan_apply",
    "tr069_bind_apply",
    "olt_or_omci_readback_verify",
    "acs_bootstrap_verify",
]

_PROVISIONING_DOMAIN_STATUS_META: dict[str, dict[str, str]] = {
    "succeeded": {"label": "Completed", "tone": "emerald"},
    "pending_verification": {"label": "Waiting", "tone": "sky"},
    "retryable_failure": {"label": "Needs retry", "tone": "amber"},
    "terminal_failure": {"label": "Failed", "tone": "rose"},
}

_PROVISIONING_FAILURE_LABELS: dict[str, str] = {
    "config_pack_missing": "Config pack missing",
    "config_pack_mismatch": "Config pack mismatch",
    "config_pack_incomplete": "Config pack incomplete",
    "regional_pack_resolution_failure": "Regional config resolution failed",
    "service_port_conflict": "Service-port conflict",
    "management_path_missing": "Management path missing",
    "omci_apply_failure": "OMCI apply failed",
    "omci_readback_failure": "OMCI readback failed",
    "tr069_binding_readback_miss": "TR-069 binding not confirmed yet",
    "acs_bootstrap_timeout": "ACS registration timeout",
    "olt_state_conflict": "OLT state conflict",
    "vendor_capability_gap": "Vendor capability gap",
    "transport_timeout": "Transport timeout",
}


def _maybe_queue_stale_ont_refresh(db: Session, ont: object, *, ont_id: str) -> bool:
    """Queue a background TR-069 refresh if ONT data is stale.

    Checks if tr069_last_snapshot_at is older than _STALE_THRESHOLD_MINUTES.
    If stale and ONT has TR-069 configured, queues a Celery task to refresh.

    Returns True if a refresh was queued, False otherwise.
    """
    snapshot_at = getattr(ont, "tr069_last_snapshot_at", None)
    if snapshot_at is None:
        return False

    # Check if snapshot is stale
    now = datetime.now(UTC)
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=UTC)

    age = now - snapshot_at
    if age < timedelta(minutes=_STALE_THRESHOLD_MINUTES):
        return False

    has_acs_identity = (
        db.query(Tr069CpeDevice.id)
        .filter(Tr069CpeDevice.ont_unit_id == getattr(ont, "id", None))
        .filter(Tr069CpeDevice.is_active.is_(True))
        .filter(Tr069CpeDevice.genieacs_device_id.isnot(None))
        .first()
        is not None
    )
    if not has_acs_identity:
        return False

    # Queue background refresh
    try:
        from app.tasks.tr069 import refresh_single_ont_runtime

        refresh_single_ont_runtime.delay(ont_id)
        logger.info(
            "Queued stale ONT refresh for %s (age=%s)",
            ont_id,
            age,
        )
        return True
    except Exception:
        logger.warning(
            "Failed to queue stale ONT refresh for %s",
            ont_id,
            exc_info=True,
        )
        return False


def _nas_status_to_monitoring_status(status: NasDeviceStatus | None) -> DeviceStatus:
    if status == NasDeviceStatus.maintenance:
        return DeviceStatus.maintenance
    if status == NasDeviceStatus.active:
        return DeviceStatus.online
    return DeviceStatus.offline


def _enum_value(value) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)


def _router_status_to_live_status(router: Router | None) -> str | None:
    if router is None:
        return None
    status = _enum_value(getattr(router, "status", None))
    if status == RouterStatus.online.value:
        return "up"
    if status in {RouterStatus.offline.value, RouterStatus.unreachable.value}:
        return "down"
    if status == RouterStatus.degraded.value:
        return "problem"
    return None


def _nas_inventory_stub(
    device: NasDevice,
    *,
    linked_device: NetworkDevice | None = None,
    linked_router: Router | None = None,
) -> SimpleNamespace:
    router_live_status = _router_status_to_live_status(linked_router)
    live_status = getattr(linked_device, "live_status", None) or router_live_status
    live_status_at = getattr(linked_device, "live_status_at", None) or getattr(
        linked_router, "last_seen_at", None
    )
    return SimpleNamespace(
        id=device.id,
        name=device.name,
        hostname=device.code,
        mgmt_ip=device.management_ip or device.ip_address or device.nas_ip,
        vendor=device.vendor.value if device.vendor else None,
        model=device.model,
        serial_number=device.serial_number,
        role=DeviceRole.access,
        status=_nas_status_to_monitoring_status(device.status),
        live_status=live_status,
        live_status_at=live_status_at,
        zabbix_hostid=getattr(linked_device, "zabbix_hostid", None)
        or device.zabbix_host_id,
        last_ping_ok=getattr(linked_device, "last_ping_ok", None),
        last_ping_at=getattr(linked_device, "last_ping_at", None),
        last_snmp_ok=getattr(linked_device, "last_snmp_ok", None),
        last_snmp_at=getattr(linked_device, "last_snmp_at", None),
        detail_url=f"/admin/network/nas/devices/{device.id}",
        edit_url=f"/admin/network/nas/{device.id}/edit",
    )


def _normalize_port_name(value: str | None) -> str:
    """Normalize interface/port names for loose matching."""
    if not value:
        return ""
    text = str(value).strip()
    hint = _extract_pon_hint(text)
    if hint:
        return hint.lower()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _dedupe_live_board_inventory(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Collapse duplicate ENTITY-MIB rows that point to the same physical slot."""
    deduped: list[dict[str, object]] = []
    seen: dict[tuple[object, object], int] = {}

    for item in items:
        category = item.get("category")
        slot_number = item.get("slot_number")
        if category == "card" and slot_number is not None:
            key = (category, slot_number)
        else:
            key = (
                category,
                str(item.get("card_type") or "").strip().lower() or item.get("index"),
            )

        existing_idx = seen.get(key)
        if existing_idx is None:
            seen[key] = len(deduped)
            deduped.append(dict(item))
            continue

        existing = deduped[existing_idx]
        current_label = str(existing.get("card_type") or "")
        candidate_label = str(item.get("card_type") or "")
        if len(candidate_label) > len(current_label):
            existing["card_type"] = item.get("card_type")
        if existing.get("slot_number") is None and slot_number is not None:
            existing["slot_number"] = slot_number
        if existing.get("index") is None and item.get("index") is not None:
            existing["index"] = item.get("index")

    return deduped


def _extract_range_display(*values: object) -> str | None:
    """Best-effort parser for optical range text like '20km' from free text."""
    for value in values:
        if not value:
            continue
        text = str(value)
        match = re.search(r"(\d+(?:\.\d+)?)\s*km\b", text, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1)} km"
    return None


def _extract_tx_power_dbm(*values: object) -> float | None:
    """Best-effort parser for TX power text like 'Tx -2.3 dBm'."""
    for value in values:
        if not value:
            continue
        text = str(value)
        tx_match = re.search(
            r"tx[^-0-9]*(-?\d+(?:\.\d+)?)\s*d?bm", text, flags=re.IGNORECASE
        )
        if tx_match:
            try:
                return float(tx_match.group(1))
            except ValueError:
                continue
    return None


def _normalize_ont_port_display(board: object, port: object) -> str | None:
    """Build display-safe PON port text from ONT board/port values."""
    board_text = str(board or "").strip()
    port_text = str(port or "").strip()
    if not board_text or not port_text:
        return None
    if board_text == "0/0" and port_text.isdigit():
        decoded = _decode_huawei_packed_fsp(int(port_text))
        if decoded:
            return decoded
    return f"{board_text}/{port_text}"


def _pon_port_display_value(pon_port: object | None, fallback: str = "-") -> str:
    """Return a concise PON label with optional description."""
    if pon_port is None:
        return fallback
    pon_number = (
        str(getattr(pon_port, "port_number", ""))
        if getattr(pon_port, "port_number", None) is not None
        else _pon_port_table_label(getattr(pon_port, "name", None))
    )
    pon_description = str(getattr(pon_port, "notes", None) or "").strip()
    if pon_description:
        return f"{pon_number} - {pon_description}"
    return str(pon_number or getattr(pon_port, "name", None) or fallback)


def _display_ont_serial(value: object) -> str:
    """Return a user-facing ONT serial, decoding Huawei hex form when possible."""
    serial = str(value or "").strip()
    if not serial:
        return ""
    if _is_synthetic_ont_serial(serial):
        return ""
    decoded = decode_huawei_hex_serial(serial)
    return decoded or serial


def _ont_display_serial(ont: object) -> str:
    """Return the best real ONT serial for UI tables.

    Imported or legacy rows may contain synthetic serial numbers before a vendor
    serial is known. Those are useful as internal unique keys, but should not be
    shown to operators as the ONT serial.
    """
    vendor_serial = _display_ont_serial(getattr(ont, "vendor_serial_number", None))
    if vendor_serial:
        return vendor_serial

    raw_serial = str(getattr(ont, "serial_number", "") or "").strip()
    if _is_synthetic_ont_serial(raw_serial):
        return ""
    return _display_ont_serial(raw_serial) or raw_serial


def _infer_ont_vendor_from_serial(serial: object) -> str:
    text = str(serial or "").strip().upper()
    if text.startswith("HWTC"):
        return "Huawei"
    if text.startswith("ZTEG"):
        return "ZTE"
    if text.startswith("ALCL"):
        return "Nokia"
    return ""


def _ont_identity_label(ont: object, display_serial_number: str) -> str:
    vendor = str(getattr(ont, "vendor", "") or "").strip()
    model = str(getattr(ont, "model", "") or "").strip()
    inferred_vendor = _infer_ont_vendor_from_serial(display_serial_number)
    parts = [vendor or inferred_vendor, model]
    label = " ".join(part for part in parts if part).strip()
    return label or display_serial_number or "Unknown ONT"


def _recent_acs_inform_by_ont_id(
    db: Session, ont_ids: Sequence[object]
) -> dict[str, datetime]:
    """Return the most recent ACS inform timestamp for each ONT."""
    if not ont_ids:
        return {}
    rows = db.execute(
        select(
            Tr069CpeDevice.ont_unit_id,
            func.max(Tr069CpeDevice.last_inform_at).label("last_inform_at"),
        )
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.ont_unit_id.in_(ont_ids))
        .group_by(Tr069CpeDevice.ont_unit_id)
    ).all()
    return {
        str(ont_id): last_inform_at
        for ont_id, last_inform_at in rows
        if ont_id is not None and last_inform_at is not None
    }


def _connection_request_state_by_ont_id(
    db: Session, ont_ids: Sequence[object]
) -> dict[str, dict[str, Any]]:
    """Return connection-request availability and latest tracked result per ONT."""
    if not ont_ids:
        return {}

    link_rows = db.execute(
        select(
            Tr069CpeDevice.ont_unit_id,
            Tr069CpeDevice.connection_request_url,
            Tr069CpeDevice.updated_at,
        )
        .where(Tr069CpeDevice.is_active.is_(True))
        .where(Tr069CpeDevice.ont_unit_id.in_(ont_ids))
        .order_by(Tr069CpeDevice.updated_at.desc(), Tr069CpeDevice.created_at.desc())
    ).all()

    state_by_ont_id: dict[str, dict[str, Any]] = {}
    for ont_id, connection_request_url, updated_at in link_rows:
        if ont_id is None:
            continue
        ont_key = str(ont_id)
        if ont_key in state_by_ont_id:
            continue
        has_url = bool(str(connection_request_url or "").strip())
        state_by_ont_id[ont_key] = {
            "connection_request_status": "ready" if has_url else "unavailable",
            "connection_request_display": "Ready" if has_url else "Unavailable",
            "connection_request_class": CONNECTION_REQUEST_STATUS_CLASSES[
                "ready" if has_url else "unavailable"
            ],
            "connection_request_url": str(connection_request_url or "").strip() or None,
            "connection_request_checked_at": updated_at,
            "connection_request_message": (
                "Connection request URL is available for on-demand TR-069 wakeup."
                if has_url
                else "No ConnectionRequestURL has been reported by the device yet."
            ),
        }

    op_rows = db.scalars(
        select(NetworkOperation)
        .where(NetworkOperation.target_type == NetworkOperationTargetType.ont)
        .where(NetworkOperation.target_id.in_(ont_ids))
        .where(
            NetworkOperation.operation_type
            == NetworkOperationType.ont_send_conn_request
        )
        .order_by(NetworkOperation.created_at.desc())
    ).all()

    for op in op_rows:
        ont_key = str(op.target_id)
        state = state_by_ont_id.setdefault(
            ont_key,
            {
                "connection_request_status": "unavailable",
                "connection_request_display": "Unavailable",
                "connection_request_class": CONNECTION_REQUEST_STATUS_CLASSES[
                    "unavailable"
                ],
                "connection_request_url": None,
                "connection_request_checked_at": None,
                "connection_request_message": (
                    "No ConnectionRequestURL has been reported by the device yet."
                ),
            },
        )
        if state.get("connection_request_last_attempt_at") is not None:
            continue

        status = op.status.value if op.status else "pending"
        mapped_status = {
            NetworkOperationStatus.succeeded.value: "successful",
            NetworkOperationStatus.failed.value: "failed",
            NetworkOperationStatus.running.value: "in_progress",
            NetworkOperationStatus.pending.value: "in_progress",
            NetworkOperationStatus.waiting.value: "in_progress",
            NetworkOperationStatus.canceled.value: "ready",
        }.get(status, "ready")

        state["connection_request_status"] = mapped_status
        state["connection_request_display"] = CONNECTION_REQUEST_STATUS_DISPLAY[
            mapped_status
        ]
        state["connection_request_class"] = CONNECTION_REQUEST_STATUS_CLASSES[
            mapped_status
        ]
        state["connection_request_last_attempt_at"] = op.created_at
        state["connection_request_message"] = (
            op.error
            or state.get("connection_request_message")
            or "Connection request attempted."
        )

    return state_by_ont_id


def _pon_port_display_text(pon_port: object | None) -> str | None:
    """Return PON port label as '<last-port-segment> - <description>' when possible."""
    if not pon_port:
        return None
    from app.services.web_network_pon_interfaces import parse_pon_port_notes

    alias_text, cleaned_notes = parse_pon_port_notes(getattr(pon_port, "notes", None))
    notes_text = str(cleaned_notes or "").strip()
    name_text = str(getattr(pon_port, "name", "") or "").strip()
    if alias_text:
        if name_text:
            port_segment = name_text.split("/")[-1].strip() or name_text
            return f"{port_segment} - {alias_text}"
        return alias_text
    if notes_text:
        if name_text:
            port_segment = name_text.split("/")[-1].strip() or name_text
            return f"{port_segment} - {notes_text}"
        return notes_text
    return name_text or None


def _extract_port_index(value: object | None) -> int | None:
    """Extract final numeric PON port index from mixed strings."""
    text = str(value or "").strip()
    if not text:
        return None
    hint = _extract_pon_hint(text)
    if hint:
        token = hint.split("/")[-1]
        return int(token) if token.isdigit() else None
    if "/" in text:
        token = text.split("/")[-1].strip()
        if token.isdigit():
            return int(token)
    trailing_num = re.search(r"(\d+)\s*$", text)
    if trailing_num:
        return int(trailing_num.group(1))
    return None


def _pon_port_table_label(
    name: object | None,
    *,
    port_number: object | None = None,
    fallback_index: int | None = None,
) -> str:
    """Return compact port label for PON table (prefer numeric port index)."""
    if port_number is not None:
        return str(port_number)
    parsed = _extract_port_index(name)
    if parsed is not None:
        return str(parsed)
    if fallback_index is not None:
        return str(fallback_index)
    name_text = str(name or "").strip()
    if not name_text:
        return "N/A"
    return name_text


def _extract_pon_hint(value: str | None) -> str | None:
    """Extract canonical F/S/P-like hint from interface names."""
    if not value:
        return None
    match = re.search(r"(\d+/\d+/\d+)\s*$", str(value).strip())
    if match:
        return match.group(1)
    return None


def _ont_pon_hints(ont: object) -> set[str]:
    """Build possible F/S/P hints from ONT fields."""
    hints: set[str] = set()
    board_raw = str(getattr(ont, "board", "") or "").strip()
    port_raw = str(getattr(ont, "port", "") or "").strip()
    channel_raw = str(getattr(ont, "gpon_channel", "") or "").strip()

    if board_raw and port_raw:
        hints.add(f"{board_raw}/{port_raw}")
        if "/" in board_raw:
            hints.add(f"{board_raw}/{port_raw}")
        if board_raw.isdigit():
            # Common MA5800 layout assumption: frame/channel first (often 0)
            frame = channel_raw if channel_raw.isdigit() else "0"
            hints.add(f"{frame}/{board_raw}/{port_raw}")
    if port_raw and re.search(r"\d+/\d+/\d+", port_raw):
        hints.add(port_raw)
    if board_raw and re.search(r"\d+/\d+/\d+", board_raw):
        hints.add(board_raw)

    return {h for h in hints if h}


def olt_detail_page_data(db: Session, olt_id: str) -> dict[str, object] | None:
    """Return OLT detail payload with PON ports, ONT assignments, and signal data."""
    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except HTTPException:
        return None

    pon_ports = network_service.pon_ports.list(
        db=db,
        olt_id=olt_id,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    pon_port_aliases = {
        str(port.id): (parse_pon_port_notes(getattr(port, "notes", None))[0] or "")
        for port in pon_ports
    }

    # Gather ONT assignments and build per-port stats
    from app.services.network.signal_thresholds import (
        classify_signal,
        get_signal_thresholds,
    )

    warn, crit = get_signal_thresholds(db)
    ont_assignments = []
    assignment_by_ont_id: dict[str, object] = {}
    port_stats: dict[str, dict[str, int]] = {}

    from app.models.network import OntAssignment

    pon_port_ids = [port.id for port in pon_ports]
    assignments_by_port_id: dict[str, list[object]] = {
        str(port.id): [] for port in pon_ports
    }
    if pon_port_ids:
        port_assignments = list(
            db.scalars(
                select(OntAssignment)
                .where(OntAssignment.pon_port_id.in_(pon_port_ids))
                .where(OntAssignment.active.is_(True))
                .options(
                    joinedload(OntAssignment.ont_unit),
                    joinedload(OntAssignment.pon_port),
                )
                .order_by(OntAssignment.created_at.desc())
            ).all()
        )
        for assignment in port_assignments:
            if assignment.pon_port_id is not None:
                assignments_by_port_id.setdefault(
                    str(assignment.pon_port_id), []
                ).append(assignment)

    for port_idx, port in enumerate(pon_ports):
        active_assignments = assignments_by_port_id.get(str(port.id), [])
        ont_assignments.extend(active_assignments)
        for a in active_assignments:
            if getattr(a, "ont_unit_id", None):
                assignment_by_ont_id[str(a.ont_unit_id)] = a

        # Per-port ONU summary
        p_online = 0
        p_offline = 0
        p_low_signal = 0
        p_signal_total = 0.0
        p_signal_count = 0
        for a in active_assignments:
            ont = a.ont_unit
            if not ont:
                continue
            s = resolve_effective_ont_status(ont).status.value
            if s == "online":
                p_online += 1
            elif s == "offline":
                p_offline += 1
            quality = classify_signal(
                getattr(ont, "olt_rx_signal_dbm", None),
                warn_threshold=warn,
                crit_threshold=crit,
            )
            olt_rx_val = getattr(ont, "olt_rx_signal_dbm", None)
            if olt_rx_val is not None:
                try:
                    p_signal_total += float(olt_rx_val)
                    p_signal_count += 1
                except Exception:
                    logger.debug(
                        "Could not parse OLT RX signal value for ONT %s: %r",
                        getattr(ont, "id", None),
                        olt_rx_val,
                        exc_info=True,
                    )
            if quality in ("warning", "critical"):
                p_low_signal += 1
        avg_signal = (p_signal_total / p_signal_count) if p_signal_count > 0 else None
        online_pct = (
            int(round((p_online / len(active_assignments)) * 100))
            if active_assignments
            else 0
        )
        port_stats[str(port.id)] = {
            "total": len(active_assignments),
            "online": p_online,
            "offline": p_offline,
            "low_signal": p_low_signal,
            "avg_olt_rx_dbm": avg_signal,
            "online_pct": online_pct,
        }

    # Include ONTs directly linked to OLT even when not assigned to a PON port.
    from app.models.network import OntUnit

    direct_onts = list(
        db.scalars(
            select(OntUnit)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
            .order_by(OntUnit.serial_number.asc())
        ).all()
    )
    onts_by_id = {str(ont.id): ont for ont in direct_onts if getattr(ont, "id", None)}
    for a in ont_assignments:
        ont = getattr(a, "ont_unit", None)
        if ont and getattr(ont, "id", None):
            onts_by_id[str(ont.id)] = ont
    onts_on_olt = list(onts_by_id.values())

    # Build signal data for each ONT
    signal_data: dict[str, dict[str, object]] = {}
    pon_port_display_by_ont_id: dict[str, str] = {}
    ont_mac_by_ont_id: dict[str, str] = {}
    # Get subscriber IDs from assignments to find related CPE devices
    # Note: Devices link to subscribers, not subscriptions (for independent OLT management)
    assignment_subscriber_ids = {
        getattr(a, "subscriber_id", None)
        for a in ont_assignments
        if getattr(a, "subscriber_id", None) is not None
    }
    cpe_mac_by_subscriber_id: dict[object, str] = {}
    if assignment_subscriber_ids:
        cpes_by_subscriber = list(
            db.scalars(
                select(CPEDevice)
                .where(CPEDevice.subscriber_id.in_(assignment_subscriber_ids))
                .order_by(CPEDevice.updated_at.desc(), CPEDevice.created_at.desc())
            ).all()
        )
        for cpe in cpes_by_subscriber:
            subscriber_id = getattr(cpe, "subscriber_id", None)
            mac = str(getattr(cpe, "mac_address", "") or "").strip()
            if subscriber_id is None or not mac:
                continue
            if subscriber_id not in cpe_mac_by_subscriber_id:
                cpe_mac_by_subscriber_id[subscriber_id] = mac
    total_online = 0
    total_offline = 0
    total_low_signal = 0
    for ont in onts_on_olt:
        ont_id = str(ont.id)
        ont_mac = str(getattr(ont, "mac_address", "") or "").strip()
        if ont_mac:
            ont_mac_by_ont_id[ont_id] = ont_mac
        assignment = assignment_by_ont_id.get(ont_id)
        if assignment:
            subscriber_id = getattr(assignment, "subscriber_id", None)
            cpe_mac = None
            if subscriber_id is not None:
                cpe_mac = cpe_mac_by_subscriber_id.get(subscriber_id)
            if cpe_mac:
                ont_mac_by_ont_id[ont_id] = cpe_mac
        if assignment and getattr(assignment, "pon_port", None):
            port_display = _pon_port_display_text(assignment.pon_port)
            if port_display:
                pon_port_display_by_ont_id[ont_id] = port_display
        else:
            normalized_port = _normalize_ont_port_display(
                getattr(ont, "board", None),
                getattr(ont, "port", None),
            )
            if normalized_port:
                pon_port_display_by_ont_id[ont_id] = normalized_port

        olt_rx = getattr(ont, "olt_rx_signal_dbm", None)
        onu_rx = getattr(ont, "onu_rx_signal_dbm", None)
        quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
        effective_status = resolve_effective_ont_status(ont)
        from app.services.device_operational_status import (
            derive_ont_operational_status,
        )

        ont_op = derive_ont_operational_status(ont)
        s = effective_status.status.value
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        distance_meters = getattr(ont, "distance_meters", None)
        # Suppress stale/offline sentinel distance values.
        if s == "offline" and distance_meters is not None and distance_meters <= 1:
            distance_meters = None
        signal_data[ont_id] = {
            "olt_rx_dbm": olt_rx,
            "onu_rx_dbm": onu_rx,
            "quality": quality,
            "quality_class": SIGNAL_QUALITY_CLASSES.get(
                quality, SIGNAL_QUALITY_CLASSES["unknown"]
            ),
            "status": s,
            "status_class": ONLINE_STATUS_CLASSES.get(
                s, ONLINE_STATUS_CLASSES["unknown"]
            ),
            "operational": ont_op.status,
            "operational_reason": ont_op.reason,
            "operational_reason_display": ont_op.reason_label,
            "status_presentation": ont_op.presentation,
            "reason_display": ont_op.reason_label,
            "distance_meters": distance_meters,
            "signal_updated_at": getattr(ont, "signal_updated_at", None),
        }
        if s == "online":
            total_online += 1
        elif s == "offline":
            total_offline += 1
        if quality in ("warning", "critical"):
            total_low_signal += 1

    port_stats = {}
    for port in pon_ports:
        active_assignments = assignments_by_port_id.get(str(port.id), [])
        p_online = 0
        p_offline = 0
        p_low_signal = 0
        p_signal_total = 0.0
        p_signal_count = 0
        for assignment in active_assignments:
            ont = assignment.ont_unit
            if not ont:
                continue
            status = resolve_effective_ont_status(ont).status.value
            if status == "online":
                p_online += 1
            else:
                p_offline += 1
            olt_rx_val = getattr(ont, "olt_rx_signal_dbm", None)
            quality = classify_signal(
                olt_rx_val,
                warn_threshold=warn,
                crit_threshold=crit,
            )
            if olt_rx_val is not None:
                p_signal_total += float(olt_rx_val)
                p_signal_count += 1
            if quality in ("warning", "critical"):
                p_low_signal += 1
        avg_signal = (p_signal_total / p_signal_count) if p_signal_count > 0 else None
        total = len(active_assignments)
        port_stats[str(port.id)] = {
            "total": total,
            "online": p_online,
            "offline": p_offline,
            "low_signal": p_low_signal,
            "avg_olt_rx_dbm": avg_signal,
            "online_pct": int(round((p_online / total) * 100)) if total else 0,
        }

    # Load shelf/card/port hierarchy via ORM relationships
    from app.models.network import OltShelf

    shelves = list(
        db.scalars(
            select(OltShelf)
            .where(OltShelf.olt_id == olt.id)
            .order_by(OltShelf.shelf_number)
        ).all()
    )

    ont_summary = {
        "total": len(onts_on_olt),
        "online": total_online,
        "offline": total_offline,
        "low_signal": total_low_signal,
    }

    # Keep detail rendering DB-only. Live health reads belong in explicit
    # monitoring/operations panels so page load does not depend on device latency.
    olt_health = {"has_data": False}

    # Fetch recent config backups
    from app.models.network import OltConfigBackup

    # SNMP settings are stored on the linked core monitoring device record.
    from app.services.network.olt_monitoring_devices import (
        resolve_linked_network_device,
    )

    monitoring_resolution = resolve_linked_network_device(db, olt)
    monitoring_device = monitoring_resolution.device

    monitoring_data: dict[str, object] | None = None
    live_board_inventory: list[dict[str, object]] = []
    resolved_device_info = {
        "hostname": monitoring_device.hostname
        if monitoring_device and monitoring_device.hostname
        else olt.hostname,
        "mgmt_ip": monitoring_device.mgmt_ip
        if monitoring_device and monitoring_device.mgmt_ip
        else olt.mgmt_ip,
        "vendor": monitoring_device.vendor
        if monitoring_device and monitoring_device.vendor
        else olt.vendor,
        "model": monitoring_device.model
        if monitoring_device and monitoring_device.model
        else olt.model,
        "serial_number": monitoring_device.serial_number
        if monitoring_device and monitoring_device.serial_number
        else olt.serial_number,
        "firmware_version": olt.firmware_version,
        "software_version": olt.software_version,
        "supported_pon_types": getattr(olt, "supported_pon_types", None),
        "status": monitoring_device.status.value
        if monitoring_device and monitoring_device.status
        else ("active" if olt.is_active else "inactive"),
        "last_ping_at": monitoring_device.last_ping_at if monitoring_device else None,
        "last_snmp_at": monitoring_device.last_snmp_at if monitoring_device else None,
        "last_ping_ok": monitoring_device.last_ping_ok if monitoring_device else None,
        "last_snmp_ok": monitoring_device.last_snmp_ok if monitoring_device else None,
        "ping_enabled": bool(monitoring_device.ping_enabled)
        if monitoring_device
        else False,
        "snmp_enabled": bool(monitoring_device.snmp_enabled)
        if monitoring_device
        else False,
    }

    if monitoring_device is not None:
        interfaces = list(
            db.scalars(
                select(DeviceInterface)
                .where(DeviceInterface.device_id == monitoring_device.id)
                .order_by(DeviceInterface.name.asc())
            ).all()
        )

        pon_interfaces: list[dict[str, object]] = []
        monitoring_interfaces: list[dict[str, object]] = []
        for iface in interfaces:
            item = {
                "id": str(iface.id),
                "name": iface.name,
                "description": iface.description,
                "status": iface.status.value if iface.status else "unknown",
                "speed_mbps": iface.speed_mbps,
                "mac_address": iface.mac_address,
                "updated_at": iface.updated_at,
            }
            monitoring_interfaces.append(item)
            text = f"{iface.name or ''} {iface.description or ''}".lower()
            if any(token in text for token in ("pon", "gpon", "epon", "xgpon", "xgs")):
                pon_interfaces.append(item)

        monitoring_data = {
            "interfaces": monitoring_interfaces,
            "pon_interfaces": pon_interfaces,
            "snmp_system": None,
        }

    live_board_inventory = _dedupe_live_board_inventory(live_board_inventory)
    live_board_inventory.sort(
        key=lambda item: (
            0 if item.get("category") == "card" else 1,
            item.get("slot_number") is None,
            item.get("slot_number") if item.get("slot_number") is not None else 10**9,
            str(item.get("card_type") or "").lower(),
        )
    )
    live_board_cards = [
        item for item in live_board_inventory if item.get("category") == "card"
    ]
    live_board_others: list[dict[str, object]] = []

    db_pon_count = len(pon_ports)
    snmp_pon_count = (
        len(monitoring_data.get("pon_interfaces", []))
        if isinstance(monitoring_data, dict)
        else 0
    )
    resolved_pon_ports_count = db_pon_count if db_pon_count > 0 else snmp_pon_count

    pon_port_table_rows: list[dict[str, object]] = []
    pon_snmp_by_norm_name: dict[str, dict[str, object]] = {}
    for iface in (
        monitoring_data.get("pon_interfaces", [])
        if isinstance(monitoring_data, dict)
        else []
    ):
        norm = _normalize_port_name(str(iface.get("name") or ""))
        if norm and norm not in pon_snmp_by_norm_name:
            pon_snmp_by_norm_name[norm] = iface

    ont_count_by_port_index: dict[int, int] = {}
    ont_signal_sum_by_port_index: dict[int, float] = {}
    ont_signal_count_by_port_index: dict[int, int] = {}
    for ont in onts_on_olt:
        ont_port_index = _extract_port_index(
            _normalize_ont_port_display(
                getattr(ont, "board", None),
                getattr(ont, "port", None),
            )
        )
        if ont_port_index is None:
            continue
        ont_count_by_port_index[ont_port_index] = (
            ont_count_by_port_index.get(ont_port_index, 0) + 1
        )
        ont_signal = signal_data.get(str(getattr(ont, "id", "")), {}).get("olt_rx_dbm")
        # Use the persisted ``olt_rx_signal_dbm`` column (written by the
        # native OLT poller). Keeps the PON-port table populated.
        if ont_signal is None:
            ont_signal = getattr(ont, "olt_rx_signal_dbm", None)
        if ont_signal is None:
            continue
        try:
            ont_signal_float = float(ont_signal)
        except (TypeError, ValueError):
            continue
        ont_signal_sum_by_port_index[ont_port_index] = (
            ont_signal_sum_by_port_index.get(ont_port_index, 0.0) + ont_signal_float
        )
        ont_signal_count_by_port_index[ont_port_index] = (
            ont_signal_count_by_port_index.get(ont_port_index, 0) + 1
        )

    for port_idx, port in enumerate(pon_ports):
        ps = port_stats.get(str(port.id), {})
        iface = pon_snmp_by_norm_name.get(
            _normalize_port_name(getattr(port, "name", None))
        )
        row_port_index = _extract_port_index(getattr(port, "port_number", None))
        if row_port_index is None:
            row_port_index = _extract_port_index(getattr(port, "name", None))
        assigned_total = int(ps.get("total", 0) or 0)
        fallback_total = (
            ont_count_by_port_index.get(row_port_index, 0)
            if row_port_index is not None
            else 0
        )
        resolved_total = assigned_total if assigned_total > 0 else fallback_total
        card_port = getattr(port, "olt_card_port", None)
        sfp_modules = (
            list(getattr(card_port, "sfp_modules", []) or []) if card_port else []
        )
        active_sfps = [m for m in sfp_modules if getattr(m, "is_active", True)]
        tx_power_dbm = None
        for sfp in active_sfps:
            if getattr(sfp, "tx_power_dbm", None) is not None:
                tx_power_dbm = sfp.tx_power_dbm
                break
        if tx_power_dbm is None:
            for sfp in sfp_modules:
                if getattr(sfp, "tx_power_dbm", None) is not None:
                    tx_power_dbm = sfp.tx_power_dbm
                    break
        if tx_power_dbm is None:
            tx_power_dbm = _extract_tx_power_dbm(
                iface.get("description") if isinstance(iface, dict) else None,
                getattr(port, "notes", None),
                getattr(card_port, "name", None),
            )

        range_display = _extract_range_display(
            getattr(port, "notes", None),
            iface.get("description") if isinstance(iface, dict) else None,
            getattr(card_port, "name", None),
        )
        description = getattr(port, "notes", None) or (
            str(iface.get("description") or "").strip()
            if isinstance(iface, dict)
            else None
        )

        status_val = (
            str((iface or {}).get("status") or "").lower()
            if isinstance(iface, dict)
            else ""
        )
        if status_val not in {"up", "down"}:
            if int(ps.get("online", 0) or 0) > 0:
                status_val = "up"
            elif int(ps.get("total", 0) or 0) > 0:
                status_val = "down"
            else:
                status_val = "down"

        port_type = "PON"
        if card_port and getattr(card_port, "port_type", None):
            raw_type = getattr(card_port.port_type, "value", card_port.port_type)
            port_type = str(raw_type or "pon").replace("_", "-").upper()

        avg_signal_dbm = ps.get("avg_olt_rx_dbm")
        if avg_signal_dbm is None and row_port_index is not None:
            signal_count = ont_signal_count_by_port_index.get(row_port_index, 0)
            if signal_count > 0:
                avg_signal_dbm = (
                    ont_signal_sum_by_port_index[row_port_index] / signal_count
                )

        pon_port_table_rows.append(
            {
                "name": _pon_port_table_label(
                    getattr(port, "name", None),
                    port_number=getattr(port, "port_number", None),
                    fallback_index=port_idx,
                ),
                "type": port_type,
                "admin_state": (
                    "Enabled" if getattr(port, "admin_enabled", True) else "Disabled"
                ),
                "status": status_val,
                "onus": resolved_total,
                "avg_signal_dbm": avg_signal_dbm,
                "description": description,
                "range_display": range_display,
                "tx_power_dbm": tx_power_dbm,
                "action_url": f"/admin/network/onts?olt_id={olt.id}&pon_port_id={port.id}",
            }
        )

    if not pon_port_table_rows and isinstance(monitoring_data, dict):
        ont_candidates = list(onts_on_olt)

        for iface_idx, iface in enumerate(monitoring_data.get("pon_interfaces", [])):
            description = str(iface.get("description") or "").strip() or None
            name_text = f"{iface.get('name') or ''} {description or ''}".lower()
            pon_hint = _extract_pon_hint(str(iface.get("name") or ""))

            matched_onts: list[Any] = []
            if pon_hint:
                matched_onts = [
                    ont for ont in ont_candidates if pon_hint in _ont_pon_hints(ont)
                ]

            signal_values = []
            for ont in matched_onts:
                signal_value = signal_data.get(str(getattr(ont, "id", "")), {}).get(
                    "olt_rx_dbm"
                )
                # Same persisted-column read as the primary aggregator
                # above (written by the native OLT poller).
                if signal_value is None:
                    signal_value = getattr(ont, "olt_rx_signal_dbm", None)
                if signal_value is not None:
                    signal_values.append(float(signal_value))
            avg_signal_dbm = (
                sum(signal_values) / len(signal_values) if signal_values else None
            )

            distance_values = [
                int(ont.distance_meters)
                for ont in matched_onts
                if getattr(ont, "distance_meters", None) is not None
            ]
            range_display = _extract_range_display(description)
            if not range_display and distance_values:
                range_display = f"{(max(distance_values) / 1000):.1f} km"

            tx_power_dbm = _extract_tx_power_dbm(
                description,
                str(iface.get("name") or ""),
            )

            if "xgs" in name_text:
                port_type = "XGS-PON"
            elif "xgpon" in name_text or "xg-pon" in name_text:
                port_type = "XG-PON"
            elif "epon" in name_text:
                port_type = "EPON"
            else:
                port_type = "GPON/PON"
            pon_port_table_rows.append(
                {
                    "name": _pon_port_table_label(
                        iface.get("name"),
                        fallback_index=iface_idx,
                    ),
                    "type": port_type,
                    "admin_state": "N/A",
                    "status": str(iface.get("status") or "down").lower(),
                    "onus": len(matched_onts),
                    "avg_signal_dbm": avg_signal_dbm,
                    "description": description,
                    "range_display": range_display,
                    "tx_power_dbm": tx_power_dbm,
                    "action_url": (
                        f"/admin/network/onts?olt_id={olt.id}&pon_hint="
                        f"{(pon_hint or str(iface.get('name') or ''))}"
                    ),
                }
            )

    # ONT display: when port descriptions exist, render as "<index> - <description>".
    desc_by_index: dict[int, str] = {}
    for idx, row in enumerate(pon_port_table_rows):
        desc = str((row or {}).get("description") or "").strip()
        if desc:
            desc_by_index[idx] = desc
    if desc_by_index:
        for ont in onts_on_olt:
            ont_id = str(getattr(ont, "id", ""))
            if not ont_id:
                continue
            port_index = _extract_port_index(
                _normalize_ont_port_display(
                    getattr(ont, "board", None),
                    getattr(ont, "port", None),
                )
            )
            if port_index is None:
                continue
            desc = desc_by_index.get(port_index)
            if desc:
                pon_port_display_by_ont_id[ont_id] = f"{port_index} - {desc}"

    config_backups = (
        db.query(OltConfigBackup)
        .filter(OltConfigBackup.olt_device_id == olt.id)
        .order_by(OltConfigBackup.created_at.desc())
        .limit(10)
        .all()
    )

    from app.services.ipam_adapter import ipam_adapter

    ipam_scope = ipam_adapter.olt_scope_context(db, olt=olt)

    return {
        "olt": olt,
        "pon_ports": pon_ports,
        "pon_port_aliases": pon_port_aliases,
        "ont_assignments": ont_assignments,
        "assignment_by_ont_id": assignment_by_ont_id,
        "pon_port_display_by_ont_id": pon_port_display_by_ont_id,
        "ont_mac_by_ont_id": ont_mac_by_ont_id,
        "onts_on_olt": onts_on_olt,
        "signal_data": signal_data,
        "port_stats": port_stats,
        "ont_summary": ont_summary,
        "shelves": shelves,
        "warn_threshold": warn,
        "crit_threshold": crit,
        "olt_health": olt_health,
        "monitoring_device": monitoring_device,
        "monitoring_resolution": {
            "match_strategy": monitoring_resolution.match_strategy,
            "authoritative": monitoring_resolution.authoritative,
            "warning": monitoring_resolution.warning,
        },
        "monitoring_data": monitoring_data,
        "resolved_device_info": resolved_device_info,
        "resolved_pon_ports_count": resolved_pon_ports_count,
        "live_board_inventory": live_board_inventory,
        "live_board_cards": live_board_cards,
        "live_board_others": live_board_others,
        "pon_port_table_rows": pon_port_table_rows,
        "config_backups": config_backups,
        **ipam_scope,
    }


def _classify_ont_signal(ont: object, warn: float, crit: float) -> str:
    """Classify ONT signal quality for template display."""
    from app.services.network.olt_polling import classify_signal

    dbm = getattr(ont, "olt_rx_signal_dbm", None)
    return classify_signal(dbm, warn_threshold=warn, crit_threshold=crit)


SIGNAL_QUALITY_CLASSES: dict[str, str] = {
    "good": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "warning": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "critical": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

ONLINE_STATUS_CLASSES: dict[str, str] = {
    "online": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "offline": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

ACS_STATUS_CLASSES: dict[str, str] = {
    "online": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "stale": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "unmanaged": "bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

CONNECTION_REQUEST_STATUS_CLASSES: dict[str, str] = {
    "successful": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "ready": "bg-blue-100 text-blue-800 dark:bg-blue-900/60 dark:text-blue-200",
    "in_progress": "bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200",
    "failed": "bg-rose-100 text-rose-800 dark:bg-rose-900/60 dark:text-rose-200",
    "unavailable": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

CONNECTION_REQUEST_STATUS_DISPLAY: dict[str, str] = {
    "successful": "Successful",
    "ready": "Ready",
    "in_progress": "In Progress",
    "failed": "Failed",
    "unavailable": "Unavailable",
}

SUBSCRIBER_STATUS_CLASSES: dict[str, str] = {
    "active": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "new": "bg-sky-100 text-sky-800 dark:bg-sky-900 dark:text-sky-200",
    "suspended": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "delinquent": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "disabled": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
    "canceled": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "unknown": "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
}

OFFLINE_REASON_DISPLAY: dict[str, str] = {
    "power_fail": "Power Fail",
    "los": "Loss of Signal",
    "dying_gasp": "Dying Gasp",
    "unknown": "Unknown",
}


def _is_synthetic_ont_serial(value: object | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(
        re.match(
            r"^(HW|ZT|NK|OLT)-[A-F0-9]{8}-[A-Z0-9]+(?:-\d{10,20})?$",
            text,
            re.IGNORECASE,
        )
    )


def onts_list_page_data(
    db: Session,
    *,
    view: str = "list",
    status: str | None = None,
    olt_id: str | None = None,
    pon_port_id: str | None = None,
    pon_hint: str | None = None,
    zone_id: str | None = None,
    operational_status: str | None = None,
    authorization: str | None = None,
    offline_reason: str | None = None,
    signal_quality: str | None = None,
    search: str | None = None,
    vendor: str | None = None,
    pppoe_health: str | None = None,
    order_by: str = "serial_number",
    order_dir: str = "asc",
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return ONT/CPE list payload with advanced filtering and signal classification."""
    from app.models.network import OLTDevice, OntUnit, OnuOnlineStatus
    from app.services.network.signal_thresholds import (
        classify_signal,
        get_signal_thresholds,
    )

    normalized_view = (
        "diagnostics"
        if view == "diagnostics"
        else "unconfigured"
        if view == "unconfigured"
        else "list"
    )
    per_page = min(max(int(per_page or 50), 10), 500)

    # Determine is_active from status filter
    status_filter = (status or "all").strip().lower()
    is_active: bool | None = None
    if status_filter == "active":
        is_active = True
    elif status_filter == "inactive":
        is_active = False

    authorization_filter = (authorization or "all").strip().lower()
    if authorization_filter not in {"authorized", "unauthorized", "all"}:
        authorization_filter = "all"
    query_authorization = (
        None if authorization_filter == "all" else authorization_filter
    )

    requested_page = max(page, 1)
    current_page = requested_page
    onts: Sequence[OntUnit] = ()
    total_filtered = 0
    diagnostics_onts: Sequence[OntUnit] = ()
    diagnostics_total = 0

    if normalized_view != "unconfigured":
        query_order_by = "signal" if normalized_view == "diagnostics" else order_by
        query_order_dir = "asc" if normalized_view == "diagnostics" else order_dir

        def _load_page(page_number: int) -> tuple[Sequence[OntUnit], int]:
            return network_service.ont_units.list_advanced(
                db,
                olt_id=olt_id,
                pon_port_id=pon_port_id,
                pon_hint=pon_hint,
                zone_id=zone_id,
                signal_quality=signal_quality,
                operational_status=operational_status,
                authorization_status=query_authorization,
                vendor=vendor,
                search=search,
                is_active=is_active,
                order_by=query_order_by,
                order_dir=query_order_dir,
                limit=per_page,
                offset=(page_number - 1) * per_page,
            )

        page_rows, page_total = _load_page(requested_page)
        page_total_pages = max(1, (page_total + per_page - 1) // per_page)
        current_page = min(requested_page, page_total_pages)
        if current_page != requested_page:
            page_rows, page_total = _load_page(current_page)

        if normalized_view == "diagnostics":
            diagnostics_onts, diagnostics_total = page_rows, page_total
        else:
            onts, total_filtered = page_rows, page_total

    if normalized_view == "diagnostics":
        diagnostics_total_pages = max(1, (diagnostics_total + per_page - 1) // per_page)
        total_pages = 1
    else:
        total_pages = max(1, (total_filtered + per_page - 1) // per_page)
        diagnostics_total_pages = 1

    # Signal threshold classification for displayed ONTs
    warn, crit = get_signal_thresholds(db)
    signal_data: dict[str, dict[str, object]] = {}
    displayed_onts = list(onts) + [
        item for item in diagnostics_onts if item not in onts
    ]
    connection_request_by_ont_id = _connection_request_state_by_ont_id(
        db, [ont.id for ont in displayed_onts if getattr(ont, "id", None)]
    )
    # The cached live-snapshot source (Zabbix) was retired with the native
    # monitoring cutover; render from the persisted inventory columns (written
    # by the native OLT poller), exactly as the permanently-cold-cache path
    # already did.
    for ont in displayed_onts:
        olt_rx_dbm = getattr(ont, "olt_rx_signal_dbm", None)
        onu_rx_dbm = getattr(ont, "onu_rx_signal_dbm", None)
        quality = classify_signal(
            olt_rx_dbm,
            warn_threshold=warn,
            crit_threshold=crit,
        )
        connection_request_info = connection_request_by_ont_id.get(str(ont.id), {})
        raw_status = getattr(ont, "olt_status", None)
        if isinstance(raw_status, OnuOnlineStatus):
            status_val = raw_status.value
        elif raw_status:
            status_val = str(raw_status)
        else:
            status_val = "offline"
        if status_val not in ONLINE_STATUS_CLASSES:
            status_val = "unknown"
        effective_status = resolve_effective_ont_status(ont)
        status_display_val = effective_status.status.value
        status_source = effective_status.source.value
        effective_last_seen_at = (
            getattr(ont, "last_seen_at", None)
            or getattr(ont, "olt_status_seen_at", None)
            or getattr(ont, "signal_updated_at", None)
        )
        reason = getattr(ont, "offline_reason", None)
        reason_val = reason.value if reason else None
        from app.services.device_operational_status import (
            derive_ont_operational_status,
        )

        ont_op = derive_ont_operational_status(ont)
        signal_data[str(ont.id)] = {
            "operational": ont_op.status,
            "operational_reason": ont_op.reason,
            "operational_reason_display": ont_op.reason_label,
            "status_presentation": ont_op.presentation,
            "olt_rx_dbm": olt_rx_dbm,
            "onu_rx_dbm": onu_rx_dbm,
            "signal_updated_at": getattr(ont, "signal_updated_at", None),
            "quality": quality,
            "quality_class": SIGNAL_QUALITY_CLASSES.get(
                quality, SIGNAL_QUALITY_CLASSES["unknown"]
            ),
            "status_class": ONLINE_STATUS_CLASSES.get(
                status_display_val,
                ONLINE_STATUS_CLASSES["offline"],
            ),
            "status_display": (
                status_display_val.replace("_", " ").title()
                if status_display_val
                else ""
            ),
            "status_source": status_source,
            "olt_status": status_val,
            "olt_status_display": (
                status_val.replace("_", " ").title() if status_val else ""
            ),
            "olt_status_class": ONLINE_STATUS_CLASSES.get(
                status_val,
                ONLINE_STATUS_CLASSES["offline"],
            ),
            "status_source_display": status_source.replace("_", " ").title(),
            "status_source_class": ACS_STATUS_CLASSES.get(
                status_source, ACS_STATUS_CLASSES["unknown"]
            ),
            "acs_last_inform_at": getattr(ont, "acs_last_inform_at", None),
            "last_seen_at": effective_last_seen_at,
            "connection_request_status": connection_request_info.get(
                "connection_request_status", "unavailable"
            ),
            "connection_request_display": connection_request_info.get(
                "connection_request_display", "Unavailable"
            ),
            "connection_request_class": connection_request_info.get(
                "connection_request_class",
                CONNECTION_REQUEST_STATUS_CLASSES["unavailable"],
            ),
            "reason_display": ont_op.reason_label,
        }

    try:
        from app.services.network.ont_status_refresh import (
            request_stale_ont_status_refreshes,
        )

        status_refresh_admission = request_stale_ont_status_refreshes(
            db, displayed_onts
        )
    except Exception:
        logger.warning("Failed to request stale ONT status refresh", exc_info=True)
        status_refresh_admission = None

    # Summary counts use the binary owner outcome; verification remains out of band.
    total_cpes_count = db.scalar(select(func.count()).select_from(CPEDevice)) or 0
    stats_filters = [OntUnit.is_active.is_(True)]
    if olt_id:
        stats_filters.append(OntUnit.olt_device_id == olt_id)

    from app.services.network.ont_status import operational_ont_working_clause

    stats_row = db.execute(
        select(
            func.count().label("total"),
            func.count().filter(operational_ont_working_clause()).label("working"),
            func.count()
            .filter(
                OntUnit.olt_rx_signal_dbm.isnot(None),
                OntUnit.olt_rx_signal_dbm < warn,
            )
            .label("low_signal"),
        )
        .select_from(OntUnit)
        .where(*stats_filters)
    ).one()
    all_onts_count = int(stats_row.total or 0)
    working_count = int(stats_row.working or 0)
    low_signal_count = int(stats_row.low_signal or 0)
    not_working_count = max(all_onts_count - working_count, 0)

    stats = {
        "total_onts": all_onts_count,
        "total_cpes": total_cpes_count,
        "total": all_onts_count + total_cpes_count,
        "working_count": working_count,
        "not_working_count": not_working_count,
        "low_signal_count": low_signal_count,
        "source": "inventory",
    }

    # OLT list for filter dropdown
    olts = list(
        db.scalars(
            select(OLTDevice)
            .where(OLTDevice.is_active.is_(True))
            .order_by(OLTDevice.name)
        ).all()
    )

    # Zone list for filter dropdown
    from app.models.network import NetworkZone

    zones = list(
        db.scalars(
            select(NetworkZone)
            .where(NetworkZone.is_active.is_(True))
            .order_by(NetworkZone.name)
        ).all()
    )

    # Build active assignment lookup for OLT/PON display
    from app.models.network import OntAssignment, PonPort

    ont_ids = list({ont.id for ont in list(onts) + list(diagnostics_onts)})
    assignment_info: dict[str, dict[str, str]] = {}
    serial_display_by_ont_id: dict[str, str] = {}
    hex_serial_by_ont_id: dict[str, str] = {}
    displayed_ont_by_id: dict[str, object] = {}
    serial_keys_by_ont_id: dict[str, str] = {}
    serial_variants: set[str] = set()
    for ont in list(onts) + [item for item in diagnostics_onts if item not in onts]:
        ont_key = str(ont.id)
        displayed_ont_by_id[ont_key] = ont
        display_serial = _ont_display_serial(ont)
        if display_serial:
            serial_display_by_ont_id[ont_key] = display_serial
            serial_keys_by_ont_id[ont_key] = display_serial.upper()
            serial_variants.add(display_serial.upper())
            # Compute hex serial for display from the real serial, not a synthetic key.
            hex_serial = encode_to_hex_serial(display_serial)
            if hex_serial:
                hex_serial_by_ont_id[ont_key] = hex_serial
                serial_variants.add(hex_serial.upper())
            raw_serial = str(getattr(ont, "serial_number", "") or "").strip()
            if raw_serial:
                serial_variants.add(raw_serial.upper())
            continue
        mac_value = str(getattr(ont, "mac_address", "") or "").strip()
        if mac_value:
            serial_display_by_ont_id[ont_key] = mac_value
        else:
            serial_display_by_ont_id[ont_key] = "-"
    if ont_ids:

        def assignment_table_info(assignment: object) -> dict[str, str]:
            pon_port = getattr(assignment, "pon_port", None)
            olt = pon_port.olt if pon_port else None
            subscriber = getattr(assignment, "subscriber", None)
            return {
                "olt_name": getattr(olt, "name", None),
                "olt_id": str(olt.id) if olt else "",
                "pon_port_name": getattr(pon_port, "name", None),
                "pon_port_display": _pon_port_display_value(pon_port),
                "subscriber_name": _subscriber_display_name(subscriber)
                if subscriber
                else "",
                "subscriber_customer_url": (
                    f"/admin/customers/business/{subscriber.id}"
                    if subscriber
                    and getattr(subscriber, "category", None)
                    == SubscriberCategory.business
                    else f"/admin/customers/person/{subscriber.id}"
                    if subscriber
                    else ""
                ),
            }

        assignment_filters = [OntAssignment.ont_unit_id.in_(ont_ids)]
        if serial_variants:
            assignment_filters.append(
                func.upper(OntUnit.serial_number).in_(serial_variants)
            )
        # Skip the wide JSON/TEXT columns on the joined ont_units rows — the
        # assignment row builder only needs the serial fields, and pulling
        # the full ONT row (5KB avg) for every joined assignment dominates
        # the page load time.
        assign_rows = db.scalars(
            select(OntAssignment)
            .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
            .options(
                joinedload(OntAssignment.ont_unit).load_only(
                    OntUnit.id,
                    OntUnit.serial_number,
                    OntUnit.vendor_serial_number,
                ),
                joinedload(OntAssignment.subscriber),
                joinedload(OntAssignment.pon_port)
                .load_only(
                    PonPort.id,
                    PonPort.name,
                    PonPort.olt_id,
                    PonPort.port_number,
                    PonPort.notes,
                )
                .joinedload(PonPort.olt)
                .load_only(OLTDevice.id, OLTDevice.name),
            )
            .where(OntAssignment.active.is_(True))
            .where(or_(*assignment_filters))
        ).all()
        assignment_info_by_serial: dict[str, dict[str, str]] = {}
        for assignment in assign_rows:
            info = assignment_table_info(assignment)
            assigned_ont_key = str(assignment.ont_unit_id)
            if assigned_ont_key in displayed_ont_by_id:
                assignment_info[assigned_ont_key] = info
            assigned_ont = getattr(assignment, "ont_unit", None)
            assigned_serial_key = _ont_display_serial(assigned_ont).upper()
            if assigned_serial_key:
                assignment_info_by_serial[assigned_serial_key] = info

        for ont_key, serial_key in serial_keys_by_ont_id.items():
            if (
                ont_key not in assignment_info
                and serial_key in assignment_info_by_serial
            ):
                assignment_info[ont_key] = {
                    **assignment_info_by_serial[serial_key],
                    "assignment_source": "normalized_serial",
                }

        direct_pon_names = {
            fsp
            for ont in displayed_onts
            if (fsp := _normalize_ont_port_display(ont.board, ont.port))
        }
        direct_olt_ids = {
            ont.olt_device_id
            for ont in displayed_onts
            if getattr(ont, "olt_device_id", None)
        }
        direct_pon_by_key: dict[tuple[str, str], PonPort] = {}
        if direct_olt_ids and direct_pon_names:
            direct_pon_rows = db.scalars(
                select(PonPort)
                .options(joinedload(PonPort.olt))
                .where(PonPort.olt_id.in_(direct_olt_ids))
                .where(PonPort.name.in_(direct_pon_names))
            ).all()
            direct_pon_by_key = {
                (str(row.olt_id), str(row.name)): row for row in direct_pon_rows
            }

        for ont in displayed_onts:
            ont_key = str(getattr(ont, "id", ""))
            if not ont_key:
                continue
            olt = getattr(ont, "olt_device", None)
            ont_olt_id = str(
                getattr(ont, "olt_device_id", None) or getattr(olt, "id", "") or ""
            )
            if not ont_olt_id:
                continue
            pon_fsp = _normalize_ont_port_display(ont.board, ont.port)
            pon_port = direct_pon_by_key.get((ont_olt_id, pon_fsp or ""))
            existing_info = assignment_info.get(ont_key, {})
            assignment_info[ont_key] = {
                **existing_info,
                "olt_name": str(getattr(olt, "name", None) or ""),
                "olt_id": ont_olt_id,
                "pon_port_name": str(getattr(pon_port, "name", None) or pon_fsp or ""),
                "pon_port_display": _pon_port_display_value(pon_port, pon_fsp or "-"),
                "subscriber_name": existing_info.get("subscriber_name", ""),
                "subscriber_customer_url": existing_info.get(
                    "subscriber_customer_url", ""
                ),
                "topology_source": "ont",
            }

    # Distinct vendors for filter dropdown
    vendor_rows = db.scalars(
        select(OntUnit.vendor)
        .where(OntUnit.vendor.isnot(None))
        .where(OntUnit.vendor != "")
        .distinct()
        .order_by(OntUnit.vendor)
    ).all()

    return {
        "onts": onts,
        "diagnostics_onts": diagnostics_onts,
        "stats": stats,
        "status_filter": status_filter,
        "signal_data": signal_data,
        "status_refresh_admission": status_refresh_admission,
        "assignment_info": assignment_info,
        "serial_display_by_ont_id": serial_display_by_ont_id,
        "hex_serial_by_ont_id": hex_serial_by_ont_id,
        "olts": olts,
        "vendors": list(vendor_rows),
        # Active filters for template state
        "zones": zones,
        # Active filters for template state
        "filters": {
            "olt_id": olt_id or "",
            "pon_port_id": pon_port_id or "",
            "pon_hint": pon_hint or "",
            "zone_id": zone_id or "",
            "operational_status": operational_status or "",
            "authorization": authorization_filter,
            "signal_quality": signal_quality or "",
            "search": search or "",
            "vendor": vendor or "",
            "view": normalized_view,
            "order_by": order_by,
            "order_dir": order_dir,
            "per_page": per_page,
        },
        # Pagination
        "pagination": {
            "page": current_page,
            "per_page": per_page,
            "total": total_filtered,
            "total_pages": total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < total_pages,
        },
        "diagnostics_pagination": {
            "page": current_page,
            "per_page": per_page,
            "total": diagnostics_total,
            "total_pages": diagnostics_total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < diagnostics_total_pages,
        },
    }


def ont_detail_page_data(db: Session, ont_id: str) -> dict[str, object] | None:
    """Return comprehensive ONT detail payload.

    Includes: device info, active assignment, OLT/PON path, subscriber,
    subscription, signal classification, and network location.
    """
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except Exception:
        return None

    assignments = network_service.ont_assignments.list(
        db=db,
        ont_unit_id=ont_id,
        pon_port_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assignment = next((a for a in assignments if a.active), None)
    past_assignments = [a for a in assignments if not a.active]

    # Signal classification uses the last persisted monitoring snapshot. Explicit
    # refresh actions are responsible for live polling.
    from app.services.network.signal_thresholds import (
        classify_signal,
        get_signal_thresholds,
        normalize_optical_signal_dbm,
    )

    olt_rx = normalize_optical_signal_dbm(getattr(ont, "olt_rx_signal_dbm", None))
    onu_rx = normalize_optical_signal_dbm(getattr(ont, "onu_rx_signal_dbm", None))
    signal_updated_at = getattr(ont, "signal_updated_at", None)

    warn, crit = get_signal_thresholds(db)
    olt_quality = classify_signal(olt_rx, warn_threshold=warn, crit_threshold=crit)
    onu_quality = classify_signal(onu_rx, warn_threshold=warn, crit_threshold=crit)
    connection_request_info = _connection_request_state_by_ont_id(db, [ont.id]).get(
        str(ont.id), {}
    )
    linked_tr069 = (
        db.query(Tr069CpeDevice)
        .filter(Tr069CpeDevice.ont_unit_id == ont.id)
        .filter(Tr069CpeDevice.is_active.is_(True))
        .order_by(Tr069CpeDevice.last_inform_at.desc().nullslast())
        .first()
    )
    raw_status = getattr(ont, "olt_status", None)
    status_val = (
        raw_status.value
        if hasattr(raw_status, "value")
        else str(raw_status or "offline")
    )
    if status_val not in ONLINE_STATUS_CLASSES:
        status_val = "unknown"
    effective_status = resolve_effective_ont_status(ont)
    from app.services.device_operational_status import (
        derive_ont_operational_status,
    )

    ont_op = derive_ont_operational_status(ont)
    status_display_val = effective_status.status.value
    status_source = effective_status.source.value
    normalized_acs_last_inform_at = getattr(ont, "acs_last_inform_at", None) or (
        getattr(linked_tr069, "last_inform_at", None) if linked_tr069 else None
    )
    acs_status = None
    acs_status_class = ACS_STATUS_CLASSES["unknown"]
    if linked_tr069 and getattr(linked_tr069, "genieacs_device_id", None):
        acs_status = "informed"
        acs_status_class = ACS_STATUS_CLASSES["online"]
    elif linked_tr069:
        acs_status = "linked"
        acs_status_class = ACS_STATUS_CLASSES["unmanaged"]
    reason = getattr(ont, "offline_reason", None)
    reason_val = reason.value if reason else None
    effective_last_seen_at = (
        getattr(ont, "last_seen_at", None)
        or getattr(ont, "olt_status_seen_at", None)
        or signal_updated_at
    )

    signal_info = {
        "olt_rx_dbm": olt_rx,
        "onu_rx_dbm": onu_rx,
        "olt_quality": olt_quality,
        "onu_quality": onu_quality,
        "olt_quality_class": SIGNAL_QUALITY_CLASSES.get(
            olt_quality, SIGNAL_QUALITY_CLASSES["unknown"]
        ),
        "onu_quality_class": SIGNAL_QUALITY_CLASSES.get(
            onu_quality, SIGNAL_QUALITY_CLASSES["unknown"]
        ),
        "distance_meters": getattr(ont, "distance_meters", None),
        "signal_updated_at": signal_updated_at,
        "signal_source": "cached",
        "olt_status": status_val,
        "olt_status_display": (
            status_val.replace("_", " ").title() if status_val else ""
        ),
        "olt_status_class": ONLINE_STATUS_CLASSES.get(
            status_val, ONLINE_STATUS_CLASSES["offline"]
        ),
        "olt_status_source": status_source,
        "status_source_class": ACS_STATUS_CLASSES.get(
            status_source, ACS_STATUS_CLASSES["unknown"]
        ),
        "acs_status": acs_status,
        "acs_status_class": acs_status_class,
        "last_seen_at": effective_last_seen_at,
        "acs_last_inform_at": normalized_acs_last_inform_at,
        "connection_request_status": connection_request_info.get(
            "connection_request_status", "unavailable"
        ),
        "connection_request_display": connection_request_info.get(
            "connection_request_display", "Unavailable"
        ),
        "connection_request_class": connection_request_info.get(
            "connection_request_class",
            CONNECTION_REQUEST_STATUS_CLASSES["unavailable"],
        ),
        "connection_request_url": connection_request_info.get("connection_request_url"),
        "connection_request_last_attempt_at": connection_request_info.get(
            "connection_request_last_attempt_at"
        ),
        "connection_request_message": connection_request_info.get(
            "connection_request_message"
        ),
        "offline_reason": reason_val,
        "offline_reason_display": OFFLINE_REASON_DISPLAY.get(reason_val, ""),
        "operational_status": ont_op.status,
        "operational_reason": ont_op.reason,
        "operational_reason_display": ont_op.reason_label,
        "status_presentation": ont_op.presentation,
        "warn_threshold": warn,
        "crit_threshold": crit,
        "online_status": status_display_val,
    }

    display_serial_number = _ont_display_serial(ont)
    display_serial_label = display_serial_number or "-"
    identity_label = _ont_identity_label(ont, display_serial_number)

    # Build network path info (OLT → PON Port → Splitter → ONT)
    network_path: dict[str, object] = {}
    if assignment and assignment.pon_port:
        pon_port = assignment.pon_port
        network_path["pon_port"] = pon_port.name
        if pon_port.olt:
            network_path["olt_name"] = pon_port.olt.name
            network_path["olt_id"] = str(pon_port.olt.id)
            network_path["olt_vendor"] = pon_port.olt.vendor
        # Check for splitter link
        if hasattr(pon_port, "splitter_link") and pon_port.splitter_link:
            link = pon_port.splitter_link
            if hasattr(link, "splitter_port") and link.splitter_port:
                sp = link.splitter_port
                if hasattr(sp, "splitter") and sp.splitter:
                    network_path["splitter_name"] = (
                        sp.splitter.name or str(sp.splitter.id)[:8]
                    )

    # Subscriber and subscription info
    subscriber_info: dict[str, object] = {}
    if assignment and assignment.subscriber:
        sub = assignment.subscriber
        subscriber_info["id"] = str(sub.id)
        subscriber_info["customer_url"] = (
            f"/admin/customers/business/{sub.id}"
            if getattr(sub, "category", None) == SubscriberCategory.business
            else f"/admin/customers/person/{sub.id}"
        )
        subscriber_info["name"] = _subscriber_display_name(sub)
        subscriber_info["status"] = sub.status.value if sub.status else "unknown"
        subscriber_info["status_class"] = SUBSCRIBER_STATUS_CLASSES.get(
            str(subscriber_info["status"]),
            SUBSCRIBER_STATUS_CLASSES["unknown"],
        )
    # Look up active subscription for subscriber (no longer directly on assignment)
    subscription = None
    if assignment and assignment.subscriber_id:
        subscription_stmt = (
            select(Subscription)
            .where(
                Subscription.subscriber_id == assignment.subscriber_id,
                Subscription.status == SubscriptionStatus.active,
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        subscription = db.scalars(subscription_stmt).first()
    else:
        effective_values = resolve_effective_ont_config(db, ont).get("values", {})
        pppoe_username = str(effective_values.get("pppoe_username") or "").strip()
        if pppoe_username:
            subscription_stmt = (
                select(Subscription)
                .options(
                    joinedload(Subscription.subscriber),
                    joinedload(Subscription.offer),
                )
                .where(Subscription.login == pppoe_username)
                .order_by(Subscription.updated_at.desc())
                .limit(1)
            )
            subscription = db.scalars(subscription_stmt).first()
            sub = getattr(subscription, "subscriber", None) if subscription else None
            if sub:
                subscriber_info["id"] = str(sub.id)
                subscriber_info["customer_url"] = (
                    f"/admin/customers/business/{sub.id}"
                    if getattr(sub, "category", None) == SubscriberCategory.business
                    else f"/admin/customers/person/{sub.id}"
                )
                subscriber_info["name"] = _subscriber_display_name(sub)
                subscriber_info["status"] = (
                    sub.status.value if sub.status else "unknown"
                )
                subscriber_info["status_class"] = SUBSCRIBER_STATUS_CLASSES.get(
                    str(subscriber_info["status"]),
                    SUBSCRIBER_STATUS_CLASSES["unknown"],
                )
                subscriber_info["source"] = "pppoe_login"
                subscriber_info["pppoe_username"] = pppoe_username

    if subscription:
        subscriber_info["subscription_id"] = str(subscription.id)
        subscriber_info["plan_name"] = (
            subscription.offer.name
            if hasattr(subscription, "offer") and subscription.offer
            else None
        )
        subscriber_info["subscription_status"] = (
            subscription.status.value if subscription.status else "unknown"
        )

    provisioning_runs: list[dict[str, object]] = []
    ont_plan: dict[str, object] = {}
    subscription_entity_id = subscription.id if subscription else None
    if subscription_entity_id is not None:
        from app.models.provisioning import ProvisioningRun, ServiceOrder

        run_stmt = (
            select(ProvisioningRun)
            .options(joinedload(ProvisioningRun.workflow))
            .where(ProvisioningRun.subscription_id == subscription_entity_id)
            .order_by(ProvisioningRun.created_at.desc())
            .limit(10)
        )
        recent_runs = list(db.scalars(run_stmt).all())
        for run in recent_runs:
            results = []
            if isinstance(run.output_payload, dict):
                maybe_results = run.output_payload.get("results")
                if isinstance(maybe_results, list):
                    results = [item for item in maybe_results if isinstance(item, dict)]
            provisioning_runs.append(
                {
                    "id": str(run.id),
                    "workflow_name": (
                        run.workflow.name
                        if getattr(run, "workflow", None) is not None
                        else None
                    ),
                    "status": run.status.value if run.status else "unknown",
                    "created_at": run.created_at,
                    "completed_at": run.completed_at,
                    "step_count": len(results),
                    "success_count": sum(
                        1 for item in results if item.get("status") == "success"
                    ),
                    "error_message": run.error_message,
                }
            )
        order_stmt = (
            select(ServiceOrder)
            .where(ServiceOrder.subscription_id == subscription_entity_id)
            .order_by(ServiceOrder.created_at.desc())
            .limit(1)
        )
        service_order = db.scalars(order_stmt).first()
        execution_context = getattr(service_order, "execution_context", None) or {}
        if isinstance(execution_context, dict):
            maybe_ont_plan = execution_context.get("ont_plan")
            if isinstance(maybe_ont_plan, dict):
                ont_plan = maybe_ont_plan

    from app.models.compensation_failure import CompensationFailure
    from app.services.network.compensation_retry import is_retry_due, next_retry_at

    compensation_stmt = (
        select(CompensationFailure)
        .where(CompensationFailure.ont_unit_id == ont.id)
        .order_by(CompensationFailure.created_at.desc())
        .limit(10)
    )
    compensation_failures = []
    for failure in db.scalars(compensation_stmt).all():
        next_attempt_at = next_retry_at(failure)
        compensation_failures.append(
            {
                "id": str(failure.id),
                "status": failure.status.value,
                "step_name": failure.step_name,
                "operation_type": failure.operation_type,
                "description": failure.description,
                "error_message": failure.error_message,
                "failure_count": failure.failure_count,
                "created_at": failure.created_at,
                "last_attempted_at": failure.last_attempted_at,
                "next_retry_at": next_attempt_at,
                "retry_due": is_retry_due(failure),
                "resolved_at": failure.resolved_at,
                "resolved_by": failure.resolved_by,
                "resource_id": failure.resource_id,
            }
        )

    # Per-ONT configuration is resolved from OLT config pack + OntUnit.desired_config.
    profile_state: dict[str, object] = {}

    # Note: available_profile_templates and available_firmware are now lazy-loaded
    # via HTMX endpoints to reduce initial page load time

    from app.services.service_intent_ui_adapter import service_intent_ui_adapter

    capabilities = service_intent_ui_adapter.ont_capabilities(db, ont_id=ont_id)
    try:
        from app.services.network.ont_tr069 import OntTR069

        cached_summary = OntTR069._summary_from_snapshot(ont)
        acs_observed_intent = (
            service_intent_ui_adapter.build_acs_observed_service_intent(cached_summary)
        )
    except Exception:
        logger.exception(
            "Failed to load cached ACS observed service intent for ONT %s", ont_id
        )
        acs_observed_intent = (
            service_intent_ui_adapter.build_acs_observed_service_intent(None)
        )

    # Auto-refresh stale TR-069 data in background (> 15 min old)
    _maybe_queue_stale_ont_refresh(db, ont, ont_id=ont_id)

    observed_runtime_summary = _acs_observed_runtime_summary(
        acs_observed_intent,
        db=db,
        ont=ont,
    )
    last_config_summary = _ont_last_config_summary(
        ont,
        acs_observed_intent=acs_observed_intent,
    )
    desired_config_summary = _ont_desired_config_summary(db, ont, ont_plan=ont_plan)
    connected_wifi_clients = observed_runtime_summary.get("wifi_clients")
    connected_customer_devices = observed_runtime_summary.get("customer_devices")

    # Configure form context (VLANs for dropdowns)
    from app.services import web_network_onts as web_network_onts_service

    configure_vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    configure_mgmt_ip_choices = web_network_onts_service.management_ip_choices_for_ont(
        db, ont
    )
    configuration_lifecycle = get_ont_service_configuration_projection(
        db, ont_unit_id=ont.id
    )
    if (
        configuration_lifecycle.configuration_head_id is not None
        and configuration_lifecycle.revision is not None
    ):
        provisioning_events = list_ont_provisioning_events(
            db,
            ont.id,
            limit=12,
            lifecycle_scope=ProvisioningLifecycleScope(
                configuration_head_id=(configuration_lifecycle.configuration_head_id),
                configuration_revision=configuration_lifecycle.revision,
            ),
        )
    else:
        provisioning_events = []
    provisioning_summary = _build_ont_provisioning_summary(
        ont,
        provisioning_events,
        configuration_phase=(
            configuration_lifecycle.phase.value
            if configuration_lifecycle.phase is not None
            else None
        ),
    )

    return {
        "ont": ont,
        "display_serial_number": display_serial_number,
        "display_serial_label": display_serial_label,
        "identity_label": identity_label,
        "assignment": assignment,
        "past_assignments": past_assignments,
        "signal_info": signal_info,
        "network_path": network_path,
        "subscriber_info": subscriber_info,
        "provisioning_runs": provisioning_runs,
        "compensation_failures": compensation_failures,
        "ont_plan": ont_plan,
        "observed_runtime_summary": observed_runtime_summary,
        "last_config_summary": last_config_summary,
        "desired_config_summary": desired_config_summary,
        "connected_customer_devices": connected_customer_devices,
        "connected_wifi_clients": connected_wifi_clients,
        "profile_state": profile_state,
        "capabilities": capabilities,
        "provisioning_summary": provisioning_summary,
        "configuration_lifecycle": configuration_lifecycle,
        "inventory_ready": (
            not bool(assignment)
            and not bool(getattr(ont, "external_id", None))
            and not bool(profile_state.get("profile_id"))
            and profile_state.get("status") in (None, "unprovisioned")
        ),
        # Configure form context
        "configure_vlans": configure_vlans,
        **configure_mgmt_ip_choices,
    }


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _runtime_value_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _acs_observed_runtime_summary(
    acs_observed_intent: dict[str, object], *, db: Session, ont: object
) -> dict[str, object]:
    effective = resolve_effective_ont_config(db, ont)
    values = effective["values"]
    tracked_index = acs_observed_intent.get("tracked_point_index", {})
    tracked_index = tracked_index if isinstance(tracked_index, dict) else {}

    def tracked_raw(key: str) -> object | None:
        point = tracked_index.get(key)
        if not isinstance(point, dict):
            return None
        return point.get("raw_value")

    observed = acs_observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    observed_wan = observed.get("wan", {})
    observed_wan = observed_wan if isinstance(observed_wan, dict) else {}
    observed_lan = observed.get("lan", {})
    observed_lan = observed_lan if isinstance(observed_lan, dict) else {}
    observed_wifi = observed.get("wifi", {})
    observed_wifi = observed_wifi if isinstance(observed_wifi, dict) else {}
    observed_system = observed.get("system", {})
    observed_system = observed_system if isinstance(observed_system, dict) else {}
    lan_hosts = observed.get("lan_hosts", [])
    lan_hosts = lan_hosts if isinstance(lan_hosts, list) else []

    wifi_clients = _safe_int(tracked_raw("wifi.connected_clients"))
    if wifi_clients is None:
        wifi_clients = _safe_int(getattr(ont, "observed_wifi_clients", None))

    active_devices = None
    if lan_hosts:
        active_devices = _lan_host_connected_count(lan_hosts)
    total_devices = len(lan_hosts) if lan_hosts else None
    if total_devices is None:
        total_devices = _safe_int(tracked_raw("lan.connected_hosts"))
    if total_devices is None:
        total_devices = _safe_int(getattr(ont, "observed_lan_hosts", None))
    customer_devices = active_devices
    if customer_devices is None:
        customer_devices = total_devices
    if customer_devices is None:
        customer_devices = wifi_clients

    wan_ip = (
        tracked_raw("wan.wan_ip")
        or observed_wan.get("wan_ip")
        or getattr(ont, "observed_wan_ip", None)
    )
    pppoe_user = (
        tracked_raw("wan.pppoe_username")
        or observed_wan.get("pppoe_username")
        or values.get("pppoe_username")
    )
    pppoe_status = (
        tracked_raw("wan.status")
        or observed_wan.get("status")
        or getattr(ont, "observed_pppoe_status", None)
    )
    lan_ip = tracked_raw("lan.lan_ip") or observed_lan.get("lan_ip")
    lan_mode = tracked_raw("lan.dhcp_enabled") or observed_lan.get("dhcp_enabled")
    ssid = observed_wifi.get("ssid")
    wifi_enabled = observed_wifi.get("enabled")
    wifi_channel = observed_wifi.get("channel")
    firmware = observed_system.get("firmware")
    uptime = observed_system.get("uptime")

    fetched_at = acs_observed_intent.get("fetched_at")
    updated_at_display = "-"
    if isinstance(fetched_at, datetime):
        updated_at_display = fetched_at.strftime("%Y-%m-%d %H:%M")
    elif fetched_at:
        updated_at_display = str(fetched_at)

    def runtime_field(
        label: str,
        value: object,
        *,
        monospace: bool = False,
        numeric: bool = False,
    ) -> dict[str, object] | None:
        if not _runtime_value_present(value):
            return None
        return {
            "label": label,
            "value": _display_bool(value) if isinstance(value, bool) else value,
            "monospace": monospace,
            "numeric": numeric,
        }

    runtime_fields = [
        runtime_field("WAN IP", wan_ip, monospace=True),
        runtime_field("PPPoE User", pppoe_user, monospace=True),
        runtime_field("PPPoE Status", pppoe_status),
        runtime_field("LAN IP", lan_ip, monospace=True),
        runtime_field("LAN DHCP", lan_mode),
        runtime_field("SSID", ssid),
        runtime_field("WiFi", wifi_enabled),
        runtime_field("WiFi Channel", wifi_channel, numeric=True),
        runtime_field("WiFi Clients", wifi_clients, numeric=True),
        runtime_field("Active Devices", active_devices, numeric=True),
        runtime_field(
            "Known Devices",
            total_devices if total_devices != active_devices else None,
            numeric=True,
        ),
        runtime_field("Firmware", firmware),
        runtime_field("Uptime", uptime),
    ]
    runtime_fields = [field for field in runtime_fields if field is not None]
    has_runtime = bool(
        runtime_fields or acs_observed_intent.get("available") or fetched_at
    )

    return {
        "has_runtime": has_runtime,
        "mac_address": tracked_raw("system.mac_address")
        or getattr(ont, "mac_address", None),
        "wan_ip": wan_ip,
        "pppoe_user": pppoe_user,
        "pppoe_status": pppoe_status,
        "wan_mode": values.get("wan_mode"),
        "lan_mode": lan_mode,
        "lan_ip": lan_ip,
        "wifi_clients": wifi_clients,
        "customer_devices": customer_devices,
        "active_devices": active_devices,
        "total_devices": total_devices,
        "runtime_fields": runtime_fields,
        "updated_at": fetched_at,
        "updated_at_display": updated_at_display,
    }


def _lan_host_connected_count(hosts: object) -> int | None:
    """Return the best known customer-device count behind an ONT."""
    if not isinstance(hosts, list):
        return None
    connected = 0
    for host in hosts:
        if not isinstance(host, dict):
            continue
        active = host.get("active")
        active_text = str(active).strip().lower()
        if active_text in {"false", "0", "no", "inactive", "down"}:
            continue
        if not any(
            str(host.get(key) or "").strip()
            for key in (
                "host_name",
                "ip_address",
                "mac_address",
                "HostName",
                "IPAddress",
                "MACAddress",
            )
        ):
            continue
        connected += 1
    return connected


def _active_ethernet_port_count(ports: object) -> tuple[int | None, int | None]:
    if not isinstance(ports, list):
        return None, None
    active = 0
    for port in ports:
        if not isinstance(port, dict):
            continue
        status = (
            str(port.get("link_status") or port.get("Status") or "").strip().lower()
        )
        if status == "up":
            active += 1
    return active, len(ports)


def _display_bool(value: object) -> str:
    if value is True:
        return "Enabled"
    if value is False:
        return "Disabled"
    return str(value or "")


def _event_status_value(value: object) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _humanize_provisioning_step(step_name: object) -> str:
    text = str(step_name or "").strip()
    known = {
        "authorization_baseline": "Authorization baseline",
        "apply_saved_service_config": "Saved service config",
        "provision": "Provisioning apply",
        "resolve_effective_config_pack": "Config pack resolution",
        "wait_tr069_bootstrap": "ACS bootstrap wait",
        "post_authorization_acs_link": "ACS link check",
    }
    if text in known:
        return known[text]
    return text.replace("_", " ").strip().title() or "Provisioning step"


def _build_ont_provisioning_summary(
    ont: object,
    events: Sequence[object],
    *,
    configuration_phase: str | None = None,
) -> dict[str, object]:
    phase_status = {
        "queued": "pending_service_config",
        "applying": "pending_service_config",
        "readback_pending": "pending_service_config",
        "verified": "provisioned",
        "failed": "failed",
        "superseded": "partial",
        "retired": "unprovisioned",
        "saved": "pending_service_config",
    }
    # Before an assigned ONT has a service-configuration head, commissioning
    # still owns the current operational phase (for example, waiting for the
    # first ACS Inform). Once a configuration lifecycle exists, its exact
    # assignment/revision phase takes precedence. Event evidence remains
    # lifecycle-scoped by the caller, so this fallback cannot reintroduce an
    # old assignment's unscoped latest failure.
    if configuration_phase is not None:
        status = phase_status.get(configuration_phase, "unprovisioned")
    else:
        raw_status = str(
            getattr(
                getattr(ont, "provisioning_status", None),
                "value",
                getattr(ont, "provisioning_status", None),
            )
            or ""
        ).strip()
        status = (
            raw_status if raw_status in _PROVISIONING_STATUS_META else "unprovisioned"
        )
    status_meta = _PROVISIONING_STATUS_META.get(
        status, _PROVISIONING_STATUS_META["unprovisioned"]
    )
    tone_classes = _PROVISIONING_TONE_CLASSES[status_meta["tone"]]

    latest_event = events[0] if events else None
    latest_domain_event = next(
        (
            event
            for event in events
            if isinstance(getattr(event, "event_data", None), dict)
            and isinstance(event.event_data.get("domain_outcomes"), dict)
        ),
        None,
    )
    latest_failure_event = next(
        (
            event
            for event in events
            if isinstance(getattr(event, "event_data", None), dict)
            and event.event_data.get("failure_class")
        ),
        None,
    )

    failure_class_code = None
    failure_class_label = None
    if latest_failure_event is not None and isinstance(
        getattr(latest_failure_event, "event_data", None), dict
    ):
        failure_class_code = (
            str(latest_failure_event.event_data.get("failure_class") or "").strip()
            or None
        )
        if failure_class_code:
            failure_class_label = _PROVISIONING_FAILURE_LABELS.get(
                failure_class_code,
                failure_class_code.replace("_", " ").strip().title(),
            )

    domain_rows: list[dict[str, object]] = []
    attention_items: list[dict[str, str]] = []
    success_items: list[str] = []
    domain_outcomes = {}
    if latest_domain_event is not None and isinstance(
        getattr(latest_domain_event, "event_data", None), dict
    ):
        raw_domain_outcomes = latest_domain_event.event_data.get("domain_outcomes")
        if isinstance(raw_domain_outcomes, dict):
            domain_outcomes = raw_domain_outcomes

    for key in _PROVISIONING_DOMAIN_ORDER:
        payload = domain_outcomes.get(key)
        if not isinstance(payload, dict):
            continue
        domain_status = str(payload.get("status") or "").strip() or "unknown"
        domain_meta = _PROVISIONING_DOMAIN_STATUS_META.get(
            domain_status, {"label": "Unknown", "tone": "slate"}
        )
        classes = _PROVISIONING_TONE_CLASSES[domain_meta["tone"]]
        label = _PROVISIONING_DOMAIN_LABELS.get(
            key, key.replace("_", " ").strip().title()
        )
        message = str(payload.get("message") or domain_meta["label"]).strip()
        row = {
            "key": key,
            "label": label,
            "status": domain_status,
            "status_label": domain_meta["label"],
            "badge_class": classes["badge"],
            "accent_class": classes["accent"],
            "dot_class": classes["dot"],
            "message": message,
        }
        domain_rows.append(row)
        if domain_status == "succeeded":
            success_items.append(f"{label}: {message}")
        else:
            attention_items.append(
                {
                    "label": label,
                    "status_label": domain_meta["label"],
                    "message": message,
                    "accent_class": classes["accent"],
                    "dot_class": classes["dot"],
                }
            )

    recent_events: list[dict[str, object]] = []
    for event in list(events)[:4]:
        recent_events.append(
            {
                "step_label": _humanize_provisioning_step(
                    getattr(event, "step_name", "")
                ),
                "status": _event_status_value(getattr(event, "status", None))
                or "unknown",
                "status_label": (
                    {
                        "succeeded": "Completed",
                        "failed": "Failed",
                        "waiting": "Waiting",
                        "skipped": "Skipped",
                    }.get(
                        _event_status_value(getattr(event, "status", None)), "Unknown"
                    )
                ),
                "badge_class": _PROVISIONING_TONE_CLASSES[
                    {
                        "succeeded": "emerald",
                        "failed": "rose",
                        "waiting": "sky",
                        "skipped": "slate",
                    }.get(_event_status_value(getattr(event, "status", None)), "slate")
                ]["badge"],
                "message": str(getattr(event, "message", "") or "").strip(),
                "created_at": getattr(event, "created_at", None),
            }
        )

    latest_message = (
        str(getattr(latest_event, "message", "") or "").strip()
        if latest_event is not None
        else ""
    )
    latest_status = (
        _event_status_value(getattr(latest_event, "status", None))
        if latest_event is not None
        else ""
    )

    return {
        "status": status,
        "headline": status_meta["headline"],
        "summary": status_meta["summary"],
        "next_step": status_meta["next_step"],
        "panel_class": tone_classes["panel"],
        "badge_class": tone_classes["badge"],
        "accent_class": tone_classes["accent"],
        "dot_class": tone_classes["dot"],
        "failure_class_code": failure_class_code,
        "failure_class_label": failure_class_label,
        "last_event_message": latest_message,
        "last_event_status": latest_status,
        "last_event_label": (
            _humanize_provisioning_step(getattr(latest_event, "step_name", ""))
            if latest_event is not None
            else ""
        ),
        "last_event_at": getattr(latest_event, "created_at", None)
        if latest_event is not None
        else None,
        "domain_rows": domain_rows,
        "attention_items": attention_items,
        "success_items": success_items[:3],
        "recent_events": recent_events,
        "has_events": bool(events),
        "action_url": f"/admin/network/onts/{getattr(ont, 'id', '')}/provision",
    }


def _format_observed_time(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if value:
        return str(value)
    return "unknown time"


def _ont_last_config_summary(
    ont: object,
    *,
    acs_observed_intent: dict[str, object],
) -> dict[str, object]:
    ont_id = str(getattr(ont, "id", ""))
    empty_summary = {
        "has_snapshot": False,
        "empty_message": "No cached config state yet.",
        "empty_hint": "Query the ONT to fetch and cache the latest TR-069 state.",
        "fetched_at": None,
        "fetched_at_display": "never",
        "source": "TR-069 cache",
        "manufacturer": "",
        "firmware": "",
        "wan_ip": "",
        "wan_status": "",
        "pppoe_user": "",
        "lan_ip": "",
        "dhcp_enabled": "",
        "ssid": "",
        "wifi_clients": None,
        "lan_hosts": None,
        "ethernet_ports": None,
        "active_ports": None,
        "metrics": [],
        "details": [],
        "configure_url": f"/admin/network/onts/{ont_id}?tab=effective-config",
        "query_url": f"/admin/network/onts/{ont_id}?tab=observed-state",
    }
    tracked_index = acs_observed_intent.get("tracked_point_index", {})
    tracked_index = tracked_index if isinstance(tracked_index, dict) else {}

    def tracked_raw(key: str) -> object | None:
        point = tracked_index.get(key)
        if not isinstance(point, dict):
            return None
        return point.get("raw_value")

    observed = acs_observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    ethernet_ports = observed.get("ethernet_ports", [])
    lan_hosts = observed.get("lan_hosts", [])

    fetched_at = acs_observed_intent.get("fetched_at")
    has_snapshot = bool(
        acs_observed_intent.get("available")
        or fetched_at
        or tracked_index
        or ethernet_ports
        or lan_hosts
    )
    if not has_snapshot:
        return empty_summary

    wifi_clients = _safe_int(tracked_raw("wifi.connected_clients"))
    lan_host_count = _lan_host_connected_count(lan_hosts)
    if lan_host_count is None:
        lan_host_count = _safe_int(tracked_raw("lan.connected_hosts"))
    active_ports, port_count = _active_ethernet_port_count(ethernet_ports)

    wan_ip = str(tracked_raw("wan.wan_ip") or "").strip()
    wan_status = str(tracked_raw("wan.status") or "").strip()
    pppoe_user = str(tracked_raw("wan.pppoe_username") or "").strip()
    lan_ip = str(tracked_raw("lan.lan_ip") or "").strip()
    dhcp_enabled = _display_bool(tracked_raw("lan.dhcp_enabled"))
    ssid = str(tracked_raw("wifi.ssid") or "").strip()
    metrics = [
        {
            "label": "WAN IP",
            "value": wan_ip or "-",
            "value_class": "font-mono",
        },
        {
            "label": "WAN status",
            "value": wan_status or "-",
            "value_class": "",
        },
        {
            "label": "SSID",
            "value": ssid or "-",
            "value_class": "font-mono",
        },
        {
            "label": "LAN hosts",
            "value": str(lan_host_count) if lan_host_count is not None else "-",
            "value_class": "",
        },
    ]
    details = [
        {
            "label": "PPPoE user",
            "value": pppoe_user or "-",
            "value_class": "font-mono",
        },
        {
            "label": "LAN gateway",
            "value": lan_ip or "-",
            "value_class": "font-mono",
        },
        {
            "label": "DHCP",
            "value": dhcp_enabled or "-",
            "value_class": "",
        },
        {
            "label": "WiFi clients",
            "value": str(wifi_clients) if wifi_clients is not None else "-",
            "value_class": "",
        },
        {
            "label": "Ethernet ports",
            "value": (
                f"{active_ports}/{port_count} up"
                if active_ports is not None and port_count is not None
                else str(port_count)
                if port_count is not None
                else "-"
            ),
            "value_class": "",
        },
    ]

    return {
        "has_snapshot": True,
        "empty_message": "",
        "empty_hint": "",
        "fetched_at": fetched_at if isinstance(fetched_at, datetime) else None,
        "fetched_at_display": _format_observed_time(fetched_at),
        "source": str(acs_observed_intent.get("source") or "TR-069 cache"),
        "manufacturer": str(tracked_raw("system.manufacturer") or ""),
        "firmware": str(tracked_raw("system.firmware") or ""),
        "wan_ip": wan_ip,
        "wan_status": wan_status,
        "pppoe_user": pppoe_user,
        "lan_ip": lan_ip,
        "dhcp_enabled": dhcp_enabled,
        "ssid": ssid,
        "wifi_clients": wifi_clients,
        "lan_hosts": lan_host_count,
        "ethernet_ports": port_count,
        "active_ports": active_ports,
        "metrics": metrics,
        "details": details,
        "configure_url": f"/admin/network/onts/{ont_id}?tab=effective-config",
        "query_url": f"/admin/network/onts/{ont_id}?tab=observed-state",
    }


def _display_config_value(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value or "").strip()
    return text or "-"


def _display_ip_protocol(value: object) -> str:
    text = _enum_or_text(value)
    if text == "dual_stack":
        return "Dual Stack (IPv4 + IPv6)"
    if text == "ipv4":
        return "IPv4"
    return _display_config_value(text)


def _secret_status(value: object) -> str:
    return "Set (hidden)" if str(value or "").strip() else "Not set"


def _enum_or_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _ont_desired_config_summary(
    db: Session,
    ont: object,
    *,
    ont_plan: dict[str, object],
) -> dict[str, object]:
    """Return DB-backed intended config for the overview page."""
    _ = ont_plan
    olt_snapshot = getattr(ont, "olt_observed_snapshot", None)
    olt_snapshot = olt_snapshot if isinstance(olt_snapshot, dict) else {}
    iphost = olt_snapshot.get("iphost_config")
    iphost = iphost if isinstance(iphost, dict) else {}
    effective = resolve_effective_ont_config(db, ont)
    values = effective["values"]

    mgmt_vlan = values.get("mgmt_vlan")
    wan_vlan = values.get("wan_vlan")
    rows = [
        {
            "label": "Mgmt mode",
            "value": _display_config_value(values.get("mgmt_ip_mode")),
            "value_class": "",
        },
        {
            "label": "Mgmt VLAN",
            "value": _display_config_value(mgmt_vlan),
            "value_class": "font-mono",
        },
        {
            "label": "Mgmt IP",
            "value": _display_config_value(values.get("mgmt_ip_address")),
            "value_class": "font-mono",
        },
        {
            "label": "Observed IPHOST",
            "value": _display_config_value(
                iphost.get("IP Address") or iphost.get("IP") or iphost.get("ip_address")
            ),
            "value_class": "font-mono",
        },
        {
            "label": "TR-069 profile",
            "value": _display_config_value(values.get("tr069_olt_profile_id")),
            "value_class": "font-mono",
        },
        {
            "label": "WAN mode",
            "value": _display_config_value(values.get("wan_mode")),
            "value_class": "",
        },
        {
            "label": "IP protocol",
            "value": _display_ip_protocol(values.get("ip_protocol")),
            "value_class": "",
        },
        {
            "label": "WAN VLAN",
            "value": _display_config_value(wan_vlan),
            "value_class": "font-mono",
        },
        {
            "label": "PPPoE user",
            "value": _display_config_value(values.get("pppoe_username")),
            "value_class": "font-mono",
        },
        {
            "label": "PPPoE password",
            "value": _secret_status(values.get("pppoe_password")),
            "value_class": "",
        },
        {
            "label": "LAN gateway",
            "value": _display_config_value(values.get("lan_ip")),
            "value_class": "font-mono",
        },
        {
            "label": "LAN subnet",
            "value": _display_config_value(values.get("lan_subnet")),
            "value_class": "font-mono",
        },
        {
            "label": "WiFi enabled",
            "value": _display_config_value(values.get("wifi_enabled")),
            "value_class": "",
        },
        {
            "label": "SSID",
            "value": _display_config_value(values.get("wifi_ssid")),
            "value_class": "font-mono",
        },
        {
            "label": "WiFi password",
            "value": _secret_status(values.get("wifi_password")),
            "value_class": "",
        },
        {
            "label": "WiFi channel",
            "value": _display_config_value(values.get("wifi_channel")),
            "value_class": "",
        },
    ]
    return {
        "rows": rows,
        "configured_count": sum(1 for row in rows if row["value"] != "-"),
        "configure_url": f"/admin/network/onts/{getattr(ont, 'id', '')}?tab=effective-config",
    }


def _subscriber_display_name(subscriber: object) -> str:
    """Build display name from subscriber fields and person fallback."""
    display_name = str(getattr(subscriber, "display_name", "") or "").strip()
    if display_name:
        return display_name

    full_name = str(getattr(subscriber, "full_name", "") or "").strip()
    if full_name:
        return full_name

    first = str(getattr(subscriber, "first_name", "") or "").strip()
    last = str(getattr(subscriber, "last_name", "") or "").strip()
    direct_name = f"{first} {last}".strip()
    if direct_name:
        return direct_name

    person = getattr(subscriber, "person", None)
    if person:
        first = getattr(person, "first_name", "") or ""
        last = getattr(person, "last_name", "") or ""
        name = f"{first} {last}".strip()
        if name:
            return name
    email = str(getattr(subscriber, "email", "") or "").strip()
    if email:
        return email
    subscriber_number = str(getattr(subscriber, "subscriber_number", "") or "").strip()
    if subscriber_number:
        return subscriber_number
    return str(getattr(subscriber, "id", "") or "").strip()[:8]


def get_change_request_asset(
    db: Session, asset_type: str | None, asset_id: str | None
) -> object | None:
    """Retrieve a fiber change request asset by type and id."""
    if not asset_type or not asset_id:
        return None
    from app.services import fiber_change_requests as change_requests

    _asset_type, model = change_requests._get_model(asset_type)
    return db.get(model, asset_id)


def _core_device_table_maps(
    db: Session, devices: Sequence[object]
) -> dict[str, object]:
    from app.services.web_network_core_devices_forms import (
        _format_uptime_short,
    )

    real_devices = [device for device in devices if isinstance(device, NetworkDevice)]
    device_ids = [device.id for device in real_devices]

    uptime_map: dict[str, str | None] = {}
    ping_history_map: dict[str, list[dict[str, object]]] = {}
    if device_ids:
        latest_uptime_subq = (
            select(
                DeviceMetric.device_id,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .where(DeviceMetric.device_id.in_(device_ids))
            .where(DeviceMetric.metric_type == MetricType.uptime)
            .group_by(DeviceMetric.device_id)
            .subquery()
        )
        latest_uptime_metrics = db.scalars(
            select(DeviceMetric)
            .join(
                latest_uptime_subq,
                and_(
                    DeviceMetric.device_id == latest_uptime_subq.c.device_id,
                    DeviceMetric.recorded_at == latest_uptime_subq.c.latest,
                ),
            )
            .where(DeviceMetric.metric_type == MetricType.uptime)
        ).all()
        for metric in latest_uptime_metrics:
            uptime_map[str(metric.device_id)] = _format_uptime_short(metric.value)

        latest_ping_subq = (
            select(
                DeviceMetric.device_id,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .where(DeviceMetric.device_id.in_(device_ids))
            .where(DeviceMetric.metric_type == MetricType.custom)
            .where(DeviceMetric.unit.in_(["ping_ms", "ping_timeout"]))
            .group_by(DeviceMetric.device_id)
            .subquery()
        )
        latest_ping_metrics = db.scalars(
            select(DeviceMetric)
            .join(
                latest_ping_subq,
                and_(
                    DeviceMetric.device_id == latest_ping_subq.c.device_id,
                    DeviceMetric.recorded_at == latest_ping_subq.c.latest,
                ),
            )
            .where(DeviceMetric.metric_type == MetricType.custom)
            .where(DeviceMetric.unit.in_(["ping_ms", "ping_timeout"]))
        ).all()
        for metric in latest_ping_metrics:
            ok = metric.unit == "ping_ms" and metric.value >= 0
            ping_history_map[str(metric.device_id)] = [
                {
                    "ok": ok,
                    "label": f"{metric.value}ms" if ok else "Timeout",
                    "recorded_at": metric.recorded_at,
                }
            ]

    return {
        "uptime_map": uptime_map,
        "ping_history_map": ping_history_map,
    }


def _inventory_search_filter(term: str, *columns: object):
    if not term:
        return None
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return or_(*(column.ilike(pattern, escape="\\") for column in columns))


def _page_metadata(total: int, requested_page: int, per_page: int) -> dict[str, object]:
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, requested_page), total_pages)
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def _statement_count(db: Session, statement) -> int:
    count_statement = select(func.count()).select_from(
        statement.order_by(None).subquery()
    )
    return int(db.scalar(count_statement) or 0)


def _paged_statement(statement, pagination: dict[str, object]):
    page = int(pagination["page"])
    per_page = int(pagination["per_page"])
    return statement.offset((page - 1) * per_page).limit(per_page)


def consolidated_page_data(
    tab: str,
    db: Session,
    search: str | None = None,
    *,
    page: int = 1,
    olt_page: int = 1,
    ont_page: int = 1,
    cpe_page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return one database-bounded consolidated inventory page."""
    term = (search or "").strip()
    selected_tab = tab if tab in {"core", "olts", "onts"} else "core"
    per_page = min(max(1, per_page), 200)

    active_olt_candidate = and_(
        NetworkDevice.is_active.is_(True),
        func.lower(func.trim(NetworkDevice.name)).like("%olt"),
    )
    network_core_filters: list[object] = [not_(active_olt_candidate)]
    network_search = _inventory_search_filter(
        term,
        NetworkDevice.name,
        NetworkDevice.hostname,
        NetworkDevice.mgmt_ip,
        NetworkDevice.vendor,
        NetworkDevice.model,
        NetworkDevice.serial_number,
        cast(NetworkDevice.role, String),
    )
    if network_search is not None:
        network_core_filters.append(network_search)

    nas_ip = func.coalesce(
        NasDevice.management_ip,
        NasDevice.ip_address,
        NasDevice.nas_ip,
        "",
    )
    matching_network_device = (
        select(NetworkDevice.id)
        .where(
            func.coalesce(NetworkDevice.mgmt_ip, "") == nas_ip,
            func.coalesce(NetworkDevice.hostname, "")
            == func.coalesce(NasDevice.code, ""),
            func.coalesce(NetworkDevice.name, "") == NasDevice.name,
        )
        .exists()
    )
    nas_core_filters: list[object] = [
        NasDevice.is_active.is_(True),
        not_(matching_network_device),
    ]
    nas_search = _inventory_search_filter(
        term,
        NasDevice.name,
        NasDevice.code,
        nas_ip,
        cast(NasDevice.vendor, String),
        NasDevice.model,
        NasDevice.serial_number,
    )
    if nas_search is not None:
        nas_core_filters.append(nas_search)

    core_refs = union_all(
        select(
            literal("network").label("kind"),
            NetworkDevice.id.label("item_id"),
            func.lower(NetworkDevice.name).label("sort_name"),
        ).where(*network_core_filters),
        select(
            literal("nas").label("kind"),
            NasDevice.id.label("item_id"),
            func.lower(NasDevice.name).label("sort_name"),
        ).where(*nas_core_filters),
    ).subquery()
    core_ref_statement = select(
        core_refs.c.kind,
        core_refs.c.item_id,
    ).order_by(core_refs.c.sort_name, core_refs.c.kind, core_refs.c.item_id)
    core_total = _statement_count(db, core_ref_statement)

    # Promotion remains an inventory synchronization concern, but the OLT page
    # keeps the established compatibility behavior until the importer owns it.
    promoted_devices = db.scalars(
        select(NetworkDevice).where(active_olt_candidate).order_by(NetworkDevice.name)
    ).all()
    for device in promoted_devices:
        resolve_olt_device_for_network_device(db, device)

    olt_filters: list[object] = []
    olt_search = _inventory_search_filter(
        term,
        OLTDevice.name,
        OLTDevice.hostname,
        OLTDevice.mgmt_ip,
        OLTDevice.vendor,
        OLTDevice.model,
        OLTDevice.serial_number,
    )
    if olt_search is not None:
        olt_filters.append(olt_search)
    olt_base = select(OLTDevice)
    if olt_filters:
        olt_base = olt_base.where(*olt_filters)
    olt_statement = olt_base.order_by(OLTDevice.name.asc(), OLTDevice.id.asc())
    olt_total = _statement_count(db, olt_statement)
    olt_active_statement = (
        select(func.count()).select_from(OLTDevice).where(OLTDevice.is_active.is_(True))
    )
    if olt_filters:
        olt_active_statement = olt_active_statement.where(*olt_filters)
    olt_active = int(db.scalar(olt_active_statement) or 0)

    ont_filters: list[object] = []
    ont_search = _inventory_search_filter(
        term,
        OntUnit.serial_number,
        OntUnit.vendor_serial_number,
        OntUnit.name,
        OntUnit.vendor,
        OntUnit.model,
        OntUnit.firmware_version,
        OntUnit.software_version,
        OntUnit.observed_wan_ip,
        OntUnit.notes,
    )
    if ont_search is not None:
        ont_filters.append(ont_search)
    ont_base = select(OntUnit)
    if ont_filters:
        ont_base = ont_base.where(*ont_filters)
    ont_statement = ont_base.order_by(OntUnit.serial_number.asc(), OntUnit.id.asc())
    ont_total = _statement_count(db, ont_statement)
    inactive_statement = (
        select(func.count()).select_from(OntUnit).where(OntUnit.is_active.is_(False))
    )
    if ont_filters:
        inactive_statement = inactive_statement.where(*ont_filters)
    ont_inactive = int(db.scalar(inactive_statement) or 0)

    cpe_filters: list[object] = []
    cpe_search = _inventory_search_filter(
        term,
        CPEDevice.serial_number,
        CPEDevice.vendor,
        CPEDevice.model,
        CPEDevice.mac_address,
        CPEDevice.last_uisp_status,
        CPEDevice.notes,
    )
    if cpe_search is not None:
        cpe_filters.append(cpe_search)
    cpe_base = select(CPEDevice)
    if cpe_filters:
        cpe_base = cpe_base.where(*cpe_filters)
    cpe_statement = cpe_base.order_by(CPEDevice.created_at.desc(), CPEDevice.id.asc())
    cpe_total = _statement_count(db, cpe_statement)

    core_pagination = _page_metadata(core_total, page, per_page)
    olt_pagination = _page_metadata(olt_total, olt_page, per_page)
    ont_pagination = _page_metadata(ont_total, ont_page, per_page)
    cpe_pagination = _page_metadata(cpe_total, cpe_page, per_page)

    core_devices: list[object] = []
    olts: list[OLTDevice] = []
    onts: list[OntUnit] = []
    cpes: list[CPEDevice] = []
    olt_stats: dict[str, dict[str, int]] = {}
    signal_data: dict[str, dict[str, str]] = {}
    serial_display_by_ont_id: dict[str, str] = {}
    core_table_maps: dict[str, object] = {
        "display_status_map": {},
        "uptime_map": {},
        "ping_history_map": {},
    }

    nas_total = int(
        db.scalar(select(func.count()).select_from(NasDevice).where(*nas_core_filters))
        or 0
    )
    role_rows = db.execute(
        select(NetworkDevice.role, func.count())
        .where(*network_core_filters)
        .group_by(NetworkDevice.role)
    ).all()
    core_roles = {role.value: int(count) for role, count in role_rows if role}
    core_roles = {
        role: core_roles.get(role, 0)
        + (nas_total if role == DeviceRole.access.value else 0)
        for role in ("core", "distribution", "access", "aggregation", "edge")
    }

    if selected_tab == "core":
        refs = db.execute(_paged_statement(core_ref_statement, core_pagination)).all()
        network_ids = [row.item_id for row in refs if row.kind == "network"]
        nas_ids = [row.item_id for row in refs if row.kind == "nas"]

        network_by_id: dict[str, NetworkDevice] = {}
        if network_ids:
            network_rows = (
                db.scalars(
                    select(NetworkDevice)
                    .options(joinedload(NetworkDevice.pop_site))
                    .where(NetworkDevice.id.in_(network_ids))
                )
                .unique()
                .all()
            )
            network_by_id = {str(device.id): device for device in network_rows}

        nas_by_id: dict[str, NasDevice] = {}
        routers_by_nas_id: dict[object, Router] = {}
        linked_network_by_id: dict[object, NetworkDevice] = {}
        if nas_ids:
            nas_rows = db.scalars(
                select(NasDevice).where(NasDevice.id.in_(nas_ids))
            ).all()
            nas_by_id = {str(device.id): device for device in nas_rows}
            linked_network_ids = [
                device.network_device_id
                for device in nas_rows
                if device.network_device_id
            ]
            if linked_network_ids:
                linked_network_by_id = {
                    device.id: device
                    for device in db.scalars(
                        select(NetworkDevice).where(
                            NetworkDevice.id.in_(linked_network_ids)
                        )
                    ).all()
                }
            routers = db.scalars(
                select(Router).where(
                    Router.is_active.is_(True),
                    Router.nas_device_id.in_(nas_ids),
                )
            ).all()
            routers_by_nas_id = {
                router.nas_device_id: router
                for router in routers
                if router.nas_device_id
            }

        for row in refs:
            if row.kind == "network":
                device = network_by_id.get(str(row.item_id))
                if device is not None:
                    core_devices.append(device)
                continue
            nas = nas_by_id.get(str(row.item_id))
            if nas is not None:
                core_devices.append(
                    _nas_inventory_stub(
                        nas,
                        linked_device=linked_network_by_id.get(nas.network_device_id),
                        linked_router=routers_by_nas_id.get(nas.id),
                    )
                )

        from app.services.device_operational_status import annotate_operational_status

        annotate_operational_status(core_devices)
        legacy_probe_statuses = _build_legacy_probe_statuses(
            [{"id": str(getattr(device, "id", ""))} for device in core_devices]
        )
        for device in core_devices:
            probe_status = (
                legacy_probe_statuses.get(str(getattr(device, "id", ""))) or {}
            )
            device.ping_status = {
                "label": probe_status.get("ping_label") or "Timeout",
                "state": probe_status.get("ping_state") or "fail",
                "source": "Legacy",
                "reason": probe_status.get("ping_reason"),
            }
            device.snmp_status = {
                "label": probe_status.get("snmp_label") or "Unknown",
                "state": probe_status.get("snmp_state") or "unknown",
                "source": "Legacy",
                "reason": probe_status.get("snmp_reason"),
            }
        core_table_maps = _core_device_table_maps(db, core_devices)

    if selected_tab == "olts":
        olts = list(db.scalars(_paged_statement(olt_statement, olt_pagination)).all())
        monitoring_conditions: list[object] = []
        for olt in olts:
            if olt.mgmt_ip:
                monitoring_conditions.append(NetworkDevice.mgmt_ip == olt.mgmt_ip)
            if olt.hostname:
                monitoring_conditions.append(NetworkDevice.hostname == olt.hostname)
            if olt.name:
                monitoring_conditions.append(NetworkDevice.name == olt.name)
        monitoring_devices = (
            list(
                db.scalars(
                    select(NetworkDevice).where(or_(*monitoring_conditions))
                ).all()
            )
            if monitoring_conditions
            else []
        )
        by_mgmt_ip = {
            device.mgmt_ip: device for device in monitoring_devices if device.mgmt_ip
        }
        by_hostname = {
            device.hostname: device for device in monitoring_devices if device.hostname
        }
        by_name = {device.name: device for device in monitoring_devices if device.name}

        interfaces_by_device_id: dict[str, list[DeviceInterface]] = {}
        monitoring_ids = [device.id for device in monitoring_devices]
        if monitoring_ids:
            interfaces = db.scalars(
                select(DeviceInterface).where(
                    DeviceInterface.device_id.in_(monitoring_ids)
                )
            ).all()
            for interface in interfaces:
                interfaces_by_device_id.setdefault(str(interface.device_id), []).append(
                    interface
                )

        pon_counts = {}
        olt_ids = [olt.id for olt in olts]
        if olt_ids:
            pon_counts = {
                str(olt_id): int(count)
                for olt_id, count in db.execute(
                    select(PonPort.olt_id, func.count())
                    .where(
                        PonPort.olt_id.in_(olt_ids),
                        PonPort.is_active.is_(True),
                    )
                    .group_by(PonPort.olt_id)
                ).all()
            }

        from app.services.device_operational_status import (
            derive_olt_operational_status,
            warmer_is_stale,
        )

        warm_stale = warmer_is_stale()
        for olt in olts:
            linked = by_mgmt_ip.get(olt.mgmt_ip) if olt.mgmt_ip else None
            if linked is None and olt.hostname:
                linked = by_hostname.get(olt.hostname)
            if linked is None:
                linked = by_name.get(olt.name)
            olt.runtime_status = (
                linked.status.value if linked and linked.status else "unknown"
            )
            olt.operational = derive_olt_operational_status(
                olt,
                linked_live_status=getattr(linked, "live_status", None),
                warm_stale=warm_stale,
            )
            db_count = pon_counts.get(str(olt.id), 0)
            snmp_count = 0
            if not db_count and linked is not None:
                snmp_count = sum(
                    1
                    for interface in interfaces_by_device_id.get(str(linked.id), [])
                    if any(
                        token
                        in f"{interface.name or ''} {interface.description or ''}".lower()
                        for token in ("pon", "gpon", "epon", "xgpon", "xgs")
                    )
                )
            olt_stats[str(olt.id)] = {"pon_ports": db_count or snmp_count}

    if selected_tab == "onts":
        onts = list(db.scalars(_paged_statement(ont_statement, ont_pagination)).all())
        cpes = list(db.scalars(_paged_statement(cpe_statement, cpe_pagination)).all())
        serial_display_by_ont_id = {
            str(ont.id): _ont_display_serial(ont) for ont in onts
        }
        from app.services.device_operational_status import derive_ont_operational_status

        for ont in onts:
            operational = derive_ont_operational_status(ont)
            signal_data[str(ont.id)] = {
                "operational": operational.status,
                "operational_reason": operational.reason,
                "status_presentation": operational.presentation,
            }

    stats = {
        "core_total": core_total,
        "core_roles": core_roles,
        "olt_total": olt_total,
        "olt_active": olt_active,
        "ont_total": ont_total,
        "ont_inactive": ont_inactive,
        "cpe_total": cpe_total,
    }
    return {
        "tab": selected_tab,
        "search": search or "",
        "stats": stats,
        "core_devices": core_devices,
        **core_table_maps,
        "olts": olts,
        "olt_stats": olt_stats,
        "onts": onts,
        "serial_display_by_ont_id": serial_display_by_ont_id,
        "signal_data": signal_data,
        "cpes": cpes,
        "core_pagination": core_pagination,
        "olt_pagination": olt_pagination,
        "ont_pagination": ont_pagination,
        "cpe_pagination": cpe_pagination,
    }


def _backup_notes_has_failure(notes: str | None) -> bool:
    if not notes:
        return False
    lowered = notes.lower()
    return any(token in lowered for token in ("fail", "error", "timeout", "denied"))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def backup_overview_page_data(
    db: Session,
    *,
    query: BackupOverviewQuery,
) -> BackupOverviewPage:
    """Return unified NAS/OLT backup overview rows for /admin/network/backups."""
    from app.models.catalog import NasConfigBackup, NasDevice
    from app.models.network import OltConfigBackup, OLTDevice

    cutoff = datetime.now(UTC) - timedelta(hours=query.stale_hours)
    rows: list[BackupOverviewRow] = []
    term = (query.list_query.search or "").lower()

    nas_devices = list(
        db.scalars(select(NasDevice).order_by(NasDevice.name.asc())).all()
    )
    for device in nas_devices:
        latest = db.execute(
            select(NasConfigBackup)
            .where(NasConfigBackup.nas_device_id == device.id)
            .order_by(NasConfigBackup.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        last_backup_at = _as_utc(latest.created_at) if latest else None
        last_message = (
            f"{latest.backup_method.value.upper() if latest.backup_method else 'MANUAL'} "
            f"backup ({latest.config_size_bytes or 0} bytes)"
            if latest
            else "No backup snapshot found"
        )
        failed = bool(
            latest
            and (
                (latest.config_size_bytes is not None and latest.config_size_bytes <= 0)
                or _backup_notes_has_failure(latest.notes)
            )
        )
        is_stale = (last_backup_at is None) or (last_backup_at < cutoff)
        backup_status: BackupOverviewStatus = (
            "failed" if failed else ("stale" if is_stale else "success")
        )
        rows.append(
            BackupOverviewRow(
                id=f"nas:{device.id}",
                device_id=str(device.id),
                backup_id=str(latest.id) if latest else None,
                device_name=device.name,
                device_type="nas",
                group=device.pop_site.name if device.pop_site else "-",
                vendor=device.vendor.value if getattr(device, "vendor", None) else None,
                model=device.model,
                ip_address=device.management_ip or device.ip_address or "-",
                port=str(device.management_port or "-"),
                last_backup_at=last_backup_at,
                last_message=last_message,
                backup_status=backup_status,
                device_url=f"/admin/network/nas/devices/{device.id}",
                backup_url=f"/admin/network/nas/backups/{latest.id}"
                if latest
                else None,
                history_url=f"/admin/network/nas/devices/{device.id}/backups",
            )
        )

    olts = list(db.scalars(select(OLTDevice).order_by(OLTDevice.name.asc())).all())
    for olt in olts:
        latest = db.execute(
            select(OltConfigBackup)
            .where(OltConfigBackup.olt_device_id == olt.id)
            .order_by(OltConfigBackup.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        last_backup_at = _as_utc(latest.created_at) if latest else None
        last_message = (
            f"{latest.backup_type.value.title()} backup ({latest.file_size_bytes or 0} bytes)"
            if latest
            else "No backup snapshot found"
        )
        failed = bool(
            latest
            and (
                (latest.file_size_bytes is not None and latest.file_size_bytes <= 0)
                or _backup_notes_has_failure(latest.notes)
            )
        )
        is_stale = (last_backup_at is None) or (last_backup_at < cutoff)
        backup_status = "failed" if failed else ("stale" if is_stale else "success")
        rows.append(
            BackupOverviewRow(
                id=f"olt:{olt.id}",
                device_id=str(olt.id),
                backup_id=str(latest.id) if latest else None,
                device_name=olt.name,
                device_type="olt",
                group="-",
                vendor=olt.vendor,
                model=olt.model,
                ip_address=olt.mgmt_ip or "-",
                port="-",
                last_backup_at=last_backup_at,
                last_message=last_message,
                backup_status=backup_status,
                device_url=f"/admin/network/olts/{olt.id}",
                backup_url=f"/admin/network/olts/backups/{latest.id}"
                if latest
                else None,
                history_url=f"/admin/network/olts/{olt.id}/backups",
            )
        )

    if query.device_type in {"nas", "olt"}:
        rows = [row for row in rows if row.device_type == query.device_type]
    if query.status in {"success", "stale", "failed"}:
        rows = [row for row in rows if row.backup_status == query.status]
    if term:
        rows = [
            row
            for row in rows
            if term
            in " ".join(
                str(value or "").lower()
                for value in (
                    row.device_name,
                    row.device_type,
                    row.group,
                    row.vendor,
                    row.model,
                    row.ip_address,
                    row.last_message,
                )
            )
        ]

    min_ts = datetime.min.replace(tzinfo=UTC)
    if query.list_query.sort_dir == "desc":
        rows.sort(key=lambda row: row.last_backup_at or min_ts, reverse=True)
    else:
        rows.sort(key=lambda row: row.last_backup_at or min_ts)

    stats = BackupOverviewStats(
        total=len(rows),
        success=sum(1 for row in rows if row.backup_status == "success"),
        stale=sum(1 for row in rows if row.backup_status == "stale"),
        failed=sum(1 for row in rows if row.backup_status == "failed"),
        nas=sum(1 for row in rows if row.device_type == "nas"),
        olt=sum(1 for row in rows if row.device_type == "olt"),
    )
    page_meta = PageMeta.from_query(query.list_query, stats.total)
    effective_query = query.list_query.with_page(page_meta.page)
    page_rows = tuple(
        rows[effective_query.offset : effective_query.offset + effective_query.per_page]
    )
    return BackupOverviewPage(
        rows=page_rows,
        stats=stats,
        query=BackupOverviewQuery(
            list_query=effective_query,
            stale_hours=query.stale_hours,
        ),
        page_meta=page_meta,
    )
