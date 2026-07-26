"""Vendor purchase-invoice policy, reads, and command coordination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import TypeVar

from sqlalchemy.orm import Session, selectinload

from app.models.domain_settings import SettingDomain
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProjectQuoteStatus,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceStatus,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
    VendorPurchaseInvoiceLineUpdate,
    VendorPurchaseInvoiceUpdate,
)
from app.services.common import apply_pagination, coerce_uuid
from app.services.domain_errors import DomainError
from app.services.file_storage import file_uploads
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value
from app.services.ui_contracts import Action
from app.services.vendor_payment_status import project_vendor_payment_status

_MONEY = Decimal("0.01")
_EDITABLE = {
    VendorPurchaseInvoiceStatus.draft.value,
    VendorPurchaseInvoiceStatus.revision_requested.value,
}
_REVIEWABLE = {
    VendorPurchaseInvoiceStatus.submitted.value,
    VendorPurchaseInvoiceStatus.under_review.value,
}
ResultT = TypeVar("ResultT")


class VendorPurchaseInvoiceError(DomainError):
    """Stable failures from purchase-invoice policy and record boundaries."""


def _error(suffix: str, message: str) -> VendorPurchaseInvoiceError:
    return VendorPurchaseInvoiceError(
        code=f"operations.vendor_purchase_invoices.{suffix}",
        message=message,
    )


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(
        owner="operations.vendor_purchase_invoices",
        concern="vendor purchase-invoice mutation coordination",
        name=name,
    )


def _execute(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    operation: Callable[[], ResultT],
) -> ResultT:
    return execute_owner_command(
        db,
        definition=_definition(name),
        context=context,
        operation=operation,
    )


@dataclass(frozen=True, slots=True)
class CreateVendorPurchaseInvoiceCommand:
    context: CommandContext
    payload: VendorPurchaseInvoiceCreate
    vendor_id: str
    created_by_system_user_id: str | None
    resolved_currency: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateVendorPurchaseInvoiceCommand:
    context: CommandContext
    invoice_id: str
    payload: VendorPurchaseInvoiceUpdate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class AddVendorPurchaseInvoiceLineCommand:
    context: CommandContext
    invoice_id: str
    payload: VendorPurchaseInvoiceLineCreate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class UpdateVendorPurchaseInvoiceLineCommand:
    context: CommandContext
    invoice_id: str
    line_id: str
    payload: VendorPurchaseInvoiceLineUpdate
    vendor_id: str


@dataclass(frozen=True, slots=True)
class DeleteVendorPurchaseInvoiceLineCommand:
    context: CommandContext
    invoice_id: str
    line_id: str
    vendor_id: str


@dataclass(frozen=True, slots=True)
class UploadVendorPurchaseInvoiceAttachmentCommand:
    context: CommandContext
    invoice_id: str
    vendor_id: str
    file_name: str
    content_type: str | None
    content: bytes


@dataclass(frozen=True, slots=True)
class ReviewVendorPurchaseInvoiceCommand:
    context: CommandContext
    invoice_id: str
    reviewer_system_user_id: str
    approve: bool
    review_notes: str | None


@dataclass(frozen=True, slots=True)
class StageVendorPurchaseInvoiceSubmission:
    context: CommandContext
    invoice_id: str
    vendor_id: str


def _money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(str(value or "0")).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _query(db: Session):
    return db.query(VendorPurchaseInvoice).options(
        selectinload(VendorPurchaseInvoice.line_items),
        selectinload(VendorPurchaseInvoice.attachment),
        selectinload(VendorPurchaseInvoice.project).selectinload(
            InstallationProject.project
        ),
        selectinload(VendorPurchaseInvoice.vendor),
    )


def _get(
    db: Session, invoice_id: str, *, for_update: bool = False
) -> VendorPurchaseInvoice:
    query = (
        _query(db)
        .filter(VendorPurchaseInvoice.id == coerce_uuid(invoice_id))
        .filter(VendorPurchaseInvoice.is_active.is_(True))
    )
    if for_update:
        query = query.with_for_update(of=VendorPurchaseInvoice)
    invoice = query.one_or_none()
    if invoice is None:
        raise _error("invoice_not_found", "Purchase invoice not found.")
    return invoice


def _assert_vendor(invoice: VendorPurchaseInvoice, vendor_id: str | None) -> None:
    if vendor_id and str(invoice.vendor_id) != str(vendor_id):
        raise _error("invoice_not_found", "Purchase invoice not found.")


def _assert_editable(invoice: VendorPurchaseInvoice) -> None:
    if invoice.status not in _EDITABLE:
        raise _error(
            "invoice_not_editable",
            "Only draft or revision-requested invoices can be edited.",
        )


def _project_for_vendor(
    db: Session,
    project_id: str,
    vendor_id: str,
    *,
    for_update: bool = False,
) -> InstallationProject:
    query = db.query(InstallationProject).filter(
        InstallationProject.id == coerce_uuid(project_id)
    )
    if for_update:
        query = query.with_for_update(of=InstallationProject)
    project = query.one_or_none()
    if project is None or not project.is_active:
        raise _error("project_not_found", "Installation project not found.")
    has_quote = (
        db.query(ProjectQuote.id)
        .filter(ProjectQuote.project_id == project.id)
        .filter(ProjectQuote.vendor_id == coerce_uuid(vendor_id))
        .filter(ProjectQuote.is_active.is_(True))
        .first()
        is not None
    )
    if str(project.assigned_vendor_id or "") != str(vendor_id) and not has_quote:
        raise _error("project_not_found", "Installation project not found.")
    return project


def _has_submitted_quote(db: Session, project_id: object, vendor_id: object) -> bool:
    return (
        db.query(ProjectQuote.id)
        .filter(ProjectQuote.project_id == project_id)
        .filter(ProjectQuote.vendor_id == vendor_id)
        .filter(ProjectQuote.is_active.is_(True))
        .filter(
            ProjectQuote.status.in_(
                (
                    ProjectQuoteStatus.submitted.value,
                    ProjectQuoteStatus.under_review.value,
                    ProjectQuoteStatus.approved.value,
                )
            )
        )
        .first()
        is not None
    )


def _recalculate(invoice: VendorPurchaseInvoice) -> None:
    subtotal = sum(
        (_money(item.amount) for item in invoice.line_items if item.is_active),
        Decimal("0.00"),
    )
    rate = Decimal(str(invoice.tax_rate_percent or "0"))
    tax_total = _money(subtotal * rate / Decimal("100"))
    invoice.subtotal = _money(subtotal)
    invoice.tax_total = tax_total
    invoice.total = _money(subtotal + tax_total)


def serialize(invoice: VendorPurchaseInvoice) -> dict:
    attachment = invoice.attachment
    editable = invoice.status in _EDITABLE
    reviewable = invoice.status in _REVIEWABLE
    project = getattr(invoice, "project", None)
    native_project = getattr(project, "project", None)
    vendor = getattr(invoice, "vendor", None)
    return {
        "id": invoice.id,
        "project_id": invoice.project_id,
        "project_name": getattr(native_project, "name", None),
        "project_code": getattr(native_project, "code", None),
        "vendor_id": invoice.vendor_id,
        "vendor_name": getattr(vendor, "name", None),
        "invoice_number": invoice.invoice_number,
        "status": invoice.status,
        "edit_action": Action(
            key="edit",
            label="Edit invoice",
            allowed=editable,
            reason=(
                None
                if editable
                else f"A {invoice.status.replace('_', ' ')} invoice cannot be edited"
            ),
        ),
        "approve_action": Action(
            key="approve_purchase_invoice",
            label="Approve invoice",
            allowed=reviewable,
            reason=(
                None
                if reviewable
                else (f"A {invoice.status.replace('_', ' ')} invoice is not reviewable")
            ),
            affected=1,
        ),
        "revision_action": Action(
            key="request_purchase_invoice_revision",
            label="Request revision",
            allowed=reviewable,
            reason=(
                None
                if reviewable
                else (f"A {invoice.status.replace('_', ' ')} invoice is not reviewable")
            ),
            affected=1,
        ),
        "approve_url": (f"/admin/vendors/operations/invoices/{invoice.id}/approve"),
        "revision_url": (
            f"/admin/vendors/operations/invoices/{invoice.id}/request-revision"
        ),
        "currency": invoice.currency,
        "tax_rate_percent": invoice.tax_rate_percent,
        "subtotal": invoice.subtotal,
        "tax_total": invoice.tax_total,
        "total": invoice.total,
        "submitted_at": invoice.submitted_at,
        "reviewed_at": invoice.reviewed_at,
        "reviewed_by_system_user_id": invoice.reviewed_by_system_user_id,
        "review_notes": invoice.review_notes,
        "created_by_system_user_id": invoice.created_by_system_user_id,
        "attachment_stored_file_id": invoice.attachment_stored_file_id,
        "attachment_file_name": attachment.original_filename if attachment else None,
        "attachment_content_type": attachment.content_type if attachment else None,
        "attachment_file_size": attachment.file_size if attachment else None,
        "payables_system": invoice.payables_system,
        "procurement_order_reference": invoice.procurement_order_reference,
        "payables_document_reference": invoice.payables_document_reference,
        "payables_document_status": getattr(invoice, "payables_document_status", None),
        "payment_status": invoice.payment_status,
        "payment_total_amount": getattr(invoice, "payment_total_amount", None),
        "payment_amount_paid": getattr(invoice, "payment_amount_paid", None),
        "payment_balance_due": getattr(invoice, "payment_balance_due", None),
        "payment_observed_at": getattr(invoice, "payment_observed_at", None),
        "payment_source_updated_at": getattr(
            invoice, "payment_source_updated_at", None
        ),
        "payment_observation_error": getattr(
            invoice, "payment_observation_error", None
        ),
        "payment": project_vendor_payment_status(invoice),
        "payables_submission_error": invoice.payables_submission_error,
        "payables_submitted_at": invoice.payables_submitted_at,
        "payables_attachment_submitted_at": invoice.payables_attachment_submitted_at,
        "is_active": invoice.is_active,
        "created_at": invoice.created_at,
        "updated_at": invoice.updated_at,
        "line_items": [item for item in invoice.line_items if item.is_active],
    }


class VendorPurchaseInvoices:
    @staticmethod
    def for_project(db: Session, project_id: str, *, vendor_id: str) -> dict | None:
        rows = VendorPurchaseInvoices.list(
            db,
            vendor_id=vendor_id,
            project_id=project_id,
            limit=1,
            offset=0,
        )
        return rows[0] if rows else None

    @staticmethod
    def list(
        db: Session,
        *,
        vendor_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        query = _query(db).filter(VendorPurchaseInvoice.is_active.is_(True))
        if vendor_id:
            query = query.filter(
                VendorPurchaseInvoice.vendor_id == coerce_uuid(vendor_id)
            )
        if project_id:
            query = query.filter(
                VendorPurchaseInvoice.project_id == coerce_uuid(project_id)
            )
        if status:
            query = query.filter(VendorPurchaseInvoice.status == status.strip())
        rows = apply_pagination(
            query.order_by(VendorPurchaseInvoice.created_at.desc()), limit, offset
        ).all()
        return [serialize(row) for row in rows]

    @staticmethod
    def get(db: Session, invoice_id: str, *, vendor_id: str | None = None) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        return serialize(invoice)

    @staticmethod
    def preview_submission(
        db: Session,
        invoice_id: str,
        *,
        vendor_id: str,
        for_update: bool = False,
    ) -> dict:
        """Own the read-only impact snapshot for a purchase-invoice submit."""
        invoice = _get(db, invoice_id, for_update=for_update)
        _assert_vendor(invoice, vendor_id)
        _assert_editable(invoice)
        if not (invoice.invoice_number or "").strip():
            raise _error("invoice_number_required", "Invoice number is required.")
        submitted_quote_query = (
            db.query(ProjectQuote)
            .filter(ProjectQuote.project_id == invoice.project_id)
            .filter(ProjectQuote.vendor_id == invoice.vendor_id)
            .filter(ProjectQuote.is_active.is_(True))
            .filter(
                ProjectQuote.status.in_(
                    (
                        ProjectQuoteStatus.submitted.value,
                        ProjectQuoteStatus.under_review.value,
                        ProjectQuoteStatus.approved.value,
                    )
                )
            )
            .order_by(ProjectQuote.id.asc())
        )
        if for_update:
            submitted_quote_query = submitted_quote_query.with_for_update(
                of=ProjectQuote
            )
        submitted_quotes = submitted_quote_query.all()
        if not submitted_quotes:
            raise _error(
                "submitted_quote_required",
                "A submitted vendor quote is required before invoicing.",
            )
        active = [item for item in invoice.line_items if item.is_active]
        if not active:
            raise _error(
                "invoice_line_required",
                "At least one active invoice line is required.",
            )
        subtotal = sum((_money(item.amount) for item in active), Decimal("0.00"))
        tax_total = _money(
            subtotal * Decimal(str(invoice.tax_rate_percent or "0")) / Decimal("100")
        )
        total = _money(subtotal + tax_total)
        return {
            "submission_type": "purchase_invoice",
            "project_id": str(invoice.project_id),
            "target_id": str(invoice.id),
            "title": "Submit purchase invoice for review",
            "summary": (
                f"{invoice.currency} {total:,.2f} from {len(active)} line "
                f"item{'s' if len(active) != 1 else ''}"
            ),
            "details": [
                ("Invoice number", invoice.invoice_number),
                ("Line items", str(len(active))),
                ("Subtotal", f"{invoice.currency} {subtotal:,.2f}"),
                ("Tax", f"{invoice.currency} {tax_total:,.2f}"),
                ("Total", f"{invoice.currency} {total:,.2f}"),
                ("Result", "Invoice becomes read-only and enters staff review"),
            ],
            "state": {
                "invoice_id": str(invoice.id),
                "project_id": str(invoice.project_id),
                "status": invoice.status,
                "invoice_number": invoice.invoice_number,
                "currency": invoice.currency,
                "tax_rate_percent": invoice.tax_rate_percent,
                "attachment_stored_file_id": (
                    str(invoice.attachment_stored_file_id)
                    if invoice.attachment_stored_file_id
                    else None
                ),
                "updated_at": invoice.updated_at,
                "eligible_quotes": [
                    {
                        "id": str(quote.id),
                        "status": quote.status,
                        "updated_at": quote.updated_at,
                    }
                    for quote in submitted_quotes
                ],
                "lines": [
                    {
                        "id": str(item.id),
                        "description": item.description,
                        "quantity": item.quantity,
                        "unit_price": item.unit_price,
                        "amount": item.amount,
                        "updated_at": item.updated_at,
                    }
                    for item in sorted(active, key=lambda row: str(row.id))
                ],
            },
        }

    @staticmethod
    def create(db: Session, command: CreateVendorPurchaseInvoiceCommand) -> dict:
        def operation() -> dict:
            from app.services import vendor_purchase_invoice_records

            currency = command.payload.currency or str(
                resolve_value(db, SettingDomain.billing, "default_currency")
            )
            return vendor_purchase_invoice_records.stage_create(
                db,
                replace(command, resolved_currency=currency.strip().upper()),
            )

        return _execute(
            db,
            context=command.context,
            name="create_vendor_purchase_invoice",
            operation=operation,
        )

    @staticmethod
    def update(db: Session, command: UpdateVendorPurchaseInvoiceCommand) -> dict:
        from app.services import vendor_purchase_invoice_records

        return _execute(
            db,
            context=command.context,
            name="update_vendor_purchase_invoice",
            operation=lambda: vendor_purchase_invoice_records.stage_update(db, command),
        )

    @staticmethod
    def add_line(db: Session, command: AddVendorPurchaseInvoiceLineCommand) -> dict:
        from app.services import vendor_purchase_invoice_records

        return _execute(
            db,
            context=command.context,
            name="add_vendor_purchase_invoice_line",
            operation=lambda: vendor_purchase_invoice_records.stage_add_line(
                db, command
            ),
        )

    @staticmethod
    def update_line(
        db: Session, command: UpdateVendorPurchaseInvoiceLineCommand
    ) -> dict:
        from app.services import vendor_purchase_invoice_records

        return _execute(
            db,
            context=command.context,
            name="update_vendor_purchase_invoice_line",
            operation=lambda: vendor_purchase_invoice_records.stage_update_line(
                db, command
            ),
        )

    @staticmethod
    def delete_line(
        db: Session, command: DeleteVendorPurchaseInvoiceLineCommand
    ) -> dict:
        from app.services import vendor_purchase_invoice_records

        return _execute(
            db,
            context=command.context,
            name="delete_vendor_purchase_invoice_line",
            operation=lambda: vendor_purchase_invoice_records.stage_delete_line(
                db, command
            ),
        )

    @staticmethod
    def upload_attachment(
        db: Session, command: UploadVendorPurchaseInvoiceAttachmentCommand
    ) -> dict:
        from app.services import vendor_purchase_invoice_records

        return _execute(
            db,
            context=command.context,
            name="upload_vendor_purchase_invoice_attachment",
            operation=lambda: vendor_purchase_invoice_records.stage_upload_attachment(
                db, command
            ),
        )

    @staticmethod
    def review(db: Session, command: ReviewVendorPurchaseInvoiceCommand) -> dict:
        from app.services import vendor_purchase_invoice_records

        return _execute(
            db,
            context=command.context,
            name="review_vendor_purchase_invoice",
            operation=lambda: vendor_purchase_invoice_records.stage_review(db, command),
        )

    @staticmethod
    def stage_submission(
        db: Session, command: StageVendorPurchaseInvoiceSubmission
    ) -> dict:
        """Participate in the signed-confirmation coordinator transaction."""
        from app.services import vendor_purchase_invoice_records

        return vendor_purchase_invoice_records.stage_submission(db, command)

    @staticmethod
    def attachment_file(db: Session, invoice_id: str, *, vendor_id: str | None):
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        file = invoice.attachment
        if file is None or file.is_deleted:
            raise _error("attachment_not_found", "Attachment not found.")
        return file, file_uploads.stream_file(file)


vendor_purchase_invoices = VendorPurchaseInvoices()
