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
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, or_, select, text
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
STAFF_PROFILE_SCOPE = "profile:self"
STAFF_LOGIN_IDENTITY_MAX_LENGTH = 150

_EMAIL_ADAPTER = TypeAdapter(EmailStr)

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
_UPDATE_IDENTITY_COMMAND = OwnerCommandDefinition(
    owner="auth.staff_provisioning",
    concern="staff identity maintenance",
    name="update_staff_identity",
)
_PREPARE_RECOVERY_COMMAND = OwnerCommandDefinition(
    owner="auth.staff_provisioning",
    concern="staff identity maintenance",
    name="prepare_staff_credential_recovery",
)
_RECONCILE_LOGIN_COMMAND = OwnerCommandDefinition(
    owner="auth.staff_provisioning",
    concern="staff identity maintenance",
    name="reconcile_staff_login_identity",
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


class StaffIdentityField(str, Enum):
    """Closed set of mutable staff profile fields."""

    first_name = "first_name"
    last_name = "last_name"
    display_name = "display_name"
    email = "email"
    phone = "phone"


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
class UpdateStaffIdentityCommand:
    """Administrative or self-service staff identity change."""

    context: CommandContext
    user_id: UUID
    fields: frozenset[StaffIdentityField]
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    new_password: str | None = None
    require_password_change: bool = True


@dataclass(frozen=True)
class PrepareStaffCredentialRecoveryCommand:
    """Ensure one active canonical local credential before recovery delivery."""

    context: CommandContext
    user_id: UUID


@dataclass(frozen=True)
class ReconcileStaffLoginIdentityCommand:
    """Repair one reviewed staff email/credential-username mismatch."""

    context: CommandContext
    user_id: UUID
    expected_email_sha256: str


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


@dataclass(frozen=True)
class StaffIdentityOutcome:
    """Committed identity state without credential secrets."""

    user_id: UUID
    email: str
    display_name: str | None
    credential_username: str
    credential_active: bool
    changed_fields: tuple[StaffIdentityField, ...]
    credential_reconciled: bool
    password_changed: bool
    revoked_sessions: int
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class StaffCredentialPreparationOutcome:
    """Recovery preparation state without returning a password or token."""

    user_id: UUID
    email: str
    credential_id: UUID
    created: bool
    changed: bool
    revoked_sessions: int
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class StaffLoginIdentityReconciliationOutcome:
    """Reconciliation result without exposing credential secrets."""

    user_id: UUID
    email: str
    credential_id: UUID
    credential_created: bool
    username_reconciled: bool
    activation_reconciled: bool
    revoked_sessions: int
    changed: bool
    command_id: UUID
    correlation_id: UUID


class StaffLoginIdentityIssue(str, Enum):
    missing_credential = "missing_credential"
    multiple_credentials = "multiple_credentials"
    username_mismatch = "username_mismatch"
    username_conflict = "username_conflict"
    activation_mismatch = "activation_mismatch"


class StaffCredentialDisplayStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    needs_reconciliation = "needs_reconciliation"


class StaffCredentialRecoveryBlock(str, Enum):
    inactive_account = "inactive_account"
    multiple_credentials = "multiple_credentials"
    identity_conflict = "identity_conflict"


@dataclass(frozen=True)
class StaffLoginCredentialView:
    username: str | None
    is_active: bool
    must_change_password: bool
    password_updated_at: datetime | None
    status: StaffCredentialDisplayStatus


@dataclass(frozen=True)
class StaffCredentialRecoveryEligibility:
    allowed: bool
    blocked_by: StaffCredentialRecoveryBlock | None = None


@dataclass(frozen=True)
class StaffLoginIdentityView:
    credential: StaffLoginCredentialView | None
    issue: StaffLoginIdentityIssue | None
    recovery: StaffCredentialRecoveryEligibility


@dataclass(frozen=True)
class StaffLoginIdentityDrift:
    user_id: UUID
    issue: StaffLoginIdentityIssue
    email_sha256: str


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


def _validate_context(
    context: CommandContext,
    *,
    allowed_scopes: tuple[str, ...] = (STAFF_ASSIGN_SCOPE,),
) -> tuple[AuditActorType, str]:
    if context.scope not in allowed_scopes:
        raise _error(
            "invalid_command",
            "Staff provisioning authorization evidence is not valid for this command.",
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
    email = _normalize_email(command.email)
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
    if len(first_name) > 80 or len(last_name) > 80:
        raise _error(
            "invalid_command",
            "Staff identity field length exceeds the canonical record limit.",
        )
    return email, first_name, last_name


def _normalize_email(value: str | None) -> str:
    email = (value or "").strip().lower()
    try:
        normalized = str(_EMAIL_ADAPTER.validate_python(email)).strip().lower()
    except ValidationError as exc:
        raise _error(
            "invalid_command",
            "Staff email address is not valid.",
            field="email",
        ) from exc
    if len(normalized) > STAFF_LOGIN_IDENTITY_MAX_LENGTH:
        raise _error(
            "invalid_command",
            "Staff email exceeds the local login identity limit.",
            field="email",
        )
    return normalized


def _validate_update_values(
    command: UpdateStaffIdentityCommand,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    if not command.fields:
        raise _error(
            "invalid_command",
            "At least one staff identity field must be supplied.",
            field="fields",
        )
    first_name = (
        (command.first_name or "").strip()
        if StaffIdentityField.first_name in command.fields
        else None
    )
    last_name = (
        (command.last_name or "").strip()
        if StaffIdentityField.last_name in command.fields
        else None
    )
    display_name = (
        (command.display_name or "").strip() or None
        if StaffIdentityField.display_name in command.fields
        else None
    )
    email = (
        _normalize_email(command.email)
        if StaffIdentityField.email in command.fields
        else None
    )
    phone = (
        (command.phone or "").strip() or None
        if StaffIdentityField.phone in command.fields
        else None
    )
    if StaffIdentityField.first_name in command.fields and not first_name:
        raise _error(
            "invalid_command",
            "Staff first name cannot be empty.",
            field="first_name",
        )
    if StaffIdentityField.last_name in command.fields and not last_name:
        raise _error(
            "invalid_command",
            "Staff last name cannot be empty.",
            field="last_name",
        )
    if first_name is not None and len(first_name) > 80:
        raise _error("invalid_command", "Staff first name is too long.")
    if last_name is not None and len(last_name) > 80:
        raise _error("invalid_command", "Staff last name is too long.")
    if display_name is not None and len(display_name) > 120:
        raise _error("invalid_command", "Staff display name is too long.")
    if phone is not None and len(phone) > 40:
        raise _error("invalid_command", "Staff phone number is too long.")
    return first_name, last_name, display_name, email, phone


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


def _lock_staff_identity(
    db: Session,
    *,
    user_id: UUID,
    new_email: str | None = None,
) -> tuple[SystemUser, str, str]:
    observed_email_value = db.execute(
        select(SystemUser.email).where(SystemUser.id == user_id)
    ).scalar_one_or_none()
    if observed_email_value is None:
        raise _error(
            "staff_account_not_found",
            "Staff account was not found.",
            user_id=str(user_id),
        )
    observed_email = _normalize_email(observed_email_value)
    desired_email = new_email or observed_email
    for locked_email in sorted({observed_email, desired_email}):
        _acquire_identity_lock(db, locked_email)
    user = _locked_user(db, user_id)
    if _normalize_email(user.email) != observed_email:
        raise _error(
            "concurrent_identity_change",
            "Staff identity changed while this command was acquiring its locks.",
            user_id=str(user_id),
        )
    return user, observed_email, desired_email


def _locked_local_credentials(
    db: Session,
    user_id: UUID,
) -> tuple[UserCredential, ...]:
    return tuple(
        db.execute(
            select(UserCredential)
            .where(UserCredential.system_user_id == user_id)
            .where(UserCredential.provider == AuthProvider.local)
            .order_by(UserCredential.created_at.asc(), UserCredential.id.asc())
            .with_for_update()
        ).scalars()
    )


def _one_local_credential(db: Session, user_id: UUID) -> UserCredential:
    credentials = _locked_local_credentials(db, user_id)
    if not credentials:
        raise _error(
            "credential_not_found",
            "Staff account does not have a local login credential.",
            user_id=str(user_id),
        )
    if len(credentials) != 1:
        raise _error(
            "credential_ambiguous",
            "Staff account has multiple local login credentials.",
            user_id=str(user_id),
            credential_count=len(credentials),
        )
    return credentials[0]


def _create_placeholder_local_credential(
    db: Session,
    *,
    user_id: UUID,
    email: str,
    is_active: bool,
) -> UserCredential:
    credential = UserCredential(
        system_user_id=user_id,
        provider=AuthProvider.local,
        username=email,
        password_hash=auth_flow_service.hash_password(secrets.token_urlsafe(32)),
        must_change_password=True,
        is_active=is_active,
    )
    db.add(credential)
    db.flush()
    return credential


def _ensure_login_identity_available(
    db: Session,
    *,
    user_id: UUID,
    email: str,
) -> None:
    system_user_conflict = db.execute(
        select(SystemUser.id)
        .where(SystemUser.id != user_id)
        .where(func.lower(SystemUser.email) == email)
        .limit(1)
    ).scalar_one_or_none()
    credential_conflict = db.execute(
        select(UserCredential.id)
        .where(UserCredential.provider == AuthProvider.local)
        .where(
            or_(
                UserCredential.system_user_id.is_(None),
                UserCredential.system_user_id != user_id,
            )
        )
        .where(func.lower(UserCredential.username) == email)
        .limit(1)
    ).scalar_one_or_none()
    if system_user_conflict is not None or credential_conflict is not None:
        raise _error(
            "identity_conflict",
            "Staff login identity conflicts with an existing canonical record.",
        )


def close_principal_access(db: Session, user_id: UUID) -> tuple[int, int]:
    """Deactivate every credential mechanism and revoke every live session.

    The single consequence of deactivating an authentication principal. It is
    exported because `set_staff_account_active` is not the only caller that may
    legitimately deactivate a principal — vendor revocation does too — and the
    consequence must not be re-implemented per caller. A caller that flips
    `SystemUser.is_active` without this leaves an authenticable account behind,
    which is what `tests/architecture/test_principal_deactivation_guard.py`
    refuses.

    Idempotent: safe to re-invoke on a principal already inactive, which is the
    remediation path for accounts deactivated before this consequence existed.

    Returns (credentials_deactivated, sessions_revoked).
    """

    credentials = int(
        db.query(UserCredential)
        .filter(
            UserCredential.system_user_id == user_id,
            UserCredential.is_active.is_(True),
        )
        .update({"is_active": False}, synchronize_session=False)
        or 0
    )
    return credentials, _revoke_active_sessions(db, user_id)


def _revoke_active_sessions(db: Session, user_id: UUID) -> int:
    now = datetime.now(UTC)
    return int(
        db.query(AuthSession)
        .filter(
            AuthSession.system_user_id == user_id,
            AuthSession.status == SessionStatus.active,
            AuthSession.revoked_at.is_(None),
        )
        .update(
            {"status": SessionStatus.revoked, "revoked_at": now},
            synchronize_session=False,
        )
        or 0
    )


def _require_self_or_assign_scope(
    command: UpdateStaffIdentityCommand,
    *,
    actor_type: AuditActorType,
    actor_id: str,
) -> None:
    if command.context.scope == STAFF_ASSIGN_SCOPE:
        return
    if (
        command.context.scope != STAFF_PROFILE_SCOPE
        or actor_type != AuditActorType.user
        or actor_id != str(command.user_id)
        or command.new_password is not None
    ):
        raise _error(
            "invalid_command_context",
            "Self-service staff identity changes must target the authenticated user.",
        )


def _require_admin_password_actor(
    db: Session,
    *,
    actor_type: AuditActorType,
    actor_id: str,
) -> None:
    if actor_type != AuditActorType.user:
        raise _error(
            "password_update_forbidden",
            "Only an administrator can set a staff password directly.",
        )
    try:
        actor_uuid = UUID(actor_id)
    except ValueError as exc:
        raise _error(
            "password_update_forbidden",
            "Only an administrator can set a staff password directly.",
        ) from exc
    roles = assignment_service.system_user_role_names(db, actor_uuid)
    if not any(role.lower() == "admin" for role in roles):
        raise _error(
            "password_update_forbidden",
            "Only an administrator can set a staff password directly.",
        )


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


def update_staff_identity(
    db: Session,
    command: UpdateStaffIdentityCommand,
) -> StaffIdentityOutcome:
    """Update canonical staff profile and local login identity atomically."""

    def operation() -> StaffIdentityOutcome:
        actor_type, actor_id = _validate_context(
            command.context,
            allowed_scopes=(STAFF_ASSIGN_SCOPE, STAFF_PROFILE_SCOPE),
        )
        _require_self_or_assign_scope(
            command,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        first_name, last_name, display_name, email, phone = _validate_update_values(
            command
        )
        user, _current_email, desired_email = _lock_staff_identity(
            db,
            user_id=command.user_id,
            new_email=email,
        )
        _ensure_login_identity_available(
            db,
            user_id=user.id,
            email=desired_email,
        )
        credential = _one_local_credential(db, user.id)

        changed_fields: list[StaffIdentityField] = []
        desired_values: tuple[tuple[StaffIdentityField, str | None], ...] = (
            (StaffIdentityField.first_name, first_name),
            (StaffIdentityField.last_name, last_name),
            (StaffIdentityField.display_name, display_name),
            (StaffIdentityField.email, email),
            (StaffIdentityField.phone, phone),
        )
        for field, value in desired_values:
            if field not in command.fields:
                continue
            if getattr(user, field.value) != value:
                setattr(user, field.value, value)
                changed_fields.append(field)

        credential_reconciled = credential.username != desired_email
        if credential_reconciled:
            credential.username = desired_email

        password_changed = command.new_password is not None
        if password_changed:
            _require_admin_password_actor(
                db,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            from app.services.auth_flow import hash_password, password_min_length_for

            minimum = password_min_length_for(db, "system_user")
            assert command.new_password is not None
            if len(command.new_password) < minimum:
                raise _error(
                    "invalid_password",
                    f"Password must be at least {minimum} characters.",
                    minimum_length=minimum,
                )
            credential.password_hash = hash_password(command.new_password)
            credential.must_change_password = command.require_password_change
            credential.password_updated_at = datetime.now(UTC)
            credential.failed_login_attempts = 0
            credential.locked_until = None

        login_identity_changed = (
            StaffIdentityField.email in changed_fields or credential_reconciled
        )
        revoked_sessions = (
            _revoke_active_sessions(db, user.id)
            if login_identity_changed or password_changed
            else 0
        )
        _invalidate_auth_after_commit(db, user.id)
        changed_fields_tuple = tuple(
            sorted(changed_fields, key=lambda item: item.value)
        )
        metadata = {
            "changed_fields": [field.value for field in changed_fields_tuple],
            "credential_reconciled": credential_reconciled,
            "password_changed": password_changed,
            "revoked_sessions": revoked_sessions,
            "email_sha256": hashlib.sha256(desired_email.encode()).hexdigest(),
        }
        _stage_audit(
            db,
            action="auth.staff_identity_updated",
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata=metadata,
        )
        if changed_fields_tuple or credential_reconciled or password_changed:
            _emit_staff_event(
                db,
                event_type=EventType.staff_account_identity_changed,
                user=user,
                context=command.context,
                payload={"user_id": str(user.id), **metadata},
            )
        return StaffIdentityOutcome(
            user_id=user.id,
            email=desired_email,
            display_name=user.display_name,
            credential_username=desired_email,
            credential_active=credential.is_active,
            changed_fields=changed_fields_tuple,
            credential_reconciled=credential_reconciled,
            password_changed=password_changed,
            revoked_sessions=revoked_sessions,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    try:
        return execute_owner_command(
            db,
            definition=_UPDATE_IDENTITY_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "Staff login identity conflicts with an existing canonical record.",
        ) from exc


def prepare_staff_credential_recovery(
    db: Session,
    command: PrepareStaffCredentialRecoveryCommand,
) -> StaffCredentialPreparationOutcome:
    """Prepare one canonical active credential before invite/reset delivery."""

    def operation() -> StaffCredentialPreparationOutcome:
        actor_type, actor_id = _validate_context(command.context)
        user, _current_email, email = _lock_staff_identity(
            db,
            user_id=command.user_id,
        )
        if not user.is_active:
            raise _error(
                "inactive_staff_account",
                "Activate the staff account before sending login recovery.",
                user_id=str(user.id),
            )
        _ensure_login_identity_available(db, user_id=user.id, email=email)
        credentials = _locked_local_credentials(db, user.id)
        if len(credentials) > 1:
            raise _error(
                "credential_ambiguous",
                "Staff account has multiple local login credentials.",
                user_id=str(user.id),
                credential_count=len(credentials),
            )
        created = not credentials
        if created:
            credential = _create_placeholder_local_credential(
                db,
                user_id=user.id,
                email=email,
                is_active=True,
            )
        else:
            credential = credentials[0]
        changed = created
        credential_reconciled = credential.username != email
        if credential_reconciled:
            credential.username = email
            changed = True
        if not credential.is_active:
            credential.is_active = True
            changed = True
        if not credential.must_change_password:
            credential.must_change_password = True
            changed = True
        revoked_sessions = (
            _revoke_active_sessions(db, user.id) if credential_reconciled else 0
        )
        _invalidate_auth_after_commit(db, user.id)
        metadata = {
            "created": created,
            "changed": changed,
            "credential_reconciled": credential_reconciled,
            "revoked_sessions": revoked_sessions,
            "email_sha256": hashlib.sha256(email.encode()).hexdigest(),
        }
        _stage_audit(
            db,
            action="auth.staff_credential_recovery_prepared",
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata=metadata,
        )
        if changed:
            _emit_staff_event(
                db,
                event_type=EventType.staff_account_credential_reconciled,
                user=user,
                context=command.context,
                payload={"user_id": str(user.id), **metadata},
            )
        return StaffCredentialPreparationOutcome(
            user_id=user.id,
            email=email,
            credential_id=credential.id,
            created=created,
            changed=changed,
            revoked_sessions=revoked_sessions,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    try:
        return execute_owner_command(
            db,
            definition=_PREPARE_RECOVERY_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "Staff login identity conflicts with an existing canonical record.",
        ) from exc


def list_staff_login_identity_drift(
    db: Session,
) -> tuple[StaffLoginIdentityDrift, ...]:
    """Return PII-safe staff login drift evidence for preview and repair."""

    users = tuple(
        db.execute(select(SystemUser).order_by(SystemUser.id.asc())).scalars()
    )
    credentials = tuple(
        db.execute(
            select(UserCredential)
            .where(UserCredential.provider == AuthProvider.local)
            .order_by(UserCredential.id.asc())
        ).scalars()
    )
    by_user: dict[UUID, list[UserCredential]] = {}
    username_owners: dict[str, set[UUID]] = {}
    for credential in credentials:
        if credential.system_user_id is not None:
            by_user.setdefault(credential.system_user_id, []).append(credential)
        if credential.username:
            username_owners.setdefault(credential.username.strip().lower(), set()).add(
                credential.id
            )

    drift: list[StaffLoginIdentityDrift] = []
    for user in users:
        email = user.email.strip().lower()
        email_digest = hashlib.sha256(email.encode()).hexdigest()
        rows = by_user.get(user.id, [])
        if not rows:
            drift.append(
                StaffLoginIdentityDrift(
                    user_id=user.id,
                    issue=StaffLoginIdentityIssue.missing_credential,
                    email_sha256=email_digest,
                )
            )
            if username_owners.get(email):
                drift.append(
                    StaffLoginIdentityDrift(
                        user_id=user.id,
                        issue=StaffLoginIdentityIssue.username_conflict,
                        email_sha256=email_digest,
                    )
                )
            continue
        if len(rows) != 1:
            drift.append(
                StaffLoginIdentityDrift(
                    user_id=user.id,
                    issue=StaffLoginIdentityIssue.multiple_credentials,
                    email_sha256=email_digest,
                )
            )
            continue
        credential = rows[0]
        if (credential.username or "").strip().lower() != email:
            owners = username_owners.get(email, set()) - {credential.id}
            drift.append(
                StaffLoginIdentityDrift(
                    user_id=user.id,
                    issue=(
                        StaffLoginIdentityIssue.username_conflict
                        if owners
                        else StaffLoginIdentityIssue.username_mismatch
                    ),
                    email_sha256=email_digest,
                )
            )
        if credential.is_active != user.is_active:
            drift.append(
                StaffLoginIdentityDrift(
                    user_id=user.id,
                    issue=StaffLoginIdentityIssue.activation_mismatch,
                    email_sha256=email_digest,
                )
            )
    return tuple(drift)


def get_staff_login_identity_view(
    db: Session,
    *,
    user_id: UUID,
) -> StaffLoginIdentityView | None:
    """Resolve credential status and recovery eligibility from owner state."""

    user = db.get(SystemUser, user_id)
    if user is None:
        return None
    credentials = tuple(
        db.execute(
            select(UserCredential)
            .where(UserCredential.system_user_id == user.id)
            .where(UserCredential.provider == AuthProvider.local)
            .order_by(UserCredential.created_at.asc(), UserCredential.id.asc())
        ).scalars()
    )
    conflict = db.execute(
        select(UserCredential.id)
        .where(UserCredential.provider == AuthProvider.local)
        .where(
            or_(
                UserCredential.system_user_id.is_(None),
                UserCredential.system_user_id != user.id,
            )
        )
        .where(func.lower(UserCredential.username) == user.email.strip().lower())
        .limit(1)
    ).scalar_one_or_none()
    if not credentials:
        missing_issue = (
            StaffLoginIdentityIssue.username_conflict
            if conflict is not None
            else StaffLoginIdentityIssue.missing_credential
        )
        blocked_by = (
            StaffCredentialRecoveryBlock.identity_conflict
            if conflict is not None
            else (
                StaffCredentialRecoveryBlock.inactive_account
                if not user.is_active
                else None
            )
        )
        return StaffLoginIdentityView(
            credential=None,
            issue=missing_issue,
            recovery=StaffCredentialRecoveryEligibility(
                allowed=blocked_by is None,
                blocked_by=blocked_by,
            ),
        )
    if len(credentials) != 1:
        return StaffLoginIdentityView(
            credential=None,
            issue=StaffLoginIdentityIssue.multiple_credentials,
            recovery=StaffCredentialRecoveryEligibility(
                allowed=False,
                blocked_by=StaffCredentialRecoveryBlock.multiple_credentials,
            ),
        )
    credential = credentials[0]
    aligned = (credential.username or "").strip().lower() == user.email.strip().lower()
    activation_aligned = credential.is_active == user.is_active
    issue = (
        StaffLoginIdentityIssue.username_conflict
        if conflict is not None
        else (
            StaffLoginIdentityIssue.username_mismatch
            if not aligned
            else (
                None
                if activation_aligned
                else StaffLoginIdentityIssue.activation_mismatch
            )
        )
    )
    status = (
        StaffCredentialDisplayStatus.needs_reconciliation
        if issue is not None
        else (
            StaffCredentialDisplayStatus.active
            if credential.is_active
            else StaffCredentialDisplayStatus.disabled
        )
    )
    blocked_by = (
        StaffCredentialRecoveryBlock.inactive_account
        if not user.is_active
        else (
            StaffCredentialRecoveryBlock.identity_conflict
            if conflict is not None
            else None
        )
    )
    return StaffLoginIdentityView(
        credential=StaffLoginCredentialView(
            username=credential.username,
            is_active=credential.is_active,
            must_change_password=credential.must_change_password,
            password_updated_at=credential.password_updated_at,
            status=status,
        ),
        issue=issue,
        recovery=StaffCredentialRecoveryEligibility(
            allowed=blocked_by is None,
            blocked_by=blocked_by,
        ),
    )


def reconcile_staff_login_identity(
    db: Session,
    command: ReconcileStaffLoginIdentityCommand,
) -> StaffLoginIdentityReconciliationOutcome:
    """Repair reviewed missing, username, or activation drift."""

    def operation() -> StaffLoginIdentityReconciliationOutcome:
        actor_type, actor_id = _validate_context(command.context)
        user, _current_email, email = _lock_staff_identity(
            db,
            user_id=command.user_id,
        )
        email_digest = hashlib.sha256(email.encode()).hexdigest()
        if email_digest != command.expected_email_sha256:
            raise _error(
                "stale_identity_evidence",
                "Staff login identity changed after the repair preview.",
                user_id=str(user.id),
            )
        _ensure_login_identity_available(db, user_id=user.id, email=email)
        credentials = _locked_local_credentials(db, user.id)
        if len(credentials) > 1:
            raise _error(
                "credential_ambiguous",
                "Staff account has multiple local login credentials.",
                user_id=str(user.id),
                credential_count=len(credentials),
            )
        credential_created = not credentials
        credential = (
            _create_placeholder_local_credential(
                db,
                user_id=user.id,
                email=email,
                is_active=user.is_active,
            )
            if credential_created
            else credentials[0]
        )
        username_reconciled = credential.username != email
        if username_reconciled:
            credential.username = email
        activation_reconciled = credential.is_active != user.is_active
        if activation_reconciled:
            credential.is_active = user.is_active
        changed = credential_created or username_reconciled or activation_reconciled
        revoke_for_deactivation = activation_reconciled and not user.is_active
        revoked_sessions = (
            _revoke_active_sessions(db, user.id)
            if username_reconciled or revoke_for_deactivation
            else 0
        )
        _invalidate_auth_after_commit(db, user.id)
        metadata = {
            "credential_created": credential_created,
            "username_reconciled": username_reconciled,
            "activation_reconciled": activation_reconciled,
            "revoked_sessions": revoked_sessions,
            "email_sha256": email_digest,
        }
        _stage_audit(
            db,
            action="auth.staff_login_identity_reconciled",
            user_id=user.id,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata=metadata,
        )
        if changed:
            _emit_staff_event(
                db,
                event_type=EventType.staff_account_credential_reconciled,
                user=user,
                context=command.context,
                payload={"user_id": str(user.id), **metadata},
            )
        return StaffLoginIdentityReconciliationOutcome(
            user_id=user.id,
            email=email,
            credential_id=credential.id,
            credential_created=credential_created,
            username_reconciled=username_reconciled,
            activation_reconciled=activation_reconciled,
            revoked_sessions=revoked_sessions,
            changed=changed,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    try:
        return execute_owner_command(
            db,
            definition=_RECONCILE_LOGIN_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "Staff login identity conflicts with an existing canonical record.",
        ) from exc


def set_staff_account_active(
    db: Session,
    command: SetStaffAccountActiveCommand,
) -> StaffAccountOutcome:
    """Converge principal, credential, and session access state atomically."""

    def operation() -> StaffAccountOutcome:
        actor_type, actor_id = _validate_context(command.context)
        if command.is_active:
            user, _current_email, email = _lock_staff_identity(
                db,
                user_id=command.user_id,
            )
        else:
            user = _locked_user(db, command.user_id)
            email = None
        if not command.is_active:
            assignment_service.ensure_can_deactivate_system_user(db, user.id)
        state_changed = user.is_active != command.is_active
        user.is_active = command.is_active
        credential_reconciled = False
        credential_created = False
        if command.is_active:
            assert email is not None
            _ensure_login_identity_available(db, user_id=user.id, email=email)
            credentials = _locked_local_credentials(db, user.id)
            if len(credentials) > 1:
                raise _error(
                    "credential_ambiguous",
                    "Staff account has multiple local login credentials.",
                    user_id=str(user.id),
                    credential_count=len(credentials),
                )
            credential_created = not credentials
            credential = (
                _create_placeholder_local_credential(
                    db,
                    user_id=user.id,
                    email=email,
                    is_active=True,
                )
                if credential_created
                else credentials[0]
            )
            credential_reconciled = credential.username != email
            if credential_reconciled:
                credential.username = email
            credential_changes = int(credential_created or not credential.is_active)
            credential.is_active = True
        else:
            credential_changes = 0
        revoked_sessions = 0
        if not command.is_active:
            # EVERY mechanism, not only local. Deactivation is a statement about
            # the principal, so leaving a RADIUS (or any future provider)
            # credential active would keep an authentication path open for an
            # account the operator believes is closed. The activation branch
            # above stays local-only on purpose: re-enabling an account must not
            # silently restore a mechanism nobody asked to restore.
            credential_changes, revoked_sessions = close_principal_access(db, user.id)
        elif credential_reconciled:
            revoked_sessions = _revoke_active_sessions(db, user.id)
        changed = bool(
            state_changed
            or credential_changes
            or credential_reconciled
            or revoked_sessions
        )
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
                "credential_created": credential_created,
                "credential_reconciled": credential_reconciled,
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
                    "credential_created": credential_created,
                    "credential_reconciled": credential_reconciled,
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
