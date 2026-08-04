"""Service helpers for admin billing invoice routes."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TypedDict
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber
from app.schemas.billing import (
    CreditNoteApplicationPreviewRequest,
    CreditNoteApplyRequest,
    InvoiceClosureConfirm,
    InvoiceLineCreate,
    InvoiceLineUpdate,
)
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services import invoice_bank_details as invoice_bank_details_service
from app.services import invoice_draft_authoring
from app.services import web_billing_customers as web_billing_customers_service
from app.services import (
    web_prepaid_draft_reconciliation as web_prepaid_draft_reconciliation_service,
)
from app.services.audit_helpers import (
    extract_changes,
    format_changes,
    log_audit_event,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.status_presentation import invoice_status_presentation
from app.validators.forms import parse_datetime, parse_decimal, parse_uuid

logger = logging.getLogger(__name__)
PROFORMA_TAG = invoice_draft_authoring.PROFORMA_TAG
PROFORMA_PREFIX = invoice_draft_authoring.PROFORMA_PREFIX
apply_proforma_form_values = invoice_draft_authoring.apply_proforma_form_values


class InvoiceLineItem(TypedDict):
    line_id: UUID | None
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate_id: UUID | None


def is_proforma_invoice(invoice: Invoice | None) -> bool:
    if not invoice:
        return False
    if bool(getattr(invoice, "is_proforma", False)):
        return True
    memo = str(getattr(invoice, "memo", "") or "")
    number = str(getattr(invoice, "invoice_number", "") or "")
    if PROFORMA_TAG in memo:
        return True
    return number.upper().startswith(PROFORMA_PREFIX)


def build_proforma_summary(invoices: list[Invoice]) -> dict[str, int]:
    proforma_rows = [item for item in invoices if is_proforma_invoice(item)]
    paid = 0
    unpaid = 0
    for invoice in proforma_rows:
        due = Decimal(str(getattr(invoice, "balance_due", 0) or 0))
        if due > 0:
            unpaid += 1
        else:
            paid += 1
    return {
        "total": len(proforma_rows),
        "paid": paid,
        "unpaid": unpaid,
    }


def convert_proforma_to_final(
    db: Session,
    *,
    invoice_id: str,
    actor_id: str | None = None,
) -> Invoice:
    """Map the admin action to the locked, idempotent conversion owner."""

    db_session_adapter.release_read_transaction(db)
    try:
        result = invoice_draft_authoring.convert_proforma_invoice(
            db,
            invoice_draft_authoring.ConvertProformaInvoiceCommand(
                invoice_id=UUID(invoice_id),
            ),
            context=CommandContext.system(
                actor=actor_id or "admin-billing",
                scope="invoice_proforma:convert",
                reason="Convert administrative proforma to final invoice",
                idempotency_key=f"invoice-proforma-conversion:{invoice_id}",
            ),
        )
    except invoice_draft_authoring.InvoiceDraftAuthoringError as exc:
        status_code = 404 if exc.code.endswith("invoice_not_found") else 409
        raise HTTPException(status_code=status_code, detail=exc.message) from exc
    return billing_service.invoices.get(db=db, invoice_id=str(result.invoice_id))


def build_invoice_payload_data(
    *,
    account_id,
    invoice_number: str | None,
    status: str | None,
    currency: str,
    issued_at,
    due_at,
    memo: str | None,
    is_proforma: bool = False,
) -> dict[str, object]:
    """Build invoice create/update payload dictionary."""
    return {
        "account_id": account_id,
        "invoice_number": invoice_number.strip() if invoice_number else None,
        "status": status or "draft",
        "currency": currency.strip().upper(),
        "issued_at": issued_at,
        "due_at": due_at,
        "memo": memo.strip() if memo else None,
        "is_proforma": is_proforma,
    }


def parse_create_line_items(
    *,
    line_items_json: str | None,
    line_description: list[str],
    line_quantity: list[str],
    line_unit_price: list[str],
    line_tax_rate_id: list[str],
    parse_decimal,
) -> list[InvoiceLineItem]:
    """Parse a complete line set and reject malformed or empty drafts."""
    line_items: list[InvoiceLineItem] = []
    if line_items_json and line_items_json.strip():
        try:
            items_data = json.loads(line_items_json)
            if not isinstance(items_data, list):
                raise ValueError("Line items must be a list.")
            for item in items_data:
                if not isinstance(item, dict):
                    raise ValueError("Each invoice line must be an object.")
                description = str(item.get("description", "")).strip()
                if not description:
                    raise ValueError("Each invoice line needs a description.")
                raw_line_id = (
                    item.get("id") or item.get("lineId") or item.get("line_id")
                )
                line_items.append(
                    {
                        "line_id": UUID(str(raw_line_id)) if raw_line_id else None,
                        "description": description,
                        "quantity": Decimal(str(item.get("quantity", 1))),
                        "unit_price": Decimal(str(item.get("unitPrice", 0))),
                        "tax_rate_id": UUID(
                            str(item.get("taxRateId") or item.get("tax_rate_id"))
                        )
                        if item.get("taxRateId") or item.get("tax_rate_id")
                        else None,
                    }
                )
        except (json.JSONDecodeError, InvalidOperation, KeyError, TypeError) as exc:
            raise ValueError("Line items are malformed.") from exc
        if not line_items:
            raise ValueError("Add at least one invoice line.")
        return line_items

    for idx, description in enumerate(line_description):
        if not description or not description.strip():
            continue
        quantity_raw = line_quantity[idx] if idx < len(line_quantity) else ""
        unit_price_raw = line_unit_price[idx] if idx < len(line_unit_price) else ""
        tax_rate_raw = line_tax_rate_id[idx] if idx < len(line_tax_rate_id) else ""
        line_items.append(
            {
                "line_id": None,
                "description": description.strip(),
                "quantity": parse_decimal(quantity_raw, "quantity", Decimal("1")),
                "unit_price": parse_decimal(
                    unit_price_raw, "unit_price", Decimal("0.00")
                ),
                "tax_rate_id": UUID(tax_rate_raw) if tax_rate_raw else None,
            }
        )
    if not line_items:
        raise ValueError("Add at least one invoice line.")
    return line_items


def create_invoice_lines(
    db: Session, *, invoice_id: UUID, line_items: list[InvoiceLineItem]
) -> None:
    """Create invoice lines from parsed line item dictionaries."""
    for item in line_items:
        billing_service.invoice_lines.create(
            db,
            InvoiceLineCreate(
                invoice_id=invoice_id,
                description=item["description"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                tax_rate_id=item["tax_rate_id"],
            ),
        )


def maybe_issue_invoice(db: Session, *, invoice_id, issue_immediately: str | None):
    """Issue invoice when requested."""
    if not issue_immediately:
        return None
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if invoice is None:
        return None
    if invoice.status != InvoiceStatus.draft:
        return invoice
    transition = billing_service.invoices.issue_draft_system(
        db,
        str(invoice_id),
        issued_at=datetime.now(UTC),
        due_at=invoice.due_at,
        reason="admin_invoice_create",
        announce=False,
        commit=True,
    )
    return transition.invoice


def maybe_send_invoice_notification(
    db: Session, *, invoice, send_notification: str | None
) -> None:
    """Request canonical invoice notification delivery when selected."""
    if not send_notification or not invoice:
        return
    if invoice.is_proforma or invoice.status in {
        InvoiceStatus.draft,
        InvoiceStatus.void,
        InvoiceStatus.written_off,
    }:
        raise HTTPException(
            status_code=409,
            detail="Only a final issued invoice can be sent",
        )
    billing_service.invoices.announce_issued(
        db,
        str(invoice.id),
        reason="admin_invoice_send",
        commit=True,
    )


def create_invoice_from_form(
    db: Session,
    *,
    account_id: str | None,
    customer_ref: str | None,
    invoice_number: str | None,
    status: str | None,
    currency: str,
    issued_at: str | None,
    due_at: str | None,
    memo: str | None,
    proforma_invoice: str | None,
    line_description: list[str],
    line_quantity: list[str],
    line_unit_price: list[str],
    line_tax_rate_id: list[str],
    line_items_json: str | None,
    issue_immediately: str | None,
    send_notification: str | None,
    actor_id: str | None = None,
    draft_idempotency_key: str | None = None,
    parse_uuid=parse_uuid,
    parse_datetime=parse_datetime,
    parse_decimal=parse_decimal,
) -> tuple[Invoice, str | None]:
    """Parse the web form and invoke the atomic draft-authoring owner."""
    resolved_account_id = account_id
    if not resolved_account_id and customer_ref:
        customer_accounts = web_billing_customers_service.accounts_for_customer(
            db, customer_ref
        )
        if len(customer_accounts) == 1:
            resolved_account_id = str(customer_accounts[0]["id"])
        elif len(customer_accounts) > 1:
            raise ValueError(
                "Please select a billing account for the selected customer."
            )
        else:
            raise ValueError("No billing account found for the selected customer.")

    invoice_number, memo = apply_proforma_form_values(
        invoice_number=invoice_number,
        memo=memo,
        proforma_invoice=bool(proforma_invoice),
    )
    if status and status != InvoiceStatus.draft.value:
        raise ValueError(
            "Administrative authoring creates a draft; issue it after review."
        )
    if issue_immediately or send_notification:
        raise ValueError(
            "Save the complete draft first, then issue or send it separately."
        )
    resolved_account_uuid = parse_uuid(resolved_account_id, "account_id")
    line_items = parse_create_line_items(
        line_items_json=line_items_json,
        line_description=line_description,
        line_quantity=line_quantity,
        line_unit_price=line_unit_price,
        line_tax_rate_id=line_tax_rate_id,
        parse_decimal=parse_decimal,
    )
    db_session_adapter.release_read_transaction(db)
    result = invoice_draft_authoring.create_invoice_draft(
        db,
        invoice_draft_authoring.CreateInvoiceDraftCommand(
            account_id=resolved_account_uuid,
            invoice_number=invoice_number,
            currency=currency,
            issued_at=parse_datetime(issued_at),
            due_at=parse_datetime(due_at),
            memo=memo,
            is_proforma=bool(proforma_invoice),
            lines=tuple(
                invoice_draft_authoring.DraftLineCommand(
                    line_id=item["line_id"],
                    description=item["description"],
                    quantity=item["quantity"],
                    unit_price=item["unit_price"],
                    tax_rate_id=item["tax_rate_id"],
                )
                for item in line_items
            ),
        ),
        context=CommandContext.system(
            actor=actor_id or "admin-billing",
            scope="invoice_draft:create",
            reason="Create administrative invoice draft",
            idempotency_key=draft_idempotency_key,
        ),
    )
    invoice = billing_service.invoices.get(db=db, invoice_id=str(result.invoice_id))
    return invoice, resolved_account_id


def update_invoice_from_form(
    db: Session,
    *,
    invoice_id: str,
    account_id: str | None,
    invoice_number: str | None,
    status: str | None,
    currency: str,
    issued_at: str | None,
    due_at: str | None,
    memo: str | None,
    proforma_invoice: str | None,
    line_items_json: str | None,
    actor_id: str | None = None,
    draft_idempotency_key: str | None = None,
    parse_uuid=parse_uuid,
    parse_datetime=parse_datetime,
) -> tuple[Invoice, dict[str, object] | None]:
    """Parse the web form and atomically replace one draft aggregate."""
    before = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if before.status != InvoiceStatus.draft:
        raise ValueError("Only draft invoices can be edited.")
    if status and status != InvoiceStatus.draft.value:
        raise ValueError("Issue and overdue transitions use their separate actions.")
    invoice_number, memo = apply_proforma_form_values(
        invoice_number=invoice_number,
        memo=memo,
        proforma_invoice=bool(proforma_invoice),
    )
    lines = parse_create_line_items(
        line_items_json=line_items_json,
        line_description=[],
        line_quantity=[],
        line_unit_price=[],
        line_tax_rate_id=[],
        parse_decimal=parse_decimal,
    )
    resolved_account_id = parse_uuid(account_id, "account_id")
    db_session_adapter.release_read_transaction(db)
    result = invoice_draft_authoring.update_invoice_draft(
        db,
        invoice_draft_authoring.UpdateInvoiceDraftCommand(
            invoice_id=UUID(invoice_id),
            account_id=resolved_account_id,
            invoice_number=invoice_number,
            currency=currency,
            issued_at=parse_datetime(issued_at),
            due_at=parse_datetime(due_at),
            memo=memo,
            is_proforma=bool(proforma_invoice),
            lines=tuple(
                invoice_draft_authoring.DraftLineCommand(
                    line_id=item["line_id"],
                    description=item["description"],
                    quantity=item["quantity"],
                    unit_price=item["unit_price"],
                    tax_rate_id=item["tax_rate_id"],
                )
                for item in lines
            ),
        ),
        context=CommandContext.system(
            actor=actor_id or "admin-billing",
            scope=f"invoice_draft:{invoice_id}:update",
            reason="Update administrative invoice draft",
            idempotency_key=draft_idempotency_key,
        ),
    )
    after = billing_service.invoices.get(db=db, invoice_id=str(result.invoice_id))
    return after, None


def create_invoice_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    account_id: str | None,
    customer_ref: str | None,
    invoice_number: str | None,
    status: str | None,
    currency: str,
    issued_at: str | None,
    due_at: str | None,
    memo: str | None,
    proforma_invoice: str | None,
    line_description: list[str],
    line_quantity: list[str],
    line_unit_price: list[str],
    line_tax_rate_id: list[str],
    line_items_json: str | None,
    issue_immediately: str | None,
    send_notification: str | None,
    draft_idempotency_key: str | None = None,
) -> tuple[Invoice, str | None]:
    invoice, resolved_account_id = create_invoice_from_form(
        db,
        account_id=account_id,
        customer_ref=customer_ref,
        invoice_number=invoice_number,
        status=status,
        currency=currency,
        issued_at=issued_at,
        due_at=due_at,
        memo=memo,
        proforma_invoice=proforma_invoice,
        line_description=line_description,
        line_quantity=line_quantity,
        line_unit_price=line_unit_price,
        line_tax_rate_id=line_tax_rate_id,
        line_items_json=line_items_json,
        issue_immediately=issue_immediately,
        send_notification=send_notification,
        actor_id=actor_id,
        draft_idempotency_key=draft_idempotency_key,
    )
    return invoice, resolved_account_id


def update_invoice_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    invoice_id: str,
    account_id: str | None,
    invoice_number: str | None,
    status: str | None,
    currency: str,
    issued_at: str | None,
    due_at: str | None,
    memo: str | None,
    proforma_invoice: str | None,
    line_items_json: str | None,
    draft_idempotency_key: str | None = None,
) -> Invoice:
    invoice, _metadata_payload = update_invoice_from_form(
        db,
        invoice_id=invoice_id,
        account_id=account_id,
        invoice_number=invoice_number,
        status=status,
        currency=currency,
        issued_at=issued_at,
        due_at=due_at,
        memo=memo,
        proforma_invoice=proforma_invoice,
        line_items_json=line_items_json,
        actor_id=actor_id,
        draft_idempotency_key=draft_idempotency_key,
    )
    return invoice


def generate_invoice_from_subscription_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    subscriber_id: str,
    subscription_id: str,
) -> Invoice:
    from app.services.billing.invoices import Invoices

    invoice = Invoices.create_for_subscription(db, subscriber_id, subscription_id)
    log_audit_event(
        db=db,
        request=request,
        action="generate_from_subscription",
        entity_type="invoice",
        entity_id=str(invoice.id),
        actor_id=actor_id,
        metadata={
            "invoice_number": invoice.invoice_number,
            "subscription_id": subscription_id,
        },
    )
    return invoice


def preview_credit_note_application(
    db: Session,
    *,
    invoice_id: str,
    credit_note_id: str,
    amount: str | None,
) -> object:
    payload = CreditNoteApplicationPreviewRequest(
        invoice_id=UUID(invoice_id),
        amount=parse_decimal(amount, "amount") if amount else None,
    )
    return billing_service.credit_notes.preview_application(db, credit_note_id, payload)


def apply_credit_note_to_invoice(
    db: Session,
    *,
    invoice_id: str,
    credit_note_id: str,
    amount: Decimal,
    memo: str | None,
    preview_fingerprint: str,
    idempotency_key: str,
):
    """Execute one confirmed preview and return exact financial evidence."""
    payload = CreditNoteApplyRequest(
        invoice_id=UUID(invoice_id),
        amount=amount,
        memo=memo,
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
    )
    return billing_service.credit_notes.apply_with_evidence(
        db, credit_note_id, payload, stage_audit=False
    )


def create_invoice_line_from_form(
    db: Session,
    *,
    invoice_id: str,
    description: str,
    quantity: str,
    unit_price: str,
    tax_rate_id: str | None,
    parse_uuid=parse_uuid,
    parse_decimal=parse_decimal,
) -> None:
    """Create invoice line from raw web form values."""
    payload = InvoiceLineCreate(
        invoice_id=parse_uuid(invoice_id, "invoice_id"),
        description=description.strip(),
        quantity=parse_decimal(quantity, "quantity"),
        unit_price=parse_decimal(unit_price, "unit_price"),
        tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
    )
    billing_service.invoice_lines.create(db, payload)


def apply_line_items_json_update(
    db: Session, *, invoice_id, before_invoice, line_items_json: str | None
) -> None:
    """Apply line item create/update/delete mutations from JSON payload."""
    if not line_items_json or not line_items_json.strip():
        return
    try:
        items_data = json.loads(line_items_json)
    except json.JSONDecodeError:
        return
    if items_data is None:
        return

    existing_lines = {
        str(line.id): line
        for line in (before_invoice.lines or [])
        if getattr(line, "is_active", True)
    }
    seen_ids: set[str] = set()
    for item in items_data:
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        quantity = Decimal(str(item.get("quantity", 1)))
        unit_price = Decimal(str(item.get("unitPrice", 0)))
        tax_rate_id = item.get("taxRateId") or item.get("tax_rate_id")
        line_id = item.get("id") or item.get("lineId") or item.get("line_id")
        if line_id and str(line_id) in existing_lines:
            seen_ids.add(str(line_id))
            billing_service.invoice_lines.update(
                db,
                str(line_id),
                InvoiceLineUpdate(
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                    tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
                ),
            )
        else:
            billing_service.invoice_lines.create(
                db,
                InvoiceLineCreate(
                    invoice_id=invoice_id,
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                    tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
                ),
            )
    for line_id in existing_lines:
        if line_id not in seen_ids:
            billing_service.invoice_lines.delete(db, line_id)


def load_tax_rates(db: Session):
    return billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )


def load_credit_application_options(db: Session, *, invoice_id: str):
    return billing_service.credit_notes.list_application_options(db, invoice_id)


def build_invoice_activities(db: Session, *, invoice_id: str) -> list[dict]:
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="invoice",
        entity_id=invoice_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {
        str(event.actor_id)
        for event in audit_events
        if getattr(event, "actor_id", None)
    }
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.scalars(
                select(Subscriber).where(Subscriber.id.in_(actor_ids))
            ).all()
        }
    activities = []
    for event in audit_events:
        actor = (
            people.get(str(event.actor_id))
            if getattr(event, "actor_id", None)
            else None
        )
        actor_name = (
            f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        )
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        activities.append(
            {
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": f"{actor_name}"
                + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def load_invoice_detail_data(
    db: Session, *, invoice_id: str
) -> dict[str, object] | None:
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice:
        return None
    pdf_export = billing_invoice_pdf_service.get_latest_export(
        db, invoice_id=invoice_id
    )
    prepaid_draft_reconciliation_preview = (
        web_prepaid_draft_reconciliation_service.preview_for_invoice_detail(
            db, invoice_id=UUID(invoice_id)
        )
    )
    return {
        "invoice": invoice,
        "invoice_financial_summary": billing_service.invoices.financial_summary(
            db, invoice_id
        ),
        "invoice_status_presentation": invoice_status_presentation(invoice.status),
        "tax_rates": load_tax_rates(db),
        "credit_application_options": load_credit_application_options(
            db, invoice_id=invoice_id
        ),
        "credit_application_idempotency_key": secrets.token_urlsafe(24),
        "invoice_void_capability": billing_service.invoices.void_capability(
            db, invoice_id
        ),
        "invoice_write_off_capability": (
            billing_service.invoices.write_off_capability(db, invoice_id)
        ),
        "activities": build_invoice_activities(db, invoice_id=invoice_id),
        "pdf_export": pdf_export,
        "is_proforma": is_proforma_invoice(invoice),
        "invoice_bank_details": invoice_bank_details_service.get_invoice_bank_details(
            db, currency=invoice.currency
        ),
        "prepaid_draft_reconciliation_preview": (prepaid_draft_reconciliation_preview),
    }


def convert_proforma_to_final_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    invoice_id: str,
) -> Invoice:
    return convert_proforma_to_final(
        db,
        invoice_id=invoice_id,
        actor_id=actor_id,
    )


def apply_credit_note_to_invoice_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    invoice_id: str,
    credit_note_id: str,
    amount: str | None,
    memo: str | None,
    preview_fingerprint: str,
    idempotency_key: str,
) -> dict[str, object]:
    result = apply_credit_note_to_invoice(
        db,
        invoice_id=invoice_id,
        credit_note_id=credit_note_id,
        amount=parse_decimal(amount, "amount"),
        memo=memo.strip() if memo else None,
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
    )
    metadata_payload = result.audit_metadata()
    if not result.idempotent_replay:
        log_audit_event(
            db=db,
            request=request,
            action="apply",
            entity_type="credit_note",
            entity_id=str(credit_note_id),
            actor_id=actor_id,
            metadata=metadata_payload,
        )
    return metadata_payload


def send_invoice_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    invoice_id: str,
) -> Invoice | None:
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if invoice:
        maybe_send_invoice_notification(db, invoice=invoice, send_notification="1")
    log_audit_event(
        db=db,
        request=request,
        action="send",
        entity_type="invoice",
        entity_id=invoice_id,
        actor_id=actor_id,
    )
    return invoice


def void_invoice_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    invoice_id: str,
) -> None:
    raise HTTPException(
        status_code=409,
        detail="Invoice void requires owner preview and confirmation",
    )


def preview_invoice_void_web(db: Session, *, invoice_id: str):
    return billing_service.invoices.preview_void(db, invoice_id)


def preview_invoice_write_off_web(db: Session, *, invoice_id: str):
    return billing_service.invoices.preview_write_off(db, invoice_id)


def confirm_invoice_void_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    invoice_id: str,
    preview_fingerprint: str,
    idempotency_key: str,
    memo: str | None,
):
    result = billing_service.invoices.confirm_void(
        db,
        invoice_id,
        InvoiceClosureConfirm(
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
            memo=memo,
        ),
    )
    log_audit_event(
        db=db,
        request=request,
        action="void",
        entity_type="invoice",
        entity_id=invoice_id,
        actor_id=actor_id,
        metadata={"closure_id": str(result.closure.id)},
    )
    return result


def confirm_invoice_write_off_web(
    db: Session,
    *,
    request,
    actor_id: str | None,
    invoice_id: str,
    preview_fingerprint: str,
    idempotency_key: str,
    memo: str | None,
):
    result = billing_service.invoices.confirm_write_off(
        db,
        invoice_id,
        InvoiceClosureConfirm(
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
            memo=memo,
        ),
    )
    log_audit_event(
        db=db,
        request=request,
        action="write_off",
        entity_type="invoice",
        entity_id=invoice_id,
        actor_id=actor_id,
        metadata={"closure_id": str(result.closure.id)},
    )
    return result
