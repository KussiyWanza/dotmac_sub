"""
MikroTik Bandwidth Poller

High-frequency polling service that collects bandwidth samples from MikroTik
devices using the RouterOS API and publishes them to a Redis stream.
"""

import asyncio
import json
import logging
import os
import re
import signal
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import redis.asyncio as redis
from routeros_api import RouterOsApiPool

from app.config import settings
from app.models.catalog import (
    CatalogOffer,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
    Subscription,
    SubscriptionStatus,
)
from app.services.credential_crypto import decrypt_credential
from app.services.db_session_adapter import db_session_adapter
from app.services.poller_health import POLLER_HEALTH_KEY
from app.services.queue_mapping import queue_mapping

logger = logging.getLogger(__name__)

_PASSWORD_RE = re.compile(r"=password=[^\x00 ]*")


def _sanitize_exc(exc: BaseException) -> str:
    """Strip routeros_api's cleartext =password=... from exception text."""
    message = _PASSWORD_RE.sub("=password=<redacted>", str(exc))
    return message or type(exc).__name__


def _error_category(exc: BaseException) -> str:
    """Bounded operator-facing classification without leaking credentials."""
    message = _sanitize_exc(exc).lower()
    if "no route to host" in message:
        return "no_route_to_host"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if "authentication" in message or "invalid user" in message:
        return "authentication_rejected"
    if "connection refused" in message:
        return "connection_refused"
    if "network is unreachable" in message:
        return "network_unreachable"
    return "transport_error"


