from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

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
from app.services.common import coerce_uuid
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.team_inbox_channel_receive import _normalize_contact


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


@dataclass(frozen=True)
class ContactLinkResult:
    contact_link_id: UUID
    channel_type: str
    normalized_contact: str
    subscriber_id: UUID | None
    reseller_id: UUID | None
    previous_link_ids_deactivated: list[UUID]


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


def link_conversation_contact(
    db: Session,
    *,
    conversation: InboxConversation,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    linked_by_person_id: str | UUID | None = None,
    note: str | None = None,
) -> ContactLinkResult:
    if not conversation.channel_type or not conversation.contact_address:
        raise ContactLinkError("Conversation does not have a linkable contact address.")
    subscriber, reseller = _target(
        db,
        subscriber_id=subscriber_id,
        reseller_id=reseller_id,
    )
    normalized_contact = _normalize_contact(
        db, conversation.channel_type, conversation.contact_address
    )
    if not normalized_contact:
        raise ContactLinkError("Conversation contact address cannot be normalized.")

    now = datetime.now(UTC)
    deactivated: list[UUID] = []
    for link in (
        db.query(InboxContactLink)
        .filter(InboxContactLink.channel_type == conversation.channel_type)
        .filter(InboxContactLink.normalized_contact == normalized_contact)
        .filter(InboxContactLink.is_active.is_(True))
        .all()
    ):
        link.is_active = False
        metadata = dict(link.metadata_ or {})
        metadata["deactivated_at"] = now.isoformat()
        metadata["deactivated_by_person_id"] = str(linked_by_person_id or "") or None
        metadata["deactivated_for_conversation_id"] = str(conversation.id)
        link.metadata_ = metadata
        deactivated.append(link.id)

    contact_link = InboxContactLink(
        channel_type=conversation.channel_type,
        normalized_contact=normalized_contact,
        subscriber_id=subscriber.id if subscriber is not None else None,
        reseller_id=reseller.id if reseller is not None else None,
        linked_by_person_id=coerce_uuid(linked_by_person_id),
        source="manual_inbox_conversation",
        is_active=True,
        metadata_={
            "conversation_id": str(conversation.id),
            "note": note,
        },
    )
    db.add(contact_link)
    db.flush()

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
        }
    )
    metadata["contact_resolution"] = contact_resolution
    metadata["manual_contact_link"] = {
        "id": str(contact_link.id),
        "linked_at": now.isoformat(),
        "linked_by_person_id": str(linked_by_person_id or "") or None,
        "note": note,
    }
    conversation.metadata_ = metadata

    return ContactLinkResult(
        contact_link_id=contact_link.id,
        channel_type=contact_link.channel_type,
        normalized_contact=contact_link.normalized_contact,
        subscriber_id=contact_link.subscriber_id,
        reseller_id=contact_link.reseller_id,
        previous_link_ids_deactivated=deactivated,
    )


def link_conversation_contact_by_id(
    db: Session,
    *,
    conversation_id: str | UUID,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    linked_by_person_id: str | UUID | None = None,
    note: str | None = None,
) -> ContactLinkResult:
    conversation_uuid = coerce_uuid(conversation_id)
    conversation = (
        db.get(InboxConversation, conversation_uuid) if conversation_uuid else None
    )
    if conversation is None or not conversation.is_active:
        raise ConversationContactLinkError("Conversation not found.")
    return link_conversation_contact(
        db,
        conversation=conversation,
        subscriber_id=subscriber_id,
        reseller_id=reseller_id,
        linked_by_person_id=linked_by_person_id,
        note=note,
    )


def link_conversation_contact_by_id_committed(
    db: Session,
    *,
    conversation_id: str | UUID,
    subscriber_id: str | UUID | None = None,
    reseller_id: str | UUID | None = None,
    linked_by_person_id: str | UUID | None = None,
    note: str | None = None,
) -> ContactLinkResult:
    return _commit(
        db,
        lambda: link_conversation_contact_by_id(
            db,
            conversation_id=conversation_id,
            subscriber_id=subscriber_id,
            reseller_id=reseller_id,
            linked_by_person_id=linked_by_person_id,
            note=note,
        ),
    )
