"""The generic lifecycle surface is read-only over owner-written evidence."""

from app.models.lifecycle import SubscriptionLifecycleEvent
from app.services import lifecycle as lifecycle_service


def _creation_evidence(db_session, subscription) -> SubscriptionLifecycleEvent:
    return (
        db_session.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
        .one()
    )


def test_lifecycle_events_expose_no_mutation_api():
    events = lifecycle_service.subscription_lifecycle_events
    assert not hasattr(events, "create")
    assert not hasattr(events, "update")
    assert not hasattr(events, "delete")


def test_list_lifecycle_evidence_by_subscription(db_session, subscription):
    event = _creation_evidence(db_session, subscription)

    events = lifecycle_service.subscription_lifecycle_events.list(
        db_session,
        subscription_id=str(subscription.id),
        event_type=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )

    assert [item.id for item in events] == [event.id]


def test_list_lifecycle_evidence_by_type(db_session, subscription):
    event = _creation_evidence(db_session, subscription)

    events = lifecycle_service.subscription_lifecycle_events.list(
        db_session,
        subscription_id=str(subscription.id),
        event_type=event.event_type.value,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )

    assert event.id in {item.id for item in events}


def test_subscription_creation_writes_a_prospective_baseline(db_session, subscription):
    event = _creation_evidence(db_session, subscription)

    assert event.from_status is None
    assert event.to_status == subscription.status
    assert event.evidence_grade == "state_baseline"
    assert event.evidence_source == "subscription_creation"
    assert event.effective_at is not None
    assert event.recorded_at is not None
    assert event.source_id
    assert event.evidence_fingerprint.startswith("sha256:")


def test_get_lifecycle_evidence(db_session, subscription):
    event = _creation_evidence(db_session, subscription)

    fetched = lifecycle_service.subscription_lifecycle_events.get(
        db_session, str(event.id)
    )

    assert fetched.id == event.id
