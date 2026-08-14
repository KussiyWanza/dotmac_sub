from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/crm_network_map_point_migration.py"
SCRIPT = PROJECT_ROOT / "scripts/network/crm_network_map_point_migration.py"


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
