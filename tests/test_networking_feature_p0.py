import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.requests import Request

from app.models.catalog import (
    ConnectionType,
    NasConfigBackup,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
)
from app.models.network import (
    CPEDevice,
    OltConfigBackup,
    OltConfigBackupType,
    OLTDevice,
    OntAssignment,
    OntAuthorizationStatus,
    OntUnit,
    OnuOnlineStatus,
    PollStatus,
    PonPort,
)
from app.models.network_monitoring import (
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DeviceStatus,
    DeviceType,
    DnsThreatAction,
    DnsThreatEvent,
    DnsThreatSeverity,
    InterfaceStatus,
    MetricType,
    NetworkDevice,
    NetworkDeviceBandwidthGraph,
    NetworkDeviceBandwidthGraphSource,
    NetworkDeviceSnmpOid,
    NetworkTopologyLink,
    PopSite,
    SpeedTestResult,
    SpeedTestSource,
    TopologyLinkMedium,
    TopologyLinkRole,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.radius import RadiusClient, RadiusServer
from app.models.radius_active_session import RadiusActiveSession
from app.models.router_management import Router, RouterStatus
from app.models.subscriber import Address, AddressType, Subscriber
from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Event,
    Tr069Job,
    Tr069JobStatus,
    Tr069Parameter,
    Tr069Session,
)
from app.schemas.catalog import NasDeviceCreate
from app.schemas.network import (
    CPEDeviceCreate,
    IPAssignmentCreate,
    IPv4AddressCreate,
    IPv6AddressCreate,
    OLTDeviceCreate,
    OntUnitCreate,
    PonPortCreate,
)
from app.schemas.network_monitoring import NetworkDeviceCreate
from app.schemas.tr069 import Tr069CpeDeviceCreate
from app.services import backup_alerts as backup_alerts_service
from app.services import nas as nas_service
from app.services import network as network_service
from app.services import network_map as network_map_service
from app.services import network_monitoring as monitoring_service
from app.services import network_topology as network_topology_service
from app.services import tr069 as tr069_service
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services import web_network_core_devices_forms as core_devices_forms
from app.services import (
    web_network_core_devices_inventory as core_devices_inventory_service,
)
from app.services import web_network_core_devices_views as core_devices_views
from app.services import web_network_core_runtime as core_runtime
from app.services import web_network_cpes as web_network_cpes_service
from app.services import web_network_dns_threats as web_network_dns_threats_service
from app.services import web_network_ip as web_network_ip_service
from app.services import web_network_monitoring as web_network_monitoring_service
from app.services import web_network_olts as web_network_olts_service
from app.services import web_network_speedtests as web_network_speedtests_service
from app.services import web_network_tr069 as web_network_tr069_service
from app.services.credential_crypto import is_encrypted
from app.services.network import olt_operations as olt_operations_service
from app.services.network import olt_ssh as olt_ssh_service
from app.services.network import olt_web_forms as olt_web_forms_service
from app.services.network import ont_web_forms as ont_web_forms_service
from app.services.network.cpe_tr069 import CpeTR069
from app.services.network.ont_tr069 import OntTR069
from app.web.admin import nas as nas_web
from app.web_domains import network_home


def _assign_ont(db_session, *, ont, pon, subscription):
    olt = db_session.get(OLTDevice, pon.olt_id)
    assert olt is not None
    olt.is_active = True
    db_session.commit()
    return network_service.ont_assignment_commands.assign(
        db_session,
        ont_unit_id=ont.id,
        subscription_id=subscription.id,
        pon_port_id=pon.id,
        subscriber_id=subscription.subscriber_id,
        source="test",
    ).assignment


def _release_ont(db_session, assignment):
    return network_service.ont_assignment_commands.release(
        db_session,
        assignment_id=assignment.id,
        reason="test_inventory_parking",
        source="test",
    ).assignment


def test_validate_ipv4_address_rejects_invalid_octet():
    error = nas_web._validate_ipv4_address("172.16.300.5", "IP address")
    assert error == "IP address must be a valid IPv4 address."


def test_merge_radius_pool_tags_replaces_previous_radius_tags():
    merged = nas_web._merge_radius_pool_tags(
        ["site:pop1", "radius_pool:old-1"],
        ["pool-a", "pool-b"],
    )
    assert merged == ["site:pop1", "radius_pool:pool-a", "radius_pool:pool-b"]


def test_extract_enhanced_fields_from_tags():
    fields = nas_web._extract_enhanced_fields(
        [
            "partner_org:11111111-1111-1111-1111-111111111111",
            "authorization_type:ppp_dhcp_radius",
            "accounting_type:radius_accounting",
            "physical_address:Main Street",
            "latitude:9.0820",
            "longitude:8.6753",
        ]
    )
    assert fields["partner_org_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert fields["authorization_type"] == "ppp_dhcp_radius"
    assert fields["accounting_type"] == "radius_accounting"


def test_extract_enhanced_fields_includes_shaper_and_mikrotik_api_tags():
    fields = nas_web._extract_enhanced_fields(
        [
            "mikrotik_api_enabled:true",
            "mikrotik_api_port:8728",
            "shaper_enabled:true",
            "shaper_target:this_router",
            "shaping_type:queue_tree",
            "wireless_access_list:true",
            "disabled_customers_address_list:false",
            "blocking_rules_enabled:true",
        ]
    )
    assert fields["mikrotik_api_enabled"] == "true"
    assert fields["mikrotik_api_port"] == "8728"
    assert fields["shaper_enabled"] == "true"
    assert fields["shaper_target"] == "this_router"
    assert fields["shaping_type"] == "queue_tree"
    assert fields["wireless_access_list"] == "true"
    assert fields["disabled_customers_address_list"] == "false"
    assert fields["blocking_rules_enabled"] == "true"


def test_nas_index_renders_status_cards_with_non_active_buckets(
    db_session, monkeypatch
):
    device_id = uuid4()
    request = Request(
        {"type": "http", "method": "GET", "path": "/admin/network/nas", "headers": []}
    )

    monkeypatch.setattr(
        nas_web,
        "_base_context",
        lambda _request, _db, active_page, active_menu="network": {
            "request": _request,
            "active_page": active_page,
            "active_menu": active_menu,
            "current_user": SimpleNamespace(
                name="Admin User", email="admin@example.com", initials="AU"
            ),
            "sidebar_stats": SimpleNamespace(
                notifications_unread=0,
                sidebar_logo_url="",
                sidebar_logo_dark_url="",
                app_name="DotMac Subs",
                module_states={},
                feature_states={},
            ),
            "csrf_token": "test-token",
        },
    )
    monkeypatch.setattr(
        nas_web,
        "_get_form_options",
        lambda _db: {
            "vendors": [SimpleNamespace(value="mikrotik", label="MikroTik")],
            "pop_sites": [],
            "organizations": [],
            "statuses": [
                SimpleNamespace(value="active", label="Active"),
                SimpleNamespace(value="offline", label="Offline"),
            ],
        },
    )
    monkeypatch.setattr(
        nas_web.nas_service,
        "build_nas_dashboard_data",
        lambda *args, **kwargs: {
            "devices": [
                SimpleNamespace(
                    id=device_id,
                    name="Edge NAS",
                    model="CCR2004",
                    ip_address="192.0.2.10",
                    vendor=SimpleNamespace(value="mikrotik"),
                    supported_connection_types=[],
                    status=SimpleNamespace(value="offline"),
                    last_seen_at=None,
                )
            ],
            "ping_statuses": {
                str(device_id): {"state": "unreachable", "label": "Unreachable"}
            },
            "runtime_statuses": {},
            "runtime_last_seen": {},
            "stats": {
                "by_vendor": {"mikrotik": 1},
                "by_status": {"active": 1, "offline": 1},
            },
            "pagination": {
                "page": 1,
                "total_pages": 1,
                "total": 1,
                "has_prev": False,
                "has_next": False,
            },
            "filters": {
                "vendor": None,
                "nas_type": None,
                "status": None,
                "pop_site_id": None,
                "partner_org_id": None,
                "olt_status": None,
                "search": None,
                "refresh": None,
            },
        },
    )

    response = nas_web.nas_index(request=request, db=db_session, page=1)

    assert response.status_code == 200
    body = response.body.decode()
    assert "NAS Devices" in body
    assert "Edge NAS" in body
    assert "Offline" in body


def test_build_nas_payload_requires_nas_ip_when_radius_authorization_selected(
    db_session,
):
    payload, errors = nas_service.build_nas_device_payload(
        db_session,
        form={
            "name": "Auth NAS",
            "vendor": "mikrotik",
            "ip_address": "192.0.2.100",
            "status": "active",
            "authorization_type": "ppp_dhcp_radius",
            "supported_connection_types": "[]",
        },
        existing_tags=None,
        for_update=False,
    )
    assert payload is None
    assert any("authorization type is PPP/DHCP Radius" in err for err in errors)


def test_build_nas_payload_normalizes_literal_none_optional_fields(db_session):
    payload, errors = nas_service.build_nas_device_payload(
        db_session,
        form={
            "name": "Optional NAS",
            "vendor": "mikrotik",
            "ip_address": "192.0.2.101",
            "status": "active",
            "supported_connection_types": "[]",
            "nas_identifier": "None",
            "ssh_password": "None",
            "api_password": "None",
            "api_key": "None",
            "snmp_community": "None",
            "backup_schedule": "None",
            "notes": "None",
        },
        existing_tags=None,
        for_update=True,
    )

    assert errors == []
    assert payload is not None
    assert payload.code is None
    assert payload.ssh_password is None
    assert payload.api_password is None
    assert payload.api_token is None
    assert payload.snmp_community is None
    assert "ssh_password" not in payload.model_fields_set
    assert "api_password" not in payload.model_fields_set
    assert "api_token" not in payload.model_fields_set
    assert "snmp_community" not in payload.model_fields_set
    assert payload.backup_schedule is None
    assert payload.notes is None


def test_usable_ipv4_count_handles_common_prefixes():
    assert web_network_ip_service._usable_ipv4_count("10.0.0.0/24") == 254
    assert web_network_ip_service._usable_ipv4_count("10.0.0.0/31") == 2
    assert web_network_ip_service._usable_ipv4_count("not-a-cidr") == 0


def test_get_ping_status_reachable_with_latency(monkeypatch):
    def _fake_run_ping(_host: str, *, timeout_seconds: int = 4):
        assert timeout_seconds == 4
        return True, 12.5

    monkeypatch.setattr(nas_service.ping_service, "run_ping", _fake_run_ping)
    status = nas_service.get_ping_status("192.0.2.10")
    assert status["state"] == "reachable"
    assert status["latency_ms"] == 12.5


def test_get_ping_status_unreachable(monkeypatch):
    def _fake_run_ping(_host: str, *, timeout_seconds: int = 4):
        assert timeout_seconds == 4
        return False, None

    monkeypatch.setattr(nas_service.ping_service, "run_ping", _fake_run_ping)
    status = nas_service.get_ping_status("192.0.2.11")
    assert status == {"state": "unreachable", "label": "Unreachable"}


def test_get_mikrotik_api_status_success(db_session, monkeypatch):
    device = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="MK1",
            vendor=NasVendor.mikrotik,
            api_url="https://router.example",
            api_username="admin",
            api_password="secret",
        ),
    )

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.text = "ok"

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(url, **kwargs):
        if url.endswith("/rest/system/resource"):
            return _Resp(
                {
                    "platform": "MikroTik",
                    "board-name": "CCR",
                    "cpu-load": 17,
                    "ipv6": True,
                }
            )
        return _Resp([{"name": "routeros", "version": "7.15"}])

    monkeypatch.setattr("requests.get", _fake_get)
    status = nas_service.get_mikrotik_api_status(device)
    assert status["platform"] == "MikroTik"
    assert status["board_name"] == "CCR"
    assert status["routeros_version"] == "7.15"


def test_get_mikrotik_pppoe_live_bandwidth_maps_routeros_rates(monkeypatch):
    from app.services.nas._mikrotik import get_mikrotik_pppoe_live_bandwidth

    disconnected = {"called": False}

    class _Resource:
        def __init__(self, rows):
            self.rows = rows

        def get(self):
            return self.rows

        def call(self, command, params):
            assert command == "monitor-traffic"
            assert params == {"interface": "<pppoe-cust001>", "once": ""}
            return [
                {
                    "rx-bits-per-second": "1kbps",
                    "tx-bits-per-second": "9kbps",
                }
            ]

    class _Api:
        def get_resource(self, path):
            if path == "/ppp/active":
                return _Resource(
                    [
                        {
                            "name": "cust001",
                            "interface": "<pppoe-cust001>",
                            "address": "10.0.0.2",
                            "uptime": "1m",
                        }
                    ]
                )
            if path == "/interface":
                return _Resource([])
            raise AssertionError(path)

    class _Pool:
        def __init__(self, *args, **kwargs):
            pass

        def get_api(self):
            return _Api()

        def disconnect(self):
            disconnected["called"] = True

    monkeypatch.setitem(
        sys.modules,
        "routeros_api",
        SimpleNamespace(RouterOsApiPool=_Pool),
    )
    monkeypatch.setattr(
        "app.services.nas._mikrotik._mikrotik_routeros_auth",
        lambda _device: ("192.0.2.1", 8728, "admin", "secret"),
    )

    result = get_mikrotik_pppoe_live_bandwidth(
        SimpleNamespace(id=uuid4(), name="Edge NAS"),
        login="cust001",
    )

    assert result["online"] is True
    assert result["interface"] == "<pppoe-cust001>"
    assert result["download_bps"] == 9000.0
    assert result["upload_bps"] == 1000.0
    assert result["framed_ip_address"] == "10.0.0.2"
    assert disconnected["called"] is True


def test_get_mikrotik_pppoe_live_bandwidth_reports_offline_session(monkeypatch):
    from app.services.nas._mikrotik import get_mikrotik_pppoe_live_bandwidth

    class _Resource:
        def get(self):
            return []

    class _Api:
        def get_resource(self, path):
            assert path == "/ppp/active"
            return _Resource()

    class _Pool:
        def __init__(self, *args, **kwargs):
            pass

        def get_api(self):
            return _Api()

        def disconnect(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "routeros_api",
        SimpleNamespace(RouterOsApiPool=_Pool),
    )
    monkeypatch.setattr(
        "app.services.nas._mikrotik._mikrotik_routeros_auth",
        lambda _device: ("192.0.2.1", 8728, "admin", "secret"),
    )

    result = get_mikrotik_pppoe_live_bandwidth(
        SimpleNamespace(id=uuid4(), name="Edge NAS"),
        login="cust001",
    )

    assert result["online"] is False
    assert result["download_bps"] == 0.0
    assert result["upload_bps"] == 0.0


def test_nas_connection_rules_create_list_toggle_delete(db_session):
    device = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="RuleNAS", vendor=NasVendor.mikrotik, ip_address="192.0.2.21"
        ),
    )
    created = nas_service.NasConnectionRules.create(
        db_session,
        nas_device_id=device.id,
        name="Corp PPPoE",
        connection_type=ConnectionType.pppoe,
        ip_assignment_mode="pool",
        rate_limit_profile="gold",
        priority=10,
    )
    assert created.name == "Corp PPPoE"
    assert created.connection_type == ConnectionType.pppoe
    rules = nas_service.NasConnectionRules.list(
        db_session, nas_device_id=device.id, is_active=None
    )
    assert [r.name for r in rules] == ["Corp PPPoE"]

    disabled = nas_service.NasConnectionRules.set_active(
        db_session,
        rule_id=created.id,
        nas_device_id=device.id,
        is_active=False,
    )
    assert disabled.is_active is False

    nas_service.NasConnectionRules.delete(
        db_session, rule_id=created.id, nas_device_id=device.id
    )
    rules_after_delete = nas_service.NasConnectionRules.list(
        db_session,
        nas_device_id=device.id,
        is_active=None,
    )
    assert rules_after_delete == []


def test_nas_connection_rules_reject_duplicate_name_per_device(db_session):
    device = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="RuleNAS-2", vendor=NasVendor.mikrotik, ip_address="192.0.2.31"
        ),
    )
    nas_service.NasConnectionRules.create(
        db_session,
        nas_device_id=device.id,
        name="Office Rule",
        connection_type=ConnectionType.dhcp,
    )
    with pytest.raises(HTTPException) as exc_info:
        nas_service.NasConnectionRules.create(
            db_session,
            nas_device_id=device.id,
            name="Office Rule",
            connection_type=ConnectionType.pppoe,
        )
    assert "already exists" in str(exc_info.value.detail).lower()


def test_nas_device_detail_data_preserves_vlans_tab(db_session, monkeypatch):
    device = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="VLAN-NAS",
            vendor=NasVendor.mikrotik,
            ip_address="192.0.2.41",
        ),
    )

    monkeypatch.setattr(
        "app.services.nas.web_builders.get_ping_status",
        lambda host: {"state": "unknown", "label": "Unknown"},
    )

    payload = nas_service.build_nas_device_detail_data(
        db_session,
        device_id=str(device.id),
        tab="vlans",
        api_test_status=None,
        api_test_message=None,
        rule_status=None,
        rule_message=None,
        build_activities_fn=lambda *args, **kwargs: [],
    )

    assert payload["active_tab"] == "vlans"


