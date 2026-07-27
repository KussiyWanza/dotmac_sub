"""`collections.lifecycle` — one reason-scoped collections owner (ADR 0007 §8).

Both mode policies feed this owner their typed proposals. It alone owns the
case per account/subscription/reason, the warning and escalation states, the
exact durable timers for the next action, consequence-request idempotency, and
close/reopen evidence.

It never mutates subscription or RADIUS state: escalation emits a
reason-scoped consequence request as a durable owner output, and only
`access.subscription_lifecycle` applies or removes the matching restriction.
Settlement or funding closes the case through the same owner, replacing or
canceling its timers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import BillingRecordAuthority
from app.models.collections_case import (
    CollectionsCase,
    CollectionsCaseState,
    CollectionsReason,
)
from app.services.collections.mode_policies import CollectionsProposal
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.runtime_durable_timers import (
    ScheduleTimerCommand,
    cancel_timer,
    schedule_timer,
)
from app.services.sot_manifest import AuthorityMigrationState

OWNER = "collections.lifecycle"

_ADVANCE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="reason-scoped collections case workflow",
    name="advance_collections_case",
)
_CLOSE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="collections case close and reopen evidence",
    name="close_collections_case",
)

_TIMER_PURPOSE_NEXT_ACTION = "collections_next_action"

# The forward path a case may take. Regressions are not transitions; a
# recovered account closes the case instead.
_NEXT_STATE: dict[CollectionsCaseState, CollectionsCaseState] = {
    CollectionsCaseState.open: CollectionsCaseState.warned,
    CollectionsCaseState.warned: CollectionsCaseState.escalated,
    CollectionsCaseState.escalated: CollectionsCaseState.consequence_requested,
}


class CollectionsLifecycleError(DomainError):
    """Fail-closed collections-lifecycle error."""


def _error(suffix: str, message: str, **details: object) -> CollectionsLifecycleError:
    return CollectionsLifecycleError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def permitted_authority() -> BillingRecordAuthority:
    """Return the authority this owner may write, from the manifest state."""

    from app.services.sot_relationships import service_relationship

    contract = service_relationship(OWNER).contract
    if contract is None:  # pragma: no cover - manifest guarantees the contract
        raise _error(
            "command_contract_violation",
            "Collections lifecycle owner has no typed manifest contract.",
        )
    if contract.migration.state in {
        AuthorityMigrationState.CUT_OVER,
        AuthorityMigrationState.COMPLETE,
    }:
        return BillingRecordAuthority.authoritative
    return BillingRecordAuthority.shadow


@dataclass(frozen=True)
class CaseAdvanceResult:
    """Outcome of applying one proposal to the case workflow."""

    case_id: UUID
    state: CollectionsCaseState
    consequence_event_id: UUID | None
    timer_scheduled: bool


class CollectionsLifecycle:
    """Public command owner for reason-scoped collections cases."""

    @staticmethod
    def advance(
        db: Session,
        proposal: CollectionsProposal,
        *,
        context: CommandContext,
        now: datetime,
        next_action_at: datetime | None = None,
    ) -> CaseAdvanceResult:
        """Open or advance the case for one typed policy proposal.

        Each call moves the case one step (open -> warned -> escalated ->
        consequence_requested), schedules the exact next-action timer, and at
        escalation emits the idempotent consequence request for the access
        owner. Re-delivery of the same proposal at a terminal step is a no-op.
        """

        if now.tzinfo is None:
            raise _error(
                "invalid_case_instant",
                "Case transitions require a timezone-aware instant.",
            )
        return execute_owner_command(
            db,
            definition=_ADVANCE_COMMAND,
            context=context,
            operation=lambda: CollectionsLifecycle._advance(
                db,
                proposal=proposal,
                context=context,
                now=now,
                next_action_at=next_action_at,
            ),
        )

    @staticmethod
    def _live_case(
        db: Session, *, proposal: CollectionsProposal
    ) -> CollectionsCase | None:
        return db.execute(
            select(CollectionsCase)
            .where(
                CollectionsCase.account_id == proposal.account_id,
                CollectionsCase.subscription_id == proposal.subscription_id,
                CollectionsCase.reason == proposal.reason,
                CollectionsCase.state != CollectionsCaseState.closed,
            )
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def _advance(
        db: Session,
        *,
        proposal: CollectionsProposal,
        context: CommandContext,
        now: datetime,
        next_action_at: datetime | None,
    ) -> CaseAdvanceResult:
        case = CollectionsLifecycle._live_case(db, proposal=proposal)
        if case is None:
            case = CollectionsCase(
                account_id=proposal.account_id,
                subscription_id=proposal.subscription_id,
                reason=proposal.reason,
                currency=proposal.currency,
                authority=permitted_authority(),
                state=CollectionsCaseState.open,
                source_kind="billing_obligation",
                source_id=proposal.obligation_id,
                opened_at=now,
                command_id=context.command_id,
                correlation_id=context.correlation_id,
            )
            db.add(case)
            db.flush()
            timer_scheduled = CollectionsLifecycle._schedule_next(
                db, case=case, context=context, next_action_at=next_action_at
            )
            return CaseAdvanceResult(
                case_id=case.id,
                state=case.state,
                consequence_event_id=None,
                timer_scheduled=timer_scheduled,
            )

        next_state = _NEXT_STATE.get(case.state)
        if next_state is None:
            # Terminal workflow step: redelivery is harmless.
            return CaseAdvanceResult(
                case_id=case.id,
                state=case.state,
                consequence_event_id=case.consequence_event_id,
                timer_scheduled=False,
            )

        case.state = next_state
        consequence_event_id: UUID | None = None
        if next_state is CollectionsCaseState.warned:
            case.warned_at = now
        elif next_state is CollectionsCaseState.escalated:
            case.escalated_at = now
        else:
            case.consequence_requested_at = now
            case.consequence_idempotency_key = (
                case.consequence_idempotency_key or f"collections:{case.id}:{uuid4()}"
            )
            consequence_event_id = stage_owner_output(
                db,
                OwnerOutputEnvelope(
                    event_type=EventType.custom,
                    producer_owner=OWNER,
                    source_kind="collections_case",
                    source_id=case.id,
                ),
                {
                    "output": "collections.consequence_requested",
                    "reason": case.reason.value,
                    "account_id": str(case.account_id),
                    "subscription_id": str(case.subscription_id),
                    "obligation_id": str(case.source_id),
                    "currency": case.currency,
                    "consequence_idempotency_key": case.consequence_idempotency_key,
                },
                context=context,
                account_id=case.account_id,
                subscription_id=case.subscription_id,
            )
            case.consequence_event_id = consequence_event_id
        db.flush()

        timer_scheduled = False
        if next_state is not CollectionsCaseState.consequence_requested:
            timer_scheduled = CollectionsLifecycle._schedule_next(
                db, case=case, context=context, next_action_at=next_action_at
            )
        else:
            cancel_timer(
                db,
                owner=OWNER,
                entity_kind="collections_case",
                entity_id=case.id,
                purpose=_TIMER_PURPOSE_NEXT_ACTION,
            )

        return CaseAdvanceResult(
            case_id=case.id,
            state=case.state,
            consequence_event_id=consequence_event_id,
            timer_scheduled=timer_scheduled,
        )

    @staticmethod
    def _schedule_next(
        db: Session,
        *,
        case: CollectionsCase,
        context: CommandContext,
        next_action_at: datetime | None,
    ) -> bool:
        if next_action_at is None:
            return False
        schedule_timer(
            db,
            ScheduleTimerCommand(
                owner=OWNER,
                entity_kind="collections_case",
                entity_id=case.id,
                purpose=_TIMER_PURPOSE_NEXT_ACTION,
                due_at=next_action_at,
                output_event_type="collections.case_action_due",
            ),
            context=context,
        )
        return True

    @staticmethod
    def close(
        db: Session,
        *,
        account_id: UUID,
        subscription_id: UUID,
        reason: CollectionsReason,
        context: CommandContext,
        now: datetime,
        close_reason: str,
    ) -> UUID | None:
        """Close the live case after settlement, funding, or correction.

        Cancels the case's next-action timer in the same transaction and
        stages the restore-evaluation output for the access owner. Idempotent:
        no live case means nothing to close.
        """

        if now.tzinfo is None:
            raise _error(
                "invalid_case_instant",
                "Case transitions require a timezone-aware instant.",
            )
        if not close_reason.strip():
            raise _error(
                "missing_close_reason",
                "Closing a case requires close evidence.",
            )
        return execute_owner_command(
            db,
            definition=_CLOSE_COMMAND,
            context=context,
            operation=lambda: CollectionsLifecycle._close(
                db,
                account_id=account_id,
                subscription_id=subscription_id,
                reason=reason,
                context=context,
                now=now,
                close_reason=close_reason,
            ),
        )

    @staticmethod
    def _close(
        db: Session,
        *,
        account_id: UUID,
        subscription_id: UUID,
        reason: CollectionsReason,
        context: CommandContext,
        now: datetime,
        close_reason: str,
    ) -> UUID | None:
        case = db.execute(
            select(CollectionsCase)
            .where(
                CollectionsCase.account_id == account_id,
                CollectionsCase.subscription_id == subscription_id,
                CollectionsCase.reason == reason,
                CollectionsCase.state != CollectionsCaseState.closed,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if case is None:
            return None

        had_consequence = case.state is CollectionsCaseState.consequence_requested
        case.state = CollectionsCaseState.closed
        case.closed_at = now
        case.close_reason = close_reason
        cancel_timer(
            db,
            owner=OWNER,
            entity_kind="collections_case",
            entity_id=case.id,
            purpose=_TIMER_PURPOSE_NEXT_ACTION,
        )
        if had_consequence:
            # The access owner alone removes the matching restriction; this is
            # the reason-scoped restore request, not an access mutation.
            stage_owner_output(
                db,
                OwnerOutputEnvelope(
                    event_type=EventType.custom,
                    producer_owner=OWNER,
                    source_kind="collections_case",
                    source_id=case.id,
                ),
                {
                    "output": "collections.case_closed",
                    "reason": case.reason.value,
                    "account_id": str(case.account_id),
                    "subscription_id": str(case.subscription_id),
                    "close_reason": close_reason,
                    "consequence_idempotency_key": (case.consequence_idempotency_key),
                },
                context=context,
                account_id=case.account_id,
                subscription_id=case.subscription_id,
            )
        db.flush()
        return case.id


__all__ = [
    "CaseAdvanceResult",
    "CollectionsLifecycle",
    "CollectionsLifecycleError",
]
