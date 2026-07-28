"""Purchase-invoice origination and repair for the Sub -> ERP outbox."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    flow_owned_by_sub,
)
from app.models.vendor_routes import (
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceStatus,
)
from app.services.dotmac_erp import outbox
from app.services.file_storage import file_uploads
from app.services.integrations.erp_capability import capability_client

logger = logging.getLogger(__name__)

ENTITY_TYPE = "vendor_purchase_invoice"
PROVIDER = "dotmac_erp"
_AMOUNT_TOLERANCE = Decimal("0.02")


@dataclass(frozen=True, slots=True)
class _PaymentObservationContext:
    id: object
    payables_document_reference: str
    currency: str


class _PurchaseInvoiceStatusClient(Protocol):
    def get_purchase_invoice_status(self, source_invoice_id: str) -> dict | None: ...

    def close(self) -> None: ...


def _normalized_status(value: object) -> str:
    status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not status:
        raise ValueError("ERP payment observation did not include a status")
    return status[:40]


def _decimal_field(response: dict[str, Any], field: str) -> Decimal:
    try:
        value = Decimal(str(response[field]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"ERP payment observation has invalid {field}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"ERP payment observation has invalid {field}")
    return value


def _optional_source_updated_at(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "ERP payment observation has invalid source_updated_at"
            ) from exc
    if parsed.tzinfo is None:
        raise ValueError("ERP payment observation source_updated_at has no timezone")
    return parsed


def _canonical_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validated_payment_observation(
    invoice: VendorPurchaseInvoice | _PaymentObservationContext,
    response: dict[str, Any],
) -> dict[str, Any]:
    source_invoice_id = str(response.get("source_invoice_id") or "")
    if source_invoice_id != str(invoice.id):
        raise ValueError("ERP payment observation source invoice does not match")
    erp_invoice_id = str(response.get("purchase_invoice_id") or "")
    if erp_invoice_id != str(invoice.payables_document_reference or ""):
        raise ValueError("ERP payment observation purchase invoice does not match")
    currency = str(response.get("currency") or "").strip().upper()
    if currency != invoice.currency.upper():
        raise ValueError("ERP payment observation currency does not match")

    total_amount = _decimal_field(response, "total_amount")
    amount_paid = _decimal_field(response, "amount_paid")
    balance_due = _decimal_field(response, "balance_due")
    if amount_paid > total_amount + _AMOUNT_TOLERANCE:
        raise ValueError("ERP payment observation amount paid exceeds total")
    if abs(total_amount - amount_paid - balance_due) > _AMOUNT_TOLERANCE:
        raise ValueError("ERP payment observation amounts do not reconcile")

    return {
        "status": _normalized_status(response.get("status")),
        "total_amount": total_amount,
        "amount_paid": amount_paid,
        "balance_due": balance_due,
        "source_updated_at": _optional_source_updated_at(
            response.get("source_updated_at")
        ),
    }


def purchase_invoice_idempotency_key(invoice: VendorPurchaseInvoice) -> str:
    return f"pinv-{invoice.id}"


def purchase_invoice_eligibility_error(invoice: VendorPurchaseInvoice) -> str | None:
    if invoice.status != VendorPurchaseInvoiceStatus.approved.value:
        return "Purchase invoice is not approved"
    if invoice.payables_document_reference:
        return "Purchase invoice is already linked to ERP"
    if invoice.project is None or invoice.project.project is None:
        return "Purchase invoice project context is missing"
    if invoice.vendor is None:
        return "Purchase invoice vendor context is missing"
    if invoice.vendor.supplier_system not in {None, PROVIDER}:
        return "Vendor is linked to another payables system"
    if not (invoice.vendor.supplier_reference or "").strip():
        return "Vendor is not linked to an ERP supplier"
    if invoice.project.procurement_system not in {None, PROVIDER}:
        return "Project purchase order belongs to another procurement system"
    erp_po_id = (
        invoice.procurement_order_reference
        or invoice.project.procurement_order_reference
        or ""
    ).strip()
    if not erp_po_id:
        return "Waiting for the installation project's ERP purchase order"
    if not any(item.is_active and item.quantity > 0 for item in invoice.line_items):
        return "Purchase invoice has no active, positive-quantity line items"
    return None


def build_purchase_invoice_payload(invoice: VendorPurchaseInvoice) -> dict:
    reason = purchase_invoice_eligibility_error(invoice)
    if reason:
        raise ValueError(reason)

    project = invoice.project
    base_project = project.project
    vendor = invoice.vendor
    erp_po_id = (
        invoice.procurement_order_reference or project.procurement_order_reference or ""
    ).strip()
    items = []
    for item in invoice.line_items:
        if not item.is_active or item.quantity <= 0:
            continue
        item_type = (item.item_type or "").strip() or "item"
        description = (item.description or "").strip()
        items.append(
            {
                "item_type": item_type[:80],
                "description": (
                    description or f"{item_type.replace('_', ' ').title()} item"
                )[:2000],
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
                "amount": str(item.amount),
                "notes": item.notes,
            }
        )

    payload = {
        "source_invoice_id": str(invoice.id),
        "source_invoice_number": invoice.invoice_number,
        "source_project_id": str(base_project.id),
        "installation_project_id": str(project.id),
        "source_quote_id": (
            str(project.approved_quote_id) if project.approved_quote_id else None
        ),
        "erp_purchase_order_id": erp_po_id,
        "vendor_name": vendor.name,
        "vendor_erp_id": vendor.supplier_reference,
        "vendor_code": (vendor.code or vendor.name)[:160],
        "currency": invoice.currency,
        "tax_rate_percent": str(invoice.tax_rate_percent or 0),
        "subtotal": str(invoice.subtotal),
        "tax_total": str(invoice.tax_total),
        "total": str(invoice.total),
        "items": items,
    }
    if base_project.code:
        payload["project_code"] = base_project.code
    if base_project.name:
        payload["project_name"] = base_project.name
    if invoice.reviewed_at:
        payload["approved_at"] = invoice.reviewed_at.isoformat()
    if invoice.reviewed_by and invoice.reviewed_by.email:
        payload["approved_by_email"] = invoice.reviewed_by.email
    return payload


def enqueue_purchase_invoice(
    db: Session, invoice: VendorPurchaseInvoice, *, isolate: bool = True
) -> FieldErpSyncEvent | None:
    """Queue a new-only invoice only after this flow has moved to Sub."""
    if not flow_owned_by_sub(db, FieldErpSyncFlow.purchase_invoice):
        return None
    reason = purchase_invoice_eligibility_error(invoice)
    if reason:
        invoice.payables_submission_error = reason[:500]
        return None
    invoice.procurement_order_reference = invoice.project.procurement_order_reference
    invoice.payables_system = PROVIDER
    invoice.payables_submission_error = None
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.purchase_invoice,
        entity_type=ENTITY_TYPE,
        entity_id=invoice.id,
        idempotency_key=purchase_invoice_idempotency_key(invoice),
        payload=build_purchase_invoice_payload(invoice),
        isolate=isolate,
    )


def event_ready(db: Session, event: FieldErpSyncEvent) -> bool:
    """Return false while a queued invoice is waiting for its ERP PO."""
    invoice = db.get(VendorPurchaseInvoice, event.entity_id)
    if invoice is None:
        return True  # Let normal delivery dead-letter the invalid source.
    erp_po_id = (
        invoice.procurement_order_reference
        or invoice.project.procurement_order_reference
    )
    if not erp_po_id:
        invoice.payables_submission_error = (
            "Waiting for the installation project's ERP purchase order"
        )
        return False
    if event.payload.get("erp_purchase_order_id") != erp_po_id:
        invoice.procurement_order_reference = erp_po_id
        event.payload = build_purchase_invoice_payload(invoice)
    return True


def _extract_erp_invoice_id(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    value = (
        response.get("purchase_invoice_id")
        or response.get("invoice_id")
        or response.get("name")
    )
    return str(value) if value else None


def apply_erp_response(db: Session, event: FieldErpSyncEvent) -> None:
    invoice = db.get(VendorPurchaseInvoice, event.entity_id)
    if invoice is None:
        logger.warning("No vendor purchase invoice for ERP event %s", event.id)
        return
    erp_id = _extract_erp_invoice_id(event.erp_response)
    if not erp_id:
        invoice.payables_submission_error = (
            "ERP response did not include a purchase invoice ID"
        )
        return
    invoice.payables_document_reference = erp_id[:100]
    invoice.payables_system = PROVIDER
    invoice.payables_document_status = str(
        (event.erp_response or {}).get("status") or "created"
    )[:40]
    invoice.payables_submission_error = None
    invoice.payables_submitted_at = datetime.now(UTC)


def upload_attachment(db: Session, invoice: VendorPurchaseInvoice) -> bool:
    if invoice.attachment is None or invoice.attachment.is_deleted:
        return False
    if (
        not invoice.payables_document_reference
        or invoice.payables_attachment_submitted_at
    ):
        return False
    stream = file_uploads.stream_file(invoice.attachment)
    data = b"".join(stream.chunks)
    payload = {
        "file_name": invoice.attachment.original_filename,
        "mime_type": invoice.attachment.content_type or "application/octet-stream",
        "content_base64": base64.b64encode(data).decode("ascii"),
    }
    with capability_client(db) as client:
        client.upload_purchase_invoice_attachment(
            invoice.payables_document_reference,
            payload,
            idempotency_key=f"pinv-attach-{invoice.id}",
        )
    invoice.payables_attachment_submitted_at = datetime.now(UTC)
    invoice.payables_submission_error = None
    return True


def repair_purchase_invoice_sync(db: Session, *, limit: int = 100) -> dict:
    """Queue newly eligible invoices and retry post-create attachments."""
    rows = (
        db.query(VendorPurchaseInvoice)
        .filter(VendorPurchaseInvoice.is_active.is_(True))
        .filter(
            VendorPurchaseInvoice.status == VendorPurchaseInvoiceStatus.approved.value
        )
        .order_by(VendorPurchaseInvoice.updated_at.asc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    processed = 0
    enqueued = 0
    attachments = 0
    errors: list[str] = []
    for invoice in rows:
        processed += 1
        try:
            if not invoice.payables_document_reference:
                if enqueue_purchase_invoice(db, invoice) is not None:
                    enqueued += 1
            elif upload_attachment(db, invoice):
                attachments += 1
            db.commit()
        except Exception as exc:  # Each invoice remains independently retryable.
            db.rollback()
            current = db.get(VendorPurchaseInvoice, invoice.id)
            if current is not None:
                current.payables_submission_error = str(exc)[:500]
                db.commit()
            errors.append(f"{invoice.id}: {exc}")
    return {
        "processed": processed,
        "enqueued": enqueued,
        "attachments": attachments,
        "errors": errors,
    }


def _record_status_error(
    db: Session,
    *,
    invoice_id: object,
    expected_erp_invoice_id: str,
    message: str,
) -> None:
    current = (
        db.query(VendorPurchaseInvoice)
        .filter(VendorPurchaseInvoice.id == invoice_id)
        .filter(VendorPurchaseInvoice.is_active.is_(True))
        .with_for_update(of=VendorPurchaseInvoice)
        .one_or_none()
    )
    if (
        current is not None
        and current.payables_document_reference == expected_erp_invoice_id
    ):
        current.payment_observation_error = message[:500]
        db.commit()
    else:
        db.commit()


def refresh_purchase_invoice_statuses(
    db: Session,
    *,
    client: _PurchaseInvoiceStatusClient | None = None,
    limit: int = 100,
    observed_at: datetime | None = None,
) -> dict:
    """Refresh ERP-owned AP settlement observations for linked vendor invoices.

    Candidate identifiers are snapshotted and the read transaction is closed
    before any network call. Each response is validated, then the source row is
    re-locked and its ERP link rechecked before the observation is projected.
    Repeated responses are safe; a failure retains the last good observation.
    """
    limit = max(1, min(int(limit or 100), 500))
    candidates = (
        db.query(
            VendorPurchaseInvoice.id,
            VendorPurchaseInvoice.payables_document_reference,
            VendorPurchaseInvoice.currency,
        )
        .filter(VendorPurchaseInvoice.is_active.is_(True))
        .filter(VendorPurchaseInvoice.payables_system == PROVIDER)
        .filter(VendorPurchaseInvoice.payables_document_reference.isnot(None))
        .order_by(
            VendorPurchaseInvoice.payment_observed_at.asc().nullsfirst(),
            VendorPurchaseInvoice.id.asc(),
        )
        .limit(limit)
        .all()
    )
    result: dict[str, object] = {
        "processed": 0,
        "observed": 0,
        "changed": 0,
        "errors": [],
    }
    if not candidates:
        return result

    owned_client = client
    created_client = False
    if owned_client is None:
        owned_client = capability_client(db)
        created_client = True

    # Never hold a database transaction open across the ERP request. The
    # candidate read is complete and has no writes, so closing it by commit is
    # safe and does not give the completed read rollback/failure semantics.
    db.commit()
    errors: list[str] = []
    processed = 0
    observed = 0
    changed = 0
    try:
        for invoice_id, linked_erp_id, currency in candidates:
            expected_erp_id = str(linked_erp_id)
            processed += 1
            try:
                response = owned_client.get_purchase_invoice_status(str(invoice_id))
                if not response:
                    raise ValueError("ERP purchase invoice was not found")
                observation = _validated_payment_observation(
                    _PaymentObservationContext(
                        id=invoice_id,
                        payables_document_reference=expected_erp_id,
                        currency=str(currency),
                    ),
                    response,
                )

                current = (
                    db.query(VendorPurchaseInvoice)
                    .filter(VendorPurchaseInvoice.id == invoice_id)
                    .filter(VendorPurchaseInvoice.is_active.is_(True))
                    .with_for_update(of=VendorPurchaseInvoice)
                    .one_or_none()
                )
                if (
                    current is None
                    or current.payables_document_reference != expected_erp_id
                    or current.currency != currency
                ):
                    db.commit()
                    continue
                before = (
                    current.payment_status,
                    current.payment_total_amount,
                    current.payment_amount_paid,
                    current.payment_balance_due,
                    _canonical_datetime(current.payment_source_updated_at),
                )
                current.payment_status = observation["status"]
                current.payment_total_amount = observation["total_amount"]
                current.payment_amount_paid = observation["amount_paid"]
                current.payment_balance_due = observation["balance_due"]
                current.payment_source_updated_at = observation["source_updated_at"]
                current.payment_observed_at = observed_at or datetime.now(UTC)
                current.payment_observation_error = None
                after = (
                    current.payment_status,
                    current.payment_total_amount,
                    current.payment_amount_paid,
                    current.payment_balance_due,
                    _canonical_datetime(current.payment_source_updated_at),
                )
                observed += 1
                if before != after:
                    changed += 1
                    # First projection of a changed payables observation is
                    # a committed output: evidence, never a Sub decision.
                    from app.services.events import EventType, emit_event

                    emit_event(
                        db,
                        EventType.vendor_purchase_invoice_payment_observed,
                        {
                            "invoice_id": str(current.id),
                            "payables_system": current.payables_system,
                            "payables_reference": (current.payables_document_reference),
                            "payment_status": current.payment_status,
                            "amount_paid": str(current.payment_amount_paid)
                            if current.payment_amount_paid is not None
                            else None,
                            "balance_due": str(current.payment_balance_due)
                            if current.payment_balance_due is not None
                            else None,
                        },
                        actor="integration.dotmac_erp_payables_adapter",
                    )
                db.commit()
            except Exception as exc:  # noqa: BLE001 - rows retry independently
                if db.in_transaction():
                    db.rollback()
                message = str(exc)
                errors.append(f"{invoice_id}: {message}")
                logger.warning(
                    "purchase_invoice_sync: status refresh failed for %s: %s",
                    invoice_id,
                    message,
                )
                _record_status_error(
                    db,
                    invoice_id=invoice_id,
                    expected_erp_invoice_id=expected_erp_id,
                    message=message,
                )
    finally:
        if created_client:
            owned_client.close()

    result.update(
        processed=processed,
        observed=observed,
        changed=changed,
        errors=errors,
    )
    return result


def run_repair_purchase_invoice_sync() -> dict[str, object]:
    """Own the background session for purchase-invoice projection repair."""
    from app.db import task_session

    with task_session() as db:
        return repair_purchase_invoice_sync(db)


def run_refresh_purchase_invoice_statuses() -> dict[str, object]:
    """Own the background session for ERP payment-observation reconciliation."""
    from app.db import task_session

    with task_session() as db:
        return refresh_purchase_invoice_statuses(db)
