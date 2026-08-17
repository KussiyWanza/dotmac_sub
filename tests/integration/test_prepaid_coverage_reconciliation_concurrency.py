"""PostgreSQL serialization coverage for prepaid evidence reconciliation."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    ServiceEntitlement,
)
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.prepaid_coverage import (
    PrepaidCoverageReconciliationItem,
    PrepaidCoverageReconciliationRun,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services.owner_commands import CommandContext
from app.services.prepaid_coverage_reconciliation import (
    ReconcilePrepaidCoverageCommand,
    preview_prepaid_coverage_reconciliation,
    reconcile_prepaid_service_coverage,
)


def test_two_confirmations_converge_on_one_reconciliation_run(engine) -> None:
    """The account lock makes concurrent use of one key a true replay."""
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    as_of = datetime.now(UTC).replace(microsecond=0)
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Coverage Reconciliation {suffix}",
            code=f"coverage-reconciliation-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Coverage",
            last_name="Concurrency",
            email=f"coverage-concurrency-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Coverage Offer {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
        )
        setup.add_all([reseller, account, offer])
        setup.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            unit_price=Decimal("35000.00"),
            start_at=as_of - timedelta(days=1),
            next_billing_at=as_of + timedelta(days=30),
        )
        setup.add(subscription)
        setup.flush()
        invoice = Invoice(
            account_id=account.id,
            status=InvoiceStatus.paid,
            currency="NGN",
            subtotal=Decimal("35000.00"),
            total=Decimal("35000.00"),
            balance_due=Decimal("0.00"),
            billing_period_start=as_of - timedelta(days=1),
            billing_period_end=as_of + timedelta(days=30),
            issued_at=as_of - timedelta(days=1),
            paid_at=as_of - timedelta(days=1),
        )
        setup.add(invoice)
        setup.flush()
        setup.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Base service",
                quantity=Decimal("1.000"),
                unit_price=Decimal("35000.00"),
                amount=Decimal("35000.00"),
                metadata_={"kind": "base_subscription"},
            )
        )
        setup.commit()
        preview = preview_prepaid_coverage_reconciliation(
            setup,
            as_of=as_of,
            subscription_ids=(subscription.id,),
        )
        command = ReconcilePrepaidCoverageCommand(
            context=CommandContext.system(
                actor="pytest",
                scope="prepaid_service_coverage",
                reason="Concurrent reviewed coverage reconciliation evidence",
                idempotency_key=f"pg-coverage-reconciliation-{suffix}",
            ),
            as_of=preview.as_of,
            preview_fingerprint=preview.fingerprint,
            subscription_ids=preview.subscription_ids,
        )
        setup.commit()
        subscription_id = subscription.id

    barrier = Barrier(2)

    def reconcile() -> tuple[uuid.UUID, bool]:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            result = reconcile_prepaid_service_coverage(worker, command)
            return result.run_id, result.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reconcile(), range(2)))

    assert len({run_id for run_id, _replayed in results}) == 1
    assert sorted(replayed for _run_id, replayed in results) == [False, True]
    with session_factory() as check:
        assert (
            check.query(PrepaidCoverageReconciliationRun)
            .filter_by(idempotency_key=f"pg-coverage-reconciliation-{suffix}")
            .count()
            == 1
        )
        assert (
            check.query(PrepaidCoverageReconciliationItem)
            .filter_by(subscription_id=subscription_id)
            .count()
            == 1
        )
        assert (
            check.query(ServiceEntitlement)
            .filter_by(subscription_id=subscription_id)
            .count()
            == 1
        )
