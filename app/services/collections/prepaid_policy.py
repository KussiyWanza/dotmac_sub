"""`collections.prepaid_policy` (ADR 0007 Phase 5, section 8).

Read-only planner over one exact uncovered prepaid obligation and the typed
per-currency funding position. No receivable is created for enforcement
convenience, and nothing here mutates state or decides access.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing_contract import (
    AccountingTreatment,
    BillingObligation,
    BillingRecordAuthority,
)
from app.models.collections_case import CollectionsReason
from app.services.billing.customer_subledger import resolve_position
from app.services.collections.mode_policies import (
    ACTIONABLE_STATES,
    CollectionsProposal,
    aware,
    require_aware,
)


def plan_prepaid_consequence(
    db: Session,
    *,
    obligation: BillingObligation,
    now: datetime,
    authority: BillingRecordAuthority | None = None,
) -> CollectionsProposal | None:
    """Propose a case for one exact uncovered prepaid obligation.

    The obligation is underfunded when its outstanding amount exceeds the
    account's typed prepaid funding plus unapplied credit in the same
    currency.
    """

    require_aware(now)
    if obligation.accounting_treatment is not AccountingTreatment.prepaid_consumption:
        return None
    if obligation.state not in ACTIONABLE_STATES:
        return None
    period_start = aware(obligation.period_start)
    if period_start is None or period_start > now:
        # The service period has not started; nothing is uncovered yet.
        return None

    outstanding = Decimal(obligation.gross_amount) - Decimal(obligation.resolved_amount)
    if outstanding <= 0:
        return None

    position = resolve_position(
        db,
        account_id=obligation.account_id,
        currency=obligation.currency,
        authority=authority,
    )
    available = position.prepaid_funding_reserved + position.unapplied_customer_credit
    if available >= outstanding:
        return None

    return CollectionsProposal(
        reason=CollectionsReason.prepaid_underfunded,
        account_id=obligation.account_id,
        subscription_id=obligation.subscription_id,
        obligation_id=obligation.id,
        currency=obligation.currency,
        outstanding_amount=outstanding,
        due_at=aware(obligation.due_at),
    )


__all__ = ["plan_prepaid_consequence"]
