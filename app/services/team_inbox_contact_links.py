from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.organization import Organization
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyIdentityStatus,
    PartyRelationship,
    PartyRelationshipStatus,
    PartyRelationshipType,
    PartyType,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxContactLink,
    InboxConversation,
    InboxMessage,
    InboxMessageDirection,
    InboxParticipantRelationship,
)
from app.services import party as party_service
from app.services import team_inbox_participants
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_channel_address,
    normalize_customer_name,
)
from app.services.customer_identity_resolution import (
    CustomerIdentityQuery,
    CustomerIdentityResolution,
    resolve_customer_identity_query,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


class ContactLinkError(ValueError):
    pass


class ConversationContactLinkError(ContactLinkError):
    pass


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
_PROVIDER_SCOPED_CHANNELS = frozenset(
    {
        PartyContactPointType.facebook_messenger.value,
        PartyContactPointType.instagram_dm.value,
    }
)
_INACTIVE_SUBSCRIBER_STATUSES = frozenset(
    {SubscriberStatus.disabled.value, SubscriberStatus.canceled.value}
)


class ContactResolutionStatus(StrEnum):
    explicit_subscriber = "explicit_subscriber"
    linked_subscriber = "linked_subscriber"
    linked_reseller = "linked_reseller"
    ambiguous = "ambiguous"
    suppressed_inactive = "suppressed_inactive"
    unmatched = "unmatched"


class AutomaticCustomerLinkStatus(StrEnum):
    linked = "linked"
    already_linked = "already_linked"
    conflict = "conflict"
    unresolved = "unresolved"


class ReviewedContactIdentityKind(StrEnum):
    customer = "customer"
    representative = "representative"
    reseller = "reseller"


class ReviewedContactLinkDisposition(StrEnum):
    linked = "linked"
    replayed = "replayed"
    conflict = "conflict"


@dataclass(frozen=True, slots=True)
class ContactResolutionQuery:
    channel_type: str
    contact_address: str
    subscriber_id: UUID | None = None
    contact_name: str | None = None
    provider: str | None = None
    provider_account_id: str | None = None
    external_subject_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContactResolution:
    status: ContactResolutionStatus
    normalized_contact: str | None
    subscriber_id: UUID | None
    reseller_id: UUID | None
    matched_subscriber_ids: tuple[UUID, ...]
    suppressed_subscriber_ids: tuple[UUID, ...]
    matched_reseller_ids: tuple[UUID, ...]
    matched_via: str | None = None
    source_table: str | None = None
    source_record_id: UUID | None = None
    party_contact_point_id: UUID | None = None
    participant_party_id: UUID | None = None
    name_tiebreaker_used: bool = False

    def as_metadata(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "normalized_contact": self.normalized_contact,
            "subscriber_id": str(self.subscriber_id) if self.subscriber_id else None,
            "reseller_id": str(self.reseller_id) if self.reseller_id else None,
            "matched_subscriber_ids": [
                str(item) for item in self.matched_subscriber_ids
            ],
            "suppressed_subscriber_ids": [
                str(item) for item in self.suppressed_subscriber_ids
            ],
            "matched_reseller_ids": [str(item) for item in self.matched_reseller_ids],
            "matched_via": self.matched_via,
            "matched_record_source": self.source_table,
            "matched_record_id": (
                str(self.source_record_id) if self.source_record_id else None
            ),
            "matched_party_contact_point_id": (
                str(self.party_contact_point_id)
                if self.party_contact_point_id
                else None
            ),
            "participant_party_id": (
                str(self.participant_party_id) if self.participant_party_id else None
            ),
            "name_tiebreaker_used": self.name_tiebreaker_used,
        }


@dataclass(frozen=True, slots=True)
class ResolveConversationCustomerCommand:
    conversation_id: UUID
    reason: str
    contact_name: str | None = None
    expected_subscriber_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ApplyResolvedCustomerCommand:
    conversation_id: UUID
    resolution: ContactResolution
    reason: str


@dataclass(frozen=True, slots=True)
class ResolveConversationCustomerOutcome:
    status: AutomaticCustomerLinkStatus
    resolution: ContactResolution
    subscriber_id: UUID | None
    previous_subscriber_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RepairConversationCustomerCommand:
    context: CommandContext
    conversation_id: UUID
    reason: str
    expected_subscriber_id: UUID


@dataclass(frozen=True, slots=True)
class ReviewConversationContactCommand:
    conversation_id: UUID
    identity_kind: ReviewedContactIdentityKind
    subscriber_id: UUID | None = None
    reseller_id: UUID | None = None
    representative_party_id: UUID | None = None
    representative_name: str | None = None
    representative_role: str | None = None
    actor_person_id: UUID | None = None
    note: str | None = None


@dataclass(frozen=True)
class ContactLinkResult:
    disposition: ReviewedContactLinkDisposition
    contact_link_id: UUID
    channel_type: str
    normalized_contact: str
    subscriber_id: UUID | None
    reseller_id: UUID | None
    party_contact_point_id: UUID | None
    speaking_party_id: UUID | None
    previous_link_ids_deactivated: tuple[UUID, ...]
    repaired_conversation_ids: tuple[UUID, ...]


T = TypeVar("T")
OWNER = "communications.team_inbox_contact_resolution"
_CONTACT_LINK_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="reviewed contact association and projection repair",
    name="execute_team_inbox_contact_link_command",
)


