from __future__ import annotations

import uuid

import pytest

from app.api import support as support_api
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxChannelType,
    InboxContactLink,
    InboxConversation,
    InboxConversationParticipant,
    InboxParticipantAdmissionSource,
    InboxParticipantRelationship,
)
from app.schemas.team_inbox import InboxConversationContactLinkRequest
from app.services import (
    team_inbox_channel_receive,
    team_inbox_contact_links,
    team_inbox_customer_completion,
)
from app.services.owner_commands import CommandContext


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


def _link(
    db_session,
    *,
    conversation: InboxConversation,
    subscriber: Subscriber | None = None,
    reseller: Reseller | None = None,
    note: str | None = None,
):
    target_type = (
        team_inbox_contact_links.ContactLinkTargetType.subscriber
        if subscriber is not None
        else team_inbox_contact_links.ContactLinkTargetType.reseller
    )
    assert subscriber is not None or reseller is not None
    target_id = subscriber.id if subscriber is not None else reseller.id
    conversation_id = conversation.id
    db_session.commit()
    return team_inbox_contact_links.link_conversation_contact_by_id_committed(
        db_session,
        team_inbox_contact_links.LinkConversationContactCommand(
            context=CommandContext.system(
                actor="pytest",
                scope="team-inbox:contact-link",
                reason="focused contact-link test",
            ),
            conversation_id=conversation_id,
            target=team_inbox_contact_links.ContactLinkTarget(target_type, target_id),
            actor_person_id=None,
            source=team_inbox_contact_links.ContactLinkSource.manual_inbox_conversation,
            note=note,
        ),
    )


def test_link_conversation_contact_to_subscriber(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    historical = _conversation(db_session)
    historical.external_thread_id = "facebook_messenger:historical"

    result = _link(
        db_session,
        conversation=conversation,
        subscriber=subscriber,
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


def test_link_conversation_contact_rejects_inactive_customer(db_session):
    subscriber = _subscriber(db_session)
    subscriber.is_active = False
    conversation = _conversation(db_session)

    with pytest.raises(
        team_inbox_contact_links.ContactLinkError,
        match="Cannot link an inactive Customer",
    ):
        _link(
            db_session,
            conversation=conversation,
            subscriber=subscriber,
        )

    assert conversation.subscriber_id is None


def test_reviewed_contact_link_does_not_repair_a_different_contact(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    unrelated = _conversation(db_session, contact="999999999999999")

    result = _link(
        db_session,
        conversation=conversation,
        subscriber=subscriber,
        note="Confirmed by support",
    )

    assert result.repaired_conversation_ids == ()
    assert unrelated.subscriber_id is None


def test_representative_link_is_conversation_scoped_and_not_a_contact_route(
    db_session,
):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    other_conversation = _conversation(db_session)
    other_conversation.external_thread_id = "facebook_messenger:other-represented"
    participant = InboxConversationParticipant(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        normalized_endpoint=conversation.contact_address,
        provider_account_scope="default",
        admission_source=InboxParticipantAdmissionSource.inbound_from.value,
    )
    db_session.add(participant)
    db_session.flush()

    result = team_inbox_contact_links.associate_represented_customer(
        db_session,
        team_inbox_contact_links.AssociateRepresentedCustomerCommand(
            conversation_id=conversation.id,
            participant_id=participant.id,
            subscriber_id=subscriber.id,
            actor_person_id=uuid.uuid4(),
            reason="Calling for the account holder",
        ),
    )

    assert result.conversation_id == conversation.id
    assert result.participant_id == participant.id
    assert conversation.subscriber_id == subscriber.id
    assert (
        participant.relationship_type
        == InboxParticipantRelationship.representative.value
    )
    assert conversation.metadata_["contact_resolution"]["status"] == (
        "represented_customer"
    )
    assert db_session.query(InboxContactLink).count() == 0
    assert other_conversation.subscriber_id is None
    assert (
        team_inbox_customer_completion.classification(db_session, conversation).value
        == "customer"
    )


def test_link_conversation_contact_to_reseller(db_session):
    reseller = _reseller(db_session)
    conversation = _conversation(db_session, contact="17841400000000000")
    conversation.channel_type = InboxChannelType.instagram_dm.value
    conversation.external_thread_id = "instagram_dm:17841400000000000"

    result = _link(
        db_session,
        conversation=conversation,
        reseller=reseller,
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
    first_result = _link(
        db_session,
        conversation=conversation,
        subscriber=first,
    )

    second_result = _link(
        db_session,
        conversation=conversation,
        subscriber=second,
    )
    db_session.commit()

    old_link = db_session.get(InboxContactLink, first_result.contact_link_id)
    new_link = db_session.get(InboxContactLink, second_result.contact_link_id)
    assert old_link.is_active is False
    assert new_link.is_active is True
    assert second_result.previous_link_ids_deactivated == (
        first_result.contact_link_id,
    )
    assert conversation.subscriber_id == second.id
    assert (
        second_result.disposition
        is team_inbox_contact_links.ContactLinkDisposition.replaced
    )
    assert db_session.query(InboxContactLink).filter_by(is_active=True).count() == 1


def test_replacing_customer_route_with_reseller_clears_customer_projection(db_session):
    subscriber = _subscriber(db_session)
    reseller = _reseller(db_session)
    conversation = _conversation(db_session)
    _link(db_session, conversation=conversation, subscriber=subscriber)

    result = _link(db_session, conversation=conversation, reseller=reseller)

    assert result.subscriber_id is None
    assert result.reseller_id == reseller.id
    assert conversation.subscriber_id is None
    assert conversation.metadata_["contact_resolution"]["status"] == "linked_reseller"
    assert (
        result.disposition is team_inbox_contact_links.ContactLinkDisposition.replaced
    )


def test_link_conversation_contact_reuses_same_active_route(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)

    first_result = _link(
        db_session,
        conversation=conversation,
        subscriber=subscriber,
        note="Original reviewed evidence",
    )
    original_manual_evidence = dict(conversation.metadata_["manual_contact_link"])
    second_result = _link(
        db_session,
        conversation=conversation,
        subscriber=subscriber,
        note="A replay must not overwrite the original evidence",
    )

    assert second_result.contact_link_id == first_result.contact_link_id
    assert second_result.previous_link_ids_deactivated == ()
    assert (
        second_result.disposition
        is team_inbox_contact_links.ContactLinkDisposition.reused
    )
    assert second_result.replayed is True
    assert conversation.metadata_["manual_contact_link"] == original_manual_evidence
    assert db_session.query(InboxContactLink).count() == 1


def test_receive_social_message_uses_manual_contact_link(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    _link(
        db_session,
        conversation=conversation,
        subscriber=subscriber,
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
