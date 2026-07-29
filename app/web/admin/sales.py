"""Admin sales routes — leads (list/board/detail), pipeline settings,
quotes (list/detail), sales orders (list/detail).

Native admin-sales port: CRM's ``web/admin/crm_leads.py`` /
``crm_sales.py`` / ``crm_quotes.py`` + the sales-order pages of
``operations.py``, restyled onto sub's thin-route + context-builder idiom
(see ``support_tickets.py``). All business logic lives in
``app.services.web_sales`` and the native sales managers.

The kanban board persists stage drags through the already-ported API
endpoints ``GET /api/v1/leads/kanban`` / ``POST /api/v1/leads/kanban/move``
(``app/api/sales.py``) via ``static/js/kanban.js``.

RBAC: ``crm:lead:*`` guards leads *and* pipeline settings (pipelines ride
lead permissions, matching the API port); quotes and sales orders use
``crm:quote:read`` / ``crm:sales_order:read``. The native sales RBAC owner seeds the keys
— the guards are in place regardless.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_sales as web_sales_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/sales", tags=["web-admin-sales"])
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


def _ctx(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "sales",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _error_detail(exc: Exception) -> str:
    return str(getattr(exc, "detail", None) or exc)


def _lead_field_errors(exc: Exception) -> dict[str, str]:
    if isinstance(exc, web_sales_service.LeadFormValidationError):
        return exc.field_errors
    if isinstance(exc, ValidationError):
        errors: dict[str, str] = {}
        for issue in exc.errors():
            location = issue.get("loc") or ("form",)
            field = str(location[-1])
            errors.setdefault(field, str(issue.get("msg") or "Invalid value."))
        return errors
    if isinstance(exc, HTTPException):
        detail = str(exc.detail or "").lower()
        if "stage does not belong" in detail or "pipeline stage" in detail:
            return {"stage_id": "Select a stage from the chosen pipeline."}
        if "subscriber" in detail or "party" in detail:
            return {
                "subscriber_id": ("Select a valid existing Person/Contact identity.")
            }
    return {"form": "The lead change was rejected. Review the form and try again."}


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@router.get(
    "/leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def leads_list(
    request: Request,
    status: str | None = Query(default=None),
    pipeline_id: str | None = Query(default=None),
    stage_id: str | None = Query(default=None),
    owner_agent_id: str | None = Query(default=None),
    lead_source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str | None = Query(default=None, alias="sort"),
    sort_dir: str | None = Query(default=None, alias="dir"),
    page: int = Query(default=1),
    per_page: int = Query(default=25),
    db: Session = Depends(get_db),
):
    try:
        state = web_sales_service.build_leads_list_context(
            db,
            status=status,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            owner_agent_id=owner_agent_id,
            lead_source=lead_source,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        )
    except SQLAlchemyError:
        logger.exception("sales_leads_list_load_failed")
        state = web_sales_service.build_leads_failure_context(
            search=search,
            page=page,
            per_page=per_page,
        )
    if state["canonicalization_needed"]:
        return RedirectResponse(
            url=state["list_query"].url("/admin/sales/leads"), status_code=307
        )
    context = _ctx(request, db, "sales-leads")
    context.update(state)
    context["success"] = (
        "Lead deleted successfully."
        if request.query_params.get("result") == "deleted"
        else None
    )
    return templates.TemplateResponse("admin/sales/leads/index.html", context)


@router.get(
    "/leads/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def lead_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-leads")
    context.update(web_sales_service.build_lead_new_context(db))
    return templates.TemplateResponse("admin/sales/leads/form.html", context)


@router.post(
    "/leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def lead_create(
    request: Request,
    title: str | None = Form(default=None),
    status: str | None = Form(default=None),
    subscriber_id: str | None = Form(default=None),
    contact_label: str | None = Form(default=None),
    owner_agent_id: str | None = Form(default=None),
    pipeline_id: str | None = Form(default=None),
    stage_id: str | None = Form(default=None),
    lead_source: str | None = Form(default=None),
    region: str | None = Form(default=None),
    estimated_value: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    address: str | None = Form(default=None),
    probability: str | None = Form(default=None),
    expected_close_date: str | None = Form(default=None),
    lost_reason: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    active = is_active is not None
    fields: dict[str, str | bool | None] = {
        "title": title,
        "status": status,
        "subscriber_id": subscriber_id,
        "contact_label": contact_label,
        "owner_agent_id": owner_agent_id,
        "pipeline_id": pipeline_id,
        "stage_id": stage_id,
        "lead_source": lead_source,
        "region": region,
        "estimated_value": estimated_value,
        "currency": currency,
        "address": address,
        "probability": probability,
        "expected_close_date": expected_close_date,
        "lost_reason": lost_reason,
        "notes": notes,
        "is_active": active,
    }
    try:
        lead_id, existing = web_sales_service.create_lead_from_form(
            db,
            title=title,
            status=status,
            subscriber_id=subscriber_id,
            owner_agent_id=owner_agent_id,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            lead_source=lead_source,
            region=region,
            estimated_value=estimated_value,
            currency=currency,
            address=address,
            probability=probability,
            expected_close_date=expected_close_date,
            lost_reason=lost_reason,
            notes=notes,
            is_active=active,
        )
        result = "existing" if existing else "created"
        return RedirectResponse(
            url=f"/admin/sales/leads/{lead_id}?result={result}", status_code=303
        )
    except (
        web_sales_service.LeadFormValidationError,
        ValidationError,
        HTTPException,
    ) as exc:
        context = _ctx(request, db, "sales-leads")
        context.update(
            web_sales_service.build_lead_form_error_context(
                db,
                mode="create",
                lead_id=None,
                field_errors=_lead_field_errors(exc),
                **fields,
            )
        )
        return templates.TemplateResponse(
            "admin/sales/leads/form.html", context, status_code=400
        )


@router.get(
    "/leads/board",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def leads_board(
    request: Request,
    pipeline_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-leads")
    context.update(
        web_sales_service.build_leads_board_context(db, pipeline_id=pipeline_id)
    )
    return templates.TemplateResponse("admin/sales/leads/board.html", context)


@router.get(
    "/leads/{lead_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def lead_detail(request: Request, lead_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-leads")
    context.update(web_sales_service.build_lead_detail_context(db, lead_id=lead_id))
    result = request.query_params.get("result")
    if result == "created":
        context["success"] = "Lead created successfully."
    elif result == "existing":
        context["success"] = "An existing open lead matched this contact and pipeline."
    elif result == "updated":
        context["success"] = "Lead updated successfully."
    elif result == "status-updated":
        context["success"] = "Lead status updated successfully."
    return templates.TemplateResponse("admin/sales/leads/detail.html", context)


@router.get(
    "/leads/{lead_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def lead_edit(request: Request, lead_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-leads")
    context.update(web_sales_service.build_lead_edit_context(db, lead_id=lead_id))
    return templates.TemplateResponse("admin/sales/leads/form.html", context)


@router.post(
    "/leads/{lead_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def lead_update(
    request: Request,
    lead_id: str,
    title: str | None = Form(default=None),
    status: str | None = Form(default=None),
    subscriber_id: str | None = Form(default=None),
    contact_label: str | None = Form(default=None),
    owner_agent_id: str | None = Form(default=None),
    pipeline_id: str | None = Form(default=None),
    stage_id: str | None = Form(default=None),
    lead_source: str | None = Form(default=None),
    region: str | None = Form(default=None),
    estimated_value: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    address: str | None = Form(default=None),
    probability: str | None = Form(default=None),
    expected_close_date: str | None = Form(default=None),
    lost_reason: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    active = is_active is not None
    fields: dict[str, str | bool | None] = {
        "title": title,
        "status": status,
        "subscriber_id": subscriber_id,
        "contact_label": contact_label,
        "owner_agent_id": owner_agent_id,
        "pipeline_id": pipeline_id,
        "stage_id": stage_id,
        "lead_source": lead_source,
        "region": region,
        "estimated_value": estimated_value,
        "currency": currency,
        "address": address,
        "probability": probability,
        "expected_close_date": expected_close_date,
        "lost_reason": lost_reason,
        "notes": notes,
        "is_active": active,
    }
    try:
        web_sales_service.update_lead_from_form(
            db,
            lead_id=lead_id,
            title=title,
            status=status,
            subscriber_id=subscriber_id,
            contact_label=contact_label,
            owner_agent_id=owner_agent_id,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            lead_source=lead_source,
            region=region,
            estimated_value=estimated_value,
            currency=currency,
            address=address,
            probability=probability,
            expected_close_date=expected_close_date,
            lost_reason=lost_reason,
            notes=notes,
            is_active=active,
        )
        return RedirectResponse(
            url=f"/admin/sales/leads/{lead_id}?result=updated", status_code=303
        )
    except (
        web_sales_service.LeadFormValidationError,
        ValidationError,
        HTTPException,
    ) as exc:
        context = _ctx(request, db, "sales-leads")
        context.update(
            web_sales_service.build_lead_form_error_context(
                db,
                mode="update",
                lead_id=lead_id,
                field_errors=_lead_field_errors(exc),
                **fields,
            )
        )
        return templates.TemplateResponse(
            "admin/sales/leads/form.html", context, status_code=400
        )


@router.post(
    "/leads/{lead_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def lead_status_update(
    request: Request,
    lead_id: str,
    status: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        web_sales_service.set_lead_status(db, lead_id=lead_id, status=status)
    except (
        web_sales_service.LeadFormValidationError,
        ValidationError,
        HTTPException,
    ):
        context = _ctx(request, db, "sales-leads")
        context.update(web_sales_service.build_lead_detail_context(db, lead_id=lead_id))
        context["api_error"] = (
            "The status could not be updated. Review the current lead and retry."
        )
        return templates.TemplateResponse(
            "admin/sales/leads/detail.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/sales/leads/{lead_id}?result=status-updated",
        status_code=303,
    )


@router.post(
    "/leads/{lead_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:delete"))],
)
def lead_delete(lead_id: str, db: Session = Depends(get_db)):
    web_sales_service.deactivate_lead(db, lead_id=lead_id)
    return RedirectResponse(url="/admin/sales/leads?result=deleted", status_code=303)


# ---------------------------------------------------------------------------
# Pipeline settings
# ---------------------------------------------------------------------------


@router.get(
    "/pipelines",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_settings_context(
            db,
            bulk_result=request.query_params.get("bulk_result", "").strip(),
            bulk_count=request.query_params.get("bulk_count", "").strip(),
        )
    )
    return templates.TemplateResponse("admin/sales/pipelines/index.html", context)


@router.get(
    "/pipelines/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-pipelines")
    context.update(web_sales_service.build_pipeline_new_context())
    return templates.TemplateResponse("admin/sales/pipelines/form.html", context)


@router.post(
    "/pipelines",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_create(
    request: Request,
    name: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    create_default_stages: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        pipeline_id = web_sales_service.create_pipeline_from_form(
            db,
            name=name,
            is_active=is_active,
            create_default_stages=create_default_stages,
        )
        return RedirectResponse(
            url=f"/admin/sales/leads/board?pipeline_id={pipeline_id}",
            status_code=303,
        )
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_form_error_context(
            mode="create",
            pipeline_id=None,
            name=name,
            is_active=is_active,
            create_default_stages=create_default_stages,
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/pipelines/form.html", context, status_code=400
    )


@router.get(
    "/pipelines/{pipeline_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_edit(request: Request, pipeline_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_edit_context(db, pipeline_id=pipeline_id)
    )
    return templates.TemplateResponse("admin/sales/pipelines/form.html", context)


@router.post(
    "/pipelines/{pipeline_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_update(
    request: Request,
    pipeline_id: str,
    name: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        web_sales_service.update_pipeline_from_form(
            db, pipeline_id=pipeline_id, name=name, is_active=is_active
        )
        return RedirectResponse(url="/admin/sales/pipelines", status_code=303)
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_form_error_context(
            mode="update",
            pipeline_id=pipeline_id,
            name=name,
            is_active=is_active,
            create_default_stages=None,
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/pipelines/form.html", context, status_code=400
    )


@router.post(
    "/pipelines/{pipeline_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_delete(request: Request, pipeline_id: str, db: Session = Depends(get_db)):
    _ = request
    web_sales_service.deactivate_pipeline(db, pipeline_id)
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/{pipeline_id}/stages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_create(
    request: Request,
    pipeline_id: str,
    name: str = Form(...),
    order_index: int = Form(0),
    default_probability: int = Form(50),
    db: Session = Depends(get_db),
):
    _ = request
    web_sales_service.create_stage_from_form(
        db,
        pipeline_id=pipeline_id,
        name=name,
        order_index=order_index,
        default_probability=default_probability,
    )
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/stages/{stage_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_update(
    request: Request,
    stage_id: str,
    name: str = Form(...),
    order_index: int = Form(0),
    default_probability: int = Form(50),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _ = request
    web_sales_service.update_stage_from_form(
        db,
        stage_id=stage_id,
        name=name,
        order_index=order_index,
        default_probability=default_probability,
        is_active=is_active,
    )
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/stages/{stage_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_delete(
    request: Request, stage_id: str, db: Session = Depends(get_db)
):
    _ = request
    web_sales_service.deactivate_stage(db, stage_id=stage_id)
    return RedirectResponse(url="/admin/sales/pipelines", status_code=303)


@router.post(
    "/pipelines/{pipeline_id}/bulk-assign-leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_bulk_assign_leads(
    request: Request,
    pipeline_id: str,
    stage_id: str | None = Form(default=None),
    scope: str = Form("unassigned"),
    db: Session = Depends(get_db),
):
    _ = request
    count = web_sales_service.bulk_assign_leads(
        db, pipeline_id=pipeline_id, stage_id=stage_id, scope=scope
    )
    return RedirectResponse(
        url=f"/admin/sales/pipelines?bulk_result=ok&bulk_count={count}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


@router.get(
    "/quotes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def quotes_list(
    request: Request,
    status: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str | None = Query(default=None, alias="sort"),
    sort_dir: str | None = Query(default=None, alias="dir"),
    page: int = Query(default=1),
    per_page: int = Query(default=25),
    db: Session = Depends(get_db),
):
    state = web_sales_service.build_quotes_list_context(
        db,
        status=status,
        lead_id=lead_id,
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )
    if state["canonicalization_needed"]:
        return RedirectResponse(
            url=state["list_query"].url("/admin/sales/quotes"), status_code=307
        )
    context = _ctx(request, db, "sales-quotes")
    context.update(state)
    return templates.TemplateResponse("admin/sales/quotes/index.html", context)


# NOTE: `/quotes/new` must stay above `/quotes/{quote_id}` or the detail route
# captures "new" as an id.
@router.get(
    "/quotes/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_new(
    request: Request,
    lead_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-quotes")
    context.update(web_sales_service.build_quote_new_context(db, lead_id=lead_id))
    return templates.TemplateResponse("admin/sales/quotes/form.html", context)


@router.post(
    "/quotes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_create(
    request: Request,
    subscriber_id: str | None = Form(default=None),
    lead_id: str | None = Form(default=None),
    status: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    tax_rate: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    latitude: str | None = Form(default=None),
    longitude: str | None = Form(default=None),
    address: str | None = Form(default=None),
    region: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    fields = {
        "subscriber_id": subscriber_id,
        "lead_id": lead_id,
        "status": status,
        "currency": currency,
        "tax_rate": tax_rate,
        "expires_at": expires_at,
        "notes": notes,
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "region": region,
    }
    try:
        quote_id = web_sales_service.create_quote_from_form(db, **fields)
        return RedirectResponse(url=f"/admin/sales/quotes/{quote_id}", status_code=303)
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-quotes")
    context.update(
        web_sales_service.build_quote_form_error_context(
            mode="create", quote_id=None, **fields
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/quotes/form.html", context, status_code=400
    )


@router.get(
    "/quotes/{quote_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def quote_detail(request: Request, quote_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-quotes")
    context.update(web_sales_service.build_quote_detail_context(db, quote_id=quote_id))
    return templates.TemplateResponse("admin/sales/quotes/detail.html", context)


@router.get(
    "/quotes/{quote_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_edit(request: Request, quote_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-quotes")
    context.update(web_sales_service.build_quote_edit_context(db, quote_id=quote_id))
    return templates.TemplateResponse("admin/sales/quotes/form.html", context)


@router.post(
    "/quotes/{quote_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_update(
    request: Request,
    quote_id: str,
    subscriber_id: str | None = Form(default=None),
    lead_id: str | None = Form(default=None),
    status: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    tax_rate: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    latitude: str | None = Form(default=None),
    longitude: str | None = Form(default=None),
    address: str | None = Form(default=None),
    region: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    fields = {
        "subscriber_id": subscriber_id,
        "lead_id": lead_id,
        "status": status,
        "currency": currency,
        "tax_rate": tax_rate,
        "expires_at": expires_at,
        "notes": notes,
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "region": region,
    }
    try:
        web_sales_service.update_quote_from_form(db, quote_id=quote_id, **fields)
        return RedirectResponse(url=f"/admin/sales/quotes/{quote_id}", status_code=303)
    except (ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-quotes")
    context.update(
        web_sales_service.build_quote_form_error_context(
            mode="update", quote_id=quote_id, **fields
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/sales/quotes/form.html", context, status_code=400
    )


@router.post(
    "/quotes/{quote_id}/line-items",
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_line_item_add(
    request: Request,
    quote_id: str,
    description: str | None = Form(default=None),
    quantity: str | None = Form(default=None),
    unit_price: str | None = Form(default=None),
    discount_percent: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        web_sales_service.add_quote_line_item_from_form(
            db,
            quote_id=quote_id,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            discount_percent=discount_percent,
        )
    except (ValidationError, ValueError) as exc:
        db.rollback()
        context = _ctx(request, db, "sales-quotes")
        context.update(
            web_sales_service.build_quote_detail_context(db, quote_id=quote_id)
        )
        context["error"] = _error_detail(exc)
        return templates.TemplateResponse(
            "admin/sales/quotes/detail.html", context, status_code=400
        )
    return RedirectResponse(url=f"/admin/sales/quotes/{quote_id}", status_code=303)


@router.post(
    "/quotes/{quote_id}/line-items/{item_id}/delete",
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_line_item_delete(quote_id: str, item_id: str, db: Session = Depends(get_db)):
    web_sales_service.delete_quote_line_item(db, item_id)
    return RedirectResponse(url=f"/admin/sales/quotes/{quote_id}", status_code=303)


@router.post(
    "/quotes/{quote_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_set_status(
    request: Request,
    quote_id: str,
    status: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        web_sales_service.set_quote_status(db, quote_id, status)
    except (ValidationError, ValueError) as exc:
        # Sending or accepting a quote with no line items is refused by the
        # sales service. Surface that to the operator instead of 500ing.
        db.rollback()
        context = _ctx(request, db, "sales-quotes")
        context.update(
            web_sales_service.build_quote_detail_context(db, quote_id=quote_id)
        )
        context["error"] = _error_detail(exc)
        return templates.TemplateResponse(
            "admin/sales/quotes/detail.html", context, status_code=400
        )
    return RedirectResponse(url=f"/admin/sales/quotes/{quote_id}", status_code=303)


@router.post(
    "/quotes/{quote_id}/delete",
    dependencies=[Depends(require_permission("crm:quote:write"))],
)
def quote_delete(quote_id: str, db: Session = Depends(get_db)):
    web_sales_service.deactivate_quote(db, quote_id)
    return RedirectResponse(url="/admin/sales/quotes", status_code=303)


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------


@router.get(
    "/sales-orders",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def sales_orders_list(
    request: Request,
    status: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str | None = Query(default=None, alias="sort"),
    sort_dir: str | None = Query(default=None, alias="dir"),
    page: int = Query(default=1),
    per_page: int = Query(default=25),
    db: Session = Depends(get_db),
):
    state = web_sales_service.build_sales_orders_list_context(
        db,
        status=status,
        payment_status=payment_status,
        source_type=source_type,
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )
    if state["canonicalization_needed"]:
        return RedirectResponse(
            url=state["list_query"].url("/admin/sales/sales-orders"),
            status_code=307,
        )
    context = _ctx(request, db, "sales-orders")
    context.update(state)
    return templates.TemplateResponse("admin/sales/sales_orders/index.html", context)


@router.get(
    "/sales-orders/{order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def sales_order_detail(request: Request, order_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-orders")
    context.update(
        web_sales_service.build_sales_order_detail_context(db, sales_order_id=order_id)
    )
    return templates.TemplateResponse("admin/sales/sales_orders/detail.html", context)
