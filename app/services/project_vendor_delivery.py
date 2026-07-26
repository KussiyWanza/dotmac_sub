"""Permission-scoped vendor-delivery projection for native project detail.

Vendor lifecycle, quote, route, as-built, invoice, and ERP payment owners keep
their existing authority. This module performs a read-only composition over
those facts so the project UI does not reproduce selection or freshness rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.vendor_routes import (
    AsBuiltRoute,
    InstallationProject,
    ProjectQuote,
    ProposedRouteRevision,
    VendorPurchaseInvoice,
)
from app.schemas.status_presentation import StatusPresentation
from app.services.status_presentation import (
    as_built_route_status_presentation,
    installation_project_status_presentation,
    proposed_route_revision_status_presentation,
    vendor_purchase_invoice_status_presentation,
    vendor_quote_status_presentation,
)
from app.services.vendor_payment_status import (
    VendorPaymentProjection,
    project_vendor_payment_status,
)

VENDOR_OPERATIONS_URL = "/admin/vendors/operations"


@dataclass(frozen=True, slots=True)
class ProjectVendorDeliveryQuery:
    """Exact project scope and read capabilities supplied by the web adapter."""

    project_id: UUID
    can_read_operations: bool = False
    can_read_routes: bool = False
    can_read_financials: bool = False

    @property
    def has_visible_scope(self) -> bool:
        return (
            self.can_read_operations or self.can_read_routes or self.can_read_financials
        )


@dataclass(frozen=True, slots=True)
class VendorQuoteGlance:
    status: StatusPresentation | None
    detail: str
    vendor_name: str | None = None
    total: Decimal | None = None
    currency: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class VendorRouteGlance:
    status: StatusPresentation | None
    detail: str
    revision_number: int | None = None
    length_meters: float | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class VendorAsBuiltGlance:
    status: StatusPresentation | None
    detail: str
    version: int | None = None
    actual_length_meters: float | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class VendorInvoiceGlance:
    status: StatusPresentation | None
    detail: str
    invoice_number: str | None = None
    total: Decimal | None = None
    currency: str | None = None
    payment: VendorPaymentProjection | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectVendorDeliveryProjection:
    installation_project_id: UUID
    vendor_name: str
    installation_status: StatusPresentation
    quote: VendorQuoteGlance | None
    route: VendorRouteGlance | None
    as_built: VendorAsBuiltGlance | None
    invoice: VendorInvoiceGlance | None
    operations_url: str | None


def _latest_quote(row: InstallationProject) -> ProjectQuote | None:
    active_quotes = [quote for quote in row.quotes if quote.is_active]
    if row.approved_quote_id is not None:
        approved = next(
            (quote for quote in active_quotes if quote.id == row.approved_quote_id),
            None,
        )
        if approved is not None:
            return approved
    if row.assigned_vendor_id is not None:
        assigned_quotes = [
            quote
            for quote in active_quotes
            if quote.vendor_id == row.assigned_vendor_id
        ]
        if assigned_quotes:
            active_quotes = assigned_quotes
    return max(
        active_quotes,
        key=lambda quote: (quote.created_at, str(quote.id)),
        default=None,
    )


def _latest_route(quote: ProjectQuote | None) -> ProposedRouteRevision | None:
    if quote is None:
        return None
    return max(
        quote.route_revisions,
        key=lambda revision: (revision.revision_number, str(revision.id)),
        default=None,
    )


def _latest_as_built(row: InstallationProject) -> AsBuiltRoute | None:
    return max(
        row.as_built_routes,
        key=lambda item: (
            item.submitted_at or item.created_at,
            item.version,
            str(item.id),
        ),
        default=None,
    )


def _current_invoice(row: InstallationProject) -> VendorPurchaseInvoice | None:
    if row.assigned_vendor_id is None:
        return None
    assigned_invoices = [
        invoice
        for invoice in row.purchase_invoices
        if invoice.is_active and invoice.vendor_id == row.assigned_vendor_id
    ]
    return max(
        assigned_invoices,
        key=lambda invoice: (invoice.updated_at, str(invoice.id)),
        default=None,
    )


def _quote_glance(
    quote: ProjectQuote | None,
    *,
    include_amount: bool,
) -> VendorQuoteGlance:
    if quote is None:
        return VendorQuoteGlance(
            status=None,
            detail="No active vendor quote exists for this installation.",
            url=VENDOR_OPERATIONS_URL,
        )
    vendor_name = getattr(quote.vendor, "name", None)
    return VendorQuoteGlance(
        status=vendor_quote_status_presentation(quote.status),
        detail=(
            f"Current quote from {vendor_name}."
            if vendor_name
            else "Current vendor quote."
        ),
        vendor_name=vendor_name,
        total=quote.total if include_amount else None,
        currency=quote.currency if include_amount else None,
        url=f"{VENDOR_OPERATIONS_URL}/quotes/{quote.id}",
    )


def _route_glance(
    row: InstallationProject,
    route: ProposedRouteRevision | None,
) -> VendorRouteGlance:
    url = f"/admin/vendors/routes/{row.id}"
    if route is None:
        return VendorRouteGlance(
            status=None,
            detail="No proposed route revision exists for the current quote.",
            url=url,
        )
    return VendorRouteGlance(
        status=proposed_route_revision_status_presentation(route.status),
        detail=f"Latest proposed route revision {route.revision_number}.",
        revision_number=route.revision_number,
        length_meters=route.length_meters,
        url=f"{url}?revision_id={route.id}",
    )


def _as_built_glance(as_built: AsBuiltRoute | None) -> VendorAsBuiltGlance:
    if as_built is None:
        return VendorAsBuiltGlance(
            status=None,
            detail="No as-built record has been submitted.",
            url=VENDOR_OPERATIONS_URL,
        )
    return VendorAsBuiltGlance(
        status=as_built_route_status_presentation(as_built.status),
        detail=f"Latest as-built record, version {as_built.version}.",
        version=as_built.version,
        actual_length_meters=as_built.actual_length_meters,
        url=f"{VENDOR_OPERATIONS_URL}/as-built/{as_built.id}",
    )


def _invoice_glance(
    row: InstallationProject,
    invoice: VendorPurchaseInvoice | None,
) -> VendorInvoiceGlance:
    if row.assigned_vendor_id is None:
        return VendorInvoiceGlance(
            status=None,
            detail="A purchase invoice is available after a vendor is assigned.",
            url=VENDOR_OPERATIONS_URL,
        )
    if invoice is None:
        return VendorInvoiceGlance(
            status=None,
            detail="No active purchase invoice exists for the assigned vendor.",
            url=VENDOR_OPERATIONS_URL,
        )
    return VendorInvoiceGlance(
        status=vendor_purchase_invoice_status_presentation(invoice.status),
        detail="Current purchase invoice for the assigned vendor.",
        invoice_number=invoice.invoice_number,
        total=invoice.total,
        currency=invoice.currency,
        payment=project_vendor_payment_status(invoice),
        url=f"{VENDOR_OPERATIONS_URL}/invoices/{invoice.id}",
    )


def get_project_vendor_delivery(
    db: Session,
    query: ProjectVendorDeliveryQuery,
) -> ProjectVendorDeliveryProjection | None:
    """Compose current vendor delivery facts without mutating owner state."""

    if not query.has_visible_scope:
        return None

    row = (
        db.query(InstallationProject)
        .options(
            joinedload(InstallationProject.assigned_vendor),
            selectinload(InstallationProject.quotes).joinedload(ProjectQuote.vendor),
            selectinload(InstallationProject.quotes).selectinload(
                ProjectQuote.route_revisions
            ),
            selectinload(InstallationProject.as_built_routes),
            selectinload(InstallationProject.purchase_invoices),
        )
        .filter(
            InstallationProject.project_id == query.project_id,
            InstallationProject.is_active.is_(True),
        )
        .one_or_none()
    )
    if row is None:
        return None

    quote = _latest_quote(row)
    route = _latest_route(quote)
    as_built = _latest_as_built(row)
    invoice = _current_invoice(row)
    operations_visible = query.can_read_operations or query.can_read_financials

    return ProjectVendorDeliveryProjection(
        installation_project_id=row.id,
        vendor_name=getattr(row.assigned_vendor, "name", None) or "Unassigned",
        installation_status=installation_project_status_presentation(row.status),
        quote=(
            _quote_glance(quote, include_amount=query.can_read_financials)
            if operations_visible
            else None
        ),
        route=_route_glance(row, route) if query.can_read_routes else None,
        as_built=_as_built_glance(as_built) if query.can_read_operations else None,
        invoice=(_invoice_glance(row, invoice) if query.can_read_financials else None),
        operations_url=VENDOR_OPERATIONS_URL if operations_visible else None,
    )
