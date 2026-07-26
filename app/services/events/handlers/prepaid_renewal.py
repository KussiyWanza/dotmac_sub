"""Request the canonical prepaid service consequence after confirmed funding."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events.types import Event, EventType
from app.services.prepaid_service_renewals import (
    evaluate_prepaid_service_after_settlement,
)

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {EventType.account_credit_deposited, EventType.payment_received}
)


class PrepaidRenewalHandler:
    """Thin event adapter around the prepaid service-renewal owner."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type not in HANDLED_EVENT_TYPES:
            return
        if event.account_id is None:
            raise DomainError(
                code="financial.prepaid_service_renewals.event_account_missing",
                message="Funding-change event has no account identifier.",
                details={"event_id": str(event.event_id)},
            )
        payment_id = event.payload.get("payment_id")
        if not payment_id:
            raise DomainError(
                code="financial.prepaid_service_renewals.event_payment_missing",
                message="Funding-change event has no payment identifier.",
                details={"event_id": str(event.event_id)},
            )
        evaluation = evaluate_prepaid_service_after_settlement(
            db,
            account_id=event.account_id,
            payment_id=coerce_uuid(payment_id),
            evidence_ref=f"{event.event_type.value}:{event.event_id}",
        )
        result = evaluation.renewal
        logger.info(
            "prepaid_renewal_after_funding_change",
            extra={
                "event": "prepaid_renewal_after_funding_change",
                "event_id": str(event.event_id),
                "payment_id": str(evaluation.payment_id),
                "account_id": str(event.account_id),
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


__all__ = ["HANDLED_EVENT_TYPES", "PrepaidRenewalHandler"]
