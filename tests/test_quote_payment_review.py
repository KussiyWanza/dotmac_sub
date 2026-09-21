"""Staff Quote-payment approval command and evidence tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.notification import CommunicationIntentRecord
from app.models.rbac import Role, SystemUserRole
from app.models.sales import (
    Quote,
    QuotePaymentReview,
    QuotePaymentReviewDecision,
    QuotePaymentReviewStatus,
    QuoteStatus,
)
from app.models.system_user import SystemUser
from app.services.owner_commands import CommandContext
from app.services.sales import quote_payment_review


def _reviewer(db_session, *, authorized: bool = True) -> SystemUser:
    reviewer = SystemUser(
        first_name="Ada",
        last_name="Reviewer",
        email=f"reviewer-{uuid4()}@example.com",
        is_active=True,
    )
    db_session.add(reviewer)
    db_session.flush()
    if authorized:
        admin = Role(name="admin", description="Test administrator", is_active=True)
        db_session.add(admin)
        db_session.flush()
        db_session.add(SystemUserRole(system_user_id=reviewer.id, role_id=admin.id))
    db_session.commit()
    return reviewer


def _pending_quote(db_session, subscriber) -> Quote:
    quote = Quote(
        subscriber_id=subscriber.id,
        status=QuoteStatus.draft.value,
        currency="NGN",
        project_type="fiber_installation",
        subtotal=Decimal("100000.00"),
        tax_total=Decimal("7500.00"),
        total=Decimal("107500.00"),
        expires_at=datetime.now(UTC) + timedelta(days=7),
        metadata_={
            "source": "portal_self_serve",
            "install": {
                "latitude": 9.0765,
                "longitude": 7.3986,
                "address": "Pinned installation address",
                "region": "Abuja",
            },
            "feasibility": {"feasible": True, "distance_meters": 42.0},
            "deposit_percent": 50,
        },
        payment_review_status=QuotePaymentReviewStatus.pending.value,
        is_active=True,
    )
    db_session.add(quote)
    db_session.commit()
    return quote


def _command(
    quote: Quote,
    reviewer: SystemUser,
    *,
    command_id=None,
    decision: QuotePaymentReviewDecision = QuotePaymentReviewDecision.approve,
    reason: str | None = None,
) -> quote_payment_review.ReviewQuotePaymentCommand:
    resolved_id = command_id or uuid4()
    return quote_payment_review.ReviewQuotePaymentCommand(
        context=CommandContext.system(
            actor=str(reviewer.id),
            scope="sales:quote:review",
            reason="Review customer Quote for payment",
            command_id=resolved_id,
            idempotency_key=f"quote-payment-review:{quote.id}:{resolved_id}",
        ),
        quote_id=quote.id,
        reviewer_system_user_id=reviewer.id,
        expected_revision=int(quote.payment_review_revision or 0),
        decision=decision,
        reason=reason,
    )


def test_approval_records_reviewer_time_revision_and_exact_snapshot(
    db_session, subscriber
):
    reviewer = _reviewer(db_session)
    quote = _pending_quote(db_session, subscriber)
    command = _command(quote, reviewer)
    db_session.rollback()

    outcome = quote_payment_review.review_quote_payment(db_session, command)

    db_session.refresh(quote)
    evidence = db_session.query(QuotePaymentReview).one()
    projection = quote_payment_review.resolve_payment_review(quote)
    assert outcome.status is QuotePaymentReviewStatus.approved
    assert outcome.revision == 1
    assert quote.payment_reviewed_by_system_user_id == reviewer.id
    assert quote.payment_reviewed_at is not None
    assert evidence.reviewed_by_system_user_id == reviewer.id
    assert evidence.quote_fingerprint == quote.payment_review_fingerprint
    assert projection.approval_current is True
    assert projection.can_pay_deposit is True
    intent = (
        db_session.query(CommunicationIntentRecord)
        .filter(CommunicationIntentRecord.event_type == "quote.payment_approved")
        .one()
    )
    assert intent.subscriber_id == subscriber.id


def test_review_command_replays_and_rejects_changed_command_reuse(
    db_session, subscriber
):
    reviewer = _reviewer(db_session)
    quote = _pending_quote(db_session, subscriber)
    command_id = uuid4()
    command = _command(quote, reviewer, command_id=command_id)
    db_session.rollback()

    first = quote_payment_review.review_quote_payment(db_session, command)
    replay = quote_payment_review.review_quote_payment(db_session, command)

    assert first.replayed is False
    assert replay.replayed is True
    assert db_session.query(QuotePaymentReview).count() == 1
    changed = _command(
        quote,
        reviewer,
        command_id=command_id,
        decision=QuotePaymentReviewDecision.reject,
        reason="Address cannot be served",
    )
    db_session.rollback()
    with pytest.raises(quote_payment_review.QuotePaymentReviewError) as exc_info:
        quote_payment_review.review_quote_payment(db_session, changed)
    assert exc_info.value.code == "sales.quote_payment_review.command_conflict"


def test_review_command_rejects_staff_without_review_permission(db_session, subscriber):
    reviewer = _reviewer(db_session, authorized=False)
    quote = _pending_quote(db_session, subscriber)
    command = _command(quote, reviewer)
    db_session.rollback()

    with pytest.raises(quote_payment_review.QuotePaymentReviewError) as exc_info:
        quote_payment_review.review_quote_payment(db_session, command)

    assert exc_info.value.code == "sales.quote_payment_review.reviewer_not_authorized"
    db_session.refresh(quote)
    assert quote.payment_review_status == QuotePaymentReviewStatus.pending.value
    assert db_session.query(QuotePaymentReview).count() == 0
