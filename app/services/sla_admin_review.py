"""Admin-only shadow comparison for ``customer.service_level``.

This module is an internal implementation shard of the
``customer.service_level`` owner.  It compares one immutable period-score
revision with the legacy availability evidence due to be retired over the
same closed calendar month.  It never writes, approves a cutover, exposes a
customer contract, or guesses why two methods differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.models.sla import SlaPeriodScoreRevision
from app.services.domain_errors import DomainError
from app.services.service_impact_contracts import SLA_CALENDAR_TIMEZONE, SlaVerdict

_DISPLAY_AUTHORITY_KEY = "sla_admin_display_authority"
_THREE_PLACES = Decimal("0.001")
_FOUR_PLACES = Decimal("0.0001")


class SlaAdminDisplayAuthority(StrEnum):
    """Which owner may supply the operational admin SLA figure."""

    legacy_availability = "legacy_availability"
    customer_service_level = "customer_service_level"


class SlaDiscrepancyKind(StrEnum):
    """Evidence-based comparison result; causes are never inferred."""

    missing_candidate_score = "missing_candidate_score"
    candidate_incomplete = "candidate_incomplete"
    candidate_unavailable = "candidate_unavailable"
    legacy_unavailable = "legacy_unavailable"
    exact_match = "exact_match"
    unreviewed_difference = "unreviewed_difference"


class SlaAdminReviewError(DomainError):
    """The admin review query could not produce trustworthy evidence."""


@dataclass(frozen=True, slots=True)
class SlaAdminDisplayDecision:
    """Effective display owner plus cutover provenance."""

    authority: SlaAdminDisplayAuthority
    source: str
    candidate_armed: bool


@dataclass(frozen=True, slots=True)
class SlaAdminReviewQuery:
    """Exact subscription and closed reporting period to compare."""

    subscriber_id: UUID
    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class SlaCandidateScoreEvidence:
    """Latest immutable candidate score for one exact period."""

    score_revision_id: UUID
    revision: int
    evaluated_at: datetime
    verdict: SlaVerdict
    evidence_complete: bool
    completeness_issues: tuple[str, ...]
    availability_lower_bound_percent: Decimal | None
    availability_upper_bound_percent: Decimal | None
    measured_availability_percent: Decimal | None
    eligible_seconds: int
    unavailable_seconds: int
    excluded_seconds: int
    unknown_seconds: int
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class SlaLegacyAvailabilityEvidence:
    """Legacy read-time evidence evaluated over the same exact period."""

    has_coverage: bool
    availability_percent: Decimal | None
    downtime_seconds: int
    observed_days: int
    expected_days: int
    path_gap: str | None


@dataclass(frozen=True, slots=True)
class SlaAdminReview:
    """Typed read model for the restricted admin comparison surface."""

    subscriber_id: UUID
    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    display: SlaAdminDisplayDecision
    candidate: SlaCandidateScoreEvidence | None
    legacy: SlaLegacyAvailabilityEvidence
    discrepancy: SlaDiscrepancyKind
    discrepancy_label: str
    delta_percent: Decimal | None
    summary: str
    cutover_blockers: tuple[str, ...]

    @property
    def ready_for_cutover(self) -> bool:
        return not self.cutover_blockers


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SlaAdminReviewError(
            code="customer.service_level.invalid_review_period",
            message="SLA review instants must include a timezone.",
        )
    return value.astimezone(UTC)


def _persisted_utc(value: datetime) -> datetime:
    """Normalise an ORM timestamp whose database type is UTC-aware.

    PostgreSQL returns ``TIMESTAMPTZ`` values with timezone information, while
    SQLite's explicitly non-authoritative unit-test adapter drops it.  The
    persisted column contract is UTC, so a naïve value at this boundary is UTC;
    typed caller inputs remain strict in :func:`_utc`.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_period(query: SlaAdminReviewQuery) -> tuple[datetime, datetime, datetime]:
    start = _utc(query.period_start)
    end = _utc(query.period_end)
    evaluated_at = _utc(query.evaluated_at)
    if end <= start:
        raise SlaAdminReviewError(
            code="customer.service_level.invalid_review_period",
            message="SLA review period end must follow its start.",
        )
    if end > evaluated_at:
        raise SlaAdminReviewError(
            code="customer.service_level.review_period_not_closed",
            message="SLA discrepancy review is limited to closed periods.",
        )

    zone = ZoneInfo(SLA_CALENDAR_TIMEZONE)
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    next_month_year = local_start.year + (1 if local_start.month == 12 else 0)
    next_month = 1 if local_start.month == 12 else local_start.month + 1
    expected_end = datetime(next_month_year, next_month, 1, tzinfo=zone)
    if (
        local_start.day != 1
        or local_start.hour != 0
        or local_start.minute != 0
        or local_start.second != 0
        or local_start.microsecond != 0
        or local_end != expected_end
    ):
        raise SlaAdminReviewError(
            code="customer.service_level.invalid_review_period",
            message=(
                "SLA discrepancy review requires one complete Africa/Lagos "
                "calendar month."
            ),
        )
    return start, end, evaluated_at


