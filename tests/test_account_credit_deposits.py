from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentProvider,
    PaymentProviderType,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
    TopupIntent,
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
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationInbox,
    IntegrationInstallation,
)
from app.models.subscriber import SubscriberStatus
from app.schemas.billing import InvoiceCreate, PaymentSyncRead
from app.services.account_credit_deposits import (
    SETTLEMENT_SCOPE,
    AccountCreditDeposits,
    AccountCreditDepositSettlementSource,
    ActiveDepositNextAction,
    ActiveDepositPhase,
    DepositEligibilityError,
    SettleAccountCreditDepositCommand,
)
from app.services.billing._common import get_account_credit_balance
from app.services.billing.account_credit import AccountCreditApplications
from app.services.billing.invoices import Invoices
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.topup_intents import (
    DIRECT_TRANSFER_PROVIDER,
    TopupIntentChannel,
    TopupIntentStatus,
)


def _provider(db_session) -> PaymentProvider:
    provider = PaymentProvider(
        name="Deposit Paystack",
        provider_type=PaymentProviderType.paystack,
        is_active=True,
    )
    db_session.add(provider)
    db_session.commit()
    db_session.refresh(provider)
    return provider


def _intent(db_session, subscriber, provider, *, amount="10000.00") -> TopupIntent:
    intent, preview, replayed = AccountCreditDeposits.stage_intent(
        db_session,
        account_id=subscriber.id,
        amount=amount,
        currency="NGN",
        minimum="1000.00",
        maximum="500000.00",
        reference=f"DEP-{subscriber.id.hex[:12]}-{amount}",
        provider_type="paystack",
        provider_id=provider.id,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        idempotency_key=f"account-credit-test-{subscriber.id}-{amount}",
        channel=TopupIntentChannel.customer_selfcare,
        created_by="pytest",
        metadata={},
    )
    db_session.commit()
    db_session.refresh(intent)
    assert not replayed
    assert preview.requested_deposit == Decimal(amount)
    return intent


def _transaction(
    intent,
    *,
    amount=None,
    provider_fee="0.00",
    currency=None,
    external_id="gateway-deposit-1",
    metadata=None,
):
    correlation = (
        intent.id
        if metadata is None
        else _provider_intent_id(metadata.get("topup_intent_id"))
    )
    return SettleAccountCreditDepositCommand(
        intent_id=intent.id,
        provider_type="paystack",
        external_transaction_id=external_id,
        amount=Decimal(amount or str(intent.requested_amount)),
        currency=currency or intent.currency,
        provider_intent_id=correlation,
        source=AccountCreditDepositSettlementSource.customer_gateway_verify,
        provider_fee=Decimal(provider_fee),
    )


def _provider_intent_id(value) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return UUID(int=0)


def _settle(db_session, *, intent_id, transaction):
    assert transaction.intent_id == intent_id
    db_session_adapter.release_read_transaction(db_session)
    result = AccountCreditDeposits.settle_verified(
        db_session,
        transaction,
        context=CommandContext.system(
            actor="pytest:account-credit-deposit",
            scope=SETTLEMENT_SCOPE,
            reason="Account-credit deposit behavior test",
            idempotency_key=f"account-credit-deposit-{intent_id}",
        ),
    )
    payment = db_session.get(Payment, result.payment_id)
    assert payment is not None
    return SimpleNamespace(
        payment=payment,
        application=SimpleNamespace(applied=result.applied_amount),
        already_recorded=result.already_recorded,
        result=result,
    )


