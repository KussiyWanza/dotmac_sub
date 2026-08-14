from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_parse_tr069_binding_for_target_ont() -> None:
    from app.services.network.olt_ssh_ont.tr069 import parse_tr069_binding

    output = """
    ont tr069-server-config 0 0 profile-id 2
    ont tr069-server-config 1 13 profile-id 5
    """

    assert parse_tr069_binding(output, port=1, ont_id=13) == 5
    assert parse_tr069_binding(output, port=1, ont_id=14) is None


def test_parse_tr069_binding_for_target_ont_without_port() -> None:
    from app.services.network.olt_ssh_ont.tr069 import parse_tr069_binding

    output = """
    ont tr069-server-config 7 profile-id 5
    ont tr069-server-config 13 profile-id 2
    """

    assert parse_tr069_binding(output, port=1, ont_id=7) == 5
    assert parse_tr069_binding(output, port=1, ont_id=8) is None


def test_parse_ont_info_detail_reads_huawei_tr069_server_profile_id_key() -> None:
    from app.services.network.parsers import parse_ont_info_detail

    output = """
F/S/P                   : 0/1/13
ONT-ID                  : 1
Run state               : online
Config state            : normal
Match state             : match
TR069 server profile ID : 2
"""

    entry = parse_ont_info_detail(output)

    assert entry is not None
    assert entry.tr069_profile_id == 2


def test_get_tr069_profile_binding_falls_back_to_display_ont_info(
    monkeypatch,
) -> None:
    import app.services.network.olt_ssh as core
    from app.services.network.olt_ssh_ont.tr069 import get_tr069_server_profile_binding

    class FakeChannel:
        def send(self, _chars: str) -> None:
            return None

    class FakeTransport:
        def close(self) -> None:
            return None

    class FakeProfile:
        def display_ont_info(self, fsp: str, ont_id: int) -> str:
            assert fsp == "0/2/1"
            assert ont_id == 13
            return "display ont info 0 2 1 13"

    commands: list[str] = []

    def fake_run(channel, command: str, prompt: str = r"#\s*$") -> str:
        assert isinstance(channel, FakeChannel)
        commands.append(command)
        if command in {"config", "interface gpon 0/2"}:
            return ""
        if command == "display this":
            return "interface gpon 0/2\n undo shutdown\n"
        if command == "display ont info 1 13":
            return "% Parameter error"
        if command == "display ont info 0 2 1 13":
            return """
F/S/P               : 0/2/1
ONT ID              : 13
Run state           : online
Config state        : normal
Match state         : match
TR069 Server Profile: 5
"""
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(core, "_validate_fsp", lambda fsp: (True, ""))
    monkeypatch.setattr(
        core,
        "_open_shell",
        lambda olt: (
            FakeTransport(),
            FakeChannel(),
            SimpleNamespace(prompt_regex=r"MA5608T#\s*$"),
        ),
    )
    monkeypatch.setattr(core, "_read_until_prompt", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(core, "_run_huawei_cmd", fake_run)
    monkeypatch.setattr(core, "_run_huawei_paged_cmd", fake_run)
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.tr069.get_huawei_command_profile",
        lambda olt: FakeProfile(),
    )

    ok, message, profile_id = get_tr069_server_profile_binding(
        SimpleNamespace(name="Test OLT", model="MA5608T"),
        "0/2/1",
        13,
    )

    assert ok is True
    assert profile_id == 5
    assert "TR-069 profile 5 bound" in message
    assert "display ont info 0 2 1 13" in message
    assert commands == [
        "config",
        "interface gpon 0/2",
        "display this",
        "display ont info 1 13",
        "display ont info 0 2 1 13",
    ]


