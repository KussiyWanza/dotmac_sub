import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from app.services.network.metrics_adapters import VictoriaMetricsAdapter
from app.services.network.olt_ssh import _run_huawei_cmd
from app.web.admin.network_olts_inventory import olt_authorize_ont


class _DummyChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, value: str) -> None:
        self.sent.append(value)


def test_promql_label_selector_does_not_overescape_hyphen() -> None:
    selector = VictoriaMetricsAdapter()._build_label_selector(
        ont_serial="485754437D4510C3",  # noqa: SLF001
        ont_id="d3158608-b57d-405b-8ace-e71ba195ed33",
    )
    assert 'ont_id="d3158608-b57d-405b-8ace-e71ba195ed33"' in selector
    assert 'ont_serial=~"' in selector
    assert r"485754437D4510C3" in selector
    assert r"485754437D4510C3\-" not in selector


def test_huawei_command_defaults_accept_user_and_exec_prompts(monkeypatch) -> None:
    channel = _DummyChannel()
    monkeypatch.setattr("app.services.network.olt_ssh.time.sleep", lambda _: None)

    def _fake_read_until_prompt(
        _channel, prompt_regex: str, timeout_sec: float = 8.0
    ) -> str:
        assert prompt_regex.startswith(r"#\s*$")
        assert timeout_sec == 12
        return "OLT>"

    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt", _fake_read_until_prompt
    )

    output = _run_huawei_cmd(channel, "display version")

    assert output == "OLT>"
    assert "".join(channel.sent) == "display version\n"


def test_huawei_command_accepts_brace_optional_prompt(monkeypatch) -> None:
    channel = _DummyChannel()
    monkeypatch.setattr("app.services.network.olt_ssh.time.sleep", lambda _: None)
    responses = iter(["{ |priority<K> }:", "OLT(config-if-gpon-0/1)#"])

    def _fake_read_until_prompt(
        _channel, prompt_regex: str, timeout_sec: float = 8.0
    ) -> str:
        assert prompt_regex.startswith(r"#\s*$")
        assert timeout_sec == 12
        return next(responses)

    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt", _fake_read_until_prompt
    )

    output = _run_huawei_cmd(channel, "ont ipconfig 13 8 ip-index 0 dhcp vlan 201")

    assert output == "OLT(config-if-gpon-0/1)#"
    assert "".join(channel.sent) == ("ont ipconfig 13 8 ip-index 0 dhcp vlan 201\n\n")


def test_inventory_authorize_route_accepts_force_reauthorize_flag() -> None:
    signature = inspect.signature(olt_authorize_ont)

    assert "force_reauthorize" in signature.parameters


def test_olt_detail_template_has_no_manual_autofind_scan_action() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    assert "/autofind" not in template
    assert "Autofind ONTs" not in template


def test_force_authorize_route_runs_synchronously(
    monkeypatch,
) -> None:
    from app.web.admin import network_olts_inventory

    captured: dict[str, object] = {}
    olt_id = uuid4()
    ont_id = uuid4()

    def _fake_request(_db, command):
        captured["command"] = command
        return SimpleNamespace(accepted=True)

    monkeypatch.setattr(
        network_olts_inventory, "request_ont_authorization", _fake_request
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "_authorization_detail_redirect_url",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "can_authorize_ont_from_request",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "submitted_authorization_ont_matches_scope",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        network_olts_inventory.web_admin_service,
        "get_current_user",
        lambda _request: {"name": "Alice Admin"},
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/network/olts/{olt_id}/authorize-ont",
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["network:write"],
    }

    response = network_olts_inventory.olt_authorize_ont(
        request,
        str(olt_id),
        fsp="0/1/6",
        serial_number="4857544328201B9A",
        ont_id=str(ont_id),
        return_to="/admin/network/onts?view=unconfigured",
        force_reauthorize="true",
        preset_id="",
        db=SimpleNamespace(rollback=lambda: None),
    )

    assert response.status_code == 303
    assert "Authorization+started" in response.headers["location"]
    command = captured["command"]
    assert command.force_reauthorize is True
    assert command.ont_id == ont_id
    assert command.target.olt_id == olt_id
    assert command.target.fsp.value == "0/1/6"
    assert command.target.serial_number.value == "HWTC28201B9A"
    assert command.context.actor == "Alice Admin"