def test_core_devices_list_page_data_filters_site_status_and_search(db_session):
    pop_a = PopSite(name="Alpha Site", is_active=True)
    pop_b = PopSite(name="Beta Site", is_active=True)
    db_session.add_all([pop_a, pop_b])
    db_session.flush()

    dev_a = NetworkDevice(
        name="Core Alpha Router",
        pop_site_id=pop_a.id,
        mgmt_ip="10.100.0.1",
        vendor="MikroTik",
        model="CCR",
        is_active=True,
    )
    dev_b = NetworkDevice(
        name="Edge Beta Switch",
        pop_site_id=pop_b.id,
        mgmt_ip="10.200.0.2",
        vendor="Ubiquiti",
        model="EdgeSwitch",
        is_active=False,
    )
    db_session.add_all([dev_a, dev_b])
    db_session.commit()

    filtered = core_devices_forms.list_page_data(
        db_session,
        role=None,
        status="active",
        pop_site_id=str(pop_a.id),
        search="alpha",
    )
    assert filtered["stats"]["total"] == 1
    assert len(filtered["devices"]) == 1
    assert filtered["devices"][0].name == "Core Alpha Router"


def test_core_devices_list_page_data_includes_uptime_ping_history_and_backup(
    db_session,
):
    pop = PopSite(name="Gamma Site", is_active=True)
    db_session.add(pop)
    db_session.flush()
    device = NetworkDevice(
        name="Gamma Router",
        pop_site_id=pop.id,
        mgmt_ip="10.210.0.1",
        is_active=True,
        ping_enabled=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add(
        DeviceMetric(
            device_id=device.id,
            metric_type=MetricType.uptime,
            value=3661,
            unit="seconds",
            recorded_at=datetime.now(UTC),
        )
    )
    db_session.add(
        DeviceMetric(
            device_id=device.id,
            metric_type=MetricType.custom,
            value=12,
            unit="ping_ms",
            recorded_at=datetime.now(UTC),
        )
    )
    nas = NasDevice(
        name="Gamma NAS",
        vendor=NasVendor.mikrotik,
        ip_address="10.210.0.1",
        management_ip="10.210.0.1",
        network_device_id=device.id,
    )
    db_session.add(nas)
    db_session.flush()
    db_session.add(
        NasConfigBackup(
            nas_device_id=nas.id,
            config_content="export compact",
            config_hash="abc",
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    payload = core_devices_forms.list_page_data(db_session, role=None, status="active")
    key = str(device.id)
    assert payload["uptime_map"][key] is not None
    assert payload["ping_history_map"][key][0]["ok"] is True
    assert payload["backup_map"][key]["status"] == "success"


def test_core_network_table_shows_ping_result_and_device_tooltip():
    template = Path("templates/admin/network/network-devices/index.html").read_text()

    assert "POP: {{ device_pop_name }}" in template
    assert "latest_ping.label if latest_ping else '-'" in template
    assert "latest_ping.recorded_at | app_datetime('%Y-%m-%d %H:%M')" in template
    assert "status-tone-{{ ping_tone }}" in template
    assert "status-tone-dot-{{ ping_tone }}" in template


def test_core_devices_list_page_data_prefers_live_status(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.device_operational_status.warmer_is_stale", lambda now=None: False
    )
    parent = NetworkDevice(
        name="Live Parent",
        mgmt_ip="10.220.0.1",
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(parent)
    db_session.flush()
    child = NetworkDevice(
        name="Live Child",
        mgmt_ip="10.220.0.2",
        parent_device_id=parent.id,
        status=DeviceStatus.degraded,
        live_status="up",
        live_status_at=datetime.now(UTC),
        is_active=True,
    )
    db_session.add(child)
    db_session.commit()

    payload = core_devices_forms.list_page_data(db_session, role=None, status="active")

    displayed_child = next(item for item in payload["devices"] if item.id == child.id)
    assert displayed_child.operational_status == "working"
    assert displayed_child.status_presentation.tone.value == "positive"
    assert payload["child_impacts"][str(parent.id)]["impacted"] is False


def test_core_devices_list_page_data_maps_expired_verification_to_not_working(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.device_operational_status.warmer_is_stale", lambda now=None: True
    )
    device = NetworkDevice(
        name="Stale Live Device",
        mgmt_ip="10.220.0.3",
        status=DeviceStatus.degraded,
        live_status="up",
        live_status_at=datetime.now(UTC) - timedelta(hours=1),
        is_active=True,
    )
    db_session.add(device)
    db_session.commit()

    payload = core_devices_forms.list_page_data(db_session, role=None, status="active")

    displayed = next(item for item in payload["devices"] if item.id == device.id)
    assert displayed.operational_status == "not_working"
    assert displayed.operational.reason == "verification_expired"
    assert not hasattr(displayed, "operational_retry_pending")
    assert displayed.status_presentation.tone.value == "negative"


def test_create_bandwidth_graph_add_source_clone_and_public_toggle(db_session):
    device = NetworkDevice(
        name="Graph Core", mgmt_ip="192.0.2.120", snmp_enabled=True, is_active=True
    )
    db_session.add(device)
    db_session.flush()

    oid = NetworkDeviceSnmpOid(
        device_id=device.id,
        title="ifOutOctets",
        oid="1.3.6.1.2.1.31.1.1.1.10.5",
        is_enabled=True,
    )
    db_session.add(oid)
    db_session.commit()

    ok, msg = core_devices_forms.create_bandwidth_graph_for_device(
        db_session,
        device_id=str(device.id),
        title="Uplink",
        vertical_axis_title="Bandwidth",
        height_px=160,
        is_public=False,
    )
    assert ok is True
    assert "created" in msg.lower()

    graph = (
        db_session.query(NetworkDeviceBandwidthGraph)
        .filter_by(device_id=device.id)
        .first()
    )
    assert graph is not None
    assert graph.is_public is False

    ok, msg = core_devices_forms.add_bandwidth_graph_source(
        db_session,
        device_id=str(device.id),
        graph_id=str(graph.id),
        source_device_id=str(device.id),
        snmp_oid_id=str(oid.id),
        factor=1.0,
        color_hex="#22c55e",
        draw_type="LINE1",
        stack_enabled=False,
        value_unit="Bps",
    )
    assert ok is True
    assert "added" in msg.lower()
    assert (
        db_session.query(NetworkDeviceBandwidthGraphSource)
        .filter_by(graph_id=graph.id)
        .count()
        == 1
    )

    ok, _ = core_devices_forms.clone_bandwidth_graph_for_device(
        db_session,
        device_id=str(device.id),
        graph_id=str(graph.id),
    )
    assert ok is True
    cloned = (
        db_session.query(NetworkDeviceBandwidthGraph)
        .filter(NetworkDeviceBandwidthGraph.title.like("Uplink (Copy)%"))
        .first()
    )
    assert cloned is not None

    ok, _ = core_devices_forms.toggle_bandwidth_graph_public(
        db_session,
        device_id=str(device.id),
        graph_id=str(graph.id),
        is_public=True,
    )
    assert ok is True
    db_session.refresh(graph)
    assert graph.public_token is not None
    public_graph = core_devices_forms.get_public_bandwidth_graph(
        db_session, token=graph.public_token
    )
    assert public_graph is not None


def test_core_backup_settings_history_filter_and_compare(db_session):
    core = NetworkDevice(name="Backup Core", mgmt_ip="192.0.2.130", is_active=True)
    db_session.add(core)
    db_session.flush()
    nas = NasDevice(
        name="Backup NAS",
        vendor=NasVendor.mikrotik,
        ip_address="192.0.2.130",
        management_ip="192.0.2.130",
        network_device_id=core.id,
    )
    db_session.add(nas)
    db_session.flush()

    old_backup = NasConfigBackup(
        nas_device_id=nas.id,
        config_content="line_a\nline_b",
        config_hash="h1",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    new_backup = NasConfigBackup(
        nas_device_id=nas.id,
        config_content="line_a\nline_c",
        config_hash="h2",
        created_at=datetime(2026, 2, 20, tzinfo=UTC),
    )
    db_session.add_all([old_backup, new_backup])
    db_session.commit()

    ok, msg = core_devices_forms.update_backup_settings_for_device(
        db_session,
        device_id=str(core.id),
        enabled=True,
        ssh_username="backup-user",
        ssh_password="secret",
        ssh_port=2222,
        backup_type="commands",
        backup_commands="export",
        hours_csv="2,8,14,20",
    )
    assert ok is True
    assert "saved" in msg.lower()

    payload = core_devices_forms.backup_page_data(
        db_session,
        str(core.id),
        date_from="2026-02-10",
        date_to="2026-02-28",
    )
    assert payload is not None
    assert payload["nas_device"] is not None
    assert len(payload["backups"]) == 1
    assert payload["backups"][0].id == new_backup.id
    assert payload["backup_config"]["ssh_port"] == 2222
    assert payload["backup_config"]["hours_csv"] == "2,8,14,20"

    cmp_payload = core_devices_forms.backup_compare_page_data(
        db_session,
        device_id=str(core.id),
        backup_id_1=str(old_backup.id),
        backup_id_2=str(new_backup.id),
    )
    assert cmp_payload is not None
    assert cmp_payload["diff"]["added_lines"] >= 1
    assert cmp_payload["diff"]["removed_lines"] >= 1
    assert "@@" in cmp_payload["diff"]["unified_diff"]


def test_trigger_backup_for_core_device_without_nas_mapping_returns_error(db_session):
    core = NetworkDevice(name="Orphan Core", mgmt_ip="192.0.2.131", is_active=True)
    db_session.add(core)
    db_session.commit()

    ok, msg = core_devices_forms.trigger_backup_for_core_device(
        db_session,
        device_id=str(core.id),
    )
    assert ok is False
    assert "no linked nas device" in msg.lower()


def test_core_device_update_syncs_linked_nas_ip_and_radius_client(db_session):
    core = NetworkDevice(
        name="Edge Router",
        mgmt_ip="192.0.2.10",
        role=DeviceRole.edge,
        status=DeviceStatus.offline,
        is_active=True,
    )
    db_session.add(core)
    db_session.flush()

    nas = NasDevice(
        name="Edge Router NAS",
        vendor=NasVendor.mikrotik,
        ip_address="192.0.2.10",
        management_ip="192.0.2.10",
        nas_ip="192.0.2.10",
        network_device_id=core.id,
    )
    server = RadiusServer(name="Primary RADIUS", host="127.0.0.1")
    db_session.add_all([nas, server])
    db_session.flush()

    client = RadiusClient(
        server_id=server.id,
        nas_device_id=nas.id,
        client_ip="192.0.2.10",
        shared_secret_hash="secret-hash",
        description=nas.name,
        is_active=True,
    )
    db_session.add(client)
    db_session.commit()

    result = core_devices_forms.update_device(
        db_session,
        core,
        {
            "name": "Edge Router",
            "hostname": None,
            "mgmt_ip": "192.0.2.20",
            "role": DeviceRole.edge,
            "pop_site_id": None,
            "parent_device_id": None,
            "vendor": None,
            "model": None,
            "serial_number": None,
            "device_type": None,
            "ping_enabled": False,
            "snmp_enabled": False,
            "send_notifications": False,
            "notification_delay_minutes": 0,
            "snmp_port": None,
            "snmp_version": None,
            "snmp_community": None,
            "snmp_username": None,
            "snmp_auth_protocol": None,
            "snmp_auth_secret": None,
            "snmp_priv_protocol": None,
            "snmp_priv_secret": None,
            "notes": None,
            "is_active": True,
        },
    )

    assert result.error is None
    db_session.refresh(core)
    db_session.refresh(nas)
    db_session.refresh(client)
    assert core.mgmt_ip == "192.0.2.20"
    assert nas.management_ip == "192.0.2.20"
    assert nas.ip_address == "192.0.2.20"
    assert nas.nas_ip == "192.0.2.20"
    assert client.client_ip == "192.0.2.20"


def test_cpe_notes_metadata_round_trip():
    notes = "[winbox_host:192.0.2.1]\n[api_host:192.0.2.2]\n[api_port:8728]\n[api_user:admin]\nInstalled in suite A"
    meta, cleaned = web_network_cpes_service.parse_cpe_notes_metadata(notes)
    assert meta["winbox_host"] == "192.0.2.1"
    assert meta["api_host"] == "192.0.2.2"
    assert meta["api_port"] == "8728"
    assert meta["api_user"] == "admin"
    assert cleaned == "Installed in suite A"

    normalized = web_network_cpes_service.normalize_cpe_notes(
        notes=cleaned,
        winbox_host=meta["winbox_host"],
        api_host=meta["api_host"],
        api_port=meta["api_port"],
        api_user=meta["api_user"],
    )
    assert normalized is not None
    assert "[winbox_host:192.0.2.1]" in normalized
    assert "Installed in suite A" in normalized


def test_build_cpe_list_data_filters_and_stats(db_session, subscriber):
    other = Subscriber(
        first_name="Other",
        last_name="User",
        email=f"other-{datetime.now(UTC).timestamp()}@example.com",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="MK-1000",
            vendor="MikroTik",
            model="hAP ac2",
            mac_address="AA:BB:CC:DD:EE:01",
            notes="[api_host:192.0.2.20]\ncustomer edge",
        ),
    )
    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=other.id,
            serial_number="ZTE-2000",
            vendor="ZTE",
            model="F660",
            mac_address="AA:BB:CC:DD:EE:02",
        ),
    )

    data = web_network_cpes_service.build_cpe_list_data(
        db_session,
        search="MK-1000",
        status="active",
        vendor="mikrotik",
        subscriber_id=str(subscriber.id),
    )
    assert len(data["cpes"]) == 1
    assert data["cpes"][0].serial_number == "MK-1000"
    assert data["stats"]["total"] == 1
    assert data["stats"]["active"] == 1
    assert data["stats"]["mikrotik"] == 1


def test_build_cpe_list_data_hides_system_owned_inventory_rows(
    db_session, subscriber, subscription
):
    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="VISIBLE-CPE-001",
            vendor="MikroTik",
            model="hAP ax2",
        ),
    )
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="Inv OLT", hostname="inv-olt.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/1"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="PARKED-ONT-001"),
    )
    assigned = _assign_ont(db_session, ont=ont, pon=pon, subscription=subscription)
    _release_ont(db_session, assigned)

    parked_cpe = db_session.scalars(
        select(CPEDevice).where(CPEDevice.serial_number == "PARKED-ONT-001").limit(1)
    ).first()
    assert parked_cpe is not None

    data = web_network_cpes_service.build_cpe_list_data(db_session)
    serials = {cpe.serial_number for cpe in data["cpes"]}
    assert "VISIBLE-CPE-001" in serials
    assert "PARKED-ONT-001" not in serials
    assert all(sub.user_type.value != "system_user" for sub in data["subscribers"])


def test_collect_devices_excludes_system_owned_inventory_cpes(
    db_session, subscriber, subscription
):
    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="VISIBLE-CORE-CPE",
            vendor="ZTE",
            model="F660",
        ),
    )
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="Core Inv OLT", hostname="core-inv-olt.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/2"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="PARKED-CORE-ONT"),
    )
    assignment = _assign_ont(db_session, ont=ont, pon=pon, subscription=subscription)
    _release_ont(db_session, assignment)

    devices = core_devices_inventory_service.collect_devices(db_session)
    cpe_serials = {
        device["serial_number"] for device in devices if device.get("type") == "cpe"
    }
    assert "VISIBLE-CORE-CPE" in cpe_serials
    assert "PARKED-CORE-ONT" not in cpe_serials
    visible_cpe = next(
        device
        for device in devices
        if device.get("serial_number") == "VISIBLE-CORE-CPE"
    )
    assert visible_cpe["status"] == "not_working"
    assert visible_cpe["operational_reason"] == "verification_not_configured"
    assert visible_cpe["status_presentation"].tone.value == "negative"


def test_web_network_home_excludes_system_owned_inventory_cpes(
    db_session, subscriber, subscription
):
    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="VISIBLE-WEB-CPE",
            vendor="Huawei",
            model="EG8145X6",
        ),
    )
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="Web Inv OLT", hostname="web-inv-olt.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/3"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="PARKED-WEB-ONT"),
    )
    assignment = _assign_ont(db_session, ont=ont, pon=pon, subscription=subscription)
    _release_ont(db_session, assignment)

    request = Request({"type": "http", "method": "GET", "path": "/web/network"})
    with patch("app.web_domains.templates.TemplateResponse") as render:
        network_home(request=request, db=db_session)

    context = render.call_args.args[1]
    serials = {item.serial_number for item in context["items"]}
    assert "VISIBLE-WEB-CPE" in serials
    assert "PARKED-WEB-ONT" not in serials


def test_get_or_create_inventory_subscriber_recovers_from_integrity_race(
    db_session, monkeypatch
):
    original_flush = db_session.flush
    original_bind = db_session.get_bind()
    state = {"raised": False}

    def _racing_flush(*args, **kwargs):
        if state["raised"]:
            return original_flush(*args, **kwargs)
        state["raised"] = True
        with Session(bind=original_bind) as other_session:
            existing = network_service.cpe.get_inventory_subscriber(other_session)
            if existing is None:
                network_service.cpe._get_or_create_inventory_subscriber(other_session)
                other_session.commit()
        raise IntegrityError("stmt", "params", "orig")

    monkeypatch.setattr(db_session, "flush", _racing_flush)

    subscriber = network_service.cpe._get_or_create_inventory_subscriber(db_session)

    assert subscriber.email == "network-inventory@dotmac.local"
    assert network_service.cpe.get_inventory_subscriber_id(db_session) == subscriber.id


