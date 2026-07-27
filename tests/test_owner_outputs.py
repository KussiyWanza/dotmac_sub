"""Behavior coverage for `events.owner_outputs` (ADR 0007 Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.event_store import EventStore
from app.models.owner_output import OwnerOutputReceipt, ReceiptOutcome
from app.services.events.owner_outputs import (
    OwnerOutputEnvelope,
    OwnerOutputError,
    consume_owner_output,
    record_terminal_failure,
    stage_owner_output,
)
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

# A real contracted owner command hosts the participant calls, exactly as a
# producing or consuming money owner would.
_HOST_COMMAND = OwnerCommandDefinition(
    owner="billing.contracts",
    concern="versioned billing contract terms",
    name="record_billing_contract_version",
)


def _context(key: str | None = None) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="owner-output:test",
        reason="pytest owner outputs",
        idempotency_key=key or f"pytest:{command_id}",
    )


def _in_owner_command(db, operation):
    return execute_owner_command(
        db, definition=_HOST_COMMAND, context=_context(), operation=operation
    )


def _envelope() -> OwnerOutputEnvelope:
    return OwnerOutputEnvelope(
        event_type=EventType.custom,
        producer_owner="billing.contracts",
        source_kind="billing_contract_version",
        source_id=uuid4(),
        occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
    )


def test_staging_outside_an_owner_command_fails_closed(db_session):
    with pytest.raises(OwnerOutputError) as excinfo:
        stage_owner_output(db_session, _envelope(), {"detail": "x"}, context=_context())

    assert excinfo.value.code == ("events.owner_outputs.output_requires_owner_command")


def test_staged_output_commits_with_the_owner_state_and_carries_the_envelope(
    db_session,
):
    context = _context("pytest:producer-key")
    event_id = _in_owner_command(
        db_session,
        lambda: stage_owner_output(
            db_session,
            _envelope(),
            {"detail": "terms recorded"},
            context=context,
        ),
    )

    record = db_session.execute(
        select(EventStore).where(EventStore.event_id == event_id)
    ).scalar_one()
    envelope = record.payload["envelope"]

    assert envelope["producer_owner"] == "billing.contracts"
    assert envelope["schema_version"] == 1
    assert envelope["idempotency_key"] == "pytest:producer-key"
    assert envelope["command_id"] == str(context.command_id)


def test_a_failed_producer_command_leaves_no_output(db_session):
    context = _context()

    class Boom(RuntimeError):
        pass

    def operation():
        stage_owner_output(db_session, _envelope(), {"detail": "x"}, context=context)
        raise Boom("state change failed after staging")

    with pytest.raises(Boom):
        _in_owner_command(db_session, operation)

    db_session.commit()
    count = len(db_session.execute(select(EventStore)).scalars().all())
    assert count == 0


def test_consumer_effect_and_receipt_commit_together_and_replay_once(db_session):
    event_id = uuid4()
    applied: list[str] = []

    def consume(context):
        return consume_owner_output(
            db_session,
            consumer="billing.obligations",
            event_id=event_id,
            event_type="billing.contract.activated",
            producer_owner="billing.contracts",
            context=context,
            operation=lambda: applied.append("effect") or "done",
        )

    first_context = _context("pytest:consumer-key")
    result, receipt = _in_owner_command(db_session, lambda: consume(first_context))

    assert result == "done"
    assert applied == ["effect"]
    assert receipt.outcome is ReceiptOutcome.succeeded
    # Reading the receipt after commit opened a refresh transaction; close it
    # so the next owner command starts transaction-free.
    db_session.commit()

    # Redelivery: same consumer, same event. The effect must not run again.
    second_context = _context("pytest:consumer-key-2")
    replay_result, replay_receipt = _in_owner_command(
        db_session, lambda: consume(second_context)
    )

    assert replay_result is None
    assert applied == ["effect"]
    assert replay_receipt.effect_idempotency_key == "pytest:consumer-key"


def test_a_raised_consumer_error_leaves_no_receipt(db_session):
    event_id = uuid4()

    class Transient(RuntimeError):
        pass

    def consume():
        context = _context()
        return consume_owner_output(
            db_session,
            consumer="billing.obligations",
            event_id=event_id,
            event_type="billing.contract.activated",
            producer_owner="billing.contracts",
            context=context,
            operation=lambda: (_ for _ in ()).throw(Transient("retry me")),
        )

    with pytest.raises(Transient):
        _in_owner_command(db_session, consume)

    db_session.commit()
    receipts = db_session.execute(select(OwnerOutputReceipt)).scalars().all()
    # No receipt: the delivery stays durably retryable, not silently done.
    assert receipts == []


def test_terminal_failure_is_recorded_with_evidence_and_only_once(db_session):
    event_id = uuid4()

    first_context = _context()
    receipt = _in_owner_command(
        db_session,
        lambda: record_terminal_failure(
            db_session,
            consumer="billing.obligations",
            event_id=event_id,
            event_type="billing.contract.activated",
            producer_owner="billing.contracts",
            context=first_context,
            failure_reason="contract version deleted before delivery; reviewed",
        ),
    )

    assert receipt.outcome is ReceiptOutcome.terminal_failure
    assert "reviewed" in receipt.failure_reason
    db_session.commit()

    second_context = _context()
    with pytest.raises(OwnerOutputError) as excinfo:
        _in_owner_command(
            db_session,
            lambda: record_terminal_failure(
                db_session,
                consumer="billing.obligations",
                event_id=event_id,
                event_type="billing.contract.activated",
                producer_owner="billing.contracts",
                context=second_context,
                failure_reason="second attempt",
            ),
        )

    assert excinfo.value.code == "events.owner_outputs.receipt_already_recorded"


def test_terminal_failure_requires_reviewable_evidence(db_session):
    context = _context()

    with pytest.raises(OwnerOutputError) as excinfo:
        _in_owner_command(
            db_session,
            lambda: record_terminal_failure(
                db_session,
                consumer="billing.obligations",
                event_id=uuid4(),
                event_type="billing.contract.activated",
                producer_owner="billing.contracts",
                context=context,
                failure_reason="   ",
            ),
        )

    assert excinfo.value.code == "events.owner_outputs.missing_failure_reason"
