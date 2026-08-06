"""Admin catalog component management web routes."""

import logging
from urllib.parse import quote_plus
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.csrf import get_csrf_token
from app.db import finish_read_transaction, form_write, get_db
from app.schemas.plan_family_catalogue import PublishPlanFamilyCatalogueCommand
from app.services import web_catalog_settings as settings_svc
from app.services.auth_dependencies import require_permission
from app.services.catalog import plan_family_catalogues
from app.services.owner_commands import CommandContext
from app.web.request_parsing import parse_form_data

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/catalog/settings", tags=["web-admin-catalog-settings"])
legacy_add_ons_router = APIRouter(
    prefix="/catalog/add-ons", tags=["web-admin-catalog-settings-legacy"]
)


def _form_getlist_str(form: FormData, key: str) -> list[str]:
    return [value for value in form.getlist(key) if isinstance(value, str)]


def _actor_id(request: Request) -> str | None:
    value = getattr(request.state, "actor_id", None)
    return str(value) if value else None


def _base_context(
    request: Request, db: Session, active_page: str, settings_tab: str = ""
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "catalog",
        "settings_tab": settings_tab,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "csrf_token": get_csrf_token(request),
    }


@legacy_add_ons_router.get("", response_class=HTMLResponse)
def add_ons_legacy_index() -> RedirectResponse:
    return RedirectResponse("/admin/catalog/settings/add-ons", status_code=307)


@legacy_add_ons_router.get("/{path:path}", response_class=HTMLResponse)
def add_ons_legacy_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/catalog/settings/add-ons/{path}", status_code=307)


# =============================================================================
# COMPONENTS OVERVIEW
# =============================================================================


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def catalog_settings_index(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Catalog component overview with cards linking to each section."""
    counts = settings_svc.settings_overview_counts(db)
    context = _base_context(request, db, active_page="catalog-settings")
    context.update(counts)
    return templates.TemplateResponse("admin/catalog/settings/index.html", context)


# =============================================================================
# PLAN CATALOGUES
# =============================================================================


def _system_user_id(request: Request) -> UUID:
    value = str(getattr(getattr(request.state, "user", None), "id", "") or "")
    try:
        return UUID(value)
    except ValueError as exc:
        raise plan_family_catalogues.PlanFamilyCatalogueError(
            code="service_intent.plan_family_catalogues.actor_not_eligible",
            message="An authenticated staff user is required.",
        ) from exc


def _catalogues_context(
    request: Request,
    db: Session,
    *,
    error: str | None = None,
    success: str | None = None,
) -> dict:
    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="catalogues"
    )
    context.update(
        {
            "catalogues": plan_family_catalogues.list_catalogue_options(db),
            "error": error,
            "success": success,
        }
    )
    return context


@router.get(
    "/catalogues",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:read"))],
)
def plan_catalogues_list(
    request: Request,
    status: str | None = Query(default=None),
    message: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "admin/catalog/settings/catalogues.html",
        _catalogues_context(
            request,
            db,
            error=message if status == "error" else None,
            success=message if status == "success" else None,
        ),
    )


@router.post(
    "/catalogues",
    dependencies=[Depends(require_permission("catalog:write"))],
)
def plan_catalogue_publish(
    request: Request,
    plan_family: str = Form(...),
    display_name: str = Form(...),
    description: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    try:
        actor_id = _system_user_id(request)
        file_bytes = file.file.read()
        finish_read_transaction(db)
        outcome = plan_family_catalogues.publish_catalogue(
            db,
            PublishPlanFamilyCatalogueCommand(
                context=CommandContext.system(
                    actor=f"system_user:{actor_id}",
                    scope="catalog:write",
                    reason="publish approved plan-family catalogue PDF",
                    idempotency_key=(f"plan-family-catalogue:{plan_family}:{uuid4()}"),
                ),
                plan_family=plan_family,
                display_name=display_name,
                description=description,
                original_filename=file.filename or "catalogue.pdf",
                content_type=file.content_type,
                file_bytes=file_bytes,
                actor_system_user_id=actor_id,
            ),
        )
    except (plan_family_catalogues.PlanFamilyCatalogueError, ValueError) as exc:
        return templates.TemplateResponse(
            "admin/catalog/settings/catalogues.html",
            _catalogues_context(
                request,
                db,
                error=getattr(exc, "message", str(exc)),
            ),
            status_code=400,
        )
    action = (
        "already current" if outcome.replayed else f"published as v{outcome.version}"
    )
    return RedirectResponse(
        "/admin/catalog/settings/catalogues"
        f"?status=success&message={quote_plus(f'{outcome.display_name} {action}')}",
        status_code=303,
    )


# =============================================================================
# REGION ZONES
# =============================================================================


@router.get("/region-zones", response_class=HTMLResponse)
def region_zones_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List region zones."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_region_zones_paginated(
        db,
        is_active=is_active,
        search=search,
        page=page,
        per_page=per_page,
    )

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="region-zones"
    )
    context.update(
        {
            "zones": result.items,
            "zone_ids": [str(getattr(zone, "id", "")) for zone in result.items],
            "status": status,
            "search": search,
            "page": page,
            "per_page": per_page,
            "total": result.total,
            "total_pages": result.total_pages,
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/region_zones.html", context
    )


@router.get("/region-zones/new", response_class=HTMLResponse)
def region_zone_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create region zone form."""
    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="region-zones"
    )
    context.update(settings_svc.region_zone_form_context(db) or {})
    return templates.TemplateResponse(
        "admin/catalog/settings/region_zone_form.html", context
    )


