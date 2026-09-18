"""`PrepaidDraftReconciliationException` writer: idempotency and evidence.

`record_prepaid_draft_reconciliation_exception` (`app.services
.prepaid_draft_reconciliation`) is the first writer this durable review-item
table has ever had — every ambiguous/insufficient prepaid funding
classification now goes through it, whether raised by the account-level
pre-existing-draft path (`stage_prepaid_draft_after_funding_change`) or the
funding-consequence owner's own ambiguous-classification path
(`financial.prepaid_service_renewals`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.models.prepaid_funding import PrepaidDraftReconciliationException
from app.models.subscriber import Subscriber
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_draft_reconciliation import (
    record_prepaid_draft_reconciliation_exception,
)


def _utc(value: datetime) -> datetime:
    """Defensively normalize a possibly-naive datetime to aware UTC.

    SQLite (this test's engine) does not preserve timezone info on
    round-trip regardless of what is inserted (2026-09, round 9): after
    `db_session.commit()` expires the ORM instance, the next attribute read
    re-fetches from the database and comes back NAIVE even though an aware
    value was originally assigned. PostgreSQL preserves timezone correctly,
    so this is purely a SQLite unit-test-tier artifact, not evidence of a
    production bug in the writer under test.
    """

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _draft_invoice(db_session, account_id) -> Invoice:
    invoice = Invoice(
        account_id=account_id,
        invoice_number=f"INV-REVIEW-{uuid4().hex[:8]}",
        status=InvoiceStatus.draft,
        currency="NGN",
        total=Decimal("1000.00"),
        subtotal=Decimal("1000.00"),
        balance_due=Decimal("1000.00"),
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def test_records_full_evidence_including_subscription_and_period(
    db_session, subscriber, subscription
):
    invoice = _draft_invoice(db_session, subscriber.id)
    # A real `Subscription` row, not a bare `uuid4()` (2026-09, round 7):
    # `PrepaidDraftReconciliationException.subscription_id` is a real FK
    # (`ondelete="RESTRICT"`), so an arbitrary UUID with no matching row
    # fails the insert with an `IntegrityError` rather than exercising this
    # writer's evidence-recording behavior.
    subscription_id = subscription.id
    starts_at = datetime.now(UTC)
    ends_at = starts_at + timedelta(days=30)
    fingerprint = "a" * 64

    exception = record_prepaid_draft_reconciliation_exception(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        currency="NGN",
        required_amount=Decimal("1000.00"),
        payment_backed_amount=Decimal("400.00"),
        opening_funding_amount=Decimal("0.00"),
        preview_fingerprint=fingerprint,
        reason="renewal_insufficient_funding",
        subscription_id=subscription_id,
        period_start=starts_at,
        period_end=ends_at,
        detail="short by 600.00",
    )
    db_session.commit()

    assert exception.status == "open"
    assert exception.reason == "renewal_insufficient_funding"
    assert exception.subscription_id == subscription_id
    assert _utc(exception.period_start) == starts_at
    assert _utc(exception.period_end) == ends_at
    assert exception.attempt_count == 1

    stored = db_session.get(PrepaidDraftReconciliationException, exception.id)
    assert stored is not None
    assert stored.invoice_id == invoice.id


def test_is_idempotent_on_invoice_id_and_bumps_attempt_count_on_new_evidence(
    db_session, subscriber
):
    invoice = _draft_invoice(db_session, subscriber.id)

    first = record_prepaid_draft_reconciliation_exception(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        currency="NGN",
        required_amount=Decimal("1000.00"),
        payment_backed_amount=Decimal("400.00"),
        opening_funding_amount=Decimal("0.00"),
        preview_fingerprint="a" * 64,
        reason="renewal_insufficient_funding",
    )
    db_session.commit()
    first_id = first.id

    # Same fingerprint: a mere replay of the same observation. Attempt count
    # must not inflate on every retry of an unresolved, unchanged case.
    replay = record_prepaid_draft_reconciliation_exception(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        currency="NGN",
        required_amount=Decimal("1000.00"),
        payment_backed_amount=Decimal("400.00"),
        opening_funding_amount=Decimal("0.00"),
        preview_fingerprint="a" * 64,
        reason="renewal_insufficient_funding",
    )
    db_session.commit()
    assert replay.id == first_id
    assert replay.attempt_count == 1

    # New evidence (a different fingerprint): genuinely new observed state,
    # so the attempt is counted.
    updated = record_prepaid_draft_reconciliation_exception(
        db_session,
        account_id=subscriber.id,
        invoice_id=invoice.id,
        currency="NGN",
        required_amount=Decimal("1000.00"),
        payment_backed_amount=Decimal("700.00"),
        opening_funding_amount=Decimal("0.00"),
        preview_fingerprint="b" * 64,
        reason="renewal_insufficient_funding",
    )
    db_session.commit()
    assert updated.id == first_id
    assert updated.attempt_count == 2
    assert updated.payment_backed_amount == Decimal("700.00")

    assert db_session.query(PrepaidDraftReconciliationException).count() == 1, (
        "one invoice must never accumulate more than one open review row"
    )


_REVIEW_DEFINITION = OwnerCommandDefinition(
    owner="financial.prepaid_draft_reconciliation",
    concern="stranded prepaid draft invoice reconciliation",
    name="test_review_item_owner_savepoint",
)


def _record_owner_review(db: Session, account_id: UUID, invoice_id: UUID) -> UUID:
    review = record_prepaid_draft_reconciliation_exception(
        db,
        account_id=account_id,
        invoice_id=invoice_id,
        currency="NGN",
        required_amount=Decimal("1000.00"),
        payment_backed_amount=Decimal("400.00"),
        opening_funding_amount=Decimal("0.00"),
        preview_fingerprint="c" * 64,
        reason="renewal_insufficient_funding",
    )
    return review.id


def test_review_item_can_be_created_inside_the_public_owner_command(
    db_session: Session,
    subscriber: Subscriber,
) -> None:
    invoice = _draft_invoice(db_session, subscriber.id)
    account_id, invoice_id = subscriber.id, invoice.id
    db_session.commit()
    review_id = execute_owner_command(
        db_session,
        definition=_REVIEW_DEFINITION,
        context=CommandContext.system(
            actor="pytest:review",
            scope="billing:write",
            reason="Verify funding review savepoint",
        ),
        operation=lambda: _record_owner_review(db_session, account_id, invoice_id),
    )
    assert not db_session.in_transaction()
    assert db_session.get(PrepaidDraftReconciliationException, review_id) is not None


def test_lost_review_insert_race_preserves_the_owner_transaction(
    db_session: Session,
    subscriber: Subscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoice = _draft_invoice(db_session, subscriber.id)
    account_id, invoice_id = subscriber.id, invoice.id
    existing_id = _record_owner_review(db_session, account_id, invoice_id)
    db_session.commit()
    scalar = db_session.scalar
    missed = False

    def miss_first_review_lookup(statement, *args, **kwargs):
        nonlocal missed
        entities = getattr(statement, "column_descriptions", ())
        if (
            not missed
            and entities
            and entities[0].get("entity") is PrepaidDraftReconciliationException
        ):
            missed = True
            return None
        return scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", miss_first_review_lookup)
    replay_id = execute_owner_command(
        db_session,
        definition=_REVIEW_DEFINITION,
        context=CommandContext.system(
            actor="pytest:review",
            scope="billing:write",
            reason="Verify duplicate review recovery",
        ),
        operation=lambda: _record_owner_review(db_session, account_id, invoice_id),
    )
    assert missed
    assert replay_id == existing_id
    assert not db_session.in_transaction()
    assert (
        len(db_session.scalars(select(PrepaidDraftReconciliationException)).all()) == 1
    )
