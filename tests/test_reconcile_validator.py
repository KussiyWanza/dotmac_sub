"""Tests for ``validate_desired``.

Each rule from the validator module gets at least one accept-case and one
reject-case test. Reject tests assert on the ``reason`` substring so changes to
the human-readable message don't accidentally break test coverage while still
catching rule-name regressions.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.network.reconcile import (
    OntDesiredState,
    Tr181WanParameterPaths,
    Validation,
    validate_desired,
)


def _tr181_paths() -> Tr181WanParameterPaths:
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


def _base() -> OntDesiredState:
    """A reasonable valid starting state — Kolawole-shaped (SPDC HG8546M)."""
    return OntDesiredState(
        ont_unit_id="ont-1",
        serial_number="HWTC8535819A",
        olt_id="olt-spdc",
        fsp="0/1/3",
        olt_ont_id=11,
        line_profile_id=40,
        service_profile_id=42,
        description="HWTC8535819A_authd_20260513",
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
        cr_password_ref="bao://acs/cr-password",
        periodic_inform_interval_sec=300,
        wan_mode="pppoe",
        wan_vlan=203,
        wan_gem_index=1,
        wan_pppoe_username="100024456",
        wan_pppoe_password_ref="bao://pppoe/100024456",
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
        wifi_password_ref="bao://wifi/ont-1",
        wifi_password_pushed_at=None,
        mgmt_service_port_index=23,
        wan_service_port_index=22,
        subscriber_external_id=None,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
    )


def _assert_rejected(v: Validation, *, reason_contains: str) -> None:
    assert v.ok is False
    assert v.reason is not None
    assert reason_contains.lower() in v.reason.lower(), (
        f"expected reason to contain {reason_contains!r}; got {v.reason!r}"
    )


# ── No-op mutation ──────────────────────────────────────────────────────────


def test_unchanged_state_is_valid():
    """An empty mutation (target == current) is always accepted."""
    state = _base()
    assert validate_desired(state, state).ok is True


# ── Immutable identity ──────────────────────────────────────────────────────


def test_changing_serial_number_is_rejected():
    current = _base()
    target = dataclasses.replace(current, serial_number="HWTC00000000")
    _assert_rejected(validate_desired(target, current), reason_contains="serial_number")


def test_changing_olt_id_is_rejected_with_migrate_hint():
    current = _base()
    target = dataclasses.replace(current, olt_id="olt-other")
    v = validate_desired(target, current)
    _assert_rejected(v, reason_contains="olt_id")
    assert "migrate_ont" in v.reason  # type: ignore[arg-type]


def test_changing_olt_ont_id_is_rejected():
    current = _base()
    target = dataclasses.replace(current, olt_ont_id=12)
    _assert_rejected(validate_desired(target, current), reason_contains="olt_ont_id")


def test_changing_ont_unit_id_is_rejected():
    current = _base()
    target = dataclasses.replace(current, ont_unit_id="ont-2")
    _assert_rejected(validate_desired(target, current), reason_contains="ont_unit_id")


# ── Service-port immutability ───────────────────────────────────────────────


def test_changing_mgmt_service_port_after_allocation_is_rejected():
    current = _base()
    target = dataclasses.replace(current, mgmt_service_port_index=99)
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="mgmt_service_port_index",
    )


def test_changing_wan_service_port_after_allocation_is_rejected():
    current = _base()
    target = dataclasses.replace(current, wan_service_port_index=99)
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="wan_service_port_index",
    )


def test_first_time_allocation_of_service_port_is_accepted():
    """When the index is currently None (pre-allocation), setting it is allowed."""
    current = dataclasses.replace(
        _base(), mgmt_service_port_index=None, wan_service_port_index=None
    )
    target = dataclasses.replace(
        current, mgmt_service_port_index=23, wan_service_port_index=22
    )
    assert validate_desired(target, current).ok is True


# ── Wi-Fi credential validation ─────────────────────────────────────────────


def test_short_raw_wifi_password_is_rejected():
    current = _base()
    target = dataclasses.replace(current, wifi_password_ref="short")

    _assert_rejected(
        validate_desired(target, current),
        reason_contains="WiFi password",
    )


@pytest.mark.parametrize(
    "protected_ref",
    (
        "enc:" + ("a" * 120),
        "bao://network/onts/ont-1#wifi_password",
    ),
)
def test_protected_wifi_password_reference_is_accepted(protected_ref: str):
    current = _base()
    target = dataclasses.replace(current, wifi_password_ref=protected_ref)

    assert validate_desired(target, current).ok is True


# ── Mode contradictions ─────────────────────────────────────────────────────


def test_bridge_mode_with_nat_enabled_is_rejected():
    current = _base()
    target = dataclasses.replace(current, wan_mode="bridge", nat_enabled=True)
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="bridge",
    )


def test_pppoe_without_mgmt_vlan_is_rejected():
    current = _base()
    target = dataclasses.replace(current, mgmt_vlan=None)
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="management VLAN",
    )


def test_omci_method_without_profile_id_is_rejected():
    current = _base()
    target = dataclasses.replace(
        current,
        wan_pppoe_provisioning_method="omci",
        wan_config_profile_id=None,
    )
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="wan_config_profile_id",
    )


def test_omci_method_with_zero_profile_id_is_rejected():
    """profile-id 0 is a Huawei no-op; treat as unset."""
    current = _base()
    target = dataclasses.replace(
        current,
        wan_pppoe_provisioning_method="omci",
        wan_config_profile_id=0,
    )
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="wan_config_profile_id",
    )


def test_omci_method_with_positive_profile_id_is_accepted():
    current = _base()
    target = dataclasses.replace(
        current,
        wan_pppoe_provisioning_method="omci",
        wan_config_profile_id=2,
        wan_internet_config_ip_index=1,
    )
    assert validate_desired(target, current).ok is True


def test_bridge_mode_with_nat_disabled_is_accepted():
    current = _base()
    target = dataclasses.replace(current, wan_mode="bridge", nat_enabled=False)
    assert validate_desired(target, current).ok is True


def test_public_static_wan_rejects_nat():
    current = _base()
    target = dataclasses.replace(
        current,
        wan_mode="static",
        wan_static_ip="160.119.127.194",
        wan_static_subnet="255.255.255.248",
        wan_static_gateway="160.119.127.193",
        wan_static_ip_is_public=True,
        nat_enabled=True,
    )
    _assert_rejected(validate_desired(target, current), reason_contains="public static")


def test_private_static_wan_rejects_nat_disabled():
    current = _base()
    target = dataclasses.replace(
        current,
        wan_mode="static",
        wan_static_ip="10.20.30.2",
        wan_static_subnet="255.255.255.0",
        wan_static_gateway="10.20.30.1",
        wan_static_ip_is_public=False,
        nat_enabled=False,
    )
    _assert_rejected(
        validate_desired(target, current), reason_contains="private static"
    )


def test_tr181_static_wan_requires_resolved_model_paths():
    current = _base()
    target = dataclasses.replace(
        current,
        wan_mode="static",
        wan_static_ip="10.20.30.2",
        wan_static_subnet="255.255.255.0",
        wan_static_gateway="10.20.30.1",
        wan_static_ip_is_public=False,
        nat_enabled=True,
        tr069_data_model_root="Device",
        tr181_wan_paths=None,
    )
    _assert_rejected(
        validate_desired(target, current), reason_contains="vendor/model parameter map"
    )


def test_tr181_dhcp_wan_requires_resolved_model_paths():
    current = _base()
    target = dataclasses.replace(
        current,
        wan_mode="dhcp",
        tr069_data_model_root="Device",
        tr181_wan_paths=None,
    )
    _assert_rejected(
        validate_desired(target, current), reason_contains="vendor/model parameter map"
    )


def test_tr181_static_wan_accepts_complete_model_paths():
    current = _base()
    target = dataclasses.replace(
        current,
        wan_mode="static",
        wan_static_ip="10.20.30.2",
        wan_static_subnet="255.255.255.0",
        wan_static_gateway="10.20.30.1",
        wan_static_ip_is_public=False,
        nat_enabled=True,
        tr069_data_model_root="Device",
        tr181_wan_paths=_tr181_paths(),
    )
    assert validate_desired(target, current).ok is True


# ── VLAN ranges ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_vlan", [0, 4095, -1, 5000])
def test_invalid_mgmt_vlan_is_rejected(bad_vlan):
    current = _base()
    target = dataclasses.replace(current, mgmt_vlan=bad_vlan)
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="mgmt_vlan",
    )


@pytest.mark.parametrize("bad_vlan", [0, 4095])
def test_invalid_wan_vlan_is_rejected(bad_vlan):
    current = _base()
    target = dataclasses.replace(current, wan_vlan=bad_vlan)
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="wan_vlan",
    )


@pytest.mark.parametrize("good_vlan", [1, 100, 201, 4094])
def test_valid_vlans_are_accepted(good_vlan):
    current = _base()
    target = dataclasses.replace(current, mgmt_vlan=good_vlan)
    assert validate_desired(target, current).ok is True


# ── Mgmt subnet membership ──────────────────────────────────────────────────


def test_mgmt_ip_outside_subnet_is_rejected():
    current = _base()
    target = dataclasses.replace(current, mgmt_ip="10.0.0.5")  # outside 172.16.210/24
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="not in network",
    )


def test_mgmt_gateway_outside_subnet_is_rejected():
    current = _base()
    target = dataclasses.replace(current, mgmt_gateway="10.0.0.1")
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="not in network",
    )


def test_mgmt_ip_equals_gateway_is_rejected():
    current = _base()
    target = dataclasses.replace(current, mgmt_ip="172.16.210.1")
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="cannot equal",
    )


def test_mgmt_ip_requires_mask_and_gateway():
    current = _base()
    target = dataclasses.replace(current, mgmt_subnet_mask=None)
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="mgmt_subnet_mask",
    )


def test_invalid_mgmt_ip_string_is_rejected():
    current = _base()
    target = dataclasses.replace(current, mgmt_ip="not.an.ip")
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="invalid",
    )


def test_non_contiguous_mgmt_mask_is_rejected():
    """255.255.0.255 has a hole — never valid."""
    current = _base()
    target = dataclasses.replace(current, mgmt_subnet_mask="255.255.0.255")
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="invalid",
    )


# ── DHCP pool ───────────────────────────────────────────────────────────────


def test_dhcp_pool_max_below_min_is_rejected():
    current = _base()
    target = dataclasses.replace(
        current, dhcp_pool_min="192.168.100.200", dhcp_pool_max="192.168.100.50"
    )
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="dhcp_pool_max must be",
    )


def test_dhcp_pool_outside_subnet_is_rejected():
    current = _base()
    target = dataclasses.replace(current, dhcp_pool_min="10.0.0.2")
    _assert_rejected(
        validate_desired(target, current),
        reason_contains="not in network",
    )


def test_dhcp_pool_check_skipped_when_dhcp_disabled():
    """If DHCP is off (bridge mode), the pool fields aren't enforced."""
    current = _base()
    target = dataclasses.replace(
        current,
        wan_mode="bridge",
        nat_enabled=False,
        dhcp_enabled=False,
        dhcp_pool_min="10.0.0.1",  # nonsense but ignored
        dhcp_pool_max="10.0.0.1",
        dhcp_subnet_mask="255.255.255.0",
    )
    assert validate_desired(target, current).ok is True


# ── Rule ordering ───────────────────────────────────────────────────────────


def test_immutable_identity_rule_runs_before_mode_rules():
    """When both an identity field and a mode field are violated, the identity
    rule fires first — operators get a deterministic, single error per call."""
    current = _base()
    target = dataclasses.replace(
        current,
        olt_id="olt-other",  # immutable violation
        nat_enabled=True,
        wan_mode="bridge",  # mode contradiction
    )
    v = validate_desired(target, current)
    assert v.ok is False
    assert "olt_id" in v.reason  # type: ignore[arg-type]
    assert "bridge" not in v.reason  # type: ignore[arg-type]
