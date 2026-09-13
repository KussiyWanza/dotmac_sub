from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.organization import Organization
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyIdentityStatus,
    PartyRelationship,
    PartyRelationshipStatus,
    PartyRelationshipType,
)
from app.models.subscriber import Reseller, Subscriber
from app.models.team_inbox import InboxContactLink, InboxConversation
from app.services import team_inbox_participants
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)
from app.services.team_inbox_channel_receive import _normalize_contact


class ContactLinkError(DomainError, ValueError):
    def __init__(
        self,
        message: str,
        *,
        suffix: str = "command_rejected",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(
            code=f"communications.team_inbox_contact_resolution.{suffix}",
            message=message,
            details=details,
        )


class ConversationContactLinkError(ContactLinkError):
    def __init__(self, message: str = "Conversation not found.") -> None:
        super().__init__(message, suffix="conversation_not_found")


class ContactLinkTargetType(StrEnum):
    subscriber = "subscriber"
    reseller = "reseller"


class ContactLinkSource(StrEnum):
    manual_inbox_conversation = "manual_inbox_conversation"
    lead_conversion = "lead_conversion"
    reviewed_repair = "reviewed_repair"


class ContactLinkDisposition(StrEnum):
    created = "created"
    reused = "reused"
    replaced = "replaced"
    repaired = "repaired"


@dataclass(frozen=True, slots=True)
class ContactLinkTarget:
    target_type: ContactLinkTargetType
    target_id: UUID


@dataclass(frozen=True, slots=True)
class LinkConversationContactCommand:
    context: CommandContext
    conversation_id: UUID
    target: ContactLinkTarget
    actor_person_id: UUID | None
    source: ContactLinkSource
    note: str | None = None
    expected_active_link_id: UUID | None = None


class CustomerLinkOptionSource(StrEnum):
    suggested = "suggested"
    search = "search"


_INBOX_PARTY_CONTACT_CHANNELS = {
    "email": PartyContactPointType.email.value,
    "whatsapp": PartyContactPointType.whatsapp.value,
    "facebook_messenger": PartyContactPointType.facebook_messenger.value,
    "instagram_dm": PartyContactPointType.instagram_dm.value,
}

_ROUTABLE_CONTACT_RELATIONSHIPS = {
    PartyRelationshipType.contact_for.value,
    PartyRelationshipType.billing_contact_for.value,
    PartyRelationshipType.technical_contact_for.value,
    PartyRelationshipType.emergency_contact_for.value,
}


@dataclass(frozen=True)
class ContactLinkResult:
    contact_link_id: UUID
    channel_type: str
    normalized_contact: str
    subscriber_id: UUID | None
    reseller_id: UUID | None
    previous_link_ids_deactivated: tuple[UUID, ...]
    repaired_conversation_ids: tuple[UUID, ...]
    disposition: ContactLinkDisposition
    replayed: bool


@dataclass(frozen=True, slots=True)
class AssociateRepresentedCustomerCommand:
    conversation_id: UUID
    participant_id: UUID
    subscriber_id: UUID
    actor_person_id: UUID | None
    reason: str


@dataclass(frozen=True, slots=True)
class RepresentedCustomerAssociation:
    conversation_id: UUID
    participant_id: UUID
    subscriber_id: UUID
    already_linked: bool


@dataclass(frozen=True, slots=True)
class CustomerLinkOptionsQuery:
    conversation_id: UUID
    search_text: str | None = None
    limit: int = 8


@dataclass(frozen=True, slots=True)
class CustomerLinkOption:
    customer_id: UUID
    label: str
    source: CustomerLinkOptionSource


@dataclass(frozen=True, slots=True)
class CustomerLinkOptionsPage:
    items: tuple[CustomerLinkOption, ...]
    count: int
    limit: int


OWNER = "communications.team_inbox_contact_resolution"
_CONTACT_LINK_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="reviewed contact association and projection repair",
    name="execute_team_inbox_contact_link_command",
)


def _subscriber_label(row: Subscriber) -> str:
    full_name = " ".join(
        part for part in [row.first_name, row.last_name] if part
    ).strip()
    label = (
        row.display_name or row.company_name or full_name or row.email or str(row.id)
    )
    extras = [
        row.account_number,
        row.subscriber_number,
        row.email,
        row.phone,
        getattr(row.status, "value", row.status),
    ]
    suffix = " · ".join(str(item) for item in extras if item)
    return f"{label} ({suffix})" if suffix else label


