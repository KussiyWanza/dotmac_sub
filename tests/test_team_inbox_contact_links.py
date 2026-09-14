from __future__ import annotations

import uuid

from app.api import support as support_api
from app.models.party import Party, PartyContactPoint, PartyRelationship, PartyType
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxChannelType,
    InboxContactLink,
    InboxConversation,
    InboxConversationParticipant,
    InboxMessage,
    InboxMessageDirection,
)
from app.schemas.team_inbox import InboxConversationContactLinkRequest
from app.services import team_inbox_channel_receive, team_inbox_contact_links


def _subscriber(db_session, *, email: str = "ada@example.com") -> Subscriber:
    party = Party(party_type=PartyType.person.value, display_name="Ada Nwosu")
    db_session.add(party)
    db_session.flush()
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email=email,
        phone="0803 555 0114",
        status=SubscriberStatus.active,
        is_active=True,
        party_id=party.id,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _reseller(db_session, *, name: str = "Partner") -> Reseller:
    party = Party(party_type=PartyType.organization.value, display_name=name)
    db_session.add(party)
    db_session.flush()
    reseller = Reseller(
        name=name,
        code=name.lower().replace(" ", "-"),
        contact_email=f"{name.lower().replace(' ', '')}@example.com",
        is_active=True,
        party_id=party.id,
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def _conversation(db_session, *, contact: str = "123456789012345"):
    conversation = InboxConversation(
        channel_type=InboxChannelType.facebook_messenger.value,
        contact_address=contact,
        external_thread_id=f"facebook_messenger:{contact}",
        metadata_={
            "contact_resolution": {"status": "unmatched"},
            "provider_identity": {
                "provider": "meta_social",
                "provider_account_id": "page-a",
                "external_subject_id": contact,
            },
        },
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _review(
    conversation: InboxConversation,
    *,
    subscriber_id=None,
    reseller_id=None,
):
    return team_inbox_contact_links.ReviewConversationContactCommand(
        conversation_id=conversation.id,
        identity_kind=(
            team_inbox_contact_links.ReviewedContactIdentityKind.customer
            if subscriber_id
            else team_inbox_contact_links.ReviewedContactIdentityKind.reseller
        ),
        subscriber_id=subscriber_id,
        reseller_id=reseller_id,
        note="Confirmed by support",
    )


def test_link_conversation_contact_to_subscriber(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    historical = _conversation(db_session)
    historical.external_thread_id = "facebook_messenger:historical"

    result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        _review(conversation, subscriber_id=subscriber.id),
    )
    db_session.commit()

    link = db_session.get(InboxContactLink, result.contact_link_id)
    assert result.subscriber_id == subscriber.id
    assert result.reseller_id is None
    assert result.normalized_contact == "123456789012345"
    assert link.subscriber_id == subscriber.id
    assert link.is_active is True
    assert db_session.get(
        PartyContactPoint, result.party_contact_point_id
    ).party_id == (subscriber.party_id)
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
        _review(conversation, subscriber_id=subscriber.id),
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
        _review(conversation, reseller_id=reseller.id),
    )
    db_session.commit()

    link = db_session.get(InboxContactLink, result.contact_link_id)
    assert result.subscriber_id is None
    assert result.reseller_id == reseller.id
    assert link.reseller_id == reseller.id
    assert conversation.subscriber_id is None
    assert conversation.metadata_["contact_resolution"]["status"] == "linked_reseller"


def test_link_conversation_contact_preserves_existing_reviewed_owner(db_session):
    first = _subscriber(db_session, email="first@example.com")
    second = _subscriber(db_session, email="second@example.com")
    conversation = _conversation(db_session)
    first_result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        _review(conversation, subscriber_id=first.id),
    )

    second_result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        _review(conversation, subscriber_id=second.id),
    )
    db_session.commit()

    old_link = db_session.get(InboxContactLink, first_result.contact_link_id)
    assert second_result.contact_link_id == first_result.contact_link_id
    assert second_result.disposition.value == "conflict"
    assert old_link.is_active is True
    assert old_link.subscriber_id == first.id
    assert second_result.previous_link_ids_deactivated == ()
    assert conversation.subscriber_id == first.id


def test_receive_social_message_uses_manual_contact_link(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    team_inbox_contact_links.link_conversation_contact(
        db_session,
        _review(conversation, subscriber_id=subscriber.id),
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.facebook_messenger.value,
            contact_address="123456789012345",
            body="I am back",
            external_message_id="m_after_link",
            metadata={
                "provider": "meta_social",
                "provider_account_scope": "page-a",
            },
        ),
    )
    db_session.commit()

    assert result.subscriber_id == str(subscriber.id)
    assert result.resolution_status == "linked_subscriber"


