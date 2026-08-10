"""Admin billing payments routes."""

from datetime import date
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import Payment
from app.models.subscriber import Subscriber
from app.schemas.billing import PaymentSettlementReconciliationRequest
from app.services import billing as billing_service
from app.services import billing_payment_receipts as payment_receipts_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services import web_billing_payment_forms as web_billing_payment_forms_service
from app.services import web_billing_payments as web_billing_payments_service
from app.services import (
    web_billing_reconciliation as web_billing_reconciliation_service,
)
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import require_permission
from app.services.inclusive_date_range import InclusiveDateRangeError
from app.web.request_parsing import parse_json_body

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


@router.get(
    "/payments",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payments_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    unallocated_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        list_query = web_billing_payments_service.build_payments_list_query(
            customer_ref=customer_ref,
            partner_id=partner_id,
            status=status,
            method=method,
            search=search,
            start_date=start_date,
            end_date=end_date,
            unallocated_only=unallocated_only,
            page=page,
            per_page=per_page,
        )
    except InclusiveDateRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError:
        # Out-of-contract sort/page params (e.g. hand-edited URL) fall back to
        # the contract defaults rather than erroring the page.
        list_query = web_billing_payments_service.build_payments_list_query(
            page=page, unallocated_only=unallocated_only
        )

    state = web_billing_payments_service.build_payments_list_data(
        db,
        list_query=list_query,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payments.html",
        {
            "request": request,
            **state,
            "page_heading": "Unallocated Payments" if unallocated_only else "Payments",
            "page_subtitle": "Payments with no invoice allocations"
            if unallocated_only
            else "Track all payment transactions and collections",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/payments/export.csv",
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payments_export_csv(
    request: Request,
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    unallocated_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        list_query = web_billing_payments_service.build_payments_list_query(
            customer_ref=customer_ref,
            partner_id=partner_id,
            status=status,
            method=method,
            search=search,
            start_date=start_date,
            end_date=end_date,
            unallocated_only=unallocated_only,
        )
    except (InclusiveDateRangeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = web_billing_payments_service.stream_payments_csv(db, list_query=list_query)
    return StreamingResponse(
        rows,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="payments_export.csv"'},
    )


@router.get(
    "/payments/unallocated",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payments_unallocated(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: Session = Depends(get_db),
):
    return payments_list(
        request=request,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
        partner_id=partner_id,
        status=status,
        method=method,
        search=search,
        start_date=start_date,
        end_date=end_date,
        unallocated_only=True,
        db=db,
    )


@router.get(
    "/payments/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def payment_new(
    request: Request,
    invoice_id: str | None = Query(None),
    invoice: str | None = Query(None),
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.build_new_form_state(
        db,
        invoice_id=invoice_id,
        invoice_alias=invoice,
        account_id=account_id,
        account_alias=account,
    )
    selected_account = cast(Subscriber | None, state["selected_account"])
    prefill = cast(dict[str, Any], state["prefill"])
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
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": None,
            "payment_methods": payment_methods,
            "payment_method_types": web_billing_payment_forms_service.payment_method_type_options(),
            "collection_accounts": state["collection_accounts"],
            "invoices": state["invoices"],
            "prefill": prefill,
            "invoice_label": state["invoice_label"],
            "action_url": "/admin/billing/payments/create/preview",
            "form_title": "Record Payment",
            "submit_label": "Preview Payment",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": bool(selected_account),
            "account_label": web_billing_payment_forms_service.account_label(
                selected_account
            )
            if selected_account
            else None,
            "account_number": selected_account.account_number
            if selected_account
            else None,
            "selected_account_id": str(selected_account.id)
            if selected_account
            else None,
            "currency_locked": bool(prefill.get("invoice_id")),
            "show_invoice_typeahead": not bool(selected_account),
            "selected_invoice_id": prefill.get("invoice_id"),
            "idempotency_token": web_billing_payment_forms_service.new_manual_payment_idempotency_token(),
            "balance_value": state["balance_value"],
            "balance_display": state["balance_display"],
            "default_currency": state["default_currency"],
        },
    )


@router.get(
    "/payments/invoice-options",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_invoice_options(
    request: Request,
    account_id: str | None = Query(None),
    invoice_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.load_invoice_options_state(
        db,
        account_id=account_id,
        invoice_id=invoice_id,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_invoice_select.html",
        {
            "request": request,
            "invoices": state["invoices"],
            "selected_invoice_id": invoice_id,
            "invoice_label": state["invoice_label"],
            "show_invoice_typeahead": not bool(state["selected_account"]),
        },
    )


@router.get(
    "/payments/invoice-currency",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_invoice_currency(
    request: Request,
    invoice_id: str | None = Query(None),
    currency: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.load_invoice_currency_state(
        db,
        invoice_id=invoice_id,
        currency=currency,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_currency_field.html",
        {
            "request": request,
            "currency_value": state["currency_value"],
            "currency_locked": state["currency_locked"],
        },
    )


@router.get(
    "/payments/invoice-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_invoice_details(
    request: Request,
    invoice_id: str | None = Query(None),
    amount: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.load_invoice_details_state(
        db,
        invoice_id=invoice_id,
        amount=amount,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_amount_field.html",
        {
            "request": request,
            "amount_value": state["amount_value"],
            "balance_value": state["balance_value"],
            "balance_display": state["balance_display"],
        },
    )


@router.get(
    "/customer-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def billing_customer_accounts(
    request: Request,
    customer_ref: str | None = Query(None),
    account_id: str | None = Query(None),
    account_x_model: str | None = Query(None),
    db: Session = Depends(get_db),
):
    accounts = web_billing_customers_service.accounts_for_customer(db, customer_ref)
    return templates.TemplateResponse(
        "admin/billing/_customer_account_select.html",
        {
            "request": request,
            "customer_ref": customer_ref,
            "accounts": accounts,
            "selected_account_id": account_id,
            "account_x_model": account_x_model,
        },
    )


@router.get(
    "/customer-subscribers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def billing_customer_subscribers(
    request: Request,
    customer_ref: str | None = Query(None),
    subscriber_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    subscribers = web_billing_customers_service.subscribers_for_customer(
        db, customer_ref
    )
    return templates.TemplateResponse(
        "admin/billing/_customer_subscriber_select.html",
        {
            "request": request,
            "customer_ref": customer_ref,
            "subscribers": subscribers,
            "selected_subscriber_id": subscriber_id,
        },
    )


def _payment_create_error_response(
    request: Request,
    db: Session,
    exc: Exception,
    *,
    account_id: str | None,
    invoice_id: str | None,
    payment_method_id: str | None,
    idempotency_token: str | None,
) -> Response:
    from fastapi import HTTPException as _HTTPException
    from pydantic import ValidationError as _ValidationError

    resolved_invoice = None
    balance_value = None
    balance_display = None
    if invoice_id:
        try:
            resolved_invoice = web_billing_payment_forms_service.resolve_invoice(
                db, invoice_id
            )
            if resolved_invoice:
                (
                    balance_value,
                    balance_display,
                ) = web_billing_payment_forms_service.invoice_balance_info(
                    resolved_invoice
                )
        except Exception:
            resolved_invoice = None
    if isinstance(exc, _ValidationError):
        errs = exc.errors()
        if errs:
            first = errs[0]
            field = str((first.get("loc") or ("value",))[-1]).replace("_", " ")
            msg = first.get("msg", "is invalid")
            error_message = f"{field[:1].upper()}{field[1:]}: {msg}"
        else:
            error_message = "Invalid input"
    elif isinstance(exc, _HTTPException):
        error_message = str(exc.detail)
    else:
        error_message = str(exc)
    deps = cast(
        dict[str, object],
        web_billing_payment_forms_service.load_create_error_dependencies(
            db,
            account_id=account_id,
            resolved_invoice=resolved_invoice,
        ),
    )
    error_state = web_billing_payment_forms_service.build_create_error_context(
        error=error_message,
        deps=deps,
        resolved_invoice=resolved_invoice,
        invoice_id=invoice_id,
        payment_method_id=payment_method_id,
        idempotency_token=idempotency_token,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            **error_state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "balance_value": balance_value,
            "balance_display": balance_display,
        },
        status_code=400,
    )


@router.post(
    "/payments/create/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def payment_create_preview(
    request: Request,
    account_id: str | None = Form(None),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    status: str | None = Form(None),
    invoice_id: str | None = Form(None),
    collection_account_id: str | None = Form(None),
    payment_method_id: str | None = Form(None),
    memo: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        result = web_billing_payments_service.preview_payment_create(
            db,
            account_id=account_id,
            amount=amount,
            currency=currency,
            status=status,
            invoice_id=invoice_id,
            collection_account_id=collection_account_id,
            payment_method_id=payment_method_id,
            memo=memo,
        )
    except Exception as exc:
        db.rollback()
        return _payment_create_error_response(
            request,
            db,
            exc,
            account_id=account_id,
            invoice_id=invoice_id,
            payment_method_id=payment_method_id,
            idempotency_token=idempotency_token,
        )
    idempotency_token = (
        idempotency_token
        or web_billing_payment_forms_service.new_manual_payment_idempotency_token()
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_create_confirm.html",
        {
            "request": request,
            "preview": result["preview"],
            "account_id": account_id,
            "amount": amount,
            "currency": currency,
            "status": status,
            "invoice_id": invoice_id,
            "collection_account_id": collection_account_id,
            "payment_method_id": payment_method_id,
            "memo": memo,
            "idempotency_token": idempotency_token,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_page": "payments",
            "active_menu": "billing",
        },
    )


@router.post(
    "/payments/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:create"))],
)
def payment_create(
    request: Request,
    account_id: str | None = Form(None),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    status: str | None = Form(None),
    invoice_id: str | None = Form(None),
    collection_account_id: str | None = Form(None),
    payment_method_id: str | None = Form(None),
    memo: str | None = Form(None),
    idempotency_token: str | None = Form(None),
    preview_fingerprint: str = Form(...),
    db: Session = Depends(get_db),
):
    resolved_invoice = None
    balance_value = None
    balance_display = None
    try:
        result = web_billing_payments_service.process_payment_create_with_audit(
            db,
            request,
            account_id=account_id,
            amount=amount,
            currency=currency,
            status=status,
            invoice_id=invoice_id,
            collection_account_id=collection_account_id,
            payment_method_id=payment_method_id,
            memo=memo,
            idempotency_token=idempotency_token,
            preview_fingerprint=preview_fingerprint,
        )
        payment = cast(Payment, result["payment"])
        resolved_invoice = result["resolved_invoice"]
        balance_value = result["balance_value"]
        balance_display = result["balance_display"]
    except Exception as exc:
        db.rollback()
        if not resolved_invoice and invoice_id:
            try:
                resolved_invoice = web_billing_payment_forms_service.resolve_invoice(
                    db, invoice_id
                )
                if resolved_invoice:
                    (
                        balance_value,
                        balance_display,
                    ) = web_billing_payment_forms_service.invoice_balance_info(
                        resolved_invoice
                    )
            except Exception:
                resolved_invoice = None
        # Surface a clean message instead of the raw Pydantic ValidationError
        # dump (which leaks internal type names and a pydantic.dev URL to staff).
        from fastapi import HTTPException as _HTTPException
        from pydantic import ValidationError as _ValidationError

        if isinstance(exc, _ValidationError):
            errs = exc.errors()
            if errs:
                first = errs[0]
                field = (
                    str((first.get("loc") or ("value",))[-1]).replace("_", " ").strip()
                )
                msg = first.get("msg", "is invalid")
                error_message = (
                    f"{field[:1].upper()}{field[1:]}: {msg}" if field else msg
                )
            else:
                error_message = "Invalid input"
        elif isinstance(exc, _HTTPException):
            error_message = str(exc.detail)
        else:
            error_message = str(exc)
        deps = cast(
            dict[str, object],
            web_billing_payment_forms_service.load_create_error_dependencies(
                db,
                account_id=account_id,
                resolved_invoice=resolved_invoice,
            ),
        )
        error_state = web_billing_payment_forms_service.build_create_error_context(
            error=error_message,
            deps=deps,
            resolved_invoice=resolved_invoice,
            invoice_id=invoice_id,
            payment_method_id=payment_method_id,
            idempotency_token=idempotency_token,
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                **error_state,
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "balance_value": balance_value,
                "balance_display": balance_display,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.get(
    "/payments/{payment_id:uuid}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_detail(
    request: Request, payment_id: UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    state = web_billing_payments_service.build_payment_detail_data(
        db, payment_id=str(payment_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_detail.html",
        {
            "request": request,
            **state,
            "activities": build_audit_activities(
                db, "payment", str(payment_id), limit=10
            ),
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/payments/{payment_id:uuid}/receipt/pdf",
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_receipt_pdf(payment_id: UUID, db: Session = Depends(get_db)) -> Response:
    """Download the authoritative receipt for a successful payment."""
    detail = payment_receipts_service.get_payment_receipt_context(db, str(payment_id))
    pdf_bytes = payment_receipts_service.build_receipt_pdf(detail)

    from app.services.file_storage import build_content_disposition

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(
                payment_receipts_service.download_filename(detail["payment"])
            )
        },
    )


@router.get(
    "/payments/{payment_id:uuid}/settlement/evidence",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
def payment_settlement_evidence(
    request: Request, payment_id: UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    evidence = billing_service.payments.inspect_settlement_evidence(db, str(payment_id))
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_settlement_evidence.html",
        {
            "request": request,
            "evidence": evidence,
            "payment_id": payment_id,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_page": "payments",
            "active_menu": "billing",
        },
    )


@router.post(
    "/payments/{payment_id:uuid}/settlement/evidence/reconcile",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:ledger:write"))],
)
async def reconcile_payment_settlement_evidence(
    request: Request, payment_id: UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    form = await request.form()
    evidence = billing_service.payments.inspect_settlement_evidence(db, str(payment_id))
    allocation_ledger_entry_ids = {}
    for allocation_id in cast(list[UUID], evidence["active_allocation_ids"]):
        selected = form.get(f"allocation_{allocation_id}")
        if selected:
            allocation_ledger_entry_ids[allocation_id] = UUID(str(selected))
    unallocated = form.get("unallocated_ledger_entry_id")
    payload = PaymentSettlementReconciliationRequest(
        allocation_ledger_entry_ids=allocation_ledger_entry_ids,
        unallocated_ledger_entry_id=UUID(str(unallocated)) if unallocated else None,
        reason=str(form.get("reason", "")).strip(),
    )
    web_billing_payments_service.reconcile_payment_settlement_evidence_with_audit(
        db, request, payment_id=str(payment_id), payload=payload
    )
    return RedirectResponse(
        url=f"/admin/billing/payments/{payment_id}", status_code=303
    )


@router.get(
    "/payments/{payment_id:uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_edit(
    request: Request, payment_id: UUID, db: Session = Depends(get_db)
) -> HTMLResponse:
    state = web_billing_payments_service.build_payment_edit_data(
        db, payment_id=str(payment_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    payment = cast(Payment, state["payment"])
    selected_account = cast(Subscriber | None, state["selected_account"])
    primary_invoice_id = state["primary_invoice_id"]
    deps = cast(dict[str, Any], state["deps"])
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": None,
            "payment_methods": deps["payment_methods"],
            "payment_method_types": deps["payment_method_types"],
            "invoices": deps["invoices"],
            "payment": payment,
            "action_url": f"/admin/billing/payments/{payment_id}/edit",
            "form_title": "Edit Payment",
            "submit_label": "Save Changes",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": True,
            "account_label": web_billing_payment_forms_service.account_label(
                selected_account
            ),
            "account_number": selected_account.account_number
            if selected_account
            else None,
            "selected_account_id": str(selected_account.id)
            if selected_account
            else str(payment.account_id),
            "currency_locked": bool(primary_invoice_id),
            "show_invoice_typeahead": False,
            "selected_invoice_id": primary_invoice_id,
            "balance_value": deps["balance_value"],
            "balance_display": deps["balance_display"],
        },
    )


@router.post(
    "/payments/{payment_id:uuid}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_update(
    request: Request,
    payment_id: UUID,
    account_id: str | None = Form(None),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    status: str | None = Form(None),
    invoice_id: str | None = Form(None),
    payment_method_id: str | None = Form(None),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_billing_payments_service.process_payment_update_with_audit(
            db,
            request,
            payment_id=str(payment_id),
            account_id=account_id,
            amount=amount,
            currency=currency,
            status=status,
            invoice_id=invoice_id,
            payment_method_id=payment_method_id,
            memo=memo,
        )
    except Exception as exc:
        db.rollback()
        edit_state = web_billing_payments_service.build_payment_edit_data(
            db, payment_id=str(payment_id)
        )
        payment = edit_state["payment"] if edit_state else None
        selected_account = edit_state["selected_account"] if edit_state else None
        deps = cast(dict[str, object], edit_state["deps"]) if edit_state else {}
        error_state = web_billing_payment_forms_service.build_edit_error_context(
            payment=payment,
            payment_id=payment_id,
            error=str(exc),
            deps=deps,
            selected_account=selected_account,
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                **error_state,
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.post(
    "/payments/{payment_id:uuid}/allocation/preview",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_allocation_preview(
    request: Request,
    payment_id: UUID,
    invoice_id: str = Form(...),
    amount: str = Form(...),
    db: Session = Depends(get_db),
):
    state = web_billing_payments_service.preview_payment_allocation(
        db,
        payment_id=str(payment_id),
        invoice_id=invoice_id,
        amount=amount,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_allocation_confirm.html",
        {
            "request": request,
            "payment_id": payment_id,
            "invoice_id": invoice_id,
            "amount": amount,
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_page": "payments",
            "active_menu": "billing",
        },
    )


@router.post(
    "/payments/{payment_id:uuid}/allocation",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_allocation(
    request: Request,
    payment_id: UUID,
    invoice_id: str = Form(...),
    amount: str = Form(...),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    web_billing_payments_service.process_payment_allocation_with_audit(
        db,
        request,
        payment_id=str(payment_id),
        invoice_id=invoice_id,
        amount=amount,
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
    )
    return RedirectResponse(
        url=f"/admin/billing/payments/{payment_id}", status_code=303
    )


@router.post(
    "/payments/{payment_id:uuid}/settlement/preview",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_settlement_preview(
    request: Request,
    payment_id: UUID,
    db: Session = Depends(get_db),
):
    state = web_billing_payments_service.preview_payment_settlement(
        db, payment_id=str(payment_id)
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_settlement_confirm.html",
        {
            "request": request,
            "payment_id": payment_id,
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_page": "payments",
            "active_menu": "billing",
        },
    )


@router.post(
    "/payments/{payment_id:uuid}/settlement",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_settlement(
    request: Request,
    payment_id: UUID,
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    web_billing_payments_service.process_payment_settlement_with_audit(
        db,
        request,
        payment_id=str(payment_id),
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
    )
    return RedirectResponse(
        url=f"/admin/billing/payments/{payment_id}", status_code=303
    )


@router.post(
    "/payments/{payment_id:uuid}/refund/preview",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_refund_preview(
    request: Request,
    payment_id: UUID,
    amount: str = Form(""),
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    """Preview distinct refund effects before confirmation."""
    state = web_billing_payments_service.preview_payment_refund(
        db,
        payment_id=str(payment_id),
        amount=amount,
        reason=reason,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_refund_confirm.html",
        {
            "request": request,
            "payment_id": payment_id,
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_page": "payments",
            "active_menu": "billing",
        },
    )


@router.post(
    "/payments/{payment_id:uuid}/refund",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_refund(
    request: Request,
    payment_id: UUID,
    amount: str = Form(...),
    reason: str = Form(""),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    """Record a completed refund after owner-authored confirmation."""
    web_billing_payments_service.process_payment_refund_with_audit(
        db,
        request,
        payment_id=str(payment_id),
        amount=amount,
        reason=reason,
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
    )
    return RedirectResponse(
        url=f"/admin/billing/payments/{payment_id}", status_code=303
    )


@router.post(
    "/payments/{payment_id:uuid}/reversal/preview",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_reversal_preview(
    request: Request,
    payment_id: UUID,
    reason: str = Form(..., min_length=3, max_length=500),
    db: Session = Depends(get_db),
):
    """Preview chargeback/bank-reversal effects before confirmation."""
    state = web_billing_payments_service.preview_payment_reversal(
        db,
        payment_id=str(payment_id),
        reason=reason,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_reversal_confirm.html",
        {
            "request": request,
            "payment_id": payment_id,
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "active_page": "payments",
            "active_menu": "billing",
        },
    )


@router.post(
    "/payments/{payment_id:uuid}/reversal",
    dependencies=[Depends(require_permission("billing:payment:update"))],
)
def payment_reversal(
    request: Request,
    payment_id: UUID,
    reason: str = Form(..., min_length=3, max_length=500),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    """Record a confirmed chargeback/bank reversal through its owner."""
    web_billing_payments_service.process_payment_reversal_with_audit(
        db,
        request,
        payment_id=str(payment_id),
        reason=reason,
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
    )
    return RedirectResponse(
        url=f"/admin/billing/payments/{payment_id}", status_code=303
    )


@router.get(
    "/payments/import",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:import:write"))],
)
def payment_import_page(
    request: Request,
    history_handler: str | None = Query(None),
    history_status: str | None = Query(None),
    history_date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    import_history = web_billing_payments_service.list_payment_import_history_filtered(
        db,
        limit=100,
        handler=history_handler,
        status=history_status,
        date_range=history_date_range,
    )
    return templates.TemplateResponse(
        "admin/billing/payment_import.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            "import_history": import_history,
            "history_handler": history_handler,
            "history_status": history_status,
            "history_date_range": history_date_range,
            "default_currency": web_billing_payments_service.resolve_default_currency(
                db
            ),
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/payments/reconciliation",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_reconciliation_page(
    request: Request,
    date_range: str | None = Query(None),
    handler: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_billing_reconciliation_service.build_reconciliation_data(
        db,
        date_range=date_range,
        handler=handler,
    )
    return templates.TemplateResponse(
        "admin/billing/payment_reconciliation.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/payments/import/history.csv",
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_import_history_csv(
    request: Request,
    history_handler: str | None = Query(None),
    history_status: str | None = Query(None),
    history_date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    rows = web_billing_payments_service.list_payment_import_history_filtered(
        db,
        limit=1000,
        handler=history_handler,
        status=history_status,
        date_range=history_date_range,
    )
    content = web_billing_payments_service.render_payment_import_history_csv(rows)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="payment_import_history.csv"'
        },
    )


@router.post(
    "/payments/import",
    dependencies=[Depends(require_permission("billing:import:write"))],
)
def payment_import_submit(
    request: Request,
    body: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    try:
        result = web_billing_payments_service.process_payment_import_payload_with_audit(
            db,
            request,
            body,
        )
        status_code = int(cast(int | str, result.get("status_code", 200)))
        if status_code != 200:
            return JSONResponse(
                cast(dict[str, object], result),
                status_code=status_code,
            )

        return JSONResponse(cast(dict[str, object], result.get("payload") or {}))

    except Exception as exc:
        return JSONResponse({"message": f"Import failed: {str(exc)}"}, status_code=500)


@router.get(
    "/payments/import/template",
    dependencies=[Depends(require_permission("billing:payment:read"))],
)
def payment_import_template():
    return Response(
        content=web_billing_payments_service.import_template_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=payment_import_template.csv"
        },
    )
