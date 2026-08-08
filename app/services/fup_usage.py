"""Single source of truth for FUP consumption windows and windowed usage (#21).

One reader, one window definition, multiple backing sources. Enforcement
(`evaluate_fup_rules`), the customer usage summary, and notifications all go
through this module so they can never drift apart. See
docs/designs/FUP_CONSUMPTION_WINDOWS.md.

This file is Phase A1 (window bounds). A2 adds ``get_fup_usage_gb`` (the reader);
B layers durable period buckets behind the same reader.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.fup import FupConsumptionPeriod


@dataclass(frozen=True)
class FupWindow:
    """An aligned [start, end) consumption window for a FUP rule."""

    period: str  # "daily" | "weekly" | "monthly"
    start: datetime  # UTC instant, inclusive
    end: datetime  # UTC instant, exclusive
    period_key: str  # "2026-06-21" | "2026-W25" | "2026-06"
    timezone: str  # tz the window was aligned to


def period_value(period: FupConsumptionPeriod | str | None) -> str:
    """Normalize a consumption_period (enum or str) to its string value."""
    if isinstance(period, FupConsumptionPeriod):
        return period.value
    value = str(period or "monthly").lower()
    return value if value in {"daily", "weekly", "monthly"} else "monthly"


def fup_window_bounds(
    period: FupConsumptionPeriod | str | None,
    now: datetime,
    tz: ZoneInfo | None = None,
) -> FupWindow:
    """Aligned consumption window for ``period`` containing ``now``.

    - daily: subscriber-local midnight to next local midnight.
    - weekly: subscriber-local Monday 00:00 to the next Monday.
    - monthly: UTC calendar month — matches the billing QuotaBucket so existing
      monthly rules are unchanged (avoids an accidental tz-shifted month).

    daily/weekly align to ``tz`` (subscriber timezone, app-tz fallback) so a
    "per day" cap resets at the customer's local midnight, not UTC midnight.
    """
    p = period_value(period)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_tz = tz or UTC

    if p == "daily":
        local = now.astimezone(local_tz)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start.astimezone(UTC)
        end = (day_start + timedelta(days=1)).astimezone(UTC)
        key = day_start.strftime("%Y-%m-%d")
        tzname = _tz_name(local_tz)
    elif p == "weekly":
        local = now.astimezone(local_tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = midnight - timedelta(days=midnight.weekday())  # Monday
        start = week_start.astimezone(UTC)
        end = (week_start + timedelta(days=7)).astimezone(UTC)
        iso = week_start.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        tzname = _tz_name(local_tz)
    else:  # monthly — UTC calendar month, matches QuotaBucket
        u = now.astimezone(UTC)
        start = datetime(u.year, u.month, 1, tzinfo=UTC)
        end = (
            datetime(u.year + 1, 1, 1, tzinfo=UTC)
            if u.month == 12
            else datetime(u.year, u.month + 1, 1, tzinfo=UTC)
        )
        key = start.strftime("%Y-%m")
        tzname = "UTC"

    return FupWindow(period=p, start=start, end=end, period_key=key, timezone=tzname)


def _tz_name(tz) -> str:
    return getattr(tz, "key", None) or str(tz)


@dataclass(frozen=True)
class FupUsageWindow:
    """Windowed FUP usage: the single value enforcement, UI, and notifications
    all read. ``source`` records which backing store answered (so A/B drift can
    be observed); ``is_authoritative`` is False when derived from possibly-sparse
    samples — callers should avoid a hard throttle on non-authoritative zero."""

    used_gb: float
    window: FupWindow
    source: str  # "quota_bucket" | "samples" | "fallback"
    is_authoritative: bool


def accrual_intervals(
    window: FupWindow,
    tz: ZoneInfo,
    acct_start: time | None,
    acct_end: time | None,
    inverse: bool = False,
) -> list[tuple[datetime, datetime]]:
    """Split a consumption window into the spans whose traffic actually counts.

    A free overnight window is not only a matter of lifting the throttle: the
    traffic a customer moves during it must not eat the next day's bucket.
    ``fup_policies.traffic_accounting_start/end`` is the field that says so, and
    until this existed it was applied to nothing — ``windowed_used_bytes``
    integrated the whole window, so "free night" counted like any other hour.

    The window is expressed in the subscriber's **wall clock** and re-derived
    per local day, so it stays correct across a DST transition rather than
    assuming a fixed offset. ``inverse`` flips which side counts.

    Returns ``[(start, end)]`` unchanged when no accounting window is set —
    the overwhelmingly common case, and one that must stay free of extra
    queries and arithmetic.
    """
    if acct_start is None or acct_end is None:
        return [(window.start, window.end)]

    spans: list[tuple[datetime, datetime]] = []
    local_start = window.start.astimezone(tz)
    local_end = window.end.astimezone(tz)
    day = local_start.replace(hour=0, minute=0, second=0, microsecond=0)

    while day < local_end:
        next_day = day + timedelta(days=1)
        if acct_start <= acct_end:
            counted = [(_at(day, acct_start, tz), _at(day, acct_end, tz))]
        else:
            # Window wraps midnight (e.g. 22:00 -> 05:00): two spans per day.
            counted = [
                (day.astimezone(UTC), _at(day, acct_end, tz)),
                (_at(day, acct_start, tz), next_day.astimezone(UTC)),
            ]
        if inverse:
            counted = _complement(
                counted, day.astimezone(UTC), next_day.astimezone(UTC)
            )
        for span_start, span_end in counted:
            clipped_start = max(span_start, window.start)
            clipped_end = min(span_end, window.end)
            if clipped_start < clipped_end:
                spans.append((clipped_start, clipped_end))
        day = next_day

    return spans


def _at(local_day: datetime, at: time, tz: ZoneInfo) -> datetime:
    """A wall-clock time on ``local_day``, as a UTC instant.

    Built naive and localized fresh so the offset is resolved for that instant
    rather than inherited across a DST boundary.
    """
    naive = datetime.combine(local_day.date(), at)
    return naive.replace(tzinfo=tz).astimezone(UTC)


def _complement(
    spans: list[tuple[datetime, datetime]],
    day_start: datetime,
    day_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """The parts of [day_start, day_end) not covered by ``spans``."""
    out: list[tuple[datetime, datetime]] = []
    cursor = day_start
    for span_start, span_end in sorted(spans):
        if span_start > cursor:
            out.append((cursor, span_start))
        cursor = max(cursor, span_end)
    if cursor < day_end:
        out.append((cursor, day_end))
    return out


def _accounting_window(db: Session, offer_id) -> tuple[time | None, time | None, bool]:
    """The offer policy's accrual window, or (None, None, False) if unset."""
    from app.services.fup import FupPolicies

    policy = FupPolicies.get_by_offer(db, str(offer_id))
    if policy is None or not policy.is_active:
        return None, None, False
    return (
        policy.traffic_accounting_start,
        policy.traffic_accounting_end,
        bool(policy.traffic_inverse_interval),
    )


