"""Starting a conversation the operator initiates.

`team_inbox_outbound` could only reply to an existing thread, so "New
conversation" was a demo adapter. This opens the thread and sends its first
message in one command.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 4.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.models.subscriber import Subscriber
from app.models.team_inbox import InboxConversation, InboxMessage
from app.services import team_inbox_commands

OVERLAYS = Path("templates/admin/inbox/_overlays.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()


@pytest.fixture()
def customer(db_session):
    from app.services.subscriber import _default_reseller_id

    row = Subscriber(
        first_name="Known",
        last_name="Customer",
        email=f"known-{uuid.uuid4().hex}@example.com",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(row)
    db_session.flush()
    captured = (row.id, row.email)
    db_session.commit()
    return captured


def test_starting_a_conversation_opens_it_and_sends_the_first_message(db_session):
    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="email",
        contact_address="stranger@example.com",
        subject="Scheduled maintenance",
        body_text="We will be working on your line tomorrow.",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.status == "open"
    assert conversation.subject == "Scheduled maintenance"
    assert (conversation.metadata_ or {}).get("source") == "operator_initiated"

    messages = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .all()
    )
    assert [m.direction for m in messages] == ["outbound"]


def test_a_known_address_resolves_to_its_customer(db_session, customer):
    """A thread the operator starts must resolve like an inbound one would."""
    subscriber_id, email = customer

    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="email",
        contact_address=email,
        body_text="Following up on your report.",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.subscriber_id == subscriber_id


def test_an_unknown_address_still_opens_a_thread(db_session):
    """The operator may be reaching someone the system does not know yet."""
    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="email",
        contact_address="nobody@example.com",
        body_text="Hello.",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.subscriber_id is None
    # Recorded, so the drawer can offer a contact link instead of showing an
    # anonymous thread as though it were resolved.
    assert (conversation.metadata_ or {}).get("contact_resolution")
    assert outcome.contact_status


def test_a_missing_body_is_refused(db_session):
    with pytest.raises(team_inbox_commands.InboxCommandError):
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="someone@example.com",
            body_text="   ",
        )


def test_a_missing_contact_is_refused(db_session):
    with pytest.raises(team_inbox_commands.InboxCommandError):
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="  ",
            body_text="Hello.",
        )


def test_email_cc_and_bcc_are_normalized_and_stored(db_session):
    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="email",
        contact_address="primary@example.com",
        body_text="Hello.",
        cc_addresses=("COPY@example.com", "copy@example.com"),
        bcc_addresses=("audit@example.com",),
    )

    message = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == outcome.conversation_id)
        .one()
    )
    assert message.cc_addresses == ["copy@example.com"]
    assert message.metadata_["cc"] == ["copy@example.com"]
    assert message.metadata_["bcc"] == ["audit@example.com"]


@pytest.mark.parametrize("field", ["cc_addresses", "bcc_addresses"])
def test_invalid_email_copy_recipient_blocks_the_send(db_session, field):
    kwargs = {field: ("not-an-email",)}
    with pytest.raises(team_inbox_commands.InboxCommandError):
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="primary@example.com",
            body_text="Hello.",
            **kwargs,
        )
    assert db_session.query(InboxConversation).count() == 0


@pytest.mark.parametrize("field", ["cc_addresses", "bcc_addresses"])
def test_email_copy_recipient_limit_blocks_the_send(db_session, field):
    kwargs = {field: tuple(f"recipient-{index}@example.com" for index in range(21))}
    with pytest.raises(team_inbox_commands.InboxCommandError) as exc:
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="primary@example.com",
            body_text="Hello.",
            **kwargs,
        )
    assert "at most 20" in str(exc.value)
    assert db_session.query(InboxConversation).count() == 0


def test_email_recipient_form_parser_accepts_all_supported_separators():
    assert team_inbox_commands.split_email_recipients(
        "one@example.com; two@example.com,\nthree@example.com"
    ) == ("one@example.com", "two@example.com", "three@example.com")


@pytest.mark.parametrize(
    ("country", "local", "expected"),
    [
        ("NG", "08012345678", "+2348012345678"),
        ("GH", "0241234567", "+233241234567"),
        ("ZA", "0821234567", "+27821234567"),
        ("KE", "0712345678", "+254712345678"),
        ("GB", "07123456789", "+447123456789"),
        ("US", "02025550123", "+12025550123"),
    ],
)
def test_whatsapp_country_numbers_are_normalized(country, local, expected):
    assert (
        team_inbox_commands._normalize_whatsapp_recipient(local, country)  # noqa: SLF001
        == expected
    )


def test_whatsapp_start_requires_and_stores_an_approved_template(
    db_session, monkeypatch
):
    from app.services.integrations import whatsapp_capability

    monkeypatch.setattr(
        whatsapp_capability,
        "list_approved_templates",
        lambda _db: (
            {
                "name": "welcome_customer",
                "language": "en",
                "status": "APPROVED",
                "components": [],
            },
        ),
    )
    components = (
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Ada"}],
        },
    )
    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="whatsapp",
        contact_address="08012345678",
        contact_country_code="NG",
        body_text="Hello Ada",
        whatsapp_template_name="welcome_customer",
        whatsapp_template_language="en",
        whatsapp_template_components=components,
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    message = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .one()
    )
    assert conversation.contact_address == "+2348012345678"
    assert message.metadata_["whatsapp_template"] == {
        "name": "welcome_customer",
        "language": "en",
        "components": list(components),
        "variables": {},
        "inbox_template_id": None,
    }


def test_whatsapp_start_rejects_a_forged_template(db_session, monkeypatch):
    from app.services.integrations import whatsapp_capability

    monkeypatch.setattr(
        whatsapp_capability,
        "list_approved_templates",
        lambda _db: (),
    )
    with pytest.raises(team_inbox_commands.InboxCommandError) as exc:
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="whatsapp",
            contact_address="+2348012345678",
            body_text="Hello",
            whatsapp_template_name="not_approved",
            whatsapp_template_language="en",
        )
    assert "approved" in str(exc.value).lower()
    assert db_session.query(InboxConversation).count() == 0


def test_whatsapp_selected_contact_uses_its_phone_when_form_value_is_missing(
    db_session, monkeypatch
):
    from app.models.party import (
        Party,
        PartyContactPoint,
        PartyDataClassification,
        PartyIdentityStatus,
        PartyType,
    )
    from app.services.integrations import whatsapp_capability

    party = Party(
        party_type=PartyType.person.value,
        display_name="Ada Contact",
        status=PartyIdentityStatus.active.value,
        data_classification=PartyDataClassification.test.value,
    )
    db_session.add(party)
    db_session.flush()
    db_session.add(
        PartyContactPoint(
            party_id=party.id,
            channel_type="whatsapp",
            normalized_value="+2348012345678",
            display_value="0801 234 5678",
            is_primary=True,
        )
    )
    db_session.flush()
    monkeypatch.setattr(
        whatsapp_capability,
        "list_approved_templates",
        lambda _db: (
            {
                "name": "welcome_customer",
                "language": "en",
                "status": "APPROVED",
                "components": [],
            },
        ),
    )

    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="whatsapp",
        contact_address="",
        contact_party_id=party.id,
        body_text="Hello Ada",
        whatsapp_template_name="welcome_customer",
        whatsapp_template_language="en",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.contact_address == "+2348012345678"


def test_whatsapp_legacy_contact_requires_matching_active_customer(
    db_session, subscriber, monkeypatch
):
    from app.services.integrations import whatsapp_capability

    subscriber.phone = "09037423041"
    subscriber.is_active = True
    db_session.commit()
    monkeypatch.setattr(
        whatsapp_capability,
        "list_approved_templates",
        lambda _db: (
            {
                "name": "custom_message",
                "language": "en",
                "status": "APPROVED",
                "components": [],
            },
        ),
    )

    outcome = team_inbox_commands.start_conversation(
        db_session,
        channel_type="whatsapp",
        contact_address="+2349037423041",
        legacy_contact_subscriber_id=subscriber.id,
        body_text="Hello",
        whatsapp_template_name="custom_message",
        whatsapp_template_language="en",
    )

    conversation = db_session.get(InboxConversation, outcome.conversation_id)
    assert conversation.subscriber_id == subscriber.id
    assert outcome.contact_status == "explicit_subscriber"


def test_whatsapp_legacy_contact_rejects_subscriber_number_mismatch(
    db_session, subscriber, monkeypatch
):
    from app.services.integrations import whatsapp_capability

    subscriber.phone = "09037423041"
    subscriber.is_active = True
    db_session.commit()
    monkeypatch.setattr(
        whatsapp_capability,
        "list_approved_templates",
        lambda _db: (
            {
                "name": "custom_message",
                "language": "en",
                "status": "APPROVED",
                "components": [],
            },
        ),
    )

    with pytest.raises(team_inbox_commands.InboxCommandError) as exc:
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="whatsapp",
            contact_address="+2348000000000",
            legacy_contact_subscriber_id=subscriber.id,
            body_text="Hello",
            whatsapp_template_name="custom_message",
            whatsapp_template_language="en",
        )

    assert "no longer matches" in str(exc.value)


def test_an_unknown_channel_is_refused(db_session):
    with pytest.raises(team_inbox_commands.InboxCommandError) as exc:
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="carrier-pigeon",
            contact_address="someone@example.com",
            body_text="Hello.",
        )
    assert "channel" in str(exc.value).lower()


def test_no_conversation_survives_a_failed_first_send(db_session, monkeypatch):
    """A thread whose opening message never left is worse than none — the queue
    would show a conversation the customer never received."""
    from app.services import team_inbox_outbound

    def _fail(*args, **kwargs):
        return team_inbox_outbound.InboxReplyResult(
            kind="failed", conversation_id="x", reason="no sender configured"
        )

    monkeypatch.setattr(team_inbox_outbound, "send_inbox_reply", _fail)

    with pytest.raises(team_inbox_commands.InboxCommandRejected):
        team_inbox_commands.start_conversation(
            db_session,
            channel_type="email",
            contact_address="someone@example.com",
            body_text="Hello.",
        )

    assert db_session.query(InboxConversation).count() == 0


# --- surface ------------------------------------------------------------


def test_the_overlay_posts_a_real_form():
    assert "submitDemoConversation" not in OVERLAYS
    assert "submitDemoConversation" not in JAVASCRIPT
    assert 'action="/admin/inbox/conversations"' in OVERLAYS
    assert "Demo state" not in OVERLAYS
    assert "components/forms/csrf_input.html" in OVERLAYS


def test_the_form_states_how_an_unknown_contact_behaves():
    assert "still opens a thread you can link later" in OVERLAYS
