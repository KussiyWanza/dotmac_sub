"""Composable billing cadence and calendar period arithmetic (ADR 0007).

This module is a pure resolver. It owns no records, opens no transaction, and
reads no session. It turns a typed :class:`BillingCadence` value object into
exact half-open ``[starts_at, ends_at)`` service and invoice intervals, and
into deterministic proration factors.

The rules it enforces come straight from ADR 0007 section 4:

- a calendar month is a calendar month; quarterly is three calendar months and
  annual is twelve, never ninety or three hundred and sixty-five days;
- a monthly anniversary on the 29th, 30th, or 31st resolves through one
  declared :class:`EndOfMonthRule`, never an implicit clamp;
- every interval is half-open, so consecutive periods neither gap nor overlap;
- proration is declared, never chosen implicitly;
- the rate unit and the invoice interval are independent, so a daily rate can
  be aggregated into a monthly invoice.

Calendar arithmetic happens in the contract's own timezone and is converted to
UTC at the boundary. Doing it in UTC would put a Lagos month boundary at the
wrong instant for any contract not anchored to UTC.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.billing_contract import (
    CadenceAlignment,
    CollectionTiming,
    EndOfMonthRule,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.services.domain_errors import DomainError


class CadenceError(DomainError):
    """Fail-closed cadence or calendar configuration error."""


@dataclass(frozen=True)
class Interval:
    """A half-open ``[starts_at, ends_at)`` instant range in UTC."""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise CadenceError(
                code="billing.cadence.invalid_interval",
                message="An interval must end strictly after it starts.",
                details={
                    "starts_at": self.starts_at.isoformat(),
                    "ends_at": self.ends_at.isoformat(),
                },
            )

    def contains(self, moment: datetime) -> bool:
        """Half-open membership: the end instant belongs to the next period."""

        return self.starts_at <= moment < self.ends_at

    @property
    def duration(self) -> timedelta:
        return self.ends_at - self.starts_at


@dataclass(frozen=True)
class BillingCadence:
    """Typed, composable cadence. Not a growing list of special-case cycles."""

    rate_basis: RateBasis
    rate_unit: IntervalUnit
    rate_quantity: Decimal
    service_interval_unit: IntervalUnit
    service_interval_count: int
    invoice_interval_unit: IntervalUnit
    invoice_interval_count: int
    collection_timing: CollectionTiming
    alignment: CadenceAlignment
    timezone_name: str
    end_of_month_rule: EndOfMonthRule = EndOfMonthRule.clamp_to_month_end
    proration_policy: ProrationPolicy = ProrationPolicy.none
    anchor_day: int | None = None

    def __post_init__(self) -> None:
        if self.service_interval_count < 1 or self.invoice_interval_count < 1:
            raise CadenceError(
                code="billing.cadence.invalid_interval_count",
                message="Service and invoice interval counts must be positive.",
            )
        if self.rate_quantity <= 0:
            raise CadenceError(
                code="billing.cadence.invalid_rate_quantity",
                message="Rate quantity must be positive.",
            )
        if self.anchor_day is not None and not 1 <= self.anchor_day <= 31:
            raise CadenceError(
                code="billing.cadence.invalid_anchor_day",
                message="Anchor day must fall between 1 and 31.",
                details={"anchor_day": self.anchor_day},
            )
        if (
            self.alignment is CadenceAlignment.fixed_anchor_day
            and self.anchor_day is None
        ):
            raise CadenceError(
                code="billing.cadence.missing_anchor_day",
                message="Fixed-anchor alignment requires an anchor day.",
            )
        # Resolve eagerly so a bad zone fails at construction, not mid-period.
        self.zone()

    def zone(self) -> ZoneInfo:
        """Return the contract timezone, failing closed on an unknown name."""

        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise CadenceError(
                code="billing.cadence.unknown_timezone",
                message="Contract timezone is not a known IANA zone.",
                details={"timezone": self.timezone_name},
            ) from exc


def _require_aware(moment: datetime, *, field: str) -> datetime:
    if moment.tzinfo is None:
        raise CadenceError(
            code="billing.cadence.naive_datetime",
            message="Billing instants must be timezone-aware.",
            details={"field": field},
        )
    return moment


def _add_months(anchor: date, months: int, rule: EndOfMonthRule) -> date | None:
    """Add calendar months, resolving an overflowing day through ``rule``.

    Returns ``None`` when ``strict_same_day_or_skip`` cannot honour the day in
    the target month, which the caller must treat as a skipped boundary rather
    than silently shifting money to a different date.
    """

    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]

    if anchor.day <= last_day:
        return date(year, month, anchor.day)
    if rule is EndOfMonthRule.clamp_to_month_end:
        return date(year, month, last_day)
    return None


def _shift(
    anchor: datetime,
    *,
    unit: IntervalUnit,
    count: int,
    cadence: BillingCadence,
) -> datetime:
    """Advance ``anchor`` by ``count`` calendar units in the contract zone."""

    zone = cadence.zone()
    local = anchor.astimezone(zone)

    if unit is IntervalUnit.day:
        shifted_local = local + timedelta(days=count)
    elif unit is IntervalUnit.week:
        shifted_local = local + timedelta(weeks=count)
    elif unit in (IntervalUnit.month, IntervalUnit.year):
        months = count * (12 if unit is IntervalUnit.year else 1)
        target = _add_months(local.date(), months, cadence.end_of_month_rule)
        if target is None:
            raise CadenceError(
                code="billing.cadence.skipped_month_boundary",
                message=(
                    "Strict same-day rule cannot place this boundary in the "
                    "target month. Declare clamp_to_month_end or correct the "
                    "contract anchor."
                ),
                details={
                    "anchor": anchor.isoformat(),
                    "months": months,
                    "rule": cadence.end_of_month_rule.value,
                },
            )
        shifted_local = datetime.combine(target, local.timetz())
    else:  # pragma: no cover - exhaustive over IntervalUnit
        raise CadenceError(
            code="billing.cadence.unsupported_unit",
            message="Unsupported calendar interval unit.",
            details={"unit": unit.value},
        )

    # Re-normalise: a DST transition can leave a wall-clock time that does not
    # exist or is ambiguous in the zone. fold=0 picks the first occurrence
    # deterministically.
    localised = shifted_local.replace(tzinfo=zone, fold=0)
    return localised.astimezone(UTC)


def _align_start(start: datetime, cadence: BillingCadence) -> datetime:
    """Place the first period boundary according to the declared alignment."""

    if cadence.alignment is CadenceAlignment.contract_anniversary:
        return start

    zone = cadence.zone()
    local = start.astimezone(zone)

    if cadence.alignment is CadenceAlignment.calendar_period_start:
        unit = cadence.service_interval_unit
        if unit is IntervalUnit.day:
            aligned = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elif unit is IntervalUnit.week:
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            aligned = midnight - timedelta(days=midnight.weekday())
        elif unit is IntervalUnit.month:
            aligned = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            aligned = local.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        return aligned.replace(tzinfo=zone, fold=0).astimezone(UTC)

    # fixed_anchor_day: the boundary sits on a declared day of the month.
    assert cadence.anchor_day is not None  # guaranteed by __post_init__
    last_day = calendar.monthrange(local.year, local.month)[1]
    day = min(cadence.anchor_day, last_day)
    anchored = local.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if anchored > local:
        # The anchor for this month is still ahead; the current period began
        # on the previous month's anchor.
        previous = _add_months(anchored.date(), -1, cadence.end_of_month_rule)
        if previous is not None:
            anchored = datetime.combine(previous, anchored.timetz())
    return anchored.replace(tzinfo=zone, fold=0).astimezone(UTC)


def _nth_interval(
    *,
    aligned: datetime,
    unit: IntervalUnit,
    count: int,
    index: int,
    cadence: BillingCadence,
) -> Interval:
    """Return period ``index`` measured from the original anchor.

    Both boundaries are shifted from ``aligned`` rather than each period being
    shifted from its predecessor. That keeps the contract anchor: a contract
    starting on the 31st clamps to the 28th in February and returns to the 31st
    in March, instead of staying clamped for the rest of its life. It also
    keeps consecutive periods exactly contiguous, because period ``i``'s end
    and period ``i+1``'s start are the same computation.
    """

    start = (
        aligned
        if index == 0
        else _shift(aligned, unit=unit, count=count * index, cadence=cadence)
    )
    end = _shift(aligned, unit=unit, count=count * (index + 1), cadence=cadence)
    return Interval(starts_at=start, ends_at=end)


def service_period(
    *,
    cadence: BillingCadence,
    contract_start: datetime,
    index: int = 0,
) -> Interval:
    """Return the ``index``-th service interval from the contract start."""

    _require_aware(contract_start, field="contract_start")
    if index < 0:
        raise CadenceError(
            code="billing.cadence.invalid_period_index",
            message="Period index cannot be negative.",
            details={"index": index},
        )

    aligned = _align_start(contract_start, cadence)
    return _nth_interval(
        aligned=aligned,
        unit=cadence.service_interval_unit,
        count=cadence.service_interval_count,
        index=index,
        cadence=cadence,
    )


def invoice_period(
    *,
    cadence: BillingCadence,
    contract_start: datetime,
    index: int = 0,
) -> Interval:
    """Return the ``index``-th invoice aggregation interval.

    Independent of the service interval: an annual contract invoiced quarterly
    has a twelve-month service period and three-month invoice periods.
    """

    _require_aware(contract_start, field="contract_start")
    if index < 0:
        raise CadenceError(
            code="billing.cadence.invalid_period_index",
            message="Period index cannot be negative.",
            details={"index": index},
        )

    aligned = _align_start(contract_start, cadence)
    return _nth_interval(
        aligned=aligned,
        unit=cadence.invoice_interval_unit,
        count=cadence.invoice_interval_count,
        index=index,
        cadence=cadence,
    )


def period_containing(
    *,
    cadence: BillingCadence,
    contract_start: datetime,
    moment: datetime,
    max_periods: int = 4096,
) -> tuple[int, Interval]:
    """Return the service period index and interval containing ``moment``.

    Walks calendar periods rather than dividing elapsed time, because month
    and year periods have no fixed duration. ``max_periods`` bounds the walk so
    a corrupt anchor cannot spin.
    """

    _require_aware(moment, field="moment")
    aligned = _align_start(contract_start, cadence)
    if moment < aligned:
        raise CadenceError(
            code="billing.cadence.moment_before_contract",
            message="Moment precedes the aligned contract start.",
            details={
                "moment": moment.isoformat(),
                "aligned_start": aligned.isoformat(),
            },
        )

    for index in range(max_periods):
        interval = service_period(
            cadence=cadence, contract_start=contract_start, index=index
        )
        if interval.contains(moment):
            return index, interval

    raise CadenceError(
        code="billing.cadence.period_walk_exhausted",
        message="Period walk exceeded its bound; check the contract anchor.",
        details={"max_periods": max_periods, "moment": moment.isoformat()},
    )


def proration_factor(
    *,
    cadence: BillingCadence,
    period: Interval,
    covered: Interval,
) -> Decimal:
    """Return the declared proration factor for ``covered`` within ``period``.

    The factor is always in ``[0, 1]``. ``ProrationPolicy`` decides the
    measure; nothing here infers one.
    """

    if not (
        covered.starts_at >= period.starts_at and covered.ends_at <= period.ends_at
    ):
        raise CadenceError(
            code="billing.cadence.covered_outside_period",
            message="Covered interval must fall inside the billing period.",
            details={
                "period_start": period.starts_at.isoformat(),
                "period_end": period.ends_at.isoformat(),
                "covered_start": covered.starts_at.isoformat(),
                "covered_end": covered.ends_at.isoformat(),
            },
        )

    policy = cadence.proration_policy
    if policy is ProrationPolicy.none or policy is ProrationPolicy.full_period:
        # ``none`` bills nothing extra for a partial period; ``full_period``
        # bills the whole period regardless of coverage. Both are full factors
        # at this layer -- the difference is whether the caller creates the
        # obligation at all, which is the obligation owner's decision.
        return Decimal("1")

    if policy is ProrationPolicy.actual_elapsed_time:
        whole = Decimal(str(period.duration.total_seconds()))
        part = Decimal(str(covered.duration.total_seconds()))
        return part / whole if whole > 0 else Decimal("0")

    # actual_calendar_days: count whole local calendar days, so a DST shift
    # does not make one day worth 23/24ths of another.
    zone = cadence.zone()
    period_days = _calendar_day_span(period, zone)
    covered_days = _calendar_day_span(covered, zone)
    if period_days <= 0:
        return Decimal("0")
    return Decimal(covered_days) / Decimal(period_days)


def _calendar_day_span(interval: Interval, zone: ZoneInfo) -> int:
    start_local = interval.starts_at.astimezone(zone).date()
    end_local = interval.ends_at.astimezone(zone).date()
    return (end_local - start_local).days


def rate_units_in(
    *,
    cadence: BillingCadence,
    period: Interval,
) -> Decimal:
    """Return how many rate units ``period`` contains.

    Used when the rate unit differs from the service interval, for example a
    per-active-day rate aggregated into one calendar month.
    """

    zone = cadence.zone()
    unit = cadence.rate_unit

    if unit is IntervalUnit.day:
        return Decimal(_calendar_day_span(period, zone))
    if unit is IntervalUnit.week:
        return Decimal(_calendar_day_span(period, zone)) / Decimal(7)

    start_local = period.starts_at.astimezone(zone).date()
    end_local = period.ends_at.astimezone(zone).date()
    months = (end_local.year - start_local.year) * 12 + (
        end_local.month - start_local.month
    )
    if end_local.day < start_local.day:
        months -= 1
    if unit is IntervalUnit.month:
        return Decimal(months)
    return Decimal(months) / Decimal(12)
