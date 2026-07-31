"""Huawei device mutations must converge on the desired-state reconciler."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

ACS_MUTATIONS = {"set_parameter_values", "add_object", "delete_object"}
OLT_MUTATIONS = {
    "authorize_ont",
    "deauthorize_ont",
    "create_service_port",
    "delete_service_port",
    "configure_iphost",
    "clear_iphost_config",
    "bind_tr069_profile",
    "configure_pppoe",
    "configure_internet_config",
    "configure_wan_config",
    "set_ont_description",
    "update_ont_profiles",
    "reboot_ont",
}

CANONICAL_ACS_WRITERS = {
    "app/services/genieacs_client.py",
    "app/services/network/reconcile/applier.py",
}
UNRELATED_SAME_NAME_CALLERS = {"app/services/object_storage.py"}
ACS_WRITE_BACKLOG = {
    "app/services/network/ont_action_common.py",
    "app/services/network/ont_action_network.py",
    "app/services/network/ont_action_wan.py",
    "app/services/network/ont_action_web_credentials.py",
    "app/services/network/tr069_batch_config.py",
    "app/services/provisioning_adapters.py",
}

CANONICAL_OLT_WRITERS = {
    "app/services/network/reconcile/applier.py",
}
OLT_WRITE_BACKLOG = {
    "app/api/network_olt_ops.py",
    "app/services/network/olt_api_operations.py",
    "app/services/network/ont_authorization.py",
    "app/services/network/ont_inventory.py",
    "app/services/network/ont_provision_steps.py",
    "app/services/network/ont_write.py",
    "app/services/network/provisioning_enforcement.py",
    "app/services/provisioning_step_executors.py",
    "app/services/web_network_ont_actions/config_setters.py",
    "app/services/web_network_ont_actions/device_actions.py",
    "app/services/web_network_service_ports.py",
    "app/web/admin/network_onts_actions.py",
}


def _mutation_modules(names: set[str]) -> set[str]:
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in names
            for node in ast.walk(tree)
        ):
            found.add(str(path.relative_to(ROOT)))
    return found


def test_no_new_acs_write_bypass() -> None:
    actual = _mutation_modules(ACS_MUTATIONS) - UNRELATED_SAME_NAME_CALLERS
    expected = CANONICAL_ACS_WRITERS | ACS_WRITE_BACKLOG
    assert actual == expected, (
        "ACS write ownership changed. New writers must use reconcile/applier.py; "
        "migrated writers must be removed from ACS_WRITE_BACKLOG. "
        f"Added={sorted(actual - expected)}, stale={sorted(expected - actual)}"
    )


def test_no_new_olt_write_bypass() -> None:
    actual = _mutation_modules(OLT_MUTATIONS)
    expected = CANONICAL_OLT_WRITERS | OLT_WRITE_BACKLOG
    assert actual == expected, (
        "OLT write ownership changed. New writers must use reconcile/applier.py; "
        "migrated writers must be removed from OLT_WRITE_BACKLOG. "
        f"Added={sorted(actual - expected)}, stale={sorted(expected - actual)}"
    )
