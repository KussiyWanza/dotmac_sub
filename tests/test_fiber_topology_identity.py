from __future__ import annotations

import uuid

import pytest

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestStatus,
)
from app.models.fiber_topology_identity import (
    FiberTopologyAssetSourceLink,
    FiberTopologyIdentityBatchReview,
    FiberTopologyIdentityDecision,
    FiberTopologyIdentityExecutionRun,
    FiberTopologyIdentityProposalBatch,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.gis import ServiceBuilding
from app.models.network import FdhCabinet, FiberAccessPoint
from app.services import fiber_change_requests
from app.services.network.fiber_topology_identity import (
    FiberTopologyIdentityError,
    approve_identity_decision,
    decline_identity_decision,
    execute_identity_decision,
    finalize_identity_decision,
    propose_identity_decision,
    supersede_approved_identity_decision,
)
from app.services.network.fiber_topology_review import (
    FiberTopologyProposalBatchBlocked,
    FiberTopologyReviewError,
    attest_identity_batch,
    execute_identity_batch,
    inspect_identity_batch,
    list_identity_review_queue,
    preview_identity_proposal_batch,
    propose_identity_batch,
    reconcile_identity_change_requests,
)


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _stage_feature(
    db_session,
    *,
    asset_type: str,
    external_id: str | None = None,
    display_name: str | None = None,
    match_status: str = "new",
    geometry: dict | None = None,
) -> FiberTopologyStagedFeature:
    is_blocked = match_status == "blocked"
    batch = FiberTopologySourceBatch(
        source_system="dotmac_osp_kmz",
        profile=f"pytest_{uuid.uuid4().hex}",
        source_name="pytest.kmz",
        asset_type=asset_type,
        external_id_key="source_id",
        file_sha256=_sha(),
        manifest_sha256=_sha(),
        status="blocked" if is_blocked else "staged",
        feature_count=1,
        blocker_count=1 if is_blocked else 0,
        candidate_count=0,
        unchanged_count=0,
        new_count=0 if is_blocked else 1,
        source_metadata={"test": True},
        created_by="pytest-stager",
    )
    feature = FiberTopologyStagedFeature(
        batch=batch,
        row_number=1,
        asset_type=asset_type,
        external_id=external_id or f"SRC-{uuid.uuid4().hex[:12]}",
        display_name=display_name or f"Test {asset_type}",
        geometry_type=(geometry or {}).get("type", "Polygon"),
        geometry_geojson=geometry
        or {
            "type": "Polygon",
            "coordinates": [[[7.40, 9.00], [7.42, 9.00], [7.42, 9.02], [7.40, 9.00]]],
        },
        source_properties={
            "Type": "FAT",
            "Placement": "aerial",
            "City": "Abuja",
        },
        content_sha256=_sha(),
        geometry_sha256=_sha(),
        match_status=match_status,
        blocker_codes=["pytest_blocker"] if is_blocked else [],
        match_reasons=[],
        candidate_asset_ids=[],
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def test_create_point_asset_uses_reviewed_change_request_then_links_source(
    db_session, subscriber, monkeypatch
):
    feature = _stage_feature(
        db_session,
        asset_type="fiber_access_point",
        external_id="FAT-PYTEST-1",
        display_name="FAT Pytest 1",
    )

    decision = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Stable source ID and geometry reviewed",
    )
    replay = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Stable source ID and geometry reviewed",
    )
    assert replay.id == decision.id

    approve_identity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Verified against the field evidence",
    )
    executed = execute_identity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )

    assert executed.status == "change_requested"
    assert db_session.query(FiberAccessPoint).count() == 0
    assert db_session.query(FiberChangeRequest).count() == 1
    change_request = db_session.get(FiberChangeRequest, executed.change_request_id)
    assert change_request is not None
    assert change_request.asset_type == "fiber_access_point"
    assert change_request.status == FiberChangeRequestStatus.pending
    assert change_request.payload["code"] == "FAT-PYTEST-1"
    assert change_request.payload["access_point_type"] == "FAT"

    execute_identity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )
    assert db_session.query(FiberChangeRequest).count() == 1

    monkeypatch.setattr(fiber_change_requests, "_geojson_to_geom", lambda _value: None)
    approved_request = fiber_change_requests.approve_request(
        db_session,
        str(change_request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical point asset approved",
    )
    canonical = db_session.get(FiberAccessPoint, approved_request.asset_id)
    assert canonical is not None

    finalized = finalize_identity_decision(
        db_session, decision.id, finalized_by="finalizer@example.com"
    )
    source_link = db_session.query(FiberTopologyAssetSourceLink).one()

    assert finalized.status == "applied"
    assert finalized.finalized_by == "finalizer@example.com"
    assert source_link.staged_feature_id == feature.id
    assert source_link.canonical_asset_type == "fiber_access_point"
    assert source_link.canonical_asset_id == canonical.id
    assert source_link.content_sha256 == feature.content_sha256
    assert (
        finalize_identity_decision(
            db_session, decision.id, finalized_by="another-finalizer@example.com"
        ).status
        == "applied"
    )
    assert db_session.query(FiberTopologyAssetSourceLink).count() == 1


def test_link_existing_writes_provenance_without_asset_change_request(db_session):
    feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-PYTEST-1",
    )
    cabinet = FdhCabinet(name="Canonical cabinet", code="CANONICAL-CAB-1")
    db_session.add(cabinet)
    db_session.commit()

    decision = propose_identity_decision(
        db_session,
        feature.id,
        "link_existing",
        target_asset_id=cabinet.id,
        proposed_by="planner@example.com",
        reason="Source cabinet matches the installed cabinet",
    )
    approve_identity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Code and coordinates independently verified",
    )
    applied = execute_identity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )

    assert applied.status == "applied"
    assert applied.change_request_id is None
    assert db_session.query(FiberChangeRequest).count() == 0
    source_link = db_session.query(FiberTopologyAssetSourceLink).one()
    assert source_link.canonical_asset_id == cabinet.id
    assert source_link.linked_by == "executor@example.com"

    new_source_version = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id=feature.external_id,
    )
    with pytest.raises(FiberTopologyIdentityError, match="already has"):
        propose_identity_decision(
            db_session,
            new_source_version.id,
            "link_existing",
            target_asset_id=cabinet.id,
            proposed_by="planner@example.com",
            reason="A second source version cannot redefine canonical identity",
        )


