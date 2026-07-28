"""Canonical password-recovery policy, capability, and credential owner.

Public recovery requests persist only PII-safe event context.  The event
consequence creates a durable communication intent, and the delivery worker
mints the short-lived bearer immediately before transport.  Reset completion
updates the credential, revokes database sessions, stages audit evidence, and
emits its versioned state event in one owner-managed transaction.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from jose import JWTError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.domain_settings import SettingDomain
from app.models.notification import Notification
from app.models.subscriber import ResellerUser, Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.services.audit_adapter import stage_audit_event
from app.services.context_signing import sign_context_token, verify_context_token
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

CREDENTIAL_RECOVERY_SCOPE = "auth:credential_recovery"
PASSWORD_RESET_TOKEN_TYPE = "password_reset"
SYSTEM_USER_RESET_TTL_CAP_MINUTES = 60

_REQUEST_COMMAND = OwnerCommandDefinition(
    owner="auth.credential_recovery",
    concern="password recovery request and delivery intent",
    name="request_password_recovery",
)
_COMPLETE_COMMAND = OwnerCommandDefinition(
    owner="auth.credential_recovery",
    concern="password reset credential transition",
    name="complete_password_reset",
)


class CredentialRecoveryError(DomainError):
    """Stable, transport-neutral credential recovery failure."""


@dataclass(frozen=True)
class RequestPasswordRecoveryCommand:
    """Enumeration-safe public recovery request by contact email."""

    context: CommandContext
    email: str
    next_login_path: str | None = None


@dataclass(frozen=True)
class RequestExactPasswordRecoveryCommand:
    """Authorized recovery delivery request for one exact principal."""

    context: CommandContext
    principal_type: str
    principal_id: UUID
    next_login_path: str | None = None


@dataclass(frozen=True)
class CompletePasswordResetCommand:
    """Redeem one purpose-bound capability and replace its credential."""

    context: CommandContext
    token: str
    new_password: str


@dataclass(frozen=True)
class PasswordRecoveryRequestOutcome:
    """Enumeration-safe accepted outcome returned without an ORM entity."""

    accepted: bool
    delivery_requested: bool
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class PasswordResetOutcome:
    """Committed credential transition returned without secret material."""

    principal_type: str
    principal_id: UUID
    reset_at: datetime
    sessions_revoked: int
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class PasswordResetCapability:
    """In-memory bearer and display context; never persist this object."""

    token: str
    email: str
    person_name: str | None
    principal_type: str
    principal_id: UUID
    ttl_minutes: int


@dataclass(frozen=True)
class PasswordRecoveryTarget:
    """Non-secret exact delivery target resolved from canonical state."""

    principal_type: str
    principal_id: UUID
    email: str
    person_name: str | None


@dataclass(frozen=True)
class _PrincipalContext:
    principal_type: str
    principal_id: UUID
    email: str
    person_name: str | None


def _error(code: str, message: str, **details: object) -> CredentialRecoveryError:
    return CredentialRecoveryError(
        code=f"auth.credential_recovery.{code}",
        message=message,
        details=details,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _email_digest(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _configured_ttl_minutes(db: Session | None) -> int:
    if db is not None:
        for key in ("password_reset_expiry_minutes", "password_reset_ttl_minutes"):
            parsed = _positive_int(resolve_value(db, SettingDomain.auth, key))
            if parsed is not None:
                return parsed
    return 1440


def _ttl_minutes(
    db: Session | None,
    *,
    principal_type: str,
    override: int | None = None,
) -> int:
    if override is not None and override > 0:
        return override
    configured = _configured_ttl_minutes(db)
    if principal_type == "system_user":
        return min(configured, SYSTEM_USER_RESET_TTL_CAP_MINUTES)
    return configured


def _safe_next_login_path(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or not normalized.startswith("/")
        or normalized.startswith("//")
        or normalized.startswith("/\\")
        or len(normalized) > 500
    ):
        raise _error(
            "invalid_command",
            "Password recovery next-login path is invalid.",
            field="next_login_path",
        )
    return normalized


def _validate_context(context: CommandContext) -> tuple[AuditActorType, str]:
    if context.scope != CREDENTIAL_RECOVERY_SCOPE:
        raise _error(
            "invalid_command",
            "Credential recovery requires authorized scope evidence.",
            field="scope",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Credential recovery actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "Credential recovery actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _principal_context(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    lock: bool = False,
) -> _PrincipalContext | None:
    if principal_type == "subscriber":
        subscriber_statement = select(Subscriber).where(Subscriber.id == principal_id)
        if lock:
            subscriber_statement = subscriber_statement.with_for_update()
        subscriber = db.execute(subscriber_statement).scalar_one_or_none()
        if (
            subscriber is None
            or not subscriber.is_active
            or subscriber.status == SubscriberStatus.canceled
            or not subscriber.email
        ):
            return None
        return _PrincipalContext(
            principal_type=principal_type,
            principal_id=subscriber.id,
            email=subscriber.email.strip().lower(),
            person_name=subscriber.display_name or subscriber.first_name,
        )
    if principal_type == "system_user":
        system_statement = select(SystemUser).where(SystemUser.id == principal_id)
        if lock:
            system_statement = system_statement.with_for_update()
        system_user = db.execute(system_statement).scalar_one_or_none()
        if system_user is None or not system_user.is_active or not system_user.email:
            return None
        return _PrincipalContext(
            principal_type=principal_type,
            principal_id=system_user.id,
            email=system_user.email.strip().lower(),
            person_name=system_user.display_name or system_user.first_name,
        )
    if principal_type == "reseller_user":
        reseller_statement = select(ResellerUser).where(ResellerUser.id == principal_id)
        if lock:
            reseller_statement = reseller_statement.with_for_update()
        reseller_user = db.execute(reseller_statement).scalar_one_or_none()
        if (
            reseller_user is None
            or not reseller_user.is_active
            or not reseller_user.email
        ):
            return None
        return _PrincipalContext(
            principal_type=principal_type,
            principal_id=reseller_user.id,
            email=reseller_user.email.strip().lower(),
            person_name=reseller_user.full_name,
        )
    return None


def _credential_filter(principal_type: str, principal_id: UUID):
    if principal_type == "subscriber":
        return UserCredential.subscriber_id == principal_id
    if principal_type == "system_user":
        return UserCredential.system_user_id == principal_id
    if principal_type == "reseller_user":
        return UserCredential.reseller_user_id == principal_id
    return None


def _active_local_credential(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    lock: bool = False,
) -> UserCredential | None:
    principal_filter = _credential_filter(principal_type, principal_id)
    if principal_filter is None:
        return None
    statement = (
        select(UserCredential)
        .where(
            principal_filter,
            UserCredential.provider == AuthProvider.local,
            UserCredential.is_active.is_(True),
        )
        .order_by(UserCredential.created_at.desc())
    )
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalars().first()


def _principal_for_email(db: Session, email: str) -> _PrincipalContext | None:
    normalized = email.strip().lower()
    if not normalized:
        return None
    subscriber_rows = db.execute(
        select(Subscriber, UserCredential)
        .join(UserCredential, UserCredential.subscriber_id == Subscriber.id)
        .where(
            func.lower(Subscriber.email) == normalized,
            Subscriber.is_active.is_(True),
            Subscriber.status != SubscriberStatus.canceled,
            UserCredential.provider == AuthProvider.local,
            UserCredential.is_active.is_(True),
        )
        .order_by(UserCredential.created_at.desc())
    ).all()
    if subscriber_rows:
        if len(subscriber_rows) > 1:
            logger.warning(
                "Password recovery matched %d credentialed subscribers; "
                "selecting the most recent credential.",
                len(subscriber_rows),
            )
        subscriber, _credential = subscriber_rows[0]
        return _PrincipalContext(
            principal_type="subscriber",
            principal_id=subscriber.id,
            email=subscriber.email.strip().lower(),
            person_name=subscriber.display_name or subscriber.first_name,
        )

    reseller_user = (
        db.execute(
            select(ResellerUser)
            .join(
                UserCredential,
                UserCredential.reseller_user_id == ResellerUser.id,
            )
            .where(
                func.lower(ResellerUser.email) == normalized,
                ResellerUser.is_active.is_(True),
                UserCredential.provider == AuthProvider.local,
                UserCredential.is_active.is_(True),
            )
            .order_by(UserCredential.created_at.desc())
        )
        .scalars()
        .first()
    )
    if reseller_user is not None and reseller_user.email:
        return _PrincipalContext(
            principal_type="reseller_user",
            principal_id=reseller_user.id,
            email=reseller_user.email.strip().lower(),
            person_name=reseller_user.full_name,
        )

    system_user = (
        db.execute(
            select(SystemUser)
            .join(UserCredential, UserCredential.system_user_id == SystemUser.id)
            .where(
                func.lower(SystemUser.email) == normalized,
                SystemUser.is_active.is_(True),
                UserCredential.provider == AuthProvider.local,
                UserCredential.is_active.is_(True),
            )
            .order_by(UserCredential.created_at.desc())
        )
        .scalars()
        .first()
    )
    if system_user is None:
        return None
    return _PrincipalContext(
        principal_type="system_user",
        principal_id=system_user.id,
        email=system_user.email.strip().lower(),
        person_name=system_user.display_name or system_user.first_name,
    )


def issue_exact_reset_capability(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
    ttl_minutes: int | None = None,
) -> PasswordResetCapability | None:
    """Mint a bearer for one exact active local principal in memory only."""

    principal = resolve_exact_recovery_target(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
    )
    if (
        principal is None
        or _active_local_credential(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
        )
        is None
    ):
        return None
    effective_ttl = _ttl_minutes(
        db,
        principal_type=principal_type,
        override=ttl_minutes,
    )
    now = _now()
    token = sign_context_token(
        db,
        {
            "sub": str(principal_id),
            "principal_id": str(principal_id),
            "principal_type": principal_type,
            "email": principal.email,
            "typ": PASSWORD_RESET_TOKEN_TYPE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=effective_ttl)).timestamp()),
        },
    )
    return PasswordResetCapability(
        token=token,
        email=principal.email,
        person_name=principal.person_name,
        principal_type=principal_type,
        principal_id=principal_id,
        ttl_minutes=effective_ttl,
    )


def resolve_exact_recovery_target(
    db: Session,
    *,
    principal_type: str,
    principal_id: UUID,
) -> PasswordRecoveryTarget | None:
    """Resolve one recoverable target without creating bearer material."""

    principal = _principal_context(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
    )
    if (
        principal is None
        or _active_local_credential(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
        )
        is None
    ):
        return None
    return PasswordRecoveryTarget(
        principal_type=principal.principal_type,
        principal_id=principal.principal_id,
        email=principal.email,
        person_name=principal.person_name,
    )


def issue_reset_capability_for_email(
    db: Session,
    email: str,
    *,
    ttl_minutes: int | None = None,
) -> PasswordResetCapability | None:
    """Compatibility/read flow for forced login recovery; never sends email."""

    principal = _principal_for_email(db, email)
    if principal is None:
        return None
    return issue_exact_reset_capability(
        db,
        principal_type=principal.principal_type,
        principal_id=principal.principal_id,
        ttl_minutes=ttl_minutes,
    )


def _request_outcome(
    context: CommandContext, *, delivery_requested: bool
) -> PasswordRecoveryRequestOutcome:
    return PasswordRecoveryRequestOutcome(
        accepted=True,
        delivery_requested=delivery_requested,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _stage_request(
    db: Session,
    *,
    principal: _PrincipalContext,
    context: CommandContext,
    next_login_path: str | None,
    actor_type: AuditActorType,
    actor_id: str,
) -> None:
    digest = _email_digest(principal.email)
    evidence = {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "causation_id": str(context.causation_id) if context.causation_id else None,
        "reason": context.reason,
        "email_sha256": digest,
        "next_login_path": next_login_path,
    }
    stage_audit_event(
        db,
        action="auth.password_recovery_requested",
        entity_type=principal.principal_type,
        entity_id=str(principal.principal_id),
        actor_type=actor_type,
        actor_id=actor_id,
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.password_recovery_requested,
        {
            **evidence,
            "aggregate_type": principal.principal_type,
            "aggregate_id": str(principal.principal_id),
            "aggregate_version": str(context.command_id),
            "principal_type": principal.principal_type,
            "principal_id": str(principal.principal_id),
        },
        actor=context.actor,
        subscriber_id=(
            principal.principal_id if principal.principal_type == "subscriber" else None
        ),
    )


def request_password_recovery(
    db: Session,
    command: RequestPasswordRecoveryCommand,
) -> PasswordRecoveryRequestOutcome:
    """Accept a public request without revealing whether the email exists."""

    def operation() -> PasswordRecoveryRequestOutcome:
        from app.services.rate_limiter_adapter import allow_operation

        actor_type, actor_id = _validate_context(command.context)
        normalized_email = command.email.strip().lower()
        next_login_path = _safe_next_login_path(command.next_login_path)
        if not normalized_email or len(normalized_email) > 320:
            return _request_outcome(command.context, delivery_requested=False)
        decision = allow_operation(
            f"auth:forgot-password:{_email_digest(normalized_email)}",
            limit=3,
            window_seconds=900,
        )
        if not decision.allowed:
            logger.info(
                "Password recovery request rate-limited; retry_after_seconds=%s",
                decision.retry_after_seconds,
            )
            return _request_outcome(command.context, delivery_requested=False)
        principal = _principal_for_email(db, normalized_email)
        if principal is None:
            return _request_outcome(command.context, delivery_requested=False)
        _stage_request(
            db,
            principal=principal,
            context=command.context,
            next_login_path=next_login_path,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        return _request_outcome(command.context, delivery_requested=True)

    return execute_owner_command(
        db,
        definition=_REQUEST_COMMAND,
        context=command.context,
        operation=operation,
    )


def request_exact_password_recovery(
    db: Session,
    command: RequestExactPasswordRecoveryCommand,
) -> PasswordRecoveryRequestOutcome:
    """Queue recovery for one exact principal after adapter authorization."""

    def operation() -> PasswordRecoveryRequestOutcome:
        actor_type, actor_id = _validate_context(command.context)
        next_login_path = _safe_next_login_path(command.next_login_path)
        principal = _principal_context(
            db,
            principal_type=command.principal_type,
            principal_id=command.principal_id,
        )
        if (
            principal is None
            or _active_local_credential(
                db,
                principal_type=command.principal_type,
                principal_id=command.principal_id,
            )
            is None
        ):
            raise _error(
                "credential_not_found",
                "An active local credential was not found.",
                principal_type=command.principal_type,
                principal_id=str(command.principal_id),
            )
        _stage_request(
            db,
            principal=principal,
            context=command.context,
            next_login_path=next_login_path,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        return _request_outcome(command.context, delivery_requested=True)

    return execute_owner_command(
        db,
        definition=_REQUEST_COMMAND,
        context=command.context,
        operation=operation,
    )


def _decode_capability(db: Session, token: str) -> dict[object, object]:
    try:
        payload = verify_context_token(db, token)
    except (JWTError, TypeError, ValueError) as exc:
        raise _error(
            "invalid_reset_capability",
            "Invalid or expired password reset capability.",
        ) from exc
    if payload.get("typ") != PASSWORD_RESET_TOKEN_TYPE:
        raise _error(
            "invalid_reset_capability",
            "Invalid or expired password reset capability.",
        )
    return payload


def _uuid_claim(payload: dict[object, object]) -> UUID:
    value = payload.get("principal_id") or payload.get("sub")
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise _error(
            "invalid_reset_capability",
            "Invalid or expired password reset capability.",
        ) from exc


def _session_filter(principal_type: str, principal_id: UUID):
    if principal_type == "subscriber":
        return AuthSession.subscriber_id == principal_id
    if principal_type == "system_user":
        return AuthSession.system_user_id == principal_id
    if principal_type == "reseller_user":
        return AuthSession.reseller_user_id == principal_id
    return None


def complete_password_reset(
    db: Session,
    command: CompletePasswordResetCommand,
) -> PasswordResetOutcome:
    """Replace one credential and revoke its sessions atomically."""

    def operation() -> PasswordResetOutcome:
        _validate_context(command.context)
        from app.services.auth_flow import hash_password, password_min_length_for

        payload = _decode_capability(db, command.token)
        principal_type = str(payload.get("principal_type") or "subscriber")
        principal_id = _uuid_claim(payload)
        # Enforce the principal-type-aware floor (staff/admin > general minimum).
        minimum = password_min_length_for(db, principal_type)
        if len(command.new_password) < minimum:
            raise _error(
                "invalid_password",
                f"Password must be at least {minimum} characters.",
                minimum_length=minimum,
            )
        token_email = str(payload.get("email") or "").strip().lower()
        if not token_email:
            raise _error(
                "invalid_reset_capability",
                "Invalid or expired password reset capability.",
            )
        principal = _principal_context(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
            lock=True,
        )
        if principal is None or principal.email != token_email:
            raise _error(
                "invalid_reset_capability",
                "Invalid or expired password reset capability.",
            )
        credential = _active_local_credential(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
            lock=True,
        )
        if credential is None:
            raise _error(
                "credential_not_found",
                "An active local credential was not found.",
                principal_type=principal_type,
                principal_id=str(principal_id),
            )

        issued_at = payload.get("iat")
        updated_at = _as_utc(credential.password_updated_at)
        try:
            issued_at_seconds = int(str(issued_at)) if issued_at is not None else None
        except (TypeError, ValueError) as exc:
            raise _error(
                "invalid_reset_capability",
                "Invalid or expired password reset capability.",
            ) from exc
        if (
            issued_at_seconds is not None
            and updated_at is not None
            and issued_at_seconds < int(updated_at.timestamp())
        ):
            raise _error(
                "invalid_reset_capability",
                "Invalid or expired password reset capability.",
            )

        now = _now()
        credential.password_hash = hash_password(command.new_password)
        updated_marker = now
        if issued_at_seconds is not None and int(now.timestamp()) <= issued_at_seconds:
            updated_marker = now + timedelta(seconds=1)
        credential.password_updated_at = updated_marker
        credential.must_change_password = False
        credential.failed_login_attempts = 0
        credential.locked_until = None
        # A completed reset is invitation acceptance when an issued
        # invitation exists for this principal; a pure recovery is a no-op.
        from app.services import access_invitations

        access_invitations.mark_accepted(
            db,
            principal_type=principal_type,
            principal_id=principal_id,
        )

        session_filter = _session_filter(principal_type, principal_id)
        if session_filter is None:
            raise _error(
                "invalid_reset_capability",
                "Invalid or expired password reset capability.",
            )
        revoked_count = (
            db.query(AuthSession)
            .filter(
                session_filter,
                AuthSession.status == SessionStatus.active,
                AuthSession.revoked_at.is_(None),
            )
            .update(
                {"status": SessionStatus.revoked, "revoked_at": now},
                synchronize_session=False,
            )
        )
        revoked = int(revoked_count or 0)
        evidence = {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "causation_id": (
                str(command.context.causation_id)
                if command.context.causation_id
                else None
            ),
            "reason": command.context.reason,
            "email_sha256": _email_digest(principal.email),
            "sessions_revoked": revoked,
        }
        stage_audit_event(
            db,
            action="auth.password_reset_completed",
            entity_type=principal_type,
            entity_id=str(principal_id),
            actor_type=AuditActorType.user,
            actor_id=str(principal_id),
            metadata=evidence,
        )
        emit_event(
            db,
            EventType.password_recovery_completed,
            {
                **evidence,
                "aggregate_type": principal_type,
                "aggregate_id": str(principal_id),
                "aggregate_version": str(command.context.command_id),
                "principal_type": principal_type,
                "principal_id": str(principal_id),
            },
            actor=command.context.actor,
            subscriber_id=principal_id if principal_type == "subscriber" else None,
        )
        return PasswordResetOutcome(
            principal_type=principal_type,
            principal_id=principal_id,
            reset_at=now,
            sessions_revoked=revoked,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_COMPLETE_COMMAND,
        context=command.context,
        operation=operation,
    )


def materialize_password_recovery_email(
    db: Session,
    *,
    notification: Notification,
    context: dict[str, object],
):
    """Revalidate, mint, and render a recovery bearer at delivery time."""

    from app.services import email as email_service
    from app.services.ephemeral_communication_actions import (
        EphemeralActionRejected,
        EphemeralEmailContent,
    )

    if not isinstance(notification, Notification):
        raise EphemeralActionRejected("invalid_notification")
    if set(context) != {
        "principal_type",
        "principal_id",
        "email_sha256",
        "next_login_path",
    }:
        raise EphemeralActionRejected("invalid_context")
    try:
        principal_type = str(context["principal_type"])
        principal_id = UUID(str(context["principal_id"]))
        email_digest = str(context["email_sha256"])
        raw_next_login = context["next_login_path"]
        next_login_path = None if raw_next_login is None else str(raw_next_login)
    except (KeyError, TypeError, ValueError) as exc:
        raise EphemeralActionRejected("invalid_context") from exc
    if principal_type not in {"subscriber", "system_user", "reseller_user"} or (
        len(email_digest) != 64
        or any(char not in "0123456789abcdef" for char in email_digest)
    ):
        raise EphemeralActionRejected("invalid_context")
    try:
        next_login_path = _safe_next_login_path(next_login_path)
    except CredentialRecoveryError as exc:
        raise EphemeralActionRejected("invalid_context") from exc
    if (
        notification.audience_type != principal_type
        or notification.audience_id != principal_id
        or _email_digest(notification.recipient) != email_digest
    ):
        raise EphemeralActionRejected("recipient_context_mismatch")

    capability = issue_exact_reset_capability(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
    )
    if capability is None or _email_digest(capability.email) != email_digest:
        raise EphemeralActionRejected("stale_account_context")
    rendered = email_service.render_password_reset_email(
        db,
        to_email=capability.email,
        reset_token=capability.token,
        person_name=capability.person_name,
        next_login_path=next_login_path,
        expires_minutes=capability.ttl_minutes,
        token_in_fragment=True,
    )
    return EphemeralEmailContent(
        subject=rendered.subject,
        body_html=rendered.body_html,
        body_text=rendered.body_text,
        activity="auth_password_reset",
    )
