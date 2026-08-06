"""A capped family cannot be sold with nothing enforcing its cap.

``home_flex`` is defined by its cap (PLAN_FAMILY_ARCHITECTURE §1): past the
allowance you keep working at a reduced speed rather than losing service.
Production carries five home_flex offers and **63 subscribers with zero FUP
rules between them** — a product sold on a limit that has never once been
enforced.

Nothing surfaced it, because nothing reads back as missing: the offer looks
complete, the family is set, and the FUP screen shows an empty list that is
indistinguishable from a family which legitimately has no rules (``dedicated``
has twelve such policies, correctly).

The validator refuses the *sale*, not the offer, and deliberately does not
invent a threshold.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.fup import (
    FUP_REQUIRED_FAMILIES,
    DeleteFupRuleCommand,
    FupRuleEngineError,
    FupRulePatch,
    UpdateFupRuleCommand,
    fup_policies,
)
from app.services.web_catalog_offers import (
    MissingSpeedReductionRule,
    assert_sellable_capped_offer_can_enforce,
)
from tests.fup_helpers import add_fup_rule, ensure_fup_policy, fup_command_context


def _offer(db, *, plan_family, sellable=True):
    offer = CatalogOffer(
        name=f"offer-{uuid4().hex[:8]}",
        code=f"c-{uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        plan_family=plan_family,
        is_active=True,
        available_for_services=sellable,
    )
    db.add(offer)
    db.flush()
    db.commit()
    return offer


def _check(db, offer, *, sellable=True):
    assert_sellable_capped_offer_can_enforce(
        db,
        offer_id=str(offer.id),
        plan_family=offer.plan_family,
        available_for_services=sellable,
    )


def test_home_flex_cannot_be_sold_without_a_throttle_rule(db_session):
    offer = _offer(db_session, plan_family="home_flex")
    with pytest.raises(MissingSpeedReductionRule) as caught:
        _check(db_session, offer)
    assert caught.value.code == "catalog.offer.missing_speed_reduction_rule"


def test_an_empty_policy_is_not_a_ladder(db_session):
    """Homeflex Basic in production has exactly this: a policy, no rules."""
    offer = _offer(db_session, plan_family="home_flex")
    ensure_fup_policy(db_session, str(offer.id))
    db_session.commit()
    with pytest.raises(MissingSpeedReductionRule):
        _check(db_session, offer)


def test_a_notify_only_ladder_is_not_enforcement(db_session):
    """Warning a customer is not capping them.

    An offer with only a notify rule would pass a naive "has rules" check while
    enforcing nothing — which is precisely the state ``unlimited`` is in, and
    it is correct there and wrong here.
    """
    offer = _offer(db_session, plan_family="home_flex")
    ensure_fup_policy(db_session, str(offer.id))
    add_fup_rule(
        db_session,
        str(offer.id),
        name="Warn only",
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=100,
        threshold_unit="gb",
        action="notify",
    )
    db_session.commit()
    with pytest.raises(MissingSpeedReductionRule):
        _check(db_session, offer)


def test_a_reduce_speed_rule_satisfies_it(db_session):
    offer = _offer(db_session, plan_family="home_flex")
    ensure_fup_policy(db_session, str(offer.id))
    add_fup_rule(
        db_session,
        str(offer.id),
        name="Throttle",
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=100,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=50,
    )
    db_session.commit()
    _check(db_session, offer)  # no raise


def test_the_threshold_itself_is_never_second_guessed(db_session):
    """Any positive threshold passes. Choosing the number is commercial.

    A validator that rejected "too generous" or defaulted a missing cap would
    silently enforce a figure nobody approved — worse than refusing outright.
    """
    offer = _offer(db_session, plan_family="home_flex")
    ensure_fup_policy(db_session, str(offer.id))
    add_fup_rule(
        db_session,
        str(offer.id),
        name="Absurdly generous but chosen by a human",
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=999_999,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=1,
    )
    db_session.commit()
    _check(db_session, offer)  # no raise


def test_a_withdrawn_offer_is_left_alone(db_session):
    """The rule is about what can be SOLD, not what exists."""
    offer = _offer(db_session, plan_family="home_flex", sellable=False)
    _check(db_session, offer, sellable=False)  # no raise


@pytest.mark.parametrize(
    "family", ["unlimited", "dedicated", "ip_block", "high_speed_data", None]
)
def test_other_families_are_untouched(db_session, family):
    """Only home_flex is in scope.

    ``dedicated`` correctly has twelve rule-less policies; ``unlimited`` is
    notify-only by design; ``high_speed_data`` is capped but its ladder shape
    is an open product decision for that segment, and asserting a requirement
    across segments is the cross-family inference plan_family exists to stop.
    """
    offer = _offer(db_session, plan_family=family)
    _check(db_session, offer)  # no raise


def test_high_speed_data_is_deliberately_out_of_scope():
    """Pinned so adding it becomes a decision rather than a drive-by edit."""
    assert FUP_REQUIRED_FAMILIES == ("home_flex",)


# --- keeping the invariant after the sale ------------------------------------
#
# The sale-time check establishes it; nothing kept it. A later deactivation
# returns the offer to exactly the state that check exists to prevent —
# sellable, capped in name, enforcing nothing — via an operator action that
# looks entirely routine.


def _throttle_rule(db, offer, *, name="Throttle"):
    ensure_fup_policy(db, str(offer.id))
    rule = add_fup_rule(
        db,
        str(offer.id),
        name=name,
        consumption_period="monthly",
        direction="up_down",
        threshold_amount=100,
        threshold_unit="gb",
        action="reduce_speed",
        speed_reduction_percent=50,
    )
    db.commit()
    return rule


def _delete(db, rule):
    # Read the id BEFORE releasing: touching an expired ORM attribute issues a
    # refresh SELECT, which reopens the transaction owner commands forbid.
    rule_id = str(rule.id)
    db_session_adapter.release_read_transaction(db)
    fup_policies.delete_rule(
        db,
        DeleteFupRuleCommand(
            context=fup_command_context(rule_id, "test_delete"),
            rule_id=rule_id,
        ),
    )


def _patch(db, rule, patch):
    rule_id = str(rule.id)
    db_session_adapter.release_read_transaction(db)
    fup_policies.update_rule(
        db,
        UpdateFupRuleCommand(
            context=fup_command_context(rule_id, "test_update"),
            rule_id=rule_id,
            patch=patch,
        ),
    )


def test_cannot_delete_the_last_enforcing_rule_while_on_sale(db_session):
    offer = _offer(db_session, plan_family="home_flex")
    rule = _throttle_rule(db_session, offer)
    with pytest.raises(FupRuleEngineError) as caught:
        _delete(db_session, rule)
    assert caught.value.code == "access.fup_rule_engine.last_enforcing_rule"


def test_cannot_deactivate_the_last_enforcing_rule_while_on_sale(db_session):
    offer = _offer(db_session, plan_family="home_flex")
    rule = _throttle_rule(db_session, offer)
    with pytest.raises(FupRuleEngineError):
        _patch(
            db_session,
            rule,
            FupRulePatch(updated_fields=frozenset({"is_active"}), is_active=False),
        )


def test_cannot_convert_the_last_enforcing_rule_to_notify(db_session):
    """Changing the action strips enforcement just as surely as deleting it."""
    offer = _offer(db_session, plan_family="home_flex")
    rule = _throttle_rule(db_session, offer)
    with pytest.raises(FupRuleEngineError):
        _patch(
            db_session,
            rule,
            FupRulePatch(updated_fields=frozenset({"action"}), action="notify"),
        )


def test_a_second_enforcing_rule_makes_the_first_removable(db_session):
    offer = _offer(db_session, plan_family="home_flex")
    first = _throttle_rule(db_session, offer, name="Throttle A")
    _throttle_rule(db_session, offer, name="Throttle B")
    _delete(db_session, first)  # no raise — enforcement remains


def test_withdrawing_from_sale_first_is_the_supported_route(db_session):
    """The guard is not a trap: there is always a way out, and it is correct."""
    offer = _offer(db_session, plan_family="home_flex")
    rule = _throttle_rule(db_session, offer)
    offer.available_for_services = False
    db_session.commit()
    _delete(db_session, rule)  # no raise


def test_other_families_can_still_remove_their_rules(db_session):
    offer = _offer(db_session, plan_family="high_speed_data")
    rule = _throttle_rule(db_session, offer)
    _delete(db_session, rule)  # no raise
