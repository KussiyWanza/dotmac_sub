from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/fiber_topology_connectivity_coverage.py"
COMMAND = PROJECT_ROOT / "scripts/network/audit_fiber_connectivity_coverage.py"
ADMIN = PROJECT_ROOT / "app/web/admin/network_fiber_plant.py"
TEMPLATE = PROJECT_ROOT / "templates/admin/network/fiber/connectivity_coverage.html"


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


def test_coverage_owner_has_no_topology_or_workflow_mutation_authority():
    tree = _tree(SERVICE)
    source = SERVICE.read_text()
    imported = _imported_names(tree)
    db_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
    }

    for forbidden_import in (
        "propose_connectivity_batch",
        "attest_connectivity_batch",
        "execute_connectivity_batch",
        "reconcile_connectivity_batch",
        "propose_connectivity_decision",
        "approve_connectivity_decision",
        "execute_connectivity_decision",
        "finalize_connectivity_decision",
        "approve_request",
    ):
        assert forbidden_import not in imported
    assert db_calls.isdisjoint(
        {"add", "delete", "flush", "commit", "rollback", "execute"}
    )
    for constructor in (
        "FiberTopologyConnectivityDecision(",
        "FiberTopologyConnectivityProposalBatch(",
        "FiberTopologyConnectivityBatchReview(",
        "FiberTopologyConnectivityRun(",
        "FiberSegment(",
        "FiberChangeRequest(",
    ):
        assert constructor not in source
    for forbidden_inference in (
        "ST_Distance",
        "nearest",
        "snap_to",
        "gps_latitude",
        "gps_longitude",
    ):
        assert forbidden_inference not in source


def test_coverage_cli_is_exhaustive_read_only_report_only():
    tree = _tree(COMMAND)
    commands = {
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

    assert commands == set()
    # The read-only guarantee now comes from the shared seam, which pins
    # REPEATABLE READ and postgresql_readonly together. Asserting the raw
    # SQL would require the broken form this repair removed.
    assert "read_only_snapshot_session()" in source
    assert "--limit" not in source
    assert "--profile" not in source
    for forbidden in ("propose", "approve", "execute", "reconcile", "apply"):
        assert f'add_parser("{forbidden}"' not in source


def test_admin_projection_is_get_only_and_cannot_hide_a_limited_audit():
    admin_source = ADMIN.read_text()
    template_source = TEMPLATE.read_text()

    assert '"/fiber-connectivity-coverage"' in admin_source
    assert "reconcile_fiber_connectivity_coverage(db)" in admin_source
    assert "coverage.paths[:250]" in template_source
    assert "gates always use the complete cohort" in template_source
    for forbidden_form in ("<form", 'type="submit"', "bulk approve"):
        assert forbidden_form not in template_source.lower()


def test_phase17_adds_no_schema_migration():
    assert not list(
        (PROJECT_ROOT / "alembic/versions").glob("345*fiber*connectivity*coverage*.py")
    )