def test_tr069_dashboard_data_filters_and_stats(db_session, acs_server):
    device_seen = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="ZTE-ONT-001",
            oui="A1B2C3",
            product_class="F680",
            is_active=True,
            last_inform_at=datetime.now(UTC),
        ),
    )
    tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="HUAWEI-002",
            oui="D4E5F6",
            product_class="HG8145X6",
            is_active=True,
            last_inform_at=datetime.now(UTC) - timedelta(days=3),
        ),
    )
    db_session.add(
        Tr069Job(
            device_id=device_seen.id,
            name="Historical Failed Command",
            command="reboot",
            status=Tr069JobStatus.failed,
        )
    )
    db_session.commit()

    data = web_network_tr069_service.tr069_dashboard_data(
        db_session,
        acs_server_id=str(acs_server.id),
        search="zte",
        only_unlinked=True,
    )
    assert len(data["devices"]) == 1
    assert data["devices"][0].serial_number == "ZTE-ONT-001"
    assert data["stats"]["devices"] == 1
    assert data["stats"]["seen_24h"] == 1
    assert data["stats"]["jobs_failed"] >= 1


def test_tr069_queue_device_job_admits_durable_operation(db_session, acs_server):
    device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="TEST-TR069-003",
            oui="112233",
            product_class="ONT",
            is_active=True,
        ),
    )
    device.genieacs_device_id = "112233-ONT-TEST-TR069-003"
    device_id = device.id
    db_session.commit()
    outcome = web_network_tr069_service.queue_device_job(
        db_session,
        tr069_device_id=str(device_id),
        action="refresh",
    )
    assert outcome.command.value == "refreshObject"
    assert outcome.status == Tr069JobStatus.queued
    job = db_session.get(Tr069Job, outcome.job_id)
    assert job is not None
    assert job.network_operation_id == outcome.operation_id


def test_tr069_dashboard_data_decodes_huawei_hex_serial_for_display(
    db_session, acs_server
):
    tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="48575443858C4184",
            oui="485754",
            product_class="HG8145X6",
            is_active=True,
        ),
    )

    data = web_network_tr069_service.tr069_dashboard_data(
        db_session,
        acs_server_id=str(acs_server.id),
    )

    assert len(data["unconfigured_devices"]) == 1
    assert data["unconfigured_devices"][0].display_serial_number == "HWTC858C4184"


def test_tr069_dashboard_data_searches_decoded_huawei_hex_serial(
    db_session, acs_server
):
    tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="4857544313EE6B84",
            oui="485754",
            product_class="HG8145X6",
            is_active=True,
        ),
    )

    data = web_network_tr069_service.tr069_dashboard_data(
        db_session,
        acs_server_id=str(acs_server.id),
        search="HWTC13EE6B84",
    )

    assert len(data["devices"]) == 1
    assert data["devices"][0].display_serial_number == "HWTC13EE6B84"


def test_tr069_dashboard_data_handles_joinedload_assignments_without_500(
    db_session, acs_server
):
    olt = OLTDevice(
        name="TR069 OLT",
        status="active",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()

    pon_port = PonPort(
        olt_id=olt.id,
        name="PON 1/1/1",
        port_number=1,
        is_active=True,
    )
    db_session.add(pon_port)
    db_session.flush()

    ont = OntUnit(
        serial_number="HWTC858C4184",
        olt_device_id=olt.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()

    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        active=True,
    )
    db_session.add(assignment)

    tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="48575443858C4184",
            oui="485754",
            product_class="HG8145X6",
            is_active=True,
        ),
    )
    db_session.commit()

    data = web_network_tr069_service.tr069_dashboard_data(
        db_session,
        acs_server_id=str(acs_server.id),
    )

    assert len(data["devices"]) == 1
    assert data["devices"][0].linked_ont is not None
    assert data["devices"][0].linked_pon_port_name == "PON 1/1/1"


def test_tr069_dashboard_hides_system_owned_inventory_cpes_from_selector(
    db_session, acs_server, subscriber, subscription
):
    visible_cpe = network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="VISIBLE-TR069-CPE",
            vendor="Huawei",
            model="HG8145V5",
        ),
    )
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="TR069 Inv OLT", hostname="tr069-inv-olt.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/4"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="PARKED-TR069-ONT"),
    )
    assignment = _assign_ont(db_session, ont=ont, pon=pon, subscription=subscription)
    _release_ont(db_session, assignment)

    data = web_network_tr069_service.tr069_dashboard_data(
        db_session,
        acs_server_id=str(acs_server.id),
    )

    labels = set(data["cpe_typeahead_labels"])
    assert any("VISIBLE-TR069-CPE" in label for label in labels)
    assert all("PARKED-TR069-ONT" not in label for label in labels)
    assert str(visible_cpe.id) in data["cpe_display_by_id"]


def test_tr069_dashboard_linked_inventory_cpe_uses_device_label_not_inventory_name(
    db_session, acs_server, subscriber, subscription
):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="TR069 Link OLT", hostname="tr069-link-olt.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/6"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="LINKED-PARKED-ONT"),
    )
    assignment = _assign_ont(db_session, ont=ont, pon=pon, subscription=subscription)
    _release_ont(db_session, assignment)

    parked_cpe = db_session.scalars(
        select(CPEDevice).where(CPEDevice.serial_number == "LINKED-PARKED-ONT").limit(1)
    ).first()
    assert parked_cpe is not None

    linked_device = Tr069CpeDevice(
        acs_server_id=acs_server.id,
        serial_number="LINKED-PARKED-ONT",
        cpe_device_id=parked_cpe.id,
        is_active=True,
    )
    db_session.add(linked_device)
    db_session.commit()

    data = web_network_tr069_service.tr069_dashboard_data(
        db_session,
        acs_server_id=str(acs_server.id),
    )

    linked = next(
        device
        for device in data["configured_devices"]
        if device.serial_number == "LINKED-PARKED-ONT"
    )
    assert linked.linked_cpe is not None
    assert linked.linked_cpe_display_label == "LINKED-PARKED-ONT"
    assert all(
        "Network Inventory" not in label for label in data["cpe_typeahead_labels"]
    )


def test_link_tr069_device_to_cpe_rejects_missing_cpe(db_session, acs_server):
    tr069_device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="TR069-LINK-MISSING",
            is_active=True,
        ),
    )

    with pytest.raises(HTTPException, match="CPE device not found") as exc_info:
        web_network_tr069_service.link_tr069_device_to_cpe(
            db_session,
            tr069_device_id=str(tr069_device.id),
            cpe_device_id=str(uuid4()),
        )
    assert exc_info.value.status_code == 404


def test_link_tr069_device_to_cpe_rejects_parked_inventory_cpe(
    db_session, acs_server, subscriber, subscription
):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(
            name="TR069 Reject OLT",
            hostname="tr069-reject-olt.local",
        ),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/7"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="REJECT-PARKED-ONT"),
    )
    assignment = _assign_ont(db_session, ont=ont, pon=pon, subscription=subscription)
    _release_ont(db_session, assignment)
    parked_cpe = db_session.scalars(
        select(CPEDevice).where(CPEDevice.serial_number == "REJECT-PARKED-ONT").limit(1)
    ).first()
    assert parked_cpe is not None

    tr069_device = tr069_service.cpe_devices.create(
        db_session,
        Tr069CpeDeviceCreate(
            acs_server_id=acs_server.id,
            serial_number="TR069-LINK-PARKED",
            is_active=True,
        ),
    )

    with pytest.raises(
        HTTPException, match="Cannot link TR-069 device to parked inventory CPE"
    ) as exc_info:
        web_network_tr069_service.link_tr069_device_to_cpe(
            db_session,
            tr069_device_id=str(tr069_device.id),
            cpe_device_id=str(parked_cpe.id),
        )
    assert exc_info.value.status_code == 400


def test_network_map_context_includes_network_device_markers(db_session):
    pop = PopSite(
        name="Map POP",
        code="MAP-POP",
        latitude=9.05,
        longitude=7.49,
        is_active=True,
    )
    db_session.add(pop)
    db_session.flush()

    db_session.add(
        NetworkDevice(
            name="Core Router 1",
            pop_site_id=pop.id,
            role=DeviceRole.core,
            status=DeviceStatus.online,
            live_status="up",
            live_status_at=datetime.now(UTC),
            ping_enabled=True,
            last_ping_ok=True,
            last_ping_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db_session.commit()

    context = network_map_service.build_network_map_context(db_session)
    device_features = [
        item
        for item in context["map_data"]["features"]
        if item.get("properties", {}).get("type") == "network_device"
    ]
    assert len(device_features) == 1
    assert device_features[0]["properties"]["name"] == "Core Router 1"
    assert device_features[0]["properties"]["status"] == "working"
    assert device_features[0]["properties"]["status_presentation"]["tone"] == "positive"
    assert context["stats"]["network_devices"] == 1
    assert context["stats"]["network_devices_working"] == 1


def test_network_map_context_uses_operational_ont_status(db_session):
    ont = OntUnit(
        serial_number="ONT-MAP-1",
        name="Map ONT",
        is_active=True,
        use_gps=True,
        gps_latitude=9.07,
        gps_longitude=7.51,
        olt_status=OnuOnlineStatus.online,
        olt_status_seen_at=datetime.now(UTC),
    )
    db_session.add(ont)
    db_session.commit()

    context = network_map_service.build_network_map_context(db_session)
    ont_features = [
        item
        for item in context["map_data"]["features"]
        if item.get("properties", {}).get("type") == "ont"
    ]

    assert len(ont_features) == 1
    assert ont_features[0]["properties"]["status"] == "working"
    assert ont_features[0]["properties"]["status_reason"] == "olt_online"
    assert context["stats"]["onts_working"] == 1
    assert context["stats"]["onts_not_working"] == 0


def test_monitoring_dashboard_stats_use_operational_state(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.device_operational_status.warmer_is_stale", lambda now=None: False
    )
    monkeypatch.setattr("app.services.monitoring_coverage.get_coverage", lambda: None)
    db_session.add_all(
        [
            NetworkDevice(
                name="Observed Up",
                status=DeviceStatus.online,
                live_status="up",
                is_active=True,
            ),
            NetworkDevice(
                name="Observed Down",
                status=DeviceStatus.online,
                live_status="down",
                is_active=True,
            ),
            NetworkDevice(
                name="Observed Degraded",
                status=DeviceStatus.online,
                live_status="problem",
                is_active=True,
            ),
            NetworkDevice(
                name="Refresh Pending",
                status=DeviceStatus.online,
                live_status=None,
                is_active=True,
            ),
            NetworkDevice(
                name="Planned Maintenance",
                status=DeviceStatus.maintenance,
                live_status="down",
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    payload = monitoring_service.NetworkDevices.get_monitoring_dashboard_stats(
        db_session,
        format_duration=lambda value: str(value),
        format_bps=lambda value: str(value),
    )

    assert payload["stats"] == {
        "devices_total": 5,
        "devices_working": 2,
        "devices_not_working": 3,
        "devices_impaired": 1,
        "alarms_open": 0,
        "subscribers_online": 0,
    }
    assert payload["device_status_chart"]["labels"] == [
        "Working",
        "Not working",
    ]
    assert payload["device_status_chart"]["tones"] == [
        "positive",
        "negative",
    ]
    assert "colors" not in payload["device_status_chart"]


def test_site_reachability_uses_binary_operation_and_separate_impairment(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.device_operational_status.warmer_is_stale", lambda now=None: False
    )
    monkeypatch.setattr("app.services.monitoring_coverage.get_coverage", lambda: None)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            NetworkDevice(
                name="Site Working",
                mgmt_ip="172.16.1.1",
                status=DeviceStatus.online,
                live_status="up",
                live_status_at=now,
                is_active=True,
            ),
            NetworkDevice(
                name="Site Impaired",
                mgmt_ip="172.16.1.2",
                status=DeviceStatus.online,
                live_status="problem",
                live_status_at=now,
                is_active=True,
            ),
            NetworkDevice(
                name="Site Unverified",
                mgmt_ip="172.16.1.3",
                status=DeviceStatus.online,
                live_status=None,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    sites = web_network_monitoring_service.get_site_reachability(db_session)

    assert sites == [
        {
            "subnet": "172.16.0.0/16",
            "name": "Abuja Management",
            "total": 3,
            "working": 2,
            "not_working": 1,
            "impaired": 1,
            "working_pct": 67,
        }
    ]


def test_speedtest_form_parse_and_validate():
    values = web_network_speedtests_service.parse_speedtest_form(
        {
            "download_mbps": "98.5",
            "upload_mbps": "45.25",
            "latency_ms": "8.1",
            "source": "manual",
        }
    )
    assert values["download_mbps"] == 98.5
    assert values["upload_mbps"] == 45.25
    assert values["latency_ms"] == 8.1
    assert web_network_speedtests_service.validate_speedtest_values(values) is None

    bad = dict(values)
    bad["download_mbps"] = -1
    assert web_network_speedtests_service.validate_speedtest_values(bad) is not None


def test_speedtest_list_page_data_filters_and_stats(db_session, subscriber):
    pop = PopSite(
        name="Speed POP",
        code="SPD-POP",
        latitude=9.1,
        longitude=7.5,
        is_active=True,
    )
    db_session.add(pop)
    db_session.flush()

    device = NetworkDevice(
        name="Speed Device",
        pop_site_id=pop.id,
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add_all(
        [
            SpeedTestResult(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                source=SpeedTestSource.manual,
                target_label="Link A",
                provider="Ookla",
                server_name="Abuja-1",
                download_mbps=100,
                upload_mbps=50,
                latency_ms=10,
                tested_at=datetime.now(UTC),
            ),
            SpeedTestResult(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                source=SpeedTestSource.api,
                target_label="Link B",
                provider="LibreSpeed",
                server_name="Abuja-2",
                download_mbps=80,
                upload_mbps=40,
                latency_ms=20,
                tested_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    data = web_network_speedtests_service.list_page_data(
        db_session,
        search="ookla",
        subscriber_id=str(subscriber.id),
        network_device_id=str(device.id),
        source="manual",
    )
    assert len(data["results"]) == 1
    assert data["results"][0].provider == "Ookla"
    assert data["stats"]["total"] == 1
    assert data["stats"]["avg_download"] == 100.0


def test_dns_threat_form_parse_and_validate():
    values = web_network_dns_threats_service.parse_event_form(
        {
            "queried_domain": "malicious.example",
            "severity": "high",
            "action": "blocked",
            "confidence_score": "92.4",
        }
    )
    assert values["queried_domain"] == "malicious.example"
    assert values["confidence_score"] == 92.4
    assert web_network_dns_threats_service.validate_event_values(values) is None

    bad = dict(values)
    bad["queried_domain"] = ""
    assert web_network_dns_threats_service.validate_event_values(bad) is not None


def test_dns_threat_list_page_data_filters_and_stats(db_session, subscriber):
    pop = PopSite(
        name="DNS POP", code="DNS-POP", latitude=9.2, longitude=7.6, is_active=True
    )
    db_session.add(pop)
    db_session.flush()

    device = NetworkDevice(
        name="DNS Guard",
        pop_site_id=pop.id,
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()

    db_session.add_all(
        [
            DnsThreatEvent(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                queried_domain="phishing.example",
                source_ip="192.0.2.11",
                threat_category="phishing",
                severity=DnsThreatSeverity.critical,
                action=DnsThreatAction.blocked,
                confidence_score=97.2,
                occurred_at=datetime.now(UTC),
            ),
            DnsThreatEvent(
                subscriber_id=subscriber.id,
                network_device_id=device.id,
                pop_site_id=pop.id,
                queried_domain="telemetry.example",
                source_ip="192.0.2.12",
                threat_category="suspicious",
                severity=DnsThreatSeverity.low,
                action=DnsThreatAction.monitored,
                confidence_score=40.0,
                occurred_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    data = web_network_dns_threats_service.list_page_data(
        db_session,
        search="phishing",
        severity="critical",
        action="blocked",
        subscriber_id=str(subscriber.id),
        network_device_id=str(device.id),
    )
    assert len(data["events"]) == 1
    assert data["events"][0].queried_domain == "phishing.example"
    assert data["stats"]["total"] == 1
    assert data["stats"]["blocked"] == 1
    assert data["stats"]["critical"] == 1


def test_dns_threat_list_page_data_handles_missing_table(db_session, monkeypatch):
    monkeypatch.setattr(
        web_network_dns_threats_service,
        "_dns_threat_table_exists",
        lambda _db: False,
    )

    data = web_network_dns_threats_service.list_page_data(db_session)

    assert data["events"] == []
    assert data["stats"]["total"] == 0
    assert data["schema_missing"] is True


def test_topology_graph_builds_link_states_from_interface_metrics(db_session, pop_site):
    parent = NetworkDevice(
        name="Topo Parent",
        pop_site_id=pop_site.id,
        role=DeviceRole.core,
        status=DeviceStatus.online,
        is_active=True,
    )
    child = NetworkDevice(
        name="Topo Child",
        pop_site_id=pop_site.id,
        role=DeviceRole.edge,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add_all([parent, child])
    db_session.flush()

    parent_if = DeviceInterface(device_id=parent.id, name="xe-0/0/0", monitored=True)
    child_if = DeviceInterface(device_id=child.id, name="xe-0/0/1", monitored=True)
    db_session.add_all([parent_if, child_if])
    db_session.flush()

    link = NetworkTopologyLink(
        source_device_id=parent.id,
        source_interface_id=parent_if.id,
        target_device_id=child.id,
        target_interface_id=child_if.id,
        link_role=TopologyLinkRole.uplink,
        medium=TopologyLinkMedium.fiber,
        capacity_bps=1_000_000_000,
        is_active=True,
    )
    now = datetime.now(UTC)
    db_session.add(link)
    db_session.add_all(
        [
            DeviceMetric(
                device_id=parent.id,
                interface_id=parent_if.id,
                metric_type=MetricType.rx_bps,
                value=600_000_000,
                recorded_at=now,
            ),
            DeviceMetric(
                device_id=parent.id,
                interface_id=parent_if.id,
                metric_type=MetricType.tx_bps,
                value=120_000_000,
                recorded_at=now,
            ),
        ]
    )
    db_session.commit()

    data = network_topology_service.list_nodes_and_edges(db_session)
    assert data["stats"]["node_count"] >= 2
    assert data["stats"]["edge_count"] >= 1
    edge = next(edge for edge in data["edges"] if edge["id"] == str(link.id))
    assert edge["utilization_pct"] == 60.0
    assert edge["util_state"] == "moderate"
    assert edge["utilization_basis"] == "max_direction"


def test_core_device_validate_values_rejects_parent_cycle(db_session):
    parent = NetworkDevice(name="Parent A", is_active=True)
    child = NetworkDevice(name="Child B", is_active=True, parent_device=parent)
    db_session.add_all([parent, child])
    db_session.commit()

    values = {
        "name": "Parent A",
        "hostname": None,
        "mgmt_ip": None,
        "role_value": "edge",
        "device_type_value": "",
        "pop_site_id": None,
        "parent_device_id": str(child.id),
        "ping_enabled": False,
        "snmp_enabled": False,
        "snmp_port_value": "",
        "vendor": None,
        "model": None,
        "serial_number": None,
        "snmp_version": None,
        "snmp_community": None,
        "snmp_username": None,
        "snmp_auth_protocol": None,
        "snmp_auth_secret": None,
        "snmp_priv_protocol": None,
        "snmp_priv_secret": None,
        "notes": None,
        "is_active": True,
    }
    normalized, error = core_devices_forms.validate_values(
        db_session,
        values,
        current_device=parent,
    )
    assert normalized is None
    assert error is not None
    assert "cycle" in error.lower()


def test_core_device_create_allows_save_when_ping_probe_fails(db_session, monkeypatch):
    monkeypatch.setattr(core_devices_forms, "run_ping_probe", lambda host: False)

    values = {
        "name": "Reachability OLT",
        "hostname": None,
        "mgmt_ip": "192.0.2.250",
        "role": DeviceRole.access,
        "device_type": None,
        "pop_site_id": None,
        "parent_device_id": None,
        "ping_enabled": True,
        "snmp_enabled": False,
        "send_notifications": True,
        "notification_delay_minutes": 0,
        "snmp_port": None,
        "snmp_version": None,
        "snmp_community": None,
        "snmp_username": None,
        "snmp_auth_protocol": None,
        "snmp_auth_secret": None,
        "snmp_priv_protocol": None,
        "snmp_priv_secret": None,
        "notes": None,
        "is_active": True,
        "host": "192.0.2.250",
    }

    result = core_devices_forms.create_device(db_session, values)

    assert result.device is not None
    assert result.error is None
    assert result.warning == "Ping probe failed. Check the management IP/hostname."
    assert result.device.last_ping_ok is False


def test_ping_device_respects_notification_delay_before_offline(
    db_session, pop_site, monkeypatch
):
    device = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Delay Ping Device",
            pop_site_id=pop_site.id,
            mgmt_ip="192.0.2.80",
            notification_delay_minutes=5,
            status="online",
        ),
    )

    def _fake_run_ping(_host: str, *, timeout_seconds: int = 4):
        assert timeout_seconds == 4
        return False, None

    monkeypatch.setattr(core_runtime.ping_service, "run_ping", _fake_run_ping)
    updated, err, ok = core_runtime.ping_device(db_session, str(device.id))
    assert err is None
    assert ok is False
    assert updated is not None
    assert updated.status.value == "online"
    assert updated.ping_down_since is not None

    updated.ping_down_since = datetime.now(UTC) - timedelta(minutes=6)
    db_session.flush()
    updated2, err2, ok2 = core_runtime.ping_device(db_session, str(device.id))
    assert err2 is None
    assert ok2 is False
    assert updated2 is not None
    assert updated2.status.value == "offline"


def test_parent_status_rollup_from_child_ping_failure_and_recovery(
    db_session, pop_site, monkeypatch
):
    parent = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Parent Node",
            pop_site_id=pop_site.id,
            mgmt_ip="192.0.2.90",
            notification_delay_minutes=0,
            status="online",
        ),
    )
    child = monitoring_service.network_devices.create(
        db_session,
        NetworkDeviceCreate(
            name="Child Node",
            pop_site_id=pop_site.id,
            parent_device_id=parent.id,
            mgmt_ip="192.0.2.91",
            notification_delay_minutes=0,
            status="online",
        ),
    )
    assert child.parent_device_id == parent.id

    def _fail_run_ping(_host: str, *, timeout_seconds: int = 4):
        assert timeout_seconds == 4
        return False, None

    monkeypatch.setattr(core_runtime.ping_service, "run_ping", _fail_run_ping)
    core_runtime.ping_device(db_session, str(child.id))
    db_session.refresh(child)
    assert child.status.value == "offline"
    db_session.refresh(parent)
    assert parent.status.value == "degraded"

    def _ok_run_ping(_host: str, *, timeout_seconds: int = 4):
        assert timeout_seconds == 4
        return True, 1.1

    monkeypatch.setattr(core_runtime.ping_service, "run_ping", _ok_run_ping)
    core_runtime.ping_device(db_session, str(child.id))
    db_session.refresh(parent)
    assert parent.status.value == "online"


def test_parent_devices_for_forms_scopes_by_site_and_excludes_descendants(db_session):
    site_a = PopSite(name="Site A", is_active=True)
    site_b = PopSite(name="Site B", is_active=True)
    db_session.add_all([site_a, site_b])
    db_session.flush()

    root = NetworkDevice(name="Root A", pop_site_id=site_a.id, is_active=True)
    child = NetworkDevice(
        name="Child A", pop_site_id=site_a.id, is_active=True, parent_device=root
    )
    grandchild = NetworkDevice(
        name="Grandchild A",
        pop_site_id=site_a.id,
        is_active=True,
        parent_device=child,
    )
    other_site = NetworkDevice(
        name="Other Site Root", pop_site_id=site_b.id, is_active=True
    )
    db_session.add_all([root, child, grandchild, other_site])
    db_session.commit()

    candidates = core_devices_forms.parent_devices_for_forms(
        db_session,
        current_device_id=root.id,
        pop_site_id=site_a.id,
    )
    candidate_ids = {device.id for device in candidates}
    assert child.id not in candidate_ids
    assert grandchild.id not in candidate_ids
    assert other_site.id not in candidate_ids


def test_backup_overview_page_data_classifies_and_filters(db_session):
    pop_site = PopSite(name="POP A", is_active=True)
    db_session.add(pop_site)
    db_session.flush()

    nas_fresh = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Fresh",
            vendor=NasVendor.mikrotik,
            management_ip="192.0.2.10",
            pop_site_id=pop_site.id,
        ),
    )
    nas_stale = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Stale",
            vendor=NasVendor.mikrotik,
            management_ip="192.0.2.11",
            pop_site_id=pop_site.id,
        ),
    )
    nas_failed = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Failed",
            vendor=NasVendor.mikrotik,
            management_ip="192.0.2.12",
            pop_site_id=pop_site.id,
        ),
    )

    fresh_backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas_fresh.id,
            config_content="/export compact",
        ),
    )
    stale_backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas_stale.id,
            config_content="/export stale",
        ),
    )
    failed_backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas_failed.id,
            config_content="x",
            notes="Backup failed due to timeout",
        ),
    )
    stale_backup.created_at = datetime.now(UTC) - timedelta(hours=60)
    db_session.flush()

    olt = OLTDevice(
        name="OLT North", mgmt_ip="198.51.100.20", vendor="Huawei", model="MA5800"
    )
    db_session.add(olt)
    db_session.flush()
    olt_backup = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="olt/backup.txt",
        file_size_bytes=1024,
        created_at=datetime.now(UTC),
    )
    db_session.add(olt_backup)
    db_session.commit()

    page = core_devices_views.backup_overview_page_data(db_session, stale_hours=24)
    by_name = {row["device_name"]: row for row in page["rows"]}
    assert by_name["NAS Fresh"]["backup_status"] == "success"
    assert by_name["NAS Stale"]["backup_status"] == "stale"
    assert by_name["NAS Failed"]["backup_status"] == "failed"
    assert by_name["OLT North"]["backup_status"] == "success"
    assert by_name["OLT North"]["backup_url"] == (
        f"/admin/network/olts/backups/{olt_backup.id}"
    )
    assert by_name["OLT North"]["history_url"] == (
        f"/admin/network/olts/{olt.id}/backups"
    )
    assert page["stats"]["total"] == 4
    assert page["stats"]["stale"] == 1
    assert page["stats"]["failed"] == 1
    assert fresh_backup.id != stale_backup.id != failed_backup.id

    stale_only = core_devices_views.backup_overview_page_data(
        db_session,
        stale_hours=24,
        status="stale",
        device_type="nas",
    )
    assert stale_only["stats"]["total"] == 1
    assert stale_only["rows"][0]["device_name"] == "NAS Stale"


