"""Canonical read and cancellation owner for customer payment intents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import TopupIntent
from app.services.billing._common import lock_account
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.topup_intents import (
    DirectTransferCancellationOutcome,
    DirectTransferCancellationSource,
    stage_cancel_unsubmitted_direct_transfer,
)

CUSTOMER_CANCEL_SCOPE = "payment-intent:cancel:self"
ADMIN_CANCEL_SCOPE = "payment-intent:cancel:admin"
_CANCEL_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_intent_management",
    concern="unsubmitted direct-transfer intent cancellation",
    name="cancel_unsubmitted_direct_transfer",
)


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
    external_id: str | None
    completed_payment_id: UUID | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, object]
    can_cancel: bool


@dataclass(frozen=True, slots=True)
class CancelPaymentIntentCommand:
    context: CommandContext
    account_id: UUID
    intent_id: UUID
    source: DirectTransferCancellationSource


def _view(intent: TopupIntent) -> PaymentIntentView:
    metadata = dict(intent.metadata_ or {})
    return PaymentIntentView(
        id=intent.id,
        reference=intent.reference,
        provider_type=intent.provider_type,
        purpose=intent.purpose,
        channel=intent.channel,
        currency=intent.currency,
        requested_amount=intent.requested_amount,
        actual_amount=intent.actual_amount,
        status=intent.status,
        external_id=intent.external_id,
        completed_payment_id=intent.completed_payment_id,
        expires_at=intent.expires_at,
        created_at=intent.created_at,
        updated_at=intent.updated_at,
        metadata=metadata,
        can_cancel=(
            intent.provider_type == "direct_bank_transfer"
            and intent.status == "pending"
            and intent.completed_payment_id is None
            and not metadata.get("payment_proof_id")
        ),
    )


def list_for_account(db: Session, account_id: UUID) -> tuple[PaymentIntentView, ...]:
    intents = db.scalars(
        select(TopupIntent)
        .where(TopupIntent.account_id == account_id)
        .order_by(TopupIntent.created_at.desc(), TopupIntent.id.desc())
    ).all()
    return tuple(_view(intent) for intent in intents)


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
