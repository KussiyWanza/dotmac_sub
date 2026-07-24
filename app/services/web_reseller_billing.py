"""Reseller portal billing page builders.

Pairs with ``app/web/reseller/billing_routes.py`` and the Jinja templates
under ``templates/reseller/billing/``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from urllib.parse import quote_plus

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.schemas.billing import BillingAccountCreditAllocationConfirm
from app.services import customer_portal_flow_payments as customer_payments
from app.services import payment_proofs, reseller_portal, reseller_portal_billing
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.web.reseller.branding import get_reseller_templates

logger = logging.getLogger(__name__)

templates = get_reseller_templates()


def _require_reseller_context(request: Request, db: Session):
    context = reseller_portal.get_context(
        db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    return context


def _login_subscriber_id(context) -> str | None:
    """The login subscriber's id for subscriber-keyed features (saved cards).

    None for a first-class reseller_user principal (Layer 3) — those have no
    backing subscriber, so saved-card flows degrade to empty rather than error.
    """
    subscriber = context.get("subscriber")
    return str(subscriber.id) if subscriber is not None else None


def billing_overview(
    request: Request,
    db: Session,
    allocated: str | None = None,
    error: str | None = None,
    subscriber_search: str | None = None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    summary = reseller_portal_billing.get_billing_account_summary(
        db, reseller_id, subscriber_search=subscriber_search
    )
    return templates.TemplateResponse(
        "reseller/billing/index.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "saved_cards": reseller_portal_billing.list_payment_methods(
                db, _login_subscriber_id(context), reseller_id
            ),
            "billing_activity": reseller_portal_billing.account_activity(
                db, reseller_id, summary
            ),
            "allocation_success": allocated,
            "allocation_error": error,
            "payment_options": [
                opt
                for opt in customer_payments._topup_payment_options(db)
                if opt["provider_type"] != "direct_bank_transfer"
            ],
            "bank_transfer_accounts": (
                customer_payments.enabled_direct_bank_transfer_accounts(db)
            ),
            "direct_bank_transfer_enabled": (
                customer_payments.direct_bank_transfer_enabled(db)
            ),
            **summary,
        },
    )


def payment_methods(request: Request, db: Session, saved=None, error=None):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    page_data = reseller_portal_billing.get_payment_methods_page(
        db, reseller_id, _login_subscriber_id(context)
    )
    return templates.TemplateResponse(
        "reseller/billing/payment_methods.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "success": saved,
            "form_error": error,
            **page_data,
        },
    )


def payment_method_set_default(request: Request, db: Session, method_id: str):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    ok = reseller_portal_billing.set_default_payment_method(
        db, _login_subscriber_id(context), method_id, str(context["reseller"].id)
    )
    if not ok:
        return RedirectResponse(
            url="/reseller/billing/payment-methods?error="
            + quote_plus("Card not found."),
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/billing/payment-methods?saved="
        + quote_plus("Default card updated."),
        status_code=303,
    )


def payment_method_remove(request: Request, db: Session, method_id: str):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    removed = reseller_portal_billing.remove_payment_method(
        db, _login_subscriber_id(context), method_id, str(context["reseller"].id)
    )
    if not removed:
        return RedirectResponse(
            url="/reseller/billing/payment-methods?error="
            + quote_plus("Card not found."),
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/billing/payment-methods?saved=" + quote_plus("Card removed."),
        status_code=303,
    )


def preview_subscriber_funds_allocation(
    request: Request, db: Session, subscriber_id: str, amount: str | None = None
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        result = reseller_portal_billing.preview_unallocated_to_subscriber(
            db, reseller_id, subscriber_id, amount=amount
        )
    except ValueError as exc:
        return RedirectResponse(
            url="/reseller/billing?error=" + quote_plus(str(exc)),
            status_code=303,
        )
    return templates.TemplateResponse(
        "reseller/billing/allocation_confirm.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "preview": result["preview"],
            "idempotency_key": result["idempotency_key"],
        },
    )


def confirm_subscriber_funds_allocation(
    request: Request,
    db: Session,
    subscriber_id: str,
    amount: str,
    preview_fingerprint: str,
    idempotency_key: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    current_user = context.get("current_user") or {}
    actor_id = (
        current_user.get("actor_id")
        or current_user.get("principal_id")
        or current_user.get("subscriber_id")
        or current_user.get("id")
    )
    try:
        result = reseller_portal_billing.confirm_unallocated_to_subscriber(
            db,
            str(context["reseller"].id),
            subscriber_id,
            BillingAccountCreditAllocationConfirm(
                amount=Decimal(amount),
                preview_fingerprint=preview_fingerprint,
                idempotency_key=idempotency_key,
            ),
            actor_id=str(actor_id) if actor_id else None,
        )
    except ValueError as exc:
        return RedirectResponse(
            url="/reseller/billing?error=" + quote_plus(str(exc)),
            status_code=303,
        )
    message = (
        f"Allocated {result.currency} {result.amount:,.2f}; "
        f"ledger evidence {result.billing_account_ledger_entry_id}."
    )
    return RedirectResponse(
        url="/reseller/billing?allocated=" + quote_plus(message),
        status_code=303,
    )


def billing_pay_intent(
    request: Request,
    db: Session,
    amount: str,
    *,
    provider: str | None = None,
    payment_method_id: str | None = None,
    save_card: bool = False,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        intent = reseller_portal_billing.start_consolidated_payment(
            db,
            reseller_id,
            Decimal(amount),
            provider=provider or None,
            payment_method_id=payment_method_id or None,
            save_card=save_card,
            login_subscriber_id=_login_subscriber_id(context),
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "reseller/billing/pay_error.html",
            {
                "request": request,
                "active_page": "billing",
                "reseller": context["reseller"],
                "current_user": context["current_user"],
                "error": str(exc),
            },
            status_code=400,
        )
    # A saved card was charged server-to-server: skip the gateway popup and go
    # straight to verification, which records the payment.
    if intent.get("charged"):
        return RedirectResponse(
            url="/reseller/billing/pay/verify?reference="
            + quote_plus(intent["reference"])
            + "&provider="
            + quote_plus(intent["provider_type"]),
            status_code=303,
        )
    return templates.TemplateResponse(
        "reseller/billing/pay_checkout.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "intent": intent,
        },
    )


def billing_pay_verify(
    request: Request, db: Session, reference: str, provider: str | None = None
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    try:
        result = reseller_portal_billing.verify_and_record_consolidated_payment(
            db, reseller_id, reference, provider=provider
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "reseller/billing/pay_error.html",
            {
                "request": request,
                "active_page": "billing",
                "reseller": context["reseller"],
                "current_user": context["current_user"],
                "error": str(exc),
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        "reseller/billing/pay_success.html",
        {
            "request": request,
            "active_page": "billing",
            "reseller": context["reseller"],
            "current_user": context["current_user"],
            "result": result,
        },
    )


async def billing_submit_transfer_proof(
    request: Request,
    db: Session,
    *,
    file,
    amount: str,
    bank_name: str | None = None,
    reference: str | None = None,
):
    """Record a reseller bulk bank-transfer receipt for unallocated credit."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    reseller_id = str(context["reseller"].id)
    from app.services import billing as billing_service

    try:
        ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
        path = await payment_proofs.save_proof_file(file)
        submitted_by = _login_subscriber_id(context)
        currency = ba.currency
        billing_account_id = str(ba.id)
        db_session_adapter.release_read_transaction(db)
        payment_proofs.submit_proof(
            db,
            None,
            context=CommandContext.system(
                actor=f"reseller:{submitted_by or reseller_id}",
                scope=payment_proofs.SUBMISSION_SCOPE,
                reason="Reseller portal consolidated transfer evidence submission",
            ),
            submitted_by=submitted_by,
            amount=amount,
            currency=currency,
            bank_name=bank_name,
            reference=reference,
            file_path=path,
            billing_account_id=billing_account_id,
        )
    except Exception as exc:  # noqa: BLE001 - surface as a friendly redirect
        detail = exc.message if isinstance(exc, DomainError) else str(exc)
        return RedirectResponse(
            url="/reseller/billing?error="
            + quote_plus(f"Could not submit receipt: {detail}"),
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/billing?allocated="
        + quote_plus(
            "Transfer receipt submitted — we will verify it and credit your "
            "account, then you can allocate it to invoices."
        ),
        status_code=303,
    )
