from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.prepaid_funding import PrepaidFundingReconstructionBatch
from app.models.splynx_transaction import SplynxBillingTransaction
from app.services.customer_financial_ledger import (
    calculate_customer_balance,
    customer_financial_balances_by_currency,
    list_customer_financial_events,
    preview_paid_prepaid_invoice_consumption,
)
from app.services.customer_financial_position import (
    prepaid_available_balance,
    prepaid_available_balances,
)
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
    verified_prepaid_funding_balance,
)
from app.services.prepaid_service_renewals import (
    confirm_prepaid_service_renewal,
    preview_prepaid_service_renewal,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


def test_canonical_ledger_ignores_archived_mirror_and_uses_native_documents(
    db_session, subscriber
):
    db_session.add_all(
        [
            SplynxBillingTransaction(
                splynx_transaction_id=1,
                splynx_customer_id=1001,
                subscriber_id=subscriber.id,
                entry_type="credit",
                amount=Decimal("100.00"),
                description="Legacy payment",
                transaction_date=date(2026, 3, 1),
            ),
            SplynxBillingTransaction(
                splynx_transaction_id=2,
                splynx_customer_id=1001,
                subscriber_id=subscriber.id,
                entry_type="debit",
                amount=Decimal("40.00"),
                description="Legacy service",
                transaction_date=date(2026, 3, 2),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("999.00"),
                currency="NGN",
                memo="Prepaid opening balance @ cutover",
                effective_date=datetime(2026, 3, 15, tzinfo=UTC),
                affects_customer_position=False,
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("888.00"),
                currency="NGN",
                memo="Correction: remove overcredit",
                effective_date=datetime(2026, 6, 29, tzinfo=UTC),
                affects_customer_position=False,
            ),
            Payment(
                account_id=subscriber.id,
                amount=Decimal("25.00"),
                refunded_amount=Decimal("0.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                paid_at=datetime(2026, 6, 20, tzinfo=UTC),
                memo="Top-up",
            ),
            Invoice(
                account_id=subscriber.id,
                status=InvoiceStatus.issued,
                total=Decimal("10.00"),
                balance_due=Decimal("10.00"),
                currency="NGN",
                issued_at=datetime(2026, 6, 21, tzinfo=UTC),
                memo="Service charge",
                is_proforma=False,
            ),
        ]
    )
    db_session.commit()

    events = list_customer_financial_events(db_session, subscriber.id, currency=None)
    assert [event.memo for event in events] == [
        "Top-up",
        "Service charge",
    ]
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("15.00")
    assert customer_financial_balances_by_currency(db_session, [subscriber.id]) == {
        subscriber.id: {"NGN": Decimal("15.00")}
    }


def test_settlement_net_is_customer_value_not_gross_provider_charge(
    db_session, subscriber
):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("10710.66"),
        provider_fee=Decimal("710.66"),
        refunded_amount=Decimal("0.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=datetime(2026, 8, 2, 20, 18, tzinfo=UTC),
        memo="Deposit Account Credit",
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        PaymentSettlement(
            payment_id=payment.id,
            amount=Decimal("10000.00"),
            unallocated_amount=Decimal("10000.00"),
            prepaid_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.provider_event,
            idempotency_key="customer-value-provider-fee",
        )
    )
    db_session.commit()

    events = list_customer_financial_events(db_session, subscriber.id)

    assert [(event.id, event.amount) for event in events] == [
        (f"payment:{payment.id}", Decimal("10000.00"))
    ]
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("10000.00")
    assert customer_financial_balances_by_currency(db_session, [subscriber.id]) == {
        subscriber.id: {"NGN": Decimal("10000.00")}
    }


def test_settlement_customer_value_applies_refund_once(db_session, subscriber):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("10710.66"),
        provider_fee=Decimal("710.66"),
        refunded_amount=Decimal("2000.00"),
        currency="NGN",
        status=PaymentStatus.partially_refunded,
        paid_at=datetime(2026, 8, 2, 20, 18, tzinfo=UTC),
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add(
        PaymentSettlement(
            payment_id=payment.id,
            amount=Decimal("10000.00"),
            unallocated_amount=Decimal("10000.00"),
            prepaid_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.provider_event,
            idempotency_key="customer-value-provider-fee-refund",
        )
    )
    db_session.commit()

    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("8000.00")
    assert customer_financial_balances_by_currency(db_session, [subscriber.id]) == {
        subscriber.id: {"NGN": Decimal("8000.00")}
    }


def test_legacy_payment_without_settlement_keeps_gross_fallback(db_session, subscriber):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("10710.66"),
        provider_fee=Decimal("710.66"),
        refunded_amount=Decimal("10.66"),
        currency="NGN",
        status=PaymentStatus.partially_refunded,
        paid_at=datetime(2026, 8, 2, 20, 18, tzinfo=UTC),
    )
    db_session.add(payment)
    db_session.commit()

    assert payment.settlement is None
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("10700.00")
    assert customer_financial_balances_by_currency(db_session, [subscriber.id]) == {
        subscriber.id: {"NGN": Decimal("10700.00")}
    }


def test_canonical_ledger_includes_native_real_adjustments_only(db_session, subscriber):
    db_session.add_all(
        [
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("120.00"),
                currency="NGN",
                memo="Approved billing adjustment",
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("50.00"),
                currency="NGN",
                memo="Data repair 2026-06-29: cleanup",
                affects_customer_position=False,
            ),
        ]
    )
    db_session.commit()

    events = list_customer_financial_events(db_session, subscriber.id, currency=None)
    assert [event.memo for event in events] == ["Approved billing adjustment"]
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("120.00")
    assert customer_financial_balances_by_currency(db_session, [subscriber.id]) == {
        subscriber.id: {"NGN": Decimal("120.00")}
    }


