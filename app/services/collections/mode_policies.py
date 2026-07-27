"""Shared value objects for the mode-specific collections planners.

The planners themselves are one module per owner:
:mod:`app.services.collections.postpaid_policy` and
:mod:`app.services.collections.prepaid_policy`. This module carries the typed
proposal and helpers both consume; it owns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.models.billing_contract import ObligationState
from app.models.collections_case import CollectionsReason
from app.services.domain_errors import DomainError

ACTIONABLE_STATES = (ObligationState.open, ObligationState.partially_resolved)


class CollectionsPolicyError(DomainError):
    """Fail-closed collections-policy error."""


@dataclass(frozen=True)
class CollectionsProposal:
    """Typed decision one mode policy hands to `collections.lifecycle`."""

    reason: CollectionsReason
    account_id: UUID
    subscription_id: UUID
    obligation_id: UUID
    currency: str
    outstanding_amount: Decimal
    due_at: datetime | None


def require_aware(moment: datetime) -> None:
    """Fail closed on a naive evaluation instant."""

    if moment.tzinfo is None:
        raise CollectionsPolicyError(
            code="collections.policy.naive_instant",
            message="Policy evaluation requires a timezone-aware instant.",
        )


def aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive instants in tests; PostgreSQL keeps the offset."""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


__all__ = [
    "ACTIONABLE_STATES",
    "CollectionsPolicyError",
    "CollectionsProposal",
    "aware",
    "require_aware",
]
