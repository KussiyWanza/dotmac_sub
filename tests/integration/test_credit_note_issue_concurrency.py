"""PostgreSQL serialization for credit-note application on issue."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    CreditNote,
    CreditNoteApplication,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerSource,
)
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.schemas.billing import CreditNoteIssuePreviewRequest, CreditNoteIssueRequest
from app.services import billing as billing_service
from app.services.subscriber import _default_reseller_id


def _setup_issue(engine, *, amount: Decimal):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    with session_factory() as setup:
        account = Subscriber(
            first_name="Credit Note",
            last_name="Concurrency",
            email=f"credit-note-concurrency-{suffix}@example.com",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(account)
        setup.flush()
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"INV-CREDIT-CONC-{suffix}",
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=amount,
            total=amount,
            balance_due=amount,
        )
        setup.add(invoice)
        setup.commit()
        request = CreditNoteIssuePreviewRequest(
            account_id=account.id,
            invoice_id=invoice.id,
            currency="NGN",
            subtotal=amount,
            total=amount,
            memo="PostgreSQL issue concurrency",
            line_description="PostgreSQL issue concurrency",
        )
        preview = billing_service.credit_notes.preview_issue(setup, request)
        setup.commit()
        return session_factory, account.id, invoice.id, request, preview.fingerprint


def _assert_one_issue_evidence_set(
    session_factory,
    *,
    account_id: UUID,
    invoice_id: UUID,
    amount: Decimal,
) -> None:
    with session_factory() as check:
        invoice = check.get(Invoice, invoice_id)
        notes = (
            check.query(CreditNote).filter(CreditNote.account_id == account_id).all()
        )
        applications = (
            check.query(CreditNoteApplication)
            .filter(CreditNoteApplication.invoice_id == invoice_id)
            .all()
        )
        ledger_entries = (
            check.query(LedgerEntry)
            .filter(LedgerEntry.account_id == account_id)
            .filter(LedgerEntry.source == LedgerSource.credit_note)
            .all()
        )
        reservations = (
            check.query(IdempotencyKey)
            .filter(IdempotencyKey.account_id == account_id)
            .filter(
                IdempotencyKey.scope.in_(
                    ["credit_note_issue", "credit_note_application"]
                )
            )
            .all()
        )

        assert invoice is not None
        assert invoice.balance_due == Decimal("0.00")
        assert invoice.status == InvoiceStatus.paid
        assert len(notes) == 1
        assert notes[0].applied_total == amount
        assert len(applications) == 1
        assert applications[0].amount == amount
        assert len(ledger_entries) == 3
        assert len(reservations) == 2


def test_same_issue_key_replays_after_waiting_for_the_account_lock(engine):
    amount = Decimal("125.00")
    session_factory, account_id, invoice_id, request, fingerprint = _setup_issue(
        engine,
        amount=amount,
    )
    key = f"credit-note-concurrency-{uuid4().hex}"
    barrier = Barrier(2)

    def issue():
        with session_factory() as worker:
            barrier.wait(timeout=10)
            return billing_service.credit_notes.issue_with_evidence(
                worker,
                CreditNoteIssueRequest(
                    **request.model_dump(),
                    preview_fingerprint=fingerprint,
                    idempotency_key=key,
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: issue(), range(2)))

    assert sorted(result.idempotent_replay for result in results) == [False, True]
    assert results[0].credit_note.id == results[1].credit_note.id
    assert results[0].application is not None
    assert results[1].application is not None
    assert (
        results[0].application.application.id == results[1].application.application.id
    )
    _assert_one_issue_evidence_set(
        session_factory,
        account_id=account_id,
        invoice_id=invoice_id,
        amount=amount,
    )


def test_distinct_issue_keys_cannot_apply_one_confirmed_receivable_twice(engine):
    amount = Decimal("85.00")
    session_factory, account_id, invoice_id, request, fingerprint = _setup_issue(
        engine,
        amount=amount,
    )
    keys = [
        f"credit-note-concurrency-{uuid4().hex}",
        f"credit-note-concurrency-{uuid4().hex}",
    ]
    barrier = Barrier(2)

    def issue(key: str) -> tuple[str, int | None]:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            try:
                billing_service.credit_notes.issue_with_evidence(
                    worker,
                    CreditNoteIssueRequest(
                        **request.model_dump(),
                        preview_fingerprint=fingerprint,
                        idempotency_key=key,
                    ),
                )
            except HTTPException as exc:
                return "rejected", exc.status_code
            return "issued", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(issue, keys))

    assert sorted(outcomes) == [("issued", None), ("rejected", 409)]
    _assert_one_issue_evidence_set(
        session_factory,
        account_id=account_id,
        invoice_id=invoice_id,
        amount=amount,
    )
