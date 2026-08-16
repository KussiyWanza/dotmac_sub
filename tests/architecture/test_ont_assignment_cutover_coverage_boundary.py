from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/ont_assignment_cutover_coverage.py"
COMMAND = PROJECT_ROOT / "scripts/network/audit_ont_assignment_cutover_coverage.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_coverage_owner_has_no_assignment_mutation_or_constraint_authority():
    tree = _tree(SERVICE)
    imported = _imported_names(tree)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "OntAssignment" not in imported
    assert "execute_assignment_identity_repair" not in imported
    assert "approve_assignment_identity_repair" not in imported
    assert "decline_assignment_identity_repair" not in imported
    assert "execute_assignment_identity_repair" not in calls
    assert "enable_constraint" not in calls
    assert "add" not in {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
    }


def test_coverage_cli_has_one_read_only_report_mode():
    tree = _tree(COMMAND)
    parser_commands = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    source = COMMAND.read_text()

    assert parser_commands == set()
    # The read-only guarantee now comes from the shared seam, which pins
    # REPEATABLE READ and postgresql_readonly together. Asserting the raw
    # SQL would require the broken form this repair removed.
    assert "read_only_snapshot_session()" in source
    assert "execute_assignment_identity_repair" not in source
    assert "enable_constraint" not in source
