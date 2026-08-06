"""Properties of the SLA interval algebra (OUTAGE_SLA_SPINE §4).

Period scoring is almost entirely this arithmetic, and the defects that matter
live in the INTERACTIONS between operations, not in any one of them. These are
therefore properties over generated inputs rather than hand-picked examples,
which only cover the interactions their author already thought of.

Hypothesis supplies the search and the shrinking. It does NOT automatically
find the structural edge cases here: a strategy drawing independent minute
offsets almost never produces an exact abuttal, and abuttal is precisely where
a half-open convention can go wrong. Verified by mutation — a ``normalize``
that fails to merge abutting spans passed its own disjointness property until
the strategy was taught to emit them. So the strategy composes anchored
abuttals, duplicates and nesting, and those cases are additionally pinned as
explicit ``@example``s so they run every time rather than by chance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from app.services.sla_interval_algebra import (
    Span,
    clamp,
    intersect,
    normalize,
    span_or_none,
    subtract,
    total_seconds,
    union,
)

ORIGIN = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW = Span(ORIGIN, ORIGIN + timedelta(days=31))
MINUTES = 44_640  # 31 days

FAST = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _span(start_min: int, length_min: int) -> Span:
    return Span(
        ORIGIN + timedelta(minutes=start_min),
        ORIGIN + timedelta(minutes=start_min + length_min),
    )


@st.composite
def spans(draw, *, min_size: int = 0, max_size: int = 6) -> list[Span]:
    """A set of spans that reaches the structural edge cases on purpose.

    Independent draws give overlap and disjointness for free but essentially
    never give exact abuttal, duplication or exact nesting, which is where the
    half-open boundary logic actually breaks.
    """
    out: list[Span] = []
    for _ in range(draw(st.integers(min_value=min_size, max_value=max_size))):
        shape = draw(st.sampled_from(["free", "abut", "duplicate", "nested"]))
        if out and shape == "abut":
            anchor = draw(st.sampled_from(out))
            start = int((anchor.end - ORIGIN).total_seconds() // 60)
            out.append(_span(start, draw(st.integers(1, 2_880))))
        elif out and shape == "duplicate":
            out.append(draw(st.sampled_from(out)))
        elif out and shape == "nested":
            anchor = draw(st.sampled_from(out))
            span_len = int(anchor.duration.total_seconds() // 60)
            if span_len > 2:
                offset = draw(st.integers(1, span_len - 1))
                inner = draw(st.integers(1, max(1, span_len - offset)))
                start = int((anchor.start - ORIGIN).total_seconds() // 60)
                out.append(_span(start + offset, inner))
            else:
                out.append(anchor)
        else:
            out.append(
                _span(draw(st.integers(0, MINUTES)), draw(st.integers(1, 2_880)))
            )
    return out


# Structural cases pinned so they run on every invocation, not by chance.
ABUTTING = [_span(0, 60), _span(60, 60)]
DUPLICATED = [_span(0, 60), _span(0, 60)]
NESTED = [_span(0, 600), _span(100, 50)]
DISJOINT = [_span(0, 60), _span(1_000, 60)]


def _is_disjoint_and_sorted(result: tuple[Span, ...]) -> bool:
    # Abutting spans must have merged, so `<` rather than `<=`.
    return all(a.end < b.start for a, b in zip(result, result[1:], strict=False))


# --- canonical form ---------------------------------------------------------


@FAST
@given(spans())
@example(ABUTTING)
@example(DUPLICATED)
@example(NESTED)
@example(DISJOINT)
def test_normalize_yields_a_disjoint_sorted_set(a):
    assert _is_disjoint_and_sorted(normalize(a))


@FAST
@given(spans())
@example(ABUTTING)
@example(NESTED)
def test_normalize_is_idempotent(a):
    once = normalize(a)
    assert normalize(once) == once


@FAST
@given(spans())
@example(NESTED)
def test_normalize_preserves_covered_instants(a):
    merged = normalize(a)
    for span in a:
        assert any(m.start <= span.start and span.end <= m.end for m in merged)


# --- union ------------------------------------------------------------------


@FAST
@given(spans(), spans())
@example(ABUTTING, DISJOINT)
def test_union_is_order_independent(a, b):
    assert union(a, b) == union(list(reversed(b)), list(reversed(a)))


@FAST
@given(spans())
@example(DUPLICATED)
def test_union_is_idempotent(a):
    assert union(a, a) == normalize(a)


@FAST
@given(spans(), spans())
@example(DUPLICATED, DUPLICATED)
def test_union_never_exceeds_the_sum_of_its_parts(a, b):
    """Overlap counted once — the "two concurrent incidents became twice the
    downtime" defect."""
    assert total_seconds(union(a, b)) <= total_seconds(a) + total_seconds(b)


# --- intersect / subtract ---------------------------------------------------


