"""Tests for the OLT and ACS readers.

Both readers take their I/O dependency as a parameter, so tests use minimal
stub classes rather than mocking SSH or HTTP. The dataclass return shape is
exercised end-to-end against synthetic inputs.

These tests don't hit a DB; they construct ``OntDesiredState`` instances
in-memory.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.genieacs_client import GenieACSError
from app.services.network.reconcile import (
    OntDesiredState,
    Tr069WifiParameterPaths,
    Tr181WanParameterPaths,
    read_acs_state,
    read_olt_state,
)


def _tr181_wan_paths() -> Tr181WanParameterPaths:
    return Tr181WanParameterPaths(
        ip_enable="Device.IP.Interface.1.Enable",
        dhcp_enable="Device.DHCPv4.Client.1.Enable",
        ip_address="Device.IP.Interface.1.IPv4Address.1.IPAddress",
        subnet_mask="Device.IP.Interface.1.IPv4Address.1.SubnetMask",
        gateway="Device.Routing.Router.1.IPv4Forwarding.1.GatewayIPAddress",
        dns_primary="Device.DNS.Client.Server.1.DNSServer",
        dns_secondary="Device.DNS.Client.Server.2.DNSServer",
        nat_enable="Device.NAT.InterfaceSetting.1.Enable",
        vlan_enable="Device.Ethernet.VLANTermination.1.Enable",
        vlan_id="Device.Ethernet.VLANTermination.1.VLANID",
    )


@pytest.fixture(autouse=True)
def _stub_optical(monkeypatch):
    """Default to a no-op optical read so existing tests stay deterministic."""
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_optical_info",
        lambda *_a, **_k: (False, "not stubbed", None),
    )


@pytest.fixture(autouse=True)
def _stub_service_ports(monkeypatch):
    """Default to a no-op service-port enumeration so existing tests stay
    deterministic. Specific tests override this when they need to assert
    on the populated tuple."""
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_service_ports_for_ont",
        lambda *_a, **_k: (False, "not stubbed", []),
    )


# ── Shared in-memory OntDesiredState ────────────────────────────────────────


def _desired(**overrides) -> OntDesiredState:
    defaults = dict(
        ont_unit_id="ont-1",
        serial_number="HWTC8535819A",
        olt_id="olt-spdc",
        fsp="0/1/3",
        olt_ont_id=11,
        line_profile_id=40,
        service_profile_id=42,
        description="stub_authd_20260513",
        mgmt_vlan=201,
        mgmt_ip="172.16.210.20",
        mgmt_subnet_mask="255.255.255.0",
        mgmt_gateway="172.16.210.1",
        mgmt_dns_primary="8.8.8.8",
        mgmt_dns_secondary="4.2.2.2",
        mgmt_iphost_priority=2,
        tr069_profile_id=2,
        acs_server_id="acs-dotmac",
        cr_username="admin",
        cr_password_ref="bao://cr",
        periodic_inform_interval_sec=300,
        wan_mode="pppoe",
        wan_vlan=203,
        wan_gem_index=1,
        wan_pppoe_username="100024456",
        wan_pppoe_password_ref="bao://pppoe",
        wan_pppoe_provisioning_method="tr069",
        wan_pppoe_wcd_index=1,
        wan_pppoe_instance_index=1,
        wan_config_profile_id=None,
        wan_internet_config_ip_index=None,
        nat_enabled=True,
        ipv6_enabled=False,
        dhcp_enabled=True,
        dhcp_pool_min="192.168.100.2",
        dhcp_pool_max="192.168.100.254",
        dhcp_subnet_mask="255.255.255.0",
        wifi_ssid="KURSI",
        wifi_password_ref="bao://wifi",
        wifi_password_pushed_at=None,
        mgmt_service_port_index=23,
        wan_service_port_index=22,
        subscriber_external_id=None,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
    )
    defaults.update(overrides)
    return OntDesiredState(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# OLT reader
# ─────────────────────────────────────────────────────────────────────────────


class _StubAdapter:
    """Minimal OltProtocolAdapter stub.

    Drives the two branches the reader cares about:
    - ``find_ont_by_serial`` outcome (success / failure / unreachable)
    - whether a registration is present in the result
    """

    def __init__(
        self,
        *,
        find_success: bool = True,
        find_message: str = "ok",
        registration: object | None = None,
    ):
        self.olt = SimpleNamespace(name="OLT-TEST")
        self._find_success = find_success
        self._find_message = find_message
        self._registration = registration
        self.find_calls: list[str] = []

    def find_ont_by_serial(self, serial_number: str):
        self.find_calls.append(serial_number)
        return SimpleNamespace(
            success=self._find_success,
            message=self._find_message,
            data={"registration": self._registration},
        )


def test_olt_reader_returns_unreachable_when_ssh_connection_failed(
    monkeypatch,
):
    adapter = _StubAdapter(
        find_success=False,
        find_message="Connection failed: timed out",
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is False
    assert result.unreachable is True
    assert "timed out" in (result.error or "")
    assert result.observed is None


def test_olt_reader_returns_clean_absent_when_ont_not_registered(monkeypatch):
    adapter = _StubAdapter(find_success=True, registration=None)
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    assert result.unreachable is False
    assert result.observed is not None
    assert result.observed.olt_present is False
    assert result.observed.olt_match_state is None


def test_olt_reader_returns_failure_when_olt_command_errored(monkeypatch):
    adapter = _StubAdapter(
        find_success=False,
        find_message="OLT error: Failure: insufficient privilege",
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is False
    assert result.unreachable is False
    assert "insufficient privilege" in (result.error or "")


def test_olt_reader_populates_detail_fields_from_get_ont_info_detail(monkeypatch):
    """When ``get_ont_info_detail`` returns rich fields, the reader fills
    description / line+srv profile id / mgmt IP+VLAN / distance from them."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda olt, fsp, ont_id: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="HWTC8535819A",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda olt, fsp, ont_id: (
            True,
            "ok",
            {
                "description": "Kolawole_Idiaro_2_authd_20260512",
                "line_profile_id": 40,
                "service_profile_id": 42,
                "tr069_profile_id": 5,
                "mgmt_ip": "172.16.210.20",
                "mgmt_vlan": 201,
                "distance_m": 4374,
            },
        ),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    obs = result.observed
    assert obs.olt_description == "Kolawole_Idiaro_2_authd_20260512"
    assert obs.olt_line_profile_id == 40
    assert obs.olt_service_profile_id == 42
    assert obs.olt_tr069_profile_id == 5
    assert obs.olt_mgmt_ip == "172.16.210.20"
    assert obs.olt_mgmt_vlan == 201
    assert obs.olt_distance_m == 4374


