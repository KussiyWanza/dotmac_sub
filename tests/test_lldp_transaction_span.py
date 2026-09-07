"""Task lifecycle and detached transport regressions; no router I/O."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.models.router_management import RouterAccessMethod
from app.services.topology import lldp_poller
from app.services.topology.lldp_contracts import (
    LldpDevice,
    LldpReadQuery,
    LldpRouter,
    LldpSnapshot,
    LldpStats,
    ReconcileLldpCommand,
)
from app.tasks import topology_lldp


def snapshot() -> LldpSnapshot:
    device_id = uuid4()
    return LldpSnapshot(
        observed_at=datetime.now(UTC),
        routers=(
            LldpRouter(
                id=uuid4(),
                name="test-router",
                management_ip="192.0.2.1",
                rest_api_port=443,
                rest_api_username="user",
                rest_api_password="secret",
                use_ssl=True,
                verify_tls=True,
                access_method=RouterAccessMethod.direct,
                jump_host=None,
                network_device_id=device_id,
            ),
        ),
        devices=(LldpDevice(device_id, "test-router", None, "192.0.2.1", True),),
        links=(),
    )


@pytest.mark.parametrize("router_error", [None, TimeoutError("slow router")])
def test_network_phase_has_no_open_session(
    monkeypatch: pytest.MonkeyPatch,
    router_error: Exception | None,
) -> None:
    events: list[str] = []
    sessions: list[MagicMock] = []
    source = snapshot()

    def create() -> MagicMock:
        events.append("open")
        db = MagicMock(spec=Session)
        db.close.side_effect = lambda: events.append("close")
        sessions.append(db)
        return db

    def read(db: Session, *, query: LldpReadQuery) -> LldpSnapshot:
        events.append("read")
        assert query.observed_at.tzinfo is not None
        return source

    def network(router: LldpRouter) -> list[dict[str, str]]:
        events.append("network")
        assert len(sessions) == 1
        sessions[0].close.assert_called_once()
        assert router == source.routers[0]
        with pytest.raises(FrozenInstanceError):
            router.name = "mutation"  # type: ignore[misc]
        if router_error:
            raise router_error
        return []

    def persist(db: Session, *, command: ReconcileLldpCommand) -> LldpStats:
        events.append("persist")
        assert len(sessions) == 2
        assert command.poll.stats.routers_failed == int(router_error is not None)
        return command.poll.stats

    monkeypatch.setattr(topology_lldp.db_session_adapter, "create_session", create)
    monkeypatch.setattr(lldp_poller, "read_snapshot", read)
    monkeypatch.setattr(lldp_poller, "_read_neighbors", network)
    monkeypatch.setattr(lldp_poller, "reconcile_poll", persist)
    store = MagicMock()
    monkeypatch.setattr(topology_lldp, "store_task_stats", store)
    result = topology_lldp.run_lldp_topology_poll()
    assert events == ["open", "read", "close", "network", "open", "persist", "close"]
    assert result["routers_failed"] == int(router_error is not None)
    store.assert_called_once_with("lldp_poll", result)


@pytest.mark.parametrize("phase", ["read", "network", "persist"])
@pytest.mark.parametrize(
    "error", [RuntimeError("database failure"), SoftTimeLimitExceeded()]
)
def test_whole_run_failure_propagates_and_reports(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error: Exception,
) -> None:
    source = snapshot()
    sessions: list[MagicMock] = []

    def create() -> MagicMock:
        db = MagicMock(spec=Session)
        sessions.append(db)
        return db

    def read(db: Session, *, query: LldpReadQuery) -> LldpSnapshot:
        if phase == "read":
            raise error
        return source

    def poll(*, snapshot: LldpSnapshot) -> lldp_poller.LldpPoll:
        if phase == "network":
            raise error
        return lldp_poller.LldpPoll(snapshot, (), frozenset(), LldpStats())

    def persist(db: Session, *, command: ReconcileLldpCommand) -> LldpStats:
        raise error

    monkeypatch.setattr(topology_lldp.db_session_adapter, "create_session", create)
    monkeypatch.setattr(lldp_poller, "read_snapshot", read)
    monkeypatch.setattr(lldp_poller, "poll_all", poll)
    monkeypatch.setattr(lldp_poller, "reconcile_poll", persist)
    store = MagicMock(side_effect=RuntimeError("metrics unavailable"))
    monkeypatch.setattr(topology_lldp, "store_task_stats", store)
    with pytest.raises(type(error)) as caught:
        topology_lldp.run_lldp_topology_poll()
    assert caught.value is error
    assert "error" in store.call_args.args[1]
    for db in sessions:
        db.close.assert_called_once()


def test_cleanup_failure_does_not_mask_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock(spec=Session)
    db.rollback.side_effect = RuntimeError("rollback failed")
    monkeypatch.setattr(topology_lldp.db_session_adapter, "create_session", lambda: db)
    original = RuntimeError("original")
    with pytest.raises(RuntimeError) as caught, topology_lldp._poll_session():
        raise original
    assert caught.value is original
    db.close.assert_called_once()


def test_celery_records_failed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        topology_lldp.db_session_adapter,
        "create_session",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(topology_lldp, "store_task_stats", MagicMock())
    result = topology_lldp.run_lldp_topology_poll.apply(throw=False)
    assert result.state == "FAILURE"
