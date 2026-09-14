"""Customer-only Inbox completion readiness and canonical profile command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.lead_intake import LeadIntakeInvitation
from app.models.organization import Organization
from app.models.party import Party, PartyContactPoint, PartyContactPointType
from app.models.subscriber import (
    Address,
    AddressType,
    Gender,
    Subscriber,
    SubscriberContact,
)
from app.models.team_inbox import (
    InboxConversation,
    InboxCustomerCompletionPolicyVersion,
)
from app.services import (
    conversation_lead_relationships,
    customer_identity_resolution,
    party,
)
from app.services import (
    customer_canonical_profile_patch as canonical_profile_patch,
)
from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    BlockerEvidence,
    NextAction,
    ReadinessState,
)
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.customer_identity_normalization import (
    is_placeholder_customer_name,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "communications.team_inbox_customer_completion"
CONCERN = "canonical Inbox Customer profile completion coordination"
ACTION_KEY = "team_inbox.resolve"


class InboxIdentityClassification(StrEnum):
    customer = "customer"
    lead = "lead"
    unresolved = "unresolved"
    ambiguous = "ambiguous"


class CustomerProfileField(StrEnum):
    name = "name"
    phone = "phone"
    address = "address"
    email = "email"
    whatsapp = "whatsapp"
    organization = "organization"
    city_region = "city_region"
    country = "country"
    date_of_birth = "date_of_birth"
    gender = "gender"
    nin = "nin"


FIELD_LABELS: dict[CustomerProfileField, str] = {
    CustomerProfileField.name: "Name",
    CustomerProfileField.phone: "Phone Number",
    CustomerProfileField.address: "Address",
    CustomerProfileField.email: "Email",
    CustomerProfileField.whatsapp: "WhatsApp",
    CustomerProfileField.organization: "Organization",
    CustomerProfileField.city_region: "City/Region",
    CustomerProfileField.country: "Country",
    CustomerProfileField.date_of_birth: "DOB",
    CustomerProfileField.gender: "Gender",
    CustomerProfileField.nin: "NIN",
}


class InboxCustomerCompletionError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class CustomerFieldReadiness:
    field: CustomerProfileField
    label: str
    complete: bool


@dataclass(frozen=True, slots=True)
class InboxCustomerResolutionReadiness:
    classification: InboxIdentityClassification
    policy_version: int | None
    fields: tuple[CustomerFieldReadiness, ...]
    readiness: ActionReadiness

    @property
    def can_agent_resolve(self) -> bool:
        return self.readiness.is_ready

    @property
    def missing_fields(self) -> tuple[CustomerProfileField, ...]:
        return tuple(item.field for item in self.fields if not item.complete)


@dataclass(frozen=True, slots=True)
class CustomerProfileValues:
    name: str | None = None
    phone: str | None = None
    address: str | None = None
    email: str | None = None
    whatsapp: str | None = None
    organization: str | None = None
    city_region: str | None = None
    country: str | None = None
    date_of_birth: str | None = None
    gender: str | None = None
    nin: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteInboxCustomerProfileCommand:
    context: CommandContext
    conversation_id: UUID
    customer_id: UUID
    values: CustomerProfileValues
    submitted_fields: frozenset[CustomerProfileField]
    confirmed_replacements: frozenset[CustomerProfileField]
    actor_person_id: UUID | None
    actor_type: AuditActorType
    decision_source: str


@dataclass(frozen=True, slots=True)
class CustomerProfileChange:
    field: CustomerProfileField
    previous_value: str | None
    new_value: str | None


@dataclass(frozen=True, slots=True)
class CompleteInboxCustomerProfileOutcome:
    conversation_id: UUID
    customer_id: UUID
    party_id: UUID | None
    changes: tuple[CustomerProfileChange, ...]
    readiness: InboxCustomerResolutionReadiness


_COMPLETE = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="complete_inbox_customer_profile",
)


def _error(suffix: str, message: str, **details: object) -> DomainError:
    return InboxCustomerCompletionError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _clean(value: str | None) -> str | None:
    cleaned = " ".join((value or "").split())
    return cleaned or None


def _party_points(db: Session, party_id: UUID | None) -> dict[str, str]:
    if party_id is None:
        return {}
    rows = db.scalars(
        select(PartyContactPoint)
        .where(
            PartyContactPoint.party_id == party_id,
            PartyContactPoint.is_active.is_(True),
        )
        .order_by(PartyContactPoint.is_primary.desc(), PartyContactPoint.created_at)
    ).all()
    result: dict[str, str] = {}
    for row in rows:
        result.setdefault(row.channel_type, row.display_value or row.normalized_value)
    return result


def _canonical_values(
    db: Session, subscriber: Subscriber, canonical_party: Party | None
) -> dict[CustomerProfileField, str | None]:
    metadata = (
        dict(canonical_party.metadata_)
        if canonical_party is not None and isinstance(canonical_party.metadata_, dict)
        else {}
    )
    points = _party_points(db, canonical_party.id if canonical_party else None)
    address_record = db.scalar(
        select(Address)
        .where(
            Address.subscriber_id == subscriber.id,
            Address.address_type == AddressType.service,
        )
        .order_by(Address.is_primary.desc(), Address.created_at, Address.id)
    )
    contact = db.scalar(
        select(SubscriberContact)
        .where(SubscriberContact.subscriber_id == subscriber.id)
        .order_by(SubscriberContact.created_at, SubscriberContact.id)
    )
    organization = (
        db.get(Organization, subscriber.organization_id)
        if subscriber.organization_id is not None
        else None
    )
    name = (
        _clean(canonical_party.display_name if canonical_party is not None else None)
        or _clean(subscriber.display_name)
        or _clean(subscriber.full_name)
    )
    if is_placeholder_customer_name(name):
        name = None
    phone = _clean(points.get("phone")) or _clean(subscriber.phone)
    address = (
        _clean(str(metadata.get("address_line1") or ""))
        or _clean(subscriber.address_line1)
        or _clean(address_record.address_line1 if address_record is not None else None)
    )
    email = _clean(points.get("email")) or _clean(subscriber.email)
    return {
        CustomerProfileField.name: name,
        CustomerProfileField.phone: phone,
        CustomerProfileField.address: address,
        CustomerProfileField.email: email,
        CustomerProfileField.whatsapp: _clean(points.get("whatsapp"))
        or _clean(contact.whatsapp if contact is not None else None),
        CustomerProfileField.organization: _clean(subscriber.company_name)
        or _clean(organization.name if organization is not None else None),
        CustomerProfileField.city_region: _clean(
            str(metadata.get("city") or metadata.get("region") or "")
        )
        or _clean(subscriber.city)
        or _clean(subscriber.region)
        or _clean(address_record.city if address_record is not None else None)
        or _clean(address_record.region if address_record is not None else None),
        CustomerProfileField.country: _clean(str(metadata.get("country_code") or ""))
        or _clean(subscriber.country_code)
        or _clean(address_record.country_code if address_record is not None else None),
        CustomerProfileField.date_of_birth: (
            str(subscriber.date_of_birth) if subscriber.date_of_birth else None
        ),
        CustomerProfileField.gender: (
            subscriber.gender.value
            if subscriber.gender is not None and subscriber.gender is not Gender.unknown
            else None
        ),
        CustomerProfileField.nin: _clean(subscriber.nin),
    }


def classification(
    db: Session, conversation: InboxConversation
) -> InboxIdentityClassification:
    evidence = conversation_lead_relationships.relationship_evidence(db, conversation)
    if evidence.identity_conflict or evidence.lead_party_mismatch:
        return InboxIdentityClassification.ambiguous
    # The active conversation-to-Lead relationship is the explicit context for
    # this conversation. A Party may also be a Customer without turning a new
    # sales opportunity into a Customer-support completion workflow.
    if evidence.active_lead_id is not None:
        return InboxIdentityClassification.lead
    completed_lead_id = db.scalar(
        select(LeadIntakeInvitation.lead_id)
        .where(
            LeadIntakeInvitation.conversation_id == conversation.id,
            LeadIntakeInvitation.status == "completed",
            LeadIntakeInvitation.lead_id.is_not(None),
        )
        .limit(1)
    )
    if completed_lead_id is not None:
        return InboxIdentityClassification.lead
    if conversation.subscriber_id is not None:
        return InboxIdentityClassification.customer
    party_ids = evidence.authoritative_party_ids
    if len(party_ids) > 1:
        return InboxIdentityClassification.ambiguous
    return InboxIdentityClassification.unresolved


def canonical_customer_values(
    db: Session, conversation: InboxConversation
) -> dict[CustomerProfileField, str | None]:
    """Return typed canonical values for the Inbox completion editor."""

    if conversation.subscriber_id is None:
        return {}
    subscriber = db.get(Subscriber, conversation.subscriber_id)
    if subscriber is None:
        return {}
    canonical_party = (
        db.get(Party, subscriber.party_id) if subscriber.party_id is not None else None
    )
    return _canonical_values(db, subscriber, canonical_party)


def resolution_readiness_for_conversation(
    db: Session, conversation_id: UUID
) -> InboxCustomerResolutionReadiness | None:
    """Resolve readiness without making the UI adapter a model reader."""

    conversation = db.get(InboxConversation, conversation_id)
    if conversation is None:
        return None
    return resolution_readiness(db, conversation)


def resolution_readiness(
    db: Session,
    conversation: InboxConversation,
    *,
    evaluated_at: datetime | None = None,
) -> InboxCustomerResolutionReadiness:
    """Return the authoritative Customer-only manual-resolution verdict."""

    observed_at = evaluated_at or datetime.now(UTC)
    identity = classification(db, conversation)
    if identity is InboxIdentityClassification.lead:
        return InboxCustomerResolutionReadiness(
            classification=identity,
            policy_version=None,
            fields=(),
            readiness=ActionReadiness(
                action_key=ACTION_KEY,
                subject_type="inbox_conversation",
                subject_id=str(conversation.id),
                owner=OWNER,
                state=ReadinessState.ready,
                evaluated_at=observed_at,
            ),
        )
    blockers: list[ActionableBlocker] = []
    field_states: list[CustomerFieldReadiness] = []
    policy_version: int | None = None
    if identity is not InboxIdentityClassification.customer:
        blockers.append(
            ActionableBlocker(
                code="inbox_identity_required",
                owner=OWNER,
                customer_message="Identify this conversation as a Customer or Lead.",
                staff_detail="Resolution requires an unambiguous Customer or Lead link.",
                evidence=BlockerEvidence(
                    summary=f"Current classification is {identity.value}."
                ),
            )
        )
    else:
        policy = (
            db.get(
                InboxCustomerCompletionPolicyVersion,
                conversation.customer_completion_policy_version_id,
            )
            if conversation.customer_completion_policy_version_id is not None
            else None
        )
        subscriber = db.get(Subscriber, conversation.subscriber_id)
        if policy is None or subscriber is None:
            blockers.append(
                ActionableBlocker(
                    code="customer_completion_policy_unavailable",
                    owner=OWNER,
                    customer_message="Customer completion requirements are unavailable.",
                    staff_detail="The snapshotted policy or linked Customer is missing.",
                    evidence=BlockerEvidence(
                        summary="Required canonical input is absent."
                    ),
                )
            )
        else:
            policy_version = policy.version
            canonical_party = (
                db.get(Party, subscriber.party_id)
                if subscriber.party_id is not None
                else None
            )
            values = _canonical_values(db, subscriber, canonical_party)
            for raw_field in policy.required_fields:
                try:
                    field = CustomerProfileField(raw_field)
                except ValueError:
                    blockers.append(
                        ActionableBlocker(
                            code=f"unsupported_customer_field_{raw_field}",
                            owner=OWNER,
                            customer_message="A configured Customer requirement cannot be checked.",
                            staff_detail=f"Unsupported policy field: {raw_field}",
                            evidence=BlockerEvidence(
                                summary="Policy field is unsupported."
                            ),
                        )
                    )
                    continue
                complete = bool(_clean(values.get(field)))
                field_states.append(
                    CustomerFieldReadiness(field, FIELD_LABELS[field], complete)
                )
                if not complete:
                    blockers.append(
                        ActionableBlocker(
                            code=f"customer_{field.value}_required",
                            owner=OWNER,
                            customer_message=f"{FIELD_LABELS[field]} is required.",
                            staff_detail=(
                                f"Customer policy version {policy.version} requires "
                                f"{FIELD_LABELS[field]}."
                            ),
                            evidence=BlockerEvidence(
                                summary=f"Canonical Customer {field.value} is empty."
                            ),
                        )
                    )
    state = ReadinessState.blocked if blockers else ReadinessState.ready
    next_actions = (
        (
            NextAction(
                key="complete_customer_profile",
                label="Complete customer information",
                owner=OWNER,
                url=f"/admin/inbox?c={conversation.id}",
            ),
        )
        if blockers
        else ()
    )
    return InboxCustomerResolutionReadiness(
        classification=identity,
        policy_version=policy_version,
        fields=tuple(field_states),
        readiness=ActionReadiness(
            action_key=ACTION_KEY,
            subject_type="inbox_conversation",
            subject_id=str(conversation.id),
            owner=OWNER,
            state=state,
            evaluated_at=observed_at,
            blockers=tuple(blockers),
            next_actions=next_actions,
        ),
    )


def require_agent_resolution_ready(
    db: Session, conversation: InboxConversation
) -> InboxCustomerResolutionReadiness:
    verdict = resolution_readiness(db, conversation)
    if not verdict.can_agent_resolve:
        message = (
            "Cannot resolve conversation. Complete the required Customer information."
            if verdict.classification is InboxIdentityClassification.customer
            else "Cannot resolve conversation. Identify the contact as a Customer or Lead."
        )
        raise _error(
            "resolution_blocked",
            message,
            conversation_id=str(conversation.id),
            classification=verdict.classification.value,
            missing_fields=[field.value for field in verdict.missing_fields],
            blockers=[blocker.code for blocker in verdict.readiness.blocking_blockers],
        )
    return verdict


def _assert_replacement(
    *,
    field: CustomerProfileField,
    previous: str | None,
    proposed: str | None,
    confirmed: frozenset[CustomerProfileField],
) -> None:
    if previous is not None and previous != proposed and field not in confirmed:
        raise _error(
            "replacement_confirmation_required",
            f"Confirm replacement of the stored {FIELD_LABELS[field]}.",
            field=field.value,
            existing_value=previous,
            proposed_value=proposed,
        )


def complete_customer_profile(
    db: Session, command: CompleteInboxCustomerProfileCommand
) -> CompleteInboxCustomerProfileOutcome:
    """Persist Inbox edits to the linked canonical Customer and Party records."""

    def operation() -> CompleteInboxCustomerProfileOutcome:
        conversation = db.scalar(
            select(InboxConversation)
            .where(InboxConversation.id == command.conversation_id)
            .with_for_update()
        )
        if conversation is None or not conversation.is_active:
            raise _error("conversation_not_found", "Conversation was not found.")
        if conversation.subscriber_id != command.customer_id:
            raise _error(
                "customer_link_mismatch",
                "The selected Customer is not linked to this conversation.",
            )
        if classification(db, conversation) is not InboxIdentityClassification.customer:
            raise _error("customer_required", "Link an existing Customer first.")
        subscriber = db.scalar(
            select(Subscriber)
            .where(Subscriber.id == command.customer_id)
            .with_for_update()
        )
        if subscriber is None:
            raise _error("customer_not_found", "The linked Customer was not found.")
        canonical_party = (
            db.get(Party, subscriber.party_id)
            if subscriber.party_id is not None
            else None
        )
        before = _canonical_values(db, subscriber, canonical_party)
        proposed = {
            field: _clean(getattr(command.values, field.value))
            for field in command.submitted_fields
        }
        if not proposed:
            raise _error("no_fields_submitted", "Enter at least one Customer value.")
        for field, value in proposed.items():
            _assert_replacement(
                field=field,
                previous=before[field],
                proposed=value,
                confirmed=command.confirmed_replacements,
            )
        if (
            CustomerProfileField.name in proposed
            and not proposed[CustomerProfileField.name]
        ):
            raise _error("invalid_name", "Name cannot be blank.")
        if CustomerProfileField.phone in proposed:
            phone_value = proposed[CustomerProfileField.phone]
            if not phone_value:
                raise _error("invalid_phone", "Phone Number cannot be blank.")
            normalized_phone = normalize_phone_identifier(phone_value)
            if normalized_phone is None:
                raise _error("invalid_phone", "Enter a valid Phone Number.")
            proposed[CustomerProfileField.phone] = normalized_phone
            identity = customer_identity_resolution.resolve_customer_identity(
                db, normalized_phone, channel_hint="phone"
            )
            if identity.ambiguous or (
                identity.matched and identity.subscriber_id != subscriber.id
            ):
                raise _error(
                    "duplicate_identity_conflict",
                    "That Phone Number is already associated with another Customer or is ambiguous.",
                    field=CustomerProfileField.phone.value,
                )
        if CustomerProfileField.address in proposed:
            address = proposed[CustomerProfileField.address]
            if not address:
                raise _error("invalid_address", "Address cannot be blank.")
        if CustomerProfileField.email in proposed:
            email = normalize_email_identifier(proposed[CustomerProfileField.email])
            if not email:
                raise _error("invalid_email", "Enter a valid Email address.")
            identity = customer_identity_resolution.resolve_customer_identity(
                db, email, channel_hint="email"
            )
            if identity.ambiguous or (
                identity.matched and identity.subscriber_id != subscriber.id
            ):
                raise _error(
                    "duplicate_identity_conflict",
                    "That Email is already associated with another Customer or is ambiguous.",
                    field=CustomerProfileField.email.value,
                )
            proposed[CustomerProfileField.email] = email
        if CustomerProfileField.country in proposed:
            country = (proposed[CustomerProfileField.country] or "").upper()
            if len(country) != 2:
                raise _error("invalid_country", "Country must be a two-letter code.")
            proposed[CustomerProfileField.country] = country
        parsed_date_of_birth: date | None = None
        if CustomerProfileField.date_of_birth in proposed:
            try:
                parsed_date_of_birth = date.fromisoformat(
                    proposed[CustomerProfileField.date_of_birth] or ""
                )
            except ValueError:
                raise _error(
                    "invalid_date_of_birth", "Enter DOB in YYYY-MM-DD format."
                ) from None
        parsed_gender: Gender | None = None
        if CustomerProfileField.gender in proposed:
            try:
                parsed_gender = Gender(
                    (proposed[CustomerProfileField.gender] or "").lower()
                )
            except ValueError:
                raise _error("invalid_gender", "Select a supported Gender.") from None
        if CustomerProfileField.nin in proposed:
            nin = proposed[CustomerProfileField.nin] or ""
            if len(nin) != 11 or not nin.isdigit():
                raise _error("invalid_nin", "NIN must contain exactly 11 digits.")
        account_fields = frozenset(
            canonical_profile_patch.CanonicalCustomerProfileField(field.value)
            for field in proposed
            if field is not CustomerProfileField.whatsapp
        )
        if account_fields:
            try:
                subscriber = canonical_profile_patch.apply_canonical_customer_profile_patch(
                    db,
                    canonical_profile_patch.ApplyCanonicalCustomerProfilePatch(
                        subscriber_id=subscriber.id,
                        submitted_fields=account_fields,
                        source=command.decision_source,
                        actor_id=command.actor_person_id,
                        values=canonical_profile_patch.CanonicalCustomerProfileValues(
                            name=proposed.get(CustomerProfileField.name),
                            phone=proposed.get(CustomerProfileField.phone),
                            address=proposed.get(CustomerProfileField.address),
                            email=proposed.get(CustomerProfileField.email),
                            organization=proposed.get(
                                CustomerProfileField.organization
                            ),
                            city_region=proposed.get(CustomerProfileField.city_region),
                            country=proposed.get(CustomerProfileField.country),
                            date_of_birth=parsed_date_of_birth,
                            gender=parsed_gender,
                            nin=proposed.get(CustomerProfileField.nin),
                        ),
                    ),
                ).subscriber
            except canonical_profile_patch.CanonicalCustomerProfilePatchError as exc:
                raise _error(
                    "canonical_customer_update_rejected",
                    exc.message,
                    owner_error_code=exc.code,
                ) from exc
        if canonical_party is not None:
            try:
                party_fields = frozenset(
                    party.CustomerPartyProfileField(field.value)
                    for field in proposed
                    if field.value
                    in {item.value for item in party.CustomerPartyProfileField}
                )
                if party_fields:
                    party.update_customer_profile(
                        db,
                        party.CustomerPartyProfileUpdate(
                            party_id=canonical_party.id,
                            display_name=(
                                subscriber.display_name or subscriber.full_name
                            ),
                            first_name=subscriber.first_name,
                            last_name=subscriber.last_name,
                            address_line1=subscriber.address_line1,
                            address_line2=subscriber.address_line2,
                            city=subscriber.city,
                            region=subscriber.region,
                            country_code=subscriber.country_code,
                            date_of_birth=(
                                str(subscriber.date_of_birth)
                                if subscriber.date_of_birth is not None
                                else None
                            ),
                            gender=subscriber.gender.value,
                            organization=subscriber.company_name,
                            primary_email=subscriber.email,
                            primary_phone=subscriber.phone,
                            source=command.decision_source,
                            submitted_fields=party_fields,
                        ),
                    )
                if CustomerProfileField.phone in proposed and subscriber.phone:
                    party.reconcile_contact_points(
                        db,
                        party.PartyContactPointSet(
                            party_id=canonical_party.id,
                            channel_type=PartyContactPointType.phone,
                            values=(subscriber.phone,),
                            primary_index=0,
                            source=command.decision_source,
                        ),
                    )
                if CustomerProfileField.email in proposed and subscriber.email:
                    party.reconcile_contact_points(
                        db,
                        party.PartyContactPointSet(
                            party_id=canonical_party.id,
                            channel_type=PartyContactPointType.email,
                            values=(subscriber.email,),
                            primary_index=0,
                            source=command.decision_source,
                        ),
                    )
                if CustomerProfileField.whatsapp in proposed:
                    whatsapp = normalize_phone_identifier(
                        proposed[CustomerProfileField.whatsapp]
                    )
                    if not whatsapp:
                        raise _error(
                            "invalid_whatsapp", "Enter a valid WhatsApp Number."
                        )
                    proposed[CustomerProfileField.whatsapp] = whatsapp
                    party.reconcile_contact_points(
                        db,
                        party.PartyContactPointSet(
                            party_id=canonical_party.id,
                            channel_type=PartyContactPointType.whatsapp,
                            values=(whatsapp,),
                            primary_index=0,
                            source=command.decision_source,
                        ),
                    )
            except party.PartyInvariantError as exc:
                raise _error(
                    "canonical_party_update_rejected",
                    str(exc),
                ) from exc
        elif CustomerProfileField.whatsapp in proposed:
            raise _error(
                "party_required",
                "A canonical Party link is required to save WhatsApp information.",
            )
        if not account_fields:
            emit_event(
                db,
                EventType.subscriber_updated,
                {
                    "schema_version": 1,
                    "subscriber_id": str(subscriber.id),
                    "changed_fields": sorted(field.value for field in proposed),
                    "source": command.decision_source,
                },
                actor=(
                    str(command.actor_person_id)
                    if command.actor_person_id
                    else command.decision_source
                ),
                subscriber_id=subscriber.id,
            )
        customer_identity_resolution.rebuild_identity_index_for_subscriber(
            db, subscriber.id
        )
        changes = tuple(
            CustomerProfileChange(field, before[field], value)
            for field, value in proposed.items()
            if before[field] != value
        )
        audit_actor = AuditActor(
            actor_type=command.actor_type,
            actor_id=(
                str(command.actor_person_id)
                if command.actor_person_id
                else command.context.actor
            ),
        )
        stage_audit_event(
            db,
            action="inbox_customer_profile.saved",
            entity_type="inbox_conversation",
            entity_id=str(conversation.id),
            actor=audit_actor,
            metadata={
                "decision_source": command.decision_source,
                "customer_id": str(subscriber.id),
                "party_id": str(canonical_party.id) if canonical_party else None,
                "submitted_fields": sorted(field.value for field in proposed),
                "changed_fields": sorted(change.field.value for change in changes),
                "correlation_id": str(command.context.correlation_id),
            },
        )
        for change in changes:
            stage_audit_event(
                db,
                action="inbox_customer_profile.changed",
                entity_type="subscriber",
                entity_id=str(subscriber.id),
                actor=audit_actor,
                metadata={
                    "decision_source": command.decision_source,
                    "conversation_id": str(conversation.id),
                    "customer_id": str(subscriber.id),
                    "party_id": str(canonical_party.id) if canonical_party else None,
                    "field": change.field.value,
                    "previous_value": change.previous_value,
                    "new_value": change.new_value,
                    "correlation_id": str(command.context.correlation_id),
                },
            )
        db.flush()
        return CompleteInboxCustomerProfileOutcome(
            conversation_id=conversation.id,
            customer_id=subscriber.id,
            party_id=canonical_party.id if canonical_party else None,
            changes=changes,
            readiness=resolution_readiness(db, conversation),
        )

    return execute_owner_command(
        db,
        definition=_COMPLETE,
        context=command.context,
        operation=operation,
    )
