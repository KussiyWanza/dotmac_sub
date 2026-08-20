"""Admin network core devices web routes."""

from typing import Literal
from urllib.parse import quote_plus
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import core_device_archive
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import has_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

_format_duration = web_network_core_runtime_service.format_duration
_format_bps = web_network_core_runtime_service.format_bps


def _render_template_fragment(template_name: str, context: dict) -> str:
    return templates.env.get_template(template_name).render(context)


def _coerce_uuid_or_none(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _archive_command_context(auth: dict[str, object], *, reason: str) -> CommandContext:
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        raise ValueError("Authorized actor identity is missing")
    actor_type = "api_key" if auth.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=core_device_archive.ARCHIVE_SCOPE,
        reason=reason.strip(),
        idempotency_key=f"core-device-lifecycle:{command_id}",
    )


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    auth = getattr(request.state, "auth", None) or {}
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "can_write_network": bool(auth)
        and has_permission(auth, db, "network:device:write"),
        "can_archive_network_device": bool(auth)
        and has_permission(auth, db, core_device_archive.ARCHIVE_SCOPE),
    }


@router.get(
    "/network-devices",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def network_devices_consolidated(
    tab: str = "core",
    search: str | None = None,
):
    """Folded into the unified device ledger (docs/design/NETWORK_IA_RATIONALIZATION.md).

    role/site are now projected as class_facts and the projected operational
    status supersedes this monitoring list; granular live ping/SNMP/board detail
    lives on the device detail. This route now redirects to the one ledger.
    """
    type_map = {"core": "core", "olts": "olt", "onts": "ont"}
    target = "/admin/network/devices?type=" + type_map.get(tab, "core")
    if search:
        target += "&search=" + quote_plus(search)
    return RedirectResponse(target, status_code=307)


@router.get(
    "/backups",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def network_backups_overview(
    request: Request,
    status: str | None = None,
    device_type: str | None = None,
    search: str | None = None,
    stale_hours: int = 24,
    sort: str = "last_backup_asc",
    sort_dir: Literal["asc", "desc"] | None = Query(None, alias="dir"),
    page: int = Query(1, ge=1),
    per_page: Literal[10, 25, 50, 100] = 25,
    db: Session = Depends(get_db),
):
    """Global backup status page across NAS and OLT devices."""
    query = web_network_core_devices_service.build_backup_overview_query(
        status=status,
        device_type=device_type,
        search=search,
        stale_hours=stale_hours,
        sort=sort,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
    )
    page_data = web_network_core_devices_service.backup_overview_page_data(
        db=db,
        query=query,
    )
    context = _base_context(request, db, active_page="network-backups")
    context.update(
        {
            "rows": page_data.rows,
            "stats": page_data.stats,
            "list_query": page_data.query.list_query,
            "page_meta": page_data.page_meta,
            "status_filter": page_data.query.status,
            "device_type_filter": page_data.query.device_type,
            "search_filter": page_data.query.list_query.search or "",
            "stale_hours": page_data.query.stale_hours,
            "sort_filter": page_data.query.sort_filter,
        }
    )
    return templates.TemplateResponse("admin/network/backups/index.html", context)


@router.get(
    "/core-devices",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_devices_list(
    request: Request,
    role: str | None = None,
    device_type: str | None = None,
    status: str | None = None,
    pop_site_id: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    """List core network devices (routers, switches, access points, etc.)."""
    page_data = web_network_core_devices_service.list_page_data(
        db,
        role,
        status,
        device_type=device_type,
        pop_site_id=pop_site_id,
        search=search,
    )
    refresh_param = (request.query_params.get("refresh") or "").strip().lower()
    force_refresh = refresh_param in {"1", "true", "yes", "on"}
    refresh_summary: dict[str, int] | None = None
    if force_refresh:
        refresh_summary = {"skipped": 1}
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context["refresh_summary"] = refresh_summary
    context.update(page_data)
    return templates.TemplateResponse("admin/network/core-devices/index.html", context)


@router.get(
    "/core-devices/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_new(
    request: Request,
    pop_site_id: str | None = None,
    db: Session = Depends(get_db),
):
    selected_pop_site_uuid = _coerce_uuid_or_none(pop_site_id)
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        pop_site_id=selected_pop_site_uuid,
    )
    form_context = web_network_core_devices_service.build_form_context(
        device=None,
        pop_sites=pop_sites,
        parent_devices=parent_devices,
        selected_pop_site_id=pop_site_id if selected_pop_site_uuid else None,
        current_device_id=None,
        action_url="/admin/network/core-devices",
    )
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(form_context)
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.post(
    "/core-devices",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    values = web_network_core_devices_service.parse_form_values(form)
    selected_pop_site_id = (
        str(values.get("pop_site_id")) if values.get("pop_site_id") else None
    )
    selected_pop_site_uuid = _coerce_uuid_or_none(selected_pop_site_id)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        pop_site_id=selected_pop_site_uuid,
    )
    normalized, error = web_network_core_devices_service.validate_values(db, values)
    if error:
        snapshot = web_network_core_devices_service.snapshot_for_form(values)
        form_context = web_network_core_devices_service.build_form_context(
            device=snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=None,
            action_url="/admin/network/core-devices",
            error=error,
        )
        context = _base_context(
            request, db, active_page="core-devices", active_menu="core-network"
        )
        context.update(form_context)
        return templates.TemplateResponse(
            "admin/network/core-devices/form.html", context
        )

    if normalized is None:
        form_context = web_network_core_devices_service.build_form_context(
            device=snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=None,
            action_url="/admin/network/core-devices",
            error="Please correct the highlighted fields.",
        )
        context = _base_context(
            request, db, active_page="core-devices", active_menu="core-network"
        )
        context.update(form_context)
        return templates.TemplateResponse(
            "admin/network/core-devices/form.html", context
        )
    result = web_network_core_devices_service.create_device(db, normalized)
    if result.error:
        form_context = web_network_core_devices_service.build_form_context(
            device=result.snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=None,
            action_url="/admin/network/core-devices",
            error=result.error,
        )
        context = _base_context(
            request, db, active_page="core-devices", active_menu="core-network"
        )
        context.update(form_context)
        return templates.TemplateResponse(
            "admin/network/core-devices/form.html", context
        )
    device = result.device
    if device is None:
        form_context = web_network_core_devices_service.build_form_context(
            device=result.snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=None,
            action_url="/admin/network/core-devices",
            error="Failed to create device",
        )
        context = _base_context(
            request, db, active_page="core-devices", active_menu="core-network"
        )
        context.update(form_context)
        return templates.TemplateResponse(
            "admin/network/core-devices/form.html", context
        )

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="core_device",
        entity_id=str(device.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": device.name, "mgmt_ip": device.mgmt_ip or None},
    )
    target_url = f"/admin/network/core-devices/{device.id}"
    if result.warning:
        target_url = f"{target_url}?error={quote_plus(result.warning)}"
    return RedirectResponse(target_url, status_code=303)


@router.get(
    "/core-devices/parent-options",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_parent_options(
    request: Request,
    pop_site_id: str | None = None,
    current_device_id: str | None = None,
    selected_parent_id: str | None = None,
    parent_device_id: str | None = None,
    db: Session = Depends(get_db),
):
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        current_device_id=_coerce_uuid_or_none(current_device_id),
        pop_site_id=_coerce_uuid_or_none(pop_site_id),
    )
    return templates.TemplateResponse(
        "admin/network/core-devices/_parent_options.html",
        {
            "request": request,
            "parent_devices": parent_devices,
            "selected_parent_id": selected_parent_id or parent_device_id,
        },
    )


@router.get(
    "/core-devices/{device_id}/archive/preview",
    response_class=HTMLResponse,
)
def core_device_archive_preview(
    request: Request,
    device_id: UUID,
    db: Session = Depends(get_db),
    _auth: dict[str, object] = Depends(
        require_permission(core_device_archive.ARCHIVE_SCOPE)
    ),
) -> HTMLResponse:
    """Render owner-resolved archive impact and confirmation."""
    try:
        preview = core_device_archive.preview_core_device_archive(
            db,
            core_device_archive.PreviewCoreDeviceArchiveRequest(device_id=device_id),
        )
        error = None
    except DomainError as exc:
        preview = None
        error = exc.message
    return templates.TemplateResponse(
        "admin/network/core-devices/_archive_preview.html",
        {
            "request": request,
            "device_id": device_id,
            "preview": preview,
            "error": error,
        },
        status_code=200 if preview is not None else 409,
    )


@router.post("/core-devices/{device_id}/archive")
def core_device_archive_execute(
    device_id: UUID,
    reason: str = Form(...),
    preview_fingerprint: str = Form(...),
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(core_device_archive.ARCHIVE_SCOPE)
    ),
) -> RedirectResponse:
    context = _archive_command_context(auth, reason=reason)
    try:
        db_session_adapter.release_read_transaction(db)
        outcome = core_device_archive.archive_core_device(
            db,
            core_device_archive.ArchiveCoreDeviceCommand(
                context=context,
                device_id=device_id,
                expected_preview_fingerprint=(
                    core_device_archive.ArchivePreviewFingerprint.parse(
                        preview_fingerprint
                    )
                ),
            ),
        )
    except DomainError as exc:
        return RedirectResponse(
            f"/admin/network/core-devices/{device_id}?error={quote_plus(exc.message)}",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/network/core-devices/{outcome.device_id}?message="
        + quote_plus(f"{outcome.device_name} decommissioned."),
        status_code=303,
    )


@router.post("/core-devices/{device_id}/restore")
def core_device_restore_execute(
    device_id: UUID,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(core_device_archive.ARCHIVE_SCOPE)
    ),
) -> RedirectResponse:
    context = _archive_command_context(
        auth,
        reason="Restore decommissioned core device to inactive inventory",
    )
    try:
        db_session_adapter.release_read_transaction(db)
        outcome = core_device_archive.restore_core_device(
            db,
            core_device_archive.RestoreCoreDeviceCommand(
                context=context,
                device_id=device_id,
            ),
        )
    except DomainError as exc:
        return RedirectResponse(
            "/admin/network/devices?type=core&lifecycle=archived&error="
            + quote_plus(exc.message),
            status_code=303,
        )
    message = (
        f"{outcome.device_name} was not decommissioned; no changes were made."
        if outcome.replayed
        else f"{outcome.device_name} restored as inactive."
    )
    return RedirectResponse(
        f"/admin/network/core-devices/{outcome.device_id}?message="
        + quote_plus(message),
        status_code=303,
    )


@router.get(
    "/core-devices/{device_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_edit(request: Request, device_id: str, db: Session = Depends(get_db)):
    device = web_network_core_devices_service.get_device(db, device_id)
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    if device.archived_at is not None:
        return RedirectResponse(
            f"/admin/network/core-devices/{device.id}?error="
            + quote_plus("Restore this decommissioned device before editing it."),
            status_code=303,
        )
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        current_device_id=device.id,
        pop_site_id=device.pop_site_id,
    )
    form_context = web_network_core_devices_service.build_form_context(
        device=device,
        pop_sites=pop_sites,
        parent_devices=parent_devices,
        selected_pop_site_id=str(device.pop_site_id) if device.pop_site_id else None,
        current_device_id=str(device.id),
        action_url=f"/admin/network/core-devices/{device.id}",
    )
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(form_context)
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.get(
    "/core-devices/{device_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_detail(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.services import core_router_metrics

    page_data = web_network_core_devices_service.detail_page_data(
        db,
        device_id,
        request.query_params.get("interface_id"),
        format_duration=_format_duration,
        format_bps=_format_bps,
        message=request.query_params.get("message"),
        error=request.query_params.get("error"),
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    activities = build_audit_activities(db, "core_device", str(device_id))
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(page_data)
    context["activities"] = activities
    if page_data["device"].archived_at is not None:
        context["can_write_network"] = False
    # Sort monitored interfaces to the top so admins don't have to scroll past
    # all the unwatched ones. Stable secondary sort by name keeps things predictable.
    interfaces = list(page_data.get("interfaces") or [])
    interfaces.sort(key=lambda i: (not bool(i.monitored), (i.name or "").lower()))
    context["interfaces"] = interfaces
    # Live per-interface bandwidth (no-op if no monitored interfaces)
    context["bandwidth"] = (
        {}
        if page_data["device"].archived_at is not None
        else core_router_metrics.get_interface_bandwidth(
            db, page_data["device"], interfaces
        )
    )
    context["format_bps"] = _format_bps
    return templates.TemplateResponse("admin/network/core-devices/detail.html", context)


@router.post(
    "/core-devices/{device_id}/provisioning-access",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_provisioning_access_update(
    device_id: str,
    ssh_username: str = Form(""),
    ssh_password: str | None = Form(None),
    shared_secret: str | None = Form(None),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.update_provisioning_access_for_device(
        db,
        device_id=device_id,
        ssh_username=ssh_username,
        ssh_password=ssh_password,
        shared_secret=shared_secret,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post(
    "/core-devices/{device_id}/interfaces/{interface_id}/toggle-monitored",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_interface_toggle_monitored(
    device_id: str,
    interface_id: str,
    monitored: bool = Form(False),
    db: Session = Depends(get_db),
):
    """Toggle whether an interface is included in bandwidth monitoring."""
    ok, msg = web_network_core_devices_service.toggle_interface_monitored(
        db, device_id=device_id, interface_id=interface_id, monitored=monitored
    )
    if not ok:
        return RedirectResponse(
            f"/admin/network/core-devices/{device_id}?error={quote_plus(msg)}",
            status_code=303,
        )
    # Invalidate the live-bandwidth cache so the next poll reflects the new set.
    from app.services import core_router_metrics

    core_router_metrics.invalidate_cache(device_id)
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}",
        status_code=303,
    )


@router.get(
    "/core-devices/{device_id}/interfaces/bandwidth",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_interfaces_bandwidth_partial(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
):
    """Re-render the interfaces card with the latest interface bandwidth values.

    Designed to be hit by HTMX polling every ~10 s. Returns the full card
    partial so all rows (including the search input via hx-preserve) refresh
    in one pass.
    """
    from app.services import core_router_metrics

    page_data = web_network_core_devices_service.detail_page_data(
        db,
        device_id,
        request.query_params.get("interface_id"),
        format_duration=_format_duration,
        format_bps=_format_bps,
    )
    if not page_data:
        return Response(status_code=404)

    device = page_data.get("device")
    if device is not None and device.archived_at is not None:
        return Response(status_code=409)
    interfaces = list(page_data.get("interfaces") or [])
    interfaces.sort(key=lambda i: (not bool(i.monitored), (i.name or "").lower()))
    bandwidth = core_router_metrics.get_interface_bandwidth(db, device, interfaces)
    context = {
        "request": request,
        "device": device,
        "interfaces": interfaces,
        "bandwidth": bandwidth,
        "format_bps": _format_bps,
        "oob_swap": False,
        "is_partial": True,  # skip outer wrapper; we only swap the inner card
    }
    return templates.TemplateResponse(
        "admin/network/core-devices/_interfaces_card.html", context
    )


@router.get(
    "/core-devices/{device_id}/graphs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_graphs(
    request: Request,
    device_id: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.bandwidth_graphs_page_data(
        db,
        device_id,
        message=request.query_params.get("message"),
        error=request.query_params.get("error"),
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(page_data)
    if page_data["device"].archived_at is not None:
        context["can_write_network"] = False
    return templates.TemplateResponse("admin/network/core-devices/graphs.html", context)


@router.post(
    "/core-devices/{device_id}/graphs",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_graph_create(
    device_id: str,
    title: str = Form(...),
    vertical_axis_title: str = Form("Bandwidth"),
    height_px: int = Form(150),
    is_public: bool = Form(False),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.create_bandwidth_graph_for_device(
        db,
        device_id=device_id,
        title=title,
        vertical_axis_title=vertical_axis_title,
        height_px=height_px,
        is_public=is_public,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post(
    "/core-devices/{device_id}/graphs/{graph_id}/sources",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_graph_source_add(
    device_id: str,
    graph_id: str,
    source_device_id: str = Form(...),
    snmp_oid_id: str = Form(...),
    factor: float = Form(1.0),
    color_hex: str = Form("#22c55e"),
    draw_type: str = Form("LINE1"),
    stack_enabled: bool = Form(False),
    value_unit: str = Form("Bps"),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.add_bandwidth_graph_source(
        db,
        device_id=device_id,
        graph_id=graph_id,
        source_device_id=source_device_id,
        snmp_oid_id=snmp_oid_id,
        factor=factor,
        color_hex=color_hex,
        draw_type=draw_type,
        stack_enabled=stack_enabled,
        value_unit=value_unit,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post(
    "/core-devices/{device_id}/graphs/{graph_id}/toggle-public",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_graph_toggle_public(
    device_id: str,
    graph_id: str,
    is_public: bool = Form(False),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.toggle_bandwidth_graph_public(
        db,
        device_id=device_id,
        graph_id=graph_id,
        is_public=is_public,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post(
    "/core-devices/{device_id}/graphs/{graph_id}/clone",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_graph_clone(
    device_id: str,
    graph_id: str,
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.clone_bandwidth_graph_for_device(
        db,
        device_id=device_id,
        graph_id=graph_id,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/graphs?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.get(
    "/core-devices/graphs/dashboard",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_graphs_dashboard(
    request: Request,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.bandwidth_dashboard_page_data(
        db,
        search=search,
    )
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(page_data)
    return templates.TemplateResponse(
        "admin/network/core-devices/graphs_dashboard.html", context
    )


@router.get(
    "/core-devices/{device_id}/backups",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_backups(
    request: Request,
    device_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_page_data(
        db,
        device_id,
        date_from=date_from,
        date_to=date_to,
        message=request.query_params.get("message"),
        error=request.query_params.get("error"),
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(page_data)
    if page_data["device"].archived_at is not None:
        context["can_write_network"] = False
    return templates.TemplateResponse(
        "admin/network/core-devices/backups.html", context
    )


@router.post(
    "/core-devices/{device_id}/backups/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_backup_settings_update(
    device_id: str,
    enabled: bool = Form(False),
    ssh_username: str = Form(""),
    ssh_password: str | None = Form(None),
    ssh_port: int = Form(22),
    backup_type: str = Form("commands"),
    backup_commands: str | None = Form("export"),
    hours_csv: str | None = Form(None),
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.update_backup_settings_for_device(
        db,
        device_id=device_id,
        enabled=enabled,
        ssh_username=ssh_username,
        ssh_password=ssh_password,
        ssh_port=ssh_port,
        backup_type=backup_type,
        backup_commands=backup_commands,
        hours_csv=hours_csv,
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/backups?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.post(
    "/core-devices/{device_id}/backups/trigger",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_backup_trigger(
    device_id: str,
    db: Session = Depends(get_db),
):
    ok, msg = web_network_core_devices_service.trigger_backup_for_core_device(
        db,
        device_id=device_id,
        triggered_by="web",
    )
    key = "message" if ok else "error"
    return RedirectResponse(
        f"/admin/network/core-devices/{device_id}/backups?{key}={quote_plus(msg)}",
        status_code=303,
    )


@router.get(
    "/core-devices/{device_id}/backups/{backup_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_backup_detail(
    request: Request,
    device_id: str,
    backup_id: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_detail_page_data(
        db,
        device_id=device_id,
        backup_id=backup_id,
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Backup not found"},
            status_code=404,
        )
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(page_data)
    return templates.TemplateResponse(
        "admin/network/core-devices/backup_detail.html", context
    )


@router.get(
    "/core-devices/{device_id}/backups/{backup_id}/download",
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_backup_download(
    device_id: str,
    backup_id: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_detail_page_data(
        db,
        device_id=device_id,
        backup_id=backup_id,
    )
    if not page_data:
        return Response(status_code=404)
    backup = page_data["backup"]
    if backup is None:
        return Response(status_code=404)
    filename = f"core_device_backup_{device_id}_{backup_id}.txt"
    return Response(
        content=str(backup.config_content or ""),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/core-devices/{device_id}/backups/compare",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_backup_compare(
    request: Request,
    device_id: str,
    backup_id_1: str,
    backup_id_2: str,
    db: Session = Depends(get_db),
):
    page_data = web_network_core_devices_service.backup_compare_page_data(
        db,
        device_id=device_id,
        backup_id_1=backup_id_1,
        backup_id_2=backup_id_2,
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Unable to compare selected backups"},
            status_code=404,
        )
    context = _base_context(
        request, db, active_page="core-devices", active_menu="core-network"
    )
    context.update(page_data)
    return templates.TemplateResponse(
        "admin/network/core-devices/backup_compare.html", context
    )


@router.get(
    "/core-devices/{device_id}/health",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def core_device_health_partial(
    request: Request, device_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    device = web_network_core_runtime_service.get_device(db, device_id)
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )
    device_health = web_network_core_runtime_service.compute_health(
        db,
        device,
        interface_id=request.query_params.get("interface_id"),
        format_duration=_format_duration,
        format_bps=_format_bps,
    )

    html = web_network_core_runtime_service.render_device_health_content(device_health)
    return HTMLResponse(
        f'<div id="device-health-content" hx-swap-oob="true">{html}</div>'
    )


@router.post(
    "/core-devices/{device_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def core_device_update(request: Request, device_id: str, db: Session = Depends(get_db)):
    device = web_network_core_devices_service.get_device(db, device_id)
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    if device.archived_at is not None:
        return RedirectResponse(
            f"/admin/network/core-devices/{device.id}?error="
            + quote_plus("Restore this decommissioned device before editing it."),
            status_code=303,
        )
    before_snapshot = model_to_dict(device)

    form = parse_form_data_sync(request)
    values = web_network_core_devices_service.parse_form_values(form)
    pop_sites = web_network_core_devices_service.pop_sites_for_forms(db)
    selected_pop_site_id = (
        str(values.get("pop_site_id")) if values.get("pop_site_id") else None
    )
    selected_pop_site_uuid = _coerce_uuid_or_none(selected_pop_site_id)
    parent_devices = web_network_core_devices_service.parent_devices_for_forms(
        db,
        current_device_id=device.id,
        pop_site_id=selected_pop_site_uuid,
    )
    normalized, error = web_network_core_devices_service.validate_values(
        db,
        values,
        current_device=device,
    )
    if error:
        snapshot = web_network_core_devices_service.snapshot_for_form(
            values,
            device_id=str(device.id),
            status=device.status,
        )
        form_context = web_network_core_devices_service.build_form_context(
            device=snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=str(device.id),
            action_url=f"/admin/network/core-devices/{device.id}",
            error=error,
        )
        context = _base_context(
            request, db, active_page="core-devices", active_menu="core-network"
        )
        context.update(form_context)
        return templates.TemplateResponse(
            "admin/network/core-devices/form.html", context
        )

    if normalized is None:
        form_context = web_network_core_devices_service.build_form_context(
            device=snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=str(device.id),
            action_url=f"/admin/network/core-devices/{device.id}",
            error="Please correct the highlighted fields.",
        )
        context = _base_context(
            request, db, active_page="core-devices", active_menu="core-network"
        )
        context.update(form_context)
        return templates.TemplateResponse(
            "admin/network/core-devices/form.html", context
        )
    result = web_network_core_devices_service.update_device(db, device, normalized)
    if result.error:
        form_context = web_network_core_devices_service.build_form_context(
            device=result.snapshot,
            pop_sites=pop_sites,
            parent_devices=parent_devices,
            selected_pop_site_id=selected_pop_site_id,
            current_device_id=str(device.id),
            action_url=f"/admin/network/core-devices/{device.id}",
            error=result.error,
        )
        context = _base_context(
            request, db, active_page="core-devices", active_menu="core-network"
        )
        context.update(form_context)
        return templates.TemplateResponse(
            "admin/network/core-devices/form.html", context
        )

    after_snapshot = model_to_dict(device)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="core_device",
        entity_id=str(device.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )
    target_url = f"/admin/network/core-devices/{device.id}"
    if result.warning:
        target_url = f"{target_url}?error={quote_plus(result.warning)}"
    return RedirectResponse(target_url, status_code=303)
