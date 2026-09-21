"""``ActionReadiness``: a stateless, transport-neutral readiness/blocker verdict.

Every domain still decides whether one of its gated actions may run. This
module gives every domain the SAME shape to hand that decision to a caller
(API, web, mobile, worker) in: a state, zero or more actionable blockers each
attributed to the domain owner that must clear them, zero or more next
actions, and optional correlation metadata linking the verdict to a running
operation.

This is explicitly **not** a workflow engine. Nothing here decides anything,
persists anything, or executes a repair. It is pure vocabulary: value objects
that validate their own internal consistency in ``__post_init__`` and an
enforced invariant that a blocker's ``owner`` names a real, decision-making
domain service — never this module and never another pure-vocabulary layer.

Structural guarantee: this module imports nothing from ``sqlalchemy``,
``app.models``, or ``app.db``, and never references ``Session``. It cannot
read live state and cannot persist anything — verified by
``tests/architecture/test_action_readiness_ownership.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ReadinessState(StrEnum):
    """The domain owner's verdict for one gated action on one subject."""

    ready = "ready"
    blocked = "blocked"
    waiting = "waiting"
    needs_verification = "needs_verification"
    failed = "failed"
    complete = "complete"


class ReadinessImpact(StrEnum):
    """Whether a finding prevents the action, or is merely informative.

    Deliberately not named "severity" — severity usually means low/medium/high.
    This answers one question only: does this finding block the action.
    """

    blocking = "blocking"
    advisory = "advisory"


_READY_STATES = frozenset({ReadinessState.ready, ReadinessState.complete})
_BLOCKED_STATES = frozenset(
    {
        ReadinessState.blocked,
        ReadinessState.waiting,
        ReadinessState.needs_verification,
        ReadinessState.failed,
    }
)


def _require_relative_url(value: str | None, *, field_name: str) -> None:
    if value is not None and not value.startswith("/"):
        raise ValueError(f"{field_name} must be application-relative: {value!r}")


def _require_tz_aware(value: datetime | None, *, field_name: str) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BlockerEvidence:
    """The concrete fact backing one blocker, for staff review."""

    summary: str
    observed_at: datetime | None = None
    source: str | None = None
    detail_url: str | None = None

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("BlockerEvidence.summary is required")
        _require_tz_aware(self.observed_at, field_name="BlockerEvidence.observed_at")
        _require_relative_url(self.detail_url, field_name="BlockerEvidence.detail_url")


@dataclass(frozen=True, slots=True)
class RepairAction:
    """A repair the OWNING domain service can execute for a blocker.

    ``runner`` is a dotted path to the real domain function that performs the
    repair. It is deliberately never exposed on the transport schema
    (``app/schemas/action_readiness.py``) — a client names the repair by
    ``key``/``action_url``; it never learns, and cannot invoke, the runner
    directly.
    """

    key: str
    label: str
    owner: str
    runner: str
    action_url: str | None = None
    automatic: bool = False

    def __post_init__(self) -> None:
        for name, value in (("key", self.key), ("label", self.label)):
            if not value.strip():
                raise ValueError(f"RepairAction.{name} is required")
        _validate_owner(self.owner, field_name="RepairAction.owner")
        _require_relative_url(self.action_url, field_name="RepairAction.action_url")


@dataclass(frozen=True, slots=True)
class ActionableBlocker:
    """One reason a gated action cannot proceed, attributed to its owner."""

    code: str
    owner: str
    customer_message: str
    staff_detail: str
    evidence: BlockerEvidence
    impact: ReadinessImpact = ReadinessImpact.blocking
    repair: RepairAction | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("code", self.code),
            ("customer_message", self.customer_message),
            ("staff_detail", self.staff_detail),
        ):
            if not value.strip():
                raise ValueError(f"ActionableBlocker.{name} is required")
        _validate_owner(self.owner, field_name="ActionableBlocker.owner")


@dataclass(frozen=True, slots=True)
class NextAction:
    """A follow-on action a caller may offer once/unless a blocker clears."""

    key: str
    label: str
    owner: str
    url: str | None = None
    permission: str | None = None
    clears_blocker_code: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("key", self.key), ("label", self.label)):
            if not value.strip():
                raise ValueError(f"NextAction.{name} is required")
        _validate_owner(self.owner, field_name="NextAction.owner")
        _require_relative_url(self.url, field_name="NextAction.url")


