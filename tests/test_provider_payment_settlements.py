import hashlib
import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentAllocationReconciliationException,
    PaymentProvider,
    PaymentProviderType,
    PaymentSettlement,
    ServiceEntitlement,
    TopupIntent,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.event_store import EventStore
from app.models.subscriber import SubscriberStatus
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.billing_payment_receipts import (
    get_payment_receipt_context,
    render_receipt_document_html,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.payment_gateway_adapter import (
    PaymentGatewayTransaction,
    payment_gateway_adapter,
)
from app.services.payment_reconciliation import (
    VERIFIED_SETTLEMENT_SCOPE,
    ReconcileVerifiedTopupCommand,
    TopupReconciliationCandidate,
    TopupReconciliationDisposition,
    settle_verified_reconciled_topup,
)
from app.services.provider_payment_settlements import (
    settle_verified_invoice_payment,
)


def _provider(db_session) -> PaymentProvider:
    provider = PaymentProvider(
        name="Paystack cash-first test",
        provider_type=PaymentProviderType.paystack,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)
    return provider


def _invoice(db_session, account_id, *, status: InvoiceStatus):
    issued_at = datetime.now(UTC) if status != InvoiceStatus.draft else None
    return billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=account_id,
            invoice_number=f"INV-CASH-FIRST-{status.value}",
            status=status,
            currency="NGN",
            subtotal=Decimal("1000.00"),
            total=Decimal("1000.00"),
            balance_due=Decimal("1000.00"),
            issued_at=issued_at,
            due_at=(issued_at + timedelta(days=30) if issued_at else None),
            due_date_basis=(
                InvoiceDueDateBasis.contract_terms
                if status != InvoiceStatus.draft
                else None
            ),
            due_date_basis_ref=(
                "test:provider-payment" if status != InvoiceStatus.draft else None
            ),
            due_date_policy_version=(
                "test-v1" if status != InvoiceStatus.draft else None
            ),
        ),
    )


def test_paystack_verification_preserves_gross_fee_and_metadata(
    monkeypatch, db_session
):
    monkeypatch.setattr(
        "app.services.integrations.payment_capability.verify_transaction",
        lambda *_args, **_kwargs: {
            "id": 6364687147,
            "status": "success",
            "amount": 1920051,
            "fees": 38801,
            "currency": "NGN",
            "metadata": {"invoice_id": "invoice-1"},
        },
    )

    transaction = payment_gateway_adapter.verify(
        db_session,
        provider_type="paystack",
        reference="DMAC-INV-111740-482f88f1",
    )

    assert transaction.amount == Decimal("19200.51")
    assert transaction.provider_fee == Decimal("388.01")
    assert transaction.metadata == {"invoice_id": "invoice-1"}


def test_verified_provider_settlement_fingerprints_fit_database_columns(
    db_session, subscriber
):
    provider = _provider(db_session)
    external_id = "paystack-fingerprint-length-1"

    result = billing_service.payments.record_verified_provider_settlement(
        db_session,
        account_id=subscriber.id,
        provider_id=provider.id,
        external_id=external_id,
        gross_amount=Decimal("1020.00"),
        provider_fee=Decimal("20.00"),
        net_amount=Decimal("1000.00"),
        currency="NGN",
        memo="Paystack verified settlement fingerprint test",
    )

    fingerprint = hashlib.sha256(f"{provider.id}:{external_id}".encode()).hexdigest()
    payment_fingerprint_length = (
        Payment.__table__.c.creation_preview_fingerprint.type.length
    )
    settlement_fingerprint_length = (
        PaymentSettlement.__table__.c.preview_fingerprint.type.length
    )
    settlement_key_length = PaymentSettlement.__table__.c.idempotency_key.type.length

    assert result.payment.creation_preview_fingerprint == fingerprint
    assert result.settlement.preview_fingerprint == fingerprint
    assert result.settlement.idempotency_key == f"provider-settlement-{fingerprint}"
    assert len(fingerprint) <= payment_fingerprint_length
    assert len(fingerprint) <= settlement_fingerprint_length
    assert len(result.settlement.idempotency_key) <= settlement_key_length


