"""Which endpoints took part in a conversation.

Shadow projection over observed message headers. It records *that* an endpoint
participated and how it was admitted; it does not decide who the endpoint
belongs to, and nothing reads it for a threading or export decision yet.

Two separations are load-bearing:

- **Endpoint before party.** ``party_contact_point_id`` is nullable. Inbox owns
  participation; ``party.registry`` owns identity. Requiring the binding would
  make an unknown colleague or an unreviewed address unrepresentable.
- **Admission source before relationship.** How an endpoint arrived (``From``,
  ``Cc``, operator) is evidence and never changes. What it turns out to be
  (customer, contact, third party) is a classification that Party may revise
  later. Collapsing them would let a reclassification rewrite history.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.party import PartyContactPoint
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationParticipant,
    InboxMessage,
    InboxMessageDirection,
    InboxParticipantAdmissionSource,
    InboxParticipantRelationship,
)
from app.services import team_inbox_routing
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_channel_address,
)

OWNER = "communications.team_inbox_participants"

_OPAQUE_ENDPOINT_CHANNELS = {
    InboxChannelType.facebook_messenger.value,
    InboxChannelType.instagram_dm.value,
    InboxChannelType.chat_widget.value,
    InboxChannelType.field_job.value,
}


@dataclass(frozen=True, slots=True)
class ObservedEndpoint:
    """One endpoint seen on one message, with how it was seen."""

    channel_type: str
    normalized_endpoint: str
    admission_source: InboxParticipantAdmissionSource
    provider_account_scope: str = "default"
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ParticipantRow:
    id: str
    channel_type: str
    normalized_endpoint: str
    provider_account_scope: str
    relationship_type: str
    admission_source: str
    admitted_at: datetime
    party_contact_point_id: str | None
    display_name: str | None
    is_active: bool


def _normalize(db: Session, channel_type: str, value: str | None) -> str | None:
    """Normalize the way the resolver does, so endpoints join up later."""
    if channel_type in _OPAQUE_ENDPOINT_CHANNELS:
        normalized = str(value or "").strip()
        return normalized or None
    if channel_type == InboxChannelType.email.value:
        return team_inbox_routing.normalize_email_address(value)
    return normalize_channel_address(
        channel_type, value, default_country_code=default_country_code(db)
    )


def _provider_account_scope(message: InboxMessage) -> str:
    metadata = message.metadata_ or {}
    for key in (
        "provider_account_scope",
        "phone_number_id",
        "page_or_account_id",
        "account_scope",
    ):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value[:200]
    return "default"


def observed_endpoints(
    db: Session,
    *,
    message: InboxMessage,
    owned_addresses: frozenset[str] | None = None,
) -> tuple[ObservedEndpoint, ...]:
    """Every counterparty endpoint this message carries.

    ``To`` is captured as well as ``From`` and ``Cc`` — a colleague addressed
    directly is as much a participant as one copied — with our own mailboxes
    removed through the routing owner, which is the register of what is ours.
    """
    channel_type = message.channel_type
    owned = (
        owned_addresses
        if owned_addresses is not None
        else team_inbox_routing.owned_mailbox_addresses(db)
    )
    scope = _provider_account_scope(message)
    inbound = message.direction == InboxMessageDirection.inbound.value

    candidates: list[tuple[str | None, InboxParticipantAdmissionSource]] = []
    if inbound:
        candidates.append(
            (message.from_address, InboxParticipantAdmissionSource.inbound_from)
        )
        candidates.extend(
            (address, InboxParticipantAdmissionSource.inbound_to)
            for address in (message.to_addresses or [])
        )
        candidates.extend(
            (address, InboxParticipantAdmissionSource.inbound_cc)
            for address in (message.cc_addresses or [])
        )
    else:
        # An outbound `from` is our sender, never a participant.
        candidates.extend(
            (address, InboxParticipantAdmissionSource.outbound_to)
            for address in (message.to_addresses or [])
        )
        candidates.extend(
            (address, InboxParticipantAdmissionSource.outbound_cc)
            for address in (message.cc_addresses or [])
        )

    seen: set[str] = set()
    endpoints: list[ObservedEndpoint] = []
    for raw, source in candidates:
        normalized = _normalize(db, channel_type, raw)
        if not normalized or normalized in seen:
            continue
        if normalized in owned:
            continue
        seen.add(normalized)
        endpoints.append(
            ObservedEndpoint(
                channel_type=channel_type,
                normalized_endpoint=normalized[:320],
                admission_source=source,
                provider_account_scope=scope,
            )
        )
    return tuple(endpoints)


def record_message_participants(
    db: Session,
    *,
    conversation: InboxConversation,
    message: InboxMessage,
    owned_addresses: frozenset[str] | None = None,
) -> int:
    """Admit every endpoint this message carries that is not already a party.

    Idempotent by ``(conversation, channel, endpoint, provider scope)``: the
    same address on every message in a thread admits one participant, and the
    first admission keeps its source. Re-observing an endpoint on a later
    message must not rewrite how it originally arrived.
    """
    endpoints = observed_endpoints(db, message=message, owned_addresses=owned_addresses)
    if not endpoints:
        return 0

    existing = {
        (row.normalized_endpoint, row.provider_account_scope)
        for row in db.execute(
            select(InboxConversationParticipant).where(
                InboxConversationParticipant.conversation_id == conversation.id,
                InboxConversationParticipant.channel_type == message.channel_type,
                InboxConversationParticipant.is_active.is_(True),
            )
        ).scalars()
    }

    admitted = 0
    for endpoint in endpoints:
        key = (endpoint.normalized_endpoint, endpoint.provider_account_scope)
        if key in existing:
            continue
        existing.add(key)
        db.add(
            InboxConversationParticipant(
                conversation_id=conversation.id,
                channel_type=endpoint.channel_type,
                normalized_endpoint=endpoint.normalized_endpoint,
                provider_account_scope=endpoint.provider_account_scope,
                relationship_type=InboxParticipantRelationship.unknown.value,
                admission_source=endpoint.admission_source.value,
                admission_message_id=message.id,
                admitted_at=message.received_at or message.sent_at or datetime.now(UTC),
                display_name=endpoint.display_name,
                is_active=True,
            )
        )
        admitted += 1
    if admitted:
        db.flush()
    return admitted


def bind_endpoint_to_contact_point(
    db: Session,
    *,
    conversation_id: UUID,
    channel_type: str,
    normalized_endpoint: str,
    provider_account_scope: str,
    party_contact_point_id: UUID,
) -> InboxConversationParticipant:
    """Bind one exact observed endpoint to Party reachability evidence."""

    row = db.scalars(
        select(InboxConversationParticipant)
        .where(
            InboxConversationParticipant.conversation_id == conversation_id,
            InboxConversationParticipant.channel_type == channel_type,
            InboxConversationParticipant.normalized_endpoint == normalized_endpoint,
            InboxConversationParticipant.provider_account_scope
            == provider_account_scope,
            InboxConversationParticipant.is_active.is_(True),
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise ValueError("The exact Inbox participant endpoint was not found.")
    contact_point = db.get(PartyContactPoint, party_contact_point_id)
    if contact_point is None:
        raise ValueError("The Party contact point was not found.")
    if (
        contact_point.channel_type != channel_type
        or contact_point.normalized_value != normalized_endpoint
    ):
        raise ValueError("The Party contact point does not match the Inbox endpoint.")
    if channel_type in _OPAQUE_ENDPOINT_CHANNELS and (
        contact_point.provider_account_id != provider_account_scope
        or contact_point.external_subject_id != normalized_endpoint
    ):
        raise ValueError("The Party contact point does not match the provider scope.")
    if row.party_contact_point_id is not None:
        if row.party_contact_point_id != contact_point.id:
            raise ValueError(
                "The Inbox endpoint is already bound to another contact point."
            )
        return row
    row.party_contact_point_id = contact_point.id
    row.party_contact_point_bound_at = datetime.now(UTC)
    row.party_contact_point_binding_source = "sales.lead_intake"
    row.party_contact_point_binding_reason = (
        "Customer completed the single-use form issued to this exact endpoint"
    )
    row.relationship_type = InboxParticipantRelationship.contact.value
    db.flush()
    return row


def list_participants(
    db: Session,
    *,
    conversation_id: str | UUID,
    include_removed: bool = False,
) -> list[ParticipantRow]:
    conversation_uuid = (
        conversation_id
        if isinstance(conversation_id, UUID)
        else UUID(str(conversation_id))
    )
    query = select(InboxConversationParticipant).where(
        InboxConversationParticipant.conversation_id == conversation_uuid
    )
    if not include_removed:
        query = query.where(InboxConversationParticipant.is_active.is_(True))
    rows = db.execute(query.order_by(InboxConversationParticipant.admitted_at.asc()))
    return [
        ParticipantRow(
            id=str(row.id),
            channel_type=row.channel_type,
            normalized_endpoint=row.normalized_endpoint,
            provider_account_scope=row.provider_account_scope,
            relationship_type=row.relationship_type,
            admission_source=row.admission_source,
            admitted_at=row.admitted_at,
            party_contact_point_id=str(row.party_contact_point_id)
            if row.party_contact_point_id
            else None,
            display_name=row.display_name,
            is_active=row.is_active,
        )
        for row in rows.scalars()
    ]


def endpoint_is_participant(
    db: Session,
    *,
    conversation_id: str | UUID,
    channel_type: str,
    endpoint: str,
) -> bool:
    """Whether this exact active endpoint is on this conversation.

    Deliberately exact, not party-wide: binding one contact point to a party
    must not silently admit every other address that party owns. A rule that
    widens on binding would grant thread access nobody reviewed.

    Not a proof of authenticity — a spoofed sender can claim a participant
    address. Any security-strength admission rule must also weigh transport
    authentication evidence, which is retained on the message but not
    interpreted here.
    """
    normalized = _normalize(db, channel_type, endpoint)
    if not normalized:
        return False
    conversation_uuid = (
        conversation_id
        if isinstance(conversation_id, UUID)
        else UUID(str(conversation_id))
    )
    return (
        db.execute(
            select(InboxConversationParticipant.id)
            .where(
                InboxConversationParticipant.conversation_id == conversation_uuid,
                InboxConversationParticipant.channel_type == channel_type,
                InboxConversationParticipant.normalized_endpoint == normalized,
                InboxConversationParticipant.is_active.is_(True),
            )
            .limit(1)
        ).first()
        is not None
    )


def backfill_conversations(
    db: Session,
    *,
    conversation_ids: Sequence[UUID] | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Rebuild participants from stored message headers.

    Idempotent: it only admits endpoints that are missing, so a re-run over an
    already-projected conversation changes nothing and a partially projected
    one is completed.

    Coverage is bounded by what the headers preserve. A conversation whose
    messages arrived without ``To``/``Cc`` — anything imported from a system
    that did not carry them — yields only its ``From`` endpoints, so a parity
    figure over these rows must be read against that, not against every
    conversation.
    """
    owned = team_inbox_routing.owned_mailbox_addresses(db)
    targets: Iterable[UUID]
    if conversation_ids is not None:
        targets = list(conversation_ids)
    else:
        # Conversations with no projection yet, oldest first, so a repeated run
        # walks forward instead of re-treading the same head.
        projected = select(InboxConversationParticipant.conversation_id)
        targets = list(
            db.scalars(
                select(InboxConversation.id)
                .where(InboxConversation.is_active.is_(True))
                .where(~InboxConversation.id.in_(projected))
                .order_by(InboxConversation.created_at.asc())
                .limit(max(1, limit))
            ).all()
        )

    conversations_touched = 0
    admitted_total = 0
    for conversation_id in targets:
        conversation = db.get(InboxConversation, conversation_id)
        if conversation is None:
            continue
        messages = db.scalars(
            select(InboxMessage)
            .where(InboxMessage.conversation_id == conversation_id)
            .where(InboxMessage.direction != InboxMessageDirection.internal.value)
            .order_by(InboxMessage.created_at.asc())
        ).all()
        admitted = 0
        for message in messages:
            admitted += record_message_participants(
                db,
                conversation=conversation,
                message=message,
                owned_addresses=owned,
            )
        if admitted:
            conversations_touched += 1
            admitted_total += admitted
    db.flush()
    return {"conversations": conversations_touched, "participants": admitted_total}