def _commit(db: Session, action: Callable[[], T]) -> T:
    return execute_owner_command(
        db,
        definition=_CONTACT_LINK_COMMAND,
        context=CommandContext.system(
            actor="system:team-inbox-contact-adapter",
            scope="team-inbox:contact-link-command",
            reason="execute reviewed Team Inbox contact association",
        ),
        operation=action,
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


def _status_value(subscriber: Subscriber) -> str:
    return str(getattr(subscriber.status, "value", subscriber.status) or "")


def _subscriber_is_linkable(subscriber: Subscriber) -> bool:
    return (
        bool(subscriber.is_active)
        and _status_value(subscriber) not in _INACTIVE_SUBSCRIBER_STATUSES
    )


def normalize_contact_address(
    db: Session,
    channel_type: str,
    value: str | None,
) -> str | None:
    if channel_type in _PROVIDER_SCOPED_CHANNELS:
        normalized = str(value or "").strip()
        return normalized or None
    return normalize_channel_address(
        channel_type,
        value,
        default_country_code=default_country_code(db),
    )


def _provider_scope(
    query: ContactResolutionQuery,
) -> tuple[str | None, str | None, str | None]:
    provider = (query.provider or "").strip() or None
    provider_account_id = (query.provider_account_id or "").strip() or None
    external_subject_id = (query.external_subject_id or "").strip() or None
    if query.channel_type in _PROVIDER_SCOPED_CHANNELS:
        external_subject_id = (
            external_subject_id or query.contact_address.strip() or None
        )
    return provider, provider_account_id, external_subject_id


def conversation_provider_identity(
    db: Session,
    conversation: InboxConversation,
) -> tuple[str | None, str | None, str | None]:
    metadata = dict(conversation.metadata_ or {})
    identity = metadata.get("provider_identity")
    identity_data = identity if isinstance(identity, dict) else {}
    provider = str(identity_data.get("provider") or "").strip() or None
    provider_account_id = (
        str(identity_data.get("provider_account_id") or "").strip() or None
    )
    external_subject_id = (
        str(identity_data.get("external_subject_id") or "").strip() or None
    )
    if conversation.channel_type not in _PROVIDER_SCOPED_CHANNELS:
        return provider, provider_account_id, external_subject_id
    if provider and provider_account_id and external_subject_id:
        return provider, provider_account_id, external_subject_id
    latest = (
        db.query(InboxMessage)
        .filter(
            InboxMessage.conversation_id == conversation.id,
            InboxMessage.direction == "inbound",
        )
        .order_by(InboxMessage.received_at.desc(), InboxMessage.id.desc())
        .first()
    )
    message_metadata = dict(latest.metadata_ or {}) if latest is not None else {}
    provider = str(message_metadata.get("provider") or "").strip() or None
    provider_account_id = (
        str(
            message_metadata.get("provider_account_scope")
            or message_metadata.get("provider_account_id")
            or message_metadata.get("page_id")
            or message_metadata.get("instagram_account_id")
            or ""
        ).strip()
        or None
    )
    external_subject_id = str(conversation.contact_address or "").strip() or None
    return provider, provider_account_id, external_subject_id


def _active_contact_links(
    db: Session,
    *,
    query: ContactResolutionQuery,
    normalized_contact: str,
) -> list[InboxContactLink]:
    rows = (
        db.query(InboxContactLink)
        .filter(InboxContactLink.channel_type == query.channel_type)
        .filter(InboxContactLink.normalized_contact == normalized_contact)
        .filter(InboxContactLink.is_active.is_(True))
    )
    if query.channel_type in _PROVIDER_SCOPED_CHANNELS:
        provider, provider_account_id, external_subject_id = _provider_scope(query)
        if not (provider and provider_account_id and external_subject_id):
            return []
        rows = rows.filter(
            InboxContactLink.provider == provider,
            InboxContactLink.provider_account_id == provider_account_id,
            InboxContactLink.external_subject_id == external_subject_id,
        )
    return rows.order_by(InboxContactLink.created_at.asc()).all()


def _canonical_resolution(
    db: Session,
    query: ContactResolutionQuery,
) -> CustomerIdentityResolution:
    provider, provider_account_id, external_subject_id = _provider_scope(query)
    return resolve_customer_identity_query(
        db,
        CustomerIdentityQuery(
            identifier=query.contact_address,
            channel_hint=query.channel_type,
            provider=provider,
            provider_account_id=provider_account_id,
            external_subject_id=external_subject_id,
        ),
    )


def resolve_contact_context(
    db: Session,
    query: ContactResolutionQuery,
) -> ContactResolution:
    """Resolve an Inbox endpoint without treating display names as identity.

    Reviewed endpoint links win. Canonical phone/email/provider identity is
    next. A provider-observed name is consulted only when multiple otherwise
    eligible Customers remain and can narrow that set to exactly one.
    """

    normalized = normalize_contact_address(
        db, query.channel_type, query.contact_address
    )
    if query.subscriber_id is not None:
        subscriber = db.get(Subscriber, query.subscriber_id)
        if subscriber is not None and _subscriber_is_linkable(subscriber):
            return ContactResolution(
                status=ContactResolutionStatus.explicit_subscriber,
                normalized_contact=normalized,
                subscriber_id=subscriber.id,
                reseller_id=subscriber.reseller_id,
                matched_subscriber_ids=(subscriber.id,),
                suppressed_subscriber_ids=(),
                matched_reseller_ids=(subscriber.reseller_id,)
                if subscriber.reseller_id
                else (),
                matched_via="explicit_subscriber",
                source_table="subscribers",
                source_record_id=subscriber.id,
                participant_party_id=subscriber.party_id,
            )
        return ContactResolution(
            status=(
                ContactResolutionStatus.suppressed_inactive
                if subscriber is not None
                else ContactResolutionStatus.unmatched
            ),
            normalized_contact=normalized,
            subscriber_id=None,
            reseller_id=None,
            matched_subscriber_ids=(),
            suppressed_subscriber_ids=(subscriber.id,) if subscriber else (),
            matched_reseller_ids=(),
        )

    if normalized:
        links = _active_contact_links(
            db,
            query=query,
            normalized_contact=normalized,
        )
        if len(links) > 1:
            return ContactResolution(
                status=ContactResolutionStatus.ambiguous,
                normalized_contact=normalized,
                subscriber_id=None,
                reseller_id=None,
                matched_subscriber_ids=tuple(
                    sorted(
                        {row.subscriber_id for row in links if row.subscriber_id},
                        key=str,
                    )
                ),
                suppressed_subscriber_ids=(),
                matched_reseller_ids=tuple(
                    sorted(
                        {row.reseller_id for row in links if row.reseller_id},
                        key=str,
                    )
                ),
                matched_via="reviewed_inbox_contact_link",
                source_table="inbox_contact_links",
            )
        if links:
            link = links[0]
            subscriber = (
                db.get(Subscriber, link.subscriber_id)
                if link.subscriber_id is not None
                else None
            )
            if subscriber is not None and _subscriber_is_linkable(subscriber):
                point = (
                    db.get(PartyContactPoint, link.party_contact_point_id)
                    if link.party_contact_point_id
                    else None
                )
                return ContactResolution(
                    status=ContactResolutionStatus.linked_subscriber,
                    normalized_contact=normalized,
                    subscriber_id=subscriber.id,
                    reseller_id=subscriber.reseller_id,
                    matched_subscriber_ids=(subscriber.id,),
                    suppressed_subscriber_ids=(),
                    matched_reseller_ids=(subscriber.reseller_id,)
                    if subscriber.reseller_id
                    else (),
                    matched_via="reviewed_inbox_contact_link",
                    source_table="inbox_contact_links",
                    source_record_id=link.id,
                    party_contact_point_id=link.party_contact_point_id,
                    participant_party_id=point.party_id
                    if point
                    else subscriber.party_id,
                )
            if subscriber is not None:
                return ContactResolution(
                    status=ContactResolutionStatus.suppressed_inactive,
                    normalized_contact=normalized,
                    subscriber_id=None,
                    reseller_id=None,
                    matched_subscriber_ids=(),
                    suppressed_subscriber_ids=(subscriber.id,),
                    matched_reseller_ids=(),
                    matched_via="reviewed_inbox_contact_link",
                    source_table="inbox_contact_links",
                    source_record_id=link.id,
                )
            reseller = (
                db.get(Reseller, link.reseller_id)
                if link.reseller_id is not None
                else None
            )
            if reseller is not None and reseller.is_active:
                return ContactResolution(
                    status=ContactResolutionStatus.linked_reseller,
                    normalized_contact=normalized,
                    subscriber_id=None,
                    reseller_id=reseller.id,
                    matched_subscriber_ids=(),
                    suppressed_subscriber_ids=(),
                    matched_reseller_ids=(reseller.id,),
                    matched_via="reviewed_inbox_contact_link",
                    source_table="inbox_contact_links",
                    source_record_id=link.id,
                    party_contact_point_id=link.party_contact_point_id,
                    participant_party_id=reseller.party_id,
                )

    canonical = _canonical_resolution(db, query)
    if canonical.matched and canonical.requires_manual_review:
        return ContactResolution(
            status=ContactResolutionStatus.ambiguous,
            normalized_contact=normalized,
            subscriber_id=None,
            reseller_id=None,
            matched_subscriber_ids=(canonical.subscriber_id,)
            if canonical.subscriber_id
            else (),
            suppressed_subscriber_ids=(),
            matched_reseller_ids=(),
            matched_via=canonical.matched_via,
            source_table=canonical.source_table,
            source_record_id=canonical.source_record_id,
        )
    candidate_ids = canonical.candidate_subscriber_ids
    if not candidate_ids and canonical.subscriber_id is not None:
        candidate_ids = (canonical.subscriber_id,)
    active: list[Subscriber] = []
    suppressed: list[Subscriber] = []
    for subscriber_id in candidate_ids:
        subscriber = db.get(Subscriber, subscriber_id)
        if subscriber is None:
            continue
        if _subscriber_is_linkable(subscriber):
            active.append(subscriber)
        else:
            suppressed.append(subscriber)

    name_tiebreaker_used = False
    if len(active) > 1:
        observed_name = normalize_customer_name(query.contact_name)
        if observed_name:
            narrowed = [
                subscriber
                for subscriber in active
                if observed_name
                in {
                    normalize_customer_name(subscriber.display_name),
                    normalize_customer_name(subscriber.full_name),
                }
            ]
            if len(narrowed) == 1:
                active = narrowed
                name_tiebreaker_used = True

    if len(active) == 1:
        selected = active[0]
        canonical_selected = canonical.subscriber_id == selected.id
        return ContactResolution(
            status=ContactResolutionStatus.linked_subscriber,
            normalized_contact=normalized,
            subscriber_id=selected.id,
            reseller_id=selected.reseller_id,
            matched_subscriber_ids=(selected.id,),
            suppressed_subscriber_ids=tuple(item.id for item in suppressed),
            matched_reseller_ids=(selected.reseller_id,)
            if selected.reseller_id
            else (),
            matched_via=(
                "canonical_name_tiebreaker"
                if name_tiebreaker_used
                else canonical.matched_via or "canonical_identity"
            ),
            source_table=canonical.source_table if canonical_selected else None,
            source_record_id=(
                canonical.source_record_id if canonical_selected else None
            ),
            party_contact_point_id=(
                canonical.matched_party_contact_point_id if canonical_selected else None
            ),
            participant_party_id=(
                canonical.participant_party_id
                if canonical_selected
                else selected.party_id
            ),
            name_tiebreaker_used=name_tiebreaker_used,
        )
    if len(active) > 1:
        return ContactResolution(
            status=ContactResolutionStatus.ambiguous,
            normalized_contact=normalized,
            subscriber_id=None,
            reseller_id=None,
            matched_subscriber_ids=tuple(sorted((item.id for item in active), key=str)),
            suppressed_subscriber_ids=tuple(item.id for item in suppressed),
            matched_reseller_ids=(),
            matched_via="canonical_identity",
        )
    if suppressed:
        return ContactResolution(
            status=ContactResolutionStatus.suppressed_inactive,
            normalized_contact=normalized,
            subscriber_id=None,
            reseller_id=None,
            matched_subscriber_ids=(),
            suppressed_subscriber_ids=tuple(item.id for item in suppressed),
            matched_reseller_ids=(),
            matched_via=canonical.matched_via,
            source_table=canonical.source_table,
            source_record_id=canonical.source_record_id,
        )
    if canonical.ambiguous:
        return ContactResolution(
            status=ContactResolutionStatus.ambiguous,
            normalized_contact=normalized,
            subscriber_id=None,
            reseller_id=None,
            matched_subscriber_ids=canonical.candidate_subscriber_ids,
            suppressed_subscriber_ids=(),
            matched_reseller_ids=(),
            matched_via=canonical.matched_via or "canonical_identity",
            source_table=canonical.source_table,
            source_record_id=canonical.source_record_id,
        )

    matched_resellers: list[Reseller] = []
    if normalized and query.channel_type not in _PROVIDER_SCOPED_CHANNELS:
        for reseller in db.query(Reseller).filter(Reseller.is_active.is_(True)).all():
            value = (
                reseller.contact_email
                if query.channel_type == "email"
                else reseller.contact_phone
            )
            if normalize_contact_address(db, query.channel_type, value) == normalized:
                matched_resellers.append(reseller)
    if len(matched_resellers) == 1:
        reseller = matched_resellers[0]
        return ContactResolution(
            status=ContactResolutionStatus.linked_reseller,
            normalized_contact=normalized,
            subscriber_id=None,
            reseller_id=reseller.id,
            matched_subscriber_ids=(),
            suppressed_subscriber_ids=(),
            matched_reseller_ids=(reseller.id,),
            matched_via="reseller_contact",
            source_table="resellers",
            source_record_id=reseller.id,
            participant_party_id=reseller.party_id,
        )
    if len(matched_resellers) > 1:
        return ContactResolution(
            status=ContactResolutionStatus.ambiguous,
            normalized_contact=normalized,
            subscriber_id=None,
            reseller_id=None,
            matched_subscriber_ids=(),
            suppressed_subscriber_ids=(),
            matched_reseller_ids=tuple(item.id for item in matched_resellers),
            matched_via="reseller_contact",
        )
    return ContactResolution(
        status=ContactResolutionStatus.unmatched,
        normalized_contact=normalized,
        subscriber_id=None,
        reseller_id=None,
        matched_subscriber_ids=(),
        suppressed_subscriber_ids=(),
        matched_reseller_ids=(),
    )


def resolve_and_link_conversation_customer(
    db: Session,
    command: ResolveConversationCustomerCommand,
) -> ResolveConversationCustomerOutcome:
    """Resolve and persist the authoritative Customer link without committing."""

    conversation = (
        db.query(InboxConversation)
        .filter(InboxConversation.id == command.conversation_id)
        .with_for_update()
        .one_or_none()
    )
    if conversation is None or not conversation.is_active:
        raise ConversationContactLinkError("Conversation not found.")
    provider, provider_account_id, external_subject_id = conversation_provider_identity(
        db, conversation
    )
    metadata = dict(conversation.metadata_ or {})
    observed_name = command.contact_name or str(metadata.get("contact_name") or "")
    resolution = resolve_contact_context(
        db,
        ContactResolutionQuery(
            channel_type=conversation.channel_type,
            contact_address=conversation.contact_address or "",
            contact_name=observed_name or None,
            provider=provider,
            provider_account_id=provider_account_id,
            external_subject_id=external_subject_id,
        ),
    )
    if (
        command.expected_subscriber_id is not None
        and resolution.subscriber_id != command.expected_subscriber_id
    ):
        return ResolveConversationCustomerOutcome(
            status=AutomaticCustomerLinkStatus.conflict,
            resolution=resolution,
            subscriber_id=conversation.subscriber_id,
            previous_subscriber_id=conversation.subscriber_id,
        )
    return apply_resolved_customer_link(
        db,
        ApplyResolvedCustomerCommand(
            conversation_id=conversation.id,
            resolution=resolution,
            reason=command.reason,
        ),
    )


def apply_resolved_customer_link(
    db: Session,
    command: ApplyResolvedCustomerCommand,
) -> ResolveConversationCustomerOutcome:
    """Persist a precomputed resolution, preserving an existing association."""

    reason = command.reason.strip()
    if not reason:
        raise ContactLinkError("Automatic Customer link reason is required.")
    conversation = (
        db.query(InboxConversation)
        .filter(InboxConversation.id == command.conversation_id)
        .with_for_update()
        .one_or_none()
    )
    if conversation is None or not conversation.is_active:
        raise ConversationContactLinkError("Conversation not found.")
    resolution = command.resolution
    metadata = dict(conversation.metadata_ or {})
    prior_subscriber_id = conversation.subscriber_id
    outcome_status = AutomaticCustomerLinkStatus.unresolved
    if prior_subscriber_id is not None:
        if (
            resolution.subscriber_id is None
            or prior_subscriber_id == resolution.subscriber_id
        ):
            outcome_status = AutomaticCustomerLinkStatus.already_linked
        else:
            outcome_status = AutomaticCustomerLinkStatus.conflict
    elif resolution.subscriber_id is not None:
        if prior_subscriber_id is None:
            conversation.subscriber_id = resolution.subscriber_id
            outcome_status = AutomaticCustomerLinkStatus.linked

    resolution_metadata = resolution.as_metadata()
    resolution_metadata["application_status"] = outcome_status.value
    resolution_metadata["application_reason"] = reason
    if outcome_status is AutomaticCustomerLinkStatus.conflict:
        resolution_metadata["existing_subscriber_id"] = str(prior_subscriber_id)
        resolution_metadata["proposed_subscriber_id"] = str(resolution.subscriber_id)
    if prior_subscriber_id is not None and (
        resolution.subscriber_id is None
        or resolution.subscriber_id != prior_subscriber_id
    ):
        metadata["latest_contact_resolution_observation"] = resolution_metadata
    else:
        metadata["contact_resolution"] = resolution_metadata
    if outcome_status is AutomaticCustomerLinkStatus.linked:
        metadata["automatic_customer_link"] = {
            "subscriber_id": str(resolution.subscriber_id),
            "matched_via": resolution.matched_via,
            "source_table": resolution.source_table,
            "source_record_id": (
                str(resolution.source_record_id)
                if resolution.source_record_id
                else None
            ),
            "party_contact_point_id": (
                str(resolution.party_contact_point_id)
                if resolution.party_contact_point_id
                else None
            ),
            "reason": reason,
            "linked_at": datetime.now(UTC).isoformat(),
        }
    conversation.metadata_ = metadata
    if outcome_status is AutomaticCustomerLinkStatus.linked:
        customer = db.get(Subscriber, resolution.subscriber_id)
        stage_audit_event(
            db,
            action="inbox_contact_identity_decided",
            entity_type="inbox_conversation",
            entity_id=str(conversation.id),
            actor_type=AuditActorType.service,
            actor_id=OWNER,
            metadata={
                "decision_source": resolution.matched_via or "canonical_identity",
                "resolution_status": resolution.status.value,
                "selected_customer_id": str(resolution.subscriber_id),
                "selected_party_id": (
                    str(customer.party_id)
                    if customer is not None and customer.party_id is not None
                    else None
                ),
                "reason": reason,
            },
        )
    db.flush()
    return ResolveConversationCustomerOutcome(
        status=outcome_status,
        resolution=resolution,
        subscriber_id=conversation.subscriber_id,
        previous_subscriber_id=prior_subscriber_id,
    )


def repair_conversation_customer_committed(
    db: Session,
    command: RepairConversationCustomerCommand,
) -> ResolveConversationCustomerOutcome:
    return execute_owner_command(
        db,
        definition=_CONTACT_LINK_COMMAND,
        context=command.context,
        operation=lambda: resolve_and_link_conversation_customer(
            db,
            ResolveConversationCustomerCommand(
                conversation_id=command.conversation_id,
                reason=command.reason,
                expected_subscriber_id=command.expected_subscriber_id,
            ),
        ),
    )


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
    subscriber = db.get(Subscriber, subscriber_uuid) if subscriber_uuid else None
    reseller = db.get(Reseller, reseller_uuid) if reseller_uuid else None
    if subscriber_uuid and subscriber is None:
        raise ContactLinkError("Subscriber not found.")
    if reseller_uuid and reseller is None:
        raise ContactLinkError("Reseller not found.")
    if reseller is not None and not reseller.is_active:
        raise ContactLinkError("Cannot link an inactive reseller.")
    return subscriber, reseller


def bind_contact_link_party_contact_point(
    db: Session,
    *,
    contact_link_id: UUID,
    party_contact_point_id: UUID,
    source: str,
    reason: str,
) -> InboxContactLink:
    """Bind an existing Inbox route to reviewed canonical reachability.

    This projection does not change the active route, target account,
    verification, consent, or authorization. The identity reader may reuse the
    reviewed exact contact point and provider scope; routing and thread
    admission remain separate decisions.
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
            normalize_contact_address(db, link.channel_type, point.normalized_value),
            normalize_contact_address(db, link.channel_type, point.external_subject_id),
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
    if link.channel_type in _PROVIDER_SCOPED_CHANNELS:
        link_scope = (
            (link.provider or "").strip() or None,
            (link.provider_account_id or "").strip() or None,
            (link.external_subject_id or "").strip() or None,
        )
        point_scope = (
            (point.provider or "").strip() or None,
            (point.provider_account_id or "").strip() or None,
            (point.external_subject_id or "").strip() or None,
        )
        if any(link_scope) and link_scope != point_scope:
            raise ContactLinkError(
                "Party contact point provider identity does not match the Inbox link."
            )
        if not any(link_scope):
            link.provider, link.provider_account_id, link.external_subject_id = (
                point_scope
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


def _reviewed_representative_party(
    db: Session,
    *,
    customer_party_id: UUID,
    representative_party_id: UUID | None,
    representative_name: str | None,
    representative_role: str | None,
    actor_person_id: UUID | None,
) -> Party:
    clean_name = " ".join(str(representative_name or "").split())
    representative = (
        db.get(Party, representative_party_id) if representative_party_id else None
    )
    if representative_party_id is not None and representative is None:
        raise ContactLinkError("The selected representative was not found.")
    if representative is None:
        normalized_name = normalize_customer_name(clean_name)
        if not normalized_name:
            raise ContactLinkError("Enter the representative's name.")
        related = (
            db.query(Party)
            .join(
                PartyRelationship,
                PartyRelationship.subject_party_id == Party.id,
            )
            .filter(
                PartyRelationship.object_party_id == customer_party_id,
                PartyRelationship.relationship_type.in_(
                    _ROUTABLE_CONTACT_RELATIONSHIPS
                ),
                PartyRelationship.status == PartyRelationshipStatus.active.value,
                Party.status == PartyIdentityStatus.active.value,
                Party.party_type == PartyType.person.value,
            )
            .with_for_update()
            .all()
        )
        exact = [
            party
            for party in related
            if normalize_customer_name(party.display_name) == normalized_name
        ]
        if len(exact) > 1:
            raise ContactLinkError(
                "More than one representative has that name; select the exact Party."
            )
        if exact:
            representative = exact[0]
        else:
            representative = party_service.create_party(
                db,
                party_type=PartyType.person,
                display_name=clean_name,
                metadata={
                    "created_by": OWNER,
                    "identity_resolution_method": "manual_review",
                    "reviewed_by_person_id": (
                        str(actor_person_id) if actor_person_id else None
                    ),
                },
            )
    if representative.party_type != PartyType.person.value:
        raise ContactLinkError("A representative must be a Person Party.")
    if representative.id == customer_party_id:
        raise ContactLinkError(
            "The Customer Party cannot also be its own representative."
        )
    relationship = (
        db.query(PartyRelationship)
        .filter(
            PartyRelationship.subject_party_id == representative.id,
            PartyRelationship.object_party_id == customer_party_id,
            PartyRelationship.relationship_type
            == PartyRelationshipType.contact_for.value,
            PartyRelationship.relationship_key == "default",
        )
        .with_for_update()
        .one_or_none()
    )
    now = datetime.now(UTC)
    if relationship is None:
        relationship = party_service.relate_parties(
            db,
            subject_party_id=representative.id,
            object_party_id=customer_party_id,
            relationship_type=PartyRelationshipType.contact_for,
            status=PartyRelationshipStatus.active,
            source=OWNER,
            metadata={
                "representative_role": (
                    " ".join(str(representative_role or "").split()) or None
                ),
                "reviewed_by_person_id": (
                    str(actor_person_id) if actor_person_id else None
                ),
                "reviewed_at": now.isoformat(),
            },
        )
    elif relationship.status != PartyRelationshipStatus.active.value:
        raise ContactLinkError(
            "This representative relationship is inactive and requires separate review."
        )
    return representative


def _manual_contact_point(
    db: Session,
    *,
    conversation: InboxConversation,
    normalized_contact: str,
    party_id: UUID,
    actor_person_id: UUID | None,
    provider: str | None,
    provider_account_id: str | None,
    external_subject_id: str | None,
    note: str | None,
) -> party_service.EnsureReviewedContactIdentityOutcome | None:
    contact_type = _INBOX_PARTY_CONTACT_CHANNELS.get(conversation.channel_type)
    if contact_type is None:
        return None
    social = conversation.channel_type in _PROVIDER_SCOPED_CHANNELS
    metadata = dict(conversation.metadata_ or {})
    observed_name = " ".join(str(metadata.get("contact_name") or "").split())
    display_value = (
        observed_name
        if social and observed_name and observed_name != normalized_contact
        else str(conversation.contact_address or normalized_contact)
    )
    try:
        return party_service.ensure_reviewed_contact_identity(
            db,
            party_service.EnsureReviewedContactIdentityCommand(
                party_id=party_id,
                channel_type=PartyContactPointType(contact_type),
                normalized_value=normalized_contact,
                display_value=display_value,
                scope_key=(
                    f"{provider}:{provider_account_id}" if social else "default"
                ),
                provider=provider if social else None,
                provider_account_id=provider_account_id if social else None,
                external_subject_id=external_subject_id if social else None,
                actor_person_id=actor_person_id,
                source=OWNER,
                reason=note or "Reviewed Team Inbox Identify Contact decision",
            ),
        )
    except party_service.PartyInvariantError as exc:
        raise ContactLinkError(str(exc)) from exc


def _bind_reviewed_participant(
    db: Session,
    *,
    conversation: InboxConversation,
    normalized_contact: str,
    provider_account_id: str | None,
    party_contact_point_id: UUID | None,
    relationship_type: InboxParticipantRelationship,
) -> None:
    if party_contact_point_id is None:
        return
    message = (
        db.query(InboxMessage)
        .filter(
            InboxMessage.conversation_id == conversation.id,
            InboxMessage.direction == InboxMessageDirection.inbound.value,
        )
        .order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc())
        .first()
    )
    if message is None:
        return
    team_inbox_participants.record_message_participants(
        db,
        conversation=conversation,
        message=message,
    )
    try:
        team_inbox_participants.bind_endpoint_to_contact_point(
            db,
            team_inbox_participants.BindEndpointContactPointCommand(
                conversation_id=conversation.id,
                channel_type=conversation.channel_type,
                normalized_endpoint=normalized_contact,
                provider_account_scope=provider_account_id or "default",
                party_contact_point_id=party_contact_point_id,
                relationship_type=relationship_type,
                source=OWNER,
                reason="Reviewed Team Inbox Identify Contact decision",
            ),
        )
    except ValueError as exc:
        raise ContactLinkError(str(exc)) from exc


def _speaking_party_conflict_result(
    db: Session,
    *,
    conversation: InboxConversation,
    contact_link: InboxContactLink,
    existing_point: PartyContactPoint,
    command: ReviewConversationContactCommand,
    selected_subscriber_id: UUID | None,
    provider: str | None,
    provider_account_id: str | None,
    external_subject_id: str | None,
    normalized_contact: str,
    proposed_speaking_party_id: UUID | None,
) -> ContactLinkResult:
    now = datetime.now(UTC)
    metadata = dict(conversation.metadata_ or {})
    metadata["identity_review_conflict"] = {
        "status": "review_required",
        "observed_at": now.isoformat(),
        "existing_contact_link_id": str(contact_link.id),
        "existing_speaking_party_id": str(existing_point.party_id),
        "proposed_speaking_party_id": (
            str(proposed_speaking_party_id) if proposed_speaking_party_id else None
        ),
        "proposed_identity_kind": command.identity_kind.value,
        "provider": provider,
        "provider_account_id": provider_account_id,
    }
    conversation.metadata_ = metadata
    stage_audit_event(
        db,
        action="inbox_contact_identity_decided",
        entity_type="inbox_conversation",
        entity_id=str(conversation.id),
        actor_type=(
            AuditActorType.user
            if command.actor_person_id is not None
            else AuditActorType.service
        ),
        actor_id=str(command.actor_person_id) if command.actor_person_id else OWNER,
        metadata={
            "decision_source": "reviewed_inbox_selection",
            "resolution_status": "speaking_party_conflict",
            "selected_customer_id": (
                str(selected_subscriber_id) if selected_subscriber_id else None
            ),
            "existing_speaking_party_id": str(existing_point.party_id),
            "proposed_speaking_party_id": (
                str(proposed_speaking_party_id) if proposed_speaking_party_id else None
            ),
            "proposed_identity_kind": command.identity_kind.value,
            "provider": provider,
            "provider_account_id": provider_account_id,
            "external_subject_id": external_subject_id,
        },
    )
    db.flush()
    return ContactLinkResult(
        disposition=ReviewedContactLinkDisposition.conflict,
        contact_link_id=contact_link.id,
        channel_type=conversation.channel_type,
        normalized_contact=normalized_contact,
        subscriber_id=contact_link.subscriber_id,
        reseller_id=contact_link.reseller_id,
        party_contact_point_id=contact_link.party_contact_point_id,
        speaking_party_id=existing_point.party_id,
        previous_link_ids_deactivated=(),
        repaired_conversation_ids=(),
    )


def link_conversation_contact(
    db: Session,
    command: ReviewConversationContactCommand,
) -> ContactLinkResult:
    conversation = (
        db.query(InboxConversation)
        .filter(InboxConversation.id == command.conversation_id)
        .with_for_update()
        .one_or_none()
    )
    if conversation is None or not conversation.is_active:
        raise ConversationContactLinkError("Conversation not found.")
    if not conversation.channel_type or not conversation.contact_address:
        raise ContactLinkError("Conversation does not have a linkable contact address.")
    subscriber, reseller = _target(
        db,
        subscriber_id=command.subscriber_id,
        reseller_id=command.reseller_id,
    )
    if command.identity_kind is ReviewedContactIdentityKind.reseller:
        if reseller is None or subscriber is not None:
            raise ContactLinkError("Choose one active reseller.")
    elif subscriber is None or reseller is not None:
        raise ContactLinkError("Choose one active Customer.")
    if subscriber is not None and not _subscriber_is_linkable(subscriber):
        raise ContactLinkError("Cannot link an inactive Customer.")
    if (
        subscriber is not None
        and conversation.subscriber_id is not None
        and conversation.subscriber_id != subscriber.id
    ):
        raise ContactLinkError(
            "Conversation is already linked to a different Customer; use the "
            "reviewed identity-conflict workflow."
        )
    normalized_contact = normalize_contact_address(
        db, conversation.channel_type, conversation.contact_address
    )
    if not normalized_contact:
        raise ContactLinkError("Conversation contact address cannot be normalized.")
    provider, provider_account_id, external_subject_id = conversation_provider_identity(
        db, conversation
    )
    if conversation.channel_type in _PROVIDER_SCOPED_CHANNELS and not (
        provider and provider_account_id and external_subject_id
    ):
        raise ContactLinkError(
            "Provider-scoped social identity is incomplete; provider, account, "
            "and external subject are required."
        )

    now = datetime.now(UTC)
    active_links = (
        db.query(InboxContactLink)
        .filter(InboxContactLink.channel_type == conversation.channel_type)
        .filter(InboxContactLink.normalized_contact == normalized_contact)
        .filter(InboxContactLink.is_active.is_(True))
    )
    if conversation.channel_type in _PROVIDER_SCOPED_CHANNELS:
        active_links = active_links.filter(
            InboxContactLink.provider == provider,
            InboxContactLink.provider_account_id == provider_account_id,
            InboxContactLink.external_subject_id == external_subject_id,
        )
    links = active_links.with_for_update().all()
    selected_subscriber_id = subscriber.id if subscriber is not None else None
    selected_reseller_id = reseller.id if reseller is not None else None
    conflicts = [
        link
        for link in links
        if link.subscriber_id != selected_subscriber_id
        or link.reseller_id != selected_reseller_id
    ]
    if conflicts:
        metadata = dict(conversation.metadata_ or {})
        metadata["identity_review_conflict"] = {
            "status": "review_required",
            "observed_at": now.isoformat(),
            "selected_customer_id": (
                str(selected_subscriber_id) if selected_subscriber_id else None
            ),
            "selected_reseller_id": (
                str(selected_reseller_id) if selected_reseller_id else None
            ),
            "existing_contact_link_ids": [str(link.id) for link in conflicts],
            "provider": provider,
            "provider_account_id": provider_account_id,
        }
        conversation.metadata_ = metadata
        stage_audit_event(
            db,
            action="inbox_contact_identity_decided",
            entity_type="inbox_conversation",
            entity_id=str(conversation.id),
            actor_type=(
                AuditActorType.user
                if command.actor_person_id is not None
                else AuditActorType.service
            ),
            actor_id=(
                str(command.actor_person_id) if command.actor_person_id else OWNER
            ),
            metadata={
                "decision_source": "reviewed_inbox_selection",
                "resolution_status": "conflict",
                "selected_customer_id": (
                    str(selected_subscriber_id) if selected_subscriber_id else None
                ),
                "selected_reseller_id": (
                    str(selected_reseller_id) if selected_reseller_id else None
                ),
                "provider": provider,
                "provider_account_id": provider_account_id,
                "external_subject_id": external_subject_id,
            },
        )
        db.flush()
        existing = conflicts[0]
        return ContactLinkResult(
            disposition=ReviewedContactLinkDisposition.conflict,
            contact_link_id=existing.id,
            channel_type=conversation.channel_type,
            normalized_contact=normalized_contact,
            subscriber_id=existing.subscriber_id,
            reseller_id=existing.reseller_id,
            party_contact_point_id=existing.party_contact_point_id,
            speaking_party_id=None,
            previous_link_ids_deactivated=(),
            repaired_conversation_ids=(),
        )

    target_party_id = None
    if subscriber is not None:
        if subscriber.party_id is None:
            raise ContactLinkError(
                "The selected Customer has no canonical Party identity."
            )
        target_party_id = subscriber.party_id
    elif reseller is not None:
        if reseller.party_id is None:
            raise ContactLinkError(
                "The selected reseller has no canonical Party identity."
            )
        target_party_id = reseller.party_id
    assert target_party_id is not None
    contact_link = links[0] if links else None
    existing_point = (
        db.get(PartyContactPoint, contact_link.party_contact_point_id)
        if contact_link is not None and contact_link.party_contact_point_id is not None
        else None
    )
    speaking_party = db.get(Party, target_party_id)
    relationship_type = InboxParticipantRelationship.customer
    if command.identity_kind is ReviewedContactIdentityKind.representative:
        representative_party_id = command.representative_party_id
        if existing_point is not None:
            assert contact_link is not None
            existing_speaker = db.get(Party, existing_point.party_id)
            proposed_name = normalize_customer_name(command.representative_name)
            existing_name = normalize_customer_name(
                existing_speaker.display_name if existing_speaker is not None else None
            )
            same_reviewed_speaker = (
                representative_party_id == existing_point.party_id
                if representative_party_id is not None
                else bool(proposed_name and proposed_name == existing_name)
            )
            if not same_reviewed_speaker:
                return _speaking_party_conflict_result(
                    db,
                    conversation=conversation,
                    contact_link=contact_link,
                    existing_point=existing_point,
                    command=command,
                    selected_subscriber_id=selected_subscriber_id,
                    provider=provider,
                    provider_account_id=provider_account_id,
                    external_subject_id=external_subject_id,
                    normalized_contact=normalized_contact,
                    proposed_speaking_party_id=representative_party_id,
                )
            representative_party_id = existing_point.party_id
        speaking_party = _reviewed_representative_party(
            db,
            customer_party_id=target_party_id,
            representative_party_id=representative_party_id,
            representative_name=command.representative_name,
            representative_role=command.representative_role,
            actor_person_id=command.actor_person_id,
        )
        relationship_type = InboxParticipantRelationship.contact
    if speaking_party is None:
        raise ContactLinkError("The selected contact has no canonical Party identity.")

    if existing_point is not None and existing_point.party_id != speaking_party.id:
        assert contact_link is not None
        return _speaking_party_conflict_result(
            db,
            conversation=conversation,
            contact_link=contact_link,
            existing_point=existing_point,
            command=command,
            selected_subscriber_id=selected_subscriber_id,
            provider=provider,
            provider_account_id=provider_account_id,
            external_subject_id=external_subject_id,
            normalized_contact=normalized_contact,
            proposed_speaking_party_id=speaking_party.id,
        )

    point_outcome = _manual_contact_point(
        db,
        conversation=conversation,
        normalized_contact=normalized_contact,
        party_id=speaking_party.id,
        actor_person_id=command.actor_person_id,
        provider=provider,
        provider_account_id=provider_account_id,
        external_subject_id=external_subject_id,
        note=command.note,
    )
    point_id = (
        point_outcome.party_contact_point_id if point_outcome is not None else None
    )
    replayed = contact_link is not None
    if contact_link is not None and contact_link.party_contact_point_id not in {
        None,
        point_id,
    }:
        raise ContactLinkError(
            "This reviewed identity link is bound to a different speaking Party; "
            "use the identity-conflict workflow."
        )
    if contact_link is None:
        contact_link = InboxContactLink(
            channel_type=conversation.channel_type,
            normalized_contact=normalized_contact,
            provider=provider,
            provider_account_id=provider_account_id,
            external_subject_id=external_subject_id,
            subscriber_id=selected_subscriber_id,
            reseller_id=selected_reseller_id,
            linked_by_person_id=command.actor_person_id,
            source="manual_inbox_conversation",
            is_active=True,
            metadata_={
                "conversation_id": str(conversation.id),
                "note": command.note,
                "provider": provider,
                "provider_account_id": provider_account_id,
                "external_subject_id": external_subject_id,
                "identity_kind": command.identity_kind.value,
                "speaking_party_id": str(speaking_party.id),
                "reviewed_at": now.isoformat(),
            },
        )
        db.add(contact_link)
        db.flush()
    if point_id is not None:
        bind_contact_link_party_contact_point(
            db,
            contact_link_id=contact_link.id,
            party_contact_point_id=point_id,
            source=OWNER,
            reason="Reviewed Team Inbox Identify Contact decision",
        )
        _bind_reviewed_participant(
            db,
            conversation=conversation,
            normalized_contact=normalized_contact,
            provider_account_id=provider_account_id,
            party_contact_point_id=point_id,
            relationship_type=relationship_type,
        )

    if subscriber is not None:
        conversation.subscriber_id = subscriber.id
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
            "party_contact_point_id": str(point_id) if point_id else None,
            "participant_party_id": str(speaking_party.id),
            "identity_kind": command.identity_kind.value,
        }
    )
    metadata["contact_resolution"] = contact_resolution
    metadata["manual_contact_link"] = {
        "id": str(contact_link.id),
        "linked_at": now.isoformat(),
        "linked_by_person_id": (
            str(command.actor_person_id) if command.actor_person_id else None
        ),
        "note": command.note,
        "identity_kind": command.identity_kind.value,
        "party_contact_point_id": str(point_id) if point_id else None,
        "speaking_party_id": str(speaking_party.id),
        "provider": provider,
        "provider_account_id": provider_account_id,
        "external_subject_id": external_subject_id,
    }
    conversation.metadata_ = metadata

    repaired_conversation_ids: list[UUID] = []
    if subscriber is not None:
        historical_rows = (
            db.query(InboxConversation)
            .filter(InboxConversation.id != conversation.id)
            .filter(InboxConversation.channel_type == conversation.channel_type)
            .filter(InboxConversation.subscriber_id.is_(None))
            .filter(InboxConversation.contact_address.isnot(None))
            .filter(InboxConversation.is_active.is_(True))
            .order_by(InboxConversation.created_at.asc(), InboxConversation.id.asc())
            .with_for_update()
            .all()
        )
        for historical in historical_rows:
            if historical.channel_type in _PROVIDER_SCOPED_CHANNELS:
                historical_scope = conversation_provider_identity(db, historical)
                if historical_scope != (
                    provider,
                    provider_account_id,
                    external_subject_id,
                ):
                    continue
            historical_normalized = normalize_contact_address(
                db,
                historical.channel_type,
                historical.contact_address or "",
            )
            if historical_normalized != normalized_contact:
                continue
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

    stage_audit_event(
        db,
        action="inbox_contact_identity_decided",
        entity_type="inbox_conversation",
        entity_id=str(conversation.id),
        actor_type=(
            AuditActorType.user
            if command.actor_person_id is not None
            else AuditActorType.service
        ),
        actor_id=(str(command.actor_person_id) if command.actor_person_id else OWNER),
        metadata={
            "decision_source": "reviewed_inbox_selection",
            "resolution_status": "replayed" if replayed else "linked",
            "selected_customer_id": (
                str(selected_subscriber_id) if selected_subscriber_id else None
            ),
            "selected_reseller_id": (
                str(selected_reseller_id) if selected_reseller_id else None
            ),
            "selected_party_id": str(target_party_id),
            "speaking_party_id": str(speaking_party.id),
            "identity_kind": command.identity_kind.value,
            "party_contact_point_id": str(point_id) if point_id else None,
            "provider": provider,
            "provider_account_id": provider_account_id,
            "external_subject_id": external_subject_id,
        },
    )
    db.flush()
    return ContactLinkResult(
        disposition=(
            ReviewedContactLinkDisposition.replayed
            if replayed
            else ReviewedContactLinkDisposition.linked
        ),
        contact_link_id=contact_link.id,
        channel_type=contact_link.channel_type,
        normalized_contact=contact_link.normalized_contact,
        subscriber_id=contact_link.subscriber_id,
        reseller_id=contact_link.reseller_id,
        party_contact_point_id=point_id,
        speaking_party_id=speaking_party.id,
        previous_link_ids_deactivated=(),
        repaired_conversation_ids=tuple(repaired_conversation_ids),
    )


def link_conversation_contact_by_id(
    db: Session,
    command: ReviewConversationContactCommand,
) -> ContactLinkResult:
    return link_conversation_contact(db, command)


def link_conversation_contact_by_id_committed(
    db: Session,
    command: ReviewConversationContactCommand,
) -> ContactLinkResult:
    return _commit(
        db,
        lambda: link_conversation_contact_by_id(db, command),
    )
