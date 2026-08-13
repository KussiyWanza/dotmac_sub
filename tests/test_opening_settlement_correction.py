"""Exact repair for an invoice already absorbed by a customer opening."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.models.billing import (
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
from app.models.billing_contract import BillingRecordAuthority
from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.customer_subledger import (
    CustomerPositionEffect,
    CustomerPostingGroup,
    CustomerSubledgerAuthorityCutover,
    CustomerSubledgerOpeningPosition,
    PositionEffectKind,
    PostingCommandKind,
    PostingProducer,
    PostingSourceKind,
)
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidOpeningFundingConsumption,
)
from app.services.billing._common import resolve_invoice_settlement_amounts
from app.services.billing.customer_subledger import resolve_position
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    OpeningSettlementCorrectionDisposition,
    OpeningSettlementCorrectionQuery,
    ReconcileOpeningSettlementCorrectionCommand,
    preview_opening_settlement_correction,
    reconcile_opening_settlement_correction,
)
from app.services.prepaid_funding_reconstruction import (
    verified_prepaid_funding_balance,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance

BASELINE_AT = datetime(2026, 7, 20, 7, 58, 22, tzinfo=UTC)
INVOICE_AT = datetime(2026, 7, 22, 12, 3, 16, tzinfo=UTC)
OPENING_AT = datetime(2026, 8, 2, 19, 51, 7, tzinfo=UTC)
PAYMENT_AT = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
CUTOVER_AT = datetime(2026, 8, 2, 20, 15, 25, tzinfo=UTC)


def _verification_run(db) -> BillingCutoverVerificationRun:  # noqa: ANN001
    command_id = uuid4()
    run = BillingCutoverVerificationRun(
        phase="customer_subledger_phase3",
        cohort_name="pytest-opening-correction",
        evidence_schema_version=1,
        policy_version="pytest",
        cutoff_at=OPENING_AT,
        observation_started_at=BASELINE_AT,
        observation_ended_at=OPENING_AT,
        cohort_count=1,
        covered_count=1,
        unresolved_count=0,
        ambiguous_count=0,
        unexpected_unlinked_count=0,
        duplicate_count=0,
        shadow_variance_count=0,
        expected_difference_count=0,
        gap_count=0,
        overlap_count=0,
        source_fingerprint="a" * 64,
        result_fingerprint="b" * 64,
        currency_totals={},
        cohort_classification={},
        event_outcomes={},
        code_version="pytest",
        database_schema_version="523",
        idempotency_key=f"pytest-opening-correction-run:{command_id}",
        command_id=command_id,
        correlation_id=command_id,
        actor="pytest:operator",
        reason="Reviewed test customer opening",
        operator_approved_by="pytest:operator",
        operator_approved_at=OPENING_AT,
        finance_approved_by="pytest:finance",
        finance_approved_at=OPENING_AT,
        created_at=OPENING_AT,
    )
    db.add(run)
    db.flush()
    return run


def _group(
    db,  # noqa: ANN001
    *,
    account_id,
    authority,
    kind,
    producer,
    source_kind,
    source_id,
    occurred_at,
    effects,
) -> CustomerPostingGroup:
    command_id = uuid4()
    group = CustomerPostingGroup(
        account_id=account_id,
        currency="NGN",
        authority=authority,
        command_kind=kind,
        producer_owner=producer.value,
        source_kind=source_kind.value,
        source_id=source_id,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        command_id=command_id,
        correlation_id=command_id,
        idempotency_key=f"pytest-posting:{source_id}:{kind.value}",
        actor="pytest:owner",
        reason="Reviewed test posting",
    )
    db.add(group)
    db.flush()
    for effect, amount, invoice_id, payment_id in effects:
        db.add(
            CustomerPositionEffect(
                group_id=group.id,
                effect=effect,
                amount=amount,
                currency="NGN",
                invoice_id=invoice_id,
                payment_id=payment_id,
            )
        )
    db.flush()
    return group


def _scenario(db, account):  # noqa: ANN001
    materialize_test_prepaid_opening_balance(
        db,
        account.id,
        Decimal("186629.03"),
        position_at=BASELINE_AT,
    )
    baseline = db.query(PrepaidFundingBaseline).filter_by(account_id=account.id).one()
    invoice = Invoice(
        account_id=account.id,
        invoice_number="INV-OPENING-CORRECTION",
        status=InvoiceStatus.partially_paid,
        currency="NGN",
        subtotal=Decimal("140000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("140000.00"),
        balance_due=Decimal("27750.00"),
        issued_at=INVOICE_AT,
        due_at=INVOICE_AT + timedelta(days=31),
        created_at=INVOICE_AT,
        is_proforma=False,
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=None,
            description="Unlimited Platinum",
            quantity=Decimal("1.000"),
            unit_price=Decimal("140000.00"),
            amount=Decimal("140000.00"),
            is_active=True,
        )
    )
    payment = Payment(
        account_id=account.id,
        amount=Decimal("150500.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=PAYMENT_AT,
        created_at=PAYMENT_AT,
        is_active=True,
    )
    db.add(payment)
    db.flush()
    unallocated = LedgerEntry(
        account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("150500.00"),
        currency="NGN",
        memo="Reviewed payment credit",
        effective_date=PAYMENT_AT,
        created_at=PAYMENT_AT,
        is_active=True,
    )
    invoice_entry = LedgerEntry(
        account_id=account.id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("112250.00"),
        currency="NGN",
        memo="Applied payment to invoice",
        effective_date=PAYMENT_AT,
        created_at=PAYMENT_AT,
        affects_customer_position=False,
        is_active=True,
    )
    consumption_entry = LedgerEntry(
        account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.other,
        amount=Decimal("112250.00"),
        currency="NGN",
        memo="Consumed account credit",
        effective_date=PAYMENT_AT,
        created_at=PAYMENT_AT,
        affects_customer_position=False,
        is_active=True,
    )
    db.add_all((unallocated, invoice_entry, consumption_entry))
    db.flush()
    db.add(
        PaymentSettlement(
            payment_id=payment.id,
            unallocated_ledger_entry_id=unallocated.id,
            amount=Decimal("150500.00"),
            unallocated_amount=Decimal("150500.00"),
            prepaid_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.system,
            idempotency_key=f"pytest-opening-settlement:{payment.id}",
            created_at=PAYMENT_AT,
        )
    )
    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=Decimal("112250.00"),
        ledger_entry_id=invoice_entry.id,
        consumption_ledger_entry_id=consumption_entry.id,
        memo="Historical allocation after approved opening",
        preview_fingerprint="c" * 64,
        idempotency_key=f"pytest-opening-allocation:{invoice.id}",
        created_at=PAYMENT_AT,
        is_active=True,
    )
    db.add(allocation)
    db.flush()

    run = _verification_run(db)
    opening_command = uuid4()
    opening = CustomerSubledgerOpeningPosition(
        verification_run_id=run.id,
        baseline_id=baseline.id,
        account_id=account.id,
        currency="NGN",
        legacy_position=Decimal("46629.03"),
        shadow_position_before=Decimal("0.00"),
        opening_delta=Decimal("46629.03"),
        evidence_fingerprint="d" * 64,
        review_reference="pytest:approved-opening",
        captured_by="pytest:finance",
        command_id=opening_command,
        correlation_id=opening_command,
        occurred_at=OPENING_AT,
        created_at=OPENING_AT,
    )
    db.add(opening)
    db.flush()
    _group(
        db,
        account_id=account.id,
        authority=BillingRecordAuthority.shadow,
        kind=PostingCommandKind.opening_position,
        producer=PostingProducer.customer_subledger_opening_positions,
        source_kind=PostingSourceKind.customer_subledger_opening_position,
        source_id=opening.id,
        occurred_at=OPENING_AT,
        effects=(
            (
                PositionEffectKind.customer_credit_created,
                Decimal("46629.03"),
                None,
                None,
            ),
        ),
    )
    cutover_command = uuid4()
    db.add(
        CustomerSubledgerAuthorityCutover(
            verification_run_id=run.id,
            result_fingerprint="e" * 64,
            review_reference="pytest:approved-cutover",
            activated_by="pytest:operator",
            command_id=cutover_command,
            correlation_id=cutover_command,
            cutover_at=CUTOVER_AT,
        )
    )
    _group(
        db,
        account_id=account.id,
        authority=BillingRecordAuthority.authoritative,
        kind=PostingCommandKind.payment_settlement,
        producer=PostingProducer.payment_proofs,
        source_kind=PostingSourceKind.payment,
        source_id=payment.id,
        occurred_at=PAYMENT_AT,
        effects=(
            (
                PositionEffectKind.customer_credit_created,
                Decimal("150500.00"),
                None,
                payment.id,
            ),
        ),
    )
    application_group = _group(
        db,
        account_id=account.id,
        authority=BillingRecordAuthority.authoritative,
        kind=PostingCommandKind.customer_credit_application,
        producer=PostingProducer.account_credit_applications,
        source_kind=PostingSourceKind.payment_allocation,
        source_id=allocation.id,
        occurred_at=PAYMENT_AT,
        effects=(
            (
                PositionEffectKind.customer_credit_consumed,
                Decimal("112250.00"),
                None,
                payment.id,
            ),
            (
                PositionEffectKind.receivable_settled,
                Decimal("112250.00"),
                invoice.id,
                None,
            ),
        ),
    )
    db.commit()
    return invoice, allocation, application_group


def test_reviewed_preopening_invoice_correction_is_exact_and_idempotent(
    db_session,
    subscriber,
):
    invoice, allocation, application_group = _scenario(db_session, subscriber)
    invoice_id = invoice.id
    allocation_id = allocation.id
    query = OpeningSettlementCorrectionQuery(
        invoice_id=invoice_id,
        allocation_id=allocation_id,
        expected_confirmed_balance=Decimal("197129.03"),
    )

    preview = preview_opening_settlement_correction(db_session, query)

    assert preview.disposition is (
        OpeningSettlementCorrectionDisposition.exact_preopening_double_application
    )
    assert preview.actionable is True
    assert preview.invoice_total == Decimal("140000.00")
    assert preview.balance_due == Decimal("27750.00")
    assert preview.allocated_amount == Decimal("112250.00")
    assert preview.confirmed_balance == Decimal("197129.03")
    assert preview.subledger_credit_before == Decimal("84879.03")
    assert preview.subledger_receivable_before == Decimal("-112250.00")
    db_session.commit()

    command = ReconcileOpeningSettlementCorrectionCommand(
        context=CommandContext.system(
            actor="pytest:billing-operator",
            scope="prepaid_draft_reconciliation",
            reason="Reviewed invoice already absorbed by approved opening",
            idempotency_key=f"pytest-opening-correction:{invoice_id}",
        ),
        query=query,
        preview_fingerprint=preview.fingerprint,
        effective_at=datetime(2026, 8, 12, 15, 34, 20, tzinfo=UTC),
    )
    result = reconcile_opening_settlement_correction(db_session, command)
    replay = reconcile_opening_settlement_correction(db_session, command)

    db_session.refresh(invoice)
    db_session.refresh(allocation)
    settlement = resolve_invoice_settlement_amounts(db_session, invoice.id)
    position = resolve_position(
        db_session,
        account_id=subscriber.id,
        currency="NGN",
    )
    assert result.replayed is False
    assert replay.replayed is True
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert allocation.is_active is False
    assert settlement.payments_applied == Decimal("0.00")
    assert settlement.opening_funding_applied == Decimal("140000.00")
    assert db_session.query(PrepaidOpeningFundingConsumption).one().amount == Decimal(
        "140000.00"
    )
    assert len(result.ledger_reversal_ids) == 2
    assert (
        db_session.query(CustomerPostingGroup)
        .filter(CustomerPostingGroup.reverses_group_id == application_group.id)
        .count()
        == 1
    )
    assert position.unapplied_customer_credit == Decimal("197129.03")
    assert position.collectible_receivable == Decimal("0.00")
    assert verified_prepaid_funding_balance(db_session, subscriber.id) == Decimal(
        "197129.03"
    )


def test_preopening_correction_fails_closed_when_opening_does_not_reconstruct(
    db_session,
    subscriber,
):
    invoice, allocation, _group_row = _scenario(db_session, subscriber)
    opening = db_session.query(CustomerSubledgerOpeningPosition).one()
    opening.legacy_position = Decimal("46630.03")
    opening.opening_delta = Decimal("46630.03")
    db_session.commit()

    preview = preview_opening_settlement_correction(
        db_session,
        OpeningSettlementCorrectionQuery(
            invoice_id=invoice.id,
            allocation_id=allocation.id,
            expected_confirmed_balance=Decimal("197129.03"),
        ),
    )

    assert preview.disposition is OpeningSettlementCorrectionDisposition.manual_review
    assert preview.actionable is False
    assert "opening does not exactly reconstruct" in preview.reason
