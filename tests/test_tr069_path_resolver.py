"""Tests for the TR-069 deterministic path resolver."""

from __future__ import annotations

import pytest

from app.services.network.tr069_paths import (
    _TR098_PATHS,
    _TR181_PATHS,
    TR069_ROOT_DEVICE,
    TR069_ROOT_IGD,
    Tr069PathError,
    Tr069PathResolver,
    tr069_path_resolver,
)


class TestResolveDevice:
    """TR-181 (Device) path resolution."""

    def test_system_manufacturer(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "system.manufacturer")
        assert path == "Device.DeviceInfo.Manufacturer"

    def test_wan_pppoe_username(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "wan.pppoe_username")
        assert path == "Device.PPP.Interface.1.Username"

    def test_wifi_ssid(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "wifi.ssid")
        assert path == "Device.WiFi.SSID.1.SSID"

    def test_lan_ip(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "lan.ip_address")
        assert path == "Device.IP.Interface.2.IPv4Address.1.IPAddress"

    def test_ping_diagnostic(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "diag.ping.host")
        assert path == "Device.IP.Diagnostics.IPPing.Host"

    def test_mgmt_conn_request(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "mgmt.conn_request_url")
        assert path == "Device.ManagementServer.ConnectionRequestURL"

    def test_optical_signal(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "optical.signal_level")
        assert path == "Device.Optical.Interface.1.OpticalSignalLevel"

    def test_ethernet_port_enable(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "ethernet.port_enable")
        assert path == "Device.Ethernet.Interface.1.Enable"

    def test_mac_address(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "system.mac_address")
        assert path == "Device.Ethernet.Interface.1.MACAddress"


class TestResolveIGD:
    """TR-098 (InternetGatewayDevice) path resolution."""

    def test_system_manufacturer(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_IGD, "system.manufacturer")
        assert path == "InternetGatewayDevice.DeviceInfo.Manufacturer"

    def test_wan_pppoe_username(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_IGD, "wan.pppoe_username")
        assert path == (
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1"
            ".WANPPPConnection.1.Username"
        )

    def test_wifi_ssid(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_IGD, "wifi.ssid")
        assert path == ("InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID")

    def test_wifi_psk_uses_standard_pre_shared_key_value(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_IGD, "wifi.psk")
        assert path == (
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1."
            "PreSharedKey.1.PreSharedKey"
        )

    def test_lan_ip(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_IGD, "lan.ip_address")
        assert path == (
            "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement"
            ".IPInterface.1.IPInterfaceIPAddress"
        )

    def test_ping_diagnostic(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_IGD, "diag.ping.host")
        assert path == "InternetGatewayDevice.IPPingDiagnostics.Host"

    def test_mac_address(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_IGD, "system.mac_address")
        assert path == (
            "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.1.MACAddress"
        )


class TestInstanceIndex:
    """Instance index {i} substitution."""

    def test_wifi_ssid_band_2(self) -> None:
        path = tr069_path_resolver.resolve(
            TR069_ROOT_DEVICE,
            "wifi.ssid",
            instance_index=2,
        )
        assert path == "Device.WiFi.SSID.2.SSID"

    def test_ethernet_port_3(self) -> None:
        path = tr069_path_resolver.resolve(
            TR069_ROOT_DEVICE,
            "ethernet.port_enable",
            instance_index=3,
        )
        assert path == "Device.Ethernet.Interface.3.Enable"

    def test_pppoe_instance_2_igd(self) -> None:
        path = tr069_path_resolver.resolve(
            TR069_ROOT_IGD,
            "wan.pppoe_username",
            instance_index=2,
        )
        assert "WANConnectionDevice.2." in path

    def test_default_instance_is_1(self) -> None:
        path = tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "wan.pppoe_username")
        assert ".Interface.1." in path

    def test_no_placeholder_unaffected(self) -> None:
        """Parameters without {i} are unaffected by instance_index."""
        path_default = tr069_path_resolver.resolve(
            TR069_ROOT_DEVICE,
            "system.manufacturer",
        )
        path_index_5 = tr069_path_resolver.resolve(
            TR069_ROOT_DEVICE,
            "system.manufacturer",
            instance_index=5,
        )
        assert path_default == path_index_5


class TestErrorHandling:
    """Fail-fast on invalid inputs."""

    def test_unknown_root_raises(self) -> None:
        with pytest.raises(Tr069PathError, match="Invalid data model root"):
            tr069_path_resolver.resolve("Unknown", "system.manufacturer")

    def test_none_root_raises(self) -> None:
        with pytest.raises(Tr069PathError, match="Invalid data model root"):
            tr069_path_resolver.resolve(None, "system.manufacturer")  # type: ignore[arg-type]

    def test_unknown_canonical_raises(self) -> None:
        with pytest.raises(Tr069PathError, match="Unknown canonical parameter"):
            tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "foo.bar.baz")

    def test_empty_canonical_raises(self) -> None:
        with pytest.raises(Tr069PathError, match="Unknown canonical parameter"):
            tr069_path_resolver.resolve(TR069_ROOT_DEVICE, "")


