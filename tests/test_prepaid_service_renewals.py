from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceStatus,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.customer_financial_ledger import calculate_customer_balance
from app.services.domain_errors import DomainError
from app.services.events.handlers.prepaid_renewal import PrepaidRenewalHandler
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.prepaid_service_renewals import (
    FundingChangeRenewalDisposition,
    PrepaidServiceRenewalError,
    PrepaidSettlementPeriodQuery,
    RunDuePrepaidServiceRenewalsCommand,
    apply_due_prepaid_service_after_funding_change,
    confirm_prepaid_service_renewal,
    execute_due_prepaid_service_renewals,
    preview_prepaid_service_renewal,
    resolve_prepaid_settlement_period,
    run_due_prepaid_service_renewals,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


def test_settlement_period_uses_lagos_calendar_day_before_month_arithmetic():
    period = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            # 00:30 on July 6 in Lagos is still July 5 in UTC.
            effective_at=datetime(2026, 7, 5, 23, 30, tzinfo=UTC),
            billing_cycle=BillingCycle.monthly,
        )
    )

    assert period.starts_on.isoformat() == "2026-07-06"
    assert period.ends_on.isoformat() == "2026-08-06"
    assert period.starts_at == datetime(2026, 7, 5, 23, tzinfo=UTC)
    assert period.ends_at == datetime(2026, 8, 5, 23, tzinfo=UTC)
    assert period.timezone_name == "Africa/Lagos"


def test_settlement_period_preserves_declared_month_end_clamp():
    period = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            effective_at=datetime(2026, 1, 31, 23, 30, tzinfo=UTC),
            billing_cycle=BillingCycle.monthly,
        )
    )

    assert period.starts_on.isoformat() == "2026-02-01"
    assert period.ends_on.isoformat() == "2026-03-01"
    assert period.starts_at == datetime(2026, 1, 31, 23, tzinfo=UTC)
    assert period.ends_at == datetime(2026, 2, 28, 23, tzinfo=UTC)


def test_funding_event_without_payment_id_remains_retryable(db_session, subscriber):
    event = Event(
        event_type=EventType.payment_received,
        payload={},
        account_id=subscriber.id,
    )

    with pytest.raises(DomainError) as exc_info:
        PrepaidRenewalHandler().handle(db_session, event)

    assert exc_info.value.code.endswith("event_payment_missing")


def test_succeeded_payment_without_settlement_evidence_remains_retryable(
    db_session,
    subscriber,
):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime(2026, 7, 23, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(payment)
    db_session.flush()
    event = Event(
        event_type=EventType.payment_received,
        payload={"payment_id": str(payment.id)},
        account_id=subscriber.id,
    )

    with pytest.raises(PrepaidServiceRenewalError) as exc_info:
        PrepaidRenewalHandler().handle(db_session, event)

    assert exc_info.value.code.endswith("settlement_missing")


def _prepare(db_session, subscriber, subscription, amount="100.00"):
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal(amount),
        position_at=datetime(2026, 6, 30, tzinfo=UTC),
    )


def _preview(db_session, subscription, amount="50.00"):
    return preview_prepaid_service_renewal(
        db_session,
        subscription_id=subscription.id,
        starts_at=datetime(2026, 7, 1, tzinfo=UTC),
        ends_at=datetime(2026, 7, 31, tzinfo=UTC),
        amount=Decimal(amount),
        currency="NGN",
    )


def test_prepaid_service_renewal_posts_exact_debit_and_entitlement(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)

    assert preview.funding_before == Decimal("100.00")
    assert preview.funding_after == Decimal("50.00")
    assert preview.allowed is True

    result = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
    )

    assert result.ledger_entry.entry_type == LedgerEntryType.debit
    assert result.ledger_entry.source == LedgerSource.adjustment
    assert result.ledger_entry.effective_date.replace(tzinfo=UTC) == datetime(
        2026, 7, 1, tzinfo=UTC
    )
    assert result.entitlement.source_ledger_entry_id == result.ledger_entry.id
    assert result.entitlement.subscription_id == subscription.id
    assert result.adjustment.origin == "prepaid_service_renewal"
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")
    db_session.refresh(subscription)
    assert subscription.next_billing_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 31, tzinfo=UTC
    )


