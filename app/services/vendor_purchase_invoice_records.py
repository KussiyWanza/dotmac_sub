"""Canonical participant writers for vendor purchase-invoice records."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceLineItem,
    VendorPurchaseInvoiceStatus,
)
from app.services.common import coerce_uuid
from app.services.events import EventType, emit_event
from app.services.file_storage import FileValidationError, file_uploads
from app.services.owner_commands import CommandContext
from app.services.vendor_purchase_invoices import (
    _REVIEWABLE,
    AddVendorPurchaseInvoiceLineCommand,
    CreateVendorPurchaseInvoiceCommand,
    DeleteVendorPurchaseInvoiceLineCommand,
    ReviewVendorPurchaseInvoiceCommand,
    StageVendorPurchaseInvoiceSubmission,
    UpdateVendorPurchaseInvoiceCommand,
    UpdateVendorPurchaseInvoiceLineCommand,
    UploadVendorPurchaseInvoiceAttachmentCommand,
    _assert_editable,
    _assert_vendor,
    _error,
    _get,
    _has_submitted_quote,
    _money,
    _project_for_vendor,
    _query,
    _recalculate,
    serialize,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _emit_change(
    db: Session,
    context: CommandContext,
    *,
    action: str,
    invoice: VendorPurchaseInvoice,
) -> None:
    emit_event(
        db,
        EventType.vendor_purchase_invoice_changed,
        {
            "schema_version": 1,
            "action": action,
            "invoice_id": str(invoice.id),
            "project_id": str(invoice.project_id),
            "vendor_id": str(invoice.vendor_id),
            "status": invoice.status,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
    )


def _lock_vendor(db: Session, vendor_id: str) -> Vendor:
    vendor = (
        db.query(Vendor)
        .filter(Vendor.id == coerce_uuid(vendor_id))
        .with_for_update(of=Vendor)
        .one_or_none()
    )
    if vendor is None or not vendor.is_active:
        raise _error("project_not_found", "Installation project not found.")
    return vendor


def _assert_invoice_number_available(
    db: Session,
    *,
    vendor_id: object,
    invoice_number: str | None,
    excluding_invoice_id: object | None = None,
) -> None:
    if not invoice_number:
        return
    query = (
        db.query(VendorPurchaseInvoice.id)
        .filter(VendorPurchaseInvoice.vendor_id == vendor_id)
        .filter(VendorPurchaseInvoice.invoice_number == invoice_number)
    )
    if excluding_invoice_id is not None:
        query = query.filter(VendorPurchaseInvoice.id != excluding_invoice_id)
    if query.first() is not None:
        raise _error(
            "invoice_number_conflict",
            "Invoice number is already used for this vendor.",
        )


def _line(
    invoice: VendorPurchaseInvoice, line_id: str
) -> VendorPurchaseInvoiceLineItem:
    row = next(
        (
            item
            for item in invoice.line_items
            if str(item.id) == str(line_id) and item.is_active
        ),
        None,
    )
    if row is None:
        raise _error("invoice_line_not_found", "Invoice line not found.")
    return row


def stage_create(db: Session, command: CreateVendorPurchaseInvoiceCommand) -> dict:
    if not command.resolved_currency:
        raise _error(
            "invalid_write_evidence",
            "Purchase-invoice currency evidence is required.",
        )
    vendor = _lock_vendor(db, command.vendor_id)
    project = _project_for_vendor(
        db,
        str(command.payload.project_id),
        command.vendor_id,
        for_update=True,
    )
    existing = (
        _query(db)
        .filter(VendorPurchaseInvoice.project_id == project.id)
        .filter(VendorPurchaseInvoice.vendor_id == vendor.id)
        .filter(VendorPurchaseInvoice.is_active.is_(True))
        .one_or_none()
    )
    if existing is not None:
        return serialize(existing)
    invoice_number = (command.payload.invoice_number or "").strip() or None
    _assert_invoice_number_available(
        db,
        vendor_id=vendor.id,
        invoice_number=invoice_number,
    )
    invoice = VendorPurchaseInvoice(
        project_id=project.id,
        vendor_id=vendor.id,
        invoice_number=invoice_number,
        currency=command.resolved_currency,
        tax_rate_percent=command.payload.tax_rate_percent,
        created_by_system_user_id=(
            coerce_uuid(command.created_by_system_user_id)
            if command.created_by_system_user_id
            else None
        ),
    )
    db.add(invoice)
    db.flush()
    _emit_change(db, command.context, action="created", invoice=invoice)
    return serialize(_get(db, str(invoice.id)))


def stage_update(db: Session, command: UpdateVendorPurchaseInvoiceCommand) -> dict:
    observed = _get(db, command.invoice_id)
    _assert_vendor(observed, command.vendor_id)
    _lock_vendor(db, str(observed.vendor_id))
    invoice = _get(db, command.invoice_id, for_update=True)
    _assert_vendor(invoice, command.vendor_id)
    _assert_editable(invoice)
    data = command.payload.model_dump(exclude_unset=True)
    if "invoice_number" in data:
        invoice_number = (data["invoice_number"] or "").strip() or None
        _assert_invoice_number_available(
            db,
            vendor_id=invoice.vendor_id,
            invoice_number=invoice_number,
            excluding_invoice_id=invoice.id,
        )
        invoice.invoice_number = invoice_number
    if data.get("currency") is not None:
        invoice.currency = str(data["currency"]).strip().upper()
    if data.get("tax_rate_percent") is not None:
        invoice.tax_rate_percent = data["tax_rate_percent"]
    _recalculate(invoice)
    db.flush()
    _emit_change(db, command.context, action="updated", invoice=invoice)
    return serialize(_get(db, command.invoice_id))


def stage_add_line(db: Session, command: AddVendorPurchaseInvoiceLineCommand) -> dict:
    invoice = _get(db, command.invoice_id, for_update=True)
    _assert_vendor(invoice, command.vendor_id)
    _assert_editable(invoice)
    line = VendorPurchaseInvoiceLineItem(
        invoice_id=invoice.id,
        item_type=(command.payload.item_type or "").strip() or None,
        description=command.payload.description.strip(),
        quantity=command.payload.quantity,
        unit_price=_money(command.payload.unit_price),
        amount=_money(command.payload.quantity * command.payload.unit_price),
        notes=(command.payload.notes or "").strip() or None,
        is_active=True,
    )
    invoice.line_items.append(line)
    _recalculate(invoice)
    db.flush()
    _emit_change(db, command.context, action="line_added", invoice=invoice)
    return serialize(_get(db, command.invoice_id))


def stage_update_line(
    db: Session, command: UpdateVendorPurchaseInvoiceLineCommand
) -> dict:
    invoice = _get(db, command.invoice_id, for_update=True)
    _assert_vendor(invoice, command.vendor_id)
    _assert_editable(invoice)
    line = _line(invoice, command.line_id)
    data = command.payload.model_dump(exclude_unset=True)
    if "item_type" in data:
        line.item_type = (data["item_type"] or "").strip() or None
    if data.get("description") is not None:
        line.description = str(data["description"]).strip()
    if data.get("quantity") is not None:
        line.quantity = data["quantity"]
    if data.get("unit_price") is not None:
        line.unit_price = _money(data["unit_price"])
    if "notes" in data:
        line.notes = (data["notes"] or "").strip() or None
    line.amount = _money(line.quantity * line.unit_price)
    _recalculate(invoice)
    db.flush()
    _emit_change(db, command.context, action="line_updated", invoice=invoice)
    return serialize(_get(db, command.invoice_id))


def stage_delete_line(
    db: Session, command: DeleteVendorPurchaseInvoiceLineCommand
) -> dict:
    invoice = _get(db, command.invoice_id, for_update=True)
    _assert_vendor(invoice, command.vendor_id)
    _assert_editable(invoice)
    line = _line(invoice, command.line_id)
    line.is_active = False
    _recalculate(invoice)
    db.flush()
    _emit_change(db, command.context, action="line_deleted", invoice=invoice)
    return serialize(_get(db, command.invoice_id))


def stage_upload_attachment(
    db: Session, command: UploadVendorPurchaseInvoiceAttachmentCommand
) -> dict:
    invoice = _get(db, command.invoice_id, for_update=True)
    _assert_vendor(invoice, command.vendor_id)
    _assert_editable(invoice)
    if not command.content:
        raise _error("empty_attachment", "Attachment is empty.")
    old_file = invoice.attachment
    try:
        stored = file_uploads.stage_upload(
            db=db,
            domain="attachments",
            entity_type="vendor_purchase_invoice",
            entity_id=str(invoice.id),
            original_filename=command.file_name or "invoice.pdf",
            content_type=command.content_type,
            data=command.content,
            uploaded_by=None,
            owner_subscriber_id=None,
        )
    except FileValidationError as exc:
        raise _error("invalid_attachment", str(exc)) from exc
    invoice.attachment_stored_file_id = stored.id
    if old_file is not None and old_file.id != stored.id:
        file_uploads.stage_soft_delete(db=db, file=old_file)
    db.flush()
    db.expire(invoice, ["attachment"])
    _emit_change(db, command.context, action="attachment_replaced", invoice=invoice)
    return serialize(_get(db, command.invoice_id))


def stage_submission(
    db: Session, command: StageVendorPurchaseInvoiceSubmission
) -> dict:
    invoice = _get(db, command.invoice_id, for_update=True)
    _assert_vendor(invoice, command.vendor_id)
    _assert_editable(invoice)
    if not (invoice.invoice_number or "").strip():
        raise _error("invoice_number_required", "Invoice number is required.")
    if not _has_submitted_quote(db, invoice.project_id, invoice.vendor_id):
        raise _error(
            "submitted_quote_required",
            "A submitted vendor quote is required before invoicing.",
        )
    if not any(item.is_active for item in invoice.line_items):
        raise _error(
            "invoice_line_required",
            "At least one active invoice line is required.",
        )
    _recalculate(invoice)
    invoice.status = VendorPurchaseInvoiceStatus.submitted.value
    invoice.submitted_at = _now()
    invoice.review_notes = None
    db.flush()
    _emit_change(db, command.context, action="submitted", invoice=invoice)
    return serialize(_get(db, command.invoice_id))


def _assert_within_quote_net_of_advances(
    db: Session, invoice: VendorPurchaseInvoice
) -> None:
    """Refuse an approval that would pay the same work twice.

    Advances leave Sub as money owed to the vendor but are never transmitted
    to ERP: the AP payload has no prepayment field, and ERP cannot park an
    on-account payment against a future invoice. ERP therefore bills the
    invoice gross, and an advance already taken would be paid a second time.

    Sub is the only place that knows both numbers, so the control lives here.
    It refuses rather than adjusting the total, because Sub never rewrites a
    vendor's stated invoice amount — the vendor reissues net of the advance.
    Only approved and settled advances count; a merely requested one
    authorises nothing.
    """

    from app.services import vendor_advances

    project = invoice.project
    if project is None or project.approved_quote_id is None:
        return
    quote = db.get(ProjectQuote, project.approved_quote_id)
    if quote is None:
        return

    quote_total = Decimal(str(quote.total or 0)).quantize(Decimal("0.01"))
    advances = vendor_advances.authorised_total(db, invoice.project_id)
    invoice_total = Decimal(str(invoice.total or 0)).quantize(Decimal("0.01"))
    if advances <= Decimal("0.00"):
        return
    if invoice_total + advances <= quote_total:
        return
    raise _error(
        "invoice_exceeds_quote_net_of_advances",
        (
            f"Invoice {invoice_total} plus advances already authorised "
            f"{advances} exceeds the approved quote {quote_total}. Advances "
            "are not netted downstream, so the invoice must be reissued net "
            "of them."
        ),
    )


def stage_review(db: Session, command: ReviewVendorPurchaseInvoiceCommand) -> dict:
    invoice = _get(db, command.invoice_id, for_update=True)
    (
        db.query(InstallationProject)
        .filter(InstallationProject.id == invoice.project_id)
        .with_for_update(of=InstallationProject)
        .one()
    )
    if invoice.status not in _REVIEWABLE:
        raise _error(
            "invoice_not_reviewable",
            "Only submitted invoices can be reviewed.",
        )
    invoice.reviewed_at = _now()
    invoice.reviewed_by_system_user_id = coerce_uuid(command.reviewer_system_user_id)
    invoice.review_notes = (command.review_notes or "").strip() or None
    action = "approved" if command.approve else "revision_requested"
    if command.approve:
        _recalculate(invoice)
        _assert_within_quote_net_of_advances(db, invoice)
        invoice.status = VendorPurchaseInvoiceStatus.approved.value
        invoice.procurement_order_reference = (
            invoice.project.procurement_order_reference
            or invoice.procurement_order_reference
        )
        db.flush()
        # The approval's committed output drives the receipted payables
        # export through the materials lifecycle projection handler; ERP
        # failure stays a durable pending delivery, never an inferred
        # payment.
        from app.services.events import EventType, emit_event

        emit_event(
            db,
            EventType.vendor_purchase_invoice_approved,
            {
                "invoice_id": str(invoice.id),
                "project_id": str(invoice.project_id),
                "vendor_id": str(invoice.vendor_id) if invoice.vendor_id else None,
                "invoice_number": invoice.invoice_number,
                "procurement_order_reference": invoice.procurement_order_reference,
            },
            actor="operations.vendor_purchase_invoice_records",
        )
    else:
        invoice.status = VendorPurchaseInvoiceStatus.revision_requested.value
    db.flush()
    _emit_change(db, command.context, action=action, invoice=invoice)
    return serialize(_get(db, command.invoice_id))
