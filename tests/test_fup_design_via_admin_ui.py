"""Configure the whole §1 FUP design through the admin UI, then prove it works.

The engine fixes in this branch were driven by a specific complaint: a free
night could be *configured* on the offer's FUP screen and would silently do
nothing. Unit tests on the repaired internals do not answer that complaint —
they test the parts, not the screen.

So this file drives the real admin form handlers (``web_fup.handle_*``, the
same functions the POST routes call, with the same ``FormData`` the templates
submit), configures PLAN_FAMILY_ARCHITECTURE §1 exactly as an operator would,
and then asserts the engine honours what was configured:

    daily bucket → warn at 80% → throttle to 50% of the plan's own rate
    → free night 22:00–05:00, during which nothing accrues and the throttle
      is released

If any of these fail, the design is not configurable from the UI, whatever the
internals do.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from starlette.datastructures import FormData

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    RadiusProfile,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.fup import FupAction, FupConsumptionPeriod, FupPolicy
from app.services.db_session_adapter import db_session_adapter
from app.services.fup import evaluate_rules, fup_policies
from app.services.fup_throttle_profile import resolve_fup_throttle_profile
from app.services.fup_usage import FupUsageWindow, accrual_intervals, fup_window_bounds
from app.services.owner_commands import CommandContext
from app.services.web_fup import handle_add_rule, handle_policy_update
from tests.fup_helpers import ensure_fup_policy, fup_command_context

LAGOS = "Africa/Lagos"
# The design's night, expressed the way the screen expects it: the accounting
# window is the hours that COUNT, so a free 22:00-05:00 is entered as
# 05:00 -> 22:00.
DAY_START = "05:00"
DAY_END = "22:00"

DAYTIME = datetime(2026, 6, 21, 13, 0, tzinfo=UTC)  # 14:00 Lagos
NIGHT = datetime(2026, 6, 21, 21, 30, tzinfo=UTC)  # 22:30 Lagos


@pytest.fixture
def submonthly_enabled(monkeypatch):
    """Operator has switched on usage.fup_submonthly_rules in System → Modules.

    Without this the screen refuses a daily rule outright, which is the
    deliberate gate on samples-derived usage — asserted separately below.
    """
    monkeypatch.setattr(
        "app.services.web_fup.control_registry.is_enabled", lambda *a, **k: True
    )


@pytest.fixture
def homeflex_offer(db_session):
    """A 10 Mbps Homeflex Basic with its full-speed RADIUS profile attached."""
    from app.models.catalog import OfferRadiusProfile

    profile = RadiusProfile(
        name="Homeflex Basic 10M",
        code=f"hf-basic-{uuid4().hex[:8]}",
        download_speed=10_000,
        upload_speed=10_000,
        mikrotik_rate_limit="10000k/10000k",
        is_active=True,
    )
    db_session.add(profile)
    db_session.flush()

    offer = CatalogOffer(
        name=f"Homeflex Basic {uuid4().hex[:6]}",
        code=f"hf-{uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_family="home_flex",
        speed_download_mbps=10,
        speed_upload_mbps=10,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(OfferRadiusProfile(offer_id=offer.id, profile_id=profile.id))
    db_session.commit()
    return offer, profile


def _context(offer_id) -> CommandContext:
    return fup_command_context(str(offer_id), "test_admin_ui_fup_design")


def _configure_free_night(db_session, offer_id) -> None:
    """What an admin submits on the 'Accounting of Traffic' form."""
    db_session_adapter.release_read_transaction(db_session)
    handle_policy_update(
        db_session,
        str(offer_id),
        FormData(
            [
                ("traffic_accounting_start", DAY_START),
                ("traffic_accounting_end", DAY_END),
            ]
        ),
        _context(offer_id),
    )
    db_session.commit()


def _add_warn_rule(db_session, offer_id, *, threshold_gb: int) -> None:
    db_session_adapter.release_read_transaction(db_session)
    handle_add_rule(
        db_session,
        str(offer_id),
        FormData(
            [
                ("name", "Warn at 80%"),
                ("consumption_period", "daily"),
                ("direction", "up_down"),
                ("threshold_amount", str(threshold_gb)),
                ("threshold_unit", "gb"),
                ("action", "notify"),
                ("sort_order", "1"),
                ("is_active", "on"),
            ]
        ),
        _context(offer_id),
    )
    db_session.commit()


def _add_throttle_rule(db_session, offer_id, *, threshold_gb: int) -> None:
    db_session_adapter.release_read_transaction(db_session)
    handle_add_rule(
        db_session,
        str(offer_id),
        FormData(
            [
                ("name", "Throttle at 100%"),
                ("consumption_period", "daily"),
                ("direction", "up_down"),
                ("threshold_amount", str(threshold_gb)),
                ("threshold_unit", "gb"),
                ("action", "reduce_speed"),
                ("speed_reduction_percent", "50"),
                ("time_start", DAY_START),
                ("time_end", DAY_END),
                ("sort_order", "2"),
                ("is_active", "on"),
            ]
        ),
        _context(offer_id),
    )
    db_session.commit()


def _policy(db_session, offer_id) -> FupPolicy:
    return fup_policies.get_by_offer(db_session, str(offer_id))


# --- the gate the operator must pass through first ---------------------------


def test_a_daily_rule_is_refused_until_the_module_is_enabled(
    db_session, homeflex_offer
):
    """The one prerequisite that is not a defect.

    Sub-monthly usage is samples-derived rather than billing-grade, so the
    screen refuses a daily rule until an operator has switched on
    usage.fup_submonthly_rules deliberately.
    """
    from fastapi import HTTPException

    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()

    with pytest.raises(HTTPException) as caught:
        _add_throttle_rule(db_session, offer.id, threshold_gb=5)
    assert caught.value.status_code == 400
    assert "fup_submonthly_rules" in str(caught.value.detail)
    db_session.rollback()


# --- configuring §1 from the screen ------------------------------------------


def test_the_full_design_persists_exactly_as_entered(
    db_session, homeflex_offer, submonthly_enabled
):
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()

    _configure_free_night(db_session, offer.id)
    _add_warn_rule(db_session, offer.id, threshold_gb=4)
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    policy = _policy(db_session, offer.id)
    assert policy.traffic_accounting_start == time(5, 0)
    assert policy.traffic_accounting_end == time(22, 0)

    rules = {rule.name: rule for rule in policy.rules}
    assert set(rules) == {"Warn at 80%", "Throttle at 100%"}

    warn = rules["Warn at 80%"]
    assert warn.consumption_period is FupConsumptionPeriod.daily
    assert warn.action is FupAction.notify
    assert warn.threshold_amount == 4

    throttle = rules["Throttle at 100%"]
    assert throttle.consumption_period is FupConsumptionPeriod.daily
    assert throttle.action is FupAction.reduce_speed
    assert throttle.speed_reduction_percent == 50
    assert throttle.time_start == time(5, 0)
    assert throttle.time_end == time(22, 0)


def test_no_block_stage_is_needed_or_created(
    db_session, homeflex_offer, submonthly_enabled
):
    """§1 has no block stage — a daily bucket is self-healing."""
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _configure_free_night(db_session, offer.id)
    _add_warn_rule(db_session, offer.id, threshold_gb=4)
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    actions = {rule.action for rule in _policy(db_session, offer.id).rules}
    assert FupAction.block not in actions


# --- and the engine honours it -----------------------------------------------


def _usage(period: str, used_gb: float, now: datetime) -> dict:
    return {
        period: FupUsageWindow(
            used_gb=used_gb,
            window=fup_window_bounds(period, now),
            source="test",
            is_authoritative=True,
        )
    }


def test_the_configured_throttle_fires_during_the_day(
    db_session, homeflex_offer, submonthly_enabled
):
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _configure_free_night(db_session, offer.id)
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    results = evaluate_rules(
        db_session,
        str(offer.id),
        current_usage_gb=6.0,
        current_time=DAYTIME,
        usage_by_period=_usage("daily", 6.0, DAYTIME),
        tz=__import__("zoneinfo").ZoneInfo(LAGOS),
    )
    throttle = next(r for r in results if r["name"] == "Throttle at 100%")
    assert throttle["triggered"] is True, throttle.get("reason")


def test_the_configured_throttle_is_dormant_at_night(
    db_session, homeflex_offer, submonthly_enabled
):
    """Same usage, 22:30 local: the rule must not fire during the free night."""
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _configure_free_night(db_session, offer.id)
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    results = evaluate_rules(
        db_session,
        str(offer.id),
        current_usage_gb=6.0,
        current_time=NIGHT,
        usage_by_period=_usage("daily", 6.0, NIGHT),
        tz=__import__("zoneinfo").ZoneInfo(LAGOS),
    )
    throttle = next(r for r in results if r["name"] == "Throttle at 100%")
    assert throttle["triggered"] is False
    assert throttle["status"] == "time_skip"


def test_the_configured_accounting_window_excludes_night_traffic(
    db_session, homeflex_offer, submonthly_enabled
):
    """The window entered on the screen actually removes hours from accrual."""
    from zoneinfo import ZoneInfo

    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _configure_free_night(db_session, offer.id)

    policy = _policy(db_session, offer.id)
    tz = ZoneInfo(LAGOS)
    window = fup_window_bounds("daily", DAYTIME, tz)
    spans = accrual_intervals(
        window,
        tz,
        policy.traffic_accounting_start,
        policy.traffic_accounting_end,
        policy.traffic_inverse_interval,
    )
    counted = sum((end - start).total_seconds() for start, end in spans)
    assert counted == 17 * 3600, "the free night must not fill the bucket"


def test_the_configured_percentage_produces_a_proportional_profile(
    db_session, homeflex_offer, submonthly_enabled, subscriber
):
    """50 entered on the screen must reach the wire as half the plan's rate."""
    offer, profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        next_billing_at=datetime.now(UTC),
    )
    db_session.add(subscription)
    db_session.commit()

    rule = next(
        r
        for r in _policy(db_session, offer.id).rules
        if r.action is FupAction.reduce_speed
    )
    fallback = RadiusProfile(
        name="FUP Throttle 1Mbps",
        code=f"fallback-{uuid4().hex[:8]}",
        download_speed=1024,
        upload_speed=1024,
        is_active=True,
    )
    db_session.add(fallback)
    db_session.flush()

    decision = resolve_fup_throttle_profile(
        db_session,
        subscription=subscription,
        rule_id=str(rule.id),
        fallback_profile_id=fallback.id,
    )
    assert decision.derived is True, decision.reason
    # Half of the subscriber's own 10 Mbps — not the flat 1 Mbps fallback.
    assert decision.download_kbps == 5_000
    assert decision.upload_kbps == 5_000
    assert decision.profile_id != fallback.id


