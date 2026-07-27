from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxChannelType,
    InboxComment,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationLabel,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxLabel,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import (
    team_inbox_field_job,
    team_inbox_media,
    team_inbox_read_state,
)


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
    contact_name: str | None
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
    needs_attention: bool
    has_ticket: bool
    is_unread: bool
    unread_count: int
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


def _message_time_column():
    """SQL twin of :func:`_message_time`, so filters and rows agree on "latest"."""
    return func.coalesce(
        InboxMessage.received_at, InboxMessage.sent_at, InboxMessage.created_at
    )


def currently_snoozed_clause():
    """Asleep right now: a wake time still ahead, or waiting on a reply.

    ``team_inbox_operations.snooze_until_reply`` deliberately stores no wake
    time, so a snooze test that only looked at ``snoozed_until`` reported those
    conversations as awake while the workspace showed them snoozed.
    """
    from app.services.team_inbox_operations import SNOOZE_UNTIL_REPLY_KEY

    # A bound Python moment rather than `func.now()`: the wake time is stored
    # tz-aware and SQLite renders `now()` as a bare string, which would compare
    # lexically against a timestamp.
    return or_(
        and_(
            InboxConversation.snoozed_until.isnot(None),
            InboxConversation.snoozed_until > datetime.now(UTC),
        ),
        InboxConversation.metadata_[SNOOZE_UNTIL_REPLY_KEY].as_boolean().is_(True),
    )


def _is_currently_snoozed(conversation: InboxConversation) -> bool:
    """Python twin of :func:`currently_snoozed_clause`, for row projection."""
    from app.services.team_inbox_operations import SNOOZE_UNTIL_REPLY_KEY

    if (conversation.metadata_ or {}).get(SNOOZE_UNTIL_REPLY_KEY) is True:
        return True
    wake_at = conversation.snoozed_until
    if wake_at is None:
        return False
    return (wake_at if wake_at.tzinfo else wake_at.replace(tzinfo=UTC)) > datetime.now(
        UTC
    )


def _latest_visible_direction():
    """Direction of the newest non-internal message, correlated per conversation.

    Internal notes are excluded for the same reason the row projection excludes
    them: an agent's private note is not an answer to the customer.
    """
    return (
        select(InboxMessage.direction)
        .where(InboxMessage.conversation_id == InboxConversation.id)
        .where(InboxMessage.direction != InboxMessageDirection.internal.value)
        .order_by(
            _message_time_column().desc(),
            InboxMessage.created_at.desc(),
            InboxMessage.id.desc(),
        )
        .limit(1)
        .correlate(InboxConversation)
        .scalar_subquery()
    )


def _timestamp(value: datetime) -> float:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.timestamp()


def _message_order_key(message: InboxMessage) -> tuple[float, float, str]:
    return (
        _timestamp(_message_time(message)),
        _timestamp(message.created_at),
        str(message.id),
    )


def _messages_by_conversation(
    db: Session,
    conversation_ids: list[UUID],
) -> dict[UUID, tuple[InboxMessage, ...]]:
    if not conversation_ids:
        return {}
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id.in_(conversation_ids))
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    grouped: dict[UUID, list[InboxMessage]] = {}
    for message in messages:
        grouped.setdefault(message.conversation_id, []).append(message)
    return {
        conversation_id: tuple(sorted(items, key=_message_order_key))
        for conversation_id, items in grouped.items()
    }


def _latest_external_message(
    messages: Sequence[InboxMessage],
) -> InboxMessage | None:
    return next(
        (
            message
            for message in reversed(messages)
            if message.direction != InboxMessageDirection.internal.value
        ),
        None,
    )


class InboxResponseCohort(StrEnum):
    """Current customer-response state derived from authoritative message history."""

    none = "none"
    unreplied = "unreplied"
    needs_attention = "needs_attention"


_SUCCESSFUL_AGENT_DELIVERY_STATUSES = frozenset(
    {"queued", "accepted", "sent", "delivered", "read", "retried"}
)
_SOCIAL_COMMENT_CHANNELS = frozenset({"facebook_comment", "instagram_comment"})
_SOCIAL_COMMENT_METADATA_VALUES = frozenset(
    {"comment", "post_comment", "facebook_comment", "instagram_comment"}
)


def _metadata_flag_is_true(metadata: dict, key: str) -> bool:
    value = metadata.get(key)
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _metadata_flag_is_false(metadata: dict, key: str) -> bool:
    if key not in metadata:
        return False
    value = metadata.get(key)
    return value is False or str(value).strip().lower() in {"0", "false", "no"}


def _is_valid_agent_reply(message: InboxMessage) -> bool:
    if message.direction != InboxMessageDirection.outbound.value:
        return False
    metadata = message.metadata_ or {}
    if not str(metadata.get("sent_by_person_id") or "").strip():
        return False
    if str(metadata.get("delivery_status") or "").strip().lower() not in (
        _SUCCESSFUL_AGENT_DELIVERY_STATUSES
    ):
        return False
    if any(
        _metadata_flag_is_true(metadata, key)
        for key in ("ai_intake", "is_ai_intake", "automated_ai_intake")
    ):
        return False
    if any(
        _metadata_flag_is_false(metadata, key)
        for key in ("requires_response", "response_required", "requires_reply")
    ):
        return False
    return True


