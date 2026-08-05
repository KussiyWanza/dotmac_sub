"""Owner of per-ONT automatic-reconciliation eligibility.

Owner: ``network.ont_reconcile_eligibility``.

Why this exists. The only way to stop the sweeper touching a device was the
fleet-wide ``network.ont_reconcile`` control. That is far too blunt: it halts
convergence for every ONT, and because ``_close_expired_remote_access`` and
``_reconcile_dialer_credentials`` run inside ``run_ont_reconcile_sweep`` AFTER
the gate, disabling it silently pauses expired remote-access cleanup and the
dialer reconcile too. Excluding five devices should not cost the other ~1,500
their convergence, nor quietly stop unrelated maintenance.

What a hold is. A reviewed, evidenced decision that ONE ONT is excluded from
ONE reconciliation scope. It is not a feature flag and not a cache: it records
a judgement about a customer device, so the evidence is mandatory --

* ``reason_code`` + ``explanation`` -- the machine code and the human sentence;
* ``actor`` -- who asked;
* ``reviewer`` -- who agreed, and it must NOT be the actor. Suppressing
  convergence on a live service is a two-person decision, and self-review is
  not review;
* ``review_due_at`` -- when a human must look again.

``review_due_at`` IS NOT AN EXPIRY. Nothing here releases a hold on a timer.
An expiring hold would hand a suppressed device back to the sweeper at an
arbitrary moment, which is exactly the surprise a hold exists to prevent. An
overdue hold stays ACTIVE and becomes reportable so it can be escalated; only
``release_reconcile_hold`` ends one.

Scope. ``automatic_sweep`` only, deliberately. An operator doing reviewed
repair must still be able to drive a device explicitly -- otherwise a held ONT
has no legitimate path back to convergence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.network import (
    OntReconcileHold,
    OntReconcileHoldStatus,
    OntReconcileScope,
    OntUnit,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

OWNER = "network.ont_reconcile_eligibility"
_CONCERN = "per-ONT automatic reconciliation eligibility"
OVERDUE_ALERT_PREFIX = "ont-reconcile-hold-overdue:"

PLACE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=_CONCERN, name="place_reconcile_hold"
)
RELEASE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=_CONCERN, name="release_reconcile_hold"
)

_AUDIT_ACTION = "ont_reconcile_hold"


class HoldRefusal(StrEnum):
    """One stable code per distinct refusal."""

    missing_ont = "reconcile_hold_missing_ont"
    missing_reason = "reconcile_hold_missing_reason"
    missing_explanation = "reconcile_hold_missing_explanation"
    missing_reviewer = "reconcile_hold_missing_reviewer"
    reviewer_is_actor = "reconcile_hold_reviewer_is_actor"
    missing_review_due = "reconcile_hold_missing_review_due"
    review_due_in_past = "reconcile_hold_review_due_in_past"
    already_held = "reconcile_hold_already_active"
    not_found = "reconcile_hold_not_found"
    already_released = "reconcile_hold_already_released"
    missing_idempotency_key = "reconcile_hold_missing_idempotency_key"
    idempotency_conflict = "reconcile_hold_idempotency_conflict"
    ont_not_found = "reconcile_hold_ont_not_found"
    concurrent_release = "reconcile_hold_concurrent_release"


class ReconcileHoldError(DomainError):
    """Fail-closed refusal from the eligibility owner."""

    def __init__(
        self, message: str, *, code: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code=code, message=message, details=details)


@dataclass(frozen=True, slots=True)
class HoldSpec:
    """Typed request to suppress automatic reconciliation for one ONT."""

    ont_unit_id: UUID
    reason_code: str
    explanation: str
    reviewer: str
    review_due_at: datetime
    scope: OntReconcileScope = OntReconcileScope.automatic_sweep


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    """Whether one ONT may be touched by one reconciliation scope.

    ``eligible`` is the only field a caller should branch on. The rest exists
    so a refusal is explainable without a second query -- a sweeper that says
    "skipped" without saying why is indistinguishable from one that is broken.
    """

    ont_unit_id: str
    scope: str
    eligible: bool
    hold_id: str = ""
    reason_code: str = ""
    review_due_at: datetime | None = None
    overdue: bool = False

    @property
    def held(self) -> bool:
        return not self.eligible


class OverdueHoldAlertSeverity(StrEnum):
    critical = "critical"


@dataclass(frozen=True, slots=True)
class OverdueHoldAlert:
    """Owner-decided alert consequence for one overdue active hold.

    The transport may choose how to persist and deliver this projection, but
    it does not decide severity, wording, identity, or the operator target.
    Actor/reviewer identities deliberately stay out of the alert payload; the
    authoritative hold remains available on the linked ONT.
    """

    fingerprint: str
    severity: OverdueHoldAlertSeverity
    title: str
    summary: str
    hold_id: str
    ont_unit_id: str
    scope: str
    reason_code: str
    review_due_at: str
    target_url: str


def _require(value: Any, *, code: HoldRefusal, message: str) -> None:
    if value in (None, "") or (isinstance(value, str) and not value.strip()):
        raise ReconcileHoldError(message, code=code.value)


def _validate(spec: HoldSpec, context: CommandContext) -> None:
    _require(
        spec.ont_unit_id,
        code=HoldRefusal.missing_ont,
        message="A reconcile hold requires an ONT.",
    )
    _require(
        spec.reason_code,
        code=HoldRefusal.missing_reason,
        message="A reconcile hold requires a reason code.",
    )
    _require(
        spec.explanation,
        code=HoldRefusal.missing_explanation,
        message="A reconcile hold requires a written explanation.",
    )
    _require(
        spec.reviewer,
        code=HoldRefusal.missing_reviewer,
        message="A reconcile hold requires a reviewer.",
    )
    if spec.reviewer.strip().lower() == (context.actor or "").strip().lower():
        raise ReconcileHoldError(
            "The reviewer must differ from the actor: suppressing convergence "
            "on a customer device is a two-person decision.",
            code=HoldRefusal.reviewer_is_actor.value,
        )
    _require(
        spec.review_due_at,
        code=HoldRefusal.missing_review_due,
        message="A reconcile hold requires a review date.",
    )
    due = spec.review_due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    if due <= datetime.now(UTC):
        raise ReconcileHoldError(
            "review_due_at must be in the future. It is a review date, not an "
            "expiry -- a hold placed already-overdue hides the decision.",
            code=HoldRefusal.review_due_in_past.value,
        )


def _lock_ont(db: Session, ont_unit_id: UUID) -> OntUnit:
    """Serialise on the ONT row. FIRST in the canonical lock order.

    Every decision about a device -- placing a hold, releasing one, or the
    sweeper deciding to touch it -- takes this lock before reading the hold.
    A once-per-pass set read cannot serialise against a placement that lands
    mid-pass: the sweeper would act on a snapshot taken before the hold
    existed. The bulk set survives only as a pre-filter.

    Canonical order: **OntUnit -> active hold**. Nothing may reverse it.
    """
    ont = (
        db.execute(
            select(OntUnit)
            .where(OntUnit.id == ont_unit_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .first()
    )
    if ont is None:
        raise ReconcileHoldError("No such ONT.", code=HoldRefusal.ont_not_found.value)
    return ont


def _active_hold(
    db: Session,
    ont_unit_id: UUID,
    scope: OntReconcileScope,
    *,
    for_update: bool = False,
) -> OntReconcileHold | None:
    statement = (
        select(OntReconcileHold)
        .where(OntReconcileHold.ont_unit_id == ont_unit_id)
        .where(OntReconcileHold.scope == scope)
        .where(OntReconcileHold.status == OntReconcileHoldStatus.active)
    )
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    return db.execute(statement).scalars().first()


def _stage(
    db: Session, *, context: CommandContext, hold: OntReconcileHold, action: str
) -> None:
    stage_audit_event(
        db,
        action=f"{_AUDIT_ACTION}.{action}",
        entity_type="ont_reconcile_hold",
        entity_id=str(hold.id),
        actor_type=AuditActorType.service,
        actor_id=context.actor,
        metadata={
            "ont_unit_id": str(hold.ont_unit_id),
            "scope": hold.scope.value,
            "status": hold.status.value,
            "reason_code": hold.reason_code,
            "reviewer": hold.reviewer,
            "review_due_at": hold.review_due_at.isoformat()
            if hold.review_due_at
            else None,
            "reason": context.reason,
        },
    )


def _replayable(
    hold: OntReconcileHold, spec: HoldSpec, context: CommandContext
) -> bool:
    """Whether an existing hold is the SAME command being retried.

    A replay is only a replay when the whole command matches. Returning a hold
    that was placed for a different ONT, scope, reason or reviewer would let a
    reused key silently substitute one decision for another.
    """
    due = spec.review_due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    stored_due = hold.review_due_at
    if stored_due is not None and stored_due.tzinfo is None:
        stored_due = stored_due.replace(tzinfo=UTC)
    return (
        hold.ont_unit_id == spec.ont_unit_id
        and hold.scope == spec.scope
        and hold.reason_code == spec.reason_code.strip()
        and hold.explanation == spec.explanation.strip()
        and hold.reviewer == spec.reviewer.strip()
        and hold.actor == context.actor
        and stored_due == due
    )


def _place(db: Session, spec: HoldSpec, context: CommandContext) -> OntReconcileHold:
    _validate(spec, context)

    # Idempotency is MANDATORY. Without a key a retried placement either
    # creates a second decision or trips the unique index with an error that
    # looks like a bug rather than a duplicate request.
    if not (context.idempotency_key or "").strip():
        raise ReconcileHoldError(
            "A reconcile hold requires an idempotency key.",
            code=HoldRefusal.missing_idempotency_key.value,
        )

    # Canonical lock order: OntUnit first, then the hold.
    _lock_ont(db, spec.ont_unit_id)

    existing = (
        db.execute(
            select(OntReconcileHold).where(
                OntReconcileHold.idempotency_key == context.idempotency_key
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if _replayable(existing, spec, context):
            return existing
        raise ReconcileHoldError(
            "That idempotency key was used for a different hold command.",
            code=HoldRefusal.idempotency_conflict.value,
        )

    if _active_hold(db, spec.ont_unit_id, spec.scope, for_update=True) is not None:
        raise ReconcileHoldError(
            "This ONT already has an active hold for that scope.",
            code=HoldRefusal.already_held.value,
        )

    due = spec.review_due_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    hold = OntReconcileHold(
        ont_unit_id=spec.ont_unit_id,
        scope=spec.scope,
        status=OntReconcileHoldStatus.active,
        reason_code=spec.reason_code.strip(),
        explanation=spec.explanation.strip(),
        actor=context.actor,
        reviewer=spec.reviewer.strip(),
        idempotency_key=context.idempotency_key,
        review_due_at=due,
    )
    db.add(hold)
    db.flush()
    _stage(db, context=context, hold=hold, action="placed")
    return hold


def _release(db: Session, hold_id: UUID, context: CommandContext) -> OntReconcileHold:
    # Read the hold once WITHOUT a lock purely to learn which ONT to lock, so
    # the canonical OntUnit -> hold order is preserved. Locking the hold first
    # would invert it against placement and deadlock.
    target = db.get(OntReconcileHold, hold_id)
    if target is None:
        raise ReconcileHoldError(
            "No such reconcile hold.", code=HoldRefusal.not_found.value
        )
    # Snapshot the pre-lock status NOW. `target` and the locked row below are
    # the SAME identity-map object, and populate_existing=True overwrites its
    # attributes -- so reading target.status after the refresh would compare
    # the row against itself and make concurrent_release unreachable.
    status_before_lock = target.status
    _lock_ont(db, target.ont_unit_id)

    hold = (
        db.execute(
            select(OntReconcileHold)
            .where(OntReconcileHold.id == hold_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .first()
    )
    if hold is None:  # pragma: no cover - deleted between read and lock
        raise ReconcileHoldError(
            "No such reconcile hold.", code=HoldRefusal.not_found.value
        )
    if hold.status is OntReconcileHoldStatus.released:
        # Re-read under the lock: a concurrent release that won the race is a
        # DIFFERENT fact from releasing an already-old hold, so it gets its own
        # code rather than being flattened into "already released".
        code = (
            HoldRefusal.concurrent_release
            if status_before_lock is OntReconcileHoldStatus.active
            else HoldRefusal.already_released
        )
        raise ReconcileHoldError("That hold is already released.", code=code.value)
    hold.status = OntReconcileHoldStatus.released
    hold.released_at = datetime.now(UTC)
    hold.released_by = context.actor
    hold.release_reason = context.reason
    db.flush()
    _stage(db, context=context, hold=hold, action="released")
    return hold


def place_reconcile_hold(
    db: Session, *, spec: HoldSpec, context: CommandContext
) -> OntReconcileHold:
    """Suppress automatic reconciliation for one ONT, with evidence."""
    return execute_owner_command(
        db,
        definition=PLACE_COMMAND,
        context=context,
        operation=lambda: _place(db, spec, context),
    )


def release_reconcile_hold(
    db: Session, *, hold_id: UUID, context: CommandContext
) -> OntReconcileHold:
    """End a hold. The ONLY way one ends -- there is no automatic expiry."""
    return execute_owner_command(
        db,
        definition=RELEASE_COMMAND,
        context=context,
        operation=lambda: _release(db, hold_id, context),
    )


def reconcile_eligibility(
    db: Session,
    ont_unit_id: Any,
    *,
    scope: OntReconcileScope = OntReconcileScope.automatic_sweep,
) -> EligibilityVerdict:
    """Whether this ONT may be reconciled by this scope right now.

    Checked BEFORE any ping, read or write. A held ONT must not be contacted at
    all: reaching a device to discover it is held would defeat the point.
    """
    if ont_unit_id is None:
        # No identity, no eligibility. Fail closed.
        return EligibilityVerdict(ont_unit_id="", scope=scope.value, eligible=False)

    hold = _active_hold(db, ont_unit_id, scope)
    if hold is None:
        return EligibilityVerdict(
            ont_unit_id=str(ont_unit_id), scope=scope.value, eligible=True
        )
    return EligibilityVerdict(
        ont_unit_id=str(ont_unit_id),
        scope=scope.value,
        eligible=False,
        hold_id=str(hold.id),
        reason_code=hold.reason_code,
        review_due_at=hold.review_due_at,
        overdue=hold.is_overdue,
    )


def eligibility_under_lock(
    db: Session,
    ont_unit_id: UUID,
    *,
    scope: OntReconcileScope = OntReconcileScope.automatic_sweep,
) -> EligibilityVerdict:
    """The decision the SWEEPER must use, taken at the point of use.

    Locks the ONT row and re-reads the hold inside the caller's transaction, so
    a hold placed after the pass began is still honoured. ``held_ont_ids`` is a
    pre-filter and is NOT sufficient on its own: acting on a set snapshot means
    acting on state that may already be stale.
    """
    try:
        _lock_ont(db, ont_unit_id)
    except ReconcileHoldError:
        # Unknown ONT: nothing to reconcile, and certainly not eligible.
        return EligibilityVerdict(
            ont_unit_id=str(ont_unit_id), scope=scope.value, eligible=False
        )
    return reconcile_eligibility(db, ont_unit_id, scope=scope)


def held_ont_ids(
    db: Session, *, scope: OntReconcileScope = OntReconcileScope.automatic_sweep
) -> frozenset[UUID]:
    """Every ONT currently held for this scope. OPTIMISATION ONLY.

    A pre-filter that saves work; it is never the decision. The authority is
    ``eligibility_under_lock``, taken per ONT inside the transaction that acts
    on it -- a set captured at the start of a pass cannot see a hold placed
    during the pass.
    """
    return frozenset(
        db.execute(
            select(OntReconcileHold.ont_unit_id)
            .where(OntReconcileHold.scope == scope)
            .where(OntReconcileHold.status == OntReconcileHoldStatus.active)
        )
        .scalars()
        .all()
    )


def overdue_holds(db: Session) -> Sequence[OntReconcileHold]:
    """Active holds past their review date.

    These remain in force. They are returned so they can be escalated, NOT so
    they can be auto-released.
    """
    now = datetime.now(UTC)
    return list(
        db.execute(
            select(OntReconcileHold)
            .where(OntReconcileHold.status == OntReconcileHoldStatus.active)
            .where(OntReconcileHold.review_due_at <= now)
            .order_by(OntReconcileHold.review_due_at)
        )
        .scalars()
        .all()
    )


def overdue_hold_alerts(db: Session) -> tuple[OverdueHoldAlert, ...]:
    """Project active overdue holds into idempotent operational alerts."""
    alerts: list[OverdueHoldAlert] = []
    for hold in overdue_holds(db):
        hold_id = str(hold.id)
        ont_unit_id = str(hold.ont_unit_id)
        due = hold.review_due_at
        due_text = due.isoformat() if due is not None else "unknown"
        alerts.append(
            OverdueHoldAlert(
                fingerprint=f"{OVERDUE_ALERT_PREFIX}{hold_id}",
                severity=OverdueHoldAlertSeverity.critical,
                title="ONT reconcile hold review is overdue",
                summary=(
                    f"Review was due {due_text}; automatic reconciliation "
                    "remains suppressed until an explicit release."
                ),
                hold_id=hold_id,
                ont_unit_id=ont_unit_id,
                scope=hold.scope.value,
                reason_code=hold.reason_code,
                review_due_at=due_text,
                target_url=f"/admin/network/onts/{ont_unit_id}",
            )
        )
    return tuple(alerts)
