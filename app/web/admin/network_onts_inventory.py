"""Admin web routes for ONT inventory, detail, and edit flows."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.models.network import OntUnit
from app.services import web_admin as web_admin_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import (
    web_network_ont_assignments as web_network_ont_assignments_service,
)
from app.services import web_network_ont_autofind as web_network_ont_autofind_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import has_permission, require_permission
from app.services.network import ont_web_forms as ont_web_forms_service
from app.services.network.ont_commissioning import (
    active_candidate_for_ont,
    latest_intent_for_ont,
)
from app.services.network.ont_scope import filter_manageable_ont_ids_from_request
from app.web.request_parsing import parse_form_data_sync
from app.web.templates import templates

router = APIRouter(prefix="/network", tags=["web-admin-network-ont-inventory"])
logger = logging.getLogger(__name__)


def _form_str(form: FormData, key: str, default: str = "") -> str:
    return ont_web_forms_service.form_str(form, key, default)


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    auth = getattr(request.state, "auth", None) or {}
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
        "can_redrive_network_operations": bool(auth)
        and has_permission(auth, db, "network:operation:redrive"),
        "can_commission_ont": bool(auth)
        and has_permission(auth, db, "network:ont:commission"),
    }


def _authorization_result_from_request(request: Request) -> dict[str, object] | None:
    raw = request.query_params.get("authorize_result")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _ont_feedback_from_request(request: Request) -> dict[str, str] | None:
    status = request.query_params.get("feedback_status")
    message = request.query_params.get("feedback_message")
    if not status or not message:
        return None
    return {"status": str(status), "message": str(message)}


def _ont_redirect(
    ont_id: str,
    *,
    tab: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> RedirectResponse:
    params: list[str] = []
    if tab:
        params.append(f"tab={tab}")
    if status:
        params.append(f"feedback_status={status}")
    if message:
        params.append(f"feedback_message={quote_plus(message)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(
        url=f"/admin/network/onts/{ont_id}{suffix}", status_code=303
    )


def _ont_form_dependencies(db: Session, ont: Any | None = None) -> dict:
    return ont_web_forms_service.ont_form_dependencies(db, ont)


def _ont_has_active_assignment(db: Session, ont_id: str) -> bool:
    return web_network_ont_assignments_service.has_active_assignment(db, ont_id)


def _assignment_form_context(
    request: Request,
    db: Session,
    *,
    ont,
    action_url: str,
    form: dict[str, object] | None = None,
    error: str | None = None,
    mode: str = "create",
    assignment=None,
) -> dict[str, object]:
    deps = web_network_ont_assignments_service.assignment_form_dependencies(db, ont=ont)
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": ont,
            **deps,
            "action_url": action_url,
            "error": error,
            "form": form,
            "form_mode": mode,
            "assignment": assignment,
        }
    )
    return context


def _is_htmx_request(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


@router.get(
    "/onts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def onts_list(
    request: Request,
    view: str = "list",
    status: str | None = None,
    candidate_view: str | None = None,
    resolution: str | None = None,
    message: str | None = None,
    olt_id: str | None = None,
    pon_port_id: str | None = None,
    pon_hint: str | None = None,
    zone_id: str | None = None,
    operational_status: str | None = None,
    authorization: str | None = None,
    offline_reason: str | None = None,
    signal_quality: str | None = None,
    search: str | None = None,
    vendor: str | None = None,
    pppoe_health: str | None = None,
    order_by: str = "serial_number",
    order_dir: str = "asc",
    page: int = 1,
    per_page: int = Query(50, ge=10, le=500),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all ONT/CPE devices with advanced filtering."""
    page_data = web_network_core_devices_service.onts_list_page_data(
        db,
        view=view,
        status=status,
        olt_id=olt_id,
        pon_port_id=pon_port_id,
        pon_hint=pon_hint,
        zone_id=zone_id,
        operational_status=operational_status,
        authorization=authorization,
        offline_reason=offline_reason,
        signal_quality=signal_quality,
        search=search,
        vendor=vendor,
        pppoe_health=pppoe_health,
        order_by=order_by,
        order_dir=order_dir,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="onts")
    context.update(page_data)
    unconfigured_data = (
        web_network_ont_autofind_service.build_unconfigured_onts_page_data(
            db,
            search=search,
            olt_id=olt_id,
            view=candidate_view,
            resolution=resolution,
        )
    )
    context["unconfigured_entries"] = unconfigured_data["entries"]
    context["unconfigured_selected_view"] = unconfigured_data["selected_view"]
    context["unconfigured_selected_resolution"] = unconfigured_data[
        "selected_resolution"
    ]
    context["unconfigured_stats"] = unconfigured_data["stats"]
    context["unconfigured_olts"] = unconfigured_data["olts"]
    context["status"] = status
    context["message"] = message
    context["firmware_images"] = web_network_onts_service.get_active_firmware_images(db)
    context["authorization_result"] = _authorization_result_from_request(request)
    return templates.TemplateResponse("admin/network/onts/index.html", context)


