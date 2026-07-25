"""Admin team inbox routes."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.services import (
    conversation_ticket_handoff,
    team_inbox_commands,
    team_inbox_contact_links,
    team_inbox_metrics,
    team_inbox_operations,
    team_inbox_projection,
    team_inbox_read_state,
    team_inbox_routing,
)
from app.services.auth_dependencies import can, require_permission
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/inbox", tags=["web-admin-inbox"])
templates = Jinja2Templates(directory="templates")


def _parse_datetime_field(value: object) -> datetime | None:
    """Parse a browser `datetime-local` value (snooze time, activity range).

    The browser sends local wall-clock without a zone; it is read as UTC so the
    stored wake time is unambiguous, and an unparsable value is treated as
    absent rather than silently snoozing to the wrong moment.
    """
    text = _query_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _prepare_mutation(db: Session) -> None:
    """Close permission/sidebar reads before entering a public owner command."""
    finish_read_transaction(db)


def _query_text(value: object) -> str | None:
    """Normalize direct-call FastAPI parameter sentinels at the adapter."""

    return value if isinstance(value, str) else None


def _query_bool(value: object, *, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _query_optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _query_int(value: object, *, default: int | None = None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "team-inbox",
        "active_menu": "services",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_queue(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    channel_type: str | None = Query(default=None),
    service_team_id: str | None = Query(default=None),
    service_team_ids: str | None = Query(default=None),
    assigned_person_id: str | None = Query(default=None),
    needs_response: bool = Query(default=False),
    contact_resolution_status: str | None = Query(default=None),
    priority_at_most: int | None = Query(default=None),
    muted: bool | None = Query(default=None),
    snoozed: bool | None = Query(default=None),
    open_only: bool = Query(default=False),
    unassigned: bool = Query(default=False),
    unread: bool = Query(default=False),
    ai_handling: str | None = Query(default=None),
    has_ticket: str | None = Query(default=None),
    activity_from: str | None = Query(default=None),
    activity_to: str | None = Query(default=None),
    sort_by: str | None = Query(default=None, alias="sort"),
    sort_dir: str | None = Query(default=None, alias="dir"),
    page: int = Query(default=1),
    per_page: int = Query(default=25),
    c: str | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id_from_request(request)
    try:
        actor_person_id = UUID(actor_id) if actor_id else None
    except ValueError:
        actor_person_id = None
    projection = team_inbox_projection.build_queue_projection(
        db,
        team_inbox_projection.InboxQueueRequest(
            search=_query_text(search),
            status=_query_text(status),
            channel_type=_query_text(channel_type),
            service_team_id=_query_text(service_team_id),
            service_team_ids=tuple(
                item.strip()
                for item in (_query_text(service_team_ids) or "").split(",")
                if item.strip()
            ),
            assigned_person_id=_query_text(assigned_person_id),
            needs_response=_query_bool(needs_response),
            contact_resolution_status=_query_text(contact_resolution_status),
            priority_at_most=_query_int(priority_at_most),
            muted=_query_optional_bool(muted),
            snoozed=_query_optional_bool(snoozed),
            open_only=_query_bool(open_only),
            unassigned=_query_bool(unassigned),
            unread=_query_bool(unread),
            ai_handling=_query_optional_bool(ai_handling),
            has_ticket=_query_optional_bool(has_ticket),
            activity_from=_parse_datetime_field(activity_from),
            activity_to=_parse_datetime_field(activity_to),
            sort_by=_query_text(sort_by),
            sort_dir=_query_text(sort_dir),
            page=_query_int(page, default=1) or 1,
            per_page=_query_int(per_page, default=25) or 25,
            selected_conversation_id=(_query_text(conversation_id) or _query_text(c)),
            actor_person_id=actor_person_id,
        ),
    )
    if projection.canonical_url is not None:
        return RedirectResponse(url=projection.canonical_url, status_code=307)
    context = _ctx(request, db)
    context.update(
        {
            "rows": projection.rows,
            "queue_metrics": projection.queue_metrics,
            "operator_unread_count": projection.operator_unread_count,
            "count": projection.count,
            "list_query": projection.list_query,
            "page_meta": projection.page_meta,
            "page": projection.page_meta.page,
            "per_page": projection.page_meta.per_page,
            "has_previous": projection.page_meta.has_previous,
            "has_next": projection.page_meta.has_next,
            "search": projection.list_query.search or "",
            "status": projection.status,
            "channel_type": projection.channel_type,
            "service_team_id": projection.service_team_id,
            "assigned_person_id": projection.assigned_person_id,
            "needs_response": projection.needs_response,
            "contact_resolution_status": projection.contact_resolution_status,
            "priority_at_most": projection.priority_at_most,
            "muted": projection.muted,
            "snoozed": projection.snoozed,
            "open_only": projection.open_only,
            "unassigned": projection.unassigned,
            "unread": projection.unread,
            "ai_handling": projection.ai_handling,
            "has_ticket": projection.has_ticket,
            "activity_from": projection.activity_from,
            "activity_to": projection.activity_to,
            "service_team_options": projection.service_team_options,
            "agent_options": projection.agent_options,
            "assignment_counts": projection.assignment_counts,
            "status_options": projection.status_options,
            "channel_options": projection.channel_options,
            "label_options": projection.label_options,
            "saved_filters": projection.saved_filters,
            "selected": (
                projection.selected.timeline
                if projection.selected is not None
                else None
            ),
            "actor_person_id": str(actor_person_id) if actor_person_id else "",
        }
    )
    if projection.selected is not None:
        context.update(
            {
                "timeline": projection.selected.timeline,
                "subscriber_summary": projection.selected.subscriber_summary,
                "contact_link_candidates": projection.selected.contact_link_candidates,
                "conversation_labels": projection.selected.conversation_labels,
                "macro_options": projection.selected.macro_options,
                "template_options": projection.selected.template_options,
                "action_eligibility": projection.selected.action_eligibility,
                "is_unread": projection.selected.is_unread,
                "priority_options": projection.selected.priority_options,
            }
        )
    if getattr(request, "headers", {}).get("hx-target") == "inbox-sidebar-content":
        return templates.TemplateResponse("admin/inbox/_sidebar.html", context)
    return templates.TemplateResponse("admin/inbox/index.html", context)


def _detail_redirect(
    conversation_id: str | UUID,
    *,
    status: str,
    message: str,
) -> RedirectResponse:
    return RedirectResponse(
        url=(
            f"/admin/inbox?c={conversation_id}&status={quote_plus(status)}"
            f"&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@router.get(
    "/{conversation_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_detail(
    conversation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    actor_id = _actor_id_from_request(request)
    try:
        actor_person_id = UUID(actor_id) if actor_id else None
    except ValueError:
        actor_person_id = None
    projection = team_inbox_projection.get_conversation_projection(
        db,
        conversation_id=conversation_id,
        actor_person_id=actor_person_id,
    )
    view = (
        {
            "timeline": projection.timeline,
            "subscriber_summary": projection.subscriber_summary,
            "contact_link_candidates": projection.contact_link_candidates,
            "label_options": projection.label_options,
            "conversation_labels": projection.conversation_labels,
            "macro_options": projection.macro_options,
            "template_options": projection.template_options,
            "action_eligibility": projection.action_eligibility,
            "is_unread": projection.is_unread,
            "actor_person_id": str(actor_person_id) if actor_person_id else "",
            "priority_options": projection.priority_options,
            "agent_options": team_inbox_projection.list_agent_options(db),
        }
        if projection is not None
        else None
    )
    if view is None:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    # HTMX list clicks swap the thread+context partial into #triage-detail;
    # a full navigation lands in the workspace with the conversation preselected.
    if request.headers.get("hx-request"):
        context = _ctx(request, db)
        context.update(view)
        return templates.TemplateResponse("admin/inbox/_conversation.html", context)
    return RedirectResponse(url=f"/admin/inbox?c={conversation_id}", status_code=303)


@router.get(
    "/{conversation_id}/contact",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_contact_context(
    conversation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    projection = team_inbox_projection.get_conversation_projection(
        db,
        conversation_id=conversation_id,
        actor_person_id=_actor_uuid_from_request(request),
    )
    if projection is None:
        return HTMLResponse(
            '<p class="p-4 text-sm text-slate-500">Contact context unavailable.</p>',
            status_code=404,
        )
    context = _ctx(request, db)
    context.update(
        {
            "timeline": projection.timeline,
            "subscriber_summary": projection.subscriber_summary,
            "contact_link_candidates": projection.contact_link_candidates,
            "conversation_labels": projection.conversation_labels,
            "label_options": projection.label_options,
            "agent_options": team_inbox_projection.list_agent_options(db),
            # The drawer surfaces customer data that is more sensitive than the
            # conversation itself. Arrears ride billing:account:read and the
            # session IP rides network:ip:read, so a support principal without
            # those keys sees the service context without the financial or
            # network detail. The customer 360 page remains the authority.
            "can_view_financials": can(request, "billing:account:read"),
            "can_view_network_detail": can(request, "network:ip:read"),
        }
    )
    return templates.TemplateResponse("admin/inbox/_contact_drawer.html", context)


def _actor_id_from_request(request: Request) -> str | None:
    from app.services import web_admin as web_admin_service

    return web_admin_service.get_actor_id(request)


def _actor_uuid_from_request(request: Request) -> UUID | None:
    actor_id = _actor_id_from_request(request)
    try:
        return UUID(actor_id) if actor_id else None
    except ValueError:
        return None


@router.post(
    "/{conversation_id}/read",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_mark_read(
    conversation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    actor_person_id = _actor_uuid_from_request(request)
    if actor_person_id is None:
        return _detail_redirect(
            conversation_id,
            status="error",
            message="Authenticated operator identity is required.",
        )
    _prepare_mutation(db)
    try:
        team_inbox_read_state.mark_conversation_read(
            db,
            team_inbox_read_state.MarkConversationReadCommand(
                context=CommandContext.system(
                    actor=f"person:{actor_person_id}",
                    scope="team-inbox:operator-read-state",
                    reason="operator explicitly marked conversation read",
                    idempotency_key=f"{actor_person_id}:{conversation_id}:read",
                ),
                conversation_id=conversation_id,
                person_id=actor_person_id,
            ),
        )
    except team_inbox_read_state.TeamInboxReadStateError as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=exc.message,
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Conversation marked read.",
    )


@router.post(
    "/{conversation_id}/reply",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_reply(
    conversation_id: UUID,
    request: Request,
    body_text: str = Form(default=""),
    macro_id: str | None = Form(default=None),
    template_id: str | None = Form(default=None),
    idempotency_key: str | None = Form(default=None),
    reply_to_message_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        outcome = team_inbox_commands.reply(
            db,
            conversation_id=conversation_id,
            body_text=body_text,
            macro_id=macro_id,
            template_id=template_id,
            idempotency_key=_query_text(idempotency_key),
            reply_to_message_id=_query_text(reply_to_message_id),
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=str(exc),
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message=(
            "Reply already submitted."
            if outcome.replayed
            else f"Reply queued from {outcome.sender}."
            if outcome.kind == "queued"
            else f"Reply sent from {outcome.sender}."
        ),
    )


@router.post(
    "/{conversation_id}/labels/create",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_label_create(
    conversation_id: UUID,
    name: str = Form(...),
    color: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_label(db, name=name, color=color)
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Label saved.",
    )


@router.post(
    "/{conversation_id}/labels/apply",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_label_apply(
    conversation_id: UUID,
    request: Request,
    label_id: str = Form(...),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.apply_label(
            db,
            conversation_id=conversation_id,
            label_id=label_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Label applied.",
    )


@router.post(
    "/{conversation_id}/labels/remove",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_label_remove(
    conversation_id: UUID,
    label_id: str = Form(...),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.remove_label(
            db,
            conversation_id=conversation_id,
            label_id=label_id,
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Label removed.",
    )


@router.post(
    "/{conversation_id}/macros/create",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_macro_create(
    conversation_id: UUID,
    request: Request,
    name: str = Form(...),
    body_text: str = Form(...),
    description: str | None = Form(default=None),
    visibility: str = Form(default="shared"),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_macro(
            db,
            name=name,
            body_text=body_text,
            description=description,
            visibility=visibility,
            actor_person_id=_actor_id_from_request(request),
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Reply macro saved.",
    )


@router.post(
    "/{conversation_id}/templates/create",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_template_create(
    conversation_id: UUID,
    name: str = Form(...),
    channel_type: str = Form(default="any"),
    subject: str | None = Form(default=None),
    body_text: str = Form(...),
    provider_template_name: str | None = Form(default=None),
    provider_template_language: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_template(
            db,
            name=name,
            channel_type=channel_type,
            subject=subject,
            body_text=body_text,
            provider_template_name=provider_template_name,
            provider_template_language=provider_template_language,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Message template saved.",
    )


@router.post(
    "/messages/{message_id}/retry",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_message_retry(
    message_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        conversation_id = team_inbox_commands.retry_message(
            db,
            message_id=message_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.MessageNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Message%20not%20found",
            status_code=303,
        )
    except team_inbox_commands.InboxCommandRejected as exc:
        return _detail_redirect(
            exc.conversation_id or "",
            status="error",
            message=str(exc),
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Message queued for retry.",
    )


@router.post(
    "/messages/retry-failed",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_retry_failed_batch(
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    retry_count = team_inbox_commands.retry_failed_batch(db, limit=50)
    return RedirectResponse(
        url=(
            "/admin/inbox/reports/outbox-failures"
            f"?status=success&message={quote_plus(f'Retried {retry_count} failed messages.')}"
        ),
        status_code=303,
    )


@router.post(
    "/{conversation_id}/workflow",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_workflow_action(
    conversation_id: UUID,
    request: Request,
    priority: int | None = Form(default=None),
    is_muted: bool | None = Form(default=None),
    snooze_minutes: int | None = Form(default=None),
    snooze_until: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.update_workflow(
            db,
            conversation_id=conversation_id,
            priority=priority,
            is_muted=is_muted,
            snooze_minutes=snooze_minutes,
            snooze_until=_parse_datetime_field(snooze_until),
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Conversation workflow updated.",
    )


@router.post(
    "/filters/save",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_saved_filter_create(
    request: Request,
    name: str = Form(...),
    search: str | None = Form(default=None),
    status_value: str | None = Form(default=None),
    channel_type: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    needs_response: bool = Form(default=False),
    contact_resolution_status: str | None = Form(default=None),
    priority_at_most: int | None = Form(default=None),
    muted: bool | None = Form(default=None),
    snoozed: bool | None = Form(default=None),
    open_only: bool = Form(default=False),
    unassigned: bool = Form(default=False),
    is_shared: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    clean_open_only = open_only if isinstance(open_only, bool) else False
    clean_unassigned = unassigned if isinstance(unassigned, bool) else False
    _prepare_mutation(db)
    try:
        team_inbox_commands.save_filter(
            db,
            name=name,
            filter_payload={
                "search": search,
                "status": status_value,
                "channel_type": channel_type,
                "service_team_id": service_team_id,
                "needs_response": needs_response,
                "contact_resolution_status": contact_resolution_status,
                "priority_at_most": priority_at_most,
                "muted": muted,
                "snoozed": snoozed,
                "open_only": clean_open_only,
                "unassigned": clean_unassigned,
            },
            actor_person_id=_actor_id_from_request(request),
            is_shared=is_shared,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/inbox?status=success&message=Saved%20filter%20created",
        status_code=303,
    )


@router.post(
    "/filters/{filter_id}/delete",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_saved_filter_delete(
    filter_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.delete_filter(
            db,
            filter_id=filter_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.InboxCommandError as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/inbox?status=success&message=Saved%20view%20deleted",
        status_code=303,
    )


@router.get(
    "/reports/outbox-failures",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_outbox_failures(
    request: Request,
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    context.update(
        {
            "messages": team_inbox_operations.list_failed_outbound_messages(db),
        }
    )
    return templates.TemplateResponse("admin/inbox/outbox_failures.html", context)


@router.post(
    "/bulk",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_bulk_action(
    request: Request,
    conversation_ids: list[str] = Form(default=[]),
    action: str = Form(...),
    status_value: str | None = Form(default=None),
    priority: int | None = Form(default=None),
    label_id: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    assigned_person_id: str | None = Form(default=None),
    auto_assign: bool = Form(default=True),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        outcome = team_inbox_commands.bulk_action(
            db,
            conversation_ids=conversation_ids,
            action=action,
            status_value=status_value,
            priority=priority,
            label_id=label_id,
            service_team_id=service_team_id,
            assigned_person_id=assigned_person_id,
            auto_assign=auto_assign,
            actor_person_id=_actor_id_from_request(request),
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/inbox?status=success&message={quote_plus(outcome.message)}",
        status_code=303,
    )


@router.post(
    "/{conversation_id}/contact-link",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_contact_link(
    conversation_id: UUID,
    request: Request,
    target_type: str = Form(...),
    subscriber_id: str | None = Form(default=None),
    reseller_id: str | None = Form(default=None),
    subscriber_id_manual: str | None = Form(default=None),
    reseller_id_manual: str | None = Form(default=None),
    note: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        outcome = team_inbox_commands.link_contact(
            db,
            conversation_id=conversation_id,
            target_type=target_type,
            subscriber_id=subscriber_id,
            reseller_id=reseller_id,
            subscriber_id_manual=subscriber_id_manual,
            reseller_id_manual=reseller_id_manual,
            actor_person_id=_actor_id_from_request(request),
            note=note,
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_contact_links.ContactLinkError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message=(
            f"Linked {outcome.channel_type.replace('_', ' ')} contact to "
            f"{outcome.target}."
        ),
    )


@router.post(
    "/{conversation_id}/note",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_internal_note(
    conversation_id: UUID,
    request: Request,
    body_text: str = Form(...),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_internal_note(
            db,
            conversation_id=conversation_id,
            body=body_text,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Internal note saved.",
    )


@router.post(
    "/{conversation_id}/comments",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_comment_create(
    conversation_id: UUID,
    request: Request,
    body_text: str = Form(...),
    message_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_comment(
            db,
            conversation_id=conversation_id,
            body=body_text,
            message_id=message_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Comment saved.",
    )


@router.post(
    "/comments/{comment_id}/resolve",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_comment_resolve(
    comment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        conversation_id = team_inbox_commands.resolve_comment(
            db,
            comment_id=comment_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Comment resolved.",
    )


@router.post(
    "/{conversation_id}/status",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_status_action(
    conversation_id: UUID,
    request: Request,
    status_value: str = Form(...),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        outcome = team_inbox_commands.update_status(
            db,
            conversation_id=conversation_id,
            status_value=status_value,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except team_inbox_commands.InboxCommandError as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=str(exc),
        )
    if outcome.already_set:
        return _detail_redirect(
            conversation_id,
            status="success",
            message=f"Conversation is already {outcome.status}.",
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message=f"Conversation marked {outcome.status.replace('_', ' ')}.",
    )


@router.post(
    "/{conversation_id}/tickets",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_issue_ticket(
    conversation_id: UUID,
    request: Request,
    title: str = Form(...),
    description: str | None = Form(default=None),
    priority: str | None = Form(default=None),
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Open a support ticket from this conversation.

    Thin adapter: `communications.conversation_ticket_handoff` owns eligibility,
    idempotency and the provenance link, and `support.ticket_lifecycle` still
    creates the ticket. This route only translates form input and outcome.
    """
    _prepare_mutation(db)
    auth = getattr(request.state, "auth", None) or {}
    try:
        result = conversation_ticket_handoff.issue_ticket(
            db,
            conversation_ticket_handoff.ConversationTicketIssueCommand(
                conversation_id=conversation_id,
                actor_id=_actor_uuid_from_request(request),
                actor_type=conversation_ticket_handoff.HandoffActorType(
                    str(auth.get("principal_type") or "system_user")
                ),
                permission_keys=frozenset({"support:ticket:update"}),
                title=title,
                description=_query_text(description),
                priority=_query_text(priority),
                reason=_query_text(reason),
                request_id=getattr(request.state, "request_id", None),
            ),
        )
    except conversation_ticket_handoff.ConversationTicketHandoffError as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=exc.message,
        )
    except ValueError as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    verb = "already open" if result.replayed else "opened"
    return _detail_redirect(
        conversation_id,
        status="success",
        message=f"Ticket {result.ticket.number or result.ticket.id} {verb}.",
    )


