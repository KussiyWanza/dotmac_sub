from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.dispatch import TechnicianProfile
from app.models.fiber_change_request import FiberChangeRequest
from app.models.fiber_physical import FiberCoreSplice, FiberPhysicalLinkDecision
from app.models.field_fiber import FieldFiberTestResult
from app.models.network import (
    FiberAccessPoint,
    FiberSegment,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    ODNEndpointType,
    OntAssignment,
    OntSignalObservation,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import (
    FieldFiberCustomerTraceRead,
    FieldSpliceProposalStatusRead,
)
from app.services import fiber_change_requests
from app.services.field import fiber as field_fiber


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Fiber",
        last_name="Tech",
        display_name="Fiber Tech",
        email=f"fiber-tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _profile(db_session, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-fiber-tech",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Fiber",
        last_name="Customer",
        email=f"fiber-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id="wo-fiber",
        subscriber_id=subscriber.id,
        title="Fiber repair",
        status="in_progress",
        assigned_to_crm_person_id="crm-fiber-tech",
        scheduled_start=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _plant(db_session):
    closure = FiberSpliceClosure(name="Closure A", is_active=True)
    access_point = FiberAccessPoint(name="FAP A", is_active=True)
    downstream_access_point = FiberAccessPoint(name="FAP B", is_active=True)
    db_session.add_all([closure, access_point, downstream_access_point])
    db_session.flush()
    tray = FiberSpliceTray(closure_id=closure.id, tray_number=1)
    upstream_point = FiberTerminationPoint(
        name="Cable A upstream",
        endpoint_type=ODNEndpointType.fiber_access_point,
        ref_id=access_point.id,
        is_active=True,
    )
    closure_point = FiberTerminationPoint(
        name="Exact closure endpoint",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
        is_active=True,
    )
    downstream_point = FiberTerminationPoint(
        name="Cable B downstream",
        endpoint_type=ODNEndpointType.fiber_access_point,
        ref_id=downstream_access_point.id,
        is_active=True,
    )
    db_session.add_all([tray, upstream_point, closure_point, downstream_point])
    db_session.flush()
    segment_a = FiberSegment(
        name=f"Cable A {uuid4().hex[:8]}",
        from_point_id=upstream_point.id,
        to_point_id=closure_point.id,
        route_geom="LINESTRING(7.40 9.00, 7.41 9.01)",
        fiber_count=1,
        is_active=True,
    )
    segment_b = FiberSegment(
        name=f"Cable B {uuid4().hex[:8]}",
        from_point_id=closure_point.id,
        to_point_id=downstream_point.id,
        route_geom="LINESTRING(7.41 9.01, 7.42 9.02)",
        fiber_count=1,
        is_active=True,
    )
    db_session.add_all([segment_a, segment_b])
    db_session.flush()
    strand_a = FiberStrand(
        cable_name=segment_a.name,
        segment_id=segment_a.id,
        strand_number=1,
        status=FiberStrandStatus.available,
        is_active=True,
    )
    strand_b = FiberStrand(
        cable_name=segment_b.name,
        segment_id=segment_b.id,
        strand_number=1,
        status=FiberStrandStatus.reserved,
        is_active=True,
    )
    db_session.add_all([strand_a, strand_b])
    db_session.flush()
    return closure, tray, strand_a, strand_b, access_point


def test_propose_splice_creates_pending_change_request(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    reviewer = _subscriber(db_session)
    closure, tray, strand_a, strand_b, _access_point = _plant(db_session)
    db_session.commit()

    result = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        from_strand_end="b",
        to_strand_id=str(strand_b.id),
        to_strand_end="a",
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
        loss_db=0.12,
        note="Field captured splice",
    )

    assert result.status.value == "pending"
    assert result.replayed is False
    change = db_session.query(FiberChangeRequest).one()
    assert change.asset_type == "fiber_splice"
    assert change.payload["field_actor"]["system_user_id"] == str(user.id)
    assert change.payload["loss_db"] == 0.12
    decision = db_session.get(FiberPhysicalLinkDecision, change.asset_id)
    assert decision is not None
    assert decision.status == "proposed"

    replay = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_b.id),
        from_strand_end="a",
        to_strand_id=str(strand_a.id),
        to_strand_end="b",
        splice_type="fusion",
    )
    assert replay.change_request_id == change.id
    assert replay.replayed is True

    applied = fiber_change_requests.approve_request(
        db_session,
        str(change.id),
        reviewer_person_id=str(reviewer.id),
        review_notes="Exact cable ends and closure tray independently reviewed",
    )
    assert applied.status.value == "applied"
    db_session.refresh(decision)
    assert decision.status == "applied"
    canonical = db_session.query(FiberCoreSplice).one()
    assert {
        (canonical.first_strand_id, canonical.first_strand_end),
        (canonical.second_strand_id, canonical.second_strand_end),
    } == {(strand_a.id, "b"), (strand_b.id, "a")}
    assert canonical.splice_type == "fusion"
    assert db_session.query(FiberSplice).count() == 0


