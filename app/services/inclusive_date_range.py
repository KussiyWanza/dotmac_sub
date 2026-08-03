"""Typed inclusive calendar-date filters for read projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta


class InclusiveDateRangeError(Exception):
    """Stable domain error for an invalid inclusive calendar-date range."""

    code = "list_filter.invalid_date_range"

    def __init__(self, *, start_date: date, end_date: date) -> None:
        self.details = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        super().__init__("start_date must be before or equal to end_date")


@dataclass(frozen=True, slots=True)
class InclusiveDateRange:
    """Inclusive calendar dates represented as UTC half-open timestamps."""

    start_date: date | None
    end_date: date | None

    @classmethod
    def from_dates(
        cls,
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> InclusiveDateRange:
        if start_date is not None and end_date is not None and start_date > end_date:
            raise InclusiveDateRangeError(
                start_date=start_date,
                end_date=end_date,
            )
        return cls(start_date=start_date, end_date=end_date)

    @classmethod
    def from_iso_values(
        cls,
        *,
        start_date: str | None,
        end_date: str | None,
    ) -> InclusiveDateRange:
        """Rehydrate normalized values owned by a ``ListQuery``."""

        parsed_start = date.fromisoformat(start_date) if start_date else None
        parsed_end = date.fromisoformat(end_date) if end_date else None
        return cls.from_dates(start_date=parsed_start, end_date=parsed_end)

    @property
    def start_at(self) -> datetime | None:
        if self.start_date is None:
            return None
        return datetime.combine(self.start_date, time.min, tzinfo=UTC)

    @property
    def end_before(self) -> datetime | None:
        if self.end_date is None:
            return None
        return datetime.combine(
            self.end_date + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )

    @property
    def start_value(self) -> str | None:
        return self.start_date.isoformat() if self.start_date else None

    @property
    def end_value(self) -> str | None:
        return self.end_date.isoformat() if self.end_date else None
