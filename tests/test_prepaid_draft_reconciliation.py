from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.models.billing import (
    AccountAdjustment,
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
    ServiceEntitlement,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.prepaid_funding import (
    PrepaidDraftReconciliationException,
    PrepaidOpeningFundingConsumption,
)
from app.services.customer_financial_position import prepaid_available_balance
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    PrepaidDraftAction,
    PrepaidDraftDisposition,
    ReconcilePrepaidDraftCommand,
    preview_prepaid_draft_cohort,
    preview_prepaid_draft_reconciliation,
    reconcile_prepaid_draft_invoice,
)
from app.services.prepaid_service_renewals import (
    FundingChangeRenewalDisposition,
    apply_due_prepaid_service_after_funding_change,
    confirm_prepaid_service_renewal,
    preview_prepaid_service_renewal,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance

START = datetime(2026, 7, 17, tzinfo=UTC)
END = datetime(2026, 8, 17, tzinfo=UTC)


def _draft(db, account, subscription, *, total: Decimal) -> Invoice:
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = START
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-DRAFT-{uuid4().hex[:8]}",
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=total,
        tax_total=Decimal("0.00"),
        total=total,
        balance_due=total,
        billing_period_start=START,
        billing_period_end=END,
        is_proforma=False,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Prepaid service",
            quantity=Decimal("1.000"),
            unit_price=total,
            amount=total,
            is_active=True,
        )
    )
    db.commit()
    return invoice


def _payment(
    db,
    account,
    *,
    amount: Decimal,
    settlement_amount: Decimal | None = None,
    provider_fee: Decimal = Decimal("0.00"),
    paid_at: datetime = datetime(2026, 7, 23, 10, tzinfo=UTC),
):
    canonical_credit = settlement_amount if settlement_amount is not None else amount
    payment = Payment(
        account_id=account.id,
        amount=amount,
        provider_fee=provider_fee,
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        is_active=True,
        created_at=paid_at,
    )
    db.add(payment)
    db.flush()
    entry = LedgerEntry(
        account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=canonical_credit,
        currency="NGN",
        memo="Reviewed test payment",
        is_active=True,
        effective_date=paid_at,
        created_at=paid_at,
    )
    db.add(entry)
    db.flush()
    db.add(
        PaymentSettlement(
            payment_id=payment.id,
            unallocated_ledger_entry_id=entry.id,
            amount=canonical_credit,
            unallocated_amount=canonical_credit,
            prepaid_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.system,
            idempotency_key=f"pytest-prepaid-draft-payment-{payment.id}",
            created_at=paid_at,
        )
    )
    db.commit()
    return payment


def test_fee_inclusive_mixed_source_uses_participant_remainder(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("7421.37"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("522.25"),
    )
    payment = _payment(
        db_session,
        subscriber,
        amount=Decimal("7012.84"),
        provider_fee=Decimal("113.72"),
        settlement_amount=Decimal("6899.12"),
    )

    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)

    assert payment.amount == Decimal("7012.84")
    assert payment.provider_fee == Decimal("113.72")
    assert payment.settlement.amount == Decimal("6899.12")
    assert payment.settlement.unallocated_amount == Decimal("6899.12")
    assert preview.payment_backed_credit == Decimal("6899.12")
    assert preview.opening_funding_required == Decimal("522.25")
    assert preview.disposition is PrepaidDraftDisposition.reviewed_opening_fundable
    invoice_id = invoice.id
    preview_fingerprint = preview.fingerprint
    db_session.commit()

    result = reconcile_prepaid_draft_invoice(
        db_session,
        _command(
            invoice_id,
            preview_fingerprint,
            key=f"pytest-fee-inclusive-mixed-{invoice_id}",
        ),
    )

    db_session.refresh(invoice)
    assert result.payment_applied_amount == Decimal("6899.12")
    assert result.opening_funding_applied_amount == Decimal("522.25")
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert db_session.query(PaymentAllocation).one().amount == Decimal("6899.12")
    assert db_session.query(PrepaidOpeningFundingConsumption).one().amount == Decimal(
        "522.25"
    )


def _command(invoice_id, fingerprint: str, *, key: str):
    return ReconcilePrepaidDraftCommand(
        context=CommandContext.system(
            actor="pytest:billing-operator",
            scope="prepaid_draft_reconciliation",
            reason="Reviewed prepaid draft reconciliation",
            idempotency_key=key,
        ),
        invoice_id=invoice_id,
        preview_fingerprint=fingerprint,
        effective_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
    )


