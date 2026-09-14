"""Service helpers for reseller portal routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.auth import MFAMethod
from app.services import auth_flow as auth_flow_service
from app.services import (
    customer_portal,
    customer_portal_flow_changes,
    customer_work_order_selfcare,
    reseller_crm_views,
    reseller_portal,
    reseller_ticket_projection,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    PageMeta,
    request_needs_canonicalization,
)
from app.services.owner_commands import CommandContext
from app.services.portal_account_health import build_portal_account_health
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.web.reseller.branding import get_reseller_templates

logger = logging.getLogger(__name__)

# The reseller accounts list's declared capabilities. Sortable keys mirror the
# reseller_portal.list_accounts order_by whitelist; per_page keeps the reseller
# portal's 20 default.
RESELLER_ACCOUNT_LIST_DEFINITION = ListDefinition(
    key="reseller_accounts",
    fields=(
        ListFieldDefinition("name", "Customer", searchable=True, sortable=True),
        ListFieldDefinition("status_filter", "Status", filterable=True),
        ListFieldDefinition("balance", "Balance", sortable=True),
        ListFieldDefinition("overdue", "Overdue", sortable=True),
        ListFieldDefinition("created_at", "Joined", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
    per_page_options=(10, 20, 50, 100),
    default_per_page=20,
)

# Per-account invoices sub-list. Sortable keys mirror list_account_invoices'
# ACCOUNT_INVOICE_SORT_COLUMNS.
RESELLER_INVOICE_LIST_DEFINITION = ListDefinition(
    key="reseller_account_invoices",
    fields=(
        ListFieldDefinition("status", "Status", sortable=True),
        ListFieldDefinition("total", "Total", sortable=True),
        ListFieldDefinition("due_at", "Due", sortable=True),
        ListFieldDefinition("issued_at", "Issued", sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="issued_at",
    default_sort_dir="desc",
    per_page_options=(10, 25, 50, 100),
    default_per_page=25,
)

templates = get_reseller_templates()

SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE = (
    "The verified prepaid funding position is still under review. "
    "Finance must complete reconstruction before this service can change."
)


RESELLER_LOGIN_URL = "/reseller/auth/login"


def _require_reseller_context(request: Request, db: Session):
    context = reseller_portal.get_context(
        db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    # Surface admin "view as reseller" state to the layout banner (read in
    # reseller.html via request.state, like the customer portal banner). Set on
    # every guarded request so any reseller page shows the exit control.
    request.state.reseller_impersonation = {
        "active": bool(context.get("is_impersonation")),
        "return_to": context.get("return_to") or "/admin/resellers",
    }
    request.state.reseller_context = context
    try:
        from app.services.brand_profiles import resolve_brand

        request.state.reseller_brand = resolve_brand(
            db, reseller_id=context["reseller"].id
        ).to_dict()
    except Exception:
        logger.debug("Failed to attach reseller branding to request context")
    return context


def require_reseller_context(request: Request, db: Session = Depends(get_db)):
    """Router-level auth guard for the reseller portal.

    Attached to the portal router via ``dependencies=`` so every route is
    protected structurally rather than by per-handler convention — a new
    route cannot accidentally ship unauthenticated. Returns the reseller
    context (handlers may also depend on it directly); raises a 303 redirect
    to the login page when there is no valid reseller session.
    """
    context = _require_reseller_context(request, db)
    if not context:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": RESELLER_LOGIN_URL},
        )
    return context


def _profile_context(request: Request, context, **extra):
    subscriber = context["subscriber"]
    mfa_methods = (
        db_methods if (db_methods := extra.pop("mfa_methods", None)) is not None else []
    )
    return {
        "request": request,
        "active_page": "profile",
        "current_user": context["current_user"],
        "reseller": context["reseller"],
        "subscriber": subscriber,
        "mfa_methods": mfa_methods,
        "mfa_enabled": any(
            bool(method.enabled and method.is_active) for method in mfa_methods
        ),
        **extra,
    }


def _reseller_mfa_methods(db: Session, context) -> list[MFAMethod]:
    """MFA methods for the acting principal — subscriber-backed reseller login
    or a first-class reseller_user (Layer 3)."""
    query = db.query(MFAMethod).filter(MFAMethod.is_active.is_(True))
    if context.get("principal_type") == "reseller_user":
        query = query.filter(MFAMethod.reseller_user_id == context["principal_id"])
    else:
        query = query.filter(MFAMethod.subscriber_id == context["principal_id"])
    return query.order_by(MFAMethod.created_at.desc()).all()


def reseller_home(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    return RedirectResponse(url="/reseller/dashboard", status_code=303)


def reseller_dashboard(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    summary = reseller_portal.get_dashboard_summary(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )
    customer_statuses = reseller_portal.list_customer_connection_statuses(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )

    account_ids = [
        UUID(str(account["id"]))
        for account in summary.get("accounts", [])
        if account.get("id")
    ]
    ticket_count = reseller_ticket_projection.native_open_ticket_count(
        db,
        query=reseller_ticket_projection.ResellerTicketCountQuery(
            reseller_id=UUID(str(context["reseller"].id)),
            account_ids=tuple(account_ids),
        ),
    )
    open_tickets = ticket_count.value
    open_tickets_state = reseller_portal.StateValue.present(ticket_count.value)

    return templates.TemplateResponse(
        "reseller/dashboard/index.html",
        {
            "request": request,
            "active_page": "dashboard",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
            "kpis": reseller_portal.dashboard_kpis(summary),
            "customer_statuses": customer_statuses,
            "open_tickets": open_tickets,
            "open_tickets_state": open_tickets_state,
            "page": page,
            "per_page": per_page,
        },
    )


def reseller_accounts(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
    search: str | None = None,
    status_filter: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    definition = RESELLER_ACCOUNT_LIST_DEFINITION
    requested_status_filter = status_filter
    if status_filter not in reseller_portal.ACCOUNT_LIST_STATUS_OPTIONS:
        status_filter = None
    safe_sort = (
        sort_by if sort_by in definition.sortable_keys else definition.default_sort
    )
    safe_dir = sort_dir if sort_dir in ("asc", "desc") else None
    safe_per_page = (
        per_page
        if per_page in definition.per_page_options
        else definition.default_per_page
    )
    requested_query = definition.build_query(
        search=search,
        filters={"status_filter": status_filter},
        sort_by=safe_sort,
        sort_dir=safe_dir,
        page=max(1, page),
        per_page=safe_per_page,
    )
    reseller_id = str(context["reseller"].id)
    total = reseller_portal.count_accounts(
        db,
        reseller_id=reseller_id,
        search=requested_query.search,
        status_filter=status_filter,
    )
    page_meta = PageMeta.from_query(requested_query, total)
    list_query = requested_query.with_page(page_meta.page)
    accounts = reseller_portal.list_accounts(
        db,
        reseller_id=reseller_id,
        limit=list_query.per_page,
        offset=(page_meta.page - 1) * list_query.per_page,
        search=list_query.search,
        status_filter=status_filter,
        order_by=list_query.sort_by,
        order_dir=list_query.sort_dir,
    )
    if request_needs_canonicalization(
        list_query,
        search=search,
        filters={"status_filter": requested_status_filter},
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    ):
        return RedirectResponse(
            url=list_query.url("/reseller/accounts"), status_code=307
        )
    return templates.TemplateResponse(
        "reseller/accounts/index.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "accounts": accounts,
            "list_query": list_query,
            "page_meta": page_meta,
            "page": page_meta.page,
            "per_page": page_meta.per_page,
            "search": list_query.search or "",
            "status_filter": status_filter or "",
            "status_options": reseller_portal.ACCOUNT_LIST_STATUS_OPTIONS,
            "total": total,
            "total_pages": page_meta.total_pages,
            "has_prev": page_meta.has_previous,
            "has_next": page_meta.has_next,
        },
    )


def reseller_account_view(
    request: Request,
    db: Session,
    account_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    session_token = reseller_portal.create_customer_impersonation_session(
        db=db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
        return_to="/reseller/accounts",
    )
    response = RedirectResponse(url="/portal/dashboard", status_code=303)
    response.set_cookie(
        key=customer_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=customer_portal.get_session_max_age(db),
    )
    return response


def reseller_account_detail(
    request: Request,
    db: Session,
    account_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    detail = reseller_portal.get_account_detail(
        db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
    )
    if not detail:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "reseller/accounts/detail.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "account": detail,
            "account_health": build_portal_account_health(db, UUID(detail["id"])),
            "status_action_contracts": reseller_portal.account_status_action_contracts(
                detail["status_actions"]
            ),
            "status_success": request.query_params.get("status_success"),
            "status_error": request.query_params.get("status_error"),
        },
    )


def reseller_service_change_page(
    request: Request,
    db: Session,
    account_id: str,
    subscription_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    account = reseller_portal.owned_account(db, str(context["reseller"].id), account_id)
    if account is None:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )
    page = customer_portal_flow_changes.get_change_plan_page(
        db, {"account_id": str(account.id)}, subscription_id
    )
    if page is None:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )
    return templates.TemplateResponse(
        "reseller/accounts/service_change.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "account": account,
            **page,
        },
    )


def reseller_service_change_quote(
    request: Request,
    db: Session,
    account_id: str,
    subscription_id: str,
    offer_id: str,
    target_service_address_id: str | None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    account = reseller_portal.owned_account(db, str(context["reseller"].id), account_id)
    if account is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        quote = customer_portal_flow_changes.get_plan_change_quote(
            db,
            {"account_id": str(account.id)},
            subscription_id,
            offer_id,
            target_service_address_id=target_service_address_id,
        )
    except PrepaidFundingBaselineMissingError:
        return JSONResponse(
            {
                "error": "financial_position_unavailable",
                "detail": SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE,
            },
            status_code=409,
        )
    if quote is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"quote": quote})


def reseller_service_change_confirm(
    request: Request,
    db: Session,
    account_id: str,
    subscription_id: str,
    *,
    offer_id: str,
    target_service_address_id: str | None,
    preview_fingerprint: str,
    field_quote_fingerprint: str | None,
    preview_effective_at: str,
    idempotency_key: str,
    notes: str | None,
):
    from datetime import datetime
    from urllib.parse import quote_plus

    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    account = reseller_portal.owned_account(db, str(context["reseller"].id), account_id)
    if account is None:
        return RedirectResponse(url="/reseller/accounts", status_code=303)
    try:
        result = customer_portal_flow_changes.confirm_service_change(
            db,
            {"account_id": str(account.id)},
            subscription_id,
            offer_id,
            notes,
            target_service_address_id=target_service_address_id,
            preview_fingerprint=preview_fingerprint,
            field_quote_fingerprint=field_quote_fingerprint,
            preview_effective_at=datetime.fromisoformat(preview_effective_at),
            idempotency_key=idempotency_key,
            confirmation_origin="reseller_web",
        )
        if not result.get("success", False):
            raise ValueError("Additional prepaid funding is required")
    except (HTTPException, PrepaidFundingBaselineMissingError, ValueError) as exc:
        db.rollback()
        message = (
            SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE
            if isinstance(exc, PrepaidFundingBaselineMissingError)
            else str(getattr(exc, "detail", exc))
        )
        return RedirectResponse(
            url=(
                f"/reseller/accounts/{account_id}?status_error=" + quote_plus(message)
            ),
            status_code=303,
        )
    return RedirectResponse(
        url=(
            f"/reseller/accounts/{account_id}?status_success="
            + quote_plus(str(result.get("message") or "Service change confirmed"))
        ),
        status_code=303,
    )


def reseller_account_status_update(
    request: Request,
    db: Session,
    account_id: str,
    action: str,
    preview_fingerprint: str,
):
    from urllib.parse import quote_plus

    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    try:
        proposal = reseller_portal.preview_customer_account_status_confirmation(
            db,
            reseller_portal.PreviewCustomerAccountStatusConfirmationRequest(
                reseller_id=context["reseller"].id,
                account_id=UUID(account_id),
                action=reseller_portal.ResellerAccountStatusAction(
                    action.strip().lower()
                ),
                expected_preview_fingerprint=preview_fingerprint,
            ),
        )
    except DomainError as exc:
        message = exc.message
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus(message)}",
            status_code=303,
        )
    except ValueError:
        message = "Unsupported status action"
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus(message)}",
            status_code=303,
        )
    except Exception:
        logger.warning("reseller_account_status_update_failed", exc_info=True)
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus('Unable to preview account status change')}",
            status_code=303,
        )

    if not proposal:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "reseller/accounts/status_confirm.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "proposal": proposal,
        },
    )


def reseller_account_status_confirm(
    request: Request,
    db: Session,
    account_id: str,
    action: str,
    preview_fingerprint: str,
    idempotency_key: str,
):
    from urllib.parse import quote_plus

    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    try:
        reseller_id = context["reseller"].id
        resolved_account_id = UUID(account_id)
        resolved_action = reseller_portal.ResellerAccountStatusAction(
            action.strip().lower()
        )
        actor = f"reseller:{reseller_id}:principal:{context['principal_id']}"
        command = reseller_portal.ConfirmCustomerAccountStatusCommand(
            context=CommandContext.system(
                actor=actor,
                scope=str(resolved_account_id),
                reason=f"confirm_{resolved_action.value}_account",
                idempotency_key=idempotency_key,
            ),
            reseller_id=reseller_id,
            account_id=resolved_account_id,
            action=resolved_action,
            expected_preview_fingerprint=preview_fingerprint,
        )
        db_session_adapter.release_read_transaction(db)
        result = reseller_portal.confirm_customer_account_status_action(
            db,
            command,
        )
    except DomainError as exc:
        message = exc.message
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus(message)}",
            status_code=303,
        )
    except ValueError:
        message = "Unsupported status action"
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus(message)}",
            status_code=303,
        )
    except Exception:
        logger.warning("reseller_account_status_confirm_failed", exc_info=True)
        return RedirectResponse(
            url=f"/reseller/accounts/{account_id}?status_error={quote_plus('Unable to update account status')}",
            status_code=303,
        )

    if not result:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    status_label = str(result.get("status") or "updated").replace("_", " ").title()
    if action.strip().lower() == "deactivate":
        status_label = "Deactivated"
    suffix = " (already processed)" if result.get("replayed") else ""
    return RedirectResponse(
        url=f"/reseller/accounts/{account_id}?status_success={quote_plus(f'Account status changed to {status_label}{suffix}')}",
        status_code=303,
    )


def reseller_account_invoices(
    request: Request,
    db: Session,
    account_id: str,
    page: int,
    per_page: int,
    sort_by: str | None = None,
    sort_dir: str | None = None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    reseller_id = str(context["reseller"].id)
    total = reseller_portal.count_account_invoices(
        db, reseller_id=reseller_id, account_id=account_id
    )
    if total is None:  # account not owned by this reseller
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    definition = RESELLER_INVOICE_LIST_DEFINITION
    safe_sort = (
        sort_by if sort_by in definition.sortable_keys else definition.default_sort
    )
    safe_dir = sort_dir if sort_dir in ("asc", "desc") else None
    safe_per_page = (
        per_page
        if per_page in definition.per_page_options
        else definition.default_per_page
    )
    requested_query = definition.build_query(
        search=None,
        filters={},
        sort_by=safe_sort,
        sort_dir=safe_dir,
        page=max(1, page),
        per_page=safe_per_page,
    )
    page_meta = PageMeta.from_query(requested_query, total)
    list_query = requested_query.with_page(page_meta.page)
    invoices = (
        reseller_portal.list_account_invoices(
            db,
            reseller_id=reseller_id,
            account_id=account_id,
            limit=list_query.per_page,
            offset=(page_meta.page - 1) * list_query.per_page,
            order_by=list_query.sort_by,
            order_dir=list_query.sort_dir,
        )
        or []
    )

    if request_needs_canonicalization(
        list_query,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    ):
        return RedirectResponse(
            url=list_query.url(f"/reseller/accounts/{account_id}/invoices"),
            status_code=307,
        )

    return templates.TemplateResponse(
        "reseller/accounts/invoices.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "invoices": invoices,
            "account_id": account_id,
            "list_query": list_query,
            "page_meta": page_meta,
            "page": page_meta.page,
            "per_page": page_meta.per_page,
            "total": total,
            "total_pages": page_meta.total_pages,
        },
    )


def reseller_invoice_detail(
    request: Request,
    db: Session,
    account_id: str,
    invoice_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    invoice = reseller_portal.get_invoice_detail(
        db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
        invoice_id=invoice_id,
    )
    if not invoice:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "reseller/accounts/invoice_detail.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "invoice": invoice,
            "account_id": account_id,
        },
    )


def reseller_revenue_report(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    summary = reseller_portal.get_revenue_summary(
        db, reseller_id=str(context["reseller"].id)
    )

    return templates.TemplateResponse(
        "reseller/reports/revenue.html",
        {
            "request": request,
            "active_page": "billing",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
            "kpis": reseller_portal.revenue_kpis(summary),
        },
    )


def reseller_profile(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    mfa_methods = _reseller_mfa_methods(db, context)
    current_session_token = request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    active_sessions = reseller_portal.list_reseller_sessions_for_principal(
        context["principal_id"],
        current_session_token=current_session_token,
    )
    return templates.TemplateResponse(
        "reseller/profile/index.html",
        _profile_context(
            request,
            context,
            mfa_methods=mfa_methods,
            active_sessions=active_sessions,
            other_session_count=sum(
                1 for session in active_sessions if not session["is_current"]
            ),
            success="Other portal sessions signed out."
            if request.query_params.get("sessions") == "signed-out"
            else None,
            verify_sent=request.query_params.get("verify_sent"),
        ),
    )


def reseller_profile_sign_out_other_sessions(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    current_session_token = request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    reseller_portal.revoke_other_reseller_sessions_for_principal(
        context["principal_id"],
        current_session_token,
        db=db,
    )
    return RedirectResponse(
        url="/reseller/profile?sessions=signed-out", status_code=303
    )


def reseller_resend_email_verification(request: Request, db: Session):
    """Resend the email-verification link to the reseller's login subscriber."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    sent = False
    # Email verification is a subscriber-account concept; a first-class
    # reseller_user principal has no backing subscriber, so this is a no-op
    # (the profile template only shows the verify card when a subscriber exists).
    subscriber = context["subscriber"]
    if subscriber is not None:
        try:
            sent = auth_flow_service.send_email_verification(db, str(subscriber.id))
        except Exception:
            logger.warning("reseller_resend_email_verification_failed", exc_info=True)
    return RedirectResponse(
        url=f"/reseller/profile?verify_sent={'1' if sent else '0'}",
        status_code=303,
    )


