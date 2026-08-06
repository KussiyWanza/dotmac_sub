"""Bandwidth band pricing — the rule dedicated circuits are quoted from."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.catalog import BandwidthPriceBand
from app.services import bandwidth_pricing as bp


def _band(db, *, frm, to, rate, family="dedicated", currency="NGN", active=True):
    band = BandwidthPriceBand(
        plan_family=family,
        speed_from_mbps=frm,
        speed_to_mbps=to,
        rate_per_mbps=Decimal(str(rate)),
        currency=currency,
        is_active=active,
    )
    db.add(band)
    db.flush()
    return band


def _ladder(db):
    """A coherent set: rate falls as the circuit grows."""
    _band(db, frm=0, to=10, rate=10_000)
    _band(db, frm=10, to=50, rate=8_000)
    _band(db, frm=50, to=200, rate=6_000)
    _band(db, frm=200, to=None, rate=4_000)


def test_a_speed_inside_the_first_band_costs_rate_times_speed(db_session):
    """The simple case sales will check by hand: 10 Mbps at N10,000/Mbps."""
    _ladder(db_session)

    quote = bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=10)

    assert quote.amount == Decimal("100000.00")
    assert quote.currency == "NGN"
    assert quote.effective_rate_per_mbps == Decimal("10000.00")


def test_bands_accumulate_progressively_not_as_a_flat_whole_speed_rate(db_session):
    """11 Mbps is the first 10 at N10,000 plus 1 more at N8,000.

    Applying the second band's rate to the whole circuit would give
    11 x N8,000 = N88,000 — cheaper than 10 Mbps. That is the exact defect
    found in the live catalog (500 Mbps priced below 300 Mbps).
    """
    _ladder(db_session)

    quote = bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=11)

    assert quote.amount == Decimal("108000.00")
    assert [(s.mbps_charged, s.rate_per_mbps) for s in quote.segments] == [
        (10, Decimal("10000.00")),
        (1, Decimal("8000.00")),
    ]


def test_price_never_falls_as_speed_rises(db_session):
    """The invariant the band mechanism exists to guarantee.

    Swept across every band boundary, because a boundary is the only place an
    inversion can appear.
    """
    _ladder(db_session)

    previous = Decimal("-1")
    for speed in list(range(1, 60)) + [100, 199, 200, 201, 400, 1000]:
        quote = bp.quote_bandwidth(
            db_session, plan_family="dedicated", speed_mbps=speed
        )
        assert quote.amount > previous, (
            f"{speed} Mbps priced at or below the speed under it"
        )
        previous = quote.amount


def test_the_blended_rate_falls_as_the_circuit_grows(db_session):
    """Volume discount is real: the effective rate/Mbps must decline."""
    _ladder(db_session)

    small = bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=10)
    large = bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=500)

    assert large.effective_rate_per_mbps < small.effective_rate_per_mbps


def test_open_top_band_quotes_any_speed_above_it(db_session):
    _ladder(db_session)

    quote = bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=1000)

    # 10x10k + 40x8k + 150x6k + 800x4k
    assert quote.amount == Decimal("4520000.00")
    assert quote.segments[-1].speed_to_mbps is None


def test_a_gap_in_the_bands_refuses_to_quote(db_session):
    """Inventing a number for an unpriced speed would put a figure in front of
    a customer that no rule produced."""
    _band(db_session, frm=0, to=10, rate=10_000)
    _band(db_session, frm=20, to=None, rate=8_000)

    with pytest.raises(bp.BandwidthPricingError) as caught:
        bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=5)
    assert caught.value.code == "catalog.bandwidth_pricing.incoherent_band_set"
    assert "gap between 10 and 20" in str(caught.value)


def test_overlapping_bands_refuse_to_quote(db_session):
    """Two rates for one Mbps means the answer depends on iteration order."""
    _band(db_session, frm=0, to=30, rate=10_000)
    _band(db_session, frm=20, to=None, rate=8_000)

    problems = bp.validate_band_set(bp.active_bands(db_session, "dedicated"))

    assert any("overlap" in p for p in problems)


def test_a_closed_top_band_is_rejected(db_session):
    """A closed top silently makes every larger circuit unquotable."""
    _band(db_session, frm=0, to=10, rate=10_000)
    _band(db_session, frm=10, to=100, rate=8_000)

    problems = bp.validate_band_set(bp.active_bands(db_session, "dedicated"))

    assert any("unquotable" in p for p in problems)


def test_bands_must_start_at_zero(db_session):
    _band(db_session, frm=5, to=None, rate=10_000)

    problems = bp.validate_band_set(bp.active_bands(db_session, "dedicated"))

    assert any("must start at 0" in p for p in problems)


def test_mixed_currencies_are_rejected(db_session):
    _band(db_session, frm=0, to=10, rate=10_000, currency="NGN")
    _band(db_session, frm=10, to=None, rate=50, currency="USD")

    problems = bp.validate_band_set(bp.active_bands(db_session, "dedicated"))

    assert any("mixed currencies" in p for p in problems)


def test_inactive_bands_are_ignored(db_session):
    """Retiring a band must not leave a hole that still prices."""
    _ladder(db_session)
    _band(db_session, frm=0, to=5, rate=99_999, active=False)

    quote = bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=10)

    assert quote.amount == Decimal("100000.00")


def test_families_price_independently(db_session):
    """A dedicated band set must not leak into an unlimited quote."""
    _ladder(db_session)

    with pytest.raises(bp.BandwidthPricingError) as caught:
        bp.quote_bandwidth(db_session, plan_family="unlimited", speed_mbps=10)
    assert caught.value.code == "catalog.bandwidth_pricing.incoherent_band_set"


def test_unknown_family_and_nonpositive_speed_are_refused(db_session):
    _ladder(db_session)

    with pytest.raises(bp.BandwidthPricingError) as unknown:
        bp.quote_bandwidth(db_session, plan_family="platinum", speed_mbps=10)
    assert unknown.value.code == "catalog.bandwidth_pricing.unknown_plan_family"

    with pytest.raises(bp.BandwidthPricingError) as bad_speed:
        bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=0)
    assert bad_speed.value.code == "catalog.bandwidth_pricing.invalid_speed"


def test_the_quote_carries_immutable_evidence(db_session):
    """A quote is evidence; evidence a caller can mutate is not evidence."""
    _ladder(db_session)

    quote = bp.quote_bandwidth(db_session, plan_family="dedicated", speed_mbps=100)

    assert isinstance(quote.segments, tuple)
    with pytest.raises(Exception):
        quote.amount = Decimal("1.00")  # type: ignore[misc]
