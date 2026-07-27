"""Terminal payment-proof to submitted top-up intent reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.billing import Payment, PaymentStatus, TopupIntent
from app.models.event_store import EventStore
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services import topup_intent_proof_reconciliation as reconciliation
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.topup_intents import DIRECT_TRANSFER_PROVIDER


def _seed_candidate(
    db_session,
    subscriber,
    *,
    proof_status: PaymentProofStatus,
    payment_status: PaymentStatus | None = None,
) -> tuple[TopupIntent, PaymentProof, Payment | None]:
    reference = f"TRF-RECON-{uuid4().hex[:12].upper()}"
    payment = None
    if payment_status is not None:
        payment = Payment(
            account_id=subscriber.id,
            amount=Decimal("5000.00"),
            currency="NGN",
            status=payment_status,
            paid_at=datetime.now(UTC),
            external_id=reference,
        )
        db_session.add(payment)
        db_session.flush()
    proof = PaymentProof(
        account_id=subscriber.id,
        submitted_by=subscriber.id,
        amount=Decimal("5000.00"),
        verified_amount=(
            Decimal("5000.00") if proof_status is PaymentProofStatus.verified else None
        ),
        currency="NGN",
        reference=reference,
        paid_at=datetime.now(UTC),
        file_path="uploads/payment_proofs/reconcile.png",
        status=proof_status,
        payment_id=payment.id if payment else None,
    )
    db_session.add(proof)
    db_session.flush()
    intent = TopupIntent(
        account_id=subscriber.id,
        reference=reference,
        provider_type=DIRECT_TRANSFER_PROVIDER,
        currency="NGN",
        requested_amount=Decimal("5000.00"),
        status="submitted",
        metadata_={
            "payment_flow": "account_topup",
            "payment_proof_id": str(proof.id),
        },
    )
    db_session.add(intent)
    db_session.commit()
    return intent, proof, payment


def _context(intent: TopupIntent, proof: PaymentProof) -> CommandContext:
    return CommandContext.system(
        actor="pytest:proof-intent-reconciliation",
        scope=reconciliation.RECONCILIATION_SCOPE,
        reason="Proof-intent reconciliation behavior test",
        idempotency_key=f"{intent.id}:{proof.id}:{proof.status.value}",
    )


def _reconcile(
    db_session,
    intent: TopupIntent,
    proof: PaymentProof,
) -> reconciliation.TopupIntentProofReconciliationResult:
    command = reconciliation.ReconcileTopupIntentProofCommand(
        intent_id=intent.id,
        proof_id=proof.id,
        proof_status=proof.status,
        payment_id=proof.payment_id,
    )
    context = _context(intent, proof)
    db_session_adapter.release_read_transaction(db_session)
    return reconciliation.reconcile_terminal_proof(
        db_session,
        command,
        context=context,
    )


def test_verified_succeeded_proof_is_discovered_and_completed(db_session, subscriber):
    intent, proof, payment = _seed_candidate(
        db_session,
        subscriber,
        proof_status=PaymentProofStatus.verified,
        payment_status=PaymentStatus.succeeded,
    )

    candidates = reconciliation.inspect_terminal_proof_drift(db_session, limit=10)
    assert len(candidates) == 1
    assert candidates[0].action is reconciliation.TopupIntentProofRepairAction.complete

    outcome = _reconcile(db_session, intent, proof)

    assert outcome.changed is True
    assert outcome.status.value == "completed"
    assert outcome.payment_id == payment.id
    db_session.refresh(intent)
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id
    assert intent.metadata_["payment_proof_resolution"]["source"] == (
        "payment_proof_reconciliation"
    )


def test_rejected_proof_is_discovered_and_canceled(db_session, subscriber):
    intent, proof, _payment = _seed_candidate(
        db_session,
        subscriber,
        proof_status=PaymentProofStatus.rejected,
    )

    candidates = reconciliation.inspect_terminal_proof_drift(db_session, limit=10)
    assert len(candidates) == 1
    assert candidates[0].action is reconciliation.TopupIntentProofRepairAction.cancel

    outcome = _reconcile(db_session, intent, proof)

    assert outcome.changed is True
    assert outcome.status.value == "canceled"
    db_session.refresh(intent)
    assert intent.status == "canceled"
    assert intent.metadata_["canceled_reason"] == "payment_proof_rejected"
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "topup_intent.direct_transfer_proof_rejected")
        .count()
        == 1
    )


def test_verified_reversed_payment_is_quarantined_without_mutation(
    db_session, subscriber
):
    intent, _proof, _payment = _seed_candidate(
        db_session,
        subscriber,
        proof_status=PaymentProofStatus.verified,
        payment_status=PaymentStatus.reversed,
    )

    candidates = reconciliation.inspect_terminal_proof_drift(db_session, limit=10)

    assert len(candidates) == 1
    assert (
        candidates[0].action
        is reconciliation.TopupIntentProofRepairAction.requires_review
    )
    assert candidates[0].review_reason == (
        "verified_proof_payment_not_currently_succeeded"
    )
    db_session.refresh(intent)
    assert intent.status == "submitted"
    assert intent.completed_payment_id is None


def test_preview_requires_exact_metadata_proof_link(db_session, subscriber):
    intent, _proof, _payment = _seed_candidate(
        db_session,
        subscriber,
        proof_status=PaymentProofStatus.rejected,
    )
    intent.metadata_ = {
        **dict(intent.metadata_ or {}),
        "payment_proof_id": str(uuid4()),
    }
    db_session.commit()

    assert reconciliation.inspect_terminal_proof_drift(db_session, limit=10) == ()
