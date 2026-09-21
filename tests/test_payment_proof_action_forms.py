from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.services import payment_proofs
from app.services import web_billing_payment_proofs as web_payment_proofs
from app.services.action_forms import (
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionFormSubmission,
)


def _proof(
    *,
    status: PaymentProofStatus = PaymentProofStatus.submitted,
    consolidated: bool = False,
) -> PaymentProof:
    return PaymentProof(
        id=uuid.uuid4(),
        account_id=None if consolidated else uuid.uuid4(),
        billing_account_id=uuid.uuid4() if consolidated else None,
        amount=Decimal("9000.00") if consolidated else Decimal("5000.00"),
        gross_amount=Decimal("10000.00") if consolidated else None,
        wht_amount=Decimal("1000.00") if consolidated else None,
        wht_rate=Decimal("10.00") if consolidated else None,
        currency="NGN",
        reference="TRF-ACTION",
        file_path="uploads/payment_proofs/action.png",
        status=status,
    )


def test_review_eligibility_is_owned_by_payment_proofs() -> None:
    proof = _proof()
    duplicate = _proof(status=PaymentProofStatus.verified)

    eligible = payment_proofs.review_eligibility(proof)
    duplicate_result = payment_proofs.review_eligibility(proof, [duplicate])
    terminal = payment_proofs.review_eligibility(
        _proof(status=PaymentProofStatus.rejected)
    )

    assert eligible.verify_allowed is True
    assert eligible.reject_allowed is True
    assert duplicate_result.verify_allowed is False
    assert str(duplicate.id) in str(duplicate_result.verify_unavailable_reason)
    assert duplicate_result.reject_allowed is True
    assert terminal.verify_allowed is False
    assert terminal.reject_allowed is False


def test_subscriber_review_actions_declare_fields_impact_and_confirmation() -> None:
    actions = web_payment_proofs._review_actions(
        _proof(), [], can_review=True, submission=None
    )

    verify, reject = actions
    assert verify.key == web_payment_proofs.VERIFY_ACTION_KEY
    assert verify.allowed is True
    assert verify.confirmation is not None
    assert "succeeded payment" in str(verify.impact)
    assert [field.key for field in verify.fields] == [
        "amount",
        "auto_allocate",
        "review_notes",
    ]
    assert verify.field("amount").kind is ActionFieldKind.decimal
    assert reject.key == web_payment_proofs.REJECT_ACTION_KEY
    assert reject.field("review_notes").required is True


def test_consolidated_review_projects_net_cash_and_wht_impact() -> None:
    actions = web_payment_proofs._review_actions(
        _proof(consolidated=True), [], can_review=True, submission=None
    )

    verify = actions[0]
    assert [field.key for field in verify.fields] == ["amount", "review_notes"]
    assert verify.field("amount").label == "Confirmed net cash (NGN)"
    assert "WHT receivable" in str(verify.impact)
    assert "WHT receivable" in str(verify.confirmation.message)


def test_duplicate_disables_verify_with_owner_reason_but_keeps_reject() -> None:
    proof = _proof()
    duplicate = _proof(status=PaymentProofStatus.verified)

    verify, reject = web_payment_proofs._review_actions(
        proof, [duplicate], can_review=True, submission=None
    )

    assert verify.allowed is False
    assert str(duplicate.id) in str(verify.disabled_reason)
    assert reject.allowed is True


def test_unauthorized_or_terminal_review_actions_are_omitted() -> None:
    assert (
        web_payment_proofs._review_actions(
            _proof(), [], can_review=False, submission=None
        )
        == ()
    )
    assert (
        web_payment_proofs._review_actions(
            _proof(status=PaymentProofStatus.verified),
            [],
            can_review=True,
            submission=None,
        )
        == ()
    )


def test_failed_submission_binds_typed_field_error_and_values() -> None:
    error = payment_proofs.PaymentProofReviewError(
        code="financial.payment_proofs.invalid_verified_amount",
        message="Invalid verified amount",
        field="amount",
    )
    submission = web_payment_proofs.review_error_submission(
        action_key=web_payment_proofs.VERIFY_ACTION_KEY,
        values={
            "amount": "not-a-number",
            "auto_allocate": "no",
            "review_notes": "bank mismatch",
        },
        error=error,
    )

    verify = web_payment_proofs._review_actions(
        _proof(), [], can_review=True, submission=submission
    )[0]

    assert verify.field("amount").value == "not-a-number"
    assert verify.field("amount").error == "Invalid verified amount"
    assert verify.field("auto_allocate").value == "no"
    assert verify.field("review_notes").value == "bank mismatch"


def test_unfielded_domain_error_becomes_general_error() -> None:
    submission = web_payment_proofs.review_error_submission(
        action_key=web_payment_proofs.VERIFY_ACTION_KEY,
        values={"amount": "5000.00", "auto_allocate": "yes"},
        error=payment_proofs.PaymentProofReviewError(
            code="financial.payment_proofs.duplicate_transfer_reference",
            message="Reference already verified",
        ),
    )

    verify = web_payment_proofs._review_actions(
        _proof(), [], can_review=True, submission=submission
    )[0]

    assert verify.general_error == "Reference already verified"


