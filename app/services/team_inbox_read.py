from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxComment,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationLabel,
    InboxConversationTeam,
    InboxLabel,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_media, team_inbox_read_state


@dataclass(frozen=True)
class InboxTimelineTeam:
    service_team_id: str
    service_team_name: str | None
    service_team_type: str | None
    role: str
    source: str
    is_active: bool


@dataclass(frozen=True)
class InboxTimelineAssignment:
    person_id: str
    service_team_id: str
    service_team_name: str | None
    assigned_by_person_id: str | None
    assigned_at: datetime
    is_active: bool


@dataclass(frozen=True)
class InboxTimelineMessage:
    id: str
    channel_type: str
    direction: str
    subject: str | None
    body: str | None
    from_address: str | None
    to_addresses: list
    cc_addresses: list
    sent_at: datetime | None
    received_at: datetime | None
    created_at: datetime
    metadata: dict | None
    attachments: list[dict]


@dataclass(frozen=True)
class InboxTimelineComment:
    id: str
    message_id: str | None
    author_person_id: str | None
    body: str
    visibility: str
    is_resolved: bool
    resolved_by_person_id: str | None
    resolved_at: datetime | None
    created_at: datetime
    metadata: dict | None


@dataclass(frozen=True)
class InboxConversationTimeline:
    id: str
    subscriber_id: str | None
    primary_service_team_id: str | None
    channel_type: str
    status: str
    priority: int
    is_muted: bool
    snoozed_until: datetime | None
    subject: str | None
    contact_address: str | None
    external_thread_id: str | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime
    metadata: dict | None
    teams: list[InboxTimelineTeam]
    assignments: list[InboxTimelineAssignment]
    messages: list[InboxTimelineMessage]
    comments: list[InboxTimelineComment]


@dataclass(frozen=True)
class InboxConversationListRow:
    id: str
    subscriber_id: str | None
    primary_service_team_id: str | None
    primary_service_team_name: str | None
    primary_service_team_type: str | None
    channel_type: str
    status: str
    priority: int
    is_muted: bool
    snoozed_until: datetime | None
    is_snoozed: bool
    subject: str | None
    contact_address: str | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    latest_message_direction: str | None
    latest_message_body: str | None
    latest_message_at: datetime | None
    contact_resolution_status: str | None
    latest_delivery_status: str | None
    latest_delivery_error: str | None
    active_assigned_person_id: str | None
    needs_response: bool
    is_unread: bool
    team_count: int
    labels: tuple[InboxConversationListLabel, ...]


@dataclass(frozen=True)
class InboxConversationListLabel:
    id: str
    name: str
    color: str | None


@dataclass(frozen=True)
class InboxConversationListResult:
    items: list[InboxConversationListRow]
    count: int
    limit: int
    offset: int


def _conversation_id(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _optional_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or str(value).strip() == "":
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def _message_time(message: InboxMessage) -> datetime:
    return message.received_at or message.sent_at or message.created_at


def _latest_messages_by_conversation(
    db: Session,
    conversation_ids: list[UUID],
) -> dict[UUID, InboxMessage]:
    if not conversation_ids:
        return {}
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id.in_(conversation_ids))
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    latest: dict[UUID, InboxMessage] = {}
    for message in messages:
        if message.direction == InboxMessageDirection.internal.value:
            continue
        current = latest.get(message.conversation_id)
        if current is None or _message_time(message) >= _message_time(current):
            latest[message.conversation_id] = message
    return latest


def _contact_resolution_status(conversation: InboxConversation) -> str | None:
    metadata = conversation.metadata_ or {}
    resolution = metadata.get("contact_resolution")
    if isinstance(resolution, dict):
        value = str(resolution.get("status") or "").strip()
        return value or None
    return None


def _delivery_status(message: InboxMessage | None) -> str | None:
    if message is None:
        return None
    metadata = message.metadata_ or {}
    value = str(metadata.get("delivery_status") or "").strip()
    if value:
        return value
    provider_result = metadata.get("provider_result")
    if isinstance(provider_result, dict):
        status_code = provider_result.get("status_code")
        if isinstance(status_code, int) and status_code >= 400:
            return "failed"
    if metadata.get("send_error"):
        return "failed"
    return None


