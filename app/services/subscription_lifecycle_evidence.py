"""Immutable evidence written atomically with subscription lifecycle state.

``access.subscription_lifecycle`` owns the status transition. This module is
its contracted, flush-only participant and the sole writer of contractual
transition history. Event handlers, generic CRUD and repair tools may observe
or request a prospective baseline; they never promote their own rows to
trusted evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


class LifecycleEvidenceGrade(StrEnum):
    """How far one immutable row may support contractual calculations."""

    transition_evidence = "transition_evidence"
    state_baseline = "state_baseline"
    unsupported_pre_cutover = "unsupported_pre_cutover"
    unsupported_observation = "unsupported_observation"


class LifecycleEvidenceSource(StrEnum):
    """Closed admission vocabulary for lifecycle evidence provenance."""

    lifecycle_command = "lifecycle_command"
    subscription_creation = "subscription_creation"
    cutover_baseline = "cutover_baseline"
    reconciliation_baseline = "reconciliation_baseline"
    untrusted_observation = "untrusted_observation"
    legacy_unattributed = "legacy_unattributed"


_TRUSTED_SOURCES = frozenset(
    {
        LifecycleEvidenceSource.lifecycle_command,
        LifecycleEvidenceSource.subscription_creation,
        LifecycleEvidenceSource.cutover_baseline,
        LifecycleEvidenceSource.reconciliation_baseline,
    }
)
_TRUSTED_GRADES = frozenset(
    {
        LifecycleEvidenceGrade.transition_evidence,
        LifecycleEvidenceGrade.state_baseline,
    }
)


class LifecycleEvidenceError(DomainError):
    """Stable fail-closed error from the lifecycle evidence participant."""


@dataclass(frozen=True, slots=True)
class RecordLifecycleEvidenceCommand:
    subscription_id: UUID
    from_status: SubscriptionStatus | None
    to_status: SubscriptionStatus
    effective_at: datetime
    evidence_source: LifecycleEvidenceSource
    evidence_grade: LifecycleEvidenceGrade
    context: CommandContext
    reason: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleEvidenceOutcome:
    evidence_id: UUID
    subscription_id: UUID
    effective_at: datetime
    source_id: str
    fingerprint: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class LifecycleEvidenceRetention:
    """Typed retention answer consumed by the catalog deletion boundary."""

    subscription_id: UUID
    retained_evidence_id: UUID | None

    @property
    def blocks_deletion(self) -> bool:
        return self.retained_evidence_id is not None


def _error(code: str, message: str, **details: object) -> LifecycleEvidenceError:
    return LifecycleEvidenceError(
        code=f"access.subscription_lifecycle_evidence.{code}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise _error(
            "naive_effective_at",
            "Lifecycle evidence effective_at must include a timezone.",
        )
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize a persisted instant; SQLite drops timezone metadata in tests."""

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def _source_id(command: RecordLifecycleEvidenceCommand) -> str:
    key = command.context.idempotency_key
    if key:
        material = f"{command.subscription_id}|{command.evidence_source.value}|{key}"
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f"idempotency:{digest}"
    return f"command:{command.context.command_id}"


def _fingerprint(
    command: RecordLifecycleEvidenceCommand,
    *,
    effective_at: datetime,
    source_id: str,
) -> str:
    material = "\0".join(
        (
            str(command.subscription_id),
            command.from_status.value if command.from_status is not None else "",
            command.to_status.value,
            effective_at.isoformat(),
            command.evidence_source.value,
            command.evidence_grade.value,
            source_id,
            command.reason or "",
        )
    )
    return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"


def _event_type(command: RecordLifecycleEvidenceCommand) -> LifecycleEventType:
    if command.to_status is SubscriptionStatus.active:
        if command.from_status in {
            SubscriptionStatus.suspended,
            SubscriptionStatus.disabled,
        }:
            return LifecycleEventType.resume
        return LifecycleEventType.activate
    if command.to_status is SubscriptionStatus.suspended:
        return LifecycleEventType.suspend
    if command.to_status is SubscriptionStatus.canceled:
        return LifecycleEventType.cancel
    return LifecycleEventType.other


def evidence_is_trusted(
    *,
    grade: LifecycleEvidenceGrade,
    source: LifecycleEvidenceSource,
    effective_at: datetime | None,
    recorded_at: datetime | None,
    source_id: str | None,
    fingerprint: str | None,
) -> bool:
    """One admission rule shared by readers and focused tests."""

    return (
        grade in _TRUSTED_GRADES
        and source in _TRUSTED_SOURCES
        and effective_at is not None
        and recorded_at is not None
        and bool(source_id)
        and bool(fingerprint)
    )


def lifecycle_evidence_retention(
    db: Session, *, subscription_id: UUID
) -> LifecycleEvidenceRetention:
    """Resolve whether immutable history retains one subscription identity."""

    retained_evidence_id = (
        db.query(SubscriptionLifecycleEvent.id)
        .filter(SubscriptionLifecycleEvent.subscription_id == subscription_id)
        .order_by(SubscriptionLifecycleEvent.created_at, SubscriptionLifecycleEvent.id)
        .limit(1)
        .scalar()
    )
    return LifecycleEvidenceRetention(
        subscription_id=subscription_id,
        retained_evidence_id=retained_evidence_id,
    )


