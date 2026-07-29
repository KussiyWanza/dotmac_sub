"""Request the canonical prepaid service consequence after confirmed funding."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events.types import Event, EventType
from app.services.prepaid_service_renewals import (
    evaluate_prepaid_service_after_settlement,
    retract_prepaid_billing_anchors_after_funding_reversal,
)

logger = logging.getLogger(__name__)

FUNDING_INCREASE_EVENT_TYPES = frozenset(
    {EventType.account_credit_deposited, EventType.payment_received}
)
# A refund, chargeback or reversal is also a funding change. The payment owner
# revokes the entitlements its money had funded and then emits the durable
# reversal event; this adapter asks the renewal owner to re-derive the billing
# anchor so a reversed period cannot keep a stale advanced anchor.
FUNDING_REVERSAL_EVENT_TYPES = frozenset(
    {EventType.payment_refunded, EventType.payment_reversed}
)
HANDLED_EVENT_TYPES = FUNDING_INCREASE_EVENT_TYPES | FUNDING_REVERSAL_EVENT_TYPES


def _require_account_id(event: Event) -> UUID:
    if event.account_id is None:
        raise DomainError(
            code="financial.prepaid_service_renewals.event_account_missing",
            message="Funding-change event has no account identifier.",
            details={"event_id": str(event.event_id)},
        )
    return event.account_id


def _require_payment_id(event: Event) -> UUID:
    payment_id = event.payload.get("payment_id")
    if not payment_id:
        raise DomainError(
            code="financial.prepaid_service_renewals.event_payment_missing",
            message="Funding-change event has no payment identifier.",
            details={"event_id": str(event.event_id)},
        )
    return coerce_uuid(payment_id)


class PrepaidRenewalHandler:
    """Thin event adapter around the prepaid service-renewal owner."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type in FUNDING_REVERSAL_EVENT_TYPES:
            self._handle_funding_reversal(db, event)
            return
        if event.event_type not in FUNDING_INCREASE_EVENT_TYPES:
            return
        account_id = _require_account_id(event)
        payment_id = _require_payment_id(event)
        evaluation = evaluate_prepaid_service_after_settlement(
            db,
            account_id=account_id,
            payment_id=payment_id,
            evidence_ref=f"{event.event_type.value}:{event.event_id}",
        )
        result = evaluation.renewal
        logger.info(
            "prepaid_renewal_after_funding_change",
            extra={
                "event": "prepaid_renewal_after_funding_change",
                "event_id": str(event.event_id),
                "payment_id": str(evaluation.payment_id),
                "account_id": str(account_id),
                "evaluation_disposition": evaluation.disposition.value,
                "renewal_disposition": (
                    result.disposition.value if result is not None else None
                ),
                "scanned": result.scanned if result is not None else 0,
                "funded": result.funded if result is not None else 0,
                "unfunded": result.unfunded if result is not None else 0,
                "already_covered": (
                    result.already_covered if result is not None else 0
                ),
                "missing_price": result.missing_price if result is not None else 0,
                "currency_mismatch": (
                    result.currency_mismatch if result is not None else 0
                ),
                "draft_review_exceptions": (
                    result.draft_review_exceptions if result is not None else 0
                ),
                "renewed_through": (
                    [outcome.renewed_through.isoformat() for outcome in result.renewals]
                    if result is not None
                    else []
                ),
            },
        )

    def _handle_funding_reversal(self, db: Session, event: Event) -> None:
        account_id = _require_account_id(event)
        payment_id = _require_payment_id(event)
        invoice_ids: list[UUID] = []
        for raw in event.payload.get("invoice_ids") or []:
            try:
                invoice_ids.append(coerce_uuid(raw))
            except (TypeError, ValueError):
                continue
        projections = retract_prepaid_billing_anchors_after_funding_reversal(
            db,
            account_id=account_id,
            payment_id=payment_id,
            invoice_ids=tuple(invoice_ids),
            evidence_ref=f"{event.event_type.value}:{event.event_id}",
        )
        logger.info(
            "prepaid_billing_anchor_retracted_after_reversal",
            extra={
                "event": "prepaid_billing_anchor_retracted_after_reversal",
                "event_id": str(event.event_id),
                "event_type": event.event_type.value,
                "payment_id": str(payment_id),
                "account_id": str(account_id),
                "subscriptions_projected": len(projections),
                "subscriptions_retracted": sum(
                    1 for item in projections if item.retracted
                ),
            },
        )


__all__ = [
    "FUNDING_INCREASE_EVENT_TYPES",
    "FUNDING_REVERSAL_EVENT_TYPES",
    "HANDLED_EVENT_TYPES",
    "PrepaidRenewalHandler",
]