def _delivery_error(message: InboxMessage | None) -> str | None:
    if message is None:
        return None
    metadata = message.metadata_ or {}
    value = str(
        metadata.get("send_error")
        or metadata.get("failure_reason")
        or metadata.get("last_error")
        or ""
    ).strip()
    return value or None


def _message_attachments(message: InboxMessage) -> list[dict]:
    metadata = message.metadata_ or {}
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, dict)]


def _asset_attachment(asset: InboxMediaAsset) -> dict:
    return {
        "id": str(asset.id),
        "type": asset.asset_type,
        "filename": asset.file_name,
        "file_name": asset.file_name,
        "mime_type": asset.mime_type,
        "file_size": asset.file_size,
        "caption": asset.caption,
        "url": asset.storage_url or asset.source_url,
        "source_url": asset.source_url,
        "storage_url": asset.storage_url,
        "provider": asset.provider,
        "provider_media_id": asset.provider_media_id,
        "download_status": asset.download_status,
        "download_error": asset.download_error,
        "metadata": asset.metadata_,
    }


def list_conversations(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    channel_type: str | None = None,
    subscriber_id: str | UUID | None = None,
    service_team_id: str | UUID | None = None,
    service_team_ids: Sequence[str | UUID] | None = None,
    assigned_person_id: str | UUID | None = None,
    needs_response: bool = False,
    contact_resolution_status: str | None = None,
    priority_at_most: int | None = None,
    muted: bool | None = None,
    snoozed: bool | None = None,
    open_only: bool = False,
    unassigned: bool = False,
    operator_person_id: UUID | None = None,
    unread_only: bool = False,
    ai_handling: bool | None = None,
    has_ticket: bool | None = None,
    activity_from: datetime | None = None,
    activity_to: datetime | None = None,
    order_by: str | None = None,
    order_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> InboxConversationListResult:
    query = (
        db.query(InboxConversation, ServiceTeam)
        .outerjoin(
            ServiceTeam, ServiceTeam.id == InboxConversation.primary_service_team_id
        )
        .filter(InboxConversation.is_active.is_(True))
    )
    clean_search = (search or "").strip()
    if clean_search:
        like = f"%{clean_search}%"
        matching_message_conversation_ids = [
            row[0]
            for row in db.query(InboxMessage.conversation_id)
            .filter(
                or_(
                    InboxMessage.subject.ilike(like),
                    InboxMessage.body.ilike(like),
                    InboxMessage.from_address.ilike(like),
                )
            )
            .distinct()
            .limit(500)
            .all()
        ]
        matching_comment_conversation_ids = [
            row[0]
            for row in db.query(InboxComment.conversation_id)
            .filter(InboxComment.body.ilike(like))
            .distinct()
            .limit(500)
            .all()
        ]
        query = query.filter(
            or_(
                InboxConversation.subject.ilike(like),
                InboxConversation.contact_address.ilike(like),
                InboxConversation.external_thread_id.ilike(like),
                InboxConversation.id.in_(
                    matching_message_conversation_ids
                    + matching_comment_conversation_ids
                ),
            )
        )
    if status:
        query = query.filter(InboxConversation.status == status)
    if open_only:
        query = query.filter(InboxConversation.status != "resolved")
    if channel_type:
        query = query.filter(InboxConversation.channel_type == channel_type)
    if priority_at_most is not None:
        query = query.filter(InboxConversation.priority <= int(priority_at_most))
    if muted is not None:
        query = query.filter(InboxConversation.is_muted.is_(bool(muted)))
    if snoozed is not None:
        if snoozed:
            query = query.filter(InboxConversation.snoozed_until.isnot(None))
        else:
            query = query.filter(InboxConversation.snoozed_until.is_(None))

    # Customer-scoped read: the conversation carries the resolved subscriber, so
    # the customer record can project its own communications without joining
    # through the contact link.
    subscriber_uuid = _optional_uuid(subscriber_id)
    if subscriber_uuid is not None:
        query = query.filter(InboxConversation.subscriber_id == subscriber_uuid)

    # Multi-team scope for "my team": an agent may belong to several teams and
    # the my_team count already spans all of them, so the filter must too or the
    # badge and the list disagree.
    team_uuids = [
        value
        for value in (_optional_uuid(item) for item in (service_team_ids or ()))
        if value is not None
    ]
    if team_uuids:
        query = query.join(
            InboxConversationTeam,
            InboxConversationTeam.conversation_id == InboxConversation.id,
        ).filter(
            InboxConversationTeam.service_team_id.in_(team_uuids),
            InboxConversationTeam.is_active.is_(True),
        )

    # Conversations an AI agent is handling, so a human can either stay out of
    # the way or take over deliberately.
    if ai_handling is not None:
        flag = InboxConversation.metadata_["ai_handling"].as_boolean()
        query = query.filter(flag.is_(True) if ai_handling else flag.isnot(True))

    # Whether a ticket was ever issued from the thread. The provenance link is
    # owned by communications.conversation_ticket_handoff.
    if has_ticket is not None:
        from app.models.support import Ticket

        issued = (
            select(Ticket.origin_conversation_id)
            .where(Ticket.origin_conversation_id.isnot(None))
            .where(Ticket.is_active.is_(True))
        )
        query = (
            query.filter(InboxConversation.id.in_(issued))
            if has_ticket
            else query.filter(~InboxConversation.id.in_(issued))
        )

    # Activity window, on last_message_at so the range means "was this thread
    # live in that period" rather than when it happened to be created.
    if activity_from is not None:
        query = query.filter(InboxConversation.last_message_at >= activity_from)
    if activity_to is not None:
        query = query.filter(InboxConversation.last_message_at <= activity_to)

    team_uuid = _optional_uuid(service_team_id)
    if team_uuid is not None:
        query = query.join(
            InboxConversationTeam,
            InboxConversationTeam.conversation_id == InboxConversation.id,
        ).filter(
            InboxConversationTeam.service_team_id == team_uuid,
            InboxConversationTeam.is_active.is_(True),
        )

    assignee_uuid = _optional_uuid(assigned_person_id)
    if assignee_uuid is not None:
        query = query.join(
            InboxConversationAssignment,
            InboxConversationAssignment.conversation_id == InboxConversation.id,
        ).filter(
            InboxConversationAssignment.person_id == assignee_uuid,
            InboxConversationAssignment.is_active.is_(True),
        )
    if unassigned:
        assigned_conversation_ids = select(
            InboxConversationAssignment.conversation_id
        ).where(InboxConversationAssignment.is_active.is_(True))
        query = query.filter(~InboxConversation.id.in_(assigned_conversation_ids))

    # order_by=None (default) or "priority" keeps the urgency composite so the
    # default queue is untouched; last_message_at / created_at sort by that one
    # column with a stable id tie-breaker. Additive change.
    if order_by == "last_message_at":
        last_message_order = (
            InboxConversation.last_message_at.asc()
            if order_dir == "asc"
            else InboxConversation.last_message_at.desc()
        ).nullslast()
        ordered_query = query.order_by(last_message_order, InboxConversation.id.asc())
    elif order_by == "created_at":
        created_order = (
            InboxConversation.created_at.asc()
            if order_dir == "asc"
            else InboxConversation.created_at.desc()
        )
        ordered_query = query.order_by(created_order, InboxConversation.id.asc())
    else:
        priority_order = (
            InboxConversation.priority.desc()
            if order_by == "priority" and order_dir == "desc"
            else InboxConversation.priority.asc()
        )
        ordered_query = query.order_by(
            priority_order,
            InboxConversation.last_message_at.desc().nullslast(),
            InboxConversation.created_at.desc(),
            InboxConversation.id.asc(),
        )
    total = query.count()
    needs_python_filter = bool(
        needs_response or contact_resolution_status or unread_only
    )
    rows = (
        ordered_query.all()
        if needs_python_filter
        else ordered_query.limit(limit).offset(offset).all()
    )
    conversations = [conversation for conversation, _team in rows]
    conversation_ids = [conversation.id for conversation in conversations]
    latest_messages = _latest_messages_by_conversation(db, conversation_ids)
    active_assignments = (
        {
            assignment.conversation_id: assignment
            for assignment in db.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id.in_(conversation_ids))
            .filter(InboxConversationAssignment.is_active.is_(True))
            .all()
        }
        if conversation_ids
        else {}
    )
    team_counts = (
        {
            conversation_id: count
            for conversation_id, count in db.query(
                InboxConversationTeam.conversation_id,
                func.count(InboxConversationTeam.id),
            )
            .filter(InboxConversationTeam.conversation_id.in_(conversation_ids))
            .filter(InboxConversationTeam.is_active.is_(True))
            .group_by(InboxConversationTeam.conversation_id)
            .all()
        }
        if conversation_ids
        else {}
    )
    labels_by_conversation: dict[UUID, list[InboxConversationListLabel]] = {}
    if conversation_ids:
        label_rows = (
            db.query(InboxConversationLabel, InboxLabel)
            .join(InboxLabel, InboxLabel.id == InboxConversationLabel.label_id)
            .filter(InboxConversationLabel.conversation_id.in_(conversation_ids))
            .filter(InboxConversationLabel.is_active.is_(True))
            .filter(InboxLabel.is_active.is_(True))
            .order_by(InboxConversationLabel.created_at.asc())
            .all()
        )
        for link, label in label_rows:
            labels_by_conversation.setdefault(link.conversation_id, []).append(
                InboxConversationListLabel(
                    id=str(label.id),
                    name=label.name,
                    color=label.color,
                )
            )

    items: list[InboxConversationListRow] = []
    for conversation, team in rows:
        latest = latest_messages.get(conversation.id)
        active_assignment = active_assignments.get(conversation.id)
        resolution_status = _contact_resolution_status(conversation)
        row_needs_response = (
            latest is not None
            and latest.direction == InboxMessageDirection.inbound.value
            and conversation.status != "resolved"
        )
        if needs_response and not row_needs_response:
            continue
        if contact_resolution_status and resolution_status != contact_resolution_status:
            continue
        row_is_unread = (
            team_inbox_read_state.conversation_is_unread(
                db,
                conversation_id=conversation.id,
                person_id=operator_person_id,
            )
            if operator_person_id is not None
            else False
        )
        if unread_only and not row_is_unread:
            continue
        items.append(
            InboxConversationListRow(
                id=str(conversation.id),
                subscriber_id=str(conversation.subscriber_id)
                if conversation.subscriber_id is not None
                else None,
                primary_service_team_id=str(conversation.primary_service_team_id)
                if conversation.primary_service_team_id is not None
                else None,
                primary_service_team_name=team.name if team is not None else None,
                primary_service_team_type=team.team_type if team is not None else None,
                channel_type=conversation.channel_type,
                status=conversation.status,
                priority=conversation.priority,
                is_muted=conversation.is_muted,
                snoozed_until=conversation.snoozed_until,
                is_snoozed=conversation.snoozed_until is not None,
                subject=conversation.subject,
                contact_address=conversation.contact_address,
                first_message_at=conversation.first_message_at,
                last_message_at=conversation.last_message_at,
                latest_message_direction=latest.direction
                if latest is not None
                else None,
                latest_message_body=latest.body if latest is not None else None,
                latest_message_at=_message_time(latest) if latest is not None else None,
                contact_resolution_status=resolution_status,
                latest_delivery_status=_delivery_status(latest),
                latest_delivery_error=_delivery_error(latest),
                active_assigned_person_id=str(active_assignment.person_id)
                if active_assignment is not None
                else None,
                needs_response=row_needs_response,
                is_unread=row_is_unread,
                team_count=int(team_counts.get(conversation.id, 0)),
                labels=tuple(labels_by_conversation.get(conversation.id, [])),
            )
        )
    filtered_count = len(items) if needs_python_filter else total
    page_items = items[offset : offset + limit] if needs_python_filter else items
    return InboxConversationListResult(
        items=page_items,
        count=filtered_count,
        limit=limit,
        offset=offset,
    )