def test_fifty_kobo_shortfall_stays_draft(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("18812.50"),
    )
    _payment(db_session, subscriber, amount=Decimal("18812.00"))

    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidDraftDisposition.insufficient_funding
    assert preview.recommended_action is PrepaidDraftAction.none
    assert preview.payment_backed_credit == Decimal("18812.00")
    assert preview.shortfall == Decimal("0.50")

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:fifty-kobo-short",
    )
    db_session.commit()

    db_session.refresh(invoice)
    assert result.disposition is FundingChangeRenewalDisposition.draft_invoice_pending
    assert result.draft_invoices_pending == 1
    assert invoice.status is InvoiceStatus.draft
    assert invoice.issued_at is None
    assert db_session.query(PaymentAllocation).count() == 0
    assert db_session.query(AccountAdjustment).count() == 0
    assert db_session.query(ServiceEntitlement).count() == 0


def test_reviewed_opening_funding_settles_exact_remainder_atomically(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("18812.50"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("2000.00"),
    )
    _payment(db_session, subscriber, amount=Decimal("16812.50"))

    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidDraftDisposition.reviewed_opening_fundable
    assert preview.payment_backed_credit == Decimal("16812.50")
    assert preview.opening_funding_required == Decimal("2000.00")
    assert preview.authoritative_funding == Decimal("18812.50")
    invoice_id = invoice.id
    db_session.commit()

    command = _command(
        invoice_id,
        preview.fingerprint,
        key=f"pytest-prepaid-opening-{invoice_id}",
    )
    result = reconcile_prepaid_draft_invoice(db_session, command)
    replay = reconcile_prepaid_draft_invoice(db_session, command)

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    consumption = db_session.query(PrepaidOpeningFundingConsumption).one()
    entitlement = db_session.query(ServiceEntitlement).one()
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert result.payment_applied_amount == Decimal("16812.50")
    assert result.opening_funding_applied_amount == Decimal("2000.00")
    assert result.opening_funding_consumption_id == consumption.id
    assert replay.replayed is True
    assert db_session.query(PaymentAllocation).count() == 1
    assert db_session.query(PrepaidOpeningFundingConsumption).count() == 1
    assert consumption.invoice_id == invoice.id
    assert consumption.baseline_id == preview.opening_funding_baseline_id
    assert consumption.ledger_entry.invoice_id == invoice.id
    assert consumption.ledger_entry.affects_customer_position is False
    assert entitlement.source_invoice_id == invoice.id
    # ADR 0007 Phase 3 forward-shadow: exactly one posting group per
    # opening-funding consumption, replay-safe, at the deciding owner.
    from app.models.customer_subledger import (
        CustomerPostingGroup,
        PositionEffectKind,
        PostingCommandKind,
    )

    groups = (
        db_session.query(CustomerPostingGroup)
        .filter(
            CustomerPostingGroup.producer_owner
            == "financial.prepaid_draft_reconciliation"
        )
        .all()
    )
    assert len(groups) == 1
    assert groups[0].command_kind is PostingCommandKind.prepaid_consumption
    assert groups[0].source_id == consumption.id
    assert groups[0].authority.value == "shadow"
    assert [e.effect for e in groups[0].effects] == [
        PositionEffectKind.prepaid_funding_consumed
    ]
    assert Decimal(str(groups[0].effects[0].amount)) == Decimal("2000.00")
    assert subscription.next_billing_at == entitlement.ends_at
    assert prepaid_available_balance(db_session, subscriber.id) == Decimal("0.00")


def test_prebaseline_credit_is_absorbed_by_reviewed_opening_boundary(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("18812.50"),
    )
    prebaseline_payment = _payment(
        db_session,
        subscriber,
        amount=Decimal("5000.00"),
        paid_at=datetime(2026, 3, 10, 10, tzinfo=UTC),
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.other,
            amount=Decimal("118760.64"),
            currency="NGN",
            memo="Pre-cutover mirror residue",
            affects_customer_position=True,
            is_active=True,
            effective_date=datetime(2026, 3, 11, 10, tzinfo=UTC),
            created_at=datetime(2026, 3, 11, 10, tzinfo=UTC),
        )
    )
    db_session.commit()
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("18760.64"),
    )
    postbaseline_payment = _payment(
        db_session,
        subscriber,
        amount=Decimal("2000.00"),
    )

    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidDraftDisposition.reviewed_opening_fundable
    assert preview.recommended_action is PrepaidDraftAction.settle_paid
    assert preview.payment_backed_credit == Decimal("2000.00")
    assert preview.opening_funding_available == Decimal("18760.64")
    assert preview.opening_funding_required == Decimal("16812.50")
    assert preview.unbacked_credit == Decimal("0.00")
    assert preview.shortfall == Decimal("16812.50")
    invoice_id = invoice.id
    prebaseline_payment_id = prebaseline_payment.id
    postbaseline_payment_id = postbaseline_payment.id
    db_session.commit()

    result = reconcile_prepaid_draft_invoice(
        db_session,
        _command(
            invoice_id,
            preview.fingerprint,
            key=f"pytest-prepaid-boundary-{invoice_id}",
        ),
    )

    db_session.refresh(invoice)
    allocations = db_session.query(PaymentAllocation).all()
    consumption = db_session.query(PrepaidOpeningFundingConsumption).one()
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert result.payment_applied_amount == Decimal("2000.00")
    assert result.opening_funding_applied_amount == Decimal("16812.50")
    assert consumption.amount == Decimal("16812.50")
    assert len(allocations) == 1
    assert allocations[0].payment_id == postbaseline_payment_id
    assert allocations[0].payment_id != prebaseline_payment_id
    assert prepaid_available_balance(db_session, subscriber.id) == Decimal("1948.14")


