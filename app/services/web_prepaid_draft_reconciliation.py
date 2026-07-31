"""Admin projection for reviewed prepaid draft reconciliation.

The financial owner remains authoritative for classification and mutation. This
module only binds its exact preview to a short-lived staff confirmation and
projects the resulting action form for the invoice page.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID, uuid4

from jose import JWTError
from sqlalchemy.orm import Session

from app.services import context_signing
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionFormSubmission,
    ActionHiddenValue,
    ActionTone,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.form_contracts import (
    FormConsequence,
    FormContract,
    FormPrerequisite,
)
from app.services.form_contracts import (
    register as register_form_contract,
)
from app.services.owner_commands import CommandContext
from app.services.prepaid_draft_reconciliation import (
    PrepaidDraftAction,
    PrepaidDraftDisposition,
    PrepaidDraftReconciliationPreview,
    PrepaidDraftReconciliationResult,
    ReconcilePrepaidDraftCommand,
    preview_prepaid_draft_reconciliation,
    reconcile_prepaid_draft_invoice,
)

ACTION_KEY = "admin.prepaid_draft_reconciliation"
ACTION_PERMISSION = "billing:invoice:update"

_TOKEN_TYPE = "prepaid_draft_reconciliation_confirmation"
_TOKEN_ISSUER = "dotmac_sub.admin.prepaid_draft_reconciliation"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)


PREPAID_DRAFT_RECONCILIATION_FORM = register_form_contract(
    FormContract(
        key=ACTION_KEY,
        title="Reconcile prepaid draft",
        entity="invoice",
        command_owner="financial.prepaid_draft_reconciliation",
        consequences=(
            FormConsequence(
                key="invoice_state",
                label=(
                    "The invoice owner will either settle the exact funded draft or "
                    "void an exact duplicate, matching the reviewed decision"
                ),
            ),
            FormConsequence(
                key="funding_provenance",
                label=(
                    "Confirmed payments are applied first; only the exact remainder "
                    "may be consumed from a reviewed opening-funding baseline"
                ),
            ),
            FormConsequence(
                key="service_projection",
                label=(
                    "Entitlement, billing anchor, eligible access restoration, audit, "
                    "and event evidence change atomically with the invoice"
                ),
            ),
        ),
    )
)


class PrepaidDraftAdminError(DomainError):
    """Safe rejection produced by the admin confirmation adapter."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidDraftAdminError(
        code=f"admin.prepaid_draft_reconciliation.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class PrepaidDraftAdminReview:
    preview: PrepaidDraftReconciliationPreview
    effective_at: datetime
    confirmation_expires_at: datetime | None
    action_form: ActionForm
    form_contract_state: dict[str, object]


def _action_copy(
    preview: PrepaidDraftReconciliationPreview,
) -> tuple[str, str, str]:
    currency = preview.currency
    if preview.recommended_action is PrepaidDraftAction.settle_paid:
        payment_applied = preview.balance_due - preview.opening_funding_required
        impact = (
            f"{currency} {preview.balance_due:,.2f} will be settled using "
            f"{currency} {payment_applied:,.2f} of confirmed payment funding and "
            f"{currency} {preview.opening_funding_required:,.2f} of reviewed opening "
            "funding."
        )
        return (
            "Settle funded prepaid draft",
            "Settle invoice",
            impact,
        )
    if preview.recommended_action is PrepaidDraftAction.void_duplicate:
        return (
            "Close duplicate prepaid draft",
            "Void duplicate draft",
            (
                "The draft will be voided with no payment or opening funding consumed "
                "because the owner found exact existing renewal and entitlement evidence."
            ),
        )
    return (
        "Prepaid draft needs review",
        "Reconciliation unavailable",
        preview.reason,
    )


