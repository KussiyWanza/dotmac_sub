"""Deliver committed materials/vendor/ERP outputs to receipted consumers.

The handler is a thin delivery adapter for the materials chain
(docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md): a committed material-request
approval reaches the receipted ERP-issue consumer, and a committed vendor
invoice approval reaches the receipted payables-export consumer. Each effect
commits atomically with its unique ``(consumer, event_id)`` receipt, so a
redelivery is an exact no-op. ERP failures stay durable pending outbox
deliveries — Sub never infers issuance or payment; ERP outcomes return only
through the durable write-back and polling observations.

A consequence that cannot be applied raises so the event delivery stays
failed and retryable instead of a warning log.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.events.handlers.owner_session import owner_session as _owner_session
from app.services.events.types import Event, EventType

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.field_material_request_approved,
        EventType.vendor_purchase_invoice_approved,
    }
)


class MaterialsLifecycleProjectionHandler:
    """Route committed materials-chain outputs to receipted consumers."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.field_material_request_approved:
            self._request_erp_issue(db, event)
        elif event.event_type == EventType.vendor_purchase_invoice_approved:
            self._request_payables_export(db, event)

    @staticmethod
    def _context(event: Event, scope: str):
        from app.services.owner_commands import CommandContext

        return CommandContext.system(
            actor=str(event.actor or "materials.lifecycle_projection"),
            scope=scope,
            reason=event.event_type.value,
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"event:{event.event_id}",
        )

    def _request_erp_issue(self, db: Session, event: Event) -> None:
        material_request_id = event.payload.get("material_request_id")
        if not material_request_id:
            logger.warning(
                "material approval event %s has no request id", event.event_id
            )
            return
        from app.services.field import material_requests

        with _owner_session(db) as owner_db:
            material_requests.consume_material_request_approved(
                owner_db,
                material_request_id=str(material_request_id),
                event_id=event.event_id,
                context=self._context(event, str(material_request_id)),
            )

    def _request_payables_export(self, db: Session, event: Event) -> None:
        invoice_id = event.payload.get("invoice_id")
        if not invoice_id:
            logger.warning(
                "invoice approval event %s has no invoice id", event.event_id
            )
            return
        from app.services import vendor_purchase_invoices

        with _owner_session(db) as owner_db:
            vendor_purchase_invoices.consume_invoice_approved(
                owner_db,
                invoice_id=str(invoice_id),
                event_id=event.event_id,
                context=self._context(event, str(invoice_id)),
            )
