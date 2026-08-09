"""Tests for the DB ↔ in-memory adapters.

Uses the project's ``db_session`` fixture (real SQLAlchemy session against a
fresh sqlite DB per test). Creates minimal ``OltDevice`` / ``OntUnit`` rows
where needed; no fixture sharing across tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.models.network import (
    OLTDevice,
    OntUnit,
    OntWanServiceInstance,
    VendorModelCapability,
    WanConnectionType,
    WanServiceType,
)
from app.models.ont_observation import OntObservation
from app.models.tr069 import Tr069AcsServer
from app.services.control_plane_intent import DesiredValueProvenance
from app.services.credential_crypto import decrypt_credential
from app.services.network.reconcile import (
    AcsObservedFields,
    OltObservedFields,
    OntObservedState,
    apply_proposed_change,
    desired_from_ont_unit,
    observed_from_ont_observation,
    upsert_ont_observation,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def olt(db_session):
    olt = OLTDevice(
        name="OLT-SPDC",
        mgmt_ip="172.20.100.30",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    return olt


@pytest.fixture
def ont(db_session, olt):
    """A SPDC-style ONT with serial, port, and an empty desired_config."""
    ont = OntUnit(
        serial_number="HWTC8535819A",
        olt_device_id=olt.id,
        board="0/1",
        port="3",
        external_id="11",
        is_active=True,
        desired_config={},
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)
    return ont


# ── desired_from_ont_unit ───────────────────────────────────────────────────


def test_desired_carries_identity_from_ont_unit(db_session, ont, olt):
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.ont_unit_id == str(ont.id)
    assert desired.serial_number == "HWTC8535819A"
    assert desired.olt_id == str(olt.id)
    assert desired.fsp == "0/1/3"
    assert desired.olt_ont_id == 11


def test_desired_inherits_acs_assignment_and_interval_from_olt(db_session, ont, olt):
    server = Tr069AcsServer(
        name="Inherited ACS",
        base_url="http://genieacs:7557",
        is_active=True,
        periodic_inform_interval=420,
    )
    db_session.add(server)
    db_session.flush()
    olt.tr069_acs_server_id = server.id
    db_session.commit()

    desired = desired_from_ont_unit(db_session, ont)

    assert desired.acs_server_id == str(server.id)
    assert desired.periodic_inform_interval_sec == 420


def test_desired_builds_fsp_from_board_and_port(db_session, ont):
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.fsp == "0/1/3"


def test_desired_fsp_is_empty_when_board_or_port_missing(db_session, olt):
    ont = OntUnit(
        serial_number="HWTCNOFSPORT",
        olt_device_id=olt.id,
        board=None,
        port=None,
        external_id="0",
        is_active=True,
        desired_config={},
    )
    db_session.add(ont)
    db_session.commit()
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.fsp == ""


def test_desired_pppoe_credentials_round_trip_through_desired_config(db_session, ont):
    ont.pppoe_username = "100024456"
    ont.pppoe_password = "PVgWc3Ch"
    db_session.commit()
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.wan_pppoe_username == "100024456"
    assert desired.wan_pppoe_password_ref == "PVgWc3Ch"


def test_desired_wifi_credentials_round_trip(db_session, ont):
    ont.desired_config = {
        "wifi": {
            "ssid": "KURSI",
            "password": "kursimining@98765",
            "enabled": False,
            "channel": "auto",
            "security_mode": "WPA2-Personal",
        }
    }
    db_session.commit()
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.wifi_ssid == "KURSI"
    assert desired.wifi_password_ref == "kursimining@98765"
    assert desired.wifi_enabled is False
    assert desired.wifi_channel == 0
    assert desired.wifi_security_mode == "WPA2-Personal"
    assert desired.wifi_paths is not None
    assert desired.wifi_paths.ssid.endswith("WLANConfiguration.1.SSID")


def test_desired_remote_access_expires_closed_and_resolves_paths(db_session, ont):
    future = datetime.now(UTC) + timedelta(hours=1)
    ont.desired_config = {
        "access": {
            "wan_remote": True,
            "wan_remote_expires_at": future.isoformat(),
            "wan_remote_source_cidrs": ["10.0.0.0/24"],
        }
    }
    db_session.commit()

    desired = desired_from_ont_unit(db_session, ont)

    assert desired.wan_remote_access_enabled is True
    assert desired.wan_remote_access_expires_at == future
    assert desired.wan_remote_access_source_cidrs == ("10.0.0.0/24",)
    assert desired.remote_access_paths is not None
    assert desired.remote_access_paths.ssh_enabled.endswith(
        "X_HW_UserInterface.SSHEnable"
    )

    access = dict(ont.desired_config["access"])
    access["wan_remote_expires_at"] = (
        datetime.now(UTC) - timedelta(minutes=1)
    ).isoformat()
    ont.desired_config = {"access": access}
    db_session.commit()
    assert desired_from_ont_unit(db_session, ont).wan_remote_access_enabled is False


def test_desired_defaults_dhcp_pool_for_empty_lan_config(db_session, ont):
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.dhcp_pool_min == "192.168.100.2"
    assert desired.dhcp_pool_max == "192.168.100.254"
    assert desired.dhcp_subnet_mask == "255.255.255.0"


def test_desired_wan_mode_normalises_setup_via_onu_to_bridge(db_session, ont):
    ont.desired_config = {"wan": {"onu_mode": "setup_via_onu"}}
    db_session.commit()
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.wan_mode == "bridge"


def test_desired_wan_mode_normalises_bridge_to_bridge(db_session, ont):
    ont.desired_config = {"wan": {"onu_mode": "bridge"}}
    db_session.commit()
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.wan_mode == "bridge"


def test_desired_wan_mode_defaults_to_pppoe_when_unspecified(db_session, ont):
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.wan_mode == "pppoe"


def test_desired_preserves_dhcp_and_static_wan_modes(db_session, ont):
    ont.desired_config = {"wan": {"mode": "dhcp"}}
    db_session.commit()
    assert desired_from_ont_unit(db_session, ont).wan_mode == "dhcp"

    ont.desired_config = {
        "wan": {
            "mode": "static_ip",
            "static_ip": "198.51.100.10",
            "static_subnet": "255.255.255.248",
            "static_gateway": "198.51.100.9",
        }
    }
    db_session.commit()
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.wan_mode == "static"
    assert desired.wan_static_ip == "198.51.100.10"


def test_desired_nat_enabled_follows_wan_mode(db_session, ont):
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.nat_enabled is True  # pppoe → NAT on

    ont.desired_config = {"wan": {"onu_mode": "bridge"}}
    db_session.commit()
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.nat_enabled is False  # bridge → NAT off


@pytest.mark.parametrize(
    ("static_ip", "nat_enabled", "is_public"),
    [
        ("10.20.30.2", True, False),
        ("160.119.127.194", False, True),
    ],
)
def test_desired_static_nat_comes_from_primary_internet_service(
    db_session, ont, static_ip, nat_enabled, is_public
):
    db_session.add(
        VendorModelCapability(
            vendor="Huawei",
            model="EG8145V5",
            tr069_root="Device",
            is_active=True,
        )
    )
    db_session.add(
        OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.internet,
            connection_type=WanConnectionType.static,
            nat_enabled=nat_enabled,
            is_active=True,
            priority=1,
        )
    )
    ont.vendor = "Huawei"
    ont.model = "EG8145V5"
    ont.tr069_data_model = "Device"
    ont.desired_config = {
        "wan": {
            "mode": "static_ip",
            "static_ip": static_ip,
            "static_subnet": "255.255.255.0",
            "static_gateway": "10.20.30.1",
        }
    }
    db_session.commit()

    desired = desired_from_ont_unit(db_session, ont)

    assert desired.nat_enabled is nat_enabled
    assert desired.wan_static_ip_is_public is is_public
    assert desired.tr181_wan_paths is not None
    assert desired.tr181_wan_paths.gateway == (
        "Device.Routing.Router.1.IPv4Forwarding.1.GatewayIPAddress"
    )


def test_desired_dual_stack_is_loaded_from_wan_intent(db_session, ont):
    ont.desired_config = {"wan": {"ip_protocol": "dual_stack"}}
    ont.tr069_data_model = "Device"
    db_session.commit()

    desired = desired_from_ont_unit(db_session, ont)

    assert desired.ipv6_enabled is True
    assert desired.ipv6_enabled_provenance is DesiredValueProvenance.explicit
    assert desired.tr069_data_model_root == "Device"


def test_missing_ipv6_intent_remains_provenance_unknown(db_session, ont):
    desired = desired_from_ont_unit(db_session, ont)

    assert desired.ipv6_enabled is False
    assert desired.ipv6_enabled_provenance is DesiredValueProvenance.unknown


def test_device_root_is_inferred_from_model_capability_before_first_inform(
    db_session, ont
):
    db_session.add(
        VendorModelCapability(
            vendor="Huawei",
            model="EG8145V5",
            tr069_root="Device",
            is_active=True,
        )
    )
    ont.vendor = "Huawei"
    ont.model = "EG8145V5"
    ont.tr069_data_model = None
    ont.desired_config = {"wan": {"mode": "dhcp"}}
    db_session.commit()

    desired = desired_from_ont_unit(db_session, ont)

    assert desired.tr069_data_model_root == "Device"
    assert desired.tr181_wan_paths is not None


def test_desired_description_uses_serial_stub_when_no_subscriber_binding(
    db_session, ont
):
    desired = desired_from_ont_unit(db_session, ont)
    assert desired.serial_number in desired.description
    assert "_authd_" in desired.description


# ── apply_proposed_change ───────────────────────────────────────────────────


def test_apply_writes_wifi_to_desired_config(db_session, ont, monkeypatch):
    """A successful WiFi-change reconcile writes ssid+password to the JSON blob."""
    encryption_key = Fernet.generate_key()
    monkeypatch.setattr(
        "app.services.credential_crypto.get_encryption_key",
        lambda: encryption_key,
    )
    initial = desired_from_ont_unit(db_session, ont)
    import dataclasses

    target = dataclasses.replace(
        initial, wifi_ssid="NEW_SSID", wifi_password_ref="new-pass"
    )

    apply_proposed_change(ont, target)
    db_session.commit()
    db_session.refresh(ont)

    assert ont.desired_config["wifi"]["ssid"] == "NEW_SSID"
    stored_password = ont.desired_config["wifi"]["password"]
    assert stored_password.startswith("enc:")
    assert decrypt_credential(stored_password) == "new-pass"
    assert "ip_protocol" not in ont.desired_config.get("wan", {})


def test_apply_persists_dual_stack_intent(db_session, ont):
    import dataclasses

    target = dataclasses.replace(
        desired_from_ont_unit(db_session, ont), ipv6_enabled=True
    )
    apply_proposed_change(
        ont,
        target,
        changed_fields=frozenset({"ipv6_enabled"}),
    )

    assert ont.desired_config["wan"]["ip_protocol"] == "dual_stack"


def test_apply_writes_pppoe_via_model_accessor(db_session, ont):
    initial = desired_from_ont_unit(db_session, ont)
    import dataclasses

    target = dataclasses.replace(
        initial,
        wan_pppoe_username="100099999",
        wan_pppoe_password_ref="newpass",
    )
    apply_proposed_change(ont, target)
    db_session.commit()
    db_session.refresh(ont)

    assert ont.pppoe_username == "100099999"
    assert ont.pppoe_password == "newpass"


def test_apply_scoped_wan_change_preserves_unrelated_desired_sections(db_session, ont):
    import copy
    import dataclasses

    ont.desired_config = {
        "wan": {
            "pppoe_username": "100000000",
            "pppoe_password": "existing-ref",
        },
        "wifi": {"ssid": "KEEP_WIFI", "password": "keep-wifi-ref"},
        "lan": {"dhcp_enabled": False, "dhcp_start": "192.168.2.10"},
        "management": {"subnet": "255.255.255.0", "gateway": "172.16.1.1"},
    }
    db_session.commit()
    before = copy.deepcopy(ont.desired_config)
    target = dataclasses.replace(
        desired_from_ont_unit(db_session, ont),
        wan_pppoe_username="100099999",
    )

    apply_proposed_change(
        ont,
        target,
        changed_fields=frozenset({"wan_pppoe_username"}),
    )

    assert ont.pppoe_username == "100099999"
    assert ont.pppoe_password == "existing-ref"
    assert ont.desired_config["wifi"] == before["wifi"]
    assert ont.desired_config["lan"] == before["lan"]
    assert ont.desired_config["management"] == before["management"]


def test_apply_empty_value_clears_section_key(db_session, ont):
    """Setting a field to '' removes the key from desired_config — matches the
    model's existing _set_desired_value semantics."""
    ont.desired_config = {"wifi": {"ssid": "OLD", "password": "old"}}
    db_session.commit()
    initial = desired_from_ont_unit(db_session, ont)

    import dataclasses

    target = dataclasses.replace(initial, wifi_ssid="", wifi_password_ref="")
    apply_proposed_change(ont, target)
    db_session.commit()
    db_session.refresh(ont)

    assert "wifi" not in (ont.desired_config or {})


