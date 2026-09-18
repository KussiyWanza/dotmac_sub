"""Bounded retry policy with real owner rollback and mocked PostgreSQL errors.

Database error codes are injected; actual cross-connection contention belongs to
migrated PostgreSQL CI, not the SQLite unit lane.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceProjection
from app.services import fup_enforcement as service


class _PgFailure(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("synthetic database contention")
        self.sqlstate = sqlstate


def _error(state: str) -> OperationalError:
    return OperationalError("synthetic locked operation", {}, _PgFailure(state))


def _discover(monkeypatch: pytest.MonkeyPatch, ids: list[UUID]) -> None:
    monkeypatch.setattr(service, "_sweep_policy", lambda db: (False, 0.8, False))
    monkeypatch.setattr(service, "_candidate_subscription_ids", lambda *a, **kw: ids)


@pytest.mark.parametrize("sqlstate", ["55P03", "40P01", "40001"])
def test_contended_subscription_does_not_rollback_success_or_abort_later_items(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str,
) -> None:
    first, blocked, last = uuid4(), uuid4(), uuid4()
    _discover(monkeypatch, [first, blocked, last])
    calls: list[service.EvaluateFupSubscriptionCommand] = []

    def evaluate(
        db: Session, command: service.EvaluateFupSubscriptionCommand
    ) -> service.FupSubscriptionOutcome:
        calls.append(command)
        db.add(
            DeviceProjection(
                device_type="test",
                source_id=str(command.subscription_id),
                operational_status="not_working",
            )
        )
        db.flush()
        if command.subscription_id == blocked:
            raise _error(sqlstate)
        return service.FupSubscriptionOutcome()

    monkeypatch.setattr(service, "_evaluate_subscription", evaluate)
    db_session.commit()
    result = service.run_fup_evaluation(db_session, service.RunFupSweepRequest(uuid4()))
    assert result.totals.processed == 2
    assert result.retried == 1
    assert result.deferred_subscription_ids == (blocked,)
    assert [c.subscription_id for c in calls] == [first, blocked, blocked, last]
    assert calls[1] is calls[2]
    assert not db_session.in_transaction()
    assert set(db_session.scalars(select(DeviceProjection.source_id))) == {
        str(first),
        str(last),
    }


def test_transient_retry_success_is_counted_once(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = uuid4()
    _discover(monkeypatch, [candidate])
    calls = 0

    def evaluate(
        db: Session, command: service.EvaluateFupSubscriptionCommand
    ) -> service.FupSubscriptionOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _error("55P03")
        return service.FupSubscriptionOutcome(enforced=1)

    monkeypatch.setattr(service, "_evaluate_subscription", evaluate)
    db_session.commit()
    result = service.run_fup_evaluation(db_session, service.RunFupSweepRequest(uuid4()))
    assert result.totals.processed == result.totals.enforced == 1
    assert result.retried == 1
    assert result.deferred_subscription_ids == ()


def test_unrecognized_database_errors_remain_failures(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _discover(monkeypatch, [uuid4(), uuid4()])
    evaluate = MagicMock(side_effect=_error("08006"))
    monkeypatch.setattr(service, "_evaluate_subscription", evaluate)
    db_session.commit()
    with pytest.raises(OperationalError):
        service.run_fup_evaluation(db_session, service.RunFupSweepRequest(uuid4()))
    assert evaluate.call_count == 1
    assert not db_session.in_transaction()


@pytest.mark.parametrize("already_retry", [False, True])
def test_task_schedules_only_the_deferred_subset_once(
    monkeypatch: pytest.MonkeyPatch,
    already_retry: bool,
) -> None:
    from app.tasks import usage

    blocked = uuid4()
    lock_db, owner_db = MagicMock(), MagicMock()
    lock_db.bind.dialect.name = "sqlite"
    monkeypatch.setattr(
        usage, "SessionLocal", MagicMock(side_effect=[lock_db, owner_db])
    )
    monkeypatch.setattr(
        service,
        "run_fup_evaluation",
        lambda *a: service.FupSweepOutcome(
            service.FupSubscriptionOutcome(processed=2), 1, (blocked,)
        ),
    )
    enqueue = MagicMock()
    monkeypatch.setattr(usage.evaluate_fup_rules, "apply_async", enqueue)
    result = usage.evaluate_fup_rules(contention_retry=already_retry)
    assert result["processed"] == 2
    assert result["deferred"] == 1
    assert result["deferred_retry_queued"] == int(not already_retry)
    if already_retry:
        enqueue.assert_not_called()
    else:
        enqueue.assert_called_once_with(
            kwargs={
                "subscription_ids": [str(blocked)],
                "source": "scheduled_full_sweep",
                "contention_retry": True,
            },
            queue="billing",
            countdown=60,
        )
    owner_db.close.assert_called_once()
