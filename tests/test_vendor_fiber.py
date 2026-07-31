from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.fiber_change_request import FiberChangeRequest
from app.models.network import (
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    ODNEndpointType,
)
from app.models.project import Project
from app.models.subscriber import Subscriber
from app.models.vendor_routes import InstallationProject, Vendor
from app.models.work_order import WorkOrder
from app.services import fiber_change_requests, vendor_fiber
from app.services.network.fiber_splice_proposals import SpliceProposalError
from app.services.vendor_portal_operations import _verification_evidence_policy


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Vendor",
        last_name="Customer",
        email=f"vendor-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _vendor(db_session, name: str) -> Vendor:
    vendor = Vendor(name=name, code=f"VEN-{uuid4().hex[:8]}")
    db_session.add(vendor)
    db_session.flush()
    return vendor


def _vendor_project(db_session, vendor: Vendor, subscriber: Subscriber):
    project = Project(name=f"OSP build {uuid4().hex[:6]}")
    db_session.add(project)
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
    )
    db_session.add(installation)
    db_session.flush()
    work_order = WorkOrder(
        crm_work_order_id=f"wo-vendor-{uuid4().hex[:6]}",
        subscriber_id=subscriber.id,
        title="Vendor splicing scope",
        status="in_progress",
        project_id=project.id,
        requires_as_built_evidence=False,
        scheduled_start=datetime.now(UTC),
    )
    db_session.add(work_order)
    db_session.flush()
    return project, installation, work_order


def _plant(db_session):
    closure = FiberSpliceClosure(name="Vendor closure", is_active=True)
    access_point = FiberAccessPoint(name="Vendor FAP A", is_active=True)
    downstream_access_point = FiberAccessPoint(name="Vendor FAP B", is_active=True)
    db_session.add_all([closure, access_point, downstream_access_point])
    db_session.flush()
    tray = FiberSpliceTray(closure_id=closure.id, tray_number=1)
    upstream_point = FiberTerminationPoint(
        name="Vendor cable A upstream",
        endpoint_type=ODNEndpointType.fiber_access_point,
        ref_id=access_point.id,
        is_active=True,
    )
    closure_point = FiberTerminationPoint(
        name="Vendor closure endpoint",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
        is_active=True,
    )
    downstream_point = FiberTerminationPoint(
        name="Vendor cable B downstream",
        endpoint_type=ODNEndpointType.fiber_access_point,
        ref_id=downstream_access_point.id,
        is_active=True,
    )
    db_session.add_all([tray, upstream_point, closure_point, downstream_point])
    db_session.flush()
    segment_a = FiberSegment(
        name=f"Vendor cable A {uuid4().hex[:8]}",
        from_point_id=upstream_point.id,
        to_point_id=closure_point.id,
        route_geom="LINESTRING(7.40 9.00, 7.41 9.01)",
        fiber_count=1,
        is_active=True,
    )
    segment_b = FiberSegment(
        name=f"Vendor cable B {uuid4().hex[:8]}",
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
    return closure, tray, strand_a, strand_b


def test_vendor_propose_splice_scoped_to_assigned_project(db_session):
    subscriber = _subscriber(db_session)
    vendor = _vendor(db_session, "SpliceWorks Ltd")
    other_vendor = _vendor(db_session, "OtherCo Ltd")
    _project, _installation, work_order = _vendor_project(
        db_session, vendor, subscriber
    )
    closure, tray, strand_a, strand_b = _plant(db_session)
    db_session.commit()

    receipt = vendor_fiber.propose_splice(
        db_session,
        vendor_id=str(vendor.id),
        vendor_user_id=str(uuid4()),
        work_order_id=work_order.public_id,
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        from_strand_end="b",
        to_strand_id=str(strand_b.id),
        to_strand_end="a",
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
        loss_db=0.15,
    )

    assert receipt.status.value == "pending"
    assert receipt.work_order_public_id == work_order.public_id
    change = db_session.get(FiberChangeRequest, receipt.change_request_id)
    assert change is not None
    assert change.requested_by_vendor_id == vendor.id
    assert change.payload["vendor_actor"]["vendor_id"] == str(vendor.id)
    assert change.payload["work_order_id"] == str(work_order.id)

    mine = vendor_fiber.list_splice_proposals(db_session, vendor_id=str(vendor.id))
    assert [row.change_request_id for row in mine] == [change.id]
    assert mine[0].work_order_public_id == work_order.public_id

    theirs = vendor_fiber.list_splice_proposals(
        db_session, vendor_id=str(other_vendor.id)
    )
    assert theirs == []


def test_vendor_propose_splice_rejects_unassigned_work_order(db_session):
    subscriber = _subscriber(db_session)
    vendor = _vendor(db_session, "AssignedCo")
    intruder = _vendor(db_session, "UnassignedCo")
    _project, _installation, work_order = _vendor_project(
        db_session, vendor, subscriber
    )
    closure, _tray, strand_a, strand_b = _plant(db_session)
    db_session.commit()

    with pytest.raises(SpliceProposalError) as exc:
        vendor_fiber.propose_splice(
            db_session,
            vendor_id=str(intruder.id),
            vendor_user_id=None,
            work_order_id=work_order.public_id,
            closure_id=str(closure.id),
            from_strand_id=str(strand_a.id),
            from_strand_end="b",
            to_strand_id=str(strand_b.id),
            to_strand_end="a",
            splice_type="fusion",
        )

    assert exc.value.kind == "not_found"


def test_verification_blocked_until_vendor_splices_reviewed(db_session):
    subscriber = _subscriber(db_session)
    vendor = _vendor(db_session, "ReviewGate Ltd")
    _project, installation, work_order = _vendor_project(db_session, vendor, subscriber)
    closure, tray, strand_a, strand_b = _plant(db_session)
    reviewer = _subscriber(db_session)
    db_session.commit()

    policy = _verification_evidence_policy(db_session, installation)
    assert policy["eligible"] is True
    assert policy["pending_splice_change_requests"] == 0

    receipt = vendor_fiber.propose_splice(
        db_session,
        vendor_id=str(vendor.id),
        vendor_user_id=None,
        work_order_id=work_order.public_id,
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        from_strand_end="b",
        to_strand_id=str(strand_b.id),
        to_strand_end="a",
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
    )

    policy = _verification_evidence_policy(db_session, installation)
    assert policy["eligible"] is False
    assert policy["pending_splice_change_requests"] == 1
    assert "splice" in str(policy["reason"]).lower()

    fiber_change_requests.approve_request(
        db_session,
        str(receipt.change_request_id),
        reviewer_person_id=str(reviewer.id),
        review_notes="Vendor splice verified against closure schedule",
    )

    policy = _verification_evidence_policy(db_session, installation)
    assert policy["eligible"] is True
    assert policy["pending_splice_change_requests"] == 0
