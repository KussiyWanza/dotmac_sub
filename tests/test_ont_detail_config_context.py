from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.web_network_ont_actions.context_builders import (
    _desired_config_context,
    _operator_summary_context,
)


def test_desired_config_context_prefers_durable_ont_fields(monkeypatch) -> None:
    ont = SimpleNamespace(
        mgmt_ip_mode=SimpleNamespace(value="static"),
        mgmt_vlan=SimpleNamespace(tag=300),
        mgmt_ip_address="10.30.0.44",
        wan_mode=SimpleNamespace(value="pppoe"),
        wan_vlan=SimpleNamespace(tag=203),
        pppoe_username="customer@example",
        lan_gateway_ip="192.168.44.1",
        lan_subnet_mask="255.255.255.0",
        lan_dhcp_enabled=True,
        lan_dhcp_start="192.168.44.20",
        lan_dhcp_end="192.168.44.200",
        wifi_enabled=True,
        wifi_ssid="CustomerWiFi",
        wifi_channel="6",
        wifi_security_mode="WPA2-Personal",
    )

    db = MagicMock()
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.context_builders.resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": None,
            "desired_config_keys": ["management.ip_address"],
            "values": {
                "mgmt_ip_mode": "static",
                "mgmt_vlan": 300,
                "mgmt_ip_address": "10.30.0.44",
                "wan_mode": "pppoe",
                "wan_vlan": 203,
                "pppoe_username": "customer@example",
                "lan_ip": "192.168.44.1",
                "lan_subnet": "255.255.255.0",
                "lan_dhcp_enabled": True,
                "lan_dhcp_start": "192.168.44.20",
                "lan_dhcp_end": "192.168.44.200",
                "wifi_enabled": True,
                "wifi_ssid": "CustomerWiFi",
                "wifi_channel": "6",
                "wifi_security_mode": "WPA2-Personal",
            },
        },
    )
    context = _desired_config_context(
        db,
        ont,
        ont_plan={
            "configure_management_ip": {
                "ip_mode": "dhcp",
                "vlan_id": 100,
                "ip_address": "10.0.0.10",
            },
            "configure_lan_tr069": {
                "lan_ip": "192.168.1.1",
                "dhcp_enabled": False,
            },
            "configure_wifi_tr069": {
                "enabled": False,
                "ssid": "OldWiFi",
            },
        },
        initial_iphost_form={
            "ip_mode": "dhcp",
            "vlan_id": "99",
            "ip_address": "10.0.0.99",
        },
    )

    assert context["desired_mgmt_config"]["ip_mode"] == "static"
    assert context["desired_mgmt_config"]["vlan_id"] == "300"
    assert context["desired_mgmt_config"]["ip_address"] == "10.30.0.44"
    assert context["desired_wan_config"]["wan_mode"] == "pppoe"
    assert context["desired_wan_config"]["wan_vlan"] == "203"
    assert context["desired_wan_config"]["pppoe_username"] == "customer@example"
    assert context["desired_lan_config"]["lan_ip"] == "192.168.44.1"
    assert context["desired_lan_config"]["dhcp_enabled"] is True
    assert context["desired_wifi_config"]["enabled"] is True
    assert context["desired_wifi_config"]["ssid"] == "CustomerWiFi"


def test_operator_summary_does_not_flag_missing_vlans_when_service_ports_deferred() -> (
    None
):
    context = _operator_summary_context(
        desired_mgmt={"vlan_id": "201"},
        desired_wan={"wan_vlan": "212"},
        service_ports_context={
            "service_ports": [],
            "error": "Live OLT service-port read deferred.",
            "deferred": True,
        },
        olt_status={
            "deferred": True,
            "entry": {
                "fsp": "0/0/6",
                "ont_id": "6",
                "description": "Customer",
            },
        },
        has_tr069_device=True,
        current_tr069_profile="DotMac-ACS",
    )

    summary = context["operator_summary"]

    assert summary["service_ports_count"] == 0
    assert summary["service_ports_deferred"] is True
    assert summary["service_ports_error"] == "Live OLT service-port read deferred."
    messages = [blocker["message"] for blocker in summary["blockers"]]
    assert not any("Service-port read failed" in message for message in messages)
    assert not any("Expected management VLAN" in message for message in messages)
    assert not any("Expected internet VLAN" in message for message in messages)
    labels = [label for label, _value in summary["olt_status_rows"]]
    assert "Run State" not in labels
    assert "Config State" not in labels
    assert "Match State" not in labels


