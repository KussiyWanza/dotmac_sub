"""Typed adapter coverage for administrative staff recovery routes."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

import app.web.admin as web_admin
from app.services import staff_provisioning, web_system_user_edit
from app.web.admin import system as admin_system

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _request(path: str, *, query_string: bytes = b"") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": query_string,
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "scheme": "http",
        }
    )
    actor_id = uuid4()
    request.state.actor_id = str(actor_id)
    request.state.auth = {
        "principal_type": "system_user",
        "principal_id": str(actor_id),
    }
    return request


def test_invite_route_builds_typed_recovery_command(monkeypatch) -> None:
    user_id = uuid4()
    captured: list[staff_provisioning.PrepareStaffCredentialRecoveryCommand] = []

    def send(_db, *, command):
        captured.append(command)
        return "Invitation sent. Password reset email delivered."

    monkeypatch.setattr(
        admin_system.web_system_user_mutations_service,
        "send_user_invite_for_user",
        send,
    )
    monkeypatch.setattr(admin_system, "_log_system_user_event", lambda *_a, **_k: None)

    response = admin_system.user_send_invite(
        _request(f"/admin/system/users/{user_id}/invite"),
        str(user_id),
        db=object(),
    )

    assert response.status_code == 303
    assert captured[0].user_id == user_id
    assert captured[0].context.scope == staff_provisioning.STAFF_ASSIGN_SCOPE
    assert captured[0].context.idempotency_key == f"staff-recovery:{user_id}:invite"


def test_bulk_invite_deduplicates_ids_and_builds_typed_commands(monkeypatch) -> None:
    first = uuid4()
    second = uuid4()
    captured: list[
        tuple[staff_provisioning.PrepareStaffCredentialRecoveryCommand, ...]
    ] = []

    def send(_db, *, commands):
        captured.append(commands)
        return len(commands), 0

    monkeypatch.setattr(
        admin_system.web_system_user_mutations_service,
        "bulk_send_user_invites",
        send,
    )

    response = admin_system.users_bulk_invite(
        _request("/admin/system/users/bulk/invite"),
        data={"user_ids": [str(first), str(first), str(second)]},
        db=object(),
    )

    assert response["sent_count"] == 2
    assert tuple(command.user_id for command in captured[0]) == (first, second)


def test_failed_invite_delivery_is_not_reported_as_success() -> None:
    note = "User created, but the reset email could not be sent."

    assert (
        admin_system.web_system_user_mutations_service.user_invite_succeeded(note)
        is False
    )


def test_staff_edit_form_carries_field_technician_access() -> None:
    form = {
        "first_name": "Field",
        "last_name": "Tech",
        "display_name": "Field Tech",
        "email": "field.tech@example.com",
        "phone": "",
        "field_technician_access": "on",
    }

    parsed = web_system_user_edit.parse_edit_form(form)
    command = web_system_user_edit.build_update_command(
        user_id=uuid4(),
        context=staff_provisioning.CommandContext.system(
            actor="system:test",
            scope=staff_provisioning.STAFF_ASSIGN_SCOPE,
            reason="test field technician access form",
        ),
        form=parsed,
        can_update_password=True,
    )

    assert parsed.field_technician_access is True
    assert command.field_technician_access is True


def test_staff_edit_form_rejects_passwords_shorter_than_eight_characters() -> None:
    form = web_system_user_edit.parse_edit_form(
        {
            "first_name": "Field",
            "last_name": "Tech",
            "email": "field.tech@example.com",
            "new_password": "short",
            "confirm_password": "short",
        }
    )

    with pytest.raises(ValueError, match="at least 8 characters"):
        web_system_user_edit.build_update_command(
            user_id=uuid4(),
            context=staff_provisioning.CommandContext.system(
                actor="system:test",
                scope=staff_provisioning.STAFF_ASSIGN_SCOPE,
                reason="test password length",
            ),
            form=form,
            can_update_password=True,
        )


def test_staff_edit_page_carries_field_technician_access(monkeypatch) -> None:
    user_id = uuid4()
    profile_id = uuid4()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        admin_system.web_system_profiles_service,
        "get_user_edit_data",
        lambda _db, _user_id: {
            "user": SimpleNamespace(id=user_id),
            "roles": [],
            "current_role_ids": set(),
            "managed_role_ids": set(),
            "all_permissions": [],
            "direct_permission_ids": set(),
            "field_technician_profile_id": profile_id,
            "field_technician_access": True,
        },
    )
    monkeypatch.setattr(admin_system, "_system_user_audit_items", lambda *_a: [])
    monkeypatch.setattr(web_admin, "get_sidebar_stats", lambda _db: {})

    def template_response(template, context, status_code=200):
        captured["template"] = template
        captured["context"] = context
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(admin_system.templates, "TemplateResponse", template_response)
    request = _request(f"/admin/system/users/{user_id}/edit")
    request.state.auth = {}

    response = admin_system.user_edit(request, str(user_id), db=object())

    context = captured["context"]
    assert response.status_code == 200
    assert captured["template"] == "admin/system/users/edit.html"
    assert context["field_technician_access"] is True
    assert context["field_technician_profile_id"] == profile_id


def test_staff_edit_page_shows_profile_save_success(monkeypatch) -> None:
    user_id = uuid4()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        admin_system.web_system_profiles_service,
        "get_user_edit_data",
        lambda _db, _user_id: {
            "user": SimpleNamespace(id=user_id),
            "roles": [],
            "current_role_ids": set(),
            "managed_role_ids": set(),
            "all_permissions": [],
            "direct_permission_ids": set(),
            "field_technician_profile_id": None,
            "field_technician_access": False,
        },
    )
    monkeypatch.setattr(admin_system, "_system_user_audit_items", lambda *_a: [])
    monkeypatch.setattr(web_admin, "get_sidebar_stats", lambda _db: {})

    def template_response(template, context, status_code=200):
        captured["template"] = template
        captured["context"] = context
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(admin_system.templates, "TemplateResponse", template_response)
    request = _request(
        f"/admin/system/users/{user_id}/edit", query_string=b"saved=profile"
    )
    request.state.auth = {}

    response = admin_system.user_edit(request, str(user_id), db=object())

    context = captured["context"]
    assert response.status_code == 200
    assert captured["template"] == "admin/system/users/edit.html"
    assert context["success"] == "User profile updated successfully."


def test_staff_edit_submit_redirects_back_with_success(monkeypatch) -> None:
    user_id = uuid4()
    captured: list[staff_provisioning.UpdateStaffIdentityCommand] = []

    def update_staff_identity(_db, command):
        captured.append(command)
        return SimpleNamespace()

    monkeypatch.setattr(
        admin_system.staff_provisioning_service,
        "update_staff_identity",
        update_staff_identity,
    )
    monkeypatch.setattr(
        admin_system.web_system_common_service,
        "is_admin_request",
        lambda _request: True,
    )

    response = admin_system.user_edit_submit(
        _request(f"/admin/system/users/{user_id}/edit"),
        str(user_id),
        form_data={
            "first_name": "Field",
            "last_name": "Tech",
            "display_name": "Field Tech",
            "email": "field.tech@example.com",
            "phone": "",
            "field_technician_access": "on",
        },
        db=object(),
    )

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/admin/system/users/{user_id}/edit?saved=profile"
    )
    assert captured[0].field_technician_access is True


def test_staff_edit_template_shows_field_service_access_status() -> None:
    template = (
        PROJECT_ROOT / "templates" / "admin" / "system" / "users" / "edit.html"
    ).read_text()

    assert "Field Service App:" in template
    assert "field_technician_access" in template
    assert "Enabled" in template
    assert "Not enabled" in template
    assert "success" in template


def test_staff_edit_template_password_controls_default_to_no_forced_reset() -> None:
    template = (
        PROJECT_ROOT / "templates" / "admin" / "system" / "users" / "edit.html"
    ).read_text()

    assert 'name="new_password" id="new_password" minlength="8"' in template
    assert 'name="confirm_password" id="confirm_password" minlength="8"' in template
    assert (
        'name="require_password_change" id="require_password_change" checked'
        not in template
    )
