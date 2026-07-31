from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.models.dispatch import TechnicianProfile
from app.models.fiber_change_request import FiberChangeRequest
from app.models.fiber_splice_plan import FiberSplicePlanStatus
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
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import FieldSplicePlanResponse
from app.services import fiber_change_requests
from app.services.db_session_adapter import db_session_adapter
from app.services.field import fiber as field_fiber
from app.services.field.transitions import field_transitions
from app.services.network import fiber_splice_plans
from app.services.network.fiber_splice_plans import SplicePlanError
from app.services.owner_commands import CommandContext


def _ctx(reason: str = "pytest splice plan command") -> CommandContext:
    return CommandContext.system(
        actor="pytest", scope="network:fiber:write", reason=reason
    )


def _cmd(db, fn, **kwargs):
    db_session_adapter.release_read_transaction(db)
    return fn(db, context=_ctx(), **kwargs)


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Plan",
        last_name="Tech",
        display_name="Plan Tech",
        email=f"plan-tech-{uuid4().hex[:8]}@example.com",
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
        crm_person_id="crm-plan-tech",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Plan",
        last_name="Customer",
        email=f"plan-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop(
            "crm_work_order_id", f"wo-plan-{uuid4().hex[:6]}"
        ),
        subscriber_id=subscriber.id,
        title="Planned splicing",
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id="crm-plan-tech",
        scheduled_start=datetime.now(UTC),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _plant(db_session, cores_per_cable: int = 2):
    closure = FiberSpliceClosure(name="Plan closure", is_active=True)
    access_point = FiberAccessPoint(name="Plan FAP A", is_active=True)
    downstream_access_point = FiberAccessPoint(name="Plan FAP B", is_active=True)
    db_session.add_all([closure, access_point, downstream_access_point])
    db_session.flush()
    tray = FiberSpliceTray(closure_id=closure.id, tray_number=1)
    upstream_point = FiberTerminationPoint(
        name="Plan cable A upstream",
        endpoint_type=ODNEndpointType.fiber_access_point,
        ref_id=access_point.id,
        is_active=True,
    )
    closure_point = FiberTerminationPoint(
        name="Plan closure endpoint",
        endpoint_type=ODNEndpointType.splice_closure,
        ref_id=closure.id,
        is_active=True,
    )
    downstream_point = FiberTerminationPoint(
        name="Plan cable B downstream",
        endpoint_type=ODNEndpointType.fiber_access_point,
        ref_id=downstream_access_point.id,
        is_active=True,
    )
    db_session.add_all([tray, upstream_point, closure_point, downstream_point])
    db_session.flush()
    segment_a = FiberSegment(
        name=f"Plan cable A {uuid4().hex[:8]}",
        from_point_id=upstream_point.id,
        to_point_id=closure_point.id,
        route_geom="LINESTRING(7.40 9.00, 7.41 9.01)",
        fiber_count=cores_per_cable,
        is_active=True,
    )
    segment_b = FiberSegment(
        name=f"Plan cable B {uuid4().hex[:8]}",
        from_point_id=closure_point.id,
        to_point_id=downstream_point.id,
        route_geom="LINESTRING(7.41 9.01, 7.42 9.02)",
        fiber_count=cores_per_cable,
        is_active=True,
    )
    db_session.add_all([segment_a, segment_b])
    db_session.flush()
    a_strands = []
    b_strands = []
    for number in range(1, cores_per_cable + 1):
        strand_a = FiberStrand(
            cable_name=segment_a.name,
            segment_id=segment_a.id,
            strand_number=number,
            status=FiberStrandStatus.available,
            is_active=True,
        )
        strand_b = FiberStrand(
            cable_name=segment_b.name,
            segment_id=segment_b.id,
            strand_number=number,
            status=FiberStrandStatus.available,
            is_active=True,
        )
        db_session.add_all([strand_a, strand_b])
        a_strands.append(strand_a)
        b_strands.append(strand_b)
    db_session.flush()
    return closure, tray, a_strands, b_strands


def _issued_two_item_plan(db_session, work_order, closure, tray, a_strands, b_strands):
    plan = _cmd(
        db_session,
        fiber_splice_plans.create_plan,
        work_order_id=work_order.public_id,
        name="Closure A cut sheet",
    )
    items = []
    for index in range(2):
        items.append(
            _cmd(
                db_session,
                fiber_splice_plans.add_item,
                plan_id=str(plan.id),
                closure_id=str(closure.id),
                from_strand_id=str(a_strands[index].id),
                from_strand_end="b",
                to_strand_id=str(b_strands[index].id),
                to_strand_end="a",
                splice_type="fusion",
                tray_id=str(tray.id),
                tray_position=index + 1,
                expected_loss_db=0.1,
            )
        )
    _cmd(db_session, fiber_splice_plans.issue_plan, plan_id=str(plan.id))
    return plan, items


