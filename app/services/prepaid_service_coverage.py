"""Canonical current-service coverage evidence for prepaid access policy.

This owner answers whether a collectible prepaid subscription is funded or has
an explicit non-financial service grant at a point in time.  A projected
``next_billing_at`` date is diagnostic state only: without one of the evidence
rows below it is an unresolved projection and must never authorize restoration
or adverse enforcement.

Historical paid-invoice rows are repaired into ``ServiceEntitlement`` by the
canonical reconciliation owner. They are not read-time coverage evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import Subscription
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionStatus,
)


class PrepaidCoverageSource(StrEnum):
    funded_entitlement = "funded_entitlement"
    service_extension_grant = "service_extension_grant"


class PrepaidCoverageStatus(StrEnum):
    covered = "covered"
    uncovered_due = "uncovered_due"
    unresolved_projection = "unresolved_projection"


@dataclass(frozen=True, slots=True)
class PrepaidCoverageEvidence:
    source: PrepaidCoverageSource
    source_id: UUID
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class PrepaidServiceCoverageDecision:
    subscription_id: UUID
    account_id: UUID
    as_of: datetime
    status: PrepaidCoverageStatus
    evidence: PrepaidCoverageEvidence | None
    projected_paid_through: datetime | None

    @property
    def covered(self) -> bool:
        return self.status == PrepaidCoverageStatus.covered


@dataclass(frozen=True, slots=True)
class PrepaidCoverageInterval:
    """One exact funded or explicitly granted half-open service interval."""

    subscription_id: UUID
    source: PrepaidCoverageSource
    source_id: UUID
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class PrepaidCoveragePeriodHistory:
    """Exact prepaid entitlement facts and projection-completeness evidence."""

    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    intervals: tuple[PrepaidCoverageInterval, ...]
    complete: bool
    issues: tuple[str, ...]


def _as_utc(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def _covers_period(
    intervals: list[PrepaidCoverageInterval], *, start: datetime, end: datetime
) -> bool:
    cursor = start
    for interval in sorted(intervals, key=lambda item: (item.starts_at, item.ends_at)):
        interval_start = max(_as_utc(interval.starts_at), start)
        interval_end = min(_as_utc(interval.ends_at), end)
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return cursor >= end


def prepaid_coverage_history_for_period(
    db: Session,
    subscription: Subscription,
    *,
    period_start: datetime,
    period_end: datetime,
) -> PrepaidCoveragePeriodHistory:
    """Return exact entitlement/grant intervals overlapping one SLA period.

    Missing rows normally mean the service was not funded and therefore was
    not eligible.  The one exception is a mutable ``next_billing_at`` that
    claims paid-through time without exact evidence: that is an unresolved
    projection, so the history is incomplete rather than silently uncovered.
    """

    start = _as_utc(period_start)
    end = _as_utc(period_end)
    if end <= start:
        raise ValueError("Prepaid coverage history requires a positive UTC period")

    intervals: list[PrepaidCoverageInterval] = []
    entitlements = db.scalars(
        select(ServiceEntitlement).where(
            ServiceEntitlement.subscription_id == subscription.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at < end,
            ServiceEntitlement.ends_at > start,
        )
    ).all()
    intervals.extend(
        PrepaidCoverageInterval(
            subscription_id=subscription.id,
            source=PrepaidCoverageSource.funded_entitlement,
            source_id=row.id,
            starts_at=max(_as_utc(row.starts_at), start),
            ends_at=min(_as_utc(row.ends_at), end),
        )
        for row in entitlements
    )

    extensions = db.execute(
        select(
            ServiceExtensionEntry.id,
            ServiceExtensionEntry.grant_starts_at,
            ServiceExtensionEntry.grant_ends_at,
        )
        .join(
            ServiceExtension, ServiceExtension.id == ServiceExtensionEntry.extension_id
        )
        .where(
            ServiceExtensionEntry.subscription_id == subscription.id,
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_starts_at.isnot(None),
            ServiceExtensionEntry.grant_starts_at < end,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at > start,
        )
    ).all()
    for row in extensions:
        assert row.grant_starts_at is not None
        assert row.grant_ends_at is not None
        intervals.append(
            PrepaidCoverageInterval(
                subscription_id=subscription.id,
                source=PrepaidCoverageSource.service_extension_grant,
                source_id=row.id,
                starts_at=max(_as_utc(row.grant_starts_at), start),
                ends_at=min(_as_utc(row.grant_ends_at), end),
            )
        )

    issues: list[str] = []
    paid_through = (
        _as_utc(subscription.next_billing_at)
        if subscription.next_billing_at is not None
        else None
    )
    projected_start = max(
        start,
        _as_utc(subscription.start_at) if subscription.start_at is not None else start,
    )
    projected_end = min(end, paid_through) if paid_through is not None else start
    if projected_end > projected_start and not _covers_period(
        intervals, start=projected_start, end=projected_end
    ):
        issues.append("unresolved_paid_through_projection")

    return PrepaidCoveragePeriodHistory(
        subscription_id=subscription.id,
        period_start=start,
        period_end=end,
        intervals=tuple(
            sorted(
                intervals,
                key=lambda item: (item.starts_at, item.ends_at, item.source_id),
            )
        ),
        complete=not issues,
        issues=tuple(issues),
    )


def resolve_prepaid_service_coverage(
    db: Session,
    subscriptions: list[Subscription],
    *,
    as_of: datetime | None = None,
) -> dict[UUID, PrepaidServiceCoverageDecision]:
    """Resolve exact current coverage for a bounded subscription cohort."""
    observed_at = _as_utc(as_of or datetime.now(UTC))
    if not subscriptions:
        return {}
    subscription_ids = [subscription.id for subscription in subscriptions]
    evidence: dict[UUID, PrepaidCoverageEvidence] = {}

    # Canonical funded-period evidence.  Deterministic ordering makes duplicate
    # active rows harmless to the read decision while reconciliation reports the
    # invariant violation separately.
    entitlements = db.scalars(
        select(ServiceEntitlement)
        .where(
            ServiceEntitlement.subscription_id.in_(subscription_ids),
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at <= observed_at,
            ServiceEntitlement.ends_at > observed_at,
        )
        .order_by(
            ServiceEntitlement.subscription_id,
            ServiceEntitlement.starts_at.desc(),
            ServiceEntitlement.id.desc(),
        )
    ).all()
    for entitlement in entitlements:
        evidence.setdefault(
            entitlement.subscription_id,
            PrepaidCoverageEvidence(
                source=PrepaidCoverageSource.funded_entitlement,
                source_id=entitlement.id,
                starts_at=entitlement.starts_at,
                ends_at=entitlement.ends_at,
            ),
        )

    # An applied service extension is an explicit access grant only for the
    # added interval.  It does not fabricate financial entitlement for the
    # original service period.
    extension_rows = db.execute(
        select(
            ServiceExtensionEntry.subscription_id,
            ServiceExtensionEntry.id,
            ServiceExtensionEntry.grant_starts_at,
            ServiceExtensionEntry.grant_ends_at,
        )
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtensionEntry.subscription_id.in_(subscription_ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_starts_at.isnot(None),
            ServiceExtensionEntry.grant_starts_at <= observed_at,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at > observed_at,
        )
        .order_by(
            ServiceExtensionEntry.subscription_id,
            ServiceExtensionEntry.grant_starts_at.desc(),
            ServiceExtensionEntry.id.desc(),
        )
    ).all()
    for extension_row in extension_rows:
        if extension_row.subscription_id in evidence:
            continue
        assert extension_row.grant_starts_at is not None
        assert extension_row.grant_ends_at is not None
        evidence[extension_row.subscription_id] = PrepaidCoverageEvidence(
            source=PrepaidCoverageSource.service_extension_grant,
            source_id=extension_row.id,
            starts_at=extension_row.grant_starts_at,
            ends_at=extension_row.grant_ends_at,
        )

    decisions: dict[UUID, PrepaidServiceCoverageDecision] = {}
    for subscription in subscriptions:
        current_evidence = evidence.get(subscription.id)
        paid_through = subscription.next_billing_at
        projected_future = bool(
            paid_through is not None and _as_utc(paid_through) > observed_at
        )
        status = (
            PrepaidCoverageStatus.covered
            if current_evidence is not None
            else (
                PrepaidCoverageStatus.unresolved_projection
                if projected_future
                else PrepaidCoverageStatus.uncovered_due
            )
        )
        decisions[subscription.id] = PrepaidServiceCoverageDecision(
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            as_of=observed_at,
            status=status,
            evidence=current_evidence,
            projected_paid_through=paid_through,
        )
    return decisions
