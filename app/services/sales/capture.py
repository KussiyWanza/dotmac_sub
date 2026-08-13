"""Transport-neutral Party-first lead capture owner."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.integration_platform import IntegrationInbox
from app.models.party import PartyContactPointType, PartyRoleStatus, PartyRoleType
from app.models.sales import Lead, LeadOriginCapture
from app.schemas.sales import LeadCaptureRequest
from app.services import party as party_service
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.events import EventType, emit_event
from app.services.integrations import inbox as integration_inbox
from app.services.owner_commands import owner_command_active
from app.services.sales import lifecycle

LEAD_CAPTURE_CAPABILITY = "sales.lead_capture.v1"


class LeadCaptureError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


@dataclass(frozen=True)
class LeadCaptureResult:
    lead: Lead
    party_id: UUID
    origin: LeadOriginCapture
    replayed: bool


def _fingerprint(payload: LeadCaptureRequest) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_capture(
    db: Session, *, source_platform: str, source_interaction_id: str
) -> LeadOriginCapture | None:
    return db.scalars(
        select(LeadOriginCapture).where(
            LeadOriginCapture.source_platform == source_platform,
            LeadOriginCapture.source_interaction_id == source_interaction_id,
        )
    ).one_or_none()


def _replay_result(
    existing: LeadOriginCapture, *, fingerprint: str
) -> LeadCaptureResult:
    if existing.capture_fingerprint != fingerprint:
        raise LeadCaptureError(
            "source_interaction_collision",
            "The source interaction was already captured with different content",
        )
    if existing.lead.party_id is None:
        raise LeadCaptureError(
            "captured_lead_party_missing",
            "Captured Lead is missing its canonical Party",
        )
    return LeadCaptureResult(
        lead=existing.lead,
        party_id=existing.lead.party_id,
        origin=existing,
        replayed=True,
    )


def _normalized_contact_value(db: Session, channel: str, value: str) -> str:
    if channel == PartyContactPointType.email.value:
        normalized = normalize_email_identifier(value)
    elif channel in {
        PartyContactPointType.phone.value,
        PartyContactPointType.sms.value,
        PartyContactPointType.whatsapp.value,
    }:
        normalized = normalize_phone_identifier(
            value, default_country_code=default_country_code(db)
        )
    else:
        normalized = str(value or "").strip()
    if not normalized:
        raise LeadCaptureError(
            "invalid_contact_observation",
            "A captured contact observation could not be normalized",
            kind="invalid",
        )
    return normalized


def capture_lead(
    db: Session,
    payload: LeadCaptureRequest,
    *,
    actor_id: str,
    commit: bool = True,
) -> LeadCaptureResult:
    """Create one Party/Lead/origin chain or return the exact source replay."""

    actor = str(actor_id or "").strip()
    if not actor:
        raise LeadCaptureError(
            "actor_required", "Capture actor is required", kind="invalid"
        )
    source_platform = payload.origin.source_platform.value
    source_interaction_id = str(payload.origin.source_interaction_id or "").strip()
    fingerprint = _fingerprint(payload)
    existing = _existing_capture(
        db,
        source_platform=source_platform,
        source_interaction_id=source_interaction_id,
    )
    if existing is not None:
        return _replay_result(existing, fingerprint=fingerprint)

    try:
        if payload.party_id is not None:
            party_id = payload.party_id
            party_service.ensure_role(
                db,
                party_id=party_id,
                role_type=PartyRoleType.prospect,
                status=PartyRoleStatus.active,
                source="sales.lead_capture",
            )
        else:
            assert payload.party is not None
            party = party_service.create_party(
                db,
                party_type=payload.party.party_type,
                display_name=payload.party.display_name,
                metadata={"created_by": "sales.lead_capture"},
            )
            party_id = party.id
            party_service.ensure_role(
                db,
                party_id=party.id,
                role_type=PartyRoleType.prospect,
                status=PartyRoleStatus.active,
                source="sales.lead_capture",
            )
            for contact in payload.party.contacts:
                channel = contact.channel_type.value
                party_service.add_contact_point(
                    db,
                    party_id=party.id,
                    channel_type=channel,
                    normalized_value=_normalized_contact_value(
                        db, channel, contact.value
                    ),
                    display_value=contact.display_value or contact.value,
                    provider=contact.provider,
                    provider_account_id=contact.provider_account_id,
                    external_subject_id=contact.external_subject_id,
                    is_primary=contact.is_primary,
                    metadata={"observed_by": "sales.lead_capture"},
                )

        origin = payload.origin.model_dump()
        origin["source_interaction_id"] = source_interaction_id
        origin["capture_fingerprint"] = fingerprint
        lead = lifecycle.create_party_lead(
            db,
            party_id=party_id,
            title=payload.title,
            lead_source=payload.lead_source,
            binding_source="sales.lead_capture",
            binding_reason="Exact Party supplied or created for the captured interaction",
            origin_capture=origin,
            region=payload.region,
            address=payload.address,
            notes=payload.notes,
            metadata={"capture_contract_version": 1},
        )
        # ``capture_lead_origin`` writes through the child FK, so the already
        # loaded parent relationship is not guaranteed to refresh in-place.
        capture = db.scalars(
            select(LeadOriginCapture).where(LeadOriginCapture.lead_id == lead.id)
        ).one()
        emit_event(
            db,
            EventType.lead_created,
            {
                "lead_id": str(lead.id),
                "party_id": str(party_id),
                "origin_capture_id": str(capture.id),
                "capture_method": capture.capture_method,
                "source_platform": capture.source_platform,
                "source_interaction_id": capture.source_interaction_id,
            },
            actor=actor,
            subscriber_id=lead.subscriber_id,
        )
        if commit:
            db.commit()
            db.refresh(lead)
            db.refresh(capture)
        return LeadCaptureResult(
            lead=lead,
            party_id=party_id,
            origin=capture,
            replayed=False,
        )
    except IntegrityError as exc:
        if not commit:
            raise LeadCaptureError(
                "capture_conflict",
                "Participant lead capture conflicted with canonical state",
            ) from exc
        db.rollback()
        winner = _existing_capture(
            db,
            source_platform=source_platform,
            source_interaction_id=source_interaction_id,
        )
        if winner is not None:
            return _replay_result(winner, fingerprint=fingerprint)
        raise LeadCaptureError(
            "capture_conflict", "Lead capture conflicted with canonical state"
        ) from exc
    except (lifecycle.LeadLifecycleError, party_service.PartyInvariantError) as exc:
        if commit:
            db.rollback()
        raise LeadCaptureError("capture_rejected", str(exc), kind="invalid") from exc


def capture_lead_participant(
    db: Session,
    payload: LeadCaptureRequest,
    *,
    actor_id: str,
) -> LeadCaptureResult:
    """Stage Party-first Lead capture inside an active coordinating command.

    The caller owns the root transaction. This entry point is flush-only and is
    intentionally used by the Team Inbox fiber inquiry and public chat owners
    so Party, Lead, Conversation, Message, and their provenance link commit
    atomically.
    """

    allowed_owners = {
        "communications.team_inbox_processing",
        "communications.team_inbox_widget",
    }
    if not any(owner_command_active(db, owner=owner) for owner in allowed_owners):
        raise LeadCaptureError(
            "owner_command_required",
            "Participant lead capture requires an active owner command",
        )
    return capture_lead(db, payload, actor_id=actor_id, commit=False)


def capture_verified_receipt(
    db: Session,
    *,
    receipt_id: UUID,
    payload: LeadCaptureRequest,
    actor_id: str = "integration.lead_capture",
) -> LeadCaptureResult:
    """Consume one verified integration receipt through the same capture owner."""

    receipt = db.scalars(
        select(IntegrationInbox)
        .where(IntegrationInbox.id == receipt_id)
        .with_for_update()
    ).one_or_none()
    if receipt is None:
        raise LeadCaptureError(
            "receipt_not_found", "Integration receipt not found", kind="not_found"
        )
    if receipt.capability_binding.capability_id != LEAD_CAPTURE_CAPABILITY:
        raise LeadCaptureError(
            "wrong_capability",
            "Integration receipt is not a lead-capture receipt",
            kind="invalid",
        )
    if str(payload.origin.source_interaction_id) != receipt.provider_event_id:
        raise LeadCaptureError(
            "receipt_identity_mismatch",
            "Capture interaction id does not match the verified provider event",
            kind="invalid",
        )
    if payload.origin.integration_inbox_id not in {None, receipt.id}:
        raise LeadCaptureError(
            "receipt_identity_mismatch",
            "Capture integration receipt does not match the requested receipt",
            kind="invalid",
        )
    normalized = payload.model_copy(
        update={
            "origin": payload.origin.model_copy(
                update={"integration_inbox_id": receipt.id}
            )
        }
    )
    if receipt.state != "processed":
        integration_inbox.claim_for_processing(receipt)
    result = capture_lead(db, normalized, actor_id=actor_id, commit=False)
    integration_inbox.mark_processed(
        receipt,
        consequence={
            "lead_id": str(result.lead.id),
            "party_id": str(result.party_id),
            "origin_capture_id": str(result.origin.id),
        },
    )
    db.commit()
    return result
