"""Admin team inbox routes."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.models.audit import AuditActorType
from app.models.team_inbox import InboxChannelType
from app.schemas.plan_family_catalogue import ResolveShareablePlanFamilyCatalogueQuery
from app.services import (
    conversation_lead_relationships,
    conversation_ticket_handoff,
    inbox_lead_actions,
    lead_intake_ai,
    team_inbox_agent_introduction,
    team_inbox_commands,
    team_inbox_contact_links,
    team_inbox_filters,
    team_inbox_media,
    team_inbox_metrics,
    team_inbox_operations,
    team_inbox_projection,
    team_inbox_read,
    team_inbox_read_state,
    team_inbox_routing,
)
from app.services import email as email_service
from app.services import (
    team_inbox_contact_context as contact_context_service,
)
from app.services.auth_dependencies import can, require_permission
from app.services.catalog import plan_family_catalogues
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.sales import lead_intake

router = APIRouter(prefix="/inbox", tags=["web-admin-inbox"])
settings_router = APIRouter(prefix="/crm/inbox", tags=["web-admin-inbox"])
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


class InboxPolishRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    context: str = Field(default="crm_reply", max_length=80)


def _json_object_list(value: str | None) -> tuple[dict[str, object], ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    parsed = json.loads(text)
    if not isinstance(parsed, list) or any(
        not isinstance(item, dict) for item in parsed
    ):
        raise ValueError("WhatsApp template components must be a JSON array.")
    return tuple(dict(item) for item in parsed)


def _form_flag(value: object) -> bool:
    """Read a checkbox flag, treating anything that is not a real boolean as off.

    FastAPI resolves `Form(default=False)` before the handler runs, but a direct
    call leaves the `FieldInfo` sentinel in place — and that object is truthy,
    which would turn an ordinary workflow submit into an until-reply snooze.
    """
    return value is True


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
    filters: str | None = Query(default=None),
    assigned_person_id: str | None = Query(default=None),
    needs_response: bool = Query(default=False),
    needs_attention: bool = Query(default=False),
    contact_resolution_status: str | None = Query(default=None),
    priority_at_most: int | None = Query(default=None),
    muted: bool | None = Query(default=None),
    snoozed: bool | None = Query(default=None),
    open_only: bool = Query(default=False),
    unassigned: bool = Query(default=False),
    unread: bool = Query(default=False),
    # Declared `bool | None` like `muted`/`snoozed`, not `str | None`: these ride
    # `_query_optional_bool`, which keeps only real booleans. Typed as strings the
    # checkbox value "true" was discarded at the adapter and neither filter ever
    # reached the read model.
    ai_handling: bool | None = Query(default=None),
    has_ticket: bool | None = Query(default=None),
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
    htmx_target = getattr(request, "headers", {}).get("hx-target")
    is_sidebar_request = htmx_target == "inbox-sidebar-content"
    is_queue_request = htmx_target == "inbox-conversation-queue"
    is_list_fragment_request = is_sidebar_request or is_queue_request
    actor_id = _actor_id_from_request(request)
    try:
        actor_person_id = UUID(actor_id) if actor_id else None
    except ValueError:
        actor_person_id = None
    try:
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
                advanced_filters=team_inbox_filters.InboxAdvancedFilterPayload(
                    raw_json=_query_text(filters)
                ),
                assigned_person_id=_query_text(assigned_person_id),
                needs_response=_query_bool(needs_response),
                needs_attention=_query_bool(needs_attention),
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
                selected_conversation_id=(
                    _query_text(conversation_id) or _query_text(c)
                ),
                actor_person_id=actor_person_id,
                composition=(
                    team_inbox_projection.InboxQueueComposition.sidebar
                    if is_list_fragment_request
                    else team_inbox_projection.InboxQueueComposition.full_workspace
                ),
            ),
        )
    except team_inbox_filters.InboxFilterError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    if projection.canonical_url is not None:
        return RedirectResponse(url=projection.canonical_url, status_code=307)
    can_manage_inbox = can(request, "support:ticket:update")
    manager_dashboard = (
        team_inbox_projection.build_manager_dashboard_projection(
            db,
            queue_metrics=projection.queue_metrics,
            needs_attention=projection.assignment_counts.needs_attention,
        )
        if can_manage_inbox and not is_list_fragment_request
        else None
    )
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
            "filters": projection.advanced_filters_json,
            "assigned_person_id": projection.assigned_person_id,
            "needs_response": projection.needs_response,
            "needs_attention": projection.needs_attention,
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
            "agent_presence": projection.agent_presence,
            "assignment_counts": projection.assignment_counts,
            "status_options": projection.status_options,
            "channel_options": projection.channel_options,
            "priority_options": projection.priority_options,
            "label_options": projection.label_options,
            "saved_filters": projection.saved_filters,
            "new_conversation_template_options": (
                tuple(team_inbox_operations.list_templates(db))
                if not is_list_fragment_request
                else ()
            ),
            "can_manage_inbox": can_manage_inbox,
            "can_manage_leads": can(request, "crm:lead:write"),
            "manager_dashboard": manager_dashboard,
            "selected": (
                projection.selected.timeline
                if projection.selected is not None
                else None
            ),
            "selected_id": projection.selected_id or "",
            "actor_person_id": str(actor_person_id) if actor_person_id else "",
            "agent_introduction_text": (
                team_inbox_agent_introduction.rendered_introduction(db, actor_person_id)
                if actor_person_id
                else ""
            ),
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
                "catalogue_options": projection.selected.catalogue_options,
                "action_eligibility": projection.selected.action_eligibility,
                "is_unread": projection.selected.is_unread,
                "priority_options": projection.selected.priority_options,
            }
        )
    if is_list_fragment_request:
        return templates.TemplateResponse("admin/inbox/_sidebar.html", context)
    return templates.TemplateResponse("admin/inbox/index.html", context)


@router.get(
    "/whatsapp-contacts",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_whatsapp_contacts(
    search: str = Query(default=""),
    db: Session = Depends(get_db),
):
    contacts = team_inbox_projection.list_whatsapp_contacts(
        db,
        search=search,
        limit=20,
    )
    return {
        "contacts": [
            {
                "id": contact.id,
                "name": contact.name,
                "whatsapp_address": contact.whatsapp_address,
            }
            for contact in contacts
        ]
    }


@router.get(
    "/whatsapp-templates",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_whatsapp_templates(
    db: Session = Depends(get_db),
):
    try:
        from app.services.integrations import whatsapp_capability

        rows = whatsapp_capability.list_approved_templates(db)
    except Exception:
        logger.warning("inbox_whatsapp_template_list_failed")
        return JSONResponse(
            {"templates": [], "error": "WhatsApp templates are unavailable."},
            status_code=200,
        )
    return {"templates": list(rows)}


def _detail_redirect(
    conversation_id: str | UUID,
    *,
    status: str,
    message: str,
    next_url: str | None = None,
) -> RedirectResponse:
    target = str(next_url or "").strip()
    parsed = urlsplit(target)
    if (
        target
        and not parsed.scheme
        and not parsed.netloc
        and parsed.path == "/admin/inbox"
    ):
        query_items = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in {"c", "status", "message"}
        ]
        query_items.extend(
            (
                ("c", str(conversation_id)),
                ("status", status),
                ("message", message),
            )
        )
        return RedirectResponse(
            url=urlunsplit(("", "", parsed.path, urlencode(query_items), "")),
            status_code=303,
        )
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
            "catalogue_options": projection.catalogue_options,
            "action_eligibility": projection.action_eligibility,
            "is_unread": projection.is_unread,
            "actor_person_id": str(actor_person_id) if actor_person_id else "",
            "agent_introduction_text": (
                team_inbox_agent_introduction.rendered_introduction(db, actor_person_id)
                if actor_person_id
                else ""
            ),
            "priority_options": projection.priority_options,
            "agent_options": team_inbox_projection.list_agent_options(db),
            "can_manage_leads": can(request, "crm:lead:write"),
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
    contact_context = contact_context_service.build_contact_context(
        db,
        conversation_id=conversation_id,
        permissions=contact_context_service.InboxContactContextPermissions(
            can_read_profile=can(request, "customer:read"),
            can_edit_profile=can(request, "customer:write"),
            can_read_leads=can(request, "crm:lead:read"),
            can_write_leads=can(request, "crm:lead:write"),
            can_read_tickets=can(request, "support:ticket:read"),
            can_read_projects=can(request, "project:read"),
            can_read_project_tasks=can(request, "project:task:read"),
        ),
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
            "can_manage_leads": can(request, "crm:lead:write"),
            "contact_context": contact_context,
            "lead_intake_invitations": lead_intake.invitation_for_conversation(
                db, conversation_id
            ),
        }
    )
    return templates.TemplateResponse("admin/inbox/_contact_drawer.html", context)


def _inbox_action_permissions(
    request: Request,
) -> inbox_lead_actions.InboxActionPermissions:
    return inbox_lead_actions.InboxActionPermissions(
        can_read_profile=can(request, "customer:read"),
        can_edit_profile=can(request, "customer:write"),
        can_read_leads=can(request, "crm:lead:read"),
        can_write_leads=can(request, "crm:lead:write"),
    )


@router.post(
    "/{conversation_id}/actions/{intent}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def resolve_inbox_action(
    conversation_id: UUID,
    intent: inbox_lead_actions.InboxActionIntent,
    request: Request,
    pipeline_id: UUID | None = Form(default=None),
    lead_id: UUID | None = Form(default=None),
    create_confirmed: bool = Form(default=False),
    title: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    permissions = _inbox_action_permissions(request)
    action = inbox_lead_actions.resolve_action(
        db,
        conversation_id=conversation_id,
        intent=intent,
        permissions=permissions,
        selected_pipeline_id=pipeline_id,
    )
    if action.action_type == inbox_lead_actions.InboxResolvedActionType.unauthorized:
        return HTMLResponse("Action restricted.", status_code=403)
    if action.destination and not action.requires_link:
        return RedirectResponse(action.destination, status_code=303)
    actor_system_user_id = _system_user_uuid_from_request(request)
    actor_person_id = _actor_uuid_from_request(request)
    if action.requires_link and action.party_id and action.lead_id:
        finish_read_transaction(db)
        try:
            inbox_lead_actions.link_existing_lead(
                db,
                inbox_lead_actions.LinkExistingLeadCommand(
                    context=CommandContext.system(
                        actor=f"system-user:{actor_system_user_id}",
                        scope="inbox:lead-link",
                        reason="Authorized Inbox resolver reused the exact Party Lead",
                        idempotency_key=f"inbox:{conversation_id}:lead:{action.lead_id}",
                    ),
                    conversation_id=conversation_id,
                    party_id=action.party_id,
                    lead_id=action.lead_id,
                    actor_person_id=actor_person_id,
                    source=conversation_lead_relationships.ConversationLeadLinkSource.exact_party_lead,
                ),
            )
        except DomainError as exc:
            return _detail_redirect(
                conversation_id, status="error", message=exc.message
            )
        return _detail_redirect(
            conversation_id, status="success", message="Conversation linked to Lead."
        )
    if (
        lead_id is not None
        and action.action_type == inbox_lead_actions.InboxResolvedActionType.select_lead
        and action.party_id is not None
        and any(option.id == lead_id for option in action.leads)
    ):
        finish_read_transaction(db)
        try:
            inbox_lead_actions.link_existing_lead(
                db,
                inbox_lead_actions.LinkExistingLeadCommand(
                    context=CommandContext.system(
                        actor=f"system-user:{actor_system_user_id}",
                        scope="inbox:lead-selection",
                        reason="Authorized operator selected an exact Party Lead",
                        idempotency_key=f"inbox:{conversation_id}:lead:{lead_id}",
                    ),
                    conversation_id=conversation_id,
                    party_id=action.party_id,
                    lead_id=lead_id,
                    actor_person_id=actor_person_id,
                    source=conversation_lead_relationships.ConversationLeadLinkSource.reviewed_selection,
                ),
            )
        except DomainError as exc:
            return _detail_redirect(
                conversation_id, status="error", message=exc.message
            )
        return _detail_redirect(
            conversation_id, status="success", message="Conversation linked to Lead."
        )
    if (
        create_confirmed
        and action.action_type
        == inbox_lead_actions.InboxResolvedActionType.create_lead_for_party
        and action.party_id is not None
        and action.pipeline_id is not None
    ):
        finish_read_transaction(db)
        try:
            inbox_lead_actions.create_lead_for_party(
                db,
                inbox_lead_actions.CreateLeadForPartyCommand(
                    context=CommandContext.system(
                        actor=f"system-user:{actor_system_user_id}",
                        scope="inbox:lead-authoring",
                        reason="Authorized operator created a Lead for an exact Inbox Party",
                        idempotency_key=(
                            f"inbox:{conversation_id}:pipeline:{action.pipeline_id}:create"
                        ),
                    ),
                    conversation_id=conversation_id,
                    party_id=action.party_id,
                    pipeline_id=action.pipeline_id,
                    stage_id=None,
                    actor_system_user_id=actor_system_user_id,
                    actor_person_id=actor_person_id,
                    title=title or "Inbox prospect",
                ),
            )
        except DomainError as exc:
            return _detail_redirect(
                conversation_id, status="error", message=exc.message
            )
        return _detail_redirect(
            conversation_id, status="success", message="Lead created and linked."
        )
    context = _ctx(request, db)
    context.update({"action": action, "timeline_id": conversation_id})
    return templates.TemplateResponse("admin/inbox/action_resolver.html", context)


def _actor_id_from_request(request: Request) -> str | None:
    from app.services import web_admin as web_admin_service

    return web_admin_service.get_actor_id(request)


def _actor_uuid_from_request(request: Request) -> UUID | None:
    actor_id = _actor_id_from_request(request)
    try:
        return UUID(actor_id) if actor_id else None
    except ValueError:
        return None


def _system_user_uuid_from_request(request: Request) -> UUID:
    value = str(getattr(getattr(request.state, "user", None), "id", "") or "")
    try:
        return UUID(value)
    except ValueError as exc:
        raise lead_intake.LeadIntakeError(
            "actor_not_eligible",
            "An authenticated staff user is required.",
            kind="forbidden",
        ) from exc


@router.post(
    "/{conversation_id}/lead-intake/issue",
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def team_inbox_lead_intake_issue(
    conversation_id: UUID,
    request: Request,
    party_type: lead_intake.LeadIntakePartyType = Form(...),
    db: Session = Depends(get_db),
):
    actor_id = _system_user_uuid_from_request(request)
    message_id = lead_intake.latest_inbound_message_id(db, conversation_id)
    if message_id is None:
        return _detail_redirect(
            conversation_id,
            status="error",
            message="No inbound message is available for this invitation.",
        )
    finish_read_transaction(db)
    try:
        outcome = lead_intake.issue_manual_invitation(
            db,
            lead_intake.ManualInvitationCommand(
                context=CommandContext.system(
                    actor=f"system_user:{actor_id}",
                    scope="sales.lead_intake:write",
                    reason="staff issued Lead intake invitation",
                    idempotency_key=f"lead-intake-manual:{conversation_id}:{message_id}",
                ),
                conversation_id=conversation_id,
                trigger_message_id=message_id,
                party_type=party_type,
                actor_system_user_id=actor_id,
            ),
        )
        body = lead_intake_ai.render_invitation_message(db, outcome)
        finish_read_transaction(db)
        try:
            reply = team_inbox_commands.reply(
                db,
                conversation_id=conversation_id,
                body_text=body,
                actor_person_id=_actor_id_from_request(request),
                idempotency_key=f"lead-intake:manual-delivery:{outcome.invitation_id}",
            )
            delivery_status = reply.kind
            outbound_message_id = UUID(reply.message_id) if reply.message_id else None
            delivery_error = None
        except team_inbox_commands.InboxCommandError as exc:
            delivery_status = "failed"
            outbound_message_id = None
            delivery_error = exc.code
        assert outcome.invitation_id is not None
        lead_intake.record_invitation_delivery(
            db,
            lead_intake.InvitationDeliveryCommand(
                context=CommandContext.system(
                    actor=f"system_user:{actor_id}",
                    scope="sales.lead_intake:write",
                    reason="record manual invitation delivery",
                    idempotency_key=(
                        f"lead-intake:manual-delivery-record:{outcome.invitation_id}"
                    ),
                ),
                invitation_id=outcome.invitation_id,
                message_id=outbound_message_id,
                delivery_status=delivery_status,
                error_code=delivery_error,
            ),
        )
    except (lead_intake.LeadIntakeError, ValueError) as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=getattr(exc, "message", str(exc)),
        )
    if delivery_status == "failed":
        return _detail_redirect(
            conversation_id,
            status="error",
            message=(
                "The form was issued but could not be sent. Revoke it before reissuing."
            ),
        )
    return _detail_redirect(
        conversation_id, status="success", message="Lead intake form sent."
    )


@router.post(
    "/{conversation_id}/lead-intake/{invitation_id}/revoke",
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def team_inbox_lead_intake_revoke(
    conversation_id: UUID,
    invitation_id: UUID,
    request: Request,
    reason: str = Form(default="Reissued by staff"),
    db: Session = Depends(get_db),
):
    actor_id = _system_user_uuid_from_request(request)
    finish_read_transaction(db)
    try:
        lead_intake.revoke_invitation(
            db,
            lead_intake.RevokeInvitationCommand(
                context=CommandContext.system(
                    actor=f"system_user:{actor_id}",
                    scope="sales.lead_intake:write",
                    reason="staff revoked Lead intake invitation",
                    idempotency_key=f"lead-intake-revoke:{invitation_id}",
                ),
                invitation_id=invitation_id,
                conversation_id=conversation_id,
                actor_system_user_id=actor_id,
                reason=reason,
            ),
        )
    except lead_intake.LeadIntakeError as exc:
        return _detail_redirect(conversation_id, status="error", message=exc.message)
    return _detail_redirect(
        conversation_id,
        status="success",
        message="Lead intake invitation revoked. You can now issue a new link.",
    )


def _audit_actor_type(principal_type: str) -> AuditActorType:
    """Map the transport principal onto the audit actor vocabulary.

    Same mapping `conversation_ticket_handoff` uses: a staff principal audits
    as `user`, because the audit vocabulary has no `system_user` member.
    """
    if principal_type == "api_key":
        return AuditActorType.api_key
    if principal_type == "service":
        return AuditActorType.service
    return AuditActorType.user


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
    attachment_ids: str | None = Form(default=None),
    send_after: str | None = Form(default=None),
    idempotency_key: str | None = Form(default=None),
    reply_to_message_id: str | None = Form(default=None),
    next_url: str | None = Form(default=None),
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
            attachment_ids=[
                item.strip()
                for item in (_query_text(attachment_ids) or "").split(",")
                if item.strip()
            ],
            send_after=_parse_datetime_field(send_after),
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
            next_url=next_url,
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message=(
            "Reply already submitted."
            if outcome.replayed
            else "Reply scheduled."
            if outcome.kind == "scheduled"
            else f"Reply queued from {outcome.sender}."
            if outcome.kind == "queued"
            else f"Reply sent from {outcome.sender}."
        ),
        next_url=next_url,
    )


@router.post(
    "/{conversation_id}/share-catalogue",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_share_catalogue(
    conversation_id: UUID,
    request: Request,
    plan_family: str = Form(...),
    idempotency_key: str | None = Form(default=None),
    next_url: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    try:
        catalogue = plan_family_catalogues.resolve_shareable_catalogue(
            db,
            ResolveShareablePlanFamilyCatalogueQuery(plan_family=plan_family),
        )
        download_url = str(
            request.url_for(
                "public_catalogue_download", catalogue_id=str(catalogue.catalogue_id)
            )
        )
        body = f"Here is our {catalogue.display_name}: {download_url}"
        _prepare_mutation(db)
        outcome = team_inbox_commands.reply(
            db,
            conversation_id=conversation_id,
            body_text=body,
            idempotency_key=_query_text(idempotency_key),
            actor_person_id=_actor_id_from_request(request),
        )
    except plan_family_catalogues.PlanFamilyCatalogueError as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=exc.message,
            next_url=next_url,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_operations.InboxOperationError,
    ) as exc:
        return _detail_redirect(
            conversation_id,
            status="error",
            message=str(exc),
            next_url=next_url,
        )
    return _detail_redirect(
        conversation_id,
        status="success",
        message=(
            "Catalogue share already submitted."
            if outcome.replayed
            else f"{catalogue.display_name} queued."
        ),
        next_url=next_url,
    )


@router.post(
    "/{conversation_id}/ai-draft",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_ai_draft(
    conversation_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    report = team_inbox_projection.build_ai_reply_projection(
        db,
        conversation_id=conversation_id,
    )
    if report is None:
        return JSONResponse(
            {"ok": False, "error": "AI Draft Unavailable"},
            status_code=200,
        )
    _prepare_mutation(db)
    try:
        from app.services.ai.engine import AIEngineError, intelligence_engine

        insight = intelligence_engine.advise(
            db,
            advisor_key="inbox_analyst",
            report=report,
            entity_type="inbox_conversation",
            entity_id=str(conversation_id),
            trigger="manual",
            triggered_by_system_user_id=_actor_id_from_request(request),
        )
    except (AIEngineError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "AI Draft Unavailable"},
            status_code=200,
        )
    except Exception:
        logger.warning(
            "inbox_ai_draft_failed conversation_id=%s",
            conversation_id,
        )
        return JSONResponse(
            {"ok": False, "error": "AI Draft Unavailable"},
            status_code=200,
        )
    output = dict(insight.structured_output or {})
    draft = str(output.get("draft") or "").strip()
    if not draft:
        return JSONResponse(
            {"ok": False, "error": "AI Draft Unavailable"},
            status_code=200,
        )
    return JSONResponse(
        {
            "ok": True,
            "draft": draft,
            "tone": output.get("tone"),
            "title": output.get("title"),
            "summary": output.get("summary"),
            "meta": {
                "provider": insight.llm_provider,
                "model": insight.llm_model,
                "endpoint": insight.llm_endpoint,
            },
        }
    )


@router.post(
    "/{conversation_id}/ai-polish",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_ai_polish(
    conversation_id: UUID,
    payload: InboxPolishRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        return JSONResponse(
            {"ok": False, "error": "Suggestion unavailable."},
            status_code=200,
        )
    text = payload.text.strip()
    if not text:
        return JSONResponse(
            {"ok": False, "error": "Enter text to polish."},
            status_code=200,
        )
    _prepare_mutation(db)
    try:
        from app.services.ai.engine import AIEngineError, intelligence_engine

        insight = intelligence_engine.advise(
            db,
            advisor_key="inbox_sentence_polish",
            report={"text": text, "context": payload.context},
            entity_type="inbox_composer",
            entity_id=str(conversation_id),
            trigger="manual",
            triggered_by_system_user_id=_actor_id_from_request(request),
        )
    except (AIEngineError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "Suggestion unavailable."},
            status_code=200,
        )
    except Exception:
        logger.warning(
            "inbox_ai_polish_failed conversation_id=%s",
            conversation_id,
        )
        return JSONResponse(
            {"ok": False, "error": "Suggestion unavailable."},
            status_code=200,
        )
    output = dict(insight.structured_output or {})
    suggestion = str(output.get("suggested_text") or "").strip()
    alternatives = output.get("alternatives")
    if not suggestion:
        return JSONResponse(
            {"ok": False, "error": "Suggestion unavailable."},
            status_code=200,
        )
    return JSONResponse(
        {
            "ok": True,
            "suggested_text": suggestion,
            "alternatives": (
                [str(value) for value in alternatives[:2]]
                if isinstance(alternatives, list)
                else []
            ),
            "meta": {
                "provider": insight.llm_provider,
                "model": insight.llm_model,
                "endpoint": insight.llm_endpoint,
            },
        }
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
    snooze_until_reply: bool = Form(default=False),
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
            snooze_until_reply=_form_flag(snooze_until_reply),
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
    service_team_ids: str | None = Form(default=None),
    filters: str | None = Form(default=None),
    assigned_person_id: str | None = Form(default=None),
    needs_response: bool = Form(default=False),
    needs_attention: bool = Form(default=False),
    contact_resolution_status: str | None = Form(default=None),
    priority_at_most: int | None = Form(default=None),
    muted: bool | None = Form(default=None),
    snoozed: bool | None = Form(default=None),
    open_only: bool = Form(default=False),
    unassigned: bool = Form(default=False),
    unread: bool = Form(default=False),
    ai_handling: bool = Form(default=False),
    has_ticket: bool = Form(default=False),
    activity_from: str | None = Form(default=None),
    activity_to: str | None = Form(default=None),
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
            filter_payload=team_inbox_filters.InboxSavedFilterPayload(
                search=_query_text(search),
                status=_query_text(status_value),
                channel_type=_query_text(channel_type),
                service_team_id=_query_text(service_team_id),
                service_team_ids=_query_text(service_team_ids),
                advanced_filters_json=_query_text(filters),
                assigned_person_id=_query_text(assigned_person_id),
                needs_response=_query_bool(needs_response),
                needs_attention=_query_bool(needs_attention),
                contact_resolution_status=_query_text(contact_resolution_status),
                priority_at_most=_query_int(priority_at_most),
                muted=_query_optional_bool(muted),
                snoozed=_query_optional_bool(snoozed),
                open_only=clean_open_only,
                unassigned=clean_unassigned,
                unread=_query_bool(unread),
                ai_handling=_query_bool(ai_handling),
                has_ticket=_query_bool(has_ticket),
                activity_from=_query_text(activity_from),
                activity_to=_query_text(activity_to),
            ),
            actor_person_id=_actor_id_from_request(request),
            is_shared=is_shared,
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_filters.InboxFilterError,
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
    "/presence",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_presence_action(
    request: Request,
    status_value: str = Form(...),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        outcome = team_inbox_commands.set_agent_presence(
            db,
            actor_person_id=_actor_id_from_request(request),
            status=status_value,
        )
    except team_inbox_commands.InboxCommandError as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    message = (
        f"You were already {outcome.status}."
        if outcome.already_set
        else f"Availability set to {outcome.status}."
    )
    return RedirectResponse(
        url=f"/admin/inbox?status=success&message={quote_plus(message)}",
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
    return templates.TemplateResponse(
        "admin/inbox/email_routes.html",
        _settings_context(request, db, status=status, message=message),
    )


def _settings_context(
    request: Request,
    db: Session,
    *,
    status: str | None = None,
    message: str | None = None,
) -> dict:
    context = _ctx(request, db)
    actor_person_id = _actor_uuid_from_request(request)
    introduction_preference = (
        team_inbox_agent_introduction.preference_for_agent(db, actor_person_id)
        if actor_person_id
        else None
    )
    context.update(
        {
            "email_routes": team_inbox_routing.list_email_routes(db),
            "channel_routes": team_inbox_routing.list_channel_routes(db),
            "ai_routes": team_inbox_routing.list_ai_routes(db),
            "service_team_options": team_inbox_metrics.active_service_team_options(db),
            "smtp_sender_options": email_service.list_smtp_sender_options(db),
            "channel_options": [
                {
                    "value": item.value,
                    "label": item.value.replace("_", " ").title(),
                }
                for item in InboxChannelType
                if item.value not in {"email", "note", "field_job"}
            ],
            "ai_channel_options": [
                {"value": "any", "label": "Any channel"},
                *[
                    {
                        "value": item.value,
                        "label": item.value.replace("_", " ").title(),
                    }
                    for item in InboxChannelType
                    if item.value not in {"note", "field_job"}
                ],
            ],
            "notice_status": _query_text(status),
            "notice_message": _query_text(message),
            "introduction_preference": introduction_preference,
        }
    )
    return context


@settings_router.post(
    "/agent-introduction",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_agent_introduction_update(
    request: Request,
    template: str = Form(...),
    auto_send_chat_widget: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    actor_person_id = _actor_uuid_from_request(request)
    if actor_person_id is None:
        return _routes_redirect(status="error", message="Agent identity is required.")
    try:
        team_inbox_agent_introduction.update_preference_committed(
            db,
            team_inbox_agent_introduction.UpdateAgentIntroductionCommand(
                context=CommandContext.system(
                    actor=f"person:{actor_person_id}",
                    scope="team-inbox:agent-introduction",
                    reason="update personal Team Inbox introduction",
                ),
                person_id=actor_person_id,
                template=template,
                auto_send_chat_widget=auto_send_chat_widget,
            ),
        )
    except ValueError as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Introduction preference saved.")


@settings_router.get(
    "/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def team_inbox_settings_entrypoint(
    request: Request,
    status: str | None = Query(default=None),
    message: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Render channel and AI routing at the requested CRM-shaped settings URL."""

    return templates.TemplateResponse(
        "admin/inbox/email_routes.html",
        _settings_context(request, db, status=status, message=message),
    )


