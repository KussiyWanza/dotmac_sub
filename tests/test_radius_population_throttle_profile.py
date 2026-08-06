"""Regression tests for credential-level throttle precedence in populate() (SP-2).

A dunning/FUP throttle is applied by setting ``AccessCredential.
radius_profile_id``. Before this fix, populate() rebuilt radreply purely from
the subscription/offer profile and ignored that override, so every throttle was
silently reverted within one sweep and never reached the router. populate() now
resolves the effective profile with credential > subscription precedence via
``_effective_profile``; these tests pin that precedence and the offer-derived
rate that flows from it.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.catalog import GuaranteedSpeedType
from app.services.radius_population import _effective_profile, _rate_limit

_SUB_PROFILE = SimpleNamespace(id="sub-prof", mikrotik_rate_limit="100M/50M")
_THROTTLE = SimpleNamespace(id="throttle-prof", mikrotik_rate_limit="1M/1M")
_PROFILES = {"sub-prof": _SUB_PROFILE, "throttle-prof": _THROTTLE}


def _cred(profile_id):
    return SimpleNamespace(radius_profile_id=profile_id)


def test_credential_throttle_wins_over_subscription_profile() -> None:
    """The applied throttle must shape the line, not the offer speed."""
    eff = _effective_profile(_cred("throttle-prof"), _SUB_PROFILE, _PROFILES)
    assert eff is _THROTTLE
    # ...and the rate-limit that populate writes is the throttled one.
    assert _rate_limit(offer=None, profile=eff) == "1M/1M"


def test_no_credential_override_uses_subscription_profile() -> None:
    """A normal (non-throttled) customer is untouched: subscription profile."""
    eff = _effective_profile(_cred(None), _SUB_PROFILE, _PROFILES)
    assert eff is _SUB_PROFILE
    assert _rate_limit(offer=None, profile=eff) == "100M/50M"


def test_missing_credential_uses_subscription_profile() -> None:
    eff = _effective_profile(None, _SUB_PROFILE, _PROFILES)
    assert eff is _SUB_PROFILE


def test_stale_credential_profile_falls_back_to_subscription() -> None:
    """A credential pointing at a profile that no longer exists falls back to
    the subscription profile rather than dropping the customer's shaping."""
    eff = _effective_profile(_cred("deleted-prof"), _SUB_PROFILE, _PROFILES)
    assert eff is _SUB_PROFILE


def test_offer_derived_rate_survives_when_no_profile_at_all() -> None:
    """With neither a credential override nor a subscription profile, the
    offer-derived speed is used (behaviour unchanged for plan-only customers)."""
    eff = _effective_profile(_cred(None), None, _PROFILES)
    assert eff is None
    # A real CatalogOffer always carries guaranteed_speed; the stub has to as
    # well now that the rate limit consults it to decide whether to emit a
    # committed rate. `none` keeps this case best-effort, as it was.
    offer = SimpleNamespace(
        speed_download_mbps=200,
        speed_upload_mbps=100,
        guaranteed_speed=GuaranteedSpeedType.none,
        guaranteed_speed_limit_at=None,
    )
    # rx/tx is NAS-perspective — upload first, download second. A 200/100 offer
    # emits 100M/200M; reversed it would sell 100 down and 200 up.
    assert _rate_limit(offer=offer, profile=eff) == "100M/200M"
