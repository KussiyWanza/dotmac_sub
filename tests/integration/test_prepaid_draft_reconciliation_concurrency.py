"""PostgreSQL serialization coverage for mixed-source prepaid reconciliation."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
    PrepaidOpeningFundingConsumption,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    ReconcilePrepaidDraftCommand,
    preview_prepaid_draft_reconciliation,
    reconcile_prepaid_draft_invoice,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


def test_concurrent_opening_funding_confirmations_converge_on_one_consumption(
    engine,
) -> None:
    """The account and baseline locks make the second confirmation a replay."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    effective_at = datetime.now(UTC).replace(microsecond=0)
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Opening Funding {suffix}",
            code=f"opening-funding-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Opening",
            last_name="Concurrency",
            email=f"opening-concurrency-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Opening Offer {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
        )
        setup.add_all([reseller, account, offer])
        setup.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            unit_price=Decimal("100.00"),
            next_billing_at=effective_at - timedelta(days=1),
        )
        setup.add(subscription)
        setup.flush()
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"INV-OPENING-{suffix}",
            status=InvoiceStatus.draft,
            currency="NGN",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            billing_period_start=effective_at - timedelta(days=1),
            billing_period_end=effective_at + timedelta(days=29),
            is_proforma=False,
            is_active=True,
        )
        setup.add(invoice)
        setup.flush()
        setup.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Prepaid service",
                quantity=Decimal("1.000"),
                unit_price=Decimal("100.00"),
                amount=Decimal("100.00"),
                is_active=True,
            )
        )
        setup.commit()
        materialize_test_prepaid_opening_balance(
            setup,
            account.id,
            Decimal("100.00"),
            position_at=effective_at - timedelta(days=2),
        )
        preview = preview_prepaid_draft_reconciliation(setup, invoice.id)
        command = ReconcilePrepaidDraftCommand(
            context=CommandContext.system(
                actor="pytest:billing-operator",
                scope="prepaid_draft_reconciliation",
                reason="Concurrent reviewed opening-funding reconciliation",
                idempotency_key=f"pg-opening-reconciliation-{suffix}",
            ),
            invoice_id=invoice.id,
            preview_fingerprint=preview.fingerprint,
            effective_at=effective_at,
        )
        setup.commit()
        account_id = account.id
        invoice_id = invoice.id

    try:
        barrier = Barrier(2)

        def reconcile() -> tuple[uuid.UUID | None, bool]:
            with session_factory() as worker:
                barrier.wait(timeout=10)
                result = reconcile_prepaid_draft_invoice(worker, command)
                return result.opening_funding_consumption_id, result.replayed

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: reconcile(), range(2)))

        assert len({consumption_id for consumption_id, _replayed in results}) == 1
        assert sorted(replayed for _consumption_id, replayed in results) == [
            False,
            True,
        ]
        with session_factory() as check:
            invoice = check.get(Invoice, invoice_id)
            assert invoice is not None
            assert invoice.status is InvoiceStatus.paid
            assert invoice.balance_due == Decimal("0.00")
            assert (
                check.query(PrepaidOpeningFundingConsumption)
                .filter_by(invoice_id=invoice_id)
                .count()
                == 1
            )
    finally:
        # This test needs independent committed sessions to exercise PostgreSQL
        # row locks. Remove its one-time authority record so the session-scoped
        # integration database retains the same empty native-install state for
        # the tests that follow.
        with session_factory() as cleanup:
            baseline = (
                cleanup.query(PrepaidFundingBaseline)
                .filter_by(account_id=account_id)
                .one_or_none()
            )
            cleanup.query(PrepaidOpeningFundingConsumption).filter_by(
                invoice_id=invoice_id
            ).delete(synchronize_session=False)
            if baseline is not None:
                batch_id = baseline.batch_id
                cleanup.delete(baseline)
                cleanup.flush()
                cleanup.query(PrepaidFundingReconstructionBatch).filter_by(
                    id=batch_id
                ).delete(synchronize_session=False)
            cleanup.commit()
