"""Read-side projections for admin payment-arrangement pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.models.payment_arrangement import (
    ArrangementStatus,
    InstallmentStatus,
    PaymentArrangement,
    PaymentFrequency,
)
from app.services import display_format
from app.services import payment_arrangements as arrangement_service
from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionFormSubmission,
    ActionHiddenValue,
    ActionTone,
)
from app.services.common import validate_enum as _validate_enum

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


APPROVE_ACTION_KEY = "payment_arrangement.approve"
CANCEL_ACTION_KEY = "payment_arrangement.cancel"
RECORD_PAYMENT_ACTION_KEY = "payment_arrangement.record_payment"

_ARRANGEMENT_PRESENTATION = {
    ArrangementStatus.pending: ("Pending", "pending"),
    ArrangementStatus.active: ("Active", "active"),
    ArrangementStatus.completed: ("Completed", "success"),
    ArrangementStatus.defaulted: ("Defaulted", "error"),
    ArrangementStatus.canceled: ("Canceled", "neutral"),
}
_INSTALLMENT_PRESENTATION = {
    InstallmentStatus.pending: ("Pending", "pending"),
    InstallmentStatus.due: ("Due", "info"),
    InstallmentStatus.paid: ("Paid", "active"),
    InstallmentStatus.overdue: ("Overdue", "error"),
    InstallmentStatus.waived: ("Waived", "neutral"),
}


@dataclass(frozen=True, slots=True)
class PaymentArrangementInstallmentProjection:
    number: int
    amount: str
    due_date: str
    status_label: str
    status_variant: str
    paid_at: str


@dataclass(frozen=True, slots=True)
class PaymentArrangementDetailProjection:
    id: str
    status_label: str
    status_variant: str
    total_amount: str
    installment_amount: str
    frequency: str
    progress: str
    start_date: str
    end_date: str
    next_due_date: str
    created_at: str
    notes: str | None
    installments: tuple[PaymentArrangementInstallmentProjection, ...]


def _date_display(value: date | None) -> str:
    return value.isoformat() if value else "—"


def _action_form(
    preview: arrangement_service.PaymentArrangementStaffActionPreview,
) -> ActionForm:
    action = preview.action
    action_url = (
        f"/admin/billing/payment-arrangements/{preview.arrangement_id}/"
        f"{action.value.replace('_', '-')}"
    )
    hidden_values = (
        ActionHiddenValue(
            key="preview_fingerprint",
            value=preview.fingerprint,
        ),
    )
    if action is arrangement_service.PaymentArrangementStaffAction.approve:
        return ActionForm(
            key=APPROVE_ACTION_KEY,
            title="Approve arrangement",
            description="Activate the reviewed installment schedule.",
            action_url=action_url,
            submit_label="Approve arrangement",
            fields=(),
            tone=ActionTone.positive,
            impact=(
                f"{preview.current_status.value.title()} → "
                f"{preview.resulting_status.value.title()}. "
                f"{preview.collection_shield_change}"
            ),
            confirmation=ActionConfirmation(
                title="Confirm approval",
                message=(
                    "I reviewed the schedule and authorize this arrangement to "
                    "become active."
                ),
            ),
            hidden_values=hidden_values,
        )
    if action is arrangement_service.PaymentArrangementStaffAction.cancel:
        return ActionForm(
            key=CANCEL_ACTION_KEY,
            title="Cancel arrangement",
            description="Stop this payment arrangement without changing receivables.",
            action_url=action_url,
            submit_label="Cancel arrangement",
            fields=(),
            tone=ActionTone.negative,
            impact=(
                f"{preview.current_status.value.title()} → "
                f"{preview.resulting_status.value.title()}. "
                f"{preview.collection_shield_change}"
            ),
            confirmation=ActionConfirmation(
                title="Confirm cancellation",
                message=(
                    "I understand the arrangement will stop and invoice balances "
                    "will remain unchanged."
                ),
            ),
            hidden_values=hidden_values,
        )
    amount = display_format.format_currency_amount(
        preview.installment_amount,
        preview.currency,
    )
    return ActionForm(
        key=RECORD_PAYMENT_ACTION_KEY,
        title=f"Record installment #{preview.installment_number}",
        description="Record an externally verified installment payment.",
        action_url=action_url,
        submit_label="Record installment payment",
        fields=(
            ActionField(
                key="note",
                label="Evidence note",
                kind=ActionFieldKind.textarea,
                max_length=255,
                rows=2,
                help_text=(
                    "Optional operational evidence only; this does not create a "
                    "billing Payment document."
                ),
            ),
        ),
        tone=ActionTone.positive,
        impact=(
            f"Installment #{preview.installment_number} for {amount} will be marked "
            f"paid. Resulting arrangement state: "
            f"{preview.resulting_status.value.title()}. "
            f"{preview.collection_shield_change}"
        ),
        confirmation=ActionConfirmation(
            title="Confirm external payment evidence",
            message=(
                "I verified this installment was paid outside the billing payment "
                "workflow and understand no Payment or ledger entry will be created."
            ),
        ),
        hidden_values=hidden_values,
    )


def _bind_submission(
    forms: tuple[ActionForm, ...],
    submission: ActionFormSubmission | None,
) -> tuple[ActionForm, ...]:
    if submission is None:
        return forms
    bound: list[ActionForm] = []
    for form in forms:
        if form.key != submission.action_key:
            bound.append(form)
            continue
        field_keys = {field.key for field in form.fields}
        bound.append(form.bind(submission.restrict(field_keys)))
    return tuple(bound)


def list_data(
    db: Session,
    *,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Build template context for the payment arrangements list page."""

    offset = (page - 1) * per_page
    arrangements = arrangement_service.payment_arrangements.list(
        db=db,
        account_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    stmt = select(func.count(PaymentArrangement.id)).where(
        PaymentArrangement.is_active.is_(True)
    )
    if status:
        stmt = stmt.where(
            PaymentArrangement.status
            == _validate_enum(status, ArrangementStatus, "status")
        )
    total = db.scalar(stmt) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "arrangements": arrangements,
        "statuses": [s.value for s in ArrangementStatus],
        "frequencies": [f.value for f in PaymentFrequency],
        "status_filter": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def detail_data(
    db: Session,
    *,
    arrangement_id: str,
    can_write: bool,
    submission: ActionFormSubmission | None = None,
) -> dict[str, object]:
    """Build a pure-render detail projection and its permitted safe actions."""

    arrangement = arrangement_service.payment_arrangements.get(db, arrangement_id)
    installments = arrangement_service.installments.list(
        db=db,
        arrangement_id=arrangement_id,
        status=None,
        order_by="installment_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    currency = display_format.currency_code(
        arrangement.invoice.currency
        if arrangement.invoice and arrangement.invoice.currency
        else display_format.default_currency(db)
    )
    status_label, status_variant = _ARRANGEMENT_PRESENTATION[arrangement.status]
    installment_projections = tuple(
        PaymentArrangementInstallmentProjection(
            number=item.installment_number,
            amount=display_format.format_currency_amount(item.amount, currency),
            due_date=_date_display(item.due_date),
            status_label=_INSTALLMENT_PRESENTATION[item.status][0],
            status_variant=_INSTALLMENT_PRESENTATION[item.status][1],
            paid_at=display_format.format_timestamp(item.paid_at, db),
        )
        for item in installments
    )
    detail = PaymentArrangementDetailProjection(
        id=str(arrangement.id),
        status_label=status_label,
        status_variant=status_variant,
        total_amount=display_format.format_currency_amount(
            arrangement.total_amount,
            currency,
        ),
        installment_amount=display_format.format_currency_amount(
            arrangement.installment_amount,
            currency,
        ),
        frequency=arrangement.frequency.value.replace("_", " ").title(),
        progress=(
            f"{arrangement.installments_paid} / "
            f"{arrangement.installments_total} installments"
        ),
        start_date=_date_display(arrangement.start_date),
        end_date=_date_display(arrangement.end_date),
        next_due_date=_date_display(arrangement.next_due_date),
        created_at=display_format.format_timestamp(arrangement.created_at, db),
        notes=arrangement.notes,
        installments=installment_projections,
    )
    previews = (
        arrangement_service.available_staff_action_previews(
            db,
            arrangement_id=arrangement.id,
        )
        if can_write
        else ()
    )
    forms = _bind_submission(
        tuple(_action_form(preview) for preview in previews),
        submission,
    )
    return {
        "arrangement_detail": detail,
        "arrangement_actions": forms,
    }


def action_error_submission(
    *,
    action: arrangement_service.PaymentArrangementStaffAction,
    note: str | None,
    error: Exception,
) -> ActionFormSubmission:
    """Bind a typed command failure to its declared safe-action form."""

    key_by_action = {
        arrangement_service.PaymentArrangementStaffAction.approve: APPROVE_ACTION_KEY,
        arrangement_service.PaymentArrangementStaffAction.cancel: CANCEL_ACTION_KEY,
        arrangement_service.PaymentArrangementStaffAction.record_payment: (
            RECORD_PAYMENT_ACTION_KEY
        ),
    }
    message = getattr(error, "message", str(error))
    field = getattr(error, "details", {}).get("field")
    values = (
        {"note": note}
        if action is arrangement_service.PaymentArrangementStaffAction.record_payment
        else {}
    )
    return ActionFormSubmission.from_mapping(
        key_by_action[action],
        values,
        field_errors={str(field): message} if field == "note" else None,
        general_error=None if field == "note" else message,
    )