def test_intent_persists_typed_server_owned_contract(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    assert intent.purpose == "account_credit_deposit"
    assert intent.allocation_policy == "credit_only"
    assert intent.credit_application_policy == "pay_eligible_invoices"
    assert intent.policy_version == 1
    assert intent.preview_fingerprint and len(intent.preview_fingerprint) == 64
    assert intent.provider_id == provider.id
    assert intent.channel == TopupIntentChannel.customer_selfcare.value


def test_active_request_resolves_gateway_confirmation(
    db_session,
    subscriber,
):
    intent = _intent(db_session, subscriber, _provider(db_session))
    observed_at = datetime.now(UTC)

    active = AccountCreditDeposits.active_request(
        db_session,
        account_id=subscriber.id,
        observed_at=observed_at,
    )

    assert active is not None
    assert active.intent_id == intent.id
    assert active.phase is ActiveDepositPhase.awaiting_provider_confirmation
    assert active.next_action is ActiveDepositNextAction.wait_for_provider
    assert active.observed_at == observed_at


@pytest.mark.parametrize(
    ("status", "expected_phase", "expected_action"),
    [
        (
            TopupIntentStatus.pending,
            ActiveDepositPhase.awaiting_receipt,
            ActiveDepositNextAction.upload_receipt,
        ),
        (
            TopupIntentStatus.submitted,
            ActiveDepositPhase.under_review,
            ActiveDepositNextAction.wait_for_review,
        ),
    ],
)
def test_active_request_resolves_direct_transfer_customer_action(
    db_session,
    subscriber,
    status,
    expected_phase,
    expected_action,
):
    intent = _intent(db_session, subscriber, _provider(db_session))
    intent.provider_type = DIRECT_TRANSFER_PROVIDER
    intent.status = status.value
    db_session.add(intent)
    db_session.commit()

    active = AccountCreditDeposits.active_request(
        db_session,
        account_id=subscriber.id,
    )

    assert active is not None
    assert active.phase is expected_phase
    assert active.next_action is expected_action


def test_active_request_ignores_expired_or_terminal_intents(
    db_session,
    subscriber,
):
    intent = _intent(db_session, subscriber, _provider(db_session))
    intent.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.add(intent)
    db_session.commit()

    assert (
        AccountCreditDeposits.active_request(
            db_session,
            account_id=subscriber.id,
        )
        is None
    )

    intent.expires_at = datetime.now(UTC) + timedelta(minutes=30)
    intent.status = TopupIntentStatus.completed.value
    db_session.add(intent)
    db_session.commit()

    assert (
        AccountCreditDeposits.active_request(
            db_session,
            account_id=subscriber.id,
        )
        is None
    )


def test_preview_applies_partial_deposit_to_existing_invoice(db_session, subscriber):
    provider = _provider(db_session)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("18000.00"),
        balance_due=Decimal("18000.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    preview = AccountCreditDeposits.preview(
        db_session,
        account_id=subscriber.id,
        amount="10000.00",
        currency="NGN",
        minimum="1000.00",
        maximum="500000.00",
    )

    assert preview.eligible_invoice_count == 1
    assert preview.total_applied_to_invoices == Decimal("10000.00")
    assert preview.total_outstanding_after_application == Decimal("8000.00")
    assert preview.remaining_account_credit == Decimal("0.00")
    assert len(preview.invoice_applications) == 1
    assert preview.invoice_applications[0].invoice_id == invoice.id
    assert preview.invoice_applications[0].amount_applied == Decimal("10000.00")
    assert preview.invoice_applications[0].outstanding_after_application == Decimal(
        "8000.00"
    )


def test_account_credit_ignores_non_position_adjustments_but_counts_consumption(
    db_session, subscriber
):
    db_session.add_all(
        [
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("10000.00"),
                currency="NGN",
                memo="Reusable payment credit",
                affects_customer_position=True,
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=Decimal("90000.00"),
                currency="NGN",
                memo="Historical credit projection evidence",
                affects_customer_position=False,
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                amount=Decimal("50000.00"),
                currency="NGN",
                memo="Historical debit projection evidence",
                affects_customer_position=False,
            ),
            LedgerEntry(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.other,
                amount=Decimal("10000.00"),
                currency="NGN",
                memo="Account-credit consumption evidence",
                affects_customer_position=False,
            ),
        ]
    )
    db_session.commit()

    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_confirmed_deposit_is_credit_only_and_grants_no_service(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent),
    )

    assert result.application.applied == Decimal("0.00")
    assert result.payment.settlement is not None
    assert result.payment.settlement.unallocated_amount == Decimal("10000.00")
    assert result.payment.settlement.prepaid_amount == Decimal("0.00")
    assert result.payment.settlement.prepaid_ledger_entry_id is None
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "10000.00"
    )


