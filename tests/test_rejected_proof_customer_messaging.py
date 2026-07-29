"""A rejected receipt must never be reported to the customer as under review.

Rejecting a proof used to leave its top-up intent `submitted`, which the portal
rendered as "Your receipt was received successfully. You do not need to upload
it again" while the customer was in fact blocked from starting a new deposit
until the intent expired. The write path was fixed by #1642, but rows stranded
before the fix survive, and the rejection consequence is still skipped when the
proof link is missing — so the read path must tell the truth on its own.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.services.account_credit_deposits import (
    ActiveDepositNextAction,
    ActiveDepositPhase,
    _rejected_proof_reason,
)

TEMPLATE = Path("templates/customer/billing/topup.html")


class _Proof:
    def __init__(self, status, review_notes=None, reference=None):
        self.status = status
        self.review_notes = review_notes
        self.reference = reference


class _Intent:
    def __init__(self, metadata=None, reference=None):
        self.metadata_ = metadata or {}
        self.reference = reference
        self.account_id = uuid.uuid4()


class _Session:
    """Minimal stand-in: `get` resolves the explicit link, `scalars` the fallback."""

    def __init__(self, *, by_id=None, by_reference=None):
        self._by_id = by_id
        self._by_reference = by_reference
        self.scalars_called = False

    def get(self, _model, _pk):
        return self._by_id

    def scalars(self, _stmt):
        self.scalars_called = True

        class _Result:
            def __init__(self, value):
                self._value = value

            def first(self):
                return self._value

        return _Result(self._by_reference)


def _status(name):
    from app.models.payment_proof import PaymentProofStatus

    return getattr(PaymentProofStatus, name)


def test_rejected_proof_reason_uses_the_review_note() -> None:
    proof = _Proof(_status("rejected"), review_notes="Amount does not match transfer")
    intent = _Intent(metadata={"payment_proof_id": str(uuid.uuid4())})

    reason = _rejected_proof_reason(_Session(by_id=proof), intent)

    assert reason == "Amount does not match transfer"


def test_rejected_proof_without_a_note_still_reports_a_reason() -> None:
    """A blank note must not collapse back to the false 'under review' branch."""
    proof = _Proof(_status("rejected"), review_notes="   ")
    intent = _Intent(metadata={"payment_proof_id": str(uuid.uuid4())})

    reason = _rejected_proof_reason(_Session(by_id=proof), intent)

    assert reason == "Your bank transfer could not be confirmed."


def test_submitted_proof_is_not_treated_as_rejected() -> None:
    proof = _Proof(_status("submitted"))
    intent = _Intent(metadata={"payment_proof_id": str(uuid.uuid4())})

    assert _rejected_proof_reason(_Session(by_id=proof), intent) is None


def test_verified_proof_is_not_treated_as_rejected() -> None:
    proof = _Proof(_status("verified"))
    intent = _Intent(metadata={"payment_proof_id": str(uuid.uuid4())})

    assert _rejected_proof_reason(_Session(by_id=proof), intent) is None


def test_missing_link_falls_back_to_the_intent_reference() -> None:
    """Older rows have no payment_proof_id; they are exactly the stranded cohort."""
    proof = _Proof(_status("rejected"), review_notes="Receipt unreadable")
    session = _Session(by_id=None, by_reference=proof)
    intent = _Intent(reference="TRF-000000000001")

    reason = _rejected_proof_reason(session, intent)

    assert reason == "Receipt unreadable"
    assert session.scalars_called


def test_unparseable_proof_link_does_not_raise() -> None:
    session = _Session(by_id=None, by_reference=None)
    intent = _Intent(metadata={"payment_proof_id": "not-a-uuid"}, reference=None)

    assert _rejected_proof_reason(session, intent) is None


def test_no_proof_at_all_leaves_the_normal_phase() -> None:
    intent = _Intent(reference="TRF-000000000002")

    assert _rejected_proof_reason(_Session(), intent) is None


def test_rejected_phase_and_action_exist_in_the_closed_vocabularies() -> None:
    assert ActiveDepositPhase.receipt_rejected.value == "receipt_rejected"
    assert ActiveDepositNextAction.contact_support.value == "contact_support"


def test_template_renders_the_rejection_before_the_under_review_branch() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    rejected_at = template.index('active_deposit_request.phase == "receipt_rejected"')
    review_at = template.index('active_deposit_request.phase == "under_review"')

    # Order matters: the rejected branch must win, or the customer sees the
    # reassuring-but-false message again.
    assert rejected_at < review_at
    assert "active_deposit_request.rejection_reason" in template


def test_template_tells_a_rejected_customer_what_to_do_next() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    rejected_block = template.split(
        'active_deposit_request.phase == "receipt_rejected"', 1
    )[1].split("{% elif", 1)[0]

    assert "was not accepted" in rejected_block
    assert "contact support" in rejected_block.lower()
    # The reassuring line belongs only to the genuine under-review case.
    assert "received successfully" not in rejected_block
