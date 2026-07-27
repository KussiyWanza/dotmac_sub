"""Accepted vendor as-built evidence becoming fiber plant.

Vendors drew what they built and staff accepted it, but the accepted geometry
never reached the network record: ``as_built_routes`` was referenced only by
vendor services, so an accepted as-built proved a payment was due and left the
fiber map unchanged. ``AsBuiltRoute.fiber_segment_id`` was a real FK that
nothing ever wrote.

These tests pin the projection, what it refuses to infer, and the repair path
that makes the segment rebuildable from the evidence.
"""

from __future__ import annotations

from uuid import uuid4

from app.models.network import (
    FiberCableType,
    FiberSegment,
    FiberTerminationPoint,
    ODNEndpointType,
)
from app.models.project import Project
from app.models.vendor_routes import (
    AsBuiltLineItem,
    AsBuiltRoute,
    AsBuiltRouteStatus,
    InstallationProject,
)
from app.services.network import as_built_plant_projection as projection

# Geometry columns are stored and returned verbatim in the suite (conftest
# patches GeoAlchemy2 for sqlite), so the value only has to round-trip.
LINESTRING = '{"type": "LineString", "coordinates": [[7.49, 9.06], [7.50, 9.07]]}'


def _accepted(
    db_session,
    *,
    status: str = AsBuiltRouteStatus.accepted.value,
    geom: str | None = LINESTRING,
    fiber_count: int | None = 24,
    cable_type: str | None = "single_mode",
    length: float | None = 1450.0,
) -> AsBuiltRoute:
    project = Project(name=f"Buildout {uuid4().hex[:6]}")
    db_session.add(project)
    db_session.flush()
    installation = InstallationProject(project_id=project.id)
    db_session.add(installation)
    db_session.flush()
    as_built = AsBuiltRoute(
        project_id=installation.id,
        status=status,
        route_geom=geom,
        actual_length_meters=length,
        version=1,
    )
    db_session.add(as_built)
    db_session.flush()
    if fiber_count is not None or cable_type is not None:
        db_session.add(
            AsBuiltLineItem(
                as_built_id=as_built.id,
                description="Trenching and cable",
                cable_type=cable_type,
                fiber_count=fiber_count,
            )
        )
    db_session.commit()
    return as_built


def test_accepted_as_built_becomes_a_fiber_segment(db_session):
    """The whole point: accepted evidence updates the network record instead of
    only proving a payment is due."""
    as_built = _accepted(db_session)

    outcome = projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()

    assert outcome.action == "created"
    db_session.refresh(as_built)
    assert as_built.fiber_segment_id is not None
    segment = db_session.get(FiberSegment, as_built.fiber_segment_id)
    assert segment.fiber_count == 24
    assert segment.cable_type is FiberCableType.single_mode
    assert segment.length_m == 1450.0
    assert segment.route_geom is not None
    # Built, not operational: fiber_segments requires both endpoints bound on
    # an active row, and binding them is a topology decision.
    assert segment.is_active is False


def test_projection_is_idempotent(db_session):
    """A replayed acceptance or a repair sweep must refresh the same segment,
    never mint a second cable for one piece of evidence."""
    as_built = _accepted(db_session)

    first = projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()
    second = projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()

    assert first.action == "created"
    assert second.action == "updated"
    assert first.fiber_segment_id == second.fiber_segment_id
    assert db_session.query(FiberSegment).count() == 1


def test_a_revised_as_built_refreshes_the_same_segment(db_session):
    """A variation re-drawn and re-accepted is the same cable with a corrected
    route, not an additional one."""
    as_built = _accepted(db_session)
    projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()
    segment_id = as_built.fiber_segment_id

    as_built.route_geom = (
        '{"type": "LineString", "coordinates": [[7.49, 9.06], [7.52, 9.09]]}'
    )
    as_built.actual_length_meters = 1610.0
    db_session.commit()
    projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()

    assert as_built.fiber_segment_id == segment_id
    assert db_session.get(FiberSegment, segment_id).length_m == 1610.0


def test_unaccepted_evidence_is_not_plant(db_session):
    """A submitted or rejected as-built is a claim about the network, not a
    record of it."""
    for status in (
        AsBuiltRouteStatus.submitted.value,
        AsBuiltRouteStatus.rejected.value,
        AsBuiltRouteStatus.under_review.value,
    ):
        as_built = _accepted(db_session, status=status)

        outcome = projection.project_accepted_as_built(db_session, str(as_built.id))

        assert outcome.action == "skipped"
        assert outcome.reason == "as_built_not_accepted"
    assert db_session.query(FiberSegment).count() == 0


