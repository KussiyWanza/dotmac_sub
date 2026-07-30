from __future__ import annotations

from types import SimpleNamespace

from app.services.network.olt_ssh_ont import status


def _stub_lookup(
    monkeypatch,
    *,
    output: str,
    commands: list[str],
    closed: list[bool],
) -> None:
    class FakeTransport:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (
            FakeTransport(),
            SimpleNamespace(),
            SimpleNamespace(prompt_regex=r">\s*$"),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._prepare_huawei_read_shell",
        lambda *_args, **_kwargs: r"#\s*$",
    )

    def fake_run(_channel, command: str, *, prompt: str) -> str:
        commands.append(command)
        assert prompt == r"#\s*$"
        return output

    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_cmd",
        fake_run,
    )


def test_find_ont_by_serial_uses_huawei_hex_serial_and_parses_registration(
    monkeypatch,
) -> None:
    commands: list[str] = []
    closed: list[bool] = []
    _stub_lookup(
        monkeypatch,
        output="""
        F/S/P               : 0/1/7
        ONT-ID              : 4
        Run state           : offline
        Config state        : initial
        Match state         : initial
        """,
        commands=commands,
        closed=closed,
    )

    ok, message, registration = status.find_ont_by_serial(
        SimpleNamespace(name="Legacy MA5608T"),
        "HWTC1234ABCD",
    )

    assert ok is True
    assert commands == ["display ont info by-sn 485754431234ABCD"]
    assert registration is not None
    assert registration.fsp == "0/1/7"
    assert registration.onu_id == 4
    assert registration.real_serial == "HWTC1234ABCD"
    assert registration.run_state == "offline"
    assert "ONT-ID 4" in message
    assert closed == [True]


def test_find_ont_by_serial_parameter_error_fails_closed(monkeypatch) -> None:
    commands: list[str] = []
    closed: list[bool] = []
    _stub_lookup(
        monkeypatch,
        output="% Parameter error, the error locates at '^'",
        commands=commands,
        closed=closed,
    )

    ok, message, registration = status.find_ont_by_serial(
        SimpleNamespace(name="Legacy MA5608T"),
        "HWTC1234ABCD",
    )

    assert ok is False
    assert registration is None
    assert "parameter_error" in message
    assert "not registered" not in message
    assert closed == [True]


def test_find_ont_by_serial_unrecognized_output_fails_closed(monkeypatch) -> None:
    commands: list[str] = []
    closed: list[bool] = []
    _stub_lookup(
        monkeypatch,
        output="Command completed without a recognizable ONT detail block",
        commands=commands,
        closed=closed,
    )

    ok, message, registration = status.find_ont_by_serial(
        SimpleNamespace(name="Legacy MA5608T"),
        "HWTC1234ABCD",
    )

    assert ok is False
    assert registration is None
    assert "not recognized" in message
    assert "not registered" not in message
    assert closed == [True]


def test_find_ont_by_serial_accepts_only_explicit_ont_absence(monkeypatch) -> None:
    commands: list[str] = []
    closed: list[bool] = []
    _stub_lookup(
        monkeypatch,
        output="Failure: The ONT does not exist",
        commands=commands,
        closed=closed,
    )

    ok, message, registration = status.find_ont_by_serial(
        SimpleNamespace(name="Legacy MA5608T"),
        "HWTC1234ABCD",
    )

    assert ok is True
    assert registration is None
    assert "not registered" in message
    assert closed == [True]
