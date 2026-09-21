"""Structural guarantees for the legacy completion-gate override.

Sensitivity: each test below is proven against both a planted defect (the
property it targets, deliberately reintroduced) and a planted near-miss (a
superficially similar but acceptable pattern) during review -- see the PR
description for the before/after grep transcripts.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
MIGRATION_MARKER = "602_inbox_completion_legacy_override.py"


def _assigns_named(tree: ast.AST, name: str) -> list[ast.Assign | ast.AnnAssign]:
    found: list[ast.Assign | ast.AnnAssign] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == name:
                    found.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            if node.target.attr == name:
                found.append(node)
    return found


def _calls_with_keyword(tree: ast.AST, keyword: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and any(kw.arg == keyword for kw in node.keywords)
    ]


def test_completion_gate_precutover_at_is_never_written_outside_the_migration():
    """Only the legacy-override migration may write the pre-cutover marker.

    Planted-defect proof: reintroducing
    ``conversation.completion_gate_precutover_at = ...`` anywhere under
    ``app/`` makes this test fail and names the offending file. Planted
    near-miss: reading the column (``if conversation.completion_gate_precutover_at
    is None``), or constructing an unrelated model kwarg such as
    ``completion_gate_precutover_at=value`` inside a *test* fixture, must NOT
    trip this scan -- it only walks ``app/``, and it only flags an assignment
    target, never a read or a comparison.
    """

    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _assigns_named(tree, "completion_gate_precutover_at"):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert not offenders, (
        "completion_gate_precutover_at must be written exactly once, by "
        f"alembic/versions/{MIGRATION_MARKER}'s upgrade(). Found application "
        f"writers: {offenders}"
    )


def test_no_macro_action_param_declares_an_override_grant():
    """A stored macro's ``actions[].params`` must never carry an override id.

    A stored macro carrying a grant id would make a single-use override
    reusable and durable, destroying the single-use guarantee. Only the
    operator invoking the macro at execution time may supply
    ``override_grant_id`` (a call-site argument, never a persisted macro
    field).

    Planted-defect proof: adding ``"override_grant_id": "..."`` (or any
    ``params`` key containing "override") to a macro action literal anywhere
    under ``app/`` makes this test fail. Planted near-miss: the
    ``execute_macro_actions(..., override_grant_id=...)`` call-site keyword
    argument itself must NOT trip this scan -- it only inspects dict literals
    assigned to (or nested under) a key literally named ``"params"``, not
    ordinary keyword arguments.
    """

    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "params"
                    and isinstance(value, ast.Dict)
                ):
                    for param_key in value.keys:
                        if (
                            isinstance(param_key, ast.Constant)
                            and isinstance(param_key.value, str)
                            and "override" in param_key.value.lower()
                        ):
                            offenders.append(
                                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                            )

    assert not offenders, (
        "A macro action's params must never carry an override reference "
        f"(the single-use grant would become reusable/durable): {offenders}"
    )


def test_consume_override_for_resolution_has_one_caller():
    """The completion-override consumer is reached from exactly one place.

    Mirrors ``test_agent_resolution_gate_has_one_central_enforcement_caller``:
    requirement #4 ("shared owner for direct, bulk, and macro resolution")
    depends on there being exactly one plumbing point into the gate, not a
    second parallel resolution path.
    """

    callers: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "consume_override_for_resolution"
            ) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "consume_override_for_resolution"
            ):
                callers.append(path.relative_to(ROOT).as_posix())

    assert callers == ["app/services/team_inbox_status.py"]
