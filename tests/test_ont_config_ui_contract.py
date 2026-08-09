from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from app.services.network.ont_actions import ActionResult
from app.services.web_network_operations import ProvisionOperationProgress
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


def test_configure_form_exposes_only_section_scoped_routed_actions() -> None:
    html = templates.env.get_template("admin/network/onts/_configure_form.html").render(
        request=_request(),
        ont_id="ont-1",
        wan_mode="setup_via_onu",
        has_tr069=False,
        configure_readiness=SimpleNamespace(
            olt_assigned=True,
            config_pack_ready=True,
            acs_registered=False,
        ),
    )

    assert 'value="all"' not in html
    assert "Apply All" not in html
    assert "Apply one section at a time" in html
    assert 'value="setup_via_onu" disabled selected' in html
    assert "Bridge / Via ONU is provisioning-only" in html
    assert ":disabled=\"wanMode === 'setup_via_onu'\"" in html
    assert "Leave blank to keep the current PPPoE password" in html
    assert "Leave blank to keep the current Wi-Fi password" in html
    assert "Supported path: Huawei routed ONTs" in html
    assert "ACS registration" in html
    assert "Not ready" in html


def test_configure_form_distinguishes_pending_delivery_from_success() -> None:
    html = templates.env.get_template("admin/network/onts/_configure_form.html").render(
        request=_request(),
        ont_id="ont-1",
        config_result=ActionResult(
            success=True,
            waiting=True,
            message="Saved; waiting for device inform.",
        ),
    )

    assert "Pending delivery" in html
    assert "Saved; waiting for device inform." in html
    assert "border-amber-200" in html
    assert "border-emerald-200" not in html


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


def test_configure_submit_rejects_bridge_before_service_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_call = MagicMock()
    monkeypatch.setattr(
        network_onts.web_network_ont_actions_service,
        "update_ont_config",
        service_call,
    )
    values = _submit_values("wan")
    values["wan_mode"] = "setup_via_onu"

    with pytest.raises(HTTPException) as exc_info:
        network_onts.ont_configure_submit(
            request=_request(),
            ont_id="ont-1",
            db=MagicMock(),
            **values,
        )

    assert exc_info.value.status_code == 400
    assert "provisioning" in str(exc_info.value.detail).lower()
    service_call.assert_not_called()


def test_configure_submit_uses_warning_toast_for_pending_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        network_onts.web_network_ont_actions_service,
        "update_ont_config",
        lambda *_args, **_kwargs: ActionResult(
            success=True,
            waiting=True,
            message="Saved; waiting for device inform.",
        ),
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
        **_submit_values("lan"),
    )

    toast = json.loads(response.headers["HX-Trigger"])["showToast"]
    assert toast["type"] == "warning"


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


def _provision_template_context() -> dict[str, object]:
    return {
        "request": _request(),
        "ont": SimpleNamespace(
            id="ont-1",
            serial_number="HWTC-PROVISION-001",
            external_id=None,
            authorization_status=None,
        ),
        "olt": None,
        "signal_info": {
            "status_presentation": SimpleNamespace(
                label="Online",
                tone=SimpleNamespace(value="success"),
                icon=SimpleNamespace(value="check"),
            ),
            "olt_rx_dbm": None,
        },
        "assignment": None,
        "subscriber": None,
        "acs_bound": False,
        "operational_acs_server_name": None,
        "pon_label": None,
        "provision_feedback": None,
        "provision_operation": None,
    }


def test_provision_page_blocks_when_authorization_is_ready_but_provisioning_is_not() -> (
    None
):
    context = _provision_template_context()
    context["provision_preflight"] = SimpleNamespace(
        ready_to_authorize=True,
        ready_to_provision=False,
        checks=[],
    )

    html = templates.env.get_template("admin/network/onts/provision.html").render(
        **context
    )

    assert "Blocked" in html
    assert '<button type="submit"\n                        disabled' in html
    assert "bg-slate-400 cursor-not-allowed" in html


def test_provision_page_shows_the_queued_operation_progress() -> None:
    context = _provision_template_context()
    context["provision_preflight"] = SimpleNamespace(
        ready_to_authorize=True,
        ready_to_provision=True,
        checks=[],
    )
    context["provision_operation"] = ProvisionOperationProgress(
        operation_id="11111111-1111-1111-1111-111111111111",
        title="ONT Provision",
        status="Pending",
        status_value="pending",
        status_class="pending-status",
        control_plane_phase="queued",
        message="",
        occurred_at=datetime(2026, 8, 8, tzinfo=UTC),
        duration=None,
        is_active=True,
    )

    html = templates.env.get_template("admin/network/onts/provision.html").render(
        **context
    )

    assert "Provisioning progress" in html
    assert "Pending" in html
    assert "queued and waiting for the provisioning worker or device" in html
    assert "Operation 11111111-1111-1111-1111-111111111111" in html
    assert "?operation_id=11111111-1111-1111-1111-111111111111" in html


def test_provision_operation_progress_is_scoped_to_the_ont(db_session) -> None:
    from app.models.network import OntUnit
    from app.models.network_operation import (
        NetworkOperation,
        NetworkOperationStatus,
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.web_network_operations import get_provision_operation_progress

    target_ont = OntUnit(serial_number="UI-PROGRESS-TARGET")
    other_ont = OntUnit(serial_number="UI-PROGRESS-OTHER")
    db_session.add_all([target_ont, other_ont])
    db_session.flush()
    operation = NetworkOperation(
        operation_type=NetworkOperationType.ont_provision,
        target_type=NetworkOperationTargetType.ont,
        target_id=target_ont.id,
        status=NetworkOperationStatus.pending,
    )
    db_session.add(operation)
    db_session.commit()

    progress = get_provision_operation_progress(
        db_session,
        ont_id=str(target_ont.id),
        operation_id=str(operation.id),
    )
    cross_ont_progress = get_provision_operation_progress(
        db_session,
        ont_id=str(other_ont.id),
        operation_id=str(operation.id),
    )

    assert progress is not None
    assert progress.operation_id == str(operation.id)
    assert progress.status == "Pending"
    assert progress.is_active is True
    assert cross_ont_progress is None