def test_get_tr069_profile_binding_falls_back_to_current_config_include(
    monkeypatch,
) -> None:
    import app.services.network.olt_ssh as core
    from app.services.network.olt_ssh_ont.tr069 import get_tr069_server_profile_binding

    class FakeChannel:
        def send(self, _chars: str) -> None:
            return None

    class FakeTransport:
        def close(self) -> None:
            return None

    class FakeProfile:
        def display_ont_info(self, fsp: str, ont_id: int) -> str:
            assert fsp == "0/1/1"
            assert ont_id == 8
            return "display ont info 0 1 1 8"

    commands: list[str] = []

    def fake_run(channel, command: str, prompt: str = r"#\s*$") -> str:
        assert isinstance(channel, FakeChannel)
        commands.append(command)
        if command in {"config", "interface gpon 0/1", "quit"}:
            return ""
        if command == "display this":
            return "% Unknown command"
        if command in {"display ont info 1 8", "display ont info 0 1 1 8"}:
            return """
F/S/P               : 0/1/1
ONT ID              : 8
Run state           : online
Config state        : normal
Match state         : match
"""
        if command == "display current-configuration | include tr069-server-config":
            return """
ont tr069-server-config 1 7 profile-id 2
ont tr069-server-config 1 8 profile-id 2
ont tr069-server-config 2 0 profile-id 2
"""
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(core, "_validate_fsp", lambda fsp: (True, ""))
    monkeypatch.setattr(
        core,
        "_open_shell",
        lambda olt: (
            FakeTransport(),
            FakeChannel(),
            SimpleNamespace(prompt_regex=r"MA5608T#\s*$"),
        ),
    )
    monkeypatch.setattr(core, "_read_until_prompt", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(core, "_run_huawei_cmd", fake_run)
    monkeypatch.setattr(core, "_run_huawei_paged_cmd", fake_run)
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.tr069.get_huawei_command_profile",
        lambda olt: FakeProfile(),
    )

    ok, message, profile_id = get_tr069_server_profile_binding(
        SimpleNamespace(name="Test OLT", model="MA5608T"),
        "0/1/1",
        8,
    )

    assert ok is True
    assert profile_id == 2
    assert "TR-069 profile 2 bound" in message
    assert "display current-configuration | include tr069-server-config" in message
    assert commands == [
        "config",
        "interface gpon 0/1",
        "display this",
        "display ont info 1 8",
        "display ont info 0 1 1 8",
        "quit",
        "quit",
        "display current-configuration | include tr069-server-config",
    ]