@router.post(
    "/onts/bulk-action",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def onts_bulk_action(
    request: Request,
    action: str = Form(""),
    ont_ids: list[str] = Form(default=[]),
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute a bulk action on selected ONTs."""
    scoped_ont_ids = filter_manageable_ont_ids_from_request(request, db, list(ont_ids))
    context = {
        "request": request,
        **web_network_onts_service.bulk_action_summary_context(
            db,
            scoped_ont_ids,
            action,
            firmware_image_id=firmware_image_id or None,
        ),
    }
    skipped_out_of_scope = max(len(ont_ids) - len(scoped_ont_ids), 0)
    if skipped_out_of_scope:
        context["stats"]["skipped_out_of_scope"] = skipped_out_of_scope
    return templates.TemplateResponse(
        "admin/network/onts/_bulk_action_summary.html",
        context,
    )


@router.get(
    "/onts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont": None,
            "action_url": "/admin/network/onts",
            **_ont_form_dependencies(db),
        }
    )
    return templates.TemplateResponse("admin/network/onts/form.html", context)


@router.post(
    "/onts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    result = ont_web_forms_service.create_ont_from_form(db, form, request=request)
    if result.error:
        context = _base_context(request, db, active_page="onts")
        context.update(
            {
                "ont": result.form_model,
                "action_url": "/admin/network/onts",
                "error": result.error,
                **_ont_form_dependencies(db, result.form_model),
            }
        )
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    assert result.ont is not None
    return RedirectResponse(f"/admin/network/onts/{result.ont.id}", status_code=303)


@router.get(
    "/onts/{ont_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_detail(
    request: Request,
    ont_id: str,
    tab: str = Query(default="overview"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    started_at = monotonic()
    logger.debug("ont_detail_start", extra={"ont_id": ont_id})
    page_data = web_network_core_devices_service.ont_detail_page_data(db, ont_id)
    page_data_duration_ms = round((monotonic() - started_at) * 1000.0, 2)
    logger.debug(
        "ont_detail_page_data_loaded",
        extra={"ont_id": ont_id, "duration_ms": page_data_duration_ms},
    )
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    # New tabbed UI tabs
    allowed_tabs = {
        "overview",
        "configuration",
        "diagnostics",
        "operations",
        "hosts",
        "files",
    }
    tab_aliases = {
        # Legacy tab names -> new tabs
        "status": "overview",
        "summary": "overview",
        "effective-config": "configuration",
        "observed-state": "overview",
        "config": "configuration",
        "service": "configuration",
        "network": "configuration",
        "service-ports": "configuration",
        "device-config": "configuration",
        "configure": "configuration",
        "device-status": "overview",
        "topology": "overview",
        "tr069": "diagnostics",
        "charts": "diagnostics",
        "history": "overview",
        "operational-health": "operations",
        "runbook": "operations",
    }
    tab = tab_aliases.get(tab, tab)
    active_tab = tab if tab in allowed_tabs else "overview"

    activities = build_audit_activities(db, "ont", str(ont_id))
    try:
        operations = web_network_operations_service.build_operation_history(
            db, "ont", str(ont_id)
        )
    except Exception:
        logger.error(
            "Failed to load operation history for ONT %s",
            ont_id,
            exc_info=True,
        )
        operations = []

    context = _base_context(request, db, active_page="onts")
    address_value, contact_value = ont_web_forms_service.split_location_metadata(
        getattr(page_data["ont"], "address_or_comment", None)
    )
    contact_value = str(getattr(page_data["ont"], "contact", None) or contact_value)
    context.update(
        {
            **page_data,
            **web_network_ont_actions_service.unified_config_context(
                db, ont_id, detail_payload=page_data
            ),
            "activities": activities,
            "operations": operations,
            "ont_active_tab": active_tab,
            "location_address_or_comment": address_value,
            "location_contact": contact_value,
            "now": datetime.now(UTC),
            "authorization_result": _authorization_result_from_request(request),
            "ont_feedback": _ont_feedback_from_request(request),
            "commissioning_intent": latest_intent_for_ont(db, ont_id),
            "commissioning_candidate": active_candidate_for_ont(
                db, cast(OntUnit, page_data["ont"])
            ),
        }
    )
    total_duration_ms = round((monotonic() - started_at) * 1000.0, 2)
    if total_duration_ms >= 2000:
        logger.info(
            "ont_detail_context_ready",
            extra={
                "ont_id": ont_id,
                "duration_ms": total_duration_ms,
                "page_data_duration_ms": page_data_duration_ms,
            },
        )
    else:
        logger.debug(
            "ont_detail_context_ready",
            extra={
                "ont_id": ont_id,
                "duration_ms": total_duration_ms,
                "page_data_duration_ms": page_data_duration_ms,
            },
        )
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get(
    "/onts/{ont_id}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_assign_new(
    request: Request,
    ont_id: str,
    return_to: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    result = web_network_ont_assignments_service.get_ont_for_assignment_form(db, ont_id)
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": result.not_found_message},
            status_code=404,
        )

    assert result.ont is not None
    context = _assignment_form_context(
        request,
        db,
        ont=result.ont,
        action_url=f"/admin/network/onts/{result.ont.id}/assign",
        mode="create",
    )
    if return_to in ("provision",):
        context["return_to"] = return_to
    return templates.TemplateResponse("admin/network/onts/assign.html", context)


@router.get(
    "/onts/{ont_id}/assign-modal",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_assign_modal(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    result = web_network_ont_assignments_service.get_ont_for_assignment_form(db, ont_id)
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    assert result.ont is not None
    context = _assignment_form_context(
        request,
        db,
        ont=result.ont,
        action_url=f"/admin/network/onts/{result.ont.id}/assign",
        mode="create",
    )
    return templates.TemplateResponse(
        "admin/network/onts/_assign_subscriber_modal.html", context
    )


@router.post(
    "/onts/{ont_id}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_assign_create(request: Request, ont_id: str, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    return_to = _form_str(form, "return_to")
    is_modal = _form_str(form, "assignment_modal") == "1" or _is_htmx_request(request)
    if is_modal and not _form_str(form, "account_id"):
        loaded = web_network_ont_assignments_service.get_ont_for_assignment_form(
            db, ont_id
        )
        if loaded.not_found:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "ONT not found"},
                status_code=404,
            )
        assert loaded.ont is not None
        context = _assignment_form_context(
            request,
            db,
            ont=loaded.ont,
            action_url=f"/admin/network/onts/{loaded.ont.id}/assign",
            error="Select a subscriber account.",
            form=web_network_ont_assignments_service.form_payload(
                {"account_id": None}, db
            ),
            mode="create",
        )
        return templates.TemplateResponse(
            "admin/network/onts/_assign_subscriber_modal.html", context
        )
    result = web_network_ont_assignments_service.create_assignment_from_form(
        db,
        ont_id,
        form,
        actor_id=web_admin_service.get_actor_id(request),
    )
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    if result.error:
        assert result.ont is not None
        context = _assignment_form_context(
            request,
            db,
            ont=result.ont,
            action_url=f"/admin/network/onts/{result.ont.id}/assign",
            error=result.error,
            form=web_network_ont_assignments_service.form_payload(
                result.values or {}, db
            ),
            mode="create",
        )
        if return_to in ("provision",):
            context["return_to"] = return_to
        if is_modal:
            return templates.TemplateResponse(
                "admin/network/onts/_assign_subscriber_modal.html", context
            )
        return templates.TemplateResponse("admin/network/onts/assign.html", context)

    assert result.ont is not None
    # Redirect back to provision page if that's where the user came from
    if return_to == "provision":
        return RedirectResponse(
            url=f"/admin/network/onts/{result.ont.id}/provision?status=success&message=ONT+assignment+created.",
            status_code=303,
        )
    if is_modal:
        return Response(
            status_code=204,
            headers={
                "HX-Redirect": (
                    f"/admin/network/onts/{result.ont.id}"
                    "?feedback_status=success"
                    "&feedback_message=ONT+assignment+created."
                )
            },
        )
    return _ont_redirect(
        str(result.ont.id),
        status="success",
        message="ONT assignment created.",
    )


@router.get(
    "/onts/available-mgmt-ips",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def available_mgmt_ips_for_vlan(
    request: Request,
    vlan_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX endpoint: return available management IPs for a VLAN as select options."""
    available_ips = web_network_ont_assignments_service.get_available_mgmt_ips_for_vlan(
        db, vlan_id
    )
    options_html = '<option value="">Select an IP address</option>'
    for ip in available_ips:
        options_html += f'<option value="{ip["address"]}">{ip["address"]}</option>'
    if not available_ips:
        options_html = '<option value="">No available IPs in pool</option>'
    return HTMLResponse(content=options_html)


@router.get(
    "/onts/{ont_id}/assignments/{assignment_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_assignment_edit(
    request: Request,
    ont_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    result = web_network_ont_assignments_service.get_assignment_edit_form(
        db,
        ont_id=ont_id,
        assignment_id=assignment_id,
    )
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": result.not_found_message},
            status_code=404,
        )

    assert result.ont is not None
    assert result.assignment is not None
    context = _assignment_form_context(
        request,
        db,
        ont=result.ont,
        action_url=f"/admin/network/onts/{result.ont.id}/assignments/{result.assignment.id}/edit",
        form=result.values,
        mode="edit",
        assignment=result.assignment,
    )
    return templates.TemplateResponse("admin/network/onts/assign.html", context)


@router.post(
    "/onts/{ont_id}/assignments/{assignment_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_assignment_update(
    request: Request,
    ont_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
):
    result = web_network_ont_assignments_service.update_assignment_from_form(
        db,
        ont_id=ont_id,
        assignment_id=assignment_id,
        form=parse_form_data_sync(request),
    )
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": result.not_found_message},
            status_code=404,
        )

    if result.error:
        assert result.ont is not None
        assert result.assignment is not None
        context = _assignment_form_context(
            request,
            db,
            ont=result.ont,
            action_url=f"/admin/network/onts/{result.ont.id}/assignments/{result.assignment.id}/edit",
            error=result.error,
            form=web_network_ont_assignments_service.form_payload(
                result.values or {}, db
            ),
            mode="edit",
            assignment=result.assignment,
        )
        return templates.TemplateResponse("admin/network/onts/assign.html", context)

    assert result.ont is not None
    return _ont_redirect(
        str(result.ont.id),
        tab="operations",
        status="success",
        message="ONT assignment updated.",
    )


@router.post(
    "/onts/{ont_id}/assignments/{assignment_id}/remove",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_assignment_remove(
    request: Request,
    ont_id: str,
    assignment_id: str,
    db: Session = Depends(get_db),
):
    result = web_network_ont_assignments_service.remove_assignment(
        db,
        ont_id=ont_id,
        assignment_id=assignment_id,
        actor_id=web_admin_service.get_actor_id(request),
    )
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    if result.error:
        assert result.ont is not None
        return _ont_redirect(
            str(result.ont.id),
            tab="operations",
            status="error",
            message=result.error,
        )

    assert result.ont is not None
    return _ont_redirect(
        str(result.ont.id),
        tab="operations",
        status="success",
        message="ONT assignment removed.",
    )


@router.get(
    "/onts/{ont_id}/onu-mode",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_onu_mode_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve ONU mode configuration modal partial."""
    try:
        context = ont_web_forms_service.onu_mode_modal_context(db, ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    context["request"] = request
    return templates.TemplateResponse(
        "admin/network/onts/_onu_mode_modal.html", context
    )


@router.post(
    "/onts/{ont_id}/onu-mode",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_onu_mode_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update ONU mode configuration."""
    result = ont_web_forms_service.update_onu_mode_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")
    if result.error:
        return _ont_redirect(
            ont_id,
            status="error",
            message=result.error,
        )

    return _ont_redirect(
        ont_id,
        status="success",
        message="ONU mode settings updated.",
    )


@router.get(
    "/onts/{ont_id}/mgmt-ip",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_mgmt_ip_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve management/VoIP IP modal partial."""
    try:
        context = ont_web_forms_service.mgmt_ip_modal_context(db, ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    context["request"] = request
    return templates.TemplateResponse("admin/network/onts/_mgmt_ip_modal.html", context)


@router.post(
    "/onts/{ont_id}/mgmt-ip",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_mgmt_ip_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Update management/VoIP IP configuration."""
    result = ont_web_forms_service.update_mgmt_ip_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")

    return _ont_redirect(
        ont_id,
        status="success",
        message="Management IP settings updated.",
    )
