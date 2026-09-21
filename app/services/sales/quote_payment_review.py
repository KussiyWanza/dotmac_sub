"""Staff approval owner for customer Quote deposit payments."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.notification import NotificationChannel
from app.models.sales import (
    Quote,
    QuotePaymentReview,
    QuotePaymentReviewDecision,
    QuotePaymentReviewStatus,
    QuoteStatus,
)
from app.models.system_user import SystemUser
from app.services import communication_intents, staff_notifications
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

REVIEW_PERMISSION = "sales:quote:review"
REVIEW_REQUEST_EVENT = "quote.payment_review_requested"

_REVIEW_QUOTE = OwnerCommandDefinition(
    owner="sales.quote_payment_review",
    concern="staff approval of customer Quote payment",
    name="review_quote_payment",
)
_REQUEST_REVIEW = OwnerCommandDefinition(
    owner="sales.quote_payment_review",
    concern="request customer Quote payment review",
    name="request_quote_payment_review",
)


class QuotePaymentReviewError(DomainError):
    """Stable transport-neutral failure from the payment-review owner."""


@dataclass(frozen=True, slots=True)
class ReviewQuotePaymentCommand:
    context: CommandContext
    quote_id: UUID
    reviewer_system_user_id: UUID
    expected_revision: int
    decision: QuotePaymentReviewDecision
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RequestQuotePaymentReviewCommand:
    context: CommandContext
    quote_id: UUID
    subscriber_id: UUID


@dataclass(frozen=True, slots=True)
class RequestQuotePaymentReviewOutcome:
    quote_id: UUID
    subscriber_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReviewQuotePaymentOutcome:
    quote_id: UUID
    revision: int
    status: QuotePaymentReviewStatus
    reviewed_by_system_user_id: UUID
    reviewed_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class QuotePaymentReviewProjection:
    status: QuotePaymentReviewStatus
    revision: int
    can_pay_deposit: bool
    approval_current: bool
    message: str
    reviewed_by_system_user_id: UUID | None
    reviewed_at: datetime | None
    reason: str | None


def _error(suffix: str, message: str, **details: object) -> QuotePaymentReviewError:
    return QuotePaymentReviewError(
        code=f"sales.quote_payment_review.{suffix}",
        message=message,
        details=details,
    )


def quote_fingerprint(quote: Quote) -> str:
    """Fingerprint every material fact staff approved for customer payment."""

    metadata = quote.metadata_ if isinstance(quote.metadata_, dict) else {}
    payload = {
        "quote_id": str(quote.id),
        "subscriber_id": str(quote.subscriber_id) if quote.subscriber_id else None,
        "is_active": bool(quote.is_active),
        "project_type": quote.project_type,
        "currency": quote.currency,
        "subtotal": str(Decimal(quote.subtotal or 0)),
        "discount_type": quote.discount_type,
        "discount_value": (
            str(Decimal(quote.discount_value))
            if quote.discount_value is not None
            else None
        ),
        "discount_amount": str(Decimal(quote.discount_amount or 0)),
        "discount_revision": int(quote.discount_revision or 0),
        "tax_rate": (
            str(Decimal(quote.tax_rate)) if quote.tax_rate is not None else None
        ),
        "tax_total": str(Decimal(quote.tax_total or 0)),
        "total": str(Decimal(quote.total or 0)),
        "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
        "install": metadata.get("install"),
        "feasibility": metadata.get("feasibility"),
        "deposit_percent": metadata.get("deposit_percent"),
        "estimate_provisional": metadata.get("estimate_provisional"),
        "pricing_mode": metadata.get("pricing_mode"),
        "lines": [
            {
                "id": str(line.id),
                "description": line.description,
                "quantity": str(Decimal(line.quantity or 0)),
                "unit_price": str(Decimal(line.unit_price or 0)),
                "amount": str(Decimal(line.amount or 0)),
                "inventory_item_id": (
                    str(line.inventory_item_id) if line.inventory_item_id else None
                ),
                "metadata": line.metadata_ if isinstance(line.metadata_, dict) else {},
            }
            for line in sorted(quote.line_items, key=lambda item: str(item.id))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def resolve_payment_review(quote: Quote) -> QuotePaymentReviewProjection:
    try:
        stored_status = QuotePaymentReviewStatus(quote.payment_review_status)
    except (TypeError, ValueError):
        stored_status = QuotePaymentReviewStatus.pending
    current_fingerprint = quote_fingerprint(quote)
    approval_current = bool(
        stored_status is QuotePaymentReviewStatus.approved
        and quote.payment_review_fingerprint == current_fingerprint
    )
    can_pay = bool(
        approval_current
        and quote.is_active
        and quote.status in {QuoteStatus.draft.value, QuoteStatus.sent.value}
    )
    if stored_status is QuotePaymentReviewStatus.rejected:
        message = "This Quote was not approved. Please contact support for help."
    elif approval_current:
        message = "Approved — payment required."
    elif stored_status is QuotePaymentReviewStatus.approved:
        message = "This Quote changed after approval and is awaiting staff review."
    else:
        message = (
            "Your estimate is under staff review. We will notify you before you "
            "should proceed with payment."
        )
    return QuotePaymentReviewProjection(
        status=(
            QuotePaymentReviewStatus.approved
            if approval_current
            else (
                QuotePaymentReviewStatus.rejected
                if stored_status is QuotePaymentReviewStatus.rejected
                else QuotePaymentReviewStatus.pending
            )
        ),
        revision=int(quote.payment_review_revision or 0),
        can_pay_deposit=can_pay,
        approval_current=approval_current,
        message=message,
        reviewed_by_system_user_id=quote.payment_reviewed_by_system_user_id,
        reviewed_at=quote.payment_reviewed_at,
        reason=quote.payment_review_reason,
    )


def queue_staff_review_request(db: Session, quote: Quote) -> None:
    staff_notifications.queue_permission_review_request(
        db,
        permission_key=REVIEW_PERMISSION,
        fingerprint=f"quote-payment-review:{quote.id}",
        event_type=REVIEW_REQUEST_EVENT,
        title="Quote awaiting payment approval",
        body=(
            f"Quote {quote.id} is ready for an address, feasibility, and price review."
        ),
        target_url=f"/admin/sales/quotes/{quote.id}",
        category="sales",
        source="sales.quote_payment_review",
    )


def _request_operation(
    db: Session, command: RequestQuotePaymentReviewCommand
) -> RequestQuotePaymentReviewOutcome:
    quote = db.scalars(
        select(Quote).where(Quote.id == command.quote_id).with_for_update()
    ).one_or_none()
    if (
        quote is None
        or not quote.is_active
        or quote.subscriber_id != command.subscriber_id
    ):
        raise _error("quote_not_found", "Quote not found.")
    if quote.status not in {QuoteStatus.draft.value, QuoteStatus.sent.value}:
        raise _error(
            "quote_status_invalid",
            "Only an active Draft or Sent Quote can be submitted for review.",
        )
    if quote.payment_review_status != QuotePaymentReviewStatus.pending.value:
        raise _error("review_not_pending", "This Quote is not awaiting staff review.")
    queue_staff_review_request(db, quote)
    result = communication_intents.submit(
        db,
        communication_intents.CommunicationIntent(
            subscriber_id=command.subscriber_id,
            event_type=REVIEW_REQUEST_EVENT,
            category="sales",
            subject="Your installation estimate is under review",
            body=(
                "We received your installation request and prepared an estimate. "
                "Our staff will review the address, feasibility, and price. We "
                "will notify you before payment is available."
            ),
            default_channels=(NotificationChannel.push, NotificationChannel.email),
            include_reseller=False,
            metadata={"quote_id": str(quote.id)},
            dedupe_key=f"quote-payment-review-requested:{quote.id}",
        ),
    )
    if not result.replayed:
        emit_event(
            db,
            EventType.quote_payment_review_requested,
            {
                "quote_id": str(quote.id),
                "subscriber_id": str(command.subscriber_id),
                "payment_review_status": QuotePaymentReviewStatus.pending.value,
            },
            actor=command.context.actor,
            subscriber_id=command.subscriber_id,
        )
        stage_audit_event(
            db,
            action="quote.payment_review_requested",
            entity_type="quote",
            entity_id=str(quote.id),
            actor=AuditActor.user(str(command.subscriber_id)),
            request_id=str(command.context.command_id),
            metadata={"payment_review_status": QuotePaymentReviewStatus.pending.value},
        )
    db.flush()
    return RequestQuotePaymentReviewOutcome(
        quote_id=quote.id,
        subscriber_id=command.subscriber_id,
        replayed=result.replayed,
    )


def request_quote_payment_review(
    db: Session, command: RequestQuotePaymentReviewCommand
) -> RequestQuotePaymentReviewOutcome:
    """Queue the staff/customer review notices for one completed estimate."""

    return execute_owner_command(
        db,
        definition=_REQUEST_REVIEW,
        context=command.context,
        operation=lambda: _request_operation(db, command),
    )


def _command_fingerprint(command: ReviewQuotePaymentCommand) -> str:
    payload = {
        "quote_id": str(command.quote_id),
        "reviewer_system_user_id": str(command.reviewer_system_user_id),
        "expected_revision": command.expected_revision,
        "decision": command.decision.value,
        "reason": (command.reason or "").strip() or None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _reviewer(db: Session, reviewer_id: UUID) -> SystemUser:
    reviewer = db.scalars(
        select(SystemUser).where(SystemUser.id == reviewer_id).with_for_update()
    ).one_or_none()
    authorized_ids = {
        user.id
        for user in staff_notifications.system_users_with_permission(
            db, REVIEW_PERMISSION
        )
    }
    if reviewer is None or not reviewer.is_active or reviewer.id not in authorized_ids:
        raise _error(
            "reviewer_not_authorized",
            "The authenticated staff user cannot review Quote payments.",
        )
    return reviewer


def _notify_customer(
    db: Session,
    *,
    quote: Quote,
    decision: QuotePaymentReviewDecision,
    revision: int,
) -> None:
    if quote.subscriber_id is None:
        return
    approved = decision is QuotePaymentReviewDecision.approve
    communication_intents.submit(
        db,
        communication_intents.CommunicationIntent(
            subscriber_id=quote.subscriber_id,
            event_type=(
                EventType.quote_payment_approved.value
                if approved
                else EventType.quote_payment_rejected.value
            ),
            category="sales",
            subject=(
                "Your installation Quote is approved"
                if approved
                else "Update on your installation Quote"
            ),
            body=(
                "Your Quote has been approved. Open Get a Quote to review the "
                "estimate and pay the deposit."
                if approved
                else "Your Quote was not approved. Please contact support if you "
                "would like help with the installation request."
            ),
            default_channels=(NotificationChannel.push, NotificationChannel.email),
            include_reseller=False,
            metadata={"quote_id": str(quote.id), "revision": revision},
            dedupe_key=f"quote-payment-review:{quote.id}:{revision}:{decision.value}",
        ),
    )


def _operation(
    db: Session, command: ReviewQuotePaymentCommand
) -> ReviewQuotePaymentOutcome:
    fingerprint = _command_fingerprint(command)
    replay = db.scalars(
        select(QuotePaymentReview).where(
            QuotePaymentReview.command_id == command.context.command_id
        )
    ).one_or_none()
    if replay is not None:
        if replay.command_fingerprint != fingerprint:
            raise _error(
                "command_conflict",
                "This review command was already used with different values.",
            )
        return ReviewQuotePaymentOutcome(
            quote_id=replay.quote_id,
            revision=replay.revision,
            status=(
                QuotePaymentReviewStatus.approved
                if replay.decision == QuotePaymentReviewDecision.approve.value
                else QuotePaymentReviewStatus.rejected
            ),
            reviewed_by_system_user_id=replay.reviewed_by_system_user_id,
            reviewed_at=replay.reviewed_at,
            replayed=True,
        )

    reviewer = _reviewer(db, command.reviewer_system_user_id)
    quote = db.scalars(
        select(Quote)
        .where(Quote.id == command.quote_id)
        .options(selectinload(Quote.line_items))
        .with_for_update()
    ).one_or_none()
    if quote is None or not quote.is_active:
        raise _error("quote_not_found", "Quote not found.")
    if quote.subscriber_id is None:
        raise _error(
            "customer_required", "Only a customer-linked Quote can be approved."
        )
    if quote.status not in {QuoteStatus.draft.value, QuoteStatus.sent.value}:
        raise _error(
            "quote_status_invalid",
            "Only an active Draft or Sent Quote can be reviewed for payment.",
        )
    current_revision = int(quote.payment_review_revision or 0)
    if command.expected_revision != current_revision:
        raise _error(
            "revision_conflict",
            "This Quote review changed. Refresh the page and review it again.",
            expected_revision=command.expected_revision,
            current_revision=current_revision,
        )
    reason = (command.reason or "").strip() or None
    if reason is not None and len(reason) > 500:
        raise _error("reason_invalid", "Review notes cannot exceed 500 characters.")
    if command.decision is QuotePaymentReviewDecision.reject and reason is None:
        raise _error("reason_required", "Add a reason before rejecting this Quote.")

    quote_snapshot = quote_fingerprint(quote)
    projection = resolve_payment_review(quote)
    if (
        command.decision is QuotePaymentReviewDecision.approve
        and projection.approval_current
    ):
        raise _error("already_approved", "This Quote is already approved for payment.")

    now = datetime.now(UTC)
    revision = current_revision + 1
    status = (
        QuotePaymentReviewStatus.approved
        if command.decision is QuotePaymentReviewDecision.approve
        else QuotePaymentReviewStatus.rejected
    )
    quote.payment_review_status = status.value
    quote.payment_review_revision = revision
    quote.payment_reviewed_by_system_user_id = reviewer.id
    quote.payment_reviewed_at = now
    quote.payment_review_reason = reason
    quote.payment_review_fingerprint = quote_snapshot
    if status is QuotePaymentReviewStatus.rejected:
        quote.status = QuoteStatus.rejected.value

    db.add(
        QuotePaymentReview(
            quote_id=quote.id,
            revision=revision,
            decision=command.decision.value,
            reason=reason,
            reviewed_by_system_user_id=reviewer.id,
            reviewed_at=now,
            quote_fingerprint=quote_snapshot,
            command_id=command.context.command_id,
            command_fingerprint=fingerprint,
        )
    )
    event_type = (
        EventType.quote_payment_approved
        if status is QuotePaymentReviewStatus.approved
        else EventType.quote_payment_rejected
    )
    emit_event(
        db,
        event_type,
        {
            "quote_id": str(quote.id),
            "subscriber_id": str(quote.subscriber_id),
            "reviewer_system_user_id": str(reviewer.id),
            "revision": revision,
            "quote_fingerprint": quote_snapshot,
            "reason": reason,
        },
        actor=command.context.actor,
        subscriber_id=quote.subscriber_id,
    )
    stage_audit_event(
        db,
        action=f"quote.payment_{status.value}",
        entity_type="quote",
        entity_id=str(quote.id),
        actor=AuditActor.user(str(reviewer.id)),
        request_id=str(command.context.command_id),
        metadata={
            "revision": revision,
            "quote_fingerprint": quote_snapshot,
            "reason": reason,
        },
    )
    _notify_customer(
        db,
        quote=quote,
        decision=command.decision,
        revision=revision,
    )
    staff_notifications.resolve_permission_review_request(
        db,
        fingerprint=f"quote-payment-review:{quote.id}",
        event_type=REVIEW_REQUEST_EVENT,
    )
    db.flush()
    return ReviewQuotePaymentOutcome(
        quote_id=quote.id,
        revision=revision,
        status=status,
        reviewed_by_system_user_id=reviewer.id,
        reviewed_at=now,
        replayed=False,
    )


def review_quote_payment(
    db: Session, command: ReviewQuotePaymentCommand
) -> ReviewQuotePaymentOutcome:
    """Approve or reject one exact Quote snapshot for customer payment."""

    return execute_owner_command(
        db,
        definition=_REVIEW_QUOTE,
        context=command.context,
        operation=lambda: _operation(db, command),
    )