def test_consolidated_binding_discards_inapplicable_allocation_value() -> None:
    submission = ActionFormSubmission.from_mapping(
        web_payment_proofs.VERIFY_ACTION_KEY,
        {
            "amount": "8500.00",
            "auto_allocate": "no",
            "review_notes": "confirmed",
        },
    )

    verify = web_payment_proofs._review_actions(
        _proof(consolidated=True), [], can_review=True, submission=submission
    )[0]

    assert verify.field("amount").value == "8500.00"
    assert verify.field("review_notes").value == "confirmed"


# ── Equivalence: OLD `PaymentProofReviewEligibility` vs NEW
# `ActionReadiness`-derived `.gated_by(...)` result ─────────────────────────
#
# These prove the pilot changed the TRANSPORT SHAPE of the existing decision
# and NOT the decision itself: for every scenario the suite above already
# covers, the old `allowed` boolean and the new `.gated_by(...)`-derived
# `allowed` boolean must agree exactly. `disabled_reason` wording may
# legitimately differ (customer prose vs a more literal message) but the
# verdict boolean has zero tolerance for disagreement.


def _assert_allowed_equivalence(
    proof: PaymentProof,
    duplicates: list[PaymentProof],
    *,
    action_key: str,
) -> None:
    eligibility = payment_proofs.review_eligibility(proof, duplicates)
    old_allowed = (
        eligibility.verify_allowed
        if action_key == web_payment_proofs.VERIFY_ACTION_KEY
        else eligibility.reject_allowed
    )

    readiness = web_payment_proofs._review_readiness(
        proof, eligibility, action_key=action_key
    )

    form = ActionForm(
        key=action_key,
        title="t",
        description="d",
        action_url="/admin/x",
        submit_label="s",
        fields=(ActionField(key="f", label="F", kind=ActionFieldKind.text),),
    ).gated_by(readiness, customer_facing=False)

    assert form.allowed == old_allowed, (
        f"OLD eligibility.allowed={old_allowed} disagrees with NEW "
        f".gated_by(...) allowed={form.allowed} for action {action_key!r}"
    )
    # Zero tolerance on the boolean verdict; the reason text may differ.
    if not old_allowed:
        assert form.disabled_reason, "a blocked form must still explain why"


def test_equivalence_eligible_submitted_proof_verify_and_reject() -> None:
    proof = _proof()
    _assert_allowed_equivalence(
        proof, [], action_key=web_payment_proofs.VERIFY_ACTION_KEY
    )
    _assert_allowed_equivalence(
        proof, [], action_key=web_payment_proofs.REJECT_ACTION_KEY
    )


def test_equivalence_duplicate_blocks_verify_but_not_reject() -> None:
    proof = _proof()
    duplicate = _proof(status=PaymentProofStatus.verified)
    _assert_allowed_equivalence(
        proof, [duplicate], action_key=web_payment_proofs.VERIFY_ACTION_KEY
    )
    _assert_allowed_equivalence(
        proof, [duplicate], action_key=web_payment_proofs.REJECT_ACTION_KEY
    )


def test_equivalence_terminal_proof_blocks_both_actions() -> None:
    proof = _proof(status=PaymentProofStatus.rejected)
    _assert_allowed_equivalence(
        proof, [], action_key=web_payment_proofs.VERIFY_ACTION_KEY
    )
    _assert_allowed_equivalence(
        proof, [], action_key=web_payment_proofs.REJECT_ACTION_KEY
    )


def test_equivalence_consolidated_proof_eligible_verify_and_reject() -> None:
    proof = _proof(consolidated=True)
    _assert_allowed_equivalence(
        proof, [], action_key=web_payment_proofs.VERIFY_ACTION_KEY
    )
    _assert_allowed_equivalence(
        proof, [], action_key=web_payment_proofs.REJECT_ACTION_KEY
    )


def test_equivalence_review_actions_call_site_matches_gated_by_for_duplicate() -> None:
    """End-to-end: the real `_review_actions` call site itself agrees."""
    proof = _proof()
    duplicate = _proof(status=PaymentProofStatus.verified)
    eligibility = payment_proofs.review_eligibility(proof, [duplicate])

    verify, reject = web_payment_proofs._review_actions(
        proof, [duplicate], can_review=True, submission=None
    )

    assert verify.allowed == eligibility.verify_allowed
    assert reject.allowed == eligibility.reject_allowed


def test_equivalence_review_actions_call_site_matches_gated_by_when_eligible() -> None:
    proof = _proof()
    eligibility = payment_proofs.review_eligibility(proof, [])

    verify, reject = web_payment_proofs._review_actions(
        proof, [], can_review=True, submission=None
    )

    assert verify.allowed == eligibility.verify_allowed is True
    assert reject.allowed == eligibility.reject_allowed is True


def test_review_readiness_panel_present_for_submitted_proof_with_duplicate() -> None:
    proof = _proof()
    duplicate = _proof(status=PaymentProofStatus.verified)

    panel = web_payment_proofs.review_readiness_panel(
        proof, [duplicate], can_review=True
    )

    assert panel is not None
    assert panel.blockers, "the blocked verify readiness must carry a blocker row"


def test_review_readiness_panel_absent_for_unauthorized_or_terminal_proof() -> None:
    assert (
        web_payment_proofs.review_readiness_panel(_proof(), [], can_review=False)
        is None
    )
    assert (
        web_payment_proofs.review_readiness_panel(
            _proof(status=PaymentProofStatus.verified), [], can_review=True
        )
        is None
    )