def test_declined_proposal_preserves_history_and_allows_correction(db_session):
    feature = _stage_feature(db_session, asset_type="fdh_cabinet")
    first = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Initial proposal",
    )

    declined = decline_identity_decision(
        db_session,
        first.id,
        reviewed_by="reviewer@example.com",
        review_notes="The initial identity evidence is insufficient",
    )
    assert declined.status == "declined"
    assert declined.closed_reason == "identity_decision_declined"

    corrected = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Corrected proposal with independently verified source evidence",
    )
    assert corrected.id != first.id
    assert corrected.status == "proposed"


def test_approved_unexecuted_decision_can_be_superseded_by_newer_source(
    db_session,
):
    feature = _stage_feature(
        db_session,
        asset_type="splice_closure",
        external_id="CLOSURE-PYTEST-1",
        display_name="Closure Pytest 1",
        geometry={"type": "Point", "coordinates": [7.4, 9.1]},
    )
    decision = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Initial source evidence",
    )
    approve_identity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Initial evidence approved",
    )
    fresh_feature = _stage_feature(
        db_session,
        asset_type="splice_closure",
        external_id="CLOSURE-PYTEST-1",
        display_name="Closure Pytest 1 refreshed",
        geometry={"type": "Point", "coordinates": [7.41, 9.11]},
    )

    superseded = supersede_approved_identity_decision(
        db_session,
        decision.id,
        superseded_by="migration-operator@example.com",
        reason="Fresh authoritative CRM snapshot supersedes the unexecuted decision",
    )

    assert superseded.status == "declined"
    assert superseded.closed_reason == "authoritative_source_superseded"
    assert superseded.reviewed_by == "reviewer@example.com"
    assert "Superseded by migration-operator@example.com" in superseded.review_notes

    corrected = propose_identity_decision(
        db_session,
        fresh_feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Fresh authoritative source evidence",
    )
    assert corrected.id != decision.id
    assert corrected.status == "proposed"


