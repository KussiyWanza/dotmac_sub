"""Reviewed account lifecycle commands for administrative status changes."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementLock
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import (
    ALLOWED_RESTORERS,
    derive_account_status_without_override,
    reactivation_blocked_by_active_login,
    transition_account_status,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

ACCOUNT_STATUS_WRITE_SCOPE = "customer:account-status:write"
_IDEMPOTENCY_SCOPE = "account_status"
_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner="customer.account_status_actions",
    concern="administrative account-bound idempotent status confirmation",
    name="confirm_account_status_change",
)


class AccountStatusAction(StrEnum):
    activate = "activate"
    suspend = "suspend"
    block = "block"
    disable = "disable"


_TARGET_STATUS = {
    AccountStatusAction.activate: SubscriberStatus.active,
    AccountStatusAction.suspend: SubscriberStatus.suspended,
    AccountStatusAction.block: SubscriberStatus.blocked,
    AccountStatusAction.disable: SubscriberStatus.disabled,
}


class AccountStatusCommandError(DomainError):
    """Stable transport-neutral account-status command failure."""


def _error(suffix: str, message: str, **details: object) -> AccountStatusCommandError:
    return AccountStatusCommandError(
        code=f"customer.account_status_actions.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class PreviewAccountStatusRequest:
    account_id: UUID
    action: AccountStatusAction


@dataclass(frozen=True, slots=True)
class AccountStatusPreview:
    account_id: UUID
    action: AccountStatusAction
    current_status: SubscriberStatus
    current_override: SubscriberStatus | None
    projected_status: SubscriberStatus
    clears_override: bool
    allowed: bool
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ConfirmAccountStatusCommand:
    context: CommandContext
    account_id: UUID
    action: AccountStatusAction
    expected_preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class AccountStatusOutcome:
    account_id: UUID
    action: AccountStatusAction
    prior_status: SubscriberStatus
    status: SubscriberStatus
    prior_override: SubscriberStatus | None
    lifecycle_override: SubscriberStatus | None
    replayed: bool


def _actor(context: CommandContext) -> tuple[AuditActorType, str]:
    prefix, separator, identifier = context.actor.partition(":")
    actor_id = identifier if separator and identifier else context.actor
    if prefix == "api_key":
        return AuditActorType.api_key, actor_id
    if prefix == "user":
        return AuditActorType.user, actor_id
    if prefix == "service":
        return AuditActorType.service, actor_id
    return AuditActorType.system, actor_id


def _load_account(db: Session, account_id: UUID) -> Subscriber:
    account = db.get(Subscriber, account_id)
    if account is None:
        raise _error(
            "account_not_found",
            "The subscriber account was not found.",
            account_id=str(account_id),
        )
    return account


def _preview(
    db: Session,
    *,
    account: Subscriber,
    action: AccountStatusAction,
) -> AccountStatusPreview:
    target = _TARGET_STATUS[action]
    subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == account.id)
            .order_by(Subscription.id)
        ).all()
    )
    locks = list(
        db.scalars(
            select(EnforcementLock)
            .where(
                EnforcementLock.subscriber_id == account.id,
                EnforcementLock.is_active.is_(True),
            )
            .order_by(EnforcementLock.id)
        ).all()
    )
    clears_override = action is AccountStatusAction.activate and bool(subscriptions)
    if clears_override:
        locks_by_subscription: dict[UUID, list[EnforcementLock]] = {}
        for lock in locks:
            locks_by_subscription.setdefault(lock.subscription_id, []).append(lock)
        projected_subscription_statuses: list[SubscriptionStatus] = []
        for subscription in subscriptions:
            projected = subscription.status
            if subscription.status in {
                SubscriptionStatus.pending,
                SubscriptionStatus.suspended,
                SubscriptionStatus.blocked,
                SubscriptionStatus.stopped,
                SubscriptionStatus.disabled,
            }:
                active_locks = locks_by_subscription.get(subscription.id, [])
                locks_allow_admin = all(
                    "admin" in ALLOWED_RESTORERS.get(lock.reason, set())
                    for lock in active_locks
                )
                if locks_allow_admin and not reactivation_blocked_by_active_login(
                    db, subscription
                ):
                    projected = SubscriptionStatus.active
            projected_subscription_statuses.append(projected)
        if any(
            status == SubscriptionStatus.active
            for status in projected_subscription_statuses
        ):
            projected_status = SubscriberStatus.active
        elif any(
            status == SubscriptionStatus.suspended
            for status in projected_subscription_statuses
        ):
            projected_status = SubscriberStatus.suspended
        elif any(
            status in {SubscriptionStatus.blocked, SubscriptionStatus.stopped}
            for status in projected_subscription_statuses
        ):
            projected_status = SubscriberStatus.blocked
        elif all(
            status == SubscriptionStatus.disabled
            for status in projected_subscription_statuses
        ):
            projected_status = SubscriberStatus.disabled
        else:
            projected_status = derive_account_status_without_override(
                db, str(account.id)
            )
    else:
        projected_status = target
    allowed = (
        account.lifecycle_override_status is not None
        if clears_override
        else account.lifecycle_override_status != target
        or account.status != projected_status
    )
    snapshot = {
        "account_id": str(account.id),
        "action": action.value,
        "billing_enabled": bool(account.billing_enabled),
        "current_status": account.status.value,
        "current_override": (
            account.lifecycle_override_status.value
            if account.lifecycle_override_status
            else None
        ),
        "projected_status": projected_status.value,
        "clears_override": clears_override,
        "subscriptions": [
            {"id": str(item.id), "status": item.status.value} for item in subscriptions
        ],
        "locks": [
            {
                "id": str(lock.id),
                "subscription_id": str(lock.subscription_id),
                "reason": lock.reason.value,
                "source": lock.source,
            }
            for lock in locks
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return AccountStatusPreview(
        account_id=account.id,
        action=action,
        current_status=account.status,
        current_override=account.lifecycle_override_status,
        projected_status=projected_status,
        clears_override=clears_override,
        allowed=allowed,
        fingerprint=fingerprint,
    )


def preview_account_status_change(
    db: Session, request: PreviewAccountStatusRequest
) -> AccountStatusPreview:
    """Return the current impact and confirmation fingerprint without mutation."""
    return _preview(
        db,
        account=_load_account(db, request.account_id),
        action=request.action,
    )


def _lock_account_and_subscriptions(db: Session, account_id: UUID) -> Subscriber:
    account = db.scalar(
        select(Subscriber).where(Subscriber.id == account_id).with_for_update()
    )
    if account is None:
        raise _error(
            "account_not_found",
            "The subscriber account was not found.",
            account_id=str(account_id),
        )
    list(
        db.scalars(
            select(Subscription.id)
            .where(Subscription.subscriber_id == account.id)
            .order_by(Subscription.id)
            .with_for_update()
        ).all()
    )
    list(
        db.scalars(
            select(EnforcementLock.id)
            .where(
                EnforcementLock.subscriber_id == account.id,
                EnforcementLock.is_active.is_(True),
            )
            .order_by(EnforcementLock.id)
            .with_for_update()
        ).all()
    )
    return account


def _reserve_idempotency(
    db: Session,
    *,
    account: Subscriber,
    command: ConfirmAccountStatusCommand,
) -> IdempotencyKey:
    key = str(command.context.idempotency_key or "").strip()
    if not key or len(key) > 120:
        raise _error(
            "invalid_idempotency_key",
            "An account-status idempotency key is required.",
        )
    scope = f"{_IDEMPOTENCY_SCOPE}:{command.action.value}"
    existing = db.scalar(
        select(IdempotencyKey)
        .where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .with_for_update()
    )
    if existing is not None:
        if existing.account_id != account.id:
            raise _error(
                "idempotency_account_mismatch",
                "The account-status confirmation belongs to another account.",
            )
        return existing
    reservation = IdempotencyKey(scope=scope, key=key, account_id=account.id)
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError as exc:
        raise _error(
            "idempotency_conflict",
            "The account-status confirmation conflicted with another request.",
        ) from exc
    return reservation


def _replayed(
    account: Subscriber,
    action: AccountStatusAction,
    ref_id: str,
) -> AccountStatusOutcome:
    parts = ref_id.split("|")
    if len(parts) != 5 or parts[0] != str(account.id):
        raise _error(
            "invalid_replay_evidence",
            "Stored account-status replay evidence is invalid.",
        )
    return AccountStatusOutcome(
        account_id=account.id,
        action=action,
        prior_status=SubscriberStatus(parts[1]),
        status=SubscriberStatus(parts[2]),
        prior_override=SubscriberStatus(parts[3]) if parts[3] else None,
        lifecycle_override=SubscriberStatus(parts[4]) if parts[4] else None,
        replayed=True,
    )


def _validate_context(command: ConfirmAccountStatusCommand) -> None:
    if command.context.scope != ACCOUNT_STATUS_WRITE_SCOPE:
        raise _error(
            "command_scope_mismatch",
            "Account-status write scope is required.",
        )
    if not command.context.reason.strip():
        raise _error("invalid_reason", "An account-status reason is required.")


def _stage_evidence(
    db: Session,
    *,
    account: Subscriber,
    command: ConfirmAccountStatusCommand,
    prior_status: SubscriberStatus,
    prior_override: SubscriberStatus | None,
) -> None:
    actor_type, actor_id = _actor(command.context)
    metadata: dict[str, object] = {
        "schema_version": 1,
        "account_id": str(account.id),
        "action": command.action.value,
        "prior_status": prior_status.value,
        "status": account.status.value,
        "prior_override": prior_override.value if prior_override else None,
        "lifecycle_override": (
            account.lifecycle_override_status.value
            if account.lifecycle_override_status
            else None
        ),
        "reason": command.context.reason,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "preview_fingerprint": command.expected_preview_fingerprint,
    }
    stage_audit_event(
        db,
        action="customer.account_status_changed",
        entity_type="subscriber",
        entity_id=str(account.id),
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=str(command.context.correlation_id),
        metadata=metadata,
    )
    emit_event(
        db,
        EventType.subscriber_updated,
        metadata,
        actor=command.context.actor,
        subscriber_id=account.id,
        account_id=account.id,
    )


def confirm_account_status_change(
    db: Session, command: ConfirmAccountStatusCommand
) -> AccountStatusOutcome:
    """Confirm one reviewed account lifecycle transition exactly once."""

    def operation() -> AccountStatusOutcome:
        _validate_context(command)
        account = _lock_account_and_subscriptions(db, command.account_id)
        reservation = _reserve_idempotency(db, account=account, command=command)
        if reservation.ref_id:
            return _replayed(account, command.action, reservation.ref_id)

        preview = _preview(db, account=account, action=command.action)
        expected = command.expected_preview_fingerprint.strip()
        if len(expected) != 64:
            raise _error(
                "invalid_preview_fingerprint",
                "The account-status preview fingerprint is invalid.",
            )
        if not secrets.compare_digest(preview.fingerprint, expected):
            raise _error(
                "stale_preview",
                "Account lifecycle state changed after preview; review it again.",
            )
        if not preview.allowed:
            raise _error(
                "action_not_allowed",
                "The account-status action would not change the account.",
            )
        if (
            command.action is AccountStatusAction.activate
            and not account.billing_enabled
        ):
            raise _error(
                "billing_approval_required",
                "Billing approval is required before the account can be activated.",
            )

        prior_status = account.status
        prior_override = account.lifecycle_override_status
        transition_account_status(
            db,
            str(account.id),
            _TARGET_STATUS[command.action],
            reason=command.context.reason,
            source=f"account_status_command:{command.context.command_id}",
        )
        db.flush()
        _stage_evidence(
            db,
            account=account,
            command=command,
            prior_status=prior_status,
            prior_override=prior_override,
        )
        reservation.ref_id = "|".join(
            (
                str(account.id),
                prior_status.value,
                account.status.value,
                prior_override.value if prior_override else "",
                account.lifecycle_override_status.value
                if account.lifecycle_override_status
                else "",
            )
        )
        db.flush()
        return AccountStatusOutcome(
            account_id=account.id,
            action=command.action,
            prior_status=prior_status,
            status=account.status,
            prior_override=prior_override,
            lifecycle_override=account.lifecycle_override_status,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=command.context,
        operation=operation,
    )
