from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketState

from app.services import realtime_platform
from app.services.realtime_platform import (
    EventType,
    build_event,
    parse_event,
    principal_topic,
    redis_channel,
    sse_message,
)
from app.services.workqueue import WorkqueueAudience, WorkqueuePrincipal, WorkqueueScope
from app.services.workqueue.events import channels_for_scope
from app.websocket.events import WebSocketEvent
from app.websocket.manager import ConnectionManager


def _run_async(coro):
    """Run a coroutine outside pytest's already-running suite event loop."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


class _Socket:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def test_one_versioned_envelope_drives_websocket_and_sse() -> None:
    event = build_event(
        "workqueue:audience:org",
        EventType.WORKQUEUE_CHANGED,
        {"item_id": str(uuid4())},
    )

    restored = parse_event(event.model_dump_json())
    sse = sse_message(event)

    assert restored == event
    assert event.schema_version == 1
    assert event.refresh_required is True
    assert sse["id"] == str(event.event_id)
    assert sse["event"] == EventType.WORKQUEUE_CHANGED.value
    assert json.loads(sse["data"])["topic"] == "workqueue:audience:org"


def test_publish_uses_shared_broker_channel_and_envelope(monkeypatch) -> None:
    published: list[tuple[str, str]] = []
    client = SimpleNamespace(
        publish=lambda channel, payload: published.append((channel, payload))
    )
    monkeypatch.setattr(realtime_platform, "get_redis", lambda: client)
    event = build_event("principal:test-user", "notification.received", {})

    assert realtime_platform.publish_event(event) is True
    assert published[0][0] == redis_channel("principal:test-user")
    assert parse_event(published[0][1]) == event


def test_publish_degrades_without_a_broker(monkeypatch) -> None:
    monkeypatch.setattr(realtime_platform, "get_redis", lambda: None)
    event = build_event("principal:test-user", "notification.received", {})

    assert realtime_platform.publish_event(event) is False


def test_publish_degrades_when_the_broker_call_fails(monkeypatch) -> None:
    class _BrokenClient:
        def publish(self, *_args) -> None:
            raise RuntimeError("broker failed")

    monkeypatch.setattr(realtime_platform, "get_redis", lambda: _BrokenClient())
    event = build_event("principal:test-user", "notification.received", {})

    assert realtime_platform.publish_event(event) is False


def test_manager_scopes_topics_to_one_socket_per_principal() -> None:
    async def exercise() -> None:
        manager = ConnectionManager()
        first = _Socket()
        second = _Socket()
        user_id = str(uuid4())
        await manager.register_connection(user_id, first)  # type: ignore[arg-type]
        await manager.register_connection(user_id, second)  # type: ignore[arg-type]
        first.sent.clear()
        second.sent.clear()

        manager.subscribe_topic(first, "workqueue:audience:org")  # type: ignore[arg-type]
        await manager._dispatch_to_subscribers(
            build_event("workqueue:audience:org", "workqueue_changed", {})
        )

        assert len(first.sent) == 1
        assert second.sent == []
        assert principal_topic(user_id) in manager._subscriptions

    _run_async(exercise())


def test_manager_does_not_double_dispatch_a_redis_publish(monkeypatch) -> None:
    async def exercise() -> None:
        async def run_inline(func, *args, **kwargs):
            return func(*args, **kwargs)

        manager = ConnectionManager()
        socket = _Socket()
        await manager.register_connection("operator", socket)  # type: ignore[arg-type]
        manager.subscribe_topic(
            socket, "operation:00000000-0000-0000-0000-000000000001"
        )  # type: ignore[arg-type]
        socket.sent.clear()
        manager._running = True
        monkeypatch.setattr(
            "app.websocket.manager.publish_event",
            lambda event: True,
        )
        monkeypatch.setattr("app.websocket.manager.asyncio.to_thread", run_inline)

        await manager.broadcast_to_topic(
            "operation:00000000-0000-0000-0000-000000000001",
            WebSocketEvent(
                event=EventType.OPERATION_STATUS, data={"status": "running"}
            ),
        )
        assert socket.sent == []

        event = build_event(
            "operation:00000000-0000-0000-0000-000000000001",
            EventType.OPERATION_STATUS,
            {"status": "running"},
        )
        await manager._handle_redis_message(
            redis_channel(event.topic), event.model_dump_json()
        )
        assert len(socket.sent) == 1

    _run_async(exercise())


def test_operation_notifications_use_the_platform_topic(monkeypatch) -> None:
    from app.services import operation_notifications

    operation_id = str(uuid4())
    captured: dict = {}

    def publish(topic, *, event_type, payload):
        captured.update(topic=topic, event_type=event_type, payload=payload)
        return True

    monkeypatch.setattr(operation_notifications, "publish_topic_event", publish)

    assert operation_notifications.publish_operation_status(
        operation_id,
        "succeeded",
        "Done",
    )
    assert captured["topic"] == f"operation:{operation_id}"
    assert captured["event_type"] == "operation_status"
    assert captured["payload"]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("recipient", "expected_topic"),
    [("operator-id", "principal:operator-id"), ("broadcast", "audience:staff")],
)
def test_websocket_notification_provider_uses_platform_topics(
    monkeypatch, recipient, expected_topic
) -> None:
    from app.services import notification_adapter

    captured: dict = {}

    def publish(topic, *, event_type, payload):
        captured.update(topic=topic, event_type=event_type, payload=payload)
        return True

    monkeypatch.setattr(realtime_platform, "publish_topic_event", publish)
    provider = notification_adapter.WebSocketProvider()
    result = provider.send(
        notification_adapter.NotificationRequest(
            channel=notification_adapter.NotificationChannel.websocket,
            recipient=recipient,
            message="Link restored",
            category=notification_adapter.NotificationCategory.network_alert,
        )
    )

    assert result.success is True
    assert captured["topic"] == expected_topic
    assert captured["event_type"] == "notification.received"
    assert captured["payload"]["category"] == "network_alert"


def test_workqueue_scope_topics_are_server_derived() -> None:
    person_id = uuid4()
    team_id = uuid4()
    principal = WorkqueuePrincipal(
        person_id=person_id,
        roles=frozenset(),
        scopes=frozenset(),
        can_view=True,
        can_act=False,
    )
    scope = WorkqueueScope(
        principal=principal,
        audience=WorkqueueAudience.team,
        member_service_team_ids=frozenset({team_id}),
        accessible_service_team_ids=frozenset({team_id}),
        accessible_person_ids=frozenset({person_id}),
        service_team_filter=None,
        is_org_wide=False,
    )

    assert channels_for_scope(scope) == [
        f"workqueue:user:{person_id}",
        f"workqueue:audience:team:{team_id}",
    ]


def test_workqueue_sse_releases_db_and_signals_no_replay(monkeypatch) -> None:
    from app.api import workqueue as workqueue_api

    person_id = uuid4()
    principal = WorkqueuePrincipal(
        person_id=person_id,
        roles=frozenset(),
        scopes=frozenset(),
        can_view=True,
        can_act=False,
    )
    scope = WorkqueueScope(
        principal=principal,
        audience=WorkqueueAudience.self_,
        member_service_team_ids=frozenset(),
        accessible_service_team_ids=frozenset(),
        accessible_person_ids=frozenset({person_id}),
        service_team_filter=None,
        is_org_wide=False,
    )

    class _Session:
        committed = False
        closed = False
        new: tuple[()] = ()
        dirty: tuple[()] = ()
        deleted: tuple[()] = ()

        def __init__(self) -> None:
            self._in_transaction = True

        def in_transaction(self):
            return self._in_transaction

        def commit(self):
            self.committed = True
            self._in_transaction = False

        def close(self):
            self.closed = True

    class _Request:
        async def is_disconnected(self):
            return False

    async def no_events(*_args, **_kwargs):
        if False:
            yield None

    db = _Session()
    monkeypatch.setattr(workqueue_api, "_principal", lambda *_args: principal)
    monkeypatch.setattr(
        workqueue_api.workqueue, "get_workqueue_scope", lambda *_args, **_kwargs: scope
    )
    monkeypatch.setattr(workqueue_api, "iter_topic_events", no_events)

    response = workqueue_api.workqueue_events(
        request=_Request(),  # type: ignore[arg-type]
        audience=None,
        last_event_id="lost-event-id",
        auth={"principal_id": str(person_id)},
        db=db,  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        stream = response.body_iterator
        ready = await anext(stream)
        reset = await anext(stream)

        assert db.committed is True
        assert db.closed is True
        assert ready["event"] == "realtime.ready"
        assert reset["event"] == "realtime.reset"
        assert (
            json.loads(reset["data"])["data"]["reason"] == "redis_pubsub_has_no_replay"
        )

    _run_async(exercise())