def test_confirmed_deposit_skips_eligible_prepaid_renewal(db_session, subscriber):
    offer = CatalogOffer(
        name="Deposit Credit Prepaid Plan",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    next_billing_at = datetime(2026, 8, 1)
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        next_billing_at=next_billing_at,
        unit_price=Decimal("1000.00"),
    )
    db_session.add_all(
        [
            subscription,
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("1000.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="1000.00")
    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-prepaid-deposit"),
    )

    db_session.refresh(subscription)
    assert result.payment.settlement is not None
    assert result.payment.settlement.unallocated_amount == Decimal("1000.00")
    assert result.payment.settlement.prepaid_amount == Decimal("0.00")
    assert result.payment.settlement.prepaid_ledger_entry_id is None
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1000.00"
    )
    assert subscription.next_billing_at == next_billing_at
    assert db_session.query(ServiceEntitlement).count() == 0
    assert (
        db_session.query(LedgerEntry)
        .filter(
            LedgerEntry.payment_id == result.payment.id,
            LedgerEntry.entry_type == LedgerEntryType.debit,
            LedgerEntry.source == LedgerSource.invoice,
        )
        .count()
        == 0
    )


def test_confirmed_deposit_partially_pays_existing_invoice(db_session, subscriber):
    provider = _provider(db_session)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-PARTIAL-DEPOSIT",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("18000.00"),
        balance_due=Decimal("18000.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    intent = _intent(db_session, subscriber, provider, amount="10000.00")

    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-partial-existing-debt"),
    )

    db_session.refresh(invoice)
    assert result.application.applied == Decimal("10000.00")
    assert invoice.status == InvoiceStatus.partially_paid
    assert invoice.balance_due == Decimal("8000.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_second_deposit_pays_remaining_invoice_balance(db_session, subscriber):
    provider = _provider(db_session)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-SECOND-DEPOSIT",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("18000.00"),
        balance_due=Decimal("18000.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    first = _intent(db_session, subscriber, provider, amount="10000.00")
    _settle(
        db_session,
        intent_id=first.id,
        transaction=_transaction(first, external_id="gateway-second-first"),
    )
    second = _intent(db_session, subscriber, provider, amount="8000.00")
    result = _settle(
        db_session,
        intent_id=second.id,
        transaction=_transaction(second, external_id="gateway-second-second"),
    )

    db_session.refresh(invoice)
    assert result.application.applied == Decimal("8000.00")
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_exact_deposit_closes_existing_invoice(db_session, subscriber):
    provider = _provider(db_session)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-EXACT-DEPOSIT",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("5000.00"),
        balance_due=Decimal("5000.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    intent = _intent(db_session, subscriber, provider, amount="5000.00")

    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-exact-existing-debt"),
    )

    db_session.refresh(invoice)
    assert result.application.applied == Decimal("5000.00")
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_excess_deposit_retains_exact_remainder_as_credit(db_session, subscriber):
    provider = _provider(db_session)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-EXCESS-DEPOSIT",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("5000.00"),
        balance_due=Decimal("5000.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    intent = _intent(db_session, subscriber, provider, amount="7000.00")

    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-excess-existing-debt"),
    )

    db_session.refresh(invoice)
    assert result.application.applied == Decimal("5000.00")
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "2000.00"
    )


def test_confirmed_deposit_renews_due_suspended_service_before_restoration(
    db_session, subscriber
):
    now = datetime.now(UTC)
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.status = SubscriberStatus.suspended
    offer = CatalogOffer(
        name="Due Deposit Credit Prepaid Plan",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.suspended,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        next_billing_at=now - timedelta(days=10),
        unit_price=Decimal("1000.00"),
    )
    db_session.add_all(
        [
            subscription,
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("1000.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            ),
        ]
    )
    db_session.flush()
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
    intent = _intent(db_session, subscriber, provider, amount="1000.00")
    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-due-prepaid-deposit"),
    )

    db_session.refresh(subscription)
    db_session.refresh(lock)
    assert result.payment.settlement is not None
    assert result.payment.settlement.prepaid_amount == Decimal("0.00")
    assert db_session.query(ServiceEntitlement).count() == 1
    assert subscription.next_billing_at.replace(tzinfo=UTC) > now
    assert subscription.status == SubscriptionStatus.active
    assert lock.is_active is False
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_invoice_created_during_checkout_consumes_confirmed_credit(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="100000.00")
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-RACE",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("60000.00"),
        balance_due=Decimal("60000.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-deposit-race"),
    )

    db_session.refresh(invoice)
    assert result.application.applied == Decimal("60000.00")
    assert invoice.status == InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "40000.00"
    )


