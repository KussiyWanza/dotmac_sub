"""Atomic owner for reseller records and reseller portal identity onboarding.

Public onboarding writes enter one manifest-verified coordinator transaction.
The coordinator composes canonical reseller/subscriber initialization, the
subscriber-assignment owner, audit evidence, and versioned events. Invitation
delivery is a durable event consequence; capabilities are minted only when the
notification worker is ready to send them.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit import AuditActorType
from app.models.auth import AuthProvider, UserCredential
from app.models.notification import Notification
from app.models.rbac import Role
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.schemas.subscriber import ResellerCreate, SubscriberCreate
from app.services import auth_cache, credential_recovery, subscriber_assignments
from app.services import auth_flow as auth_flow_service
from app.services import subscriber as subscriber_service
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

RESELLER_WRITE_SCOPE = "reseller:write"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner="auth.reseller_onboarding",
    concern="reseller portal principal onboarding",
    name="create_reseller",
)
_PROVISION_COMMAND = OwnerCommandDefinition(
    owner="auth.reseller_onboarding",
    concern="reseller portal principal onboarding",
    name="provision_reseller_user",
)
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


class ResellerOnboardingError(DomainError):
    """Stable, transport-neutral reseller onboarding failure."""


@dataclass(frozen=True)
class ResellerPortalUserSpec:
    first_name: str
    last_name: str
    email: str
    username: str | None = None
    password: str | None = None
    role_name: str | None = None
    send_invite: bool = True


@dataclass(frozen=True)
class CreateResellerCommand:
    context: CommandContext
    reseller: ResellerCreate
    portal_user: ResellerPortalUserSpec | None = None
    assignment_context: CommandContext | None = None


@dataclass(frozen=True)
class ProvisionResellerUserCommand:
    context: CommandContext
    reseller_id: UUID
    portal_user: ResellerPortalUserSpec
    assignment_context: CommandContext | None = None


@dataclass(frozen=True)
class ResellerOnboardingOutcome:
    reseller_id: UUID
    principal_type: str | None
    principal_id: UUID | None
    subscriber_id: UUID | None
    reseller_user_id: UUID | None
    role_names: tuple[str, ...]
    invite_requested: bool
    command_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class _NormalizedUser:
    first_name: str
    last_name: str
    email: str
    username: str
    password: str | None
    role_name: str | None
    send_invite: bool


def _error(code: str, message: str, **details: object) -> ResellerOnboardingError:
    return ResellerOnboardingError(
        code=f"auth.reseller_onboarding.{code}",
        message=message,
        details=details,
    )


def _validate_context(context: CommandContext) -> tuple[AuditActorType, str]:
    if context.scope != RESELLER_WRITE_SCOPE:
        raise _error(
            "invalid_command",
            "Reseller onboarding requires reseller write authorization.",
            field="scope",
        )
    actor_type_value, separator, actor_id = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
    except ValueError as exc:
        raise _error(
            "invalid_command",
            "Reseller onboarding actor type is not supported.",
            field="actor",
        ) from exc
    if not separator or not actor_id.strip():
        raise _error(
            "invalid_command",
            "Reseller onboarding actor identity is incomplete.",
            field="actor",
        )
    return actor_type, actor_id.strip()


def _normalize_user(spec: ResellerPortalUserSpec) -> _NormalizedUser:
    first_name = spec.first_name.strip()
    last_name = spec.last_name.strip()
    try:
        email = str(_EMAIL_ADAPTER.validate_python(spec.email.strip())).lower()
    except ValidationError as exc:
        raise _error(
            "invalid_command",
            "Reseller portal user email is invalid.",
            field="email",
        ) from exc
    username = (spec.username or email).strip().lower()
    role_name = (spec.role_name or "").strip() or None
    if not first_name or not last_name or not username:
        raise _error(
            "invalid_command",
            "Reseller portal user identity fields cannot be empty.",
        )
    if (
        len(first_name) > 80
        or len(last_name) > 80
        or len(username) > 150
        or (role_name is not None and len(role_name) > 80)
    ):
        raise _error(
            "invalid_command",
            "Reseller portal user identity exceeds the canonical record limit.",
        )
    return _NormalizedUser(
        first_name=first_name,
        last_name=last_name,
        email=email,
        username=username,
        password=spec.password,
        role_name=role_name,
        send_invite=spec.send_invite,
    )


def _validate_assignment_context(
    owner_context: CommandContext,
    assignment_context: CommandContext | None,
) -> CommandContext:
    if assignment_context is None:
        raise _error(
            "assignment_authorization_required",
            "Reseller role assignment requires RBAC assignment authorization.",
        )
    if (
        assignment_context.scope != subscriber_assignments.ASSIGNMENT_SCOPE
        or assignment_context.actor != owner_context.actor
        or assignment_context.correlation_id != owner_context.correlation_id
    ):
        raise _error(
            "assignment_authorization_required",
            "RBAC assignment evidence does not match the onboarding command.",
        )
    return assignment_context


def _acquire_identity_lock(db: Session, email: str, username: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    for value in sorted({f"email:{email}", f"username:{username}"}):
        lock_key = int.from_bytes(
            hashlib.sha256(f"reseller-onboarding:{value}".encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


def _ensure_identity_available(
    db: Session,
    *,
    email: str,
    username: str,
) -> None:
    if (
        db.execute(
            select(UserCredential.id).where(
                UserCredential.provider == AuthProvider.local,
                func.lower(UserCredential.username) == username,
            )
        ).scalar_one_or_none()
        is not None
    ):
        raise _error(
            "identity_conflict",
            "Username is already assigned to another local principal.",
        )
    if (
        db.execute(
            select(Subscriber.id).where(func.lower(Subscriber.email) == email)
        ).first()
        is not None
        or db.execute(
            select(ResellerUser.id).where(func.lower(ResellerUser.email) == email)
        ).first()
        is not None
    ):
        raise _error(
            "identity_conflict",
            "Email is already assigned to another portal principal.",
        )


def _locked_reseller(db: Session, reseller_id: UUID) -> Reseller:
    reseller = db.execute(
        select(Reseller).where(Reseller.id == reseller_id).with_for_update()
    ).scalar_one_or_none()
    if reseller is None:
        raise _error(
            "reseller_not_found",
            "Reseller was not found.",
            reseller_id=str(reseller_id),
        )
    if not reseller.is_active:
        raise _error(
            "inactive_reseller",
            "An inactive reseller cannot receive a new portal principal.",
            reseller_id=str(reseller_id),
        )
    return reseller


def _role_by_name(db: Session, role_name: str) -> Role:
    role = db.execute(
        select(Role).where(Role.name == role_name).with_for_update()
    ).scalar_one_or_none()
    if role is None or not role.is_active:
        raise _error(
            "role_not_found",
            "Selected reseller role is not active.",
            role_name=role_name,
        )
    return role


def _create_credential(
    db: Session,
    *,
    username: str,
    password: str | None,
    subscriber_id: UUID | None = None,
    reseller_user_id: UUID | None = None,
) -> None:
    placeholder = password or secrets.token_urlsafe(32)
    db.add(
        UserCredential(
            subscriber_id=subscriber_id,
            reseller_user_id=reseller_user_id,
            provider=AuthProvider.local,
            username=username,
            password_hash=auth_flow_service.hash_password(placeholder),
            must_change_password=True,
            password_updated_at=datetime.now(UTC),
            is_active=True,
        )
    )


def _create_portal_principal(
    db: Session,
    *,
    reseller: Reseller,
    user: _NormalizedUser,
    owner_context: CommandContext,
    assignment_context: CommandContext | None,
) -> tuple[str, UUID, UUID | None, UUID | None, tuple[str, ...]]:
    _acquire_identity_lock(db, user.email, user.username)
    _ensure_identity_available(db, email=user.email, username=user.username)

    if settings.reseller_user_principal_enabled:
        if user.role_name is not None:
            raise _error(
                "unsupported_role_target",
                "Subscriber roles cannot be assigned to a first-class reseller user.",
                role_name=user.role_name,
            )
        reseller_user = ResellerUser(
            reseller_id=reseller.id,
            email=user.email,
            full_name=f"{user.first_name} {user.last_name}".strip(),
            is_active=True,
        )
        db.add(reseller_user)
        db.flush()
        _create_credential(
            db,
            username=user.username,
            password=user.password,
            reseller_user_id=reseller_user.id,
        )
        return "reseller_user", reseller_user.id, None, reseller_user.id, ()

    subscriber = subscriber_service.subscribers.prepare_new_account(
        db,
        SubscriberCreate(
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
            reseller_id=reseller.id,
            is_active=True,
        ),
    )
    subscriber.user_type = UserType.reseller
    _create_credential(
        db,
        username=user.username,
        password=user.password,
        subscriber_id=subscriber.id,
    )
    link = ResellerUser(
        reseller_id=reseller.id,
        subscriber_id=subscriber.id,
        is_active=True,
    )
    db.add(link)
    db.flush()
    role_names: tuple[str, ...] = ()
    if user.role_name is not None:
        role = _role_by_name(db, user.role_name)
        evidence = _validate_assignment_context(owner_context, assignment_context)
        subscriber_assignments.ensure_role_grant_in_transaction(
            db,
            context=evidence,
            subscriber_id=subscriber.id,
            role_id=role.id,
        )
        role_names = (role.name,)
    emit_event(
        db,
        EventType.subscriber_created,
        {
            "subscriber_id": str(subscriber.id),
            "subscriber_number": subscriber.subscriber_number,
        },
        actor=owner_context.actor,
        subscriber_id=subscriber.id,
    )
    return "subscriber", subscriber.id, subscriber.id, link.id, role_names


def _command_metadata(context: CommandContext) -> dict[str, object]:
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


def _stage_onboarding_evidence(
    db: Session,
    *,
    reseller: Reseller,
    user: _NormalizedUser | None,
    principal_type: str | None,
    principal_id: UUID | None,
    role_names: tuple[str, ...],
    context: CommandContext,
    actor_type: AuditActorType,
    actor_id: str,
    reseller_created: bool,
) -> None:
    invite_requested = bool(user is not None and user.send_invite)
    metadata = {
        **_command_metadata(context),
        "reseller_created": reseller_created,
        "principal_type": principal_type,
        "principal_id": str(principal_id) if principal_id else None,
        "role_names": list(role_names),
        "invite_requested": invite_requested,
    }
    stage_audit_event(
        db,
        action=(
            "auth.reseller_onboarded"
            if reseller_created
            else "auth.reseller_user_provisioned"
        ),
        entity_type="reseller",
        entity_id=str(reseller.id),
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=str(context.correlation_id),
        status_code=201,
        metadata=metadata,
    )
    if reseller_created:
        emit_event(
            db,
            EventType.reseller_created,
            {
                **_command_metadata(context),
                "aggregate_type": "reseller",
                "aggregate_id": str(reseller.id),
                "aggregate_version": str(context.command_id),
                "reseller_id": str(reseller.id),
            },
            actor=context.actor,
        )
    if user is not None and principal_type is not None and principal_id is not None:
        emit_event(
            db,
            EventType.reseller_user_provisioned,
            {
                **_command_metadata(context),
                "aggregate_type": principal_type,
                "aggregate_id": str(principal_id),
                "aggregate_version": str(context.command_id),
                "reseller_id": str(reseller.id),
                "principal_type": principal_type,
                "principal_id": str(principal_id),
                "email_sha256": hashlib.sha256(user.email.encode()).hexdigest(),
                "role_names": list(role_names),
                "invite_requested": invite_requested,
            },
            actor=context.actor,
        )


def _invalidate_after_commit(
    db: Session,
    principal_type: str | None,
    principal_id: UUID | None,
) -> None:
    if principal_type is None or principal_id is None:
        return

    def invalidate(_callback_db: Session) -> None:
        auth_cache.invalidate_principal(principal_type, str(principal_id))

    run_after_commit(db, invalidate)


def _outcome(
    *,
    reseller: Reseller,
    principal_type: str | None,
    principal_id: UUID | None,
    subscriber_id: UUID | None,
    reseller_user_id: UUID | None,
    role_names: tuple[str, ...],
    invite_requested: bool,
    context: CommandContext,
) -> ResellerOnboardingOutcome:
    return ResellerOnboardingOutcome(
        reseller_id=reseller.id,
        principal_type=principal_type,
        principal_id=principal_id,
        subscriber_id=subscriber_id,
        reseller_user_id=reseller_user_id,
        role_names=role_names,
        invite_requested=invite_requested,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def create_reseller(
    db: Session,
    command: CreateResellerCommand,
) -> ResellerOnboardingOutcome:
    """Create a reseller and optional portal principal in one transaction."""

    def operation() -> ResellerOnboardingOutcome:
        actor_type, actor_id = _validate_context(command.context)
        reseller = subscriber_service.resellers.prepare_new(db, command.reseller)
        normalized_user = (
            _normalize_user(command.portal_user) if command.portal_user else None
        )
        if normalized_user is not None and not reseller.is_active:
            raise _error(
                "inactive_reseller",
                "An inactive reseller cannot receive a portal principal.",
                reseller_id=str(reseller.id),
            )
        principal_type: str | None = None
        principal_id: UUID | None = None
        subscriber_id: UUID | None = None
        reseller_user_id: UUID | None = None
        role_names: tuple[str, ...] = ()
        if normalized_user is not None:
            (
                principal_type,
                principal_id,
                subscriber_id,
                reseller_user_id,
                role_names,
            ) = _create_portal_principal(
                db,
                reseller=reseller,
                user=normalized_user,
                owner_context=command.context,
                assignment_context=command.assignment_context,
            )
        db.flush()
        _stage_onboarding_evidence(
            db,
            reseller=reseller,
            user=normalized_user,
            principal_type=principal_type,
            principal_id=principal_id,
            role_names=role_names,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            reseller_created=True,
        )
        _invalidate_after_commit(db, principal_type, principal_id)
        return _outcome(
            reseller=reseller,
            principal_type=principal_type,
            principal_id=principal_id,
            subscriber_id=subscriber_id,
            reseller_user_id=reseller_user_id,
            role_names=role_names,
            invite_requested=bool(normalized_user and normalized_user.send_invite),
            context=command.context,
        )

    try:
        return execute_owner_command(
            db,
            definition=_CREATE_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "Reseller onboarding conflicts with an existing canonical record.",
        ) from exc


def provision_reseller_user(
    db: Session,
    command: ProvisionResellerUserCommand,
) -> ResellerOnboardingOutcome:
    """Provision one portal principal for an existing active reseller."""

    def operation() -> ResellerOnboardingOutcome:
        actor_type, actor_id = _validate_context(command.context)
        reseller = _locked_reseller(db, command.reseller_id)
        user = _normalize_user(command.portal_user)
        (
            principal_type,
            principal_id,
            subscriber_id,
            reseller_user_id,
            role_names,
        ) = _create_portal_principal(
            db,
            reseller=reseller,
            user=user,
            owner_context=command.context,
            assignment_context=command.assignment_context,
        )
        db.flush()
        _stage_onboarding_evidence(
            db,
            reseller=reseller,
            user=user,
            principal_type=principal_type,
            principal_id=principal_id,
            role_names=role_names,
            context=command.context,
            actor_type=actor_type,
            actor_id=actor_id,
            reseller_created=False,
        )
        _invalidate_after_commit(db, principal_type, principal_id)
        return _outcome(
            reseller=reseller,
            principal_type=principal_type,
            principal_id=principal_id,
            subscriber_id=subscriber_id,
            reseller_user_id=reseller_user_id,
            role_names=role_names,
            invite_requested=user.send_invite,
            context=command.context,
        )

    try:
        return execute_owner_command(
            db,
            definition=_PROVISION_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "Reseller onboarding conflicts with an existing canonical record.",
        ) from exc


def materialize_reseller_invite_email(
    db: Session,
    *,
    notification: Notification,
    context: dict[str, object],
):
    """Mint and render an exact reseller reset capability at delivery time."""

    from app.services import email as email_service
    from app.services.ephemeral_communication_actions import (
        EphemeralActionRejected,
        EphemeralEmailContent,
    )

    if not isinstance(notification, Notification):
        raise EphemeralActionRejected("invalid_notification")
    if set(context) != {
        "reseller_id",
        "principal_type",
        "principal_id",
        "email_sha256",
    }:
        raise EphemeralActionRejected("invalid_context")
    try:
        reseller_id = UUID(str(context["reseller_id"]))
        principal_type = str(context["principal_type"])
        principal_id = UUID(str(context["principal_id"]))
        email_digest = str(context["email_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EphemeralActionRejected("invalid_context") from exc
    if principal_type not in {"subscriber", "reseller_user"} or (
        len(email_digest) != 64
        or any(char not in "0123456789abcdef" for char in email_digest)
    ):
        raise EphemeralActionRejected("invalid_context")
    recipient_digest = hashlib.sha256(
        notification.recipient.strip().lower().encode()
    ).hexdigest()
    if (
        notification.audience_type != principal_type
        or notification.audience_id != principal_id
        or recipient_digest != email_digest
    ):
        raise EphemeralActionRejected("recipient_context_mismatch")

    if principal_type == "subscriber":
        subscriber = db.get(Subscriber, principal_id)
        valid = bool(
            subscriber
            and subscriber.is_active
            and subscriber.user_type == UserType.reseller
            and subscriber.reseller_id == reseller_id
        )
        email = subscriber.email if subscriber else ""
        name = subscriber.display_name or subscriber.first_name if subscriber else None
    else:
        reseller_user = db.get(ResellerUser, principal_id)
        valid = bool(
            reseller_user
            and reseller_user.is_active
            and reseller_user.reseller_id == reseller_id
            and reseller_user.subscriber_id is None
        )
        email = reseller_user.email or "" if reseller_user else ""
        name = reseller_user.full_name if reseller_user else None
    if (
        not valid
        or not email
        or hashlib.sha256(email.strip().lower().encode()).hexdigest() != email_digest
    ):
        raise EphemeralActionRejected("stale_account_context")

    reset = credential_recovery.issue_exact_reset_capability(
        db,
        principal_type=principal_type,
        principal_id=principal_id,
    )
    if (
        reset is None
        or reset.principal_type != principal_type
        or reset.principal_id != principal_id
        or not reset.token
    ):
        raise EphemeralActionRejected("stale_account_context")
    rendered = email_service.render_user_invite_email(
        db,
        to_email=email,
        reset_token=reset.token,
        person_name=name,
        next_login_path="/reseller/auth/login?next=/reseller/dashboard",
        expires_minutes=reset.ttl_minutes,
        token_in_fragment=True,
    )
    return EphemeralEmailContent(
        subject=rendered.subject,
        body_html=rendered.body_html,
        body_text=rendered.body_text,
        activity="auth_reseller_invite",
    )
    from app.services import access_invitations

    access_invitations.record_issued(
        db,
        principal_type=reset.principal_type,
        principal_id=reset.principal_id,
        purpose="reseller_invite",
        email=reset.email,
        ttl_minutes=reset.ttl_minutes,
        source="reseller_onboarding",
    )
