from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_registry.registry import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/fiber_topology_cutover_readiness.py"
COMMAND = PROJECT_ROOT / "scripts/network/audit_fiber_cutover_readiness.py"
TOPOLOGY_OWNER = PROJECT_ROOT / "app/services/fiber_topology.py"
SOT_MAP = PROJECT_ROOT / "docs/SOT_RELATIONSHIP_MAP.md"
DESIGN = PROJECT_ROOT / "docs/designs/FIBER_TOPOLOGY_SOT.md"


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


def test_cutover_readiness_owner_is_read_only_and_composes_exact_owners():
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

    assert {
        "audit_fiber_topology",
        "reconcile_fiber_connectivity_coverage",
        "reconcile_fiber_field_worklist",
        "reconcile_fiber_identity_coverage",
    } <= imported
    assert "audit_fiber_topology(db, verify_customer_traces=True)" in source
    assert db_calls.isdisjoint(
        {"add", "delete", "flush", "commit", "rollback", "execute"}
    )
    for forbidden in (
        "propose_identity_decision",
        "approve_identity_decision",
        "execute_identity_decision",
        "record_fiber_field_observation",
        "approve_request",
        "create_work_order",
    ):
        assert forbidden not in imported
    for forbidden_inference in (
        "ST_Distance",
        "nearest",
        "snap_to",
        "gps_latitude",
        "gps_longitude",
    ):
        assert forbidden_inference not in source


def test_only_combined_policy_owner_names_cutover_review_readiness():
    topology_source = TOPOLOGY_OWNER.read_text()
    policy_source = SERVICE.read_text()

    assert "customer_trace_cutover_ready" not in topology_source
    assert "customer_trace_evidence_complete" in topology_source
    assert "ready_for_cutover_review" in policy_source
    assert "authorize a production cutover" in policy_source


def test_cutover_readiness_cli_is_complete_read_only_and_has_no_apply_mode():
    tree = _tree(COMMAND)
    source = COMMAND.read_text()
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

    assert commands == set()
    assert "SET TRANSACTION READ ONLY" in source
    assert "REPEATABLE READ" in source
    assert "cannot authorize or" in source
    assert "--limit" not in source
    assert "--profile" not in source
    assert "--cohort" not in source
    for forbidden in ("propose", "approve", "execute", "reconcile", "apply"):
        assert f'add_parser("{forbidden}"' not in source


def test_cutover_readiness_adds_no_schema_migration():
    assert not list(
        (PROJECT_ROOT / "alembic/versions").glob("*fiber*cutover*readiness*.py")
    )


def test_cutover_readiness_owner_and_numeric_policy_are_checked_in_sot():
    owner = service_relationship("network.fiber_cutover_readiness")
    sot_map = SOT_MAP.read_text()
    design = DESIGN.read_text()

    assert "versioned numeric fiber cutover-readiness policy" in owner.owns
    assert "`network.fiber_cutover_readiness`" in sot_map
    assert "`fiber_topology_cutover_v1`" in sot_map
    assert "100% exact-current" in sot_map
    assert "20% audit with a 25-row minimum" in sot_map
    assert "strictly above 2%" in design
    assert "cannot authorize or perform a production" in design
    assert "cutover." in design
