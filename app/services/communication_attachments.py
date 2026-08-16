"""Resolve durable communication attachment references at delivery time."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoicePdfExportStatus, InvoiceStatus
from app.models.ncc_reporting import NccWeeklyReportRun, NccWeeklyReportRunStatus
from app.models.notification import Notification
from app.models.sales import QuotePdfExport
from app.services import billing_invoice_pdf
from app.services.communication_intents import (
    MAX_EMAIL_ATTACHMENT_BYTES,
    CommunicationAttachmentKind,
)
from app.services.sales import quote_documents

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class CommunicationAttachmentError(RuntimeError):
    """A required delivery attachment could not be safely materialized."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResolvedEmailAttachment:
    filename: str
    content_type: str
    content: bytes


def _safe_filename(value: object) -> str:
    filename = str(value or "invoice.pdf").replace("\\", "_").replace("/", "_")
    filename = _SAFE_FILENAME.sub("-", filename).strip("._-")
    return filename[:180] or "invoice.pdf"


def _resolve_invoice_pdf(
    db: Session, notification: Notification, descriptor: dict[str, object]
) -> ResolvedEmailAttachment:
    try:
        invoice_id = UUID(str(descriptor.get("entity_id") or ""))
    except ValueError as exc:
        raise CommunicationAttachmentError("invoice_pdf_invalid_reference") from exc

    invoice = db.get(Invoice, invoice_id)
    if invoice is None or not invoice.is_active:
        raise CommunicationAttachmentError("invoice_pdf_not_found")
    subject_subscriber_id = notification.subscriber_id
    if subject_subscriber_id is None:
        raw_subject_id = (notification.metadata_ or {}).get("subject_subscriber_id")
        try:
            subject_subscriber_id = UUID(str(raw_subject_id or ""))
        except ValueError as exc:
            raise CommunicationAttachmentError(
                "invoice_attachment_scope_missing"
            ) from exc
    if invoice.account_id != subject_subscriber_id:
        raise CommunicationAttachmentError("invoice_attachment_scope_mismatch")
    if invoice.is_proforma or invoice.status in {
        InvoiceStatus.draft,
        InvoiceStatus.void,
        InvoiceStatus.written_off,
    }:
        raise CommunicationAttachmentError("invoice_pdf_not_sendable")

    try:
        export = billing_invoice_pdf.generate_export_now(
            db, invoice_id=str(invoice.id), requested_by_id=None
        )
        if export.status != InvoicePdfExportStatus.completed:
            raise CommunicationAttachmentError("invoice_pdf_generation_failed")
        stream = billing_invoice_pdf.stream_export(db, export)
        content = b"".join(stream.chunks)
    except CommunicationAttachmentError:
        raise
    except Exception as exc:
        raise CommunicationAttachmentError("invoice_pdf_generation_failed") from exc

    if not content.startswith(b"%PDF-"):
        raise CommunicationAttachmentError("invoice_pdf_invalid_content")
    if len(content) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise CommunicationAttachmentError("invoice_pdf_too_large")
    return ResolvedEmailAttachment(
        filename=_safe_filename(
            descriptor.get("filename") or billing_invoice_pdf.download_filename(invoice)
        ),
        content_type="application/pdf",
        content=content,
    )


def _resolve_quote_pdf(
    db: Session, notification: Notification, descriptor: dict[str, object]
) -> ResolvedEmailAttachment:
    try:
        export_id = UUID(str(descriptor.get("entity_id") or ""))
    except ValueError as exc:
        raise CommunicationAttachmentError("quote_pdf_invalid_reference") from exc
    export = db.get(QuotePdfExport, export_id)
    if export is None:
        raise CommunicationAttachmentError("quote_pdf_not_found")
    expected_quote_id = str((notification.metadata_ or {}).get("quote_id") or "")
    if expected_quote_id != str(export.quote_id):
        raise CommunicationAttachmentError("quote_attachment_scope_mismatch")
    try:
        stream = quote_documents.stream_export(db, export)
        content = b"".join(stream.chunks)
    except Exception as exc:
        raise CommunicationAttachmentError("quote_pdf_generation_failed") from exc
    if not content.startswith(b"%PDF-"):
        raise CommunicationAttachmentError("quote_pdf_invalid_content")
    if len(content) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise CommunicationAttachmentError("quote_pdf_too_large")
    return ResolvedEmailAttachment(
        filename=_safe_filename(
            descriptor.get("filename") or quote_documents.download_filename(export)
        ),
        content_type="application/pdf",
        content=content,
    )


def _resolve_ncc_weekly_xlsx(
    db: Session, notification: Notification, descriptor: dict[str, object]
) -> ResolvedEmailAttachment:
    try:
        run_id = UUID(str(descriptor.get("entity_id") or ""))
    except ValueError as exc:
        raise CommunicationAttachmentError("ncc_xlsx_invalid_reference") from exc
    run = db.get(NccWeeklyReportRun, run_id)
    expected_run_id = str(
        (notification.metadata_ or {}).get("ncc_weekly_report_run_id") or ""
    )
    if run is None or run.status is not NccWeeklyReportRunStatus.queued:
        raise CommunicationAttachmentError("ncc_xlsx_not_found")
    if expected_run_id != str(run.id) or run.notification_id != notification.id:
        raise CommunicationAttachmentError("ncc_xlsx_scope_mismatch")
    content = run.artifact_content or b""
    if not content.startswith(b"PK\x03\x04"):
        raise CommunicationAttachmentError("ncc_xlsx_invalid_content")
    if hashlib.sha256(content).hexdigest() != run.artifact_sha256:
        raise CommunicationAttachmentError("ncc_xlsx_integrity_failed")
    if len(content) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise CommunicationAttachmentError("ncc_xlsx_too_large")
    return ResolvedEmailAttachment(
        filename=_safe_filename(descriptor.get("filename") or run.artifact_filename),
        content_type=(
            run.artifact_content_type
            or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        content=content,
    )


def resolve_email_attachments(
    db: Session, notification: Notification
) -> tuple[ResolvedEmailAttachment, ...]:
    raw = (notification.metadata_ or {}).get("attachments", [])
    if not isinstance(raw, list):
        raise CommunicationAttachmentError("invalid_attachment_contract")
    resolved: list[ResolvedEmailAttachment] = []
    for item in raw:
        if not isinstance(item, dict):
            raise CommunicationAttachmentError("invalid_attachment_contract")
        try:
            kind = CommunicationAttachmentKind(str(item.get("kind") or ""))
        except ValueError as exc:
            raise CommunicationAttachmentError("unsupported_attachment_kind") from exc
        if kind == CommunicationAttachmentKind.invoice_pdf:
            resolved.append(_resolve_invoice_pdf(db, notification, item))
        elif kind == CommunicationAttachmentKind.quote_pdf:
            resolved.append(_resolve_quote_pdf(db, notification, item))
        elif kind == CommunicationAttachmentKind.ncc_weekly_xlsx:
            resolved.append(_resolve_ncc_weekly_xlsx(db, notification, item))
    return tuple(resolved)
