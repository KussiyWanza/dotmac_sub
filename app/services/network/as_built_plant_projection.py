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
was built — but connecting it into the graph is a staff decision, not something
a vendor's drawing may infer.

**Activation is a separate, explicit command.** ``project_accepted_as_built``
still refuses to invent endpoints, but leaving the projected row inactive
forever made every accepted as-built invisible: every fiber map and plant read
filters ``is_active``, and nothing in the system ever set it. So this module
also owns ``activate_projected_segment`` — the one command that turns *its own*
projected row into operational plant, once an operator states which two
terminations the cable actually landed on.

It is deliberately not a general-purpose "activate a fiber segment" switch:

* it only ever resolves a segment through the ``fiber_segment_id`` backlink of
  an **accepted** as-built, so a segment created by any other owner cannot be
  activated here;
* the operational shape is never satisfied by defaulting. Geometry must already
  be present, and the fiber count comes from the accepted evidence — the
  operator may supply one only when the evidence carries none, and may never
  contradict it;
* endpoint identity, rootedness, and exact core inventory stay with
  ``network.fiber_plant_integrity``. This command binds the endpoints the
  operator names and then submits the result to that validator, so activating
  from an as-built is held to exactly the same operational invariants as a
  reviewed fiber change. It never re-implements them.

What it deliberately does not do:

* It never *retires* plant. A rejected or superseded as-built leaves any
  segment it already produced in place, because removing cable from the network
  record is an operational decision with consequences this reconciler cannot
  see (traces, splices, customer paths).
* It never deactivates a segment, and never re-binds endpoints on a segment
  that is already active — a replayed activation returns the row unchanged.
* It never *infers* endpoints. The projection binds none, and the activation
  command binds only the two an operator explicitly named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    FiberCableType,
    FiberSegment,
    FiberSegmentType,
    FiberTerminationPoint,
)
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


@dataclass(frozen=True, slots=True)
class PlantActivationOutcome:
    """Result of ``activate_projected_segment``.

    ``action`` is ``activated`` for the run that bound the endpoints, and
    ``already_active`` for every replay of it. The replay is deliberately not
    an error: a double-submitted form, a retried task, or two operators racing
    the same queue row must not produce a 500, and must not silently re-bind
    the endpoints a previous activation already recorded. The point ids
    reported on a replay are the ones the segment actually carries, not the
    ones the caller asked for.
    """

    as_built_id: str
    fiber_segment_id: str
    action: str  # activated | already_active
    from_point_id: str
    to_point_id: str
    fiber_count: int


@dataclass(frozen=True, slots=True)
class AwaitingActivationRow:
    """One accepted as-built whose projected cable is still not on the map."""

    as_built_id: str
    fiber_segment_id: str
    segment_name: str
    project_id: str
    project_label: str
    version: int
    reviewed_at: datetime | None
    fiber_count: int | None
    length_m: float | None
    has_route_geometry: bool


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


def _project_label(db: Session, as_built: AsBuiltRoute) -> str:
    project = db.get(InstallationProject, as_built.project_id)
    native = getattr(project, "project", None)
    return getattr(native, "name", None) or str(as_built.project_id)


def _segment_name(db: Session, as_built: AsBuiltRoute) -> str:
    """``fiber_segments.name`` is unique, so the as-built id fragment is part
    of the name rather than trusting project names to differ."""
    label = _project_label(db, as_built)
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


