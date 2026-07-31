from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.dispatch import TechnicianProfile
from app.models.network import (
    FiberAccessPoint,
    FiberSegment,
    FiberTerminationPoint,
    ODNEndpointType,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import FieldFiberTestRead
from app.services.fiber_topology import (
    FiberSubscriptionTrace,
    FiberTraceHop,
)
from app.services.field import fiber as field_fiber
from app.services.network import fiber_test_acceptance
from app.services.network.fiber_test_acceptance import (
    ACCEPTANCE_POLICY_VERSION,
    FiberTestVerdict,
    derive_link_budget,
    derive_verdict,
)


def test_verdict_matrix_per_test_type():
    ok = derive_verdict(test_type="insertion_loss", value_db=0.2)
    assert ok.verdict is FiberTestVerdict.within_threshold
    assert ok.passed is True
    assert ok.threshold is not None and ok.threshold.maximum == 0.30

    high = derive_verdict(test_type="insertion_loss", value_db=0.5)
    assert high.verdict is FiberTestVerdict.exceeds_threshold
    assert high.passed is False

    rx_ok = derive_verdict(test_type="optical_power", value_db=-20.0)
    assert rx_ok.passed is True
    rx_low = derive_verdict(test_type="optical_power", value_db=-30.5)
    assert rx_low.verdict is FiberTestVerdict.exceeds_threshold
    rx_hot = derive_verdict(test_type="optical_power", value_db=-5.0)
    assert rx_hot.verdict is FiberTestVerdict.exceeds_threshold

    reflect_ok = derive_verdict(test_type="reflectance", value_db=-50.0)
    assert reflect_ok.passed is True
    reflect_bad = derive_verdict(test_type="reflectance", value_db=-20.0)
    assert reflect_bad.passed is False

    no_value = derive_verdict(test_type="otdr", value_db=None)
    assert no_value.verdict is FiberTestVerdict.no_measurement
    assert no_value.passed is None

    continuity = derive_verdict(test_type="continuity", value_db=1.0)
    assert continuity.verdict is FiberTestVerdict.no_policy
    assert continuity.passed is None
    assert continuity.threshold is None


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Accept",
        last_name="Tech",
        display_name="Accept Tech",
        email=f"accept-tech-{uuid4().hex[:8]}@example.com",
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


def test_record_test_snapshots_derived_verdict_and_flags_conflict(db_session):
    user = _user(db_session)
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-accept-tech",
    )
    subscriber = Subscriber(
        first_name="Accept",
        last_name="Customer",
        email=f"accept-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([profile, subscriber])
    db_session.flush()
    work_order = WorkOrder(
        crm_work_order_id="wo-acceptance",
        subscriber_id=subscriber.id,
        title="Acceptance testing",
        status="in_progress",
        assigned_to_crm_person_id="crm-accept-tech",
        scheduled_start=datetime.now(UTC),
    )
    access_point = FiberAccessPoint(name="Acceptance FAP", is_active=True)
    db_session.add_all([work_order, access_point])
    db_session.commit()

    # Technician asserts pass, but the measured splice loss exceeds policy.
    result = field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id="wo-acceptance",
        asset_type="fiber_access_point",
        asset_id=str(access_point.id),
        test_type="insertion_loss",
        value_db=0.6,
        unit="dB",
        passed=True,
        instrument="OTDR",
    )

    assert result.derived_passed is False
    assert result.derived_verdict == FiberTestVerdict.exceeds_threshold.value
    assert result.applied_maximum_db == 0.30
    assert result.acceptance_policy_version == ACCEPTANCE_POLICY_VERSION
    assert result.passed is True  # the technician's assertion is untouched
    assert result.assertion_conflict is True

    read = FieldFiberTestRead.model_validate(result)
    assert read.assertion_conflict is True
    assert read.derived_verdict == "exceeds_threshold"

    # A no-policy type derives nothing and cannot conflict.
    continuity = field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id="wo-acceptance",
        asset_type="fiber_access_point",
        asset_id=str(access_point.id),
        test_type="continuity",
        passed=True,
    )
    assert continuity.derived_passed is None
    assert continuity.derived_verdict == FiberTestVerdict.no_policy.value
    assert continuity.assertion_conflict is False


