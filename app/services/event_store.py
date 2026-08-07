from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.event_store import EventHandlerAttempt, EventStatus, EventStore
from app.services.events.types import Event, EventType


class EventTerminalDisposition(StrEnum):
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"


@dataclass(frozen=True, slots=True)
class WaitForEventTerminalQuery:
    event_id: UUID
    event_type: EventType
    timeout: timedelta
    poll_interval: timedelta = timedelta(milliseconds=500)


@dataclass(frozen=True, slots=True)
class EventTerminalOutcome:
    event_id: UUID
    event_type: EventType
    disposition: EventTerminalDisposition
    retry_count: int
    failed_handlers: tuple[str, ...]


def wait_for_event_terminal(
    db: Session,
    query: WaitForEventTerminalQuery,
) -> EventTerminalOutcome:
    """Wait for one exact durable event; never substitute a newer row."""

    timeout_seconds = query.timeout.total_seconds()
    poll_seconds = query.poll_interval.total_seconds()
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("event terminal wait durations must be positive")
    deadline = time.monotonic() + timeout_seconds
    retry_count = 0
    while True:
        if db.in_transaction():
            db.rollback()
        record = db.scalar(
            select(EventStore).where(
                EventStore.event_id == query.event_id,
                EventStore.event_type == query.event_type.value,
            )
        )
        if record is not None:
            retry_count = int(record.retry_count or 0)
            if record.status in {EventStatus.completed, EventStatus.failed}:
                failures = tuple(
                    sorted(
                        str(item.get("handler") or "")
                        for item in (record.failed_handlers or [])
                        if item.get("handler")
                    )
                )
                return EventTerminalOutcome(
                    event_id=query.event_id,
                    event_type=query.event_type,
                    disposition=(
                        EventTerminalDisposition.completed
                        if record.status is EventStatus.completed
                        else EventTerminalDisposition.failed
                    ),
                    retry_count=retry_count,
                    failed_handlers=failures,
                )
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return EventTerminalOutcome(
                event_id=query.event_id,
                event_type=query.event_type,
                disposition=EventTerminalDisposition.timed_out,
                retry_count=retry_count,
                failed_handlers=(),
            )
        time.sleep(min(poll_seconds, remaining_seconds))


_SENSITIVE_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "password",
    "secret",
    "token",
}


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if key.lower() in _SENSITIVE_KEYS
            else _sanitize_payload(val)
            for key, val in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitize_payload(item) for item in value]
    return value


def create_event_record(
    db: Session,
    event: Event,
    *,
    status: EventStatus = EventStatus.processing,
) -> EventStore:
    record = EventStore(
        id=uuid4(),
        event_id=event.event_id,
        event_type=event.event_type.value,
        payload=_sanitize_payload(event.payload),
        status=status,
        actor=event.actor,
        subscriber_id=event.subscriber_id,
        account_id=event.account_id,
        subscription_id=event.subscription_id,
        invoice_id=event.invoice_id,
        service_order_id=event.service_order_id,
    )
    db.add(record)
    db.flush()
    return record


def list_pending_event_ids(db: Session, *, limit: int) -> list[UUID]:
    rows = (
        db.query(EventStore.id)
        .filter(EventStore.status == EventStatus.pending)
        .filter(EventStore.is_active.is_(True))
        .order_by(EventStore.created_at.asc())
        .limit(limit)
        .all()
    )
    return [row[0] for row in rows]


def claim_pending_event(db: Session, event_store_id: UUID) -> EventStore | None:
    record = (
        db.query(EventStore)
        .filter(EventStore.id == event_store_id)
        .filter(EventStore.status == EventStatus.pending)
        .filter(EventStore.is_active.is_(True))
        .with_for_update(skip_locked=True)
        .one_or_none()
    )
    if record is None:
        return None
    record.status = EventStatus.processing
    db.flush()
    return record