def test_plan_lifecycle_validations(db_session):
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    closure, tray, a_strands, b_strands = _plant(db_session)
    db_session.commit()

    plan = _cmd(
        db_session,
        fiber_splice_plans.create_plan,
        work_order_id=work_order.public_id,
        name="Cut sheet 1",
    )
    assert plan.status == FiberSplicePlanStatus.draft.value

    with pytest.raises(SplicePlanError) as exc:
        _cmd(
            db_session,
            fiber_splice_plans.create_plan,
            work_order_id=work_order.public_id,
            name="Second live plan",
        )
    assert exc.value.kind == "conflict"

    with pytest.raises(SplicePlanError) as exc:
        _cmd(db_session, fiber_splice_plans.issue_plan, plan_id=str(plan.id))
    assert "empty" in exc.value.message

    item = _cmd(
        db_session,
        fiber_splice_plans.add_item,
        plan_id=str(plan.id),
        closure_id=str(closure.id),
        from_strand_id=str(a_strands[0].id),
        from_strand_end="b",
        to_strand_id=str(b_strands[0].id),
        to_strand_end="a",
        splice_type="fusion",
    )
    assert item.position_index == 1

    with pytest.raises(SplicePlanError) as exc:
        _cmd(
            db_session,
            fiber_splice_plans.add_item,
            plan_id=str(plan.id),
            closure_id=str(closure.id),
            from_strand_id=str(b_strands[0].id),
            from_strand_end="a",
            to_strand_id=str(a_strands[0].id),
            to_strand_end="b",
            splice_type="fusion",
        )
    assert exc.value.kind == "conflict"

    a_strands[1].status = FiberStrandStatus.in_use
    db_session.commit()
    with pytest.raises(SplicePlanError) as exc:
        _cmd(
            db_session,
            fiber_splice_plans.add_item,
            plan_id=str(plan.id),
            closure_id=str(closure.id),
            from_strand_id=str(a_strands[1].id),
            from_strand_end="b",
            to_strand_id=str(b_strands[1].id),
            to_strand_end="a",
            splice_type="fusion",
        )
    assert exc.value.kind == "invalid"

    issued = _cmd(db_session, fiber_splice_plans.issue_plan, plan_id=str(plan.id))
    assert issued.status == FiberSplicePlanStatus.issued.value
    with pytest.raises(SplicePlanError):
        _cmd(
            db_session,
            fiber_splice_plans.add_item,
            plan_id=str(plan.id),
            closure_id=str(closure.id),
            from_strand_id=str(b_strands[1].id),
            from_strand_end="b",
            to_strand_id=str(a_strands[0].id),
            to_strand_end="a",
            splice_type="fusion",
        )

    cancelled = _cmd(db_session, fiber_splice_plans.cancel_plan, plan_id=str(plan.id))
    assert cancelled.status == FiberSplicePlanStatus.cancelled.value
    replacement = _cmd(
        db_session,
        fiber_splice_plans.create_plan,
        work_order_id=work_order.public_id,
        name="Cut sheet 2",
    )
    assert replacement.status == FiberSplicePlanStatus.draft.value


def test_execute_plan_items_explicit_and_auto_match(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    closure, tray, a_strands, b_strands = _plant(db_session)
    plan, items = _issued_two_item_plan(
        db_session, work_order, closure, tray, a_strands, b_strands
    )
    db_session.commit()

    receipt = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(a_strands[0].id),
        from_strand_end="b",
        to_strand_id=str(b_strands[0].id),
        to_strand_end="a",
        splice_type="fusion",
        work_order_id=work_order.public_id,
        plan_item_id=str(items[0].id),
    )
    assert receipt.plan_id == plan.id
    assert receipt.plan_item_id == items[0].id
    change = db_session.get(FiberChangeRequest, receipt.change_request_id)
    assert change.payload["plan_item_id"] == str(items[0].id)
    db_session.refresh(items[0])
    assert items[0].executed_change_request_id == change.id

    auto = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(a_strands[1].id),
        from_strand_end="b",
        to_strand_id=str(b_strands[1].id),
        to_strand_end="a",
        splice_type="fusion",
        work_order_id=work_order.public_id,
    )
    assert auto.plan_item_id == items[1].id

    diff = fiber_splice_plans.diff_for_work_order(db_session, work_order.id)
    assert diff is not None
    assert set(diff.executed_items) == {items[0].id, items[1].id}
    assert diff.unexecuted_items == ()
    assert diff.unplanned_change_requests == ()
    assert set(diff.pending_review_items) == {items[0].id, items[1].id}
    assert diff.complete is True

    reviewer = _subscriber(db_session)
    fiber_change_requests.reject_request(
        db_session,
        str(change.id),
        reviewer_person_id=str(reviewer.id),
        review_notes="Wrong tray per closure photo",
    )
    diff = fiber_splice_plans.diff_for_work_order(db_session, work_order.id)
    assert diff.unexecuted_items == (items[0].id,)
    assert diff.complete is False


