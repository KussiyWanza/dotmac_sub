"""Admin web routes for ONT actions and runtime tabs."""

from __future__ import annotations

import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import settings
from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import web_network_ont_charts as web_network_ont_charts_service
from app.services import web_network_ont_topology as web_network_ont_topology_service
from app.services import web_network_ont_tr069 as web_network_ont_tr069_service
from app.services.auth_dependencies import require_permission
from app.services.network.action_logging import log_network_action_result
from app.services.network.ont_scope import can_manage_ont_from_request
from app.services.service_intent_ui_adapter import service_intent_ui_adapter
from app.web.request_parsing import parse_form_data_sync
from app.web.templates import templates

router = APIRouter(prefix="/network", tags=["web-admin-network-ont-actions"])


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


def _sanitize_header_message(message: str) -> str:
    """Sanitize a message for safe inclusion in HTTP headers.

    Removes control characters and non-printable bytes that would cause
    'Invalid HTTP header value' errors from uvicorn/httptools.
    """
    # Remove control characters (0x00-0x1F, 0x7F) and non-ASCII
    return "".join(c for c in message if 0x20 <= ord(c) < 0x7F or c in ("\t",)).strip()


def _toast_headers(message: str, toast_type: str) -> dict[str, str]:
    safe_message = _sanitize_header_message(message)
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": safe_message, "type": toast_type}},
            ensure_ascii=True,
        )
    }


def _ensure_ont_write_scope(
    request: Request, db: Session, ont_id: str
) -> JSONResponse | None:
    if can_manage_ont_from_request(request, db, ont_id):
        return None
    return JSONResponse(
        {"success": False, "message": "ONT scope check failed"},
        status_code=403,
        headers=_toast_headers("ONT scope check failed", "error"),
    )


def _action_json_response(
    *,
    success: bool,
    message: str,
    action: str,
    request: Request | None = None,
    ont_id: str | None = None,
    waiting: bool = False,
    status_code: int | None = None,
    detail: str | None = None,
    hx_refresh: bool = False,
    operation_id: str | None = None,
    status_url: str | None = None,
) -> JSONResponse:
    """Return a consistent JSON contract for ONT action requests."""
    toast_type = "info" if waiting else ("success" if success else "error")
    phase = "waiting" if waiting else ("succeeded" if success else "failed")
    log_network_action_result(
        request=request,
        resource_type="ont",
        resource_id=ont_id,
        action=action,
        success=success,
        message=message,
        waiting=waiting,
    )
    headers = _toast_headers(message, toast_type)
    if hx_refresh:
        headers["HX-Refresh"] = "true"
    payload = {
        "success": success or waiting,
        "message": message,
        "phase": phase,
        "waiting": waiting,
        "operation": {
            "action": action,
            "phase": phase,
            "detail": detail or message,
        },
    }
    if operation_id:
        payload["operation_id"] = operation_id
    if status_url:
        payload["status_url"] = status_url
    return JSONResponse(
        payload,
        status_code=status_code
        if status_code is not None
        else (200 if success else (202 if waiting else 400)),
        headers=headers,
    )


def _action_result_response(
    *,
    result: object,
    request: Request,
    ont_id: str,
    action: str,
) -> JSONResponse:
    """Return a JSON response from an ActionResult-like object.

    Delegates to _action_json_response for consistent response format.
    """
    success = bool(getattr(result, "success", False))
    message = str(getattr(result, "message", "Action failed"))
    waiting = bool(getattr(result, "waiting", False))
    return _action_json_response(
        success=success,
        message=message,
        action=action,
        request=request,
        ont_id=ont_id,
        waiting=waiting,
    )


def _lan_ports_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    """Return the LAN ports controls partial with current port status."""
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    no_tr069 = not bool(observed_intent.get("available"))
    ethernet_ports = list(observed.get("ethernet_ports", []) or [])

    context = {
        "request": request,
        "ont_id": ont_id,
        "ethernet_ports": ethernet_ports,
        "no_tr069": no_tr069,
    }
    response = templates.TemplateResponse(
        "admin/network/onts/_lan_ports_controls.html", context
    )
    if toast_message:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": toast_message, "type": toast_type}},
            ensure_ascii=True,
        )
    return response


def _refresh_runtime_for_partial(
    db: Session,
    ont_id: str,
    *,
    request: Request,
) -> tuple[bool, str]:
    """Refresh ONT runtime before returning an HTMX status partial."""
    result = web_network_ont_actions_service.execute_refresh(
        db,
        ont_id,
        request=request,
    )
    success = bool(getattr(result, "success", False))
    message = str(getattr(result, "message", "Refresh failed."))
    if success:
        try:
            from app.services.genieacs_service_intent import genieacs_service_intent

            genieacs_service_intent.refresh_observed_summary_for_ont(
                db,
                ont_id=ont_id,
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            success = False
            message = (
                f"Runtime refresh completed, but observed data reload failed: {exc}"
            )
    return success, message


def _ethernet_ports_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    """Return the Ethernet ports status partial with current port state."""
    context = _base_context(request, db, active_page="onts")
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    context["ont_id"] = ont_id
    context["ethernet_ports"] = observed.get("ethernet_ports", [])
    response = templates.TemplateResponse(
        "admin/network/onts/_ethernet_ports_partial.html", context
    )
    if toast_message:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": toast_message, "type": toast_type}},
            ensure_ascii=True,
        )
    return response


