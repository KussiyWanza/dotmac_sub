"""`financial.customer_subledger` — operational customer postings (ADR 0007 Phase 3).

The public business owner decides *why* money moves; this module records the
immutable posting evidence and derives typed per-currency position from it.

Two rules shape the API:

1. ``stage_posting_group`` is a **required flush-only participant**. It runs
   only inside another owner's active ``execute_owner_command`` transaction and
   never commits on its own. The document/resolution, posting, audit, and event
   therefore commit atomically or roll back together.
2. Posted history is append-only. A wrong posting is corrected by
   ``stage_reversal`` — a linked group whose effects negate the original in
   position math — never by update or delete.

Position is derived only from these postings, per currency and semantic lane.
There is no cross-currency total and no generic mutable balance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import BillingRecordAuthority
from app.models.customer_subledger import (
    CustomerPositionEffect,
    CustomerPostingGroup,
    PositionEffectKind,
    PostingCommandKind,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext, owner_command_active
from app.services.sot_manifest import AuthorityMigrationState

OWNER = "financial.customer_subledger"


class CustomerSubledgerError(DomainError):
    """Fail-closed customer-subledger error."""


def _error(suffix: str, message: str, **details: object) -> CustomerSubledgerError:
    return CustomerSubledgerError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def permitted_authority() -> BillingRecordAuthority:
    """Return the authority this owner may write, from the manifest state."""

    from app.services.sot_relationships import service_relationship

    contract = service_relationship(OWNER).contract
    if contract is None:  # pragma: no cover - manifest guarantees the contract
        raise _error(
            "command_contract_violation",
            "Customer subledger owner has no typed manifest contract.",
        )
    if contract.migration.state in {
        AuthorityMigrationState.CUT_OVER,
        AuthorityMigrationState.COMPLETE,
    }:
        return BillingRecordAuthority.authoritative
    return BillingRecordAuthority.shadow


@dataclass(frozen=True)
class EffectInput:
    """One typed movement carried by a posting group."""

    effect: PositionEffectKind
    amount: Decimal
    obligation_id: UUID | None = None
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    credit_note_id: UUID | None = None
    entitlement_id: UUID | None = None


@dataclass(frozen=True)
class StagePostingGroupCommand:
    """Typed request to stage one posting group for one business result."""

    account_id: UUID
    currency: str
    command_kind: PostingCommandKind
    producer_owner: str
    source_kind: str
    source_id: UUID
    occurred_at: datetime
    effects: tuple[EffectInput, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CustomerFinancialPosition:
    """Typed per-currency position, one value per semantic lane.

    Lanes are independent: a receivable is not netted against prepaid funding
    and nothing here produces a single signed "balance".
    """

    account_id: UUID
    currency: str
    authority: BillingRecordAuthority
    collectible_receivable: Decimal
    unapplied_customer_credit: Decimal
    prepaid_funding_reserved: Decimal
    prepaid_funding_consumed: Decimal
    written_off_total: Decimal
    refunded_total: Decimal
    adjustment_total: Decimal


# How each effect kind moves its lane. (lane, direction)
_LANE_MOVES: dict[PositionEffectKind, tuple[str, int]] = {
    PositionEffectKind.receivable_issued: ("collectible_receivable", +1),
    PositionEffectKind.receivable_settled: ("collectible_receivable", -1),
    PositionEffectKind.receivable_written_off: ("collectible_receivable", -1),
    PositionEffectKind.customer_credit_created: ("unapplied_customer_credit", +1),
    PositionEffectKind.customer_credit_consumed: ("unapplied_customer_credit", -1),
    PositionEffectKind.credit_refunded: ("unapplied_customer_credit", -1),
    PositionEffectKind.prepaid_funding_reserved: ("prepaid_funding_reserved", +1),
    PositionEffectKind.prepaid_reservation_released: (
        "prepaid_funding_reserved",
        -1,
    ),
    PositionEffectKind.prepaid_funding_consumed: ("prepaid_funding_reserved", -1),
}

# Cumulative evidence lanes (never reduced except by reversal).
_EVIDENCE_MOVES: dict[PositionEffectKind, str] = {
    PositionEffectKind.receivable_written_off: "written_off_total",
    PositionEffectKind.credit_refunded: "refunded_total",
    PositionEffectKind.adjustment_applied: "adjustment_total",
    PositionEffectKind.prepaid_funding_consumed: "prepaid_funding_consumed",
}


def _validate(command: StagePostingGroupCommand, context: CommandContext) -> None:
    if not context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "A posting group requires a business idempotency key.",
        )
    if len(command.currency) != 3:
        raise _error(
            "invalid_posting_currency",
            "Posting currency must be a three-letter code.",
            currency=command.currency,
        )
    if command.occurred_at.tzinfo is None:
        raise _error(
            "invalid_posting_instant",
            "Posting occurred-at must be timezone-aware.",
        )
    for item in command.effects:
        if item.amount <= 0:
            raise _error(
                "invalid_effect_amount",
                "Every position effect amount must be positive.",
                effect=item.effect.value,
            )


def stage_posting_group(
    db: Session,
    command: StagePostingGroupCommand,
    *,
    context: CommandContext,
) -> CustomerPostingGroup:
    """Stage one posting group inside the calling owner's transaction.

    Flush-only: this participant never begins, commits, or rolls back a
    transaction. Calling it outside an active owner command fails closed —
    a posting group without its business result would be an orphan fact.
    """

    if not owner_command_active(db):
        raise _error(
            "posting_requires_owner_command",
            "Posting groups are staged only inside an active owner command.",
        )
    _validate(command, context)

    existing = db.execute(
        select(CustomerPostingGroup).where(
            CustomerPostingGroup.producer_owner == command.producer_owner,
            CustomerPostingGroup.idempotency_key == context.idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # One posting group per idempotent business result: the replayed
        # command gets its original evidence back, bit for bit.
        return existing

    group = CustomerPostingGroup(
        account_id=command.account_id,
        currency=command.currency,
        authority=permitted_authority(),
        command_kind=command.command_kind,
        producer_owner=command.producer_owner,
        source_kind=command.source_kind,
        source_id=command.source_id,
        occurred_at=command.occurred_at,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        causation_id=context.causation_id,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        reason=context.reason,
    )
    db.add(group)
    db.flush()

    for item in command.effects:
        db.add(
            CustomerPositionEffect(
                group_id=group.id,
                effect=item.effect,
                amount=item.amount,
                currency=command.currency,
                obligation_id=item.obligation_id,
                invoice_id=item.invoice_id,
                payment_id=item.payment_id,
                credit_note_id=item.credit_note_id,
                entitlement_id=item.entitlement_id,
            )
        )
    db.flush()
    return group


def stage_reversal(
    db: Session,
    *,
    group_id: UUID,
    context: CommandContext,
    occurred_at: datetime,
) -> CustomerPostingGroup:
    """Stage the reversal of one posted group inside the owner's transaction.

    The reversal is a linked group replaying the original effects; position
    math negates them. History is never edited. A group can be reversed at
    most once (unique reversal chain).
    """

    if not owner_command_active(db):
        raise _error(
            "posting_requires_owner_command",
            "Reversals are staged only inside an active owner command.",
        )
    original = db.get(CustomerPostingGroup, group_id)
    if original is None:
        raise _error(
            "posting_group_not_found",
            "Reversal requires an existing posting group.",
            group_id=str(group_id),
        )
    already = db.execute(
        select(CustomerPostingGroup).where(
            CustomerPostingGroup.reverses_group_id == group_id
        )
    ).scalar_one_or_none()
    if already is not None:
        raise _error(
            "posting_group_already_reversed",
            "A posting group has one active reversal chain.",
            group_id=str(group_id),
            reversal_id=str(already.id),
        )

    reversal = stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=original.account_id,
            currency=original.currency,
            command_kind=PostingCommandKind.reversal,
            producer_owner=original.producer_owner,
            source_kind=original.source_kind,
            source_id=original.source_id,
            occurred_at=occurred_at,
            effects=tuple(
                EffectInput(
                    effect=item.effect,
                    amount=item.amount,
                    obligation_id=item.obligation_id,
                    invoice_id=item.invoice_id,
                    payment_id=item.payment_id,
                    credit_note_id=item.credit_note_id,
                    entitlement_id=item.entitlement_id,
                )
                for item in original.effects
            ),
        ),
        context=context,
    )
    reversal.reverses_group_id = original.id
    db.flush()
    return reversal


def resolve_position(
    db: Session,
    *,
    account_id: UUID,
    currency: str,
    authority: BillingRecordAuthority | None = None,
) -> CustomerFinancialPosition:
    """Derive the typed position for one account and currency.

    Read-only aggregation over immutable postings. A reversal group negates
    its original's contribution. ``authority`` defaults to what this owner is
    currently permitted to write, so shadow evidence is compared against
    shadow and an authoritative read never counts shadow rows.
    """

    resolved_authority = authority or permitted_authority()
    lanes = {
        "collectible_receivable": Decimal("0"),
        "unapplied_customer_credit": Decimal("0"),
        "prepaid_funding_reserved": Decimal("0"),
        "prepaid_funding_consumed": Decimal("0"),
        "written_off_total": Decimal("0"),
        "refunded_total": Decimal("0"),
        "adjustment_total": Decimal("0"),
    }

    rows = db.execute(
        select(
            CustomerPositionEffect.effect,
            CustomerPositionEffect.amount,
            CustomerPostingGroup.reverses_group_id,
        )
        .join(
            CustomerPostingGroup,
            CustomerPositionEffect.group_id == CustomerPostingGroup.id,
        )
        .where(
            CustomerPostingGroup.account_id == account_id,
            CustomerPostingGroup.currency == currency,
            CustomerPostingGroup.authority == resolved_authority,
        )
    ).all()

    for effect, amount, reverses_group_id in rows:
        sign = -1 if reverses_group_id is not None else 1
        value = Decimal(amount) * sign
        move = _LANE_MOVES.get(effect)
        if move is not None:
            lane, direction = move
            lanes[lane] += value * direction
        evidence_lane = _EVIDENCE_MOVES.get(effect)
        if evidence_lane is not None:
            lanes[evidence_lane] += value

    return CustomerFinancialPosition(
        account_id=account_id,
        currency=currency,
        authority=resolved_authority,
        **lanes,
    )


__all__ = [
    "CustomerFinancialPosition",
    "CustomerSubledgerError",
    "EffectInput",
    "PositionEffectKind",
    "PostingCommandKind",
    "StagePostingGroupCommand",
    "resolve_position",
    "stage_posting_group",
    "stage_reversal",
]
