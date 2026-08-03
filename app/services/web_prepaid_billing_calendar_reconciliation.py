"""Admin projection for reviewed prepaid billing-calendar reconciliation."""

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
from app.services.form_contracts import FormConsequence, FormContract, FormPrerequisite
from app.services.form_contracts import register as register_form_contract
from app.services.owner_commands import CommandContext
from app.services.prepaid_billing_calendar_reconciliation import (
    PrepaidBillingCalendarCohort,
    PrepaidBillingCalendarPreview,
    PrepaidBillingCalendarReconciliationResult,
    ReconcilePrepaidBillingCalendarCommand,
    preview_prepaid_billing_calendar_cohort,
    preview_prepaid_billing_calendar_reconciliation,
    reconcile_prepaid_billing_calendar,
)

ACTION_KEY = "admin.prepaid_billing_calendar_reconciliation"
READ_PERMISSION = "billing:reconciliation:read"
WRITE_PERMISSION = "billing:reconciliation:write"
_TOKEN_TYPE = "prepaid_billing_calendar_reconciliation_confirmation"
_TOKEN_ISSUER = "dotmac_sub.admin.prepaid_billing_calendar_reconciliation"
_TOKEN_VERSION = 1
_TOKEN_TTL = timedelta(minutes=10)


PREPAID_BILLING_CALENDAR_FORM = register_form_contract(
    FormContract(
        key=ACTION_KEY,
        title="Correct prepaid billing dates",
        entity="invoice",
        command_owner="financial.prepaid_billing_calendar_reconciliation",
        consequences=(
            FormConsequence(
                key="calendar_projection",
                label=(
                    "Invoice, line, entitlement, and subscription anchor move to the "
                    "reviewed Africa/Lagos calendar interval"
                ),
            ),
            FormConsequence(
                key="financial_invariance",
                label=(
                    "Invoice totals, payment, allocation, settlement, status, access, "
                    "and ledger evidence do not change"
                ),
            ),
            FormConsequence(
                key="evidence",
                label="Audit and outbox evidence are recorded atomically",
            ),
        ),
    )
)