def get_conversation_timeline(
    db: Session,
    conversation_id: str | UUID,
) -> InboxConversationTimeline | None:
    conversation = db.get(InboxConversation, _conversation_id(conversation_id))
    if conversation is None or not conversation.is_active:
        return None

    team_rows = (
        db.query(InboxConversationTeam, ServiceTeam)
        .outerjoin(ServiceTeam, ServiceTeam.id == InboxConversationTeam.service_team_id)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .order_by(
            InboxConversationTeam.role.asc(), InboxConversationTeam.created_at.asc()
        )
        .all()
    )
    assignment_rows = (
        db.query(InboxConversationAssignment, ServiceTeam)
        .outerjoin(
            ServiceTeam,
            ServiceTeam.id == InboxConversationAssignment.service_team_id,
        )
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .order_by(
            InboxConversationAssignment.is_active.desc(),
            InboxConversationAssignment.assigned_at.desc(),
        )
        .all()
    )
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .order_by(
            InboxMessage.created_at.asc(),
            InboxMessage.received_at.asc(),
            InboxMessage.sent_at.asc(),
        )
        .all()
    )
    assets_by_message = team_inbox_media.assets_for_messages(
        db,
        [message.id for message in messages],
    )
    comments = (
        db.query(InboxComment)
        .filter(InboxComment.conversation_id == conversation.id)
        .order_by(InboxComment.created_at.asc())
        .all()
    )

    return InboxConversationTimeline(
        id=str(conversation.id),
        subscriber_id=str(conversation.subscriber_id)
        if conversation.subscriber_id is not None
        else None,
        primary_service_team_id=str(conversation.primary_service_team_id)
        if conversation.primary_service_team_id is not None
        else None,
        channel_type=conversation.channel_type,
        status=conversation.status,
        priority=conversation.priority,
        is_muted=conversation.is_muted,
        snoozed_until=conversation.snoozed_until,
        subject=conversation.subject,
        contact_address=conversation.contact_address,
        external_thread_id=conversation.external_thread_id,
        first_message_at=conversation.first_message_at,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        metadata=conversation.metadata_,
        teams=[
            InboxTimelineTeam(
                service_team_id=str(link.service_team_id),
                service_team_name=team.name if team is not None else None,
                service_team_type=team.team_type if team is not None else None,
                role=link.role,
                source=link.source,
                is_active=link.is_active,
            )
            for link, team in team_rows
        ],
        assignments=[
            InboxTimelineAssignment(
                person_id=str(assignment.person_id),
                service_team_id=str(assignment.service_team_id),
                service_team_name=team.name if team is not None else None,
                assigned_by_person_id=str(assignment.assigned_by_person_id)
                if assignment.assigned_by_person_id is not None
                else None,
                assigned_at=assignment.assigned_at,
                is_active=assignment.is_active,
            )
            for assignment, team in assignment_rows
        ],
        messages=[
            InboxTimelineMessage(
                id=str(message.id),
                channel_type=message.channel_type,
                direction=message.direction,
                subject=message.subject,
                body=message.body,
                from_address=message.from_address,
                to_addresses=list(message.to_addresses or []),
                cc_addresses=list(message.cc_addresses or []),
                sent_at=message.sent_at,
                received_at=message.received_at,
                created_at=message.created_at,
                metadata=message.metadata_,
                attachments=(
                    [
                        _asset_attachment(asset)
                        for asset in assets_by_message.get(message.id, [])
                    ]
                    or _message_attachments(message)
                ),
            )
            for message in messages
        ],
        comments=[
            InboxTimelineComment(
                id=str(comment.id),
                message_id=str(comment.message_id) if comment.message_id else None,
                author_person_id=str(comment.author_person_id)
                if comment.author_person_id
                else None,
                body=comment.body,
                visibility=comment.visibility,
                is_resolved=comment.is_resolved,
                resolved_by_person_id=str(comment.resolved_by_person_id)
                if comment.resolved_by_person_id
                else None,
                resolved_at=comment.resolved_at,
                created_at=comment.created_at,
                metadata=comment.metadata_,
            )
            for comment in comments
        ],
    )
