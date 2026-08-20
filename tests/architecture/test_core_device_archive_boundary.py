"""Archive lifecycle stays behind its typed owner and reversible contract."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app" / "services" / "core_device_archive.py"
ROUTE = ROOT / "app" / "web" / "admin" / "network_core_devices.py"
CONSOLIDATED_ROUTE = ROOT / "app" / "web" / "admin" / "network.py"
MONITORING_API = ROOT / "app" / "api" / "domains_monitoring.py"
LEGACY_MUTATIONS = ROOT / "app" / "services" / "web_network_core_devices_forms.py"
MIGRATION = ROOT / "alembic" / "versions" / "535_core_device_archive.py"
DETAIL_TEMPLATE = (
    ROOT / "templates" / "admin" / "network" / "core-devices" / "detail.html"
)


def test_archive_owner_is_fully_registered() -> None:
    owner = service_relationship("network.core_device_archive")
    assert owner.module == "app.services.core_device_archive"
    assert "reviewed core device archive and restoration" in owner.owns
    assert owner.is_contracted
    assert owner.contract is not None
    assert owner.contract.concerns

    presentation_owner = service_relationship("ui.network_device_status_presentation")
    assert presentation_owner.module == (
        "app.services.network_device_status_presentation"
    )
    assert presentation_owner.is_contracted


def test_web_adapter_delegates_without_writing_archive_fields() -> None:
    tree = ast.parse(ROUTE.read_text(encoding="utf-8"))
    forbidden = {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and target.attr in {"archived_at", "archived_by", "archive_reason"}
    }
    assert forbidden == set()
    source = ROUTE.read_text(encoding="utf-8")
    assert "ArchiveCoreDeviceCommand(" in source
    assert "RestoreCoreDeviceCommand(" in source
    assert ".commit(" not in source


def test_archive_is_reversible_and_never_raw_deletes_the_device() -> None:
    source = OWNER.read_text(encoding="utf-8")
    assert "restore_core_device" in source
    assert "db.delete(" not in source
    assert "expected_preview_fingerprint" in source
    assert "with_for_update()" in source


def test_schema_and_detail_surface_preserve_reversible_state() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "534_session_party_projection"' in migration
    for field in ("archived_at", "archived_by", "archive_reason"):
        assert field in migration
    assert "network:device:archive" in migration
    assert "Restore all archived core devices before downgrading" in migration

    template = DETAIL_TEMPLATE.read_text(encoding="utf-8")
    assert "This device is decommissioned and read-only." in template
    assert "Decommission Device" in template
    assert 'action="/admin/network/core-devices/{{ device.id }}/restore"' in template


def test_every_legacy_core_device_mutation_uses_archive_policy() -> None:
    tree = ast.parse(LEGACY_MUTATIONS.read_text(encoding="utf-8"))
    guarded_functions = {
        "update_device",
        "update_provisioning_access_for_device",
        "toggle_interface_monitored",
        "create_bandwidth_graph_for_device",
        "add_bandwidth_graph_source",
        "toggle_bandwidth_graph_public",
        "clone_bandwidth_graph_for_device",
        "update_backup_settings_for_device",
        "trigger_backup_for_core_device",
    }
    functions = {
        node.name: ast.get_source_segment(
            LEGACY_MUTATIONS.read_text(encoding="utf-8"), node
        )
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in guarded_functions
    }
    assert functions.keys() == guarded_functions
    for name, source in functions.items():
        assert source is not None, name
        assert (
            "_mutable_device(" in source or "require_core_device_mutable(" in source
        ), name

    consolidated = CONSOLIDATED_ROUTE.read_text(encoding="utf-8")
    for name in ("device_ping", "device_reboot", "device_reboot_preview"):
        node = next(
            item
            for item in ast.parse(consolidated).body
            if isinstance(item, ast.FunctionDef) and item.name == name
        )
        source = ast.get_source_segment(consolidated, node)
        assert source is not None
        assert "_require_core_device_action_allowed(" in source

    monitoring_api = MONITORING_API.read_text(encoding="utf-8")
    monitoring_tree = ast.parse(monitoring_api)
    for name in (
        "update_network_device",
        "delete_network_device",
        "create_device_interface",
        "update_device_interface",
        "delete_device_interface",
    ):
        node = next(
            item
            for item in monitoring_tree.body
            if isinstance(item, ast.FunctionDef) and item.name == name
        )
        source = ast.get_source_segment(monitoring_api, node)
        assert source is not None
        assert "_require_core_device_mutable(" in source
