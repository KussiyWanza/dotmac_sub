from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import TopupIntent
from app.services import payment_intent_management
from app.services.owner_commands import CommandContext
from app.services.topup_intents import (
    DirectTransferCancellationSource,
    TopupIntentError,
)


def _intent(db_session, subscriber, *, metadata=None) -> TopupIntent:
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=f"TRF-{uuid4().hex[:12]}",
        provider_type="direct_bank_transfer",
        currency="NGN",
        requested_amount=Decimal("1499.99"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        metadata_=metadata or {"payment_flow": "account_topup"},
    )
    db_session.add(intent)
    db_session.commit()
    return intent


def _command(subscriber, intent, *, reason="Failed pending intent"):
    return payment_intent_management.CancelPaymentIntentCommand(
        context=CommandContext.system(
            actor="user:samuel-ojo",
            scope=payment_intent_management.ADMIN_CANCEL_SCOPE,
            reason=reason,
            idempotency_key=f"cancel:{intent.id}",
        ),
        account_id=subscriber.id,
        intent_id=intent.id,
        source=DirectTransferCancellationSource.admin_customer_billing,
    )


def test_cancels_pending_unsubmitted_direct_transfer(db_session, subscriber):
    intent = _intent(db_session, subscriber)

    outcome = payment_intent_management.cancel_unsubmitted_direct_transfer(
        db_session, _command(subscriber, intent)
    )

    db_session.refresh(intent)
    assert outcome.changed is True
    assert intent.status == "canceled"
    assert intent.metadata_["cancellation"]["reason"] == "Failed pending intent"
    assert intent.metadata_["cancellation"]["source"] == "admin_customer_billing"


def test_rejects_cancellation_after_payment_proof_is_linked(db_session, subscriber):
    intent = _intent(db_session, subscriber, metadata={"payment_proof_id": "proof-1"})

    with pytest.raises(TopupIntentError) as exc:
        payment_intent_management.cancel_unsubmitted_direct_transfer(
            db_session, _command(subscriber, intent)
        )

    assert exc.value.code == "financial.topup_intents.proof_link_conflict"
    db_session.refresh(intent)
    assert intent.status == "pending"


def test_history_marks_only_unsubmitted_pending_transfer_cancelable(
    db_session, subscriber
):
    cancelable = _intent(db_session, subscriber)
    linked = _intent(db_session, subscriber, metadata={"payment_proof_id": "proof-2"})

    views = payment_intent_management.list_for_account(db_session, subscriber.id)
    by_id = {view.id: view for view in views}

    assert by_id[cancelable.id].can_cancel is True
    assert by_id[linked.id].can_cancel is False
