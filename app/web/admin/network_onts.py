"""Admin network ONT web routes."""

import json
import logging
import uuid
from typing import Any, cast

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.models.network import OntAuthorizationStatus
from app.services import network as network_service
from app.services import web_admin as web_admin_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_ont_actions as web_network_ont_actions_service
from app.services import web_network_onts as web_network_onts_service
from app.services import web_network_operations as web_network_operations_service
from app.services import web_network_service_ports as web_network_service_ports_service
from app.services.audit_helpers import build_audit_activities
from app.services.auth_dependencies import has_permission, require_permission
from app.services.catalog.ip_block_choices import IpBlockPrefix
from app.services.domain_errors import DomainError
from app.services.network import ont_web_forms as ont_web_forms_service
from app.services.network.action_logging import log_network_action_result
from app.services.network.ont_service_configuration import (
    AdvancedConfigurationChange,
    ConfigureOntServiceCommand,
    LanConfigurationChange,
    ManagementConfigurationChange,
    OntConfigurationChange,
    OntConfigurationSection,
    RetryOntServiceConfigurationCommand,
    WanConfigurationChange,
    WifiConfigurationChange,
    configure_ont_service,
    get_ont_service_configuration_projection,
    retry_ont_service_configuration,
)
from app.services.owner_commands import CommandContext
from app.web.request_parsing import parse_form_data_sync
from app.web.templates import templates

router = APIRouter(prefix="/network", tags=["web-admin-network"])


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
        "can_redrive_network_operations": bool(auth)
        and has_permission(auth, db, "network:operation:redrive"),
    }


def _service_ports_partial_response(
    request: Request,
    db: Session,
    ont_id: str,
    *,
    toast_message: str | None = None,
    toast_type: str = "success",
) -> HTMLResponse:
    context = web_network_service_ports_service.list_context(db, ont_id)
    response = templates.TemplateResponse(
        "admin/network/onts/_service_ports_tab.html",
        {"request": request, **context},
    )
    if toast_message:
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": toast_message, "type": toast_type}}
        )
    return response


def _toast_headers(message: str, toast_type: str) -> dict[str, str]:
    """Build latin-1-safe HX-Trigger headers for toast notifications."""
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": message, "type": toast_type}},
            ensure_ascii=True,
        )
    }


def _configure_push_scope_sections(
    push_scope: str | None,
) -> tuple[bool, bool, bool, bool]:
    """Return WAN/LAN/management/WiFi inclusion flags for a configure submit."""
    push_scope_value = (push_scope or "").strip().lower()
    if push_scope_value == "all":
        raise HTTPException(
            status_code=400,
            detail=(
                "Apply All is not supported. Apply one ONT configuration "
                "section at a time."
            ),
        )
    if push_scope_value not in {
        "wan",
        "lan",
        "management",
        "mgmt",
        "wifi",
        "advanced",
    }:
        raise HTTPException(status_code=400, detail="Invalid ONT configuration scope.")
    return (
        push_scope_value == "wan",
        push_scope_value == "lan",
        push_scope_value in {"management", "mgmt"},
        push_scope_value == "wifi",
    )