def test_two_invoices_consume_one_credit_source_in_oldest_debt_order(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="9000.00")
    older = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-OLDER",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("6000.00"),
        balance_due=Decimal("6000.00"),
        due_at=datetime.now(UTC),
    )
    newer = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-NEWER",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("6000.00"),
        balance_due=Decimal("6000.00"),
        due_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add_all([older, newer])
    db_session.commit()

    _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-two-invoices"),
    )

    db_session.refresh(older)
    db_session.refresh(newer)
    assert older.status == InvoiceStatus.paid
    assert newer.status == InvoiceStatus.partially_paid
    assert newer.balance_due == Decimal("3000.00")


def test_invoice_issued_after_deposit_uses_same_applicator(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="40000.00")
    settlement = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-before-invoice"),
    )

    invoice = Invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            invoice_number="INV-AFTER-DEPOSIT",
            currency="NGN",
            subtotal=Decimal("60000.00"),
            total=Decimal("60000.00"),
            balance_due=Decimal("60000.00"),
            status=InvoiceStatus.issued,
        ),
    )

    assert invoice.status == InvoiceStatus.partially_paid
    assert invoice.balance_due == Decimal("20000.00")
    allocation = (
        db_session.query(PaymentAllocation)
        .filter_by(payment_id=settlement.payment.id, invoice_id=invoice.id)
        .one()
    )
    assert allocation.amount == Decimal("40000.00")
    assert allocation.ledger_entry_id is not None
    assert allocation.consumption_ledger_entry_id is not None


def test_voiding_invoice_releases_applied_account_credit(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")
    settlement = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-before-void"),
    )
    invoice = Invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            invoice_number="INV-VOID-CREDIT",
            currency="NGN",
            subtotal=Decimal("6000.00"),
            total=Decimal("6000.00"),
            balance_due=Decimal("6000.00"),
            status=InvoiceStatus.issued,
        ),
    )
    allocation = (
        db_session.query(PaymentAllocation)
        .filter_by(payment_id=settlement.payment.id, invoice_id=invoice.id)
        .one()
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "4000.00"
    )

    result = Invoices.void_system(
        db_session,
        str(invoice.id),
        reason="Invoice canceled after account-credit application",
        idempotency_key="void-account-credit-allocation-0001",
    )

    db_session.refresh(allocation)
    assert result.invoice.status == InvoiceStatus.void
    assert allocation.is_active is False
    assert len(result.closure.ledger_evidence) == 2
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "10000.00"
    )


def test_draft_invoice_does_not_consume_credit_until_issued(db_session, subscriber):
    provider = _provider(db_session)
    draft = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-DRAFT",
        status=InvoiceStatus.draft,
        currency="NGN",
        total=Decimal("5000.00"),
        balance_due=Decimal("5000.00"),
    )
    db_session.add(draft)
    db_session.commit()
    intent = _intent(db_session, subscriber, provider, amount="5000.00")
    _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-draft"),
    )
    assert (
        not db_session.query(PaymentAllocation).filter_by(invoice_id=draft.id).count()
    )

    Invoices.issue_draft_system(
        db_session,
        str(draft.id),
        issued_at=datetime.now(UTC),
        due_at=datetime.now(UTC),
        reason="test",
        commit=True,
    )
    db_session.refresh(draft)
    assert draft.status == InvoiceStatus.paid