# ── observed_from_ont_observation ───────────────────────────────────────────


def test_observed_returns_none_when_no_observation_row(db_session, ont):
    assert observed_from_ont_observation(None) is None


def test_observed_round_trips_olt_and_acs_fields(db_session, ont):
    """Write a full observation, read it back through the adapter, assert the
    in-memory shape matches what we put in."""
    observed = OntObservedState(
        last_reconciled_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
        last_reconcile_duration_ms=8421,
        mgmt_ip_pingable=True,
        consecutive_sweep_unreachable=0,
        olt=OltObservedFields(
            olt_present=True,
            olt_match_state="match",
            olt_run_state="online",
            olt_distance_m=4374,
            olt_rx_dbm=-28.54,
            olt_tx_dbm=2.29,
            olt_temperature_c=40,
            olt_description="Kolawole_Idiaro_2_zone_Zone_1_authd_20260512",
            olt_mgmt_ip="172.16.210.20",
            olt_mgmt_vlan=201,
            olt_line_profile_id=40,
            olt_service_profile_id=42,
            olt_service_ports=(
                {"index": 22, "vlan": 203, "gem": 1, "state": "up"},
                {"index": 23, "vlan": 201, "gem": 2, "state": "up"},
            ),
        ),
        acs=AcsObservedFields(
            acs_present=True,
            acs_last_inform_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
            acs_last_boot_at=None,
            acs_last_bootstrap_at=None,
            acs_observed_software_version="V5R019C10S100",
            acs_observed_pppoe_username="100024456",
            acs_observed_pppoe_enable=True,
            acs_observed_wan_vlan=203,
            acs_observed_wan_external_ip=None,
            acs_observed_wan_connection_status=None,
            acs_observed_nat_enabled=True,
            acs_observed_dhcp_enabled=True,
            acs_observed_ssid="KURSI",
            acs_observed_wifi_enabled=True,
            acs_observed_wifi_channel=6,
            acs_observed_wifi_security_mode="11i",
            acs_observed_wifi_instance_index=7,
            acs_observed_remote_ssh_enabled=True,
            acs_observed_remote_ssh_port=22,
            acs_observed_remote_telnet_enabled=False,
            acs_observed_remote_telnet_port=23,
            acs_observed_periodic_inform_interval_sec=300,
            acs_observed_cr_username="admin",
            acs_observed_cr_username_set=True,
            acs_observed_cr_password_set=True,
            acs_observed_wan_wcd_index=1,
            acs_observed_wan_instance_index=1,
            acs_observed_wan_ppp_locations=((1, 1),),
        ),
    )
    upsert_ont_observation(db_session, ont.id, observed)
    db_session.commit()

    row = db_session.get(OntObservation, _only_obs_id(db_session, ont.id))
    materialised = observed_from_ont_observation(row)

    assert materialised is not None
    assert materialised.olt.olt_present is True
    assert materialised.olt.olt_description.startswith("Kolawole_Idiaro_2")
    assert materialised.olt.olt_service_ports[0]["vlan"] == 203
    assert materialised.acs.acs_observed_ssid == "KURSI"
    assert materialised.acs.acs_observed_wifi_enabled is True
    assert materialised.acs.acs_observed_wifi_channel == 6
    assert materialised.acs.acs_observed_wifi_security_mode == "11i"
    assert materialised.acs.acs_observed_wifi_instance_index == 7
    assert materialised.acs.acs_observed_remote_ssh_enabled is True
    assert materialised.acs.acs_observed_remote_ssh_port == 22
    assert materialised.acs.acs_observed_remote_telnet_enabled is False
    assert materialised.acs.acs_observed_remote_telnet_port == 23
    assert materialised.acs.acs_observed_software_version == "V5R019C10S100"