def test_same_social_subject_isolated_by_provider_account(db_session):
    first = _subscriber(db_session, email="first-social@example.com")
    second = _subscriber(db_session, email="second-social@example.com")
    first_conversation = _conversation(db_session, contact="same-subject")
    second_conversation = _conversation(db_session, contact="same-subject")
    second_conversation.external_thread_id = "facebook_messenger:page-b:same-subject"
    second_conversation.metadata_["provider_identity"] = {
        "provider": "meta_social",
        "provider_account_id": "page-b",
        "external_subject_id": "same-subject",
    }

    first_link = team_inbox_contact_links.link_conversation_contact(
        db_session,
        _review(first_conversation, subscriber_id=first.id),
    )
    second_link = team_inbox_contact_links.link_conversation_contact(
        db_session,
        _review(second_conversation, subscriber_id=second.id),
    )
    db_session.flush()

    assert first_link.contact_link_id != second_link.contact_link_id
    assert (
        db_session.get(InboxContactLink, first_link.contact_link_id).provider_account_id
        == "page-a"
    )
    assert (
        db_session.get(
            InboxContactLink, second_link.contact_link_id
        ).provider_account_id
        == "page-b"
    )


def test_one_customer_keeps_multiple_reviewed_social_identities(db_session):
    subscriber = _subscriber(db_session, email="multi-social@example.com")
    first = _conversation(db_session, contact="ig-subject-a")
    first.channel_type = InboxChannelType.instagram_dm.value
    first.metadata_["provider_identity"] = {
        "provider": "meta_social",
        "provider_account_id": "ig-business-a",
        "external_subject_id": "ig-subject-a",
    }
    second = _conversation(db_session, contact="ig-subject-b")
    second.channel_type = InboxChannelType.instagram_dm.value
    second.metadata_["provider_identity"] = {
        "provider": "meta_social",
        "provider_account_id": "ig-business-a",
        "external_subject_id": "ig-subject-b",
    }

    first_result = team_inbox_contact_links.link_conversation_contact(
        db_session, _review(first, subscriber_id=subscriber.id)
    )
    second_result = team_inbox_contact_links.link_conversation_contact(
        db_session, _review(second, subscriber_id=subscriber.id)
    )
    points = (
        db_session.query(PartyContactPoint)
        .filter(PartyContactPoint.party_id == subscriber.party_id)
        .all()
    )

    assert first_result.party_contact_point_id != second_result.party_contact_point_id
    assert {point.external_subject_id for point in points} == {
        "ig-subject-a",
        "ig-subject-b",
    }


def test_reviewed_representative_identity_remains_separate_and_reusable(db_session):
    subscriber = _subscriber(db_session, email="represented@example.com")
    conversation = _conversation(db_session, contact="representative-psid")
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        direction=InboxMessageDirection.inbound.value,
        body="I am contacting you for the customer",
    )
    db_session.add(message)
    db_session.flush()

    result = team_inbox_contact_links.link_conversation_contact(
        db_session,
        team_inbox_contact_links.ReviewConversationContactCommand(
            conversation_id=conversation.id,
            identity_kind=(
                team_inbox_contact_links.ReviewedContactIdentityKind.representative
            ),
            subscriber_id=subscriber.id,
            representative_name="Chinedu Okoro",
            representative_role="IT manager",
        ),
    )
    point = db_session.get(PartyContactPoint, result.party_contact_point_id)
    participant = db_session.query(InboxConversationParticipant).one()
    relationship = db_session.query(PartyRelationship).one()

    assert conversation.subscriber_id == subscriber.id
    assert result.speaking_party_id != subscriber.party_id
    assert point.party_id == result.speaking_party_id
    assert participant.party_contact_point_id == point.id
    assert relationship.subject_party_id == result.speaking_party_id
    assert relationship.object_party_id == subscriber.party_id


def test_representative_identity_conflict_does_not_create_an_orphan_party(db_session):
    subscriber = _subscriber(db_session, email="represented-conflict@example.com")
    conversation = _conversation(db_session, contact="representative-conflict")
    first = team_inbox_contact_links.link_conversation_contact(
        db_session,
        team_inbox_contact_links.ReviewConversationContactCommand(
            conversation_id=conversation.id,
            identity_kind=(
                team_inbox_contact_links.ReviewedContactIdentityKind.representative
            ),
            subscriber_id=subscriber.id,
            representative_name="Chinedu Okoro",
        ),
    )
    party_count = db_session.query(Party).count()

    conflict = team_inbox_contact_links.link_conversation_contact(
        db_session,
        team_inbox_contact_links.ReviewConversationContactCommand(
            conversation_id=conversation.id,
            identity_kind=(
                team_inbox_contact_links.ReviewedContactIdentityKind.representative
            ),
            subscriber_id=subscriber.id,
            representative_name="A Different Person",
        ),
    )

    assert conflict.disposition.value == "conflict"
    assert conflict.contact_link_id == first.contact_link_id
    assert db_session.query(Party).count() == party_count


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
