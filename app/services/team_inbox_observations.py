"""Durable Team Inbox provider observations.

Transport adapters normalize provider payloads into the dataclasses in this
module. The observation owner commits that fact before the processing owner
resolves contact, thread, routing, or delivery state.
"""

from __future__ import annotations

import hashlib
import json
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


class ObservationProcessingOutcome(StrEnum):
    recorded = "recorded"
    replayed = "replayed"
    processed = "processed"
    already_processed = "already_processed"


class TeamInboxObservationError(DomainError):
    """Transport-neutral provider observation rejection."""


@dataclass(frozen=True, slots=True)
class InboundAttachmentObservation:
    asset_type: str
    file_name: str | None = None
    mime_type: str | None = None
    provider_media_id: str | None = None
    source_url: str | None = None
    caption: str | None = None
    file_size: int | None = None


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
    attachments: tuple[InboundAttachmentObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliveryReceiptObservation:
    status: str
    recipient_id: str | None = None
    error_codes: tuple[str, ...] = ()


NormalizedObservation = InboundMessageObservation | DeliveryReceiptObservation


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
        if not isinstance(command.payload, InboundMessageObservation):
            raise _error(
                "invalid_observation", "Message observation payload is invalid."
            )
        if (
            not command.payload.contact_address.strip()
            or not command.payload.body.strip()
        ):
            raise _error(
                "invalid_observation",
                "Inbound contact address and message body are required.",
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
