"""Mutation helpers for admin system user management routes."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db import finish_read_transaction
from app.models.auth import (
    ApiKey,
    MFAMethod,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.domain_settings import SettingDomain
from app.models.system_user import SystemUser
from app.services import auth_cache, credential_recovery, staff_provisioning
from app.services import system_user_assignments as assignment_service
from app.services import web_system_users as web_system_users_service
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid
from app.services.owner_commands import CommandContext
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def _invite_login_route_for_user() -> str:
    return "/auth/login?next=/admin/dashboard"


def _user_invite_expiry_minutes(db: Session) -> int:
    value = resolve_value(db, SettingDomain.auth, "user_invite_expiry_minutes") or 1440
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 1440
    return parsed if parsed > 0 else 1440


def disable_user_mfa(db: Session, *, user_id: str, actor_id: str | None = None) -> None:
    """Disable all MFA methods for a system user."""
    from app.services.audit_adapter import record_audit_event

    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    db.query(MFAMethod).filter(MFAMethod.system_user_id == system_user.id).update(
        {
            "enabled": False,
            "is_active": False,
            "failed_attempts": 0,
            "locked_until": None,
        }
    )
    record_audit_event(
        db,
        action="auth.mfa_disabled",
        entity_type="system_user",
        entity_id=str(system_user.id),
        actor_id=actor_id,
        metadata={"email": system_user.email},
    )
    db.commit()
    auth_cache.invalidate_principal("system_user", str(system_user.id))


def disable_subscriber_mfa(
    db: Session, *, subscriber_id: str, actor_id: str | None = None
) -> None:
    """Disable all MFA methods for a subscriber (customer/reseller portal user).

    Admin recovery path for customers who lost their authenticator; without it
    a locked-out subscriber has no way back in short of account deletion.
    """
    from app.models.subscriber import Subscriber
    from app.services.audit_adapter import record_audit_event

    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
    if not subscriber:
        raise ValueError("Subscriber not found")
    db.query(MFAMethod).filter(MFAMethod.subscriber_id == subscriber.id).update(
        {
            "enabled": False,
            "is_active": False,
            "failed_attempts": 0,
            "locked_until": None,
        }
    )
    record_audit_event(
        db,
        action="auth.mfa_disabled",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        actor_id=actor_id,
        metadata={"email": subscriber.email},
    )
    db.commit()
    auth_cache.invalidate_principal("subscriber", str(subscriber.id))


def reset_user_password(db: Session, *, user_id: str) -> str:
    """Reset active credential password and require change at next login."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    temp_password = secrets.token_urlsafe(16)
    db.query(UserCredential).filter(
        UserCredential.system_user_id == system_user.id,
        UserCredential.is_active.is_(True),
    ).update(
        {
            "password_hash": hash_password(temp_password),
            "must_change_password": True,  # nosec
            "password_updated_at": datetime.now(UTC),
            # An admin reset must also unlock a locked-out account.
            "failed_login_attempts": 0,
            "locked_until": None,
        }
    )
    db.commit()
    auth_cache.invalidate_principal("system_user", str(system_user.id))
    return temp_password


def bulk_set_user_type(
    db: Session,
    *,
    user_ids: list[str],
    user_type: str,
) -> int:
    """Bulk update user type for selected system users."""
    if not user_ids:
        return 0
    normalized_type = web_system_users_service.normalize_user_type(user_type)
    uuids = [coerce_uuid(user_id) for user_id in user_ids]
    updated = (
        db.query(SystemUser)
        .filter(SystemUser.id.in_(uuids))
        .update({"user_type": normalized_type}, synchronize_session=False)
    )
    db.commit()
    return int(updated or 0)