def test_opening_funding_shortfall_stays_unmodified(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("18812.50"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("1000.00"),
    )
    _payment(db_session, subscriber, amount=Decimal("16812.50"))

    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)
    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:opening-shortfall",
    )
    db_session.commit()

    assert preview.disposition is PrepaidDraftDisposition.insufficient_funding
    assert result.disposition is FundingChangeRenewalDisposition.draft_invoice_pending
    assert db_session.query(PaymentAllocation).count() == 0
    assert db_session.query(PrepaidOpeningFundingConsumption).count() == 0
    assert db_session.query(PrepaidDraftReconciliationException).count() == 0


def test_lapsed_opening_funded_invoice_reanchors_coverage_to_effective_date(
    db_session,
    subscriber,
    subscription,
):
    effective_at = datetime(2026, 7, 23, 10, tzinfo=UTC)
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    invoice.billing_period_start = datetime(2026, 5, 1, tzinfo=UTC)
    invoice.billing_period_end = datetime(2026, 6, 1, tzinfo=UTC)
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
    )
    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)
    invoice_id = invoice.id
    db_session.commit()

    reconcile_prepaid_draft_invoice(
        db_session,
        ReconcilePrepaidDraftCommand(
            context=CommandContext.system(
                actor="pytest:billing-operator",
                scope="prepaid_draft_reconciliation",
                reason="Reviewed lapsed prepaid reconciliation",
                idempotency_key=f"pytest-lapsed-opening-{invoice_id}",
            ),
            invoice_id=invoice_id,
            preview_fingerprint=preview.fingerprint,
            effective_at=effective_at,
        ),
    )

    db_session.refresh(invoice)
    db_session.refresh(subscription)
    entitlement = db_session.query(ServiceEntitlement).one()
    assert invoice.billing_period_start == datetime(2026, 7, 22, 23)
    assert invoice.billing_period_end == datetime(2026, 8, 22, 23)
    assert (
        invoice.billing_period_start.replace(tzinfo=UTC)
        .astimezone(ZoneInfo("Africa/Lagos"))
        .date()
        .isoformat()
        == "2026-07-23"
    )
    assert (
        invoice.billing_period_end.replace(tzinfo=UTC)
        .astimezone(ZoneInfo("Africa/Lagos"))
        .date()
        .isoformat()
        == "2026-08-23"
    )
    assert entitlement.starts_at == invoice.billing_period_start
    assert entitlement.ends_at == invoice.billing_period_end
    assert subscription.next_billing_at == entitlement.ends_at