class TestResolveManyAndBuildParams:
    """Batch resolution methods."""

    def test_resolve_many(self) -> None:
        result = tr069_path_resolver.resolve_many(
            TR069_ROOT_DEVICE,
            ["system.manufacturer", "wan.pppoe_username"],
        )
        assert len(result) == 2
        assert "system.manufacturer" in result
        assert "wan.pppoe_username" in result
        assert result["system.manufacturer"] == "Device.DeviceInfo.Manufacturer"

    def test_build_params(self) -> None:
        result = tr069_path_resolver.build_params(
            TR069_ROOT_DEVICE,
            {"wan.pppoe_username": "testuser", "wan.pppoe_password": "testpass"},
        )
        assert "Device.PPP.Interface.1.Username" in result
        assert result["Device.PPP.Interface.1.Username"] == "testuser"
        assert "Device.PPP.Interface.1.Password" in result
        assert result["Device.PPP.Interface.1.Password"] == "testpass"

    def test_build_params_with_instance(self) -> None:
        result = tr069_path_resolver.build_params(
            TR069_ROOT_IGD,
            {"wan.pppoe_username": "user1"},
            instance_index=2,
        )
        key = next(iter(result))
        assert "WANConnectionDevice.2." in key


class TestObjectBases:
    """Enumerable object base path resolution."""

    def test_device_ethernet_ports(self) -> None:
        base = tr069_path_resolver.resolve_object_base(
            TR069_ROOT_DEVICE,
            "ethernet_ports",
        )
        assert base == "Device.Ethernet.Interface."

    def test_igd_ethernet_ports(self) -> None:
        base = tr069_path_resolver.resolve_object_base(
            TR069_ROOT_IGD,
            "ethernet_ports",
        )
        assert base == ("InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.")

    def test_device_lan_hosts(self) -> None:
        base = tr069_path_resolver.resolve_object_base(
            TR069_ROOT_DEVICE,
            "lan_hosts",
        )
        assert base == "Device.Hosts.Host."

    def test_igd_lan_hosts(self) -> None:
        base = tr069_path_resolver.resolve_object_base(
            TR069_ROOT_IGD,
            "lan_hosts",
        )
        assert base == "InternetGatewayDevice.LANDevice.1.Hosts.Host."

    def test_unknown_object_type_raises(self) -> None:
        with pytest.raises(Tr069PathError, match="Unknown object type"):
            tr069_path_resolver.resolve_object_base(TR069_ROOT_DEVICE, "invalid")


class TestHasCanonicalAndList:
    """Introspection methods."""

    def test_has_canonical_true(self) -> None:
        assert tr069_path_resolver.has_canonical(TR069_ROOT_DEVICE, "wifi.ssid")

    def test_has_canonical_false(self) -> None:
        assert not tr069_path_resolver.has_canonical(TR069_ROOT_DEVICE, "nonexistent")

    def test_list_canonical_names_device(self) -> None:
        names = tr069_path_resolver.list_canonical_names(TR069_ROOT_DEVICE)
        assert "system.manufacturer" in names
        assert "wifi.ssid" in names
        assert isinstance(names, list)
        assert names == sorted(names)

    def test_list_canonical_names_union(self) -> None:
        names = tr069_path_resolver.list_canonical_names()
        # Union should include both Device-only and IGD-only names
        assert "system.manufacturer" in names


class TestPathConsistency:
    """Cross-standard consistency checks."""

    def test_both_roots_have_same_canonical_coverage_for_common_params(self) -> None:
        """Core params like system.*, mgmt.*, diag.* should exist in both roots."""
        core_params = [
            "system.manufacturer",
            "system.serial_number",
            "system.uptime",
            "mgmt.conn_request_url",
            "diag.ping.host",
            "wan.pppoe_username",
            "wifi.ssid",
            "lan.ip_address",
            "ethernet.port_enable",
        ]
        for param in core_params:
            assert param in _TR181_PATHS, f"{param} missing from TR-181"
            assert param in _TR098_PATHS, f"{param} missing from TR-098"

    def test_all_paths_are_non_empty_strings(self) -> None:
        for name, path in _TR181_PATHS.items():
            assert isinstance(path, str) and path, f"TR-181 '{name}' has empty path"
        for name, path in _TR098_PATHS.items():
            assert isinstance(path, str) and path, f"TR-098 '{name}' has empty path"

    def test_no_paths_start_with_root_prefix(self) -> None:
        """Paths should be suffixes, not full paths."""
        for name, path in _TR181_PATHS.items():
            assert not path.startswith("Device."), (
                f"TR-181 '{name}' should be a suffix, not start with 'Device.'"
            )
        for name, path in _TR098_PATHS.items():
            assert not path.startswith("InternetGatewayDevice."), (
                f"TR-098 '{name}' should be a suffix, not start with 'InternetGatewayDevice.'"
            )


class TestSingleton:
    """Singleton instance check."""

    def test_singleton_is_resolver_instance(self) -> None:
        assert isinstance(tr069_path_resolver, Tr069PathResolver)
