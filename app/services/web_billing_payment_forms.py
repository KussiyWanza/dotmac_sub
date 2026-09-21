"""Form/dependency helpers for admin billing payment routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import uuid4

from app.models.billing import InvoiceStatus
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.services import billing as billing_service
from app.services import settings_spec
from app.services import subscriber as subscriber_service

logger = logging.getLogger(__name__)


def _default_currency(db) -> str:
    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    code = str(value or "NGN").strip().upper()
    return code or "NGN"


def new_manual_payment_idempotency_token() -> str:
    """Return one stable token for a rendered manual-payment form."""
    return uuid4().hex


def account_label(account) -> str:
    """Build a human label for account/subscriber objects."""
    if not account:
        return "Account"
    name = str(getattr(account, "name", "") or "").strip()
    if name:
        return name
    label = f"{getattr(account, 'first_name', '')} {getattr(account, 'last_name', '')}".strip()
    if label:
        return label
    if getattr(account, "display_name", None):
        return str(account.display_name)
    if getattr(account, "account_number", None):
        return f"Account {account.account_number}"
    return "Account"


def filter_open_invoices(invoices: list) -> list:
    return [
        invoice
        for invoice in invoices
        if invoice.status
        in {InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue}
    ]


def resolve_invoice(db, invoice_id: str | None):
    if not invoice_id:
        return None
    try:
        return billing_service.invoices.get(db=db, invoice_id=invoice_id)
    except Exception:
        return None


def invoice_balance_info(invoice) -> tuple[str | None, str | None]:
    if not invoice:
        return None, None
    # Use balance_due even when it is 0 (a fully-paid invoice); only fall back to
    # the total when balance_due was never computed (legacy/None). Otherwise a
    # paid invoice shows its full total as "balance due" and the staff member
    # records a duplicate payment, overpaying the account.
    balance = invoice.balance_due if invoice.balance_due is not None else invoice.total
    if balance is None:
        return None, None
    value = f"{balance:.2f}"
    display = f"{invoice.currency} {balance:,.2f}"
    return value, display


def build_new_form_state(
    db,
    *,
    invoice_id: str | None,
    invoice_alias: str | None,
    account_id: str | None,
    account_alias: str | None,
    amount: str | None = None,
) -> dict[str, object]:
    """Build state for payment create form."""
    prefill: dict[str, object] = {}
    resolved_invoice_id = invoice_id or invoice_alias
    resolved_account_id = account_id or account_alias
    selected_account = None
    invoice_label = None
    balance_value = None
    balance_display = None

    if resolved_invoice_id:
        invoice_obj = resolve_invoice(db, resolved_invoice_id)
        if invoice_obj:
            prefill["invoice_id"] = str(invoice_obj.id)
            prefill["invoice_number"] = invoice_obj.invoice_number
            invoice_label = invoice_obj.invoice_number or "Invoice"
            balance_value, balance_display = invoice_balance_info(invoice_obj)
            if invoice_obj.balance_due is not None:
                prefill["amount"] = float(invoice_obj.balance_due)
            elif invoice_obj.total:
                prefill["amount"] = float(invoice_obj.total)
            if invoice_obj.currency:
                prefill["currency"] = invoice_obj.currency
            prefill["status"] = "succeeded"
            resolved_account_id = str(invoice_obj.account_id)

    if resolved_account_id:
        prefill["account_id"] = resolved_account_id
        try:
            selected_account = subscriber_service.accounts.get(
                db=db, account_id=resolved_account_id
            )
        except Exception:
            selected_account = None

    # An account-scoped workflow may suggest the amount being received, but it
    # never asserts settlement. The normal payment preview/confirmation owner
    # still validates and records the receipt. Invoice-specific amounts remain
    # authoritative and therefore take precedence over this convenience value.
    if not resolved_invoice_id and amount:
        try:
            suggested_amount = Decimal(amount)
        except (InvalidOperation, ValueError):
            suggested_amount = Decimal("0.00")
        if suggested_amount.is_finite() and suggested_amount > 0:
            prefill["amount"] = suggested_amount

    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    invoices = []
    if selected_account:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=str(selected_account.id),
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        invoices = filter_open_invoices(invoices)
        selected_invoice_id = prefill.get("invoice_id")
        if selected_invoice_id:
            invoice_obj = resolve_invoice(db, str(selected_invoice_id))
            if invoice_obj and all(
                str(inv.id) != str(invoice_obj.id) for inv in invoices
            ):
                invoices = [invoice_obj, *invoices]

    return {
        "prefill": prefill,
        "selected_account": selected_account,
        "invoice_label": invoice_label,
        "balance_value": balance_value,
        "balance_display": balance_display,
        "collection_accounts": collection_accounts,
        "invoices": invoices,
        "default_currency": _default_currency(db),
    }


def load_invoice_options_state(
    db,
    *,
    account_id: str | None,
    invoice_id: str | None,
) -> dict[str, object]:
    """Build state for invoice options HTMX partial."""
    selected_account = None
    if account_id:
        try:
            selected_account = subscriber_service.accounts.get(
                db=db, account_id=account_id
            )
        except Exception:
            selected_account = None
    invoices = []
    if selected_account:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=str(selected_account.id),
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        invoices = filter_open_invoices(invoices)
    invoice_label = None
    if invoice_id:
        invoice_obj = resolve_invoice(db, invoice_id)
        if invoice_obj:
            invoice_label = invoice_obj.invoice_number or "Invoice"
            if all(str(inv.id) != str(invoice_obj.id) for inv in invoices):
                invoices = [invoice_obj, *invoices]
    return {
        "selected_account": selected_account,
        "invoices": invoices,
        "invoice_label": invoice_label,
    }


def load_invoice_currency_state(
    db,
    *,
    invoice_id: str | None,
    currency: str | None,
) -> dict[str, object]:
    """Build state for invoice currency HTMX partial."""
    invoice_obj = resolve_invoice(db, invoice_id)
    currency_value = currency or _default_currency(db)
    currency_locked = False
    if invoice_obj and invoice_obj.currency:
        currency_value = invoice_obj.currency
        currency_locked = True
    return {"currency_value": currency_value, "currency_locked": currency_locked}


def load_invoice_details_state(
    db,
    *,
    invoice_id: str | None,
    amount: str | None,
) -> dict[str, object]:
    """Build state for invoice amount/details HTMX partial."""
    invoice_obj = resolve_invoice(db, invoice_id)
    amount_value = amount or ""
    balance_value = None
    balance_display = None
    if invoice_obj:
        balance_value, balance_display = invoice_balance_info(invoice_obj)
        if balance_value:
            amount_value = balance_value
    return {
        "amount_value": amount_value,
        "balance_value": balance_value,
        "balance_display": balance_display,
    }


def build_create_error_context(
    *,
    error: str,
    deps: dict[str, object],
    resolved_invoice,
    invoice_id: str | None,
    payment_method_id: str | None = None,
    idempotency_token: str | None = None,
) -> dict[str, object]:
    selected_account = cast(Subscriber | None, deps.get("selected_account"))
    return {
        "accounts": None,
        "payment_methods": deps["payment_methods"],
        "payment_method_types": deps["payment_method_types"],
        "collection_accounts": deps["collection_accounts"],
        "invoices": deps["invoices"],
        "action_url": "/admin/billing/payments/create/preview",
        "form_title": "Record Payment",
        "submit_label": "Preview Payment",
        "error": error,
        "active_page": "payments",
        "active_menu": "billing",
        "account_locked": bool(selected_account),
        "account_label": account_label(selected_account) if selected_account else None,
        "account_number": selected_account.account_number if selected_account else None,
        "selected_account_id": str(selected_account.id) if selected_account else None,
        "currency_locked": bool(resolved_invoice),
        "show_invoice_typeahead": not bool(selected_account),
        "selected_invoice_id": invoice_id,
        "selected_payment_method_id": payment_method_id,
        "idempotency_token": idempotency_token
        or new_manual_payment_idempotency_token(),
    }


def build_edit_error_context(
    *,
    payment,
    payment_id,
    error: str,
    deps: dict[str, object],
    selected_account,
) -> dict[str, object]:
    primary_invoice_id = deps["primary_invoice_id"]
    return {
        "accounts": None,
        "payment_methods": deps["payment_methods"],
        "payment_method_types": deps["payment_method_types"],
        "invoices": deps["invoices"],
        "payment": payment,
        "action_url": f"/admin/billing/payments/{payment_id}/edit",
        "form_title": "Edit Payment",
        "submit_label": "Save Changes",
        "error": error,
        "active_page": "payments",
        "active_menu": "billing",
        "account_locked": True,
        "account_label": account_label(selected_account),
        "account_number": selected_account.account_number if selected_account else None,
        "selected_account_id": str(selected_account.id)
        if selected_account
        else str(payment.account_id)
        if payment
        else None,
        "currency_locked": bool(primary_invoice_id) if payment else False,
        "show_invoice_typeahead": False,
        "selected_invoice_id": primary_invoice_id,
        "balance_value": deps["balance_value"],
        "balance_display": deps["balance_display"],
    }


def load_create_error_dependencies(
    db,
    *,
    account_id: str | None,
    resolved_invoice,
) -> dict[str, object]:
    """Load dependencies for payment create error re-render."""
    selected_account = None
    if account_id or resolved_invoice:
        try:
            selected_account = subscriber_service.accounts.get(
                db=db,
                account_id=account_id or str(resolved_invoice.account_id),
            )
        except Exception:
            selected_account = None
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    invoices = []
    payment_methods = []
    if selected_account:
        payment_methods = billing_service.payment_methods.list(
            db=db,
            account_id=str(selected_account.id),
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        invoices = billing_service.invoices.list(
            db=db,
            account_id=str(selected_account.id),
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        invoices = filter_open_invoices(invoices)
    return {
        "selected_account": selected_account,
        "payment_methods": payment_methods,
        "payment_method_types": payment_method_type_options(),
        "collection_accounts": collection_accounts,
        "invoices": invoices,
    }


def payment_method_type_options() -> list[str]:
    return [
        "cash",
        "transfer",
        "card",
        "bank_account",
        "check",
        "other",
    ]


def load_edit_dependencies(
    db,
    *,
    payment,
    selected_account,
) -> dict[str, object]:
    """Load dependencies for payment edit + edit error re-render."""
    payment_methods = billing_service.payment_methods.list(
        db=db,
        account_id=str(payment.account_id),
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    payment_method_types = payment_method_type_options()
    if payment.payment_method_id and all(
        str(method.id) != str(payment.payment_method_id) for method in payment_methods
    ):
        method = billing_service.payment_methods.get(db, str(payment.payment_method_id))
        if method:
            payment_methods = [*payment_methods, method]
    invoices = billing_service.invoices.list(
        db=db,
        account_id=str(selected_account.id) if selected_account else None,
        status=None,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=200,
        offset=0,
    )
    invoices = filter_open_invoices(invoices)
    primary_invoice_id = None
    if payment and payment.allocations:
        allocation = min(
            payment.allocations,
            key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
        )
        primary_invoice_id = str(allocation.invoice_id)
    invoice_obj = None
    if primary_invoice_id:
        try:
            invoice_obj = billing_service.invoices.get(
                db=db, invoice_id=primary_invoice_id
            )
            if all(str(inv.id) != str(invoice_obj.id) for inv in invoices):
                invoices = [invoice_obj, *invoices]
        except Exception:
            invoice_obj = None
    balance_value, balance_display = invoice_balance_info(invoice_obj)
    return {
        "payment_methods": payment_methods,
        "payment_method_types": payment_method_types,
        "invoices": invoices,
        "primary_invoice_id": primary_invoice_id,
        "balance_value": balance_value,
        "balance_display": balance_display,
    }
