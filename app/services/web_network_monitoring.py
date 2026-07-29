"""Service helpers for admin network monitoring web routes."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess  # nosec
import time
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any, cast

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.services.audit_helpers import build_audit_activities_for_types

logger = logging.getLogger(__name__)

_VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")
WG_BIN = shutil.which("wg") or "/usr/bin/wg"

_TUNNEL_NAMES = {
    "KX5kLfJ1uMzMHTdLbdMVXTdxgwoDm7FR/xTvTlh2Lyw=": "Abuja Core (Garki)",
    "5EotB4DMlz9h89pRSmmSd2J0krVKRgdJsNzRx1ya5Gw=": "Lagos Medallion",
    "6zaWZIeQkgLRhePeGB+UReEMqbCg+RG95HMTEMQ69Tk=": "Demo NAS (Karu)",
}


def active_monitoring_devices(db: Session) -> list:
    """Return active monitoring devices for health refresh jobs."""
    from app.models.network_monitoring import NetworkDevice

    return db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()


def monitoring_page_data(
    db: Session,
    *,
    format_duration,
    format_bps,
    query: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Return payload for network monitoring dashboard.

    Delegates to the centralized core service method and adds
    bandwidth summary context.
    """
    from app.services.network_monitoring import NetworkDevices

    data = NetworkDevices.get_monitoring_dashboard_stats(
        db,
        format_duration=format_duration,
        format_bps=format_bps,
        query=query,
    )

    # Add bandwidth context
    data["bandwidth"] = _get_bandwidth_summary()
    data["nas_throughput"] = _get_nas_throughput_summary(db)

    # ONT status summaries and PON outage detection (Sprint 2)
    from app.services.network_monitoring import (
        get_onu_status_summary,
        get_pon_outage_summary,
    )

    data["ont_service_summary"] = get_onu_status_summary(db)
    data["pon_outages"] = get_pon_outage_summary(db)

    # ONT status trend chart (24-hour time series)
    data["ont_service_trend"] = _get_onu_status_trend(db, hours=24)

    # ONU authorization trend (Phase 6D — new registrations per day, 30 days)
    data["onu_auth_trend"] = _get_onu_auth_trend(db, days=30)

    # Network activity feed (Phase 6E — recent audit events)
    data["network_activity"] = _get_network_activity_feed(db, limit=15)

    data["query"] = (query or "").strip()
    data["last_refreshed_at"] = datetime.now(UTC)

    # Device health metrics table (CPU, memory, temperature per device)
    device_health, device_health_pagination = _get_device_health_page(
        db, query=query, page=page, per_page=per_page
    )
    data["device_health"] = device_health
    data["device_health_total"] = device_health_pagination["total"]
    data["device_health_pagination"] = device_health_pagination

    return data


def dispatch_monitoring_refresh(*, request_id: str | None = None) -> None:
    """No-op refresh hook for the page's ``?refresh=1``.

    The Zabbix cache warmer this used to enqueue was retired with the native
    monitoring cutover; the dashboard reads native poll state directly, so
    there is nothing to re-warm.
    """
    del request_id


