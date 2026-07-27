"""Project accepted vendor as-built evidence into fiber plant.

Vendors draw what they actually built and staff accept it, but the accepted
geometry never reached the network record: ``as_built_routes`` was referenced
only by vendor services, so an accepted as-built proved a payment was due and
left the fiber map unchanged. ``AsBuiltRoute.fiber_segment_id`` — a real FK to
``fiber_segments`` — has existed all along with nothing writing it.

Ownership shape (source-of-truth standard):

* ``operations.vendor_project_records`` owns the as-built evidence and its
  review state. That stays authoritative and is never written here.
* This reconciler owns exactly one derived thing: the ``FiberSegment``
  projection of an accepted as-built, and the ``fiber_segment_id`` link that
  binds them.

Because the evidence is authoritative and the segment is derived, a lost or
corrupted segment is repairable: ``reconcile_accepted_as_builts`` rebuilds from
the accepted rows alone. The projection is never the only copy of the truth.

**Built is not the same as operational.** ``fiber_segments`` enforces that an
active segment has both endpoints bound and distinct, which is the schema
stating that a cable nobody has spliced into anything is not yet part of the
network. So the projection creates the segment ``is_active=False``: the cable
exists, its route is recorded, and it is bound to the evidence that proves it
was built — but connecting it into the graph, and thereby activating it, is
``network.fiber_topology``'s decision. A vendor drawing a line does not decide
what it splices into.

What it deliberately does not do:

* It never *retires* plant. A rejected or superseded as-built leaves any
  segment it already produced in place, because removing cable from the network
  record is an operational decision with consequences this reconciler cannot
  see (traces, splices, customer paths).
* It never binds endpoints or activates a segment, for the reason above. It
  also never deactivates one that topology has since activated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import FiberCableType, FiberSegment, FiberSegmentType
from app.models.vendor_routes import (
    AsBuiltLineItem,
    AsBuiltRoute,
    AsBuiltRouteStatus,
    InstallationProject,
)
from app.services.common import coerce_uuid

# Vendors describe cable in their own words on the as-built line items. Map the
# recognisable ones; anything else leaves cable_type unset rather than guessing,
# because a wrong cable type in the plant record is worse than a missing one.
_CABLE_TYPE_ALIASES: dict[str, FiberCableType] = {
    "single_mode": FiberCableType.single_mode,
    "singlemode": FiberCableType.single_mode,
    "sm": FiberCableType.single_mode,
    "multi_mode": FiberCableType.multi_mode,
    "multimode": FiberCableType.multi_mode,
    "mm": FiberCableType.multi_mode,
    "armored": FiberCableType.armored,
    "armoured": FiberCableType.armored,
    "aerial": FiberCableType.aerial,
    "adss": FiberCableType.aerial,
    "underground": FiberCableType.underground,
    "duct": FiberCableType.underground,
    "direct_buried": FiberCableType.direct_buried,
    "buried": FiberCableType.direct_buried,
}


class AsBuiltPlantProjectionError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


@dataclass(frozen=True, slots=True)
class PlantProjectionOutcome:
    as_built_id: str
    fiber_segment_id: str | None
    action: str  # created | updated | skipped
    reason: str | None = None


def _normalize_cable_type(value: str | None) -> FiberCableType | None:
    key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _CABLE_TYPE_ALIASES.get(key)


def _plant_attributes(db: Session, as_built: AsBuiltRoute) -> dict[str, Any]:
    """Derive the segment shape from the as-built and its line items.

    The line items are where the vendor states cable type and fiber count, so
    the plant attributes come from the same evidence staff accepted rather than
    from a separate staff entry that could disagree with it.
    """
    items = list(
        db.scalars(
            select(AsBuiltLineItem)
            .where(AsBuiltLineItem.as_built_id == as_built.id)
            .where(AsBuiltLineItem.is_active.is_(True))
            .order_by(AsBuiltLineItem.created_at.asc())
        )
    )
    fiber_count = next(
        (item.fiber_count for item in items if (item.fiber_count or 0) > 0), None
    )
    cable_type = next(
        (
            resolved
            for item in items
            if (resolved := _normalize_cable_type(item.cable_type)) is not None
        ),
        None,
    )
    return {"fiber_count": fiber_count, "cable_type": cable_type}


def _segment_name(db: Session, as_built: AsBuiltRoute) -> str:
    """``fiber_segments.name`` is unique, so the as-built id fragment is part
    of the name rather than trusting project names to differ."""
    project = db.get(InstallationProject, as_built.project_id)
    native = getattr(project, "project", None)
    label = getattr(native, "name", None) or str(as_built.project_id)
    suffix = f" — as-built v{as_built.version} [{str(as_built.id)[:8]}]"
    return f"{label[: 160 - len(suffix)]}{suffix}"


def project_accepted_as_built(
    db: Session,
    as_built_id: str,
) -> PlantProjectionOutcome:
    """Create or refresh the fiber segment one accepted as-built represents.

    Staged only — the caller owns the transaction, so the projection commits
    with whatever decision triggered it.
    """
    as_built = db.get(AsBuiltRoute, coerce_uuid(as_built_id))
    if as_built is None:
        raise AsBuiltPlantProjectionError(
            "as_built_not_found", "As-built route not found.", kind="not_found"
        )
    if as_built.status != AsBuiltRouteStatus.accepted.value:
        # Only accepted evidence becomes plant. Submitted or rejected geometry
        # is a claim, not a record of the network.
        return PlantProjectionOutcome(
            as_built_id=str(as_built.id),
            fiber_segment_id=(
                str(as_built.fiber_segment_id) if as_built.fiber_segment_id else None
            ),
            action="skipped",
            reason="as_built_not_accepted",
        )
    if as_built.route_geom is None:
        # An accepted as-built with no geometry is legitimate (line-item-only
        # evidence for work with nothing to draw); it simply is not a cable.
        return PlantProjectionOutcome(
            as_built_id=str(as_built.id),
            fiber_segment_id=None,
            action="skipped",
            reason="no_route_geometry",
        )

    attributes = _plant_attributes(db, as_built)
    if not attributes["fiber_count"]:
        # fiber_segments enforces a positive fiber_count on active operational
        # rows, so projecting without one would fail the check constraint.
        # Surface it as a skip staff can act on, not a crash.
        return PlantProjectionOutcome(
            as_built_id=str(as_built.id),
            fiber_segment_id=None,
            action="skipped",
            reason="missing_fiber_count",
        )

    segment = (
        db.get(FiberSegment, as_built.fiber_segment_id)
        if as_built.fiber_segment_id
        else None
    )
    created = segment is None
    if segment is None:
        segment = FiberSegment(
            name=_segment_name(db, as_built),
            segment_type=FiberSegmentType.distribution,
            # Built, not yet operational: activating requires bound endpoints,
            # which is a topology decision this owner does not make.
            is_active=False,
        )
        db.add(segment)

    segment.route_geom = as_built.route_geom
    segment.length_m = as_built.actual_length_meters
    segment.fiber_count = attributes["fiber_count"]
    if attributes["cable_type"] is not None:
        segment.cable_type = attributes["cable_type"]
    # ``is_active`` is intentionally untouched on refresh: once topology has
    # connected and activated this cable, a re-accepted variation corrects its
    # route without knocking it out of service.
    db.flush()

    as_built.fiber_segment_id = segment.id
    return PlantProjectionOutcome(
        as_built_id=str(as_built.id),
        fiber_segment_id=str(segment.id),
        action="created" if created else "updated",
    )


def reconcile_accepted_as_builts(
    db: Session,
    *,
    apply: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Repair the projection from the authoritative accepted evidence.

    The reconciler exists so a dropped event, a failed transaction, or a
    restored backup cannot leave the fiber map permanently behind the accepted
    as-builts. Running it twice changes nothing the first run already did.
    """
    query = (
        select(AsBuiltRoute)
        .where(AsBuiltRoute.status == AsBuiltRouteStatus.accepted.value)
        .order_by(AsBuiltRoute.reviewed_at.asc().nullslast())
    )
    if limit is not None:
        query = query.limit(limit)

    counts = {"created": 0, "updated": 0, "skipped": 0}
    for as_built in db.scalars(query):
        if not apply and as_built.fiber_segment_id is not None:
            # Dry run reports only what it would change.
            counts["updated"] += 1
            continue
        if not apply:
            counts["created"] += 1
            continue
        outcome = project_accepted_as_built(db, str(as_built.id))
        counts[outcome.action] = counts.get(outcome.action, 0) + 1
    if apply:
        db.commit()
    return counts
