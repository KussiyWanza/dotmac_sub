"""PostgreSQL retry coverage for paid proforma conversion."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditEvent
from app.models.billing import Invoice, InvoiceLine, InvoiceStatus, PaymentAllocation
from app.models.catalog import BillingMode
from app.models.event_store import EventStore
from app.models.subscriber import Reseller, Subscriber
from app.services import invoice_draft_authoring
from app.services.account_credit_deposits import (
    SETTLEMENT_SCOPE,
    AccountCreditDeposits,
    AccountCreditDepositSettlementSource,
    SettleAccountCreditDepositCommand,
)
from app.services.owner_commands import CommandContext
from app.services.topup_intents import TopupIntentChannel


def test_concurrent_conversion_retry_preserves_paid_status(engine) -> None:
    """One retry cannot restore issued after the first request applies credit."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Proforma Conversion {suffix}",
            code=f"proforma-conversion-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Proforma",
            last_name="Concurrency",
            email=f"proforma-conversion-{suffix}@example.com",
            reseller=reseller,
            billing_mode=BillingMode.postpaid,
        )
        setup.add_all([reseller, account])
        setup.commit()

        intent, _preview, _replayed = AccountCreditDeposits.stage_intent(
            setup,
            account_id=account.id,
            amount="10000.00",
            currency="NGN",
            minimum="1000.00",
            maximum="500000.00",
            reference=f"pg-proforma-{suffix}",
            provider_type="paystack",
            provider_id=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=f"pg-proforma-intent-{suffix}",
            channel=TopupIntentChannel.customer_selfcare,
            created_by="pytest",
        )
        setup.commit()
        AccountCreditDeposits.settle_verified(
            setup,
            SettleAccountCreditDepositCommand(
                intent_id=intent.id,
                provider_type="paystack",
                external_transaction_id=f"pg-proforma-payment-{suffix}",
                amount=Decimal("10000.00"),
                currency="NGN",
                provider_intent_id=intent.id,
                source=AccountCreditDepositSettlementSource.customer_gateway_verify,
            ),
            context=CommandContext.system(
                actor="pytest:proforma-conversion",
                scope=SETTLEMENT_SCOPE,
                reason="Create exact credit for proforma conversion retry test",
            ),
        )
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"PF-{suffix}",
            status=InvoiceStatus.draft,
            is_proforma=True,
            currency="NGN",
            subtotal=Decimal("10000.00"),
            total=Decimal("10000.00"),
            balance_due=Decimal("10000.00"),
            memo="[PROFORMA] Concurrent retry",
        )
        setup.add(invoice)
        setup.flush()
        setup.add(
            InvoiceLine(
                invoice_id=invoice.id,
                description="Service",
                quantity=Decimal("1.000"),
                unit_price=Decimal("10000.00"),
                amount=Decimal("10000.00"),
            )
        )
        setup.commit()
        invoice_id = invoice.id

    barrier = Barrier(2)
    idempotency_key = f"proforma-conversion-retry-{suffix}"

    def convert() -> tuple[InvoiceStatus, bool]:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            result = invoice_draft_authoring.convert_proforma_invoice(
                worker,
                invoice_draft_authoring.ConvertProformaInvoiceCommand(
                    invoice_id=invoice_id,
                ),
                context=CommandContext.system(
                    actor="pytest:proforma-conversion",
                    scope="invoice_proforma:convert",
                    reason="Verify concurrent conversion retry",
                    idempotency_key=idempotency_key,
                ),
            )
            return result.status, result.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: convert(), range(2)))

    assert sorted(replayed for _status, replayed in results) == [False, True]
    assert all(status is InvoiceStatus.paid for status, _replayed in results)

    with session_factory() as check:
        invoice = check.get(Invoice, invoice_id)
        allocations = check.scalars(
            select(PaymentAllocation)
            .where(PaymentAllocation.invoice_id == invoice_id)
            .where(PaymentAllocation.is_active.is_(True))
        ).all()
        audits = check.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_id == str(invoice_id))
            .where(AuditEvent.action == "convert_proforma_invoice")
        ).all()
        events = check.scalars(
            select(EventStore)
            .where(EventStore.invoice_id == invoice_id)
            .where(EventStore.event_type == "invoice.sent")
        ).all()

        assert invoice is not None
        assert invoice.is_proforma is False
        assert invoice.status is InvoiceStatus.paid
        assert invoice.balance_due == Decimal("0.00")
        assert len(allocations) == 1
        assert allocations[0].amount == Decimal("10000.00")
        assert len(audits) == 1
        assert len(events) == 1
