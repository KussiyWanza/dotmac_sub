"""Project committed lifecycle facts into downstream sales-service owners.

The handler is a thin delivery adapter: funding satisfaction, vendor
verification, service-order release/completion, and CX acceptance remain
facts owned by their originating services. The verified-implementation,
release, and acceptance consequences run through ``sales.fulfillment``'s
receipted consumer commands on a fresh owner-command session — the effect
and its unique ``(consumer, event_id)`` receipt commit atomically, so a
redelivery is an exact no-op. A consequence that cannot be applied raises
so the event delivery stays failed and retryable instead of a warning log.

The funding consequence is the one hop still consumed without a receipt:
its effect creates subscriptions and invoices through billing/catalog
creators that commit internally, which ``execute_owner_command`` correctly
forbids. It stays idempotent on business keys until those creators are
commit-free participants.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.events.handlers.owner_session import owner_session as _owner_session
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.sales_order_funding_satisfied,
        EventType.vendor_project_verified,
        EventType.service_order_released,
        EventType.service_order_completed,
        EventType.customer_experience_accepted,
        EventType.custom,
    }
)

_CX_ACCEPTANCE_DUE_TRIGGER = "sales.cx_acceptance_due"


class SalesLifecycleProjectionHandler:
    """Deliver committed lifecycle outputs to their receipted consumers."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.sales_order_funding_satisfied:
            self._apply_funding_consequences(db, event)
        elif event.event_type == EventType.vendor_project_verified:
            self._release_verified_implementation(db, event)
        elif event.event_type == EventType.service_order_released:
            self._advance_released_order(db, event)
        elif event.event_type == EventType.service_order_completed:
            self._prepare_customer_experience_handoff(db, event)
        elif event.event_type == EventType.customer_experience_accepted:
            self._fulfill_sales_order(db, event)
        elif event.event_type == EventType.custom:
            if event.payload.get("trigger") == _CX_ACCEPTANCE_DUE_TRIGGER:
                self._flag_overdue_acceptance(db, event)
            # Every other custom payload belongs to other adapters.

    @staticmethod
    def _context(event: Event, scope: str):
        from app.services.owner_commands import CommandContext

        return CommandContext.system(
            actor=str(event.actor or "sales.lifecycle_projection"),
            scope=scope,
            reason=event.event_type.value,
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"event:{event.event_id}",
        )

    @staticmethod
    def _apply_funding_consequences(db: Session, event: Event) -> None:
        sales_order_id = event.payload.get("sales_order_id")
        if not sales_order_id:
            logger.warning(
                "funding satisfaction event %s has no sales order id",
                event.event_id,
            )
            return
        from app.services import sales_orders

        sales_orders.apply_funding_consequences(
            db,
            sales_order_id=coerce_uuid(sales_order_id),
            actor_id=str(event.actor or "sales.lifecycle_projection"),
            record_order_payment=bool(event.payload.get("record_order_payment", True)),
        )

    def _release_verified_implementation(self, db: Session, event: Event) -> None:
        installation_project_id = event.payload.get("project_id")
        if not installation_project_id:
            logger.warning(
                "vendor verification event %s has no installation project id",
                event.event_id,
            )
            return
        from app.services import sales_fulfillment

        with _owner_session(db) as owner_db:
            sales_fulfillment.consume_verified_implementation(
                owner_db,
                installation_project_id=coerce_uuid(installation_project_id),
                verification_event_id=event.event_id,
                event_id=event.event_id,
                context=self._context(event, str(installation_project_id)),
            )

    def _advance_released_order(self, db: Session, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get(
            "service_order_id"
        )
        # Non-sales repair/reprovisioning orders keep their manual
        # progression; only the sales chain auto-enters provisioning.
        if not service_order_id or not event.payload.get("sales_order_id"):
            return
        from app.services import sales_fulfillment

        with _owner_session(db) as owner_db:
            sales_fulfillment.consume_service_order_release(
                owner_db,
                service_order_id=coerce_uuid(service_order_id),
                event_id=event.event_id,
                context=self._context(event, str(service_order_id)),
            )

    def _prepare_customer_experience_handoff(self, db: Session, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get(
            "service_order_id"
        )
        # Non-sales repair/reprovisioning orders intentionally have no CX
        # sales handoff. The owner validates all other structural context.
        if not service_order_id or not event.payload.get("sales_order_id"):
            return
        from app.services import customer_experience_handoffs

        with _owner_session(db) as owner_db:
            customer_experience_handoffs.consume_service_order_completion(
                owner_db,
                service_order_id=coerce_uuid(service_order_id),
                event_id=event.event_id,
                context=self._context(event, str(service_order_id)),
            )

    def _fulfill_sales_order(self, db: Session, event: Event) -> None:
        sales_order_id = event.payload.get("sales_order_id")
        handoff_id = event.payload.get("handoff_id")
        if not sales_order_id or not handoff_id:
            logger.warning(
                "customer-experience acceptance event %s lacks sales order "
                "or handoff id",
                event.event_id,
            )
            return
        from app.services import sales_fulfillment

        with _owner_session(db) as owner_db:
            sales_fulfillment.consume_cx_acceptance(
                owner_db,
                sales_order_id=coerce_uuid(sales_order_id),
                handoff_id=coerce_uuid(handoff_id),
                event_id=event.event_id,
                context=self._context(event, str(sales_order_id)),
            )

    def _flag_overdue_acceptance(self, db: Session, event: Event) -> None:
        handoff_id = event.payload.get("entity_id")
        if not handoff_id:
            return
        from app.services import customer_experience_handoffs

        with _owner_session(db) as owner_db:
            customer_experience_handoffs.consume_cx_acceptance_due(
                owner_db,
                handoff_id=coerce_uuid(handoff_id),
                event_id=event.event_id,
                context=self._context(event, str(handoff_id)),
            )