def resolve_admin_display_authority(db: Session) -> SlaAdminDisplayDecision:
    """Resolve the fail-closed admin display selector.

    The registered control currently permits only ``legacy_availability``.
    Even if a future settings edit accidentally returns the candidate value,
    this PR refuses it until the reviewed activation change removes the guard.
    """

    from app.services import settings_spec

    raw = settings_spec.resolve_string(
        db, SettingDomain.subscriber, _DISPLAY_AUTHORITY_KEY
    )
    try:
        authority = SlaAdminDisplayAuthority(raw)
    except ValueError as exc:
        raise SlaAdminReviewError(
            code="customer.service_level.invalid_display_authority",
            message="The configured SLA admin display authority is invalid.",
            details={"configured_value": raw},
        ) from exc
    if authority is not SlaAdminDisplayAuthority.legacy_availability:
        raise SlaAdminReviewError(
            code="customer.service_level.candidate_display_not_armed",
            message=(
                "The customer.service_level candidate is not armed for the "
                "operational admin display."
            ),
            details={"configured_value": authority.value},
        )
    return SlaAdminDisplayDecision(
        authority=authority,
        source="control.settings_spec:subscriber.sla_admin_display_authority",
        candidate_armed=False,
    )


def _candidate(row: SlaPeriodScoreRevision | None) -> SlaCandidateScoreEvidence | None:
    if row is None:
        return None
    lower = (
        Decimal(row.availability_lower_bound_percent)
        if row.availability_lower_bound_percent is not None
        else None
    )
    upper = (
        Decimal(row.availability_upper_bound_percent)
        if row.availability_upper_bound_percent is not None
        else None
    )
    measured = lower if row.evidence_complete and lower == upper else None
    return SlaCandidateScoreEvidence(
        score_revision_id=row.id,
        revision=row.revision,
        evaluated_at=_persisted_utc(row.evaluated_at),
        verdict=SlaVerdict(row.verdict),
        evidence_complete=row.evidence_complete,
        completeness_issues=tuple(row.completeness_issues or ()),
        availability_lower_bound_percent=lower,
        availability_upper_bound_percent=upper,
        measured_availability_percent=measured,
        eligible_seconds=row.eligible_seconds,
        unavailable_seconds=row.unavailable_seconds,
        excluded_seconds=row.excluded_seconds,
        unknown_seconds=row.unknown_seconds,
        evidence_digest=row.evidence_digest,
    )


