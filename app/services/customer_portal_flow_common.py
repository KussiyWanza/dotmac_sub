"""Shared helpers for customer portal flow modules."""

import logging
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _compute_total_pages(total: int, per_page: int) -> int:
    """Compute total pages from total count and per_page size."""
    return (total + per_page - 1) // per_page if total else 1


def _resolve_next_billing_date(db: Session, subscription: Any) -> date | None:
    """Resolve the next billing date for a subscription."""
    if not subscription:
        return None
    next_billing_at = getattr(subscription, "next_billing_at", None)
    if isinstance(next_billing_at, datetime):
        from app.services.display_format import display_timezone

        aware = (
            next_billing_at
            if next_billing_at.tzinfo is not None
            else next_billing_at.replace(tzinfo=UTC)
        )
        return aware.astimezone(display_timezone(db)).date()
    start_at = getattr(subscription, "start_at", None) or getattr(
        subscription, "created_at", None
    )
    offer_id = getattr(subscription, "offer_id", None)
    if not start_at or not offer_id:
        return None
    from app.services.catalog.subscriptions import (
        _compute_next_billing_at,
        _resolve_billing_cycle,
    )

    offer_version_id = getattr(subscription, "offer_version_id", None)
    cycle = _resolve_billing_cycle(
        db,
        str(offer_id),
        str(offer_version_id) if offer_version_id else None,
    )
    next_bill = cast(datetime, _compute_next_billing_at(start_at, cycle))
    for _ in range(240):
        if next_bill.date() >= date.today():
            break
        next_bill = cast(datetime, _compute_next_billing_at(next_bill, cycle))
    return next_bill.date()


__all__ = ["_compute_total_pages", "_resolve_next_billing_date"]
