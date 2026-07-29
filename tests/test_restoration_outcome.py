"""A restoration attempt must tell the operator exactly what happened.

Reason scoping is intentional and unchanged here: payment does not lift FUP,
admin, or fraud locks, and no payment path clears a lifecycle override. The
defect being closed is silence — the owner returned a bare ``False`` for five
different situations, so a customer who had genuinely paid stayed dark with no
stated blocker, no required action, and no operator worklist entry.
"""

from __future__ import annotations

import pytest

from app.models.admin_alert import AdminAlert
from app.models.catalog import SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import (
    ALLOWED_RESTORERS,
    PAYMENT_TRIGGERS,
    RestorationOutcome,
    payment_clearable_reasons,
    restore_subscription,
    restore_subscription_detailed,
    set_account_lifecycle_override,
    suspend_subscription,
)
from app.services.settled_access_blocked import (
    clear_financially_settled_but_access_blocked,
    open_settled_access_blocked_worklist,
    worklist_fingerprint,
)


def test_top_up_clears_both_fup_and_prepaid() -> None:
    """Guard the correction: `top_up` is authorized for FUP as well as prepaid.

    Reporting FUP as payment-unclearable would misdirect every operator who
    reads `remaining_blockers`.
    """
    assert "top_up" in ALLOWED_RESTORERS[EnforcementReason.fup]
    assert "top_up" in ALLOWED_RESTORERS[EnforcementReason.prepaid]
    clearable = payment_clearable_reasons()
    assert {"overdue", "fup", "prepaid"} <= clearable
    assert "admin" not in clearable
    assert "fraud" not in clearable
    assert PAYMENT_TRIGGERS == {"payment", "top_up", "collections_resolution"}


def test_successful_restore_reports_restored_and_no_blockers(db_session, subscription):
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )

    assert result.outcome is RestorationOutcome.restored
    assert result.payment_settled is True
    assert result.financial_lock_cleared is True
    assert result.access_restored is True
    assert result.remaining_blockers == ()
    assert result.required_action is None
    assert result.financially_settled_but_access_blocked is False


def test_payment_cannot_clear_an_admin_lock_and_says_so(db_session, subscription):
    """The old silent `return False` at the unauthorized-trigger branch."""
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.admin,
        source="admin:test",
        emit=False,
    )

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )

    assert result.outcome is RestorationOutcome.blocked_by_unauthorized_trigger
    assert result.access_restored is False
    assert result.payment_settled is True
    assert result.financial_lock_cleared is False
    assert "admin" in result.remaining_blockers
    assert result.payment_clearable_blockers == ()
    assert result.required_action == "admin_review"
    assert result.financially_settled_but_access_blocked is True


def test_partial_clear_reports_the_surviving_blockers(db_session, subscription):
    """The old silent `else` branch: 'still has active locks' logged, nothing else."""
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="risk:test",
        emit=False,
    )

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )

    assert result.outcome is RestorationOutcome.blocked_by_remaining_locks
    assert result.access_restored is False
    assert result.financial_lock_cleared is True, "the overdue lock did clear"
    assert result.resolved_lock_count == 1
    assert result.remaining_blockers == ("fraud",)
    assert result.required_action == "admin_review"


def test_fup_is_not_reported_as_payment_unclearable(db_session, subscription):
    """A FUP lock IS clearable by a top-up; the outcome must route there."""
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fup,
        source="fup_rule:test",
        emit=False,
    )

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )

    assert result.remaining_blockers == ("fup",)
    assert result.payment_clearable_blockers == ("fup",)
    assert result.required_action == "retry_with_authorized_trigger"


def test_lifecycle_override_is_exposed_but_never_cleared(db_session, subscription):
    """Payment must EXPOSE an override, never silently remove it."""
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )
    set_account_lifecycle_override(
        db_session,
        str(subscription.subscriber_id),
        status=SubscriberStatus.suspended,
        reason="Regulatory hold pending KYC review",
        source="admin:compliance",
    )
    db_session.flush()

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )

    assert result.outcome is RestorationOutcome.blocked_by_lifecycle_override
    assert result.access_restored is False
    assert result.lifecycle_override_status == SubscriberStatus.suspended.value
    assert result.lifecycle_override_reason == "Regulatory hold pending KYC review"
    assert result.lifecycle_override_source == "admin:compliance"
    assert "lifecycle_override" in result.remaining_blockers
    assert result.required_action == "clear_lifecycle_override"

    subscriber = db_session.get(Subscriber, subscription.subscriber_id)
    assert subscriber.lifecycle_override_status is SubscriberStatus.suspended, (
        "payment must never clear a lifecycle override"
    )


def test_blocked_settlement_lands_on_the_operator_worklist(db_session, subscription):
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="risk:test",
        emit=False,
    )

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )
    db_session.flush()

    assert result.financially_settled_but_access_blocked is True
    alert = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint == worklist_fingerprint(str(subscription.id)))
        .one()
    )
    assert alert.status is AlertStatus.open
    assert alert.severity is AlertSeverity.warning
    assert alert.details["remaining_blockers"] == ["fraud"]
    assert alert.details["required_action"] == "admin_review"
    assert alert.target_url.endswith(str(subscription.subscriber_id))

    worklist = open_settled_access_blocked_worklist(db_session)
    assert [item.id for item in worklist] == [alert.id]


def test_worklist_entry_is_deduplicated_across_repeated_attempts(
    db_session, subscription
):
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="risk:test",
        emit=False,
    )

    for _ in range(3):
        restore_subscription_detailed(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:test",
            emit=False,
        )
    db_session.flush()

    assert len(open_settled_access_blocked_worklist(db_session)) == 1


