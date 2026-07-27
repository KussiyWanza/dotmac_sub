from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxTeamSource,
)
from app.services import (
    team_inbox_assignment,
    team_inbox_channel_receive,
    team_inbox_operations,
    team_inbox_participants,
    team_inbox_realtime,
    team_inbox_routing,
)
from app.services.realtime_platform import EventType

_MESSAGE_ID_RE = re.compile(r"<[^<>]+>")
_AUTO_ASSIGN_METADATA_KEYS = (
    "inbox_auto_assign_to_agent",
    "auto_assign_to_online_agent",
    "inbox_auto_assign",
)


@dataclass(frozen=True)
class InboundEmailPayload:
    from_address: str
    subject: str | None = None
    body: str | None = None
    to_addresses: list[str] = field(default_factory=list)
    cc_addresses: list[str] = field(default_factory=list)
    message_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    received_at: datetime | None = None
    subscriber_id: str | UUID | None = None
    fallback_service_team_id: str | UUID | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class InboundEmailReceiveResult:
    kind: str
    conversation_id: str
    message_id: str
    duplicate: bool
    # Same shape the channel result carries, so the observation coordinator
    # records what an email actually resolved to instead of reading these off a
    # result that never had them and storing "unmatched" for every mailbox.
    subscriber_id: str | None = None
    reseller_id: str | None = None
    resolution_status: str = "unmatched"


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    match = _MESSAGE_ID_RE.search(stripped)
    return match.group(0) if match else stripped


def _extract_message_ids(*headers: str | None) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for header in headers:
        if not header:
            continue
        candidates = _MESSAGE_ID_RE.findall(header)
        if not candidates:
            normalized = _normalize_message_id(header)
            candidates = [normalized] if normalized else []
        for candidate in candidates:
            normalized = _normalize_message_id(candidate)
            if normalized and normalized not in seen:
                seen.add(normalized)
                values.append(normalized)
    return values


def _find_duplicate_message(
    db: Session, external_message_id: str | None
) -> InboxMessage | None:
    if not external_message_id:
        return None
    return (
        db.query(InboxMessage)
        .filter(InboxMessage.channel_type == InboxChannelType.email.value)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .filter(InboxMessage.external_message_id == external_message_id)
        .first()
    )


