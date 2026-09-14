"""Trigger-execution receipt semantics and rollback isolation.

Covers the highest-priority acceptance gaps the round-1 review found
missing: payment preservation on a forced consequence-transaction failure,
and the receipt's exact-replay / mismatched-replay semantics. This is a
deliberately narrower slice than the full ten-category test list the
corrected brief calls for -- see the final report for what is NOT covered
here (two-session PostgreSQL concurrency, the full `reviewed_opening_fundable`
lane, and full end-to-end ambiguous-classification-through-dispatch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.billing import (
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
)
from app.models.catalog import BillingCycle, BillingMode, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.prepaid_funding import PrepaidFundingTriggerExecution
from app.services import event_store as event_store_service
from app.services.domain_errors import DomainError
from app.services.events.types import Event, EventType
from app.services.prepaid_service_renewals import (
    PrepaidServiceRenewalError,
    evaluate_prepaid_service_after_settlement,
)
from tests.prepaid_funding_helpers import (
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)


def _durable_event(db_session, *, account_id, payment_id) -> EventStore:
    event = Event(
        event_type=EventType.payment_received,
        payload={"payment_id": str(payment_id)},
        account_id=account_id,
    )
    record = event_store_service.create_event_record(db_session, event)
    db_session.flush()
    return record


def _settled_payment(db_session, subscriber) -> Payment:
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(payment)
    db_session.flush()
    settlement = PaymentSettlement(
        payment_id=payment.id,
        currency="NGN",
        amount=Decimal("100.00"),
        origin=PaymentSettlementOrigin.system,
    )
    db_session.add(settlement)
    db_session.commit()
    db_session.refresh(payment)
    return payment


def test_payment_and_settlement_rows_survive_a_forced_consequence_rollback(
    db_session, subscriber, subscription, monkeypatch
):
    """A forced failure in THIS handler's own transaction must never reach
    back and mutate the payment/settlement another owner already committed.
    """
    subscriber.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    ensure_test_prepaid_contract(db_session, subscription, Decimal("50.00"))
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
        position_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    payment = _settled_payment(db_session, subscriber)
    payment_id = payment.id
    payment_paid_at = payment.paid_at
    payment_status = payment.status

    def _boom(*_args, **_kwargs):
        raise PrepaidServiceRenewalError(
            code="test.forced_consequence_failure",
            message="forced failure for the payment-preservation test",
            retryable=False,
        )

    monkeypatch.setattr(
        "app.services.prepaid_service_renewals.apply_due_prepaid_service_after_funding_change",
        _boom,
    )

    with pytest.raises(DomainError):
        evaluate_prepaid_service_after_settlement(
            db_session,
            account_id=subscriber.id,
            payment_id=payment_id,
            evidence_ref="pytest:payment-preservation",
            # No `event_id` here on purpose (this test predates the receipt
            # model), but the fail-closed guard added 2026-09 round 7 would
            # otherwise raise on THAT before ever reaching the injected
            # `_boom` failure below, making this test pass for the wrong
            # reason (a vacuous pass, not a real proof of payment/settlement
            # preservation) -- `skip_receipt_for_repair=True` is the same
            # explicit, named opt-out a real repair call uses.
            skip_receipt_for_repair=True,
        )

    # The forced failure must not have reached back into the payment/
    # settlement this handler's transaction never owns.
    reloaded = db_session.get(Payment, payment_id)
    assert reloaded is not None
    assert reloaded.paid_at == payment_paid_at
    assert reloaded.status == payment_status
    assert reloaded.is_active is True
    settlement = (
        db_session.query(PaymentSettlement)
        .filter(PaymentSettlement.payment_id == payment_id)
        .one()
    )
    assert settlement.amount == Decimal("100.00")


def test_receipt_replay_returns_same_result_without_duplicate_mutation(
    db_session, subscriber, subscription
):
    subscriber.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.billing_cycle = BillingCycle.monthly
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    ensure_test_prepaid_contract(db_session, subscription, Decimal("50.00"))
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
        position_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    payment = _settled_payment(db_session, subscriber)
    event_record = _durable_event(
        db_session, account_id=subscriber.id, payment_id=payment.id
    )
    db_session.commit()

    first = evaluate_prepaid_service_after_settlement(
        db_session,
        account_id=subscriber.id,
        payment_id=payment.id,
        evidence_ref="pytest:receipt-replay",
        event_id=event_record.event_id,
    )
    db_session.commit()
    assert db_session.query(PrepaidFundingTriggerExecution).count() == 1
    entitlement_count_after_first = db_session.query(ServiceEntitlement).count()

    second = evaluate_prepaid_service_after_settlement(
        db_session,
        account_id=subscriber.id,
        payment_id=payment.id,
        evidence_ref="pytest:receipt-replay",
        event_id=event_record.event_id,
    )
    db_session.commit()

    # Still exactly one receipt row -- the replay did not create a second.
    assert db_session.query(PrepaidFundingTriggerExecution).count() == 1
    assert db_session.query(ServiceEntitlement).count() == entitlement_count_after_first
    assert second.disposition == first.disposition


def test_receipt_mismatch_raises_permanent_conflict(
    db_session, subscriber, subscription, monkeypatch
):
    """Same durable event, but a receipt already exists recording DIFFERENT
    computed inputs than what this call now presents -- a genuine
    data/logic inconsistency, not a routine replay.

    This only asserts the RAISE. The review item's OUT-OF-BAND write
    (`_record_review_item_out_of_band`) and its survival across a real
    `execute_owner_command` rollback are proven in
    `tests/integration/test_prepaid_funding_trigger_execution_rollback.py`,
    which requires a genuine second PostgreSQL connection -- this file's
    `db_session` fixture shares one physical SQLite connection for the whole
    test (`StaticPool`), so a same-session assertion here would pass even if
    the out-of-band write were broken (exactly the round-2 defect this test
    used to hide).

    `_record_review_item_out_of_band` opens its own session via
    `db_session_adapter.create_session()` (`SessionLocal()`, bound to
    `DATABASE_URL`) and RE-RAISES if that write fails (round-3 correction:
    a permanent conflict that cannot be durably recorded must not be
    silently skipped past). `conftest.py` deliberately points `DATABASE_URL`
    at an unreachable port (`127.0.0.1:9`, a discard port, `connect_timeout=
    1`) so any accidental real-DB access in the SQLite unit-test lane fails
    fast and loud rather than silently hitting a real database -- correct
    for that purpose, but it means this exact code path cannot complete in
    this tier without redirecting it (2026-09, round 9). Redirected to this
    test's OWN `db_session` (the established pattern used across this
    codebase's unit tests for exactly this scenario, e.g.
    `test_forwarding_observation_collector.py`) -- safe here specifically
    because nothing in this test touches `db_session` after the assertion
    block below, so an out-of-band `.commit()`/`.close()` against the same
    object cannot corrupt a later assertion.
    """
    monkeypatch.setattr(
        "app.services.db_session_adapter.db_session_adapter.create_session",
        lambda: db_session,
    )
    subscriber.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    ensure_test_prepaid_contract(db_session, subscription, Decimal("50.00"))
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
        position_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    payment = _settled_payment(db_session, subscriber)
    event_record = _durable_event(
        db_session, account_id=subscriber.id, payment_id=payment.id
    )
    db_session.commit()

    # Plant a receipt for this exact event_store_id with a fingerprint that
    # cannot match anything this call will ever compute.
    stale_receipt = PrepaidFundingTriggerExecution(
        event_store_id=event_record.id,
        event_id=event_record.event_id,
        event_type=event_record.event_type,
        payment_id=payment.id,
        account_id=subscriber.id,
        currency="NGN",
        effective_at=payment.paid_at,
        request_fingerprint="f" * 64,
        outcome_fingerprint="0" * 64,
        disposition="renewal_review_required",
    )
    db_session.add(stale_receipt)
    db_session.commit()

    with pytest.raises(PrepaidServiceRenewalError) as exc_info:
        evaluate_prepaid_service_after_settlement(
            db_session,
            account_id=subscriber.id,
            payment_id=payment.id,
            evidence_ref="pytest:receipt-conflict",
            event_id=event_record.event_id,
        )
    assert exc_info.value.code.endswith("trigger_execution_conflict")
    assert exc_info.value.retryable is False
