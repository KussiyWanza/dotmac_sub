"""Atomic admin authoring for one Person Party and one Lead."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import RegionZone
from app.models.organization import Organization, OrganizationAccountType
from app.models.party import (
    Party,
    PartyContactPointType,
    PartyIdentityStatus,
    PartyRelationshipType,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
)
from app.models.sales import (
    Lead,
    LeadCaptureMethod,
    LeadSourcePlatform,
    LeadStatus,
    Pipeline,
    PipelineStage,
)
from app.models.service_team import ServiceTeamMember
from app.models.subscriber import Reseller
from app.models.system_user import SystemUser
from app.services import conversation_lead_relationships
from app.services import party as party_service
from app.services.audit_adapter import stage_audit_event
from app.services.credential_crypto import encrypt_credential
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales import lifecycle

_AUTHOR_LEAD = OwnerCommandDefinition(
    owner="sales.lead_authoring",
    concern="atomic admin Person and Lead authoring",
    name="author_lead",
)
_EMAIL = TypeAdapter(EmailStr)
_NIN = re.compile(r"^[0-9]{11}$")
_COUNTRY = re.compile(r"^[A-Za-z]{1,2}$")
_ELIGIBLE_PARTY_STATUSES = {
    PartyIdentityStatus.active.value,
    PartyIdentityStatus.quarantined.value,
}


class LeadAuthoringError(DomainError):
    """Stable, form-safe rejection from the Lead authoring owner."""


@dataclass(frozen=True, slots=True)
class LeadContactDraft:
    values: tuple[str, ...]
    primary_index: int


@dataclass(frozen=True, slots=True)
class LeadPersonDraft:
    display_name: str
    emails: LeadContactDraft
    phones: LeadContactDraft
    whatsapp_phone_indices: tuple[int, ...]
    address_line1: str | None
    address_line2: str | None
    date_of_birth: date | None
    gender: str
    nin: str | None
    city: str | None
    postal_code: str | None
    country_code: str | None
    organization_id: UUID | None
    reseller_id: UUID | None


@dataclass(frozen=True, slots=True)
class AuthorLeadCommand:
    context: CommandContext
    lead_id: UUID
    actor_system_user_id: UUID
    status: LeadStatus
    owner_system_user_id: UUID | None
    pipeline_id: UUID | None
    stage_id: UUID | None
    lead_source: str | None
    region_zone_id: UUID | None
    estimated_value: Decimal | None
    currency: str | None
    probability: int | None
    expected_close_date: date | None
    lost_reason: str | None
    notes: str | None
    is_active: bool
    person: LeadPersonDraft
    origin_conversation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AuthorLeadOutcome:
    lead_id: UUID
    party_id: UUID
    replayed: bool
    reseller_routed: bool


def _error(
    suffix: str,
    message: str,
    *,
    field: str | None = None,
    **details: object,
) -> LeadAuthoringError:
    payload = dict(details)
    if field:
        payload["field"] = field
    return LeadAuthoringError(
        code=f"sales.lead_authoring.{suffix}", message=message, details=payload
    )


def split_display_name(value: str | None) -> tuple[str, str, str]:
    """Return canonical display, first, and last names for admin Lead capture."""

    clean = " ".join(str(value or "").split())
    if len(clean) > 120:
        raise _error(
            "display_name_too_long",
            "Display Name must be 120 characters or fewer.",
            field="display_name",
        )
    if not clean:
        return "Unknown", "Unknown", "Unknown"
    first, separator, remainder = clean.partition(" ")
    return clean, first, remainder if separator else "Unknown"


def _fingerprint(command: AuthorLeadCommand) -> str:
    payload = {
        "actor": str(command.actor_system_user_id),
        "status": command.status.value,
        "owner": str(command.owner_system_user_id)
        if command.owner_system_user_id
        else None,
        "pipeline": str(command.pipeline_id) if command.pipeline_id else None,
        "stage": str(command.stage_id) if command.stage_id else None,
        "lead_source": command.lead_source,
        "region_zone": str(command.region_zone_id) if command.region_zone_id else None,
        "estimated_value": str(command.estimated_value)
        if command.estimated_value is not None
        else None,
        "currency": command.currency,
        "probability": command.probability,
        "expected_close_date": command.expected_close_date.isoformat()
        if command.expected_close_date
        else None,
        "lost_reason": command.lost_reason,
        "notes": command.notes,
        "is_active": command.is_active,
        "origin_conversation_id": (
            str(command.origin_conversation_id)
            if command.origin_conversation_id is not None
            else None
        ),
        "person": {
            "display_name": command.person.display_name,
            "emails": command.person.emails.values,
            "primary_email": command.person.emails.primary_index,
            "phones": command.person.phones.values,
            "primary_phone": command.person.phones.primary_index,
            "whatsapp": command.person.whatsapp_phone_indices,
            "address_line1": command.person.address_line1,
            "address_line2": command.person.address_line2,
            "date_of_birth": command.person.date_of_birth.isoformat()
            if command.person.date_of_birth
            else None,
            "gender": command.person.gender,
            "nin": command.person.nin,
            "city": command.person.city,
            "postal_code": command.person.postal_code,
            "country_code": command.person.country_code,
            "organization_id": str(command.person.organization_id)
            if command.person.organization_id
            else None,
            "reseller_id": str(command.person.reseller_id)
            if command.person.reseller_id
            else None,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _active_actor(db: Session, actor_id: UUID) -> SystemUser:
    actor = db.scalars(
        select(SystemUser)
        .where(SystemUser.id == actor_id, SystemUser.is_active.is_(True))
        .with_for_update()
    ).one_or_none()
    if actor is None:
        raise _error(
            "actor_not_eligible",
            "The authenticated staff user cannot create Leads.",
        )
    return actor


def _eligible_owner(db: Session, owner_id: UUID | None) -> UUID | None:
    if owner_id is None:
        return None
    owner = db.scalars(
        select(SystemUser)
        .join(
            ServiceTeamMember,
            ServiceTeamMember.person_id == SystemUser.person_party_id,
        )
        .where(
            SystemUser.id == owner_id,
            SystemUser.is_active.is_(True),
            ServiceTeamMember.is_active.is_(True),
        )
        .with_for_update()
    ).first()
    if owner is None:
        raise _error(
            "owner_not_eligible",
            "Select an active eligible Lead owner.",
            field="owner_agent_id",
        )
    return owner.id


def _pipeline_stage(
    db: Session, pipeline_id: UUID | None, stage_id: UUID | None
) -> tuple[UUID | None, UUID | None]:
    if pipeline_id is None and stage_id is None:
        return None, None
    if pipeline_id is None or stage_id is None:
        field = "stage_id" if stage_id is None else "pipeline_id"
        raise _error(
            "pipeline_stage_incomplete",
            "Select both a Pipeline and one of its Stages.",
            field=field,
        )
    pipeline = db.scalars(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.is_active.is_(True))
    ).one_or_none()
    stage = db.scalars(
        select(PipelineStage).where(
            PipelineStage.id == stage_id, PipelineStage.is_active.is_(True)
        )
    ).one_or_none()
    if pipeline is None:
        raise _error(
            "pipeline_not_active",
            "Select an active Pipeline.",
            field="pipeline_id",
        )
    if stage is None or stage.pipeline_id != pipeline.id:
        raise _error(
            "stage_pipeline_mismatch",
            "Select a Stage from the chosen Pipeline.",
            field="stage_id",
        )
    return pipeline.id, stage.id


def _region(db: Session, region_zone_id: UUID | None) -> RegionZone | None:
    if region_zone_id is None:
        return None
    region = db.scalars(
        select(RegionZone).where(
            RegionZone.id == region_zone_id, RegionZone.is_active.is_(True)
        )
    ).one_or_none()
    if region is None:
        raise _error(
            "region_not_active",
            "Select an active configured Region.",
            field="region_zone_id",
        )
    return region


def _organization(db: Session, organization_id: UUID | None) -> Organization | None:
    if organization_id is None:
        return None
    organization = db.scalars(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.is_active.is_(True),
        )
    ).one_or_none()
    if organization is None:
        raise _error(
            "organization_not_active",
            "Select an active accessible Organization.",
            field="organization_id",
        )
    if organization.party_id is not None:
        organization_party = db.get(Party, organization.party_id)
        if (
            organization_party is None
            or organization_party.party_type != PartyType.organization.value
            or organization_party.status not in _ELIGIBLE_PARTY_STATUSES
        ):
            raise _error(
                "organization_party_ineligible",
                "The selected Organization does not have a usable identity.",
                field="organization_id",
            )
    return organization


def _reseller(db: Session, reseller_id: UUID | None) -> Reseller | None:
    if reseller_id is None:
        return None
    reseller = db.scalars(
        select(Reseller)
        .where(
            Reseller.id == reseller_id,
            Reseller.is_active.is_(True),
            Reseller.is_house.is_(False),
        )
        .with_for_update()
    ).one_or_none()
    if reseller is None:
        raise _error(
            "reseller_not_active",
            "Select an active reseller.",
            field="reseller_id",
        )
    return reseller


def _stable_unique(values: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
    seen: set[str] = set()
    result: list[tuple[int, str]] = []
    for index, value in enumerate(values):
        if value and value not in seen:
            seen.add(value)
            result.append((index, value))
    return tuple(result)


def _emails(draft: LeadContactDraft) -> tuple[tuple[str, ...], int]:
    normalized: list[str] = []
    for raw in draft.values:
        value = normalize_email_identifier(raw)
        if not value:
            normalized.append("")
            continue
        try:
            value = str(_EMAIL.validate_python(value)).lower()
        except ValidationError:
            raise _error(
                "email_invalid",
                "Enter a valid email address.",
                field="emails",
            ) from None
        normalized.append(value)
    unique = _stable_unique(tuple(normalized))
    values = tuple(value for _index, value in unique)
    selected_value = (
        normalized[draft.primary_index]
        if 0 <= draft.primary_index < len(normalized)
        else ""
    )
    primary = values.index(selected_value) if selected_value in values else 0
    # Email is contact information rather than identity. Multiple People may
    # legitimately share one address, especially customers managed by a reseller.
    return values, primary


def _phones(
    db: Session, draft: LeadContactDraft
) -> tuple[tuple[str, ...], int, dict[int, int]]:
    country = default_country_code(db)
    normalized: list[str] = []
    for raw in draft.values:
        value = normalize_phone_identifier(raw, default_country_code=country) or ""
        if value:
            digits = re.sub(r"\D", "", value)
            if not 8 <= len(digits) <= 15:
                raise _error(
                    "phone_invalid",
                    "Phone numbers must contain between 8 and 15 digits.",
                    field="phones",
                )
        normalized.append(value)
    unique = _stable_unique(tuple(normalized))
    values = tuple(value for _index, value in unique)
    original_to_unique = {
        original: position for position, (original, _value) in enumerate(unique)
    }
    selected_value = (
        normalized[draft.primary_index]
        if 0 <= draft.primary_index < len(normalized)
        else ""
    )
    primary = values.index(selected_value) if selected_value in values else 0
    return values, primary, original_to_unique


def _validate_scalar_fields(
    command: AuthorLeadCommand,
) -> tuple[str | None, str | None]:
    if command.status == LeadStatus.won:
        raise _error(
            "status_not_allowed",
            "A Lead becomes Won only through Quote acceptance.",
            field="status",
        )
    if command.estimated_value is not None and (
        not command.estimated_value.is_finite() or command.estimated_value < 0
    ):
        raise _error(
            "estimated_value_invalid",
            "Estimated Value cannot be negative.",
            field="estimated_value",
        )
    currency = (command.currency or "").strip().upper() or None
    if currency is not None and (
        len(currency) != 3 or not currency.isascii() or not currency.isalpha()
    ):
        raise _error(
            "currency_invalid",
            "Currency must contain exactly three alphabetic characters.",
            field="currency",
        )
    if command.probability is not None and not 0 <= command.probability <= 100:
        raise _error(
            "probability_invalid",
            "Probability must be between 0 and 100.",
            field="probability",
        )
    person = command.person
    for field, value, maximum in (
        ("address_line1", person.address_line1, 120),
        ("address_line2", person.address_line2, 120),
        ("city", person.city, 80),
        ("postal_code", person.postal_code, 20),
    ):
        if value and len(value) > maximum:
            raise _error(
                f"{field}_too_long",
                f"{field.replace('_', ' ').title()} must be {maximum} characters or fewer.",
                field=field,
            )
    if person.date_of_birth and person.date_of_birth > date.today():
        raise _error(
            "date_of_birth_future",
            "Date of Birth cannot be in the future.",
            field="date_of_birth",
        )
    if person.gender not in {"unknown", "female", "male", "non_binary", "other"}:
        raise _error("gender_invalid", "Select a valid Gender.", field="gender")
    if person.nin and not _NIN.fullmatch(person.nin):
        raise _error("nin_invalid", "NIN must contain exactly 11 digits.", field="nin")
    country_code = (person.country_code or "").strip().upper() or None
    if country_code and not _COUNTRY.fullmatch(country_code):
        raise _error(
            "country_code_invalid",
            "Country Code must contain one or two alphabetic characters.",
            field="country_code",
        )
    return currency, country_code


def _inbox_lead_source(channel_type: str) -> str:
    return {
        "email": "Email",
        "whatsapp": "Whatsapp",
        "facebook_messenger": "Facebook",
        "facebook_comment": "Facebook",
        "instagram_dm": "Instagram",
        "instagram_comment": "Instagram",
    }.get(channel_type, "Website")


def _operation(db: Session, command: AuthorLeadCommand) -> AuthorLeadOutcome:
    actor = _active_actor(db, command.actor_system_user_id)
    fingerprint = _fingerprint(command)
    replay = db.scalars(
        select(Lead).where(Lead.id == command.lead_id).with_for_update()
    ).one_or_none()
    if replay is not None:
        metadata = replay.metadata_ if isinstance(replay.metadata_, dict) else {}
        if metadata.get("authoring_fingerprint") != fingerprint:
            raise _error(
                "submission_conflict",
                "This Lead submission was already used with different values.",
            )
        if replay.party_id is None:
            raise _error(
                "replay_party_missing", "The saved Lead is missing its Person."
            )
        if command.origin_conversation_id is not None:
            conversation_lead_relationships.link_conversation_lead_participant(
                db,
                conversation_lead_relationships.ConversationLeadLinkCommand(
                    context=command.context,
                    conversation_id=command.origin_conversation_id,
                    lead_id=replay.id,
                    party_id=replay.party_id,
                    actor_person_id=actor.person_party_id,
                    source=conversation_lead_relationships.ConversationLeadLinkSource.inbox_lead_authoring,
                    reason="Inbox Party and Lead authoring replay restored exact provenance",
                ),
            )
        return AuthorLeadOutcome(
            lead_id=replay.id,
            party_id=replay.party_id,
            replayed=True,
            reseller_routed=bool(metadata.get("communication_routed_through_reseller")),
        )

    conversation = (
        conversation_lead_relationships.require_new_prospect_conversation(
            db, command.origin_conversation_id
        )
        if command.origin_conversation_id is not None
        else None
    )
    currency, country_code = _validate_scalar_fields(command)
    owner_id = _eligible_owner(db, command.owner_system_user_id)
    pipeline_id, stage_id = _pipeline_stage(db, command.pipeline_id, command.stage_id)
    region = _region(db, command.region_zone_id)
    organization = _organization(db, command.person.organization_id)
    reseller = _reseller(db, command.person.reseller_id)
    reseller_routed = bool(
        reseller
        or (
            organization
            and organization.account_type == OrganizationAccountType.reseller.value
        )
    )
    emails, primary_email = _emails(command.person.emails)
    phones, primary_phone, phone_indexes = _phones(db, command.person.phones)
    whatsapp_indexes = tuple(
        dict.fromkeys(
            phone_indexes[index]
            for index in command.person.whatsapp_phone_indices
            if index in phone_indexes
        )
    )
    display_name, first_name, last_name = split_display_name(
        command.person.display_name
    )
    party_id = uuid5(command.lead_id, "admin-lead-person")
    person_metadata: dict[str, object] = {
        "profile_version": 1,
        "first_name": first_name,
        "last_name": last_name,
        "address_line1": command.person.address_line1,
        "address_line2": command.person.address_line2,
        "date_of_birth": command.person.date_of_birth.isoformat()
        if command.person.date_of_birth
        else None,
        "gender": command.person.gender,
        "nin_encrypted": encrypt_credential(command.person.nin),
        "city": command.person.city,
        "postal_code": command.person.postal_code,
        "country_code": country_code,
        "organization_id": str(organization.id) if organization else None,
        "reseller_id": str(reseller.id) if reseller else None,
        "primary_email": emails[primary_email] if emails else None,
        "primary_phone": phones[primary_phone] if phones else None,
        "identity_managed_by": "sub",
        "communication_routed_through_reseller": reseller_routed,
    }
    party = party_service.create_party(
        db,
        party_id=party_id,
        party_type=PartyType.person,
        display_name=display_name,
        metadata=person_metadata,
    )
    party_service.ensure_role(
        db,
        party_id=party.id,
        role_type=PartyRoleType.prospect,
        status=PartyRoleStatus.active,
        source="sales.lead_authoring",
    )
    for index, email in enumerate(emails):
        party_service.add_contact_point(
            db,
            party_id=party.id,
            channel_type=PartyContactPointType.email,
            normalized_value=email,
            display_value=email,
            is_primary=index == primary_email,
            metadata={"captured_by": "sales.lead_authoring"},
        )
    for index, phone in enumerate(phones):
        party_service.add_contact_point(
            db,
            party_id=party.id,
            channel_type=PartyContactPointType.phone,
            normalized_value=phone,
            display_value=phone,
            is_primary=index == primary_phone,
            metadata={"captured_by": "sales.lead_authoring"},
        )
        if index in whatsapp_indexes:
            party_service.add_contact_point(
                db,
                party_id=party.id,
                channel_type=PartyContactPointType.whatsapp,
                normalized_value=phone,
                display_value=phone,
                is_primary=False,
                metadata={"captured_by": "sales.lead_authoring"},
            )
    if organization and organization.party_id:
        party_service.relate_parties(
            db,
            subject_party_id=party.id,
            object_party_id=organization.party_id,
            relationship_type=PartyRelationshipType.contact_for,
            source="sales.lead_authoring",
            metadata={"organization_profile_id": str(organization.id)},
        )

    source = (
        _inbox_lead_source(conversation.channel_type)
        if conversation is not None
        else command.lead_source or "Portal"
    )
    lead_metadata: dict[str, object] = {
        "authoring_key": str(command.lead_id),
        "authoring_fingerprint": fingerprint,
        "authoring_actor_system_user_id": str(actor.id),
        "person_party_id": str(party.id),
        "organization_id": str(organization.id) if organization else None,
        "reseller_id": str(reseller.id) if reseller else None,
        "region_zone_id": str(region.id) if region else None,
        "communication_routed_through_reseller": reseller_routed,
        "origin_conversation_id": (
            str(conversation.id) if conversation is not None else None
        ),
    }
    origin = {
        "capture_method": (
            LeadCaptureMethod.inbox_form.value
            if conversation is not None
            else LeadCaptureMethod.agent_declared.value
        ),
        "source_platform": (
            LeadSourcePlatform.team_inbox.value
            if conversation is not None
            else LeadSourcePlatform.agent.value
        ),
        "source_interaction_id": (
            f"inbox-conversation:{conversation.id}"
            if conversation is not None
            else f"admin-lead:{command.lead_id}"
        ),
        "capture_fingerprint": fingerprint,
        "capture_source": (
            "communications.inbox_lead_actions"
            if conversation is not None
            else "admin_sales_lead_form"
        ),
        "capture_reason": (
            "Authorized operator created a new prospect from an Inbox conversation"
            if conversation is not None
            else "Authenticated staff created the Lead"
        ),
    }
    lead = lifecycle.create_party_lead(
        db,
        lead_id=command.lead_id,
        party_id=party.id,
        title=display_name,
        lead_source=source,
        binding_source="sales.lead_authoring",
        binding_reason="Person Party created atomically with the admin Lead",
        origin_capture=origin,
        region=region.name if region else None,
        notes=(command.notes or "").strip() or None,
        metadata=lead_metadata,
        owner_agent_id=owner_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        status=command.status,
        estimated_value=command.estimated_value,
        currency=currency,
        probability=command.probability,
        expected_close_date=command.expected_close_date,
        lost_reason=(
            (command.lost_reason or "").strip() or None
            if command.status == LeadStatus.lost
            else None
        ),
        is_active=command.is_active,
        reseller_id=reseller.id if reseller else None,
    )
    emit_event(
        db,
        EventType.lead_created,
        {
            "lead_id": str(lead.id),
            "party_id": str(party.id),
            "status": lead.status,
            "lead_source": lead.lead_source,
            "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else None,
        },
        actor=command.context.actor,
    )
    stage_audit_event(
        db,
        action="lead.created",
        entity_type="lead",
        entity_id=str(lead.id),
        actor_id=str(actor.id),
        request_id=str(command.context.command_id),
        metadata={
            "party_id": str(party.id),
            "status": lead.status,
            "channel_count": 0
            if reseller_routed
            else len(emails) + len(phones) + len(whatsapp_indexes),
            "reseller_routed": reseller_routed,
        },
    )
    if conversation is not None:
        conversation_lead_relationships.link_conversation_lead_participant(
            db,
            conversation_lead_relationships.ConversationLeadLinkCommand(
                context=command.context,
                conversation_id=conversation.id,
                lead_id=lead.id,
                party_id=party.id,
                actor_person_id=actor.person_party_id,
                source=conversation_lead_relationships.ConversationLeadLinkSource.inbox_lead_authoring,
                reason="Party and Lead created atomically from this Inbox conversation",
            ),
        )
    db.flush()
    return AuthorLeadOutcome(
        lead_id=lead.id,
        party_id=party.id,
        replayed=False,
        reseller_routed=reseller_routed,
    )


def author_lead(db: Session, command: AuthorLeadCommand) -> AuthorLeadOutcome:
    """Create one Person Party, its contacts, origin, and Lead atomically."""

    return execute_owner_command(
        db,
        definition=_AUTHOR_LEAD,
        context=command.context,
        operation=lambda: _operation(db, command),
    )
