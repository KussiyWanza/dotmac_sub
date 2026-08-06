"""The committed rate (CIR) that turns a dedicated tier into a guarantee.

Without ``rx-rate-min``/``tx-rate-min`` every tier is best effort regardless of
price. These tests pin when the floor is emitted, when it is deliberately not,
and the two ways a bad floor could take a subscriber's rate limit away entirely.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.catalog import GuaranteedSpeedType
from app.services.radius_population import _rate_limit


def _offer(**kwargs):
    base = dict(
        speed_download_mbps=100,
        speed_upload_mbps=100,
        guaranteed_speed=GuaranteedSpeedType.none,
        guaranteed_speed_limit_at=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_best_effort_emits_a_ceiling_only(db_session=None):
    """unlimited and home_flex sell a ceiling. Emitting a floor for them would
    reserve capacity the customer never bought."""
    result = _rate_limit(offer=_offer(), profile=None)

    assert result == "100M/100M"
    assert " " not in result, "a best-effort limit carries no positional tail"


def test_a_fixed_guarantee_emits_the_committed_rate_last():
    """The full positional grammar, because rx-rate-min is only reachable at
    the end: rx/tx burst threshold burst-time priority min."""
    result = _rate_limit(
        offer=_offer(
            guaranteed_speed=GuaranteedSpeedType.fixed,
            guaranteed_speed_limit_at=100,
        ),
        profile=None,
    )

    assert result == "100M/100M 0/0 0/0 0/0 8 100M/100M"


def test_a_one_to_one_guarantee_has_floor_equal_to_ceiling():
    """That equality IS the 1:1 promise — reserved, not merely allowed."""
    result = _rate_limit(
        offer=_offer(
            speed_download_mbps=45,
            speed_upload_mbps=45,
            guaranteed_speed=GuaranteedSpeedType.fixed,
            guaranteed_speed_limit_at=45,
        ),
        profile=None,
    )

    ceiling, _, tail = result.partition(" ")
    assert ceiling == "45M/45M"
    assert tail.endswith("45M/45M")


def test_a_floor_above_the_ceiling_is_capped_not_emitted_raw():
    """RouterOS rejects a committed rate above the max rate, and it rejects the
    WHOLE attribute — the subscriber would end up with no rate limit at all,
    i.e. unshaped. Capping fails safe instead."""
    result = _rate_limit(
        offer=_offer(
            speed_download_mbps=50,
            speed_upload_mbps=50,
            guaranteed_speed=GuaranteedSpeedType.fixed,
            guaranteed_speed_limit_at=500,
        ),
        profile=None,
    )

    assert result == "50M/50M 0/0 0/0 0/0 8 50M/50M"


def test_a_fixed_guarantee_with_no_floor_value_stays_best_effort():
    """Half-configured is not a promise. Emitting 0M/0M would be a floor of
    zero dressed up as a guarantee."""
    result = _rate_limit(
        offer=_offer(
            guaranteed_speed=GuaranteedSpeedType.fixed,
            guaranteed_speed_limit_at=None,
        ),
        profile=None,
    )

    assert result == "100M/100M"


def test_a_relative_guarantee_is_a_percentage_of_the_line_rate():
    result = _rate_limit(
        offer=_offer(
            speed_download_mbps=80,
            speed_upload_mbps=40,
            guaranteed_speed=GuaranteedSpeedType.relative,
            guaranteed_speed_limit_at=50,
        ),
        profile=None,
    )

    # Upload first throughout: 40 up / 80 down, floor at half of each.
    assert result == "40M/80M 0/0 0/0 0/0 8 20M/40M"


def test_a_relative_guarantee_rounding_to_zero_stays_best_effort():
    """1% of 1 Mbps floors to 0. A 0M floor is not a guarantee, and emitting it
    would claim one."""
    result = _rate_limit(
        offer=_offer(
            speed_download_mbps=1,
            speed_upload_mbps=1,
            guaranteed_speed=GuaranteedSpeedType.relative,
            guaranteed_speed_limit_at=1,
        ),
        profile=None,
    )

    assert result == "1M/1M"


def test_an_explicit_profile_override_still_wins():
    """A credential-level throttle must not be overridden by an offer's
    guarantee — that is how a FUP or dunning throttle stays enforced."""
    profile = SimpleNamespace(mikrotik_rate_limit="1M/1M")
    result = _rate_limit(
        offer=_offer(
            guaranteed_speed=GuaranteedSpeedType.fixed,
            guaranteed_speed_limit_at=100,
        ),
        profile=profile,
    )

    assert result == "1M/1M"