def test_propose_splice_rejects_in_use_strand(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    closure, _tray, strand_a, strand_b, _access_point = _plant(db_session)
    strand_b.status = FiberStrandStatus.in_use
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_fiber.propose_splice(
            db_session,
            _auth(user),
            closure_id=str(closure.id),
            from_strand_id=str(strand_a.id),
            from_strand_end="b",
            to_strand_id=str(strand_b.id),
            to_strand_end="a",
            splice_type="fusion",
        )

    assert exc.value.status_code == 422


def test_rejecting_field_splice_declines_the_exact_physical_decision(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    reviewer = _subscriber(db_session)
    closure, tray, strand_a, strand_b, _access_point = _plant(db_session)
    db_session.commit()

    proposal = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        from_strand_end="b",
        to_strand_id=str(strand_b.id),
        to_strand_end="a",
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
        note="Field splice requiring review",
    )
    request = db_session.get(FiberChangeRequest, proposal.change_request_id)
    assert request is not None
    decision = db_session.get(FiberPhysicalLinkDecision, request.asset_id)
    assert decision is not None

    rejected = fiber_change_requests.reject_request(
        db_session,
        str(request.id),
        reviewer_person_id=str(reviewer.id),
        review_notes="Exact cable ends do not match the closure tray schedule",
    )

    assert rejected.status.value == "rejected"
    db_session.refresh(decision)
    assert decision.status == "declined"
    assert decision.closed_reason == "physical_link_decision_declined"
    assert db_session.query(FiberCoreSplice).count() == 0


def test_record_fiber_test_is_scoped_and_idempotent(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber)
    _closure, _tray, _strand_a, _strand_b, access_point = _plant(db_session)
    client_ref = uuid4()
    db_session.commit()

    result = field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id="wo-fiber",
        asset_type="fiber_access_point",
        asset_id=str(access_point.id),
        test_type="optical_power",
        wavelength_nm=1490,
        value_db=-20.5,
        unit="dBm",
        passed=True,
        instrument="Power meter",
        client_ref=str(client_ref),
    )

    replay = field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id="wo-fiber",
        asset_type="fiber_access_point",
        asset_id=str(access_point.id),
        test_type="optical_power",
        client_ref=str(client_ref),
    )
    assert replay.id == result.id
    assert db_session.query(FieldFiberTestResult).count() == 1

    rows = field_fiber.list_tests(db_session, _auth(user), crm_work_order_id="wo-fiber")
    assert [row.id for row in rows] == [result.id]
    assert rows[0].value_db == -20.5


def test_customer_trace_projects_job_customer_fiber(
    db_session, subscriber, subscription
):
    user = _user(db_session)
    _profile(db_session, user)
    _work_order(db_session, subscriber)
    db_session.commit()

    traces = field_fiber.list_customer_traces(
        db_session, _auth(user), crm_work_order_id="wo-fiber"
    )

    assert len(traces) == 1
    item = traces[0]
    assert item.trace.subscription_id == subscription.id
    assert item.trace.customer_trace_complete is False
    assert any(gap.code == "exact_ont_assignment_missing" for gap in item.trace.gaps)
    assert item.ont_live is None
    FieldFiberCustomerTraceRead(**item.to_dict())


def test_customer_trace_requires_a_scoped_work_order(
    db_session, subscriber, subscription
):
    user = _user(db_session)
    profile = _profile(db_session, user)
    profile.crm_person_id = "crm-other-tech"
    _work_order(db_session, subscriber)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_fiber.list_customer_traces(
            db_session, _auth(user), crm_work_order_id="wo-fiber"
        )

    assert exc.value.status_code == 404


