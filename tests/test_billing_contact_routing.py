"""Billing communications honour a designated billing contact, when there is one.

`SubscriberContact` has carried a `contact_type` and an `is_billing_contact`
flag that nothing read: every contact with `receives_notifications` received
everything, so a contact typed *technical* got billing notices and naming a
billing contact selected nobody.

Roles are honoured only where the account expressed a preference. Switching this
on must not quietly narrow an account that never designated anyone — 26 of the
30 contacts in production are untyped `general`, and they should keep receiving
exactly what they receive today.
"""

from __future__ import annotations

from app.models.notification import NotificationChannel
from app.models.subscriber import SubscriberContact
from app.services.communication_intents import _subscriber_addresses


def _contact(db_session, subscriber, email, *, billing: bool) -> SubscriberContact:
    contact = SubscriberContact(
        subscriber_id=subscriber.id,
        full_name=email,
        email=email,
        receives_notifications=True,
        is_billing_contact=billing,
    )
    db_session.add(contact)
    db_session.commit()
    return contact


def test_billing_goes_only_to_the_designated_billing_contact(db_session, subscriber):
    _contact(db_session, subscriber, "billing@example.com", billing=True)
    _contact(db_session, subscriber, "technical@example.com", billing=False)

    addresses = _subscriber_addresses(
        db_session, subscriber, NotificationChannel.email, "billing"
    )

    assert "billing@example.com" in addresses
    assert "technical@example.com" not in addresses


def test_the_account_holder_is_always_included(db_session, subscriber):
    """Narrowing contacts must never drop the person who owns the account."""
    _contact(db_session, subscriber, "billing@example.com", billing=True)

    addresses = _subscriber_addresses(
        db_session, subscriber, NotificationChannel.email, "billing"
    )

    assert subscriber.email in addresses


def test_nothing_narrows_when_no_billing_contact_is_designated(db_session, subscriber):
    """The grandfather case: an account that never expressed a preference."""
    _contact(db_session, subscriber, "one@example.com", billing=False)
    _contact(db_session, subscriber, "two@example.com", billing=False)

    addresses = _subscriber_addresses(
        db_session, subscriber, NotificationChannel.email, "billing"
    )

    assert "one@example.com" in addresses
    assert "two@example.com" in addresses


def test_non_billing_categories_still_reach_every_notification_contact(
    db_session, subscriber
):
    """A billing designation must not remove someone from service messages."""
    _contact(db_session, subscriber, "billing@example.com", billing=True)
    _contact(db_session, subscriber, "technical@example.com", billing=False)

    addresses = _subscriber_addresses(
        db_session, subscriber, NotificationChannel.email, "service"
    )

    assert "billing@example.com" in addresses
    assert "technical@example.com" in addresses


def test_an_absent_category_behaves_as_before(db_session, subscriber):
    _contact(db_session, subscriber, "billing@example.com", billing=True)
    _contact(db_session, subscriber, "technical@example.com", billing=False)

    addresses = _subscriber_addresses(db_session, subscriber, NotificationChannel.email)

    assert "billing@example.com" in addresses
    assert "technical@example.com" in addresses
