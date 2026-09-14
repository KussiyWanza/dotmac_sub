"""PPPoE credential auto-generation service.

Generates PPPoE usernames from the subscriber canonical id and random passwords
when a subscription is activated and no active AccessCredential exists.
This always runs on subscription activation — PPPoE credentials are
mandatory for all subscribers.
"""

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from app.models.catalog import (
    AccessCredential,
    ConnectionType,
    RadiusProfile,
    Subscription,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import numbering, settings_spec
from app.services.credential_crypto import encrypt_credential
from app.services.customer_identifiers import pppoe_username_from_subscriber_number
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# DocumentSequence key for the fallback username (non-canonical subscribers).
SEQUENCE_KEY = "pppoe_username"
_OWNER = "access.pppoe_credentials"


class PppoeCredentialError(DomainError):
    """Stable failure at the PPPoE credential owner boundary."""


class PppoeCredentialDisposition(StrEnum):
    created = "created"
    reused = "reused"
    rebound_legacy = "rebound_legacy"
    reactivated = "reactivated"


@dataclass(frozen=True, slots=True)
class EnsurePppoeCredentialCommand:
    """Typed request to ensure one subscriber/service PPPoE identity."""

    subscriber_id: UUID
    subscription_id: UUID | None = None
    radius_profile_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EnsurePppoeCredentialOutcome:
    """Secret-free result of ensuring a PPPoE credential."""

    credential_id: UUID
    username: str
    disposition: PppoeCredentialDisposition
    changed: bool


def _error(code: str, message: str) -> PppoeCredentialError:
    return PppoeCredentialError(
        code=f"{_OWNER}.{code}",
        message=message,
        details={},
        retryable=False,
    )


def _stage_credential_event(
    db: Session,
    *,
    command: EnsurePppoeCredentialCommand,
    outcome: EnsurePppoeCredentialOutcome,
) -> EnsurePppoeCredentialOutcome:
    if not outcome.changed:
        return outcome
    emit_event(
        db,
        EventType.access_credential_ensured,
        {
            "schema_version": 1,
            "credential_id": str(outcome.credential_id),
            "subscriber_id": str(command.subscriber_id),
            "subscription_id": str(command.subscription_id)
            if command.subscription_id
            else None,
            "radius_profile_id": str(command.radius_profile_id)
            if command.radius_profile_id
            else None,
            "disposition": outcome.disposition.value,
        },
        account_id=command.subscriber_id,
        subscription_id=command.subscription_id,
    )
    return outcome


def _generate_random_password(length: int) -> str:
    """Generate a cryptographically random alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _resolve_int_setting(value: object, fallback: int) -> int:
    """Coerce a setting value to int with a fallback."""
    if value is None:
        return fallback
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return fallback
    return fallback


def _resolve_radius_setting(db: Session, key: str) -> object | None:
    """Resolve a radius setting directly from the database for activation-time consistency."""
    spec = settings_spec.get_spec(SettingDomain.radius, key)
    if not spec:
        return None

    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.radius)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    raw = settings_spec.extract_db_value(setting)
    if raw is None:
        raw = spec.default

    value, error = settings_spec.coerce_value(spec, raw)
    if error:
        value = spec.default
    if spec.allowed and value is not None and value not in spec.allowed:
        value = spec.default
    if spec.value_type == SettingValueType.integer and value is not None:
        parsed = _resolve_int_setting(
            value, spec.default if isinstance(spec.default, int) else 1
        )
        if spec.min_value is not None and parsed < spec.min_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        if spec.max_value is not None and parsed > spec.max_value:
            parsed = spec.default if isinstance(spec.default, int) else parsed
        value = parsed
    return value


def _generate_pppoe_username(db: Session, subscriber_id: UUID) -> str | None:
    from app.models.subscriber import Subscriber

    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is not None:
        derived = pppoe_username_from_subscriber_number(
            db, subscriber.subscriber_number
        )
        if derived:
            return derived
    # Fall back to a sequential username when the subscriber number isn't in the
    # canonical SUB-<digits> shape (imported/manual records). PPPoE credentials
    # are mandatory for activation, so generation must always yield a username —
    # otherwise non-canonical subscribers could never be activated.
    return _generate_pppoe_username_sequence(db)


def _generate_pppoe_username_sequence(db: Session) -> str | None:
    prefix_value = _resolve_radius_setting(db, "pppoe_username_prefix")
    padding_value = _resolve_radius_setting(db, "pppoe_username_padding")
    start_value = _resolve_radius_setting(db, "pppoe_username_start")
    return numbering.generate_number_with_config(
        db,
        SEQUENCE_KEY,
        prefix=prefix_value if isinstance(prefix_value, str) else None,
        padding=_resolve_int_setting(padding_value, 5),
        start_value=_resolve_int_setting(start_value, 1),
    )


def ensure_pppoe_credential(
    db: Session,
    command: EnsurePppoeCredentialCommand,
) -> EnsurePppoeCredentialOutcome:
    """Ensure one active PPPoE credential and its exact service binding.

    This participant is idempotent and flush-only. The lifecycle, catalog, or
    provisioning coordinator that admitted ``command`` owns transaction
    completion. Credential secrets never leave this boundary.
    """
    from app.models.subscriber import Subscriber

    subscriber = db.scalar(
        select(Subscriber)
        .where(Subscriber.id == command.subscriber_id)
        .with_for_update()
    )
    if subscriber is None:
        raise _error("subscriber_missing", "The target subscriber was not found.")

    subscription = None
    if command.subscription_id is not None:
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise _error(
                "subscription_missing", "The target subscription was not found."
            )
        if subscription.subscriber_id != command.subscriber_id:
            raise _error(
                "subscriber_mismatch",
                "The target subscription no longer belongs to the subscriber.",
            )

    if command.radius_profile_id is not None:
        profile = db.get(RadiusProfile, command.radius_profile_id)
        if profile is None or not profile.is_active:
            raise _error(
                "radius_profile_unavailable",
                "The target RADIUS profile is unavailable.",
            )

    existing_query = db.query(AccessCredential).filter(
        AccessCredential.subscriber_id == command.subscriber_id,
        AccessCredential.is_active.is_(True),
    )
    if command.subscription_id is not None:
        existing = (
            existing_query.filter(
                AccessCredential.subscription_id == command.subscription_id
            )
            .with_for_update()
            .first()
        )
        if existing is None:
            legacy = (
                existing_query.filter(AccessCredential.subscription_id.is_(None))
                .with_for_update()
                .all()
            )
            if len(legacy) == 1:
                existing = legacy[0]
                existing.subscription_id = command.subscription_id
                existing.radius_profile_id = command.radius_profile_id
                existing.connection_type = ConnectionType.pppoe
                if subscription is not None:
                    subscription.login = existing.username
                db.flush()
                return _stage_credential_event(
                    db,
                    command=command,
                    outcome=EnsurePppoeCredentialOutcome(
                        credential_id=existing.id,
                        username=existing.username,
                        disposition=PppoeCredentialDisposition.rebound_legacy,
                        changed=True,
                    ),
                )
    else:
        existing = existing_query.with_for_update().first()
    if existing:
        changed = existing.connection_type != ConnectionType.pppoe
        changed = changed or (
            command.radius_profile_id is not None
            and existing.radius_profile_id != command.radius_profile_id
        )
        changed = changed or (
            subscription is not None and subscription.login != existing.username
        )
        existing.connection_type = ConnectionType.pppoe
        if command.radius_profile_id is not None:
            existing.radius_profile_id = command.radius_profile_id
        if subscription is not None:
            subscription.login = existing.username
        db.flush()
        return _stage_credential_event(
            db,
            command=command,
            outcome=EnsurePppoeCredentialOutcome(
                credential_id=existing.id,
                username=existing.username,
                disposition=PppoeCredentialDisposition.reused,
                changed=changed,
            ),
        )

    has_other_service_credential = (
        command.subscription_id is not None
        and existing_query.filter(
            AccessCredential.subscription_id.is_not(None),
            AccessCredential.subscription_id != command.subscription_id,
        ).first()
        is not None
    )
    username = (
        _generate_pppoe_username_sequence(db)
        if has_other_service_credential
        else _generate_pppoe_username(db, command.subscriber_id)
    )
    if not username:
        raise _error(
            "username_unavailable",
            "A PPPoE username could not be allocated for the subscription.",
        )

    password_length_raw = _resolve_radius_setting(db, "pppoe_default_password_length")
    password_length = _resolve_int_setting(password_length_raw, 12)
    password_length = max(8, min(64, password_length))

    plain_password = _generate_random_password(password_length)
    encrypted_password = encrypt_credential(plain_password)

    credential = (
        db.query(AccessCredential)
        .filter(AccessCredential.username == username)
        .with_for_update()
        .first()
    )
    if credential is not None and credential.subscriber_id != command.subscriber_id:
        raise _error(
            "username_conflict",
            "The derived PPPoE username is already assigned to another subscriber.",
        )

    disposition = PppoeCredentialDisposition.reactivated
    if credential is None:
        credential = AccessCredential(
            subscriber_id=command.subscriber_id,
            subscription_id=command.subscription_id,
            username=username,
            is_active=True,
            connection_type=ConnectionType.pppoe,
        )
        db.add(credential)
        disposition = PppoeCredentialDisposition.created

    credential.secret_hash = encrypted_password
    credential.subscription_id = command.subscription_id
    credential.is_active = True
    credential.connection_type = ConnectionType.pppoe
    credential.radius_profile_id = command.radius_profile_id
    if subscription is not None:
        subscription.login = username

    db.flush()
    logger.info(
        "Ensured PPPoE credential",
        extra={
            "credential_id": str(credential.id),
            "subscription_id": str(command.subscription_id)
            if command.subscription_id
            else None,
            "disposition": disposition.value,
        },
    )
    return _stage_credential_event(
        db,
        command=command,
        outcome=EnsurePppoeCredentialOutcome(
            credential_id=credential.id,
            username=credential.username,
            disposition=disposition,
            changed=True,
        ),
    )


__all__ = [
    "EnsurePppoeCredentialCommand",
    "EnsurePppoeCredentialOutcome",
    "PppoeCredentialDisposition",
    "PppoeCredentialError",
    "ensure_pppoe_credential",
]
