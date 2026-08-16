"""Execution tests for canonical single-subscription lifecycle commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.idempotency import IdempotencyKey
from app.models.lifecycle import SubscriptionLifecycleEvent
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.subscription_lifecycle_schedule import (
    SubscriptionLifecycleSchedule,
    SubscriptionLifecycleScheduleStatus,
)
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcome,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecycleError,
    resolve_subscription_lifecycle,
)
from app.services.subscription_lifecycle_commands import (
    execute_subscription_command,
)
from app.services.subscription_lifecycle_schedules import (
    apply_due_subscription_status_commands,
    cancel_scheduled_subscription_status_command,
)
from app.services.web_catalog_subscription_workflows import (
    execute_lifecycle_command_response,
    preview_lifecycle_command_response,
)


def _subscription(
    db_session,
    subscriber,
    offer,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
) -> Subscription:
    subscriber.billing_mode = BillingMode.postpaid
    item = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=BillingMode.postpaid,
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db_session.add(item)
    db_session.flush()
    return item


def _target_offer(db_session, current: CatalogOffer) -> CatalogOffer:
    current.plan_family = "unlimited"
    current.billing_mode = BillingMode.postpaid
    target = CatalogOffer(
        name="Faster Internet",
        code="FAST-EXEC",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.postpaid,
        plan_family="unlimited",
    )
    db_session.add(target)
    db_session.flush()
    db_session.add_all(
        [
            OfferPrice(
                offer_id=current.id,
                price_type=PriceType.recurring,
                amount=Decimal("10000.00"),
                currency="NGN",
            ),
            OfferPrice(
                offer_id=target.id,
                price_type=PriceType.recurring,
                amount=Decimal("15000.00"),
                currency="NGN",
            ),
        ]
    )
    db_session.flush()
    return target


def test_execute_activation_applies_and_records_outcome(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        expected_head=reviewed.head,
        idempotency_key="activate-1",
    )

    outcome = execute_subscription_command(
        db_session,
        command,
        actor_id="operator-1",
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    db_session.refresh(subscription)
    assert outcome.status == SubscriptionCommandOutcomeStatus.applied
    assert outcome.previous_head == reviewed.head
    assert outcome.current_head != reviewed.head
    assert subscription.status == SubscriptionStatus.active
    assert db_session.query(IdempotencyKey).count() == 1
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "subscription_lifecycle_command")
        .one()
    )
    assert audit.actor_id == "operator-1"
    assert audit.metadata_["status"] == "applied"


def test_admin_workflow_serializes_canonical_command_outcome(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))

    payload, status_code = execute_lifecycle_command_response(
        db_session,
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        actor_id="operator-2",
        expected_head=reviewed.head,
        idempotency_key="admin-workflow-activate",
        reason="installation accepted",
    )

    assert status_code == 200
    assert payload["status"] == "applied"
    assert payload["previous_head"] == reviewed.head
    assert payload["current_head"] != reviewed.head
    assert payload["replayed"] is False


def test_admin_workflow_returns_structured_validation_error(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)

    payload, status_code = execute_lifecycle_command_response(
        db_session,
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.change_plan,
        actor_id="operator-2",
    )

    assert status_code == 422
    assert payload["status"] == "rejected"
    assert payload["error_code"] == "invalid_lifecycle_command"
    assert "target_offer_id" in str(payload["message"])


def test_admin_workflow_maps_missing_funding_to_unavailable(db_session, monkeypatch):
    from app.services import web_catalog_subscription_workflows as workflows
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )

    def missing_funding(*_args, **_kwargs):
        raise PrepaidFundingBaselineMissingError("baseline missing")

    monkeypatch.setattr(
        workflows,
        "execute_subscription_command",
        missing_funding,
    )

    payload, status_code = execute_lifecycle_command_response(
        db_session,
        subscription_id="00000000-0000-0000-0000-000000000001",
        kind=SubscriptionCommandKind.change_plan,
        actor_id="operator-2",
        target_offer_id="00000000-0000-0000-0000-000000000002",
        preview_fingerprint="a" * 64,
        expected_head="b" * 64,
        idempotency_key="missing-funding",
    )

    assert status_code == 409
    assert payload["status"] == "unavailable"
    assert payload["error_code"] == "financial_position_unavailable"


def test_execution_requires_reviewed_head_and_idempotency_key(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    missing_head = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.activate,
            source="admin:test",
            idempotency_key="missing-head",
        ),
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    missing_key = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.activate,
            source="admin:test",
            expected_head=reviewed.head,
        ),
    )

    assert missing_head.error_code == "expected_head_required"
    assert missing_key.error_code == "idempotency_key_required"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.pending


def test_idempotent_replay_precedes_stale_head_validation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        expected_head=reviewed.head,
        idempotency_key="activate-retry",
    )

    first = execute_subscription_command(db_session, command)
    replay = execute_subscription_command(db_session, command)

    assert first.status == SubscriptionCommandOutcomeStatus.applied
    assert replay.status == SubscriptionCommandOutcomeStatus.skipped
    assert replay.replayed is True
    assert replay.error_code == "idempotent_replay"
    assert db_session.query(IdempotencyKey).count() == 1


def test_reused_idempotency_key_with_changed_payload_is_rejected(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    original = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        expected_head=reviewed.head,
        idempotency_key="shared-key",
    )
    execute_subscription_command(db_session, original)
    changed = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.suspend,
        source="admin:test",
        reason="different command",
        expected_head=reviewed.head,
        idempotency_key="shared-key",
    )

    outcome = execute_subscription_command(db_session, changed)

    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "idempotency_key_conflict"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_stale_review_is_superseded_without_mutation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.cancel,
        source="admin:test",
        expected_head="stale-head",
        idempotency_key="cancel-stale",
    )

    outcome = execute_subscription_command(db_session, command)

    assert outcome.status == SubscriptionCommandOutcomeStatus.superseded
    assert outcome.error_code == "subscription_head_changed"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active
    assert db_session.query(IdempotencyKey).count() == 0


def test_suspend_and_restore_delegate_to_account_lifecycle(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    suspend = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.suspend,
        source="admin:test",
        reason="operator hold",
        expected_head=reviewed.head,
        idempotency_key="suspend-then-restore:suspend",
    )

    suspended = execute_subscription_command(db_session, suspend)
    suspended_head = resolve_subscription_lifecycle(
        db_session, str(subscription.id)
    ).head
    restored = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.restore,
            source="admin:test",
            reason="operator cleared hold",
            expected_head=suspended_head,
            idempotency_key="suspend-then-restore:restore",
        ),
    )

    db_session.refresh(subscription)
    assert suspended.status == SubscriptionCommandOutcomeStatus.applied
    assert len(suspended.artifact_ids) == 1
    lock = db_session.get(EnforcementLock, suspended.artifact_ids[0])
    assert lock is not None
    assert lock.is_active is False
    assert restored.status == SubscriptionCommandOutcomeStatus.applied
    assert subscription.status == SubscriptionStatus.active


def test_restore_rejects_unresolved_prepaid_financial_lock_and_redirects_operator(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.suspended,
    )
    db_session.add(
        EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.prepaid,
            source="prepaid_enforcement:test",
            is_active=True,
        )
    )
    db_session.commit()

    preview_payload, preview_status = preview_lifecycle_command_response(
        db_session,
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.restore,
        actor_id="operator-1",
        reason="operator requested restore",
    )
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.restore,
            source="admin:test",
            reason="operator requested restore",
            expected_head=reviewed.head,
            idempotency_key="restore-prepaid-financial-lock",
        ),
    )

    assert preview_status == 200
    assert preview_payload["eligible"] is False
    assert preview_payload["eligibility_reasons"] == [
        "prepaid_financial_reconciliation_required"
    ]
    assert (
        preview_payload["recommended_action_url"]
        == "/admin/billing/invoices?status=draft"
    )
    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "prepaid_financial_reconciliation_required"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.suspended


def test_disable_and_restore_preserve_service_configuration(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    subscription.login = "retained-command-login"
    subscription.ipv4_address = "10.91.0.10"
    original_next_billing_at = datetime.now(UTC) + timedelta(days=20)
    subscription.next_billing_at = original_next_billing_at
    db_session.flush()
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))

    disabled = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.disable,
            source="admin:test",
            reason="operator disabled service",
            expected_head=reviewed.head,
            idempotency_key="disable-then-restore:disable",
        ),
    )
    disabled_event = (
        db_session.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
        .filter(SubscriptionLifecycleEvent.to_status == SubscriptionStatus.disabled)
        .order_by(SubscriptionLifecycleEvent.created_at.desc())
        .first()
    )
    assert disabled_event is not None
    disabled_event.created_at = datetime.now(UTC) - timedelta(days=5)
    db_session.flush()
    disabled_head = resolve_subscription_lifecycle(
        db_session, str(subscription.id)
    ).head
    restored = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.restore,
            source="admin:test",
            reason="operator restored service",
            expected_head=disabled_head,
            idempotency_key="disable-then-restore:restore",
        ),
    )

    db_session.refresh(subscription)
    assert disabled.status == SubscriptionCommandOutcomeStatus.applied
    assert restored.status == SubscriptionCommandOutcomeStatus.applied
    assert subscription.status == SubscriptionStatus.active
    assert subscription.login == "retained-command-login"
    assert subscription.ipv4_address == "10.91.0.10"
    shifted_next_billing_at = subscription.next_billing_at
    assert shifted_next_billing_at is not None
    if shifted_next_billing_at.tzinfo is None:
        shifted_next_billing_at = shifted_next_billing_at.replace(tzinfo=UTC)
    assert shifted_next_billing_at >= original_next_billing_at + timedelta(days=4)


def test_immediate_plan_change_delegates_to_catalog_owner(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.change_plan,
        source="admin:test",
        target_offer_id=str(target.id),
        expected_head=reviewed.head,
        idempotency_key="plan-now-1",
    )

    outcome = execute_subscription_command(db_session, command)

    db_session.refresh(subscription)
    assert outcome.status == SubscriptionCommandOutcomeStatus.applied
    assert subscription.offer_id == target.id
    assert db_session.query(IdempotencyKey).count() == 1


def test_deferred_plan_change_returns_scheduled_artifact_and_replays_it(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.change_plan,
        source="admin:test",
        target_offer_id=str(target.id),
        effective_timing=SubscriptionEffectiveTiming.next_cycle,
        expected_head=reviewed.head,
        idempotency_key="plan-next-1",
    )

    outcome = execute_subscription_command(db_session, command)
    replay = execute_subscription_command(db_session, command)

    assert outcome.status == SubscriptionCommandOutcomeStatus.scheduled
    assert len(outcome.artifact_ids) == 1
    request = db_session.get(SubscriptionChangeRequest, outcome.artifact_ids[0])
    assert request is not None
    assert request.status == SubscriptionChangeStatus.approved
    assert request.effective_date.isoformat() == "2026-08-01"
    assert replay.replayed is True
    assert replay.artifact_ids == outcome.artifact_ids
    db_session.refresh(subscription)
    assert subscription.offer_id == catalog_offer.id


def test_renewal_execution_remains_billing_owned(db_session, subscriber, catalog_offer):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))

    renewal = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.renew,
            source="admin:test",
            expected_head=reviewed.head,
            idempotency_key="renewal-owned-by-billing",
        ),
    )
    assert renewal.status == SubscriptionCommandOutcomeStatus.rejected
    assert renewal.error_code == "renewal_execution_is_billing_owned"


def test_deferred_status_command_is_durable_and_replays_schedule_artifact(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.cancel,
        source="admin:test",
        effective_timing=SubscriptionEffectiveTiming.scheduled,
        effective_at=datetime(2026, 8, 15, tzinfo=UTC),
        expected_head=reviewed.head,
        idempotency_key="cancel-august",
    )

    outcome = execute_subscription_command(
        db_session, command, now=datetime(2026, 7, 14, tzinfo=UTC)
    )
    replay = execute_subscription_command(
        db_session, command, now=datetime(2026, 7, 14, tzinfo=UTC)
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.scheduled
    assert replay.status == SubscriptionCommandOutcomeStatus.skipped
    assert replay.replayed is True
    assert replay.artifact_ids == outcome.artifact_ids
    schedule = db_session.get(SubscriptionLifecycleSchedule, outcome.artifact_ids[0])
    assert schedule is not None
    assert schedule.status == SubscriptionLifecycleScheduleStatus.pending
    assert schedule.reviewed_head == reviewed.head
    assert schedule.actor_type == "system"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_identical_schedules_replay_their_own_idempotency_artifact(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))

    def _command(key: str) -> SubscriptionLifecycleCommand:
        return SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
            effective_at=datetime(2026, 8, 15, tzinfo=UTC),
            expected_head=reviewed.head,
            idempotency_key=key,
        )

    now = datetime(2026, 7, 14, tzinfo=UTC)
    first = execute_subscription_command(db_session, _command("cancel-first"), now=now)
    second = execute_subscription_command(
        db_session, _command("cancel-second"), now=now
    )
    replay = execute_subscription_command(
        db_session, _command("cancel-second"), now=now
    )

    assert first.artifact_ids != second.artifact_ids
    assert replay.replayed is True
    assert replay.artifact_ids == second.artifact_ids


def test_due_status_command_executes_through_canonical_owner(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
            effective_at=datetime(2026, 7, 15, tzinfo=UTC),
            expected_head=reviewed.head,
            idempotency_key="cancel-due",
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    result = apply_due_subscription_status_commands(
        db_session,
        now=datetime(2026, 7, 15, tzinfo=UTC),
        worker_id="test-worker",
    )

    schedule = db_session.get(SubscriptionLifecycleSchedule, outcome.artifact_ids[0])
    db_session.refresh(subscription)
    assert result == {
        "claimed": 1,
        "applied": 1,
        "retried": 0,
        "superseded": 0,
        "rejected": 0,
        "failed": 0,
    }
    assert schedule is not None
    assert schedule.status == SubscriptionLifecycleScheduleStatus.applied
    assert schedule.applied_at is not None
    assert schedule.applied_at.replace(tzinfo=UTC) == datetime(2026, 7, 15, tzinfo=UTC)
    assert schedule.claimed_by is None
    assert subscription.status == SubscriptionStatus.canceled


def test_due_status_command_is_superseded_when_reviewed_state_drifted(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
            effective_at=datetime(2026, 7, 15, tzinfo=UTC),
            expected_head=reviewed.head,
            idempotency_key="cancel-superseded",
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    subscription.offer_id = _target_offer(db_session, catalog_offer).id
    subscription.updated_at = datetime(2026, 7, 14, 12, tzinfo=UTC)
    db_session.commit()

    result = apply_due_subscription_status_commands(
        db_session, now=datetime(2026, 7, 15, tzinfo=UTC)
    )

    schedule = db_session.get(SubscriptionLifecycleSchedule, outcome.artifact_ids[0])
    db_session.refresh(subscription)
    assert result["superseded"] == 1
    assert schedule is not None
    assert schedule.status == SubscriptionLifecycleScheduleStatus.superseded
    assert schedule.last_error_code == "subscription_head_changed"
    assert subscription.status == SubscriptionStatus.active


def test_pending_status_schedule_can_be_canceled(db_session, subscriber, catalog_offer):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.expire,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.next_cycle,
            expected_head=reviewed.head,
            idempotency_key="expire-next-cycle",
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    schedule = cancel_scheduled_subscription_status_command(
        db_session,
        outcome.artifact_ids[0],
        subscription_id=str(subscription.id),
        actor_id="operator-1",
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )
    result = apply_due_subscription_status_commands(
        db_session, now=datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert schedule.status == SubscriptionLifecycleScheduleStatus.canceled
    assert schedule.canceled_by == "operator-1"
    assert result["claimed"] == 0
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_failed_due_status_command_retries_with_backoff(
    db_session, subscriber, catalog_offer, monkeypatch
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
            effective_at=datetime(2026, 7, 15, tzinfo=UTC),
            expected_head=reviewed.head,
            idempotency_key="cancel-retry-backoff",
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    def _fail(_db, command, **kwargs):
        return SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.failed,
            message="temporary executor outage",
            previous_head=command.expected_head or "unknown",
            current_head=command.expected_head,
            error_code="command_execution_failed",
        )

    monkeypatch.setattr(
        "app.services.subscription_lifecycle_commands.execute_subscription_command",
        _fail,
    )
    result = apply_due_subscription_status_commands(
        db_session, now=datetime(2026, 7, 15, tzinfo=UTC)
    )

    schedule = db_session.get(SubscriptionLifecycleSchedule, outcome.artifact_ids[0])
    assert result["retried"] == 1
    assert schedule is not None
    assert schedule.status == SubscriptionLifecycleScheduleStatus.pending
    assert schedule.attempt_count == 1
    assert schedule.next_attempt_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 15, 0, 5, tzinfo=UTC
    )
    assert schedule.last_error_code == "command_execution_failed"


def test_due_status_command_stops_after_max_attempts(
    db_session, subscriber, catalog_offer, monkeypatch
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
            effective_at=datetime(2026, 7, 15, tzinfo=UTC),
            expected_head=reviewed.head,
            idempotency_key="cancel-max-attempts",
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    schedule = db_session.get(SubscriptionLifecycleSchedule, outcome.artifact_ids[0])
    assert schedule is not None
    schedule.max_attempts = 1
    db_session.commit()

    def _fail(_db, command, **kwargs):
        return SubscriptionCommandOutcome(
            command=command,
            status=SubscriptionCommandOutcomeStatus.failed,
            message="permanent executor outage",
            previous_head=command.expected_head or "unknown",
            current_head=command.expected_head,
            error_code="command_execution_failed",
        )

    monkeypatch.setattr(
        "app.services.subscription_lifecycle_commands.execute_subscription_command",
        _fail,
    )
    result = apply_due_subscription_status_commands(
        db_session, now=datetime(2026, 7, 15, tzinfo=UTC)
    )

    db_session.refresh(schedule)
    assert result["failed"] == 1
    assert schedule.status == SubscriptionLifecycleScheduleStatus.failed
    assert schedule.attempt_count == 1


def test_expired_processing_lease_is_reclaimed(db_session, subscriber, catalog_offer):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
            effective_at=datetime(2026, 7, 15, tzinfo=UTC),
            expected_head=reviewed.head,
            idempotency_key="cancel-lease-reclaim",
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    schedule = db_session.get(SubscriptionLifecycleSchedule, outcome.artifact_ids[0])
    assert schedule is not None
    schedule.status = SubscriptionLifecycleScheduleStatus.processing
    schedule.claimed_by = "dead-worker"
    schedule.claimed_at = datetime(2026, 7, 15, tzinfo=UTC)
    schedule.claim_expires_at = datetime(2026, 7, 15, 0, 15, tzinfo=UTC)
    db_session.commit()

    result = apply_due_subscription_status_commands(
        db_session, now=datetime(2026, 7, 15, 0, 16, tzinfo=UTC)
    )

    db_session.refresh(schedule)
    assert result["applied"] == 1
    assert schedule.status == SubscriptionLifecycleScheduleStatus.applied
    assert schedule.attempt_count == 1


def test_future_time_is_not_executed_under_immediate_timing(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))

    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_at=datetime(2026, 7, 15, tzinfo=UTC),
            expected_head=reviewed.head,
            idempotency_key="future-immediate-command",
        ),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "future_effective_at_requires_scheduling"
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_next_cycle_rejects_an_explicit_custom_date() -> None:
    with pytest.raises(SubscriptionLifecycleError, match="use scheduled"):
        SubscriptionLifecycleCommand(
            subscription_id="subscription-1",
            kind=SubscriptionCommandKind.change_plan,
            source="admin:test",
            target_offer_id="offer-1",
            effective_timing=SubscriptionEffectiveTiming.next_cycle,
            effective_at=datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_vacation_hold_and_resume_use_exact_customer_lock(
    db_session, subscriber, catalog_offer, monkeypatch
):
    monkeypatch.setattr(
        "app.services.settings_spec.resolve_value",
        lambda db, domain, key: {
            "max_suspend_days": 30,
            "max_suspend_holds_per_year": 2,
            "suspend_cooldown_days": 0,
        }.get(key),
    )
    subscription = _subscription(db_session, subscriber, catalog_offer)
    db_session.commit()
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    held = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.vacation_hold,
            source=f"customer:{subscriber.id}:vacation_hold",
            expected_head=reviewed.head,
            idempotency_key="vacation-owner:hold",
            vacation_hold_days=7,
        ),
    )
    assert held.status == SubscriptionCommandOutcomeStatus.applied
    assert len(held.artifact_ids) == 1
    lock = db_session.get(EnforcementLock, held.artifact_ids[0])
    assert lock is not None
    assert lock.reason == EnforcementReason.customer_hold
    assert lock.resume_at is not None
    assert subscription.status == SubscriptionStatus.suspended
    held_replay = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.vacation_hold,
            source=f"customer:{subscriber.id}:vacation_hold",
            expected_head=reviewed.head,
            idempotency_key="vacation-owner:hold",
            vacation_hold_days=7,
        ),
    )
    assert held_replay.replayed is True
    assert held_replay.artifact_ids == (str(lock.id),)

    held_head = resolve_subscription_lifecycle(db_session, str(subscription.id)).head
    resumed = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.vacation_resume,
            source=f"customer:{subscriber.id}:vacation_resume",
            expected_head=held_head,
            idempotency_key=f"vacation-owner:resume:{lock.id}",
        ),
    )
    db_session.refresh(lock)
    db_session.refresh(subscription)
    assert resumed.status == SubscriptionCommandOutcomeStatus.applied
    assert resumed.artifact_ids == (str(lock.id),)
    assert lock.is_active is False
    assert subscription.status == SubscriptionStatus.active
    resumed_replay = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.vacation_resume,
            source=f"customer:{subscriber.id}:vacation_resume",
            expected_head=held_head,
            idempotency_key=f"vacation-owner:resume:{lock.id}",
        ),
    )
    assert resumed_replay.replayed is True
    assert resumed_replay.artifact_ids == (str(lock.id),)


def test_vacation_hold_policy_rejects_annual_limit_inside_owner(
    db_session, subscriber, catalog_offer, monkeypatch
):
    monkeypatch.setattr(
        "app.services.settings_spec.resolve_value",
        lambda db, domain, key: {
            "max_suspend_days": 30,
            "max_suspend_holds_per_year": 1,
            "suspend_cooldown_days": 0,
        }.get(key),
    )
    subscription = _subscription(db_session, subscriber, catalog_offer)
    db_session.add(
        EnforcementLock(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            reason=EnforcementReason.customer_hold,
            source="historical-hold",
            is_active=False,
            resolved_at=datetime.now(UTC),
            resolved_by="test",
        )
    )
    db_session.commit()
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    outcome = execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.vacation_hold,
            source=f"customer:{subscriber.id}:vacation_hold",
            expected_head=reviewed.head,
            idempotency_key="vacation-owner:annual-limit",
            vacation_hold_days=7,
        ),
    )
    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "vacation_hold_annual_limit_reached"


def test_owner_failure_rolls_back_idempotency_reservation(
    db_session, subscriber, catalog_offer, monkeypatch
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    db_session.commit()

    def _fail(*args, **kwargs):
        raise RuntimeError("owner unavailable")

    monkeypatch.setattr(
        "app.services.account_lifecycle.transition_subscription_status",
        _fail,
    )
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
        expected_head=resolve_subscription_lifecycle(
            db_session, str(subscription.id)
        ).head,
        idempotency_key="activation-owner-failure",
    )

    outcome = execute_subscription_command(db_session, command)

    assert outcome.status == SubscriptionCommandOutcomeStatus.failed
    assert outcome.error_code == "command_execution_failed"
    assert "owner unavailable" in outcome.message
    assert db_session.query(IdempotencyKey).count() == 0


def test_expired_subscription_is_rejected_before_reservation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.expired,
    )
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.cancel,
        source="admin:test",
        expected_head=resolve_subscription_lifecycle(
            db_session, str(subscription.id)
        ).head,
        idempotency_key="terminal-cancel",
    )

    outcome = execute_subscription_command(
        db_session,
        command,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.rejected
    assert outcome.error_code == "status_expired_not_eligible_for_cancel"
    assert db_session.query(IdempotencyKey).count() == 0
