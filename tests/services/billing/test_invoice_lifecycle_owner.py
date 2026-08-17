from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceStatus,
    LedgerEntry,
)
from app.models.catalog import BillingMode
from app.services.billing.invoices import (
    InvoiceIssuanceInput,
    InvoiceOwnerError,
    Invoices,
)


def _issuance(now: datetime, *, reason: str) -> InvoiceIssuanceInput:
    return InvoiceIssuanceInput(
        issued_at=now,
        due_at=now + timedelta(days=7),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref="test:invoice-lifecycle",
        due_date_policy_version="test-v1",
        reason=reason,
    )


def _invoice(db_session, subscriber, *, status: InvoiceStatus, due_at=None):
    issued_at = (
        (due_at - timedelta(days=1))
        if due_at is not None and status != InvoiceStatus.draft
        else None
    )
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"LIFECYCLE-{uuid.uuid4().hex[:8]}",
        status=status,
        currency="NGN",
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        issued_at=issued_at,
        due_at=due_at,
        due_date_basis=(
            InvoiceDueDateBasis.contract_terms
            if status != InvoiceStatus.draft
            else None
        ),
        due_date_basis_ref=(
            "test:legacy-row" if status != InvoiceStatus.draft else None
        ),
        due_date_policy_version=("test-v1" if status != InvoiceStatus.draft else None),
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_invoice_owner_issues_draft_with_audited_no_money_result(
    db_session, subscriber
):
    invoice = _invoice(db_session, subscriber, status=InvoiceStatus.draft)
    now = datetime.now(UTC)

    result = Invoices.issue_draft_system(
        db_session,
        str(invoice.id),
        issuance=_issuance(now, reason="test_system_issue"),
        commit=True,
    )

    assert result.changed is True
    assert result.invoice.status == InvoiceStatus.issued
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "issue_invoice_system")
        .filter(AuditEvent.entity_id == str(invoice.id))
        .one()
    )
    assert audit.metadata_["from_status"] == "draft"
    assert audit.metadata_["to_status"] == "issued"
    assert audit.metadata_["ledger_transaction_id"] is None
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.invoice_id == invoice.id)
        .count()
        == 0
    )


def test_invoice_owner_skips_underfunded_credit_when_full_credit_required(
    db_session, subscriber, monkeypatch
):
    invoice = _invoice(db_session, subscriber, status=InvoiceStatus.draft)
    applied: list[str] = []

    monkeypatch.setattr(
        "app.services.billing.account_credit.AccountCreditApplications.preview_invoice_funding",
        lambda db, invoice: type(
            "Preview",
            (),
            {"fully_funded": False, "fingerprint": "underfunded-preview"},
        )(),
    )
    monkeypatch.setattr(
        "app.services.billing.account_credit.AccountCreditApplications.apply_invoice_fully",
        lambda db, invoice, *, preview_fingerprint: applied.append(preview_fingerprint),
    )

    result = Invoices.issue_draft_system(
        db_session,
        str(invoice.id),
        issuance=_issuance(datetime.now(UTC), reason="test_full_credit_required"),
        require_full_available_credit=True,
        commit=True,
    )

    assert result.invoice.status == InvoiceStatus.issued
    assert result.invoice.balance_due == Decimal("100.00")
    assert applied == []


def test_invoice_owner_applies_credit_when_full_credit_required_and_funded(
    db_session, subscriber, monkeypatch
):
    invoice = _invoice(db_session, subscriber, status=InvoiceStatus.draft)
    applied: list[str] = []

    monkeypatch.setattr(
        "app.services.billing.account_credit.AccountCreditApplications.preview_invoice_funding",
        lambda db, invoice: type(
            "Preview",
            (),
            {"fully_funded": True, "fingerprint": "fully-funded-preview"},
        )(),
    )

    def _fake_apply(db, invoice, *, preview_fingerprint):
        applied.append(preview_fingerprint)
        invoice.balance_due = Decimal("0.00")
        invoice.status = InvoiceStatus.paid

    monkeypatch.setattr(
        "app.services.billing.account_credit.AccountCreditApplications.apply_invoice_fully",
        _fake_apply,
    )

    result = Invoices.issue_draft_system(
        db_session,
        str(invoice.id),
        issuance=_issuance(datetime.now(UTC), reason="test_full_credit_required"),
        require_full_available_credit=True,
        commit=True,
    )

    assert result.invoice.status == InvoiceStatus.paid
    assert result.invoice.balance_due == Decimal("0.00")
    assert applied == ["fully-funded-preview"]


def test_invoice_owner_marks_overdue_once_and_keeps_access_as_observation(
    db_session, subscriber
):
    now = datetime.now(UTC)
    invoice = _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.issued,
        due_at=now - timedelta(days=2),
    )

    first = Invoices.mark_overdue_system(
        db_session,
        str(invoice.id),
        as_of=now,
        reason="test_overdue",
        commit=True,
    )
    replay = Invoices.mark_overdue_system(
        db_session,
        str(invoice.id),
        as_of=now,
        reason="test_overdue",
        commit=True,
    )

    assert first.changed is True
    assert first.event_emitted is True
    assert replay.changed is False
    assert replay.event_emitted is False
    audits = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "mark_invoice_overdue_system")
        .filter(AuditEvent.entity_id == str(invoice.id))
        .all()
    )
    assert len(audits) == 1
    assert audits[0].metadata_["service_access_consequence"] == "observation_only"


def test_unknown_due_date_provenance_cannot_drive_overdue(db_session, subscriber):
    now = datetime.now(UTC)
    invoice = _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.issued,
        due_at=now - timedelta(days=2),
    )
    invoice.due_date_basis = InvoiceDueDateBasis.unknown_unverified
    invoice.due_date_basis_ref = None
    invoice.due_date_policy_version = None
    db_session.commit()

    with pytest.raises(InvoiceOwnerError) as rejected:
        Invoices.mark_overdue_system(
            db_session,
            str(invoice.id),
            as_of=now,
            reason="test_unverified_due_date",
        )

    assert rejected.value.code == "financial.invoice.due_date_unverified"


def test_invoice_owner_returns_only_unfunded_prepaid_receivable_to_draft(
    db_session, subscriber
):
    subscriber.billing_mode = BillingMode.prepaid
    db_session.commit()
    invoice = _invoice(
        db_session,
        subscriber,
        status=InvoiceStatus.overdue,
        due_at=datetime.now(UTC) - timedelta(days=2),
    )

    result = Invoices.return_unfunded_prepaid_to_draft_system(
        db_session,
        str(invoice.id),
        reason="test_prepaid_reclassification",
        commit=True,
    )

    assert result.changed is True
    assert result.invoice.status == InvoiceStatus.draft
    assert result.invoice.issued_at is None
    assert result.invoice.due_at is None
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "return_unfunded_prepaid_invoice_to_draft")
        .filter(AuditEvent.entity_id == str(invoice.id))
        .one()
    )
    assert audit.metadata_["payments_applied"] == "0.00"
    assert audit.metadata_["credits_applied"] == "0.00"
    assert audit.metadata_["ledger_transaction_id"] is None
