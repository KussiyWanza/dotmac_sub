"""Tests for ``compute_plan``.

These tests construct ``OntDesiredState`` + ``OntObservedState`` instances
directly (no DB) and assert on the emitted action sequence. They cover the
critical scenarios from the design discussion: fresh authorization, fully
synced, single-field changes, OMCI-vs-TR-069 selection, WiFi password mode
gating, and bootstrap mode.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from app.services.network.reconcile import (
    AcsAddObject,
    AcsDeleteObject,
    AcsObservedFields,
    AcsSetDhcpServer,
    AcsSetIpv6,
    AcsSetManagementServer,
    AcsSetNatEnabled,
    AcsSetPppoe,
    AcsSetRemoteAccess,
    AcsSetWanIp,
    AcsSetWifiConfig,
    OltAuthorize,
    OltClearIphost,
    OltCreateServicePort,
    OltDeleteServicePort,
    OltIpconfig,
    OltModifyDescription,
    OltObservedFields,
    OltOmciInternetConfig,
    OltOmciPppoe,
    OltOmciWanConfig,
    OltReset,
    OltTr069ServerConfig,
    OntDesiredState,
    OntObservedState,
    Plan,
    Tr069RemoteAccessParameterPaths,
    Tr181WanParameterPaths,
    compute_plan,
)

# ── Builders ────────────────────────────────────────────────────────────────


def _desired(**overrides) -> OntDesiredState:
    defaults = dict(
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
        # The persisted Tr069CpeDevice.genieacs_device_id for this ONT. The
        # planner never fabricates one; without it (or an ACS-reported id) no
        # ACS action may be emitted at all.
        acs_device_id="00259E-HG8546M-HWTC8535819A",
    )
    defaults.update(overrides)
    return OntDesiredState(**defaults)


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


def _remote_paths() -> Tr069RemoteAccessParameterPaths:
    return Tr069RemoteAccessParameterPaths(
        ssh_enabled="InternetGatewayDevice.X_HW_UserInterface.SSHEnable",
        ssh_port="InternetGatewayDevice.X_HW_UserInterface.SSHPort",
        telnet_enabled="InternetGatewayDevice.X_HW_UserInterface.TelnetEnable",
        telnet_port="InternetGatewayDevice.X_HW_UserInterface.TelnetPort",
    )


def _olt_observed(**overrides) -> OltObservedFields:
    defaults = dict(
        olt_present=False,
        olt_match_state=None,
        olt_run_state=None,
        olt_distance_m=None,
        olt_rx_dbm=None,
        olt_tx_dbm=None,
        olt_temperature_c=None,
        olt_description=None,
        olt_mgmt_ip=None,
        olt_mgmt_vlan=None,
        olt_line_profile_id=None,
        olt_service_profile_id=None,
        olt_service_ports=(),
    )
    defaults.update(overrides)
    return OltObservedFields(**defaults)


def _acs_observed(**overrides) -> AcsObservedFields:
    defaults = dict(
        acs_present=False,
        acs_last_inform_at=None,
        acs_last_boot_at=None,
        acs_last_bootstrap_at=None,
        acs_observed_software_version=None,
        acs_observed_pppoe_username=None,
        acs_observed_pppoe_enable=None,
        acs_observed_wan_vlan=None,
        acs_observed_wan_external_ip=None,
        acs_observed_wan_connection_status=None,
        acs_observed_nat_enabled=None,
        acs_observed_dhcp_enabled=None,
        acs_observed_ssid=None,
        acs_observed_periodic_inform_interval_sec=None,
        acs_observed_cr_username=None,
        acs_observed_cr_username_set=None,
        acs_observed_cr_password_set=None,
        acs_observed_wan_wcd_index=None,
        acs_observed_wan_instance_index=None,
        acs_observed_wan_ppp_locations=(),
    )
    defaults.update(overrides)
    return AcsObservedFields(**defaults)


def _informed_acs_observed(**overrides) -> AcsObservedFields:
    """A device the ACS knows about but that has reported no configuration yet.

    This is the state a brand-new ONT reaches after its first Inform: the ACS
    holds a document (so writes can be delivered) with an ``_id`` we can target,
    but none of the managed parameters are populated.
    """
    defaults = dict(
        acs_present=True,
        acs_observed_device_id="00259E-HG8546M-HWTC8535819A",
        acs_observed_device_match_count=1,
    )
    defaults.update(overrides)
    return _acs_observed(**defaults)


def _observed(
    *, olt: OltObservedFields | None = None, acs: AcsObservedFields | None = None
) -> OntObservedState:
    return OntObservedState(
        last_reconciled_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
        last_reconcile_duration_ms=0,
        mgmt_ip_pingable=False,
        consecutive_sweep_unreachable=0,
        olt=olt or _olt_observed(),
        acs=acs or _acs_observed(),
    )


def _synced_observed(desired: OntDesiredState) -> OntObservedState:
    """Construct an observed state that matches the desired state exactly."""
    return _observed(
        olt=_olt_observed(
            olt_present=True,
            olt_match_state="match",
            olt_run_state="online",
            olt_description=desired.description,
            olt_mgmt_ip=desired.mgmt_ip,
            olt_mgmt_vlan=desired.mgmt_vlan,
            olt_line_profile_id=desired.line_profile_id,
            olt_service_profile_id=desired.service_profile_id,
            olt_service_ports=(
                {
                    "index": desired.mgmt_service_port_index,
                    "vlan": desired.mgmt_vlan,
                    "gem": 2,
                    "state": "up",
                },
                {
                    "index": desired.wan_service_port_index,
                    "vlan": desired.wan_vlan,
                    "gem": desired.wan_gem_index,
                    "state": "up",
                },
            ),
        ),
        acs=_acs_observed(
            acs_present=True,
            acs_observed_device_id=desired.acs_device_id,
            acs_observed_device_match_count=1,
            acs_observed_pppoe_username=desired.wan_pppoe_username,
            acs_observed_pppoe_enable=True,
            acs_observed_wan_vlan=desired.wan_vlan,
            acs_observed_nat_enabled=desired.nat_enabled,
            acs_observed_dhcp_enabled=desired.dhcp_enabled,
            acs_observed_ssid=desired.wifi_ssid,
            acs_observed_wifi_enabled=desired.wifi_enabled,
            acs_observed_wifi_channel=desired.wifi_channel,
            acs_observed_wifi_security_mode=desired.wifi_security_mode,
            acs_observed_periodic_inform_interval_sec=(
                desired.periodic_inform_interval_sec
            ),
            acs_observed_cr_username=desired.cr_username,
            acs_observed_cr_username_set=True,
            acs_observed_cr_password_set=True,
            acs_observed_wan_wcd_index=desired.wan_pppoe_wcd_index,
            acs_observed_wan_instance_index=desired.wan_pppoe_instance_index,
            acs_observed_wan_ppp_locations=(
                (desired.wan_pppoe_wcd_index, desired.wan_pppoe_instance_index),
            ),
        ),
    )


def _types(plan: Plan) -> list[type]:
    return [type(a) for a in plan.actions]


# ── Synced state → empty plan ───────────────────────────────────────────────


def test_synced_ont_produces_empty_plan():
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "sweep")
    assert plan.is_empty
    assert plan.required_surfaces == frozenset()


def test_dual_stack_tr181_emits_ipv6_and_pd_action():
    desired = _desired(ipv6_enabled=True, tr069_data_model_root="Device")
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_data_model_root="Device",
            acs_observed_ipv6_enabled=False,
        ),
    )

    plan = compute_plan(desired, observed, "sweep")

    action = next(item for item in plan.actions if isinstance(item, AcsSetIpv6))
    assert action.enabled is True
    assert action.request_prefixes is True
    assert action.interface_index == 1


def test_dual_stack_tr098_is_visible_unrepairable_drift():
    desired = _desired(
        ipv6_enabled=True,
        tr069_data_model_root="InternetGatewayDevice",
    )
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_data_model_root="InternetGatewayDevice",
            acs_observed_ipv6_enabled=False,
        ),
    )

    plan = compute_plan(desired, observed, "sweep")

    assert not any(isinstance(item, AcsSetIpv6) for item in plan.actions)
    drift = next(item for item in plan.drifts if item.field == "ipv6_enabled")
    assert drift.repairable is False


def test_dhcp_wan_emits_reconciled_wan_ip_action():
    desired = _desired(wan_mode="dhcp", nat_enabled=True)
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_data_model_root="InternetGatewayDevice",
            acs_observed_wan_ip_enable=False,
            acs_observed_wan_addressing_type="Static",
        ),
    )

    plan = compute_plan(desired, observed, "sweep")

    action = next(item for item in plan.actions if isinstance(item, AcsSetWanIp))
    assert action.mode == "dhcp"
    assert action.nat_enabled is True
    assert action.vlan == 203


def test_static_wan_action_carries_addressing_intent():
    desired = _desired(
        wan_mode="static",
        nat_enabled=True,
        wan_static_ip="198.51.100.10",
        wan_static_subnet="255.255.255.248",
        wan_static_gateway="198.51.100.9",
        wan_static_dns="1.1.1.1,8.8.8.8",
    )
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_data_model_root="InternetGatewayDevice",
            acs_observed_wan_ip_enable=True,
            acs_observed_wan_addressing_type="DHCP",
        ),
    )

    action = next(
        item
        for item in compute_plan(desired, observed, "sync").actions
        if isinstance(item, AcsSetWanIp)
    )
    assert action.mode == "static"
    assert action.ip_address == "198.51.100.10"
    assert action.gateway == "198.51.100.9"


def test_static_wan_nat_or_dns_drift_replans_device_write():
    desired = _desired(
        wan_mode="static",
        nat_enabled=False,
        wan_static_ip="160.119.127.194",
        wan_static_subnet="255.255.255.248",
        wan_static_gateway="160.119.127.193",
        wan_static_dns="1.1.1.1,8.8.8.8",
    )
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_observed_wan_ip_enable=True,
            acs_observed_wan_addressing_type="Static",
            acs_observed_wan_vlan=203,
            acs_observed_nat_enabled=True,
            acs_observed_wan_ip_address="160.119.127.194",
            acs_observed_wan_subnet_mask="255.255.255.248",
            acs_observed_wan_gateway="160.119.127.193",
            acs_observed_wan_dns_servers="1.1.1.1,8.8.8.8",
        ),
    )

    assert any(
        isinstance(item, AcsSetWanIp)
        for item in compute_plan(desired, observed, "sweep").actions
    )


def test_tr181_static_missing_projected_dns_remains_drift():
    desired = _desired(
        wan_mode="static",
        nat_enabled=False,
        wan_static_ip="160.119.127.194",
        wan_static_subnet="255.255.255.248",
        wan_static_gateway="160.119.127.193",
        wan_static_dns="1.1.1.1,8.8.8.8",
        tr069_data_model_root="Device",
        tr181_wan_paths=_tr181_wan_paths(),
    )
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_data_model_root="Device",
            acs_observed_wan_ip_enable=True,
            acs_observed_wan_addressing_type="Static",
            acs_observed_wan_vlan=203,
            acs_observed_nat_enabled=False,
            acs_observed_wan_ip_address="160.119.127.194",
            acs_observed_wan_subnet_mask="255.255.255.248",
            acs_observed_wan_gateway="160.119.127.193",
            acs_observed_wan_dns_servers=None,
        ),
    )

    assert any(
        isinstance(item, AcsSetWanIp)
        for item in compute_plan(desired, observed, "sweep").actions
    )


# ── Fresh authorization (TR-069 WAN path) ───────────────────────────────────


def test_fresh_authorize_emits_authorize_servicep_ipconfig_tr069_acs():
    desired = _desired()
    plan = compute_plan(desired, _observed(acs=_informed_acs_observed()), "sync")

    action_types = _types(plan)
    # OLT side: authorize, then service ports, then iphost (clear x2 + write),
    # then tr069 binding, then a trailing reset.
    assert OltAuthorize in action_types
    assert OltCreateServicePort in action_types
    assert OltClearIphost in action_types
    assert OltIpconfig in action_types
    assert OltTr069ServerConfig in action_types
    assert OltReset in action_types

    # ACS side: addObject WANPPPConnection, set PPPoE, set WiFi SSID + password,
    # set NAT, set DHCP, set ManagementServer.
    assert AcsAddObject in action_types
    assert AcsSetPppoe in action_types
    assert AcsSetWifiConfig in action_types
    wifi = next(item for item in plan.actions if isinstance(item, AcsSetWifiConfig))
    assert wifi.ssid == desired.wifi_ssid
    assert wifi.password_ref == desired.wifi_password_ref
    assert AcsSetDhcpServer in action_types
    assert AcsSetManagementServer in action_types
    assert plan.acs_wait_reason is None

    # NAT defensive is emitted when nat_enabled observed != desired. Fresh
    # device has nat=None observed; the diff helper treats None as "no signal"
    # so NAT push is skipped until the device informs.
    assert AcsSetNatEnabled not in action_types


def test_fresh_authorize_absent_from_acs_plans_olt_only_and_waits_for_inform():
    """A brand-new ONT is not in the ACS yet.

    Every NBI write against a device the ACS has no document for is a 404, so
    the plan carries the OLT half only and records an explicit wait. The old
    behaviour pushed the whole ACS sequence at a fabricated device id.
    """
    plan = compute_plan(_desired(), _observed(), "sync")

    action_types = _types(plan)
    assert OltAuthorize in action_types
    assert not any(action.surface == "acs" for action in plan.actions)
    assert plan.required_surfaces == frozenset({"olt"})
    assert plan.waiting_for_acs is True
    assert plan.acs_wait_reason == "ont_not_informing"


def test_sweep_repairs_observed_tr069_profile_drift():
    desired = _desired(tr069_profile_id=5)
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        olt=dataclasses.replace(observed.olt, olt_tr069_profile_id=2),
    )

    plan = compute_plan(desired, observed, "sweep")

    profile_actions = [
        action for action in plan.actions if isinstance(action, OltTr069ServerConfig)
    ]
    assert len(profile_actions) == 1
    assert profile_actions[0].profile_id == 5
    assert any(drift.field == "olt_tr069_profile_id" for drift in plan.drifts)


def test_fresh_authorize_requires_both_surfaces():
    plan = compute_plan(_desired(), _observed(acs=_informed_acs_observed()), "sync")
    assert plan.required_surfaces == frozenset({"olt", "acs"})


def test_fresh_authorize_orders_olt_before_acs():
    plan = compute_plan(_desired(), _observed(acs=_informed_acs_observed()), "sync")
    olt_action_indices = [i for i, a in enumerate(plan.actions) if a.surface == "olt"]
    acs_action_indices = [i for i, a in enumerate(plan.actions) if a.surface == "acs"]
    assert olt_action_indices  # we have OLT actions
    assert acs_action_indices  # we have ACS actions
    # Every OLT action precedes every ACS action.
    assert max(olt_action_indices) < min(acs_action_indices)


def test_fresh_authorize_places_reset_after_olt_actions_before_acs():
    plan = compute_plan(_desired(), _observed(), "sync")
    reset_idx = next(i for i, a in enumerate(plan.actions) if isinstance(a, OltReset))
    # Reset is the last OLT action.
    olt_indices = [i for i, a in enumerate(plan.actions) if a.surface == "olt"]
    assert reset_idx == max(olt_indices)


def test_fresh_authorize_clears_iphost_at_both_indices_before_writing():
    plan = compute_plan(_desired(), _observed(), "sync")
    clear_indices = [
        i for i, a in enumerate(plan.actions) if isinstance(a, OltClearIphost)
    ]
    write_idx = next(
        i for i, a in enumerate(plan.actions) if isinstance(a, OltIpconfig)
    )
    assert len(clear_indices) == 2  # ip_index 0 and 1
    assert all(i < write_idx for i in clear_indices)
    cleared = {plan.actions[i].ip_index for i in clear_indices}
    assert cleared == {0, 1}


def test_fresh_authorize_uses_desired_description():
    plan = compute_plan(
        _desired(description="Kolawole_Idiaro_2_a"), _observed(), "sync"
    )
    authorize = next(a for a in plan.actions if isinstance(a, OltAuthorize))
    assert authorize.description == "Kolawole_Idiaro_2_a"


# ── Bridge mode skips PPPoE + DHCP-related ACS pushes ───────────────────────


def test_bridge_mode_skips_pppoe_and_nat_actions():
    desired = _desired(
        wan_mode="bridge",
        nat_enabled=False,
        wan_vlan=None,
        wan_pppoe_username=None,
        wan_pppoe_password_ref=None,
    )
    plan = compute_plan(desired, _observed(), "sync")
    action_types = _types(plan)
    assert AcsAddObject not in action_types
    assert AcsSetPppoe not in action_types
    assert AcsSetNatEnabled not in action_types


# ── WiFi password — mode gating (Hole 3 resolution) ─────────────────────────


def test_wifi_password_pushed_on_sync_for_fresh_ont():
    plan = compute_plan(_desired(), _observed(acs=_informed_acs_observed()), "sync")
    assert AcsSetWifiConfig in _types(plan)
    assert (
        next(
            item for item in plan.actions if isinstance(item, AcsSetWifiConfig)
        ).password_ref
        == _desired().wifi_password_ref
    )


def test_wifi_password_pushed_on_bootstrap_regardless_of_olt_presence():
    """Bootstrap event → device was wiped; rebuild full config including PSK
    even though the OLT still has the ONT in its table."""
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "bootstrap")
    assert AcsSetWifiConfig in _types(plan)


def test_wifi_password_skipped_on_sweep():
    """Sweeper never pushes the PSK — no observable to confirm drift."""
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "sweep")
    assert AcsSetWifiConfig not in _types(plan)


def test_wifi_password_skipped_on_sync_when_ont_already_present():
    """A no-op sync on a present-and-synced ONT shouldn't re-push the PSK."""
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "sync")
    assert AcsSetWifiConfig not in _types(plan)


