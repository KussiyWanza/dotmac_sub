from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.dispatch import TechnicianProfile
from app.models.fiber_change_request import FiberChangeRequest
from app.models.network import (
    FdhCabinet,
    FiberSegment,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    ODNEndpointType,
    OntAssignment,
    OntUnit,
    Splitter,
    SplitterPort,
    SplitterPortType,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import FieldJobEvidenceRead
from app.services import fiber_change_requests
from app.services.field import fiber as field_fiber
from app.services.network.fiber_inventory_proposals import (
    FiberInventoryProposalError,
)


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Journey",
        last_name="Tech",
        display_name="Journey Tech",
        email=f"journey-tech-{uuid4().hex[:8]}@example.com",
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
        crm_person_id="crm-journey-tech",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Journey",
        last_name="Customer",
        email=f"journey-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=f"wo-journey-{uuid4().hex[:6]}",
        subscriber_id=subscriber.id,
        title="Journey job",
        status="in_progress",
        assigned_to_crm_person_id="crm-journey-tech",
        scheduled_start=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _segment_with_strands(db_session, *, fibers_per_tube=2, fiber_count=4):
    start = FiberTerminationPoint(
        name=f"Journey start {uuid4().hex[:6]}",
        endpoint_type=ODNEndpointType.other,
        is_active=True,
    )
    end = FiberTerminationPoint(
        name=f"Journey end {uuid4().hex[:6]}",
        endpoint_type=ODNEndpointType.other,
        is_active=True,
    )
    db_session.add_all([start, end])
    db_session.flush()
    segment = FiberSegment(
        name=f"Journey cable {uuid4().hex[:8]}",
        from_point_id=start.id,
        to_point_id=end.id,
        route_geom="LINESTRING(7.40 9.00, 7.41 9.01)",
        fiber_count=fiber_count,
        fibers_per_tube=fibers_per_tube,
        color_standard="eia_tia_598",
        is_active=True,
    )
    db_session.add(segment)
    db_session.flush()
    strands = []
    for number in range(1, fiber_count + 1):
        strand = FiberStrand(
            cable_name=segment.name,
            segment_id=segment.id,
            strand_number=number,
            status=FiberStrandStatus.available,
            is_active=True,
        )
        db_session.add(strand)
        strands.append(strand)
    db_session.flush()
    return segment, strands


def test_field_cable_registration_reviewed_and_applied_inactive(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    reviewer = _subscriber(db_session)
    db_session.commit()

    name = f"Field-built cable {uuid4().hex[:8]}"
    receipt = field_fiber.register_cable(
        db_session,
        _auth(user),
        name=name,
        fiber_count=24,
        cable_type="aerial",
        fibers_per_tube=12,
        color_standard="eia_tia_598",
        length_m=850.0,
        notes="New aerial span hung along Journey street",
        work_order_id=work_order.public_id,
    )

    assert receipt.status.value == "pending"
    assert receipt.work_order_public_id == work_order.public_id
    change = db_session.get(FiberChangeRequest, receipt.change_request_id)
    assert change.payload["is_active"] is False
    assert change.payload["provenance"]["kind"] == "field_technician"
    assert change.payload["provenance"]["work_order_id"] == str(work_order.id)

    # Duplicate name fails closed.
    with pytest.raises(HTTPException) as exc:
        field_fiber.register_cable(
            db_session,
            _auth(user),
            name=name,
            fiber_count=24,
        )
    assert exc.value.status_code in {409, 422}

    applied = fiber_change_requests.approve_request(
        db_session,
        str(change.id),
        reviewer_person_id=str(reviewer.id),
        review_notes="Cable registration reviewed against the build record",
    )
    assert applied.status.value == "applied"
    segment = db_session.query(FiberSegment).filter(FiberSegment.name == name).one()
    assert segment.is_active is False
    assert segment.fibers_per_tube == 12
    assert segment.color_standard == "eia_tia_598"


def test_field_strand_damage_single_and_tube_scope(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    reviewer = _subscriber(db_session)
    segment, strands = _segment_with_strands(db_session)
    db_session.commit()

    single = field_fiber.report_strand_damage(
        db_session,
        _auth(user),
        note="Core crushed at pole 14",
        strand_id=str(strands[0].id),
        work_order_id=work_order.public_id,
    )
    assert single.strand_ids == (strands[0].id,)
    request = db_session.get(FiberChangeRequest, single.change_request_ids[0])
    assert request.payload["status"] == "damaged"
    assert request.payload["provenance"]["work_order_public_id"] == (
        work_order.public_id
    )

    applied = fiber_change_requests.approve_request(
        db_session,
        str(request.id),
        reviewer_person_id=str(reviewer.id),
        review_notes="Damage confirmed from field photos",
    )
    assert applied.status.value == "applied"
    db_session.refresh(strands[0])
    assert strands[0].status == FiberStrandStatus.damaged

    # Tube 2 covers strands 3 and 4.
    tube = field_fiber.report_strand_damage(
        db_session,
        _auth(user),
        note="Tube pinched in closure",
        segment_id=str(segment.id),
        tube_number=2,
    )
    assert set(tube.strand_ids) == {strands[2].id, strands[3].id}
    assert len(tube.change_request_ids) == 2

    # Undeclared construction refuses tube scope.
    bare_segment, _ = _segment_with_strands(db_session, fibers_per_tube=None)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        field_fiber.report_strand_damage(
            db_session,
            _auth(user),
            note="Tube guess",
            segment_id=str(bare_segment.id),
            tube_number=1,
        )
    assert exc.value.status_code == 422


def test_field_ont_attachment_proposal_scoped_to_job_customer(db_session, olt_device):
    from app.models.network import PonPort

    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    stranger = _subscriber(db_session)

    olt_device.is_active = True
    fdh = FdhCabinet(name="Journey FDH", code=f"FDH-{uuid4().hex[:6]}", is_active=True)
    db_session.add(fdh)
    db_session.flush()
    splitter = Splitter(
        name="Journey splitter", fdh_id=fdh.id, splitter_ratio="1:8", is_active=True
    )
    pon = PonPort(olt_id=olt_device.id, name="0/1/1", is_active=True)
    db_session.add_all([splitter, pon])
    db_session.flush()
    from app.models.network import PonPortSplitterLink

    input_port = SplitterPort(
        splitter_id=splitter.id,
        port_number=0,
        port_type=SplitterPortType.input,
        is_active=True,
    )
    output = SplitterPort(
        splitter_id=splitter.id,
        port_number=1,
        port_type=SplitterPortType.output,
        is_active=True,
    )
    ont = OntUnit(
        serial_number=f"ONT-J-{uuid4().hex[:6]}",
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    db_session.add_all([input_port, output, ont])
    db_session.flush()
    db_session.add(
        PonPortSplitterLink(
            pon_port_id=pon.id,
            splitter_port_id=input_port.id,
            active=True,
        )
    )
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    db_session.commit()

    receipt = field_fiber.propose_ont_attachment(
        db_session,
        _auth(user),
        crm_work_order_id=work_order.public_id,
        ont_unit_id=str(ont.id),
        splitter_port_id=str(output.id),
        note="Customer drop landed on port 1",
    )
    assert receipt.status == "proposed"
    assert receipt.splitter_port_id == output.id

    # An ONT assigned to another customer is out of job scope.
    other_ont = OntUnit(serial_number=f"ONT-X-{uuid4().hex[:6]}", is_active=True)
    db_session.add(other_ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=other_ont.id,
            subscriber_id=stranger.id,
            active=True,
        )
    )
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        field_fiber.propose_ont_attachment(
            db_session,
            _auth(user),
            crm_work_order_id=work_order.public_id,
            ont_unit_id=str(other_ont.id),
            splitter_port_id=str(output.id),
        )
    assert exc.value.status_code == 404


def test_job_evidence_summary_counts_and_gate(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    segment, strands = _segment_with_strands(db_session)
    db_session.commit()

    # One failing-derived test asserted as pass (a conflict), one clean pass.
    field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id=work_order.public_id,
        asset_type="fiber_strand",
        asset_id=str(strands[0].id),
        test_type="insertion_loss",
        value_db=0.6,
        unit="dB",
        passed=True,
    )
    field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id=work_order.public_id,
        asset_type="fiber_strand",
        asset_id=str(strands[0].id),
        test_type="insertion_loss",
        value_db=0.1,
        unit="dB",
        passed=True,
    )
    field_fiber.report_strand_damage(
        db_session,
        _auth(user),
        note="Found broken core while testing",
        strand_id=str(strands[1].id),
        work_order_id=work_order.public_id,
    )

    payload = field_fiber.get_job_evidence(
        db_session, _auth(user), crm_work_order_id=work_order.public_id
    )

    assert payload["fiber_test_count"] == 2
    assert payload["derived_failed_count"] == 1
    assert payload["assertion_conflict_count"] == 1
    assert payload["splice_proposals"] == {
        "pending": 0,
        "applied": 0,
        "rejected": 0,
    }
    assert payload["pending_inventory_proposals"] == 1
    assert payload["plan"] is None
    assert payload["as_built_required"] is False
    assert payload["as_built_satisfied"] is True
    FieldJobEvidenceRead(**payload)


def test_inventory_intake_requires_exclusive_scope(db_session):
    segment, strands = _segment_with_strands(db_session)
    db_session.commit()
    from app.services.network import fiber_inventory_proposals
    from app.services.network.fiber_splice_proposals import FieldTechnicianActor

    actor = FieldTechnicianActor(
        technician_id=uuid4(), person_id=uuid4(), system_user_id=None
    )
    with pytest.raises(FiberInventoryProposalError) as exc:
        fiber_inventory_proposals.report_strand_damage(
            db_session,
            actor=actor,
            note="both scopes",
            strand_id=str(strands[0].id),
            segment_id=str(segment.id),
            tube_number=1,
        )
    assert exc.value.kind == "invalid"