def send_user_invite(
    db: Session,
    *,
    email: str,
    next_login_path: str | None = None,
) -> str:
    """Send invitation email to a newly created user.

    Returns a status note describing the outcome.
    """
    from app.services import email as email_service

    reset = credential_recovery.issue_reset_capability_for_email(
        db,
        email,
        ttl_minutes=_user_invite_expiry_minutes(db),
    )
    if reset is None or not reset.token:
        return "User created, but no reset token was generated."

    sent = email_service.send_user_invite_email(
        db,
        to_email=email,
        reset_token=reset.token,
        person_name=reset.person_name,
        next_login_path=next_login_path,
        expires_minutes=reset.ttl_minutes,
    )
    if sent:
        return "Invitation sent. Password reset email delivered."
    return "User created, but the reset email could not be sent."


def send_user_invite_for_user(
    db: Session,
    *,
    command: staff_provisioning.PrepareStaffCredentialRecoveryCommand,
) -> str:
    """Send invite email for an existing user."""
    prepared = staff_provisioning.prepare_staff_credential_recovery(db, command)
    principal_id = prepared.user_id
    next_login_path = _invite_login_route_for_user()
    reset = credential_recovery.issue_exact_reset_capability(
        db,
        principal_type="system_user",
        principal_id=principal_id,
        ttl_minutes=_user_invite_expiry_minutes(db),
    )
    if reset is not None:
        from app.services import access_invitations

        access_invitations.record_issued(
            db,
            principal_type="system_user",
            principal_id=reset.principal_id,
            purpose="user_invite",
            email=reset.email,
            ttl_minutes=reset.ttl_minutes,
            source="admin_web",
        )
        db.commit()
    return _send_user_invite_capability(
        db,
        reset=reset,
        next_login_path=next_login_path,
    )


def send_subscriber_invite(
    db: Session,
    *,
    subscriber_id: str,
    next_login_path: str | None = None,
) -> str:
    """Send an invitation using one exact subscriber reset capability."""

    reset = credential_recovery.issue_exact_reset_capability(
        db,
        principal_type="subscriber",
        principal_id=coerce_uuid(subscriber_id),
        ttl_minutes=_user_invite_expiry_minutes(db),
    )
    return _send_user_invite_capability(
        db,
        reset=reset,
        next_login_path=next_login_path,
    )


def _send_user_invite_capability(
    db: Session,
    *,
    reset: credential_recovery.PasswordResetCapability | None,
    next_login_path: str | None,
) -> str:
    from app.services import email as email_service

    if reset is None or not reset.token:
        return "User created, but no reset token was generated."
    sent = email_service.send_user_invite_email(
        db,
        to_email=reset.email,
        reset_token=reset.token,
        person_name=reset.person_name,
        next_login_path=next_login_path,
        expires_minutes=reset.ttl_minutes,
        token_in_fragment=True,
    )
    if sent:
        return "Invitation sent. Password reset email delivered."
    return "User created, but the reset email could not be sent."


def user_invite_succeeded(note: str) -> bool:
    """Return whether the stable invitation status represents delivery."""

    return note == "Invitation sent. Password reset email delivered."


def bulk_send_user_invites(
    db: Session,
    *,
    commands: tuple[staff_provisioning.PrepareStaffCredentialRecoveryCommand, ...],
) -> tuple[int, int]:
    """Send welcome invites for selected users.

    Returns (sent_count, failed_count).
    """
    sent_count = 0
    failed_count = 0

    for command in commands:
        try:
            note = send_user_invite_for_user(db, command=command)
            if user_invite_succeeded(note):
                sent_count += 1
            else:
                failed_count += 1
        except Exception:
            failed_count += 1
        finally:
            finish_read_transaction(db)

    return sent_count, failed_count