def monitoring_index_context(
    db: Session,
    *,
    format_duration,
    format_bps,
    query: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    """Build the full monitoring dashboard context payload."""
    data = monitoring_page_data(
        db,
        format_duration=format_duration,
        format_bps=format_bps,
        query=query,
        page=page,
        per_page=per_page,
    )
    data["vpn_tunnels"] = get_vpn_tunnel_status()
    data["site_reachability"] = get_site_reachability(db)
    data["activities"] = build_audit_activities_for_types(
        db,
        ["core_device", "network_device"],
        limit=5,
    )
    return data


_BANDWIDTH_CTX_CACHE: dict[str, Any] = {"value": None, "expires_at": 0.0}
_BANDWIDTH_CTX_TTL_SECONDS = 10.0


def monitoring_bandwidth_context(db: Session) -> dict[str, object]:
    """Build context for the auto-refreshing live bandwidth partial.

    Mirrors the bandwidth/nas_throughput slice of ``monitoring_page_data`` so
    the network-wide throughput panel can poll independently of the full page.

    The result is the same network-wide aggregate for every viewer, so it is
    cached ~10s and shared process-wide: the 15s HTMX poll then issues the
    VictoriaMetrics fan-out (5 blocking instant queries) at most once per
    window regardless of how many NOC tabs are open, instead of once per tab.
    """
    now = time.monotonic()
    cached = _BANDWIDTH_CTX_CACHE
    if cached["value"] is not None and now < cached["expires_at"]:
        return cached["value"]
    value: dict[str, object] = {
        "bandwidth": _get_bandwidth_summary(),
        "nas_throughput": _get_nas_throughput_summary(db),
    }
    cached["value"] = value
    cached["expires_at"] = now + _BANDWIDTH_CTX_TTL_SECONDS
    return value


def monitoring_kpi_context(
    db: Session,
    *,
    format_duration,
    format_bps,
) -> dict[str, object]:
    """Build context for the auto-refreshing monitoring KPI partial."""
    from app.services.network_monitoring import (
        NetworkDevices,
        get_onu_status_summary,
        get_pon_outage_summary,
    )

    stats = NetworkDevices.get_monitoring_dashboard_stats(
        db, format_duration=format_duration, format_bps=format_bps
    )
    alarms_data = alarms_page_data(db, severity=None, status=None)
    return {
        "stats": stats.get("stats", {}),
        "ont_service_summary": get_onu_status_summary(db),
        "pon_outages": get_pon_outage_summary(db),
        "alarms": alarms_data.get("alarms", []),
        "vpn_tunnels": get_vpn_tunnel_status(),
        "site_reachability": get_site_reachability(db),
        "now": datetime.now(UTC),
    }


def get_vpn_tunnel_status() -> list[dict[str, object]]:
    """Read WireGuard peer status from wg show."""
    tunnels: list[dict[str, object]] = []
    try:
        result = subprocess.run(  # noqa: S603
            [WG_BIN, "show", "wg0", "dump"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        lines = result.stdout.strip().split("\n")
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            pubkey = parts[0]
            endpoint = parts[2] if parts[2] != "(none)" else None
            handshake_ts = int(parts[4]) if parts[4] != "0" else 0
            rx_bytes = int(parts[5])
            tx_bytes = int(parts[6])

            handshake_dt = (
                datetime.fromtimestamp(handshake_ts, tz=UTC) if handshake_ts else None
            )
            stale = True
            if handshake_dt:
                stale = (datetime.now(UTC) - handshake_dt) > timedelta(minutes=3)

            tunnels.append(
                {
                    "name": _TUNNEL_NAMES.get(pubkey, pubkey[:12] + "..."),
                    "endpoint": endpoint,
                    "handshake": handshake_dt,
                    "handshake_ago": format_ago(handshake_dt)
                    if handshake_dt
                    else "never",
                    "rx": format_bytes(rx_bytes),
                    "tx": format_bytes(tx_bytes),
                    "up": not stale and handshake_ts > 0,
                    "stale": stale,
                }
            )
    except FileNotFoundError:
        logger.warning("WireGuard 'wg' command not found - VPN status unavailable")
    except PermissionError:
        logger.warning("Insufficient permissions to read WireGuard status")
    except Exception as exc:
        logger.warning("Failed to read WireGuard status: %s", exc)
    return tunnels


def format_ago(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    if delta.total_seconds() < 60:
        return f"{int(delta.total_seconds())}s ago"
    if delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() / 60)}m ago"
    return f"{int(delta.total_seconds() / 3600)}h ago"


def format_bytes(value: int) -> str:
    if value >= 1_073_741_824:
        return f"{value / 1_073_741_824:.1f} GB"
    if value >= 1_048_576:
        return f"{value / 1_048_576:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} B"


def alarms_page_data(
    db: Session,
    *,
    severity: str | None,
    status: str | None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    """Return payload for monitoring alarms page."""
    from app.models.network_monitoring import (
        Alert,
        AlertRule,
        AlertSeverity,
        AlertStatus,
    )

    alarms_query = db.query(Alert).order_by(Alert.triggered_at.desc())
    if status:
        try:
            alarms_query = alarms_query.filter(Alert.status == AlertStatus(status))
        except ValueError:
            pass
    else:
        alarms_query = alarms_query.filter(
            Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged])
        )
    if severity:
        try:
            alarms_query = alarms_query.filter(
                Alert.severity == AlertSeverity(severity)
            )
        except ValueError:
            pass
    total = alarms_query.order_by(None).count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    alarms = alarms_query.offset((page - 1) * per_page).limit(per_page).all()
    rules = (
        db.query(AlertRule)
        .filter(AlertRule.is_active.is_(True))
        .order_by(AlertRule.name)
        .all()
    )
    severity_rows = (
        db.query(Alert.severity, func.count(Alert.id))
        .filter(Alert.status == AlertStatus.open)
        .group_by(Alert.severity)
        .all()
    )
    open_counts = {row[0].value: int(row[1]) for row in severity_rows}
    stats = {
        "critical": open_counts.get(AlertSeverity.critical.value, 0),
        "warning": open_counts.get(AlertSeverity.warning.value, 0),
        "info": open_counts.get(AlertSeverity.info.value, 0),
        "total_open": sum(open_counts.values()),
    }
    return {
        "alarms": alarms,
        "rules": rules,
        "stats": stats,
        "severity": severity,
        "status": status,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    }


def _vm_instant_query(query: str) -> list[dict[str, Any]]:
    """Execute a synchronous PromQL instant query against VictoriaMetrics.

    Returns an empty list on any error so callers always get a safe value.
    """
    try:
        resp = httpx.get(
            f"{_VICTORIAMETRICS_URL}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "success":
            result = data.get("data", {}).get("result", [])
            if isinstance(result, list):
                return result
    except Exception as exc:
        logger.warning("VictoriaMetrics instant query failed (%s): %s", query[:60], exc)
    return []


def _vm_range_query(
    query: str,
    start: datetime,
    end: datetime,
    step: str,
) -> list[dict[str, Any]]:
    """Execute a synchronous PromQL range query against VictoriaMetrics.

    Returns an empty list on any error so callers always get a safe value.
    """
    try:
        resp = httpx.get(
            f"{_VICTORIAMETRICS_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "step": step,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "success":
            result = data.get("data", {}).get("result", [])
            if isinstance(result, list):
                return result
    except Exception as exc:
        logger.warning("VictoriaMetrics range query failed (%s): %s", query[:60], exc)
    return []


def _get_onu_status_trend(db: Session, hours: int = 24) -> dict[str, Any]:
    """Return the current binary ONT rollup in chart-compatible shape."""
    from app.services.network_monitoring import get_onu_status_summary

    now = datetime.now(UTC)
    summary = get_onu_status_summary(db)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    working = float(summary["working"])
    not_working = float(summary["not_working"])

    return {
        "timestamps": [timestamp],
        "working": [working],
        "not_working": [not_working],
        "low_signal": [float(summary["low_signal"])],
        "has_data": bool(summary["total"]),
        "source": "inventory",
        "hours": hours,
    }


def _format_bps_simple(bps: float) -> str:
    """Human-readable bps formatting."""
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.2f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} Kbps"
    return f"{bps:.0f} bps"


def _get_bandwidth_summary() -> dict[str, Any]:
    """Fetch aggregate bandwidth summary from VictoriaMetrics.

    Returns a dict with total_rx, total_tx (formatted), top_users list,
    and has_data boolean.  Gracefully returns zeros when VM has no data.
    """
    # Total aggregate throughput
    rx_results = _vm_instant_query("sum(bandwidth_rx_bps_avg)")
    tx_results = _vm_instant_query("sum(bandwidth_tx_bps_avg)")

    total_rx = 0.0
    total_tx = 0.0
    if rx_results and rx_results[0].get("value"):
        total_rx = float(rx_results[0]["value"][1])
    if tx_results and tx_results[0].get("value"):
        total_tx = float(tx_results[0]["value"][1])

    has_data = total_rx > 0 or total_tx > 0

    # Top 5 consumers
    top_users: list[dict[str, Any]] = []
    if has_data:
        top_query = (
            "topk(5, sum by (subscription_id) "
            "(bandwidth_rx_bps_avg + bandwidth_tx_bps_avg))"
        )
        top_results = _vm_instant_query(top_query)
        for r in top_results:
            metric = r.get("metric", {})
            value = r.get("value", [0, 0])
            total_bps = float(value[1]) if len(value) > 1 else 0.0
            top_users.append(
                {
                    "subscription_id": metric.get("subscription_id", "unknown"),
                    "total_bps": total_bps,
                    "total_formatted": _format_bps_simple(total_bps),
                }
            )

    return {
        "total_rx": total_rx,
        "total_tx": total_tx,
        "total_rx_formatted": _format_bps_simple(total_rx),
        "total_tx_formatted": _format_bps_simple(total_tx),
        "top_users": top_users,
        "has_data": has_data,
    }


def _get_nas_throughput_summary(db: Session) -> dict[str, Any]:
    """Fetch per-NAS throughput summary.

    Cross-references NAS IPs from VictoriaMetrics with the nas_devices
    table for friendly names.
    """
    from app.models.catalog import NasDevice

    # Per-NAS aggregate
    rx_results = _vm_instant_query("sum by (nas_device_id) (bandwidth_rx_bps_avg)")
    tx_results = _vm_instant_query("sum by (nas_device_id) (bandwidth_tx_bps_avg)")

    has_data = bool(rx_results) or bool(tx_results)

    # Build lookup of rx/tx by nas_device_id
    nas_rx: dict[str, float] = {}
    nas_tx: dict[str, float] = {}
    all_nas_ids: set[str] = set()
    for r in rx_results:
        nas_id = r.get("metric", {}).get("nas_device_id", "")
        val = float(r.get("value", [0, 0])[1]) if r.get("value") else 0.0
        nas_rx[nas_id] = val
        all_nas_ids.add(nas_id)
    for r in tx_results:
        nas_id = r.get("metric", {}).get("nas_device_id", "")
        val = float(r.get("value", [0, 0])[1]) if r.get("value") else 0.0
        nas_tx[nas_id] = val
        all_nas_ids.add(nas_id)

    # Resolve NAS names from DB
    nas_names: dict[str, str] = {}
    if all_nas_ids:
        devices = db.query(NasDevice).filter(NasDevice.id.in_(list(all_nas_ids))).all()
        for d in devices:
            nas_names[str(d.id)] = d.name or str(d.id)[:8]

    items: list[dict[str, Any]] = []
    for nas_id in sorted(all_nas_ids):
        rx = nas_rx.get(nas_id, 0.0)
        tx = nas_tx.get(nas_id, 0.0)
        items.append(
            {
                "nas_id": nas_id,
                "name": nas_names.get(nas_id, nas_id[:8] if nas_id else "Unknown"),
                "rx_bps": rx,
                "tx_bps": tx,
                "rx_formatted": _format_bps_simple(rx),
                "tx_formatted": _format_bps_simple(tx),
            }
        )

    # Sort by total throughput descending
    items.sort(key=lambda x: x["rx_bps"] + x["tx_bps"], reverse=True)

    return {
        "items": items,
        "has_data": has_data,
    }


def _get_onu_auth_trend(db: Session, days: int = 30) -> dict[str, Any]:
    """Count new ONT registrations per day for a bar chart.

    Queries OntUnit.created_at grouped by date for the last N days.
    """
    from datetime import date

    from sqlalchemy import func

    from app.models.network import OntUnit

    cutoff = datetime.now(UTC) - timedelta(days=days)

    rows = (
        db.query(
            func.date(OntUnit.created_at).label("day"),
            func.count().label("cnt"),
        )
        .filter(OntUnit.created_at >= cutoff)
        .group_by("day")
        .order_by("day")
        .all()
    )

    labels: list[str] = []
    values: list[int] = []
    for row in rows:
        day = row.day
        if isinstance(day, datetime):
            label = day.date().isoformat()
        elif isinstance(day, date):
            label = day.isoformat()
        elif isinstance(day, str):
            label = day
        else:
            label = ""
        labels.append(label)
        values.append(int(row.cnt or 0))

    return {
        "labels": labels,
        "values": values,
        "has_data": bool(values),
    }


# Network entity types for the activity feed
_NETWORK_ENTITY_TYPES = (
    "olt",
    "ont",
    "pon_port",
    "vlan",
    "ip_pool",
    "ip_block",
    "network_device",
    "network_zone",
    "core_device",
    "nas",
    "splitter",
    "fdh_cabinet",
    "pop_site",
)

# Action → human-readable label
_ACTION_DISPLAY: dict[str, str] = {
    "create": "Created",
    "update": "Updated",
    "delete": "Deleted",
    "reboot": "Rebooted",
    "factory_reset": "Factory Reset",
    "set_wifi_ssid": "WiFi SSID Changed",
    "set_wifi_password": "WiFi Password Changed",  # nosec
    "toggle_lan_port": "LAN Port Toggled",
    "assign": "Assigned",
    "unassign": "Unassigned",
    "activate": "Activated",
    "deactivate": "Deactivated",
}


def get_site_reachability(db: Session) -> list[dict[str, Any]]:
    """Group owner-resolved device operation by management subnet."""
    from sqlalchemy import select as sa_select

    from app.models.network_monitoring import NetworkDevice
    from app.services.device_operational_status import annotate_operational_status

    devices = list(
        db.scalars(
            sa_select(NetworkDevice).where(NetworkDevice.is_active.is_(True))
        ).all()
    )
    annotate_operational_status(devices)

    sites: dict[str, dict[str, Any]] = {}
    for d in devices:
        if not d.mgmt_ip:
            continue
        octets = d.mgmt_ip.split(".")
        if len(octets) < 3:
            continue
        subnet = f"{octets[0]}.{octets[1]}.0.0/16"
        if subnet not in sites:
            sites[subnet] = {
                "subnet": subnet,
                "name": "",
                "total": 0,
                "working": 0,
                "not_working": 0,
                "impaired": 0,
            }
        s = sites[subnet]
        s["total"] += 1
        operational = cast(Any, d).operational
        if operational.status == "working":
            s["working"] += 1
            if operational.impaired:
                s["impaired"] += 1
        else:
            s["not_working"] += 1

    subnet_names = {
        "172.16.0.0/16": "Abuja Management",
        "172.20.0.0/16": "OLT Management",
        "172.21.0.0/16": "Lagos Management",
        "102.220.0.0/16": "Public Edge (Lagos/Abuja)",
        "160.119.0.0/16": "Core Infrastructure",
    }
    result = []
    for subnet, data in sorted(
        sites.items(), key=lambda x: x[1]["total"], reverse=True
    ):
        data["name"] = subnet_names.get(subnet, subnet)
        pct = round((data["working"] / data["total"]) * 100) if data["total"] > 0 else 0
        data["working_pct"] = pct
        result.append(data)
    return result


def _get_network_activity_feed(db: Session, limit: int = 15) -> list[dict[str, Any]]:
    """Fetch recent audit events for network-related entities."""
    from app.models.audit import AuditEvent

    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.entity_type.in_(_NETWORK_ENTITY_TYPES))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )

    items: list[dict[str, Any]] = []
    for ev in events:
        action_label = _ACTION_DISPLAY.get(
            ev.action, ev.action.replace("_", " ").title()
        )
        items.append(
            {
                "title": f"{ev.entity_type.replace('_', ' ').title()} {action_label}",
                "entity_type": ev.entity_type,
                "entity_id": ev.entity_id,
                "action": ev.action,
                "occurred_at": ev.occurred_at,
                "actor_type": ev.actor_type.value if ev.actor_type else "system",
                "is_success": ev.is_success,
            }
        )

    return items


