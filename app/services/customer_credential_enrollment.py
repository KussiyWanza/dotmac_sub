"""Passwordless invitation into customer-owned local credential enrollment.

Referral signup creates an account but no placeholder credential. This owner
queues a non-secret action, mints its purpose-bound capability only at delivery,
and creates the local credential only after the recipient supplies a password.
Completing enrollment verifies the account email; it does not activate, merge,
or verify the account's Party identity.
"""

from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from jose import JWTError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import AuthProvider, UserCredential
from app.models.domain_settings import SettingDomain
from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
)
from app.models.referral_native import Referral
from app.models.sales import Lead
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import auth_flow as auth_flow_service
from app.services import context_signing
from app.services import email as email_service
from app.services.audit_adapter import stage_audit_event
from app.services.capability_recipient import (
    CapabilityRecipientError,
    resolve_capability_recipient,
)
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
)
from app.services.communication_intents import (
    submit as submit_communication_intent,
)
from app.services.domain_errors import DomainError
from app.services.ephemeral_communication_actions import (
    EPHEMERAL_ACTION_METADATA_KEY,
    REFERRAL_CREDENTIAL_ENROLLMENT_ACTION,
    EphemeralActionRejected,
    EphemeralEmailContent,
)
from app.services.ephemeral_communication_actions import (
    descriptor as ephemeral_action_descriptor,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.rate_limiter_adapter import allow_operation
from app.services.settings_spec import resolve_value

EnrollmentDeliveryStatus = Literal[
    "queued",
    "rate_limited",
    "suppressed",
    "already_enrolled",
    "manual_review_required",
]

_CAPABILITY_PURPOSE = "referral_credential_enrollment"
_CAPABILITY_ISSUER = "dotmac_sub.auth.customer_credential_enrollment"
_TOKEN_VERSION = 1
_TOKEN_CLOCK_SKEW = timedelta(minutes=5)
_REQUEST_STATUS_METADATA_KEY = "credential_enrollment_request_status"

CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE = "auth:customer_credential_enrollment"

_REQUEST_COMMAND = OwnerCommandDefinition(
    owner="auth.customer_credential_enrollment",
    concern="credential enrollment delivery request",
    name="request_referral_enrollment",
)
_COMPLETE_COMMAND = OwnerCommandDefinition(
    owner="auth.customer_credential_enrollment",
    concern="referral-created customer local credential enrollment",
    name="complete_referral_enrollment",
)


class CustomerCredentialEnrollmentError(DomainError):
    """Stable, transport-neutral credential-enrollment failure."""


def _error(
    code: str, message: str, **details: object
) -> CustomerCredentialEnrollmentError:
    return CustomerCredentialEnrollmentError(
        code=f"auth.customer_credential_enrollment.{code}",
        message=message,
        details=details,
    )


@dataclass(frozen=True)
class RequestReferralEnrollmentCommand:
    """Request one referral-created account's credential capability delivery."""

    context: CommandContext
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_id: UUID


@dataclass(frozen=True)
class CompleteReferralEnrollmentCommand:
    """Redeem one purpose-bound capability into a local customer credential."""

    context: CommandContext
    token: str
    new_password: str
    username: str | None = None


@dataclass(frozen=True)
class EnrollmentContext:
    referral_id: UUID
    referred_party_id: UUID
    referred_lead_id: UUID
    subscriber_id: UUID
    email_digest: str


@dataclass(frozen=True)
class EnrollmentDeliveryResult:
    subscriber_id: UUID
    status: EnrollmentDeliveryStatus
    retry_after_seconds: int | None = None
    command_id: UUID | None = None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class EnrollmentCompletionResult:
    subscriber_id: UUID
    username: str
    email_verified: bool
    enrolled_at: datetime
    command_id: UUID | None = None
    correlation_id: UUID | None = None


def _email_digest(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_command_context(context: CommandContext) -> None:
    if context.scope != CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE:
        raise _error(
            "invalid_command",
            "Credential enrollment command scope is invalid.",
            field="scope",
        )


def _policy_integer(db: Session, key: str) -> int:
    value = resolve_value(db, SettingDomain.auth, key)
    try:
        resolved = int(str(value))
    except (TypeError, ValueError) as exc:
        raise _error(
            "invalid_configuration",
            "Credential enrollment policy configuration is invalid.",
            setting=f"auth.{key}",
        ) from exc
    if resolved <= 0:
        raise _error(
            "invalid_configuration",
            "Credential enrollment policy configuration is invalid.",
            setting=f"auth.{key}",
        )
    return resolved


def _token_ttl_minutes(db: Session) -> int:
    return _policy_integer(db, "user_invite_expiry_minutes")


def _request_rate_policy(db: Session) -> tuple[int, int]:
    return (
        _policy_integer(db, "credential_enrollment_request_limit"),
        _policy_integer(db, "credential_enrollment_request_window_seconds"),
    )


def _canonical_context(
    db: Session,
    *,
    referral_id: UUID,
    referred_party_id: UUID,
    referred_lead_id: UUID,
    subscriber_id: UUID,
    lock: bool,
) -> tuple[Referral, Subscriber, EnrollmentContext]:
    referral_stmt = select(Referral).where(Referral.id == referral_id)
    subscriber_stmt = select(Subscriber).where(Subscriber.id == subscriber_id)
    lead_stmt = select(Lead).where(Lead.id == referred_lead_id)
    if lock:
        referral_stmt = referral_stmt.with_for_update()
        subscriber_stmt = subscriber_stmt.with_for_update()
        lead_stmt = lead_stmt.with_for_update()
    referral = db.scalars(referral_stmt).one_or_none()
    subscriber = db.scalars(subscriber_stmt).one_or_none()
    lead = db.scalars(lead_stmt).one_or_none()
    if referral is None or not referral.is_active or subscriber is None or lead is None:
        raise _error(
            "context_not_found",
            "Credential enrollment context was not found.",
        )
    if (
        referral.referred_party_id != referred_party_id
        or referral.referred_lead_id != referred_lead_id
        or referral.referred_subscriber_id != subscriber_id
        or subscriber.party_id != referred_party_id
        or lead.party_id != referred_party_id
        or lead.subscriber_id != subscriber_id
    ):
        raise _error(
            "stale_context",
            "Credential enrollment context is stale or does not match.",
        )
    if not subscriber.is_active or subscriber.status in {
        SubscriberStatus.canceled,
        SubscriberStatus.disabled,
    }:
        raise _error(
            "inactive_account",
            "Inactive, disabled, or canceled accounts cannot enroll a credential.",
        )
    context = EnrollmentContext(
        referral_id=referral.id,
        referred_party_id=referred_party_id,
        referred_lead_id=referred_lead_id,
        subscriber_id=subscriber.id,
        email_digest=_email_digest(subscriber.email),
    )
    return referral, subscriber, context


def _local_credential(db: Session, subscriber_id: UUID) -> UserCredential | None:
    return db.scalars(
        select(UserCredential)
        .where(UserCredential.subscriber_id == subscriber_id)
        .where(UserCredential.provider == AuthProvider.local)
        .order_by(UserCredential.created_at.desc())
    ).first()


def _request_dedupe_key(referral_id: UUID) -> str:
    return f"auth:referral-credential-enrollment:{referral_id}"


def _existing_request_status(
    db: Session, *, referral_id: UUID
) -> EnrollmentDeliveryStatus | None:
    record = db.scalars(
        select(CommunicationIntentRecord).where(
            CommunicationIntentRecord.dedupe_key == _request_dedupe_key(referral_id)
        )
    ).one_or_none()
    if record is None:
        return None
    status = record.metadata_.get(_REQUEST_STATUS_METADATA_KEY)
    if status in {"queued", "suppressed"}:
        return status
    return "suppressed" if record.suppression_reasons else "queued"


def _stage_request_outcome(
    db: Session,
    *,
    command: RequestReferralEnrollmentCommand,
    delivery_status: EnrollmentDeliveryStatus,
    retry_after_seconds: int | None = None,
) -> None:
    evidence = {
        "schema_version": 1,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "causation_id": (
            str(command.context.causation_id)
            if command.context.causation_id is not None
            else None
        ),
        "reason": command.context.reason,
        "delivery_status": delivery_status,
        "retry_after_seconds": retry_after_seconds,
        "referral_id": str(command.referral_id),
    }
    stage_audit_event(
        db,
        action="auth.customer_credential_enrollment_requested",
        entity_type="subscriber",
        entity_id=str(command.subscriber_id),
        actor_type=AuditActorType.system,
        actor_id=command.context.actor,
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.customer_credential_enrollment_requested,
        {
            **evidence,
            "aggregate_type": "subscriber",
            "aggregate_id": str(command.subscriber_id),
            "aggregate_version": str(command.context.command_id),
            "subscriber_id": str(command.subscriber_id),
            "referred_party_id": str(command.referred_party_id),
            "referred_lead_id": str(command.referred_lead_id),
        },
        actor=command.context.actor,
        subscriber_id=command.subscriber_id,
    )


def _issue_token(
    db: Session,
    context: EnrollmentContext,
    *,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    expires_at = issued_at + timedelta(minutes=_token_ttl_minutes(db))
    token = context_signing.sign_context_token(
        db,
        {
            "typ": _CAPABILITY_PURPOSE,
            "iss": _CAPABILITY_ISSUER,
            "ver": _TOKEN_VERSION,
            "sub": str(context.subscriber_id),
            "referral_id": str(context.referral_id),
            "referred_party_id": str(context.referred_party_id),
            "referred_lead_id": str(context.referred_lead_id),
            "subscriber_id": str(context.subscriber_id),
            "email_sha256": context.email_digest,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        },
    )
    return token, expires_at


def _decode_token(
    db: Session,
    token: str,
    *,
    now: datetime | None = None,
) -> EnrollmentContext:
    normalized_token = str(token or "").strip()
    if not normalized_token or len(normalized_token) > 4096:
        raise _error(
            "invalid_capability",
            "Invalid or expired credential enrollment capability.",
        )
    try:
        payload = context_signing.verify_context_token(db, normalized_token)
    except (JWTError, TypeError, ValueError) as exc:
        raise _error(
            "invalid_capability",
            "Invalid or expired credential enrollment capability.",
        ) from exc
    if (
        payload.get("typ") != _CAPABILITY_PURPOSE
        or payload.get("iss") != _CAPABILITY_ISSUER
        or payload.get("ver") != _TOKEN_VERSION
    ):
        raise _error(
            "invalid_capability",
            "Invalid or expired credential enrollment capability.",
        )
    try:
        context = EnrollmentContext(
            referral_id=UUID(str(payload["referral_id"])),
            referred_party_id=UUID(str(payload["referred_party_id"])),
            referred_lead_id=UUID(str(payload["referred_lead_id"])),
            subscriber_id=UUID(str(payload["subscriber_id"])),
            email_digest=str(payload["email_sha256"]),
        )
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise _error(
            "invalid_capability",
            "Invalid or expired credential enrollment capability.",
        ) from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if (
        str(payload.get("sub") or "") != str(context.subscriber_id)
        or len(context.email_digest) != 64
        or context.email_digest != context.email_digest.lower()
        or any(char not in string.hexdigits.lower() for char in context.email_digest)
        or issued_at > current + _TOKEN_CLOCK_SKEW
        or expires_at <= current
        or expires_at <= issued_at
        or expires_at - issued_at
        > timedelta(minutes=_token_ttl_minutes(db)) + _TOKEN_CLOCK_SKEW
    ):
        raise _error(
            "invalid_capability",
            "Invalid or expired credential enrollment capability.",
        )
    return context


def request_referral_enrollment(
    db: Session,
    command: RequestReferralEnrollmentCommand,
) -> EnrollmentDeliveryResult:
    """Queue capability delivery without creating a token or placeholder password."""

    def operation() -> EnrollmentDeliveryResult:
        _validate_command_context(command.context)
        _, subscriber, enrollment_context = _canonical_context(
            db,
            referral_id=command.referral_id,
            referred_party_id=command.referred_party_id,
            referred_lead_id=command.referred_lead_id,
            subscriber_id=command.subscriber_id,
            lock=True,
        )
        credential = _local_credential(db, subscriber.id)
        if credential is not None:
            state: EnrollmentDeliveryStatus = (
                "already_enrolled" if credential.is_active else "manual_review_required"
            )
            _stage_request_outcome(db, command=command, delivery_status=state)
            return EnrollmentDeliveryResult(
                subscriber_id=subscriber.id,
                status=state,
                command_id=command.context.command_id,
                correlation_id=command.context.correlation_id,
            )

        existing_status = _existing_request_status(db, referral_id=command.referral_id)
        if existing_status is not None:
            _stage_request_outcome(
                db,
                command=command,
                delivery_status=existing_status,
            )
            return EnrollmentDeliveryResult(
                subscriber_id=subscriber.id,
                status=existing_status,
                command_id=command.context.command_id,
                correlation_id=command.context.correlation_id,
            )

        request_limit, request_window_seconds = _request_rate_policy(db)
        decision = allow_operation(
            f"auth:referral-enrollment:{subscriber.id}",
            limit=request_limit,
            window_seconds=request_window_seconds,
        )
        if not decision.allowed:
            _stage_request_outcome(
                db,
                command=command,
                delivery_status="rate_limited",
                retry_after_seconds=decision.retry_after_seconds,
            )
            return EnrollmentDeliveryResult(
                subscriber_id=subscriber.id,
                status="rate_limited",
                retry_after_seconds=decision.retry_after_seconds,
                command_id=command.context.command_id,
                correlation_id=command.context.correlation_id,
            )

        action_context: dict[str, object] = {
            "referral_id": str(enrollment_context.referral_id),
            "referred_party_id": str(enrollment_context.referred_party_id),
            "referred_lead_id": str(enrollment_context.referred_lead_id),
            "subscriber_id": str(enrollment_context.subscriber_id),
            "email_sha256": enrollment_context.email_digest,
        }
        intent_result = submit_communication_intent(
            db,
            CommunicationIntent(
                subscriber_id=subscriber.id,
                event_type=REFERRAL_CREDENTIAL_ENROLLMENT_ACTION,
                category="credentials",
                subject=None,
                body=None,
                communication_class=CommunicationClass.transactional,
                channels=(NotificationChannel.email,),
                include_reseller=False,
                persist_policy_suppressions=True,
                recipients={NotificationChannel.email: subscriber.email},
                metadata={
                    EPHEMERAL_ACTION_METADATA_KEY: ephemeral_action_descriptor(
                        action_type=REFERRAL_CREDENTIAL_ENROLLMENT_ACTION,
                        version=1,
                        context=action_context,
                    ),
                    "command_id": str(command.context.command_id),
                    "correlation_id": str(command.context.correlation_id),
                },
                dedupe_key=_request_dedupe_key(command.referral_id),
            ),
        )
        delivery_status: EnrollmentDeliveryStatus = (
            "queued" if intent_result.queued else "suppressed"
        )
        intent_record = db.get(CommunicationIntentRecord, intent_result.intent_id)
        if intent_record is not None:
            intent_record.metadata_ = {
                **intent_record.metadata_,
                _REQUEST_STATUS_METADATA_KEY: delivery_status,
            }
        _stage_request_outcome(
            db,
            command=command,
            delivery_status=delivery_status,
        )
        return EnrollmentDeliveryResult(
            subscriber_id=subscriber.id,
            status=delivery_status,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_REQUEST_COMMAND,
        context=command.context,
        operation=operation,
    )


def _context_from_delivery_metadata(context: dict[str, object]) -> EnrollmentContext:
    if set(context) != {
        "referral_id",
        "referred_party_id",
        "referred_lead_id",
        "subscriber_id",
        "email_sha256",
    }:
        raise EphemeralActionRejected("invalid_context")
    try:
        resolved = EnrollmentContext(
            referral_id=UUID(str(context["referral_id"])),
            referred_party_id=UUID(str(context["referred_party_id"])),
            referred_lead_id=UUID(str(context["referred_lead_id"])),
            subscriber_id=UUID(str(context["subscriber_id"])),
            email_digest=str(context["email_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EphemeralActionRejected("invalid_context") from exc
    if (
        len(resolved.email_digest) != 64
        or resolved.email_digest != resolved.email_digest.lower()
        or any(char not in string.hexdigits.lower() for char in resolved.email_digest)
    ):
        raise EphemeralActionRejected("invalid_context")
    return resolved


def materialize_enrollment_email(
    db: Session,
    *,
    notification: Notification,
    context: dict[str, object],
) -> EphemeralEmailContent:
    """Mint and render the enrollment capability at the transport boundary."""

    requested = _context_from_delivery_metadata(context)
    if (
        notification.subscriber_id != requested.subscriber_id
        or _email_digest(notification.recipient) != requested.email_digest
    ):
        raise EphemeralActionRejected("recipient_context_mismatch")
    try:
        _, subscriber, canonical = _canonical_context(
            db,
            referral_id=requested.referral_id,
            referred_party_id=requested.referred_party_id,
            referred_lead_id=requested.referred_lead_id,
            subscriber_id=requested.subscriber_id,
            lock=False,
        )
    except CustomerCredentialEnrollmentError as exc:
        raise EphemeralActionRejected("stale_account_context") from exc
    if canonical != requested:
        raise EphemeralActionRejected("stale_account_context")
    if _local_credential(db, subscriber.id) is not None:
        raise EphemeralActionRejected("already_enrolled")

    # Resolve the one authorised address before minting the token. Enrollment
    # grants portal access, so it must reach a single authorised mailbox.
    try:
        recipient = resolve_capability_recipient(
            subscriber.email, subject=f"subscriber:{subscriber.id}"
        )
    except CapabilityRecipientError as exc:
        raise EphemeralActionRejected("unresolved_recipient") from exc

    token, _ = _issue_token(db, canonical)
    rendered = email_service.render_user_invite_email(
        db,
        to_email=recipient,
        reset_token=token,
        person_name=subscriber.display_name or subscriber.first_name,
        expires_minutes=_token_ttl_minutes(db),
        action_path="/portal/auth/credential-enrollment",
        # Fragments stay out of normal HTTP request and access logs. The
        # browser moves the capability into a CSRF-protected POST body.
        token_in_fragment=True,
    )
    return EphemeralEmailContent(
        subject=rendered.subject,
        body_html=rendered.body_html,
        body_text=rendered.body_text,
        activity="auth_user_invite",
    )


def complete_referral_enrollment(
    db: Session,
    command: CompleteReferralEnrollmentCommand,
) -> EnrollmentCompletionResult:
    """Create the user-chosen local credential and verify account email once."""

    def operation() -> EnrollmentCompletionResult:
        _validate_command_context(command.context)
        context = _decode_token(db, command.token)
        minimum = _policy_integer(db, "password_min_length")
        if len(command.new_password) < minimum or len(command.new_password) > 255:
            raise _error(
                "invalid_password",
                f"Password must be between {minimum} and 255 characters.",
                minimum_length=minimum,
                maximum_length=255,
            )
        _, subscriber, canonical = _canonical_context(
            db,
            referral_id=context.referral_id,
            referred_party_id=context.referred_party_id,
            referred_lead_id=context.referred_lead_id,
            subscriber_id=context.subscriber_id,
            lock=True,
        )
        if canonical.email_digest != context.email_digest:
            raise _error(
                "invalid_capability",
                "Invalid or expired credential enrollment capability.",
            )
        if _local_credential(db, subscriber.id) is not None:
            raise _error(
                "invalid_capability",
                "Invalid or expired credential enrollment capability.",
            )
        normalized_username = str(command.username or subscriber.email).strip().lower()
        if (
            not normalized_username
            or len(normalized_username) > 150
            or any(char.isspace() for char in normalized_username)
        ):
            raise _error(
                "invalid_username",
                "A whitespace-free username of at most 150 characters is required.",
            )
        collision = db.scalars(
            select(UserCredential.id)
            .where(UserCredential.provider == AuthProvider.local)
            .where(func.lower(UserCredential.username) == normalized_username)
            .limit(1)
        ).first()
        if collision is not None:
            raise _error(
                "username_unavailable",
                "That username is unavailable.",
            )
        enrolled_at = datetime.now(UTC)
        credential = UserCredential(
            subscriber_id=subscriber.id,
            provider=AuthProvider.local,
            username=normalized_username,
            password_hash=auth_flow_service.hash_password(command.new_password),
            must_change_password=False,
            password_updated_at=enrolled_at,
            is_active=True,
        )
        db.add(credential)
        subscriber.email_verified = True
        db.flush()
        evidence = {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "causation_id": (
                str(command.context.causation_id)
                if command.context.causation_id is not None
                else None
            ),
            "reason": command.context.reason,
            "referral_id": str(context.referral_id),
            "email_sha256": context.email_digest,
            "credential_id": str(credential.id),
            "email_verified": True,
        }
        stage_audit_event(
            db,
            action="auth.customer_credential_enrollment_completed",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            actor_type=AuditActorType.user,
            actor_id=str(subscriber.id),
            metadata=evidence,
        )
        emit_event(
            db,
            EventType.customer_credential_enrollment_completed,
            {
                **evidence,
                "aggregate_type": "subscriber",
                "aggregate_id": str(subscriber.id),
                "aggregate_version": str(command.context.command_id),
                "principal_type": "subscriber",
                "principal_id": str(subscriber.id),
            },
            actor=command.context.actor,
            subscriber_id=subscriber.id,
        )
        return EnrollmentCompletionResult(
            subscriber_id=subscriber.id,
            username=normalized_username,
            email_verified=True,
            enrolled_at=enrolled_at,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    try:
        return execute_owner_command(
            db,
            definition=_COMPLETE_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "username_unavailable",
            "That username is unavailable.",
        ) from exc
