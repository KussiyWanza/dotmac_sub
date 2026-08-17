"""Webhook-driven settlement: a signed provider event must actually move money.

A customer who pays at the gateway and never returns to the verify URL
(abandoned webview, dropped redirect) is settled by the webhook alone. These
tests pin that the webhook creates exactly one payment, marks the invoice
paid, credits top-ups, refuses to double-credit a transaction the verify path
already recorded, and that the reconciliation sweep recovers stranded intents.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentAllocationReconciliationException,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.integration_platform import IntegrationInbox
from app.models.subscription_engine import SettingValueType
from app.schemas.billing import InvoiceCreate, PaymentCreate
from app.services import billing as billing_service
from app.services.api_billing_webhooks import (
    process_flutterwave_webhook,
    process_paystack_webhook,
)
from app.services.billing_enforcement_guards import payment_channel_health
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations.payment_capability import PaymentCapabilityError
from app.services.owner_commands import CommandContext
from app.services.payment_gateway_adapter import PaymentGatewayTransaction
from app.services.payment_reconciliation import (
    RECONCILIATION_SCOPE,
    RunTopupReconciliationCommand,
    reconcile_pending_topups,
    topup_reconciliation_backlog,
)
from app.services.settings_cache import SettingsCache
from app.services.settings_spec import get_spec
from tests.integration_platform_helpers import enable_payment_provider


@pytest.fixture(autouse=True)
def _payment_capabilities(db_session, monkeypatch):
    monkeypatch.setenv("PAYSTACK_TEST_SECRET", "sk_test_webhook_secret")
    monkeypatch.setenv("PAYSTACK_TEST_PUBLIC", "pk_test_webhook")
    monkeypatch.setenv("FLUTTERWAVE_TEST_SECRET", "flw-test-secret")
    monkeypatch.setenv("FLUTTERWAVE_TEST_PUBLIC", "flw-public")
    monkeypatch.setenv("FLUTTERWAVE_TEST_WEBHOOK", "flutterwave-webhook-secret")
    enable_payment_provider(db_session, "paystack")
    enable_payment_provider(db_session, "flutterwave")


def _make_provider(db, provider_type=PaymentProviderType.paystack, name="Paystack"):
    provider = PaymentProvider(name=name, provider_type=provider_type)
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _make_invoice(db, account_id, *, amount: str, invoice_number: str):
    issued_at = datetime.now(UTC)
    return billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=account_id,
            invoice_number=invoice_number,
            currency="NGN",
            subtotal=Decimal(amount),
            total=Decimal(amount),
            balance_due=Decimal(amount),
            status=InvoiceStatus.issued,
            issued_at=issued_at,
            due_at=issued_at + timedelta(days=30),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:webhook-payment",
            due_date_policy_version="test-v1",
        ),
    )


def _paystack_body(
    *, reference: str, tx_id: str, amount_kobo: int, metadata: dict, fees_kobo: int = 0
) -> bytes:
    return json.dumps(
        {
            "event": "charge.success",
            "data": {
                "id": tx_id,
                "reference": reference,
                "amount": amount_kobo,
                "fees": fees_kobo,
                "currency": "NGN",
                "status": "success",
                "metadata": metadata,
            },
        }
    ).encode()


def _flutterwave_body(
    *, tx_ref: str, tx_id: str, amount: str, status: str, meta: dict, app_fee: str = "0"
) -> bytes:
    return json.dumps(
        {
            "event": "charge.completed",
            "data": {
                "id": tx_id,
                "tx_ref": tx_ref,
                "amount": amount,
                "app_fee": app_fee,
                "currency": "NGN",
                "status": status,
                "meta": meta,
            },
        }
    ).encode()


def _post_paystack(db, body: bytes):
    signature = hmac.new(b"sk_test_webhook_secret", body, hashlib.sha512).hexdigest()
    return process_paystack_webhook(db=db, body=body, signature=signature)


def _post_flutterwave(db, body: bytes):
    return process_flutterwave_webhook(
        db=db,
        body=body,
        signature="flutterwave-webhook-secret",
    )


def test_paystack_webhook_settles_invoice_end_to_end(db_session, subscriber):
    provider = _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="3000.00", invoice_number="INV-WH-1"
    )
    body = _paystack_body(
        reference="DMAC-WH-1",
        tx_id="990001",
        amount_kobo=300000,
        metadata={"invoice_id": str(invoice.id)},
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    payment = db_session.query(Payment).filter_by(external_id="990001").one()
    assert payment.status == PaymentStatus.succeeded
    assert payment.provider_id == provider.id
    assert payment.amount == Decimal("3000.00")
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")
    assert invoice.status == InvoiceStatus.paid
    event = db_session.query(PaymentProviderEvent).filter_by(external_id="990001").one()
    assert event.payment_id == payment.id


def test_paystack_webhook_captures_provider_fee(db_session, subscriber):
    """M-1: the gateway fee on the charge payload is persisted on the payment so
    ERP can split the receipt and bank reconciliation ties."""
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="3000.00", invoice_number="INV-FEE-1"
    )
    body = _paystack_body(
        reference="DMAC-FEE-1",
        tx_id="990010",
        amount_kobo=300000,
        fees_kobo=4500,  # ₦45.00 fee
        metadata={"invoice_id": str(invoice.id)},
    )

    _post_paystack(db_session, body)

    payment = db_session.query(Payment).filter_by(external_id="990010").one()
    assert payment.amount == Decimal("3000.00")  # gross unchanged
    assert payment.provider_fee == Decimal("45.00")  # fee captured


def test_paystack_webhook_keeps_net_credit_when_invoice_allocation_fails(
    db_session, subscriber
):
    _make_provider(db_session)
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber.id,
            invoice_number="INV-WH-DRAFT-CASH-FIRST",
            currency="NGN",
            subtotal=Decimal("1000.00"),
            total=Decimal("1000.00"),
            balance_due=Decimal("1000.00"),
            status=InvoiceStatus.draft,
        ),
    )
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="DMAC-WH-DRAFT-CASH-FIRST",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("1000.00"),
        status="pending",
        metadata_={
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice.id),
            "account_id": str(subscriber.id),
        },
    )
    db_session.add(intent)
    db_session.commit()
    body = _paystack_body(
        reference=intent.reference,
        tx_id="990011",
        amount_kobo=102000,
        fees_kobo=2000,
        metadata={
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice.id),
            "account_id": str(subscriber.id),
        },
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    payment = db_session.query(Payment).filter_by(external_id="990011").one()
    exception = (
        db_session.query(PaymentAllocationReconciliationException)
        .filter_by(payment_id=payment.id, invoice_id=invoice.id)
        .one()
    )
    db_session.refresh(intent)
    assert payment.amount == Decimal("1020.00")
    assert payment.provider_fee == Decimal("20.00")
    assert payment.settlement.amount == Decimal("1000.00")
    assert payment.settlement.unallocated_amount == Decimal("1000.00")
    assert exception.status == "open"
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id


def test_flutterwave_webhook_captures_app_fee(db_session, subscriber):
    _make_provider(
        db_session, provider_type=PaymentProviderType.flutterwave, name="Flutterwave"
    )
    invoice = _make_invoice(
        db_session, subscriber.id, amount="5000.00", invoice_number="INV-FEE-2"
    )
    body = _flutterwave_body(
        tx_ref="FW-FEE-2",
        tx_id="880010",
        amount="5000.00",
        status="successful",
        app_fee="70.00",
        meta={"invoice_id": str(invoice.id)},
    )

    _post_flutterwave(db_session, body)

    payment = db_session.query(Payment).filter_by(external_id="880010").one()
    assert payment.amount == Decimal("5000.00")
    assert payment.provider_fee == Decimal("70.00")


def test_extract_settlement_defaults_fee_to_zero_when_absent(db_session):
    from app.services.payment_webhook_commands import (
        PaymentWebhookProvider,
        _settlement_observation,
    )

    settlement = _settlement_observation(
        PaymentWebhookProvider.PAYSTACK,
        event_type="charge.success",
        data={"amount": 100000, "currency": "NGN", "reference": "r"},
    )
    assert settlement is not None
    assert settlement.provider_fee == Decimal("0.00")


def test_successful_webhook_requires_observed_currency():
    from app.services.payment_webhook_commands import (
        PaymentWebhookError,
        PaymentWebhookProvider,
        _settlement_observation,
    )

    with pytest.raises(PaymentWebhookError) as captured:
        _settlement_observation(
            PaymentWebhookProvider.PAYSTACK,
            event_type="charge.success",
            data={"amount": 100000, "reference": "r"},
        )

    assert captured.value.code == "financial.payment_webhooks.payload_invalid"


def test_paystack_webhook_replay_creates_one_payment(db_session, subscriber):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2000.00", invoice_number="INV-WH-2"
    )
    body = _paystack_body(
        reference="DMAC-WH-2",
        tx_id="990002",
        amount_kobo=200000,
        metadata={"invoice_id": str(invoice.id)},
    )

    first = _post_paystack(db_session, body)
    second = _post_paystack(db_session, body)

    assert first.status_code == 200
    assert second.status_code == 200
    payments = db_session.query(Payment).filter_by(external_id="990002").all()
    assert len(payments) == 1
    events = (
        db_session.query(PaymentProviderEvent).filter_by(external_id="990002").all()
    )
    assert len(events) == 1


def test_live_success_without_settlement_owner_rolls_back_consequence(db_session):
    _make_provider(db_session)
    body = _paystack_body(
        reference="DMAC-WH-UNLINKED",
        tx_id="990019",
        amount_kobo=100000,
        metadata={},
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 500
    assert (
        db_session.query(PaymentProviderEvent).filter_by(external_id="990019").count()
        == 0
    )
    receipt = (
        db_session.query(IntegrationInbox)
        .filter_by(provider_event_id="paystack-DMAC-WH-UNLINKED")
        .one()
    )
    assert receipt.state == "retryable"
    assert "did not post or link a payment" in (receipt.error_detail or "")
    assert "payment_inbox_failures" in payment_channel_health(db_session).reasons


def test_invalid_invoice_identifier_is_dead_lettered_without_money(db_session):
    _make_provider(db_session)
    body = _paystack_body(
        reference="DMAC-WH-INVALID-INVOICE",
        tx_id="990021",
        amount_kobo=100000,
        metadata={"invoice_id": "not-a-uuid"},
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 400
    assert db_session.query(Payment).filter_by(external_id="990021").count() == 0
    assert (
        db_session.query(PaymentProviderEvent).filter_by(external_id="990021").count()
        == 0
    )
    receipt = (
        db_session.query(IntegrationInbox)
        .filter_by(provider_event_id="paystack-DMAC-WH-INVALID-INVOICE")
        .one()
    )
    assert receipt.state == "dead_letter"
    assert receipt.error_code == "payment_event_rejected"


def test_explicit_unknown_topup_intent_cannot_fall_back_to_reference(
    db_session, subscriber
):
    provider = _make_provider(db_session)
    intent = TopupIntent(
        account_id=subscriber.id,
        provider_id=provider.id,
        reference="DMAC-WH-EXACT-INTENT",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("1000.00"),
        status="pending",
    )
    db_session.add(intent)
    db_session.commit()
    body = _paystack_body(
        reference=intent.reference,
        tx_id="990022",
        amount_kobo=100000,
        metadata={"topup_intent_id": str(uuid4())},
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 409
    assert db_session.query(Payment).filter_by(external_id="990022").count() == 0
    db_session.refresh(intent)
    assert intent.status == "pending"


def test_processed_receipt_failure_rolls_back_payment_and_event(
    db_session, subscriber, monkeypatch
):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="3000.00",
        invoice_number="INV-WH-ATOMIC",
    )
    body = _paystack_body(
        reference="DMAC-WH-ATOMIC",
        tx_id="990020",
        amount_kobo=300000,
        metadata={"invoice_id": str(invoice.id)},
    )
    monkeypatch.setattr(
        "app.services.payment_webhook_commands.integration_inbox.mark_processed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("receipt projection unavailable")
        ),
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 500
    assert db_session.query(Payment).filter_by(external_id="990020").count() == 0
    assert (
        db_session.query(PaymentProviderEvent).filter_by(external_id="990020").count()
        == 0
    )
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("3000.00")
    receipt = (
        db_session.query(IntegrationInbox)
        .filter_by(provider_event_id="paystack-DMAC-WH-ATOMIC")
        .one()
    )
    assert receipt.state == "retryable"
    assert receipt.error_code == "payment_event_processing_failed"


def test_webhook_does_not_double_credit_verify_path_payment(db_session, subscriber):
    """The race that used to double-credit: verify settles first, webhook lands
    second for the same gateway transaction."""
    provider = _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="4000.00", invoice_number="INV-WH-3"
    )
    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            provider_id=provider.id,
            amount=Decimal("4000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            external_id="990003",
        ),
    )
    body = _paystack_body(
        reference="DMAC-WH-3",
        tx_id="990003",
        amount_kobo=400000,
        fees_kobo=4500,
        metadata={"invoice_id": str(invoice.id)},
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    payments = db_session.query(Payment).filter_by(external_id="990003").all()
    assert len(payments) == 1
    assert payments[0].provider_fee == Decimal("45.00")


def test_webhook_matches_legacy_payment_without_provider_id(db_session, subscriber):
    """Verify-path rows written before provider_id stamping must still dedupe."""
    _make_provider(db_session)
    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("1500.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            external_id="990004",
        ),
    )
    body = _paystack_body(
        reference="DMAC-WH-4",
        tx_id="990004",
        amount_kobo=150000,
        metadata={},
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    payments = db_session.query(Payment).filter_by(external_id="990004").all()
    assert len(payments) == 1


def test_paystack_webhook_credits_topup_intent(db_session, subscriber):
    provider = _make_provider(db_session)
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="DMAC-TOPUP-WH-1",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    db_session.add(intent)
    db_session.commit()
    db_session.refresh(intent)
    body = _paystack_body(
        reference="DMAC-TOPUP-WH-1",
        tx_id="990005",
        amount_kobo=500000,
        metadata={
            "payment_flow": "account_topup",
            "topup_intent_id": str(intent.id),
            "account_id": str(subscriber.id),
        },
    )

    response = _post_paystack(db_session, body)

    assert response.status_code == 200
    payment = db_session.query(Payment).filter_by(external_id="990005").one()
    assert payment.status == PaymentStatus.succeeded
    assert payment.provider_id == provider.id
    assert payment.account_id == subscriber.id
    db_session.refresh(intent)
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id
    assert intent.actual_amount == Decimal("5000.00")


def test_flutterwave_successful_charge_settles_invoice(db_session, subscriber):
    _make_provider(
        db_session,
        provider_type=PaymentProviderType.flutterwave,
        name="Flutterwave",
    )
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-WH-5"
    )
    body = _flutterwave_body(
        tx_ref="DMAC-WH-5",
        tx_id="881001",
        amount="2500.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )

    response = _post_flutterwave(db_session, body)

    assert response.status_code == 200
    payment = db_session.query(Payment).filter_by(external_id="881001").one()
    assert payment.status == PaymentStatus.succeeded
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid


def test_flutterwave_failed_charge_completed_moves_no_money(db_session, subscriber):
    """charge.completed carries both outcomes; a failed one must not settle."""
    _make_provider(
        db_session,
        provider_type=PaymentProviderType.flutterwave,
        name="Flutterwave",
    )
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-WH-6"
    )
    body = _flutterwave_body(
        tx_ref="DMAC-WH-6",
        tx_id="881002",
        amount="2500.00",
        status="failed",
        meta={"invoice_id": str(invoice.id)},
    )

    response = _post_flutterwave(db_session, body)

    assert response.status_code == 200
    assert db_session.query(Payment).filter_by(external_id="881002").count() == 0
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("2500.00")


def test_paystack_signature_actually_verified(db_session, monkeypatch):
    """Real HMAC-SHA512 check: valid signature passes the gate, tampered fails."""
    body = _paystack_body(
        reference="DMAC-SIG-1", tx_id="770001", amount_kobo=1000, metadata={}
    )
    good_sig = hmac.new(b"sk_test_webhook_secret", body, hashlib.sha512).hexdigest()

    bad = process_paystack_webhook(db=db_session, body=body, signature="forged")
    assert bad.status_code == 400

    good = process_paystack_webhook(db=db_session, body=body, signature=good_sig)
    # Signature gate passed; no provider row is configured in this test, so the
    # canonical receipt is retained for retry rather than rejected as unsigned.
    assert good.status_code == 503


def _stale_intent(db, subscriber, *, reference: str, minutes_old: int = 60):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=reference,
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(minutes=minutes_old - 30),
    )
    db.add(intent)
    db.commit()
    intent.created_at = datetime.now(UTC) - timedelta(minutes=minutes_old)
    db.commit()
    db.refresh(intent)
    return intent


def _run_reconciliation(db):
    observed_at = datetime.now(UTC)
    db_session_adapter.release_read_transaction(db)
    return reconcile_pending_topups(
        db,
        RunTopupReconciliationCommand(observed_at=observed_at),
        context=CommandContext.system(
            actor="pytest",
            scope=RECONCILIATION_SCOPE,
            reason="Test stranded top-up reconciliation",
        ),
    )


def test_reconciliation_backlog_separates_eligible_and_outside_window(
    db_session, subscriber
):
    observed_at = datetime.now(UTC)
    eligible = _stale_intent(
        db_session,
        subscriber,
        reference="DMAC-RECON-BACKLOG-ELIGIBLE",
    )
    outside_window = _stale_intent(
        db_session,
        subscriber,
        reference="DMAC-RECON-BACKLOG-OUTSIDE",
    )
    outside_window.created_at = observed_at - timedelta(days=8)
    db_session.commit()

    backlog = topup_reconciliation_backlog(
        db_session,
        observed_at=observed_at,
    )

    assert backlog.pending == 2
    assert backlog.eligible == 1
    assert backlog.outside_window == 1
    assert backlog.oldest_pending_at == outside_window.created_at
    assert eligible.created_at < backlog.stale_before.replace(tzinfo=None)


def test_reconciliation_recovers_stranded_topup(db_session, subscriber):
    _make_provider(db_session)
    intent = _stale_intent(db_session, subscriber, reference="DMAC-RECON-1")

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify",
        return_value=PaymentGatewayTransaction(
            provider_type="paystack",
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="660001",
            memo_prefix="Paystack",
        ),
    ):
        result = _run_reconciliation(db_session)

    assert result.recovered == 1
    assert result.errors == 0
    payment = db_session.query(Payment).filter_by(external_id="660001").one()
    assert payment.status == PaymentStatus.succeeded
    db_session.refresh(intent)
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id


def test_reconciliation_routes_typed_deposit_through_deposit_owner(
    db_session, subscriber
):
    provider = _make_provider(db_session)
    intent = TopupIntent(
        account_id=subscriber.id,
        provider_id=provider.id,
        purpose="account_credit_deposit",
        allocation_policy="credit_only",
        credit_application_policy="pay_eligible_invoices",
        policy_version=1,
        preview_fingerprint="a" * 64,
        idempotency_key="typed-reconciliation-deposit",
        channel="customer_selfcare",
        created_by="pytest",
        reference="DMAC-RECON-TYPED-DEPOSIT",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(minutes=30),
        metadata_={"payment_flow": "account_credit_deposit"},
    )
    db_session.add(intent)
    db_session.commit()
    intent.created_at = datetime.now(UTC) - timedelta(minutes=60)
    db_session.commit()

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify",
        return_value=PaymentGatewayTransaction(
            provider_type="paystack",
            amount=Decimal("5090.00"),
            provider_fee=Decimal("90.00"),
            currency="NGN",
            external_id="typed-reconciliation-deposit-payment",
            memo_prefix="Paystack",
        ),
    ):
        result = _run_reconciliation(db_session)

    db_session.refresh(intent)
    payment = db_session.get(Payment, intent.completed_payment_id)
    assert result.recovered == 1
    assert payment is not None
    assert payment.amount == Decimal("5090.00")
    assert payment.provider_fee == Decimal("90.00")
    assert payment.settlement is not None
    assert payment.settlement.amount == Decimal("5000.00")
    assert payment.settlement.unallocated_amount == Decimal("5000.00")
    assert intent.status == "completed"
    assert intent.metadata_["settlement_payment_id"] == str(payment.id)


def test_reconciliation_links_existing_payment_without_duplicating(
    db_session, subscriber
):
    _make_provider(db_session)
    intent = _stale_intent(db_session, subscriber, reference="DMAC-RECON-2")
    billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("5000.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            external_id="660002",
        ),
    )

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify",
        return_value=PaymentGatewayTransaction(
            provider_type="paystack",
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="660002",
            memo_prefix="Paystack",
        ),
    ):
        result = _run_reconciliation(db_session)

    assert result.linked == 1
    assert result.recovered == 0
    assert db_session.query(Payment).filter_by(external_id="660002").count() == 1
    db_session.refresh(intent)
    assert intent.status == "completed"


def test_reconciliation_expires_long_dead_intent(db_session, subscriber):
    _make_provider(db_session)
    intent = _stale_intent(
        db_session, subscriber, reference="DMAC-RECON-3", minutes_old=3 * 24 * 60
    )

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify",
        side_effect=ValueError("Payment was not successful (status: abandoned)"),
    ):
        result = _run_reconciliation(db_session)

    assert result.expired == 1
    assert db_session.query(Payment).count() == 0
    db_session.refresh(intent)
    assert intent.status == "expired"


def test_reconciliation_skips_fresh_pending_intent(db_session, subscriber):
    """An intent inside the checkout window must not be swept mid-payment."""
    _make_provider(db_session)
    intent = TopupIntent(
        account_id=subscriber.id,
        reference="DMAC-RECON-4",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    db_session.add(intent)
    db_session.commit()

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify"
    ) as verify_mock:
        result = _run_reconciliation(db_session)

    assert result.checked == 0
    verify_mock.assert_not_called()
    db_session.refresh(intent)
    assert intent.status == "pending"


def test_reconciliation_uses_configured_sweep_windows(db_session, subscriber):
    specs = {
        "topup_reconciliation_stale_minutes": (15, 1, 1440),
        "topup_reconciliation_max_age_days": (7, 1, 30),
        "topup_reconciliation_expiry_grace_hours": (24, 0, 168),
        "topup_reconciliation_batch_size": (50, 1, 500),
    }
    for key, (default, min_value, max_value) in specs.items():
        spec = get_spec(SettingDomain.billing, key)
        assert spec is not None
        assert spec.default == default
        assert spec.min_value == min_value
        assert spec.max_value == max_value

    _make_provider(db_session)
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="topup_reconciliation_stale_minutes",
            value_type=SettingValueType.integer,
            value_text="120",
            is_active=True,
        )
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="topup_reconciliation_max_age_days",
            value_type=SettingValueType.integer,
            value_text="2",
            is_active=True,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.billing.value, "topup_reconciliation_stale_minutes"
    )
    SettingsCache.invalidate(
        SettingDomain.billing.value, "topup_reconciliation_max_age_days"
    )
    _stale_intent(db_session, subscriber, reference="DMAC-RECON-CFG", minutes_old=60)

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify"
    ) as verify_mock:
        result = _run_reconciliation(db_session)

    assert result.checked == 0
    verify_mock.assert_not_called()


def _capability_http_error(status_code: int) -> PaymentCapabilityError:
    return PaymentCapabilityError(f"provider_http_{status_code}")


def test_reconciliation_expires_intent_on_gateway_not_found(db_session, subscriber):
    """A 400/404 from the gateway (abandoned, never-charged reference) must
    expire the intent like a not-successful verify — not count as a retryable
    error that re-fires every sweep and jams the bounded queue."""
    _make_provider(db_session)
    intent = _stale_intent(
        db_session, subscriber, reference="DMAC-RECON-404", minutes_old=3 * 24 * 60
    )

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify",
        side_effect=_capability_http_error(400),
    ):
        result = _run_reconciliation(db_session)

    assert result.expired == 1
    assert result.errors == 0
    db_session.refresh(intent)
    assert intent.status == "expired"


def test_reconciliation_keeps_5xx_as_retryable_error(db_session, subscriber):
    """A gateway 5xx is transient — keep it an error (retry next sweep), never
    silently expire a possibly-paid intent."""
    _make_provider(db_session)
    intent = _stale_intent(
        db_session, subscriber, reference="DMAC-RECON-503", minutes_old=3 * 24 * 60
    )

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify",
        side_effect=_capability_http_error(503),
    ):
        result = _run_reconciliation(db_session)

    assert result.errors == 1
    assert result.expired == 0
    db_session.refresh(intent)
    assert intent.status == "pending"


def test_reconciliation_skips_non_gateway_provider(db_session, subscriber):
    """direct_bank_transfer settles via proof upload, not a verify API — it
    must never enter the gateway sweep (it would 400 forever)."""
    _make_provider(db_session)
    intent = _stale_intent(
        db_session, subscriber, reference="TRF-RECON-1", minutes_old=3 * 24 * 60
    )
    intent.provider_type = "direct_bank_transfer"
    db_session.commit()

    with patch(
        "app.services.payment_reconciliation.payment_gateway_adapter.verify"
    ) as verify_mock:
        result = _run_reconciliation(db_session)

    assert result.checked == 0
    verify_mock.assert_not_called()
    db_session.refresh(intent)
    assert intent.status == "pending"
