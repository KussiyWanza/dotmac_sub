"""Shared billing/dunning communication suppression policy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.customer_service_state import (
    active_outage_subscription_ids,
    subscribers_with_open_infrastructure_down_tickets,
)

#: A customer-impact shield defers billing consequences, so it must expire:
#: an evidence source older than this stops suppressing notices and the
#: normal progression resumes. Kept as a registered setting so operations
#: can widen it during a genuine prolonged incident.
DEFAULT_NOTICE_SHIELD_MAX_HOURS = 72


def _notice_shield_max_hours(db: Session) -> int:
    from app.services.settings_spec import resolve_value

    try:
        value = int(
            resolve_value(db, SettingDomain.billing, "notice_shield_max_hours")
            or DEFAULT_NOTICE_SHIELD_MAX_HOURS
        )
    except (TypeError, ValueError):
        value = DEFAULT_NOTICE_SHIELD_MAX_HOURS
    return max(1, value)


@dataclass(frozen=True)
class BillingCommunicationDecision:
    subscription_id: object
    subscriber_id: object | None
    suppress_expiry_notice: bool
    suppress_suspension_notice: bool
    suppress_dunning_notice: bool
    reason: str | None


def billing_communication_decisions(
    db: Session,
    subscriptions,
) -> dict[object, BillingCommunicationDecision]:
    """Return billing-comms decisions for a batch of subscriptions."""
    rows = list(subscriptions)
    subscription_ids = {sub.id for sub in rows if getattr(sub, "id", None)}
    subscriber_ids = {
        sub.subscriber_id for sub in rows if getattr(sub, "subscriber_id", None)
    }
    shield_max_hours = _notice_shield_max_hours(db)
    outage_ids = (
        active_outage_subscription_ids(db, manual_open_max_hours=shield_max_hours)
        & subscription_ids
    )
    ticket_subscribers = subscribers_with_open_infrastructure_down_tickets(
        db, subscriber_ids, max_age_hours=shield_max_hours
    )

    decisions: dict[object, BillingCommunicationDecision] = {}
    for subscription in rows:
        subscription_id = getattr(subscription, "id", None)
        if subscription_id is None:
            continue
        subscriber_id = getattr(subscription, "subscriber_id", None)
        if subscription_id in outage_ids:
            reason = "active_infrastructure_outage"
        elif subscriber_id in ticket_subscribers:
            reason = "open_infrastructure_down_ticket"
        else:
            reason = None
        suppress = reason is not None
        decisions[subscription_id] = BillingCommunicationDecision(
            subscription_id=subscription_id,
            subscriber_id=subscriber_id,
            suppress_expiry_notice=suppress,
            suppress_suspension_notice=suppress,
            suppress_dunning_notice=suppress,
            reason=reason,
        )
    return decisions
