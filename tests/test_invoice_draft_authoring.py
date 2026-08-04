from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    PaymentAllocation,
    TaxRate,
)
from app.models.catalog import BillingMode
from app.models.event_store import EventStore
from app.schemas.billing import InvoiceCreate, InvoiceLineCreate
from app.services import billing as billing_service
from app.services import customer_tax_policies, invoice_draft_authoring
from app.services.account_credit_deposits import (
    SETTLEMENT_SCOPE,
    AccountCreditDeposits,
    AccountCreditDepositSettlementSource,
    SettleAccountCreditDepositCommand,
)
from app.services.billing.account_credit import eligible_invoices
from app.services.db_session_adapter import db_session_adapter
from app.services.events.handlers.notification import NotificationHandler
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.topup_intents import TopupIntentChannel


def _line(
    description: str = "Monthly service",
    *,
    line_id=None,
    amount: str = "100.00",
) -> invoice_draft_authoring.DraftLineCommand:
    return invoice_draft_authoring.DraftLineCommand(
        line_id=line_id,
        description=description,
        quantity=Decimal("1"),
        unit_price=Decimal(amount),
    )


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="finance-test",
        scope="invoice_draft:test",
        reason="Invoice draft regression test",
        idempotency_key=key,
    )


def _create_command(subscriber) -> invoice_draft_authoring.CreateInvoiceDraftCommand:
    return invoice_draft_authoring.CreateInvoiceDraftCommand(
        account_id=subscriber.id,
        invoice_number=None,
        currency="NGN",
        issued_at=None,
        due_at=None,
        memo="Atomic draft",
        is_proforma=False,
        lines=(_line(),),
    )


def _settle_exact_account_credit(
    db_session,
    subscriber,
    *,
    reference: str,
) -> None:
    intent, _preview, _replayed = AccountCreditDeposits.stage_intent(
        db_session,
        account_id=subscriber.id,
        amount="100.00",
        currency="NGN",
        minimum="10.00",
        maximum="500000.00",
        reference=reference,
        provider_type="paystack",
        provider_id=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        idempotency_key=f"{reference}-intent",
        channel=TopupIntentChannel.customer_selfcare,
        created_by="pytest",
    )
    intent_id = intent.id
    db_session.commit()
    AccountCreditDeposits.settle_verified(
        db_session,
        SettleAccountCreditDepositCommand(
            intent_id=intent_id,
            provider_type="paystack",
            external_transaction_id=f"{reference}-payment",
            amount=Decimal("100.00"),
            currency="NGN",
            provider_intent_id=intent_id,
            source=AccountCreditDepositSettlementSource.customer_gateway_verify,
        ),
        context=CommandContext.system(
            actor="finance-test",
            scope=SETTLEMENT_SCOPE,
            reason="Create exact credit for proforma conversion regression",
        ),
    )


def test_create_draft_commits_complete_aggregate_and_replays(
    db_session, subscriber
) -> None:
    command = _create_command(subscriber)
    db_session_adapter.release_read_transaction(db_session)

    created = invoice_draft_authoring.create_invoice_draft(
        db_session,
        command,
        context=_context("invoice-draft-create-replay"),
    )
    replay = invoice_draft_authoring.create_invoice_draft(
        db_session,
        command,
        context=_context("invoice-draft-create-replay"),
    )

    invoice = db_session.get(Invoice, created.invoice_id)
    lines = db_session.scalars(
        select(InvoiceLine).where(InvoiceLine.invoice_id == created.invoice_id)
    ).all()
    event = db_session.scalar(
        select(EventStore).where(EventStore.invoice_id == created.invoice_id)
    )

    assert created.status is InvoiceStatus.draft
    assert created.total == Decimal("100.00")
    assert replay.invoice_id == created.invoice_id
    assert replay.replayed is True
    assert invoice is not None
    assert invoice.balance_due == Decimal("100.00")
    assert len(lines) == 1
    assert event is not None
    assert event.payload["amount"] == "100.00"
    assert event.payload["status"] == "draft"

    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(
        invoice_draft_authoring.InvoiceDraftAuthoringError
    ) as mismatched_replay:
        invoice_draft_authoring.create_invoice_draft(
            db_session,
            replace(command, memo="Changed retry payload"),
            context=_context("invoice-draft-create-replay"),
        )
    assert mismatched_replay.value.code.endswith(".idempotency_conflict")


