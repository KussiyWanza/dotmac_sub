"""Regression tests for live-bandwidth SSE DB session boundaries."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4


class _Request:
    async def is_disconnected(self) -> bool:
        return True


class _Session:
    def __init__(self) -> None:
        self.rolled_back = False
        self.committed = False
        self.closed = False
        self.expire_on_commit = True
        self.new: set[object] = set()
        self.dirty: set[object] = set()
        self.deleted: set[object] = set()

    def in_transaction(self) -> bool:
        return True

    def in_nested_transaction(self) -> bool:
        return False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_admin_live_bandwidth_releases_request_db_before_stream(monkeypatch):
    from app.api import bandwidth as bandwidth_api

    db = _Session()
    subscription_id = uuid4()
    checked: dict[str, object] = {}

    def check_access(session, query):
        checked["session"] = session
        checked["query"] = query

    monkeypatch.setattr(bandwidth_api, "authorize_live_bandwidth_read", check_access)

    response = bandwidth_api.get_live_bandwidth(
        subscription_id=subscription_id,
        request=_Request(),
        db=db,
        current_user={"role": "admin"},
    )

    assert response is not None
    assert checked["session"] is db
    assert checked["query"].subscription_id == subscription_id
    assert checked["query"].access.roles == frozenset({"admin"})
    assert db.committed is True
    assert db.rolled_back is False
    assert db.closed is True


def test_customer_live_bandwidth_releases_request_db_before_stream(monkeypatch):
    from app.web.customer import routes

    db = _Session()
    subscription_id = uuid4()
    request = _Request()

    monkeypatch.setattr(
        routes,
        "get_current_customer_from_request",
        lambda req, session: SimpleNamespace(id=uuid4()),
    )
    monkeypatch.setattr(
        routes,
        "resolve_customer_subscription",
        lambda session, customer: SimpleNamespace(id=subscription_id),
    )

    response = routes.customer_bandwidth_live(request=request, db=db)

    assert response is not None
    assert db.committed is True
    assert db.rolled_back is False
    assert db.closed is True


def test_live_bandwidth_stream_bounds_slow_dependencies_and_offloads_db(monkeypatch):
    from app.services.network import live_bandwidth_observations as observations

    class SlowMetricsStore:
        async def get_current_bandwidth_observation(self, _subscription_id):
            await asyncio.sleep(1)
            raise AssertionError("timeout should cancel the metrics read")

    worker_threads: list[int] = []

    def no_postgres_sample(_subscription_id, _cutoff):
        worker_threads.append(threading.get_ident())
        return None

    monkeypatch.setattr(observations, "get_metrics_store", SlowMetricsStore)
    monkeypatch.setattr(
        observations.async_redis,
        "from_url",
        lambda _url: (_ for _ in ()).throw(RuntimeError("redis unavailable")),
    )
    monkeypatch.setattr(
        observations,
        "_latest_postgres_reading",
        no_postgres_sample,
    )

    async def connected() -> bool:
        return False

    async def read_one():
        stream = observations.live_bandwidth_events(
            observations.LiveBandwidthStreamQuery(
                subscription_id=uuid4(),
                interval_seconds=0.01,
                refresh_interval_seconds=5,
                external_timeout_seconds=0.01,
            ),
            is_disconnected=connected,
        )
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(read_one())
    payload = json.loads(event["data"])

    assert payload["source"] == "unavailable"
    assert worker_threads
    assert worker_threads[0] != threading.get_ident()


def test_live_bandwidth_stream_reuses_fresh_reading_between_refreshes(monkeypatch):
    from app.services.network import live_bandwidth_observations as observations

    class MetricsStore:
        def __init__(self) -> None:
            self.calls = 0

        async def get_current_bandwidth_observation(self, _subscription_id):
            self.calls += 1
            return SimpleNamespace(
                rx_bps=1200.0,
                tx_bps=340.0,
                has_sample=True,
                observed_at=datetime.now(UTC),
            )

    metrics_store = MetricsStore()
    monkeypatch.setattr(observations, "get_metrics_store", lambda: metrics_store)
    monkeypatch.setattr(
        observations.async_redis,
        "from_url",
        lambda _url: (_ for _ in ()).throw(RuntimeError("redis unavailable")),
    )

    async def connected() -> bool:
        return False

    async def read_two():
        stream = observations.live_bandwidth_events(
            observations.LiveBandwidthStreamQuery(
                subscription_id=uuid4(),
                interval_seconds=0.01,
                refresh_interval_seconds=30,
                external_timeout_seconds=0.1,
            ),
            is_disconnected=connected,
        )
        first = await anext(stream)
        second = await anext(stream)
        await stream.aclose()
        return first, second

    first, second = asyncio.run(read_two())

    assert metrics_store.calls == 1
    assert json.loads(first["data"])["source"] == "victoriametrics"
    assert json.loads(second["data"])["source"] == "victoriametrics"
