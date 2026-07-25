"""Admin billing invoice bulk action routes."""

import json
import secrets
from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_invoice_bulk as web_billing_invoice_bulk_service
from app.services import (
    web_billing_invoice_bulk_actions as web_billing_invoice_bulk_actions_service,
)
from app.services import web_billing_overview as web_billing_overview_service
from app.services.auth_dependencies import has_permission, require_permission

router = APIRouter(prefix="/billing", tags=["web-admin-billing"])
templates = Jinja2Templates(directory="templates")


def _require_action_permission(
    db: Session,
    *,
    auth: dict,
    action: str,
) -> None:
    definition = (
        web_billing_invoice_bulk_actions_service.invoice_bulk_review_action_definition(
            action
        )
    )
    if not has_permission(auth, db, definition.permission):
        raise HTTPException(status_code=403, detail="Forbidden")


def _require_confirmed_invoice_scope(
    db: Session,
    *,
    action: str,
    invoice_ids: str,
    confirmed: bool,
    expected_count: int | None,
    expected_scope_token: str | None,
) -> None:
    if not confirmed:
        raise HTTPException(
            status_code=400, detail="Invoice action confirmation required"
        )
    try:
        web_billing_invoice_bulk_service.require_invoice_bulk_confirmation(
            db,
            action=action,
            invoice_ids_csv=invoice_ids,
            expected_count=expected_count,
            expected_scope_token=expected_scope_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/invoices/bulk/review/{action}",
    response_class=HTMLResponse,
)
def invoice_bulk_review(
    request: Request,
    action: str,
    invoice_ids: str = Form(...),
    auth: dict = Depends(require_permission("billing:invoice:read")),
    db: Session = Depends(get_db),
):
    normalized_action = action.replace("-", "_")
    try:
        _require_action_permission(db, auth=auth, action=normalized_action)
        state = web_billing_invoice_bulk_actions_service.build_invoice_bulk_review(
            db,
            action_key=normalized_action,
            invoice_ids_csv=invoice_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/invoice_bulk_review.html",
        {
            "request": request,
            **state,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/invoices/bulk/confirm/{action}",
    response_class=HTMLResponse,
)
def invoice_bulk_confirm(
    request: Request,
    action: str,
    invoice_ids: str = Form(...),
    confirmed: str | None = Form(None),
    expected_count: int | None = Form(None),
    expected_scope_token: str | None = Form(None),
    auth: dict = Depends(require_permission("billing:invoice:read")),
    db: Session = Depends(get_db),
):
    normalized_action = action.replace("-", "_")
    _require_action_permission(db, auth=auth, action=normalized_action)
    _require_confirmed_invoice_scope(
        db,
        action=normalized_action,
        invoice_ids=invoice_ids,
        confirmed=confirmed == "yes",
        expected_count=expected_count,
        expected_scope_token=expected_scope_token,
    )
    if normalized_action == "generate_pdf":
        from app.web.admin import get_current_user

        current_user = get_current_user(request) or {}
        actor_id = current_user.get("subscriber_id")
        pdf_result = web_billing_invoice_bulk_service.bulk_queue_pdf_exports(
            db,
            invoice_ids,
            requested_by_id=str(actor_id) if actor_id else None,
        )
        note = (
            f"Queued {len(pdf_result['queued'])} PDF export(s); "
            f"{len(pdf_result['ready'])} already ready; "
            f"{len(pdf_result['missing'])} skipped."
        )
    else:
        result = web_billing_invoice_bulk_service.execute_audited_bulk_action_result(
            db,
            request,
            action=normalized_action,
            invoice_ids_csv=invoice_ids,
        )
        verbs = {
            "issue": "Issued",
            "send": "Queued notifications for",
            "mark_paid": "Marked paid",
        }
        note = result.message(verbs[normalized_action])
    return RedirectResponse(
        url=f"/admin/billing/invoices?notice={quote(note)}",
        status_code=303,
    )


@router.post(
    "/invoices/bulk/void",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_bulk_void(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    previews, skipped_ids = web_billing_invoice_bulk_service.preview_bulk_void(
        db, invoice_ids
    )
    return templates.TemplateResponse(
        "admin/billing/invoice_bulk_void_confirm.html",
        {
            "request": request,
            "previews": previews,
            "selected_ids": web_billing_invoice_bulk_service.parse_ids_csv(invoice_ids),
            "skipped_ids": skipped_ids,
            "preview_fingerprints_json": json.dumps(
                {str(preview.invoice_id): preview.fingerprint for preview in previews}
            ),
            "batch_key": f"admin-bulk-void-{secrets.token_urlsafe(18)}",
        },
    )


@router.post(
    "/invoices/bulk/void/confirm",
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_bulk_void_confirm(
    request: Request,
    invoice_ids: str = Form(...),
    preview_fingerprints_json: str = Form(...),
    batch_key: str = Form(...),
    db: Session = Depends(get_db),
):
    result = web_billing_invoice_bulk_service.confirm_bulk_void_result(
        db,
        invoice_ids_csv=invoice_ids,
        preview_fingerprints_json=preview_fingerprints_json,
        batch_key=batch_key,
    )
    return RedirectResponse(
        url=f"/admin/billing/invoices?notice={quote(result.message('Voided'))}",
        status_code=303,
    )


@router.get(
    "/invoices/bulk/pdf-ready",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_pdf_ready(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    payload = web_billing_invoice_bulk_service.bulk_pdf_readiness(db, invoice_ids)
    return JSONResponse(payload)


@router.get(
    "/invoices/bulk/export.csv",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_export_csv(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    invoices = web_billing_invoice_bulk_service.list_invoices_by_ids(db, invoice_ids)
    content = web_billing_overview_service.render_invoices_csv(
        cast(list[Any], invoices)
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="invoices_selected_export.csv"'
        },
    )


@router.get(
    "/invoices/bulk/export.zip",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_export_pdf_zip(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    archive_buffer = web_billing_invoice_bulk_service.build_pdf_zip(db, invoice_ids)
    return StreamingResponse(
        archive_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="invoices_selected_pdfs.zip"'
        },
    )
