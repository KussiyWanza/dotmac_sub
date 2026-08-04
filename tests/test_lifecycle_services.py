"""Tests for lifecycle service."""

from app.models.lifecycle import LifecycleEventType
from app.schemas.lifecycle import (
    SubscriptionLifecycleEventCreate,
)
from app.services import lifecycle as lifecycle_service


def test_create_lifecycle_event(db_session, subscription):
    """Test creating a subscription lifecycle event."""
    event = lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
            notes="Subscription activated",
        ),
    )
    assert event.subscription_id == subscription.id
    assert event.event_type == LifecycleEventType.activate
    assert event.notes == "Subscription activated"


def test_list_lifecycle_events_by_subscription(db_session, subscription):
    """Test listing lifecycle events by subscription."""
    # Create multiple events
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
        ),
    )
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.suspend,
        ),
    )

    events = lifecycle_service.subscription_lifecycle_events.list(
        db_session,
        subscription_id=str(subscription.id),
        event_type=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(events) >= 2
    assert all(e.subscription_id == subscription.id for e in events)


def test_list_lifecycle_events_by_type(db_session, subscription):
    """Test listing lifecycle events filtered by type."""
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
        ),
    )
    lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.suspend,
        ),
    )

    activate_events = lifecycle_service.subscription_lifecycle_events.list(
        db_session,
        subscription_id=None,
        event_type="activate",
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert all(e.event_type == LifecycleEventType.activate for e in activate_events)


def test_lifecycle_events_expose_no_mutation_api():
    """Transitions are contractual evidence for SLA eligibility, so the
    service must not offer a way to edit or remove them. Corrections are new
    transitions; migration 468 enforces the same rule in the database, where
    it cannot be re-added by a later refactor."""

    events = lifecycle_service.subscription_lifecycle_events
    assert not hasattr(events, "update")
    assert not hasattr(events, "delete")


def test_new_transitions_are_graded_as_evidence(db_session, subscription):
    """Rows written after the cutover can be trusted; the grade says so."""

    event = lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.activate,
        ),
    )

    assert event.evidence_grade == "transition_evidence"


def test_get_lifecycle_event(db_session, subscription):
    """Test getting a lifecycle event by ID."""
    event = lifecycle_service.subscription_lifecycle_events.create(
        db_session,
        SubscriptionLifecycleEventCreate(
            subscription_id=subscription.id,
            event_type=LifecycleEventType.cancel,
            notes="Service cancelled",
        ),
    )

    fetched = lifecycle_service.subscription_lifecycle_events.get(
        db_session, str(event.id)
    )
    assert fetched is not None
    assert fetched.id == event.id
    assert fetched.event_type == LifecycleEventType.cancel