def test_plan_item_mismatch_fails_closed(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    closure, tray, a_strands, b_strands = _plant(db_session)
    _plan, items = _issued_two_item_plan(
        db_session, work_order, closure, tray, a_strands, b_strands
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_fiber.propose_splice(
            db_session,
            _auth(user),
            closure_id=str(closure.id),
            from_strand_id=str(a_strands[1].id),
            from_strand_end="b",
            to_strand_id=str(b_strands[1].id),
            to_strand_end="a",
            splice_type="fusion",
            work_order_id=work_order.public_id,
            plan_item_id=str(items[0].id),
        )
    assert exc.value.status_code == 422
    assert "cut sheet" in exc.value.detail


def test_unplanned_work_order_proposal_appears_in_diff(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    closure, tray, a_strands, b_strands = _plant(db_session, cores_per_cable=3)
    _plan, items = _issued_two_item_plan(
        db_session, work_order, closure, tray, a_strands, b_strands
    )
    db_session.commit()

    unplanned = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(a_strands[2].id),
        from_strand_end="b",
        to_strand_id=str(b_strands[2].id),
        to_strand_end="a",
        splice_type="fusion",
        work_order_id=work_order.public_id,
    )
    assert unplanned.plan_item_id is None

    diff = fiber_splice_plans.diff_for_work_order(db_session, work_order.id)
    assert diff.unplanned_change_requests == (unplanned.change_request_id,)
    assert set(diff.unexecuted_items) == {items[0].id, items[1].id}
    assert diff.complete is False


def test_completion_gate_requires_plan_execution(db_session, monkeypatch):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    closure, tray, a_strands, b_strands = _plant(db_session)
    _plan, items = _issued_two_item_plan(
        db_session, work_order, closure, tray, a_strands, b_strands
    )
    db_session.commit()

    from app.services.field import transitions as transitions_module

    monkeypatch.setattr(
        transitions_module,
        "resolve_completion_requirements",
        lambda db: transitions_module.FieldCompletionRequirements(
            evidence_required=True,
            minimum_photo_count=0,
            customer_signoff_required=False,
            signature_unavailable_reason_allowed=True,
        ),
    )

    with pytest.raises(HTTPException) as exc:
        field_transitions.apply(
            db_session,
            _auth(user),
            work_order.public_id,
            event="complete",
            client_event_id=uuid4(),
        )
    assert exc.value.status_code == 422
    assert "splice plan" in exc.value.detail

    for index in range(2):
        field_fiber.propose_splice(
            db_session,
            _auth(user),
            closure_id=str(closure.id),
            from_strand_id=str(a_strands[index].id),
            from_strand_end="b",
            to_strand_id=str(b_strands[index].id),
            to_strand_end="a",
            splice_type="fusion",
            work_order_id=work_order.public_id,
            plan_item_id=str(items[index].id),
        )

    completed = field_transitions.apply(
        db_session,
        _auth(user),
        work_order.public_id,
        event="complete",
        client_event_id=uuid4(),
    )
    assert completed["job"].status == "completed"


def test_field_splice_plan_read_is_scoped_and_typed(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber)
    closure, tray, a_strands, b_strands = _plant(db_session)
    plan, _items = _issued_two_item_plan(
        db_session, work_order, closure, tray, a_strands, b_strands
    )
    db_session.commit()

    result = field_fiber.get_splice_plan(
        db_session, _auth(user), crm_work_order_id=work_order.public_id
    )
    assert result["plan"] is not None
    assert result["plan"]["plan_id"] == plan.id
    assert result["plan"]["unexecuted_count"] == 2
    assert result["diff"] is not None
    FieldSplicePlanResponse(**result)

    outsider = _user(db_session)
    outsider_profile = TechnicianProfile(
        person_id=outsider.id,
        system_user_id=outsider.id,
        crm_person_id="crm-not-on-this-job",
    )
    db_session.add(outsider_profile)
    db_session.flush()
    with pytest.raises(HTTPException) as exc:
        field_fiber.get_splice_plan(
            db_session, _auth(outsider), crm_work_order_id=work_order.public_id
        )
    assert exc.value.status_code == 404


def test_admin_plan_routes_are_permission_guarded():
    from app.api import domains_network_fiber

    plan_routes = [
        route
        for route in domains_network_fiber.router.routes
        if isinstance(route, APIRoute) and "/fiber-splice-plans" in route.path
    ]
    assert len(plan_routes) >= 7
    for route in plan_routes:
        captured = []
        for dependency in route.dependant.dependencies:
            for cell in getattr(dependency.call, "__closure__", None) or ():
                captured.append(str(cell.cell_contents))
        assert any("network:fiber:" in value for value in captured), route.path