def test_reviewed_opening_position_replaces_older_native_projections(
    db_session, subscriber
):
    position_at = datetime(2026, 7, 1, tzinfo=UTC)
    db_session.add_all(
        [
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("999.00"),
                currency="NGN",
                memo="Old native projection",
                effective_date=position_at - timedelta(days=1),
                created_at=position_at - timedelta(days=1),
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("25.00"),
                currency="NGN",
                memo="Post-cutover service adjustment",
                effective_date=position_at + timedelta(days=1),
            ),
        ]
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
        position_at=position_at,
    )

    events = list_customer_financial_events(db_session, subscriber.id)

    assert [event.memo for event in events] == [
        "Reviewed prepaid opening position",
        "Post-cutover service adjustment",
    ]
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("75.00")


def test_reviewed_opening_position_keeps_late_recorded_backdated_money(
    db_session, subscriber
):
    position_at = datetime(2026, 7, 1, tzinfo=UTC)
    recorded_at = position_at + timedelta(days=1)
    occurred_at = position_at - timedelta(days=1)
    db_session.add_all(
        [
            Payment(
                account_id=subscriber.id,
                amount=Decimal("50.00"),
                refunded_amount=Decimal("0.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
                paid_at=occurred_at,
                created_at=recorded_at,
                memo="Late-entered backdated payment",
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("25.00"),
                currency="NGN",
                memo="Late-entered backdated debit",
                effective_date=occurred_at,
                created_at=recorded_at,
            ),
        ]
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
        position_at=position_at,
    )

    events = list_customer_financial_events(db_session, subscriber.id)

    assert {event.memo for event in events} == {
        "Reviewed prepaid opening position",
        "Late-entered backdated payment",
        "Late-entered backdated debit",
    }
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("125.00")
    assert customer_financial_balances_by_currency(
        db_session,
        [subscriber.id],
        start=position_at,
    ) == {subscriber.id: {"NGN": Decimal("25.00")}}
    assert verified_prepaid_funding_balance(db_session, subscriber.id) == Decimal(
        "125.00"
    )


def test_paid_prepaid_invoice_consumes_payment_from_canonical_position(
    db_session, subscriber, subscription
):
    position_at = datetime(2026, 7, 20, 7, 58, 22, tzinfo=UTC)
    paid_at = datetime(2026, 7, 21, 20, 16, 40, tzinfo=UTC)
    period_end = datetime(2026, 8, 21, tzinfo=UTC)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("18812.50"),
        refunded_amount=Decimal("0.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        created_at=paid_at,
        memo="Prepaid renewal payment",
    )
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PREPAID-CONSUMED",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("17500.00"),
        tax_total=Decimal("1312.50"),
        total=Decimal("18812.50"),
        balance_due=Decimal("0.00"),
        billing_period_start=paid_at.replace(hour=0, minute=0, second=0),
        billing_period_end=period_end,
        issued_at=paid_at,
        paid_at=paid_at,
        created_at=paid_at,
    )
    db_session.add_all([payment, invoice])
    db_session.flush()
    db_session.add_all(
        [
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Prepaid base service",
                quantity=Decimal("1.000"),
                unit_price=Decimal("17500.00"),
                amount=Decimal("17500.00"),
                metadata_={"kind": "base_subscription"},
            ),
            PaymentAllocation(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=Decimal("18812.50"),
            ),
        ]
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("0.00"),
        position_at=position_at,
    )

    events = list_customer_financial_events(db_session, subscriber.id)

    assert [event.id.split(":", 1)[0] for event in events] == [
        "prepaid-opening",
        "payment",
        "prepaid-invoice-consumption",
    ]
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("0.00")
    assert verified_prepaid_funding_balance(db_session, subscriber.id) == Decimal(
        "0.00"
    )
    assert customer_financial_balances_by_currency(
        db_session,
        [subscriber.id],
        start=position_at,
    ) == {subscriber.id: {"NGN": Decimal("0.00")}}
    preview = preview_paid_prepaid_invoice_consumption(
        db_session,
        account_ids=(subscriber.id,),
        recorded_after=position_at,
    )
    assert preview.projected_count == 1
    assert preview.already_represented_count == 0
    assert preview.quarantined_count == 0
    assert len(preview.fingerprint) == 64


