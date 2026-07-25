"""Focused tests for previewed dunning staff actions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.billing import Invoice, InvoiceStatus
from app.models.collections import DunningActionLog, DunningCaseStatus
from app.schemas.collections import DunningCaseCreate
from app.services import collections as collections_service
from app.services import dunning_staff_actions
from app.services.collections import _core as dunning_owner
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _case(db_session, account, status=DunningCaseStatus.open):
    return collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(
            account_id=account.id,
            status=status,
        ),
    )


def _context(action: str) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{uuid4()}",
        scope=dunning_staff_actions.ACTION_SCOPE,
        reason=f"pytest dunning {action}",
        idempotency_key=f"pytest:dunning:{action}:{uuid4()}",
    )


def _command(
    preview: dunning_owner.DunningStaffActionPreview,
    *,
    confirmed: bool = True,
    actor_id: str | None = None,
) -> dunning_staff_actions.ConfirmDunningStaffAction:
    return dunning_staff_actions.ConfirmDunningStaffAction(
        action=preview.action,
        case_ids=preview.requested_case_ids,
        preview_fingerprint=preview.fingerprint,
        confirmed=confirmed,
        actor_id=actor_id or str(uuid4()),
        context=_context(preview.action.value),
    )


def test_preview_reports_exact_eligible_and_skipped_membership(
    db_session,
    subscriber_account,
):
    open_case = _case(db_session, subscriber_account)
    paused_case = _case(
        db_session,
        subscriber_account,
        DunningCaseStatus.paused,
    )
    missing_id = uuid4()

    preview = dunning_staff_actions.preview_staff_action(
        db_session,
        action=dunning_owner.DunningStaffAction.pause,
        case_ids=(str(open_case.id), str(paused_case.id), str(missing_id)),
    )

    assert preview.selected_count == 3
    assert preview.eligible_case_ids == (open_case.id,)
    assert preview.skipped_count == 2
    missing = next(impact for impact in preview.impacts if impact.case_id == missing_id)
    assert missing.exists is False
    assert missing.reason == "Dunning case was not found."


def test_explicit_confirmation_is_required(db_session, subscriber_account):
    case = _case(db_session, subscriber_account)
    preview = dunning_staff_actions.preview_staff_action(
        db_session,
        action=dunning_owner.DunningStaffAction.pause,
        case_ids=(str(case.id),),
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(dunning_staff_actions.DunningStaffCommandError) as exc_info:
        dunning_staff_actions.confirm_staff_action(
            db_session,
            _command(preview, confirmed=False),
        )

    assert exc_info.value.code.endswith(".confirmation_required")
    db_session.refresh(case)
    assert case.status is DunningCaseStatus.open


def test_confirmed_bulk_action_commits_transition_log_and_audit_together(
    db_session,
    subscriber_account,
):
    open_case = _case(db_session, subscriber_account)
    paused_case = _case(
        db_session,
        subscriber_account,
        DunningCaseStatus.paused,
    )
    actor_id = str(uuid4())
    preview = dunning_staff_actions.preview_staff_action(
        db_session,
        action=dunning_owner.DunningStaffAction.pause,
        case_ids=(str(open_case.id), str(paused_case.id)),
    )
    db_session_adapter.release_read_transaction(db_session)

    result = dunning_staff_actions.confirm_staff_action(
        db_session,
        _command(preview, actor_id=actor_id),
    )

    assert result.processed_case_ids == (open_case.id,)
    db_session.refresh(open_case)
    db_session.refresh(paused_case)
    assert open_case.status is DunningCaseStatus.paused
    assert paused_case.status is DunningCaseStatus.paused
    action_log = (
        db_session.query(DunningActionLog)
        .filter(DunningActionLog.case_id == open_case.id)
        .filter(DunningActionLog.outcome == "paused")
        .one()
    )
    assert action_log.notes == "Case paused by staff"
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "dunning_case")
        .filter(AuditEvent.entity_id == str(open_case.id))
        .filter(AuditEvent.action == "pause")
        .one()
    )
    assert audit.actor_id == actor_id
    assert audit.metadata_["selected_count"] == 2
    assert audit.metadata_["skipped_count"] == 1


def test_audit_failure_rolls_back_every_selected_transition(
    db_session,
    subscriber_account,
    monkeypatch,
):
    first = _case(db_session, subscriber_account)
    second = _case(db_session, subscriber_account)
    preview = dunning_staff_actions.preview_staff_action(
        db_session,
        action=dunning_owner.DunningStaffAction.pause,
        case_ids=(str(first.id), str(second.id)),
    )
    db_session_adapter.release_read_transaction(db_session)

    def _fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(
        dunning_staff_actions.audit_service.audit_events,
        "stage",
        _fail_audit,
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        dunning_staff_actions.confirm_staff_action(
            db_session,
            _command(preview),
        )

    db_session.refresh(first)
    db_session.refresh(second)
    assert first.status is DunningCaseStatus.open
    assert second.status is DunningCaseStatus.open
    assert (
        db_session.query(DunningActionLog)
        .filter(DunningActionLog.case_id.in_([first.id, second.id]))
        .filter(DunningActionLog.outcome == "paused")
        .count()
        == 0
    )


def test_changed_case_state_rejects_stale_preview(db_session, subscriber_account):
    case = _case(db_session, subscriber_account)
    preview = dunning_staff_actions.preview_staff_action(
        db_session,
        action=dunning_owner.DunningStaffAction.pause,
        case_ids=(str(case.id),),
    )
    case.notes = "New collections evidence"
    db_session.commit()

    with pytest.raises(dunning_staff_actions.DunningStaffCommandError) as exc_info:
        dunning_staff_actions.confirm_staff_action(
            db_session,
            _command(preview),
        )

    assert exc_info.value.code.endswith(".stale_preview")
    db_session.refresh(case)
    assert case.status is DunningCaseStatus.open


def test_close_preview_uses_canonical_collectible_receivables(
    db_session,
    subscriber_account,
):
    case = _case(db_session, subscriber_account)
    invoice = Invoice(
        account_id=subscriber_account.id,
        status=InvoiceStatus.overdue,
        currency="NGN",
        subtotal=Decimal("250.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("250.00"),
        balance_due=Decimal("250.00"),
        issued_at=datetime.now(UTC) - timedelta(days=40),
        due_at=datetime.now(UTC) - timedelta(days=10),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.commit()

    preview = dunning_staff_actions.preview_staff_action(
        db_session,
        action=dunning_owner.DunningStaffAction.close,
        case_ids=(str(case.id),),
    )

    impact = preview.impacts[0]
    assert impact.eligible is False
    assert impact.unpaid_invoice_count == 1
    assert impact.unpaid_receivables == (("NGN", Decimal("250.00")),)
    assert "collectible overdue receivables" in (impact.reason or "")


def test_close_changes_case_only_after_clear_receivable_preview(
    db_session,
    subscriber_account,
):
    case = _case(db_session, subscriber_account)
    preview = dunning_staff_actions.preview_staff_action(
        db_session,
        action=dunning_owner.DunningStaffAction.close,
        case_ids=(str(case.id),),
    )
    db_session_adapter.release_read_transaction(db_session)

    result = dunning_staff_actions.confirm_staff_action(
        db_session,
        _command(preview),
    )

    assert result.processed_case_ids == (case.id,)
    db_session.refresh(case)
    assert case.status is DunningCaseStatus.closed
    assert case.resolved_at is not None
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_id == str(case.id))
        .filter(AuditEvent.action == "close")
        .one()
    )
    assert audit.metadata_["current_status"] == "open"
    assert audit.metadata_["resulting_status"] == "closed"
