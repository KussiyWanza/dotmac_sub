"""Keep recurring dashboard requests off live infrastructure probes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_dashboard_reads_only_the_scheduled_infrastructure_snapshot() -> None:
    dashboard = _source("app/services/web_admin_dashboard.py")
    task = _source("app/tasks/monitoring_cleanup.py")

    assert "infrastructure_health_service.check_all_services(" not in dashboard
    assert "infrastructure_health_service.load_health_snapshot()" in dashboard
    assert "publish_health_snapshot(services)" in task