def test_wifi_password_pushed_on_operator_password_change():
    """Password is write-only, so an explicit UI change must force one write."""
    desired = _desired(wifi_password_ref="new-pass")
    observed = _synced_observed(_desired(description="old-description"))
    plan = compute_plan(
        desired,
        observed,
        "sync",
        proposed_fields=frozenset({"wifi_password_ref"}),
    )
    assert _types(plan) == [AcsSetWifiConfig]
    assert plan.actions[0].password_ref == "new-pass"
    assert OltModifyDescription not in _types(plan)


def test_wifi_password_change_not_re_emitted_on_verify_plan():
    """Verification omits proposed_fields to avoid an endless write-only drift."""
    desired = _desired(wifi_password_ref="new-pass")
    observed = _synced_observed(dataclasses.replace(desired, description="old"))
    plan = compute_plan(
        desired,
        observed,
        "sync",
        proposed_fields=frozenset({"wifi_password_ref"}),
        force_proposed_writes=False,
    )
    assert AcsSetWifiConfig not in _types(plan)
    assert OltModifyDescription not in _types(plan)


def test_wifi_ssid_change_scopes_out_unrelated_olt_drift():
    desired = _desired(wifi_ssid="NEW_SSID")
    observed = _synced_observed(_desired(description="old"))
    plan = compute_plan(
        desired,
        observed,
        "sync",
        proposed_fields=frozenset({"wifi_ssid"}),
    )
    assert _types(plan) == [AcsSetWifiConfig]
    assert plan.actions[0].ssid == "NEW_SSID"


