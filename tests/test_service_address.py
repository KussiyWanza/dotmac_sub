"""Tests for the canonical subscriber service-address resolver."""

from __future__ import annotations

from app.models.subscriber import Address, AddressType
from app.services.service_address import (
    address_parts,
    format_address,
    pick_service_address,
    service_address,
)


def test_format_address_joins_populated_canonical_parts():
    address = Address(
        subscriber_id=None,
        address_line1="12 Fiber Close",
        city="Abuja",
        region="FCT",
        country_code="NG",
    )

    assert format_address(address) == "12 Fiber Close, Abuja, FCT, NG"
    assert format_address(None) is None


def _add(db, subscriber_id, **kw):
    address = Address(subscriber_id=subscriber_id, address_line1="x", **kw)
    db.add(address)
    db.commit()
    return address


class TestServiceAddress:
    def test_none_when_no_address(self, db_session, subscriber):
        assert service_address(db_session, subscriber.id) is None

    def test_primary_service_address_wins(self, db_session, subscriber):
        _add(
            db_session, subscriber.id, address_type=AddressType.billing, is_primary=True
        )
        want = _add(
            db_session,
            subscriber.id,
            address_type=AddressType.service,
            is_primary=True,
        )
        assert service_address(db_session, subscriber.id).id == want.id

    def test_falls_back_to_any_address(self, db_session, subscriber):
        want = _add(
            db_session,
            subscriber.id,
            address_type=AddressType.service,
            is_primary=False,
        )
        assert service_address(db_session, subscriber.id).id == want.id


class TestPickServiceAddress:
    def test_none_for_empty(self):
        assert pick_service_address(None) is None
        assert pick_service_address([]) is None

    def test_prefers_primary_service(self):
        billing = Address(
            subscriber_id=None,
            address_line1="b",
            address_type=AddressType.billing,
            is_primary=True,
        )
        service = Address(
            subscriber_id=None,
            address_line1="s",
            address_type=AddressType.service,
            is_primary=True,
        )
        assert pick_service_address([billing, service]) is service

    def test_returns_only_address(self):
        only = Address(
            subscriber_id=None,
            address_line1="x",
            address_type=AddressType.service,
            is_primary=False,
        )
        assert pick_service_address([only]) is only


class TestAddressParts:
    def test_prefers_canonical_address(self, db_session, subscriber):
        _add(
            db_session,
            subscriber.id,
            address_type=AddressType.service,
            is_primary=True,
            city="AddrCity",
        )
        db_session.refresh(subscriber)
        assert address_parts(subscriber).city == "AddrCity"

    def test_falls_back_to_inline(self, db_session, subscriber):
        subscriber.city = "InlineCity"
        db_session.commit()
        db_session.refresh(subscriber)
        assert address_parts(subscriber).city == "InlineCity"
