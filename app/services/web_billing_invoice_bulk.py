"""Service helpers for bulk invoice web actions."""

from __future__ import annotations

import hmac
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus, PaymentStatus
from app.schemas.billing import (
    InvoiceClosureConfirm,
    PaymentAllocationApply,
    PaymentCreate,
)
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services.audit_adapter import record_audit_event
from app.services.billing.invoices import InvoiceClosurePreview, verified_draft_issuance
from app.services.bulk_actions import membership_scope_token
from app.services.object_storage import ObjectNotFoundError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BulkInvoiceActionResult:
    """Outcome for a bulk invoice action."""

    selected_ids: list[str]
    processed_ids: list[str] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)

    @property
    def selected(self) -> int:
        return len(self.selected_ids)

    @property
    def processed(self) -> int:
        return len(self.processed_ids)

    @property
    def skipped(self) -> int:
        return len(self.skipped_ids)

    @property
    def failed(self) -> int:
        return len(self.failed_ids)

    def message(self, verb: str, noun: str = "invoice") -> str:
        noun_text = noun if self.selected == 1 else f"{noun}s"
        message = f"{verb} {self.processed} of {self.selected} selected {noun_text}"
        details = []
        if self.skipped:
            details.append(f"{self.skipped} skipped")
        if self.failed:
            details.append(f"{self.failed} failed")
        if details:
            message += f"; {', '.join(details)}"
        return message

    def as_response(self, verb: str) -> dict[str, object]:
        return {
            "message": self.message(verb),
            "count": self.processed,
            "selected": self.selected,
            "skipped": self.skipped,
            "failed": self.failed,
        }


@dataclass(frozen=True, slots=True)
class InvoiceBulkActionPreview:
    """Server-owned eligibility and membership snapshot for one action."""

    action: str
    selected_ids: tuple[str, ...]
    resolved_ids: tuple[str, ...]
    eligible_ids: tuple[str, ...]
    skipped: tuple[dict[str, str], ...]

    @property
    def scope_token(self) -> str:
        eligible = set(self.eligible_ids)
        skipped_reasons = {item["id"]: item["reason"] for item in self.skipped}
        outcomes = [
            (
                f"{invoice_id}:eligible"
                if invoice_id in eligible
                else f"{invoice_id}:skipped:{skipped_reasons.get(invoice_id, 'unknown')}"
            )
            for invoice_id in self.selected_ids
        ]
        return membership_scope_token(f"selected:{self.action}", outcomes)

    def as_response(self) -> dict[str, object]:
        return {
            "action": self.action,
            "selected_count": len(self.selected_ids),
            "matched_count": len(self.resolved_ids),
            "eligible_count": len(self.eligible_ids),
            "skipped_count": len(self.skipped),
            "eligible_ids": list(self.eligible_ids),
            "skipped": list(self.skipped),
            "scope_token": self.scope_token,
        }


def parse_ids_csv(ids_csv: str) -> list[str]:
    """Parse comma-separated IDs into a cleaned list."""
    normalized: list[str] = []
    seen: set[str] = set()
    for item in ids_csv.split(","):
        invoice_id = item.strip()
        if not invoice_id or invoice_id in seen:
            continue
        seen.add(invoice_id)
        normalized.append(invoice_id)
    return normalized


def invoice_bulk_action_ineligibility(invoice, action: str) -> str | None:
    """Return the canonical reason an invoice cannot receive an action."""

    if action == "issue":
        return None if invoice.status == InvoiceStatus.draft else "Already issued"
    if action == "send":
        if invoice.status in {
            InvoiceStatus.draft,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        }:
            return "Only issued invoices can be sent"
        account = getattr(invoice, "account", None)
        if not account or not getattr(account, "email", None):
            return "Customer has no email address"
        return None
    if action == "void":
        if invoice.status in {
            InvoiceStatus.paid,
            InvoiceStatus.void,
            InvoiceStatus.written_off,
        }:
            return "Paid or closed invoices cannot be voided"
        return None
    if action == "mark_paid":
        if invoice.status not in {
            InvoiceStatus.issued,
            InvoiceStatus.overdue,
            InvoiceStatus.partially_paid,
        }:
            return "Invoice is not open for payment"
        if (invoice.balance_due or Decimal("0")) <= 0:
            return "Invoice has no outstanding balance"
        return None
    if action in {"export_csv", "export_pdf", "generate_pdf"}:
        return None
    raise ValueError("Unsupported invoice bulk action")


