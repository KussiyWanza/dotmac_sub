"""Focused contract tests for previewed payment-arrangement staff actions."""

from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.payment_arrangement import ArrangementStatus, InstallmentStatus
from app.services import payment_arrangement_staff_actions as staff_actions
from app.services import payment_arrangements
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from tests.test_payment_arrangements import (
    _create_arrangement_directly,
    _installments_for,
)


def _context(action: str) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{uuid4()}",
        scope=staff_actions.ACTION_SCOPE,
        reason=f"pytest {action}",
        idempotency_key=f"pytest:arrangement:{action}:{uuid4()}",
    )


def _command(
    preview: payment_arrangements.PaymentArrangementStaffActionPreview,
    *,
    confirmed: bool = True,
    actor_id: str | None = None,
    note: str | None = None,
) -> staff_actions.ConfirmPaymentArrangementStaffAction:
    return staff_actions.ConfirmPaymentArrangementStaffAction(
        arrangement_id=preview.arrangement_id,
        action=preview.action,
        preview_fingerprint=preview.fingerprint,
        confirmed=confirmed,
        actor_id=actor_id or str(uuid4()),
        note=note,
        context=_context(preview.action.value),
    )


def test_available_preview_owns_pending_action_eligibility(db_session, subscriber):
    arrangement = _create_arrangement_directly(db_session, subscriber)

    previews = payment_arrangements.available_staff_action_previews(
        db_session,
        arrangement_id=arrangement.id,
    )

    assert [preview.action for preview in previews] == [
        payment_arrangements.PaymentArrangementStaffAction.approve,
        payment_arrangements.PaymentArrangementStaffAction.cancel,
    ]
    assert previews[0].current_status is ArrangementStatus.pending
    assert previews[0].resulting_status is ArrangementStatus.active
    assert "shielding" in previews[0].collection_shield_change


def test_confirmation_is_required_before_any_transition(db_session, subscriber):
    arrangement = _create_arrangement_directly(db_session, subscriber)
    preview = payment_arrangements.preview_staff_action(
        db_session,
        arrangement_id=arrangement.id,
        action=payment_arrangements.PaymentArrangementStaffAction.approve,
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(staff_actions.PaymentArrangementStaffCommandError) as exc_info:
        staff_actions.confirm_staff_action(
            db_session,
            _command(preview, confirmed=False),
        )

    assert exc_info.value.code.endswith(".confirmation_required")
    db_session.refresh(arrangement)
    assert arrangement.status is ArrangementStatus.pending


def test_approve_and_audit_commit_atomically(db_session, subscriber):
    arrangement = _create_arrangement_directly(db_session, subscriber)
    actor_id = str(uuid4())
    preview = payment_arrangements.preview_staff_action(
        db_session,
        arrangement_id=arrangement.id,
        action=payment_arrangements.PaymentArrangementStaffAction.approve,
    )
    db_session_adapter.release_read_transaction(db_session)

    result = staff_actions.confirm_staff_action(
        db_session,
        _command(preview, actor_id=actor_id),
    )

    assert result.arrangement.status is ArrangementStatus.active
    assert result.arrangement.approved_by_user_id == actor_id
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "payment_arrangement")
        .filter(AuditEvent.entity_id == str(arrangement.id))
        .filter(AuditEvent.action == "approve")
        .one()
    )
    assert audit.actor_id == actor_id
    assert audit.metadata_["preview_fingerprint"] == preview.fingerprint


def test_audit_failure_rolls_back_the_transition(
    db_session,
    subscriber,
    monkeypatch,
):
    arrangement = _create_arrangement_directly(db_session, subscriber)
    preview = payment_arrangements.preview_staff_action(
        db_session,
        arrangement_id=arrangement.id,
        action=payment_arrangements.PaymentArrangementStaffAction.approve,
    )
    db_session_adapter.release_read_transaction(db_session)

    def _fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(staff_actions.audit_service.audit_events, "stage", _fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        staff_actions.confirm_staff_action(db_session, _command(preview))

    db_session.refresh(arrangement)
    assert arrangement.status is ArrangementStatus.pending
    assert arrangement.approved_by_user_id is None


def test_changed_state_rejects_stale_preview(db_session, subscriber):
    arrangement = _create_arrangement_directly(db_session, subscriber)
    preview = payment_arrangements.preview_staff_action(
        db_session,
        arrangement_id=arrangement.id,
        action=payment_arrangements.PaymentArrangementStaffAction.approve,
    )
    arrangement.notes = "New evidence arrived"
    db_session.commit()

    with pytest.raises(staff_actions.PaymentArrangementStaffCommandError) as exc_info:
        staff_actions.confirm_staff_action(db_session, _command(preview))

    assert exc_info.value.code.endswith(".stale_preview")
    db_session.refresh(arrangement)
    assert arrangement.status is ArrangementStatus.pending


def test_record_payment_targets_previewed_installment_without_ledger_claim(
    db_session,
    subscriber,
):
    arrangement = _create_arrangement_directly(db_session, subscriber)
    payment_arrangements.payment_arrangements.approve(
        db_session,
        str(arrangement.id),
    )
    preview = payment_arrangements.preview_staff_action(
        db_session,
        arrangement_id=arrangement.id,
        action=payment_arrangements.PaymentArrangementStaffAction.record_payment,
    )
    db_session_adapter.release_read_transaction(db_session)

    result = staff_actions.confirm_staff_action(
        db_session,
        _command(preview, note="Cash desk receipt verified"),
    )

    assert result.installment is not None
    assert result.installment.id == preview.installment_id
    assert result.installment.status is InstallmentStatus.paid
    assert "Cash desk receipt verified" in (result.installment.notes or "")
    installments = _installments_for(db_session, arrangement)
    assert installments[1].status is InstallmentStatus.due
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(arrangement.id))
        .filter(AuditEvent.action == "record_payment")
        .one()
    )
    assert audit.metadata_["installment_id"] == str(preview.installment_id)
