"""Canonical subscription lifecycle state and command preview contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import SubscriberStatus
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcome,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecycleError,
    SubscriptionLifecycleHeadConflict,
    SubscriptionSessionAction,
    assert_subscription_transition,
    preview_subscription_command,
    resolve_subscription_lifecycle,
)


def _subscription(
    db_session,
    subscriber,
    offer,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
    billing_mode: BillingMode = BillingMode.postpaid,
) -> Subscription:
    item = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        billing_mode=billing_mode,
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db_session.add(item)
    db_session.flush()
    return item


def _target_offer(db_session, current: CatalogOffer) -> CatalogOffer:
    current.plan_family = "unlimited"
    current.billing_mode = BillingMode.postpaid
    target = CatalogOffer(
        name="Faster Internet",
        code="FAST-INT",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.postpaid,
        plan_family="unlimited",
    )
    db_session.add(target)
    db_session.flush()
    db_session.add_all(
        [
            OfferPrice(
                offer_id=current.id,
                price_type=PriceType.recurring,
                amount=Decimal("10000.00"),
                currency="NGN",
            ),
            OfferPrice(
                offer_id=target.id,
                price_type=PriceType.recurring,
                amount=Decimal("15000.00"),
                currency="NGN",
            ),
        ]
    )
    db_session.flush()
    return target


def test_state_resolver_combines_account_billing_and_radius_truth(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    subscriber.status = SubscriberStatus.disabled
    subscriber.is_active = False
    db_session.flush()

    snapshot = resolve_subscription_lifecycle(db_session, str(subscription.id))

    assert snapshot.account_status == SubscriberStatus.disabled.value
    assert snapshot.account_enabled is False
    assert snapshot.state.status == SubscriptionStatus.active.value
    assert snapshot.state.billing_collectible is False
    assert snapshot.state.radius_allowed is False
    assert snapshot.state.radius_blocked is True
    assert snapshot.state.access_block_reason == "subscriber_inactive"
    assert len(snapshot.head) == 64


def test_activate_preview_exposes_proposed_access_and_confirmation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.pending,
    )
    command = SubscriptionLifecycleCommand(
        subscription_id=str(subscription.id),
        kind=SubscriptionCommandKind.activate,
        source="admin:test",
    )

    preview = preview_subscription_command(
        db_session,
        command,
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert preview.eligible is True
    assert preview.current.state.status == SubscriptionStatus.pending.value
    assert preview.proposed.status == SubscriptionStatus.active.value
    assert preview.access_impact.allowed_before is False
    assert preview.access_impact.allowed_after is True
    assert preview.access_impact.session_action == SubscriptionSessionAction.authorize
    assert preview.billing_impact.action == "start_or_resume_collection"
    assert preview.requires_confirmation is True


def test_cancel_preview_stops_collection_and_deprovisions_access(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            reason="customer requested cancellation",
        ),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert preview.eligible is True
    assert preview.proposed.status == SubscriptionStatus.canceled.value
    assert preview.proposed.terminal is True
    assert preview.billing_impact.collectible_before is True
    assert preview.billing_impact.collectible_after is False
    assert preview.billing_impact.possible_prorated_credit is True
    assert preview.access_impact.session_action == SubscriptionSessionAction.deprovision
    assert preview.access_impact.proposed_state == "terminated"


def test_disable_preview_pauses_billing_and_preserves_reactivation(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.disable,
            source="admin:test",
            reason="administrative pause",
        ),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert preview.eligible is True
    assert preview.proposed.status == SubscriptionStatus.disabled.value
    assert preview.proposed.terminal is False
    assert preview.billing_impact.action == "stop_collection"
    assert preview.billing_impact.collectible_before is True
    assert preview.billing_impact.collectible_after is False
    assert preview.billing_impact.possible_prorated_credit is False
    assert preview.access_impact.session_action == SubscriptionSessionAction.disconnect
    assert preview.access_impact.proposed_state == "suspended"


@pytest.mark.parametrize(
    ("current_status", "kind", "proposed_status", "session_action"),
    [
        (
            SubscriptionStatus.active,
            SubscriptionCommandKind.suspend,
            SubscriptionStatus.suspended,
            SubscriptionSessionAction.disconnect,
        ),
        (
            SubscriptionStatus.suspended,
            SubscriptionCommandKind.restore,
            SubscriptionStatus.active,
            SubscriptionSessionAction.authorize,
        ),
        (
            SubscriptionStatus.disabled,
            SubscriptionCommandKind.restore,
            SubscriptionStatus.active,
            SubscriptionSessionAction.authorize,
        ),
        (
            SubscriptionStatus.active,
            SubscriptionCommandKind.expire,
            SubscriptionStatus.expired,
            SubscriptionSessionAction.deprovision,
        ),
    ],
)
def test_status_command_previews_share_one_transition_projection(
    db_session,
    subscriber,
    catalog_offer,
    current_status,
    kind,
    proposed_status,
    session_action,
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=current_status,
    )

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=kind,
            source="admin:test",
        ),
    )

    assert preview.eligible is True
    assert preview.current.state.status == current_status.value
    assert preview.proposed.status == proposed_status.value
    assert preview.access_impact.session_action == session_action


@pytest.mark.parametrize(
    "current_status",
    [
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
    ],
)
def test_suspend_preview_preserves_suspended_equivalent_status(
    db_session,
    subscriber,
    catalog_offer,
    current_status,
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=current_status,
    )

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.suspend,
            source="enforcement:test",
        ),
    )

    assert preview.eligible is True
    assert preview.proposed.status == current_status.value
    assert preview.access_impact.session_action == SubscriptionSessionAction.disconnect


def test_terminal_service_cannot_be_reactivated_or_previewed_as_eligible(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.canceled,
    )

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.activate,
            source="admin:test",
        ),
    )

    assert preview.eligible is False
    assert preview.eligibility_reasons == ("status_canceled_not_eligible_for_activate",)
    with pytest.raises(SubscriptionLifecycleError, match="terminal"):
        assert_subscription_transition(
            SubscriptionStatus.canceled,
            SubscriptionStatus.active,
        )


def test_preview_rejects_stale_review_head(db_session, subscriber, catalog_offer):
    subscription = _subscription(db_session, subscriber, catalog_offer)

    with pytest.raises(SubscriptionLifecycleHeadConflict, match="refresh"):
        preview_subscription_command(
            db_session,
            SubscriptionLifecycleCommand(
                subscription_id=str(subscription.id),
                kind=SubscriptionCommandKind.cancel,
                source="admin:test",
                expected_head="stale-review-head",
            ),
        )


def test_review_head_changes_with_account_and_billing_inputs(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    before = resolve_subscription_lifecycle(db_session, str(subscription.id))

    subscriber.is_active = False
    subscription.next_billing_at = datetime(2026, 9, 1, tzinfo=UTC)
    db_session.flush()
    after = resolve_subscription_lifecycle(db_session, str(subscription.id))

    assert after.head != before.head
    with pytest.raises(SubscriptionLifecycleHeadConflict, match="refresh"):
        preview_subscription_command(
            db_session,
            SubscriptionLifecycleCommand(
                subscription_id=str(subscription.id),
                kind=SubscriptionCommandKind.suspend,
                source="admin:test",
                expected_head=before.head,
            ),
        )


def test_review_head_ignores_derived_access_projection(
    db_session, subscriber, catalog_offer
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    before = resolve_subscription_lifecycle(db_session, str(subscription.id))

    subscription.access_state = "active"
    db_session.flush()
    after = resolve_subscription_lifecycle(db_session, str(subscription.id))

    assert after.head == before.head


def test_review_head_changes_when_pending_plan_change_is_created(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    before = resolve_subscription_lifecycle(db_session, str(subscription.id))
    db_session.add(
        SubscriptionChangeRequest(
            subscription_id=subscription.id,
            current_offer_id=catalog_offer.id,
            requested_offer_id=target.id,
            status=SubscriptionChangeStatus.approved,
            effective_date=date(2026, 8, 1),
        )
    )
    db_session.flush()

    after = resolve_subscription_lifecycle(db_session, str(subscription.id))

    assert after.head != before.head


def test_plan_change_preview_uses_canonical_proration_and_access_impact(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.change_plan,
            source="admin:test",
            target_offer_id=str(target.id),
            # The pricing owner keeps its own clock unless the command
            # carries an explicit effective time; without it the quote uses
            # wall clock and this pinned mid-cycle preview breaks once the
            # real date passes next_billing_at (2026-08-01).
            effective_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        ),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert preview.eligible is True
    assert preview.proposed.offer_id == str(target.id)
    assert preview.billing_impact.action == "apply_prorated_plan_change"
    assert preview.billing_impact.currency == "NGN"
    assert preview.billing_impact.net_amount > Decimal("0.00")
    assert preview.billing_impact.details
    assert "quote" in preview.billing_impact.details
    assert preview.access_impact.session_action == SubscriptionSessionAction.reauthorize


def test_next_cycle_plan_change_has_no_immediate_charge(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.change_plan,
            source="admin:test",
            target_offer_id=str(target.id),
            effective_timing=SubscriptionEffectiveTiming.next_cycle,
        ),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )

    assert preview.eligible is True
    assert preview.effective_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert preview.billing_impact.action == (
        "replace_recurring_price_at_effective_date"
    )
    assert preview.billing_impact.net_amount == Decimal("0.00")
    assert preview.billing_impact.required_amount == Decimal("15000.00")


def test_renewal_preview_is_billing_owned_and_uses_recurring_price(
    db_session, subscriber, catalog_offer
):
    catalog_offer.billing_mode = BillingMode.prepaid
    db_session.add(
        OfferPrice(
            offer_id=catalog_offer.id,
            price_type=PriceType.recurring,
            amount=Decimal("12000.00"),
            currency="NGN",
        )
    )
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        billing_mode=BillingMode.prepaid,
    )
    db_session.flush()

    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.renew,
            source="billing:test",
        ),
    )

    assert preview.eligible is True
    assert preview.proposed.status == SubscriptionStatus.active.value
    assert preview.effective_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert preview.billing_impact.action == "billing_owned_renewal"
    assert preview.billing_impact.required_amount == Decimal("12000.00")
    assert preview.access_impact.session_action == SubscriptionSessionAction.none


def test_outstanding_change_is_visible_and_blocks_duplicate_plan_command(
    db_session, subscriber, catalog_offer
):
    target = _target_offer(db_session, catalog_offer)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    request = SubscriptionChangeRequest(
        subscription_id=subscription.id,
        current_offer_id=catalog_offer.id,
        requested_offer_id=target.id,
        status=SubscriptionChangeStatus.approved,
        effective_date=date(2026, 8, 1),
    )
    db_session.add(request)
    db_session.flush()

    snapshot = resolve_subscription_lifecycle(db_session, str(subscription.id))
    preview = preview_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.change_plan,
            source="admin:test",
            target_offer_id=str(target.id),
            effective_timing=SubscriptionEffectiveTiming.next_cycle,
        ),
    )

    assert snapshot.pending_change is not None
    assert snapshot.pending_change.request_id == str(request.id)
    assert snapshot.pending_change.target_offer_name == target.name
    assert preview.eligible is False
    assert "outstanding_plan_change_exists" in preview.eligibility_reasons


def test_command_contract_requires_target_and_scheduled_date() -> None:
    with pytest.raises(SubscriptionLifecycleError, match="target_offer_id"):
        SubscriptionLifecycleCommand(
            subscription_id="sub-1",
            kind=SubscriptionCommandKind.change_plan,
            source="admin:test",
        )
    with pytest.raises(SubscriptionLifecycleError, match="effective_at"):
        SubscriptionLifecycleCommand(
            subscription_id="sub-1",
            kind=SubscriptionCommandKind.cancel,
            source="admin:test",
            effective_timing=SubscriptionEffectiveTiming.scheduled,
        )


def test_outcome_contract_carries_partial_execution_evidence() -> None:
    command = SubscriptionLifecycleCommand(
        subscription_id="sub-1",
        kind=SubscriptionCommandKind.cancel,
        source="bulk:test",
    )

    outcome = SubscriptionCommandOutcome(
        command=command,
        status=SubscriptionCommandOutcomeStatus.skipped,
        message="Subscription is already terminal",
        previous_head="reviewed-head",
        current_head="current-head",
        error_code="status_canceled_not_eligible_for_cancel",
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.skipped
    assert outcome.error_code == "status_canceled_not_eligible_for_cancel"
