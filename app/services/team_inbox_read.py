from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.party import Party
from app.models.service_team import ServiceTeam
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxChannelType,
    InboxComment,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationLabel,
    InboxConversationQueueEntry,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxLabel,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
    InboxQueueEntryStatus,
)
from app.services import (
    service_team_composition,
    team_inbox_assignment,
    team_inbox_field_job,
    team_inbox_filters,
    team_inbox_media,
    team_inbox_observations,
    team_inbox_read_state,
)


@dataclass(frozen=True)
class InboxTimelineTeam:
    service_team_id: str
    service_team_name: str | None
    service_team_capabilities: tuple[str, ...]
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


class InboxTimelineSenderSource(StrEnum):
    staff = "staff"
    fallback = "fallback"


@dataclass(frozen=True, slots=True)
class InboxTimelineSenderIdentity:
    system_user_id: str | None
    display_name: str
    initials: str
    source: InboxTimelineSenderSource


@dataclass(frozen=True)
class InboxTimelineLocation:
    latitude: float
    longitude: float
    name: str | None
    address: str | None
    map_url: str


@dataclass(frozen=True)
class InboxTimelineAttachment:
    id: str | None
    type: str
    filename: str | None
    file_name: str | None
    mime_type: str | None
    file_size: int | None
    caption: str | None
    url: str | None
    source_url: str | None
    storage_url: str | None
    provider: str | None
    provider_media_id: str | None
    download_status: str | None
    download_error: str | None
    content_available: bool
    metadata: dict[str, object] | None
    location: InboxTimelineLocation | None


@dataclass(frozen=True)
class InboxTimelineMessage:
    id: str
    channel_type: str
    direction: str
    subject: str | None
    body: str | None
    from_address: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    sent_at: datetime | None
    received_at: datetime | None
    created_at: datetime
    metadata: dict[str, object] | None
    attachments: list[InboxTimelineAttachment]
    sender: InboxTimelineSenderIdentity | None


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
    metadata: dict[str, object] | None


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
    contact_name: str
    contact_initials: str
    contact_name_source: str
    external_thread_id: str | None
    first_message_at: datetime | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, object] | None
    queue_position: int | None
    queued_at: datetime | None
    estimated_wait_minutes: int | None
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
    primary_service_team_capabilities: tuple[str, ...]
    channel_type: str
    status: str
    priority: int
    is_muted: bool
    snoozed_until: datetime | None
    is_snoozed: bool
    contact_name: str
    contact_initials: str
    contact_name_source: str
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
    queue_position: int | None
    queued_at: datetime | None
    estimated_wait_minutes: int | None
    needs_response: bool
    needs_attention: bool
    has_ticket: bool
    is_unread: bool
    unread_count: int
    team_count: int
    labels: tuple[InboxConversationListLabel, ...]
    reply_window_status: str = "not_applicable"


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


class InboxContactDisplaySource(StrEnum):
    party = "party"
    subscriber = "subscriber"
    provider = "provider"
    operator = "operator"
    address = "address"


@dataclass(frozen=True, slots=True)
class InboxContactDisplayIdentity:
    display_name: str
    initials: str
    source: InboxContactDisplaySource


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


def _base_queue_query(db: Session):
    query = db.query(InboxConversation).filter(InboxConversation.is_active.is_(True))
    followup = InboxConversation.metadata_[
        team_inbox_field_job.QUEUE_FOLLOWUP_KEY
    ].as_boolean()
    return query.filter(
        or_(
            InboxConversation.channel_type != InboxChannelType.field_job.value,
            followup.is_(True),
        )
    )


def queue_conversation_count(db: Session) -> int:
    return int(
        _base_queue_query(db).with_entities(func.count(InboxConversation.id)).scalar()
        or 0
    )


