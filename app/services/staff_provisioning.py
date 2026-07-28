"""Canonical staff-account provisioning owner for ERP HR commands.

Public writes enter one manifest-verified transaction. Staff identity and local
credential bootstrap are committed atomically with assignment-owner managed
grants, audit evidence, and a versioned event. Invitation delivery is an event
consequence; this owner never calls an email transport or persists a token.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.party import PartyDataClassification, PartyType
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import auth_cache, credential_recovery
from app.services import auth_flow as auth_flow_service
from app.services import party as party_registry
from app.services import system_user_assignments as assignment_service
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.session_hooks import run_after_commit

if TYPE_CHECKING:
    from app.models.notification import Notification
    from app.services.ephemeral_communication_actions import EphemeralEmailContent

ERP_HR_ROLE_SOURCE = "erp_hr"
STAFF_ASSIGN_SCOPE = "rbac:assign"

_PROVISION_COMMAND = OwnerCommandDefinition(
    owner="auth.staff_provisioning",
    concern="staff account provisioning",
    name="provision_staff_account",
)
_CREATE_LOCAL_COMMAND = OwnerCommandDefinition(
    owner="auth.staff_provisioning",
    concern="staff account provisioning",
    name="create_local_staff_account",
)
_SYNC_ROLES_COMMAND = OwnerCommandDefinition(
    owner="auth.staff_provisioning",
    concern="staff account provisioning",
    name="sync_staff_account_roles",
)
_SET_ACTIVE_COMMAND = OwnerCommandDefinition(
    owner="auth.staff_provisioning",
    concern="staff account provisioning",
    name="set_staff_account_active",
)


class StaffProvisioningError(DomainError):
    """Stable, transport-neutral staff command failure."""


class UnknownRoleError(StaffProvisioningError):
    """Requested role name does not identify an active role."""

    def __init__(self, role_names: tuple[str, ...] | list[str]) -> None:
        normalized = tuple(role_names)
        super().__init__(
            code="auth.staff_provisioning.unknown_roles",
            message="One or more requested roles are not active.",
            details={"role_names": list(normalized)},
        )
        self.role_names = normalized


@dataclass(frozen=True)
class ProvisionStaffAccountCommand:
    """ERP HR request to create or reconcile one staff principal."""

    context: CommandContext
    email: str
    first_name: str
    last_name: str
    role_names: tuple[str, ...]
    send_invite: bool = True


@dataclass(frozen=True)
class CreateLocalStaffAccountCommand:
    """Administrative request to create one locally managed staff principal."""

    context: CommandContext
    email: str
    first_name: str
    last_name: str
    role_id: UUID
    send_invite: bool = True


@dataclass(frozen=True)
class SyncStaffRolesCommand:
    """ERP HR request to converge its managed grants for one principal."""

    context: CommandContext
    user_id: UUID
    role_names: tuple[str, ...]


@dataclass(frozen=True)
class SetStaffAccountActiveCommand:
    """ERP HR request to change staff access state."""

    context: CommandContext
    user_id: UUID
    is_active: bool


@dataclass(frozen=True)
class StaffAccountOutcome:
    """Committed staff-account state returned without leaking an ORM entity."""

    user_id: UUID
    person_party_id: UUID | None
    email: str
    display_name: str | None
    is_active: bool
    role_names: tuple[str, ...]
    created: bool
    changed: bool
    invite_requested: bool
    command_id: UUID
    correlation_id: UUID


def _error(
    code: str,
    message: str,
    **details: object,
) -> StaffProvisioningError:
    return StaffProvisioningError(
        code=f"auth.staff_provisioning.{code}",
        message=message,
        details=details,
    )


def _normalize_role_names(role_names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name.strip() for name in role_names if name.strip()))


def _validate_context(context: CommandContext) -> tuple[AuditActorType, str]:
    if context.scope != STAFF_ASSIGN_SCOPE:
        raise _error(
            "invalid_command",
            "Staff provisioning requires authorized RBAC assignment evidence.",
            field="scope",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Staff provisioning actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "Staff provisioning actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _validate_identity(
    command: ProvisionStaffAccountCommand | CreateLocalStaffAccountCommand,
) -> tuple[str, str, str]:
    email = command.email.strip().lower()
    first_name = command.first_name.strip()
    last_name = command.last_name.strip()
    invalid_fields = [
        field
        for field, value in (
            ("email", email),
            ("first_name", first_name),
            ("last_name", last_name),
        )
        if not value
    ]
    if invalid_fields:
        raise _error(
            "invalid_command",
            "Staff identity fields cannot be empty.",
            fields=invalid_fields,
        )
    if len(email) > 255 or len(first_name) > 80 or len(last_name) > 80:
        raise _error(
            "invalid_command",
            "Staff identity field length exceeds the canonical record limit.",
        )
    return email, first_name, last_name


def _role_names(role_names: tuple[str, ...]) -> tuple[str, ...]:
    normalized = _normalize_role_names(role_names)
    if not normalized:
        raise UnknownRoleError(("At least one active role is required",))
    if len(normalized) > 20 or any(len(name) > 80 for name in normalized):
        raise _error(
            "invalid_command",
            "Requested staff roles exceed the command limits.",
            field="role_names",
        )
    return normalized


def _acquire_identity_lock(db: Session, email: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    lock_key = int.from_bytes(
        hashlib.sha256(f"staff:{email}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def _locked_user(db: Session, user_id: UUID) -> SystemUser:
    user = db.execute(
        select(SystemUser).where(SystemUser.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        raise _error(
            "staff_account_not_found",
            "Staff account was not found.",
            user_id=str(user_id),
        )
    return user


def _actor_metadata(context: CommandContext) -> dict[str, object]:
    return {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "causation_id": str(context.causation_id) if context.causation_id else None,
        "idempotency_key_sha256": (
            hashlib.sha256(context.idempotency_key.encode()).hexdigest()
            if context.idempotency_key
            else None
        ),
        "scope": context.scope,
        "reason": context.reason,
    }


def _stage_audit(
    db: Session,
    *,
    action: str,
    user_id: UUID,
    context: CommandContext,
    actor_type: AuditActorType,
    actor_id: str,
    metadata: dict[str, object],
    status_code: int = 200,
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type="system_user",
        entity_id=str(user_id),
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=str(context.correlation_id),
        status_code=status_code,
        metadata={**_actor_metadata(context), **metadata},
    )


def _emit_staff_event(
    db: Session,
    *,
    event_type: EventType,
    user: SystemUser,
    context: CommandContext,
    payload: dict[str, object],
) -> None:
    emit_event(
        db,
        event_type,
        {
            **_actor_metadata(context),
            "aggregate_type": "system_user",
            "aggregate_id": str(user.id),
            "aggregate_version": str(context.command_id),
            **payload,
        },
        actor=context.actor,
    )


def _invalidate_auth_after_commit(db: Session, user_id: UUID) -> None:
    def invalidate(_callback_db: Session) -> None:
        auth_cache.invalidate_principal("system_user", str(user_id))

    run_after_commit(db, invalidate)


def _outcome(
    user: SystemUser,
    *,
    role_names: tuple[str, ...],
    created: bool,
    changed: bool,
    invite_requested: bool,
    context: CommandContext,
) -> StaffAccountOutcome:
    return StaffAccountOutcome(
        user_id=user.id,
        person_party_id=user.person_party_id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        role_names=role_names,
        created=created,
        changed=changed,
        invite_requested=invite_requested,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _sync_roles(
    db: Session,
    *,
    user: SystemUser,
    role_names: tuple[str, ...],
) -> assignment_service.SourceRoleSyncResult:
    try:
        return assignment_service.sync_source_roles_by_names(
            db,
            user_id=user.id,
            role_names=role_names,
            source=ERP_HR_ROLE_SOURCE,
        )
    except assignment_service.RoleResolutionError as exc:
        raise UnknownRoleError(exc.role_names) from exc


def _create_principal(
    db: Session,
    *,
    email: str,
    first_name: str,
    last_name: str,
    context: CommandContext,
    binding_source: str,
) -> SystemUser:
    display_name = f"{first_name} {last_name}".strip()
    user = SystemUser(
        first_name=first_name,
        last_name=last_name,
        display_name=display_name,
        email=email,
        user_type=UserType.system_user,
        is_active=True,
    )
    db.add(user)
    db.flush()
    person = party_registry.create_party(
        db,
        party_type=PartyType.person,
        display_name=display_name,
        data_classification=PartyDataClassification.production,
        metadata={
            "staff_identity_bootstrap": {
                "schema_version": 1,
                "owner": "auth.staff_provisioning",
                "command_id": str(context.command_id),
            }
        },
    )
    party_registry.bind_system_user_principal(
        db,
        system_user_id=user.id,
        person_party_id=person.id,
        source=binding_source,
        reason=context.reason,
    )
    placeholder = secrets.token_urlsafe(32)
    db.add(
        UserCredential(
            system_user_id=user.id,
            provider=AuthProvider.local,
            username=email,
            password_hash=auth_flow_service.hash_password(placeholder),
            must_change_password=True,
            is_active=True,
        )
    )
    return user


def _provision(
    db: Session,
    command: ProvisionStaffAccountCommand,
) -> StaffAccountOutcome:
    actor_type, actor_id = _validate_context(command.context)
    email, first_name, last_name = _validate_identity(command)
    desired_roles = _role_names(command.role_names)
    _acquire_identity_lock(db, email)

    user = db.execute(
        select(SystemUser).where(SystemUser.email == email).with_for_update()
    ).scalar_one_or_none()
    created = user is None
    if user is None:
        user = _create_principal(
            db,
            email=email,
            first_name=first_name,
            last_name=last_name,
            context=command.context,
            binding_source="auth.staff_provisioning:erp_hr",
        )

    role_result = _sync_roles(db, user=user, role_names=desired_roles)
    invite_requested = bool(created and command.send_invite)
    changed = bool(created or role_result.changed)
    _invalidate_auth_after_commit(db, user.id)

    if created:
        email_digest = hashlib.sha256(email.encode()).hexdigest()
        _stage_audit(
            db,
            action="auth.staff_account_provisioned",
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            status_code=201,
            metadata={
                "role_names": list(role_result.role_names),
                "invite_requested": invite_requested,
            },
        )
        _emit_staff_event(
            db,
            event_type=EventType.staff_account_provisioned,
            user=user,
            context=command.context,
            payload={
                "user_id": str(user.id),
                "role_names": list(role_result.role_names),
                "invite_requested": invite_requested,
                "email_sha256": email_digest,
            },
        )
    else:
        _stage_audit(
            db,
            action="auth.staff_account_reconciled",
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={
                "role_names": list(role_result.role_names),
                "roles_changed": role_result.changed,
            },
        )
        if role_result.changed:
            _emit_staff_event(
                db,
                event_type=EventType.staff_account_roles_changed,
                user=user,
                context=command.context,
                payload={
                    "user_id": str(user.id),
                    "role_names": list(role_result.role_names),
                },
            )

    return _outcome(
        user,
        role_names=role_result.role_names,
        created=created,
        changed=changed,
        invite_requested=invite_requested,
        context=command.context,
    )


def provision_staff_account(
    db: Session,
    command: ProvisionStaffAccountCommand,
) -> StaffAccountOutcome:
    """Create or reconcile one account in a complete owner transaction."""

    try:
        return execute_owner_command(
            db,
            definition=_PROVISION_COMMAND,
            context=command.context,
            operation=lambda: _provision(db, command),
        )
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "Staff identity conflicts with an existing canonical record.",
        ) from exc


def create_local_staff_account(
    db: Session,
    command: CreateLocalStaffAccountCommand,
) -> StaffAccountOutcome:
    """Create one local staff principal, credential, grant, audit, and event."""

    def operation() -> StaffAccountOutcome:
        actor_type, actor_id = _validate_context(command.context)
        email, first_name, last_name = _validate_identity(command)
        _acquire_identity_lock(db, email)
        existing = db.execute(
            select(SystemUser.id).where(SystemUser.email == email).with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            raise _error(
                "identity_conflict",
                "Staff identity conflicts with an existing canonical record.",
            )
        user = _create_principal(
            db,
            email=email,
            first_name=first_name,
            last_name=last_name,
            context=command.context,
            binding_source="auth.staff_provisioning:local",
        )
        try:
            role_result = assignment_service.sync_source_roles_by_ids(
                db,
                user_id=user.id,
                role_ids=(command.role_id,),
                source=assignment_service.LOCAL_ROLE_SOURCE,
            )
        except assignment_service.RoleResolutionError as exc:
            raise UnknownRoleError(tuple(str(item) for item in exc.role_ids)) from exc
        invite_requested = bool(command.send_invite)
        _invalidate_auth_after_commit(db, user.id)
        _stage_audit(
            db,
            action="auth.staff_account_provisioned",
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            status_code=201,
            metadata={
                "role_names": list(role_result.role_names),
                "invite_requested": invite_requested,
                "grant_source": assignment_service.LOCAL_ROLE_SOURCE,
            },
        )
        _emit_staff_event(
            db,
            event_type=EventType.staff_account_provisioned,
            user=user,
            context=command.context,
            payload={
                "user_id": str(user.id),
                "role_names": list(role_result.role_names),
                "invite_requested": invite_requested,
                "email_sha256": hashlib.sha256(email.encode()).hexdigest(),
            },
        )
        return _outcome(
            user,
            role_names=role_result.role_names,
            created=True,
            changed=True,
            invite_requested=invite_requested,
            context=command.context,
        )

    try:
        return execute_owner_command(
            db,
            definition=_CREATE_LOCAL_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "Staff identity conflicts with an existing canonical record.",
        ) from exc


def sync_staff_account_roles(
    db: Session,
    command: SyncStaffRolesCommand,
) -> StaffAccountOutcome:
    """Converge ERP-managed roles without touching local or scoped grants."""

    def operation() -> StaffAccountOutcome:
        actor_type, actor_id = _validate_context(command.context)
        user = _locked_user(db, command.user_id)
        role_result = _sync_roles(
            db,
            user=user,
            role_names=_role_names(command.role_names),
        )
        _invalidate_auth_after_commit(db, user.id)
        _stage_audit(
            db,
            action="auth.staff_roles_reconciled",
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={
                "role_names": list(role_result.role_names),
                "changed": role_result.changed,
            },
        )
        if role_result.changed:
            _emit_staff_event(
                db,
                event_type=EventType.staff_account_roles_changed,
                user=user,
                context=command.context,
                payload={
                    "user_id": str(user.id),
                    "role_names": list(role_result.role_names),
                },
            )
        return _outcome(
            user,
            role_names=role_result.role_names,
            created=False,
            changed=role_result.changed,
            invite_requested=False,
            context=command.context,
        )

    return execute_owner_command(
        db,
        definition=_SYNC_ROLES_COMMAND,
        context=command.context,
        operation=operation,
    )


def set_staff_account_active(
    db: Session,
    command: SetStaffAccountActiveCommand,
) -> StaffAccountOutcome:
    """Converge principal, credential, and session access state atomically."""

    def operation() -> StaffAccountOutcome:
        actor_type, actor_id = _validate_context(command.context)
        user = _locked_user(db, command.user_id)
        if not command.is_active:
            assignment_service.ensure_can_deactivate_system_user(db, user.id)
        state_changed = user.is_active != command.is_active
        user.is_active = command.is_active
        credential_changes = (
            db.query(UserCredential)
            .filter(
                UserCredential.system_user_id == user.id,
                UserCredential.is_active.is_not(command.is_active),
            )
            .update(
                {"is_active": command.is_active},
                synchronize_session=False,
            )
        )
        revoked_sessions = 0
        if not command.is_active:
            revoked_sessions = (
                db.query(AuthSession)
                .filter(
                    AuthSession.system_user_id == user.id,
                    AuthSession.status == SessionStatus.active,
                    AuthSession.revoked_at.is_(None),
                )
                .update(
                    {
                        "status": SessionStatus.revoked,
                        "revoked_at": datetime.now(UTC),
                    },
                    synchronize_session=False,
                )
            )
        changed = bool(state_changed or credential_changes or revoked_sessions)
        role_names = assignment_service.system_user_role_names(db, user.id)
        _invalidate_auth_after_commit(db, user.id)
        _stage_audit(
            db,
            action=(
                "auth.staff_account_activated"
                if command.is_active
                else "auth.staff_account_deactivated"
            ),
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={
                "changed": changed,
                "credential_changes": int(credential_changes or 0),
                "revoked_sessions": int(revoked_sessions or 0),
            },
        )
        if changed:
            _emit_staff_event(
                db,
                event_type=(
                    EventType.staff_account_activated
                    if command.is_active
                    else EventType.staff_account_deactivated
                ),
                user=user,
                context=command.context,
                payload={
                    "user_id": str(user.id),
                    "is_active": command.is_active,
                    "revoked_sessions": int(revoked_sessions or 0),
                },
            )
        return _outcome(
            user,
            role_names=role_names,
            created=False,
            changed=changed,
            invite_requested=False,
            context=command.context,
        )

    return execute_owner_command(
        db,
        definition=_SET_ACTIVE_COMMAND,
        context=command.context,
        operation=operation,
    )


def find_by_email(db: Session, email: str) -> SystemUser | None:
    """Read-only ERP reconcile lookup by normalized canonical email."""

    normalized = email.strip().lower()
    return db.query(SystemUser).filter(SystemUser.email == normalized).first()


def get_role_names(db: Session, user: SystemUser) -> list[str]:
    """Compatibility query for read-only adapters."""

    return list(assignment_service.system_user_role_names(db, user.id))


def materialize_staff_invite_email(
    db: Session,
    *,
    notification: Notification,
    context: dict[str, object],
) -> EphemeralEmailContent:
    """Mint and render an exact staff reset capability at delivery time."""

    from app.models.notification import Notification
    from app.services import email as email_service
    from app.services.ephemeral_communication_actions import (
        EphemeralActionRejected,
        EphemeralEmailContent,
    )

    if not isinstance(notification, Notification):
        raise EphemeralActionRejected("invalid_notification")
    if set(context) != {"user_id", "email_sha256"}:
        raise EphemeralActionRejected("invalid_context")
    try:
        user_id = UUID(str(context["user_id"]))
        email_digest = str(context["email_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EphemeralActionRejected("invalid_context") from exc
    if len(email_digest) != 64 or any(
        char not in "0123456789abcdef" for char in email_digest
    ):
        raise EphemeralActionRejected("invalid_context")
    recipient_digest = hashlib.sha256(
        notification.recipient.strip().lower().encode()
    ).hexdigest()
    if (
        notification.audience_type != "system_user"
        or notification.audience_id != user_id
        or recipient_digest != email_digest
    ):
        raise EphemeralActionRejected("recipient_context_mismatch")

    user = db.get(SystemUser, user_id)
    if (
        user is None
        or not user.is_active
        or hashlib.sha256(user.email.strip().lower().encode()).hexdigest()
        != email_digest
    ):
        raise EphemeralActionRejected("stale_account_context")
    reset = credential_recovery.issue_exact_reset_capability(
        db,
        principal_type="system_user",
        principal_id=user.id,
    )
    if reset is None or reset.principal_id != user.id or not reset.token:
        raise EphemeralActionRejected("stale_account_context")
    from app.services import access_invitations

    access_invitations.record_issued(
        db,
        principal_type="system_user",
        principal_id=reset.principal_id,
        purpose="staff_invite",
        email=reset.email,
        ttl_minutes=reset.ttl_minutes,
        source="staff_provisioning",
    )

    rendered = email_service.render_user_invite_email(
        db,
        to_email=user.email,
        reset_token=reset.token,
        person_name=user.display_name or user.first_name,
        next_login_path="/auth/login?next=/admin/dashboard",
        expires_minutes=reset.ttl_minutes,
        token_in_fragment=True,
    )
    return EphemeralEmailContent(
        subject=rendered.subject,
        body_html=rendered.body_html,
        body_text=rendered.body_text,
        activity="auth_user_invite",
    )
