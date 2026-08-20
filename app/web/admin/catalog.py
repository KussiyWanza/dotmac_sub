"""Admin catalog management web routes."""

import logging
from datetime import datetime
from typing import Any, cast
from urllib.parse import quote_plus
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.services import catalog as catalog_service
from app.services import radio_registration as radio_registration_service
from app.services import web_admin as web_admin_service
from app.services import web_bulk_tariff_change as web_bulk_tariff_change_service
from app.services import web_catalog_calculator as web_catalog_calculator_service
from app.services import web_catalog_offers as web_catalog_offers_service
from app.services import (
    web_catalog_subscription_workflows as web_catalog_subscription_workflows_service,
)
from app.services import web_catalog_subscriptions as web_catalog_subscriptions_service
from app.services import web_fup as web_fup_service
from app.services import (
    web_network_ont_assignments as web_network_ont_assignments_service,
)
from app.services.auth_dependencies import (
    has_permission,
    require_any_permission,
    require_permission,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionEffectiveTiming,
)
from app.services.topology.customer_path import resolve_customer_path
from app.web.request_parsing import parse_form_data, parse_form_data_sync

logger = logging.getLogger(__name__)

ADDITIONAL_IP_WRITE_PERMISSION = "subscription:additional_ip:write"
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog", tags=["web-admin-catalog"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "catalog"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _get_actor_id(request: Request) -> str | None:
    return web_admin_service.get_actor_id(request)


def _fup_command_context(
    request: Request,
    *,
    scope: str,
    reason: str,
) -> CommandContext:
    actor = _get_actor_id(request)
    if not actor:
        raise HTTPException(status_code=401, detail="Authenticated actor required")
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=actor,
        scope=scope,
        reason=reason,
    )


def _fup_http_error(exc: DomainError) -> HTTPException:
    suffix = exc.code.rsplit(".", 1)[-1]
    if suffix.endswith("not_found"):
        status_code = 404
    elif suffix in {"invalid_rule", "invalid_rule_chain"}:
        status_code = 400
    elif suffix == "active_caller_transaction":
        status_code = 409
    else:
        status_code = 500
    return HTTPException(status_code=status_code, detail=exc.message)


def _assert_lifecycle_command_permission(
    request: Request,
    db: Session,
    kind: SubscriptionCommandKind,
) -> None:
    auth = getattr(request.state, "auth", None) or {}
    if auth and has_permission(auth, db, "catalog:write"):
        return
    action_permission = {
        SubscriptionCommandKind.activate: "subscription:activate",
        SubscriptionCommandKind.restore: "subscription:activate",
        SubscriptionCommandKind.suspend: "subscription:suspend",
    }.get(kind)
    if action_permission and auth and has_permission(auth, db, action_permission):
        return
    raise HTTPException(status_code=403, detail="Forbidden")


