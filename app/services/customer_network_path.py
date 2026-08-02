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

# Measurement vocabulary rendered inside the path today. RF signal stays on the
# serving-endpoint block (its owner-composed display lives there); widening the
# in-path measurement set is a deliberate later slice, not a template decision.
_ONT_RX_DETAIL_KEY = "onu_rx_signal_dbm"


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
            )
        )
    edges = tuple(
        NetworkGraphEdge(source_id=nodes[i].id, target_id=nodes[i + 1].id)
        for i in range(len(nodes) - 1)
    )
    gaps = tuple(
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
        )
        for break_ in trace.breaks
    )
    return NetworkGraphView(
        subject_kind="subscription",
        subject_id=str(trace.subscription_id),
        access_kind=trace.access_kind,
        evaluated_at=trace.evaluated_at,
        nodes=tuple(nodes),
        edges=edges,
        gaps=gaps,
    )


def _node_id(node, index: int) -> str:
    if node.asset_id is not None:
        return f"{node.kind}:{node.asset_id}"
    return f"{node.kind}#{index}"


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
    value = node.detail.get(_ONT_RX_DETAIL_KEY)
    if value is None:
        return ()
    return (
        NetworkGraphMeasurement(
            name=_ONT_RX_DETAIL_KEY,
            label="ONT receive power",
            display=f"{value} dBm",
            value=float(value),
            unit="dBm",
            observed_at=node.observed_at,
        ),
    )


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
