"""Envelope -> Sub command normalization for the Integrator messaging port.

Owner: ``communications.team_inbox_integrator_envelope``.

This module is pure. It takes no session, writes nothing, reaches no network,
and holds no state. Everything it produces is a
:class:`~app.services.team_inbox_observations.RecordProviderObservationCommand`
handed to the existing observation owner, which is the only writer of an
``inbox_provider_observations`` row.

## Why the normalization lives on Sub's side of the wire

The Integrator translates a provider's wire format into a provider-neutral
capability envelope. It does not, and must not, know that Sub prefixes a
provider event identity with ``message:``, that Sub's WhatsApp channel is
called ``whatsapp``, or that Sub truncates an account scope to 160 characters.
Those are Sub's conventions for Sub's own columns. If the Integrator encoded
them, every one of Sub's internal renamings would become a cross-repository
breaking change.

So the envelope carries the provider's facts and Sub converts them using the
*same* conventions its own receiver uses. That is what makes an Integrator-fed
observation and a webhook-fed observation land on one identity — which is in
turn what makes the producer-overlap window safe (see
``docs/INTEGRATOR_MESSAGING_RECEIVE_CUTOVER.md``).

## `provider` names the provider, never the transport

A WhatsApp message that reached Sub through the Integrator is still a
``meta_cloud_api`` observation. Recording it under an ``integrator`` provider
would do two harmful things at once: it would let a transport masquerade as a
provider in the domain identity, and it would give the same upstream event two
different identities either side of a cutover, so every message in flight would
be recorded twice. Which transport carried it is provenance, and provenance
lives on the ``integration_inbox`` receipt — a row that already names the
binding, the installation and therefore the Integrator.

## Two different fingerprints, deliberately

``payload_fingerprint`` on the envelope is the **transport** fingerprint: a
canonical-JSON SHA-256 over the observation body as the Integrator serialized
it, recomputed here so a body mangled in transit is refused rather than
recorded. Sub's **domain** fingerprint is computed inside
``team_inbox_observations._fingerprint`` over the whole normalized command and
is what arbitrates replay against collision. They answer different questions
and neither substitutes for the other.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from app.models.team_inbox import InboxChannelType, InboxObservationKind
from app.schemas.integrator_observation import (
    MESSAGING_RECEIVE_CAPABILITY,
    SUPPORTED_CONTRACT_VERSIONS,
    IntegratorObservationEnvelope,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.team_inbox_observations import (
    DeliveryReceiptObservation,
    InboundAttachmentObservation,
    InboundMessageObservation,
    InboxProvider,
    RecordProviderObservationCommand,
)

OWNER = "communications.team_inbox_integrator_envelope"

#: Which Sub channel each provider family is allowed to claim. An envelope
#: naming a channel outside its provider's row is refused: ``note`` and
#: ``field_job`` are internal channels with no external transport, and
#: ``website_fiber`` carries a different observation type under a different
#: capability. Fail-closed, so a new channel is an explicit decision here
#: rather than something an authenticated caller can assert into existence.
PROVIDER_CHANNELS: dict[InboxProvider, frozenset[InboxChannelType]] = {
    InboxProvider.meta_cloud_api: frozenset({InboxChannelType.whatsapp}),
    InboxProvider.meta_social: frozenset(
        {
            InboxChannelType.facebook_messenger,
            InboxChannelType.instagram_dm,
            InboxChannelType.facebook_comment,
            InboxChannelType.instagram_comment,
        }
    ),
    InboxProvider.chat_widget: frozenset({InboxChannelType.chat_widget}),
    InboxProvider.smtp: frozenset({InboxChannelType.email}),
}


class IntegratorEnvelopeError(DomainError):
    """A transport-neutral rejection of one Integrator envelope."""


class UnknownCapability(IntegratorEnvelopeError):
    """The envelope names a capability this deployment does not accept."""


class UnsupportedContractVersion(IntegratorEnvelopeError):
    """The envelope names a contract version this deployment has not deployed."""


@dataclass(frozen=True, slots=True)
class NormalizedEnvelope:
    """One envelope, converted into Sub's own vocabulary. Nothing is decided."""

    command: RecordProviderObservationCommand
    #: The Integrator's binding scope, carried for the transport receipt only.
    scope_kind: str
    scope_ref: str

    @property
    def identity(self) -> tuple[str, str, str]:
        """The domain dedup identity: provider, account scope, event id."""

        return (
            self.command.provider.value,
            self.command.provider_account_scope,
            self.command.provider_event_id,
        )


def _error(suffix: str, message: str, **details: object) -> IntegratorEnvelopeError:
    return IntegratorEnvelopeError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def canonical_fingerprint(body: dict[str, Any]) -> str:
    """Canonical-JSON SHA-256 over an observation body, as both sides compute it."""

    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _observation_body(envelope: IntegratorObservationEnvelope) -> dict[str, Any]:
    """The exact sub-document the transport fingerprint covers."""

    payload = envelope.message or envelope.delivery_receipt
    if payload is None:  # pragma: no cover - guarded by require_capability
        raise _error("empty_observation", "The envelope carries no observation.")
    return payload.model_dump(mode="json")


