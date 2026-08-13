"""An issued credit note reduces the receivable it names, at issue.

A credit note is a reduction of the receivable, not a customer deposit. Leaving
an issued note unapplied while the same account carries the invoice it credits
overstates AR: the obligation is already reduced, but the invoice keeps ageing
and keeps being dunned. Application used to be a separate manual step, so the
overstatement lasted until somebody remembered.

Issue and application are now one transaction — the note, its funding, the
application, the consumption evidence, the invoice recalculation, the audit
trail and the settlement consequence land together or not at all.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import (
    CreditNoteApplication,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.idempotency import IdempotencyKey
from app.schemas.billing import (
    CreditNoteApplicationPreviewRequest,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteIssueApplicationDisposition,
    CreditNoteIssueApplicationReason,
    CreditNoteIssueConfirmation,
    CreditNoteIssuePreviewRequest,
    CreditNoteIssueRequest,
)
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance


def _invoice(db_session, account_id, total: str, **kwargs) -> Invoice:
    invoice = Invoice(
        account_id=account_id,
        status=kwargs.pop("status", InvoiceStatus.issued),
        currency=kwargs.pop("currency", "USD"),
        total=Decimal(total),
        balance_due=Decimal(kwargs.pop("balance_due", total)),
        **kwargs,
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def _issue(db_session, account_id, total: str, *, invoice_id=None, key=None):
    request = CreditNoteIssuePreviewRequest(
        account_id=account_id,
        invoice_id=invoice_id,
        currency="USD",
        subtotal=Decimal(total),
        total=Decimal(total),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    preview = billing_service.credit_notes.preview_issue(db_session, request)
    confirmation = CreditNoteIssueRequest(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key or uuid4().hex,
    )
    return preview, billing_service.credit_notes.issue_with_evidence(
        db_session, confirmation
    )


def _applications(db_session, credit_note_id) -> list[CreditNoteApplication]:
    return (
        db_session.query(CreditNoteApplication)
        .filter(CreditNoteApplication.credit_note_id == credit_note_id)
        .all()
    )


def test_exact_credit_settles_the_named_invoice_at_issue(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "75.00")

    preview, result = _issue(
        db_session, subscriber_account.id, "75.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("75.00")
    assert preview.invoice_receivable_after == Decimal("0.00")
    assert preview.residual_credit == Decimal("0.00")
    assert preview.settles_invoice is True
    assert preview.application_disposition == (
        CreditNoteIssueApplicationDisposition.apply_to_invoice
    )
    assert preview.application_reason == (
        CreditNoteIssueApplicationReason.invoice_receivable_open
    )
    assert preview.access_consequence == "recheck_after_receivable_settlement"

    assert result.application is not None
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")
    assert invoice.status == InvoiceStatus.paid
    assert len(_applications(db_session, result.credit_note.id)) == 1


def test_partial_credit_reduces_the_receivable_and_leaves_it_open(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "100.00")

    preview, result = _issue(
        db_session, subscriber_account.id, "40.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("40.00")
    assert preview.invoice_receivable_after == Decimal("60.00")
    assert preview.settles_invoice is False
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("60.00")
    assert invoice.status != InvoiceStatus.paid
    assert result.application.application.amount == Decimal("40.00")


def test_excess_credit_settles_the_invoice_and_keeps_the_residual_as_credit(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "30.00")
    before = get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    )

    preview, result = _issue(
        db_session, subscriber_account.id, "50.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("30.00")
    assert preview.residual_credit == Decimal("20.00")
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")
    assert result.credit_note.applied_total == Decimal("30.00")
    # Only the unapplied part survives on the account: the funding entry adds
    # the whole credit and the consumption debit removes what was applied.
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == before + Decimal("20.00")


def test_an_unlinked_note_names_no_invoice_and_applies_nothing(
    db_session, subscriber_account
):
    """Credit with no named receivable is held, and says so."""
    _invoice(db_session, subscriber_account.id, "90.00")
    before = get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    )

    preview, result = _issue(db_session, subscriber_account.id, "50.00")

    assert preview.application_amount == Decimal("0.00")
    assert preview.residual_credit == Decimal("50.00")
    assert preview.application_disposition == (
        CreditNoteIssueApplicationDisposition.retain_account_credit
    )
    assert preview.application_reason == (
        CreditNoteIssueApplicationReason.no_invoice_named
    )
    assert preview.settles_invoice is False
    assert result.application is None
    assert _applications(db_session, result.credit_note.id) == []
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == before + Decimal("50.00")


def test_a_paid_invoice_retains_the_credit_without_claiming_settlement(
    db_session, subscriber_account
):
    invoice = _invoice(
        db_session,
        subscriber_account.id,
        "40.00",
        status=InvoiceStatus.paid,
        balance_due="0.00",
    )

    preview, result = _issue(
        db_session, subscriber_account.id, "40.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("0.00")
    assert preview.invoice_receivable_after == Decimal("0.00")
    assert preview.settles_invoice is False
    assert preview.application_disposition == (
        CreditNoteIssueApplicationDisposition.retain_account_credit
    )
    assert preview.application_reason == (
        CreditNoteIssueApplicationReason.invoice_already_paid
    )
    assert result.application is None
    assert result.credit_note.status == CreditNoteStatus.issued


def test_an_open_invoice_with_nothing_left_to_reduce_retains_the_credit(
    db_session, subscriber_account
):
    """An open invoice may legitimately owe nothing — settled by earlier credit
    or payment without reaching paid, or issued at zero. Refusing would deny a
    cancellation its credit for a state the customer did not cause."""
    invoice = _invoice(
        db_session,
        subscriber_account.id,
        "40.00",
        balance_due="0.00",
    )

    preview, result = _issue(
        db_session, subscriber_account.id, "40.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("0.00")
    assert preview.invoice_receivable_after == Decimal("0.00")
    assert preview.settles_invoice is False
    assert preview.application_disposition == (
        CreditNoteIssueApplicationDisposition.retain_account_credit
    )
    assert preview.application_reason == (
        CreditNoteIssueApplicationReason.invoice_receivable_exhausted
    )
    assert result.application is None
    assert result.credit_note.status == CreditNoteStatus.issued


@pytest.mark.parametrize(
    "invoice_kwargs",
    [
        {"status": InvoiceStatus.void},
        {"status": InvoiceStatus.written_off},
        {"is_proforma": True},
        {"is_active": False},
    ],
)
def test_incoherent_named_invoice_states_fail_issue_preview(
    db_session, subscriber_account, invoice_kwargs
):
    invoice = _invoice(
        db_session,
        subscriber_account.id,
        "40.00",
        **invoice_kwargs,
    )

    with pytest.raises(HTTPException) as rejected:
        _issue(db_session, subscriber_account.id, "40.00", invoice_id=invoice.id)

    assert rejected.value.status_code in {400, 409}


def test_replaying_the_issue_does_not_apply_twice(db_session, subscriber_account):
    """The application key is derived from the issue, so a replay is idempotent."""
    invoice = _invoice(db_session, subscriber_account.id, "60.00")
    key = uuid4().hex

    _, first = _issue(
        db_session, subscriber_account.id, "60.00", invoice_id=invoice.id, key=key
    )
    request = CreditNoteIssuePreviewRequest(
        account_id=subscriber_account.id,
        invoice_id=invoice.id,
        currency="USD",
        subtotal=Decimal("60.00"),
        total=Decimal("60.00"),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    replay = billing_service.credit_notes.issue_with_evidence(
        db_session,
        CreditNoteIssueRequest(
            **request.model_dump(),
            preview_fingerprint=first.credit_note.issue_preview_fingerprint,
            idempotency_key=key,
        ),
    )

    assert replay.idempotent_replay is True
    assert replay.credit_note.id == first.credit_note.id
    assert replay.application is not None
    assert first.application is not None
    assert replay.application.idempotent_replay is True
    assert replay.application.application.id == first.application.application.id
    assert len(_applications(db_session, first.credit_note.id)) == 1
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")


def test_a_failure_after_staging_rolls_back_the_whole_issue(
    db_session, subscriber_account
):
    """Atomicity is the point: no orphan note, no orphan funding, no half-apply."""
    invoice = _invoice(db_session, subscriber_account.id, "45.00")
    before_credit = get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    )
    notes_before = db_session.query(LedgerEntry).count()
    applications_before = db_session.query(CreditNoteApplication).count()

    request = CreditNoteIssuePreviewRequest(
        account_id=subscriber_account.id,
        invoice_id=invoice.id,
        currency="USD",
        subtotal=Decimal("45.00"),
        total=Decimal("45.00"),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    preview = billing_service.credit_notes.preview_issue(db_session, request)
    confirmation = CreditNoteIssueRequest(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=uuid4().hex,
    )

    with (
        patch(
            "app.services.billing.credit_notes._stage_credit_audit",
            side_effect=RuntimeError("audit exploded"),
        ),
        pytest.raises(RuntimeError),
    ):
        # The fixture itself owns an outer transaction. Exercise the staged
        # participant inside an explicit test savepoint so rolling it back does
        # not erase setup evidence committed only within that outer fixture.
        with db_session.begin_nested():
            billing_service.credit_notes.issue_with_evidence(
                db_session,
                confirmation,
                commit=False,
            )

    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("45.00")
    assert invoice.status != InvoiceStatus.paid
    assert db_session.query(CreditNoteApplication).count() == applications_before
    assert db_session.query(LedgerEntry).count() == notes_before
    assert (
        get_account_credit_balance(
            db_session,
            str(subscriber_account.id),
            currency="USD",
        )
        == before_credit
    )
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.key == confirmation.idempotency_key)
        .count()
        == 0
    )


def test_manual_application_still_works_through_the_same_participant(
    db_session, subscriber_account
):
    """Both paths share one staging participant, so evidence cannot drift."""
    target = _invoice(db_session, subscriber_account.id, "25.00")

    _, issue_result = _issue(db_session, subscriber_account.id, "80.00")
    preview = billing_service.credit_notes.preview_application(
        db_session,
        str(issue_result.credit_note.id),
        CreditNoteApplicationPreviewRequest(
            invoice_id=target.id,
            amount=Decimal("25.00"),
        ),
    )
    result = billing_service.credit_notes.apply_with_evidence(
        db_session,
        str(issue_result.credit_note.id),
        CreditNoteApplyRequest(
            invoice_id=target.id,
            amount=preview.apply_amount,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=uuid4().hex,
        ),
    )
    application = result.application

    assert application.ledger_entry_id is not None
    assert application.consumption_ledger_entry_id is not None
    consumption = db_session.get(LedgerEntry, application.consumption_ledger_entry_id)
    assert consumption.entry_type == LedgerEntryType.debit
    assert consumption.source == LedgerSource.credit_note
    assert consumption.invoice_id is None
    posting = db_session.get(LedgerEntry, application.ledger_entry_id)
    assert posting.invoice_id == target.id
    db_session.refresh(target)
    assert target.balance_due == Decimal("0.00")


def test_draft_issue_uses_the_same_application_participant_and_replays(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "55.00")
    draft = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            invoice_id=invoice.id,
            currency="USD",
            subtotal=Decimal("55.00"),
            total=Decimal("55.00"),
        ),
    )
    preview = billing_service.credit_notes.preview_draft_issue(
        db_session,
        str(draft.id),
    )
    confirmation = CreditNoteIssueConfirmation(
        preview_fingerprint=preview.fingerprint,
        idempotency_key=uuid4().hex,
    )

    first = billing_service.credit_notes.issue_draft_with_evidence(
        db_session,
        str(draft.id),
        confirmation,
    )
    replay = billing_service.credit_notes.issue_draft_with_evidence(
        db_session,
        str(draft.id),
        confirmation,
    )

    assert first.application is not None
    assert replay.application is not None
    assert replay.idempotent_replay is True
    assert replay.application.application.id == first.application.application.id
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")
    assert len(_applications(db_session, draft.id)) == 1


def test_draft_issue_rolls_back_funding_and_application_together(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "32.00")
    draft = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            invoice_id=invoice.id,
            currency="USD",
            subtotal=Decimal("32.00"),
            total=Decimal("32.00"),
        ),
    )
    preview = billing_service.credit_notes.preview_draft_issue(
        db_session,
        str(draft.id),
    )
    confirmation = CreditNoteIssueConfirmation(
        preview_fingerprint=preview.fingerprint,
        idempotency_key=uuid4().hex,
    )
    entries_before = db_session.query(LedgerEntry).count()

    with (
        patch(
            "app.services.billing.credit_notes._stage_credit_audit",
            side_effect=RuntimeError("audit exploded"),
        ),
        pytest.raises(RuntimeError),
    ):
        with db_session.begin_nested():
            billing_service.credit_notes.issue_draft_with_evidence(
                db_session,
                str(draft.id),
                confirmation,
                commit=False,
            )

    db_session.refresh(draft)
    db_session.refresh(invoice)
    assert draft.status == CreditNoteStatus.draft
    assert draft.funding_ledger_entry_id is None
    assert invoice.balance_due == Decimal("32.00")
    assert _applications(db_session, draft.id) == []
    assert db_session.query(LedgerEntry).count() == entries_before
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.key == confirmation.idempotency_key)
        .count()
        == 0
    )


def test_unrelated_negative_legacy_credit_history_does_not_leak_in(
    db_session, subscriber_account
):
    """A pre-existing debit on the account must not fund the application."""
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            amount=Decimal("500.00"),
            currency="USD",
            memo="legacy carried-in debit",
        )
    )
    db_session.commit()
    invoice = _invoice(db_session, subscriber_account.id, "35.00")

    preview, result = _issue(
        db_session, subscriber_account.id, "35.00", invoice_id=invoice.id
    )

    # Funding comes from this note's own funding entry, not the account's net
    # position, so unrelated history neither blocks nor inflates it.
    assert preview.application_amount == Decimal("35.00")
    assert result.application is not None
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")


def test_a_note_tied_to_another_invoice_is_refused_not_silently_held(
    db_session, subscriber_account
):
    """Incoherent requests still raise; only ordinary states are skipped."""
    other = _invoice(db_session, subscriber_account.id, "20.00", currency="NGN")

    request = CreditNoteIssuePreviewRequest(
        account_id=subscriber_account.id,
        invoice_id=other.id,
        currency="USD",
        subtotal=Decimal("20.00"),
        total=Decimal("20.00"),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    # Refused at preview, before any evidence exists — the currency mismatch is
    # incoherent whichever step notices it first.
    with pytest.raises(HTTPException) as rejected:
        billing_service.credit_notes.preview_issue(db_session, request)

    assert rejected.value.status_code == 400
    assert rejected.value.detail == "Currency does not match invoice"


def test_a_reversible_workflow_can_hold_its_credit(db_session, subscriber_account):
    """`apply_on_issue=False` is a recorded decision, not an oversight.

    A credit note cannot be voided once applied and there is no un-apply path,
    so billing remediation — which rolls back by voiding the note it issued —
    must hold its credit rather than settle the invoice at issue.
    """
    invoice = _invoice(db_session, subscriber_account.id, "45.00")

    result = billing_service.credit_notes.issue_system(
        db_session,
        CreditNoteIssuePreviewRequest(
            account_id=subscriber_account.id,
            invoice_id=invoice.id,
            currency="USD",
            subtotal=Decimal("45.00"),
            total=Decimal("45.00"),
            memo="Billing-integrity correction",
            line_description="Billing-integrity correction",
        ),
        idempotency_key=uuid4().hex,
        commit=True,
        apply_on_issue=False,
    )

    assert result.application is None
    assert result.credit_note.applied_total == Decimal("0.00")
    assert result.preview.application_amount == Decimal("0.00")
    assert result.preview.application_disposition == (
        CreditNoteIssueApplicationDisposition.retain_account_credit
    )
    assert result.preview.application_reason == (
        CreditNoteIssueApplicationReason.reversible_workflow_hold
    )
    assert result.preview.residual_credit == Decimal("45.00")
    # Still voidable, which is the whole reason for the hold.
    assert result.credit_note.status == CreditNoteStatus.issued
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("45.00")
