"""Recipient fields holding more than one address must still be deliverable.

Production stored several addresses in the single ``Notification.recipient``
column (``"a@x.com, b@y.com"``). That whole string went to ``sendmail`` as one
``RCPT TO``, the relay answered ``501 Syntax error in recipient address``, and
the customer received no billing notice at all while enforcement continued on
schedule.
"""

import pytest

from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.services import email as email_service
from app.services.customer_portal_contacts import validated_contact_email
from tests.mocks import FakeSMTP

resolve = email_service.resolve_recipient_addresses


# --- the resolver ----------------------------------------------------------


def test_single_address_is_unchanged():
    resolved = resolve("solo@example.com")
    assert resolved.deliverable == ("solo@example.com",)
    assert resolved.rejected == ()
    assert resolved.header_value == "solo@example.com"


def test_comma_separated_pair_splits():
    """The exact production shape that produced SMTP 501."""
    resolved = resolve("a@example.com, b@example.org")
    assert resolved.deliverable == ("a@example.com", "b@example.org")
    assert resolved.rejected == ()


@pytest.mark.parametrize(
    "raw",
    [
        "a@example.com,b@example.org",
        "a@example.com;b@example.org",
        "a@example.com,   b@example.org",
        "a@example.com\nb@example.org",
    ],
)
def test_separators_and_spacing_variants(raw):
    assert resolve(raw).deliverable == ("a@example.com", "b@example.org")


def test_comma_inside_a_quoted_display_name_is_not_a_separator():
    resolved = resolve('"Doe, John" <john@example.com>, b@example.org')
    assert resolved.deliverable == ("john@example.com", "b@example.org")


def test_invalid_parts_are_dropped_but_valid_ones_still_send():
    """A partially broken field must not cost the working address its notice."""
    resolved = resolve("good@example.com, not-an-address")
    assert resolved.deliverable == ("good@example.com",)
    assert resolved.rejected == ("not-an-address",)


def test_field_with_no_valid_address_yields_nothing_deliverable():
    resolved = resolve("nonsense, also-nonsense")
    assert resolved.deliverable == ()
    assert resolved.rejected == ("nonsense", "also-nonsense")


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_recipient_yields_nothing(raw):
    resolved = resolve(raw)
    assert resolved.deliverable == ()
    assert resolved.rejected == ()


def test_duplicates_collapse_case_insensitively_but_case_is_preserved():
    resolved = resolve("Person@Example.com, person@example.com")
    assert resolved.deliverable == ("Person@Example.com",)


def test_header_value_is_a_valid_multi_address_header():
    assert resolve("a@example.com,b@example.org").header_value == (
        "a@example.com, b@example.org"
    )


# --- the send paths --------------------------------------------------------


def _smtp(monkeypatch):
    fake = FakeSMTP()
    monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: fake)
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *a, **k: fake)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")
    return fake


def test_send_email_delivers_to_every_address_in_the_field(db_session, monkeypatch):
    fake = _smtp(monkeypatch)

    assert email_service.send_email(
        db=db_session,
        to_email="a@example.com, b@example.org",
        subject="Invoice",
        body_html="<p>Due</p>",
        track=False,
    )

    _from, recipients, message = fake.messages[0]
    assert recipients == ["a@example.com", "b@example.org"]
    assert "To: a@example.com, b@example.org" in message


def test_send_email_with_config_delivers_to_every_address(monkeypatch):
    fake = _smtp(monkeypatch)

    assert email_service.send_email_with_config(
        {
            "host": "smtp.selected.test",
            "port": 587,
            "from_email": "support@example.com",
            "from_name": "Support",
        },
        "a@example.com, b@example.org",
        "Invoice",
        "<p>Due</p>",
        cc_addresses=["copy@example.com"],
    )

    _from, recipients, _message = fake.messages[0]
    assert recipients == ["a@example.com", "b@example.org", "copy@example.com"]


def test_undeliverable_recipient_never_reaches_smtp(db_session, monkeypatch):
    fake = _smtp(monkeypatch)

    assert not email_service.send_email(
        db=db_session,
        to_email="not-an-address",
        subject="Invoice",
        body_html="<p>Due</p>",
        track=False,
    )
    assert fake.messages == []


def test_undeliverable_recipient_is_recorded_with_a_distinguishable_reason(
    db_session, monkeypatch
):
    """The failure must read as a data defect, not a transport blip."""
    _smtp(monkeypatch)
    notification = Notification(
        channel=NotificationChannel.email,
        recipient="broken-address",
        subject="Invoice",
        body=None,
        status=NotificationStatus.sending,
    )
    db_session.add(notification)
    db_session.commit()

    assert not email_service.send_email(
        db=db_session,
        to_email=notification.recipient,
        subject="Invoice",
        body_html="<p>Due</p>",
        track=False,
        notification_id=str(notification.id),
    )

    db_session.refresh(notification)
    assert notification.status == NotificationStatus.failed
    assert notification.last_error == email_service.NO_DELIVERABLE_RECIPIENT

    delivery = (
        db_session.query(NotificationDelivery)
        .filter(NotificationDelivery.notification_id == notification.id)
        .one()
    )
    assert delivery.response_code == email_service.NO_DELIVERABLE_RECIPIENT


# --- write-time validation -------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["a@example.com, b@example.org", "a@example.com;b@example.org"],
)
def test_contact_email_refuses_more_than_one_address(raw):
    with pytest.raises(ValueError, match="single email address"):
        validated_contact_email(raw)


def test_contact_email_refuses_a_malformed_address():
    with pytest.raises(ValueError):
        validated_contact_email("not-an-address")


def test_contact_email_accepts_a_single_address():
    assert validated_contact_email(" person@example.com ") == "person@example.com"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_contact_email_treats_blank_as_absent(raw):
    assert validated_contact_email(raw) is None
