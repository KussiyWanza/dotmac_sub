from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.services import infrastructure_health, observability


def test_root_transaction_span_emits_metric_and_correlated_warning(monkeypatch, caplog):
    from app.services import session_hooks

    observed: list[tuple[float, bool]] = []
    session = SimpleNamespace(
        info={
            session_hooks._ROOT_TRANSACTION_SPAN_KEY: {
                "started": 10.0,
                "request_id": "request-123",
            }
        }
    )
    transaction = SimpleNamespace(parent=None)
    monkeypatch.setattr(session_hooks, "monotonic", lambda: 45.5)
    monkeypatch.setattr(
        session_hooks,
        "_observe_transaction_span",
        lambda duration, *, slow: observed.append((duration, slow)),
    )

    with caplog.at_level("WARNING"):
        session_hooks._finish_root_transaction_span(session, transaction)

    assert observed == [(35.5, True)]
    record = next(
        item
        for item in caplog.records
        if item.message == "database_transaction_span_slow"
    )
    assert record.duration_seconds == 35.5
    assert record.request_id == "request-123"


def test_fast_root_transaction_records_histogram_without_warning(monkeypatch, caplog):
    from app.services import session_hooks

    observed: list[tuple[float, bool]] = []
    session = SimpleNamespace(
        info={
            session_hooks._ROOT_TRANSACTION_SPAN_KEY: {
                "started": 10.0,
                "request_id": "request-456",
            }
        }
    )
    monkeypatch.setattr(session_hooks, "monotonic", lambda: 10.25)
    monkeypatch.setattr(
        session_hooks,
        "_observe_transaction_span",
        lambda duration, *, slow: observed.append((duration, slow)),
    )

    session_hooks._finish_root_transaction_span(session, SimpleNamespace(parent=None))

    assert observed == [(0.25, False)]
    assert "database_transaction_span_slow" not in caplog.messages


def test_database_pressure_publisher_emits_bounded_observations(monkeypatch):
    captured = {}

    def publish(domain, observations, **kwargs):
        captured.update(
            domain=domain,
            observations=list(observations),
            status=kwargs["status"],
        )
        return True

    monkeypatch.setattr(observability, "publish_state_snapshot", publish)
    service = infrastructure_health.ServiceStatus(
        name="PostgreSQL",
        status="degraded",
        response_ms=12.5,
        details={
            "total_connections": 20,
            "active_connections": 4,
            "idle_connections": 15,
            "idle_in_transaction": 1,
            "idle_in_transaction_over_60s": 1,
            "max_idle_in_transaction_seconds": 90.0,
            "waiting_on_lock": 0,
            "max_connections": 100,
            "connection_utilization_pct": 20.0,
        },
    )

    assert infrastructure_health.publish_database_pressure_snapshot(service) is True
    assert captured["domain"] == "database_pressure"
    assert captured["status"] == "degraded"
    values = {
        (item.signal, item.scope): item.value for item in captured["observations"]
    }
    assert values[("probe_success", "postgres")] == 1.0
    assert values[("total_connections", "postgres")] == 20.0
    assert values[("response_ms", "postgres")] == 12.5
    assert len(values) <= 16


def test_database_pressure_collector_never_opens_database(monkeypatch):
    from app import metrics
    from app.services.db_session_adapter import db_session_adapter

    snapshot = {
        "domain": "database_pressure",
        "status": "ok",
        "observed_at": datetime.now(UTC).isoformat(),
        "observations": [
            {"signal": "probe_success", "scope": "postgres", "value": 1},
            {"signal": "total_connections", "scope": "postgres", "value": 20},
            {"signal": "active_connections", "scope": "postgres", "value": 4},
            {"signal": "idle_connections", "scope": "postgres", "value": 15},
            {"signal": "idle_in_transaction", "scope": "postgres", "value": 1},
            {
                "signal": "idle_in_transaction_over_60s",
                "scope": "postgres",
                "value": 1,
            },
            {
                "signal": "max_idle_in_transaction_seconds",
                "scope": "postgres",
                "value": 90,
            },
            {"signal": "waiting_on_lock", "scope": "postgres", "value": 0},
            {
                "signal": "connection_utilization_pct",
                "scope": "postgres",
                "value": 20,
            },
        ],
    }
    monkeypatch.setattr(
        observability,
        "load_state_snapshot",
        lambda domain: snapshot if domain == "database_pressure" else None,
    )

    @contextmanager
    def database_access_is_a_failure(*_args, **_kwargs):
        raise AssertionError("scrape-time database access is forbidden")
        yield  # pragma: no cover

    monkeypatch.setattr(
        db_session_adapter,
        "read_session",
        database_access_is_a_failure,
    )

    families = list(metrics._DatabasePressureCollector().collect())
    by_name = {family.name: family for family in families}

    assert by_name["postgres_activity_snapshot_available"].samples[0].value == 1.0
    assert by_name["postgres_activity_probe_success"].samples[0].value == 1.0
    assert any(
        sample.labels == {"state": "active"} and sample.value == 4.0
        for sample in by_name["postgres_activity_connections"].samples
    )


def test_stale_infrastructure_task_publishes_postgres_snapshot():
    from app.tasks.monitoring_cleanup import check_stale_infrastructure

    postgres = infrastructure_health.ServiceStatus(
        name="PostgreSQL",
        status="up",
        details={"total_connections": 1},
    )

    @contextmanager
    def read_session():
        yield object()

    with (
        patch(
            "app.tasks.monitoring_cleanup.db_session_adapter.read_session",
            read_session,
        ),
        patch(
            "app.services.infrastructure_health.check_all_services",
            return_value=[postgres],
        ),
        patch(
            "app.services.infrastructure_health.publish_database_pressure_snapshot",
            return_value=True,
        ) as publish,
        patch(
            "app.services.infrastructure_health.publish_health_snapshot",
            return_value=True,
        ) as publish_health,
    ):
        result = check_stale_infrastructure()

    assert result == {
        "status": "up",
        "degraded": [],
        "checked": 1,
        "snapshot_stored": True,
    }
    publish.assert_called_once_with(postgres)
    publish_health.assert_called_once_with([postgres])


def test_stale_infrastructure_snapshot_producer_has_hard_deadline():
    from app.celery_app import celery_app

    task = celery_app.tasks["app.tasks.monitoring_cleanup.check_stale_infrastructure"]
    assert task.soft_time_limit == 50
    assert task.time_limit == 55