async def _resolve_fup_usage(
    db: Session,
    subscription,
    period: FupConsumptionPeriod | str | None,
    now: datetime,
    tz: ZoneInfo | None,
) -> FupUsageWindow:
    # Lazy import keeps fup_usage importable without pulling the bandwidth/VM
    # stack at module load, and avoids any import cycle with usage_summary.
    from app.services.usage_summary import (
        _GB_BYTES,
        _current_bucket_used_gb,
        _subscriber_tz,
        windowed_used_bytes,
    )

    p = period_value(period)
    if tz is None:
        tz = _subscriber_tz(db, str(subscription.subscriber_id))
    window = fup_window_bounds(p, now, tz)

    if p == "monthly":
        # Authoritative billing-cycle usage from the rated quota bucket.
        used = _current_bucket_used_gb(db, subscription.id)
        return FupUsageWindow(
            used_gb=float(used or 0.0),
            window=window,
            source="quota_bucket",
            is_authoritative=used is not None,
        )

    acct_start, acct_end, inverse = _accounting_window(db, subscription.offer_id)
    spans = accrual_intervals(window, tz, acct_start, acct_end, inverse)
    total_bytes = 0
    had_data = False
    for span_start, span_end in spans:
        span_bytes, span_had_data = await windowed_used_bytes(
            db, [subscription.id], span_start, span_end, tz
        )
        total_bytes += span_bytes
        had_data = had_data or span_had_data
    # "no_data" (offline OR metrics store down) is distinct from a measured 0 —
    # enforcement must not act on a blind reading (#21 safeguard).
    return FupUsageWindow(
        used_gb=total_bytes / _GB_BYTES,
        window=window,
        source="samples" if had_data else "no_data",
        is_authoritative=False,
    )


async def get_fup_usage_gb_async(
    db: Session,
    subscription,
    period: FupConsumptionPeriod | str | None,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> FupUsageWindow:
    """Windowed FUP usage for one subscription. Await this from async callers
    (e.g. the customer usage-summary endpoint)."""
    return await _resolve_fup_usage(
        db, subscription, period, now or datetime.now(UTC), tz
    )


def get_fup_usage_gb(
    db: Session,
    subscription,
    period: FupConsumptionPeriod | str | None,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> FupUsageWindow:
    """Sync entry for the Celery FUP-evaluation task. Bridges the async reader
    via asyncio.run — do NOT call from within a running event loop (use
    ``get_fup_usage_gb_async`` there)."""
    return asyncio.run(
        _resolve_fup_usage(db, subscription, period, now or datetime.now(UTC), tz)
    )


def build_usage_by_period(
    db: Session,
    subscription,
    offer_id: str,
    now: datetime,
    monthly_used_gb: float,
) -> dict[str, FupUsageWindow]:
    """Usage per consumption period needed by an offer's active FUP rules.

    monthly reuses the already-resolved billing-bucket figure (authoritative, no
    extra query); daily/weekly come from the windowed reader. Keyed by the
    period string so ``evaluate_rules`` can look each rule up by its
    ``consumption_period``. Sync — call only from the Celery evaluation task.
    """
    from app.services.fup import FupPolicies

    policy = FupPolicies.get_by_offer(db, offer_id)
    periods = (
        {period_value(r.consumption_period) for r in policy.rules if r.is_active}
        if policy
        else set()
    )
    out: dict[str, FupUsageWindow] = {}
    for p in periods:
        if p == "monthly":
            out["monthly"] = FupUsageWindow(
                used_gb=float(monthly_used_gb),
                window=fup_window_bounds("monthly", now),  # monthly ignores tz
                source="quota_bucket",
                is_authoritative=True,
            )
        else:
            out[p] = get_fup_usage_gb(db, subscription, p, now=now)
    return out
