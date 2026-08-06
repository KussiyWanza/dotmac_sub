"""Half-open interval algebra for SLA period scoring (OUTAGE_SLA_SPINE §4).

Pure functions over immutable spans — no session, no clock, no I/O. Scoring
correctness is almost entirely this arithmetic, so it lives on its own where
it can be exhaustively tested without a database.

Every span is half-open ``[start, end)``. That is not a detail: it is what
lets one interval end exactly where the next begins without the boundary
instant being counted twice or dropped. A closed convention would make
"policy A until 09:00, policy B from 09:00" either double-count or lose the
09:00 instant, and a month of those errors is a wrong customer-facing number.

The operations exist because a period score is a set-algebra problem:

    downtime  = confirmed ∩ eligible          (never outside entitlement)
    excluded  = reviewed/non-exact ∩ eligible − downtime
    unknown   = eligible − monitored − downtime − excluded
    uptime    = eligible − downtime − unknown − excluded (never assumed)

Overlapping intervals are unioned, never summed: two concurrent incidents on
one subscription are one outage to the customer, and adding them would invent
downtime that did not happen.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

__all__ = [
    "Span",
    "clamp",
    "intersect",
    "normalize",
    "subtract",
    "total_seconds",
    "union",
]


@dataclass(frozen=True, slots=True, order=True)
class Span:
    """One half-open ``[start, end)`` interval in UTC.

    Empty and inverted spans are rejected at construction rather than being
    silently tolerated: a zero-length "outage" contributes nothing but a
    negative one would quietly subtract real downtime.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
            offset = None if value.tzinfo is None else value.utcoffset()
            if offset is None:
                raise ValueError(f"{name} must be timezone-aware UTC")
            # Aware-but-not-UTC is the dangerous case: comparisons still work,
            # so a +01:00 boundary would silently shift a period edge by an
            # hour rather than failing. The type says UTC; enforce it.
            if offset != timedelta(0):
                raise ValueError(f"{name} must be UTC (offset +00:00), got {offset}")
        if self.end <= self.start:
            raise ValueError("a span must cover a positive duration")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end

    def abuts(self, other: Span) -> bool:
        return self.end == other.start or other.end == self.start


def normalize(spans: Iterable[Span]) -> tuple[Span, ...]:
    """Sort and merge into a canonical, disjoint, gap-free-where-contiguous set.

    Abutting spans merge as well as overlapping ones, so the canonical form of
    a set is independent of how it was assembled — which is what makes
    ``union`` order-independent and ``normalize`` idempotent.
    """

    ordered = sorted(spans)
    merged: list[Span] = []
    for span in ordered:
        if merged and span.start <= merged[-1].end:
            previous = merged[-1]
            if span.end > previous.end:
                merged[-1] = Span(previous.start, span.end)
        else:
            merged.append(span)
    return tuple(merged)


def union(*groups: Iterable[Span]) -> tuple[Span, ...]:
    """Everything covered by any input. Order-independent by construction."""

    collected: list[Span] = []
    for group in groups:
        collected.extend(group)
    return normalize(collected)


def intersect(left: Iterable[Span], right: Iterable[Span]) -> tuple[Span, ...]:
    """Only what BOTH sides cover — e.g. downtime inside entitlement."""

    a = normalize(left)
    b = normalize(right)
    out: list[Span] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i].start, b[j].start)
        end = min(a[i].end, b[j].end)
        if start < end:
            out.append(Span(start, end))
        # Advance whichever ends first; the other may still meet the next one.
        if a[i].end <= b[j].end:
            i += 1
        else:
            j += 1
    return tuple(out)


def subtract(left: Iterable[Span], right: Iterable[Span]) -> tuple[Span, ...]:
    """What the left side covers and the right side does not."""

    remaining = normalize(left)
    for cut in normalize(right):
        carried: list[Span] = []
        for span in remaining:
            if not span.overlaps(cut):
                carried.append(span)
                continue
            if span.start < cut.start:
                carried.append(Span(span.start, cut.start))
            if cut.end < span.end:
                carried.append(Span(cut.end, span.end))
        remaining = tuple(carried)
    return normalize(remaining)


def clamp(spans: Iterable[Span], window: Span) -> tuple[Span, ...]:
    """Restrict to a reporting window — the period boundary is authoritative."""

    return intersect(spans, (window,))


def total_seconds(spans: Iterable[Span]) -> int:
    """Whole seconds covered, counting each instant once.

    Normalizes first: summing raw spans would double-count overlap, which is
    exactly the "two concurrent incidents become twice the downtime" error.
    """

    return sum(int(span.duration.total_seconds()) for span in normalize(spans))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def span_or_none(start: datetime | None, end: datetime | None) -> Span | None:
    """Build a span from optional, possibly-naive bounds, or None if empty.

    Persisted rows carry nullable, sometimes naive timestamps; this is the one
    place that coercion happens so callers never construct an invalid Span.
    """

    lo = _utc(start)
    hi = _utc(end)
    # Normalise a non-UTC aware value rather than rejecting it here: this is
    # the coercion boundary for persisted rows, and Span enforces the invariant.
    lo = lo.astimezone(UTC) if lo is not None else None
    hi = hi.astimezone(UTC) if hi is not None else None
    if lo is None or hi is None or hi <= lo:
        return None
    return Span(lo, hi)