def test_proforma_conversion_retry_preserves_credit_derived_paid_status(
    db_session,
    subscriber,
) -> None:
    subscriber.billing_mode = BillingMode.postpaid
    proforma_command = replace(
        _create_command(subscriber),
        invoice_number="PF-RETRY-PAID",
        memo="[PROFORMA] Retry regression",
        is_proforma=True,
    )
    db_session.commit()
    _settle_exact_account_credit(
        db_session,
        subscriber,
        reference="proforma-retry-credit",
    )
    created = invoice_draft_authoring.create_invoice_draft(
        db_session,
        proforma_command,
        context=_context("proforma-retry-create"),
    )
    conversion_context = CommandContext.system(
        actor="finance-test",
        scope="invoice_proforma:convert",
        reason="Convert proforma exactly once",
        idempotency_key="proforma-conversion-retry-paid",
    )

    converted = invoice_draft_authoring.convert_proforma_invoice(
        db_session,
        invoice_draft_authoring.ConvertProformaInvoiceCommand(
            invoice_id=created.invoice_id,
        ),
        context=conversion_context,
    )
    replayed = invoice_draft_authoring.convert_proforma_invoice(
        db_session,
        invoice_draft_authoring.ConvertProformaInvoiceCommand(
            invoice_id=created.invoice_id,
        ),
        context=conversion_context,
    )

    allocations = db_session.scalars(
        select(PaymentAllocation)
        .where(PaymentAllocation.invoice_id == created.invoice_id)
        .where(PaymentAllocation.is_active.is_(True))
    ).all()
    assert converted.status is InvoiceStatus.paid
    assert converted.balance_due == Decimal("0.00")
    assert replayed.status is InvoiceStatus.paid
    assert replayed.replayed is True
    assert len(allocations) == 1


def test_funded_prepaid_proforma_requires_reviewed_reconciliation(
    db_session,
    subscriber,
) -> None:
    subscriber.billing_mode = BillingMode.prepaid
    proforma_command = replace(
        _create_command(subscriber),
        invoice_number="PF-PREPAID-FUNDED",
        memo="[PROFORMA] Prepaid funded regression",
        is_proforma=True,
    )
    db_session.commit()
    _settle_exact_account_credit(
        db_session,
        subscriber,
        reference="prepaid-proforma-funded-credit",
    )
    created = invoice_draft_authoring.create_invoice_draft(
        db_session,
        proforma_command,
        context=_context("prepaid-proforma-funded-create"),
    )

    with pytest.raises(invoice_draft_authoring.InvoiceDraftAuthoringError) as rejected:
        invoice_draft_authoring.convert_proforma_invoice(
            db_session,
            invoice_draft_authoring.ConvertProformaInvoiceCommand(
                invoice_id=created.invoice_id,
            ),
            context=CommandContext.system(
                actor="finance-test",
                scope="invoice_proforma:convert",
                reason="Generic conversion must fail closed for prepaid",
                idempotency_key="prepaid-proforma-funded-convert",
            ),
        )

    assert rejected.value.code.endswith(".prepaid_reconciliation_required")
    invoice = db_session.get(Invoice, created.invoice_id)
    allocations = db_session.scalars(
        select(PaymentAllocation)
        .where(PaymentAllocation.invoice_id == created.invoice_id)
        .where(PaymentAllocation.is_active.is_(True))
    ).all()
    assert invoice is not None
    assert invoice.status is InvoiceStatus.draft
    assert invoice.is_proforma is True
    assert invoice.balance_due == Decimal("100.00")
    assert allocations == []


def test_create_draft_omits_vat_for_exempt_customer(
    db_session,
    subscriber,
) -> None:
    tax_rate = TaxRate(name="VAT", rate=Decimal("0.075"))
    db_session.add(tax_rate)
    db_session.commit()
    account_id = subscriber.id
    tax_rate_id = tax_rate.id
    command = replace(
        _create_command(subscriber),
        lines=(
            replace(
                _line(),
                tax_rate_id=tax_rate_id,
            ),
        ),
    )
    db_session_adapter.release_read_transaction(db_session)
    customer_tax_policies.set_customer_vat_exemption_policy(
        db_session,
        customer_tax_policies.SetCustomerVatExemptionPolicyCommand(
            account_id=account_id,
            vat_exempt=True,
            updated_by="finance-test",
        ),
        context=CommandContext.system(
            actor="finance-test",
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Exempt customer from VAT",
            idempotency_key=f"invoice-draft-vat-exemption:{account_id}",
        ),
    )
    db_session_adapter.release_read_transaction(db_session)

    created = invoice_draft_authoring.create_invoice_draft(
        db_session,
        command,
        context=_context("invoice-draft-vat-exempt"),
    )

    line = db_session.scalar(
        select(InvoiceLine).where(InvoiceLine.invoice_id == created.invoice_id)
    )
    invoice = db_session.get(Invoice, created.invoice_id)
    assert line is not None
    assert line.tax_rate_id is None
    assert invoice is not None
    assert invoice.tax_total == Decimal("0.00")