def test_prepaid_service_renewal_is_idempotent(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    first = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
    )
    replay = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
    )

    assert replay.replayed is True
    assert replay.ledger_entry.id == first.ledger_entry.id
    assert db_session.query(AccountAdjustment).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")


def test_prepaid_service_renewal_replay_checks_under_account_lock(
    db_session, subscriber, subscription, monkeypatch
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
    )

    locks: list[str] = []
    monkeypatch.setattr(
        "app.services.prepaid_service_renewals.lock_account",
        lambda _db, account_id: locks.append(account_id),
    )

    replay = confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
    )

    assert replay.replayed is True
    assert locks == [str(subscriber.id)]


def test_prepaid_service_renewal_rejects_insufficient_canonical_funding(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription, amount="40.00")
    preview = _preview(db_session, subscription)

    assert preview.allowed is False
    assert preview.shortfall == Decimal("10.00")
    with pytest.raises(PrepaidServiceRenewalError) as exc:
        confirm_prepaid_service_renewal(
            db_session,
            preview,
            evidence_ref="pytest:reviewed-service-cycle",
        )
    assert exc.value.code.endswith("insufficient_funding")
    assert db_session.query(AccountAdjustment).count() == 0


def test_prepaid_service_renewal_rejects_overlapping_entitlement(
    db_session, subscriber, subscription
):
    _prepare(db_session, subscriber, subscription)
    preview = _preview(db_session, subscription)
    confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:reviewed-service-cycle",
    )

    with pytest.raises(PrepaidServiceRenewalError, match="already has active funding"):
        preview_prepaid_service_renewal(
            db_session,
            subscription_id=subscription.id,
            starts_at=datetime(2026, 7, 15, tzinfo=UTC),
            ends_at=datetime(2026, 8, 15, tzinfo=UTC),
            amount=Decimal("50.00"),
            currency="NGN",
        )


def _prepare_scheduled_cycle(db_session, subscriber, subscription):
    _prepare(db_session, subscriber, subscription)
    subscription.offer.billing_cycle = BillingCycle.monthly
    subscription.offer.is_active = True
    subscription.unit_price = Decimal("50.00")
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("50.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.commit()


def _add_due_account_without_baseline(db_session, subscriber, subscription):
    account = Subscriber(
        first_name="Missing",
        last_name="Baseline",
        email="missing-baseline@example.invalid",
        billing_mode=BillingMode.prepaid,
        reseller_id=subscriber.reseller_id,
        created_at=datetime(2026, 6, 29, tzinfo=UTC),
    )
    db_session.add(account)
    db_session.flush()
    due_subscription = Subscription(
        subscriber_id=account.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        unit_price=Decimal("50.00"),
    )
    db_session.add(due_subscription)
    db_session.commit()
    return account, due_subscription


def test_scheduled_owner_funds_current_due_cycle(db_session, subscriber, subscription):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )
    db_session.commit()

    assert summary["prepaid_renewals_scanned"] == 1
    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")


def _scheduled_command(run_at: datetime, *, key: str):
    return RunDuePrepaidServiceRenewalsCommand(
        context=CommandContext.system(
            actor="pytest:billing_automation",
            scope="prepaid_service_renewals",
            reason="verify the scheduled owner root",
            idempotency_key=key,
        ),
        run_at=run_at,
    )


