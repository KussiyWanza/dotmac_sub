"""Keep staff lifecycle writes behind the contracted coordinator boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "staff_provisioning.py"
ADAPTER = PROJECT_ROOT / "app" / "api" / "staff_sync.py"
HANDLER = PROJECT_ROOT / "app" / "services" / "events" / "handlers" / "staff_invite.py"
ADMIN_ROUTE = PROJECT_ROOT / "app" / "web" / "admin" / "system.py"
EDIT_ADAPTER = PROJECT_ROOT / "app" / "services" / "web_system_user_edit.py"
RECOVERY_ADAPTER = PROJECT_ROOT / "app" / "services" / "web_system_user_mutations.py"
PROFILE_ADAPTER = PROJECT_ROOT / "app" / "services" / "user_profile.py"
PROFILE_READ_ADAPTER = PROJECT_ROOT / "app" / "services" / "web_system_profiles.py"


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def _function_calls(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    calls: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def test_staff_owner_uses_verified_boundary_without_helper_completion() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "web_system_user_mutations" not in source
    assert "send_user_invite" not in source


def test_staff_sync_adapter_owns_no_persistence_or_delivery() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    calls = _calls(ADAPTER)

    assert "provision_staff_account" in calls
    assert "sync_staff_account_roles" in calls
    assert "set_staff_account_active" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls
    assert "send_user_invite" not in source


def test_staff_invite_handler_delegates_to_communication_owner() -> None:
    source = HANDLER.read_text(encoding="utf-8")
    calls = _calls(HANDLER)

    assert "submit_communication_intent" in calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls
    assert "send_email" not in source


def test_admin_identity_form_is_a_typed_non_persistent_adapter() -> None:
    source = EDIT_ADAPTER.read_text(encoding="utf-8")
    calls = _calls(EDIT_ADAPTER)

    assert "UpdateStaffIdentityCommand" in calls
    assert "UserCredential" not in source
    assert "SystemUser" not in source
    assert not {"add", "delete", "flush", "commit", "rollback"} & calls


def test_staff_profile_adapters_delegate_identity_changes_to_owner() -> None:
    admin_calls = _function_calls(ADMIN_ROUTE, "user_edit_submit")
    self_profile_calls = _function_calls(ADMIN_ROUTE, "user_profile_update")
    api_profile_calls = _function_calls(PROFILE_ADAPTER, "update_me")

    assert "update_staff_identity" in admin_calls
    assert "update_staff_identity" in self_profile_calls
    assert "update_staff_identity" in api_profile_calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & admin_calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & self_profile_calls


def test_staff_recovery_adapters_prepare_identity_through_owner() -> None:
    source = RECOVERY_ADAPTER.read_text(encoding="utf-8")
    invite_calls = _function_calls(RECOVERY_ADAPTER, "send_user_invite_for_user")
    reset_calls = _function_calls(
        RECOVERY_ADAPTER,
        "send_password_reset_link_for_user",
    )

    assert "prepare_staff_credential_recovery" in invite_calls
    assert "prepare_staff_credential_recovery" in reset_calls
    assert "ensure_local_credential_for_user" not in source
    assert "set_local_login_active" not in source


def test_login_credential_card_uses_owner_status_and_eligibility() -> None:
    detail_calls = _function_calls(PROFILE_READ_ADAPTER, "get_user_detail_data")

    assert "get_staff_login_identity_view" in detail_calls
