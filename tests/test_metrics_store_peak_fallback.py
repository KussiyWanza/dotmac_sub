"""Peak-bandwidth range fallback.

The exact ``max_over_time`` instant query can return nothing on a long
lookbehind window (VictoriaMetrics rejects an over-long ``[duration]`` rollup,
or the data falls outside the instant evaluation's staleness window). When that
happens ``get_peak_bandwidth`` must fall back to the max of the *range* series
— the same query path the chart uses — so the "Peak" tile shows a real figure
instead of a blank when throughput data exists.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

from app.services.metrics_store import MetricsStore, TimeSeriesPoint


def _window(days=24):
    end = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    return end - timedelta(days=days), end


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result = []
    error = []

    def runner():
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            error.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def test_instant_peak_used_when_present():
    store = MetricsStore(base_url="http://vm.test")

    async def fake_instant(query):
        # Both rx and tx return an instant value.
        return [{"value": [0, "5000000" if "rx" in query else "1000000"]}]

    async def fake_range(*a, **k):  # pragma: no cover - must not be needed
        raise AssertionError("range fallback should not run when instant works")

    store.get_instant = fake_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = fake_range  # type: ignore[assignment]

    start, end = _window()
    peak = _run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 5000000.0, "tx_peak_bps": 1000000.0}


def test_range_fallback_when_instant_empty():
    store = MetricsStore(base_url="http://vm.test")

    async def empty_instant(query):
        return []  # the long-window instant query yields nothing

    async def fake_range(subscription_id, start, end, step="1m", aggregate="avg"):
        return {
            "rx": [
                TimeSeriesPoint(timestamp=start, value=2_000_000.0),
                TimeSeriesPoint(timestamp=end, value=7_500_000.0),  # peak
            ],
            "tx": [
                TimeSeriesPoint(timestamp=start, value=900_000.0),
                TimeSeriesPoint(timestamp=end, value=1_200_000.0),  # peak
            ],
        }

    store.get_instant = empty_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = fake_range  # type: ignore[assignment]

    start, end = _window()
    peak = _run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 7_500_000.0, "tx_peak_bps": 1_200_000.0}


def test_range_fallback_when_instant_raises():
    store = MetricsStore(base_url="http://vm.test")

    async def boom_instant(query):
        raise RuntimeError("VM 422: too-long rollup window")

    async def fake_range(subscription_id, start, end, step="1m", aggregate="avg"):
        return {
            "rx": [TimeSeriesPoint(timestamp=start, value=3_300_000.0)],
            "tx": [TimeSeriesPoint(timestamp=start, value=800_000.0)],
        }

    store.get_instant = boom_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = fake_range  # type: ignore[assignment]

    start, end = _window()
    peak = _run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 3_300_000.0, "tx_peak_bps": 800_000.0}


def test_no_data_anywhere_returns_zero():
    store = MetricsStore(base_url="http://vm.test")

    async def empty_instant(query):
        return []

    async def empty_range(subscription_id, start, end, step="1m", aggregate="avg"):
        return {"rx": [], "tx": []}

    store.get_instant = empty_instant  # type: ignore[assignment]
    store.get_subscription_bandwidth = empty_range  # type: ignore[assignment]

    start, end = _window()
    peak = _run(store.get_peak_bandwidth("sub-1", start, end))
    assert peak == {"rx_peak_bps": 0.0, "tx_peak_bps": 0.0}


def test_reads_target_ingested_aggregate_series():
    """Reads must hit the suffixed aggregate series the worker actually writes
    (``_avg``/``_max``), not the dead unsuffixed ``bandwidth_rx_bps`` series —
    that mismatch is what left the Peak tile blank despite live throughput.
    """
    store = MetricsStore(base_url="http://vm.test")
    instant_queries: list[str] = []
    range_queries: list[str] = []

    async def capture_instant(query):
        instant_queries.append(query)
        return []  # force the peak range fallback

    async def capture_range(query, start, end, step):
        range_queries.append(query)
        return []

    store.get_instant = capture_instant  # type: ignore[assignment]
    store.query_range = capture_range  # type: ignore[assignment]

    start, end = _window()

    # Peak: instant uses _max, range fallback uses _max.
    _run(store.get_peak_bandwidth("sub-1", start, end))
    assert any("bandwidth_rx_bps_max" in q for q in instant_queries)
    assert any("bandwidth_rx_bps_max" in q for q in range_queries)
    assert any("bandwidth_tx_bps_max" in q for q in range_queries)
    assert not any(
        "bandwidth_rx_bps{" in q or "bandwidth_rx_bps[" in q for q in instant_queries
    )

    # Chart series defaults to the _avg aggregate.
    range_queries.clear()
    _run(store.get_subscription_bandwidth("sub-1", start, end))
    assert all(
        "bandwidth_rx_bps_avg" in q or "bandwidth_tx_bps_avg" in q
        for q in range_queries
    )

    # Current bandwidth reads the _avg aggregate too.
    instant_queries.clear()
    _run(store.get_current_bandwidth("sub-1"))
    assert all("_avg" in q for q in instant_queries)


def test_current_observation_distinguishes_idle_zero_from_missing_series():
    store = MetricsStore(base_url="http://vm.test")

    async def idle_instant(_query):
        return [{"value": [1_725_000_000, "0"]}]

    store.get_instant = idle_instant  # type: ignore[assignment]

    observation = _run(store.get_current_bandwidth_observation("sub-1"))

    assert observation.has_sample is True
    assert observation.rx_bps == 0
    assert observation.tx_bps == 0
    assert observation.observed_at is not None
