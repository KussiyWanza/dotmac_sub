"""Golden lifecycle characterization pins (platform adoption, Phase 1).

Pins the DB-row outcomes of three critical money/service flows so later
refactors — and eventually kernel adoption — cannot drift them silently:

1. Invoice settlement: allocation row, its linked ledger credit, the
   PaymentSettlement record, and invoice arithmetic.
2. Prepaid payment -> canonical renewal: settlement records money only, then
   the renewal owner writes the debit, entitlement, and outcome evidence.
3. Suspend/restore: end-to-end SubscriptionLifecycleEvent rows and
   EnforcementLock forensics from the real service calls (emit=True).

Deliberately complements — never duplicates — the assertions in
tests/test_account_lifecycle.py (statuses, return values, event payloads via
monkeypatched emit), tests/services/billing/test_payment_status_recompute.py
(invoice status transitions, ServiceEntitlement rows), and
tests/test_invoice_settlement_restores_service.py (lock lifting): this module
pins the ROW SHAPES those flows write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
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
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.event_store import EventStore
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.billing import PaymentAllocationApply, PaymentCreate
from app.services.account_lifecycle import (
    restore_subscription,
    suspend_subscription,
)
from app.services.billing._common import get_account_credit_balance
from app.services.billing.payments import Payments
from app.services.events.handlers.prepaid_renewal import PrepaidRenewalHandler
from app.services.events.types import Event, EventType


def _make_subscriber(db: Session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Golden",
        last_name="Scenario",
        email=f"golden-{uuid.uuid4().hex[:8]}@example.com",
        status=SubscriberStatus.active,
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _make_offer(db: Session) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Golden Offer {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        status=OfferStatus.active,
        is_active=True,
    )
    db.add(offer)
    db.flush()
    return offer


def _make_subscription(
    db: Session, subscriber: Subscriber, offer: CatalogOffer, **kwargs
) -> Subscription:
    defaults = {
        "subscriber_id": subscriber.id,
        "offer_id": offer.id,
        "status": SubscriptionStatus.active,
        "billing_mode": BillingMode.prepaid,
    }
    defaults.update(kwargs)
    subscription = Subscription(**defaults)
    db.add(subscription)
    db.flush()
    return subscription


def _make_invoice(db: Session, subscriber: Subscriber, total: str) -> Invoice:
    amount = Decimal(total)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"GOLD-{uuid.uuid4().hex[:10]}",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=amount,
        tax_total=Decimal("0.00"),
        total=amount,
        balance_due=amount,
        issued_at=datetime(2026, 8, 1, tzinfo=UTC),
        due_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    db.add(invoice)
    db.flush()
    return invoice


def _settlement(db: Session, payment_id) -> PaymentSettlement:
    return (
        db.query(PaymentSettlement)
        .filter(PaymentSettlement.payment_id == payment_id)
        .one()
    )


def _created_payment(db: Session, subscriber: Subscriber) -> Payment:
    return (
        db.query(Payment)
        .filter(Payment.account_id == subscriber.id)
        .order_by(Payment.created_at.desc())
        .first()
    )


class TestInvoiceSettlementRowShapes:
    def test_full_settlement_pins_allocation_ledger_and_settlement(
        self, db_session
    ) -> None:
        subscriber = _make_subscriber(db_session)
        invoice = _make_invoice(db_session, subscriber, "100.00")
        db_session.commit()

        Payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=invoice.id, amount=Decimal("100.00")
                    )
                ],
            ),
        )
        payment = _created_payment(db_session, subscriber)

        allocation = (
            db_session.query(PaymentAllocation)
            .filter(PaymentAllocation.payment_id == payment.id)
            .one()
        )
        assert allocation.invoice_id == invoice.id
        assert allocation.amount == Decimal("100.00")
        assert allocation.is_active is True
        assert allocation.ledger_entry_id is not None

        ledger = db_session.get(LedgerEntry, allocation.ledger_entry_id)
        assert ledger.entry_type == LedgerEntryType.credit
        assert ledger.source == LedgerSource.payment
        assert ledger.account_id == subscriber.id
        assert ledger.invoice_id == invoice.id
        assert ledger.amount == Decimal("100.00")
        assert ledger.is_active is True

        settlement = _settlement(db_session, payment.id)
        assert settlement.amount == Decimal("100.00")
        assert settlement.unallocated_amount == Decimal("0.00")
        assert settlement.prepaid_amount == Decimal("0.00")

        db_session.refresh(invoice)
        assert invoice.status == InvoiceStatus.paid
        assert invoice.balance_due == Decimal("0.00")
        assert invoice.paid_at is not None
        assert get_account_credit_balance(db_session, subscriber.id) == Decimal("0.00")

    def test_overpayment_posts_unallocated_wallet_credit(self, db_session) -> None:
        subscriber = _make_subscriber(db_session)
        invoice = _make_invoice(db_session, subscriber, "60.00")
        db_session.commit()

        Payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=invoice.id, amount=Decimal("60.00")
                    )
                ],
            ),
        )
        payment = _created_payment(db_session, subscriber)

        settlement = _settlement(db_session, payment.id)
        assert settlement.unallocated_amount == Decimal("40.00")
        assert settlement.prepaid_amount == Decimal("0.00")
        assert settlement.unallocated_ledger_entry_id is not None

        credit = db_session.get(LedgerEntry, settlement.unallocated_ledger_entry_id)
        assert credit.entry_type == LedgerEntryType.credit
        assert credit.invoice_id is None
        assert credit.amount == Decimal("40.00")

        assert get_account_credit_balance(db_session, subscriber.id) == Decimal("40.00")


class TestPrepaidTopupRenewalRowShapes:
    def test_topup_renews_service_and_pins_debit_shape(self, db_session) -> None:
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        paid_at = datetime(2026, 8, 5, 14, 30, tzinfo=UTC)
        subscription = _make_subscription(
            db_session,
            subscriber,
            offer,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            next_billing_at=paid_at.replace(hour=0, minute=0, second=0, microsecond=0),
        )
        subscription.unit_price = Decimal("1000.00")
        db_session.add(
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("1000.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
        db_session.commit()

        Payments.create(
            db_session,
            PaymentCreate(
                account_id=subscriber.id,
                amount=Decimal("1500.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                paid_at=paid_at,
            ),
        )
        payment = _created_payment(db_session, subscriber)

        settlement = _settlement(db_session, payment.id)
        assert settlement.prepaid_amount == Decimal("0.00")
        assert settlement.prepaid_ledger_entry_id is None
        assert settlement.unallocated_amount == Decimal("1500.00")

        funding_event = Event(
            event_type=EventType.payment_received,
            payload={"payment_id": str(payment.id)},
            account_id=subscriber.id,
        )
        handler = PrepaidRenewalHandler()
        handler.handle(db_session, funding_event)
        handler.handle(db_session, funding_event)
        db_session.commit()

        application = Payments.application_summary(db_session, payment)
        assert application.amount_received == Decimal("1500.00")
        assert application.amount_credited == Decimal("1500.00")
        assert application.invoice_amount_applied == Decimal("1000.00")
        assert application.prepaid_amount_applied == Decimal("0.00")
        assert application.unallocated_credit == Decimal("500.00")

        entitlement = db_session.query(ServiceEntitlement).one()
        outcome = (
            db_session.query(EventStore)
            .filter_by(
                event_type=EventType.prepaid_service_renewed.value,
                subscription_id=subscription.id,
            )
            .one()
        )
        assert outcome.payload["trigger_payment_id"] == str(payment.id)
        assert entitlement.source_ledger_entry_id is None
        assert entitlement.source_invoice_id is not None
        assert entitlement.source_invoice_line_id is not None
        invoice = db_session.get(Invoice, entitlement.source_invoice_id)
        assert invoice is not None
        allocation = (
            db_session.query(PaymentAllocation).filter_by(invoice_id=invoice.id).one()
        )
        assert invoice.status is InvoiceStatus.paid
        assert invoice.balance_due == Decimal("0.00")
        assert allocation.amount == Decimal("1000.00")

        # Wallet invariant: 1500 top-up credit minus the 1000 renewal debit.
        assert get_account_credit_balance(db_session, subscriber.id) == Decimal(
            "500.00"
        )


class TestSuspendRestoreRowShapes:
    def test_suspend_restore_writes_event_rows_and_lock_forensics(
        self, db_session
    ) -> None:
        subscriber = _make_subscriber(db_session)
        offer = _make_offer(db_session)
        subscription = _make_subscription(db_session, subscriber, offer)
        db_session.commit()

        lock = suspend_subscription(
            db_session,
            str(subscription.id),
            EnforcementReason.overdue,
            source="golden-scenario",
        )
        assert lock.access_mode == AccessRestrictionMode.hard_reject
        assert lock.source == "golden-scenario"
        assert lock.is_active is True
        assert lock.resolved_at is None
        db_session.commit()

        suspend_events = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(
                SubscriptionLifecycleEvent.subscription_id == subscription.id,
                SubscriptionLifecycleEvent.event_type == LifecycleEventType.suspend,
            )
            .all()
        )
        assert len(suspend_events) == 1
        assert suspend_events[0].from_status == SubscriptionStatus.active
        assert suspend_events[0].to_status == SubscriptionStatus.suspended

        restored = restore_subscription(
            db_session,
            str(subscription.id),
            trigger="admin",
            resolved_by="golden-tester",
        )
        assert restored is True
        db_session.commit()

        db_session.refresh(lock)
        assert lock.is_active is False
        assert lock.resolved_at is not None
        assert lock.resolved_by == "golden-tester"

        resume_events = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(
                SubscriptionLifecycleEvent.subscription_id == subscription.id,
                SubscriptionLifecycleEvent.event_type == LifecycleEventType.resume,
            )
            .all()
        )
        assert len(resume_events) == 1
        assert resume_events[0].from_status == SubscriptionStatus.suspended
        assert resume_events[0].to_status == SubscriptionStatus.active

        active_locks = (
            db_session.query(EnforcementLock)
            .filter(
                EnforcementLock.subscription_id == subscription.id,
                EnforcementLock.is_active.is_(True),
            )
            .count()
        )
        assert active_locks == 0
