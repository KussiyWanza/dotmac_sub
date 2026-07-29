"""Last-mile diagnoser — P2 (design: docs/designs/OUTAGE_CLASSIFIER.md §5).

When P1 says a subscription's session is DOWN but the infra above it is UP
(``service_fault`` / a localized boundary with survivors), *why* is this one
customer offline? Descend the last-mile ladder (§5) and name it, so support and
selfcare get "reboot your router" vs "your ONT has no power" vs "a tech is
needed" instead of a useless "area down".

The ladder — each rung a signal already on prod (§5)::

    session (PPPoE)          ← symptom: no fresh RadiusActiveSession
      ⇑ CPE authenticating    ← RADIUS reject (radius_auth_errors) + ACS inform
      ⇑ CPE link healthy      ← optical Rx (fiber) / RF (wireless — see gap)
      ⇑ CPE present at node    ← ONT olt_status / radio last_uisp_status
      ⇑ CPE powered           ← absent everywhere ⟹ off / drop cut

Verdicts (§5 table): ``power`` (off / drop cut), ``signal_degraded`` (present,
bad signal — schedule tech), ``router_offline`` (present, good signal, ACS
stale — "reboot"), ``auth`` (present, informing, RADIUS reject — operator-fix,
no truck), ``config`` (present, good signal, no RADIUS attempt — not dialing),
plus ``healthy`` (session actually up) and ``unknown`` (insufficient linkage /
signal, or the fault is upstream and P1 owns it).

**Medium asymmetry (design §4 — the real P2 gap).** Fiber ONTs report presence
(``olt_status``), optical Rx (``onu_rx_signal_dbm``) and an offline *reason*
(power_fail / los / dying_gasp), so the fiber ladder is complete. Wireless radios
(``CPEDevice``) store only ``last_uisp_status`` (active / disconnected /
unauthorized / vanished) — **no RF/RSSI value exists on prod** — so the wireless
link-signal rung is unobservable: ``signal_dbm`` is ``None`` and ``signal_degraded``
can't be diagnosed for wireless. We report presence + auth honestly and stop
there rather than fabricate an RF verdict.

Out of P2 scope (documented TODOs): flapping / degraded-vs-down separation
(§7.7), maintenance-window suppression (§7.8), splice inference (§6, P3),
surfaces (P4).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription
from app.models.network import OnuOfflineReason
from app.models.network_monitoring import NetworkDevice
from app.models.radius_error import RadiusAuthError
from app.services.device_operational_status import warmer_is_stale
from app.services.topology.affected import affected_customers
from app.services.topology.customer_path import CustomerPath, resolve_customer_path
from app.services.topology.health_classifier import (
    HEALTHY as NODE_HEALTHY,
)
from app.services.topology.health_classifier import (
    SERVICE_FAULT as NODE_SERVICE_FAULT,
)
from app.services.topology.health_classifier import (
    classify_node,
    online_subscription_ids,
)

logger = logging.getLogger(__name__)

# --- tunable thresholds (documented so ops can retune) --------------------

# GPON downstream Rx below this is marginal/degraded — schedule a tech rather
# than call it an outage. -27 dBm is a conservative GPON floor (LOS approaches
# ~-28..-30). TODO(design §7.7): separate marginal-but-up (flapping) from a
# clean cut once we track Rx history, not a single sample.
RX_SIGNAL_MIN_DBM = -27.0

# TR-069 periodic-inform cadence has headroom at 30 min: if the ONT/router has
# not informed the ACS within this window it is treated as not-informing
# ("reboot your router"), distinct from session freshness.
ACS_STALE_TTL = timedelta(minutes=30)

# Only auth failures this recent count as "the CPE tried and was rejected".
# Older rows are history, not the current symptom.
AUTH_ERROR_LOOKBACK = timedelta(minutes=30)

# --- verdicts (design §5) -------------------------------------------------

POWER = "power"
SIGNAL_DEGRADED = "signal_degraded"
ROUTER_OFFLINE = "router_offline"
AUTH = "auth"
CONFIG = "config"
HEALTHY = "healthy"
UNKNOWN = "unknown"

MEDIUM_FIBER = "fiber"
MEDIUM_WIRELESS = "wireless"
MEDIUM_UNKNOWN = "unknown"

# Customer-facing message + the action support/dispatch should take, per verdict.
_MESSAGES: dict[str, tuple[str, str]] = {
    POWER: (
        "Your equipment appears to have no power. Check the ONT/router is "
        "plugged in and its lights are on.",
        "customer_power_or_drop — confirm power with customer before dispatch",
    ),
    SIGNAL_DEGRADED: (
        "We're seeing a weak signal to your equipment. We'll schedule a "
        "technician to check the line.",
        "schedule_tech — line/optical degraded",
    ),
    ROUTER_OFFLINE: (
        "Your router isn't responding. Please power it off, wait 30 seconds, "
        "and turn it back on.",
        "ask_customer_reboot — no truck roll",
    ),
    AUTH: (
        "There's an account/authentication issue on our side. We're on it — "
        "no action needed from you.",
        "operator_fix_auth — no truck roll",
    ),
    CONFIG: (
        "Your equipment is online but isn't connecting. We're checking its "
        "configuration.",
        "operator_check_config — CPE not dialing",
    ),
    HEALTHY: ("Your connection looks healthy.", "none"),
    UNKNOWN: (
        "We're still diagnosing your connection.",
        "insufficient_signal_or_upstream_fault — see evidence",
    ),
}


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a DB datetime to tz-aware UTC (SQLite returns naive; PG aware)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _result(
    verdict: str, medium: str, signal_dbm: float | None, evidence: dict
) -> dict:
    msg, action = _MESSAGES[verdict]
    return {
        "verdict": verdict,
        "medium": medium,
        "signal_dbm": signal_dbm,
        "evidence": evidence,
        "customer_message": msg,
        "agent_action": action,
    }


def _has_recent_auth_reject(session, subscription: Subscription, now: datetime) -> bool:
    """True if this subscriber hit RADIUS and was refused, recently (§5 auth rung).

    A row in ``radius_auth_errors`` means the CPE *reached* RADIUS — so its
    absence (with no live session) is the "no RADIUS attempt" (config) signal,
    and its presence is the "reject" (auth) signal.
    """
    cutoff = now - AUTH_ERROR_LOOKBACK
    q = session.query(RadiusAuthError.id).filter(RadiusAuthError.occurred_at >= cutoff)
    # Prefer subscription-scoped; fall back to subscriber (auth errors are
    # sometimes only keyed to the subscriber before the sub is resolved).
    row = q.filter(RadiusAuthError.subscription_id == subscription.id).first()
    if row is not None:
        return True
    if subscription.subscriber_id is not None:
        row = q.filter(
            RadiusAuthError.subscriber_id == subscription.subscriber_id
        ).first()
    return row is not None


def _plant_is_up(
    session,
    node: NetworkDevice | None,
    cache: dict | None,
    *,
    now: datetime | None = None,
    warm_stale: bool | None = None,
) -> bool | None:
    """Is the customer's parent access node UP? (design §7.3 gate.)

    Reuses P1: a node with any online customer, or classified healthy /
    service_fault, is up (proof-of-life). Returns ``None`` when there is no node
    to judge (can't tell → don't blame the customer *or* the plant).

    ``cache`` (node_id -> bool|None) lets ``diagnose_many`` avoid re-resolving
    the same node's affected set per customer.

    The freshness signal reaches this customer-facing path through
    ``classify_node``: a node whose ``live_status`` nothing refreshes any more
    cannot classify ``service_fault``, so a dead warmer can no longer certify
    the plant "up" and push the blame onto the customer's router.
    """
    if node is None:
        return None
    if cache is not None and node.id in cache:
        return cache[node.id]
    impact = affected_customers(session, node=node)
    online_count = impact["online_count"]
    had_prior_life = impact["count"] > 0
    state = classify_node(
        node, online_count, had_prior_life, now=now, warm_stale=warm_stale
    )
    # healthy or service_fault ⟹ the node itself is reachable/serving (plant up);
    # node_outage / monitoring_fault ⟹ don't attribute to the last mile.
    up = state in (NODE_HEALTHY, NODE_SERVICE_FAULT)
    if cache is not None:
        cache[node.id] = up
    return up


def _diagnose_fiber(
    session,
    subscription: Subscription,
    path: CustomerPath,
    now: datetime,
    plant_cache: dict | None,
    warm_stale: bool | None = None,
) -> dict:
    ont = path.ont
    from app.services.network.ont_status import resolve_effective_ont_status

    effective = resolve_effective_ont_status(ont, now=now) if ont is not None else None
    ev: dict = {
        "rung": "fiber",
        "olt_status": getattr(ont.olt_status, "value", None) if ont else None,
        "effective_status": effective.status.value if effective else None,
        "status_retry_pending": effective.retry_pending if effective else False,
        "offline_reason": (
            getattr(ont.offline_reason, "value", None)
            if ont and ont.offline_reason is not None
            else None
        ),
        "onu_rx_signal_dbm": ont.onu_rx_signal_dbm if ont else None,
        "acs_last_inform_at": (
            ont.acs_last_inform_at.isoformat()
            if ont and ont.acs_last_inform_at
            else None
        ),
    }
    rx = ont.onu_rx_signal_dbm if ont else None

    # Rung: present at node? ONT offline on the OLT ⟹ not present.
    if ont is None or effective is None or not effective.is_online:
        if effective is not None and effective.retry_pending:
            return _result(UNKNOWN, MEDIUM_FIBER, rx, ev)
        plant_up = _plant_is_up(
            session, path.node, plant_cache, now=now, warm_stale=warm_stale
        )
        ev["plant_up"] = plant_up
        if plant_up is False:
            # The OLT/plant itself is dark — this is a node outage P1 owns, not
            # a last-mile call. Don't blame the customer's power (§7.3 inverse).
            return _result(UNKNOWN, MEDIUM_FIBER, rx, ev)
        # Plant up (or unknown) + ONT absent ⟹ customer side. offline_reason
        # refines the message but the verdict is `power` (§5 absent row).
        if ont is not None and ont.offline_reason == OnuOfflineReason.los:
            ev["note"] = "loss-of-signal: possible drop-fiber cut, may need tech"
        return _result(POWER, MEDIUM_FIBER, rx, ev)

    # Rung: link healthy? Present but Rx below floor ⟹ degraded.
    if rx is not None and rx < RX_SIGNAL_MIN_DBM:
        return _result(SIGNAL_DEGRADED, MEDIUM_FIBER, rx, ev)

    # Rung: CPE authenticating? ACS informing recently?
    acs = _aware(ont.acs_last_inform_at)
    acs_fresh = acs is not None and acs >= now - ACS_STALE_TTL
    ev["acs_fresh"] = acs_fresh
    if not acs_fresh:
        # ONT is up on the OLT, optical fine, but the router behind it isn't
        # talking to the ACS ⟹ reboot the router.
        return _result(ROUTER_OFFLINE, MEDIUM_FIBER, rx, ev)

    # Informing + good signal: reject vs never-attempted.
    reject = _has_recent_auth_reject(session, subscription, now)
    ev["recent_auth_reject"] = reject
    if reject:
        return _result(AUTH, MEDIUM_FIBER, rx, ev)
    return _result(CONFIG, MEDIUM_FIBER, rx, ev)


# UISP radio states that mean the radio is NOT associated to its AP.
_WIRELESS_ABSENT = frozenset({"disconnected", "vanished", None})
_WIRELESS_UNAUTH = "unauthorized"


def _diagnose_wireless(
    session,
    subscription: Subscription,
    path: CustomerPath,
    now: datetime,
    plant_cache: dict | None,
    warm_stale: bool | None = None,
) -> dict:
    radio = path.radio
    status = (radio.last_uisp_status or "").strip().lower() or None if radio else None
    ev: dict = {
        "rung": "wireless",
        "last_uisp_status": status,
        "rf_signal": None,  # design §4 gap: no RF value stored on prod
        "note": "wireless RF signal is not collected — link-signal rung unobservable",
    }

    if radio is None or status in _WIRELESS_ABSENT:
        plant_up = _plant_is_up(
            session, path.node, plant_cache, now=now, warm_stale=warm_stale
        )
        ev["plant_up"] = plant_up
        if plant_up is False:
            return _result(UNKNOWN, MEDIUM_WIRELESS, None, ev)
        # Radio not associated + AP up ⟹ customer-side (radio powered off /
        # knocked out of alignment). Verdict `power` (§5 absent row).
        return _result(POWER, MEDIUM_WIRELESS, None, ev)

    if status == _WIRELESS_UNAUTH:
        # Associated but the AP refuses it ⟹ auth/provisioning, operator-fix.
        return _result(AUTH, MEDIUM_WIRELESS, None, ev)

    # Associated (active). We can't check RF, so distinguish only reject vs
    # not-dialing; default to router_offline (radio up, router behind it may be
    # off) rather than claiming a signal verdict we can't back.
    reject = _has_recent_auth_reject(session, subscription, now)
    ev["recent_auth_reject"] = reject
    if reject:
        return _result(AUTH, MEDIUM_WIRELESS, None, ev)
    # No RADIUS attempt at all ⟹ not dialing (config); otherwise the radio is
    # up but service isn't ⟹ ask for a router reboot.
    return _result(ROUTER_OFFLINE, MEDIUM_WIRELESS, None, ev)


def diagnose_last_mile(
    session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
    plant_cache: dict | None = None,
    warm_stale: bool | None = None,
) -> dict:
    """Diagnose WHY one subscription is offline (design §5).

    Returns ``{verdict, medium, signal_dbm, evidence, customer_message,
    agent_action}``. Safe to call even when the customer is online — returns
    ``healthy`` in that case. ``plant_cache`` is an optional node_id->bool map
    for batch callers (see ``diagnose_many``), and ``warm_stale`` is the
    warmer's dead-man signal, resolved once per batch by those callers.
    """
    now = _now(now)
    if warm_stale is None:
        warm_stale = warmer_is_stale(now)

    # Session actually up? Then nothing is wrong — proof-of-life beats every
    # lower rung (design §0). Uses the same freshness window as P1.
    online = online_subscription_ids(session, [subscription.id], now=now)
    if subscription.id in online:
        return _result(
            HEALTHY, MEDIUM_UNKNOWN, None, {"rung": "session", "online": True}
        )

    path = resolve_customer_path(session, subscription)

    # Fiber if there's an ONT; wireless if there's a radio; else we have no CPE
    # telemetry below the session (NAS-only) and can't diagnose the last mile.
    if path.ont is not None:
        return _diagnose_fiber(
            session, subscription, path, now, plant_cache, warm_stale
        )
    if path.radio is not None:
        return _diagnose_wireless(
            session, subscription, path, now, plant_cache, warm_stale
        )

    return _result(
        UNKNOWN,
        MEDIUM_UNKNOWN,
        None,
        {
            "rung": "linkage",
            "gap": path.gap,
            "access_device_kind": path.access_device_kind,
            "note": "no ONT or radio linked — no last-mile telemetry below session",
        },
    )


def diagnose_many(
    session,
    subscription_ids,
    *,
    now: datetime | None = None,
) -> dict:
    """Diagnose a batch, keyed by subscription id (design §5 support view).

    Shares one ``plant_cache`` across the batch so a shared node's health is
    resolved once, not per customer. Missing/unknown subscription ids are
    skipped. TODO(perf): a set-based path resolver would beat N× resolve when a
    whole element's customers are diagnosed together — P1's ``affected_customers``
    already batches; wire it in when the outage console lands (P4).
    """
    ids = list(subscription_ids)
    if not ids:
        return {}
    subs = session.query(Subscription).filter(Subscription.id.in_(ids)).all()
    plant_cache: dict = {}
    now = _now(now)
    # One dead-man read for the whole batch, not one per customer.
    warm_stale = warmer_is_stale(now)
    return {
        sub.id: diagnose_last_mile(
            session,
            sub,
            now=now,
            plant_cache=plant_cache,
            warm_stale=warm_stale,
        )
        for sub in subs
    }
