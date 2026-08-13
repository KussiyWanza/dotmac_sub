"""Canonical coordinator for approved existing-staff Party adoption.

This owner binds one existing SystemUser to one reviewed Person Party. It does
not select identity, create a Party, write credential projection fields, or cut
authentication readers over. The operator adapter supplies exact UUID and
digest evidence and invokes the credential projection owner separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.party import Party
from app.models.system_user import SystemUser
from app.services import party as party_registry
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "party.staff_principal_adoption"
COMMAND_SCOPE = "party:staff_principal_adoption"
_BIND_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="existing staff Party principal adoption",
    name="bind_existing_staff_party",
)


class StaffPartyAdoptionError(DomainError):
    """Stable, transport-neutral existing-staff adoption refusal."""


@dataclass(frozen=True, slots=True)
class BindExistingStaffPartyCommand:
    """Approved exact Party binding for one existing staff principal."""

    context: CommandContext
    system_user_id: UUID
    person_party_id: UUID
    binding_source: str
    binding_reason: str


@dataclass(frozen=True, slots=True)
class ExistingStaffPartyBindingOutcome:
    """Committed staff Party binding without identity display fields."""

    system_user_id: UUID
    person_party_id: UUID
    bound_at: datetime
    replayed: bool


def _error(code: str, message: str, **details: object) -> StaffPartyAdoptionError:
    return StaffPartyAdoptionError(
        code=f"{OWNER}.{code}",
        message=message,
        details=details,
    )


def _validate_context(context: CommandContext) -> UUID:
    if context.scope != COMMAND_SCOPE:
        raise _error(
            "invalid_command",
            "Existing staff Party adoption scope is invalid.",
            field="scope",
        )
    actor_type, separator, actor_id = context.actor.partition(":")
    if actor_type != AuditActorType.user.value or not separator:
        raise _error(
            "invalid_command",
            "Existing staff Party adoption requires an attributable user approver.",
            field="actor",
        )
    try:
        return UUID(actor_id)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Existing staff Party adoption requires a UUID user approver.",
            field="actor",
        ) from exc


def _bind_existing_staff_party(
    db: Session,
    command: BindExistingStaffPartyCommand,
) -> ExistingStaffPartyBindingOutcome:
    actor_id = _validate_context(command.context)
    source = command.binding_source.strip()
    reason = command.binding_reason.strip()
    if not source or len(source) > 80 or not reason:
        raise _error(
            "invalid_command",
            "Staff Party binding evidence is incomplete or exceeds its contract.",
            field="binding_evidence",
        )

    # Match the credential projection owner's Party-before-principal lock
    # suffix. Party.registry remains the only native field writer and repeats
    # its own Person/status validation under these locks.
    party = db.scalar(
        select(Party).where(Party.id == command.person_party_id).with_for_update()
    )
    if party is None:
        raise _error(
            "party_binding_refused",
            "The reviewed Person Party is unavailable for staff adoption.",
        )
    user = db.scalar(
        select(SystemUser)
        .where(SystemUser.id == command.system_user_id)
        .with_for_update()
    )
    if user is None:
        raise _error(
            "staff_account_not_found",
            "The reviewed staff principal was not found.",
        )

    replayed = user.person_party_id is not None
    if replayed and (
        user.person_party_id != party.id
        or user.party_binding_source != source
        or user.party_binding_reason != reason
        or user.party_bound_at is None
    ):
        raise _error(
            "party_binding_refused",
            "The staff principal is already bound with different or incomplete "
            "review evidence; an exact retry is required.",
        )
    try:
        bound = party_registry.bind_system_user_principal(
            db,
            system_user_id=user.id,
            person_party_id=party.id,
            source=source,
            reason=reason,
        )
    except party_registry.PartyInvariantError as exc:
        raise _error(
            "party_binding_refused",
            "The reviewed staff-to-Party binding no longer matches current state.",
        ) from exc
    if bound.party_bound_at is None or bound.person_party_id is None:
        raise _error(
            "party_binding_refused",
            "The staff Party owner returned incomplete binding evidence.",
        )

    if not replayed:
        stage_audit_event(
            db,
            action="party.staff_principal_adopted",
            entity_type="system_user",
            entity_id=str(bound.id),
            actor_type=AuditActorType.user,
            actor_id=str(actor_id),
            actor_label=command.context.actor,
            request_id=str(command.context.correlation_id),
            metadata={
                "person_party_id": str(bound.person_party_id),
                "binding_source": source,
                "command_id": str(command.context.command_id),
            },
        )
    bound_at = bound.party_bound_at
    if bound_at.tzinfo is None:
        bound_at = bound_at.replace(tzinfo=UTC)
    else:
        bound_at = bound_at.astimezone(UTC)
    return ExistingStaffPartyBindingOutcome(
        system_user_id=bound.id,
        person_party_id=bound.person_party_id,
        bound_at=bound_at,
        replayed=replayed,
    )


def bind_existing_staff_party(
    db: Session,
    command: BindExistingStaffPartyCommand,
) -> ExistingStaffPartyBindingOutcome:
    """Bind one reviewed existing staff principal in a complete transaction."""

    return execute_owner_command(
        db,
        definition=_BIND_COMMAND,
        context=command.context,
        operation=lambda: _bind_existing_staff_party(db, command),
    )


__all__ = [
    "COMMAND_SCOPE",
    "BindExistingStaffPartyCommand",
    "ExistingStaffPartyBindingOutcome",
    "StaffPartyAdoptionError",
    "bind_existing_staff_party",
]
