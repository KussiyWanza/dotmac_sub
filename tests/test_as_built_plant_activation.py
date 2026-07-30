"""Putting the cable an accepted as-built proved was built into service.

``project_accepted_as_built`` correctly refuses to invent endpoints, so it
creates its segment ``is_active=False``. Nothing then ever set ``is_active``,
and every fiber map and plant read filters on it — so an accepted as-built
updated the database and stayed invisible to every operator, forever.

These tests pin the command that closes that gap: what it activates, what it
refuses to activate, and that the row actually reaches the reads that were
hiding it.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.models.network import (
    FiberSegment,
    FiberSpliceClosure,
    FiberStrand,
    FiberTerminationPoint,
    ODNEndpointType,
    OLTDevice,
    PonPort,
)
from app.models.project import Project
from app.models.vendor_routes import (
    AsBuiltLineItem,
    AsBuiltRoute,
    AsBuiltRouteStatus,
    InstallationProject,
)
from app.services import fiber_plant_api
from app.services.network import as_built_plant_projection as projection

LINESTRING = '{"type": "LineString", "coordinates": [[7.49, 9.06], [7.50, 9.07]]}'


def _accepted(
    db_session,
    *,
    status: str = AsBuiltRouteStatus.accepted.value,
    geom: str | None = LINESTRING,
    fiber_count: int | None = 24,
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
        actual_length_meters=1450.0,
        version=1,
    )
    db_session.add(as_built)
    db_session.flush()
    if fiber_count is not None:
        db_session.add(
            AsBuiltLineItem(
                as_built_id=as_built.id,
                description="Trenching and cable",
                cable_type="single_mode",
                fiber_count=fiber_count,
            )
        )
    db_session.commit()
    return as_built


def _projected(db_session, **kwargs) -> AsBuiltRoute:
    as_built = _accepted(db_session, **kwargs)
    projection.project_accepted_as_built(db_session, str(as_built.id))
    db_session.commit()
    return as_built


def _rooted_terminations(db_session) -> tuple[str, str]:
    """A PON-port termination and a splice-closure termination.

    ``network.fiber_plant_integrity`` requires an active cable's component to
    resolve to an exact serving PON/OLT root and every termination to name real
    active infrastructure, so activation tests have to build plant that is
    genuinely connectable rather than two bare rows.
    """
    olt = OLTDevice(name=f"OLT-{uuid4().hex[:6]}", is_active=True)
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name=f"pon-{uuid4().hex[:6]}", is_active=True)
    closure = FiberSpliceClosure(name=f"CL-{uuid4().hex[:6]}", is_active=True)
    db_session.add_all([pon, closure])
    db_session.flush()
    upstream = FiberTerminationPoint(
        name=f"PON {pon.name}",
        endpoint_type=ODNEndpointType.pon_port,
        ref_id=pon.id,
        is_active=True,
    )
    downstream = FiberTerminationPoint(
        name=f"Closure {closure.name}",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
        is_active=True,
    )
    db_session.add_all([upstream, downstream])
    db_session.commit()
    return str(upstream.id), str(downstream.id)


def test_activation_binds_endpoints_and_puts_the_cable_in_service(db_session):
    """The whole point: the accepted as-built stops being a database row nobody
    can see."""
    as_built = _projected(db_session)
    from_id, to_id = _rooted_terminations(db_session)

    outcome = projection.activate_projected_segment(
        db_session,
        as_built_id=str(as_built.id),
        from_point_id=from_id,
        to_point_id=to_id,
        actor_id="staff-1",
    )

    assert outcome.action == "activated"
    segment = db_session.get(FiberSegment, as_built.fiber_segment_id)
    assert segment.is_active is True
    assert str(segment.from_point_id) == from_id
    assert str(segment.to_point_id) == to_id
    assert segment.fiber_count == 24


def test_the_activated_cable_reaches_the_is_active_filtered_plant_reads(db_session):
    """Every plant and map read filters ``is_active``. Before activation the
    accepted as-built is absent from all of them; after it, it is counted."""
    as_built = _projected(db_session)
    from_id, to_id = _rooted_terminations(db_session)

    assert fiber_plant_api.get_fiber_plant_stats(db_session)["fiber_segments"] == 0

    projection.activate_projected_segment(
        db_session,
        as_built_id=str(as_built.id),
        from_point_id=from_id,
        to_point_id=to_id,
    )

    assert fiber_plant_api.get_fiber_plant_stats(db_session)["fiber_segments"] == 1
    assert (
        db_session.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).one().id
        == as_built.fiber_segment_id
    )


def test_activation_materialises_the_exact_core_inventory(db_session):
    """Activation goes through ``network.fiber_plant_integrity`` rather than
    only satisfying the check constraint, so the cable arrives sized."""
    as_built = _projected(db_session)
    from_id, to_id = _rooted_terminations(db_session)

    projection.activate_projected_segment(
        db_session,
        as_built_id=str(as_built.id),
        from_point_id=from_id,
        to_point_id=to_id,
    )

    strands = (
        db_session.query(FiberStrand)
        .filter(FiberStrand.segment_id == as_built.fiber_segment_id)
        .all()
    )
    assert len(strands) == 24


def test_a_segment_no_accepted_as_built_projected_cannot_be_activated(db_session):
    """This is not a general-purpose activate-anything switch: a segment some
    other owner created is unreachable from here."""
    from_id, to_id = _rooted_terminations(db_session)
    foreign = FiberSegment(
        name=f"SEG-{uuid4().hex[:6]}",
        route_geom=LINESTRING,
        fiber_count=12,
        is_active=False,
    )
    db_session.add(foreign)
    db_session.commit()

    with pytest.raises(projection.AsBuiltPlantProjectionError) as exc:
        projection.activate_projected_segment(
            db_session,
            segment_id=str(foreign.id),
            from_point_id=from_id,
            to_point_id=to_id,
        )

    assert exc.value.code == "segment_not_projected"
    assert db_session.get(FiberSegment, foreign.id).is_active is False


def test_evidence_that_was_never_accepted_cannot_be_activated(db_session):
    as_built = _accepted(db_session, status=AsBuiltRouteStatus.submitted.value)
    from_id, to_id = _rooted_terminations(db_session)

    with pytest.raises(projection.AsBuiltPlantProjectionError) as exc:
        projection.activate_projected_segment(
            db_session,
            as_built_id=str(as_built.id),
            from_point_id=from_id,
            to_point_id=to_id,
        )

    assert exc.value.code == "as_built_not_accepted"


def test_a_missing_termination_point_is_refused(db_session):
    as_built = _projected(db_session)
    from_id, _to_id = _rooted_terminations(db_session)

    with pytest.raises(projection.AsBuiltPlantProjectionError) as exc:
        projection.activate_projected_segment(
            db_session,
            as_built_id=str(as_built.id),
            from_point_id=from_id,
            to_point_id=str(uuid4()),
        )

    assert exc.value.code == "termination_point_not_found"
    assert db_session.get(FiberSegment, as_built.fiber_segment_id).is_active is False


def test_a_malformed_termination_id_is_a_domain_refusal_not_a_crash(db_session):
    """The ids come off a form, so garbage must produce a stable code the
    adapter can map, not an unhandled ValueError."""
    as_built = _projected(db_session)
    from_id, _to_id = _rooted_terminations(db_session)

    with pytest.raises(projection.AsBuiltPlantProjectionError) as exc:
        projection.activate_projected_segment(
            db_session,
            as_built_id=str(as_built.id),
            from_point_id=from_id,
            to_point_id="not-a-uuid",
        )

    assert exc.value.code == "termination_point_not_found"


def test_a_cable_cannot_start_and_end_at_the_same_point(db_session):
    as_built = _projected(db_session)
    from_id, _to_id = _rooted_terminations(db_session)

    with pytest.raises(projection.AsBuiltPlantProjectionError) as exc:
        projection.activate_projected_segment(
            db_session,
            as_built_id=str(as_built.id),
            from_point_id=from_id,
            to_point_id=from_id,
        )

    assert exc.value.code == "termination_points_not_distinct"
    assert db_session.get(FiberSegment, as_built.fiber_segment_id).is_active is False


def test_a_replayed_activation_returns_the_row_unchanged(db_session):
    """A double-submitted form or a retried task must not re-bind the endpoints
    a previous activation already recorded."""
    as_built = _projected(db_session)
    from_id, to_id = _rooted_terminations(db_session)
    other_from, other_to = _rooted_terminations(db_session)
    projection.activate_projected_segment(
        db_session,
        as_built_id=str(as_built.id),
        from_point_id=from_id,
        to_point_id=to_id,
    )

    replay = projection.activate_projected_segment(
        db_session,
        as_built_id=str(as_built.id),
        from_point_id=other_from,
        to_point_id=other_to,
    )

    assert replay.action == "already_active"
    assert replay.from_point_id == from_id
    assert replay.to_point_id == to_id
    segment = db_session.get(FiberSegment, as_built.fiber_segment_id)
    assert str(segment.from_point_id) == from_id
    assert str(segment.to_point_id) == to_id


def test_an_operator_may_not_contradict_the_accepted_fiber_count(db_session):
    """The plant record must not disagree with the evidence it was built from."""
    as_built = _projected(db_session)
    from_id, to_id = _rooted_terminations(db_session)

    with pytest.raises(projection.AsBuiltPlantProjectionError) as exc:
        projection.activate_projected_segment(
            db_session,
            as_built_id=str(as_built.id),
            from_point_id=from_id,
            to_point_id=to_id,
            fiber_count=48,
        )

    assert exc.value.code == "fiber_count_conflicts_with_evidence"


def test_activation_fails_closed_when_the_cable_reaches_no_pon_root(db_session):
    """Activation delegates to ``network.fiber_plant_integrity`` instead of
    satisfying only the check constraint, so unrooted cable stays out."""
    as_built = _projected(db_session)
    closures = [
        FiberSpliceClosure(name=f"CL-{uuid4().hex[:6]}", is_active=True)
        for _ in range(2)
    ]
    db_session.add_all(closures)
    db_session.flush()
    stranded = [
        FiberTerminationPoint(
            name=f"Closure {closure.name}",
            endpoint_type=ODNEndpointType.splice_closure,
            ref_id=closure.id,
            is_active=True,
        )
        for closure in closures
    ]
    db_session.add_all(stranded)
    db_session.commit()

    with pytest.raises(projection.AsBuiltPlantProjectionError) as exc:
        projection.activate_projected_segment(
            db_session,
            as_built_id=str(as_built.id),
            from_point_id=str(stranded[0].id),
            to_point_id=str(stranded[1].id),
        )

    assert exc.value.code == "plant_integrity_refused"
    assert db_session.get(FiberSegment, as_built.fiber_segment_id).is_active is False


def test_the_queue_lists_accepted_evidence_whose_cable_is_still_invisible(db_session):
    as_built = _projected(db_session)

    rows = projection.awaiting_activation_queue(db_session)

    assert projection.awaiting_activation_count(db_session) == 1
    assert [row.as_built_id for row in rows] == [str(as_built.id)]
    assert rows[0].fiber_segment_id == str(as_built.fiber_segment_id)
    assert rows[0].fiber_count == 24
    assert rows[0].has_route_geometry is True


def test_the_queue_drops_the_row_once_the_cable_is_in_service(db_session):
    as_built = _projected(db_session)
    from_id, to_id = _rooted_terminations(db_session)

    projection.activate_projected_segment(
        db_session,
        as_built_id=str(as_built.id),
        from_point_id=from_id,
        to_point_id=to_id,
    )

    assert projection.awaiting_activation_queue(db_session) == []
    assert projection.awaiting_activation_count(db_session) == 0


def test_the_queue_ignores_evidence_that_was_never_accepted(db_session):
    _accepted(db_session, status=AsBuiltRouteStatus.submitted.value)

    assert projection.awaiting_activation_count(db_session) == 0


def test_the_admin_adapter_is_thin_and_gated_by_the_existing_plant_permission():
    """Activation is plant authority, so it reuses ``network:fiber:write``
    rather than minting a permission or borrowing the vendor-operations
    inventory/finance ones. The adapter parses a form and calls the owner; it
    never touches ``is_active`` itself."""
    from app.web.admin import network_fiber_plant

    routes = {route.path: route for route in network_fiber_plant.router.routes}
    activate = routes["/network/fiber-as-built-activation/{as_built_id}/activate"]
    queue = routes["/network/fiber-as-built-activation"]
    source = Path("app/web/admin/network_fiber_plant.py").read_text(encoding="utf-8")

    assert activate.methods == {"POST"}
    assert queue.methods == {"GET"}
    guards = {
        dependency.call.__name__ for dependency in activate.dependant.dependencies
    }
    assert "_require_permission" in guards
    assert 'require_permission("network:fiber:write")' in source
    assert "activate_projected_segment(" in source
    assert "is_active" not in source


def test_the_queue_template_offers_no_bare_activation_toggle():
    template = Path("templates/admin/network/fiber/as_built_activation.html")

    content = template.read_text(encoding="utf-8")
    assert "awaiting_activation_count" in content
    assert 'name="from_point_id"' in content
    assert 'name="to_point_id"' in content
    assert 'include "components/forms/csrf_input.html"' in content
    assert 'name="is_active"' not in content