def mark_event_completed(
    db: Session,
    record: EventStore,
    failed_handlers: list[dict[str, str]],
) -> None:
    if failed_handlers:
        record.status = EventStatus.failed
        record.failed_handlers = failed_handlers
        record.error = json.dumps([failure["error"] for failure in failed_handlers])
    else:
        record.status = EventStatus.completed
        record.failed_handlers = None
        record.error = None
    record.processed_at = datetime.now(UTC)
    db.flush()


def record_handler_attempt(
    db: Session,
    *,
    event_store_id: UUID,
    handler_name: str,
    status: str,
    error: str | None = None,
    retry_count: int = 0,
) -> EventHandlerAttempt:
    attempt = EventHandlerAttempt(
        event_store_id=event_store_id,
        handler_name=handler_name,
        status=status,
        error=error,
        retry_count=retry_count,
    )
    db.add(attempt)
    db.flush()
    return attempt


def mark_retry_started(db: Session, record: EventStore) -> None:
    record.retry_count += 1
    record.status = EventStatus.processing
    db.flush()


def failed_handler_names(record: EventStore) -> set[str]:
    # ``failed_handlers`` is the current attempt's authoritative manifest.
    # Historical attempt rows intentionally retain earlier failures for audit;
    # consulting them first would re-run handlers that already recovered.
    if record.failed_handlers is not None:
        return {failure["handler"] for failure in record.failed_handlers}

    attempts = getattr(record, "handler_attempts", None) or []
    latest_by_handler: dict[str, EventHandlerAttempt] = {}
    for attempt in attempts:
        previous = latest_by_handler.get(attempt.handler_name)
        attempt_key = (
            int(getattr(attempt, "retry_count", 0) or 0),
            getattr(attempt, "attempted_at", datetime.min.replace(tzinfo=UTC)),
        )
        previous_key = (
            int(getattr(previous, "retry_count", 0) or 0),
            getattr(previous, "attempted_at", datetime.min.replace(tzinfo=UTC)),
        )
        if previous is None or attempt_key >= previous_key:
            latest_by_handler[attempt.handler_name] = attempt
    return {
        name
        for name, attempt in latest_by_handler.items()
        if getattr(attempt, "status", None) in {"failed", "blocked"}
    }


def list_retryable_failed_events(
    db: Session,
    *,
    max_retries: int,
    max_age_hours: int,
    limit: int,
) -> list[EventStore]:
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    return (
        db.query(EventStore)
        .filter(EventStore.status == EventStatus.failed)
        .filter(EventStore.retry_count < max_retries)
        .filter(EventStore.created_at > cutoff)
        .filter(EventStore.is_active.is_(True))
        .order_by(EventStore.created_at.asc())
        .limit(limit)
        .all()
    )


def cleanup_completed_events(db: Session, *, retention_days: int) -> dict[str, int]:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    old_completed_event_ids = (
        db.query(EventStore.id)
        .filter(EventStore.status == EventStatus.completed)
        .filter(EventStore.created_at < cutoff)
        .scalar_subquery()
    )
    deleted_attempt_count = (
        db.query(EventHandlerAttempt)
        .filter(EventHandlerAttempt.event_store_id.in_(old_completed_event_ids))
        .delete(synchronize_session=False)
    )
    deleted_count = (
        db.query(EventStore)
        .filter(EventStore.id.in_(old_completed_event_ids))
        .delete(synchronize_session=False)
    )
    return {
        "deleted": int(deleted_count or 0),
        "handler_attempts_deleted": int(deleted_attempt_count or 0),
    }


def mark_stale_processing_events(db: Session, *, stale_minutes: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    stuck_events = (
        db.query(EventStore)
        .filter(EventStore.status == EventStatus.processing)
        .filter(EventStore.updated_at < cutoff)
        .filter(EventStore.is_active.is_(True))
        .all()
    )
    for record in stuck_events:
        record.status = EventStatus.failed
        record.error = "Event processing timed out (marked as stale)"
    db.flush()
    return len(stuck_events)
