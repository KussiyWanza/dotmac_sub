"""Per-subscription SLA policy resolution and scoring (shadow phase).

Owner: ``customer.service_level`` (OUTAGE_SLA_SPINE §4). Eligibility composes
typed lifecycle and entitlement histories; positive monitoring comes only from
exact-subscription RADIUS accounting; downtime comes only from the outage
accrual ledger. Effective policy changes segment the calculation. Missing
evidence stays unknown and yields bounds, never guessed uptime or a final pass.
Recorded results append an immutable revision plus exact eligibility and
monitoring snapshots. Customer display still uses the legacy availability
owner until PR 3 performs the reviewed atomic cutover.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import (
    BillingMode,
    CatalogOffer,
    SlaProfile,
    Subscription,
)
from app.models.catalog import (
    SlaPolicyVersion as SlaPolicyVersionRecord,
)
from app.models.sla import (
    SlaEligibilityInterval,
    SlaMonitoringInterval,
    SlaPeriodScoreRevision,
)
from app.services import sla_admin_review as _sla_admin_review
from app.services.billing.contracts import postpaid_entitlement_history_for_period
from app.services.domain_errors import DomainError
from app.services.network.customer_outage_accrual import intervals_for_subscription
from app.services.network.radius_sessions import accounting_coverage_for_period
from app.services.prepaid_service_coverage import prepaid_coverage_history_for_period
from app.services.service_impact_contracts import (
    SLA_CALENDAR_TIMEZONE,
    ImpactState,
    SlaPlanFamily,
    SlaPolicySegmentScore,
    SlaPolicySource,
    SlaPolicyVersion,
    SlaScore,
    SlaVerdict,
)
from app.services.sla_admin_review import (
    SlaAdminDisplayDecision,
    SlaAdminReview,
    SlaAdminReviewQuery,
)
from app.services.sla_interval_algebra import (
    Span,
    intersect,
    subtract,
    total_seconds,
    union,
)
from app.services.subscription_lifecycle_history import lifecycle_history_for_period

if TYPE_CHECKING:  # avoids a runtime import cycle via owner_commands
    from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)

# Downtime consuming more than this share of the allowed budget flags the
# period at-risk before it breaches.
_AT_RISK_BUDGET_SHARE = 0.8


@dataclass(frozen=True, slots=True)
class SlaShadowComparison:
    """Ledger-based score next to the legacy read-time availability.

    Approximate by design (calendar month-to-date vs trailing window); the
    comparison exists to find discrepancies before cutover, not to publish
    two operational admin authorities. New code uses ``review_admin_period``
    so both methods cover one exact closed calendar month.
    """

    score: SlaScore
    legacy_availability_percent: float | None
    delta_percent: float | None


#: Approved precedence, highest first (design §4). A subscription contract
#: beats an account contract, which beats the subscribed offer version, which
#: beats the commercial family default, which beats the internal measurement
#: policy. Encoded once, here, so the resolver and any future reporting
#: surface cannot disagree about which term won.
_PRECEDENCE: tuple[SlaPolicySource, ...] = (
    SlaPolicySource.subscription_contract,
    SlaPolicySource.account_contract,
    SlaPolicySource.offer_version,
    SlaPolicySource.plan_family,
    SlaPolicySource.internal_measurement,
)


def _row_to_policy(row: SlaPolicyVersionRecord) -> SlaPolicyVersion:
    """Persisted row → the immutable typed contract the scorer consumes."""

    return SlaPolicyVersion(
        policy_id=row.id,
        version=row.version,
        source=SlaPolicySource(row.source),
        effective_from=_utc(row.effective_from) or datetime.now(UTC),
        effective_to=_utc(row.effective_to),
        availability_target_percent=(
            float(row.availability_target_percent)
            if row.availability_target_percent is not None
            else None
        ),
        calendar_timezone=row.calendar_timezone or SLA_CALENDAR_TIMEZONE,
        maintenance_excludable=bool(row.maintenance_excludable),
        credit_percent_per_breach=(
            float(row.credit_percent_per_breach)
            if row.credit_percent_per_breach is not None
            else None
        ),
        credit_cap_percent=(
            float(row.credit_cap_percent)
            if row.credit_cap_percent is not None
            else None
        ),
    )


def _persisted_versions_covering(
    db: Session,
    subscription: Subscription,
    *,
    start: datetime,
    end: datetime,
) -> list[SlaPolicyVersionRecord]:
    """Every persisted version whose effective range overlaps [start, end).

    Ordered by precedence then by ``effective_from`` so the caller can take
    the winning source without re-deciding the ranking.
    """

    scope_filters = [
        SlaPolicyVersionRecord.source == SlaPolicySource.internal_measurement.value,
        SlaPolicyVersionRecord.subscription_id == subscription.id,
    ]
    if subscription.subscriber_id is not None:
        scope_filters.append(
            SlaPolicyVersionRecord.subscriber_id == subscription.subscriber_id
        )
    if subscription.offer_id is not None:
        scope_filters.append(SlaPolicyVersionRecord.offer_id == subscription.offer_id)
        # The family default applies through the subscribed offer. db.get hits
        # the identity map on the repeat lookup in _legacy_offer_policy, so
        # this costs at most one query per resolve.
        offer = db.get(CatalogOffer, subscription.offer_id)
        family = getattr(offer, "plan_family", None) if offer is not None else None
        if family:
            scope_filters.append(SlaPolicyVersionRecord.plan_family == family)

    rows = (
        db.query(SlaPolicyVersionRecord)
        .filter(
            or_(*scope_filters),
            SlaPolicyVersionRecord.effective_from < end,
            or_(
                SlaPolicyVersionRecord.effective_to.is_(None),
                SlaPolicyVersionRecord.effective_to > start,
            ),
        )
        .all()
    )
    rank = {source.value: index for index, source in enumerate(_PRECEDENCE)}
    return sorted(
        rows,
        key=lambda row: (
            rank.get(row.source, len(rank)),
            _utc(row.effective_from) or datetime.min.replace(tzinfo=UTC),
        ),
    )


def resolve_effective_policy(
    db: Session,
    subscription: Subscription,
    *,
    at: datetime | None = None,
) -> SlaPolicyVersion | None:
    """The effective contractual policy at ``at``, or None — never invented.

    During the shadow migration BOTH sources are live, so both are ranked by
    the one precedence order. Preferring any persisted row over the legacy
    derivation would let a global ``internal_measurement`` policy — the
    LOWEST precedence there is — mask a customer's actual offer SLA. The
    legacy profile therefore competes at ``offer_version`` precedence, which
    is what it represents.

    Ties go to the persisted row: it is the authority the legacy derivation
    is being retired in favour of.
    """

    instant = at or datetime.now(UTC)
    covering = _persisted_versions_covering(
        db, subscription, start=instant, end=instant + timedelta(microseconds=1)
    )
    candidates = [_row_to_policy(row) for row in covering]
    legacy = _legacy_offer_policy(db, subscription)
    if (
        legacy is not None
        and legacy.effective_from <= instant
        and (legacy.effective_to is None or instant < legacy.effective_to)
    ):
        candidates.append(legacy)
    if not candidates:
        return None
    rank = {source: index for index, source in enumerate(_PRECEDENCE)}
    # min() is stable, so persisted rows (added first) win an equal-precedence
    # tie against the legacy derivation.
    return min(candidates, key=lambda policy: rank[policy.source])


def _legacy_offer_policy(
    db: Session, subscription: Subscription
) -> SlaPolicyVersion | None:
    """The mutable-``SlaProfile`` derivation this slice supersedes.

    Retained as the fallback for subscriptions with no persisted policy during
    shadow scoring. Retired by the cutover PR.
    """

    offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    profile = (
        db.get(SlaProfile, offer.sla_profile_id)
        if offer is not None and offer.sla_profile_id
        else None
    )
    if profile is None or profile.uptime_percent is None:
        return None
    effective_from = profile.created_at or datetime.now(UTC)
    if effective_from.tzinfo is None:
        effective_from = effective_from.replace(tzinfo=UTC)
    return SlaPolicyVersion(
        policy_id=profile.id,
        version=1,
        source=SlaPolicySource.offer_version,
        effective_from=effective_from,
        effective_to=None,
        availability_target_percent=float(profile.uptime_percent),
        credit_percent_per_breach=(
            float(profile.credit_percent)
            if profile.credit_percent is not None
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class PolicySegment:
    """One contiguous stretch of a reporting period under a single policy.

    A mid-period policy change must split the calculation rather than apply
    the latest terms retroactively (design §4), so scoring consumes segments,
    not one policy. ``policy`` is None where no contractual policy was in
    force for that stretch — measured availability still applies there, under
    the ``no_contractual_sla`` verdict.
    """

    start: datetime
    end: datetime
    policy: SlaPolicyVersion | None

    @property
    def seconds(self) -> int:
        return max(int((self.end - self.start).total_seconds()), 0)


def policy_segments_for_period(
    db: Session,
    subscription: Subscription,
    *,
    period_start: datetime,
    period_end: datetime,
) -> tuple[PolicySegment, ...]:
    """Split ``[period_start, period_end)`` at every effective policy change.

    Boundaries come from the winning policy at each instant, so a change of
    *precedence* splits the period exactly like a change of terms: if a
    subscription contract starts mid-month, the offer policy governs the
    stretch before it and the subscription contract the stretch after.
    """

    covering = _persisted_versions_covering(
        db, subscription, start=period_start, end=period_end
    )
    boundaries = {period_start, period_end}
    for row in covering:
        for edge in (_utc(row.effective_from), _utc(row.effective_to)):
            if edge is not None and period_start < edge < period_end:
                boundaries.add(edge)
    legacy = _legacy_offer_policy(db, subscription)
    if legacy is not None:
        for edge in (legacy.effective_from, legacy.effective_to):
            if edge is not None and period_start < edge < period_end:
                boundaries.add(edge)

    ordered = sorted(boundaries)
    segments: list[PolicySegment] = []
    for start, end in zip(ordered, ordered[1:], strict=False):
        if end <= start:
            continue
        segments.append(
            PolicySegment(
                start=start,
                end=end,
                # Resolve at the segment's own start: the winning policy is a
                # property of the instant, not of the period.
                policy=resolve_effective_policy(db, subscription, at=start),
            )
        )
    return tuple(segments)


def period_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The Africa/Lagos calendar month containing ``now``, as UTC instants."""

    zone = ZoneInfo(SLA_CALENDAR_TIMEZONE)
    local = now.astimezone(zone)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _merge_windows(
    windows: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Union overlapping windows so concurrent incidents never double-count."""

    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def score_subscription_period(
    db: Session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> SlaScore:
    """Calculate one honest shadow score from typed historical owner facts."""

    return _calculate_period_score(
        db,
        subscription,
        now=now,
        period_start=period_start,
        period_end=period_end,
    ).score


def _complete_verdict(
    *,
    policy: SlaPolicyVersion | None,
    eligible_seconds: int,
    unavailable_seconds: int,
) -> SlaVerdict:
    if eligible_seconds <= 0:
        return SlaVerdict.unavailable
    if policy is None or policy.availability_target_percent is None:
        return SlaVerdict.no_contractual_sla
    availability = 100.0 * (eligible_seconds - unavailable_seconds) / eligible_seconds
    target = policy.availability_target_percent
    if availability < target:
        return SlaVerdict.breach
    allowed_downtime = eligible_seconds * (100.0 - target) / 100.0
    if allowed_downtime > 0 and unavailable_seconds >= (
        _AT_RISK_BUDGET_SHARE * allowed_downtime
    ):
        return SlaVerdict.at_risk
    return SlaVerdict.passing


@dataclass(frozen=True, slots=True)
class _EligibilitySnapshot:
    span: Span
    evidence_grade: str
    entitlement_source: str
    entitlement_evidence_ids: tuple[UUID, ...]
    lifecycle_evidence_ids: tuple[UUID, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _MonitoringSnapshot:
    span: Span
    source_id: UUID
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _PeriodCalculation:
    score: SlaScore
    evaluated_at: datetime
    eligibility: tuple[_EligibilitySnapshot, ...]
    monitoring: tuple[_MonitoringSnapshot, ...]
    lifecycle_evidence_ids: tuple[UUID, ...]


def _fingerprint(*parts: object) -> str:
    material = "\0".join(str(part) for part in parts).encode()
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _entitlement_history(
    db: Session,
    subscription: Subscription,
    *,
    period_start: datetime,
    period_end: datetime,
) -> tuple[tuple[tuple[Span, str, UUID], ...], bool, tuple[str, ...]]:
    if subscription.billing_mode is BillingMode.prepaid:
        prepaid_history = prepaid_coverage_history_for_period(
            db,
            subscription,
            period_start=period_start,
            period_end=period_end,
        )
        intervals = tuple(
            (Span(item.starts_at, item.ends_at), item.source.value, item.source_id)
            for item in prepaid_history.intervals
        )
        return intervals, prepaid_history.complete, prepaid_history.issues

    postpaid_history = postpaid_entitlement_history_for_period(
        db,
        subscription_id=subscription.id,
        period_start=period_start,
        period_end=period_end,
    )
    intervals = tuple(
        (
            Span(item.starts_at, item.ends_at),
            "billing_contract_version",
            item.contract_version_id,
        )
        for item in postpaid_history.intervals
    )
    return intervals, postpaid_history.complete, postpaid_history.issues


def _serialize_policy(policy: SlaPolicyVersion | None) -> dict[str, object] | None:
    if policy is None:
        return None
    return {
        "policy_id": str(policy.policy_id),
        "version": policy.version,
        "source": policy.source.value,
        "effective_from": policy.effective_from.isoformat(),
        "effective_to": (
            policy.effective_to.isoformat() if policy.effective_to is not None else None
        ),
        "availability_target_percent": policy.availability_target_percent,
        "calendar_timezone": policy.calendar_timezone,
        "maintenance_excludable": policy.maintenance_excludable,
        "credit_percent_per_breach": policy.credit_percent_per_breach,
        "credit_cap_percent": policy.credit_cap_percent,
    }


def _segment_verdict(
    *,
    policy: SlaPolicyVersion | None,
    eligible_seconds: int,
    unavailable_seconds: int,
    excluded_seconds: int,
    unknown_seconds: int,
    evidence_complete: bool,
) -> SlaVerdict:
    denominator = eligible_seconds - excluded_seconds
    if denominator <= 0:
        return SlaVerdict.unavailable
    if policy is None or policy.availability_target_percent is None:
        return SlaVerdict.no_contractual_sla
    upper = 100.0 * (denominator - unavailable_seconds) / denominator
    if upper < policy.availability_target_percent:
        # A breach is final even under the best possible interpretation of
        # every unknown second. Incomplete evidence may prove failure; it may
        # never prove success.
        return SlaVerdict.breach
    if not evidence_complete or unknown_seconds > 0:
        return SlaVerdict.unavailable
    return _complete_verdict(
        policy=policy,
        eligible_seconds=denominator,
        unavailable_seconds=unavailable_seconds,
    )


def _calculate_period_score(
    db: Session,
    subscription: Subscription,
    *,
    now: datetime | None,
    period_start: datetime | None,
    period_end: datetime | None,
) -> _PeriodCalculation:
    evaluated_at = _utc(now or datetime.now(UTC)) or datetime.now(UTC)
    if period_start is None or period_end is None:
        period_start, period_end = period_bounds(evaluated_at)
    start = _utc(period_start)
    end = _utc(period_end)
    if start is None or end is None or end <= start:
        raise ValueError("SLA scoring requires a positive UTC reporting period")
    if evaluated_at < start:
        raise ValueError("SLA scoring cannot evaluate before the period begins")
    measured_end = min(end, evaluated_at)
    if measured_end <= start:
        measured_end = start + timedelta(microseconds=1)

    lifecycle = lifecycle_history_for_period(
        db,
        subscription.id,
        period_start=start,
        period_end=measured_end,
    )
    entitlements, entitlement_complete, entitlement_issues = _entitlement_history(
        db,
        subscription,
        period_start=start,
        period_end=measured_end,
    )

    eligibility_rows: list[_EligibilitySnapshot] = []
    lifecycle_ids = tuple(lifecycle.supporting_evidence_ids)
    for active in lifecycle.active_windows:
        active_end = _utc(active.end) or measured_end
        active_span = Span(_utc(active.start) or start, min(active_end, measured_end))
        for entitlement_span, entitlement_source, entitlement_id in entitlements:
            for eligible_span in intersect((active_span,), (entitlement_span,)):
                fingerprint = _fingerprint(
                    subscription.id,
                    eligible_span.start.isoformat(),
                    eligible_span.end.isoformat(),
                    entitlement_source,
                    entitlement_id,
                    *sorted(str(item) for item in lifecycle_ids),
                )
                eligibility_rows.append(
                    _EligibilitySnapshot(
                        span=eligible_span,
                        evidence_grade="authoritative",
                        entitlement_source=entitlement_source,
                        entitlement_evidence_ids=(entitlement_id,),
                        lifecycle_evidence_ids=lifecycle_ids,
                        fingerprint=fingerprint,
                    )
                )
    eligible = union(item.span for item in eligibility_rows)

    accounting = accounting_coverage_for_period(
        db,
        subscription.id,
        period_start=start,
        period_end=measured_end,
    )
    monitoring_rows = tuple(
        _MonitoringSnapshot(
            span=Span(item.starts_at, item.ends_at),
            source_id=item.source_id,
            fingerprint=_fingerprint(
                subscription.id,
                item.starts_at.isoformat(),
                item.ends_at.isoformat(),
                "radius_accounting_session",
                item.source_id,
            ),
        )
        for item in accounting.intervals
    )
    monitored = intersect(eligible, (item.span for item in monitoring_rows))

    qualifying: list[Span] = []
    exclusion_candidates: list[Span] = []
    outage_rows: list[dict[str, object]] = []
    interval_ids: list[str] = []
    for interval in intervals_for_subscription(db, subscription.id, since=start):
        if interval.state != ImpactState.confirmed_unavailable.value:
            continue
        interval_start = max(_utc(interval.started_at) or start, start)
        interval_end = min(_utc(interval.ended_at) or measured_end, measured_end)
        if interval_end <= interval_start:
            continue
        span = Span(interval_start, interval_end)
        interval_ids.append(str(interval.id))
        outage_rows.append(
            {
                "id": str(interval.id),
                "start": interval_start.isoformat(),
                "end": interval_end.isoformat(),
                "quality": interval.quality,
                "exclusion": interval.exclusion_candidate,
                "finalized_at": (
                    (_utc(interval.finalized_at) or interval.finalized_at).isoformat()
                    if interval.finalized_at is not None
                    else None
                ),
            }
        )
        if interval.exclusion_candidate is None and interval.quality == "exact":
            qualifying.append(span)
        else:
            exclusion_candidates.append(span)

    downtime = intersect(eligible, qualifying)
    excluded = subtract(intersect(eligible, exclusion_candidates), downtime)
    known = union(monitored, downtime, excluded)
    unknown = subtract(eligible, known)

    base_complete = lifecycle.complete and entitlement_complete and accounting.complete
    completeness_issues = [
        *(f"lifecycle:{issue}" for issue in lifecycle.issues),
        *(f"entitlement:{issue}" for issue in entitlement_issues),
        *(f"monitoring:{issue}" for issue in accounting.issues),
    ]
    if unknown:
        completeness_issues.append("monitoring:unknown_eligible_coverage")
    evidence_complete = base_complete and not unknown

    policy_segments = policy_segments_for_period(
        db,
        subscription,
        period_start=start,
        period_end=measured_end,
    )
    segment_scores: list[SlaPolicySegmentScore] = []
    for segment in policy_segments:
        segment_span = Span(segment.start, segment.end)
        segment_eligible = intersect(eligible, (segment_span,))
        segment_downtime = intersect(downtime, (segment_span,))
        segment_excluded = intersect(excluded, (segment_span,))
        segment_unknown = intersect(unknown, (segment_span,))
        values = {
            "eligible_seconds": total_seconds(segment_eligible),
            "unavailable_seconds": total_seconds(segment_downtime),
            "excluded_seconds": total_seconds(segment_excluded),
            "unknown_seconds": total_seconds(segment_unknown),
        }
        segment_scores.append(
            SlaPolicySegmentScore(
                start=segment.start,
                end=segment.end,
                **values,
                verdict=_segment_verdict(
                    policy=segment.policy,
                    evidence_complete=base_complete,
                    **values,
                ),
                policy=segment.policy,
            )
        )

    eligible_seconds = total_seconds(eligible)
    unavailable_seconds = total_seconds(downtime)
    excluded_seconds = total_seconds(excluded)
    unknown_seconds = total_seconds(unknown)
    contractual = [
        item
        for item in segment_scores
        if item.policy is not None and item.eligible_seconds - item.excluded_seconds > 0
    ]
    if eligible_seconds <= 0:
        verdict = SlaVerdict.unavailable
    elif not contractual:
        verdict = SlaVerdict.no_contractual_sla
    elif any(item.verdict is SlaVerdict.breach for item in contractual):
        verdict = SlaVerdict.breach
    elif not evidence_complete:
        verdict = SlaVerdict.unavailable
    elif any(item.verdict is SlaVerdict.at_risk for item in contractual):
        verdict = SlaVerdict.at_risk
    else:
        verdict = SlaVerdict.passing

    policies = {
        item.policy.policy_id: item.policy
        for item in segment_scores
        if item.policy is not None
    }
    compatibility_policy = next(iter(policies.values())) if len(policies) == 1 else None
    digest_payload = {
        "subscription_id": str(subscription.id),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "evaluated_through": measured_end.isoformat(),
        "eligibility": [
            {
                "start": item.span.start.isoformat(),
                "end": item.span.end.isoformat(),
                "grade": item.evidence_grade,
                "source": item.entitlement_source,
                "entitlement_ids": [
                    str(value) for value in item.entitlement_evidence_ids
                ],
                "lifecycle_ids": [str(value) for value in item.lifecycle_evidence_ids],
            }
            for item in eligibility_rows
        ],
        "monitoring": [
            {
                "start": item.span.start.isoformat(),
                "end": item.span.end.isoformat(),
                "source_id": str(item.source_id),
            }
            for item in monitoring_rows
        ],
        "outages": outage_rows,
        "policies": [
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "policy": _serialize_policy(item.policy),
            }
            for item in segment_scores
        ],
        "completeness_issues": sorted(set(completeness_issues)),
    }
    evidence_digest = _fingerprint(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":"))
    )
    score = SlaScore(
        subscription_id=subscription.id,
        period_start=start,
        period_end=end,
        eligible_seconds=eligible_seconds,
        unavailable_seconds=unavailable_seconds,
        excluded_seconds=excluded_seconds,
        unknown_seconds=unknown_seconds,
        verdict=verdict,
        policy=compatibility_policy,
        evidence_digest=evidence_digest,
        interval_ids=tuple(sorted(interval_ids)),
        policy_segments=tuple(segment_scores),
        evidence_complete=evidence_complete,
        completeness_issues=tuple(sorted(set(completeness_issues))),
    )
    return _PeriodCalculation(
        score=score,
        evaluated_at=evaluated_at,
        eligibility=tuple(eligibility_rows),
        monitoring=monitoring_rows,
        lifecycle_evidence_ids=lifecycle_ids,
    )


class SlaScoreError(DomainError):
    """Period-score recording failed closed at the owner boundary."""


@dataclass(frozen=True, slots=True)
class RecordPeriodScoreCommand:
    """Typed request to calculate and append one immutable score revision."""

    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    evaluated_at: datetime
    context: CommandContext


@dataclass(frozen=True, slots=True)
class PeriodScoreOutcome:
    """Immutable result; callers never receive mutable score ORM entities."""

    score_revision_id: UUID
    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    revision: int
    supersedes_id: UUID | None
    verdict: SlaVerdict
    evidence_complete: bool
    evidence_digest: str
    replayed: bool


def _score_command(name: str):
    from app.services.owner_commands import OwnerCommandDefinition

    return OwnerCommandDefinition(
        owner="customer.service_level",
        concern="immutable SLA period-score revisions and evidence snapshots",
        name=name,
    )


def _score_outcome(
    row: SlaPeriodScoreRevision, *, replayed: bool
) -> PeriodScoreOutcome:
    return PeriodScoreOutcome(
        score_revision_id=row.id,
        subscription_id=row.subscription_id,
        period_start=_utc(row.period_start) or row.period_start,
        period_end=_utc(row.period_end) or row.period_end,
        revision=row.revision,
        supersedes_id=row.supersedes_id,
        verdict=SlaVerdict(row.verdict),
        evidence_complete=row.evidence_complete,
        evidence_digest=row.evidence_digest,
        replayed=replayed,
    )


def record_period_score(
    db: Session, command: RecordPeriodScoreCommand
) -> PeriodScoreOutcome:
    """Append a reproducible score revision and exact input snapshots.

    The subscription row is the single lock target for this series.  Repeating
    the same command identity and evidence replays; changing inputs under the
    same identity conflicts; changed evidence under a new identity appends the
    next revision.  No prior result is updated or deleted.
    """

    from sqlalchemy.exc import IntegrityError

    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType
    from app.services.owner_commands import execute_owner_command

    def operation() -> PeriodScoreOutcome:
        locked_subscription = (
            db.query(Subscription)
            .filter(Subscription.id == command.subscription_id)
            .with_for_update()
            .one_or_none()
        )
        if locked_subscription is None:
            raise SlaScoreError(
                code="customer.service_level.unknown_subscription",
                message="No subscription exists for the requested SLA score.",
                details={"subscription_id": str(command.subscription_id)},
            )

        calculation = _calculate_period_score(
            db,
            locked_subscription,
            now=command.evaluated_at,
            period_start=command.period_start,
            period_end=command.period_end,
        )
        score = calculation.score

        identity_filters = [
            SlaPeriodScoreRevision.command_id == command.context.command_id
        ]
        if command.context.idempotency_key is not None:
            identity_filters.append(
                SlaPeriodScoreRevision.command_idempotency_key
                == command.context.idempotency_key
            )
        from sqlalchemy import or_ as sql_or

        prior_identity = (
            db.query(SlaPeriodScoreRevision)
            .filter(sql_or(*identity_filters))
            .with_for_update()
            .one_or_none()
        )
        if prior_identity is not None:
            if prior_identity.evidence_digest == score.evidence_digest:
                return _score_outcome(prior_identity, replayed=True)
            raise SlaScoreError(
                code="customer.service_level.score_idempotency_conflict",
                message=(
                    "This score command identity was already used for different "
                    "evidence; use a new command identity for a new revision."
                ),
                details={"score_revision_id": str(prior_identity.id)},
            )

        duplicate_evidence = (
            db.query(SlaPeriodScoreRevision)
            .filter(
                SlaPeriodScoreRevision.subscription_id == command.subscription_id,
                SlaPeriodScoreRevision.period_start == score.period_start,
                SlaPeriodScoreRevision.period_end == score.period_end,
                SlaPeriodScoreRevision.evidence_digest == score.evidence_digest,
            )
            .with_for_update()
            .one_or_none()
        )
        if duplicate_evidence is not None:
            raise SlaScoreError(
                code="customer.service_level.duplicate_score_evidence",
                message=(
                    "This exact period evidence was already recorded under a "
                    "different command identity."
                ),
                details={"score_revision_id": str(duplicate_evidence.id)},
            )

        latest = (
            db.query(SlaPeriodScoreRevision)
            .filter(
                SlaPeriodScoreRevision.subscription_id == command.subscription_id,
                SlaPeriodScoreRevision.period_start == score.period_start,
                SlaPeriodScoreRevision.period_end == score.period_end,
            )
            .order_by(SlaPeriodScoreRevision.revision.desc())
            .with_for_update()
            .first()
        )
        policy_payload = [
            {
                "start": segment.start.isoformat(),
                "end": segment.end.isoformat(),
                "policy": _serialize_policy(segment.policy),
                "eligible_seconds": segment.eligible_seconds,
                "unavailable_seconds": segment.unavailable_seconds,
                "excluded_seconds": segment.excluded_seconds,
                "unknown_seconds": segment.unknown_seconds,
                "verdict": segment.verdict.value,
                "availability_lower_bound_percent": (
                    segment.availability_lower_bound_percent
                ),
                "availability_upper_bound_percent": (
                    segment.availability_upper_bound_percent
                ),
            }
            for segment in score.policy_segments
        ]
        policy_ids = sorted(
            {
                str(segment.policy.policy_id)
                for segment in score.policy_segments
                if segment.policy is not None
            }
        )
        record = SlaPeriodScoreRevision(
            subscription_id=score.subscription_id,
            period_start=score.period_start,
            period_end=score.period_end,
            evaluated_at=calculation.evaluated_at,
            revision=(latest.revision + 1) if latest is not None else 1,
            supersedes_id=latest.id if latest is not None else None,
            eligible_seconds=score.eligible_seconds,
            unavailable_seconds=score.unavailable_seconds,
            excluded_seconds=score.excluded_seconds,
            unknown_seconds=score.unknown_seconds,
            verdict=score.verdict.value,
            evidence_complete=score.evidence_complete,
            completeness_issues=list(score.completeness_issues),
            availability_lower_bound_percent=(
                Decimal(str(score.availability_lower_bound_percent))
                if score.availability_lower_bound_percent is not None
                else None
            ),
            availability_upper_bound_percent=(
                Decimal(str(score.availability_upper_bound_percent))
                if score.availability_upper_bound_percent is not None
                else None
            ),
            policy_segments=policy_payload,
            policy_version_ids=policy_ids,
            outage_interval_ids=list(score.interval_ids),
            lifecycle_evidence_ids=[
                str(item) for item in calculation.lifecycle_evidence_ids
            ],
            evidence_digest=score.evidence_digest,
            recorded_by=command.context.actor,
            command_id=command.context.command_id,
            command_idempotency_key=command.context.idempotency_key,
            correlation_id=command.context.correlation_id,
        )
        db.add(record)
        try:
            db.flush()
            for eligibility_item in calculation.eligibility:
                db.add(
                    SlaEligibilityInterval(
                        score_revision_id=record.id,
                        subscription_id=record.subscription_id,
                        starts_at=eligibility_item.span.start,
                        ends_at=eligibility_item.span.end,
                        evidence_grade=eligibility_item.evidence_grade,
                        entitlement_source=eligibility_item.entitlement_source,
                        entitlement_evidence_ids=[
                            str(value)
                            for value in eligibility_item.entitlement_evidence_ids
                        ],
                        lifecycle_evidence_ids=[
                            str(value)
                            for value in eligibility_item.lifecycle_evidence_ids
                        ],
                        fingerprint=eligibility_item.fingerprint,
                    )
                )
            for monitoring_item in calculation.monitoring:
                db.add(
                    SlaMonitoringInterval(
                        score_revision_id=record.id,
                        subscription_id=record.subscription_id,
                        starts_at=monitoring_item.span.start,
                        ends_at=monitoring_item.span.end,
                        source="radius_accounting_session",
                        source_id=monitoring_item.source_id,
                        fingerprint=monitoring_item.fingerprint,
                    )
                )
            db.flush()
        except IntegrityError as exc:
            constraint = getattr(
                getattr(getattr(exc, "orig", None), "diag", None),
                "constraint_name",
                None,
            )
            if constraint in {
                "uq_sla_period_scores_period_revision",
                "uq_sla_period_scores_period_evidence",
                "uq_sla_period_scores_command_id",
                "uq_sla_period_scores_idempotency_key",
            }:
                raise SlaScoreError(
                    code="customer.service_level.concurrent_score_conflict",
                    message=(
                        "Another writer recorded this score series concurrently; "
                        "re-read and retry."
                    ),
                    details={"constraint": constraint},
                ) from exc
            raise

        emit_event(
            db,
            EventType.sla_period_score_recorded,
            {
                "score_revision_id": str(record.id),
                "subscription_id": str(record.subscription_id),
                "period_start": record.period_start.isoformat(),
                "period_end": record.period_end.isoformat(),
                "revision": record.revision,
                "supersedes_id": (
                    str(record.supersedes_id)
                    if record.supersedes_id is not None
                    else None
                ),
                "verdict": record.verdict,
                "evidence_complete": record.evidence_complete,
                "evidence_digest": record.evidence_digest,
            },
            actor=command.context.actor,
        )
        return _score_outcome(record, replayed=False)

    return execute_owner_command(
        db,
        definition=_score_command("record_period_score"),
        context=command.context,
        operation=operation,
    )


def shadow_compare(
    db: Session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
) -> SlaShadowComparison:
    """Ledger score vs the legacy read-time availability, for the cutover gate."""

    evaluated_at = now or datetime.now(UTC)
    score = score_subscription_period(db, subscription, now=evaluated_at)
    legacy_percent: float | None = None
    try:
        from app.services.topology.customer_availability import (
            customer_availability,
        )

        elapsed_days = max(
            int(
                (evaluated_at - score.period_start).total_seconds()
                // int(timedelta(days=1).total_seconds())
            ),
            1,
        )
        legacy = customer_availability(
            db, subscription, days=elapsed_days, now=evaluated_at
        )
        if legacy.has_infrastructure_coverage:
            legacy_percent = float(legacy.effective_uptime_percent)
    except Exception:
        logger.warning(
            "Legacy availability comparison failed for subscription %s",
            subscription.id,
            exc_info=True,
        )
    measured = score.measured_availability_percent
    delta = (
        round(measured - legacy_percent, 3)
        if measured is not None and legacy_percent is not None
        else None
    )
    return SlaShadowComparison(
        score=score,
        legacy_availability_percent=legacy_percent,
        delta_percent=delta,
    )


def resolve_admin_display_authority(db: Session) -> SlaAdminDisplayDecision:
    """Public customer.service_level query for the inert admin selector."""

    return _sla_admin_review.resolve_admin_display_authority(db)


def review_admin_period(db: Session, query: SlaAdminReviewQuery) -> SlaAdminReview:
    """Public customer.service_level query for one closed-period comparison."""

    return _sla_admin_review.review_admin_period(db, query)


# --- recording a new effective-dated version --------------------------------


class SlaPolicyError(DomainError):
    """Invalid policy-version input (adapter: HTTP 400)."""


@dataclass(frozen=True, slots=True)
class RecordPolicyVersionCommand:
    """Typed input for establishing new contractual terms.

    ``effective_from`` is the instant the terms begin, not the instant the
    record was typed — a contract signed today may start next month, and the
    resolver answers by instant, so the two must not be conflated.

    There is deliberately no ``policy_key``: the series identity is derived
    from ``(source, scope)`` inside the owner. A caller-supplied key would let
    two different keys target the same subscription for the same period,
    producing two equal-precedence policies and an undefined resolver winner —
    the exclusion constraint only forbids overlap *within* a key.
    """

    source: SlaPolicySource
    effective_from: datetime
    availability_target_percent: float | None
    context: CommandContext
    subscription_id: UUID | None = None
    subscriber_id: UUID | None = None
    offer_id: UUID | None = None
    plan_family: SlaPlanFamily | None = None
    calendar_timezone: str = SLA_CALENDAR_TIMEZONE
    maintenance_excludable: bool = True
    credit_percent_per_breach: float | None = None
    credit_cap_percent: float | None = None
    contract_reference: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyVersionOutcome:
    """Immutable result of recording terms — never the ORM row itself.

    Returning the entity would hand callers a mutable handle to a record whose
    entire purpose is being append-only, and would let a caller edit terms
    after the command validated them.
    """

    policy_version_id: UUID
    policy_key: str
    version: int
    source: SlaPolicySource
    effective_from: datetime
    superseded_version_id: UUID | None
    superseded_at: datetime | None
    replayed: bool
    fingerprint: str


def derive_policy_key(
    source: SlaPolicySource,
    *,
    subscription_id: UUID | None = None,
    subscriber_id: UUID | None = None,
    offer_id: UUID | None = None,
    plan_family: SlaPlanFamily | None = None,
) -> str:
    """The series identity for one real scope. Owner-derived, never supplied.

    One scope has exactly one policy series, so "the terms in force for this
    subscription at T" cannot have two equal-precedence answers. The database
    enforces the same shape: a unique index on (source, scope) inside the
    exclusion constraint.
    """

    if source is SlaPolicySource.subscription_contract:
        if subscription_id is None:
            raise SlaPolicyError(
                code="customer.service_level.scope_required",
                message="A subscription contract needs its subscription.",
            )
        return f"subscription_contract:{subscription_id}"
    if source is SlaPolicySource.account_contract:
        if subscriber_id is None:
            raise SlaPolicyError(
                code="customer.service_level.scope_required",
                message="An account contract needs its account.",
            )
        return f"account_contract:{subscriber_id}"
    if source is SlaPolicySource.offer_version:
        if offer_id is None:
            raise SlaPolicyError(
                code="customer.service_level.scope_required",
                message="An offer policy needs its offer.",
            )
        return f"offer_version:{offer_id}"
    if source is SlaPolicySource.plan_family:
        if plan_family is None:
            raise SlaPolicyError(
                code="customer.service_level.scope_required",
                message="A family policy needs its plan family.",
            )
        if not isinstance(plan_family, SlaPlanFamily):
            raise SlaPolicyError(
                code="customer.service_level.unknown_plan_family",
                message=(
                    f"{plan_family!r} is not an SLA-enabled plan family "
                    f"({', '.join(SlaPlanFamily)})."
                ),
            )
        return f"plan_family:{plan_family.value}"
    return "internal_measurement:global"


#: Constraints that genuinely represent a lost race. A raw collision does not
#: reveal WHAT the winner wrote, so all of these are reported as retryable:
#: the retry re-reads the winner and decides replay-or-conflict from evidence
#: rather than guessing here.
_RACE_CONSTRAINTS = frozenset(
    {
        "ex_sla_policy_versions_no_overlap",
        "uq_sla_policy_versions_key_version",
        "uq_sla_policy_versions_fingerprint",
        "uq_sla_policy_versions_idempotency_key",
    }
)

#: Constraints that can only mean bad input. Everything outside these two sets
#: is an unexpected defect and MUST stay unexpected — swallowing an unknown
#: constraint or driver failure into a tidy domain error hides the bug.
_INPUT_CONSTRAINTS = frozenset(
    {
        "ck_sla_policy_versions_version",
        "ck_sla_policy_versions_range",
        "ck_sla_policy_versions_target_bounds",
        "ck_sla_policy_versions_contractual_target",
        "ck_sla_policy_versions_scope_matches_source",
        "ck_sla_policy_versions_key_is_derived",
        # A family outside the closed vocabulary is bad input, not a lost
        # race — retrying it can never succeed.
        "ck_sla_policy_versions_plan_family_vocab",
    }
)


def _validate_scope(db: Session, command: RecordPolicyVersionCommand) -> None:
    """Exactly one source-matching scope, and its parent must exist.

    Without this, an extra scope id or a nonexistent parent reaches PostgreSQL
    and surfaces as an IntegrityError, which the writer would then mislabel as
    a concurrency conflict — telling the caller to retry something that can
    never succeed.
    """

    from app.models.subscriber import Subscriber

    supplied = {
        "subscription_id": command.subscription_id,
        "subscriber_id": command.subscriber_id,
        "offer_id": command.offer_id,
        "plan_family": command.plan_family,
    }
    expected = {
        SlaPolicySource.subscription_contract: "subscription_id",
        SlaPolicySource.account_contract: "subscriber_id",
        SlaPolicySource.offer_version: "offer_id",
        SlaPolicySource.plan_family: "plan_family",
        SlaPolicySource.internal_measurement: None,
    }[command.source]

    extra = sorted(k for k, v in supplied.items() if v is not None and k != expected)
    if extra:
        raise SlaPolicyError(
            code="customer.service_level.invalid_scope",
            message=(
                f"A {command.source.value} policy must carry only its own "
                f"scope; got {', '.join(extra)}."
            ),
            details={"unexpected_scope": extra},
        )
    if expected is None:
        return
    scope_id = supplied[expected]
    if scope_id is None:
        raise SlaPolicyError(
            code="customer.service_level.scope_required",
            message=f"A {command.source.value} policy needs its {expected}.",
        )
    # A family is a closed vocabulary, not a row: there is no parent to look
    # up, so membership is the whole existence check. derive_policy_key
    # re-validates it, but doing it here keeps the error a scope error rather
    # than surfacing from key derivation.
    if expected == "plan_family":
        if not isinstance(scope_id, SlaPlanFamily):
            raise SlaPolicyError(
                code="customer.service_level.unknown_plan_family",
                message=(
                    f"{scope_id!r} is not an SLA-enabled plan family "
                    f"({', '.join(SlaPlanFamily)})."
                ),
                details={"plan_family": str(scope_id)},
            )
        return

    parent = {
        "subscription_id": Subscription,
        "subscriber_id": Subscriber,
        "offer_id": CatalogOffer,
    }[expected]
    if db.get(parent, scope_id) is None:
        raise SlaPolicyError(
            code="customer.service_level.unknown_scope",
            message=f"No {parent.__name__} exists for the supplied {expected}.",
            details={expected: str(scope_id)},
        )


def _policy_fingerprint(command: RecordPolicyVersionCommand, key: str) -> str:
    """Stable identity of the *intent*, for replay arbitration.

    A retry of the same command must return the original outcome rather than
    raising ``not_after_current`` against the row it already created.
    """

    material = "\0".join(
        str(part)
        for part in (
            key,
            command.source.value,
            _utc(command.effective_from),
            command.availability_target_percent,
            command.calendar_timezone,
            command.maintenance_excludable,
            command.credit_percent_per_breach,
            command.credit_cap_percent,
            command.contract_reference,
        )
    )
    return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"


def _policy_command(name: str):
    from app.services.owner_commands import OwnerCommandDefinition

    return OwnerCommandDefinition(
        owner="customer.service_level",
        concern="immutable effective-dated SLA policy versions",
        name=name,
    )


def _outcome(record: SlaPolicyVersionRecord, *, replayed: bool) -> PolicyVersionOutcome:
    return PolicyVersionOutcome(
        policy_version_id=record.id,
        policy_key=record.policy_key,
        version=record.version,
        source=SlaPolicySource(record.source),
        effective_from=_utc(record.effective_from) or datetime.now(UTC),
        superseded_version_id=record.supersedes_id,
        # Nothing was superseded on version 1, so there is no instant at which
        # it happened. Reporting effective_from here would invent one.
        superseded_at=(
            _utc(record.effective_from) if record.supersedes_id is not None else None
        ),
        replayed=replayed,
        fingerprint=record.command_fingerprint or "",
    )


def record_policy_version(
    db: Session, command: RecordPolicyVersionCommand
) -> PolicyVersionOutcome:
    """Append a new version, closing the one it supersedes. Never edits terms.

    A period already scored keeps the terms that were in force when it was
    measured — that is the whole reason this table is append-only. Changing
    terms means closing the open version at ``effective_from`` and inserting
    the next, so the two abut exactly and the exclusion constraint stays
    satisfiable.

    Concurrency and replay:

    - the series row set is locked ``FOR UPDATE`` before any decision, so two
      concurrent writers on one scope serialise rather than both reading a
      stale "current version";
    - a replay of the same command fingerprint returns the original outcome
      instead of raising against the row it already created;
    - a different command that loses the race surfaces
      ``concurrent_version_conflict`` rather than a raw database error.

    Backdating behind an already-closed version is refused: it would silently
    rewrite a scored period, which is exactly what superseding ``SlaProfile``
    was meant to stop.
    """

    from sqlalchemy.exc import IntegrityError

    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType
    from app.services.owner_commands import execute_owner_command

    def operation() -> PolicyVersionOutcome:
        effective_from = _utc(command.effective_from)
        if effective_from is None:
            raise SlaPolicyError(
                code="customer.service_level.missing_effective_from",
                message="A policy version needs the instant its terms begin.",
            )
        if (
            command.source is not SlaPolicySource.internal_measurement
            and command.availability_target_percent is None
        ):
            raise SlaPolicyError(
                code="customer.service_level.contractual_target_required",
                message=(
                    "A contractual policy must state its availability target; "
                    "only the internal measurement policy may omit one."
                ),
            )

        _validate_scope(db, command)
        policy_key = derive_policy_key(
            command.source,
            subscription_id=command.subscription_id,
            subscriber_id=command.subscriber_id,
            offer_id=command.offer_id,
            plan_family=command.plan_family,
        )
        fingerprint = _policy_fingerprint(command, policy_key)
        idempotency_key = getattr(command.context, "idempotency_key", None)

        # Idempotency semantics. When a key is supplied it — not the
        # fingerprint — is the identity:
        #   same key + same fingerprint  -> replay the original outcome
        #   same key + different inputs  -> conflict, never a second version
        # Replaying on a fingerprint match alone would report success under a
        # key that was never persisted, leaving that key free to append later
        # with different terms instead of conflicting.
        duplicate_terms = (
            db.query(SlaPolicyVersionRecord)
            .filter(SlaPolicyVersionRecord.command_fingerprint == fingerprint)
            .one_or_none()
        )
        if idempotency_key:
            prior = (
                db.query(SlaPolicyVersionRecord)
                .filter(
                    SlaPolicyVersionRecord.command_idempotency_key == idempotency_key
                )
                .with_for_update()
                .one_or_none()
            )
            if prior is not None:
                if prior.command_fingerprint == fingerprint:
                    return _outcome(prior, replayed=True)
                raise SlaPolicyError(
                    code="customer.service_level.idempotency_conflict",
                    message=(
                        "This idempotency key was already used for different "
                        "policy terms; issue a new key or resend the original "
                        "command unchanged."
                    ),
                    details={"policy_key": prior.policy_key},
                )
            if duplicate_terms is not None:
                # These exact terms exist under a DIFFERENT key. Reporting
                # success would reserve nothing for this key. Retrying cannot
                # help, so this is a conflict, not concurrency.
                raise SlaPolicyError(
                    code="customer.service_level.duplicate_policy_terms",
                    message=(
                        "These exact policy terms were already recorded under "
                        "a different command; resend that command's key to "
                        "replay it, or change the terms."
                    ),
                    details={"policy_key": duplicate_terms.policy_key},
                )
        elif duplicate_terms is not None:
            # No key supplied, so the fingerprint is the only identity there
            # is and replaying on it is the best available contract.
            return _outcome(duplicate_terms, replayed=True)

        # Serialise writers on this series before reading "current version".
        existing = (
            db.query(SlaPolicyVersionRecord)
            .filter(SlaPolicyVersionRecord.policy_key == policy_key)
            .order_by(SlaPolicyVersionRecord.version.desc())
            .with_for_update()
            .all()
        )

        for row in existing:
            row_end = _utc(row.effective_to)
            if row_end is not None and row_end > effective_from:
                raise SlaPolicyError(
                    code="customer.service_level.would_rewrite_closed_period",
                    message=(
                        "Backdating behind a closed version would rewrite a "
                        "period that may already have been scored."
                    ),
                    details={"policy_key": policy_key},
                )

        open_row = next((r for r in existing if r.effective_to is None), None)
        if open_row is not None:
            if (_utc(open_row.effective_from) or effective_from) >= effective_from:
                raise SlaPolicyError(
                    code="customer.service_level.not_after_current",
                    message=(
                        "New terms must begin after the version currently in force."
                    ),
                    details={"policy_key": policy_key},
                )
            open_row.effective_to = effective_from

        record = SlaPolicyVersionRecord(
            policy_key=policy_key,
            version=(existing[0].version + 1) if existing else 1,
            source=command.source.value,
            subscription_id=command.subscription_id,
            subscriber_id=command.subscriber_id,
            offer_id=command.offer_id,
            plan_family=(
                command.plan_family.value if command.plan_family is not None else None
            ),
            effective_from=effective_from,
            effective_to=None,
            availability_target_percent=(
                Decimal(str(command.availability_target_percent))
                if command.availability_target_percent is not None
                else None
            ),
            calendar_timezone=command.calendar_timezone,
            maintenance_excludable=command.maintenance_excludable,
            credit_percent_per_breach=(
                Decimal(str(command.credit_percent_per_breach))
                if command.credit_percent_per_breach is not None
                else None
            ),
            credit_cap_percent=(
                Decimal(str(command.credit_cap_percent))
                if command.credit_cap_percent is not None
                else None
            ),
            contract_reference=command.contract_reference,
            established_by=command.context.actor,
            supersedes_id=open_row.id if open_row is not None else None,
            command_fingerprint=fingerprint,
            command_idempotency_key=idempotency_key,
        )
        db.add(record)
        try:
            db.flush()
        except IntegrityError as exc:
            constraint = getattr(
                getattr(getattr(exc, "orig", None), "diag", None),
                "constraint_name",
                None,
            )
            if constraint in _RACE_CONSTRAINTS:
                # A raw collision does not reveal whether the winner wrote the
                # SAME terms or different ones — a concurrent identical retry
                # hits the key constraint exactly as a conflicting one does.
                # So this is retryable; the retry re-reads the winner above and
                # decides replay-or-conflict from evidence.
                raise SlaPolicyError(
                    code="customer.service_level.concurrent_version_conflict",
                    message=(
                        "Another writer recorded a version of this policy "
                        "concurrently; re-read the series and retry."
                    ),
                    details={"policy_key": policy_key, "constraint": constraint},
                ) from exc
            if constraint in _INPUT_CONSTRAINTS:
                raise SlaPolicyError(
                    code="customer.service_level.invalid_policy_version",
                    message="The policy version was rejected by the database.",
                    details={"policy_key": policy_key, "constraint": constraint},
                ) from exc
            # An unrecognised constraint or a driver failure is an unexpected
            # defect. Dressing it as a domain error would hide the bug behind a
            # tidy contract the caller can neither act on nor report.
            raise

        emit_event(
            db,
            EventType.sla_policy_version_recorded,
            {
                "policy_key": record.policy_key,
                "version": record.version,
                "source": record.source,
                "effective_from": effective_from.isoformat(),
                "supersedes_id": str(open_row.id) if open_row is not None else None,
                "fingerprint": fingerprint,
            },
            actor=command.context.actor,
        )
        return _outcome(record, replayed=False)

    return execute_owner_command(
        db,
        definition=_policy_command("record_policy_version"),
        context=command.context,
        operation=operation,
    )
