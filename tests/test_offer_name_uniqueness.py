"""One name, one sellable offer.

Two production incidents in a single day came from the same root: a name is not
an identity, but every picker presents it as one. Two offers named
"25 Mbps Fiber" (N537,500 and N0.00) put two customers on unbilled dedicated
fibre; two named "Unlimited Pro" caused a live 50 Mbps plan to be withdrawn
from sale by a script matching on name.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
)
from app.services.web_catalog_offers import (
    OfferNameConflict,
    assert_sellable_name_is_unique,
)


def _offer(db, name, *, code=None, sellable=True, active=True):
    offer = CatalogOffer(
        name=name,
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        is_active=active,
        available_for_services=sellable,
    )
    db.add(offer)
    db.flush()
    return offer


def test_two_sellable_offers_cannot_share_a_name(db_session):
    _offer(db_session, "25 Mbps Fiber", code="25 Mbps Dedicated")

    with pytest.raises(OfferNameConflict) as caught:
        assert_sellable_name_is_unique(db_session, "25 Mbps Fiber")

    # A typed domain error, not an HTTPException: this module is a service and
    # the transport boundary is guarded by tests/architecture.
    assert caught.value.code == "catalog.offer.duplicate_sellable_name"
    # The operator needs to know WHICH offer collides, or they cannot resolve
    # it — the two rows are indistinguishable by the name they just typed.
    assert "25 Mbps Dedicated" in caught.value.message


def test_a_withdrawn_offer_may_keep_its_name(db_session):
    """Withdrawing one of a pair from sale resolves the collision without
    renaming or deleting anything — the retired row keeps its history."""
    _offer(db_session, "25 Mbps Fiber", code="old", sellable=False)

    assert_sellable_name_is_unique(db_session, "25 Mbps Fiber")


def test_an_inactive_offer_may_keep_its_name(db_session):
    _offer(db_session, "Unlimited Pro", code="archived-200m", active=False)

    assert_sellable_name_is_unique(db_session, "Unlimited Pro")


def test_an_offer_does_not_collide_with_itself_on_update(db_session):
    """Editing an unrelated field must not trip the check on the row's own
    name, or every offer becomes uneditable the moment this lands."""
    offer = _offer(db_session, "Unlimited Elite", code="UNL-ELITE")

    assert_sellable_name_is_unique(
        db_session, "Unlimited Elite", exclude_offer_id=str(offer.id)
    )


def test_a_blank_name_is_not_checked_here(db_session):
    """Emptiness is the create payload's problem; this check is about
    ambiguity between two real names."""
    assert_sellable_name_is_unique(db_session, "   ")


def test_the_database_refuses_the_collision_too(db_session):
    """The importer and direct SQL do not pass through the service, so the
    rule cannot live only there."""
    _offer(db_session, "Duplicate Plan", code="first")

    with pytest.raises(IntegrityError):
        _offer(db_session, "Duplicate Plan", code="second")
        db_session.flush()

    # A failed flush leaves the session unusable until it is rolled back.
    # Without this the poisoned session escapes into whichever test runs next,
    # which shows up as an unrelated failure somewhere else in the suite —
    # exactly the kind of order-dependent break that is miserable to trace.
    db_session.rollback()