def _ont_configuration_command_context(
    request: Request,
    *,
    reason: str,
    idempotency_key: str,
) -> CommandContext:
    command_id = uuid.uuid4()
    request_id = str(getattr(request.state, "request_id", "") or "").strip()
    try:
        correlation_id = uuid.UUID(request_id)
    except ValueError:
        correlation_id = command_id
    auth = getattr(request.state, "auth", {}) or {}
    principal_type = str(auth.get("principal_type") or "user")
    actor_id = web_admin_service.get_actor_id(request) or "unknown"
    return CommandContext(
        command_id=command_id,
        correlation_id=correlation_id,
        actor=f"{principal_type}:{actor_id}",
        scope="network:ont:write",
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _ont_configuration_form_context(db: Session, ont_id: str) -> dict[str, object]:
    parsed_ont_id = uuid.UUID(ont_id)
    context = web_network_ont_actions_service.configure_form_context(db, ont_id)
    context["configuration_lifecycle"] = get_ont_service_configuration_projection(
        db, ont_unit_id=parsed_ont_id
    )
    context["config_idempotency_key"] = str(uuid.uuid4())
    context["retry_idempotency_key"] = str(uuid.uuid4())
    return context


def _log_ont_action_result(
    *,
    request: Request | None,
    ont_id: str | None,
    action: str,
    ok: bool,
    message: str,
    metadata: dict[str, object] | None = None,
) -> None:
    log_network_action_result(
        request=request,
        resource_type="ont",
        resource_id=ont_id,
        action=action,
        success=ok,
        message=message,
        metadata=metadata,
    )


def _ont_form_dependencies(db: Session, ont: Any | None = None) -> dict:
    """Build all dropdown data needed by the ONT configuration form."""
    return ont_web_forms_service.ont_form_dependencies(db, ont)


@router.get(
    "/onts/{ont_id}/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_detail_preview(
    request: Request,
    ont_id: str,
    tab: str = Query(default="overview"),
    db: Session = Depends(get_db),
) -> Response:
    """Temporary preview page for ONT detail layout experiments."""
    page_data = web_network_core_devices_service.ont_detail_page_data(db, ont_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    allowed_tabs = {"status", "config", "history"}
    tab_aliases = {
        # Legacy tab names
        "summary": "status",
        "overview": "status",
        "effective-config": "config",
        "observed-state": "status",
        # Convenience aliases
        "service": "config",
        "network": "config",
        "service-ports": "config",
        "device-config": "config",
        "configuration": "config",
        "configure": "config",
        "device-status": "status",
        "diagnostics": "status",
        "topology": "status",
        "tr069": "history",
        "charts": "history",
    }
    tab = tab_aliases.get(tab, tab)
    active_tab = tab if tab in allowed_tabs else "status"

    activities = build_audit_activities(db, "ont", str(ont_id))
    try:
        operations = web_network_operations_service.build_operation_history(
            db, "ont", str(ont_id)
        )
    except Exception:
        logger.error(
            "Failed to load operation history for ONT preview %s",
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
            **_ont_form_dependencies(db, page_data["ont"]),
            **web_network_ont_actions_service.unified_config_context(
                db, ont_id, detail_payload=page_data
            ),
            "activities": activities,
            "operations": operations,
            "ont_active_tab": active_tab,
            "preview_mode": True,
            "preview_origin_url": f"/admin/network/onts/{ont_id}",
            "location_address_or_comment": address_value,
            "location_contact": contact_value,
        }
    )
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get(
    "/onts/{ont_id}/location-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_location_details_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve location details modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    return templates.TemplateResponse(
        "admin/network/onts/_location_details_modal.html",
        {
            "request": request,
            **ont_web_forms_service.location_modal_context(db, ont),
        },
    )


@router.post(
    "/onts/{ont_id}/location-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_location_details_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Update ONT location details."""
    result = ont_web_forms_service.update_location_details_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")
    if result.error:
        return templates.TemplateResponse(
            "admin/network/onts/_location_details_modal.html",
            {
                "request": request,
                **ont_web_forms_service.location_modal_context(
                    db,
                    result.ont,
                    error=result.error,
                    form_values=cast(dict[str, Any], result.form_model),
                ),
            },
            status_code=400,
        )

    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


@router.get(
    "/onts/{ont_id}/device-info",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_device_info_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve device information modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    return templates.TemplateResponse(
        "admin/network/onts/_device_info_modal.html",
        {
            "request": request,
            **ont_web_forms_service.device_info_modal_context(db, ont),
        },
    )


@router.post(
    "/onts/{ont_id}/device-info",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_device_info_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Update ONT device information."""
    result = ont_web_forms_service.update_device_info_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")
    if result.error:
        return templates.TemplateResponse(
            "admin/network/onts/_device_info_modal.html",
            {
                "request": request,
                **ont_web_forms_service.device_info_modal_context(
                    db,
                    result.ont,
                    error=result.error,
                    form_values=cast(dict[str, Any], result.form_model),
                ),
            },
            status_code=400,
        )

    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


@router.get(
    "/onts/{ont_id}/gpon-channel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_gpon_channel_modal(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Serve GPON channel modal partial."""
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        raise HTTPException(status_code=404, detail="ONT not found")

    return templates.TemplateResponse(
        "admin/network/onts/_gpon_channel_modal.html",
        {"request": request, **ont_web_forms_service.gpon_channel_modal_context(ont)},
    )


@router.post(
    "/onts/{ont_id}/gpon-channel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_gpon_channel_update(
    ont_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Update ONT GPON channel."""
    result = ont_web_forms_service.update_gpon_channel_from_form(
        db, ont_id, parse_form_data_sync(request), request=request
    )
    if result.not_found:
        raise HTTPException(status_code=404, detail="ONT not found")

    return RedirectResponse(url=f"/admin/network/onts/{ont_id}", status_code=303)


@router.get(
    "/onts/{ont_id}/wifi-controls",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_wifi_controls(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: WiFi controls with current SSID pre-filled."""
    context = {
        "request": request,
        **ont_web_forms_service.wifi_controls_context(db, ont_id),
    }
    return templates.TemplateResponse("admin/network/onts/_wifi_controls.html", context)


@router.get(
    "/onts/{ont_id}/lan-ports-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_lan_ports_status(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: LAN port status with toggle controls."""
    from app.web.admin.network_onts_actions import _lan_ports_partial_response

    return _lan_ports_partial_response(request, db, ont_id)


@router.get(
    "/onts/{ont_id}/firmware-form",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_firmware_form(
    request: Request, ont_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: Firmware selection form with available images."""
    context = {
        "request": request,
        **ont_web_forms_service.firmware_form_context(db, ont_id),
    }
    return templates.TemplateResponse("admin/network/onts/_firmware_form.html", context)


@router.get(
    "/onts/{ont_id}/provision",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_provision_wizard(
    request: Request,
    ont_id: str,
    operation_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """One-page gated ONT provisioning configuration workflow."""
    context = web_network_onts_service.provision_wizard_context(
        request,
        db,
        ont_id,
        operation_id=operation_id,
    )
    if context.get("error"):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": context["error"]},
            status_code=404,
        )
    ont = context.get("ont")
    if getattr(ont, "authorization_status", None) != OntAuthorizationStatus.authorized:
        from urllib.parse import quote_plus

        return RedirectResponse(
            (
                f"/admin/network/onts/{ont_id}?feedback_status=error"
                f"&feedback_message={quote_plus('Authorize the ONT on the OLT before configuring it.')}"
            ),
            status_code=303,
        )
    if status and message:
        context["provision_feedback"] = {"status": status, "message": message}
    return templates.TemplateResponse("admin/network/onts/provision.html", context)


# -- Service-port management routes --------------------------------------------


@router.get(
    "/onts/{ont_id}/service-ports",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_service_ports(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Service-ports tab for ONT detail page."""
    data = web_network_service_ports_service.list_context(db, ont_id)
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_service_ports_tab.html", context
    )


@router.post(
    "/onts/{ont_id}/service-ports/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_service_port_create(
    request: Request,
    ont_id: str,
    vlan_id: int = Form(...),
    gem_index: int = Form(...),
    user_vlan: str = Form(default=""),
    tag_transform: str = Form(default="translate"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Create a single service-port on the OLT for this ONT."""
    resolved_user_vlan, error = web_network_service_ports_service.coerce_user_vlan(
        user_vlan
    )
    if error:
        _log_ont_action_result(
            request=request,
            ont_id=ont_id,
            action="Create Service Port",
            ok=False,
            message=error,
            metadata={"vlan_id": vlan_id, "user_vlan": user_vlan},
        )
        return _service_ports_partial_response(
            request,
            db,
            ont_id,
            toast_message=error,
            toast_type="error",
        )

    ok, msg = web_network_service_ports_service.handle_create(
        db,
        ont_id,
        vlan_id,
        gem_index,
        user_vlan=resolved_user_vlan,
        tag_transform=tag_transform,
    )
    _log_ont_action_result(
        request=request,
        ont_id=ont_id,
        action="Create Service Port",
        ok=ok,
        message=msg,
        metadata={"vlan_id": vlan_id, "gem_index": gem_index},
    )
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


@router.post(
    "/onts/{ont_id}/service-ports/{index}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_service_port_delete(
    request: Request,
    ont_id: str,
    index: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Delete a service-port from the OLT by index."""
    ok, msg = web_network_service_ports_service.handle_delete(db, ont_id, index)
    _log_ont_action_result(
        request=request,
        ont_id=ont_id,
        action="Delete Service Port",
        ok=ok,
        message=msg,
        metadata={"service_port_index": index},
    )
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


@router.post(
    "/onts/{ont_id}/service-ports/clone",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_service_port_clone(
    request: Request,
    ont_id: str,
    ref_ont_id: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Clone service-ports from a reference ONT."""
    ok, msg = web_network_service_ports_service.handle_clone(db, ont_id, ref_ont_id)
    _log_ont_action_result(
        request=request,
        ont_id=ont_id,
        action="Clone Service Ports",
        ok=ok,
        message=msg,
        metadata={"reference_ont_id": ref_ont_id},
    )
    return _service_ports_partial_response(
        request,
        db,
        ont_id,
        toast_message=msg,
        toast_type="success" if ok else "error",
    )


# -- Configure Tab Routes (Database-backed ONT configuration) ------------------


@router.get(
    "/onts/{ont_id}/configure",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_configure_form(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Configure form populated from resolved ONT desired config."""
    context = _base_context(request, db, active_page="onts")
    try:
        context.update(_ont_configuration_form_context(db, ont_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid ONT identifier.") from exc
    return templates.TemplateResponse(
        "admin/network/onts/_configure_form.html", context
    )


@router.post(
    "/onts/{ont_id}/configure",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_configure_submit(
    request: Request,
    ont_id: str,
    wan_mode: str = Form(default=""),
    ip_protocol: str = Form(default=""),
    wan_static_ip: str = Form(default=""),
    wan_static_subnet: str = Form(default=""),
    wan_static_gateway: str = Form(default=""),
    wan_static_dns: str = Form(default=""),
    mgmt_ip_mode: str = Form(default=""),
    mgmt_ip_address: str = Form(default=""),
    mgmt_remote_access: bool = Form(default=False),
    lan_gateway_ip: str = Form(default=""),
    lan_block_prefix: str = Form(default=""),
    lan_dhcp_enabled: bool = Form(default=False),
    lan_dhcp_start: str = Form(default=""),
    lan_dhcp_end: str = Form(default=""),
    wifi_enabled: bool = Form(default=False),
    wifi_ssid: str = Form(default=""),
    wifi_channel: str = Form(default=""),
    wifi_security_mode: str = Form(default=""),
    wifi_password: str = Form(default=""),
    pppoe_wcd_index: str = Form(default=""),
    mgmt_wcd_index: str = Form(default=""),
    voip_wcd_index: str = Form(default=""),
    mgmt_service_port_index: str = Form(default=""),
    wan_service_port_index: str = Form(default=""),
    push_scope: str = Form(default="management"),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Parse one section and delegate admission to the typed coordinator."""
    push_wan, push_lan, push_mgmt, push_wifi = _configure_push_scope_sections(
        push_scope
    )
    if push_wan and wan_mode.strip().lower() in {
        "bridge",
        "bridged",
        "bridging",
        "setup_via_onu",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bridge / Via ONU is provisioning-only. The admin Configuration "
                "tab supports routed DHCP, PPPoE, and static WAN modes."
            ),
        )
    # ``advanced`` scope persists OLT-binding overrides only — no traditional
    # WAN/LAN/MGMT/WiFi device push runs. Index fields under the WAN or
    # management sections also persist on those scopes so an operator can
    # tweak the WCD slot inline with a "Apply WAN" / "Apply Mgmt" click.
    push_advanced = (push_scope or "").strip().lower() == "advanced"

    def _parse_index_override(raw: str) -> int | None:
        """Empty string = no change; '0' or 'inherit' = clear override.
        Anything else must parse as a positive int; bad input → 400.
        """
        text = raw.strip()
        if text == "":
            return None
        if text in {"0", "inherit"}:
            return 0
        try:
            value = int(text)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid integer override: {raw!r}",
            ) from exc
        if value < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Index override must be non-negative: {value}",
            )
        return value

    pppoe_wcd_value = (
        _parse_index_override(pppoe_wcd_index) if (push_wan or push_advanced) else None
    )
    mgmt_wcd_value = (
        _parse_index_override(mgmt_wcd_index) if (push_mgmt or push_advanced) else None
    )
    voip_wcd_value = _parse_index_override(voip_wcd_index) if push_advanced else None
    mgmt_sp_value = (
        _parse_index_override(mgmt_service_port_index) if push_advanced else None
    )
    wan_sp_value = (
        _parse_index_override(wan_service_port_index)
        if (push_wan or push_advanced)
        else None
    )

    change: OntConfigurationChange
    if push_wan:
        section = OntConfigurationSection.wan
        change = WanConfigurationChange(
            mode=wan_mode,
            ip_protocol=ip_protocol,
            static_ip=wan_static_ip or None,
            static_subnet=wan_static_subnet or None,
            static_gateway=wan_static_gateway or None,
            static_dns=wan_static_dns or None,
            pppoe_wcd_index=pppoe_wcd_value,
            wan_service_port_index=wan_sp_value,
        )
    elif push_lan:
        section = OntConfigurationSection.lan
        try:
            block_prefix = IpBlockPrefix(lan_block_prefix) if lan_block_prefix else None
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Catalog IP block size."
            ) from exc
        change = LanConfigurationChange(
            gateway_ip=lan_gateway_ip or None,
            block_prefix=block_prefix,
            dhcp_enabled=lan_dhcp_enabled,
            dhcp_start=lan_dhcp_start or None,
            dhcp_end=lan_dhcp_end or None,
        )
    elif push_mgmt:
        section = OntConfigurationSection.management
        change = ManagementConfigurationChange(
            ip_mode=mgmt_ip_mode or None,
            ip_address=mgmt_ip_address or None,
            remote_access=mgmt_remote_access,
            wcd_index=mgmt_wcd_value,
        )
    elif push_wifi:
        section = OntConfigurationSection.wifi
        change = WifiConfigurationChange(
            enabled=wifi_enabled,
            ssid=wifi_ssid or None,
            channel=wifi_channel or None,
            security_mode=wifi_security_mode or None,
            password=wifi_password or None,
        )
    else:
        section = OntConfigurationSection.advanced
        change = AdvancedConfigurationChange(
            pppoe_wcd_index=pppoe_wcd_value,
            management_wcd_index=mgmt_wcd_value,
            voip_wcd_index=voip_wcd_value,
            management_service_port_index=mgmt_sp_value,
            wan_service_port_index=wan_sp_value,
        )

    auth = getattr(request.state, "auth", {}) or {}
    permission_granted = bool(auth) and has_permission(auth, db, "network:ont:write")
    finish_read_transaction(db)
    result = None
    error: DomainError | None = None
    try:
        result = configure_ont_service(
            db,
            ConfigureOntServiceCommand(
                context=_ont_configuration_command_context(
                    request,
                    reason=f"Configure ONT {section.value} service settings",
                    idempotency_key=idempotency_key,
                ),
                ont_unit_id=uuid.UUID(ont_id),
                permission_granted=permission_granted,
                section=section,
                change=change,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid ONT identifier.") from exc
    except DomainError as exc:
        error = exc

    if result is not None:
        result_message = result.message
    else:
        assert error is not None
        result_message = error.message

    _log_ont_action_result(
        request=request,
        ont_id=ont_id,
        action="Configure ONT",
        ok=result is not None,
        message=result_message,
        metadata={
            "push_scope": push_scope,
            "operation_id": str(result.operation_id) if result is not None else None,
            "error_code": error.code if error is not None else None,
        },
    )

    context = _base_context(request, db, active_page="onts")
    context.update(_ont_configuration_form_context(db, ont_id))
    if result is not None:
        context["config_result"] = result
    if error is not None:
        context["config_error"] = error

    response = templates.TemplateResponse(
        "admin/network/onts/_configure_form.html", context
    )
    response.headers.update(
        _toast_headers(
            result_message,
            "success" if result is not None else "error",
        )
    )
    return response


@router.post(
    "/onts/{ont_id}/configure/retry",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_configure_retry(
    request: Request,
    ont_id: str,
    configuration_head_id: str = Form(...),
    configuration_revision: int = Form(..., ge=1),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Queue a reviewed repair attempt for the exact failed revision."""

    auth = getattr(request.state, "auth", {}) or {}
    permission_granted = bool(auth) and has_permission(auth, db, "network:ont:write")
    finish_read_transaction(db)
    result = None
    error: DomainError | None = None
    try:
        if not permission_granted:
            raise HTTPException(status_code=403, detail="Forbidden")
        result = retry_ont_service_configuration(
            db,
            RetryOntServiceConfigurationCommand(
                context=_ont_configuration_command_context(
                    request,
                    reason="Retry current failed ONT service configuration",
                    idempotency_key=idempotency_key,
                ),
                ont_unit_id=uuid.UUID(ont_id),
                expected_head_id=uuid.UUID(configuration_head_id),
                expected_revision=configuration_revision,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid lifecycle identifier."
        ) from exc
    except DomainError as exc:
        error = exc

    if result is not None:
        result_message = result.message
    else:
        assert error is not None
        result_message = error.message

    context = _base_context(request, db, active_page="onts")
    context.update(_ont_configuration_form_context(db, ont_id))
    if result is not None:
        context["config_result"] = result
    if error is not None:
        context["config_error"] = error
    response = templates.TemplateResponse(
        "admin/network/onts/_configure_form.html", context
    )
    response.headers.update(
        _toast_headers(
            result_message,
            "success" if result is not None else "error",
        )
    )
    return response