def test_scheduled_public_owner_stages_exact_consumption_group(
    db_session, subscriber, subscription
):
    from app.models.customer_subledger import (
        CustomerPositionEffect,
        CustomerPostingGroup,
        PositionEffectKind,
        PostingCommandKind,
    )

    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    run_at = datetime(2026, 7, 1, 12, tzinfo=UTC)

    summary = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(run_at, key="pytest:scheduled-renewal-owner"),
    )

    assert summary["prepaid_renewals_funded"] == 1
    group = db_session.query(CustomerPostingGroup).one()
    adjustment = db_session.query(AccountAdjustment).one()
    entitlement = db_session.query(ServiceEntitlement).one()
    assert group.command_kind is PostingCommandKind.prepaid_consumption
    assert group.producer_owner == "financial.prepaid_service_renewals"
    assert group.source_kind == "account_adjustment"
    assert group.source_id == adjustment.id
    effects = (
        db_session.query(CustomerPositionEffect)
        .filter(CustomerPositionEffect.group_id == group.id)
        .all()
    )
    assert {effect.effect for effect in effects} == {
        PositionEffectKind.customer_credit_consumed,
        PositionEffectKind.prepaid_funding_reserved,
        PositionEffectKind.prepaid_funding_consumed,
    }
    assert {effect.entitlement_id for effect in effects} == {entitlement.id}
    assert {Decimal(str(effect.amount)) for effect in effects} == {Decimal("50.00")}

    # The same scheduled instant observes no second due period and never
    # duplicates the already committed group.
    db_session.commit()
    replay = execute_due_prepaid_service_renewals(
        db_session,
        _scheduled_command(run_at, key="pytest:scheduled-renewal-owner"),
    )
    assert replay["prepaid_renewals_funded"] == 0
    assert db_session.query(CustomerPostingGroup).count() == 1


def test_scheduled_posting_failure_rolls_back_renewal_business_result(
    db_session, subscriber, subscription, monkeypatch
):
    from app.models.customer_subledger import CustomerPostingGroup

    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("renewal posting unavailable")

    monkeypatch.setattr(
        "app.services.billing.customer_subledger.stage_posting_group", _boom
    )
    with pytest.raises(RuntimeError, match="renewal posting unavailable"):
        execute_due_prepaid_service_renewals(
            db_session,
            _scheduled_command(
                datetime(2026, 7, 1, 12, tzinfo=UTC),
                key="pytest:scheduled-renewal-atomicity",
            ),
        )

    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert db_session.query(CustomerPostingGroup).count() == 0


def test_funding_event_uses_owner_root_for_renewal_consumption(
    db_session, subscriber, subscription
):
    from app.models.customer_subledger import CustomerPostingGroup

    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    paid_at = datetime(2026, 7, 20, 17, 30, tzinfo=UTC)
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("50.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        is_active=True,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        PaymentSettlement(
            payment_id=payment.id,
            amount=payment.amount,
            unallocated_amount=payment.amount,
            prepaid_amount=Decimal("0.00"),
            currency=payment.currency,
            origin=PaymentSettlementOrigin.system,
            idempotency_key=f"pytest:settlement:{payment.id}",
        )
    )
    db_session.commit()

    event = Event(
        event_type=EventType.payment_received,
        payload={"payment_id": str(payment.id)},
        account_id=subscriber.id,
    )
    PrepaidRenewalHandler().handle(db_session, event)

    group = (
        db_session.query(CustomerPostingGroup)
        .filter_by(producer_owner="financial.prepaid_service_renewals")
        .one()
    )
    assert group.command_kind.value == "prepaid_consumption"
    assert group.source_kind == "account_adjustment"


def test_scheduled_owner_dry_run_writes_nothing(db_session, subscriber, subscription):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        dry_run=True,
    )

    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("100.00")


def test_scheduled_owner_skips_quarantine_and_funds_verified_account(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.next_billing_at = datetime(2026, 7, 1, 1, tzinfo=UTC)
    _account, excluded_subscription = _add_due_account_without_baseline(
        db_session, subscriber, subscription
    )

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )
    db_session.commit()

    assert summary["prepaid_renewals_scanned"] == 2
    assert summary["prepaid_renewals_quarantined"] == 1
    assert summary["prepaid_renewals_missing_baseline"] == 0
    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1
    db_session.refresh(excluded_subscription)
    assert excluded_subscription.next_billing_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 1, tzinfo=UTC
    )


