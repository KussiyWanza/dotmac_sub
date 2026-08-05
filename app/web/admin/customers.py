"""Admin customer (person & business) management web routes."""

import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import quote_plus

import anyio
from fastapi import (
    APIRouter,
    Body,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import Reseller, SubscriberCategory
from app.services import customer_network_path as customer_network_path_service
from app.services import customer_portal
from app.services import network_monitoring as network_monitoring_service
from app.services import subscriber as subscriber_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services import (
    web_catalog_subscription_workflows as web_catalog_subscription_workflows_service,
)
from app.services import web_customer_actions as web_customer_actions_service
from app.services import (
    web_customer_availability as web_customer_availability_service,
)
from app.services import (
    web_customer_bulk_actions as web_customer_bulk_actions_service,
)
from app.services import web_customer_details as web_customer_details_service
from app.services import web_customer_lists as web_customer_lists_service
from app.services import web_customer_user_access as web_customer_user_access_service
from app.services import web_notifications as web_notifications_service
from app.services.audit_helpers import (
    build_changes_metadata,
    log_audit_event,
)
from app.services.auth_dependencies import (
    has_permission,
    require_any_permission,
    require_permission,
)
from app.services.bandwidth import bandwidth_samples
from app.services.customer_portal_context import resolve_customer_subscription
from app.services.customer_timeline import CustomerTimelineItem
from app.services.domain_errors import DomainError
from app.services.queue_adapter import enqueue_task
from app.services.subscription_change_execution import (
    RemoteProvisionActionCommand,
    RemoteProvisionActionStatus,
    SubscriptionChangeExecutionError,
    provision_and_verify_remote_change,
)
from app.web.customer.branding import register_customer_portal_filters
from app.web.request_parsing import parse_json_body

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
register_customer_portal_filters(templates)
router = APIRouter(prefix="/customers", tags=["web-admin-customers"])

_NOTIFICATION_QUEUE_TASK = "app.tasks.notifications.deliver_notification_queue"


def _reseller_form_context(
    db: Session, reseller_id: object | None
) -> dict[str, object]:
    reseller = db.get(Reseller, reseller_id) if reseller_id else None
    managed = bool(reseller and not reseller.is_house)
    return {
        "managed_by_reseller": managed,
        "selected_reseller_id": str(reseller.id) if managed and reseller else "",
        "selected_reseller_label": (
            f"{reseller.name} ({reseller.contact_email})"
            if managed and reseller and reseller.contact_email
            else (reseller.name if managed and reseller else "")
        ),
    }


def _safe_form_error(exc: Exception) -> str:
    """Message safe to render back into a form.

    User-facing validation errors (``ValueError``) are shown verbatim; any other
    exception (DB constraint, integrity error, etc.) is logged with a full
    traceback but shown generically so SQL/schema/internals never leak to the UI.
    Call only from inside an ``except`` block (uses the active exception for the
    traceback log).
    """
    if isinstance(exc, ValueError):
        return str(exc)
    logger.exception("Admin customer form action failed")
    return "Something went wrong. Please try again or contact support if it persists."


def _kick_notification_delivery(result: dict[str, object]) -> dict[str, object]:
    """Ask Celery to drain the notification queue after an operator bulk send.

    Celery beat normally runs this task every minute. Triggering it here makes
    the customer-list send action behave immediately even if beat is down or has
    not picked up the schedule yet.
    """
    queued_count = result.get("queued_count")
    if not isinstance(queued_count, int) or queued_count <= 0:
        return result

    dispatch = enqueue_task(
        _NOTIFICATION_QUEUE_TASK,
        queue="celery",
        source="admin_customers_bulk_send",
    )
    result["delivery_dispatch"] = {
        "queued": dispatch.queued,
        "task_id": dispatch.task_id,
        "error": dispatch.error,
    }
    if not dispatch.queued:
        logger.warning(
            "Failed to dispatch notification queue after customer bulk send: %s",
            dispatch.error,
        )
    return result


contacts_router = APIRouter(prefix="/contacts", tags=["web-admin-contacts"])
_ALLOWED_USAGE_PERIODS = {"current", "last"}


def _htmx_error_response(
    message: str,
    status_code: int = 409,
    title: str = "Delete blocked",
    reswap: str | None = None,
) -> Response:
    trigger = {
        "showToast": {
            "type": "error",
            "title": title,
            "message": message,
        }
    }
    headers = {"HX-Trigger": json.dumps(trigger)}
    if reswap:
        headers["HX-Reswap"] = reswap
    return Response(status_code=status_code, headers=headers)


def _get_subscriber(db: Session, subscriber_id: str):
    return subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)


def _resolve_business_customer_id(db: Session, customer_id: str) -> str:
    return web_customer_actions_service.resolve_business_customer_id(db, customer_id)


def _get_actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    return str(current_user.get("subscriber_id")) if current_user else None


def _subscription_action_permission_context(
    request: Request, db: Session
) -> dict[str, bool]:
    auth = getattr(getattr(request, "state", None), "auth", None) or {}
    can_write_catalog = bool(auth) and has_permission(auth, db, "catalog:write")
    return {
        "can_activate_subscriptions": can_write_catalog
        or (bool(auth) and has_permission(auth, db, "subscription:activate")),
        "can_suspend_subscriptions": can_write_catalog
        or (bool(auth) and has_permission(auth, db, "subscription:suspend")),
        "can_reconcile_service_changes": bool(auth)
        and has_permission(auth, db, "provisioning:service_change_reconcile"),
    }


def _workflow_changed_count(result: Mapping[str, Any]) -> int:
    value = result.get("changed") or result.get("count") or 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _load_tax_rates(db: Session):
    return web_billing_invoices_service.load_tax_rates(db)


def _billing_form_defaults(db: Session, customer_type: str, customer) -> dict[str, str]:
    values = web_customer_actions_service.billing_form_defaults(customer)
    if customer is not None:
        from app.services import customer_tax_policies
        from app.services.collections.grace_policy import (
            resolve_effective_grace_policy,
        )

        grace = resolve_effective_grace_policy(db, customer)
        wht_policy = customer_tax_policies.get_customer_withholding_tax_policy(
            db,
            account_id=customer.id,
        )
        values["effective_grace_days"] = str(grace.days)
        values["effective_grace_source"] = grace.source.replace("_", " ")
        values["withholding_tax_enabled"] = (
            "true" if wht_policy.withholding_tax_enabled else "false"
        )
        vat_policy = customer_tax_policies.get_customer_vat_exemption_policy(
            db,
            account_id=customer.id,
        )
        values["vat_exempt"] = "true" if vat_policy.vat_exempt else "false"
    return values


def _customer_audit_items(
    db: Session, customer, limit: int = 5
) -> list[CustomerTimelineItem]:
    if not customer:
        return []
    try:
        return web_customer_details_service.get_customer_audit_activity_items(
            db,
            str(customer.id),
            limit=limit,
        )
    except Exception:
        logger.exception("Unable to load customer audit items for %s", customer.id)
        db.rollback()
        return []


def _normalize_usage_period(value: str | None) -> str:
    normalized = str(value or "").strip().lower().rstrip(".,;:!?")
    if normalized in _ALLOWED_USAGE_PERIODS:
        return normalized
    return "current"


def _normalize_usage_view(value: object) -> Literal["chart", "table"]:
    """Resolve the records view when route handlers are called outside FastAPI."""
    if value == "table":
        return "table"
    return "chart"


