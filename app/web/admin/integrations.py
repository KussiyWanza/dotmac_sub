"""Admin integrations routes."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import connector as connector_service
from app.services import integration as integration_service
from app.services import web_connector_runtime as web_connector_runtime_service
from app.services import web_integration_syncs as web_integration_syncs_service
from app.services import web_integrations as web_integrations_service
from app.services import (
    web_integrations_payment_gateways as web_integrations_payment_gateways_service,
)
from app.services import web_integrations_webhooks as webhooks_service
from app.services import web_integrations_whatsapp as web_integrations_whatsapp_service
from app.services.audit_helpers import recent_activity_for_paths
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field import material_catalog
from app.services.integrations import erp_admin as erp_admin_service
from app.services.integrations import installations
from app.services.owner_commands import CommandContext
from app.tasks.dotmac_erp_outbox import refresh_material_catalog

router = APIRouter(prefix="/integrations", tags=["web-admin-integrations"])
templates = Jinja2Templates(directory="templates")


@router.get(
    "/erp-material-catalog",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def erp_material_catalog(
    request: Request,
    notice: str | None = None,
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, active_page="erp-material-catalog")
    context.update(
        {
            "items": material_catalog.list_material_catalog(db, limit=500),
            "warehouses": material_catalog.list_material_warehouses(db),
            "notice": notice,
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/erp_material_catalog.html", context
    )


@router.post(
    "/erp-material-catalog/import",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def import_erp_material_catalog():
    refresh_material_catalog.delay()
    return RedirectResponse(
        "/admin/integrations/erp-material-catalog?notice=ERP+catalogue+import+queued",
        status_code=303,
    )


@router.post("/erp-material-catalog/{item_id}/eligibility")
def set_erp_material_eligibility(
    item_id: UUID,
    eligible: bool = Form(...),
    reason: str = Form("Field operations catalogue review"),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("operations:material_request:write")),
):
    command_id = uuid4()
    actor_id = auth.get("user_id") or auth.get("sub") or "unknown"
    db_session_adapter.release_read_transaction(db)
    material_catalog.set_material_eligibility(
        db,
        material_catalog.SetMaterialEligibility(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{actor_id}",
                scope="operations:material_request:write",
                reason="Set field material eligibility",
                idempotency_key=f"material-eligibility:{item_id}:{eligible}:{command_id}",
            ),
            item_id=item_id,
            eligible=eligible,
            reason=reason,
        ),
    )
    return RedirectResponse(
        "/admin/integrations/erp-material-catalog?notice=Eligibility+updated",
        status_code=303,
    )


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "integrations"
) -> dict:
    """Build base template context."""
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


# ==================== Overview ====================


@router.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def integrations_overview(request: Request, db: Session = Depends(get_db)):
    """Main integrations page with connector inventory and integration actions."""
    state = web_integrations_service.build_connectors_list_data(db)

    context = _base_context(request, db, active_page="integrations")
    context.update(
        {
            **state,
            "page_title": "Integrations",
            "page_subtitle": "Manage integrations, connectors, syncs, and external system access",
            "table_title": "Connectors",
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/connectors/index.html", context
    )


@router.get(
    "/erp",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def erp_connector_config(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="connectors")
    context.update(erp_admin_service.build_config_state(db))
    context["saved"] = request.query_params.get("saved") == "1"
    context["queued"] = request.query_params.get("queued") == "1"
    return templates.TemplateResponse("admin/integrations/erp/config.html", context)


@router.post(
    "/erp",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def erp_connector_config_save(
    request: Request,
    domains: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    auth: dict = Depends(require_permission("system:settings:write")),
):
    actor_id = str(auth.get("user_id") or auth.get("sub") or "unknown")
    command_id = uuid4()
    try:
        selection = erp_admin_service.parse_domain_selection(domains)
        plan = erp_admin_service.review_domain_configuration(
            db,
            selection=selection,
        )
        db_session_adapter.release_read_transaction(db)
        erp_admin_service.configure_domains(
            db,
            plan=plan,
            actor=f"user:{actor_id}",
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{actor_id}",
                scope=installations.CAPABILITY_PROVISIONING_SCOPE,
                reason="Configure DotMac ERP operational synchronization",
                idempotency_key=f"erp-operational-domains:{command_id}",
            ),
        )
    except (DomainError, installations.InstallationError) as exc:
        context = _base_context(request, db, active_page="connectors")
        context.update(erp_admin_service.build_config_state(db))
        context["error"] = str(exc)
        return templates.TemplateResponse(
            "admin/integrations/erp/config.html", context, status_code=400
        )
    return RedirectResponse("/admin/integrations/erp?saved=1", status_code=303)


@router.post(
    "/erp/run",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def erp_connector_run_now():
    from app.tasks.dotmac_erp_outbox import sync_erp_operational_domains

    sync_erp_operational_domains.delay()
    return RedirectResponse("/admin/integrations/erp?queued=1", status_code=303)


# ==================== Runtime posture ====================


@router.get(
    "/runtime",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def integrations_runtime_posture(request: Request, db: Session = Depends(get_db)):
    """Read-only view of each connector's runtime tier and confinement."""
    context = _base_context(request, db, active_page="runtime")
    context.update(web_connector_runtime_service.build_runtime_posture(db))
    context["page_title"] = "Connector runtime"
    context["page_subtitle"] = (
        "Runtime tier, confinement, and executability for every registered connector"
    )
    return templates.TemplateResponse("admin/integrations/runtime.html", context)


