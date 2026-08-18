from __future__ import annotations

from scripts import verify_celery_workers


class _FakeControl:
    def __init__(
        self,
        replies: list[dict[str, dict[str, str]]],
        active_queues: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self._replies = replies
        self._active_queues = active_queues or {}
        self.destination: list[str] | None = None
        self.timeout: float | None = None

    def ping(
        self,
        *,
        destination: list[str],
        timeout: float,
    ) -> list[dict[str, dict[str, str]]]:
        self.destination = destination
        self.timeout = timeout
        return self._replies

    def inspect(self, *, destination: list[str], timeout: float):
        control = self

        class _Inspector:
            def active_queues(self):
                return control._active_queues

        return _Inspector()


class _FakeCelery:
    control: _FakeControl

    def __init__(
        self,
        _name: str,
        *,
        broker: str,
        control: _FakeControl,
    ) -> None:
        assert broker == "redis://broker"
        self.control = control


def test_verify_worker_nodes_requires_every_exact_reply(monkeypatch) -> None:
    control = _FakeControl(
        [
            {"celery@worker-a": {"ok": "pong"}},
            {"celery@worker-b": {"ok": "pong"}},
        ]
    )
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker")
    monkeypatch.setattr(
        verify_celery_workers,
        "Celery",
        lambda name, *, broker: _FakeCelery(name, broker=broker, control=control),
    )

    assert verify_celery_workers.verify_worker_nodes(
        ["celery@worker-b", "celery@worker-a"],
        timeout=3.0,
    )
    assert control.destination == ["celery@worker-a", "celery@worker-b"]
    assert control.timeout == 3.0


def test_verify_worker_nodes_fails_when_one_node_does_not_reply(monkeypatch) -> None:
    control = _FakeControl([{"celery@worker-a": {"ok": "pong"}}])
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker")
    monkeypatch.setattr(
        verify_celery_workers,
        "Celery",
        lambda name, *, broker: _FakeCelery(name, broker=broker, control=control),
    )

    assert not verify_celery_workers.verify_worker_nodes(
        ["celery@worker-a", "celery@worker-b"],
        timeout=3.0,
    )


def test_verify_worker_nodes_fails_closed_without_broker(monkeypatch) -> None:
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)

    assert not verify_celery_workers.verify_worker_nodes(
        ["celery@worker-a"],
        timeout=3.0,
    )


def test_verify_worker_nodes_requires_expected_queue_binding(monkeypatch) -> None:
    control = _FakeControl(
        [{"celery@worker-a": {"ok": "pong"}}],
        active_queues={"celery@worker-a": [{"name": "notifications"}]},
    )
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker")
    monkeypatch.setattr(
        verify_celery_workers,
        "Celery",
        lambda name, *, broker: _FakeCelery(name, broker=broker, control=control),
    )

    assert verify_celery_workers.verify_worker_nodes(
        ["celery@worker-a"],
        timeout=3.0,
        required_queues={"celery@worker-a": ["notifications"]},
    )
    assert not verify_celery_workers.verify_worker_nodes(
        ["celery@worker-a"],
        timeout=3.0,
        required_queues={"celery@worker-a": ["notifications_immediate"]},
    )