# ── WiFi SSID — observable, diff-driven ─────────────────────────────────────


def test_wifi_ssid_change_emits_only_ssid_action():
    desired = _desired(wifi_ssid="NEW_SSID")
    observed = _synced_observed(_desired())  # observed reflects OLD ssid
    plan = compute_plan(desired, observed, "sync")
    types = _types(plan)
    assert AcsSetWifiConfig in types
    # SSID-only change shouldn't drag the OLT side along.
    assert OltAuthorize not in types
    assert OltIpconfig not in types


def test_wifi_ssid_match_skips_ssid_action():
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "sync")
    assert AcsSetWifiConfig not in _types(plan)


def test_wifi_fields_are_batched_and_security_is_native_for_tr098():
    desired = _desired(
        wifi_enabled=False,
        wifi_channel=6,
        wifi_security_mode="WPA2-Personal",
        tr069_data_model_root="InternetGatewayDevice",
    )
    observed = _synced_observed(_desired())
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_data_model_root="InternetGatewayDevice",
            acs_observed_wifi_enabled=True,
            acs_observed_wifi_channel=1,
            acs_observed_wifi_security_mode="WPA",
            acs_observed_wifi_instance_index=7,
        ),
    )

    plan = compute_plan(
        desired,
        observed,
        "sync",
        proposed_fields=frozenset(
            {"wifi_enabled", "wifi_channel", "wifi_security_mode"}
        ),
    )

    assert _types(plan) == [AcsSetWifiConfig]
    action = plan.actions[0]
    assert isinstance(action, AcsSetWifiConfig)
    assert action.enabled is False
    assert action.channel == 6
    assert action.security_mode == "11i"
    assert action.ssid is None
    assert action.password_ref is None
    assert ".WLANConfiguration.7." in action.paths.ssid


