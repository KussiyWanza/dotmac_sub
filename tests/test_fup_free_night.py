"""The free overnight window, and a throttle proportional to the plan.

PLAN_FAMILY_ARCHITECTURE §1 defines the FUP shape as a daily bucket, a throttle
to 50% of the plan's own rate, and a free night from 22:00 to 05:00. Three
defects stood between that design and the engine, and each has a test here:

1. ``speed_reduction_percent`` was written and never read — every throttled
   subscriber landed on one global 1 Mbps profile regardless of plan.
2. Time-of-day windows were compared against a UTC instant, so a Lagos
   customer's 22:00 window opened at 23:00.
3. The accounting window was applied to nothing, so traffic during a "free"
   night still consumed the next day's bucket.

The release half of (2) — a throttle that lifts when its rule leaves its
window — is covered in ``test_fup_free_night_release.py`` where a DB-backed
enforcement sweep is available.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.services.fup import _day_in_list, _time_in_window
from app.services.fup_throttle_profile import (
    MIN_THROTTLE_KBPS,
    FupThrottleRateError,
    reduced_kbps,
    resolve_or_create_profile,
)
from app.services.fup_usage import accrual_intervals, fup_window_bounds

LAGOS = ZoneInfo("Africa/Lagos")  # UTC+1, no DST
NIGHT_START = time(22, 0)
NIGHT_END = time(5, 0)
DAY_START = time(5, 0)
DAY_END = time(22, 0)


# --- 1. the throttle is a percentage of the plan, not a fixed rate -----------


def test_fifty_percent_halves_the_rate():
    assert reduced_kbps(50_000, 50) == 25_000
    assert reduced_kbps(6_000, 50) == 3_000


def test_the_same_percentage_bites_proportionally_across_tiers():
    """The defect this replaces: one global 1 Mbps profile for every plan.

    A 50 Mbps plan and a 6 Mbps plan given the same reduction must end up at
    rates in the same ratio as they started, not at the same absolute rate.
    """
    premium = reduced_kbps(50_000, 50)
    starter = reduced_kbps(6_000, 50)
    assert premium / starter == 50_000 / 6_000


def test_reduction_is_a_cut_not_a_target():
    """90 means "cut by 90%", leaving a tenth — not "reduce to 90%"."""
    assert reduced_kbps(10_000, 90) == 1_000


def test_a_deep_cut_on_a_slow_plan_is_floored_not_honoured_exactly():
    # 95% of 2 Mbps would be 100 kbps, which is not a service.
    assert reduced_kbps(2_000, 95) == MIN_THROTTLE_KBPS


@pytest.mark.parametrize("pct", [0, 100, -5, 140])
def test_an_out_of_range_reduction_is_refused(pct):
    """0 is a no-op and 100 is a disconnection; neither is a throttle.

    The rule engine validates 1..99 on write, but an imported or hand-edited
    row can hold either, and it must not quietly become a RADIUS profile.
    """
    with pytest.raises(FupThrottleRateError) as caught:
        reduced_kbps(10_000, pct)
    assert caught.value.code == "access.fup_throttle_rate.invalid_reduction_percent"


def test_a_non_positive_rate_cannot_be_reduced():
    with pytest.raises(FupThrottleRateError) as caught:
        reduced_kbps(0, 50)
    assert caught.value.code == "access.fup_throttle_rate.invalid_full_rate"


def test_derived_profile_is_created_once_and_reused(db_session):
    first = resolve_or_create_profile(
        db_session, download_kbps=25_000, upload_kbps=5_000
    )
    second = resolve_or_create_profile(
        db_session, download_kbps=25_000, upload_kbps=5_000
    )
    assert first.id == second.id


def test_derived_profile_puts_upload_first_in_the_rate_limit(db_session):
    """MikroTik Rate-Limit is rx/tx and rx is the subscriber's UPLOAD.

    An asymmetric throttle written the other way round silently swaps a
    customer's up and down rates, which is invisible until someone measures.
    """
    profile = resolve_or_create_profile(
        db_session, download_kbps=25_000, upload_kbps=5_000
    )
    assert profile.mikrotik_rate_limit == "5000k/25000k"
    assert profile.download_speed == 25_000
    assert profile.upload_speed == 5_000


# --- 2. windows are read on the customer's clock ----------------------------


def test_night_window_opens_at_local_2200_not_utc_2200():
    """21:30 UTC is 22:30 in Lagos — inside the free night."""
    at_2230_lagos = datetime(2026, 6, 21, 21, 30, tzinfo=UTC)
    assert _time_in_window(at_2230_lagos, DAY_START, DAY_END, tz=LAGOS) is False
    # Without a tz the same instant reads as 21:30 and the day window still
    # applies — the off-by-an-hour this fixes.
    assert _time_in_window(at_2230_lagos, DAY_START, DAY_END) is True


def test_daytime_is_inside_the_enforcing_window():
    at_1400_lagos = datetime(2026, 6, 21, 13, 0, tzinfo=UTC)
    assert _time_in_window(at_1400_lagos, DAY_START, DAY_END, tz=LAGOS) is True


def test_a_wrapping_night_window_is_understood():
    at_2330_lagos = datetime(2026, 6, 21, 22, 30, tzinfo=UTC)
    at_0300_lagos = datetime(2026, 6, 21, 2, 0, tzinfo=UTC)
    at_1200_lagos = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
    assert _time_in_window(at_2330_lagos, NIGHT_START, NIGHT_END, tz=LAGOS) is True
    assert _time_in_window(at_0300_lagos, NIGHT_START, NIGHT_END, tz=LAGOS) is True
    assert _time_in_window(at_1200_lagos, NIGHT_START, NIGHT_END, tz=LAGOS) is False


def test_weekday_is_resolved_on_the_local_clock():
    """23:30 Lagos on a Sunday is 22:30 UTC on that same Sunday...

    ...but 00:30 Lagos on Monday is 23:30 UTC on Sunday, and a Monday-only rule
    must see Monday.
    """
    monday_0030_lagos = datetime(2026, 6, 21, 23, 30, tzinfo=UTC)  # Sun in UTC
    assert _day_in_list(monday_0030_lagos, [0], tz=LAGOS) is True
    assert _day_in_list(monday_0030_lagos, [0]) is False  # UTC says Sunday


def test_no_window_configured_always_applies():
    assert _time_in_window(datetime(2026, 6, 21, 3, 0, tzinfo=UTC), None, None) is True


# --- 3. night traffic does not accrue ---------------------------------------


def _daily_window(now: datetime):
    return fup_window_bounds("daily", now, LAGOS)


def test_no_accounting_window_leaves_the_day_whole():
    """The common case must stay a single span — no extra queries, no maths."""
    window = _daily_window(datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    spans = accrual_intervals(window, LAGOS, None, None)
    assert spans == [(window.start, window.end)]


def test_free_night_is_excluded_from_accrual():
    """A 05:00-22:00 accounting window counts 17 of the day's 24 hours."""
    window = _daily_window(datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    spans = accrual_intervals(window, LAGOS, DAY_START, DAY_END)
    counted = sum((end - start).total_seconds() for start, end in spans)
    assert counted == 17 * 3600


def test_the_excluded_span_is_the_night_the_customer_was_promised():
    window = _daily_window(datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    spans = accrual_intervals(window, LAGOS, DAY_START, DAY_END)
    assert len(spans) == 1
    start, end = spans[0]
    # 05:00 Lagos == 04:00 UTC; 22:00 Lagos == 21:00 UTC.
    assert start == datetime(2026, 6, 21, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 6, 21, 21, 0, tzinfo=UTC)


def test_inverse_counts_only_the_night():
    window = _daily_window(datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    spans = accrual_intervals(window, LAGOS, DAY_START, DAY_END, inverse=True)
    counted = sum((end - start).total_seconds() for start, end in spans)
    assert counted == 7 * 3600


def test_a_wrapping_accounting_window_spans_two_pieces_of_each_day():
    """Counting 22:00-05:00 — the inverse framing of the same free night."""
    window = _daily_window(datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    spans = accrual_intervals(window, LAGOS, NIGHT_START, NIGHT_END)
    counted = sum((end - start).total_seconds() for start, end in spans)
    assert counted == 7 * 3600
    assert len(spans) == 2  # midnight-05:00 and 22:00-midnight


def test_spans_never_escape_the_consumption_window():
    window = _daily_window(datetime(2026, 6, 21, 12, 0, tzinfo=UTC))
    for start, end in accrual_intervals(window, LAGOS, NIGHT_START, NIGHT_END):
        assert window.start <= start < end <= window.end


def test_a_weekly_window_excludes_the_night_of_every_day():
    window = fup_window_bounds(
        "weekly", datetime(2026, 6, 24, 12, 0, tzinfo=UTC), LAGOS
    )
    spans = accrual_intervals(window, LAGOS, DAY_START, DAY_END)
    counted = sum((end - start).total_seconds() for start, end in spans)
    assert counted == 7 * 17 * 3600