class PrepaidBillingCalendarAdminError(DomainError):
    """Safe rejection produced by the admin confirmation adapter."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidBillingCalendarAdminError(
        code=f"admin.prepaid_billing_calendar_reconciliation.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class PrepaidBillingCalendarAdminReview:
    preview: PrepaidBillingCalendarPreview
    reviewed_at: datetime
    confirmation_expires_at: datetime | None
    action_form: ActionForm
    form_contract_state: dict[str, object]


def load_admin_queue(
    db: Session, *, limit: int = 100, offset: int = 0
) -> PrepaidBillingCalendarCohort:
    return preview_prepaid_billing_calendar_cohort(db, limit=limit, offset=offset)


def _claims(
    *, actor: str, preview: PrepaidBillingCalendarPreview, now: datetime
) -> dict[str, object]:
    return {
        "typ": _TOKEN_TYPE,
        "iss": _TOKEN_ISSUER,
        "ver": _TOKEN_VERSION,
        "jti": uuid4().hex,
        "actor": actor,
        "invoice_id": str(preview.invoice_id),
        "preview_fingerprint": preview.fingerprint,
        "iat": int(now.timestamp()),
        "exp": int((now + _TOKEN_TTL).timestamp()),
    }


def build_admin_review(
    db: Session,
    *,
    invoice_id: UUID,
    actor: str,
    now: datetime | None = None,
) -> PrepaidBillingCalendarAdminReview:
    normalized_actor = actor.strip()
    if not normalized_actor:
        _error("actor_required", "An authorized staff actor is required.")
    preview = preview_prepaid_billing_calendar_reconciliation(db, invoice_id)
    reviewed_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    expires_at = reviewed_at + _TOKEN_TTL
    hidden_values: tuple[ActionHiddenValue, ...] = ()
    confirmation: ActionConfirmation | None = None
    if preview.actionable:
        token = context_signing.sign_context_token(
            db, _claims(actor=normalized_actor, preview=preview, now=reviewed_at)
        )
        hidden_values = (
            ActionHiddenValue(key="preview_fingerprint", value=preview.fingerprint),
            ActionHiddenValue(key="confirmation_token", value=token),
        )
        confirmation = ActionConfirmation(
            title="Confirm this exact zero-value correction",
            message=(
                "I reviewed the before/after dates and understand that the owner "
                "will recheck every guard under lock before changing them."
            ),
        )
    action_form = ActionForm(
        key=ACTION_KEY,
        title="Correct prepaid billing dates",
        description=(
            "Record the operational reason. The reason is retained with the invoice, "
            "audit event, and durable domain event."
        ),
        action_url=(
            f"/admin/billing/reconciliation/billing-dates/{preview.invoice_id}/confirm"
        ),
        submit_label="Apply date correction",
        fields=(
            ActionField(
                key="reason",
                label="Operator reason",
                kind=ActionFieldKind.textarea,
                required=True,
                max_length=500,
                rows=3,
                placeholder="Explain why this exact UTC-to-WAT correction is approved.",
                help_text="Required audit evidence; do not include secrets.",
            ),
        ),
        hidden_values=hidden_values,
        tone=ActionTone.neutral,
        impact=(
            "Only calendar projections move. The economic delta is NGN 0.00 and no "
            "payment, ledger, invoice status, or access decision changes."
        ),
        confirmation=confirmation,
        allowed=preview.actionable,
        disabled_reason=None if preview.actionable else preview.reason,
    )
    prerequisites = [
        FormPrerequisite(
            key="exact_legacy_signature",
            label="The owner found one exact retired UTC-period signature",
            met=preview.actionable,
            reason=None if preview.actionable else preview.reason,
        ),
        FormPrerequisite(
            key="unambiguous_chain",
            label="Payment, settlement, invoice, entitlement, and anchor agree",
            met=preview.actionable,
            reason=None if preview.actionable else preview.reason,
        ),
    ]
    return PrepaidBillingCalendarAdminReview(
        preview=preview,
        reviewed_at=reviewed_at,
        confirmation_expires_at=expires_at if preview.actionable else None,
        action_form=action_form,
        form_contract_state=PREPAID_BILLING_CALENDAR_FORM.state(prerequisites),
    )


def _decode(db: Session, token: str) -> dict[Any, Any]:
    normalized = token.strip()
    if not normalized or len(normalized) > 131_072:
        _error("invalid_confirmation", "The confirmation is invalid; preview again.")
    try:
        claims = context_signing.verify_context_token(db, normalized)
    except JWTError as exc:
        raise PrepaidBillingCalendarAdminError(
            code="admin.prepaid_billing_calendar_reconciliation.expired_confirmation",
            message="The confirmation expired or is invalid; preview again.",
        ) from exc
    if (
        claims.get("typ") != _TOKEN_TYPE
        or claims.get("iss") != _TOKEN_ISSUER
        or claims.get("ver") != _TOKEN_VERSION
    ):
        _error("invalid_confirmation", "The confirmation is invalid; preview again.")
    return claims


def confirm_admin_review(
    db: Session,
    *,
    invoice_id: UUID,
    actor: str,
    preview_fingerprint: str,
    confirmation_token: str,
    confirmed: str | None,
    reason: str,
) -> PrepaidBillingCalendarReconciliationResult:
    normalized_actor = actor.strip()
    normalized_reason = reason.strip()
    if not normalized_actor:
        _error("actor_required", "An authorized staff actor is required.")
    if confirmed != "yes":
        _error("confirmation_required", "Confirm the reviewed correction first.")
    if not normalized_reason:
        _error("reason_required", "An operator reason is required.", field="reason")
    if len(normalized_reason) > 500:
        _error(
            "reason_too_long",
            "The operator reason must be 500 characters or fewer.",
            field="reason",
        )
    claims = _decode(db, confirmation_token)
    if (
        str(claims.get("invoice_id") or "") != str(invoice_id)
        or not hmac.compare_digest(str(claims.get("actor") or ""), normalized_actor)
        or not hmac.compare_digest(
            str(claims.get("preview_fingerprint") or ""), preview_fingerprint
        )
    ):
        _error(
            "confirmation_context_changed",
            "The invoice, actor, or evidence changed; preview again.",
        )
    try:
        token_id = UUID(hex=str(claims["jti"])).hex
    except (KeyError, TypeError, ValueError) as exc:
        raise PrepaidBillingCalendarAdminError(
            code="admin.prepaid_billing_calendar_reconciliation.invalid_confirmation",
            message="The confirmation is invalid; preview again.",
        ) from exc
    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    return reconcile_prepaid_billing_calendar(
        db,
        ReconcilePrepaidBillingCalendarCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=normalized_actor,
                scope=WRITE_PERMISSION,
                reason=normalized_reason,
                idempotency_key=f"billing-calendar-admin:{token_id}",
            ),
            invoice_id=invoice_id,
            preview_fingerprint=preview_fingerprint,
        ),
    )


def rebuild_review_with_error(
    db: Session,
    *,
    invoice_id: UUID,
    actor: str,
    reason: str,
    error: DomainError,
) -> PrepaidBillingCalendarAdminReview:
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
    "READ_PERMISSION",
    "WRITE_PERMISSION",
    "PrepaidBillingCalendarAdminReview",
    "build_admin_review",
    "confirm_admin_review",
    "load_admin_queue",
    "rebuild_review_with_error",
]
