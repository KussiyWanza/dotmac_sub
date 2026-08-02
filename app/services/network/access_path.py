"""Single source of truth for customer-to-network access paths."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.forwarding_topology import ForwardingTopologyDeclaration
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.services.common import coerce_uuid
from app.services.fiber_topology import localize_fiber_fault, trace_fiber_subscription
from app.services.network._common import decode_huawei_hex_serial
from app.services.network.fiber_physical_continuity import (
    resolve_subscription_core_continuity,
)
from app.services.network.fiber_plant_integrity import cable_capacity
from app.services.network.forwarding_topology import (
    project_authoritative_forwarding_graph,
)
from app.services.network.radio_signal import resolve_effective_radio_signal
from app.services.topology.customer_path import CustomerPath, resolve_customer_path


@dataclass(frozen=True)
class AccessPathSummary:
    subscription_id: object
    subscriber_id: object | None
    access_kind: str | None
    node_id: object | None
    node_name: str | None
    basestation_id: object | None
    basestation_name: str | None
    gap: str | None
    live_session: bool
    # Serving access endpoint. Resolved by CustomerPath but previously dropped
    # here, which forced the customer page to fall back to the static
    # provisioning NAS site and made "which cabinet is this customer on?" an
    # unanswerable question in the admin UI.
    access_device_id: object | None = None
    access_device_name: str | None = None
    pon_port_id: object | None = None
    pon_port_label: str | None = None
    ont_id: object | None = None
    ont_serial: str | None = None
    radio_id: object | None = None
    radio_label: str | None = None
    # Wireless RF observation, resolved through radio_signal so a value is
    # only ever presented alongside its freshness (fresh | stale) — a stale or
    # cleared observation must not render as a current signal.
    radio_signal_dbm: float | None = None
    radio_signal_freshness: str | None = None
    radio_signal_observed_at: datetime | None = None
    # Projection of customer_path.RadioResolutionTier ('subscription' exact /
    # 'subscriber_legacy' pre-binding fallback). None off the wireless arm.
    # Stringified here like endpoint_source: this summary is the JSON/template
    # boundary; the typed enum lives on CustomerPath.
    radio_resolution: str | None = None
    # True when an active radio exists but has no parent AP edge: the
    # repairable "AP unresolved" gap, surfaced explicitly instead of letting
    # the card silently degrade to the coarse NAS endpoint.
    radio_ap_unresolved: bool = False
    # Composed here, never in a template, so admin/portal/API render one string.
    endpoint_display: str | None = None
    # Which record this endpoint came from, named rather than implied. Using a
    # single live-vs-provisioned flag misreports the fibre and wireless arms:
    # CustomerPath.live_session is specific to the NAS arm, so a perfectly
    # current OLT endpoint would be chipped "provisioned" and read as stale.
    #   live_session      - NAS arm, resolved from a live RADIUS session
    #   provisioning      - NAS arm, resolved from the provisioning NAS
    #   ont_assignment    - fibre arm, resolved from the ONT assignment record
    #   uisp_observation  - wireless arm, resolved from the UISP relationship
    #   unresolved        - Sub cannot prove any of them
    endpoint_source: str = "unresolved"
    # False when the path resolved a device but still carries a gap. Kept apart
    # from endpoint_source so provenance and completeness stop contradicting
    # each other on the same card.
    endpoint_complete: bool = True


@dataclass(frozen=True)
class TopologyTraceNode:
    """One element of the active-path chain shown to support."""

    kind: str  # ont | pon_port | olt | ap | nas | network_device
    label: str
    asset_id: object | None
    # Health is layered on by observation owners (see network.service_diagnosis).
    # access_path owns identity and ordering only, so an unenriched trace is
    # honestly "unknown" rather than falsely "up".
    state: str = "unknown"
    observed_at: datetime | None = None
    source: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TopologyTraceBreak:
    """A point where the chain stops being provable."""

    code: str
    message: str
    after_index: int | None


@dataclass(frozen=True)
class SubscriberTopologyTrace:
    subscription_id: object
    subscriber_id: object | None
    access_kind: str | None
    nodes: tuple[TopologyTraceNode, ...]
    breaks: tuple[TopologyTraceBreak, ...]
    evaluated_at: datetime

    @property
    def complete(self) -> bool:
        return bool(self.nodes) and not self.breaks

    def to_dict(self) -> dict[str, object]:
        """JSON-safe by construction.

        asdict() preserves UUIDs and datetimes, which json.dumps rejects, so
        every id is stringified and every timestamp isoformatted here rather
        than left for each consumer to discover the hard way.
        """

        return {
            "subscription_id": str(self.subscription_id),
            "subscriber_id": str(self.subscriber_id)
            if self.subscriber_id is not None
            else None,
            "access_kind": self.access_kind,
            "nodes": [
                {
                    "kind": node.kind,
                    "label": node.label,
                    "asset_id": str(node.asset_id)
                    if node.asset_id is not None
                    else None,
                    "state": node.state,
                    "observed_at": node.observed_at.isoformat()
                    if node.observed_at
                    else None,
                    "source": node.source,
                    "detail": node.detail,
                }
                for node in self.nodes
            ],
            "breaks": [asdict(gap) for gap in self.breaks],
            "complete": self.complete,
            "evaluated_at": self.evaluated_at.isoformat(),
            "schema_version": 1,
        }


@dataclass(frozen=True)
class FiberServicePathHop:
    domain: str
    kind: str
    asset_id: object
    label: str
    evidence_refs: tuple[str, ...]
    capacity: dict[str, object] | None = None


@dataclass(frozen=True)
class FiberServicePathGap:
    code: str
    domain: str
    message: str
    after_asset_id: object | None = None


@dataclass(frozen=True)
class FiberEndToEndPath:
    subscription_id: object
    evaluated_at: datetime
    hops: tuple[FiberServicePathHop, ...]
    gaps: tuple[FiberServicePathGap, ...]
    passive_complete: bool
    core_continuity_complete: bool
    core_continuity_sha256: str
    forwarding_complete: bool
    provisioning_nas_device_id: object | None
    live_nas_device_id: object | None
    live_nas_state: str
    forwarding_report_sha256: str | None
    forwarding_declaration_ids: tuple[object, ...]
    fault_telemetry_state: str
    fault_candidates: tuple[dict[str, object], ...]
    evidence_sha256: str

    @property
    def complete(self) -> bool:
        return (
            self.passive_complete
            and self.core_continuity_complete
            and self.forwarding_complete
            and not self.gaps
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["complete"] = self.complete
        payload["schema_version"] = 2
        return payload


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            default=str,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _fiber_subscription(db: Session, subscription: Subscription | str) -> Subscription:
    row = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if row is None:
        raise ValueError("subscription not found")
    return row


def _live_nas_id(db: Session, subscription: Subscription) -> uuid.UUID | None:
    if subscription.subscriber_id is None:
        return None
    row = db.execute(
        select(RadiusActiveSession.nas_device_id)
        .where(
            RadiusActiveSession.subscriber_id == subscription.subscriber_id,
            RadiusActiveSession.nas_device_id.is_not(None),
            or_(
                RadiusActiveSession.subscription_id == subscription.id,
                RadiusActiveSession.subscription_id.is_(None),
            ),
        )
        .order_by(
            case(
                (RadiusActiveSession.subscription_id == subscription.id, 0),
                else_=1,
            ),
            RadiusActiveSession.last_update.desc().nullslast(),
            RadiusActiveSession.session_start.desc(),
            RadiusActiveSession.id,
        )
        .limit(1)
    ).first()
    return row[0] if row is not None else None


def _exact_olt_node(
    db: Session, olt_id: object
) -> tuple[NetworkDevice | None, str | None]:
    rows = list(
        db.scalars(
            select(NetworkDevice).where(
                NetworkDevice.is_active.is_(True),
                NetworkDevice.matched_device_type == "olt",
                NetworkDevice.matched_device_id == coerce_uuid(olt_id),
            )
        ).all()
    )
    if not rows:
        return None, "olt_network_device_missing"
    if len(rows) != 1:
        return None, "olt_network_device_conflict"
    return rows[0], None


def _capacity_for_passive_hop(
    db: Session, kind: str, asset_id: object | None
) -> dict[str, object] | None:
    if asset_id is None or not kind.endswith("segment"):
        return None
    try:
        capacity = cable_capacity(db, coerce_uuid(asset_id))
    except (TypeError, ValueError):
        return None
    return {
        "available_fibers": capacity.available_fibers,
        "complete": capacity.complete,
        "damaged_fibers": capacity.damaged_fibers,
        "in_use_fibers": capacity.in_use_fibers,
        "modeled_fibers": capacity.modeled_fibers,
        "reserved_fibers": capacity.reserved_fibers,
        "retired_fibers": capacity.retired_fibers,
        "total_fibers": capacity.total_fibers,
        "unmodeled_fibers": capacity.unmodeled_fibers,
    }


def resolve_fiber_end_to_end_path(
    db: Session,
    subscription: Subscription | str,
    *,
    as_of: datetime | None = None,
    vrf_name: str = "main",
    maximum_forwarding_hops: int = 16,
) -> FiberEndToEndPath:
    """Compose exact ONT/passive plant with agreeing NAS/core forwarding.

    Provisioning NAS identity and live RADIUS NAS observation remain visibly
    separate. Missing edges become typed gaps; no observation or name fallback
    manufactures an authoritative hop.
    """

    subscription_obj = _fiber_subscription(db, subscription)
    evaluated_at = as_of or datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    trace = trace_fiber_subscription(db, subscription_obj.id)
    hops: list[FiberServicePathHop] = []
    gaps = [
        FiberServicePathGap(
            code=f"passive.{gap.code}",
            domain="passive_fiber",
            message=gap.message,
            after_asset_id=gap.after_asset_id,
        )
        for gap in trace.gaps
    ]
    cable_capacity_complete = True
    for hop in reversed(trace.hops):
        if hop.asset_id is None or hop.validation != "validated":
            continue
        capacity = _capacity_for_passive_hop(db, hop.kind, hop.asset_id)
        if capacity is not None and capacity["complete"] is False:
            cable_capacity_complete = False
            gaps.append(
                FiberServicePathGap(
                    code="capacity.cable_inventory_incomplete",
                    domain="passive_capacity",
                    message=(
                        "The cable's exact numbered core inventory does not match "
                        "its declared fiber_count."
                    ),
                    after_asset_id=hop.asset_id,
                )
            )
        hops.append(
            FiberServicePathHop(
                domain="passive_fiber",
                kind=hop.kind,
                asset_id=hop.asset_id,
                label=hop.label,
                evidence_refs=(hop.evidence,),
                capacity=capacity,
            )
        )

    core_continuity = resolve_subscription_core_continuity(db, subscription_obj)
    for gap in core_continuity.gaps:
        gaps.append(
            FiberServicePathGap(
                code=gap.code,
                domain="physical_core",
                message=gap.message,
                after_asset_id=gap.after_asset_id,
            )
        )
    for core_hop in reversed(core_continuity.hops):
        hops.append(
            FiberServicePathHop(
                domain="physical_core",
                kind=core_hop.kind,
                asset_id=core_hop.asset_id,
                label=core_hop.label,
                evidence_refs=(
                    *core_hop.evidence_refs,
                    f"core-continuity-report:{core_continuity.evidence_sha256}",
                ),
            )
        )
    passive_complete = bool(
        trace.customer_trace_complete
        and cable_capacity_complete
        and core_continuity.complete
    )
    olt_hops = [hop for hop in trace.hops if hop.kind == "olt" and hop.asset_id]
    olt_node: NetworkDevice | None = None
    if len(olt_hops) != 1:
        gaps.append(
            FiberServicePathGap(
                code="identity.olt_missing_or_conflicting",
                domain="network_identity",
                message="The passive trace does not name exactly one serving OLT.",
            )
        )
    else:
        olt_node, node_gap = _exact_olt_node(db, olt_hops[0].asset_id)
        if node_gap:
            gaps.append(
                FiberServicePathGap(
                    code=f"identity.{node_gap}",
                    domain="network_identity",
                    message=(
                        "network.identity does not resolve the serving OLT to one "
                        "exact active forwarding device."
                    ),
                    after_asset_id=olt_hops[0].asset_id,
                )
            )

    expected_nas_id = subscription_obj.provisioning_nas_device_id
    expected_nas = (
        db.get(NasDevice, expected_nas_id) if expected_nas_id is not None else None
    )
    nas_node_id: uuid.UUID | None = None
    if expected_nas_id is None:
        gaps.append(
            FiberServicePathGap(
                code="provisioning.nas_missing",
                domain="provisioning",
                message="The subscription has no authoritative provisioning NAS.",
            )
        )
    elif expected_nas is None or not expected_nas.is_active:
        gaps.append(
            FiberServicePathGap(
                code="provisioning.nas_inactive",
                domain="provisioning",
                message="The authoritative provisioning NAS is missing or inactive.",
                after_asset_id=expected_nas_id,
            )
        )
    elif expected_nas.network_device_id is None:
        gaps.append(
            FiberServicePathGap(
                code="identity.nas_network_device_missing",
                domain="network_identity",
                message="The provisioning NAS has no exact NetworkDevice identity.",
                after_asset_id=expected_nas.id,
            )
        )
    else:
        nas_node = db.get(NetworkDevice, expected_nas.network_device_id)
        if nas_node is None or not nas_node.is_active:
            gaps.append(
                FiberServicePathGap(
                    code="identity.nas_network_device_inactive",
                    domain="network_identity",
                    message="The provisioning NAS NetworkDevice is missing or inactive.",
                    after_asset_id=expected_nas.id,
                )
            )
        else:
            nas_node_id = nas_node.id

    live_nas_id = _live_nas_id(db, subscription_obj)
    live_nas_state = (
        "missing_observation"
        if live_nas_id is None
        else "agreement"
        if live_nas_id == expected_nas_id
        else "drift"
    )

    graph = None
    used_declaration_ids: list[uuid.UUID] = []
    forwarding_complete = False
    chain_ids: list[uuid.UUID] = []
    if olt_node is not None:
        graph = project_authoritative_forwarding_graph(db, vrf_name=vrf_name)
        hops.append(
            FiberServicePathHop(
                domain="forwarding",
                kind="access_network_device",
                asset_id=olt_node.id,
                label=olt_node.name,
                evidence_refs=(
                    f"network.identity:olt:{olt_hops[0].asset_id}",
                    f"forwarding-report:{graph.report_sha256}",
                ),
            )
        )
        seen = {olt_node.id}
        current = olt_node.id
        reached_root = current in graph.root_device_ids
        while not reached_root and len(chain_ids) < maximum_forwarding_hops:
            upstream = graph.upstream_by_downstream.get(current)
            declaration_id = graph.declaration_by_downstream.get(current)
            if upstream is None or declaration_id is None:
                break
            if upstream in seen:
                gaps.append(
                    FiberServicePathGap(
                        code="forwarding.cycle",
                        domain="forwarding",
                        message="The agreeing forwarding projection contains a cycle.",
                        after_asset_id=current,
                    )
                )
                break
            device = db.get(NetworkDevice, upstream)
            if device is None or not device.is_active:
                gaps.append(
                    FiberServicePathGap(
                        code="forwarding.device_missing",
                        domain="forwarding",
                        message="An agreeing forwarding hop no longer resolves active.",
                        after_asset_id=current,
                    )
                )
                break
            chain_ids.append(upstream)
            used_declaration_ids.append(declaration_id)
            hops.append(
                FiberServicePathHop(
                    domain="forwarding",
                    kind=("nas" if upstream == nas_node_id else "network_device"),
                    asset_id=upstream,
                    label=device.name,
                    evidence_refs=(f"forwarding-declaration:{declaration_id}",),
                )
            )
            seen.add(upstream)
            current = upstream
            reached_root = current in graph.root_device_ids
        if not reached_root:
            gaps.append(
                FiberServicePathGap(
                    code="forwarding.core_or_border_root_missing",
                    domain="forwarding",
                    message=(
                        "No complete observation-agreeing declaration chain reaches "
                        "an authoritative core or border root."
                    ),
                    after_asset_id=current,
                )
            )
        if nas_node_id is not None and nas_node_id not in chain_ids:
            gaps.append(
                FiberServicePathGap(
                    code="forwarding.provisioning_nas_not_on_path",
                    domain="forwarding",
                    message=(
                        "The agreeing OLT-to-root chain does not traverse the "
                        "subscription's authoritative provisioning NAS."
                    ),
                    after_asset_id=olt_node.id,
                )
            )
        nas_termination = None
        if nas_node_id is not None:
            nas_termination = db.scalar(
                select(ForwardingTopologyDeclaration).where(
                    ForwardingTopologyDeclaration.id.in_(used_declaration_ids),
                    ForwardingTopologyDeclaration.active.is_(True),
                    ForwardingTopologyDeclaration.path_kind == "nas_termination",
                    ForwardingTopologyDeclaration.downstream_device_id == nas_node_id,
                    ForwardingTopologyDeclaration.nas_device_id == expected_nas_id,
                )
            )
            if nas_termination is None:
                gaps.append(
                    FiberServicePathGap(
                        code="forwarding.nas_termination_declaration_missing",
                        domain="forwarding",
                        message=(
                            "The selected chain lacks an exact agreeing NAS "
                            "termination declaration for this provisioning NAS."
                        ),
                        after_asset_id=nas_node_id,
                    )
                )
        forwarding_complete = bool(
            reached_root
            and nas_node_id is not None
            and nas_node_id in chain_ids
            and nas_termination is not None
        )

    localization = localize_fiber_fault(db, subscription_obj.id, now=evaluated_at)
    fault_candidates = tuple(
        {
            "asset_ids": [str(value) for value in candidate.asset_ids],
            "confidence": candidate.confidence,
            "evidence": {
                "asset_id": str(candidate.evidence.asset_id),
                "offline": candidate.evidence.offline,
                "online": candidate.evidence.online,
                "scope": candidate.evidence.scope,
                "stale": candidate.evidence.stale,
                "total": candidate.evidence.total,
            },
            "label": candidate.label,
            "rationale": candidate.rationale,
            "scope": candidate.scope,
            "score": candidate.score,
        }
        for candidate in localization.candidates
    )
    evidence_payload = {
        "evaluated_at": evaluated_at.isoformat(),
        "fault_candidates": fault_candidates,
        "fault_telemetry_state": localization.telemetry_state,
        "forwarding_declaration_ids": [str(value) for value in used_declaration_ids],
        "forwarding_report_sha256": graph.report_sha256 if graph else None,
        "gaps": [asdict(gap) for gap in gaps],
        "hops": [asdict(hop) for hop in hops],
        "live_nas_device_id": str(live_nas_id) if live_nas_id else None,
        "live_nas_state": live_nas_state,
        "passive_complete": passive_complete,
        "core_continuity_complete": core_continuity.complete,
        "core_continuity_sha256": core_continuity.evidence_sha256,
        "provisioning_nas_device_id": (
            str(expected_nas_id) if expected_nas_id else None
        ),
        "subscription_id": str(subscription_obj.id),
    }
    return FiberEndToEndPath(
        subscription_id=subscription_obj.id,
        evaluated_at=evaluated_at,
        hops=tuple(hops),
        gaps=tuple(gaps),
        passive_complete=passive_complete,
        core_continuity_complete=core_continuity.complete,
        core_continuity_sha256=core_continuity.evidence_sha256,
        forwarding_complete=forwarding_complete,
        provisioning_nas_device_id=expected_nas_id,
        live_nas_device_id=live_nas_id,
        live_nas_state=live_nas_state,
        forwarding_report_sha256=graph.report_sha256 if graph else None,
        forwarding_declaration_ids=tuple(used_declaration_ids),
        fault_telemetry_state=localization.telemetry_state,
        fault_candidates=fault_candidates,
        evidence_sha256=_digest(evidence_payload),
    )


def resolve_subscription_access_path(
    db: Session,
    subscription: Subscription | str,
) -> CustomerPath:
    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        raise ValueError("subscription not found")
    return resolve_customer_path(db, subscription_obj)


def _pon_port_label(pon_port: Any | None) -> str | None:
    """Prefer the operator-facing port name ("0/1/3") over the numeric id."""

    if pon_port is None:
        return None
    name = getattr(pon_port, "name", None)
    if name:
        return str(name)
    port_number = getattr(pon_port, "port_number", None)
    return str(port_number) if port_number is not None else None


def _ont_label(ont: Any | None) -> str | None:
    """Match how the rest of the UI prints an ONT serial.

    Huawei vendor serials are stored hex-encoded. Printing the raw column here
    would show the same ONT under two different strings on one page.
    """

    if ont is None:
        return None
    for attribute in ("vendor_serial_number", "serial_number"):
        raw = str(getattr(ont, attribute, "") or "").strip()
        if raw:
            return decode_huawei_hex_serial(raw) or raw
    return None


def _radio_label(radio: Any | None) -> str | None:
    """CPEDevice has no name column; identify it by serial, model or UISP id."""

    if radio is None:
        return None
    for attribute in ("serial_number", "model", "uisp_device_id", "mac_address"):
        value = str(getattr(radio, attribute, "") or "").strip()
        if value:
            return value
    return None


def _endpoint_source(path: CustomerPath) -> str:
    kind = path.access_device_kind
    if path.access_device is None and path.node is None:
        return "unresolved"
    if kind == "olt":
        return "ont_assignment"
    if kind == "ap":
        return "uisp_observation"
    if kind == "nas":
        return "live_session" if path.live_session else "provisioning"
    return "live_session" if path.live_session else "provisioning"


def _endpoint_display(path: CustomerPath) -> str | None:
    """One string every surface renders identically.

    Composed in the service on purpose: templates that build this themselves
    drift apart, and the admin/portal disagreement is exactly what makes
    support escalate.
    """

    device_name = getattr(path.access_device, "name", None)
    if path.access_device_kind == "olt":
        port = _pon_port_label(path.pon_port)
        if device_name and port:
            return f"{device_name} ({port})"
        return device_name or (f"PON {port}" if port else None)
    if path.access_device_kind == "ap":
        node_name = getattr(path.node, "name", None) or device_name
        basestation = getattr(path.basestation, "name", None)
        if node_name and basestation and basestation != node_name:
            return f"{node_name} ({basestation})"
        return node_name or basestation
    if path.access_device_kind == "nas":
        return device_name or getattr(path.node, "name", None)
    return device_name or getattr(path.node, "name", None)


def summarize_subscription_access_path(
    db: Session,
    subscription: Subscription | str,
) -> AccessPathSummary:
    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        raise ValueError("subscription not found")
    path = resolve_customer_path(db, subscription_obj)
    return summarize_customer_path(subscription_obj, path)


def summarize_customer_path(
    subscription: Subscription,
    path: CustomerPath,
) -> AccessPathSummary:
    """Project a resolved path. Split out so callers that already hold a
    CustomerPath do not re-resolve it."""

    effective_signal = (
        resolve_effective_radio_signal(path.radio) if path.radio is not None else None
    )
    unparented = getattr(path, "unparented_radio", None)
    ap_unresolved = path.radio is None and unparented is not None
    return AccessPathSummary(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        access_kind=path.access_device_kind,
        node_id=getattr(path.node, "id", None),
        node_name=getattr(path.node, "name", None),
        basestation_id=getattr(path.basestation, "id", None),
        basestation_name=getattr(path.basestation, "name", None),
        gap=path.gap,
        live_session=path.live_session,
        access_device_id=getattr(path.access_device, "id", None),
        access_device_name=getattr(path.access_device, "name", None),
        pon_port_id=getattr(path.pon_port, "id", None),
        pon_port_label=_pon_port_label(path.pon_port),
        ont_id=getattr(path.ont, "id", None),
        ont_serial=_ont_label(path.ont),
        radio_id=getattr(path.radio, "id", None)
        if path.radio is not None
        else getattr(unparented, "id", None),
        radio_label=_radio_label(path.radio) or _radio_label(unparented),
        radio_signal_dbm=effective_signal.signal_dbm if effective_signal else None,
        radio_signal_freshness=(
            effective_signal.freshness.value if effective_signal else None
        ),
        radio_signal_observed_at=(
            effective_signal.observed_at if effective_signal else None
        ),
        radio_resolution=(
            path.radio_resolution.value
            if getattr(path, "radio_resolution", None) is not None
            else None
        ),
        radio_ap_unresolved=ap_unresolved,
        endpoint_display=_endpoint_display(path),
        endpoint_source=_endpoint_source(path),
        endpoint_complete=path.gap is None,
    )


def _ont_observed_state(ont: Any) -> tuple[str, datetime | None]:
    """Map stored OLT-observed ONU status. Stored telemetry only — never a probe."""

    raw = getattr(ont, "olt_status", None)
    value = str(getattr(raw, "value", raw) or "").lower()
    observed_at = getattr(ont, "olt_status_seen_at", None) or getattr(
        ont, "last_seen_at", None
    )
    if "offline" in value:
        return "down", observed_at
    if "online" in value:
        return "up", observed_at
    return "unknown", observed_at


def _radio_observed_state(radio: Any) -> tuple[str, datetime | None]:
    """Map stored UISP radio status. Stored telemetry only — never a probe."""

    value = str(getattr(radio, "last_uisp_status", None) or "").lower()
    observed_at = getattr(radio, "rf_signal_observed_at", None) or getattr(
        radio, "uisp_synced_at", None
    )
    if value == "active":
        return "up", observed_at
    if value in ("disconnected", "missing", "vanished"):
        return "down", observed_at
    return "unknown", observed_at


def _trace_nodes_for_path(path: CustomerPath) -> list[TopologyTraceNode]:
    nodes: list[TopologyTraceNode] = []

    if path.ont is not None:
        state, observed_at = _ont_observed_state(path.ont)
        nodes.append(
            TopologyTraceNode(
                kind="ont",
                label=_ont_label(path.ont) or "ONT",
                asset_id=getattr(path.ont, "id", None),
                state=state,
                observed_at=observed_at,
                source="network.olt_observed_state",
                detail={
                    "model": getattr(path.ont, "model", None),
                    "onu_rx_signal_dbm": getattr(path.ont, "onu_rx_signal_dbm", None),
                    "olt_rx_signal_dbm": getattr(path.ont, "olt_rx_signal_dbm", None),
                },
            )
        )

    if path.radio is not None:
        state, observed_at = _radio_observed_state(path.radio)
        effective_signal = resolve_effective_radio_signal(path.radio)
        nodes.append(
            TopologyTraceNode(
                kind="radio",
                label=_radio_label(path.radio) or "CPE radio",
                asset_id=getattr(path.radio, "id", None),
                state=state,
                observed_at=observed_at,
                source="network.uisp_ap_station",
                detail={
                    "rf_signal_dbm": effective_signal.signal_dbm,
                    "rf_signal_freshness": effective_signal.freshness.value,
                },
            )
        )

    if path.pon_port is not None:
        nodes.append(
            TopologyTraceNode(
                kind="pon_port",
                label=_pon_port_label(path.pon_port) or "PON",
                asset_id=getattr(path.pon_port, "id", None),
                source="network.access_path",
            )
        )

    if path.access_device is not None:
        nodes.append(
            TopologyTraceNode(
                kind=path.access_device_kind or "network_device",
                label=str(getattr(path.access_device, "name", "access device")),
                asset_id=getattr(path.access_device, "id", None),
                source="network.access_path",
                detail={
                    "basestation": getattr(path.basestation, "name", None),
                },
            )
        )

    for device in path.upstream_chain:
        nodes.append(
            TopologyTraceNode(
                kind="network_device",
                label=str(getattr(device, "name", "device")),
                asset_id=getattr(device, "id", None),
                source="network.forwarding_topology",
            )
        )

    return nodes


def resolve_subscription_topology_trace(
    db: Session,
    subscription: Subscription | str,
    *,
    as_of: datetime | None = None,
) -> SubscriberTopologyTrace:
    """Active-path chain for one subscription: ONT/radio -> PON -> OLT/AP -> upstream.

    Passive plant (splitter, FDH, drop) is intentionally excluded. Those assets
    carry no observable live state, so they widen the support view without
    helping answer "why is this customer down"; they belong to the fibre-plant
    view that owns them.

    The upstream chain is whatever ``CustomerPath`` can prove. A missing NAS hop
    is reported as a break rather than back-filled from the provisioning record,
    keeping this module's rule that no fallback manufactures an authoritative
    hop.
    """

    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        raise ValueError("subscription not found")

    path = resolve_customer_path(db, subscription_obj)
    return build_topology_trace(subscription_obj, path, as_of=as_of)


def build_topology_trace(
    subscription: Subscription,
    path: CustomerPath,
    *,
    as_of: datetime | None = None,
) -> SubscriberTopologyTrace:
    """Build a trace from an already-resolved path.

    Callers that render the endpoint and the chain together resolve the path
    once and project it twice, rather than paying for resolution per surface.
    """

    evaluated_at = as_of or datetime.now(UTC)
    nodes = _trace_nodes_for_path(path)

    breaks: list[TopologyTraceBreak] = []
    if path.gap:
        breaks.append(
            TopologyTraceBreak(
                code=f"path.{path.gap}",
                message="Sub cannot resolve this subscription's access path.",
                after_index=len(nodes) - 1 if nodes else None,
            )
        )
    if path.access_device is not None and not path.upstream_chain:
        breaks.append(
            TopologyTraceBreak(
                code="upstream.unproven",
                message=(
                    "No reviewed forwarding path from this access device toward "
                    "the core is declared."
                ),
                after_index=len(nodes) - 1,
            )
        )
    if not nodes:
        breaks.append(
            TopologyTraceBreak(
                code="path.unresolved",
                message="No access equipment is associated with this subscription.",
                after_index=None,
            )
        )

    return SubscriberTopologyTrace(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        access_kind=path.access_device_kind,
        nodes=tuple(nodes),
        breaks=tuple(breaks),
        evaluated_at=evaluated_at,
    )


def resolve_subscriber_access_paths(
    db: Session,
    subscriber_id,
    *,
    active_only: bool = True,
) -> dict[object, CustomerPath]:
    stmt = select(Subscription).where(
        Subscription.subscriber_id == coerce_uuid(subscriber_id)
    )
    if active_only:
        stmt = stmt.where(Subscription.status == SubscriptionStatus.active)
    stmt = stmt.order_by(Subscription.created_at.desc())
    paths: dict[object, CustomerPath] = {}
    for subscription in db.scalars(stmt).all():
        paths[subscription.id] = resolve_customer_path(db, subscription)
    return paths


def access_path_scope(path: CustomerPath) -> dict[str, Any | None]:
    return {
        "access_kind": path.access_device_kind,
        "node_id": str(path.node.id) if path.node is not None else None,
        "node_name": getattr(path.node, "name", None),
        "basestation_id": str(path.basestation.id)
        if path.basestation is not None
        else None,
        "basestation_name": getattr(path.basestation, "name", None),
        "gap": path.gap,
        "live_session": path.live_session,
    }
