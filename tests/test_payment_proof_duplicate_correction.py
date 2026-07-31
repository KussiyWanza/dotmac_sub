from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.billing import PaymentReversal, PaymentReversalOrigin, PaymentStatus
from app.models.event_store import EventStore
from app.models.payment_proof import (
    PaymentProof,
    PaymentProofCorrection,
    PaymentProofStatus,
)
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services import payment_proofs
from app.services.billing._common import get_account_credit_balance
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _payment(db, subscriber, amount: str):
    return billing_service.payments.create(
        db,
        PaymentCreate(
            account_id=subscriber.id,
            amount=Decimal(amount),
            currency="NGN",
            status=PaymentStatus.succeeded,
            external_id=f"proof-test:{uuid4()}",
        ),
    )


def _proof(db, subscriber, payment, *, reference: str):
    proof = PaymentProof(
        account_id=subscriber.id,
        submitted_by=subscriber.id,
        amount=payment.amount,
        verified_amount=payment.amount,
        currency=payment.currency,
        reference=reference,
        file_path=f"uploads/payment_proofs/{uuid4().hex}.png",
        status=PaymentProofStatus.verified,
        verified_by=str(uuid4()),
        payment_id=payment.id,
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    return proof


def _context(key: str) -> CommandContext:
    return CommandContext.system(
        actor="user:test-finance-operator",
        scope=payment_proofs.REVIEW_SCOPE,
        reason="Correct duplicate payment-proof verification",
        idempotency_key=key,
    )


def _preview(db, duplicate, original):
    return payment_proofs.preview_duplicate_correction(
        db,
        duplicate_proof_id=duplicate.id,
        original_proof_id=original.id,
        reason=(
            "The later upload repeats cash already recorded by the original proof."
        ),
    )


def _command(preview):
    return payment_proofs.CorrectDuplicatePaymentProofCommand(
        duplicate_proof_id=preview.duplicate_proof_id,
        original_proof_id=preview.original_proof_id,
        actor_id=str(uuid4()),
        reason=preview.reason,
        preview_fingerprint=preview.fingerprint,
    )


def test_duplicate_correction_reverses_only_later_payment_and_links_evidence(
    db_session, subscriber
):
    original_payment = _payment(db_session, subscriber, "37625.00")
    duplicate_payment = _payment(db_session, subscriber, "37625.00")
    original = _proof(
        db_session,
        subscriber,
        original_payment,
        reference="TRF-ORIGINAL",
    )
    duplicate = _proof(
        db_session,
        subscriber,
        duplicate_payment,
        reference="TRF-REUPLOAD",
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "75250.00"
    )

    preview = _preview(db_session, duplicate, original)
    assert preview.account_credit_before == Decimal("75250.00")
    assert preview.account_credit_after == Decimal("37625.00")
    assert preview.invoice_effect_count == 0

    command = _command(preview)
    db_session_adapter.release_read_transaction(db_session)
    result = payment_proofs.correct_duplicate_payment_proof(
        db_session,
        context=_context("proof-correction-test-0001"),
        command=command,
    )

    db_session.refresh(original_payment)
    db_session.refresh(duplicate_payment)
    assert original_payment.status is PaymentStatus.succeeded
    assert duplicate_payment.status is PaymentStatus.reversed
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "37625.00"
    )
    correction = db_session.get(PaymentProofCorrection, result.correction_id)
    assert correction is not None
    assert correction.duplicate_proof_id == duplicate.id
    assert correction.original_proof_id == original.id
    assert correction.duplicate_payment_id == duplicate_payment.id
    assert correction.payment_reversal_id == result.payment_reversal_id
    assert correction.ledger_entry_id == result.ledger_entry_id
    reversal = db_session.query(PaymentReversal).one()
    assert reversal.origin is PaymentReversalOrigin.administrative_correction
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "correct_duplicate_verification")
        .one()
    )
    assert audit.entity_id == str(duplicate.id)
    assert audit.metadata_["original_proof_id"] == str(original.id)
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "payment_proof.corrected")
        .one()
    )
    assert event.payload["payment_reversal_id"] == str(result.payment_reversal_id)


def test_duplicate_correction_replays_once_by_idempotency_key(db_session, subscriber):
    original_payment = _payment(db_session, subscriber, "100.00")
    duplicate_payment = _payment(db_session, subscriber, "100.00")
    original = _proof(
        db_session,
        subscriber,
        original_payment,
        reference="ORIGINAL",
    )
    duplicate = _proof(
        db_session,
        subscriber,
        duplicate_payment,
        reference="DUPLICATE",
    )
    preview = _preview(db_session, duplicate, original)
    command = _command(preview)
    key = "proof-correction-test-replay"

    db_session_adapter.release_read_transaction(db_session)
    first = payment_proofs.correct_duplicate_payment_proof(
        db_session,
        context=_context(key),
        command=command,
    )
    db_session_adapter.release_read_transaction(db_session)
    replay = payment_proofs.correct_duplicate_payment_proof(
        db_session,
        context=_context(key),
        command=command,
    )

    assert replay.idempotent_replay is True
    assert replay.correction_id == first.correction_id
    assert db_session.query(PaymentProofCorrection).count() == 1
    assert db_session.query(PaymentReversal).count() == 1


def test_duplicate_correction_rejects_cross_account_original(db_session, subscriber):
    from app.models.subscriber import Subscriber

    other = Subscriber(
        first_name="Other",
        last_name="Account",
        email=f"other-{uuid4().hex}@example.com",
    )
    db_session.add(other)
    db_session.commit()
    duplicate = _proof(
        db_session,
        subscriber,
        _payment(db_session, subscriber, "100.00"),
        reference="DUPLICATE",
    )
    original = _proof(
        db_session,
        other,
        _payment(db_session, other, "100.00"),
        reference="ORIGINAL",
    )

    with pytest.raises(DomainError) as exc:
        _preview(db_session, duplicate, original)

    assert exc.value.code == "financial.payment_proofs.correction_account_mismatch"


def test_candidate_projection_does_not_infer_different_amount_as_original(
    db_session, subscriber
):
    matching = _proof(
        db_session,
        subscriber,
        _payment(db_session, subscriber, "100.00"),
        reference="MATCH",
    )
    duplicate = _proof(
        db_session,
        subscriber,
        _payment(db_session, subscriber, "100.00"),
        reference="DUPLICATE",
    )
    _proof(
        db_session,
        subscriber,
        _payment(db_session, subscriber, "200.00"),
        reference="DIFFERENT",
    )

    candidates = payment_proofs.duplicate_correction_candidates(
        db_session, duplicate.id
    )

    assert [candidate.proof_id for candidate in candidates] == [matching.id]
