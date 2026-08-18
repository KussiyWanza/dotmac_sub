from __future__ import annotations

import uuid

from app.api import support as support_api
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import InboxChannelType, InboxContactLink, InboxConversation
from app.schemas.team_inbox import InboxConversationContactLinkRequest
from app.services import team_inbox_channel_receive, team_inbox_contact_links


def _subscriber(db_session, *, email: str = "ada@example.com") -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email=email,
        phone="0803 555 0114",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _reseller(db_session, *, name: str = "Partner") -> Reseller:
    reseller = Reseller(
        name=name,
        code=name.lower().replace(" ", "-"),
        contact_email=f"{name.lower().replace(' ', '')}@example.com",
        is_active=True,
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def _conversation(db_session, *, contact: str = "123456789012345"):
    conversation = InboxConversation(
        channel_type=InboxChannelType.facebook_messenger.value,
        contact_address=contact,
        external_thread_id=f"facebook_messenger:{contact}",
        metadata_={"contact_resolution": {"status": "unmatched"}},
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_link_conversation_contact_to_subscriber(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    historical = _conversation(db_session)
    historical.external_thread_id = "facebook_messenger:historical"

    result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        conversation=conversation,
        subscriber_id=subscriber.id,
        note="Confirmed by support",
    )
    db_session.commit()

    link = db_session.get(InboxContactLink, result.contact_link_id)
    assert result.subscriber_id == subscriber.id
    assert result.reseller_id is None
    assert result.normalized_contact == "123456789012345"
    assert link.subscriber_id == subscriber.id
    assert link.is_active is True
    assert conversation.subscriber_id == subscriber.id
    assert conversation.metadata_["contact_resolution"][
        "manual_contact_link_id"
    ] == str(link.id)
    assert result.repaired_conversation_ids == (historical.id,)
    assert historical.subscriber_id == subscriber.id
    assert historical.metadata_["contact_resolution"][
        "repair_source_conversation_id"
    ] == str(conversation.id)


def test_reviewed_contact_link_does_not_repair_a_different_contact(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    unrelated = _conversation(db_session, contact="999999999999999")

    result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        conversation=conversation,
        subscriber_id=subscriber.id,
        note="Confirmed by support",
    )

    assert result.repaired_conversation_ids == ()
    assert unrelated.subscriber_id is None


def test_link_conversation_contact_to_reseller(db_session):
    reseller = _reseller(db_session)
    conversation = _conversation(db_session, contact="17841400000000000")
    conversation.channel_type = InboxChannelType.instagram_dm.value
    conversation.external_thread_id = "instagram_dm:17841400000000000"

    result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        conversation=conversation,
        reseller_id=reseller.id,
    )
    db_session.commit()

    link = db_session.get(InboxContactLink, result.contact_link_id)
    assert result.subscriber_id is None
    assert result.reseller_id == reseller.id
    assert link.reseller_id == reseller.id
    assert conversation.subscriber_id is None
    assert conversation.metadata_["contact_resolution"]["status"] == "linked_reseller"


def test_link_conversation_contact_replaces_active_link(db_session):
    first = _subscriber(db_session, email="first@example.com")
    second = _subscriber(db_session, email="second@example.com")
    conversation = _conversation(db_session)
    first_result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        conversation=conversation,
        subscriber_id=first.id,
    )

    second_result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        conversation=conversation,
        subscriber_id=second.id,
    )
    db_session.commit()

    old_link = db_session.get(InboxContactLink, first_result.contact_link_id)
    new_link = db_session.get(InboxContactLink, second_result.contact_link_id)
    assert old_link.is_active is False
    assert new_link.is_active is True
    assert second_result.previous_link_ids_deactivated == [first_result.contact_link_id]
    assert conversation.subscriber_id == second.id


def test_receive_social_message_uses_manual_contact_link(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    team_inbox_contact_links.link_conversation_contact(
        db_session,
        conversation=conversation,
        subscriber_id=subscriber.id,
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.facebook_messenger.value,
            contact_address="123456789012345",
            body="I am back",
            external_message_id="m_after_link",
        ),
    )
    db_session.commit()

    assert result.subscriber_id == str(subscriber.id)
    assert result.resolution_status == "linked_subscriber"


def test_support_api_links_inbox_conversation_contact(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    actor_id = uuid.uuid4()

    response = support_api.link_inbox_conversation_contact(
        conversation.id,
        InboxConversationContactLinkRequest(
            subscriber_id=subscriber.id,
            note="Matched from customer account",
        ),
        auth={"principal_id": actor_id},
        db=db_session,
    )

    link = db_session.get(InboxContactLink, response.contact_link_id)
    assert response.conversation_id == conversation.id
    assert response.subscriber_id == subscriber.id
    assert link.linked_by_person_id == actor_id