def test_scheduled_owner_isolates_unexpected_missing_baseline(
    db_session, subscriber, subscription, monkeypatch
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.next_billing_at = datetime(2026, 7, 1, 1, tzinfo=UTC)
    _add_due_account_without_baseline(db_session, subscriber, subscription)
    monkeypatch.setattr(
        "app.services.prepaid_funding_reconstruction."
        "prepaid_funding_quarantined_account_ids",
        lambda _db, _account_ids: set(),
    )

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        dry_run=True,
    )

    assert summary["prepaid_renewals_scanned"] == 2
    assert summary["prepaid_renewals_quarantined"] == 0
    assert summary["prepaid_renewals_missing_baseline"] == 1
    assert summary["prepaid_renewals_funded"] == 1
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0


def test_scheduled_owner_refuses_catalog_fallback_without_contract_price(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.unit_price = None
    db_session.commit()

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )

    assert summary["prepaid_renewals_missing_price"] == 1
    assert summary["prepaid_renewals_funded"] == 0
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("100.00")


def test_scheduled_owner_refuses_historical_catch_up(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.next_billing_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.commit()

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 5, tzinfo=UTC),
    )

    assert summary["prepaid_renewals_stale_anchor"] == 1
    assert summary["prepaid_renewals_funded"] == 0
    assert db_session.query(AccountAdjustment).count() == 0


def test_scheduled_owner_restores_canonically_funded_prepaid_lock(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.status = SubscriptionStatus.suspended
    subscriber.status = SubscriberStatus.suspended
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber.id,
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid-balance",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()

    summary = run_due_prepaid_service_renewals(
        db_session,
        run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
    )
    db_session.commit()

    db_session.refresh(lock)
    db_session.refresh(subscription)
    assert summary["prepaid_renewals_funded"] == 1
    assert summary["prepaid_renewals_restored"] == 1
    assert lock.is_active is False
    assert subscription.status == SubscriptionStatus.active
    event = (
        db_session.query(EventStore)
        .filter_by(
            event_type="prepaid_service.renewed",
            subscription_id=subscription.id,
        )
        .one()
    )
    assert event.payload["source"] == "scheduled"
    assert event.payload["trigger_payment_id"] is None
    assert event.payload["renewed_through"] == "2026-08-01T00:00:00+00:00"


def test_funding_change_renews_suspended_due_service_from_payment_day(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    subscription.status = SubscriptionStatus.suspended
    db_session.commit()

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 20, 17, 30, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:account-credit-event",
    )
    db_session.commit()

    db_session.refresh(subscription)
    entitlement = db_session.query(ServiceEntitlement).one()
    assert result.disposition == FundingChangeRenewalDisposition.funded
    assert result.funded == 1
    assert entitlement.starts_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 20, tzinfo=UTC
    )
    assert entitlement.ends_at.replace(tzinfo=UTC) == datetime(2026, 8, 20, tzinfo=UTC)
    assert subscription.next_billing_at.replace(tzinfo=UTC) == datetime(
        2026, 8, 20, tzinfo=UTC
    )
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("50.00")
    assert len(result.renewals) == 1
    assert result.renewals[0].renewed_through == datetime(2026, 8, 20, tzinfo=UTC)
    assert result.renewals[0].source.value == "account_credit"
    event = (
        db_session.query(EventStore)
        .filter_by(event_type="prepaid_service.renewed")
        .one()
    )
    assert event.payload["renewed_through"] == "2026-08-20T00:00:00+00:00"


def test_funding_change_leaves_service_due_while_payable_invoice_remains(
    db_session, subscriber, subscription
):
    _prepare_scheduled_cycle(db_session, subscriber, subscription)
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-FIRST-CLAIM",
            status=InvoiceStatus.partially_paid,
            currency="NGN",
            total=Decimal("75.00"),
            balance_due=Decimal("25.00"),
            is_active=True,
        )
    )
    db_session.commit()

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 20, 17, 30, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:account-credit-event",
    )

    assert result.disposition == (
        FundingChangeRenewalDisposition.payable_invoice_remaining
    )
    assert result.funded == 0
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0
