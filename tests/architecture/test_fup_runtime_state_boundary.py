"""Protect the FUP runtime-state participant and its repair contract."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICES = PROJECT_ROOT / "app" / "services"
OWNER = SERVICES / "fup_state.py"
STATE_FIELDS = {
    "active_rule_id",
    "action_status",
    "speed_reduction_percent",
    "original_profile_id",
    "throttle_profile_id",
    "cap_resets_at",
    "last_evaluated_at",
    "notes",
}
ALLOWED_MUTATION_CALLERS = {
    "app/services/enforcement.py",
    "app/services/fup_enforcement.py",
    "app/services/subscription_correction.py",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _attributes(target: ast.AST) -> set[str]:
    return {node.attr for node in ast.walk(target) if isinstance(node, ast.Attribute)}


def test_fup_runtime_fields_have_one_service_writer() -> None:
    writers: set[str] = set()
    for path in SERVICES.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "FupState" not in source and "fup_state" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_attributes(target) & STATE_FIELDS for target in targets):
                writers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert writers == {"app/services/fup_state.py"}


def test_fup_runtime_participant_has_only_named_service_callers() -> None:
    callers: set[str] = set()
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if path == OWNER:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr in {
                "apply_action",
                "clear",
            } and "fup_state" in ast.unparse(node.func.value):
                callers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert callers == ALLOWED_MUTATION_CALLERS


def test_fup_runtime_participant_only_flushes_and_stages_events() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "HTTPException" not in source
    assert "datetime.now" not in source
    assert "emit_event(" in source
    assert ".with_for_update()" in source