def test_customer_trace_returns_latest_ont_signal(
    db_session, subscriber, subscription, olt_device
):
    user = _user(db_session)
    _profile(db_session, user)
    _work_order(db_session, subscriber)
    pon = PonPort(olt_id=olt_device.id, name="0/1/0", is_active=True)
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-FIELD-1",
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
        olt_status=OnuOnlineStatus.online,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            active=True,
        )
    )
    db_session.add_all(
        [
            OntSignalObservation(
                ont_unit_id=ont.id,
                olt_status=OnuOnlineStatus.online,
                rx_signal_dbm=-23.4,
                observed_at=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
            ),
            OntSignalObservation(
                ont_unit_id=ont.id,
                olt_status=OnuOnlineStatus.online,
                rx_signal_dbm=-19.2,
                observed_at=datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
            ),
        ]
    )
    db_session.commit()

    traces = field_fiber.list_customer_traces(
        db_session, _auth(user), crm_work_order_id="wo-fiber"
    )

    assert len(traces) == 1
    live = traces[0].ont_live
    assert live is not None
    assert live.serial_number == "ONT-FIELD-1"
    assert live.olt_status == "online"
    assert live.rx_signal_dbm == -19.2
    FieldFiberCustomerTraceRead(**traces[0].to_dict())


def test_customer_trace_annotates_segment_odf_terminations(
    db_session, subscriber, subscription, olt_device, network_device
):
    from app.models.fiber_physical import FiberPatchPanel, FiberRack
    from app.services.network.fiber_physical_continuity import (
        approve_physical_link,
        execute_physical_link,
        propose_physical_link,
    )
    from app.services.network.fiber_plant_integrity import (
        ensure_segment_strand_inventory,
    )
    from tests.test_fiber_physical_continuity import _connector
    from tests.test_fiber_subscription_trace import _complete_path

    user = _user(db_session)
    _profile(db_session, user)
    _work_order(db_session, subscriber)
    assets = _complete_path(
        db_session, subscription, subscriber, olt_device, network_device
    )
    feeder = assets["feeder"]
    feeder.color_standard = "eia_tia_598"
    ensure_segment_strand_inventory(db_session, feeder)
    feeder_core = (
        db_session.query(FiberStrand)
        .filter(FiberStrand.segment_id == feeder.id, FiberStrand.strand_number == 1)
        .one()
    )

    rack = FiberRack(
        code=f"RACK-{uuid4().hex[:10]}",
        name="Serving POP fiber rack",
        pop_site_id=network_device.pop_site_id,
        rack_units=42,
        is_active=True,
    )
    db_session.add(rack)
    db_session.flush()
    odf = FiberPatchPanel(
        rack_id=rack.id,
        name="OLT ODF 01",
        panel_type="odf",
        rack_unit_start=1,
        rack_unit_height=1,
        port_capacity=24,
        connector_type="sc",
        polish_type="apc",
        fiber_mode="single_mode",
        is_active=True,
    )
    db_session.add(odf)
    db_session.flush()
    odf_port = _connector(
        patch_panel_id=odf.id,
        port_number=1,
        label="ODF-01/01",
    )
    db_session.add(odf_port)
    db_session.commit()

    decision = propose_physical_link(
        db_session,
        "strand_termination",
        "connect",
        proposed_by="planner@example.com",
        reason="Exact installed optical continuity independently verified",
        first_strand_id=feeder_core.id,
        first_strand_end="a",
        connector_port_id=odf_port.id,
    )
    approve_physical_link(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Connector, strand end, location, and labels checked",
    )
    applied = execute_physical_link(
        db_session, decision.id, executed_by="executor@example.com"
    )
    assert applied.status == "applied"
    db_session.commit()

    traces = field_fiber.list_customer_traces(
        db_session, _auth(user), crm_work_order_id="wo-fiber"
    )

    assert len(traces) == 1
    item = traces[0]
    details = {detail.segment_id: detail for detail in item.physical_details}
    feeder_detail = details[feeder.id]
    assert feeder_detail.termination_count == 1
    termination = feeder_detail.terminations[0]
    assert termination.strand_number == 1
    assert termination.strand_end == "a"
    assert termination.port_label == "ODF-01/01"
    assert termination.panel_name == "OLT ODF 01"
    assert termination.rack_name == "Serving POP fiber rack"
    assert termination.colors is not None
    assert termination.colors.core_color == "blue"
    drop_detail = details[assets["drop"].id]
    assert drop_detail.termination_count == 0
    FieldFiberCustomerTraceRead(**item.to_dict())


