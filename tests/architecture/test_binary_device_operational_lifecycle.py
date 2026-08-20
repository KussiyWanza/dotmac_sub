"""Enforce the binary, permanently verified device operational lifecycle."""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.domain_settings import SettingDomain
from app.services.device_operational_status import DeviceOperationalState
from app.services.scheduler import PERMANENT_LIFECYCLE_TASKS
from app.services.settings_spec import get_spec
from app.services.status_presentation import device_operational_status_presentation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_PATH = PROJECT_ROOT / "app/services/scheduler_config.py"

PERMANENT_VERIFICATION_TASKS = frozenset(
    {
        "app.tasks.monitoring_coverage.refresh_monitoring_coverage",
        "app.tasks.monitoring_cleanup.sync_inventory_to_monitoring",
        "app.tasks.channel_health.observe_channel_health",
        "app.tasks.device_projection.reconcile_device_projections",
    }
)
RETIRED_CONTROLS = (
    "monitoring_coverage_enabled",
    "monitoring_inventory_sync_enabled",
    "channel_health_enabled",
)
PUBLIC_OPERATIONAL_SURFACES = (
    "app/schemas/network_monitoring.py",
    "app/services/status_presentation.py",
    "app/services/network_device_status_presentation.py",
    "app/services/device_projection_views.py",
    "app/services/web_network_core_devices_inventory.py",
    "app/services/web_network_monitoring.py",
    "app/services/network_map.py",
    "templates/admin/network/devices/index.html",
    "templates/admin/network/monitoring/_kpi_partial.html",
    "templates/admin/network/monitoring/index.html",
    "templates/admin/network/olts/index.html",
    "templates/admin/network/onts/index.html",
    "templates/admin/network/onts/detail.html",
    "templates/admin/network/onts/_hero_header.html",
    "templates/admin/network/onts/_tab_overview.html",
    "templates/admin/network/onts/_topology_partial.html",
    "templates/admin/network/performance/index.html",
)


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_public_device_operational_vocabulary_is_exactly_binary() -> None:
    assert {state.value for state in DeviceOperationalState} == {
        "working",
        "not_working",
    }
    working = device_operational_status_presentation("working")
    not_working = device_operational_status_presentation("not_working")
    assert (working.label, working.tone.value, working.icon.value) == (
        "Working",
        "positive",
        "check",
    )
    assert (
        not_working.label,
        not_working.tone.value,
        not_working.icon.value,
    ) == ("Not working", "negative", "x")


def test_public_surfaces_do_not_expose_freshness_as_device_state() -> None:
    source = "\n".join(
        (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for relative in PUBLIC_OPERATIONAL_SURFACES
    )
    for forbidden in (
        "operational_retry_pending",
        "runtime_retry_pending",
        "devices_retry_pending",
        "network_devices_retry_pending",
        "Refresh pending",
        "Status refresh pending",
    ):
        assert forbidden not in source


def test_verification_inputs_and_projection_repair_are_permanent() -> None:
    assert PERMANENT_VERIFICATION_TASKS <= PERMANENT_LIFECYCLE_TASKS

    scheduler_tree = ast.parse(
        SCHEDULER_PATH.read_text(encoding="utf-8"),
        filename=str(SCHEDULER_PATH),
    )
    enabled_by_task: dict[str, ast.expr | None] = {}
    for node in ast.walk(scheduler_tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "_sync_scheduled_task":
            continue
        kwargs = {item.arg: item.value for item in node.keywords}
        task = kwargs.get("task_name")
        if isinstance(task, ast.Constant) and isinstance(task.value, str):
            enabled_by_task[task.value] = kwargs.get("enabled")

    for task_name in PERMANENT_VERIFICATION_TASKS:
        enabled = enabled_by_task[task_name]
        assert isinstance(enabled, ast.Constant)
        assert enabled.value is True


def test_verification_disable_controls_are_retired() -> None:
    scheduler_source = SCHEDULER_PATH.read_text(encoding="utf-8")
    settings_source = (PROJECT_ROOT / "app/services/settings_spec.py").read_text(
        encoding="utf-8"
    )
    for key in RETIRED_CONTROLS:
        assert key not in scheduler_source
        assert key not in settings_source
        assert get_spec(SettingDomain.network_monitoring, key) is None

    migration = (
        PROJECT_ROOT / "alembic/versions/416_binary_device_operational_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "415_permanent_lifecycle_drainage"' in migration
    assert "ck_device_projection_binary_operational_status" in migration
    for key in RETIRED_CONTROLS:
        assert key in migration


def test_ont_operational_filter_does_not_alias_raw_olt_status() -> None:
    crud_source = (PROJECT_ROOT / "app/services/network/ont_crud.py").read_text(
        encoding="utf-8"
    )
    assert "operational_status: str | None = None" in crud_source
    assert 'operational_status in {"working", "not_working"}' in crud_source
    assert '"online", "offline"' not in crud_source


def test_map_and_dashboard_consume_binary_ont_projection() -> None:
    map_source = (PROJECT_ROOT / "app/services/network_map.py").read_text(
        encoding="utf-8"
    )
    monitoring_source = (
        PROJECT_ROOT / "app/services/web_network_monitoring.py"
    ).read_text(encoding="utf-8")
    map_template = (PROJECT_ROOT / "templates/admin/network/map.html").read_text(
        encoding="utf-8"
    )
    monitoring_template = (
        PROJECT_ROOT / "templates/admin/network/monitoring/_kpi_partial.html"
    ).read_text(encoding="utf-8")

    assert "derive_ont_operational_status" in map_source
    assert "resolve_effective_ont_status" not in map_source
    assert "onts_online" not in map_source + map_template
    assert "onts_offline" not in map_source + map_template
    assert "ont_olt_link_summary" not in monitoring_source + monitoring_template
    assert "ont_service_summary.online" not in monitoring_template
    assert "ont_service_summary.offline" not in monitoring_template
    assert '"working": 0' in monitoring_source
    assert '"not_working": 0' in monitoring_source
    monitoring_page = (
        PROJECT_ROOT / "templates/admin/network/monitoring/index.html"
    ).read_text(encoding="utf-8")
    assert "site.working_pct" in monitoring_page
    assert "site.reachable_pct" not in monitoring_page


def test_topology_keeps_device_operation_separate_from_asset_lifecycle() -> None:
    service_source = (
        PROJECT_ROOT / "app/services/web_network_ont_topology.py"
    ).read_text(encoding="utf-8")
    template_source = (
        PROJECT_ROOT / "templates/admin/network/onts/_topology_partial.html"
    ).read_text(encoding="utf-8")

    assert "derive_ont_operational_status" in service_source
    assert "derive_olt_operational_status" in service_source
    assert "operational_status" in service_source + template_source
    assert "lifecycle_status" in service_source + template_source
    assert "node.status" not in template_source