def test_supersede_refuses_decisions_without_newer_evidence_or_change_requests(
    db_session,
):
    feature = _stage_feature(
        db_session,
        asset_type="splice_closure",
        external_id="CLOSURE-PYTEST-2",
        geometry={"type": "Point", "coordinates": [7.4, 9.1]},
    )
    decision = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Initial source evidence",
    )
    approve_identity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Initial evidence approved",
    )

    with pytest.raises(FiberTopologyIdentityError, match="no newer staged"):
        supersede_approved_identity_decision(
            db_session,
            decision.id,
            superseded_by="migration-operator@example.com",
            reason="No newer source evidence exists",
        )

    executed = execute_identity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )

    assert executed.status == "change_requested"
    _stage_feature(
        db_session,
        asset_type="splice_closure",
        external_id="CLOSURE-PYTEST-2",
        geometry={"type": "Point", "coordinates": [7.41, 9.11]},
    )
    with pytest.raises(FiberTopologyIdentityError, match="approved, unexecuted"):
        supersede_approved_identity_decision(
            db_session,
            decision.id,
            superseded_by="migration-operator@example.com",
            reason="A pending owner request must keep its lifecycle",
        )


def test_proposer_cannot_review_and_changed_content_invalidates_decision(db_session):
    feature = _stage_feature(db_session, asset_type="fdh_cabinet")
    decision = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="same-actor@example.com",
        reason="Initial identity review",
    )

    with pytest.raises(FiberTopologyIdentityError, match="proposer cannot review"):
        approve_identity_decision(
            db_session,
            decision.id,
            reviewed_by="same-actor@example.com",
            review_notes="Self approval is forbidden",
        )

    feature.content_sha256 = _sha()
    db_session.commit()
    with pytest.raises(FiberTopologyIdentityError, match="content changed"):
        approve_identity_decision(
            db_session,
            decision.id,
            reviewed_by="independent@example.com",
            review_notes="This evidence is stale",
        )


def test_buildings_are_link_only_and_support_structures_use_reviewed_create(
    db_session,
):
    building_feature = _stage_feature(db_session, asset_type="service_building")
    building = ServiceBuilding(name="Canonical service building")
    db_session.add(building)
    db_session.commit()

    with pytest.raises(FiberTopologyIdentityError, match="not enabled"):
        propose_identity_decision(
            db_session,
            building_feature.id,
            "create",
            proposed_by="planner@example.com",
            reason="Building create is owned outside fiber mutations",
        )

    building_decision = propose_identity_decision(
        db_session,
        building_feature.id,
        "link_existing",
        target_asset_id=building.id,
        proposed_by="planner@example.com",
        reason="Existing GIS building verified",
    )
    approve_identity_decision(
        db_session,
        building_decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Building identity independently verified",
    )
    assert (
        execute_identity_decision(
            db_session,
            building_decision.id,
            executed_by="executor@example.com",
        ).status
        == "applied"
    )

    support_feature = _stage_feature(db_session, asset_type="support_structure")
    support_feature.source_properties = {"Type": "pole"}
    db_session.commit()
    support_decision = propose_identity_decision(
        db_session,
        support_feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Stable pole identity and location independently reviewable",
    )
    approve_identity_decision(
        db_session,
        support_decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Canonical support identity independently confirmed",
    )
    requested = execute_identity_decision(
        db_session, support_decision.id, executed_by="executor@example.com"
    )
    change_request = db_session.get(FiberChangeRequest, requested.change_request_id)
    assert requested.status == "change_requested"
    assert change_request is not None
    assert change_request.asset_type == "support_structure"
    assert change_request.payload["code"] == support_feature.external_id
    assert change_request.payload["support_type"] == "pole"