def test_backup_overview_page_data_search_and_sort(db_session):
    nas = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="NAS Searchable",
            vendor=NasVendor.ubiquiti,
            management_ip="203.0.113.5",
        ),
    )
    backup = nas_service.NasConfigBackups.create(
        db_session,
        nas_service.NasConfigBackupCreate(
            nas_device_id=nas.id,
            config_content="/export verbose",
        ),
    )
    backup.created_at = datetime.now(UTC) - timedelta(hours=2)

    olt = OLTDevice(name="OLT Searchable", mgmt_ip="203.0.113.8")
    db_session.add(olt)
    db_session.flush()
    db_session.add(
        OltConfigBackup(
            olt_device_id=olt.id,
            backup_type=OltConfigBackupType.manual,
            file_path="olt/searchable.txt",
            file_size_bytes=2048,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    db_session.commit()

    filtered = core_devices_views.backup_overview_page_data(
        db_session,
        search="203.0.113.5",
        sort="last_backup_desc",
    )
    assert filtered["stats"]["total"] == 1
    assert filtered["rows"][0]["device_name"] == "NAS Searchable"


def test_consolidated_page_data_search_includes_onts_beyond_default_limit(db_session):
    for idx in range(500):
        db_session.add(OntUnit(serial_number=f"AA-{idx:04d}", is_active=True))
    target = OntUnit(
        serial_number="ZZ-TARGET-ONT", is_active=True, mgmt_ip_address="10.55.66.77"
    )
    db_session.add(target)
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(
        tab="onts",
        db=db_session,
        search="ZZ-TARGET-ONT",
    )
    serials = {ont.serial_number for ont in payload["onts"]}
    assert "ZZ-TARGET-ONT" in serials


def test_consolidated_page_data_includes_ont_signal_data(db_session):
    ont = OntUnit(
        serial_number="ONT-CONSOLIDATED-STATUS",
        is_active=True,
        olt_status=OnuOnlineStatus.online,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(
        tab="onts",
        db=db_session,
        search="ONT-CONSOLIDATED-STATUS",
    )

    signal_data = payload["signal_data"][str(ont.id)]
    assert signal_data["operational"] == "not_working"
    assert signal_data["status_presentation"].label == "Not working"
    assert signal_data["operational_reason"] == "verification_expired"


def test_consolidated_page_data_moves_network_devices_ending_in_olt_to_olt_bucket(
    db_session,
):
    promoted = NetworkDevice(
        name="Aggregation OLT",
        hostname="agg-olt.local",
        mgmt_ip="192.0.2.210",
        vendor="Huawei",
        model="MA5800",
        role=DeviceRole.aggregation,
        status=DeviceStatus.online,
        is_active=True,
    )
    core = NetworkDevice(
        name="Aggregation SW1",
        hostname="agg-sw1.local",
        mgmt_ip="192.0.2.211",
        vendor="Cisco",
        model="NCS",
        role=DeviceRole.aggregation,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add_all([promoted, core])
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(tab="core", db=db_session)
    olt_payload = core_devices_views.consolidated_page_data(tab="olts", db=db_session)

    assert payload["stats"]["core_total"] == 1
    assert payload["stats"]["olt_total"] == 1
    assert [device.name for device in payload["core_devices"]] == ["Aggregation SW1"]
    assert payload["olts"] == []
    assert [olt.name for olt in olt_payload["olts"]] == ["Aggregation OLT"]
    created_olt = db_session.scalars(
        select(OLTDevice).where(OLTDevice.mgmt_ip == "192.0.2.210")
    ).first()
    assert created_olt is not None
    assert created_olt.is_active is False
    assert "Auto-created from monitoring inventory" in str(created_olt.notes or "")


def test_consolidated_page_data_includes_active_nas_inventory_devices(db_session):
    db_session.add(
        NasDevice(
            name="Fiber POP NAS",
            code="fiber-pop-nas",
            vendor=NasVendor.huawei,
            model="MA5608T",
            management_ip="192.0.2.230",
            status=NasDeviceStatus.active,
            is_active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(tab="core", db=db_session)

    included = next(
        device for device in payload["core_devices"] if device.name == "Fiber POP NAS"
    )
    assert included.detail_url.endswith(f"/admin/network/nas/devices/{included.id}")


def test_devices_list_page_data_includes_core_network_devices(db_session):
    core = NetworkDevice(
        name="Distribution Switch 1",
        hostname="dist-sw1.local",
        mgmt_ip="192.0.2.240",
        vendor="Ubiquiti",
        model="EdgeSwitch",
        serial_number="SW-001",
        device_type=DeviceType.switch,
        role=DeviceRole.distribution,
        status=DeviceStatus.online,
        is_active=True,
    )
    olt_monitor = NetworkDevice(
        name="Lab Huawei OLT",
        hostname="lab-olt.local",
        mgmt_ip="192.0.2.241",
        vendor="Huawei",
        model="MA5608T",
        device_type=DeviceType.switch,
        role=DeviceRole.access,
        status=DeviceStatus.online,
        is_active=True,
    )
    olt = OLTDevice(
        name="Lab Huawei OLT",
        hostname="lab-olt.local",
        mgmt_ip="192.0.2.241",
        vendor="Huawei",
        model="MA5608T",
        is_active=True,
    )
    db_session.add_all([core, olt_monitor, olt])
    db_session.commit()

    from app.services.device_projection_reconcile import (
        ReconcileDeviceProjectionsCommand,
        reconcile_device_projections,
    )
    from app.services.owner_commands import CommandContext

    reconcile_device_projections(
        db_session,
        ReconcileDeviceProjectionsCommand(
            context=CommandContext.system(
                actor="pytest:networking_feature_p0",
                scope="network:test",
                reason="materialize device-list test projection",
            )
        ),
    )
    query = core_devices_inventory_service.build_network_device_list_query()
    payload = core_devices_inventory_service.devices_list_page_data(db_session, query)

    names_by_type = {(device["name"], device["type"]) for device in payload["devices"]}
    assert ("Distribution Switch 1", "core") in names_by_type
    assert ("Lab Huawei OLT", "olt") in names_by_type
    assert ("Lab Huawei OLT", "core") not in names_by_type
    assert payload["stats"]["core"] == 1


def test_devices_list_page_data_filters_core_network_devices(db_session):
    db_session.add_all(
        [
            NetworkDevice(
                name="Core Router 1",
                mgmt_ip="192.0.2.242",
                device_type=DeviceType.router,
                role=DeviceRole.core,
                status=DeviceStatus.online,
                is_active=True,
            ),
            OLTDevice(
                name="Access OLT",
                mgmt_ip="192.0.2.243",
                vendor="Huawei",
                model="MA5800",
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    from app.services.device_projection_reconcile import (
        ReconcileDeviceProjectionsCommand,
        reconcile_device_projections,
    )
    from app.services.owner_commands import CommandContext

    reconcile_device_projections(
        db_session,
        ReconcileDeviceProjectionsCommand(
            context=CommandContext.system(
                actor="pytest:networking_feature_p0",
                scope="network:test",
                reason="materialize filtered device-list test projection",
            )
        ),
    )
    query = core_devices_inventory_service.build_network_device_list_query(
        device_type="core"
    )
    payload = core_devices_inventory_service.devices_list_page_data(db_session, query)

    assert [device["name"] for device in payload["devices"]] == ["Core Router 1"]
    assert payload["type"] == "core"


def test_network_devices_legacy_probe_statuses_fail_closed():
    """The Zabbix probe source was retired: every core-device row degrades to
    a fixed not-working result (native reachability lives on the
    poll columns / operational status)."""
    statuses = core_devices_inventory_service._build_legacy_probe_statuses(
        [
            {"id": "device-a"},
            {"id": "device-b"},
            {"id": ""},
        ]
    )

    assert set(statuses) == {"device-a", "device-b"}
    for status in statuses.values():
        assert status["ping_label"] == "Not working"
        assert status["ping_state"] == "fail"
        assert status["snmp_label"] == "Not working"
        assert status["snmp_state"] == "fail"
        assert status["ping_reason"] == "Legacy probe source is not configured"


def test_consolidated_nas_inventory_inherits_linked_monitoring_status(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.device_operational_status.warmer_is_stale",
        lambda _now=None: False,
    )
    monitoring_device = NetworkDevice(
        name="SPDC",
        hostname="spdc",
        mgmt_ip="160.119.127.83",
        status=DeviceStatus.online,
        role=DeviceRole.access,
        live_status="up",
        live_status_at=datetime.now(UTC),
        zabbix_hostid="10736",
    )
    db_session.add(monitoring_device)
    db_session.flush()
    db_session.add(
        NasDevice(
            name="SPDC Access",
            code="spdc-access",
            vendor=NasVendor.mikrotik,
            management_ip="160.119.127.83",
            status=NasDeviceStatus.active,
            network_device_id=monitoring_device.id,
            zabbix_host_id="10696",
            is_active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(
        tab="core", db=db_session, search="spdc"
    )

    included = next(
        device for device in payload["core_devices"] if device.name == "SPDC Access"
    )
    assert included.live_status == "up"
    assert included.zabbix_hostid == "10736"
    assert included.operational.status == "working"


def test_consolidated_nas_inventory_uses_direct_router_status_without_link(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.device_operational_status.warmer_is_stale",
        lambda _now=None: False,
    )
    nas = NasDevice(
        name="Direct NAS",
        code="direct-nas",
        vendor=NasVendor.mikrotik,
        management_ip="192.0.2.231",
        status=NasDeviceStatus.active,
        is_active=True,
    )
    db_session.add(nas)
    db_session.flush()
    db_session.add(
        Router(
            name="Direct NAS Router",
            hostname="direct-nas",
            management_ip="192.0.2.231",
            rest_api_username="admin",
            rest_api_password="enc:test",
            status=RouterStatus.online,
            nas_device_id=nas.id,
            is_active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.consolidated_page_data(
        tab="core", db=db_session, search="direct nas"
    )

    included = next(
        device for device in payload["core_devices"] if device.name == "Direct NAS"
    )
    assert included.zabbix_hostid is None
    assert included.live_status == "up"
    assert included.operational.status == "working"


def test_olts_list_page_data_includes_network_devices_ending_in_olt(
    db_session, monkeypatch
):
    from app.services import web_network_core_runtime as core_runtime_service
    from app.services.network import olt_device_crud

    # Bypass the active-OLT config-pack validator; same reason as the
    # consolidated-page test above.
    monkeypatch.setattr(
        olt_device_crud.OLTDevices,
        "_require_authorization_ready",
        staticmethod(lambda _db, _device: None),
    )

    def fail_live_refresh(*_args, **_kwargs):
        raise AssertionError("OLT list must not run live SNMP refreshes")

    monkeypatch.setattr(
        core_runtime_service, "refresh_stale_devices_health", fail_live_refresh
    )

    promoted = NetworkDevice(
        name="Metro OLT",
        hostname="metro-olt.local",
        mgmt_ip="192.0.2.220",
        vendor="ZTE",
        model="C320",
        role=DeviceRole.access,
        status=DeviceStatus.online,
        snmp_enabled=True,
        last_snmp_ok=True,
        is_active=True,
    )
    db_session.add(promoted)
    db_session.commit()

    payload = web_network_core_devices_service.olts_list_page_data(db_session)

    assert payload["stats"]["total"] == 1
    assert payload["olts"][0]["name"] == "Metro OLT"
    assert payload["olts"][0]["detail_url"].startswith("/admin/network/olts/")
    assert (
        db_session.scalars(
            select(OLTDevice).where(OLTDevice.mgmt_ip == "192.0.2.220")
        ).first()
        is not None
    )


def test_olts_list_page_data_uses_binary_operational_status(db_session):
    now = datetime.now(UTC)
    online = OLTDevice(
        name="OLT Online",
        mgmt_ip="192.0.2.221",
        last_ping_at=now,
        last_ping_ok=True,
        last_poll_status=PollStatus.success,
    )
    offline = OLTDevice(
        name="OLT Offline",
        mgmt_ip="192.0.2.222",
        last_ping_at=now,
        last_ping_ok=False,
    )
    pending = OLTDevice(name="OLT Pending", mgmt_ip="192.0.2.223")
    db_session.add_all([online, offline, pending])
    db_session.commit()

    payload = web_network_core_devices_service.olts_list_page_data(db_session)
    rows = {item["name"]: item for item in payload["olts"]}

    assert rows["OLT Online"]["operational_status"] == "working"
    assert rows["OLT Online"]["status_presentation"].tone.value == "positive"
    assert "runtime_retry_pending" not in rows["OLT Online"]
    assert rows["OLT Offline"]["operational_status"] == "not_working"
    assert rows["OLT Offline"]["runtime_health_state"] == "attention"
    assert rows["OLT Offline"]["status_presentation"].tone.value == "negative"
    assert rows["OLT Pending"]["operational_status"] == "not_working"
    assert rows["OLT Pending"]["runtime_health_state"] == "not_working"
    assert "runtime_retry_pending" not in rows["OLT Pending"]
    assert rows["OLT Pending"]["status_presentation"].tone.value == "negative"
    assert payload["stats"]["working"] == 1
    assert payload["stats"]["not_working"] == 2
    assert payload["stats"]["attention"] == 1

    not_working_payload = web_network_core_devices_service.olts_list_page_data(
        db_session, status="not_working"
    )
    assert {item["name"] for item in not_working_payload["olts"]} == {
        "OLT Offline",
        "OLT Pending",
    }

    working_payload = web_network_core_devices_service.olts_list_page_data(
        db_session, status="working"
    )
    assert [item["name"] for item in working_payload["olts"]] == ["OLT Online"]


def test_olts_list_page_data_paginates_and_clamps_out_of_range_page(db_session):
    db_session.add_all(
        [
            OLTDevice(
                name=f"OLT Page {index:02d}",
                mgmt_ip=f"198.51.100.{index + 1}",
                is_active=True,
            )
            for index in range(11)
        ]
    )
    db_session.commit()

    payload = web_network_core_devices_service.olts_list_page_data(
        db_session,
        page=999,
        per_page=10,
    )

    assert payload["pagination"] == {
        "page": 2,
        "per_page": 10,
        "total": 11,
        "total_pages": 2,
        "has_prev": True,
        "has_next": False,
    }
    assert len(payload["olts"]) == 1


def test_onts_list_defaults_to_all_authorization_states_and_clamps_page(db_session):
    onts = [
        OntUnit(
            serial_number=f"ONT-PAGE-{index:02d}",
            is_active=True,
            authorization_status=(
                OntAuthorizationStatus.authorized
                if index == 0
                else OntAuthorizationStatus.pending
            ),
        )
        for index in range(11)
    ]
    db_session.add_all(onts)
    db_session.commit()

    payload = core_devices_views.onts_list_page_data(
        db_session,
        page=999,
        per_page=10,
    )

    assert payload["filters"]["authorization"] == "all"
    assert payload["pagination"]["page"] == 2
    assert payload["pagination"]["total"] == 11
    assert len(payload["onts"]) == 1


def test_onts_diagnostics_view_runs_one_inventory_query(db_session, monkeypatch):
    db_session.add(
        OntUnit(
            serial_number="ONT-DIAGNOSTICS-ONE-QUERY",
            is_active=True,
            authorization_status=OntAuthorizationStatus.authorized,
        )
    )
    db_session.commit()

    original = network_service.ont_units.list_advanced
    calls = 0

    def counted_list_advanced(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        network_service.ont_units, "list_advanced", counted_list_advanced
    )

    payload = core_devices_views.onts_list_page_data(
        db_session,
        view="diagnostics",
        per_page=50,
    )

    assert calls == 1
    assert payload["onts"] == ()
    assert len(payload["diagnostics_onts"]) == 1


def test_olt_detail_page_data_pon_rows_link_to_specific_pon(db_session):
    olt = OLTDevice(name="OLT-PON-LINK", mgmt_ip="198.51.100.120")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(olt_id=olt.id, name="0/1/0", is_active=True)
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-PON-LINK-001", is_active=True, olt_device_id=olt.id
    )
    db_session.add(ont)
    db_session.flush()

    assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
    db_session.add(assignment)
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    rows = payload["pon_port_table_rows"]
    assert rows
    assert (
        rows[0]["action_url"]
        == f"/admin/network/onts?olt_id={olt.id}&pon_port_id={pon.id}"
    )


def test_olt_detail_page_data_pon_row_name_uses_short_port_number(db_session):
    olt = OLTDevice(name="OLT-PON-LABEL", mgmt_ip="198.51.100.124")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(
        olt_id=olt.id,
        name="Huawei-MA5600-V800R013-GPON_UNI 0/2/0",
        port_number=0,
        is_active=True,
    )
    db_session.add(pon)
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    rows = payload["pon_port_table_rows"]
    assert rows
    assert rows[0]["name"] == "0"


def test_olt_detail_page_data_ont_pon_display_prefers_port_description(db_session):
    olt = OLTDevice(name="OLT-PON-DESC", mgmt_ip="198.51.100.123")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(
        olt_id=olt.id, name="0/2/0", notes="Main Street Splitter A", is_active=True
    )
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-PON-DESC-001", is_active=True, olt_device_id=olt.id
    )
    db_session.add(ont)
    db_session.flush()

    db_session.add(OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True))
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    display = payload["pon_port_display_by_ont_id"].get(str(ont.id))
    assert display == "0 - Main Street Splitter A"


def test_olt_detail_page_data_ont_pon_display_uses_port_index_and_description(
    db_session,
):
    olt = OLTDevice(name="OLT-PON-DESC-IDX", mgmt_ip="198.51.100.126")
    db_session.add(olt)
    db_session.flush()

    db_session.add_all(
        [
            PonPort(
                olt_id=olt.id,
                name="Huawei-MA5600-V800R013-GPON_UNI-A",
                notes="Progressive Governors Forum",
                is_active=True,
            ),
            PonPort(
                olt_id=olt.id,
                name="Huawei-MA5600-V800R013-GPON_UNI-B",
                notes="AYA 2",
                is_active=True,
            ),
        ]
    )
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-PON-DESC-IDX-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="1",
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    display = payload["pon_port_display_by_ont_id"].get(str(ont.id))
    assert display == "1 - AYA 2"


def test_olt_detail_page_data_maps_ont_mac_from_assigned_subscriber_cpe(
    db_session, subscriber
):
    olt = OLTDevice(name="OLT-ONT-MAC", mgmt_ip="198.51.100.127")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(olt_id=olt.id, name="0/2/0", is_active=True)
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(serial_number="ONT-MAC-001", is_active=True, olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()

    network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="CPE-MAC-001",
            mac_address="AA:BB:CC:DD:EE:FF",
        ),
    )

    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    assert payload["ont_mac_by_ont_id"].get(str(ont.id)) == "AA:BB:CC:DD:EE:FF"


def test_olt_detail_page_data_maps_ont_mac_directly_from_ont_unit(db_session):
    olt = OLTDevice(name="OLT-ONT-MAC-DIRECT", mgmt_ip="198.51.100.128")
    db_session.add(olt)
    db_session.flush()

    ont = OntUnit(
        serial_number="ONT-MAC-DIRECT-001",
        is_active=True,
        olt_device_id=olt.id,
        mac_address="11:22:33:44:55:66",
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    assert payload["ont_mac_by_ont_id"].get(str(ont.id)) == "11:22:33:44:55:66"


def test_ont_tr069_persists_observed_runtime_fields(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-TR069-RUNTIME", mgmt_ip="198.51.100.129")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-TR069-RUNTIME-001", is_active=True, olt_device_id=olt.id
    )
    db_session.add(ont)
    db_session.commit()

    class _FakeClient:
        @staticmethod
        def get_device(_device_id):
            return {}

        @staticmethod
        def parse_device_id(_device_id):
            return ("HWTC7D", "EG8145V5", "HWTC7D4806C3")

        @staticmethod
        def extract_parameter_value(_device, path):
            # GenieACS virtual parameters are the canonical TR-098/TR-181
            # normalization surface (see tr069_paths.VIRTUAL_PARAM_GROUPS).
            # The summary builder pulls each field exclusively from
            # ``VirtualParameters.<name>`` — native paths are not consulted
            # by ``_extract_first`` here.
            values = {
                "VirtualParameters.MAC_Address": "AA:AA:AA:AA:AA:AA",
                "VirtualParameters.WAN_IP": "100.64.10.5",
                "VirtualParameters.PPPoE_Username": "subscriber001",
                "VirtualParameters.WAN_Status": "Up",
                "VirtualParameters.WAN_Connection_Type": "PPPoE",
                "VirtualParameters.WiFi_Clients": "5",
                "VirtualParameters.Connected_Hosts": "12",
                "VirtualParameters.DHCP_Enabled": "true",
            }
            return values.get(path)

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(
        ont_tr069_module,
        "resolve_genieacs",
        lambda _db, _ont: (_FakeClient(), "fake-device-id"),
    )

    summary = OntTR069.get_device_summary(
        db_session,
        str(ont.id),
        persist_observed_runtime=True,
    )
    assert summary.available is True
    refreshed = db_session.get(OntUnit, ont.id)
    assert refreshed is not None
    assert refreshed.mac_address == "AA:AA:AA:AA:AA:AA"
    assert refreshed.observed_wan_ip == "100.64.10.5"
    assert refreshed.pppoe_username is None
    assert refreshed.observed_pppoe_status == "Up"
    assert refreshed.observed_wifi_clients == 5
    assert refreshed.observed_lan_hosts == 12
    assert refreshed.observed_lan_mode == "router"
    assert refreshed.tr069_last_snapshot_at is not None
    assert isinstance(refreshed.tr069_last_snapshot, dict)
    assert refreshed.tr069_last_snapshot["wan"]["WAN IP"] == "100.64.10.5"
    assert refreshed.tr069_last_snapshot["raw_device"] == {}


def test_ont_tr069_keeps_radius_framed_ip_as_observed_wan(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-TR069-RADIUS-RUNTIME", mgmt_ip="198.51.100.131")
    db_session.add(olt)
    db_session.flush()
    subscriber = Subscriber(
        first_name="Radius",
        last_name="Runtime",
        email="radius-runtime@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-TR069-RADIUS-RUNTIME-001",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            subscriber_id=subscriber.id,
            active=True,
            pppoe_username="100016329",
            assigned_at=datetime.now(UTC),
        )
    )
    db_session.add(
        RadiusActiveSession(
            username="100016329",
            acct_session_id="sess-radius-runtime",
            subscriber_id=subscriber.id,
            framed_ip_address="172.16.145.51",
            session_start=datetime.now(UTC) - timedelta(minutes=5),
            last_update=datetime.now(UTC),
        )
    )
    db_session.commit()

    class _FakeClient:
        @staticmethod
        def get_device(_device_id):
            return {}

        @staticmethod
        def parse_device_id(_device_id):
            return ("HWTC7D", "EG8145V5", "HWTC7D4806C3")

        @staticmethod
        def extract_parameter_value(_device, path):
            values = {
                "VirtualParameters.WAN_IP": "172.16.204.34",
                "VirtualParameters.WAN_Status": "Up",
            }
            return values.get(path)

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(
        ont_tr069_module,
        "resolve_genieacs",
        lambda _db, _ont: (_FakeClient(), "fake-device-id"),
    )

    summary = OntTR069.get_device_summary(
        db_session,
        str(ont.id),
        persist_observed_runtime=True,
    )

    assert summary.available is True
    refreshed = db_session.get(OntUnit, ont.id)
    assert refreshed is not None
    assert refreshed.observed_wan_ip == "172.16.145.51"
    assert refreshed.observed_pppoe_status == "Connected"
    assert refreshed.tr069_last_snapshot["wan"]["WAN IP"] == "172.16.204.34"


def test_ont_tr069_prefers_device_mac_over_first_lan_port(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-TR069-MAC", mgmt_ip="198.51.100.130")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-TR069-MAC-001", is_active=True, olt_device_id=olt.id
    )
    db_session.add(ont)
    db_session.commit()

    class _FakeClient:
        @staticmethod
        def get_device(_device_id):
            return {
                "Device": {
                    "Ethernet": {
                        "Interface": {
                            "1": {"MACAddress": {"_value": "E0:37:68:80:50:0E"}},
                            "2": {"MACAddress": {"_value": "E0:37:68:80:50:11"}},
                        }
                    }
                }
            }

        @staticmethod
        def extract_parameter_value(_device, path):
            values = {
                "Device.Ethernet.Interface.1.MACAddress": "E0:37:68:80:50:0E",
                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.MACAddress": "e0-37-68-80-50-11",
            }
            return values.get(path)

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(
        ont_tr069_module,
        "resolve_genieacs",
        lambda _db, _ont: (_FakeClient(), "fake-device-id"),
    )

    OntTR069.get_device_summary(
        db_session,
        str(ont.id),
        persist_observed_runtime=True,
    )

    refreshed = db_session.get(OntUnit, ont.id)
    assert refreshed is not None
    assert refreshed.mac_address == "E0:37:68:80:50:11"


def test_ont_tr069_uses_cached_snapshot_when_live_fetch_fails(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-TR069-CACHE", mgmt_ip="198.51.100.133")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-TR069-CACHE-001",
        is_active=True,
        olt_device_id=olt.id,
        tr069_last_snapshot={
            "system": {"Model": "EG8145V5", "Serial": "HWTC7D4806C3"},
            "wan": {"WAN IP": "172.16.1.10", "Status": "Connected"},
            "lan": {"DHCP Enabled": "true", "Connected Hosts": "4"},
            "wireless": {"SSID": "DotMac", "Connected Clients": "2"},
            "ethernet_ports": [{"index": 1, "Status": "Up"}],
            "lan_hosts": [{"HostName": "phone", "IPAddress": "192.168.1.10"}],
            "fetched_at": "2026-04-02T15:15:00+00:00",
            "raw_device": {
                "Device": {"DeviceInfo": {"ModelName": {"_value": "EG8145V5"}}}
            },
        },
    )
    db_session.add(ont)
    db_session.commit()

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(ont_tr069_module, "resolve_genieacs", lambda _db, _ont: None)

    summary = OntTR069.get_device_summary(
        db_session,
        str(ont.id),
        persist_observed_runtime=True,
    )

    assert summary.available is True
    assert summary.source == "cache"
    assert summary.wan["WAN IP"] == "172.16.1.10"
    assert summary.wireless["SSID"] == "DotMac"
    assert summary.ethernet_ports[0]["link_status"] == "Up"
    assert summary.ethernet_ports[0]["port"] == 1
    assert summary.lan_hosts[0]["host_name"] == "phone"
    assert summary.lan_hosts[0]["ip_address"] == "192.168.1.10"
    assert summary.raw_device == {
        "Device": {"DeviceInfo": {"ModelName": {"_value": "EG8145V5"}}}
    }
    assert "cached TR-069 snapshot" in (summary.error or "")


def test_ont_device_info_modal_does_not_allow_serial_number_change(
    db_session, subscription
):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="OLT-LOCKED-SERIAL-002"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/2"),
    )
    ont = OntUnit(
        serial_number="ONT-LOCKED-SERIAL-002",
        vendor="Huawei",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add(ont)
    db_session.flush()
    _assign_ont(db_session, ont=ont, pon=pon, subscription=subscription)

    result = ont_web_forms_service.update_device_info_from_form(
        db_session,
        str(ont.id),
        FormData(
            [
                ("serial_number", "ONT-TAMPERED-MODAL"),
                ("vendor", "ZTE"),
                ("model", "F670L"),
                ("firmware_version", "V9"),
            ]
        ),
    )

    assert result.error is None
    db_session.refresh(ont)
    assert ont.serial_number == "ONT-LOCKED-SERIAL-002"
    assert ont.vendor == "Huawei"
    assert ont.model is None


def test_ont_tr069_summary_includes_recent_informs(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-TR069-INFORMS", mgmt_ip="198.51.100.134")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-TR069-INFORM-001",
        is_active=True,
        olt_device_id=olt.id,
        tr069_last_snapshot={"system": {}, "wan": {}, "lan": {}, "wireless": {}},
    )
    db_session.add(ont)
    db_session.flush()
    server = Tr069AcsServer(
        name="Inform ACS",
        cwmp_url="https://acs.test/cwmp",
        cwmp_username="u",
        cwmp_password="p",
        connection_request_username="cu",
        connection_request_password="cp",
        base_url="https://acs.test",
        is_active=True,
    )
    db_session.add(server)
    db_session.flush()
    device = Tr069CpeDevice(
        acs_server_id=server.id,
        ont_unit_id=ont.id,
        serial_number=ont.serial_number,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()
    session = Tr069Session(
        device_id=device.id,
        event_type=Tr069Event.periodic,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        inform_payload={"event": "periodic", "parameter_count": 3},
    )
    db_session.add(session)
    db_session.commit()

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(ont_tr069_module, "resolve_genieacs", lambda _db, _ont: None)

    summary = OntTR069.get_device_summary(db_session, str(ont.id))

    assert summary.available is True
    assert len(summary.recent_informs) == 1
    assert summary.recent_informs[0].inform_payload["parameter_count"] == 3


def test_ont_tr069_summary_includes_cached_config_parameters(db_session, monkeypatch):
    ont = OntUnit(serial_number="ONT-TR069-PARAM-001", is_active=True)
    db_session.add(ont)
    db_session.flush()
    server = Tr069AcsServer(
        name="Parameter ACS",
        cwmp_url="https://acs.test/cwmp",
        cwmp_username="u",
        cwmp_password="p",
        connection_request_username="cu",
        connection_request_password="cp",
        base_url="https://acs.test",
        is_active=True,
    )
    db_session.add(server)
    db_session.flush()
    device = Tr069CpeDevice(
        acs_server_id=server.id,
        ont_unit_id=ont.id,
        serial_number=ont.serial_number,
        is_active=True,
    )
    db_session.add(device)
    db_session.flush()
    db_session.add_all(
        [
            Tr069Parameter(
                device_id=device.id,
                name="Device.DeviceInfo.SoftwareVersion",
                value="V1.2.3",
                updated_at=datetime.now(UTC),
            ),
            Tr069Parameter(
                device_id=device.id,
                name="InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
                value="subscriber001",
                updated_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(ont_tr069_module, "resolve_genieacs", lambda _db, _ont: None)

    summary = OntTR069.get_device_summary(db_session, str(ont.id))

    assert summary.cached_parameters["System"][0]["value"] == "V1.2.3"
    assert summary.cached_parameters["WAN / ACS"][0]["value"] == "subscriber001"


def test_ont_tr069_inform_wifi_data_renders_in_partial(db_session, monkeypatch):
    ont = OntUnit(serial_number="ONT-TR069-WIFI-INFORM-001", is_active=True)
    db_session.add(ont)
    db_session.flush()
    server = Tr069AcsServer(
        name="WiFi Inform ACS",
        cwmp_url="https://acs.test/cwmp",
        cwmp_username="u",
        cwmp_password="p",
        connection_request_username="cu",
        connection_request_password="cp",
        base_url="https://acs.test",
        is_active=True,
    )
    db_session.add(server)
    db_session.flush()
    device = Tr069CpeDevice(
        acs_server_id=server.id,
        ont_unit_id=ont.id,
        serial_number=ont.serial_number,
        genieacs_device_id="00259E-EG8145V5-ONT-TR069-WIFI-INFORM-001",
        is_active=True,
    )
    db_session.add(device)
    db_session.commit()

    result = tr069_service.receive_inform(
        db_session,
        serial_number=ont.serial_number,
        device_id_raw=device.genieacs_device_id,
        event=[{"EventCode": "2 PERIODIC"}],
        raw_payload={
            "parameters": {
                "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID": "DotMac-Test",
                "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.TotalAssociations": "7",
                "Device.WiFi.AccessPoint.1.AssociatedDeviceNumberOfEntries": "7",
                "InternetGatewayDevice.LANDevice.1.Hosts.HostNumberOfEntries": "11",
            }
        },
    )
    assert result["parameters"] == 4

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(ont_tr069_module, "resolve_genieacs", lambda _db, _ont: None)

    summary = OntTR069.get_device_summary(db_session, str(ont.id))
    assert summary.available is True
    assert summary.recent_informs
    assert summary.wireless["SSID"] == "DotMac-Test"
    assert summary.wireless["Connected Clients"] == "7"
    assert summary.lan["Connected Hosts"] == "11"
    assert any(
        param["name"] == "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID"
        and param["value"] == "DotMac-Test"
        for param in summary.cached_parameters["Wireless"]
    )

    templates = Jinja2Templates(directory="templates")
    html = templates.env.get_template("admin/network/onts/_tr069_partial.html").render(
        {"tr069": summary}
    )

    assert "Recent ACS Informs" in html
    assert "Cached Config Parameters" in html
    assert "WiFi Clients" in html
    assert "DotMac-Test" in html
    assert "Connected Clients" in html
    assert "TotalAssociations" in html
    assert "AssociatedDeviceNumberOfEntries" in html
    assert ">7<" in html
    assert "HostNumberOfEntries" in html
    assert ">11<" in html


def test_ont_tr069_live_acs_read_wifi_data_renders_in_partial(db_session, monkeypatch):
    ont = OntUnit(serial_number="ONT-TR069-LIVE-WIFI-001", is_active=True)
    db_session.add(ont)
    db_session.commit()

    class _FakeClient:
        @staticmethod
        def get_device(_device_id):
            return {
                "InternetGatewayDevice": {
                    "LANDevice": {
                        "1": {
                            "WLANConfiguration": {
                                "1": {
                                    "SSID": {"_value": "DotMac-Live"},
                                    "TotalAssociations": {"_value": "6"},
                                }
                            },
                            "Hosts": {
                                "HostNumberOfEntries": {"_value": "9"},
                                "Host": {
                                    "1": {
                                        "HostName": {"_value": "phone"},
                                        "IPAddress": {"_value": "192.168.1.20"},
                                        "MACAddress": {"_value": "AA:BB:CC:DD:EE:01"},
                                        "InterfaceType": {"_value": "802.11"},
                                        "Active": {"_value": "1"},
                                    }
                                },
                            },
                        }
                    }
                }
            }

        @staticmethod
        def extract_parameter_value(_device, path):
            # See test_ont_tr069_persists_observed_runtime_fields above:
            # the summary builder reads VirtualParameters, not the native
            # TR-098 paths. Synthesise the same values from the device
            # body so the wireless/lan summary sections populate.
            values = {
                "VirtualParameters.WiFi_SSID": "DotMac-Live",
                "VirtualParameters.WiFi_Clients": "6",
                "VirtualParameters.Connected_Hosts": "9",
            }
            return values.get(path)

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    monkeypatch.setattr(
        ont_tr069_module,
        "resolve_genieacs",
        lambda _db, _ont: (_FakeClient(), "live-device-id"),
    )

    summary = OntTR069.get_device_summary(db_session, str(ont.id))

    assert summary.available is True
    assert summary.source == "live"
    assert summary.wireless["SSID"] == "DotMac-Live"
    assert summary.wireless["Connected Clients"] == "6"
    assert summary.lan["Connected Hosts"] == "9"
    assert summary.lan_hosts[0]["HostName"] == "phone"

    templates = Jinja2Templates(directory="templates")
    html = templates.env.get_template("admin/network/onts/_tr069_partial.html").render(
        {"tr069": summary}
    )

    assert "Live ACS Data" in html
    assert "DotMac-Live" in html
    assert "WiFi Clients" in html
    assert ">6<" in html
    assert "LAN Hosts" in html
    assert ">1<" in html
    assert "phone" in html
    assert "Raw ACS Device Data" in html


def test_build_cpe_identity_context_prefers_active_ont_mac(db_session, subscriber):
    olt = OLTDevice(name="OLT-CPE-IDENTITY-MAC", mgmt_ip="198.51.100.131")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(olt_id=olt.id, name="0/1/0", is_active=True)
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(
        serial_number="HWTC7D4806C3",
        is_active=True,
        olt_device_id=olt.id,
        mac_address="E0:37:68:80:50:11",
    )
    db_session.add(ont)
    db_session.flush()

    cpe = network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="HWTC7D4806C3",
            mac_address="E0:37:68:80:50:0E",
        ),
    )

    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    db_session.commit()

    identity = web_network_cpes_service.build_cpe_identity_context(db_session, cpe)
    assert identity["mac_address"] == "E0:37:68:80:50:11"


def test_cpe_tr069_summary_prefers_active_ont_mac_over_acs_value(
    db_session, subscriber, monkeypatch
):
    olt = OLTDevice(name="OLT-CPE-TR069-MAC", mgmt_ip="198.51.100.132")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(olt_id=olt.id, name="0/1/0", is_active=True)
    db_session.add(pon)
    db_session.flush()

    ont = OntUnit(
        serial_number="HWTC7D4806C3",
        is_active=True,
        olt_device_id=olt.id,
        mac_address="E0:37:68:80:50:11",
    )
    db_session.add(ont)
    db_session.flush()

    cpe = network_service.cpe_devices.create(
        db_session,
        CPEDeviceCreate(
            account_id=subscriber.id,
            serial_number="HWTC7D4806C3",
            mac_address="E0:37:68:80:50:0E",
        ),
    )

    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    db_session.flush()

    acs_server = Tr069AcsServer(
        name="ACS",
        base_url="http://acs.local",
        is_active=True,
    )
    db_session.add(acs_server)
    db_session.flush()

    db_session.add(
        Tr069CpeDevice(
            acs_server_id=acs_server.id,
            cpe_device_id=cpe.id,
            serial_number="HWTC7D4806C3",
            product_class="EG8145V5",
            is_active=True,
        )
    )
    db_session.commit()

    class _FakeClient:
        @staticmethod
        def get_device(_device_id):
            return {}

        @staticmethod
        def parse_device_id(_device_id):
            return ("HWTC7D", "EG8145V5", "HWTC7D4806C3")

        @staticmethod
        def extract_parameter_value(_device, path):
            values = {
                "Device.DeviceInfo.Manufacturer": "Huawei",
                "Device.DeviceInfo.ModelName": "EG8145V5",
                "Device.DeviceInfo.SerialNumber": "HWTC7D4806C3",
                "Device.Ethernet.Interface.1.MACAddress": "E0:37:68:80:50:0E",
            }
            return values.get(path)

    cpe_tr069_module = importlib.import_module("app.services.network.cpe_tr069")
    monkeypatch.setattr(
        cpe_tr069_module,
        "resolve_genieacs_for_cpe",
        lambda _db, _cpe: (_FakeClient(), "fake-device-id"),
    )

    summary = CpeTR069.get_device_summary(db_session, str(cpe.id))

    refreshed = db_session.get(CPEDevice, cpe.id)
    assert summary.system["MAC Address"] == "E0:37:68:80:50:11"
    assert refreshed is not None
    assert refreshed.mac_address == "E0:37:68:80:50:11"


def test_onts_list_page_data_filters_by_pon_port(db_session):
    olt = OLTDevice(name="OLT-PON-FILTER", mgmt_ip="198.51.100.121")
    db_session.add(olt)
    db_session.flush()

    pon_a = PonPort(olt_id=olt.id, name="0/1/0", is_active=True)
    pon_b = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add_all([pon_a, pon_b])
    db_session.flush()

    ont_a = OntUnit(serial_number="ONT-PON-A", is_active=True, olt_device_id=olt.id)
    ont_b = OntUnit(serial_number="ONT-PON-B", is_active=True, olt_device_id=olt.id)
    db_session.add_all([ont_a, ont_b])
    db_session.flush()

    db_session.add_all(
        [
            OntAssignment(ont_unit_id=ont_a.id, pon_port_id=pon_a.id, active=True),
            OntAssignment(ont_unit_id=ont_b.id, pon_port_id=pon_b.id, active=True),
        ]
    )
    db_session.commit()

    filtered = core_devices_views.onts_list_page_data(
        db_session,
        olt_id=str(olt.id),
        pon_port_id=str(pon_a.id),
        per_page=50,
    )
    serials = [ont.serial_number for ont in filtered["onts"]]
    assert serials == ["ONT-PON-A"]
    assert filtered["filters"]["pon_port_id"] == str(pon_a.id)


def test_onts_list_page_data_filters_by_pon_hint_for_snmp_rows(db_session):
    olt = OLTDevice(name="OLT-PON-HINT", mgmt_ip="198.51.100.122")
    db_session.add(olt)
    db_session.flush()

    ont_a = OntUnit(
        serial_number="ONT-HINT-A",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="7",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    ont_b = OntUnit(
        serial_number="ONT-HINT-B",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="8",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add_all([ont_a, ont_b])
    db_session.commit()

    filtered = core_devices_views.onts_list_page_data(
        db_session,
        olt_id=str(olt.id),
        pon_hint="0/2/7",
        per_page=50,
    )
    serials = [ont.serial_number for ont in filtered["onts"]]
    assert serials == ["ONT-HINT-A"]
    assert filtered["filters"]["pon_hint"] == "0/2/7"


def test_olt_detail_snmp_pon_rows_use_ont_fallback_stats(db_session):
    olt = OLTDevice(name="OLT-SNMP-PON", mgmt_ip="198.51.100.130", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()

    monitoring = NetworkDevice(
        name="OLT-SNMP-PON",
        mgmt_ip="198.51.100.130",
        vendor="Huawei",
        model="MA5800",
        role=DeviceRole.access,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(monitoring)
    db_session.flush()

    db_session.add(
        DeviceInterface(
            device_id=monitoring.id,
            name="Huawei-MA5800-V100R019-GPON_UNI 0/2/7",
            description="Zone 4",
            status=InterfaceStatus.up,
        )
    )

    ont_match = OntUnit(
        serial_number="ONT-SNMP-ROW-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="7",
        olt_rx_signal_dbm=-23.4,
    )
    ont_other = OntUnit(
        serial_number="ONT-SNMP-ROW-002",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="8",
        olt_rx_signal_dbm=-18.0,
    )
    db_session.add_all([ont_match, ont_other])
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    rows = payload["pon_port_table_rows"]
    assert rows
    row = rows[0]
    assert row["name"] == "7"
    assert row["onus"] == 1
    assert row["avg_signal_dbm"] == -23.4
    assert "pon_hint=0/2/7" in row["action_url"]


def test_olt_detail_snmp_pon_rows_match_onts_with_channel_board_port_format(db_session):
    olt = OLTDevice(name="OLT-SNMP-PON-CHAN", mgmt_ip="198.51.100.131", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()

    monitoring = NetworkDevice(
        name="OLT-SNMP-PON-CHAN",
        mgmt_ip="198.51.100.131",
        vendor="Huawei",
        model="MA5800",
        role=DeviceRole.access,
        status=DeviceStatus.online,
        is_active=True,
    )
    db_session.add(monitoring)
    db_session.flush()

    db_session.add(
        DeviceInterface(
            device_id=monitoring.id,
            name="Huawei-MA5800-V100R019-GPON_UNI 0/2/7",
            description="Area 8",
            status=InterfaceStatus.up,
        )
    )

    ont = OntUnit(
        serial_number="ONT-SNMP-CHAN-001",
        is_active=True,
        olt_device_id=olt.id,
        gpon_channel="gpon",
        board="2",
        port="7",
        olt_rx_signal_dbm=-24.0,
    )
    db_session.add(ont)
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    rows = payload["pon_port_table_rows"]
    assert rows
    assert rows[0]["onus"] == 1
    assert rows[0]["avg_signal_dbm"] == -24.0


def test_olt_detail_db_pon_rows_fallback_to_direct_ont_port_index(db_session):
    olt = OLTDevice(name="OLT-DB-PON-FALLBACK", mgmt_ip="198.51.100.132")
    db_session.add(olt)
    db_session.flush()

    pon = PonPort(
        olt_id=olt.id,
        name="Huawei-MA5600-V800R013-GPON_UNI 0/2/0",
        port_number=0,
        is_active=True,
    )
    db_session.add(pon)
    db_session.flush()

    db_session.add(
        OntUnit(
            serial_number="ONT-DB-FALLBACK-001",
            is_active=True,
            olt_device_id=olt.id,
            board="0/2",
            port="0",
            olt_rx_signal_dbm=-20.5,
        )
    )
    db_session.commit()

    payload = core_devices_views.olt_detail_page_data(db_session, str(olt.id))
    assert payload is not None
    rows = payload["pon_port_table_rows"]
    assert rows
    assert rows[0]["onus"] == 1
    assert rows[0]["avg_signal_dbm"] == -20.5


def test_queue_backup_failure_notification_queues_notification(db_session, monkeypatch):
    values = {
        "alert_notifications_enabled": True,
        "alert_notifications_default_recipient": "noc@example.com",
        "alert_notifications_default_channel": "email",
    }

    def _fake_resolve(_db, _domain, key):
        return values.get(key)

    monkeypatch.setattr(
        backup_alerts_service.settings_spec, "resolve_value", _fake_resolve
    )

    queued = backup_alerts_service.queue_backup_failure_notification(
        db_session,
        device_kind="olt",
        device_name="OLT Alpha",
        device_ip="198.51.100.1",
        error_message="timeout",
        run_type="scheduled",
    )
    assert queued is True
    db_session.commit()

    items = db_session.query(Notification).all()
    assert len(items) == 1
    assert items[0].channel == NotificationChannel.email
    assert items[0].status == NotificationStatus.queued
    assert items[0].recipient == "noc@example.com"
    assert "Backup Failure" in (items[0].subject or "")


def test_queue_backup_failure_notification_no_recipient_noop(db_session, monkeypatch):
    values = {
        "alert_notifications_enabled": True,
        "alert_notifications_default_recipient": "",
        "alert_notifications_default_channel": "email",
    }

    def _fake_resolve(_db, _domain, key):
        return values.get(key)

    monkeypatch.setattr(
        backup_alerts_service.settings_spec, "resolve_value", _fake_resolve
    )

    queued = backup_alerts_service.queue_backup_failure_notification(
        db_session,
        device_kind="nas",
        device_name="NAS Beta",
        device_ip="203.0.113.10",
        error_message="auth failed",
        run_type="manual",
    )
    assert queued is False
    assert db_session.query(Notification).count() == 0


def test_list_olt_backups_orders_desc_and_filters(db_session):
    olt = OLTDevice(name="OLT Backups", mgmt_ip="198.51.100.40")
    db_session.add(olt)
    db_session.flush()

    old = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="olt/old.txt",
        file_size_bytes=100,
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    recent = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path="olt/recent.txt",
        file_size_bytes=200,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add_all([old, recent])
    db_session.commit()

    rows = web_network_olts_service.list_olt_backups(db_session, olt_id=str(olt.id))
    assert [row.file_path for row in rows] == ["olt/recent.txt", "olt/old.txt"]

    filtered = web_network_olts_service.list_olt_backups(
        db_session,
        olt_id=str(olt.id),
        start_at=datetime.now(UTC) - timedelta(hours=12),
    )
    assert [row.file_path for row in filtered] == ["olt/recent.txt"]


def test_olt_backup_file_resolution_and_preview(
    db_session, monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Files", mgmt_ip="198.51.100.41")
    db_session.add(olt)
    db_session.flush()

    relative = "device1/snapshot.txt"
    full = tmp_path / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("line1\nline2\nline3\n")

    backup = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path=relative,
        file_size_bytes=full.stat().st_size,
    )
    db_session.add(backup)
    db_session.commit()

    resolved = web_network_olts_service.backup_file_path(backup)
    assert resolved == full.resolve()
    preview = web_network_olts_service.read_backup_preview(backup, limit_chars=8)
    assert preview == "line1\nli"
    preview_info = web_network_olts_service.read_backup_preview_info(
        backup, limit_chars=8
    )
    assert preview_info["preview"] == "line1\nli"
    assert preview_info["truncated"] is True

    bad = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="../escape.txt",
        file_size_bytes=10,
    )
    with pytest.raises(HTTPException):
        web_network_olts_service.backup_file_path(bad)


def test_compare_olt_backups_returns_diff(db_session, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Compare", mgmt_ip="198.51.100.42")
    db_session.add(olt)
    db_session.flush()

    path1 = tmp_path / "compare" / "b1.txt"
    path2 = tmp_path / "compare" / "b2.txt"
    path1.parent.mkdir(parents=True, exist_ok=True)
    path1.write_text("line-a\nline-b\n")
    path2.write_text("line-a\nline-c\n")

    b1 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="compare/b1.txt",
        file_size_bytes=path1.stat().st_size,
    )
    b2 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path="compare/b2.txt",
        file_size_bytes=path2.stat().st_size,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    _, _, diff = web_network_olts_service.compare_olt_backups(
        db_session,
        str(b1.id),
        str(b2.id),
    )
    assert isinstance(diff["unified_diff"], str)
    assert isinstance(diff["added_lines"], int)
    assert isinstance(diff["removed_lines"], int)
    assert diff["added_lines"] >= 1
    assert diff["removed_lines"] >= 1


def test_compare_olt_backups_rejects_cross_olt(db_session):
    olt1 = OLTDevice(name="OLT 1", mgmt_ip="198.51.100.43")
    olt2 = OLTDevice(name="OLT 2", mgmt_ip="198.51.100.44")
    db_session.add_all([olt1, olt2])
    db_session.flush()
    b1 = OltConfigBackup(
        olt_device_id=olt1.id,
        backup_type=OltConfigBackupType.auto,
        file_path="x1.txt",
        file_size_bytes=1,
    )
    b2 = OltConfigBackup(
        olt_device_id=olt2.id,
        backup_type=OltConfigBackupType.auto,
        file_path="x2.txt",
        file_size_bytes=1,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    with pytest.raises(HTTPException):
        web_network_olts_service.compare_olt_backups(
            db_session,
            str(b1.id),
            str(b2.id),
        )


def test_compare_olt_backups_rejects_same_backup(db_session):
    olt = OLTDevice(name="OLT Same Compare", mgmt_ip="198.51.100.45")
    db_session.add(olt)
    db_session.flush()
    backup = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="same.txt",
        file_size_bytes=1,
    )
    db_session.add(backup)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_network_olts_service.compare_olt_backups(
            db_session,
            str(backup.id),
            str(backup.id),
        )
    assert exc.value.status_code == 400
    assert "different backups" in str(exc.value.detail).lower()


def test_compare_olt_backups_rejects_large_web_diff(
    db_session, monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Large Compare", mgmt_ip="198.51.100.46")
    db_session.add(olt)
    db_session.flush()

    path1 = tmp_path / "large" / "b1.txt"
    path2 = tmp_path / "large" / "b2.txt"
    path1.parent.mkdir(parents=True, exist_ok=True)
    path1.write_text("a" * 101)
    path2.write_text("b\n")
    b1 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="large/b1.txt",
        file_size_bytes=101,
    )
    b2 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path="large/b2.txt",
        file_size_bytes=2,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_network_olts_service.compare_olt_backups(
            db_session,
            str(b1.id),
            str(b2.id),
            max_bytes=100,
        )
    assert exc.value.status_code == 413
    assert "too large" in str(exc.value.detail).lower()


def test_compare_olt_backups_uses_actual_file_size_for_limit(
    db_session, monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Stale Size Compare", mgmt_ip="198.51.100.47")
    db_session.add(olt)
    db_session.flush()

    path1 = tmp_path / "stale-size" / "b1.txt"
    path2 = tmp_path / "stale-size" / "b2.txt"
    path1.parent.mkdir(parents=True, exist_ok=True)
    path1.write_text("a" * 101)
    path2.write_text("b\n")
    b1 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.auto,
        file_path="stale-size/b1.txt",
        file_size_bytes=1,
    )
    b2 = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path="stale-size/b2.txt",
        file_size_bytes=1,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        web_network_olts_service.compare_olt_backups(
            db_session,
            str(b1.id),
            str(b2.id),
            max_bytes=100,
        )
    assert exc.value.status_code == 413


def test_test_olt_connection_and_test_backup(db_session, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Test Actions", mgmt_ip="198.51.100.50")
    db_session.add(olt)
    db_session.commit()

    monkeypatch.setattr(
        olt_operations_service,
        "fetch_running_config",
        lambda _olt: "sysName = OLT\nsysDescr = Device\n",
    )

    ok, message = web_network_olts_service.test_olt_connection(db_session, str(olt.id))
    assert ok is True
    assert "successful" in message.lower()

    backup, backup_message = web_network_olts_service.run_test_backup(
        db_session, str(olt.id)
    )
    assert backup is not None
    assert "successfully" in backup_message.lower()
    assert backup.backup_type == OltConfigBackupType.manual
    assert (tmp_path / backup.file_path).exists()


def test_test_olt_connection_handles_missing_ip(db_session):
    olt = OLTDevice(name="OLT Missing IP", mgmt_ip=None)
    db_session.add(olt)
    db_session.commit()

    ok, message = web_network_olts_service.test_olt_connection(db_session, str(olt.id))
    assert ok is False
    assert "management ip" in message.lower()


def test_restore_olt_backup_replays_saved_configuration(
    db_session, monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="OLT Restore", mgmt_ip="198.51.100.51")
    db_session.add(olt)
    db_session.commit()

    backup_dir = tmp_path / str(olt.id)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "restore.txt"
    backup_path.write_text(
        "# header\n"
        "sysname Restored-OLT\n"
        "display version\n"
        "interface gpon 0/1\n"
        "port 0 ont-auto-find enable\n"
        "return\n",
        encoding="utf-8",
    )
    backup = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path=str(backup_path.relative_to(tmp_path)),
        file_size_bytes=backup_path.stat().st_size,
    )
    db_session.add(backup)
    db_session.commit()

    captured: dict[str, object] = {}

    def fake_restore(_olt, config_text, *, persist=True):
        captured["olt_id"] = str(_olt.id)
        captured["config_text"] = config_text
        captured["persist"] = persist
        return (
            True,
            "Restored 3 configuration commands from backup and saved startup config",
        )

    monkeypatch.setattr(
        "app.services.network.olt_operations.olt_ssh_config_service.restore_config_from_backup",
        fake_restore,
    )
    monkeypatch.setattr(
        "app.services.network.olt_operations.log_olt_audit_event",
        lambda *args, **kwargs: captured.setdefault("audit_called", True),
    )

    ok, message = web_network_olts_service.restore_from_backup(
        db_session, str(olt.id), str(backup.id)
    )

    assert ok is True
    assert "restored" in message.lower()
    assert captured["olt_id"] == str(olt.id)
    assert "sysname Restored-OLT" in str(captured["config_text"])
    assert captured["persist"] is True
    assert captured["audit_called"] is True


def test_olt_backups_page_renders_restore_action(db_session, monkeypatch):
    from app.web.admin import network_olts_profiles as network_olts_profiles_web

    olt_id = uuid4()
    backup_id = uuid4()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/admin/network/olts/{olt_id}/backups",
            "headers": [],
        }
    )

    monkeypatch.setattr(
        network_olts_profiles_web,
        "_base_context",
        lambda _request, _db, active_page: {
            "request": _request,
            "active_page": active_page,
            "active_menu": "network",
            "current_user": SimpleNamespace(
                name="Admin User", email="admin@example.com", initials="AU"
            ),
            "sidebar_stats": SimpleNamespace(
                notifications_unread=0,
                sidebar_logo_url="",
                sidebar_logo_dark_url="",
                app_name="DotMac Subs",
                module_states={},
                feature_states={},
            ),
            "csrf_token": "test-token",
        },
    )
    monkeypatch.setattr(
        network_olts_profiles_web,
        "get_olt_or_none",
        lambda *_args: SimpleNamespace(id=olt_id, name="Restore OLT"),
    )
    monkeypatch.setattr(
        network_olts_profiles_web.olt_operations_service,
        "list_olt_backups",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                id=backup_id,
                backup_type=SimpleNamespace(value="manual"),
                file_path="restore.cfg",
                file_size_bytes=1024,
                created_at=datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
            )
        ],
    )

    response = network_olts_profiles_web.olt_backups_list(
        request=request, olt_id=str(olt_id), db=db_session
    )

    assert response.status_code == 200
    body = response.body.decode()
    assert f"/admin/network/olts/backups/{backup_id}#restore" in body
    assert "Restore review" in body
    assert "Manual Config Backup" in body


def test_olt_backup_detail_renders_restore_confirmation(
    db_session, monkeypatch, tmp_path: Path
):
    from app.web.admin import network_olts_profiles as network_olts_profiles_web

    monkeypatch.setenv("OLT_BACKUP_DIR", str(tmp_path))
    olt = OLTDevice(name="Restore Detail OLT", mgmt_ip="198.51.100.52")
    db_session.add(olt)
    db_session.flush()
    path = tmp_path / str(olt.id) / "detail.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("0123456789" * 20)
    backup = OltConfigBackup(
        olt_device_id=olt.id,
        backup_type=OltConfigBackupType.manual,
        file_path=str(path.relative_to(tmp_path)),
        file_size_bytes=path.stat().st_size,
        file_hash="abc123",
    )
    db_session.add(backup)
    db_session.commit()

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/admin/network/olts/backups/{backup.id}",
            "headers": [],
        }
    )
    monkeypatch.setattr(
        network_olts_profiles_web,
        "_base_context",
        lambda _request, _db, active_page: {
            "request": _request,
            "active_page": active_page,
            "active_menu": "network",
            "current_user": SimpleNamespace(
                name="Admin User", email="admin@example.com", initials="AU"
            ),
            "sidebar_stats": SimpleNamespace(
                notifications_unread=0,
                sidebar_logo_url="",
                sidebar_logo_dark_url="",
                app_name="DotMac Subs",
                module_states={},
                feature_states={},
            ),
            "csrf_token": "test-token",
        },
    )
    monkeypatch.setattr(
        network_olts_profiles_web.olt_operations_service,
        "read_backup_preview_info",
        lambda _backup: {
            "preview": "01234567",
            "truncated": True,
            "limit_chars": 8,
        },
    )

    response = network_olts_profiles_web.olt_backup_detail(
        request=request,
        backup_id=str(backup.id),
        db=db_session,
    )

    assert response.status_code == 200
    body = response.body.decode()
    assert "confirm_olt_name" in body
    assert "Type Restore Detail OLT to confirm" in body
    assert "abc123" in body
    assert "Preview is truncated at 8 characters" in body


def test_olt_backup_restore_route_requires_exact_olt_name(db_session, monkeypatch):
    from app.web.admin import network_olts_profiles as network_olts_profiles_web

    olt_id = uuid4()
    backup_id = uuid4()
    olt = SimpleNamespace(id=olt_id, name="Restore OLT")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        network_olts_profiles_web,
        "get_olt_or_none",
        lambda *_args: olt,
    )
    monkeypatch.setattr(
        network_olts_profiles_web.olt_operations_service,
        "restore_from_backup",
        lambda _db, oid, bid: calls.append((oid, bid)) or (True, "restored"),
    )

    mismatch = network_olts_profiles_web.olt_backup_restore(
        olt_id=str(olt_id),
        backup_id=str(backup_id),
        confirm_olt_name="wrong",
        db=db_session,
    )
    assert mismatch.status_code == 303
    assert calls == []
    assert "Type+the+exact+OLT+name" in mismatch.headers["location"]

    matched = network_olts_profiles_web.olt_backup_restore(
        olt_id=str(olt_id),
        backup_id=str(backup_id),
        confirm_olt_name="Restore OLT",
        db=db_session,
    )
    assert matched.status_code == 303
    assert calls == [(str(olt_id), str(backup_id))]
    assert "test_status=success" in matched.headers["location"]


def test_update_olt_keeps_existing_ssh_password_when_blank(db_session):
    olt = OLTDevice(name="Keep Secret OLT", ssh_password="existing-secret")
    db_session.add(olt)
    db_session.commit()

    # ``is_active=False`` here so the OLT validation that requires an
    # authorization-ready config pack doesn't fire. This test exercises the
    # SSH-password retention path, not the active-OLT contract.
    updated, error = web_network_olts_service.update_olt(
        db_session,
        str(olt.id),
        {
            "name": "Keep Secret OLT",
            "hostname": None,
            "mgmt_ip": None,
            "vendor": None,
            "model": None,
            "serial_number": None,
            "ssh_username": "netops",
            "ssh_password": None,
            "ssh_port": 22,
            "notes": None,
            "is_active": False,
        },
    )

    assert error is None
    assert updated is not None
    db_session.refresh(olt)
    assert olt.ssh_username == "netops"
    assert is_encrypted(olt.ssh_password) is True


def test_create_payload_encrypts_olt_ssh_password(monkeypatch):
    monkeypatch.setattr(
        olt_web_forms_service,
        "encrypt_credential",
        lambda value: f"enc-test:{value}" if value else value,
    )
    payload = web_network_olts_service.create_payload(
        {
            "name": "Metro OLT",
            "hostname": None,
            "mgmt_ip": None,
            "vendor": "Huawei",
            "model": "MA5800",
            "serial_number": None,
            "ssh_username": "netops",
            "ssh_password": "secret",
            "ssh_port": 22,
            "netconf_enabled": False,
            "netconf_port": None,
            "notes": None,
            "is_active": True,
        }
    )
    assert payload.ssh_password == "enc-test:secret"


def test_olt_model_policy_resolution():
    olt = OLTDevice(name="OLT One", vendor="Huawei", model="MA5800")
    policy = olt_ssh_service.resolve_policy(olt)
    assert policy.key == "huawei_ma5800"
    assert "aes256-ctr" in policy.ciphers


def test_olt_model_policy_resolution_rejects_unknown():
    olt = OLTDevice(name="OLT Unknown", vendor="Huawei", model="EA5801")
    with pytest.raises(ValueError):
        olt_ssh_service.resolve_policy(olt)


def test_test_olt_ssh_connection_service_wrapper(db_session, monkeypatch):
    olt = OLTDevice(
        name="OLT SSH",
        mgmt_ip="198.51.100.51",
        vendor="Huawei",
        model="MA5608T",
        ssh_username="netops",
        ssh_password="plain:secret",
    )
    db_session.add(olt)
    db_session.commit()

    monkeypatch.setattr(
        olt_ssh_service,
        "test_connection",
        lambda _olt: (True, "SSH connection test successful", "huawei_ma5608t"),
    )
    ok, message, policy_key = web_network_olts_service.test_olt_ssh_connection(
        db_session, str(olt.id)
    )
    assert ok is True
    assert "huawei_ma5608t" in message
    assert policy_key == "huawei_ma5608t"


def test_ip_pool_create_rejects_overlapping_cidr(db_session):
    existing, err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-A",
            "ip_version": "ipv4",
            "cidr": "10.0.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert err is None
    assert existing is not None

    created, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-B",
            "ip_version": "ipv4",
            "cidr": "10.0.0.128/25",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert created is None
    assert error is not None
    assert "overlaps existing pool" in error.lower()


def test_ip_pool_update_rejects_overlapping_cidr(db_session):
    first, err1 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-1",
            "ip_version": "ipv4",
            "cidr": "172.16.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    second, err2 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Pool-2",
            "ip_version": "ipv4",
            "cidr": "172.16.1.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert err1 is None and err2 is None
    assert first is not None and second is not None

    updated, changes, error = web_network_ip_service.update_ip_pool(
        db_session,
        pool_id=str(second.id),
        values={
            "name": "Pool-2",
            "ip_version": "ipv4",
            "cidr": "172.16.0.128/25",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert updated is None
    assert changes is None
    assert error is not None
    assert "overlaps existing pool" in error.lower()


def test_import_ip_pools_csv_creates_valid_rows_and_reports_errors(db_session):
    csv_text = (
        "name,cidr,ip_version,gateway,is_active\n"
        "Import A,10.20.0.0/24,ipv4,10.20.0.1,true\n"
        "Import Bad,,ipv4,10.20.1.1,true\n"
    )
    result = web_network_ip_service.import_ip_pools_csv(
        db_session,
        csv_text=csv_text,
        default_ip_version="ipv4",
    )
    assert result["total_rows"] == 2
    assert len(result["created"]) == 1
    assert len(result["errors"]) == 1
    assert "cidr block is required" in str(result["errors"][0]["error"]).lower()


def test_import_ip_pools_csv_reports_overlap_errors(db_session):
    created, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Existing",
            "ip_version": "ipv4",
            "cidr": "10.30.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert created is not None
    assert error is None

    csv_text = "name,cidr,ip_version\nOverlap,10.30.0.128/25,ipv4\n"
    result = web_network_ip_service.import_ip_pools_csv(
        db_session,
        csv_text=csv_text,
        default_ip_version="ipv4",
    )
    assert len(result["created"]) == 0
    assert len(result["errors"]) == 1
    assert "overlaps existing pool" in str(result["errors"][0]["error"]).lower()


def test_create_ip_block_rejects_overlap_and_outside_pool(db_session):
    pool, pool_error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Block Pool",
            "ip_version": "ipv4",
            "cidr": "10.40.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert pool is not None
    assert pool_error is None

    block_ok, err_ok = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.40.0.0/25",
            "notes": None,
            "is_active": True,
        },
    )
    assert block_ok is not None
    assert err_ok is None

    block_overlap, err_overlap = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.40.0.64/26",
            "notes": None,
            "is_active": True,
        },
    )
    assert block_overlap is None
    assert err_overlap is not None
    assert "overlaps existing block" in err_overlap.lower()

    block_outside, err_outside = web_network_ip_service.create_ip_block(
        db_session,
        {
            "pool_id": str(pool.id),
            "cidr": "10.41.0.0/24",
            "notes": None,
            "is_active": True,
        },
    )
    assert block_outside is None
    assert err_outside is not None
    assert "must be inside pool cidr" in err_outside.lower()


def test_parse_ip_pool_form_supports_fallback_flag():
    form = {
        "name": "Fallback Pool",
        "ip_version": "ipv4",
        "cidr": "10.50.0.0/24",
        "is_fallback": "true",
        "is_active": "true",
    }
    parsed = web_network_ip_service.parse_ip_pool_form(form)
    assert parsed["is_fallback"] is True
    assert parsed["is_active"] is True


def test_create_ip_pool_normalizes_fallback_notes(db_session):
    created, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Fallback Tagged",
            "ip_version": "ipv4",
            "cidr": "10.51.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": "overflow pool",
            "is_fallback": True,
            "is_active": True,
        },
    )
    assert error is None
    assert created is not None
    assert created.notes is not None
    assert "[fallback]" in created.notes.lower()
    assert "overflow pool" in created.notes


def test_build_ip_pools_data_filters_fallback_and_standard(db_session):
    fallback, fallback_err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Fallback Only",
            "ip_version": "ipv4",
            "cidr": "10.52.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": "fallback note",
            "is_fallback": True,
            "is_active": True,
        },
    )
    standard, standard_err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Standard Only",
            "ip_version": "ipv4",
            "cidr": "10.53.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert fallback_err is None and standard_err is None
    assert fallback is not None and standard is not None

    fallback_state = web_network_ip_service.build_ip_pools_data(
        db_session, pool_type="fallback"
    )
    fallback_names = {pool.name for pool in fallback_state["pools"]}
    assert fallback_names == {"Fallback Only"}
    assert fallback_state["stats"]["fallback_pools"] == 1

    standard_state = web_network_ip_service.build_ip_pools_data(
        db_session, pool_type="standard"
    )
    standard_names = {pool.name for pool in standard_state["pools"]}
    assert "Standard Only" in standard_names
    assert "Fallback Only" not in standard_names


def test_build_ip_pools_data_tracks_ipv6_utilization(db_session):
    pool, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv6 Util Pool",
            "ip_version": "ipv6",
            "cidr": "2001:db8:abcd::/126",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert error is None
    assert pool is not None

    network_service.ipv6_addresses.create(
        db_session,
        IPv6AddressCreate(
            address="2001:db8:abcd::1", pool_id=pool.id, is_reserved=False
        ),
    )
    network_service.ipv6_addresses.create(
        db_session,
        IPv6AddressCreate(
            address="2001:db8:abcd::2", pool_id=pool.id, is_reserved=False
        ),
    )

    state = web_network_ip_service.build_ip_pools_data(db_session, pool_type="all")
    util = state["pool_utilization"][str(pool.id)]
    assert util["total"] == 4
    assert util["used"] == 0
    assert util["tracked"] == 2
    assert util["percent"] == 0


def test_pool_form_snapshot_from_model_parses_metadata(db_session):
    pool, error = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Meta IPv6 Pool",
            "ip_version": "ipv6",
            "cidr": "2001:db8:ffff::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": "main fabric",
            "location": "Abuja POP",
            "category": "Production",
            "network_type": "Infrastructure",
            "router": "Core-v6-1",
            "is_fallback": True,
            "is_active": True,
        },
    )
    assert error is None
    assert pool is not None

    snapshot = web_network_ip_service.pool_form_snapshot_from_model(pool)
    assert snapshot["location"] == "Abuja POP"
    assert snapshot["category"] == "Production"
    assert snapshot["network_type"] == "Infrastructure"
    assert snapshot["router"] == "Core-v6-1"
    assert snapshot["is_fallback"] is True
    assert snapshot["notes"] == "main fabric"


def test_build_ipv6_networks_data_filters_and_sorts(db_session):
    pool_a, err_a = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv6 Abuja",
            "ip_version": "ipv6",
            "cidr": "2001:db8:a::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "router": "R1",
            "is_fallback": False,
            "is_active": True,
        },
    )
    pool_b, err_b = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv6 Kano",
            "ip_version": "ipv6",
            "cidr": "2001:db8:b::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Kano",
            "category": "Dev",
            "network_type": "Infrastructure",
            "router": "R2",
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert err_a is None and err_b is None
    assert pool_a is not None and pool_b is not None

    filtered = web_network_ip_service.build_ipv6_networks_data(
        db_session,
        location="Abuja",
        category="Production",
        sort_by="title",
        sort_dir="asc",
    )
    assert len(filtered["networks"]) == 1
    assert filtered["networks"][0]["pool"].name == "IPv6 Abuja"

    sorted_state = web_network_ip_service.build_ipv6_networks_data(
        db_session,
        sort_by="title",
        sort_dir="desc",
    )
    names = [item["pool"].name for item in sorted_state["networks"]]
    assert names == sorted(names, reverse=True)