# ── upsert_ont_observation ──────────────────────────────────────────────────


def _minimal_observed(*, ssid: str = "KURSI") -> OntObservedState:
    return OntObservedState(
        last_reconciled_at=datetime(2026, 5, 13, 0, 0, tzinfo=UTC),
        last_reconcile_duration_ms=100,
        mgmt_ip_pingable=True,
        consecutive_sweep_unreachable=0,
        olt=OltObservedFields(
            olt_present=True,
            olt_match_state="match",
            olt_run_state="online",
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
        ),
        acs=AcsObservedFields(
            acs_present=True,
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
            acs_observed_ssid=ssid,
            acs_observed_periodic_inform_interval_sec=None,
            acs_observed_cr_username=None,
            acs_observed_cr_username_set=None,
            acs_observed_cr_password_set=None,
            acs_observed_wan_wcd_index=None,
            acs_observed_wan_instance_index=None,
            acs_observed_wan_ppp_locations=(),
        ),
    )


def test_upsert_creates_a_row_on_first_call(db_session, ont):
    upsert_ont_observation(db_session, ont.id, _minimal_observed())
    db_session.commit()
    obs = (
        db_session.query(OntObservation)
        .filter(OntObservation.ont_unit_id == ont.id)
        .one()
    )
    assert obs.acs_observed_ssid == "KURSI"


