from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_registry.registry import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app/services/network/forwarding_topology.py"
COLLECTOR = PROJECT_ROOT / "app/services/network/forwarding_observation_collector.py"
COLLECTOR_TASK = PROJECT_ROOT / "app/tasks/forwarding_control_observations.py"
MODEL = PROJECT_ROOT / "app/models/forwarding_topology.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/360_forwarding_topology_declarations.py"
COMMAND = PROJECT_ROOT / "scripts/network/review_forwarding_topology.py"
CUSTOMER_PATH = PROJECT_ROOT / "app/services/topology/customer_path.py"
AFFECTED = PROJECT_ROOT / "app/services/topology/affected.py"
REACHABILITY = PROJECT_ROOT / "app/services/topology/reachability.py"
OUTAGE = PROJECT_ROOT / "app/services/topology/outage.py"
OUTAGE_RECONCILE = PROJECT_ROOT / "app/services/topology/outage_reconcile.py"
SOT_MAP = PROJECT_ROOT / "docs/SOT_RELATIONSHIP_MAP.md"


def _constructors(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_forwarding_owner_is_the_only_application_writer():
    expected = {
        "ForwardingTopologyDecision",
        "ForwardingTopologyDeclaration",
        "ForwardingControlObservation",
    }
    writers: dict[str, set[str]] = {}
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        found = _constructors(path).intersection(expected)
        if found:
            writers[str(path.relative_to(PROJECT_ROOT))] = found

    assert writers == {"app/services/network/forwarding_topology.py": expected}


def test_owner_separates_reviewed_declaration_observation_and_projection():
    source = OWNER.read_text(encoding="utf-8")

    for required in (
        "preview_forwarding_topology_decision",
        "proposer cannot review",
        "with_for_update",
        "expected_topology_sha256",
        "declaration_sha256",
        "result_payload",
        "result_sha256",
        "record_forwarding_control_observation",
        "reconcile_forwarding_topology",
        "project_authoritative_forwarding_graph",
        "resolve_authoritative_upstream_chain",
        "missing_observation",
        "invalid_declaration",
        "online_session_observation_only",
        "stage_audit_event(",
    ):
        assert required in source
    for forbidden in (
        "DeviceRole.core",
        "configure_router",
        "apply_router",
        "send_command",
        "ST_Distance",
        "nearest",
    ):
        assert forbidden not in source


def test_operational_consumers_no_longer_traverse_lldp_directly():
    consumers = (CUSTOMER_PATH, AFFECTED, REACHABILITY, OUTAGE, OUTAGE_RECONCILE)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in consumers)

    assert "resolve_authoritative_upstream_chain" in CUSTOMER_PATH.read_text()
    assert "project_authoritative_forwarding_graph" in AFFECTED.read_text()
    assert "forwarding_graph_projection" in REACHABILITY.read_text()
    for forbidden in (
        "lldp_adjacency",
        "LLDP_SOURCE",
        "NetworkTopologyLink",
        "DeviceRole.core",
    ):
        assert forbidden not in combined


def test_model_and_migration_enforce_exact_reviewed_evidence():
    model = MODEL.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")

    for required in (
        "uq_forwarding_topology_active_decision_path",
        "uq_forwarding_topology_active_path_key",
        "uq_forwarding_topology_active_preference",
        "ck_forwarding_topology_decision_review_separation",
        "ck_forwarding_topology_decision_result_evidence",
        "ck_forwarding_topology_declaration_shape",
        "ck_forwarding_control_observation_shape",
    ):
        assert required in model
        assert required in migration
    assert 'revision = "362_forwarding_topology_declarations"' in migration
    assert 'down_revision = "361_splitter_cascade_links"' in migration


def test_operator_command_is_thin_and_cannot_apply_router_configuration():
    source = COMMAND.read_text(encoding="utf-8")

    for command in (
        "preview",
        "propose",
        "approve",
        "decline",
        "execute",
        "inspect",
        "audit",
    ):
        assert f'"{command}"' in source
    assert "expected-decision-sha256" in source
    assert "reconcile_forwarding_topology(" in source
    assert "ForwardingTopologyDeclaration(" not in source
    assert "ForwardingControlObservation(" not in source
    assert "db.add(" not in source
    assert "db.commit(" not in source
    assert "apply_router" not in source


def test_routeros_collector_is_a_read_only_observation_adapter():
    collector = COLLECTOR.read_text(encoding="utf-8")
    task = COLLECTOR_TASK.read_text(encoding="utf-8")

    assert "record_forwarding_control_observation(" in collector
    assert (
        'RouterConnectionService.execute(\n                router,\n                "GET",'
        in collector
    )
    assert "ForwardingControlObservation(" not in collector
    assert "ForwardingTopologyDeclaration(" not in collector
    assert "db.add(" not in collector
    assert "db.commit(" not in collector
    for forbidden in (
        '"POST"',
        '"PATCH"',
        '"PUT"',
        '"DELETE"',
        "apply_router",
        "execute_config_push",
        "NetworkDevice.role",
    ):
        assert forbidden not in collector
    assert "network.forwarding_observation_collection" in task
    assert "collect_forwarding_control_observations(" in task


def test_forwarding_owner_is_checked_in_to_the_sot_registry_and_map():
    owner = service_relationship("network.forwarding_topology")
    sot_map = SOT_MAP.read_text(encoding="utf-8")

    assert "reviewed downstream-to-upstream forwarding declarations" in owner.owns
    assert "`network.forwarding_topology`" in sot_map
    assert "LLDP, BGP, routing-table, and RADIUS" in sot_map