def test_build_ipv4_networks_data_filters_sorts_and_utilization(db_session):
    pool_a, err_a = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv4 Abuja",
            "ip_version": "ipv4",
            "cidr": "10.70.0.0/24",
            "gateway": "10.70.0.1",
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "router": "RTR-A",
            "usage_type": "Static",
            "allow_network_broadcast": False,
            "is_fallback": False,
            "is_active": True,
        },
    )
    pool_b, err_b = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv4 Kano",
            "ip_version": "ipv4",
            "cidr": "10.71.0.0/24",
            "gateway": "10.71.0.1",
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Kano",
            "category": "Dev",
            "network_type": "Infrastructure",
            "router": "RTR-B",
            "usage_type": "Dynamic/DHCP",
            "allow_network_broadcast": True,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert err_a is None and err_b is None
    assert pool_a is not None and pool_b is not None

    network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.70.0.10", pool_id=pool_a.id, is_reserved=False),
    )
    network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.70.0.11", pool_id=pool_a.id, is_reserved=False),
    )

    filtered = web_network_ip_service.build_ipv4_networks_data(
        db_session,
        location="Abuja",
        category="Production",
        network_type="EndNet",
        sort_by="title",
        sort_dir="asc",
    )
    assert len(filtered["networks"]) == 1
    net = filtered["networks"][0]
    assert net["pool"].name == "IPv4 Abuja"
    assert net["subnet_mask"] == "255.255.255.0"
    assert net["utilization"]["used"] == 0
    assert net["utilization"]["tracked"] == 2
    assert net["usage_type"] == "Static"

    sorted_state = web_network_ip_service.build_ipv4_networks_data(
        db_session,
        sort_by="title",
        sort_dir="desc",
    )
    names = [item["pool"].name for item in sorted_state["networks"]]
    assert names == sorted(names, reverse=True)