def require_capability(envelope: IntegratorObservationEnvelope) -> None:
    """Refuse anything this deployment does not accept, before touching a row.

    A capability Sub does not accept is reported as *not found* by the adapter
    rather than *forbidden*: telling an authenticated caller which capabilities
    exist is itself information, and the caller has no need for it.
    """

    if envelope.capability_id != MESSAGING_RECEIVE_CAPABILITY:
        raise UnknownCapability(
            code=f"{OWNER}.unknown_capability",
            message="This deployment does not accept the requested capability.",
            details={"capability_id": envelope.capability_id},
        )
    if envelope.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        raise UnsupportedContractVersion(
            code=f"{OWNER}.unsupported_contract_version",
            message="This deployment has not deployed the requested contract version.",
            details={
                "contract_version": envelope.contract_version,
                "supported": sorted(SUPPORTED_CONTRACT_VERSIONS),
            },
        )
    if (envelope.message is None) == (envelope.delivery_receipt is None):
        raise _error(
            "invalid_envelope",
            "An envelope carries exactly one of message or delivery_receipt.",
        )


def _provider(envelope: IntegratorObservationEnvelope) -> InboxProvider:
    try:
        provider = InboxProvider(envelope.provider)
    except ValueError as exc:
        raise _error(
            "unknown_provider",
            "The envelope names a provider family Sub does not record.",
            provider=envelope.provider,
        ) from exc
    if provider not in PROVIDER_CHANNELS:
        raise _error(
            "unsupported_provider",
            "That provider family is not carried by this capability.",
            provider=provider.value,
        )
    return provider


def _channel(
    envelope: IntegratorObservationEnvelope, provider: InboxProvider
) -> InboxChannelType:
    try:
        channel = InboxChannelType(envelope.channel)
    except ValueError as exc:
        raise _error(
            "unknown_channel",
            "The envelope names a channel Sub does not carry.",
            channel=envelope.channel,
        ) from exc
    if channel not in PROVIDER_CHANNELS[provider]:
        raise _error(
            "channel_provider_mismatch",
            "That channel is not carried by that provider family.",
            channel=channel.value,
            provider=provider.value,
        )
    return channel


def _attachments(
    envelope: IntegratorObservationEnvelope,
) -> tuple[InboundAttachmentObservation, ...]:
    message = envelope.message
    if message is None:
        return ()
    return tuple(
        InboundAttachmentObservation(
            asset_type=item.asset_type,
            file_name=item.file_name,
            mime_type=item.mime_type,
            provider_media_id=item.provider_media_id,
            source_url=item.source_url,
            caption=item.caption,
            file_size=item.file_size,
            download_status=item.download_status,
        )
        for item in message.attachments
    )


def scoped_provider_event_id(
    *, kind: InboxObservationKind, provider_event_id: str
) -> str:
    """Namespace a provider event id the way Sub's own receivers already do.

    A provider reuses one message id across an inbound message and its later
    delivery receipts. Sub's existing receivers therefore prefix the identity
    with the observation kind, and the port must use the identical prefix or an
    Integrator-fed observation would be a *different* row from the webhook-fed
    one for the same upstream event.
    """

    prefix = "message" if kind is InboxObservationKind.message else "receipt"
    return f"{prefix}:{provider_event_id}"


def normalize(
    envelope: IntegratorObservationEnvelope,
    *,
    context: CommandContext,
) -> NormalizedEnvelope:
    """Convert one verified envelope into the observation owner's command."""

    require_capability(envelope)
    if envelope.payload_fingerprint != canonical_fingerprint(
        _observation_body(envelope)
    ):
        raise _error(
            "payload_fingerprint_mismatch",
            "The observation body does not match its declared fingerprint.",
        )

    provider = _provider(envelope)
    channel = _channel(envelope, provider)
    observed_at = envelope.observed_at
    if observed_at.tzinfo is None:
        raise _error("invalid_observed_at", "observed_at must be timezone-aware.")
    observed_at = observed_at.astimezone(UTC)

    if envelope.message is not None:
        message = envelope.message
        kind = InboxObservationKind.message
        external_message_id = message.external_message_id
        payload: InboundMessageObservation | DeliveryReceiptObservation = (
            InboundMessageObservation(
                contact_address=message.contact_address,
                body=message.body,
                contact_name=message.contact_name,
                subject=message.subject,
                external_thread_id=message.external_thread_id,
                provider_account_id=message.provider_account_id,
                external_account_id=message.external_account_id,
                page_id=message.page_id,
                instagram_account_id=message.instagram_account_id,
                surface=message.surface,
                permalink_url=message.permalink_url,
                media_url=message.media_url,
                contact_profile=(
                    message.contact_profile.model_dump()
                    if message.contact_profile is not None
                    else None
                ),
                attachments=_attachments(envelope),
            )
        )
    elif (receipt := envelope.delivery_receipt) is not None:
        kind = InboxObservationKind.delivery_receipt
        external_message_id = receipt.external_message_id
        payload = DeliveryReceiptObservation(
            status=receipt.status,
            recipient_id=receipt.recipient_id,
            error_codes=tuple(receipt.error_codes),
        )
    else:  # pragma: no cover - require_capability proved exactly one is present
        raise _error("empty_observation", "The envelope carries no observation.")

    return NormalizedEnvelope(
        command=RecordProviderObservationCommand(
            context=context,
            provider=provider,
            provider_account_scope=envelope.provider_account_scope.strip()[:160],
            provider_event_id=scoped_provider_event_id(
                kind=kind, provider_event_id=envelope.provider_event_id.strip()
            )[:255],
            kind=kind,
            channel_type=channel,
            external_message_id=external_message_id[:255],
            observed_at=observed_at,
            payload=payload,
        ),
        scope_kind=envelope.scope.kind,
        scope_ref=envelope.scope.ref,
    )


def observation_context(envelope: IntegratorObservationEnvelope) -> CommandContext:
    """The observation-write context, keyed on the provider's own event identity."""

    return CommandContext.system(
        actor="transport:integrator",
        scope="team-inbox:provider-observation",
        reason="record Integrator-delivered inbound observation",
        idempotency_key=f"{envelope.provider}:{envelope.provider_event_id}",
    )