def _format_bps(value: float | int | None) -> str:
    amount = float(value or 0)
    if amount <= 0:
        return "0 bps"
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    unit_index = 0
    while amount >= 1000 and unit_index < len(units) - 1:
        amount /= 1000
        unit_index += 1
    precision = 0 if unit_index == 0 else (2 if amount < 10 else 1)
    return f"{amount:.{precision}f} {units[unit_index]}"


def _load_initial_bandwidth_stats(
    db: Session, subscription_id: str | uuid.UUID | None
) -> dict[str, Any] | None:
    if not subscription_id:
        return None
    try:
        stats = anyio.from_thread.run(
            bandwidth_samples.get_bandwidth_stats,
            db,
            subscription_id,
            "24h",
        )
    except Exception:
        logger.debug(
            "admin_customer_initial_bandwidth_stats_failed",
            extra={
                "event": "admin_customer_initial_bandwidth_stats_failed",
                "subscription_id": str(subscription_id),
            },
            exc_info=True,
        )
        return None

    return {
        **stats,
        "current_rx_formatted": _format_bps(stats.get("current_rx_bps")),
        "current_tx_formatted": _format_bps(stats.get("current_tx_bps")),
        "peak_rx_formatted": _format_bps(stats.get("peak_rx_bps")),
        "peak_tx_formatted": _format_bps(stats.get("peak_tx_bps")),
    }


def _toast_response(
    *,
    request: Request,
    redirect_url: str,
    ok: bool,
    title: str,
    message: str,
) -> Response:
    trigger = {
        "showToast": {
            "type": "success" if ok else "error",
            "title": title,
            "message": message,
            "duration": 8000,
        }
    }
    if request.headers.get("HX-Request"):
        headers = {"HX-Trigger": json.dumps(trigger), "HX-Refresh": "true"}
        return Response(status_code=204, headers=headers)
    return RedirectResponse(url=redirect_url, status_code=303)


