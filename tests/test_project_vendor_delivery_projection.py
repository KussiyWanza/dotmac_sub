"""Read-only project-detail composition for vendor delivery facts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.project import Project
from app.models.vendor_routes import (
    AsBuiltRoute,
    AsBuiltRouteStatus,
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceStatus,
)
from app.services.project_vendor_delivery import (
    ProjectVendorDeliveryQuery,
    get_project_vendor_delivery,
)
from app.services.ui_contracts import StateKind


def _vendor_delivery_chain(db_session):
    project = Project(name="Project vendor delivery")
    vendor = Vendor(name="Abuja Fibre Delivery", code=f"AFD-{uuid4().hex[:8]}")
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
        total=Decimal("425000.00"),
    )
    db_session.add(quote)
    db_session.flush()
    installation.approved_quote_id = quote.id

    route = ProposedRouteRevision(
        quote_id=quote.id,
        revision_number=2,
        status=ProposedRouteRevisionStatus.accepted.value,
        length_meters=1860.0,
    )
    as_built = AsBuiltRoute(
        project_id=installation.id,
        status=AsBuiltRouteStatus.under_review.value,
        version=3,
        actual_length_meters=1905.0,
    )
    invoice = VendorPurchaseInvoice(
        project_id=installation.id,
        vendor_id=vendor.id,
        invoice_number="AFD-2026-0042",
        status=VendorPurchaseInvoiceStatus.approved.value,
        currency="NGN",
        total=Decimal("425000.00"),
        payables_document_reference="PINV-0042",
        payment_status="partially_paid",
        payment_total_amount=Decimal("425000.00"),
        payment_amount_paid=Decimal("200000.00"),
        payment_balance_due=Decimal("225000.00"),
        payment_observed_at=datetime.now(UTC),
    )
    db_session.add_all([route, as_built, invoice])
    db_session.commit()
    return project


def test_projection_composes_current_owner_facts_without_writing(db_session):
    project = _vendor_delivery_chain(db_session)
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted

    projection = get_project_vendor_delivery(
        db_session,
        ProjectVendorDeliveryQuery(
            project_id=project.id,
            can_read_operations=True,
            can_read_routes=True,
            can_read_financials=True,
        ),
    )

    assert projection is not None
    assert projection.vendor_name == "Abuja Fibre Delivery"
    assert projection.installation_status.value == "in_progress"
    assert projection.quote is not None
    assert projection.quote.status is not None
    assert projection.quote.status.value == "approved"
    assert projection.quote.total == Decimal("425000.00")
    assert projection.quote.url is not None
    assert "/admin/vendors/operations/quotes/" in projection.quote.url
    assert projection.route is not None
    assert projection.route.status is not None
    assert projection.route.status.value == "accepted"
    assert projection.route.revision_number == 2
    assert projection.route.url is not None
    assert "?revision_id=" in projection.route.url
    assert projection.as_built is not None
    assert projection.as_built.status is not None
    assert projection.as_built.status.value == "under_review"
    assert projection.as_built.version == 3
    assert projection.as_built.url is not None
    assert "/vendors/operations/as-built/" in projection.as_built.url
    assert projection.invoice is not None
    assert projection.invoice.status is not None
    assert projection.invoice.status.value == "approved"
    assert projection.invoice.url is not None
    assert "/vendors/operations/invoices/" in projection.invoice.url
    assert projection.invoice.payment is not None
    assert projection.invoice.payment.status.kind is StateKind.present
    assert projection.invoice.payment.status.value.value == "partially_paid"
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


def test_projection_omits_financial_and_route_facts_without_permissions(db_session):
    project = _vendor_delivery_chain(db_session)

    projection = get_project_vendor_delivery(
        db_session,
        ProjectVendorDeliveryQuery(
            project_id=project.id,
            can_read_operations=True,
        ),
    )

    assert projection is not None
    assert projection.quote is not None
    assert projection.quote.status is not None
    assert projection.quote.total is None
    assert projection.route is None
    assert projection.as_built is not None
    assert projection.invoice is None


def test_route_only_projection_does_not_expose_quote_or_finance(db_session):
    project = _vendor_delivery_chain(db_session)

    projection = get_project_vendor_delivery(
        db_session,
        ProjectVendorDeliveryQuery(
            project_id=project.id,
            can_read_routes=True,
        ),
    )

    assert projection is not None
    assert projection.operations_url is None
    assert projection.quote is None
    assert projection.route is not None
    assert projection.as_built is None
    assert projection.invoice is None


def test_projection_is_absent_without_scope_or_installation_project(db_session):
    project = Project(name="No vendor delivery scope")
    db_session.add(project)
    db_session.commit()

    assert (
        get_project_vendor_delivery(
            db_session,
            ProjectVendorDeliveryQuery(
                project_id=project.id,
                can_read_operations=True,
            ),
        )
        is None
    )


def test_unassigned_installation_does_not_invent_invoice_state(db_session):
    project = Project(name="Unassigned vendor delivery")
    db_session.add(project)
    db_session.flush()
    db_session.add(InstallationProject(project_id=project.id))
    db_session.commit()

    projection = get_project_vendor_delivery(
        db_session,
        ProjectVendorDeliveryQuery(
            project_id=project.id,
            can_read_operations=True,
            can_read_financials=True,
        ),
    )

    assert projection is not None
    assert projection.vendor_name == "Unassigned"
    assert projection.invoice is not None
    assert projection.invoice.status is None
    assert "after a vendor is assigned" in projection.invoice.detail
    assert (
        get_project_vendor_delivery(
            db_session,
            ProjectVendorDeliveryQuery(project_id=project.id),
        )
        is None
    )
