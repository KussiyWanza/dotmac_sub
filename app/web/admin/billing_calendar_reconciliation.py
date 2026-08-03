"""Admin routes for the reviewed prepaid billing-calendar repair queue."""

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_prepaid_billing_calendar_reconciliation as service
from app.services.auth_dependencies import require_permission
from app.services.domain_errors import DomainError

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _principal_actor(auth: dict) -> str:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    principal_type = str(auth.get("principal_type") or "user").strip()
    return f"{principal_type}:{principal_id}"


def _render_review(
    request: Request,
    *,
    db: Session,
    review: service.PrepaidBillingCalendarAdminReview,
    status_code: int = 200,
) -> Response:
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/prepaid_billing_calendar_confirm.html",
        {
            "request": request,
            "review": review,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status_code,
    )


@router.get(
    "/reconciliation/billing-dates",
    response_class=HTMLResponse,
)
def billing_date_reconciliation_queue(
    request: Request,
    page: int = Query(1, ge=1),
    auth: dict = Depends(require_permission(service.READ_PERMISSION)),
    db: Session = Depends(get_db),
) -> Response:
    del auth
    from app.web.admin import get_current_user, get_sidebar_stats

    page_size = 100
    cohort = service.load_admin_queue(
        db, limit=page_size, offset=(page - 1) * page_size
    )
    return templates.TemplateResponse(
        "admin/billing/prepaid_billing_calendar_reconciliation.html",
        {
            "request": request,
            "cohort": cohort,
            "page": page,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/reconciliation/billing-dates/{invoice_id:uuid}/preview",
    response_class=HTMLResponse,
)
def billing_date_reconciliation_preview(
    request: Request,
    invoice_id: UUID,
    auth: dict = Depends(require_permission(service.WRITE_PERMISSION)),
    db: Session = Depends(get_db),
) -> Response:
    try:
        review = service.build_admin_review(
            db, invoice_id=invoice_id, actor=_principal_actor(auth)
        )
    except DomainError as exc:
        return RedirectResponse(
            "/admin/billing/reconciliation/billing-dates"
            f"?error={quote_plus(exc.message)}",
            status_code=303,
        )
    return _render_review(request, db=db, review=review)


@router.post(
    "/reconciliation/billing-dates/{invoice_id:uuid}/confirm",
    response_class=HTMLResponse,
)
def billing_date_reconciliation_confirm(
    request: Request,
    invoice_id: UUID,
    preview_fingerprint: str = Form(...),
    confirmation_token: str = Form(...),
    confirmed: str | None = Form(None),
    reason: str = Form(""),
    auth: dict = Depends(require_permission(service.WRITE_PERMISSION)),
    db: Session = Depends(get_db),
) -> Response:
    actor = _principal_actor(auth)
    try:
        service.confirm_admin_review(
            db,
            invoice_id=invoice_id,
            actor=actor,
            preview_fingerprint=preview_fingerprint,
            confirmation_token=confirmation_token,
            confirmed=confirmed,
            reason=reason,
        )
    except DomainError as exc:
        try:
            review = service.rebuild_review_with_error(
                db,
                invoice_id=invoice_id,
                actor=actor,
                reason=reason,
                error=exc,
            )
        except DomainError:
            return RedirectResponse(
                "/admin/billing/reconciliation/billing-dates"
                f"?error={quote_plus(exc.message)}",
                status_code=303,
            )
        status_code = 409 if exc.code.endswith("stale_preview") else 400
        return _render_review(request, db=db, review=review, status_code=status_code)
    return RedirectResponse(
        "/admin/billing/reconciliation/billing-dates"
        "?notice=Prepaid+billing+dates+reconciled",
        status_code=303,
    )
