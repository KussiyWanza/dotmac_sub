"""Infrastructure performance & SLA dashboards — service layer.

Powers ``/admin/network/performance`` (see INFRASTRUCTURE_SLA_PERFORMANCE.md).
Three surfaces, all built from existing data:

* **ranking** — worst-performer table per tier (BTS / OLT / PON / AP) over a
  window, sorted by uptime % ascending, enriched with affected-subscriber count
  and incident count. Reuses ``network_monitoring.uptime_report`` as the engine.
* **wallboard** — binary working/not-working counts per tier from timestamped
  cached observations + ONT operation (no live fan-out on the request path).
* **sla** — uptime % vs target with PASS/BREACH + MTTR (see Phase 2b).

Tiers map onto the engine's ``group_by`` dimensions:

    bts -> pop_site   olt -> device(OLT-only)   ap -> access_point   pon -> pon
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.network import OntAssignment
from app.models.network_monitoring import (
    Alert,
    AlertStatus,
    DeviceType,
    MetricType,
    NetworkDevice,
)
from app.schemas.network_monitoring import UptimeReportItem, UptimeReportRequest
from app.services import network_monitoring as network_monitoring_service

# tier key -> (label, engine group_by)
TIERS: dict[str, tuple[str, str]] = {
    "bts": ("Basestation (BTS)", "pop_site"),
    "olt": ("OLT", "device"),
    "ap": ("Access point", "access_point"),
    "pon": ("OLT PON port", "pon"),
}

# tier key -> AvailabilitySnapshot.element_type (for trend drill-down). PON
# snapshots are keyed by port; OLT and AP are both device-backed.
TIER_ELEMENT_TYPE: dict[str, str] = {
    "bts": "pop_site",
    "olt": "device",
    "ap": "device",
    "pon": "pon_port",
}

# window key -> (label, timedelta)
WINDOWS: dict[str, tuple[str, timedelta]] = {
    "24h": ("Last 24 hours", timedelta(hours=24)),
    "7d": ("Last 7 days", timedelta(days=7)),
    "30d": ("Last 30 days", timedelta(days=30)),
}


def _now() -> datetime:
    return datetime.now(UTC)


def resolve_window(window_key: str) -> tuple[str, datetime, datetime]:
    """Return (normalized_key, period_start, period_end) for a window key."""
    key = window_key if window_key in WINDOWS else "7d"
    _, delta = WINDOWS[key]
    end = _now()
    return key, end - delta, end


def _olt_device_ids(db: Session) -> set:
    return {
        r[0]
        for r in db.query(NetworkDevice.id)
        .filter(NetworkDevice.matched_device_type == "olt")
        .all()
    }


def _incident_counts_by_device(
    db: Session, period_start: datetime, period_end: datetime
) -> dict[str, int]:
    """Number of uptime alerts triggered per device within the window."""
    rows = (
        db.query(Alert.device_id, func.count(Alert.id))
        .filter(
            Alert.metric_type == MetricType.uptime,
            Alert.device_id.isnot(None),
            Alert.triggered_at >= period_start,
            Alert.triggered_at <= period_end,
        )
        .group_by(Alert.device_id)
        .all()
    )
    return {str(device_id): int(count) for device_id, count in rows}


def _mttr_by_device(
    db: Session, period_start: datetime, period_end: datetime
) -> dict[str, tuple[float, int]]:
    """device_id -> (total_repair_seconds, resolved_count) for uptime alerts
    that both triggered and resolved within the window. MTTR = total/count."""
    rows = (
        db.query(Alert.device_id, Alert.triggered_at, Alert.resolved_at)
        .filter(
            Alert.metric_type == MetricType.uptime,
            Alert.device_id.isnot(None),
            Alert.status == AlertStatus.resolved,
            Alert.resolved_at.isnot(None),
            Alert.triggered_at >= period_start,
            Alert.resolved_at <= period_end,
        )
        .all()
    )
    acc: dict[str, tuple[float, int]] = {}
    for device_id, triggered_at, resolved_at in rows:
        if triggered_at is None or resolved_at is None:
            continue
        secs = (resolved_at - triggered_at).total_seconds()
        if secs < 0:
            continue
        key = str(device_id)
        total, count = acc.get(key, (0.0, 0))
        acc[key] = (total + secs, count + 1)
    return acc


def _device_meta(db: Session) -> dict[str, dict]:
    """device_id -> {subs, pop_site_id} for blast-radius enrichment (cheap;
    uses the maintained current_subscriber_count, not a per-row BFS)."""
    rows = db.query(
        NetworkDevice.id,
        NetworkDevice.current_subscriber_count,
        NetworkDevice.pop_site_id,
    ).all()
    return {
        str(did): {"subs": int(subs or 0), "pop_site_id": pid}
        for did, subs, pid in rows
    }


def _pon_subscriber_counts(db: Session) -> dict[str, int]:
    rows = (
        db.query(OntAssignment.pon_port_id, func.count(OntAssignment.id))
        .filter(
            OntAssignment.pon_port_id.isnot(None),
            OntAssignment.active.is_(True),
            OntAssignment.subscriber_id.isnot(None),
        )
        .group_by(OntAssignment.pon_port_id)
        .all()
    )
    return {str(pid): int(count) for pid, count in rows}


def _site_device_index(device_meta: dict[str, dict]) -> dict[str, list[str]]:
    """pop_site_id -> [device_id]. Built once so per-row BTS lookups are O(1)
    instead of rescanning every device per ranked site (O(sites×devices))."""
    index: dict[str, list[str]] = {}
    for did, m in device_meta.items():
        pid = m.get("pop_site_id")
        if pid is not None:
            index.setdefault(str(pid), []).append(did)
    return index


def _row_device_ids(
    tier: str, item: UptimeReportItem, site_index: dict[str, list[str]]
) -> list[str]:
    """The NetworkDevice ids backing a ranked row (for alert-derived metrics).
    PON rows have no device-keyed uptime alerts, so they map to no devices."""
    gid = str(item.group_id) if item.group_id else None
    if not gid:
        return []
    if tier in ("olt", "ap"):
        return [gid]
    if tier == "bts":
        return site_index.get(gid, [])
    return []


def _sla_status(uptime_percent, target: float) -> str:
    if uptime_percent is None:
        return "no_data"
    return "pass" if float(uptime_percent) >= target else "breach"


def ranking(db: Session, tier: str, window_key: str, *, limit: int = 100) -> dict:
    """Worst-performer ranking for a tier+window (worst uptime first)."""
    if tier not in TIERS:
        tier = "bts"
    label, group_by = TIERS[tier]
    window_key, period_start, period_end = resolve_window(window_key)

    report = network_monitoring_service.uptime_report(
        db,
        UptimeReportRequest(
            period_start=period_start,
            period_end=period_end,
            group_by=group_by,
        ),
    )
    items = list(report.items)
    if tier == "olt":
        olt_ids = _olt_device_ids(db)
        items = [it for it in items if it.group_id in olt_ids]

    from app.config import settings

    target = float(settings.infra_sla_target_percent)
    device_meta = _device_meta(db)
    site_index = _site_device_index(device_meta)
    incidents = _incident_counts_by_device(db, period_start, period_end)
    mttr_map = _mttr_by_device(db, period_start, period_end)
    pon_subs = _pon_subscriber_counts(db) if tier == "pon" else {}

    rows: list[dict] = []
    for it in items:
        dev_ids = _row_device_ids(tier, it, site_index)
        if tier == "pon":
            affected = pon_subs.get(str(it.group_id) if it.group_id else "", 0)
        else:
            affected = sum(device_meta.get(d, {}).get("subs", 0) for d in dev_ids)
        incident_count = sum(incidents.get(d, 0) for d in dev_ids)
        mttr_total = sum(mttr_map.get(d, (0.0, 0))[0] for d in dev_ids)
        mttr_count = sum(mttr_map.get(d, (0.0, 0))[1] for d in dev_ids)
        mttr_seconds = mttr_total / mttr_count if mttr_count else None
        rows.append(
            {
                "id": it.group_id,
                "name": it.name,
                "uptime_percent": it.uptime_percent,
                "downtime_seconds": it.downtime_seconds,
                "downtime_human": _humanize_seconds(it.downtime_seconds),
                "device_count": it.device_count,
                "incident_count": incident_count,
                "affected_subscribers": affected,
                "mttr_seconds": int(mttr_seconds) if mttr_seconds is not None else None,
                "mttr_human": (
                    _humanize_seconds(int(mttr_seconds))
                    if mttr_seconds is not None
                    else None
                ),
                "sla_status": _sla_status(it.uptime_percent, target),
                "derived": it.derived,
            }
        )

    # Worst first: unknown (None uptime) sorts last; among known, lowest %, then
    # most affected subscribers as the tie-breaker.
    rows.sort(
        key=lambda r: (
            r["uptime_percent"] is None,
            r["uptime_percent"] if r["uptime_percent"] is not None else 0,
            -r["affected_subscribers"],
        )
    )
    return {
        "tier": tier,
        "tier_label": label,
        "window_key": window_key,
        "window_label": WINDOWS[window_key][0],
        "period_start": period_start,
        "period_end": period_end,
        "rows": rows[:limit],
        "total": len(rows),
        "derived": group_by == "pon",
        "sla_target": target,
        "breach_count": sum(1 for r in rows if r["sla_status"] == "breach"),
        "element_type": TIER_ELEMENT_TYPE[tier],
    }


_STATE_WORKING = "working"
_STATE_NOT_WORKING = "not_working"
_STATE_ORDER = {
    _STATE_NOT_WORKING: 2,
    _STATE_WORKING: 1,
}


def _device_state(
    status,
    live_status: str | None,
    live_status_at: datetime | None,
    ping_enabled: bool,
    last_ping_ok: bool | None,
    last_ping_at: datetime | None,
    snmp_enabled: bool,
    last_snmp_ok: bool | None,
    last_snmp_at: datetime | None,
    *,
    warm_stale: bool,
) -> str:
    """Coarse wallboard bucket for a device, via the shared operational-status
    reader so the wallboard and Network Devices page agree."""
    from types import SimpleNamespace

    from app.services.device_operational_status import derive_operational_status

    op = derive_operational_status(
        SimpleNamespace(
            status=status,
            live_status=live_status,
            live_status_at=live_status_at,
            ping_enabled=ping_enabled,
            last_ping_ok=last_ping_ok,
            last_ping_at=last_ping_at,
            snmp_enabled=snmp_enabled,
            last_snmp_ok=last_snmp_ok,
            last_snmp_at=last_snmp_at,
        ),
        warm_stale=warm_stale,
    )
    return op.status


def _empty_card(tier: str, label: str) -> dict:
    return {
        "tier": tier,
        "label": label,
        _STATE_WORKING: 0,
        _STATE_NOT_WORKING: 0,
        "total": 0,
    }


def _bump(card: dict, state: str) -> None:
    card[state] = card.get(state, 0) + 1
    card["total"] += 1


def wallboard(db: Session) -> dict:
    """Binary operational counts per tier from the canonical resolver."""
    devices = (
        db.query(
            NetworkDevice.id,
            NetworkDevice.device_type,
            NetworkDevice.matched_device_type,
            NetworkDevice.pop_site_id,
            NetworkDevice.live_status,
            NetworkDevice.live_status_at,
            NetworkDevice.ping_enabled,
            NetworkDevice.last_ping_ok,
            NetworkDevice.last_ping_at,
            NetworkDevice.snmp_enabled,
            NetworkDevice.last_snmp_ok,
            NetworkDevice.last_snmp_at,
            NetworkDevice.status,
        )
        .filter(NetworkDevice.is_active.is_(True))
        .all()
    )

    from app.services.device_operational_status import warmer_is_stale

    warm_stale = warmer_is_stale()
    olt_card = _empty_card("olt", TIERS["olt"][0])
    ap_card = _empty_card("ap", TIERS["ap"][0])
    # BTS: roll each pop_site up to the worst state among its devices.
    site_worst: dict[str, str] = {}
    for (
        did,
        dtype,
        matched,
        pop_site_id,
        live_status,
        live_status_at,
        ping_enabled,
        last_ping_ok,
        last_ping_at,
        snmp_enabled,
        last_snmp_ok,
        last_snmp_at,
        status,
    ) in devices:
        state = _device_state(
            status,
            live_status,
            live_status_at,
            ping_enabled,
            last_ping_ok,
            last_ping_at,
            snmp_enabled,
            last_snmp_ok,
            last_snmp_at,
            warm_stale=warm_stale,
        )
        if matched == "olt":
            _bump(olt_card, state)
        if dtype == DeviceType.access_point:
            _bump(ap_card, state)
        if pop_site_id is not None:
            key = str(pop_site_id)
            prev = site_worst.get(key)
            if prev is None or _STATE_ORDER[state] > _STATE_ORDER[prev]:
                site_worst[key] = state

    bts_card = _empty_card("bts", TIERS["bts"][0])
    for state in site_worst.values():
        _bump(bts_card, state)

    pon_card = _pon_wallboard_card(db)

    return {"cards": [bts_card, olt_card, pon_card, ap_card]}


def _pon_wallboard_card(db: Session) -> dict:
    """PON tier card: every ONT must be confirmed working for the port to work."""
    from app.models.network import OntUnit, PonPort
    from app.services.network.ont_status import operational_ont_working_clause

    working_count = func.count(case((operational_ont_working_clause(), OntUnit.id)))
    rows = (
        db.query(
            PonPort.id,
            func.count(OntUnit.id),
            working_count,
        )
        .select_from(PonPort)
        .outerjoin(OntUnit, OntUnit.pon_port_id == PonPort.id)
        .filter(PonPort.is_active.is_(True))
        .group_by(PonPort.id)
        .all()
    )
    card = _empty_card("pon", TIERS["pon"][0])
    for _pid, total, working in rows:
        total = int(total or 0)
        working = int(working or 0)
        state = _STATE_WORKING if total > 0 and working == total else _STATE_NOT_WORKING
        _bump(card, state)
    return card


def build_ranking_csv(db: Session, tier: str, window_key: str) -> str:
    """CSV of the worst-performer ranking for export."""
    import csv
    import io

    data = ranking(db, tier, window_key, limit=100_000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "name",
            "uptime_percent",
            "downtime_seconds",
            "incident_count",
            "mttr_seconds",
            "affected_subscribers",
            "sla_status",
            "derived",
        ]
    )
    for r in data["rows"]:
        writer.writerow(
            [
                r["name"],
                "" if r["uptime_percent"] is None else r["uptime_percent"],
                r["downtime_seconds"],
                r["incident_count"],
                "" if r["mttr_seconds"] is None else r["mttr_seconds"],
                r["affected_subscribers"],
                r["sla_status"],
                r["derived"],
            ]
        )
    return buf.getvalue()


def _humanize_seconds(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0m"
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"