def test_documentary_paid_invoice_does_not_duplicate_direct_renewal_debit(
    db_session, subscriber, subscription
):
    position_at = datetime(2026, 7, 20, tzinfo=UTC)
    paid_at = datetime(2026, 7, 21, tzinfo=UTC)
    period_end = datetime(2026, 8, 21, tzinfo=UTC)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.add(
        Payment(
            account_id=subscriber.id,
            amount=Decimal("18812.50"),
            refunded_amount=Decimal("0.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=paid_at,
            created_at=paid_at,
        )
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("0.00"),
        position_at=position_at,
    )
    preview = preview_prepaid_service_renewal(
        db_session,
        subscription_id=subscription.id,
        starts_at=paid_at,
        ends_at=period_end,
        amount=Decimal("18812.50"),
        currency="NGN",
    )
    confirm_prepaid_service_renewal(
        db_session,
        preview,
        evidence_ref="pytest:direct-renewal-before-documentary-invoice",
    )
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-DOCUMENTARY-ONLY",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("17500.00"),
        tax_total=Decimal("1312.50"),
        total=Decimal("18812.50"),
        balance_due=Decimal("0.00"),
        billing_period_start=paid_at,
        billing_period_end=period_end,
        issued_at=paid_at,
        paid_at=paid_at,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Documentary prepaid base service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("17500.00"),
            amount=Decimal("17500.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()

    events = list_customer_financial_events(db_session, subscriber.id)

    assert not any(
        event.id == f"prepaid-invoice-consumption:{invoice.id}" for event in events
    )
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("0.00")
    assert verified_prepaid_funding_balance(db_session, subscriber.id) == Decimal(
        "0.00"
    )
    preview = preview_paid_prepaid_invoice_consumption(
        db_session,
        account_ids=(subscriber.id,),
        recorded_after=position_at,
    )
    assert preview.projected_count == 0
    assert preview.already_represented_count == 1
    assert preview.quarantined_count == 0


def test_malformed_paid_prepaid_invoice_is_quarantined_not_projected(
    db_session, subscriber, subscription
):
    position_at = datetime(2026, 7, 20, tzinfo=UTC)
    paid_at = position_at + timedelta(days=1)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        refunded_amount=Decimal("0.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        created_at=paid_at,
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("10.00"),
        billing_period_start=paid_at,
        billing_period_end=paid_at + timedelta(days=30),
        issued_at=paid_at,
        paid_at=paid_at,
        created_at=paid_at,
    )
    db_session.add_all([payment, invoice])
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Malformed prepaid service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("0.00"),
        position_at=position_at,
    )

    preview = preview_paid_prepaid_invoice_consumption(
        db_session,
        account_ids=(subscriber.id,),
        recorded_after=position_at,
    )

    assert preview.projected_count == 0
    assert preview.already_represented_count == 0
    assert preview.quarantined_count == 1
    assert preview.items[0].reason == "paid_invoice_has_balance"
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("100.00")


def test_paid_prepaid_invoice_without_settlement_is_quarantined_not_debited(
    db_session, subscriber, subscription
):
    position_at = datetime(2026, 7, 20, tzinfo=UTC)
    paid_at = position_at + timedelta(days=1)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=paid_at,
        billing_period_end=paid_at + timedelta(days=30),
        issued_at=paid_at,
        paid_at=paid_at,
        created_at=paid_at,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Unsupported paid prepaid service",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            metadata_={"kind": "base_subscription"},
        )
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("0.00"),
        position_at=position_at,
    )

    preview = preview_paid_prepaid_invoice_consumption(
        db_session,
        account_ids=(subscriber.id,),
        recorded_after=position_at,
    )

    assert preview.projected_count == 0
    assert preview.already_represented_count == 0
    assert preview.quarantined_count == 1
    assert preview.items[0].reason == "missing_exact_settlement_evidence"
    assert calculate_customer_balance(db_session, subscriber.id) == Decimal("0.00")


