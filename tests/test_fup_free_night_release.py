"""A free night must LIFT the throttle, not merely stop re-applying it.

The only release path used to be ``FupState.cap_resets_at`` — the period
boundary, which for a daily rule is local midnight. So a rule whose window
closed at 22:00 stopped firing but left the subscriber throttled for the first
two hours of the night they were told was free. See
PLAN_FAMILY_ARCHITECTURE §12.

The release is deliberately narrow: it triggers only when the rule that CAUSED
the current enforcement is itself outside its window. These tests pin both
halves of that — it releases when it should, and it does not release for any
other reason a rule happens to stop triggering.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.fup_state import FupActionStatus
from app.models.usage import QuotaBucket
from app.services import fup_enforcement
from app.services.fup_enforcement import (
    EvaluateFupSubscriptionCommand,
    evaluate_fup_subscription,
)
from app.services.fup_state import ApplyFupRuntimeState, fup_state
from app.services.owner_commands import CommandContext
from tests.fup_helpers import add_fup_rule, ensure_fup_policy

# 22:30 Lagos — inside a free night that runs 22:00-05:00, so a rule whose
# enforcing window is 05:00-22:00 is outside it.
NIGHT = datetime(2026, 6, 21, 21, 30, tzinfo=UTC)
# 14:00 Lagos — squarely inside the enforcing window.
DAYTIME = datetime(2026, 6, 21, 13, 0, tzinfo=UTC)


def _context(subscription_id) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="test:fup",
        scope=str(subscription_id),
        reason="test_free_night_release",
    )


def _command(subscription_id, evaluated_at) -> EvaluateFupSubscriptionCommand:
    return EvaluateFupSubscriptionCommand(
        context=_context(subscription_id),
        subscription_id=subscription_id,
        evaluated_at=evaluated_at,
        warning_enabled=False,
        warning_ratio=0.8,
        throttle_profile_configured=True,
    )


@pytest.fixture
def throttled_subscription(db_session, subscriber):
    """A subscriber throttled by a rule that only enforces 05:00-22:00 local."""
    offer = CatalogOffer(
        name="homeflex-nighttest",
        code=f"hf-night-{uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        speed_download_mbps=10,
        speed_upload_mbps=10,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()

    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        next_billing_at=datetime.now(UTC),
    )
    db_session.add(sub)
    db_session.flush()

    db_session.add(
        QuotaBucket(
            subscription_id=sub.id,
            period_start=datetime(2026, 6, 1, tzinfo=UTC),
            period_end=datetime(2026, 7, 1, tzinfo=UTC),
            included_gb=Decimal("10.00"),
            used_gb=Decimal("50.00"),  # well over every threshold below
            rollover_gb=Decimal("0.00"),
            overage_gb=Decimal("0.00"),
        )
    )
    db_session.commit()

    ensure_fup_policy(db_session, str(offer.id))
    rule = add_fup_rule(
        db_session,
        str(offer.id),
        name="Throttle at 100% (day only)",
        consumption_period="monthly",
        direction="down",
        threshold_amount=10,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=50,
        time_start=time(5, 0),
        time_end=time(22, 0),
        sort_order=1,
    )
    db_session.commit()

    # Put the subscriber in the throttled state that rule would have produced.
    fup_state.apply_action(
        db_session,
        ApplyFupRuntimeState(
            subscription_id=sub.id,
            offer_id=offer.id,
            rule_id=rule.id,
            action_status=FupActionStatus.throttled,
            evaluated_at=DAYTIME,
            notes="test fixture: throttled during the day",
        ),
    )
    db_session.commit()
    return sub, offer, rule


def _evaluate(db_session, subscription_id, evaluated_at):
    """Enter the owner command the way production does — no open read txn."""
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db_session)
    return evaluate_fup_subscription(
        db_session, _command(subscription_id, evaluated_at)
    )


def _lift_calls(monkeypatch):
    """Record lift_fup_enforcement calls without touching RADIUS."""
    calls = []

    def _fake_lift(db, subscription_id, *, evaluated_at):
        calls.append(str(subscription_id))
        return {"lifted": True, "actions": ["profile_restored"]}

    import app.services.enforcement as enforcement_module

    monkeypatch.setattr(enforcement_module, "lift_fup_enforcement", _fake_lift)
    return calls


def test_throttle_lifts_when_its_rule_leaves_its_window(
    db_session, throttled_subscription, monkeypatch, subscriber_lagos_tz
):
    sub, _offer, _rule = throttled_subscription
    calls = _lift_calls(monkeypatch)

    outcome = _evaluate(db_session, sub.id, NIGHT)

    assert calls == [str(sub.id)], (
        "at 22:30 local the enforcing rule is outside its 05:00-22:00 window, "
        "so the throttle must be released rather than left until midnight"
    )
    assert outcome.reset == 1


def test_throttle_stays_on_inside_the_window(
    db_session, throttled_subscription, monkeypatch, subscriber_lagos_tz
):
    sub, _offer, _rule = throttled_subscription
    calls = _lift_calls(monkeypatch)

    _evaluate(db_session, sub.id, DAYTIME)

    assert calls == [], "14:00 local is inside the window; nothing should lift"


def test_an_unthrottled_subscriber_is_not_lifted_at_night(
    db_session, throttled_subscription, monkeypatch, subscriber_lagos_tz
):
    """The release is scoped to active enforcement, not to every evaluation."""
    sub, _offer, _rule = throttled_subscription
    from app.services.fup_state import ClearFupRuntimeState

    fup_state.clear(
        db_session,
        ClearFupRuntimeState(subscription_id=sub.id, evaluated_at=DAYTIME),
    )
    db_session.commit()
    calls = _lift_calls(monkeypatch)

    _evaluate(db_session, sub.id, NIGHT)

    assert calls == [], "nothing was enforced, so there is nothing to lift"


@pytest.fixture
def subscriber_lagos_tz(monkeypatch):
    """Pin the subscriber timezone so the window is read on a Lagos clock."""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(
        fup_enforcement,
        "_subscriber_tz",
        lambda db, subscriber_id: ZoneInfo("Africa/Lagos"),
        raising=False,
    )
    import app.services.usage_summary as usage_summary

    monkeypatch.setattr(
        usage_summary,
        "_subscriber_tz",
        lambda db, subscriber_id: ZoneInfo("Africa/Lagos"),
    )
