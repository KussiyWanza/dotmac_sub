"""Central event dispatcher for the event system.

This module provides the main entry point for emitting events throughout
the application. When an event is emitted, it is routed to all registered
handlers (webhook, lifecycle, notification, audit).

Events are persisted before dispatching to enable retry of failed handlers.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.event_store import EventStatus, EventStore
from app.services import event_store as event_store_service
from app.services.events.types import Event, EventType
from app.services.session_hooks import run_after_commit

logger = logging.getLogger(__name__)


@contextmanager
def _isolated_handler_session(db: Session | Any) -> Iterator[Session | Any]:
    """Contain each handler's transaction without hiding parent writes.

    Handlers share the dispatcher's connection through a savepoint-backed child
    session. A handler can flush or even commit without ending the parent event
    transaction, while a database error rolls back only that handler's work.
    """
    if not isinstance(db, Session):
        yield db
        return

    handler_db = Session(
        bind=db.connection(),
        autoflush=False,
        autocommit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield handler_db
        handler_db.commit()
    except Exception:
        handler_db.rollback()
        raise
    finally:
        handler_db.close()


def _event_extra(
    event: Event,
    *,
    handler_count: int | None = None,
    failed_handlers: list[dict[str, str]] | None = None,
    retry_count: int | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "domain_event",
        "event_id": str(event.event_id),
        "event_type": event.event_type.value,
        "actor": event.actor,
        "subscriber_id": str(event.subscriber_id) if event.subscriber_id else None,
        "account_id": str(event.account_id) if event.account_id else None,
        "subscription_id": str(event.subscription_id)
        if event.subscription_id
        else None,
        "invoice_id": str(event.invoice_id) if event.invoice_id else None,
        "service_order_id": str(event.service_order_id)
        if event.service_order_id
        else None,
    }
    if handler_count is not None:
        extra["handler_count"] = handler_count
    if failed_handlers is not None:
        extra["failed_handlers"] = failed_handlers
        extra["failed_handler_count"] = len(failed_handlers)
    if retry_count is not None:
        extra["retry_count"] = retry_count
    return extra


class EventDispatcher:
    """Central dispatcher that routes events to all registered handlers.

    Events are persisted to the event_store table before dispatching,
    enabling retry of failed handlers and providing an audit trail.
    """

    def __init__(self):
        self._handlers: list = []

    def register_handler(self, handler):
        """Register an event handler."""
        self._handlers.append(handler)

    def dispatch(
        self,
        db: Session,
        event: Event,
        *,
        event_record: EventStore | None = None,
    ) -> None:
        """Dispatch an event to all registered handlers.

        The event is persisted before dispatching to enable retry on failure.
        Each handler is called in sequence. Failed handlers are tracked
        for later retry.

        IMPORTANT: Event persistence uses a SEPARATE database session to avoid
        committing the caller's pending transaction. This ensures event emission
        doesn't break transaction isolation for callers.

        Args:
            db: Database session for handlers that need DB access
            event: The event to dispatch
        """
        from app.services.control_relationships import event_execution_plan

        plan = event_execution_plan(event.event_type.value, self._handlers)
        logger.info(
            "event_dispatch_start",
            extra=_event_extra(event, handler_count=len(plan)),
        )

        # 1. Persist event before processing.
        if event_record is None:
            try:
                event_record = event_store_service.create_event_record(db, event)
            except Exception as persist_exc:
                # Legacy direct dispatch remains best-effort. ``emit_event``
                # stages a pending row transactionally before reaching here.
                logger.warning(
                    "event_persist_failed",
                    extra={
                        **_event_extra(event, handler_count=len(plan)),
                        "error": str(persist_exc),
                    },
                )
                try:
                    db.rollback()
                except Exception:
                    logger.exception("event_persist_rollback_failed")

        # 2. Process the event-specific plan, tracking failures and dependency blocks.
        succeeded_handlers: set[str] = set()
        failed_handlers: list[dict[str, str]] = []
        for step in plan:
            unmet = sorted(set(step.dependencies) - succeeded_handlers)
            if unmet:
                error = f"blocked by event dependencies: {', '.join(unmet)}"
                failed_handlers.append(
                    {
                        "handler": step.handler_name,
                        "error": error,
                        "blocked_by": ",".join(unmet),
                    }
                )
                if event_record and event_record.id:
                    event_store_service.record_handler_attempt(
                        db,
                        event_store_id=event_record.id,
                        handler_name=step.handler_name,
                        status="blocked",
                        error=error,
                    )
                continue
            try:
                with _isolated_handler_session(db) as handler_db:
                    step.handler.handle(handler_db, event)
                succeeded_handlers.add(step.handler_name)
                if event_record and event_record.id:
                    event_store_service.record_handler_attempt(
                        db,
                        event_store_id=event_record.id,
                        handler_name=step.handler_name,
                        status="success",
                    )
            except Exception as exc:
                logger.exception(
                    "event_handler_failed",
                    extra={
                        **_event_extra(event, handler_count=len(plan)),
                        "handler": step.handler_name,
                        "error": str(exc),
                    },
                )
                failed_handlers.append(
                    {
                        "handler": step.handler_name,
                        "error": str(exc),
                    }
                )
                if event_record and event_record.id:
                    try:
                        event_store_service.record_handler_attempt(
                            db,
                            event_store_id=event_record.id,
                            handler_name=step.handler_name,
                            status="failed",
                            error=str(exc),
                        )
                    except Exception:
                        logger.exception("event_handler_attempt_failed")

        # 3. Update event status.
        if event_record:
            try:
                event_store_service.mark_event_completed(
                    db, event_record, failed_handlers
                )
            except Exception as update_exc:
                logger.warning(
                    "event_status_update_failed",
                    extra={
                        **_event_extra(
                            event,
                            handler_count=len(plan),
                            failed_handlers=failed_handlers,
                        ),
                        "error": str(update_exc),
                    },
                )
        logger.info(
            "event_dispatch_complete",
            extra=_event_extra(
                event,
                handler_count=len(plan),
                failed_handlers=failed_handlers,
            ),
        )

    def dispatch_pending_event(self, db: Session, event_store_id: UUID) -> bool:
        """Claim and dispatch one durable event-store outbox row."""
        event_record = event_store_service.claim_pending_event(db, event_store_id)
        if event_record is None:
            return False
        event = Event(
            event_type=EventType(event_record.event_type),
            payload=event_record.payload,
            event_id=event_record.event_id,
            actor=event_record.actor,
            subscriber_id=event_record.subscriber_id,
            account_id=event_record.account_id,
            subscription_id=event_record.subscription_id,
            invoice_id=event_record.invoice_id,
            service_order_id=event_record.service_order_id,
        )
        self.dispatch(db, event, event_record=event_record)
        return True

    def retry_event(self, db: Session, event_record) -> bool:
        """Retry processing a failed event.

        Args:
            db: Database session
            event_record: The EventStore record to retry

        Returns:
            True if all handlers succeeded, False otherwise
        """
        # Reconstruct the Event from stored data
        event = Event(
            event_type=EventType(event_record.event_type),
            payload=event_record.payload,
            event_id=event_record.event_id,
            actor=event_record.actor,
            subscriber_id=event_record.subscriber_id,
            account_id=event_record.account_id,
            subscription_id=event_record.subscription_id,
            invoice_id=event_record.invoice_id,
            service_order_id=event_record.service_order_id,
        )

        # Get handlers that failed previously
        failed_handler_names = event_store_service.failed_handler_names(event_record)
        from app.services.control_relationships import event_execution_plan

        plan = event_execution_plan(event.event_type.value, self._handlers)

        # Update retry count and status
        event_store_service.mark_retry_started(db, event_record)
        db.commit()
        logger.info(
            "event_retry_start",
            extra=_event_extra(
                event,
                handler_count=len(plan),
                retry_count=event_record.retry_count,
            ),
        )

        # Retry only the current failure manifest. Previously successful
        # predecessors satisfy dependencies without being executed again.
        plan_names = {step.handler_name for step in plan}
        succeeded_handlers = (
            plan_names - failed_handler_names if failed_handler_names else set()
        )
        new_failures: list[dict[str, str]] = []
        for step in plan:
            if failed_handler_names and step.handler_name not in failed_handler_names:
                continue
            unmet = sorted(set(step.dependencies) - succeeded_handlers)
            if unmet:
                error = f"blocked by event dependencies: {', '.join(unmet)}"
                new_failures.append(
                    {
                        "handler": step.handler_name,
                        "error": error,
                        "blocked_by": ",".join(unmet),
                    }
                )
                event_store_service.record_handler_attempt(
                    db,
                    event_store_id=event_record.id,
                    handler_name=step.handler_name,
                    status="blocked",
                    error=error,
                    retry_count=event_record.retry_count,
                )
                continue
            try:
                with _isolated_handler_session(db) as handler_db:
                    step.handler.handle(handler_db, event)
                succeeded_handlers.add(step.handler_name)
                event_store_service.record_handler_attempt(
                    db,
                    event_store_id=event_record.id,
                    handler_name=step.handler_name,
                    status="success",
                    retry_count=event_record.retry_count,
                )
            except Exception as exc:
                logger.exception(
                    "event_retry_handler_failed",
                    extra={
                        **_event_extra(
                            event,
                            handler_count=len(plan),
                            retry_count=event_record.retry_count,
                        ),
                        "handler": step.handler_name,
                        "error": str(exc),
                    },
                )
                new_failures.append(
                    {
                        "handler": step.handler_name,
                        "error": str(exc),
                    }
                )
                event_store_service.record_handler_attempt(
                    db,
                    event_store_id=event_record.id,
                    handler_name=step.handler_name,
                    status="failed",
                    error=str(exc),
                    retry_count=event_record.retry_count,
                )

        # Update final status
        event_store_service.mark_event_completed(db, event_record, new_failures)
        db.commit()
        logger.info(
            "event_retry_complete",
            extra=_event_extra(
                event,
                handler_count=len(plan),
                failed_handlers=new_failures,
                retry_count=event_record.retry_count,
            ),
        )

        return len(new_failures) == 0


# Global dispatcher instance
_dispatcher: EventDispatcher | None = None


def get_dispatcher() -> EventDispatcher:
    """Get the global event dispatcher, initializing handlers if needed."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = EventDispatcher()
        _initialize_handlers(_dispatcher)
    return _dispatcher


