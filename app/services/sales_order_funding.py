"""`sales.order_funding` — the finite order-funding gate (ADR 0007 Phase 6).

The accepted SalesOrderLine creates a finite contract/obligation chain; this
owner records exactly which finite obligations one order depends on and
advances the order's funding gate only by consuming their exact resolution
outputs.

Rules enforced here, straight from the ADR:

- partial funding never advances the gate;
- the complete finite set resolving advances it exactly once, staging the
  ``sales.order_funding.completed`` owner output;
- an obligation that was never registered as part of the finite set — a
  future recurring obligation on the subscription contract — cannot affect
  the gate at all;
- a resolution recorded twice is idempotent.

Billing still cannot activate service: the funded gate is one input to the
fulfillment and access owners, not an access mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import BillingRecordAuthority
from app.models.sales_order_funding import (
    FundingGateState,
    SalesOrderFundingGate,
    SalesOrderFundingObligation,
)
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sot_manifest import AuthorityMigrationState

OWNER = "sales.order_funding"

_REGISTER_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="finite order-obligation funding set",
    name="register_finite_obligations",
)
_RESOLVE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="exact funding-gate transition evidence",
    name="record_obligation_resolution",
)


class OrderFundingError(DomainError):
    """Fail-closed order-funding error."""


def _error(suffix: str, message: str, **details: object) -> OrderFundingError:
    return OrderFundingError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def permitted_authority() -> BillingRecordAuthority:
    """Return the authority this owner may write, from the manifest state."""

    from app.services.sot_relationships import service_relationship

    contract = service_relationship(OWNER).contract
    if contract is None:  # pragma: no cover - manifest guarantees the contract
        raise _error(
            "command_contract_violation",
            "Order funding owner has no typed manifest contract.",
        )
    if contract.migration.state in {
        AuthorityMigrationState.CUT_OVER,
        AuthorityMigrationState.COMPLETE,
    }:
        return BillingRecordAuthority.authoritative
    return BillingRecordAuthority.shadow


@dataclass(frozen=True)
class FundingGateStatus:
    """Typed gate status after a command."""

    gate_id: UUID
    state: FundingGateState
    total_obligations: int
    resolved_obligations: int
    funded_event_id: UUID | None


class SalesOrderFunding:
    """Public command owner for the finite order-funding gate."""

    @staticmethod
    def register_finite_obligations(
        db: Session,
        *,
        sales_order_id: UUID,
        obligation_ids: tuple[UUID, ...],
        context: CommandContext,
    ) -> FundingGateStatus:
        """Bind the exact finite obligation set to one order's gate.

        Idempotent per obligation. Registration is only legal while the gate
        is pending: the finite historical result cannot be inflated later.
        """

        if not obligation_ids:
            raise _error(
                "empty_finite_obligation_set",
                "A funding gate requires at least one finite obligation.",
            )
        return execute_owner_command(
            db,
            definition=_REGISTER_COMMAND,
            context=context,
            operation=lambda: SalesOrderFunding._register(
                db,
                sales_order_id=sales_order_id,
                obligation_ids=obligation_ids,
                context=context,
            ),
        )

    @staticmethod
    def _gate(
        db: Session, *, sales_order_id: UUID, context: CommandContext
    ) -> SalesOrderFundingGate:
        gate = db.execute(
            select(SalesOrderFundingGate)
            .where(SalesOrderFundingGate.sales_order_id == sales_order_id)
            .with_for_update()
        ).scalar_one_or_none()
        if gate is None:
            gate = SalesOrderFundingGate(
                sales_order_id=sales_order_id,
                authority=permitted_authority(),
                command_id=context.command_id,
                correlation_id=context.correlation_id,
            )
            db.add(gate)
            db.flush()
        return gate

    @staticmethod
    def _register(
        db: Session,
        *,
        sales_order_id: UUID,
        obligation_ids: tuple[UUID, ...],
        context: CommandContext,
    ) -> FundingGateStatus:
        gate = SalesOrderFunding._gate(
            db, sales_order_id=sales_order_id, context=context
        )
        if gate.state is not FundingGateState.pending:
            raise _error(
                "gate_already_funded",
                "The finite obligation set of a funded order cannot change.",
                sales_order_id=str(sales_order_id),
            )

        existing = {
            row.obligation_id
            for row in db.execute(
                select(SalesOrderFundingObligation).where(
                    SalesOrderFundingObligation.gate_id == gate.id
                )
            ).scalars()
        }
        for obligation_id in obligation_ids:
            if obligation_id in existing:
                continue
            db.add(
                SalesOrderFundingObligation(
                    gate_id=gate.id, obligation_id=obligation_id
                )
            )
        db.flush()
        return SalesOrderFunding._status(db, gate)

    @staticmethod
    def record_obligation_resolution(
        db: Session,
        *,
        sales_order_id: UUID,
        obligation_id: UUID,
        resolution_kind: str,
        resolved_event_id: UUID,
        resolved_at: datetime,
        context: CommandContext,
    ) -> FundingGateStatus:
        """Consume one exact obligation-resolution output.

        Unregistered obligations fail closed — a recurring obligation cannot
        touch the historical order. When the last registered obligation
        resolves, the gate advances exactly once and stages its output.
        """

        if resolved_at.tzinfo is None:
            raise _error(
                "invalid_resolution_instant",
                "Resolution evidence requires a timezone-aware instant.",
            )
        return execute_owner_command(
            db,
            definition=_RESOLVE_COMMAND,
            context=context,
            operation=lambda: SalesOrderFunding._record_resolution(
                db,
                sales_order_id=sales_order_id,
                obligation_id=obligation_id,
                resolution_kind=resolution_kind,
                resolved_event_id=resolved_event_id,
                resolved_at=resolved_at,
                context=context,
            ),
        )

    @staticmethod
    def _record_resolution(
        db: Session,
        *,
        sales_order_id: UUID,
        obligation_id: UUID,
        resolution_kind: str,
        resolved_event_id: UUID,
        resolved_at: datetime,
        context: CommandContext,
    ) -> FundingGateStatus:
        gate = db.execute(
            select(SalesOrderFundingGate)
            .where(SalesOrderFundingGate.sales_order_id == sales_order_id)
            .with_for_update()
        ).scalar_one_or_none()
        if gate is None:
            raise _error(
                "funding_gate_not_found",
                "No funding gate exists for this order.",
                sales_order_id=str(sales_order_id),
            )

        row = db.execute(
            select(SalesOrderFundingObligation).where(
                SalesOrderFundingObligation.gate_id == gate.id,
                SalesOrderFundingObligation.obligation_id == obligation_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise _error(
                "obligation_not_in_finite_set",
                "Only registered finite obligations can fund this order.",
                sales_order_id=str(sales_order_id),
                obligation_id=str(obligation_id),
            )

        if not row.resolved:
            row.resolved = True
            row.resolution_kind = resolution_kind
            row.resolved_event_id = resolved_event_id
            row.resolved_at = resolved_at
            db.flush()

        status = SalesOrderFunding._status(db, gate)
        if (
            gate.state is FundingGateState.pending
            and status.resolved_obligations == status.total_obligations
        ):
            gate.state = FundingGateState.funded
            gate.funded_at = resolved_at
            funded_event_id = stage_owner_output(
                db,
                OwnerOutputEnvelope(
                    event_type=EventType.custom,
                    producer_owner=OWNER,
                    source_kind="sales_order_funding_gate",
                    source_id=gate.id,
                ),
                {
                    "output": "sales.order_funding.completed",
                    "sales_order_id": str(sales_order_id),
                    "obligation_count": status.total_obligations,
                },
                context=context,
            )
            gate.funded_event_id = funded_event_id
            db.flush()
            status = SalesOrderFunding._status(db, gate)
        return status

    @staticmethod
    def gate_status(db: Session, *, sales_order_id: UUID) -> FundingGateStatus | None:
        """Read-only: the gate status for one order, if a gate exists."""

        gate = db.execute(
            select(SalesOrderFundingGate).where(
                SalesOrderFundingGate.sales_order_id == sales_order_id
            )
        ).scalar_one_or_none()
        if gate is None:
            return None
        return SalesOrderFunding._status(db, gate)

    @staticmethod
    def _status(db: Session, gate: SalesOrderFundingGate) -> FundingGateStatus:
        rows = list(
            db.execute(
                select(SalesOrderFundingObligation.resolved).where(
                    SalesOrderFundingObligation.gate_id == gate.id
                )
            ).scalars()
        )
        return FundingGateStatus(
            gate_id=gate.id,
            state=gate.state,
            total_obligations=len(rows),
            resolved_obligations=sum(1 for resolved in rows if resolved),
            funded_event_id=gate.funded_event_id,
        )


__all__ = [
    "FundingGateStatus",
    "OrderFundingError",
    "SalesOrderFunding",
]