def test_olt_reader_filters_dash_placeholders_from_detail(monkeypatch):
    """Huawei emits ``-`` for missing values; reader normalises to None."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *a, **k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *a, **k: (True, "ok", {"description": "-", "mgmt_ip": "-"}),
    )
    result = read_olt_state(adapter, _desired())
    assert result.observed.olt_description is None
    assert result.observed.olt_mgmt_ip is None


def test_olt_reader_detail_unreachable_propagates_as_unreachable(monkeypatch):
    """If the detail SSH call gets a connection-failed error, the whole read
    is treated as unreachable — partial data isn't worth the risk."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *a, **k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *a, **k: (False, "Connection failed: timed out", None),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is False
    assert result.unreachable is True


def test_parse_ont_info_detail_extracts_huawei_keyvalue_lines():
    """Pure parser test against a representative Huawei display-ont-info
    output. No SSH involved."""
    from app.services.network.olt_ssh_ont.status import parse_ont_info_detail

    sample = """\
  F/S/P                   : 0/1/3
  ONT-ID                  : 11
  Control flag            : active
  Run state               : online
  Config state            : normal
  Match state             : match
  ONT distance(m)         : 4374
  Authentic type          : SN-auth
  SN                      : 485754438535819A (HWTC-8535819A)
  Management mode         : OMCI
  ONT IP 0 address/mask   : 172.16.210.20/24
  ONT manage VLAN         : 201
  Description             : Kolawole_Idiaro_2_zone_Zone_1_au
                            thd_20260512
  Line profile ID      : 40
  Line profile name    : SMARTOLT_FLEXIBLE_GPON
  Service profile ID   : 42
  Service profile name : HG8546M
"""
    parsed = parse_ont_info_detail(sample)
    assert parsed["description"] == "Kolawole_Idiaro_2_zone_Zone_1_authd_20260512"
    assert parsed["line_profile_id"] == 40
    assert parsed["service_profile_id"] == 42
    assert parsed["mgmt_ip"] == "172.16.210.20"
    assert parsed["mgmt_vlan"] == 201
    assert parsed["distance_m"] == 4374


def test_parse_ont_info_detail_returns_none_for_missing_fields():
    from app.services.network.olt_ssh_ont.status import parse_ont_info_detail

    parsed = parse_ont_info_detail("F/S/P : 0/1/3\nONT-ID : 11\n")
    assert all(v is None for v in parsed.values())


