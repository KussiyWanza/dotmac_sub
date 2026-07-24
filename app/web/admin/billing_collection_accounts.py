"""Admin billing collection accounts routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    web_billing_collection_accounts as web_billing_collection_accounts_service,
)
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/settings/billing", tags=["web-admin-settings"])


def _base_context(request: Request, db: Session, active_page: str) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:read"))],
)
def collection_accounts_list(
    request: Request,
    show_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    state = web_billing_collection_accounts_service.list_data(
        db, show_inactive=show_inactive
    )
    return templates.TemplateResponse(
        "admin/billing/collection_accounts.html",
        {
            **_base_context(request, db, "collection-accounts"),
            **state,
        },
    )


@router.post(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def collection_accounts_create(
    request: Request,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_name: str | None = Form(None),
    account_number: str | None = Form(None),
    sort_code: str | None = Form(None),
    accounting_code: str | None = Form(None),
    presentment_priority: int = Form(0),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_billing_collection_accounts_service.create_collection_account_from_form(
            db=db,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_name=account_name,
            account_number=account_number,
            sort_code=sort_code,
            accounting_code=accounting_code,
            presentment_priority=presentment_priority,
            notes=notes,
        )
        return RedirectResponse(
            url="/admin/settings/billing/collection-accounts", status_code=303
        )
    except (HTTPException, ValueError) as exc:
        state = web_billing_collection_accounts_service.list_data(
            db, show_inactive=False
        )
        return templates.TemplateResponse(
            "admin/billing/collection_accounts.html",
            {
                **_base_context(request, db, "collection-accounts"),
                **state,
                "error": str(exc),
            },
            status_code=400,
        )


@router.get(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def collection_accounts_edit(
    request: Request, account_id: UUID, db: Session = Depends(get_db)
):
    state = web_billing_collection_accounts_service.edit_data(
        db, account_id=str(account_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Collection account not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/billing/collection_account_form.html",
        {
            **_base_context(request, db, "collection-accounts"),
            **state,
            "action_url": (
                f"/admin/settings/billing/collection-accounts/{account_id}/edit"
            ),
            "form_title": "Edit Collection Account",
            "submit_label": "Update Account",
        },
    )


@router.post(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:account:write"))],
)
def collection_accounts_update(
    request: Request,
    account_id: UUID,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_name: str | None = Form(None),
    account_number: str | None = Form(None),
    sort_code: str | None = Form(None),
    accounting_code: str | None = Form(None),
    presentment_priority: int = Form(0),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_billing_collection_accounts_service.update_collection_account_from_form(
            db=db,
            account_id=account_id,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_name=account_name,
            account_number=account_number,
            sort_code=sort_code,
            accounting_code=accounting_code,
            presentment_priority=presentment_priority,
            notes=notes,
        )
        return RedirectResponse(
            url="/admin/settings/billing/collection-accounts", status_code=303
        )
    except (HTTPException, ValueError) as exc:
        state = web_billing_collection_accounts_service.edit_data(
            db, account_id=str(account_id)
        )
        return templates.TemplateResponse(
            "admin/billing/collection_account_form.html",
            {
                **_base_context(request, db, "collection-accounts"),
                **(state or {}),
                "action_url": (
                    f"/admin/settings/billing/collection-accounts/{account_id}/edit"
                ),
                "form_title": "Edit Collection Account",
                "submit_label": "Update Account",
                "error": str(exc),
            },
            status_code=400,
        )
