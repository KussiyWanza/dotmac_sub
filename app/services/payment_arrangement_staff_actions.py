"""Atomic staff commands for payment-arrangement lifecycle actions.

``financial.payment_arrangements`` owns arrangement eligibility and mutation.
This coordinator binds an owner-authored impact preview to explicit staff
confirmation, then stages the transition and immutable audit evidence in one
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.payment_arrangement import (
    PaymentArrangement,
    PaymentArrangementInstallment,
)
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services import payment_arrangements
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.payment_arrangement_staff_actions"
ACTION_SCOPE = "billing:arrangement:write"
_ACTION_CONCERN = "atomic staff arrangement transition and audit coordination"
_CONFIRM_ACTION = OwnerCommandDefinition(
    owner=OWNER,
    concern=_ACTION_CONCERN,
    name="confirm_payment_arrangement_staff_action",
)


class PaymentArrangementStaffCommandError(DomainError):
    """Stable command errors for staff arrangement actions."""


@dataclass(frozen=True, slots=True)
class ConfirmPaymentArrangementStaffAction:
    arrangement_id: UUID
    action: payment_arrangements.PaymentArrangementStaffAction
    preview_fingerprint: str
    confirmed: bool
    actor_id: str
    note: str | None
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PaymentArrangementStaffActionResult:
    preview: payment_arrangements.PaymentArrangementStaffActionPreview
    arrangement: PaymentArrangement
    installment: PaymentArrangementInstallment | None


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> PaymentArrangementStaffCommandError:
    return PaymentArrangementStaffCommandError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _normalized_note(note: str | None) -> str | None:
    value = str(note or "").strip()
    if len(value) > 255:
        raise _error(
            "invalid_note",
            "Payment note cannot exceed 255 characters.",
            field="note",
        )
    return value or None


def _stage_audit(
    db: Session,
    *,
    command: ConfirmPaymentArrangementStaffAction,
    preview: payment_arrangements.PaymentArrangementStaffActionPreview,
    installment: PaymentArrangementInstallment | None,
    note: str | None,
) -> None:
    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=command.actor_id,
            action=preview.action.value,
            entity_type="payment_arrangement",
            entity_id=str(preview.arrangement_id),
            status_code=200,
            is_success=True,
            request_id=str(command.context.correlation_id),
            metadata_={
                "owner": OWNER,
                "account_id": str(preview.account_id),
                "current_status": preview.current_status.value,
                "resulting_status": preview.resulting_status.value,
                "total_amount": str(preview.total_amount),
                "currency": preview.currency,
                "installment_id": str(installment.id) if installment else None,
                "installment_number": (
                    installment.installment_number if installment else None
                ),
                "installment_amount": (
                    str(installment.amount) if installment is not None else None
                ),
                "note": note,
                "preview_fingerprint": preview.fingerprint,
                "command_id": str(command.context.command_id),
                "command_scope": command.context.scope,
                "command_reason": command.context.reason,
            },
        ),
    )


def _stage_confirmation(
    db: Session,
    command: ConfirmPaymentArrangementStaffAction,
) -> PaymentArrangementStaffActionResult:
    if command.context.scope != ACTION_SCOPE:
        raise _error(
            "invalid_scope",
            "Payment-arrangement staff action has an invalid authorization scope.",
        )
    actor_id = command.actor_id.strip()
    if not actor_id:
        raise _error("invalid_actor", "Authorized staff actor is required.")
    if not command.confirmed:
        raise _error(
            "confirmation_required",
            "Confirm the displayed impact before applying this action.",
            field="confirmed",
        )
    note = _normalized_note(command.note)
    preview = payment_arrangements.preview_staff_action(
        db,
        arrangement_id=command.arrangement_id,
        action=command.action,
        lock=True,
    )
    if preview.fingerprint != command.preview_fingerprint:
        raise _error(
            "stale_preview",
            "Payment-arrangement state changed after preview; review the new impact.",
        )
    arrangement, installment = payment_arrangements.stage_staff_action(
        db,
        preview=preview,
        actor_id=actor_id,
        note=note,
    )
    _stage_audit(
        db,
        command=command,
        preview=preview,
        installment=installment,
        note=note,
    )
    return PaymentArrangementStaffActionResult(
        preview=preview,
        arrangement=arrangement,
        installment=installment,
    )


def confirm_staff_action(
    db: Session,
    command: ConfirmPaymentArrangementStaffAction,
) -> PaymentArrangementStaffActionResult:
    """Confirm one previewed staff action in an owned transaction."""

    return execute_owner_command(
        db,
        definition=_CONFIRM_ACTION,
        context=command.context,
        operation=lambda: _stage_confirmation(db, command),
    )