def reset_dispatcher() -> None:
    """Reset the global dispatcher for testing.

    This clears any mocked or stale state from the dispatcher singleton.
    Should only be called in test teardown, not in production code.
    """
    global _dispatcher
    _dispatcher = None


def _initialize_handlers(dispatcher: EventDispatcher) -> None:
    """Initialize and register all event handlers."""
    from app.services.events.handlers.arrangements import ArrangementHandler
    from app.services.events.handlers.credential_session_projection import (
        CredentialSessionProjectionHandler,
    )
    from app.services.events.handlers.enforcement import EnforcementHandler
    from app.services.events.handlers.identity_lifecycle_projection import (
        IdentityLifecycleProjectionHandler,
    )
    from app.services.events.handlers.ip_assignment_projection import (
        IPAssignmentProjectionHandler,
    )
    from app.services.events.handlers.lifecycle import LifecycleHandler
    from app.services.events.handlers.materials_lifecycle_projection import (
        MaterialsLifecycleProjectionHandler,
    )
    from app.services.events.handlers.notification import NotificationHandler
    from app.services.events.handlers.outage_lifecycle_projection import (
        OutageLifecycleProjectionHandler,
    )
    from app.services.events.handlers.password_recovery import PasswordRecoveryHandler
    from app.services.events.handlers.prepaid_renewal import PrepaidRenewalHandler
    from app.services.events.handlers.provisioning import ProvisioningHandler
    from app.services.events.handlers.referral import ReferralHandler
    from app.services.events.handlers.reseller_invite import ResellerInviteHandler
    from app.services.events.handlers.sales_lifecycle_projection import (
        SalesLifecycleProjectionHandler,
    )
    from app.services.events.handlers.staff_invite import StaffInviteHandler
    from app.services.events.handlers.support_lifecycle_projection import (
        SupportLifecycleProjectionHandler,
    )
    from app.services.events.handlers.webhook import WebhookHandler

    dispatcher.register_handler(WebhookHandler())
    dispatcher.register_handler(LifecycleHandler())
    dispatcher.register_handler(NotificationHandler())
    dispatcher.register_handler(ProvisioningHandler())
    dispatcher.register_handler(SalesLifecycleProjectionHandler())
    dispatcher.register_handler(OutageLifecycleProjectionHandler())
    dispatcher.register_handler(SupportLifecycleProjectionHandler())
    dispatcher.register_handler(MaterialsLifecycleProjectionHandler())
    dispatcher.register_handler(IdentityLifecycleProjectionHandler())
    dispatcher.register_handler(EnforcementHandler())
    dispatcher.register_handler(IPAssignmentProjectionHandler())
    dispatcher.register_handler(CredentialSessionProjectionHandler())
    dispatcher.register_handler(ArrangementHandler())
    from app.services.events.handlers.subscription_change_execution import (
        SubscriptionChangeExecutionHandler,
    )

    dispatcher.register_handler(SubscriptionChangeExecutionHandler())
    dispatcher.register_handler(ReferralHandler())
    dispatcher.register_handler(PrepaidRenewalHandler())
    dispatcher.register_handler(StaffInviteHandler())
    dispatcher.register_handler(ResellerInviteHandler())
    dispatcher.register_handler(PasswordRecoveryHandler())

    from app.services.control_relationships import (
        validate_and_order_handlers,
        validate_event_execution_policy,
    )

    dispatcher._handlers = validate_and_order_handlers(dispatcher._handlers)
    validate_event_execution_policy(dispatcher._handlers)

    logger.info(
        "Event handlers initialized: integration_delivery, lifecycle, "
        "notification, provisioning, sales_lifecycle_projection, enforcement, "
        "ip_assignment_projection, credential_session_projection, arrangements, "
        "referral, prepaid_renewal, "
        "staff_invite, reseller_invite, password_recovery",
        extra={
            "event": "event_handlers_initialized",
            "handler_count": len(dispatcher._handlers),
        },
    )


