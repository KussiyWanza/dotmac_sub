from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.fiber_change_request import FiberChangeRequest
from app.models.fiber_topology_identity import (
    FiberTopologyAssetSourceLink,
    FiberTopologyIdentityDecision,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import FdhCabinet
from app.services.network.crm_network_map_point_migration import (
    CrmNetworkMapPointMigrationError,
    build_crm_point_migration_report,
    dry_run_crm_point_identity_apply,
    execute_crm_point_identity_apply,
    propose_crm_point_identity_proposals,
    reconcile_authoritative_crm_points,
    select_authoritative_crm_point_batches,
)
from app.services.network.fiber_topology_identity import stable_source_external_id
from app.services.network.fiber_topology_review import attest_identity_batch

ARCHIVE = "a" * 64
OLD_ARCHIVE = "b" * 64
MANIFEST = "c" * 64
OLD_MANIFEST = "d" * 64


def _sha() -> str:
    return uuid.uuid4().hex * 2


def _metadata(
    *,
    archive: str = ARCHIVE,
    manifest: str = MANIFEST,
    staged_count: int = 1,
    snapshot: str = "2026-08-14T10:00:00+00:00",
) -> dict[str, object]:
    return {
        "source_archive_sha256": archive,
        "source_database": "dotmac_omni isolated restore",
        "full_manifest_sha256": manifest,
        "snapshot_timestamp": snapshot,
        "importer_version": "stage_crm_network_map:v1",
        "source_count": staged_count,
        "restored_count": staged_count,
        "active_source_count": staged_count,
        "valid_active_source_count": staged_count,
        "staged_count": staged_count,
        "reconciliation_status": "source_restore_staged_counts_match",
    }


def _profile(asset_type: str) -> str:
    return {
        "fdh_cabinet": "crm_fdh_cabinets",
        "fiber_access_point": "crm_access_points",
        "splice_closure": "crm_splice_closures",
    }[asset_type]


def _batch(
    db_session,
    *,
    asset_type: str = "fdh_cabinet",
    archive: str = ARCHIVE,
    manifest: str = MANIFEST,
    staged_count: int = 1,
    snapshot: str = "2026-08-14T10:00:00+00:00",
    created_at: datetime | None = None,
    status: str = "staged",
) -> FiberTopologySourceBatch:
    batch = FiberTopologySourceBatch(
        source_system="dotmac_crm_fiber_map",
        profile=_profile(asset_type),
        source_name=f"{_profile(asset_type)}-pytest.kml",
        asset_type=asset_type,
        external_id_key="crm_id",
        file_sha256=_sha(),
        manifest_sha256=_sha(),
        status=status,
        feature_count=staged_count,
        blocker_count=1 if status == "blocked" else 0,
        candidate_count=0,
        unchanged_count=0,
        new_count=0 if status == "blocked" else staged_count,
        source_metadata=_metadata(
            archive=archive,
            manifest=manifest,
            staged_count=staged_count,
            snapshot=snapshot,
        ),
        created_by="pytest",
        created_at=created_at or datetime.now(UTC),
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _feature(
    db_session,
    batch: FiberTopologySourceBatch,
    *,
    row_number: int = 1,
    external_id: str | None = None,
    display_name: str | None = None,
    latitude: float = 9.0,
    longitude: float = 7.0,
    source_properties: dict | None = None,
    match_status: str = "new",
    blocker_codes: list[str] | None = None,
    candidate_asset_ids: list[str] | None = None,
) -> FiberTopologyStagedFeature:
    feature = FiberTopologyStagedFeature(
        batch_id=batch.id,
        row_number=row_number,
        asset_type=batch.asset_type,
        external_id=external_id or f"CRM-{uuid.uuid4().hex[:8]}",
        display_name=display_name or f"CRM {batch.asset_type}",
        geometry_type="Point",
        geometry_geojson={"type": "Point", "coordinates": [longitude, latitude]},
        source_properties=source_properties
        or {"crm_id": external_id or "CRM-ID", "code": external_id or "CRM-ID"},
        content_sha256=_sha(),
        geometry_sha256=_sha(),
        match_status=match_status,
        blocker_codes=blocker_codes or [],
        match_reasons=[],
        candidate_asset_ids=candidate_asset_ids or [],
    )
    db_session.add(feature)
    db_session.commit()
    db_session.refresh(feature)
    return feature


def test_authoritative_selection_supersedes_older_batches_without_using_largest_id(
    db_session,
):
    old = _batch(
        db_session,
        archive=OLD_ARCHIVE,
        manifest=OLD_MANIFEST,
        snapshot="2026-08-13T10:00:00+00:00",
        created_at=datetime.now(UTC) + timedelta(days=1),
    )
    _feature(db_session, old, external_id="CRM-OLD")
    selected = _batch(
        db_session,
        archive=ARCHIVE,
        manifest=MANIFEST,
        snapshot="2026-08-14T10:00:00+00:00",
        created_at=datetime.now(UTC),
    )
    _feature(db_session, selected, external_id="CRM-NEW")

    selections = select_authoritative_crm_point_batches(db_session)
    replay = select_authoritative_crm_point_batches(db_session)

    assert selections == replay
    assert selections[0].batch_ids == (selected.id,)
    assert selections[0].superseded_batch_ids == (old.id,)
    rows = reconcile_authoritative_crm_points(db_session)
    assert [row.classification for row in rows] == [
        "create_new",
        "superseded_source",
    ]
    assert rows[1].reason_code == "newer_authoritative_source_cohort_selected"

    with pytest.raises(CrmNetworkMapPointMigrationError, match="not authoritative"):
        select_authoritative_crm_point_batches(
            db_session, expected_archive_sha256=OLD_ARCHIVE
        )


def test_authoritative_selection_requires_complete_reconciliation_metadata(db_session):
    batch = _batch(db_session)
    metadata = dict(batch.source_metadata)
    metadata.pop("importer_version")
    batch.source_metadata = metadata
    db_session.commit()

    assert select_authoritative_crm_point_batches(db_session) == ()
    assert (
        select_authoritative_crm_point_batches(
            db_session, expected_archive_sha256=ARCHIVE
        )
        == ()
    )


def test_authoritative_selection_ignores_legacy_incomplete_batches_with_expected_hash(
    db_session,
):
    legacy = _batch(
        db_session,
        archive=OLD_ARCHIVE,
        manifest=OLD_MANIFEST,
        snapshot="2026-08-13T10:00:00+00:00",
        created_at=datetime.now(UTC) + timedelta(days=1),
    )
    legacy_metadata = dict(legacy.source_metadata)
    legacy_metadata.pop("importer_version")
    legacy.source_metadata = legacy_metadata
    _feature(db_session, legacy, external_id="CRM-OLD")

    selected = _batch(
        db_session,
        archive=ARCHIVE,
        manifest=MANIFEST,
        snapshot="2026-08-14T10:00:00+00:00",
        created_at=datetime.now(UTC),
    )
    _feature(db_session, selected, external_id="CRM-NEW")
    db_session.commit()

    selections = select_authoritative_crm_point_batches(
        db_session, expected_archive_sha256=ARCHIVE
    )

    assert len(selections) == 1
    assert selections[0].batch_ids == (selected.id,)
    assert legacy.id not in selections[0].superseded_batch_ids


def test_stable_crm_source_identity_is_used_for_decisions_and_links(
    db_session,
):
    batch = _batch(db_session)
    feature = _feature(db_session, batch, external_id="245")
    result = propose_crm_point_identity_proposals(
        db_session,
        expected_archive_sha256=ARCHIVE,
        proposed_by="planner@example.com",
        reason="Create reviewed CRM point assets",
    )
    decision = db_session.get(FiberTopologyIdentityDecision, result.decision_ids[0])
    row = reconcile_authoritative_crm_points(db_session)[0]

    assert decision is not None
    assert row.source_identity == "crm_network_map:fdh_cabinet:245"
    assert stable_source_external_id("dotmac_crm_fiber_map", "fdh_cabinet", "245") == (
        "crm_network_map:fdh_cabinet:245"
    )
    assert decision.source_external_id == "crm_network_map:fdh_cabinet:245"
    assert feature.external_id == "245"


def test_reconciliation_classifies_existing_links_exact_candidates_creates_and_invalids(
    db_session,
):
    linked_batch = _batch(db_session, staged_count=6)
    linked = _feature(db_session, linked_batch, row_number=1, external_id="L")
    linked_asset = FdhCabinet(name="Linked", code="LINKED")
    exact_asset = FdhCabinet(name="Exact", code="EXACT")
    candidate_asset = FdhCabinet(name="Candidate", code="OTHER")
    db_session.add_all([linked_asset, exact_asset, candidate_asset])
    db_session.commit()
    decision = FiberTopologyIdentityDecision(
        staged_feature_id=linked.id,
        source_system="dotmac_crm_fiber_map",
        source_asset_type="fdh_cabinet",
        source_external_id="crm_network_map:fdh_cabinet:L",
        feature_content_sha256=linked.content_sha256,
        action="link_existing",
        status="applied",
        target_asset_type="fdh_cabinet",
        target_asset_id=linked_asset.id,
        reason="pytest existing link",
        decision_sha256=_sha(),
        proposed_by="planner@example.com",
        reviewed_by="reviewer@example.com",
        review_notes="reviewed",
        reviewed_at=datetime.now(UTC),
        executed_by="executor@example.com",
        executed_at=datetime.now(UTC),
        finalized_by="executor@example.com",
        finalized_at=datetime.now(UTC),
    )
    db_session.add(decision)
    db_session.flush()
    db_session.add(
        FiberTopologyAssetSourceLink(
            decision_id=decision.id,
            staged_feature_id=linked.id,
            source_system="dotmac_crm_fiber_map",
            source_profile="crm_fdh_cabinets",
            source_asset_type="fdh_cabinet",
            external_id="crm_network_map:fdh_cabinet:L",
            content_sha256=linked.content_sha256,
            canonical_asset_type="fdh_cabinet",
            canonical_asset_id=linked_asset.id,
            status="active",
            linked_by="pytest",
        )
    )
    _feature(
        db_session,
        linked_batch,
        row_number=2,
        external_id="E",
        source_properties={"crm_id": "E", "code": "EXACT"},
    )
    _feature(
        db_session,
        linked_batch,
        row_number=3,
        external_id="C",
        display_name="Candidate",
        source_properties={"crm_id": "C", "code": "NO-MATCH"},
        candidate_asset_ids=[str(candidate_asset.id)],
    )
    _feature(db_session, linked_batch, row_number=4, external_id="N")
    _feature(db_session, linked_batch, row_number=5, external_id="DUP")
    _feature(db_session, linked_batch, row_number=6, external_id="DUP")

    rows = reconcile_authoritative_crm_points(db_session)

    assert [row.classification for row in rows] == [
        "already_linked",
        "exact_match",
        "candidate_match",
        "create_new",
        "invalid",
        "invalid",
    ]
    assert rows[4].reason_code == "duplicate_source_identity"


def test_inactive_and_invalid_coordinate_sources_fail_closed(db_session):
    batch = _batch(db_session, staged_count=2)
    _feature(
        db_session,
        batch,
        row_number=1,
        external_id="INACTIVE",
        source_properties={
            "crm_id": "INACTIVE",
            "code": "INACTIVE",
            "is_active": "false",
        },
    )
    _feature(db_session, batch, row_number=2, external_id="BAD", latitude=100.0)

    rows = reconcile_authoritative_crm_points(db_session)

    assert [row.reason_code for row in rows] == [
        "inactive_source_asset",
        "invalid_coordinates",
    ]


def test_proposal_generation_is_idempotent_and_does_not_write_canonical_assets(
    db_session,
):
    batch = _batch(db_session)
    _feature(db_session, batch, external_id="NEW")

    first = propose_crm_point_identity_proposals(
        db_session,
        expected_archive_sha256=ARCHIVE,
        proposed_by="planner@example.com",
        reason="Create missing CRM point assets",
    )
    second = propose_crm_point_identity_proposals(
        db_session,
        expected_archive_sha256=ARCHIVE,
        proposed_by="planner@example.com",
        reason="Create missing CRM point assets",
    )

    assert second.created is False
    assert second.batch_id == first.batch_id
    assert db_session.query(FdhCabinet).count() == 0


def test_dry_run_apply_writes_nothing_and_authoritative_apply_guards_hashes(
    db_session,
):
    batch = _batch(db_session)
    _feature(db_session, batch, external_id="NEW")
    proposed = propose_crm_point_identity_proposals(
        db_session,
        expected_archive_sha256=ARCHIVE,
        proposed_by="planner@example.com",
        reason="Create missing CRM point assets",
    )
    reviewed = attest_identity_batch(
        db_session,
        proposed.batch_id,
        expected_manifest_sha256=proposed.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Approved exact CRM point manifest",
    )

    dry_run = dry_run_crm_point_identity_apply(
        db_session,
        proposal_batch_id=proposed.batch_id,
        expected_archive_sha256=ARCHIVE,
    )

    assert dry_run["approved_decision_count"] == 1
    assert dry_run["canonical_writes"] == 0
    assert db_session.query(FiberChangeRequest).count() == 0
    with pytest.raises(CrmNetworkMapPointMigrationError, match="archive"):
        execute_crm_point_identity_apply(
            db_session,
            proposal_batch_id=proposed.batch_id,
            expected_manifest_sha256=proposed.manifest_sha256,
            expected_archive_sha256=OLD_ARCHIVE,
            executed_by="executor@example.com",
        )
    executed = execute_crm_point_identity_apply(
        db_session,
        proposal_batch_id=proposed.batch_id,
        expected_manifest_sha256=reviewed.batch_manifest_sha256,
        expected_archive_sha256=ARCHIVE,
        executed_by="executor@example.com",
    )

    assert executed.counts["change_requested"] == 1
    assert db_session.query(FdhCabinet).count() == 0
    assert db_session.query(FiberChangeRequest).count() == 1


def test_superseded_source_batch_refuses_apply(db_session):
    old = _batch(
        db_session,
        archive=OLD_ARCHIVE,
        manifest=OLD_MANIFEST,
        snapshot="2026-08-13T10:00:00+00:00",
    )
    _feature(db_session, old, external_id="OLD")
    proposed = propose_crm_point_identity_proposals(
        db_session,
        expected_archive_sha256=OLD_ARCHIVE,
        proposed_by="planner@example.com",
        reason="Old proposal",
    )
    attest_identity_batch(
        db_session,
        proposed.batch_id,
        expected_manifest_sha256=proposed.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Old manifest approved before newer snapshot",
    )
    new = _batch(
        db_session,
        archive=ARCHIVE,
        manifest=MANIFEST,
        snapshot="2026-08-14T10:00:00+00:00",
    )
    _feature(db_session, new, external_id="NEW")

    with pytest.raises(CrmNetworkMapPointMigrationError, match="superseded"):
        dry_run_crm_point_identity_apply(
            db_session,
            proposal_batch_id=proposed.batch_id,
            expected_archive_sha256=ARCHIVE,
        )

    with pytest.raises(CrmNetworkMapPointMigrationError, match="not authoritative"):
        dry_run_crm_point_identity_apply(
            db_session,
            proposal_batch_id=proposed.batch_id,
            expected_archive_sha256=OLD_ARCHIVE,
        )


def test_report_is_sanitized_and_read_only(db_session):
    batch = _batch(db_session)
    _feature(db_session, batch, external_id="NEW")

    report = build_crm_point_migration_report(db_session, include_rows=False)

    assert "rows" not in report
    assert report["canonical_count_before"] == {
        "fdh_cabinet": 0,
        "fiber_access_point": 0,
        "splice_closure": 0,
    }
    assert report["canonical_count_after"] == report["canonical_count_before"]
    assert report["per_asset"]["fdh_cabinet"] == {
        "source_total": 1,
        "valid_active_source_total": 1,
        "authoritative_batch": [str(batch.id)],
        "superseded_batches": [],
        "staged_total": 1,
        "already_linked": 0,
        "exact_matches": 0,
        "candidate_matches": 0,
        "proposed_creates": 1,
        "conflicts": 0,
        "invalid_records": 0,
        "approved_proposals": 0,
        "rejected_proposals": 0,
        "applied_proposals": 0,
        "failed_applies": 0,
        "canonical_count_before": 0,
        "canonical_count_after": 0,
        "hard_reconciliation_failure": False,
        "total_mismatch": False,
    }
    assert report["hard_reconciliation_failure"] is False