@dataclass(frozen=True, slots=True)
class OperationReference:
    """A pointer to the running operation this readiness verdict describes."""

    kind: str
    id: UUID
    status: str | None = None
    detail_url: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("OperationReference.kind is required")
        _require_relative_url(
            self.detail_url, field_name="OperationReference.detail_url"
        )


@dataclass(frozen=True, slots=True)
class ActionCorrelation:
    """Correlation/causation metadata carried alongside a readiness verdict.

    Field names deliberately byte-match ``app.services.owner_commands
    .CommandContext``'s ``correlation_id``/``causation_id``/``idempotency_key``
    — proven, without importing that module, by
    ``tests/test_action_readiness.py``.
    """

    correlation_id: UUID
    causation_id: UUID | None = None
    idempotency_key: str | None = None
    workflow_key: str | None = None
    operation_reference: OperationReference | None = None


@dataclass(frozen=True, slots=True)
class ActionReadiness:
    """One domain owner's stateless readiness verdict for one gated action."""

    action_key: str
    subject_type: str
    subject_id: str
    owner: str
    state: ReadinessState
    evaluated_at: datetime
    blockers: tuple[ActionableBlocker, ...] = ()
    next_actions: tuple[NextAction, ...] = ()
    correlation: ActionCorrelation | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("action_key", self.action_key),
            ("subject_type", self.subject_type),
            ("subject_id", self.subject_id),
        ):
            if not value.strip():
                raise ValueError(f"ActionReadiness.{name} is required")
        _validate_owner(self.owner, field_name="ActionReadiness.owner")
        _require_tz_aware(self.evaluated_at, field_name="ActionReadiness.evaluated_at")

        codes = [blocker.code for blocker in self.blockers]
        if len(set(codes)) != len(codes):
            raise ValueError("ActionReadiness.blockers must have unique codes")

        blocking = tuple(
            blocker
            for blocker in self.blockers
            if blocker.impact == ReadinessImpact.blocking
        )
        if self.state in _READY_STATES and blocking:
            raise ValueError(
                f"ActionReadiness.state={self.state.value!r} requires zero "
                "blocking blockers"
            )
        if self.state in _BLOCKED_STATES and not blocking:
            raise ValueError(
                f"ActionReadiness.state={self.state.value!r} requires at "
                "least one blocking blocker"
            )

        declared_codes = set(codes)
        for next_action in self.next_actions:
            if (
                next_action.clears_blocker_code is not None
                and next_action.clears_blocker_code not in declared_codes
            ):
                raise ValueError(
                    "NextAction.clears_blocker_code "
                    f"{next_action.clears_blocker_code!r} names no declared "
                    "blocker code"
                )

    @property
    def is_ready(self) -> bool:
        return self.state in _READY_STATES

    @property
    def blocking_blockers(self) -> tuple[ActionableBlocker, ...]:
        return tuple(
            blocker
            for blocker in self.blockers
            if blocker.impact == ReadinessImpact.blocking
        )

    @property
    def advisories(self) -> tuple[ActionableBlocker, ...]:
        return tuple(
            blocker
            for blocker in self.blockers
            if blocker.impact == ReadinessImpact.advisory
        )

    @property
    def primary_blocker(self) -> ActionableBlocker | None:
        blocking = self.blocking_blockers
        return blocking[0] if blocking else None


def _validate_owner(owner: str, *, field_name: str) -> None:
    """Enforce that ``owner`` names a real, decision-making SOT service.

    Lazily imported to mirror the existing pattern in
    ``app.services.owner_commands._validate_manifest`` (avoids an import
    cycle between this pure-vocabulary module and the declarative registry).
    """

    if not owner.strip():
        raise ValueError(f"{field_name} is required")

    from app.services.sot_manifest import TransactionMode
    from app.services.sot_relationships import service_relationship

    try:
        service = service_relationship(owner)
    except KeyError as exc:
        raise ValueError(
            f"{field_name}={owner!r} is not a registered SOT service"
        ) from exc

    contract = service.contract
    if (
        contract is not None
        and contract.transaction.mode == TransactionMode.NOT_APPLICABLE
    ):
        raise ValueError(
            f"{field_name}={owner!r} is a pure-vocabulary contract "
            "(TransactionMode.NOT_APPLICABLE) and cannot own a blocker or "
            "next action — a blocker must name a real decision-making owner"
        )