def assigned_conversation_count(
    db: Session,
    *,
    assigned_person_id: str | UUID | None,
) -> int:
    assignee_uuid = _optional_uuid(assigned_person_id)
    if assignee_uuid is None:
        return 0
    return int(
        _base_queue_query(db)
        .filter(
            InboxConversation.id.in_(
                select(InboxConversationAssignment.conversation_id).where(
                    InboxConversationAssignment.person_id == assignee_uuid,
                    InboxConversationAssignment.is_active.is_(True),
                )
            )
        )
        .with_entities(func.count(InboxConversation.id))
        .scalar()
        or 0
    )


def needs_response_conversation_count(db: Session) -> int:
    return int(
        _base_queue_query(db)
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .filter(_latest_visible_direction() == InboxMessageDirection.inbound.value)
        .with_entities(func.count(InboxConversation.id))
        .scalar()
        or 0
    )


def needs_attention_conversation_count(db: Session) -> int:
    return len(needs_attention_conversation_ids(db))


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
    {"accepted", "sent", "delivered", "read"}
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


_NEEDS_ATTENTION_BATCH_SIZE = 500


def _valid_agent_reply_clause() -> ColumnElement[bool]:
    """SQL prefilter twin of :func:`_is_valid_agent_reply`.

    This clause only narrows candidates. The authoritative Python classifier is
    still applied to every candidate, so JSON differences between supported
    database engines cannot change the visible cohort.
    """

    metadata = InboxMessage.metadata_

    def normalized(key: str) -> ColumnElement[str]:
        return func.lower(func.trim(func.coalesce(metadata[key].as_string(), "")))

    return and_(
        InboxMessage.direction == InboxMessageDirection.outbound.value,
        normalized("sent_by_person_id") != "",
        normalized("delivery_status").in_(_SUCCESSFUL_AGENT_DELIVERY_STATUSES),
        *(
            ~normalized(key).in_(("1", "true", "yes"))
            for key in ("ai_intake", "is_ai_intake", "automated_ai_intake")
        ),
        *(
            ~normalized(key).in_(("0", "false", "no"))
            for key in ("requires_response", "response_required", "requires_reply")
        ),
    )


