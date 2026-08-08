from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from app.services.network.ont_actions import ActionResult
from app.web.admin import network_onts
from app.web.templates import templates


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/network/onts/ont-1/configure",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "scheme": "http",
        }
    )
    request.state.csrf_token = "test-csrf-token"
    return request


def test_configure_form_renders_olt_owned_vlans_as_read_only() -> None:
    html = templates.env.get_template("admin/network/onts/_configure_form.html").render(
        request=_request(),
        ont_id="ont-1",
        config_pack_name="Abuja Huawei GPON",
        config_pack_olt_id="olt-1",
        wan_vlan=203,
        mgmt_vlan=201,
        tr069_profile_name="Dotmac ACS",
        has_tr069=False,
        acs_last_inform=None,
    )

    assert 'name="wan_vlan_id"' not in html
    assert 'name="mgmt_vlan_id"' not in html
    assert 'aria-label="Internet VLAN inherited from OLT config"' in html
    assert 'aria-label="Management VLAN inherited from OLT config"' in html
    assert "VLANs are inherited from the OLT config." in html
    assert "/admin/network/olts/olt-1?tab=settings" in html
    assert "Edit OLT config" in html


def _submit_values(push_scope: str) -> dict[str, object]:
    return {
        "wan_mode": "",
        "ip_protocol": "",
        "wan_static_ip": "",
        "wan_static_subnet": "",
        "wan_static_gateway": "",
        "wan_static_dns": "",
        "pppoe_username": "",
        "pppoe_password": "",
        "mgmt_ip_mode": "inactive",
        "mgmt_ip_address": "",
        "mgmt_remote_access": False,
        "lan_gateway_ip": "",
        "lan_subnet_mask": "",
        "lan_dhcp_enabled": False,
        "lan_dhcp_start": "",
        "lan_dhcp_end": "",
        "wifi_enabled": False,
        "wifi_ssid": "",
        "wifi_channel": "",
        "wifi_security_mode": "",
        "wifi_password": "",
        "pppoe_wcd_index": "",
        "mgmt_wcd_index": "",
        "voip_wcd_index": "",
        "mgmt_service_port_index": "",
        "wan_service_port_index": "",
        "push_to_device": False,
        "push_scope": push_scope,
    }


@pytest.mark.parametrize(
    ("push_scope", "cleared_fields", "untouched_field"),
    (
        ("wan", ("wan_mode", "ip_protocol"), "lan_subnet_mask"),
        ("lan", ("lan_subnet_mask",), "wifi_channel"),
        ("wifi", ("wifi_channel", "wifi_security_mode"), "wan_mode"),
    ),
)
def test_configure_default_choices_clear_only_the_submitted_section(
    monkeypatch: pytest.MonkeyPatch,
    push_scope: str,
    cleared_fields: tuple[str, ...],
    untouched_field: str,
) -> None:
    captured: dict[str, object] = {}

    def fake_update(_db: object, _ont_id: str, **kwargs: object) -> ActionResult:
        captured.update(kwargs)
        return ActionResult(success=True, message="Saved")

    monkeypatch.setattr(
        network_onts.web_network_ont_actions_service,
        "update_ont_config",
        fake_update,
    )
    monkeypatch.setattr(
        network_onts.web_network_ont_actions_service,
        "configure_form_context",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        network_onts.templates,
        "TemplateResponse",
        lambda *_args, **_kwargs: HTMLResponse("updated"),
    )
    monkeypatch.setattr(network_onts, "_log_ont_action_result", lambda **_kwargs: None)

    response = network_onts.ont_configure_submit(
        request=_request(),
        ont_id="ont-1",
        db=MagicMock(),
        **_submit_values(push_scope),
    )

    assert response.status_code == 200
    for field in cleared_fields:
        assert captured[field] == ""
    assert captured[untouched_field] is None


def test_empty_default_values_remove_existing_ont_overrides(db_session) -> None:
    from app.models.network import OntUnit
    from app.services.web_network_ont_actions.db_config import update_ont_config

    ont = OntUnit(
        serial_number="UI-CLEAR-DEFAULTS-001",
        desired_config={
            "wan": {"mode": "pppoe", "ip_protocol": "dual_stack"},
            "lan": {"subnet": "255.255.255.0"},
            "wifi": {"channel": "6", "security_mode": "WPA2-Personal"},
        },
    )
    db_session.add(ont)
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        wan_mode="",
        ip_protocol="",
        lan_subnet_mask="",
        wifi_channel="",
        wifi_security_mode="",
        push_to_device=False,
        push_wan=True,
        push_lan=True,
        push_mgmt=False,
        push_wifi=True,
    )

    assert result.success is True
    assert ont.desired_config == {}