def _activation_id(value: str | None, *, code: str, subject: str):
    """Coerce an operator-supplied id, refusing malformed input in-domain.

    The ids arrive from a form, so a malformed one is an operator mistake with
    a stable code, not an unhandled ``ValueError`` the adapter turns into a 500.
    """
    try:
        return coerce_uuid(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AsBuiltPlantProjectionError(
            code, f"{subject} is not a valid identifier.", kind="not_found"
        ) from exc


def _resolve_activation_target(
    db: Session,
    *,
    as_built_id: str | None,
    segment_id: str | None,
) -> tuple[AsBuiltRoute, FiberSegment]:
    """Resolve the (accepted as-built, projected segment) pair, or refuse.

    Both directions go through ``AsBuiltRoute.fiber_segment_id``. That backlink
    is what makes this a projection-activation command rather than a general
    ``is_active`` toggle: a segment no accepted as-built points at cannot be
    reached from here at all.
    """
    if bool(as_built_id) == bool(segment_id):
        raise AsBuiltPlantProjectionError(
            "activation_target_required",
            "Name exactly one of as_built_id or segment_id.",
        )

    if as_built_id:
        as_built = db.get(
            AsBuiltRoute,
            _activation_id(
                as_built_id, code="as_built_not_found", subject="The as-built route"
            ),
        )
        if as_built is None:
            raise AsBuiltPlantProjectionError(
                "as_built_not_found", "As-built route not found.", kind="not_found"
            )
    else:
        if segment_id is None:  # Defensive narrowing after the exclusive-target guard.
            raise AsBuiltPlantProjectionError(
                "activation_target_required",
                "Name exactly one of as_built_id or segment_id.",
            )
        as_built = db.scalars(
            select(AsBuiltRoute).where(
                AsBuiltRoute.fiber_segment_id
                == _activation_id(
                    segment_id,
                    code="segment_not_projected",
                    subject="The fiber segment",
                )
            )
        ).first()
        if as_built is None:
            # The segment exists or it does not; either way it is not this
            # owner's row, so this owner does not activate it.
            raise AsBuiltPlantProjectionError(
                "segment_not_projected",
                "That fiber segment is not the projection of an accepted "
                "as-built, so it cannot be activated here.",
                kind="not_found",
            )

    if as_built.status != AsBuiltRouteStatus.accepted.value:
        raise AsBuiltPlantProjectionError(
            "as_built_not_accepted",
            "Only accepted as-built evidence describes plant that can be activated.",
            kind="conflict",
        )
    if as_built.fiber_segment_id is None:
        raise AsBuiltPlantProjectionError(
            "segment_not_projected",
            "This as-built has no projected fiber segment yet.",
            kind="not_found",
        )
    segment = db.get(FiberSegment, as_built.fiber_segment_id)
    if segment is None:
        raise AsBuiltPlantProjectionError(
            "segment_not_projected",
            "The projected fiber segment for this as-built is missing; run the "
            "as-built plant reconciler to rebuild it.",
            kind="not_found",
        )
    return as_built, segment


def _activation_termination_point(
    db: Session, point_id: str, *, role: str
) -> FiberTerminationPoint:
    resolved = (
        _activation_id(
            point_id,
            code="termination_point_not_found",
            subject=f"The {role} termination point",
        )
        if point_id
        else None
    )
    point = db.get(FiberTerminationPoint, resolved) if resolved else None
    if point is None:
        raise AsBuiltPlantProjectionError(
            "termination_point_not_found",
            f"The {role} termination point does not exist.",
            kind="not_found",
        )
    return point


def _activation_fiber_count(
    db: Session,
    as_built: AsBuiltRoute,
    segment: FiberSegment,
    supplied: int | None,
) -> int:
    """Fiber count for the activated cable, from evidence where evidence exists.

    The check constraint would accept any positive integer. Accepting a number
    an operator typed over one the vendor stated on the accepted line items
    would make the plant record disagree with the evidence it was built from,
    so a contradiction is refused rather than silently resolved.
    """
    from_evidence = (
        segment.fiber_count or _plant_attributes(db, as_built)["fiber_count"]
    )
    if from_evidence:
        if supplied is not None and int(supplied) != int(from_evidence):
            raise AsBuiltPlantProjectionError(
                "fiber_count_conflicts_with_evidence",
                f"The accepted as-built states {int(from_evidence)} fibers; "
                f"correct the evidence rather than overriding it here.",
                kind="conflict",
            )
        return int(from_evidence)
    if supplied is None or int(supplied) <= 0:
        raise AsBuiltPlantProjectionError(
            "missing_fiber_count",
            "The accepted as-built carries no fiber count, so activation "
            "requires one to be supplied.",
        )
    return int(supplied)


def activate_projected_segment(
    db: Session,
    *,
    as_built_id: str | None = None,
    segment_id: str | None = None,
    from_point_id: str,
    to_point_id: str,
    actor_id: str | None = None,
    fiber_count: int | None = None,
) -> PlantActivationOutcome:
    """Bind the two terminations an operator names and put the cable in service.

    This command owns its transaction: it commits the activation, its audit
    row, and its domain event together. Activation is one operator decision,
    not a step inside somebody else's — unlike the projection, which stages
    inside the acceptance.

    Every refusal is raised before anything is flushed, so a rejected
    activation leaves the database untouched without the command having to roll
    back a transaction a caller might be sharing.
    """
    from app.services.audit_helpers import log_audit_event
    from app.services.events import emit_event
    from app.services.events.types import EventType
    from app.services.network.fiber_plant_integrity import (
        FiberPlantIntegrityError,
        ensure_segment_strand_inventory,
        validate_active_segment,
    )

    as_built, segment = _resolve_activation_target(
        db, as_built_id=as_built_id, segment_id=segment_id
    )
    if segment.is_active:
        return PlantActivationOutcome(
            as_built_id=str(as_built.id),
            fiber_segment_id=str(segment.id),
            action="already_active",
            from_point_id=str(segment.from_point_id),
            to_point_id=str(segment.to_point_id),
            fiber_count=int(segment.fiber_count or 0),
        )

    from_point = _activation_termination_point(db, from_point_id, role="from")
    to_point = _activation_termination_point(db, to_point_id, role="to")
    if from_point.id == to_point.id:
        raise AsBuiltPlantProjectionError(
            "termination_points_not_distinct",
            "A cable cannot start and end at the same termination point.",
        )
    if segment.route_geom is None:
        # The projection only creates a segment for as-builts that carry
        # geometry, so this is a repair signal, not operator error.
        raise AsBuiltPlantProjectionError(
            "missing_route_geometry",
            "The projected segment has no route geometry; re-run the as-built "
            "plant reconciler before activating it.",
        )
    resolved_fiber_count = _activation_fiber_count(db, as_built, segment, fiber_count)

    segment.from_point_id = from_point.id
    segment.to_point_id = to_point.id
    segment.fiber_count = resolved_fiber_count
    segment.is_active = True

    try:
        # Endpoint identity, PON rootedness, and exact core inventory belong to
        # network.fiber_plant_integrity. Activating from an as-built is held to
        # the same invariants as a reviewed fiber change, so this delegates
        # instead of re-deciding, and fails closed if the cable does not yet
        # reach a serving PON root.
        #
        # The candidate is bound in memory and ruled on before anything is
        # flushed, so a refusal leaves no trace in the database and does not
        # have to roll back a transaction it may be sharing. Both validators
        # read through ``no_autoflush`` and raise before writing anything.
        with db.no_autoflush:
            validate_active_segment(db, segment)
            ensure_segment_strand_inventory(db, segment)
        db.flush()
    except FiberPlantIntegrityError as exc:
        db.expire(segment)
        raise AsBuiltPlantProjectionError(
            "plant_integrity_refused", str(exc), kind="conflict"
        ) from exc

    payload = {
        "as_built_id": str(as_built.id),
        "fiber_segment_id": str(segment.id),
        "segment_name": segment.name,
        "from_point_id": str(from_point.id),
        "to_point_id": str(to_point.id),
        "fiber_count": resolved_fiber_count,
        "source": "vendor_as_built",
    }
    emit_event(db, EventType.fiber_segment_activated, payload, actor=actor_id)
    log_audit_event(
        db=db,
        request=None,
        action="fiber_segment.activated_from_as_built",
        entity_type="fiber_segment",
        entity_id=str(segment.id),
        actor_id=actor_id,
        metadata=payload,
    )
    db.commit()
    return PlantActivationOutcome(
        as_built_id=str(as_built.id),
        fiber_segment_id=str(segment.id),
        action="activated",
        from_point_id=str(from_point.id),
        to_point_id=str(to_point.id),
        fiber_count=resolved_fiber_count,
    )


def _awaiting_activation_query():
    return (
        select(AsBuiltRoute, FiberSegment)
        .join(FiberSegment, FiberSegment.id == AsBuiltRoute.fiber_segment_id)
        .where(AsBuiltRoute.status == AsBuiltRouteStatus.accepted.value)
        .where(FiberSegment.is_active.is_(False))
    )


def awaiting_activation_queue(
    db: Session, *, limit: int = 200
) -> list[AwaitingActivationRow]:
    """Accepted as-builts whose projected cable is still off the map.

    Every plant and map read filters ``is_active``, so an unactivated
    projection is invisible rather than wrong. That makes it exactly the kind
    of work that disappears into tribal knowledge unless it is counted, which
    is what this read exists to prevent.
    """
    rows = db.execute(
        _awaiting_activation_query()
        .order_by(AsBuiltRoute.reviewed_at.asc().nullslast())
        .limit(limit)
    ).all()
    return [
        AwaitingActivationRow(
            as_built_id=str(as_built.id),
            fiber_segment_id=str(segment.id),
            segment_name=segment.name,
            project_id=str(as_built.project_id),
            project_label=_project_label(db, as_built),
            version=int(as_built.version or 0),
            reviewed_at=as_built.reviewed_at,
            fiber_count=segment.fiber_count,
            length_m=segment.length_m,
            has_route_geometry=segment.route_geom is not None,
        )
        for as_built, segment in rows
    ]


def awaiting_activation_count(db: Session) -> int:
    return int(
        db.scalar(_awaiting_activation_query().with_only_columns(func.count())) or 0
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
