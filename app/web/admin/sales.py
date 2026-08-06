"""Admin sales routes — leads (list/board/detail), pipeline settings,
quotes (list/detail), sales orders (list/detail).

Native admin-sales port: CRM's ``web/admin/crm_leads.py`` /
``crm_sales.py`` / ``crm_quotes.py`` + the sales-order pages of
``operations.py``, restyled onto sub's thin-route + context-builder idiom
(see ``support_tickets.py``). Business rules and dashboard calculations live
in the native sales owner; web services only assemble presentation context.

The kanban board persists stage drags through the already-ported API
endpoints ``GET /api/v1/leads/kanban`` / ``POST /api/v1/leads/kanban/move``
(``app/api/sales.py``) via ``static/js/kanban.js``.

RBAC: ``crm:lead:*`` guards leads *and* pipeline settings (pipelines ride
lead permissions, matching the API port); quotes and sales orders use
``crm:quote:read`` / ``crm:quote:send`` / ``crm:sales_order:read``. The native
sales RBAC owner seeds the keys — the guards are in place regardless.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import conversation_lead_relationships
from app.services import web_sales as web_sales_service
from app.services import web_sales_dashboard as dashboard_service
from app.services.auth_dependencies import can, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.file_storage import build_content_disposition
from app.services.owner_commands import CommandContext
from app.services.sales import quote_delivery, quote_documents

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


def _quote_command_context(
    request: Request,
    quote_id: str,
    *,
    action: str = "accept",
) -> CommandContext:
    return CommandContext.system(
        actor=str(getattr(request.state, "actor_id", None) or "admin-sales-user"),
        scope=f"sales:quote-{action}",
        reason=f"Admin Quote {action}",
        idempotency_key=(
            f"quote-acceptance:{quote_id}" if action == "accept" else None
        ),
    )


def _quote_actor_system_user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    actor_id = str(getattr(user, "id", "") or "").strip()
    if not actor_id:
        raise ValueError("An authenticated staff user is required.")
    return actor_id


def _lead_actor_system_user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    actor_id = str(getattr(user, "id", "") or "").strip()
    if not actor_id:
        raise ValueError("An authenticated staff user is required.")
    return actor_id


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
            return {"party_id": "Select a valid existing Person/Contact identity."}
    if isinstance(exc, DomainError):
        field = str((exc.details or {}).get("field") or "form")
        return {field: exc.message}
    return {"form": "The lead change was rejected. Review the form and try again."}


def _pipeline_settings_redirect(
    notice: str,
    *,
    bulk_count: int | None = None,
) -> RedirectResponse:
    params: dict[str, str | int] = {"notice": notice}
    if bulk_count is not None:
        params["bulk_count"] = bulk_count
    return RedirectResponse(
        url=f"/admin/sales/pipelines-settings?{urlencode(params)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def sales_dashboard(
    request: Request,
    pipeline_id: str | None = Query(default=None),
    period_days: int = Query(default=30),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "sales-dashboard")
    context.update(
        dashboard_service.build_dashboard_shell_context(
            db,
            pipeline_id=pipeline_id,
            period_days=period_days,
        )
    )
    return templates.TemplateResponse("admin/sales/dashboard.html", context)


@router.get(
    "/dashboard-data",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def sales_dashboard_data(
    request: Request,
    pipeline_id: str | None = Query(default=None),
    period_days: int = Query(default=30),
    db: Session = Depends(get_db),
):
    context: dict[str, object] = {"request": request}
    try:
        context.update(
            dashboard_service.build_dashboard_data_context(
                db,
                pipeline_id=pipeline_id,
                period_days=period_days,
            )
        )
        status_code = 200
    except Exception:
        logger.exception("sales_dashboard_projection_failed")
        context.update(
            {
                "dashboard_error": (
                    "Sales reporting is temporarily unavailable. "
                    "Your pipeline data has not been changed."
                ),
                "dashboard_data_url": str(request.url),
            }
        )
        # HTMX swaps successful responses by default; the partial itself
        # carries the explicit unavailable state.
        status_code = 200
    return templates.TemplateResponse(
        "admin/sales/_dashboard_data.html",
        context,
        status_code=status_code,
    )


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
def lead_new(
    request: Request,
    inbox_conversation_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if inbox_conversation_id and not can(request, "support:ticket:read"):
        raise HTTPException(status_code=403, detail="Inbox access is required.")
    if inbox_conversation_id:
        try:
            UUID(inbox_conversation_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=404, detail="Conversation not found."
            ) from exc
    context = _ctx(request, db, "sales-leads")
    context.update(
        web_sales_service.build_lead_new_context(
            db, actor_system_user_id=_lead_actor_system_user_id(request)
        )
    )
    context["inbox_conversation_id"] = inbox_conversation_id
    return templates.TemplateResponse("admin/sales/leads/new_form.html", context)


@router.post(
    "/leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def lead_create(
    request: Request,
    submission_id: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    status: str | None = Form(default=None),
    owner_agent_id: str | None = Form(default=None),
    emails: list[str] = Form(default=[]),
    primary_email: str | None = Form(default=None),
    phones: list[str] = Form(default=[]),
    primary_phone: str | None = Form(default=None),
    whatsapp_phone_indices: list[str] = Form(default=[]),
    address_line1: str | None = Form(default=None),
    address_line2: str | None = Form(default=None),
    date_of_birth: str | None = Form(default=None),
    gender: str | None = Form(default=None),
    nin: str | None = Form(default=None),
    city: str | None = Form(default=None),
    postal_code: str | None = Form(default=None),
    country_code: str | None = Form(default=None),
    organization_id: str | None = Form(default=None),
    organization_label: str | None = Form(default=None),
    managed_by_reseller: str | None = Form(default=None),
    reseller_id: str | None = Form(default=None),
    reseller_label: str | None = Form(default=None),
    pipeline_id: str | None = Form(default=None),
    stage_id: str | None = Form(default=None),
    lead_source: str | None = Form(default=None),
    region_zone_id: str | None = Form(default=None),
    estimated_value: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    probability: str | None = Form(default=None),
    expected_close_date: str | None = Form(default=None),
    lost_reason: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    inbox_conversation_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    if inbox_conversation_id and not can(request, "support:ticket:read"):
        raise HTTPException(status_code=403, detail="Inbox access is required.")
    active = is_active is not None
    actor_system_user_id = _lead_actor_system_user_id(request)
    fields: dict[str, object] = {
        "submission_id": submission_id,
        "display_name": display_name,
        "status": status,
        "owner_agent_id": owner_agent_id,
        "emails": emails,
        "primary_email": primary_email,
        "phones": phones,
        "primary_phone": primary_phone,
        "whatsapp_phone_indices": whatsapp_phone_indices,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "date_of_birth": date_of_birth,
        "gender": gender,
        "nin": nin,
        "city": city,
        "postal_code": postal_code,
        "country_code": country_code,
        "organization_id": organization_id,
        "organization_label": organization_label,
        "managed_by_reseller": managed_by_reseller is not None,
        "reseller_id": reseller_id,
        "reseller_label": reseller_label,
        "pipeline_id": pipeline_id,
        "stage_id": stage_id,
        "lead_source": lead_source,
        "region_zone_id": region_zone_id,
        "estimated_value": estimated_value,
        "currency": currency,
        "probability": probability,
        "expected_close_date": expected_close_date,
        "lost_reason": lost_reason,
        "notes": notes,
        "is_active": active,
        "inbox_conversation_id": inbox_conversation_id,
    }
    try:
        outcome = web_sales_service.author_lead_from_form(
            db,
            actor_system_user_id=actor_system_user_id,
            submission_id=submission_id,
            display_name=display_name,
            status=status,
            owner_agent_id=owner_agent_id,
            emails=emails,
            primary_email=primary_email,
            phones=phones,
            primary_phone=primary_phone,
            whatsapp_phone_indices=whatsapp_phone_indices,
            address_line1=address_line1,
            address_line2=address_line2,
            date_of_birth=date_of_birth,
            gender=gender,
            nin=nin,
            city=city,
            postal_code=postal_code,
            country_code=country_code,
            organization_id=organization_id,
            managed_by_reseller=managed_by_reseller is not None,
            reseller_id=reseller_id,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            lead_source=lead_source,
            region_zone_id=region_zone_id,
            estimated_value=estimated_value,
            currency=currency,
            probability=probability,
            expected_close_date=expected_close_date,
            lost_reason=lost_reason,
            notes=notes,
            is_active=active,
            inbox_conversation_id=inbox_conversation_id,
        )
        result = "existing" if outcome.replayed else "created"
        if inbox_conversation_id:
            return RedirectResponse(
                url=(
                    f"/admin/inbox?c={inbox_conversation_id}"
                    f"&status=success&message=Lead%20{result}"
                ),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/admin/sales/leads/{outcome.lead_id}?result={result}",
            status_code=303,
        )
    except (
        web_sales_service.LeadFormValidationError,
        ValidationError,
        HTTPException,
        DomainError,
    ) as exc:
        context = _ctx(request, db, "sales-leads")
        context.update(
            web_sales_service.build_lead_create_error_context(
                db,
                actor_system_user_id=actor_system_user_id,
                field_errors=_lead_field_errors(exc),
                **fields,
            )
        )
        return templates.TemplateResponse(
            "admin/sales/leads/new_form.html", context, status_code=400
        )


@router.get(
    "/pipeline-board",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def pipeline_board(
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
    "/leads/board",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
    include_in_schema=False,
)
def legacy_leads_board_redirect(
    pipeline_id: str | None = Query(default=None),
):
    query = urlencode({"pipeline_id": pipeline_id}) if pipeline_id else ""
    suffix = f"?{query}" if query else ""
    return RedirectResponse(
        url=f"/admin/sales/pipeline-board{suffix}",
        status_code=308,
    )


@router.get(
    "/leads/{lead_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def lead_detail(
    request: Request,
    lead_id: str,
    inbox_conversation_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if inbox_conversation_id:
        if not can(request, "support:ticket:read"):
            raise HTTPException(status_code=403, detail="Inbox access is required.")
        try:
            valid_return = conversation_lead_relationships.conversation_links_lead(
                db,
                conversation_id=UUID(inbox_conversation_id),
                lead_id=UUID(lead_id),
            )
        except ValueError:
            valid_return = False
        if not valid_return:
            raise HTTPException(status_code=404, detail="Conversation not found.")
    context = _ctx(request, db, "sales-leads")
    context.update(web_sales_service.build_lead_detail_context(db, lead_id=lead_id))
    context["inbox_conversation_id"] = inbox_conversation_id
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
def lead_edit(
    request: Request,
    lead_id: str,
    inbox_conversation_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if inbox_conversation_id:
        if not can(request, "support:ticket:read"):
            raise HTTPException(status_code=403, detail="Inbox access is required.")
        try:
            valid_return = conversation_lead_relationships.conversation_links_lead(
                db,
                conversation_id=UUID(inbox_conversation_id),
                lead_id=UUID(lead_id),
            )
        except ValueError:
            valid_return = False
        if not valid_return:
            raise HTTPException(status_code=404, detail="Conversation not found.")
    context = _ctx(request, db, "sales-leads")
    context.update(web_sales_service.build_lead_edit_context(db, lead_id=lead_id))
    context["inbox_conversation_id"] = inbox_conversation_id
    return templates.TemplateResponse("admin/sales/leads/new_form.html", context)


@router.post(
    "/leads/{lead_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def lead_update(
    request: Request,
    lead_id: str,
    submission_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
    status: str | None = Form(default=None),
    owner_agent_id: str | None = Form(default=None),
    emails: list[str] = Form(default=[]),
    primary_email: str | None = Form(default=None),
    phones: list[str] = Form(default=[]),
    primary_phone: str | None = Form(default=None),
    whatsapp_phone_indices: list[str] = Form(default=[]),
    address_line1: str | None = Form(default=None),
    address_line2: str | None = Form(default=None),
    date_of_birth: str | None = Form(default=None),
    gender: str | None = Form(default=None),
    nin: str | None = Form(default=None),
    city: str | None = Form(default=None),
    postal_code: str | None = Form(default=None),
    country_code: str | None = Form(default=None),
    organization_id: str | None = Form(default=None),
    organization_label: str | None = Form(default=None),
    managed_by_reseller: str | None = Form(default=None),
    reseller_id: str | None = Form(default=None),
    reseller_label: str | None = Form(default=None),
    pipeline_id: str | None = Form(default=None),
    stage_id: str | None = Form(default=None),
    lead_source: str | None = Form(default=None),
    region_zone_id: str | None = Form(default=None),
    estimated_value: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    address: str | None = Form(default=None),
    probability: str | None = Form(default=None),
    expected_close_date: str | None = Form(default=None),
    lost_reason: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    inbox_conversation_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    if inbox_conversation_id:
        if not can(request, "support:ticket:read"):
            raise HTTPException(status_code=403, detail="Inbox access is required.")
        try:
            valid_return = conversation_lead_relationships.conversation_links_lead(
                db,
                conversation_id=UUID(inbox_conversation_id),
                lead_id=UUID(lead_id),
            )
        except ValueError:
            valid_return = False
        if not valid_return:
            raise HTTPException(status_code=404, detail="Conversation not found.")
    active = is_active is not None
    actor_system_user_id = _lead_actor_system_user_id(request)
    fields: dict[str, object] = {
        "submission_id": submission_id,
        "title": title,
        "display_name": display_name,
        "status": status,
        "owner_agent_id": owner_agent_id,
        "emails": emails,
        "primary_email": primary_email,
        "phones": phones,
        "primary_phone": primary_phone,
        "whatsapp_phone_indices": whatsapp_phone_indices,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "date_of_birth": date_of_birth,
        "gender": gender,
        "nin": nin,
        "city": city,
        "postal_code": postal_code,
        "country_code": country_code,
        "organization_id": organization_id,
        "organization_label": organization_label,
        "managed_by_reseller": managed_by_reseller is not None,
        "reseller_id": reseller_id,
        "reseller_label": reseller_label,
        "pipeline_id": pipeline_id,
        "stage_id": stage_id,
        "lead_source": lead_source,
        "region_zone_id": region_zone_id,
        "estimated_value": estimated_value,
        "currency": currency,
        "address": address,
        "probability": probability,
        "expected_close_date": expected_close_date,
        "lost_reason": lost_reason,
        "notes": notes,
        "is_active": active,
        "inbox_conversation_id": inbox_conversation_id,
    }
    try:
        web_sales_service.update_lead_from_form(
            db,
            actor_system_user_id=actor_system_user_id,
            submission_id=submission_id,
            lead_id=lead_id,
            title=title,
            display_name=display_name,
            status=status,
            owner_agent_id=owner_agent_id,
            emails=emails,
            primary_email=primary_email,
            phones=phones,
            primary_phone=primary_phone,
            whatsapp_phone_indices=whatsapp_phone_indices,
            address_line1=address_line1,
            address_line2=address_line2,
            date_of_birth=date_of_birth,
            gender=gender,
            nin=nin,
            city=city,
            postal_code=postal_code,
            country_code=country_code,
            organization_id=organization_id,
            managed_by_reseller=managed_by_reseller is not None,
            reseller_id=reseller_id,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            lead_source=lead_source,
            region_zone_id=region_zone_id,
            estimated_value=estimated_value,
            currency=currency,
            address=address,
            probability=probability,
            expected_close_date=expected_close_date,
            lost_reason=lost_reason,
            notes=notes,
            is_active=active,
        )
        destination = (
            f"/admin/inbox?c={inbox_conversation_id}"
            "&status=success&message=Lead%20updated"
            if inbox_conversation_id
            else f"/admin/sales/leads/{lead_id}?result=updated"
        )
        return RedirectResponse(url=destination, status_code=303)
    except (
        web_sales_service.LeadFormValidationError,
        ValidationError,
        HTTPException,
        DomainError,
    ) as exc:
        context = _ctx(request, db, "sales-leads")
        context.update(
            web_sales_service.build_lead_edit_error_context(
                db,
                lead_id=lead_id,
                field_errors=_lead_field_errors(exc),
                **fields,
            )
        )
        context["inbox_conversation_id"] = inbox_conversation_id
        return templates.TemplateResponse(
            "admin/sales/leads/new_form.html", context, status_code=400
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
    "/pipelines-settings",
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
            notice=request.query_params.get("notice", "").strip(),
            bulk_count=request.query_params.get("bulk_count", "").strip(),
        )
    )
    return templates.TemplateResponse("admin/sales/pipelines/index.html", context)


@router.get(
    "/pipelines",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
)
def legacy_pipeline_settings(request: Request):
    query = str(request.url.query)
    suffix = f"?{query}" if query else ""
    return RedirectResponse(
        url=f"/admin/sales/pipelines-settings{suffix}",
        status_code=308,
    )


@router.get(
    "/pipelines-settings/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-pipelines")
    context.update(web_sales_service.build_pipeline_new_context())
    return templates.TemplateResponse("admin/sales/pipelines/form.html", context)


@router.get(
    "/pipelines/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
)
def legacy_pipeline_new():
    return RedirectResponse(
        url="/admin/sales/pipelines-settings/new",
        status_code=308,
    )


@router.post(
    "/pipelines-settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
@router.post(
    "/pipelines",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
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
            url=(
                f"/admin/sales/pipeline-board?pipeline_id={pipeline_id}"
                "&notice=pipeline_created"
            ),
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
    "/pipelines-settings/{pipeline_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_edit(request: Request, pipeline_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-pipelines")
    context.update(
        web_sales_service.build_pipeline_edit_context(db, pipeline_id=pipeline_id)
    )
    return templates.TemplateResponse("admin/sales/pipelines/form.html", context)


@router.get(
    "/pipelines/{pipeline_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
)
def legacy_pipeline_edit(pipeline_id: str):
    return RedirectResponse(
        url=f"/admin/sales/pipelines-settings/{pipeline_id}/edit",
        status_code=308,
    )


@router.post(
    "/pipelines-settings/{pipeline_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
@router.post(
    "/pipelines/{pipeline_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
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
        return _pipeline_settings_redirect("pipeline_updated")
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
    return _pipeline_settings_redirect("pipeline_deactivated")


@router.post(
    "/pipelines-settings/{pipeline_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_status_update(
    request: Request,
    pipeline_id: str,
    is_active: bool = Form(...),
    db: Session = Depends(get_db),
):
    _ = request
    try:
        web_sales_service.set_pipeline_active(
            db,
            pipeline_id=pipeline_id,
            is_active=is_active,
        )
    except (HTTPException, ValidationError, ValueError):
        return _pipeline_settings_redirect("operation_failed")
    return _pipeline_settings_redirect(
        "pipeline_activated" if is_active else "pipeline_deactivated"
    )


@router.post(
    "/pipelines-settings/{pipeline_id}/stages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
@router.post(
    "/pipelines/{pipeline_id}/stages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
)
def pipeline_stage_create(
    request: Request,
    pipeline_id: str,
    name: str = Form(...),
    order_index: int = Form(0),
    default_probability: int = Form(50),
    stage_type: str = Form("standard"),
    color: str = Form("#06B6D4"),
    icon: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _ = request
    try:
        web_sales_service.create_stage_from_form(
            db,
            pipeline_id=pipeline_id,
            name=name,
            order_index=order_index,
            default_probability=default_probability,
            stage_type=stage_type,
            color=color,
            icon=icon,
        )
    except (HTTPException, ValidationError, ValueError):
        return _pipeline_settings_redirect("operation_failed")
    return _pipeline_settings_redirect("stage_created")


@router.post(
    "/pipelines-settings/stages/{stage_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
@router.post(
    "/pipelines/stages/{stage_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
)
def pipeline_stage_update(
    request: Request,
    stage_id: str,
    name: str = Form(...),
    order_index: int = Form(0),
    default_probability: int = Form(50),
    is_active: str | None = Form(default=None),
    stage_type: str = Form("standard"),
    color: str = Form("#06B6D4"),
    icon: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _ = request
    try:
        web_sales_service.update_stage_from_form(
            db,
            stage_id=stage_id,
            name=name,
            order_index=order_index,
            default_probability=default_probability,
            is_active=is_active,
            stage_type=stage_type,
            color=color,
            icon=icon,
        )
    except (HTTPException, ValidationError, ValueError):
        return _pipeline_settings_redirect("operation_failed")
    return _pipeline_settings_redirect("stage_updated")


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
    return _pipeline_settings_redirect("stage_deactivated")


@router.post(
    "/pipelines-settings/stages/{stage_id}/status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_status_update(
    request: Request,
    stage_id: str,
    is_active: bool = Form(...),
    db: Session = Depends(get_db),
):
    _ = request
    try:
        web_sales_service.set_stage_active(
            db,
            stage_id=stage_id,
            is_active=is_active,
        )
    except (HTTPException, ValidationError, ValueError):
        return _pipeline_settings_redirect("operation_failed")
    return _pipeline_settings_redirect(
        "stage_activated" if is_active else "stage_deactivated"
    )


@router.post(
    "/pipelines-settings/{pipeline_id}/stages/reorder",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def pipeline_stage_reorder(
    request: Request,
    pipeline_id: str,
    stage_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    _ = request
    try:
        web_sales_service.reorder_stages(
            db,
            pipeline_id=pipeline_id,
            stage_ids=stage_ids,
        )
    except (HTTPException, ValidationError, ValueError):
        return _pipeline_settings_redirect("operation_failed")
    return _pipeline_settings_redirect("stages_reordered")


@router.post(
    "/pipelines-settings/{pipeline_id}/bulk-assign-leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
@router.post(
    "/pipelines/{pipeline_id}/bulk-assign-leads",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:lead:write"))],
    include_in_schema=False,
)
def pipeline_bulk_assign_leads(
    request: Request,
    pipeline_id: str,
    stage_id: str | None = Form(default=None),
    scope: str = Form("unassigned"),
    db: Session = Depends(get_db),
):
    _ = request
    try:
        count = web_sales_service.bulk_assign_leads(
            db, pipeline_id=pipeline_id, stage_id=stage_id, scope=scope
        )
    except (HTTPException, ValidationError, ValueError):
        return _pipeline_settings_redirect("operation_failed")
    return _pipeline_settings_redirect("bulk_assigned", bulk_count=count)


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
    try:
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
    except SQLAlchemyError as exc:
        logger.error(
            "sales_quotes_list_load_failed",
            extra={
                "error_type": type(exc).__name__,
                "route": "/admin/sales/quotes",
            },
        )
        db_session_adapter.release_read_transaction(db)
        state = web_sales_service.build_quotes_failure_context(
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
    submission_id: str | None = Form(default=None),
    lead_id: str | None = Form(default=None),
    status: str | None = Form(default=None),
    currency: str | None = Form(default=None),
    project_type: str | None = Form(default=None),
    tax_rate_id: str | None = Form(default=None),
    manual_tax_total: str | None = Form(default=None),
    expires_at: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    latitude: str | None = Form(default=None),
    longitude: str | None = Form(default=None),
    address: str | None = Form(default=None),
    region: str | None = Form(default=None),
    item_description: list[str] = Form(default=[]),
    item_quantity: list[str] = Form(default=[]),
    item_unit_price: list[str] = Form(default=[]),
    item_discount_percent: list[str] = Form(default=[]),
    item_sub_offer_id: list[str] = Form(default=[]),
    item_inventory_item_id: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    active = is_active is not None
    items = web_sales_service.quote_form_item_rows(
        descriptions=item_description,
        quantities=item_quantity,
        unit_prices=item_unit_price,
        discount_percents=item_discount_percent,
        sub_offer_ids=item_sub_offer_id,
        inventory_item_ids=item_inventory_item_id,
    )
    fields = {
        "submission_id": submission_id,
        "lead_id": lead_id,
        "status": status,
        "currency": currency,
        "project_type": project_type,
        "tax_rate_id": tax_rate_id,
        "manual_tax_total": manual_tax_total,
        "expires_at": expires_at,
        "is_active": active,
        "notes": notes,
        "latitude": latitude,
        "longitude": longitude,
        "address": address,
        "region": region,
        "items": items,
    }
    try:
        web_sales_service.create_quote_from_form(
            db,
            actor_system_user_id=_quote_actor_system_user_id(request),
            submission_id=submission_id,
            lead_id=lead_id,
            status=status,
            currency=currency,
            project_type=project_type,
            tax_rate_id=tax_rate_id,
            manual_tax_total=manual_tax_total,
            expires_at=expires_at,
            is_active=active,
            notes=notes,
            latitude=latitude,
            longitude=longitude,
            address=address,
            region=region,
            descriptions=item_description,
            quantities=item_quantity,
            unit_prices=item_unit_price,
            discount_percents=item_discount_percent,
            sub_offer_ids=item_sub_offer_id,
            inventory_item_ids=item_inventory_item_id,
        )
        return RedirectResponse(url="/admin/sales/quotes", status_code=303)
    except (DomainError, ValidationError, ValueError) as exc:
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-quotes")
    context.update(
        web_sales_service.build_quote_form_error_context(
            db, mode="create", quote_id=None, **fields
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


@router.post(
    "/quotes/{quote_id}/pdf",
    dependencies=[Depends(require_permission("crm:quote:read"))],
)
def quote_pdf_download(
    request: Request,
    quote_id: str,
    db: Session = Depends(get_db),
):
    try:
        outcome = quote_documents.generate_quote_pdf(
            db,
            quote_documents.GenerateQuotePdfCommand(
                context=_quote_command_context(request, quote_id, action="pdf-export"),
                quote_id=UUID(quote_id),
            ),
        )
        export = quote_documents.get_export(db, outcome.export_id)
        stream = quote_documents.stream_export(db, export)
    except (DomainError, ValueError) as exc:
        context = _ctx(request, db, "sales-quotes")
        context.update(
            web_sales_service.build_quote_detail_context(db, quote_id=quote_id)
        )
        context["error"] = _error_detail(exc)
        return templates.TemplateResponse(
            "admin/sales/quotes/detail.html", context, status_code=400
        )
    headers = {"Content-Disposition": build_content_disposition(outcome.filename)}
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type="application/pdf",
        headers=headers,
    )


@router.post(
    "/quotes/{quote_id}/send-email",
    dependencies=[Depends(require_permission("crm:quote:send"))],
)
def quote_send_email(
    request: Request,
    quote_id: str,
    request_id: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        command_id = UUID(request_id)
        outcome = quote_delivery.send_quote_email(
            db,
            quote_delivery.SendQuoteEmailCommand(
                context=CommandContext.system(
                    actor=str(
                        getattr(request.state, "actor_id", None) or "admin-sales-user"
                    ),
                    scope="sales:quote-delivery",
                    reason="Admin requested branded Quote email",
                    command_id=command_id,
                    idempotency_key=f"quote-email:{quote_id}:{command_id}",
                ),
                quote_id=UUID(quote_id),
            ),
        )
    except (DomainError, ValueError) as exc:
        context = _ctx(request, db, "sales-quotes")
        context.update(
            web_sales_service.build_quote_detail_context(db, quote_id=quote_id)
        )
        context["error"] = _error_detail(exc)
        return templates.TemplateResponse(
            "admin/sales/quotes/detail.html", context, status_code=400
        )
    notice = "email_queued" if outcome.queued else "email_suppressed"
    return RedirectResponse(
        url=f"/admin/sales/quotes/{quote_id}?notice={notice}", status_code=303
    )


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
        web_sales_service.update_quote_from_form(
            db,
            quote_id=quote_id,
            context=_quote_command_context(request, quote_id, action="update"),
            **fields,
        )
        return RedirectResponse(url=f"/admin/sales/quotes/{quote_id}", status_code=303)
    except (DomainError, ValidationError, ValueError) as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "sales-quotes")
    context.update(
        web_sales_service.build_quote_form_error_context(
            db, mode="update", quote_id=quote_id, **fields
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
            context=_quote_command_context(request, quote_id, action="line-add"),
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
def quote_line_item_delete(
    request: Request,
    quote_id: str,
    item_id: str,
    db: Session = Depends(get_db),
):
    web_sales_service.delete_quote_line_item(
        db,
        item_id,
        context=_quote_command_context(request, quote_id, action="line-remove"),
    )
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
        web_sales_service.set_quote_status(
            db,
            quote_id,
            status,
            context=_quote_command_context(
                request,
                quote_id,
                action="accept" if status == "accepted" else "status",
            ),
        )
    except (DomainError, ValidationError, ValueError) as exc:
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
def quote_delete(
    request: Request,
    quote_id: str,
    db: Session = Depends(get_db),
):
    web_sales_service.deactivate_quote(
        db,
        quote_id,
        context=_quote_command_context(request, quote_id, action="deactivate"),
    )
    return RedirectResponse(url="/admin/sales/quotes", status_code=303)


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------


@router.get(
    "/sales-order",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
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
    owner_agent_id: str | None = Query(default=None),
    lead_source: str | None = Query(default=None),
    period: str | None = Query(default=None),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
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
        owner_agent_id=owner_agent_id,
        lead_source=lead_source,
        period=period,
        from_date=from_date,
        to_date=to_date,
        search=search,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )
    if request.url.path.endswith("/sales-orders"):
        return RedirectResponse(
            url=state["list_query"].url("/admin/sales/sales-order"),
            status_code=307,
        )
    if state["canonicalization_needed"]:
        return RedirectResponse(
            url=state["list_query"].url("/admin/sales/sales-order"),
            status_code=307,
        )
    context = _ctx(request, db, "sales-orders")
    context.update(state)
    return templates.TemplateResponse("admin/sales/sales_orders/index.html", context)


@router.get(
    "/sales-order/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def sales_order_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-orders")
    context.update(web_sales_service.build_sales_order_form_context(db))
    return templates.TemplateResponse("admin/sales/sales_orders/form.html", context)


@router.post(
    "/sales-order/new",
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
async def sales_order_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        order = web_sales_service.save_manual_sales_order(
            db,
            sales_order_id=None,
            subscriber_id=str(form.get("subscriber_id") or ""),
            owner_agent_id=str(form.get("owner_agent_id") or ""),
            source=str(form.get("source") or ""),
            project_type=str(form.get("project_type") or ""),
            status=str(form.get("status") or ""),
            payment_status=str(form.get("payment_status") or ""),
            amount_paid=str(form.get("amount_paid") or "0"),
            paid_at=str(form.get("paid_at") or ""),
            notes=str(form.get("notes") or ""),
            descriptions=[str(item) for item in form.getlist("description")],
            quantities=[str(item) for item in form.getlist("quantity")],
            unit_prices=[str(item) for item in form.getlist("unit_price")],
            inventory_item_ids=[
                str(item) for item in form.getlist("inventory_item_id")
            ],
            subscription_plan_ids=[
                str(item) for item in form.getlist("subscription_plan_id")
            ],
        )
    except (ValueError, ValidationError) as exc:
        context = _ctx(request, db, "sales-orders")
        context.update(web_sales_service.build_sales_order_form_context(db))
        context.update({"form_error": _error_detail(exc), "form_data": dict(form)})
        return templates.TemplateResponse(
            "admin/sales/sales_orders/form.html", context, status_code=422
        )
    return RedirectResponse(url=f"/admin/sales/sales-order/{order.id}", status_code=303)


@router.get(
    "/sales-order/{order_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def sales_order_edit(request: Request, order_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "sales-orders")
    context.update(
        web_sales_service.build_sales_order_form_context(db, sales_order_id=order_id)
    )
    return templates.TemplateResponse("admin/sales/sales_orders/form.html", context)


@router.post(
    "/sales-order/{order_id}/edit",
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
async def sales_order_update(
    request: Request, order_id: str, db: Session = Depends(get_db)
):
    form = await request.form()
    try:
        order = web_sales_service.save_manual_sales_order(
            db,
            sales_order_id=order_id,
            subscriber_id=str(form.get("subscriber_id") or ""),
            owner_agent_id=str(form.get("owner_agent_id") or ""),
            source=str(form.get("source") or ""),
            project_type=str(form.get("project_type") or ""),
            status=str(form.get("status") or ""),
            payment_status=str(form.get("payment_status") or ""),
            amount_paid=str(form.get("amount_paid") or "0"),
            paid_at=str(form.get("paid_at") or ""),
            notes=str(form.get("notes") or ""),
            descriptions=[str(item) for item in form.getlist("description")],
            quantities=[str(item) for item in form.getlist("quantity")],
            unit_prices=[str(item) for item in form.getlist("unit_price")],
            inventory_item_ids=[
                str(item) for item in form.getlist("inventory_item_id")
            ],
            subscription_plan_ids=[
                str(item) for item in form.getlist("subscription_plan_id")
            ],
            line_ids=[str(item) for item in form.getlist("line_id")],
        )
    except (ValueError, ValidationError) as exc:
        context = _ctx(request, db, "sales-orders")
        context.update(
            web_sales_service.build_sales_order_form_context(
                db, sales_order_id=order_id
            )
        )
        context.update({"form_error": _error_detail(exc), "form_data": dict(form)})
        return templates.TemplateResponse(
            "admin/sales/sales_orders/form.html", context, status_code=422
        )
    return RedirectResponse(url=f"/admin/sales/sales-order/{order.id}", status_code=303)


@router.post(
    "/sales-order/{order_id}/delete",
    dependencies=[Depends(require_permission("crm:sales_order:write"))],
)
def sales_order_delete(order_id: str, db: Session = Depends(get_db)):
    web_sales_service.delete_sales_order(db, order_id)
    return RedirectResponse(url="/admin/sales/sales-order", status_code=303)


@router.get(
    "/sales-order/{order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
@router.get(
    "/sales-orders/{order_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("crm:sales_order:read"))],
)
def sales_order_detail(request: Request, order_id: str, db: Session = Depends(get_db)):
    if request.url.path.startswith("/admin/sales/sales-orders/"):
        return RedirectResponse(
            url=f"/admin/sales/sales-order/{order_id}", status_code=307
        )
    context = _ctx(request, db, "sales-orders")
    context.update(
        web_sales_service.build_sales_order_detail_context(db, sales_order_id=order_id)
    )
    return templates.TemplateResponse("admin/sales/sales_orders/detail.html", context)
