"""Prevent scheduler booleans from becoming a parallel decision system."""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import control_registry
from app.services.control_registry import Layer
from app.services.scheduler import PERMANENT_LIFECYCLE_TASKS
from app.services.settings_spec import (
    SCHEDULER_BOOLEAN_SETTING_KEYS,
    get_spec,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_PATH = PROJECT_ROOT / "app/services/scheduler_config.py"
DEVICE_PROJECTION_TASK = "app.tasks.device_projection.reconcile_device_projections"


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


def test_every_scheduler_setting_boolean_is_registered() -> None:
    calls = {
        _setting_key(node)
        for node in ast.walk(_scheduler_tree())
        if isinstance(node, ast.Call)
        and _call_name(node) == "_scheduler_setting_enabled"
    }

    assert calls == SCHEDULER_BOOLEAN_SETTING_KEYS
    for domain, key in calls:
        spec = get_spec(domain, key)
        assert spec is not None
        assert spec.value_type is SettingValueType.boolean
        assert isinstance(spec.default, bool)


def test_every_scheduler_feature_boolean_uses_a_canonical_control() -> None:
    controls = {control.key: control for control in control_registry.all_controls()}
    keys: set[str] = set()
    for node in ast.walk(_scheduler_tree()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "control_registry"
            and node.func.attr == "is_enabled"
        ):
            continue
        assert len(node.args) == 2
        key_node = node.args[1]
        assert isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)
        keys.add(key_node.value)

    assert keys
    for key in keys:
        assert key in controls
        assert controls[key].layer is Layer.feature


def test_scheduler_has_no_legacy_boolean_fallback() -> None:
    source = SCHEDULER_PATH.read_text(encoding="utf-8")

    assert "_effective_bool" not in source
    assert "_env_bool" not in source
    assert "control_for_legacy" not in source


def test_device_projection_repair_is_permanent_and_uncontrolled() -> None:
    assert DEVICE_PROJECTION_TASK in PERMANENT_LIFECYCLE_TASKS

    matched = False
    for node in ast.walk(_scheduler_tree()):
        if not isinstance(node, ast.Call) or _call_name(node) != "_sync_scheduled_task":
            continue
        kwargs = {item.arg: item.value for item in node.keywords}
        task_node = kwargs.get("task_name")
        if not (
            isinstance(task_node, ast.Constant)
            and task_node.value == DEVICE_PROJECTION_TASK
        ):
            continue
        enabled_node = kwargs.get("enabled")
        assert isinstance(enabled_node, ast.Constant)
        assert enabled_node.value is True
        matched = True
    assert matched

    sources = "\n".join(
        (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "app/services/scheduler_config.py",
            "app/services/settings_spec.py",
            "app/services/settings_seed.py",
            "app/services/control_registry.py",
        )
    )
    assert "device_projection_reconcile_enabled" not in sources
    assert "DEVICE_PROJECTION_RECONCILE_ENABLED" not in sources

    migration = (
        PROJECT_ROOT / "alembic/versions/414_permanent_device_projection.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "413_audit_actor_label"' in migration
    assert "DELETE FROM domain_settings" in migration
    assert "UPDATE scheduled_tasks SET enabled = true" in migration