def _host_has_display_details(host: dict[str, object]) -> bool:
    """Return true when a LAN host row contains useful display data."""
    for key in (
        "host_name",
        "HostName",
        "ip_address",
        "IPAddress",
        "mac_address",
        "MACAddress",
        "interface_type",
        "InterfaceType",
    ):
        value = host.get(key)
        if str(value or "").strip():
            return True
    return False


def _hosts_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    """Return the connected hosts partial with blank ACS placeholders removed."""
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    lan_hosts = observed.get("lan_hosts", [])

    hosts = []
    placeholder_count = 0
    for host in lan_hosts if isinstance(lan_hosts, list) else []:
        if not isinstance(host, dict):
            continue
        if not _host_has_display_details(host):
            placeholder_count += 1
            continue
        hosts.append(
            {
                "hostname": host.get("host_name") or host.get("HostName") or "-",
                "mac_address": host.get("mac_address") or host.get("MACAddress") or "-",
                "ip_address": host.get("ip_address") or host.get("IPAddress") or "-",
                "interface": host.get("interface_type")
                or host.get("InterfaceType")
                or "-",
                "active": str(host.get("active", "")).lower()
                not in {"false", "0", "no"},
            }
        )

    context = _base_context(request, db, active_page="onts")
    context["hosts"] = hosts
    context["ont_id"] = ont_id
    context["host_placeholder_count"] = placeholder_count
    response = templates.TemplateResponse(
        "admin/network/onts/_hosts_table.html", context
    )
    if toast_message:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": toast_message, "type": toast_type}},
            ensure_ascii=True,
        )
    return response


