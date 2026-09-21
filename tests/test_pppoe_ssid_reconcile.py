"""Tests for set_wifi_ssid + set_pppoe_credentials routed through reconcile_ont.

Same translation harness as the WiFi password tests; these confirm the
mode and proposed_change shapes specific to SSID + PPPoE.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.services.network.reconcile import (
    AppliedAction,
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileResult,
)
from app.services.web_network_ont_actions.config_setters import (
    set_pppoe_credentials,
    set_wifi_ssid,
)


@pytest.fixture
def olt_device(db_session):
    olt = OLTDevice(
        name="OLT-SSID-PPPOE-TEST",
        mgmt_ip="172.20.100.30",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    db_session.refresh(olt)
    return olt


@pytest.fixture
def ont(db_session, olt_device):
    ont = OntUnit(
        serial_number="HWTC8535819A",
        olt_device_id=olt_device.id,
        board="0/1",
        port="3",
        external_id="11",
        is_active=True,
        sync_status=OntSyncStatus.synced,
        desired_config={
            "wifi": {"ssid": "OLD_SSID"},
            "wan": {
                "pppoe_username": "100024456",
                "pppoe_password": "old_pw",
            },
        },
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)
    return ont


def _stub_result(success: bool, **overrides) -> ReconcileResult:
    defaults = dict(
        success=success,
        sync_status="synced" if success else "out_of_sync",
        actions_applied=(),
        drift_before=(),
        drift_after=(),
        observed_after=None,
        failure=(
            None
            if success
            else ReconcileFailure(
                reason=ReconcileFailureReason.OLT_UNREACHABLE,
                message="OLT unreachable",
            )
        ),
        duration_ms=10,
        reconciled_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ReconcileResult(**defaults)


# ── set_wifi_ssid ──────────────────────────────────────────────────────────


def test_set_wifi_ssid_passes_ssid_in_proposed_change(db_session, ont, monkeypatch):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **_):
        captured["proposed_change"] = proposed_change
        captured["mode"] = mode
        return _stub_result(
            True,
            actions_applied=(
                AppliedAction(
                    field="wifi_ssid",
                    surface="acs",
                    old_value="OLD_SSID",
                    new_value="NEW_SSID",
                    duration_ms=10,
                ),
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    result = set_wifi_ssid(db_session, str(ont.id), "NEW_SSID")

    assert result.success is True
    assert captured["proposed_change"] == {"wifi_ssid": "NEW_SSID"}
    assert captured["mode"] == "sync"
    assert "wifi_ssid" in result.data["actions_applied"]


def test_set_wifi_ssid_failure_surfaces_reason(db_session, ont, monkeypatch):
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_result(
            False,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.ACS_WRITE_FAULTED,
                message="CWMP fault 9002",
            ),
        ),
    )

    result = set_wifi_ssid(db_session, str(ont.id), "X")
    assert result.success is False
    assert result.data["failure_reason"] == ReconcileFailureReason.ACS_WRITE_FAULTED


def test_set_wifi_ssid_passes_explicit_timeout_sec(db_session, ont, monkeypatch):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    set_wifi_ssid(db_session, str(ont.id), "NEW_SSID", timeout_sec=180)

    assert captured["kwargs"].get("timeout_sec") == 180


def test_set_wifi_ssid_omits_timeout_sec_when_not_supplied(
    db_session, ont, monkeypatch
):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    set_wifi_ssid(db_session, str(ont.id), "NEW_SSID")

    assert "timeout_sec" not in captured["kwargs"]


# ── set_pppoe_credentials ──────────────────────────────────────────────────


def test_set_pppoe_passes_credentials_and_vlan_in_proposed_change(
    db_session, ont, monkeypatch
):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **_):
        captured["proposed_change"] = proposed_change
        captured["mode"] = mode
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    result = set_pppoe_credentials(
        db_session,
        str(ont.id),
        username="100099999",
        password="newpw",
        wan_vlan=203,
    )

    assert result.success is True
    assert captured["mode"] == "sync"
    assert captured["proposed_change"]["wan_pppoe_username"] == "100099999"
    assert captured["proposed_change"]["wan_pppoe_password_ref"] == "newpw"
    assert captured["proposed_change"]["wan_vlan"] == 203


def test_set_pppoe_omits_vlan_when_not_specified(db_session, ont, monkeypatch):
    """Caller can omit wan_vlan; the proposed_change shouldn't carry None
    for fields the caller didn't touch — that would set them to None and
    invalidate the desired state."""
    captured: dict = {}

    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda db, ont_unit_id, *, proposed_change, mode, **_: (
            captured.update({"pc": proposed_change}) or _stub_result(True)
        ),
    )

    set_pppoe_credentials(db_session, str(ont.id), username="u", password="p")
    assert "wan_vlan" not in captured["pc"]


def test_set_pppoe_uses_non_default_instance_index(db_session, ont, monkeypatch):
    """Some ONTs put the PPP on WANConnectionDevice.2 (UnitedAbuja case);
    the caller passes instance_index=2 and it flows through."""
    captured: dict = {}

    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda db, ont_unit_id, *, proposed_change, mode, **_: (
            captured.update({"pc": proposed_change}) or _stub_result(True)
        ),
    )

    set_pppoe_credentials(
        db_session, str(ont.id), username="u", password="p", instance_index=2
    )
    assert captured["pc"]["wan_pppoe_instance_index"] == 2


def test_set_pppoe_passes_explicit_timeout_sec(db_session, ont, monkeypatch):
    """An operator-supplied ``timeout_sec`` must reach ``reconcile_ont`` so a
    slow OLT shelf can be given a longer apply/idle-in-transaction budget
    than the 60s default."""
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    set_pppoe_credentials(
        db_session, str(ont.id), username="u", password="p", timeout_sec=180
    )
    assert captured["kwargs"].get("timeout_sec") == 180


def test_set_pppoe_omits_timeout_sec_when_not_supplied(db_session, ont, monkeypatch):
    """No explicit override means ``reconcile_ont`` keeps using its own
    default (60s) — this change must not alter default behavior."""
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    set_pppoe_credentials(db_session, str(ont.id), username="u", password="p")
    assert "timeout_sec" not in captured["kwargs"]


def test_set_pppoe_failure_surfaces_actionable_on_cr_failed(
    db_session, ont, monkeypatch
):
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_result(
            False,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.ACS_CR_FAILED,
                message=(
                    "setParameterValues queued but Connection Request failed; "
                    "drain via OLT `ont reset`."
                ),
            ),
        ),
    )

    result = set_pppoe_credentials(db_session, str(ont.id), username="u", password="p")
    assert result.success is False
    assert result.data["actionable"] is True
