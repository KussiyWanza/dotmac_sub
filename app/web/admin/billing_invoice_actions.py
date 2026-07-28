"""Admin billing invoice action/detail routes."""

import secrets
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    web_billing_invoice_actions as web_billing_invoice_actions_service,
)
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services.auth_dependencies import require_permission
from app.services.domain_errors import DomainError

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


@router.post(
    "/invoices/{invoice_id:uuid}/convert-proforma",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_convert_proforma(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
):
    web_billing_invoices_service.convert_proforma_to_final_web(
        db,
        request=request,
        actor_id=_actor_id(request),
        invoice_id=str(invoice_id),
    )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}", status_code=303
    )


@router.get(
    "/invoices/{invoice_id:uuid}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_detail(
    request: Request,
    invoice_id: UUID,
    pdf_notice: str | None = Query(None),
    db: Session = Depends(get_db),
):
    detail_data = web_billing_invoices_service.load_invoice_detail_data(
        db,
        invoice_id=str(invoice_id),
    )
    if not detail_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/invoice_detail.html",
        {
            "request": request,
            **detail_data,
            "pdf_notice": pdf_notice,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/invoices/{invoice_id:uuid}/prepaid-pay-now/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_prepaid_pay_now_preview(
    request: Request, invoice_id: UUID, db: Session = Depends(get_db)
) -> Response:
    try:
        preview_context = (
            web_billing_invoices_service.prepaid_recovery_pay_now_preview_context(
                db, invoice_id=str(invoice_id)
            )
        )
    except DomainError as exc:
        return RedirectResponse(
            f"/admin/billing/invoices/{invoice_id}?error={quote_plus(exc.message)}",
            status_code=303,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/prepaid_pay_now_confirm.html",
        {
            "request": request,
            **preview_context,
            "invoice_id": invoice_id,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/invoices/{invoice_id:uuid}/prepaid-pay-now/confirm",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_prepaid_pay_now_confirm(
    request: Request,
    invoice_id: UUID,
    preview_fingerprint: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        web_billing_invoices_service.confirm_prepaid_recovery_pay_now(
            db,
            invoice_id=str(invoice_id),
            fingerprint=preview_fingerprint,
            actor_id=_actor_id(request),
        )
    except DomainError as exc:
        return RedirectResponse(
            f"/admin/billing/invoices/{invoice_id}?error={quote_plus(exc.message)}",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/billing/invoices/{invoice_id}?notice=Recovery+invoice+settled",
        status_code=303,
    )


@router.post(
    "/invoices/{invoice_id:uuid}/lines",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_line_create(
    request: Request,
    invoice_id: UUID,
    description: str = Form(...),
    quantity: str = Form("1"),
    unit_price: str = Form("0"),
    tax_rate_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_billing_invoices_service.create_invoice_line_from_form(
            db,
            invoice_id=str(invoice_id),
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            tax_rate_id=tax_rate_id,
        )
    except Exception as exc:
        detail_data = web_billing_invoices_service.load_invoice_detail_data(
            db,
            invoice_id=str(invoice_id),
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/invoice_detail.html",
            {
                "request": request,
                **(detail_data or {}),
                "error": str(exc),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}", status_code=303
    )


@router.post(
    "/invoices/{invoice_id:uuid}/apply-credit/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_apply_credit_preview(
    request: Request,
    invoice_id: UUID,
    credit_note_id: str = Form(...),
    amount: str | None = Form(None),
    memo: str | None = Form(None),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        preview = web_billing_invoices_service.preview_credit_note_application(
            db,
            invoice_id=str(invoice_id),
            credit_note_id=credit_note_id,
            amount=amount,
        )
    except Exception as exc:
        detail_data = web_billing_invoices_service.load_invoice_detail_data(
            db,
            invoice_id=str(invoice_id),
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/invoice_detail.html",
            {
                "request": request,
                **(detail_data or {}),
                "error": str(exc),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/credit_apply_confirm.html",
        {
            "request": request,
            "preview": preview,
            "memo": memo.strip() if memo else None,
            "idempotency_key": idempotency_key,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/invoices/{invoice_id:uuid}/apply-credit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_apply_credit(
    request: Request,
    invoice_id: UUID,
    credit_note_id: str = Form(...),
    amount: str = Form(...),
    memo: str | None = Form(None),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        web_billing_invoices_service.apply_credit_note_to_invoice_web(
            db,
            request=request,
            actor_id=_actor_id(request),
            invoice_id=str(invoice_id),
            credit_note_id=credit_note_id,
            amount=amount,
            memo=memo,
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        detail_data = web_billing_invoices_service.load_invoice_detail_data(
            db,
            invoice_id=str(invoice_id),
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/invoice_detail.html",
            {
                "request": request,
                **(detail_data or {}),
                "error": str(exc),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}", status_code=303
    )


@router.get(
    "/invoices/{invoice_id:uuid}/pdf",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_pdf(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    response, invoice_found = (
        web_billing_invoice_actions_service.cached_invoice_pdf_response(
            db, invoice_id=invoice_id
        )
    )
    if not invoice_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )
    if response is not None:
        return response

    export = web_billing_invoice_actions_service.generate_invoice_pdf_export(
        db,
        invoice_id=invoice_id,
        requested_by_id=_actor_id(request),
    )

    if export is None:
        return RedirectResponse(
            url=f"/admin/billing/invoices/{invoice_id}?pdf=queued",
            status_code=303,
        )
    response = web_billing_invoice_actions_service.generated_pdf_response(
        db, invoice_id=invoice_id, export=export
    )
    if response is not None:
        return response

    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}?pdf_notice={web_billing_invoice_actions_service.pdf_notice_for_export(export)}",
        status_code=303,
    )


@router.get(
    "/invoices/{invoice_id:uuid}/pdf/download",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_pdf_download(
    request: Request, invoice_id: UUID, db: Session = Depends(get_db)
):
    response, invoice_found = (
        web_billing_invoice_actions_service.cached_invoice_pdf_response(
            db, invoice_id=invoice_id
        )
    )
    if not invoice_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )
    if response is not None:
        return response
    return invoice_pdf(request=request, invoice_id=invoice_id, db=db)


@router.post(
    "/invoices/{invoice_id:uuid}/pdf/regenerate",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_pdf_regenerate(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
):
    web_billing_invoice_actions_service.regenerate_invoice_pdf(
        db,
        invoice_id=invoice_id,
        requested_by_id=_actor_id(request),
    )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}?pdf_notice=queued",
        status_code=303,
    )


@router.post(
    "/invoices/{invoice_id:uuid}/send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_send(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    web_billing_invoices_service.send_invoice_web(
        db,
        request=request,
        actor_id=_actor_id(request),
        invoice_id=str(invoice_id),
    )
    return HTMLResponse(web_billing_invoice_actions_service.send_message(invoice_id))


@router.post(
    "/invoices/{invoice_id:uuid}/send-and-return",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_send_and_return(
    request: Request,
    invoice_id: UUID,
    next_url: str = Form("/admin/billing/invoices"),
    db: Session = Depends(get_db),
):
    invoice_send(request=request, invoice_id=invoice_id, db=db)
    return RedirectResponse(url=next_url, status_code=303)


@router.post(
    "/invoices/{invoice_id:uuid}/void",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_void(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    web_billing_invoices_service.void_invoice_web(
        db,
        request=request,
        actor_id=_actor_id(request),
        invoice_id=str(invoice_id),
    )
    return HTMLResponse(web_billing_invoice_actions_service.void_message(invoice_id))


@router.post(
    "/invoices/{invoice_id:uuid}/void/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_void_preview(
    request: Request, invoice_id: UUID, db: Session = Depends(get_db)
):
    preview = web_billing_invoices_service.preview_invoice_void_web(
        db, invoice_id=str(invoice_id)
    )
    return templates.TemplateResponse(
        "admin/billing/invoice_closure_confirm.html",
        {
            "request": request,
            "preview": preview,
            "action_label": "Void invoice",
            "action_explanation": (
                "Use void only when the invoice should never have existed."
            ),
            "confirm_url": f"/admin/billing/invoices/{invoice_id}/void/confirm",
            "idempotency_key": secrets.token_urlsafe(24),
        },
    )


@router.post(
    "/invoices/{invoice_id:uuid}/void/confirm",
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_void_confirm(
    request: Request,
    invoice_id: UUID,
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    web_billing_invoices_service.confirm_invoice_void_web(
        db,
        request=request,
        actor_id=_actor_id(request),
        invoice_id=str(invoice_id),
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
        memo=memo,
    )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}", status_code=303
    )


@router.post(
    "/invoices/{invoice_id:uuid}/write-off/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_write_off_preview(
    request: Request, invoice_id: UUID, db: Session = Depends(get_db)
):
    preview = web_billing_invoices_service.preview_invoice_write_off_web(
        db, invoice_id=str(invoice_id)
    )
    return templates.TemplateResponse(
        "admin/billing/invoice_closure_confirm.html",
        {
            "request": request,
            "preview": preview,
            "action_label": "Write off receivable",
            "action_explanation": (
                "Use write-off for collectible postpaid debt that will not be "
                "collected; it is not payment and not invoice void."
            ),
            "confirm_url": f"/admin/billing/invoices/{invoice_id}/write-off/confirm",
            "idempotency_key": secrets.token_urlsafe(24),
        },
    )


@router.post(
    "/invoices/{invoice_id:uuid}/write-off/confirm",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_write_off_confirm(
    request: Request,
    invoice_id: UUID,
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    web_billing_invoices_service.confirm_invoice_write_off_web(
        db,
        request=request,
        actor_id=_actor_id(request),
        invoice_id=str(invoice_id),
        preview_fingerprint=preview_fingerprint,
        idempotency_key=idempotency_key,
        memo=memo,
    )
    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}", status_code=303
    )


@router.post(
    "/invoices/{invoice_id:uuid}/void-and-return",
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_void_and_return(
    request: Request,
    invoice_id: UUID,
    next_url: str | None = Form(None),
    db: Session = Depends(get_db),
):
    invoice_void(request=request, invoice_id=invoice_id, db=db)
    return RedirectResponse(
        url=next_url or f"/admin/billing/invoices/{invoice_id}",
        status_code=303,
    )
