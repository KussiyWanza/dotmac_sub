"""Reviewed reactivation owner for a quarantined canonical Party identity."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.party import Party, PartyIdentityStatus, PartyType
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "party.identity_reactivation"
COMMAND_SCOPE = "party:identity_reactivate"
AUDIT_ACTION = "party.identity_reactivated"

_REACTIVATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="reviewed quarantined Party identity reactivation",
    name="reactivate_quarantined_party",
)


class PartyIdentityReactivationError(DomainError):
    """Stable refusal from the reviewed Party-reactivation boundary."""


class PartyReactivationDecisionSource(StrEnum):
    administrative_review = "administrative_review"


@dataclass(frozen=True, slots=True)
class ReactivateQuarantinedPartyCommand:
    context: CommandContext
    party_id: UUID
    expected_party_type: PartyType
    expected_updated_at: datetime
    reviewed_by_user_id: UUID
    reviewed_at: datetime
    decision_source: PartyReactivationDecisionSource
    review_reason: str


@dataclass(frozen=True, slots=True)
class PartyIdentityReactivationOutcome:
    party_id: UUID
    previous_status: PartyIdentityStatus
    current_status: PartyIdentityStatus
    updated_at: datetime
    replayed: bool
    command_id: UUID


def _error(
    suffix: str, message: str, **details: object
) -> PartyIdentityReactivationError:
    return PartyIdentityReactivationError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise _error(
            "invalid_command",
            "Party reactivation timestamps must include a timezone.",
            field=field,
        )
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validated_review(command: ReactivateQuarantinedPartyCommand) -> tuple[str, str]:
    if command.context.scope != COMMAND_SCOPE:
        raise _error("invalid_command", "Party reactivation scope is invalid.")
    actor_type, separator, actor_id = command.context.actor.partition(":")
    if actor_type != AuditActorType.user.value or not separator:
        raise _error(
            "invalid_command",
            "Party reactivation requires an attributable administrator.",
        )
    try:
        actor_user_id = UUID(actor_id)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Party reactivation requires a UUID administrator identity.",
        ) from exc
    if actor_user_id != command.reviewed_by_user_id:
        raise _error(
            "invalid_command",
            "The authenticated administrator must match the reviewed decision.",
        )
    reviewed_at = _as_utc(command.reviewed_at, field="reviewed_at")
    if reviewed_at > datetime.now(UTC):
        raise _error(
            "invalid_command",
            "Party reactivation review time cannot be in the future.",
            field="reviewed_at",
        )
    _as_utc(command.expected_updated_at, field="expected_updated_at")
    reason = command.review_reason.strip()
    if len(reason) < 20 or len(reason) > 2000:
        raise _error(
            "invalid_command",
            "Party reactivation review evidence must be between 20 and 2000 characters.",
            field="review_reason",
        )
    return str(actor_user_id), hashlib.sha256(reason.encode()).hexdigest()


def _matching_replay_audit(
    db: Session,
    *,
    command: ReactivateQuarantinedPartyCommand,
    reason_sha256: str,
) -> bool:
    events = db.scalars(
        select(AuditEvent).where(
            AuditEvent.action == AUDIT_ACTION,
            AuditEvent.entity_type == "party",
            AuditEvent.entity_id == str(command.party_id),
            AuditEvent.request_id == str(command.context.correlation_id),
        )
    ).all()
    for event in events:
        metadata = event.metadata_ if isinstance(event.metadata_, dict) else {}
        if (
            metadata.get("command_id") == str(command.context.command_id)
            and metadata.get("reason_sha256") == reason_sha256
            and metadata.get("decision_source") == command.decision_source.value
            and metadata.get("reviewed_by_user_id") == str(command.reviewed_by_user_id)
        ):
            return True
    return False


def _reactivate(
    db: Session, command: ReactivateQuarantinedPartyCommand
) -> PartyIdentityReactivationOutcome:
    actor_id, reason_sha256 = _validated_review(command)
    party = db.scalar(
        select(Party).where(Party.id == command.party_id).with_for_update()
    )
    if party is None:
        raise _error("party_not_found", "The reviewed Party was not found.")
    if party.party_type != command.expected_party_type.value:
        raise _error(
            "party_type_changed",
            "The reviewed Party type no longer matches the decision.",
            party_id=str(party.id),
        )
    if party.status == PartyIdentityStatus.active.value:
        if not _matching_replay_audit(db, command=command, reason_sha256=reason_sha256):
            raise _error(
                "party_not_quarantined",
                "The Party is active without matching reactivation evidence.",
                party_id=str(party.id),
            )
        return PartyIdentityReactivationOutcome(
            party_id=party.id,
            previous_status=PartyIdentityStatus.quarantined,
            current_status=PartyIdentityStatus.active,
            updated_at=_database_utc(party.updated_at),
            replayed=True,
            command_id=command.context.command_id,
        )
    if party.status != PartyIdentityStatus.quarantined.value:
        raise _error(
            "party_not_quarantined",
            "Only a quarantined Party can be reactivated.",
            party_id=str(party.id),
            current_status=party.status,
        )
    if _database_utc(party.updated_at) != _as_utc(
        command.expected_updated_at, field="expected_updated_at"
    ):
        raise _error(
            "stale_party",
            "The Party changed after the reactivation decision was reviewed.",
            party_id=str(party.id),
        )

    party.status = PartyIdentityStatus.active.value
    party.merge_reason = None
    db.flush()
    updated_at = _database_utc(party.updated_at)
    evidence: dict[str, object] = {
        "command_id": str(command.context.command_id),
        "previous_status": PartyIdentityStatus.quarantined.value,
        "current_status": PartyIdentityStatus.active.value,
        "decision_source": command.decision_source.value,
        "reviewed_by_user_id": str(command.reviewed_by_user_id),
        "reviewed_at": _as_utc(command.reviewed_at, field="reviewed_at").isoformat(),
        "reason_sha256": reason_sha256,
    }
    stage_audit_event(
        db,
        action=AUDIT_ACTION,
        entity_type="party",
        entity_id=str(party.id),
        actor=AuditActor(
            actor_type=AuditActorType.user,
            actor_id=actor_id,
            label=command.context.actor,
        ),
        request_id=str(command.context.correlation_id),
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.party_identity_reactivated,
        {
            "aggregate_type": "party",
            "aggregate_id": str(party.id),
            "aggregate_version": str(command.context.command_id),
            **evidence,
        },
        actor=command.context.actor,
    )
    return PartyIdentityReactivationOutcome(
        party_id=party.id,
        previous_status=PartyIdentityStatus.quarantined,
        current_status=PartyIdentityStatus.active,
        updated_at=updated_at,
        replayed=False,
        command_id=command.context.command_id,
    )


def reactivate_quarantined_party(
    db: Session, command: ReactivateQuarantinedPartyCommand
) -> PartyIdentityReactivationOutcome:
    """Reactivate one exact reviewed quarantine in a complete owner transaction."""

    return execute_owner_command(
        db,
        definition=_REACTIVATE_COMMAND,
        context=command.context,
        operation=lambda: _reactivate(db, command),
    )
