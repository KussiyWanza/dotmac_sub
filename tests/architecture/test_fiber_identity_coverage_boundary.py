from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/fiber_topology_identity_coverage.py"
COMMAND = PROJECT_ROOT / "scripts/network/audit_fiber_identity_coverage.py"
ADMIN = PROJECT_ROOT / "app/web/admin/network_fiber_plant.py"
TEMPLATE = PROJECT_ROOT / "templates/admin/network/fiber/identity_coverage.html"


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


def test_identity_coverage_owner_has_no_workflow_or_asset_mutation_authority():
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
        "propose_identity_batch",
        "attest_identity_batch",
        "execute_identity_batch",
        "reconcile_identity_change_requests",
        "propose_identity_decision",
        "approve_identity_decision",
        "execute_identity_decision",
        "finalize_identity_decision",
        "approve_request",
    ):
        assert forbidden_import not in imported
    assert db_calls.isdisjoint(
        {"add", "delete", "flush", "commit", "rollback", "execute"}
    )
    for constructor in (
        "FiberTopologyIdentityDecision(",
        "FiberTopologyIdentityProposalBatch(",
        "FiberTopologyIdentityBatchReview(",
        "FiberTopologyIdentityExecutionRun(",
        "FiberTopologyAssetSourceLink(",
        "FiberChangeRequest(",
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
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


def test_identity_coverage_cli_is_exhaustive_read_only_report_only():
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
    assert "decide support mounts" in source
    assert "reject-only" not in source
    assert "--limit" not in source
    assert "--profile" not in source
    for forbidden in ("propose", "approve", "execute", "reconcile", "apply"):
        assert f'add_parser("{forbidden}"' not in source


def test_identity_coverage_admin_is_get_only_and_full_cohort_gated():
    admin_source = ADMIN.read_text()
    template_source = TEMPLATE.read_text()

    assert '"/fiber-identity-coverage"' in admin_source
    assert "reconcile_fiber_identity_coverage(db)" in admin_source
    assert "coverage.assets[:250]" in template_source
    assert "gates always use the complete cohort" in template_source
    assert "decide support mounts" in template_source
    for forbidden_form in ("<form", 'type="submit"', "bulk approve"):
        assert forbidden_form not in template_source.lower()


def test_identity_coverage_adds_no_separate_schema_migration():
    assert not list(
        (PROJECT_ROOT / "alembic/versions").glob("345*fiber*identity*coverage*.py")
    )