def test_path_geometry_is_not_an_identity_or_connectivity_decision(db_session):
    feature = _stage_feature(
        db_session,
        asset_type="fiber_segment",
        geometry={
            "type": "LineString",
            "coordinates": [[7.40, 9.00], [7.42, 9.02]],
        },
    )

    with pytest.raises(FiberTopologyIdentityError, match="point assets"):
        propose_identity_decision(
            db_session,
            feature.id,
            "create",
            proposed_by="planner@example.com",
            reason="Geometry alone must not create a connectivity edge",
        )


def test_rejected_change_request_closes_without_source_link(db_session, subscriber):
    feature = _stage_feature(db_session, asset_type="fdh_cabinet")
    decision = propose_identity_decision(
        db_session,
        feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Candidate cabinet identity",
    )
    approve_identity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Identity accepted for canonical mutation review",
    )
    executed = execute_identity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )
    assert (
        finalize_identity_decision(
            db_session, decision.id, finalized_by="early-finalizer@example.com"
        ).status
        == "change_requested"
    )

    fiber_change_requests.reject_request(
        db_session,
        str(executed.change_request_id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical mutation rejected",
    )
    closed = finalize_identity_decision(
        db_session, decision.id, finalized_by="finalizer@example.com"
    )

    assert closed.status == "closed"
    assert closed.closed_reason == "fiber_change_request_rejected"
    assert db_session.query(FiberTopologyAssetSourceLink).count() == 0


def test_review_queue_uses_latest_source_version_and_compares_candidates(db_session):
    old_feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-QUEUE-1",
        display_name="Old source cabinet",
    )
    candidate = FdhCabinet(
        name="Canonical queue cabinet",
        code="CANONICAL-QUEUE-1",
        latitude=9.01,
        longitude=7.41,
    )
    db_session.add(candidate)
    db_session.commit()
    new_feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-QUEUE-1",
        display_name="Latest source cabinet",
    )
    new_feature.candidate_asset_ids = [str(candidate.id)]
    new_feature.canonical_asset_id = candidate.id
    db_session.commit()

    page = list_identity_review_queue(db_session)

    assert page.total == 1
    assert len(page.items) == 1
    item = page.items[0]
    assert item["staged_feature_id"] == str(new_feature.id)
    assert item["staged_feature_id"] != str(old_feature.id)
    assert item["review_state"] == "unreviewed"
    assert item["eligible_actions"] == ["create", "link_existing", "reject"]
    assert item["candidate_assets"][0]["id"] == str(candidate.id)
    assert item["candidate_assets"][0]["is_suggested"] is True
    assert item["candidate_assets"][0]["distance_meters"] is not None

    propose_identity_decision(
        db_session,
        new_feature.id,
        "link_existing",
        target_asset_id=candidate.id,
        proposed_by="planner@example.com",
        reason="Queue candidate accepted for independent review",
    )
    assert list_identity_review_queue(db_session).total == 0
    active_page = list_identity_review_queue(db_session, state="active")
    assert active_page.total == 1
    assert active_page.items[0]["latest_decision"]["status"] == "proposed"