def test_an_admin_can_read_back_what_they_configured(
    db_session, homeflex_offer, submonthly_enabled
):
    """The screen re-renders from these fields, so they must round-trip.

    A value that saves but does not read back is how an operator ends up
    reconfiguring the same thing repeatedly and believing it never took.
    """
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _configure_free_night(db_session, offer.id)
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    db_session.expire_all()
    policy = _policy(db_session, offer.id)
    assert policy.traffic_accounting_start.strftime("%H:%M") == DAY_START
    assert policy.traffic_accounting_end.strftime("%H:%M") == DAY_END
    throttle = next(r for r in policy.rules if r.action is FupAction.reduce_speed)
    assert throttle.time_start.strftime("%H:%M") == DAY_START
    assert throttle.time_end.strftime("%H:%M") == DAY_END
    assert throttle.speed_reduction_percent == 50


# --- the screen itself renders, and says what the fields mean ----------------


class _State:
    csrf_token = "test-csrf-token"
    auth: dict = {"permission_keys": {"*"}}


class _URL:
    path = "/admin/catalog/offers/x/fup"

    def __str__(self) -> str:
        return self.path


class DummyRequest:
    state = _State()
    query_params: dict = {}
    headers: dict = {}
    cookies: dict = {}
    url = _URL()
    session: dict = {}
    client = None
    scope: dict = {}

    def url_for(self, *args, **kwargs) -> str:
        return "/"


