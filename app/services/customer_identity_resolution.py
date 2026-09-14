"""Deterministic inbound customer identity resolution."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.metrics import record_customer_identity_resolution
from app.models.comms import CustomerNotificationEvent
from app.models.communication_log import CommunicationLog
from app.models.customer_identity import CustomerIdentityIndex
from app.models.domain_settings import SettingDomain
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyIdentityStatus,
    PartyRelationship,
    PartyRelationshipStatus,
    PartyRelationshipType,
)
from app.models.subscriber import Subscriber, SubscriberChannel, SubscriberContact
from app.services.customer_identity_normalization import (
    IDENTITY_TYPE_EMAIL,
    IDENTITY_TYPE_PHONE,
    default_country_code,
    normalize_channel_address,
    normalize_email_identifier,
    normalize_identifier,
    normalize_phone_identifier,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

MATCH_VIA_SUBSCRIBER = "subscriber"
MATCH_VIA_SUBSCRIBER_CONTACT = "subscriber_contact"
MATCH_VIA_SUBSCRIBER_CHANNEL = "subscriber_channel"
MATCH_VIA_HISTORICAL_PARTICIPANT = "historical_participant"
MATCH_VIA_PARTY_CONTACT_POINT = "party_contact_point"

MATCH_CONFIDENCE_NONE = "NONE"
MATCH_CONFIDENCE_HIGH = "HIGH"
MATCH_CONFIDENCE_MEDIUM = "MEDIUM"
MATCH_CONFIDENCE_LOW = "LOW"

SOURCE_SUBSCRIBERS = "subscribers"
SOURCE_SUBSCRIBER_CONTACTS = "subscriber_contacts"
SOURCE_SUBSCRIBER_CHANNELS = "subscriber_channels"
SOURCE_COMMUNICATION_LOGS = "communication_logs"
SOURCE_CUSTOMER_NOTIFICATION_EVENTS = "customer_notification_events"
SOURCE_PARTY_CONTACT_POINTS = "party_contact_points"
IDENTITY_TYPE_PROVIDER_SUBJECT = "provider_subject"

AUTOMATION_SUPPRESSION_REASON_IDENTITY_REVIEW = "identity_manual_review_required"


@dataclass(frozen=True)
class CustomerIdentityResolution:
    raw_identifier: str | None
    normalized_identifier: str | None
    identity_type: str | None
    inbound_channel: str | None
    matched: bool
    ambiguous: bool
    subscriber_id: UUID | None = None
    customer_account_id: UUID | None = None
    matched_via: str | None = None
    matched_field: str | None = None
    matched_contact_id: UUID | None = None
    matched_channel_id: UUID | None = None
    source_table: str | None = None
    source_record_id: UUID | None = None
    matched_party_contact_point_id: UUID | None = None
    participant_party_id: UUID | None = None
    candidate_subscriber_ids: tuple[UUID, ...] = ()
    ambiguity_count: int = 0
    match_confidence: str = MATCH_CONFIDENCE_NONE

    @property
    def status(self) -> str:
        if self.matched:
            return "matched"
        if self.ambiguous:
            return "ambiguous"
        return "unmatched"

    @property
    def requires_manual_review(self) -> bool:
        return self.ambiguous or self.match_confidence == MATCH_CONFIDENCE_LOW

    @property
    def allows_sensitive_automation(self) -> bool:
        return (
            self.matched
            and not self.requires_manual_review
            and self.match_confidence
            in {MATCH_CONFIDENCE_HIGH, MATCH_CONFIDENCE_MEDIUM}
        )

    def as_metadata(self) -> dict[str, object]:
        return {
            "status": self.status,
            "raw_identifier": self.raw_identifier,
            "normalized_identifier": self.normalized_identifier,
            "identity_type": self.identity_type,
            "inbound_channel": self.inbound_channel,
            "matched_via": self.matched_via,
            "matched_field": self.matched_field,
            "matched_contact_id": str(self.matched_contact_id)
            if self.matched_contact_id
            else None,
            "matched_channel_id": str(self.matched_channel_id)
            if self.matched_channel_id
            else None,
            "matched_record_id": str(self.source_record_id)
            if self.source_record_id
            else None,
            "matched_record_source": self.source_table,
            "matched_party_contact_point_id": (
                str(self.matched_party_contact_point_id)
                if self.matched_party_contact_point_id
                else None
            ),
            "participant_party_id": (
                str(self.participant_party_id) if self.participant_party_id else None
            ),
            "candidate_subscriber_ids": [
                str(item) for item in self.candidate_subscriber_ids
            ],
            "subscriber_id": str(self.subscriber_id) if self.subscriber_id else None,
            "customer_account_id": str(self.customer_account_id)
            if self.customer_account_id
            else None,
            "ambiguous": self.ambiguous,
            "ambiguity_count": self.ambiguity_count,
            "match_confidence": self.match_confidence,
            "manual_review_required": self.requires_manual_review,
            "allows_sensitive_automation": self.allows_sensitive_automation,
        }


@dataclass(frozen=True)
class _StageMatch:
    subscriber_id: UUID
    matched_via: str
    matched_field: str
    source_table: str
    source_record_id: UUID
    match_confidence: str
    matched_contact_id: UUID | None = None
    matched_channel_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CustomerIdentityQuery:
    identifier: str | None
    channel_hint: str | None = None
    provider: str | None = None
    provider_account_id: str | None = None
    external_subject_id: str | None = None


@dataclass(frozen=True, slots=True)
class _IdentityEvidence:
    subscriber_id: UUID
    matched_via: str
    matched_field: str
    source_table: str
    source_record_id: UUID
    match_confidence: str
    matched_contact_id: UUID | None = None
    matched_channel_id: UUID | None = None
    matched_party_contact_point_id: UUID | None = None
    participant_party_id: UUID | None = None


_PROVIDER_SCOPED_CHANNELS = frozenset(
    {
        PartyContactPointType.facebook_messenger.value,
        PartyContactPointType.instagram_dm.value,
    }
)
_ROUTABLE_CONTACT_RELATIONSHIPS = frozenset(
    {
        PartyRelationshipType.contact_for.value,
        PartyRelationshipType.billing_contact_for.value,
        PartyRelationshipType.technical_contact_for.value,
        PartyRelationshipType.emergency_contact_for.value,
    }
)
_EVIDENCE_RANK = {
    MATCH_VIA_SUBSCRIBER: 0,
    MATCH_VIA_PARTY_CONTACT_POINT: 1,
    MATCH_VIA_SUBSCRIBER_CONTACT: 2,
    MATCH_VIA_SUBSCRIBER_CHANNEL: 3,
    MATCH_VIA_HISTORICAL_PARTICIPANT: 4,
}


def identity_resolution_requires_manual_review(
    resolution: CustomerIdentityResolution | dict[str, object] | None,
) -> bool:
    if isinstance(resolution, CustomerIdentityResolution):
        return resolution.requires_manual_review
    if not isinstance(resolution, dict):
        return False
    status = str(resolution.get("status") or "").strip().lower()
    confidence = str(resolution.get("match_confidence") or "").strip().upper()
    return (
        bool(resolution.get("manual_review_required"))
        or status == "ambiguous"
        or (confidence == MATCH_CONFIDENCE_LOW)
    )


def identity_resolution_allows_sensitive_automation(
    resolution: CustomerIdentityResolution | dict[str, object] | None,
    db: Session | None = None,
) -> bool:
    min_confidence = _sensitive_automation_min_confidence(db)
    allowed_confidences = {MATCH_CONFIDENCE_HIGH}
    if min_confidence == MATCH_CONFIDENCE_MEDIUM:
        allowed_confidences.add(MATCH_CONFIDENCE_MEDIUM)
    if isinstance(resolution, CustomerIdentityResolution):
        return (
            resolution.matched
            and not resolution.requires_manual_review
            and resolution.match_confidence in allowed_confidences
        )
    if not isinstance(resolution, dict):
        return False
    status = str(resolution.get("status") or "").strip().lower()
    confidence = str(resolution.get("match_confidence") or "").strip().upper()
    return (
        status == "matched"
        and not identity_resolution_requires_manual_review(resolution)
        and confidence in allowed_confidences
    )


def _sensitive_automation_min_confidence(db: Session | None = None) -> str:
    if db is None:
        return MATCH_CONFIDENCE_MEDIUM
    try:
        value = resolve_value(
            db,
            SettingDomain.subscriber,
            "identity_sensitive_automation_min_confidence",
        )
    except Exception:
        value = None
    normalized = str(value or MATCH_CONFIDENCE_MEDIUM).strip().upper()
    if normalized == MATCH_CONFIDENCE_HIGH:
        return MATCH_CONFIDENCE_HIGH
    return MATCH_CONFIDENCE_MEDIUM


def rebuild_identity_index_for_subscriber(
    db: Session,
    subscriber_id: UUID | str | None,
) -> None:
    subscriber_uuid = _coerce_uuid(subscriber_id)
    if subscriber_uuid is None:
        return
    subscriber = db.get(Subscriber, subscriber_uuid)
    if subscriber is None:
        return
    country_code = default_country_code(db)

    deleted_count = (
        db.query(CustomerIdentityIndex)
        .filter(CustomerIdentityIndex.subscriber_id == subscriber_uuid)
        .delete(synchronize_session=False)
    )

    rows: list[CustomerIdentityIndex] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    def _append_row(
        *,
        identity_type: str,
        normalized_value: str | None,
        source_table: str,
        source_field: str,
        contact_id: UUID | None = None,
        channel_id: UUID | None = None,
    ) -> None:
        if not normalized_value:
            return
        dedupe_key = (
            identity_type,
            normalized_value,
            source_table,
            source_field,
            str(contact_id or channel_id or subscriber_uuid),
        )
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        rows.append(
            CustomerIdentityIndex(
                identity_type=identity_type,
                normalized_value=normalized_value,
                subscriber_id=subscriber_uuid,
                subscriber_contact_id=contact_id,
                subscriber_channel_id=channel_id,
                source_table=source_table,
                source_field=source_field,
            )
        )

    _append_row(
        identity_type=IDENTITY_TYPE_EMAIL,
        normalized_value=normalize_email_identifier(subscriber.email),
        source_table=SOURCE_SUBSCRIBERS,
        source_field="email",
    )
    _append_row(
        identity_type=IDENTITY_TYPE_PHONE,
        normalized_value=normalize_phone_identifier(
            subscriber.phone, default_country_code=country_code
        ),
        source_table=SOURCE_SUBSCRIBERS,
        source_field="phone",
    )

    contacts = db.scalars(
        select(SubscriberContact).where(
            SubscriberContact.subscriber_id == subscriber_uuid
        )
    ).all()
    for contact in contacts:
        _append_row(
            identity_type=IDENTITY_TYPE_EMAIL,
            normalized_value=normalize_email_identifier(contact.email),
            source_table=SOURCE_SUBSCRIBER_CONTACTS,
            source_field="email",
            contact_id=contact.id,
        )
        _append_row(
            identity_type=IDENTITY_TYPE_PHONE,
            normalized_value=normalize_phone_identifier(
                contact.phone, default_country_code=country_code
            ),
            source_table=SOURCE_SUBSCRIBER_CONTACTS,
            source_field="phone",
            contact_id=contact.id,
        )
        _append_row(
            identity_type=IDENTITY_TYPE_PHONE,
            normalized_value=normalize_phone_identifier(
                contact.whatsapp, default_country_code=country_code
            ),
            source_table=SOURCE_SUBSCRIBER_CONTACTS,
            source_field="whatsapp",
            contact_id=contact.id,
        )

    channels = db.scalars(
        select(SubscriberChannel).where(
            SubscriberChannel.subscriber_id == subscriber_uuid
        )
    ).all()
    for channel in channels:
        field = str(channel.channel_type.value if channel.channel_type else "").strip()
        identity_type = (
            IDENTITY_TYPE_EMAIL if field == IDENTITY_TYPE_EMAIL else IDENTITY_TYPE_PHONE
        )
        _append_row(
            identity_type=identity_type,
            normalized_value=normalize_channel_address(
                field, channel.address, default_country_code=country_code
            ),
            source_table=SOURCE_SUBSCRIBER_CHANNELS,
            source_field=field or "address",
            channel_id=channel.id,
        )

    if rows:
        db.add_all(rows)
    db.flush()
    logger.info(
        "customer_identity_index_rebuilt stale_deleted=%s rows_inserted=%s",
        deleted_count,
        len(rows),
    )


def resolve_customer_identity(
    db: Session,
    identifier: str | None,
    *,
    channel_hint: str | None = None,
    provider: str | None = None,
    provider_account_id: str | None = None,
    external_subject_id: str | None = None,
) -> CustomerIdentityResolution:
    return resolve_customer_identity_query(
        db,
        CustomerIdentityQuery(
            identifier=identifier,
            channel_hint=channel_hint,
            provider=provider,
            provider_account_id=provider_account_id,
            external_subject_id=external_subject_id,
        ),
    )


def resolve_customer_identity_query(
    db: Session,
    query: CustomerIdentityQuery,
) -> CustomerIdentityResolution:
    """Resolve one canonical Customer identity across every current source.

    Current canonical evidence is aggregated before a decision is returned, so
    a direct Subscriber value cannot hide a conflicting contact/channel/Party
    identity. Historical communication evidence remains a final, low-confidence
    fallback and never outranks current identity facts.
    """

    raw_identifier = str(query.identifier or "").strip()
    country_code = default_country_code(db)
    inbound_channel = str(query.channel_hint or "").strip().lower() or None
    provider_scoped = inbound_channel in _PROVIDER_SCOPED_CHANNELS
    normalized = normalize_identifier(
        raw_identifier,
        query.channel_hint,
        default_country_code=country_code,
    )
    if provider_scoped:
        normalized = str(query.external_subject_id or raw_identifier).strip() or None
    identity_type = (
        IDENTITY_TYPE_PROVIDER_SUBJECT
        if provider_scoped
        else (
            IDENTITY_TYPE_EMAIL
            if inbound_channel == IDENTITY_TYPE_EMAIL or "@" in raw_identifier
            else IDENTITY_TYPE_PHONE
        )
    )
    if not normalized:
        resolution = CustomerIdentityResolution(
            raw_identifier=raw_identifier or None,
            normalized_identifier=None,
            identity_type=identity_type,
            inbound_channel=inbound_channel,
            matched=False,
            ambiguous=False,
        )
        _log_resolution(resolution)
        return resolution

    evidence = _current_identity_evidence(
        db,
        normalized=normalized,
        identity_type=identity_type,
        inbound_channel=inbound_channel,
        country_code=country_code,
        provider=(query.provider or "").strip() or None,
        provider_account_id=(query.provider_account_id or "").strip() or None,
        external_subject_id=(query.external_subject_id or "").strip() or None,
    )
    candidate_ids = tuple(sorted({item.subscriber_id for item in evidence}, key=str))
    if len(candidate_ids) > 1:
        resolution = CustomerIdentityResolution(
            raw_identifier=raw_identifier,
            normalized_identifier=normalized,
            identity_type=identity_type,
            inbound_channel=inbound_channel,
            matched=False,
            ambiguous=True,
            candidate_subscriber_ids=candidate_ids,
            ambiguity_count=len(candidate_ids),
        )
    elif len(candidate_ids) == 1:
        candidate_id = candidate_ids[0]
        selected = min(
            (item for item in evidence if item.subscriber_id == candidate_id),
            key=lambda item: (
                _EVIDENCE_RANK.get(item.matched_via, 99),
                str(item.source_record_id),
            ),
        )
        resolution = CustomerIdentityResolution(
            raw_identifier=raw_identifier,
            normalized_identifier=normalized,
            identity_type=identity_type,
            inbound_channel=inbound_channel,
            matched=True,
            ambiguous=False,
            subscriber_id=selected.subscriber_id,
            customer_account_id=selected.subscriber_id,
            matched_via=selected.matched_via,
            matched_field=selected.matched_field,
            matched_contact_id=selected.matched_contact_id,
            matched_channel_id=selected.matched_channel_id,
            source_table=selected.source_table,
            source_record_id=selected.source_record_id,
            matched_party_contact_point_id=selected.matched_party_contact_point_id,
            participant_party_id=selected.participant_party_id,
            candidate_subscriber_ids=candidate_ids,
            match_confidence=selected.match_confidence,
        )
    elif provider_scoped:
        resolution = CustomerIdentityResolution(
            raw_identifier=raw_identifier,
            normalized_identifier=normalized,
            identity_type=identity_type,
            inbound_channel=inbound_channel,
            matched=False,
            ambiguous=False,
        )
    elif identity_type == IDENTITY_TYPE_EMAIL:
        resolution = _resolve_historical_identity(
            raw_identifier=raw_identifier,
            normalized=normalized,
            identity_type=identity_type,
            inbound_channel=inbound_channel,
            stage=_resolve_historical_email(db, normalized),
        )
    else:
        resolution = _resolve_historical_identity(
            raw_identifier=raw_identifier,
            normalized=normalized,
            identity_type=identity_type,
            inbound_channel=inbound_channel,
            stage=_resolve_historical_phone(
                db,
                normalized,
                country_code=country_code,
            ),
        )

    _log_resolution(resolution)
    return resolution


def _resolve_historical_identity(
    *,
    raw_identifier: str,
    normalized: str,
    identity_type: str,
    inbound_channel: str | None,
    stage: tuple[_StageMatch | None, int],
) -> CustomerIdentityResolution:
    resolved = _finalize_stage(
        raw_identifier,
        normalized,
        identity_type,
        inbound_channel,
        stage,
    )
    return resolved or CustomerIdentityResolution(
        raw_identifier=raw_identifier,
        normalized_identifier=normalized,
        identity_type=identity_type,
        inbound_channel=inbound_channel,
        matched=False,
        ambiguous=False,
    )


def _current_identity_evidence(
    db: Session,
    *,
    normalized: str,
    identity_type: str,
    inbound_channel: str | None,
    country_code: str,
    provider: str | None,
    provider_account_id: str | None,
    external_subject_id: str | None,
) -> tuple[_IdentityEvidence, ...]:
    evidence: list[_IdentityEvidence] = []

    if inbound_channel in _PROVIDER_SCOPED_CHANNELS:
        if not (provider and provider_account_id and external_subject_id):
            return ()
        points = db.scalars(
            select(PartyContactPoint).where(
                PartyContactPoint.channel_type == inbound_channel,
                PartyContactPoint.provider == provider,
                PartyContactPoint.provider_account_id == provider_account_id,
                PartyContactPoint.external_subject_id == external_subject_id,
                PartyContactPoint.is_active.is_(True),
                PartyContactPoint.verification_status
                == PartyContactVerificationStatus.verified.value,
            )
        ).all()
        for point in points:
            evidence.extend(_party_contact_point_evidence(db, point))
        return _dedupe_evidence(evidence)

    index_rows = db.scalars(
        select(CustomerIdentityIndex).where(
            CustomerIdentityIndex.identity_type == identity_type,
            CustomerIdentityIndex.normalized_value == normalized,
        )
    ).all()
    for row in index_rows:
        matched_via = {
            SOURCE_SUBSCRIBERS: MATCH_VIA_SUBSCRIBER,
            SOURCE_SUBSCRIBER_CONTACTS: MATCH_VIA_SUBSCRIBER_CONTACT,
            SOURCE_SUBSCRIBER_CHANNELS: MATCH_VIA_SUBSCRIBER_CHANNEL,
        }.get(row.source_table)
        if matched_via is None:
            continue
        evidence.append(
            _IdentityEvidence(
                subscriber_id=row.subscriber_id,
                matched_via=matched_via,
                matched_field=row.source_field,
                source_table=row.source_table,
                source_record_id=(
                    row.subscriber_contact_id
                    or row.subscriber_channel_id
                    or row.subscriber_id
                ),
                match_confidence=_match_confidence_for_index_row(db, row, matched_via),
                matched_contact_id=row.subscriber_contact_id,
                matched_channel_id=row.subscriber_channel_id,
            )
        )

    subscribers: Sequence[Subscriber]
    contacts: Sequence[SubscriberContact]
    channels: Sequence[SubscriberChannel]
    point_channels: tuple[str, ...]
    if identity_type == IDENTITY_TYPE_EMAIL:
        subscribers = db.scalars(
            select(Subscriber).where(
                func.lower(func.trim(Subscriber.email)) == normalized
            )
        ).all()
        contacts = db.scalars(
            select(SubscriberContact).where(
                func.lower(func.trim(SubscriberContact.email)) == normalized
            )
        ).all()
        channels = db.scalars(
            select(SubscriberChannel).where(
                SubscriberChannel.channel_type == IDENTITY_TYPE_EMAIL,
                func.lower(func.trim(SubscriberChannel.address)) == normalized,
            )
        ).all()
        point_channels = (PartyContactPointType.email.value,)
    else:
        subscribers = [
            row
            for row in db.scalars(
                select(Subscriber).where(Subscriber.phone.is_not(None))
            ).all()
            if normalize_phone_identifier(row.phone, default_country_code=country_code)
            == normalized
        ]
        contacts = [
            row
            for row in db.scalars(
                select(SubscriberContact).where(
                    or_(
                        SubscriberContact.phone.is_not(None),
                        SubscriberContact.whatsapp.is_not(None),
                    )
                )
            ).all()
            if normalize_phone_identifier(row.phone, default_country_code=country_code)
            == normalized
            or normalize_phone_identifier(
                row.whatsapp, default_country_code=country_code
            )
            == normalized
        ]
        channels = [
            row
            for row in db.scalars(
                select(SubscriberChannel).where(
                    SubscriberChannel.address.is_not(None),
                    SubscriberChannel.channel_type.in_(
                        (IDENTITY_TYPE_PHONE, "sms", "whatsapp")
                    ),
                )
            ).all()
            if normalize_phone_identifier(
                row.address, default_country_code=country_code
            )
            == normalized
        ]
        point_channels = (
            PartyContactPointType.phone.value,
            PartyContactPointType.sms.value,
            PartyContactPointType.whatsapp.value,
        )

    for subscriber in subscribers:
        evidence.append(
            _IdentityEvidence(
                subscriber_id=subscriber.id,
                matched_via=MATCH_VIA_SUBSCRIBER,
                matched_field=identity_type,
                source_table=SOURCE_SUBSCRIBERS,
                source_record_id=subscriber.id,
                match_confidence=MATCH_CONFIDENCE_HIGH,
                participant_party_id=subscriber.party_id,
            )
        )
    for contact in contacts:
        matched_field = "email"
        if identity_type == IDENTITY_TYPE_PHONE:
            matched_field = (
                "phone"
                if normalize_phone_identifier(
                    contact.phone, default_country_code=country_code
                )
                == normalized
                else "whatsapp"
            )
        evidence.append(
            _IdentityEvidence(
                subscriber_id=contact.subscriber_id,
                matched_via=MATCH_VIA_SUBSCRIBER_CONTACT,
                matched_field=matched_field,
                source_table=SOURCE_SUBSCRIBER_CONTACTS,
                source_record_id=contact.id,
                match_confidence=MATCH_CONFIDENCE_MEDIUM,
                matched_contact_id=contact.id,
                participant_party_id=contact.person_party_id,
            )
        )
    for channel in channels:
        channel_value = str(
            channel.channel_type.value if channel.channel_type else identity_type
        )
        evidence.append(
            _IdentityEvidence(
                subscriber_id=channel.subscriber_id,
                matched_via=MATCH_VIA_SUBSCRIBER_CHANNEL,
                matched_field=channel_value,
                source_table=SOURCE_SUBSCRIBER_CHANNELS,
                source_record_id=channel.id,
                match_confidence=(
                    MATCH_CONFIDENCE_HIGH
                    if channel.is_verified
                    else MATCH_CONFIDENCE_MEDIUM
                ),
                matched_channel_id=channel.id,
            )
        )

    points = db.scalars(
        select(PartyContactPoint).where(
            PartyContactPoint.channel_type.in_(point_channels),
            PartyContactPoint.is_active.is_(True),
            PartyContactPoint.verification_status
            == PartyContactVerificationStatus.verified.value,
        )
    ).all()
    for point in points:
        candidate_value = (
            point.normalized_value.casefold()
            if identity_type == IDENTITY_TYPE_EMAIL
            else normalize_phone_identifier(
                point.normalized_value, default_country_code=country_code
            )
        )
        if candidate_value == normalized:
            evidence.extend(_party_contact_point_evidence(db, point))
    return _dedupe_evidence(evidence)


def _party_contact_point_evidence(
    db: Session,
    point: PartyContactPoint,
) -> tuple[_IdentityEvidence, ...]:
    party = db.get(Party, point.party_id)
    if party is None or party.status in {
        PartyIdentityStatus.merged.value,
        PartyIdentityStatus.archived.value,
    }:
        return ()
    subscriber_ids = set(
        db.scalars(select(Subscriber.id).where(Subscriber.party_id == point.party_id))
    )
    represented_party_ids = set(
        db.scalars(
            select(PartyRelationship.object_party_id).where(
                PartyRelationship.subject_party_id == point.party_id,
                PartyRelationship.relationship_type.in_(
                    _ROUTABLE_CONTACT_RELATIONSHIPS
                ),
                PartyRelationship.status == PartyRelationshipStatus.active.value,
            )
        )
    )
    if represented_party_ids:
        subscriber_ids.update(
            db.scalars(
                select(Subscriber.id).where(
                    Subscriber.party_id.in_(represented_party_ids)
                )
            )
        )
    return tuple(
        _IdentityEvidence(
            subscriber_id=subscriber_id,
            matched_via=MATCH_VIA_PARTY_CONTACT_POINT,
            matched_field=point.channel_type,
            source_table=SOURCE_PARTY_CONTACT_POINTS,
            source_record_id=point.id,
            match_confidence=MATCH_CONFIDENCE_HIGH,
            matched_party_contact_point_id=point.id,
            participant_party_id=point.party_id,
        )
        for subscriber_id in sorted(subscriber_ids, key=str)
    )


def _dedupe_evidence(
    evidence: Iterable[_IdentityEvidence],
) -> tuple[_IdentityEvidence, ...]:
    unique: dict[tuple[UUID, str, UUID], _IdentityEvidence] = {}
    for item in evidence:
        unique[(item.subscriber_id, item.source_table, item.source_record_id)] = item
    return tuple(unique.values())


def _finalize_stage(
    raw_identifier: str,
    normalized: str,
    identity_type: str,
    inbound_channel: str | None,
    stage: tuple[_StageMatch | None, int] | None,
) -> CustomerIdentityResolution | None:
    if stage is None:
        return None
    match, ambiguity_count = stage
    if match is None and ambiguity_count <= 0:
        return None
    if match is None:
        return CustomerIdentityResolution(
            raw_identifier=raw_identifier,
            normalized_identifier=normalized,
            identity_type=identity_type,
            inbound_channel=inbound_channel,
            matched=False,
            ambiguous=True,
            ambiguity_count=ambiguity_count,
        )
    return CustomerIdentityResolution(
        raw_identifier=raw_identifier,
        normalized_identifier=normalized,
        identity_type=identity_type,
        inbound_channel=inbound_channel,
        matched=True,
        ambiguous=False,
        subscriber_id=match.subscriber_id,
        customer_account_id=match.subscriber_id,
        matched_via=match.matched_via,
        matched_field=match.matched_field,
        matched_contact_id=match.matched_contact_id,
        matched_channel_id=match.matched_channel_id,
        source_table=match.source_table,
        source_record_id=match.source_record_id,
        match_confidence=match.match_confidence,
    )


def _match_confidence_for_index_row(
    db: Session,
    row: CustomerIdentityIndex,
    matched_via: str,
) -> str:
    if matched_via == MATCH_VIA_SUBSCRIBER:
        return MATCH_CONFIDENCE_HIGH
    if matched_via == MATCH_VIA_SUBSCRIBER_CONTACT:
        return MATCH_CONFIDENCE_MEDIUM
    if matched_via == MATCH_VIA_SUBSCRIBER_CHANNEL:
        channel = (
            db.get(SubscriberChannel, row.subscriber_channel_id)
            if row.subscriber_channel_id
            else None
        )
        return (
            MATCH_CONFIDENCE_HIGH
            if channel is not None and channel.is_verified
            else MATCH_CONFIDENCE_MEDIUM
        )
    return MATCH_CONFIDENCE_LOW


def _resolve_historical_email(
    db: Session, normalized_value: str
) -> tuple[_StageMatch | None, int]:
    event_rows = {
        subscriber_id
        for subscriber_id in db.scalars(
            select(CustomerNotificationEvent.subscriber_id).where(
                CustomerNotificationEvent.subscriber_id.is_not(None),
                func.lower(CustomerNotificationEvent.recipient) == normalized_value,
            )
        ).all()
        if subscriber_id is not None
    }
    log_rows = {
        subscriber_id
        for subscriber_id in db.scalars(
            select(CommunicationLog.subscriber_id).where(
                CommunicationLog.subscriber_id.is_not(None),
                or_(
                    func.lower(CommunicationLog.recipient) == normalized_value,
                    func.lower(CommunicationLog.sender) == normalized_value,
                ),
            )
        ).all()
        if subscriber_id is not None
    }
    if log_rows:
        return _collapse_historical_rows(
            log_rows, field="email", source_table=SOURCE_COMMUNICATION_LOGS
        )
    return _collapse_historical_rows(
        event_rows,
        field="email",
        source_table=SOURCE_CUSTOMER_NOTIFICATION_EVENTS,
    )


def _resolve_historical_phone(
    db: Session, normalized_value: str, *, country_code: str
) -> tuple[_StageMatch | None, int]:
    event_subscribers: set[UUID | None] = set()
    for event_row in db.scalars(
        select(CustomerNotificationEvent).where(
            CustomerNotificationEvent.subscriber_id.is_not(None)
        )
    ).all():
        if (
            normalize_phone_identifier(
                event_row.recipient, default_country_code=country_code
            )
            == normalized_value
        ):
            event_subscribers.add(event_row.subscriber_id)

    log_subscribers: set[UUID | None] = set()
    for log_row in db.scalars(
        select(CommunicationLog).where(CommunicationLog.subscriber_id.is_not(None))
    ).all():
        if (
            normalize_phone_identifier(
                log_row.recipient, default_country_code=country_code
            )
            == normalized_value
            or normalize_phone_identifier(
                log_row.sender, default_country_code=country_code
            )
            == normalized_value
        ):
            log_subscribers.add(log_row.subscriber_id)

    if log_subscribers:
        return _collapse_historical_rows(
            log_subscribers,
            field="phone",
            source_table=SOURCE_COMMUNICATION_LOGS,
        )
    return _collapse_historical_rows(
        event_subscribers,
        field="phone",
        source_table=SOURCE_CUSTOMER_NOTIFICATION_EVENTS,
    )


def _collapse_historical_rows(
    subscriber_ids: Iterable[UUID | None],
    *,
    field: str,
    source_table: str,
) -> tuple[_StageMatch | None, int]:
    unique_subscribers = {
        subscriber_id for subscriber_id in subscriber_ids if subscriber_id
    }
    if not unique_subscribers:
        return (None, 0)
    if len(unique_subscribers) > 1:
        return (None, len(unique_subscribers))
    subscriber_id = next(iter(unique_subscribers))
    return (
        _StageMatch(
            subscriber_id=subscriber_id,
            matched_via=MATCH_VIA_HISTORICAL_PARTICIPANT,
            matched_field=field,
            source_table=source_table,
            source_record_id=subscriber_id,
            match_confidence=MATCH_CONFIDENCE_LOW,
        ),
        1,
    )


def _coerce_uuid(value: UUID | str | None) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _log_resolution(resolution: CustomerIdentityResolution) -> None:
    result = resolution.status
    record_customer_identity_resolution(
        result=result,
        identity_type=resolution.identity_type,
        match_source=resolution.matched_via,
        confidence=resolution.match_confidence,
        inbound_channel=resolution.inbound_channel,
    )
    if resolution.matched:
        logger.info(
            "customer_identity_resolved identity_type=%s inbound_channel=%s "
            "matched_via=%s matched_field=%s matched_record_source=%s "
            "confidence=%s ambiguous=%s",
            resolution.identity_type,
            resolution.inbound_channel,
            resolution.matched_via,
            resolution.matched_field,
            resolution.source_table,
            resolution.match_confidence,
            resolution.ambiguous,
        )
        return
    level = logger.warning if resolution.ambiguous else logger.info
    level(
        "customer_identity_unresolved identity_type=%s inbound_channel=%s "
        "ambiguous=%s ambiguity_count=%s confidence=%s",
        resolution.identity_type,
        resolution.inbound_channel,
        resolution.ambiguous,
        resolution.ambiguity_count,
        resolution.match_confidence,
    )
    if resolution.ambiguous:
        logger.warning(
            "customer_identity_ambiguous_identifier identity_type=%s "
            "ambiguity_count=%s inbound_channel=%s",
            resolution.identity_type,
            resolution.ambiguity_count,
            resolution.inbound_channel,
        )