def test_create_draft_rolls_back_header_lines_and_evidence_on_failure(
    db_session, subscriber, monkeypatch
) -> None:
    command = _create_command(subscriber)
    db_session_adapter.release_read_transaction(db_session)

    def fail_after_lines(*_args, **_kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(invoice_draft_authoring, "_emit_created", fail_after_lines)

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        invoice_draft_authoring.create_invoice_draft(
            db_session,
            command,
            context=_context("invoice-draft-create-rollback"),
        )

    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 0
    assert db_session.scalar(select(func.count()).select_from(InvoiceLine)) == 0


def test_shared_invoice_constructor_rolls_back_header_when_lines_fail(
    db_session, subscriber, monkeypatch
) -> None:
    db_session.commit()

    def fail_line_replacement(*_args, **_kwargs):
        raise RuntimeError("line replacement failed")

    monkeypatch.setattr(
        billing_service.InvoiceLines,
        "replace_admin_draft_lines",
        fail_line_replacement,
    )

    with pytest.raises(RuntimeError, match="line replacement failed"):
        billing_service.invoices.create_with_lines(
            db_session,
            InvoiceCreate(
                account_id=subscriber.id,
                invoice_number="INV-ATOMIC-ROLLBACK",
                currency="NGN",
                status=InvoiceStatus.issued,
            ),
            (
                InvoiceLineCreate(
                    invoice_id=uuid4(),
                    description="Installation",
                    quantity=Decimal("1"),
                    unit_price=Decimal("100.00"),
                ),
            ),
        )

    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 0
    assert db_session.scalar(select(func.count()).select_from(InvoiceLine)) == 0


def test_update_replaces_lines_only_while_invoice_is_draft(
    db_session, subscriber
) -> None:
    command = _create_command(subscriber)
    subscriber_id = command.account_id
    db_session_adapter.release_read_transaction(db_session)
    created = invoice_draft_authoring.create_invoice_draft(
        db_session,
        command,
        context=_context("invoice-draft-update-create"),
    )
    existing_line_id = db_session.scalar(
        select(InvoiceLine.id).where(InvoiceLine.invoice_id == created.invoice_id)
    )
    db_session_adapter.release_read_transaction(db_session)

    updated = invoice_draft_authoring.update_invoice_draft(
        db_session,
        invoice_draft_authoring.UpdateInvoiceDraftCommand(
            invoice_id=created.invoice_id,
            account_id=subscriber_id,
            invoice_number=created.invoice_number,
            currency="NGN",
            issued_at=None,
            due_at=None,
            memo="Updated once",
            is_proforma=False,
            lines=(
                _line("Updated service", line_id=existing_line_id, amount="125.00"),
                _line("Router rental", amount="25.00"),
            ),
        ),
        context=_context("invoice-draft-update"),
    )

    assert updated.total == Decimal("150.00")

    invoice = db_session.get(Invoice, created.invoice_id)
    assert invoice is not None
    invoice.status = InvoiceStatus.issued
    db_session.commit()
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(invoice_draft_authoring.InvoiceDraftAuthoringError) as rejected:
        invoice_draft_authoring.update_invoice_draft(
            db_session,
            invoice_draft_authoring.UpdateInvoiceDraftCommand(
                invoice_id=created.invoice_id,
                account_id=subscriber_id,
                invoice_number=created.invoice_number,
                currency="NGN",
                issued_at=None,
                due_at=None,
                memo="Illegal edit",
                is_proforma=False,
                lines=(_line(line_id=uuid4()),),
            ),
            context=_context("invoice-draft-update-issued"),
        )

    assert rejected.value.code.endswith(".invoice_not_editable")


def test_issued_lines_are_immutable_and_proformas_are_not_collectible(
    db_session, subscriber
) -> None:
    issued = Invoice(
        account_id=subscriber.id,
        invoice_number="INV-ISSUED-GUARD",
        status=InvoiceStatus.issued,
        currency="NGN",
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        is_proforma=False,
    )
    proforma = Invoice(
        account_id=subscriber.id,
        invoice_number="PF-CREDIT-GUARD",
        status=InvoiceStatus.draft,
        currency="NGN",
        total=Decimal("200.00"),
        balance_due=Decimal("200.00"),
        is_proforma=True,
    )
    db_session.add_all([issued, proforma])
    db_session.commit()

    with pytest.raises(HTTPException) as line_rejected:
        billing_service.invoice_lines.create(
            db_session,
            InvoiceLineCreate(
                invoice_id=issued.id,
                description="Illegal issued edit",
                quantity=Decimal("1"),
                unit_price=Decimal("10.00"),
            ),
        )

    assert line_rejected.value.status_code == 409
    assert eligible_invoices(db_session, str(subscriber.id)) == [issued]

    with pytest.raises(HTTPException) as issue_rejected:
        billing_service.invoices.issue_draft_system(
            db_session,
            str(proforma.id),
            issued_at=proforma.created_at,
            due_at=None,
            reason="proforma-guard-test",
        )
    assert issue_rejected.value.status_code == 409


def test_draft_created_event_never_queues_customer_notification(monkeypatch) -> None:
    handler = NotificationHandler()
    monkeypatch.setattr(
        handler,
        "_load_templates",
        lambda *_args, **_kwargs: pytest.fail(
            "draft notification policy should return before template loading"
        ),
    )

    handler.handle(
        object(),
        Event(
            event_type=EventType.invoice_created,
            payload={"status": "draft", "invoice_number": "INV-DRAFT"},
        ),
    )
