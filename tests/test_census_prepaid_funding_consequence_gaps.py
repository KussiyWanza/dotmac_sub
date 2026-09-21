"""Non-vacuous proof of the census's zero-child successful-receipt check.

`find_successful_receipts_missing_child_evidence`
(`scripts/billing/census_prepaid_funding_consequence_gaps.py`) is the
structural check added 2026-09 round 7 for the exact defect this whole
round targets: a `draft_invoice_settled`/`funded` receipt that committed
with ZERO `PrepaidFundingTriggerSubscriptionOutcome` child rows. This plants
both shapes directly against the model layer -- a genuine bug in the
write-site invariant (`_record_prepaid_funding_trigger_execution`) would now
raise before such a row could ever be committed, so this test is deliberately
independent of that write path: it proves the DETECTOR itself is sound
against a hand-planted row, the way a real historical/pre-fix row (or a
future regression that reaches the database some other way) would look.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.event_store import EventStore
from app.models.prepaid_funding import (
    PrepaidFundingTriggerExecution,
    PrepaidFundingTriggerSubscriptionOutcome,
)
from app.services.events.types import Event, EventType
from scripts.billing.census_prepaid_funding_consequence_gaps import (
    find_successful_receipts_missing_child_evidence,
)


def _event_store_row(db_session, *, account_id) -> EventStore:
    from app.services import event_store as event_store_service

    event = Event(
        event_type=EventType.payment_received,
        payload={},
        account_id=account_id,
    )
    record = event_store_service.create_event_record(db_session, event)
    db_session.flush()
    return record


def _receipt(
    db_session,
    *,
    account_id,
    disposition: str,
) -> PrepaidFundingTriggerExecution:
    event_store_row = _event_store_row(db_session, account_id=account_id)
    receipt = PrepaidFundingTriggerExecution(
        event_store_id=event_store_row.id,
        event_id=event_store_row.event_id,
        event_type=event_store_row.event_type,
        account_id=account_id,
        currency="NGN",
        effective_at=datetime(2026, 7, 1, tzinfo=UTC),
        request_fingerprint="a" * 64,
        outcome_fingerprint="b" * 64,
        disposition=disposition,
        # Explicit, timezone-aware, not left to the column's
        # `server_default=sa.func.now()`: on SQLite (this test's engine)
        # that default produces a NAIVE datetime, which then fails to
        # subtract against `find_successful_receipts_missing_child_evidence`'s
        # `datetime.now(UTC)` with `TypeError: can't subtract offset-naive
        # and offset-aware datetimes`.
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    db_session.add(receipt)
    db_session.flush()
    return receipt


def test_flags_a_successful_receipt_with_zero_children(db_session, subscriber):
    receipt = _receipt(db_session, account_id=subscriber.id, disposition="funded")
    db_session.commit()

    cases = find_successful_receipts_missing_child_evidence(
        db_session, account_id=subscriber.id
    )

    assert len(cases) == 1
    assert cases[0]["trigger_execution_id"] == str(receipt.id)
    assert cases[0]["disposition"] == "funded"


def test_does_not_flag_a_successful_receipt_with_a_real_child(
    db_session, subscriber, subscription
):
    receipt = _receipt(
        db_session, account_id=subscriber.id, disposition="draft_invoice_settled"
    )
    db_session.add(
        PrepaidFundingTriggerSubscriptionOutcome(
            trigger_execution_id=receipt.id,
            subscription_id=subscription.id,
            period_start=datetime(2026, 7, 1, tzinfo=UTC),
            period_end=datetime(2026, 8, 1, tzinfo=UTC),
            disposition="existing_draft_settled",
            funding_source="opening_funding",
            funding_evidence_ids=[str(uuid4())],
            amount=Decimal("100.00"),
            currency="NGN",
            evidence_fingerprint="c" * 64,
        )
    )
    db_session.commit()

    cases = find_successful_receipts_missing_child_evidence(
        db_session, account_id=subscriber.id
    )

    assert cases == []


def test_does_not_flag_a_blocked_disposition_with_zero_children(db_session, subscriber):
    """A genuinely blocked/review-required disposition legitimately has no
    children -- this check is scoped to SUCCESSFUL dispositions only, not a
    blanket zero-children alarm."""

    _receipt(
        db_session,
        account_id=subscriber.id,
        disposition="draft_invoice_review_required",
    )
    db_session.commit()

    cases = find_successful_receipts_missing_child_evidence(
        db_session, account_id=subscriber.id
    )

    assert cases == []
