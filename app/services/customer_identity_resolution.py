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

MATCH_CONFIDENCE_NONE = "NONE"
MATCH_CONFIDENCE_HIGH = "HIGH"
MATCH_CONFIDENCE_MEDIUM = "MEDIUM"
MATCH_CONFIDENCE_LOW = "LOW"

SOURCE_SUBSCRIBERS = "subscribers"
SOURCE_SUBSCRIBER_CONTACTS = "subscriber_contacts"
SOURCE_SUBSCRIBER_CHANNELS = "subscriber_channels"
SOURCE_COMMUNICATION_LOGS = "communication_logs"
SOURCE_CUSTOMER_NOTIFICATION_EVENTS = "customer_notification_events"

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
) -> CustomerIdentityResolution:
    raw_identifier = str(identifier or "").strip()
    country_code = default_country_code(db)
    normalized = normalize_identifier(
        raw_identifier,
        channel_hint,
        default_country_code=country_code,
    )
    inbound_channel = str(channel_hint or "").strip().lower() or None
    identity_type = (
        IDENTITY_TYPE_EMAIL
        if inbound_channel == IDENTITY_TYPE_EMAIL or "@" in raw_identifier
        else IDENTITY_TYPE_PHONE
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

    if identity_type == IDENTITY_TYPE_EMAIL:
        resolution = _resolve_email_identity(
            db,
            raw_identifier,
            normalized,
            inbound_channel=inbound_channel,
        )
    else:
        resolution = _resolve_phone_identity(
            db,
            raw_identifier,
            normalized,
            inbound_channel=inbound_channel,
            country_code=country_code,
        )

    _log_resolution(resolution)
    return resolution


def _resolve_email_identity(
    db: Session,
    raw_identifier: str,
    normalized: str,
    *,
    inbound_channel: str | None,
) -> CustomerIdentityResolution:
    # Authoritative current identities always win. Historical participant linkage
    # is intentionally evaluated last and stays LOW confidence only.
    for resolver in (
        lambda: _resolve_index_stage(
            db,
            identity_type=IDENTITY_TYPE_EMAIL,
            normalized_value=normalized,
            source_table=SOURCE_SUBSCRIBERS,
            matched_via=MATCH_VIA_SUBSCRIBER,
            field_order=("email",),
        ),
        lambda: _resolve_live_direct_email(db, normalized),
        lambda: _resolve_index_stage(
            db,
            identity_type=IDENTITY_TYPE_EMAIL,
            normalized_value=normalized,
            source_table=SOURCE_SUBSCRIBER_CONTACTS,
            matched_via=MATCH_VIA_SUBSCRIBER_CONTACT,
            field_order=("email",),
        ),
        lambda: _resolve_live_contact_email(db, normalized),
        lambda: _resolve_index_stage(
            db,
            identity_type=IDENTITY_TYPE_EMAIL,
            normalized_value=normalized,
            source_table=SOURCE_SUBSCRIBER_CHANNELS,
            matched_via=MATCH_VIA_SUBSCRIBER_CHANNEL,
            field_order=("email",),
        ),
        lambda: _resolve_live_channel_email(db, normalized),
        lambda: _resolve_historical_email(db, normalized),
    ):
        resolution = _finalize_stage(
            raw_identifier,
            normalized,
            IDENTITY_TYPE_EMAIL,
            inbound_channel,
            resolver(),
        )
        if resolution is not None:
            return resolution
    return CustomerIdentityResolution(
        raw_identifier=raw_identifier,
        normalized_identifier=normalized,
        identity_type=IDENTITY_TYPE_EMAIL,
        inbound_channel=inbound_channel,
        matched=False,
        ambiguous=False,
    )


def _resolve_phone_identity(
    db: Session,
    raw_identifier: str,
    normalized: str,
    *,
    inbound_channel: str | None,
    country_code: str,
) -> CustomerIdentityResolution:
    # Historical participant linkage must remain the final fallback so it can
    # never override direct subscriber, linked-contact, or subscriber-channel identities.
    for resolver in (
        lambda: _resolve_index_stage(
            db,
            identity_type=IDENTITY_TYPE_PHONE,
            normalized_value=normalized,
            source_table=SOURCE_SUBSCRIBERS,
            matched_via=MATCH_VIA_SUBSCRIBER,
            field_order=("phone",),
        ),
        lambda: _resolve_live_direct_phone(
            db,
            normalized,
            country_code=country_code,
        ),
        lambda: _resolve_index_stage(
            db,
            identity_type=IDENTITY_TYPE_PHONE,
            normalized_value=normalized,
            source_table=SOURCE_SUBSCRIBER_CONTACTS,
            matched_via=MATCH_VIA_SUBSCRIBER_CONTACT,
            field_order=("phone", "whatsapp"),
        ),
        lambda: _resolve_live_contact_phone(
            db,
            normalized,
            country_code=country_code,
        ),
        lambda: _resolve_index_stage(
            db,
            identity_type=IDENTITY_TYPE_PHONE,
            normalized_value=normalized,
            source_table=SOURCE_SUBSCRIBER_CHANNELS,
            matched_via=MATCH_VIA_SUBSCRIBER_CHANNEL,
            field_order=("phone", "sms", "whatsapp"),
        ),
        lambda: _resolve_live_channel_phone(
            db,
            normalized,
            country_code=country_code,
        ),
        lambda: _resolve_historical_phone(
            db,
            normalized,
            country_code=country_code,
        ),
    ):
        resolution = _finalize_stage(
            raw_identifier,
            normalized,
            IDENTITY_TYPE_PHONE,
            inbound_channel,
            resolver(),
        )
        if resolution is not None:
            return resolution
    return CustomerIdentityResolution(
        raw_identifier=raw_identifier,
        normalized_identifier=normalized,
        identity_type=IDENTITY_TYPE_PHONE,
        inbound_channel=inbound_channel,
        matched=False,
        ambiguous=False,
    )


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


def _resolve_index_stage(
    db: Session,
    *,
    identity_type: str,
    normalized_value: str,
    source_table: str,
    matched_via: str,
    field_order: tuple[str, ...],
) -> tuple[_StageMatch | None, int]:
    rows = db.scalars(
        select(CustomerIdentityIndex).where(
            CustomerIdentityIndex.identity_type == identity_type,
            CustomerIdentityIndex.normalized_value == normalized_value,
            CustomerIdentityIndex.source_table == source_table,
        )
    ).all()
    return _collapse_index_rows(
        db,
        rows,
        matched_via=matched_via,
        field_order=field_order,
    )


def _collapse_index_rows(
    db: Session,
    rows: Sequence[CustomerIdentityIndex],
    *,
    matched_via: str,
    field_order: tuple[str, ...],
) -> tuple[_StageMatch | None, int]:
    if not rows:
        return (None, 0)
    unique_subscribers = {
        row.subscriber_id for row in rows if row.subscriber_id is not None
    }
    if len(unique_subscribers) > 1:
        return (None, len(unique_subscribers))
    field_rank = {field: index for index, field in enumerate(field_order)}
    row = sorted(
        rows,
        key=lambda item: (
            field_rank.get(item.source_field, len(field_rank)),
            str(item.subscriber_contact_id or item.subscriber_channel_id or item.id),
        ),
    )[0]
    return (
        _StageMatch(
            subscriber_id=row.subscriber_id,
            matched_via=matched_via,
            matched_field=row.source_field,
            source_table=row.source_table,
            source_record_id=row.subscriber_contact_id
            or row.subscriber_channel_id
            or row.subscriber_id,
            match_confidence=_match_confidence_for_index_row(db, row, matched_via),
            matched_contact_id=row.subscriber_contact_id,
            matched_channel_id=row.subscriber_channel_id,
        ),
        1,
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


def _resolve_live_direct_email(
    db: Session, normalized_value: str
) -> tuple[_StageMatch | None, int]:
    rows = db.scalars(
        select(Subscriber.id).where(func.lower(Subscriber.email) == normalized_value)
    ).all()
    return _collapse_subscriber_rows(
        rows, matched_via=MATCH_VIA_SUBSCRIBER, field="email"
    )


def _resolve_live_direct_phone(
    db: Session, normalized_value: str, *, country_code: str
) -> tuple[_StageMatch | None, int]:
    subscribers = db.scalars(
        select(Subscriber).where(Subscriber.phone.is_not(None))
    ).all()
    rows = [
        subscriber.id
        for subscriber in subscribers
        if normalize_phone_identifier(
            subscriber.phone, default_country_code=country_code
        )
        == normalized_value
    ]
    return _collapse_subscriber_rows(
        rows, matched_via=MATCH_VIA_SUBSCRIBER, field="phone"
    )


def _collapse_subscriber_rows(
    subscriber_ids: Sequence[UUID],
    *,
    matched_via: str,
    field: str,
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
            matched_via=matched_via,
            matched_field=field,
            source_table=SOURCE_SUBSCRIBERS,
            source_record_id=subscriber_id,
            match_confidence=MATCH_CONFIDENCE_HIGH,
        ),
        1,
    )


def _resolve_live_contact_email(
    db: Session, normalized_value: str
) -> tuple[_StageMatch | None, int]:
    contacts = db.scalars(
        select(SubscriberContact).where(
            func.lower(SubscriberContact.email) == normalized_value
        )
    ).all()
    return _collapse_contact_rows(contacts, field_order=("email",))


def _resolve_live_contact_phone(
    db: Session, normalized_value: str, *, country_code: str
) -> tuple[_StageMatch | None, int]:
    contacts = db.scalars(
        select(SubscriberContact).where(
            or_(
                SubscriberContact.phone.is_not(None),
                SubscriberContact.whatsapp.is_not(None),
            )
        )
    ).all()
    matches = [
        contact
        for contact in contacts
        if normalize_phone_identifier(contact.phone, default_country_code=country_code)
        == normalized_value
        or normalize_phone_identifier(
            contact.whatsapp, default_country_code=country_code
        )
        == normalized_value
    ]
    return _collapse_contact_rows(
        matches,
        field_order=("phone", "whatsapp"),
        normalized_value=normalized_value,
        country_code=country_code,
    )


def _collapse_contact_rows(
    contacts: Sequence[SubscriberContact],
    *,
    field_order: tuple[str, ...],
    normalized_value: str | None = None,
    country_code: str | None = None,
) -> tuple[_StageMatch | None, int]:
    if not contacts:
        return (None, 0)
    unique_subscribers = {
        contact.subscriber_id
        for contact in contacts
        if contact.subscriber_id is not None
    }
    if len(unique_subscribers) > 1:
        return (None, len(unique_subscribers))
    field_rank = {field: index for index, field in enumerate(field_order)}

    def _matched_field(contact: SubscriberContact) -> str:
        phone_country_code = country_code or default_country_code()
        if (
            "phone" in field_order
            and normalize_phone_identifier(
                contact.phone, default_country_code=phone_country_code
            )
            == normalized_value
        ):
            return "phone"
        if (
            "whatsapp" in field_order
            and normalize_phone_identifier(
                contact.whatsapp, default_country_code=phone_country_code
            )
            == normalized_value
        ):
            return "whatsapp"
        return field_order[0]

    contact = sorted(
        contacts,
        key=lambda item: (
            field_rank.get(_matched_field(item), len(field_rank)),
            str(item.id),
        ),
    )[0]
    return (
        _StageMatch(
            subscriber_id=contact.subscriber_id,
            matched_via=MATCH_VIA_SUBSCRIBER_CONTACT,
            matched_field=_matched_field(contact),
            source_table=SOURCE_SUBSCRIBER_CONTACTS,
            source_record_id=contact.id,
            match_confidence=MATCH_CONFIDENCE_MEDIUM,
            matched_contact_id=contact.id,
        ),
        1,
    )


def _resolve_live_channel_email(
    db: Session, normalized_value: str
) -> tuple[_StageMatch | None, int]:
    channels = db.scalars(
        select(SubscriberChannel).where(
            func.lower(SubscriberChannel.address) == normalized_value
        )
    ).all()
    return _collapse_channel_rows(channels, field_order=("email",))


def _resolve_live_channel_phone(
    db: Session, normalized_value: str, *, country_code: str
) -> tuple[_StageMatch | None, int]:
    channels = db.scalars(
        select(SubscriberChannel).where(SubscriberChannel.address.is_not(None))
    ).all()
    matches = [
        channel
        for channel in channels
        if normalize_phone_identifier(
            channel.address, default_country_code=country_code
        )
        == normalized_value
    ]
    return _collapse_channel_rows(matches, field_order=("phone", "sms", "whatsapp"))


def _collapse_channel_rows(
    channels: Sequence[SubscriberChannel],
    *,
    field_order: tuple[str, ...],
) -> tuple[_StageMatch | None, int]:
    if not channels:
        return (None, 0)
    unique_subscribers = {
        channel.subscriber_id
        for channel in channels
        if channel.subscriber_id is not None
    }
    if len(unique_subscribers) > 1:
        return (None, len(unique_subscribers))
    field_rank = {field: index for index, field in enumerate(field_order)}
    channel = sorted(
        channels,
        key=lambda item: (
            field_rank.get(
                str(item.channel_type.value if item.channel_type else "address"),
                len(field_rank),
            ),
            str(item.id),
        ),
    )[0]
    matched_field = str(
        channel.channel_type.value if channel.channel_type else "address"
    )
    return (
        _StageMatch(
            subscriber_id=channel.subscriber_id,
            matched_via=MATCH_VIA_SUBSCRIBER_CHANNEL,
            matched_field=matched_field,
            source_table=SOURCE_SUBSCRIBER_CHANNELS,
            source_record_id=channel.id,
            match_confidence=MATCH_CONFIDENCE_HIGH
            if channel.is_verified
            else MATCH_CONFIDENCE_MEDIUM,
            matched_channel_id=channel.id,
        ),
        1,
    )


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
