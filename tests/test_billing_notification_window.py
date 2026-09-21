"""Tests for the send-window gating of billing/dunning notifications (phase 2)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.models.domain_settings import SettingDomain
from app.services import billing_automation
from app.services import enforcement_window as ew


def _patch_settings(monkeypatch, *, send_hour=None, timezone="UTC", hourly=False):
    def fake_resolve(db, domain, key):
        if domain == SettingDomain.scheduler and key == "timezone":
            return timezone
        if domain == SettingDomain.collections and key == "billing_notif_send_hour":
            return send_hour
        if (
            domain == SettingDomain.collections
            and key == "billing_notifications_hourly_enabled"
        ):
            return hourly
        return None

    monkeypatch.setattr(ew.settings_spec, "resolve_value", fake_resolve)
    return fake_resolve


def test_within_send_window_unset_hour_is_open(monkeypatch):
    _patch_settings(monkeypatch, send_hour=None)
    assert ew.within_send_window(object(), datetime(2026, 1, 5, 3, 0, tzinfo=UTC))


def test_within_send_window_only_during_configured_hour(monkeypatch):
    _patch_settings(monkeypatch, send_hour=8, timezone="UTC")
    assert ew.within_send_window(object(), datetime(2026, 1, 5, 8, 30, tzinfo=UTC))
    assert not ew.within_send_window(object(), datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
    assert not ew.within_send_window(object(), datetime(2026, 1, 5, 7, 59, tzinfo=UTC))


def test_within_send_window_respects_timezone(monkeypatch):
    # send_hour 8 local (Africa/Lagos = UTC+1) => 07:00-07:59 UTC
    _patch_settings(monkeypatch, send_hour=8, timezone="Africa/Lagos")
    assert ew.within_send_window(object(), datetime(2026, 1, 5, 7, 30, tzinfo=UTC))
    assert not ew.within_send_window(object(), datetime(2026, 1, 5, 8, 30, tzinfo=UTC))


def test_within_send_window_invalid_hour_fails_closed(monkeypatch):
    _patch_settings(monkeypatch, send_hour="nope")
    assert not ew.within_send_window(object(), datetime(2026, 1, 5, 3, 0, tzinfo=UTC))
    _patch_settings(monkeypatch, send_hour=99)
    assert not ew.within_send_window(object(), datetime(2026, 1, 5, 3, 0, tzinfo=UTC))


def test_run_billing_notifications_skips_outside_window(monkeypatch):
    monkeypatch.setattr(
        billing_automation.enforcement_window,
        "within_send_window",
        lambda db, now: False,
    )
    # Emits must NOT be called when outside the window.
    monkeypatch.setattr(
        billing_automation,
        "_emit_invoice_reminders",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not emit")),
    )
    db = MagicMock()
    result = billing_automation.run_billing_notifications(
        db, datetime(2026, 1, 5, 3, tzinfo=UTC)
    )
    assert result == {
        "invoice_reminders_sent": 0,
        "skipped_outside_window": True,
    }
    db.commit.assert_not_called()


def test_run_billing_notifications_emits_inside_window(monkeypatch):
    monkeypatch.setattr(
        billing_automation.enforcement_window,
        "within_send_window",
        lambda db, now: True,
    )
    monkeypatch.setattr(
        billing_automation, "_emit_invoice_reminders", lambda db, run_at: 3
    )
    db = MagicMock()
    result = billing_automation.run_billing_notifications(
        db, datetime(2026, 1, 5, 8, tzinfo=UTC)
    )
    assert result == {
        "invoice_reminders_sent": 3,
        "skipped_outside_window": False,
    }
    db.commit.assert_called_once()


def test_daily_invoice_cycle_leaves_notifications_to_permanent_hourly_owner(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        billing_automation,
        "_emit_invoice_reminders",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("raw emit")),
    )
    monkeypatch.setattr(
        billing_automation,
        "run_billing_notifications",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("inline fallback")),
    )

    run_at = datetime(2026, 1, 5, 3, tzinfo=UTC)
    summary = billing_automation.run_invoice_cycle(db_session, run_at=run_at)

    assert summary["invoice_reminders_sent"] == 0
