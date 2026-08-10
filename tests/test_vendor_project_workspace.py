"""Behavior tests for typed vendor project workspace commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.project import Project
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
    Vendor,
)
from app.schemas.vendor_portal import (
    VendorQuoteCreate,
    VendorQuoteLineCreate,
    VendorRouteRevisionCreate,
)
from app.services import vendor_project_records
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_operations import (
    AddVendorQuoteLineCommand,
    CreateVendorQuoteCommand,
    CreateVendorRouteRevisionCommand,
    ReviewVendorQuoteCommand,
    SubmitVendorRouteRevisionCommand,
    VendorProjectWorkspaceError,
    vendor_portal_operations,
)


def _context(*, actor: str, scope: str, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=actor,
        scope=scope,
        reason=reason,
    )


def _chain(db_session):
    project = Project(name="Typed vendor workspace")
    vendor = Vendor(name="Workspace Vendor", code=f"WV-{uuid4().hex[:8]}")
    user = SystemUser(
        first_name="Workspace",
        last_name="Operator",
        display_name="Workspace Operator",
        email=f"vendor-workspace-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([project, vendor, user])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
    )
    db_session.add(installation)
    db_session.commit()
    return installation, vendor, user


def _create_quote_command(installation, vendor_id, user_id):
    return CreateVendorQuoteCommand(
        context=_context(
            actor=str(user_id),
            scope=str(vendor_id),
            reason="test quote creation",
        ),
        payload=VendorQuoteCreate(
            project_id=installation.id,
            currency="NGN",
            vat_rate_percent=Decimal("7.5"),
        ),
        vendor_id=str(vendor_id),
        user_id=str(user_id),
    )


def test_a_vendor_cannot_quote_a_project_assigned_to_another_vendor(db_session):
    """The marketplace listing hid other vendors' projects, but the command
    never enforced it: any vendor holding a project id could open a quote on
    work assigned elsewhere. Visibility is a decision the owner makes."""
    installation, _assigned_vendor, user = _chain(db_session)
    intruder = Vendor(name="Intruder", code=f"INT-{uuid4().hex[:8]}")
    db_session.add(intruder)
    db_session.commit()
    command = _create_quote_command(installation, intruder.id, user.id)
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(VendorProjectWorkspaceError) as exc:
        vendor_portal_operations.create_quote(db_session, command)

    assert exc.value.code.endswith(".quote_creation_not_allowed")
    assert db_session.query(ProjectQuote).count() == 0


def test_an_unassigned_project_is_not_quotable_until_bidding_opens(db_session):
    """An unassigned project sitting in ``draft`` was never published. Without
    a status check any vendor could quote it the moment it was created."""
    installation, vendor, user = _chain(db_session)
    installation.assigned_vendor_id = None
    db_session.commit()
    command = _create_quote_command(installation, vendor.id, user.id)
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(VendorProjectWorkspaceError) as exc:
        vendor_portal_operations.create_quote(db_session, command)

    assert exc.value.code.endswith(".quote_creation_not_allowed")


def test_staff_vendor_queue_searches_draft_projects_for_assignment(db_session):
    installation, _vendor, _user = _chain(db_session)
    installation.project.name = "Cabinet Alpha Build"
    installation.project.code = "CAB-114"
    installation.project.number = "114"
    other_project = Project(name="Other draft project", code="OTHER-QUEUE")
    db_session.add(other_project)
    db_session.flush()
    db_session.add(InstallationProject(project_id=other_project.id))
    db_session.commit()

    result = vendor_portal_operations.list_draft_projects(db_session, search="114")

    assert [str(project.id) for project in result] == [str(installation.id)]


def test_staff_vendor_queue_searches_reviewable_quotes_by_project_and_vendor(
    db_session,
):
    installation, vendor, _user = _chain(db_session)
    installation.project.name = "Quote Search Project"
    installation.project.number = "QSP-114"
    vendor.name = "Searchable Queue Vendor"
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.submitted.value,
    )
    hidden = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.draft.value,
    )
    db_session.add_all([quote, hidden])
    db_session.commit()

    by_project = vendor_portal_operations.list_reviewable_quotes(
        db_session, search="QSP-114"
    )
    by_vendor = vendor_portal_operations.list_reviewable_quotes(
        db_session, search="Queue Vendor"
    )

    assert [str(row.id) for row in by_project] == [str(quote.id)]
    assert [str(row.id) for row in by_vendor] == [str(quote.id)]


def test_open_bidding_requires_an_actual_window(db_session):
    """``list_projects(available=True)`` requires both window bounds. The
    command must not accept a project the listing would never have shown."""
    installation, vendor, user = _chain(db_session)
    installation.assigned_vendor_id = None
    installation.status = InstallationProjectStatus.open_for_bidding.value
    db_session.commit()
    command = _create_quote_command(installation, vendor.id, user.id)
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(VendorProjectWorkspaceError) as exc:
        vendor_portal_operations.create_quote(db_session, command)

    assert exc.value.code.endswith(".quote_creation_not_allowed")


def test_any_vendor_may_quote_inside_an_open_bidding_window(db_session):
    """The positive case the guard must not break: genuinely published work is
    quotable by a vendor it was never explicitly assigned to."""
    installation, _vendor, user = _chain(db_session)
    bidder = Vendor(name="Bidder", code=f"BID-{uuid4().hex[:8]}")
    db_session.add(bidder)
    installation.assigned_vendor_id = None
    installation.status = InstallationProjectStatus.open_for_bidding.value
    installation.bidding_open_at = datetime.now(UTC) - timedelta(days=1)
    installation.bidding_close_at = datetime.now(UTC) + timedelta(days=1)
    db_session.commit()
    command = _create_quote_command(installation, bidder.id, user.id)
    db_session_adapter.release_read_transaction(db_session)

    quote = vendor_portal_operations.create_quote(db_session, command)

    assert quote["status"] == ProjectQuoteStatus.draft.value


def test_an_awarded_project_stops_accepting_new_quotes(db_session):
    """After award, change is a variation — not another bid. Even the winning
    vendor may not open a fresh quote against approved work."""
    installation, vendor, user = _chain(db_session)
    installation.status = InstallationProjectStatus.approved.value
    db_session.commit()
    command = _create_quote_command(installation, vendor.id, user.id)
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(VendorProjectWorkspaceError) as exc:
        vendor_portal_operations.create_quote(db_session, command)

    assert exc.value.code.endswith(".quote_creation_not_allowed")


def test_an_existing_open_draft_stays_reachable_after_the_window_closes(db_session):
    """Returning the vendor's own editable quote is a read of a row they
    already own, so it must not be gated by the creation policy."""
    installation, vendor, user = _chain(db_session)
    existing = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.draft.value,
    )
    db_session.add(existing)
    installation.status = InstallationProjectStatus.approved.value
    db_session.commit()
    command = _create_quote_command(installation, vendor.id, user.id)
    db_session_adapter.release_read_transaction(db_session)

    quote = vendor_portal_operations.create_quote(db_session, command)

    assert str(quote["id"]) == str(existing.id)
    assert db_session.query(ProjectQuote).count() == 1


def test_quote_creation_refusal_maps_to_403_on_both_transports():
    """Both vendor transports map error *suffixes*, defaulting anything they
    do not recognise to 500. An authorization refusal that arrives as a server
    error reads as a bug in Sub rather than a denied request, so the mapping is
    part of the fix, not a detail."""
    from app.api.vendor_portal import _vendor_http_error
    from app.web.vendor_portal import _submission_http_error

    error = VendorProjectWorkspaceError(
        code="operations.vendor_project_workspace.quote_creation_not_allowed",
        message="Project is assigned to another vendor.",
    )

    assert _vendor_http_error(error).status_code == 403
    assert _submission_http_error(error).status_code == 403


def test_typed_quote_commands_commit_rows_and_event_evidence(db_session):
    installation, vendor, user = _chain(db_session)
    vendor_id = str(vendor.id)
    user_id = str(user.id)
    command = CreateVendorQuoteCommand(
        context=_context(
            actor=user_id,
            scope=vendor_id,
            reason="test quote creation",
        ),
        payload=VendorQuoteCreate(
            project_id=installation.id,
            currency="NGN",
            vat_rate_percent=Decimal("7.5"),
        ),
        vendor_id=vendor_id,
        user_id=user_id,
    )
    db_session_adapter.release_read_transaction(db_session)
    quote = vendor_portal_operations.create_quote(db_session, command)
    assert db_session.in_transaction() is False

    quote = vendor_portal_operations.add_quote_line(
        db_session,
        AddVendorQuoteLineCommand(
            context=_context(
                actor=user_id,
                scope=vendor_id,
                reason="test quote line creation",
            ),
            quote_id=str(quote["id"]),
            payload=VendorQuoteLineCreate(
                description="Installation labor",
                quantity=Decimal("2"),
                unit_price=Decimal("10000"),
            ),
            vendor_id=vendor_id,
        ),
    )

    assert db_session.in_transaction() is False
    assert quote["subtotal"] == Decimal("20000.00")
    assert quote["tax_total"] == Decimal("1500.00")
    assert quote["total"] == Decimal("21500.00")
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "vendor_quote.changed")
        .count()
        == 2
    )


def test_rejected_quote_edit_rolls_back_the_owner_transaction(db_session):
    installation, vendor, user = _chain(db_session)
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.submitted.value,
    )
    db_session.add(quote)
    db_session.commit()
    command = AddVendorQuoteLineCommand(
        context=_context(
            actor=str(user.id),
            scope=str(vendor.id),
            reason="test rejected quote edit",
        ),
        quote_id=str(quote.id),
        payload=VendorQuoteLineCreate(
            description="Late line",
            quantity=Decimal("1"),
            unit_price=Decimal("1"),
        ),
        vendor_id=str(vendor.id),
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(VendorProjectWorkspaceError) as exc:
        vendor_portal_operations.add_quote_line(db_session, command)

    assert exc.value.code.endswith(".quote_not_editable")
    assert db_session.in_transaction() is False
    assert db_session.query(EventStore).count() == 0


def test_route_revision_commands_create_then_submit_owned_evidence(
    db_session,
    monkeypatch,
):
    installation, vendor, user = _chain(db_session)
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
    )
    db_session.add(quote)
    db_session.commit()
    monkeypatch.setattr(vendor_project_records, "_geom", lambda _geojson: None)
    vendor_id = str(vendor.id)
    user_id = str(user.id)
    quote_id = str(quote.id)

    db_session_adapter.release_read_transaction(db_session)
    created = vendor_portal_operations.create_route_revision(
        db_session,
        CreateVendorRouteRevisionCommand(
            context=_context(
                actor=user_id,
                scope=vendor_id,
                reason="test route revision creation",
            ),
            quote_id=quote_id,
            payload=VendorRouteRevisionCreate(
                geojson={
                    "type": "LineString",
                    "coordinates": [[7.4, 9.0], [7.5, 9.1]],
                },
                length_meters=125.5,
            ),
            vendor_id=vendor_id,
        ),
    )

    assert db_session.in_transaction() is False
    assert created["status"] == ProposedRouteRevisionStatus.draft.value

    submitted = vendor_portal_operations.submit_route_revision(
        db_session,
        SubmitVendorRouteRevisionCommand(
            context=_context(
                actor=user_id,
                scope=vendor_id,
                reason="test route revision submission",
            ),
            revision_id=str(created["id"]),
            vendor_id=vendor_id,
            user_id=user_id,
        ),
    )

    assert db_session.in_transaction() is False
    assert submitted["status"] == ProposedRouteRevisionStatus.submitted.value
    persisted = db_session.get(ProposedRouteRevision, created["id"])
    assert persisted is not None
    assert persisted.submitted_by_person_id == user.id
    events = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "vendor_route_revision.changed")
        .all()
    )
    assert [event.payload["action"] for event in events] == ["created", "submitted"]


def test_quote_review_updates_project_in_the_same_transaction(db_session):
    installation, vendor, user = _chain(db_session)
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.submitted.value,
    )
    db_session.add(quote)
    db_session.commit()
    command = ReviewVendorQuoteCommand(
        context=_context(
            actor=str(user.id),
            scope=str(quote.id),
            reason="test quote approval",
        ),
        quote_id=str(quote.id),
        reviewer_id=str(user.id),
        approve=True,
        notes="Reviewed",
    )
    db_session_adapter.release_read_transaction(db_session)

    result = vendor_portal_operations.review_quote(
        db_session,
        command,
    )

    assert db_session.in_transaction() is False
    db_session.refresh(installation)
    assert result["status"] == ProjectQuoteStatus.approved.value
    assert installation.status == InstallationProjectStatus.approved.value
    assert installation.approved_quote_id == quote.id
    event = db_session.query(EventStore).one()
    assert event.event_type == "vendor_quote.changed"
    assert event.payload["action"] == "approved"
