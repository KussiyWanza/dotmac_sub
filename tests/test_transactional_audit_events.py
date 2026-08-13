from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.audit import AuditActorType, AuditEvent
from app.models.event_store import EventHandlerAttempt, EventStatus, EventStore
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.events.dispatcher import emit_event
from app.services.events.types import EventType


def _audit_rows_for(db_session, entity_id: str) -> list[AuditEvent]:
    return db_session.query(AuditEvent).filter(AuditEvent.entity_id == entity_id).all()


def test_audit_record_is_deferred_until_commit(db_session):
    subscriber = Subscriber(
        first_name="Audit", last_name="Deferred", email="audit-deferred@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()

    audit_service.audit_events.record(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(subscriber.id),
            action="update",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            is_success=True,
        ),
    )

    assert _audit_rows_for(db_session, str(subscriber.id)) == []

    db_session.commit()

    rows = _audit_rows_for(db_session, str(subscriber.id))
    assert len(rows) == 1
    assert rows[0].entity_id == str(subscriber.id)


def test_audit_record_is_discarded_on_rollback(db_session):
    subscriber = Subscriber(
        first_name="Audit", last_name="Rollback", email="audit-rollback@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()

    audit_service.audit_events.record(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(subscriber.id),
            action="delete",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            is_success=False,
        ),
    )

    db_session.rollback()

    assert _audit_rows_for(db_session, str(subscriber.id)) == []


def test_audit_stage_is_atomic_with_caller_transaction(db_session):
    event = audit_service.audit_events.stage(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="credential_cleanup",
            entity_type="NasDevice",
            entity_id=str(uuid.uuid4()),
        ),
    )

    db_session.flush()
    assert db_session.get(AuditEvent, event.id) is event

    db_session.rollback()
    assert db_session.get(AuditEvent, event.id) is None


def test_audit_stage_dual_writes_kernel_details(db_session):
    event = audit_service.audit_events.stage(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.service,
            actor_id="service:parity-test",
            action="audit_r1_parity_test",
            entity_type="audit_event",
            metadata_={
                "source": "legacy-metadata",
                "ip_address": "stale-value",
            },
            details={"candidate": "a42"},
            ip_address="192.0.2.10",
            user_agent="audit-r1-test",
        ),
    )
    db_session.flush()

    assert event.metadata_ == {
        "source": "legacy-metadata",
        "ip_address": "stale-value",
    }
    assert event.details == {
        "source": "legacy-metadata",
        "candidate": "a42",
        "ip_address": "192.0.2.10",
        "user_agent": "audit-r1-test",
    }
    assert event.created_at is not None


@pytest.mark.parametrize(
    "actor_type",
    [AuditActorType.user, AuditActorType.service, AuditActorType.api_key],
)
def test_non_system_audit_actor_requires_an_identifier(actor_type):
    with pytest.raises(ValueError, match="needs a non-empty actor_id"):
        AuditEventCreate(
            actor_type=actor_type,
            action="invalid_actor",
            entity_type="audit_event",
        )


def test_system_audit_actor_may_omit_an_identifier():
    payload = AuditEventCreate(
        actor_type=AuditActorType.system,
        action="system_actor",
        entity_type="audit_event",
    )

    assert payload.actor_id is None


def test_actor_party_enrichment_is_not_a_foreign_key():
    actor_party_id = AuditEvent.__table__.c.actor_party_id

    assert not actor_party_id.foreign_keys
    assert actor_party_id.nullable is True


def test_audit_record_in_nested_transaction_waits_for_outer_commit(db_session):
    subscriber = Subscriber(
        first_name="Audit",
        last_name="Nested",
        email="audit-nested@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()

    savepoint = db_session.begin_nested()
    audit_service.audit_events.record(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(subscriber.id),
            action="nested_update",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            is_success=True,
        ),
    )
    savepoint.commit()

    assert _audit_rows_for(db_session, str(subscriber.id)) == []

    db_session.commit()

    rows = _audit_rows_for(db_session, str(subscriber.id))
    assert len(rows) == 1
    assert rows[0].action == "nested_update"


