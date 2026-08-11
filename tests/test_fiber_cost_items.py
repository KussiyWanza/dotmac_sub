"""Behavior and transaction evidence for the fiber drop-cost owner."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditActorType, AuditEvent
from app.models.event_store import EventStore
from app.models.fiber_cost_item import FiberCostItem
from app.schemas.fiber_cost_items import (
    CreateFiberCostItemCommand,
    FiberCostUnit,
    UpdateFiberCostItemCommand,
)
from app.services import fiber_cost_items
from app.services.owner_commands import CommandContext

_ACTOR_ID = uuid4()


def _context(action: str, identity: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{_ACTOR_ID}",
        scope=fiber_cost_items.WRITE_SCOPE,
        reason=f"pytest {action} {identity}",
        idempotency_key=f"pytest:fiber-cost:{action}:{identity}",
    )


def _create_command(
    code: str,
    *,
    label: str | None = None,
    unit: FiberCostUnit = FiberCostUnit.FLAT,
    amount: Decimal | None = None,
    sort_order: int = 100,
) -> CreateFiberCostItemCommand:
    return CreateFiberCostItemCommand(
        context=_context("create", code),
        actor_id=_ACTOR_ID,
        actor_type=AuditActorType.user,
        code=fiber_cost_items.parse_code(code),
        label=label or code.replace("_", " ").title(),
        unit=unit,
        amount=amount,
        sort_order=sort_order,
        description=None,
    )


def _update_command(
    item_id: UUID,
    expected_version: int,
    *,
    label: str = "Updated item",
    unit: FiberCostUnit = FiberCostUnit.FLAT,
    amount: Decimal | None = None,
    is_active: bool = True,
    sort_order: int = 100,
    description: str | None = None,
) -> UpdateFiberCostItemCommand:
    return UpdateFiberCostItemCommand(
        context=_context("update", f"{item_id}:v{expected_version}"),
        actor_id=_ACTOR_ID,
        actor_type=AuditActorType.user,
        item_id=item_id,
        expected_version=expected_version,
        label=label,
        unit=unit,
        amount=amount,
        is_active=is_active,
        sort_order=sort_order,
        description=description,
    )


def _item(db_session, code, unit, amount, *, active=True, order=10):
    item = FiberCostItem(
        code=code,
        label=code.replace("_", " ").title(),
        unit=unit,
        amount=amount,
        is_active=active,
        sort_order=order,
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_a_priced_estimate_sums_per_meter_and_flat(db_session):
    _item(db_session, "drop_cable", FiberCostUnit.PER_METER, Decimal("10.00"), order=1)
    _item(db_session, "ont", FiberCostUnit.FLAT, Decimal("35000.00"), order=2)

    estimate = fiber_cost_items.estimate_for_distance(db_session, Decimal("120"))

    assert estimate.is_complete
    assert [line.code.value for line in estimate.lines] == ["drop_cable", "ont"]
    assert estimate.lines[0].total == Decimal("1200.00")
    assert estimate.lines[1].total == Decimal("35000.00")
    assert estimate.total == Decimal("36200.00")


def test_an_unpriced_component_makes_the_estimate_incomplete(db_session):
    _item(db_session, "drop_cable", FiberCostUnit.PER_METER, Decimal("10.00"))
    _item(db_session, "permit_fee", FiberCostUnit.FLAT, None)

    estimate = fiber_cost_items.estimate_for_distance(db_session, Decimal("100"))

    assert not estimate.is_complete
    assert [code.value for code in estimate.unpriced] == ["permit_fee"]
    assert all(line.code.value != "permit_fee" for line in estimate.lines)


def test_no_components_at_all_is_also_incomplete(db_session):
    estimate = fiber_cost_items.estimate_for_distance(db_session, Decimal("100"))

    assert not estimate.is_complete
    assert estimate.lines == ()
    assert estimate.total == Decimal("0.00")


def test_an_inactive_component_is_neither_priced_nor_reported(db_session):
    _item(db_session, "drop_cable", FiberCostUnit.PER_METER, Decimal("10.00"))
    _item(db_session, "old_thing", FiberCostUnit.FLAT, None, active=False)

    estimate = fiber_cost_items.estimate_for_distance(db_session, Decimal("10"))

    assert estimate.is_complete
    assert estimate.unpriced == ()


def test_zero_is_a_price_and_none_is_not(db_session):
    _item(db_session, "free_thing", FiberCostUnit.FLAT, Decimal("0.00"))

    estimate = fiber_cost_items.estimate_for_distance(db_session, Decimal("10"))

    assert estimate.is_complete
    assert estimate.total == Decimal("0.00")
    assert [line.code.value for line in estimate.lines] == ["free_thing"]


def test_create_normalizes_code_and_records_atomic_evidence(db_session):
    outcome = fiber_cost_items.create_item(
        db_session,
        _create_command("Splice Closure", label="Splice closure"),
    )

    assert outcome.code.value == "splice_closure"
    assert outcome.amount is None
    assert outcome.version == 1
    audit = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_id == str(outcome.item_id),
            AuditEvent.action == "fiber_cost_item.created",
        )
    )
    event = db_session.scalar(
        select(EventStore).where(EventStore.event_type == "fiber.cost_item_changed")
    )
    assert audit is not None
    assert audit.actor_type is AuditActorType.user
    assert audit.actor_id == str(_ACTOR_ID)
    assert audit.metadata_["before"] is None
    assert audit.metadata_["after"]["amount"] is None
    assert event is not None
    assert event.actor == f"user:{_ACTOR_ID}"
    assert event.payload["code"] == "splice_closure"
    assert event.payload["version"] == 1
    assert "amount" not in event.payload


def test_owner_rolls_back_row_audit_and_event_together(db_session, monkeypatch):
    def fail_event(*_args, **_kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(fiber_cost_items, "_announce", fail_event)

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        fiber_cost_items.create_item(db_session, _create_command("rollback_item"))

    assert (
        db_session.scalar(
            select(FiberCostItem).where(FiberCostItem.code == "rollback_item")
        )
        is None
    )
    assert (
        db_session.scalar(
            select(AuditEvent).where(AuditEvent.entity_type == "fiber_cost_item")
        )
        is None
    )
    assert (
        db_session.scalar(
            select(EventStore).where(EventStore.event_type == "fiber.cost_item_changed")
        )
        is None
    )


def test_update_records_before_after_price_and_rejects_stale_version(db_session):
    created = fiber_cost_items.create_item(
        db_session,
        _create_command("ont", label="ONT", amount=Decimal("35000.00")),
    )
    updated = fiber_cost_items.update_item(
        db_session,
        _update_command(
            created.item_id,
            created.version,
            label="ONT device",
            amount=Decimal("42500.00"),
            description="Operator-reviewed replacement price",
        ),
    )

    assert updated.version == 2
    assert updated.amount == Decimal("42500.00")
    audit = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_id == str(created.item_id),
            AuditEvent.action == "fiber_cost_item.updated",
        )
    )
    assert audit is not None
    assert audit.metadata_["before"]["amount"] == "35000.00"
    assert audit.metadata_["before"]["version"] == 1
    assert audit.metadata_["after"]["amount"] == "42500.00"
    assert audit.metadata_["after"]["version"] == 2

    db_session.commit()
    with pytest.raises(fiber_cost_items.FiberCostItemError) as exc_info:
        fiber_cost_items.update_item(
            db_session,
            _update_command(
                created.item_id,
                created.version,
                amount=Decimal("1.00"),
            ),
        )

    assert exc_info.value.code == "network.fiber_cost_items.stale_version"
    current = db_session.get(FiberCostItem, created.item_id)
    assert current is not None
    assert current.amount == Decimal("42500.00")
    assert current.version == 2


def test_a_duplicate_code_is_refused(db_session):
    fiber_cost_items.create_item(db_session, _create_command("ont"))

    with pytest.raises(fiber_cost_items.FiberCostItemError) as exc_info:
        fiber_cost_items.create_item(db_session, _create_command("ont"))

    assert exc_info.value.code == "network.fiber_cost_items.duplicate_code"


@pytest.mark.parametrize("raw", ["not-a-unit", "per_pole"])
def test_a_unit_the_estimator_cannot_apply_is_refused(raw):
    with pytest.raises(fiber_cost_items.FiberCostItemError) as exc_info:
        fiber_cost_items.parse_unit(raw)

    assert exc_info.value.code == "network.fiber_cost_items.unknown_unit"


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "oops"])
def test_a_non_finite_or_non_numeric_cost_is_refused(raw):
    with pytest.raises(fiber_cost_items.FiberCostItemError) as exc_info:
        fiber_cost_items.parse_amount(raw)

    assert exc_info.value.code == "network.fiber_cost_items.invalid_amount"


def test_a_negative_cost_is_refused_at_the_owner_boundary(db_session):
    with pytest.raises(fiber_cost_items.FiberCostItemError) as exc_info:
        fiber_cost_items.create_item(
            db_session,
            _create_command("rebate", amount=Decimal("-5")),
        )

    assert exc_info.value.code == "network.fiber_cost_items.negative_amount"


def test_pricing_state_tells_the_page_why_it_cannot_estimate(db_session):
    _item(db_session, "drop_cable", FiberCostUnit.PER_METER, None)

    state = fiber_cost_items.pricing_state(db_session)

    assert not state.is_complete
    assert [code.value for code in state.unpriced] == ["drop_cable"]
    assert state.currency
