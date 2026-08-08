"""A capability may only be delivered to one authorised address.

Customer communications and capability delivery answer different questions.
A billing notice should reach the account holder and their nominated contacts;
a credential link must reach exactly one authorised mailbox, because an extra
recipient is an authorisation decision.

Nothing owned that distinction, so the recipient set was decided by the
transport splitter — which exists to fan customer communications out. Thirty
production accounts hold two addresses in `subscribers.email`, and every one of
them has an active credential, so credential links were reaching two mailboxes.
"""

from __future__ import annotations

import pytest

from app.services.capability_recipient import (
    CapabilityRecipientError,
    resolve_capability_recipient,
)

SUBJECT = "subscriber:11111111-1111-1111-1111-111111111111"


def test_a_single_address_resolves():
    assert resolve_capability_recipient("solo@example.com", subject=SUBJECT) == (
        "solo@example.com"
    )


def test_surrounding_whitespace_is_tolerated():
    assert resolve_capability_recipient("  solo@example.com  ", subject=SUBJECT) == (
        "solo@example.com"
    )


@pytest.mark.parametrize(
    "value",
    [
        "a@example.com, b@example.org",
        "a@example.com;b@example.org",
        "a@example.com, b@example.org, c@example.net",
    ],
)
def test_more_than_one_address_refuses_rather_than_guessing(value):
    """Picking the first would be a silent guess about who is authorised."""
    with pytest.raises(CapabilityRecipientError) as exc:
        resolve_capability_recipient(value, subject=SUBJECT)

    assert exc.value.code.endswith("ambiguous_address")
    assert exc.value.details["subject"] == SUBJECT


def test_one_deliverable_address_beside_an_unparseable_one_resolves():
    """An unparseable fragment is not a competing recipient.

    Refusing here would be the safer-sounding choice and the worse one: it locks
    the only reachable person out of their own password reset because someone
    typed a second address badly. Ambiguity means two candidates who could each
    receive the capability, and a fragment that cannot be sent to is not one.
    """
    assert (
        resolve_capability_recipient(
            "good@example.com, not-an-address", subject=SUBJECT
        )
        == "good@example.com"
    )


@pytest.mark.parametrize("value", [None, "", "   ", "not-an-address"])
def test_no_deliverable_address_refuses(value):
    with pytest.raises(CapabilityRecipientError) as exc:
        resolve_capability_recipient(value, subject=SUBJECT)

    assert exc.value.code.endswith("no_deliverable_address")


def test_the_refusal_names_the_record_to_correct():
    with pytest.raises(CapabilityRecipientError) as exc:
        resolve_capability_recipient("a@example.com, b@example.org", subject=SUBJECT)

    assert exc.value.details["subject"] == SUBJECT
    assert exc.value.details["address_count"] == 2


def test_a_refusal_is_logged_with_the_record_to_correct(caplog):
    """Failing closed is silent on its own; these records need correcting."""
    with caplog.at_level("WARNING"), pytest.raises(CapabilityRecipientError):
        resolve_capability_recipient("a@example.com, b@example.org", subject=SUBJECT)

    logged = caplog.text
    assert "capability_recipient_unresolved" in logged
    assert "reason=ambiguous_address" in logged
    assert SUBJECT in logged