def _is_social_comment_conversation(
    conversation: InboxConversation,
    messages: Sequence[InboxMessage],
) -> bool:
    if conversation.channel_type in _SOCIAL_COMMENT_CHANNELS:
        return True
    metadata_values = [conversation.metadata_ or {}]
    metadata_values.extend(message.metadata_ or {} for message in messages)
    for metadata in metadata_values:
        if _metadata_flag_is_true(metadata, "is_comment"):
            return True
        if any(
            str(metadata.get(key) or "").strip().lower()
            in _SOCIAL_COMMENT_METADATA_VALUES
            for key in ("interaction_type", "message_type", "source_type", "surface")
        ):
            return True
    return False


def _ticketed_conversation_ids(
    db: Session,
    conversation_ids: Sequence[UUID],
) -> set[UUID]:
    if not conversation_ids:
        return set()
    from app.models.support import Ticket

    return {
        conversation_id
        for (conversation_id,) in db.query(Ticket.origin_conversation_id)
        .filter(Ticket.origin_conversation_id.in_(conversation_ids))
        .all()
        if conversation_id is not None
    }


def response_cohort(
    conversation: InboxConversation,
    messages: Sequence[InboxMessage],
    *,
    has_ticket_handoff: bool,
) -> InboxResponseCohort:
    """Classify an active thread without persisting a stale response flag."""

    external_messages = tuple(
        message
        for message in sorted(messages, key=_message_order_key)
        if message.direction != InboxMessageDirection.internal.value
    )
    inbound_indexes = [
        index
        for index, message in enumerate(external_messages)
        if message.direction == InboxMessageDirection.inbound.value
    ]
    if (
        not conversation.is_active
        or conversation.status == InboxConversationStatus.resolved.value
        or not inbound_indexes
    ):
        return InboxResponseCohort.none

    latest_inbound_index = inbound_indexes[-1]
    valid_reply_indexes = [
        index
        for index, message in enumerate(external_messages)
        if _is_valid_agent_reply(message)
    ]
    if any(index > latest_inbound_index for index in valid_reply_indexes):
        return InboxResponseCohort.none

    has_completed_exchange = any(
        reply_index < latest_inbound_index
        and any(inbound_index < reply_index for inbound_index in inbound_indexes)
        for reply_index in valid_reply_indexes
    )
    if not has_completed_exchange:
        return InboxResponseCohort.unreplied

    if (
        conversation.status == InboxConversationStatus.snoozed.value
        or conversation.snoozed_until is not None
        or has_ticket_handoff
        or _is_social_comment_conversation(conversation, external_messages)
    ):
        return InboxResponseCohort.none
    return InboxResponseCohort.needs_attention


def _contact_resolution_status(conversation: InboxConversation) -> str | None:
    metadata = conversation.metadata_ or {}
    resolution = metadata.get("contact_resolution")
    if isinstance(resolution, dict):
        value = str(resolution.get("status") or "").strip()
        return value or None
    return None