def _render_fup_page(db_session, offer) -> str:
    """Render the real FUP page through the environment the route uses."""
    from app.services import web_fup as web_fup_service
    from app.web.admin.catalog import templates

    request = DummyRequest()
    context = {
        "request": request,
        "active_page": "catalog",
        "active_menu": "operations",
        "current_user": {"name": "Test Admin", "email": "admin@example.com"},
        "sidebar_stats": {},
        "fup_return_to": "/admin/catalog/offers/x/fup",
    }
    context.update(web_fup_service.fup_context(request, db_session, str(offer.id)))
    html = templates.env.get_template("admin/catalog/fup.html").render(**context)
    assert html.strip()
    return html


def test_the_fup_page_renders_with_the_design_configured(
    db_session, homeflex_offer, submonthly_enabled
):
    """A template edit that breaks Jinja must not reach an operator."""
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _configure_free_night(db_session, offer.id)
    _add_warn_rule(db_session, offer.id, threshold_gb=4)
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    html = _render_fup_page(db_session, offer)
    # The configured values are visible on the page an admin returns to.
    assert 'value="05:00"' in html
    assert 'value="22:00"' in html
    assert "Throttle 50%" in html or "50%" in html


def test_the_screen_offers_a_daily_bucket(db_session, homeflex_offer):
    """§1 needs a daily period; the dropdown must actually offer one."""
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    html = _render_fup_page(db_session, offer)
    assert 'value="daily"' in html


