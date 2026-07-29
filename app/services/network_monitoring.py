from __future__ import annotations

import builtins
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeVar, cast

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.network import FdhCabinet, OLTDevice, OntUnit, PonPort
from app.models.network_monitoring import (
    Alert,
    AlertEvent,
    AlertOperator,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    DeviceInterface,
    DeviceMetric,
    DeviceType,
    InterfaceStatus,
    MetricType,
    NetworkDevice,
    PopSite,
)
from app.schemas.network_monitoring import (
    AlertAcknowledgeRequest,
    AlertResolveRequest,
    AlertRuleBulkUpdateRequest,
    AlertRuleCreate,
    AlertRuleUpdate,
    DeviceInterfaceCreate,
    DeviceInterfaceUpdate,
    DeviceMetricCreate,
    DeviceMetricUpdate,
    NetworkDeviceCreate,
    NetworkDeviceUpdate,
    PopSiteCreate,
    PopSiteUpdate,
    UptimeReportItem,
    UptimeReportRequest,
    UptimeReportResponse,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _commit(db: Session, action: Callable[[], T]) -> T:
    """Run a monitoring mutation and own the transaction boundary."""
    try:
        result = action()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ── Device admission lifecycle (owned by network.monitoring_inventory) ───────
#
# The single transition every ``NetworkDevice.is_active`` change goes through.
# Before it existed, three callers each flipped the flag directly and each got
# only half a deactivation: the row left the pollable set (so the topology
# warmer stopped visiting it) but kept whatever ``live_status`` it last held,
# forever. A device frozen at ``up`` then vetoed outage detection
# (``health_classifier.classify_node``: mgmt up + nobody online = "service
# fault, NOT an area outage"), so no incident opened and the customer surface
# reported the cabinet healthy indefinitely.
#
# Deactivation is therefore one atomic slice, not a flag:
#   1. leave polling eligibility (``is_active``);
#   2. DECAY the derived reachability cache to ``unknown`` — withdraw the
#      unsupported assertion rather than freeze it;
#   3. stay visible in inventory, marked inactive (``collect_devices`` no longer
#      filters the flag, so the row cannot vanish from the staff ledger and get
#      pruned out of ``device_projections``).
#
# (2) is the owner writing a *derived cache* it is retiring, not an
# observation: it asserts nothing about reachability, it withdraws an
# assertion nothing is checking any more. Reachability observations still flow
# only from the poller, and they never drive inventory lifecycle.

_INACTIVE_LIVE_STATUS = "unknown"


def set_network_device_active(
    db: Session,
    device: NetworkDevice,
    active: bool,
    *,
    reason: str,
    now: datetime | None = None,
) -> NetworkDevice:
    """Transition a monitoring device's admission state. The only writer.

    ``reason`` is a short machine string naming the caller (``manual_delete``,
    ``router_delete_cascade``, ``router_inventory_sync``) so the log line
    identifies which upstream drove the transition.

    Idempotent: re-applying the current state still normalises the derived
    cache, so a row that was deactivated before this transition existed is
    repaired the next time anything touches it.
    """
    stamp = now or datetime.now(UTC)
    changed = bool(device.is_active) != bool(active)

    # Blast radius is read BEFORE the flag flips: the authoritative forwarding
    # graph excludes inactive devices, so once the write lands the node has no
    # adjacency left and the downstream arm silently reads zero.
    impact = _stranded_customer_impact(db, device) if changed and not active else None

    device.is_active = bool(active)

    # Both directions decay: deactivation removes the device from polling, and
    # reactivation must not resurrect a pre-deactivation verdict. Either way the
    # next warm/poll cycle is what re-establishes a real state.
    if device.live_status not in (None, _INACTIVE_LIVE_STATUS):
        device.live_status = _INACTIVE_LIVE_STATUS
        device.live_status_at = stamp

    db.flush()
    if changed:
        logger.info(
            "network device %s admission -> %s (reason=%s)",
            device.id,
            "active" if active else "inactive",
            reason,
        )
        _sync_stranded_customer_alert(
            db, device, active=active, reason=reason, impact=impact
        )
    return device


# ── Stranded-customer integrity alert (admin-facing; NOT an incident) ───────
#
# A device can be deactivated while customers are still attached to it. The
# sweep that opens customer-facing incidents (``outage_reconcile``) only visits
# pollable nodes, so once the row leaves that set nothing re-examines it and
# the stranding is invisible until someone calls in.
#
# The rejected fix was to make inventory absence open an outage incident. An
# unpolled device supports NO reachability verdict — that is precisely why this
# slice makes deactivation classify as ``UNKNOWN`` rather than ``NODE_OUTAGE``
# — so deriving "outage" from an administrative flag would reintroduce the same
# boundary violation inverted. It would also fire on every routine decommission
# until operators learned to ignore the whole class.
#
# What this is instead: an admin-facing DATA-INTEGRITY alert raised at the
# deactivation transition. It is a statement about the inventory record ("this
# device left the monitored set with customers still hanging off it"), not
# about reachability. It fires once, at the transition, with a known blast
# radius; it never touches ``OutageIncident`` and never reaches a customer
# surface.
#
# It is raised on the deactivation edge only, and the reactivation edge
# resolves it — deactivation is otherwise effectively one-way (the router
# mirror in ``monitoring_metrics`` iterates only active routers), which is
# exactly why an accidental deactivation has to announce itself at the moment
# it happens rather than wait for a sweep that will never come.

_ADMISSION_ALERT_PREFIX = "network:device-admission:"


def _stranded_customer_fingerprint(device: NetworkDevice) -> str:
    """Stable per-device fingerprint — the dedupe key.

    ``admin_alerts`` upserts on fingerprint, so repeated deactivation
    transitions on the same device update one row instead of stacking, and
    ``sync_alert`` re-notifies only on open/escalation, never on an update.
    """
    return f"{_ADMISSION_ALERT_PREFIX}stranded-customers:{device.id}"


def _stranded_customer_impact(
    db: Session, device: NetworkDevice
) -> dict[str, int] | None:
    """Customers still attached to ``device`` as it leaves the pollable set.

    Read through the existing reverse traversal (``topology.affected``) rather
    than a second one, so the number in the alert is the same number the outage
    console quotes for this node.

    ``attached`` is the node's own attachment arm — the customers whose serving
    node is the one being deactivated, i.e. exactly those who lose incident
    coverage. ``affected`` is the wider failure domain (node plus downstream
    access nodes) and is reported for context only: those downstream nodes stay
    active, stay polled, and keep their own coverage.

    Returns ``None`` when the read fails. An advisory integrity alert must
    never be able to block the lifecycle transition it observes.
    """
    from app.services.topology.affected import affected_customers

    try:
        result = affected_customers(db, node=device)
        by_node = result.get("subscriptions_by_node") or {}
        attached = len(by_node.get(device.id) or ())
        return {
            "attached": attached,
            "affected": max(int(result.get("count") or 0), attached),
        }
    except Exception:
        logger.exception(
            "stranded-customer impact read failed for network device %s", device.id
        )
        return None


def _sync_stranded_customer_alert(
    db: Session,
    device: NetworkDevice,
    *,
    active: bool,
    reason: str,
    impact: dict[str, int] | None,
) -> str | None:
    """Raise (deactivation) or clear (reactivation) the integrity alert.

    Inside the caller's transaction, like every other domain-owned alert — the
    admission transition owns its commit boundary and this rides along with it.
    """
    fingerprint = _stranded_customer_fingerprint(device)
    try:
        from app.services import admin_alerts

        if active:
            # Re-admission repairs the condition: the device is polled again,
            # so any outstanding alert for it stops being true.
            cleared = admin_alerts.resolve_alert_by_fingerprint(
                db, fingerprint, mark_notifications_read=True
            )
            return None if cleared is None else "resolved"

        if impact is None:
            return None
        attached = impact["attached"]
        if attached <= 0:
            # Routine decommission of an unattached device. The silence is the
            # feature: an alert that fires on every retirement is an alert
            # operators are trained to skip past.
            return None

        affected = impact["affected"]
        wider = f" ({affected} in its failure domain)" if affected > attached else ""
        name = device.name or str(device.id)
        summary = (
            f"{name} left the monitored set (reason={reason}) with {attached} "
            f"active subscription(s) still attached to it{wider}. Nothing polls "
            "it now, so no outage incident can open for those customers. "
            "Re-attach them or re-activate the device."
        )
        return admin_alerts.sync_alert(
            db,
            admin_alerts.AlertFinding(
                fingerprint=fingerprint,
                category="network",
                source="device-admission",
                severity=AlertSeverity.warning,
                title=f"Device deactivated with customers attached: {name}"[:180],
                summary=summary[:255],
                details={
                    "device_id": str(device.id),
                    "device_name": name,
                    "device_type": getattr(device, "device_type", None),
                    "mgmt_ip": getattr(device, "mgmt_ip", None),
                    "pop_site_id": getattr(device, "pop_site_id", None),
                    "reason": reason,
                    "attached_customer_count": attached,
                    "affected_customer_count": affected,
                },
                target_url=f"/admin/network/core-devices/{device.id}",
            ),
        )
    except Exception:
        logger.exception(
            "stranded-customer alert sync failed for network device %s", device.id
        )
        return None


def _round_percent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals.sort(key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _alert_intervals_by_device(
    db: Session, period_start: datetime, period_end: datetime
) -> dict[str, list[tuple[datetime, datetime]]]:
    period_start_utc = cast(datetime, _as_utc(period_start))
    period_end_utc = cast(datetime, _as_utc(period_end))
    alerts = (
        db.query(Alert)
        .filter(Alert.metric_type == MetricType.uptime)
        .filter(Alert.triggered_at <= period_end_utc)
        .filter((Alert.resolved_at.is_(None)) | (Alert.resolved_at >= period_start_utc))
        .all()
    )
    intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    for alert in alerts:
        if not alert.device_id:
            continue
        triggered_at = _as_utc(alert.triggered_at)
        resolved_at = _as_utc(alert.resolved_at)
        if triggered_at is None:
            continue
        start = max(triggered_at, period_start_utc)
        end = min(resolved_at or period_end_utc, period_end_utc)
        if end <= start:
            continue
        key = str(alert.device_id)
        intervals.setdefault(key, []).append((start, end))
    return intervals


def _pon_availability_items(db: Session, window_seconds: int) -> list[UptimeReportItem]:
    """Per-PON-port availability, derived from current working ONTs.

    PON ports have no monitored host and no uptime ``Alert`` source, so unlike the
    other dimensions this is not measured from downtime intervals. We approximate
    a port's health as the fraction of its ONTs the operational owner currently
    resolves as ``working``. Rows are flagged ``derived=True`` so the UI labels
    them estimates, not contractual SLA.
    Ports with no ONTs are reported with ``uptime_percent=None`` (no signal).
    """
    from app.services.network.ont_status import operational_ont_working_clause

    working_count = func.count(case((operational_ont_working_clause(), OntUnit.id)))
    rows = (
        db.query(
            PonPort.id,
            PonPort.name,
            OLTDevice.name.label("olt_name"),
            func.count(OntUnit.id).label("total"),
            working_count.label("working"),
        )
        .select_from(PonPort)
        .join(OLTDevice, OLTDevice.id == PonPort.olt_id)
        .outerjoin(OntUnit, OntUnit.pon_port_id == PonPort.id)
        .filter(PonPort.is_active.is_(True))
        .group_by(PonPort.id, PonPort.name, OLTDevice.name)
        .all()
    )
    items: list[UptimeReportItem] = []
    for pon_id, pon_name, olt_name, total, working in rows:
        total = int(total or 0)
        working = int(working or 0)
        if total == 0:
            uptime_percent = None
            downtime = 0
        else:
            ratio = Decimal(working) / Decimal(total)
            uptime_percent = _round_percent(ratio * Decimal("100.00"))
            downtime = int((Decimal(1) - ratio) * Decimal(window_seconds))
        items.append(
            UptimeReportItem(
                group_by="pon",
                group_id=pon_id,
                name=f"{olt_name} / {pon_name}",
                device_count=total,
                total_seconds=window_seconds,
                downtime_seconds=downtime,
                uptime_percent=uptime_percent,
                derived=True,
            )
        )
    return items


def bandwidth_summary(db: Session, *, window_seconds: int = 600) -> dict[str, float]:
    """Aggregate current downstream throughput across monitored devices.

    Canonical read owner for the overview's live bandwidth figure: sums the most
    recent ``rx_bps`` device metrics within the window. Returns raw bits/second;
    unit formatting (Gbps/Mbps/Kbps) is a presentation concern for the caller.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    total_bps = db.scalar(
        select(func.coalesce(func.sum(DeviceMetric.value), 0)).where(
            DeviceMetric.metric_type == MetricType.rx_bps,
            DeviceMetric.recorded_at > cutoff,
            DeviceMetric.value > 0,
        )
    )
    return {"total_bps": float(total_bps or 0)}


_PON_NAME_PATTERNS = ("%pon%", "%gpon%", "%epon%", "%xgpon%", "%xgs%")


def _pon_interface_clause():
    """SQL clause classifying monitoring interfaces as PON-facing."""
    clauses = []
    for pattern in _PON_NAME_PATTERNS:
        clauses.append(
            func.lower(func.coalesce(DeviceInterface.name, "")).like(pattern)
        )
        clauses.append(
            func.lower(func.coalesce(DeviceInterface.description, "")).like(pattern)
        )
    return or_(*clauses)


def pon_interface_summary(db: Session) -> dict[str, int]:
    """Canonical up/down/unknown counts for PON-facing monitoring interfaces."""
    counts = (
        db.query(
            func.count(DeviceInterface.id).label("total"),
            func.count(DeviceInterface.id)
            .filter(DeviceInterface.status == InterfaceStatus.up)
            .label("up"),
            func.count(DeviceInterface.id)
            .filter(DeviceInterface.status == InterfaceStatus.down)
            .label("down"),
        )
        .join(NetworkDevice, NetworkDevice.id == DeviceInterface.device_id)
        .filter(NetworkDevice.is_active.is_(True))
        .filter(_pon_interface_clause())
        .one()
    )
    total = counts.total or 0
    up = counts.up or 0
    down = counts.down or 0
    return {"up": up, "down": down, "unknown": total - up - down, "total": total}


def pon_outages(db: Session, limit: int = 10) -> list[dict]:
    """PON-facing interfaces currently down, most recently changed first."""
    rows = (
        db.query(
            DeviceInterface.id,
            DeviceInterface.name,
            DeviceInterface.description,
            DeviceInterface.updated_at,
            NetworkDevice.id.label("device_id"),
            NetworkDevice.name.label("device_name"),
        )
        .join(NetworkDevice, NetworkDevice.id == DeviceInterface.device_id)
        .filter(NetworkDevice.is_active.is_(True))
        .filter(_pon_interface_clause())
        .filter(DeviceInterface.status == InterfaceStatus.down)
        .order_by(DeviceInterface.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "description": row.description or "",
            "olt_id": str(row.device_id),
            "olt_name": row.device_name or "Unknown OLT",
            "down_since": row.updated_at,
        }
        for row in rows
    ]


def network_health_summary(
    db: Session,
    *,
    warn_pct: int = 90,
    crit_pct: int = 70,
    fallback_stats: dict | None = None,
) -> dict[str, object]:
    """Fleet-level binary device health and health-ring status.

    Counts come from the canonical device projection. When no OLT rows exist,
    the ring falls back to the complete projected device fleet so a
    monitoring-only deployment still reports health.
    """
    from app.services.device_projection_views import device_projection_stats

    olt_stats = device_projection_stats(db, device_type="olt")
    ont_stats = device_projection_stats(db, device_type="ont")
    olt_total = olt_stats["total"]
    olt_working = olt_stats["working"]
    ont_total = ont_stats["total"]
    ont_working = ont_stats["working"]

    stats = fallback_stats or {}
    if olt_total == 0 and (stats.get("total_count") or 0) > 0:
        olts_total = int(stats["total_count"])
        olts_working = int(stats.get("working_count") or 0)
    else:
        olts_total = olt_total
        olts_working = olt_working

    health_pct = int((olts_working / olts_total) * 100) if olts_total > 0 else 0
    if health_pct >= warn_pct:
        health_status = "healthy"
    elif health_pct >= crit_pct:
        health_status = "warning"
    else:
        health_status = "critical"

    return {
        "olt_total": olt_total,
        "olt_working": olt_working,
        "ont_total": ont_total,
        "ont_working": ont_working,
        "olts_total": olts_total,
        "olts_working": olts_working,
        "health_pct": health_pct,
        "health_status": health_status,
    }


def uptime_report(db: Session, payload: UptimeReportRequest) -> UptimeReportResponse:
    if payload.period_end <= payload.period_start:
        raise HTTPException(
            status_code=400, detail="period_end must be after period_start"
        )
    window_seconds = int((payload.period_end - payload.period_start).total_seconds())
    devices = (
        db.query(NetworkDevice)
        .options(selectinload(NetworkDevice.pop_site))
        .filter(NetworkDevice.is_active.is_(True))
        .all()
    )
    downtime_intervals = _alert_intervals_by_device(
        db, payload.period_start, payload.period_end
    )
    downtime_by_device: dict[str, int] = {}
    for device_id, intervals in downtime_intervals.items():
        merged = _merge_intervals(intervals)
        total = sum(int((end - start).total_seconds()) for start, end in merged)
        downtime_by_device[device_id] = total
    items: list[UptimeReportItem] = []
    group_by = payload.group_by
    if group_by == "device":
        for device in devices:
            downtime = downtime_by_device.get(str(device.id), 0)
            uptime_percent = _round_percent(
                (Decimal(window_seconds - downtime) * Decimal("100.00"))
                / Decimal(window_seconds)
            )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=device.id,
                    name=device.name,
                    device_count=1,
                    total_seconds=window_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "pop_site":
        grouped: dict[str, list[NetworkDevice]] = {}
        for device in devices:
            key = str(device.pop_site_id) if device.pop_site_id else "unknown"
            grouped.setdefault(key, []).append(device)
        for key, grouped_devices in grouped.items():
            device_count = len(grouped_devices)
            total_seconds = window_seconds * device_count
            downtime = sum(
                downtime_by_device.get(str(device.id), 0) for device in grouped_devices
            )
            pop_site = grouped_devices[0].pop_site if grouped_devices else None
            name = pop_site.name if pop_site else "Unknown"
            uptime_percent = None
            if total_seconds > 0:
                uptime_percent = _round_percent(
                    (Decimal(total_seconds - downtime) * Decimal("100.00"))
                    / Decimal(total_seconds)
                )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=grouped_devices[0].pop_site_id
                    if grouped_devices
                    else None,
                    name=name,
                    device_count=device_count,
                    total_seconds=total_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "area":
        grouped_by_region: dict[str, list[NetworkDevice]] = {}
        for device in devices:
            region = device.pop_site.region if device.pop_site else None
            key = region or "Unknown"
            grouped_by_region.setdefault(key, []).append(device)
        for region, grouped_devices in grouped_by_region.items():
            device_count = len(grouped_devices)
            total_seconds = window_seconds * device_count
            downtime = sum(
                downtime_by_device.get(str(device.id), 0) for device in grouped_devices
            )
            uptime_percent = None
            if total_seconds > 0:
                uptime_percent = _round_percent(
                    (Decimal(total_seconds - downtime) * Decimal("100.00"))
                    / Decimal(total_seconds)
                )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=None,
                    name=region,
                    device_count=device_count,
                    total_seconds=total_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "fdh":
        region_to_devices: dict[str, list[NetworkDevice]] = {}
        for device in devices:
            region = device.pop_site.region if device.pop_site else None
            if not region:
                continue
            region_to_devices.setdefault(region, []).append(device)
        fdh_list = (
            db.query(FdhCabinet)
            .options(selectinload(FdhCabinet.region))
            .filter(FdhCabinet.is_active.is_(True))
            .all()
        )
        for fdh in fdh_list:
            region_name = fdh.region.name if fdh.region else None
            grouped_devices = region_to_devices.get(region_name or "", [])
            device_count = len(grouped_devices)
            total_seconds = window_seconds * device_count
            downtime = sum(
                downtime_by_device.get(str(device.id), 0) for device in grouped_devices
            )
            uptime_percent = None
            if total_seconds > 0:
                uptime_percent = _round_percent(
                    (Decimal(total_seconds - downtime) * Decimal("100.00"))
                    / Decimal(total_seconds)
                )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=fdh.id,
                    name=fdh.name,
                    device_count=device_count,
                    total_seconds=total_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "access_point":
        for device in devices:
            if device.device_type != DeviceType.access_point:
                continue
            downtime = downtime_by_device.get(str(device.id), 0)
            uptime_percent = _round_percent(
                (Decimal(window_seconds - downtime) * Decimal("100.00"))
                / Decimal(window_seconds)
            )
            items.append(
                UptimeReportItem(
                    group_by=group_by,
                    group_id=device.id,
                    name=device.name,
                    device_count=1,
                    total_seconds=window_seconds,
                    downtime_seconds=downtime,
                    uptime_percent=uptime_percent,
                )
            )
    elif group_by == "pon":
        items = _pon_availability_items(db, window_seconds)
    else:
        raise HTTPException(status_code=400, detail="Invalid group_by")
    return UptimeReportResponse(
        period_start=payload.period_start,
        period_end=payload.period_end,
        group_by=group_by,
        items=items,
    )


def _operator_check(operator: AlertOperator, measured: float, threshold: float) -> bool:
    if operator == AlertOperator.gt:
        return measured > threshold
    if operator == AlertOperator.gte:
        return measured >= threshold
    if operator == AlertOperator.lt:
        return measured < threshold
    if operator == AlertOperator.lte:
        return measured <= threshold
    return measured == threshold


def _rule_matches(rule: AlertRule, metric: DeviceMetric) -> bool:
    if rule.metric_type != metric.metric_type:
        return False
    if rule.device_id and rule.device_id != metric.device_id:
        return False
    if rule.interface_id and rule.interface_id != metric.interface_id:
        return False
    return True


def _violates_rule(db: Session, rule: AlertRule, metric: DeviceMetric) -> bool:
    if rule.duration_seconds and metric.recorded_at:
        window_start = metric.recorded_at - timedelta(seconds=rule.duration_seconds)
        query = (
            db.query(DeviceMetric)
            .filter(DeviceMetric.metric_type == rule.metric_type)
            .filter(DeviceMetric.device_id == metric.device_id)
            .filter(DeviceMetric.recorded_at >= window_start)
        )
        if metric.interface_id:
            query = query.filter(DeviceMetric.interface_id == metric.interface_id)
        samples = query.all()
        if not samples:
            return False
        violations = [
            sample
            for sample in samples
            if _operator_check(
                rule.operator, float(sample.value), float(rule.threshold)
            )
        ]
        return len(violations) == len(samples)
    return _operator_check(rule.operator, float(metric.value), float(rule.threshold))


def _process_alerts(db: Session, metric: DeviceMetric) -> None:
    rules = db.query(AlertRule).filter(AlertRule.is_active.is_(True)).all()
    for rule in rules:
        if not _rule_matches(rule, metric):
            continue
        _process_single_alert_rule(db, rule, metric)


def _process_single_alert_rule(
    db: Session, rule: AlertRule, metric: DeviceMetric
) -> None:
    """Process a single alert rule for a metric with deduplication.

    Uses advisory lock to prevent duplicate alert creation under concurrent
    metric ingestion. Lock key is derived from rule_id + device_id + interface_id.
    """
    from sqlalchemy import text

    # Build advisory lock key from rule + device + interface combination
    # Using hash to fit into bigint range for pg_advisory_xact_lock
    lock_parts = [
        str(rule.id) if rule.id else "",
        str(metric.device_id) if metric.device_id else "",
        str(metric.interface_id) if metric.interface_id else "",
    ]
    lock_key = hash("_".join(lock_parts)) & 0x7FFFFFFFFFFFFFFF  # Ensure positive bigint

    # Acquire advisory lock for this rule+device+interface combination
    # This serializes alert creation to prevent duplicates
    # Only available on PostgreSQL; skip on SQLite (used in tests)
    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    violated = _violates_rule(db, rule, metric)
    existing = (
        db.query(Alert)
        .filter(Alert.rule_id == rule.id)
        .filter(Alert.device_id == metric.device_id)
        .filter(Alert.interface_id == metric.interface_id)
        .filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
        .first()
    )
    if violated:
        if existing:
            existing.measured_value = float(metric.value)
            return
        if not _can_create_alert_for_metric(db, rule, metric):
            return
        alert = Alert(
            rule_id=rule.id,
            device_id=metric.device_id,
            interface_id=metric.interface_id,
            metric_type=metric.metric_type,
            measured_value=float(metric.value),
            status=AlertStatus.open,
            severity=rule.severity or AlertSeverity.warning,
            triggered_at=metric.recorded_at or datetime.now(UTC),
        )
        db.add(alert)
        db.flush()
        event = AlertEvent(
            alert_id=alert.id,
            status=AlertStatus.open,
            message="Alert triggered",
        )
        db.add(event)
        from app.services import notification as notification_service

        notification_service.alert_notification_policies.emit_for_alert(
            db, alert, AlertStatus.open
        )
    else:
        if existing:
            existing.status = AlertStatus.resolved
            existing.resolved_at = datetime.now(UTC)
            event = AlertEvent(
                alert_id=existing.id,
                status=AlertStatus.resolved,
                message="Alert resolved",
            )
            db.add(event)
            from app.services import notification as notification_service

            notification_service.alert_notification_policies.emit_for_alert(
                db, existing, AlertStatus.resolved
            )


def _can_create_alert_for_metric(
    db: Session, rule: AlertRule, metric: DeviceMetric
) -> bool:
    """Return whether a new alert should be opened for this metric sample."""
    if not metric.device_id:
        return True
    device = db.get(NetworkDevice, metric.device_id)
    if not device:
        return True
    if not device.send_notifications:
        return False
    delay_minutes = max(0, int(device.notification_delay_minutes or 0))
    if delay_minutes == 0:
        return True
    if metric.metric_type != MetricType.uptime:
        return True
    return _delay_window_violated(db, rule, metric, delay_minutes)


def _delay_window_violated(
    db: Session,
    rule: AlertRule,
    metric: DeviceMetric,
    delay_minutes: int,
) -> bool:
    """Check whether violation has persisted long enough to pass delay gate."""
    metric_recorded_at = _as_utc(metric.recorded_at)
    if not metric_recorded_at:
        return False
    window = timedelta(minutes=delay_minutes)
    # Walk recent samples backwards and find start of the current violation streak.
    query = (
        db.query(DeviceMetric)
        .filter(DeviceMetric.metric_type == metric.metric_type)
        .filter(DeviceMetric.device_id == metric.device_id)
        .filter(DeviceMetric.recorded_at <= metric_recorded_at)
    )
    if metric.interface_id is None:
        query = query.filter(DeviceMetric.interface_id.is_(None))
    else:
        query = query.filter(DeviceMetric.interface_id == metric.interface_id)
    query = query.order_by(DeviceMetric.recorded_at.desc()).limit(500)

    samples = query.all()
    if not samples:
        return False

    streak_start = metric_recorded_at
    for sample in samples:
        sample_recorded_at = _as_utc(sample.recorded_at)
        if sample_recorded_at is None:
            continue
        if not _operator_check(
            rule.operator, float(sample.value), float(rule.threshold)
        ):
            break
        streak_start = sample_recorded_at

    return (metric_recorded_at - streak_start) >= window


class PopSites(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PopSiteCreate):
        site = PopSite(**payload.model_dump())
        db.add(site)
        db.flush()
        db.refresh(site)
        return site

    @staticmethod
    def create_committed(db: Session, payload: PopSiteCreate):
        return _commit(db, lambda: PopSites.create(db, payload))

    @staticmethod
    def get(db: Session, site_id: str):
        site = db.get(PopSite, site_id)
        if not site:
            raise HTTPException(status_code=404, detail="PoP site not found")
        return site

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(PopSite)
        if is_active is None:
            stmt = stmt.where(PopSite.is_active.is_(True))
        else:
            stmt = stmt.where(PopSite.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": PopSite.created_at, "name": PopSite.name},
        )
        stmt = apply_pagination(stmt, limit, offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def update(db: Session, site_id: str, payload: PopSiteUpdate):
        site = db.get(PopSite, site_id)
        if not site:
            raise HTTPException(status_code=404, detail="PoP site not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(site, key, value)
        db.flush()
        db.refresh(site)
        return site

    @staticmethod
    def update_committed(db: Session, site_id: str, payload: PopSiteUpdate):
        return _commit(db, lambda: PopSites.update(db, site_id, payload))

    @staticmethod
    def delete(db: Session, site_id: str):
        site = db.get(PopSite, site_id)
        if not site:
            raise HTTPException(status_code=404, detail="PoP site not found")
        site.is_active = False
        db.flush()

    @staticmethod
    def delete_committed(db: Session, site_id: str):
        return _commit(db, lambda: PopSites.delete(db, site_id))


class NetworkDevices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: NetworkDeviceCreate):
        device = NetworkDevice(**payload.model_dump())
        db.add(device)
        db.flush()
        db.refresh(device)
        return device

    @staticmethod
    def create_committed(db: Session, payload: NetworkDeviceCreate):
        return _commit(db, lambda: NetworkDevices.create(db, payload))

    @staticmethod
    def get(db: Session, device_id: str):
        device = db.get(NetworkDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Network device not found")
        return device

    @staticmethod
    def list(
        db: Session,
        pop_site_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(NetworkDevice)
        if pop_site_id:
            stmt = stmt.where(NetworkDevice.pop_site_id == pop_site_id)
        if is_active is None:
            stmt = stmt.where(NetworkDevice.is_active.is_(True))
        else:
            stmt = stmt.where(NetworkDevice.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": NetworkDevice.created_at, "name": NetworkDevice.name},
        )
        stmt = apply_pagination(stmt, limit, offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def update(db: Session, device_id: str, payload: NetworkDeviceUpdate):
        device = db.get(NetworkDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Network device not found")
        fields = payload.model_dump(exclude_unset=True)
        # Admission is a lifecycle transition, not a field copy — it must carry
        # the derived-cache decay with it, so it never lands via setattr.
        admission = fields.pop("is_active", None)
        for key, value in fields.items():
            setattr(device, key, value)
        if admission is not None:
            set_network_device_active(
                db, device, bool(admission), reason="inventory_update"
            )
        db.flush()
        db.refresh(device)
        return device

    @staticmethod
    def update_committed(db: Session, device_id: str, payload: NetworkDeviceUpdate):
        return _commit(db, lambda: NetworkDevices.update(db, device_id, payload))

    @staticmethod
    def delete(db: Session, device_id: str):
        device = db.get(NetworkDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Network device not found")
        set_network_device_active(db, device, False, reason="manual_delete")

    @staticmethod
    def delete_committed(db: Session, device_id: str):
        return _commit(db, lambda: NetworkDevices.delete(db, device_id))

    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Build network dashboard stats for admin overview.

        Returns:
            Dictionary with working/total counts, operating percentage,
            active_alarms, device_status_chart, and alarm severity breakdown.

        Device counts come from the canonical cross-type projection instead of
        reinterpreting the mutable ``NetworkDevice.status`` admin field.
        """
        from app.models.network_monitoring import AlertSeverity as Sev
        from app.models.network_monitoring import AlertStatus as AStatus
        from app.services.device_projection_views import device_projection_stats

        device_counts = device_projection_stats(db)
        total_count = device_counts["total"]
        working_count = device_counts["working"]
        not_working_count = device_counts["not_working"]

        uptime_percentage = (
            round(working_count / total_count * 100, 1) if total_count > 0 else 0.0
        )

        device_status_chart = {
            "labels": ["Working", "Not working"],
            "values": [working_count, not_working_count],
            "tones": ["positive", "negative"],
        }

        # Alarm counts using SQL aggregation
        alarm_counts = (
            db.query(
                func.count(Alert.id)
                .filter(Alert.severity == Sev.critical)
                .label("critical"),
                func.count(Alert.id)
                .filter(Alert.severity == Sev.warning)
                .label("warning"),
                func.count(Alert.id).filter(Alert.severity == Sev.info).label("info"),
            )
            .filter(Alert.status == AStatus.open)
            .one()
        )

        alarms_critical = alarm_counts.critical or 0
        alarms_warning = alarm_counts.warning or 0
        alarms_info = alarm_counts.info or 0

        # Top 5 critical/recent alarms - query only what we need
        top_alarms = (
            db.query(Alert)
            .filter(Alert.status == AStatus.open)
            .order_by(
                # Critical first, then by created_at descending
                (Alert.severity != Sev.critical).asc(),
                Alert.created_at.desc(),
            )
            .limit(5)
            .all()
        )

        critical_alarms = [
            {
                "id": str(a.id),
                "device_id": str(a.device_id) if a.device_id else None,
                "severity": a.severity.value if a.severity else "unknown",
                "message": a.notes or "",
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in top_alarms
        ]

        return {
            "working_count": working_count,
            "total_count": total_count,
            "not_working_count": not_working_count,
            "uptime_percentage": uptime_percentage,
            "device_status_chart": device_status_chart,
            "alarms_critical": alarms_critical,
            "alarms_major": 0,
            "alarms_minor": alarms_info,
            "alarms_warning": alarms_warning,
            "active_alarms": critical_alarms,
        }

    @staticmethod
    def get_monitoring_dashboard_stats(
        db: Session,
        *,
        format_duration,
        format_bps,
        query: str | None = None,
    ) -> dict:
        """Build full monitoring dashboard data.

        Extends the base dashboard stats with performance metrics,
        device health table, subscriber count, alarms, and recent events.
        """
        from sqlalchemy import and_ as sa_and
        from sqlalchemy import func as sa_func
        from sqlalchemy import or_ as sa_or

        from app.models.network_monitoring import MetricType as MT
        from app.models.usage import AccountingStatus, RadiusAccountingSession
        from app.services.device_operational_status import (
            NOT_WORKING,
            WORKING,
            OperationalStatus,
            annotate_operational_status,
        )

        filter_value = (query or "").strip().lower()

        # ---- Device counts (always from all devices for accurate stats) ----
        base_query = db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True))
        devices = base_query.order_by(NetworkDevice.name).all()
        annotate_operational_status(devices)
        operational_by_id = {
            device.id: cast(
                OperationalStatus,
                cast(Any, device).operational,
            )
            for device in devices
        }
        working_count = len(
            [
                device
                for device in devices
                if operational_by_id[device.id].status == WORKING
            ]
        )
        not_working_count = len(
            [
                device
                for device in devices
                if operational_by_id[device.id].status == NOT_WORKING
            ]
        )
        impaired_count = len(
            [device for device in devices if operational_by_id[device.id].impaired]
        )

        device_status_chart = {
            "labels": ["Working", "Not working"],
            "values": [working_count, not_working_count],
            "tones": ["positive", "negative"],
        }

        # ---- Alarms ----
        alarms = (
            db.query(Alert)
            .filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
            .order_by(Alert.triggered_at.desc())
            .limit(10)
            .all()
        )
        alarms_open = sum(1 for a in alarms if a.status == AlertStatus.open)

        # ---- Recent events ----
        recent_events = (
            db.query(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(10).all()
        )

        # ---- Subscribers online ----
        subscribers_online = (
            db.query(
                sa_func.count(sa_func.distinct(RadiusAccountingSession.subscription_id))
            )
            .filter(RadiusAccountingSession.session_end.is_(None))
            .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
            .scalar()
            or 0
        )

        # ---- Performance metrics (latest per device) ----
        metric_types = [MT.cpu, MT.memory, MT.uptime, MT.rx_bps, MT.tx_bps]
        latest_subq = (
            db.query(
                DeviceMetric.device_id,
                DeviceMetric.metric_type,
                sa_func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .filter(DeviceMetric.metric_type.in_(metric_types))
            .group_by(DeviceMetric.device_id, DeviceMetric.metric_type)
            .subquery()
        )
        latest_metrics = (
            db.query(DeviceMetric)
            .join(
                latest_subq,
                sa_and(
                    DeviceMetric.device_id == latest_subq.c.device_id,
                    DeviceMetric.metric_type == latest_subq.c.metric_type,
                    DeviceMetric.recorded_at == latest_subq.c.latest,
                ),
            )
            .all()
        )

        metrics_by_device: dict[str, dict] = {}
        rx_total = 0.0
        tx_total = 0.0
        cpu_values: list[float] = []
        mem_values: list[float] = []
        for metric in latest_metrics:
            device_metrics = metrics_by_device.setdefault(str(metric.device_id), {})
            device_metrics[metric.metric_type] = metric
            if metric.metric_type == MT.rx_bps:
                rx_total += float(metric.value or 0)
            elif metric.metric_type == MT.tx_bps:
                tx_total += float(metric.value or 0)
            elif metric.metric_type == MT.cpu:
                cpu_values.append(float(metric.value or 0))
            elif metric.metric_type == MT.memory:
                mem_values.append(float(metric.value or 0))

        avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else None
        avg_mem = sum(mem_values) / len(mem_values) if mem_values else None

        # ---- Device health table (with SQL-level search filtering) ----
        if filter_value:
            # Apply search filter at SQL level for better performance
            search_pattern = f"%{filter_value}%"
            filtered_devices = (
                base_query.filter(
                    sa_or(
                        NetworkDevice.name.ilike(search_pattern),
                        NetworkDevice.hostname.ilike(search_pattern),
                        NetworkDevice.mgmt_ip.ilike(search_pattern),
                        NetworkDevice.vendor.ilike(search_pattern),
                        NetworkDevice.model.ilike(search_pattern),
                    )
                )
                .order_by(NetworkDevice.name)
                .all()
            )
        else:
            filtered_devices = devices

        device_health = []
        for device in filtered_devices:
            operational = operational_by_id[device.id]
            dm = metrics_by_device.get(str(device.id), {})
            cpu_m = dm.get(MT.cpu)
            mem_m = dm.get(MT.memory)
            uptime_m = dm.get(MT.uptime)
            device_health.append(
                {
                    "id": str(device.id),
                    "name": device.name,
                    "hostname": device.hostname,
                    "mgmt_ip": device.mgmt_ip,
                    "status": operational.status,
                    "status_reason": operational.reason,
                    "status_presentation": operational.presentation,
                    "health_status": device.health_status.value
                    if device.health_status
                    else "unknown",
                    "max_concurrent_subscribers": device.max_concurrent_subscribers,
                    "current_subscriber_count": device.current_subscriber_count or 0,
                    "cpu": f"{cpu_m.value:.1f}%" if cpu_m else "--",
                    "memory": f"{mem_m.value:.1f}%" if mem_m else "--",
                    "uptime": format_duration(uptime_m.value if uptime_m else None),
                    "last_seen": device.last_ping_at or device.last_snmp_at,
                }
            )

        return {
            "stats": {
                "devices_total": len(devices),
                "devices_working": working_count,
                "devices_not_working": not_working_count,
                "devices_impaired": impaired_count,
                "alarms_open": alarms_open,
                "subscribers_online": subscribers_online,
            },
            "alarms": alarms,
            "recent_events": recent_events,
            "performance": {
                "avg_cpu": f"{avg_cpu:.1f}%" if avg_cpu is not None else "--",
                "avg_memory": f"{avg_mem:.1f}%" if avg_mem is not None else "--",
                "rx_bps": format_bps(rx_total) if rx_total > 0 else "--",
                "tx_bps": format_bps(tx_total) if tx_total > 0 else "--",
            },
            "device_health": device_health,
            "device_health_total": len(device_health),
            "device_status_chart": device_status_chart,
        }


class DeviceInterfaces(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DeviceInterfaceCreate):
        interface = DeviceInterface(**payload.model_dump())
        db.add(interface)
        db.flush()
        db.refresh(interface)
        return interface

    @staticmethod
    def create_committed(db: Session, payload: DeviceInterfaceCreate):
        return _commit(db, lambda: DeviceInterfaces.create(db, payload))

    @staticmethod
    def get(db: Session, interface_id: str):
        interface = db.get(DeviceInterface, interface_id)
        if not interface:
            raise HTTPException(status_code=404, detail="Device interface not found")
        return interface

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(DeviceInterface)
        if device_id:
            stmt = stmt.where(DeviceInterface.device_id == device_id)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": DeviceInterface.created_at, "name": DeviceInterface.name},
        )
        stmt = apply_pagination(stmt, limit, offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def update(db: Session, interface_id: str, payload: DeviceInterfaceUpdate):
        interface = db.get(DeviceInterface, interface_id)
        if not interface:
            raise HTTPException(status_code=404, detail="Device interface not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(interface, key, value)
        db.flush()
        db.refresh(interface)
        return interface

    @staticmethod
    def update_committed(
        db: Session, interface_id: str, payload: DeviceInterfaceUpdate
    ):
        return _commit(db, lambda: DeviceInterfaces.update(db, interface_id, payload))

    @staticmethod
    def delete(db: Session, interface_id: str):
        interface = db.get(DeviceInterface, interface_id)
        if not interface:
            raise HTTPException(status_code=404, detail="Device interface not found")
        db.delete(interface)
        db.flush()

    @staticmethod
    def delete_committed(db: Session, interface_id: str):
        return _commit(db, lambda: DeviceInterfaces.delete(db, interface_id))


class DeviceMetrics(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: DeviceMetricCreate):
        metric = DeviceMetric(**payload.model_dump())
        db.add(metric)
        db.flush()
        _process_alerts(db, metric)
        db.flush()
        db.refresh(metric)
        return metric

    @staticmethod
    def create_committed(db: Session, payload: DeviceMetricCreate):
        return _commit(db, lambda: DeviceMetrics.create(db, payload))

    @staticmethod
    def get(db: Session, metric_id: str):
        metric = db.get(DeviceMetric, metric_id)
        if not metric:
            raise HTTPException(status_code=404, detail="Device metric not found")
        return metric

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        interface_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(DeviceMetric)
        if device_id:
            stmt = stmt.where(DeviceMetric.device_id == device_id)
        if interface_id:
            stmt = stmt.where(DeviceMetric.interface_id == interface_id)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {
                "created_at": DeviceMetric.created_at,
                "recorded_at": DeviceMetric.recorded_at,
            },
        )
        stmt = apply_pagination(stmt, limit, offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def update(db: Session, metric_id: str, payload: DeviceMetricUpdate):
        metric = db.get(DeviceMetric, metric_id)
        if not metric:
            raise HTTPException(status_code=404, detail="Device metric not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(metric, key, value)
        db.flush()
        db.refresh(metric)
        return metric

    @staticmethod
    def update_committed(db: Session, metric_id: str, payload: DeviceMetricUpdate):
        return _commit(db, lambda: DeviceMetrics.update(db, metric_id, payload))

    @staticmethod
    def delete(db: Session, metric_id: str):
        metric = db.get(DeviceMetric, metric_id)
        if not metric:
            raise HTTPException(status_code=404, detail="Device metric not found")
        db.delete(metric)
        db.flush()

    @staticmethod
    def delete_committed(db: Session, metric_id: str):
        return _commit(db, lambda: DeviceMetrics.delete(db, metric_id))


class AlertRules(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AlertRuleCreate):
        rule = AlertRule(**payload.model_dump())
        db.add(rule)
        db.flush()
        db.refresh(rule)
        return rule

    @staticmethod
    def create_committed(db: Session, payload: AlertRuleCreate):
        return _commit(db, lambda: AlertRules.create(db, payload))

    @staticmethod
    def get(db: Session, rule_id: str):
        rule = db.get(AlertRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        return rule

    @staticmethod
    def list(
        db: Session,
        scope: str | None = None,
        severity: str | None = None,
        is_active: bool | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 50,
        offset: int = 0,
        metric_type: str | None = None,
        device_id: str | None = None,
        interface_id: str | None = None,
    ):
        stmt = select(AlertRule)
        if scope == "device":
            stmt = stmt.where(AlertRule.device_id.isnot(None))
        elif scope == "interface":
            stmt = stmt.where(AlertRule.interface_id.isnot(None))
        elif scope == "global":
            stmt = stmt.where(
                AlertRule.device_id.is_(None), AlertRule.interface_id.is_(None)
            )
        if severity:
            stmt = stmt.where(
                AlertRule.severity == validate_enum(severity, AlertSeverity, "severity")
            )
        if metric_type:
            stmt = stmt.where(
                AlertRule.metric_type
                == validate_enum(metric_type, MetricType, "metric_type")
            )
        if device_id:
            stmt = stmt.where(AlertRule.device_id == device_id)
        if interface_id:
            stmt = stmt.where(AlertRule.interface_id == interface_id)
        if is_active is None:
            stmt = stmt.where(AlertRule.is_active.is_(True))
        else:
            stmt = stmt.where(AlertRule.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": AlertRule.created_at, "name": AlertRule.name},
        )
        stmt = apply_pagination(stmt, limit, offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def update(db: Session, rule_id: str, payload: AlertRuleUpdate):
        rule = db.get(AlertRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(rule, key, value)
        db.flush()
        db.refresh(rule)
        return rule

    @staticmethod
    def update_committed(db: Session, rule_id: str, payload: AlertRuleUpdate):
        return _commit(db, lambda: AlertRules.update(db, rule_id, payload))

    @staticmethod
    def delete(db: Session, rule_id: str):
        rule = db.get(AlertRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        rule.is_active = False
        db.flush()

    @staticmethod
    def delete_committed(db: Session, rule_id: str):
        return _commit(db, lambda: AlertRules.delete(db, rule_id))

    @staticmethod
    def bulk_update(db: Session, payload: AlertRuleBulkUpdateRequest) -> int:
        if not payload.rule_ids:
            raise HTTPException(status_code=400, detail="rule_ids required")
        ids = [coerce_uuid(rule_id) for rule_id in payload.rule_ids]
        rules = db.query(AlertRule).filter(AlertRule.id.in_(ids)).all()
        if len(rules) != len(ids):
            raise HTTPException(
                status_code=404, detail="One or more alert rules not found"
            )
        for rule in rules:
            rule.is_active = payload.is_active
        db.flush()
        return len(rules)

    @staticmethod
    def bulk_update_response(db: Session, payload: AlertRuleBulkUpdateRequest) -> dict:
        updated = AlertRules.bulk_update(db, payload)
        return {"updated": updated}

    @staticmethod
    def bulk_update_response_committed(
        db: Session, payload: AlertRuleBulkUpdateRequest
    ) -> dict:
        return _commit(db, lambda: AlertRules.bulk_update_response(db, payload))


class Alerts(ListResponseMixin):
    @staticmethod
    def get(db: Session, alert_id: str):
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        return alert

    @staticmethod
    def list(
        db: Session,
        rule_id: str | None,
        device_id: str | None,
        interface_id: str | None,
        status: str | None,
        severity: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(Alert)
        if rule_id:
            stmt = stmt.where(Alert.rule_id == rule_id)
        if device_id:
            stmt = stmt.where(Alert.device_id == device_id)
        if interface_id:
            stmt = stmt.where(Alert.interface_id == interface_id)
        if status:
            stmt = stmt.where(
                Alert.status == validate_enum(status, AlertStatus, "status")
            )
        if severity:
            stmt = stmt.where(
                Alert.severity == validate_enum(severity, AlertSeverity, "severity")
            )
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": Alert.created_at, "triggered_at": Alert.triggered_at},
        )
        stmt = apply_pagination(stmt, limit, offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def acknowledge(db: Session, alert_id: str, payload: AlertAcknowledgeRequest):
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        alert.status = AlertStatus.acknowledged
        alert.acknowledged_at = datetime.now(UTC)
        event = AlertEvent(
            alert_id=alert.id,
            status=AlertStatus.acknowledged,
            message=payload.message or "Alert acknowledged",
        )
        db.add(event)
        db.flush()
        db.refresh(alert)
        return alert

    @staticmethod
    def acknowledge_committed(
        db: Session, alert_id: str, payload: AlertAcknowledgeRequest
    ):
        return _commit(db, lambda: Alerts.acknowledge(db, alert_id, payload))

    @staticmethod
    def resolve(db: Session, alert_id: str, payload: AlertResolveRequest):
        alert = db.get(Alert, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        alert.status = AlertStatus.resolved
        alert.resolved_at = datetime.now(UTC)
        event = AlertEvent(
            alert_id=alert.id,
            status=AlertStatus.resolved,
            message=payload.message or "Alert resolved",
        )
        db.add(event)
        db.flush()
        db.refresh(alert)
        return alert

    @staticmethod
    def resolve_committed(db: Session, alert_id: str, payload: AlertResolveRequest):
        return _commit(db, lambda: Alerts.resolve(db, alert_id, payload))

    @staticmethod
    def bulk_acknowledge(
        db: Session, alert_ids: builtins.list[str], payload: AlertAcknowledgeRequest
    ) -> int:
        if not alert_ids:
            raise HTTPException(status_code=400, detail="alert_ids required")
        ids = [coerce_uuid(alert_id) for alert_id in alert_ids]
        alerts = db.query(Alert).filter(Alert.id.in_(ids)).all()
        if len(alerts) != len(ids):
            raise HTTPException(status_code=404, detail="One or more alerts not found")
        now = datetime.now(UTC)
        for alert in alerts:
            alert.status = AlertStatus.acknowledged
            alert.acknowledged_at = now
            event = AlertEvent(
                alert_id=alert.id,
                status=AlertStatus.acknowledged,
                message=payload.message or "Alert acknowledged",
            )
            db.add(event)
        db.flush()
        return len(alerts)

    @staticmethod
    def bulk_acknowledge_response(
        db: Session, alert_ids: builtins.list[str], payload: AlertAcknowledgeRequest
    ) -> dict[str, int]:
        updated = Alerts.bulk_acknowledge(db, alert_ids, payload)
        return {"updated": updated}

    @staticmethod
    def bulk_acknowledge_response_committed(
        db: Session, alert_ids: builtins.list[str], payload: AlertAcknowledgeRequest
    ) -> dict[str, int]:
        return _commit(
            db, lambda: Alerts.bulk_acknowledge_response(db, alert_ids, payload)
        )

    @staticmethod
    def bulk_resolve(
        db: Session, alert_ids: builtins.list[str], payload: AlertResolveRequest
    ) -> int:
        if not alert_ids:
            raise HTTPException(status_code=400, detail="alert_ids required")
        ids = [coerce_uuid(alert_id) for alert_id in alert_ids]
        alerts = db.query(Alert).filter(Alert.id.in_(ids)).all()
        if len(alerts) != len(ids):
            raise HTTPException(status_code=404, detail="One or more alerts not found")
        now = datetime.now(UTC)
        for alert in alerts:
            alert.status = AlertStatus.resolved
            alert.resolved_at = now
            event = AlertEvent(
                alert_id=alert.id,
                status=AlertStatus.resolved,
                message=payload.message or "Alert resolved",
            )
            db.add(event)
        db.flush()
        return len(alerts)

    @staticmethod
    def bulk_resolve_response(
        db: Session, alert_ids: builtins.list[str], payload: AlertResolveRequest
    ) -> dict[str, int]:
        updated = Alerts.bulk_resolve(db, alert_ids, payload)
        return {"updated": updated}

    @staticmethod
    def bulk_resolve_response_committed(
        db: Session, alert_ids: builtins.list[str], payload: AlertResolveRequest
    ) -> dict[str, int]:
        return _commit(db, lambda: Alerts.bulk_resolve_response(db, alert_ids, payload))


class AlertEvents(ListResponseMixin):
    @staticmethod
    def list(
        db: Session,
        alert_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        stmt = select(AlertEvent)
        if alert_id:
            stmt = stmt.where(AlertEvent.alert_id == alert_id)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {"created_at": AlertEvent.created_at},
        )
        stmt = apply_pagination(stmt, limit, offset)
        return list(db.scalars(stmt).all())


def get_onu_status_summary(db: Session, *, refresh: bool = False) -> dict[str, int]:
    """Aggregate the owner-resolved binary ONT operational status.

    Returns:
        Dictionary with total, working, not_working, low_signal counts.
    """
    del refresh
    from app.services.network.ont_status import ont_status_summary

    return ont_status_summary(db, low_signal_threshold_dbm=-27)


def get_pon_outage_summary(db: Session) -> list[dict]:
    """Detect fully offline PON ports (possible fiber cut).

    Groups offline ONTs by PON port and returns only ports where every
    assigned ONT is currently offline.

    Returns:
        List of dicts: {pon_port_name, olt_name, offline_count, total_count,
        offline_reasons, last_seen}.
    """
    from app.models.network import (
        OLTDevice,
        OntAssignment,
        OntUnit,
        PonPort,
    )

    assignments = (
        db.query(OntAssignment)
        .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
        .filter(OntAssignment.active.is_(True), OntUnit.is_active.is_(True))
        .all()
    )
    ont_ids = [assignment.ont_unit_id for assignment in assignments]
    onts = db.query(OntUnit).filter(OntUnit.id.in_(ont_ids)).all() if ont_ids else []
    ont_by_id = {ont.id: ont for ont in onts}
    from app.services.network.ont_status import resolve_effective_ont_status

    port_offline: dict[str, list[dict]] = {}
    total_per_port: dict[str, int] = {}
    for assignment in assignments:
        pon_port_id = assignment.pon_port_id
        port_key = str(pon_port_id) if pon_port_id else ""
        if not port_key:
            continue
        total_per_port[port_key] = total_per_port.get(port_key, 0) + 1
        ont = ont_by_id.get(assignment.ont_unit_id)
        if ont is None:
            continue
        effective = resolve_effective_ont_status(ont)
        if effective.is_online or effective.retry_pending:
            continue
        reason = getattr(ont, "offline_reason", None)
        last_seen = getattr(ont, "last_seen_at", None)
        port_offline.setdefault(port_key, []).append(
            {"reason": reason.value if reason else "offline", "last_seen": last_seen}
        )

    outage_port_ids = [
        pid
        for pid, items in port_offline.items()
        if total_per_port.get(pid, 0) > 0 and len(items) == total_per_port[pid]
    ]
    if not outage_port_ids:
        return []

    # Enrich with PON port and OLT names + total ONT count per port
    pon_ports = db.query(PonPort).filter(PonPort.id.in_(outage_port_ids)).all()
    pon_port_map = {str(p.id): p for p in pon_ports}

    # Get OLT names
    olt_ids = list({str(p.olt_id) for p in pon_ports if p.olt_id})
    olts = (
        db.query(OLTDevice).filter(OLTDevice.id.in_(olt_ids)).all() if olt_ids else []
    )
    olt_map = {str(o.id): o.name for o in olts}

    results: list[dict] = []
    for port_id in outage_port_ids:
        port = pon_port_map.get(port_id)
        if not port:
            continue
        offline_items = port_offline[port_id]
        reasons: dict[str, int] = {}
        latest_seen = None
        for item in offline_items:
            r = item["reason"]
            reasons[r] = reasons.get(r, 0) + 1
            if item["last_seen"] and (
                latest_seen is None or item["last_seen"] > latest_seen
            ):
                latest_seen = item["last_seen"]

        results.append(
            {
                "pon_port_name": port.name or str(port.id)[:8],
                "olt_name": olt_map.get(str(port.olt_id), "Unknown"),
                "offline_count": len(offline_items),
                "total_count": total_per_port.get(port_id, 0),
                "offline_reasons": reasons,
                "last_seen": latest_seen,
            }
        )

    # Sort by offline count descending
    results.sort(key=lambda x: x["offline_count"], reverse=True)
    return results


pop_sites = PopSites()
network_devices = NetworkDevices()
device_interfaces = DeviceInterfaces()
device_metrics = DeviceMetrics()
alert_rules = AlertRules()
alerts = Alerts()
alert_events = AlertEvents()
