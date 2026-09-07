from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.models.ai_intake import AiIntakeSession
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import team_inbox_channel_receive


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _reseller(
    db_session,
    *,
    name: str = "Partner",
    phone: str | None = None,
) -> Reseller:
    reseller = Reseller(
        name=name,
        code=name.lower().replace(" ", "-"),
        contact_phone=phone,
        is_active=True,
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def _subscriber(
    db_session,
    *,
    phone: str,
    email: str,
    reseller: Reseller | None = None,
    status: SubscriberStatus = SubscriberStatus.active,
    is_active: bool = True,
) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email=email,
        phone=phone,
        status=status,
        is_active=is_active,
        reseller_id=reseller.id if reseller else None,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_receive_whatsapp_links_single_active_subscriber_and_reseller(db_session):
    team = _team(db_session)
    reseller = _reseller(db_session, name="North Partner")
    subscriber = _subscriber(
        db_session,
        phone="0803 555 0114",
        email="ada@example.com",
        reseller=reseller,
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="whatsapp:+2348035550114",
            body="My service is down",
            external_message_id="wamid-1",
            fallback_service_team_id=team.id,
            received_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    resolution = conversation.metadata_["contact_resolution"]
    assert result.kind == "received"
    assert result.subscriber_id == str(subscriber.id)
    assert result.reseller_id == str(reseller.id)
    assert result.resolution_status == "linked_subscriber"
    assert conversation.subscriber_id == subscriber.id
    assert conversation.primary_service_team_id == team.id
    assert conversation.contact_address == "+2348035550114"
    assert message.from_address == "+2348035550114"
    assert message.metadata_["contact_resolution"]["subscriber_id"] == str(
        subscriber.id
    )
    assert resolution["reseller_id"] == str(reseller.id)


def test_receive_whatsapp_records_ambiguous_shared_phone_without_guessing(db_session):
    _subscriber(
        db_session,
        phone="0803 555 0114",
        email="ada@example.com",
    )
    _subscriber(
        db_session,
        phone="+2348035550114",
        email="shared@example.com",
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="08035550114",
            body="Who owns this?",
            external_message_id="wamid-ambiguous",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    resolution = conversation.metadata_["contact_resolution"]
    assert result.subscriber_id is None
    assert result.resolution_status == "ambiguous"
    assert conversation.subscriber_id is None
    assert len(resolution["matched_subscriber_ids"]) == 2


def test_receive_whatsapp_suppresses_disabled_or_canceled_matches(db_session):
    disabled = _subscriber(
        db_session,
        phone="0803 555 0114",
        email="disabled@example.com",
        status=SubscriberStatus.disabled,
    )
    canceled = _subscriber(
        db_session,
        phone="+2348035550114",
        email="canceled@example.com",
        status=SubscriberStatus.canceled,
    )
    inactive = _subscriber(
        db_session,
        phone="+2348035550114",
        email="inactive@example.com",
        is_active=False,
    )
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="08035550114",
            body="Please reactivate me",
            external_message_id="wamid-suppressed",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    resolution = conversation.metadata_["contact_resolution"]
    assert result.subscriber_id is None
    assert conversation.subscriber_id is None
    assert set(resolution["suppressed_subscriber_ids"]) == {
        str(disabled.id),
        str(canceled.id),
        str(inactive.id),
    }


def test_receive_whatsapp_links_reseller_contact_without_subscriber(db_session):
    reseller = _reseller(db_session, name="VIP Reseller", phone="0808 111 2222")
    db_session.commit()

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="08081112222",
            body="One of my customers is down",
            external_message_id="wamid-reseller",
        ),
    )
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert result.subscriber_id is None
    assert result.reseller_id == str(reseller.id)
    assert result.resolution_status == "linked_reseller"
    assert conversation.metadata_["contact_resolution"]["reseller_id"] == str(
        reseller.id
    )


def test_receive_whatsapp_webhook_normalizes_and_deduplicates(db_session):
    first = team_inbox_channel_receive.receive_whatsapp_webhook(
        db_session,
        provider="meta_cloud_api",
        payload={
            "message": {
                "from": "2348012345678",
                "text": "Hello",
                "id": "wamid-1",
            },
            "contact_name": "Amina Customer",
        },
    )
    second = team_inbox_channel_receive.receive_whatsapp_webhook(
        db_session,
        provider="meta_cloud_api",
        payload={
            "message": {
                "from": "2348012345678",
                "text": "Hello again",
                "id": "wamid-1",
            },
        },
    )
    db_session.commit()

    message = db_session.get(InboxMessage, first.message_id)
    assert first.kind == "received"
    assert second.kind == "duplicate"
    assert second.conversation_id == first.conversation_id
    assert message.channel_type == InboxChannelType.whatsapp.value
    assert message.from_address == "+2348012345678"
    assert message.body == "Hello"
    conversation = db_session.get(InboxConversation, first.conversation_id)
    assert conversation.metadata_["contact_name"] == "Amina Customer"
    assert conversation.metadata_["contact_name_source"] == "provider_observation"


def test_recent_ai_context_excludes_private_notes_and_preserves_roles(db_session):
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        status="pending",
        contact_address="+2348012345678",
        external_thread_id=f"thread-{uuid4()}",
        metadata_={},
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add_all(
        [
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=conversation.channel_type,
                direction=InboxMessageDirection.inbound.value,
                body="Customer statement",
                metadata_={},
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=conversation.channel_type,
                direction=InboxMessageDirection.internal.value,
                body="Private diagnosis that must never reach the model",
                metadata_={"source": "agent_note"},
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=conversation.channel_type,
                direction=InboxMessageDirection.outbound.value,
                body="AI question",
                metadata_={"sender_type": "ai"},
            ),
            InboxMessage(
                conversation_id=conversation.id,
                channel_type=conversation.channel_type,
                direction=InboxMessageDirection.outbound.value,
                body="Human reply",
                metadata_={"sent_by_person_id": str(uuid4())},
            ),
        ]
    )
    db_session.flush()

    context = team_inbox_channel_receive._recent_intake_context(
        db_session, conversation_id=conversation.id
    )

    assert [item.role.value for item in context] == [
        "customer",
        "ai",
        "human_agent",
    ]
    assert "Private diagnosis" not in " ".join(item.body for item in context)