def test_worklist_entry_clears_once_access_is_restored(db_session, subscription):
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="risk:test",
        emit=False,
    )
    restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )
    db_session.flush()
    assert open_settled_access_blocked_worklist(db_session)

    restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="admin",
        resolved_by="admin:test",
        emit=False,
    )
    clear_financially_settled_but_access_blocked(db_session, str(subscription.id))
    db_session.flush()

    assert open_settled_access_blocked_worklist(db_session) == []


def test_a_payment_clearable_blocker_is_critical_on_the_worklist(
    db_session, subscription
):
    """A blocker payment IS authorized to clear surviving is a routing defect."""
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.prepaid,
        source="prepaid_sweep:test",
        emit=False,
    )

    restore_subscription_detailed(
        db_session,
        str(subscription.id),
        # `payment` is NOT an allowed restorer for `prepaid`; only `top_up` is.
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )
    db_session.flush()

    alert = (
        db_session.query(AdminAlert)
        .filter(AdminAlert.fingerprint == worklist_fingerprint(str(subscription.id)))
        .one()
    )
    assert alert.severity is AlertSeverity.critical
    assert alert.details["payment_clearable_blockers"] == ["prepaid"]
    assert alert.details["required_action"] == "retry_with_authorized_trigger"


def test_facade_reports_nothing_needed_doing_as_false(db_session, subscription):
    """An already-active subscription must not read as "I restored something".

    The ~10 existing `restore_subscription` callers were deliberately left
    untouched, so the facade's truthiness must not shift underneath them: a
    caller that treats True as a restoration could notify a customer, clear an
    alert, or resolve a worklist entry for an account never suspended.
    """
    # The shared fixture is `pending`; make this genuinely "already active with
    # service" so the two flags are distinguishable rather than both falsy.
    subscription.status = SubscriptionStatus.active
    db_session.flush()

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )

    assert result.outcome is RestorationOutcome.already_active
    assert result.subscription_reactivated is False, "nothing was transitioned"
    assert result.access_restored is True, "the customer does have service"
    assert result.financially_settled_but_access_blocked is False
    assert (
        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:test",
            emit=False,
        )
        is False
    )


@pytest.mark.parametrize(
    ("scenario", "lock_reasons", "trigger", "expected"),
    [
        # (outcome branch, locks to place, trigger, legacy boolean)
        ("already_active", (), "payment", False),
        ("unauthorized_trigger", (EnforcementReason.fraud,), "payment", False),
        (
            "remaining_locks",
            (EnforcementReason.overdue, EnforcementReason.fraud),
            "payment",
            False,
        ),
        ("lifecycle_override", (EnforcementReason.overdue,), "payment", True),
        ("restored", (EnforcementReason.overdue,), "payment", True),
    ],
)
def test_facade_matches_the_legacy_boolean_on_every_branch(
    db_session, subscription, scenario, lock_reasons, trigger, expected
):
    """Pin `restore_subscription`'s truthiness across all five outcome branches.

    The legacy contract is "did this call transition the subscription to
    active". Each row below is one branch of `restore_subscription_detailed`
    and the boolean the pre-split implementation returned for it. The
    `lifecycle_override` row is the interesting one: the row DID move to active,
    so the facade must still say True even though the customer has no service —
    that nuance belongs on the typed outcome, not on a flag ten unreviewed
    callers read.
    """
    if scenario == "already_active":
        subscription.status = SubscriptionStatus.active
        db_session.flush()
    for reason in lock_reasons:
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=reason,
            source=f"test:{reason.value}",
            emit=False,
        )
    if scenario == "lifecycle_override":
        set_account_lifecycle_override(
            db_session,
            str(subscription.subscriber_id),
            status=SubscriberStatus.suspended,
            reason="Regulatory hold",
            source="admin:compliance",
        )
    db_session.flush()

    assert (
        restore_subscription(
            db_session,
            str(subscription.id),
            trigger=trigger,
            resolved_by="payment:test",
            emit=False,
        )
        is expected
    )


def test_facade_reports_a_restore_blocked_by_an_override_as_transitioned(
    db_session, subscription
):
    """Legacy parity: the facade tracks the transition, not the access verdict.

    The row did move to active, which is what the old boolean meant and what
    its callers were reviewed against. That the account override still keeps
    the customer dark is carried by the typed outcome, not by flipping the
    facade under callers nobody re-reviewed.
    """
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )
    set_account_lifecycle_override(
        db_session,
        str(subscription.subscriber_id),
        status=SubscriberStatus.suspended,
        reason="Regulatory hold",
        source="admin:compliance",
    )
    db_session.flush()

    result = restore_subscription_detailed(
        db_session,
        str(subscription.id),
        trigger="payment",
        resolved_by="payment:test",
        emit=False,
    )

    assert result.subscription_reactivated is True
    assert result.access_restored is False
    assert result.outcome is RestorationOutcome.blocked_by_lifecycle_override


def test_boolean_facade_still_answers_callers(db_session, subscription):
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="dunning_case:test",
        emit=False,
    )

    assert (
        restore_subscription(
            db_session,
            str(subscription.id),
            trigger="payment",
            resolved_by="payment:test",
            emit=False,
        )
        is True
    )


def test_missing_subscription_still_raises(db_session):
    from uuid import uuid4

    with pytest.raises(ValueError):
        restore_subscription_detailed(
            db_session,
            str(uuid4()),
            trigger="payment",
            resolved_by="payment:test",
            emit=False,
        )
