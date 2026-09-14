"""Sub-owned real-time projection contract and Redis event broker.

Real-time delivery is deliberately best-effort. Domain owners commit durable
state first and may then publish an invalidation event; clients reconnect and
refetch the canonical read model instead of treating this stream as history.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

REALTIME_SCHEMA_VERSION = 1
REDIS_CHANNEL_PREFIX = f"realtime:v{REALTIME_SCHEMA_VERSION}:"
STAFF_AUDIENCE_TOPIC = "audience:staff"


class EventType(str, Enum):
    """Stable event names retained across WebSocket and SSE transports."""

    MESSAGE_NEW = "message_new"
    MESSAGE_STATUS_CHANGED = "message_status_changed"
    CONVERSATION_UPDATED = "conversation_updated"
    CONVERSATION_SUMMARY = "conversation_summary"
    USER_TYPING = "user_typing"
    CONNECTION_ACK = "connection_ack"
    HEARTBEAT = "heartbeat"
    OPERATION_STATUS = "operation_status"
    WORKQUEUE_CHANGED = "workqueue_changed"
    SUPPORT_TICKET_COMMENT_CHANGED = "support_ticket_comment_changed"


class RealtimeEvent(BaseModel):
    """Canonical wire envelope shared by WebSocket and SSE transports."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    event: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    topic: str = Field(min_length=1, max_length=200, pattern=r"^[a-z][a-z0-9_.:-]*$")
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: Literal[1] = 1
    refresh_required: bool = True


def _enum_value(value: str | Enum) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def build_event(
    topic: str,
    event_type: str | Enum,
    payload: Mapping[str, Any] | None = None,
    *,
    refresh_required: bool = True,
    timestamp: datetime | None = None,
) -> RealtimeEvent:
    """Build the versioned event envelope; callers cannot override its id."""
    values: dict[str, Any] = {
        "topic": topic,
        "event": _enum_value(event_type),
        "data": dict(payload or {}),
        "refresh_required": refresh_required,
    }
    if timestamp is not None:
        values["timestamp"] = timestamp
    return RealtimeEvent(**values)


def redis_channel(topic: str) -> str:
    """Return the broker channel for a topic validated at the event boundary."""
    return f"{REDIS_CHANNEL_PREFIX}{topic}"


def publish_event(event: RealtimeEvent) -> bool:
    """Publish an event once through the shared sync Redis client.

    ``False`` means delivery was unavailable. It must never roll back or fail
    the durable domain write which requested the projection.
    """
    try:
        client = get_redis()
        if client is None:
            return False
        client.publish(redis_channel(event.topic), event.model_dump_json())
        return True
    except Exception as exc:
        logger.warning(
            "realtime_publish_failed topic=%s event=%s error=%s",
            event.topic,
            event.event,
            exc,
        )
        return False


def publish_topic_event(
    topic: str,
    *,
    event_type: str | Enum,
    payload: Mapping[str, Any],
    refresh_required: bool = True,
) -> bool:
    return publish_event(
        build_event(
            topic,
            event_type,
            payload,
            refresh_required=refresh_required,
        )
    )


def parse_event(raw: str | bytes) -> RealtimeEvent:
    return RealtimeEvent.model_validate_json(raw)


def sse_message(event: RealtimeEvent) -> dict[str, str]:
    """Project the same canonical envelope into an SSE frame."""
    return {
        "id": str(event.event_id),
        "event": event.event,
        "data": event.model_dump_json(),
    }


def ready_event(topics: Iterable[str], *, transport: str) -> RealtimeEvent:
    topic_list = sorted(set(topics))
    return build_event(
        "realtime:connection",
        "realtime.ready",
        {"transport": transport, "topics": topic_list},
        refresh_required=True,
    )


def reset_event(topics: Iterable[str], *, reason: str) -> RealtimeEvent:
    return build_event(
        "realtime:connection",
        "realtime.reset",
        {"reason": reason, "topics": sorted(set(topics))},
        refresh_required=True,
    )


async def iter_topic_events(
    topics: Iterable[str],
    *,
    stop_requested: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[RealtimeEvent]:
    """Yield Redis pub/sub events for explicit, already-authorized topics.

    Redis pub/sub has no replay. A disconnect ends this iterator and the client
    must reconnect/refetch; the transport adapter communicates that contract.
    """
    import redis.asyncio as aioredis

    topic_list = sorted(set(topics))
    if not topic_list:
        return

    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(*(redis_channel(topic) for topic in topic_list))
        while True:
            if stop_requested is not None and await stop_requested():
                return
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if not message or message.get("type") != "message":
                continue
            try:
                event = parse_event(message["data"])
                if event.topic not in topic_list or str(
                    message.get("channel")
                ) != redis_channel(event.topic):
                    logger.warning(
                        "realtime_channel_topic_mismatch channel=%s event_topic=%s",
                        message.get("channel"),
                        event.topic,
                    )
                    continue
                yield event
            except (ValueError, TypeError, json.JSONDecodeError):
                logger.warning("realtime_invalid_event", exc_info=True)
    finally:
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        finally:
            await client.aclose()


def conversation_topic(conversation_id: UUID | str) -> str:
    return f"conversation:{UUID(str(conversation_id))}"


def operation_topic(operation_id: UUID | str) -> str:
    return f"operation:{UUID(str(operation_id))}"


def principal_topic(principal_id: UUID | str) -> str:
    return f"principal:{principal_id}"