def test_missing_fiber_count_is_skipped_not_crashed(db_session):
    """``fiber_segments`` requires a positive fiber_count on active operational
    rows, so projecting without one would hit the check constraint. Staff need
    an actionable skip, not a 500."""
    as_built = _accepted(db_session, fiber_count=None)

    outcome = projection.project_accepted_as_built(db_session, str(as_built.id))

    assert outcome.action == "skipped"
    assert outcome.reason == "missing_fiber_count"
    assert db_session.query(FiberSegment).count() == 0


def test_as_built_without_geometry_is_not_a_cable(db_session):
    """Line-item-only evidence for work with nothing to draw is legitimate; it
    simply does not describe a cable."""
    as_built = _accepted(db_session, geom=None)

    outcome = projection.project_accepted_as_built(db_session, str(as_built.id))

    assert outcome.action == "skipped"
    assert outcome.reason == "no_route_geometry"


def test_an_unrecognised_cable_type_is_left_unset(db_session):
    """Vendors describe cable in their own words. A wrong cable type in the
    plant record is worse than a missing one, so guessing is refused."""
    as_built = _accepted(db_session, cable_type="whatever the client supplied")

    projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()

    segment = db_session.get(FiberSegment, as_built.fiber_segment_id)
    assert segment.cable_type is None
    assert segment.fiber_count == 24


def test_cable_type_aliases_are_normalised(db_session):
    for supplied, expected in (
        ("Single-Mode", FiberCableType.single_mode),
        ("ADSS", FiberCableType.aerial),
        ("armoured", FiberCableType.armored),
        ("duct", FiberCableType.underground),
    ):
        as_built = _accepted(db_session, cable_type=supplied)
        projection.project_accepted_as_built(db_session, str(as_built.id))
        db_session.commit()
        segment = db_session.get(FiberSegment, as_built.fiber_segment_id)
        assert segment.cable_type is expected, supplied


def test_the_projection_never_binds_topology_endpoints(db_session):
    """A vendor drawing a line does not decide what it splices into — that is
    ``network.fiber_topology``'s decision."""
    as_built = _accepted(db_session)

    projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()

    segment = db_session.get(FiberSegment, as_built.fiber_segment_id)
    assert segment.from_point_id is None
    assert segment.to_point_id is None


def test_refreshing_does_not_deactivate_cable_topology_activated(db_session):
    """Once topology has connected and activated the cable, an accepted
    variation corrects its route — it must not knock it out of service."""
    as_built = _accepted(db_session)
    projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()
    segment = db_session.get(FiberSegment, as_built.fiber_segment_id)
    # Stand in for the topology decision: bind both endpoints, then activate.
    # The constraint refuses activation without them, which is the schema
    # stating that unconnected cable is not operational plant.
    points = [
        FiberTerminationPoint(name=f"TP-{uuid4().hex[:6]}", endpoint_type=endpoint)
        for endpoint in (ODNEndpointType.olt_port, ODNEndpointType.splitter)
    ]
    db_session.add_all(points)
    db_session.flush()
    segment.from_point_id = points[0].id
    segment.to_point_id = points[1].id
    segment.is_active = True
    db_session.commit()

    as_built.actual_length_meters = 1700.0
    db_session.commit()
    projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()

    db_session.refresh(segment)
    assert segment.is_active is True
    assert segment.length_m == 1700.0


def test_reconciler_rebuilds_a_lost_projection(db_session):
    """The evidence is authoritative and the segment is derived, so a dropped
    event or restored backup must be repairable without re-reviewing anything."""
    as_built = _accepted(db_session)
    # Simulate the projection never having run.
    assert as_built.fiber_segment_id is None

    counts = projection.reconcile_accepted_as_builts(db_session, apply=True)

    db_session.refresh(as_built)
    assert counts["created"] == 1
    assert as_built.fiber_segment_id is not None


def test_reconciler_is_a_no_op_on_a_second_run(db_session):
    as_built = _accepted(db_session)
    projection.reconcile_accepted_as_builts(db_session, apply=True)

    counts = projection.reconcile_accepted_as_builts(db_session, apply=True)

    assert counts["created"] == 0
    assert counts["updated"] == 1
    assert db_session.query(FiberSegment).count() == 1
    db_session.refresh(as_built)


def test_reconciler_dry_run_changes_nothing(db_session):
    _accepted(db_session)

    counts = projection.reconcile_accepted_as_builts(db_session, apply=False)

    assert counts["created"] == 1
    assert db_session.query(FiberSegment).count() == 0
