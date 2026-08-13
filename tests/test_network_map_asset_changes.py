from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditActorType, AuditEvent
from app.models.network import (
    FdhCabinet,
    FiberSegment,
    FiberSegmentType,
    FiberTerminationPoint,
)
from app.models.network_map_asset_change import NetworkMapAssetChangeProposal
from app.schemas.network_map_asset_changes import (
    GovernedNetworkAssetType,
    NetworkAssetChangeOperation,
    NetworkAssetCoordinates,
    NetworkAssetDraft,
    NetworkAssetProposalStatus,
    NetworkAssetReviewDecision,
    ReviewNetworkAssetProposalCommand,
    SubmitNetworkAssetProposalCommand,
)
from app.services import network_map_asset_changes as service
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

PROPOSER = uuid4()
REVIEWER = uuid4()


def test_create_proposal_before_values_persist_as_sql_null():
    column_type = NetworkMapAssetChangeProposal.__table__.c.before_values.type

    assert column_type.none_as_null is True


def _context(*, actor_id: UUID, scope: str, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{actor_id}",
        scope=scope,
        reason=reason,
        idempotency_key=f"test:{uuid4()}",
    )


def _submit(
    db_session,
    *,
    operation: NetworkAssetChangeOperation,
    asset_id: UUID | None,
    draft: NetworkAssetDraft,
):
    db_session_adapter.release_read_transaction(db_session)
    return service.submit_proposal(
        db_session,
        SubmitNetworkAssetProposalCommand(
            context=_context(
                actor_id=PROPOSER,
                scope=service.PROPOSE_PERMISSION,
                reason=f"Controlled {operation.value} fixture",
            ),
            actor_id=PROPOSER,
            actor_type=AuditActorType.user,
            actor_label="Map operator",
            asset_type=GovernedNetworkAssetType.fdh_cabinet,
            operation=operation,
            asset_id=asset_id,
            proposed=draft,
        ),
    )


def _review(db_session, proposal, decision, *, actor_id: UUID = REVIEWER):
    db_session_adapter.release_read_transaction(db_session)
    return service.review_proposal(
        db_session,
        ReviewNetworkAssetProposalCommand(
            context=_context(
                actor_id=actor_id,
                scope=service.REVIEW_PERMISSION,
                reason="Independent controlled review",
            ),
            actor_id=actor_id,
            actor_type=AuditActorType.user,
            actor_label="Independent reviewer",
            proposal_id=proposal.proposal_id,
            decision=decision,
            expected_proposal_sha256=proposal.proposal_sha256,
            review_notes="Reviewed against the authoritative asset evidence.",
        ),
    )


def _fdh(db_session, *, name: str = "FDH Alpha", latitude: float = 9.0):
    asset = FdhCabinet(
        name=name,
        code=f"FDH-{uuid4().hex[:8]}",
        latitude=latitude,
        longitude=7.0,
        notes="Canonical notes",
        is_active=True,
    )
    db_session.add(asset)
    db_session.commit()
    return asset


def test_create_edit_and_move_submission_never_mutate_canonical_assets(db_session):
    asset = _fdh(db_session)
    original_name = asset.name
    original_latitude = asset.latitude

    created = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.create,
        asset_id=None,
        draft=NetworkAssetDraft(
            name="FDH Proposed",
            code=f"FDH-{uuid4().hex[:8]}",
            coordinates=NetworkAssetCoordinates(latitude=9.2, longitude=7.2),
        ),
    )
    edited = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.edit,
        asset_id=asset.id,
        draft=NetworkAssetDraft(name="FDH Renamed"),
    )
    moved = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.move,
        asset_id=asset.id,
        draft=NetworkAssetDraft(
            coordinates=NetworkAssetCoordinates(latitude=9.1, longitude=7.1)
        ),
    )

    db_session.refresh(asset)
    assert created.status is NetworkAssetProposalStatus.pending
    assert edited.before is not None and edited.before.name == original_name
    assert edited.after.name == "FDH Renamed"
    assert moved.before is not None
    assert moved.after.coordinates.latitude == 9.1
    assert asset.name == original_name
    assert asset.latitude == original_latitude
    assert (
        db_session.scalar(select(FdhCabinet).where(FdhCabinet.name == "FDH Proposed"))
        is None
    )