def test_build_ipv4_network_detail_data_exposes_assignment_rows(db_session, subscriber):
    pool, err = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "IPv4 Detail",
            "ip_version": "ipv4",
            "cidr": "10.72.0.0/29",
            "gateway": "10.72.0.1",
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "router": "RTR-D",
            "usage_type": "Static",
            "allow_network_broadcast": False,
            "is_fallback": False,
            "is_active": True,
        },
    )
    assert err is None
    assert pool is not None

    ip_record = network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.72.0.2", pool_id=pool.id, is_reserved=False),
    )
    reserved = network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.72.0.3", pool_id=pool.id, is_reserved=True),
    )
    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            account_id=subscriber.id,
            ip_version="ipv4",
            ipv4_address_id=ip_record.id,
            prefix_length=29,
            gateway="10.72.0.1",
            dns_primary="8.8.8.8",
            dns_secondary="8.8.4.4",
        ),
    )

    data = web_network_ip_service.build_ipv4_network_detail_data(
        db_session,
        pool_id=str(pool.id),
        limit=16,
    )
    assert data is not None
    statuses = {row["ip_address"]: row["status"] for row in data["ip_rows"]}
    assert statuses["10.72.0.2"] == "assigned"
    assert statuses["10.72.0.3"] == "reserved"
    assert "10.72.0.4" in statuses and statuses["10.72.0.4"] == "available"
    assert data["usage_type"] == "Static"
    assert data["allow_network_broadcast"] is False
    assert reserved is not None


