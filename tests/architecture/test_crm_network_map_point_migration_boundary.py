from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/crm_network_map_point_migration.py"
SCRIPT = PROJECT_ROOT / "scripts/network/crm_network_map_point_migration.py"
STAGING_SCRIPT = PROJECT_ROOT / "scripts/network/stage_crm_network_map.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def test_crm_point_migration_uses_existing_identity_and_asset_owners():
    source = SERVICE.read_text()

    assert "propose_identity_batch" in source
    assert "execute_identity_batch" in source
    assert "FdhCabinet(" not in source
    assert "FiberAccessPoint(" not in source
    assert "FiberSpliceClosure(" not in source
    assert "FiberTopologyIdentityDecision(" not in source
    assert "FiberTopologyAssetSourceLink(" not in source
    assert "stage_fiber_preview_batch" not in source
    assert "extract_crm_network_map" not in source


def test_crm_point_migration_cli_keeps_stages_explicit_and_has_no_startup_hook():
    tree = _tree(SCRIPT)
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
    source = SCRIPT.read_text()

    assert commands == {
        "apply-approved",
        "dry-run-apply",
        "preview-proposals",
        "propose-batch",
        "report",
        "select",
    }
    assert "snapshot" not in commands
    assert "stage" not in commands
    assert 'if __name__ == "__main__"' in source
    assert "scheduler" not in source.casefold()
    assert "startup" not in source.casefold()


def test_crm_point_migration_cli_separates_read_and_write_session_capabilities():
    source = SCRIPT.read_text()

    assert "READ_ONLY_COMMANDS = frozenset(" in source
    assert '{"report", "select", "preview-proposals", "dry-run-apply"}' in source
    assert 'WRITE_COMMANDS = frozenset({"propose-batch", "apply-approved"})' in source
    assert "with read_only_snapshot_session() as db:" in source
    assert "with db_session_adapter.owner_command_session() as db:" in source

    read_command = source.split("def _run_read_command", 1)[1].split(
        "def _run_write_command", 1
    )[0]
    write_command = source.split("def _run_write_command", 1)[1].split("def main", 1)[0]
    assert "propose_crm_point_identity_proposals" not in read_command
    assert "execute_crm_point_identity_apply" not in read_command
    assert "read_only_snapshot_session" not in write_command


def test_crm_staging_uses_snapshot_capture_time_not_execution_time():
    source = STAGING_SCRIPT.read_text()

    assert '"--snapshot-captured-at"' in source
    assert "snapshot_timestamp = args.snapshot_captured_at.isoformat()" in source
    assert "snapshot_timestamp = datetime.now" not in source
    assert '"importer_version": "stage_crm_network_map:v2"' in source
