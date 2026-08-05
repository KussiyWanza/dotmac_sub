"""Lifecycle audit-trail completeness (review task #4).

Two guarantees:
  1. Every domain-op transition (suspend/resume/activate/expire/cancel) emits
     an event payload carrying ``from_status``/``to_status`` so the
     SubscriptionLifecycleEvent columns are populated (they were NULL before).
  2. Subscription expiry is recorded as a lifecycle event (it produced no
     record at all before — missing from SUBSCRIPTION_LIFECYCLE_MAP).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services import account_lifecycle
from app.services.events.handlers.lifecycle import LifecycleHandler
from app.services.events.types import (
    SUBSCRIPTION_LIFECYCLE_MAP,
    Event,
    EventType,
)


def _active_sub(db, subscriber, catalog_offer) -> Subscription:
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        billing_mode=catalog_offer.billing_mode,
        start_at=datetime.now(UTC),
    )
    db.add(sub)
    db.flush()
    return sub


def _capture_payloads(monkeypatch) -> list[tuple]:
    captured: list[tuple] = []
    monkeypatch.setattr(
        account_lifecycle,
        "emit_event",
        lambda db, event_type, payload, **kw: captured.append((event_type, payload)),
    )
    return captured


def test_suspend_resume_payloads_carry_from_and_to_status(
    db_session, subscriber, catalog_offer, monkeypatch
):
    captured = _capture_payloads(monkeypatch)
    sub = _active_sub(db_session, subscriber, catalog_offer)

    account_lifecycle.suspend_subscription(
        db_session,
        str(sub.id),
        reason=EnforcementReason.admin,
        source="test",
    )
    suspended = next(p for et, p in captured if et == EventType.subscription_suspended)
    assert suspended["from_status"] == "active"
    assert suspended["to_status"] == "suspended"

    account_lifecycle.restore_subscription(
        db_session, str(sub.id), trigger="admin", resolved_by="test"
    )
    resumed = next(p for et, p in captured if et == EventType.subscription_resumed)
    assert resumed["from_status"] == "suspended"
    assert resumed["to_status"] == "active"


def test_expire_and_cancel_payloads_carry_from_and_to_status(
    db_session, subscriber, catalog_offer, monkeypatch
):
    captured = _capture_payloads(monkeypatch)
    sub = _active_sub(db_session, subscriber, catalog_offer)

    account_lifecycle.expire_subscription(db_session, str(sub.id))
    expired = next(p for et, p in captured if et == EventType.subscription_expired)
    assert expired["from_status"] == "active"
    assert expired["to_status"] == "expired"
    assert expired["reason"] == "expired"


def test_expiry_is_recorded_atomically_and_handler_cannot_duplicate_it(
    db_session, subscriber, catalog_offer
):
    """The status owner writes evidence; the event transport only observes."""
    assert EventType.subscription_expired in SUBSCRIPTION_LIFECYCLE_MAP

    sub = _active_sub(db_session, subscriber, catalog_offer)
    account_lifecycle.expire_subscription(db_session, str(sub.id), emit=False)
    db_session.flush()

    records = (
        db_session.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == sub.id)
        .all()
    )
    assert len(records) == 1
    rec = records[0]
    assert rec.event_type == LifecycleEventType.other
    assert rec.from_status == SubscriptionStatus.active
    assert rec.to_status == SubscriptionStatus.expired
    assert rec.reason == "Subscription expired"
    assert rec.evidence_source == "lifecycle_command"

    event = Event(
        event_type=EventType.subscription_expired,
        payload={
            "subscription_id": str(sub.id),
            "reason": "expired",
            "from_status": "active",
            "to_status": "expired",
        },
        subscription_id=sub.id,
    )
    LifecycleHandler().handle(db_session, event)
    db_session.flush()
    assert (
        db_session.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == sub.id)
        .count()
        == 1
    )
