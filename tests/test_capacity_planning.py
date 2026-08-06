"""Capacity verdicts for a shared segment.

The point of these is the boundaries: an unmeasured segment must not read as
healthy, and a reserved rate must not be netted off against statistical
multiplexing that cannot apply to it.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.capacity_planning import (
    CapacityVerdict,
    SegmentUsage,
    can_accept,
    verdict_for,
)


class _Offer:
    """Minimal stand-in for a CatalogOffer; only the rate fields matter here."""

    def __init__(self, down, up, guaranteed=None, floor=None):
        from app.models.catalog import GuaranteedSpeedType

        self.speed_download_mbps = down
        self.speed_upload_mbps = up
        self.guaranteed_speed = guaranteed or GuaranteedSpeedType.none
        self.guaranteed_speed_limit_at = floor


def _usage(**kwargs) -> SegmentUsage:
    base = dict(
        segment_id="p1",
        segment_name="OLT-1 PON 1/1/1",
        downstream_mbps=2488,
        upstream_mbps=1244,
        target_oversubscription=Decimal("5"),
        subscriber_count=0,
        sold_downstream_mbps=0,
        sold_upstream_mbps=0,
        committed_downstream_mbps=0,
        committed_upstream_mbps=0,
        verdict=CapacityVerdict.ok,
    )
    base.update(kwargs)
    return SegmentUsage(**base)


def test_a_lightly_loaded_segment_is_ok():
    verdict = verdict_for(
        downstream_mbps=2488,
        sold_downstream_mbps=1000,
        committed_downstream_mbps=0,
        target_oversubscription=Decimal("5"),
    )

    assert verdict is CapacityVerdict.ok


def test_selling_beyond_the_allowance_is_oversubscribed():
    """2488 x 5 = 12440 sellable; 13000 sold is past it."""
    verdict = verdict_for(
        downstream_mbps=2488,
        sold_downstream_mbps=13000,
        committed_downstream_mbps=0,
        target_oversubscription=Decimal("5"),
    )

    assert verdict is CapacityVerdict.oversubscribed


def test_approaching_the_allowance_flags_at_risk_before_it_breaches():
    """A check that only fires after the fact is a report, not a control."""
    verdict = verdict_for(
        downstream_mbps=1000,
        sold_downstream_mbps=4300,  # 86% of 1000 x 5
        committed_downstream_mbps=0,
        target_oversubscription=Decimal("5"),
    )

    assert verdict is CapacityVerdict.at_risk


def test_committed_rates_beyond_physical_capacity_are_overcommitted():
    """Reserved bandwidth is held whether or not it is used, so no
    oversubscription allowance can rescue it — a generous target must not turn
    an unkeepable set of guarantees into a pass."""
    verdict = verdict_for(
        downstream_mbps=1000,
        sold_downstream_mbps=1200,
        committed_downstream_mbps=1200,
        target_oversubscription=Decimal("50"),
    )

    assert verdict is CapacityVerdict.overcommitted


def test_overcommitment_outranks_oversubscription():
    """Both conditions hold; the caller must be told the unfixable one."""
    verdict = verdict_for(
        downstream_mbps=100,
        sold_downstream_mbps=100_000,
        committed_downstream_mbps=500,
        target_oversubscription=Decimal("1"),
    )

    assert verdict is CapacityVerdict.overcommitted


def test_one_to_one_target_permits_exactly_capacity_and_no_more():
    assert (
        verdict_for(
            downstream_mbps=1000,
            sold_downstream_mbps=1000,
            committed_downstream_mbps=0,
            target_oversubscription=Decimal("1"),
        )
        is not CapacityVerdict.oversubscribed
    )
    assert (
        verdict_for(
            downstream_mbps=1000,
            sold_downstream_mbps=1001,
            committed_downstream_mbps=0,
            target_oversubscription=Decimal("1"),
        )
        is CapacityVerdict.oversubscribed
    )


def test_an_unsurveyed_segment_is_unknown_not_healthy():
    """A port is nameable before it is measured, so the survey backlog can be
    enumerated. Returning ok on a NULL capacity would make the ports nobody has
    measured look like the safest on the network."""
    verdict = verdict_for(
        downstream_mbps=None,
        sold_downstream_mbps=5000,
        committed_downstream_mbps=1000,
        target_oversubscription=Decimal("5"),
    )

    assert verdict is CapacityVerdict.unknown


def test_an_unsurveyed_segment_reports_no_headroom_figure():
    """None, not zero. Zero reads as 'full' and a guess reads as room that may
    not exist; neither is the truth, which is that nobody has measured it."""
    usage = _usage(downstream_mbps=None, sold_downstream_mbps=900)

    assert usage.sellable_downstream_mbps is None
    assert usage.headroom_downstream_mbps is None
    assert usage.committed_share is None


def test_an_unknown_segment_refuses_rather_than_accepting():
    """The check exists to stop overselling. A missing capacity figure is
    exactly where overselling hides, so it must not read as a pass."""
    accepted, verdict, reason = can_accept(
        _usage(verdict=CapacityVerdict.unknown), _Offer(100, 100)
    )

    assert accepted is False
    assert verdict is CapacityVerdict.unknown
    assert "not established" in reason


def test_a_best_effort_sale_fits_where_a_guaranteed_one_would_not():
    """The same 1000 Mbps sells fine best-effort and is refused as a
    guarantee — which is the whole reason committed rate is tracked apart."""
    usage = _usage(downstream_mbps=1000, sold_downstream_mbps=0)

    best_effort_ok, _, _ = can_accept(usage, _Offer(1000, 1000))

    from app.models.catalog import GuaranteedSpeedType

    guaranteed = _Offer(1000, 1000, guaranteed=GuaranteedSpeedType.fixed, floor=1000)
    usage_with_load = _usage(
        downstream_mbps=1000,
        sold_downstream_mbps=800,
        committed_downstream_mbps=800,
    )
    guaranteed_ok, verdict, reason = can_accept(usage_with_load, guaranteed)

    assert best_effort_ok is True
    assert guaranteed_ok is False
    assert verdict is CapacityVerdict.overcommitted
    assert "physical capacity" in reason


def test_headroom_and_committed_share_are_reported_not_just_a_verdict():
    """A service order should record the numbers, so a human can overrule the
    verdict with evidence rather than by guessing."""
    usage = _usage(
        downstream_mbps=1000,
        sold_downstream_mbps=3000,
        committed_downstream_mbps=250,
        target_oversubscription=Decimal("5"),
    )

    assert usage.sellable_downstream_mbps == Decimal("5000")
    assert usage.headroom_downstream_mbps == Decimal("2000")
    assert usage.committed_share == Decimal("0.25")