def _reseller_label(row: Reseller) -> str:
    extras = [row.code, row.contact_email, row.contact_phone]
    suffix = " · ".join(str(item) for item in extras if item)
    return f"{row.name} ({suffix})" if suffix else row.name


def _organization_label(row: Organization) -> str:
    label = row.name or row.legal_name or row.domain or str(row.id)
    extras = [row.legal_name, row.domain, row.email, row.phone, row.account_status]
    suffix = " Â· ".join(str(item) for item in extras if item and item != label)
    return f"{label} ({suffix})" if suffix else label


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conversation_customer_terms(conversation: InboxConversation) -> tuple[str, ...]:
    metadata = (
        conversation.metadata_ if isinstance(conversation.metadata_, dict) else {}
    )
    values: list[object] = [
        conversation.contact_address,
        metadata.get("contact_name"),
        conversation.subject,
        conversation.external_thread_id,
    ]
    resolution = metadata.get("contact_resolution")
    if isinstance(resolution, dict):
        values.extend(
            (
                resolution.get("normalized_contact"),
                resolution.get("subscriber_id"),
            )
        )
        matched_ids = resolution.get("matched_subscriber_ids")
        if isinstance(matched_ids, list):
            values.extend(matched_ids)

    terms: list[str] = []
    for value in values:
        term = str(value or "").strip()
        if len(term) >= 3 and term not in terms:
            terms.append(term)
    return tuple(terms[:8])