def test_ineligible_invoice_states_and_currency_consume_nothing(db_session, subscriber):
    provider = _provider(db_session)
    invoices = [
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-DRAFT-SKIP",
            status=InvoiceStatus.draft,
            currency="NGN",
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
        ),
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-VOID-SKIP",
            status=InvoiceStatus.void,
            currency="NGN",
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
        ),
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-WRITEOFF-SKIP",
            status=InvoiceStatus.written_off,
            currency="NGN",
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
        ),
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-INACTIVE-SKIP",
            status=InvoiceStatus.issued,
            currency="NGN",
            total=Decimal("5000.00"),
            balance_due=Decimal("5000.00"),
            is_active=False,
        ),
        Invoice(
            account_id=subscriber.id,
            invoice_number="INV-USD-SKIP",
            status=InvoiceStatus.issued,
            currency="USD",
            total=Decimal("5.00"),
            balance_due=Decimal("5.00"),
        ),
    ]
    db_session.add_all(invoices)
    db_session.commit()
    intent = _intent(db_session, subscriber, provider, amount="5000.00")
    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-skip-ineligible"),
    )

    assert result.application.applied == Decimal("0.00")
    assert db_session.query(PaymentAllocation).count() == 0
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "5000.00"
    )


def test_duplicate_confirmation_returns_same_payment(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    transaction = _transaction(intent, external_id="gateway-idempotent")

    first = _settle(db_session, intent_id=intent.id, transaction=transaction)
    second = _settle(db_session, intent_id=intent.id, transaction=transaction)

    assert second.already_recorded
    assert second.payment.id == first.payment.id
    assert (
        db_session.query(Payment).filter_by(external_id="gateway-idempotent").count()
        == 1
    )


def test_provider_gross_including_fee_settles_authorized_deposit(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="10000.00")

    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(
            intent,
            amount="10175.00",
            provider_fee="175.00",
            external_id="gateway-gross-including-fee",
        ),
    )

    assert result.payment.amount == Decimal("10175.00")
    assert result.payment.provider_fee == Decimal("175.00")
    assert result.payment.settlement.amount == Decimal("10000.00")
    assert result.payment.settlement.unallocated_amount == Decimal("10000.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "10000.00"
    )


def test_provider_amount_mismatch_posts_no_money(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    with pytest.raises(DepositEligibilityError) as exc_info:
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, amount="9999.00"),
        )

    assert exc_info.value.code == "deposit_amount_mismatch"
    assert db_session.query(Payment).count() == 0


def test_provider_currency_mismatch_posts_no_money(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    with pytest.raises(DepositEligibilityError) as exc_info:
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, currency="USD"),
        )

    assert exc_info.value.code == "deposit_currency_mismatch"
    assert db_session.query(Payment).count() == 0


def test_provider_correlation_mismatch_posts_no_money(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)

    with pytest.raises(DepositEligibilityError) as exc_info:
        _settle(
            db_session,
            intent_id=intent.id,
            transaction=_transaction(intent, metadata={}),
        )

    assert exc_info.value.code == "deposit_provider_correlation_mismatch"
    assert db_session.query(Payment).count() == 0


