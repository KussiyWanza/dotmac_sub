from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import (
    CreditNote,
    CreditNoteApplication,
    CreditNoteStatus,
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    TaxApplication,
)
from app.models.idempotency import IdempotencyKey
from app.schemas.billing import (
    CreditNoteApplicationPreviewRequest,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteIssueConfirmation,
    CreditNoteIssuePreviewRequest,
    CreditNoteIssueRequest,
    CreditNoteLineCreate,
    CreditNoteVoidRequest,
    InvoiceCreate,
    LedgerEntryCreate,
    TaxRateCreate,
)
from app.services import billing as billing_service
from app.services import web_billing_invoices
from app.services.billing._common import get_account_credit_balance
from app.services.customer_financial_ledger import calculate_customer_balance
from app.services.locking import lock_for_update as _real_lock_for_update
from app.services.web_billing_credits import preview_credit_from_form


def _confirmed_apply_request(
    db,
    credit_note,
    invoice,
    *,
    amount: Decimal | None = None,
    idempotency_key: str | None = None,
):
    preview = billing_service.credit_notes.preview_application(
        db,
        str(credit_note.id),
        CreditNoteApplicationPreviewRequest(
            invoice_id=invoice.id,
            amount=amount,
        ),
    )
    return CreditNoteApplyRequest(
        invoice_id=invoice.id,
        amount=preview.apply_amount,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=idempotency_key or uuid4().hex,
    )


def _issued_credit_note_and_invoice(db_session, account_id, *, amount):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=account_id,
            currency="USD",
            subtotal=amount,
            tax_total=Decimal("0.00"),
            total=amount,
            balance_due=amount,
        ),
    )
    credit_note = billing_service.credit_notes.issue_system(
        db_session,
        CreditNoteIssuePreviewRequest(
            account_id=account_id,
            currency="USD",
            subtotal=amount,
            tax_total=Decimal("0.00"),
            total=amount,
        ),
        idempotency_key=uuid4().hex,
        commit=True,
    ).credit_note
    return credit_note, invoice


def test_credit_note_owner_persists_first_issuance_timestamp(
    db_session, subscriber_account
):
    draft = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            status=CreditNoteStatus.draft,
            currency="NGN",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
        ),
    )
    assert draft.issued_at is None

    preview = billing_service.credit_notes.preview_draft_issue(
        db_session,
        str(draft.id),
    )
    issued = billing_service.credit_notes.issue_draft_with_evidence(
        db_session,
        str(draft.id),
        CreditNoteIssueConfirmation(
            preview_fingerprint=preview.fingerprint,
            idempotency_key=f"issue-{draft.id}",
        ),
    ).credit_note

    assert issued.issued_at is not None


def test_credit_form_rejects_non_positive_amounts(db_session, subscriber_account):
    with pytest.raises(ValueError, match="Amount must be greater than 0"):
        preview_credit_from_form(
            db_session,
            account_id=str(subscriber_account.id),
            amount="0",
            currency="USD",
            memo=None,
        )

    with pytest.raises(ValueError, match="Amount must be greater than 0"):
        preview_credit_from_form(
            db_session,
            account_id=str(subscriber_account.id),
            amount="-1",
            currency="USD",
            memo=None,
        )


def test_proforma_cannot_consume_credit_note(db_session, subscriber_account) -> None:
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session,
        subscriber_account.id,
        amount=Decimal("100.00"),
    )
    invoice.is_proforma = True
    db_session.commit()

    assert (
        billing_service.credit_notes.list_application_options(
            db_session,
            str(invoice.id),
        )
        == []
    )
    with pytest.raises(HTTPException) as rejected:
        billing_service.credit_notes.preview_application(
            db_session,
            str(credit_note.id),
            CreditNoteApplicationPreviewRequest(invoice_id=invoice.id),
        )
    assert rejected.value.status_code == 400