def _form_contract_state(
    preview: PrepaidDraftReconciliationPreview,
) -> dict[str, object]:
    eligible_dispositions = {
        PrepaidDraftDisposition.exact_payment_fundable,
        PrepaidDraftDisposition.reviewed_opening_fundable,
        PrepaidDraftDisposition.already_renewed,
    }
    prerequisites = [
        FormPrerequisite(
            key="authoritative_classification",
            label="The authoritative prepaid-draft owner classified this invoice",
            met=preview.disposition in eligible_dispositions,
            reason=None if preview.actionable else preview.reason,
        ),
        FormPrerequisite(
            key="exact_action",
            label="The preview identifies one exact, atomic reconciliation action",
            met=preview.actionable,
            reason=None if preview.actionable else preview.reason,
        ),
    ]
    return PREPAID_DRAFT_RECONCILIATION_FORM.state(prerequisites)


def _confirmation_claims(
    *,
    actor: str,
    preview: PrepaidDraftReconciliationPreview,
    effective_at: datetime,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "ver": _TOKEN_VERSION,
        "jti": uuid4().hex,
        "actor": actor,
        "invoice_id": str(preview.invoice_id),
        "preview_fingerprint": preview.fingerprint,
        "effective_at": int(effective_at.timestamp()),
        "iat": int(effective_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }


def preview_for_invoice_detail(
    db: Session, *, invoice_id: UUID
) -> PrepaidDraftReconciliationPreview | None:
    """Return only an actionable owner hint for the invoice action menu."""

    try:
        preview = preview_prepaid_draft_reconciliation(db, invoice_id)
    except DomainError:
        return None
    return preview if preview.actionable else None


def build_admin_review(
    db: Session,
    *,
    invoice_id: UUID,
    actor: str,
    now: datetime | None = None,
) -> PrepaidDraftAdminReview:
    """Project one exact owner preview into a signed staff confirmation."""

    normalized_actor = actor.strip()
    if not normalized_actor:
        _error("actor_required", "An authorized staff actor is required.")
    preview = preview_prepaid_draft_reconciliation(db, invoice_id)
    effective_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    expires_at = effective_at + _TOKEN_TTL
    title, submit_label, impact = _action_copy(preview)
    hidden_values: tuple[ActionHiddenValue, ...] = ()
    confirmation: ActionConfirmation | None = None
    if preview.actionable:
        token = context_signing.sign_context_token(
            db,
            _confirmation_claims(
                actor=normalized_actor,
                preview=preview,
                effective_at=effective_at,
                expires_at=expires_at,
            ),
        )
        hidden_values = (
            ActionHiddenValue(key="preview_fingerprint", value=preview.fingerprint),
            ActionHiddenValue(key="confirmation_token", value=token),
        )
        confirmation = ActionConfirmation(
            title="Confirm this exact reconciliation",
            message=(
                "I reviewed the funding breakdown and understand that the owner will "
                "recheck it under lock before making one atomic change."
            ),
        )
    action_form = ActionForm(
        key=ACTION_KEY,
        title=title,
        description=(
            "Record why this reviewed reconciliation is being confirmed. The reason "
            "is retained with the command and audit evidence."
        ),
        action_url=(
            f"/admin/billing/invoices/{preview.invoice_id}/"
            "prepaid-draft-reconciliation/confirm"
        ),
        submit_label=submit_label,
        fields=(
            ActionField(
                key="reason",
                label="Operator reason",
                kind=ActionFieldKind.textarea,
                required=True,
                max_length=500,
                rows=3,
                placeholder="Explain the reviewed funding evidence and intended repair.",
                help_text="Required financial review evidence; do not include secrets.",
            ),
        ),
        hidden_values=hidden_values,
        tone=(
            ActionTone.positive
            if preview.recommended_action is PrepaidDraftAction.settle_paid
            else ActionTone.neutral
        ),
        impact=impact,
        confirmation=confirmation,
        allowed=preview.actionable,
        disabled_reason=None if preview.actionable else preview.reason,
    )
    return PrepaidDraftAdminReview(
        preview=preview,
        effective_at=effective_at,
        confirmation_expires_at=expires_at if preview.actionable else None,
        action_form=action_form,
        form_contract_state=_form_contract_state(preview),
    )


def _decode_confirmation(db: Session, token: str) -> dict[Any, Any]:
    normalized = token.strip()
    if not normalized or len(normalized) > 131_072:
        _error(
            "invalid_confirmation",
            "The reconciliation confirmation is invalid; preview again.",
        )
    try:
        claims = context_signing.verify_context_token(db, normalized)
    except JWTError as exc:
        raise PrepaidDraftAdminError(
            code="admin.prepaid_draft_reconciliation.expired_confirmation",
            message="The reconciliation confirmation expired or is invalid; preview again.",
        ) from exc
    if (
        claims.get("typ") != _TOKEN_TYPE
        or claims.get("iss") != _TOKEN_ISSUER
        or claims.get("ver") != _TOKEN_VERSION
    ):
        _error(
            "invalid_confirmation",
            "The reconciliation confirmation is invalid; preview again.",
        )
    return claims


def _validated_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        _error("reason_required", "An operator reason is required.", field="reason")
    if len(normalized) > 500:
        _error(
            "reason_too_long",
            "The operator reason must be 500 characters or fewer.",
            field="reason",
        )
    return normalized


def confirm_admin_review(
    db: Session,
    *,
    invoice_id: UUID,
    actor: str,
    preview_fingerprint: str,
    confirmation_token: str,
    confirmed: str | None,
    reason: str,
) -> PrepaidDraftReconciliationResult:
    """Validate the staff review envelope and invoke the authoritative owner."""

    normalized_actor = actor.strip()
    if not normalized_actor:
        _error("actor_required", "An authorized staff actor is required.")
    if confirmed != "yes":
        _error(
            "confirmation_required",
            "Confirm the reviewed reconciliation before continuing.",
        )
    normalized_reason = _validated_reason(reason)
    claims = _decode_confirmation(db, confirmation_token)
    if (
        str(claims.get("invoice_id") or "") != str(invoice_id)
        or not hmac.compare_digest(str(claims.get("actor") or ""), normalized_actor)
        or not hmac.compare_digest(
            str(claims.get("preview_fingerprint") or ""), preview_fingerprint
        )
    ):
        _error(
            "confirmation_context_changed",
            "The reviewed invoice, actor, or evidence changed; preview again.",
        )
    try:
        effective_at = datetime.fromtimestamp(int(claims["effective_at"]), tz=UTC)
        token_id = UUID(hex=str(claims["jti"])).hex
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PrepaidDraftAdminError(
            code="admin.prepaid_draft_reconciliation.invalid_confirmation",
            message="The reconciliation confirmation is invalid; preview again.",
        ) from exc

    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    return reconcile_prepaid_draft_invoice(
        db,
        ReconcilePrepaidDraftCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=normalized_actor,
                scope=ACTION_PERMISSION,
                reason=normalized_reason,
                idempotency_key=f"prepaid-draft-admin:{token_id}",
            ),
            invoice_id=invoice_id,
            preview_fingerprint=preview_fingerprint,
            effective_at=effective_at,
        ),
    )


def rebuild_review_with_error(
    db: Session,
    *,
    invoice_id: UUID,
    actor: str,
    reason: str,
    error: DomainError,
) -> PrepaidDraftAdminReview:
    """Issue a fresh preview after a rejected confirmation and retain safe input."""

    review = build_admin_review(db, invoice_id=invoice_id, actor=actor)
    field = str(error.details.get("field") or "")
    submission = ActionFormSubmission.from_mapping(
        ACTION_KEY,
        {"reason": reason},
        field_errors={field: error.message} if field == "reason" else None,
        general_error=None if field == "reason" else error.message,
    )
    return replace(review, action_form=review.action_form.bind(submission))


__all__ = [
    "ACTION_KEY",
    "ACTION_PERMISSION",
    "PREPAID_DRAFT_RECONCILIATION_FORM",
    "PrepaidDraftAdminError",
    "PrepaidDraftAdminReview",
    "build_admin_review",
    "confirm_admin_review",
    "preview_for_invoice_detail",
    "rebuild_review_with_error",
]