def test_rejection_preserves_asset_and_records_before_after_audit(db_session):
    asset = _fdh(db_session)
    proposal = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.edit,
        asset_id=asset.id,
        draft=NetworkAssetDraft(name="Rejected name"),
    )

    rejected = _review(
        db_session,
        proposal,
        NetworkAssetReviewDecision.reject,
    )

    db_session.refresh(asset)
    assert rejected.status is NetworkAssetProposalStatus.rejected
    assert asset.name == "FDH Alpha"
    events = tuple(
        db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == service.ENTITY_TYPE,
                AuditEvent.entity_id == str(proposal.proposal_id),
            )
        )
    )
    assert {event.action for event in events} == {
        "network_map_asset_change.proposed",
        "network_map_asset_change.rejected",
    }
    assert all((event.metadata_ or {}).get("before") for event in events)
    assert all((event.metadata_ or {}).get("after") for event in events)


def test_independent_approval_updates_only_the_intended_asset(db_session):
    asset = _fdh(db_session)
    untouched = _fdh(db_session, name="FDH Untouched", latitude=9.5)
    proposal = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.edit,
        asset_id=asset.id,
        draft=NetworkAssetDraft(name="FDH Approved"),
    )

    with pytest.raises(DomainError, match="cannot review their own"):
        _review(
            db_session,
            proposal,
            NetworkAssetReviewDecision.approve,
            actor_id=PROPOSER,
        )

    applied = _review(
        db_session,
        proposal,
        NetworkAssetReviewDecision.approve,
    )
    db_session.refresh(asset)
    db_session.refresh(untouched)
    assert applied.status is NetworkAssetProposalStatus.applied
    assert applied.result_asset_id == asset.id
    assert asset.name == "FDH Approved"
    assert untouched.name == "FDH Untouched"


def test_approved_creation_materializes_one_canonical_asset(db_session):
    code = f"FDH-{uuid4().hex[:8]}"
    proposal = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.create,
        asset_id=None,
        draft=NetworkAssetDraft(
            name="Approved creation",
            code=code,
            coordinates=NetworkAssetCoordinates(latitude=9.22, longitude=7.22),
        ),
    )
    assert db_session.scalar(select(FdhCabinet).where(FdhCabinet.code == code)) is None

    applied = _review(
        db_session,
        proposal,
        NetworkAssetReviewDecision.approve,
    )

    created = db_session.scalar(select(FdhCabinet).where(FdhCabinet.code == code))
    assert created is not None
    assert applied.result_asset_id == created.id
    assert created.latitude == 9.22


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(91.0, 7.0), (-91.0, 7.0), (9.0, 181.0), (float("nan"), 7.0)],
)
def test_invalid_coordinates_are_rejected(db_session, latitude, longitude):
    with pytest.raises(DomainError, match="Latitude must be"):
        _submit(
            db_session,
            operation=NetworkAssetChangeOperation.create,
            asset_id=None,
            draft=NetworkAssetDraft(
                name="Invalid coordinate",
                coordinates=NetworkAssetCoordinates(
                    latitude=latitude,
                    longitude=longitude,
                ),
            ),
        )


def test_connected_asset_movement_requires_separate_topology_review(db_session):
    asset = _fdh(db_session)
    start = FiberTerminationPoint(
        name="FDH termination",
        ref_id=asset.id,
        latitude=9.0,
        longitude=7.0,
        is_active=True,
    )
    end = FiberTerminationPoint(
        name="Other termination",
        latitude=9.1,
        longitude=7.1,
        is_active=True,
    )
    segment = FiberSegment(
        name=f"SEG-{uuid4().hex[:8]}",
        segment_type=FiberSegmentType.distribution,
        from_point=start,
        to_point=end,
        route_geom="LINESTRING(7.0 9.0, 7.1 9.1)",
        fiber_count=12,
        is_active=True,
    )
    db_session.add_all([start, end, segment])
    db_session.commit()
    proposal = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.move,
        asset_id=asset.id,
        draft=NetworkAssetDraft(
            coordinates=NetworkAssetCoordinates(latitude=9.3, longitude=7.3)
        ),
    )

    with pytest.raises(DomainError, match="Connected assets cannot move"):
        _review(
            db_session,
            proposal,
            NetworkAssetReviewDecision.approve,
        )

    db_session.refresh(asset)
    db_session.refresh(segment)
    assert asset.latitude == 9.0
    assert str(segment.route_geom)


def test_nearby_unrelated_asset_movement_does_not_infer_a_connection(db_session):
    asset = _fdh(db_session, latitude=9.0)
    _fdh(db_session, name="Nearby unrelated", latitude=9.000001)
    proposal = _submit(
        db_session,
        operation=NetworkAssetChangeOperation.move,
        asset_id=asset.id,
        draft=NetworkAssetDraft(
            coordinates=NetworkAssetCoordinates(latitude=9.01, longitude=7.01)
        ),
    )

    applied = _review(
        db_session,
        proposal,
        NetworkAssetReviewDecision.approve,
    )

    db_session.refresh(asset)
    assert applied.status is NetworkAssetProposalStatus.applied
    assert asset.latitude == 9.01
