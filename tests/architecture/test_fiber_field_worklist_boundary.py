from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/fiber_topology_field_worklist.py"
COMMAND = PROJECT_ROOT / "scripts/network/audit_fiber_field_verification.py"
ADMIN = PROJECT_ROOT / "app/web/admin/network_fiber_plant.py"
TEMPLATE = (
    PROJECT_ROOT / "templates/admin/network/fiber/field_verification_worklist.html"
)


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


def test_worklist_owner_is_exhaustive_read_only_evidence_projection():
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

    assert db_calls.isdisjoint(
        {"add", "delete", "flush", "commit", "rollback", "execute"}
    )
    for forbidden_import in (
        "WorkOrder",
        "WorkOrderAssignmentQueue",
        "FiberTopologyFieldObservation",
        "FiberTopologyIdentityDecision",
        "FiberTopologyConnectivityDecision",
        "FiberChangeRequest",
        "propose_identity_decision",
        "propose_connectivity_decision",
        "record_fiber_field_observation",
    ):
        assert forbidden_import not in imported
    for forbidden_constructor in (
        "WorkOrder(",
        "WorkOrderAssignmentQueue(",
        "FiberTopologyFieldObservation(",
        "FiberTopologyIdentityDecision(",
        "FiberTopologyConnectivityDecision(",
        "FiberChangeRequest(",
    ):
        assert forbidden_constructor not in source
    assert "SOURCE_ASSET_TYPES" in source
    assert "--profile" not in source
    assert "--limit" not in source
    assert "ready_for" not in source


def test_worklist_cli_is_full_cohort_read_only_and_has_no_readiness_exit():
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
    assert "return 0" in source
    assert "return 2" not in source
    assert "--limit" not in source
    assert "--profile" not in source
    for forbidden in ("propose", "approve", "execute", "reconcile", "apply"):
        assert f'add_parser("{forbidden}"' not in source


def test_worklist_admin_is_get_only_bounded_display_of_complete_report():
    admin_source = ADMIN.read_text()
    template_source = TEMPLATE.read_text()

    assert '"/fiber-field-verification"' in admin_source
    assert "get_fiber_field_worklist_page" in admin_source
    assert "worklist_rows" in template_source
    assert "worklist.rows[:500]" not in template_source
    assert "list_pagination" in template_source
    assert "latest-staged-source-identities" in template_source
    assert "always use the complete cohort" in template_source
    assert "This worklist has no job writer" in template_source
    for forbidden_form in ('type="submit"', "bulk approve"):
        assert forbidden_form not in template_source.lower()


def test_phase20_adds_no_schema_migration():
    assert not list(
        (PROJECT_ROOT / "alembic/versions").glob("346*fiber*field*worklist*.py")
    )
