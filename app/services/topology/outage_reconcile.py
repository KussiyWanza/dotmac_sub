"""Detected-outage incident reconcile (design §7.6).

The classifier (``health_classifier``) is a point-in-time read: it says what is
dark *right now*. That is too twitchy to notify on — a single missed poll or a
reboot would open and close an "outage" every few minutes. This module adds the
TIME dimension: a discover-reconcile loop that debounces the classifier's
verdicts into a trustworthy incident with a lifecycle, so notify / ticket /
restore / MTTR can later attach to a persisted spine instead of a transient read.

Each pass (mirrors the radius / outage-autodetect sweeps — advisory-lock
single-flight in the task wrapper, per-item savepoint here so one bad candidate
never poisons the run):

1. **Verdicts** — classify every active node with the online
   overlay (``affected.subscriptions_for_nodes`` + proof-of-life) and mgmt
   ``live_status``; keep the nodes currently ``node_outage``. Localize each dark
   node to the deepest dark-under-live boundary (``localize_outage``) -> a
   candidate outage per dark component (root_node, basestation, affected_count,
   confidence, classification).
2. **Identity** — find-or-open an OPEN classifier incident per candidate by the
   §7.6 identity rule (basestation, else exact root, else connected-dark
   component), RE-POINTING the root on localization drift rather than opening a
   duplicate.
3. **Debounce up** — ``suspected -> confirmed`` once the suspicion has persisted
   ``W_confirm(affected_count)`` (impact-scaled), else it stays suspected; a
   suspected incident whose node recovered before W_confirm is ``discarded``
   (the false-positive suppression — no confirmed event ever fires).
4. **Debounce down** — ``confirmed -> clearing`` on recovery (stamp cleared_at),
   ``clearing -> resolved`` once recovery is sustained past ``W_resolve``, and
   ``clearing -> confirmed`` (reopen) if it re-darkens inside the resolve window
   (hysteresis).

Firing stays GATED: this loop only persists the lifecycle and fans lifecycle
events into the existing webhook machinery. No customer / ticket notification is
sent here. MTTR is derivable as ``resolved_at - confirmed_at``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network_monitoring import NetworkDevice, OutageIncident
from app.services.device_operational_status import warmer_is_stale
from app.services.topology.affected import (
    _dist_to_core,
    affected_customers,
    downstream_nodes,
    forwarding_graph_projection,
    subscriptions_for_nodes,
)
from app.services.topology.health_classifier import (
    NODE_OUTAGE,
    classify_node,
    localize_outage,
    online_subscription_ids,
)
from app.services.topology.outage import (
    CLASSIFIER_OPEN_STATUSES,
    CLASSIFIER_SOURCE,
    OutageStatus,
    confirm_incident,
    discard_incident,
    find_open_classifier_incident,
    open_classifier_incident,
    reopen_incident,
    repoint_root,
    resolve_classifier_incident,
    start_clearing,
    update_classifier_snapshot,
)

logger = logging.getLogger(__name__)

# Single-flight guard (pg advisory lock via db_session_adapter.advisory_lock,
# same pattern as the radius reconcile). "oir" = Outage Incident Reconcile.
ADVISORY_LOCK_KEY = 0x6F_69_72

# W_confirm (suspected -> confirmed) is scaled by impact (design §7.6 decision 2):
# a large blast radius is unambiguous and confirms immediately; a small one waits
# out flaps. All settings-backed (SettingDomain.network_monitoring) with these
# fallbacks.
CONFIRM_SECONDS_LARGE_DEFAULT = 0  # affected_count >= threshold_large -> now
CONFIRM_SECONDS_MED_DEFAULT = 360  # threshold_med .. threshold_large-1
CONFIRM_SECONDS_SMALL_DEFAULT = 600  # < threshold_med
CONFIRM_THRESHOLD_LARGE_DEFAULT = 20
CONFIRM_THRESHOLD_MED_DEFAULT = 5
# W_resolve (clearing -> resolved): sustained-recovery window, fixed default.
RESOLVE_SECONDS_DEFAULT = 300

# Coarse classifier confidence label -> stored Float (the model column is Float;
# localize_outage still returns the labelled band).
_CONFIDENCE_SCORE = {"low": 0.3, "medium": 0.6, "high": 0.9}


def _setting_int(session: Session, key: str, default: int) -> int:
    """Advisory settings read (never fails the pass over a bad setting)."""
    try:
        from app.services.settings_spec import resolve_value

        raw = resolve_value(session, SettingDomain.network_monitoring, key)
        if raw is None:
            return default
        return int(str(raw))
    except Exception:  # settings are advisory
        return default


@dataclass(frozen=True)
class _Windows:
    confirm: dict
    resolve: int


def _resolve_windows(session: Session) -> _Windows:
    small = max(
        _setting_int(
            session, "outage_confirm_seconds_small", CONFIRM_SECONDS_SMALL_DEFAULT
        ),
        0,
    )
    med = max(
        _setting_int(
            session, "outage_confirm_seconds_med", CONFIRM_SECONDS_MED_DEFAULT
        ),
        0,
    )
    large = max(
        _setting_int(
            session, "outage_confirm_seconds_large", CONFIRM_SECONDS_LARGE_DEFAULT
        ),
        0,
    )
    threshold_med = max(
        _setting_int(
            session, "outage_confirm_threshold_med", CONFIRM_THRESHOLD_MED_DEFAULT
        ),
        1,
    )
    threshold_large = max(
        _setting_int(
            session, "outage_confirm_threshold_large", CONFIRM_THRESHOLD_LARGE_DEFAULT
        ),
        threshold_med,
    )
    resolve = max(
        _setting_int(session, "outage_resolve_seconds", RESOLVE_SECONDS_DEFAULT), 0
    )
    return _Windows(
        confirm={
            "small": small,
            "med": med,
            "large": large,
            "threshold_med": threshold_med,
            "threshold_large": threshold_large,
        },
        resolve=resolve,
    )


def confirm_window_seconds(
    affected_count: int,
    *,
    small: int,
    med: int,
    large: int,
    threshold_med: int,
    threshold_large: int,
) -> int:
    """W_confirm in seconds for a given blast radius (design §7.6 decision 2).

    ``affected_count >= threshold_large`` confirms this cycle (``large``, 0s by
    default); ``threshold_med..threshold_large-1`` waits ``med``; below
    ``threshold_med`` waits ``small``.
    """
    if affected_count >= threshold_large:
        return large
    if affected_count >= threshold_med:
        return med
    return small


def _elapsed_seconds(now: datetime, ts: datetime | None) -> float:
    """Seconds since ``ts`` (tz-coerced). A missing stamp reads as 0 elapsed so a
    transition never fires off a null timestamp."""
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


@dataclass
class _Candidate:
    root_node: NetworkDevice
    basestation_id: object
    affected_count: int
    confidence: float | None
    classification: str
    # Nodes forming this candidate's connected-dark component (current deepest +
    # every dark node that localized to it + its downstream span) — the set the
    # identity rule matches a drifted incident root against.
    component_node_ids: set


def _candidate_outages(session: Session, now: datetime) -> dict:
    """Current classifier candidate outages, keyed by boundary node id.

    Two detector arms feed one lifecycle:

    1. **Dark nodes** — one online-overlay pass over every active node -> the
       nodes currently ``node_outage`` -> localize each to its deepest
       dark-under-live boundary.
    2. **Wireless clusters** — an AP whose subscriber radios are down en masse
       while nobody behind it is online. The AP itself often stays mgmt-UP
       (``service_fault`` to the ladder), so the dark arm can never see it —
       this arm carries the retired auto-detect scan's unique coverage, but
       STATE-based (current radio state, not transition diffs) so recovery
       debounces down through the ordinary lifecycle.

    Whole-graph maps computed ONCE and reused (no per-candidate BFS).
    """
    graph = forwarding_graph_projection(session)
    adjacency = graph.adjacency
    dist = _dist_to_core(
        session,
        adjacency=adjacency,
        root_ids=graph.root_device_ids,
    )
    candidates: dict = {}
    # One dead-man read for the whole sweep — a frozen live_status must not
    # certify a node "reachable but serving nobody" here either.
    warm_stale = warmer_is_stale(now)

    nodes = session.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()
    dark_nodes: list[NetworkDevice] = []
    online_by_node: dict = {}
    provisioned: dict = {}
    if nodes:
        node_ids = [n.id for n in nodes]
        by_node = subscriptions_for_nodes(session, node_ids)
        all_sub_ids = {s.id for subs in by_node.values() for s in subs}
        online_ids = online_subscription_ids(session, all_sub_ids, now=now)
        online_by_node = {
            nid: sum(1 for s in by_node.get(nid, []) if s.id in online_ids)
            for nid in node_ids
        }
        provisioned = {nid: len(by_node.get(nid, [])) for nid in node_ids}
        dark_nodes = [
            n
            for n in nodes
            if classify_node(
                n,
                online_by_node[n.id],
                provisioned[n.id] > 0,
                now=now,
                warm_stale=warm_stale,
            )
            == NODE_OUTAGE
        ]

    dark_ids = {n.id for n in dark_nodes}
    for dn in dark_nodes:
        # Localize among the ``node_outage`` nodes in this dark node's downstream
        # span only — not the full scope. localize_outage reads "dark" as
        # proof-of-life collapse alone, so a mgmt-UP node merely serving nobody
        # (service_fault) would otherwise be mistaken for the outage boundary.
        # Restricting to node_outage nodes keeps the boundary a genuine outage.
        scope = downstream_nodes(session, dn, dist=dist, adjacency=adjacency)
        dark_in_scope = [nid for nid in scope if nid in dark_ids]
        loc = localize_outage(session, dark_in_scope, now=now, warm_stale=warm_stale)
        if loc is None:
            continue
        fid = loc["failure_node"]
        if fid in candidates:
            candidates[fid].component_node_ids.update(scope)
            candidates[fid].component_node_ids.add(dn.id)
            continue
        fnode = session.get(NetworkDevice, fid)
        if fnode is None:
            continue
        impact = affected_customers(session, node=fnode, dist=dist, adjacency=adjacency)
        # The identity-match component: everything below the current boundary,
        # plus this dark node's full span (which may sit ABOVE the boundary when
        # drift moved the root deeper) — so a drifted incident root, in either
        # direction, is still recognised as the same outage.
        component = set(
            downstream_nodes(session, fnode, dist=dist, adjacency=adjacency)
        )
        component.update(scope)
        component.add(dn.id)
        component.add(fid)
        candidates[fid] = _Candidate(
            root_node=fnode,
            basestation_id=fnode.pop_site_id,
            affected_count=impact["count"],
            confidence=_CONFIDENCE_SCORE.get(loc["confidence"]),
            classification=loc["class"],
            component_node_ids=component,
        )
    _add_wireless_cluster_candidates(
        session,
        candidates,
        dist=dist,
        adjacency=adjacency,
        online_by_node=online_by_node,
        provisioned=provisioned,
    )
    return candidates


# UISP radio statuses that count as "up" — NULL covers rows written before the
# status column existed (same reading as customer_path / affected).
_RADIO_UP_STATUSES = (None, "active")

# Wireless-cluster thresholds reuse the auto-detect scan's setting keys so
# existing operator tuning carries over.
MIN_AFFECTED_DEFAULT = 3
MIN_FRACTION_PCT_DEFAULT = 40

RADIO_CLUSTER_CLASSIFICATION = "radio_cluster"
_RADIO_CLUSTER_CONFIDENCE = _CONFIDENCE_SCORE["medium"]


def _add_wireless_cluster_candidates(
    session: Session,
    candidates: dict,
    *,
    dist: dict,
    adjacency: dict,
    online_by_node: dict,
    provisioned: dict,
) -> None:
    """Add AP-scope candidates for wireless clusters the dark arm can't see.

    An AP trips when BOTH thresholds pass — at least ``min_affected`` of its
    subscriber-linked radios are currently down AND they are at least
    ``min_fraction`` of its radio population — and nobody behind it holds a
    live session (proof-of-life veto: one online customer kills the
    candidate, chronic-churn noise included). A mgmt-DOWN AP is left to the
    dark arm, which owns that shape. The online/provisioned maps come from
    the population pass so the veto shares its time base (``now``).
    """
    from app.models.network import CPEDevice, DeviceStatus

    min_affected = max(
        _setting_int(session, "outage_autodetect_min_affected", MIN_AFFECTED_DEFAULT),
        1,
    )
    min_fraction = (
        max(
            min(
                _setting_int(
                    session,
                    "outage_autodetect_min_fraction_pct",
                    MIN_FRACTION_PCT_DEFAULT,
                ),
                100,
            ),
            1,
        )
        / 100.0
    )

    per_ap: dict = {}
    for parent_id, uisp_status in (
        session.query(CPEDevice.parent_network_device_id, CPEDevice.last_uisp_status)
        .filter(
            CPEDevice.parent_network_device_id.isnot(None),
            CPEDevice.subscriber_id.isnot(None),
            CPEDevice.status == DeviceStatus.active,
        )
        .all()
    ):
        if uisp_status == "vanished":
            continue
        total, down = per_ap.get(parent_id, (0, 0))
        per_ap[parent_id] = (
            total + 1,
            down + (0 if uisp_status in _RADIO_UP_STATUSES else 1),
        )

    for ap_id, (total, down) in per_ap.items():
        if ap_id in candidates:
            continue  # the dark arm already owns this boundary
        if total <= 0 or down < min_affected or (down / total) < min_fraction:
            continue
        try:
            ap = session.get(NetworkDevice, ap_id)
            if ap is None or not ap.is_active:
                continue
            if (getattr(ap, "live_status", "") or "").strip().lower() == "down":
                continue  # mgmt-dark AP is the dark arm's shape
            if provisioned.get(ap.id, 0) <= 0 or online_by_node.get(ap.id, 0) > 0:
                continue  # dormant scope, or proof of life vetoes the outage
            component = set(
                downstream_nodes(session, ap, dist=dist, adjacency=adjacency)
            )
            component.add(ap.id)
            candidates[ap.id] = _Candidate(
                root_node=ap,
                basestation_id=ap.pop_site_id,
                affected_count=provisioned[ap.id],
                confidence=_RADIO_CLUSTER_CONFIDENCE,
                classification=RADIO_CLUSTER_CLASSIFICATION,
                component_node_ids=component,
            )
        except Exception:  # one bad AP must not poison the pass
            logger.exception(
                "outage_reconcile_wireless_candidate_failed", extra={"ap": str(ap_id)}
            )


def reconcile_detected_outages(
    session: Session, *, now: datetime | None = None
) -> dict[str, int]:
    """One reconcile pass over the classifier-driven incident lifecycle.

    Idempotent across (serialized) runs: identity dedupes candidates onto their
    existing incident, and every transition is guarded by the incident's current
    state, so a re-run with the same signals is a no-op. Per-item savepoints keep
    one failing candidate from rolling back the whole pass.
    """
    now = now or datetime.now(UTC)
    windows = _resolve_windows(session)
    counters: dict[str, int] = {
        "candidates": 0,
        "open_incidents": 0,
        "suspected_opened": 0,
        OutageStatus.confirmed.value: 0,
        OutageStatus.discarded.value: 0,
        OutageStatus.clearing.value: 0,
        OutageStatus.resolved.value: 0,
        "reopened": 0,
        "rerooted": 0,
        "errors": 0,
    }

    candidates = _candidate_outages(session, now)
    counters["candidates"] = len(candidates)

    # Snapshot the open classifier incidents BEFORE mutating — anything a
    # candidate does not claim this pass is a recovery to debounce down.
    open_incidents = (
        session.query(OutageIncident)
        .filter(
            OutageIncident.detection_source == CLASSIFIER_SOURCE,
            OutageIncident.status.in_(CLASSIFIER_OPEN_STATUSES),
        )
        .all()
    )
    counters["open_incidents"] = len(open_incidents)

    used: set = set()

    # --- 1. dark candidates: find-or-open, re-point on drift, debounce up ------
    for root_id, cand in candidates.items():
        try:
            with session.begin_nested():
                incident = find_open_classifier_incident(
                    session,
                    basestation_id=cand.basestation_id,
                    root_node_id=root_id,
                    component_node_ids=cand.component_node_ids,
                    exclude_ids=used,
                )
                if incident is None:
                    incident = open_classifier_incident(
                        session,
                        root_node=cand.root_node,
                        basestation_id=cand.basestation_id,
                        affected_count=cand.affected_count,
                        confidence=cand.confidence,
                        classification=cand.classification,
                        now=now,
                    )
                    counters["suspected_opened"] += 1
                else:
                    # Re-point a localized root on drift. A site id can remain
                    # the incident identity while the exact failure node moves;
                    # a pure site-scoped incident has no root and stays site-only.
                    if incident.root_node_id is not None and repoint_root(
                        session, incident, cand.root_node
                    ):
                        counters["rerooted"] += 1
                    update_classifier_snapshot(
                        incident,
                        affected_count=cand.affected_count,
                        confidence=cand.confidence,
                        classification=cand.classification,
                    )
                    # Re-darkened inside the resolve window -> reopen (hysteresis).
                    if incident.status == OutageStatus.clearing.value:
                        reopen_incident(session, incident)
                        counters["reopened"] += 1
                used.add(incident.id)

                # Debounce up: suspected -> confirmed once W_confirm has elapsed.
                if incident.status == OutageStatus.suspected.value:
                    window = confirm_window_seconds(
                        incident.affected_count, **windows.confirm
                    )
                    if _elapsed_seconds(now, incident.suspected_at) >= window:
                        confirm_incident(session, incident, now=now)
                        counters[OutageStatus.confirmed.value] += 1
        except Exception:  # noqa: BLE001 - one bad candidate must not poison the run
            counters["errors"] += 1
            logger.exception(
                "outage_reconcile_candidate_failed", extra={"root_node": str(root_id)}
            )

    # --- 2. recovered incidents (no current candidate): debounce down ---------
    for incident in open_incidents:
        if incident.id in used:
            continue
        try:
            with session.begin_nested():
                if incident.status == OutageStatus.suspected.value:
                    # Recovered before W_confirm -> false positive, discard.
                    discard_incident(session, incident)
                    counters[OutageStatus.discarded.value] += 1
                elif incident.status == OutageStatus.confirmed.value:
                    start_clearing(session, incident, now=now)
                    counters[OutageStatus.clearing.value] += 1
                elif incident.status == OutageStatus.clearing.value:
                    if _elapsed_seconds(now, incident.cleared_at) >= windows.resolve:
                        resolve_classifier_incident(session, incident, now=now)
                        counters[OutageStatus.resolved.value] += 1
        except Exception:  # noqa: BLE001
            counters["errors"] += 1
            logger.exception(
                "outage_reconcile_recovery_failed",
                extra={"incident": str(incident.id)},
            )

    logger.info("outage_reconcile: %s", counters)
    return counters
