"""Typed read contract over subscription lifecycle transition evidence.

Consumers that need *history* — notably ``customer.service_level``, which
derives SLA eligibility from "active lifecycle ∩ proven entitlement" — read it
through here rather than querying ``subscription_lifecycle_events`` directly.
Two reasons, both learned the hard way elsewhere in this codebase:

- an arbitrary ORM query cannot tell a caller that the rows it just read are
  not trustworthy. The grade travels with the data here, so a consumer must
  handle it to use it;
- the shape a consumer needs (contiguous active spans) is not the shape the
  table stores (point transitions), and every consumer deriving that
  conversion independently is how two surfaces come to disagree about when a
  customer was actually a customer.

This module answers only what the evidence supports. Where history is missing
or ungraded it says so; it never fills the gap from current status,
``created_at``, ``updated_at`` or billing anchors, because a reconstructed
timeline looks authoritative while resting on nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import SubscriptionStatus
from app.models.lifecycle import SubscriptionLifecycleEvent

#: Statuses during which a subscription is receiving the service it is owed.
#: Anything else — pending, suspended, cancelled — is outside entitlement and
#: therefore outside any SLA denominator.
_ACTIVE_STATUSES = frozenset({SubscriptionStatus.active})


class EvidenceGrade(StrEnum):
    """How far a transition row can be trusted."""

    transition_evidence = "transition_evidence"
    unsupported_pre_cutover = "unsupported_pre_cutover"


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    """One immutable observed transition."""

    subscription_id: UUID
    at: datetime
    from_status: SubscriptionStatus | None
    to_status: SubscriptionStatus | None
    grade: EvidenceGrade
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveWindow:
    """A stretch during which the subscription was observed active.

    ``end is None`` means still active as of the latest evidence — not
    "active forever". Callers clamp it to their own reporting period.
    """

    start: datetime
    end: datetime | None


@dataclass(frozen=True, slots=True)
class LifecycleHistory:
    """Everything a consumer needs, including what is NOT known.

    ``complete`` is the load-bearing field: false means some or all of this
    history predates the append-only cutover and was mutable, so a contractual
    calculation resting on it must report itself incomplete rather than
    presenting a confident number.
    """

    subscription_id: UUID
    transitions: tuple[LifecycleTransition, ...]
    active_windows: tuple[ActiveWindow, ...]
    complete: bool
    unsupported_transitions: int
    earliest_supported_at: datetime | None

    @property
    def has_evidence(self) -> bool:
        return bool(self.transitions)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def _grade(raw: str | None) -> EvidenceGrade:
    try:
        return EvidenceGrade(raw or "")
    except ValueError:
        # An unrecognised grade is treated as unsupported rather than assumed
        # good: the failure mode of guessing wrong is a confident wrong number.
        return EvidenceGrade.unsupported_pre_cutover


def transition_history(db: Session, subscription_id: UUID) -> LifecycleHistory:
    """Ordered transition evidence for one subscription, with its grade."""

    rows = (
        db.query(SubscriptionLifecycleEvent)
        .filter(SubscriptionLifecycleEvent.subscription_id == subscription_id)
        .order_by(
            SubscriptionLifecycleEvent.created_at,
            SubscriptionLifecycleEvent.id,
        )
        .all()
    )

    transitions: list[LifecycleTransition] = []
    for row in rows:
        at = _utc(row.created_at)
        if at is None:
            continue
        transitions.append(
            LifecycleTransition(
                subscription_id=subscription_id,
                at=at,
                from_status=row.from_status,
                to_status=row.to_status,
                grade=_grade(getattr(row, "evidence_grade", None)),
                actor=row.actor,
            )
        )

    unsupported = sum(
        1 for t in transitions if t.grade is EvidenceGrade.unsupported_pre_cutover
    )
    supported = [t for t in transitions if t.grade is EvidenceGrade.transition_evidence]
    return LifecycleHistory(
        subscription_id=subscription_id,
        transitions=tuple(transitions),
        active_windows=_active_windows(transitions),
        # No evidence at all is also incomplete: absence of transitions is not
        # proof the subscription was never active.
        complete=bool(transitions) and unsupported == 0,
        unsupported_transitions=unsupported,
        earliest_supported_at=supported[0].at if supported else None,
    )


def _active_windows(
    transitions: tuple[LifecycleTransition, ...] | list[LifecycleTransition],
) -> tuple[ActiveWindow, ...]:
    """Fold point transitions into contiguous active stretches.

    A transition INTO an active status opens a window; a transition OUT of one
    closes it. Repeated transitions into active do not open a second window —
    that would double-count the same entitlement.
    """

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
