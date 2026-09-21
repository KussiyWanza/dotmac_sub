import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import app.poller.mikrotik_poller as mikrotik_poller
from app.poller.mikrotik_poller import (
    BandwidthPoller,
    DevicePool,
    MikroTikConnection,
    QueueStats,
    _sanitize_exc,
)


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


class _FakeDevicePool:
    def __init__(self, device_id, subscription_id):
        self.device_id = device_id
        self.subscription_id = subscription_id

    async def poll_all(self, active_devices=None):
        yield (
            self.device_id,
            [
                QueueStats(
                    name="<pppoe-100024890>",
                    target="",
                    rate_rx=6_000_000,
                    rate_tx=6_000_000,
                    bytes_rx=0,
                    bytes_tx=0,
                    packets_rx=0,
                    packets_tx=0,
                )
            ],
        )

    def resolve_subscription(self, device_id, queue_name):
        return self.subscription_id

    def resolve_speed(self, subscription_id):
        # No plan cap in this fixture -> no clamp applied.
        return (0, 0)


def test_mikrotik_queue_rates_are_stored_as_bits_per_second():
    device_id = uuid4()
    subscription_id = uuid4()
    poller = BandwidthPoller()
    poller.device_pool = _FakeDevicePool(device_id, subscription_id)
    published = []

    async def capture_samples(samples):
        published.extend(samples)

    poller._publish_samples = capture_samples

    _run_async(poller._poll_once())

    assert len(published) == 1
    assert published[0].rx_bps == 6_000_000
    assert published[0].tx_bps == 6_000_000


def test_mikrotik_connection_does_not_pass_unsupported_socket_timeout(monkeypatch):
    captured_kwargs = {}

    class _Pool:
        def __init__(self, host, **kwargs):
            captured_kwargs.update(kwargs)

        def get_api(self):
            return object()

    monkeypatch.setattr(mikrotik_poller, "RouterOsApiPool", _Pool)

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is True
    assert "socket_timeout" not in captured_kwargs


def test_connect_disconnects_pool_when_get_api_fails(monkeypatch):
    """Regression: a failure after the pool logs in must release the session.

    Constructing RouterOsApiPool opens the socket and logs in, so a session
    exists on the router. If get_api() then fails, connect() must disconnect the
    pool (exactly once) instead of orphaning the session — the production
    symptom was stale snap-api sessions accumulating on garki-core.
    """
    disconnect_calls = []

    class _Pool:
        def __init__(self, host, **kwargs):
            # Construction "succeeds": the API session now exists on the router.
            pass

        def get_api(self):
            raise RuntimeError("router busy: get_api timed out")

        def disconnect(self):
            disconnect_calls.append(True)

    monkeypatch.setattr(mikrotik_poller, "RouterOsApiPool", _Pool)

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is False
    # The established session was released, not leaked.
    assert disconnect_calls == [True]
    assert conn._pool is None
    assert conn._connection is None


def test_connect_success_does_not_disconnect(monkeypatch):
    """The success path returns True and must NOT disconnect the pool."""
    disconnect_calls = []

    class _Pool:
        def __init__(self, host, **kwargs):
            pass

        def get_api(self):
            return object()

        def disconnect(self):
            disconnect_calls.append(True)

    monkeypatch.setattr(mikrotik_poller, "RouterOsApiPool", _Pool)

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is True
    assert disconnect_calls == []
    assert conn._connection is not None
    assert conn._pool is not None


def test_connect_handles_none_pool(monkeypatch):
    """A None pool from construction is handled without crashing."""
    monkeypatch.setattr(
        mikrotik_poller, "RouterOsApiPool", lambda *args, **kwargs: None
    )

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is False
    assert conn._pool is None
    assert conn._connection is None


def test_connect_drains_pool_when_construction_times_out(monkeypatch):
    """Door #2: pool construction outruns the connect() timeout.

    When wait_for times out, the constructor thread keeps running and logs in,
    creating a session with no reference to release it. connect() must schedule
    a background drain that disconnects that late-born session so it is not
    orphaned on the router.
    """
    disconnect_calls = []

    class _Pool:
        def __init__(self, host, **kwargs):
            # Construction "succeeds" after the timeout -> session established.
            pass

        def get_api(self):
            return object()

        def disconnect(self):
            disconnect_calls.append(True)

    monkeypatch.setattr(mikrotik_poller, "RouterOsApiPool", _Pool)

    async def _timeout_wait_for(awaitable, timeout):
        # Simulate the construction wait_for timing out while the underlying
        # (shielded) executor future keeps running to completion.
        raise TimeoutError

    monkeypatch.setattr(mikrotik_poller.asyncio, "wait_for", _timeout_wait_for)

    async def _scenario():
        conn = MikroTikConnection(
            device_id=uuid4(),
            host="192.0.2.10",
            username="admin",
            password="secret",
        )
        result = await conn.connect()
        # Let the background drain finish releasing the late-born session.
        if conn._drain_tasks:
            await asyncio.gather(*list(conn._drain_tasks))
        return result, conn

    result, conn = _run_async(_scenario())
    assert result is False
    assert disconnect_calls == [True]
    assert conn._pool is None
    assert conn._connection is None


def test_sanitize_exc_names_blank_exceptions_and_redacts_password():
    assert _sanitize_exc(TimeoutError()) == "TimeoutError"
    assert (
        _sanitize_exc(RuntimeError("failure =password=secret "))
        == "failure =password=<redacted> "
    )


def test_persistently_unreachable_router_uses_bounded_long_backoff():
    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )
    conn._consecutive_failures = 20
    conn._last_attempt = datetime.now(UTC) - timedelta(seconds=60)

    assert conn.should_retry is False

    conn._last_attempt = datetime.now(UTC) - timedelta(
        seconds=mikrotik_poller.FAILURE_BACKOFF_MAX_SECONDS + 1
    )
    assert conn.should_retry is True


def test_device_pool_bounds_concurrent_routeros_calls():
    class _Connection:
        should_retry = True

        async def get_queue_stats(self):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return []

    async def _poll():
        pool = DevicePool()
        pool._last_refresh = datetime.now(UTC)
        pool._connections = {
            uuid4(): _Connection()
            for _ in range(mikrotik_poller.MAX_CONCURRENT_DEVICE_POLLS + 3)
        }
        return [item async for item in pool.poll_all()]

    running = 0
    peak = 0
    assert _run_async(_poll()) == []
    assert peak == mikrotik_poller.MAX_CONCURRENT_DEVICE_POLLS
