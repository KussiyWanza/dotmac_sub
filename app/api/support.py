from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.schemas.common import ListResponse
from app.schemas.dispatch import WorkOrderHeaderRead
from app.schemas.support import (
    TicketBulkUpdateRequest,
    TicketCommentCreate,
    TicketCommentRead,
    TicketCommentUpdate,
    TicketCreate,
    TicketLinkCreate,
    TicketMergeRequest,
    TicketRead,
    TicketSlaEventCreate,
    TicketSlaEventRead,
    TicketSlaEventUpdate,
    TicketUpdate,
    TicketWorkOrderIssueRequest,
)
from app.schemas.team_inbox import (
    InboxConversationContactLinkRead,
    InboxConversationContactLinkRequest,
    InboxConversationEscalateRequest,
    InboxConversationEscalationRead,
    InboxConversationListItemRead,
    InboxConversationReplyRead,
    InboxConversationReplyRequest,
    InboxConversationTimelineRead,
)
from app.services import (
    support as support_service,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_contact_links,
    team_inbox_filters,
    team_inbox_outbound,
    team_inbox_read,
    ticket_validation,
    ticket_work_order_handoff,
)
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/support", tags=["support"])


def _actor_id(auth: dict) -> str | None:
    principal = auth.get("principal_id")
    return str(principal) if principal else None


def require_agent_or_admin(auth=Depends(require_user_auth)):
    roles = {str(role).lower() for role in (auth.get("roles") or [])}
    if "admin" in roles or "agent" in roles or "support" in roles:
        return auth
    raise HTTPException(status_code=403, detail="Forbidden")


