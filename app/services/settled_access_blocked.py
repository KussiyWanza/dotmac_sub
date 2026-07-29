"""Worklist for accounts that paid but are still walled off.

Reason scoping is intentional: a payment does not lift a FUP, admin, or fraud
lock, and no payment path clears an explicit lifecycle override. What was wrong
was the silence — the restore owner returned a bare ``False``, so a customer who
had genuinely settled stayed dark with no operator-visible reason and no
required action.

This module turns the typed restoration outcome into a durable, deduplicated,
actionable operator worklist entry. It decides nothing about access and clears
no lock; it is a consequence projection of the lifecycle owner's decision. The
entry resolves itself the moment access is genuinely restored.

No money tolerance is introduced anywhere here. A sub-naira residue correctly
blocks restore; the point is that it becomes VISIBLE.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.admin_alert import AdminAlert
from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.services.admin_alerts import (
    AlertFinding,
    resolve_alert_by_fingerprint,
    sync_alert,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.services.account_lifecycle import SubscriptionRestorationResult

logger = logging.getLogger(__name__)

SETTLED_ACCESS_BLOCKED_PREFIX = "financially_settled_but_access_blocked:"
SETTLED_ACCESS_BLOCKED_CATEGORY = "billing"
SETTLED_ACCESS_BLOCKED_SOURCE = "financial.access_restoration"


def worklist_fingerprint(subscription_id: str) -> str:
    return f"{SETTLED_ACCESS_BLOCKED_PREFIX}{subscription_id}"


def _severity(result: SubscriptionRestorationResult) -> AlertSeverity:
    # A blocker a payment-side trigger is authorized to clear but did not is a
    # real routing defect, not an expected policy hold.
    if result.payment_clearable_blockers:
        return AlertSeverity.critical
    return AlertSeverity.warning


def _title(result: SubscriptionRestorationResult) -> str:
    blockers = ", ".join(result.remaining_blockers) or "no active lock"
    return f"Paid but still blocked: {blockers}"


def _summary(result: SubscriptionRestorationResult) -> str:
    return (
        f"Payment-side trigger {result.trigger!r} settled and cleared "
        f"{result.resolved_lock_count} lock(s), but access was not restored "
        f"({result.outcome.value}). Remaining blockers: "
        f"{', '.join(result.remaining_blockers) or 'none'}. "
        f"Required action: {result.required_action or 'none'}."
    )


def stage_financially_settled_but_access_blocked(
    db: Session,
    result: SubscriptionRestorationResult,
) -> AdminAlert | None:
    """Record one durable, deduplicated worklist entry for this outcome.

    Flush-only: the lifecycle owner's transaction stays in charge of the
    commit. Idempotent by fingerprint, so repeated payment attempts on the same
    still-blocked subscription refresh one entry instead of spamming the queue.
    """

    if not result.financially_settled_but_access_blocked:
        return None

    fingerprint = worklist_fingerprint(result.subscription_id)
    sync_alert(
        db,
        AlertFinding(
            fingerprint=fingerprint,
            category=SETTLED_ACCESS_BLOCKED_CATEGORY,
            source=SETTLED_ACCESS_BLOCKED_SOURCE,
            severity=_severity(result),
            title=_title(result),
            summary=_summary(result),
            details=result.to_dict(),
            target_url=f"/admin/customers/{result.account_id}",
        ),
    )
    _note_metric(result)
    logger.warning(
        "financially_settled_but_access_blocked",
        extra={
            "event": "financially_settled_but_access_blocked",
            **result.to_dict(),
        },
    )
    return db.scalar(select(AdminAlert).where(AdminAlert.fingerprint == fingerprint))


def clear_financially_settled_but_access_blocked(
    db: Session,
    subscription_id: str,
) -> AdminAlert | None:
    """Resolve the worklist entry once the customer is genuinely back on."""
    return resolve_alert_by_fingerprint(
        db,
        worklist_fingerprint(subscription_id),
        mark_notifications_read=True,
    )


def open_settled_access_blocked_worklist(
    db: Session,
    *,
    limit: int = 200,
) -> list[AdminAlert]:
    """Return the open worklist, newest first, for staff surfaces."""
    return list(
        db.scalars(
            select(AdminAlert)
            .where(
                AdminAlert.fingerprint.like(f"{SETTLED_ACCESS_BLOCKED_PREFIX}%"),
                AdminAlert.status != AlertStatus.resolved,
            )
            .order_by(AdminAlert.last_seen_at.desc())
            .limit(limit)
        ).all()
    )


def _note_metric(result: SubscriptionRestorationResult) -> None:
    try:
        from app.metrics import SETTLED_ACCESS_BLOCKED

        for blocker in result.remaining_blockers or ("none",):
            SETTLED_ACCESS_BLOCKED.labels(
                blocker=blocker,
                trigger=result.trigger,
                required_action=result.required_action or "none",
            ).inc()
    except Exception:  # pragma: no cover - metrics must never break a decision
        pass


__all__ = [
    "SETTLED_ACCESS_BLOCKED_CATEGORY",
    "SETTLED_ACCESS_BLOCKED_PREFIX",
    "SETTLED_ACCESS_BLOCKED_SOURCE",
    "clear_financially_settled_but_access_blocked",
    "open_settled_access_blocked_worklist",
    "stage_financially_settled_but_access_blocked",
    "worklist_fingerprint",
]
