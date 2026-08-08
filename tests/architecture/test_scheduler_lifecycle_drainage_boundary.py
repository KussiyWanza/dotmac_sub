"""Keep accepted lifecycle work independent from mutable scheduler controls."""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.scheduler import PERMANENT_LIFECYCLE_TASKS
from app.services.settings_spec import (
    SCHEDULER_ENV_BOOTSTRAP_SETTING_KEYS,
    get_spec,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_PATH = PROJECT_ROOT / "app/services/scheduler_config.py"

REQUIRED_PERMANENT_TASKS = frozenset(
    {
        "app.tasks.provisioning.retry_pending_compensation_failures",
        "app.tasks.usage.lift_expired_fup_enforcement",
        "app.tasks.radius.reconcile_active_sessions",
        "app.tasks.radius_population.sync_device_login",
        "app.tasks.campaigns.process_due_campaigns",
        "app.tasks.campaigns.process_due_campaign_steps",
        "app.tasks.monitoring_coverage.refresh_monitoring_coverage",
        "app.tasks.monitoring_cleanup.sync_inventory_to_monitoring",
        "app.tasks.channel_health.observe_channel_health",
    }
)


def _scheduler_tree() -> ast.Module:
    return ast.parse(
        SCHEDULER_PATH.read_text(encoding="utf-8"),
        filename=str(SCHEDULER_PATH),
    )


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _setting_key(node: ast.Call) -> tuple[SettingDomain, str]:
    assert len(node.args) == 3
    domain_node = node.args[1]
    key_node = node.args[2]
    assert (
        isinstance(domain_node, ast.Attribute)
        and isinstance(domain_node.value, ast.Name)
        and domain_node.value.id == "SettingDomain"
    )
    assert isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
    # `getattr`, not `SettingDomain[...]`: the member type is an open `str`
    # subclass now, so it has no enum-style subscript lookup.
    return getattr(SettingDomain, domain_node.attr), key_node.value


def test_scheduler_tuning_uses_only_registered_typed_settings() -> None:
    calls = [
        node
        for node in ast.walk(_scheduler_tree())
        if isinstance(node, ast.Call)
        and _call_name(node) in {"resolve_integer", "resolve_string"}
    ]

    assert calls
    for call in calls:
        domain, key = _setting_key(call)
        spec = get_spec(domain, key)
        assert spec is not None
        expected = (
            SettingValueType.integer
            if _call_name(call) == "resolve_integer"
            else SettingValueType.string
        )
        assert spec.value_type is expected

    source = SCHEDULER_PATH.read_text(encoding="utf-8")
    for retired in (
        "_effective_int",
        "_resolve_int",
        "_effective_str",
        "_env_int",
        "_env_value",
        "_get_setting_value",
    ):
        assert retired not in source


def test_scheduler_environment_inputs_are_bootstrap_only() -> None:
    for domain, key in SCHEDULER_ENV_BOOTSTRAP_SETTING_KEYS:
        spec = get_spec(domain, key)
        assert spec is not None
        assert spec.env_var
        assert spec.value_type in {
            SettingValueType.boolean,
            SettingValueType.integer,
            SettingValueType.string,
        }

    # Broker endpoints are needed before database bootstrap and are explicitly
    # deployment-owned instead of mutable scheduler settings.
    assert get_spec(SettingDomain.scheduler, "broker_url") is None
    assert get_spec(SettingDomain.scheduler, "result_backend") is None
    direct_environment_keys = {
        node.args[0].value
        for node in ast.walk(_scheduler_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "getenv"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert direct_environment_keys == {
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "REDIS_URL",
    }


def test_accepted_lifecycle_and_projection_work_is_permanent() -> None:
    assert REQUIRED_PERMANENT_TASKS <= PERMANENT_LIFECYCLE_TASKS

    enabled_by_task: dict[str, ast.expr] = {}
    for node in ast.walk(_scheduler_tree()):
        if not isinstance(node, ast.Call) or _call_name(node) != "_sync_scheduled_task":
            continue
        kwargs = {item.arg: item.value for item in node.keywords}
        task = kwargs.get("task_name")
        enabled = kwargs.get("enabled")
        if isinstance(task, ast.Constant) and isinstance(task.value, str):
            enabled_by_task[task.value] = enabled

    for task_name in REQUIRED_PERMANENT_TASKS:
        enabled = enabled_by_task[task_name]
        assert isinstance(enabled, ast.Constant)
        assert enabled.value is True


def test_retired_drainage_controls_cannot_return() -> None:
    runtime_sources = "\n".join(
        (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "app/services/scheduler_config.py",
            "app/services/control_registry.py",
            "app/services/settings_spec.py",
        )
    )
    assert "provisioning.compensation_retry" not in runtime_sources
    assert "compensation_retry_enabled" not in runtime_sources
    assert "device_login_sync_enabled" not in runtime_sources

    scheduler_source = SCHEDULER_PATH.read_text(encoding="utf-8")
    assert "campaign_processing_enabled" not in scheduler_source

    campaign_source = (PROJECT_ROOT / "app/services/comms_campaigns.py").read_text(
        encoding="utf-8"
    )
    assert "_assert_periodic_campaign_admission_enabled" in campaign_source
    assert "campaign_processing_enabled" in campaign_source

    migration = (
        PROJECT_ROOT / "alembic/versions/415_permanent_lifecycle_drainage.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "414_permanent_device_projection"' in migration
    assert "status = 'paused'" in migration
    assert "UPDATE scheduled_tasks SET enabled = true" in migration