def preview_invoice_bulk_action(
    db, *, action: str, invoice_ids_csv: str
) -> InvoiceBulkActionPreview:
    """Resolve exact membership and action eligibility without side effects."""

    selected_ids = tuple(parse_ids_csv(invoice_ids_csv))
    if not selected_ids:
        raise ValueError("Select at least one invoice before using a bulk action")
    resolved_ids: list[str] = []
    eligible_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    for invoice_id in selected_ids:
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
        except HTTPException as exc:
            if exc.status_code >= 500:
                raise
            invoice = None
        except (TypeError, ValueError):
            # UI selections contain UUIDs, but the adapter remains a public
            # trust boundary. Treat malformed identifiers like missing rows
            # instead of allowing UUID coercion to surface as a 500 response.
            invoice = None
        if not invoice:
            skipped.append({"id": invoice_id, "reason": "Invoice not found"})
            continue
        resolved_id = str(invoice.id)
        resolved_ids.append(resolved_id)
        reason = invoice_bulk_action_ineligibility(invoice, action)
        if reason:
            skipped.append({"id": resolved_id, "reason": reason})
        else:
            eligible_ids.append(resolved_id)
    return InvoiceBulkActionPreview(
        action=action,
        selected_ids=selected_ids,
        resolved_ids=tuple(resolved_ids),
        eligible_ids=tuple(eligible_ids),
        skipped=tuple(skipped),
    )


def require_invoice_bulk_confirmation(
    db,
    *,
    action: str,
    invoice_ids_csv: str,
    expected_count: int | None,
    expected_scope_token: str | None,
) -> InvoiceBulkActionPreview:
    """Reject execution when membership changed after the server preview."""

    if expected_count is None or not expected_scope_token:
        raise ValueError("Preview the invoice action before confirming")
    preview = preview_invoice_bulk_action(
        db, action=action, invoice_ids_csv=invoice_ids_csv
    )
    scope_changed = expected_count != len(
        preview.resolved_ids
    ) or not hmac.compare_digest(expected_scope_token, preview.scope_token)
    if scope_changed:
        raise HTTPException(
            status_code=409,
            detail=(
                "The selected invoice scope changed after preview. "
                "Review the updated impact before confirming again."
            ),
        )
    return preview


def list_invoices_by_ids(db, invoice_ids_csv: str):
    """Return invoices for the provided IDs, preserving input order."""
    invoices = []
    seen: set[str] = set()
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        if invoice_id in seen:
            continue
        seen.add(invoice_id)
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice:
                invoices.append(invoice)
        except Exception:
            logger.debug(
                "Skipping invoice %s while loading bulk list", invoice_id, exc_info=True
            )
            continue
    return invoices


def bulk_issue(db, invoice_ids_csv: str) -> list[str]:
    """Issue draft invoices; return IDs that were updated."""
    updated: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice_bulk_action_ineligibility(invoice, "issue") is None:
                billing_service.invoices.issue_draft_system(
                    db,
                    invoice_id,
                    issuance=verified_draft_issuance(
                        invoice,
                        issued_at=datetime.now(UTC),
                        reason="admin_bulk_issue",
                    ),
                    announce=True,
                    commit=True,
                )
                updated.append(invoice_id)
        except Exception:
            logger.debug(
                "Skipping invoice %s during bulk issue", invoice_id, exc_info=True
            )
            continue
    return updated


def bulk_issue_result(db, invoice_ids_csv: str) -> BulkInvoiceActionResult:
    """Issue draft invoices and report processed/skipped/failed counts."""
    result = BulkInvoiceActionResult(selected_ids=parse_ids_csv(invoice_ids_csv))
    for invoice_id in result.selected_ids:
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice_bulk_action_ineligibility(invoice, "issue") is None:
                billing_service.invoices.issue_draft_system(
                    db,
                    invoice_id,
                    issuance=verified_draft_issuance(
                        invoice,
                        issued_at=datetime.now(UTC),
                        reason="admin_bulk_issue",
                    ),
                    announce=True,
                    commit=True,
                )
                result.processed_ids.append(invoice_id)
            else:
                result.skipped_ids.append(invoice_id)
        except HTTPException as exc:
            if exc.status_code < 500:
                result.skipped_ids.append(invoice_id)
                continue
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk issue", invoice_id, exc_info=True
            )
            result.failed_ids.append(invoice_id)
        except Exception:
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk issue", invoice_id, exc_info=True
            )
            result.failed_ids.append(invoice_id)
    return result


def bulk_send(db, invoice_ids_csv: str) -> list[str]:
    """Send invoice notifications for eligible invoices."""
    queued: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice_bulk_action_ineligibility(invoice, "send") is None:
                web_billing_invoices_service.maybe_send_invoice_notification(
                    db,
                    invoice=invoice,
                    send_notification="1",
                )
                queued.append(invoice_id)
        except Exception:
            logger.debug(
                "Skipping invoice %s during bulk send", invoice_id, exc_info=True
            )
            continue
    return queued