def _subscriber_search_conditions(
    terms: tuple[str, ...],
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    for term in terms:
        try:
            customer_id = UUID(term)
        except ValueError:
            customer_id = None
        if customer_id is not None:
            conditions.append(Subscriber.id == customer_id)
            continue

        escaped = _escape_like(term)
        like = f"%{escaped}%"
        conditions.extend(
            [
                Subscriber.email.ilike(like, escape="\\"),
                Subscriber.phone.ilike(like, escape="\\"),
                Subscriber.first_name.ilike(like, escape="\\"),
                Subscriber.last_name.ilike(like, escape="\\"),
                Subscriber.display_name.ilike(like, escape="\\"),
                Subscriber.company_name.ilike(like, escape="\\"),
                Subscriber.legal_name.ilike(like, escape="\\"),
                Subscriber.account_number.ilike(like, escape="\\"),
                Subscriber.subscriber_number.ilike(like, escape="\\"),
            ]
        )

    if len(terms) == 1:
        words = terms[0].split()
        if len(words) >= 2:
            first = f"%{_escape_like(words[0])}%"
            remainder = f"%{_escape_like(' '.join(words[1:]))}%"
            conditions.append(
                and_(
                    Subscriber.first_name.ilike(first, escape="\\"),
                    Subscriber.last_name.ilike(remainder, escape="\\"),
                )
            )
    return conditions


def customer_link_options(
    db: Session,
    *,
    query: CustomerLinkOptionsQuery,
) -> CustomerLinkOptionsPage:
    """Return bounded Customer suggestions or a search over only entered text."""

    limit = max(1, min(query.limit, 8))
    conversation = db.get(InboxConversation, query.conversation_id)
    if conversation is None or not conversation.is_active:
        raise ConversationContactLinkError("Conversation not found.")

    if query.search_text is None:
        terms = _conversation_customer_terms(conversation)
        source = CustomerLinkOptionSource.suggested
    else:
        search_text = query.search_text.strip()
        if len(search_text) < 2:
            raise ContactLinkError("Enter at least two characters to search Customers.")
        if len(search_text) > 120:
            raise ContactLinkError("Customer search text is too long.")
        terms = (search_text,)
        source = CustomerLinkOptionSource.search

    conditions = _subscriber_search_conditions(terms)
    if not conditions:
        return CustomerLinkOptionsPage(items=(), count=0, limit=limit)

    statement = select(Subscriber).where(
        Subscriber.is_active.is_(True),
        or_(*conditions),
    )
    if source is CustomerLinkOptionSource.suggested:
        statement = statement.order_by(
            Subscriber.updated_at.desc().nullslast(),
            Subscriber.id.asc(),
        )
    else:
        statement = statement.order_by(
            Subscriber.last_name.asc(),
            Subscriber.first_name.asc(),
            Subscriber.id.asc(),
        )
    subscribers = tuple(db.scalars(statement.limit(limit)).all())
    items = tuple(
        CustomerLinkOption(
            customer_id=subscriber.id,
            label=_subscriber_label(subscriber),
            source=source,
        )
        for subscriber in subscribers
    )
    return CustomerLinkOptionsPage(items=items, count=len(items), limit=limit)


def contact_link_candidates(
    db: Session,
    terms: list[str],
) -> dict[str, list[dict[str, str]]]:
    subscribers: list[Subscriber] = []
    resellers: list[Reseller] = []
    organizations: list[Organization] = []
    if terms:
        subscriber_filters = []
        reseller_filters = []
        organization_filters = []
        for term in terms:
            like = f"%{term}%"
            subscriber_filters.extend(
                [
                    Subscriber.email.ilike(like),
                    Subscriber.phone.ilike(like),
                    Subscriber.first_name.ilike(like),
                    Subscriber.last_name.ilike(like),
                    Subscriber.display_name.ilike(like),
                    Subscriber.company_name.ilike(like),
                    Subscriber.account_number.ilike(like),
                    Subscriber.subscriber_number.ilike(like),
                ]
            )
            reseller_filters.extend(
                [
                    Reseller.name.ilike(like),
                    Reseller.code.ilike(like),
                    Reseller.contact_email.ilike(like),
                    Reseller.contact_phone.ilike(like),
                ]
            )
            organization_filters.extend(
                [
                    Organization.name.ilike(like),
                    Organization.legal_name.ilike(like),
                    Organization.domain.ilike(like),
                    Organization.email.ilike(like),
                    Organization.phone.ilike(like),
                ]
            )
        subscribers = (
            db.query(Subscriber)
            .filter(Subscriber.is_active.is_(True))
            .filter(or_(*subscriber_filters))
            .order_by(Subscriber.updated_at.desc().nullslast())
            .limit(8)
            .all()
        )
        resellers = (
            db.query(Reseller)
            .filter(Reseller.is_active.is_(True))
            .filter(or_(*reseller_filters))
            .order_by(Reseller.name.asc())
            .limit(8)
            .all()
        )
        organizations = (
            db.query(Organization)
            .filter(Organization.is_active.is_(True))
            .filter(or_(*organization_filters))
            .order_by(Organization.updated_at.desc().nullslast())
            .limit(8)
            .all()
        )
    if not subscribers:
        subscribers = (
            db.query(Subscriber)
            .filter(Subscriber.is_active.is_(True))
            .order_by(Subscriber.updated_at.desc().nullslast())
            .limit(8)
            .all()
        )
    if not resellers:
        resellers = (
            db.query(Reseller)
            .filter(Reseller.is_active.is_(True))
            .order_by(Reseller.name.asc())
            .limit(8)
            .all()
        )
    if not organizations:
        organizations = (
            db.query(Organization)
            .filter(Organization.is_active.is_(True))
            .order_by(Organization.updated_at.desc().nullslast())
            .limit(8)
            .all()
        )
    return {
        "subscribers": [
            {"id": str(row.id), "label": _subscriber_label(row)} for row in subscribers
        ],
        "resellers": [
            {"id": str(row.id), "label": _reseller_label(row)} for row in resellers
        ],
        "organizations": [
            {"id": str(row.id), "label": _organization_label(row)}
            for row in organizations
        ],
    }


def _target(
    db: Session,
    *,
    subscriber_id: str | UUID | None,
    reseller_id: str | UUID | None,
) -> tuple[Subscriber | None, Reseller | None]:
    subscriber_uuid = coerce_uuid(subscriber_id)
    reseller_uuid = coerce_uuid(reseller_id)
    if bool(subscriber_uuid) == bool(reseller_uuid):
        raise ContactLinkError("Provide exactly one of subscriber_id or reseller_id.")
    subscriber = (
        db.scalar(
            select(Subscriber).where(Subscriber.id == subscriber_uuid).with_for_update()
        )
        if subscriber_uuid
        else None
    )
    reseller = (
        db.scalar(
            select(Reseller).where(Reseller.id == reseller_uuid).with_for_update()
        )
        if reseller_uuid
        else None
    )
    if subscriber_uuid and subscriber is None:
        raise ContactLinkError("Subscriber not found.")
    if subscriber is not None and not subscriber.is_active:
        raise ContactLinkError("Cannot link an inactive Customer.")
    if reseller_uuid and reseller is None:
        raise ContactLinkError("Reseller not found.")
    if reseller is not None and not reseller.is_active:
        raise ContactLinkError("Cannot link an inactive reseller.")
    return subscriber, reseller


def associate_represented_customer(
    db: Session,
    command: AssociateRepresentedCustomerCommand,
) -> RepresentedCustomerAssociation:
    """Link only this conversation to the Customer another person represents.

    Unlike ``link_conversation_contact``, this deliberately creates no
    ``InboxContactLink`` and performs no historical repair. A representative's
    endpoint may be used for several different Customers, so treating it as a
    reusable Customer route would silently misidentify future conversations.
    """

    reason = command.reason.strip()
    if not reason:
        raise ContactLinkError("Explain why this person represents the Customer.")
    if len(reason) > 2000:
        raise ContactLinkError("Representative link reason is too long.")
    conversation = db.scalars(
        select(InboxConversation)
        .where(
            InboxConversation.id == command.conversation_id,
            InboxConversation.is_active.is_(True),
        )
        .with_for_update()
    ).one_or_none()
    if conversation is None:
        raise ConversationContactLinkError("Conversation not found.")
    subscriber = db.get(Subscriber, command.subscriber_id)
    if subscriber is None or not subscriber.is_active:
        raise ContactLinkError("Choose an active Customer.")
    if (
        conversation.subscriber_id is not None
        and conversation.subscriber_id != subscriber.id
    ):
        raise ContactLinkError(
            "This conversation is already linked to another Customer. Use the "
            "reviewed identity correction workflow."
        )

    participant = team_inbox_participants.mark_representative(
        db,
        team_inbox_participants.MarkRepresentativeCommand(
            conversation_id=conversation.id,
            participant_id=command.participant_id,
            actor_person_id=command.actor_person_id,
            source="communications.team_inbox_contact_resolution",
            reason=reason,
        ),
    )
    already_linked = conversation.subscriber_id == subscriber.id
    conversation.subscriber_id = subscriber.id
    metadata = dict(conversation.metadata_ or {})
    contact_resolution = dict(metadata.get("contact_resolution") or {})
    contact_resolution.update(
        {
            "status": "represented_customer",
            "subscriber_id": str(subscriber.id),
            "representative_participant_id": str(participant.participant_id),
            "decision_source": "reviewed_conversation_representation",
        }
    )
    metadata["contact_resolution"] = contact_resolution
    conversation.metadata_ = metadata
    db.flush()
    return RepresentedCustomerAssociation(
        conversation_id=conversation.id,
        participant_id=participant.participant_id,
        subscriber_id=subscriber.id,
        already_linked=already_linked and participant.already_classified,
    )


def bind_contact_link_party_contact_point(
    db: Session,
    *,
    contact_link_id: UUID,
    party_contact_point_id: UUID,
    source: str,
    reason: str,
) -> InboxContactLink:
    """Bind an existing Inbox route to reviewed canonical reachability.

    This shadow projection does not change the active route, target account,
    conversation resolution, verification, consent, or authorization. Current
    inbound readers continue to use channel/normalized_contact until a separate
    parity-gated cutover.
    """

    normalized_source = source.strip()
    normalized_reason = reason.strip()
    if not normalized_source:
        raise ContactLinkError("source is required")
    if not normalized_reason:
        raise ContactLinkError("reason is required")
    link = db.get(InboxContactLink, contact_link_id)
    if link is None:
        raise ContactLinkError("Inbox contact link not found.")
    point = db.get(PartyContactPoint, party_contact_point_id)
    if point is None:
        raise ContactLinkError("Party contact point not found.")
    party = db.get(Party, point.party_id)
    if party is None or party.status in {
        PartyIdentityStatus.merged.value,
        PartyIdentityStatus.archived.value,
    }:
        raise ContactLinkError("Party contact point has no routable Party.")
    if not point.is_active:
        raise ContactLinkError("Party contact point is inactive.")
    expected_channel = _INBOX_PARTY_CONTACT_CHANNELS.get(link.channel_type)
    if expected_channel is None:
        raise ContactLinkError(
            f"Inbox channel '{link.channel_type}' has no canonical contact-point "
            "projection contract."
        )
    if point.channel_type != expected_channel:
        raise ContactLinkError(
            "Party contact point channel does not match the Inbox contact link."
        )
    normalized_values = {
        value
        for value in (
            _normalize_contact(db, link.channel_type, point.normalized_value),
            _normalize_contact(db, link.channel_type, point.external_subject_id),
        )
        if value
    }
    if link.normalized_contact not in normalized_values:
        raise ContactLinkError(
            "Party contact point does not match the Inbox normalized contact."
        )
    if link.channel_type in {
        "facebook_messenger",
        "instagram_dm",
    } and not (
        (point.provider or "").strip()
        and (point.provider_account_id or "").strip()
        and (point.external_subject_id or "").strip()
    ):
        raise ContactLinkError(
            "Social Party contact point lacks immutable provider identity scope."
        )
    target_party_id = None
    if link.subscriber_id is not None:
        subscriber = db.get(Subscriber, link.subscriber_id)
        target_party_id = subscriber.party_id if subscriber is not None else None
    elif link.reseller_id is not None:
        reseller = db.get(Reseller, link.reseller_id)
        target_party_id = reseller.party_id if reseller is not None else None
    if target_party_id is None:
        raise ContactLinkError(
            "Inbox contact-link target must have a reviewed Party binding first."
        )
    target_party = db.get(Party, target_party_id)
    if target_party is None or target_party.status in {
        PartyIdentityStatus.merged.value,
        PartyIdentityStatus.archived.value,
    }:
        raise ContactLinkError("Inbox contact-link target has no routable Party.")
    if point.party_id != target_party_id:
        routed_relationship = (
            db.query(PartyRelationship.id)
            .filter(
                PartyRelationship.subject_party_id == point.party_id,
                PartyRelationship.object_party_id == target_party_id,
                PartyRelationship.relationship_type.in_(
                    _ROUTABLE_CONTACT_RELATIONSHIPS
                ),
                PartyRelationship.status == PartyRelationshipStatus.active.value,
            )
            .scalar()
        )
        if routed_relationship is None:
            raise ContactLinkError(
                "Party contact point owner has no active contact relationship to "
                "the Inbox target Party."
            )
    if link.party_contact_point_id is not None:
        if link.party_contact_point_id != point.id:
            raise ContactLinkError(
                "Inbox contact link is already bound to another Party contact "
                "point; use the reviewed merge/repoint workflow."
            )
        if not (
            link.party_contact_point_bound_at is not None
            and (link.party_contact_point_binding_source or "").strip()
            and (link.party_contact_point_binding_reason or "").strip()
        ):
            raise ContactLinkError(
                "Inbox contact link has incomplete Party contact-point evidence."
            )
        return link
    link.party_contact_point_id = point.id
    link.party_contact_point_bound_at = datetime.now(UTC)
    link.party_contact_point_binding_source = normalized_source
    link.party_contact_point_binding_reason = normalized_reason
    db.flush()
    return link


def _contact_route_lock_key(channel_type: str, normalized_contact: str) -> int:
    digest = hashlib.sha256(
        f"team-inbox-contact-link:{channel_type}:{normalized_contact}".encode()
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def lock_conversation_contact_route(
    db: Session, *, conversation_id: UUID
) -> tuple[InboxConversation, str]:
    """Lock an endpoint before its conversation so route-wide repair cannot race."""

    snapshot = db.get(InboxConversation, conversation_id)
    if snapshot is None or not snapshot.is_active:
        raise ConversationContactLinkError()
    if not snapshot.channel_type or not snapshot.contact_address:
        raise ContactLinkError("Conversation does not have a linkable contact address.")
    normalized_contact = _normalize_contact(
        db, snapshot.channel_type, snapshot.contact_address
    )
    if not normalized_contact:
        raise ContactLinkError("Conversation contact address cannot be normalized.")
    channel_type = snapshot.channel_type
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _contact_route_lock_key(channel_type, normalized_contact)},
        )
    conversation = db.scalar(
        select(InboxConversation)
        .where(
            InboxConversation.id == conversation_id,
            InboxConversation.is_active.is_(True),
        )
        .with_for_update()
    )
    if conversation is None:
        raise ConversationContactLinkError()
    locked_normalized = _normalize_contact(
        db, conversation.channel_type, conversation.contact_address or ""
    )
    if (
        conversation.channel_type != channel_type
        or locked_normalized != normalized_contact
    ):
        raise ContactLinkError(
            "The conversation contact route changed. Refresh and try again.",
            suffix="stale_contact_route",
        )
    return conversation, normalized_contact


