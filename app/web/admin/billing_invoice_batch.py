"""Admin billing invoice batch routes."""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_invoice_batch as web_billing_invoice_batch_service
from app.services.auth_dependencies import has_permission, require_permission
from app.services.domain_errors import DomainError

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])
logger = logging.getLogger(__name__)


def _render_batch_page(
    request: Request,
    db: Session,
    auth: dict,
    *,
    note: str | None = None,
    error: str | None = None,
    preview=None,
    status_code: int = 200,
):
    from app.web.admin import get_current_user, get_sidebar_stats

    can_write = has_permission(auth, db, "billing:batch:write")
    state = web_billing_invoice_batch_service.build_batch_page_state(
        db,
        note=note,
        error=error,
        preview=preview,
        batch_action_form=(
            web_billing_invoice_batch_service.build_batch_action_form(preview)
            if preview is not None and can_write
            else None
        ),
        can_write=can_write,
    )
    return templates.TemplateResponse(
        "admin/billing/invoice_batch.html",
        {
            "request": request,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **state,
        },
        status_code=status_code,
    )


@router.get(
    "/invoices/batch",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch(
    request: Request,
    note: str | None = Query(None),
    auth: dict = Depends(require_permission("billing:batch:read")),
    db: Session = Depends(get_db),
):
    return _render_batch_page(request, db, auth, note=note)


@router.post(
    "/invoices/generate-batch/preview",
    response_class=HTMLResponse,
)
def invoice_generate_batch_preview(
    request: Request,
    billing_cycle: str | None = Form(None),
    billing_date: str | None = Form(None),
    auth: dict = Depends(require_permission("billing:batch:write")),
    db: Session = Depends(get_db),
):
    try:
        preview = web_billing_invoice_batch_service.preview_batch_action(
            db,
            billing_cycle=billing_cycle,
            billing_date=billing_date,
        )
    except (ValueError, DomainError):
        return _render_batch_page(
            request,
            db,
            auth,
            error=web_billing_invoice_batch_service.INVOICE_BATCH_PREVIEW_ERROR_MESSAGE,
            status_code=400,
        )
    return _render_batch_page(request, db, auth, preview=preview)


@router.post(
    "/invoices/batch/{run_id}/retry/preview",
    response_class=HTMLResponse,
)
def invoice_batch_retry_preview(
    request: Request,
    run_id: str,
    auth: dict = Depends(require_permission("billing:batch:write")),
    db: Session = Depends(get_db),
):
    try:
        preview = web_billing_invoice_batch_service.preview_retry_batch(
            db,
            run_id=run_id,
        )
    except DomainError as exc:
        return _render_batch_page(
            request,
            db,
            auth,
            error=exc.message,
            status_code=400,
        )
    return _render_batch_page(request, db, auth, preview=preview)


@router.post(
    "/invoices/generate-batch/confirm",
    response_class=HTMLResponse,
)
def invoice_generate_batch_confirm(
    request: Request,
    billing_cycle: str | None = Form(None),
    billing_date: str | None = Form(None),
    preview_fingerprint: str = Form(...),
    source_run_id: str | None = Form(None),
    confirmed: str | None = Form(None),
    auth: dict = Depends(require_permission("billing:batch:write")),
    db: Session = Depends(get_db),
):
    try:
        note = web_billing_invoice_batch_service.confirm_batch_action(
            db,
            billing_cycle=billing_cycle,
            billing_date=billing_date,
            preview_fingerprint=preview_fingerprint,
            source_run_id=source_run_id,
            confirmed=confirmed == "yes",
            actor=str(auth.get("principal_id") or ""),
        )
    except DomainError as exc:
        try:
            preview = web_billing_invoice_batch_service.preview_batch_action(
                db,
                billing_cycle=(
                    None if billing_cycle in {None, "", "all"} else billing_cycle
                ),
                billing_date=billing_date,
                source_run_id=(
                    None if source_run_id in {None, "", "manual"} else source_run_id
                ),
            )
        except Exception:
            preview = None
        return _render_batch_page(
            request,
            db,
            auth,
            error=exc.message,
            preview=preview,
            status_code=409 if exc.code.endswith("stale_preview") else 400,
        )
    except Exception:
        logger.exception("Confirmed invoice batch failed")
        return _render_batch_page(
            request,
            db,
            auth,
            error=web_billing_invoice_batch_service.INVOICE_BATCH_ERROR_MESSAGE,
            status_code=500,
        )
    query = urlencode({"note": note})
    return RedirectResponse(
        url=f"/admin/billing/invoices/batch?{query}",
        status_code=303,
    )


@router.get(
    "/invoices/batch/history-panel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch_history_panel(
    request: Request,
    auth: dict = Depends(require_permission("billing:batch:read")),
    db: Session = Depends(get_db),
):
    recent_runs = web_billing_invoice_batch_service.list_recent_runs(db, limit=25)
    return templates.TemplateResponse(
        "admin/billing/_invoice_batch_history_table.html",
        {
            "request": request,
            "recent_runs": recent_runs,
            "can_write": has_permission(auth, db, "billing:batch:write"),
        },
    )


@router.get(
    "/invoices/batch/history.csv",
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch_history_csv(
    request: Request,
    db: Session = Depends(get_db),
):
    rows = web_billing_invoice_batch_service.list_recent_runs(db, limit=1000)
    content = web_billing_invoice_batch_service.render_runs_csv(rows)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="billing_run_history.csv"'
        },
    )


@router.get(
    "/invoices/batch/{run_id}/export.csv",
    dependencies=[Depends(require_permission("billing:batch:read"))],
)
def invoice_batch_run_csv(
    request: Request,
    run_id: str,
    db: Session = Depends(get_db),
):
    row = web_billing_invoice_batch_service.get_run_row(db, run_id=run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Billing run not found")
    content = web_billing_invoice_batch_service.render_single_run_csv(row)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="billing_run_{run_id}.csv"'
        },
    )
