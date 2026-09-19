from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceClosure,
    InvoiceClosureOrigin,
    InvoiceClosureType,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.models.event_store import EventStore
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services.billing._common import get_spendable_account_credit_balance
from app.services.billing.account_credit import AccountCreditApplications
from app.services.billing.invoices import Invoices
from app.services.billing.payments import PaymentAllocations
from app.services.events.types import EventType
from app.services.historical_invoice_tax_corrections import (
    CORRECTION_SCOPE,
    CorrectHistoricalInvoiceTaxCommand,
    HistoricalInvoiceTaxCorrectionDisposition,
    HistoricalInvoiceTaxCorrectionError,
    HistoricalInvoiceTaxCorrectionQuery,
    correct_historical_invoice_tax,
    preview_historical_invoice_tax_correction,
)
from app.services.owner_commands import CommandContext


@dataclass(frozen=True, slots=True)
class _Scenario:
    account_id: UUID
    source_invoice_id: UUID
    source_line_id: UUID
    void_evidence_invoice_id: UUID
    subscription_invoice_id: UUID
    payment_id: UUID
    tax_rate_id: UUID
    query: HistoricalInvoiceTaxCorrectionQuery


def _scenario(db_session, subscriber) -> _Scenario:
    issued_at = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    tax_rate = TaxRate(
        name=f"VAT-{uuid4().hex[:8]}",
        code="VAT",
        rate=Decimal("7.5000"),
        is_active=True,
    )
    source = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-SOURCE-{uuid4().hex[:8]}",
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("200000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("200000.00"),
        balance_due=Decimal("200000.00"),
        issued_at=issued_at,
    )
    void_evidence = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-EVIDENCE-{uuid4().hex[:8]}",
        status=InvoiceStatus.void,
        currency="NGN",
        subtotal=Decimal("200000.00"),
        tax_total=Decimal("15000.00"),
        total=Decimal("215000.00"),
        balance_due=Decimal("0.00"),
        issued_at=issued_at,
    )
    subscription = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-SUBSCRIPTION-{uuid4().hex[:8]}",
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("17500.00"),
        tax_total=Decimal("1312.50"),
        total=Decimal("18812.50"),
        balance_due=Decimal("18812.50"),
    )
    db_session.add_all([tax_rate, source, void_evidence, subscription])
    db_session.flush()

    source_line = InvoiceLine(
        invoice_id=source.id,
        description="Air Fiber Installation",
        quantity=Decimal("1.000"),
        unit_price=Decimal("200000.00"),
        amount=Decimal("200000.00"),
        tax_application=TaxApplication.exclusive,
        is_active=True,
    )
    evidence_line = InvoiceLine(
        invoice_id=void_evidence.id,
        description="Air Fiber Installation",
        quantity=Decimal("1.000"),
        unit_price=Decimal("200000.00"),
        amount=Decimal("200000.00"),
        tax_rate_id=tax_rate.id,
        tax_rate_snapshot_version=1,
        tax_rate_code_snapshot="VAT",
        tax_rate_percent_snapshot=Decimal("7.5000"),
        tax_rate_is_active_snapshot=True,
        tax_application=TaxApplication.exclusive,
        is_active=True,
    )
    subscription_line = InvoiceLine(
        invoice_id=subscription.id,
        description="Unlimited Basic",
        quantity=Decimal("1.000"),
        unit_price=Decimal("17500.00"),
        amount=Decimal("17500.00"),
        tax_rate_id=tax_rate.id,
        tax_rate_snapshot_version=1,
        tax_rate_code_snapshot="VAT",
        tax_rate_percent_snapshot=Decimal("7.5000"),
        tax_rate_is_active_snapshot=True,
        tax_application=TaxApplication.exclusive,
        is_active=True,
    )
    db_session.add_all([source_line, evidence_line, subscription_line])
    db_session.add(
        InvoiceClosure(
            invoice_id=void_evidence.id,
            closure_type=InvoiceClosureType.void,
            origin=InvoiceClosureOrigin.manual,
            amount=Decimal("0.00"),
            receivable_before=Decimal("0.00"),
            receivable_after=Decimal("0.00"),
            payments_applied=Decimal("0.00"),
            credits_applied=Decimal("0.00"),
            currency="NGN",
            reason="Reviewed Finance draft was voided",
        )
    )
    db_session.commit()

    payment = billing_service.payments.create(
        db_session,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal("233812.50"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            external_id=f"test-historical-tax-{uuid4()}",
        ),
        auto_allocate=False,
    )
    funding_preview = AccountCreditApplications.preview_invoice_funding(
        db_session, source
    )
    AccountCreditApplications.apply_invoice_fully(
        db_session,
        source,
        preview_fingerprint=funding_preview.fingerprint,
    )
    db_session.commit()
    db_session.refresh(source)
    db_session.refresh(source_line)
    assert source.status is InvoiceStatus.paid

    query = HistoricalInvoiceTaxCorrectionQuery(
        account_id=subscriber.id,
        source_invoice_id=source.id,
        source_invoice_line_id=source_line.id,
        void_evidence_invoice_id=void_evidence.id,
        subscription_invoice_id=subscription.id,
        payment_id=payment.id,
        tax_rate_id=tax_rate.id,
        issued_at=issued_at,
        due_at=issued_at + timedelta(days=30),
    )
    return _Scenario(
        account_id=subscriber.id,
        source_invoice_id=source.id,
        source_line_id=source_line.id,
        void_evidence_invoice_id=void_evidence.id,
        subscription_invoice_id=subscription.id,
        payment_id=payment.id,
        tax_rate_id=tax_rate.id,
        query=query,
    )