def test_verified_provider_money_is_recorded_before_successful_allocation(
    monkeypatch, db_session, subscriber
):
    provider = _provider(db_session)
    invoice = _invoice(db_session, subscriber.id, status=InvoiceStatus.issued)
    payments_service = importlib.import_module("app.services.billing.payments")
    monkeypatch.setattr(
        payments_service,
        "calculate_customer_balance",
        lambda *_args, **_kwargs: Decimal("0.00"),
    )

    result = settle_verified_invoice_payment(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        topup_intent_id=None,
        provider_id=provider.id,
        provider_reference="DMAC-INV-CASH-FIRST",
        external_id="paystack-cash-first-1",
        gross_amount=Decimal("1020.00"),
        provider_fee=Decimal("20.00"),
        net_amount=Decimal("1000.00"),
        currency="NGN",
        memo="Paystack verified invoice payment",
    )

    db_session.refresh(invoice)
    payment = result.payment
    settlement = (
        db_session.query(PaymentSettlement).filter_by(payment_id=payment.id).one()
    )
    assert payment.amount == Decimal("1020.00")
    assert payment.provider_fee == Decimal("20.00")
    assert settlement.amount == Decimal("1000.00")
    assert settlement.unallocated_amount == Decimal("1000.00")
    assert result.allocation is not None
    assert result.allocation.amount == Decimal("1000.00")
    assert result.reconciliation_exception is None
    assert invoice.status == InvoiceStatus.paid

    receipt = get_payment_receipt_context(db_session, str(payment.id))
    assert receipt["amount_received"] == Decimal("1020.00")
    assert receipt["amount_credited"] == Decimal("1000.00")
    assert receipt["amount_applied"] == Decimal("1000.00")
    assert receipt["prepaid_amount_applied"] == Decimal("0.00")
    assert receipt["unallocated_credit"] == Decimal("0.00")
    assert receipt["settlement_evidenced"] is True
    rendered = render_receipt_document_html(receipt)
    assert "Amount Credited" in rendered
    assert "₦1,000.00" in rendered
    assert "Account Credit Remaining" in rendered

    replay = settle_verified_invoice_payment(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        topup_intent_id=None,
        provider_id=provider.id,
        provider_reference="DMAC-INV-CASH-FIRST",
        external_id="paystack-cash-first-1",
        gross_amount=Decimal("1020.00"),
        provider_fee=Decimal("20.00"),
        net_amount=Decimal("1000.00"),
        currency="NGN",
        memo="Paystack verified invoice payment",
    )
    assert replay.payment_created is False
    assert (
        db_session.query(Payment).filter_by(external_id="paystack-cash-first-1").count()
        == 1
    )
    assert (
        db_session.query(PaymentAllocation)
        .filter_by(payment_id=payment.id, invoice_id=invoice.id)
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="account_credit.deposited", account_id=subscriber.id)
        .count()
        == 1
    )


def test_allocation_failure_keeps_net_credit_and_one_exception(db_session, subscriber):
    provider = _provider(db_session)
    invoice = _invoice(db_session, subscriber.id, status=InvoiceStatus.draft)

    first = settle_verified_invoice_payment(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        topup_intent_id=None,
        provider_id=provider.id,
        provider_reference="DMAC-INV-DRAFT-FAILURE",
        external_id="paystack-draft-failure-1",
        gross_amount=Decimal("1020.00"),
        provider_fee=Decimal("20.00"),
        net_amount=Decimal("1000.00"),
        currency="NGN",
        memo="Paystack verified draft invoice payment",
    )

    payment = first.payment
    settlement = (
        db_session.query(PaymentSettlement).filter_by(payment_id=payment.id).one()
    )
    exception = first.reconciliation_exception
    assert exception is not None
    assert exception.status == "open"
    assert "draft invoice" in exception.error_message
    assert first.allocation is None
    assert payment.amount == Decimal("1020.00")
    assert payment.provider_fee == Decimal("20.00")
    assert settlement.amount == Decimal("1000.00")
    assert settlement.unallocated_amount == Decimal("1000.00")
    credit = (
        db_session.query(LedgerEntry)
        .filter_by(
            payment_id=payment.id,
            invoice_id=None,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            is_active=True,
        )
        .one()
    )
    assert credit.amount == Decimal("1000.00")

    replay = settle_verified_invoice_payment(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        topup_intent_id=None,
        provider_id=provider.id,
        provider_reference="DMAC-INV-DRAFT-FAILURE",
        external_id="paystack-draft-failure-1",
        gross_amount=Decimal("1020.00"),
        provider_fee=Decimal("20.00"),
        net_amount=Decimal("1000.00"),
        currency="NGN",
        memo="Paystack verified draft invoice payment",
    )
    assert replay.payment_created is False
    assert (
        db_session.query(Payment)
        .filter_by(external_id="paystack-draft-failure-1")
        .count()
        == 1
    )
    assert (
        db_session.query(LedgerEntry)
        .filter_by(
            payment_id=payment.id,
            invoice_id=None,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            is_active=True,
        )
        .count()
        == 1
    )
    exceptions = (
        db_session.query(PaymentAllocationReconciliationException)
        .filter_by(payment_id=payment.id, invoice_id=invoice.id)
        .all()
    )
    assert len(exceptions) == 1
    assert exceptions[0].attempt_count == 2


