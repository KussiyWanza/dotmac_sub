"""Flush-only owner for one access credential service/profile binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, RadiusProfile, Subscription
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import owner_command_active

_OWNER = "access.credential_binding"
_COORDINATOR = "access.subscription_correction"


class AccessCredentialBindingError(DomainError):
    """Stable failure at the credential-binding participant boundary."""


def _error(suffix: str, message: str) -> NoReturn:
    raise AccessCredentialBindingError(
        code=f"{_OWNER}.{suffix}", message=message, details={}
    )


@dataclass(frozen=True, slots=True)
class BindAccessCredentialCommand:
    credential_id: UUID
    subscriber_id: UUID
    target_subscription_id: UUID
    target_radius_profile_id: UUID
    actor: str


def stage_access_credential_binding(
    db: Session, command: BindAccessCredentialCommand
) -> AccessCredential:
    """Flush one exact credential-to-service/profile binding as a participant."""
    if not owner_command_active(db, owner=_COORDINATOR):
        _error(
            "coordinator_required",
            "Credential binding changes require the subscription-correction owner.",
        )
    credential = db.scalar(
        select(AccessCredential)
        .where(AccessCredential.id == command.credential_id)
        .with_for_update()
    )
    target = db.get(Subscription, command.target_subscription_id)
    profile = db.get(RadiusProfile, command.target_radius_profile_id)
    if credential is None or not credential.is_active:
        _error("credential_missing", "The active access credential was not found.")
    if target is None or target.subscriber_id != command.subscriber_id:
        _error("account_mismatch", "The target subscription changed account.")
    if credential.subscriber_id != command.subscriber_id:
        _error("account_mismatch", "The access credential changed account.")
    if profile is None or not profile.is_active:
        _error("radius_profile_inactive", "The target RADIUS profile is unavailable.")
    previous_subscription_id = credential.subscription_id
    previous_profile_id = credential.radius_profile_id
    credential.subscription_id = target.id
    credential.radius_profile_id = profile.id
    credential.pre_throttle_radius_profile_id = None
    db.flush()
    emit_event(
        db,
        EventType.access_credential_binding_changed,
        {
            "schema_version": 1,
            "credential_id": str(credential.id),
            "subscriber_id": str(command.subscriber_id),
            "previous_subscription_id": str(previous_subscription_id)
            if previous_subscription_id
            else None,
            "target_subscription_id": str(target.id),
            "previous_radius_profile_id": str(previous_profile_id)
            if previous_profile_id
            else None,
            "target_radius_profile_id": str(profile.id),
            "reason": "mistaken_subscription_correction",
        },
        actor=command.actor,
        account_id=command.subscriber_id,
        subscription_id=target.id,
    )
    return credential


__all__ = [
    "AccessCredentialBindingError",
    "BindAccessCredentialCommand",
    "stage_access_credential_binding",
]