def emit_event(
    db: Session,
    event_type: EventType,
    payload: dict[str, Any],
    *,
    actor: str | None = None,
    subscriber_id: UUID | str | None = None,
    account_id: UUID | str | None = None,
    subscription_id: UUID | str | None = None,
    invoice_id: UUID | str | None = None,
    service_order_id: UUID | str | None = None,
    defer_until_commit: bool = True,
) -> Event:
    """Emit an event to all registered handlers.

    This is the main entry point for services to emit events. After calling
    this function, the event will be:
    - Delivered to subscribed webhook endpoints (via Celery task)
    - Recorded as a lifecycle event (if applicable)
    - Queued as a notification (if template configured)

    Args:
        db: Database session
        event_type: The type of event
        payload: Event-specific data
        actor: Who/what triggered the event
        subscriber_id: Related subscriber ID
        account_id: Related account ID
        subscription_id: Related subscription ID
        invoice_id: Related invoice ID
        service_order_id: Related service order ID

    Returns:
        The created Event object

    Example:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.subscription_activated,
            {"subscription_id": str(sub.id), "offer_name": sub.offer.name},
            subscription_id=sub.id,
            account_id=sub.account_id,
        )
    """

    # Normalize UUIDs
    def to_uuid(value: UUID | str | None) -> UUID | None:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        return UUID(value)

    event = Event(
        event_type=event_type,
        payload=payload,
        actor=actor,
        subscriber_id=to_uuid(subscriber_id),
        account_id=to_uuid(account_id),
        subscription_id=to_uuid(subscription_id),
        invoice_id=to_uuid(invoice_id),
        service_order_id=to_uuid(service_order_id),
    )

    dispatcher = get_dispatcher()

    # Non-SQLAlchemy test/dry-run adapters preserve the legacy best-effort
    # dispatch path. Production sessions stage the outbox row in the same
    # transaction as the domain change that emitted it.
    if not isinstance(db, Session):
        dispatcher.dispatch(db, event)
        return event

    event_record = event_store_service.create_event_record(
        db,
        event,
        status=EventStatus.pending,
    )
    event_record_id = event_record.id

    def _dispatch_after_commit(callback_db: Session) -> None:
        dispatcher.dispatch_pending_event(callback_db, event_record_id)
        if isinstance(callback_db, Session):
            try:
                callback_db.commit()
            except Exception:
                callback_db.rollback()
                raise

    if defer_until_commit:
        run_after_commit(db, _dispatch_after_commit)
    else:
        dispatcher.dispatch_pending_event(db, event_record_id)

    logger.info(
        "event_emitted",
        extra=_event_extra(event, handler_count=len(dispatcher._handlers)),
    )

    return event