def _contact_display_name(conversation: InboxConversation) -> str | None:
    metadata = conversation.metadata_ or {}
    for key in ("contact_name", "sender_name", "profile_name"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value[:200]
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
    needs_attention: bool = False,
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
    if channel_type != InboxChannelType.field_job.value:
        # A job chat is held directly by the technician who is en route, so it
        # is not triage work and must not land in the queue. The one exception
        # is a visit that ended with the customer unanswered: nothing else is
        # watching a job chat, so that message would otherwise be lost.
        followup = InboxConversation.metadata_[
            team_inbox_field_job.QUEUE_FOLLOWUP_KEY
        ].as_boolean()
        query = query.filter(
            or_(
                InboxConversation.channel_type != InboxChannelType.field_job.value,
                followup.is_(True),
            )
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
        # "Snoozed" means asleep *now*, not "was snoozed at some point". The
        # bare NOT NULL test kept a conversation snoozed until Tuesday filed as
        # snoozed forever, and disagreed with the workqueue provider, which has
        # always read a passed wake time as awake. The scheduled waker settles
        # the durable status; this keeps the queue right in between its runs.
        query = query.filter(
            currently_snoozed_clause() if snoozed else ~currently_snoozed_clause()
        )

    # Customer-scoped read: the conversation carries the resolved subscriber, so
    # the customer record can project its own communications without joining
    # through the contact link.
    subscriber_uuid = _optional_uuid(subscriber_id)
    if subscriber_uuid is not None:
        query = query.filter(InboxConversation.subscriber_id == subscriber_uuid)

    # Multi-team scope for "my team": an agent may belong to several teams and
    # the my_team count already spans all of them, so the filter must too or the
    # badge and the list disagree.
    #
    # Membership is tested with a subquery, not a join. Joining the one-to-many
    # team link returned a conversation once per matching team, so a thread
    # shared by two of the operator's teams appeared twice and `count()`
    # double-counted it — the exact disagreement this scope exists to prevent.
    # A second join on the same relation (single-team filter set as well) also
    # made SQLAlchemy refuse the query outright.
    team_uuids = [
        value
        for value in (_optional_uuid(item) for item in (service_team_ids or ()))
        if value is not None
    ]
    if team_uuids:
        query = query.filter(
            InboxConversation.id.in_(
                select(InboxConversationTeam.conversation_id).where(
                    InboxConversationTeam.service_team_id.in_(team_uuids),
                    InboxConversationTeam.is_active.is_(True),
                )
            )
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

        issued = select(Ticket.origin_conversation_id).where(
            Ticket.origin_conversation_id.isnot(None)
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
        query = query.filter(
            InboxConversation.id.in_(
                select(InboxConversationTeam.conversation_id).where(
                    InboxConversationTeam.service_team_id == team_uuid,
                    InboxConversationTeam.is_active.is_(True),
                )
            )
        )

    assignee_uuid = _optional_uuid(assigned_person_id)
    if assignee_uuid is not None:
        query = query.filter(
            InboxConversation.id.in_(
                select(InboxConversationAssignment.conversation_id).where(
                    InboxConversationAssignment.person_id == assignee_uuid,
                    InboxConversationAssignment.is_active.is_(True),
                )
            )
        )
    if unassigned:
        assigned_conversation_ids = select(
            InboxConversationAssignment.conversation_id
        ).where(InboxConversationAssignment.is_active.is_(True))
        query = query.filter(~InboxConversation.id.in_(assigned_conversation_ids))

    # The next three used to be applied in Python, which meant the whole
    # filtered set was loaded, every message for it fetched, and one unread
    # query issued per conversation before a single page could be sliced.
    # `needs_response` is the default "Unreplied" cohort, so that was the
    # ordinary path. They are correlated subqueries now and the database keeps
    # both the filter and the pagination.
    if needs_response:
        query = query.filter(InboxConversation.status != "resolved").filter(
            _latest_visible_direction() == InboxMessageDirection.inbound.value
        )
    if contact_resolution_status:
        query = query.filter(
            InboxConversation.metadata_["contact_resolution"]["status"].as_string()
            == contact_resolution_status
        )
    if unread_only:
        # The unread rule stays with its owner; this only asks for it in SQL.
        # Without an operator nothing can be unread, and an empty page is the
        # honest answer rather than the whole queue.
        query = query.filter(
            team_inbox_read_state.unread_conversation_clause(operator_person_id)
            if operator_person_id is not None
            else false()
        )

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
    # `needs_response` and `contact_resolution_status` are already SQL filters
    # above, so listing them here too would load the whole filtered set before a
    # page could be sliced — and `needs_response` is the default "Unreplied"
    # cohort, so that is the ordinary path. Only `needs_attention` genuinely
    # needs Python: its rule reads ordering across the message sequence and
    # loose-typed metadata on every message, which has no faithful SQL twin.
    # The row-level checks below stay as a safety net; they are no-ops whenever
    # the SQL twin agrees.
    needs_python_filter = bool(needs_attention)
    rows = (
        ordered_query.all()
        if needs_python_filter
        else ordered_query.limit(limit).offset(offset).all()
    )
    conversations = [conversation for conversation, _team in rows]
    conversation_ids = [conversation.id for conversation in conversations]
    messages_by_conversation = _messages_by_conversation(db, conversation_ids)
    latest_messages = {
        conversation_id: latest
        for conversation_id, messages in messages_by_conversation.items()
        if (latest := _latest_external_message(messages)) is not None
    }
    ticketed_conversation_ids = _ticketed_conversation_ids(db, conversation_ids)
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

    unread_counts = (
        team_inbox_read_state.conversation_unread_message_counts(
            db,
            conversation_ids=conversation_ids,
            person_id=operator_person_id,
        )
        if conversation_ids and operator_person_id is not None
        else {}
    )

    items: list[InboxConversationListRow] = []
    for conversation, team in rows:
        latest = latest_messages.get(conversation.id)
        active_assignment = active_assignments.get(conversation.id)
        resolution_status = _contact_resolution_status(conversation)
        cohort = response_cohort(
            conversation,
            messages_by_conversation.get(conversation.id, ()),
            has_ticket_handoff=conversation.id in ticketed_conversation_ids,
        )
        row_needs_response = cohort == InboxResponseCohort.unreplied
        row_needs_attention = cohort == InboxResponseCohort.needs_attention
        if needs_response and not row_needs_response:
            continue
        if needs_attention and not row_needs_attention:
            continue
        if contact_resolution_status and resolution_status != contact_resolution_status:
            continue
        unread_count = unread_counts.get(conversation.id, 0)
        row_is_unread = unread_count > 0
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
                is_snoozed=_is_currently_snoozed(conversation),
                contact_name=_contact_display_name(conversation),
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
                needs_attention=row_needs_attention,
                has_ticket=conversation.id in ticketed_conversation_ids,
                is_unread=row_is_unread,
                unread_count=unread_count,
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
