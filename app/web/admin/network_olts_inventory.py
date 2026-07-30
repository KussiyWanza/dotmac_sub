"""Admin web routes for OLT inventory, detail, and sync flows."""

from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import OLTDevice, PonPort
from app.models.ont_autofind import OltAutofindCandidate
from app.services import network as network_service
from app.services import web_admin as web_admin_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_ont_autofind as web_network_ont_autofind_service
from app.services import web_network_onts as web_network_onts_service
from app.services import (
    web_network_pon_interfaces as web_network_pon_interfaces_service,
)
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.ipam_adapter import ipam_adapter
from app.services.network import olt_operations as olt_operations_service
from app.services.network import olt_pon_port_control as olt_pon_port_control_service
from app.services.network import olt_tr069_admin as olt_tr069_admin_service
from app.services.network import olt_web_forms as olt_web_forms_service
from app.services.network import olt_web_topology as olt_web_topology_service
from app.services.network.action_logging import actor_label, log_network_action_result
from app.services.network.olt_inventory import get_olt_or_none
from app.services.network.olt_lifecycle import get_deletion_impact
from app.services.network.ont_commissioning import (
    RequestOntCommissioning,
    request_ont_commissioning,
)
from app.services.network.ont_provisioning_commands import request_ont_authorization
from app.services.network.ont_scope import (
    can_authorize_ont_from_request,
    resolve_authorization_redirect_ont,
    submitted_authorization_ont_matches_scope,
)
from app.services.olt_detail_adapter import olt_detail_adapter
from app.services.owner_commands import CommandContext
from app.web.request_parsing import parse_form_data_sync

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network-olt-inventory"])


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


def _acs_prefill_from_olt(olt: OLTDevice | None) -> dict[str, str]:
    acs = getattr(olt, "tr069_acs_server", None) if olt else None
    if acs is None:
        return {}
    return {
        "cwmp_url": getattr(acs, "cwmp_url", "") or "",
        "cwmp_username": getattr(acs, "cwmp_username", "") or "",
    }


def _authorization_result_query(result: object | None) -> str | None:
    if result is None or not hasattr(result, "to_dict"):
        return None
    try:
        payload = json.dumps(result.to_dict(), separators=(",", ":"))
    except Exception:
        return None
    return quote_plus(payload)


def _authorization_result_from_request(request: Request) -> dict[str, object] | None:
    raw = request.query_params.get("authorize_result")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _toast_headers(message: str, toast_type: str) -> dict[str, str]:
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": message, "type": toast_type}},
            ensure_ascii=True,
        )
    }


def _commissioning_form_context(
    request: Request,
    db: Session,
    *,
    olt_id: str,
    candidate_id: str,
    error: str | None = None,
    reason: str = "",
    reference: str = "",
) -> dict[str, object] | None:
    try:
        parsed_candidate_id = uuid.UUID(candidate_id)
        parsed_olt_id = uuid.UUID(olt_id)
    except ValueError:
        return None
    candidate = db.get(OltAutofindCandidate, parsed_candidate_id)
    olt = db.get(OLTDevice, parsed_olt_id)
    if (
        candidate is None
        or olt is None
        or candidate.olt_id != olt.id
        or not candidate.is_active
    ):
        return None
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "olt": olt,
            "candidate": candidate,
            "error": error,
            "reason": reason,
            "reference": reference,
        }
    )
    return context


def _authorization_detail_redirect_url(
    db: Session,
    *,
    ont_id: str | None,
    olt_id: str,
    fsp: str,
    serial_number: str,
    message: str,
) -> str | None:
    """Resolve the ONT detail URL to use after an authorization request."""
    ont = resolve_authorization_redirect_ont(
        db,
        ont_id=ont_id,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
    )
    if ont is None:
        return None
    return f"/admin/network/onts/{ont.id}?status=success&message={quote_plus(message)}"


def _log_olt_action_result(
    *,
    request: Request | None,
    olt_id: str | None,
    action: str,
    ok: bool,
    message: str,
    metadata: dict[str, object] | None = None,
) -> None:
    log_network_action_result(
        request=request,
        resource_type="olt",
        resource_id=olt_id,
        action=action,
        success=ok,
        message=message,
        metadata=metadata,
    )