def test_credit_note_apply_reduces_invoice_balance(db_session, subscriber_account):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        ),
    )
    credit_note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            currency="USD",
        ),
    )
    billing_service.credit_note_lines.create(
        db_session,
        CreditNoteLineCreate(
            credit_note_id=credit_note.id,
            description="Service credit",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
        ),
    )
    issue_preview = billing_service.credit_notes.preview_draft_issue(
        db_session, str(credit_note.id)
    )
    credit_note = billing_service.credit_notes.issue_draft_with_evidence(
        db_session,
        str(credit_note.id),
        CreditNoteIssueConfirmation(
            preview_fingerprint=issue_preview.fingerprint,
            idempotency_key=uuid4().hex,
        ),
    ).credit_note
    application = billing_service.credit_notes.apply(
        db_session,
        str(credit_note.id),
        _confirmed_apply_request(
            db_session,
            credit_note,
            invoice,
            amount=Decimal("30.00"),
        ),
    )
    assert application.amount == Decimal("30.00")
    refreshed_invoice = billing_service.invoices.get(db_session, str(invoice.id))
    assert refreshed_invoice.balance_due == Decimal("70.00")
    refreshed_credit_note = billing_service.credit_notes.get(
        db_session, str(credit_note.id)
    )
    assert refreshed_credit_note.applied_total == Decimal("30.00")
    assert refreshed_credit_note.status == CreditNoteStatus.partially_applied


def test_credit_note_apply_without_lines_uses_total(db_session, subscriber_account):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("120.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("120.00"),
            balance_due=Decimal("120.00"),
        ),
    )
    credit_note = billing_service.credit_notes.issue_system(
        db_session,
        CreditNoteIssuePreviewRequest(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("50.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("50.00"),
        ),
        idempotency_key=uuid4().hex,
        commit=True,
    ).credit_note
    application = billing_service.credit_notes.apply(
        db_session,
        str(credit_note.id),
        _confirmed_apply_request(db_session, credit_note, invoice),
    )
    assert application.amount == Decimal("50.00")
    refreshed_invoice = billing_service.invoices.get(db_session, str(invoice.id))
    assert refreshed_invoice.balance_due == Decimal("70.00")
    refreshed_credit_note = billing_service.credit_notes.get(
        db_session, str(credit_note.id)
    )
    assert refreshed_credit_note.total == Decimal("50.00")
    assert refreshed_credit_note.applied_total == Decimal("50.00")
    assert refreshed_credit_note.status == CreditNoteStatus.applied


def test_apply_locks_credit_note_and_invoice_rows(db_session, subscriber_account):
    """Apply must SELECT ... FOR UPDATE both rows before the balance check, so
    concurrent applies can't each read a stale applied_total and over-spend the
    note. SQLite can't exercise the race (FOR UPDATE is a no-op), so we pin that
    the locks are acquired for the right rows."""
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    with patch(
        "app.services.billing.credit_notes.lock_for_update",
        wraps=_real_lock_for_update,
    ) as spy:
        billing_service.credit_notes.apply(
            db_session,
            str(credit_note.id),
            _confirmed_apply_request(
                db_session,
                credit_note,
                invoice,
                amount=Decimal("40.00"),
            ),
        )
    locked_models = {call.args[1] for call in spy.call_args_list}
    assert CreditNote in locked_models
    assert Invoice in locked_models


def test_apply_cannot_exceed_credit_note_balance(db_session, subscriber_account):
    """Sequential over-application is rejected (the lock makes the same guard
    hold under concurrency): a fully-applied note has no remaining balance."""
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    first_request = _confirmed_apply_request(
        db_session,
        credit_note,
        invoice,
        amount=Decimal("100.00"),
    )
    billing_service.credit_notes.apply(
        db_session,
        str(credit_note.id),
        first_request,
    )
    with pytest.raises(HTTPException) as exc:
        billing_service.credit_notes.apply(
            db_session,
            str(credit_note.id),
            CreditNoteApplyRequest(
                invoice_id=invoice.id,
                amount=Decimal("1.00"),
                preview_fingerprint=first_request.preview_fingerprint,
                idempotency_key=uuid4().hex,
            ),
        )
    assert exc.value.status_code == 400


def test_confirmed_apply_links_exact_ledger_and_replays_idempotently(
    db_session, subscriber_account
):
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    request = _confirmed_apply_request(
        db_session,
        credit_note,
        invoice,
        amount=Decimal("35.00"),
        idempotency_key=uuid4().hex,
    )

    first = billing_service.credit_notes.apply_with_evidence(
        db_session, str(credit_note.id), request
    )
    replay = billing_service.credit_notes.apply_with_evidence(
        db_session, str(credit_note.id), request
    )

    assert replay.idempotent_replay is True
    assert replay.application.id == first.application.id
    assert first.application.ledger_entry_id == first.ledger_entry.id
    assert first.application.preview_fingerprint == request.preview_fingerprint
    assert first.ledger_entry.source == LedgerSource.credit_note
    assert first.ledger_entry.amount == Decimal("35.00")
    assert (
        db_session.query(CreditNoteApplication)
        .filter(CreditNoteApplication.credit_note_id == credit_note.id)
        .count()
        == 1
    )
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.id == first.ledger_entry.id)
        .count()
        == 1
    )
    key = (
        db_session.query(IdempotencyKey)
        .filter_by(
            scope="credit_note_application",
            key=request.idempotency_key,
        )
        .one()
    )
    assert key.ref_id == str(first.application.id)

    second_preview = billing_service.credit_notes.preview_application(
        db_session,
        str(credit_note.id),
        CreditNoteApplicationPreviewRequest(
            invoice_id=invoice.id,
            amount=Decimal("20.00"),
        ),
    )
    with pytest.raises(HTTPException, match="different confirmation") as exc:
        billing_service.credit_notes.apply_with_evidence(
            db_session,
            str(credit_note.id),
            CreditNoteApplyRequest(
                invoice_id=invoice.id,
                amount=second_preview.apply_amount,
                preview_fingerprint=second_preview.fingerprint,
                idempotency_key=request.idempotency_key,
            ),
        )
    assert exc.value.status_code == 409