# ── OMCI vs TR-069 selection ────────────────────────────────────────────────


def test_omci_wan_emits_three_steps_when_profile_id_set():
    desired = _desired(
        wan_pppoe_provisioning_method="omci",
        wan_config_profile_id=2,
        wan_internet_config_ip_index=1,
    )
    plan = compute_plan(desired, _observed(), "sync")
    types = _types(plan)
    assert OltOmciPppoe in types
    assert OltOmciInternetConfig in types
    assert OltOmciWanConfig in types
    # When OMCI owns the WAN, TR-069 PPPoE actions are skipped.
    assert AcsSetPppoe not in types
    assert AcsAddObject not in types


def test_omci_wan_skipped_when_profile_id_is_zero_or_none():
    """``wan_config_profile_id=0`` is a Huawei silent no-op (today's Fix #4).
    Planner falls back to TR-069 in this case."""
    desired = _desired(
        wan_pppoe_provisioning_method="auto",
        wan_config_profile_id=None,
        wan_internet_config_ip_index=1,
    )
    plan = compute_plan(desired, _observed(acs=_informed_acs_observed()), "sync")
    types = _types(plan)
    assert OltOmciPppoe not in types
    assert AcsSetPppoe in types  # TR-069 path


def test_omci_wan_skipped_when_method_is_tr069():
    desired = _desired(
        wan_pppoe_provisioning_method="tr069",
        wan_config_profile_id=2,  # would be a valid OMCI value, but method says tr069
        wan_internet_config_ip_index=1,
    )
    plan = compute_plan(desired, _observed(acs=_informed_acs_observed()), "sync")
    assert OltOmciPppoe not in _types(plan)
    assert AcsSetPppoe in _types(plan)


