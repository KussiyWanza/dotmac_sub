"""Typed notice outcomes never stop the prepaid collections progression.

Only a live customer-impact shield (``policy_suppressed``) may defer the
timer. A missing contact route or blocked delivery arms the timer anyway and
becomes an owned staff work item; shields themselves expire after
``billing.notice_shield_max_hours``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.admin_alert import AdminAlert
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.notification import Notification
from app.models.subscriber import SubscriberStatus
from app.models.support import Ticket, TicketStatus
from app.services.billing_communication_policy import billing_communication_decisions
from app.services.collections.prepaid_balance_sweep import (
    _NO_CONTACT_FINDING_PREFIX,
    run_prepaid_balance_sweep,
)
from tests.prepaid_funding_helpers import (
    ensure_test_prepaid_contract,
    materialize_test_prepaid_opening_balance,
)

_MONDAY_NOON = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _prepare(db, account, subscription, *, grace_days: int = 1) -> None:
    account.billing_mode = BillingMode.prepaid
    account.min_balance = Decimal("100.00")
    account.splynx_customer_id = None
    account.deposit = None
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    account.grace_period_days = grace_days
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = None
    ensure_test_prepaid_contract(db, subscription)
    db.commit()
    materialize_test_prepaid_opening_balance(db, account.id, Decimal("0.00"))


def _enforcement_notifications(db):
    return (
        db.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .all()
    )


def test_missing_contact_route_arms_timer_and_creates_work_item(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.email = ""
    subscriber_account.phone = ""
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["no_contact_route"] == 1
    assert result["warned"] == 0
    db_session.refresh(subscriber_account)
    # The collections timer armed despite the missing route.
    assert subscriber_account.prepaid_low_balance_at is not None
    assert _enforcement_notifications(db_session) == []
    alert = (
        db_session.query(AdminAlert)
        .filter(
            AdminAlert.fingerprint
            == f"{_NO_CONTACT_FINDING_PREFIX}{subscriber_account.id}"
        )
        .one()
    )
    assert alert.status.value == "open"
    assert alert.details["owner"] == "support-collections"
    assert alert.details["sla_due_at"]


def test_phone_only_customer_is_warned_over_non_email_channels(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.email = ""
    subscriber_account.phone = "+2348012345678"
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["warned"] == 1
    assert result["no_contact_route"] == 0
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at is not None
    notifications = _enforcement_notifications(db_session)
    assert notifications
    assert all(n.channel.value in {"sms", "whatsapp"} for n in notifications)
    # No work item for a reachable customer.
    assert (
        db_session.query(AdminAlert)
        .filter(
            AdminAlert.fingerprint
            == f"{_NO_CONTACT_FINDING_PREFIX}{subscriber_account.id}"
        )
        .count()
        == 0
    )


def test_work_item_resolves_when_account_exits_enforcement(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.email = ""
    subscriber_account.phone = ""
    db_session.commit()

    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)
    fingerprint = f"{_NO_CONTACT_FINDING_PREFIX}{subscriber_account.id}"
    alert = (
        db_session.query(AdminAlert).filter(AdminAlert.fingerprint == fingerprint).one()
    )
    assert alert.status.value == "open"

    # Exiting enforcement clears the prepaid timers; the work item must
    # close with it (here via the stale-timer repair for an account that
    # stopped being billing-eligible).
    subscriber_account.billing_enabled = False
    db_session.commit()
    run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON + timedelta(hours=1))
    db_session.refresh(alert)
    assert alert.status.value == "resolved"


def test_notice_shield_expires_after_max_hours(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    ticket = Ticket(
        subscriber_id=subscriber_account.id,
        title="Customer link disconnection",
        description="Infrastructure down, no internet at site",
        status=TicketStatus.open.value,
        ticket_type="Customer Link Disconnection",
    )
    db_session.add(ticket)
    db_session.commit()

    fresh = billing_communication_decisions(db_session, [subscription])
    assert fresh[subscription.id].suppress_suspension_notice is True
    assert fresh[subscription.id].reason == "open_infrastructure_down_ticket"

    # The same evidence older than the shield window stops suppressing:
    # a forgotten ticket cannot pin the account outside collections forever.
    ticket.created_at = datetime.now(UTC) - timedelta(hours=80)
    db_session.commit()

    expired = billing_communication_decisions(db_session, [subscription])
    assert expired[subscription.id].suppress_suspension_notice is False
    assert expired[subscription.id].reason is None
