"""Tests for force-push WiFi + force-resync endpoints.

Both share the same ``ReconcileResult → ActionResult`` translation as
``set_wifi_password`` (verified in ``test_wifi_endpoint_reconcile.py``);
these tests confirm the mode-specific behavior and audit-action labels.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.network import OLTDevice, OntSyncStatus, OntUnit
from app.services.network.reconcile import (
    ReconcileFailure,
    ReconcileFailureReason,
    ReconcileResult,
)
from app.services.web_network_ont_actions.config_setters import (
    force_push_wifi_password,
    force_resync_ont,
)


@pytest.fixture
def olt_device(db_session):
    olt = OLTDevice(
        name="OLT-FORCE-TEST",
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
        desired_config={"wifi": {"ssid": "KURSI"}},
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


# ── force_push_wifi_password ────────────────────────────────────────────────


def test_force_push_invokes_reconcile_with_bootstrap_mode(db_session, ont, monkeypatch):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **_):
        captured["proposed_change"] = proposed_change
        captured["mode"] = mode
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    result = force_push_wifi_password(db_session, str(ont.id), "newpw")

    assert result.success is True
    assert captured["mode"] == "bootstrap"
    assert captured["proposed_change"] == {"wifi_password_ref": "newpw"}
    assert "push attempted" in result.message.lower()


def test_force_push_failure_surfaces_with_actionable_for_cr_failed(
    db_session, ont, monkeypatch
):
    """Bootstrap-mode force-push that hits an empty-CR-creds device should
    surface ``actionable=True`` so the UI can render the recovery
    instructions verbatim."""
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_result(
            False,
            failure=ReconcileFailure(
                reason=ReconcileFailureReason.ACS_CR_FAILED,
                message=(
                    "setParameterValues queued but Connection Request failed: "
                    "empty CR creds. Force OLT `ont reset` to drain."
                ),
            ),
        ),
    )

    result = force_push_wifi_password(db_session, str(ont.id), "x")
    assert result.success is False
    assert result.data["actionable"] is True
    assert result.data["failure_reason"] == ReconcileFailureReason.ACS_CR_FAILED


def test_force_push_passes_explicit_timeout_sec_to_reconcile_ont(
    db_session, ont, monkeypatch
):
    """An operator-supplied ``timeout_sec`` must reach ``reconcile_ont`` so a
    slow OLT shelf (e.g. Huawei MA5608T under ``slow_send`` pacing) can be
    given a longer apply/idle-in-transaction budget than the 60s default."""
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    force_push_wifi_password(db_session, str(ont.id), "newpw", timeout_sec=180)

    assert captured["kwargs"].get("timeout_sec") == 180


def test_force_push_omits_timeout_sec_when_not_supplied(db_session, ont, monkeypatch):
    """No explicit override means ``reconcile_ont`` keeps using its own
    default (60s) — this change must not alter default behavior."""
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    force_push_wifi_password(db_session, str(ont.id), "newpw")

    assert "timeout_sec" not in captured["kwargs"]


def test_force_push_audit_action_name(db_session, ont, monkeypatch):
    """The audit log entry uses ``force_push_wifi_password`` (distinct from
    the legacy ``set_wifi_password`` name) so historical filtering can
    distinguish the two paths."""
    captured: dict = {}

    def _capture(db, *, request=None, action, ont_id, metadata):
        captured["action"] = action

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._log_action_audit",
        _capture,
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_result(True),
    )

    force_push_wifi_password(db_session, str(ont.id), "x")
    assert captured["action"] == "force_push_wifi_password"


# ── force_resync_ont ────────────────────────────────────────────────────────


def test_force_resync_invokes_reconcile_with_sweep_mode_no_change(
    db_session, ont, monkeypatch
):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **_):
        captured["proposed_change"] = proposed_change
        captured["mode"] = mode
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    result = force_resync_ont(db_session, str(ont.id))

    assert result.success is True
    assert captured["mode"] == "sweep"
    assert captured["proposed_change"] is None
    assert "reconciled" in result.message.lower()


def test_force_resync_clears_out_of_sync_path(db_session, ont, monkeypatch):
    """force_resync_ont is the operator's escape hatch for ``out_of_sync``.
    Sweep mode proceeds against that state and the result reports the new
    status."""
    ont.sync_status = OntSyncStatus.out_of_sync
    ont.last_error = "prior failure"
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_result(True),
    )

    result = force_resync_ont(db_session, str(ont.id))
    assert result.success is True
    assert result.data["sync_status"] == "synced"


def test_force_resync_passes_explicit_timeout_sec_to_reconcile_ont(
    db_session, ont, monkeypatch
):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    force_resync_ont(db_session, str(ont.id), timeout_sec=180)

    assert captured["kwargs"].get("timeout_sec") == 180


def test_force_resync_omits_timeout_sec_when_not_supplied(db_session, ont, monkeypatch):
    captured: dict = {}

    def _fake_reconcile(db, ont_unit_id, *, proposed_change, mode, **kwargs):
        captured["kwargs"] = kwargs
        return _stub_result(True)

    monkeypatch.setattr("app.services.network.reconcile.reconcile_ont", _fake_reconcile)

    force_resync_ont(db_session, str(ont.id))

    assert "timeout_sec" not in captured["kwargs"]


def test_force_resync_audit_action_name(db_session, ont, monkeypatch):
    captured: dict = {}

    def _capture(db, *, request=None, action, ont_id, metadata):
        captured["action"] = action

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._log_action_audit",
        _capture,
    )
    monkeypatch.setattr(
        "app.services.network.reconcile.reconcile_ont",
        lambda *a, **k: _stub_result(True),
    )

    force_resync_ont(db_session, str(ont.id))
    assert captured["action"] == "force_resync_ont"