def test_tr069_wan_wrong_wcd_is_healed_on_desired_wcd_and_stale_child_deleted():
    desired = _desired(wan_pppoe_wcd_index=2, wan_pppoe_instance_index=1)
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_observed_wan_wcd_index=1,
            acs_observed_wan_instance_index=1,
            acs_observed_wan_ppp_locations=((1, 1),),
        ),
    )
    plan = compute_plan(desired, observed, "sweep")
    add = next(a for a in plan.actions if isinstance(a, AcsAddObject))
    ppp = next(a for a in plan.actions if isinstance(a, AcsSetPppoe))
    nat = next(a for a in plan.actions if isinstance(a, AcsSetNatEnabled))
    assert add.object_path.endswith("WANConnectionDevice.2.WANPPPConnection")
    assert ppp.wcd_index == 2
    assert nat.wcd_index == 2
    # The old child is not pruned here because the reader's live PPP values are
    # still sourced from WCD 1, making deletion ambiguous and potentially
    # destructive.
    assert AcsDeleteObject not in _types(plan)


def test_tr069_wan_duplicate_children_keep_primary_target_and_delete_stale_one():
    desired = _desired(wan_pppoe_wcd_index=2, wan_pppoe_instance_index=1)
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_observed_wan_wcd_index=2,
            acs_observed_wan_instance_index=1,
            acs_observed_wan_ppp_locations=((2, 1), (2, 3)),
        ),
    )
    plan = compute_plan(desired, observed, "sweep")
    assert AcsAddObject not in _types(plan)
    ppp = next(a for a in plan.actions if isinstance(a, AcsSetPppoe))
    nat = next(a for a in plan.actions if isinstance(a, AcsSetNatEnabled))
    delete = next(a for a in plan.actions if isinstance(a, AcsDeleteObject))
    assert ppp.wcd_index == 2
    assert ppp.instance_index == 1
    assert nat.instance_index == 1
    assert delete.object_path.endswith("WANConnectionDevice.2.WANPPPConnection.3.")


