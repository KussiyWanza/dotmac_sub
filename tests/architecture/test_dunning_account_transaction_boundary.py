"""Scheduled dunning isolates account roots without participant savepoints."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "app" / "services" / "collections" / "_core.py"


def test_dunning_run_has_no_nested_transaction_escape_hatch() -> None:
    source = CORE.read_text(encoding="utf-8")
    assert "begin_nested" not in source
    tree = ast.parse(source)
    workflow = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DunningWorkflow"
    )
    run = next(
        node
        for node in workflow.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    calls = {
        node.func.attr
        for node in ast.walk(run)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "begin_nested" not in calls
    assert {"commit", "rollback"} <= calls


def test_dunning_exposes_and_persists_account_local_failures() -> None:
    source = CORE.read_text(encoding="utf-8")
    assert "DunningAccountRunCommand" in source
    assert "dunning_account_failed" in source
    assert "errors=errors" in source
