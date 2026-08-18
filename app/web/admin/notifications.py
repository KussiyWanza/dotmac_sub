"""Admin notifications management routes."""

import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin_notifications as web_admin_notifications_service
from app.services import (
    web_notification_channels as web_notification_channels_service,
)
from app.services import web_notifications as web_notifications_service
from app.services import (
    web_notifications_alert_policies as web_alert_policies_service,
)
from app.services import (
    web_notifications_sla_policies as web_sla_policies_service,
)
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.timezone import APP_TIMEZONE_NAME

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/notifications", tags=["web-admin-notifications"])


def _sla_policy_command_context(
    request: Request,
    *,
    scope: str,
    reason: str,
) -> CommandContext:
    auth = getattr(request.state, "auth", None) or {}
    actor_id = auth.get("principal_id") or auth.get("person_id")
    principal_type = str(auth.get("principal_type") or "system_user")
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{principal_type}:{actor_id}" if actor_id else "",
        scope=scope,
        reason=reason,
    )


def _htmx_error_response(
    message: str,
    status_code: int = 409,
    title: str = "Error",
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


@router.get("", response_class=HTMLResponse)
def notifications_menu(request: Request, db: Session = Depends(get_db)):
    """Notifications dropdown menu."""
    return web_admin_notifications_service.notifications_menu(request, db)


@router.get("/inbox/{notification_id}/open")
def notification_inbox_open(
    notification_id: UUID,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_user_auth),
):
    """Mark one assigned staff inbox item read and follow its target."""
    system_user_id = (
        str(auth.get("principal_id"))
        if auth.get("principal_type") == "system_user"
        else ""
    )
    from app.services import admin_alerts as admin_alerts_service

    notification = admin_alerts_service.mark_notification_read(
        db,
        str(notification_id),
        system_user_id=system_user_id,
    )
    if notification is None:
        return RedirectResponse(url="/admin", status_code=303)
    return RedirectResponse(url=notification.target_url, status_code=303)


