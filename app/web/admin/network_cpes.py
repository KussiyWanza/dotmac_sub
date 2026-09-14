"""Admin network CPE management routes."""

import json
import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_cpe_actions as web_network_cpe_actions_service
from app.services import web_network_cpes as web_network_cpes_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/cpes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def cpe_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    subscriber_id: str | None = None,
    page: int = 1,
    per_page: int = 25,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    state = web_network_cpes_service.build_cpe_list_data(
        db,
        search=search,
        status=status,
        vendor=vendor,
        subscriber_id=subscriber_id,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="cpes")
    context.update(state)
    return templates.TemplateResponse("admin/network/cpes/index.html", context)


@router.get(
    "/cpes/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def cpe_new(
    request: Request,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": web_network_cpes_service.cpe_form_snapshot(
                {"subscriber_id": subscriber_id or ""}
            ),
            "action_url": "/admin/network/cpes",
            **web_network_cpes_service.cpe_form_reference_data(
                db, subscriber_id=subscriber_id
            ),
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/form.html", context)


@router.get(
    "/cpes/subscriber-fields",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def cpe_subscriber_fields(
    request: Request,
    subscriber_id: str | None = None,
    subscription_id: str | None = None,
    service_address_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    cpe = web_network_cpes_service.cpe_form_snapshot(
        {
            "subscriber_id": subscriber_id or "",
            "subscription_id": subscription_id or "",
            "service_address_id": service_address_id or "",
        }
    )
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": cpe,
            **web_network_cpes_service.cpe_form_reference_data(
                db, subscriber_id=subscriber_id
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/cpes/_subscriber_fields.html", context
    )


@router.post(
    "/cpes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def cpe_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_cpes_service.parse_cpe_form(form)
    result = web_network_cpes_service.create_cpe_from_values(
        db,
        values,
        request=request,
    )
    if result.error:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": result.form_model,
                "action_url": "/admin/network/cpes",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": result.error,
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)

    cpe = result.cpe
    if cpe is None:
        raise HTTPException(
            status_code=500, detail="CPE creation did not return a device"
        )
    return RedirectResponse(f"/admin/network/cpes/{cpe.id}", status_code=303)


@router.get(
    "/cpes/{cpe_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def cpe_edit(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except (HTTPException, ValueError, LookupError):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    cpe_data = web_network_cpes_service.cpe_form_snapshot_from_model(cpe)
    context = _base_context(request, db, active_page="cpes")
    context.update(
        {
            "cpe": cpe_data,
            "action_url": f"/admin/network/cpes/{cpe_id}",
            **web_network_cpes_service.cpe_form_reference_data(
                db, subscriber_id=str(cpe_data.get("subscriber_id") or "")
            ),
            "error": None,
        }
    )
    return templates.TemplateResponse("admin/network/cpes/form.html", context)


@router.post(
    "/cpes/{cpe_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def cpe_update(request: Request, cpe_id: str, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_cpes_service.parse_cpe_form(form)
    result = web_network_cpes_service.update_cpe_from_values(
        db, cpe_id=cpe_id, values=values, request=request
    )
    if result.not_found:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    if result.error:
        context = _base_context(request, db, active_page="cpes")
        context.update(
            {
                "cpe": result.form_model,
                "action_url": f"/admin/network/cpes/{cpe_id}",
                **web_network_cpes_service.cpe_form_reference_data(
                    db, subscriber_id=str(values.get("subscriber_id") or "")
                ),
                "error": result.error,
            }
        )
        return templates.TemplateResponse("admin/network/cpes/form.html", context)

    cpe = result.cpe
    if cpe is None:
        raise HTTPException(
            status_code=500, detail="CPE update did not return a device"
        )
    return RedirectResponse(f"/admin/network/cpes/{cpe.id}", status_code=303)


@router.post(
    "/cpes/{cpe_id}/test-api",
    dependencies=[Depends(require_permission("network:cpe:write"))],
)
def cpe_test_api(cpe_id: str, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        cpe = web_network_cpes_service.get_cpe(db, cpe_id=cpe_id)
    except (HTTPException, ValueError, LookupError):
        message = quote_plus("CPE not found")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    except Exception as exc:
        logger.error(
            "Failed to load CPE %s for API test: %s", cpe_id, exc, exc_info=True
        )
        message = quote_plus("Failed to load CPE")
        return RedirectResponse(
            f"/admin/network/cpes/{cpe_id}?test_status=error&test_message={message}",
            status_code=303,
        )
    success, message = web_network_cpes_service.validate_mikrotik_api_config(cpe)
    encoded_message = quote_plus(message)
    status = "success" if success else "error"
    return RedirectResponse(
        f"/admin/network/cpes/{cpe_id}?test_status={status}&test_message={encoded_message}",
        status_code=303,
    )


@router.get(
    "/cpes/{cpe_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def cpe_detail(
    request: Request,
    cpe_id: str,
    test_status: str | None = None,
    test_message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        state = web_network_cpes_service.build_cpe_detail_data(
            db,
            cpe_id=cpe_id,
            test_status=test_status,
            test_message=test_message,
        )
    except (HTTPException, ValueError, LookupError):
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )
    context = _base_context(request, db, active_page="cpes")
    context.update(state)
    return templates.TemplateResponse("admin/network/cpes/detail.html", context)


# ── CPE TR-069 Remote Management ─────────────────────────────────


@router.get(
    "/cpes/{cpe_id}/quick-status",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def cpe_quick_status(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: lightweight TR-069 status for CPE overview sidebar."""
    from app.services.network.cpe_tr069 import CpeTR069

    summary = CpeTR069.get_device_summary(db, cpe_id)
    context = _base_context(request, db, active_page="cpes")
    context["status"] = summary
    return templates.TemplateResponse(
        "admin/network/cpes/_quick_status_partial.html", context
    )


def _cpe_action_response(
    result: object,
    *,
    request: Request | None = None,
    db: Session | None = None,
    cpe_id: str | None = None,
) -> JSONResponse | HTMLResponse:
    """Build an action response with HX toast support.

    Browser-triggered HTMX actions get the refreshed TR-069 partial so stale
    values do not linger after refreshes or port toggles. Non-HTMX callers keep
    the JSON contract and status codes.
    """
    from app.services.network.ont_action_common import ActionResult

    if not isinstance(result, ActionResult):
        raise TypeError("Expected ActionResult from CPE action handler")
    if result.success:
        status_code = 200
    elif result.error_code and result.error_code.endswith(
        ("permission_denied", "cpe_scope_denied")
    ):
        status_code = 403
    else:
        status_code = 502
    headers = {
        # HTTP header values must be latin-1 encodable; ensure_ascii keeps the
        # JSON payload header-safe while preserving the original response body.
        "HX-Trigger": json.dumps(
            {
                "showToast": {
                    "message": result.message,
                    "type": "success" if result.success else "error",
                }
            },
            ensure_ascii=True,
        )
    }
    if (
        request is not None
        and db is not None
        and cpe_id is not None
        and request.headers.get("HX-Request", "").lower() == "true"
    ):
        from app.services.network.cpe_tr069 import CpeTR069

        summary = CpeTR069.get_device_summary(db, cpe_id)
        context = _base_context(request, db, active_page="cpes")
        context.update({"tr069": summary, "tr069_available": summary.available})
        response = templates.TemplateResponse(
            "admin/network/cpes/_tr069_partial.html", context
        )
        response.headers.update(headers)
        return response
    body: dict[str, object] = {"success": result.success, "message": result.message}
    if result.error_code:
        body["error_code"] = result.error_code
    return JSONResponse(
        body,
        status_code=status_code,
        headers=headers,
    )


@router.get(
    "/cpes/{cpe_id}/tr069",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:cpe:read"))],
)
def cpe_tr069_tab(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    """HTMX partial: TR-069 device details for CPE detail page tab."""
    from app.services.network.cpe_tr069 import CpeTR069

    summary = CpeTR069.get_device_summary(db, cpe_id)
    context = _base_context(request, db, active_page="cpes")
    context.update({"tr069": summary, "tr069_available": summary.available})
    return templates.TemplateResponse("admin/network/cpes/_tr069_partial.html", context)


@router.post(
    "/cpes/{cpe_id}/reboot",
    dependencies=[Depends(require_permission("network:cpe:write"))],
    response_model=None,
)
def cpe_reboot(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Reboot CPE device via TR-069."""
    result = web_network_cpe_actions_service.execute_reboot_from_request(
        db, cpe_id, request=request
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/factory-reset",
    dependencies=[Depends(require_permission("network:cpe:write"))],
    response_model=None,
)
def cpe_factory_reset(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Factory reset CPE device via TR-069."""
    result = web_network_cpe_actions_service.execute_factory_reset_from_request(
        db, cpe_id, request=request
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/refresh",
    dependencies=[Depends(require_permission("network:cpe:write"))],
    response_model=None,
)
def cpe_refresh(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Refresh CPE device status from ACS."""
    result = web_network_cpe_actions_service.execute_refresh_from_request(
        db, cpe_id, request=request
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/wifi-ssid",
    dependencies=[
        Depends(require_permission("network:cpe:write")),
        Depends(require_permission("network:ont:write")),
    ],
    response_model=None,
)
def cpe_wifi_ssid(
    request: Request, cpe_id: str, ssid: str = "", db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Queue a WiFi SSID change on the ONT behind this CPE.

    Delegates to ``network.ont_service_configuration`` (the same owner the
    ONT Configure tab and customer self-care WiFi flow use) instead of
    writing to GenieACS directly; requires ``network:ont:write`` because that
    owner enforces its own admission scope.
    """
    result = web_network_cpe_actions_service.execute_wifi_ssid_from_request(
        db, cpe_id, ssid=ssid, request=request
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/wifi-password",
    dependencies=[
        Depends(require_permission("network:cpe:write")),
        Depends(require_permission("network:ont:write")),
    ],
    response_model=None,
)
def cpe_wifi_password(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Queue a WiFi password change on the ONT behind this CPE.

    See ``cpe_wifi_ssid`` above for why this delegates to
    ``network.ont_service_configuration`` and requires ``network:ont:write``.
    """
    form = parse_form_data_sync(request)
    password = str(form.get("password") or "")
    result = web_network_cpe_actions_service.execute_wifi_password_from_request(
        db, cpe_id, password=password, request=request
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/lan-port",
    dependencies=[Depends(require_permission("network:cpe:write"))],
    response_model=None,
)
def cpe_lan_port(
    request: Request,
    cpe_id: str,
    port: int = 1,
    enabled: str = "true",
    db: Session = Depends(get_db),
) -> JSONResponse | HTMLResponse:
    """Toggle LAN port on CPE device via TR-069."""
    result = web_network_cpe_actions_service.execute_lan_port(
        db, cpe_id, port=port, enabled=enabled.lower() == "true"
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/connection-request",
    dependencies=[Depends(require_permission("network:cpe:write"))],
    response_model=None,
)
def cpe_connection_request(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Send connection request to CPE device."""
    result = web_network_cpe_actions_service.execute_connection_request_from_request(
        db, cpe_id, request=request
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/ping-diagnostic",
    dependencies=[Depends(require_permission("network:cpe:write"))],
    response_model=None,
)
def cpe_ping_diagnostic(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Run ping diagnostic from CPE device via TR-069."""
    form = parse_form_data_sync(request)
    host = str(form.get("host") or "")
    try:
        count = int(str(form.get("count") or 4))
    except (ValueError, TypeError):
        count = 4
    result = web_network_cpe_actions_service.execute_ping_diagnostic(
        db, cpe_id, host=host, count=count
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)


@router.post(
    "/cpes/{cpe_id}/traceroute-diagnostic",
    dependencies=[Depends(require_permission("network:cpe:write"))],
    response_model=None,
)
def cpe_traceroute_diagnostic(
    request: Request, cpe_id: str, db: Session = Depends(get_db)
) -> JSONResponse | HTMLResponse:
    """Run traceroute diagnostic from CPE device via TR-069."""
    form = parse_form_data_sync(request)
    host = str(form.get("host") or "8.8.8.8")
    result = web_network_cpe_actions_service.execute_traceroute_diagnostic(
        db, cpe_id, host=host
    )
    return _cpe_action_response(result, request=request, db=db, cpe_id=cpe_id)