def test_olt_reader_populates_present_match_and_run_state(monkeypatch):
    """When find returns a registration AND get_ont_status succeeds, the reader
    reports present=True with normalized state strings."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda olt, fsp, ont_id: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda olt, fsp, ont_id: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="HWTC8535819A",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    assert result.observed is not None
    assert result.observed.olt_present is True
    assert result.observed.olt_run_state == "online"
    assert result.observed.olt_match_state == "match"


def test_olt_reader_treats_present_but_status_dark_as_unknown_state(monkeypatch):
    """If the ONT is registered but get_ont_status fails (non-connection),
    report present=True with unknown state — the planner should still see it
    in the OLT table."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda olt, fsp, ont_id: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda olt, fsp, ont_id: (False, "Failure: ONT busy", None),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    assert result.observed.olt_present is True
    assert result.observed.olt_run_state is None
    assert result.observed.olt_match_state is None


def test_olt_reader_normalises_legacy_normal_run_state(monkeypatch):
    """Older Huawei firmware reports 'normal' for healthy ONTs."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda olt, fsp, ont_id: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda olt, fsp, ont_id: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="HWTC8535819A",
                run_state="normal",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    result = read_olt_state(adapter, _desired())
    assert result.observed.olt_run_state == "online"


def test_olt_reader_unknown_state_strings_normalise_to_none(monkeypatch):
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda olt, fsp, ont_id: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda olt, fsp, ont_id: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="HWTC8535819A",
                run_state="weird-state",
                match_state="unknown",
                config_state="normal",
            ),
        ),
    )
    result = read_olt_state(adapter, _desired())
    assert result.observed.olt_run_state is None
    assert result.observed.olt_match_state is None


def test_olt_reader_rejects_adapter_without_olt_attribute():
    adapter = SimpleNamespace(find_ont_by_serial=lambda s: None)
    result = read_olt_state(adapter, _desired())
    assert result.success is False
    assert result.unreachable is False
    assert "no .olt" in (result.error or "")


def test_olt_reader_populates_optical_fields_from_optical_info(monkeypatch):
    from app.services.network.olt_ssh_diagnostics import OpticalInfo

    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *_a, **_k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *_a, **_k: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_optical_info",
        lambda *_a, **_k: (
            True,
            "ok",
            OpticalInfo(
                fsp="0/1/3",
                ont_id=11,
                rx_power_dbm=-22.4,
                tx_power_dbm=2.1,
                olt_rx_power_dbm=-21.8,
                temperature_c=44.6,
            ),
        ),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    obs = result.observed
    assert obs.olt_rx_dbm == -21.8  # OLT-side Rx (drop-fiber alert metric)
    assert obs.olt_tx_dbm == 2.1  # ONT-reported upstream Tx
    assert obs.olt_temperature_c == 45  # rounded to int


def test_olt_reader_tolerates_optical_failure(monkeypatch):
    """An out-of-range / unsupported optical reply leaves the fields None
    but does NOT fail the whole read."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *_a, **_k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *_a, **_k: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_optical_info",
        lambda *_a, **_k: (False, "Out of range", None),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    obs = result.observed
    assert obs.olt_rx_dbm is None
    assert obs.olt_tx_dbm is None
    assert obs.olt_temperature_c is None


def test_olt_reader_populates_service_ports_from_get_service_ports_for_ont(
    monkeypatch,
):
    from app.services.network.parsers.loader import ServicePortEntry

    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *_a, **_k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *_a, **_k: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_service_ports_for_ont",
        lambda *_a, **_k: (
            True,
            "ok",
            [
                ServicePortEntry(
                    index=22,
                    vlan_id=203,
                    ont_id=11,
                    gem_index=1,
                    flow_type="vlan",
                    flow_para="203",
                    state="up",
                    fsp="0/1/3",
                    tag_transform="translate",
                ),
                ServicePortEntry(
                    index=23,
                    vlan_id=201,
                    ont_id=11,
                    gem_index=2,
                    flow_type="vlan",
                    flow_para="201",
                    state="up",
                    fsp="0/1/3",
                    tag_transform="translate",
                ),
            ],
        ),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    ports = result.observed.olt_service_ports
    assert len(ports) == 2
    assert ports[0] == {
        "index": 22,
        "vlan_id": 203,
        "ont_id": 11,
        "gem_index": 1,
        "flow_type": "vlan",
        "flow_para": "203",
        "state": "up",
        "fsp": "0/1/3",
        "tag_transform": "translate",
    }
    # Tuple, not list — planner expects an immutable observation.
    assert isinstance(result.observed.olt_service_ports, tuple)


def test_olt_reader_service_ports_empty_on_failure(monkeypatch):
    """SSH failure on service-port read leaves olt_service_ports as ()
    without failing the whole read."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *_a, **_k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *_a, **_k: (True, "ok", {}),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_service_ports_for_ont",
        lambda *_a, **_k: (False, "SSH error", []),
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    assert result.observed.olt_service_ports == ()


def test_olt_reader_catches_service_ports_exception(monkeypatch):
    """A raised exception inside service-port enumeration is swallowed so
    the rest of the read still produces a useful observation."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *_a, **_k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *_a, **_k: (True, "ok", {}),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("ssh torn down mid-read")

    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_service_ports_for_ont",
        _boom,
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    assert result.observed.olt_service_ports == ()


