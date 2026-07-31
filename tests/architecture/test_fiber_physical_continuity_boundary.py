from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL = PROJECT_ROOT / "app/models/fiber_physical.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/364_fiber_core_continuity.py"
OWNER = PROJECT_ROOT / "app/services/network/fiber_physical_continuity.py"
CHANGE_OWNER = PROJECT_ROOT / "app/services/fiber_change_requests.py"
FIELD_ADAPTER = PROJECT_ROOT / "app/services/field/fiber.py"
VENDOR_ADAPTER = PROJECT_ROOT / "app/services/vendor_fiber.py"
SPLICE_INTAKE = PROJECT_ROOT / "app/services/network/fiber_splice_proposals.py"
LEGACY_ADAPTER = PROJECT_ROOT / "app/services/network/fiber_services.py"
ACCESS_PATH = PROJECT_ROOT / "app/services/network/access_path.py"


def _constructors(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_migration_adds_empty_exact_rack_patch_and_core_inventory():
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "364_fiber_core_continuity"' in source
    assert 'down_revision = "363_fiber_plant_operational_integrity"' in source
    for table in (
        "fiber_racks",
        "fiber_patch_panels",
        "fiber_connector_ports",
        "fiber_physical_link_decisions",
        "fiber_core_splices",
        "fiber_strand_terminations",
        "fiber_patch_cords",
    ):
        assert f'"{table}"' in source
    for constraint in (
        "ck_fiber_racks_exact_host",
        "ck_fiber_patch_panels_positive_capacity",
        "ck_fiber_connector_ports_exact_owner",
        "ck_fiber_physical_link_decisions_review_evidence",
        "ck_fiber_physical_link_decisions_result_evidence",
        "uq_fiber_strand_terminations_active_end",
        "uq_fiber_patch_cords_active_first_connector",
    ):
        assert constraint in source
    assert "UPDATE fiber_splices" not in source
    assert "INSERT INTO fiber_core_splices" not in source


def test_physical_owner_is_the_only_application_writer_for_exact_links():
    expected = {"FiberCoreSplice", "FiberStrandTermination", "FiberPatchCord"}
    writers: dict[str, set[str]] = {}
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        found = _constructors(path).intersection(expected)
        if found:
            writers[str(path.relative_to(PROJECT_ROOT))] = found

    assert writers == {
        "app/services/network/fiber_physical_continuity.py": expected,
    }


def test_owner_requires_exact_preview_review_execution_and_result_evidence():
    source = OWNER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    accessed_attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for required in (
        "preview_physical_link",
        "propose_physical_link",
        "approve_physical_link",
        "execute_physical_link",
        "with_for_update",
        "the proposer cannot review",
        "decision_sha256",
        "result_payload",
        "result_sha256",
        "strand_end",
        "patch_panel_id",
        "rack_unit_start",
        "port_capacity",
        "splice_type",
    ):
        assert required in source
    for forbidden in ("ST_Distance", "nearest", "proximity"):
        assert forbidden not in source
    assert "fiber_strand_id" not in accessed_attributes
    assert "FiberSplice," not in source


def test_connector_inventory_is_single_channel_until_lane_model_exists():
    model_source = MODEL.read_text(encoding="utf-8")
    migration_source = MIGRATION.read_text(encoding="utf-8")
    owner_source = OWNER.read_text(encoding="utf-8")

    assert 'CONNECTOR_TYPES = ("sc", "lc", "fc", "st")' in owner_source
    assert "explicit assembly and lane model" in owner_source
    for source in (model_source, migration_source):
        assert "connector_type IN ('sc', 'lc', 'fc', 'st')" in source
    assert "One optical channel" in model_source
    assert "assembly_label" in model_source


def test_field_and_change_request_adapters_delegate_exact_splice_decisions():
    intake_source = SPLICE_INTAKE.read_text(encoding="utf-8")
    field_source = FIELD_ADAPTER.read_text(encoding="utf-8")
    vendor_source = VENDOR_ADAPTER.read_text(encoding="utf-8")
    change_source = CHANGE_OWNER.read_text(encoding="utf-8")
    legacy_source = LEGACY_ADAPTER.read_text(encoding="utf-8")

    for required in (
        "from_strand_end",
        "to_strand_end",
        "physical_link_decision_id",
        "propose_physical_link(",
    ):
        assert required in intake_source
    # Field and vendor transports delegate splice intake to the shared owner
    # and never talk to the continuity owner directly.
    for transport_source in (field_source, vendor_source):
        assert "fiber_splice_proposals.propose_splice(" in transport_source
        assert "propose_physical_link(" not in transport_source
    assert "approve_physical_link(" in change_source
    assert "execute_physical_link(" in change_source
    change_tree = ast.parse(change_source)
    physical_review_calls = [
        node
        for node in ast.walk(change_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {
            "approve_physical_link",
            "decline_physical_link",
            "execute_physical_link",
        }
    ]
    assert len(physical_review_calls) == 3
    assert all(
        any(
            keyword.arg == "commit"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
        for call in physical_review_calls
    )
    assert "with db.begin_nested()" in OWNER.read_text(encoding="utf-8")
    assert "created_asset: Any = model(**payload)" in change_source
    assert 'if normalized == "fiber_splice"' in change_source
    assert "_retired_legacy_splice_mutation()" in legacy_source
    assert "Direct legacy splice mutation is retired" in legacy_source


def test_composed_access_path_requires_exact_physical_core_continuity():
    source = ACCESS_PATH.read_text(encoding="utf-8")

    for required in (
        "resolve_subscription_core_continuity",
        "core_continuity_complete",
        "core_continuity_sha256",
        'domain="physical_core"',
        "and core_continuity.complete",
    ):
        assert required in source
