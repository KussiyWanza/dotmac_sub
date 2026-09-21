"""Tests for ``set_wifi_password`` — first production caller of reconcile_ont.

These verify that:
* the service translates ``ReconcileResult`` into ``ActionResult`` correctly
* the proposed_change carries the new password into desired_state
* failure modes (CR-failed, unreachable, etc.) surface with the right
  ``failure_reason`` in ``ActionResult.data`` so the UI can render them
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.services.network.reconcile import (
    AppliedAction,
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileResult,
)
from app.services.web_network_ont_actions.config_setters import set_wifi_password

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def olt_device(db_session):
    olt = OLTDevice(
        name="OLT-WIFI-TEST",
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
            "wifi": {"ssid": "KURSI", "password": "OLD"},
            "wan": {"pppoe_username": "100024456", "pppoe_password": "x"},
        },
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)
    return ont


def _stub_reconcile_result(*, success: bool, **overrides) -> ReconcileResult:
    """Build a synthetic ``ReconcileResult`` the stubbed reconcile_ont returns."""
    from datetime import UTC, datetime

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
                reason=ReconcileFailureReason.ACS_CR_FAILED,
                message="connection request failed: empty creds",
            )
        ),
        duration_ms=42,
        reconciled_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ReconcileResult(**defaults)


# ── Success path ────────────────────────────────────────────────────────────


def test_set_wifi_password_success_returns_success_action_result(
    db_session, ont, monkeypatch
):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **_):
        captured["ont_unit_id"] = ont_unit_id
        captured["proposed_change"] = proposed_change
        captured["mode"] = mode
        return _stub_reconcile_result(
            success=True,
            actions_applied=(
                AppliedAction(
                    field="wifi_ssid",
                    surface="acs",
                    old_value=None,
                    new_value="KURSI",
                    duration_ms=10,
                ),
            ),
        )

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    result = set_wifi_password(db_session, str(ont.id), "kursimining@98765")

    assert result.success is True
    assert "updated" in result.message.lower()
    assert result.data["sync_status"] == "synced"
    assert "wifi_ssid" in result.data["actions_applied"]
    # Proposed change carried the new password
    assert captured["proposed_change"] == {"wifi_password_ref": "kursimining@98765"}
    assert captured["mode"] == "sync"
    assert captured["ont_unit_id"] == str(ont.id)


# ── timeout_sec propagation ─────────────────────────────────────────────────


def test_set_wifi_password_passes_explicit_timeout_sec_to_reconcile_ont(
    db_session, ont, monkeypatch
):
    """An operator-supplied ``timeout_sec`` must reach ``reconcile_ont`` so a
    slow OLT shelf can be given a longer apply/idle-in-transaction budget
    than the 60s default."""
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_reconcile_result(success=True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    set_wifi_password(db_session, str(ont.id), "newpw", timeout_sec=180)

    assert captured["kwargs"].get("timeout_sec") == 180


def test_set_wifi_password_omits_timeout_sec_when_not_supplied(
    db_session, ont, monkeypatch
):
    """No explicit override means ``reconcile_ont`` keeps using its own
    default (60s) — this change must not alter default behavior."""
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_reconcile_result(success=True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    set_wifi_password(db_session, str(ont.id), "newpw")

    assert "timeout_sec" not in captured["kwargs"]


# ── Failure paths ───────────────────────────────────────────────────────────


def test_set_wifi_password_failure_surfaces_failure_reason(
    db_session, ont, monkeypatch
):
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_reconcile_result(
            success=False,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.OLT_UNREACHABLE,
                message="OLT SPDC unreachable: timed out",
            ),
        ),
    )

    result = set_wifi_password(db_session, str(ont.id), "newpw")

    assert result.success is False
    assert "unreachable" in result.message.lower()
    assert result.data["sync_status"] == "out_of_sync"
    assert result.data["failure_reason"] == ReconcileFailureReason.OLT_UNREACHABLE
    # Not an actionable CR-failed surface
    assert result.data["actionable"] is False


def test_set_wifi_password_cr_failed_marks_actionable(db_session, ont, monkeypatch):
    """ACS_CR_FAILED carries an operator-actionable hint ('drain via OLT
    ont reset'). The service surfaces ``actionable=True`` so the UI can
    render the recovery instructions verbatim."""
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_reconcile_result(
            success=False,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.ACS_CR_FAILED,
                message=(
                    "setParameterValues queued but Connection Request failed: "
                    "empty CR creds. Force OLT `ont reset` to drain."
                ),
            ),
        ),
    )

    result = set_wifi_password(db_session, str(ont.id), "newpw")

    assert result.success is False
    assert result.data["actionable"] is True
    assert "ont reset" in result.message.lower()


def test_set_wifi_password_blocked_out_of_sync_surfaces_explicitly(
    db_session, ont, monkeypatch
):
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_reconcile_result(
            success=False,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.BLOCKED_OUT_OF_SYNC,
                message="ONT is out_of_sync (last_error: prior failure)",
            ),
        ),
    )

    result = set_wifi_password(db_session, str(ont.id), "newpw")

    assert result.success is False
    assert result.data["failure_reason"] == ReconcileFailureReason.BLOCKED_OUT_OF_SYNC
    assert "out_of_sync" in result.message.lower()


# ── Audit ───────────────────────────────────────────────────────────────────


def test_set_wifi_password_logs_audit_for_both_outcomes(db_session, ont, monkeypatch):
    """The audit log entry is created regardless of reconcile outcome — both
    success and failure are operator-visible events."""
    captured: dict = {}

    def _capture_audit(db, *, request=None, action, ont_id, metadata):
        captured.update(metadata)
        captured["action"] = action
        captured["ont_id"] = ont_id

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._log_action_audit",
        _capture_audit,
    )

    # Failure case
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_reconcile_result(success=False),
    )
    set_wifi_password(db_session, str(ont.id), "x")
    assert captured["success"] is False
    assert captured["failure_reason"] == ReconcileFailureReason.ACS_CR_FAILED
    assert captured["action"] == "set_wifi_password"

    captured.clear()

    # Success case
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_reconcile_result(success=True),
    )
    set_wifi_password(db_session, str(ont.id), "y")
    assert captured["success"] is True
    assert captured["sync_status"] == "synced"
    assert captured.get("failure_reason") is None


# ── Sanity: no legacy genieacs_service path is invoked ─────────────────────


def test_set_wifi_password_does_not_call_legacy_genieacs_service(
    db_session, ont, monkeypatch
):
    """Confirm the legacy direct-NBI path has been removed. If the test
    fixture's reconcile_ont stub isn't called, the test fails — meaning a
    fall-back exists. We don't want one."""
    reconcile_called = SimpleNamespace(value=False)

    def _fake_reconcile(*a, **k):
        reconcile_called.value = True
        return _stub_reconcile_result(success=True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    # Also patch the legacy service to RAISE — if anything calls it, the
    # test crashes loudly.
    def _legacy_must_not_run(*a, **k):
        raise AssertionError(
            "Legacy genieacs_service.set_wifi_password was invoked; "
            "reconciler should be the only path"
        )

    monkeypatch.setattr(
        "app.services.genieacs_service.genieacs_service.set_wifi_password",
        _legacy_must_not_run,
    )

    result = set_wifi_password(db_session, str(ont.id), "newpw")
    assert result.success is True
    assert reconcile_called.value is True
