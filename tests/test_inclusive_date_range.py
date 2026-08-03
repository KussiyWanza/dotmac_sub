from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.inclusive_date_range import (
    InclusiveDateRange,
    InclusiveDateRangeError,
)


def test_inclusive_date_range_uses_utc_half_open_bounds() -> None:
    value = InclusiveDateRange.from_dates(
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
    )

    assert value.start_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert value.end_before == datetime(2026, 8, 1, tzinfo=UTC)
    assert value.start_value == "2026-07-01"
    assert value.end_value == "2026-07-31"


def test_inclusive_date_range_supports_open_bounds() -> None:
    start_only = InclusiveDateRange.from_dates(
        start_date=date(2026, 7, 1),
        end_date=None,
    )
    end_only = InclusiveDateRange.from_dates(
        start_date=None,
        end_date=date(2026, 7, 31),
    )

    assert start_only.end_before is None
    assert end_only.start_at is None


def test_inclusive_date_range_rejects_reversed_dates() -> None:
    with pytest.raises(InclusiveDateRangeError) as exc_info:
        InclusiveDateRange.from_dates(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 7, 31),
        )

    assert exc_info.value.code == "list_filter.invalid_date_range"
    assert exc_info.value.details == {
        "start_date": "2026-08-01",
        "end_date": "2026-07-31",
    }