def test_olt_reader_catches_optical_exception(monkeypatch):
    """If get_ont_optical_info raises (e.g. SSH closed unexpectedly), the
    optical fields collapse to None instead of crashing the read."""
    adapter = _StubAdapter(
        find_success=True,
        registration=SimpleNamespace(fsp="0/1/3", onu_id=11),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_status",
        lambda *_a, **_k: (
            True,
            "ok",
            SimpleNamespace(
                serial_number="x",
                run_state="online",
                match_state="match",
                config_state="normal",
            ),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_info_detail",
        lambda *_a, **_k: (True, "ok", {}),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("ssh session torn down")

    monkeypatch.setattr(
        "app.services.network.reconcile.readers.olt_reader.get_ont_optical_info",
        _boom,
    )
    result = read_olt_state(adapter, _desired())
    assert result.success is True
    assert result.observed.olt_rx_dbm is None


# ─────────────────────────────────────────────────────────────────────────────
# ACS reader
# ─────────────────────────────────────────────────────────────────────────────


class _StubGenieAcsClient:
    """Drives the list_devices outcomes the reader cares about."""

    def __init__(self, *, devices=None, raises=None):
        self._devices = devices if devices is not None else []
        self._raises = raises
        self.list_calls: list[tuple] = []

    def list_devices(self, query=None, projection=None):
        self.list_calls.append((query, projection))
        if self._raises is not None:
            raise self._raises
        return self._devices


def _leaf(value):
    """Build a GenieACS leaf dict (the ``_value``-bearing shape)."""
    return {"_value": value, "_object": False, "_writable": True}


def _device_doc(*, serial: str = "HWTC8535819A", overrides=None) -> dict:
    """Construct a representative GenieACS device document."""
    overrides = overrides or {}
    base = {
        "_id": f"00259E-HG8546M-{serial}",
        "_lastInform": "2026-05-13T00:00:00.000Z",
        "_lastBoot": "2026-05-13T00:00:00.000Z",
        "_lastBootstrap": "2026-05-12T19:28:25.000Z",
        "InternetGatewayDevice": {
            "DeviceInfo": {"SoftwareVersion": _leaf("V5R019C10S100")},
            "ManagementServer": {
                "URL": _leaf("https://acs.example.net/cwmp"),
                "Username": _leaf("cwmp-user"),
                "Password": _leaf("configured"),
                "PeriodicInformInterval": _leaf("300"),
                "ConnectionRequestUsername": _leaf("admin"),
                "ConnectionRequestPassword": _leaf("admin"),
            },
            "X_HW_UserInterface": {
                "SSHEnable": _leaf("true"),
                "SSHPort": _leaf("22"),
                "TelnetEnable": _leaf("false"),
                "TelnetPort": _leaf("23"),
            },
            "WANDevice": {
                "1": {
                    "WANConnectionDevice": {
                        "1": {
                            "WANPPPConnection": {
                                "1": {
                                    "Username": _leaf("100024456"),
                                    "Enable": _leaf("true"),
                                    "X_HW_VLAN": _leaf("203"),
                                    "NATEnabled": _leaf("true"),
                                    "ConnectionStatus": _leaf("Connected"),
                                }
                            }
                        }
                    }
                }
            },
            "LANDevice": {
                "1": {
                    "LANHostConfigManagement": {"DHCPServerEnable": _leaf("true")},
                    "WLANConfiguration": {
                        "1": {
                            "SSID": _leaf("KURSI"),
                            "Enable": _leaf("true"),
                            "Channel": _leaf("6"),
                            "BeaconType": _leaf("11i"),
                        }
                    },
                }
            },
        },
    }
    base.update(overrides)
    return base


def test_acs_reader_returns_unreachable_when_genieacs_errors():
    client = _StubGenieAcsClient(raises=GenieACSError("502 Bad Gateway"))
    result = read_acs_state(client, _desired())
    assert result.success is False
    assert result.unreachable is True
    assert "Bad Gateway" in (result.error or "")


def test_acs_reader_returns_unreachable_on_unexpected_exception():
    client = _StubGenieAcsClient(raises=RuntimeError("socket reset"))
    result = read_acs_state(client, _desired())
    assert result.success is False
    assert result.unreachable is True


def test_acs_reader_returns_absent_when_device_not_in_genieacs():
    """ONT hasn't bootstrapped yet — clean read, present=False."""
    client = _StubGenieAcsClient(devices=[])
    result = read_acs_state(client, _desired())
    assert result.success is True
    assert result.unreachable is False
    assert result.observed is not None
    assert result.observed.acs_present is False


def test_acs_reader_uses_trailing_serial_regex_match():
    """The query should match any device-id ending with the desired serial."""
    client = _StubGenieAcsClient(devices=[])
    read_acs_state(client, _desired(serial_number="HWTCABC123"))
    assert len(client.list_calls) == 1
    query, projection = client.list_calls[0]
    assert query == {"_id": {"$regex": r".*-HWTCABC123$"}}
    assert "InternetGatewayDevice.ManagementServer.PeriodicInformInterval" in projection
    assert "Device.IP.Interface" in projection


def test_acs_reader_uses_recorded_id_for_hex_serial_device_document():
    """Huawei OLT and CWMP serial renderings still identify one exact ONT."""
    device_id = "00259E-HG8546M-485754431DAF83D1"
    client = _StubGenieAcsClient(devices=[_device_doc(serial="485754431DAF83D1")])

    result = read_acs_state(
        client,
        _desired(
            serial_number="HWTC1DAF83D1",
            acs_device_id=device_id,
        ),
    )

    assert client.list_calls[0][0] == {"_id": device_id}
    assert len(client.list_calls) == 1
    assert result.observed is not None
    assert result.observed.acs_present is True
    assert result.observed.acs_observed_device_id == device_id


def test_acs_reader_finds_hex_serial_variant_without_a_recorded_id():
    device_id = "00259E-HG8546M-485754431DAF83D1"
    client = _StubGenieAcsClient(devices=[_device_doc(serial="485754431DAF83D1")])

    result = read_acs_state(
        client,
        _desired(serial_number="HWTC1DAF83D1", acs_device_id=None),
    )

    assert client.list_calls[0][0] == {
        "_id": {"$regex": r".*-(?:HWTC1DAF83D1|HWTC\-1DAF83D1|485754431DAF83D1)$"}
    }
    assert result.observed is not None
    assert result.observed.acs_observed_device_id == device_id


def test_acs_reader_falls_back_to_serial_to_expose_stale_recorded_id():
    recorded_id = "00259E-HG8546M-HWTCOLD"
    observed_id = "00259E-HG8546M-HWTCABC123"

    class _StaleIdentityClient:
        def __init__(self):
            self.list_calls: list[tuple[dict[str, object] | None, object]] = []

        def list_devices(self, query=None, projection=None):
            self.list_calls.append((query, projection))
            if query == {"_id": recorded_id}:
                return []
            return [_device_doc(serial="HWTCABC123")]

    client = _StaleIdentityClient()
    result = read_acs_state(
        client,
        _desired(serial_number="HWTCABC123", acs_device_id=recorded_id),
    )

    assert [call[0] for call in client.list_calls] == [
        {"_id": recorded_id},
        {"_id": {"$regex": r".*-HWTCABC123$"}},
    ]
    assert result.observed is not None
    assert result.observed.acs_observed_device_id == observed_id


def test_acs_reader_never_projects_wifi_psk() -> None:
    desired = _desired(
        wifi_paths=Tr069WifiParameterPaths(
            enabled="Device.WiFi.SSID.1.Enable",
            ssid="Device.WiFi.SSID.1.SSID",
            psk_path="Device.WiFi.AccessPoint.1.Security.KeyPassphrase",
            channel="Device.WiFi.Radio.1.Channel",
            security_mode="Device.WiFi.AccessPoint.1.Security.ModeEnabled",
        )
    )
    client = _StubGenieAcsClient(devices=[])

    read_acs_state(client, desired)

    projection = client.list_calls[0][1]
    assert "Device.WiFi.SSID.1.SSID" in projection
    assert "KeyPassphrase" not in projection


def test_acs_reader_parses_a_full_device_document():
    client = _StubGenieAcsClient(devices=[_device_doc()])
    result = read_acs_state(client, _desired())
    obs = result.observed
    assert obs is not None
    assert obs.acs_present is True
    assert obs.acs_last_inform_at == datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    assert obs.acs_observed_software_version == "V5R019C10S100"
    assert obs.acs_observed_pppoe_username == "100024456"
    assert obs.acs_observed_pppoe_enable is True
    assert obs.acs_observed_wan_vlan == 203
    assert obs.acs_observed_nat_enabled is True
    assert obs.acs_observed_dhcp_enabled is True
    assert obs.acs_observed_ssid == "KURSI"
    assert obs.acs_observed_wifi_enabled is True
    assert obs.acs_observed_wifi_channel == 6
    assert obs.acs_observed_wifi_security_mode == "11i"
    assert obs.acs_observed_wifi_instance_index == 1
    assert obs.acs_observed_remote_ssh_enabled is True
    assert obs.acs_observed_remote_ssh_port == 22
    assert obs.acs_observed_remote_telnet_enabled is False
    assert obs.acs_observed_remote_telnet_port == 23
    assert obs.acs_observed_periodic_inform_interval_sec == 300
    assert obs.acs_observed_cr_username == "admin"
    assert obs.acs_observed_cr_username_set is True
    assert obs.acs_observed_cr_password_set is True
    assert obs.acs_observed_url == "https://acs.example.net/cwmp"
    assert obs.acs_observed_username == "cwmp-user"
    assert obs.acs_observed_password_set is True
    assert obs.acs_observed_wan_wcd_index == 1
    assert obs.acs_observed_wan_instance_index == 1
    assert obs.acs_observed_wan_ppp_locations == ((1, 1),)


def test_acs_reader_selects_active_wifi_instance() -> None:
    doc = _device_doc()
    wlan = doc["InternetGatewayDevice"]["LANDevice"]["1"]["WLANConfiguration"]
    wlan["1"] = {
        "SSID": _leaf("placeholder"),
        "Enable": _leaf("false"),
        "Status": _leaf("Down"),
    }
    wlan["7"] = {
        "SSID": _leaf("CUSTOMER"),
        "Enable": _leaf("true"),
        "Status": _leaf("Up"),
        "Channel": _leaf("11"),
        "BeaconType": _leaf("11i"),
    }

    observed = read_acs_state(_StubGenieAcsClient(devices=[doc]), _desired()).observed

    assert observed is not None
    assert observed.acs_observed_wifi_instance_index == 7
    assert observed.acs_observed_ssid == "CUSTOMER"
    assert observed.acs_observed_wifi_channel == 11


def test_acs_reader_parses_tr098_static_wan_ip_state():
    doc = _device_doc()
    doc["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"]["2"] = {
        "WANIPConnection": {
            "1": {
                "Enable": _leaf("true"),
                "AddressingType": _leaf("Static"),
                "ExternalIPAddress": _leaf("198.51.100.10"),
                "SubnetMask": _leaf("255.255.255.248"),
                "DefaultGateway": _leaf("198.51.100.9"),
                "X_HW_SERVICELIST": _leaf("INTERNET"),
            }
        }
    }

    observed = read_acs_state(_StubGenieAcsClient(devices=[doc]), _desired()).observed

    assert observed.acs_observed_wan_ip_enable is True
    assert observed.acs_observed_wan_addressing_type == "Static"
    assert observed.acs_observed_wan_ip_address == "198.51.100.10"
    assert observed.acs_observed_wan_gateway == "198.51.100.9"


def test_acs_reader_prefers_internet_wan_ip_over_tr069_management():
    doc = _device_doc()
    wcd = doc["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"]
    wcd["1"]["WANIPConnection"] = {
        "1": {
            "Enable": _leaf("true"),
            "AddressingType": _leaf("DHCP"),
            "ExternalIPAddress": _leaf("172.16.201.10"),
            "X_HW_SERVICELIST": _leaf("TR069"),
        }
    }
    wcd["2"] = {
        "WANIPConnection": {
            "1": {
                "Enable": _leaf("true"),
                "AddressingType": _leaf("DHCP"),
                "ExternalIPAddress": _leaf("100.64.0.10"),
                "X_HW_SERVICELIST": _leaf("INTERNET"),
            }
        }
    }

    observed = read_acs_state(_StubGenieAcsClient(devices=[doc]), _desired()).observed

    assert observed.acs_observed_wan_ip_address == "100.64.0.10"


def test_acs_reader_locates_wan_ppp_on_alternate_wcd_slot():
    """Multi-WCD device — WANPPPConnection actually lives on
    WANConnectionDevice.2.WANPPPConnection.1. Reader must find it."""
    doc = _device_doc()
    doc["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"] = {
        "1": {},
        "2": {
            "WANPPPConnection": {
                "1": {
                    "Username": _leaf("100099999"),
                    "Enable": _leaf("true"),
                    "X_HW_VLAN": _leaf("203"),
                }
            }
        },
    }
    client = _StubGenieAcsClient(devices=[doc])
    result = read_acs_state(client, _desired())
    assert result.observed.acs_observed_wan_wcd_index == 2
    assert result.observed.acs_observed_wan_instance_index == 1
    assert result.observed.acs_observed_wan_ppp_locations == ((2, 1),)
    assert result.observed.acs_observed_pppoe_username == "100099999"


def test_acs_reader_handles_device_with_no_wan_ppp_instance():
    """Fresh ONT: bootstrap done, but operator hasn't created
    WANPPPConnection yet. PPPoE fields should be None, present=True."""
    doc = _device_doc()
    doc["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"] = {"1": {}}
    client = _StubGenieAcsClient(devices=[doc])
    result = read_acs_state(client, _desired())
    assert result.observed.acs_present is True
    assert result.observed.acs_observed_pppoe_username is None
    assert result.observed.acs_observed_wan_wcd_index is None
    assert result.observed.acs_observed_wan_instance_index is None


def test_acs_reader_handles_empty_cr_username_as_blank_string():
    """CR username is value-verified. A blank string stays distinct from a
    missing field so the planner can treat it as a mismatch."""
    doc = _device_doc()
    doc["InternetGatewayDevice"]["ManagementServer"]["ConnectionRequestUsername"] = (
        _leaf("")
    )
    client = _StubGenieAcsClient(devices=[doc])
    result = read_acs_state(client, _desired())
    assert result.observed.acs_observed_cr_username == ""
    assert result.observed.acs_observed_cr_username_set is False


def test_acs_reader_uses_tr181_root_when_present():
    """For future TR-181 devices, the reader falls through to the Device.*
    root for software version and management-server fields."""
    doc = {
        "_id": "00ABCD-TR181Box-HWTC1111",
        "_lastInform": "2026-05-13T00:00:00Z",
        "Device": {
            "DeviceInfo": {"SoftwareVersion": _leaf("FW-1.0")},
            "ManagementServer": {"PeriodicInformInterval": _leaf("600")},
            "IP": {"Interface": {"1": {"IPv6Enable": _leaf("true")}}},
        },
    }
    client = _StubGenieAcsClient(devices=[doc])
    result = read_acs_state(client, _desired())
    assert result.observed.acs_observed_software_version == "FW-1.0"
    assert result.observed.acs_observed_periodic_inform_interval_sec == 600
    assert result.observed.acs_data_model_root == "Device"
    assert result.observed.acs_observed_ipv6_enabled is True
    # No TR-098 → all WAN/LAN/WiFi fields None
    assert result.observed.acs_observed_pppoe_username is None
    assert result.observed.acs_observed_ssid is None


def test_acs_reader_parses_model_resolved_tr181_static_wan():
    doc = {
        "_id": "00ABCD-EG8145V5-HWTC1111",
        "Device": {
            "IP": {
                "Interface": {
                    "1": {
                        "Enable": _leaf("true"),
                        "IPv4Address": {
                            "1": {
                                "IPAddress": _leaf("160.119.127.194"),
                                "SubnetMask": _leaf("255.255.255.248"),
                            }
                        },
                    }
                }
            },
            "DHCPv4": {"Client": {"1": {"Enable": _leaf("false")}}},
            "Routing": {
                "Router": {
                    "1": {
                        "IPv4Forwarding": {
                            "1": {"GatewayIPAddress": _leaf("160.119.127.193")}
                        }
                    }
                }
            },
            "DNS": {
                "Client": {
                    "Server": {
                        "1": {"DNSServer": _leaf("1.1.1.1")},
                        "2": {"DNSServer": _leaf("8.8.8.8")},
                    }
                }
            },
            "NAT": {"InterfaceSetting": {"1": {"Enable": _leaf("false")}}},
            "Ethernet": {
                "VLANTermination": {
                    "1": {"Enable": _leaf("true"), "VLANID": _leaf("203")}
                }
            },
        },
    }
    desired = _desired(
        tr069_data_model_root="Device", tr181_wan_paths=_tr181_wan_paths()
    )

    observed = read_acs_state(_StubGenieAcsClient(devices=[doc]), desired).observed

    assert observed.acs_observed_wan_addressing_type == "Static"
    assert observed.acs_observed_wan_ip_address == "160.119.127.194"
    assert observed.acs_observed_wan_gateway == "160.119.127.193"
    assert observed.acs_observed_wan_dns_servers == "1.1.1.1,8.8.8.8"
    assert observed.acs_observed_nat_enabled is False
    assert observed.acs_observed_wan_vlan == 203


def test_acs_reader_handles_malformed_timestamps():
    """A malformed _lastInform string shouldn't crash the parser."""
    doc = _device_doc()
    doc["_lastInform"] = "not-a-timestamp"
    client = _StubGenieAcsClient(devices=[doc])
    result = read_acs_state(client, _desired())
    assert result.success is True
    assert result.observed.acs_last_inform_at is None


def test_acs_reader_refreshes_and_reparses_when_wan_ppp_looks_ghosted():
    """Ghost-instance recovery. First list_devices returns a doc where
    WANPPPConnection.1 has Username/Enable/etc. cached but no
    ConnectionStatus — the signature of a setParameterValues that landed on
    a non-existent CWMP path on HG8546M V5R019C10S100. The reader must
    queue a narrow refreshObject on the affected WCD and re-parse, picking
    up the post-refresh truth (instance gone). Without this, the planner
    skips addObject and keeps re-writing ghosts."""

    class _RefreshAwareClient:
        def __init__(self, *, before, after):
            self._before = before
            self._after = after
            self._refreshed = False
            self.list_calls = 0
            self.refresh_calls: list[tuple] = []

        def list_devices(self, query=None, projection=None):
            self.list_calls += 1
            return self._after if self._refreshed else self._before

        def refresh_object(self, device_id, object_path, *, allow_when_pending=False):
            self.refresh_calls.append((device_id, object_path, allow_when_pending))
            self._refreshed = True
            return {"status": "queued"}

    ghost_doc = _device_doc()
    # Ghost shape: Username/Enable/X_HW_VLAN present, ConnectionStatus absent.
    ghost_doc["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"] = {
        "2": {
            "WANPPPConnection": {
                "1": {
                    "Username": _leaf("100025915"),
                    "Enable": _leaf("true"),
                    "X_HW_VLAN": _leaf("203"),
                    "NATEnabled": _leaf("true"),
                }
            }
        }
    }
    truth_doc = _device_doc()
    # Post-refresh truth: no .1 instance on the device for that WCD.
    truth_doc["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"] = {
        "2": {"WANPPPConnection": {}}
    }

    client = _RefreshAwareClient(before=[ghost_doc], after=[truth_doc])
    result = read_acs_state(client, _desired())

    assert result.success is True
    assert client.list_calls == 2  # initial + post-refresh re-fetch
    assert len(client.refresh_calls) == 1
    device_id, path, allow_when_pending = client.refresh_calls[0]
    assert path == "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2"
    assert allow_when_pending is True
    # Post-refresh: ghost is gone, planner will now correctly schedule addObject.
    assert result.observed.acs_observed_wan_instance_index is None
    assert result.observed.acs_observed_wan_wcd_index is None
    assert result.observed.acs_observed_wan_ppp_locations == ()
    assert result.observed.acs_observed_pppoe_username is None


def test_acs_reader_skips_refresh_when_wan_ppp_state_is_healthy():
    """The refresh path is opt-in via the ghost heuristic. A healthy
    observation (ConnectionStatus reported) must not trigger any extra
    refreshObject — sweepers run too often to pay that round-trip on every
    ONT."""

    class _RefreshAwareClient:
        def __init__(self, *, devices):
            self._devices = devices
            self.refresh_calls: list[tuple] = []

        def list_devices(self, query=None, projection=None):
            return self._devices

        def refresh_object(self, device_id, object_path, *, allow_when_pending=False):
            self.refresh_calls.append((device_id, object_path, allow_when_pending))
            return {"status": "queued"}

    client = _RefreshAwareClient(devices=[_device_doc()])
    result = read_acs_state(client, _desired())
    assert result.success is True
    assert result.observed.acs_observed_wan_connection_status == "Connected"
    assert client.refresh_calls == []


def test_acs_reader_falls_back_to_original_observation_when_refresh_fails():
    """If the refreshObject task fails (NBI error, device unreachable for
    CR, etc.), the reader returns the original cached observation rather
    than crashing the read — the planner has its own safety nets."""

    class _FlakyRefreshClient:
        def __init__(self, *, devices):
            self._devices = devices

        def list_devices(self, query=None, projection=None):
            return self._devices

        def refresh_object(self, device_id, object_path, *, allow_when_pending=False):
            raise RuntimeError("CR delivery timed out")

    ghost_doc = _device_doc()
    ghost_doc["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"] = {
        "2": {
            "WANPPPConnection": {
                "1": {
                    "Username": _leaf("100025915"),
                    "Enable": _leaf("true"),
                    "X_HW_VLAN": _leaf("203"),
                }
            }
        }
    }
    client = _FlakyRefreshClient(devices=[ghost_doc])
    result = read_acs_state(client, _desired())
    assert result.success is True
    # Original (ghost) observation preserved; planner will see the cached
    # instance and the existing safety nets take over.
    assert result.observed.acs_observed_wan_instance_index == 1
    assert result.observed.acs_observed_pppoe_username == "100025915"


def test_acs_reader_handles_genieacs_z_timestamp_format():
    """GenieACS commonly emits trailing-Z ISO timestamps."""
    doc = _device_doc()
    doc["_lastInform"] = "2026-05-13T12:34:56.789Z"
    client = _StubGenieAcsClient(devices=[doc])
    result = read_acs_state(client, _desired())
    assert result.observed.acs_last_inform_at == datetime(
        2026, 5, 13, 12, 34, 56, 789_000, tzinfo=UTC
    )


# ── Smoke: every field on AcsObservedFields has a reachable type ────────────


def test_absent_acs_observation_dataclass_round_trips():
    """An absent observation should be a valid frozen dataclass — sanity
    against the field set drifting against the state.py definition."""
    client = _StubGenieAcsClient(devices=[])
    result = read_acs_state(client, _desired())
    assert dataclasses.is_dataclass(result.observed)
    assert result.observed.acs_present is False