def bulk_send_result(db, invoice_ids_csv: str) -> BulkInvoiceActionResult:
    """Send invoice notifications and report processed/skipped/failed counts."""
    result = BulkInvoiceActionResult(selected_ids=parse_ids_csv(invoice_ids_csv))
    for invoice_id in result.selected_ids:
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice_bulk_action_ineligibility(invoice, "send") is None:
                web_billing_invoices_service.maybe_send_invoice_notification(
                    db,
                    invoice=invoice,
                    send_notification="1",
                )
                result.processed_ids.append(invoice_id)
            else:
                result.skipped_ids.append(invoice_id)
        except HTTPException as exc:
            if exc.status_code < 500:
                result.skipped_ids.append(invoice_id)
                continue
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk send", invoice_id, exc_info=True
            )
            result.failed_ids.append(invoice_id)
        except Exception:
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk send", invoice_id, exc_info=True
            )
            result.failed_ids.append(invoice_id)
    return result


def bulk_void(db, invoice_ids_csv: str) -> list[str]:
    """Void eligible invoices; return IDs that were updated."""
    updated: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice_bulk_action_ineligibility(invoice, "void") is None:
                # Use the canonical void so debit ledger entries are reversed
                # (previously bulk void only flipped the status, leaving the AR
                # ledger out of sync).
                billing_service.invoices.void_system(
                    db,
                    invoice_id,
                    reason="Admin bulk invoice void",
                    idempotency_key=f"admin-bulk-invoice-void-{invoice_id}",
                )
                updated.append(invoice_id)
        except Exception:
            logger.debug(
                "Skipping invoice %s during bulk void", invoice_id, exc_info=True
            )
            continue
    return updated


def bulk_void_result(db, invoice_ids_csv: str) -> BulkInvoiceActionResult:
    """Void eligible invoices and report processed/skipped/failed counts."""
    result = BulkInvoiceActionResult(selected_ids=parse_ids_csv(invoice_ids_csv))
    for invoice_id in result.selected_ids:
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice_bulk_action_ineligibility(invoice, "void") is None:
                billing_service.invoices.void_system(
                    db,
                    invoice_id,
                    reason="Admin bulk invoice void",
                    idempotency_key=f"admin-bulk-invoice-void-{invoice_id}",
                )
                result.processed_ids.append(invoice_id)
            else:
                result.skipped_ids.append(invoice_id)
        except HTTPException as exc:
            if exc.status_code < 500:
                result.skipped_ids.append(invoice_id)
                continue
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk void", invoice_id, exc_info=True
            )
            result.failed_ids.append(invoice_id)
        except Exception:
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk void", invoice_id, exc_info=True
            )
            result.failed_ids.append(invoice_id)
    return result


def preview_bulk_void(
    db, invoice_ids_csv: str
) -> tuple[list[InvoiceClosurePreview], list[str]]:
    """Return owner previews and explicitly skipped ids for an admin batch."""
    previews: list[InvoiceClosurePreview] = []
    skipped: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            previews.append(billing_service.invoices.preview_void(db, invoice_id))
        except HTTPException as exc:
            if exc.status_code < 500:
                skipped.append(invoice_id)
                continue
            raise
    return previews, skipped


def confirm_bulk_void_result(
    db,
    *,
    invoice_ids_csv: str,
    preview_fingerprints_json: str,
    batch_key: str,
) -> BulkInvoiceActionResult:
    """Confirm exactly the per-invoice owner previews shown to the operator."""
    try:
        raw = json.loads(preview_fingerprints_json)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="Invalid bulk void preview"
        ) from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Invalid bulk void preview")
    fingerprints = {str(key): str(value) for key, value in raw.items()}
    result = BulkInvoiceActionResult(selected_ids=parse_ids_csv(invoice_ids_csv))
    for invoice_id in result.selected_ids:
        fingerprint = fingerprints.get(invoice_id)
        if not fingerprint:
            result.skipped_ids.append(invoice_id)
            continue
        try:
            billing_service.invoices.confirm_void(
                db,
                invoice_id,
                InvoiceClosureConfirm(
                    preview_fingerprint=fingerprint,
                    idempotency_key=f"{batch_key}.{invoice_id}",
                    memo="Admin bulk invoice void",
                ),
            )
            result.processed_ids.append(invoice_id)
        except HTTPException as exc:
            if exc.status_code < 500:
                result.skipped_ids.append(invoice_id)
                continue
            db.rollback()
            result.failed_ids.append(invoice_id)
        except Exception:
            db.rollback()
            logger.debug(
                "Failed invoice %s during confirmed bulk void",
                invoice_id,
                exc_info=True,
            )
            result.failed_ids.append(invoice_id)
    return result


