"""Calendar guarantees of the composable billing cadence (ADR 0007 section 4).

These are the rules the target must not quietly break: a quarter is three
calendar months, a year is twelve, month-end anniversaries resolve through one
declared rule, consecutive periods are contiguous and half-open, and proration
is whatever was declared rather than whatever the arithmetic happened to do.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.billing_contract import (
    CadenceAlignment,
    CollectionTiming,
    EndOfMonthRule,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.services.billing.cadence import (
    BillingCadence,
    CadenceError,
    Interval,
    invoice_period,
    period_containing,
    proration_factor,
    rate_units_in,
    service_period,
)

LAGOS = "Africa/Lagos"


def _cadence(
    *,
    service_unit: IntervalUnit = IntervalUnit.month,
    service_count: int = 1,
    invoice_unit: IntervalUnit | None = None,
    invoice_count: int | None = None,
    rate_unit: IntervalUnit | None = None,
    proration: ProrationPolicy = ProrationPolicy.none,
    end_of_month: EndOfMonthRule = EndOfMonthRule.clamp_to_month_end,
    alignment: CadenceAlignment = CadenceAlignment.contract_anniversary,
    anchor_day: int | None = None,
    timezone_name: str = LAGOS,
) -> BillingCadence:
    return BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=rate_unit or service_unit,
        rate_quantity=Decimal("1"),
        service_interval_unit=service_unit,
        service_interval_count=service_count,
        invoice_interval_unit=invoice_unit or service_unit,
        invoice_interval_count=invoice_count or service_count,
        collection_timing=CollectionTiming.advance,
        alignment=alignment,
        timezone_name=timezone_name,
        end_of_month_rule=end_of_month,
        proration_policy=proration,
        anchor_day=anchor_day,
    )


def test_quarterly_is_three_calendar_months_not_ninety_days() -> None:
    cadence = _cadence(service_unit=IntervalUnit.month, service_count=3)
    start = datetime(2026, 1, 15, tzinfo=UTC)

    period = service_period(cadence=cadence, contract_start=start, index=0)

    # 2026-01-15 + 3 calendar months is April, not 90 days later (which lands
    # in mid-April only by coincidence and drifts every quarter).
    assert period.ends_at.astimezone(UTC).month == 4
    assert period.ends_at.astimezone(UTC).day == 15
    assert period.duration.days == 90  # this quarter happens to be 90 days
    next_period = service_period(cadence=cadence, contract_start=start, index=1)
    assert next_period.duration.days == 91  # the next one is not


def test_annual_is_twelve_calendar_months_across_a_leap_year() -> None:
    cadence = _cadence(service_unit=IntervalUnit.year, service_count=1)
    start = datetime(2027, 3, 1, tzinfo=UTC)

    period = service_period(cadence=cadence, contract_start=start, index=0)

    assert period.ends_at.astimezone(UTC).year == 2028
    assert period.ends_at.astimezone(UTC).month == 3
    assert period.ends_at.astimezone(UTC).day == 1
    # 2028 is a leap year, so this service year is 366 days, not 365.
    assert period.duration.days == 366


def test_month_end_anniversary_clamps_under_the_declared_rule() -> None:
    cadence = _cadence(end_of_month=EndOfMonthRule.clamp_to_month_end)
    start = datetime(2026, 1, 31, 9, 0, tzinfo=UTC)

    february = service_period(cadence=cadence, contract_start=start, index=0)
    march = service_period(cadence=cadence, contract_start=start, index=1)

    assert february.ends_at.astimezone(UTC).month == 2
    assert february.ends_at.astimezone(UTC).day == 28
    # The anchor is not lost: March returns to the 31st rather than staying
    # clamped to the 28th for the rest of the contract.
    assert march.ends_at.astimezone(UTC).day == 31


def test_strict_same_day_rule_fails_closed_instead_of_silently_shifting() -> None:
    cadence = _cadence(end_of_month=EndOfMonthRule.strict_same_day_or_skip)
    start = datetime(2026, 1, 31, tzinfo=UTC)

    with pytest.raises(CadenceError) as excinfo:
        service_period(cadence=cadence, contract_start=start, index=0)

    assert excinfo.value.code == "billing.cadence.skipped_month_boundary"


def test_consecutive_periods_are_contiguous_and_half_open() -> None:
    cadence = _cadence()
    start = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)

    first = service_period(cadence=cadence, contract_start=start, index=0)
    second = service_period(cadence=cadence, contract_start=start, index=1)

    assert first.ends_at == second.starts_at
    assert first.contains(first.starts_at)
    assert not first.contains(first.ends_at)
    assert second.contains(second.starts_at)


def test_rate_unit_is_independent_of_invoice_interval() -> None:
    """A daily rate aggregated into a monthly invoice."""

    cadence = _cadence(
        service_unit=IntervalUnit.month,
        invoice_unit=IntervalUnit.month,
        rate_unit=IntervalUnit.day,
    )
    start = datetime(2026, 6, 1, tzinfo=UTC)

    period = invoice_period(cadence=cadence, contract_start=start, index=0)

    assert rate_units_in(cadence=cadence, period=period) == Decimal(30)


def test_annual_service_period_invoiced_quarterly() -> None:
    cadence = _cadence(
        service_unit=IntervalUnit.year,
        service_count=1,
        invoice_unit=IntervalUnit.month,
        invoice_count=3,
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)

    service = service_period(cadence=cadence, contract_start=start, index=0)
    first_invoice = invoice_period(cadence=cadence, contract_start=start, index=0)
    fourth_invoice = invoice_period(cadence=cadence, contract_start=start, index=3)

    assert service.ends_at.astimezone(UTC).year == 2027
    assert first_invoice.ends_at.astimezone(UTC).month == 4
    assert fourth_invoice.ends_at == service.ends_at


def test_period_containing_walks_calendar_periods() -> None:
    cadence = _cadence()
    start = datetime(2026, 1, 31, tzinfo=UTC)

    index, interval = period_containing(
        cadence=cadence,
        contract_start=start,
        moment=datetime(2026, 4, 2, tzinfo=UTC),
    )

    assert index == 2
    assert interval.contains(datetime(2026, 4, 2, tzinfo=UTC))


def test_moment_before_contract_start_fails_closed() -> None:
    cadence = _cadence()
    start = datetime(2026, 2, 1, tzinfo=UTC)

    with pytest.raises(CadenceError) as excinfo:
        period_containing(
            cadence=cadence,
            contract_start=start,
            moment=datetime(2026, 1, 1, tzinfo=UTC),
        )

    assert excinfo.value.code == "billing.cadence.moment_before_contract"


def test_calendar_day_proration_is_declared_not_inferred() -> None:
    cadence = _cadence(proration=ProrationPolicy.actual_calendar_days)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    period = service_period(cadence=cadence, contract_start=start, index=0)
    covered = Interval(
        starts_at=datetime(2026, 6, 16, tzinfo=UTC), ends_at=period.ends_at
    )

    factor = proration_factor(cadence=cadence, period=period, covered=covered)

    assert factor == Decimal(15) / Decimal(30)


def test_no_proration_policy_bills_the_full_period() -> None:
    cadence = _cadence(proration=ProrationPolicy.none)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    period = service_period(cadence=cadence, contract_start=start, index=0)
    covered = Interval(
        starts_at=datetime(2026, 6, 16, tzinfo=UTC), ends_at=period.ends_at
    )

    assert proration_factor(cadence=cadence, period=period, covered=covered) == (
        Decimal("1")
    )


def test_covered_interval_outside_the_period_fails_closed() -> None:
    cadence = _cadence(proration=ProrationPolicy.actual_calendar_days)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    period = service_period(cadence=cadence, contract_start=start, index=0)
    covered = Interval(
        starts_at=datetime(2026, 5, 20, tzinfo=UTC), ends_at=period.ends_at
    )

    with pytest.raises(CadenceError) as excinfo:
        proration_factor(cadence=cadence, period=period, covered=covered)

    assert excinfo.value.code == "billing.cadence.covered_outside_period"


def test_calendar_alignment_snaps_to_the_month_boundary() -> None:
    cadence = _cadence(alignment=CadenceAlignment.calendar_period_start)
    start = datetime(2026, 6, 17, 14, 30, tzinfo=UTC)

    period = service_period(cadence=cadence, contract_start=start, index=0)
    local_start = period.starts_at.astimezone(cadence.zone())

    assert (local_start.day, local_start.hour, local_start.minute) == (1, 0, 0)
    assert local_start.month == 6


def test_periods_are_computed_in_the_contract_timezone() -> None:
    """A Lagos month boundary is not a UTC month boundary."""

    cadence = _cadence(alignment=CadenceAlignment.calendar_period_start)
    start = datetime(2026, 6, 17, tzinfo=UTC)

    period = service_period(cadence=cadence, contract_start=start, index=0)

    # Lagos is UTC+1 with no DST, so local midnight is 23:00 UTC the day before.
    assert period.starts_at.astimezone(UTC).hour == 23
    assert period.starts_at.astimezone(UTC).day == 31


def test_naive_datetimes_are_refused() -> None:
    cadence = _cadence()

    with pytest.raises(CadenceError) as excinfo:
        service_period(cadence=cadence, contract_start=datetime(2026, 1, 1))

    assert excinfo.value.code == "billing.cadence.naive_datetime"


def test_unknown_timezone_fails_at_construction() -> None:
    with pytest.raises(CadenceError) as excinfo:
        _cadence(timezone_name="Mars/Olympus_Mons")

    assert excinfo.value.code == "billing.cadence.unknown_timezone"


def test_fixed_anchor_alignment_requires_an_anchor_day() -> None:
    with pytest.raises(CadenceError) as excinfo:
        _cadence(alignment=CadenceAlignment.fixed_anchor_day)

    assert excinfo.value.code == "billing.cadence.missing_anchor_day"


def test_interval_must_end_after_it_starts() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(CadenceError) as excinfo:
        Interval(starts_at=moment, ends_at=moment)

    assert excinfo.value.code == "billing.cadence.invalid_interval"