@router.post(
    "/onts/{ont_id}/reboot",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_reboot(
    request: Request,
    ont_id: str,
    source: str = "auto",
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Send reboot command to ONT via the selected transport."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied

    source_normalized = (source or "auto").strip().lower()
    if source_normalized in {"olt", "omci"}:
        ok, msg = web_network_ont_actions_service.execute_omci_reboot(
            db,
            ont_id,
            initiated_by=None,
        )
        return _action_json_response(
            success=ok,
            message=msg,
            action="OLT Reboot",
            request=request,
            ont_id=ont_id,
            status_code=200 if ok else 400,
        )

    result = web_network_ont_actions_service.execute_reboot(db, ont_id, request=request)
    if (
        source_normalized in {"auto", ""}
        and not result.success
        and not getattr(result, "waiting", False)
    ):
        ok, msg = web_network_ont_actions_service.execute_omci_reboot(
            db,
            ont_id,
            initiated_by=None,
        )
        fallback_msg = (
            f"TR-069 reboot failed ({result.message}); OLT reboot sent: {msg}"
            if ok
            else f"TR-069 reboot failed ({result.message}); OLT reboot also failed: {msg}"
        )
        return _action_json_response(
            success=ok,
            message=fallback_msg,
            action="Reboot ONT",
            request=request,
            ont_id=ont_id,
            status_code=200 if ok else 400,
        )

    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Reboot ONT",
        request=request,
        ont_id=ont_id,
        waiting=result.waiting,
    )


@router.post(
    "/onts/{ont_id}/reauthorize",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_reauthorize(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Quick re-authorize ONT on OLT (force mode)."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied

    result = web_network_ont_actions_service.execute_reauthorize(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Re-authorize ONT",
        request=request,
        ont_id=ont_id,
    )


@router.post(
    "/onts/{ont_id}/refresh",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_refresh(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Queue an OLT/TR-069 status refresh for an ONT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.queue_refresh(db, ont_id, request=request)
    status_url = (
        f"/admin/network/onts/{ont_id}/refresh-status"
        f"?operation_id={result.operation_id}"
        if result.operation_id
        else None
    )
    return _action_json_response(
        success=result.success and not result.waiting,
        message=result.message,
        action="Refresh ONT",
        request=request,
        ont_id=ont_id,
        waiting=result.waiting,
        operation_id=result.operation_id,
        status_url=status_url,
    )


@router.post(
    "/onts/{ont_id}/config/refresh",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_config_refresh(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Refresh the stored last-known TR-069 config snapshot."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_config_snapshot_refresh(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Refresh ONT Config Snapshot",
        request=request,
        ont_id=ont_id,
        hx_refresh=result.success,
    )


@router.get(
    "/onts/{ont_id}/config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Fetch and display running config from ONT."""
    context = _base_context(request, db, active_page="onts")
    context.update(web_network_ont_actions_service.running_config_context(db, ont_id))
    return templates.TemplateResponse(
        "admin/network/onts/_config_partial.html", context
    )


@router.get(
    "/onts/{ont_id}/olt-config",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_olt_side_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Fetch ONT config from OLT side via SSH (works without GenieACS)."""
    context = {
        "request": request,
        **web_network_ont_actions_service.olt_side_config_context(db, ont_id),
    }
    return templates.TemplateResponse(
        "admin/network/onts/_olt_side_config.html",
        context,
    )


@router.get(
    "/onts/{ont_id}/olt-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_olt_status(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Query the OLT directly for ONT registration state (GPON layer)."""
    context = {
        "request": request,
        **web_network_ont_actions_service.olt_status_context(db, ont_id),
    }
    return templates.TemplateResponse(
        "admin/network/onts/_olt_status.html",
        context,
    )


@router.post(
    "/onts/{ont_id}/return-to-inventory",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_return_to_inventory(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> Response:
    """Reset an ONT to reusable inventory state."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.return_to_inventory_for_web(
        db,
        ont_id,
        request=request,
    )
    if result.success:
        message = result.message or "ONT returned to inventory"
        target = "/admin/network/onts?view=unconfigured"
        if request.headers.get("hx-request") == "true":
            return Response(
                status_code=200,
                headers={
                    **_toast_headers(message, "success"),
                    "HX-Redirect": target,
                },
            )
        return RedirectResponse(
            f"{target}&status=success&message={quote_plus(message)}",
            status_code=303,
        )

    return Response(
        status_code=400,
        headers=_toast_headers(
            result.message or "Failed to return ONT to inventory",
            "error",
        ),
    )


@router.post(
    "/onts/{ont_id}/factory-reset",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_factory_reset(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Send factory reset command to ONT via GenieACS."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_factory_reset(
        db, ont_id, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Factory Reset",
    )


@router.post(
    "/onts/{ont_id}/firmware-upgrade",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_firmware_upgrade(
    request: Request,
    ont_id: str,
    firmware_image_id: str = Form(""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Trigger firmware upgrade on ONT via TR-069 Download RPC."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    if not firmware_image_id:
        return _action_json_response(
            success=False,
            message="No firmware image selected",
            action="Firmware Upgrade",
            request=request,
            ont_id=ont_id,
        )

    result = web_network_ont_actions_service.firmware_upgrade(
        db, ont_id, firmware_image_id, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Firmware Upgrade",
    )


@router.post(
    "/onts/{ont_id}/wifi-ssid",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_wifi_ssid(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
    ssid: str = Form(""),
) -> JSONResponse:
    """Set WiFi SSID on ONT via GenieACS TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    # Also accept ssid from query params (used by TR-069 tab Alpine.js modal)
    if not ssid:
        ssid = request.query_params.get("ssid", "")
    result = web_network_ont_actions_service.set_wifi_ssid(
        db, ont_id, ssid, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WiFi SSID",
    )


@router.post(
    "/onts/{ont_id}/wifi-password",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_wifi_password(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
    password: str = Form(""),
) -> JSONResponse:
    """Set WiFi password on ONT via the reconciler (sync mode).

    Updates ``OntDesiredState.wifi_password_ref`` durably; the actual push
    to the device happens on the next BOOTSTRAP event (e.g. after a factory
    reset). Use ``/wifi-password/push`` to force an immediate push.
    """
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.set_wifi_password(
        db, ont_id, password, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WiFi Password",
    )


@router.post(
    "/onts/{ont_id}/wifi-password/push",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_force_push_wifi_password(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
    password: str = Form(""),
) -> JSONResponse:
    """Force an immediate WiFi password push to the device.

    Uses ``reconcile_ont(mode=bootstrap)`` so the planner emits the
    ``AcsSetWifiPassword`` action regardless of whether the device is
    currently present and observed. Restores legacy "push every time"
    semantics for operators who need them.
    """
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.force_push_wifi_password(
        db, ont_id, password, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Force-Push WiFi Password",
    )


@router.post(
    "/onts/{ont_id}/force-resync",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_force_resync(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Force a sweep-mode reconcile — clears an ``out_of_sync`` row.

    Sync-mode endpoints refuse against ``out_of_sync`` rows so an operator
    has to acknowledge the prior failure before mutating state further.
    This endpoint is the explicit acknowledgement: re-run reconciliation
    of the existing desired state against the live OLT/ACS state.
    """
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.force_resync_ont(
        db, ont_id, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Force Resync ONT",
    )


@router.post(
    "/onts/{ont_id}/wifi-config",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_wifi_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Set WiFi radio, SSID, security, channel, and password via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    enabled_raw = _form_str(form, "enabled").strip().lower()
    enabled = None
    if enabled_raw:
        enabled = enabled_raw in {"true", "1", "yes", "on", "enabled"}
    channel_raw = _form_str(form, "channel").strip()
    channel = int(channel_raw) if channel_raw.isdigit() else None
    result = web_network_ont_actions_service.set_wifi_config(
        db,
        ont_id,
        enabled=enabled,
        ssid=_form_str(form, "ssid").strip() or None,
        password=_form_str(form, "password").strip() or None,
        channel=channel,
        security_mode=_form_str(form, "security_mode").strip() or None,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WiFi Config",
    )


@router.post(
    "/onts/{ont_id}/lan-port",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_toggle_lan_port(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> Response:
    """Toggle LAN port on ONT via GenieACS TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    port_str = request.query_params.get("port") or _form_str(form, "port", "1")
    enabled_str = request.query_params.get("enabled") or _form_str(
        form, "enabled", "true"
    )
    try:
        port = int(port_str)
    except ValueError:
        port = 1
    enabled = enabled_str.lower() in ("true", "1", "yes")
    result = web_network_ont_actions_service.toggle_lan_port(
        db, ont_id, port, enabled, request=request
    )
    if request.headers.get("HX-Request"):
        if not result.success:
            log_network_action_result(
                request=request,
                resource_type="ont",
                resource_id=ont_id,
                action="Toggle LAN Port",
                success=result.success,
                message=result.message,
                waiting=getattr(result, "waiting", False),
            )
        hx_target = request.headers.get("HX-Target", "")
        if hx_target == "ethernet-ports-panel":
            return _ethernet_ports_partial_response(
                request,
                db,
                ont_id,
                toast_message=result.message,
                toast_type="success" if result.success else "error",
            )
        return _lan_ports_partial_response(
            request,
            db,
            ont_id,
            toast_message=result.message,
            toast_type="success" if result.success else "error",
        )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Toggle LAN Port",
    )


@router.post(
    "/onts/{ont_id}/lan-config",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_lan_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set LAN gateway and DHCP server settings on ONT via GenieACS TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    lan_ip = _form_str(form, "lan_ip").strip() or None
    lan_subnet = _form_str(form, "lan_subnet").strip() or None
    dhcp_enabled_raw = _form_str(form, "dhcp_enabled").strip().lower()
    dhcp_enabled = None
    if dhcp_enabled_raw:
        dhcp_enabled = dhcp_enabled_raw in {"true", "1", "yes", "on", "enabled"}
    dhcp_start = _form_str(form, "dhcp_start").strip() or None
    dhcp_end = _form_str(form, "dhcp_end").strip() or None
    result = web_network_ont_actions_service.set_lan_config(
        db,
        ont_id,
        lan_ip=lan_ip,
        lan_subnet=lan_subnet,
        dhcp_enabled=dhcp_enabled,
        dhcp_start=dhcp_start,
        dhcp_end=dhcp_end,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set LAN Config",
    )


@router.post(
    "/onts/{ont_id}/wan-remote-access",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_wan_remote_access(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Enable or disable WAN remote access via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    enabled = _form_str(form, "enabled").strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
        "enabled",
    }
    result = web_network_ont_actions_service.set_wan_remote_access(
        db, ont_id, enabled=enabled, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WAN Remote Access",
    )


@router.post(
    "/onts/{ont_id}/bind-internet-wan",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_bind_internet_wan(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Bind active PPP internet WAN to customer-facing LAN/WiFi interfaces."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)

    def enabled(key: str, default: bool = False) -> bool:
        raw = _form_str(form, key, "true" if default else "").strip().lower()
        return raw in {"true", "1", "yes", "on", "enabled"}

    result = web_network_ont_actions_service.bind_internet_wan(
        db,
        ont_id,
        ssid1=enabled("ssid1"),
        lan1=enabled("lan1"),
        lan2=enabled("lan2"),
        lan3=enabled("lan3"),
        lan4=enabled("lan4"),
        request=request,
    )
    success = bool(getattr(result, "success", False))
    return _action_json_response(
        success=success,
        message=str(getattr(result, "message", "Internet WAN bind failed")),
        action="Bind Internet WAN",
        request=request,
        ont_id=ont_id,
        waiting=bool(getattr(result, "waiting", False)),
        hx_refresh=success,
    )


@router.post(
    "/onts/{ont_id}/mgmt-remote-access",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_mgmt_remote_access(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Enable or disable management-side remote access via OLT IPHOST + TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    enabled = _form_str(form, "enabled").strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
        "enabled",
    }
    result = web_network_ont_actions_service.set_mgmt_remote_access(
        db, ont_id, enabled=enabled, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set MGMT Remote Access",
    )


@router.post(
    "/onts/{ont_id}/voip-config",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_voip_config(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set VoIP enabled status on ONT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    voip_enabled_raw = _form_str(form, "voip_enabled").strip()
    voip_enabled = voip_enabled_raw in {"true", "1", "yes", "on"}

    result = web_network_ont_actions_service.set_voip_enabled(
        db, ont_id, enabled=voip_enabled, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set VoIP Config",
    )


@router.post(
    "/onts/{ont_id}/web-credentials",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_web_credentials(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set ONT web login credentials via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    username = _form_str(form, "username").strip()
    password = _form_str(form, "password").strip()
    if not username or not password:
        return _action_json_response(
            success=False,
            message="Web username and password are required",
            action="Set Web Credentials",
            request=request,
            ont_id=ont_id,
        )
    result = web_network_ont_actions_service.set_web_credentials(
        db, ont_id, username=username, password=password, request=request
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set Web Credentials",
    )


@router.post(
    "/onts/{ont_id}/connection-request-credentials",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_connection_request_credentials(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> JSONResponse:
    """Set ONT ACS connection-request credentials via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    username = _form_str(form, "username").strip()
    password = _form_str(form, "password").strip()
    default_interval = settings.tr069_periodic_inform_interval
    interval_raw = _form_str(
        form, "periodic_inform_interval", str(default_interval)
    ).strip()
    interval = int(interval_raw) if interval_raw.isdigit() else default_interval
    if not username or not password:
        return _action_json_response(
            success=False,
            message="Connection request username and password are required",
            action="Set Connection Request Credentials",
            request=request,
            ont_id=ont_id,
        )
    result = web_network_ont_actions_service.set_connection_request_credentials(
        db,
        ont_id,
        username=username,
        password=password,
        periodic_inform_interval=interval,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set Connection Request Credentials",
    )


@router.get(
    "/onts/{ont_id}/pppoe-password",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_reveal_pppoe_password(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Decrypt and return the stored PPPoE password for verification."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    password, found = web_network_ont_actions_service.reveal_stored_pppoe_password(
        db, ont_id, request=request
    )
    if not found:
        return JSONResponse({"password": ""}, status_code=404)
    return JSONResponse({"password": password})


@router.get(
    "/onts/{ont_id}/running-config",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_running_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Fetch ONT-specific configuration from the OLT via SSH.

    Returns the service-port and ONT info from the OLT CLI.
    Falls back to cached data if OLT is unreachable.
    """
    result = web_network_ont_actions_service.fetch_olt_running_config(db, ont_id)
    return templates.TemplateResponse(
        "admin/network/onts/_running_config_modal.html",
        {
            "request": request,
            "ont": result.ont,
            "olt": result.olt,
            "error": result.error,
            "config_text": result.config_text,
            "from_cache": result.from_cache,
            "fetched_at": result.fetched_at,
        },
    )


@router.post(
    "/onts/{ont_id}/ping-diagnostic",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_ping_diagnostic(
    request: Request,
    ont_id: str,
    host: str = Form(""),
    count: int = Form(4),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Run ping diagnostic from ONT via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.run_ping_diagnostic(
        db, ont_id, host, count, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Run Ping Diagnostic",
        request=request,
        ont_id=ont_id,
    )


@router.post(
    "/onts/{ont_id}/traceroute-diagnostic",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_traceroute_diagnostic(
    request: Request,
    ont_id: str,
    host: str = Form(""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Run traceroute diagnostic from ONT via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.run_traceroute_diagnostic(
        db, ont_id, host, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Run Traceroute Diagnostic",
        request=request,
        ont_id=ont_id,
    )


@router.post(
    "/onts/{ont_id}/connection-request",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_connection_request(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Send a TR-069 connection request to an ONT for on-demand management."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.execute_connection_request(
        db, ont_id, request=request
    )
    return _action_json_response(
        success=result.success,
        message=result.message,
        action="Connection Request",
        request=request,
        ont_id=ont_id,
        waiting=result.waiting,
    )


@router.get(
    "/onts/{ont_id}/lan-hosts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_lan_hosts(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: LAN hosts connected to an ONT."""
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    lan_hosts = observed.get("lan_hosts", [])
    context = _base_context(request, db, active_page="onts")
    context["lan_hosts"] = lan_hosts
    return templates.TemplateResponse(
        "admin/network/onts/_lan_hosts_partial.html", context
    )


@router.get(
    "/onts/{ont_id}/ethernet-ports",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_ethernet_ports(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Ethernet port status for an ONT."""
    observed_intent = service_intent_ui_adapter.load_acs_observed_service_intent(
        db, ont_id=ont_id
    )
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    ethernet_ports = observed.get("ethernet_ports", [])
    context = _base_context(request, db, active_page="onts")
    context["ont_id"] = ont_id
    context["ethernet_ports"] = ethernet_ports
    return templates.TemplateResponse(
        "admin/network/onts/_ethernet_ports_partial.html", context
    )


@router.get(
    "/onts/{ont_id}/tr069",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_tr069_detail(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: TR-069 device details for ONT detail page tab."""
    data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse("admin/network/onts/_tr069_partial.html", context)


@router.get(
    "/onts/{ont_id}/charts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_charts(
    request: Request,
    ont_id: str,
    time_range: str = "24h",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Traffic and signal charts for ONT detail page."""
    data = web_network_ont_charts_service.charts_tab_data(db, ont_id, time_range)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_charts_partial.html", context
    )


@router.post(
    "/onts/{ont_id}/charts/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_charts_refresh(
    request: Request,
    ont_id: str,
    time_range: str = "24h",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Refresh ONT runtime and return the charts partial."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied  # type: ignore[return-value]
    refresh_ok, refresh_msg = _refresh_runtime_for_partial(
        db,
        ont_id,
        request=request,
    )
    data = web_network_ont_charts_service.charts_tab_data(db, ont_id, time_range)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    response = templates.TemplateResponse(
        "admin/network/onts/_charts_partial.html", context
    )
    response.headers["HX-Trigger"] = json.dumps(
        {
            "showToast": {
                "message": refresh_msg,
                "type": "success" if refresh_ok else "error",
            }
        },
        ensure_ascii=True,
    )
    return response


@router.post(
    "/onts/{ont_id}/lan-ports-status/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_lan_ports_status_refresh(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Refresh ONT runtime and return the LAN port status partial."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied  # type: ignore[return-value]
    refresh_ok, refresh_msg = _refresh_runtime_for_partial(
        db,
        ont_id,
        request=request,
    )
    return _lan_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=refresh_msg,
        toast_type="success" if refresh_ok else "error",
    )


@router.get(
    "/onts/{ont_id}/topology",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_topology(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Fiber path topology for ONT detail page."""
    data = web_network_ont_topology_service.topology_tab_data(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_topology_partial.html", context
    )


@router.get(
    "/onts/{ont_id}/operational-health",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_operational_health(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: operational readiness/actions for the ONT detail page."""
    context: dict[str, object] = {"request": request}
    context.update(
        web_network_ont_actions_service.operational_health_context(db, ont_id)
    )
    return templates.TemplateResponse(
        "admin/network/onts/_operational_health.html", context
    )


@router.post(
    "/onts/{ont_id}/reconcile",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_reconcile(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Run OLT/ACS reconciliation and return refreshed operational panel."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = web_network_ont_actions_service.reconcile_operational_state(
        db,
        ont_id,
        request=request,
    )
    context: dict[str, object] = {"request": request}
    context.update(
        web_network_ont_actions_service.operational_health_context(
            db,
            ont_id,
            message=result.message,
            message_type="success" if result.success else "error",
        )
    )
    response = templates.TemplateResponse(
        "admin/network/onts/_operational_health.html", context
    )
    response.headers["HX-Trigger"] = json.dumps(
        {
            "showToast": {
                "message": result.message,
                "type": "success" if result.success else "error",
            }
        }
    )
    return response


@router.post(
    "/onts/{ont_id}/actions/omci-reboot",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_omci_reboot(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Reboot ONT via OMCI through the OLT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    ok, msg = web_network_ont_actions_service.execute_omci_reboot(db, ont_id)
    return _action_json_response(
        success=ok,
        message=msg,
        action="OMCI Reboot",
        request=request,
        ont_id=ont_id,
        status_code=200 if ok else 400,
    )


@router.post(
    "/onts/{ont_id}/actions/configure-mgmt-ip",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_configure_mgmt_ip(
    request: Request,
    ont_id: str,
    ip_mode: str = Form(default="dhcp"),
    ip_address: str = Form(default=""),
    subnet: str = Form(default=""),
    gateway: str = Form(default=""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure ONT management IP via OLT IPHOST command."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    ok, msg = web_network_ont_actions_service.configure_management_ip(
        db,
        ont_id,
        ip_mode,
        ip_address=ip_address or None,
        subnet=subnet or None,
        gateway=gateway or None,
    )
    return _action_json_response(
        success=ok,
        message=msg,
        action="Configure Management IP",
        request=request,
        ont_id=ont_id,
        status_code=200 if ok else 400,
    )


@router.post(
    "/onts/{ont_id}/actions/bind-tr069-profile",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_bind_tr069_profile(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bind TR-069 server profile to ONT via OLT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    ok, msg = web_network_ont_actions_service.bind_tr069_profile(db, ont_id)
    return _action_json_response(
        success=ok,
        message=msg,
        action="Bind TR-069 Profile",
        request=request,
        ont_id=ont_id,
        status_code=200 if ok else 400,
    )


# ── Config Snapshots ──────────────────────────────────────────────────────────


@router.get(
    "/onts/{ont_id}/config-snapshots",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_config_snapshots(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Return the current config snapshot list."""
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "ont_id": ont_id,
            "config_snapshots": web_network_ont_actions_service.list_config_snapshots(
                db,
                ont_id,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_list.html",
        context,
    )


@router.post(
    "/onts/{ont_id}/config-snapshot",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_capture_config_snapshot(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Capture a new config snapshot from TR-069 and return updated list."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    label = _form_str(form, "label").strip() or None
    snapshot_context, error_msg = (
        web_network_ont_actions_service.capture_config_snapshot_list_context(
            db,
            ont_id=ont_id,
            label=label,
        )
    )
    context = _base_context(request, db, active_page="onts")
    context.update(snapshot_context)
    response = templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_list.html", context
    )
    if error_msg:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": error_msg, "type": "error"}}
        )
    else:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": "Config snapshot saved.", "type": "success"}}
        )
    return response


@router.get(
    "/onts/{ont_id}/config-snapshots/{snapshot_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_view_config_snapshot(
    request: Request,
    ont_id: str,
    snapshot_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """View a single config snapshot detail in a slide-over."""
    context = _base_context(request, db, active_page="onts")
    context.update(
        web_network_ont_actions_service.config_snapshot_detail_context(
            db,
            ont_id=ont_id,
            snapshot_id=snapshot_id,
        )
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_detail.html", context
    )


@router.delete(
    "/onts/{ont_id}/config-snapshots/{snapshot_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_delete_config_snapshot(
    request: Request,
    ont_id: str,
    snapshot_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Delete a config snapshot and return updated list."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    context = _base_context(request, db, active_page="onts")
    context.update(
        web_network_ont_actions_service.delete_config_snapshot_list_context(
            db,
            ont_id=ont_id,
            snapshot_id=snapshot_id,
        )
    )
    return templates.TemplateResponse(
        "admin/network/onts/_config_snapshot_list.html", context
    )


# ---------------------------------------------------------------------------
# TR-069 WAN Configuration Routes
# ---------------------------------------------------------------------------


@router.post(
    "/onts/{ont_id}/wan/pppoe-credentials",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_pppoe_credentials(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Write the CPE's PPPoE dialer values via TR-069.

    This does not modify RADIUS. The subscriber's authoritative access
    credential is owned by ``access.radius_projection``; this endpoint only
    changes what the ONT dials with, and the dialer reconciler converges it
    back onto the authoritative credential.
    """
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    username = _form_str(form, "pppoe_username").strip()
    password = _form_str(form, "pppoe_password").strip()
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_vlan_raw = _form_str(form, "wan_vlan").strip()

    if not username or not password:
        return _action_json_response(
            success=False,
            message="PPPoE username and password are required",
            action="Set PPPoE Credentials",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    wan_vlan = int(wan_vlan_raw) if wan_vlan_raw.isdigit() else None

    result = web_network_ont_actions_service.set_pppoe_credentials(
        db,
        ont_id,
        username=username,
        password=password,
        instance_index=instance_index,
        wan_vlan=wan_vlan,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set PPPoE Credentials",
    )


@router.post(
    "/onts/{ont_id}/wan/dhcp",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_wan_dhcp(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure WAN for DHCP mode via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_vlan_raw = _form_str(form, "wan_vlan").strip()

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    wan_vlan = int(wan_vlan_raw) if wan_vlan_raw.isdigit() else None

    result = web_network_ont_actions_service.set_wan_dhcp(
        db,
        ont_id,
        instance_index=instance_index,
        wan_vlan=wan_vlan,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WAN DHCP",
    )


@router.post(
    "/onts/{ont_id}/wan/static",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_wan_static(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure WAN for static IP mode via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    ip_address = _form_str(form, "ip_address").strip()
    subnet_mask = _form_str(form, "subnet_mask").strip()
    gateway = _form_str(form, "gateway").strip()
    dns_servers_raw = _form_str(form, "dns_servers").strip()
    instance_index_raw = _form_str(form, "instance_index").strip()

    if not ip_address or not subnet_mask or not gateway:
        return _action_json_response(
            success=False,
            message="IP address, subnet mask, and gateway are required",
            action="Set WAN Static",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    # ``set_wan_static`` takes a comma-joined string; collapse the list back
    # for the API call (we still split into a list above for validation).
    dns_servers = (
        ",".join(s.strip() for s in dns_servers_raw.split(",") if s.strip())
        if dns_servers_raw
        else None
    )

    result = web_network_ont_actions_service.set_wan_static(
        db,
        ont_id,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WAN Static",
    )


@router.post(
    "/onts/{ont_id}/wan/config",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_wan_config(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure WAN mode and settings via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    wan_mode = _form_str(form, "wan_mode").strip().lower()
    pppoe_username = _form_str(form, "pppoe_username").strip() or None
    pppoe_password = _form_str(form, "pppoe_password").strip() or None
    ip_address = _form_str(form, "ip_address").strip() or None
    subnet_mask = _form_str(form, "subnet_mask").strip() or None
    gateway = _form_str(form, "gateway").strip() or None
    dns_servers_raw = _form_str(form, "dns_servers").strip()
    instance_index_raw = _form_str(form, "instance_index").strip()
    wan_vlan_raw = _form_str(form, "wan_vlan").strip()

    if wan_mode not in {"pppoe", "dhcp", "static", "bridge"}:
        return _action_json_response(
            success=False,
            message="Invalid WAN mode",
            action="Set WAN Config",
            request=request,
            ont_id=ont_id,
        )

    instance_index = int(instance_index_raw) if instance_index_raw.isdigit() else 1
    wan_vlan = int(wan_vlan_raw) if wan_vlan_raw.isdigit() else None
    # ``set_wan_config`` takes a list[str] | None (unlike ``set_wan_static``
    # used in the other endpoint, which takes a comma-joined string).
    dns_servers = (
        [s.strip() for s in dns_servers_raw.split(",") if s.strip()]
        if dns_servers_raw
        else None
    )

    result = web_network_ont_actions_service.set_wan_config(
        db,
        ont_id,
        wan_mode=wan_mode,
        pppoe_username=pppoe_username,
        pppoe_password=pppoe_password,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
        wan_vlan=wan_vlan,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set WAN Config",
    )


@router.post(
    "/onts/{ont_id}/http-management",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_set_http_management(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Enable or disable HTTP management interface via TR-069."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    form = parse_form_data_sync(request)
    enabled_raw = _form_str(form, "enabled").strip().lower()
    port_raw = _form_str(form, "port").strip()

    enabled = enabled_raw in {"true", "1", "yes", "on", "enabled"}
    port = int(port_raw) if port_raw.isdigit() else 80

    result = web_network_ont_actions_service.set_http_management(
        db,
        ont_id,
        enabled=enabled,
        port=port,
        request=request,
    )
    return _action_result_response(
        result=result,
        request=request,
        ont_id=ont_id,
        action="Set HTTP Management",
    )


# =============================================================================
# ONT Decommission Routes
# =============================================================================


@router.get(
    "/onts/{ont_id}/decommission",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:admin"))],
)
def ont_decommission_preview(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show simplified decommission confirmation modal."""
    from app.services.network.ont_decommission import (
        DECOMMISSION_REASONS,
        preview_decommission,
    )

    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        context = _base_context(request, db, active_page="onts")
        context["error"] = "ONT scope check failed"
        return templates.TemplateResponse(
            "admin/network/onts/_decommission_modal.html", context
        )

    preview = preview_decommission(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(
        {
            "preview": preview.to_dict(),
            "ont_id": ont_id,
            "decommission_reasons": DECOMMISSION_REASONS,
        }
    )
    return templates.TemplateResponse(
        "admin/network/onts/_decommission_modal.html", context
    )


@router.post(
    "/onts/{ont_id}/decommission",
    dependencies=[Depends(require_permission("network:admin"))],
)
def ont_decommission_execute(
    request: Request,
    ont_id: str,
    reason: str = Form("hardware_fault"),
    remove_from_acs: bool = Form(True),
    deauthorize_on_olt: bool = Form(True),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Execute ONT decommission.

    Simplified flow - single click decommission for NOC efficiency.
    Requires network:admin permission.
    """
    from app.services.network.ont_decommission import decommission_ont_audited

    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied

    result = decommission_ont_audited(
        db,
        ont_id,
        reason=reason,
        confirm=True,  # Simplified flow - modal click is confirmation
        remove_from_acs=remove_from_acs,
        deauthorize_on_olt=deauthorize_on_olt,
        request=request,
    )

    toast_type = "success" if result.success else "error"
    status_code = 200 if result.success else 400
    return JSONResponse(
        result.to_dict(),
        status_code=status_code,
        headers=_toast_headers(result.message, toast_type),
    )


# ──────────────────────────────────────────────────────────────────────────────
# ONT Detail Tab Endpoints (for tabbed UI)
# ──────────────────────────────────────────────────────────────────────────────


@router.get(
    "/onts/{ont_id}/hosts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_hosts_tab(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Connected hosts table for the Hosts tab."""
    return _hosts_partial_response(request, db, ont_id)


@router.post(
    "/onts/{ont_id}/hosts/refresh",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_hosts_refresh(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Refresh ONT runtime and return the connected hosts partial."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied  # type: ignore[return-value]
    refresh_ok, refresh_msg = _refresh_runtime_for_partial(
        db,
        ont_id,
        request=request,
    )
    return _hosts_partial_response(
        request,
        db,
        ont_id,
        toast_message=refresh_msg,
        toast_type="success" if refresh_ok else "error",
    )


@router.get(
    "/onts/{ont_id}/tr069-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_tr069_status_modal(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: TR-069/ACS status modal content."""
    data = web_network_ont_tr069_service.tr069_tab_data(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_tr069_status_modal.html", context
    )


@router.get(
    "/onts/{ont_id}/refresh-status",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_refresh_status_get(
    request: Request,
    ont_id: str,
    operation_id: str = Query(min_length=1),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return read-only progress for a queued ONT status refresh."""
    result = web_network_ont_actions_service.refresh_operation_status(
        db, ont_id, operation_id
    )
    return JSONResponse(result)