# ==================== Syncs ====================


@router.get(
    "/syncs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def syncs_list(
    request: Request,
    direction: str | None = None,
    active: str | None = None,
    db: Session = Depends(get_db),
):
    """Generic sync profiles across external systems."""
    state = web_integration_syncs_service.build_syncs_index_data(
        db,
        direction=direction,
        active=active in ("1", "true", "on", "yes"),
    )
    context = _base_context(request, db, active_page="syncs")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(
                db, ["/admin/integrations/syncs"]
            ),
        }
    )
    return templates.TemplateResponse("admin/integrations/syncs/index.html", context)


@router.get(
    "/syncs/{job_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def sync_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Sync profile detail and run history."""
    try:
        state = web_integration_syncs_service.build_sync_detail_data(db, job_id)
    except ValueError:
        context = _base_context(request, db, active_page="syncs")
        context["message"] = "The sync profile you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )
    context = _base_context(request, db, active_page="syncs")
    context.update(state)
    return templates.TemplateResponse("admin/integrations/syncs/detail.html", context)


@router.post(
    "/syncs/{job_id}/run",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def sync_run(job_id: str, db: Session = Depends(get_db)):
    """Queue a manual sync run (disabled jobs are refused)."""
    job = integration_service.integration_jobs.get(db, job_id)
    if not job.is_active:
        return RedirectResponse(
            url=f"/admin/integrations/syncs/{job_id}?error=disabled", status_code=303
        )
    web_integration_syncs_service.trigger_sync_job(job_id)
    return RedirectResponse(
        url=f"/admin/integrations/syncs/{job_id}?queued=1", status_code=303
    )


@router.post(
    "/syncs/{job_id}/configure",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def sync_configure(
    job_id: str,
    schedule_type: str = Form("manual"),
    interval_minutes: str | None = Form(None),
    trigger_mode: str | None = Form(None),
    mapping_config: str | None = Form(None),
    filter_config: str | None = Form(None),
    page_size: str | None = Form(None),
    max_pages: str | None = Form(None),
    sync_comments: bool = Form(False),
    mapping_primary: str | None = Form(None),
    mapping_fallback: str | None = Form(None),
    mapping_ambiguous: str | None = Form(None),
    conflict_policy: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    web_integration_syncs_service.update_sync_profile(
        db,
        job_id,
        schedule_type=schedule_type,
        interval_minutes=interval_minutes,
        trigger_mode=trigger_mode,
        mapping_config=mapping_config,
        filter_config=filter_config,
        page_size=page_size,
        max_pages=max_pages,
        sync_comments=sync_comments,
        mapping_primary=mapping_primary,
        mapping_fallback=mapping_fallback,
        mapping_ambiguous=mapping_ambiguous,
        conflict_policy=conflict_policy,
        is_active=is_active,
    )
    return RedirectResponse(
        url=f"/admin/integrations/syncs/{job_id}?saved=1", status_code=303
    )


@router.post(
    "/syncs/{job_id}/backfill-crm-history",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def sync_backfill_crm_history(job_id: str, db: Session = Depends(get_db)):
    web_integration_syncs_service.backfill_crm_ticket_import_history(db, job_id)
    return RedirectResponse(
        url=f"/admin/integrations/syncs/{job_id}?backfilled=1", status_code=303
    )


@router.get(
    "/connectors",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def connectors_list(request: Request, db: Session = Depends(get_db)):
    """List all connector configurations."""
    state = web_integrations_service.build_connectors_list_data(db)

    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/connectors/index.html", context
    )


@router.get("/marketplace", response_class=HTMLResponse)
def integrations_marketplace(request: Request, db: Session = Depends(get_db)):
    """Integrations marketplace with discovered and installed connectors."""
    state = web_integrations_service.build_marketplace_data(db)
    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            **state,
            "updates_checked": request.query_params.get("checked") == "1",
        }
    )
    return templates.TemplateResponse("admin/integrations/marketplace.html", context)


@router.post(
    "/marketplace/check-updates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_marketplace_check_updates():
    return RedirectResponse(
        "/admin/integrations/marketplace?checked=1", status_code=303
    )


@router.get("/installed", response_class=HTMLResponse)
def integrations_installed(request: Request, db: Session = Depends(get_db)):
    state = web_integrations_service.build_installed_integrations_data(db)
    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            **state,
            "saved": request.query_params.get("saved") == "1",
        }
    )
    return templates.TemplateResponse("admin/integrations/installed.html", context)


@router.post(
    "/installed/bulk",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_installed_bulk(
    connector_ids: list[str] = Form(default=[]),
    action: str = Form(...),
    db: Session = Depends(get_db),
):
    if connector_ids:
        web_integrations_service.bulk_set_integrations_enabled(
            db,
            connector_ids=connector_ids,
            enabled=(action == "enable"),
        )
    return RedirectResponse("/admin/integrations/installed?saved=1", status_code=303)


@router.post(
    "/installed/{connector_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_installed_toggle(
    connector_id: str,
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    web_integrations_service.bulk_set_integrations_enabled(
        db,
        connector_ids=[connector_id],
        enabled=enabled,
    )
    return RedirectResponse("/admin/integrations/installed?saved=1", status_code=303)


@router.post(
    "/installed/{connector_id}/uninstall",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def integrations_installed_uninstall(connector_id: str, db: Session = Depends(get_db)):
    web_integrations_service.uninstall_integration(db, connector_id)
    return RedirectResponse("/admin/integrations/installed?saved=1", status_code=303)


@router.get("/connectors/new", response_class=HTMLResponse)
def connector_new(request: Request, db: Session = Depends(get_db)):
    """New connector form."""
    context = _base_context(request, db, active_page="connectors")
    context.update(web_integrations_service.connector_form_options())
    return templates.TemplateResponse("admin/integrations/connectors/new.html", context)


@router.post(
    "/connectors",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def connector_create(
    request: Request,
    name: str = Form(...),
    connector_type: str = Form("custom"),
    auth_type: str = Form("none"),
    base_url: str | None = Form(None),
    timeout_sec: str | None = Form(None),
    auth_config: str | None = Form(None),
    headers: str | None = Form(None),
    retry_policy: str | None = Form(None),
    metadata: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        connector = web_integrations_service.create_connector(
            db,
            name=name,
            connector_type=connector_type,
            auth_type=auth_type,
            base_url=base_url,
            timeout_sec=timeout_sec,
            auth_config=auth_config,
            headers=headers,
            retry_policy=retry_policy,
            metadata=metadata,
            notes=notes,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="connectors")
        context.update(
            {
                **web_integrations_service.connector_error_state(
                    name=name,
                    connector_type=connector_type,
                    auth_type=auth_type,
                    base_url=base_url,
                    timeout_sec=timeout_sec,
                    auth_config=auth_config,
                    headers=headers,
                    retry_policy=retry_policy,
                    metadata=metadata,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/connectors/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/connectors/{connector.id}", status_code=303
    )


@router.get("/connectors/{connector_id}", response_class=HTMLResponse)
def connector_detail(
    request: Request, connector_id: str, db: Session = Depends(get_db)
):
    """Connector detail view."""
    try:
        connector = connector_service.connector_configs.get(db, connector_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="connectors")
        context["message"] = "The connector you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="connectors")
    context.update(
        {
            "connector": connector,
            # Secret-keyed header/metadata values are masked for display; the
            # update path restores them unless overwritten (see web_integrations).
            "headers_display": web_integrations_service.mask_secret_values(
                connector.headers
            ),
            "metadata_display": web_integrations_service.mask_secret_values(
                connector.metadata_
            ),
            **web_integrations_service.connector_form_options(),
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/connectors/detail.html", context
    )


@router.post(
    "/connectors/{connector_id}",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def connector_update(
    connector_id: str,
    base_url: str | None = Form(None),
    auth_type: str = Form("none"),
    timeout_sec: str | None = Form(None),
    auth_config: str | None = Form(None),
    headers: str | None = Form(None),
    retry_policy: str | None = Form(None),
    metadata: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    web_integrations_service.update_connector_config(
        db,
        connector_id,
        base_url=base_url,
        auth_type=auth_type,
        timeout_sec=timeout_sec,
        auth_config=auth_config,
        headers=headers,
        retry_policy=retry_policy,
        metadata=metadata,
        notes=notes,
        is_active=is_active,
    )
    return RedirectResponse(
        url=f"/admin/integrations/connectors/{connector_id}?saved=1", status_code=303
    )


@router.get("/connectors/{connector_id}/embed", response_class=HTMLResponse)
def connector_embed(request: Request, connector_id: str, db: Session = Depends(get_db)):
    """Embedded connector frame view with reload/open controls."""
    perform_check = request.query_params.get("check") == "1"
    try:
        state = web_integrations_service.build_embedded_connector_data(
            db,
            connector_id=connector_id,
            perform_check=perform_check,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="connectors")
        context["message"] = "The connector you are trying to embed does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="connectors")
    context.update(state)
    return templates.TemplateResponse(
        "admin/integrations/connectors/embed.html", context
    )


@router.get("/targets", response_class=HTMLResponse)
def targets_list(request: Request, db: Session = Depends(get_db)):
    """List all integration targets."""
    state = web_integrations_service.build_targets_list_data(db)

    context = _base_context(request, db, active_page="targets")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/targets/index.html", context)


@router.get("/targets/new", response_class=HTMLResponse)
def target_new(request: Request, db: Session = Depends(get_db)):
    """New target form."""
    context = _base_context(request, db, active_page="targets")
    context.update(web_integrations_service.target_form_options(db))
    return templates.TemplateResponse("admin/integrations/targets/new.html", context)


@router.post(
    "/targets",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def target_create(
    request: Request,
    name: str = Form(...),
    target_type: str = Form("custom"),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        target = web_integrations_service.create_target(
            db,
            name=name,
            target_type=target_type,
            notes=notes,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="targets")
        context.update(
            {
                **web_integrations_service.target_error_state(
                    db,
                    name=name,
                    target_type=target_type,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/targets/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/targets/{target.id}", status_code=303
    )


@router.get("/targets/{target_id}", response_class=HTMLResponse)
def target_detail(request: Request, target_id: str, db: Session = Depends(get_db)):
    """Target detail view."""
    try:
        target = integration_service.integration_targets.get(db, target_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="targets")
        context["message"] = (
            "The integration target you are looking for does not exist."
        )
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="targets")
    context.update({"target": target})
    return templates.TemplateResponse("admin/integrations/targets/detail.html", context)


# ==================== Integration Jobs ====================


@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, db: Session = Depends(get_db)):
    """List all integration jobs."""
    state = web_integrations_service.build_jobs_list_data(db)

    context = _base_context(request, db, active_page="jobs")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/jobs/index.html", context)


@router.get("/jobs/new", response_class=HTMLResponse)
def job_new(request: Request, db: Session = Depends(get_db)):
    """New job form."""
    context = _base_context(request, db, active_page="jobs")
    context.update(web_integrations_service.job_form_options(db))
    return templates.TemplateResponse("admin/integrations/jobs/new.html", context)


@router.post(
    "/jobs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def job_create(
    request: Request,
    target_id: str = Form(...),
    name: str = Form(...),
    job_type: str = Form("sync"),
    schedule_type: str = Form("manual"),
    interval_minutes: str | None = Form(None),
    capability_binding_id: str = Form(...),
    entity_type: str | None = Form(None),
    direction: str | None = Form(None),
    trigger_mode: str | None = Form(None),
    mapping_config: str | None = Form(None),
    filter_config: str | None = Form(None),
    conflict_policy: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        job = web_integrations_service.create_job(
            db,
            target_id=target_id,
            name=name,
            job_type=job_type,
            schedule_type=schedule_type,
            interval_minutes=interval_minutes,
            capability_binding_id=capability_binding_id,
            entity_type=entity_type,
            direction=direction,
            trigger_mode=trigger_mode,
            mapping_config=mapping_config,
            filter_config=filter_config,
            conflict_policy=conflict_policy,
            notes=notes,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="jobs")
        context.update(
            {
                **web_integrations_service.job_error_state(
                    db,
                    target_id=target_id,
                    name=name,
                    job_type=job_type,
                    schedule_type=schedule_type,
                    interval_minutes=interval_minutes,
                    capability_binding_id=capability_binding_id,
                    entity_type=entity_type,
                    direction=direction,
                    trigger_mode=trigger_mode,
                    mapping_config=mapping_config,
                    filter_config=filter_config,
                    conflict_policy=conflict_policy,
                    notes=notes,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/jobs/new.html", context, status_code=400
        )
    return RedirectResponse(url=f"/admin/integrations/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str, db: Session = Depends(get_db)):
    """Job detail view with run history."""
    try:
        job = integration_service.integration_jobs.get(db, job_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="jobs")
        context["message"] = "The integration job you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    runs = integration_service.integration_runs.list(
        db=db,
        job_id=str(job.id),
        status=None,
        order_by="started_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    context = _base_context(request, db, active_page="jobs")
    context.update({"job": job, "runs": runs})
    return templates.TemplateResponse("admin/integrations/jobs/detail.html", context)


@router.post(
    "/jobs/{job_id}/run",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def job_run(job_id: str, db: Session = Depends(get_db)):
    """Queue a manual integration job run (disabled jobs are refused)."""
    job = integration_service.integration_jobs.get(db, job_id)
    if not job.is_active:
        return RedirectResponse(
            url=f"/admin/integrations/jobs/{job_id}?error=disabled", status_code=303
        )
    web_integration_syncs_service.trigger_sync_job(job_id)
    return RedirectResponse(
        url=f"/admin/integrations/jobs/{job_id}?queued=1", status_code=303
    )


# ==================== Webhooks ====================


@router.get("/webhooks", response_class=HTMLResponse)
def webhooks_list(request: Request, db: Session = Depends(get_db)):
    """List all webhook endpoints."""
    state = webhooks_service.build_webhooks_list_data(db)

    context = _base_context(request, db, active_page="webhooks")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(db, ["/admin/integrations"]),
        }
    )
    return templates.TemplateResponse("admin/integrations/webhooks/index.html", context)


@router.get("/webhooks/new", response_class=HTMLResponse)
def webhook_new(request: Request, db: Session = Depends(get_db)):
    """New webhook endpoint form."""
    context = _base_context(request, db, active_page="webhooks")
    context.update(webhooks_service.webhook_form_options(db))
    context.update(
        {
            "action_url": "/admin/integrations/webhooks",
            "submit_label": "Create Webhook",
        }
    )
    return templates.TemplateResponse("admin/integrations/webhooks/new.html", context)


@router.post(
    "/webhooks",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    signing_secret_ref: str | None = Form(None),
    authorization_ref: str | None = Form(None),
    event_types: list[str] | None = Form(None),
    delivery_timeout_seconds: str | None = Form(None),
    max_retries: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        endpoint = webhooks_service.create_webhook_endpoint(
            db,
            name=name,
            url=url,
            signing_secret_ref=signing_secret_ref,
            authorization_ref=authorization_ref,
            event_types=event_types,
            delivery_timeout_seconds=delivery_timeout_seconds,
            max_retries=max_retries,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="webhooks")
        context.update(
            {
                **webhooks_service.webhook_error_state(
                    db,
                    name=name,
                    url=url,
                    signing_secret_ref=signing_secret_ref,
                    authorization_ref=authorization_ref,
                    event_types=event_types,
                    delivery_timeout_seconds=delivery_timeout_seconds,
                    max_retries=max_retries,
                    is_active=is_active,
                ),
                "error": str(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/webhooks/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint.id}", status_code=303
    )


@router.get("/webhooks/{endpoint_id}/edit", response_class=HTMLResponse)
def webhook_edit(request: Request, endpoint_id: str, db: Session = Depends(get_db)):
    """Edit webhook endpoint form."""
    try:
        state = webhooks_service.build_webhook_edit_data(
            db,
            endpoint_id=endpoint_id,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="webhooks")
        context["message"] = "The webhook endpoint you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )
    context = _base_context(request, db, active_page="webhooks")
    context.update(state)
    return templates.TemplateResponse("admin/integrations/webhooks/new.html", context)


@router.post(
    "/webhooks/{endpoint_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_update(
    request: Request,
    endpoint_id: str,
    name: str = Form(...),
    url: str = Form(...),
    signing_secret_ref: str | None = Form(None),
    authorization_ref: str | None = Form(None),
    event_types: list[str] | None = Form(None),
    delivery_timeout_seconds: str | None = Form(None),
    max_retries: str | None = Form(None),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        endpoint = webhooks_service.update_webhook_endpoint(
            db,
            endpoint_id=endpoint_id,
            name=name,
            url=url,
            signing_secret_ref=signing_secret_ref,
            authorization_ref=authorization_ref,
            event_types=event_types,
            delivery_timeout_seconds=delivery_timeout_seconds,
            max_retries=max_retries,
            is_active=is_active,
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="webhooks")
        context.update(
            {
                **webhooks_service.webhook_error_state(
                    db,
                    name=name,
                    url=url,
                    signing_secret_ref=signing_secret_ref,
                    authorization_ref=authorization_ref,
                    event_types=event_types,
                    delivery_timeout_seconds=delivery_timeout_seconds,
                    max_retries=max_retries,
                    is_active=is_active,
                ),
                "endpoint": None,
                "action_url": f"/admin/integrations/webhooks/{endpoint_id}",
                "submit_label": "Save Webhook",
                "error": str(exc),
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/webhooks/new.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint.id}?saved=1", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/enable",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_enable(endpoint_id: str, db: Session = Depends(get_db)):
    webhooks_service.set_webhook_endpoint_active(
        db, endpoint_id=endpoint_id, is_active=True
    )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint_id}?saved=1", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/disable",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_disable(endpoint_id: str, db: Session = Depends(get_db)):
    webhooks_service.set_webhook_endpoint_active(
        db, endpoint_id=endpoint_id, is_active=False
    )
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint_id}?saved=1", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/test",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_test(endpoint_id: str, db: Session = Depends(get_db)):
    try:
        webhooks_service.queue_webhook_test_delivery(db, endpoint_id=endpoint_id)
        query = "test=queued"
    except Exception:
        query = "test=failed"
    return RedirectResponse(
        url=f"/admin/integrations/webhooks/{endpoint_id}?{query}", status_code=303
    )


@router.post(
    "/webhooks/{endpoint_id}/delete",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def webhook_delete(endpoint_id: str, db: Session = Depends(get_db)):
    webhooks_service.delete_webhook_endpoint(db, endpoint_id=endpoint_id)
    return RedirectResponse(
        url="/admin/integrations/webhooks?deleted=1", status_code=303
    )


@router.get("/webhooks/{endpoint_id}", response_class=HTMLResponse)
def webhook_detail(request: Request, endpoint_id: str, db: Session = Depends(get_db)):
    """Webhook endpoint detail view."""
    try:
        state = webhooks_service.build_webhook_detail_data(
            db,
            endpoint_id=endpoint_id,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        context = _base_context(request, db, active_page="webhooks")
        context["message"] = "The webhook endpoint you are looking for does not exist."
        return templates.TemplateResponse(
            "admin/errors/404.html", context, status_code=404
        )

    context = _base_context(request, db, active_page="webhooks")
    context.update(state)
    return templates.TemplateResponse(
        "admin/integrations/webhooks/detail.html", context
    )


# ==================== Payment Gateways ====================


def _payment_gateway_config_response(
    request: Request,
    db: Session,
    *,
    provider_type: str,
    error: str | None = None,
    status_code: int = 200,
):
    state = web_integrations_payment_gateways_service.build_config_state(
        db, provider_type
    )
    context = _base_context(request, db, active_page="payment-gateways")
    context.update(
        {
            **state,
            "error": error,
            "recent_activities": recent_activity_for_paths(
                db, [f"/admin/integrations/payment-gateways/{provider_type}"]
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/payment_gateways/config.html",
        context,
        status_code=status_code,
    )


@router.get(
    "/payment-gateways/{provider_type}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def payment_gateway_config_page(
    request: Request,
    provider_type: str,
    db: Session = Depends(get_db),
):
    try:
        return _payment_gateway_config_response(
            request, db, provider_type=provider_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/payment-gateways/{provider_type}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def payment_gateway_config_save(
    request: Request,
    provider_type: str,
    presentment_priority: int = Form(0),
    gateway_credentials: str = Form(""),
    public_key: str = Form(""),
    webhook_signing_secret: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        installations.execute_command(
            db,
            lambda: web_integrations_payment_gateways_service.save_config(
                db,
                provider_type_value=provider_type,
                presentment_priority=presentment_priority,
                gateway_credentials=gateway_credentials,
                public_key=public_key,
                webhook_signing_secret=webhook_signing_secret,
            ),
        )
    except Exception as exc:
        return _payment_gateway_config_response(
            request,
            db,
            provider_type=provider_type,
            error=str(exc),
            status_code=400,
        )
    return RedirectResponse(
        url=(f"/admin/integrations/payment-gateways/{provider_type}?saved=1"),
        status_code=303,
    )


@router.post(
    "/payment-gateways/{provider_type}/enable",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def payment_gateway_enable(
    request: Request,
    provider_type: str,
    confirm_enable: bool = Form(False),
    db: Session = Depends(get_db),
):
    if not confirm_enable:
        return _payment_gateway_config_response(
            request,
            db,
            provider_type=provider_type,
            error=(
                "Confirm that successful validation may expose this gateway "
                "for new customer and reseller checkouts."
            ),
            status_code=400,
        )
    try:
        installations.execute_command(
            db,
            lambda: web_integrations_payment_gateways_service.validate_and_enable(
                db, provider_type_value=provider_type
            ),
        )
    except Exception as exc:
        return _payment_gateway_config_response(
            request,
            db,
            provider_type=provider_type,
            error=str(exc),
            status_code=400,
        )
    return RedirectResponse(
        url=(f"/admin/integrations/payment-gateways/{provider_type}?enabled=1"),
        status_code=303,
    )


@router.post(
    "/payment-gateways/{provider_type}/disable",
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def payment_gateway_disable(
    request: Request,
    provider_type: str,
    confirm_disable: bool = Form(False),
    db: Session = Depends(get_db),
):
    if not confirm_disable:
        return _payment_gateway_config_response(
            request,
            db,
            provider_type=provider_type,
            error=(
                "Confirm that this gateway should stop accepting new checkouts. "
                "Webhook, reconciliation, and refund processing will remain enabled."
            ),
            status_code=400,
        )
    try:
        installations.execute_command(
            db,
            lambda: web_integrations_payment_gateways_service.disable(
                db, provider_type_value=provider_type
            ),
        )
    except Exception as exc:
        return _payment_gateway_config_response(
            request,
            db,
            provider_type=provider_type,
            error=str(exc),
            status_code=400,
        )
    return RedirectResponse(
        url=(f"/admin/integrations/payment-gateways/{provider_type}?disabled=1"),
        status_code=303,
    )


@router.get("/whatsapp/config", response_class=HTMLResponse)
def whatsapp_config_page(request: Request, db: Session = Depends(get_db)):
    state = web_integrations_whatsapp_service.build_config_state(db)
    context = _base_context(request, db, active_page="whatsapp")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(
                db, ["/admin/integrations/whatsapp"]
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/integrations/whatsapp/config.html", context
    )


@router.post(
    "/whatsapp/config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def whatsapp_config_save(
    request: Request,
    provider: str = Form("meta_cloud_api"),
    phone_number: str = Form(""),
    phone_number_id: str = Form(""),
    waba_id: str = Form(""),
    webhook_url: str = Form(""),
    graph_version: str = Form("v21.0"),
    api_key: str = Form(""),
    api_secret: str = Form(""),
    webhook_verify_token: str = Form(""),
    message_templates_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    try:
        installations.execute_command(
            db,
            lambda: web_integrations_whatsapp_service.save_config(
                db,
                provider=provider,
                phone_number=phone_number,
                phone_number_id=phone_number_id,
                waba_id=waba_id,
                webhook_url=webhook_url,
                graph_version=graph_version,
                api_key=api_key,
                api_secret=api_secret,
                webhook_verify_token=webhook_verify_token,
                message_templates_json=message_templates_json,
            ),
        )
        return RedirectResponse(
            url="/admin/integrations/whatsapp/config?saved=1", status_code=303
        )
    except Exception as exc:
        state = web_integrations_whatsapp_service.build_config_state(db)
        context = _base_context(request, db, active_page="whatsapp")
        context.update(
            {
                **state,
                "recent_activities": recent_activity_for_paths(
                    db, ["/admin/integrations/whatsapp"]
                ),
                "error": str(exc),
                "form": {
                    **state.get("form", {}),
                    "provider": provider,
                    "phone_number": phone_number,
                    "phone_number_id": phone_number_id,
                    "waba_id": waba_id,
                    "webhook_url": webhook_url,
                    "graph_version": graph_version,
                    "message_templates_json": message_templates_json,
                },
            }
        )
        return templates.TemplateResponse(
            "admin/integrations/whatsapp/config.html",
            context,
            status_code=400,
        )


@router.post(
    "/whatsapp/config/test-send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def whatsapp_config_test_send(
    request: Request,
    test_recipient: str = Form(""),
    test_template_name: str = Form(""),
    test_variables_json: str = Form("{}"),
    db: Session = Depends(get_db),
):
    state = web_integrations_whatsapp_service.build_config_state(db)
    context = _base_context(request, db, active_page="whatsapp")
    context.update(
        {
            **state,
            "recent_activities": recent_activity_for_paths(
                db, ["/admin/integrations/whatsapp"]
            ),
        }
    )
    try:
        result = web_integrations_whatsapp_service.run_test_send(
            db,
            recipient=test_recipient,
            template_name=test_template_name,
            variables_json=test_variables_json,
        )
        context["test_result"] = result
    except Exception as exc:
        context["error"] = str(exc)
    return templates.TemplateResponse(
        "admin/integrations/whatsapp/config.html", context
    )
