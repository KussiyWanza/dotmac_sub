"""Customer 360 network-path read projection.

network.access_path owns path identity, ordering, and gaps; observation owners
own each hop's state and freshness; ui.status_presentation owns label, tone,
and icon meaning. This read owner composes those facts into the shared
NetworkGraphView and the serving-endpoint presentation the admin customer
surfaces render. It makes no topology, health, outage, or notification
decision, performs no device I/O, and never manufactures a hop, an edge, or a
status. A failed resolution degrades to an explicit unresolved projection —
an unavailable path must not take the customer record with it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.schemas.status_presentation import StatusPresentation
from app.services.network.access_path import (
    AccessPathSummary,
    SubscriberTopologyTrace,
    build_topology_trace,
    resolve_subscription_access_path,
    summarize_customer_path,
)
from app.services.network_graph import (
    NetworkGraphEdge,
    NetworkGraphEvidence,
    NetworkGraphGap,
    NetworkGraphMeasurement,
    NetworkGraphNode,
    NetworkGraphView,
)
from app.services.status_presentation import (
    access_endpoint_source_presentation,
    path_gap_presentation,
    radio_signal_freshness_presentation,
    topology_hop_status_presentation,
)

logger = logging.getLogger(__name__)

_UNRESOLVED_SOURCE = "unresolved"

_ONT_RX_DETAIL_KEY = "onu_rx_signal_dbm"

# Canonical asset destinations per hop kind, with the permission each
# destination requires. Renderers show a link only when the viewer holds the
# permission; the projection itself never varies facts by viewer.
_NODE_LINKS: dict[str, tuple[str, str]] = {
    "ont": ("/admin/network/onts/{id}", "network:ont:read"),
    "radio": ("/admin/network/cpes/{id}", "network:cpe:read"),
    "olt": ("/admin/network/olts/{id}", "network:olt:read"),
    "ap": ("/admin/network/core-devices/{id}", "network:device:read"),
    "nas": ("/admin/network/nas/devices/{id}", "network:nas:read"),
    "network_device": ("/admin/network/core-devices/{id}", "network:device:read"),
    "fdh": ("/admin/network/fdh-cabinets/{id}", "network:fiber:read"),
    "splitter": ("/admin/network/splitters/{id}", "network:fiber:read"),
    "pop": ("/admin/network/pop-sites/{id}", "network:pop:read"),
}

# Canonical repair destination for the unmatched-radio review queue (plain
# support tickets typed unmatched_radio — see app.services.unmatched_radio_queue).
UNMATCHED_RADIO_QUEUE_HREF = "/admin/support/tickets?ticket_type=unmatched_radio"
UNMATCHED_RADIO_QUEUE_PERMISSION = "support:ticket:read"

TOPOLOGY_GAPS_HREF = "/admin/network/topology-gaps"
TOPOLOGY_GAPS_PERMISSION = "monitoring:read"


@dataclass(frozen=True, slots=True)
class AccessEndpointProjection:
    """Serving-endpoint facts plus their owner-resolved presentation.

    Field names deliberately mirror the legacy card dictionary so the ticket
    prefill and template contracts hold; the presentation and composed display
    fields are what moved out of the template.
    """

    endpoint_source: str
    source_presentation: StatusPresentation
    endpoint_display: str | None = None
    endpoint_complete: bool = True
    access_kind: str | None = None
    access_device_name: str | None = None
    access_device_id: str | None = None
    pon_port_label: str | None = None
    ont_serial: str | None = None
    radio_label: str | None = None
    serving_ap_name: str | None = None
    rf_signal_dbm: float | None = None
    rf_signal_freshness: str | None = None
    rf_signal_observed_at: str | None = None
    rf_freshness_presentation: StatusPresentation | None = None
    # Owner-composed display strings; templates render them verbatim.
    rf_display: str | None = None
    rf_observed_display: str | None = None
    partial_notice: str | None = None
    ap_unresolved_notice: str | None = None
    ap_unresolved_repair_href: str | None = None
    ap_unresolved_repair_permission: str | None = None
    radio_ap_unresolved: bool = False
    basestation_name: str | None = None
    gap: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint_display": self.endpoint_display,
            "endpoint_source": self.endpoint_source,
            "source_presentation": self.source_presentation,
            "access_device_name": self.access_device_name,
            "access_device_id": self.access_device_id,
            "access_kind": self.access_kind,
            "pon_port_label": self.pon_port_label,
            "ont_serial": self.ont_serial,
            "radio_label": self.radio_label,
            "serving_ap_name": self.serving_ap_name,
            "rf_signal_dbm": self.rf_signal_dbm,
            "rf_signal_freshness": self.rf_signal_freshness,
            "rf_signal_observed_at": self.rf_signal_observed_at,
            "rf_freshness_presentation": self.rf_freshness_presentation,
            "rf_display": self.rf_display,
            "rf_observed_display": self.rf_observed_display,
            "partial_notice": self.partial_notice,
            "ap_unresolved_notice": self.ap_unresolved_notice,
            "ap_unresolved_repair_href": self.ap_unresolved_repair_href,
            "ap_unresolved_repair_permission": self.ap_unresolved_repair_permission,
            "radio_ap_unresolved": self.radio_ap_unresolved,
            "basestation_name": self.basestation_name,
            "gap": self.gap,
            "endpoint_complete": self.endpoint_complete,
        }


@dataclass(frozen=True, slots=True)
class SubscriptionNetworkPath:
    """One subscription's path projection: endpoint, graph view, raw trace.

    ``view`` and ``trace`` are None when resolution failed; the endpoint then
    reports unresolved instead of pretending a path exists.
    """

    subscription_id: str
    endpoint: AccessEndpointProjection
    view: NetworkGraphView | None = None
    trace: SubscriberTopologyTrace | None = None

    @property
    def view_dict(self) -> dict[str, object] | None:
        return self.view.to_dict() if self.view else None

    @property
    def trace_dict(self) -> dict[str, object] | None:
        return self.trace.to_dict() if self.trace else None


def unresolved_subscription_network_path(subscription) -> SubscriptionNetworkPath:
    """The honest projection when the owner could not resolve a path."""

    return SubscriptionNetworkPath(
        subscription_id=str(getattr(subscription, "id", "")),
        endpoint=AccessEndpointProjection(
            endpoint_source=_UNRESOLVED_SOURCE,
            source_presentation=access_endpoint_source_presentation(_UNRESOLVED_SOURCE),
        ),
    )


def project_subscription_network_path(
    db: Session,
    subscription,
    *,
    path=None,
) -> SubscriptionNetworkPath:
    """Project one subscription's path; resolves it when not already held.

    Callers that already resolved a CustomerPath pass it so the endpoint, the
    graph view, and the trace share one resolution.
    """

    if path is None:
        try:
            path = resolve_subscription_access_path(db, subscription)
        except Exception:
            logger.warning(
                "Access path resolution failed for subscription %s",
                getattr(subscription, "id", None),
                exc_info=True,
            )
            return unresolved_subscription_network_path(subscription)

    summary = summarize_customer_path(subscription, path)
    trace = build_topology_trace(subscription, path)
    return SubscriptionNetworkPath(
        subscription_id=str(getattr(subscription, "id", "")),
        endpoint=_endpoint_projection(summary),
        view=build_network_graph_view(trace),
        trace=trace,
    )


def project_subscription_network_paths(
    db: Session,
    subscriptions: Sequence,
) -> dict[str, SubscriptionNetworkPath]:
    """Project every given subscription, isolating failures per subscription."""

    return {
        str(subscription.id): project_subscription_network_path(db, subscription)
        for subscription in subscriptions
    }


def build_network_graph_view(trace: SubscriberTopologyTrace) -> NetworkGraphView:
    """Restate the owner's trace as the shared graph view.

    Identity, order, state words, and breaks come from network.access_path
    verbatim; this projection adds presentation, tooltips, ordered edges, and
    measurement display strings — nothing else.
    """

    nodes: list[NetworkGraphNode] = []
    for index, node in enumerate(trace.nodes):
        href, href_permission = asset_link(node.kind, node.asset_id)
        nodes.append(
            NetworkGraphNode(
                id=_node_id(node, index),
                kind=node.kind,
                label=node.label,
                state=node.state,
                presentation=topology_hop_status_presentation(node.state),
                asset_id=str(node.asset_id) if node.asset_id is not None else None,
                tooltip=_node_tooltip(node),
                evidence=(
                    NetworkGraphEvidence(
                        owner=node.source,
                        observed_at=node.observed_at,
                        freshness=_node_freshness(node),
                    )
                    if node.source
                    else None
                ),
                measurements=_node_measurements(node),
                href=href,
                href_permission=href_permission,
            )
        )
    nodes = _link_pon_ports_to_their_olt(nodes)
    edges = tuple(
        NetworkGraphEdge(source_id=nodes[i].id, target_id=nodes[i + 1].id)
        for i in range(len(nodes) - 1)
    )
    gaps: list[NetworkGraphGap] = []
    for break_ in trace.breaks:
        repair_href, repair_permission = _gap_repair(break_.code, trace.subscriber_id)
        gaps.append(
            NetworkGraphGap(
                code=break_.code,
                message=break_.message,
                presentation=path_gap_presentation(break_.code),
                after_node_id=(
                    nodes[break_.after_index].id
                    if break_.after_index is not None
                    and 0 <= break_.after_index < len(nodes)
                    else None
                ),
                repair_href=repair_href,
                repair_permission=repair_permission,
            )
        )
    return NetworkGraphView(
        subject_kind="subscription",
        subject_id=str(trace.subscription_id),
        access_kind=trace.access_kind,
        evaluated_at=trace.evaluated_at,
        nodes=tuple(nodes),
        edges=edges,
        gaps=tuple(gaps),
    )


def _node_id(node, index: int) -> str:
    if node.asset_id is not None:
        return f"{node.kind}:{node.asset_id}"
    return f"{node.kind}#{index}"


def asset_link(kind: str, asset_id) -> tuple[str | None, str | None]:
    """Canonical (href, required-permission) for an asset kind, or (None, None)."""

    if asset_id is None:
        return None, None
    link = _NODE_LINKS.get(kind)
    if link is None:
        return None, None
    template, permission = link
    return template.format(id=asset_id), permission


def _link_pon_ports_to_their_olt(
    nodes: list[NetworkGraphNode],
) -> list[NetworkGraphNode]:
    """PON ports have no page of their own; they live on their OLT's tab.

    The trace orders the PON port adjacent to its OLT (before it on the
    customer trace, after it on the fibre trace), so the link is taken from
    the adjacent proven hop — never inferred from names.
    """

    linked: list[NetworkGraphNode] = []
    for index, node in enumerate(nodes):
        neighbours = [nodes[i] for i in (index + 1, index - 1) if 0 <= i < len(nodes)]
        olt = next(
            (
                neighbour
                for neighbour in neighbours
                if neighbour.kind == "olt" and neighbour.asset_id is not None
            ),
            None,
        )
        if node.kind == "pon_port" and node.href is None and olt is not None:
            node = NetworkGraphNode(
                id=node.id,
                kind=node.kind,
                label=node.label,
                state=node.state,
                presentation=node.presentation,
                asset_id=node.asset_id,
                tooltip=node.tooltip,
                evidence=node.evidence,
                measurements=node.measurements,
                href=f"/admin/network/olts/{olt.asset_id}?tab=pon-ports",
                href_permission="network:olt:read",
            )
        linked.append(node)
    return linked


def _gap_repair(code: str, subscriber_id) -> tuple[str | None, str | None]:
    """Canonical review destination for a path break, keyed on the owner code.

    The mapping names where the gap is repaired; it never repairs or bridges
    anything itself.
    """

    normalized = (code or "").lower()
    if "radio" in normalized or "ap_unresolved" in normalized:
        return UNMATCHED_RADIO_QUEUE_HREF, UNMATCHED_RADIO_QUEUE_PERMISSION
    if "ont" in normalized and subscriber_id is not None:
        return (
            f"/admin/network/onts?assign_subscriber={subscriber_id}",
            "network:ont:read",
        )
    return TOPOLOGY_GAPS_HREF, TOPOLOGY_GAPS_PERMISSION


def _node_tooltip(node) -> str:
    parts = [node.kind]
    if node.observed_at:
        parts.append(f"seen {node.observed_at.isoformat()}")
    if node.source:
        parts.append(str(node.source))
    return " · ".join(parts)


def _node_freshness(node) -> str | None:
    freshness = node.detail.get("rf_signal_freshness")
    return str(freshness) if freshness else None


def _node_measurements(node) -> tuple[NetworkGraphMeasurement, ...]:
    measurements: list[NetworkGraphMeasurement] = []
    ont_rx = node.detail.get(_ONT_RX_DETAIL_KEY)
    if ont_rx is not None:
        measurements.append(
            NetworkGraphMeasurement(
                name=_ONT_RX_DETAIL_KEY,
                label="ONT receive power",
                display=f"{ont_rx} dBm",
                value=float(ont_rx),
                unit="dBm",
                observed_at=node.observed_at,
            )
        )
    rf_dbm = node.detail.get("rf_signal_dbm")
    if rf_dbm is not None:
        freshness = node.detail.get("rf_signal_freshness")
        suffix = " (stale)" if freshness == "stale" else ""
        measurements.append(
            NetworkGraphMeasurement(
                name="rf_signal_dbm",
                label="RF signal",
                display=f"{float(rf_dbm):.0f} dBm{suffix}",
                value=float(rf_dbm),
                unit="dBm",
                freshness=str(freshness) if freshness else None,
                observed_at=node.observed_at,
            )
        )
    return tuple(measurements)


def project_subscription_fiber_detail(
    db: Session, subscription_id
) -> NetworkGraphView | None:
    """Passive fibre plant detail for one fibre subscription.

    network.fiber_topology owns the validated hop order and gap codes; this
    projection restates them in the shared graph vocabulary. Passive plant
    renders identity and continuity — its state is honestly not-applicable,
    never a fabricated up/down. Returns None when the owner cannot trace the
    subscription (unknown id or non-fibre service).
    """

    from app.services.fiber_topology import trace_fiber_subscription

    try:
        trace = trace_fiber_subscription(db, subscription_id)
    except ValueError:
        return None

    nodes: list[NetworkGraphNode] = []
    for index, hop in enumerate(trace.hops):
        state = hop.operational_state or "not_applicable"
        href, href_permission = asset_link(hop.kind, hop.asset_id)
        nodes.append(
            NetworkGraphNode(
                id=(
                    f"{hop.kind}:{hop.asset_id}"
                    if hop.asset_id is not None
                    else f"{hop.kind}#{index}"
                ),
                kind=hop.kind,
                label=hop.label,
                state=state,
                presentation=topology_hop_status_presentation(state),
                asset_id=str(hop.asset_id) if hop.asset_id is not None else None,
                tooltip=f"{hop.kind} · {hop.evidence}" if hop.evidence else hop.kind,
                evidence=(
                    NetworkGraphEvidence(owner=hop.evidence) if hop.evidence else None
                ),
                measurements=_fiber_hop_measurements(hop),
                href=href,
                href_permission=href_permission,
            )
        )
    nodes = _link_pon_ports_to_their_olt(nodes)
    edges = tuple(
        NetworkGraphEdge(source_id=nodes[i].id, target_id=nodes[i + 1].id)
        for i in range(len(nodes) - 1)
    )
    gaps = tuple(
        NetworkGraphGap(
            code=gap.code,
            message=gap.message,
            presentation=path_gap_presentation(gap.code),
            repair_href=(
                f"/admin/network/fiber-trace?subscription_id={trace.subscription_id}"
            ),
            repair_permission="network:fiber:read",
        )
        for gap in trace.gaps
    )
    return NetworkGraphView(
        subject_kind="subscription_fiber_plant",
        subject_id=str(trace.subscription_id),
        access_kind="fiber",
        evaluated_at=datetime.now(UTC),
        nodes=tuple(nodes),
        edges=edges,
        gaps=gaps,
    )


def _fiber_hop_measurements(hop) -> tuple[NetworkGraphMeasurement, ...]:
    measurements: list[NetworkGraphMeasurement] = []
    for name, label, raw in (
        ("insertion_loss_db", "Insertion loss", hop.insertion_loss_db),
        (
            "cumulative_splitter_loss_db",
            "Cumulative splitter loss",
            hop.cumulative_splitter_loss_db,
        ),
    ):
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        measurements.append(
            NetworkGraphMeasurement(
                name=name,
                label=label,
                display=f"{raw} dB",
                value=value,
                unit="dB",
            )
        )
    return tuple(measurements)


def _endpoint_projection(summary: AccessPathSummary) -> AccessEndpointProjection:
    rf_display, rf_observed_display = _rf_displays(summary)
    observed_at = (
        summary.radio_signal_observed_at.isoformat()
        if summary.radio_signal_observed_at
        else None
    )
    return AccessEndpointProjection(
        endpoint_source=summary.endpoint_source,
        source_presentation=access_endpoint_source_presentation(
            summary.endpoint_source
        ),
        endpoint_display=summary.endpoint_display,
        endpoint_complete=summary.endpoint_complete,
        access_kind=summary.access_kind,
        access_device_name=summary.access_device_name,
        access_device_id=(
            str(summary.access_device_id) if summary.access_device_id else None
        ),
        pon_port_label=summary.pon_port_label,
        ont_serial=summary.ont_serial,
        radio_label=summary.radio_label,
        serving_ap_name=(
            summary.access_device_name if summary.access_kind == "ap" else None
        ),
        rf_signal_dbm=summary.radio_signal_dbm,
        rf_signal_freshness=summary.radio_signal_freshness,
        rf_signal_observed_at=observed_at,
        rf_freshness_presentation=radio_signal_freshness_presentation(
            summary.radio_signal_freshness or "unavailable"
        ),
        rf_display=rf_display,
        rf_observed_display=rf_observed_display,
        partial_notice=(
            None
            if summary.endpoint_complete
            else (f"Partial — {summary.gap}" if summary.gap else "Partial")
        ),
        ap_unresolved_notice=_ap_unresolved_notice(summary),
        ap_unresolved_repair_href=(
            UNMATCHED_RADIO_QUEUE_HREF if summary.radio_ap_unresolved else None
        ),
        ap_unresolved_repair_permission=(
            UNMATCHED_RADIO_QUEUE_PERMISSION if summary.radio_ap_unresolved else None
        ),
        radio_ap_unresolved=summary.radio_ap_unresolved,
        basestation_name=summary.basestation_name,
        gap=summary.gap,
    )


def _ap_unresolved_notice(summary: AccessPathSummary) -> str | None:
    if not summary.radio_ap_unresolved:
        return None
    subject = f"Radio {summary.radio_label}" if summary.radio_label else "Radio"
    return f"{subject} has no serving-AP mapping — see the unmatched-radio queue"


def _rf_displays(summary: AccessPathSummary) -> tuple[str | None, str | None]:
    """Compose the RF strings every surface renders identically.

    fresh   -> "-62 dBm" plus a separate observed-at line
    stale   -> one line carrying the last value and when it was seen
    other   -> "Signal unavailable"; a cleared or never-seen observation must
               not render as a current signal.
    """

    freshness = summary.radio_signal_freshness
    dbm = summary.radio_signal_dbm
    observed_at = (
        summary.radio_signal_observed_at.isoformat()
        if summary.radio_signal_observed_at
        else None
    )
    if freshness == "fresh" and dbm is not None:
        return f"{dbm:.0f} dBm", (f"Observed at {observed_at}" if observed_at else None)
    if freshness == "stale" and dbm is not None:
        return f"Stale (last {dbm:.0f} dBm at {observed_at})", None
    return "Signal unavailable", None