def test_get_tr069_profile_binding_handles_parenthesized_and_paged_huawei_output(
    monkeypatch,
) -> None:
    import app.services.network.olt_ssh as core
    from app.services.network.olt_ssh_ont.tr069 import get_tr069_server_profile_binding

    class FakeChannel:
        def send(self, _chars: str) -> None:
            return None

    class FakeTransport:
        def close(self) -> None:
            return None

    commands: list[tuple[str, str]] = []

    def fake_run(channel, command: str, prompt: str = r"#\s*$") -> str:
        assert isinstance(channel, FakeChannel)
        commands.append((command, prompt))
        if command == "config":
            return "config\n\nMA5608T(config)#"
        if command == "interface gpon 0/1":
            return "interface gpon 0/1\n\nMA5608T(config-if-gpon-0/1)#"
        if command == "display this":
            return "% Unknown command"
        if command in {"display ont info 13 1", "display ont info 0 1 13 1"}:
            return """
display ont info 13 1
  Temperature             : 57(C)
  Authentic type          : SN-auth
  SN                      : 48575443A31CB903 (HWTC-A31CB903)
  -----------------------------------------------------------------------------
  TR069 server profile ID      : 2
  TR069 server profile name    : DotMac-ACS
---- More ( Press 'Q' to break ) ----
MA5608T(config-if-gpon-0/1)#
"""
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(core, "_validate_fsp", lambda fsp: (True, ""))
    monkeypatch.setattr(
        core,
        "_open_shell",
        lambda olt: (
            FakeTransport(),
            FakeChannel(),
            SimpleNamespace(prompt_regex=r"MA5608T#\s*$"),
        ),
    )
    monkeypatch.setattr(core, "_read_until_prompt", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(core, "_run_huawei_cmd", fake_run)
    monkeypatch.setattr(core, "_run_huawei_paged_cmd", fake_run)

    ok, message, profile_id = get_tr069_server_profile_binding(
        SimpleNamespace(name="Test OLT", model="MA5608T"),
        "0/1/13",
        1,
    )

    assert ok is True
    assert profile_id == 2
    assert "display ont info 13 1" in message
    assert [command for command, _prompt in commands] == [
        "config",
        "interface gpon 0/1",
        "display this",
        "display ont info 13 1",
    ]
    assert commands[1][1].endswith(r"MA5608T\(config\)\#\s*$")
    assert commands[2][1].endswith(r"MA5608T\(config\-if\-gpon\-0/1\)\#\s*$")
    assert commands[3][1].endswith(r"MA5608T\(config\-if\-gpon\-0/1\)\#\s*$")


def test_get_tr069_profile_binding_reads_display_this_with_paged_helper(
    monkeypatch,
) -> None:
    import app.services.network.olt_ssh as core
    from app.services.network.olt_ssh_ont.tr069 import get_tr069_server_profile_binding

    class FakeChannel:
        def send(self, _chars: str) -> None:
            return None

    class FakeTransport:
        def close(self) -> None:
            return None

    non_paged_commands: list[str] = []
    paged_commands: list[str] = []

    def fake_run(channel, command: str, prompt: str = r"#\s*$") -> str:
        assert isinstance(channel, FakeChannel)
        non_paged_commands.append(command)
        if command == "config":
            return "config\n\nMA5800-X2(config)#"
        if command == "interface gpon 0/2":
            return "interface gpon 0/2\n\nMA5800-X2(config-if-gpon-0/2)#"
        if command == "display this":
            raise AssertionError("display this must use paged readback")
        raise AssertionError(f"Unexpected command: {command}")

    def fake_paged(channel, command: str, prompt: str = r"#\s*$") -> str:
        assert isinstance(channel, FakeChannel)
        paged_commands.append(command)
        if command == "display this":
            return """
interface gpon 0/2
 ont tr069-server-config 0 0 profile-id 2
---- More ( Press 'Q' to break ) ----
 ont tr069-server-config 1 13 profile-id 5

MA5800-X2(config-if-gpon-0/2)#
"""
        raise AssertionError(f"Unexpected paged command: {command}")

    monkeypatch.setattr(core, "_validate_fsp", lambda fsp: (True, ""))
    monkeypatch.setattr(
        core,
        "_open_shell",
        lambda olt: (
            FakeTransport(),
            FakeChannel(),
            SimpleNamespace(prompt_regex=r"MA5800-X2#\s*$"),
        ),
    )
    monkeypatch.setattr(core, "_read_until_prompt", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(core, "_run_huawei_cmd", fake_run)
    monkeypatch.setattr(core, "_run_huawei_paged_cmd", fake_paged)

    ok, message, profile_id = get_tr069_server_profile_binding(
        SimpleNamespace(name="Garki Huawei OLT", model="MA5800-X2"),
        "0/2/1",
        13,
    )

    assert ok is True
    assert profile_id == 5
    assert "TR-069 profile 5 bound" in message
    assert non_paged_commands == ["config", "interface gpon 0/2"]
    assert paged_commands == ["display this"]


def test_provision_fails_when_tr069_binding_readback_mismatches(monkeypatch) -> None:
    from app.services.network import ont_provision_steps
    from app.services.network.ont_provision_steps import provision_with_reconciliation
    from app.services.network.ont_provisioning.context import OltContext

    ont = SimpleNamespace(id="ont-1", serial_number="RTKG00060198")
    olt = SimpleNamespace(id="olt-1", name="Garki Huawei OLT")
    ctx = OltContext(ont=ont, olt=olt, fsp="0/2/1", olt_ont_id=13)
    config_pack = SimpleNamespace(
        management_vlan=SimpleNamespace(tag=201),
        internet_config_ip_index=1,
        wan_config_profile_id=0,
        tr069_olt_profile_id=5,
    )
    values = {
        "wan_vlan": 203,
        "wan_gem_index": 1,
        "wan_mode": "pppoe",
        "mgmt_vlan": 201,
        "mgmt_gem_index": 2,
        "mgmt_ip_mode": "static_ip",
        "mgmt_ip_address": "172.16.201.141",
        "mgmt_subnet": "255.255.255.0",
        "mgmt_gateway": "172.16.201.1",
        "tr069_olt_profile_id": 5,
        "internet_config_ip_index": 1,
        "wan_config_profile_id": 0,
    }

    adapter = MagicMock()
    adapter.create_service_port.return_value = SimpleNamespace(
        success=True,
        message="created",
        data={},
    )
    adapter.clear_iphost_config.return_value = SimpleNamespace(success=False)
    adapter.clear_internet_config.return_value = SimpleNamespace(success=False)
    adapter.clear_wan_config.return_value = SimpleNamespace(success=False)
    adapter.configure_management_batch.return_value = SimpleNamespace(
        success=True,
        message="management configured",
        data={
            "steps_completed": [
                "create_mgmt_service_port",
                "configure_iphost",
                "bind_tr069",
            ]
        },
    )
    adapter.get_service_ports_for_ont.return_value = SimpleNamespace(
        success=True,
        message="ports ok",
        data={"service_ports": []},
    )
    adapter.get_tr069_profile_binding.return_value = SimpleNamespace(
        success=True,
        message="No TR-069 profile binding found for ONT 13 on 0/2/1",
        data={"profile_id": None},
    )

    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_olt_context",
        lambda *_args, **_kwargs: (ctx, ""),
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {"config_pack": config_pack, "values": values},
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "_validate_olt_profile_dependencies",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.iphost_priority.resolve_management_iphost_priority",
        lambda *_args, **_kwargs: 2,
    )
    monkeypatch.setattr(ont_provision_steps, "_record_step", lambda *_args: None)
    monkeypatch.setattr(
        ont_provision_steps, "_send_failure_notification", lambda *_args: None
    )

    result = provision_with_reconciliation(MagicMock(), "ont-1")

    assert result.success is True
    assert result.waiting is True
    assert "waiting for TR-069 binding verification" in result.message
    assert result.data["expected_tr069_profile_id"] == 5
    assert result.data["readback_tr069_profile_id"] is None
    assert result.data["failure_class"] == "tr069_binding_readback_miss"
    assert result.data["domain_outcomes"]["olt_l2_apply"]["status"] == "succeeded"
    assert (
        result.data["domain_outcomes"]["management_path_apply"]["status"] == "succeeded"
    )
    assert result.data["domain_outcomes"]["tr069_bind_apply"]["status"] == "succeeded"
    assert (
        result.data["domain_outcomes"]["olt_or_omci_readback_verify"]["status"]
        == "pending_verification"
    )
    assert (
        result.data["domain_outcomes"]["acs_bootstrap_verify"]["status"]
        == "pending_verification"
    )


def test_provision_recovers_tr069_binding_after_reset(monkeypatch) -> None:
    from app.services.network import ont_provision_steps
    from app.services.network.ont_provision_steps import provision_with_reconciliation
    from app.services.network.ont_provisioning.context import OltContext

    ont = SimpleNamespace(id="ont-1", serial_number="RTKG00060198")
    olt = SimpleNamespace(id="olt-1", name="Garki Huawei OLT")
    ctx = OltContext(ont=ont, olt=olt, fsp="0/2/1", olt_ont_id=13)
    config_pack = SimpleNamespace(
        management_vlan=SimpleNamespace(tag=201),
        internet_config_ip_index=1,
        wan_config_profile_id=0,
        tr069_olt_profile_id=5,
    )
    values = {
        "wan_vlan": 203,
        "wan_gem_index": 1,
        "wan_mode": "pppoe",
        "mgmt_vlan": 201,
        "mgmt_gem_index": 2,
        "mgmt_ip_mode": "static_ip",
        "mgmt_ip_address": "172.16.201.141",
        "mgmt_subnet": "255.255.255.0",
        "mgmt_gateway": "172.16.201.1",
        "tr069_olt_profile_id": 5,
        "internet_config_ip_index": 1,
        "wan_config_profile_id": 0,
    }

    adapter = MagicMock()
    adapter.create_service_port.return_value = SimpleNamespace(
        success=True,
        message="created",
        data={},
    )
    adapter.clear_iphost_config.return_value = SimpleNamespace(success=False)
    adapter.clear_internet_config.return_value = SimpleNamespace(success=False)
    adapter.clear_wan_config.return_value = SimpleNamespace(success=False)
    adapter.configure_management_batch.return_value = SimpleNamespace(
        success=True,
        message="management configured",
        data={
            "steps_completed": [
                "create_mgmt_service_port",
                "configure_iphost",
                "bind_tr069",
            ]
        },
    )
    adapter.get_service_ports_for_ont.return_value = SimpleNamespace(
        success=True,
        message="ports ok",
        data={"service_ports": []},
    )
    adapter.get_tr069_profile_binding.side_effect = [
        SimpleNamespace(
            success=True,
            message="No TR-069 profile binding found for ONT 13 on 0/2/1",
            data={"profile_id": None},
        ),
        SimpleNamespace(
            success=True,
            message="TR-069 profile 5 bound for ONT 13 on 0/2/1",
            data={"profile_id": 5},
        ),
    ]
    adapter.reboot_ont.return_value = SimpleNamespace(
        success=True,
        message="ONT 13 reboot command sent via OMCI",
    )

    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_olt_context",
        lambda *_args, **_kwargs: (ctx, ""),
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {"config_pack": config_pack, "values": values},
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "_validate_olt_profile_dependencies",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.iphost_priority.resolve_management_iphost_priority",
        lambda *_args, **_kwargs: 2,
    )
    monkeypatch.setattr(ont_provision_steps, "_record_step", lambda *_args: None)
    monkeypatch.setattr(
        ont_provision_steps, "_send_failure_notification", lambda *_args: None
    )
    monkeypatch.setattr(ont_provision_steps.time, "sleep", lambda *_args: None)

    result = provision_with_reconciliation(MagicMock(), "ont-1")

    assert result.success is True
    assert result.data["readback_tr069_profile_id"] == 5
    assert "reset_after_tr069_bind" in result.data["steps_completed"]
    assert (
        result.data["domain_outcomes"]["olt_or_omci_readback_verify"]["status"]
        == "succeeded"
    )
    adapter.reboot_ont.assert_called_once_with("0/2/1", 13)
