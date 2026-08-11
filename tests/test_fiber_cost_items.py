"""What a fiber drop costs is data, and an unpriced component is not free.

The estimator used four hardcoded components, each restated in a `SettingSpec`,
a reader in `web_network_fiber`, and the arithmetic in the map template's
JavaScript. Their defaults — 2.50/m, 1.50/m, 85.00, 50.00 — were USD-shaped
values rendered with `billing/default_currency`, so against NGN the page quoted
₦85 for an ONT and nothing anywhere could notice: an amount carries no currency
of its own and therefore looks correct in every one.

So the components are rows, the arithmetic has one home, and a component with no
price makes the estimate INCOMPLETE rather than contributing zero.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.fiber_cost_item import FiberCostItem, FiberCostUnit
from app.services import fiber_cost_items


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

    estimate = fiber_cost_items.estimate_for_distance(db_session, 120)

    assert estimate.is_complete
    assert [line.code for line in estimate.lines] == ["drop_cable", "ont"]
    assert estimate.lines[0].total == Decimal("1200.00")
    assert estimate.lines[1].total == Decimal("35000.00")
    assert estimate.total == Decimal("36200.00")


def test_an_unpriced_component_makes_the_estimate_incomplete(db_session):
    """The heart of it.

    Treating an unpriced component as zero would produce a total that looks
    like an answer — which is exactly the failure the retired defaults caused,
    with the number merely wrong rather than absent.
    """

    _item(db_session, "drop_cable", FiberCostUnit.PER_METER, Decimal("10.00"))
    _item(db_session, "permit_fee", FiberCostUnit.FLAT, None)

    estimate = fiber_cost_items.estimate_for_distance(db_session, 100)

    assert not estimate.is_complete
    assert estimate.unpriced == ("permit_fee",)
    assert all(line.code != "permit_fee" for line in estimate.lines)


def test_no_components_at_all_is_also_incomplete(db_session):
    estimate = fiber_cost_items.estimate_for_distance(db_session, 100)

    assert not estimate.is_complete
    assert estimate.lines == ()
    assert estimate.total == Decimal("0.00")


def test_an_inactive_component_is_neither_priced_nor_reported(db_session):
    """Retiring a component must not make every estimate incomplete forever."""

    _item(db_session, "drop_cable", FiberCostUnit.PER_METER, Decimal("10.00"))
    _item(db_session, "old_thing", FiberCostUnit.FLAT, None, active=False)

    estimate = fiber_cost_items.estimate_for_distance(db_session, 10)

    assert estimate.is_complete
    assert estimate.unpriced == ()


def test_zero_is_a_price_and_none_is_not(db_session):
    """A free component is a real answer; not-priced-yet is a different one."""

    _item(db_session, "free_thing", FiberCostUnit.FLAT, Decimal("0.00"))

    estimate = fiber_cost_items.estimate_for_distance(db_session, 10)

    assert estimate.is_complete
    assert estimate.total == Decimal("0.00")
    assert [line.code for line in estimate.lines] == ["free_thing"]


def test_an_empty_amount_field_means_unpriced_not_zero(db_session):
    item = fiber_cost_items.create_item(
        db_session, code="Splice Closure", label="Splice closure", unit="flat"
    )

    assert item.code == "splice_closure"
    assert item.amount is None
    assert not item.is_priced


def test_a_duplicate_code_is_refused(db_session):
    fiber_cost_items.create_item(
        db_session, code="ont", label="ONT", unit="flat", amount="35000"
    )

    with pytest.raises(fiber_cost_items.FiberCostItemError, match="already exists"):
        fiber_cost_items.create_item(
            db_session, code="ont", label="ONT again", unit="flat"
        )


def test_a_unit_the_estimator_cannot_apply_is_refused(db_session):
    with pytest.raises(fiber_cost_items.FiberCostItemError, match="not a unit"):
        fiber_cost_items.create_item(
            db_session, code="per_pole", label="Per pole", unit="per_pole"
        )


def test_a_negative_cost_is_refused(db_session):
    with pytest.raises(fiber_cost_items.FiberCostItemError, match="negative"):
        fiber_cost_items.create_item(
            db_session, code="rebate", label="Rebate", unit="flat", amount="-5"
        )


def test_pricing_state_tells_the_page_why_it_cannot_estimate(db_session):
    _item(db_session, "drop_cable", FiberCostUnit.PER_METER, None)

    state = fiber_cost_items.pricing_state(db_session)

    assert state["is_complete"] is False
    assert state["unpriced"] == ["drop_cable"]
    # The page needs the currency to label its own message; it does not receive
    # amounts, because it no longer does the arithmetic.
    assert state["currency"]
    assert "amount" not in state