# Configuration
# Default cadence is 5s: 1s polling produces ~4k Redis ops/sec over WAN, which
# saturates the poller process and dwarfs any UI responsiveness benefit. Real
# customer dashboards display at ~5s granularity; tune via env if needed.
POLL_INTERVAL_MS = int(os.getenv("BANDWIDTH_POLL_INTERVAL_MS", "5000"))
# Reported-rate clamp tolerance: a transient reading up to this % of a queue's
# max-limit is kept as-is; above it is clamped to the cap (glitch absorption).
MAX_RATE_TOLERANCE = (
    int(os.getenv("BANDWIDTH_MAX_RATE_TOLERANCE_PERCENT", "105")) / 100.0
)
# Hard cap on any single device's blocking socket I/O. Without this a silently-
# dropping router (firewalled/half-open) can hang its executor thread
# indefinitely; enough of them exhaust the thread pool and stall polling for ALL
# devices. The installed routeros_api version does not support socket timeout
# kwargs, so asyncio.wait_for provides the async-side guard.
DEVICE_IO_TIMEOUT_SEC = int(os.getenv("BANDWIDTH_DEVICE_IO_TIMEOUT_SEC", "12"))
# Persistently unreachable routers remain visible in the health snapshot, but
# do not need a fresh connection attempt every minute.  A bounded exponential
# backoff protects the poller and logs while still probing recovery.
FAILURE_BACKOFF_MAX_SECONDS = 15 * 60
# Bound Redis I/O so a slow/unreachable Redis drops one poll cycle's samples
# instead of stalling the whole poller (the publish is best-effort telemetry).
REDIS_IO_TIMEOUT_SEC = int(os.getenv("BANDWIDTH_REDIS_IO_TIMEOUT_SEC", "5"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_STREAM = os.getenv("BANDWIDTH_REDIS_STREAM", "bandwidth:samples")
POLLING_ENABLED = os.getenv("BANDWIDTH_POLLING_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)

# On-demand mode polls only devices that host an active live viewer; idle when
# no operator is watching. RADIUS interim-update sampling provides the
# always-on baseline for historical charts (see usage.py).
POLL_MODE = os.getenv("BANDWIDTH_POLL_MODE", "always").strip().lower()
ACTIVE_VIEWERS_KEY = settings.bandwidth_active_viewers_key
# A live viewer's ZADD score is the unix timestamp of the most recent SSE tick.
# Memberships older than this window are treated as gone.
ACTIVE_VIEWER_TTL_SECONDS = int(os.getenv("BANDWIDTH_ACTIVE_VIEWER_TTL_SECONDS", "15"))


@dataclass
class QueueStats:
    """Statistics for a single MikroTik queue."""

    name: str
    target: str
    rate_rx: int  # bits per second
    rate_tx: int  # bits per second
    bytes_rx: int
    bytes_tx: int
    packets_rx: int
    packets_tx: int
    max_rx: int = 0  # configured max-limit (bits/s), 0 = unlimited
    max_tx: int = 0


def _clamp_rate(
    rate_bps: int, max_bps: int, tolerance: float = MAX_RATE_TOLERANCE
) -> int:
    """Clamp a reported queue rate to its configured max-limit.

    RouterOS occasionally reports a transient ``rate`` above the queue's
    ``max-limit`` (a measurement glitch). A queue cannot physically pass more
    than its cap, so such a reading would otherwise surface as a bogus "peak".
    ``max_bps <= 0`` means unlimited (no cap to apply). A small tolerance
    absorbs rounding right at the cap.
    """
    if rate_bps < 0:
        return 0
    if max_bps <= 0:
        return rate_bps
    if rate_bps > int(max_bps * tolerance):
        return max_bps
    return rate_bps


@dataclass
class BandwidthSample:
    """A single bandwidth sample to be published."""

    subscription_id: str
    nas_device_id: str
    queue_name: str
    rx_bps: int  # bits per second
    tx_bps: int  # bits per second
    sample_at: datetime


class MikroTikConnection:
    """
    Manages a persistent connection to a MikroTik device.

    Uses the RouterOS API to communicate with MikroTik devices.
    """

    def __init__(
        self,
        device_id: UUID,
        host: str,
        username: str,
        password: str,
        port: int = 8728,
        display_name: str | None = None,
    ):
        self.device_id = device_id
        self.display_name = display_name or str(device_id)
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._pool: RouterOsApiPool | None = None
        self._connection: Any | None = None
        self._last_connected: datetime | None = None
        self._last_attempt: datetime | None = None
        self._consecutive_failures: int = 0
        self._last_error_category: str | None = None
        self._last_error: str | None = None
        self._last_successful_poll: datetime | None = None
        # Background tasks that disconnect pools whose construction was still
        # in-flight when connect() timed out. Held so asyncio does not GC them.
        self._drain_tasks: set[asyncio.Task] = set()

    async def connect(self) -> bool:
        """Establish connection to the device."""
        self._last_attempt = datetime.now(UTC)
        # RouterOS API is synchronous, run in executor. wait_for guarantees
        # the async loop is never blocked by a hung device even if the
        # executor thread lingers.
        loop = asyncio.get_event_loop()
        # Constructing RouterOsApiPool opens the TCP socket AND performs the
        # API login, so the session already exists on the router the instant
        # this returns. Keep the pool in a local so any failure after this
        # point can release it — otherwise the next connect() overwrites
        # self._pool, dropping the only reference and orphaning the session
        # on the router (it lingers for days). See fix/mikrotik-poller-session-leak.
        pool: RouterOsApiPool | None = None
        try:
            # TLS-readiness: tag a NAS with mikrotik_api_port:8729 to poll over
            # RouterOS API-SSL (encrypted transport) instead of plaintext 8728.
            # No-op until a device is on 8729, so the fleet is unchanged today.
            # ssl_verify stays off for now (encrypted but unverified) — cert
            # verification is a later step once the routers have trusted certs.
            use_ssl = self.port == 8729
            # Keep the executor future so that if wait_for TIMES OUT (the pool
            # is not built yet at that instant, so it cannot be released from
            # the except block below), we can still disconnect the session that
            # the thread goes on to establish. shield() stops wait_for from
            # cancelling the underlying future, keeping it awaitable for the
            # drain. This closes the second door to the same session leak.
            construct_future = loop.run_in_executor(
                None,
                lambda: RouterOsApiPool(
                    self.host,
                    username=self.username,
                    password=self.password,
                    port=self.port,
                    plaintext_login=True,
                    use_ssl=use_ssl,
                    ssl_verify=False,
                    ssl_verify_hostname=False,
                ),
            )
            try:
                pool = await asyncio.wait_for(
                    asyncio.shield(construct_future),
                    timeout=DEVICE_IO_TIMEOUT_SEC + 3,
                )
            except BaseException:
                # The construction thread may still finish (and log in) after
                # this timeout/cancellation; drain that late session instead of
                # orphaning it. connect() still returns promptly.
                self._drain_pool_future(loop, construct_future)
                raise
            self._pool = pool
            if pool is None:
                await self._release_pool(loop, pool)
                self._pool = None
                self._connection = None
                return False
            self._connection = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: pool.get_api()),
                timeout=DEVICE_IO_TIMEOUT_SEC + 3,
            )
            self._last_connected = datetime.now(UTC)
            self._consecutive_failures = 0
            self._last_error_category = None
            self._last_error = None
            logger.info(f"Connected to MikroTik device {self.device_id} at {self.host}")
            return True
        except Exception as e:
            self._consecutive_failures += 1
            self._last_error_category = _error_category(e)
            self._last_error = _sanitize_exc(e)[:240]
            logger.error(f"Failed to connect to {self.host}: {_sanitize_exc(e)}")
            # The pool was already constructed (and thus logged in) before the
            # failure, so disconnect it here to avoid orphaning the session.
            await self._release_pool(loop, pool)
            self._pool = None
            self._connection = None
            return False

    async def _release_pool(self, loop, pool) -> None:
        """Best-effort disconnect of a pool during a failed connect().

        Mirrors disconnect()'s safe pattern so a disconnect error never masks
        the original connect failure.
        """
        if pool is None:
            return
        try:
            await loop.run_in_executor(None, pool.disconnect)
        except Exception as e:
            logger.warning(
                f"Error releasing orphaned pool for {self.host}: {_sanitize_exc(e)}"
            )

    def _drain_pool_future(self, loop, construct_future) -> None:
        """Disconnect a pool whose construction outran a connect() timeout.

        The RouterOsApiPool constructor opens the socket and logs in. If
        wait_for timed out before it returned, the executor thread keeps going
        and establishes a session with no reference to release it. This awaits
        that in-flight future in the background and disconnects the resulting
        pool, so the session is not orphaned. Fire-and-forget: connect() has
        already returned False by the time this runs.
        """

        async def _drain() -> None:
            try:
                pool = await construct_future
            except Exception:
                # Construction ultimately failed → no session was established.
                return
            await self._release_pool(loop, pool)

        task = loop.create_task(_drain())
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    async def disconnect(self):
        """Close the connection."""
        if self._pool:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._pool.disconnect)
            except Exception as e:
                logger.warning(f"Error disconnecting from {self.host}: {e}")
            self._pool = None
            self._connection = None

    async def is_connected(self) -> bool:
        """Check if connection is still alive."""
        conn = self._connection
        if conn is None:
            return False
        try:
            # Try a simple command to verify connection
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: conn.get_resource("/system/identity").get()
            )
            return True
        except Exception:
            return False

    async def get_queue_stats(self) -> list[QueueStats]:
        """
        Fetch queue statistics from the device.

        Returns list of QueueStats for all simple queues.
        """
        conn = self._connection
        if conn is None:
            if not await self.connect():
                return []
            conn = self._connection
            if conn is None:
                return []

        try:
            loop = asyncio.get_event_loop()
            queues = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: conn.get_resource("/queue/simple").get()
                ),
                timeout=DEVICE_IO_TIMEOUT_SEC + 3,
            )

            stats = []
            for q in queues:
                # Parse rate strings like "12345/67890" (rx/tx in bits/s)
                rate = q.get("rate", "0/0").split("/")
                bytes_val = q.get("bytes", "0/0").split("/")
                packets = q.get("packets", "0/0").split("/")
                # max-limit "rxMax/txMax" (bits/s); "0/0" means unlimited.
                max_limit = q.get("max-limit", "0/0").split("/")

                stats.append(
                    QueueStats(
                        name=q.get("name", ""),
                        target=q.get("target", ""),
                        rate_rx=int(rate[0]) if rate[0].isdigit() else 0,
                        rate_tx=int(rate[1])
                        if len(rate) > 1 and rate[1].isdigit()
                        else 0,
                        bytes_rx=int(bytes_val[0]) if bytes_val[0].isdigit() else 0,
                        bytes_tx=int(bytes_val[1])
                        if len(bytes_val) > 1 and bytes_val[1].isdigit()
                        else 0,
                        packets_rx=int(packets[0]) if packets[0].isdigit() else 0,
                        packets_tx=int(packets[1])
                        if len(packets) > 1 and packets[1].isdigit()
                        else 0,
                        max_rx=int(max_limit[0]) if max_limit[0].isdigit() else 0,
                        max_tx=int(max_limit[1])
                        if len(max_limit) > 1 and max_limit[1].isdigit()
                        else 0,
                    )
                )

            self._consecutive_failures = 0
            self._last_successful_poll = datetime.now(UTC)
            self._last_error_category = None
            self._last_error = None
            return stats

        except Exception as e:
            self._consecutive_failures += 1
            self._last_error_category = _error_category(e)
            self._last_error = _sanitize_exc(e)[:240]
            logger.error(
                f"Failed to get queue stats from {self.host}: {_sanitize_exc(e)}"
            )
            # Reconnect on next attempt
            await self.disconnect()
            return []

    @property
    def retry_delay_seconds(self) -> int:
        if self._consecutive_failures == 0:
            return 0
        return min(
            2**self._consecutive_failures,
            FAILURE_BACKOFF_MAX_SECONDS,
        )

    @property
    def next_retry_at(self) -> datetime | None:
        reference = self._last_attempt or self._last_connected
        if reference is None or self._consecutive_failures == 0:
            return None
        return reference + timedelta(seconds=self.retry_delay_seconds)

    @property
    def should_retry(self) -> bool:
        """Check if we should attempt reconnection."""
        # Back off exponentially based on failures.
        backoff_seconds = self.retry_delay_seconds
        if backoff_seconds == 0:
            return True
        # Compare against the last attempt (success or failure). Using
        # _last_connected alone would skip backoff entirely for devices that
        # have never connected — producing a 1-Hz retry storm.
        reference = self._last_attempt or self._last_connected
        if reference:
            elapsed = (datetime.now(UTC) - reference).total_seconds()
            return bool(elapsed >= backoff_seconds)
        return True