def test_normal_authorize_route_runs_synchronously(
    monkeypatch,
) -> None:
    from app.web.admin import network_olts_inventory

    captured: dict[str, object] = {}
    olt_id = uuid4()
    ont_id = uuid4()

    def _fake_request(_db, command):
        captured["command"] = command
        return SimpleNamespace(accepted=True)

    monkeypatch.setattr(
        network_olts_inventory, "request_ont_authorization", _fake_request
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "_authorization_detail_redirect_url",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "can_authorize_ont_from_request",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        network_olts_inventory,
        "submitted_authorization_ont_matches_scope",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        network_olts_inventory.web_admin_service,
        "get_current_user",
        lambda _request: {"name": "Alice Admin"},
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/network/olts/{olt_id}/authorize-ont",
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": [],
        "scopes": ["network:write"],
    }

    response = network_olts_inventory.olt_authorize_ont(
        request,
        str(olt_id),
        fsp="0/1/6",
        serial_number="4857544328201B9A",
        ont_id=str(ont_id),
        return_to="/admin/network/onts?view=unconfigured",
        force_reauthorize="",
        preset_id="",
        db=SimpleNamespace(rollback=lambda: None),
    )

    assert response.status_code == 303
    assert "Authorization+started" in response.headers["location"]
    command = captured["command"]
    assert command.force_reauthorize is False
    assert command.ont_id == ont_id
    assert command.target.olt_id == olt_id
    assert command.target.fsp.value == "0/1/6"
    assert command.target.serial_number.value == "HWTC28201B9A"
    assert command.context.actor == "Alice Admin"


def test_olt_detail_template_exposes_import_state_actions() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    assert 'name="import_source" value="live"' in template
    assert 'name="import_source" value="dump"' in template
    assert "/root/olt_audit_20260506" in template
    assert "Import Live State" in template
    assert "Import Dump" in template


def test_olt_detail_template_uses_imported_state_sections() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    assert "Imported OLT Profiles" in template
    assert 'hx-get="/admin/network/olts/{{ olt.id }}/profiles/imported"' in template
    assert 'x-show="activeTab' not in template
    assert "Subscriber Impact" not in template
    assert "Runtime and hardware state" not in template


def test_olt_detail_template_exposes_pon_port_identifier_editor() -> None:
    template = Path("templates/admin/network/olts/detail.html").read_text()

    assert "Edit PON Port Identifier" in template
    assert "Edit identifiers" in template
    assert 'aria-haspopup="dialog"' in template
    assert "/pon-port-identifier" in template
    assert "Identifier Name" in template
    assert "No identifier" in template


def test_olt_save_pon_port_identifier_route_updates_alias(
    db_session, monkeypatch
) -> None:
    from app.models.network import OLTDevice, PonPort
    from app.web.admin import network_olts_inventory

    olt = OLTDevice(name="OLT-J", mgmt_ip="198.51.100.19", is_active=True)
    db_session.add(olt)
    db_session.flush()
    port = PonPort(olt_id=olt.id, name="0/9/0", is_active=True)
    db_session.add(port)
    db_session.commit()

    monkeypatch.setattr(
        network_olts_inventory, "_log_olt_action_result", lambda **_kwargs: None
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/network/olts/{olt.id}/pon-port-identifier",
            "headers": [],
            "query_string": b"",
        }
    )

    response = network_olts_inventory.olt_save_pon_port_identifier(
        request,
        str(olt.id),
        pon_port_id=str(port.id),
        alias="Feeder Cabinet A",
        db=db_session,
    )

    db_session.refresh(port)

    assert response.status_code == 303
    assert "sync_status=success" in response.headers["location"]
    assert "Identifier+updated+for+0%2F9%2F0." in response.headers["location"]
    assert "[[alias:Feeder Cabinet A]]" in (port.notes or "")


def test_ont_operations_tab_lazy_loads_only_when_selected() -> None:
    template = Path("templates/admin/network/onts/detail.html").read_text()

    assert 'hx-get="/admin/network/onts/{{ ont.id }}/operational-health"' in template
    assert 'hx-trigger="loadOperationalHealth from:window"' in template
    assert "load, revealed, loadOperationalHealth" not in template
    assert "activeTab === 'operations'" in template


def test_ont_overview_template_includes_provisioning_status_panel() -> None:
    template = Path("templates/admin/network/onts/_tab_overview.html").read_text()
    panel = Path(
        "templates/admin/network/onts/_provisioning_status_panel.html"
    ).read_text()

    assert "_provisioning_status_panel.html" in template
    assert "Provisioning Status" in panel
    assert "Stage Results" in panel
    assert "Recent Provisioning Activity" in panel
