"""Durable Team Inbox provider observations.

Transport adapters normalize provider payloads into the dataclasses in this
module. The observation owner commits that fact before the processing owner
resolves contact, thread, routing, or delivery state.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.team_inbox import (
    InboxChannelType,
    InboxObservationKind,
    InboxObservationStatus,
    InboxProviderObservation,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OBSERVATION_OWNER = "communications.team_inbox_observations"

_RECORD_OBSERVATION = OwnerCommandDefinition(
    owner=OBSERVATION_OWNER,
    concern="normalized inbound provider observation ledger",
    name="record_team_inbox_provider_observation",
)


class InboxProvider(StrEnum):
    smtp = "smtp"
    meta_cloud_api = "meta_cloud_api"
    meta_social = "meta_social"
    chat_widget = "chat_widget"
    fiber_website = "fiber_website"


class ObservationProcessingOutcome(StrEnum):
    recorded = "recorded"
    replayed = "replayed"
    processed = "processed"
    already_processed = "already_processed"


class TeamInboxObservationError(DomainError):
    """Transport-neutral provider observation rejection."""


@dataclass(frozen=True, slots=True)
class InboundLocationObservation:
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.latitude)
            or not -90 <= self.latitude <= 90
            or not math.isfinite(self.longitude)
            or not -180 <= self.longitude <= 180
        ):
            raise TeamInboxObservationError(
                code="communications.team_inbox_observations.invalid_location",
                message="The provider location coordinates are invalid.",
            )

    def to_metadata(self) -> dict[str, float | str | None]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "name": self.name,
            "address": self.address,
        }


def inbound_location_observation(
    *,
    latitude: object,
    longitude: object,
    name: object = None,
    address: object = None,
) -> InboundLocationObservation | None:
    if latitude is None and longitude is None:
        return None
    if (
        latitude is None
        or longitude is None
        or isinstance(latitude, bool)
        or isinstance(longitude, bool)
    ):
        raise TeamInboxObservationError(
            code="communications.team_inbox_observations.invalid_location",
            message="The provider location must include valid latitude and longitude.",
        )
    try:
        latitude_value = float(str(latitude).strip())
        longitude_value = float(str(longitude).strip())
    except (TypeError, ValueError) as exc:
        raise TeamInboxObservationError(
            code="communications.team_inbox_observations.invalid_location",
            message="The provider location must include valid latitude and longitude.",
        ) from exc
    return InboundLocationObservation(
        latitude=latitude_value,
        longitude=longitude_value,
        name=(
            str(name).strip()[:255] if name is not None and str(name).strip() else None
        ),
        address=(
            str(address).strip()[:500]
            if address is not None and str(address).strip()
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class InboundAttachmentObservation:
    asset_type: str
    file_name: str | None = None
    mime_type: str | None = None
    provider_media_id: str | None = None
    source_url: str | None = None
    caption: str | None = None
    file_size: int | None = None
    download_status: str | None = None
    location: InboundLocationObservation | None = None


@dataclass(frozen=True, slots=True)
class InboundMessageObservation:
    contact_address: str
    body: str
    contact_name: str | None = None
    subject: str | None = None
    external_thread_id: str | None = None
    subscriber_id: UUID | None = None
    fallback_service_team_id: UUID | None = None
    to_addresses: tuple[str, ...] = ()
    cc_addresses: tuple[str, ...] = ()
    in_reply_to: str | None = None
    references: str | None = None
    smtp_probe: bool = False
    campaign_attributed: bool = False
    # Transport-authentication evidence exactly as the sending relay wrote it.
    # Ingestion is the only moment it exists — nothing can recover an SPF or
    # DKIM result for a message already accepted — so it is carried even though
    # no admission policy reads it yet.
    authentication: dict[str, object] | None = None
    provider_account_id: str | None = None
    external_account_id: str | None = None
    page_id: str | None = None
    instagram_account_id: str | None = None
    provider_comment_id: str | None = None
    comment_id: str | None = None
    post_id: str | None = None
    media_id: str | None = None
    parent_provider_comment_id: str | None = None
    commenter_id: str | None = None
    commenter_name: str | None = None
    commenter_username: str | None = None
    surface: str | None = None
    permalink_url: str | None = None
    media_url: str | None = None
    contact_profile: dict[str, str | None] | None = None
    attachments: tuple[InboundAttachmentObservation, ...] = ()
    body_text: str | None = None
    html_body: str | None = None


@dataclass(frozen=True, slots=True)
class FiberWebsiteInquiryObservation:
    full_name: str
    email: str
    phone: str | None
    interest: str
    message: str | None
    integration_inbox_id: UUID
    form_version: str = "fiber-contact-v1"


@dataclass(frozen=True, slots=True)
class DeliveryReceiptObservation:
    status: str
    recipient_id: str | None = None
    error_codes: tuple[str, ...] = ()


NormalizedObservation = (
    InboundMessageObservation
    | FiberWebsiteInquiryObservation
    | DeliveryReceiptObservation
)


@dataclass(frozen=True, slots=True)
class RecordProviderObservationCommand:
    context: CommandContext
    provider: InboxProvider
    provider_account_scope: str
    provider_event_id: str
    kind: InboxObservationKind
    channel_type: InboxChannelType
    external_message_id: str | None
    observed_at: datetime
    payload: NormalizedObservation


@dataclass(frozen=True, slots=True)
class ProviderObservationOutcome:
    observation_id: UUID
    outcome: ObservationProcessingOutcome
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    processing_status: InboxObservationStatus = InboxObservationStatus.recorded
    consequence_kind: str | None = None
    subscriber_id: UUID | None = None
    reseller_id: UUID | None = None
    resolution_status: str | None = None


def _error(suffix: str, message: str, **details: object) -> TeamInboxObservationError:
    return TeamInboxObservationError(
        code=f"{OBSERVATION_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _payload_dict(payload: NormalizedObservation) -> dict[str, object]:
    data = asdict(payload)
    for key, value in tuple(data.items()):
        if isinstance(value, UUID):
            data[key] = str(value)
        elif isinstance(value, tuple):
            data[key] = list(value)
    return data


def _fingerprint(command: RecordProviderObservationCommand) -> str:
    evidence = {
        "provider": command.provider.value,
        "provider_account_scope": command.provider_account_scope,
        "provider_event_id": command.provider_event_id,
        "kind": command.kind.value,
        "channel_type": command.channel_type.value,
        "external_message_id": command.external_message_id,
        "observed_at": command.observed_at.astimezone(UTC).isoformat(),
        "payload": _payload_dict(command.payload),
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def observation_fingerprint(command: RecordProviderObservationCommand) -> str:
    """The domain fingerprint this owner arbitrates replay against collision by.

    Public because a parity harness must compare against the SAME rule the
    owner enforces. A caller that recomputed its own equivalent would be a
    second definition of "is this the same fact?", and the two would drift.
    Callers may read it; only this module writes it to a row.
    """

    return _fingerprint(command)


def normalized_payload(payload: NormalizedObservation) -> dict[str, object]:
    """The exact JSON shape this owner persists for one normalized payload."""

    return _payload_dict(payload)


def _validate(command: RecordProviderObservationCommand) -> tuple[str, str, str]:
    provider_scope = command.provider_account_scope.strip()
    provider_event_id = command.provider_event_id.strip()
    external_message_id = str(command.external_message_id or "").strip()
    if not provider_scope:
        raise _error("invalid_observation", "Provider account scope is required.")
    if not provider_event_id:
        raise _error("invalid_observation", "Provider event identity is required.")
    if command.observed_at.tzinfo is None:
        raise _error(
            "invalid_observation", "Provider observed_at must be timezone-aware."
        )
    if command.kind is InboxObservationKind.message:
        if not isinstance(
            command.payload,
            (InboundMessageObservation, FiberWebsiteInquiryObservation),
        ):
            raise _error(
                "invalid_observation", "Message observation payload is invalid."
            )
        if isinstance(command.payload, InboundMessageObservation) and (
            not command.payload.contact_address.strip()
            or (not command.payload.body.strip() and not command.payload.attachments)
        ):
            raise _error(
                "invalid_observation",
                "Inbound contact address and message content are required.",
            )
        if isinstance(command.payload, FiberWebsiteInquiryObservation) and (
            command.channel_type is not InboxChannelType.website_fiber
            or command.provider is not InboxProvider.fiber_website
            or not command.payload.full_name.strip()
            or not command.payload.email.strip()
            or not command.payload.interest.strip()
        ):
            raise _error(
                "invalid_observation",
                "Fiber inquiry identity and interest are required.",
            )
        if not external_message_id:
            raise _error(
                "invalid_observation",
                "Inbound provider message identity is required.",
            )
    elif not isinstance(command.payload, DeliveryReceiptObservation):
        raise _error("invalid_observation", "Delivery receipt payload is invalid.")
    elif not external_message_id or not command.payload.status.strip():
        raise _error(
            "invalid_observation",
            "Delivery receipt message identity and status are required.",
        )
    return provider_scope[:160], provider_event_id[:255], external_message_id[:255]


def record_provider_observation(
    db: Session,
    command: RecordProviderObservationCommand,
) -> ProviderObservationOutcome:
    """Commit one normalized provider fact and prove exact replay equivalence."""

    def operation() -> ProviderObservationOutcome:
        provider_scope, provider_event_id, external_message_id = _validate(command)
        fingerprint = _fingerprint(command)
        existing = db.execute(
            select(InboxProviderObservation).where(
                InboxProviderObservation.provider == command.provider.value,
                InboxProviderObservation.provider_account_scope == provider_scope,
                InboxProviderObservation.provider_event_id == provider_event_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.payload_fingerprint != fingerprint:
                raise _error(
                    "provider_event_identity_collision",
                    "Provider reused an observation identity with different evidence.",
                    provider=command.provider.value,
                )
            return ProviderObservationOutcome(
                observation_id=existing.id,
                outcome=ObservationProcessingOutcome.replayed,
                conversation_id=existing.conversation_id,
                message_id=existing.message_id,
                processing_status=InboxObservationStatus(existing.processing_status),
            )

        row = InboxProviderObservation(
            provider=command.provider.value,
            provider_account_scope=provider_scope,
            provider_event_id=provider_event_id,
            observation_kind=command.kind.value,
            channel_type=command.channel_type.value,
            external_message_id=external_message_id or None,
            external_thread_id=(
                command.payload.external_thread_id[:255]
                if isinstance(command.payload, InboundMessageObservation)
                and command.payload.external_thread_id
                else None
            ),
            payload_fingerprint=fingerprint,
            normalized_payload=_payload_dict(command.payload),
            observed_at=command.observed_at.astimezone(UTC),
            recorded_at=datetime.now(UTC),
            processing_status=InboxObservationStatus.recorded.value,
        )
        db.add(row)
        db.flush()
        return ProviderObservationOutcome(
            observation_id=row.id,
            outcome=ObservationProcessingOutcome.recorded,
        )

    try:
        return execute_owner_command(
            db,
            definition=_RECORD_OBSERVATION,
            context=command.context,
            operation=operation,
        )
    except IntegrityError:
        # A concurrent exact replay may lose the unique-key race after its
        # initial lookup. The first command has rolled back, so retry the same
        # owner boundary once: it now proves fingerprint equivalence against
        # the committed winner and returns the stable replay outcome. Changed
        # evidence is still rejected by ``operation``.
        return execute_owner_command(
            db,
            definition=_RECORD_OBSERVATION,
            context=command.context,
            operation=operation,
        )