# ── Service-port repair ─────────────────────────────────────────────────────


def test_stale_service_port_is_deleted():
    desired = _desired(mgmt_service_port_index=23, wan_service_port_index=22)
    olt = _olt_observed(
        olt_present=True,
        olt_match_state="match",
        olt_run_state="online",
        olt_description=desired.description,
        olt_mgmt_ip=desired.mgmt_ip,
        olt_mgmt_vlan=desired.mgmt_vlan,
        olt_line_profile_id=desired.line_profile_id,
        olt_service_profile_id=desired.service_profile_id,
        olt_service_ports=(
            {"index": 22, "vlan": 203, "gem": 1, "state": "up"},
            {"index": 23, "vlan": 201, "gem": 2, "state": "up"},
            {"index": 99, "vlan": 999, "gem": 3, "state": "up"},  # stale
        ),
    )
    plan = compute_plan(
        desired, _observed(olt=olt, acs=_synced_observed(desired).acs), "sync"
    )
    delete_actions = [a for a in plan.actions if isinstance(a, OltDeleteServicePort)]
    assert len(delete_actions) == 1
    assert delete_actions[0].service_port_index == 99


def test_no_action_when_service_ports_match():
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "sync")
    assert OltCreateServicePort not in _types(plan)
    assert OltDeleteServicePort not in _types(plan)


