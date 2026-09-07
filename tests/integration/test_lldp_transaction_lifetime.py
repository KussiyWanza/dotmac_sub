"""Migration-backed PostgreSQL locking and transaction lifetime acceptance."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.event_store import EventStatus, EventStore
from app.models.network_monitoring import NetworkDevice
from app.models.router_management import Router
from app.services.events.types import Event
from app.services.owner_commands import CommandContext
from app.services.topology import lldp_poller
from app.services.topology.lldp_contracts import (
    LldpReadQuery,
    LldpSnapshot,
    LldpStats,
    ReconcileLldpCommand,
)
from app.tasks import topology_lldp
from tests.test_lldp_transaction_span import snapshot


def _router_node(db: Session, name: str) -> tuple[NetworkDevice, Router]:
    node = NetworkDevice(name=name, source="pytest", is_active=True)
    db.add(node)
    db.flush()
    router = Router(
        name=name,
        hostname=name,
        management_ip="192.0.2.1",
        rest_api_username="api-user",
        rest_api_password="api-pass",
        network_device_id=node.id,
        is_active=True,
    )
    db.add(router)
    db.flush()
    return node, router


def _plain(db: Session, name: str) -> NetworkDevice:
    node = NetworkDevice(name=name, source="pytest", is_active=True)
    db.add(node)
    db.flush()
    return node


@pytest.mark.parametrize("timeout", [False, True])
def test_real_read_transaction_ends_before_router_call(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    source = snapshot()
    backend: list[int] = []

    def read(db: Session, *, query: LldpReadQuery) -> LldpSnapshot:
        backend.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
        assert db.in_transaction()
        return source

    def network(router: lldp_poller.LldpRouter) -> list[dict[str, str]]:
        with engine.connect() as observer:
            row = observer.execute(
                text("SELECT state, xact_start FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": backend[0]},
            ).one()
            # The pool may hand the observer the released backend. It can be
            # active here, but must never retain the earlier idle transaction.
            assert row.state != "idle in transaction"
        if timeout:
            raise TimeoutError("router timeout after read session closed")
        return []

    monkeypatch.setattr(
        topology_lldp.db_session_adapter, "create_session", lambda: Session(engine)
    )
    monkeypatch.setattr(lldp_poller, "read_snapshot", read)
    monkeypatch.setattr(lldp_poller, "_read_neighbors", network)
    monkeypatch.setattr(
        lldp_poller, "reconcile_poll", lambda db, *, command: command.poll.stats
    )
    monkeypatch.setattr(topology_lldp, "store_task_stats", MagicMock())
    result = topology_lldp.run_lldp_topology_poll()
    assert result["routers_polled"] == int(not timeout)
    assert result["routers_failed"] == int(timeout)


def test_reconciliation_locks_inventory_and_topology(
    engine: Engine,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = uuid4().hex
    _router_node(db_session, f"local-{label}")
    _plain(db_session, f"remote-{label}")
    db_session.commit()
    source = lldp_poller.read_snapshot(
        db_session, query=LldpReadQuery(datetime.now(UTC))
    )
    db_session.rollback()
    poll = lldp_poller.poll_all(
        snapshot=source, read_neighbors=lambda _: [{"identity": f"remote-{label}"}]
    )
    record = lldp_poller.create_event_record

    def verify_locks(db: Session, event: Event, *, status: EventStatus) -> EventStore:
        for table in (
            "routers",
            "jump_hosts",
            "network_devices",
            "network_topology_links",
        ):
            with engine.connect() as competing:
                with pytest.raises(OperationalError):
                    competing.execute(
                        text(f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE NOWAIT")
                    )
                competing.rollback()
        return record(db, event=event, status=status)

    monkeypatch.setattr(lldp_poller, "create_event_record", verify_locks)
    result = lldp_poller.reconcile_poll(
        db_session,
        command=ReconcileLldpCommand(
            CommandContext.system(
                actor="test:lldp", scope="network", reason="lock acceptance"
            ),
            poll,
        ),
    )
    assert isinstance(result, LldpStats)
    assert result.created == 1