def test_unified_config_context_does_not_perform_live_reads(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services import service_intent_ui_adapter
    from app.services import web_network_onts as web_network_onts_service
    from app.services.web_network_ont_actions import context_builders

    ont = OntUnit(
        serial_number="DB-ONLY-CONFIG",
        desired_config={
            "wan": {"pppoe_username": "customer@example"},
            "wifi": {"ssid": "CustomerWiFi"},
        },
    )
    db_session.add(ont)
    db_session.commit()

    def fail_live_read(*_args, **_kwargs):
        raise AssertionError("unified config overview must not perform live reads")

    monkeypatch.setattr(
        web_network_onts_service,
        "get_tr069_profiles_for_ont_with_meta",
        fail_live_read,
    )
    monkeypatch.setattr(
        service_intent_ui_adapter.service_intent_ui_adapter,
        "load_acs_observed_service_intent",
        fail_live_read,
    )

    context = context_builders.unified_config_context(db_session, str(ont.id))

    assert context["desired_wan_config"]["pppoe_username"] == "customer@example"
    assert context["desired_wifi_config"]["ssid"] == "CustomerWiFi"
    assert context["desired_lan_config"]["lan_ip"] == ""
    assert context["iphost_freshness"]["source"] == "db"


def test_unified_config_context_preserves_cached_freshness_and_summaries(
    db_session, monkeypatch
) -> None:
    from datetime import UTC, datetime

    from app.models.network import OLTDevice, OntUnit
    from app.services import service_intent_ui_adapter
    from app.services.olt_observed_state_adapter import ObservedReadResult
    from app.services.web_network_ont_actions import context_builders

    fetched_at = datetime(2026, 4, 21, 10, 30, tzinfo=UTC)
    olt = OLTDevice(name="OLT-A")
    ont = OntUnit(
        serial_number="CTX-SUMMARY-001",
        olt_device=olt,
        desired_config={
            "wan": {"pppoe_username": "db-user"},
            "wifi": {"ssid": "DB-SSID"},
            "management": {"ip_address": "10.30.0.44"},
        },
        observed_wan_ip="41.0.0.10",
        observed_pppoe_status="connected",
    )
    db_session.add_all([olt, ont])
    db_session.commit()

    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter.get_cached_iphost_config",
        lambda _ont: ObservedReadResult(
            ok=True,
            message="Using cached IPHOST configuration.",
            data={"mode": "static"},
            source="db",
            fetched_at=fetched_at,
            stale=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter.get_cached_tr069_profiles_for_olt",
        lambda _olt: ObservedReadResult(
            ok=True,
            message="Using cached TR-069 profile list.",
            data=[SimpleNamespace(profile_id=7, name="ACS Primary")],
            source="db",
            fetched_at=fetched_at,
            stale=True,
        ),
    )
    monkeypatch.setattr(
        service_intent_ui_adapter.service_intent_ui_adapter,
        "load_ont_plan_for_ont",
        lambda *_args, **_kwargs: {
            "configure_management_ip": {
                "ip_mode": "static",
                "vlan_id": 300,
                "ip_address": "10.30.0.44",
            },
        },
    )

    context = context_builders.unified_config_context(db_session, str(ont.id))

    assert context["iphost_freshness"]["stale"] is True
    assert context["iphost_freshness"]["fetched_at"] == fetched_at
    assert context["tr069_profiles_freshness"]["stale"] is True
    assert context["iphost_config"]["mode"] == "static"
    assert context["mgmt_ip_summary"]["ip"] is None
    assert context["wan_summary"]["pppoe_user"] == "db-user"
    assert context["wan_summary"]["wan_ip"] == "41.0.0.10"
    assert context["wan_summary"]["status"] == "connected"
    assert context["wifi_summary"]["ssid"] == "DB-SSID"


def test_unified_config_context_exposes_shared_db_observed_state(
    db_session, monkeypatch
) -> None:
    from datetime import UTC, datetime

    from app.models.network import OLTDevice, OntUnit
    from app.services import service_intent_ui_adapter
    from app.services.olt_observed_state_adapter import ObservedReadResult
    from app.services.web_network_ont_actions import context_builders

    fetched_at = datetime(2026, 4, 21, 11, 0, tzinfo=UTC)
    olt = OLTDevice(name="CTX-TABS-OLT")
    ont = OntUnit(
        serial_number="CTX-TABS-001",
        olt_device=olt,
        desired_config={
            "wan": {"pppoe_username": "shared-user"},
            "wifi": {"ssid": "Shared-SSID"},
            "lan": {"ip": "192.168.55.1", "subnet": "255.255.255.0"},
        },
        observed_wan_ip="41.0.0.20",
        observed_pppoe_status="connected",
    )
    db_session.add_all([olt, ont])
    db_session.commit()

    def fail_live_read(*_args, **_kwargs):
        raise AssertionError("detail tabs must use shared DB/cached observed state")

    monkeypatch.setattr(
        service_intent_ui_adapter.service_intent_ui_adapter,
        "load_acs_observed_service_intent",
        fail_live_read,
    )
    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter.get_cached_iphost_config",
        lambda _ont: ObservedReadResult(
            ok=True,
            message="Using cached IPHOST configuration.",
            data={"mode": "static", "ip_address": "10.30.0.44"},
            source="db",
            fetched_at=fetched_at,
            stale=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter.get_cached_tr069_profiles_for_olt",
        lambda _olt: ObservedReadResult(
            ok=True,
            message="Using cached TR-069 profile list.",
            data=[SimpleNamespace(profile_id=7, name="ACS Primary")],
            source="db",
            fetched_at=fetched_at,
            stale=True,
        ),
    )
    monkeypatch.setattr(
        service_intent_ui_adapter.service_intent_ui_adapter,
        "resolve_effective_tr069_profile",
        lambda *_args, **_kwargs: (None, None),
    )

    context = context_builders.unified_config_context(db_session, str(ont.id))

    assert context["wan_info"]["pppoe_username"] is None
    assert context["wan_info"]["wan_ip"] == "41.0.0.20"
    assert context["wireless_info"]["ssid"] is None
    assert context["lan_info"]["lan_ip"] is None
    assert context["tr069_profiles"][0].name == "ACS Primary"
    assert context["tr069_profiles_freshness"]["fetched_at"] == fetched_at


def test_unified_config_context_includes_configure_form_values(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services.web_network_ont_actions import context_builders

    ont = OntUnit(serial_number="CTX-FORM-VALUES-001")
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        context_builders,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": SimpleNamespace(
                olt_id="olt-config-pack-id",
                name="Residential GPON",
                tr069_profile_name="DotMac ACS",
            ),
            "desired_config_keys": ["wan.mode"],
            "values": {
                "wan_mode": "pppoe",
                "ip_protocol": "ipv4",
                "pppoe_username": "subscriber@example",
                "wan_vlan": 203,
                "wan_vlan_id": "wan-vlan-id",
                "mgmt_ip_mode": "static_ip",
                "mgmt_ip_address": "10.30.0.44",
                "mgmt_vlan": 300,
                "mgmt_vlan_id": "mgmt-vlan-id",
                "lan_ip": "192.168.1.1",
                "wifi_ssid": "DesiredWiFi",
                "wifi_enabled": True,
                "tr069_olt_profile_id": 7,
            },
        },
    )

    context = context_builders.unified_config_context(db_session, str(ont.id))

    assert context["wan_mode"] == "pppoe"
    assert context["ip_protocol"] == "ipv4"
    assert context["pppoe_username"] == "subscriber@example"
    assert context["wan_vlan"] == 203
    assert "wan_vlan_id" not in context
    assert context["mgmt_ip_mode"] == "static_ip"
    assert context["mgmt_ip_address"] == "10.30.0.44"
    assert context["mgmt_vlan"] == 300
    assert "mgmt_vlan_id" not in context
    assert context["lan_gateway_ip"] == "192.168.1.1"
    assert context["wifi_ssid"] == "DesiredWiFi"
    assert context["config_pack_name"] == "Residential GPON"
    assert context["config_pack_olt_id"] == "olt-config-pack-id"
    assert context["tr069_profile_name"] == "DotMac ACS"