def _context(*, key: str = "historical-tax-correction-test-key") -> CommandContext:
    return CommandContext.system(
        actor="test-finance-operator",
        scope=CORRECTION_SCOPE,
        reason="Correct omitted VAT using the exact reviewed payment evidence",
        idempotency_key=key,
    )


def _command(
    scenario: _Scenario,
    fingerprint: str,
    *,
    permission_granted: bool = True,
) -> CorrectHistoricalInvoiceTaxCommand:
    return CorrectHistoricalInvoiceTaxCommand(
        query=scenario.query,
        expected_preview_fingerprint=fingerprint,
        permission_granted=permission_granted,
        authorized_system_user_id=uuid4(),
    )


def test_exact_payment_correction_is_atomic_and_idempotent(db_session, subscriber):
    scenario = _scenario(db_session, subscriber)

    preview = preview_historical_invoice_tax_correction(db_session, scenario.query)

    assert preview.disposition is HistoricalInvoiceTaxCorrectionDisposition.eligible
    assert preview.source_subtotal == Decimal("200000.00")
    assert preview.subscription_total == Decimal("18812.50")
    assert preview.tax_amount == Decimal("15000.00")
    assert preview.replacement_total == Decimal("215000.00")
    assert preview.payment_available_before == Decimal("33812.50")
    assert preview.payment_available_after_void == Decimal("233812.50")
    assert preview.source_payment_allocation_id is not None
    assert preview.projected_final_payment_available == Decimal("0.00")
    assert preview.projected_final_account_credit == Decimal("0.00")

    db_session.rollback()
    context = _context()
    command = _command(scenario, preview.fingerprint)
    result = correct_historical_invoice_tax(db_session, command, context=context)

    source = db_session.get(Invoice, scenario.source_invoice_id)
    subscription = db_session.get(Invoice, scenario.subscription_invoice_id)
    replacement = db_session.get(Invoice, result.replacement_invoice_id)
    payment = db_session.get(Payment, scenario.payment_id)
    assert source is not None and source.status is InvoiceStatus.void
    assert subscription is not None and subscription.status is InvoiceStatus.paid
    assert subscription.balance_due == Decimal("0.00")
    assert replacement is not None and replacement.status is InvoiceStatus.paid
    assert replacement.subtotal == Decimal("200000.00")
    assert replacement.tax_total == Decimal("15000.00")
    assert replacement.total == Decimal("215000.00")
    assert replacement.balance_due == Decimal("0.00")
    assert payment is not None
    assert PaymentAllocations.available_amount(db_session, str(payment.id)) == Decimal(
        "0.00"
    )
    assert get_spendable_account_credit_balance(
        db_session,
        str(scenario.account_id),
        currency="NGN",
    ) == Decimal("0.00")

    evidence = Invoices.historical_tax_correction_evidence(replacement)
    assert evidence is not None
    assert evidence.source_invoice_id == source.id
    assert evidence.source_payment_allocation_id == result.source_payment_allocation_id
    assert evidence.payment_id == payment.id
    assert evidence.subscription_total == Decimal("18812.50")
    assert evidence.tax_amount == Decimal("15000.00")
    allocations = tuple(
        db_session.scalars(
            select(PaymentAllocation)
            .where(
                PaymentAllocation.payment_id == payment.id,
                PaymentAllocation.is_active.is_(True),
            )
            .order_by(PaymentAllocation.amount)
        ).all()
    )
    assert tuple(allocation.amount for allocation in allocations) == (
        Decimal("18812.50"),
        Decimal("215000.00"),
    )
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "correct_historical_invoice_tax")
        .filter(AuditEvent.entity_id == str(source.id))
        .count()
        == 1
    )
    assert (
        db_session.query(EventStore)
        .filter(
            EventStore.event_type == EventType.invoice_tax_correction_completed.value
        )
        .count()
        == 1
    )

    db_session.rollback()
    replay = correct_historical_invoice_tax(db_session, command, context=context)
    assert replay.replayed is True
    assert replay.replacement_invoice_id == result.replacement_invoice_id


