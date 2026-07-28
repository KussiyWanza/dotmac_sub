"""Behavior tests for the binary device operational owner."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.device_operational_status import (
    NOT_WORKING,
    WORKING,
    annotate_operational_status,
    derive_operational_status,
)


class _Enum:
    def __init__(self, value):
        self.value = value


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _dev(status=None, live=None, enum=True, **overrides):
    def wrap(value):
        if value is None:
            return None
        return _Enum(value) if enum else value

    values = {
        "status": wrap(status),
        "live_status": wrap(live),
        "ping_enabled": False,
        "last_ping_ok": None,
        "last_ping_at": None,
        "snmp_enabled": False,
        "last_snmp_ok": None,
        "last_snmp_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_lifecycle_maintenance_is_not_working_and_non_alarming():
    op = derive_operational_status(_dev("maintenance", "down"), warm_stale=False)

    assert op.status == NOT_WORKING
    assert op.reason == "admin_maintenance"
    assert op.alarming is False
    assert op.verification_failed is False


def test_missing_observation_is_not_working_due_to_uncompleted_verification():
    op = derive_operational_status(_dev("online", None), warm_stale=False)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_not_started"
    assert op.verification_failed is True
    assert op.alarming is False


def test_expired_observation_is_not_working_without_claiming_physical_failure():
    op = derive_operational_status(_dev("online", "up"), warm_stale=True)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_expired"
    assert op.reason_label == "Unable to verify — confirmation expired"
    assert op.alarming is False


def test_unknown_observation_is_not_working_and_inconclusive():
    op = derive_operational_status(_dev("online", "unknown"), warm_stale=False)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_inconclusive"
    assert op.alarming is False


def test_problem_observation_is_working_with_separate_impairment():
    op = derive_operational_status(_dev("online", "problem"), warm_stale=False)

    assert op.status == WORKING
    assert op.reason == "active_trigger"
    assert op.impaired is True
    assert op.alarming is False


def test_negative_observation_is_not_working_and_alarming():
    op = derive_operational_status(_dev("offline", "down"), warm_stale=False)

    assert op.status == NOT_WORKING
    assert op.reason == "observed_not_working"
    assert op.alarming is True


def test_positive_observation_is_working():
    op = derive_operational_status(_dev("online", "up"), warm_stale=False)

    assert op.status == WORKING
    assert op.reason == "observed_working"
    assert op.alarming is False


def test_transition_timestamp_is_not_used_as_observation_freshness():
    device = _dev(
        "online",
        "up",
        live_status_at=NOW,
        ping_enabled=True,
        last_ping_ok=True,
        last_ping_at=NOW - timedelta(hours=1),
    )

    op = derive_operational_status(device, warm_stale=False, now=NOW)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_expired"


def test_old_transition_remains_working_when_native_observation_is_current():
    device = _dev(
        "online",
        "up",
        live_status_at=NOW - timedelta(days=30),
        ping_enabled=True,
        last_ping_ok=True,
        last_ping_at=NOW - timedelta(minutes=2),
    )

    op = derive_operational_status(device, warm_stale=True, now=NOW)

    assert op.status == WORKING
    assert op.reason == "observed_working"


def test_plain_string_attributes_are_supported():
    op = derive_operational_status(_dev("online", "up", enum=False), warm_stale=False)

    assert op.status == WORKING


def test_unrecognized_observation_fails_closed_as_not_working():
    op = derive_operational_status(_dev("online", "weird"), warm_stale=False)

    assert op.status == NOT_WORKING
    assert op.reason == "verification_inconclusive"


def test_mismatch_admin_online_not_working():
    op = derive_operational_status(_dev("online", "down"), warm_stale=False)

    assert op.mismatch is True
    assert op.mismatch_reason == "admin_online_not_working"


def test_mismatch_admin_offline_working():
    op = derive_operational_status(_dev("offline", "up"), warm_stale=False)

    assert op.mismatch is True
    assert op.mismatch_reason == "admin_offline_working"


def test_no_mismatch_when_admin_agrees_with_observation():
    op = derive_operational_status(_dev("online", "up"), warm_stale=False)

    assert op.mismatch is False


def test_annotate_sets_only_binary_public_fields(monkeypatch):
    monkeypatch.setattr(
        "app.services.device_operational_status.warmer_is_stale",
        lambda _now=None: False,
    )
    devices = [
        _dev("online", "up"),
        _dev("offline", "down"),
        SimpleNamespace(),
    ]

    annotate_operational_status(devices)

    assert devices[0].operational.status == WORKING
    assert devices[0].operational_status == WORKING
    assert devices[0].status_presentation.tone.value == "positive"
    assert devices[1].operational.status == NOT_WORKING
    assert devices[2].operational.status == NOT_WORKING
    assert devices[2].operational.verification_failed is True
    assert devices[2].status_presentation.tone.value == "negative"
    assert not hasattr(devices[2], "operational_retry_pending")
