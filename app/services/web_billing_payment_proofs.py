"""Service helpers for the bank-transfer payment-proof admin web pages."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.models.system_user import SystemUser
from app.services import payment_proofs as payment_proofs_service
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionFormSubmission,
    ActionOption,
    ActionTone,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VERIFY_ACTION_KEY = "payment_proof.verify"
REJECT_ACTION_KEY = "payment_proof.reject"
CORRECT_DUPLICATE_ACTION_KEY = "payment_proof.correct_duplicate"


@dataclass(frozen=True)
class ReviewerIdentityProjection:
    actor_id: str | None
    display_name: str | None
    resolved_system_user_id: UUID | None
    identity_resolved: bool


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _system_user_display_name(user: SystemUser, *, actor_id: str | None) -> str:
    display_name = _clean_text(user.display_name)
    if display_name:
        return display_name
    first_name = _clean_text(user.first_name)
    last_name = _clean_text(user.last_name)
    combined_name = " ".join(part for part in (first_name, last_name) if part)
    if combined_name:
        return combined_name
    email = _clean_text(user.email)
    if email:
        return email
    return actor_id or "User"


def _reviewer_identity(
    db: Session,
    *,
    actor_id: str | None,
) -> ReviewerIdentityProjection:
    cleaned_actor_id = _clean_text(actor_id)
    if cleaned_actor_id is None:
        return ReviewerIdentityProjection(
            actor_id=None,
            display_name=None,
            resolved_system_user_id=None,
            identity_resolved=False,
        )

    try:
        actor_uuid = UUID(cleaned_actor_id)
    except ValueError:
        return ReviewerIdentityProjection(
            actor_id=cleaned_actor_id,
            display_name=cleaned_actor_id,
            resolved_system_user_id=None,
            identity_resolved=False,
        )

    system_user = db.get(SystemUser, actor_uuid)
    if system_user is None:
        return ReviewerIdentityProjection(
            actor_id=cleaned_actor_id,
            display_name=cleaned_actor_id,
            resolved_system_user_id=None,
            identity_resolved=False,
        )

    return ReviewerIdentityProjection(
        actor_id=cleaned_actor_id,
        display_name=_system_user_display_name(
            system_user,
            actor_id=cleaned_actor_id,
        ),
        resolved_system_user_id=system_user.id,
        identity_resolved=True,
    )


def _review_actions(
    proof: PaymentProof,
    duplicates: list[PaymentProof],
    *,
    can_review: bool,
    submission: ActionFormSubmission | None,
) -> tuple[ActionForm, ...]:
    if not can_review or proof.status != PaymentProofStatus.submitted:
        return ()

    eligibility = payment_proofs_service.review_eligibility(proof, duplicates)
    consolidated = proof.billing_account_id is not None
    amount_label = (
        f"Confirmed net cash ({proof.currency})"
        if consolidated
        else f"Confirmed amount ({proof.currency})"
    )
    amount_help = (
        "Confirm the net cash on the bank statement. The submitted gross less "
        "this amount becomes the WHT receivable."
        if consolidated
        else "Confirm the cash received on the bank statement. The payment is "
        "created for this amount."
    )
    verify_fields = [
        ActionField(
            key="amount",
            label=amount_label,
            kind=ActionFieldKind.decimal,
            value=f"{proof.amount:.2f}",
            required=True,
            min_value="0.01",
            step="0.01",
            help_text=amount_help,
        )
    ]
    if not consolidated:
        verify_fields.append(
            ActionField(
                key="auto_allocate",
                label="Allocation",
                kind=ActionFieldKind.select,
                value="yes",
                options=(
                    ActionOption(value="yes", label="Apply to oldest open invoices"),
                    ActionOption(value="no", label="Keep as account credit"),
                ),
                help_text=(
                    "Any amount not allocated to an invoice remains account credit."
                ),
            )
        )
    verify_fields.append(
        ActionField(
            key="review_notes",
            label="Review notes",
            kind=ActionFieldKind.textarea,
            max_length=2000,
            rows=2,
            help_text="Optional internal evidence about the bank-statement review.",
        )
    )

    verify_impact = (
        "The billing account will be credited with the gross settlement. Any "
        "difference between gross and confirmed net cash will become a tracked "
        "WHT receivable."
        if consolidated
        else "A succeeded payment will be created for the confirmed amount. "
        "Invoice allocation follows the selection below."
    )
    verify_confirmation = (
        "Record the confirmed net cash, credit the gross settlement, and create "
        "the resulting WHT receivable?"
        if consolidated
        else "Record a succeeded payment for the confirmed amount and apply the "
        "selected allocation policy?"
    )
    actions = (
        ActionForm(
            key=VERIFY_ACTION_KEY,
            title="Verify transfer",
            description="Confirm the receipt against the bank statement.",
            action_url=f"/admin/billing/payment-proofs/{proof.id}/verify",
            submit_label="Verify and record payment",
            fields=tuple(verify_fields),
            tone=ActionTone.positive,
            impact=verify_impact,
            confirmation=ActionConfirmation(
                title="Confirm financial posting",
                message=verify_confirmation,
            ),
            allowed=eligibility.verify_allowed,
            disabled_reason=eligibility.verify_unavailable_reason,
        ),
        ActionForm(
            key=REJECT_ACTION_KEY,
            title="Reject proof",
            description="Record why the transfer evidence could not be accepted.",
            action_url=f"/admin/billing/payment-proofs/{proof.id}/reject",
            submit_label="Reject proof",
            fields=(
                ActionField(
                    key="review_notes",
                    label="Reason",
                    kind=ActionFieldKind.textarea,
                    required=True,
                    max_length=2000,
                    rows=3,
                    placeholder="For example: amount does not match the bank statement",
                    help_text="This reason is sent to the customer or reseller.",
                ),
            ),
            tone=ActionTone.negative,
            impact="No payment will be created. The submitter will receive the reason.",
            confirmation=ActionConfirmation(
                title="Confirm rejection",
                message="Reject this transfer proof and notify the submitter?",
            ),
            allowed=eligibility.reject_allowed,
            disabled_reason=eligibility.reject_unavailable_reason,
        ),
    )
    if submission is None:
        return actions
    action_keys = {action.key for action in actions}
    if submission.action_key not in action_keys:
        raise ValueError(
            f"Unsupported payment-proof action submission: {submission.action_key}"
        )
    bound_actions: list[ActionForm] = []
    for action in actions:
        if action.key != submission.action_key:
            bound_actions.append(action)
            continue
        field_keys = {field.key for field in action.fields}
        bound_actions.append(action.bind(submission.restrict(field_keys)))
    return tuple(bound_actions)


def _duplicate_correction_actions(
    db: Session,
    proof: PaymentProof,
    *,
    can_correct: bool,
) -> tuple[ActionForm, ...]:
    if not can_correct or proof.status is not PaymentProofStatus.verified:
        return ()
    candidates = payment_proofs_service.duplicate_correction_candidates(db, proof.id)
    if not candidates:
        return ()
    options = tuple(
        ActionOption(
            value=str(candidate.proof_id),
            label=(
                f"{candidate.currency} {candidate.amount:,.2f} · "
                f"{candidate.reference or 'no reference'} · "
                f"{candidate.created_at:%Y-%m-%d %H:%M}"
            ),
        )
        for candidate in candidates
    )
    return (
        ActionForm(
            key=CORRECT_DUPLICATE_ACTION_KEY,
            title="Correct duplicate verification",
            description=(
                "Use only when this verified proof represents payment already "
                "recorded by an earlier verified proof."
            ),
            action_url=(
                f"/admin/billing/payment-proofs/{proof.id}/duplicate-correction/preview"
            ),
            submit_label="Review duplicate correction",
            fields=(
                ActionField(
                    key="original_proof_id",
                    label="Original verified proof to retain",
                    kind=ActionFieldKind.select,
                    required=True,
                    options=options,
                    help_text=(
                        "Compare both receipts and bank evidence before selecting "
                        "the original settlement."
                    ),
                ),
                ActionField(
                    key="reason",
                    label="Correction reason",
                    kind=ActionFieldKind.textarea,
                    required=True,
                    max_length=500,
                    rows=3,
                    placeholder=(
                        "Explain how the second verification duplicated the "
                        "earlier payment."
                    ),
                ),
            ),
            tone=ActionTone.negative,
            impact=(
                "The next screen previews the exact balance, invoice, payment, "
                "and audit effects. Nothing changes until confirmation."
            ),
        ),
    )


def review_error_submission(
    *,
    action_key: str,
    values: dict[str, object | None],
    error: DomainError,
) -> ActionFormSubmission:
    """Preserve one failed command submission using the owner's typed error."""

    detail = error.message
    field = getattr(error, "field", None)
    return ActionFormSubmission.from_mapping(
        action_key,
        values,
        field_errors={str(field): detail} if field else None,
        general_error=None if field else detail,
    )