def test_partial_credit_does_not_infer_an_access_transition(
    db_session, subscriber_account
):
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    request = _confirmed_apply_request(
        db_session,
        credit_note,
        invoice,
        amount=Decimal("25.00"),
    )

    with patch(
        "app.services.billing.invoices.reconcile_service_after_invoice_settlement"
    ) as reconcile_access:
        result = billing_service.credit_notes.apply_with_evidence(
            db_session, str(credit_note.id), request
        )

    assert result.preview is not None
    assert result.preview.access_consequence == "none"
    reconcile_access.assert_not_called()


def test_apply_rejects_a_stale_financial_preview(db_session, subscriber_account):
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    request = _confirmed_apply_request(
        db_session,
        credit_note,
        invoice,
        amount=Decimal("40.00"),
    )
    credit_note.total = Decimal("90.00")
    credit_note.subtotal = Decimal("90.00")
    db_session.commit()

    with pytest.raises(HTTPException, match="Financial state changed") as exc:
        billing_service.credit_notes.apply(db_session, str(credit_note.id), request)

    assert exc.value.status_code == 409
    assert db_session.query(CreditNoteApplication).count() == 0


def test_web_credit_application_audit_names_exact_result_and_ledger(
    db_session, subscriber_account
):
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("80.00")
    )
    preview = billing_service.credit_notes.preview_application(
        db_session,
        str(credit_note.id),
        CreditNoteApplicationPreviewRequest(
            invoice_id=invoice.id,
            amount=Decimal("30.00"),
        ),
    )

    metadata = web_billing_invoices.apply_credit_note_to_invoice_web(
        db_session,
        request=None,
        actor_id=None,
        invoice_id=str(invoice.id),
        credit_note_id=str(credit_note.id),
        amount=str(preview.apply_amount),
        memo="Service adjustment",
        preview_fingerprint=preview.fingerprint,
        idempotency_key=uuid4().hex,
    )

    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "credit_note")
        .filter(AuditEvent.entity_id == str(credit_note.id))
        .filter(AuditEvent.action == "apply")
        .one()
    )
    assert event.metadata_["application_id"] == metadata["application_id"]
    assert event.metadata_["ledger_entry_id"] == metadata["ledger_entry_id"]
    assert event.metadata_["invoice_receivable_before"] == "80.00"
    assert event.metadata_["invoice_receivable_after"] == "50.00"