def test_media_only_first_contact_uses_live_ingress_handoff_path(
    db_session, monkeypatch
):
    team = _team(db_session)
    policy_id = uuid4()
    version_id = uuid4()
    sent: list[str] = []
    assigned: list[str] = []
    monkeypatch.setattr(
        team_inbox_channel_receive.ai_conversation_intake,
        "resolve_media_first_handoff_policy",
        lambda *_args, **_kwargs: (
            team_inbox_channel_receive.ai_conversation_intake.AiMediaFirstHandoffPolicy(
                policy_id=policy_id,
                policy_version_id=version_id,
                display_name="Dotmac Support",
                customer_message="I cannot inspect this attachment, so support will review it.",
                fallback_team_id=team.id,
            )
        ),
    )

    def _send(*_args, **kwargs):
        sent.append(kwargs["body_text"])
        return SimpleNamespace(kind="queued", message_id=str(uuid4()))

    def _assign(*_args, **kwargs):
        assigned.append(str(kwargs["service_team_id"]))
        return SimpleNamespace(kind="queued")

    monkeypatch.setattr(
        team_inbox_channel_receive.team_inbox_outbound,
        "send_ai_intake_message",
        _send,
    )
    monkeypatch.setattr(
        team_inbox_channel_receive.team_inbox_assignment,
        "assign_conversation_to_available_agent",
        _assign,
    )

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="+2348012345678",
            body="",
            external_message_id=f"media-{uuid4()}",
            fallback_service_team_id=team.id,
            metadata={
                "provider": "meta_cloud_api",
                "provider_account_scope": "phone-1",
                "attachments": [
                    {
                        "type": "image",
                        "mime_type": "image/jpeg",
                        "provider_media_id": "media-1",
                    }
                ],
            },
        ),
    )

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    notes = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.internal.value)
        .all()
    )
    assets = (
        db_session.query(InboxMediaAsset)
        .filter(InboxMediaAsset.message_id == message.id)
        .all()
    )
    assert message.body == ""
    assert len(assets) == 1
    assert sent == ["I cannot inspect this attachment, so support will review it."]
    assert assigned == [str(team.id)]
    assert len(notes) == 1
    assert db_session.query(AiIntakeSession).count() == 0
    assert conversation.metadata_["ai_handling"] is False
    assert conversation.metadata_["ai_intake"]["reason"] == ("media_only_first_message")


def test_captioned_media_follows_normal_text_intake(db_session, monkeypatch):
    monkeypatch.setattr(
        team_inbox_channel_receive.ai_conversation_intake,
        "resolve_media_first_handoff_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("captioned media must not use media-only handoff")
        ),
    )

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="+2348012349999",
            body="My internet is not browsing.",
            external_message_id=f"captioned-{uuid4()}",
            metadata={
                "provider": "meta_cloud_api",
                "provider_account_scope": "phone-1",
                "attachments": [{"type": "image", "provider_media_id": "media-2"}],
            },
        ),
    )

    message = db_session.get(InboxMessage, result.message_id)
    assert message.body == "My internet is not browsing."