@contacts_router.post(
    "/{person_id}/convert",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def contacts_convert_to_subscriber(
    request: Request,
    person_id: uuid.UUID,
    subscriber_type: str | None = Form("person"),
    account_status: str | None = Form("active"),
    db: Session = Depends(get_db),
):
    """Convert a person contact into an active subscriber."""
    person, missing_email = web_customer_actions_service.convert_contact_to_subscriber(
        db=db,
        person_id=person_id,
        account_status=account_status,
    )
    # Log unsupported subscriber_type without changing behavior.
    if subscriber_type and subscriber_type != "person":
        logger.info(
            "Unsupported subscriber_type",
            extra={"subscriber_type": subscriber_type, "person_id": str(person.id)},
        )
    redirect_url = f"/admin/customers/person/{person.id}"
    if missing_email:
        redirect_url = f"{redirect_url}?missing_email=1"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def customers_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    customer_type: str | None = None,  # 'person' or 'business'
    nas_id: str | None = None,
    pop_site_id: str | None = None,
    sort: Literal["created_at", "name", "status"] = Query("created_at"),
    direction: Literal["asc", "desc"] = Query("desc", alias="dir"),
    page: int = Query(1, ge=1),
    per_page: str | None = Query("25"),
    db: Session = Depends(get_db),
):
    """List all customers with search and filtering."""
    try:
        list_query = web_customer_lists_service.build_customer_list_query(
            search=search,
            status=status,
            customer_type=customer_type,
            nas_id=nas_id,
            pop_site_id=pop_site_id,
            sort_by=sort,
            sort_dir=direction,
            page=page,
            per_page=per_page,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    page_data = web_customer_lists_service.build_customers_index_context(
        db=db,
        list_query=list_query,
    )
    subscription_action_permissions = _subscription_action_permission_context(
        request, db
    )
    customer_bulk_action_contract = (
        web_customer_bulk_actions_service.build_customer_bulk_action_contract(
            db,
            auth=getattr(request.state, "auth", None) or {},
        )
    )

    effective_query = page_data["list_query"]
    page_was_clamped = effective_query.page != page

    # Check if this is an HTMX request for table body only
    if request.headers.get("HX-Request"):
        response = templates.TemplateResponse(
            "admin/customers/_table.html",
            {
                "request": request,
                **page_data,
                **subscription_action_permissions,
                "customer_bulk_action_contract": customer_bulk_action_contract,
            },
        )
        if page_was_clamped:
            response.headers["HX-Replace-Url"] = effective_query.url("/admin/customers")
        return response

    if page_was_clamped:
        return RedirectResponse(
            url=effective_query.url("/admin/customers"),
            status_code=307,
        )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/index.html",
        {
            "request": request,
            **page_data,
            **subscription_action_permissions,
            "customer_bulk_action_contract": customer_bulk_action_contract,
            **web_notifications_service.bulk_notification_setup_context(db),
            "active_page": "customers",
            "active_menu": "operations",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


# Note: /new routes must be defined BEFORE /{customer_id} to avoid path matching issues


@router.get(
    "/wizard",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_wizard_form(
    request: Request,
    db: Session = Depends(get_db),
):
    """Customer creation wizard (multi-step form)."""
    from app.services.smart_defaults import SmartDefaultsService
    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    # Get default country from settings
    defaults_service = SmartDefaultsService(db)
    customer_defaults = defaults_service.get_customer_defaults("person")

    return templates.TemplateResponse(
        "admin/customers/form_wizard.html",
        {
            "request": request,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "default_country": customer_defaults.get("country_code", "NG"),
        },
    )


@router.post("/wizard", dependencies=[Depends(require_permission("customer:write"))])
def customer_wizard_create(
    request: Request,
    data: dict = Body(...),
    db: Session = Depends(get_db),
):
    """Create a customer from wizard (JSON submission)."""
    try:
        created_type, created_id = (
            web_customer_actions_service.create_customer_from_wizard(
                db=db,
                data=data,
            )
        )
        return {
            "success": True,
            "redirect": f"/admin/customers/{created_type}/{created_id}",
        }

    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    except IntegrityError:
        db.rollback()
        return {
            "success": False,
            "message": "A customer with this information already exists.",
        }
    except Exception as exc:
        logger.error("Customer wizard error: %s", exc)
        return {
            "success": False,
            "message": "An unexpected error occurred. Please try again.",
        }


@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_new(
    request: Request,
    type: str | None = "person",
    db: Session = Depends(get_db),
):
    """New customer form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)
    pop_sites = network_monitoring_service.pop_sites.list(
        db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    return templates.TemplateResponse(
        "admin/customers/form.html",
        {
            "request": request,
            "customer": None,
            "customer_type": type,
            "action": "create",
            "pop_sites": pop_sites,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            **_reseller_form_context(db, None),
        },
    )


@router.post(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_create(
    request: Request,
    customer_type: str = Form(...),
    # Subscriber fields
    first_name: str | None = Form(None),
    last_name: str | None = Form(None),
    display_name: str | None = Form(None),
    avatar_url: str | None = Form(None),
    bio: str | None = Form(None),
    # Business fields
    name: str | None = Form(None),
    legal_name: str | None = Form(None),
    tax_id: str | None = Form(None),
    domain: str | None = Form(None),
    website: str | None = Form(None),
    org_notes: str | None = Form(None),
    # Common fields
    email: str | None = Form(None),
    email_verified: str | None = Form(None),
    phone: str | None = Form(None),
    nin: str | None = Form(None),
    date_of_birth: str | None = Form(None),
    gender: str | None = Form(None),
    preferred_contact_method: str | None = Form(None),
    locale: str | None = Form(None),
    timezone: str | None = Form(None),
    address_line1: str | None = Form(None),
    address_line2: str | None = Form(None),
    city: str | None = Form(None),
    region: str | None = Form(None),
    postal_code: str | None = Form(None),
    country_code: str | None = Form(None),
    pop_site_id: str | None = Form(None),
    status: str | None = Form(None),
    is_active: str | None = Form(None),
    marketing_opt_in: str | None = Form(None),
    notes: str | None = Form(None),
    account_start_date: str | None = Form(None),
    org_account_start_date: str | None = Form(None),
    metadata: str | None = Form(None),
    contact_first_name: list[str] = Form([]),
    contact_last_name: list[str] = Form([]),
    contact_title: list[str] = Form([]),
    contact_role: list[str] = Form([]),
    contact_email: list[str] = Form([]),
    contact_phone: list[str] = Form([]),
    contact_is_primary: list[str] = Form([]),
    managed_by_reseller: str | None = Form(None),
    reseller_id: str | None = Form(None),
    reseller_label: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new customer (person or business)."""
    try:
        contact_columns = {
            "first_name": contact_first_name,
            "last_name": contact_last_name,
            "title": contact_title,
            "role": contact_role,
            "email": contact_email,
            "phone": contact_phone,
            "is_primary": contact_is_primary,
        }
        form_data = {
            "first_name": first_name,
            "last_name": last_name,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "bio": bio,
            "name": name,
            "legal_name": legal_name,
            "tax_id": tax_id,
            "domain": domain,
            "website": website,
            "org_notes": org_notes,
            "email": email,
            "email_verified": email_verified,
            "phone": phone,
            "nin": nin,
            "date_of_birth": date_of_birth,
            "gender": gender,
            "preferred_contact_method": preferred_contact_method,
            "locale": locale,
            "timezone": timezone,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "city": city,
            "region": region,
            "postal_code": postal_code,
            "country_code": country_code,
            "pop_site_id": pop_site_id,
            "status": status,
            "is_active": is_active,
            "marketing_opt_in": marketing_opt_in,
            "notes": notes,
            "account_start_date": account_start_date,
            "org_account_start_date": org_account_start_date,
            "metadata_json": web_customer_actions_service.parse_json_object(
                metadata, "metadata"
            ),
            "managed_by_reseller": managed_by_reseller is not None,
            "reseller_id": reseller_id,
        }
        created_type, created_id = (
            web_customer_actions_service.create_customer_from_form(
                db=db,
                customer_type=customer_type,
                form_data=form_data,
                contact_columns=contact_columns,
            )
        )

        return RedirectResponse(
            url=f"/admin/customers/{created_type}/{created_id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        # Ensure failed transactions don't break error-page queries/rendering.
        db.rollback()
        # Only surface user-facing validation messages (ValueError). Unexpected
        # errors (DB constraints, etc.) are logged but shown generically so we
        # don't leak SQL/schema/internals to the admin UI.
        if isinstance(e, ValueError):
            error_message = str(e)
        else:
            logger.exception("Customer create failed (customer_type=%s)", customer_type)
            error_message = (
                "Something went wrong creating the customer. "
                "Please try again or contact support if it persists."
            )
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        contact_rows = []
        try:
            contact_rows = web_customer_actions_service.build_error_contact_rows(
                {
                    "first_name": contact_first_name,
                    "last_name": contact_last_name,
                    "title": contact_title,
                    "role": contact_role,
                    "email": contact_email,
                    "phone": contact_phone,
                    "is_primary": contact_is_primary,
                }
            )
        except Exception:
            contact_rows = []
        return templates.TemplateResponse(
            "admin/customers/form.html",
            {
                "request": request,
                "customer": None,
                "customer_type": customer_type,
                "action": "create",
                "error": error_message,
                "form": {
                    "contact_rows": contact_rows or None,
                },
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
                "managed_by_reseller": managed_by_reseller is not None,
                "selected_reseller_id": reseller_id or "",
                "selected_reseller_label": reseller_label or "",
            },
            status_code=400,
        )


@router.get(
    "/person/{customer_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def person_detail(
    request: Request,
    customer_id: str,
    usage_period: str = Query("current"),
    usage_page: int = Query(1, ge=1),
    usage_per_page: int = Query(25, ge=10, le=100),
    usage_view: str = Query("chart", pattern="^(chart|table)$"),
    db: Session = Depends(get_db),
):
    """View customer details (unified — person and org members)."""
    usage_period = _normalize_usage_period(usage_period)
    usage_view = _normalize_usage_view(usage_view)
    request_auth = getattr(getattr(request, "state", None), "auth", None) or {}
    # Same gate the inbox workspace uses, decided here and honoured by the
    # snapshot builder so unpermitted conversation data is never assembled.
    show_conversations = bool(request_auth) and has_permission(
        request_auth, db, "support:ticket:read"
    )
    try:
        detail_data = web_customer_details_service.build_customer_detail_snapshot(
            db=db,
            customer_id=customer_id,
            include_conversations=show_conversations,
        )
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Customer not found"},
            status_code=404,
        )
    except Exception:
        logger.exception("Error loading customer detail for %s", customer_id)
        raise

    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)
    auth = getattr(getattr(request, "state", None), "auth", None) or {}
    from app.services import location_capture

    can_confirm_location = bool(auth) and has_permission(auth, db, "customer:write")
    can_unsuspend_account = bool(auth) and has_permission(auth, db, "customer:update")
    location_capture_enabled = can_confirm_location and location_capture.prompt_enabled(
        db
    )
    notification_context = web_notifications_service.bulk_notification_setup_context(db)
    pppoe_access = detail_data.get("pppoe_access") or {
        "has_credential": False,
        "credential_id": None,
        "login": None,
        "has_password": False,
    }
    customer = detail_data["customer"]
    customer_type = str(detail_data.get("customer_type") or "person")
    notification_channels = notification_context.get("bulk_notification_channels") or []
    notification_templates = (
        notification_context.get("bulk_notification_templates") or []
    )
    stats_url = (
        f"/admin/customers/person/{customer.id}/stats"
        f"?usage_period={usage_period}"
        f"&usage_page={usage_page}"
        f"&usage_per_page={usage_per_page}"
    )
    if usage_view == "table":
        stats_url += "&usage_view=table"
    detail_config = {
        "statsUrl": stats_url,
        "detailUrl": f"/admin/customers/person/{customer.id}",
        "customerId": str(customer.id),
        "customerType": customer_type,
        "notificationChannels": notification_channels,
        "notificationTemplates": notification_templates,
    }

    return templates.TemplateResponse(
        "admin/customers/detail.html",
        {
            "request": request,
            **detail_data,
            "pppoe_access": pppoe_access,
            "usage_period": usage_period,
            "usage_page": usage_page,
            "usage_per_page": usage_per_page,
            "usage_view": usage_view,
            "customer_type": customer_type,
            "detail_config": detail_config,
            "bulk_notification_channels": notification_channels,
            "bulk_notification_templates": notification_templates,
            **_subscription_action_permission_context(request, db),
            "current_user": current_user,
            "location_capture_enabled": location_capture_enabled,
            "can_unsuspend_account": can_unsuspend_account,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post(
    "/person/{customer_id}/services/{subscription_id}/remote-plan-change/{request_id}/provision",
    dependencies=[Depends(require_permission("provisioning:service_change_reconcile"))],
)
def provision_remote_plan_change(
    request: Request,
    customer_id: uuid.UUID,
    subscription_id: uuid.UUID,
    request_id: uuid.UUID,
    idempotency_key: str = Form(..., min_length=16, max_length=160),
    reason: str = Form(..., min_length=8, max_length=500),
    confirmed_price_fingerprint: str | None = Form(None, max_length=64),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    actor_id = str(_get_actor_id(request) or "admin")
    try:
        outcome = provision_and_verify_remote_change(
            db,
            RemoteProvisionActionCommand(
                request_id=request_id,
                subscription_id=subscription_id,
                account_id=customer_id,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                reason=reason,
                confirmed_price_fingerprint=confirmed_price_fingerprint,
            ),
        )
        feedback_status = (
            "price_changed"
            if outcome.status == RemoteProvisionActionStatus.price_review_required
            else "billing_error"
            if outcome.status == RemoteProvisionActionStatus.billing_blocked
            else "success"
        )
        feedback_message = outcome.message
        feedback_reference = outcome.operation_reference
    except SubscriptionChangeExecutionError as exc:
        log_audit_event(
            db,
            request,
            action="provision_remote_plan_change",
            entity_type="subscription_change_request",
            entity_id=str(request_id),
            actor_id=actor_id,
            metadata={
                "subscription_id": str(subscription_id),
                "error_code": exc.code,
            },
            status_code=409,
            is_success=False,
        )
        feedback_status = (
            "pending"
            if exc.code
            in {
                "remote_reprovision_verification_missing",
                "remote_reprovision_compensation_failed",
            }
            else "error"
        )
        feedback_message = str(exc)
        feedback_reference = f"remote-plan-change:{request_id}"
    except Exception as exc:
        logger.exception(
            "Remote plan provisioning failed",
            extra={
                "subscription_change_request_id": str(request_id),
                "subscription_id": str(subscription_id),
            },
        )
        log_audit_event(
            db,
            request,
            action="provision_remote_plan_change",
            entity_type="subscription_change_request",
            entity_id=str(request_id),
            actor_id=actor_id,
            metadata={
                "subscription_id": str(subscription_id),
                "error_type": type(exc).__name__,
            },
            status_code=500,
            is_success=False,
        )
        feedback_status = "error"
        feedback_message = (
            "Provisioning did not complete. No plan change was applied; retry this "
            "request using the operation reference below."
        )
        feedback_reference = f"remote-plan-change:{request_id}"
    return RedirectResponse(
        url=(
            f"/admin/customers/person/{customer_id}"
            f"?service_change_status={quote_plus(feedback_status)}"
            f"&service_change_message={quote_plus(feedback_message)}"
            f"&service_change_reference={quote_plus(feedback_reference)}"
        ),
        status_code=303,
    )


@router.get(
    "/subscriptions/{subscription_id}/fiber-path",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def subscription_fiber_path_panel(
    request: Request,
    subscription_id: str,
    db: Session = Depends(get_db),
):
    """Lazy passive-fibre plant fragment for one subscription's path card.

    ui.customer_network_path_projection composes network.fiber_topology's
    validated trace; this route only authorizes and renders it.
    """

    view = customer_network_path_service.project_subscription_fiber_detail(
        db, subscription_id
    )
    return templates.TemplateResponse(
        "admin/customers/_fiber_path_panel.html",
        {
            "request": request,
            "fiber_path": view.to_dict() if view else None,
        },
    )


@router.get(
    "/person/{customer_id}/stats",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def person_detail_stats(
    request: Request,
    customer_id: str,
    usage_period: str = Query("current"),
    usage_page: int = Query(1, ge=1),
    usage_per_page: int = Query(25, ge=10, le=100),
    usage_view: str = Query("chart", pattern="^(chart|table)$"),
    db: Session = Depends(get_db),
):
    usage_period = _normalize_usage_period(usage_period)
    usage_view = _normalize_usage_view(usage_view)
    subscriber = _get_subscriber(db=db, subscriber_id=customer_id)

    usage_customer = {"subscriber_id": str(subscriber.id)}
    usage_portal = customer_portal.get_usage_page(
        db,
        usage_customer,
        period=usage_period,
        page=usage_page,
        per_page=usage_per_page,
        allow_postgres_fallback=True,
    )
    usage_subscription = resolve_customer_subscription(db, usage_customer)
    initial_bandwidth_stats = _load_initial_bandwidth_stats(
        db,
        usage_subscription.id if usage_subscription else None,
    )

    return templates.TemplateResponse(
        "admin/customers/_stats_panel.html",
        {
            "request": request,
            "customer_id": str(subscriber.id),
            "usage_portal": usage_portal,
            "bandwidth_chart_initial_stats": initial_bandwidth_stats,
            "usage_subscription_id": (
                str(usage_subscription.id) if usage_subscription else None
            ),
            "usage_view": usage_view,
        },
    )


@router.get(
    "/person/{customer_id}/pppoe-password",
    dependencies=[Depends(require_permission("customer:read"))],
)
def person_pppoe_password(
    request: Request,
    customer_id: str,
    credential_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Reveal a customer's reusable PPPoE service password.

    Staff-only (admin router is system_user-gated). Every reveal is audited
    and per-actor rate-limited — this exposes a reusable credential whose leak
    equals takeover of the customer's connection.
    """
    from app.models.audit import AuditActorType
    from app.services import web_admin as web_admin_service
    from app.services.audit_adapter import record_audit_event
    from app.services.rate_limiter_adapter import allow_operation

    actor = web_admin_service.get_current_user(request)
    actor_id = web_admin_service.get_actor_id(request)
    actor_metadata = {
        key: value
        for key, value in {
            "actor_name": actor.get("name") if actor else None,
            "actor_email": actor.get("email") if actor else None,
        }.items()
        if value
    }

    decision = allow_operation(
        f"pppoe-reveal:{actor_id or 'unknown'}",
        limit=30,
        window_seconds=3600,
    )
    if not decision.allowed:
        record_audit_event(
            db,
            action="customer.pppoe_password_reveal",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_type=AuditActorType.user,
            actor_id=actor_id,
            metadata={
                "credential_id": credential_id,
                "reason": "rate_limited",
                **actor_metadata,
            },
            status_code=429,
            is_success=False,
        )
        return JSONResponse(
            {"password": "", "detail": "Reveal rate limit reached. Try again later."},
            status_code=429,
        )

    password, found = web_customer_details_service.reveal_customer_pppoe_password(
        db,
        customer_id,
        credential_id=credential_id,
    )
    record_audit_event(
        db,
        action="customer.pppoe_password_reveal",
        entity_type="subscriber",
        entity_id=str(customer_id),
        actor_type=AuditActorType.user,
        actor_id=actor_id,
        metadata={
            "credential_id": credential_id,
            "found": bool(found),
            **actor_metadata,
        },
        status_code=200 if found else 404,
        is_success=bool(found),
    )
    if not found:
        return JSONResponse({"password": ""}, status_code=404)
    return JSONResponse({"password": password})


@router.get(
    "/business/{customer_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def business_detail(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    try:
        subscriber = _get_subscriber(db=db, subscriber_id=customer_id)
        if subscriber.category != SubscriberCategory.business:
            raise HTTPException(status_code=404, detail="Business customer not found")
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Customer not found"},
            status_code=404,
        )
    return RedirectResponse(
        url=f"/admin/customers/person/{subscriber.id}",
        status_code=302,
    )


@router.post(
    "/{customer_type}/{customer_id}/user/invite",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_user_send_invite(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        result = web_customer_user_access_service.send_customer_invite(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
            request=request,
            actor_id=actor_id,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=bool(result["ok"]),
            title=str(result["title"]),
            message=str(result["message"]),
        )
    except Exception as exc:
        web_customer_user_access_service.log_customer_user_access_error(
            db=db,
            request=request,
            action=web_customer_user_access_service.INVITE_AUDIT_ACTION,
            customer_type=customer_type,
            customer_id=customer_id,
            actor_id=actor_id,
            error=exc,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="User invite",
            message=str(exc),
        )


@router.post(
    "/{customer_type}/{customer_id}/user/reset-link",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_user_send_reset_link(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        result = web_customer_user_access_service.send_customer_reset_link(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
            request=request,
            actor_id=actor_id,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=bool(result["ok"]),
            title=str(result["title"]),
            message=str(result["message"]),
        )
    except Exception as exc:
        web_customer_user_access_service.log_customer_user_access_error(
            db=db,
            request=request,
            action=web_customer_user_access_service.RESET_AUDIT_ACTION,
            customer_type=customer_type,
            customer_id=customer_id,
            actor_id=actor_id,
            error=exc,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Password reset",
            message=str(exc),
        )


@router.post(
    "/{customer_type}/{customer_id}/user/reset-mfa",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_user_reset_mfa(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        result = web_customer_user_access_service.reset_customer_mfa(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
            request=request,
            actor_id=actor_id,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=bool(result["ok"]),
            title=str(result["title"]),
            message=str(result["message"]),
        )
    except Exception as exc:
        web_customer_user_access_service.log_customer_user_access_error(
            db=db,
            request=request,
            action=web_customer_user_access_service.MFA_RESET_AUDIT_ACTION,
            customer_type=customer_type,
            customer_id=customer_id,
            actor_id=actor_id,
            error=exc,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="MFA reset",
            message=str(exc),
        )


@router.post(
    "/{customer_type}/{customer_id}/user/activate-login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_user_activate_login(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        result = web_customer_user_access_service.set_customer_login_active(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
            request=request,
            actor_id=actor_id,
            is_active=True,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=bool(result["ok"]),
            title=str(result["title"]),
            message=str(result["message"]),
        )
    except Exception as exc:
        web_customer_user_access_service.log_customer_user_access_error(
            db=db,
            request=request,
            action=web_customer_user_access_service.LOGIN_TOGGLE_AUDIT_ACTION,
            customer_type=customer_type,
            customer_id=customer_id,
            actor_id=actor_id,
            error=exc,
            login_active=True,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Login activation",
            message=str(exc),
        )


@router.post(
    "/{customer_type}/{customer_id}/user/deactivate-login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def customer_user_deactivate_login(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        result = web_customer_user_access_service.set_customer_login_active(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
            request=request,
            actor_id=actor_id,
            is_active=False,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=bool(result["ok"]),
            title=str(result["title"]),
            message=str(result["message"]),
        )
    except Exception as exc:
        web_customer_user_access_service.log_customer_user_access_error(
            db=db,
            request=request,
            action=web_customer_user_access_service.LOGIN_TOGGLE_AUDIT_ACTION,
            customer_type=customer_type,
            customer_id=customer_id,
            actor_id=actor_id,
            error=exc,
            login_active=False,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Login deactivation",
            message=str(exc),
        )


@router.post("/person/{customer_id}/impersonate", response_class=HTMLResponse)
def person_impersonate(
    request: Request,
    customer_id: str,
    account_id: str = Form(...),
    subscription_id: str | None = Form(None),
    db: Session = Depends(get_db),
    auth=Depends(require_permission("subscriber:impersonate")),
):
    """Impersonate a person customer and open the portal."""
    try:
        session_token = web_customer_actions_service.create_impersonation_session(
            db=db,
            request=request,
            customer_type="person",
            customer_id=customer_id,
            account_id=account_id,
            subscription_id=subscription_id,
            auth=auth,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
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


@router.post("/business/{customer_id}/impersonate", response_class=HTMLResponse)
def business_impersonate(
    request: Request,
    customer_id: str,
    account_id: str = Form(...),
    subscription_id: str | None = Form(None),
    db: Session = Depends(get_db),
    auth=Depends(require_permission("subscriber:impersonate")),
):
    """Impersonate a business customer and open the portal."""
    try:
        session_token = web_customer_actions_service.create_impersonation_session(
            db=db,
            request=request,
            customer_type="business",
            customer_id=customer_id,
            account_id=account_id,
            subscription_id=subscription_id,
            auth=auth,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
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


@router.get(
    "/person/{customer_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def person_edit(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Edit person form."""
    try:
        customer = _get_subscriber(db=db, subscriber_id=customer_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Customer not found"},
            status_code=404,
        )
    except Exception:
        logger.exception("Error loading person edit for %s", customer_id)
        raise

    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/form.html",
        {
            "request": request,
            "customer": customer,
            "customer_type": "person",
            "action": "edit",
            "tax_rates": _load_tax_rates(db),
            "billing_form": _billing_form_defaults(db, "person", customer),
            "customer_audit_items": _customer_audit_items(db, customer),
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            **_reseller_form_context(db, customer.reseller_id),
        },
    )


@router.get(
    "/business/{customer_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def business_edit(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Edit business customer form."""
    try:
        resolved_customer_id = _resolve_business_customer_id(db, customer_id)
        customer = _get_subscriber(db=db, subscriber_id=resolved_customer_id)
    except HTTPException:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Business customer not found"},
            status_code=404,
        )
    except Exception:
        logger.exception("Error loading business edit for %s", customer_id)
        raise

    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/form.html",
        {
            "request": request,
            "customer": customer,
            "customer_type": "business",
            "action": "edit",
            "tax_rates": _load_tax_rates(db),
            "billing_form": _billing_form_defaults(db, "business", customer),
            "customer_audit_items": _customer_audit_items(db, customer),
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            **_reseller_form_context(db, customer.reseller_id),
        },
    )


@router.post(
    "/person/{customer_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def person_update(
    request: Request,
    customer_id: str,
    first_name: str = Form(...),
    last_name: str = Form(...),
    display_name: str | None = Form(None),
    avatar_url: str | None = Form(None),
    bio: str | None = Form(None),
    email: str | None = Form(None),
    email_verified: str | None = Form(None),
    phone: str | None = Form(None),
    nin: str | None = Form(None),
    date_of_birth: str | None = Form(None),
    gender: str | None = Form(None),
    preferred_contact_method: str | None = Form(None),
    locale: str | None = Form(None),
    timezone: str | None = Form(None),
    address_line1: str | None = Form(None),
    address_line2: str | None = Form(None),
    city: str | None = Form(None),
    region: str | None = Form(None),
    postal_code: str | None = Form(None),
    country_code: str | None = Form(None),
    marketing_opt_in: str | None = Form(None),
    notes: str | None = Form(None),
    account_start_date: str | None = Form(None),
    billing_enabled_override: str | None = Form(None),
    billing_day: str | None = Form(None),
    payment_due_days: str | None = Form(None),
    grace_period_days: str | None = Form(None),
    min_balance: str | None = Form(None),
    captive_redirect_enabled: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    withholding_tax_enabled: str | None = Form(None),
    vat_exempt: str | None = Form(None),
    payment_method: str | None = Form(None),
    metadata: str | None = Form(None),
    managed_by_reseller: str | None = Form(None),
    reseller_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update a person."""
    try:
        before, after = web_customer_actions_service.update_person_customer(
            db=db,
            customer_id=customer_id,
            first_name=first_name,
            last_name=last_name,
            display_name=display_name,
            avatar_url=avatar_url,
            email=email,
            email_verified=email_verified,
            phone=phone,
            nin=nin,
            date_of_birth=date_of_birth,
            gender=gender,
            preferred_contact_method=preferred_contact_method,
            locale=locale,
            timezone_value=timezone,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            marketing_opt_in=marketing_opt_in,
            notes=notes,
            account_start_date=account_start_date,
            billing_enabled_override=billing_enabled_override,
            billing_day=billing_day,
            payment_due_days=payment_due_days,
            grace_period_days=grace_period_days,
            min_balance=min_balance,
            captive_redirect_enabled=captive_redirect_enabled,
            tax_rate_id=tax_rate_id,
            withholding_tax_enabled=withholding_tax_enabled,
            vat_exempt=vat_exempt,
            payment_method=payment_method,
            metadata_json=web_customer_actions_service.parse_json_object(
                metadata, "metadata"
            )
            if metadata is not None
            else None,
            actor_id=_get_actor_id(request),
            managed_by_reseller=managed_by_reseller is not None,
            reseller_id=reseller_id,
        )
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(
            url=f"/admin/customers/person/{customer_id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        # Roll back the failed transaction before re-querying for the form
        # re-render; otherwise the aborted session turns any DB error (e.g. a
        # unique-email violation) into a 500 instead of a graceful 400.
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        try:
            customer = _get_subscriber(db=db, subscriber_id=customer_id)
        except Exception:
            customer = None
        return templates.TemplateResponse(
            "admin/customers/form.html",
            {
                "request": request,
                "customer": customer,
                "customer_type": "person",
                "action": "edit",
                "error": _safe_form_error(e),
                "tax_rates": _load_tax_rates(db),
                "billing_form": _billing_form_defaults(db, "person", customer),
                "customer_audit_items": _customer_audit_items(db, customer),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
                **_reseller_form_context(
                    db, customer.reseller_id if customer is not None else None
                ),
            },
            status_code=400,
        )


@router.post(
    "/business/{customer_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def business_update(
    request: Request,
    customer_id: str,
    name: str = Form(...),
    legal_name: str | None = Form(None),
    tax_id: str | None = Form(None),
    domain: str | None = Form(None),
    website: str | None = Form(None),
    business_notes: str | None = Form(None),
    business_account_start_date: str | None = Form(None),
    billing_enabled_override: str | None = Form(None),
    billing_day: str | None = Form(None),
    payment_due_days: str | None = Form(None),
    grace_period_days: str | None = Form(None),
    min_balance: str | None = Form(None),
    captive_redirect_enabled: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    withholding_tax_enabled: str | None = Form(None),
    vat_exempt: str | None = Form(None),
    payment_method: str | None = Form(None),
    managed_by_reseller: str | None = Form(None),
    reseller_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update a business customer."""
    try:
        before, after = web_customer_actions_service.update_business_customer(
            db=db,
            customer_id=customer_id,
            name=name,
            legal_name=legal_name,
            tax_id=tax_id,
            domain=domain,
            website=website,
            org_notes=business_notes,
            org_account_start_date=business_account_start_date,
            billing_enabled_override=billing_enabled_override,
            billing_day=billing_day,
            payment_due_days=payment_due_days,
            grace_period_days=grace_period_days,
            min_balance=min_balance,
            captive_redirect_enabled=captive_redirect_enabled,
            tax_rate_id=tax_rate_id,
            withholding_tax_enabled=withholding_tax_enabled,
            vat_exempt=vat_exempt,
            payment_method=payment_method,
            actor_id=_get_actor_id(request),
            managed_by_reseller=managed_by_reseller is not None,
            reseller_id=reseller_id,
        )
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(
            url=f"/admin/customers/business/{customer_id}",
            status_code=303,
        )
    except Exception as e:
        # Roll back the failed transaction before re-querying for the form
        # re-render; otherwise the aborted session turns any DB error (e.g. a
        # unique-email violation) into a 500 instead of a graceful 400.
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        try:
            customer = _get_subscriber(db=db, subscriber_id=customer_id)
        except Exception:
            customer = None
        return templates.TemplateResponse(
            "admin/customers/form.html",
            {
                "request": request,
                "customer": customer,
                "customer_type": "business",
                "action": "edit",
                "error": _safe_form_error(e),
                "tax_rates": _load_tax_rates(db),
                "billing_form": _billing_form_defaults(db, "business", customer),
                "customer_audit_items": _customer_audit_items(db, customer),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
                **_reseller_form_context(
                    db, customer.reseller_id if customer is not None else None
                ),
            },
            status_code=400,
        )


@router.post(
    "/person/{customer_id}/convert-to-business",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def person_convert_to_business(
    request: Request,
    customer_id: str,
    company_name: str = Form(...),
    legal_name: str | None = Form(None),
    tax_id: str | None = Form(None),
    domain: str | None = Form(None),
    website: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Convert an individual customer into a business customer."""
    try:
        before, after = (
            web_customer_actions_service.convert_person_to_business_customer(
                db=db,
                customer_id=customer_id,
                company_name=company_name,
                legal_name=legal_name,
                tax_id=tax_id,
                domain=domain,
                website=website,
            )
        )
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="convert_to_business",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(
            url=f"/admin/customers/person/{customer_id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        try:
            customer = _get_subscriber(db=db, subscriber_id=customer_id)
        except Exception:
            customer = None
        return templates.TemplateResponse(
            "admin/customers/form.html",
            {
                "request": request,
                "customer": customer,
                "customer_type": "person",
                "action": "edit",
                "error": _safe_form_error(e),
                "tax_rates": _load_tax_rates(db),
                "billing_form": _billing_form_defaults(db, "person", customer),
                "customer_audit_items": _customer_audit_items(db, customer),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=400,
        )


@router.post(
    "/person/{customer_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def person_deactivate(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Deactivate a person before deletion."""
    before, after = web_customer_actions_service.deactivate_person_customer(
        db=db,
        customer_id=customer_id,
        actor_id=_get_actor_id(request),
    )
    metadata_payload = build_changes_metadata(before, after)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscriber",
        entity_id=str(customer_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(
        url=f"/admin/customers/person/{customer_id}", status_code=303
    )


@router.post(
    "/business/{customer_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def business_deactivate(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Deactivate a business customer before deletion."""
    web_customer_actions_service.deactivate_business_customer(
        db=db,
        customer_id=customer_id,
        actor_id=_get_actor_id(request),
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscriber",
        entity_id=str(customer_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"changes": {"is_active": {"from": True, "to": False}}},
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(
        url=f"/admin/customers/business/{customer_id}", status_code=303
    )


@router.post(
    "/{customer_type}/{customer_id}/suspend-services",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_any_permission("catalog:write", "subscription:suspend"))
    ],
)
def customer_suspend_active_services(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Suspend all active services attached to a customer row."""
    active_subscription_ids = web_customer_actions_service.list_active_subscription_ids(
        db, customer_id
    )
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    if not active_subscription_ids:
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="No active services",
            message="This customer has no active services to suspend.",
        )

    result = web_catalog_subscription_workflows_service.bulk_suspend_response(
        db,
        subscription_ids=",".join(active_subscription_ids),
        request=request,
        actor_id=_get_actor_id(request),
    )
    changed = _workflow_changed_count(result)
    return _toast_response(
        request=request,
        redirect_url=redirect_url,
        ok=changed > 0,
        title="Services suspended" if changed else "No services suspended",
        message=str(result.get("message") or "Suspension completed."),
    )


@router.post(
    "/{customer_type}/{customer_id}/activate-services",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_any_permission("catalog:write", "subscription:activate"))
    ],
)
def customer_activate_suspended_services(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Activate all suspended services attached to a customer row."""
    suspended_subscription_ids = (
        web_customer_actions_service.list_suspended_subscription_ids(db, customer_id)
    )
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    if not suspended_subscription_ids:
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="No suspended services",
            message="This customer has no suspended services to activate.",
        )

    result = web_catalog_subscription_workflows_service.bulk_restore_response(
        db,
        subscription_ids=",".join(suspended_subscription_ids),
        actor_id=_get_actor_id(request),
    )
    changed = _workflow_changed_count(result)
    return _toast_response(
        request=request,
        redirect_url=redirect_url,
        ok=changed > 0,
        title="Services activated" if changed else "No services activated",
        message=str(result.get("message") or "Activation completed."),
    )


@router.post(
    "/{customer_type}/{customer_id}/unsuspend",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:update"))],
)
def customer_unsuspend_account(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    preview_fingerprint: str = Form(..., min_length=64, max_length=64),
    db: Session = Depends(get_db),
):
    """Unsuspend one account without enabling disabled historical services."""
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    try:
        outcome = web_customer_actions_service.unsuspend_customer_account(
            db,
            customer_id,
            preview_fingerprint=preview_fingerprint,
            actor_id=_get_actor_id(request),
        )
    except (DomainError, ValueError) as exc:
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Account not unsuspended",
            message=str(exc),
        )
    except Exception:
        logger.exception("Failed to unsuspend customer account %s", customer_id)
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Account not unsuspended",
            message="The account could not be unsuspended. Review its access state.",
        )

    return _toast_response(
        request=request,
        redirect_url=redirect_url,
        ok=True,
        title="Account unsuspended",
        message=(
            f"The account is now {outcome.status.value}. Disabled services and "
            "unrelated enforcement locks were preserved."
        ),
    )


@router.post(
    "/{customer_type}/{customer_id}/repair-access-state",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_any_permission("customer:write", "subscription:activate"))
    ],
)
def customer_repair_access_state(
    request: Request,
    customer_type: Literal["person", "business"],
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Repair one customer's account/RADIUS projection when safe to do so."""
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    try:
        result = web_customer_actions_service.repair_customer_access_state(
            db, customer_id
        )
    except ValueError as exc:
        db.rollback()
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Access repair skipped",
            message=str(exc),
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to repair access state for customer %s", customer_id)
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Access repair failed",
            message="The repair could not be completed. Check logs before retrying.",
        )

    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscriber",
        entity_id=str(customer_id),
        actor_id=_get_actor_id(request),
        metadata={"access_state_repair": result},
    )
    db.commit()

    return _toast_response(
        request=request,
        redirect_url=redirect_url,
        ok=True,
        title="Access state repaired",
        message=(
            "Recomputed account status and refreshed RADIUS for "
            f"{result.get('active_subscriptions', 0)} active subscription(s)."
        ),
    )


@router.delete(
    "/person/{customer_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:delete"))],
)
@router.post(
    "/person/{customer_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:delete"))],
)
def person_delete(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Delete a person."""
    try:
        web_customer_actions_service.delete_person_customer(
            db=db, customer_id=customer_id
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": "/admin/customers"})
        return RedirectResponse(url="/admin/customers", status_code=303)
    except HTTPException as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc.detail), status_code=200, reswap="none")
        raise
    except IntegrityError:
        db.rollback()
        message = "Cannot delete customer. Linked records exist."
        if request.headers.get("HX-Request"):
            return _htmx_error_response(message, status_code=200, reswap="none")
        raise HTTPException(status_code=409, detail=message)
    except Exception as e:
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {
                "request": request,
                "error": _safe_form_error(e),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=500,
        )


@router.delete(
    "/business/{customer_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:delete"))],
)
@router.post(
    "/business/{customer_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:delete"))],
)
def business_delete(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Delete a business customer."""
    try:
        web_customer_actions_service.delete_business_customer(
            db=db,
            customer_id=customer_id,
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": "/admin/customers"})
        return RedirectResponse(url="/admin/customers", status_code=303)
    except HTTPException as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc.detail), status_code=200, reswap="none")
        raise
    except IntegrityError:
        db.rollback()
        message = "Cannot delete business customer. Linked records exist."
        if request.headers.get("HX-Request"):
            return _htmx_error_response(message, status_code=200, reswap="none")
        raise HTTPException(status_code=409, detail=message)
    except Exception as e:
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {
                "request": request,
                "error": _safe_form_error(e),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=500,
        )


