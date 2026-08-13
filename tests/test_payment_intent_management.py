from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.models.billing import TopupIntent
from app.services import payment_intent_management
from app.services.owner_commands import CommandContext
from app.services.topup_intents import (
    DirectTransferCancellationSource,
    TopupIntentError,
)


def _intent(
    db_session,
    account_id: UUID,
    *,
    metadata: dict[str, object] | None = None,
) -> tuple[TopupIntent, UUID]:
    intent = TopupIntent(
        account_id=account_id,
        reference=f"TRF-{uuid4().hex[:12]}",
        provider_type="direct_bank_transfer",
        currency="NGN",
        requested_amount=Decimal("1499.99"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        metadata_=metadata or {"payment_flow": "account_topup"},
    )
    db_session.add(intent)
    db_session.flush()
    intent_id = intent.id
    db_session.commit()
    return intent, intent_id


def _command(
    account_id: UUID,
    intent_id: UUID,
    *,
    reason: str = "Failed pending intent",
) -> payment_intent_management.CancelPaymentIntentCommand:
    return payment_intent_management.CancelPaymentIntentCommand(
        context=CommandContext.system(
            actor="user:samuel-ojo",
            scope=payment_intent_management.ADMIN_CANCEL_SCOPE,
            reason=reason,
            idempotency_key=f"cancel:{intent_id}",
        ),
        account_id=account_id,
        intent_id=intent_id,
        source=DirectTransferCancellationSource.admin_customer_billing,
    )


def test_cancels_pending_unsubmitted_direct_transfer(db_session, subscriber):
    account_id = subscriber.id
    intent, intent_id = _intent(db_session, account_id)
    command = _command(account_id, intent_id)

    outcome = payment_intent_management.cancel_unsubmitted_direct_transfer(
        db_session, command
    )

    db_session.refresh(intent)
    assert outcome.changed is True
    assert intent.status == "canceled"
    assert intent.metadata_["cancellation"]["reason"] == "Failed pending intent"
    assert intent.metadata_["cancellation"]["source"] == "admin_customer_billing"


def test_rejects_cancellation_after_payment_proof_is_linked(db_session, subscriber):
    account_id = subscriber.id
    intent, intent_id = _intent(
        db_session, account_id, metadata={"payment_proof_id": "proof-1"}
    )
    command = _command(account_id, intent_id)

    with pytest.raises(TopupIntentError) as exc:
        payment_intent_management.cancel_unsubmitted_direct_transfer(
            db_session, command
        )

    assert exc.value.code == "financial.topup_intents.proof_link_conflict"
    db_session.refresh(intent)
    assert intent.status == "pending"


def test_history_marks_only_unsubmitted_pending_transfer_cancelable(
    db_session, subscriber
):
    account_id = subscriber.id
    cancelable, _ = _intent(db_session, account_id)
    linked, _ = _intent(
        db_session, account_id, metadata={"payment_proof_id": "proof-2"}
    )

    views = payment_intent_management.list_for_account(db_session, account_id)
    by_id = {view.id: view for view in views}

    assert by_id[cancelable.id].can_cancel is True
    assert by_id[linked.id].can_cancel is False
