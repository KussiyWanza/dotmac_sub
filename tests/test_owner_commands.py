"""Behavior contract for the manifest-verified public command boundary."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceProjection
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    OwnerCommandError,
    execute_owner_command,
    execute_owner_savepoint,
)

_DEFINITION = OwnerCommandDefinition(
    owner="network.device_projection",
    concern="device_projections materialised table",
    name="test_projection_command",
)


def _context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:owner_commands",
        scope="network:test",
        reason="verify owner command transaction semantics",
    )


def _projection(source_id: str) -> DeviceProjection:
    return DeviceProjection(
        device_type="test",
        source_id=source_id,
        operational_status="not_working",
    )


def _count(db_session) -> int:
    return int(
        db_session.scalar(select(func.count()).select_from(DeviceProjection)) or 0
    )


def test_success_commits_before_return(db_session) -> None:
    def operation() -> str:
        db_session.add(_projection("committed"))
        return "complete"

    result = execute_owner_command(
        db_session,
        definition=_DEFINITION,
        context=_context(),
        operation=operation,
    )

    assert result == "complete"
    assert not db_session.in_transaction()
    assert _count(db_session) == 1


def test_failure_rolls_back_before_raising(db_session) -> None:
    def operation() -> None:
        db_session.add(_projection("rolled-back"))
        db_session.flush()
        raise RuntimeError("test failure")

    with pytest.raises(RuntimeError, match="test failure"):
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_active_caller_transaction_fails_closed_and_is_cleared(db_session) -> None:
    db_session.execute(select(DeviceProjection.id))

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=lambda: None,
        )

    assert captured.value.code == "network.device_projection.active_caller_transaction"
    assert not db_session.in_transaction()


def test_nested_public_command_rolls_back_outer_command(db_session) -> None:
    def outer_operation() -> None:
        db_session.add(_projection("nested"))
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=lambda: None,
        )

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=outer_operation,
        )

    assert captured.value.code == "network.device_projection.nested_owner_command"
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_helper_commit_is_rejected_before_state_is_committed(db_session) -> None:
    def operation() -> None:
        db_session.add(_projection("helper-commit"))
        db_session.commit()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_helper_rollback_cannot_return_a_successful_outcome(db_session) -> None:
    def operation() -> None:
        db_session.add(_projection("helper-rollback"))
        db_session.flush()
        db_session.rollback()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_open_savepoint_rolls_back_the_owner_transaction(db_session) -> None:
    savepoint = None

    def operation() -> None:
        nonlocal savepoint
        db_session.add(_projection("open-savepoint"))
        savepoint = db_session.begin_nested()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert savepoint is not None
    assert not savepoint.is_active
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_helper_savepoint_rollback_cannot_hide_partial_failure(db_session) -> None:
    def operation() -> None:
        savepoint = db_session.begin_nested()
        db_session.add(_projection("savepoint-rollback"))
        db_session.flush()
        savepoint.rollback()

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=operation,
        )

    assert (
        captured.value.code == "network.device_projection.nested_transaction_completion"
    )
    assert not db_session.in_transaction()
    assert _count(db_session) == 0


def test_authorized_owner_savepoint_can_commit_an_optional_step(db_session) -> None:
    def operation() -> None:
        execute_owner_savepoint(
            db_session,
            lambda: db_session.add(_projection("isolated-success")),
        )

    execute_owner_command(
        db_session,
        definition=_DEFINITION,
        context=_context(),
        operation=operation,
    )

    assert _count(db_session) == 1


def test_authorized_owner_savepoint_can_stage_failure_evidence(db_session) -> None:
    def fail_optional_step() -> None:
        db_session.add(_projection("discarded-optional-write"))
        db_session.flush()
        raise ValueError("optional step rejected")

    def operation() -> None:
        try:
            execute_owner_savepoint(db_session, fail_optional_step)
        except ValueError:
            db_session.add(_projection("durable-failure-evidence"))

    execute_owner_command(
        db_session,
        definition=_DEFINITION,
        context=_context(),
        operation=operation,
    )

    source_ids = set(db_session.scalars(select(DeviceProjection.source_id)).all())
    assert source_ids == {"durable-failure-evidence"}


def test_uncontracted_owner_cannot_use_runtime_boundary(db_session) -> None:
    uncontracted = OwnerCommandDefinition(
        owner="events.store",
        concern="event persistence",
        name="invalid_event_store_command",
    )

    with pytest.raises(OwnerCommandError) as captured:
        execute_owner_command(
            db_session,
            definition=uncontracted,
            context=_context(),
            operation=lambda: None,
        )

    assert captured.value.code == "events.store.command_contract_violation"
    assert not db_session.in_transaction()


def test_owner_savepoint_recovers_real_flush_constraint_failure(
    db_session: Session,
) -> None:
    def insert_duplicate() -> None:
        db_session.add(_projection("duplicate-source"))
        db_session.flush()

    def operation() -> None:
        db_session.add(_projection("duplicate-source"))
        db_session.flush()
        with pytest.raises(IntegrityError):
            execute_owner_savepoint(db_session, insert_duplicate)
        assert db_session.is_active
        assert not db_session.in_nested_transaction()
        db_session.add(_projection("after-conflict"))

    execute_owner_command(
        db_session, definition=_DEFINITION, context=_context(), operation=operation
    )

    assert not db_session.in_transaction()
    assert set(db_session.scalars(select(DeviceProjection.source_id))) == {
        "duplicate-source",
        "after-conflict",
    }


@pytest.mark.parametrize(
    "action", ["commit", "rollback", "savepoint_commit", "savepoint_rollback"]
)
def test_owner_savepoint_still_rejects_participant_transaction_completion(
    db_session: Session,
    action: str,
) -> None:
    def participant() -> None:
        db_session.add(_projection("illegal-participant"))
        db_session.flush()
        if action == "commit":
            db_session.commit()
        elif action == "rollback":
            db_session.rollback()
        else:
            nested = db_session.get_nested_transaction()
            assert nested is not None
            if action == "savepoint_commit":
                nested.commit()
            else:
                nested.rollback()

    with pytest.raises(OwnerCommandError):
        execute_owner_command(
            db_session,
            definition=_DEFINITION,
            context=_context(),
            operation=lambda: execute_owner_savepoint(db_session, participant),
        )

    assert not db_session.in_transaction()
    assert _count(db_session) == 0
