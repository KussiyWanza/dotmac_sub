"""Keep admin setting form writes behind one atomic owner boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]
FORM_SERVICE = ROOT / "app" / "services" / "web_system_settings_forms.py"
OWNER = ROOT / "app" / "services" / "domain_settings.py"


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _called_attributes(function: ast.FunctionDef) -> list[str]:
    return [
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def _called_names(function: ast.FunctionDef) -> list[str]:
    return [
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def test_settings_form_update_owner_has_complete_atomic_contract() -> None:
    service = service_relationship("control.settings_form_updates")

    assert service.contract is not None
    assert service.contract.migration.state.value == "complete"
    assert service.contract.transaction.mode.value == "owner_managed"


def test_form_prepares_before_calling_the_single_batch_owner() -> None:
    source = FORM_SERVICE.read_text(encoding="utf-8")

    assert "_normalize_spec_setting" in source
    assert ".upsert_by_key(" not in source
    assert "apply_admin_settings_form_updates" in source


def test_batch_owner_stages_without_helper_transaction_completion() -> None:
    operation = _function(OWNER, "_apply_admin_settings_form_operation")
    boundary = _function(OWNER, "apply_admin_settings_form_updates")

    assert "stage_upsert_by_key" in _called_attributes(operation)
    assert "execute_owner_command" in _called_names(boundary)
    assert not {"commit", "rollback"} & set(_called_attributes(operation))
