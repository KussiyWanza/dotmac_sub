"""Typed owner for subscription-scoped live bandwidth observations."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

import redis.asyncio as async_redis
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import finish_read_transaction
from app.models.bandwidth import BandwidthSample
from app.services import app_cache
from app.services.bandwidth import live_event_payload
from app.services.db_session_adapter import db_session_adapter
from app.services.metrics_store import get_metrics_store
from app.services.nas._mikrotik import (
    MikrotikLiveBandwidthTarget,
    get_mikrotik_pppoe_live_bandwidth,
)
from app.services.network.identity import (
    LiveBandwidthNetworkIdentity,
    live_bandwidth_identity_for_subscription,
)

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_FRESHNESS_SECONDS = 120
DEFAULT_STREAM_INTERVAL_SECONDS = 1.0
DEFAULT_EXTERNAL_REFRESH_SECONDS = 5.0
DEFAULT_EXTERNAL_TIMEOUT_SECONDS = 2.0
DIRECT_PROBE_LOCK_SECONDS = 300


class LiveBandwidthSource(StrEnum):
    VICTORIAMETRICS = "victoriametrics"
    POSTGRES_SAMPLE = "postgres_sample"
    MIKROTIK_ROUTEROS_API = "mikrotik_routeros_api"
    UNAVAILABLE = "unavailable"


class LiveBandwidthError(Exception):
    """Stable domain error mapped by HTTP adapters."""

    code = "network.live_bandwidth_observations.error"


class LiveBandwidthNotFound(LiveBandwidthError):
    code = "network.live_bandwidth_observations.subscription_not_found"


class LiveBandwidthAccessDenied(LiveBandwidthError):
    code = "network.live_bandwidth_observations.access_denied"


class LiveBandwidthConfigurationError(LiveBandwidthError):
    code = "network.live_bandwidth_observations.invalid_nas_configuration"


class LiveBandwidthProbeBusy(LiveBandwidthError):
    code = "network.live_bandwidth_observations.probe_in_progress"


class LiveBandwidthCoordinationUnavailable(LiveBandwidthError):
    code = "network.live_bandwidth_observations.coordination_unavailable"


@dataclass(frozen=True)
class LiveBandwidthAccess:
    roles: frozenset[str]
    principal_type: str | None
    owner_id: str | None

    @classmethod
    def from_principal(cls, principal: Mapping[str, object]) -> LiveBandwidthAccess:
        raw_roles = principal.get("roles")
        roles = (
            frozenset(str(role) for role in raw_roles)
            if isinstance(raw_roles, (list, tuple, set, frozenset))
            else frozenset()
        )
        role = principal.get("role")
        if isinstance(role, str):
            roles = roles | {role}
        owner = (
            principal.get("account_id")
            or principal.get("subscriber_id")
            or principal.get("principal_id")
        )
        principal_type = principal.get("principal_type")
        return cls(
            roles=roles,
            principal_type=(
                str(principal_type) if principal_type is not None else None
            ),
            owner_id=str(owner) if owner is not None else None,
        )


@dataclass(frozen=True)
class LiveBandwidthReadQuery:
    subscription_id: UUID
    access: LiveBandwidthAccess


@dataclass(frozen=True)
class LiveBandwidthStreamQuery:
    subscription_id: UUID
    sample_freshness_seconds: int = DEFAULT_SAMPLE_FRESHNESS_SECONDS
    interval_seconds: float = DEFAULT_STREAM_INTERVAL_SECONDS
    refresh_interval_seconds: float = DEFAULT_EXTERNAL_REFRESH_SECONDS
    external_timeout_seconds: float = DEFAULT_EXTERNAL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.sample_freshness_seconds < 1:
            raise ValueError("sample_freshness_seconds must be positive")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        if self.external_timeout_seconds <= 0:
            raise ValueError("external_timeout_seconds must be positive")


@dataclass(frozen=True)
class _StreamReading:
    rx_bps: float
    tx_bps: float
    observed_at: datetime
    source: LiveBandwidthSource


class LiveBandwidthProbeObservation(BaseModel):
    """PII-minimized public outcome of a direct operator diagnostic."""

    online: bool
    available: bool = True
    source: LiveBandwidthSource = LiveBandwidthSource.MIKROTIK_ROUTEROS_API
    nas_device_id: UUID
    nas_device_name: str
    timestamp: datetime
    current_rx_bps: float
    current_tx_bps: float
    download_bps: float
    upload_bps: float
    error: str | None = None


class _ProbeLockClient(Protocol):
    def set(self, key: str, value: str, *, nx: bool, ex: int) -> object: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...


def _authorized_subscription(
    db: Session,
    query: LiveBandwidthReadQuery,
) -> LiveBandwidthNetworkIdentity:
    identity = live_bandwidth_identity_for_subscription(db, query.subscription_id)
    if identity is None:
        raise LiveBandwidthNotFound
    if "admin" in query.access.roles or query.access.principal_type == "system_user":
        return identity
    if query.access.owner_id and str(identity.subscriber_id) == query.access.owner_id:
        return identity
    raise LiveBandwidthAccessDenied


def authorize_live_bandwidth_read(
    db: Session,
    query: LiveBandwidthReadQuery,
) -> None:
    """Validate access without exposing an ORM entity across the public boundary."""
    _authorized_subscription(db, query)


def _claim_direct_probe(nas_device_id: UUID) -> tuple[_ProbeLockClient, str]:
    raw_client = app_cache.get_cache_redis()
    if raw_client is None:
        raise LiveBandwidthCoordinationUnavailable
    client = cast(_ProbeLockClient, raw_client)
    key = app_cache.cache_key("network", "live_bandwidth_probe", "nas", nas_device_id)
    token = secrets.token_urlsafe(24)
    try:
        claimed = client.set(key, token, nx=True, ex=DIRECT_PROBE_LOCK_SECONDS)
    except RedisError as exc:
        logger.warning("live_bandwidth_probe_claim_failed error=%s", type(exc).__name__)
        raise LiveBandwidthCoordinationUnavailable from exc
    if not claimed:
        raise LiveBandwidthProbeBusy
    return client, token


def _release_direct_probe(
    client: _ProbeLockClient, nas_device_id: UUID, token: str
) -> None:
    key = app_cache.cache_key("network", "live_bandwidth_probe", "nas", nas_device_id)
    compare_and_delete = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        client.eval(compare_and_delete, 1, key, token)
    except RedisError as exc:
        logger.warning(
            "live_bandwidth_probe_release_failed error=%s", type(exc).__name__
        )


def _probe_observation(raw: Mapping[str, object]) -> LiveBandwidthProbeObservation:
    def observation_float(value: object) -> float:
        if not isinstance(value, (int, float, str)):
            return 0.0
        try:
            return float(value or 0)
        except ValueError:
            return 0.0

    return LiveBandwidthProbeObservation(
        online=bool(raw.get("online", False)),
        available=bool(raw.get("available", True)),
        nas_device_id=UUID(str(raw["nas_device_id"])),
        nas_device_name=str(raw.get("nas_device_name") or "Unknown NAS"),
        timestamp=datetime.fromisoformat(str(raw["timestamp"])),
        current_rx_bps=observation_float(raw.get("current_rx_bps")),
        current_tx_bps=observation_float(raw.get("current_tx_bps")),
        download_bps=observation_float(raw.get("download_bps")),
        upload_bps=observation_float(raw.get("upload_bps")),
        error=str(raw["error"]) if raw.get("error") else None,
    )


def probe_live_bandwidth(
    db: Session,
    query: LiveBandwidthReadQuery,
    *,
    transport: Callable[
        [MikrotikLiveBandwidthTarget], Mapping[str, object]
    ] = get_mikrotik_pppoe_live_bandwidth,
) -> LiveBandwidthProbeObservation:
    """Run one admitted diagnostic without holding a database transaction."""
    identity = _authorized_subscription(db, query)
    target = identity.target
    if target is None:
        raise LiveBandwidthConfigurationError

    # Materialization is complete. Returning the clean read transaction releases
    # its pooled connection before Redis admission or RouterOS network I/O.
    finish_read_transaction(db)
    client, token = _claim_direct_probe(target.device_id)
    try:
        return _probe_observation(transport(target))
    finally:
        _release_direct_probe(client, target.device_id, token)


def _latest_postgres_reading(
    subscription_id: UUID,
    cutoff: datetime,
) -> _StreamReading | None:
    """Read one recent sample in a worker thread, never on the ASGI event loop."""

    with db_session_adapter.read_session() as sse_db:
        latest = (
            sse_db.query(BandwidthSample)
            .filter(
                BandwidthSample.subscription_id == subscription_id,
                BandwidthSample.sample_at >= cutoff,
            )
            .order_by(BandwidthSample.sample_at.desc())
            .first()
        )
        if latest is None:
            return None
        return _StreamReading(
            rx_bps=float(latest.rx_bps or 0),
            tx_bps=float(latest.tx_bps or 0),
            observed_at=latest.sample_at,
            source=LiveBandwidthSource.POSTGRES_SAMPLE,
        )


async def live_bandwidth_events(
    query: LiveBandwidthStreamQuery,
    *,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[dict[str, str]]:
    """Yield bounded, cached observations without blocking the ASGI event loop."""

    metrics_store = get_metrics_store()
    redis_client: async_redis.Redis | None = None
    try:
        redis_client = async_redis.from_url(settings.redis_url)
    except Exception as exc:
        logger.debug(
            "active_viewer_redis_init_failed error_type=%s", type(exc).__name__
        )

    last_good: _StreamReading | None = None
    next_refresh_at = 0.0
    try:
        while not await is_disconnected():
            now = datetime.now(UTC)
            cutoff = now - timedelta(seconds=query.sample_freshness_seconds)
            reading = (
                last_good
                if last_good is not None and last_good.observed_at >= cutoff
                else None
            )
            refresh_due = time.monotonic() >= next_refresh_at

            if refresh_due:
                next_refresh_at = time.monotonic() + query.refresh_interval_seconds
                if redis_client is not None:
                    try:
                        await asyncio.wait_for(
                            redis_client.zadd(
                                settings.bandwidth_active_viewers_key,
                                {str(query.subscription_id): time.time()},
                            ),
                            timeout=query.external_timeout_seconds,
                        )
                    except Exception as exc:
                        logger.debug(
                            "active_viewer_heartbeat_failed error_type=%s",
                            type(exc).__name__,
                        )

                candidate: _StreamReading | None = None
                try:
                    observation = await asyncio.wait_for(
                        metrics_store.get_current_bandwidth_observation(
                            str(query.subscription_id)
                        ),
                        timeout=query.external_timeout_seconds,
                    )
                    if (
                        observation.has_sample
                        and observation.observed_at is not None
                        and observation.observed_at >= cutoff
                    ):
                        candidate = _StreamReading(
                            rx_bps=observation.rx_bps,
                            tx_bps=observation.tx_bps,
                            observed_at=observation.observed_at,
                            source=LiveBandwidthSource.VICTORIAMETRICS,
                        )
                except Exception as exc:
                    logger.debug(
                        "live_bandwidth_metrics_query_failed error_type=%s",
                        type(exc).__name__,
                    )

                if candidate is None:
                    try:
                        candidate = await asyncio.wait_for(
                            asyncio.to_thread(
                                _latest_postgres_reading,
                                query.subscription_id,
                                cutoff,
                            ),
                            timeout=query.external_timeout_seconds,
                        )
                    except Exception as exc:
                        logger.debug(
                            "live_bandwidth_db_fallback_failed error_type=%s",
                            type(exc).__name__,
                        )

                if candidate is not None:
                    last_good = candidate
                    reading = candidate

            if reading is None:
                current = {"rx_bps": 0.0, "tx_bps": 0.0}
                observed_at = now
                source = LiveBandwidthSource.UNAVAILABLE
                has_sample = False
            else:
                current = {"rx_bps": reading.rx_bps, "tx_bps": reading.tx_bps}
                observed_at = reading.observed_at
                source = reading.source
                has_sample = True

            payload = live_event_payload(
                current,
                observed_at,
                has_sample=has_sample,
            )
            payload["source"] = source.value
            yield {"event": "bandwidth", "data": json.dumps(payload)}
            await asyncio.sleep(max(0.1, query.interval_seconds))
    finally:
        if redis_client is not None:
            try:
                await asyncio.wait_for(
                    redis_client.aclose(),
                    timeout=query.external_timeout_seconds,
                )
            except Exception as exc:
                logger.debug(
                    "active_viewer_redis_cleanup_failed error_type=%s",
                    type(exc).__name__,
                )
