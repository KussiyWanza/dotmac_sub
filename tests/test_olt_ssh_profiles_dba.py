from __future__ import annotations

from types import SimpleNamespace


def test_get_dba_profiles_runs_display_command(monkeypatch) -> None:
    from app.services.network import olt_ssh_profiles

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display dba-profile all":
            # Match the MA5608T-style ``display dba-profile all`` output
            # the parser is built for (key-value blocks separated by
            # dash dividers).
            return """
  ----------------------------------------------------------------------------
  Profile-ID    : 50
  Profile-name  : DOTMAC_100M
  Type          : type3
  Assure(kbps)  : 50000
  Max(kbps)     : 100000
  ----------------------------------------------------------------------------
"""
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_cmd",
        fake_run,
    )

    ok, message, entries = olt_ssh_profiles.get_dba_profiles(
        SimpleNamespace(name="OLT 1")
    )

    assert ok is True
    assert message == "Found 1 DBA profile(s)"
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display dba-profile all",
    ]
    assert sent[-1] == "close"
    assert entries[0].profile_id == 50
    assert entries[0].name == "DOTMAC_100M"


def test_get_traffic_tables_uses_bounded_from_index_command(monkeypatch) -> None:
    from app.services.network import olt_ssh_profiles

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display traffic table ip from-index 0":
            return """
            Index  Name            CIR(kbps)  PIR(kbps)  Priority
            6      DOTMAC_100M_IN  50000      100000     0
            """
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_paged_cmd",
        fake_run,
    )

    ok, message, entries = olt_ssh_profiles.get_traffic_tables(
        SimpleNamespace(name="OLT 1")
    )

    assert ok is True
    assert message == "Found 1 traffic table(s)"
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display traffic table ip from-index 0",
    ]
    assert sent[-1] == "close"
    assert entries[0].index == 6
    assert entries[0].name == "DOTMAC_100M_IN"


def test_get_traffic_tables_uses_from_index_for_ma5800(monkeypatch) -> None:
    from app.services.network import olt_ssh_profiles

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display traffic table ip from-index 0":
            return """
            TID CIR      CBS        PIR      PBS        Pri Copy-policy     Pri-Policy
              7 10240    329680     10240    329680       0 -                local-pri
             86 2048     67536      10432    335824       2 -                local-pri
            """
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_cmd",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_paged_cmd",
        fake_run,
    )

    ok, message, entries = olt_ssh_profiles.get_traffic_tables(
        SimpleNamespace(name="OLT 1", model="MA5800-X2")
    )

    assert ok is True
    assert message == "Found 2 traffic table(s)"
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display traffic table ip from-index 0",
    ]
    assert sent[-1] == "close"
    assert [entry.index for entry in entries] == [7, 86]


def test_get_traffic_tables_uses_bounded_command_for_older_huawei(monkeypatch) -> None:
    from app.services.network import olt_ssh_profiles

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display traffic table ip from-index 0":
            return """
            TID CIR      CBS        PIR      PBS        Pri Copy-policy     Pri-Policy
             87 1048064  33540048   1048064  33540048     0 -                local-pri
             88 1048064  33540048   1048064  33540048     0 -                local-pri
            """
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_paged_cmd",
        fake_run,
    )

    ok, message, entries = olt_ssh_profiles.get_traffic_tables(
        SimpleNamespace(name="OLT 1", model="MA5608T")
    )

    assert ok is True
    assert message == "Found 2 traffic table(s)"
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display traffic table ip from-index 0",
    ]
    assert sent[-1] == "close"
    assert [entry.index for entry in entries] == [87, 88]


def test_get_wan_profiles_runs_display_command(monkeypatch) -> None:
    from app.services.network import olt_ssh_profiles

    sent: list[str] = []

    class FakeChannel:
        def send(self, command: str) -> None:
            sent.append(command)

    class FakeTransport:
        def close(self) -> None:
            sent.append("close")

    def fake_run(_channel, command, **_kwargs):
        sent.append(command)
        if command == "display ont wan-profile all":
            return """
            Profile-ID  Profile-name    Connection-type  NAT
            0           Default_Router  route            enable
            """
        return ""

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (FakeTransport(), FakeChannel(), None),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_cmd",
        fake_run,
    )

    ok, message, entries = olt_ssh_profiles.get_wan_profiles(
        SimpleNamespace(name="OLT 1")
    )

    assert ok is True
    assert message == "Found 1 WAN profile(s)"
    assert sent[:3] == [
        "enable\n",
        "screen-length 0 temporary\n",
        "display ont wan-profile all",
    ]
    assert sent[-1] == "close"
    assert entries[0].profile_id == 0
    assert entries[0].connection_type == "route"
