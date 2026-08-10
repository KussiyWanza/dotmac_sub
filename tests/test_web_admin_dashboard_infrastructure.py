from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import infrastructure_health, web_admin_dashboard


def test_infrastructure_service_summary_groups_operational_states():
    services = [
        SimpleNamespace(status="up"),
        SimpleNamespace(status="healthy"),
        SimpleNamespace(status="degraded"),
        SimpleNamespace(status="warning"),
        SimpleNamespace(status="down"),
        SimpleNamespace(status="failed"),
        SimpleNamespace(status="not_configured"),
    ]

    summary = web_admin_dashboard._build_infrastructure_service_summary(services)

    assert summary == {
        "total": 7,
        "up": 2,
        "degraded": 2,
        "down": 2,
        "unknown": 1,
    }


def test_dashboard_infrastructure_health_reads_scheduled_snapshot(monkeypatch):
    observed_at = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    services = (infrastructure_health.ServiceStatus(name="PostgreSQL", status="up"),)
    snapshot = infrastructure_health.InfrastructureHealthSnapshot(
        services=services,
        observed_at=observed_at,
        freshness=infrastructure_health.InfrastructureHealthFreshness.fresh,
        age_seconds=30.0,
    )
    monkeypatch.setattr(
        web_admin_dashboard.infrastructure_health_service,
        "load_health_snapshot",
        lambda: snapshot,
    )
    monkeypatch.setattr(
        web_admin_dashboard.web_system_health_service,
        "_build_worker_health",
        lambda _services: "workers",
    )

    result = web_admin_dashboard._load_dashboard_infrastructure_health(object())

    assert result[0] == list(services)
    assert result[1] == "workers"
    assert result[2]["up"] == 1
    assert result[3] is snapshot


def test_dashboard_infrastructure_health_does_not_probe_on_missing_snapshot(
    monkeypatch,
):
    monkeypatch.setattr(
        web_admin_dashboard.infrastructure_health_service,
        "load_health_snapshot",
        lambda: None,
    )

    def forbidden_probe(_db):
        raise AssertionError("dashboard request must not run dependency probes")

    monkeypatch.setattr(
        web_admin_dashboard.infrastructure_health_service,
        "check_all_services",
        forbidden_probe,
    )

    services, worker_health, summary, snapshot = (
        web_admin_dashboard._load_dashboard_infrastructure_health(object())
    )

    assert services == []
    assert worker_health["status"] == "unknown"
    assert summary == {
        "total": 0,
        "up": 0,
        "degraded": 0,
        "down": 0,
        "unknown": 0,
    }
    assert snapshot is None


def test_health_snapshot_loader_marks_old_projection_stale(monkeypatch):
    observed_at = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    payload = {
        "schema_version": 1,
        "observed_at": observed_at.isoformat(),
        "services": [
            {
                "name": "PostgreSQL",
                "status": "up",
                "version": "16",
                "response_ms": 4.5,
                "details": {"total_connections": 20},
                "checked_at": observed_at.isoformat(),
                "icon": "",
            }
        ],
    }
    monkeypatch.setattr("app.services.app_cache.get_json", lambda _key: payload)

    snapshot = infrastructure_health.load_health_snapshot(
        now=observed_at + timedelta(minutes=11)
    )

    assert snapshot is not None
    assert (
        snapshot.freshness is infrastructure_health.InfrastructureHealthFreshness.stale
    )
    assert snapshot.age_seconds == 660.0


def test_health_snapshot_publisher_enforces_service_budget():
    services = [
        infrastructure_health.ServiceStatus(name=f"service-{index}", status="up")
        for index in range(9)
    ]

    with pytest.raises(
        ValueError,
        match="Infrastructure health snapshot exceeds service budget",
    ):
        infrastructure_health.publish_health_snapshot(services)
