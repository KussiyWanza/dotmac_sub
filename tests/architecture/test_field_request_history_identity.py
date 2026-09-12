"""Requester-owned field history must keep every canonical identity bridge."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SERVICES = {
    ROOT / "app/services/field/material_requests.py": (
        "_material_request_ownership",
        "list_requester_material_requests",
        "requested_by_technician_id",
        "requested_by_person_id",
        "requested_by_system_user_id",
    ),
    ROOT / "app/services/field/expense_requests.py": (
        "_expense_request_ownership",
        "_expense_request_ownership",
        "requested_by_technician_id",
        "requested_by_person_id",
        "requested_by_system_user_id",
    ),
}


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.relative_to(ROOT)} does not define {name}")


def test_request_history_scope_keeps_all_canonical_requester_links() -> None:
    for path, (helper_name, _delegate_name, *required_fields) in SERVICES.items():
        helper = _function(path, helper_name)
        used_fields = {
            node.attr for node in ast.walk(helper) if isinstance(node, ast.Attribute)
        }
        assert set(required_fields) <= used_fields, (
            f"{path.relative_to(ROOT)}:{helper.lineno} must scope requester history "
            "by technician, Person Party, and SystemUser identity"
        )


def test_request_history_lists_delegate_to_the_identity_scope() -> None:
    for path, (_helper_name, delegate_name, *_required_fields) in SERVICES.items():
        list_mine = _function(path, "list_mine")
        called_names = {
            node.func.id
            for node in ast.walk(list_mine)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert delegate_name in called_names, (
            f"{path.relative_to(ROOT)}:{list_mine.lineno} bypasses the canonical "
            "requester-history scope"
        )


def test_material_history_query_does_not_require_active_profile() -> None:
    path = ROOT / "app/services/field/material_requests.py"
    requester_identity = _function(path, "_requester_identity")
    source = ast.unparse(requester_identity)
    assert "SystemUser" in source
    assert "TechnicianProfile.is_active" not in source


def test_request_history_repair_migration_covers_both_owned_tables() -> None:
    migration = (
        ROOT / "alembic/versions/587_field_request_requester_history.py"
    ).read_text(encoding="utf-8")
    for table_name in ("field_material_requests", "field_expense_requests"):
        assert table_name in migration
    for identity_column in (
        "requested_by_technician_id",
        "requested_by_person_id",
        "requested_by_system_user_id",
    ):
        assert identity_column in migration
