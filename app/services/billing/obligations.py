"""`billing.obligations` — finite billable units (ADR 0007 Phase 1).

An obligation is the finite billable unit for one contract line, charge
component, source fact, period, and currency. It is not an invoice, a payment,
or an entitlement, and its state is never inferred from an invoice label or a
payment origin string.

Its natural identity is enforced in the database, so replay and concurrency
produce one obligation rather than a duplicate charge:

    contract line + contract version + charge component
      + source fact/version + period start + period end + currency

Phase 1 writes obligations in shadow beside current invoice and renewal
behavior. Authority is read from this owner's declared manifest migration
state, so nothing here can be promoted by a runtime flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import overload
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing_contract import (
    AccountingTreatment,
    BillingContractLine,
    BillingContractVersion,
    BillingObligation,
    BillingRecordAuthority,
    ChargeComponent,
    CollectionTiming,
    ObligationResolutionKind,
    ObligationState,
)
from app.services.billing.cadence import Interval, service_period
from app.services.billing.contracts import BillingContracts
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import (
    OwnerOutputEnvelope,
    consume_owner_output,
    stage_owner_output,
)
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sot_manifest import AuthorityMigrationState

OWNER = "billing.obligations"

_SCHEDULE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="unique billing obligation identity",
    name="schedule_billing_obligation",
)
_OPEN_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="billing obligation state transition",
    name="open_billing_obligation",
)
_CONSUME_CONTRACT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="unique billing obligation identity",
    name="consume_contract_shadow",
)

# States from which an explicit terminal resolution is still allowed.
_RESOLVABLE = {
    ObligationState.open,
    ObligationState.partially_resolved,
}


class BillingObligationError(DomainError):
    """Fail-closed billing-obligation error."""


def _error(suffix: str, message: str, **details: object) -> BillingObligationError:
    return BillingObligationError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


@overload
def _aware_utc(value: datetime) -> datetime: ...


@overload
def _aware_utc(value: None) -> None: ...


def _aware_utc(value: datetime | None) -> datetime | None:
    """Restore UTC tzinfo on instants read back from persistence.

    SQLite drops timezone metadata in tests; production PostgreSQL preserves
    the UTC offset the owner wrote.

    Overloaded because it reads both nullable columns (``ends_at``) and
    non-nullable ones (``starts_at``). Without this a non-null instant comes
    back widened to ``datetime | None`` and callers that genuinely cannot see
    None would each need their own assert.
    """

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def permitted_authority() -> BillingRecordAuthority:
    """Return the authority this owner may write, from the manifest state."""

    from app.services.sot_relationships import service_relationship

    contract = service_relationship(OWNER).contract
    if contract is None:  # pragma: no cover - manifest guarantees the contract
        raise _error(
            "command_contract_violation",
            "Billing obligation owner has no typed manifest contract.",
        )
    if contract.migration.state in {
        AuthorityMigrationState.CUT_OVER,
        AuthorityMigrationState.COMPLETE,
    }:
        return BillingRecordAuthority.authoritative
    return BillingRecordAuthority.shadow


@dataclass(frozen=True)
class ScheduleObligationCommand:
    """Typed request to create one obligation for an exact period."""

    contract_version_id: UUID
    contract_line_key: UUID
    period_index: int
    # Rated amounts. Phase 2's rating owner supplies these; Phase 1 backfill
    # carries the contracted line amount directly.
    net_amount: Decimal
    tax_amount: Decimal = Decimal("0")
    due_at: datetime | None = None


@dataclass(frozen=True)
class ObligationResult:
    """Outcome of scheduling an obligation, including replay detection."""

    obligation_id: UUID
    state: ObligationState
    authority: BillingRecordAuthority
    period: Interval
    gross_amount: Decimal
    replayed: bool


class BillingObligations:
    """Public command owner for finite billing obligations."""

    @staticmethod
    def schedule(
        db: Session,
        command: ScheduleObligationCommand,
        *,
        context: CommandContext,
    ) -> ObligationResult:
        """Create one obligation for an exact contract period."""

        return execute_owner_command(
            db,
            definition=_SCHEDULE_COMMAND,
            context=context,
            operation=lambda: BillingObligations._schedule(
                db, command=command, context=context
            ),
        )

    @staticmethod
    def consume_contract_shadow(
        db: Session,
        *,
        sales_order_id: UUID,
        commands: tuple[ScheduleObligationCommand, ...],
        event_id: UUID,
        context: CommandContext,
    ) -> tuple[ObligationResult, ...] | None:
        """Receipt recorded contract versions and schedule shadow obligations."""

        def _effect() -> tuple[ObligationResult, ...]:
            results = tuple(
                BillingObligations._schedule(db, command=command, context=context)
                for command in commands
            )
            stage_owner_output(
                db,
                OwnerOutputEnvelope(
                    event_type=EventType.custom,
                    producer_owner=OWNER,
                    source_kind="sales_order",
                    source_id=sales_order_id,
                ),
                {
                    "output": "billing.obligations.shadow_scheduled",
                    "sales_order_id": str(sales_order_id),
                    "obligations": [
                        {
                            "obligation_id": str(result.obligation_id),
                            "authority": result.authority.value,
                            "state": result.state.value,
                            "period_start": result.period.starts_at.isoformat(),
                            "period_end": result.period.ends_at.isoformat(),
                            "gross_amount": str(result.gross_amount),
                        }
                        for result in results
                    ],
                },
                context=context,
            )
            return results

        return execute_owner_command(
            db,
            definition=_CONSUME_CONTRACT_COMMAND,
            context=context,
            operation=lambda: consume_owner_output(
                db,
                consumer=OWNER,
                event_id=event_id,
                event_type="billing.contracts.shadow_recorded",
                producer_owner="billing.contracts",
                context=context,
                operation=_effect,
            )[0],
        )

    @staticmethod
    def _schedule(
        db: Session,
        *,
        command: ScheduleObligationCommand,
        context: CommandContext,
    ) -> ObligationResult:
        if not context.idempotency_key:
            raise _error(
                "missing_idempotency_key",
                "Scheduling an obligation requires a business idempotency key.",
            )
        if command.net_amount < 0 or command.tax_amount < 0:
            raise _error(
                "invalid_obligation_amount",
                "Obligation net and tax amounts cannot be negative.",
            )

        version = lock_for_update(
            db, BillingContractVersion, command.contract_version_id
        )
        if version is None:
            raise _error(
                "contract_version_not_found",
                "Obligation requires an existing contract version.",
                contract_version_id=str(command.contract_version_id),
            )

        line = db.execute(
            select(BillingContractLine).where(
                BillingContractLine.contract_version_id == version.id,
                BillingContractLine.contract_line_key == command.contract_line_key,
            )
        ).scalar_one_or_none()
        if line is None:
            raise _error(
                "contract_line_not_found",
                "Obligation requires a line on the named contract version.",
                contract_line_key=str(command.contract_line_key),
            )

        cadence = BillingContracts.cadence_of(version)
        period = service_period(
            cadence=cadence,
            contract_start=_aware_utc(version.starts_at),
            index=command.period_index,
        )
        version_ends_at = _aware_utc(version.ends_at)
        if version_ends_at is not None and period.starts_at >= version_ends_at:
            raise _error(
                "period_outside_contract_version",
                "Obligation period starts after this version stopped applying.",
                period_start=period.starts_at.isoformat(),
                version_ends_at=version_ends_at.isoformat(),
            )

        gross = command.net_amount + command.tax_amount
        authority = permitted_authority()

        existing = db.execute(
            select(BillingObligation).where(
                BillingObligation.contract_line_key == command.contract_line_key,
                BillingObligation.contract_version_id == version.id,
                BillingObligation.charge_component == line.charge_component,
                BillingObligation.source_kind == version.source_kind,
                BillingObligation.source_id == version.source_id,
                BillingObligation.source_version == version.source_version,
                BillingObligation.period_start == period.starts_at,
                BillingObligation.period_end == period.ends_at,
                BillingObligation.currency == line.currency,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return ObligationResult(
                obligation_id=existing.id,
                state=existing.state,
                authority=existing.authority,
                period=period,
                gross_amount=existing.gross_amount,
                replayed=True,
            )

        obligation = BillingObligation(
            contract_id=version.contract_id,
            contract_version_id=version.id,
            contract_line_key=command.contract_line_key,
            account_id=version.account_id,
            subscription_id=version.subscription_id,
            authority=authority,
            charge_component=line.charge_component,
            source_kind=version.source_kind,
            source_id=version.source_id,
            source_version=version.source_version,
            period_start=period.starts_at,
            period_end=period.ends_at,
            currency=line.currency,
            net_amount=command.net_amount,
            tax_amount=command.tax_amount,
            gross_amount=gross,
            accounting_treatment=line.accounting_treatment,
            collection_timing=version.collection_timing,
            is_finite=line.is_finite,
            state=ObligationState.scheduled,
            due_at=command.due_at,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            idempotency_key=context.idempotency_key,
        )
        db.add(obligation)
        try:
            db.flush()
        except IntegrityError as exc:
            # The natural-identity constraint is the real guarantee; a
            # concurrent command that won the race already created this exact
            # obligation. Fail closed rather than charging twice.
            raise _error(
                "duplicate_obligation",
                "An obligation already exists for this exact natural identity.",
                contract_line_key=str(command.contract_line_key),
                period_start=period.starts_at.isoformat(),
            ) from exc

        return ObligationResult(
            obligation_id=obligation.id,
            state=obligation.state,
            authority=authority,
            period=period,
            gross_amount=gross,
            replayed=False,
        )

    @staticmethod
    def open(
        db: Session,
        *,
        obligation_id: UUID,
        context: CommandContext,
        opened_at: datetime | None = None,
    ) -> ObligationState:
        """Move a scheduled obligation to open. Idempotent."""

        return execute_owner_command(
            db,
            definition=_OPEN_COMMAND,
            context=context,
            operation=lambda: BillingObligations._open(
                db, obligation_id=obligation_id, opened_at=opened_at
            ),
        )

    @staticmethod
    def _open(
        db: Session, *, obligation_id: UUID, opened_at: datetime | None
    ) -> ObligationState:
        obligation = BillingObligations._locked(db, obligation_id)
        if obligation.state is ObligationState.open:
            return obligation.state
        if obligation.state is not ObligationState.scheduled:
            raise _error(
                "invalid_obligation_transition",
                "Only a scheduled obligation can be opened.",
                state=obligation.state.value,
            )
        obligation.state = ObligationState.open
        obligation.opened_at = opened_at or obligation.period_start
        db.flush()
        return obligation.state

    @staticmethod
    def resolve(
        db: Session,
        *,
        obligation_id: UUID,
        kind: ObligationResolutionKind,
        amount: Decimal,
        context: CommandContext,
        resolved_at: datetime | None = None,
    ) -> ObligationState:
        """Apply an explicit terminal or partial resolution.

        Every terminal resolution is explicit (settlement, credit, prepaid
        consumption, grant, waiver, write-off, pre-earning cancellation, or
        reversal). Nothing here infers resolution from an invoice status.
        """

        return execute_owner_command(
            db,
            definition=_OPEN_COMMAND,
            context=context,
            operation=lambda: BillingObligations._resolve(
                db,
                obligation_id=obligation_id,
                kind=kind,
                amount=amount,
                resolved_at=resolved_at,
            ),
        )

    @staticmethod
    def _resolve(
        db: Session,
        *,
        obligation_id: UUID,
        kind: ObligationResolutionKind,
        amount: Decimal,
        resolved_at: datetime | None,
    ) -> ObligationState:
        obligation = BillingObligations._locked(db, obligation_id)
        if obligation.state not in _RESOLVABLE:
            raise _error(
                "invalid_obligation_transition",
                "Only an open or partially resolved obligation can resolve.",
                state=obligation.state.value,
            )
        if amount <= 0:
            raise _error(
                "invalid_obligation_amount",
                "A resolution amount must be positive.",
            )

        applied = obligation.resolved_amount + amount
        if applied > obligation.gross_amount:
            raise _error(
                "resolution_exceeds_obligation",
                "Applications cannot exceed the obligation's gross amount.",
                gross_amount=str(obligation.gross_amount),
                attempted=str(applied),
            )

        obligation.resolved_amount = applied
        obligation.resolution_kind = kind
        if kind is ObligationResolutionKind.write_off:
            obligation.state = ObligationState.written_off
        elif kind is ObligationResolutionKind.pre_earning_cancellation:
            obligation.state = ObligationState.canceled
        elif applied == obligation.gross_amount:
            obligation.state = ObligationState.resolved
        else:
            obligation.state = ObligationState.partially_resolved

        if obligation.state is not ObligationState.partially_resolved:
            obligation.resolved_at = resolved_at or obligation.period_end
        db.flush()
        return obligation.state

    @staticmethod
    def _locked(db: Session, obligation_id: UUID) -> BillingObligation:
        obligation = lock_for_update(db, BillingObligation, obligation_id)
        if obligation is None:
            raise _error(
                "obligation_not_found",
                "Obligation does not exist.",
                obligation_id=str(obligation_id),
            )
        return obligation

    @staticmethod
    def open_obligations_for_account(
        db: Session,
        *,
        account_id: UUID,
        currency: str,
        treatment: AccountingTreatment | None = None,
    ) -> list[BillingObligation]:
        """Read-only: open obligations for one account and currency.

        Scoped to one account and currency by design. ADR 0007 invariant 13
        forbids a nominal cross-currency comparison, and section 7 forbids a
        business-wide scan standing in for a durable timer.
        """

        query = select(BillingObligation).where(
            BillingObligation.account_id == account_id,
            BillingObligation.currency == currency,
            BillingObligation.state.in_(
                (ObligationState.open, ObligationState.partially_resolved)
            ),
        )
        if treatment is not None:
            query = query.where(BillingObligation.accounting_treatment == treatment)
        ordered = query.order_by(BillingObligation.period_start)
        return list(db.execute(ordered).scalars())


__all__ = [
    "BillingObligationError",
    "BillingObligations",
    "ChargeComponent",
    "CollectionTiming",
    "ObligationResult",
    "ScheduleObligationCommand",
]