def _olt_delete_impact(db: Session, olt: OLTDevice) -> dict[str, object]:
    impact = get_deletion_impact(db, str(olt.id))
    active_onts = impact.active_onts if impact else 0
    active_assignments = impact.active_assignments if impact else 0
    linked_vlans = impact.vlans_to_orphan if impact else 0
    linked_ip_pools = impact.ip_pools_to_orphan if impact else 0
    blocking_reasons = []
    if active_onts:
        blocking_reasons.append(
            f"{active_onts} active ONT(s) are still linked to this OLT."
        )
    if active_assignments:
        blocking_reasons.append(
            f"{active_assignments} active ONT assignment(s) still use this OLT."
        )

    warnings = []
    if linked_vlans:
        warnings.append("OLT-scoped VLANs remain preserved for audit and restore.")
    if linked_ip_pools:
        warnings.append("OLT-scoped IP pools remain preserved for audit and restore.")
    return {
        "olt_id": str(olt.id),
        "olt_name": olt.name,
        "can_delete": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "active_onts": active_onts,
        "active_assignments": active_assignments,
        "vlans_to_orphan": linked_vlans,
        "ip_pools_to_orphan": linked_ip_pools,
        "warnings": warnings,
    }


@router.get(
    "/pon-interfaces",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def pon_interfaces_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    olt_id: str | None = None,
    aliased: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="pon-interfaces")
    context.update(
        web_network_pon_interfaces_service.build_page_data(
            db,
            search=search,
            status=status,
            olt_id=olt_id,
            aliased=aliased,
        )
    )
    return templates.TemplateResponse(
        "admin/network/pon_interfaces/index.html", context
    )