# ── Per-field changes (description, profile bindings) ──────────────────────


def test_description_drift_emits_modify_description():
    desired = _desired(description="NEW_DESC")
    observed = _synced_observed(_desired())  # observed has old desc
    plan = compute_plan(desired, observed, "sync")
    assert OltModifyDescription in _types(plan)


def test_unobserved_description_does_not_trigger_modify():
    """When the OLT reader hasn't populated description (None), the planner
    shouldn't infer drift — it has no signal. Fresh authorize provides desc
    via OltAuthorize."""
    desired = _desired()
    olt = _olt_observed(
        olt_present=True,
        olt_match_state="match",
        olt_run_state="online",
        olt_description=None,  # unobserved
        olt_mgmt_ip=desired.mgmt_ip,
        olt_mgmt_vlan=desired.mgmt_vlan,
        olt_line_profile_id=desired.line_profile_id,
        olt_service_profile_id=desired.service_profile_id,
        olt_service_ports=_synced_observed(desired).olt.olt_service_ports,
    )
    plan = compute_plan(
        desired, _observed(olt=olt, acs=_synced_observed(desired).acs), "sync"
    )
    assert OltModifyDescription not in _types(plan)


# ── ManagementServer — drives CR-cred recovery ──────────────────────────────


def test_missing_cr_credentials_trigger_management_server_push():
    desired = _desired()
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_observed_cr_username="",
            acs_observed_cr_username_set=False,
            acs_observed_cr_password_set=False,
        ),
    )
    plan = compute_plan(desired, observed, "sweep")
    assert AcsSetManagementServer in _types(plan)


def test_mismatched_cr_username_triggers_management_server_push():
    desired = _desired()
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(observed.acs, acs_observed_cr_username="wrong-admin"),
    )
    plan = compute_plan(desired, observed, "sweep")
    assert AcsSetManagementServer in _types(plan)


def test_missing_inform_interval_triggers_management_server_push():
    desired = _desired()
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_observed_periodic_inform_interval_sec=None,
        ),
    )
    plan = compute_plan(desired, observed, "sweep")
    assert AcsSetManagementServer in _types(plan)


def test_present_cr_credentials_skip_management_server_push():
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "sweep")
    assert AcsSetManagementServer not in _types(plan)


def test_acs_endpoint_drift_plans_verified_management_server_update():
    desired = _desired(
        acs_url="https://new-acs.example.net/cwmp",
        acs_username="cwmp-user",
        acs_password_ref="encrypted-cwmp-password",
    )
    observed = dataclasses.replace(
        _synced_observed(desired),
        acs=dataclasses.replace(
            _synced_observed(desired).acs,
            acs_observed_url="https://old-acs.example.net/cwmp",
            acs_observed_username="old-user",
            acs_observed_password_set=True,
        ),
    )

    plan = compute_plan(desired, observed, "sweep")

    action = next(a for a in plan.actions if isinstance(a, AcsSetManagementServer))
    assert action.acs_url == "https://new-acs.example.net/cwmp"
    assert action.acs_username == "cwmp-user"
    assert action.acs_password_ref == "encrypted-cwmp-password"
    assert any(d.field == "acs_management_server" for d in plan.drifts)


