"""Buildout work reaching a vendor.

Sales could scope vendor work; network buildout could not. ``InstallationProject``
always allowed it — ``subscriber_id`` is nullable and ``buildout_project_id``
FKs to ``buildout_projects`` — but the only creation path demanded a subscriber,
so plant we build ourselves had no way to reach a vendor at all.

These tests pin the second entry point and the two intake decisions that make a
drafted project vendor-visible, which previously had no writer anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.project import Project
from app.models.qualification import BuildoutProject
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectStatus,
    Vendor,
    VendorAssignmentType,
)
from app.services import installation_projects, vendor_project_lifecycle
from app.services.vendor_portal_errors import VendorProjectLifecycleError

ACTOR = "staff:test"


def _buildout(db_session) -> BuildoutProject:
    buildout = BuildoutProject()
    db_session.add(buildout)
    db_session.commit()
    return buildout


def _scoped(db_session) -> InstallationProject:
    buildout = _buildout(db_session)
    installation = installation_projects.ensure_for_buildout(
        db_session, buildout_project_id=buildout.id, actor_id=ACTOR
    )
    db_session.commit()
    return installation


# ---------------------------------------------------------------------------
# Scoping buildout work
# ---------------------------------------------------------------------------


def test_buildout_scope_creates_a_subscriberless_project_root(db_session):
    buildout = _buildout(db_session)

    installation = installation_projects.ensure_for_buildout(
        db_session, buildout_project_id=buildout.id, actor_id=ACTOR
    )
    db_session.commit()

    assert installation.buildout_project_id == buildout.id
    assert installation.subscriber_id is None
    assert installation.status == InstallationProjectStatus.draft.value
    project = db_session.get(Project, installation.project_id)
    assert project.subscriber_id is None
    assert project.sales_order_id is None
    # Provenance doubles as the idempotency key.
    assert project.external_system == "buildout"
    assert project.external_reference == str(buildout.id)


def test_buildout_scope_is_idempotent(db_session):
    """Re-scoping the same buildout must not mint a second project root — the
    downstream award/PO chain anchors on the installation project id."""
    buildout = _buildout(db_session)

    first = installation_projects.ensure_for_buildout(
        db_session, buildout_project_id=buildout.id, actor_id=ACTOR
    )
    db_session.commit()
    second = installation_projects.ensure_for_buildout(
        db_session, buildout_project_id=buildout.id, actor_id=ACTOR
    )
    db_session.commit()

    assert first.id == second.id
    assert db_session.query(InstallationProject).count() == 1
    assert db_session.query(Project).count() == 1


def test_buildout_scope_rejects_an_unknown_buildout(db_session):
    with pytest.raises(installation_projects.InstallationScopeError) as exc:
        installation_projects.ensure_for_buildout(
            db_session, buildout_project_id=uuid4(), actor_id=ACTOR
        )

    assert exc.value.code == "buildout_project_not_found"


def test_buildout_scope_requires_an_actor(db_session):
    buildout = _buildout(db_session)

    with pytest.raises(installation_projects.InstallationScopeError) as exc:
        installation_projects.ensure_for_buildout(
            db_session, buildout_project_id=buildout.id, actor_id="  "
        )

    assert exc.value.code == "actor_required"


# ---------------------------------------------------------------------------
# Publishing for bidding
# ---------------------------------------------------------------------------


def test_publishing_opens_bidding_and_records_evidence(db_session):
    installation = _scoped(db_session)
    opens = datetime.now(UTC)
    closes = opens + timedelta(days=7)

    result = vendor_project_lifecycle.stage_publish_for_bidding(
        db_session,
        vendor_project_lifecycle.StagePublishForBidding(
            project_id=str(installation.id),
            actor_id=ACTOR,
            bidding_open_at=opens,
            bidding_close_at=closes,
        ),
    )
    db_session.commit()

    db_session.refresh(installation)
    assert installation.status == InstallationProjectStatus.open_for_bidding.value
    assert installation.assignment_type == VendorAssignmentType.bidding.value
    assert installation.bidding_open_at is not None
    assert installation.bidding_close_at is not None
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    assert evidence.from_status == InstallationProjectStatus.draft.value
    assert evidence.to_status == InstallationProjectStatus.open_for_bidding.value
    assert evidence.actor_id == ACTOR
    assert result["lifecycle_event_id"] == str(evidence.id)
    # Scoping already emitted project.created + installation_scope.created,
    # so assert on the transition's own event rather than the only event.
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "vendor_project.published")
        .count()
        == 1
    )


def test_publishing_requires_a_window_that_closes_after_it_opens(db_session):
    installation = _scoped(db_session)
    opens = datetime.now(UTC)

    with pytest.raises(VendorProjectLifecycleError) as exc:
        vendor_project_lifecycle.stage_publish_for_bidding(
            db_session,
            vendor_project_lifecycle.StagePublishForBidding(
                project_id=str(installation.id),
                actor_id=ACTOR,
                bidding_open_at=opens,
                bidding_close_at=opens - timedelta(hours=1),
            ),
        )

    assert exc.value.code.endswith(".invalid_bidding_window")


def test_publishing_refuses_an_already_directed_project(db_session):
    """Bidding and direct assignment are alternative intakes, not a sequence."""
    installation = _scoped(db_session)
    vendor = Vendor(name="Directed", code=f"D-{uuid4().hex[:8]}")
    db_session.add(vendor)
    db_session.commit()
    vendor_project_lifecycle.stage_assign_vendor_directly(
        db_session,
        vendor_project_lifecycle.StageAssignVendorDirectly(
            project_id=str(installation.id),
            vendor_id=str(vendor.id),
            actor_id=ACTOR,
        ),
    )
    db_session.commit()
    opens = datetime.now(UTC)

    with pytest.raises(VendorProjectLifecycleError) as exc:
        vendor_project_lifecycle.stage_publish_for_bidding(
            db_session,
            vendor_project_lifecycle.StagePublishForBidding(
                project_id=str(installation.id),
                actor_id=ACTOR,
                bidding_open_at=opens,
                bidding_close_at=opens + timedelta(days=1),
            ),
        )

    # It never reaches the assignment check: the project already left draft.
    assert exc.value.code.endswith(".invalid_transition")


# ---------------------------------------------------------------------------
# Direct assignment
# ---------------------------------------------------------------------------


def test_direct_assignment_names_the_vendor_without_a_window(db_session):
    installation = _scoped(db_session)
    vendor = Vendor(name="Kaduna Fibre", code=f"KF-{uuid4().hex[:8]}")
    db_session.add(vendor)
    db_session.commit()

    vendor_project_lifecycle.stage_assign_vendor_directly(
        db_session,
        vendor_project_lifecycle.StageAssignVendorDirectly(
            project_id=str(installation.id),
            vendor_id=str(vendor.id),
            actor_id=ACTOR,
            reason="Existing framework agreement",
        ),
    )
    db_session.commit()

    db_session.refresh(installation)
    assert installation.status == InstallationProjectStatus.assigned.value
    assert installation.assigned_vendor_id == vendor.id
    assert installation.assignment_type == VendorAssignmentType.direct.value
    assert installation.bidding_open_at is None
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    assert evidence.reason == "Existing framework agreement"
    assert evidence.vendor_id == vendor.id
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "vendor_project.assigned")
        .count()
        == 1
    )


def test_direct_assignment_rejects_an_inactive_vendor(db_session):
    installation = _scoped(db_session)
    vendor = Vendor(name="Retired", code=f"R-{uuid4().hex[:8]}", is_active=False)
    db_session.add(vendor)
    db_session.commit()

    with pytest.raises(VendorProjectLifecycleError) as exc:
        vendor_project_lifecycle.stage_assign_vendor_directly(
            db_session,
            vendor_project_lifecycle.StageAssignVendorDirectly(
                project_id=str(installation.id),
                vendor_id=str(vendor.id),
                actor_id=ACTOR,
            ),
        )

    assert exc.value.code.endswith(".vendor_not_found")


# ---------------------------------------------------------------------------
# The journey this unblocks
# ---------------------------------------------------------------------------


def test_a_published_buildout_is_quotable_by_an_unassigned_vendor(db_session):
    """The end-to-end point of this slice.

    Before: buildout work could not be scoped, could not be published, and the
    quote policy refused every project a vendor was not already assigned to —
    so no vendor could ever quote plant work. This asserts the whole intake
    path now lands somewhere a vendor can actually act.
    """
    from app.models.system_user import SystemUser
    from app.schemas.vendor_portal import VendorQuoteCreate
    from app.services.db_session_adapter import db_session_adapter
    from app.services.owner_commands import CommandContext
    from app.services.vendor_portal_operations import (
        CreateVendorQuoteCommand,
        vendor_portal_operations,
    )

    installation = _scoped(db_session)
    bidder = Vendor(name="Open Bidder", code=f"OB-{uuid4().hex[:8]}")
    user = SystemUser(
        first_name="Vendor",
        last_name="Estimator",
        display_name="Vendor Estimator",
        email=f"bid-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([bidder, user])
    db_session.commit()

    opens = datetime.now(UTC) - timedelta(hours=1)
    vendor_project_lifecycle.stage_publish_for_bidding(
        db_session,
        vendor_project_lifecycle.StagePublishForBidding(
            project_id=str(installation.id),
            actor_id=ACTOR,
            bidding_open_at=opens,
            bidding_close_at=opens + timedelta(days=7),
        ),
    )
    db_session.commit()

    # It shows up in the vendor-facing marketplace read...
    available = vendor_portal_operations.list_projects(
        db_session, str(bidder.id), available=True, limit=50, offset=0
    )
    assert [str(row["id"]) for row in available] == [str(installation.id)]

    # ...and the command owner lets that vendor quote it.
    command_id = uuid4()
    command = CreateVendorQuoteCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=str(user.id),
            scope=str(bidder.id),
            reason="buildout bid",
        ),
        payload=VendorQuoteCreate(project_id=installation.id, currency="NGN"),
        vendor_id=str(bidder.id),
        user_id=str(user.id),
    )
    db_session_adapter.release_read_transaction(db_session)
    quote = vendor_portal_operations.create_quote(db_session, command)

    assert quote["project_id"] == installation.id
