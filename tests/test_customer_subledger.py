"""Behavior coverage for `financial.customer_subledger` (ADR 0007 Phase 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing_contract import BillingRecordAuthority
from app.services.billing.customer_subledger import (
    CustomerSubledgerError,
    EffectInput,
    PositionEffectKind,
    PostingCommandKind,
    PostingProducer,
    PostingSourceKind,
    StagePostingGroupCommand,
    StageReversalCommand,
    resolve_position,
    stage_posting_group,
    stage_reversal,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OCCURRED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

# The subledger is a participant: tests drive it through a real contracted
# owner command boundary, exactly as a deciding money owner would.
_HOST_COMMAND = OwnerCommandDefinition(
    owner="billing.contracts",
    concern="versioned billing contract terms",
    name="record_billing_contract_version",
)


def _context(key: str | None = None) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="customer-subledger:test",
        reason="pytest customer subledger",
        idempotency_key=key or f"pytest:{command_id}",
    )


def _in_owner_command(db, operation):
    return execute_owner_command(
        db, definition=_HOST_COMMAND, context=_context(), operation=operation
    )


def _stage(
    db,
    account_id,
    *,
    effects,
    key=None,
    kind=PostingCommandKind.adjustment,
    source_id=None,
):
    command = StagePostingGroupCommand(
        account_id=account_id,
        currency="NGN",
        command_kind=kind,
        producer_owner=PostingProducer.account_adjustments,
        source_kind=PostingSourceKind.account_adjustment,
        source_id=source_id if source_id is not None else uuid4(),
        occurred_at=OCCURRED,
        effects=effects,
    )
    context = _context(key)
    return _in_owner_command(
        db, lambda: stage_posting_group(db, command, context=context)
    )


@pytest.fixture()
def account_id(db_session, subscriber):
    captured = subscriber.id
    db_session.commit()
    return captured


def test_staging_outside_an_owner_command_fails_closed(db_session, account_id):
    command = StagePostingGroupCommand(
        account_id=account_id,
        currency="NGN",
        command_kind=PostingCommandKind.adjustment,
        producer_owner=PostingProducer.account_adjustments,
        source_kind=PostingSourceKind.account_adjustment,
        source_id=uuid4(),
        occurred_at=OCCURRED,
        effects=(),
    )

    with pytest.raises(CustomerSubledgerError) as excinfo:
        stage_posting_group(db_session, command, context=_context())

    assert excinfo.value.code == (
        "financial.customer_subledger.posting_requires_owner_command"
    )


def test_one_posting_group_per_idempotent_business_result(db_session, account_id):
    effects = (
        EffectInput(
            effect=PositionEffectKind.receivable_issued, amount=Decimal("5000.00")
        ),
    )

    source = uuid4()
    first = _stage(
        db_session, account_id, effects=effects, key="pytest:same", source_id=source
    )
    second = _stage(
        db_session, account_id, effects=effects, key="pytest:same", source_id=source
    )

    assert second.id == first.id
    assert len(second.effects) == 1


def test_replayed_key_with_different_meaning_is_a_conflict(db_session, account_id):
    effects = (
        EffectInput(
            effect=PositionEffectKind.receivable_issued, amount=Decimal("5000.00")
        ),
    )
    source = uuid4()
    _stage(
        db_session,
        account_id,
        effects=effects,
        key="pytest:conflict",
        source_id=source,
    )
    db_session.commit()

    changed = (
        EffectInput(
            effect=PositionEffectKind.receivable_issued, amount=Decimal("7500.00")
        ),
    )
    with pytest.raises(CustomerSubledgerError) as excinfo:
        _stage(
            db_session,
            account_id,
            effects=changed,
            key="pytest:conflict",
            source_id=source,
        )
    assert excinfo.value.code == ("financial.customer_subledger.idempotency_conflict")


def test_positions_are_per_currency_and_semantic_lane(db_session, account_id):
    _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.receivable_issue,
        effects=(
            EffectInput(
                effect=PositionEffectKind.receivable_issued,
                amount=Decimal("25000.00"),
            ),
        ),
    )
    _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.payment_settlement,
        effects=(
            EffectInput(
                effect=PositionEffectKind.receivable_settled,
                amount=Decimal("10000.00"),
            ),
            EffectInput(
                effect=PositionEffectKind.customer_credit_created,
                amount=Decimal("2000.00"),
            ),
        ),
    )

    ngn = resolve_position(
        db_session,
        account_id=account_id,
        currency="NGN",
        authority=BillingRecordAuthority.shadow,
    )
    usd = resolve_position(
        db_session,
        account_id=account_id,
        currency="USD",
        authority=BillingRecordAuthority.shadow,
    )

    assert ngn.collectible_receivable == Decimal("15000.00")
    assert ngn.unapplied_customer_credit == Decimal("2000.00")
    # No cross-currency total: the USD position is independently zero.
    assert usd.collectible_receivable == Decimal("0")


def test_prepaid_lanes_track_reservation_and_consumption(db_session, account_id):
    _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.prepaid_reservation,
        effects=(
            EffectInput(
                effect=PositionEffectKind.prepaid_funding_reserved,
                amount=Decimal("8000.00"),
            ),
        ),
    )
    _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.prepaid_consumption,
        effects=(
            EffectInput(
                effect=PositionEffectKind.prepaid_funding_consumed,
                amount=Decimal("8000.00"),
            ),
        ),
    )

    position = resolve_position(
        db_session,
        account_id=account_id,
        currency="NGN",
        authority=BillingRecordAuthority.shadow,
    )

    assert position.prepaid_funding_reserved == Decimal("0")
    assert position.prepaid_funding_consumed == Decimal("8000.00")


def test_write_off_reduces_receivable_and_keeps_evidence(db_session, account_id):
    _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.receivable_issue,
        effects=(
            EffectInput(
                effect=PositionEffectKind.receivable_issued,
                amount=Decimal("25000.00"),
            ),
        ),
    )
    _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.write_off,
        effects=(
            EffectInput(
                effect=PositionEffectKind.receivable_written_off,
                amount=Decimal("25000.00"),
            ),
        ),
    )

    position = resolve_position(
        db_session,
        account_id=account_id,
        currency="NGN",
        authority=BillingRecordAuthority.shadow,
    )

    assert position.collectible_receivable == Decimal("0")
    # The evidence lane survives: a write-off is not erased history.
    assert position.written_off_total == Decimal("25000.00")


def test_reversal_negates_the_original_and_chains_once(db_session, account_id):
    group = _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.receivable_issue,
        effects=(
            EffectInput(
                effect=PositionEffectKind.receivable_issued,
                amount=Decimal("5000.00"),
            ),
        ),
    )
    group_id = group.id
    db_session.commit()

    reversal_context = _context()
    _in_owner_command(
        db_session,
        lambda: stage_reversal(
            db_session,
            StageReversalCommand(
                original_group_id=group_id,
                producer_owner=PostingProducer.account_adjustments,
                source_kind=PostingSourceKind.account_adjustment,
                source_id=uuid4(),
                occurred_at=OCCURRED,
            ),
            context=reversal_context,
        ),
    )

    position = resolve_position(
        db_session,
        account_id=account_id,
        currency="NGN",
        authority=BillingRecordAuthority.shadow,
    )
    assert position.collectible_receivable == Decimal("0")
    # Close the read transaction the position query opened; the next owner
    # command requires a transaction-free session.
    db_session.commit()

    second_context = _context()
    with pytest.raises(CustomerSubledgerError) as excinfo:
        _in_owner_command(
            db_session,
            lambda: stage_reversal(
                db_session,
                StageReversalCommand(
                    original_group_id=group_id,
                    producer_owner=PostingProducer.account_adjustments,
                    source_kind=PostingSourceKind.account_adjustment,
                    source_id=uuid4(),
                    occurred_at=OCCURRED,
                ),
                context=second_context,
            ),
        )

    assert excinfo.value.code == (
        "financial.customer_subledger.posting_group_already_reversed"
    )


def test_non_positive_effect_amounts_fail_closed(db_session, account_id):
    with pytest.raises(CustomerSubledgerError) as excinfo:
        _stage(
            db_session,
            account_id,
            effects=(
                EffectInput(
                    effect=PositionEffectKind.receivable_issued,
                    amount=Decimal("0"),
                ),
            ),
        )

    assert excinfo.value.code == "financial.customer_subledger.invalid_effect_amount"


def test_shadow_rows_never_count_toward_an_authoritative_read(db_session, account_id):
    _stage(
        db_session,
        account_id,
        kind=PostingCommandKind.receivable_issue,
        effects=(
            EffectInput(
                effect=PositionEffectKind.receivable_issued,
                amount=Decimal("5000.00"),
            ),
        ),
    )

    authoritative = resolve_position(
        db_session,
        account_id=account_id,
        currency="NGN",
        authority=BillingRecordAuthority.authoritative,
    )

    assert authoritative.collectible_receivable == Decimal("0")