def test_newer_source_content_blocks_stale_review_and_parallel_identity(db_session):
    old_feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-STALE-1",
    )
    decision = propose_identity_decision(
        db_session,
        old_feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Initial source version",
    )
    new_feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-STALE-1",
        display_name="Changed source version",
    )

    queue = list_identity_review_queue(db_session, state="active")
    assert queue.total == 1
    assert queue.items[0]["staged_feature_id"] == str(new_feature.id)
    assert queue.items[0]["decision_content_is_current"] is False

    with pytest.raises(FiberTopologyIdentityError, match="newer staged version"):
        approve_identity_decision(
            db_session,
            decision.id,
            reviewed_by="reviewer@example.com",
            review_notes="Stale evidence must not be approved",
        )
    with pytest.raises(FiberTopologyIdentityError, match="active identity decision"):
        propose_identity_decision(
            db_session,
            new_feature.id,
            "create",
            proposed_by="planner@example.com",
            reason="Parallel source decision is forbidden",
        )


def test_applied_change_request_keeps_exact_old_provenance_and_surfaces_drift(
    db_session, subscriber, monkeypatch
):
    old_feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-DRIFT-1",
    )
    decision = propose_identity_decision(
        db_session,
        old_feature.id,
        "create",
        proposed_by="planner@example.com",
        reason="Original reviewed source content",
    )
    approve_identity_decision(
        db_session,
        decision.id,
        reviewed_by="reviewer@example.com",
        review_notes="Original content verified before change request",
    )
    executed = execute_identity_decision(
        db_session, decision.id, executed_by="executor@example.com"
    )
    new_feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-DRIFT-1",
        display_name="Changed after request emission",
    )
    change_request = db_session.get(FiberChangeRequest, executed.change_request_id)
    assert change_request is not None
    monkeypatch.setattr(fiber_change_requests, "_geojson_to_geom", lambda _value: None)
    fiber_change_requests.approve_request(
        db_session,
        str(change_request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical mutation already independently approved",
    )

    result = reconcile_identity_change_requests(
        db_session, finalized_by="reconciler@example.com"
    )
    linked = list_identity_review_queue(db_session, state="linked")

    assert result.applied == 1
    assert linked.total == 1
    assert linked.items[0]["staged_feature_id"] == str(new_feature.id)
    assert linked.items[0]["content_changed_since_link"] is True
    assert linked.items[0]["decision_content_is_current"] is False
    source_link = db_session.query(FiberTopologyAssetSourceLink).one()
    assert source_link.staged_feature_id == old_feature.id
    assert source_link.content_sha256 == old_feature.content_sha256


def test_batch_preview_persists_atomically_and_replays_by_request_hash(db_session):
    cabinet = _stage_feature(db_session, asset_type="fdh_cabinet")
    support = _stage_feature(db_session, asset_type="support_structure")
    items = [
        {"staged_feature_id": str(cabinet.id), "action": "create"},
        {
            "staged_feature_id": str(support.id),
            "action": "reject",
            "reason": "No canonical support-structure owner exists",
        },
    ]

    preview = preview_identity_proposal_batch(
        db_session,
        items,
        proposed_by="planner@example.com",
        reason="Reviewed point-asset cleanup cohort",
        source_name="pytest-review.json",
    )

    assert preview.ready is True
    assert len(preview.items) == 2
    assert db_session.query(FiberTopologyIdentityProposalBatch).count() == 0

    created = propose_identity_batch(
        db_session,
        items,
        proposed_by="planner@example.com",
        reason="Reviewed point-asset cleanup cohort",
        source_name="pytest-review.json",
    )
    decisions = (
        db_session.query(FiberTopologyIdentityDecision)
        .order_by(FiberTopologyIdentityDecision.proposal_batch_row_number)
        .all()
    )

    assert created.created is True
    assert created.request_sha256 == preview.request_sha256
    assert created.manifest_sha256 == preview.manifest_sha256
    assert len(decisions) == 2
    assert decisions[0].proposal_batch_id == created.batch_id
    assert decisions[0].proposal_batch_row_number == 1
    assert decisions[1].proposal_batch_row_number == 2

    replay = propose_identity_batch(
        db_session,
        items,
        proposed_by="planner@example.com",
        reason="Reviewed point-asset cleanup cohort",
        source_name="pytest-review.json",
    )
    assert replay.created is False
    assert replay.batch_id == created.batch_id
    assert replay.decision_ids == created.decision_ids
    assert db_session.query(FiberTopologyIdentityProposalBatch).count() == 1
    assert db_session.query(FiberTopologyIdentityDecision).count() == 2


def test_blocked_batch_writes_neither_manifest_nor_partial_decisions(db_session):
    cabinet = _stage_feature(db_session, asset_type="fdh_cabinet")
    path = _stage_feature(
        db_session,
        asset_type="fiber_segment",
        geometry={
            "type": "LineString",
            "coordinates": [[7.40, 9.00], [7.42, 9.02]],
        },
    )
    items = [
        {"staged_feature_id": str(cabinet.id), "action": "create"},
        {"staged_feature_id": str(path.id), "action": "create"},
    ]

    preview = preview_identity_proposal_batch(
        db_session,
        items,
        proposed_by="planner@example.com",
        reason="Cohort containing an ineligible path",
    )
    assert preview.ready is False
    assert preview.blockers[0]["row_number"] == 2
    assert "point assets" in preview.blockers[0]["message"]

    with pytest.raises(FiberTopologyProposalBatchBlocked) as exc:
        propose_identity_batch(
            db_session,
            items,
            proposed_by="planner@example.com",
            reason="Cohort containing an ineligible path",
        )
    assert exc.value.preview.manifest_sha256 == preview.manifest_sha256
    assert db_session.query(FiberTopologyIdentityProposalBatch).count() == 0
    assert db_session.query(FiberTopologyIdentityDecision).count() == 0


def test_reconciliation_sweep_projects_applied_rejected_and_pending_outcomes(
    db_session, subscriber, monkeypatch
):
    features = [
        _stage_feature(db_session, asset_type="fiber_access_point"),
        _stage_feature(db_session, asset_type="fdh_cabinet"),
        _stage_feature(db_session, asset_type="splice_closure"),
    ]
    decisions = []
    for index, feature in enumerate(features, start=1):
        decision = propose_identity_decision(
            db_session,
            feature.id,
            "create",
            proposed_by="planner@example.com",
            reason=f"Reconciliation cohort item {index}",
        )
        approve_identity_decision(
            db_session,
            decision.id,
            reviewed_by="reviewer@example.com",
            review_notes=f"Approved identity item {index}",
        )
        decisions.append(
            execute_identity_decision(
                db_session, decision.id, executed_by="executor@example.com"
            )
        )

    rejected_request = db_session.get(
        FiberChangeRequest, decisions[1].change_request_id
    )
    applied_request = db_session.get(FiberChangeRequest, decisions[2].change_request_id)
    assert rejected_request is not None
    assert applied_request is not None
    fiber_change_requests.reject_request(
        db_session,
        str(rejected_request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical cabinet creation rejected",
    )
    monkeypatch.setattr(fiber_change_requests, "_geojson_to_geom", lambda _value: None)
    fiber_change_requests.approve_request(
        db_session,
        str(applied_request.id),
        reviewer_person_id=str(subscriber.id),
        review_notes="Canonical closure creation approved",
    )

    result = reconcile_identity_change_requests(
        db_session, finalized_by="reconciler@example.com"
    )

    assert result.scanned == 3
    assert result.applied == 1
    assert result.closed == 1
    assert result.pending == 1
    assert result.errors == ()
    assert db_session.query(FiberTopologyAssetSourceLink).count() == 1

    replay = reconcile_identity_change_requests(
        db_session, finalized_by="reconciler@example.com"
    )
    assert replay.scanned == 1
    assert replay.pending == 1


def test_batch_attestation_requires_exact_manifest_and_independent_reviewer(
    db_session,
):
    features = [
        _stage_feature(db_session, asset_type="fdh_cabinet"),
        _stage_feature(db_session, asset_type="support_structure"),
    ]
    batch = propose_identity_batch(
        db_session,
        [
            {"staged_feature_id": str(features[0].id), "action": "create"},
            {"staged_feature_id": str(features[1].id), "action": "reject"},
        ],
        proposed_by="planner@example.com",
        reason="Exact batch attestation cohort",
    )

    with pytest.raises(FiberTopologyReviewError, match="expected manifest"):
        attest_identity_batch(
            db_session,
            batch.batch_id,
            expected_manifest_sha256=_sha(),
            action="approve",
            reviewed_by="reviewer@example.com",
            review_notes="Wrong manifest must fail closed",
        )
    with pytest.raises(FiberTopologyReviewError, match="proposer cannot attest"):
        attest_identity_batch(
            db_session,
            batch.batch_id,
            expected_manifest_sha256=batch.manifest_sha256,
            action="approve",
            reviewed_by="planner@example.com",
            review_notes="Self review must fail closed",
        )
    assert db_session.query(FiberTopologyIdentityBatchReview).count() == 0
    assert {
        decision.status
        for decision in db_session.query(FiberTopologyIdentityDecision).all()
    } == {"proposed"}

    reviewed = attest_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Independently verified exact manifest",
    )

    assert reviewed.created is True
    assert len(reviewed.decision_ids) == 2
    assert {
        decision.status
        for decision in db_session.query(FiberTopologyIdentityDecision).all()
    } == {"approved"}
    evidence = db_session.query(FiberTopologyIdentityBatchReview).one()
    assert evidence.batch_manifest_sha256 == batch.manifest_sha256
    assert evidence.attestation_sha256 == reviewed.attestation_sha256

    replay = attest_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Independently verified exact manifest",
    )
    assert replay.created is False
    assert replay.review_id == reviewed.review_id
    assert db_session.query(FiberTopologyIdentityBatchReview).count() == 1