def test_draft_invoice_failure_renews_due_prepaid_service_from_retained_cash(
    db_session, subscriber, subscription
):
    now = datetime.now(UTC)
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.status = SubscriberStatus.suspended
    subscription.billing_mode = BillingMode.prepaid
    subscription.billing_cycle = BillingCycle.monthly
    subscription.status = SubscriptionStatus.suspended
    subscription.next_billing_at = now - timedelta(days=10)
    subscription.unit_price = Decimal("1000.00")
    subscription.offer.billing_cycle = BillingCycle.monthly
    subscription.offer.is_active = True
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
    lock = EnforcementLock(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        reason=EnforcementReason.prepaid,
        source="pytest:prepaid-balance",
        is_active=True,
    )
    db_session.add(lock)
    db_session.commit()
    provider = _provider(db_session)
    invoice = _invoice(db_session, subscriber.id, status=InvoiceStatus.draft)

    result = settle_verified_invoice_payment(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        topup_intent_id=None,
        provider_id=provider.id,
        provider_reference="DMAC-DRAFT-DUE-PREPAID",
        external_id="paystack-draft-due-prepaid-1",
        gross_amount=Decimal("1020.00"),
        provider_fee=Decimal("20.00"),
        net_amount=Decimal("1000.00"),
        currency="NGN",
        memo="Paystack verified draft invoice payment",
        paid_at=now,
    )

    db_session.refresh(subscription)
    db_session.refresh(lock)
    assert result.allocation is None
    assert result.reconciliation_exception is not None
    assert db_session.query(ServiceEntitlement).count() == 1
    assert subscription.next_billing_at.replace(tzinfo=UTC) > now
    assert subscription.status == SubscriptionStatus.active
    assert lock.is_active is False
    renewal_event = (
        db_session.query(EventStore)
        .filter_by(
            event_type="prepaid_service.renewed",
            account_id=subscriber.id,
            subscription_id=subscription.id,
        )
        .one()
    )
    assert renewal_event.payload["trigger_payment_id"] == str(result.payment.id)
    assert renewal_event.payload["source"] == "account_credit"
    assert renewal_event.payload["renewed_through"] == (
        subscription.next_billing_at.replace(tzinfo=UTC).isoformat()
    )
    receipt = get_payment_receipt_context(db_session, str(result.payment.id))
    assert len(receipt["renewals"]) == 1
    assert receipt["renewals"][0].renewed_through == (
        subscription.next_billing_at.replace(tzinfo=UTC)
    )


def test_recovery_completes_intent_after_cash_recording_when_allocation_fails(
    db_session, subscriber
):
    _provider(db_session)
    invoice = _invoice(db_session, subscriber.id, status=InvoiceStatus.draft)
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="DMAC-INV-RECOVERY-DRAFT",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("1000.00"),
        status="pending",
        metadata_={
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice.id),
        },
    )
    db_session.add(intent)
    db_session.commit()
    db_session.refresh(intent)

    observed_at = datetime.now(UTC)
    command = ReconcileVerifiedTopupCommand(
        candidate=TopupReconciliationCandidate(
            intent_id=intent.id,
            provider_type=PaymentProviderType.paystack,
            reference=intent.reference,
        ),
        transaction=PaymentGatewayTransaction(
            provider_type=PaymentProviderType.paystack.value,
            external_id="paystack-recovery-draft-1",
            amount=Decimal("1020.00"),
            provider_fee=Decimal("20.00"),
            currency="NGN",
            memo_prefix="Paystack",
        ),
        observed_at=observed_at,
    )
    db_session_adapter.release_read_transaction(db_session)
    result = settle_verified_reconciled_topup(
        db_session,
        command,
        context=CommandContext.system(
            actor="pytest",
            scope=VERIFIED_SETTLEMENT_SCOPE,
            reason="Test cash-first reconciliation allocation failure",
        ),
    )

    db_session.refresh(intent)
    payment = (
        db_session.query(Payment)
        .filter_by(external_id="paystack-recovery-draft-1")
        .one()
    )
    exception = (
        db_session.query(PaymentAllocationReconciliationException)
        .filter_by(payment_id=payment.id, invoice_id=invoice.id)
        .one()
    )
    assert result.disposition is TopupReconciliationDisposition.recovered
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id
    assert payment.amount == Decimal("1020.00")
    assert payment.provider_fee == Decimal("20.00")
    assert payment.settlement.amount == Decimal("1000.00")
    assert payment.settlement.unallocated_amount == Decimal("1000.00")
    assert exception.status == "open"
