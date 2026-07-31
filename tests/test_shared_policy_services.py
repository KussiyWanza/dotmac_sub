"""Contracts for shared billing/access policy services."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.models.catalog import AccessState, BillingMode, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.services.billing_communication_policy import billing_communication_decisions
from app.services.billing_profile import (
    BillingProfile,
    BillingProfileReason,
    BillingProfileSource,
    plan_billing_mode_transition,
    plan_subscription_billing_mode_write,
)
from app.services.customer_reporting_policy import active_customer_subscription_count
from app.services.radius_projection_planner import plan_radius_projection
from app.services.subscription_lifecycle_policy import (
    BILLING_COLLECTIBLE_SERVICE_STATUSES,
    MRR_COUNTABLE_SERVICE_STATUSES,
    PORTAL_VISIBLE_SERVICE_STATUSES,
    RADIUS_PROJECTABLE_SERVICE_STATUSES,
    TERMINAL_SERVICE_STATUSES,
    is_customer_impact_service_status,
    is_mrr_countable_service_status,
)
from tests.test_customer_plan_change_prepaid import _make_offer, _make_subscription


def _subscription_object(
    *,
    subscriber_status=SubscriberStatus.active,
    subscription_status=SubscriptionStatus.active,
    billing_mode=BillingMode.postpaid,
):
    subscriber_id = uuid.uuid4()
    subscriber = SimpleNamespace(
        id=subscriber_id,
        status=subscriber_status,
        is_active=True,
        billing_enabled=True,
        billing_mode=billing_mode,
        captive_redirect_enabled=False,
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        subscriber_id=subscriber_id,
        status=subscription_status,
        billing_mode=billing_mode,
        subscriber=subscriber,
    )


def test_radius_projection_planner_parent_block_overrides_active_subscription():
    subscription = _subscription_object(subscriber_status=SubscriberStatus.disabled)

    plan = plan_radius_projection(subscription)

    assert plan.mode == "reject"
    assert plan.access_state == AccessState.suspended
    assert plan.blocked is True
    assert plan.write_password is False
    assert plan.write_radreply is False
    assert plan.block_reason == "subscriber_status_disabled"


def test_billing_mode_transition_blocks_mixed_collectible_modes():
    profile = BillingProfile(
        account_id=uuid.uuid4(),
        account_mode=BillingMode.prepaid,
        subscription_modes=frozenset({BillingMode.prepaid, BillingMode.postpaid}),
        effective_mode=None,
        source=BillingProfileSource.MIXED_SUBSCRIPTIONS,
        account_subscription_mismatch=True,
        invalid_reason=(
            BillingProfileReason.MIXED_COLLECTIBLE_SUBSCRIPTION_BILLING_MODES
        ),
    )

    decision = plan_billing_mode_transition(profile, BillingMode.postpaid)

    assert decision.allowed is False
    assert decision.requires_subscription_alignment is True
    assert decision.reason == "mixed_collectible_subscription_billing_modes"


def test_subscription_billing_mode_write_rejects_account_offer_drift():
    decision = plan_subscription_billing_mode_write(
        account_mode=BillingMode.prepaid,
        offer_mode=BillingMode.postpaid,
        requested_mode=None,
    )

    assert decision.allowed is False
    assert decision.reason == "account_offer_billing_mode_mismatch"


def test_subscription_billing_mode_write_rejects_explicit_override():
    decision = plan_subscription_billing_mode_write(
        account_mode=BillingMode.prepaid,
        offer_mode=BillingMode.prepaid,
        requested_mode=BillingMode.postpaid,
    )

    assert decision.allowed is False
    assert decision.reason == "requested_billing_mode_mismatch"


def test_subscription_lifecycle_status_sets_are_named_by_workflow():
    assert SubscriptionStatus.active in PORTAL_VISIBLE_SERVICE_STATUSES
    assert SubscriptionStatus.stopped in PORTAL_VISIBLE_SERVICE_STATUSES
    assert SubscriptionStatus.blocked in BILLING_COLLECTIBLE_SERVICE_STATUSES
    assert SubscriptionStatus.blocked in RADIUS_PROJECTABLE_SERVICE_STATUSES
    assert SubscriptionStatus.active in MRR_COUNTABLE_SERVICE_STATUSES
    assert SubscriptionStatus.suspended not in MRR_COUNTABLE_SERVICE_STATUSES
    assert is_customer_impact_service_status(SubscriptionStatus.active) is True
    assert is_customer_impact_service_status(SubscriptionStatus.suspended) is False
    assert is_mrr_countable_service_status(SubscriptionStatus.active) is True
    assert is_mrr_countable_service_status(SubscriptionStatus.suspended) is False
    assert SubscriptionStatus.canceled in TERMINAL_SERVICE_STATUSES


def test_billing_communication_policy_batches_outage_and_ticket_suppression(
    db_session, monkeypatch
):
    outage = SimpleNamespace(id="sub-outage", subscriber_id="acct-outage")
    ticket = SimpleNamespace(id="sub-ticket", subscriber_id="acct-ticket")
    clean = SimpleNamespace(id="sub-clean", subscriber_id="acct-clean")
    monkeypatch.setattr(
        "app.services.billing_communication_policy.active_outage_subscription_ids",
        lambda session, manual_open_max_hours=None: {"sub-outage"},
    )
    monkeypatch.setattr(
        "app.services.billing_communication_policy.subscribers_with_open_infrastructure_down_tickets",
        lambda session, subscriber_ids, max_age_hours=None: {"acct-ticket"},
    )

    decisions = billing_communication_decisions(db_session, [outage, ticket, clean])

    assert decisions["sub-outage"].suppress_dunning_notice is True
    assert decisions["sub-outage"].reason == "active_infrastructure_outage"
    assert decisions["sub-ticket"].suppress_suspension_notice is True
    assert decisions["sub-ticket"].reason == "open_infrastructure_down_ticket"
    assert decisions["sub-clean"].suppress_expiry_notice is False


def test_customer_reporting_counts_active_customer_subscriptions(db_session):
    offer = _make_offer(
        db_session,
        name="Reporting Plan",
        amount=Decimal("100.00"),
        plan_family="unlimited",
    )
    active_customer = Subscriber(
        first_name="Active",
        last_name="Customer",
        email="report-active@example.com",
        user_type=UserType.customer,
        status=SubscriberStatus.active,
        is_active=True,
    )
    disabled_customer = Subscriber(
        first_name="Disabled",
        last_name="Customer",
        email="report-disabled@example.com",
        user_type=UserType.customer,
        status=SubscriberStatus.disabled,
        is_active=True,
    )
    system_account = Subscriber(
        first_name="System",
        last_name="Account",
        email="report-system@example.com",
        user_type=UserType.system_user,
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add_all([active_customer, disabled_customer, system_account])
    db_session.commit()
    kwargs = {
        "next_billing_at": datetime.now(UTC) + timedelta(days=10),
        "start_at": datetime.now(UTC) - timedelta(days=20),
    }
    _make_subscription(db_session, active_customer, offer, **kwargs)
    _make_subscription(db_session, disabled_customer, offer, **kwargs)
    _make_subscription(db_session, system_account, offer, **kwargs)
    db_session.commit()

    assert active_customer_subscription_count(db_session) == 1