def test_credit_note_line_inclusive_tax_extracts_tax(
    db_session,
    subscriber_account,
):
    tax = billing_service.tax_rates.create(
        db_session,
        TaxRateCreate(name="Credit GST", rate=Decimal("10.0000")),
    )
    credit_note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            currency="USD",
        ),
    )
    billing_service.credit_note_lines.create(
        db_session,
        CreditNoteLineCreate(
            credit_note_id=credit_note.id,
            description="Inclusive service credit",
            quantity=Decimal("1"),
            unit_price=Decimal("110.00"),
            tax_rate_id=tax.id,
            tax_application=TaxApplication.inclusive,
        ),
    )

    refreshed_credit_note = billing_service.credit_notes.get(
        db_session, str(credit_note.id)
    )
    assert refreshed_credit_note.subtotal == Decimal("100.00")
    assert refreshed_credit_note.tax_total == Decimal("10.00")
    assert refreshed_credit_note.total == Decimal("110.00")


def test_issue_preview_confirmation_links_exact_funding_without_double_counting(
    db_session, subscriber_account
):
    before = calculate_customer_balance(
        db_session, subscriber_account.id, currency="USD"
    )
    request = CreditNoteIssuePreviewRequest(
        account_id=subscriber_account.id,
        currency="USD",
        subtotal=Decimal("75.00"),
        total=Decimal("75.00"),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    preview = billing_service.credit_notes.preview_issue(db_session, request)
    confirmation = CreditNoteIssueRequest(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=uuid4().hex,
    )

    result = billing_service.credit_notes.issue_with_evidence(db_session, confirmation)
    replay = billing_service.credit_notes.issue_with_evidence(db_session, confirmation)

    assert replay.idempotent_replay is True
    assert replay.credit_note.id == result.credit_note.id
    assert result.credit_note.funding_ledger_entry_id == result.funding_ledger_entry.id
    assert result.credit_note.issue_preview_fingerprint == preview.fingerprint
    assert result.funding_ledger_entry.invoice_id is None
    assert result.funding_ledger_entry.entry_type == LedgerEntryType.credit
    assert result.funding_ledger_entry.source == LedgerSource.credit_note
    assert result.funding_ledger_entry.amount == Decimal("75.00")
    assert calculate_customer_balance(
        db_session, subscriber_account.id, currency="USD"
    ) == before + Decimal("75.00")
    audit = (
        db_session.query(AuditEvent)
        .filter_by(
            entity_type="credit_note",
            entity_id=str(result.credit_note.id),
            action="issue",
        )
        .one()
    )
    assert audit.metadata_["funding_ledger_entry_id"] == str(
        result.funding_ledger_entry.id
    )


def test_funded_application_consumes_operational_credit_once(
    db_session, subscriber_account
):
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    funding_before = get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    )
    result = billing_service.credit_notes.apply_with_evidence(
        db_session,
        str(credit_note.id),
        _confirmed_apply_request(
            db_session,
            credit_note,
            invoice,
            amount=Decimal("30.00"),
        ),
    )

    assert result.consumption_ledger_entry is not None
    assert (
        result.application.consumption_ledger_entry_id
        == result.consumption_ledger_entry.id
    )
    assert result.consumption_ledger_entry.invoice_id is None
    assert result.consumption_ledger_entry.entry_type == LedgerEntryType.debit
    assert result.consumption_ledger_entry.amount == Decimal("30.00")
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == funding_before - Decimal("30.00")