def reseller_profile_update(
    request: Request,
    db: Session,
    contact_email: str | None,
    contact_phone: str | None,
    notes: str | None,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    reseller = context["reseller"]
    if contact_email is not None:
        reseller.contact_email = contact_email.strip() or None
    if contact_phone is not None:
        reseller.contact_phone = contact_phone.strip() or None
    if notes is not None:
        reseller.notes = notes.strip() or None
    db.commit()
    db.refresh(reseller)
    mfa_methods = _reseller_mfa_methods(db, context)

    return templates.TemplateResponse(
        "reseller/profile/index.html",
        _profile_context(
            request,
            {**context, "reseller": reseller},
            mfa_methods=mfa_methods,
            success="Profile updated successfully.",
        ),
    )


def reseller_mfa_setup(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    if context["principal_type"] == "reseller_user":
        setup = auth_flow_service.auth_flow.reseller_mfa_setup(
            db, context["principal_id"], "Authenticator app"
        )
    else:
        setup = auth_flow_service.auth_flow.mfa_setup(
            db, context["principal_id"], "Authenticator app"
        )
    return templates.TemplateResponse(
        "reseller/profile/mfa_setup.html",
        {
            "request": request,
            "active_page": "profile",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "method_id": setup["method_id"],
            "secret_key": setup["secret"],
            "otpauth_uri": setup["otpauth_uri"],
        },
    )


def reseller_mfa_confirm(request: Request, db: Session, method_id: str, code: str):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    try:
        if context["principal_type"] == "reseller_user":
            method = auth_flow_service.auth_flow.reseller_mfa_confirm(
                db, method_id, code.strip(), context["principal_id"]
            )
        else:
            method = auth_flow_service.auth_flow.mfa_confirm(
                db, method_id, code.strip(), context["principal_id"]
            )
    except Exception:
        return templates.TemplateResponse(
            "reseller/profile/mfa_setup.html",
            {
                "request": request,
                "active_page": "profile",
                "current_user": context["current_user"],
                "reseller": context["reseller"],
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "error": "Invalid verification code. Please try again.",
            },
            status_code=401,
        )

    recovery_codes = (
        auth_flow_service.generate_mfa_recovery_codes(db, method)
        if getattr(method, "id", None)
        else []
    )
    if recovery_codes:
        return templates.TemplateResponse(
            "reseller/profile/mfa_setup.html",
            {
                "request": request,
                "active_page": "profile",
                "current_user": context["current_user"],
                "reseller": context["reseller"],
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "recovery_codes": recovery_codes,
                "continue_url": "/reseller/profile",
            },
        )

    mfa_methods = _reseller_mfa_methods(db, context)
    return templates.TemplateResponse(
        "reseller/profile/index.html",
        _profile_context(
            request,
            context,
            mfa_methods=mfa_methods,
            success="Two-factor authentication enabled.",
        ),
    )


def reseller_account_tickets(
    request: Request,
    db: Session,
    account_id: str,
):
    """Show native support tickets for a reseller's customer account."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    # Verify reseller owns this account
    detail = reseller_portal.get_account_detail(
        db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
    )
    if not detail:
        return templates.TemplateResponse(
            "reseller/errors/404.html",
            {
                "request": request,
                "current_user": context["current_user"],
                "reseller": context["reseller"],
            },
            status_code=404,
        )

    tickets = reseller_ticket_projection.native_account_ticket_summaries(
        db,
        query=reseller_ticket_projection.ResellerAccountTicketsQuery(
            reseller_id=UUID(str(context["reseller"].id)),
            account_id=UUID(account_id),
        ),
    )
    return templates.TemplateResponse(
        "reseller/accounts/tickets.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "account": detail,
            "account_id": account_id,
            "tickets": [ticket.to_dict() for ticket in tickets],
        },
    )


def reseller_fiber_map(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    from app.services import web_network_fiber as fiber_service

    map_data = fiber_service.get_fiber_plant_map_data(db)

    return templates.TemplateResponse(
        "reseller/network/fiber-map.html",
        {
            "request": request,
            "active_page": "fiber-map",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            **map_data,
            "read_only": True,
        },
    )


def reseller_quotes_page(request: Request, db: Session):
    """Sales/Quotes across the reseller's customers: quotes, installs, visits."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    from app.services import reseller_crm_views

    reseller_id = str(context["reseller"].id)
    return templates.TemplateResponse(
        "reseller/quotes/index.html",
        {
            "request": request,
            "active_page": "quotes",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "quotes": reseller_crm_views.quotes_for_reseller(db, reseller_id),
            "projects": reseller_crm_views.projects_for_reseller(db, reseller_id),
            "work_orders": reseller_crm_views.work_orders_for_reseller(db, reseller_id),
        },
    )


def reseller_service_requests_page(request: Request, db: Session):
    """New-service / installation requests: list + submission form."""
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    from app.services import reseller_service_requests

    items = reseller_service_requests.list_for_reseller(
        db, str(context["reseller"].id), limit=100, offset=0
    )
    return templates.TemplateResponse(
        "reseller/service-requests/index.html",
        {
            "request": request,
            "active_page": "service-requests",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "service_requests": items,
            "form_error": request.query_params.get("error"),
            "submitted": request.query_params.get("submitted") == "1",
        },
    )


def reseller_service_request_create(
    request: Request,
    db: Session,
    *,
    contact_name: str,
    contact_phone: str,
    contact_email: str,
    address: str,
    latitude: str,
    longitude: str,
    notes: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    from urllib.parse import quote_plus

    from app.services import reseller_service_requests

    def _coord(raw: str, low: float, high: float) -> float | None:
        try:
            value = float(raw.strip())
        except (TypeError, ValueError):
            return None
        return value if low <= value <= high else None

    lat = _coord(latitude, -90.0, 90.0)
    lon = _coord(longitude, -180.0, 180.0)
    if (lat is None) != (lon is None):
        lat = lon = None

    try:
        reseller_service_requests.create_request(
            db,
            str(context["reseller"].id),
            subscriber_id=None,
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            address=address,
            latitude=lat,
            longitude=lon,
            notes=notes,
        )
    except HTTPException as exc:
        return RedirectResponse(
            url=f"/reseller/service-requests?error={quote_plus(str(exc.detail))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/reseller/service-requests?submitted=1", status_code=303
    )


# ─── Field-service work orders (technician map + rating, per managed account) ──


def reseller_work_orders_page(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    tracker = reseller_crm_views.work_orders_for_reseller(
        db, str(context["reseller"].id)
    )
    return templates.TemplateResponse(
        "reseller/work_orders/index.html",
        {
            "request": request,
            "active_page": "work-orders",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "tracker": tracker,
        },
    )


def reseller_technician_location(
    request: Request, db: Session, account_id: str, work_order_id: str
):
    """Live tech position for a managed account's work order (polled). Same-origin,
    reseller-session-authed; gated to accounts the reseller owns."""
    context = _require_reseller_context(request, db)
    if not context:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    account = reseller_portal.owned_account(db, str(context["reseller"].id), account_id)
    if account is None:
        return JSONResponse({"available": False, "reason": "not_found"})
    data = customer_work_order_selfcare.technician_location(
        db, str(account.id), work_order_id
    )
    return JSONResponse(data.model_dump(mode="json"))


def reseller_rate_technician(
    request: Request,
    db: Session,
    account_id: str,
    work_order_id: str,
    rating: int,
    comment: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    account = reseller_portal.owned_account(db, str(context["reseller"].id), account_id)
    status_flag = "ok"
    if account is None:
        status_flag = "error"
    else:
        try:
            customer_work_order_selfcare.rate_technician(
                db,
                str(account.id),
                work_order_id,
                rating=max(1, min(5, rating)),
                comment=(comment or "")[:2000] or None,
            )
        except (LookupError, ValueError):
            status_flag = "error"
    return RedirectResponse(
        url=f"/reseller/work-orders?rated={status_flag}", status_code=303
    )