def _routes_redirect(*, status: str, message: str) -> RedirectResponse:
    return RedirectResponse(
        url=(
            "/admin/crm/inbox/settings"
            f"?status={quote_plus(status)}&message={quote_plus(message)}"
        ),
        status_code=303,
    )


@router.post(
    "/settings/email-routes",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
@settings_router.post(
    "/email-routes",
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
@settings_router.post(
    "/email-routes/{route_id}",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_email_route_update(
    route_id: UUID,
    priority: int | None = Form(default=None),
    is_primary: bool | None = Form(default=None),
    is_active: bool | None = Form(default=None),
    outbound_email_sender_key: str | None = Form(default=None),
    update_outbound_email_sender: bool = Form(default=False),
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
            outbound_email_sender_key=_query_text(outbound_email_sender_key),
            update_outbound_email_sender=_form_flag(update_outbound_email_sender),
        )
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Mailbox route updated.")


@router.post(
    "/settings/email-routes/{route_id}/delete",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
@settings_router.post(
    "/email-routes/{route_id}/delete",
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


@settings_router.post(
    "/channel-routes",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_channel_route_create(
    channel_type: str = Form(...),
    provider: str = Form("default"),
    account_scope: str = Form("default"),
    display_name: str | None = Form(default=None),
    service_team_id: str = Form(..., alias="channel_service_team_id"),
    priority: int = Form(default=100),
    allow_ai_routing: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_channel_route(
            db,
            channel_type=channel_type,
            provider=provider,
            account_scope=account_scope,
            display_name=display_name,
            service_team_id=service_team_id,
            allow_ai_routing=allow_ai_routing,
            priority=priority,
        )
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Channel route saved.")


@settings_router.post(
    "/channel-routes/{route_id}",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_channel_route_update(
    route_id: UUID,
    display_name: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None, alias="channel_service_team_id"),
    priority: int | None = Form(default=None),
    allow_ai_routing: bool | None = Form(default=None),
    is_active: bool | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.update_channel_route(
            db,
            route_id=route_id,
            display_name=display_name,
            service_team_id=service_team_id,
            allow_ai_routing=allow_ai_routing,
            priority=priority,
            is_active=is_active,
        )
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Channel route updated.")


@settings_router.post(
    "/channel-routes/{route_id}/delete",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_channel_route_delete(
    route_id: UUID,
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.delete_channel_route(db, route_id=route_id)
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="Channel route deactivated.")


@settings_router.post(
    "/ai-routes",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_ai_route_create(
    channel_type: str = Form("any"),
    intent_key: str = Form(...),
    display_name: str | None = Form(default=None),
    service_team_id: str = Form(..., alias="ai_service_team_id"),
    confidence_threshold: float = Form(default=0.75),
    priority: int = Form(default=100),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.create_ai_route(
            db,
            channel_type=channel_type,
            intent_key=intent_key,
            display_name=display_name,
            service_team_id=service_team_id,
            confidence_threshold=confidence_threshold,
            priority=priority,
        )
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="AI intake route saved.")


@settings_router.post(
    "/ai-routes/{route_id}",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_ai_route_update(
    route_id: UUID,
    display_name: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None, alias="ai_service_team_id"),
    confidence_threshold: float | None = Form(default=None),
    priority: int | None = Form(default=None),
    is_active: bool | None = Form(default=None),
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.update_ai_route(
            db,
            route_id=route_id,
            display_name=display_name,
            service_team_id=service_team_id,
            confidence_threshold=confidence_threshold,
            priority=priority,
            is_active=is_active,
        )
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="AI intake route updated.")


@settings_router.post(
    "/ai-routes/{route_id}/delete",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_ai_route_delete(
    route_id: UUID,
    db: Session = Depends(get_db),
):
    _prepare_mutation(db)
    try:
        team_inbox_commands.delete_ai_route(db, route_id=route_id)
    except (team_inbox_routing.EmailRouteError, ValueError) as exc:
        return _routes_redirect(status="error", message=str(exc))
    return _routes_redirect(status="success", message="AI intake route deactivated.")


@router.post(
    "/{conversation_id}/attachments",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
async def team_inbox_stage_attachments(
    conversation_id: UUID,
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Stage operator uploads and return their ids for the composer.

    Reading the uploads is async, so the bytes are collected before entering the
    owner command — the command boundary is synchronous and must not await.
    """
    uploads: list[tuple[str, str | None, bytes]] = []
    for upload in files:
        data = await upload.read()
        uploads.append((upload.filename or "attachment", upload.content_type, data))

    _prepare_mutation(db)
    try:
        staged = team_inbox_commands.stage_attachments(
            db,
            conversation_id=conversation_id,
            uploads=uploads,
            actor_person_id=_actor_id_from_request(request),
        )
    except team_inbox_commands.ConversationNotFoundError:
        return JSONResponse({"error": "Conversation not found."}, status_code=404)
    except (team_inbox_media.MediaUploadError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"attachment_ids": staged})


@router.post(
    "/voice/transcription",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
async def team_inbox_voice_transcription(
    request: Request,
    audio: UploadFile = File(...),
    context: str = Form(...),
    duration_ms: int = Form(...),
    db: Session = Depends(get_db),
):
    from app.services.ai import voice_transcription
    from app.services.audit_adapter import record_audit_event
    from app.services.rate_limiter_adapter import allow_operation

    actor_id = _actor_id_from_request(request)
    decision = allow_operation(
        f"team-inbox:voice:{actor_id or 'unknown'}",
        limit=10,
        window_seconds=60,
    )
    if not decision.allowed:
        return JSONResponse(
            {"ok": False, "error": "Too many voice requests. Try again shortly."},
            status_code=200,
        )
    installation_decision = allow_operation(
        "team-inbox:voice:installation",
        limit=60,
        window_seconds=60,
    )
    if not installation_decision.allowed:
        return JSONResponse(
            {"ok": False, "error": "Voice transcription is busy. Try again shortly."},
            status_code=200,
        )
    if not actor_id or not voice_transcription.acquire_actor_slot(actor_id):
        return JSONResponse(
            {
                "ok": False,
                "error": "A voice transcription is already in progress.",
            },
            status_code=200,
        )

    try:
        data = b""
        content_type = voice_transcription.normalized_content_type(audio.content_type)
        try:
            data = await audio.read(voice_transcription.MAX_AUDIO_BYTES + 1)
        finally:
            await audio.close()

        try:
            result = voice_transcription.transcribe(
                db,
                audio=data,
                content_type=content_type,
                context=context,
                duration_ms=duration_ms,
            )
        except voice_transcription.VoiceTranscriptionError as exc:
            finish_read_transaction(db)
            record_audit_event(
                db,
                action="voice_transcription_failed",
                entity_type="inbox_voice_transcription",
                actor_type=AuditActorType.user,
                actor_id=actor_id,
                is_success=False,
                status_code=400,
                request_id=request.headers.get("x-request-id"),
                metadata={
                    "context": context,
                    "audio_byte_count": len(data),
                    "duration_ms": duration_ms,
                    "content_type": content_type,
                    "outcome": exc.code,
                },
            )
            return JSONResponse(
                {"ok": False, "error": str(exc), "text": ""},
                status_code=200,
            )
        except Exception:
            finish_read_transaction(db)
            record_audit_event(
                db,
                action="voice_transcription_failed",
                entity_type="inbox_voice_transcription",
                actor_type=AuditActorType.user,
                actor_id=actor_id,
                is_success=False,
                status_code=500,
                request_id=request.headers.get("x-request-id"),
                metadata={
                    "context": context,
                    "audio_byte_count": len(data),
                    "duration_ms": duration_ms,
                    "content_type": content_type,
                    "outcome": "ai.voice_transcription.unexpected_failure",
                },
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Transcription is unavailable. Please try again.",
                    "text": "",
                },
                status_code=200,
            )
        finish_read_transaction(db)
        record_audit_event(
            db,
            action="voice_transcription_completed",
            entity_type="inbox_voice_transcription",
            actor_type=AuditActorType.user,
            actor_id=actor_id,
            request_id=request.headers.get("x-request-id"),
            metadata={
                "context": context,
                "audio_byte_count": len(data),
                "duration_ms": duration_ms,
                "content_type": content_type,
                "provider": result.provider,
                "model": result.model,
                "endpoint": result.endpoint,
                "outcome": "completed",
                "retry_count": result.retry_count,
                "elapsed_ms": result.elapsed_ms,
            },
        )
        return JSONResponse(
            {
                "ok": True,
                "text": result.text,
                "meta": {
                    "provider": result.provider,
                    "model": result.model,
                },
            }
        )
    finally:
        voice_transcription.release_actor_slot(actor_id)


async def _read_new_conversation_uploads(
    files: list[UploadFile],
) -> list[tuple[str, str | None, bytes]]:
    """Normalize browser multipart file parts for the Inbox command owner."""
    uploads: list[tuple[str, str | None, bytes]] = []
    for upload in files:
        data = await upload.read()
        filename = str(upload.filename or "").strip()
        if not filename and not data:
            # Browsers may serialize an untouched file input as an empty
            # multipart part. That is transport-level "no attachment", not a
            # selected zero-byte file. Named empty files still reach the media
            # owner and retain its fail-closed validation.
            continue
        uploads.append((filename or "attachment", upload.content_type, data))
    return uploads


@router.post(
    "/conversations",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
async def team_inbox_start_conversation(
    request: Request,
    channel_type: str = Form(...),
    contact_address: str = Form(...),
    body_text: str = Form(...),
    subject: str | None = Form(default=None),
    service_team_id: str | None = Form(default=None),
    contact_name: str | None = Form(default=None),
    contact_id: str | None = Form(default=None),
    contact_country_code: str | None = Form(default=None),
    template_id: str | None = Form(default=None),
    template_values: str | None = Form(default=None),
    whatsapp_template_name: str | None = Form(default=None),
    whatsapp_template_language: str | None = Form(default=None),
    whatsapp_template_components: str | None = Form(default=None),
    cc: str | None = Form(default=None),
    bcc: str | None = Form(default=None),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Open a new conversation and send its first message."""
    uploads = await _read_new_conversation_uploads(files)
    _prepare_mutation(db)
    try:
        outcome = team_inbox_commands.start_conversation(
            db,
            channel_type=channel_type,
            contact_address=contact_address,
            body_text=body_text,
            subject=_query_text(subject),
            service_team_id=_query_text(service_team_id),
            actor_person_id=_actor_id_from_request(request),
            contact_name=_query_text(contact_name),
            contact_party_id=_query_text(contact_id),
            contact_country_code=_query_text(contact_country_code),
            template_id=_query_text(template_id),
            template_values=tuple(
                value.strip()
                for value in (_query_text(template_values) or "").splitlines()
                if value.strip()
            ),
            whatsapp_template_name=_query_text(whatsapp_template_name),
            whatsapp_template_language=_query_text(whatsapp_template_language),
            whatsapp_template_components=_json_object_list(
                _query_text(whatsapp_template_components)
            ),
            cc_addresses=team_inbox_commands.split_email_recipients(_query_text(cc)),
            bcc_addresses=team_inbox_commands.split_email_recipients(_query_text(bcc)),
            uploads=tuple(uploads),
        )
    except (
        team_inbox_commands.InboxCommandError,
        team_inbox_media.MediaUploadError,
        team_inbox_operations.InboxOperationError,
        ValueError,
    ) as exc:
        return RedirectResponse(
            url=f"/admin/inbox?status=error&message={quote_plus(str(exc))}",
            status_code=303,
        )
    message = f"Conversation started from {outcome.sender}."
    if outcome.contact_status not in {"linked_subscriber", "explicit_subscriber"}:
        # Say so rather than leave an anonymous thread looking resolved.
        message += " Contact is unmatched — link it from the contact panel."
    return _detail_redirect(outcome.conversation_id, status="success", message=message)


@router.post(
    "/{conversation_id}/transcript",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def team_inbox_email_transcript(
    conversation_id: UUID,
    request: Request,
    recipient: str = Form(...),
    db: Session = Depends(get_db),
):
    """Email a transcript of this conversation.

    Exporting a conversation is audited by the command owner; this only
    translates the transport principal into the audit actor vocabulary, the
    same way the ticket handoff route does.
    """
    _prepare_mutation(db)
    auth = getattr(request.state, "auth", None) or {}
    try:
        sent_to = team_inbox_commands.email_transcript(
            db,
            conversation_id=conversation_id,
            recipient=recipient,
            actor_person_id=_actor_id_from_request(request),
            actor_type=_audit_actor_type(str(auth.get("principal_type") or "")),
            request_id=getattr(request.state, "request_id", None),
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
        conversation_id, status="success", message=f"Transcript sent to {sent_to}."
    )
