"""Typed adapter coverage for administrative staff recovery routes."""

from uuid import uuid4

from starlette.requests import Request

from app.services import staff_provisioning, web_system_user_edit
from app.web.admin import system as admin_system


def _request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
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