def list_data(
    db: Session,
    *,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Build template context for the payment proofs list page (newest first)."""
    query = db.query(PaymentProof)
    if status:
        try:
            query = query.filter(PaymentProof.status == PaymentProofStatus(status))
        except ValueError:
            status = None
    total = query.count()
    proofs = (
        query.order_by(PaymentProof.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    submitted_count = (
        db.query(PaymentProof)
        .filter(PaymentProof.status == PaymentProofStatus.submitted)
        .count()
    )
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "proofs": proofs,
        "statuses": [s.value for s in PaymentProofStatus],
        "status_filter": status,
        "submitted_count": submitted_count,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def detail_data(
    db: Session,
    *,
    proof_id: str,
    can_review: bool = False,
    can_correct: bool = False,
    submission: ActionFormSubmission | None = None,
) -> dict[str, object] | None:
    """Build template context for the payment proof detail page."""
    proof = payment_proofs_service.get_proof(db, proof_id)
    if not proof:
        return None

    duplicates = payment_proofs_service.find_duplicate_proofs(db, proof)
    suffix = Path(proof.file_path or "").suffix.lower()
    try:
        payment_proofs_service.resolve_proof_file(proof)
        file_available = True
    except payment_proofs_service.PaymentProofError:
        file_available = False

    review_actions = _review_actions(
        proof,
        duplicates,
        can_review=can_review,
        submission=submission,
    )
    reviewer = _reviewer_identity(db, actor_id=proof.verified_by)
    correction = payment_proofs_service.get_duplicate_correction(db, proof.id)

    return {
        "proof": proof,
        "account": proof.account,
        "payment": proof.payment,
        "reviewer": reviewer,
        "duplicates": duplicates,
        "file_available": file_available,
        "file_is_image": suffix in _IMAGE_SUFFIXES,
        "file_is_pdf": suffix == ".pdf",
        "review_actions": review_actions,
        "correction_actions": _duplicate_correction_actions(
            db,
            proof,
            can_correct=can_correct,
        ),
        "correction": correction,
        "review_outcome": proof.status != PaymentProofStatus.submitted,
    }


def file_response_args(db: Session, *, proof_id: str) -> tuple[Path, str] | None:
    """Resolve (path, media_type) for streaming a proof's receipt, or None."""
    proof = payment_proofs_service.get_proof(db, proof_id)
    if not proof:
        return None
    return payment_proofs_service.resolve_proof_file(proof)


def verify_proof(
    db: Session,
    *,
    context: CommandContext,
    proof_id: str,
    verified_by: str,
    amount: str | None,
    auto_allocate: bool,
    review_notes: str | None,
) -> dict[str, object | None]:
    return payment_proofs_service.verify_proof(
        db,
        proof_id,
        context=context,
        verified_by=verified_by,
        amount=(amount or "").strip() or None,
        auto_allocate=auto_allocate,
        review_notes=review_notes,
    ).to_dict()


def reject_proof(
    db: Session,
    *,
    context: CommandContext,
    proof_id: str,
    verified_by: str,
    review_notes: str,
) -> dict[str, object | None]:
    return payment_proofs_service.reject_proof(
        db,
        proof_id,
        context=context,
        verified_by=verified_by,
        review_notes=review_notes,
    ).to_dict()


def preview_duplicate_correction(
    db: Session,
    *,
    proof_id: UUID,
    original_proof_id: UUID,
    reason: str,
) -> payment_proofs_service.DuplicateProofCorrectionPreview:
    return payment_proofs_service.preview_duplicate_correction(
        db,
        duplicate_proof_id=proof_id,
        original_proof_id=original_proof_id,
        reason=reason,
    )


def correct_duplicate_payment_proof(
    db: Session,
    *,
    context: CommandContext,
    proof_id: UUID,
    original_proof_id: UUID,
    actor_id: str,
    reason: str,
    preview_fingerprint: str,
) -> payment_proofs_service.DuplicateProofCorrectionResult:
    return payment_proofs_service.correct_duplicate_payment_proof(
        db,
        context=context,
        command=payment_proofs_service.CorrectDuplicatePaymentProofCommand(
            duplicate_proof_id=proof_id,
            original_proof_id=original_proof_id,
            actor_id=actor_id,
            reason=reason,
            preview_fingerprint=preview_fingerprint,
        ),
    )
