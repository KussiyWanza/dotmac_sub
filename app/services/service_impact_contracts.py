"""Shared typed vocabulary for the outage/SLA spine.

Design contract: docs/designs/OUTAGE_SLA_SPINE.md (policy baseline approved
2026-08-02). These immutable value objects are the one impact vocabulary the
impact resolver, downtime ledger, SLA scorer, maintenance lifecycle, and UI
surfaces share. Exposure is never downtime: ``potentially_affected`` states
topology, ``confirmed_unavailable`` requires the approved evidence rules, and
``unknown`` is never silently treated as up, down, or SLA uptime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

# Approved accrual constants (design §2): recovery is finalized only after at
# least this many healthy observations spanning at least this window; the
# interval still ends at the first healthy observation.
RECOVERY_HOLD_MIN_OBSERVATIONS = 2
RECOVERY_HOLD_WINDOW = timedelta(minutes=5)

# Approved maintenance notice (design §5, NCC directive 2025-05-25).
MAINTENANCE_NOTICE_DAYS = 7

# Instants are stored UTC; calendar periods use this zone (design §2, §4).
SLA_CALENDAR_TIMEZONE = "Africa/Lagos"

# NCC-classified major outages beyond this duration create a mandatory
# compensation obligation/review (design §6).
MAJOR_OUTAGE_COMPENSATION_THRESHOLD = timedelta(hours=24)


class ImpactState(StrEnum):
    """Per-subscription service-impact vocabulary (design §1).

    ``potentially_affected`` is topological exposure from
    network.outage_impact. ``confirmed_unavailable`` requires provider-fault
    evidence under the approved rules. ``unknown`` is stale/missing telemetry
    and never becomes unavailable, available, or SLA uptime.
    """

    potentially_affected = "potentially_affected"
    confirmed_unavailable = "confirmed_unavailable"
    degraded = "degraded"
    restored = "restored"
    unknown = "unknown"
    excluded = "excluded"


#: States that may accrue contractual downtime; degradation only where the
#: applicable SLA defines a measurable threshold (design §1).
ACCRUING_IMPACT_STATES = frozenset(
    {ImpactState.confirmed_unavailable, ImpactState.degraded}
)


class ImpactEvidenceKind(StrEnum):
    """How an impact state was proven (design §1)."""

    shared_boundary_failure = "shared_boundary_failure"
    independent_observation = "independent_observation"
    provider_fault_review = "provider_fault_review"
    operator_override = "operator_override"
    recovery_observation = "recovery_observation"


class IntervalQuality(StrEnum):
    """Evidence quality of a downtime interval (design: backfill rules).

    Only ``exact`` intervals enter contractual calculations by default;
    ``estimated`` and ``unavailable`` are labelled, never silently treated as
    contractual truth.
    """

    exact = "exact"
    estimated = "estimated"
    unavailable = "unavailable"


class SlaVerdict(StrEnum):
    """Per-period SLA outcome (design §4).

    ``no_contractual_sla`` renders measured availability without inventing a
    target; ``unavailable`` means the evidence cannot support a verdict.
    """

    passing = "passing"
    at_risk = "at_risk"
    breach = "breach"
    unavailable = "unavailable"
    no_contractual_sla = "no_contractual_sla"


class SlaPlanFamily(StrEnum):
    """Commercial families eligible to own one SLA default.

    Catalog classification is settings-driven and may contain additional
    values. SLA authority is deliberately narrower: adding another family to
    this protocol also requires the matching model constraint, Alembic
    migration, precedence tests, and commercial approval of its terms.
    """

    unlimited = "unlimited"
    dedicated = "dedicated"
    home_flex = "home_flex"


class SlaPolicySource(StrEnum):
    """Approved policy precedence, highest first (design §4)."""

    subscription_contract = "subscription_contract"
    account_contract = "account_contract"
    offer_version = "offer_version"
    # A commercial family default (unlimited / dedicated / home_flex). Sits
    # below offer_version so a plan that negotiates its own terms still wins,
    # and above internal_measurement so a family default is a real promise
    # rather than a measurement statement.
    plan_family = "plan_family"
    internal_measurement = "internal_measurement"


class MaintenanceState(StrEnum):
    """network.maintenance_lifecycle vocabulary (design §5)."""

    draft = "draft"
    approved = "approved"
    announced = "announced"
    in_progress = "in_progress"
    completed = "completed"
    canceled = "canceled"
    overrun = "overrun"


class IncidentRelation(StrEnum):
    """Linked-incident semantics that prevent double accrual (design §3)."""

    duplicate_of = "duplicate_of"
    merged_into = "merged_into"
    split_from = "split_from"
    superseded_by = "superseded_by"


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(None) and value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be stored as UTC instants")


@dataclass(frozen=True, slots=True)
class ImpactEvidence:
    """One typed evidence item behind an impact decision.

    ``owner`` names the source-of-truth service or reviewed record that
    asserted the fact; customer complaints are not constructible as accrual
    evidence by design.
    """

    kind: ImpactEvidenceKind
    owner: str
    observed_at: datetime
    reference: str
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.observed_at, "observed_at")
        if not self.owner:
            raise ValueError("evidence must name its owning source of truth")
        if not self.reference:
            raise ValueError("evidence must carry a stable reference")


@dataclass(frozen=True, slots=True)
class ScopeRevision:
    """One immutable incident scope/root revision (design §3)."""

    incident_id: UUID
    sequence: int
    effective_at: datetime
    old_scope_type: str | None
    old_scope_id: UUID | None
    new_scope_type: str
    new_scope_id: UUID
    reason: str
    membership_token: str
    entering_subscription_count: int
    leaving_subscription_count: int
    evidence: tuple[ImpactEvidence, ...] = ()
    actor: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.effective_at, "effective_at")
        if self.sequence < 1:
            raise ValueError("scope revisions use a monotonic sequence from 1")
        if not self.membership_token:
            raise ValueError("every scope revision carries a membership token")
        if not self.reason:
            raise ValueError("every scope revision records its reason")
        if min(self.entering_subscription_count, self.leaving_subscription_count) < 0:
            raise ValueError("audience deltas cannot be negative")


@dataclass(frozen=True, slots=True)
class ImpactInterval:
    """One immutable per-subscription service-impact interval (design §2, §7).

    ``ended_at is None`` means the interval is open. ``resolved_at`` never
    appears here — workflow closure does not determine customer downtime.
    """

    incident_id: UUID
    subscription_id: UUID
    state: ImpactState
    quality: IntervalQuality
    started_at: datetime
    scope_revision_sequence: int
    idempotency_key: str
    ended_at: datetime | None = None
    first_evidence: ImpactEvidence | None = None
    recovery_evidence: ImpactEvidence | None = None
    exclusion_candidate: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.started_at, "started_at")
        if self.ended_at is not None:
            _require_utc(self.ended_at, "ended_at")
            if self.ended_at < self.started_at:
                raise ValueError("an interval cannot end before it starts")
        if self.state not in ACCRUING_IMPACT_STATES and (
            self.state is not ImpactState.unknown
        ):
            raise ValueError(
                "intervals record accruing states or explicit unknown periods; "
                f"{self.state} is a point-in-time word, not an interval"
            )
        if self.state is ImpactState.confirmed_unavailable and (
            self.quality is IntervalQuality.exact and self.first_evidence is None
        ):
            raise ValueError(
                "an exact confirmed-unavailable interval requires its first "
                "qualifying evidence"
            )
        if not self.idempotency_key:
            raise ValueError("intervals carry an idempotency key")
        if self.scope_revision_sequence < 1:
            raise ValueError("intervals bind to a scope revision sequence")

    @property
    def duration(self) -> timedelta | None:
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at


@dataclass(frozen=True, slots=True)
class SlaPolicyVersion:
    """One immutable effective-dated SLA policy version (design §4)."""

    policy_id: UUID
    version: int
    source: SlaPolicySource
    effective_from: datetime
    effective_to: datetime | None
    availability_target_percent: float | None
    calendar_timezone: str = SLA_CALENDAR_TIMEZONE
    maintenance_excludable: bool = True
    credit_percent_per_breach: float | None = None
    credit_cap_percent: float | None = None

    def __post_init__(self) -> None:
        _require_utc(self.effective_from, "effective_from")
        if self.effective_to is not None:
            _require_utc(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValueError("a policy version cannot end before it starts")
        if self.version < 1:
            raise ValueError("policy versions are numbered from 1")
        if self.availability_target_percent is not None and not (
            0.0 < self.availability_target_percent <= 100.0
        ):
            raise ValueError("availability target must be within (0, 100]")
        if (
            self.source is not SlaPolicySource.internal_measurement
            and self.availability_target_percent is None
        ):
            raise ValueError(
                "contractual policy versions must state their availability target"
            )


@dataclass(frozen=True, slots=True)
class SlaPolicySegmentScore:
    """One policy-homogeneous part of a period with its own evidence bounds."""

    start: datetime
    end: datetime
    eligible_seconds: int
    unavailable_seconds: int
    excluded_seconds: int
    unknown_seconds: int
    verdict: SlaVerdict
    policy: SlaPolicyVersion | None

    def __post_init__(self) -> None:
        _require_utc(self.start, "start")
        _require_utc(self.end, "end")
        if self.end <= self.start:
            raise ValueError("an SLA policy segment must cover positive time")
        _validate_score_seconds(
            eligible_seconds=self.eligible_seconds,
            unavailable_seconds=self.unavailable_seconds,
            excluded_seconds=self.excluded_seconds,
            unknown_seconds=self.unknown_seconds,
        )
        if self.policy is None and self.verdict not in (
            SlaVerdict.no_contractual_sla,
            SlaVerdict.unavailable,
        ):
            raise ValueError("a targeted segment verdict requires a policy")

    @property
    def availability_lower_bound_percent(self) -> float | None:
        return _availability_bound(
            eligible_seconds=self.eligible_seconds,
            unavailable_seconds=self.unavailable_seconds,
            excluded_seconds=self.excluded_seconds,
            unknown_seconds=self.unknown_seconds,
            unknown_is_uptime=False,
        )

    @property
    def availability_upper_bound_percent(self) -> float | None:
        return _availability_bound(
            eligible_seconds=self.eligible_seconds,
            unavailable_seconds=self.unavailable_seconds,
            excluded_seconds=self.excluded_seconds,
            unknown_seconds=self.unknown_seconds,
            unknown_is_uptime=True,
        )


def _validate_score_seconds(
    *,
    eligible_seconds: int,
    unavailable_seconds: int,
    excluded_seconds: int,
    unknown_seconds: int,
) -> None:
    values = {
        "eligible_seconds": eligible_seconds,
        "unavailable_seconds": unavailable_seconds,
        "excluded_seconds": excluded_seconds,
        "unknown_seconds": unknown_seconds,
    }
    for name, value in values.items():
        if value < 0:
            raise ValueError(f"{name} cannot be negative")
    if unavailable_seconds + excluded_seconds + unknown_seconds > eligible_seconds:
        raise ValueError(
            "unavailable, excluded, and unknown seconds cannot exceed eligible seconds"
        )


def _availability_bound(
    *,
    eligible_seconds: int,
    unavailable_seconds: int,
    excluded_seconds: int,
    unknown_seconds: int,
    unknown_is_uptime: bool,
) -> float | None:
    denominator = eligible_seconds - excluded_seconds
    if denominator <= 0:
        return None
    unavailable = unavailable_seconds + (0 if unknown_is_uptime else unknown_seconds)
    return round(100.0 * max(denominator - unavailable, 0) / denominator, 4)


@dataclass(frozen=True, slots=True)
class SlaScore:
    """One per-subscription, per-period SLA result (design §4).

    Unknown seconds or incomplete lifecycle/entitlement lineage make the score
    provisional.  A provisional result exposes a lower and upper bound but no
    guessed measured percentage and can never be labelled passing or at-risk.
    """

    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    eligible_seconds: int
    unavailable_seconds: int
    excluded_seconds: int
    unknown_seconds: int
    verdict: SlaVerdict
    policy: SlaPolicyVersion | None
    evidence_digest: str
    interval_ids: tuple[str, ...] = ()
    policy_segments: tuple[SlaPolicySegmentScore, ...] = ()
    evidence_complete: bool = True
    completeness_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_utc(self.period_start, "period_start")
        _require_utc(self.period_end, "period_end")
        if self.period_end <= self.period_start:
            raise ValueError("a reporting period cannot end before it starts")
        _validate_score_seconds(
            eligible_seconds=self.eligible_seconds,
            unavailable_seconds=self.unavailable_seconds,
            excluded_seconds=self.excluded_seconds,
            unknown_seconds=self.unknown_seconds,
        )
        if (
            self.policy is None
            and not any(segment.policy is not None for segment in self.policy_segments)
            and self.verdict
            not in (
                SlaVerdict.no_contractual_sla,
                SlaVerdict.unavailable,
            )
        ):
            raise ValueError(
                "a verdict against a target requires an effective policy version"
            )
        if not self.evidence_digest:
            raise ValueError("scores bind to an evidence digest")
        if not self.evidence_complete and self.verdict in (
            SlaVerdict.passing,
            SlaVerdict.at_risk,
        ):
            raise ValueError("incomplete evidence cannot produce a passing verdict")
        if self.evidence_complete and self.completeness_issues:
            raise ValueError("a complete score cannot carry completeness issues")

    @property
    def measured_availability_percent(self) -> float | None:
        """One final percentage only when evidence is complete."""

        if not self.evidence_complete or self.unknown_seconds > 0:
            return None
        return self.availability_lower_bound_percent

    @property
    def availability_lower_bound_percent(self) -> float | None:
        return _availability_bound(
            eligible_seconds=self.eligible_seconds,
            unavailable_seconds=self.unavailable_seconds,
            excluded_seconds=self.excluded_seconds,
            unknown_seconds=self.unknown_seconds,
            unknown_is_uptime=False,
        )

    @property
    def availability_upper_bound_percent(self) -> float | None:
        return _availability_bound(
            eligible_seconds=self.eligible_seconds,
            unavailable_seconds=self.unavailable_seconds,
            excluded_seconds=self.excluded_seconds,
            unknown_seconds=self.unknown_seconds,
            unknown_is_uptime=True,
        )

    @property
    def is_provisional(self) -> bool:
        return not self.evidence_complete or self.unknown_seconds > 0