def test_funding_event_creates_review_exception_without_spending_opening_funding(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("18812.50"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("2000.00"),
    )
    _payment(db_session, subscriber, amount=Decimal("16812.50"))

    first = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:opening-review-required",
    )
    second = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:opening-review-required-retry",
    )
    db_session.commit()

    exception = db_session.query(PrepaidDraftReconciliationException).one()
    assert (
        first.disposition
        is FundingChangeRenewalDisposition.draft_invoice_review_required
    )
    assert (
        second.disposition
        is FundingChangeRenewalDisposition.draft_invoice_review_required
    )
    assert exception.invoice_id == invoice.id
    assert exception.status == "open"
    assert exception.opening_funding_amount == Decimal("2000.00")
    assert exception.attempt_count == 1
    assert db_session.query(PaymentAllocation).count() == 0
    assert db_session.query(PrepaidOpeningFundingConsumption).count() == 0


def test_multiple_drafts_fail_closed_without_exception_or_funding_consumption(
    db_session,
    subscriber,
    subscription,
):
    first = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    second = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("200.00"),
    )

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:multiple-drafts",
    )
    db_session.commit()

    db_session.refresh(first)
    db_session.refresh(second)
    assert result.disposition is FundingChangeRenewalDisposition.draft_invoice_pending
    assert result.draft_invoices_pending == 2
    assert first.status is InvoiceStatus.draft
    assert second.status is InvoiceStatus.draft
    assert db_session.query(PaymentAllocation).count() == 0
    assert db_session.query(PrepaidOpeningFundingConsumption).count() == 0
    assert db_session.query(PrepaidDraftReconciliationException).count() == 0


def test_consumed_opening_funding_cannot_fund_a_second_invoice(
    db_session,
    subscriber,
    subscription,
):
    first_invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
    )
    first_preview = preview_prepaid_draft_reconciliation(
        db_session,
        first_invoice.id,
    )
    first_invoice_id = first_invoice.id
    db_session.commit()
    reconcile_prepaid_draft_invoice(
        db_session,
        _command(
            first_invoice_id,
            first_preview.fingerprint,
            key=f"pytest-opening-first-{first_invoice_id}",
        ),
    )

    second_invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    second_invoice.billing_period_start = datetime(2026, 9, 1, tzinfo=UTC)
    second_invoice.billing_period_end = datetime(2026, 10, 1, tzinfo=UTC)
    db_session.commit()

    second_preview = preview_prepaid_draft_reconciliation(
        db_session,
        second_invoice.id,
    )

    assert second_preview.opening_funding_available == Decimal("0.00")
    assert second_preview.recommended_action is PrepaidDraftAction.none
    assert db_session.query(PrepaidOpeningFundingConsumption).count() == 1


def test_reversed_payment_is_not_reused_with_opening_funding(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("3000.00"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("2000.00"),
    )
    payment = _payment(db_session, subscriber, amount=Decimal("1000.00"))
    payment.status = PaymentStatus.reversed
    db_session.commit()

    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)

    assert preview.payment_backed_credit == Decimal("0.00")
    # A status-only reversal leaves unmatched ledger credit. The reconciler
    # quarantines that ambiguity instead of combining it with opening funding.
    assert preview.opening_funding_available == Decimal("0.00")
    assert preview.unbacked_credit == Decimal("1000.00")
    assert preview.disposition is PrepaidDraftDisposition.legacy_unbacked_funding


def test_cohort_deduplicates_invoice_with_multiple_prepaid_lines(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Additional prepaid line",
            quantity=Decimal("1.000"),
            unit_price=Decimal("1.00"),
            amount=Decimal("1.00"),
            is_active=True,
        )
    )
    db_session.commit()

    previews = preview_prepaid_draft_cohort(
        db_session,
        account_id=subscriber.id,
    )

    assert tuple(preview.invoice_id for preview in previews) == (invoice.id,)


def test_legacy_unbacked_credit_is_separated_from_native_shortfall(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("18812.50"),
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.other,
            amount=Decimal("18812.00"),
            currency="NGN",
            memo="Legacy wallet projection",
            is_active=True,
        )
    )
    db_session.commit()

    preview = preview_prepaid_draft_reconciliation(db_session, invoice.id)

    assert preview.disposition is PrepaidDraftDisposition.legacy_unbacked_funding
    assert preview.recommended_action is PrepaidDraftAction.none
    assert preview.payment_backed_credit == Decimal("0.00")
    assert preview.unbacked_credit == Decimal("18812.00")
    assert preview.shortfall == Decimal("18812.50")