class DevicePool:
    """
    Manages connections to all active NAS devices.

    Periodically refreshes the device list from the database.
    """

    def __init__(self, refresh_interval: int = 60):
        self._connections: dict[UUID, MikroTikConnection] = {}
        self._queue_mappings: dict[UUID, dict[str, UUID]] = {}
        self._login_mappings: dict[UUID, dict[str, UUID]] = {}
        # subscription_id -> (rx_cap_bps, tx_cap_bps) from the plan's provisioned
        # speed. The fallback cap when a RouterOS queue is uncapped (max-limit
        # 0/0, common on "unlimited" plans), so a glitchy rate still can't exceed
        # the plan rate.
        self._speed_caps: dict[UUID, tuple[int, int]] = {}
        self._refresh_interval = refresh_interval
        self._last_refresh: datetime | None = None

    def resolve_speed(self, subscription_id: UUID) -> tuple[int, int]:
        """Plan rate caps (rx_bps, tx_bps) for a subscription, or (0, 0) if
        unknown (no cap applied)."""
        return self._speed_caps.get(subscription_id, (0, 0))

    @staticmethod
    def _resolve_mikrotik_api_port(device: NasDevice) -> int:
        """
        Resolve MikroTik API port.

        The NAS record's management_port is commonly SSH and should not be used
        for RouterOS API polling. Preferred source is the ``mikrotik_api_port``
        column; falls back to the legacy tag `mikrotik_api_port:NNNN`, then 8728.
        """
        column_port = getattr(device, "mikrotik_api_port", None)
        if isinstance(column_port, int) and 1 <= column_port <= 65535:
            return column_port
        tags = getattr(device, "tags", None)
        if isinstance(tags, list):
            for tag in tags:
                if not isinstance(tag, str):
                    continue
                token = tag.strip()
                if token.lower().startswith("mikrotik_api_port:"):
                    raw = token.split(":", 1)[1].strip()
                    try:
                        port = int(raw)
                        if 1 <= port <= 65535:
                            return port
                    except (TypeError, ValueError):
                        continue
        return 8728

    @staticmethod
    def _queue_aliases(queue_name: str) -> set[str]:
        """Generate normalized aliases for queue name lookup."""
        raw = (queue_name or "").strip()
        if not raw:
            return set()
        aliases: set[str] = set()
        candidates = {raw}
        if raw.startswith("<") and raw.endswith(">") and len(raw) > 2:
            candidates.add(raw[1:-1].strip())

        # Build canonical variants
        for candidate in list(candidates):
            normalized = candidate.strip()
            if not normalized:
                continue
            lower = normalized.lower()
            aliases.add(normalized)
            aliases.add(lower)

            # Common queue prefixes that may wrap login/username
            for prefix in ("queue-", "pppoe-", "hotspot-", "dhcp-", "sub-"):
                if lower.startswith(prefix) and len(normalized) > len(prefix):
                    stripped = normalized[len(prefix) :].strip()
                    if stripped:
                        aliases.add(stripped)
                        aliases.add(stripped.lower())
                else:
                    aliases.add(f"{prefix}{normalized}")
                    aliases.add(f"{prefix}{lower}")
        return aliases

    @classmethod
    def _build_mapping_alias_dict(
        cls, mapping_dict: dict[str, UUID]
    ) -> dict[str, UUID]:
        """Expand queue mapping keys to include common naming variants."""
        expanded: dict[str, UUID] = {}
        for queue_name, subscription_id in mapping_dict.items():
            for alias in cls._queue_aliases(queue_name):
                expanded[alias] = subscription_id
        return expanded

    async def refresh_devices(self):
        """Refresh the list of devices from the database."""
        db = db_session_adapter.create_session()
        try:
            devices = (
                db.query(NasDevice)
                .filter(
                    NasDevice.vendor == NasVendor.mikrotik,
                    NasDevice.status == NasDeviceStatus.active,
                    NasDevice.is_active.is_(True),
                )
                .all()
            )

            current_ids = set(self._connections.keys())
            new_ids = set()

            for device in devices:
                device_id = device.id
                new_ids.add(device_id)

                # Skip if already connected
                if device_id in self._connections:
                    continue

                # Create new connection if we have credentials
                if device.api_username and device.api_password and device.management_ip:
                    try:
                        api_password = decrypt_credential(device.api_password)
                    except ValueError as exc:
                        # One bad ciphertext must not gate the whole refresh.
                        logger.warning(
                            "Skipping NAS %s: api_password decrypt failed (%s)",
                            device_id,
                            exc,
                        )
                        continue
                    if not api_password:
                        logger.warning(
                            "Skipping NAS %s: API password could not be decrypted",
                            device_id,
                        )
                        continue
                    self._connections[device_id] = MikroTikConnection(
                        device_id=device_id,
                        display_name=str(device.name or device_id),
                        host=device.management_ip,
                        username=device.api_username,
                        password=api_password,
                        port=self._resolve_mikrotik_api_port(device),
                    )

                # Fallback mapping from active subscriptions on this NAS by login.
                # Duplicate logins are common (migrated re-installs leave many
                # disabled/canceled rows). When multiple rows share a login,
                # prefer status=active over status=pending so the customer
                # portal (which resolves the active sub) finds samples tagged
                # with the same sub it queries.
                login_rows = (
                    db.query(Subscription.id, Subscription.login, Subscription.status)
                    .filter(
                        Subscription.provisioning_nas_device_id == device_id,
                        Subscription.status.in_(
                            [SubscriptionStatus.active, SubscriptionStatus.pending]
                        ),
                        Subscription.login.isnot(None),
                    )
                    .all()
                )
                _STATUS_PRIORITY = {
                    SubscriptionStatus.active: 0,
                    SubscriptionStatus.pending: 1,
                }
                best_for_login: dict[str, tuple[UUID, int]] = {}
                for sub_id, login, status in login_rows:
                    login_clean = str(login).strip()
                    if not login_clean:
                        continue
                    pri = _STATUS_PRIORITY.get(status, 99)
                    existing = best_for_login.get(login_clean)
                    if existing is None or pri < existing[1]:
                        best_for_login[login_clean] = (sub_id, pri)

                # Load queue mappings for this device and remap any entry whose
                # subscription_id is a stale duplicate (same login as an active
                # sub but pointing to a disabled/canceled/suspended row). The
                # QueueMapping table is populated at turn-up and isn't updated
                # when an old sub is suspended in favor of a re-install.
                raw_mapping = queue_mapping.get_device_mapping_dict(db, device_id)
                if raw_mapping and best_for_login:
                    mapped_sub_ids = {sid for sid in raw_mapping.values() if sid}
                    sub_logins = (
                        dict(
                            db.query(Subscription.id, Subscription.login)
                            .filter(Subscription.id.in_(mapped_sub_ids))
                            .all()
                        )
                        if mapped_sub_ids
                        else {}
                    )
                    fixed_mapping: dict[str, UUID] = {}
                    for queue_name, sub_id in raw_mapping.items():
                        login = sub_logins.get(sub_id)
                        if login:
                            preferred = best_for_login.get(str(login).strip())
                            if preferred and preferred[0] != sub_id:
                                sub_id = preferred[0]
                        fixed_mapping[queue_name] = sub_id
                    raw_mapping = fixed_mapping
                self._queue_mappings[device_id] = self._build_mapping_alias_dict(
                    raw_mapping
                )
                login_map: dict[str, UUID] = {}
                for login_clean, (sub_id, _) in best_for_login.items():
                    for alias in self._queue_aliases(login_clean):
                        login_map[alias] = sub_id
                self._login_mappings[device_id] = login_map

            # Remove connections for devices that are no longer active
            removed_ids = current_ids - new_ids
            for device_id in removed_ids:
                conn = self._connections.pop(device_id, None)
                if conn:
                    await conn.disconnect()
                self._queue_mappings.pop(device_id, None)
                self._login_mappings.pop(device_id, None)

            # Plan rate caps (subscriber download = NAS tx, upload = NAS rx),
            # used to clamp glitchy samples on uncapped RouterOS queues.
            speed_caps: dict[UUID, tuple[int, int]] = {}
            speed_rows = (
                db.query(
                    Subscription.id,
                    CatalogOffer.speed_download_mbps,
                    CatalogOffer.speed_upload_mbps,
                )
                .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
                .filter(
                    Subscription.status.in_(
                        [SubscriptionStatus.active, SubscriptionStatus.pending]
                    )
                )
                .all()
            )
            for sub_id, dl_mbps, ul_mbps in speed_rows:
                rx_cap = int(ul_mbps) * 1_000_000 if ul_mbps else 0  # upload -> rx
                tx_cap = int(dl_mbps) * 1_000_000 if dl_mbps else 0  # download -> tx
                speed_caps[sub_id] = (rx_cap, tx_cap)
            self._speed_caps = speed_caps

            logger.info(f"Device pool refreshed: {len(self._connections)} devices")

        finally:
            # Always advance the refresh marker — a failed refresh must not
            # cause a 1-Hz retry storm against the DB and credential layer.
            self._last_refresh = datetime.now(UTC)
            db.close()

    def _should_refresh(self) -> bool:
        if not self._last_refresh:
            return True
        elapsed = (datetime.now(UTC) - self._last_refresh).total_seconds()
        return elapsed >= self._refresh_interval

    def _devices_with_active_subscriptions(self, active_sub_ids: set[str]) -> set[UUID]:
        """Return device IDs that host at least one of the given subscriptions."""
        if not active_sub_ids:
            return set()
        hosts: set[UUID] = set()
        for device_id, mapping in self._queue_mappings.items():
            if any(str(sub_id) in active_sub_ids for sub_id in mapping.values()):
                hosts.add(device_id)
                continue
            login_map = self._login_mappings.get(device_id, {})
            if any(str(sub_id) in active_sub_ids for sub_id in login_map.values()):
                hosts.add(device_id)
        return hosts

    async def poll_all(
        self, active_devices: set[UUID] | None = None
    ) -> AsyncIterator[tuple[UUID, list[QueueStats]]]:
        """Poll connected devices and yield queue stats.

        When ``active_devices`` is provided, only those device IDs are polled;
        this is how on-demand mode skips devices with no live viewer.
        """
        if self._should_refresh():
            await self.refresh_devices()

        # Poll all devices concurrently
        async def poll_device(device_id: UUID, conn: MikroTikConnection):
            if not conn.should_retry:
                return device_id, []
            stats = await conn.get_queue_stats()
            return device_id, stats

        tasks = [
            poll_device(device_id, conn)
            for device_id, conn in self._connections.items()
            if active_devices is None or device_id in active_devices
        ]

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                logger.error(f"Polling error: {result}")
                continue
            device_id, stats = result
            if stats:
                yield device_id, stats

    def resolve_subscription(self, device_id: UUID, queue_name: str) -> UUID | None:
        """Resolve a queue name to a subscription ID."""
        queue_aliases = self._queue_aliases(queue_name)
        if not queue_aliases:
            return None

        mappings = self._queue_mappings.get(device_id, {})
        for key in queue_aliases:
            if key in mappings:
                return mappings[key]

        login_mappings = self._login_mappings.get(device_id, {})
        for key in queue_aliases:
            if key in login_mappings:
                return login_mappings[key]

        # Legacy auto-generated fallback: sub-<uuid>
        for key in queue_aliases:
            if key.lower().startswith("sub-"):
                candidate = key[4:]
                try:
                    return UUID(candidate)
                except ValueError:
                    continue
        return None

    def health_snapshot(self) -> dict[str, object]:
        """Per-cycle device health for telemetry: total / reachable / failing.

        A device is "failing" while it has consecutive poll failures (it's in
        backoff). Surfaces silently-broken routers that would otherwise be
        invisible until a customer complains.
        """
        total = len(self._connections)
        failing = sum(
            1 for c in self._connections.values() if c._consecutive_failures > 0
        )
        failures = []
        for connection in self._connections.values():
            if connection._consecutive_failures <= 0:
                continue
            mapped_services = len(
                set(self._queue_mappings.get(connection.device_id, {}).values())
                | set(self._login_mappings.get(connection.device_id, {}).values())
            )
            failures.append(
                {
                    "device_id": str(connection.device_id),
                    "name": connection.display_name,
                    "host": connection.host,
                    "error_category": connection._last_error_category
                    or "transport_error",
                    "error": connection._last_error or "Polling did not complete",
                    "consecutive_failures": connection._consecutive_failures,
                    "last_attempt_at": (
                        connection._last_attempt.isoformat()
                        if connection._last_attempt
                        else None
                    ),
                    "last_success_at": (
                        connection._last_successful_poll.isoformat()
                        if connection._last_successful_poll
                        else None
                    ),
                    "next_attempt_at": (
                        connection.next_retry_at.isoformat()
                        if connection.next_retry_at
                        else None
                    ),
                    "services_without_live_bandwidth": mapped_services,
                }
            )
        failures.sort(
            key=lambda item: (
                -int(item["consecutive_failures"]),
                str(item["name"]).lower(),
            )
        )
        return {
            "devices_total": total,
            "devices_ok": total - failing,
            "devices_failing": failing,
            "device_failures": failures[:100],
        }

    async def close(self):
        """Close all connections."""
        for conn in self._connections.values():
            await conn.disconnect()
        self._connections.clear()


