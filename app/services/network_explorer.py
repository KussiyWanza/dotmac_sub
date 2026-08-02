"""Unified network explorer read projection.

Subject-centred, bounded network graphs plus typed cross-asset search for
/admin/network/explorer. Every fact is restated from an existing owner:
ui.customer_network_path_projection supplies subscription paths,
network.forwarding_topology supplies reviewed device adjacency,
network.device_state supplies the binary device verdict,
network.olt_observed_state facts supply ONT words, network.outage_impact
supplies audience cohorts, and ui.status_presentation supplies meaning. This
projection decides no topology, health, outage, or consequence; it never
loads the whole fleet, never manufactures an edge from names or geography,
and groups large fan-out into explicit cohort nodes instead of truncating
silently. Site containment renders as a "containment" edge, never as
connectivity.
"""

from __future__ import annotations

import logging
import uuid as uuid_module
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription
from app.models.network import (
    CPEDevice,
    FdhCabinet,
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
    Splitter,
)
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Subscriber
from app.schemas.status_presentation import StatusPresentation
from app.services.customer_network_path import (
    TOPOLOGY_GAPS_HREF,
    TOPOLOGY_GAPS_PERMISSION,
    UNMATCHED_RADIO_QUEUE_HREF,
    UNMATCHED_RADIO_QUEUE_PERMISSION,
    asset_link,
    project_subscription_network_path,
)
from app.services.device_operational_status import annotate_operational_status
from app.services.network.ont_status import resolve_effective_ont_status
from app.services.network_graph import (
    NetworkGraphEdge,
    NetworkGraphEvidence,
    NetworkGraphMeasurement,
    NetworkGraphNode,
    NetworkGraphView,
)
from app.services.status_presentation import (
    device_operational_status_presentation,
    outage_status_presentation,
    topology_hop_status_presentation,
)
from app.services.topology import affected

logger = logging.getLogger(__name__)

EXPLORER_PATH = "/admin/network/explorer"
EXPLORER_PAGE_PERMISSION = "network:device:read"

# Bounded by design: the explorer opens around a subject, never the fleet.
MAX_GRAPH_NODES = 100
GROUP_THRESHOLD = 25
_SEARCH_LIMIT_PER_KIND = 5
_MAX_UPSTREAM_HOPS = 16

_CUSTOMER_SUBJECT_KINDS = frozenset({"subscription", "subscriber"})

_SUBJECT_KIND_LABELS = {
    "subscription": "Subscription",
    "subscriber": "Customer",
    "ont": "ONT",
    "radio": "Radio",
    "device": "Network device",
    "nas": "NAS",
    "olt": "OLT",
    "pon_port": "PON port",
    "fdh": "FDH cabinet",
    "splitter": "Splitter",
    "pop_site": "Site",
}


@dataclass(frozen=True, slots=True)
class ExplorerSearchResult:
    """One typed search hit; opening it recentres the explorer."""

    kind: str
    kind_label: str
    subject: str
    label: str
    detail: str | None
    subject_url: str


@dataclass(frozen=True, slots=True)
class ExplorerContext:
    """Everything the explorer page renders for one request."""

    subject: str | None
    subject_kind: str | None
    subject_kind_label: str | None
    query: str
    results: tuple[ExplorerSearchResult, ...]
    view: NetworkGraphView | None
    subject_missing: bool

    @property
    def view_dict(self) -> dict[str, object] | None:
        return self.view.to_dict() if self.view else None


@dataclass(frozen=True, slots=True)
class InspectorIncident:
    """One live incident covering the inspected subject."""

    incident_id: str
    status: str
    presentation: StatusPresentation
    detection_source: str | None
    started_at: str | None


@dataclass(frozen=True, slots=True)
class InspectorFact:
    """One owner-provided identity or neighbourhood fact, ready to render."""

    label: str
    display: str
    href: str | None = None
    href_permission: str | None = None


@dataclass(frozen=True, slots=True)
class ExplorerInspector:
    """On-demand inspector projection for one subject.

    Composes the owner verdict, its machine reason, owner-composed
    measurements, live incidents, and the reverse affected-customer cohort.
    Every list is bounded and every tone/label is owner-provided.
    """

    subject: str
    kind: str
    kind_label: str
    label: str
    href: str | None = None
    href_permission: str | None = None
    state_presentation: StatusPresentation | None = None
    state_reason: str | None = None
    observed_at: str | None = None
    measurements: tuple[NetworkGraphMeasurement, ...] = ()
    facts: tuple[InspectorFact, ...] = ()
    affected_count: int | None = None
    affected_online: int | None = None
    incidents: tuple[InspectorIncident, ...] = ()
    customer360_href: str | None = None


def build_explorer_context(
    db: Session,
    *,
    subject: str | None,
    query: str | None,
    include_customer_identity: bool,
) -> ExplorerContext:
    """Compose search results and the subject-centred graph for one request."""

    normalized_query = (query or "").strip()
    results: tuple[ExplorerSearchResult, ...] = ()
    if normalized_query:
        results = search_explorer_subjects(
            db,
            normalized_query,
            include_customer_identity=include_customer_identity,
        )

    view: NetworkGraphView | None = None
    subject_kind: str | None = None
    subject_missing = False
    normalized_subject = (subject or "").strip() or None
    if normalized_subject:
        subject_kind = normalized_subject.partition(":")[0]
        if subject_kind in _CUSTOMER_SUBJECT_KINDS and not include_customer_identity:
            subject_missing = True
        else:
            view = build_explorer_view(db, normalized_subject)
            subject_missing = view is None

    return ExplorerContext(
        subject=normalized_subject,
        subject_kind=subject_kind,
        subject_kind_label=_SUBJECT_KIND_LABELS.get(subject_kind or ""),
        query=normalized_query,
        results=results,
        view=view,
        subject_missing=subject_missing,
    )


# --- typed search ----------------------------------------------------------


def search_explorer_subjects(
    db: Session,
    query: str,
    *,
    include_customer_identity: bool,
    limit_per_kind: int = _SEARCH_LIMIT_PER_KIND,
) -> tuple[ExplorerSearchResult, ...]:
    """Typed lookup across customers, access assets, and infrastructure.

    Results are typed so similarly named assets stay distinguishable, and
    customer-identity kinds are omitted entirely for viewers without
    customer:read.
    """

    like = f"%{_escape_like(query)}%"
    results: list[ExplorerSearchResult] = []

    if include_customer_identity:
        results.extend(_search_subscriptions(db, query, like, limit_per_kind))
        results.extend(_search_subscribers(db, like, limit_per_kind))
        results.extend(_search_radios(db, like, limit_per_kind))
    results.extend(_search_onts(db, like, limit_per_kind))
    results.extend(_search_olts(db, like, limit_per_kind))
    results.extend(_search_nas(db, like, limit_per_kind))
    results.extend(_search_devices(db, like, limit_per_kind))
    results.extend(_search_fdh(db, like, limit_per_kind))
    results.extend(_search_splitters(db, like, limit_per_kind))
    results.extend(_search_pop_sites(db, like, limit_per_kind))
    return tuple(results)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _result(
    kind: str, asset_id, label: str, detail: str | None
) -> ExplorerSearchResult:
    subject = f"{kind}:{asset_id}"
    return ExplorerSearchResult(
        kind=kind,
        kind_label=_SUBJECT_KIND_LABELS.get(kind, kind.replace("_", " ").title()),
        subject=subject,
        label=label,
        detail=detail,
        subject_url=f"{EXPLORER_PATH}?subject={subject}",
    )


