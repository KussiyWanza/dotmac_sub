"""Typed vendor supply projections and stale-safe staff review."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    Vendor,
)
from app.models.vendor_supply import VendorMaterialReleaseStatus
from app.schemas.vendor_portal import (
    VendorAdvanceCreate,
    VendorMaterialReleaseCreate,
    VendorMaterialReleaseItemCreate,
)
from app.services import (
    vendor_material_release,
    vendor_supply_review_proposals,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field import vendor_capabilities
from app.services.owner_commands import CommandContext
from app.services.ui_contracts import StateKind
from app.services.vendor_portal_operations import (
    RequestVendorAdvanceCommand,
    RequestVendorMaterialReleaseCommand,
    vendor_portal_operations,
)
from app.services.vendor_supply_review_proposals import (
    ConfirmVendorSupplyReviewCommand,
)
from app.services.vendor_supply_views import (
    VendorSupplyReviewAction,
    VendorSupplyType,
    project_workspace,
)


def _project(db_session):
    project = Project(name=f"Supply UI {uuid4().hex[:6]}")
    vendor = Vendor(name=f"Supply Vendor {uuid4().hex[:6]}", code=uuid4().hex[:10])
    db_session.add_all([project, vendor])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
        status=InstallationProjectStatus.in_progress.value,
    )
    db_session.add(installation)
    db_session.flush()
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.approved.value,
        currency="NGN",
        total=Decimal("100000.00"),
    )
    db_session.add(quote)
    db_session.flush()
    installation.approved_quote_id = quote.id
    db_session.commit()
    return installation, vendor


def _release(db_session, installation, vendor):
    row = vendor_material_release.request_release(
        db_session,
        vendor_material_release.RequestMaterialRelease(
            project_id=installation.id,
            vendor_id=vendor.id,
            requested_by_person_id=uuid4(),
            items=(
                {
                    "description": "24-core fibre",
                    "quantity": 250,
                    "unit": "m",
                },
            ),
        ),
    )
    db_session.commit()
    return row


def _context(*, actor: UUID, scope: UUID) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(actor),
        scope=str(scope),
        reason="test_vendor_supply_review",
    )


def test_project_workspace_exposes_owner_eligibility_and_provider_boundary(db_session):
    installation, vendor = _project(db_session)
    release = _release(db_session, installation, vendor)

    result = project_workspace(
        db_session,
        project_id=installation.id,
        vendor_id=vendor.id,
        capabilities=vendor_capabilities.capabilities_for_role("owner"),
    )

    assert result.material_request_action.allowed is True
    assert result.advance_request_action.allowed is True
    assert result.advance_quote_total == Decimal("100000.00")
    assert result.advance_remaining == Decimal("100000.00")
    projected = next(item for item in result.material_releases if item.id == release.id)
    assert projected.status.label == "Requested"
    assert projected.provider.status.kind is StateKind.not_applicable
    assert projected.approve_action.requires_confirmation is True


def test_typed_workspace_commands_own_vendor_supply_request_transactions(db_session):
    installation, vendor = _project(db_session)
    actor_id = uuid4()
    installation_id = installation.id
    vendor_id = vendor.id
    db_session_adapter.release_read_transaction(db_session)

    material = vendor_portal_operations.request_material_release(
        db_session,
        RequestVendorMaterialReleaseCommand(
            context=_context(actor=actor_id, scope=installation_id),
            payload=VendorMaterialReleaseCreate(
                project_id=installation_id,
                items=[
                    VendorMaterialReleaseItemCreate(
                        description="Joint enclosure",
                        quantity=2,
                        unit="each",
                    )
                ],
            ),
            vendor_id=vendor_id,
            user_id=actor_id,
        ),
    )
    advance = vendor_portal_operations.request_advance(
        db_session,
        RequestVendorAdvanceCommand(
            context=_context(actor=actor_id, scope=installation_id),
            payload=VendorAdvanceCreate(
                project_id=installation_id,
                amount=Decimal("25000.00"),
                reason="Mobilisation",
            ),
            vendor_id=vendor_id,
            user_id=actor_id,
        ),
    )

    assert material.status.value == "requested"
    assert advance.status.value == "requested"
    assert advance.amount == Decimal("25000.00")


def test_supervisor_can_request_material_but_not_an_advance(db_session):
    installation, vendor = _project(db_session)

    result = project_workspace(
        db_session,
        project_id=installation.id,
        vendor_id=vendor.id,
        capabilities=vendor_capabilities.capabilities_for_role("supervisor"),
    )

    assert result.material_request_action.allowed is True
    assert result.advance_request_action.allowed is False
    assert result.advance_request_action.reason == (
        "Only a vendor owner can request an advance."
    )


def test_signed_material_review_confirms_once_and_records_decision(db_session):
    installation, vendor = _project(db_session)
    release = _release(db_session, installation, vendor)
    actor_id = uuid4()
    release_id = release.id
    proposal = vendor_supply_review_proposals.issue_review(
        db_session,
        supply_type=VendorSupplyType.material,
        record_id=release_id,
        action=VendorSupplyReviewAction.approve,
        actor_id=actor_id,
        reason="Required for the approved route",
    )
    confirmation_token = proposal.confirmation_token
    db_session_adapter.release_read_transaction(db_session)

    result = vendor_supply_review_proposals.confirm_review(
        db_session,
        ConfirmVendorSupplyReviewCommand(
            context=_context(actor=actor_id, scope=release_id),
            confirmation_token=confirmation_token,
            supply_type=VendorSupplyType.material,
            record_id=release_id,
            action=VendorSupplyReviewAction.approve,
            actor_id=actor_id,
        ),
    )

    db_session.refresh(release)
    assert result.replayed is False
    assert release.status == VendorMaterialReleaseStatus.approved.value
    assert release.review_notes == "Required for the approved route"
    assert release.support_status is None


def test_material_confirmation_fails_closed_when_a_line_changes(db_session):
    installation, vendor = _project(db_session)
    release = _release(db_session, installation, vendor)
    actor_id = uuid4()
    release_id = release.id
    proposal = vendor_supply_review_proposals.issue_review(
        db_session,
        supply_type=VendorSupplyType.material,
        record_id=release_id,
        action=VendorSupplyReviewAction.approve,
        actor_id=actor_id,
    )
    confirmation_token = proposal.confirmation_token
    release.items[0].quantity = 300
    db_session.commit()
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(DomainError) as exc:
        vendor_supply_review_proposals.confirm_review(
            db_session,
            ConfirmVendorSupplyReviewCommand(
                context=_context(actor=actor_id, scope=release_id),
                confirmation_token=confirmation_token,
                supply_type=VendorSupplyType.material,
                record_id=release_id,
                action=VendorSupplyReviewAction.approve,
                actor_id=actor_id,
            ),
        )

    assert exc.value.code.endswith(".stale_proposal")
