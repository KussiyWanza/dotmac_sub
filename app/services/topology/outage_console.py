"""Outage console read-model — P4 surface (design §P4, docs/designs/OUTAGE_CLASSIFIER.md).

The classifier-driven counterpart to the *manual* outage console: instead of an
operator declaring an incident against infrastructure, this reads the P1/P2/P3
engines and shows what is *actually* down right now, how deep, and why.

  - ``network_health_summary`` — run P1 ``classify_node`` across every pingable
    infra node (OLT / NAS / AP / BTS-router); counts by class + the not-healthy
    nodes with their affected/online figures and (for faults) the localized
    boundary. The console's top-level feed.
  - ``active_outages`` — just the localized failure boundaries (deepest
    dark-under-live, P1 ``localize_outage``), deduped, with a SEPARATE
    monitoring-fault bucket: a session-up-while-ping-down contradiction is a
    broken check to self-heal, NOT an outage (design §1).
  - ``outage_detail`` — one domain drill-in: each session-down customer's P2
    ``diagnose_last_mile`` verdict, plus P3 predictive branch alerts
    (``infer_branches`` / ``detect_rx_droop``) when the node is an OLT.

Pure read service. It reuses the batching already in ``affected_customers`` /
``diagnose_many`` and CAPS large sweeps (logging when it does — no silent
truncation). Nothing here writes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.models.network_monitoring import DeviceRole, DeviceType, NetworkDevice
from app.services.device_operational_status import warmer_is_stale
from app.services.topology.affected import affected_customers
from app.services.topology.health_classifier import (
    HEALTHY,
    MONITORING_FAULT,
    NODE_OUTAGE,
    SERVICE_FAULT,
    UNKNOWN,
    classify_node,
    localize_outage,
)
from app.services.topology.last_mile import diagnose_many
from app.services.topology.splice_inference import detect_rx_droop, infer_branches

logger = logging.getLogger(__name__)

# Infra roles we ping/classify — everything that is a shared node. CPE is the
# customer's own device (P2 territory), never a plant node, so it's excluded.
_INFRA_ROLES = frozenset(
    {
        DeviceRole.core,
        DeviceRole.distribution,
        DeviceRole.access,
        DeviceRole.aggregation,
        DeviceRole.edge,
    }
)

# Bound the per-request sweep. Real fleets are in the low hundreds of infra
# nodes; beyond this we cap and LOG rather than melt the request (design: no
# silent truncation). Raise if the fleet outgrows it.
MAX_NODES = 750

# Cap per-domain last-mile diagnosis on the drill-in — a huge domain would fan
# out N× resolve_customer_path. Log when we clip.
MAX_DETAIL_CUSTOMERS = 300

# Classes that represent a real, actionable fault (vs healthy / unknown /
# monitoring_fault which are handled separately).
_FAULT_CLASSES = frozenset({NODE_OUTAGE, SERVICE_FAULT})

_AREA_OUTAGE_VERDICT = "area_outage"
_NETWORK_MEDIUM = "network"


def _pingable_infra_nodes(session: Session) -> list[NetworkDevice]:
    rows = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.ping_enabled.is_(True),
            NetworkDevice.role.in_(_INFRA_ROLES),
        )
        .order_by(NetworkDevice.name)
        .limit(MAX_NODES + 1)
        .all()
    )
    if len(rows) > MAX_NODES:
        logger.warning(
            "outage_console: %d pingable infra nodes exceeds cap %d — "
            "classifying the first %d by name only",
            len(rows),
            MAX_NODES,
            MAX_NODES,
        )
        rows = rows[:MAX_NODES]
    return rows


def _node_medium(node: NetworkDevice) -> str:
    """Coarse access medium for a node (design §4 asymmetry).

    Fiber if it's an OLT; wireless if it's an access point / BTS radio; else
    network for a shared NAS/router node; otherwise unknown. Only used for
    display + the fiber/wireless precision note.
    """
    if node.matched_device_type == "olt":
        return "fiber"
    if node.device_type == DeviceType.access_point:
        return "wireless"
    if node.matched_device_type == "nas" or node.device_type == DeviceType.router:
        return _NETWORK_MEDIUM
    return "unknown"


def _customer_display_verdict(node: NetworkDevice, cls: str, verdict: dict) -> dict:
    """Operator-facing customer verdict for an outage-domain table row.

    P2 last-mile diagnosis is intentionally conservative: a NAS-only affected
    subscription has no ONT/radio telemetry below the PPPoE session, so it
    returns unknown. In a drill-in for a confirmed shared-node outage, the
    operator-facing answer is still known: the customer is affected by the
    selected network node being down.
    """
    evidence = verdict.get("evidence") or {}
    if (
        cls == NODE_OUTAGE
        and verdict.get("verdict") == UNKNOWN
        and evidence.get("access_device_kind") == "nas"
    ):
        display = dict(verdict)
        display["verdict"] = _AREA_OUTAGE_VERDICT
        display["medium"] = _NETWORK_MEDIUM
        display["customer_message"] = (
            f"Affected by shared network outage at {node.name}."
        )
        display["agent_action"] = f"network_team_restore_node - restore {node.name}"
        display["evidence"] = {
            **evidence,
            "raw_verdict": verdict.get("verdict"),
            "display_override": "shared_node_outage",
        }
        return display
    return verdict


def _node_brief(node: NetworkDevice, impact: dict, cls: str) -> dict:
    return {
        "node_id": node.id,
        "name": node.name,
        "role": node.role.value if node.role else None,
        "medium": _node_medium(node),
        "live_status": node.live_status,
        "class": cls,
        "count": impact["count"],
        "online_count": impact["online_count"],
    }


def network_health_summary(session: Session, *, now: datetime | None = None) -> dict:
    """Classify every pingable infra node; counts by class + the not-healthy set.

    Returns::

        {
          "counts": {healthy, service_fault, node_outage, monitoring_fault,
                     unknown},
          "total_nodes": int,
          "not_healthy": [ {node_id, name, role, medium, live_status, class,
                            count, online_count, boundary?}, ... ],
        }

    ``boundary`` (present only for fault classes) is the P1 ``localize_outage``
    result for that node's domain — the deepest dark-under-live element.
    """
    nodes = _pingable_infra_nodes(session)
    counts = {
        HEALTHY: 0,
        SERVICE_FAULT: 0,
        NODE_OUTAGE: 0,
        MONITORING_FAULT: 0,
        UNKNOWN: 0,
    }
    not_healthy: list[dict] = []

    warm_stale = warmer_is_stale(now)
    for node in nodes:
        impact = affected_customers(session, node=node)
        cls = classify_node(
            node,
            impact["online_count"],
            had_prior_life=impact["count"] > 0,
            now=now,
            warm_stale=warm_stale,
        )
        counts[cls] = counts.get(cls, 0) + 1
        if cls == HEALTHY:
            continue
        brief = _node_brief(node, impact, cls)
        if cls in _FAULT_CLASSES:
            brief["boundary"] = localize_outage(
                session, impact["node_ids"], now=now, warm_stale=warm_stale
            )
        not_healthy.append(brief)

    # Surface the worst first: outages, then service faults, then the rest.
    _order = {NODE_OUTAGE: 0, SERVICE_FAULT: 1, MONITORING_FAULT: 2, UNKNOWN: 3}
    not_healthy.sort(key=lambda b: (_order.get(b["class"], 9), -b["count"]))

    return {
        "counts": counts,
        "total_nodes": len(nodes),
        "not_healthy": not_healthy,
    }


def active_outages(session: Session, *, now: datetime | None = None) -> dict:
    """Localized failure boundaries + the self-heal (monitoring-fault) queue.

    ``outages`` are deduped by boundary node (several parents can localize to
    the same deepest element). ``monitoring_faults`` are the impossible
    contradictions — session up while ping/snmp says down — which are NOT
    outages but broken checks to self-heal (design §1).

    Returns ``{outages: [...], monitoring_faults: [...]}``.
    """
    summary = network_health_summary(session, now=now)

    outages_by_boundary: dict[uuid.UUID, dict] = {}
    monitoring_faults: list[dict] = []

    for brief in summary["not_healthy"]:
        if brief["class"] == MONITORING_FAULT:
            monitoring_faults.append(
                {
                    "node_id": brief["node_id"],
                    "name": brief["name"],
                    "online_count": brief["online_count"],
                    "live_status": brief["live_status"],
                    "note": "session up while ping/snmp reports down — "
                    "self-heal the check, not an outage",
                }
            )
            continue
        if brief["class"] not in _FAULT_CLASSES:
            continue
        boundary = brief.get("boundary")
        # No boundary means every element with customers still has a survivor —
        # a service_fault with nothing localized. Anchor it on the node itself
        # so the operator still sees it.
        bnode = boundary["failure_node"] if boundary else brief["node_id"]
        existing = outages_by_boundary.get(bnode)
        row = {
            "failure_node": bnode,
            "name": brief["name"],
            "class": (boundary["class"] if boundary else brief["class"]),
            "medium": brief["medium"],
            "affected": (
                boundary["affected_online_before"] if boundary else brief["count"]
            ),
            "online_now": brief["online_count"],
            "confidence": boundary["confidence"] if boundary else "low",
            "localized": boundary is not None,
        }
        # Keep the row with the larger affected footprint if two collide.
        if existing is None or row["affected"] > existing["affected"]:
            outages_by_boundary[bnode] = row

    outages = sorted(outages_by_boundary.values(), key=lambda r: -r["affected"])
    return {"outages": outages, "monitoring_faults": monitoring_faults}


def _olt_pon_port_ids(session: Session, olt_device_id: uuid.UUID) -> list[uuid.UUID]:
    """Distinct PON ports carrying ONTs on this OLT (P3 keys on ont pon_port)."""
    rows = session.execute(
        select(OntUnit.pon_port_id)
        .where(
            OntUnit.olt_device_id == olt_device_id,
            OntUnit.pon_port_id.is_not(None),
        )
        .distinct()
    ).all()
    return [r[0] for r in rows if r[0] is not None]


def _predictive_branches(
    session: Session, node: NetworkDevice, *, now: datetime | None
) -> dict:
    """P3 branch alerts for an OLT node — co-failure + correlated-Rx droop.

    Empty for non-OLT nodes (wireless localizes only to the BTS — design §4).
    """
    if node.matched_device_type != "olt" or node.matched_device_id is None:
        return {"co_failure": [], "rx_droop": []}
    co_failure: list[dict] = []
    rx_droop: list[dict] = []
    for pon_id in _olt_pon_port_ids(session, node.matched_device_id):
        for b in infer_branches(session, pon_id, now=now):
            co_failure.append({"pon_port_id": pon_id, **b})
        for d in detect_rx_droop(session, pon_id, now=now):
            rx_droop.append({"pon_port_id": pon_id, **d})
    return {"co_failure": co_failure, "rx_droop": rx_droop}


def outage_detail(
    session: Session, node_id: uuid.UUID, *, now: datetime | None = None
) -> dict | None:
    """One domain drill-in: per-customer P2 verdicts + P3 predictive alerts.

    Returns ``None`` if the node doesn't exist. Otherwise::

        {
          "node": {id, name, role, medium, live_status},
          "class": <P1 class>,
          "count", "online_count",
          "boundary": <localize_outage or None>,
          "customers": [ {subscription_id, subscriber_name, online,
                          **diagnose_last_mile} ... ],   # session-down first
          "capped": bool,
          "predictive": {co_failure: [...], rx_droop: [...]},
        }
    """
    node = session.get(NetworkDevice, node_id)
    if node is None:
        return None

    impact = affected_customers(session, node=node)
    warm_stale = warmer_is_stale(now)
    cls = classify_node(
        node,
        impact["online_count"],
        had_prior_life=impact["count"] > 0,
        now=now,
        warm_stale=warm_stale,
    )

    subs = impact["subscriptions"]
    capped = False
    if len(subs) > MAX_DETAIL_CUSTOMERS:
        logger.warning(
            "outage_console: domain for node %s has %d customers > cap %d — "
            "diagnosing the first %d",
            node_id,
            len(subs),
            MAX_DETAIL_CUSTOMERS,
            MAX_DETAIL_CUSTOMERS,
        )
        subs = subs[:MAX_DETAIL_CUSTOMERS]
        capped = True

    verdicts = diagnose_many(session, [s.id for s in subs], now=now)
    customers = []
    for s in subs:
        v = _customer_display_verdict(node, cls, verdicts.get(s.id, {}))
        online = v.get("verdict") == "healthy"
        customers.append(
            {
                "subscription_id": s.id,
                "subscriber_name": (
                    f"{s.subscriber.first_name} {s.subscriber.last_name}"
                    if s.subscriber
                    else "—"
                ),
                "online": online,
                "verdict": v.get("verdict"),
                "medium": v.get("medium"),
                "signal_dbm": v.get("signal_dbm"),
                "customer_message": v.get("customer_message"),
                "agent_action": v.get("agent_action"),
                "evidence": v.get("evidence"),
            }
        )
    # Session-down (the actionable ones) first, then by verdict for grouping.
    customers.sort(key=lambda c: (c["online"], c["verdict"] or ""))

    return {
        "node": {
            "id": node.id,
            "name": node.name,
            "role": node.role.value if node.role else None,
            "medium": _node_medium(node),
            "live_status": node.live_status,
        },
        "class": cls,
        "count": impact["count"],
        "online_count": impact["online_count"],
        "boundary": localize_outage(
            session, impact["node_ids"], now=now, warm_stale=warm_stale
        ),
        "customers": customers,
        "capped": capped,
        "predictive": _predictive_branches(session, node, now=now),
    }
