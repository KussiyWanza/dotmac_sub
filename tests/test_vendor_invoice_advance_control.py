"""The disbursement control that stops a vendor being paid twice.

An advance leaves Sub as money owed to the vendor, but it is never transmitted
to ERP: the AP sync payload has no prepayment field (and forbids extras), and
ERP cannot park an on-account payment against a future invoice. ERP therefore
bills every Sub-originated invoice gross. Nothing in either system nets the
advance, so an approved advance plus a full invoice pays the same work twice.

Sub is the only place holding both numbers, so the control lives at invoice
approval. It refuses rather than adjusting the total, because Sub never
rewrites a vendor's stated invoice amount.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.project import Project
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceLineItem,
    VendorPurchaseInvoiceStatus,
)
from app.models.vendor_supply import VendorAdvance, VendorAdvanceStatus
from app.services import vendor_purchase_invoice_records
from app.services.owner_commands import CommandContext
from app.services.vendor_purchase_invoices import ReviewVendorPurchaseInvoiceCommand


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{uuid4()}",
        scope="operations:vendor_invoice:review",
        reason="advance control test",
    )


def _reviewer(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Invoice",
        last_name="Reviewer",
        display_name="Invoice Reviewer",
        email=f"reviewer-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _project(db_session, *, quote_total: Decimal = Decimal("1000000.00")):
    project = Project(name=f"Buildout {uuid4().hex[:6]}")
    vendor = Vendor(name=f"Vendor {uuid4().hex[:6]}", code=f"V-{uuid4().hex[:8]}")
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
        total=quote_total,
    )
    db_session.add(quote)
    db_session.flush()
    installation.approved_quote_id = quote.id
    db_session.flush()
    return installation, vendor


def _advance(db_session, installation, vendor, amount: Decimal, status: str):
    db_session.add(
        VendorAdvance(
            project_id=installation.id,
            vendor_id=vendor.id,
            quote_id=installation.approved_quote_id,
            amount=amount,
            currency="NGN",
            status=status,
            is_active=True,
        )
    )
    db_session.flush()


def _submitted_invoice(db_session, installation, vendor, amount: Decimal):
    invoice = VendorPurchaseInvoice(
        project_id=installation.id,
        vendor_id=vendor.id,
        invoice_number=f"INV-{uuid4().hex[:6]}",
        currency="NGN",
        status=VendorPurchaseInvoiceStatus.submitted.value,
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        VendorPurchaseInvoiceLineItem(
            invoice_id=invoice.id,
            description="Fibre build",
            quantity=Decimal("1"),
            unit_price=amount,
            amount=amount,
            is_active=True,
        )
    )
    db_session.flush()
    return invoice


def _approve(db_session, invoice, reviewer):
    return vendor_purchase_invoice_records.stage_review(
        db_session,
        ReviewVendorPurchaseInvoiceCommand(
            context=_context(),
            invoice_id=str(invoice.id),
            reviewer_system_user_id=str(reviewer.id),
            approve=True,
            review_notes=None,
        ),
    )


def test_invoice_plus_approved_advance_over_the_quote_is_refused(db_session):
    reviewer = _reviewer(db_session)
    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.approved.value,
    )
    invoice = _submitted_invoice(
        db_session, installation, vendor, Decimal("1000000.00")
    )
    db_session.commit()

    with pytest.raises(Exception) as excinfo:
        _approve(db_session, invoice, reviewer)

    assert "invoice_exceeds_quote_net_of_advances" in str(
        getattr(excinfo.value, "code", "")
    )
    # The refusal names both numbers so the reviewer can tell the vendor what
    # to reissue, rather than leaving them to guess at the shortfall.
    assert "300000" in str(excinfo.value)
    assert "1000000" in str(excinfo.value)


def test_invoice_issued_net_of_the_advance_is_approved(db_session):
    reviewer = _reviewer(db_session)
    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.approved.value,
    )
    invoice = _submitted_invoice(db_session, installation, vendor, Decimal("700000.00"))
    db_session.commit()

    result = _approve(db_session, invoice, reviewer)

    assert result["status"] == VendorPurchaseInvoiceStatus.approved.value


def test_a_merely_requested_advance_does_not_reduce_what_may_be_invoiced(db_session):
    """A requested advance reserves ceiling but authorises no disbursement."""

    reviewer = _reviewer(db_session)
    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.requested.value,
    )
    invoice = _submitted_invoice(
        db_session, installation, vendor, Decimal("1000000.00")
    )
    db_session.commit()

    result = _approve(db_session, invoice, reviewer)

    assert result["status"] == VendorPurchaseInvoiceStatus.approved.value


def test_a_rejected_advance_frees_the_amount_for_invoicing(db_session):
    reviewer = _reviewer(db_session)
    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.rejected.value,
    )
    invoice = _submitted_invoice(
        db_session, installation, vendor, Decimal("1000000.00")
    )
    db_session.commit()

    result = _approve(db_session, invoice, reviewer)

    assert result["status"] == VendorPurchaseInvoiceStatus.approved.value


def test_the_quote_total_itself_is_reachable_exactly(db_session):
    """The boundary is inclusive: advance plus invoice may equal the quote."""

    reviewer = _reviewer(db_session)
    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("400000.00"),
        VendorAdvanceStatus.settled.value,
    )
    invoice = _submitted_invoice(db_session, installation, vendor, Decimal("600000.00"))
    db_session.commit()

    result = _approve(db_session, invoice, reviewer)

    assert result["status"] == VendorPurchaseInvoiceStatus.approved.value


def test_a_project_without_advances_is_unaffected(db_session):
    reviewer = _reviewer(db_session)
    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    invoice = _submitted_invoice(
        db_session, installation, vendor, Decimal("1000000.00")
    )
    db_session.commit()

    result = _approve(db_session, invoice, reviewer)

    assert result["status"] == VendorPurchaseInvoiceStatus.approved.value


def test_an_operator_records_the_disbursement_that_no_transport_reports(db_session):
    """Payment happens outside Sub, so the operator who paid is the observation.

    Without this the settled state is unreachable and Sub cannot tell money it
    has committed from money the vendor actually holds.
    """

    from app.services import vendor_advances
    from app.services.vendor_supply_views import (
        VendorSupplyReviewAction,
        advance_review_preview,
    )

    reviewer = _reviewer(db_session)
    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.approved.value,
    )
    advance = db_session.query(VendorAdvance).one()
    db_session.commit()

    preview = advance_review_preview(
        db_session,
        advance_id=str(advance.id),
        action=VendorSupplyReviewAction.disburse,
        reason="NIBSS-TRF-99812",
    )
    assert "paid" in preview.summary

    settled = vendor_advances.apply_payables_observation(
        db_session,
        advance.id,
        payables_system="operator",
        payables_reference="NIBSS-TRF-99812",
        payables_status="paid",
    )

    assert settled.status == VendorAdvanceStatus.settled.value
    assert settled.payables_reference == "NIBSS-TRF-99812"
    # A settled advance still counts against what may be invoiced.
    assert vendor_advances.authorised_total(db_session, installation.id) == Decimal(
        "300000.00"
    )
    assert reviewer is not None


def test_a_disbursement_record_requires_the_payment_reference(db_session):
    from app.services.vendor_supply_views import (
        VendorSupplyProjectionError,
        VendorSupplyReviewAction,
        advance_review_preview,
    )

    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.approved.value,
    )
    advance = db_session.query(VendorAdvance).one()
    db_session.commit()

    with pytest.raises(VendorSupplyProjectionError):
        advance_review_preview(
            db_session,
            advance_id=str(advance.id),
            action=VendorSupplyReviewAction.disburse,
            reason="   ",
        )


def test_only_an_approved_advance_can_be_recorded_as_paid(db_session):
    from app.services.vendor_supply_views import (
        VendorSupplyProjectionError,
        VendorSupplyReviewAction,
        advance_review_preview,
    )

    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.requested.value,
    )
    advance = db_session.query(VendorAdvance).one()
    db_session.commit()

    with pytest.raises(VendorSupplyProjectionError):
        advance_review_preview(
            db_session,
            advance_id=str(advance.id),
            action=VendorSupplyReviewAction.disburse,
            reason="NIBSS-TRF-11111",
        )


def test_approved_advances_surface_as_outstanding_disbursement_work(db_session):
    """The disbursement action needs somewhere that shows the work.

    The review queue lists only *requested* advances, so an approved one would
    be invisible and its payment record would never be made — leaving the
    vendor's invoice blocked with nothing explaining why.
    """

    from app.services.vendor_supply_views import (
        advance_disbursement_queue,
        advance_review_queue,
    )

    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.approved.value,
    )
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("50000.00"),
        VendorAdvanceStatus.requested.value,
    )
    db_session.commit()

    outstanding = advance_disbursement_queue(db_session)
    review = advance_review_queue(db_session)

    assert outstanding.count == 1
    assert outstanding.items[0].amount == Decimal("300000.00")
    assert outstanding.items[0].disburse_action.allowed is True
    # The requested one is still review work, not disbursement work.
    assert review.count == 1
    assert review.items[0].amount == Decimal("50000.00")
    assert review.items[0].disburse_action.allowed is False


def test_a_settled_advance_leaves_the_outstanding_queue(db_session):
    from app.services import vendor_advances
    from app.services.vendor_supply_views import advance_disbursement_queue

    installation, vendor = _project(db_session, quote_total=Decimal("1000000.00"))
    _advance(
        db_session,
        installation,
        vendor,
        Decimal("300000.00"),
        VendorAdvanceStatus.approved.value,
    )
    advance = db_session.query(VendorAdvance).one()
    db_session.commit()

    vendor_advances.apply_payables_observation(
        db_session,
        advance.id,
        payables_system="operator",
        payables_reference="NIBSS-TRF-40021",
        payables_status="paid",
    )
    db_session.commit()

    assert advance_disbursement_queue(db_session).count == 0


def test_the_operations_page_surfaces_outstanding_disbursements():
    from pathlib import Path

    source = Path("templates/admin/vendors/operations.html").read_text(encoding="utf-8")

    assert "Advances awaiting disbursement record" in source
    assert "advances_awaiting_disbursement | length" in source
    assert "their invoice can be refused" in source
