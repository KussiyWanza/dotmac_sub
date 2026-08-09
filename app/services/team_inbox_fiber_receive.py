"""Fiber website inquiry transport and Team Inbox participant."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.party import PartyContactPointType, PartyType
from app.models.sales import LeadCaptureMethod, LeadSourcePlatform
from app.models.team_inbox import (
    InboxChannelType,
    InboxObservationKind,
)
from app.schemas.fiber_inquiry import FiberInquiryRequest
from app.schemas.sales import (
    LeadCapturePartyCreate,
    LeadCaptureRequest,
    LeadContactObservation,
    LeadOriginCaptureCreate,
)
from app.services import (
    team_inbox_observations,
)
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_phone_identifier,
)
from app.services.owner_commands import CommandContext
from app.services.sales import capture
from app.services.team_inbox_channel_receive import resolve_contact_context

PROVIDER = team_inbox_observations.InboxProvider.fiber_website
CHANNEL = InboxChannelType.website_fiber


@dataclass(frozen=True, slots=True)
class FiberInquiryIngressCommand:
    delivery_id: str
    site_id: str
    payload: FiberInquiryRequest
    actor: str
    integration_inbox_id: UUID


@dataclass(frozen=True, slots=True)
class FiberIdentityResolution:
    status: str
    subscriber_id: UUID | None
    matched_subscriber_ids: tuple[str, ...]
    suppressed_subscriber_ids: tuple[str, ...]
    identity_review_required: bool

    def as_metadata(self) -> dict[str, object]:
        return {
            "status": self.status,
            "subscriber_id": str(self.subscriber_id) if self.subscriber_id else None,
            "matched_subscriber_ids": list(self.matched_subscriber_ids),
            "suppressed_subscriber_ids": list(self.suppressed_subscriber_ids),
            "identity_review_required": self.identity_review_required,
        }


@dataclass(frozen=True, slots=True)
class FiberInquiryReceiveResult:
    kind: str
    conversation_id: str
    message_id: str
    duplicate: bool
    subscriber_id: str | None
    reseller_id: str | None = None
    resolution_status: str = "unmatched"


@dataclass(frozen=True, slots=True)
class FiberInquiryIngressOutcome:
    observation_id: UUID
    conversation_id: UUID
    message_id: UUID
    replayed: bool
    resolution_status: str


def resolve_fiber_identity(
    db: Session, *, email: str, phone: str | None
) -> FiberIdentityResolution:
    resolutions = [
        resolve_contact_context(
            db,
            channel_type=InboxChannelType.email.value,
            contact_address=email,
        )
    ]
    if phone:
        resolutions.append(
            resolve_contact_context(
                db,
                channel_type=InboxChannelType.whatsapp.value,
                contact_address=phone,
            )
        )
    matched = tuple(
        sorted(
            {
                subscriber_id
                for resolution in resolutions
                for subscriber_id in resolution.matched_subscriber_ids
            }
        )
    )
    suppressed = tuple(
        sorted(
            {
                subscriber_id
                for resolution in resolutions
                for subscriber_id in resolution.suppressed_subscriber_ids
            }
        )
    )
    ambiguous = (
        any(
            resolution.status in {"ambiguous", "suppressed_inactive"}
            for resolution in resolutions
        )
        or len(matched) > 1
    )
    subscriber_id = UUID(matched[0]) if len(matched) == 1 and not ambiguous else None
    if subscriber_id is not None:
        status = "linked_subscriber"
    elif ambiguous or suppressed:
        status = "identity_review_required"
    else:
        status = "unmatched"
    return FiberIdentityResolution(
        status=status,
        subscriber_id=subscriber_id,
        matched_subscriber_ids=matched,
        suppressed_subscriber_ids=suppressed,
        identity_review_required=status == "identity_review_required",
    )


def render_fiber_inquiry_body(payload: FiberInquiryRequest) -> str:
    phone = payload.phone or "Not supplied"
    message = payload.message or "No additional message."
    return (
        "Fiber website inquiry\n\n"
        f"Name: {payload.full_name}\n"
        f"Email: {payload.email}\n"
        f"Phone: {phone}\n"
        f"Interest: {payload.interest.label}\n\n"
        f"{message}"
    )


def capture_fiber_prospect(
    db: Session,
    *,
    payload: FiberInquiryRequest,
    delivery_id: str,
    actor: str,
):
    contacts = [
        LeadContactObservation(
            channel_type=PartyContactPointType.email,
            value=str(payload.email),
            display_value=str(payload.email),
            provider="fiber_website",
            provider_account_id="fiber.dotmac.ng",
            is_primary=True,
        )
    ]
    normalized_phone = normalize_phone_identifier(
        payload.phone,
        default_country_code=default_country_code(db),
    )
    if normalized_phone:
        contacts.append(
            LeadContactObservation(
                channel_type=PartyContactPointType.phone,
                value=normalized_phone,
                display_value=payload.phone,
                provider="fiber_website",
                provider_account_id="fiber.dotmac.ng",
            )
        )
    notes = f"Interest: {payload.interest.label}"
    if payload.message:
        notes += f"\n\n{payload.message}"
    return capture.capture_lead_participant(
        db,
        LeadCaptureRequest(
            party=LeadCapturePartyCreate(
                party_type=PartyType.person,
                display_name=payload.full_name,
                contacts=contacts,
            ),
            title=f"Fiber inquiry — {payload.interest.label}",
            lead_source="Website",
            origin=LeadOriginCaptureCreate(
                capture_method=LeadCaptureMethod.landing_page,
                source_platform=LeadSourcePlatform.website,
                source_interaction_id=delivery_id,
                external_form_id=payload.form_version,
                landing_path="/contact/",
                captured_at=payload.submitted_at,
                capture_source="fiber.website_inquiry",
                capture_reason="Signed fiber.dotmac.ng contact form submission",
            ),
            notes=notes,
        ),
        actor_id=actor,
    )


def receive_fiber_inquiry_committed(
    db: Session,
    command: FiberInquiryIngressCommand,
) -> FiberInquiryIngressOutcome:
    from app.services import team_inbox_processing

    delivery_id = command.delivery_id.strip()
    site_id = command.site_id.strip()
    recorded = team_inbox_observations.record_provider_observation(
        db,
        team_inbox_observations.RecordProviderObservationCommand(
            context=CommandContext.system(
                actor=command.actor,
                scope="team-inbox:fiber-provider-observation",
                reason="record signed fiber website inquiry observation",
                idempotency_key=delivery_id,
            ),
            provider=PROVIDER,
            provider_account_scope=site_id,
            provider_event_id=delivery_id,
            kind=InboxObservationKind.message,
            channel_type=CHANNEL,
            external_message_id=delivery_id,
            observed_at=command.payload.submitted_at,
            payload=team_inbox_observations.FiberWebsiteInquiryObservation(
                full_name=command.payload.full_name,
                email=str(command.payload.email),
                phone=command.payload.phone,
                interest=command.payload.interest.value,
                message=command.payload.message,
                integration_inbox_id=command.integration_inbox_id,
                form_version=command.payload.form_version,
            ),
        ),
    )
    processed = team_inbox_processing.process_provider_observation(
        db,
        observation_id=recorded.observation_id,
        context=CommandContext.system(
            actor="system:team-inbox-fiber-observation-processor",
            scope="team-inbox:provider-consequence",
            reason="resolve committed fiber website inquiry observation",
            idempotency_key=str(recorded.observation_id),
        ),
    )
    if processed.conversation_id is None or processed.message_id is None:
        raise team_inbox_observations.TeamInboxObservationError(
            code="communications.team_inbox_processing.missing_fiber_consequence",
            message="Fiber inquiry processing did not produce Inbox records.",
            details={"observation_id": str(recorded.observation_id)},
        )
    return FiberInquiryIngressOutcome(
        observation_id=recorded.observation_id,
        conversation_id=processed.conversation_id,
        message_id=processed.message_id,
        replayed=recorded.outcome
        is team_inbox_observations.ObservationProcessingOutcome.replayed,
        resolution_status=processed.resolution_status or "unmatched",
    )
