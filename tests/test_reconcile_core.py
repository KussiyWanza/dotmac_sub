"""Integration tests for ``reconcile_ont``.

These exercise the end-to-end composition: lock + adapters + readers +
planner + applier + persistence. Adapters/clients are stubs so no real I/O,
but everything else runs as in production — the SQLAlchemy session is real,
DB rows are written, sync_status transitions happen on actual ``OntUnit``
rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.models.ont_observation import OntObservation
from app.services.network.reconcile import (
    OntDesiredState,
    ReconcileFailureReason,
    reconcile_ont,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_desired(ont, **overrides) -> OntDesiredState:
    """Build a valid OntDesiredState for the fixture ONT.

    Tests stub ``desired_from_ont_unit`` to return this so they don't depend
    on the OltConfigPack + VLAN model plumbing that ``adapters.py`` reaches
    through. The adapter conversion is exercised in
    ``test_reconcile_adapters.py``.
    """
    defaults = dict(
        ont_unit_id=str(ont.id),
        serial_number=ont.serial_number,
        olt_id=str(ont.olt_device_id),
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
        wifi_password_ref="OLD_PASS",
        wifi_password_pushed_at=None,
        mgmt_service_port_index=23,
        wan_service_port_index=22,
        subscriber_external_id=None,
        wan_uprate_kbps=None,
        wan_downrate_kbps=None,
    )
    defaults.update(overrides)
    return OntDesiredState(**defaults)


@pytest.fixture(autouse=True)
def stub_ping(monkeypatch):
    """Stub the mgmt-IP ping so tests don't shell out to the real ``ping``
    binary (which sits on a 2s timeout per packet and slows the suite
    dramatically). Returns False — tests that need a different outcome
    override this fixture per-test."""
    monkeypatch.setattr(
        "app.services.network.reconcile.core.is_pingable",
        lambda ip, **kwargs: False,
    )


@pytest.fixture
def stub_desired(monkeypatch, ont):
    """Make ``desired_from_ont_unit`` return a known-valid desired state for
    the fixture ONT, so tests focus on reconcile_ont composition rather than
    effective-config plumbing."""
    desired = _make_desired(ont)

    def _fake(db, target_ont):
        return desired

    monkeypatch.setattr(
        "app.services.network.reconcile.core.desired_from_ont_unit", _fake
    )
    return desired


@pytest.fixture
def stub_ont_status(monkeypatch, ont):
    """Default the OLT reader to return a fully synced observation.

    Stubs at the ``read_olt_state`` boundary (not the underlying
    ``get_ont_status``) because the current OLT reader doesn't yet parse
    description / profile-id / service-ports out of ``display ont info``.
    Until that parser lands, ``read_olt_state`` returns those as ``None``,
    which the planner treats as "no signal" — except service-ports, where
    the planner emits a CreateServicePort to materialise the slot. To make
    a 'fully synced' observation in the test, we bypass the reader entirely
    and return the shape the post-parsing reader will eventually emit.
    """
    from app.services.network.reconcile import OltObservedFields
    from app.services.network.reconcile.readers import ReadResult

    def _fake_olt_read(adapter, desired, *, deadline=None):
        return ReadResult(
            success=True,
            unreachable=False,
            observed=OltObservedFields(
                olt_present=True,
                olt_match_state="match",
                olt_run_state="online",
                olt_distance_m=4000,
                olt_rx_dbm=-28.0,
                olt_tx_dbm=2.0,
                olt_temperature_c=40,
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
            error=None,
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.read_olt_state", _fake_olt_read
    )


# ── Stubs ───────────────────────────────────────────────────────────────────


class _StubOltAdapter:
    """OLT adapter stub. Behaves like a healthy OLT with a pre-authorized
    ONT unless configured otherwise."""

    def __init__(
        self,
        *,
        olt: object | None = None,
        present: bool = True,
        find_unreachable: bool = False,
        fail_on: str | None = None,
    ):
        self.olt = olt or SimpleNamespace(name="OLT-STUB")
        self._present = present
        self._find_unreachable = find_unreachable
        self._fail_on = fail_on
        self.calls: list[str] = []

    def _ok(self, method: str):
        self.calls.append(method)
        return SimpleNamespace(
            success=(method != self._fail_on),
            message="ok" if method != self._fail_on else "rejected",
        )

    def find_ont_by_serial(self, serial: str):
        self.calls.append("find_ont_by_serial")
        if self._find_unreachable:
            return SimpleNamespace(
                success=False,
                message="Connection failed: timed out",
                data=None,
            )
        registration = (
            SimpleNamespace(fsp="0/1/3", onu_id=11) if self._present else None
        )
        return SimpleNamespace(
            success=True, message="ok", data={"registration": registration}
        )

    # All other adapter methods just record + succeed (unless fail_on matches).
    def authorize_ont(self, *a, **k):
        return self._ok("authorize_ont")

    def update_ont_profiles(self, *a, **k):
        return self._ok("update_ont_profiles")

    def clear_iphost_config(self, *a, **k):
        return self._ok("clear_iphost_config")

    def configure_iphost(self, *a, **k):
        return self._ok("configure_iphost")

    def bind_tr069_profile(self, *a, **k):
        return self._ok("bind_tr069_profile")

    def create_service_port(self, *a, **k):
        return self._ok("create_service_port")

    def delete_service_port(self, *a, **k):
        return self._ok("delete_service_port")

    def configure_pppoe(self, *a, **k):
        return self._ok("configure_pppoe")

    def configure_internet_config(self, *a, **k):
        return self._ok("configure_internet_config")

    def set_ont_description(self, *a, **k):
        return self._ok("set_ont_description")

    def configure_wan_config(self, *a, **k):
        return self._ok("configure_wan_config")

    def reboot_ont(self, *a, **k):
        return self._ok("reboot_ont")


class _StubAcsClient:
    """ACS NBI client stub. Returns a healthy synced-looking device unless
    configured to be unreachable or to fault."""

    def __init__(
        self,
        *,
        unreachable: bool = False,
        device: dict | None = None,
        spv_raises: Exception | None = None,
        spv_cr_error: str | None = None,
    ):
        self._unreachable = unreachable
        self._device = device
        self._spv_raises = spv_raises
        self._spv_cr_error = spv_cr_error
        self.spv_calls: list[tuple] = []
        self.add_object_calls: list[tuple] = []

    def list_devices(self, query=None, projection=None):
        if self._unreachable:
            from app.services.genieacs_client import GenieACSError

            raise GenieACSError("502 Bad Gateway")
        if self._device is None:
            return []
        return [self._device]

    def add_object(self, device_id, path):
        self.add_object_calls.append((device_id, path))
        return {"_id": "task-addObject"}

    def set_parameter_values(self, device_id, params, **kwargs):
        self.spv_calls.append((device_id, params, kwargs))
        if self._spv_raises is not None:
            raise self._spv_raises
        result: dict = {"_id": "task-spv"}
        if self._spv_cr_error:
            result["connectionRequestError"] = self._spv_cr_error
        return result


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def olt_device(db_session):
    olt = OLTDevice(
        name="OLT-CORE-TEST",
        mgmt_ip="172.20.100.30",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    return olt


@pytest.fixture
def ont(db_session, olt_device):
    """Pre-authorized ONT in a synced-looking state.

    ``desired_config`` carries the WiFi SSID + PSK that callers will mutate,
    plus PPPoE creds, so this ONT can exercise the WiFi-only proposed_change
    path end-to-end."""
    ont = OntUnit(
        serial_number="HWTC8535819A",
        olt_device_id=olt_device.id,
        board="0/1",
        port="3",
        external_id="11",
        is_active=True,
        sync_status=OntSyncStatus.synced,
        desired_config={
            "wifi": {"ssid": "KURSI", "password": "OLD_PASS"},
            "wan": {
                "pppoe_username": "100024456",
                "pppoe_password": "PVgWc3Ch",
            },
            "management": {"ip_address": "172.16.210.20"},
        },
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)
    return ont


def _synced_acs_device(ont) -> dict:
    """A GenieACS device document that matches the fixture ONT's desired
    state — used to set up the 'no drift' baseline."""

    def leaf(v):
        return {"_value": v, "_object": False, "_writable": True}

    return {
        "_id": f"00259E-HG8546M-{ont.serial_number}",
        "_lastInform": "2026-05-13T00:00:00.000Z",
        "InternetGatewayDevice": {
            "DeviceInfo": {"SoftwareVersion": leaf("V5R019C10S100")},
            "ManagementServer": {
                "PeriodicInformInterval": leaf("300"),
                "ConnectionRequestUsername": leaf("admin"),
                "ConnectionRequestPassword": leaf("admin"),
            },
            "WANDevice": {
                "1": {
                    "WANConnectionDevice": {
                        "1": {
                            "WANPPPConnection": {
                                "1": {
                                    "Username": leaf("100024456"),
                                    "Enable": leaf("true"),
                                    "X_HW_VLAN": leaf("203"),
                                    "NATEnabled": leaf("true"),
                                }
                            }
                        }
                    }
                }
            },
            "LANDevice": {
                "1": {
                    "LANHostConfigManagement": {"DHCPServerEnable": leaf("true")},
                    "WLANConfiguration": {"1": {"SSID": leaf("KURSI")}},
                }
            },
        },
    }


# ── Happy paths ─────────────────────────────────────────────────────────────


def test_synced_ont_no_proposed_change_is_a_noop(
    db_session, ont, stub_desired, stub_ont_status
):
    """Reconciling a synced ONT with no proposed_change produces an empty
    plan and a synced result. No adapter writes."""
    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=olt,
        acs_client=acs,
    )
    db_session.flush()

    assert result.success is True
    assert result.sync_status == "synced"
    assert result.actions_applied == ()
    assert result.failure is None
    # Adapter saw the reads (find_ont_by_serial + status fetch) but no writes.
    assert "authorize_ont" not in olt.calls
    assert "configure_iphost" not in olt.calls
    assert acs.spv_calls == []


def test_dual_stack_tr181_reconciles_and_persists_verified_observation(
    db_session, ont, stub_ont_status, monkeypatch
):
    desired = _make_desired(
        ont,
        ipv6_enabled=True,
        tr069_data_model_root="Device",
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.core.desired_from_ont_unit",
        lambda db, target_ont: desired,
    )

    def leaf(value):
        return {"_value": value, "_object": False, "_writable": True}

    device = _synced_acs_device(ont)
    device["Device"] = {
        "DeviceInfo": {"SoftwareVersion": leaf("FW-TR181")},
        "ManagementServer": {
            "PeriodicInformInterval": leaf("300"),
            "ConnectionRequestUsername": leaf("admin"),
            "ConnectionRequestPassword": leaf("admin"),
        },
        "IP": {"Interface": {"1": {"IPv6Enable": leaf("false")}}},
        "DHCPv6": {
            "Client": {
                "1": {
                    "Enable": leaf("false"),
                    "RequestPrefixes": leaf("false"),
                }
            }
        },
        "RouterAdvertisement": {"InterfaceSettings": {"1": {"Enable": leaf("false")}}},
    }

    class _Ipv6Acs(_StubAcsClient):
        def set_parameter_values(self, device_id, params, **kwargs):
            result = super().set_parameter_values(device_id, params, **kwargs)
            if "Device.IP.Interface.1.IPv6Enable" in params:
                self._device["Device"]["IP"]["Interface"]["1"]["IPv6Enable"] = leaf(
                    params["Device.IP.Interface.1.IPv6Enable"]
                )
                self._device["Device"]["DHCPv6"]["Client"]["1"]["Enable"] = leaf(
                    params["Device.DHCPv6.Client.1.Enable"]
                )
                self._device["Device"]["DHCPv6"]["Client"]["1"]["RequestPrefixes"] = (
                    leaf(params["Device.DHCPv6.Client.1.RequestPrefixes"])
                )
                self._device["Device"]["RouterAdvertisement"]["InterfaceSettings"]["1"][
                    "Enable"
                ] = leaf(
                    params["Device.RouterAdvertisement.InterfaceSettings.1.Enable"]
                )
            return result

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=_StubOltAdapter(present=True),
        acs_client=_Ipv6Acs(device=device),
    )

    assert result.success is True
    assert any(action.field == "acs_ipv6_enabled" for action in result.actions_applied)
    observation = db_session.query(OntObservation).filter_by(ont_unit_id=ont.id).one()
    assert observation.acs_data_model_root == "Device"
    assert observation.acs_observed_ipv6_enabled is True
    assert observation.acs_observed_dhcpv6_enabled is True
    assert observation.acs_observed_dhcpv6_request_prefixes is True
    assert observation.acs_observed_ra_enabled is True


def test_wifi_password_change_on_synced_ont_pushes_once(
    db_session, ont, stub_desired, stub_ont_status
):
    """Operator password changes are explicit writes even though PSK is
    write-only. Verification should not re-emit the password write.
    """
    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        proposed_change={"wifi_password_ref": "kursimining@98765"},
        mode="sync",
        olt_adapter=olt,
        acs_client=acs,
    )
    db_session.flush()

    assert result.success is True
    assert result.sync_status == "synced"
    psk_writes = [
        call for call in acs.spv_calls if "PreSharedKey" in next(iter(call[1]))
    ]
    assert len(psk_writes) == 1
    # apply_proposed_change writes the new password into desired_config.
    db_session.refresh(ont)
    assert ont.desired_config["wifi"]["password"] == "kursimining@98765"


def test_bootstrap_mode_pushes_wifi_password_on_synced_ont(
    db_session, ont, stub_desired, stub_ont_status
):
    """BOOTSTRAP event from GenieACS — device was wiped, push full config."""
    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="bootstrap",
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is True
    # Bootstrap pushes WiFi PSK regardless.
    psk_pushes = [
        call for call in acs.spv_calls if "PreSharedKey" in next(iter(call[1]))
    ]
    assert len(psk_pushes) == 1


# ── Mode guard: out_of_sync blocks sync ─────────────────────────────────────


def test_sync_mode_refuses_against_out_of_sync_ont(db_session, ont, stub_desired):
    """An ONT in out_of_sync state blocks sync-mode reconciles. Operator must
    use the force-reconcile (sweep) endpoint to clear it."""
    ont.sync_status = OntSyncStatus.out_of_sync
    ont.last_error = "previous bootstrap failed"
    db_session.commit()

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sync",
        olt_adapter=_StubOltAdapter(),
        acs_client=_StubAcsClient(),
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.BLOCKED_OUT_OF_SYNC
    assert "previous bootstrap failed" in result.failure.message


def test_sweep_mode_proceeds_against_out_of_sync_ont(
    db_session, ont, stub_desired, stub_ont_status
):
    """Sweep mode is how out_of_sync clears — proceed despite the status."""
    ont.sync_status = OntSyncStatus.out_of_sync
    ont.last_error = "previous failure"
    db_session.commit()

    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is True
    assert result.sync_status == "synced"


# ── Failure paths ──────────────────────────────────────────────────────────


def test_ont_not_found_returns_invalid_change(db_session):
    missing = uuid.uuid4()
    result = reconcile_ont(
        db_session,
        missing,
        olt_adapter=_StubOltAdapter(),
        acs_client=_StubAcsClient(),
    )
    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.INVALID_CHANGE
    assert str(missing) in result.failure.message


def test_invalid_proposed_change_returns_invalid_change_without_writes(
    db_session, ont, stub_desired
):
    """Validator rejects (here: changing serial_number)."""
    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        proposed_change={"serial_number": "HWTCDIFFERENT"},
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.INVALID_CHANGE
    assert "serial_number is immutable" in result.failure.message
    # No writes.
    assert acs.spv_calls == []
    assert "authorize_ont" not in olt.calls


def test_olt_unreachable_fast_fails_before_writes(
    db_session, ont, stub_desired, monkeypatch
):
    """OLT unreachable at read time — fast-fail before any apply. Doesn't
    use the ``stub_ont_status`` fixture because we explicitly want the
    reader to report unreachable."""
    from app.services.network.reconcile.readers import ReadResult

    def _fake_olt_unreachable(adapter, desired, *, deadline=None):
        return ReadResult(
            success=False,
            unreachable=True,
            observed=None,
            error="Connection failed: timed out",
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.read_olt_state",
        _fake_olt_unreachable,
    )

    olt = _StubOltAdapter()
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        proposed_change={"wifi_ssid": "NEW_SSID"},
        mode="bootstrap",  # ensures a non-empty plan
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.OLT_UNREACHABLE
    assert "timed out" in result.failure.message.lower()
    # No ACS writes either — fast-fail before any apply.
    assert acs.spv_calls == []


def test_acs_unreachable_fast_fails_before_writes(
    db_session, ont, stub_desired, stub_ont_status
):
    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(unreachable=True)

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="bootstrap",  # ensures ACS-side actions are required
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.ACS_UNREACHABLE
    assert "Bad Gateway" in result.failure.message


def test_apply_failure_marks_ont_out_of_sync(
    db_session, ont, stub_desired, stub_ont_status
):
    """If the applier fails mid-plan, the ONT is marked out_of_sync with the
    failure message, and the row persists."""
    olt = _StubOltAdapter(present=True)
    # WiFi password push will fail because the ACS client raises.
    acs = _StubAcsClient(
        device=_synced_acs_device(ont),
        spv_raises=RuntimeError("ACS exploded"),
    )

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="bootstrap",
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.ACS_WRITE_FAULTED
    db_session.flush()
    status = db_session.execute(
        text("SELECT sync_status, last_error FROM ont_units WHERE id = :id"),
        {"id": str(ont.id)},
    ).one()
    assert status[0] == OntSyncStatus.out_of_sync.value
    assert "ACS exploded" in (status[1] or "")


# ── Status persistence ─────────────────────────────────────────────────────


def test_successful_sync_clears_last_error(
    db_session, ont, stub_desired, stub_ont_status
):
    """A successful reconcile clears any stale last_error on the row."""
    ont.last_error = "ancient failure"
    db_session.commit()

    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=olt,
        acs_client=acs,
    )
    db_session.flush()

    assert result.success is True
    row = db_session.execute(
        text("SELECT last_error, last_reconciled_at FROM ont_units WHERE id = :id"),
        {"id": str(ont.id)},
    ).one()
    assert row[0] is None
    assert row[1] is not None


def test_observation_row_is_upserted(db_session, ont, stub_desired, stub_ont_status):
    """After a successful reconcile, the OntObservation row exists."""
    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=olt,
        acs_client=acs,
    )
    db_session.flush()

    obs = (
        db_session.query(OntObservation)
        .filter(OntObservation.ont_unit_id == ont.id)
        .one()
    )
    assert obs.olt_present is True
    assert obs.acs_present is True
    assert obs.acs_observed_ssid == "KURSI"


def test_consecutive_sweep_unreachable_resets_on_success(
    db_session, ont, stub_desired, stub_ont_status
):
    """A reconcile that successfully reaches both surfaces should reset the
    sweep-unreachable counter (so the alert escalation tracks current
    state, not historical state)."""
    ont.consecutive_sweep_unreachable = 5
    db_session.commit()

    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=olt,
        acs_client=acs,
    )
    db_session.flush()

    row = db_session.execute(
        text("SELECT consecutive_sweep_unreachable FROM ont_units WHERE id = :id"),
        {"id": str(ont.id)},
    ).scalar_one()
    assert row == 0


# ── Crashed-prior recovery ──────────────────────────────────────────────────


def test_sync_refuses_after_crashed_prior_is_detected(db_session, ont, stub_desired):
    """If a prior reconcile crashed (sync_status stuck at reconciling), the
    lock module flips it to out_of_sync. Sync-mode refuses thereafter."""
    ont.sync_status = OntSyncStatus.reconciling
    ont.last_reconcile_started_at = datetime(2026, 5, 1, tzinfo=UTC)
    ont.last_error = None
    db_session.commit()

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sync",
        olt_adapter=_StubOltAdapter(),
        acs_client=_StubAcsClient(),
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.BLOCKED_OUT_OF_SYNC
    # The crash detection message surfaces in last_error.
    assert "did not finalise" in result.failure.message


# ── Verification re-read after apply ────────────────────────────────────────


def test_verification_re_read_passes_when_state_matches_after_apply(
    db_session, ont, stub_desired, stub_ont_status
):
    """Bootstrap reconciles always emit a WiFi PSK action (unobservable, gated
    open in bootstrap mode). After apply, the verify re-read uses the same
    fully-synced stub observation and produces zero drift, so the reconcile
    finalises as synced."""
    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="bootstrap",
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is True
    assert result.sync_status == "synced"
    # actions_applied must be non-empty for this test to exercise the verify
    # path — if it ever becomes empty, the verify short-circuit makes this a
    # weaker test.
    assert len(result.actions_applied) >= 1
    assert result.drift_after == ()


def test_verification_re_read_marks_out_of_sync_when_drift_remains(
    db_session, ont, stub_desired, monkeypatch
):
    """If the post-apply re-read shows drift (e.g. an ACS write claimed
    success but the device snapshot still reports the old value), the
    reconcile must refuse to acknowledge convergence."""
    from app.services.network.reconcile import OltObservedFields
    from app.services.network.reconcile.readers import ReadResult

    call_count = {"n": 0}

    def _drifty_olt_read(adapter, desired, *, deadline=None):
        call_count["n"] += 1
        # Observed description is a non-None stale value that differs from
        # desired — _observed_differs returns True (None-observed would mean
        # "field not read", per planner semantics).
        observed = OltObservedFields(
            olt_present=True,
            olt_match_state="match",
            olt_run_state="online",
            olt_distance_m=4000,
            olt_rx_dbm=-28.0,
            olt_tx_dbm=2.0,
            olt_temperature_c=40,
            olt_description="stale_authd_20251101",  # ← differs from desired
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
        )
        return ReadResult(
            success=True, unreachable=False, observed=observed, error=None
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.read_olt_state", _drifty_olt_read
    )

    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    def _must_not_persist_unverified_intent(*args, **kwargs):
        raise AssertionError("unverified desired state was persisted")

    monkeypatch.setattr(
        "app.services.network.reconcile.core.apply_proposed_change",
        _must_not_persist_unverified_intent,
    )

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="bootstrap",
        proposed_change={"wifi_enabled": False},
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.VERIFICATION_MISMATCH
    assert "description" in result.failure.message.lower()
    # The pre-apply read AND the verify re-read both happened
    assert call_count["n"] == 2
    assert result.drift_after != ()

    db_session.flush()
    db_session.refresh(ont)
    assert ont.sync_status == OntSyncStatus.out_of_sync


def test_verification_re_read_marks_out_of_sync_when_olt_unreachable_post_apply(
    db_session, ont, stub_desired, monkeypatch
):
    """If the post-apply OLT read returns unreachable (network blip
    immediately after the write), we cannot confirm convergence and the
    reconcile must refuse to mark synced even though the writes appeared
    to succeed."""
    from app.services.network.reconcile import OltObservedFields
    from app.services.network.reconcile.readers import ReadResult

    call_count = {"n": 0}

    def _flaky_olt_read(adapter, desired, *, deadline=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Pre-apply: drift on description forces at least one action so
            # the verify path is exercised (no actions ⇒ verify short-circuit).
            return ReadResult(
                success=True,
                unreachable=False,
                observed=OltObservedFields(
                    olt_present=True,
                    olt_match_state="match",
                    olt_run_state="online",
                    olt_distance_m=4000,
                    olt_rx_dbm=-28.0,
                    olt_tx_dbm=2.0,
                    olt_temperature_c=40,
                    olt_description="stale_authd_20251101",
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
                error=None,
            )
        # Verify read: SSH connection dropped
        return ReadResult(
            success=False,
            unreachable=True,
            observed=None,
            error="Connection failed: timed out",
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.read_olt_state", _flaky_olt_read
    )

    olt = _StubOltAdapter(present=True)
    acs = _StubAcsClient(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="bootstrap",
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.OLT_UNREACHABLE
    assert "verification" in result.failure.message.lower()
    assert call_count["n"] == 2


def test_verification_re_read_marks_out_of_sync_when_acs_unreachable_post_apply(
    db_session, ont, stub_desired, stub_ont_status
):
    """Same shape for ACS — post-apply NBI 502 means we cannot verify
    convergence, so refuse to mark synced."""

    class _FlakyAcs(_StubAcsClient):
        def __init__(self, device):
            super().__init__(device=device)
            self.list_calls = 0

        def list_devices(self, query=None, projection=None):
            self.list_calls += 1
            if self.list_calls == 1:
                return [self._device]
            # Verify re-read: GenieACS NBI errors out
            from app.services.genieacs_client import GenieACSError

            raise GenieACSError("502 Bad Gateway")

    olt = _StubOltAdapter(present=True)
    acs = _FlakyAcs(device=_synced_acs_device(ont))

    result = reconcile_ont(
        db_session,
        ont.id,
        mode="bootstrap",
        olt_adapter=olt,
        acs_client=acs,
    )

    assert result.success is False
    assert result.failure.reason == ReconcileFailureReason.ACS_UNREACHABLE
    assert "verification" in result.failure.message.lower()
    assert acs.list_calls == 2


def test_tr069_profile_change_is_olt_only_and_persists_after_readback(
    db_session, ont, stub_desired, monkeypatch
):
    from app.services.network.reconcile import AcsObservedFields, OltObservedFields
    from app.services.network.reconcile.readers import ReadResult

    reads = 0

    def _profile_read(adapter, desired, *, deadline=None):
        nonlocal reads
        reads += 1
        observed_profile = 2 if reads == 1 else desired.tr069_profile_id
        return ReadResult(
            success=True,
            unreachable=False,
            observed=OltObservedFields(
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
                olt_tr069_profile_id=observed_profile,
            ),
            error=None,
        )

    monkeypatch.setattr(
        "app.services.network.reconcile.core.read_olt_state", _profile_read
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.core._resolve_acs_client",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("profile-only reconcile must not require ACS")
        ),
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.core._cached_acs_observation",
        lambda db, target: AcsObservedFields(
            acs_present=True,
            acs_last_inform_at=None,
            acs_last_boot_at=None,
            acs_last_bootstrap_at=None,
            acs_observed_software_version="V5R020",
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
        ),
    )
    adapter = _StubOltAdapter()

    result = reconcile_ont(
        db_session,
        str(ont.id),
        proposed_change={"tr069_profile_id": 5},
        olt_adapter=adapter,
    )

    assert result.success is True
    assert adapter.calls == ["bind_tr069_profile"]
    assert reads == 2
    db_session.refresh(ont)
    assert ont.desired_tr069_profile_id == 5
    observation = db_session.query(OntObservation).one()
    assert observation.olt_tr069_profile_id == 5
    assert observation.acs_present is True
    assert observation.acs_observed_software_version == "V5R020"


# ── PPP refusal is residual drift, not a hard failure ───────────────────────


def _acs_device_missing_pppoe(ont, *, stale_ssid: bool = False) -> dict:
    """A device document identical to the synced baseline except the PPPoE
    username, so the planner emits PPP work that delivery must refuse.

    ``stale_ssid`` additionally diverges the Wi-Fi SSID, so one full-scope pass
    carries both unrelated repair work and PPP work.
    """
    import copy

    device = copy.deepcopy(_synced_acs_device(ont))
    wan = device["InternetGatewayDevice"]["WANDevice"]["1"]["WANConnectionDevice"]["1"]
    wan["WANPPPConnection"]["1"]["Username"]["_value"] = "SOMEONE-ELSES-LOGIN"
    if stale_ssid:
        lan = device["InternetGatewayDevice"]["LANDevice"]["1"]
        lan["WLANConfiguration"]["1"]["SSID"]["_value"] = "STALE-SSID"
    return device


def test_refused_ppp_is_reported_as_out_of_sync_not_synced(
    db_session, ont, stub_desired, stub_ont_status
):
    """The public result must not contradict the row it just wrote.

    The pass succeeds -- nothing was rejected and everything unrelated
    converged -- but PPP was withheld, so both the persisted status and the
    returned status are `out_of_sync`, with no `last_error` because nothing
    failed.
    """
    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=_StubOltAdapter(present=True),
        acs_client=_StubAcsClient(device=_acs_device_missing_pppoe(ont)),
    )

    assert result.success is True
    assert result.failure is None
    assert result.sync_status == "out_of_sync"
    assert ont.sync_status == OntSyncStatus.out_of_sync
    assert result.sync_status == ont.sync_status.value, (
        "returned status must equal the persisted status"
    )
    # Policy-withheld work carries no error: that is what distinguishes it
    # from a hard failure for the mode guard.
    assert ont.last_error is None
    assert any("ppp_delivery" in drift.field for drift in result.drift_after)


class _StatefulAcsClient(_StubAcsClient):
    """An ACS stub that reflects writes back into its device document.

    The plain stub never updates the document, so post-apply verification
    always reports a just-written field as still divergent. That makes it
    impossible to prove CONVERGENCE -- only dispatch. This one applies each
    written parameter to the document, so a second pass can legitimately reach
    `synced` for the fields it wrote.
    """

    def set_parameter_values(self, device_id, params, **kwargs):
        result = super().set_parameter_values(device_id, params, **kwargs)
        # `params` is a {path: value} mapping here; older call shapes pass
        # (path, value) pairs. Handle both rather than silently iterating the
        # characters of a dict key.
        items = (
            params.items()
            if isinstance(params, dict)
            else [(entry[0], entry[1]) for entry in params]
        )
        for path, value in items:
            node = self._device
            keys = str(path).split(".")
            for key in keys[:-1]:
                node = node.setdefault(key, {})
                if not isinstance(node, dict):  # pragma: no cover - defensive
                    return result
            node[keys[-1]] = {
                "_value": value,
                "_object": False,
                "_writable": True,
            }
        return result


def test_a_ppp_refusal_does_not_freeze_ordinary_sync(
    db_session, ont, stub_desired, stub_ont_status
):
    """Second pass: an unrelated change actually CONVERGES while PPP stays
    refused.

    The refusal recurs every pass by design, so blocking sync on it would
    freeze the device permanently. This drives a real Wi-Fi SSID change through
    a stateful ACS stub and asserts the second pass succeeds, that Wi-Fi is
    gone from the residual drift, and that only the refused PPP work keeps the
    ONT out_of_sync.
    """
    first = reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=_StubOltAdapter(present=True),
        acs_client=_StatefulAcsClient(device=_acs_device_missing_pppoe(ont)),
    )
    assert first.sync_status == "out_of_sync"
    assert ont.last_error is None

    # A full-scope sync carrying BOTH unrelated repair work (the Wi-Fi SSID
    # has drifted) and PPP work. A wifi-only proposed_change would scope the
    # plan to Wi-Fi and never exercise the refusal at all.
    acs = _StatefulAcsClient(device=_acs_device_missing_pppoe(ont, stale_ssid=True))
    second = reconcile_ont(
        db_session,
        ont.id,
        mode="sync",
        olt_adapter=_StubOltAdapter(present=True),
        acs_client=acs,
    )

    # The guard opened...
    assert second.failure is None, f"unrelated sync must not fail: {second.failure}"
    assert second.success is True

    # ...the Wi-Fi change converged...
    assert any("KURSI" in str(call) for call in acs.spv_calls), (
        "the drifted Wi-Fi SSID must be repaired"
    )
    assert not any("wifi" in drift.field for drift in second.drift_after), (
        "a converged Wi-Fi change must not remain in residual drift"
    )

    # ...and ONLY the refused PPP work keeps the ONT out of sync.
    assert second.sync_status == "out_of_sync"
    assert second.drift_after, "the refusal must stay visible"
    assert all("ppp_delivery" in drift.field for drift in second.drift_after)
    assert ont.last_error is None, "a policy refusal is not a failure"
    assert not any("WANPPPConnection" in str(call) for call in acs.spv_calls)


def test_an_authorized_ppp_readback_failure_still_hard_fails(
    db_session, ont, stub_desired, stub_ont_status, subscription
):
    """The containment must not soften genuine PPP failures.

    When delivery IS authorized, `residual_ppp_drift` is empty, so a PPP write
    that lands but does not read back stays a real VERIFICATION_MISMATCH with a
    `last_error` -- and therefore still blocks the next sync-mode pass.
    """
    from app.models.catalog import AccessCredential
    from app.models.network import (
        OntAssignment,
        OntWanServiceInstance,
        OntWanServiceLifecycle,
    )
    from app.services.cpe_dialer_credential_reconcile import dialer_fingerprint
    from app.services.credential_crypto import encrypt_credential
    from app.services.network.ont_desired_config import set_desired_config_values

    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscription_id=subscription.id, active=True)
    )
    db_session.add(
        OntWanServiceInstance(
            ont_id=ont.id,
            subscription_id=subscription.id,
            name="internet",
            service_type="internet",
            connection_type="pppoe",
            is_primary=True,
            lifecycle_state=OntWanServiceLifecycle.active,
            is_active=True,
        )
    )
    # Real credential authority: without it the delivery is refused and this
    # test would silently stop exercising the authorized path it exists for.
    username, secret = "100024456", "s3cret-pppoe"
    db_session.add(
        AccessCredential(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            username=username,
            secret_hash=encrypt_credential(secret),
            is_active=True,
        )
    )
    set_desired_config_values(
        ont,
        {
            "delivery.dialer_credential_fingerprint": dialer_fingerprint(
                username, secret
            )
        },
    )
    db_session.add(ont)
    db_session.commit()

    # The stub accepts the write but keeps reporting the stale username, so
    # verification cannot confirm convergence.
    result = reconcile_ont(
        db_session,
        ont.id,
        mode="sweep",
        olt_adapter=_StubOltAdapter(present=True),
        acs_client=_StubAcsClient(device=_acs_device_missing_pppoe(ont)),
    )

    assert result.success is False
    assert result.failure is not None
    # Not a policy refusal: authorization succeeded, so residual_ppp_drift is
    # empty and none of the softening paths apply.
    assert result.drift_after == () or not any(
        "ppp_delivery" in d.field for d in result.drift_after
    )
    assert ont.sync_status == OntSyncStatus.out_of_sync
    # A hard failure records an error, which is what makes it block sync.
    assert ont.last_error is not None