@router.post(
    "/pon-interfaces/alias",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def pon_interface_save_alias(
    olt_id: str = Form(""),
    interface_name: str = Form(""),
    alias: str = Form(""),
    pon_port_id: str = Form(""),
    return_to: str = Form("/admin/network/pon-interfaces"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    web_network_pon_interfaces_service.save_alias(
        db,
        olt_id=olt_id,
        interface_name=interface_name,
        alias=alias,
        pon_port_id=pon_port_id or None,
    )
    return RedirectResponse(
        url=return_to or "/admin/network/pon-interfaces",
        status_code=303,
    )


@router.get(
    "/olts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olts_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all OLT devices."""
    page_data = web_network_core_devices_service.olts_list_page_data(
        db,
        search=search,
        status=status,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="olts")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/olts/index.html", context)


@router.get(
    "/olts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            "olt": None,
            "action_url": "/admin/network/olts",
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                db
            ),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_create(request: Request, db: Session = Depends(get_db)):
    values = olt_web_forms_service.parse_form_values(parse_form_data_sync(request))
    error = olt_web_forms_service.validate_values(db, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": None,
                "action_url": "/admin/network/olts",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                    db
                ),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    olt, error = olt_web_forms_service.create_olt_with_audit(db, request, values)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": olt_web_forms_service.snapshot(values),
                "action_url": "/admin/network/olts",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                    db
                ),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    if olt is None:
        raise HTTPException(status_code=500, detail="OLT creation returned no object")
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get(
    "/olts/{olt_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_edit(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    # Get VLANs and IP pools assigned to this OLT for Config Pack section
    ipam_context = ipam_adapter.olt_scope_context(db, olt=olt)
    context.update(
        {
            "olt": olt_web_forms_service.build_form_model(db, olt),
            "action_url": f"/admin/network/olts/{olt.id}",
            "tr069_servers": web_network_onts_service.get_tr069_servers(db),
            "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                db, olt=olt
            ),
            "olt_vlans": ipam_context.get("olt_vlans", []),
            "olt_ip_pools": ipam_context.get("olt_ip_pools", []),
        }
    )
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post(
    "/olts/{olt_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_update(request: Request, olt_id: str, db: Session = Depends(get_db)):
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    values = olt_web_forms_service.parse_form_values(parse_form_data_sync(request))
    error = olt_web_forms_service.validate_values(db, values, current_olt=olt)
    # Get VLANs and IP pools for Config Pack section (needed for error re-render)
    ipam_context = ipam_adapter.olt_scope_context(db, olt=olt)
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": olt_web_forms_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt.id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "operational_acs_server": olt_tr069_admin_service.resolve_operational_acs_server(
                    db, olt=olt
                ),
                "olt_vlans": ipam_context.get("olt_vlans", []),
                "olt_ip_pools": ipam_context.get("olt_ip_pools", []),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    olt, error = olt_web_forms_service.update_olt_with_audit(
        db, request, olt_id, olt, values
    )
    if error:
        context = _base_context(request, db, active_page="olts")
        context.update(
            {
                "olt": olt_web_forms_service.snapshot(values),
                "action_url": f"/admin/network/olts/{olt_id}",
                "error": error,
                "tr069_servers": web_network_onts_service.get_tr069_servers(db),
                "olt_vlans": ipam_context.get("olt_vlans", []),
                "olt_ip_pools": ipam_context.get("olt_ip_pools", []),
            }
        )
        return templates.TemplateResponse("admin/network/olts/form.html", context)
    if olt is None:
        raise HTTPException(status_code=500, detail="OLT update returned no object")
    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get(
    "/olts/{olt_id}/config-pack-validation",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_config_pack_validation_badge(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Return HTMX fragment showing config pack validation status badge."""
    from app.services.network.olt_config_pack import validate_config_pack_comprehensive

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return HTMLResponse(
            '<span class="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-slate-500">'
            "Not found</span>"
        )

    validation = validate_config_pack_comprehensive(db, olt_id)

    if validation.is_valid and not validation.has_warnings:
        # Green: All good
        return HTMLResponse(
            '<span class="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">'
            '<svg class="h-3 w-3" fill="currentColor" viewBox="0 0 20 20">'
            '<path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/>'
            "</svg>Ready</span>"
        )
    elif validation.is_valid:
        # Amber: Warnings but valid
        return HTMLResponse(
            f'<span class="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">'
            f'<svg class="h-3 w-3" fill="currentColor" viewBox="0 0 20 20">'
            f'<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>'
            f"</svg>{len(validation.warnings)} warning{'s' if len(validation.warnings) != 1 else ''}</span>"
        )
    else:
        # Red: Errors
        return HTMLResponse(
            f'<span class="inline-flex items-center gap-1.5 rounded-full bg-rose-50 px-2.5 py-1 text-xs font-medium text-rose-700 dark:bg-rose-900/30 dark:text-rose-400">'
            f'<svg class="h-3 w-3" fill="currentColor" viewBox="0 0 20 20">'
            f'<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/>'
            f"</svg>{len(validation.errors)} error{'s' if len(validation.errors) != 1 else ''}</span>"
        )


@router.get(
    "/olts/{olt_id}/config-pack-validation-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_config_pack_validation_details(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Return HTMX fragment showing config pack validation details."""
    from app.services.network.olt_config_pack import validate_config_pack_comprehensive

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return HTMLResponse("")

    validation = validate_config_pack_comprehensive(db, olt_id)

    if validation.is_valid and not validation.has_warnings:
        return HTMLResponse("")

    html_parts = []

    # Errors banner
    if validation.errors:
        errors_html = "".join(f"<li>{e}</li>" for e in validation.errors)
        html_parts.append(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm dark:border-rose-800 dark:bg-rose-900/30">'
            f'<div class="flex items-start gap-2">'
            f'<svg class="h-5 w-5 flex-shrink-0 text-rose-500 dark:text-rose-400" fill="currentColor" viewBox="0 0 20 20">'
            f'<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/>'
            f"</svg>"
            f"<div>"
            f'<p class="font-medium text-rose-700 dark:text-rose-400">ONT authorization is blocked</p>'
            f'<ul class="mt-1 list-disc list-inside text-rose-600 dark:text-rose-300">{errors_html}</ul>'
            f"</div></div></div>"
        )

    # Warnings banner
    if validation.warnings:
        warnings_html = "".join(f"<li>{w}</li>" for w in validation.warnings)
        html_parts.append(
            f'<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm dark:border-amber-800 dark:bg-amber-900/30">'
            f'<div class="flex items-start gap-2">'
            f'<svg class="h-5 w-5 flex-shrink-0 text-amber-500 dark:text-amber-400" fill="currentColor" viewBox="0 0 20 20">'
            f'<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>'
            f"</svg>"
            f"<div>"
            f'<p class="font-medium text-amber-700 dark:text-amber-400">Configuration incomplete</p>'
            f'<ul class="mt-1 list-disc list-inside text-amber-600 dark:text-amber-300">{warnings_html}</ul>'
            f"</div></div></div>"
        )

    return HTMLResponse("".join(html_parts))


@router.get(
    "/olts/{olt_id}/delete-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_delete_preview(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="olts")
    context["impact"] = _olt_delete_impact(db, olt)
    return templates.TemplateResponse(
        "admin/network/olts/_delete_preview.html", context
    )


@router.post(
    "/olts/{olt_id}/delete",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_delete(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return RedirectResponse(
            "/admin/network/olts?delete_status=error&delete_message=OLT+not+found",
            status_code=303,
        )

    impact = _olt_delete_impact(db, olt)
    if not impact["can_delete"]:
        message = quote_plus("Resolve active ONTs or assignments before archiving OLT.")
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="archive_olt",
            ok=False,
            message="OLT archive blocked by active dependencies.",
            metadata=impact,
        )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={message}",
            status_code=303,
        )

    try:
        network_service.olt_devices.delete(db, olt_id)
    except HTTPException as exc:
        detail = quote_plus(str(exc.detail))
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="archive_olt",
            ok=False,
            message=str(exc.detail),
            metadata=impact,
        )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={detail}",
            status_code=303,
        )

    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="archive_olt",
        ok=True,
        message="OLT archived.",
        metadata=impact,
    )
    message = quote_plus(f"OLT {olt.name} archived.")
    return RedirectResponse(
        f"/admin/network/olts?delete_status=success&delete_message={message}",
        status_code=303,
    )


@router.get(
    "/olts/{olt_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_detail(
    request: Request,
    olt_id: str,
    ssh_test_status: str | None = None,
    ssh_test_message: str | None = None,
    snmp_test_status: str | None = None,
    snmp_test_message: str | None = None,
    sync_status: str | None = None,
    sync_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = olt_detail_adapter.page_data(db, olt_id=olt_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
            "ssh_test_status": ssh_test_status,
            "ssh_test_message": ssh_test_message,
            "snmp_test_status": snmp_test_status,
            "snmp_test_message": snmp_test_message,
            "sync_status": sync_status,
            "sync_message": sync_message,
            "authorization_result": _authorization_result_from_request(request),
        }
    )
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


@router.get(
    "/olts/{olt_id}/events",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_device_events(
    request: Request,
    olt_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Lazy panel: ONT device events for this OLT."""
    data = olt_detail_adapter.events_context(db, olt_id=olt_id)
    context = _base_context(request, db, active_page="olts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/olts/_events_partial.html", context
    )


@router.get(
    "/olts/{olt_id}/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_detail_preview(
    request: Request,
    olt_id: str,
    ssh_test_status: str | None = None,
    ssh_test_message: str | None = None,
    snmp_test_status: str | None = None,
    snmp_test_message: str | None = None,
    sync_status: str | None = None,
    sync_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    page_data = olt_detail_adapter.page_data(db, olt_id=olt_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="olts")
    context.update(
        {
            **page_data,
            "ssh_test_status": ssh_test_status,
            "ssh_test_message": ssh_test_message,
            "snmp_test_status": snmp_test_status,
            "snmp_test_message": snmp_test_message,
            "sync_status": sync_status,
            "sync_message": sync_message,
            "authorization_result": _authorization_result_from_request(request),
            "preview_mode": True,
        }
    )
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


@router.post(
    "/olts/{olt_id}/pon-port-identifier",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_save_pon_port_identifier(
    request: Request,
    olt_id: str,
    pon_port_id: str = Form(...),
    alias: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    olt = get_olt_or_none(db, olt_id)
    if not olt:
        message = quote_plus("OLT not found")
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={message}",
            status_code=303,
        )

    port = db.get(PonPort, pon_port_id)
    if not port or str(port.olt_id) != str(olt.id):
        message = quote_plus("PON port not found for this OLT")
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="update_pon_port_identifier",
            ok=False,
            message="PON port not found for this OLT.",
            metadata={"pon_port_id": pon_port_id},
        )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={message}",
            status_code=303,
        )

    try:
        web_network_pon_interfaces_service.save_alias(
            db,
            olt_id=str(olt.id),
            interface_name=str(port.name or ""),
            alias=alias,
            pon_port_id=str(port.id),
        )
    except HTTPException as exc:
        detail = str(exc.detail)
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="update_pon_port_identifier",
            ok=False,
            message=detail,
            metadata={"pon_port_id": str(port.id), "interface_name": port.name},
        )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?sync_status=error&sync_message={quote_plus(detail)}",
            status_code=303,
        )

    alias_text = (alias or "").strip()
    message = (
        f"Identifier updated for {port.name}."
        if alias_text
        else f"Identifier cleared for {port.name}."
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="update_pon_port_identifier",
        ok=True,
        message=message,
        metadata={
            "pon_port_id": str(port.id),
            "interface_name": port.name,
            "alias": alias_text or None,
        },
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status=success&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/vlans/assign",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_assign_vlan(
    request: Request,
    olt_id: str,
    vlan_id: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    ipam_adapter.assign_vlan_to_olt(db, olt_id, vlan_id)
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=settings", status_code=303
    )


@router.post(
    "/olts/{olt_id}/vlans/unassign",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_unassign_vlan(
    request: Request,
    olt_id: str,
    vlan_id: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    ipam_adapter.unassign_vlan_from_olt(db, olt_id, vlan_id)
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=settings", status_code=303
    )


@router.post(
    "/olts/{olt_id}/ip-pools/assign",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_assign_ip_pool(
    request: Request,
    olt_id: str,
    pool_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ipam_adapter.assign_ip_pool_to_olt(db, olt_id, pool_id)
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=settings", status_code=303
    )


@router.post(
    "/olts/{olt_id}/ip-pools/unassign",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_unassign_ip_pool(
    request: Request,
    olt_id: str,
    pool_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ipam_adapter.unassign_ip_pool_from_olt(db, olt_id, pool_id)
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=settings", status_code=303
    )


@router.post(
    "/olts/{olt_id}/cli",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_run_cli_command(
    request: Request,
    olt_id: str,
    command: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute a read-only CLI command on an OLT and return the output as HTML partial."""
    cmd = command.strip()
    if not cmd:
        return HTMLResponse(
            '<pre class="text-sm text-slate-400 dark:text-slate-500">Enter a command above.</pre>'
        )
    import html as html_mod

    ok, message, output = olt_operations_service.execute_cli_command(db, olt_id, cmd)
    if not ok:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Run CLI Command",
            ok=ok,
            message=message,
            metadata={"command": cmd},
        )
        return HTMLResponse(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 '
            f'dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{html_mod.escape(message)}</div>'
        )
    escaped_output = html_mod.escape(output)
    return HTMLResponse(
        f'<pre class="whitespace-pre-wrap break-words text-sm font-mono text-emerald-800 '
        f'dark:text-emerald-300 bg-slate-900 dark:bg-slate-950 rounded-lg p-4 overflow-x-auto">'
        f"{escaped_output or '(no output)'}</pre>"
    )


def _olt_status_by_serial_html(
    *, ok: bool, message: str, status: dict[str, object]
) -> HTMLResponse:
    import html as html_mod

    escaped_msg = html_mod.escape(message)
    if not ok:
        return HTMLResponse(
            f'<div class="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 '
            f'dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-400">{escaped_msg}</div>'
        )

    rows = [
        ("Requested Serial", status.get("requested_serial")),
        ("Lookup Serial", status.get("lookup_serial")),
        ("Registered Serial", status.get("registered_serial")),
        ("Status Serial", status.get("status_serial")),
        ("F/S/P", status.get("fsp")),
        ("ONT-ID", status.get("ont_id")),
        ("Run State", status.get("run_state")),
        ("Config State", status.get("config_state")),
        ("Match State", status.get("match_state")),
    ]
    row_html = "".join(
        "<tr>"
        f'<th class="px-3 py-2 text-left text-xs font-semibold uppercase text-slate-500 dark:text-slate-400">{html_mod.escape(label)}</th>'
        f'<td class="px-3 py-2 font-mono text-sm text-slate-900 dark:text-slate-100">{html_mod.escape(str(value or "-"))}</td>'
        "</tr>"
        for label, value in rows
    )
    return HTMLResponse(
        f'<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-700 '
        f'dark:border-emerald-900/30 dark:bg-emerald-900/10 dark:text-emerald-300 mb-3">{escaped_msg}</div>'
        f'<div class="overflow-hidden rounded-lg border border-slate-200 dark:border-slate-700">'
        f'<table class="min-w-full divide-y divide-slate-200 dark:divide-slate-700">'
        f'<tbody class="divide-y divide-slate-100 bg-white dark:divide-slate-800 dark:bg-slate-900">{row_html}</tbody>'
        f"</table></div>"
    )


@router.post(
    "/olts/{olt_id}/ont-status-by-serial",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_ont_status_by_serial(
    request: Request,
    olt_id: str,
    serial_number: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Lookup an ONT by serial on this OLT and return full OLT-side status."""
    ok, message, status = olt_operations_service.get_ont_status_by_serial(
        db, olt_id, serial_number, request=request
    )
    return _olt_status_by_serial_html(ok=ok, message=message, status=status)


@router.post(
    "/olts/{olt_id}/test-ssh",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_test_ssh_connection(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message, _policy_key = olt_operations_service.test_olt_ssh_connection(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Test SSH Connection",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?ssh_test_status={status}&ssh_test_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/ssh-get-config",
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_ssh_get_config(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Fetch OLT running config via SSH/CLI and return as formatted HTML."""
    import html as html_mod

    ok, message, config_text = olt_operations_service.fetch_running_config_ssh_preview(
        db, olt_id, request=request
    )
    escaped_msg = html_mod.escape(message)
    if not ok:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Get SSH Running Config",
            ok=ok,
            message=message,
        )
        return HTMLResponse(
            f'<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            f'dark:border-red-900/30 dark:bg-red-900/10 dark:text-red-300">{escaped_msg}</div>'
        )

    escaped_config = html_mod.escape(config_text)
    return HTMLResponse(
        f'<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-700 '
        f'dark:border-emerald-900/30 dark:bg-emerald-900/10 dark:text-emerald-300 mb-3">'
        f"{escaped_msg}. Retrieved over SSH CLI.</div>"
        f'<pre class="rounded-lg bg-slate-900 p-4 text-xs font-mono text-emerald-400 overflow-x-auto '
        f'max-h-[600px] overflow-y-auto whitespace-pre-wrap">{escaped_config}</pre>'
    )


@router.post(
    "/olts/{olt_id}/refresh-telemetry",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_refresh_ont_telemetry(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    # The Zabbix telemetry ingest this button used to queue was retired with
    # the native monitoring cutover; report the retirement instead of queueing.
    ok = False
    message = "ONU telemetry refresh is unavailable: monitoring ingest retired."
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Refresh ONU Telemetry",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/{olt_id}/refresh-telemetry",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_refresh_ont_telemetry_get_fallback(olt_id: str) -> RedirectResponse:
    """GET fallback for auth-refresh redirects targeting the telemetry POST endpoint."""
    message = quote_plus(
        "Telemetry refresh uses POST. Please click Refresh Telemetry again."
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status=info&sync_message={message}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/pon-ports/{pon_port_id}/admin-state",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_set_pon_port_admin_state(
    request: Request,
    olt_id: str,
    pon_port_id: str,
    enabled: bool = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Enable or disable one physical PON port on a supported OLT."""
    ok, message = olt_pon_port_control_service.set_pon_port_admin_state(
        db,
        olt_id=olt_id,
        pon_port_id=pon_port_id,
        enabled=enabled,
        request=request,
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Set PON Port Admin State",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/repair-pon-ports",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_repair_pon_ports(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message, _stats = olt_web_topology_service.repair_pon_ports_for_olt_tracked(
        db, olt_id, request=request
    )
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Repair PON Ports",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/olts/{olt_id}/repair-pon-ports",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_repair_pon_ports_get_fallback(olt_id: str) -> RedirectResponse:
    message = quote_plus(
        "Repair PON ports uses POST. Please click Repair PON Ports again."
    )
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?sync_status=info&sync_message={message}",
        status_code=303,
    )


@router.post(
    "/olts/{olt_id}/discover-hardware",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_discover_hardware(
    request: Request, olt_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Trigger SNMP Entity MIB hardware discovery for a single OLT."""
    from app.services.network.olt import OLTDevices
    from app.services.network.olt_hardware_discovery import (
        discover_olt_hardware_audited,
    )

    try:
        olt = OLTDevices.get(db, olt_id)
    except HTTPException:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Discover Hardware",
            ok=False,
            message="OLT not found",
        )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?tab=overview&sync_status=error&sync_message={quote_plus('OLT not found')}",
            status_code=303,
        )

    ok, message, _stats = discover_olt_hardware_audited(db, olt, request=request)
    _log_olt_action_result(
        request=request,
        olt_id=olt_id,
        action="Discover Hardware",
        ok=ok,
        message=message,
    )
    status = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/network/olts/{olt_id}?tab=overview&sync_status={status}&sync_message={quote_plus(message)}",
        status_code=303,
    )


@router.get(
    "/unconfigured-onts",
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def unconfigured_onts_list(
    search: str | None = None,
    olt_id: str | None = None,
    view: str | None = None,
    resolution: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> RedirectResponse:
    target = web_network_ont_autofind_service.build_unconfigured_onts_redirect_url(
        search=search,
        olt_id=olt_id,
        view=view,
        resolution=resolution,
        status=status,
        message=message,
    )
    return RedirectResponse(target, status_code=303)


@router.post(
    "/unconfigured-onts/{candidate_id}/restore",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def restore_autofind_candidate(
    request: Request, candidate_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    ok, message = web_network_ont_autofind_service.restore_candidate_audited(
        db, candidate_id=candidate_id, request=request
    )
    log_network_action_result(
        request=request,
        resource_type="ont_autofind_candidate",
        resource_id=candidate_id,
        action="Restore Autofind Candidate",
        success=ok,
        message=message,
    )
    status = "success" if ok else "error"
    target = web_network_ont_autofind_service.build_unconfigured_onts_feedback_url(
        status=status,
        message=message,
    )
    return RedirectResponse(
        target,
        status_code=303,
    )


@router.post(
    "/unconfigured-onts/refresh",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def refresh_autofind_candidates(
    request: Request,
    olt_id: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not olt_id:
        ok = False
        message = "Select one OLT before refreshing autofind candidates"
    else:
        ok, message, _stats = (
            web_network_ont_autofind_service.refresh_autofind_from_olt_audited(
                db,
                olt_id=olt_id,
                request=request,
            )
        )
        db.commit()
    log_network_action_result(
        request=request,
        resource_type="olt",
        resource_id=olt_id or "none",
        action="Refresh Autofind Candidates",
        success=ok,
        message=message,
    )
    status = "success" if ok else "error"
    target = web_network_ont_autofind_service.build_unconfigured_onts_feedback_url(
        status=status,
        message=message,
    )
    return RedirectResponse(target, status_code=303)


@router.get(
    "/olts/{olt_id}/commission-ont",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:commission"))],
)
def olt_commission_ont_form(
    request: Request,
    olt_id: str,
    candidate_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render explicit confirmation for assignment-free management bootstrap."""

    context = _commissioning_form_context(
        request,
        db,
        olt_id=olt_id,
        candidate_id=candidate_id,
    )
    if context is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {
                "request": request,
                "message": "Active autofind candidate not found on this OLT.",
            },
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/network/onts/commission.html",
        context,
    )


@router.post(
    "/olts/{olt_id}/commission-ont",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:commission"))],
)
def olt_commission_ont(
    request: Request,
    olt_id: str,
    candidate_id: str = Form(""),
    expected_fsp: str = Form(""),
    expected_serial: str = Form(""),
    reason: str = Form(""),
    reference: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Admit one exact, expiring management-only commissioning intent."""

    try:
        parsed_olt_id = uuid.UUID(olt_id)
        parsed_candidate_id = uuid.UUID(candidate_id)
    except ValueError:
        context = None
        error = "The commissioning target is invalid. Refresh the autofind list."
    else:
        db_session_adapter.release_read_transaction(db)
        try:
            outcome = request_ont_commissioning(
                db,
                RequestOntCommissioning(
                    context=CommandContext.system(
                        actor=actor_label(request),
                        scope="network:ont:commission",
                        reason="operator requested management-only ONT commissioning",
                        idempotency_key=f"ont-commission:{parsed_candidate_id}",
                    ),
                    candidate_id=parsed_candidate_id,
                    expected_olt_id=parsed_olt_id,
                    expected_fsp=expected_fsp,
                    expected_serial=expected_serial,
                    reason=reason,
                    reference=reference or None,
                ),
            )
        except DomainError as exc:
            error = exc.message
            context = _commissioning_form_context(
                request,
                db,
                olt_id=olt_id,
                candidate_id=candidate_id,
                error=error,
                reason=reason,
                reference=reference,
            )
        else:
            target = (
                "/admin/network/onts?view=unconfigured"
                f"&search={quote_plus(expected_serial)}"
                f"&status=success&message={quote_plus(outcome.message)}"
            )
            return RedirectResponse(target, status_code=303)
    if context is None:
        return templates.TemplateResponse(
            "admin/errors/400.html",
            {"request": request, "message": error},
            status_code=400,
        )
    return templates.TemplateResponse(
        "admin/network/onts/commission.html",
        context,
        status_code=400,
    )


@router.post(
    "/olts/{olt_id}/authorize-ont",
    dependencies=[Depends(require_permission("network:olt:write"))],
)
def olt_authorize_ont(
    request: Request,
    olt_id: str,
    fsp: str = Form(""),
    serial_number: str = Form(""),
    ont_id: str = Form(""),
    return_to: str = Form(""),
    force_reauthorize: str = Form(""),
    preset_id: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Authorize a discovered ONT on the OLT via SSH."""
    is_htmx = request.headers.get("HX-Request") == "true"

    if not fsp or not serial_number:
        _log_olt_action_result(
            request=request,
            olt_id=olt_id,
            action="Authorize ONT",
            ok=False,
            message="Missing port or serial number",
            metadata={"fsp": fsp, "serial_number": serial_number},
        )
        msg = quote_plus("Missing port or serial number")
        if return_to in (
            "/admin/network/unconfigured-onts",
            "/admin/network/onts",
            "/admin/network/onts?view=unconfigured",
        ):
            target = (
                "/admin/network/onts?view=unconfigured"
                if return_to != "/admin/network/onts"
                else return_to
            )
            separator = "&" if "?" in target else "?"
            return RedirectResponse(
                f"{target}{separator}status=error&message={msg}",
                status_code=303,
            )
        target = f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status=error&sync_message={msg}"
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers("Missing port or serial number", "error"),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    if isinstance(ont_id, str) and ont_id:
        if not submitted_authorization_ont_matches_scope(
            db,
            ont_id=ont_id,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
        ):
            error_msg = "ONT authorization scope check failed"
            if is_htmx:
                return Response(
                    status_code=403,
                    headers=_toast_headers(error_msg, "error"),
                )
            return RedirectResponse(
                f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status=error"
                f"&sync_message={quote_plus(error_msg)}",
                status_code=303,
            )

    scoped_ont_id = ont_id if isinstance(ont_id, str) else ""
    if not can_authorize_ont_from_request(request, db, scoped_ont_id):
        error_msg = "ONT authorization scope check failed"
        if is_htmx:
            return Response(
                status_code=403,
                headers=_toast_headers(error_msg, "error"),
            )
        return RedirectResponse(
            f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status=error"
            f"&sync_message={quote_plus(error_msg)}",
            status_code=303,
        )

    force = str(force_reauthorize or "").lower() in ("true", "1", "on", "yes")
    # Normalize preset_id: empty string means no preset selected
    effective_preset_id = preset_id.strip() if preset_id else None
    try:
        command = request_ont_authorization(
            db,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
            force_reauthorize=force,
            preset_id=effective_preset_id,
            scoped_ont_id=scoped_ont_id or None,
            initiated_by=actor_label(request),
        )
    except Exception as exc:
        logger.exception(
            "Failed to stage ONT authorization olt_id=%s fsp=%s serial=%s",
            olt_id,
            fsp,
            serial_number,
        )
        command = None
        auth_msg = f"Authorization could not be started: {exc}"

    if command and command.accepted:
        auth_msg = (
            f"Authorization started for ONT {serial_number}. "
            "The ONT detail page will show progress and next actions."
        )
        detail_target = _authorization_detail_redirect_url(
            db,
            ont_id=scoped_ont_id or None,
            olt_id=olt_id,
            fsp=fsp,
            serial_number=serial_number,
            message=auth_msg,
        )
        if detail_target:
            target = detail_target
        else:
            # Redirect to unconfigured ONTs list with serial search
            target = (
                f"/admin/network/onts?view=unconfigured"
                f"&search={quote_plus(serial_number)}"
                f"&status=success&message={quote_plus(auth_msg)}"
            )
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers(auth_msg, "success"),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    auth_msg = (
        auth_msg
        if "auth_msg" in locals()
        else (
            "Authorization could not be started: "
            f"{getattr(command, 'message', 'unknown error')}"
        )
    )

    # On failure, redirect back to where user came from with error message
    if return_to in (
        "/admin/network/unconfigured-onts",
        "/admin/network/onts",
        "/admin/network/onts?view=unconfigured",
    ):
        target_base = (
            "/admin/network/onts?view=unconfigured"
            if return_to != "/admin/network/onts"
            else return_to
        )
        separator = "&" if "?" in target_base else "?"
        target = f"{target_base}{separator}status=error&message={quote_plus(auth_msg)}"
        if is_htmx:
            return Response(
                status_code=200,
                headers={
                    **_toast_headers(auth_msg, "error"),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(target, status_code=303)

    # Default: redirect to OLT provisioning tab with error
    target = (
        f"/admin/network/olts/{olt_id}?tab=provisioning&sync_status=error"
        f"&sync_message={quote_plus(auth_msg)}"
    )
    if is_htmx:
        return Response(
            status_code=200,
            headers={
                **_toast_headers(auth_msg, "error"),
                "HX-Redirect": target,
            },
        )
    return RedirectResponse(target, status_code=303)


@router.get(
    "/olts/{olt_id}/authorize-ont",
    dependencies=[Depends(require_permission("network:olt:read"))],
)
def olt_authorize_ont_redirect(olt_id: str) -> RedirectResponse:
    """Redirect accidental GET replays back to the OLT autofind tab."""
    return RedirectResponse(
        url=f"/admin/network/olts/{olt_id}?tab=provisioning",
        status_code=303,
    )