def test_the_screen_disambiguates_the_two_things_an_admin_must_not_guess(
    db_session, homeflex_offer
):
    """Bare labels are how a free night gets configured backwards.

    "Speed Reduction %" does not say whether 50 means cut-by-half or
    reduce-to-half, and two different time windows on one screen do not say
    which governs accrual and which governs enforcement. Both are stated.
    """
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    # Collapse whitespace: the template wraps these sentences across lines.
    html = " ".join(_render_fup_page(db_session, offer).split())

    assert "not the speed they are left with" in html, (
        "the reduction field must say it is a cut, not the retained speed"
    )
    assert "traffic <strong>counts</strong> towards the allowance" in html, (
        "the accounting window must say it governs accrual"
    )
    assert "this rule <strong>enforces</strong>" in html, (
        "the rule window must say it governs enforcement"
    )
    assert "subscriber's local clock" in html.replace("&#39;", "'"), (
        "both windows must say whose clock they are read on"
    )


# --- the gap the UI cannot see ----------------------------------------------


def test_a_daily_rule_evaluates_without_a_monthly_quota_bucket(
    db_session, homeflex_offer, submonthly_enabled, subscriber, monkeypatch
):
    """home_flex has no usage allowance, so it gets no QuotaBucket — and a
    daily rule must still be evaluated.

    Quota buckets are only created for offers with usage_allowance_id set
    (``usage.meter_active_subscriptions``). home_flex deliberately has none:
    the allowance is a BILLING object and FUP is enforcement (§1). But
    enforcement bailed out entirely when no bucket existed, so a perfectly
    configured daily ladder on home_flex would never fire — the exact failure
    mode this branch exists to remove, one layer further down.

    A daily rule reads its usage from the windowed reader, not the bucket, so
    the bucket is only genuinely required by MONTHLY rules.
    """
    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _configure_free_night(db_session, offer.id)
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        next_billing_at=datetime.now(UTC),
    )
    db_session.add(subscription)
    db_session.commit()

    # No QuotaBucket exists for this subscription — as in production.
    from app.models.usage import QuotaBucket

    assert (
        db_session.query(QuotaBucket)
        .filter(QuotaBucket.subscription_id == subscription.id)
        .count()
        == 0
    )

    evaluated = {}

    def _spy(db, offer_id, **kwargs):
        evaluated["called"] = True
        return []

    monkeypatch.setattr("app.services.fup.evaluate_rules", _spy)

    from app.services.fup_enforcement import (
        EvaluateFupSubscriptionCommand,
        evaluate_fup_subscription,
    )

    # Read the id BEFORE releasing: touching an expired ORM attribute issues a
    # refresh SELECT, which reopens the transaction the owner command forbids.
    subscription_id = subscription.id
    db_session.commit()
    db_session_adapter.release_read_transaction(db_session)
    evaluate_fup_subscription(
        db_session,
        EvaluateFupSubscriptionCommand(
            context=fup_command_context(str(subscription_id), "test_no_bucket"),
            subscription_id=subscription_id,
            evaluated_at=DAYTIME,
            warning_enabled=False,
            warning_ratio=0.8,
            throttle_profile_configured=True,
        ),
    )

    assert evaluated.get("called"), (
        "enforcement skipped a subscription with daily rules because it had no "
        "monthly quota bucket; home_flex never has one"
    )


def test_the_approaching_warning_measures_the_rule_own_window(
    db_session, homeflex_offer, submonthly_enabled
):
    """A daily rule must be warned against a day of traffic, not a month.

    The sweep's "approaching limit" warning divided the MONTHLY bucket by each
    rule's threshold regardless of that rule's period. Against a daily 5 GB
    cap, a normal month of traffic gives a ratio far above 1.0, so the warning
    silently never fired for the very ladder §1 specifies.

    evaluate_rules already computes usage_percent from each rule's own window;
    this pins that the two agree.
    """
    from zoneinfo import ZoneInfo

    offer, _profile = homeflex_offer
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    _add_throttle_rule(db_session, offer.id, threshold_gb=5)

    # 4.25 GB of a 5 GB daily cap is 85% — inside the 80% warning band —
    # while the month stands at 50 GB, which is 10x the daily threshold.
    results = evaluate_rules(
        db_session,
        str(offer.id),
        current_usage_gb=50.0,
        current_time=DAYTIME,
        usage_by_period=_usage("daily", 4.25, DAYTIME),
        tz=ZoneInfo(LAGOS),
    )
    row = next(r for r in results if r["name"] == "Throttle at 100%")
    assert row["triggered"] is False
    ratio = row["usage_percent"] / 100.0
    assert 0.8 <= ratio < 1.0, (
        "the rule's own window puts it in the warning band; the monthly figure "
        f"would have given {50.0 / 5:.1f}"
    )