def test_settlement_rolls_back_all_evidence_when_event_staging_fails(
    db_session, subscriber, monkeypatch
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    intent_id = intent.id

    def fail_event(*args, **kwargs):
        raise RuntimeError("event staging failed")

    monkeypatch.setattr(
        "app.services.account_credit_deposits.emit_event",
        fail_event,
    )

    with pytest.raises(RuntimeError, match="event staging failed"):
        _settle(
            db_session,
            intent_id=intent_id,
            transaction=_transaction(intent, external_id="gateway-event-failure"),
        )

    persisted_intent = db_session.get(TopupIntent, intent_id)
    assert persisted_intent is not None
    assert persisted_intent.completed_payment_id is None
    assert db_session.query(Payment).count() == 0


def test_disabled_account_cannot_create_deposit(db_session, subscriber):
    provider = _provider(db_session)
    subscriber.status = SubscriberStatus.disabled
    db_session.commit()

    with pytest.raises(DepositEligibilityError) as exc_info:
        _intent(db_session, subscriber, provider)

    assert exc_info.value.code == "deposit_account_inactive"


def test_erp_payment_projection_carries_deposit_policy_and_settlement(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    result = _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-erp"),
    )

    projected = PaymentSyncRead.model_validate(result.payment)
    assert projected.intent_purpose == "account_credit_deposit"
    assert projected.allocation_policy == "credit_only"
    assert projected.credit_application_policy == "pay_eligible_invoices"
    assert projected.policy_version == 1
    assert projected.settlement is not None
    assert projected.settlement.unallocated_amount == Decimal("10000.00")


def test_invariant_monitor_ignores_incompatible_currency_credit(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-monitor"),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="USD",
        total=Decimal("1.00"),
        balance_due=Decimal("1.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    # Same-currency credit is required, so this mismatch is not an anomaly.
    assert not AccountCreditApplications.inspect_invariants(
        db_session, str(subscriber.id)
    )


def test_invariant_monitor_reports_payable_invoice_with_unused_credit(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider)
    _settle(
        db_session,
        intent_id=intent.id,
        transaction=_transaction(intent, external_id="gateway-monitor-positive"),
    )
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.issued,
            currency="NGN",
            total=Decimal("1.00"),
            balance_due=Decimal("1.00"),
        )
    )
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(
        db_session, str(subscriber.id)
    )

    assert [item.code for item in violations] == ["eligible_invoice_with_unused_credit"]
    summary = AccountCreditApplications.summarize_invariants(db_session)
    assert summary.eligible_invoice_with_unused_credit == 1
    assert summary.total == len(violations)


def test_invariant_monitor_reports_paid_invoice_without_settlement_evidence(
    db_session, subscriber
):
    db_session.add(
        Invoice(
            account_id=subscriber.id,
            status=InvoiceStatus.paid,
            currency="NGN",
            total=Decimal("5000.00"),
            balance_due=Decimal("0.00"),
        )
    )
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(
        db_session, str(subscriber.id)
    )

    assert [item.code for item in violations] == ["paid_invoice_underfunded"]
    summary = AccountCreditApplications.summarize_invariants(db_session)
    assert summary.paid_invoice_underfunded == 1
    assert summary.total == len(violations)


def test_invariant_summary_matches_payment_capacity_violations(db_session, subscriber):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        status=PaymentStatus.succeeded,
        paid_at=datetime.now(UTC),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        total=Decimal("125.00"),
        balance_due=Decimal("0.00"),
    )
    db_session.add_all([payment, invoice])
    db_session.flush()
    consumption = LedgerEntry(
        account_id=subscriber.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.payment,
        amount=Decimal("125.00"),
        currency="NGN",
    )
    db_session.add(consumption)
    db_session.flush()
    db_session.add_all(
        [
            PaymentSettlement(
                payment_id=payment.id,
                amount=Decimal("100.00"),
                unallocated_amount=Decimal("100.00"),
                prepaid_amount=Decimal("0.00"),
                currency="NGN",
                origin=PaymentSettlementOrigin.system,
                idempotency_key="invariant-summary-capacity",
            ),
            PaymentAllocation(
                payment_id=payment.id,
                invoice_id=invoice.id,
                consumption_ledger_entry_id=consumption.id,
                amount=Decimal("125.00"),
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(db_session)
    summary = AccountCreditApplications.summarize_invariants(db_session)

    assert [item.code for item in violations] == [
        "payment_overallocated",
        "negative_payment_credit_source_availability",
    ]
    assert summary.payment_overallocated == 1
    assert summary.negative_payment_credit_source_availability == 1
    assert summary.total == len(violations)


def test_invariant_summary_matches_partial_refund_settlement(db_session, subscriber):
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("100.00"),
        refunded_amount=Decimal("25.00"),
        status=PaymentStatus.partially_refunded,
        paid_at=datetime.now(UTC),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.paid,
        currency="NGN",
        total=Decimal("75.00"),
        balance_due=Decimal("0.00"),
    )
    db_session.add_all([payment, invoice])
    db_session.flush()
    db_session.add(
        PaymentAllocation(
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=Decimal("100.00"),
            is_active=True,
        )
    )
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(db_session)
    summary = AccountCreditApplications.summarize_invariants(db_session)

    assert violations == []
    assert summary.paid_invoice_underfunded == 0
    assert summary.total == 0


def test_invariant_summary_matches_completed_deposit_without_payment(
    db_session, subscriber
):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="1000.00")
    intent.status = "completed"
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(db_session)
    summary = AccountCreditApplications.summarize_invariants(db_session)

    assert [item.code for item in violations] == [
        "settled_deposit_without_exact_payment"
    ]
    assert summary.settled_deposit_without_exact_payment == 1
    assert summary.total == len(violations)


def test_invariant_summary_matches_unresolved_deposit_webhook(db_session, subscriber):
    provider = _provider(db_session)
    intent = _intent(db_session, subscriber, provider, amount="1000.00")
    installation = IntegrationInstallation(
        connector_key="paystack",
        connector_version="1.0.0",
        manifest_digest="a" * 64,
        name="Paystack invariant test",
        environment="test",
        state="enabled",
    )
    db_session.add(installation)
    db_session.flush()
    binding = IntegrationCapabilityBinding(
        installation_id=installation.id,
        capability_id="payments.webhook.v1",
        state="enabled",
    )
    db_session.add(binding)
    db_session.flush()
    db_session.add(
        IntegrationInbox(
            installation_id=installation.id,
            capability_binding_id=binding.id,
            provider_event_id="unresolved-deposit-event",
            event_type="charge.success",
            payload_digest="b" * 64,
            payload_json={"data": {"metadata": {"topup_intent_id": str(intent.id)}}},
            state="retryable",
        )
    )
    db_session.commit()

    violations = AccountCreditApplications.inspect_invariants(db_session)
    summary = AccountCreditApplications.summarize_invariants(db_session)

    assert [item.code for item in violations] == ["deposit_webhook_unresolved"]
    assert summary.deposit_webhook_unresolved == 1
    assert summary.total == len(violations)


def test_duplicate_provider_reference_is_unreachable_by_constraint(
    db_session, subscriber
):
    """The seventh invariant has no equivalence test because it cannot happen.

    `uq_payments_active_external_id` is a unique index over
    `(provider_id, external_id)`, on PostgreSQL partial with predicate
    `is_active AND provider_id IS NOT NULL AND external_id IS NOT NULL` --
    exactly the rows `duplicate_provider_reference` inspects. The database
    refuses the state the invariant looks for, so both the forensic scan and
    the aggregate can only ever report zero, and neither can be exercised
    against a real duplicate.

    Pin the constraint instead. If it is ever dropped or narrowed, this fails
    and the invariant becomes live -- at which point it needs the equivalence
    test that cannot be written today.
    """
    provider = _provider(db_session)
    db_session.add(
        Payment(
            account_id=subscriber.id,
            amount=Decimal("500.00"),
            status=PaymentStatus.succeeded,
            paid_at=datetime.now(UTC),
            provider_id=provider.id,
            external_id="duplicate-provider-reference",
        )
    )
    db_session.commit()

    db_session.add(
        Payment(
            account_id=subscriber.id,
            amount=Decimal("500.00"),
            status=PaymentStatus.succeeded,
            paid_at=datetime.now(UTC),
            provider_id=provider.id,
            external_id="duplicate-provider-reference",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    summary = AccountCreditApplications.summarize_invariants(db_session)
    violations = AccountCreditApplications.inspect_invariants(db_session)
    assert summary.duplicate_provider_reference == 0
    assert [item.code for item in violations] == []
    assert summary.total == len(violations)


def test_invariant_summary_query_count_is_bounded(db_session, subscriber):
    for index in range(25):
        db_session.add(
            Payment(
                account_id=subscriber.id,
                amount=Decimal("100.00"),
                status=PaymentStatus.succeeded,
                paid_at=datetime.now(UTC),
                external_id=f"bounded-payment-{index}",
            )
        )
        db_session.add(
            Invoice(
                account_id=subscriber.id,
                status=InvoiceStatus.paid,
                currency="NGN",
                total=Decimal("100.00"),
                balance_due=Decimal("0.00"),
            )
        )
    db_session.commit()

    statements: list[str] = []

    def capture(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        summary = AccountCreditApplications.summarize_invariants(db_session)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert summary.paid_invoice_underfunded == 25
    assert summary.total == 25
    # Full-fleet health uses a fixed set of aggregate reads instead of one or
    # more relationship/settlement queries for every payment and invoice.
    assert len(statements) <= 8
