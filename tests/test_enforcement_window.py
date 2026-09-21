"""Unit tests for the shared billing/dunning time-of-day window helper."""

from datetime import UTC, datetime, time

from app.services import enforcement_window as ew

# 2026-01-05 is a Monday; 2026-01-10 is Saturday.
MON_9AM = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
MON_7AM = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)
SAT_NOON = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def test_parse_time_variants():
    assert ew.parse_time("08:00") == time(8, 0)
    assert ew.parse_time("08:30:15") == time(8, 30, 15)
    assert ew.parse_time(" 9:05 ") == time(9, 5)


def test_parse_time_empty_or_invalid_returns_none():
    assert ew.parse_time(None) is None
    assert ew.parse_time("") is None
    assert ew.parse_time("   ") is None
    assert ew.parse_time("not-a-time") is None
    assert ew.parse_time("25:00") is None


def test_no_gate_proceeds():
    assert ew.window_block_reason(MON_9AM) is None


def test_start_only_matches_legacy_blocking_time():
    # before the start hour -> blocked; at/after -> proceed
    assert ew.window_block_reason(MON_7AM, start_time=time(8, 0)) == "before_window"
    assert ew.window_block_reason(MON_9AM, start_time=time(8, 0)) is None
    # exactly at start is allowed (>= semantics)
    assert ew.window_block_reason(MON_9AM, start_time=time(9, 0)) is None


def test_bounded_window():
    start, end = time(9, 0), time(18, 0)
    assert ew.window_block_reason(MON_9AM, start_time=start, end_time=end) is None
    assert (
        ew.window_block_reason(MON_7AM, start_time=start, end_time=end)
        == "outside_window"
    )
    # end is exclusive
    six_pm = datetime(2026, 1, 5, 18, 0, tzinfo=UTC)
    assert (
        ew.window_block_reason(six_pm, start_time=start, end_time=end)
        == "outside_window"
    )


def test_window_wrapping_midnight():
    start, end = time(22, 0), time(6, 0)  # 22:00 -> 06:00
    late = datetime(2026, 1, 5, 23, 0, tzinfo=UTC)
    early = datetime(2026, 1, 5, 3, 0, tzinfo=UTC)
    midday = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    assert ew.window_block_reason(late, start_time=start, end_time=end) is None
    assert ew.window_block_reason(early, start_time=start, end_time=end) is None
    assert (
        ew.window_block_reason(midday, start_time=start, end_time=end)
        == "outside_window"
    )


def test_same_time_window_applies_on_weekends():
    assert ew.window_block_reason(SAT_NOON) is None
    assert ew.window_block_reason(SAT_NOON, start_time=time(13, 0)) == "before_window"


def test_resolve_timezone_name_default(monkeypatch):
    monkeypatch.setattr(ew.settings_spec, "resolve_value", lambda *a, **k: None)
    assert ew.resolve_timezone_name(object()) == "UTC"


def test_to_local_uses_configured_timezone(monkeypatch):
    monkeypatch.setattr(
        ew.settings_spec, "resolve_value", lambda *a, **k: "Africa/Lagos"
    )
    # 09:00 UTC -> 10:00 in Afric/Lagos (UTC+1, no DST)
    local = ew.to_local(object(), MON_9AM)
    assert local.hour == 10
    assert local.utcoffset().total_seconds() == 3600


def test_to_local_bad_timezone_falls_back(monkeypatch):
    monkeypatch.setattr(ew.settings_spec, "resolve_value", lambda *a, **k: "Not/AZone")
    assert ew.to_local(object(), MON_9AM) == MON_9AM


def test_within_send_window_unset_hour_has_no_gate(monkeypatch):
    monkeypatch.setattr(ew.settings_spec, "resolve_value", lambda *a, **k: None)
    assert ew.within_send_window(object(), MON_9AM) is True


def test_within_send_window_malformed_hour_fails_closed(monkeypatch, caplog):
    monkeypatch.setattr(
        ew.settings_spec, "resolve_value", lambda *a, **k: "not-a-number"
    )
    with caplog.at_level("WARNING"):
        result = ew.within_send_window(object(), MON_9AM)
    assert result is False
    assert any(
        "billing_notif_send_hour" in record.getMessage() for record in caplog.records
    )


def test_within_send_window_out_of_range_hour_fails_closed(monkeypatch, caplog):
    monkeypatch.setattr(ew.settings_spec, "resolve_value", lambda *a, **k: "99")
    with caplog.at_level("WARNING"):
        result = ew.within_send_window(object(), MON_9AM)
    assert result is False
    assert any(
        "billing_notif_send_hour" in record.getMessage() for record in caplog.records
    )