def test_batch_decline_is_atomic_and_mixed_review_state_blocks(db_session):
    features = [
        _stage_feature(db_session, asset_type="fdh_cabinet"),
        _stage_feature(db_session, asset_type="support_structure"),
    ]
    batch = propose_identity_batch(
        db_session,
        [
            {"staged_feature_id": str(features[0].id), "action": "create"},
            {"staged_feature_id": str(features[1].id), "action": "reject"},
        ],
        proposed_by="planner@example.com",
        reason="Mixed-state guard cohort",
    )
    first = db_session.get(FiberTopologyIdentityDecision, batch.decision_ids[0])
    assert first is not None
    approve_identity_decision(
        db_session,
        first.id,
        reviewed_by="individual-reviewer@example.com",
        review_notes="Individual review creates a mixed batch state",
    )

    with pytest.raises(FiberTopologyReviewError, match="every decision"):
        attest_identity_batch(
            db_session,
            batch.batch_id,
            expected_manifest_sha256=batch.manifest_sha256,
            action="decline",
            reviewed_by="batch-reviewer@example.com",
            review_notes="Mixed batch must not partially decline",
        )
    statuses = [
        db_session.get(FiberTopologyIdentityDecision, decision_id).status
        for decision_id in batch.decision_ids
    ]
    assert statuses == ["approved", "proposed"]
    assert db_session.query(FiberTopologyIdentityBatchReview).count() == 0

    decline_features = [
        _stage_feature(db_session, asset_type="splice_closure"),
        _stage_feature(db_session, asset_type="support_structure"),
    ]
    decline_batch = propose_identity_batch(
        db_session,
        [
            {"staged_feature_id": str(decline_features[0].id), "action": "create"},
            {"staged_feature_id": str(decline_features[1].id), "action": "reject"},
        ],
        proposed_by="planner@example.com",
        reason="Atomic decline cohort",
    )
    declined = attest_identity_batch(
        db_session,
        decline_batch.batch_id,
        expected_manifest_sha256=decline_batch.manifest_sha256,
        action="decline",
        reviewed_by="batch-reviewer@example.com",
        review_notes="Cohort rejected after independent review",
    )
    assert declined.action == "decline"
    assert [
        db_session.get(FiberTopologyIdentityDecision, decision_id).status
        for decision_id in decline_batch.decision_ids
    ] == ["declined", "declined"]


