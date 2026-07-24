"""Direct-transfer intent creation owner and participant composition."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from app.models.billing import (
    CollectionAccount,
    CollectionAccountType,
    Invoice,
    InvoiceStatus,
    TopupIntent,
)
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.event_store import EventStore
from app.services import direct_transfer_intents as svc
from app.services import topup_intents
from app.services.account_credit_deposits import AccountCreditDeposits
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _context(*, idempotency_key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor="customer:test",
        scope=svc.CREATE_SCOPE,
        reason="Direct-transfer intent behavior test",
        idempotency_key=idempotency_key,
    )


def _configuration(
    *, enabled: bool = True
) -> topup_intents.DirectTransferConfiguration:
    return topup_intents.DirectTransferConfiguration(
        accounts=(
            topup_intents.DirectTransferConfiguredAccount(
                id="bank-primary",
                enabled=True,
                bank_name="Dotmac Test Bank",
                account_name="Dotmac Payments",
                account_number="0123456789",
                sort_code="123456",
            ),
        )
        if enabled
        else (),
        bank_name="Dotmac Test Bank",
        account_name="Dotmac Payments",
        account_number="0123456789",
        sort_code="123456",
        instructions="Use the supplied reference.",
    )


def _patch_policy(monkeypatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(
        topup_intents,
        "direct_transfer_configuration",
        lambda _db: _configuration(enabled=enabled),
    )

    def resolve_policy(_db, _domain, key):
        return {
            "topup_min_amount": 1000,
            "topup_max_amount": 500000,
            "direct_bank_transfer_intent_ttl_days": 7,
            "withholding_tax_rate_percent": "5.00",
        }[key]

    monkeypatch.setattr(svc, "resolve_value", resolve_policy)


def _create(
    db_session,
    *,
    account_id: UUID,
    amount: str | None = None,
    invoice_id: UUID | None = None,
    idempotency_key: str | None = None,
    preview_fingerprint: str | None = None,
) -> svc.DirectTransferIntentResult:
    if preview_fingerprint is None and invoice_id is None and amount is not None:
        preview_fingerprint = AccountCreditDeposits.preview(
            db_session,
            account_id=account_id,
            amount=amount,
            currency="NGN",
            minimum="1000.00",
            maximum="500000.00",
        ).fingerprint
    command = svc.CreateDirectTransferIntentCommand(
        account_id=account_id,
        created_by="pytest",
        requested_amount=amount,
        invoice_id=invoice_id,
        expected_preview_fingerprint=preview_fingerprint,
    )
    db_session_adapter.release_read_transaction(db_session)
    return svc.create_direct_transfer_intent(
        db_session,
        command,
        context=_context(idempotency_key=idempotency_key),
    )


def test_configuration_projection_uses_active_stable_accounts(
    db_session,
):
    account = CollectionAccount(
        name="Primary transfer account",
        account_type=CollectionAccountType.bank,
        currency="NGN",
        is_active=True,
        bank_name="Dotmac Test Bank",
        account_name="Dotmac Payments",
        account_number="0123456789",
        sort_code="123456",
        presentment_priority=100,
    )
    db_session.add_all(
        [
            account,
            DomainSetting(
                domain=SettingDomain.modules,
                key="billing_direct_bank_transfer",
                value_type=SettingValueType.boolean,
                value_text="false",
                is_active=True,
            ),
        ]
    )
    db_session.commit()
    first = topup_intents.direct_transfer_configuration(db_session)
    db_session.expire_all()
    second = topup_intents.direct_transfer_configuration(db_session)

    assert first.enabled is True
    assert first.enabled_accounts[0].id == str(account.id)
    assert second.enabled_accounts[0].id == first.enabled_accounts[0].id


def test_deposit_direct_transfer_creation_is_typed_atomic_and_evented(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)

    result = _create(
        db_session,
        account_id=subscriber.id,
        amount="5000.00",
        idempotency_key="deposit-create-key",
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert intent is not None
    assert result.payment_flow == "account_credit_deposit"
    assert result.requested_amount == Decimal("5000.00")
    assert intent.purpose == "account_credit_deposit"
    assert intent.provider_type == topup_intents.DIRECT_TRANSFER_PROVIDER
    assert intent.metadata_ == {
        "payment_method": "bank_transfer",
        "payment_flow": "account_credit_deposit",
    }
    created = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "topup_intent.direct_transfer_created")
        .one()
    )
    assert created.payload["topup_intent_id"] == str(intent.id)


def test_deposit_direct_transfer_idempotency_replays_without_second_event(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)

    first = _create(
        db_session,
        account_id=subscriber.id,
        amount="5000.00",
        idempotency_key="same-deposit-create-key",
    )
    second = _create(
        db_session,
        account_id=subscriber.id,
        amount="5000.00",
        idempotency_key="same-deposit-create-key",
        preview_fingerprint=first.preview_fingerprint,
    )

    assert second.intent_id == first.intent_id
    assert second.replayed is True
    assert db_session.query(TopupIntent).count() == 1
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "topup_intent.direct_transfer_created")
        .count()
        == 1
    )


def test_deposit_direct_transfer_requires_reviewed_preview(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)

    command = svc.CreateDirectTransferIntentCommand(
        account_id=subscriber.id,
        created_by="pytest",
        requested_amount="5000.00",
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(svc.DirectTransferIntentError) as exc_info:
        svc.create_direct_transfer_intent(
            db_session,
            command,
            context=_context(),
        )

    assert (
        exc_info.value.code
        == "financial.direct_transfer_intent_commands.preview_required"
    )


def test_invoice_creation_uses_locked_balance_and_replaces_pending_attempt(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("2500.00"),
        balance_due=Decimal("1750.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    first = _create(
        db_session,
        account_id=subscriber.id,
        amount="999999.00",
        invoice_id=invoice.id,
        idempotency_key="invoice-create-first",
    )
    second = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="invoice-create-second",
    )

    first_intent = db_session.get(TopupIntent, first.intent_id)
    second_intent = db_session.get(TopupIntent, second.intent_id)
    assert first_intent is not None and second_intent is not None
    assert first.requested_amount == Decimal("1750.00")
    assert first_intent.status == "canceled"
    assert first_intent.metadata_["replaced_by_intent_id"] == str(second.intent_id)
    assert second_intent.status == "pending"
    assert second.replaced_intent_ids == (first.intent_id,)
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "topup_intent.direct_transfer_canceled")
        .count()
        == 1
    )


def test_invoice_creation_idempotency_replays_without_replacement(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("2500.00"),
        balance_due=Decimal("2500.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    first = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="same-invoice-create-key",
    )
    second = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="same-invoice-create-key",
    )

    assert second.intent_id == first.intent_id
    assert second.replayed is True
    assert second.replaced_intent_ids == ()
    assert db_session.query(TopupIntent).count() == 1


def test_creation_rolls_back_intent_when_creation_event_cannot_stage(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("intent event unavailable")

    monkeypatch.setattr(
        topup_intents,
        "stage_direct_transfer_intent_created_event",
        fail_event,
    )

    with pytest.raises(RuntimeError, match="intent event unavailable"):
        _create(
            db_session,
            account_id=subscriber.id,
            amount="5000.00",
        )

    assert db_session.query(TopupIntent).count() == 0
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "topup_intent.direct_transfer_created")
        .count()
        == 0
    )


def test_invoice_replacement_rolls_back_cancellation_when_event_staging_fails(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("2500.00"),
        balance_due=Decimal("2500.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    first = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="replacement-before-failure",
    )

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("replacement event unavailable")

    monkeypatch.setattr(
        topup_intents,
        "stage_direct_transfer_intent_created_event",
        fail_event,
    )

    with pytest.raises(RuntimeError, match="replacement event unavailable"):
        _create(
            db_session,
            account_id=subscriber.id,
            invoice_id=invoice.id,
            idempotency_key="replacement-that-rolls-back",
        )

    db_session.expire_all()
    original = db_session.get(TopupIntent, first.intent_id)
    assert original is not None
    assert original.status == "pending"
    assert db_session.query(TopupIntent).count() == 1


def test_creation_fails_closed_when_feature_has_no_available_configuration(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch, enabled=False)

    with pytest.raises(svc.DirectTransferIntentError) as exc:
        _create(
            db_session,
            account_id=subscriber.id,
            amount="5000.00",
        )

    assert exc.value.code == "financial.direct_transfer_intent_commands.unavailable"
    assert db_session.query(TopupIntent).count() == 0


def test_invoice_creation_snapshots_customer_wht_from_vat_exclusive_basis(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)
    monkeypatch.setattr(
        svc.customer_tax_policies,
        "get_customer_withholding_tax_policy",
        lambda *_args, **_kwargs: (
            svc.customer_tax_policies.CustomerWithholdingTaxPolicy(
                account_id=subscriber.id,
                withholding_tax_enabled=True,
                version=3,
                updated_by="admin-1",
                updated_at=None,
            )
        ),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("7500.00"),
        total=Decimal("107500.00"),
        balance_due=Decimal("107500.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    result = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="invoice-wht-snapshot",
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert result.requested_amount == Decimal("102500.00")
    assert intent is not None
    assert intent.metadata_["payment_flow"] == "invoice_payment"
    assert intent.metadata_["invoice_id"] == str(invoice.id)
    assert intent.metadata_["withholding_tax"] == {
        "schema_version": 1,
        "account_id": str(subscriber.id),
        "policy_version": 3,
        "source_invoice_id": str(invoice.id),
        "currency": "NGN",
        "vat_exclusive_amount": "100000.00",
        "vat_amount": "7500.00",
        "gross_amount": "107500.00",
        "withholding_tax_rate_percent": "5.00",
        "withholding_tax_amount": "5000.00",
        "net_amount": "102500.00",
    }


def test_invoice_creation_omits_wht_snapshot_when_customer_policy_disabled(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)
    monkeypatch.setattr(
        svc.customer_tax_policies,
        "get_customer_withholding_tax_policy",
        lambda *_args, **_kwargs: (
            svc.customer_tax_policies.CustomerWithholdingTaxPolicy(
                account_id=subscriber.id,
                withholding_tax_enabled=False,
                version=0,
                updated_by=None,
                updated_at=None,
            )
        ),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("7500.00"),
        total=Decimal("107500.00"),
        balance_due=Decimal("107500.00"),
    )
    db_session.add(invoice)
    db_session.commit()

    result = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="invoice-no-wht-snapshot",
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert result.requested_amount == Decimal("107500.00")
    assert intent is not None
    assert "withholding_tax" not in (intent.metadata_ or {})


def test_invoice_creation_snapshot_survives_later_setting_change(
    db_session, subscriber, monkeypatch
):
    _patch_policy(monkeypatch)
    monkeypatch.setattr(
        svc.customer_tax_policies,
        "get_customer_withholding_tax_policy",
        lambda *_args, **_kwargs: (
            svc.customer_tax_policies.CustomerWithholdingTaxPolicy(
                account_id=subscriber.id,
                withholding_tax_enabled=True,
                version=7,
                updated_by="admin-1",
                updated_at=None,
            )
        ),
    )
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("7500.00"),
        total=Decimal("107500.00"),
        balance_due=Decimal("107500.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    first = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="invoice-snapshot-stable",
    )

    monkeypatch.setattr(
        svc,
        "resolve_value",
        lambda _db, _domain, key: {
            "topup_min_amount": 1000,
            "topup_max_amount": 500000,
            "direct_bank_transfer_intent_ttl_days": 7,
            "withholding_tax_rate_percent": "10.00",
        }[key],
    )

    same = _create(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        idempotency_key="invoice-snapshot-stable",
    )

    persisted = db_session.get(TopupIntent, first.intent_id)
    assert same.intent_id == first.intent_id
    assert persisted is not None
    assert (
        persisted.metadata_["withholding_tax"]["withholding_tax_rate_percent"] == "5.00"
    )
    assert persisted.metadata_["withholding_tax"]["withholding_tax_amount"] == "5000.00"
