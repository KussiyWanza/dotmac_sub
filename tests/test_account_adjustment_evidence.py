"""Preview/confirmation evidence for debit-only account adjustments."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import billing as billing_api
from app.models.audit import AuditEvent
from app.models.billing import (
    AccountAdjustment,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.event_store import EventStore
from app.models.subscription_engine import SettingValueType
from app.schemas.billing import (
    AccountAdjustmentConfirm,
    AccountAdjustmentPreviewRequest,
    AccountAdjustmentReversalConfirm,
    AccountAdjustmentReversalPreviewRequest,
    LedgerEntryCreate,
)
from app.services.billing._common import get_account_credit_balance
from app.services.billing.adjustments import (
    ACCOUNT_ADJUSTMENT_SCOPE,
    ConfirmAccountAdjustmentCommand,
    PreviewAccountAdjustmentQuery,
    PreviewAccountAdjustmentReversalQuery,
    ReverseAccountAdjustmentCommand,
    confirm_account_adjustment,
    inspect_account_adjustment_evidence,
    preview_account_adjustment,
    preview_account_adjustment_reversal,
    reverse_account_adjustment,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _fund(
    db_session,
    subscriber,
    amount: Decimal,
    *,
    currency: str = "NGN",
) -> LedgerEntry:
    entry = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        category=LedgerCategory.top_up,
        amount=amount,
        currency=currency,
        memo="Confirmed prepaid funding",
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def _request(subscriber, *, amount: Decimal = Decimal("2500.00")):
    return AccountAdjustmentPreviewRequest(
        account_id=subscriber.id,
        category=LedgerCategory.custom_service,
        amount=amount,
        currency="NGN",
        memo="Manual service debit",
        reason="Operator-confirmed service adjustment",
    )


def _confirm(db_session, request, fingerprint, *, key="adjustment-test-key-0001"):
    db_session_adapter.release_read_transaction(db_session)
    return confirm_account_adjustment(
        db_session,
        ConfirmAccountAdjustmentCommand(
            context=CommandContext.system(
                actor="user:test-operator",
                scope=ACCOUNT_ADJUSTMENT_SCOPE,
                reason="Test account-adjustment confirmation",
                idempotency_key=key,
            ),
            confirmation=AccountAdjustmentConfirm(
                **request.model_dump(),
                preview_fingerprint=fingerprint,
                idempotency_key=key,
            ),
        ),
    )


def test_adjustment_preview_and_confirmation_link_exact_debit(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)

    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )

    assert preview.prepaid_funding_before == Decimal("5000.00")
    assert preview.prepaid_funding_after == Decimal("2500.00")
    assert preview.postpaid_receivables == Decimal("0.00")
    assert preview.collection_blocking_balance == Decimal("0.00")
    assert preview.ledger_entry_type == LedgerEntryType.debit
    assert preview.ledger_source == LedgerSource.adjustment
    assert preview.ledger_amount == Decimal("2500.00")
    assert preview.access_consequence == "none_adjustment_only"
    assert preview.allowed is True

    result = _confirm(db_session, request, preview.fingerprint)

    assert result.adjustment.ledger_entry_id == result.ledger_entry.id
    assert result.ledger_entry.entry_type == LedgerEntryType.debit
    assert result.ledger_entry.source == LedgerSource.adjustment
    assert result.ledger_entry.category == LedgerCategory.custom_service
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "2500.00"
    )
    audit = (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "account_adjustment",
            AuditEvent.entity_id == str(result.adjustment.id),
            AuditEvent.action == "confirm",
        )
        .one()
    )
    assert audit.metadata_["ledger_entry_id"] == str(result.ledger_entry.id)
    assert audit.metadata_["prepaid_funding_before"] == "5000.00"
    assert audit.metadata_["prepaid_funding_after"] == "2500.00"
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "account_adjustment.confirmed")
        .one()
    )
    assert event.subscriber_id == subscriber.id
    assert event.payload["ledger_entry_id"] == str(result.ledger_entry.id)


def test_adjustment_omitted_currency_uses_settings_spec(db_session, subscriber):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="default_currency",
            value_type=SettingValueType.string,
            value_text="USD",
            is_active=True,
        )
    )
    _fund(db_session, subscriber, Decimal("5000.00"), currency="USD")
    request = AccountAdjustmentPreviewRequest(
        account_id=subscriber.id,
        category=LedgerCategory.custom_service,
        amount=Decimal("2500.00"),
        memo="Manual service debit",
        reason="Operator-confirmed service adjustment",
    )

    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )

    assert preview.currency == "USD"
    assert preview.allowed is True


def test_billing_api_adapts_typed_adjustment_command(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    principal = {"principal_type": "user", "principal_id": "finance-operator"}
    preview = billing_api.preview_account_adjustment(request, db_session, principal)

    adjustment = billing_api.confirm_account_adjustment(
        AccountAdjustmentConfirm(
            **request.model_dump(),
            preview_fingerprint=preview["fingerprint"],
            idempotency_key="adjustment-api-adapter-0001",
        ),
        db_session,
        principal,
    )

    assert adjustment.account_id == subscriber.id
    assert adjustment.ledger_entry.entry_type is LedgerEntryType.debit


def test_adjustment_confirmation_rejects_stale_financial_state(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )
    _fund(db_session, subscriber, Decimal("500.00"))

    with pytest.raises(DomainError, match="preview again") as exc:
        _confirm(db_session, request, preview.fingerprint)

    assert exc.value.code == "financial.account_adjustments.stale_preview"
    assert db_session.query(AccountAdjustment).count() == 0
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "5500.00"
    )


def test_adjustment_rejects_insufficient_funding_without_a_write(
    db_session, subscriber
):
    _fund(db_session, subscriber, Decimal("1000.00"))
    request = _request(subscriber)
    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )

    assert preview.allowed is False
    assert preview.rejection_reason == "insufficient_prepaid_funding"
    assert preview.shortfall == Decimal("1500.00")
    with pytest.raises(DomainError) as exc:
        _confirm(db_session, request, preview.fingerprint)

    assert exc.value.code == "financial.account_adjustments.insufficient_funding"
    assert db_session.query(AccountAdjustment).count() == 0
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1000.00"
    )


def test_adjustment_confirmation_replays_without_a_second_debit(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )
    first = _confirm(db_session, request, preview.fingerprint)

    replay = _confirm(db_session, request, preview.fingerprint)

    assert replay.replayed is True
    assert replay.adjustment.id == first.adjustment.id
    assert replay.ledger_entry.id == first.ledger_entry.id
    assert replay.preview.prepaid_funding_before == Decimal("5000.00")
    assert replay.preview.prepaid_funding_after == Decimal("2500.00")
    assert db_session.query(AccountAdjustment).count() == 1
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.source == LedgerSource.adjustment)
        .count()
        == 1
    )


def test_adjustment_reversal_is_previewed_idempotent_and_structurally_linked(
    db_session, subscriber
):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    debit_preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )
    debit = _confirm(db_session, request, debit_preview.fingerprint)
    reversal_request = AccountAdjustmentReversalPreviewRequest(
        reason="Operator-confirmed correction"
    )
    preview = preview_account_adjustment_reversal(
        db_session,
        PreviewAccountAdjustmentReversalQuery(
            adjustment_id=debit.adjustment.id,
            request=reversal_request,
        ),
    )

    adjustment_id = debit.adjustment.id
    reversal_key = "adjustment-reversal-test-0001"
    db_session_adapter.release_read_transaction(db_session)
    result = reverse_account_adjustment(
        db_session,
        ReverseAccountAdjustmentCommand(
            context=CommandContext.system(
                actor="user:test-operator",
                scope=ACCOUNT_ADJUSTMENT_SCOPE,
                reason="Test account-adjustment reversal",
                idempotency_key=reversal_key,
            ),
            adjustment_id=adjustment_id,
            confirmation=AccountAdjustmentReversalConfirm(
                reason=reversal_request.reason,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=reversal_key,
            ),
        ),
    )

    assert result.ledger_entry.entry_type == LedgerEntryType.credit
    assert result.ledger_entry.category == LedgerCategory.custom_service
    assert result.ledger_entry.reversal_of_entry_id == debit.ledger_entry.id
    assert result.adjustment.reversal_ledger_entry_id == result.ledger_entry.id
    assert result.adjustment.reversal_prepaid_funding_before == Decimal("2500.00")
    assert result.adjustment.reversal_prepaid_funding_after == Decimal("5000.00")
    assert result.preview.access_consequence == "none_adjustment_reversal_only"
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "5000.00"
    )

    db_session_adapter.release_read_transaction(db_session)
    replay = reverse_account_adjustment(
        db_session,
        ReverseAccountAdjustmentCommand(
            context=CommandContext.system(
                actor="user:test-operator",
                scope=ACCOUNT_ADJUSTMENT_SCOPE,
                reason="Test account-adjustment reversal replay",
                idempotency_key=reversal_key,
            ),
            adjustment_id=adjustment_id,
            confirmation=AccountAdjustmentReversalConfirm(
                reason=reversal_request.reason,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=reversal_key,
            ),
        ),
    )
    assert replay.replayed is True
    assert replay.ledger_entry.id == result.ledger_entry.id
    assert replay.preview.prepaid_funding_before == Decimal("2500.00")
    assert replay.preview.prepaid_funding_after == Decimal("5000.00")
    # ADR 0007 Phase 3 forward-shadow: the confirmed adjustment staged one
    # posting group and the reversal negated + linked exactly that group.
    from app.models.customer_subledger import (
        CustomerPostingGroup,
        PostingCommandKind,
    )

    shadow_groups = (
        db_session.query(CustomerPostingGroup)
        .filter(CustomerPostingGroup.producer_owner == "financial.account_adjustments")
        .all()
    )
    originals = [
        g for g in shadow_groups if g.command_kind is PostingCommandKind.adjustment
    ]
    reversals = [
        g for g in shadow_groups if g.command_kind is PostingCommandKind.reversal
    ]
    linked = [g for g in reversals if g.reverses_group_id is not None]
    assert len(originals) >= 1
    assert len(linked) == 1
    assert linked[0].reverses_group_id in {g.id for g in originals}
    assert (
        db_session.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == str(debit.adjustment.id),
            AuditEvent.action == "reverse",
        )
        .count()
        == 1
    )
    reversal_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "account_adjustment.reversed")
        .one()
    )
    assert reversal_event.subscriber_id == subscriber.id
    assert reversal_event.payload["reverses_ledger_entry_id"] == str(
        debit.ledger_entry.id
    )

    report = inspect_account_adjustment_evidence(db_session)
    assert report.scanned_count == 1
    assert report.drift_count == 0


def test_public_adjustment_owner_rejects_an_active_caller_transaction(
    db_session, subscriber
):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )

    with pytest.raises(DomainError) as exc:
        confirm_account_adjustment(
            db_session,
            ConfirmAccountAdjustmentCommand(
                context=CommandContext.system(
                    actor="user:test-operator",
                    scope=ACCOUNT_ADJUSTMENT_SCOPE,
                    reason="Prove adapters cannot own the transaction",
                    idempotency_key="adjustment-active-transaction-0001",
                ),
                confirmation=AccountAdjustmentConfirm(
                    **request.model_dump(),
                    preview_fingerprint=preview.fingerprint,
                    idempotency_key="adjustment-active-transaction-0001",
                ),
            ),
        )

    assert exc.value.code == "financial.account_adjustments.active_caller_transaction"
    assert db_session.query(AccountAdjustment).count() == 0


def test_late_event_failure_rolls_back_debit_and_evidence(
    db_session, subscriber, monkeypatch
):
    from app.services.billing import adjustments as adjustment_service

    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("event staging unavailable")

    monkeypatch.setattr(adjustment_service, "emit_event", fail_event)
    with pytest.raises(RuntimeError, match="event staging unavailable"):
        _confirm(
            db_session,
            request,
            preview.fingerprint,
            key="adjustment-event-rollback-0001",
        )

    assert db_session.query(AccountAdjustment).count() == 0
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.source == LedgerSource.adjustment)
        .count()
        == 0
    )
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "account_adjustment")
        .count()
        == 0
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "5000.00"
    )


def test_evidence_inspection_reports_structural_drift(db_session, subscriber):
    _fund(db_session, subscriber, Decimal("5000.00"))
    request = _request(subscriber)
    preview = preview_account_adjustment(
        db_session, PreviewAccountAdjustmentQuery(request=request)
    )
    result = _confirm(
        db_session,
        request,
        preview.fingerprint,
        key="adjustment-drift-inspection-0001",
    )

    result.ledger_entry.currency = "USD"
    db_session.commit()
    report = inspect_account_adjustment_evidence(db_session)

    assert report.drift_count == 1
    assert report.issues[0].adjustment_id == result.adjustment.id
    assert "debit_currency_mismatch" in report.issues[0].issue_codes


def test_generic_ledger_write_and_reversal_api_are_gated(db_session, subscriber):
    with pytest.raises(HTTPException, match="Direct ledger posting is disabled"):
        billing_api.create_ledger_entry(
            LedgerEntryCreate(
                account_id=subscriber.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                category=LedgerCategory.other,
                amount=Decimal("1.00"),
                currency="NGN",
                memo="Bypass attempt",
            ),
            db_session,
        )
    with pytest.raises(HTTPException, match="Direct ledger reversal is disabled"):
        billing_api.reverse_ledger_entry("unused", None, db_session)
