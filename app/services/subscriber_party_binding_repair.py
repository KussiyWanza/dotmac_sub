"""Reviewed, single-account Party binding repair for the admin customer UI.

This coordinator does not infer identity from customer contact data and never
repoints an account.  It only applies an administrator-reviewed choice through
the canonical Party registry and records PII-free audit evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.party import Party, PartyDataClassification, PartyType
from app.models.subscriber import Subscriber
from app.services import party as party_registry
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "party.subscriber_binding_repair"
COMMAND_SCOPE = "party:subscriber_binding_repair"
_BIND_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="reviewed single-subscriber Party binding repair",
    name="repair_subscriber_party_binding",
)
_BINDING_SOURCE = "admin_customer_party_binding_repair"


class SubscriberPartyBindingRepairError(DomainError):
    """Stable, transport-neutral refusal for an identity repair."""


@dataclass(frozen=True, slots=True)
class SubscriberPartyBindingRepairContext:
    subscriber_id: UUID
    subscriber_display_name: str
    is_active: bool
    party_id: UUID | None
    party_bound_at: datetime | None
    party_binding_source: str | None
    party_binding_reason: str | None
    can_repair: bool
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class BindSubscriberToExistingPartyCommand:
    context: CommandContext
    subscriber_id: UUID
    party_id: UUID
    binding_reason: str


@dataclass(frozen=True, slots=True)
class CreateAndBindSubscriberPartyCommand:
    context: CommandContext
    subscriber_id: UUID
    party_type: PartyType
    party_display_name: str
    binding_reason: str


@dataclass(frozen=True, slots=True)
class SubscriberPartyBindingRepairOutcome:
    subscriber_id: UUID
    party_id: UUID
    party_created: bool
    bound_at: datetime
    replayed: bool


def _error(
    code: str, message: str, **details: object
) -> SubscriberPartyBindingRepairError:
    return SubscriberPartyBindingRepairError(
        code=f"{OWNER}.{code}", message=message, details=details
    )


def _review_reason(value: str) -> str:
    reason = value.strip()
    if len(reason) < 10 or len(reason) > 2000:
        raise _error(
            "invalid_command",
            "Review evidence must be between 10 and 2000 characters.",
            field="binding_reason",
        )
    return reason


def _actor_id(context: CommandContext) -> UUID:
    if context.scope != COMMAND_SCOPE:
        raise _error("invalid_command", "Party binding repair scope is invalid.")
    actor_type, separator, actor_id = context.actor.partition(":")
    if actor_type != AuditActorType.user.value or not separator:
        raise _error(
            "invalid_command",
            "Party binding repair requires an attributable administrator.",
        )
    try:
        return UUID(actor_id)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Party binding repair requires a UUID administrator identity.",
        ) from exc


def resolve_repair_context(
    db: Session, *, subscriber_id: UUID
) -> SubscriberPartyBindingRepairContext:
    """Return authoritative repair eligibility without selecting an identity."""

    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise _error("subscriber_not_found", "Customer account was not found.")
    bound = subscriber.party_id is not None
    return SubscriberPartyBindingRepairContext(
        subscriber_id=subscriber.id,
        subscriber_display_name=(subscriber.display_name or "").strip(),
        is_active=bool(subscriber.is_active),
        party_id=subscriber.party_id,
        party_bound_at=subscriber.party_bound_at,
        party_binding_source=subscriber.party_binding_source,
        party_binding_reason=subscriber.party_binding_reason,
        can_repair=not bound,
        unavailable_reason=(
            "This customer already has a Party binding. Repointing requires the "
            "separate reviewed merge/repoint workflow."
            if bound
            else None
        ),
    )


def _locked_subscriber(db: Session, subscriber_id: UUID) -> Subscriber:
    subscriber = db.scalar(
        select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
    )
    if subscriber is None:
        raise _error("subscriber_not_found", "Customer account was not found.")
    return subscriber


def _outcome(
    *, subscriber: Subscriber, party_id: UUID, party_created: bool, replayed: bool
) -> SubscriberPartyBindingRepairOutcome:
    if subscriber.party_bound_at is None:
        raise _error("party_binding_refused", "Party binding evidence is incomplete.")
    bound_at = subscriber.party_bound_at
    if bound_at.tzinfo is None:
        bound_at = bound_at.replace(tzinfo=UTC)
    else:
        bound_at = bound_at.astimezone(UTC)
    return SubscriberPartyBindingRepairOutcome(
        subscriber_id=subscriber.id,
        party_id=party_id,
        party_created=party_created,
        bound_at=bound_at,
        replayed=replayed,
    )


def _stage_audit(
    db: Session,
    *,
    actor_id: UUID,
    context: CommandContext,
    subscriber: Subscriber,
    party_id: UUID,
    party_created: bool,
) -> None:
    stage_audit_event(
        db,
        action="party.subscriber_binding_repaired",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        actor_type=AuditActorType.user,
        actor_id=str(actor_id),
        actor_label=context.actor,
        request_id=str(context.correlation_id),
        metadata={
            "party_id": str(party_id),
            "party_created": party_created,
            "binding_source": _BINDING_SOURCE,
            "command_id": str(context.command_id),
        },
    )


def _bind_existing(
    db: Session, command: BindSubscriberToExistingPartyCommand
) -> SubscriberPartyBindingRepairOutcome:
    actor_id = _actor_id(command.context)
    reason = _review_reason(command.binding_reason)
    party = db.scalar(
        select(Party).where(Party.id == command.party_id).with_for_update()
    )
    if party is None:
        raise _error("party_binding_refused", "The reviewed Party is unavailable.")
    subscriber = _locked_subscriber(db, command.subscriber_id)
    replayed = subscriber.party_id is not None
    if replayed and (
        subscriber.party_id != party.id
        or subscriber.party_binding_source != _BINDING_SOURCE
        or subscriber.party_binding_reason != reason
        or subscriber.party_bound_at is None
    ):
        raise _error(
            "party_binding_refused",
            "This customer is already bound with different or incomplete review "
            "evidence; a Party repoint is not available here.",
        )
    try:
        bound = party_registry.bind_subscriber_account(
            db,
            subscriber_id=subscriber.id,
            party_id=party.id,
            source=_BINDING_SOURCE,
            reason=reason,
        )
    except party_registry.PartyInvariantError as exc:
        raise _error(
            "party_binding_refused",
            "The reviewed customer-to-Party binding no longer matches current state.",
        ) from exc
    if not replayed:
        _stage_audit(
            db,
            actor_id=actor_id,
            context=command.context,
            subscriber=bound,
            party_id=party.id,
            party_created=False,
        )
    return _outcome(
        subscriber=bound, party_id=party.id, party_created=False, replayed=replayed
    )


def _create_and_bind(
    db: Session, command: CreateAndBindSubscriberPartyCommand
) -> SubscriberPartyBindingRepairOutcome:
    actor_id = _actor_id(command.context)
    reason = _review_reason(command.binding_reason)
    display_name = command.party_display_name.strip()
    if not display_name or len(display_name) > 200:
        raise _error(
            "invalid_command",
            "Party display name is required and limited to 200 characters.",
        )
    subscriber = _locked_subscriber(db, command.subscriber_id)
    if subscriber.party_id is not None:
        raise _error(
            "party_binding_refused",
            "This customer already has a Party binding; a Party repoint is not available here.",
        )
    party = party_registry.create_party(
        db,
        party_type=command.party_type,
        display_name=display_name,
        data_classification=PartyDataClassification.production,
    )
    try:
        bound = party_registry.bind_subscriber_account(
            db,
            subscriber_id=subscriber.id,
            party_id=party.id,
            source=_BINDING_SOURCE,
            reason=reason,
        )
    except party_registry.PartyInvariantError as exc:
        raise _error(
            "party_binding_refused",
            "The reviewed customer-to-Party binding could not be completed.",
        ) from exc
    _stage_audit(
        db,
        actor_id=actor_id,
        context=command.context,
        subscriber=bound,
        party_id=party.id,
        party_created=True,
    )
    return _outcome(
        subscriber=bound, party_id=party.id, party_created=True, replayed=False
    )


def bind_subscriber_to_existing_party(
    db: Session, command: BindSubscriberToExistingPartyCommand
) -> SubscriberPartyBindingRepairOutcome:
    return execute_owner_command(
        db,
        definition=_BIND_COMMAND,
        context=command.context,
        operation=lambda: _bind_existing(db, command),
    )


def create_and_bind_subscriber_party(
    db: Session, command: CreateAndBindSubscriberPartyCommand
) -> SubscriberPartyBindingRepairOutcome:
    return execute_owner_command(
        db,
        definition=_BIND_COMMAND,
        context=command.context,
        operation=lambda: _create_and_bind(db, command),
    )