def test_parse_ipv6_network_form_maps_to_pool_payload():
    values = web_network_ip_service.parse_ipv6_network_form(
        {
            "title": "Abuja IPv6",
            "network": "2001:db8:1::",
            "prefix_length": "48",
            "comment": "Core aggregation",
            "location": "Abuja",
            "category": "Production",
            "network_type": "EndNet",
            "usage_type": "Static",
            "router": "RTR-v6",
            "gateway": "2001:db8:1::1",
            "dns_primary": "2001:4860:4860::8888",
            "dns_secondary": "2001:4860:4860::8844",
            "is_active": "true",
        }
    )
    assert values["name"] == "Abuja IPv6"
    assert values["ip_version"] == "ipv6"
    assert values["cidr"] == "2001:db8:1::/48"
    assert values["usage_type"] == "Static"
    assert values["is_active"] is True


def test_create_ipv6_network_from_dedicated_form_values(db_session):
    values = web_network_ip_service.parse_ipv6_network_form(
        {
            "title": "Kano IPv6",
            "network": "2001:db8:2::",
            "prefix_length": "56",
            "comment": "Distribution",
            "location": "Kano",
            "category": "Dev",
            "network_type": "EndNet",
            "usage_type": "Dynamic/DHCP",
            "router": "RTR-v6-kano",
            "is_active": "true",
        }
    )
    pool, err = web_network_ip_service.create_ip_pool(db_session, values)
    assert err is None
    assert pool is not None
    snapshot = web_network_ip_service.pool_form_snapshot_from_model(pool)
    assert snapshot["ip_version"]["value"] == "ipv6"
    assert snapshot["cidr"] == "2001:db8:2::/56"
    assert snapshot["usage_type"] == "Dynamic/DHCP"
    assert snapshot["location"] == "Kano"


