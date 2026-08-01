"""Protect the reviewed mistaken-subscription correction ownership boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app/services/subscription_correction.py"
BINDING_OWNER = PROJECT_ROOT / "app/services/access_credential_binding.py"
ROUTE = PROJECT_ROOT / "app/web/admin/catalog.py"
TEMPLATE = PROJECT_ROOT / "templates/admin/catalog/subscription_detail.html"


def test_correction_owner_uses_one_public_transaction_and_flush_only_participants() -> (
    None
):
    source = OWNER.read_text(encoding="utf-8")

    assert source.count("execute_owner_command(") == 1
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "stage_access_credential_binding(" in source
    assert "fup_state.clear(" in source
    assert "cancel_subscription(" in source
    assert "generate_credit=False" in source
    binding_source = BINDING_OWNER.read_text(encoding="utf-8")
    assert ".commit(" not in binding_source
    assert ".rollback(" not in binding_source
    assert "owner_command_active(db, owner=_COORDINATOR)" in binding_source


def test_correction_adapters_delegate_without_business_writes() -> None:
    route_source = ROUTE.read_text(encoding="utf-8")
    template_source = TEMPLATE.read_text(encoding="utf-8")

    assert "execute_subscription_correction_response(" in route_source
    assert "action_form(correction_action)" in template_source
    assert "Correct Subscription" in template_source

    tree = ast.parse(route_source, filename=str(ROUTE))
    correction_functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and "correction" in node.name
    }
    assert set(correction_functions) == {"catalog_subscription_execute_correction"}
    forbidden = {"commit", "rollback", "flush", "add", "delete"}
    calls = {
        node.func.attr
        for function in correction_functions.values()
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (calls & forbidden)


def test_credential_binding_event_is_secret_safe() -> None:
    source = BINDING_OWNER.read_text(encoding="utf-8")
    payload = source.split("EventType.access_credential_binding_changed", 1)[1].split(
        "actor=", 1
    )[0]

    assert "secret_hash" not in payload
    assert '"credential_id"' in payload
    assert '"target_subscription_id"' in payload
    assert '"target_radius_profile_id"' in payload