def test_splice_proposal_listing_is_scoped_to_the_technician(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    reviewer = _subscriber(db_session)
    closure, tray, strand_a, strand_b, _access_point = _plant(db_session)
    db_session.commit()

    proposal = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        from_strand_end="b",
        to_strand_id=str(strand_b.id),
        to_strand_end="a",
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
        loss_db=0.12,
    )

    change = db_session.get(FiberChangeRequest, proposal.change_request_id)
    assert change is not None

    rows = field_fiber.list_splice_proposals(db_session, _auth(user))
    assert [row.change_request_id for row in rows] == [change.id]
    assert rows[0].status.value == "pending"
    assert rows[0].closure_id == closure.id
    FieldSpliceProposalStatusRead(**rows[0].to_dict())

    other_user = _user(db_session)
    other_profile = TechnicianProfile(
        person_id=other_user.id,
        system_user_id=other_user.id,
        crm_person_id="crm-other-tech",
    )
    db_session.add(other_profile)
    db_session.flush()
    assert field_fiber.list_splice_proposals(db_session, _auth(other_user)) == []

    fiber_change_requests.reject_request(
        db_session,
        str(change.id),
        reviewer_person_id=str(reviewer.id),
        review_notes="Closure tray schedule mismatch",
    )

    rows = field_fiber.list_splice_proposals(db_session, _auth(user))
    assert rows[0].status.value == "rejected"
    assert rows[0].review_notes == "Closure tray schedule mismatch"


def test_propose_splice_links_scoped_work_order(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    closure, tray, strand_a, strand_b, _access_point = _plant(db_session)
    db_session.commit()

    receipt = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        from_strand_end="b",
        to_strand_id=str(strand_b.id),
        to_strand_end="a",
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
        work_order_id="wo-fiber",
    )

    assert receipt.work_order_public_id == "wo-fiber"
    change = db_session.get(FiberChangeRequest, receipt.change_request_id)
    assert change is not None
    assert change.payload["work_order_id"] == str(work_order.id)
    assert change.payload["work_order_public_id"] == work_order.public_id

    rows = field_fiber.list_splice_proposals(db_session, _auth(user))
    assert rows[0].work_order_public_id == "wo-fiber"


def test_propose_splice_records_derived_strand_colors(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    closure, tray, strand_a, strand_b, _access_point = _plant(db_session)
    strand_a.segment.fibers_per_tube = 1
    strand_a.segment.color_standard = "eia_tia_598"
    strand_b.segment.color_standard = "eia_tia_598"
    db_session.commit()

    receipt = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        from_strand_end="b",
        to_strand_id=str(strand_b.id),
        to_strand_end="a",
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
    )

    assert receipt.from_strand_colors is not None
    assert receipt.from_strand_colors.tube_number == 1
    assert receipt.from_strand_colors.tube_color == "blue"
    assert receipt.from_strand_colors.core_color == "blue"
    assert receipt.to_strand_colors is not None
    assert receipt.to_strand_colors.tube_number is None
    assert receipt.to_strand_colors.core_color == "blue"

    change = db_session.get(FiberChangeRequest, receipt.change_request_id)
    assert change.payload["from_strand_colors"]["core_color"] == "blue"

    rows = field_fiber.list_splice_proposals(db_session, _auth(user))
    assert rows[0].from_strand_colors == receipt.from_strand_colors
    FieldSpliceProposalStatusRead(**rows[0].to_dict())


def test_propose_splice_rejects_out_of_scope_work_order(db_session):
    user = _user(db_session)
    profile = _profile(db_session, user)
    profile.crm_person_id = "crm-not-assigned"
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber)
    closure, _tray, strand_a, strand_b, _access_point = _plant(db_session)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_fiber.propose_splice(
            db_session,
            _auth(user),
            closure_id=str(closure.id),
            from_strand_id=str(strand_a.id),
            from_strand_end="b",
            to_strand_id=str(strand_b.id),
            to_strand_end="a",
            splice_type="fusion",
            work_order_id="wo-fiber",
        )

    assert exc.value.status_code == 404