def needs_attention_conversation_ids(
    db: Session,
    *,
    batch_size: int = _NEEDS_ATTENTION_BATCH_SIZE,
) -> tuple[UUID, ...]:
    """Return the exact attention cohort without hydrating the whole inbox.

    A conversation needs at least two inbound messages and one valid human
    reply before it can enter this cohort. PostgreSQL applies those selective
    predicates first; the existing authoritative classifier then evaluates the
    remaining rows in fixed-size batches, including ticket and social-comment
    exclusions. ORM hydration is therefore bounded independently of inbox size.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    inbound_conversations = (
        select(InboxMessage.conversation_id)
        .where(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .group_by(InboxMessage.conversation_id)
        .having(func.count(InboxMessage.id) >= 2)
    )
    replied_conversations = select(InboxMessage.conversation_id).where(
        _valid_agent_reply_clause()
    )
    candidate_ids = [
        conversation_id
        for (conversation_id,) in db.query(InboxConversation.id)
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .filter(InboxConversation.status != InboxConversationStatus.snoozed.value)
        .filter(InboxConversation.snoozed_until.is_(None))
        .filter(InboxConversation.id.in_(inbound_conversations))
        .filter(InboxConversation.id.in_(replied_conversations))
        .order_by(InboxConversation.id.asc())
        .all()
    ]

    matches: list[UUID] = []

    def classify(ids: list[UUID]) -> None:
        conversations = (
            db.query(InboxConversation).filter(InboxConversation.id.in_(ids)).all()
        )
        messages = _messages_by_conversation(db, ids)
        ticketed = _ticketed_conversation_ids(db, ids)
        matches.extend(
            conversation.id
            for conversation in conversations
            if response_cohort(
                conversation,
                messages.get(conversation.id, ()),
                has_ticket_handoff=conversation.id in ticketed,
            )
            == InboxResponseCohort.needs_attention
        )

    for offset in range(0, len(candidate_ids), batch_size):
        classify(candidate_ids[offset : offset + batch_size])
    return tuple(matches)


def _contact_resolution_status(conversation: InboxConversation) -> str | None:
    metadata = conversation.metadata_ or {}
    resolution = metadata.get("contact_resolution")
    if isinstance(resolution, dict):
        value = str(resolution.get("status") or "").strip()
        return value or None
    return None


def _bounded_name(value: object | None) -> str | None:
    clean = str(value or "").strip()
    return clean[:200] if clean else None


def _display_initials(display_name: str) -> str:
    words = display_name.split()
    if len(words) > 1:
        return f"{words[0][0]}{words[-1][0]}".upper()
    return display_name[:2].upper() or "?"


_FALLBACK_OUTBOUND_SENDER = InboxTimelineSenderIdentity(
    system_user_id=None,
    display_name="Support agent",
    initials="AG",
    source=InboxTimelineSenderSource.fallback,
)


def _outbound_sender_system_user_id(message: InboxMessage) -> UUID | None:
    if message.direction != InboxMessageDirection.outbound.value:
        return None
    raw_value = (message.metadata_ or {}).get("sent_by_person_id")
    try:
        return UUID(str(raw_value)) if raw_value else None
    except (TypeError, ValueError, AttributeError):
        return None


def _staff_display_name(user: SystemUser) -> str:
    return (
        str(user.display_name or "").strip()
        or f"{user.first_name} {user.last_name}".strip()
        or str(user.email or "").strip()
        or "Support agent"
    )


def _outbound_sender_identities(
    db: Session,
    messages: Sequence[InboxMessage],
) -> dict[UUID, InboxTimelineSenderIdentity]:
    sender_ids = {
        sender_id
        for message in messages
        if (sender_id := _outbound_sender_system_user_id(message)) is not None
    }
    if not sender_ids:
        return {}
    users = db.query(SystemUser).filter(SystemUser.id.in_(sender_ids)).all()
    return {
        user.id: InboxTimelineSenderIdentity(
            system_user_id=str(user.id),
            display_name=(display_name := _staff_display_name(user)),
            initials=_display_initials(display_name),
            source=InboxTimelineSenderSource.staff,
        )
        for user in users
    }


def _timeline_sender_identity(
    message: InboxMessage,
    sender_identities: Mapping[UUID, InboxTimelineSenderIdentity],
) -> InboxTimelineSenderIdentity | None:
    if message.direction != InboxMessageDirection.outbound.value:
        return None
    sender_id = _outbound_sender_system_user_id(message)
    if sender_id is None:
        return _FALLBACK_OUTBOUND_SENDER
    return sender_identities.get(sender_id, _FALLBACK_OUTBOUND_SENDER)


def _legacy_subscriber_name(subscriber: Subscriber) -> str | None:
    return next(
        (
            name
            for value in (
                subscriber.display_name,
                subscriber.company_name,
                subscriber.legal_name,
                f"{subscriber.first_name} {subscriber.last_name}",
                subscriber.billing_name,
            )
            if (name := _bounded_name(value)) is not None
        ),
        None,
    )


def _metadata_contact_name(
    conversation: InboxConversation,
) -> tuple[str | None, InboxContactDisplaySource]:
    metadata = conversation.metadata_ or {}
    for key in ("contact_name", "sender_name", "profile_name"):
        if value := _bounded_name(metadata.get(key)):
            source = (
                InboxContactDisplaySource.operator
                if metadata.get("source") == "operator_initiated"
                else InboxContactDisplaySource.provider
            )
            return value, source
    return None, InboxContactDisplaySource.address


def _provider_message_name(messages: Sequence[InboxMessage]) -> str | None:
    for message in reversed(messages):
        if message.direction != InboxMessageDirection.inbound.value:
            continue
        metadata = message.metadata_ or {}
        for key in ("from_name", "contact_name", "sender_name", "profile_name"):
            if value := _bounded_name(metadata.get(key)):
                return value
    return None


def _contact_display_identities(
    db: Session,
    conversations: Sequence[InboxConversation],
    messages_by_conversation: Mapping[UUID, Sequence[InboxMessage]],
) -> dict[UUID, InboxContactDisplayIdentity]:
    subscriber_ids = tuple(
        {row.subscriber_id for row in conversations if row.subscriber_id is not None}
    )
    canonical_names: dict[UUID, tuple[str, InboxContactDisplaySource]] = {}
    if subscriber_ids:
        identity_rows = (
            db.query(Subscriber, Party)
            .outerjoin(Party, Party.id == Subscriber.party_id)
            .filter(Subscriber.id.in_(subscriber_ids))
            .all()
        )
        for subscriber, party in identity_rows:
            party_name = _bounded_name(
                party.display_name if party is not None else None
            )
            if party_name:
                canonical_names[subscriber.id] = (
                    party_name,
                    InboxContactDisplaySource.party,
                )
            elif legacy_name := _legacy_subscriber_name(subscriber):
                canonical_names[subscriber.id] = (
                    legacy_name,
                    InboxContactDisplaySource.subscriber,
                )

    identities: dict[UUID, InboxContactDisplayIdentity] = {}
    for conversation in conversations:
        canonical = (
            canonical_names.get(conversation.subscriber_id)
            if conversation.subscriber_id is not None
            else None
        )
        metadata_name, metadata_source = _metadata_contact_name(conversation)
        provider_name = _provider_message_name(
            messages_by_conversation.get(conversation.id, [])
        )
        if canonical is not None:
            display_name, source = canonical
        elif provider_name:
            display_name = provider_name
            source = InboxContactDisplaySource.provider
        elif metadata_name:
            display_name = metadata_name
            source = metadata_source
        else:
            display_name = (
                _bounded_name(conversation.contact_address) or "Unknown contact"
            )
            source = InboxContactDisplaySource.address
        identities[conversation.id] = InboxContactDisplayIdentity(
            display_name=display_name,
            initials=_display_initials(display_name),
            source=source,
        )
    return identities


def contact_display_identity(
    db: Session,
    *,
    conversation: InboxConversation,
    messages: Sequence[InboxMessage],
) -> InboxContactDisplayIdentity:
    return _contact_display_identities(
        db,
        (conversation,),
        {conversation.id: list(messages)},
    )[conversation.id]


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


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _location_presentation(value: object) -> InboxTimelineLocation | None:
    if not isinstance(value, Mapping):
        return None
    try:
        location = team_inbox_observations.inbound_location_observation(
            latitude=value.get("latitude"),
            longitude=value.get("longitude"),
            name=value.get("name"),
            address=value.get("address"),
        )
    except team_inbox_observations.TeamInboxObservationError:
        return None
    if location is None:
        return None
    latitude = format(location.latitude, ".7f").rstrip("0").rstrip(".")
    longitude = format(location.longitude, ".7f").rstrip("0").rstrip(".")
    query = urlencode({"api": "1", "query": f"{latitude},{longitude}"})
    return InboxTimelineLocation(
        latitude=location.latitude,
        longitude=location.longitude,
        name=location.name,
        address=location.address,
        map_url=f"https://www.google.com/maps/search/?{query}",
    )


def _attachment_from_mapping(item: Mapping[str, object]) -> InboxTimelineAttachment:
    asset_type = _optional_text(item.get("type") or item.get("asset_type")) or "file"
    location = (
        _location_presentation(item.get("location"))
        if asset_type == "location"
        else None
    )
    raw_url = _optional_text(
        item.get("url") or item.get("storage_url") or item.get("source_url")
    )
    url = (
        location.map_url
        if location
        else (None if asset_type == "location" else raw_url)
    )
    raw_metadata = item.get("metadata")
    return InboxTimelineAttachment(
        id=_optional_text(item.get("id")),
        type=asset_type,
        filename=_optional_text(item.get("filename") or item.get("file_name")),
        file_name=_optional_text(item.get("file_name") or item.get("filename")),
        mime_type=_optional_text(item.get("mime_type")),
        file_size=_optional_int(item.get("file_size")),
        caption=_optional_text(item.get("caption")),
        url=url,
        source_url=_optional_text(item.get("source_url")),
        storage_url=_optional_text(item.get("storage_url")),
        provider=_optional_text(item.get("provider")),
        provider_media_id=_optional_text(
            item.get("provider_media_id") or item.get("id")
        ),
        download_status=_optional_text(item.get("download_status")),
        download_error=_optional_text(item.get("download_error")),
        content_available=bool(url),
        metadata=(
            {str(key): nested for key, nested in raw_metadata.items()}
            if isinstance(raw_metadata, Mapping)
            else None
        ),
        location=location,
    )


def _message_attachments(message: InboxMessage) -> list[InboxTimelineAttachment]:
    metadata = message.metadata_ or {}
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [
        _attachment_from_mapping(item) for item in attachments if isinstance(item, dict)
    ]


def _asset_attachment(asset: InboxMediaAsset) -> InboxTimelineAttachment:
    metadata = asset.metadata_ or {}
    location = (
        _location_presentation(metadata.get("location"))
        if asset.asset_type == "location"
        else None
    )
    url = None
    if location is not None:
        url = location.map_url
    elif asset.asset_type != "location":
        url = (
            team_inbox_media.media_content_url(asset.id)
            if asset.download_status in {"stored", "remote_available", "metadata_only"}
            else (asset.storage_url or asset.source_url)
        )
    return InboxTimelineAttachment(
        id=str(asset.id),
        type=asset.asset_type,
        filename=asset.file_name,
        file_name=asset.file_name,
        mime_type=asset.mime_type,
        file_size=asset.file_size,
        caption=asset.caption,
        url=url,
        source_url=asset.source_url,
        storage_url=asset.storage_url,
        provider=asset.provider,
        provider_media_id=asset.provider_media_id,
        download_status=asset.download_status,
        download_error=asset.download_error,
        content_available=bool(url),
        metadata={str(key): value for key, value in metadata.items()},
        location=location,
    )


def list_conversations(
    db: Session,
    *,
    conversation_id: str | UUID | None = None,
    search: str | None = None,
    status: str | None = None,
    channel_type: str | None = None,
    channel_types: Sequence[str] | None = None,
    subscriber_id: str | UUID | None = None,
    service_team_id: str | UUID | None = None,
    service_team_ids: Sequence[str | UUID] | None = None,
    advanced_filters: team_inbox_filters.InboxAdvancedFilterQuery | None = None,
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
    reply_window_status: str | None = None,
    ai_handling: bool | None = None,
    has_ticket: bool | None = None,
    activity_from: datetime | None = None,
    activity_to: datetime | None = None,
    order_by: str | None = None,
    order_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
    include_total_count: bool = True,
) -> InboxConversationListResult:
    query = (
        db.query(InboxConversation, ServiceTeam)
        .outerjoin(
            ServiceTeam, ServiceTeam.id == InboxConversation.primary_service_team_id
        )
        .filter(InboxConversation.is_active.is_(True))
    )
    try:
        target_conversation_id = _optional_uuid(conversation_id)
    except ValueError:
        return InboxConversationListResult(
            items=[], count=0, limit=limit, offset=offset
        )
    if target_conversation_id is not None:
        query = query.filter(InboxConversation.id == target_conversation_id)
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
    clean_channel_types = tuple(
        str(item).strip() for item in (channel_types or ()) if str(item).strip()
    )
    if channel_type:
        query = query.filter(InboxConversation.channel_type == channel_type)
    elif clean_channel_types:
        query = query.filter(InboxConversation.channel_type.in_(clean_channel_types))
    clean_reply_window_status = str(reply_window_status or "").strip().lower()
    if clean_reply_window_status == "expired":
        reply_window_cutoff = datetime.now(UTC) - timedelta(hours=24)
        latest_inbound = (
            db.query(
                InboxMessage.conversation_id.label("conversation_id"),
                func.max(
                    func.coalesce(InboxMessage.received_at, InboxMessage.created_at)
                ).label("last_inbound_at"),
            )
            .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
            .filter(
                or_(
                    InboxMessage.metadata_["reply_window_qualifying"]
                    .as_boolean()
                    .isnot(False),
                    InboxMessage.metadata_["reply_window_qualifying"].is_(None),
                )
            )
            .group_by(InboxMessage.conversation_id)
            .subquery()
        )
        query = query.join(
            latest_inbound,
            latest_inbound.c.conversation_id == InboxConversation.id,
        ).filter(
            InboxConversation.channel_type.in_(
                (
                    InboxChannelType.whatsapp.value,
                    InboxChannelType.facebook_messenger.value,
                    InboxChannelType.instagram_dm.value,
                )
            ),
            latest_inbound.c.last_inbound_at.isnot(None),
            latest_inbound.c.last_inbound_at <= reply_window_cutoff,
        )
        if not status:
            query = query.filter(
                InboxConversation.status.in_(
                    (
                        InboxConversationStatus.open.value,
                        InboxConversationStatus.pending.value,
                        InboxConversationStatus.snoozed.value,
                    )
                )
            )
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

    if advanced_filters is not None:
        advanced_filter_expression = team_inbox_filters.build_filter_expression(
            advanced_filters
        )
        if advanced_filter_expression is not None:
            query = query.filter(advanced_filter_expression)

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
        # The unread rule stays with its owner; this only asks for its grouped
        # set selector. No correlated per-conversation probe is admitted here.
        # Without an operator nothing can be unread, and an empty page is the
        # honest answer rather than the whole queue.
        query = query.filter(
            InboxConversation.id.in_(
                team_inbox_read_state.unread_conversation_ids_select(operator_person_id)
            )
            if operator_person_id is not None
            else false()
        )

    if needs_attention:
        attention_ids = needs_attention_conversation_ids(db)
        query = query.filter(
            InboxConversation.id.in_(attention_ids) if attention_ids else false()
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
    total = query.count() if include_total_count else 0
    # `needs_response` and `contact_resolution_status` are already SQL filters
    # above, so listing them here too would load the whole filtered set before a
    # The Python-only attention classifier now runs on a selective candidate
    # set in fixed-size batches before this query. Pagination remains in SQL;
    # the row-level checks below stay as a safety net.
    needs_python_filter = False
    row_limit = limit if include_total_count else limit + 1
    rows = (
        ordered_query.all()
        if needs_python_filter
        else ordered_query.limit(row_limit).offset(offset).all()
    )
    has_next_page = not include_total_count and len(rows) > limit
    if has_next_page:
        rows = rows[:limit]
    if not include_total_count:
        total = offset + len(rows) + (1 if has_next_page else 0)
        if not rows and offset > 0:
            total = query.count()
    conversations = [conversation for conversation, _team in rows]
    conversation_ids = [conversation.id for conversation in conversations]
    messages_by_conversation = _messages_by_conversation(db, conversation_ids)
    latest_messages = {
        conversation_id: latest
        for conversation_id, messages in messages_by_conversation.items()
        if (latest := _latest_external_message(messages)) is not None
    }
    contact_identities = _contact_display_identities(
        db,
        conversations,
        messages_by_conversation,
    )
    ticketed_conversation_ids = _ticketed_conversation_ids(db, conversation_ids)
    queue_entries = (
        db.query(InboxConversationQueueEntry)
        .filter(
            InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
        )
        .filter(
            InboxConversationQueueEntry.service_team_id.in_(
                [
                    conversation.primary_service_team_id
                    for conversation in conversations
                    if conversation.primary_service_team_id is not None
                ]
            )
        )
        .order_by(
            InboxConversationQueueEntry.service_team_id.asc(),
            InboxConversationQueueEntry.entered_at.asc(),
            InboxConversationQueueEntry.queue_position.asc(),
        )
        .all()
        if conversations
        else []
    )
    queue_by_conversation: dict[UUID, tuple[InboxConversationQueueEntry, int]] = {}
    team_rank: dict[UUID, int] = {}
    for entry in queue_entries:
        rank = team_rank.get(entry.service_team_id, 0) + 1
        team_rank[entry.service_team_id] = rank
        queue_by_conversation[entry.conversation_id] = (entry, rank)
    capacity_by_team = {
        team_id: team_inbox_assignment.team_capacity_snapshot(db, team_id)
        for team_id in team_rank
    }
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
    reply_window_statuses = (
        _reply_window_statuses(db, conversations) if conversations else {}
    )

    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        tuple(team.id for _conversation, team in rows if team is not None),
    )
    items: list[InboxConversationListRow] = []
    for conversation, team in rows:
        latest = latest_messages.get(conversation.id)
        contact_identity = contact_identities[conversation.id]
        active_assignment = active_assignments.get(conversation.id)
        queue_projection = queue_by_conversation.get(conversation.id)
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
                primary_service_team_capabilities=(
                    tuple(
                        capability.value for capability in capabilities_by_team[team.id]
                    )
                    if team is not None
                    else ()
                ),
                channel_type=conversation.channel_type,
                status=conversation.status,
                priority=conversation.priority,
                is_muted=conversation.is_muted,
                snoozed_until=conversation.snoozed_until,
                is_snoozed=_is_currently_snoozed(conversation),
                contact_name=contact_identity.display_name,
                contact_initials=contact_identity.initials,
                contact_name_source=contact_identity.source.value,
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
                queue_position=queue_projection[1] if queue_projection else None,
                queued_at=queue_projection[0].entered_at if queue_projection else None,
                estimated_wait_minutes=(
                    team_inbox_assignment.estimate_queue_wait_minutes(
                        queue_position=queue_projection[1],
                        active_assignments=capacity_by_team[
                            queue_projection[0].service_team_id
                        ].active_assignments,
                        total_capacity=capacity_by_team[
                            queue_projection[0].service_team_id
                        ].total_capacity,
                    )
                    if queue_projection
                    else None
                ),
                needs_response=row_needs_response,
                needs_attention=row_needs_attention,
                has_ticket=conversation.id in ticketed_conversation_ids,
                is_unread=row_is_unread,
                unread_count=unread_count,
                team_count=int(team_counts.get(conversation.id, 0)),
                labels=tuple(labels_by_conversation.get(conversation.id, [])),
                reply_window_status=reply_window_statuses.get(
                    conversation.id, "not_applicable"
                ),
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


def _reply_window_statuses(
    db: Session, conversations: Sequence[InboxConversation]
) -> dict[UUID, str]:
    if not conversations:
        return {}
    conversation_ids = [conversation.id for conversation in conversations]
    meta_channels = {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
    }
    meta_conversation_ids = [
        conversation.id
        for conversation in conversations
        if conversation.channel_type in meta_channels
    ]
    statuses = {
        conversation.id: (
            "not_applicable"
            if conversation.channel_type not in meta_channels
            else "unavailable"
        )
        for conversation in conversations
    }
    if not meta_conversation_ids:
        return statuses
    rows = (
        db.query(
            InboxMessage.conversation_id,
            func.max(func.coalesce(InboxMessage.received_at, InboxMessage.created_at)),
        )
        .filter(InboxMessage.conversation_id.in_(meta_conversation_ids))
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .filter(
            or_(
                InboxMessage.metadata_["reply_window_qualifying"]
                .as_boolean()
                .isnot(False),
                InboxMessage.metadata_["reply_window_qualifying"].is_(None),
            )
        )
        .group_by(InboxMessage.conversation_id)
        .all()
    )
    now = datetime.now(UTC)
    for conversation_id, last_inbound_at in rows:
        if last_inbound_at is None:
            continue
        if last_inbound_at.tzinfo is None:
            last_inbound_at = last_inbound_at.replace(tzinfo=UTC)
        statuses[conversation_id] = (
            "open" if now < last_inbound_at + timedelta(hours=24) else "expired"
        )
    return statuses


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
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        tuple(team.id for _link, team in team_rows if team is not None),
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
            _message_time_column().asc(),
            InboxMessage.id.asc(),
        )
        .all()
    )
    outbound_sender_identities = _outbound_sender_identities(db, messages)
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
    contact_identity = contact_display_identity(
        db,
        conversation=conversation,
        messages=messages,
    )
    queue_entry = (
        db.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .filter(
            InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
        )
        .one_or_none()
    )
    queue_position = None
    estimated_wait_minutes = None
    if queue_entry is not None:
        queue_position = int(
            db.query(func.count(InboxConversationQueueEntry.id))
            .filter(
                InboxConversationQueueEntry.service_team_id
                == queue_entry.service_team_id
            )
            .filter(
                InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
            )
            .filter(
                or_(
                    InboxConversationQueueEntry.entered_at < queue_entry.entered_at,
                    and_(
                        InboxConversationQueueEntry.entered_at
                        == queue_entry.entered_at,
                        InboxConversationQueueEntry.queue_position
                        <= queue_entry.queue_position,
                    ),
                )
            )
            .scalar()
            or 0
        )
        capacity = team_inbox_assignment.team_capacity_snapshot(
            db, queue_entry.service_team_id
        )
        estimated_wait_minutes = team_inbox_assignment.estimate_queue_wait_minutes(
            queue_position=queue_position,
            active_assignments=capacity.active_assignments,
            total_capacity=capacity.total_capacity,
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
        contact_name=contact_identity.display_name,
        contact_initials=contact_identity.initials,
        contact_name_source=contact_identity.source.value,
        external_thread_id=conversation.external_thread_id,
        first_message_at=conversation.first_message_at,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        metadata=conversation.metadata_,
        queue_position=queue_position,
        queued_at=queue_entry.entered_at if queue_entry else None,
        estimated_wait_minutes=estimated_wait_minutes,
        teams=[
            InboxTimelineTeam(
                service_team_id=str(link.service_team_id),
                service_team_name=team.name if team is not None else None,
                service_team_capabilities=(
                    tuple(
                        capability.value for capability in capabilities_by_team[team.id]
                    )
                    if team is not None
                    else ()
                ),
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
            _timeline_message_projection(
                message,
                assets=assets_by_message.get(message.id, []),
                outbound_sender_identities=outbound_sender_identities,
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


def _timeline_message_projection(
    message: InboxMessage,
    *,
    assets: Sequence[InboxMediaAsset],
    outbound_sender_identities: Mapping[UUID, InboxTimelineSenderIdentity],
) -> InboxTimelineMessage:
    return InboxTimelineMessage(
        id=str(message.id),
        channel_type=message.channel_type,
        direction=message.direction,
        subject=message.subject,
        body=message.body,
        from_address=message.from_address,
        to_addresses=[str(value) for value in (message.to_addresses or [])],
        cc_addresses=[str(value) for value in (message.cc_addresses or [])],
        sent_at=message.sent_at,
        received_at=message.received_at,
        created_at=message.created_at,
        metadata=message.metadata_,
        attachments=(
            [_asset_attachment(asset) for asset in assets]
            or _message_attachments(message)
        ),
        sender=_timeline_sender_identity(message, outbound_sender_identities),
    )


def get_conversation_message(
    db: Session,
    *,
    conversation_id: UUID,
    message_id: UUID,
) -> InboxTimelineMessage | None:
    """Project one authoritative message without rebuilding its full thread."""

    message = (
        db.query(InboxMessage)
        .join(InboxConversation, InboxConversation.id == InboxMessage.conversation_id)
        .filter(InboxConversation.id == conversation_id)
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxMessage.id == message_id)
        .one_or_none()
    )
    if message is None:
        return None
    assets_by_message = team_inbox_media.assets_for_messages(db, [message.id])
    return _timeline_message_projection(
        message,
        assets=assets_by_message.get(message.id, []),
        outbound_sender_identities=_outbound_sender_identities(db, [message]),
    )