def bulk_mark_paid(db, invoice_ids_csv: str) -> list[str]:
    """Mark eligible invoices as paid; return IDs that were updated."""
    updated: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if not invoice:
                continue
            if invoice_bulk_action_ineligibility(invoice, "mark_paid") is not None:
                continue
            balance = invoice.balance_due or Decimal("0")
            # Record a real succeeded payment allocated to the invoice instead
            # of poking status=paid+balance=0 raw. The raw write had no backing
            # PaymentAllocation, so the next _recalculate_invoice_totals (any
            # later payment/credit/line change) reverted it to issued/overdue
            # and re-triggered dunning. Routing through Payments.create produces
            # the ledger + allocation, so the recalc keeps it paid. A per-invoice
            # external_id dodges the 60s identical-amount duplicate guard.
            billing_service.payments.create(
                db,
                PaymentCreate(
                    account_id=invoice.account_id,
                    amount=balance,
                    currency=invoice.currency,
                    status=PaymentStatus.succeeded,
                    external_id=f"bulk-mark-paid:{invoice.id}",
                    allocations=[
                        PaymentAllocationApply(invoice_id=invoice.id, amount=balance)
                    ],
                ),
            )
            updated.append(invoice_id)
        except Exception:
            logger.debug(
                "Skipping invoice %s during bulk mark paid", invoice_id, exc_info=True
            )
            continue
    return updated


def bulk_mark_paid_result(db, invoice_ids_csv: str) -> BulkInvoiceActionResult:
    """Mark eligible invoices as paid and report processed/skipped/failed counts."""
    result = BulkInvoiceActionResult(selected_ids=parse_ids_csv(invoice_ids_csv))
    for invoice_id in result.selected_ids:
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if (
                not invoice
                or invoice_bulk_action_ineligibility(invoice, "mark_paid") is not None
            ):
                result.skipped_ids.append(invoice_id)
                continue
            balance = invoice.balance_due or Decimal("0")
            billing_service.payments.create(
                db,
                PaymentCreate(
                    account_id=invoice.account_id,
                    amount=balance,
                    currency=invoice.currency,
                    status=PaymentStatus.succeeded,
                    external_id=f"bulk-mark-paid:{invoice.id}",
                    allocations=[
                        PaymentAllocationApply(invoice_id=invoice.id, amount=balance)
                    ],
                ),
            )
            result.processed_ids.append(invoice_id)
        except HTTPException as exc:
            if exc.status_code < 500:
                result.skipped_ids.append(invoice_id)
                continue
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk mark paid",
                invoice_id,
                exc_info=True,
            )
            result.failed_ids.append(invoice_id)
        except Exception:
            db.rollback()
            logger.debug(
                "Skipping invoice %s during bulk mark paid", invoice_id, exc_info=True
            )
            result.failed_ids.append(invoice_id)
    return result


def execute_bulk_action(db, *, action: str, invoice_ids_csv: str) -> list[str]:
    """Execute a named bulk invoice action and return processed IDs."""
    if action == "issue":
        return bulk_issue(db, invoice_ids_csv)
    if action == "send":
        return bulk_send(db, invoice_ids_csv)
    if action == "void":
        return bulk_void(db, invoice_ids_csv)
    if action == "mark_paid":
        return bulk_mark_paid(db, invoice_ids_csv)
    raise ValueError("Unsupported invoice bulk action")


def execute_bulk_action_result(
    db, *, action: str, invoice_ids_csv: str
) -> BulkInvoiceActionResult:
    """Execute a named bulk invoice action and return a full outcome."""
    if action == "issue":
        return bulk_issue_result(db, invoice_ids_csv)
    if action == "send":
        return bulk_send_result(db, invoice_ids_csv)
    if action == "void":
        return bulk_void_result(db, invoice_ids_csv)
    if action == "mark_paid":
        return bulk_mark_paid_result(db, invoice_ids_csv)
    raise ValueError("Unsupported invoice bulk action")


