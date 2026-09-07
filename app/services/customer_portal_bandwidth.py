"""Compatibility adapter for the canonical live-bandwidth observation owner."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

from app.services.network.live_bandwidth_observations import (
    LiveBandwidthStreamQuery,
)
from app.services.network.live_bandwidth_observations import (
    live_bandwidth_events as owner_live_bandwidth_events,
)


async def live_bandwidth_events(
    *,
    subscription_id: UUID,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[dict[str, str]]:
    """Yield from the registered owner while callers migrate imports."""
    async for event in owner_live_bandwidth_events(
        LiveBandwidthStreamQuery(subscription_id=subscription_id),
        is_disconnected=is_disconnected,
    ):
        yield event
