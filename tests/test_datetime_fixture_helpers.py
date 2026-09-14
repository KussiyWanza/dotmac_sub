"""Tests for cross-dialect datetime assertion helpers."""

from datetime import datetime, timedelta, timezone

from tests.datetime_fixture_helpers import naive_utc


def test_naive_utc_converts_offset_before_dropping_tzinfo() -> None:
    plus_one = timezone(timedelta(hours=1))

    assert naive_utc(datetime(2026, 9, 11, 10, 0, tzinfo=plus_one)) == datetime(
        2026, 9, 11, 9, 0
    )
