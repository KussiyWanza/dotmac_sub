"""
VictoriaMetrics client for bandwidth metrics storage.

Provides methods for writing bandwidth samples and querying time series data
using VictoriaMetrics' Prometheus-compatible API.
"""

import logging
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")
_DEFAULT_TIMEOUT = 30.0  # fallback when settings unavailable


# VictoriaMetrics rejects a query_range whose point count (range / step) exceeds
# -search.maxPointsPerTimeseries (default 30000) with HTTP 422. Target well under
# that so wide windows (e.g. 24h at a 1s step = 86,400 points) are auto-coarsened.
_VM_MAX_POINTS = 10_000


def _parse_step_seconds(step: str) -> int:
    """Parse a PromQL step ('1s'/'30s'/'1m'/'5m'/'1h'/'1d' or bare seconds)."""
    text = str(step).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        if text and text[-1] in units:
            return max(1, int(float(text[:-1]) * units[text[-1]]))
        return max(1, int(float(text)))
    except ValueError:
        return 60


def _clamp_step_to_points(
    start: datetime, end: datetime, step: str, max_points: int = _VM_MAX_POINTS
) -> str:
    """Coarsen ``step`` so (range / step) stays under VictoriaMetrics' point cap.

    Callers may pass a fine step suited to raw storage (e.g. '1s'); over a wide
    range that would exceed maxPointsPerTimeseries and 422. Bump the step up to
    the coarser of (requested, range/max_points); never finer than requested.
    """
    range_seconds = max(1, int((end - start).total_seconds()))
    step_seconds = _parse_step_seconds(step)
    if range_seconds // step_seconds <= max_points:
        return step
    safe = max(step_seconds, math.ceil(range_seconds / max_points))
    return f"{safe}s"


@dataclass
class BandwidthPoint:
    """A single bandwidth measurement point."""

    timestamp: datetime
    subscription_id: str
    nas_device_id: str | None
    rx_bps: int
    tx_bps: int


@dataclass(frozen=True)
class CurrentBandwidthObservation:
    """Current rates that distinguish an idle zero from an absent series."""

    rx_bps: float
    tx_bps: float
    has_sample: bool
    observed_at: datetime | None


@dataclass
class TimeSeriesPoint:
    """A single point in a time series query result."""

    timestamp: datetime
    value: float


@dataclass
class TimeSeriesResult:
    """Result from a time series query."""

    metric: dict[str, str]
    values: list[TimeSeriesPoint]


class MetricsStoreError(Exception):
    """Base exception for metrics store errors."""

    pass


