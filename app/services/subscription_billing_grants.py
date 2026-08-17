"""Participant owner for exact non-cash subscription service-period grants."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import Subscription
from app.models.subscription_billing_treatment import (
    BillingTreatmentStatus,
    SubscriptionBillingArrangement,
    SubscriptionBillingGrant,
)
from app.services.account_lifecycle import (
    BillingAnchorProjectionCommand,
    BillingAnchorProjectionSource,
    stage_subscription_billing_anchor,
)
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.subscription_billing_treatments import (
    BillingTreatmentDecision,
    resolve_subscription_reference_price,
)


class SubscriptionBillingGrantError(DomainError):
    """Stable transport-neutral non-cash grant failure."""


def _error(
    suffix: str, message: str, **details: object
) -> SubscriptionBillingGrantError:
    return SubscriptionBillingGrantError(
        code=f"financial.subscription_billing_grants.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class BillingGrantOutcome:
    grant_id: UUID
    entitlement_id: UUID
    arrangement_id: UUID
    subscription_id: UUID
    starts_at: datetime
    ends_at: datetime
    reference_amount: Decimal
    currency: str
    replayed: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stage_subscription_billing_grant(
    db: Session,
    *,
    subscription: Subscription,
    decision: BillingTreatmentDecision,
    starts_at: datetime,
    ends_at: datetime,
    actor: str,
    correlation_id: UUID | None = None,
    reference_amount: Decimal | None = None,
) -> BillingGrantOutcome:
    """Flush-only participant for one exact non-cash service period."""
    start = _utc(starts_at)
    end = _utc(ends_at)
    if not decision.grantable or decision.arrangement_id is None:
        raise _error(
            "grant_blocked",
            "The subscription billing treatment is not valid for a service grant.",
            subscription_id=str(subscription.id),
            drift_reason=decision.drift_reason,
        )
    if end <= start:
        raise _error("invalid_grant_period", "Grant end must be after its start.")
    locked_subscription = db.scalar(
        select(Subscription).where(Subscription.id == subscription.id).with_for_update()
    )
    if locked_subscription is None:
        raise _error("subscription_not_found", "The subscription was not found.")
    subscription = locked_subscription
    arrangement = db.scalar(
        select(SubscriptionBillingArrangement)
        .where(SubscriptionBillingArrangement.id == decision.arrangement_id)
        .with_for_update()
    )
    if (
        arrangement is None
        or arrangement.status != BillingTreatmentStatus.active
        or arrangement.subscription_id != subscription.id
        or arrangement.account_id != subscription.subscriber_id
        or arrangement.authorized_offer_id != subscription.offer_id
        or arrangement.treatment != decision.treatment
    ):
        raise _error(
            "arrangement_not_effective",
            "The approved billing treatment no longer matches the subscription.",
            subscription_id=str(subscription.id),
        )
    if start < _utc(arrangement.starts_at):
        raise _error(
            "grant_outside_arrangement",
            "Grant period starts before the approved treatment.",
        )
    if end > _utc(arrangement.ends_at):
        raise _error(
            "grant_outside_arrangement",
            "Grant period exceeds the approved treatment.",
        )
    reference = resolve_subscription_reference_price(
        db, subscription, effective_at=start
    )
    if (
        reference.amount > arrangement.maximum_recurring_amount
        or reference.currency != arrangement.currency
        or reference.billing_cycle != arrangement.billing_cycle
    ):
        raise _error(
            "approved_value_exceeded",
            "Current service value or cadence exceeds the approved billing treatment.",
            subscription_id=str(subscription.id),
        )
    grant_value = round_money(
        reference.amount if reference_amount is None else reference_amount
    )
    if grant_value <= Decimal("0.00") or grant_value > reference.amount:
        raise _error(
            "invalid_reference_amount",
            "Grant reference amount must be positive and no greater than service value.",
            subscription_id=str(subscription.id),
        )
    evidence = (
        f"{decision.arrangement_id}:{subscription.id}:"
        f"{start.isoformat()}:{end.isoformat()}"
    )
    key_hash = _sha256(evidence)
    command_id = uuid5(NAMESPACE_URL, f"billing-grant:{key_hash}")
    resolved_correlation = correlation_id or command_id
    grant = db.scalar(
        select(SubscriptionBillingGrant).where(
            SubscriptionBillingGrant.idempotency_key_sha256 == key_hash
        )
    )
    replayed = grant is not None
    if grant is not None and (
        grant.arrangement_id != arrangement.id
        or grant.subscription_id != subscription.id
        or grant.account_id != subscription.subscriber_id
        or grant.treatment != arrangement.treatment
        or _utc(grant.starts_at) != start
        or _utc(grant.ends_at) != end
        or round_money(grant.reference_amount) != grant_value
        or grant.currency != reference.currency
    ):
        raise _error(
            "idempotency_conflict",
            "Existing grant evidence does not match the requested service period.",
        )
    if grant is None:
        grant = SubscriptionBillingGrant(
            arrangement_id=arrangement.id,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            treatment=arrangement.treatment,
            starts_at=start,
            ends_at=end,
            reference_amount=grant_value,
            currency=reference.currency,
            idempotency_key_sha256=key_hash,
            command_id=command_id,
            correlation_id=resolved_correlation,
            actor=actor,
            reason=arrangement.reason,
        )
        db.add(grant)
        db.flush()
    entitlement = db.scalar(
        select(ServiceEntitlement).where(
            ServiceEntitlement.source_billing_grant_id == grant.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
        )
    )
    if entitlement is None:
        entitlement = ServiceEntitlement(
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            source_billing_grant_id=grant.id,
            starts_at=start,
            ends_at=end,
            amount_funded=Decimal("0.00"),
            currency=reference.currency,
            status=ServiceEntitlementStatus.active,
            metadata_={
                "source": "subscription_billing_treatment",
                "treatment": decision.treatment.value,
                "reference_amount": str(grant_value),
                "arrangement_id": str(arrangement.id),
            },
        )
        db.add(entitlement)
        db.flush()
    elif (
        entitlement.account_id != subscription.subscriber_id
        or entitlement.subscription_id != subscription.id
        or entitlement.source_billing_grant_id != grant.id
        or _utc(entitlement.starts_at) != start
        or _utc(entitlement.ends_at) != end
        or round_money(entitlement.amount_funded) != Decimal("0.00")
        or entitlement.currency != reference.currency
    ):
        raise _error(
            "entitlement_conflict",
            "Grant-linked entitlement evidence conflicts with the approved period.",
        )
    current_anchor = subscription.next_billing_at
    if current_anchor is None or _utc(current_anchor) < end:
        stage_subscription_billing_anchor(
            db,
            subscription,
            BillingAnchorProjectionCommand(
                subscription_id=subscription.id,
                expected_previous=current_anchor,
                target=end,
                source=BillingAnchorProjectionSource.subscription_billing_grant,
                evidence_ref=f"billing-grant:{grant.id}",
            ),
        )
    if not replayed:
        emit_event(
            db,
            EventType.subscription_service_granted,
            {
                "schema_version": 1,
                "grant_id": str(grant.id),
                "arrangement_id": str(arrangement.id),
                "subscription_id": str(subscription.id),
                "entitlement_id": str(entitlement.id),
                "treatment": decision.treatment.value,
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat(),
                "reference_amount": str(grant_value),
                "currency": reference.currency,
                "command_id": str(command_id),
                "correlation_id": str(resolved_correlation),
            },
            actor=actor,
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
        )
    db.flush()
    return BillingGrantOutcome(
        grant_id=grant.id,
        entitlement_id=entitlement.id,
        arrangement_id=arrangement.id,
        subscription_id=subscription.id,
        starts_at=start,
        ends_at=end,
        reference_amount=grant_value,
        currency=reference.currency,
        replayed=replayed,
    )