def _find_thread_conversation(
    db: Session,
    *,
    message_ids: list[str],
) -> InboxConversation | None:
    """The live conversation a referenced Message-ID belongs to, if any.

    Only a live thread is joinable. The referenced message used to be matched
    with no conditions on its conversation at all, so a reply could attach to a
    soft-deleted thread, or to a resolved one — and since inbound email never
    changes status, a resolved thread did not reopen either, so the message
    landed where nobody was looking.

    This mirrors the channel path's ``_find_open_conversation``, which has
    always required an active, unresolved conversation. A reply that finds no
    live thread opens a new one, exactly as a WhatsApp reply after resolution
    already does.
    """
    if not message_ids:
        return None
    message = (
        db.query(InboxMessage)
        .join(InboxConversation, InboxConversation.id == InboxMessage.conversation_id)
        .filter(InboxMessage.channel_type == InboxChannelType.email.value)
        .filter(InboxMessage.external_message_id.in_(message_ids))
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    return message.conversation if message else None


def _trim_subject(value: str | None) -> str | None:
    subject = (value or "").strip()
    if not subject:
        return None
    return subject[:200]


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def _metadata_auto_assign_enabled(metadata: dict | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    nested = metadata.get("inbox_assignment")
    candidates = [metadata.get(key) for key in _AUTO_ASSIGN_METADATA_KEYS]
    if isinstance(nested, dict):
        candidates.extend(nested.get(key) for key in _AUTO_ASSIGN_METADATA_KEYS)
    return any(_truthy(value) for value in candidates)


def _route_auto_assign_enabled(
    db: Session,
    plan: team_inbox_routing.EmailTeamRoutingPlan,
) -> bool:
    if not plan.primary_service_team_id:
        return False
    primary_match = next(
        (
            match
            for match in plan.matches
            if match.service_team_id == plan.primary_service_team_id
        ),
        None,
    )
    if primary_match and _metadata_auto_assign_enabled(primary_match.metadata):
        return True
    team = db.get(ServiceTeam, UUID(str(plan.primary_service_team_id)))
    return _metadata_auto_assign_enabled(team.metadata_ if team is not None else None)


def receive_inbound_email(
    db: Session,
    payload: InboundEmailPayload,
) -> InboundEmailReceiveResult:
    external_message_id = _normalize_message_id(payload.message_id)
    duplicate = _find_duplicate_message(db, external_message_id)
    if duplicate is not None:
        return InboundEmailReceiveResult(
            kind="duplicate",
            conversation_id=str(duplicate.conversation_id),
            message_id=str(duplicate.id),
            duplicate=True,
        )

    normalized_from = team_inbox_routing.normalize_email_address(payload.from_address)
    normalized_to = team_inbox_routing.normalize_email_addresses(payload.to_addresses)
    normalized_cc = team_inbox_routing.normalize_email_addresses(payload.cc_addresses)
    received_at = payload.received_at or datetime.now(UTC)

    # Email resolves its sender exactly like every other channel. It used to
    # carry only whatever `subscriber_id` the caller supplied — and no caller
    # supplies one — so every inbound email landed with a null subscriber and
    # no `contact_resolution`, invisible to the contact filter and to the
    # customer record's communications section.
    # The already-parsed address, not the raw `From:` header — a header carries
    # a display name ("Ada <ada@example.com>") and the channel normalizer does
    # not strip one, so passing it raw resolved nobody.
    resolution = team_inbox_channel_receive.resolve_contact_context(
        db,
        channel_type=InboxChannelType.email.value,
        contact_address=normalized_from or payload.from_address,
        subscriber_id=payload.subscriber_id,
    )

    thread_message_ids = _extract_message_ids(payload.in_reply_to, payload.references)
    conversation = _find_thread_conversation(db, message_ids=thread_message_ids)
    created_conversation = conversation is None

    if conversation is None:
        conversation = InboxConversation(
            subscriber_id=resolution.subscriber_id,
            channel_type=InboxChannelType.email.value,
            status=InboxConversationStatus.open.value,
            subject=_trim_subject(payload.subject),
            contact_address=normalized_from,
            external_thread_id=thread_message_ids[0]
            if thread_message_ids
            else external_message_id,
            first_message_at=received_at,
            last_message_at=received_at,
            metadata_={"contact_resolution": resolution.as_metadata()},
        )
        db.add(conversation)
        db.flush()
    else:
        conversation.last_message_at = received_at
        if normalized_from and not conversation.contact_address:
            conversation.contact_address = normalized_from
        if resolution.subscriber_id and not conversation.subscriber_id:
            conversation.subscriber_id = resolution.subscriber_id
        metadata = dict(conversation.metadata_ or {})
        metadata["contact_resolution"] = resolution.as_metadata()
        conversation.metadata_ = metadata

    routing_plan = team_inbox_routing.build_email_team_routing_plan(
        db,
        to_addresses=payload.to_addresses,
        cc_addresses=payload.cc_addresses,
        fallback_service_team_id=payload.fallback_service_team_id,
    )
    team_inbox_routing.apply_email_routing_plan(
        db,
        conversation=conversation,
        plan=routing_plan,
    )
    if (
        created_conversation
        and routing_plan.primary_service_team_id
        and _route_auto_assign_enabled(db, routing_plan)
    ):
        team_inbox_assignment.assign_conversation_to_available_agent(
            db,
            conversation=conversation,
            service_team_id=routing_plan.primary_service_team_id,
            reason="inbound_email_route_auto_assign",
            source=InboxTeamSource.routing_rule.value,
            now=received_at,
        )

    metadata = dict(payload.metadata or {})
    metadata["in_reply_to"] = payload.in_reply_to
    metadata["references"] = payload.references
    metadata["contact_resolution"] = resolution.as_metadata()
    metadata["routing"] = {
        "primary_service_team_id": routing_plan.primary_service_team_id,
        "participant_service_team_ids": routing_plan.participant_service_team_ids,
        "unmatched_recipients": routing_plan.unmatched_recipients,
    }
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.email.value,
        direction=InboxMessageDirection.inbound.value,
        subject=_trim_subject(payload.subject),
        body=payload.body,
        external_message_id=external_message_id,
        external_thread_id=conversation.external_thread_id,
        from_address=normalized_from,
        to_addresses=normalized_to,
        cc_addresses=normalized_cc,
        received_at=received_at,
        metadata_=metadata,
    )
    db.add(message)
    db.flush()

    # Shadow projection: record which endpoints took part. Nothing reads it for
    # a threading or export decision yet, so a failure here must not cost us an
    # ingested message.
    team_inbox_participants.record_message_participants(
        db, conversation=conversation, message=message
    )

    conversation.last_message_at = received_at
    # Same wake rule as the channel path: an inbound email is the reply.
    team_inbox_operations.wake_on_inbound(db, conversation=conversation)
    if conversation.first_message_at is None:
        conversation.first_message_at = received_at
    # Same liveness signal as the channel path. Without it an inbound email was
    # the one arrival an operator with a healthy socket never saw.
    team_inbox_realtime.publish_conversation_event(
        db,
        str(conversation.id),
        event_type=EventType.MESSAGE_NEW,
        payload=team_inbox_realtime.message_event_payload(
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            body=message.body,
            direction=message.direction,
            channel_type=message.channel_type,
            created_at=message.created_at,
            extra={"sender_type": "visitor", "from_customer": True},
        ),
    )
    team_inbox_realtime.publish_queue_event(
        db,
        conversation_id=str(conversation.id),
        created=created_conversation,
    )
    return InboundEmailReceiveResult(
        kind="received",
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        duplicate=False,
        subscriber_id=str(resolution.subscriber_id)
        if resolution.subscriber_id
        else None,
        reseller_id=str(resolution.reseller_id) if resolution.reseller_id else None,
        resolution_status=resolution.status,
    )
