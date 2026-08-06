"""Service helpers for billing invoice form pages."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import billing as billing_service
from app.services import settings_spec
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services.billing_settings import resolve_payment_due_days
from app.timezone import APP_TIMEZONE

logger = logging.getLogger(__name__)


def _build_invoice_form_config(
    invoice,
    tax_rates_json: list | None,
    existing_line_items: list | None,
    payment_terms_days: int = 30,
) -> dict:
    currency = ""
    if invoice and invoice.currency:
        currency = str(invoice.currency).upper()
    return {
        "accountId": str(invoice.account_id) if invoice and invoice.account_id else "",
        "invoiceNumber": invoice.invoice_number
        if invoice and invoice.invoice_number
        else "",
        "status": invoice.status.value if invoice and invoice.status else "draft",
        "currency": currency or "NGN",
        "issuedAt": invoice.issued_at.strftime("%Y-%m-%d")
        if invoice and invoice.issued_at
        else "",
        "dueAt": invoice.due_at.strftime("%Y-%m-%d")
        if invoice and invoice.due_at
        else "",
        "memo": invoice.memo if invoice and invoice.memo else "",
        "taxRates": tax_rates_json or [],
        "lineItems": existing_line_items or [],
        "invoiceId": str(invoice.id) if invoice else "",
        "paymentTermsDays": payment_terms_days,
        "discountType": getattr(invoice, "discount_type", None) if invoice else "",
        "discountValue": (
            float(getattr(invoice, "discount_value", 0))
            if invoice and getattr(invoice, "discount_value", None) is not None
            else ""
        ),
        "discountReason": getattr(invoice, "discount_reason", None) if invoice else "",
        "discountLocked": bool(
            invoice and getattr(invoice, "discount_source", None) == "quote"
        ),
    }


def resolve_selected_account(db: Session, account_id: str | None):
    if not account_id:
        return None
    try:
        return subscriber_service.accounts.get(db=db, account_id=account_id)
    except Exception:
        return None


def new_form_state(db: Session, *, account_id: str | None) -> dict[str, object]:
    selected_account = resolve_selected_account(db, account_id)
    tax_rates = web_billing_invoices_service.load_tax_rates(db)
    tax_rates_json = [
        {"id": str(rate.id), "name": rate.name, "rate": float(rate.rate or 0)}
        for rate in tax_rates or []
    ]
    payment_due_days = resolve_payment_due_days(db, subscriber=selected_account)
    invoice_config = _build_invoice_form_config(
        None,
        tax_rates_json,
        [
            {
                "description": "",
                "quantity": 1,
                "unit_price": 0,
                "tax_rate_id": None,
            }
        ],
        payment_due_days,
    )
    if selected_account:
        invoice_config["accountId"] = str(selected_account.id)

    default_currency = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_currency"
    )
    if default_currency is None:
        default_currency = "NGN"
    today = datetime.now(UTC).date()

    return {
        "accounts": None,
        "tax_rates": tax_rates,
        "tax_rates_json": tax_rates_json,
        "invoice_config": invoice_config,
        "default_issue_date": today.strftime("%Y-%m-%d"),
        "default_due_date": (today + timedelta(days=payment_due_days)).strftime(
            "%Y-%m-%d"
        ),
        "default_currency": default_currency,
        "account_locked": bool(selected_account),
        "account_label": web_billing_customers_service.account_label(selected_account)
        if selected_account
        else None,
        "account_number": selected_account.account_number if selected_account else None,
        "selected_account_id": str(selected_account.id) if selected_account else None,
        "account_x_model": "accountId",
        "draft_idempotency_key": secrets.token_urlsafe(24),
        "discount_applied_date": datetime.now(APP_TIMEZONE).date().isoformat(),
    }


def edit_form_state(db: Session, *, invoice_id: str) -> dict[str, object] | None:
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice:
        return None
    selected_account = invoice.account
    if not selected_account and invoice.account_id:
        selected_account = resolve_selected_account(db, str(invoice.account_id))
    tax_rates = web_billing_invoices_service.load_tax_rates(db)
    existing_line_items = [
        {
            "id": str(line.id),
            "description": line.description,
            "quantity": float(line.quantity or 1),
            "unit_price": float(line.unit_price or 0),
            "tax_rate_id": str(line.tax_rate_id) if line.tax_rate_id else None,
        }
        for line in (invoice.lines or [])
        if getattr(line, "is_active", True)
    ]
    tax_rates_json = [
        {"id": str(rate.id), "name": rate.name, "rate": float(rate.rate or 0)}
        for rate in tax_rates or []
    ]
    payment_due_days = resolve_payment_due_days(db, subscriber=selected_account)
    invoice_config = _build_invoice_form_config(
        invoice, tax_rates_json, existing_line_items, payment_due_days
    )
    discount_applied_at = getattr(invoice, "discount_applied_at", None)
    if discount_applied_at is not None:
        if discount_applied_at.tzinfo is None:
            discount_applied_at = discount_applied_at.replace(tzinfo=UTC)
        discount_applied_date = discount_applied_at.astimezone(APP_TIMEZONE).date()
    else:
        discount_applied_date = datetime.now(APP_TIMEZONE).date()
    return {
        "invoice": invoice,
        "accounts": None,
        "tax_rates": tax_rates,
        "tax_rates_json": tax_rates_json,
        "existing_line_items": existing_line_items,
        "invoice_config": invoice_config,
        "show_line_items": False,
        "account_locked": True,
        "account_label": web_billing_customers_service.account_label(selected_account),
        "account_number": selected_account.account_number if selected_account else None,
        "selected_account_id": str(selected_account.id)
        if selected_account
        else str(invoice.account_id),
        "account_x_model": "accountId",
        "draft_idempotency_key": secrets.token_urlsafe(24),
        "discount_applied_date": discount_applied_date.isoformat(),
    }