def test_funding_change_settles_exact_existing_draft_before_direct_renewal(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("18812.50"),
    )
    _payment(db_session, subscriber, amount=Decimal("18812.50"))

    result = apply_due_prepaid_service_after_funding_change(
        db_session,
        account_id=subscriber.id,
        effective_at=datetime(2026, 7, 23, 10, tzinfo=UTC),
        funding_currency="NGN",
        evidence_ref="pytest:exact-draft-first",
    )
    db_session.commit()

    db_session.refresh(invoice)
    assert result.disposition is FundingChangeRenewalDisposition.draft_invoice_settled
    assert result.draft_invoices_settled == 1
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert db_session.query(PaymentAllocation).count() == 1
    entitlement = db_session.query(ServiceEntitlement).one()
    assert entitlement.source_invoice_id == invoice.id
    assert db_session.query(AccountAdjustment).count() == 0


def test_reviewed_exact_funding_command_is_atomic_and_replay_safe(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    _payment(db_session, subscriber, amount=Decimal("100.00"))
    invoice_id = invoice.id
    preview = preview_prepaid_draft_reconciliation(db_session, invoice_id)
    assert preview.disposition is PrepaidDraftDisposition.exact_payment_fundable
    db_session.commit()

    command = _command(
        invoice_id,
        preview.fingerprint,
        key=f"pytest-prepaid-draft-{invoice_id}",
    )
    first = reconcile_prepaid_draft_invoice(db_session, command)
    replay = reconcile_prepaid_draft_invoice(db_session, command)

    db_session.refresh(invoice)
    assert first.action is PrepaidDraftAction.settle_paid
    assert first.applied_amount == Decimal("100.00")
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.invoice_id == first.invoice_id
    assert invoice.status is InvoiceStatus.paid
    assert db_session.query(PaymentAllocation).count() == 1


def test_reviewed_command_rejects_insufficient_funding_without_mutation(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.50"),
    )
    _payment(db_session, subscriber, amount=Decimal("100.00"))
    invoice_id = invoice.id
    preview = preview_prepaid_draft_reconciliation(db_session, invoice_id)
    db_session.commit()

    with pytest.raises(DomainError) as exc_info:
        reconcile_prepaid_draft_invoice(
            db_session,
            _command(
                invoice_id,
                preview.fingerprint,
                key=f"pytest-prepaid-draft-short-{invoice_id}",
            ),
        )

    assert exc_info.value.code.endswith("not_actionable")
    db_session.refresh(invoice)
    assert invoice.status is InvoiceStatus.draft
    assert db_session.query(PaymentAllocation).count() == 0


def test_exact_direct_renewal_overlap_voids_duplicate_without_second_charge(
    db_session,
    subscriber,
    subscription,
):
    invoice = _draft(
        db_session,
        subscriber,
        subscription,
        total=Decimal("100.00"),
    )
    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber.id,
        Decimal("100.00"),
        position_at=datetime(2026, 7, 16, tzinfo=UTC),
    )
    renewal_preview = preview_prepaid_service_renewal(
        db_session,
        subscription_id=subscription.id,
        starts_at=START,
        ends_at=END,
        amount=Decimal("100.00"),
        currency="NGN",
    )
    renewal = confirm_prepaid_service_renewal(
        db_session,
        renewal_preview,
        evidence_ref="pytest:historical-direct-renewal",
    )
    db_session.commit()

    invoice_id = invoice.id
    preview = preview_prepaid_draft_reconciliation(db_session, invoice_id)
    assert preview.disposition is PrepaidDraftDisposition.already_renewed
    assert preview.recommended_action is PrepaidDraftAction.void_duplicate
    assert preview.entitlement_ids == (renewal.entitlement.id,)
    db_session.commit()

    result = reconcile_prepaid_draft_invoice(
        db_session,
        _command(
            invoice_id,
            preview.fingerprint,
            key=f"pytest-prepaid-draft-overlap-{invoice_id}",
        ),
    )

    db_session.refresh(invoice)
    assert result.action is PrepaidDraftAction.void_duplicate
    assert result.applied_amount == Decimal("0.00")
    assert invoice.status is InvoiceStatus.void
    assert db_session.query(AccountAdjustment).count() == 1
    assert db_session.query(ServiceEntitlement).count() == 1
    assert db_session.query(PaymentAllocation).count() == 0
