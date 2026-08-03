from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
    ServiceEntitlement,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    OfferStatus,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementReason
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.billing import PaymentAllocationApply, PaymentCreate
from app.services import billing as billing_service
from app.services.account_lifecycle import suspend_subscription


def _make_subscriber(db: Session, *, status: SubscriberStatus) -> Subscriber:
    subscriber = Subscriber(
        first_name="Payment",
        last_name="Status",
        email=f"payment-status-{uuid.uuid4().hex[:8]}@example.com",
        status=status,
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _make_offer(
    db: Session,
    *,
    billing_mode: BillingMode = BillingMode.postpaid,
    billing_cycle: BillingCycle = BillingCycle.monthly,
) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Payment Status Offer {uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=billing_mode,
        billing_cycle=billing_cycle,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _make_subscription(
    db: Session,
    subscriber: Subscriber,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
    billing_mode: BillingMode = BillingMode.postpaid,
    billing_cycle: BillingCycle = BillingCycle.monthly,
    next_billing_at: datetime | None = None,
) -> Subscription:
    offer = _make_offer(db, billing_mode=billing_mode, billing_cycle=billing_cycle)
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=billing_mode,
        unit_price=(
            Decimal("1000.00") if billing_mode == BillingMode.prepaid else None
        ),
        next_billing_at=next_billing_at,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _make_overdue_invoice(
    db: Session,
    subscriber: Subscriber,
    *,
    total: str = "1000.00",
) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        due_at=datetime.now(UTC) - timedelta(days=3),
    )
    db.add(invoice)
    db.flush()
    return invoice


def _make_prepaid_renewal_invoice(
    db: Session,
    subscriber: Subscriber,
    subscription: Subscription,
    *,
    period_start: datetime,
    period_end: datetime,
    total: str = "1000.00",
) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        billing_period_start=period_start,
        billing_period_end=period_end,
        issued_at=period_start,
        due_at=period_start,
    )
    db.add(invoice)
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description=(
            f"{subscription.offer.name} ({period_start.date()} - {period_end.date()})"
        ),
        quantity=Decimal("1.000"),
        unit_price=Decimal(total),
        amount=Decimal(total),
        metadata_={
            "kind": "base_subscription",
            "billing_period_start": period_start.isoformat(),
            "billing_period_end": period_end.isoformat(),
        },
    )
    db.add(line)
    db.flush()
    return invoice


def _utc_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def test_final_invoice_payment_restores_subscription_and_recomputes_account_status(
    db_session,
):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.active)
    subscription = _make_subscription(db_session, subscriber)
    invoice = _make_overdue_invoice(db_session, subscriber)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
        emit=False,
    )
    subscriber.status = SubscriberStatus.suspended
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    db_session.refresh(subscriber)
    assert invoice.status == InvoiceStatus.paid
    assert subscription.status == SubscriptionStatus.active
    assert subscriber.status == SubscriberStatus.active


def test_partial_invoice_payment_does_not_restore_or_mark_account_active(db_session):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.active)
    subscription = _make_subscription(db_session, subscriber)
    invoice = _make_overdue_invoice(db_session, subscriber)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
        emit=False,
    )
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("400.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("400.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    db_session.refresh(subscriber)
    assert invoice.status == InvoiceStatus.partially_paid
    assert subscription.status == SubscriptionStatus.suspended
    assert subscriber.status == SubscriberStatus.suspended


def test_payment_recomputes_stale_account_status_after_subscription_already_active(
    db_session,
):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.suspended)
    subscription = _make_subscription(
        db_session, subscriber, status=SubscriptionStatus.active
    )
    invoice = _make_overdue_invoice(db_session, subscriber)
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    db_session.refresh(subscriber)
    assert invoice.status == InvoiceStatus.paid
    assert subscription.status == SubscriptionStatus.active
    assert subscriber.status == SubscriberStatus.active


def test_lapsed_prepaid_invoice_payment_reanchors_period_to_payment_date(db_session):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.suspended)
    old_start = datetime(2026, 7, 3, tzinfo=UTC)
    old_end = datetime(2026, 8, 3, tzinfo=UTC)
    paid_at = datetime(2026, 8, 5, 14, 30, tzinfo=UTC)
    subscription = _make_subscription(
        db_session,
        subscriber,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        next_billing_at=old_end,
    )
    invoice = _make_prepaid_renewal_invoice(
        db_session,
        subscriber,
        subscription,
        period_start=old_start,
        period_end=old_end,
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source=f"invoice:{invoice.id}",
        emit=False,
    )
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=paid_at,
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    assert invoice.status == InvoiceStatus.paid
    assert invoice.billing_period_start == _utc_naive(
        datetime(2026, 8, 4, 23, tzinfo=UTC)
    )
    assert invoice.billing_period_end == _utc_naive(
        datetime(2026, 9, 4, 23, tzinfo=UTC)
    )
    assert subscription.next_billing_at == _utc_naive(
        datetime(2026, 9, 4, 23, tzinfo=UTC)
    )
    assert subscription.status == SubscriptionStatus.active
    assert line.description.endswith("(2026-08-05 - 2026-09-05)")
    assert line.metadata_["billing_period_start"] == "2026-08-04T23:00:00+00:00"
    assert line.metadata_["billing_period_end"] == "2026-09-04T23:00:00+00:00"
    entitlement = (
        db_session.query(ServiceEntitlement)
        .filter(ServiceEntitlement.subscription_id == subscription.id)
        .one()
    )
    assert entitlement.source_invoice_id == invoice.id
    assert entitlement.starts_at == _utc_naive(datetime(2026, 8, 4, 23, tzinfo=UTC))
    assert entitlement.ends_at == _utc_naive(datetime(2026, 9, 4, 23, tzinfo=UTC))