def test_bulk_balance_matches_canonical_multi_currency_refund_rules(
    db_session, subscriber
):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        refunded_amount=Decimal("20.00"),
        currency="USD",
        status=PaymentStatus.partially_refunded,
    )
    db_session.add(payment)
    db_session.flush()
    db_session.add_all(
        [
            Invoice(
                account_id=subscriber.id,
                status=InvoiceStatus.issued,
                total=Decimal("30.00"),
                balance_due=Decimal("30.00"),
                currency="USD",
                is_proforma=False,
            ),
            CreditNote(
                account_id=subscriber.id,
                status=CreditNoteStatus.issued,
                total=Decimal("5.00"),
                currency="USD",
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("200.00"),
                currency="NGN",
                memo="Approved customer credit",
            ),
            # The payment document already represents this refund through
            # refunded_amount, so the linked ledger row must not count twice.
            LedgerEntry(
                account_id=subscriber.id,
                payment_id=payment.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.refund,
                amount=Decimal("20.00"),
                currency="USD",
                memo="Provider refund",
            ),
        ]
    )
    db_session.commit()

    assert customer_financial_balances_by_currency(db_session, [subscriber.id]) == {
        subscriber.id: {
            "NGN": Decimal("200.00"),
            "USD": Decimal("55.00"),
        }
    }
    db_session.query(PrepaidFundingReconstructionBatch).delete()
    db_session.commit()
    with pytest.raises(PrepaidFundingBaselineMissingError, match="cutover"):
        prepaid_available_balance(db_session, subscriber.id)
    with pytest.raises(PrepaidFundingBaselineMissingError, match="cutover"):
        prepaid_available_balances(db_session, [subscriber.id])