@router.get(
    "/setup",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def notification_bulk_setup(
    request: Request,
    db: Session = Depends(get_db),
):
    """Show channel readiness and template setup for bulk notifications."""
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/setup.html",
        {
            "request": request,
            **web_notifications_service.bulk_notification_setup_context(db),
            "active_page": "notification-setup",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def notification_channel_policy_page(
    request: Request,
    db: Session = Depends(get_db),
):
    """Show which channels each notification event goes out on."""
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/channels.html",
        {
            "request": request,
            **web_notification_channels_service.channel_policy_context(db),
            "active_page": "notification-channels",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
async def notification_channel_policy_save(
    request: Request,
    db: Session = Depends(get_db),
):
    """Persist the channel matrix through the policy owner."""
    form = await request.form()
    try:
        web_notification_channels_service.save_channel_policy(db, form)
    except DomainError as exc:
        return _htmx_error_response(str(exc), title="Channel policy not saved")
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(url="/admin/notifications/channels", status_code=303)


@router.get(
    "/templates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def notification_templates_list(
    request: Request,
    channel: str | None = None,
    status: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List notification templates."""
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        list_query = web_notifications_service.build_templates_list_query(
            channel=channel, status=status, search=search, page=page, per_page=per_page
        )
    except ValueError:
        list_query = web_notifications_service.build_templates_list_query(page=page)

    return templates.TemplateResponse(
        "admin/notifications/templates_list.html",
        {
            "request": request,
            **web_notifications_service.templates_list_context(db, list_query),
            "active_page": "notification-templates",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/templates/{template_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_toggle(
    request: Request,
    template_id: UUID,
    db: Session = Depends(get_db),
):
    """Toggle a notification template active/inactive from the list."""
    template = web_notifications_service.toggle_template(db, template_id=template_id)
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return RedirectResponse(
        url=f"/admin/notifications/templates?status={'active' if template.is_active else 'inactive'}",
        status_code=303,
    )


@router.get(
    "/templates/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_new(request: Request, db: Session = Depends(get_db)):
    """Create new notification template form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_notifications_service.template_form_context(db)
    return templates.TemplateResponse(
        "admin/notifications/template_form.html",
        {
            "request": request,
            **(state or {}),
            "active_page": "notification-templates",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/templates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_create(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    channel: str = Form(...),
    subject: str | None = Form(None),
    body: str = Form(...),
    conditions_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a notification template."""
    try:
        template = web_notifications_service.create_template(
            db,
            name=name,
            code=code,
            channel=channel,
            subject=subject,
            body=body,
            conditions_json=conditions_json,
        )
        return RedirectResponse(
            url=f"/admin/notifications/templates/{template.id}", status_code=303
        )
    except Exception as exc:
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/notifications/template_form.html",
            {
                "request": request,
                **(
                    web_notifications_service.template_form_context(db, error=str(exc))
                    or {}
                ),
                "active_page": "notification-templates",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/templates/{template_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def notification_template_detail(
    request: Request,
    template_id: UUID,
    db: Session = Depends(get_db),
):
    """View and edit notification template."""
    state = web_notifications_service.template_form_context(db, template_id=template_id)
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Template not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/template_form.html",
        {
            "request": request,
            **state,
            "active_page": "notification-templates",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/templates/{template_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_update(
    request: Request,
    template_id: UUID,
    name: str = Form(...),
    code: str = Form(...),
    channel: str = Form(...),
    subject: str | None = Form(None),
    body: str = Form(...),
    conditions_json: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update a notification template."""
    try:
        web_notifications_service.update_template(
            db,
            template_id=template_id,
            name=name,
            code=code,
            channel=channel,
            subject=subject,
            body=body,
            is_active=is_active is not None,
            conditions_json=conditions_json,
        )
        return RedirectResponse(
            url=f"/admin/notifications/templates/{template_id}", status_code=303
        )
    except Exception as exc:
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/notifications/template_form.html",
            {
                "request": request,
                **(
                    web_notifications_service.template_form_context(
                        db, template_id=template_id, error=str(exc)
                    )
                    or {}
                ),
                "active_page": "notification-templates",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/templates/{template_id}/test",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_test(
    request: Request,
    template_id: UUID,
    test_recipient: str = Form(...),
    test_variables_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Send a test notification using this template."""
    try:
        message = web_notifications_service.send_template_test(
            db,
            template_id=template_id,
            test_recipient=test_recipient,
            test_variables_json=test_variables_json,
        )
        if request.headers.get("HX-Request"):
            trigger = {
                "showToast": {
                    "type": "success",
                    "title": "Test Sent",
                    "message": message,
                }
            }
            return Response(
                status_code=200, headers={"HX-Trigger": json.dumps(trigger)}
            )
        return RedirectResponse(
            url=f"/admin/notifications/templates/{template_id}", status_code=303
        )
    except Exception as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc), status_code=200, reswap="none")
        return RedirectResponse(
            url=f"/admin/notifications/templates/{template_id}", status_code=303
        )


@router.post(
    "/templates/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_unsaved_preview(
    request: Request,
    channel: str = Form(...),
    subject: str | None = Form(None),
    body: str = Form(...),
    test_variables_json: str | None = Form(None),
):
    """Render template preview before the first save."""
    return templates.TemplateResponse(
        request,
        "admin/notifications/_template_preview.html",
        {
            "request": request,
            **web_notifications_service.render_template_preview_from_fields(
                channel=channel,
                subject=subject,
                body=body,
                test_variables_json=test_variables_json,
            ),
        },
    )


@router.post(
    "/templates/{template_id}/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_preview(
    request: Request,
    template_id: UUID,
    test_variables_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Render template preview with variable substitution."""
    return templates.TemplateResponse(
        request,
        "admin/notifications/_template_preview.html",
        {
            "request": request,
            **web_notifications_service.render_template_preview(
                db,
                template_id=template_id,
                test_variables_json=test_variables_json,
            ),
        },
    )


@router.delete(
    "/templates/{template_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
@router.post(
    "/templates/{template_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def notification_template_delete(
    request: Request,
    template_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete (deactivate) a notification template."""
    try:
        web_notifications_service.delete_template(db, template_id=template_id)
        if request.headers.get("HX-Request"):
            return Response(
                status_code=200,
                headers={"HX-Redirect": "/admin/notifications/templates"},
            )
        return RedirectResponse(url="/admin/notifications/templates", status_code=303)
    except Exception as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc), status_code=200, reswap="none")
        return RedirectResponse(
            url=f"/admin/notifications/templates/{template_id}", status_code=303
        )


@router.get(
    "/queue",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def notification_queue(
    request: Request,
    status: str | None = None,
    channel: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View pending and queued notifications."""
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        list_query = web_notifications_service.build_queue_list_query(
            status=status, channel=channel, page=page, per_page=per_page
        )
    except ValueError:
        list_query = web_notifications_service.build_queue_list_query(page=page)

    return templates.TemplateResponse(
        "admin/notifications/queue.html",
        {
            "request": request,
            **web_notifications_service.queue_context(db, list_query),
            "active_page": "notification-queue",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/history",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def notification_history(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """View notification delivery history."""
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        list_query = web_notifications_service.build_history_list_query(
            status=status, page=page, per_page=per_page
        )
    except ValueError:
        list_query = web_notifications_service.build_history_list_query(page=page)

    return templates.TemplateResponse(
        "admin/notifications/history.html",
        {
            "request": request,
            **web_notifications_service.history_context(db, list_query),
            "active_page": "notification-history",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


# ---------------------------------------------------------------------------
# Alert Notification Policies
# ---------------------------------------------------------------------------


@router.get(
    "/alert-policies",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def alert_policies_list(
    request: Request,
    channel: str | None = None,
    status: str | None = None,
    severity_min: str | None = None,
    active: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List alert notification policies."""
    state = web_alert_policies_service.alert_policies_list_data(
        db,
        page=page,
        per_page=per_page,
        channel=channel,
        status=status,
        severity_min=severity_min,
        active=active,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/alert_policies.html",
        {
            "request": request,
            **state,
            "active_page": "alert-policies",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/alert-policies/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def alert_policy_new(request: Request, db: Session = Depends(get_db)):
    """Create new alert policy form."""
    state = web_alert_policies_service.alert_policy_form_data(db)
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/alert_policy_form.html",
        {
            "request": request,
            **state,
            "action_url": "/admin/notifications/alert-policies",
            "form_title": "New Alert Policy",
            "submit_label": "Create Policy",
            "active_page": "alert-policies",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/alert-policies",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def alert_policy_create(
    request: Request,
    name: str = Form(...),
    channel: str = Form(...),
    recipient: str = Form(...),
    severity_min: str = Form("warning"),
    template_id: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create an alert notification policy."""
    try:
        policy = web_alert_policies_service.create_alert_policy(
            db,
            name=name,
            channel=channel,
            recipient=recipient,
            severity_min=severity_min,
            template_id=template_id,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(
            url=f"/admin/notifications/alert-policies/{policy.id}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        state = web_alert_policies_service.alert_policy_form_data(db)
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/notifications/alert_policy_form.html",
            {
                "request": request,
                **state,
                "action_url": "/admin/notifications/alert-policies",
                "form_title": "New Alert Policy",
                "submit_label": "Create Policy",
                "error": str(exc),
                "active_page": "alert-policies",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/alert-policies/{policy_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def alert_policy_detail(
    request: Request,
    policy_id: UUID,
    db: Session = Depends(get_db),
):
    """View and edit alert policy with escalation steps."""
    state = web_alert_policies_service.alert_policy_detail_data(
        db, policy_id=str(policy_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Alert policy not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/alert_policy_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/notifications/alert-policies/{policy_id}",
            "form_title": "Edit Alert Policy",
            "submit_label": "Update Policy",
            "active_page": "alert-policies",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/alert-policies/{policy_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def alert_policy_update(
    request: Request,
    policy_id: UUID,
    name: str = Form(...),
    channel: str = Form(...),
    recipient: str = Form(...),
    severity_min: str = Form("warning"),
    template_id: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update an alert notification policy."""
    try:
        web_alert_policies_service.update_alert_policy(
            db,
            policy_id=policy_id,
            name=name,
            channel=channel,
            recipient=recipient,
            severity_min=severity_min,
            template_id=template_id,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(
            url=f"/admin/notifications/alert-policies/{policy_id}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        state = web_alert_policies_service.alert_policy_detail_data(
            db, policy_id=str(policy_id)
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/notifications/alert_policy_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/notifications/alert-policies/{policy_id}",
                "form_title": "Edit Alert Policy",
                "submit_label": "Update Policy",
                "error": str(exc),
                "active_page": "alert-policies",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/alert-policies/{policy_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def alert_policy_delete(policy_id: UUID, db: Session = Depends(get_db)):
    """Delete an alert notification policy."""
    web_alert_policies_service.delete_alert_policy(db, policy_id=policy_id)
    return RedirectResponse(url="/admin/notifications/alert-policies", status_code=303)


@router.post(
    "/alert-policies/{policy_id}/steps",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def alert_policy_step_create(
    request: Request,
    policy_id: UUID,
    step_index: int = Form(0),
    delay_minutes: int = Form(0),
    step_channel: str = Form("email"),
    step_recipient: str | None = Form(None),
    step_rotation_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Add an escalation step to an alert policy."""
    web_alert_policies_service.create_alert_policy_step(
        db,
        policy_id=policy_id,
        step_index=step_index,
        delay_minutes=delay_minutes,
        step_channel=step_channel,
        step_recipient=step_recipient,
        step_rotation_id=step_rotation_id,
    )
    return RedirectResponse(
        url=f"/admin/notifications/alert-policies/{policy_id}",
        status_code=303,
    )


@router.post(
    "/alert-policies/{policy_id}/steps/{step_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def alert_policy_step_delete(
    policy_id: UUID,
    step_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete an escalation step."""
    web_alert_policies_service.delete_alert_policy_step(db, step_id=step_id)
    return RedirectResponse(
        url=f"/admin/notifications/alert-policies/{policy_id}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Operational SLA Policies
# ---------------------------------------------------------------------------


def _sla_policy_page_context(
    request: Request,
    db: Session,
    state: dict[str, object],
    **extra,
) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        **state,
        **extra,
        "active_page": "sla-policies",
        "active_menu": "system",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/sla-policies",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def sla_policies_list(
    request: Request,
    trigger: str | None = None,
    active: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List UI-owned SLA event escalation policies."""
    state = web_sla_policies_service.list_data(
        db,
        trigger=trigger,
        active=active,
        page=page,
        per_page=per_page,
    )
    return templates.TemplateResponse(
        "admin/notifications/sla_policies.html",
        _sla_policy_page_context(request, db, state),
    )


@router.get(
    "/sla-policies/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def sla_policy_new(request: Request, db: Session = Depends(get_db)):
    state = web_sla_policies_service.form_data(db)
    assert state is not None
    return templates.TemplateResponse(
        "admin/notifications/sla_policy_form.html",
        _sla_policy_page_context(
            request,
            db,
            state,
            action_url="/admin/notifications/sla-policies",
            form_title="New SLA Policy",
            submit_label="Create Policy",
        ),
    )


@router.post(
    "/sla-policies",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def sla_policy_create(
    request: Request,
    name: str = Form(...),
    entity_type: str = Form(...),
    trigger: str = Form(...),
    level: int = Form(...),
    delay_minutes: int = Form(...),
    channels: list[str] = Form(default=[]),
    min_severity: str | None = Form(None),
    min_affected_customers: int | None = Form(None),
    near_breach_minutes: int | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    context = _sla_policy_command_context(
        request,
        scope=f"{entity_type}:{trigger}:{level}",
        reason="admin_operational_sla_policy_creation",
    )
    db_session_adapter.release_read_transaction(db)
    try:
        policy = web_sla_policies_service.create_policy(
            db,
            context=context,
            name=name,
            entity_type=entity_type,
            trigger=trigger,
            level=level,
            delay_minutes=delay_minutes,
            channels=channels,
            min_severity=min_severity,
            min_affected_customers=min_affected_customers,
            near_breach_minutes=near_breach_minutes,
            notes=notes,
            is_active=is_active is not None,
        )
        return RedirectResponse(
            url=f"/admin/notifications/sla-policies/{policy.id}",
            status_code=303,
        )
    except DomainError as exc:
        state = web_sla_policies_service.form_data(db)
        assert state is not None
        return templates.TemplateResponse(
            "admin/notifications/sla_policy_form.html",
            _sla_policy_page_context(
                request,
                db,
                state,
                action_url="/admin/notifications/sla-policies",
                form_title="New SLA Policy",
                submit_label="Create Policy",
                error=exc.message,
            ),
            status_code=400,
        )


@router.get(
    "/sla-policies/{policy_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def sla_policy_detail(
    request: Request,
    policy_id: UUID,
    db: Session = Depends(get_db),
):
    state = web_sla_policies_service.form_data(db, policy_id=policy_id)
    if state is None:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "SLA policy not found"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "admin/notifications/sla_policy_form.html",
        _sla_policy_page_context(
            request,
            db,
            state,
            action_url=f"/admin/notifications/sla-policies/{policy_id}",
            form_title="Edit SLA Policy",
            submit_label="Update Policy",
        ),
    )


@router.post(
    "/sla-policies/{policy_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def sla_policy_update(
    request: Request,
    policy_id: UUID,
    name: str = Form(...),
    entity_type: str = Form(...),
    trigger: str = Form(...),
    level: int = Form(...),
    delay_minutes: int = Form(...),
    channels: list[str] = Form(default=[]),
    min_severity: str | None = Form(None),
    min_affected_customers: int | None = Form(None),
    near_breach_minutes: int | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    context = _sla_policy_command_context(
        request,
        scope=str(policy_id),
        reason="admin_operational_sla_policy_update",
    )
    db_session_adapter.release_read_transaction(db)
    try:
        web_sla_policies_service.update_policy(
            db,
            context=context,
            policy_id=policy_id,
            name=name,
            entity_type=entity_type,
            trigger=trigger,
            level=level,
            delay_minutes=delay_minutes,
            channels=channels,
            min_severity=min_severity,
            min_affected_customers=min_affected_customers,
            near_breach_minutes=near_breach_minutes,
            notes=notes,
            is_active=is_active is not None,
        )
        return RedirectResponse(
            url=f"/admin/notifications/sla-policies/{policy_id}",
            status_code=303,
        )
    except DomainError as exc:
        state = web_sla_policies_service.form_data(db, policy_id=policy_id)
        if state is None:
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "SLA policy not found"},
                status_code=404,
            )
        return templates.TemplateResponse(
            "admin/notifications/sla_policy_form.html",
            _sla_policy_page_context(
                request,
                db,
                state,
                action_url=f"/admin/notifications/sla-policies/{policy_id}",
                form_title="Edit SLA Policy",
                submit_label="Update Policy",
                error=exc.message,
            ),
            status_code=400,
        )


@router.post(
    "/sla-policies/{policy_id}/delete",
    dependencies=[Depends(require_permission("notification:write"))],
)
def sla_policy_delete(
    policy_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    context = _sla_policy_command_context(
        request,
        scope=str(policy_id),
        reason="admin_operational_sla_policy_deactivation",
    )
    db_session_adapter.release_read_transaction(db)
    web_sla_policies_service.deactivate_policy(
        db,
        context=context,
        policy_id=policy_id,
    )
    return RedirectResponse(url="/admin/notifications/sla-policies", status_code=303)


# ---------------------------------------------------------------------------
# On-Call Rotations
# ---------------------------------------------------------------------------


@router.get(
    "/oncall-rotations",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def oncall_rotations_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List on-call rotations."""
    state = web_alert_policies_service.oncall_rotations_list_data(
        db, page=page, per_page=per_page
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/oncall_rotations.html",
        {
            "request": request,
            **state,
            "active_page": "oncall-rotations",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/oncall-rotations",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def oncall_rotation_create(
    request: Request,
    name: str = Form(...),
    timezone: str = Form(APP_TIMEZONE_NAME),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create an on-call rotation."""
    try:
        rotation = web_alert_policies_service.create_oncall_rotation(
            db,
            name=name,
            timezone=timezone,
            notes=notes,
        )
        return RedirectResponse(
            url=f"/admin/notifications/oncall-rotations/{rotation.id}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        state = web_alert_policies_service.oncall_rotations_list_data(
            db, page=1, per_page=25
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/notifications/oncall_rotations.html",
            {
                "request": request,
                **state,
                "error": str(exc),
                "active_page": "oncall-rotations",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/oncall-rotations/{rotation_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:read"))],
)
def oncall_rotation_detail(
    request: Request,
    rotation_id: UUID,
    db: Session = Depends(get_db),
):
    """View on-call rotation with members."""
    state = web_alert_policies_service.oncall_rotation_detail_data(
        db, rotation_id=str(rotation_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "On-call rotation not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/notifications/oncall_form.html",
        {
            "request": request,
            **state,
            "active_page": "oncall-rotations",
            "active_menu": "system",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/oncall-rotations/{rotation_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def oncall_rotation_update(
    request: Request,
    rotation_id: UUID,
    name: str = Form(...),
    timezone: str = Form(APP_TIMEZONE_NAME),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update an on-call rotation."""
    try:
        web_alert_policies_service.update_oncall_rotation(
            db,
            rotation_id=rotation_id,
            name=name,
            timezone=timezone,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(
            url=f"/admin/notifications/oncall-rotations/{rotation_id}",
            status_code=303,
        )
    except Exception as exc:
        db.rollback()
        state = web_alert_policies_service.oncall_rotation_detail_data(
            db, rotation_id=str(rotation_id)
        )
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/notifications/oncall_form.html",
            {
                "request": request,
                **(state or {}),
                "error": str(exc),
                "active_page": "oncall-rotations",
                "active_menu": "system",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/oncall-rotations/{rotation_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def oncall_rotation_delete(rotation_id: UUID, db: Session = Depends(get_db)):
    """Delete an on-call rotation."""
    web_alert_policies_service.delete_oncall_rotation(db, rotation_id=rotation_id)
    return RedirectResponse(
        url="/admin/notifications/oncall-rotations", status_code=303
    )


@router.post(
    "/oncall-rotations/{rotation_id}/members",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def oncall_rotation_member_create(
    request: Request,
    rotation_id: UUID,
    member_name: str = Form(...),
    member_contact: str = Form(...),
    member_priority: int = Form(0),
    db: Session = Depends(get_db),
):
    """Add a member to an on-call rotation."""
    web_alert_policies_service.create_oncall_rotation_member(
        db,
        rotation_id=rotation_id,
        member_name=member_name,
        member_contact=member_contact,
        member_priority=member_priority,
    )
    return RedirectResponse(
        url=f"/admin/notifications/oncall-rotations/{rotation_id}",
        status_code=303,
    )


@router.post(
    "/oncall-rotations/{rotation_id}/members/{member_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("notification:write"))],
)
def oncall_rotation_member_delete(
    rotation_id: UUID,
    member_id: UUID,
    db: Session = Depends(get_db),
):
    """Remove a member from an on-call rotation."""
    web_alert_policies_service.delete_oncall_rotation_member(db, member_id=member_id)
    return RedirectResponse(
        url=f"/admin/notifications/oncall-rotations/{rotation_id}",
        status_code=303,
    )