def test_funded_application_uses_exact_note_funding_when_legacy_pool_is_negative(
    db_session, subscriber_account
):
    billing_service.ledger_entries.create(
        db_session,
        LedgerEntryCreate(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.other,
            amount=Decimal("250.00"),
            currency="USD",
            memo="Legacy account-credit drift evidence",
        ),
    )
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) < Decimal("0.00")

    result = billing_service.credit_notes.apply_with_evidence(
        db_session,
        str(credit_note.id),
        _confirmed_apply_request(
            db_session,
            credit_note,
            invoice,
            amount=Decimal("100.00"),
        ),
    )

    assert result.application.amount == Decimal("100.00")
    assert result.consumption_ledger_entry is not None
    assert invoice.balance_due == Decimal("0.00")


def test_void_requires_confirmation_and_posts_exact_reversal(
    db_session, subscriber_account
):
    note, _ = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("40.00")
    )
    customer_before = calculate_customer_balance(
        db_session, subscriber_account.id, currency="USD"
    )
    preview = billing_service.credit_notes.preview_void(db_session, str(note.id))
    confirmation = CreditNoteVoidRequest(
        preview_fingerprint=preview.fingerprint,
        idempotency_key=uuid4().hex,
    )

    result = billing_service.credit_notes.void_with_evidence(
        db_session, str(note.id), confirmation
    )
    replay = billing_service.credit_notes.void_with_evidence(
        db_session, str(note.id), confirmation
    )

    assert replay.idempotent_replay is True
    assert result.credit_note.status == CreditNoteStatus.void
    assert result.credit_note.void_ledger_entry_id == result.void_ledger_entry.id
    assert result.void_ledger_entry.entry_type == LedgerEntryType.debit
    assert result.void_ledger_entry.reversal_of_entry_id == note.funding_ledger_entry_id
    assert result.void_ledger_entry.amount == Decimal("40.00")
    assert calculate_customer_balance(
        db_session, subscriber_account.id, currency="USD"
    ) == customer_before - Decimal("40.00")


def test_void_uses_exact_note_funding_when_legacy_pool_is_negative(
    db_session, subscriber_account
):
    billing_service.ledger_entries.create(
        db_session,
        LedgerEntryCreate(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.other,
            amount=Decimal("250.00"),
            currency="USD",
            memo="Legacy account-credit drift evidence",
        ),
    )
    note, _invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) < Decimal("0.00")

    preview = billing_service.credit_notes.preview_void(db_session, str(note.id))
    result = billing_service.credit_notes.void_with_evidence(
        db_session,
        str(note.id),
        CreditNoteVoidRequest(
            preview_fingerprint=preview.fingerprint,
            idempotency_key=uuid4().hex,
        ),
    )

    assert result.credit_note.status == CreditNoteStatus.void
    assert result.void_ledger_entry.reversal_of_entry_id == note.funding_ledger_entry_id


def test_historical_funding_reconciliation_never_guesses(
    db_session, subscriber_account
):
    note = CreditNote(
        account_id=subscriber_account.id,
        status=CreditNoteStatus.issued,
        currency="USD",
        subtotal=Decimal("25.00"),
        total=Decimal("25.00"),
    )
    db_session.add(note)
    db_session.commit()

    report = billing_service.credit_notes.reconcile_funding_evidence(
        db_session, str(note.id)
    )
    assert report.status == "missing_review_required"
    assert report.funding_ledger_entry_id is None
    with pytest.raises(HTTPException, match="reconciled before voiding"):
        billing_service.credit_notes.preview_void(db_session, str(note.id))

    repaired = billing_service.credit_notes.reconcile_funding_evidence(
        db_session,
        str(note.id),
        apply=True,
        create_missing=True,
    )
    assert repaired.applied is True
    assert repaired.funding_ledger_entry_id is not None


def test_create_cannot_bypass_issue_confirmation(db_session, subscriber_account):
    with pytest.raises(HTTPException, match="issue workflow") as exc:
        billing_service.credit_notes.create(
            db_session,
            CreditNoteCreate(
                account_id=subscriber_account.id,
                status=CreditNoteStatus.issued,
                currency="USD",
                subtotal=Decimal("10.00"),
                total=Decimal("10.00"),
            ),
        )
    assert exc.value.status_code == 409