def _assert_lifecycle_preview_permission(
    request: Request,
    db: Session,
    kind: SubscriptionCommandKind,
) -> None:
    """Allow catalog readers or the operator authorized for this exact action."""
    auth = getattr(request.state, "auth", None) or {}
    if auth and has_permission(auth, db, "catalog:read"):
        return
    _assert_lifecycle_command_permission(request, db, kind)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def catalog_overview(
    request: Request,
    status: str | None = None,
    plan_kind: str | None = None,
    plan_category: str | None = None,
    plan_family: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_catalog_offers_service.overview_page_data(
        db,
        status=status,
        plan_kind=plan_kind,
        plan_category=plan_category,
        plan_family=plan_family,
        search=search,
        page=page,
        per_page=per_page,
    )
    catalog_stats = web_catalog_offers_service.dashboard_stats(db)
    context = _base_context(request, db, active_page="catalog")
    context.update(page_data)
    context["catalog_stats"] = catalog_stats
    template_name = (
        "admin/catalog/_grid.html"
        if request.headers.get("HX-Request") == "true"
        else "admin/catalog/index.html"
    )
    return templates.TemplateResponse(template_name, context)


@router.get("/products", response_class=HTMLResponse)
def catalog_products(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/products/{path:path}", response_class=HTMLResponse)
def catalog_products_redirect(request: Request, path: str) -> RedirectResponse:
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/offers", response_class=HTMLResponse)
def catalog_offers(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse("/admin/catalog", status_code=302)


@router.get("/offers/create", response_class=HTMLResponse)
def catalog_offers_create(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    offer = web_catalog_offers_service.default_offer_form(db)
    context = _base_context(request, db, active_page="catalog")
    context.update(web_catalog_offers_service.offer_form_context(db, offer))
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post(
    "/offers",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("catalog:write")),
        Depends(require_permission("catalog:billing_write")),
    ],
)
def catalog_offers_create_post(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_offers_service.handle_offer_create_form(
        db,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.get("/offers/{offer_id}", response_class=HTMLResponse)
def catalog_offer_detail(
    request: Request, offer_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    detail_context = web_catalog_offers_service.offer_detail_context(db, offer_id)
    if detail_context is None:
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="catalog")
    context.update(detail_context)
    context.update(web_fup_service.fup_context(request, db, offer_id))
    return templates.TemplateResponse("admin/catalog/offer_detail.html", context)


@router.get("/offers/{offer_id}/edit", response_class=HTMLResponse)
def catalog_offer_edit(
    request: Request, offer_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    form_context = web_catalog_offers_service.offer_edit_form_context(db, offer_id)
    if form_context is None:
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="catalog")
    context.update(form_context)
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post(
    "/offers/{offer_id}/edit",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("catalog:write")),
        Depends(require_permission("catalog:billing_write")),
    ],
)
def catalog_offer_edit_post(
    request: Request,
    offer_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_offers_service.handle_offer_update_form(
        db,
        offer_id=offer_id,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("not_found"):
        context = _base_context(request, db, active_page="catalog")
        context.update({"message": "Offer not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/offer_form.html", context)


@router.post(
    "/offers/{offer_id}/archive",
    dependencies=[
        Depends(require_permission("catalog:write")),
        Depends(require_permission("catalog:billing_write")),
    ],
)
def catalog_offer_archive(
    request: Request, offer_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Archive an offer (soft-delete: status=archived, is_active=False)."""
    catalog_service.offers.delete(
        db,
        offer_id,
        actor_id=_get_actor_id(request),
        actor_type="user",
    )
    db.commit()
    return RedirectResponse("/admin/catalog/offers", status_code=303)


@router.post(
    "/offers/{offer_id}/restore",
    dependencies=[
        Depends(require_permission("catalog:write")),
        Depends(require_permission("catalog:billing_write")),
    ],
)
def catalog_offer_restore(
    request: Request, offer_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Restore an archived offer (status=active, is_active=True)."""
    catalog_service.offers.restore(
        db,
        offer_id,
        actor_id=_get_actor_id(request),
        actor_type="user",
    )
    db.commit()
    return RedirectResponse("/admin/catalog/offers?status=archived", status_code=303)


# ---------------------------------------------------------------------------
# FUP (Fair Usage Policy) routes
# ---------------------------------------------------------------------------


@router.get(
    "/offers/{offer_id}/fup",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_fup(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """FUP configuration page for a catalog offer."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_fup_service.fup_context(request, db, offer_id))
    context["fup_return_to"] = f"/admin/catalog/offers/{offer_id}/fup"
    return templates.TemplateResponse("admin/catalog/fup.html", context)


@router.post(
    "/offers/{offer_id}/fup/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_settings_update(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update FUP policy accounting settings."""
    form = parse_form_data_sync(request)
    context = _fup_command_context(
        request, scope=offer_id, reason="admin_fup_policy_update"
    )
    try:
        web_fup_service.handle_policy_update(db, offer_id, form, context)
    except DomainError as exc:
        raise _fup_http_error(exc) from exc
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/rules",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_add_rule(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Add a new FUP rule."""
    form = parse_form_data_sync(request)
    context = _fup_command_context(
        request, scope=offer_id, reason="admin_fup_rule_creation"
    )
    try:
        web_fup_service.handle_add_rule(db, offer_id, form, context)
    except DomainError as exc:
        raise _fup_http_error(exc) from exc
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/rules/{rule_id}/update",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_update_rule(
    offer_id: str, rule_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update an existing FUP rule."""
    form = parse_form_data_sync(request)
    context = _fup_command_context(
        request, scope=offer_id, reason="admin_fup_rule_update"
    )
    try:
        web_fup_service.handle_update_rule(db, rule_id, form, context)
    except DomainError as exc:
        raise _fup_http_error(exc) from exc
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/rules/{rule_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_delete_rule(
    offer_id: str, rule_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete an FUP rule."""
    form = parse_form_data_sync(request)
    context = _fup_command_context(
        request, scope=offer_id, reason="admin_fup_rule_deletion"
    )
    try:
        web_fup_service.handle_delete_rule(db, rule_id, context)
    except DomainError as exc:
        raise _fup_http_error(exc) from exc
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/clone",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def offer_fup_clone_rules(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Clone FUP rules from another offer."""
    form = parse_form_data_sync(request)
    source_offer_id = str(form.get("source_offer_id", ""))
    context = _fup_command_context(
        request, scope=offer_id, reason="admin_fup_rule_clone"
    )
    try:
        web_fup_service.handle_clone_rules(db, source_offer_id, offer_id, context)
    except DomainError as exc:
        raise _fup_http_error(exc) from exc
    return RedirectResponse(
        url=web_fup_service.redirect_to_fup_context(form, offer_id),
        status_code=303,
    )


@router.post(
    "/offers/{offer_id}/fup/simulate",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_fup_simulate(offer_id: str, request: Request, db: Session = Depends(get_db)):
    """Simulate FUP rules for a given usage scenario. Returns JSON for HTMX."""
    form = parse_form_data_sync(request)
    result = web_fup_service.simulate_offer_fup(db, offer_id, form)
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


@router.get(
    "/offers/{offer_id}/fup/rule-impact-preview",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_fup_rule_impact_preview(
    offer_id: str,
    threshold_amount: str,
    threshold_unit: str = "gb",
    direction: str = "up_down",
    consumption_period: str = "monthly",
    action: str = "reduce_speed",
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Blast-radius preview: how many active subs a draft FUP rule would hit now.

    Read-only, no side effects — mirrors the change-plan-quote preview pattern.
    """
    result = web_fup_service.preview_rule_impact(
        db,
        offer_id,
        threshold_amount=threshold_amount,
        threshold_unit=threshold_unit,
        direction=direction,
        consumption_period=consumption_period,
        action=action,
    )
    return JSONResponse(result, status_code=400 if result.get("error") else 200)


# ---------------------------------------------------------------------------
# Tariff Plan Usage Graph
# ---------------------------------------------------------------------------


@router.get(
    "/offers/{offer_id}/usage-graph",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_usage_graph_modal(
    offer_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Render the usage graph modal partial for HTMX."""
    context: dict[str, Any] = {"request": request}
    context.update(
        web_catalog_offers_service.offer_usage_graph_modal_context(
            db,
            offer_id,
            period=request.query_params.get("period", "monthly"),
        )
    )
    return templates.TemplateResponse("admin/catalog/_plan_graph_modal.html", context)


@router.get(
    "/offers/{offer_id}/usage-graph/data",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def offer_usage_graph_data(
    offer_id: str,
    period: str = "monthly",
    months: int = 12,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return JSON data for the plan usage graph."""
    if period not in {"daily", "weekly", "monthly", "quarterly", "annual"}:
        period = "monthly"
    data = web_catalog_offers_service.plan_usage_graph_data(
        db, offer_id, period=period, months=months
    )
    return JSONResponse(data)


@router.get("/subscriptions", response_class=HTMLResponse)
def catalog_subscriptions(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = web_catalog_subscriptions_service.subscriptions_list_page_data(
        db, status=status, page=page, per_page=per_page
    )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/subscriptions.html", context)


@router.get("/subscriptions/new", response_class=HTMLResponse)
def catalog_subscription_new(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    account_id = request.query_params.get("account_id", "").strip()
    subscriber_id = request.query_params.get("subscriber_id", "").strip()
    subscription = web_catalog_subscriptions_service.default_subscription_form(
        account_id, subscriber_id
    )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(
        web_catalog_subscriptions_service.subscription_form_context(db, subscription)
    )
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get("/subscription-ipv4-addresses", response_class=JSONResponse)
def catalog_subscription_ipv4_addresses(
    selector: str = Query(...),
    q: str = "",
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> JSONResponse:
    try:
        payload = (
            web_catalog_subscriptions_service.available_ipv4_addresses_for_selector(
                db,
                selector=selector,
                query=q,
                limit=limit,
            )
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc), "addresses": []}, status_code=400)
    return JSONResponse(payload)


@router.post(
    "/subscriptions",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_subscription_workflows_service.handle_subscription_create_form(
        db,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.get(
    "/subscriptions/ipam/ipv4-available",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def catalog_subscription_ipv4_available(
    selector: str = Query(...),
    current_ip: str | None = None,
    db: Session = Depends(get_db),
) -> JSONResponse:
    try:
        available_ips = (
            web_catalog_subscriptions_service.available_ipv4_options_for_selector(
                db,
                selector=selector,
                current_ip=current_ip,
            )
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"available_ips": available_ips})


@router.get(
    "/subscriptions/ipam/route-children",
    dependencies=[Depends(require_permission(ADDITIONAL_IP_WRITE_PERMISSION))],
)
def catalog_subscription_route_children(
    parent_cidr: str = Query(...),
    prefix: int = Query(...),
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
) -> JSONResponse:
    try:
        children = web_catalog_subscriptions_service.route_child_options_for_parent(
            db,
            parent_cidr=parent_cidr,
            prefix=prefix,
            current_subscriber_id=subscriber_id,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"children": children})


@router.get(
    "/subscriptions/service-access/available-ipv4",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_service_access_available_ipv4(
    target_nas_device_id: UUID = Query(...),
    target_pool_id: UUID = Query(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    try:
        addresses = web_catalog_subscription_workflows_service.service_access_move_available_ipv4(
            db,
            target_nas_device_id=target_nas_device_id,
            target_pool_id=target_pool_id,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"addresses": addresses})


@router.get("/subscriptions/{subscription_id}/edit", response_class=HTMLResponse)
def catalog_subscription_edit(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(
        require_any_permission("catalog:read", ADDITIONAL_IP_WRITE_PERMISSION)
    ),
) -> HTMLResponse:
    form_context = (
        web_catalog_subscription_workflows_service.subscription_edit_form_context(
            db, subscription_id
        )
    )
    if form_context is None:
        context = _base_context(request, db, active_page="catalog-subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(form_context)
    context["can_manage_subscription_catalog"] = has_permission(
        auth, db, "catalog:write"
    )
    context["can_manage_additional_ip"] = has_permission(
        auth, db, ADDITIONAL_IP_WRITE_PERMISSION
    )
    context["notice"] = request.query_params.get("notice")
    context["error"] = request.query_params.get("error") or context.get("error")
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post(
    "/subscriptions/{subscription_id}/additional-ip",
    dependencies=[Depends(require_permission(ADDITIONAL_IP_WRITE_PERMISSION))],
)
def catalog_subscription_additional_ip_update(
    request: Request,
    subscription_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    error = web_catalog_subscription_workflows_service.handle_subscription_additional_ip_form(
        db,
        subscription_id=subscription_id,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    base_url = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    if error:
        return RedirectResponse(
            f"{base_url}?error={quote_plus(error)}#additional-ip", status_code=303
        )
    notice = quote_plus("Additional IP allocation updated.")
    return RedirectResponse(
        f"{base_url}?notice={notice}#additional-ip", status_code=303
    )


@router.get(
    "/subscriptions/{subscription_id}/access/move",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_service_access_move(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form_context = (
        web_catalog_subscription_workflows_service.service_access_move_form_context(
            db, subscription_id, error=request.query_params.get("error")
        )
    )
    if form_context is None:
        context = _base_context(request, db, active_page="catalog-subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/catalog/subscription_service_access_move.html", context
    )


@router.post(
    "/subscriptions/{subscription_id}/access/move/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_service_access_move_preview(
    request: Request,
    subscription_id: str,
    target_nas_device_id: UUID = Form(...),
    target_pool_id: UUID = Form(...),
    target_ipv4: str = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    error: str | None = None
    preview = None
    try:
        if not reason.strip():
            raise ValueError("An operational reason is required.")
        preview = web_catalog_subscription_workflows_service.preview_subscription_service_access_move_form(
            db,
            subscription_id=UUID(subscription_id),
            target_nas_device_id=target_nas_device_id,
            target_pool_id=target_pool_id,
            target_ipv4=target_ipv4,
        )
    except (DomainError, ValueError) as exc:
        error = str(getattr(exc, "message", None) or str(exc))
    form_context = (
        web_catalog_subscription_workflows_service.service_access_move_form_context(
            db,
            subscription_id,
            preview=preview,
            reason=reason,
            error=error,
        )
    )
    if form_context is None:
        context = _base_context(request, db, active_page="catalog-subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/catalog/subscription_service_access_move.html", context
    )


@router.post(
    "/subscriptions/{subscription_id}/access/move/confirm",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_service_access_move_confirm(
    request: Request,
    subscription_id: str,
    target_nas_device_id: UUID = Form(...),
    target_pool_id: UUID = Form(...),
    target_ipv4: str = Form(...),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    reason: str = Form(...),
    confirmed: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    base_url = f"/admin/catalog/subscriptions/{subscription_id}/access/move"
    if confirmed != "yes":
        message = "Confirm the exact reviewed router and IPv4 move before continuing."
        return RedirectResponse(
            f"{base_url}?error={quote_plus(message)}", status_code=303
        )
    try:
        confirmation = web_catalog_subscription_workflows_service.SubscriptionServiceAccessMoveConfirmation(
            subscription_id=UUID(subscription_id),
            target_nas_device_id=target_nas_device_id,
            target_pool_id=target_pool_id,
            target_ipv4=target_ipv4,
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
            actor_id=str(_get_actor_id(request) or ""),
            reason=reason,
        )
        outcome = web_catalog_subscription_workflows_service.execute_subscription_service_access_move(
            db, confirmation=confirmation
        )
    except (DomainError, ValueError) as exc:
        message = str(getattr(exc, "message", None) or str(exc))
        return RedirectResponse(
            f"{base_url}?error={quote_plus(message)}", status_code=303
        )
    message = (
        f"Service access moved to {outcome.target_ipv4}; billing unchanged. "
        "RADIUS and old-address session convergence was requested."
    )
    edit_url = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return RedirectResponse(f"{edit_url}?notice={quote_plus(message)}", status_code=303)


@router.get("/subscriptions/{subscription_id}", response_class=HTMLResponse)
def catalog_subscription_detail(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
    notice: str | None = None,
    warning: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    detail_context = (
        web_catalog_subscription_workflows_service.subscription_detail_page_context(
            db, subscription_id
        )
    )
    if detail_context is None:
        context = _base_context(request, db, active_page="catalog-subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(detail_context)
    auth = getattr(request.state, "auth", None)
    context["can_correct_subscription"] = bool(
        auth and has_permission(auth, db, "catalog:write")
    )
    context["can_change_ont"] = bool(
        auth and has_permission(auth, db, "network:ont:write")
    )
    context["notice"] = notice
    context["warning"] = warning
    context["error"] = error
    subscription_obj = context.get("subscription")
    network_path = (
        resolve_customer_path(db, subscription_obj)
        if subscription_obj is not None
        else None
    )
    context["network_path"] = network_path
    if network_path is not None:
        from app.services.topology.outage import open_incident_for_path

        context["known_outage"] = open_incident_for_path(db, network_path)
    context["registered_radios"] = (
        radio_registration_service.list_radios_for_subscriber(
            db, subscription_obj.subscriber_id
        )
        if subscription_obj is not None
        else []
    )
    return templates.TemplateResponse("admin/catalog/subscription_detail.html", context)


@router.get(
    "/subscriptions/{subscription_id}/change-ont",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def catalog_subscription_change_ont_form(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    form_context = web_network_ont_assignments_service.change_ont_form_context(
        db,
        subscription_id=subscription_id,
    )
    if form_context.not_found:
        context = _base_context(request, db, active_page="catalog-subscriptions")
        context.update({"message": "Subscription not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update({"change_ont": form_context})
    return templates.TemplateResponse(
        "admin/catalog/subscription_change_ont.html",
        context,
    )


@router.post(
    "/subscriptions/{subscription_id}/change-ont",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def catalog_subscription_change_ont_submit(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
) -> Response:
    form = parse_form_data_sync(request)
    success, message = (
        web_network_ont_assignments_service.reassign_subscription_ont_from_form(
            db,
            subscription_id=subscription_id,
            form=form,
            actor_id=_get_actor_id(request),
        )
    )
    param = "notice" if success else "error"
    return RedirectResponse(
        f"/admin/catalog/subscriptions/{subscription_id}?{param}={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/subscriptions/{subscription_id}/bill-now/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def catalog_subscription_bill_now_preview(
    request: Request, subscription_id: str, db: Session = Depends(get_db)
) -> Response:
    try:
        preview_context = (
            web_catalog_subscription_workflows_service.prepaid_bill_now_preview_context(
                db, subscription_id=subscription_id
            )
        )
    except DomainError as exc:
        existing_invoice_id = exc.details.get("invoice_id")
        if existing_invoice_id is not None:
            return RedirectResponse(
                f"/admin/billing/invoices/{existing_invoice_id}"
                f"?error={quote_plus(exc.message)}#prepaid-reconciliation",
                status_code=303,
            )
        return RedirectResponse(
            f"/admin/catalog/subscriptions/{subscription_id}?error={quote_plus(exc.message)}",
            status_code=303,
        )
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(preview_context)
    context["subscription_id"] = subscription_id
    return templates.TemplateResponse(
        "admin/catalog/prepaid_bill_now_confirm.html", context
    )


@router.post(
    "/subscriptions/{subscription_id}/bill-now/confirm",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def catalog_subscription_bill_now_confirm(
    request: Request,
    subscription_id: str,
    preview_fingerprint: str = Form(...),
    preview_starts_at: datetime = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        invoice_url = (
            web_catalog_subscription_workflows_service.confirm_prepaid_bill_now(
                db,
                subscription_id=subscription_id,
                fingerprint=preview_fingerprint,
                starts_at=preview_starts_at,
                actor_id=_get_actor_id(request),
            )
        )
    except DomainError as exc:
        existing_invoice_id = exc.details.get("invoice_id")
        if existing_invoice_id is not None:
            return RedirectResponse(
                f"/admin/billing/invoices/{existing_invoice_id}"
                f"?error={quote_plus(exc.message)}#prepaid-reconciliation",
                status_code=303,
            )
        return RedirectResponse(
            f"/admin/catalog/subscriptions/{subscription_id}?error={quote_plus(exc.message)}",
            status_code=303,
        )
    return RedirectResponse(invoice_url, status_code=303)


@router.post(
    "/subscriptions/{subscription_id}/register-radio-mac",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_register_radio_mac(
    request: Request,
    subscription_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Record the customer radio MAC at install time (capture at source)."""
    base_url = f"/admin/catalog/subscriptions/{subscription_id}"
    mac = str(form.get("mac_address") or "")
    try:
        result = radio_registration_service.register_radio_mac(
            db,
            subscription_id=subscription_id,
            mac=mac,
            actor_id=_get_actor_id(request),
            source=radio_registration_service.SOURCE_ADMIN,
            request=request,
        )
    except LookupError:
        return RedirectResponse(
            f"{base_url}?error={quote_plus('Subscription not found.')}",
            status_code=303,
        )
    except radio_registration_service.InvalidMacError as exc:
        return RedirectResponse(
            f"{base_url}?error={quote_plus(str(exc))}", status_code=303
        )
    except radio_registration_service.MacConflictError as exc:
        return RedirectResponse(
            f"{base_url}?error={quote_plus(str(exc))}", status_code=303
        )
    mac_display = result.device.mac_address
    if result.created:
        message = f"Radio MAC {mac_display} registered."
    else:
        message = f"Radio MAC {mac_display} was already registered."
    if result.warnings:
        message = f"{message} {' '.join(result.warnings)}"
        return RedirectResponse(
            f"{base_url}?warning={quote_plus(message)}", status_code=303
        )
    return RedirectResponse(f"{base_url}?notice={quote_plus(message)}", status_code=303)


@router.post(
    "/subscriptions/{subscription_id}/force-reauth",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_force_reauth(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        result = web_catalog_subscriptions_service.force_subscription_reauth(
            db,
            subscription_id,
            request,
            actor_id=_get_actor_id(request),
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        url = (
            f"/admin/catalog/subscriptions/{subscription_id}"
            f"?error={quote_plus(str(detail))}"
        )
        return RedirectResponse(
            url,
            status_code=303,
        )
    count_raw = result.get("sessions_disconnected", 0)
    if isinstance(count_raw, int):
        count = count_raw
    elif isinstance(count_raw, str):
        count = int(count_raw or "0")
    else:
        count = 0
    if count > 0:
        message = (
            f"Force reauthentication requested. Disconnected {count} active session(s)."
        )
        url = (
            f"/admin/catalog/subscriptions/{subscription_id}"
            f"?notice={quote_plus(message)}"
        )
        return RedirectResponse(
            url,
            status_code=303,
        )
    message = (
        "Force reauthentication requested, but no active PPPoE session was "
        "found to disconnect."
    )
    url = (
        f"/admin/catalog/subscriptions/{subscription_id}?warning={quote_plus(message)}"
    )
    return RedirectResponse(
        url,
        status_code=303,
    )


@router.post(
    "/subscriptions/{subscription_id}/ipv4/replace",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_replace_ipv4(
    request: Request,
    subscription_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    redirect_url = (
        web_catalog_subscription_workflows_service.handle_subscription_ipv4_replacement(
            db,
            subscription_id=subscription_id,
            form=form,
            actor_id=_get_actor_id(request),
        )
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.post(
    "/subscriptions/{subscription_id}/ipv4/reconcile",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_reconcile_ipv4(
    request: Request,
    subscription_id: str,
    assignment_id: str = Form(...),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    confirmed: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Apply one confirmed, owner-reviewed served-IPv4 reconciliation."""

    base_url = f"/admin/catalog/subscriptions/{subscription_id}"
    if confirmed != "yes":
        message = "Confirm the reviewed IPv4 reconciliation before continuing."
        return RedirectResponse(
            f"{base_url}?error={quote_plus(message)}#ipv4-projection-reconciliation",
            status_code=303,
        )
    actor_id = str(_get_actor_id(request) or "").strip()
    try:
        command = web_catalog_subscription_workflows_service.SubscriptionIPv4ProjectionReconciliationCommand(
            subscription_id=UUID(subscription_id),
            assignment_id=UUID(assignment_id),
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
        )
        outcome = web_catalog_subscription_workflows_service.execute_subscription_ipv4_projection_reconciliation(
            db,
            command=command,
        )
    except (DomainError, ValueError) as exc:
        message = str(getattr(exc, "message", None) or str(exc))
        return RedirectResponse(
            f"{base_url}?error={quote_plus(message)}#ipv4-projection-reconciliation",
            status_code=303,
        )
    message = (
        f"Served IPv4 reconciliation committed for {outcome.desired_address}; "
        "RADIUS and old-address session convergence requested."
    )
    return RedirectResponse(
        f"{base_url}?notice={quote_plus(message)}#ipv4-projection-reconciliation",
        status_code=303,
    )


@router.post(
    "/subscriptions/{subscription_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_update(
    request: Request,
    subscription_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    result = web_catalog_subscription_workflows_service.handle_subscription_update_form(
        db,
        subscription_id=subscription_id,
        form=form,
        request=request,
        actor_id=_get_actor_id(request),
    )
    if result.get("redirect_url"):
        return RedirectResponse(str(result["redirect_url"]), status_code=303)
    context = _base_context(request, db, active_page="catalog-subscriptions")
    context.update(cast(dict[str, Any], result["form_context"]))
    return templates.TemplateResponse("admin/catalog/subscription_form.html", context)


@router.post(
    "/subscriptions/{subscription_id}/lifecycle/preview",
    dependencies=[
        Depends(
            require_any_permission(
                "catalog:read",
                "catalog:write",
                "subscription:activate",
                "subscription:suspend",
            )
        )
    ],
)
def catalog_subscription_preview_lifecycle_command(
    request: Request,
    subscription_id: str,
    kind: SubscriptionCommandKind = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    target_offer_id: str | None = Form(None),
    reason: str | None = Form(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Preview one lifecycle command without mutating subscription state."""
    _assert_lifecycle_preview_permission(request, db, kind)
    payload, status_code = (
        web_catalog_subscription_workflows_service.preview_lifecycle_command_response(
            db,
            subscription_id=subscription_id,
            kind=kind,
            actor_id=_get_actor_id(request),
            reason=reason,
            target_offer_id=target_offer_id,
            effective_timing=effective_timing,
            effective_at=effective_at,
        )
    )
    return JSONResponse(payload, status_code=status_code)


@router.post(
    "/subscriptions/{subscription_id}/correction/execute",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_execute_correction(
    request: Request,
    subscription_id: str,
    target_subscription_id: str = Form(...),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    confirmed: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Apply one server-reviewed mistaken-subscription correction."""
    if confirmed != "yes":
        message = "Confirm the reviewed subscription correction before continuing."
        return RedirectResponse(
            f"/admin/catalog/subscriptions/{subscription_id}"
            f"?error={quote_plus(message)}#subscription-correction",
            status_code=303,
        )
    payload, status_code = (
        web_catalog_subscription_workflows_service.execute_subscription_correction_response(
            db,
            active_subscription_id=subscription_id,
            target_subscription_id=target_subscription_id,
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
            actor_id=_get_actor_id(request),
        )
    )
    if status_code != 200:
        return RedirectResponse(
            f"/admin/catalog/subscriptions/{subscription_id}"
            f"?error={quote_plus(str(payload['message']))}#subscription-correction",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/catalog/subscriptions/{payload['target_subscription_id']}"
        f"?notice={quote_plus(str(payload['message']))}",
        status_code=303,
    )


@router.post(
    "/subscriptions/{subscription_id}/lifecycle/execute",
    dependencies=[
        Depends(
            require_any_permission(
                "catalog:write",
                "subscription:activate",
                "subscription:suspend",
            )
        )
    ],
)
def catalog_subscription_execute_lifecycle_command(
    request: Request,
    subscription_id: str,
    kind: SubscriptionCommandKind = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    target_offer_id: str | None = Form(None),
    reason: str | None = Form(None),
    expected_head: str = Form(...),
    preview_fingerprint: str | None = Form(None),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Execute one canonical, reviewed subscription lifecycle command."""
    _assert_lifecycle_command_permission(request, db, kind)
    payload, status_code = (
        web_catalog_subscription_workflows_service.execute_lifecycle_command_response(
            db,
            subscription_id=subscription_id,
            kind=kind,
            actor_id=_get_actor_id(request),
            expected_head=expected_head,
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
            reason=reason,
            target_offer_id=target_offer_id,
            effective_timing=effective_timing,
            effective_at=effective_at,
        )
    )
    return JSONResponse(payload, status_code=status_code)


@router.post(
    "/subscriptions/{subscription_id}/lifecycle/schedules/{schedule_id}/cancel",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_cancel_lifecycle_schedule(
    request: Request,
    subscription_id: str,
    schedule_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Cancel one pending deferred lifecycle status command."""
    payload, status_code = (
        web_catalog_subscription_workflows_service.cancel_lifecycle_schedule_response(
            db,
            subscription_id=subscription_id,
            schedule_id=schedule_id,
            actor_id=_get_actor_id(request),
        )
    )
    return JSONResponse(payload, status_code=status_code)


@router.post(
    "/subscriptions/{subscription_id}/lifecycle/schedules/{schedule_id}/cancel-view",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_cancel_lifecycle_schedule_view(
    request: Request,
    subscription_id: str,
    schedule_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Cancel a deferred lifecycle command from the subscription detail page."""
    return RedirectResponse(
        web_catalog_subscription_workflows_service.cancel_lifecycle_schedule_redirect(
            db,
            subscription_id=subscription_id,
            schedule_id=schedule_id,
            actor_id=_get_actor_id(request),
        ),
        status_code=303,
    )


@router.post(
    "/subscriptions/{subscription_id}/send-credentials",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_send_credentials(
    subscription_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    return RedirectResponse(
        web_catalog_subscription_workflows_service.send_subscription_credentials_redirect(
            db,
            subscription_id=subscription_id,
        ),
        status_code=303,
    )


@router.post(
    "/subscriptions/{subscription_id}/resume-vacation-hold",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def catalog_subscription_resume_vacation_hold(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Admin action to resume a customer vacation hold."""
    return RedirectResponse(
        web_catalog_subscription_workflows_service.admin_resume_vacation_hold_redirect(
            db,
            subscription_id=subscription_id,
            actor_id=_get_actor_id(request),
        ),
        status_code=303,
    )


@router.post(
    "/subscriptions/bulk/lifecycle/preview",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def subscription_bulk_lifecycle_preview(
    request: Request,
    subscription_ids: str = Form(...),
    kind: SubscriptionCommandKind = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    target_offer_id: str | None = Form(None),
    reason: str | None = Form(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Preview a subscription lifecycle batch without mutating any item."""
    payload, status_code = (
        web_catalog_subscription_workflows_service.preview_bulk_lifecycle_response(
            db,
            subscription_ids=subscription_ids,
            kind=kind,
            actor_id=_get_actor_id(request),
            effective_timing=effective_timing,
            effective_at=effective_at,
            target_offer_id=target_offer_id,
            reason=reason,
        )
    )
    return JSONResponse(payload, status_code=status_code)


@router.post(
    "/subscriptions/bulk/activate",
    dependencies=[
        Depends(require_any_permission("catalog:write", "subscription:activate"))
    ],
)
def subscription_bulk_activate(
    request: Request,
    subscription_ids: str = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    reason: str | None = Form(None),
    reviewed_heads: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk activate subscriptions."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_activate_response(
            db,
            subscription_ids=subscription_ids,
            request=request,
            actor_id=_get_actor_id(request),
            effective_timing=effective_timing,
            effective_at=effective_at,
            reason=reason,
            reviewed_heads=reviewed_heads,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/subscriptions/bulk/restore",
    dependencies=[
        Depends(require_any_permission("catalog:write", "subscription:activate"))
    ],
)
def subscription_bulk_restore(
    request: Request,
    subscription_ids: str = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    reason: str | None = Form(None),
    reviewed_heads: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk restore subscriptions through canonical lifecycle commands."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_restore_response(
            db,
            subscription_ids=subscription_ids,
            actor_id=_get_actor_id(request),
            effective_timing=effective_timing,
            effective_at=effective_at,
            reason=reason,
            reviewed_heads=reviewed_heads,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/subscriptions/bulk/suspend",
    dependencies=[
        Depends(require_any_permission("catalog:write", "subscription:suspend"))
    ],
)
def subscription_bulk_suspend(
    request: Request,
    subscription_ids: str = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    reason: str | None = Form(None),
    reviewed_heads: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk suspend subscriptions."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_suspend_response(
            db,
            subscription_ids=subscription_ids,
            request=request,
            actor_id=_get_actor_id(request),
            effective_timing=effective_timing,
            effective_at=effective_at,
            reason=reason,
            reviewed_heads=reviewed_heads,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/subscriptions/bulk/cancel",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def subscription_bulk_cancel(
    request: Request,
    subscription_ids: str = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    reason: str | None = Form(None),
    reviewed_heads: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk cancel subscriptions."""
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_cancel_response(
            db,
            subscription_ids=subscription_ids,
            request=request,
            actor_id=_get_actor_id(request),
            effective_timing=effective_timing,
            effective_at=effective_at,
            reason=reason,
            reviewed_heads=reviewed_heads,
            idempotency_key=idempotency_key,
        )
    )


@router.get(
    "/subscriptions/{subscription_id}/change-plan-quote",
    dependencies=[Depends(require_permission("catalog:read"))],
)
def subscription_change_plan_quote(
    subscription_id: str,
    target_offer_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Proration preview for the change-plan modal (no side effects)."""
    try:
        payload = web_catalog_subscription_workflows_service.change_plan_quote_response(
            db,
            subscription_id=subscription_id,
            target_offer_id=target_offer_id,
        )
    except PrepaidFundingBaselineMissingError:
        return JSONResponse(
            {
                "status": "unavailable",
                "message": (
                    web_catalog_subscription_workflows_service.SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE
                ),
                "error_code": "financial_position_unavailable",
            },
            status_code=409,
        )
    return JSONResponse(payload)


@router.post(
    "/subscriptions/bulk/change-plan",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def subscription_bulk_change_plan(
    request: Request,
    subscription_ids: str = Form(...),
    target_offer_id: str = Form(...),
    effective_timing: SubscriptionEffectiveTiming = Form(
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = Form(None),
    reason: str | None = Form(None),
    reviewed_heads: str | None = Form(None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bulk change plan/offer for subscriptions.

    ``effective_timing`` follows the canonical lifecycle timing contract.
    """
    return JSONResponse(
        web_catalog_subscription_workflows_service.bulk_change_plan_response(
            db,
            subscription_ids=subscription_ids,
            target_offer_id=target_offer_id,
            request=request,
            actor_id=_get_actor_id(request),
            effective_timing=effective_timing,
            effective_at=effective_at,
            reason=reason,
            reviewed_heads=reviewed_heads,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/subscriptions/{subscription_id}/scheduled-change/{request_id}/cancel",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def subscription_cancel_scheduled_change(
    request: Request,
    subscription_id: str,
    request_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Cancel a scheduled next-cycle plan change before it takes effect."""
    return RedirectResponse(
        web_catalog_subscription_workflows_service.cancel_scheduled_plan_change_redirect(
            db,
            subscription_id=subscription_id,
            request_id=request_id,
            actor_id=_get_actor_id(request),
        ),
        status_code=303,
    )


@router.get("/calculator", response_class=HTMLResponse)
def pricing_calculator(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Pricing calculator tool to test and validate offers."""
    page_data = web_catalog_calculator_service.calculator_page_data(db)
    context = _base_context(request, db, active_page="catalog-calculator")
    context.update(page_data)
    return templates.TemplateResponse("admin/catalog/calculator.html", context)


# ---------------------------------------------------------------------------
# Bulk Tariff Change routes
# ---------------------------------------------------------------------------


@router.get(
    "/bulk-tariff-change",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_tariff_change_page(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Bulk tariff change wizard."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_bulk_tariff_change_service.page_context(request, db))
    return templates.TemplateResponse("admin/catalog/bulk_tariff_change.html", context)


@router.post(
    "/bulk-tariff-change/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_tariff_change_preview(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Preview bulk tariff change results."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_bulk_tariff_change_service.preview_context(request, db, form))
    return templates.TemplateResponse("admin/catalog/bulk_tariff_change.html", context)


@router.post(
    "/bulk-tariff-change/execute",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_tariff_change_execute(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute the bulk tariff change."""
    context = _base_context(request, db, active_page="catalog")
    context.update(web_bulk_tariff_change_service.execute_context(request, db, form))
    return templates.TemplateResponse("admin/catalog/bulk_tariff_change.html", context)