def _classify(
    candidate: SlaCandidateScoreEvidence | None,
    legacy: SlaLegacyAvailabilityEvidence,
) -> tuple[SlaDiscrepancyKind, str, Decimal | None, str, tuple[str, ...]]:
    blockers: list[str] = ["candidate_display_not_armed"]
    if candidate is None:
        return (
            SlaDiscrepancyKind.missing_candidate_score,
            "Missing candidate score",
            None,
            "No immutable candidate score has been recorded for this period.",
            (*blockers, "missing_candidate_score"),
        )
    if not candidate.evidence_complete:
        return (
            SlaDiscrepancyKind.candidate_incomplete,
            "Candidate evidence incomplete",
            None,
            "Candidate evidence is incomplete; its bounds are not a final score.",
            (*blockers, "candidate_evidence_incomplete"),
        )
    if candidate.measured_availability_percent is None:
        return (
            SlaDiscrepancyKind.candidate_unavailable,
            "Candidate score unavailable",
            None,
            "The complete candidate has no scoreable eligible time.",
            (*blockers, "candidate_score_unavailable"),
        )
    if not legacy.has_coverage or legacy.availability_percent is None:
        return (
            SlaDiscrepancyKind.legacy_unavailable,
            "Legacy evidence unavailable",
            None,
            "Legacy infrastructure evidence is unavailable for this period.",
            (*blockers, "legacy_evidence_unavailable"),
        )

    raw_delta = candidate.measured_availability_percent - legacy.availability_percent
    delta = raw_delta.quantize(_FOUR_PLACES)
    if raw_delta == 0:
        return (
            SlaDiscrepancyKind.exact_match,
            "Exact match",
            delta,
            "Candidate and legacy availability are numerically identical.",
            tuple(blockers),
        )
    return (
        SlaDiscrepancyKind.unreviewed_difference,
        "Unreviewed difference",
        delta,
        "The methods differ; no cause or tolerance is inferred automatically.",
        (*blockers, "unreviewed_difference"),
    )


def review_admin_period(db: Session, query: SlaAdminReviewQuery) -> SlaAdminReview:
    """Compare one latest immutable candidate with exact-period legacy evidence."""

    start, end, _evaluated_at = _validate_period(query)
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.id == query.subscription_id,
            Subscription.subscriber_id == query.subscriber_id,
        )
        .one_or_none()
    )
    if subscription is None:
        raise SlaAdminReviewError(
            code="customer.service_level.unknown_review_subscription",
            message="No subscription exists in the requested customer scope.",
            details={
                "subscriber_id": str(query.subscriber_id),
                "subscription_id": str(query.subscription_id),
            },
        )

    row = (
        db.query(SlaPeriodScoreRevision)
        .filter(
            SlaPeriodScoreRevision.subscription_id == query.subscription_id,
            SlaPeriodScoreRevision.period_start == start,
            SlaPeriodScoreRevision.period_end == end,
        )
        .order_by(SlaPeriodScoreRevision.revision.desc())
        .first()
    )
    candidate = _candidate(row)

    period_seconds = int((end - start).total_seconds())
    day_seconds = 24 * 60 * 60
    if period_seconds % day_seconds:
        raise SlaAdminReviewError(
            code="customer.service_level.invalid_review_period",
            message="The legacy comparison period must contain whole days.",
        )
    from app.services.topology.customer_availability import customer_availability

    report = customer_availability(
        db, subscription, days=period_seconds // day_seconds, now=end
    )
    legacy_percent = (
        Decimal(str(report.effective_uptime_percent)).quantize(_THREE_PLACES)
        if report.has_infrastructure_coverage
        else None
    )
    legacy = SlaLegacyAvailabilityEvidence(
        has_coverage=report.has_infrastructure_coverage,
        availability_percent=legacy_percent,
        downtime_seconds=report.effective_downtime_seconds,
        observed_days=report.infrastructure_observed_days,
        expected_days=report.period_days,
        path_gap=report.path_gap,
    )
    discrepancy, discrepancy_label, delta, summary, blockers = _classify(
        candidate, legacy
    )
    return SlaAdminReview(
        subscriber_id=query.subscriber_id,
        subscription_id=query.subscription_id,
        period_start=start,
        period_end=end,
        display=resolve_admin_display_authority(db),
        candidate=candidate,
        legacy=legacy,
        discrepancy=discrepancy,
        discrepancy_label=discrepancy_label,
        delta_percent=delta,
        summary=summary,
        cutover_blockers=blockers,
    )


__all__ = (
    "SlaAdminDisplayAuthority",
    "SlaAdminDisplayDecision",
    "SlaAdminReview",
    "SlaAdminReviewError",
    "SlaAdminReviewQuery",
    "SlaCandidateScoreEvidence",
    "SlaDiscrepancyKind",
    "SlaLegacyAvailabilityEvidence",
    "resolve_admin_display_authority",
    "review_admin_period",
)