def test_permission_denial_preserves_all_reviewed_documents(db_session, subscriber):
    scenario = _scenario(db_session, subscriber)
    preview = preview_historical_invoice_tax_correction(db_session, scenario.query)
    db_session.rollback()

    with pytest.raises(HistoricalInvoiceTaxCorrectionError) as exc:
        correct_historical_invoice_tax(
            db_session,
            _command(scenario, preview.fingerprint, permission_granted=False),
            context=_context(key="historical-tax-permission-denied"),
        )

    assert exc.value.code.endswith("permission_denied")
    source = db_session.get(Invoice, scenario.source_invoice_id)
    subscription = db_session.get(Invoice, scenario.subscription_invoice_id)
    assert source is not None and source.status is InvoiceStatus.paid
    assert subscription is not None and subscription.status is InvoiceStatus.draft
    assert (
        db_session.query(Invoice)
        .filter(Invoice.account_id == scenario.account_id)
        .count()
        == 3
    )


def test_event_staging_failure_rolls_back_the_complete_correction(
    db_session, subscriber, monkeypatch
):
    scenario = _scenario(db_session, subscriber)
    preview = preview_historical_invoice_tax_correction(db_session, scenario.query)
    db_session.rollback()

    def _fail_event(*_args, **_kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(
        "app.services.historical_invoice_tax_corrections.emit_event",
        _fail_event,
    )
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        correct_historical_invoice_tax(
            db_session,
            _command(scenario, preview.fingerprint),
            context=_context(key="historical-tax-event-rollback"),
        )

    source = db_session.get(Invoice, scenario.source_invoice_id)
    subscription = db_session.get(Invoice, scenario.subscription_invoice_id)
    assert source is not None and source.status is InvoiceStatus.paid
    assert subscription is not None and subscription.status is InvoiceStatus.draft
    assert (
        db_session.query(Invoice)
        .filter(Invoice.account_id == scenario.account_id)
        .count()
        == 3
    )
    active_allocation = db_session.scalar(
        select(PaymentAllocation).where(
            PaymentAllocation.payment_id == scenario.payment_id,
            PaymentAllocation.invoice_id == scenario.source_invoice_id,
            PaymentAllocation.is_active.is_(True),
        )
    )
    assert active_allocation is not None
