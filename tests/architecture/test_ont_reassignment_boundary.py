from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_change_ont_route_is_adapter_only() -> None:
    source = (PROJECT_ROOT / "app/web/admin/catalog.py").read_text()
    start = source.index("def catalog_subscription_change_ont_submit")
    end = source.index("@router.post", start + 1)
    route_source = source[start:end]

    assert "reassign_subscription_ont_from_form(" in route_source
    assert "OntAssignment(" not in route_source
    assert ".active =" not in route_source
    assert ".ont_unit_id =" not in route_source
    assert ".subscription_id =" not in route_source
    assert ".subscriber_id =" not in route_source
    assert ".commit(" not in route_source
    assert ".rollback(" not in route_source


def test_reassignment_owner_uses_command_boundary_and_flush_only_helpers() -> None:
    source = (
        PROJECT_ROOT / "app/services/network/ont_assignment_commands.py"
    ).read_text()
    start = source.index("def reassign_active_ont")
    end = source.index("    def assign", start)
    owner_source = source[start:end]

    assert "execute_owner_command(" in owner_source
    assert "commit=False" in owner_source
    assert ".commit(" not in owner_source