def test_upsert_updates_existing_row_on_subsequent_call(db_session, ont):
    upsert_ont_observation(db_session, ont.id, _minimal_observed(ssid="OLD"))
    db_session.commit()
    upsert_ont_observation(db_session, ont.id, _minimal_observed(ssid="NEW"))
    db_session.commit()

    rows = (
        db_session.query(OntObservation)
        .filter(OntObservation.ont_unit_id == ont.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].acs_observed_ssid == "NEW"


def test_upsert_accepts_string_ont_unit_id(db_session, ont):
    """The reconcile loop may pass str(ont.id); the adapter coerces."""
    upsert_ont_observation(db_session, str(ont.id), _minimal_observed())
    db_session.commit()
    obs = (
        db_session.query(OntObservation)
        .filter(OntObservation.ont_unit_id == ont.id)
        .one()
    )
    assert obs.olt_present is True


def test_observation_cascade_deletes_with_ont_unit(db_session, ont):
    upsert_ont_observation(db_session, ont.id, _minimal_observed())
    db_session.commit()
    ont_id = ont.id

    db_session.delete(ont)
    db_session.commit()

    remaining = (
        db_session.query(OntObservation)
        .filter(OntObservation.ont_unit_id == ont_id)
        .all()
    )
    assert remaining == []


# ── Internal helpers ────────────────────────────────────────────────────────


def _only_obs_id(db_session, ont_unit_id: uuid.UUID):
    """Return the (only) OntObservation row's id for a given ONT."""
    row = (
        db_session.query(OntObservation)
        .filter(OntObservation.ont_unit_id == ont_unit_id)
        .one()
    )
    return row.id