class MetricsStore:
    """
    VictoriaMetrics client for bandwidth metrics.

    Uses Prometheus remote write format for ingestion and PromQL for queries.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = base_url or VICTORIAMETRICS_URL
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_timeout(self) -> float:
        """Get the HTTP timeout, using configurable settings."""
        if self._timeout is not None:
            return self._timeout
        # Try to get from settings; if DB-backed settings aren't available in this
        # context, fall back to environment/default timeout.
        timeout_obj = None
        try:
            timeout_obj = resolve_value(
                None, SettingDomain.bandwidth, "victoriametrics_timeout_seconds"
            )
        except Exception:
            timeout_obj = os.getenv("VICTORIAMETRICS_TIMEOUT_SECONDS")
        try:
            return (
                float(str(timeout_obj)) if timeout_obj is not None else _DEFAULT_TIMEOUT
            )
        except (TypeError, ValueError):
            return _DEFAULT_TIMEOUT

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._get_timeout())
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _to_prometheus_line(self, point: BandwidthPoint) -> str:
        """
        Convert a bandwidth point to Prometheus line protocol format.
        """
        labels = f'subscription_id="{point.subscription_id}"'
        if point.nas_device_id:
            labels += f',nas_device_id="{point.nas_device_id}"'

        timestamp_ms = int(point.timestamp.timestamp() * 1000)

        lines = [
            f"bandwidth_rx_bps{{{labels}}} {point.rx_bps} {timestamp_ms}",
            f"bandwidth_tx_bps{{{labels}}} {point.tx_bps} {timestamp_ms}",
        ]
        return "\n".join(lines)

    async def write_samples(self, points: list[BandwidthPoint]) -> bool:
        """
        Write bandwidth samples to VictoriaMetrics.

        Args:
            points: List of bandwidth measurement points

        Returns:
            True if write was successful
        """
        if not points:
            return True

        client = await self._get_client()
        lines = "\n".join(self._to_prometheus_line(p) for p in points)

        try:
            response = await client.post(
                f"{self.base_url}/api/v1/import/prometheus",
                content=lines,
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to write samples to VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Write failed: {e}") from e

    async def write_aggregates(
        self,
        subscription_id: str,
        nas_device_id: str | None,
        timestamp: datetime,
        rx_avg: float,
        tx_avg: float,
        rx_max: float,
        tx_max: float,
        sample_count: int,
    ) -> bool:
        """
        Write pre-aggregated bandwidth data to VictoriaMetrics.

        This is used for pushing aggregates from the Celery worker.
        """
        client = await self._get_client()

        labels = f'subscription_id="{subscription_id}"'
        if nas_device_id:
            labels += f',nas_device_id="{nas_device_id}"'

        timestamp_ms = int(timestamp.timestamp() * 1000)

        lines = [
            f"bandwidth_rx_bps_avg{{{labels}}} {rx_avg} {timestamp_ms}",
            f"bandwidth_tx_bps_avg{{{labels}}} {tx_avg} {timestamp_ms}",
            f"bandwidth_rx_bps_max{{{labels}}} {rx_max} {timestamp_ms}",
            f"bandwidth_tx_bps_max{{{labels}}} {tx_max} {timestamp_ms}",
            f"bandwidth_sample_count{{{labels}}} {sample_count} {timestamp_ms}",
        ]

        try:
            response = await client.post(
                f"{self.base_url}/api/v1/import/prometheus",
                content="\n".join(lines),
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to write aggregates to VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Write failed: {e}") from e

    async def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step: str = "1m",
    ) -> list[TimeSeriesResult]:
        """
        Execute a PromQL range query.

        Args:
            query: PromQL query string
            start: Start time
            end: End time
            step: Query resolution (e.g., "1m", "5m", "1h")

        Returns:
            List of time series results
        """
        client = await self._get_client()

        # Coarsen the step if it would blow past VictoriaMetrics' point cap (e.g.
        # a 1s step over 24h), which otherwise 422s and silently breaks the chart.
        step = _clamp_step_to_points(start, end, step)

        params = {
            "query": query,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step": step,
        }

        try:
            response = await client.get(
                f"{self.base_url}/api/v1/query_range",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                raise MetricsStoreError(
                    f"Query failed: {data.get('error', 'Unknown error')}"
                )

            results = []
            for result in data.get("data", {}).get("result", []):
                values = [
                    TimeSeriesPoint(
                        timestamp=datetime.fromtimestamp(ts, tz=UTC),
                        value=float(val),
                    )
                    for ts, val in result.get("values", [])
                ]
                results.append(
                    TimeSeriesResult(
                        metric=result.get("metric", {}),
                        values=values,
                    )
                )

            return results

        except httpx.HTTPError as e:
            logger.error(f"Failed to query VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Query failed: {e}") from e

    async def get_instant(self, query: str) -> list[dict[str, Any]]:
        """
        Execute an instant PromQL query (current value).

        Args:
            query: PromQL query string

        Returns:
            List of metric results with current values
        """
        client = await self._get_client()

        try:
            response = await client.get(
                f"{self.base_url}/api/v1/query",
                params={"query": query},
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise MetricsStoreError("Query failed: invalid response payload")
            if data.get("status") != "success":
                raise MetricsStoreError(
                    f"Query failed: {data.get('error', 'Unknown error')}"
                )

            data_obj = data.get("data")
            if not isinstance(data_obj, dict):
                return []
            result_obj = data_obj.get("result")
            if not isinstance(result_obj, list):
                return []
            return cast(list[dict[str, Any]], result_obj)

        except httpx.HTTPError as e:
            logger.error(f"Failed to query VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Query failed: {e}") from e

    async def get_subscription_bandwidth(
        self,
        subscription_id: str,
        start: datetime,
        end: datetime,
        step: str = "1m",
        aggregate: str = "avg",
    ) -> dict[str, list[TimeSeriesPoint]]:
        """
        Get bandwidth time series for a specific subscription.

        Reads the per-window aggregate series written by the bandwidth worker
        (``bandwidth_rx_bps_avg`` / ``bandwidth_rx_bps_max``) — the raw
        unsuffixed ``bandwidth_rx_bps`` series is no longer ingested.
        ``aggregate`` selects which: ``"avg"`` for the throughput chart,
        ``"max"`` for the peak fallback.

        Args:
            subscription_id: The subscription UUID
            start: Start time
            end: End time
            step: Query resolution
            aggregate: ``"avg"`` or ``"max"`` — which aggregate series to read

        Returns:
            Dict with 'rx' and 'tx' keys containing time series points
        """
        rx_query = (
            f'bandwidth_rx_bps_{aggregate}{{subscription_id="{subscription_id}"}}'
        )
        tx_query = (
            f'bandwidth_tx_bps_{aggregate}{{subscription_id="{subscription_id}"}}'
        )

        rx_results = await self.query_range(rx_query, start, end, step)
        tx_results = await self.query_range(tx_query, start, end, step)

        return {
            "rx": rx_results[0].values if rx_results else [],
            "tx": tx_results[0].values if tx_results else [],
        }

    async def get_current_bandwidth(
        self,
        subscription_id: str,
    ) -> dict[str, float]:
        """
        Get current bandwidth for a subscription.

        Args:
            subscription_id: The subscription UUID

        Returns:
            Dict with rx_bps and tx_bps values
        """
        observation = await self.get_current_bandwidth_observation(subscription_id)
        return {"rx_bps": observation.rx_bps, "tx_bps": observation.tx_bps}

    async def get_current_bandwidth_observation(
        self,
        subscription_id: str,
    ) -> CurrentBandwidthObservation:
        """Return rates plus source presence and the latest sample timestamp."""
        rx_query = f'bandwidth_rx_bps_avg{{subscription_id="{subscription_id}"}}'
        tx_query = f'bandwidth_tx_bps_avg{{subscription_id="{subscription_id}"}}'
        rx_results = await self.get_instant(rx_query)
        tx_results = await self.get_instant(tx_query)

        rx_value = rx_results[0].get("value") if rx_results else None
        tx_value = tx_results[0].get("value") if tx_results else None
        rx_bps = float(rx_value[1]) if isinstance(rx_value, list) else 0.0
        tx_bps = float(tx_value[1]) if isinstance(tx_value, list) else 0.0
        timestamps = [
            datetime.fromtimestamp(float(value[0]), tz=UTC)
            for value in (rx_value, tx_value)
            if isinstance(value, list) and value
        ]
        return CurrentBandwidthObservation(
            rx_bps=rx_bps,
            tx_bps=tx_bps,
            has_sample=bool(rx_value or tx_value),
            observed_at=max(timestamps) if timestamps else None,
        )

    async def get_peak_bandwidth(
        self,
        subscription_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, float]:
        """
        Get peak bandwidth for a subscription within a time range.

        Reads the per-window ``bandwidth_rx_bps_max`` aggregate written by the
        bandwidth worker (the raw unsuffixed series is no longer ingested).

        Prefers an exact ``max_over_time`` instant query. That query can return
        nothing on a long lookbehind window (VictoriaMetrics rejects an
        over-long ``[duration]`` rollup, or the data predates the instant
        evaluation's staleness window), so when it yields no peak we fall back
        to the max of the *range* ``_max`` series, which is known to return
        data over multi-day windows.
        """
        duration = int((end - start).total_seconds())
        rx_query = f'max_over_time(bandwidth_rx_bps_max{{subscription_id="{subscription_id}"}}[{duration}s])'
        tx_query = f'max_over_time(bandwidth_tx_bps_max{{subscription_id="{subscription_id}"}}[{duration}s])'

        rx_peak = 0.0
        tx_peak = 0.0
        try:
            rx_results = await self.get_instant(rx_query)
            tx_results = await self.get_instant(tx_query)
            rx_peak = max(
                (float(r["value"][1]) for r in rx_results if r.get("value")),
                default=0.0,
            )
            tx_peak = max(
                (float(r["value"][1]) for r in tx_results if r.get("value")),
                default=0.0,
            )
        except Exception as exc:  # pragma: no cover - metrics store dependent
            logger.warning(
                "peak instant query failed, falling back to range series: %s", exc
            )

        if rx_peak <= 0 and tx_peak <= 0:
            series = await self.get_subscription_bandwidth(
                subscription_id, start, end, step="1m", aggregate="max"
            )
            rx_peak = max((p.value for p in series.get("rx", [])), default=0.0)
            tx_peak = max((p.value for p in series.get("tx", [])), default=0.0)

        return {"rx_peak_bps": rx_peak, "tx_peak_bps": tx_peak}

    async def get_total_bytes(
        self,
        subscription_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, float]:
        """
        Get total bytes transferred for a subscription within a time range.
        """
        duration = int((end - start).total_seconds())
        # Integrate the average-bps gauge over the window to estimate bytes:
        # mean(bps) * seconds / 8. (The unsuffixed counter-style series this
        # once used ``increase()`` on is no longer ingested.)
        rx_query = f'sum(avg_over_time(bandwidth_rx_bps_avg{{subscription_id="{subscription_id}"}}[{duration}s])) * {duration} / 8'
        tx_query = f'sum(avg_over_time(bandwidth_tx_bps_avg{{subscription_id="{subscription_id}"}}[{duration}s])) * {duration} / 8'

        rx_results = await self.get_instant(rx_query)
        tx_results = await self.get_instant(tx_query)

        rx_bytes = 0.0
        tx_bytes = 0.0

        if rx_results and rx_results[0].get("value"):
            rx_bytes = float(rx_results[0]["value"][1])
        if tx_results and tx_results[0].get("value"):
            tx_bytes = float(tx_results[0]["value"][1])

        return {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}

    async def get_top_users(
        self,
        limit: int = 10,
        duration: str = "1h",
    ) -> list[dict[str, Any]]:
        """
        Get top bandwidth consumers.

        Args:
            limit: Number of top users to return
            duration: Time window for calculation

        Returns:
            List of top users with subscription_id and bandwidth
        """
        query = (
            f"topk({limit}, sum by (subscription_id) ("
            f"avg_over_time(bandwidth_rx_bps_avg[{duration}]) + "
            f"avg_over_time(bandwidth_tx_bps_avg[{duration}])))"
        )

        results = await self.get_instant(query)

        return [
            {
                "subscription_id": r.get("metric", {}).get("subscription_id"),
                "total_bps": float(r.get("value", [0, 0])[1]),
            }
            for r in results
        ]

    async def health_check(self) -> bool:
        """Check if VictoriaMetrics is healthy."""
        try:
            client = await self._get_client()
            response = await client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except Exception:
            return False


# Singleton instance
_metrics_store: MetricsStore | None = None


def get_metrics_store() -> MetricsStore:
    """Get or create the metrics store singleton."""
    global _metrics_store
    if _metrics_store is None:
        _metrics_store = MetricsStore()
    return _metrics_store