def _subscriber_label(subscriber: Subscriber | None) -> str | None:
    if subscriber is None:
        return None
    parts = [subscriber.first_name or "", subscriber.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or getattr(subscriber, "company_name", None) or None


def _search_subscriptions(db, query, like, limit) -> list[ExplorerSearchResult]:
    filters = [Subscription.login.ilike(like, escape="\\")]
    if _looks_like_ipv4(query):
        filters.append(Subscription.ipv4_address == query)
    rows = (
        db.query(Subscription, Subscriber)
        .outerjoin(Subscriber, Subscription.subscriber_id == Subscriber.id)
        .filter(or_(*filters))
        .limit(limit)
        .all()
    )
    return [
        _result(
            "subscription",
            subscription.id,
            subscription.login or subscription.ipv4_address or "Subscription",
            _subscriber_label(subscriber),
        )
        for subscription, subscriber in rows
    ]


def _search_subscribers(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(Subscriber)
        .filter(
            or_(
                Subscriber.first_name.ilike(like, escape="\\"),
                Subscriber.last_name.ilike(like, escape="\\"),
                Subscriber.email.ilike(like, escape="\\"),
                Subscriber.account_number.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [
        _result(
            "subscriber",
            subscriber.id,
            _subscriber_label(subscriber) or "Customer",
            subscriber.email,
        )
        for subscriber in rows
    ]


def _search_radios(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(CPEDevice)
        .filter(
            or_(
                CPEDevice.serial_number.ilike(like, escape="\\"),
                CPEDevice.mac_address.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [
        _result(
            "radio",
            radio.id,
            radio.serial_number or radio.mac_address or "Radio",
            radio.model,
        )
        for radio in rows
    ]


def _search_onts(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(OntUnit)
        .filter(
            or_(
                OntUnit.serial_number.ilike(like, escape="\\"),
                OntUnit.vendor_serial_number.ilike(like, escape="\\"),
                OntUnit.name.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [
        _result(
            "ont",
            ont.id,
            ont.serial_number or ont.vendor_serial_number or "ONT",
            ont.name,
        )
        for ont in rows
    ]


def _search_olts(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(OLTDevice)
        .filter(
            or_(
                OLTDevice.name.ilike(like, escape="\\"),
                OLTDevice.hostname.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [
        _result("olt", olt.id, olt.name or olt.hostname or "OLT", olt.mgmt_ip)
        for olt in rows
    ]


def _search_nas(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(NasDevice)
        .filter(
            or_(
                NasDevice.name.ilike(like, escape="\\"),
                NasDevice.ip_address.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [_result("nas", nas.id, nas.name or "NAS", nas.ip_address) for nas in rows]


def _search_devices(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(NetworkDevice)
        .filter(
            or_(
                NetworkDevice.name.ilike(like, escape="\\"),
                NetworkDevice.hostname.ilike(like, escape="\\"),
                NetworkDevice.mgmt_ip.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [
        _result(
            "device",
            device.id,
            device.name or device.hostname or "Device",
            device.role,
        )
        for device in rows
    ]


def _search_fdh(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(FdhCabinet)
        .filter(
            or_(
                FdhCabinet.name.ilike(like, escape="\\"),
                FdhCabinet.code.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [
        _result("fdh", fdh.id, fdh.name or fdh.code or "FDH", fdh.code) for fdh in rows
    ]


def _search_splitters(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(Splitter)
        .filter(Splitter.name.ilike(like, escape="\\"))
        .limit(limit)
        .all()
    )
    return [
        _result(
            "splitter",
            splitter.id,
            splitter.name or "Splitter",
            splitter.splitter_ratio,
        )
        for splitter in rows
    ]


def _search_pop_sites(db, like, limit) -> list[ExplorerSearchResult]:
    rows = (
        db.query(PopSite)
        .filter(
            or_(
                PopSite.name.ilike(like, escape="\\"),
                PopSite.code.ilike(like, escape="\\"),
            )
        )
        .limit(limit)
        .all()
    )
    return [
        _result("pop_site", site.id, site.name or "Site", site.city) for site in rows
    ]


def _looks_like_ipv4(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 4 and all(part.isdigit() for part in parts)


# --- subject-centred graph -------------------------------------------------


def build_explorer_view(db: Session, subject: str) -> NetworkGraphView | None:
    """Bounded graph around one subject; None when it cannot be proven."""

    kind, _, raw_id = subject.partition(":")
    builder = {
        "subscription": _subscription_view,
        "subscriber": _subscriber_view,
        "ont": _ont_view,
        "radio": _radio_view,
        "device": _device_view,
        "nas": _nas_view,
        "olt": _olt_view,
        "pon_port": _pon_port_view,
        "fdh": _fdh_view,
        "splitter": _splitter_view,
        "pop_site": _pop_site_view,
    }.get(kind)
    if builder is None or not raw_id:
        return None
    try:
        subject_id = uuid_module.UUID(raw_id)
    except ValueError:
        return None
    try:
        return builder(db, subject_id)
    except Exception:
        logger.warning("Explorer view failed for subject %s", subject, exc_info=True)
        return None


def _view(kind: str, subject_id, nodes, edges, gaps=()) -> NetworkGraphView:
    nodes = _enforce_node_cap(list(nodes))
    return NetworkGraphView(
        subject_kind=kind,
        subject_id=str(subject_id),
        access_kind=None,
        evaluated_at=datetime.now(UTC),
        nodes=tuple(nodes),
        edges=tuple(
            edge
            for edge in edges
            if _has_node(nodes, edge.source_id) and _has_node(nodes, edge.target_id)
        ),
        gaps=tuple(gaps),
    )


def _has_node(nodes, node_id: str) -> bool:
    return any(node.id == node_id for node in nodes)


def _enforce_node_cap(nodes: list[NetworkGraphNode]) -> list[NetworkGraphNode]:
    if len(nodes) <= MAX_GRAPH_NODES:
        return nodes
    kept = nodes[: MAX_GRAPH_NODES - 1]
    dropped = len(nodes) - len(kept)
    kept.append(
        _identity_node(
            "cohort:overflow",
            "cohort",
            f"+{dropped} more (open the canonical list)",
        )
    )
    logger.info("Explorer view capped: dropped %s nodes", dropped)
    return kept


def _identity_node(
    node_id: str,
    kind: str,
    label: str,
    *,
    state: str = "not_applicable",
    asset_id=None,
    tooltip: str | None = None,
    href: str | None = None,
    href_permission: str | None = None,
    evidence_owner: str | None = None,
) -> NetworkGraphNode:
    if href is None and asset_id is not None:
        href, href_permission = asset_link(kind, asset_id)
    return NetworkGraphNode(
        id=node_id,
        kind=kind,
        label=label,
        state=state,
        presentation=topology_hop_status_presentation(state),
        asset_id=str(asset_id) if asset_id is not None else None,
        tooltip=tooltip or kind,
        evidence=(
            NetworkGraphEvidence(owner=evidence_owner) if evidence_owner else None
        ),
        href=href,
        href_permission=href_permission,
    )


def _device_node(device: NetworkDevice) -> NetworkGraphNode:
    operational = getattr(device, "operational", None)
    if operational is not None:
        state = operational.status
        presentation = device_operational_status_presentation(operational)
        tooltip = f"network_device · {operational.reason}"
    else:
        state = "unknown"
        presentation = topology_hop_status_presentation("unknown")
        tooltip = "network_device"
    href, href_permission = asset_link("network_device", device.id)
    return NetworkGraphNode(
        id=f"device:{device.id}",
        kind="network_device",
        label=device.name or device.hostname or str(device.id),
        state=state,
        presentation=presentation,
        asset_id=str(device.id),
        tooltip=tooltip,
        evidence=NetworkGraphEvidence(
            owner="network.device_state",
            observed_at=getattr(device, "live_status_at", None),
        ),
        href=href,
        href_permission=href_permission,
    )


def _ont_node(ont: OntUnit) -> NetworkGraphNode:
    effective = resolve_effective_ont_status(ont)
    status_word = str(getattr(effective.status, "value", effective.status))
    state = {"online": "up", "offline": "down"}.get(status_word, "unknown")
    href, href_permission = asset_link("ont", ont.id)
    return NetworkGraphNode(
        id=f"ont:{ont.id}",
        kind="ont",
        label=ont.serial_number or ont.vendor_serial_number or str(ont.id),
        state=state,
        presentation=topology_hop_status_presentation(state),
        asset_id=str(ont.id),
        tooltip=f"ont · {effective.reason}",
        evidence=NetworkGraphEvidence(
            owner="network.olt_observed_state",
            observed_at=getattr(ont, "olt_status_seen_at", None),
        ),
        href=href,
        href_permission=href_permission,
    )


def _cohort_node(
    node_id: str, label: str, *, href: str | None = None, permission: str | None = None
) -> NetworkGraphNode:
    return _identity_node(
        node_id,
        "cohort",
        label,
        href=href,
        href_permission=permission,
        tooltip="cohort · grouped fan-out; open the canonical list to expand",
    )


def _subscription_view(db, subscription_id) -> NetworkGraphView | None:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return None
    return project_subscription_network_path(db, subscription).view


def _subscriber_view(db, subscriber_id) -> NetworkGraphView | None:
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        return None
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .order_by(Subscription.created_at)
        .limit(GROUP_THRESHOLD)
        .all()
    )
    root = _identity_node(
        f"subscriber:{subscriber_id}",
        "subscriber",
        _subscriber_label(subscriber) or "Customer",
        tooltip="subscriber",
    )
    nodes = [root]
    edges = []
    for subscription in subscriptions:
        node = _identity_node(
            f"subscription:{subscription.id}",
            "subscription",
            subscription.login or subscription.ipv4_address or "Subscription",
            href=f"{EXPLORER_PATH}?subject=subscription:{subscription.id}",
            href_permission=EXPLORER_PAGE_PERMISSION,
            tooltip="subscription · open to trace its path",
        )
        nodes.append(node)
        edges.append(
            NetworkGraphEdge(source_id=root.id, target_id=node.id, kind="access")
        )
    return _view("subscriber", subscriber_id, nodes, edges)


def _ont_view(db, ont_id) -> NetworkGraphView | None:
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return None
    assignment = (
        db.query(OntAssignment)
        .filter(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
        .first()
    )
    subscription_id = getattr(assignment, "subscription_id", None)
    if subscription_id is not None:
        subscription = db.get(Subscription, subscription_id)
        if subscription is not None:
            return project_subscription_network_path(db, subscription).view

    nodes = [_ont_node(ont)]
    edges = []
    pon = db.get(PonPort, ont.pon_port_id) if ont.pon_port_id else None
    if pon is not None:
        pon_node = _identity_node(
            f"pon_port:{pon.id}",
            "pon_port",
            pon.name or f"PON {pon.port_number}",
            state="unknown",
            asset_id=pon.id,
        )
        nodes.append(pon_node)
        edges.append(
            NetworkGraphEdge(
                source_id=nodes[0].id, target_id=pon_node.id, kind="access"
            )
        )
        olt = db.get(OLTDevice, pon.olt_id) if pon.olt_id else None
        if olt is not None:
            olt_node = _identity_node(
                f"olt:{olt.id}", "olt", olt.name or "OLT", asset_id=olt.id
            )
            nodes.append(olt_node)
            edges.append(
                NetworkGraphEdge(
                    source_id=pon_node.id, target_id=olt_node.id, kind="access"
                )
            )
    return _view("ont", ont_id, nodes, edges)


def _radio_view(db, radio_id) -> NetworkGraphView | None:
    radio = db.get(CPEDevice, radio_id)
    if radio is None:
        return None
    if radio.subscription_id is not None:
        subscription = db.get(Subscription, radio.subscription_id)
        if subscription is not None:
            return project_subscription_network_path(db, subscription).view
    href, href_permission = asset_link("radio", radio.id)
    nodes = [
        NetworkGraphNode(
            id=f"radio:{radio.id}",
            kind="radio",
            label=radio.serial_number or radio.mac_address or str(radio.id),
            state="unknown",
            presentation=topology_hop_status_presentation("unknown"),
            asset_id=str(radio.id),
            tooltip="radio",
            href=href,
            href_permission=href_permission,
        )
    ]
    edges = []
    if radio.parent_network_device_id is not None:
        parent = db.get(NetworkDevice, radio.parent_network_device_id)
        if parent is not None:
            annotate_operational_status([parent])
            ap_node = _device_node(parent)
            nodes.append(ap_node)
            edges.append(
                NetworkGraphEdge(
                    source_id=nodes[0].id, target_id=ap_node.id, kind="access"
                )
            )
    return _view("radio", radio_id, nodes, edges)


def _device_view(db, device_id) -> NetworkGraphView | None:
    device = db.get(NetworkDevice, device_id)
    if device is None:
        return None
    graph = affected.forwarding_graph_projection(db)

    upstream_ids: list = []
    cursor = device.id
    for _ in range(_MAX_UPSTREAM_HOPS):
        parent = graph.upstream_by_downstream.get(cursor)
        if parent is None or parent in upstream_ids or parent == device.id:
            break
        upstream_ids.append(parent)
        cursor = parent
    child_ids = list(graph.adjacency.get(device.id, ()))

    devices = {device.id: device}
    wanted = upstream_ids + child_ids[:GROUP_THRESHOLD]
    if wanted:
        for row in db.query(NetworkDevice).filter(NetworkDevice.id.in_(wanted)).all():
            devices[row.id] = row
    annotate_operational_status(list(devices.values()))

    nodes = [_device_node(device)]
    edges = []
    previous = device.id
    for parent_id in upstream_ids:
        parent = devices.get(parent_id)
        if parent is None:
            continue
        nodes.append(_device_node(parent))
        edges.append(
            NetworkGraphEdge(
                source_id=f"device:{previous}",
                target_id=f"device:{parent_id}",
                kind="forwarding",
            )
        )
        previous = parent_id
    for child_id in child_ids[:GROUP_THRESHOLD]:
        child = devices.get(child_id)
        if child is None:
            continue
        nodes.append(_device_node(child))
        edges.append(
            NetworkGraphEdge(
                source_id=f"device:{child_id}",
                target_id=f"device:{device.id}",
                kind="forwarding",
            )
        )
    if len(child_ids) > GROUP_THRESHOLD:
        cohort = _cohort_node(
            f"cohort:children:{device.id}",
            f"+{len(child_ids) - GROUP_THRESHOLD} more downstream devices",
            href="/admin/network/network-devices",
            permission="network:device:read",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(
                source_id=cohort.id,
                target_id=f"device:{device.id}",
                kind="forwarding",
            )
        )

    subscription_count = len(
        affected.subscriptions_for_nodes(db, [device.id]).get(device.id, [])
    )
    if subscription_count:
        cohort = _cohort_node(
            f"cohort:subscriptions:{device.id}",
            f"{subscription_count} attached subscriptions",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(
                source_id=cohort.id,
                target_id=f"device:{device.id}",
                kind="access",
            )
        )
    return _view("device", device_id, nodes, edges)


def _nas_view(db, nas_id) -> NetworkGraphView | None:
    nas = db.get(NasDevice, nas_id)
    if nas is None:
        return None
    nodes = [_identity_node(f"nas:{nas.id}", "nas", nas.name or "NAS", asset_id=nas.id)]
    edges = []
    matched = (
        db.query(NetworkDevice)
        .filter(
            NetworkDevice.matched_device_type == "nas",
            NetworkDevice.matched_device_id == nas.id,
        )
        .first()
    )
    if matched is not None:
        annotate_operational_status([matched])
        device_node = _device_node(matched)
        nodes.append(device_node)
        edges.append(
            NetworkGraphEdge(
                source_id=nodes[0].id,
                target_id=device_node.id,
                kind="containment",
            )
        )
    provisioned = (
        db.query(func.count(Subscription.id))
        .filter(Subscription.provisioning_nas_device_id == nas.id)
        .scalar()
        or 0
    )
    if provisioned:
        cohort = _cohort_node(
            f"cohort:subscriptions:{nas.id}",
            f"{provisioned} provisioned subscriptions",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(source_id=cohort.id, target_id=nodes[0].id, kind="access")
        )
    return _view("nas", nas_id, nodes, edges)


def _olt_view(db, olt_id) -> NetworkGraphView | None:
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return None
    olt_node = _identity_node(
        f"olt:{olt.id}", "olt", olt.name or olt.hostname or "OLT", asset_id=olt.id
    )
    nodes = [olt_node]
    edges = []

    pon_rows = (
        db.query(PonPort, func.count(OntAssignment.id))
        .outerjoin(
            OntAssignment,
            (OntAssignment.pon_port_id == PonPort.id) & OntAssignment.active.is_(True),
        )
        .filter(PonPort.olt_id == olt.id)
        .group_by(PonPort.id)
        .order_by(PonPort.port_number)
        .all()
    )
    if len(pon_rows) > GROUP_THRESHOLD:
        cohort = _cohort_node(
            f"cohort:pons:{olt.id}",
            f"{len(pon_rows)} PON ports",
            href=f"/admin/network/olts/{olt.id}?tab=pon-ports",
            permission="network:olt:read",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(source_id=cohort.id, target_id=olt_node.id, kind="access")
        )
    else:
        for pon, ont_count in pon_rows:
            label = pon.name or f"PON {pon.port_number}"
            if ont_count:
                label = f"{label} · {ont_count} ONTs"
            pon_node = _identity_node(
                f"pon_port:{pon.id}",
                "pon_port",
                label,
                state="unknown",
                asset_id=pon.id,
                href=f"{EXPLORER_PATH}?subject=pon_port:{pon.id}",
                href_permission=EXPLORER_PAGE_PERMISSION,
            )
            nodes.append(pon_node)
            edges.append(
                NetworkGraphEdge(
                    source_id=pon_node.id, target_id=olt_node.id, kind="access"
                )
            )

    matched = (
        db.query(NetworkDevice)
        .filter(
            NetworkDevice.matched_device_type == "olt",
            NetworkDevice.matched_device_id == olt.id,
        )
        .first()
    )
    if matched is not None:
        annotate_operational_status([matched])
        device_node = _device_node(matched)
        nodes.append(device_node)
        edges.append(
            NetworkGraphEdge(
                source_id=olt_node.id,
                target_id=device_node.id,
                kind="containment",
            )
        )
    return _view("olt", olt_id, nodes, edges)


def _pon_port_view(db, pon_port_id) -> NetworkGraphView | None:
    pon = db.get(PonPort, pon_port_id)
    if pon is None:
        return None
    pon_node = _identity_node(
        f"pon_port:{pon.id}",
        "pon_port",
        pon.name or f"PON {pon.port_number}",
        state="unknown",
        asset_id=pon.id,
    )
    nodes = [pon_node]
    edges = []

    olt = db.get(OLTDevice, pon.olt_id) if pon.olt_id else None
    if olt is not None:
        olt_node = _identity_node(
            f"olt:{olt.id}", "olt", olt.name or "OLT", asset_id=olt.id
        )
        nodes.append(olt_node)
        edges.append(
            NetworkGraphEdge(
                source_id=pon_node.id, target_id=olt_node.id, kind="access"
            )
        )

    assigned_ids = select(OntAssignment.ont_unit_id).where(
        OntAssignment.pon_port_id == pon.id,
        OntAssignment.active.is_(True),
    )
    onts = (
        db.query(OntUnit)
        .filter(or_(OntUnit.pon_port_id == pon.id, OntUnit.id.in_(assigned_ids)))
        .order_by(OntUnit.serial_number)
        .all()
    )
    shown = onts[:GROUP_THRESHOLD]
    for ont in shown:
        node = _ont_node(ont)
        nodes.append(node)
        edges.append(
            NetworkGraphEdge(source_id=node.id, target_id=pon_node.id, kind="access")
        )
    if len(onts) > len(shown):
        olt_id = pon.olt_id
        cohort = _cohort_node(
            f"cohort:onts:{pon.id}",
            f"+{len(onts) - len(shown)} more ONTs",
            href=f"/admin/network/onts?olt_id={olt_id}&pon_port_id={pon.id}",
            permission="network:ont:read",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(source_id=cohort.id, target_id=pon_node.id, kind="access")
        )
    return _view("pon_port", pon_port_id, nodes, edges)


def _fdh_view(db, fdh_id) -> NetworkGraphView | None:
    fdh = db.get(FdhCabinet, fdh_id)
    if fdh is None:
        return None
    fdh_node = _identity_node(
        f"fdh:{fdh.id}", "fdh", fdh.name or fdh.code or "FDH", asset_id=fdh.id
    )
    nodes = [fdh_node]
    edges = []
    splitters = (
        db.query(Splitter)
        .filter(Splitter.fdh_id == fdh.id, Splitter.is_active.is_(True))
        .order_by(Splitter.name)
        .all()
    )
    for splitter in splitters[:GROUP_THRESHOLD]:
        node = _identity_node(
            f"splitter:{splitter.id}",
            "splitter",
            splitter.name or f"Splitter {splitter.splitter_ratio or ''}".strip(),
            asset_id=splitter.id,
            evidence_owner="network.splitter_inventory",
        )
        nodes.append(node)
        edges.append(
            NetworkGraphEdge(
                source_id=node.id, target_id=fdh_node.id, kind="containment"
            )
        )
    if len(splitters) > GROUP_THRESHOLD:
        cohort = _cohort_node(
            f"cohort:splitters:{fdh.id}",
            f"+{len(splitters) - GROUP_THRESHOLD} more splitters",
            href=f"/admin/network/fdh-cabinets/{fdh.id}",
            permission="network:fiber:read",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(
                source_id=cohort.id, target_id=fdh_node.id, kind="containment"
            )
        )

    from app.services.network.outage_impact import resolve_fdh_audience

    audience = resolve_fdh_audience(db, fdh)
    if audience.subscription_ids:
        cohort = _cohort_node(
            f"cohort:subscriptions:{fdh.id}",
            f"{len(audience.subscription_ids)} served subscriptions",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(source_id=cohort.id, target_id=fdh_node.id, kind="access")
        )
    return _view("fdh", fdh_id, nodes, edges)


def _splitter_view(db, splitter_id) -> NetworkGraphView | None:
    splitter = db.get(Splitter, splitter_id)
    if splitter is None:
        return None
    splitter_node = _identity_node(
        f"splitter:{splitter.id}",
        "splitter",
        splitter.name or "Splitter",
        asset_id=splitter.id,
        evidence_owner="network.splitter_inventory",
    )
    nodes = [splitter_node]
    edges = []
    if splitter.fdh_id:
        fdh = db.get(FdhCabinet, splitter.fdh_id)
        if fdh is not None:
            fdh_node = _identity_node(
                f"fdh:{fdh.id}",
                "fdh",
                fdh.name or fdh.code or "FDH",
                asset_id=fdh.id,
                href=f"{EXPLORER_PATH}?subject=fdh:{fdh.id}",
                href_permission=EXPLORER_PAGE_PERMISSION,
            )
            nodes.append(fdh_node)
            edges.append(
                NetworkGraphEdge(
                    source_id=splitter_node.id,
                    target_id=fdh_node.id,
                    kind="containment",
                )
            )
    return _view("splitter", splitter_id, nodes, edges)


def _pop_site_view(db, pop_site_id) -> NetworkGraphView | None:
    site = db.get(PopSite, pop_site_id)
    if site is None:
        return None
    site_node = _identity_node(
        f"pop_site:{site.id}",
        "pop",
        site.name or "Site",
        asset_id=site.id,
        tooltip="site · containment is not connectivity",
    )
    nodes = [site_node]
    edges = []
    devices = (
        db.query(NetworkDevice)
        .filter(
            NetworkDevice.pop_site_id == site.id,
            NetworkDevice.is_active.is_(True),
        )
        .order_by(NetworkDevice.name)
        .all()
    )
    annotate_operational_status(devices[:GROUP_THRESHOLD])
    for device in devices[:GROUP_THRESHOLD]:
        node = _device_node(device)
        nodes.append(node)
        edges.append(
            NetworkGraphEdge(
                source_id=node.id, target_id=site_node.id, kind="containment"
            )
        )
    if len(devices) > GROUP_THRESHOLD:
        cohort = _cohort_node(
            f"cohort:devices:{site.id}",
            f"+{len(devices) - GROUP_THRESHOLD} more devices",
            href="/admin/network/network-devices",
            permission="network:device:read",
        )
        nodes.append(cohort)
        edges.append(
            NetworkGraphEdge(
                source_id=cohort.id, target_id=site_node.id, kind="containment"
            )
        )
    return _view("pop_site", pop_site_id, nodes, edges)


# --- inspector -------------------------------------------------------------


def build_inspector(
    db: Session,
    subject: str,
    *,
    include_customer_identity: bool,
) -> ExplorerInspector | None:
    """On-demand inspector for one subject; None when it cannot be proven."""

    kind, _, raw_id = subject.partition(":")
    if kind in _CUSTOMER_SUBJECT_KINDS and not include_customer_identity:
        return None
    builder = {
        "subscription": _subscription_inspector,
        "subscriber": _subscriber_inspector,
        "ont": _ont_inspector,
        "radio": _radio_inspector,
        "device": _device_inspector,
        "nas": _nas_inspector,
        "olt": _olt_inspector,
        "pon_port": _pon_inspector,
        "fdh": _fdh_inspector,
        "splitter": _splitter_inspector,
        "pop_site": _pop_site_inspector,
    }.get(kind)
    if builder is None or not raw_id:
        return None
    try:
        subject_id = uuid_module.UUID(raw_id)
    except ValueError:
        return None
    try:
        return builder(db, subject_id, include_customer_identity)
    except Exception:
        logger.warning(
            "Explorer inspector failed for subject %s", subject, exc_info=True
        )
        return None


def _live_incidents(
    db: Session,
    *,
    node_id=None,
    basestation_id=None,
    fdh_id=None,
) -> tuple[InspectorIncident, ...]:
    from app.services.topology.outage import detection_source, list_open_incidents

    picked = []
    for incident in list_open_incidents(db):
        if (
            (node_id is not None and incident.root_node_id == node_id)
            or (
                basestation_id is not None and incident.basestation_id == basestation_id
            )
            or (fdh_id is not None and incident.fdh_cabinet_id == fdh_id)
        ):
            picked.append(incident)
    return tuple(
        InspectorIncident(
            incident_id=str(incident.id),
            status=str(incident.status),
            presentation=outage_status_presentation(incident.status),
            detection_source=detection_source(incident),
            started_at=(
                incident.started_at.isoformat() if incident.started_at else None
            ),
        )
        for incident in picked[:5]
    )


def _inspector(
    subject_kind: str,
    subject_id,
    label: str,
    **kwargs,
) -> ExplorerInspector:
    href = kwargs.pop("href", None)
    href_permission = kwargs.pop("href_permission", None)
    if href is None:
        href, href_permission = asset_link(
            {"device": "network_device", "pop_site": "pop"}.get(
                subject_kind, subject_kind
            ),
            subject_id,
        )
    return ExplorerInspector(
        subject=f"{subject_kind}:{subject_id}",
        kind=subject_kind,
        kind_label=_SUBJECT_KIND_LABELS.get(subject_kind, subject_kind),
        label=label,
        href=href,
        href_permission=href_permission,
        **kwargs,
    )


def _device_link_facts(db, device_id) -> list[InspectorFact]:
    """Top device links with owner-computed utilization, bounded to five.

    network_topology owns link capacity and utilization; this projection only
    composes the display string.
    """

    from app.services.network_topology import node_summary

    try:
        summary = node_summary(db, str(device_id))
    except Exception:
        return []
    links = sorted(
        summary.get("links", []),
        key=lambda link: link.get("utilization_pct") or 0,
        reverse=True,
    )[:5]
    facts = []
    for link in links:
        other = link.get("target_device") or link.get("source_device") or "link"
        utilization = link.get("utilization_pct")
        capacity = link.get("capacity_bps")
        capacity_display = (
            f"{round(capacity / 1e6)} Mbps" if capacity else "unknown capacity"
        )
        utilization_display = f"{utilization:.0f}%" if utilization is not None else "—"
        facts.append(
            InspectorFact(
                label=f"Link · {other}",
                display=f"{utilization_display} of {capacity_display}",
            )
        )
    return facts


def _device_inspector(db, device_id, _identity) -> ExplorerInspector | None:
    device = db.get(NetworkDevice, device_id)
    if device is None:
        return None
    annotate_operational_status([device])
    operational = getattr(device, "operational", None)
    impact = affected.affected_customers(db, node=device)
    facts = [InspectorFact(label="Role", display=device.role or "—")]
    if device.pop_site_id:
        site = db.get(PopSite, device.pop_site_id)
        if site is not None:
            facts.append(
                InspectorFact(
                    label="Site",
                    display=site.name or "Site",
                    href=f"{EXPLORER_PATH}?subject=pop_site:{site.id}",
                    href_permission=EXPLORER_PAGE_PERMISSION,
                )
            )
    facts.extend(_device_link_facts(db, device.id))
    return _inspector(
        "device",
        device_id,
        device.name or device.hostname or str(device_id),
        state_presentation=(
            device_operational_status_presentation(operational)
            if operational
            else topology_hop_status_presentation("unknown")
        ),
        state_reason=getattr(operational, "reason", None),
        observed_at=(
            device.live_status_at.isoformat()
            if getattr(device, "live_status_at", None)
            else None
        ),
        facts=tuple(facts),
        affected_count=impact["count"],
        affected_online=impact["online_count"],
        incidents=_live_incidents(db, node_id=device.id),
    )


def _ont_inspector(db, ont_id, identity) -> ExplorerInspector | None:
    from app.services.network.ont_status import get_optical_metrics

    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return None
    effective = resolve_effective_ont_status(ont)
    status_word = str(getattr(effective.status, "value", effective.status))
    state = {"online": "up", "offline": "down"}.get(status_word, "unknown")
    optical = get_optical_metrics(db, ont)
    measurements = []
    for name, label, value, unit in (
        ("onu_rx_dbm", "ONT receive power", optical.onu_rx_dbm, "dBm"),
        ("onu_tx_dbm", "ONT transmit power", optical.onu_tx_dbm, "dBm"),
        ("olt_rx_dbm", "OLT receive power", optical.olt_rx_dbm, "dBm"),
        ("temperature_c", "Temperature", optical.temperature_c, "°C"),
    ):
        if value is None:
            continue
        measurements.append(
            NetworkGraphMeasurement(
                name=name,
                label=label,
                display=f"{value} {unit}",
                value=float(value),
                unit=unit,
                observed_at=optical.fetched_at,
            )
        )
    customer360 = None
    assignment = (
        db.query(OntAssignment)
        .filter(
            OntAssignment.ont_unit_id == ont_id,
            OntAssignment.active.is_(True),
        )
        .first()
    )
    if identity and assignment is not None and assignment.subscriber_id:
        customer360 = f"/admin/customers/person/{assignment.subscriber_id}"
    return _inspector(
        "ont",
        ont_id,
        ont.serial_number or ont.vendor_serial_number or str(ont_id),
        state_presentation=topology_hop_status_presentation(state),
        state_reason=effective.reason,
        observed_at=(
            ont.olt_status_seen_at.isoformat()
            if getattr(ont, "olt_status_seen_at", None)
            else None
        ),
        measurements=tuple(measurements),
        customer360_href=customer360,
    )


def _radio_inspector(db, radio_id, identity) -> ExplorerInspector | None:
    from app.services.network.radio_signal import resolve_effective_radio_signal

    radio = db.get(CPEDevice, radio_id)
    if radio is None:
        return None
    signal = resolve_effective_radio_signal(radio)
    status = (radio.last_uisp_status or "").lower()
    state = {"active": "up"}.get(
        status,
        "down" if status in ("disconnected", "missing", "vanished") else "unknown",
    )
    measurements: tuple[NetworkGraphMeasurement, ...] = ()
    if signal.signal_dbm is not None:
        suffix = " (stale)" if signal.freshness.value == "stale" else ""
        measurements = (
            NetworkGraphMeasurement(
                name="rf_signal_dbm",
                label="RF signal",
                display=f"{signal.signal_dbm:.0f} dBm{suffix}",
                value=signal.signal_dbm,
                unit="dBm",
                freshness=signal.freshness.value,
                observed_at=signal.observed_at,
            ),
        )
    facts = []
    if radio.parent_network_device_id:
        facts.append(
            InspectorFact(
                label="Serving AP",
                display="Open AP neighbourhood",
                href=f"{EXPLORER_PATH}?subject=device:{radio.parent_network_device_id}",
                href_permission=EXPLORER_PAGE_PERMISSION,
            )
        )
    customer360 = None
    if identity and radio.subscriber_id:
        customer360 = f"/admin/customers/person/{radio.subscriber_id}"
    return _inspector(
        "radio",
        radio_id,
        radio.serial_number or radio.mac_address or str(radio_id),
        state_presentation=topology_hop_status_presentation(state),
        state_reason=signal.reason,
        observed_at=(signal.observed_at.isoformat() if signal.observed_at else None),
        measurements=measurements,
        facts=tuple(facts),
        customer360_href=customer360,
    )


def _matched_network_device(db, matched_type: str, matched_id) -> NetworkDevice | None:
    return (
        db.query(NetworkDevice)
        .filter(
            NetworkDevice.matched_device_type == matched_type,
            NetworkDevice.matched_device_id == matched_id,
        )
        .first()
    )


def _olt_inspector(db, olt_id, _identity) -> ExplorerInspector | None:
    olt = db.get(OLTDevice, olt_id)
    if olt is None:
        return None
    pon_count = (
        db.query(func.count(PonPort.id)).filter(PonPort.olt_id == olt.id).scalar() or 0
    )
    matched = _matched_network_device(db, "olt", olt.id)
    state_presentation = topology_hop_status_presentation("not_applicable")
    state_reason = None
    incidents: tuple[InspectorIncident, ...] = ()
    affected_count = affected_online = None
    if matched is not None:
        annotate_operational_status([matched])
        operational = getattr(matched, "operational", None)
        if operational is not None:
            state_presentation = device_operational_status_presentation(operational)
            state_reason = operational.reason
        impact = affected.affected_customers(db, node=matched)
        affected_count = impact["count"]
        affected_online = impact["online_count"]
        incidents = _live_incidents(db, node_id=matched.id)
    return _inspector(
        "olt",
        olt_id,
        olt.name or olt.hostname or str(olt_id),
        state_presentation=state_presentation,
        state_reason=state_reason,
        facts=(InspectorFact(label="PON ports", display=str(pon_count)),),
        affected_count=affected_count,
        affected_online=affected_online,
        incidents=incidents,
    )


def _nas_inspector(db, nas_id, _identity) -> ExplorerInspector | None:
    nas = db.get(NasDevice, nas_id)
    if nas is None:
        return None
    provisioned = (
        db.query(func.count(Subscription.id))
        .filter(Subscription.provisioning_nas_device_id == nas.id)
        .scalar()
        or 0
    )
    matched = _matched_network_device(db, "nas", nas.id)
    state_presentation = topology_hop_status_presentation("not_applicable")
    state_reason = None
    incidents: tuple[InspectorIncident, ...] = ()
    affected_count = affected_online = None
    if matched is not None:
        annotate_operational_status([matched])
        operational = getattr(matched, "operational", None)
        if operational is not None:
            state_presentation = device_operational_status_presentation(operational)
            state_reason = operational.reason
        impact = affected.affected_customers(db, node=matched)
        affected_count = impact["count"]
        affected_online = impact["online_count"]
        incidents = _live_incidents(db, node_id=matched.id)
    return _inspector(
        "nas",
        nas_id,
        nas.name or str(nas_id),
        state_presentation=state_presentation,
        state_reason=state_reason,
        facts=(
            InspectorFact(label="Provisioned subscriptions", display=str(provisioned)),
        ),
        affected_count=affected_count,
        affected_online=affected_online,
        incidents=incidents,
    )


def _pon_inspector(db, pon_port_id, _identity) -> ExplorerInspector | None:
    from app.services.network.ont_status import effective_ont_online_clause

    pon = db.get(PonPort, pon_port_id)
    if pon is None:
        return None
    assigned_ids = select(OntAssignment.ont_unit_id).where(
        OntAssignment.pon_port_id == pon.id,
        OntAssignment.active.is_(True),
    )
    membership = or_(OntUnit.pon_port_id == pon.id, OntUnit.id.in_(assigned_ids))
    total = db.query(func.count(OntUnit.id)).filter(membership).scalar() or 0
    online = (
        db.query(func.count(OntUnit.id))
        .filter(membership, effective_ont_online_clause())
        .scalar()
        or 0
    )
    facts = [
        InspectorFact(label="ONTs", display=str(total)),
        InspectorFact(label="ONTs online", display=str(online)),
    ]
    if pon.olt_id:
        facts.append(
            InspectorFact(
                label="OLT",
                display="Open OLT neighbourhood",
                href=f"{EXPLORER_PATH}?subject=olt:{pon.olt_id}",
                href_permission=EXPLORER_PAGE_PERMISSION,
            )
        )
    return _inspector(
        "pon_port",
        pon_port_id,
        pon.name or f"PON {pon.port_number}",
        state_presentation=topology_hop_status_presentation("unknown"),
        facts=tuple(facts),
    )


def _fdh_inspector(db, fdh_id, _identity) -> ExplorerInspector | None:
    from app.services.network.outage_impact import resolve_fdh_audience

    fdh = db.get(FdhCabinet, fdh_id)
    if fdh is None:
        return None
    audience = resolve_fdh_audience(db, fdh)
    splitters = (
        db.query(func.count(Splitter.id))
        .filter(Splitter.fdh_id == fdh.id, Splitter.is_active.is_(True))
        .scalar()
        or 0
    )
    return _inspector(
        "fdh",
        fdh_id,
        fdh.name or fdh.code or str(fdh_id),
        state_presentation=topology_hop_status_presentation("not_applicable"),
        facts=(
            InspectorFact(label="Active splitters", display=str(splitters)),
            InspectorFact(
                label="Map",
                display="Open fibre map",
                href="/admin/network/fiber-map",
                href_permission="network:fiber:read",
            ),
        ),
        affected_count=len(audience.subscription_ids),
        incidents=_live_incidents(db, fdh_id=fdh.id),
    )


def _splitter_inspector(db, splitter_id, _identity) -> ExplorerInspector | None:
    splitter = db.get(Splitter, splitter_id)
    if splitter is None:
        return None
    facts = [
        InspectorFact(label="Ratio", display=splitter.splitter_ratio or "—"),
        InspectorFact(
            label="Ports",
            display=(
                f"{splitter.input_ports or 0} in / {splitter.output_ports or 0} out"
            ),
        ),
    ]
    if splitter.fdh_id:
        facts.append(
            InspectorFact(
                label="FDH cabinet",
                display="Open FDH neighbourhood",
                href=f"{EXPLORER_PATH}?subject=fdh:{splitter.fdh_id}",
                href_permission=EXPLORER_PAGE_PERMISSION,
            )
        )
    facts.append(
        InspectorFact(
            label="Map",
            display="Open fibre map",
            href="/admin/network/fiber-map",
            href_permission="network:fiber:read",
        )
    )
    return _inspector(
        "splitter",
        splitter_id,
        splitter.name or "Splitter",
        state_presentation=topology_hop_status_presentation("not_applicable"),
        facts=tuple(facts),
    )


def _pop_site_inspector(db, pop_site_id, _identity) -> ExplorerInspector | None:
    site = db.get(PopSite, pop_site_id)
    if site is None:
        return None
    device_count = (
        db.query(func.count(NetworkDevice.id))
        .filter(
            NetworkDevice.pop_site_id == site.id,
            NetworkDevice.is_active.is_(True),
        )
        .scalar()
        or 0
    )
    return _inspector(
        "pop_site",
        pop_site_id,
        site.name or str(pop_site_id),
        state_presentation=topology_hop_status_presentation("not_applicable"),
        facts=(
            InspectorFact(label="Contained devices", display=str(device_count)),
            InspectorFact(label="Note", display="Containment is not connectivity"),
            InspectorFact(
                label="Map",
                display="Open network map",
                href="/admin/network/map",
                href_permission="network:map:read",
            ),
        ),
        incidents=_live_incidents(db, basestation_id=site.id),
    )


def _subscription_inspector(db, subscription_id, _identity) -> ExplorerInspector | None:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return None
    projection = project_subscription_network_path(db, subscription)
    endpoint = projection.endpoint
    facts = []
    if endpoint.endpoint_display:
        facts.append(
            InspectorFact(label="Serving endpoint", display=endpoint.endpoint_display)
        )
    if projection.view is not None:
        facts.append(
            InspectorFact(
                label="Path",
                display=(
                    "complete"
                    if projection.view.complete
                    else f"{len(projection.view.gaps)} gap(s)"
                ),
            )
        )
    return _inspector(
        "subscription",
        subscription_id,
        subscription.login or subscription.ipv4_address or "Subscription",
        href=None,
        state_presentation=endpoint.source_presentation,
        state_reason=endpoint.gap,
        facts=tuple(facts),
        customer360_href=(
            f"/admin/customers/person/{subscription.subscriber_id}"
            if subscription.subscriber_id
            else None
        ),
    )


def _subscriber_inspector(db, subscriber_id, _identity) -> ExplorerInspector | None:
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        return None
    subscription_count = (
        db.query(func.count(Subscription.id))
        .filter(Subscription.subscriber_id == subscriber_id)
        .scalar()
        or 0
    )
    return _inspector(
        "subscriber",
        subscriber_id,
        _subscriber_label(subscriber) or "Customer",
        facts=(InspectorFact(label="Subscriptions", display=str(subscription_count)),),
        customer360_href=f"/admin/customers/person/{subscriber_id}",
    )


# --- coverage and drift ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class MediumCoverage:
    """Per-access-medium path completeness, calculated per subscription."""

    medium: str
    label: str
    total: int
    complete: int

    @property
    def percent(self) -> float | None:
        if not self.total:
            return None
        return round(100.0 * self.complete / self.total, 1)


@dataclass(frozen=True, slots=True)
class CoverageMetric:
    """One drift/coverage worklist with its canonical repair destination."""

    key: str
    label: str
    count: int
    presentation: StatusPresentation
    detail: str | None = None
    href: str | None = None
    href_permission: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkCoverage:
    """Topology-quality projection: per-subscription coverage plus drift.

    Aggregate device counts cannot prove a continuous customer path, so
    coverage is derived from the per-subscription gap classification that is
    contractually kept in sync with resolve_customer_path.
    """

    evaluated_at: datetime
    active_subscriptions: int
    complete_paths: int
    gap_counts: tuple[tuple[str, int], ...]
    by_medium: tuple[MediumCoverage, ...]
    metrics: tuple[CoverageMetric, ...]

    @property
    def coverage_percent(self) -> float | None:
        if not self.active_subscriptions:
            return None
        return round(100.0 * self.complete_paths / self.active_subscriptions, 1)


_MEDIUM_LABELS = {
    "fiber": "Fibre",
    "wireless": "Wireless",
    "nas": "NAS-only",
    "unknown": "Unknown medium",
}


def build_network_coverage(db: Session) -> NetworkCoverage:
    """Compose per-subscription coverage and the open drift worklists."""

    from collections import Counter

    from app.services.status_presentation import coverage_metric_presentation
    from app.services.topology.gaps import classify_active_subscriptions

    classified = classify_active_subscriptions(db)
    total = len(classified)
    complete = sum(1 for row in classified if not row["gap"])
    gap_counter = Counter(row["gap"] for row in classified if row["gap"])
    medium_totals: Counter = Counter(row["medium"] for row in classified)
    medium_complete: Counter = Counter(
        row["medium"] for row in classified if not row["gap"]
    )
    by_medium = tuple(
        MediumCoverage(
            medium=medium,
            label=_MEDIUM_LABELS.get(medium, medium.title()),
            total=medium_totals[medium],
            complete=medium_complete.get(medium, 0),
        )
        for medium in sorted(medium_totals)
    )

    metrics: list[CoverageMetric] = []

    subscription_gaps = total - complete
    metrics.append(
        CoverageMetric(
            key="subscription_gaps",
            label="Subscriptions without a complete path",
            count=subscription_gaps,
            presentation=coverage_metric_presentation(subscription_gaps),
            detail=(
                ", ".join(
                    f"{code}: {count}" for code, count in sorted(gap_counter.items())
                )
                or None
            ),
            href=TOPOLOGY_GAPS_HREF,
            href_permission=TOPOLOGY_GAPS_PERMISSION,
        )
    )

    unproven = _forwarding_unproven_count(db)
    metrics.append(
        CoverageMetric(
            key="forwarding_unproven",
            label="Forwarding declarations without current agreement",
            count=unproven.count,
            presentation=coverage_metric_presentation(unproven.count),
            detail=unproven.detail,
        )
    )

    orphan_devices = (
        db.query(func.count(NetworkDevice.id))
        .filter(
            NetworkDevice.matched_device_id.is_(None),
            NetworkDevice.is_active.is_(True),
        )
        .scalar()
        or 0
    )
    metrics.append(
        CoverageMetric(
            key="orphan_devices",
            label="Monitored devices with no provisioning match",
            count=orphan_devices,
            presentation=coverage_metric_presentation(orphan_devices),
            href=TOPOLOGY_GAPS_HREF,
            href_permission=TOPOLOGY_GAPS_PERMISSION,
        )
    )

    radio_queue = _unmatched_radio_queue_metric(db)
    metrics.append(radio_queue)

    onts_without_pon = (
        db.query(func.count(OntUnit.id))
        .filter(
            OntUnit.pon_port_id.is_(None),
            ~OntUnit.id.in_(
                select(OntAssignment.ont_unit_id).where(
                    OntAssignment.active.is_(True),
                    OntAssignment.pon_port_id.isnot(None),
                )
            ),
        )
        .scalar()
        or 0
    )
    metrics.append(
        CoverageMetric(
            key="onts_without_pon",
            label="ONTs with no PON association",
            count=onts_without_pon,
            presentation=coverage_metric_presentation(onts_without_pon),
            href="/admin/network/unconfigured-onts",
            href_permission="network:olt:read",
        )
    )

    onts_without_plant = (
        db.query(func.count(OntUnit.id))
        .filter(
            OntUnit.splitter_port_id.is_(None),
            OntUnit.pon_port_id.isnot(None),
        )
        .scalar()
        or 0
    )
    metrics.append(
        CoverageMetric(
            key="onts_without_plant",
            label="Connected ONTs with no splitter/FDH association",
            count=onts_without_plant,
            presentation=coverage_metric_presentation(onts_without_plant),
            href="/admin/network/fiber-trace",
            href_permission="network:fiber:read",
        )
    )

    return NetworkCoverage(
        evaluated_at=datetime.now(UTC),
        active_subscriptions=total,
        complete_paths=complete,
        gap_counts=tuple(sorted(gap_counter.items())),
        by_medium=by_medium,
        metrics=tuple(metrics),
    )


@dataclass(frozen=True, slots=True)
class _UnprovenForwarding:
    count: int
    detail: str | None


def _forwarding_unproven_count(db: Session) -> _UnprovenForwarding:
    from app.services.network.forwarding_topology import (
        reconcile_forwarding_topology,
    )

    try:
        report = reconcile_forwarding_topology(db)
    except Exception:
        logger.warning("Forwarding coverage read failed", exc_info=True)
        return _UnprovenForwarding(count=0, detail="forwarding report unavailable")
    open_states = {
        state: count
        for state, count in report.state_counts.items()
        if state != "agreement" and count
    }
    return _UnprovenForwarding(
        count=sum(open_states.values()),
        detail=(
            ", ".join(
                f"{state}: {count}" for state, count in sorted(open_states.items())
            )
            or None
        ),
    )


def _unmatched_radio_queue_metric(db: Session) -> CoverageMetric:
    from app.models.support import Ticket
    from app.services.status_presentation import coverage_metric_presentation

    resolved = ("resolved", "closed", "canceled", "merged")
    open_query = db.query(Ticket).filter(
        Ticket.ticket_type == "unmatched_radio",
        ~Ticket.status.in_(resolved),
    )
    count = open_query.count()
    oldest = open_query.order_by(Ticket.created_at).limit(1).first() if count else None
    detail = None
    if oldest is not None and oldest.created_at is not None:
        created_at = oldest.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age_days = max((datetime.now(UTC) - created_at).days, 0)
        detail = f"oldest open {age_days} day(s)"
    return CoverageMetric(
        key="unmatched_radio_queue",
        label="Unmatched-radio review queue",
        count=count,
        presentation=coverage_metric_presentation(count),
        detail=detail,
        href=UNMATCHED_RADIO_QUEUE_HREF,
        href_permission=UNMATCHED_RADIO_QUEUE_PERMISSION,
    )