def _get_device_health_page(
    db: Session,
    query: str | None = None,
    *,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[dict], dict[str, int | bool]]:
    """Build a device health summary with CPU, memory, temperature, uptime.

    Queries the latest DeviceMetric for each active device.
    Returns a list of dicts suitable for the monitoring dashboard table.
    """
    from sqlalchemy import func, select

    from app.models.network_monitoring import (
        DeviceMetric,
        MetricType,
        NetworkDevice,
    )
    from app.services.device_operational_status import (
        derive_operational_status,
        warmer_is_stale,
    )
    from app.services.web_network_core_devices_forms import _format_uptime_short

    devices_query = db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True))
    term = (query or "").strip()
    if term:
        like = f"%{term}%"
        devices_query = devices_query.filter(
            (NetworkDevice.name.ilike(like))
            | (NetworkDevice.hostname.ilike(like))
            | (NetworkDevice.mgmt_ip.ilike(like))
            | (NetworkDevice.vendor.ilike(like))
        )
    per_page = min(max(int(per_page or 50), 10), 100)
    total = devices_query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    current_page = min(max(page, 1), total_pages)
    devices = (
        devices_query.order_by(NetworkDevice.name.asc())
        .offset((current_page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    pagination: dict[str, int | bool] = {
        "page": current_page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": current_page > 1,
        "has_next": current_page < total_pages,
    }

    if not devices:
        return [], pagination

    metric_fields = [
        (MetricType.cpu, "cpu"),
        (MetricType.memory, "memory"),
        (MetricType.temperature, "temperature"),
        (MetricType.uptime, "uptime"),
    ]

    # Latest value per (device, metric_type) in ONE windowed query instead of
    # four queries per device — that N+1 fan-out (~400 queries) serialized to
    # ~37s and dominated the monitoring dashboard load.
    device_ids = [device.id for device in devices]
    ranked = (
        select(
            DeviceMetric.device_id.label("device_id"),
            DeviceMetric.metric_type.label("metric_type"),
            DeviceMetric.value.label("value"),
            func.row_number()
            .over(
                partition_by=(DeviceMetric.device_id, DeviceMetric.metric_type),
                order_by=DeviceMetric.recorded_at.desc(),
            )
            .label("rn"),
        )
        .where(
            DeviceMetric.device_id.in_(device_ids),
            DeviceMetric.metric_type.in_([mt for mt, _ in metric_fields]),
        )
        .subquery()
    )
    latest_by_key: dict[tuple, float] = {}
    for device_id, metric_type, value, _rn in db.execute(
        select(
            ranked.c.device_id, ranked.c.metric_type, ranked.c.value, ranked.c.rn
        ).where(ranked.c.rn == 1)
    ).all():
        if value is not None:
            latest_by_key[(device_id, metric_type)] = round(float(value), 1)

    results = []
    warm_stale = warmer_is_stale()
    for device in devices:
        operational = derive_operational_status(device, warm_stale=warm_stale)
        row: dict = {
            "id": str(device.id),
            "name": device.name or str(device.id)[:8],
            "ip": str(device.mgmt_ip or ""),
            "mgmt_ip": str(device.mgmt_ip or ""),
            "status": operational.status,
            "status_reason": operational.reason,
            "status_presentation": operational.presentation,
            "vendor": str(device.vendor or ""),
            "health_status": None,
            "max_concurrent_subscribers": getattr(
                device, "max_concurrent_subscribers", None
            ),
            "current_subscriber_count": getattr(
                device, "current_subscriber_count", None
            ),
            "last_ping_at": device.last_ping_at,
            "last_snmp_at": device.last_snmp_at,
            "cpu": None,
            "memory": None,
            "temperature": None,
            "uptime": None,
        }
        for mt, field in metric_fields:
            value = latest_by_key.get((device.id, mt))
            if value is not None:
                row[field] = (
                    _format_uptime_short(int(value)) if field == "uptime" else value
                )
        results.append(row)

    return results, pagination


def _get_device_health_table(db: Session, query: str | None = None) -> list[dict]:
    """Compatibility wrapper for callers that only need the first 100 rows."""
    rows, _pagination = _get_device_health_page(db, query=query, page=1, per_page=100)
    return rows


# ── Bulk actions on monitoring devices ────────────────────────────────

_MONITORING_BULK_ACTIONS = frozenset(
    {
        "enable_monitoring",
        "disable_monitoring",
        "enable_notifications",
        "disable_notifications",
        "deactivate",
    }
)

_MAX_BULK = 50
_BULK_ACTION_LABELS: dict[str, str] = {
    "enable_monitoring": "Enable Monitoring",
    "disable_monitoring": "Disable Monitoring",
    "enable_notifications": "Enable Notifications",
    "disable_notifications": "Disable Notifications",
    "deactivate": "Deactivate",
}


def execute_device_bulk_action(
    db: Session,
    device_ids: list[str],
    action: str,
) -> dict[str, Any]:
    """Execute a bulk action on selected monitoring devices.

    Supported actions:
        enable_monitoring   – set ping_enabled=True, snmp_enabled=True
        disable_monitoring  – set ping_enabled=False, snmp_enabled=False
        enable_notifications – set send_notifications=True
        disable_notifications – set send_notifications=False
        deactivate          – soft-delete (is_active=False)

    Returns:
        Stats dict with succeeded/failed/skipped counts and optional error.
    """
    from app.models.network_monitoring import NetworkDevice
    from app.services.common import coerce_uuid

    if action not in _MONITORING_BULK_ACTIONS:
        return {"succeeded": 0, "failed": 0, "skipped": 0, "error": "Invalid action"}

    if not device_ids:
        return {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "error": "No devices selected",
        }

    capped = device_ids[:_MAX_BULK]
    skipped = len(device_ids) - len(capped)
    if skipped:
        logger.warning(
            "Bulk %s: %d device IDs exceeded cap of %d, %d skipped",
            action,
            len(device_ids),
            _MAX_BULK,
            skipped,
        )
    succeeded = 0
    failed = 0

    for raw_id in capped:
        uid = coerce_uuid(raw_id)
        if uid is None:
            logger.warning("Bulk %s: invalid device ID skipped: %s", action, raw_id)
            failed += 1
            continue
        device = db.get(NetworkDevice, uid)
        if not device or (not device.is_active and action != "deactivate"):
            logger.warning("Bulk %s: device %s not found or inactive", action, uid)
            failed += 1
            continue
        if action == "enable_monitoring":
            device.ping_enabled = True
            device.snmp_enabled = True
        elif action == "disable_monitoring":
            device.ping_enabled = False
            device.snmp_enabled = False
        elif action == "enable_notifications":
            device.send_notifications = True
        elif action == "disable_notifications":
            device.send_notifications = False
        elif action == "deactivate":
            from app.services.network_monitoring import set_network_device_active

            set_network_device_active(db, device, False, reason="bulk_deactivate")
        succeeded += 1

    if succeeded > 0:
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "Bulk %s commit failed for %d devices",
                action,
                len(capped),
            )
            return {
                "succeeded": 0,
                "failed": len(capped),
                "skipped": skipped,
                "error": "Database error — no changes were saved. Please try again.",
            }

    return {"succeeded": succeeded, "failed": failed, "skipped": skipped}


def render_bulk_result(stats: dict[str, Any], action: str) -> str:
    """Build the HTML snippet for the bulk action result banner."""
    error = stats.get("error")
    if error:
        return (
            '<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm'
            " text-rose-700 dark:border-rose-800 dark:bg-rose-900/20"
            f' dark:text-rose-400">{escape(str(error))}</div>'
        )

    label = escape(_BULK_ACTION_LABELS.get(action, action))
    skipped_text = (
        f", {stats['skipped']} skipped (max 50)" if stats.get("skipped") else ""
    )
    body = (
        f"Bulk <strong>{label}</strong>: {stats['succeeded']} succeeded,"
        f" {stats['failed']} failed{skipped_text}."
    )

    if stats["succeeded"] == 0 and stats["failed"] > 0:
        return (
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm'
            " text-amber-700 dark:border-amber-800 dark:bg-amber-900/20"
            f' dark:text-amber-400">{body}</div>'
        )
    return (
        '<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm'
        " text-emerald-700 dark:border-emerald-800 dark:bg-emerald-900/20"
        f' dark:text-emerald-400">{body}</div>'
    )
