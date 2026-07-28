"""Topology live-status warmer.

Derives a coarse ``live_status`` (up/down/unknown) for reconciled topology
nodes from the native poll columns the infrastructure poller maintains
(``last_ping_*`` / ``last_snmp_*``, see ``services.infrastructure_polling``)
and writes it into the network_devices cache. A failed ping is always the
primary outage signal; ping success is the primary healthy signal; SNMP
reachability is used only when there is no fresh ping result. The Network
Path panel reads that cache — no probe ever runs on the request path (same
warm-and-store pattern the retired monitoring warmers used).

Formerly this warmer batch-fetched Zabbix host availability for reconciled
(``source == zabbix_reconcile``) nodes; the derived statuses, heartbeat key
and SLA availability bridge are unchanged, but the data source moved to the
native poll columns and the population is now source-agnostic: every active
*pollable* device (same predicate as the poll sweep) gets a live_status,
however its row was created. Unpollable devices keep a NULL live_status so
surfaces with their own fallbacks (e.g. linked-router status) still apply
them. The old ``uisp.status`` trapper fallback is gone: radio/CPE health
feeds the outage pipeline natively via ``CPEDevice.last_uisp_status``
(uisp_sync), and a pollable node with neither a fresh ping nor SNMP result
reads ``unknown``, which every consumer already treats conservatively.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.services.infrastructure_polling import pollable_device_criteria

UP = "up"
DOWN = "down"
PROBLEM = "problem"
UNKNOWN = "unknown"

# A poll result older than this no longer proves anything about the device —
# the poller has stopped covering it (disabled checks, poller down), so the
# node degrades to unknown instead of freezing on its last state. Generous
# multiple of the default 60s ping staleness window.
STALE_POLL_AFTER_SECONDS = 900

# Heartbeat written on every warm run so the customer-facing connection-status
# reader can tell whether live_status is being refreshed. If the warmer dies,
# this key ages out and good states stop being trusted (see topology.selfcare).
# TTL is far longer than the staleness window so the timestamp survives to be
# age-compared (a TTL-expired key reads as "missing", which we treat as
# unknown-freshness, not stale — see selfcare._warm_is_stale).
WARM_HEARTBEAT_KEY = "topology:live_status:warmed_at"
_WARM_HEARTBEAT_TTL_SECONDS = 86_400


def touch_warm_heartbeat(now: datetime | None = None) -> None:
    """Record that the live_status warmer just ran (advisory, cache-only).

    Called from the warm task after a successful refresh — kept out of the pure
    ``warm_topology_status`` service function so that has no cache side effects.
    """
    try:
        from app.services.app_cache import set_json

        stamp = (now or _now()).isoformat()
        set_json(WARM_HEARTBEAT_KEY, stamp, _WARM_HEARTBEAT_TTL_SECONDS)
    except Exception:  # cache is advisory; never fail the warm over it
        pass


def _now() -> datetime:
    return datetime.now(UTC)


def _sla_log_enabled() -> bool:
    try:
        from app.config import settings

        return bool(settings.sla_availability_log_enabled)
    except Exception:  # config is advisory here; never fail the warm over it
        return False


def _coverage():
    """Monitoring-path coverage for SLA-bridge gating; None on any failure
    (then the bridge logs everything, i.e. pre-Phase-3 behaviour)."""
    try:
        from app.services.monitoring_coverage import get_coverage

        return get_coverage()
    except Exception:
        return None


def _fresh(checked_at: datetime | None, now: datetime, window_seconds: int) -> bool:
    if checked_at is None:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return (now - checked_at).total_seconds() <= window_seconds


def derive_live_status(
    node: NetworkDevice,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> str:
    """Map a node's native poll columns to up/down/unknown.

    A device in operator ``maintenance`` can't be trusted to report real
    reachability (mirrors the old Zabbix maintenance handling): it reads
    ``unknown`` rather than surfacing a deliberate shutdown to customers as an
    outage. Ping is authoritative when fresh; SNMP reachability only fills in
    for ping-disabled devices.
    """
    now = now or _now()
    if node.status == DeviceStatus.maintenance:
        return UNKNOWN
    if (
        node.ping_enabled
        and node.last_ping_ok is not None
        and _fresh(node.last_ping_at, now, stale_after_seconds)
    ):
        return UP if node.last_ping_ok else DOWN
    if (
        node.snmp_enabled
        and node.last_snmp_ok is not None
        and _fresh(node.last_snmp_at, now, stale_after_seconds)
    ):
        return UP if node.last_snmp_ok else DOWN
    return UNKNOWN


def live_status_observed_at(
    node: NetworkDevice,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> datetime | None:
    """Return the native timestamp relevant to the current ``live_status``.

    ``live_status_at`` is deliberately not an observation timestamp: it records
    when the derived status last changed for debounce and availability dwell
    calculations. Freshness decisions must follow the same ping/SNMP precedence
    as :func:`derive_live_status` and use the selected collector timestamp. If
    no collector result is fresh enough to produce a conclusive status, return
    the preferred stale timestamp so callers can distinguish expired evidence
    from a legacy row that has no per-device observation evidence.
    """
    now = now or _now()
    if getattr(node, "status", None) == DeviceStatus.maintenance:
        return None
    if (
        getattr(node, "ping_enabled", False)
        and getattr(node, "last_ping_ok", None) is not None
        and _fresh(getattr(node, "last_ping_at", None), now, stale_after_seconds)
    ):
        return getattr(node, "last_ping_at", None)
    if (
        getattr(node, "snmp_enabled", False)
        and getattr(node, "last_snmp_ok", None) is not None
        and _fresh(getattr(node, "last_snmp_at", None), now, stale_after_seconds)
    ):
        return getattr(node, "last_snmp_at", None)
    if (
        getattr(node, "ping_enabled", False)
        and getattr(node, "last_ping_ok", None) is not None
    ):
        return getattr(node, "last_ping_at", None)
    if (
        getattr(node, "snmp_enabled", False)
        and getattr(node, "last_snmp_ok", None) is not None
    ):
        return getattr(node, "last_snmp_at", None)
    return None


def warm_topology_status(
    session: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> dict:
    """Refresh live_status for every active pollable device."""
    nodes = session.query(NetworkDevice).filter(*pollable_device_criteria()).all()
    if not nodes:
        return {"nodes": 0}

    now = now or _now()
    sla_logging = _sla_log_enabled()
    coverage = _coverage() if sla_logging else None
    counts: Counter = Counter()
    for n in nodes:
        status = derive_live_status(n, now=now, stale_after_seconds=stale_after_seconds)
        # Stamp live_status_at only when the state CHANGES, so it marks when the
        # node entered its current state — the dwell clock the customer-facing
        # connection-status debounce relies on (see topology.selfcare).
        if n.live_status != status:
            # Bridge the transition into an uptime Alert interval so the SLA
            # report has real downtime to merge (flag-gated, additive — never
            # alters live_status). Skip devices with no monitoring path: their
            # "down" is a blind spot, not real downtime. See
            # availability_log / monitoring_coverage / INFRASTRUCTURE_SLA.
            if sla_logging and (
                coverage is None or coverage.covers(getattr(n, "mgmt_ip", None))
            ):
                from app.services.topology.availability_log import record_transition

                record_transition(session, n, status, now=now)
            n.live_status = status
            n.live_status_at = now
        counts[status] += 1
    session.flush()
    return {"nodes": len(nodes), **counts}
