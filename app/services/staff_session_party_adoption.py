"""Canonical writer for approved staff-session Party projections.

The operator adapter selects no identity. It supplies an exact session,
SystemUser and Person Party tuple backed by digest-bound evidence. This owner
re-validates that tuple under locks and writes only ``sessions.party_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.party import Party, PartyType
from app.models.system_user import SystemUser
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "party.staff_session_projection"
COMMAND_SCOPE = "party:staff_session_projection"
_PROJECT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="approved staff session Party projection",
    name="project_staff_session_party",
)


class StaffSessionPartyAdoptionError(DomainError):
    """Stable, transport-neutral session-projection refusal."""


@dataclass(frozen=True, slots=True)
class ProjectStaffSessionPartyCommand:
    """One exact, independently approved session identity projection."""

    context: CommandContext
    session_id: UUID
    expected_system_user_id: UUID
    person_party_id: UUID
    decision_id: UUID
    plan_digest: str
    evidence_sha256: str
    approval_id: UUID
    approval_sha256: str


@dataclass(frozen=True, slots=True)
class StaffSessionPartyProjectionOutcome:
    """Committed projection result without identity display fields."""

    session_id: UUID
    system_user_id: UUID
    party_id: UUID
    replayed: bool


def _error(
    code: str,
    message: str,
    **details: object,
) -> StaffSessionPartyAdoptionError:
    return StaffSessionPartyAdoptionError(
        code=f"{OWNER}.{code}",
        message=message,
        details=details,
    )


def _approver_id(context: CommandContext) -> UUID:
    if context.scope != COMMAND_SCOPE:
        raise _error(
            "invalid_command",
            "Staff session Party projection scope is invalid.",
            field="scope",
        )
    actor_type, separator, actor_id = context.actor.partition(":")
    if actor_type != AuditActorType.user.value or not separator:
        raise _error(
            "invalid_command",
            "Staff session Party projection requires an attributable user approver.",
            field="actor",
        )
    try:
        return UUID(actor_id)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Staff session Party projection requires a UUID user approver.",
            field="actor",
        ) from exc


def _approver_actor(db: Session, context: CommandContext) -> AuditActor:
    """Resolve the approval principal's canonical Party for audit provenance."""

    approver_id = _approver_id(context)
    row = db.execute(
        select(SystemUser.is_active, SystemUser.person_party_id).where(
            SystemUser.id == approver_id
        )
    ).one_or_none()
    if row is None or not row.is_active or row.person_party_id is None:
        raise _error(
            "invalid_command",
            "Staff session Party projection requires an active Party-bound approver.",
            field="actor",
        )
    return AuditActor.user(
        str(approver_id),
        label=context.actor,
        party_id=row.person_party_id,
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _project_staff_session_party(
    db: Session,
    command: ProjectStaffSessionPartyCommand,
) -> StaffSessionPartyProjectionOutcome:
    approver = _approver_actor(db, command.context)
    if not all(
        _is_sha256(value)
        for value in (
            command.plan_digest,
            command.evidence_sha256,
            command.approval_sha256,
        )
    ):
        raise _error(
            "invalid_command",
            "Staff session projection evidence must use lowercase SHA-256 digests.",
            field="projection_evidence",
        )

    # One lock order across every execution prevents two reviewed plans from
    # projecting the same identity/session tuple in opposite order.
    party = db.scalar(
        select(Party).where(Party.id == command.person_party_id).with_for_update()
    )
    if party is None or party.party_type != PartyType.person.value:
        raise _error(
            "party_binding_refused",
            "The approved Person Party is unavailable for session projection.",
        )
    principal = db.scalar(
        select(SystemUser)
        .where(SystemUser.id == command.expected_system_user_id)
        .with_for_update()
    )
    if principal is None:
        raise _error(
            "staff_account_not_found",
            "The approved staff principal was not found.",
        )
    if not principal.is_active or principal.person_party_id != party.id:
        raise _error(
            "party_binding_refused",
            "The approved staff principal no longer has the exact active Party binding.",
        )
    auth_session = db.scalar(
        select(AuthSession)
        .where(AuthSession.id == command.session_id)
        .with_for_update()
    )
    if auth_session is None:
        raise _error(
            "session_not_found",
            "The approved staff session was not found.",
        )
    if auth_session.system_user_id != principal.id:
        raise _error(
            "session_principal_conflict",
            "The approved session no longer belongs to the exact staff principal.",
        )

    if auth_session.party_id is not None:
        if auth_session.party_id != party.id:
            raise _error(
                "session_party_conflict",
                "The staff session already carries a different Party projection.",
            )
        return StaffSessionPartyProjectionOutcome(
            session_id=auth_session.id,
            system_user_id=principal.id,
            party_id=party.id,
            replayed=True,
        )

    # The campaign projects only usable legacy sessions. Historical revoked or
    # non-active rows remain preserved and nullable; they cannot authenticate
    # and are not identity evidence worth guessing about.
    if (
        auth_session.status is not SessionStatus.active
        or auth_session.revoked_at is not None
    ):
        raise _error(
            "session_ineligible",
            "The approved session is no longer active and unrevoked.",
        )

    auth_session.party_id = party.id
    db.add(auth_session)
    stage_audit_event(
        db,
        action="party.staff_session_projected",
        entity_type="session",
        entity_id=str(auth_session.id),
        actor=approver,
        request_id=str(command.context.correlation_id),
        metadata={
            "person_party_id": str(party.id),
            "system_user_id": str(principal.id),
            "decision_id": str(command.decision_id),
            "plan_digest": command.plan_digest,
            "evidence_sha256": command.evidence_sha256,
            "approval_id": str(command.approval_id),
            "approval_sha256": command.approval_sha256,
            "command_id": str(command.context.command_id),
        },
    )
    db.flush()
    return StaffSessionPartyProjectionOutcome(
        session_id=auth_session.id,
        system_user_id=principal.id,
        party_id=party.id,
        replayed=False,
    )


def project_staff_session_party(
    db: Session,
    command: ProjectStaffSessionPartyCommand,
) -> StaffSessionPartyProjectionOutcome:
    """Project one approved legacy session in a complete transaction."""

    return execute_owner_command(
        db,
        definition=_PROJECT_COMMAND,
        context=command.context,
        operation=lambda: _project_staff_session_party(db, command),
    )


__all__ = [
    "COMMAND_SCOPE",
    "ProjectStaffSessionPartyCommand",
    "StaffSessionPartyAdoptionError",
    "StaffSessionPartyProjectionOutcome",
    "project_staff_session_party",
]