def test_configure_form_context_does_not_use_lan_mode_as_wan_mode(
    db_session, monkeypatch
) -> None:
    from app.models.network import OntUnit
    from app.services.web_network_ont_actions import context_builders

    ont = OntUnit(
        serial_number="CTX-WAN-MODE-001",
        observed_lan_mode="router",
    )
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        context_builders,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": None,
            "desired_config_keys": [],
            "values": {
                "wan_mode": "pppoe",
                "ip_protocol": "ipv4",
            },
        },
    )

    context = context_builders.configure_form_context(db_session, str(ont.id))

    assert context["wan_mode"] == "pppoe"


def test_tr069_profiles_resolve_olt_from_active_assignment(
    db_session, monkeypatch
) -> None:
    from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
    from app.services import web_network_onts as web_network_onts_service
    from app.services.olt_observed_state_adapter import ObservedReadResult

    olt = OLTDevice(name="OLT-ASSIGNMENT-ONLY", is_active=True)
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(olt_id=olt.id, name="0/1/7", is_active=True)
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(serial_number="ASSIGNMENT-ONLY-ONT", is_active=True)
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            active=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.olt_observed_state_adapter.get_tr069_profiles_for_olt",
        lambda _db, resolved_olt: ObservedReadResult(
            ok=True,
            message="Using resolved assignment OLT.",
            data=[SimpleNamespace(profile_id=11, name=resolved_olt.name)],
            source="db",
            fetched_at=None,
            stale=False,
        ),
    )

    result = web_network_onts_service.get_tr069_profiles_for_ont_with_meta(
        db_session, ont
    )

    assert result.ok is True
    assert result.data[0].name == "OLT-ASSIGNMENT-ONLY"
