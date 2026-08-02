"""Shared typed network graph view contract.

One vocabulary for rendering a customer's network path today and, later, the
subject-centred network explorer. Domain owners keep deciding topology, state,
and consequences: network.access_path owns identity, order, and gaps;
observation owners own state and freshness; ui.status_presentation owns
label/tone/icon meaning. A NetworkGraphView carries those facts side by side —
it never manufactures an edge, a hop, or a status, and unknown, stale,
unavailable, and not-applicable stay distinct instead of collapsing into one
grey chip.

Routes, templates, and HTMX fragments render these value objects (or their
``to_dict`` projections) without re-deriving meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.schemas.status_presentation import StatusPresentation

NETWORK_GRAPH_SCHEMA_VERSION = 1


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _presentation_dict(presentation: StatusPresentation) -> dict[str, str]:
    return {
        "value": presentation.value,
        "label": presentation.label,
        "tone": presentation.tone.value,
        "icon": presentation.icon.value,
    }


@dataclass(frozen=True, slots=True)
class NetworkGraphEvidence:
    """Who asserted a fact and when — never a substitute for the fact itself."""

    # Named evidence source, e.g. "network.olt_observed_state". Kept verbatim
    # from the owning trace so support can answer "says who?" per hop.
    owner: str
    observed_at: datetime | None = None
    # fresh | stale when the owner qualified observation age; None when the
    # owner reports no age semantics (which is not the same as fresh).
    freshness: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "observed_at": _isoformat(self.observed_at),
            "freshness": self.freshness,
        }


@dataclass(frozen=True, slots=True)
class NetworkGraphMeasurement:
    """One owner-provided reading (optical power, RF signal) with provenance.

    ``display`` is composed by the projection owner so every surface renders
    the same string; templates do not format units or precision themselves.
    """

    name: str
    label: str
    display: str
    value: float | None = None
    unit: str | None = None
    freshness: str | None = None
    observed_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "display": self.display,
            "value": self.value,
            "unit": self.unit,
            "freshness": self.freshness,
            "observed_at": _isoformat(self.observed_at),
        }


@dataclass(frozen=True, slots=True)
class NetworkGraphNode:
    """One asset on the path, with state and presentation resolved by owners.

    ``kind`` mirrors the access-path trace vocabulary (ont | radio | pon_port |
    olt | ap | nas | network_device). ``state`` stays the owner's word —
    an unenriched hop is honestly "unknown", never dressed up as "up".
    """

    id: str
    kind: str
    label: str
    presentation: StatusPresentation
    state: str = "unknown"
    asset_id: str | None = None
    # Hover summary composed by the projection owner (kind, seen-at, source).
    tooltip: str | None = None
    evidence: NetworkGraphEvidence | None = None
    measurements: tuple[NetworkGraphMeasurement, ...] = ()
    # Deep link to the asset's own page, plus the permission the viewer needs
    # for that destination. Renderers show the link only when the viewer holds
    # the permission; the projection never varies the facts by viewer.
    href: str | None = None
    href_permission: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "presentation": _presentation_dict(self.presentation),
            "asset_id": self.asset_id,
            "tooltip": self.tooltip,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "measurements": [m.to_dict() for m in self.measurements],
            "href": self.href,
            "href_permission": self.href_permission,
        }


@dataclass(frozen=True, slots=True)
class NetworkGraphEdge:
    """An ordered adjacency projected from an owner's path — never inferred.

    "path_order" edges restate the owning trace's hop order. No edge kind may
    be constructed from names, geography, or proximity.
    """

    source_id: str
    target_id: str
    kind: str = "path_order"

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class NetworkGraphGap:
    """A point where the path stops being provable, rendered inside the path."""

    code: str
    message: str
    presentation: StatusPresentation
    # The last proven node before the gap; None when nothing resolved at all.
    after_node_id: str | None = None
    # Canonical review-queue destination able to repair this gap, when one
    # exists, plus the permission that destination requires. The UI never
    # bridges a gap itself.
    repair_href: str | None = None
    repair_permission: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "presentation": _presentation_dict(self.presentation),
            "after_node_id": self.after_node_id,
            "repair_href": self.repair_href,
            "repair_permission": self.repair_permission,
        }


@dataclass(frozen=True, slots=True)
class NetworkGraphView:
    """A bounded, subject-centred graph ready to render."""

    subject_kind: str
    subject_id: str
    access_kind: str | None
    evaluated_at: datetime
    nodes: tuple[NetworkGraphNode, ...] = ()
    edges: tuple[NetworkGraphEdge, ...] = ()
    gaps: tuple[NetworkGraphGap, ...] = field(default=())

    @property
    def complete(self) -> bool:
        return bool(self.nodes) and not self.gaps

    def to_dict(self) -> dict[str, object]:
        return {
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "access_kind": self.access_kind,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "gaps": [gap.to_dict() for gap in self.gaps],
            "complete": self.complete,
            "evaluated_at": self.evaluated_at.isoformat(),
            "schema_version": NETWORK_GRAPH_SCHEMA_VERSION,
        }
