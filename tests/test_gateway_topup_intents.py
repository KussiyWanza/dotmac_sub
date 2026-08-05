"""Gateway top-up intent command-owner behavior."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.billing import BillingAccount, Invoice, InvoiceStatus, TopupIntent
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Reseller
from app.services import gateway_topup_intents as svc
from app.services.account_credit_deposits import AccountCreditDeposits
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_payment_provider


def _patch_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_value",
        lambda _db, _domain, key: {
            "gateway_topup_intent_ttl_minutes": 30,
            "topup_min_amount": 1000,
            "topup_max_amount": 500000,
        }[key],
    )


def _context(scope: str, *, idempotency_key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor="pytest:gateway-intents",
        scope=scope,
        reason="Gateway intent behavior test",
        idempotency_key=idempotency_key,
    )


def _checkout_binding(db_session, provider_type: str = "paystack"):
    return enable_payment_provider(db_session, provider_type)["payments.intent.v1"]


def _create_deposit(
    db_session,
    subscriber,
    *,
    capability_binding_id: UUID,
    preview_fingerprint: str | None = None,
) -> svc.GatewayTopupIntentResult:
    account_id = subscriber.id
    if preview_fingerprint is None:
        preview_fingerprint = AccountCreditDeposits.preview(
            db_session,
            account_id=account_id,
            amount="5000.00",
            currency="NGN",
            minimum="1000.00",
            maximum="500000.00",
        ).fingerprint
    command = svc.CreateCustomerGatewayTopupIntentCommand(
        flow=svc.CustomerGatewayTopupFlow.account_credit_deposit,
        account_id=account_id,
        requested_amount="5000.00",
        reference="gateway-deposit-test-ref",
        provider_type="paystack",
        provider_id=None,
        created_by="pytest",
        expected_preview_fingerprint=preview_fingerprint,
        capability_binding_id=capability_binding_id,
    )
    db_session_adapter.release_read_transaction(db_session)
    return svc.create_customer_gateway_topup_intent(
        db_session,
        command,
        context=_context(
            svc.CREATE_CUSTOMER_SCOPE,
            idempotency_key="stable-deposit-test-key",
        ),
    )


def test_customer_invoice_creation_derives_locked_invoice_amount(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-GATEWAY-OWNER",
        currency="NGN",
        subtotal=Decimal("3750.00"),
        total=Decimal("3750.00"),
        balance_due=Decimal("3750.00"),
        status=InvoiceStatus.issued,
    )
    db_session.add(invoice)
    db_session.commit()
    invoice_id = invoice.id
    account_id = subscriber.id
    binding = _checkout_binding(db_session)
    binding_id = binding.id

    db_session_adapter.release_read_transaction(db_session)
    result = svc.create_customer_gateway_topup_intent(
        db_session,
        svc.CreateCustomerGatewayTopupIntentCommand(
            flow=svc.CustomerGatewayTopupFlow.invoice_payment,
            account_id=account_id,
            invoice_id=invoice_id,
            reference="gateway-invoice-test-ref",
            provider_type="paystack",
            provider_id=None,
            created_by="pytest",
            capability_binding_id=binding_id,
        ),
        context=_context(svc.CREATE_CUSTOMER_SCOPE),
    )

    assert result.requested_amount == Decimal("3750.00")
    intent = db_session.get(TopupIntent, result.intent_id)
    assert intent is not None
    assert intent.invoice_id == invoice_id
    assert intent.metadata_ == {
        "payment_flow": "invoice_payment",
        "invoice_id": str(invoice_id),
        "invoice_number": "INV-GATEWAY-OWNER",
        "account_id": str(account_id),
        "capability_binding_id": str(binding_id),
    }


def test_customer_invoice_creation_rejects_proforma_at_owner_boundary(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number="PF-GATEWAY-OWNER",
        currency="NGN",
        subtotal=Decimal("3750.00"),
        total=Decimal("3750.00"),
        balance_due=Decimal("3750.00"),
        status=InvoiceStatus.issued,
        is_proforma=True,
    )
    db_session.add(invoice)
    db_session.commit()
    binding = _checkout_binding(db_session)
    account_id = subscriber.id
    invoice_id = invoice.id
    binding_id = binding.id

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(svc.GatewayTopupIntentError) as rejected:
        svc.create_customer_gateway_topup_intent(
            db_session,
            svc.CreateCustomerGatewayTopupIntentCommand(
                flow=svc.CustomerGatewayTopupFlow.invoice_payment,
                account_id=account_id,
                invoice_id=invoice_id,
                reference="gateway-proforma-test-ref",
                provider_type="paystack",
                provider_id=None,
                created_by="pytest",
                capability_binding_id=binding_id,
            ),
            context=_context(svc.CREATE_CUSTOMER_SCOPE),
        )

    assert rejected.value.code.endswith("invoice_not_payable")
    assert (
        db_session.scalar(
            select(TopupIntent).where(
                TopupIntent.reference == "gateway-proforma-test-ref"
            )
        )
        is None
    )


def test_customer_deposit_creation_uses_policy_and_replays(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)

    first = _create_deposit(
        db_session,
        subscriber,
        capability_binding_id=binding.id,
    )
    second = _create_deposit(
        db_session,
        subscriber,
        capability_binding_id=binding.id,
        preview_fingerprint=first.preview_fingerprint,
    )

    assert first.intent_id == second.intent_id
    assert first.preview_fingerprint
    assert second.replayed is True
    assert first.requested_amount == Decimal("5000.00")


def test_customer_deposit_creation_requires_reviewed_preview(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)

    command = svc.CreateCustomerGatewayTopupIntentCommand(
        flow=svc.CustomerGatewayTopupFlow.account_credit_deposit,
        account_id=subscriber.id,
        requested_amount="5000.00",
        reference="gateway-deposit-missing-preview",
        provider_type="paystack",
        provider_id=None,
        created_by="pytest",
        capability_binding_id=binding.id,
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(svc.GatewayTopupIntentError) as exc_info:
        svc.create_customer_gateway_topup_intent(
            db_session,
            command,
            context=_context(svc.CREATE_CUSTOMER_SCOPE),
        )

    assert exc_info.value.code.endswith("preview_required")


def test_reseller_creation_locks_canonical_billing_account(monkeypatch, db_session):
    _patch_policy(monkeypatch)
    reseller = Reseller(name="Gateway Reseller", code="GATEWAY-OWNER")
    db_session.add(reseller)
    db_session.flush()
    billing_account = BillingAccount(
        reseller_id=reseller.id,
        name="Gateway Reseller Billing",
        currency="NGN",
        status="active",
        is_active=True,
    )
    db_session.add(billing_account)
    db_session.commit()
    reseller_id = reseller.id
    billing_account_id = billing_account.id
    binding = _checkout_binding(db_session)
    binding_id = binding.id

    db_session_adapter.release_read_transaction(db_session)
    result = svc.create_reseller_gateway_topup_intent(
        db_session,
        svc.CreateResellerGatewayTopupIntentCommand(
            billing_account_id=billing_account_id,
            reseller_id=reseller_id,
            reference="gateway-reseller-test-ref",
            provider_type="paystack",
            provider_id=None,
            requested_amount="12000.00",
            capability_binding_id=binding_id,
            save_card=True,
        ),
        context=_context(svc.CREATE_RESELLER_SCOPE),
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert result.billing_account_id == billing_account_id
    assert intent is not None
    assert intent.metadata_["payment_flow"] == "reseller_consolidated"
    assert intent.metadata_["reseller_card_id"] == str(reseller_id)


def test_saved_card_failure_atomically_releases_retry_reservation(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)
    result = _create_deposit(db_session, subscriber, capability_binding_id=binding.id)
    reservation = IdempotencyKey(
        scope=svc.SavedCardChargeScope.account_credit_deposit.value,
        key="declined-charge-key",
        account_id=subscriber.id,
    )
    db_session.add(reservation)
    db_session.commit()
    reservation_id: UUID = reservation.id

    db_session_adapter.release_read_transaction(db_session)
    failure = svc.fail_saved_card_charge(
        db_session,
        svc.FailSavedCardChargeCommand(
            intent_id=result.intent_id,
            reservation_id=reservation_id,
            reservation_scope=svc.SavedCardChargeScope.account_credit_deposit,
        ),
        context=_context(svc.FAIL_SAVED_CARD_SCOPE),
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert failure.changed is True
    assert failure.reservation_released is True
    assert intent is not None and intent.status == "failed"
    assert db_session.get(IdempotencyKey, reservation_id) is None


def test_saved_card_failure_rolls_back_on_reservation_mismatch(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    binding = _checkout_binding(db_session)
    result = _create_deposit(db_session, subscriber, capability_binding_id=binding.id)
    reservation = IdempotencyKey(
        scope=svc.SavedCardChargeScope.invoice.value,
        key="mismatched-charge-key",
        account_id=subscriber.id,
    )
    db_session.add(reservation)
    db_session.commit()
    reservation_id = reservation.id

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(svc.GatewayTopupIntentError) as exc_info:
        svc.fail_saved_card_charge(
            db_session,
            svc.FailSavedCardChargeCommand(
                intent_id=result.intent_id,
                reservation_id=reservation_id,
                reservation_scope=svc.SavedCardChargeScope.account_credit_deposit,
            ),
            context=_context(svc.FAIL_SAVED_CARD_SCOPE),
        )

    assert exc_info.value.code.endswith("reservation_mismatch")
    intent = db_session.get(TopupIntent, result.intent_id)
    assert intent is not None and intent.status == "pending"
    assert db_session.get(IdempotencyKey, reservation_id) is not None


def test_customer_creation_rejects_missing_checkout_binding(
    monkeypatch, db_session, subscriber
):
    _patch_policy(monkeypatch)
    command = svc.CreateCustomerGatewayTopupIntentCommand(
        flow=svc.CustomerGatewayTopupFlow.account_credit_deposit,
        account_id=subscriber.id,
        requested_amount="5000.00",
        reference="gateway-missing-binding-ref",
        provider_type="paystack",
        provider_id=None,
        created_by="pytest",
        capability_binding_id=UUID(int=0),
    )

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(svc.GatewayTopupIntentError) as exc_info:
        svc.create_customer_gateway_topup_intent(
            db_session,
            command,
            context=_context(svc.CREATE_CUSTOMER_SCOPE),
        )

    assert exc_info.value.code.endswith("checkout_binding_unavailable")