def test_explicit_acs_password_rotation_forces_one_write_only_push():
    desired = _desired(
        acs_url="https://acs.example.net/cwmp",
        acs_username="cwmp-user",
        acs_password_ref="rotated-password",
    )
    observed = dataclasses.replace(
        _synced_observed(desired),
        acs=dataclasses.replace(
            _synced_observed(desired).acs,
            acs_observed_url=desired.acs_url,
            acs_observed_username=desired.acs_username,
            acs_observed_password_set=True,
        ),
    )

    apply_phase = compute_plan(
        desired,
        observed,
        "sweep",
        proposed_fields=frozenset({"acs_password_ref"}),
    )
    verify_phase = compute_plan(
        desired,
        observed,
        "sweep",
        proposed_fields=frozenset({"acs_password_ref"}),
        force_proposed_writes=False,
    )

    assert AcsSetManagementServer in _types(apply_phase)
    assert AcsSetManagementServer not in _types(verify_phase)


# ── Plan determinism ────────────────────────────────────────────────────────


def test_plan_is_deterministic():
    desired = _desired()
    observed = _observed()
    plan1 = compute_plan(desired, observed, "sync")
    plan2 = compute_plan(desired, observed, "sync")
    assert plan1.actions == plan2.actions
    assert plan1.required_surfaces == plan2.required_surfaces


# ── Drift records ───────────────────────────────────────────────────────────


def test_drift_records_match_action_count_for_fresh_authorize():
    desired = _desired()
    plan = compute_plan(desired, _observed(acs=_informed_acs_observed()), "sync")
    # Drift entries are emitted alongside repair actions. We expect at least
    # one drift per major repair (authorize, iphost, ssid, pppoe, dhcp).
    drift_fields = {d.field for d in plan.drifts}
    assert "olt_present" in drift_fields
    assert "olt_mgmt_ip" in drift_fields
    assert "wifi_ssid" in drift_fields
    assert "wan_pppoe_username" in drift_fields


# ── Sanity: synced state under bootstrap mode still pushes WiFi PSK ─────────


def test_bootstrap_mode_pushes_wifi_password_on_synced_state():
    desired = _desired()
    plan = compute_plan(desired, _synced_observed(desired), "bootstrap")
    assert AcsSetWifiConfig in _types(plan)
    # ...but nothing else is required, so this should be the only action.
    assert _types(plan) == [AcsSetWifiConfig]


def test_remote_access_enable_is_one_narrow_atomic_action():
    desired = _desired(
        wan_remote_access_enabled=True,
        wan_remote_access_ssh_port=22,
        remote_access_paths=_remote_paths(),
    )
    observed = _synced_observed(desired)
    observed = dataclasses.replace(
        observed,
        acs=dataclasses.replace(
            observed.acs,
            acs_observed_remote_ssh_enabled=False,
            acs_observed_remote_ssh_port=None,
            acs_observed_remote_telnet_enabled=True,
        ),
    )

    plan = compute_plan(
        desired,
        observed,
        "sync",
        proposed_fields=frozenset(
            {
                "wan_remote_access_enabled",
                "wan_remote_access_expires_at",
                "wan_remote_access_source_cidrs",
            }
        ),
    )

    assert _types(plan) == [AcsSetRemoteAccess]
    action = plan.actions[0]
    assert isinstance(action, AcsSetRemoteAccess)
    assert action.ssh_enabled is True
    assert action.ssh_port == 22
    assert action.telnet_enabled is False


def test_remote_access_enable_requires_ssh_telnet_and_port_readback():
    desired = _desired(
        wan_remote_access_enabled=True,
        remote_access_paths=_remote_paths(),
    )
    observed = _synced_observed(desired)

    plan = compute_plan(
        desired,
        observed,
        "sync",
        proposed_fields=frozenset({"wan_remote_access_enabled"}),
        force_proposed_writes=False,
    )

    assert _types(plan) == [AcsSetRemoteAccess]
    assert {drift.field for drift in plan.drifts} == {
        "wan_remote_access_enabled",
        "wan_remote_access_ssh_port",
        "wan_remote_telnet_disabled",
    }
