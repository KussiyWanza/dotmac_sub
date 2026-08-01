"""The primary marker: which held address RADIUS actually serves.

A service may legitimately hold several IPv4 addresses — the admin subscription
form allocates one per selected block. "Holds" and "is served on" are different
facts, and before this marker only the first had storage, so a consumer facing
two active assignments had to guess. These tests pin that the marker records the
answer, that the constraint permits holding several while forbidding two
primaries, and that an absent marker still fails closed rather than licensing a
guess.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.radius_population import _single_active_ipv4


def _subscription(db_session, catalog_offer, *, login, tag):
    subscriber = Subscriber(
        first_name="Primary", last_name="Case", email=f"{tag}@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        login=login,
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def _assign(db_session, sub, ip, *, primary=False, active=True):
    address = db_session.query(IPv4Address).filter_by(address=ip).one_or_none()
    if address is None:
        address = IPv4Address(address=ip)
        db_session.add(address)
        db_session.flush()
    assignment = IPAssignment(
        subscriber_id=sub.subscriber_id,
        subscription_id=sub.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=active,
        is_primary=primary,
    )
    db_session.add(assignment)
    db_session.flush()
    return assignment


def test_a_service_may_still_hold_several_addresses(db_session, catalog_offer):
    """The invariant must not delete the feature it was meant to protect."""
    sub = _subscription(db_session, catalog_offer, login="multi", tag="multi")

    _assign(db_session, sub, "172.16.20.1", primary=True)
    _assign(db_session, sub, "172.16.20.2")
    _assign(db_session, sub, "172.16.20.3")
    db_session.commit()

    held = (
        db_session.query(IPAssignment)
        .filter(IPAssignment.subscription_id == sub.id, IPAssignment.is_active)
        .all()
    )
    assert len(held) == 3
    assert sum(1 for a in held if a.is_primary) == 1


def test_two_active_primaries_are_forbidden(db_session, catalog_offer):
    sub = _subscription(db_session, catalog_offer, login="two", tag="two")
    _assign(db_session, sub, "172.16.21.1", primary=True)

    with pytest.raises(IntegrityError):
        _assign(db_session, sub, "172.16.21.2", primary=True)
    db_session.rollback()


def test_an_inactive_primary_does_not_block_a_new_one(db_session, catalog_offer):
    """Re-addressing a service must not be blocked by its own history."""
    sub = _subscription(db_session, catalog_offer, login="rehome", tag="rehome")
    _assign(db_session, sub, "172.16.22.1", primary=True, active=False)

    _assign(db_session, sub, "172.16.22.2", primary=True)
    db_session.commit()

    active_primary = (
        db_session.query(IPAssignment)
        .filter(
            IPAssignment.subscription_id == sub.id,
            IPAssignment.is_active,
            IPAssignment.is_primary,
        )
        .one()
    )
    assert active_primary.ipv4_address.address == "172.16.22.2"


def test_two_services_may_each_have_a_primary(db_session, catalog_offer):
    """The index is per service, not global."""
    first = _subscription(db_session, catalog_offer, login="a", tag="a")
    second = _subscription(db_session, catalog_offer, login="b", tag="b")

    _assign(db_session, first, "172.16.23.1", primary=True)
    _assign(db_session, second, "172.16.23.2", primary=True)
    db_session.commit()

    assert (
        db_session.query(IPAssignment)
        .filter(IPAssignment.is_active, IPAssignment.is_primary)
        .count()
        == 2
    )


def test_absent_marker_still_fails_closed_on_several_holdings():
    """The marker narrows ambiguity; it does not license a guess without one.

    A service holding several addresses with none marked is exactly the state
    the backfill refuses to resolve, so the projection must keep refusing too.
    """
    resolved, ambiguous = _single_active_ipv4({"172.16.24.1", "172.16.24.2"})

    assert resolved is None
    assert ambiguous == ("172.16.24.1", "172.16.24.2")


def test_absent_marker_resolves_a_lone_holding():
    """One holding is unambiguous whether or not anyone marked it."""
    resolved, ambiguous = _single_active_ipv4({"172.16.24.9"})

    assert resolved == "172.16.24.9"
    assert ambiguous == ()