def test_build_dual_stack_data_groups_ipv4_and_ipv6_by_subscriber_location(
    db_session,
    subscriber,
):
    address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        address_line1="10 Main Street",
        city="Abuja",
        region="FCT",
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()
    db_session.refresh(address)

    pool4, err4 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Dual v4",
            "ip_version": "ipv4",
            "cidr": "10.60.0.0/24",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    pool6, err6 = web_network_ip_service.create_ip_pool(
        db_session,
        {
            "name": "Dual v6",
            "ip_version": "ipv6",
            "cidr": "2001:db8:60::/64",
            "gateway": None,
            "dns_primary": None,
            "dns_secondary": None,
            "notes": None,
            "is_active": True,
        },
    )
    assert err4 is None and err6 is None
    assert pool4 is not None and pool6 is not None

    v4 = network_service.ipv4_addresses.create(
        db_session,
        IPv4AddressCreate(address="10.60.0.10", pool_id=pool4.id, is_reserved=False),
    )
    v6 = network_service.ipv6_addresses.create(
        db_session,
        IPv6AddressCreate(
            address="2001:db8:60::10", pool_id=pool6.id, is_reserved=False
        ),
    )

    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            account_id=subscriber.id,
            service_address_id=address.id,
            ip_version="ipv4",
            ipv4_address_id=v4.id,
            is_active=True,
        ),
    )
    network_service.ip_assignments.create(
        db_session,
        IPAssignmentCreate(
            account_id=subscriber.id,
            service_address_id=address.id,
            ip_version="ipv6",
            ipv6_address_id=v6.id,
            prefix_length=64,
            is_active=True,
        ),
    )

    state = web_network_ip_service.build_dual_stack_data(
        db_session,
        view_mode="location",
        location_query="abuja",
    )
    assert state["stats"]["total_records"] == 1
    assert state["stats"]["dual_stack_records"] == 1
    assert len(state["rows"]) == 1
    row = state["rows"][0]
    assert row["is_dual_stack"] is True
    assert row["ipv4_address"] == "10.60.0.10"
    assert row["ipv6_address"] == "2001:db8:60::10/64"
    assert "Abuja" in (row["location"] or "")
