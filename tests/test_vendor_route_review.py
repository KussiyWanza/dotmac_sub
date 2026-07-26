"""Staff proposed-route review is explicit, durable, and project-neutral."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.project import Project
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionReviewEvent,
    ProposedRouteRevisionReviewEventImmutableError,
    ProposedRouteRevisionStatus,
    Vendor,
)
from app.services import vendor_route_review_proposals
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_errors import VendorPortalOperationError
from app.services.vendor_portal_operations import vendor_portal_operations
from app.services.vendor_route_review_proposals import (
    ConfirmVendorRouteReviewCommand,
    VendorRouteReviewConfirmationError,
)


def _submitted(db_session):
    project = Project(name="Proposed route review project")
    vendor = Vendor(name="Route Vendor", code=f"RV-{uuid4().hex[:8]}")
    reviewer = SystemUser(
        first_name="Route",
        last_name="Reviewer",
        email=f"route-reviewer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([project, vendor, reviewer])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        status=InstallationProjectStatus.quoted.value,
    )
    db_session.add(installation)
    db_session.flush()
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.submitted.value,
    )
    db_session.add(quote)
    db_session.flush()
    revision = ProposedRouteRevision(
        quote_id=quote.id,
        revision_number=1,
        status=ProposedRouteRevisionStatus.submitted.value,
        length_meters=860.0,
    )
    db_session.add(revision)
    db_session.commit()
    return installation, vendor, reviewer, revision


def _confirm(
    db_session,
    *,
    token: str,
    revision_id: str,
    action: str,
    actor_id: str,
):
    db_session_adapter.release_read_transaction(db_session)
    command_id = uuid4()
    return vendor_route_review_proposals.confirm_review(
        db_session,
        ConfirmVendorRouteReviewCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=actor_id,
                scope=revision_id,
                reason="test_vendor_route_review_confirmation",
            ),
            confirmation_token=token,
            revision_id=revision_id,
            action=action,
            actor_id=actor_id,
        ),
    )


def test_accept_records_immutable_evidence_without_approving_quote_or_project(
    db_session,
):
    installation, vendor, reviewer, revision = _submitted(db_session)
    quote_status = revision.quote.status
    project_status = installation.status

    result = vendor_portal_operations.transition_route_revision_review(
        db_session,
        str(revision.id),
        action="accept",
        actor_id=str(reviewer.id),
        reason="Route is buildable",
    )

    db_session.refresh(revision)
    db_session.refresh(installation)
    evidence = db_session.query(ProposedRouteRevisionReviewEvent).one()
    outbox = db_session.query(EventStore).one()
    assert revision.status == ProposedRouteRevisionStatus.accepted.value
    assert revision.reviewed_by_person_id == reviewer.id
    assert revision.review_notes == "Route is buildable"
    assert revision.quote.status == quote_status
    assert installation.status == project_status
    assert result["review_event_id"] == str(evidence.id)
    assert evidence.vendor_id == vendor.id
    assert evidence.event_type == "vendor_route_revision.accepted"
    assert evidence.reason == "Route is buildable"
    assert outbox.event_id == evidence.event_id
    assert outbox.payload["revision_id"] == str(revision.id)


def test_reject_requires_reason(db_session):
    _installation, _vendor, reviewer, revision = _submitted(db_session)

    with pytest.raises(VendorPortalOperationError) as exc:
        vendor_portal_operations.transition_route_revision_review(
            db_session,
            str(revision.id),
            action="reject",
            actor_id=str(reviewer.id),
        )

    assert exc.value.code == "reason_required"


def test_signed_confirmation_is_exactly_replayable(db_session):
    _installation, _vendor, reviewer, revision = _submitted(db_session)
    proposal = vendor_route_review_proposals.issue_review(
        db_session,
        revision_id=str(revision.id),
        action="accept",
        actor_id=str(reviewer.id),
        reason="Route accepted",
    )

    result = _confirm(
        db_session,
        token=proposal.confirmation_token,
        revision_id=str(revision.id),
        action="accept",
        actor_id=str(reviewer.id),
    )
    replay = _confirm(
        db_session,
        token=proposal.confirmation_token,
        revision_id=str(revision.id),
        action="accept",
        actor_id=str(reviewer.id),
    )

    assert result.replayed is False
    assert replay.replayed is True
    assert replay.review_event_id == result.review_event_id
    assert db_session.query(ProposedRouteRevisionReviewEvent).count() == 1
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == "vendor_route_accept")
        .count()
        == 1
    )


def test_confirmation_fails_closed_when_route_changes_after_preview(db_session):
    _installation, _vendor, reviewer, revision = _submitted(db_session)
    proposal = vendor_route_review_proposals.issue_review(
        db_session,
        revision_id=str(revision.id),
        action="accept",
        actor_id=str(reviewer.id),
    )
    revision.length_meters = 900.0
    db_session.commit()

    with pytest.raises(VendorRouteReviewConfirmationError) as exc:
        _confirm(
            db_session,
            token=proposal.confirmation_token,
            revision_id=str(revision.id),
            action="accept",
            actor_id=str(reviewer.id),
        )

    assert exc.value.code.endswith(".stale_proposal")
    db_session.refresh(revision)
    assert revision.status == ProposedRouteRevisionStatus.submitted.value


def test_review_evidence_is_append_only(db_session):
    _installation, _vendor, reviewer, revision = _submitted(db_session)
    vendor_portal_operations.transition_route_revision_review(
        db_session,
        str(revision.id),
        action="reject",
        actor_id=str(reviewer.id),
        reason="Correct the route",
    )
    evidence = db_session.query(ProposedRouteRevisionReviewEvent).one()
    evidence.reason = "Changed"

    with pytest.raises(ProposedRouteRevisionReviewEventImmutableError):
        db_session.flush()
    db_session.rollback()


def test_review_queue_routes_templates_and_sot_are_explicit():
    root = Path(__file__).resolve().parents[1]
    routes = (root / "app/web/admin/vendor_operations.py").read_text()
    map_routes = (root / "app/web/admin/vendor_routes.py").read_text()
    route_template = (root / "templates/admin/vendors/route_view.html").read_text()
    queue = (root / "templates/admin/vendors/operations.html").read_text()
    sot = (root / "docs/SOT_RELATIONSHIP_MAP.md").read_text()
    migration = (
        root / "alembic/versions/424_proposed_route_review_evidence.py"
    ).read_text()

    assert "vendor_route_review_proposals.issue_review(" in routes
    assert "vendor_route_review_proposals.confirm_review(" in routes
    assert "transition_route_revision_review(" not in routes
    assert "list_route_revisions_for_project(" in map_routes
    assert "action_permitted(request, revision.accept_action)" in route_template
    assert "revision.detail_url" in queue
    assert "operations.vendor_route_review_confirmation" in sot
    assert "vendor_route_revision.accepted" in sot
    assert "vendor_route_revision.rejected" in sot
    assert "proposed_route_revision_review_events_append_only" in migration
