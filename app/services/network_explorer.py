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
from app.services.customer_network_path import (
    asset_link,
    project_subscription_network_path,
)
from app.services.device_operational_status import annotate_operational_status
from app.services.network.ont_status import resolve_effective_ont_status
from app.services.network_graph import (
    NetworkGraphEdge,
    NetworkGraphEvidence,
    NetworkGraphNode,
    NetworkGraphView,
)
from app.services.status_presentation import (
    device_operational_status_presentation,
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