def _outcome(
    row: SubscriptionLifecycleEvent, *, replayed: bool
) -> LifecycleEvidenceOutcome:
    if (
        row.effective_at is None
        or row.source_id is None
        or row.evidence_fingerprint is None
    ):
        raise _error(
            "incomplete_replay_state",
            "Stored lifecycle evidence is missing replay identity.",
            evidence_id=str(row.id),
        )
    return LifecycleEvidenceOutcome(
        evidence_id=row.id,
        subscription_id=row.subscription_id,
        effective_at=_stored_utc(row.effective_at),
        source_id=row.source_id,
        fingerprint=row.evidence_fingerprint,
        replayed=replayed,
    )


def record_lifecycle_evidence(
    db: Session, command: RecordLifecycleEvidenceCommand
) -> LifecycleEvidenceOutcome:
    """Append trusted transition/baseline evidence and flush; never commit.

    The parent lifecycle command owns the transaction and locks the
    subscription. The unique ``(evidence_source, source_id)`` constraint is the
    final concurrent-replay arbiter. A raw collision is retryable only by
    retrying the complete parent owner command, which then re-reads the winner.
    """

    if command.evidence_source not in _TRUSTED_SOURCES:
        raise _error(
            "untrusted_source",
            "Only an admitted lifecycle owner source may write trusted evidence.",
            evidence_source=command.evidence_source.value,
        )
    if command.evidence_grade not in _TRUSTED_GRADES:
        raise _error(
            "untrusted_grade",
            "The trusted writer requires a transition or state-baseline grade.",
            evidence_grade=command.evidence_grade.value,
        )
    if command.from_status is command.to_status:
        raise _error(
            "no_state_change",
            "Transition evidence cannot repeat the same lifecycle state.",
        )
    if (
        command.evidence_grade is LifecycleEvidenceGrade.state_baseline
        and command.from_status is not None
    ):
        raise _error(
            "baseline_has_from_status",
            "A state baseline establishes current state and has no from_status.",
        )

    effective_at = _utc(command.effective_at)
    source_id = _source_id(command)
    fingerprint = _fingerprint(
        command,
        effective_at=effective_at,
        source_id=source_id,
    )
    prior = (
        db.query(SubscriptionLifecycleEvent)
        .filter(
            SubscriptionLifecycleEvent.evidence_source == command.evidence_source.value,
            SubscriptionLifecycleEvent.source_id == source_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if prior is not None:
        if prior.evidence_fingerprint != fingerprint:
            raise _error(
                "idempotency_conflict",
                "Lifecycle evidence source identity was reused for different state.",
                source_id=source_id,
            )
        return _outcome(prior, replayed=True)

    subscription = db.get(Subscription, command.subscription_id)
    if subscription is None:
        raise _error(
            "subscription_not_found",
            "Lifecycle evidence requires an existing subscription.",
            subscription_id=str(command.subscription_id),
        )
    if subscription.status is not command.to_status:
        raise _error(
            "status_not_applied",
            "Lifecycle evidence may be appended only with the applied status change.",
            subscription_id=str(command.subscription_id),
            expected_status=command.to_status.value,
            current_status=subscription.status.value,
        )

    recorded_at = datetime.now(UTC)
    row = SubscriptionLifecycleEvent(
        subscription_id=command.subscription_id,
        event_type=_event_type(command),
        from_status=command.from_status,
        to_status=command.to_status,
        reason=command.reason or command.context.reason,
        notes=command.notes,
        metadata_={
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "causation_id": (
                str(command.context.causation_id)
                if command.context.causation_id is not None
                else None
            ),
            "scope": command.context.scope,
        },
        actor=command.context.actor,
        evidence_grade=command.evidence_grade.value,
        evidence_source=command.evidence_source.value,
        source_id=source_id,
        evidence_fingerprint=fingerprint,
        effective_at=effective_at,
        recorded_at=recorded_at,
        created_at=recorded_at,
    )
    db.add(row)
    db.flush()
    return _outcome(row, replayed=False)


def record_current_state_baseline(
    db: Session,
    *,
    subscription: Subscription,
    effective_at: datetime,
    evidence_source: LifecycleEvidenceSource,
    context: CommandContext,
) -> LifecycleEvidenceOutcome:
    """Append a prospective baseline; never claim history before it."""

    if evidence_source not in {
        LifecycleEvidenceSource.cutover_baseline,
        LifecycleEvidenceSource.reconciliation_baseline,
        LifecycleEvidenceSource.subscription_creation,
    }:
        raise _error(
            "invalid_baseline_source",
            "A lifecycle state baseline needs a reviewed baseline source.",
            evidence_source=evidence_source.value,
        )
    return record_lifecycle_evidence(
        db,
        RecordLifecycleEvidenceCommand(
            subscription_id=subscription.id,
            from_status=None,
            to_status=subscription.status,
            effective_at=effective_at,
            evidence_source=evidence_source,
            evidence_grade=LifecycleEvidenceGrade.state_baseline,
            context=context,
            reason=context.reason,
        ),
    )
