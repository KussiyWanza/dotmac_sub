from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.models.billing import TopupIntent
from app.models.payment_proof import PaymentProof, PaymentProofStatus
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


def test_history_offers_proof_rejection_for_expired_submitted_transfer(
    db_session, subscriber
):
    intent, _ = _intent(db_session, subscriber.id)
    proof = PaymentProof(
        account_id=subscriber.id,
        submitted_by=subscriber.id,
        amount=intent.requested_amount,
        currency=intent.currency,
        reference=intent.reference,
        file_path="uploads/payment_proofs/stale-transfer.png",
        status=PaymentProofStatus.submitted,
    )
    db_session.add(proof)
    db_session.flush()
    intent.status = "submitted"
    intent.expires_at = datetime.now(UTC) - timedelta(days=1)
    intent.metadata_ = {
        **dict(intent.metadata_ or {}),
        "payment_proof_id": str(proof.id),
    }
    db_session.commit()

    view = payment_intent_management.get_for_account(
        db_session,
        account_id=subscriber.id,
        intent_id=intent.id,
    )

    assert view is not None
    assert view.can_cancel is True
    assert view.cancellation_action is not None
    assert (
        view.cancellation_action.kind
        is payment_intent_management.PaymentIntentCancellationKind.stale_submitted_proof
    )
    assert view.cancellation_action.proof_id == proof.id
    assert view.cancellation_action.label == "Cancel stale intent"
    assert "no payment was received" in view.cancellation_action.confirmation_message


@pytest.mark.parametrize(
    ("proof_status", "expires_delta"),
    [
        (PaymentProofStatus.submitted, timedelta(days=1)),
        (PaymentProofStatus.verified, timedelta(days=-1)),
        (PaymentProofStatus.rejected, timedelta(days=-1)),
    ],
)
def test_history_hides_stale_cancel_when_proof_is_not_eligible(
    db_session,
    subscriber,
    proof_status,
    expires_delta,
):
    intent, _ = _intent(db_session, subscriber.id)
    proof = PaymentProof(
        account_id=subscriber.id,
        submitted_by=subscriber.id,
        amount=intent.requested_amount,
        currency=intent.currency,
        reference=intent.reference,
        file_path="uploads/payment_proofs/guarded-transfer.png",
        status=proof_status,
    )
    db_session.add(proof)
    db_session.flush()
    intent.status = "submitted"
    intent.expires_at = datetime.now(UTC) + expires_delta
    intent.metadata_ = {
        **dict(intent.metadata_ or {}),
        "payment_proof_id": str(proof.id),
    }
    db_session.commit()

    view = payment_intent_management.get_for_account(
        db_session,
        account_id=subscriber.id,
        intent_id=intent.id,
    )

    assert view is not None
    assert view.can_cancel is False
    assert view.cancellation_action is None


def test_admin_cancel_stale_intent_delegates_to_payment_proof_owner(
    monkeypatch,
    db_session,
    subscriber,
):
    from app.web.admin import customers as customer_routes

    intent, _ = _intent(db_session, subscriber.id)
    proof = PaymentProof(
        account_id=subscriber.id,
        submitted_by=subscriber.id,
        amount=intent.requested_amount,
        currency=intent.currency,
        reference=intent.reference,
        file_path="uploads/payment_proofs/stale-admin-transfer.png",
        status=PaymentProofStatus.submitted,
    )
    db_session.add(proof)
    db_session.flush()
    intent.status = "submitted"
    intent.expires_at = datetime.now(UTC) - timedelta(days=1)
    intent.metadata_ = {
        **dict(intent.metadata_ or {}),
        "payment_proof_id": str(proof.id),
    }
    db_session.commit()
    captured: dict[str, object] = {}

    def _reject_proof(
        db,
        proof_id,
        *,
        context,
        verified_by,
        review_notes,
    ):
        captured.update(
            proof_id=proof_id,
            context=context,
            verified_by=verified_by,
            review_notes=review_notes,
        )
        return object()

    monkeypatch.setattr(customer_routes.payment_proofs, "reject_proof", _reject_proof)
    response = customer_routes.cancel_customer_payment_intent(
        customer_id=subscriber.id,
        intent_id=intent.id,
        reason="No matching transfer on the bank statement",
        db=db_session,
        auth={
            "principal_id": "finance-admin",
            "principal_type": "system_user",
            "roles": ["admin"],
            "scopes": [],
        },
    )

    assert response.status_code == 303
    assert captured["proof_id"] == str(proof.id)
    assert captured["verified_by"] == "finance-admin"
    assert captured["review_notes"] == "No matching transfer on the bank statement"
    context = captured["context"]
    assert isinstance(context, CommandContext)
    assert context.scope == customer_routes.payment_proofs.REVIEW_SCOPE
    assert (
        context.idempotency_key
        == f"admin-cancel-stale-payment-intent:{intent.id}:{proof.id}"
    )


def test_history_uses_safe_authoritative_gateway_projection(db_session, subscriber):
    observed_at = datetime.now(UTC) - timedelta(minutes=2)
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=f"GW-{uuid4().hex[:12]}",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("2500.00"),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
        metadata_={
            "gateway_verification": {
                "schema_version": 1,
                "outcome": "processing",
                "provider_status": "ongoing",
                "reason_code": "provider_reported_processing",
                "observed_at": observed_at.isoformat(),
                "source": "gateway_reconciliation",
            },
            "private_gateway_payload": {"must": "not be projected"},
        },
    )
    db_session.add(intent)
    db_session.commit()

    view = payment_intent_management.list_for_account(db_session, subscriber.id)[0]

    assert view.status == "processing"
    assert view.status_label == "Processing"
    assert view.safe_reason_code == "provider_reported_processing"
    assert view.last_verification_at == observed_at
    assert view.blocks_another_attempt is True
    assert view.customer_retry_allowed is False
    assert not hasattr(view, "metadata")


def test_history_projects_legacy_pending_gateway_as_expired(db_session, subscriber):
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=f"GW-{uuid4().hex[:12]}",
        provider_type="paystack",
        currency="NGN",
        requested_amount=Decimal("2500.00"),
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(intent)
    db_session.commit()

    view = payment_intent_management.list_for_account(db_session, subscriber.id)[0]

    assert view.status == "expired"
    assert view.status_label == "Expired"
    assert view.stored_status == "pending"
    assert view.blocks_another_attempt is False
    assert view.customer_retry_allowed is True