def test_bounded_batch_execution_records_exact_outcomes_without_auto_approval(
    db_session,
):
    create_feature = _stage_feature(db_session, asset_type="fiber_access_point")
    reject_feature = _stage_feature(db_session, asset_type="support_structure")
    link_feature = _stage_feature(db_session, asset_type="fdh_cabinet")
    cabinet = FdhCabinet(name="Execution target", code="EXECUTION-TARGET")
    db_session.add(cabinet)
    db_session.commit()
    batch = propose_identity_batch(
        db_session,
        [
            {"staged_feature_id": str(create_feature.id), "action": "create"},
            {"staged_feature_id": str(reject_feature.id), "action": "reject"},
            {
                "staged_feature_id": str(link_feature.id),
                "action": "link_existing",
                "target_asset_id": str(cabinet.id),
            },
        ],
        proposed_by="planner@example.com",
        reason="Bounded execution cohort",
    )
    attest_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="All three identities independently verified",
    )

    with pytest.raises(FiberTopologyReviewError, match="expected manifest"):
        execute_identity_batch(
            db_session,
            batch.batch_id,
            expected_manifest_sha256=_sha(),
            executed_by="executor@example.com",
            limit=2,
        )
    first = execute_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        executed_by="executor@example.com",
        limit=2,
    )

    assert first.created is True
    assert first.counts == {
        "applied": 0,
        "change_requested": 1,
        "closed": 1,
        "error": 0,
    }
    assert first.remaining_approved_count == 1
    assert db_session.query(FiberAccessPoint).count() == 0
    change_request = db_session.query(FiberChangeRequest).one()
    assert change_request.status == FiberChangeRequestStatus.pending
    assert db_session.query(FiberTopologyIdentityExecutionRun).count() == 1

    second = execute_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        executed_by="executor@example.com",
        limit=2,
    )
    assert second.counts["applied"] == 1
    assert second.remaining_approved_count == 0
    assert db_session.query(FiberTopologyAssetSourceLink).count() == 1
    assert db_session.query(FiberChangeRequest).count() == 1

    complete = execute_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        executed_by="executor@example.com",
        limit=2,
    )
    assert complete.created is False
    assert complete.outcomes == ()
    assert db_session.query(FiberTopologyIdentityExecutionRun).count() == 2
    inspection = inspect_identity_batch(db_session, batch.batch_id)
    assert inspection["decision_status_counts"] == {
        "applied": 1,
        "change_requested": 1,
        "closed": 1,
    }
    assert len(inspection["execution_runs"]) == 2


def test_batch_execution_records_stale_source_error_and_leaves_decision_approved(
    db_session,
):
    feature = _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-BATCH-STALE",
    )
    batch = propose_identity_batch(
        db_session,
        [{"staged_feature_id": str(feature.id), "action": "create"}],
        proposed_by="planner@example.com",
        reason="Execution drift cohort",
    )
    attest_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Source was current at review time",
    )
    _stage_feature(
        db_session,
        asset_type="fdh_cabinet",
        external_id="CAB-BATCH-STALE",
        display_name="Changed after review",
    )

    result = execute_identity_batch(
        db_session,
        batch.batch_id,
        expected_manifest_sha256=batch.manifest_sha256,
        executed_by="executor@example.com",
        limit=1,
    )

    assert result.counts["error"] == 1
    assert "newer staged version" in result.outcomes[0]["message"]
    assert result.remaining_approved_count == 1
    decision = db_session.get(FiberTopologyIdentityDecision, batch.decision_ids[0])
    assert decision is not None
    assert decision.status == "approved"
    run = db_session.query(FiberTopologyIdentityExecutionRun).one()
    assert run.result_payload["outcomes"] == list(result.outcomes)
    assert run.result_sha256 == result.result_sha256
