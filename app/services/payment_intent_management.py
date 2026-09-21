"""Canonical read and cancellation owner for customer payment intents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import TopupIntent
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services.billing._common import lock_account
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.topup_intents import (
    DirectTransferCancellationOutcome,
    DirectTransferCancellationSource,
    project_topup_intent_lifecycle,
    stage_cancel_unsubmitted_direct_transfer,
)

CUSTOMER_CANCEL_SCOPE = "payment-intent:cancel:self"
ADMIN_CANCEL_SCOPE = "payment-intent:cancel:admin"
_CANCEL_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_intent_management",
    concern="unsubmitted direct-transfer intent cancellation",
    name="cancel_unsubmitted_direct_transfer",
)


class PaymentIntentCancellationKind(str, Enum):
    unsubmitted_direct_transfer = "unsubmitted_direct_transfer"
    stale_submitted_proof = "stale_submitted_proof"


@dataclass(frozen=True, slots=True)
class PaymentIntentCancellationAction:
    kind: PaymentIntentCancellationKind
    label: str
    reason_label: str
    reason_placeholder: str
    confirmation_message: str
    impact: str
    proof_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PaymentIntentView:
    id: UUID
    reference: str
    provider_type: str
    purpose: str | None
    channel: str | None
    currency: str
    requested_amount: Decimal
    actual_amount: Decimal | None
    status: str
    status_label: str
    stored_status: str
    safe_reason_code: str | None
    last_verification_at: datetime | None
    blocks_another_attempt: bool
    customer_retry_allowed: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_cancel: bool
    cancellation_action: PaymentIntentCancellationAction | None


@dataclass(frozen=True, slots=True)
class CancelPaymentIntentCommand:
    context: CommandContext
    account_id: UUID
    intent_id: UUID
    source: DirectTransferCancellationSource


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _linked_proof_id(intent: TopupIntent) -> UUID | None:
    raw_id = str((intent.metadata_ or {}).get("payment_proof_id") or "").strip()
    if not raw_id:
        return None
    try:
        return UUID(raw_id)
    except ValueError:
        return None


def _cancellation_action(
    intent: TopupIntent,
    *,
    proof: PaymentProof | None,
    observed_at: datetime,
) -> PaymentIntentCancellationAction | None:
    if intent.provider_type != "direct_bank_transfer" or intent.completed_payment_id:
        return None

    raw_linked_proof_id = str(
        (intent.metadata_ or {}).get("payment_proof_id") or ""
    ).strip()
    linked_proof_id = _linked_proof_id(intent)
    if intent.status == "pending" and not raw_linked_proof_id:
        return PaymentIntentCancellationAction(
            kind=PaymentIntentCancellationKind.unsubmitted_direct_transfer,
            label="Cancel intent",
            reason_label="Cancellation reason",
            reason_placeholder="Why is this payment attempt being canceled?",
            confirmation_message=(
                "Cancel this pending payment intent? The customer will be able "
                "to start a new payment."
            ),
            impact=(
                "Cancels this unsubmitted bank-transfer attempt so the customer "
                "can start another payment."
            ),
        )

    expires_at = intent.expires_at
    if (
        intent.status != "submitted"
        or linked_proof_id is None
        or expires_at is None
        or _as_utc(expires_at) > observed_at
        or proof is None
        or proof.id != linked_proof_id
        or proof.account_id != intent.account_id
        or str(proof.reference or "").strip() != intent.reference
        or proof.status is not PaymentProofStatus.submitted
        or proof.payment_id is not None
    ):
        return None

    return PaymentIntentCancellationAction(
        kind=PaymentIntentCancellationKind.stale_submitted_proof,
        label="Cancel stale intent",
        reason_label="Rejection reason",
        reason_placeholder="Why should this unreviewed transfer proof be rejected?",
        confirmation_message=(
            "Reject the unreviewed transfer proof and cancel this expired payment "
            "intent? Confirm that no payment was received before continuing."
        ),
        impact=(
            "This expired intent still has an unreviewed transfer proof and blocks "
            "the customer's next payment. Review the proof, confirm that no payment "
            "was received, then cancel it."
        ),
        proof_id=proof.id,
    )


def _view(
    intent: TopupIntent,
    *,
    proof: PaymentProof | None,
    observed_at: datetime,
) -> PaymentIntentView:
    lifecycle = project_topup_intent_lifecycle(intent, observed_at=observed_at)
    cancellation_action = _cancellation_action(
        intent,
        proof=proof,
        observed_at=observed_at,
    )
    return PaymentIntentView(
        id=intent.id,
        reference=intent.reference,
        provider_type=intent.provider_type,
        purpose=intent.purpose,
        channel=intent.channel,
        currency=intent.currency,
        requested_amount=intent.requested_amount,
        actual_amount=intent.actual_amount,
        status=lifecycle.normalized_status.value,
        status_label=lifecycle.label,
        stored_status=lifecycle.stored_status,
        safe_reason_code=lifecycle.reason_code,
        last_verification_at=lifecycle.last_verification_at,
        blocks_another_attempt=lifecycle.blocks_another_attempt,
        customer_retry_allowed=lifecycle.customer_retry_allowed,
        expires_at=intent.expires_at,
        created_at=intent.created_at,
        updated_at=intent.updated_at,
        can_cancel=cancellation_action is not None,
        cancellation_action=cancellation_action,
    )


def list_for_account(db: Session, account_id: UUID) -> tuple[PaymentIntentView, ...]:
    intents = db.scalars(
        select(TopupIntent)
        .where(TopupIntent.account_id == account_id)
        .order_by(TopupIntent.created_at.desc(), TopupIntent.id.desc())
    ).all()
    observed_at = datetime.now(UTC)
    proof_ids = {
        proof_id
        for intent in intents
        if (proof_id := _linked_proof_id(intent)) is not None
    }
    proofs_by_id = (
        {
            proof.id: proof
            for proof in db.scalars(
                select(PaymentProof).where(PaymentProof.id.in_(proof_ids))
            ).all()
        }
        if proof_ids
        else {}
    )
    return tuple(
        _view(
            intent,
            proof=(
                proofs_by_id.get(proof_id)
                if (proof_id := _linked_proof_id(intent)) is not None
                else None
            ),
            observed_at=observed_at,
        )
        for intent in intents
    )


def get_for_account(
    db: Session,
    *,
    account_id: UUID,
    intent_id: UUID,
) -> PaymentIntentView | None:
    intent = db.scalar(
        select(TopupIntent).where(
            TopupIntent.id == intent_id,
            TopupIntent.account_id == account_id,
        )
    )
    if intent is None:
        return None
    proof_id = _linked_proof_id(intent)
    proof = db.get(PaymentProof, proof_id) if proof_id is not None else None
    return _view(intent, proof=proof, observed_at=datetime.now(UTC))


def cancel_unsubmitted_direct_transfer(
    db: Session, command: CancelPaymentIntentCommand
) -> DirectTransferCancellationOutcome:
    if command.context.scope not in {CUSTOMER_CANCEL_SCOPE, ADMIN_CANCEL_SCOPE}:
        raise ValueError("Payment-intent cancellation scope is not authorized")

    def operation() -> DirectTransferCancellationOutcome:
        lock_account(db, str(command.account_id))
        return stage_cancel_unsubmitted_direct_transfer(
            db,
            intent_id=command.intent_id,
            account_id=command.account_id,
            source=command.source,
            context=command.context,
        )

    return execute_owner_command(
        db,
        definition=_CANCEL_COMMAND,
        context=command.context,
        operation=operation,
    )