def send_password_reset_link_for_user(
    db: Session,
    *,
    command: staff_provisioning.PrepareStaffCredentialRecoveryCommand,
) -> str:
    """Send password reset link email for an existing user."""
    prepared = staff_provisioning.prepare_staff_credential_recovery(db, command)
    principal_id = prepared.user_id
    next_login_path = _invite_login_route_for_user()
    outcome = credential_recovery.request_exact_password_recovery(
        db,
        credential_recovery.RequestExactPasswordRecoveryCommand(
            context=CommandContext.system(
                actor=command.context.actor,
                scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
                reason="Administrator requested staff password recovery",
                correlation_id=command.context.correlation_id,
                causation_id=command.context.command_id,
                idempotency_key=(
                    f"{command.context.idempotency_key or command.context.command_id}:"
                    "delivery"
                ),
            ),
            principal_type="system_user",
            principal_id=principal_id,
            next_login_path=next_login_path,
        ),
    )
    if outcome.delivery_requested:
        return "Password reset link queued successfully."
    return "Password reset link could not be queued."


def delete_user_records(db: Session, *, user_id: str) -> SystemUser:
    """Delete system user and linked auth/RBAC rows."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")

    db.query(UserCredential).filter(
        UserCredential.system_user_id == system_user.id
    ).delete(synchronize_session=False)
    db.query(MFAMethod).filter(MFAMethod.system_user_id == system_user.id).delete(
        synchronize_session=False
    )
    db.query(AuthSession).filter(AuthSession.system_user_id == system_user.id).delete(
        synchronize_session=False
    )
    db.query(ApiKey).filter(ApiKey.system_user_id == system_user.id).delete(
        synchronize_session=False
    )
    assignment_service.remove_all_for_system_user(db, system_user.id)
    db.delete(system_user)
    db.commit()
    return system_user


def set_device_login(
    db: Session,
    *,
    user_id: str,
    enabled: bool,
    secret: str | None,
    commit: bool = True,
) -> SystemUser:
    """Enable/disable device login and optionally set/rotate the secret.

    With ``commit=False`` the change is flushed but not committed, so the caller
    can commit it atomically with an audit-log row. This avoids the "credential
    already changed in the DB but the request reported failure" divergence when
    a later audit write or sync-enqueue fails.
    """
    from app.services.credential_crypto import encrypt_credential

    u = db.get(SystemUser, coerce_uuid(user_id))
    if not u:
        raise ValueError("User not found")
    u.device_login_enabled = enabled
    if enabled:
        u.device_login_revoked_at = None
    if secret:
        u.device_login_secret = encrypt_credential(secret)
        u.device_login_secret_set_at = datetime.now(UTC)
    db.add(u)
    if commit:
        db.commit()
        db.refresh(u)
    else:
        db.flush()
    return u


def revoke_device_login(
    db: Session, *, user_id: str, commit: bool = True
) -> SystemUser:
    """Revoke device login access: disable and timestamp revocation.

    ``commit=False`` flushes without committing (see ``set_device_login``).
    """
    u = db.get(SystemUser, coerce_uuid(user_id))
    if not u:
        raise ValueError("User not found")
    u.device_login_enabled = False
    u.device_login_revoked_at = datetime.now(UTC)
    db.add(u)
    if commit:
        db.commit()
        db.refresh(u)
    else:
        db.flush()
    return u


def bulk_delete_user_records(db: Session, *, user_ids: list[str]) -> tuple[int, int]:
    """Delete inactive system users that have no linked records.

    Returns (deleted_count, skipped_count).
    """
    deleted_count = 0
    skipped_count = 0

    for user_id in user_ids:
        system_user = db.get(SystemUser, coerce_uuid(user_id))
        if not system_user or system_user.is_active:
            skipped_count += 1
            continue

        db.query(UserCredential).filter(
            UserCredential.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.query(MFAMethod).filter(MFAMethod.system_user_id == system_user.id).delete(
            synchronize_session=False
        )
        db.query(AuthSession).filter(
            AuthSession.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.system_user_id == system_user.id).delete(
            synchronize_session=False
        )
        assignment_service.remove_all_for_system_user(db, system_user.id)
        db.delete(system_user)
        deleted_count += 1

    db.commit()
    return deleted_count, skipped_count
