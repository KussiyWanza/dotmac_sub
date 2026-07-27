"""`collections.postpaid_policy` (ADR 0007 Phase 5, section 8).

Read-only planner over one exact overdue collectible receivable obligation.
Returns a typed proposal for `collections.lifecycle`; decides no consequence
and mutates nothing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing_contract import AccountingTreatment, BillingObligation
from app.models.collections_case import CollectionsReason
from app.services.collections.mode_policies import (
    ACTIONABLE_STATES,
    CollectionsProposal,
    aware,
    require_aware,
)


def plan_postpaid_consequence(
    db: Session,
    *,
    obligation: BillingObligation,
    now: datetime,
) -> CollectionsProposal | None:
    """Propose a case for one exact overdue collectible receivable.

    Returns ``None`` when the obligation is not an actionable overdue
    receivable — not due yet, already resolved, or not a receivable at all.
    """

    require_aware(now)
    if obligation.accounting_treatment is not AccountingTreatment.receivable:
        return None
    if obligation.state not in ACTIONABLE_STATES:
        return None
    due_at = aware(obligation.due_at)
    if due_at is None or due_at > now:
        return None

    outstanding = Decimal(obligation.gross_amount) - Decimal(obligation.resolved_amount)
    if outstanding <= 0:
        return None

    return CollectionsProposal(
        reason=CollectionsReason.postpaid_overdue,
        account_id=obligation.account_id,
        subscription_id=obligation.subscription_id,
        obligation_id=obligation.id,
        currency=obligation.currency,
        outstanding_amount=outstanding,
        due_at=due_at,
    )


__all__ = ["plan_postpaid_consequence"]
