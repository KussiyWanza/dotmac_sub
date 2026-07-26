"""Staff as-built review is explicit, durable, and separate from project state."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.project import Project
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    AsBuiltRoute,
    AsBuiltRouteReviewEvent,
    AsBuiltRouteReviewEventImmutableError,
    AsBuiltRouteStatus,
    InstallationProject,
    InstallationProjectStatus,
    Vendor,
)
from app.schemas.vendor_portal import VendorAsBuiltCreate, VendorAsBuiltLineCreate
from app.services import vendor_as_built_review_proposals, vendor_submission_proposals
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.vendor_as_built_review_proposals import (
    ConfirmVendorAsBuiltReviewCommand,
    VendorAsBuiltReviewConfirmationError,
)
from app.services.vendor_portal_errors import VendorPortalOperationError
from app.services.vendor_portal_operations import (
    VendorProjectWorkspaceError,
    vendor_portal_operations,
)
from app.services.vendor_submission_proposals import ConfirmVendorSubmissionCommand


def _confirm(
    db_session,
    *,
    token: str,
    as_built_id: str,
    action: str,
    actor_id: str,
):
    db_session_adapter.release_read_transaction(db_session)
    command_id = uuid4()
    return vendor_as_built_review_proposals.confirm_review(
        db_session,
        ConfirmVendorAsBuiltReviewCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=actor_id,
                scope=as_built_id,
                reason="test_vendor_as_built_review_confirmation",
            ),
            confirmation_token=token,
            as_built_id=as_built_id,
            action=action,
            actor_id=actor_id,
        ),
    )


def _submit_as_built(
    db_session,
    *,
    payload: VendorAsBuiltCreate,
    vendor_id: str,
    user_id: str,
):
    proposal = vendor_submission_proposals.issue_as_built_submission(
        db_session,
        payload=payload,
        vendor_id=vendor_id,
        user_id=user_id,
    )
    db_session_adapter.release_read_transaction(db_session)
    command_id = uuid4()
    return vendor_submission_proposals.confirm_submission(
        db_session,
        ConfirmVendorSubmissionCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=user_id,
                scope=vendor_id,
                reason="test_replacement_as_built_submission",
            ),
            confirmation_token=proposal.confirmation_token,
            vendor_id=vendor_id,
            user_id=user_id,
            project_id=str(payload.project_id),
        ),
    )


def _submitted(db_session):
    project = Project(name="As-built review project")
    vendor = Vendor(name="Evidence Vendor", code=f"EV-{uuid4().hex[:8]}")
    reviewer = SystemUser(
        first_name="Evidence",
        last_name="Reviewer",
        email=f"evidence-reviewer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([project, vendor, reviewer])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
        status=InstallationProjectStatus.completed.value,
    )
    db_session.add(installation)
    db_session.flush()
    as_built = AsBuiltRoute(
        project_id=installation.id,
        status=AsBuiltRouteStatus.submitted.value,
        actual_length_meters=125.0,
        variation_reason="Avoided a blocked duct",
        version=1,
    )
    db_session.add(as_built)
    db_session.commit()
    return installation, vendor, reviewer, as_built


def test_accept_records_review_projection_and_append_only_event(db_session):
    installation, vendor, reviewer, as_built = _submitted(db_session)

    result = vendor_portal_operations.transition_as_built_review(
        db_session,
        str(as_built.id),
        action="accept",
        actor_id=str(reviewer.id),
        reason="Route and quantities accepted",
    )

    db_session.refresh(as_built)
    db_session.refresh(installation)
    evidence = db_session.query(AsBuiltRouteReviewEvent).one()
    outbox = db_session.query(EventStore).one()
    assert as_built.status == AsBuiltRouteStatus.accepted.value
    assert as_built.reviewed_by_person_id == reviewer.id
    assert as_built.reviewed_at is not None
    assert as_built.review_notes == "Route and quantities accepted"
    assert installation.status == InstallationProjectStatus.completed.value
    assert result["accept_action"].allowed is False
    assert evidence.vendor_id == vendor.id
    assert evidence.event_type == "vendor_as_built.accepted"
    assert evidence.actor_id == str(reviewer.id)
    assert evidence.reason == "Route and quantities accepted"
    assert outbox.event_id == evidence.event_id
    assert outbox.payload["as_built_id"] == str(as_built.id)


def test_reject_requires_reason_and_does_not_request_project_rework(db_session):
    installation, _vendor, reviewer, as_built = _submitted(db_session)

    with pytest.raises(VendorPortalOperationError) as exc:
        vendor_portal_operations.transition_as_built_review(
            db_session,
            str(as_built.id),
            action="reject",
            actor_id=str(reviewer.id),
        )
    assert exc.value.code == "reason_required"

    vendor_portal_operations.transition_as_built_review(
        db_session,
        str(as_built.id),
        action="reject",
        actor_id=str(reviewer.id),
        reason="Route evidence is incomplete",
    )
    db_session.refresh(installation)
    assert installation.status == InstallationProjectStatus.completed.value
    evidence = db_session.query(AsBuiltRouteReviewEvent).one()
    assert evidence.event_type == "vendor_as_built.rejected"
    assert evidence.reason == "Route evidence is incomplete"


def test_review_event_is_append_only(db_session):
    _installation, _vendor, reviewer, as_built = _submitted(db_session)
    vendor_portal_operations.transition_as_built_review(
        db_session,
        str(as_built.id),
        action="accept",
        actor_id=str(reviewer.id),
    )
    evidence = db_session.query(AsBuiltRouteReviewEvent).one()
    evidence.reason = "rewritten"
    with pytest.raises(AsBuiltRouteReviewEventImmutableError):
        db_session.flush()
    db_session.rollback()


def test_signed_review_confirmation_is_stale_safe_and_exact_replay(db_session):
    _installation, _vendor, reviewer, as_built = _submitted(db_session)
    proposal = vendor_as_built_review_proposals.issue_review(
        db_session,
        as_built_id=str(as_built.id),
        action="accept",
        actor_id=str(reviewer.id),
        reason="Evidence accepted",
    )

    first = _confirm(
        db_session,
        token=proposal.confirmation_token,
        as_built_id=str(as_built.id),
        action="accept",
        actor_id=str(reviewer.id),
    )
    replay = _confirm(
        db_session,
        token=proposal.confirmation_token,
        as_built_id=str(as_built.id),
        action="accept",
        actor_id=str(reviewer.id),
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.review_event_id == first.review_event_id
    assert db_session.query(AsBuiltRouteReviewEvent).count() == 1
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == "vendor_as_built_accept")
        .count()
        == 1
    )


def test_review_confirmation_rejects_changed_evidence(db_session):
    _installation, _vendor, reviewer, as_built = _submitted(db_session)
    proposal = vendor_as_built_review_proposals.issue_review(
        db_session,
        as_built_id=str(as_built.id),
        action="accept",
        actor_id=str(reviewer.id),
    )
    as_built.actual_length_meters = 130.0
    db_session.commit()

    with pytest.raises(VendorAsBuiltReviewConfirmationError) as exc:
        _confirm(
            db_session,
            token=proposal.confirmation_token,
            as_built_id=str(as_built.id),
            action="accept",
            actor_id=str(reviewer.id),
        )

    assert exc.value.code.endswith(".stale_proposal")
    assert db_session.query(AsBuiltRouteReviewEvent).count() == 0
    assert db_session.query(IdempotencyKey).count() == 0


def test_rejected_evidence_can_be_replaced_with_next_version(db_session):
    installation, vendor, reviewer, as_built = _submitted(db_session)
    rejection = vendor_as_built_review_proposals.issue_review(
        db_session,
        as_built_id=str(as_built.id),
        action="reject",
        actor_id=str(reviewer.id),
        reason="Correct the quantities",
    )
    _confirm(
        db_session,
        token=rejection.confirmation_token,
        as_built_id=str(as_built.id),
        action="reject",
        actor_id=str(reviewer.id),
    )

    result = _submit_as_built(
        db_session,
        payload=VendorAsBuiltCreate(
            project_id=installation.id,
            line_items=[
                VendorAsBuiltLineCreate(
                    description="Corrected installed cable",
                    quantity=Decimal("125"),
                    unit_price=Decimal("0"),
                )
            ],
        ),
        vendor_id=str(vendor.id),
        user_id=str(reviewer.id),
    )

    replacement = db_session.get(AsBuiltRoute, result.result_id)
    assert replacement.status == AsBuiltRouteStatus.submitted.value
    assert db_session.query(AsBuiltRoute).count() == 2
    assert replacement.version == 2


def test_pending_evidence_blocks_duplicate_submission(db_session):
    installation, vendor, reviewer, _as_built = _submitted(db_session)
    with pytest.raises(VendorProjectWorkspaceError) as exc:
        vendor_portal_operations.preview_as_built_submission(
            db_session,
            VendorAsBuiltCreate(
                project_id=installation.id,
                line_items=[
                    VendorAsBuiltLineCreate(
                        description="Duplicate evidence",
                        quantity=Decimal("1"),
                        unit_price=Decimal("0"),
                    )
                ],
            ),
            str(vendor.id),
        )
    assert exc.value.code.endswith(".as_built_submission_not_allowed")


def test_review_queue_projects_inventory_gated_actions(db_session):
    _installation, _vendor, _reviewer, as_built = _submitted(db_session)
    projected = vendor_portal_operations.list_reviewable_as_builts(db_session)[0]
    assert projected["id"] == as_built.id
    assert projected["accept_action"].permission == "inventory:write"
    assert projected["reject_action"].permission == "inventory:write"


def test_review_routes_templates_and_sot_are_thin_and_explicit():
    root = Path(__file__).resolve().parents[1]
    routes = (root / "app/web/admin/vendor_operations.py").read_text(encoding="utf-8")
    queue = (root / "templates/admin/vendors/operations.html").read_text(
        encoding="utf-8"
    )
    detail = (root / "templates/admin/vendors/as_built_review_detail.html").read_text(
        encoding="utf-8"
    )
    vendor = (root / "templates/vendor/project_detail.html").read_text(encoding="utf-8")
    sot = (root / "docs/SOT_RELATIONSHIP_MAP.md").read_text(encoding="utf-8")
    migration = (root / "alembic/versions/374_as_built_review_evidence.py").read_text(
        encoding="utf-8"
    )

    assert "vendor_as_built_review_proposals.issue_review(" in routes
    assert "vendor_as_built_review_proposals.confirm_review(" in routes
    assert "transition_as_built_review(" not in routes
    assert "/admin/vendors/operations/as-built/{{ as_built.id }}" in queue
    assert "action_permitted(request, as_built.accept_action)" in detail
    assert "route_geojson" in detail
    assert "show_field_reviews" in queue
    assert "project.as_built_submissions" in vendor
    assert "operations.vendor_as_built_review_confirmation" in sot
    assert "vendor_as_built.accepted" in sot
    assert "vendor_as_built.rejected" in sot
    assert "as_built_route_review_events_append_only" in migration