# ============================================================================
# Address Management Routes
# ============================================================================


@router.post(
    "/addresses",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def create_address(
    request: Request,
    subscriber_id: str = Form(...),
    customer_type: str = Form(...),
    customer_id: str = Form(...),
    address_type: str = Form("service"),
    label: str | None = Form(None),
    address_line1: str = Form(...),
    address_line2: str | None = Form(None),
    city: str | None = Form(None),
    region: str | None = Form(None),
    postal_code: str | None = Form(None),
    country_code: str | None = Form(None),
    is_primary: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new address for a subscriber."""
    try:
        web_customer_actions_service.create_customer_address(
            db=db,
            subscriber_id=subscriber_id,
            address_type=address_type,
            label=label,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            is_primary=is_primary,
        )

        # Redirect back to customer detail page
        redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)

    except (IntegrityError, ValueError) as e:
        # Recoverable input errors (duplicate/constraint, bad id, validation)
        # must not render a 500. Roll back first so the aborted transaction
        # can't poison the error-path re-render (the #19/#24 pattern), then
        # surface a clean toast (HX) or 4xx.
        db.rollback()
        conflict = isinstance(e, IntegrityError)
        message = (
            "This conflicts with an existing record."
            if conflict
            else _safe_form_error(e)
        )
        status_code = 409 if conflict else 400
        if request.headers.get("HX-Request"):
            return _htmx_error_response(
                message, status_code=200, title="Could not save", reswap="none"
            )
        raise HTTPException(status_code=status_code, detail=message)
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats

        db.rollback()
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {
                "request": request,
                "error": _safe_form_error(e),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=500,
        )


@router.post(
    "/addresses/{address_id}/geocode",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def geocode_address(
    address_id: str,
    latitude: float = Body(...),
    longitude: float = Body(...),
    db: Session = Depends(get_db),
):
    """Update address coordinates from a geocoding selection."""
    return JSONResponse(
        web_customer_actions_service.save_address_coordinates(
            db=db,
            address_id=address_id,
            latitude=latitude,
            longitude=longitude,
        )
    )


@router.post(
    "/profile/{customer_id}/geocode-primary",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def geocode_primary_address(
    customer_id: str,
    latitude: float = Body(...),
    longitude: float = Body(...),
    db: Session = Depends(get_db),
):
    """Save coordinates to a primary address, creating one from profile address if missing."""
    return JSONResponse(
        web_customer_actions_service.save_primary_address_coordinates(
            db=db,
            customer_id=customer_id,
            latitude=latitude,
            longitude=longitude,
        )
    )


@router.post(
    "/profile/{customer_id}/confirm-location",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def confirm_customer_location(
    request: Request,
    customer_id: str,
    latitude: float = Body(...),
    longitude: float = Body(...),
    accuracy_m: float | None = Body(default=None),
    claimed_state: str | None = Body(default=None),
    claimed_lga: str | None = Body(default=None),
    claimed_postcode: str | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """Agent-confirmed service location for the NCC verification ledger.

    The agent, on a call with the customer, confirms or corrects the location
    we hold. Reconciled facts land in the ledger via ``location_capture``; a
    disagreement is flagged, not silently overwritten.
    """
    from app.services import location_capture
    from app.web.admin import get_current_user

    actor = get_current_user(request) or {}
    actor_id = str(actor.get("id") or actor.get("subscriber_id") or "") or None
    actor_name = actor.get("email") or actor.get("username")
    try:
        result = location_capture.capture(
            db,
            customer_id,
            lat=latitude,
            lng=longitude,
            accuracy_m=accuracy_m,
            source=location_capture.SOURCE_AGENT,
            actor_id=actor_id,
            actor_name=actor_name,
            claimed_state=claimed_state,
            claimed_lga=claimed_lga,
            claimed_postcode=claimed_postcode,
        )
    except location_capture.LocationCaptureDisabled as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return JSONResponse(
        {
            "captured": [k.value for k in result.captured_keys],
            "needs_human": [
                {"field": f.key.value, "note": f.note}
                for f in result.reconciliation.needs_human
            ],
        }
    )


@router.delete(
    "/addresses/{address_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def delete_address(
    request: Request,
    address_id: str,
    db: Session = Depends(get_db),
):
    """Delete an address."""
    try:
        web_customer_actions_service.delete_customer_address(
            db=db, address_id=address_id
        )
        # Return empty response for HTMX to remove the element
        return HTMLResponse(content="")
    except Exception as e:
        logger.exception("Error deleting address %s: %s", address_id, e)
        return HTMLResponse(
            content='<div class="text-red-500 text-sm p-2">An error occurred while deleting.</div>',
            status_code=500,
        )


# ============================================================================
# Contact Management Routes
# ============================================================================


@router.post(
    "/contacts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def create_contact(
    request: Request,
    account_id: str = Form(...),
    customer_type: str = Form(...),
    customer_id: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    role: str = Form("primary"),
    title: str | None = Form(None),
    email: str | None = Form(None),
    phone: str | None = Form(None),
    is_primary: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new contact for an account."""
    try:
        web_customer_actions_service.create_customer_contact(
            db=db,
            account_id=account_id,
            first_name=first_name,
            last_name=last_name,
            role=role,
            title=title,
            email=email,
            phone=phone,
            is_primary=is_primary,
        )

        # Redirect back to customer detail page
        redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)

    except (IntegrityError, ValueError) as e:
        # Recoverable input errors (duplicate/constraint, bad id, validation)
        # must not render a 500. Roll back first so the aborted transaction
        # can't poison the error-path re-render (the #19/#24 pattern), then
        # surface a clean toast (HX) or 4xx.
        db.rollback()
        conflict = isinstance(e, IntegrityError)
        message = (
            "This conflicts with an existing record."
            if conflict
            else _safe_form_error(e)
        )
        status_code = 409 if conflict else 400
        if request.headers.get("HX-Request"):
            return _htmx_error_response(
                message, status_code=200, title="Could not save", reswap="none"
            )
        raise HTTPException(status_code=status_code, detail=message)
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats

        db.rollback()
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {
                "request": request,
                "error": _safe_form_error(e),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=500,
        )


@router.delete(
    "/contacts/{contact_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def delete_contact(
    request: Request,
    contact_id: str,
    db: Session = Depends(get_db),
):
    """Delete a contact."""
    try:
        web_customer_actions_service.delete_customer_contact(
            db=db, contact_id=contact_id
        )
        # Return empty response for HTMX to remove the element
        return HTMLResponse(content="")
    except Exception as e:
        logger.exception("Error deleting contact %s: %s", contact_id, e)
        return HTMLResponse(
            content='<div class="text-red-500 text-sm p-2">An error occurred while deleting.</div>',
            status_code=500,
        )


# ============================================================================
# Bulk Operations Routes
# ============================================================================


@router.post(
    "/bulk/status", dependencies=[Depends(require_permission("customer:write"))]
)
def bulk_update_status(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Bulk update customer status (activate/deactivate)."""
    try:
        return web_customer_actions_service.bulk_update_customer_status_from_payload(
            db=db, payload=data
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Bulk customer status update failed")
        raise HTTPException(
            status_code=500,
            detail="Bulk status update failed. Please try again.",
        ) from e


@router.post(
    "/bulk/update", dependencies=[Depends(require_permission("customer:write"))]
)
def bulk_update_customers(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Bulk update supported customer fields."""
    try:
        return web_customer_actions_service.bulk_update_customers_from_payload(
            db=db,
            payload=data,
            actor_id=_get_actor_id(request),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Bulk customer update failed")
        raise HTTPException(
            status_code=500,
            detail="Bulk customer update failed. Please try again.",
        ) from e


@router.post(
    "/bulk/send-message", dependencies=[Depends(require_permission("customer:write"))]
)
def bulk_send_customer_message(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Queue a bulk notification for selected or filtered customers."""
    try:
        result = web_customer_actions_service.queue_bulk_message_from_payload(
            db=db, payload=data
        )
        if result.get("preview") is True:
            return result
        return _kick_notification_delivery(result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Bulk customer message queue failed")
        raise HTTPException(
            status_code=500,
            detail="Bulk message queue failed. Please try again.",
        ) from e


@router.get(
    "/bulk/whatsapp-template",
    dependencies=[Depends(require_permission("customer:read"))],
)
def bulk_whatsapp_template_details(
    name: str = Query(..., min_length=1),
    language: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Fetch Meta WhatsApp template component details for the broadcast modal."""
    try:
        return web_customer_actions_service.whatsapp_template_details(
            db=db,
            name=name,
            language=language,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post(
    "/bulk/delete", dependencies=[Depends(require_permission("customer:delete"))]
)
def bulk_delete_customers(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Bulk delete customers (only inactive customers without subscribers)."""
    try:
        return web_customer_actions_service.bulk_delete_customers_from_payload(
            db=db, payload=data
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/export", dependencies=[Depends(require_permission("customer:read"))])
def export_customers(
    request: Request,
    ids: str = Query("all"),
    search: str | None = None,
    customer_type: str | None = None,
    db: Session = Depends(get_db),
):
    """Export customers to CSV."""
    content, filename = web_customer_actions_service.export_customers_csv(
        db=db,
        ids=ids,
        search=search,
        customer_type=customer_type,
    )

    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )


@router.get(
    "/{subscriber_id}/availability",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def customer_availability_report(
    request: Request,
    subscriber_id: str,
    period: str | None = None,
    db: Session = Depends(get_db),
):
    """Agent-facing service availability for one customer over a period.

    Infrastructure-based: what the shared path serving them actually delivered,
    plus individual provider-fault tickets. The customer's own CPE/session
    downtime is deliberately excluded — see topology.customer_availability.
    """
    from app.web.admin import get_current_user, get_sidebar_stats

    context = web_customer_availability_service.customer_availability_context(
        db, subscriber_id, period=period
    )
    context.update(
        {
            "request": request,
            "active_page": "customers",
            "active_menu": "customers",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        }
    )
    return templates.TemplateResponse("admin/customers/availability.html", context)