@router.post(
    "/{conversation_id}/assign",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_assign(
    conversation_id: UUID,
    request: Request,
    person_id: str = Form(...),
    service_team_id: str | None = Form(default=None),
    reason: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Hand one conversation to a named teammate."""
    _prepare_mutation(db)
    try:
        team_id = _query_text(service_team_id)
        if not team_id:
            projection = team_inbox_projection.get_conversation_projection(
                db,
                conversation_id=conversation_id,
                actor_person_id=_actor_uuid_from_request(request),
            )
            team_id = (
                projection.timeline.primary_service_team_id if projection else None
            )
        if not team_id:
            return _detail_redirect(
                conversation_id,
                status="error",
                message="Assign the conversation to a team before an agent.",
            )
        outcome = team_inbox_commands.assign_conversation(
            db,
            conversation_id=conversation_id,
            service_team_id=team_id,
            person_id=person_id,
            actor_person_id=_actor_id_from_request(request),
            reason=_query_text(reason),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    # The assignment owner reports refusals in the result rather than raising —
    # an agent who is not an active member of the target team comes back as
    # `invalid_agent`. Reporting success here would tell the operator the
    # conversation moved when it did not.
    if outcome.kind != "assigned":
        return _detail_redirect(
            conversation_id,
            status="error",
            message=outcome.reason or "Could not assign this conversation.",
        )
    return _detail_redirect(
        conversation_id, status="success", message="Conversation assigned."
    )


@router.post(
    "/{conversation_id}/run-macro",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_run_macro(
    conversation_id: UUID,
    request: Request,
    macro_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Execute a macro's recorded actions against this conversation.

    Distinct from inserting its body into the composer, which is text only.
    """
    _prepare_mutation(db)
    try:
        result = team_inbox_commands.run_macro(
            db,
            conversation_id=conversation_id,
            macro_id=macro_id,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return RedirectResponse(
            url="/admin/inbox?status=error&message=Conversation%20not%20found",
            status_code=303,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(conversation_id, status="error", message=str(exc))
    executed = result.get("executed") if isinstance(result, dict) else None
    return _detail_redirect(
        conversation_id,
        status="success",
        message=f"Macro applied ({executed} action{'' if executed == 1 else 's'}).",
    )


@router.get(
    "/settings/email-routes",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_email_routes(
    request: Request,
    status: str | None = Query(default=None),
    message: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Which mailbox belongs to which team.

    This table decides where inbound email lands. It is the gate on channel
    cutover: the SMTP listener only receives what is forwarded to it, and this
    is where an operator says which team owns each address.
    """
    context = _ctx(request, db)
    context.update(
        {
            "email_routes": team_inbox_routing.list_email_routes(db),
            "service_team_options": team_inbox_metrics.active_service_team_options(db),
            "notice_status": _query_text(status),
            "notice_message": _query_text(message),
        }
    )
    return templates.TemplateResponse("admin/inbox/email_routes.html", context)


def _routes_redirect(*, status: str, message: str) -> RedirectResponse:
    return RedirectResponse(
        url=(
            "/admin/inbox/settings/email-routes"
            f"?status={quote_plus(status)}&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@router.post(
    "/settings/email-routes",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_email_route_create(
    service_team_id: str = Form(...),
    email_address: str = Form(...),
    priority: int = Form(default=100),
    is_primary: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_email_route(
            db,
            service_team_id=service_team_id,
            email_address=email_address,
            is_primary=is_primary,
            priority=priority,
        )
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Mailbox routed.")


@router.post(
    "/settings/email-routes/{route_id}",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_email_route_update(
    route_id: UUID,
    priority: int | None = Form(default=None),
    is_primary: bool | None = Form(default=None),
    is_active: bool | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.update_email_route(
            db,
            route_id=route_id,
            is_primary=is_primary,
            priority=priority,
            is_active=is_active,
        )
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Mailbox route updated.")


@router.post(
    "/settings/email-routes/{route_id}/delete",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_email_route_delete(
    route_id: UUID,
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.delete_email_route(db, route_id=route_id)
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Mailbox route deactivated.")