class BandwidthPoller:
    """
    Main polling orchestrator.

    Runs a continuous polling loop, collecting bandwidth samples from
    all devices and publishing them to a Redis stream.
    """

    def __init__(self, poll_interval_ms: int = POLL_INTERVAL_MS):
        self.poll_interval_ms = poll_interval_ms
        self.device_pool = DevicePool()
        self._redis: redis.Redis | None = None
        self._running = False
        self._poll_count = 0
        self._sample_count = 0

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = cast(
                redis.Redis,
                redis.from_url(
                    REDIS_URL,
                    socket_timeout=REDIS_IO_TIMEOUT_SEC,
                    socket_connect_timeout=REDIS_IO_TIMEOUT_SEC,
                ),
            )
        return self._redis

    async def _publish_samples(self, samples: list[BandwidthSample]):
        """Publish samples to Redis stream as a single pipelined batch.

        Individual XADD per sample yields one WAN round-trip each; on a remote
        Redis at 66ms RTT that bottlenecks the poller. A pipeline collapses
        every sample in a poll cycle into a single network exchange.
        """
        if not samples:
            return

        r = await self._get_redis()
        try:
            async with r.pipeline(transaction=False) as pipe:
                for sample in samples:
                    data: dict[bytes | str | int | float, bytes | str | int | float] = {
                        "subscription_id": sample.subscription_id,
                        "nas_device_id": sample.nas_device_id,
                        "queue_name": sample.queue_name,
                        "rx_bps": str(sample.rx_bps),
                        "tx_bps": str(sample.tx_bps),
                        "sample_at": sample.sample_at.isoformat(),
                    }
                    pipe.xadd(REDIS_STREAM, data, maxlen=100000)
                await asyncio.wait_for(pipe.execute(), timeout=REDIS_IO_TIMEOUT_SEC)
        except (TimeoutError, redis.RedisError) as exc:
            # Best-effort telemetry: a slow/broken Redis must not stall polling.
            # Drop this cycle's samples (the next cycle re-reads current rates).
            logger.warning(
                "bandwidth sample publish failed (%s); dropping %d samples",
                exc,
                len(samples),
            )
            return

        self._sample_count += len(samples)

    async def _publish_health(self, cycle_seconds: float) -> None:
        """Write a best-effort health snapshot for the web /metrics collector.

        Never raises — health telemetry must never affect polling.
        """
        try:
            payload = json.dumps(
                {
                    **self.device_pool.health_snapshot(),
                    "cycle_seconds": round(cycle_seconds, 3),
                    "poll_count": self._poll_count,
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
            r = await self._get_redis()
            await asyncio.wait_for(
                r.set(POLLER_HEALTH_KEY, payload), timeout=REDIS_IO_TIMEOUT_SEC
            )
        except Exception:
            pass

    async def _active_viewer_subscriptions(self) -> set[str]:
        """Return subscription IDs with a live SSE viewer within the TTL window."""
        try:
            r = await self._get_redis()
            cutoff = time.time() - ACTIVE_VIEWER_TTL_SECONDS
            members = await r.zrangebyscore(ACTIVE_VIEWERS_KEY, cutoff, "+inf")
        except Exception as exc:
            logger.debug("active viewer lookup failed: %s", exc)
            return set()
        result: set[str] = set()
        for member in members:
            if isinstance(member, bytes):
                result.add(member.decode("utf-8", errors="ignore"))
            else:
                result.add(str(member))
        return result

    async def _poll_once(self):
        """Execute a single polling cycle."""
        sample_time = datetime.now(UTC)
        samples = []

        active_devices: set[UUID] | None = None
        if POLL_MODE == "on_demand":
            active_subs = await self._active_viewer_subscriptions()
            if not active_subs:
                # No live viewers — RADIUS interim-update samples (60s cadence)
                # keep historical charts populated without burning the poller.
                self._poll_count += 1
                return
            active_devices = self.device_pool._devices_with_active_subscriptions(
                active_subs
            )
            if not active_devices:
                self._poll_count += 1
                return

        async for device_id, queue_stats in self.device_pool.poll_all(active_devices):
            for qs in queue_stats:
                subscription_id = self.device_pool.resolve_subscription(
                    device_id, qs.name
                )
                if subscription_id:
                    # RouterOS simple-queue rate is already bits/s. Clamp to the
                    # queue's configured max-limit: a queue physically cannot pass
                    # more than its cap, so a reported rate above it is a RouterOS
                    # glitch that would otherwise become a bogus "peak". When the
                    # queue is uncapped (max-limit 0/0, common on "unlimited"
                    # plans), fall back to the plan's provisioned speed so the
                    # glitch still can't exceed the rate the customer pays for.
                    rx_plan, tx_plan = self.device_pool.resolve_speed(subscription_id)
                    rx_bps = _clamp_rate(qs.rate_rx, qs.max_rx or rx_plan)
                    tx_bps = _clamp_rate(qs.rate_tx, qs.max_tx or tx_plan)
                    samples.append(
                        BandwidthSample(
                            subscription_id=str(subscription_id),
                            nas_device_id=str(device_id),
                            queue_name=qs.name,
                            rx_bps=rx_bps,
                            tx_bps=tx_bps,
                            sample_at=sample_time,
                        )
                    )

        if samples:
            await self._publish_samples(samples)

        self._poll_count += 1

    async def run(self):
        """
        Run the polling loop.

        Polls all devices at the configured interval and publishes
        bandwidth samples to Redis.
        """
        if not POLLING_ENABLED:
            logger.warning("Bandwidth polling is disabled")
            return

        self._running = True
        interval_seconds = self.poll_interval_ms / 1000.0

        logger.info(f"Starting bandwidth poller with {interval_seconds}s interval")

        try:
            while self._running:
                start = time.monotonic()

                try:
                    await self._poll_once()
                except Exception as e:
                    logger.error(f"Polling error: {e}")

                # Calculate sleep time to maintain consistent interval
                elapsed = time.monotonic() - start
                await self._publish_health(elapsed)
                sleep_time = max(0, interval_seconds - elapsed)

                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

                # Log stats periodically
                if self._poll_count % 60 == 0:
                    logger.info(
                        f"Poller stats: {self._poll_count} polls, "
                        f"{self._sample_count} samples"
                    )

        finally:
            await self.stop()

    async def stop(self):
        """Stop the polling loop and cleanup."""
        self._running = False
        await self.device_pool.close()
        if self._redis:
            await self._redis.close()
        logger.info("Bandwidth poller stopped")


async def main():
    """Entry point for the poller service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    poller = BandwidthPoller()

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def handle_signal():
        logger.info("Received shutdown signal")
        asyncio.create_task(poller.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    await poller.run()


if __name__ == "__main__":
    asyncio.run(main())
