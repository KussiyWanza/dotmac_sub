from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.dispatch import TechnicianProfile
from app.models.fiber_topology_field_observation import (
    FiberTopologyFieldObservation,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.network.fiber_topology_field_observations import (
    record_fiber_field_observation,
)
from app.services.network.fiber_topology_field_worklist import (
    reconcile_fiber_field_worklist,
)
from app.web.admin import network_fiber_plant


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage(
    db_session,
    external_id: str,
    *,
    asset_type: str = "fiber_access_point",
    profile: str | None = None,
    created_at: datetime | None = None,
) -> FiberTopologyStagedFeature:
    observed_at = created_at or datetime.now(UTC)
    is_path = asset_type == "fiber_segment"
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=profile or f"pytest_worklist_{uuid.uuid4().hex}",
        source_name="pytest-field-worklist.kmz",
        asset_type=asset_type,
        external_id_key="spanid" if is_path else "assetid",
        file_sha256=_sha(),
        manifest_sha256=_sha(),
        status="staged",
        feature_count=1,
        blocker_count=0,
        candidate_count=0,
        unchanged_count=0,
        new_count=1,
        source_metadata={"test": True},
        created_by="pytest-stager",
        created_at=observed_at,
    )
    feature = FiberTopologyStagedFeature(
        batch=batch,
        row_number=1,
        asset_type=asset_type,
        external_id=external_id,
        display_name=external_id,
        geometry_type="LineString" if is_path else "Point",
        geometry_geojson=(
            {
                "type": "LineString",
                "coordinates": [[7.40, 9.00], [7.42, 9.02]],
            }
            if is_path
            else {"type": "Point", "coordinates": [7.40, 9.00]}
        ),
        source_properties={"test": True},
        content_sha256=_sha(),
        geometry_sha256=_sha(),
        match_status="new",
        blocker_codes=[],
        match_reasons=[],
        candidate_asset_ids=[],
        created_at=observed_at,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def _actor_and_job(db_session):
    user = SystemUser(
        first_name="Worklist",
        last_name="Verifier",
        display_name="Worklist Verifier",
        email=f"worklist-verifier-{uuid.uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    subscriber = Subscriber(
        first_name="Fiber",
        last_name="Customer",
        email=f"worklist-customer-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([user, subscriber])
    db_session.flush()
    technician = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-worklist-verifier",
    )
    work_order = WorkOrder(
        public_id=f"WO-WORKLIST-{uuid.uuid4().hex[:8]}",
        crm_work_order_id=f"crm-wo-{uuid.uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        title="Verify staged fiber evidence",
        status="in_progress",
        assigned_to_crm_person_id="crm-worklist-verifier",
        scheduled_start=datetime.now(UTC),
    )
    db_session.add_all([technician, work_order])
    db_session.commit()
    return technician, work_order


def _record(
    db_session,
    feature: FiberTopologyStagedFeature,
    technician: TechnicianProfile,
    work_order: WorkOrder,
    *,
    outcome: str = "agrees",
    label: str | None = "Exact field label",
) -> FiberTopologyFieldObservation:
    return record_fiber_field_observation(
        db_session,
        staged_feature_id=feature.id,
        expected_feature_content_sha256=feature.content_sha256,
        work_order_id=work_order.id,
        recorded_by_technician_id=technician.id,
        recorded_by_person_id=technician.person_id,
        recorded_by_system_user_id=technician.system_user_id,
        verification_scope="identity",
        outcome=outcome,
        observed_at=datetime.now(UTC),
        client_ref=uuid.uuid4(),
        observed_external_label=label,
    )


def test_empty_worklist_is_stable_exhaustive_and_read_only(db_session):
    before = db_session.query(FiberTopologyFieldObservation).count()

    report = reconcile_fiber_field_worklist(db_session)
    replay = reconcile_fiber_field_worklist(db_session)

    assert report.staged_feature_count == 0
    assert report.needs_follow_up_count == 0
    assert report.current_agreement_count == 0
    assert report.rows == ()
    assert replay.report_sha256 == report.report_sha256
    assert db_session.query(FiberTopologyFieldObservation).count() == before
    assert "ready" not in report.to_dict()


def test_worklist_keeps_complete_cohort_and_orders_every_evidence_state(db_session):
    observed_at = datetime.now(UTC)
    drift = _stage(db_session, "FAT-WORKLIST-DRIFT")
    conflicting = _stage(db_session, "FAT-WORKLIST-CONTRADICTORY")
    conflict = _stage(db_session, "FAT-WORKLIST-CONFLICT")
    superseded = _stage(
        db_session,
        "FAT-WORKLIST-SUPERSEDED",
        profile="worklist_old",
        created_at=observed_at,
    )
    unobserved = _stage(
        db_session,
        "SPAN-WORKLIST-UNOBSERVED",
        asset_type="fiber_segment",
    )
    inconclusive = _stage(db_session, "FAT-WORKLIST-INCONCLUSIVE")
    agreement = _stage(db_session, "FAT-WORKLIST-AGREEMENT")
    technician, work_order = _actor_and_job(db_session)

    drift_observation = _record(db_session, drift, technician, work_order)
    drift_observation.notes = "tampered after immutable digest"
    db_session.commit()
    _record(db_session, conflicting, technician, work_order)
    _record(
        db_session,
        conflicting,
        technician,
        work_order,
        outcome="conflicts",
        label="Different field label",
    )
    _record(
        db_session,
        conflict,
        technician,
        work_order,
        outcome="conflicts",
    )
    _record(db_session, superseded, technician, work_order)
    latest_superseding = _stage(
        db_session,
        "FAT-WORKLIST-SUPERSEDED",
        profile="worklist_new",
        created_at=observed_at + timedelta(seconds=1),
    )
    _record(
        db_session,
        inconclusive,
        technician,
        work_order,
        outcome="inaccessible",
        label=None,
    )
    _record(db_session, agreement, technician, work_order)

    report = reconcile_fiber_field_worklist(db_session)
    rows = {row["external_id"]: row for row in report.rows}

    assert report.staged_feature_count == 7
    assert [row["verification_state"] for row in report.rows] == [
        "evidence_drift",
        "conflicting_observations",
        "current_conflict",
        "superseded_only",
        "unobserved",
        "current_inconclusive",
        "current_agreement",
    ]
    assert report.needs_follow_up_count == 6
    assert report.current_agreement_count == 1
    assert report.asset_type_counts == {"fiber_access_point": 6, "fiber_segment": 1}
    assert rows["FAT-WORKLIST-SUPERSEDED"]["staged_feature_id"] == str(
        latest_superseding.id
    )
    assert rows["SPAN-WORKLIST-UNOBSERVED"]["staged_feature_id"] == str(unobserved.id)
    assert all(len(str(row["row_sha256"])) == 64 for row in report.rows)


def test_worklist_separates_current_and_superseded_native_job_context(db_session):
    observed_at = datetime.now(UTC)
    current = _stage(db_session, "FAT-WORKLIST-CURRENT-JOB")
    old = _stage(
        db_session,
        "FAT-WORKLIST-OLD-JOB",
        created_at=observed_at,
    )
    technician, work_order = _actor_and_job(db_session)
    _record(db_session, current, technician, work_order)
    _record(db_session, old, technician, work_order)
    _stage(
        db_session,
        "FAT-WORKLIST-OLD-JOB",
        created_at=observed_at + timedelta(seconds=1),
    )

    report = reconcile_fiber_field_worklist(db_session)
    rows = {row["external_id"]: row for row in report.rows}

    assert rows["FAT-WORKLIST-CURRENT-JOB"]["current_work_orders"] == [
        {
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
        }
    ]
    assert rows["FAT-WORKLIST-CURRENT-JOB"]["superseded_work_orders"] == []
    assert rows["FAT-WORKLIST-OLD-JOB"]["current_work_orders"] == []
    assert rows["FAT-WORKLIST-OLD-JOB"]["superseded_work_orders"] == [
        {
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
        }
    ]
    assert report.rows_with_current_work_orders == 1
    assert report.rows_with_superseded_work_orders == 1


def test_new_observation_changes_exact_report_without_creating_other_state(db_session):
    feature = _stage(db_session, "FAT-WORKLIST-REPORT-HASH")
    technician, work_order = _actor_and_job(db_session)
    before = reconcile_fiber_field_worklist(db_session)

    _record(db_session, feature, technician, work_order)
    after = reconcile_fiber_field_worklist(db_session)
    replay = reconcile_fiber_field_worklist(db_session)

    assert before.report_sha256 != after.report_sha256
    assert after.rows[0]["verification_state"] == "current_agreement"
    assert replay.report_sha256 == after.report_sha256
    assert db_session.query(FiberTopologyFieldObservation).count() == 1


def test_admin_route_and_template_are_read_only_complete_cohort_projection():
    routes = {route.path: route for route in network_fiber_plant.router.routes}
    route = routes["/network/fiber-field-verification"]
    template = Path("templates/admin/network/fiber/field_verification_worklist.html")

    assert route.methods == {"GET"}
    assert template.exists()
    content = template.read_text()
    assert "always use the complete cohort" in content
    assert "cannot create or assign work orders" in content
    assert "worklist_rows" in content
    assert "list_pagination" in content
    assert "worklist.rows[:500]" not in content
    assert "cutover gate" in content
    assert 'type="submit"' not in content