def execute_audited_bulk_action(
    db,
    request,
    *,
    action: str,
    invoice_ids_csv: str,
) -> list[str]:
    """Execute a bulk invoice action and log one audit event per affected invoice."""
    updated_ids = execute_bulk_action(
        db,
        action=action,
        invoice_ids_csv=invoice_ids_csv,
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    for invoice_id in updated_ids:
        record_audit_event(
            db,
            action=action,
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=actor_id,
        )
    return updated_ids


def execute_audited_bulk_action_result(
    db,
    request,
    *,
    action: str,
    invoice_ids_csv: str,
) -> BulkInvoiceActionResult:
    """Execute a bulk invoice action and audit only affected invoices."""
    result = execute_bulk_action_result(
        db,
        action=action,
        invoice_ids_csv=invoice_ids_csv,
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor_id = str(current_user.get("subscriber_id")) if current_user else None
    for invoice_id in result.processed_ids:
        record_audit_event(
            db,
            action=action,
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=actor_id,
        )
    return result


def bulk_queue_pdf_exports(
    db, invoice_ids_csv: str, requested_by_id: str | None = None
) -> dict[str, list[str]]:
    """Queue PDF exports for selected invoices and report results."""
    queued: list[str] = []
    ready: list[str] = []
    missing: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if not invoice:
                missing.append(invoice_id)
                continue
            latest_export = billing_invoice_pdf_service.get_latest_export(
                db, invoice_id=str(invoice.id)
            )
            latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(
                db, latest_export
            )
            if billing_invoice_pdf_service.export_file_exists(db, latest_export):
                ready.append(str(invoice.id))
                continue
            billing_invoice_pdf_service.queue_export(
                db,
                invoice_id=str(invoice.id),
                requested_by_id=requested_by_id,
            )
            queued.append(str(invoice.id))
        except Exception:
            missing.append(invoice_id)
            continue
    return {"queued": queued, "ready": ready, "missing": missing}


def build_pdf_zip(db: Session, invoice_ids_csv: str) -> io.BytesIO:
    """Build a ZIP archive containing PDF exports for the given invoices.

    Fetches the latest PDF export for each invoice, bundles them into a
    ZIP file with duplicate-filename resolution, and appends a README
    when invoices are skipped or the selection is empty.

    Args:
        db: Database session.
        invoice_ids_csv: Comma-separated invoice IDs.

    Returns:
        A BytesIO buffer containing the ZIP archive.
    """
    invoices = list_invoices_by_ids(db, invoice_ids_csv)
    archive_buffer = io.BytesIO()
    skipped: list[str] = []
    used_names: set[str] = set()

    with zipfile.ZipFile(
        archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for invoice in invoices:
            latest_export = billing_invoice_pdf_service.get_latest_export(
                db, invoice_id=str(invoice.id)
            )
            latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(
                db, latest_export
            )
            if not billing_invoice_pdf_service.export_file_exists(db, latest_export):
                skipped.append(str(invoice.invoice_number or invoice.id))
                continue
            try:
                stream = billing_invoice_pdf_service.stream_export(db, latest_export)
                pdf_bytes = b"".join(stream.chunks)
            except ObjectNotFoundError:
                skipped.append(str(invoice.invoice_number or invoice.id))
                continue

            filename = billing_invoice_pdf_service.download_filename(invoice)
            if filename in used_names:
                stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
                suffix = 2
                while f"{stem}_{suffix}.pdf" in used_names:
                    suffix += 1
                filename = f"{stem}_{suffix}.pdf"
            used_names.add(filename)
            archive.writestr(filename, pdf_bytes)

        if skipped:
            logger.info("Bulk PDF ZIP skipped %d invoices", len(skipped))
            archive.writestr(
                "README.txt",
                "Some selected invoices were skipped because PDF exports were not ready:\n"
                + "\n".join(f"- {value}" for value in skipped),
            )
        elif not invoices:
            archive.writestr("README.txt", "No invoices were selected.")

    archive_buffer.seek(0)
    return archive_buffer


def bulk_pdf_readiness(db, invoice_ids_csv: str) -> dict[str, object]:
    """Return readiness summary for selected invoice PDFs."""
    ready: list[str] = []
    pending: list[str] = []
    missing: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if not invoice:
                missing.append(invoice_id)
                continue
            latest_export = billing_invoice_pdf_service.get_latest_export(
                db, invoice_id=str(invoice.id)
            )
            latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(
                db, latest_export
            )
            if billing_invoice_pdf_service.export_file_exists(db, latest_export):
                ready.append(str(invoice.id))
            else:
                pending.append(str(invoice.id))
        except Exception:
            missing.append(invoice_id)
            continue

    total = len(ready) + len(pending)
    return {
        "total": total,
        "ready_count": len(ready),
        "pending_count": len(pending),
        "missing_count": len(missing),
        "all_ready": total > 0 and len(pending) == 0,
        "ready": ready,
        "pending": pending,
        "missing": missing,
    }