def test_lapsed_prepaid_payment_in_first_wat_hour_uses_new_local_day(db_session):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.suspended)
    subscription = _make_subscription(
        db_session,
        subscriber,
        status=SubscriptionStatus.suspended,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    invoice = _make_prepaid_renewal_invoice(
        db_session,
        subscriber,
        subscription,
        period_start=datetime(2026, 6, 1, tzinfo=UTC),
        period_end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            # July 5 at 23:30 UTC is July 6 at 00:30 WAT.
            paid_at=datetime(2026, 7, 5, 23, 30, tzinfo=UTC),
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    line = db_session.query(InvoiceLine).filter_by(invoice_id=invoice.id).one()
    assert invoice.billing_period_start == _utc_naive(
        datetime(2026, 7, 5, 23, tzinfo=UTC)
    )
    assert invoice.billing_period_end == _utc_naive(
        datetime(2026, 8, 5, 23, tzinfo=UTC)
    )
    assert subscription.next_billing_at == _utc_naive(
        datetime(2026, 8, 5, 23, tzinfo=UTC)
    )
    assert line.description.endswith("(2026-07-06 - 2026-08-06)")


def test_lapsed_prepaid_payment_preserves_existing_extension_delta(db_session):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.suspended)
    old_start = datetime(2026, 7, 3, tzinfo=UTC)
    old_end = datetime(2026, 8, 3, tzinfo=UTC)
    extension_days = timedelta(days=5)
    subscription = _make_subscription(
        db_session,
        subscriber,
        status=SubscriptionStatus.suspended,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        next_billing_at=old_end + extension_days,
    )
    invoice = _make_prepaid_renewal_invoice(
        db_session,
        subscriber,
        subscription,
        period_start=old_start,
        period_end=old_end,
    )
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=datetime(2026, 8, 5, 14, 30, tzinfo=UTC),
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    assert invoice.billing_period_start == _utc_naive(
        datetime(2026, 8, 4, 23, tzinfo=UTC)
    )
    assert invoice.billing_period_end == _utc_naive(
        datetime(2026, 9, 4, 23, tzinfo=UTC)
    )
    assert subscription.next_billing_at == _utc_naive(
        datetime(2026, 9, 9, 23, tzinfo=UTC)
    )


def test_current_prepaid_invoice_payment_keeps_existing_period_anchor(db_session):
    subscriber = _make_subscriber(db_session, status=SubscriberStatus.active)
    period_start = datetime(2026, 8, 3, tzinfo=UTC)
    period_end = datetime(2026, 9, 3, tzinfo=UTC)
    subscription = _make_subscription(
        db_session,
        subscriber,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        next_billing_at=period_end,
    )
    invoice = _make_prepaid_renewal_invoice(
        db_session,
        subscriber,
        subscription,
        period_start=period_start,
        period_end=period_end,
    )
    db_session.commit()

    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=datetime(2026, 8, 2, 10, 0, tzinfo=UTC),
            allocations=[
                PaymentAllocationApply(
                    invoice_id=invoice.id,
                    amount=Decimal("1000.00"),
                )
            ],
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    assert invoice.status == InvoiceStatus.paid
    assert invoice.billing_period_start == _utc_naive(period_start)
    assert invoice.billing_period_end == _utc_naive(period_end)
    assert subscription.next_billing_at == _utc_naive(period_end)
    entitlement = (
        db_session.query(ServiceEntitlement)
        .filter(ServiceEntitlement.subscription_id == subscription.id)
        .one()
    )
    assert entitlement.source_invoice_id == invoice.id
    assert entitlement.starts_at == _utc_naive(period_start)
    assert entitlement.ends_at == _utc_naive(period_end)


@pytest.mark.parametrize(
    "subscription_status",
    [
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
    ],
)
def test_canonical_prepaid_funding_renewal_creates_entitlement(
    db_session, subscription_status
):
    from app.services.prepaid_service_renewals import (
        FundingChangeRenewalDisposition,
        apply_due_prepaid_service_after_funding_change,
    )

    subscriber = _make_subscriber(db_session, status=SubscriberStatus.active)
    paid_at = datetime(2026, 8, 5, 14, 30, tzinfo=UTC)
    subscription = _make_subscription(
        db_session,
        subscriber,
        status=subscription_status,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        next_billing_at=paid_at.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    subscription.unit_price = Decimal("1000.00")
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("1000.00"),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("1000.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("1000.00"),
            currency="NGN",
            memo="wallet top-up",
        )
    )
    db_session.commit()

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=paid_at,
        funding_currency="NGN",
        evidence_ref=f"pytest:payment:{payment.id}",
        trigger_payment_id=payment.id,
    )
    assert result.disposition == FundingChangeRenewalDisposition.funded
    db_session.flush()

    db_session.refresh(subscription)
    entitlement = (
        db_session.query(ServiceEntitlement)
        .filter(ServiceEntitlement.subscription_id == subscription.id)
        .one()
    )
    assert entitlement.amount_funded == Decimal("1000.00")
    assert _utc_naive(entitlement.starts_at) == _utc_naive(
        datetime(2026, 8, 5, tzinfo=UTC)
    )
    assert _utc_naive(entitlement.ends_at) == _utc_naive(subscription.next_billing_at)
    event = (
        db_session.query(EventStore)
        .filter_by(
            event_type="prepaid_service.renewed",
            subscription_id=subscription.id,
        )
        .one()
    )
    assert event.payload["source"] == "account_credit"
    assert event.payload["trigger_payment_id"] == str(payment.id)
    assert (
        event.payload["renewed_through"] == datetime(2026, 9, 5, tzinfo=UTC).isoformat()
    )
