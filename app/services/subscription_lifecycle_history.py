"""Period-scoped read contract over immutable subscription lifecycle evidence.

The SLA consumer needs two answers, not one plausible timeline: which spans
are proven active, and whether the whole requested period is covered by
authoritative evidence. Unsupported observations break coverage; they never
become state transitions. A later trusted baseline restores coverage only
prospectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import SubscriptionLifecycleEvent
from app.services.subscription_lifecycle_evidence import (
    LifecycleEvidenceGrade,
    LifecycleEvidenceSource,
    evidence_is_trusted,
)

_ACTIVE_STATUSES = frozenset({SubscriptionStatus.active})


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    """One immutable row with explicit trust and time provenance."""

    evidence_id: UUID
    subscription_id: UUID
    at: datetime
    effective_at: datetime | None
    recorded_at: datetime | None
    from_status: SubscriptionStatus | None
    to_status: SubscriptionStatus | None
    grade: LifecycleEvidenceGrade
    source: LifecycleEvidenceSource
    source_id: str | None
    fingerprint: str | None
    trusted: bool
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveWindow:
    """A proven-active half-open interval."""

    start: datetime
    end: datetime | None


@dataclass(frozen=True, slots=True)
class LifecycleHistory:
    """Diagnostic full history; period scoring uses the period projection."""

    subscription_id: UUID
    transitions: tuple[LifecycleTransition, ...]
    active_windows: tuple[ActiveWindow, ...]
    complete: bool
    unsupported_transitions: int
    earliest_supported_at: datetime | None
    issues: tuple[str, ...]

    @property
    def has_evidence(self) -> bool:
        return bool(self.transitions)


@dataclass(frozen=True, slots=True)
class LifecyclePeriodHistory:
    """Proven lifecycle state for exactly one half-open reporting period."""

    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    active_windows: tuple[ActiveWindow, ...]
    complete: bool
    coverage_start: datetime | None
    supporting_evidence_ids: tuple[UUID, ...]
    issues: tuple[str, ...]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def _grade(raw: str | None) -> LifecycleEvidenceGrade:
    try:
        return LifecycleEvidenceGrade(raw or "")
    except ValueError:
        return LifecycleEvidenceGrade.unsupported_observation


def _source(raw: str | None) -> LifecycleEvidenceSource:
    try:
        return LifecycleEvidenceSource(raw or "")
    except ValueError:
        return LifecycleEvidenceSource.untrusted_observation


def _transition(row: SubscriptionLifecycleEvent) -> LifecycleTransition:
    grade = _grade(getattr(row, "evidence_grade", None))
    source = _source(getattr(row, "evidence_source", None))
    effective_at = _utc(getattr(row, "effective_at", None))
    recorded_at = _utc(getattr(row, "recorded_at", None))
    diagnostic_created_at = _utc(row.created_at)
    at = (
        effective_at
        or recorded_at
        or diagnostic_created_at
        or datetime.min.replace(tzinfo=UTC)
    )
    source_id = getattr(row, "source_id", None)
    fingerprint = getattr(row, "evidence_fingerprint", None)
    return LifecycleTransition(
        evidence_id=row.id,
        subscription_id=row.subscription_id,
        at=at,
        effective_at=effective_at,
        recorded_at=recorded_at,
        from_status=row.from_status,
        to_status=row.to_status,
        grade=grade,
        source=source,
        source_id=source_id,
        fingerprint=fingerprint,
        trusted=evidence_is_trusted(
            grade=grade,
            source=source,
            effective_at=effective_at,
            recorded_at=recorded_at,
            source_id=source_id,
            fingerprint=fingerprint,
        ),
        actor=row.actor,
    )


def _ordered_transitions(
    db: Session, subscription_id: UUID
) -> tuple[LifecycleTransition, ...]:
    rows = (
        db.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == subscription_id)
        .all()
    )
    return tuple(
        sorted(
            (_transition(row) for row in rows),
            key=lambda item: (item.at, str(item.evidence_id)),
        )
    )


def transition_history(db: Session, subscription_id: UUID) -> LifecycleHistory:
    """All rows for diagnostics; active windows use trusted rows only."""

    transitions = _ordered_transitions(db, subscription_id)
    supported = tuple(item for item in transitions if item.trusted)
    issues = _lineage_issues(supported)
    return LifecycleHistory(
        subscription_id=subscription_id,
        transitions=transitions,
        active_windows=_active_windows(supported),
        complete=bool(supported) and not issues,
        unsupported_transitions=sum(not item.trusted for item in transitions),
        earliest_supported_at=supported[0].at if supported else None,
        issues=issues,
    )


def lifecycle_history_for_period(
    db: Session,
    subscription_id: UUID,
    *,
    period_start: datetime,
    period_end: datetime,
) -> LifecyclePeriodHistory:
    """Return proven active spans and honest completeness for ``[start, end)``."""

    start = _utc(period_start)
    end = _utc(period_end)
    if start is None or end is None or end <= start:
        raise ValueError("Lifecycle history requires a positive UTC period")
    return _project_period(
        subscription_id,
        _ordered_transitions(db, subscription_id),
        period_start=start,
        period_end=end,
    )


def _project_period(
    subscription_id: UUID,
    transitions: tuple[LifecycleTransition, ...],
    *,
    period_start: datetime,
    period_end: datetime,
) -> LifecyclePeriodHistory:
    state: SubscriptionStatus | None = None
    coverage_start: datetime | None = None
    issues: list[str] = []
    supporting: list[UUID] = []

    # Establish state at the left edge. A trusted row proves its to_status from
    # that instant. An unsupported observation after it invalidates that proof
    # until the next trusted row/baseline.
    for item in transitions:
        if item.at > period_start:
            break
        if item.trusted and item.to_status is not None:
            state = item.to_status
            coverage_start = item.at
            supporting.append(item.evidence_id)
        elif item.effective_at is not None:
            state = None
            coverage_start = None

    complete = state is not None
    if not complete:
        issues.append("missing_supported_left_edge")

    active_start = period_start if state in _ACTIVE_STATUSES else None
    windows: list[ActiveWindow] = []
    for item in transitions:
        if item.at <= period_start or item.at >= period_end:
            continue

        if active_start is not None and item.at > active_start:
            windows.append(ActiveWindow(start=active_start, end=item.at))
            active_start = None

        if item.trusted and item.to_status is not None:
            if (
                state is not None
                and item.from_status is not None
                and item.from_status is not state
            ):
                complete = False
                issues.append(f"lineage_discontinuity:{item.evidence_id}")
            state = item.to_status
            coverage_start = coverage_start or item.at
            supporting.append(item.evidence_id)
            if state in _ACTIVE_STATUSES:
                active_start = item.at
        elif item.effective_at is not None:
            complete = False
            state = None
            coverage_start = None
            issues.append(f"unsupported_observation:{item.evidence_id}")

    if active_start is not None and period_end > active_start:
        windows.append(ActiveWindow(start=active_start, end=period_end))

    return LifecyclePeriodHistory(
        subscription_id=subscription_id,
        period_start=period_start,
        period_end=period_end,
        active_windows=tuple(windows),
        complete=complete and state is not None,
        coverage_start=coverage_start,
        supporting_evidence_ids=tuple(dict.fromkeys(supporting)),
        issues=tuple(dict.fromkeys(issues)),
    )


def _lineage_issues(
    transitions: tuple[LifecycleTransition, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    state: SubscriptionStatus | None = None
    for item in transitions:
        if (
            state is not None
            and item.from_status is not None
            and item.from_status is not state
        ):
            issues.append(f"lineage_discontinuity:{item.evidence_id}")
        state = item.to_status
    return tuple(issues)


def _active_windows(
    transitions: tuple[LifecycleTransition, ...] | list[LifecycleTransition],
) -> tuple[ActiveWindow, ...]:
    windows: list[ActiveWindow] = []
    open_start: datetime | None = None
    for transition in transitions:
        becomes_active = transition.to_status in _ACTIVE_STATUSES
        if becomes_active and open_start is None:
            open_start = transition.at
        elif not becomes_active and open_start is not None:
            if transition.at > open_start:
                windows.append(ActiveWindow(start=open_start, end=transition.at))
            open_start = None
    if open_start is not None:
        windows.append(ActiveWindow(start=open_start, end=None))
    return tuple(windows)
