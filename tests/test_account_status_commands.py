"""Behavior coverage for reviewed administrative account-status commands."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.event_store import EventStore
from app.models.subscriber import SubscriberStatus
from app.schemas.subscriber import SubscriberUpdate
from app.services import account_status_commands as status_service
from app.services import subscriber as subscriber_service
from app.services.account_lifecycle import get_active_locks, suspend_subscription
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _confirm(
    db,
    *,
    account_id: UUID,
    action: status_service.AccountStatusAction,
    idempotency_key: str | None = None,
    fingerprint: str | None = None,
):
    preview = status_service.preview_account_status_change(
        db,
        status_service.PreviewAccountStatusRequest(
            account_id=account_id,
            action=action,
        ),
    )
    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    return status_service.confirm_account_status_change(
        db,
        status_service.ConfirmAccountStatusCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:pytest-operator",
                scope=status_service.ACCOUNT_STATUS_WRITE_SCOPE,
                reason=f"pytest {action.value} account",
                idempotency_key=idempotency_key or f"pytest:{command_id}",
            ),
            account_id=account_id,
            action=action,
            expected_preview_fingerprint=fingerprint or preview.fingerprint,
        ),
    )


def test_generic_update_contract_excludes_lifecycle_fields() -> None:
    with pytest.raises(ValidationError):
        SubscriberUpdate.model_validate({"status": "suspended"})
    with pytest.raises(ValidationError):
        SubscriberUpdate.model_validate({"is_active": False})


def test_identity_edit_preserves_existing_account_override(db_session, subscriber):
    subscriber.lifecycle_override_status = SubscriberStatus.suspended
    subscriber.lifecycle_override_reason = "Security review"
    subscriber.lifecycle_override_source = "admin:security"
    subscriber.status = SubscriberStatus.suspended
    db_session.commit()

    subscriber_service.subscribers.update(
        db_session,
        str(subscriber.id),
        SubscriberUpdate(first_name="Updated"),
    )

    db_session.refresh(subscriber)
    assert subscriber.first_name == "Updated"
    assert subscriber.status == SubscriberStatus.suspended
    assert subscriber.lifecycle_override_status == SubscriberStatus.suspended
    assert subscriber.lifecycle_override_source == "admin:security"


def test_confirmed_suspend_is_audited_and_replays_exactly_once(db_session, subscriber):
    account_id = subscriber.id
    key = f"pytest:{uuid4()}"
    first = _confirm(
        db_session,
        account_id=account_id,
        action=status_service.AccountStatusAction.suspend,
        idempotency_key=key,
    )
    db_session.expire_all()
    replay = _confirm(
        db_session,
        account_id=account_id,
        action=status_service.AccountStatusAction.suspend,
        idempotency_key=key,
        fingerprint="0" * 64,
    )

    refreshed = db_session.get(type(subscriber), account_id)
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.prior_status == first.prior_status
    assert replay.status == first.status
    assert refreshed.status == SubscriberStatus.suspended
    assert refreshed.lifecycle_override_status == SubscriberStatus.suspended
    event = db_session.scalar(
        select(EventStore).where(
            EventStore.event_type == "subscriber.updated",
            EventStore.account_id == account_id,
        )
    )
    assert event is not None
    assert event.payload["action"] == "suspend"


def test_stale_preview_fails_closed(db_session, subscriber):
    preview = status_service.preview_account_status_change(
        db_session,
        status_service.PreviewAccountStatusRequest(
            account_id=subscriber.id,
            action=status_service.AccountStatusAction.suspend,
        ),
    )
    subscriber.billing_enabled = False
    db_session.commit()

    with pytest.raises(DomainError) as exc_info:
        _confirm(
            db_session,
            account_id=subscriber.id,
            action=status_service.AccountStatusAction.suspend,
            fingerprint=preview.fingerprint,
        )

    assert exc_info.value.code.endswith("stale_preview")


def test_account_restore_runs_through_explicit_subscription_lifecycle(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    subscriber.billing_enabled = True
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    _confirm(
        db_session,
        account_id=account_id,
        action=status_service.AccountStatusAction.suspend,
    )
    suspend_subscription(
        db_session,
        str(subscription_id),
        reason=EnforcementReason.fraud,
        source="fraud:pytest",
        emit=False,
    )
    db_session.commit()

    restored = _confirm(
        db_session,
        account_id=account_id,
        action=status_service.AccountStatusAction.activate,
    )

    refreshed = db_session.get(type(subscriber), account_id)
    locks = get_active_locks(db_session, subscription_id=str(subscription_id))
    assert restored.status == SubscriberStatus.active
    assert refreshed.lifecycle_override_status is None
    assert locks == []


def test_unsuspend_restores_only_same_source_suspension(
    db_session, subscriber, subscription
):
    subscriber.billing_enabled = True
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    _confirm(
        db_session,
        account_id=subscriber.id,
        action=status_service.AccountStatusAction.suspend,
    )
    preview = status_service.preview_account_status_change(
        db_session,
        status_service.PreviewAccountStatusRequest(
            account_id=subscriber.id,
            action=status_service.AccountStatusAction.unsuspend,
        ),
    )

    assert preview.allowed is True
    assert preview.affected_subscription_ids == (subscription.id,)
    assert len(preview.matching_lock_ids) == 1

    outcome = _confirm(
        db_session,
        account_id=subscriber.id,
        action=status_service.AccountStatusAction.unsuspend,
    )

    db_session.refresh(subscriber)
    db_session.refresh(subscription)
    assert outcome.status == SubscriberStatus.active
    assert subscriber.lifecycle_override_status is None
    assert subscription.status == SubscriptionStatus.active
    assert get_active_locks(db_session, subscription_id=str(subscription.id)) == []


def test_unsuspend_legacy_override_preserves_disabled_subscription(
    db_session, subscriber, subscription
):
    subscriber.billing_enabled = True
    subscriber.status = SubscriberStatus.suspended
    subscriber.lifecycle_override_status = SubscriberStatus.suspended
    subscriber.lifecycle_override_reason = "Legacy administrative suspension"
    subscriber.lifecycle_override_source = "subscriber_service:update"
    subscription.status = SubscriptionStatus.active
    disabled = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.disabled,
        billing_mode=subscription.billing_mode,
        unit_price=subscription.unit_price,
    )
    db_session.add(disabled)
    db_session.commit()

    preview = status_service.preview_account_status_change(
        db_session,
        status_service.PreviewAccountStatusRequest(
            account_id=subscriber.id,
            action=status_service.AccountStatusAction.unsuspend,
        ),
    )
    assert preview.allowed is True
    assert preview.projected_status == SubscriberStatus.active
    assert preview.affected_subscription_ids == ()
    assert preview.preserved_disabled_subscription_ids == (disabled.id,)

    _confirm(
        db_session,
        account_id=subscriber.id,
        action=status_service.AccountStatusAction.unsuspend,
    )

    db_session.refresh(subscriber)
    db_session.refresh(subscription)
    db_session.refresh(disabled)
    assert subscriber.status == SubscriberStatus.active
    assert subscriber.lifecycle_override_status is None
    assert subscription.status == SubscriptionStatus.active
    assert disabled.status == SubscriptionStatus.disabled


def test_unsuspend_preserves_unrelated_lock(
    db_session, subscriber, subscription, catalog_offer
):
    subscriber.billing_enabled = True
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    _confirm(
        db_session,
        account_id=subscriber.id,
        action=status_service.AccountStatusAction.suspend,
    )
    unrelated = suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="fraud:pytest",
        emit=False,
    )
    active_sibling = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=subscription.billing_mode,
        unit_price=subscription.unit_price,
    )
    db_session.add(active_sibling)
    db_session.commit()

    preview = status_service.preview_account_status_change(
        db_session,
        status_service.PreviewAccountStatusRequest(
            account_id=subscriber.id,
            action=status_service.AccountStatusAction.unsuspend,
        ),
    )
    assert preview.allowed is True
    assert "fraud" in preview.remaining_blockers

    _confirm(
        db_session,
        account_id=subscriber.id,
        action=status_service.AccountStatusAction.unsuspend,
    )

    db_session.refresh(subscription)
    db_session.refresh(active_sibling)
    db_session.refresh(unrelated)
    assert subscription.status == SubscriptionStatus.suspended
    assert active_sibling.status == SubscriptionStatus.active
    assert unrelated.is_active is True