def link_conversation_contact(
    db: Session, command: LinkConversationContactCommand
) -> ContactLinkResult:
    """Apply a reviewed endpoint association inside an active owner command."""

    if not owner_command_active(db):
        raise ContactLinkError(
            "Contact links require an active owner command.",
            suffix="owner_command_required",
        )
    conversation: InboxConversation
    normalized_contact: str
    conversation, normalized_contact = lock_conversation_contact_route(
        db,
        conversation_id=command.conversation_id,
    )
    subscriber, reseller = _target(
        db,
        subscriber_id=(
            command.target.target_id
            if command.target.target_type is ContactLinkTargetType.subscriber
            else None
        ),
        reseller_id=(
            command.target.target_id
            if command.target.target_type is ContactLinkTargetType.reseller
            else None
        ),
    )

    now = datetime.now(UTC)
    deactivated: list[UUID] = []
    active_link: InboxContactLink | None = db.scalar(
        select(InboxContactLink)
        .where(
            InboxContactLink.channel_type == conversation.channel_type,
            InboxContactLink.normalized_contact == normalized_contact,
            InboxContactLink.is_active.is_(True),
        )
        .with_for_update()
    )
    if command.expected_active_link_id is not None and (
        active_link is None or active_link.id != command.expected_active_link_id
    ):
        raise ContactLinkError(
            "The reviewed contact link changed. Refresh and try again.",
            suffix="stale_contact_link",
        )
    same_target = bool(
        active_link is not None
        and active_link.subscriber_id == (subscriber.id if subscriber else None)
        and active_link.reseller_id == (reseller.id if reseller else None)
    )
    if active_link is not None and not same_target:
        active_link.is_active = False
        metadata = dict(active_link.metadata_ or {})
        metadata["deactivated_at"] = now.isoformat()
        metadata["deactivated_by_person_id"] = (
            str(command.actor_person_id) if command.actor_person_id else None
        )
        metadata["deactivated_for_conversation_id"] = str(conversation.id)
        active_link.metadata_ = metadata
        deactivated.append(active_link.id)
        # PostgreSQL must observe the partial-index release before the
        # replacement INSERT. A single combined flush can insert first.
        db.flush()

    contact_link = active_link if same_target else None
    if contact_link is None:
        contact_link = InboxContactLink(
            channel_type=conversation.channel_type,
            normalized_contact=normalized_contact,
            subscriber_id=subscriber.id if subscriber is not None else None,
            reseller_id=reseller.id if reseller is not None else None,
            linked_by_person_id=command.actor_person_id,
            source=command.source.value,
            is_active=True,
            metadata_={
                "conversation_id": str(conversation.id),
                "note": command.note,
            },
        )
        db.add(contact_link)
        db.flush()

    prior_subscriber_id = conversation.subscriber_id
    prior_metadata = dict(conversation.metadata_ or {})
    conversation.subscriber_id = subscriber.id if subscriber is not None else None
    metadata = dict(conversation.metadata_ or {})
    contact_resolution = dict(metadata.get("contact_resolution") or {})
    linked_reseller_id = reseller.id if reseller is not None else None
    if subscriber is not None and subscriber.reseller_id is not None:
        linked_reseller_id = subscriber.reseller_id
    contact_resolution.update(
        {
            "status": "linked_subscriber" if subscriber else "linked_reseller",
            "normalized_contact": normalized_contact,
            "subscriber_id": str(subscriber.id) if subscriber else None,
            "reseller_id": str(linked_reseller_id) if linked_reseller_id else None,
            "manual_contact_link_id": str(contact_link.id),
        }
    )
    metadata["contact_resolution"] = contact_resolution
    existing_manual_link = metadata.get("manual_contact_link")
    if not (
        same_target
        and isinstance(existing_manual_link, dict)
        and existing_manual_link.get("id") == str(contact_link.id)
    ):
        metadata["manual_contact_link"] = {
            "id": str(contact_link.id),
            "linked_at": now.isoformat(),
            "linked_by_person_id": (
                str(command.actor_person_id) if command.actor_person_id else None
            ),
            "note": command.note,
        }
    conversation.metadata_ = metadata

    repaired_conversation_ids: list[UUID] = []
    if subscriber is not None:
        historical_rows = (
            db.query(InboxConversation)
            .filter(InboxConversation.id != conversation.id)
            .filter(InboxConversation.channel_type == conversation.channel_type)
            .filter(InboxConversation.contact_address == normalized_contact)
            .filter(InboxConversation.subscriber_id.is_(None))
            .filter(InboxConversation.is_active.is_(True))
            .order_by(InboxConversation.created_at.asc(), InboxConversation.id.asc())
            .with_for_update()
            .all()
        )
        for historical in historical_rows:
            historical.subscriber_id = subscriber.id
            historical_metadata = dict(historical.metadata_ or {})
            historical_resolution = dict(
                historical_metadata.get("contact_resolution") or {}
            )
            historical_resolution.update(
                {
                    "status": "linked_subscriber",
                    "normalized_contact": normalized_contact,
                    "subscriber_id": str(subscriber.id),
                    "reseller_id": str(linked_reseller_id)
                    if linked_reseller_id
                    else None,
                    "manual_contact_link_id": str(contact_link.id),
                    "repair_source_conversation_id": str(conversation.id),
                }
            )
            historical_metadata["contact_resolution"] = historical_resolution
            historical_metadata["subscriber_link_repaired_at"] = now.isoformat()
            historical.metadata_ = historical_metadata
            repaired_conversation_ids.append(historical.id)
        db.flush()

    current_changed = (
        prior_subscriber_id != conversation.subscriber_id or prior_metadata != metadata
    )
    changed = bool(
        deactivated or not same_target or repaired_conversation_ids or current_changed
    )
    if changed:
        emit_event(
            db,
            EventType.custom,
            {
                "name": "team_inbox.contact_link_changed.v1",
                "conversation_id": str(conversation.id),
                "contact_link_id": str(contact_link.id),
                "target_type": command.target.target_type.value,
                "target_id": str(command.target.target_id),
                "source": command.source.value,
                "disposition": (
                    ContactLinkDisposition.replaced.value
                    if deactivated
                    else ContactLinkDisposition.created.value
                    if not same_target
                    else ContactLinkDisposition.repaired.value
                ),
                "repaired_conversation_count": len(repaired_conversation_ids),
            },
            actor=command.context.actor,
        )
    disposition = (
        ContactLinkDisposition.replaced
        if deactivated
        else ContactLinkDisposition.created
        if not same_target
        else ContactLinkDisposition.repaired
        if changed
        else ContactLinkDisposition.reused
    )
    return ContactLinkResult(
        contact_link_id=contact_link.id,
        channel_type=contact_link.channel_type,
        normalized_contact=contact_link.normalized_contact,
        subscriber_id=contact_link.subscriber_id,
        reseller_id=contact_link.reseller_id,
        previous_link_ids_deactivated=tuple(deactivated),
        repaired_conversation_ids=tuple(repaired_conversation_ids),
        disposition=disposition,
        replayed=not changed,
    )


def link_conversation_contact_by_id(
    db: Session,
    command: LinkConversationContactCommand,
) -> ContactLinkResult:
    return link_conversation_contact(db, command)


def link_conversation_contact_by_id_committed(
    db: Session,
    command: LinkConversationContactCommand,
) -> ContactLinkResult:
    return execute_owner_command(
        db,
        definition=_CONTACT_LINK_COMMAND,
        context=command.context,
        operation=lambda: link_conversation_contact_by_id(db, command),
    )
