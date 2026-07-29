"""Outage classifier — P1 core (design: docs/designs/OUTAGE_CLASSIFIER.md).

Turn the signals already on prod into one self-consistent judgement: *what is
actually down, how deep*. P1 uses two signals only:

  - **data plane** — a live ``RadiusActiveSession`` is proof a customer is
    ACTUALLY served. ``online(E) >= 1`` ⟹ E and everything upstream is UP
    (design §0/§2). One survivor vetoes "down".
  - **mgmt plane** — ``NetworkDevice.live_status`` (native warmer: up/down/
    unknown/problem), the combined reachability+agent signal today.

The two form a dependency ladder (design §1): a live session REQUIRES mgmt up,
so ``session up + mgmt down`` is physically impossible — the mgmt check is
lying, not the device. We never override that contradiction, we flag it
(``monitoring_fault``) so it gets self-healed.

Out of P1 scope (documented TODOs, later phases):
  - temporal ``baseline(E)`` — trigger on deviation, not ``== 0`` (design §2);
    P1 uses a coarse "has provisioned customers" prior-life denominator.
  - splitting mgmt back into separate ping vs snmp signals (design §1 table).
  - last-mile per-customer diagnoser (design §5, P2).
  - splice inference from co-failure / correlated Rx (design §6, P3).
  - maintenance-window suppression (design §7.8) and time-series debounce
    (design §7.6) — P1 is a point-in-time read.
  - admin console / selfcare surfaces / notify send-path (P4).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.services.topology.affected import (
    _dist_to_core,
    subscriptions_for_nodes,
)
from app.services.topology.live_status import trusted_live_status

logger = logging.getLogger(__name__)

# A session counts as proof-of-life only if it was updated recently — a stale
# row is not evidence the customer is online *now* (design §7.6 staleness).
# TODO(design §2): move to settings / per-element baseline once temporal
# history lands; 20min covers the interim-update cadence with headroom.
ONLINE_SESSION_TTL = timedelta(minutes=20)

# Below this many provisioned customers behind an element, survivors can't
# exist by chance, so we don't infer a plant outage — we degrade to
# per-customer last-mile (design §7.1, P2). P1 just lowers confidence.
SMALL_N_THRESHOLD = 3

# Node ladder classes (design §1).
HEALTHY = "healthy"
SERVICE_FAULT = "service_fault"  # up/up/down row: data-plane, NOT "area down"
MONITORING_FAULT = "monitoring_fault"  # impossible contradiction -> self-heal
NODE_OUTAGE = "node_outage"  # all planes consistently dark
UNKNOWN = "unknown"  # insufficient signal to call it


def _cutoff(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)) - ONLINE_SESSION_TTL


def _fresh(now: datetime | None):
    # coalesce: last_update is null until the first interim-update, so fall
    # back to session_start (non-null) for freshly-started sessions.
    return func.coalesce(
        RadiusActiveSession.last_update, RadiusActiveSession.session_start
    ) >= _cutoff(now)


def online_subscription_ids(
    session: Session, subscription_ids, *, now: datetime | None = None
) -> set:
    """Subset of ``subscription_ids`` with a FRESH live RADIUS session.

    The per-element proof-of-life primitive: which of these subscriptions are
    online right now (design §2). Empty in -> empty out.
    """
    ids = list(subscription_ids)
    if not ids:
        return set()
    rows = (
        session.query(RadiusActiveSession.subscription_id)
        .filter(
            RadiusActiveSession.subscription_id.in_(ids),
            _fresh(now),
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows if r[0] is not None}


def online_subscribers(
    session: Session, subscriber_ids, *, now: datetime | None = None
) -> set:
    """Subset of ``subscriber_ids`` online on ANY node — GLOBAL proof-of-life.

    Proof-of-life is global (design §7.4 failover/roaming): before calling a
    node's droppers "down", check whether they're online *anywhere* — if so
    it's a failover, zero customer impact, not an outage. Keyed by subscriber
    (not subscription) because a roaming customer's session may land on a
    different NAS but is the same person.
    """
    ids = list(subscriber_ids)
    if not ids:
        return set()
    rows = (
        session.query(RadiusActiveSession.subscriber_id)
        .filter(
            RadiusActiveSession.subscriber_id.in_(ids),
            _fresh(now),
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows if r[0] is not None}


def _mgmt_state(
    node,
    *,
    now: datetime | None = None,
    warm_stale: bool | None = None,
) -> str:
    """Node -> mgmt-plane state (up/down/unknown), with the freshness gate on.

    Today ``live_status`` is the *combined* reachability+agent signal. When the
    warmer starts publishing separate ping vs snmp signals (design §1), split
    this into two inputs and pass both to ``classify_node``; the impossible-row
    logic below already generalises.

    Reads through :func:`trusted_live_status` rather than the raw column: a
    ``live_status`` nothing is refreshing any more (device deactivated, checks
    disabled, warmer dead) must not keep asserting ``up`` here, because the
    ladder below turns a stale ``up`` into ``service_fault`` — "reachable but
    serving nobody, NOT an area outage" — which silently vetoes outage
    detection for every customer behind the node.
    """
    value = trusted_live_status(node, now=now, warm_stale=warm_stale)
    if value == "up":
        return "up"
    if value == "down":
        return "down"
    # "unknown", "problem" (legacy cached), None, unwarmed -> can't assert.
    return "unknown"


def classify_node(
    node: NetworkDevice,
    online_count: int,
    had_prior_life: bool,
    *,
    now: datetime | None = None,
    warm_stale: bool | None = None,
) -> str:
    """Per-node ladder state from mgmt-plane (live_status) + data-plane (online).

    Design §1 table, collapsed to the two P1 signals:

      online>0                 -> a customer is served THROUGH this node, so the
                                  node + everything upstream is UP (design §0).
                                  mgmt up  -> healthy.
                                  mgmt !up -> monitoring_fault: session up while
                                  ping/snmp says down is physically impossible;
                                  the check is lying — self-heal, never "down".
      online==0, had life:
                                  mgmt up   -> service_fault (up/up/down row):
                                  reachable but serving nobody it used to —
                                  PPPoE/RADIUS/upstream, NOT an area outage.
                                  mgmt down -> node_outage: all planes dark.
                                  mgmt unk  -> unknown (only one dark signal).
      online==0, no prior life -> unknown: dormant / small-N / never-provisioned,
                                  nothing to conclude.

    ``warm_stale`` is the warmer's dead-man signal. Pass it in from batch
    callers (one cache read for the whole sweep); left as ``None`` it is
    resolved per node.
    """
    mgmt = _mgmt_state(node, now=now, warm_stale=warm_stale)
    if online_count > 0:
        if mgmt == "up":
            return HEALTHY
        return MONITORING_FAULT
    if not had_prior_life:
        return UNKNOWN
    if mgmt == "up":
        return SERVICE_FAULT
    if mgmt == "down":
        return NODE_OUTAGE
    return UNKNOWN


def _confidence(affected_before: int, survivors_elsewhere: bool) -> str:
    """Coarse confidence for a localized boundary (design §3: f(N, ...)).

    TODO(design §3): fold in baseline-deviation and signal-agreement once the
    temporal baseline (§2) and split ping/snmp signals (§1) exist.
    """
    if affected_before < SMALL_N_THRESHOLD:
        return "low"  # too few customers to distinguish plant from last-mile
    if survivors_elsewhere:
        return "high"  # a live peer proves upstream up -> boundary is real
    return "medium"  # nobody online anywhere -> could be a wider/upstream fault


def localize_outage(
    session: Session,
    node_ids,
    *,
    now: datetime | None = None,
    warm_stale: bool | None = None,
) -> dict | None:
    """Deepest dark-under-live node in a failure domain (design §3).

    Given the candidate ``node_ids`` (e.g. ``affected_customers()["node_ids"]``),
    find the DEEPEST element whose online customers collapsed to zero while a
    peer/parent still has survivors — that is the failure boundary. Returns
    ``None`` when no such boundary exists (every element with customers still
    has at least one online — nothing to declare).

    Result::

        {failure_node, class, affected_online_before, affected_now, confidence}

    ``affected_online_before`` is a COARSE prior-life denominator (provisioned
    active customers behind the node); the real temporal baseline is a TODO
    (design §2). ``class`` comes from ``classify_node`` with online forced to 0.
    """
    node_ids = list(node_ids)
    if not node_ids:
        return None

    by_node = subscriptions_for_nodes(session, node_ids)
    all_sub_ids = {s.id for subs in by_node.values() for s in subs}
    online_ids = online_subscription_ids(session, all_sub_ids, now=now)

    provisioned_by_node = {nid: len(by_node.get(nid, [])) for nid in node_ids}
    online_by_node = {
        nid: sum(1 for s in by_node.get(nid, []) if s.id in online_ids)
        for nid in node_ids
    }

    # A node is a suspect only if it had customers (coarse prior life) and none
    # are online now (design §2: collapse). == 0 is P1's proxy for "well below
    # baseline"; temporal baseline (§2) refines this.
    dark = [
        nid
        for nid in node_ids
        if provisioned_by_node[nid] > 0 and online_by_node[nid] == 0
    ]
    if not dark:
        return None

    survivors_elsewhere = any(online_by_node[nid] > 0 for nid in node_ids)

    # Deepest = furthest from a declared core/border root. Nodes without an
    # agreeing authoritative path sort shallowest; observations cannot deepen
    # an outage boundary on their own.
    dist = _dist_to_core(session)

    def _depth_key(nid):
        d = dist.get(nid)
        return (d is not None, d if d is not None else -1)

    failure_node = max(dark, key=_depth_key)
    affected_before = provisioned_by_node[failure_node]

    node = session.get(NetworkDevice, failure_node)
    cls = (
        classify_node(node, 0, had_prior_life=True, now=now, warm_stale=warm_stale)
        if node is not None
        else UNKNOWN
    )
    return {
        "failure_node": failure_node,
        "class": cls,
        "affected_online_before": affected_before,
        "affected_now": 0,
        "confidence": _confidence(affected_before, survivors_elsewhere),
    }