@router.post(
    "/tickets",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:create"))],
)
def create_ticket(
    payload: TicketCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.create(
        db, payload, actor_id=_actor_id(auth), request=None
    )


@router.get(
    "/tickets",
    response_model=ListResponse[TicketRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_tickets(
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    assigned_to_person_id: str | None = Query(default=None),
    created_by_person_id: str | None = Query(default=None),
    project_manager_person_id: str | None = Query(default=None),
    site_coordinator_person_id: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    filters: str | None = Query(
        default=None,
        description=(
            "Advanced JSON filter: rows [doctype, field, operator, value] "
            'combined as {"and": [rows], "or": [rows]} or a list of rows '
            'with optional inline {"or": [rows]} groups.'
        ),
    ),
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return support_service.tickets.list_response(
        db,
        search=search,
        status=status,
        ticket_type=ticket_type,
        priority=priority,
        channel=channel,
        assigned_to_person_id=assigned_to_person_id,
        created_by_person_id=created_by_person_id,
        project_manager_person_id=project_manager_person_id,
        site_coordinator_person_id=site_coordinator_person_id,
        subscriber_id=subscriber_id,
        is_active=is_active,
        filters=filters,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/tickets/duplicates",
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def ticket_duplicate_lookup(
    title: str | None = Query(default=None),
    description: str | None = Query(default=None),
    exclude_ticket_id: str | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
    customer_account_id: str | None = Query(default=None),
    customer_person_id: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    base_station_details: str | None = Query(default=None),
    tags: str | None = Query(default=None),
    region: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Live duplicate-candidate lookup backing the admin create-form warning."""
    result = ticket_validation.find_duplicate_ticket_candidates(
        db,
        ticket_validation.TicketDuplicateInput(
            title=title,
            description=description,
            exclude_ticket_id=exclude_ticket_id,
            subscriber_id=subscriber_id,
            customer_account_id=customer_account_id,
            customer_person_id=customer_person_id,
            lead_id=lead_id,
            ticket_type=ticket_type,
            base_station_details=base_station_details,
            tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
            region=region,
        ),
    )
    return result.as_dict()


@router.get(
    "/tickets/lookup/{ticket_lookup}",
    response_model=TicketRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_ticket(ticket_lookup: str, db: Session = Depends(get_db)):
    return support_service.tickets.get_by_lookup(db, ticket_lookup)


@router.get(
    "/tickets/{ticket_lookup}",
    response_model=TicketRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_ticket_legacy_path(ticket_lookup: str, db: Session = Depends(get_db)):
    return support_service.tickets.get_by_lookup(db, ticket_lookup)


@router.get(
    "/tickets/{ticket_id}/work-orders",
    response_model=ListResponse[WorkOrderHeaderRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_ticket_work_orders(
    ticket_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    support_service.tickets.get(db, str(ticket_id))
    rows = ticket_work_order_handoff.list_for_ticket(
        db, ticket_id, limit=limit, offset=offset
    )
    return {"items": rows, "count": len(rows), "limit": limit, "offset": offset}


@router.post(
    "/tickets/{ticket_id}/work-orders",
    response_model=WorkOrderHeaderRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def issue_ticket_work_order(
    ticket_id: UUID,
    payload: TicketWorkOrderIssueRequest,
    auth: dict = Depends(require_permission("operations:dispatch:write")),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
    db: Session = Depends(get_db),
):
    actor_id = coerce_uuid(_actor_id(auth))
    if actor_id is None:
        raise HTTPException(status_code=403, detail="Authenticated actor required")
    db_session_adapter.release_read_transaction(db)
    return ticket_work_order_handoff.issue_work_order(
        db,
        ticket_work_order_handoff.TicketWorkOrderIssueCommand(
            ticket_id=ticket_id,
            request=payload,
            actor_id=actor_id,
            actor_type=ticket_work_order_handoff.HandoffActorType(
                str(auth.get("principal_type") or "system_user")
            ),
            permissions=frozenset(
                {"support:ticket:update", "operations:dispatch:write"}
            ),
            context=CommandContext.system(
                actor=str(actor_id),
                scope=ticket_work_order_handoff.WORK_ORDER_ISSUE_SCOPE,
                reason=payload.reason,
                idempotency_key=idempotency_key,
            ),
            request_id=request_id,
        ),
    ).work_order


@router.patch(
    "/tickets/{ticket_id}",
    response_model=TicketRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def update_ticket(
    ticket_id: UUID,
    payload: TicketUpdate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.update(
        db, str(ticket_id), payload, actor_id=_actor_id(auth), request=None
    )


@router.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:delete"))],
)
def soft_delete_ticket(
    ticket_id: UUID, auth=Depends(require_user_auth), db: Session = Depends(get_db)
):
    db_session_adapter.release_read_transaction(db)
    support_service.tickets.soft_delete(
        db, str(ticket_id), actor_id=_actor_id(auth), request=None
    )


@router.post(
    "/tickets/bulk-update",
    response_model=ListResponse[TicketRead],
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def bulk_update_tickets(
    payload: TicketBulkUpdateRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    items = support_service.tickets.bulk_update(
        db, payload, actor_id=_actor_id(auth), request=None
    )
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.post(
    "/tickets/{ticket_id}/auto-assign",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def manual_auto_assign(
    ticket_id: UUID, auth=Depends(require_user_auth), db: Session = Depends(get_db)
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.manual_auto_assign(
        db, str(ticket_id), actor_id=_actor_id(auth), request=None
    )


@router.post(
    "/inbox/conversations/{conversation_id}/escalate",
    response_model=InboxConversationEscalationRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def escalate_inbox_conversation(
    conversation_id: UUID,
    payload: InboxConversationEscalateRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id(auth)
    finish_read_transaction(db)
    result = team_inbox_assignment.escalate_conversation_committed(
        db,
        conversation_id=conversation_id,
        service_team_id=payload.service_team_id,
        assigned_person_id=payload.assigned_person_id,
        auto_assign=payload.auto_assign,
        assigned_by_person_id=actor_id,
        reason=payload.reason,
    )
    if result.kind == "conversation_not_found":
        raise HTTPException(status_code=404, detail=result.reason)
    if result.kind == "conversation_resolved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.reason,
        )
    if result.kind in {"invalid_team", "invalid_agent"}:
        raise HTTPException(status_code=400, detail=result.reason)

    return InboxConversationEscalationRead(
        conversation_id=conversation_id,
        kind=result.kind,
        service_team_id=UUID(result.service_team_id)
        if result.service_team_id
        else None,
        assigned_person_id=(
            UUID(result.assigned_person_id) if result.assigned_person_id else None
        ),
        reason=result.reason,
    )


@router.get(
    "/inbox/conversations",
    response_model=ListResponse[InboxConversationListItemRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_inbox_conversations(
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    channel_type: str | None = Query(default=None),
    service_team_id: str | None = Query(default=None),
    filters: str | None = Query(
        default=None,
        description=(
            "Advanced Service Team JSON filter using InboxConversation rows and "
            "the =, !=, in, not in, is, or is not operators."
        ),
    ),
    assigned_person_id: str | None = Query(default=None),
    needs_response: bool = Query(default=False),
    contact_resolution_status: str | None = Query(default=None),
    priority_at_most: int | None = Query(default=None, ge=0, le=999),
    muted: bool | None = Query(default=None),
    snoozed: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    clean_contact_resolution_status = (
        contact_resolution_status.strip()
        if isinstance(contact_resolution_status, str)
        and contact_resolution_status.strip()
        else None
    )
    clean_priority_at_most = (
        priority_at_most if isinstance(priority_at_most, int) else None
    )
    clean_muted = muted if isinstance(muted, bool) else None
    clean_snoozed = snoozed if isinstance(snoozed, bool) else None
    try:
        advanced_filters, _team_options = team_inbox_filters.resolve_filter_query(
            db,
            team_inbox_filters.InboxAdvancedFilterPayload(
                raw_json=filters if isinstance(filters, str) else None
            ),
        )
    except team_inbox_filters.InboxFilterError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    result = team_inbox_read.list_conversations(
        db,
        search=search,
        status=status,
        channel_type=channel_type,
        service_team_id=service_team_id,
        advanced_filters=advanced_filters,
        assigned_person_id=assigned_person_id,
        needs_response=needs_response,
        contact_resolution_status=clean_contact_resolution_status,
        priority_at_most=clean_priority_at_most,
        muted=clean_muted,
        snoozed=clean_snoozed,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [
            InboxConversationListItemRead.model_validate(row, from_attributes=True)
            for row in result.items
        ],
        "count": result.count,
        "limit": result.limit,
        "offset": result.offset,
    }


@router.get(
    "/inbox/conversations/{conversation_id}",
    response_model=InboxConversationTimelineRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_inbox_conversation_timeline(
    conversation_id: UUID,
    db: Session = Depends(get_db),
):
    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return InboxConversationTimelineRead.model_validate(timeline, from_attributes=True)


@router.post(
    "/inbox/conversations/{conversation_id}/reply",
    response_model=InboxConversationReplyRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def reply_to_inbox_conversation(
    conversation_id: UUID,
    payload: InboxConversationReplyRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    finish_read_transaction(db)
    result = team_inbox_outbound.send_inbox_reply_for_conversation_committed(
        db,
        conversation_id=conversation_id,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html=payload.body_html,
            body_text=payload.body_text,
            subject=payload.subject,
            to_email=payload.to_email,
            sent_by_person_id=_actor_id(auth),
        ),
    )
    if result.kind == "conversation_not_found":
        raise HTTPException(status_code=404, detail=result.reason)
    if result.kind == "invalid_conversation":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.reason,
        )
    if result.kind in {"missing_recipient", "empty_body"}:
        raise HTTPException(status_code=400, detail=result.reason)
    if result.kind == "send_failed":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.reason,
        )
    if result.kind == "suppressed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.reason,
        )

    return InboxConversationReplyRead(
        conversation_id=UUID(result.conversation_id),
        kind=result.kind,
        message_id=UUID(result.message_id) if result.message_id else None,
        service_team_id=UUID(result.service_team_id)
        if result.service_team_id
        else None,
        sender_key=result.sender_key,
        activity=result.activity,
        from_address=result.from_address,
        to_email=result.to_email,
        reason=result.reason,
    )


@router.post(
    "/inbox/conversations/{conversation_id}/contact-link",
    response_model=InboxConversationContactLinkRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def link_inbox_conversation_contact(
    conversation_id: UUID,
    payload: InboxConversationContactLinkRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    finish_read_transaction(db)
    try:
        result = team_inbox_contact_links.link_conversation_contact_by_id_committed(
            db,
            conversation_id=conversation_id,
            subscriber_id=payload.subscriber_id,
            reseller_id=payload.reseller_id,
            linked_by_person_id=_actor_id(auth),
            note=payload.note,
        )
    except team_inbox_contact_links.ConversationContactLinkError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except team_inbox_contact_links.ContactLinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return InboxConversationContactLinkRead(
        conversation_id=conversation_id,
        contact_link_id=result.contact_link_id,
        channel_type=result.channel_type,
        normalized_contact=result.normalized_contact,
        subscriber_id=result.subscriber_id,
        reseller_id=result.reseller_id,
        previous_link_ids_deactivated=result.previous_link_ids_deactivated,
    )


@router.post(
    "/tickets/{ticket_id}/links",
    dependencies=[
        Depends(require_permission("support:ticket:update")),
        Depends(require_agent_or_admin),
    ],
)
def create_ticket_link(
    ticket_id: UUID,
    payload: TicketLinkCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.link_ticket(
        db,
        from_ticket_id=str(ticket_id),
        to_ticket_id=str(payload.to_ticket_id),
        link_type=payload.link_type,
        actor_id=_actor_id(auth),
        request=None,
    )


@router.post(
    "/tickets/{ticket_id}/merge",
    response_model=TicketRead,
    dependencies=[
        Depends(require_permission("support:ticket:update")),
        Depends(require_agent_or_admin),
    ],
)
def merge_ticket(
    ticket_id: UUID,
    payload: TicketMergeRequest,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.merge(
        db,
        str(ticket_id),
        payload,
        actor_id=_actor_id(auth),
        request=None,
    )


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=TicketCommentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def create_comment(
    ticket_id: UUID,
    payload: TicketCommentCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.create_comment(
        db,
        str(ticket_id),
        payload,
        actor_id=_actor_id(auth),
        request=None,
    )


@router.post(
    "/tickets/{ticket_id}/comments/bulk",
    response_model=ListResponse[TicketCommentRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def bulk_create_comments(
    ticket_id: UUID,
    payload: list[TicketCommentCreate],
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    db_session_adapter.release_read_transaction(db)
    items = support_service.tickets.bulk_create_comments(
        db,
        str(ticket_id),
        payload,
        actor_id=_actor_id(auth),
        request=None,
    )
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.get(
    "/tickets/{ticket_id}/comments",
    response_model=ListResponse[TicketCommentRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_comments(
    ticket_id: UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = support_service.ticket_comments.list(
        db, str(ticket_id), limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get(
    "/tickets/{ticket_id}/comments/{comment_id}",
    response_model=TicketCommentRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_comment(ticket_id: UUID, comment_id: UUID, db: Session = Depends(get_db)):
    comment = support_service.ticket_comments.get(db, str(comment_id))
    if str(comment.ticket_id) != str(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket comment not found")
    return comment


@router.patch(
    "/tickets/{ticket_id}/comments/{comment_id}",
    response_model=TicketCommentRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def update_comment(
    ticket_id: UUID,
    comment_id: UUID,
    payload: TicketCommentUpdate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    comment = support_service.ticket_comments.get(db, str(comment_id))
    if str(comment.ticket_id) != str(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket comment not found")
    db_session_adapter.release_read_transaction(db)
    return support_service.ticket_comments.update(
        db, comment=comment, payload=payload, actor_id=_actor_id(auth), request=None
    )


@router.delete(
    "/tickets/{ticket_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def delete_comment(
    ticket_id: UUID,
    comment_id: UUID,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    comment = support_service.ticket_comments.get(db, str(comment_id))
    if str(comment.ticket_id) != str(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket comment not found")
    db_session_adapter.release_read_transaction(db)
    support_service.ticket_comments.delete(
        db, comment=comment, actor_id=_actor_id(auth), request=None
    )


@router.post(
    "/sla-events",
    response_model=TicketSlaEventRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def create_sla_event(payload: TicketSlaEventCreate, db: Session = Depends(get_db)):
    db_session_adapter.release_read_transaction(db)
    return support_service.ticket_sla_events.create(db, payload)


@router.get(
    "/sla-events/{event_id}",
    response_model=TicketSlaEventRead,
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def get_sla_event(event_id: UUID, db: Session = Depends(get_db)):
    return support_service.ticket_sla_events.get(db, str(event_id))


@router.get(
    "/sla-events",
    response_model=ListResponse[TicketSlaEventRead],
    dependencies=[Depends(require_permission("support:ticket:read"))],
)
def list_sla_events(
    ticket_id: UUID,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = support_service.ticket_sla_events.list(
        db, str(ticket_id), limit=limit, offset=offset
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.patch(
    "/sla-events/{event_id}",
    response_model=TicketSlaEventRead,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def update_sla_event(
    event_id: UUID, payload: TicketSlaEventUpdate, db: Session = Depends(get_db)
):
    event = support_service.ticket_sla_events.get(db, str(event_id))
    db_session_adapter.release_read_transaction(db)
    return support_service.ticket_sla_events.update(db, event, payload)


@router.delete(
    "/sla-events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def delete_sla_event(event_id: UUID, db: Session = Depends(get_db)):
    event = support_service.ticket_sla_events.get(db, str(event_id))
    db_session_adapter.release_read_transaction(db)
    support_service.ticket_sla_events.delete(db, event)