def _trace(hops, *, physical_complete=True) -> FiberSubscriptionTrace:
    return FiberSubscriptionTrace(
        subscription_id=uuid4(),
        customer_label="Acceptance Customer",
        subscription_status="active",
        hops=tuple(hops),
        gaps=(),
        electronic_complete=True,
        physical_complete=physical_complete,
        upstream_scope="pop_boundary_only",
        upstream_message="test",
    )


def _segment(db_session, name: str, length_m: float) -> FiberSegment:
    start = FiberTerminationPoint(
        name=f"{name} start",
        endpoint_type=ODNEndpointType.other,
        is_active=True,
    )
    end = FiberTerminationPoint(
        name=f"{name} end",
        endpoint_type=ODNEndpointType.other,
        is_active=True,
    )
    db_session.add_all([start, end])
    db_session.flush()
    segment = FiberSegment(
        name=name,
        from_point_id=start.id,
        to_point_id=end.id,
        route_geom="LINESTRING(7.40 9.00, 7.41 9.01)",
        fiber_count=12,
        length_m=length_m,
        is_active=True,
    )
    db_session.add(segment)
    db_session.flush()
    return segment


def test_link_budget_derives_components_margin_and_assumptions(db_session):
    feeder = _segment(db_session, f"Budget feeder {uuid4().hex[:6]}", 2000.0)
    drop = _segment(db_session, f"Budget drop {uuid4().hex[:6]}", 500.0)
    db_session.commit()

    hops = [
        FiberTraceHop(
            kind="feeder_segment",
            label=feeder.name,
            asset_id=feeder.id,
            evidence="test",
        ),
        FiberTraceHop(
            kind="splitter_output",
            label="1:8 output",
            asset_id=uuid4(),
            evidence="test",
            cumulative_splitter_loss_db="10.5",
        ),
        FiberTraceHop(
            kind="drop_segment",
            label=drop.name,
            asset_id=drop.id,
            evidence="test",
        ),
    ]
    budget = derive_link_budget(db_session, _trace(hops), measured_rx_dbm=-19.0)

    assert budget is not None
    assert budget.complete is True
    names = {item.name: item.loss_db for item in budget.components}
    assert names["splitter_cumulative_loss"] == 10.5
    # 2.5 km at 0.35 dB/km
    assert names["fiber_attenuation"] == 0.88
    assert budget.expected_loss_db == 11.38
    assert budget.assumed_tx_dbm == fiber_test_acceptance.ASSUMED_OLT_TX_DBM
    # margin = rx - (tx - expected) = -19.0 - (2.0 - 11.38)
    assert budget.margin_db == -9.62
    assert any("0.35 dB/km" in item for item in budget.assumptions)
    assert any("launch power" in item for item in budget.assumptions)
    assert any("splice" in item for item in budget.assumptions)


def test_link_budget_refuses_without_loss_evidence(db_session):
    hops = [FiberTraceHop(kind="olt", label="OLT", asset_id=uuid4(), evidence="test")]
    assert derive_link_budget(db_session, _trace(hops)) is None


def test_link_budget_labels_incomplete_traces(db_session):
    segment = _segment(db_session, f"Budget partial {uuid4().hex[:6]}", 1000.0)
    db_session.commit()
    hops = [
        FiberTraceHop(
            kind="feeder_segment",
            label=segment.name,
            asset_id=segment.id,
            evidence="test",
        )
    ]
    budget = derive_link_budget(db_session, _trace(hops, physical_complete=False))
    assert budget is not None
    assert budget.complete is False
    assert budget.margin_db is None
