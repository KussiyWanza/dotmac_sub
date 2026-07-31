from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from app.services.web_network_ont_actions import device_actions


class _OntDb:
    def __init__(self, ont) -> None:
        self.ont = ont

    def get(self, _model, _entity_id):
        return self.ont


def test_reauthorize_stages_assignment_gated_command(monkeypatch) -> None:
    ont = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        olt_device_id=UUID("00000000-0000-0000-0000-000000000002"),
        board="0/1",
        port="3",
        serial_number="HWTC7D4607C3",
    )
    captured = {}

    def request_authorization(db, command):
        captured.update({"db": db, "command": command})
        return SimpleNamespace(
            accepted=True,
            waiting=True,
            message="ONT authorization accepted",
            operation_id=UUID("00000000-0000-0000-0000-000000000003"),
            dispatch_id=UUID("00000000-0000-0000-0000-000000000004"),
            duplicate=False,
        )

    monkeypatch.setattr(
        "app.services.network.ont_provisioning_commands.request_ont_authorization",
        request_authorization,
    )
    monkeypatch.setattr(
        device_actions,
        "actor_name_from_request",
        lambda _request: "noc.operator",
    )
    monkeypatch.setattr(
        device_actions, "_log_action_audit", lambda *_args, **_kwargs: None
    )
    db = _OntDb(ont)

    result = device_actions.execute_reauthorize(
        db,
        "00000000-0000-0000-0000-000000000001",
    )

    assert result.success is True
    assert result.waiting is True
    assert result.data == {
        "operation_id": "00000000-0000-0000-0000-000000000003",
        "dispatch_id": "00000000-0000-0000-0000-000000000004",
        "duplicate": False,
    }
    assert captured["db"] is db
    command = captured["command"]
    assert command.ont_id == ont.id
    assert command.target.olt_id == ont.olt_device_id
    assert command.target.fsp.value == "0/1/3"
    assert command.target.serial_number.value == "HWTC7D4607C3"
    assert command.force_reauthorize is True
    assert command.context.actor == "noc.operator"


def test_reauthorize_preserves_assignment_admission_rejection(monkeypatch) -> None:
    ont = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        olt_device_id=UUID("00000000-0000-0000-0000-000000000002"),
        board="0/1",
        port="3",
        serial_number="HWTC7D4607C3",
    )
    monkeypatch.setattr(
        "app.services.network.ont_provisioning_commands.request_ont_authorization",
        lambda *_args, **_kwargs: SimpleNamespace(
            accepted=False,
            waiting=False,
            message="Authorize & provision requires an active assignment",
            operation_id=None,
            dispatch_id=None,
            duplicate=False,
        ),
    )
    monkeypatch.setattr(
        device_actions, "_log_action_audit", lambda *_args, **_kwargs: None
    )

    result = device_actions.execute_reauthorize(
        _OntDb(ont),
        "00000000-0000-0000-0000-000000000001",
    )

    assert result.success is False
    assert result.waiting is False
    assert "active assignment" in result.message