@router.post(
    "/region-zones",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def region_zone_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Create region zone."""
    zone = settings_svc.parse_region_zone_form(form)
    try:
        with form_write(db):
            settings_svc.create_region_zone_from_form(db, form=form)
        return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="region-zones"
    )
    context.update(
        {
            "zone": zone,
            "error": error,
            "action_url": "/admin/catalog/settings/region-zones",
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/region_zone_form.html", context
    )


@router.get("/region-zones/{zone_id}/edit", response_class=HTMLResponse)
def region_zone_edit(
    request: Request, zone_id: str, db: Session = Depends(get_db)
) -> Response:
    """Edit region zone form."""
    form_context = settings_svc.region_zone_form_context(db, zone_id=zone_id)
    if form_context is None:
        return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="region-zones"
    )
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/catalog/settings/region_zone_form.html", context
    )


@router.post(
    "/region-zones/{zone_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def region_zone_update(
    request: Request,
    zone_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Update region zone."""
    zone = {"id": zone_id, **settings_svc.parse_region_zone_form(form)}
    try:
        with form_write(db):
            settings_svc.update_region_zone_from_form(db, zone_id=zone_id, form=form)
        return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="region-zones"
    )
    context.update(
        {
            "zone": zone,
            "error": error,
            "action_url": f"/admin/catalog/settings/region-zones/{zone_id}/edit",
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/region_zone_form.html", context
    )


@router.post(
    "/region-zones/{zone_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def region_zone_delete(
    request: Request, zone_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete (deactivate) region zone."""
    try:
        settings_svc.delete_region_zone(db, zone_id=zone_id)
    except Exception:
        logger.warning("Failed to delete region zone %s", zone_id, exc_info=True)
    return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)


# =============================================================================
# USAGE ALLOWANCES
# =============================================================================


@router.get("/usage-allowances", response_class=HTMLResponse)
def usage_allowances_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List usage allowances."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_usage_allowances_paginated(
        db,
        is_active=is_active,
        search=search,
        page=page,
        per_page=per_page,
    )

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="usage-allowances"
    )
    context.update(
        {
            "allowances": result.items,
            "status": status,
            "search": search,
            "page": page,
            "per_page": per_page,
            "total": result.total,
            "total_pages": result.total_pages,
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/usage_allowances.html", context
    )


@router.get("/usage-allowances/new", response_class=HTMLResponse)
def usage_allowance_new(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Create usage allowance form."""
    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="usage-allowances"
    )
    context.update(settings_svc.usage_allowance_form_context(db) or {})
    return templates.TemplateResponse(
        "admin/catalog/settings/usage_allowance_form.html", context
    )


@router.post(
    "/usage-allowances",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def usage_allowance_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Create usage allowance."""
    allowance = settings_svc.parse_usage_allowance_form(form)

    try:
        with form_write(db):
            settings_svc.create_usage_allowance_from_form(db, form=form)
        return RedirectResponse(
            "/admin/catalog/settings/usage-allowances", status_code=303
        )
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="usage-allowances"
    )
    context.update(
        {
            "allowance": allowance,
            "error": error,
            "action_url": "/admin/catalog/settings/usage-allowances",
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/usage_allowance_form.html", context
    )


@router.get("/usage-allowances/{allowance_id}/edit", response_class=HTMLResponse)
def usage_allowance_edit(
    request: Request, allowance_id: str, db: Session = Depends(get_db)
) -> Response:
    """Edit usage allowance form."""
    form_context = settings_svc.usage_allowance_form_context(
        db, allowance_id=allowance_id
    )
    if form_context is None:
        return RedirectResponse(
            "/admin/catalog/settings/usage-allowances", status_code=303
        )

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="usage-allowances"
    )
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/catalog/settings/usage_allowance_form.html", context
    )


@router.post(
    "/usage-allowances/{allowance_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def usage_allowance_update(
    request: Request,
    allowance_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Update usage allowance."""
    allowance = {"id": allowance_id, **settings_svc.parse_usage_allowance_form(form)}

    try:
        with form_write(db):
            settings_svc.update_usage_allowance_from_form(
                db, allowance_id=allowance_id, form=form
            )
        return RedirectResponse(
            "/admin/catalog/settings/usage-allowances", status_code=303
        )
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="usage-allowances"
    )
    context.update(
        {
            "allowance": allowance,
            "error": error,
            "action_url": f"/admin/catalog/settings/usage-allowances/{allowance_id}/edit",
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/usage_allowance_form.html", context
    )


@router.post(
    "/usage-allowances/{allowance_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def usage_allowance_delete(
    request: Request, allowance_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete (deactivate) usage allowance."""
    try:
        settings_svc.delete_usage_allowance(db, allowance_id=allowance_id)
    except Exception:
        logger.warning(
            "Failed to delete usage allowance %s", allowance_id, exc_info=True
        )
    return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)


# =============================================================================
# SLA PROFILES
# =============================================================================


@router.get("/sla-profiles", response_class=HTMLResponse)
def sla_profiles_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List SLA profiles."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_sla_profiles_paginated(
        db,
        is_active=is_active,
        search=search,
        page=page,
        per_page=per_page,
    )

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="sla-profiles"
    )
    context.update(
        {
            "profiles": result.items,
            "status": status,
            "search": search,
            "page": page,
            "per_page": per_page,
            "total": result.total,
            "total_pages": result.total_pages,
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/sla_profiles.html", context
    )


@router.get("/sla-profiles/new", response_class=HTMLResponse)
def sla_profile_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create SLA profile form."""
    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="sla-profiles"
    )
    context.update(settings_svc.sla_profile_form_context(db) or {})
    return templates.TemplateResponse(
        "admin/catalog/settings/sla_profile_form.html", context
    )


@router.post(
    "/sla-profiles",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def sla_profile_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Create SLA profile."""
    profile = settings_svc.parse_sla_profile_form(form)

    try:
        with form_write(db):
            settings_svc.create_sla_profile_from_form(db, form=form)
        return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="sla-profiles"
    )
    context.update(
        {
            "profile": profile,
            "error": error,
            "action_url": "/admin/catalog/settings/sla-profiles",
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/sla_profile_form.html", context
    )


@router.get("/sla-profiles/{profile_id}/edit", response_class=HTMLResponse)
def sla_profile_edit(
    request: Request, profile_id: str, db: Session = Depends(get_db)
) -> Response:
    """Edit SLA profile form."""
    form_context = settings_svc.sla_profile_form_context(db, profile_id=profile_id)
    if form_context is None:
        return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="sla-profiles"
    )
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/catalog/settings/sla_profile_form.html", context
    )


@router.post(
    "/sla-profiles/{profile_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def sla_profile_update(
    request: Request,
    profile_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Update SLA profile."""
    profile = {"id": profile_id, **settings_svc.parse_sla_profile_form(form)}

    try:
        with form_write(db):
            settings_svc.update_sla_profile_from_form(
                db, profile_id=profile_id, form=form
            )
        return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="sla-profiles"
    )
    context.update(
        {
            "profile": profile,
            "error": error,
            "action_url": f"/admin/catalog/settings/sla-profiles/{profile_id}/edit",
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/sla_profile_form.html", context
    )


@router.post(
    "/sla-profiles/{profile_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def sla_profile_delete(
    request: Request, profile_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete (deactivate) SLA profile."""
    try:
        settings_svc.delete_sla_profile(db, profile_id=profile_id)
    except Exception:
        logger.warning("Failed to delete SLA profile %s", profile_id, exc_info=True)
    return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)


# =============================================================================
# POLICY SETS (with nested dunning steps)
# =============================================================================


@router.get("/policy-sets", response_class=HTMLResponse)
def policy_sets_list(
    request: Request,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List policy sets."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_policy_sets_paginated(
        db,
        is_active=is_active,
        search=search,
        page=page,
        per_page=per_page,
    )

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="policy-sets"
    )
    context.update(
        {
            "policies": result.items,
            "status": status,
            "search": search,
            "page": page,
            "per_page": per_page,
            "total": result.total,
            "total_pages": result.total_pages,
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/policy_sets.html", context
    )


def _policy_form_context(
    request: Request, db: Session, error: str | None = None
) -> dict:
    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="policy-sets"
    )
    if error:
        context["error"] = error
    return context


@router.get("/policy-sets/new", response_class=HTMLResponse)
def policy_set_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create policy set form."""
    context = _policy_form_context(request, db)
    context.update(settings_svc.policy_set_form_context(db) or {})
    return templates.TemplateResponse(
        "admin/catalog/settings/policy_set_form.html", context
    )


@router.post(
    "/policy-sets",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def policy_set_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Create policy set with dunning steps."""
    policy = settings_svc.parse_policy_set_form(form)

    try:
        with form_write(db):
            settings_svc.create_policy_set_from_form(db, form=form)
        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _policy_form_context(request, db, error)
    context.update(
        {
            "policy": policy,
            "action_url": "/admin/catalog/settings/policy-sets",
            **settings_svc.policy_set_form_options(),
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/policy_set_form.html", context
    )


@router.get("/policy-sets/{policy_id}/edit", response_class=HTMLResponse)
def policy_set_edit(
    request: Request, policy_id: str, db: Session = Depends(get_db)
) -> Response:
    """Edit policy set form."""
    form_context = settings_svc.policy_set_form_context(db, policy_id=policy_id)
    if form_context is None:
        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)

    context = _policy_form_context(request, db)
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/catalog/settings/policy_set_form.html", context
    )


@router.post(
    "/policy-sets/{policy_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def policy_set_update(
    request: Request,
    policy_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Update policy set with dunning steps."""
    policy = {
        "id": policy_id,
        **settings_svc.parse_policy_set_form(form, include_dunning_ids=True),
    }

    try:
        with form_write(db):
            settings_svc.update_policy_set_from_form(db, policy_id=policy_id, form=form)
        return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _policy_form_context(request, db, error)
    context.update(
        {
            "policy": policy,
            "action_url": f"/admin/catalog/settings/policy-sets/{policy_id}/edit",
            **settings_svc.policy_set_form_options(),
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/policy_set_form.html", context
    )


@router.post(
    "/policy-sets/{policy_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def policy_set_delete(
    request: Request, policy_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete (deactivate) policy set."""
    try:
        settings_svc.delete_policy_set(db, policy_id=policy_id)
    except Exception:
        logger.warning("Failed to delete policy set %s", policy_id, exc_info=True)
    return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)


# =============================================================================
# ADD-ONS (with nested prices)
# =============================================================================


@router.get("/add-ons", response_class=HTMLResponse)
def add_ons_list(
    request: Request,
    status: str | None = None,
    addon_type: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List add-ons."""
    is_active = None
    if status == "active":
        is_active = True
    elif status == "inactive":
        is_active = False

    result = settings_svc.list_add_ons_paginated(
        db,
        is_active=is_active,
        addon_type=addon_type,
        search=search,
        page=page,
        per_page=per_page,
    )

    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="add-ons"
    )
    context.update(
        {
            "add_ons": result.items,
            "status": status,
            "addon_type": addon_type,
            "addon_types": settings_svc.add_on_form_options()["addon_types"],
            "search": search,
            "page": page,
            "per_page": per_page,
            "total": result.total,
            "total_pages": result.total_pages,
        }
    )
    return templates.TemplateResponse("admin/catalog/settings/add_ons.html", context)


def _addon_form_context(
    request: Request, db: Session, error: str | None = None
) -> dict:
    context = _base_context(
        request, db, active_page="catalog-settings", settings_tab="add-ons"
    )
    if error:
        context["error"] = error
    return context


@router.get("/add-ons/new", response_class=HTMLResponse)
def add_on_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Create add-on form."""
    context = _addon_form_context(request, db)
    context.update(settings_svc.add_on_form_context(db) or {})
    return templates.TemplateResponse(
        "admin/catalog/settings/add_on_form.html", context
    )


@router.post(
    "/add-ons",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("catalog:write")),
        Depends(require_permission("catalog:billing_write")),
    ],
)
def add_on_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Create add-on with prices."""
    addon = settings_svc.parse_add_on_form(form)

    try:
        with form_write(db):
            settings_svc.create_add_on_from_form(
                db,
                form=form,
                actor_id=_actor_id(request),
            )
        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _addon_form_context(request, db, error)
    context.update(
        {
            "addon": addon,
            "action_url": "/admin/catalog/settings/add-ons",
            **settings_svc.add_on_form_options(),
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/add_on_form.html", context
    )


@router.get("/add-ons/{addon_id}/edit", response_class=HTMLResponse)
def add_on_edit(
    request: Request, addon_id: str, db: Session = Depends(get_db)
) -> Response:
    """Edit add-on form."""
    form_context = settings_svc.add_on_form_context(db, addon_id=addon_id)
    if form_context is None:
        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)

    context = _addon_form_context(request, db)
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/catalog/settings/add_on_form.html", context
    )


@router.post(
    "/add-ons/{addon_id}/edit",
    response_class=HTMLResponse,
    dependencies=[
        Depends(require_permission("catalog:write")),
        Depends(require_permission("catalog:billing_write")),
    ],
)
def add_on_update(
    request: Request,
    addon_id: str,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> Response:
    """Update add-on with prices."""
    addon = {
        "id": addon_id,
        **settings_svc.parse_add_on_form(form, include_price_ids=True),
    }

    try:
        with form_write(db):
            settings_svc.update_add_on_from_form(
                db,
                addon_id=addon_id,
                form=form,
                actor_id=_actor_id(request),
            )
        return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _addon_form_context(request, db, error)
    context.update(
        {
            "addon": addon,
            "action_url": f"/admin/catalog/settings/add-ons/{addon_id}/edit",
            **settings_svc.add_on_form_options(),
        }
    )
    return templates.TemplateResponse(
        "admin/catalog/settings/add_on_form.html", context
    )


@router.post(
    "/add-ons/{addon_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def add_on_delete(
    request: Request, addon_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Delete (deactivate) add-on."""
    try:
        settings_svc.delete_add_on(db, addon_id=addon_id)
    except Exception:
        logger.warning("Failed to delete add-on %s", addon_id, exc_info=True)
    return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)


# =============================================================================
# BULK DELETE OPERATIONS
# =============================================================================


@router.post(
    "/region-zones/bulk-delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_delete_region_zones(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Bulk delete (deactivate) region zones."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_region_zones(db, ids)
    return RedirectResponse("/admin/catalog/settings/region-zones", status_code=303)


@router.post(
    "/usage-allowances/bulk-delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_delete_usage_allowances(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Bulk delete (deactivate) usage allowances."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_usage_allowances(db, ids)
    return RedirectResponse("/admin/catalog/settings/usage-allowances", status_code=303)


@router.post(
    "/sla-profiles/bulk-delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_delete_sla_profiles(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Bulk delete (deactivate) SLA profiles."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_sla_profiles(db, ids)
    return RedirectResponse("/admin/catalog/settings/sla-profiles", status_code=303)


@router.post(
    "/policy-sets/bulk-delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_delete_policy_sets(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Bulk delete (deactivate) policy sets."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_policy_sets(db, ids)
    return RedirectResponse("/admin/catalog/settings/policy-sets", status_code=303)


@router.post(
    "/add-ons/bulk-delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("catalog:write"))],
)
def bulk_delete_add_ons(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Bulk delete (deactivate) add-ons."""
    ids = _form_getlist_str(form, "ids")
    settings_svc.bulk_delete_add_ons(db, ids)
    return RedirectResponse("/admin/catalog/settings/add-ons", status_code=303)


# =============================================================================
# CSV EXPORT OPERATIONS
# =============================================================================


@router.get("/region-zones/export")
def export_region_zones(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export region zones to CSV."""
    csv_content = settings_svc.export_region_zones_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=region_zones.csv"},
    )


@router.get("/usage-allowances/export")
def export_usage_allowances(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export usage allowances to CSV."""
    csv_content = settings_svc.export_usage_allowances_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage_allowances.csv"},
    )


@router.get("/sla-profiles/export")
def export_sla_profiles(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export SLA profiles to CSV."""
    csv_content = settings_svc.export_sla_profiles_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sla_profiles.csv"},
    )


@router.get("/policy-sets/export")
def export_policy_sets(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export policy sets to CSV."""
    csv_content = settings_svc.export_policy_sets_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=policy_sets.csv"},
    )


@router.get("/add-ons/export")
def export_add_ons(db: Session = Depends(get_db)) -> StreamingResponse:
    """Export add-ons to CSV."""
    csv_content = settings_svc.export_add_ons_csv(db)
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=add_ons.csv"},
    )