def test_audit_record_in_nested_rollback_is_discarded(db_session):
    subscriber = Subscriber(
        first_name="Audit",
        last_name="NestedRollback",
        email="audit-nested-rollback@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()

    savepoint = db_session.begin_nested()
    audit_service.audit_events.record(
        db_session,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(subscriber.id),
            action="nested_delete",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            is_success=False,
        ),
    )
    savepoint.rollback()
    db_session.commit()

    assert _audit_rows_for(db_session, str(subscriber.id)) == []


def test_audit_request_payload_redacts_sensitive_query_params():
    from fastapi import Request, Response

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/system/audit",
        "query_string": b"token=abc123&password=secret&visible=value",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    request = Request(scope)
    response = Response(status_code=200)

    payload = audit_service.audit_events.build_request_payload(request, response)

    assert payload.metadata_ is not None
    assert payload.metadata_["query"]["token"] == "<redacted>"
    assert payload.metadata_["query"]["password"] == "<redacted>"
    assert payload.metadata_["query"]["visible"] == "value"


@patch("app.services.events.dispatcher._dispatcher", None)
@patch("app.services.events.dispatcher._initialize_handlers")
def test_emit_event_is_deferred_until_commit(_mock_init_handlers, db_session):
    subscriber = Subscriber(
        first_name="Event", last_name="Deferred", email="event-deferred@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()

    emit_event(
        db_session,
        EventType.subscriber_created,
        {"subscriber_id": str(subscriber.id)},
        subscriber_id=subscriber.id,
    )

    staged = db_session.query(EventStore).one()
    assert staged.status == EventStatus.pending

    db_session.commit()

    rows = db_session.query(EventStore).all()
    assert len(rows) == 1
    assert rows[0].event_type == EventType.subscriber_created.value


@patch("app.services.events.dispatcher._dispatcher", None)
@patch("app.services.events.dispatcher._initialize_handlers")
def test_emit_event_is_discarded_on_rollback(_mock_init_handlers, db_session):
    subscriber = Subscriber(
        first_name="Event", last_name="Rollback", email="event-rollback@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()

    emit_event(
        db_session,
        EventType.subscriber_created,
        {"subscriber_id": str(subscriber.id)},
        subscriber_id=subscriber.id,
    )

    db_session.rollback()

    assert db_session.query(EventStore).count() == 0


@patch("app.services.events.dispatcher._dispatcher", None)
@patch("app.services.events.dispatcher._initialize_handlers")
def test_emit_event_remains_pending_when_after_commit_dispatch_fails(
    _mock_init_handlers, db_session, monkeypatch
):
    from app.services.events.dispatcher import EventDispatcher

    monkeypatch.setattr(
        EventDispatcher,
        "dispatch_pending_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("worker lost")),
    )
    event = emit_event(
        db_session,
        EventType.subscriber_created,
        {"subscriber_id": "recoverable"},
    )

    db_session.commit()

    row = (
        db_session.query(EventStore).filter(EventStore.event_id == event.event_id).one()
    )
    assert row.status == EventStatus.pending


def test_emit_event_sanitizes_sensitive_payload_values(db_session):
    event = emit_event(
        db_session,
        EventType.subscriber_created,
        {
            "subscriber_id": "sub-1",
            "api_token": "secret-token",
            "nested": {"password": "letmein", "visible": "ok"},
        },
    )
    db_session.commit()

    row = (
        db_session.query(EventStore).filter(EventStore.event_id == event.event_id).one()
    )
    assert row.payload["api_token"] == "<redacted>"
    assert row.payload["nested"]["password"] == "<redacted>"
    assert row.payload["nested"]["visible"] == "ok"


def test_dispatcher_persists_first_class_handler_attempts(db_session):
    from app.services.events.dispatcher import EventDispatcher
    from app.services.events.types import Event

    class SuccessHandler:
        def handle(self, db, event):
            return None

    dispatcher = EventDispatcher()
    dispatcher.register_handler(SuccessHandler())
    event = Event(event_type=EventType.custom, payload={"visible": "ok"})

    dispatcher.dispatch(db_session, event)

    stored_event = (
        db_session.query(EventStore).filter(EventStore.event_id == event.event_id).one()
    )
    attempts = (
        db_session.query(EventHandlerAttempt)
        .filter(EventHandlerAttempt.event_store_id == stored_event.id)
        .all()
    )
    assert len(attempts) == 1
    assert attempts[0].handler_name == "SuccessHandler"
    assert attempts[0].status == "success"


def test_dispatcher_contains_handler_database_failures(db_session):
    from app.services.events.dispatcher import EventDispatcher
    from app.services.events.types import Event

    handled: list[str] = []

    class ArrangementHandler:
        def handle(self, db, event):
            db.execute(text("SELECT * FROM event_handler_table_that_does_not_exist"))

    class NotificationHandler:
        def handle(self, db, event):
            db.execute(text("SELECT 1"))
            handled.append(event.event_type.value)

    dispatcher = EventDispatcher()
    dispatcher.register_handler(ArrangementHandler())
    dispatcher.register_handler(NotificationHandler())
    event = Event(event_type=EventType.payment_received, payload={"amount": 50})

    dispatcher.dispatch(db_session, event)

    stored_event = (
        db_session.query(EventStore).filter(EventStore.event_id == event.event_id).one()
    )
    attempts = {
        attempt.handler_name: attempt.status
        for attempt in db_session.query(EventHandlerAttempt)
        .filter(EventHandlerAttempt.event_store_id == stored_event.id)
        .all()
    }
    assert handled == [EventType.payment_received.value]
    assert attempts == {
        "ArrangementHandler": "failed",
        "NotificationHandler": "success",
    }


def test_retry_event_uses_first_class_handler_attempt_rows_as_source_of_truth(
    db_session,
):
    from app.services.events.dispatcher import EventDispatcher

    class RetryableHandler:
        def handle(self, db, event):
            return None

    event_record = EventStore(
        event_id=uuid.uuid4(),
        event_type=EventType.custom.value,
        payload={"visible": "ok"},
        status=EventStatus.failed,
        failed_handlers=None,
        handler_attempts=None,
    )
    db_session.add(event_record)
    db_session.flush()
    db_session.add(
        EventHandlerAttempt(
            event_store_id=event_record.id,
            handler_name="RetryableHandler",
            status="failed",
            retry_count=0,
        )
    )
    db_session.commit()

    dispatcher = EventDispatcher()
    dispatcher.register_handler(RetryableHandler())

    result = dispatcher.retry_event(db_session, event_record)

    db_session.refresh(event_record)
    assert result is True
    assert event_record.status == EventStatus.completed


@patch("app.services.events.dispatcher._dispatcher", None)
@patch("app.services.events.dispatcher._initialize_handlers")
def test_emit_event_in_nested_transaction_waits_for_outer_commit(
    _mock_init_handlers, db_session
):
    subscriber = Subscriber(
        first_name="Event",
        last_name="Nested",
        email="event-nested@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()

    savepoint = db_session.begin_nested()
    emit_event(
        db_session,
        EventType.subscriber_created,
        {"subscriber_id": str(subscriber.id)},
        subscriber_id=subscriber.id,
    )
    savepoint.commit()

    staged = db_session.query(EventStore).one()
    assert staged.status == EventStatus.pending

    db_session.commit()

    rows = db_session.query(EventStore).all()
    assert len(rows) == 1
    assert rows[0].event_type == EventType.subscriber_created.value


@patch("app.services.events.dispatcher._dispatcher", None)
@patch("app.services.events.dispatcher._initialize_handlers")
def test_emit_event_in_nested_rollback_is_discarded(_mock_init_handlers, db_session):
    subscriber = Subscriber(
        first_name="Event",
        last_name="NestedRollback",
        email="event-nested-rollback@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()

    savepoint = db_session.begin_nested()
    emit_event(
        db_session,
        EventType.subscriber_created,
        {"subscriber_id": str(subscriber.id)},
        subscriber_id=subscriber.id,
    )
    savepoint.rollback()
    db_session.commit()

    assert db_session.query(EventStore).count() == 0
