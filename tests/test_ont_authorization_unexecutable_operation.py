from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.network import ont_provisioning_execution
from app.tasks import ont_provisioning

OPERATION_ID = UUID("00000000-0000-0000-0000-0000000000aa")


class _MarkFailedOperations:
    """Minimal stand-in recording how the operation was terminalized."""

    def __init__(self) -> None:
        self.failed: list[tuple[str, str, dict]] = []

    def get(self, _db, _operation_id):
        return SimpleNamespace(output_payload={"phase": "pre-cutover"})

    def mark_failed(self, _db, operation_id, message, output_payload=None):
        self.failed.append((operation_id, message, dict(output_payload or {})))


class _CommittingDb:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_unexecutable_authorization_is_marked_failed_not_left_pending(
    monkeypatch,
) -> None:
    """An operation that can never satisfy the contract must terminalize.

    Raising instead would leave it non-terminal forever, which reads as queued
    work rather than dead work.
    """

    operations = _MarkFailedOperations()
    monkeypatch.setattr(
        "app.services.network_operations.network_operations", operations
    )
    db = _CommittingDb()

    outcome = ont_provisioning_execution.fail_unexecutable_authorization(
        db,
        operation_id=OPERATION_ID,
        message="Authorize & provision requires an assigned ONT.",
    )

    assert outcome.status.value == "error"
    assert outcome.partial_success is False
    assert outcome.authorization.success is False
    assert db.commits == 1

    assert len(operations.failed) == 1
    tracked_id, message, payload = operations.failed[0]
    assert tracked_id == str(OPERATION_ID)
    assert "assigned ONT" in message
    # The pre-existing payload survives, so the evidence of what was attempted
    # is not erased by terminalizing it.
    assert payload["phase"] == "pre-cutover"
    assert payload["operation_id"] == str(OPERATION_ID)
    assert payload["status"] == "error"


def test_authorization_task_terminalizes_an_operation_without_an_ont(
    monkeypatch,
) -> None:
    """The task must not raise past its own handler for a missing ONT.

    An unhandled raise here strands the operation: Celery records a task error
    but nothing marks the row, so it stays non-terminal.
    """

    calls: list[dict] = []

    def _fail_unexecutable(db, *, operation_id, message):
        calls.append({"operation_id": operation_id, "message": message})
        return SimpleNamespace(to_dict=lambda: {"status": "error", "marked": True})

    monkeypatch.setattr(
        ont_provisioning_execution,
        "fail_unexecutable_authorization",
        _fail_unexecutable,
    )

    class _Session:
        def __enter__(self):
            return _CommittingDb()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        ont_provisioning.db_session_adapter, "session", lambda: _Session()
    )

    result = ont_provisioning.authorize_ont.__wrapped__.__wrapped__(
        "00000000-0000-0000-0000-000000000002",
        "0/1/3",
        "HWTC7D4607C3",
        scoped_ont_id=None,
        operation_id=str(OPERATION_ID),
        _network_dispatch_id="dispatch-1",
    )

    assert result == {"status": "error", "marked": True}
    assert len(calls) == 1
    assert calls[0]["operation_id"] == OPERATION_ID
    assert "assigned ONT" in calls[0]["message"]


def test_authorization_task_still_refuses_an_untracked_operation() -> None:
    """Without an operation id there is nothing to terminalize, so raising is right."""

    with pytest.raises(ValueError, match="operation is required"):
        ont_provisioning.authorize_ont.__wrapped__.__wrapped__(
            "00000000-0000-0000-0000-000000000002",
            "0/1/3",
            "HWTC7D4607C3",
            scoped_ont_id=None,
            operation_id=None,
            _network_dispatch_id="dispatch-1",
        )