@FAST
@given(spans(), spans())
@example(ABUTTING, ABUTTING)
@example(NESTED, DISJOINT)
def test_intersect_is_commutative_and_bounded(a, b):
    both = intersect(a, b)
    assert both == intersect(b, a)
    assert total_seconds(both) <= min(total_seconds(a), total_seconds(b))
    assert _is_disjoint_and_sorted(both)


@FAST
@given(spans())
@example(ABUTTING)
def test_intersect_with_self_is_the_canonical_form(a):
    assert intersect(a, a) == normalize(a)


@FAST
@given(spans())
@example(NESTED)
def test_subtracting_self_leaves_nothing(a):
    assert subtract(a, a) == ()


@FAST
@given(spans(), spans())
@example(ABUTTING, NESTED)
@example(NESTED, ABUTTING)
def test_subtract_and_intersect_partition_exactly(a, b):
    """a == (a ∩ b) ⊎ (a − b), with no gap and no double count."""
    inside = intersect(a, b)
    outside = subtract(a, b)
    assert intersect(inside, outside) == ()
    assert union(inside, outside) == normalize(a)
    assert total_seconds(inside) + total_seconds(outside) == total_seconds(a)


# --- clamping to a reporting period -----------------------------------------


@FAST
@given(spans())
@example(ABUTTING)
def test_clamp_never_escapes_the_window(a):
    for span in clamp(a, WINDOW):
        assert WINDOW.start <= span.start < span.end <= WINDOW.end


@FAST
@given(spans())
def test_clamp_is_idempotent(a):
    once = clamp(a, WINDOW)
    assert clamp(once, WINDOW) == once


# --- the scoring identity ---------------------------------------------------


@FAST
@given(spans(), spans(), spans(min_size=1))
@example(ABUTTING, DISJOINT, NESTED)
def test_downtime_and_unknown_can_never_exceed_eligible_time(
    confirmed, monitored, eligible_raw
):
    """The invariant the score contract rests on: uptime is what remains, and
    it can never be negative.

    All three sets are drawn INDEPENDENTLY. An earlier version rebuilt
    ``eligible`` from a fixed seed inside the loop, so every case scored the
    same entitlement and the property was far weaker than it looked.
    """
    eligible = clamp(eligible_raw, WINDOW)
    assume(eligible)

    downtime = intersect(confirmed, eligible)
    unknown = subtract(eligible, union(monitored, downtime))
    uptime = subtract(eligible, union(downtime, unknown))

    eligible_s = total_seconds(eligible)
    assert total_seconds(downtime) + total_seconds(unknown) <= eligible_s
    assert (
        total_seconds(downtime) + total_seconds(unknown) + total_seconds(uptime)
        == eligible_s
    ), "downtime, unknown and uptime must partition eligible time exactly"


def test_unknown_is_the_absence_of_monitoring_not_uptime():
    """Eligible time with no monitoring evidence is unknown, never uptime."""
    eligible = (Span(ORIGIN, ORIGIN + timedelta(days=10)),)
    monitored = (Span(ORIGIN, ORIGIN + timedelta(days=4)),)

    unknown = subtract(eligible, union(monitored, ()))

    assert total_seconds(unknown) == int(timedelta(days=6).total_seconds())


# --- construction guards ----------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        (ORIGIN, ORIGIN),
        (ORIGIN + timedelta(hours=1), ORIGIN),
    ],
)
def test_degenerate_spans_are_rejected(start, end):
    with pytest.raises(ValueError, match="positive duration"):
        Span(start, end)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Span(datetime(2026, 8, 1), datetime(2026, 8, 2))  # noqa: DTZ001


def test_aware_but_non_utc_datetimes_are_rejected():
    """The dangerous case: comparisons still work, so a +01:00 boundary would
    silently shift a period edge by an hour instead of failing."""
    lagos = timezone(timedelta(hours=1))
    with pytest.raises(ValueError, match="must be UTC"):
        Span(datetime(2026, 8, 1, tzinfo=lagos), ORIGIN + timedelta(days=1))


def test_span_or_none_coerces_and_rejects_empty():
    assert span_or_none(None, ORIGIN) is None
    assert span_or_none(ORIGIN, ORIGIN) is None
    assert span_or_none(ORIGIN, ORIGIN - timedelta(hours=1)) is None
    naive = span_or_none(datetime(2026, 8, 1), datetime(2026, 8, 2))  # noqa: DTZ001
    assert naive == Span(ORIGIN, ORIGIN + timedelta(days=1))
    # A non-UTC aware bound is normalised here rather than rejected: this is
    # the coercion boundary for persisted rows.
    shifted = span_or_none(
        datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=1))),
        datetime(2026, 8, 2, tzinfo=UTC),
    )
    assert shifted is not None
    assert shifted.start == ORIGIN - timedelta(hours=1)
