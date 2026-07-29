from app.models.network import OLTDevice, PonPort
from app.services.network import olt_pon_port_control


def _olt_and_port(db_session, *, enabled: bool = True):
    olt = OLTDevice(
        name="Huawei Access OLT",
        vendor="Huawei",
        model="MA5800",
        mgmt_ip="192.0.2.10",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    port = PonPort(
        olt_id=olt.id,
        name="0/1/3",
        port_number=3,
        is_active=True,
        admin_enabled=enabled,
    )
    db_session.add(port)
    db_session.commit()
    return olt, port


def test_disable_pon_port_executes_huawei_shutdown(db_session, monkeypatch):
    olt, port = _olt_and_port(db_session)
    observed = {}
    monkeypatch.setattr(
        olt_pon_port_control, "get_olt_write_mode_enabled", lambda _db: True
    )

    def fake_run(device, fsp, command, *, success_message):
        observed.update(
            device=device, fsp=fsp, command=command, success_message=success_message
        )
        return True, success_message

    monkeypatch.setattr(olt_pon_port_control, "_run_ont_config_command", fake_run)

    ok, message = olt_pon_port_control.set_pon_port_admin_state(
        db_session,
        olt_id=str(olt.id),
        pon_port_id=str(port.id),
        enabled=False,
    )

    assert ok is True
    assert "disabled" in message
    assert observed["device"].id == olt.id
    assert observed["fsp"] == "0/1/3"
    assert observed["command"] == "shutdown 3"
    saved = db_session.get(PonPort, port.id)
    assert saved.admin_enabled is False
    assert saved.is_active is True


def test_enable_pon_port_executes_huawei_undo_shutdown(db_session, monkeypatch):
    olt, port = _olt_and_port(db_session, enabled=False)
    monkeypatch.setattr(
        olt_pon_port_control, "get_olt_write_mode_enabled", lambda _db: True
    )
    commands = []
    monkeypatch.setattr(
        olt_pon_port_control,
        "_run_ont_config_command",
        lambda _device, _fsp, command, **_kwargs: (
            commands.append(command) is None,
            "enabled",
        ),
    )

    ok, _message = olt_pon_port_control.set_pon_port_admin_state(
        db_session,
        olt_id=str(olt.id),
        pon_port_id=str(port.id),
        enabled=True,
    )

    assert ok is True
    assert commands == ["undo shutdown 3"]
    assert db_session.get(PonPort, port.id).admin_enabled is True


def test_pon_port_control_stops_when_write_mode_is_disabled(db_session, monkeypatch):
    olt, port = _olt_and_port(db_session)
    monkeypatch.setattr(
        olt_pon_port_control, "get_olt_write_mode_enabled", lambda _db: False
    )
    monkeypatch.setattr(
        olt_pon_port_control,
        "_run_ont_config_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SSH must not run")
        ),
    )

    ok, message = olt_pon_port_control.set_pon_port_admin_state(
        db_session,
        olt_id=str(olt.id),
        pon_port_id=str(port.id),
        enabled=False,
    )

    assert ok is False
    assert "write mode is disabled" in message
    assert db_session.get(PonPort, port.id).admin_enabled is True


def test_failed_device_command_does_not_change_local_state(db_session, monkeypatch):
    olt, port = _olt_and_port(db_session)
    monkeypatch.setattr(
        olt_pon_port_control, "get_olt_write_mode_enabled", lambda _db: True
    )
    monkeypatch.setattr(
        olt_pon_port_control,
        "_run_ont_config_command",
        lambda *_args, **_kwargs: (False, "OLT rejected command"),
    )

    ok, message = olt_pon_port_control.set_pon_port_admin_state(
        db_session,
        olt_id=str(olt.id),
        pon_port_id=str(port.id),
        enabled=False,
    )

    assert ok is False
    assert message == "OLT rejected command"
    assert db_session.get(PonPort, port.id).admin_enabled is True
