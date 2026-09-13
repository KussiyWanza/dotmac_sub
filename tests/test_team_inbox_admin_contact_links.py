from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import BackgroundTasks
from starlette.requests import Request

from app.models.notification import Notification, NotificationStatus
from app.models.party import Party, PartyType
from app.models.sales import Lead
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxChannelType,
    InboxContactLink,
    InboxConversation,
    InboxConversationLeadLink,
    InboxConversationParticipant,
    InboxConversationStatus,
    InboxLabel,
    InboxMessage,
    InboxMessageDirection,
    InboxMessageTemplate,
    InboxParticipantAdmissionSource,
    InboxParticipantRelationship,
    InboxReplyMacro,
)
from app.services import team_inbox_operations, team_inbox_projection, team_inbox_read
from app.web.admin import inbox as inbox_web
from tests.staff_identity_fixtures import add_bound_staff_user


def _request() -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/"}, receive)
    request.state.user = SimpleNamespace(id=uuid.uuid4())
    return request


def _subscriber(db_session, *, first_name: str = "Ada") -> Subscriber:
    subscriber = Subscriber(
        first_name=first_name,
        last_name="Nwosu",
        display_name=f"{first_name} Nwosu",
        email=f"{first_name.lower()}@example.com",
        phone="0803 555 0114",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _reseller(db_session, *, name: str = "North Partner") -> Reseller:
    reseller = Reseller(
        name=name,
        code="north",
        contact_email="north@example.com",
        is_active=True,
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def _conversation(db_session, *, subject: str = "Ada needs help") -> InboxConversation:
    conversation = InboxConversation(
        channel_type=InboxChannelType.facebook_messenger.value,
        subject=subject,
        contact_address="psid-123",
        external_thread_id="facebook_messenger:psid-123",
        metadata_={"contact_resolution": {"status": "unmatched"}},
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _online_reply_agent(db_session, conversation: InboxConversation):
    team = ServiceTeam(
        name=f"Reply Team {uuid.uuid4().hex[:8]}",
        team_type=ServiceTeamType.support.value,
    )
    db_session.add(team)
    db_session.flush()
    user, person = add_bound_staff_user(db_session)
    db_session.add_all(
        [
            ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True),
            InboxAgentPresence(
                person_id=user.id,
                status="online",
                manual_override_status="online",
                last_seen_at=datetime.now(UTC),
            ),
        ]
    )
    conversation.primary_service_team_id = team.id
    db_session.flush()
    return user


def test_admin_contact_link_candidates_match_timeline_context(db_session):
    subscriber = _subscriber(db_session, first_name="Ada")
    reseller = _reseller(db_session, name="Ada Partner")
    conversation = _conversation(db_session, subject="Ada social message")
    timeline = team_inbox_read.get_conversation_timeline(db_session, conversation.id)

    candidates = team_inbox_projection.contact_link_candidates(db_session, timeline)

    assert candidates.subscribers[0].id == str(subscriber.id)
    assert candidates.resellers[0].id == str(reseller.id)


def test_admin_contact_link_route_links_subscriber(db_session, monkeypatch):
    actor_id = uuid.uuid4()
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_contact_link(
        conversation.id,
        _request(),
        target_type="subscriber",
        subscriber_id=str(subscriber.id),
        reseller_id=None,
        subscriber_id_manual=None,
        reseller_id_manual=None,
        note=None,
        db=db_session,
    )

    link = db_session.query(InboxContactLink).one()
    assert response.status_code == 303
    assert "status=success" in response.headers["location"]
    assert link.subscriber_id == subscriber.id
    assert link.linked_by_person_id == actor_id


def test_admin_contact_link_route_reports_missing_target(db_session):
    conversation = _conversation(db_session)

    response = inbox_web.team_inbox_contact_link(
        conversation.id,
        _request(),
        target_type="subscriber",
        subscriber_id=None,
        reseller_id=None,
        subscriber_id_manual=None,
        reseller_id_manual=None,
        note=None,
        db=db_session,
    )

    assert response.status_code == 303
    assert "status=error" in response.headers["location"]
    assert db_session.query(InboxContactLink).count() == 0


def test_admin_represented_customer_route_keeps_sender_as_representative(
    db_session, monkeypatch
):
    actor_id = uuid.uuid4()
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session)
    participant = InboxConversationParticipant(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        normalized_endpoint=conversation.contact_address,
        provider_account_scope="default",
        admission_source=InboxParticipantAdmissionSource.inbound_from.value,
    )
    db_session.add(participant)
    db_session.flush()
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_represented_customer(
        conversation.id,
        _request(),
        participant_id=str(participant.id),
        subscriber_id=str(subscriber.id),
        reason="Calling for the account holder",
        db=db_session,
    )

    db_session.refresh(conversation)
    db_session.refresh(participant)
    assert response.status_code == 303
    assert "status=success" in response.headers["location"]
    assert conversation.subscriber_id == subscriber.id
    assert (
        participant.relationship_type
        == InboxParticipantRelationship.representative.value
    )
    assert db_session.query(InboxContactLink).count() == 0


def test_admin_represented_lead_route_links_subject_not_sender(db_session, monkeypatch):
    actor_id = uuid.uuid4()
    conversation = _conversation(db_session)
    participant = InboxConversationParticipant(
        conversation_id=conversation.id,
        channel_type=conversation.channel_type,
        normalized_endpoint=conversation.contact_address,
        provider_account_scope="default",
        admission_source=InboxParticipantAdmissionSource.inbound_from.value,
    )
    represented_party = Party(
        party_type=PartyType.person.value,
        display_name="Represented Prospect",
    )
    db_session.add_all([participant, represented_party])
    db_session.flush()
    lead = Lead(
        party_id=represented_party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Explicit represented Lead fixture",
        title="Represented Prospect",
    )
    db_session.add(lead)
    db_session.flush()
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_represented_lead(
        conversation.id,
        _request(),
        participant_id=str(participant.id),
        lead_id=str(lead.id),
        reason="Calling for the prospective account holder",
        db=db_session,
    )

    db_session.refresh(participant)
    relationship = db_session.query(InboxConversationLeadLink).one()
    assert response.status_code == 303
    assert "status=success" in response.headers["location"]
    assert relationship.lead_id == lead.id
    assert relationship.party_id == represented_party.id
    assert participant.relationship_type == (
        InboxParticipantRelationship.representative.value
    )
    assert conversation.subscriber_id is None
    assert db_session.query(InboxContactLink).count() == 0


def test_inbox_represented_lead_search_uses_typed_active_lead_query(db_session):
    represented_party = Party(
        party_type=PartyType.person.value,
        display_name="Searchable Represented Prospect",
    )
    db_session.add(represented_party)
    db_session.flush()
    lead = Lead(
        party_id=represented_party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Explicit represented Lead fixture",
        title="Searchable Represented Prospect",
    )
    db_session.add(lead)
    db_session.flush()

    response = inbox_web.team_inbox_lead_search(
        q="Searchable Represented",
        limit=8,
        db=db_session,
    )

    assert len(response["items"]) == 1
    assert response["items"][0]["id"] == str(lead.id)
    assert response["items"][0]["type"] == "lead"
    assert "Searchable Represented Prospect" in response["items"][0]["label"]


def test_admin_merge_contact_route_finds_customer_and_attaches_lead(
    db_session, monkeypatch
):
    actor_id = uuid.uuid4()
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subject="Please connect my account")
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_merge_contact(
        conversation.id,
        _request(),
        target_type="subscriber",
        target_query=subscriber.email,
        db=db_session,
    )

    lead = db_session.query(Lead).one()
    link = db_session.query(InboxContactLink).one()
    db_session.refresh(conversation)
    db_session.refresh(subscriber)
    assert response.status_code == 303
    assert "status=success" in response.headers["location"]
    assert lead.subscriber_id == subscriber.id
    assert subscriber.party_id == lead.party_id
    assert link.subscriber_id == subscriber.id
    assert conversation.subscriber_id == subscriber.id
    assert (
        conversation.metadata_["lead_capture"]["merge"]["target_type"] == "subscriber"
    )


def test_admin_internal_note_route_records_private_message(db_session, monkeypatch):
    actor_id = uuid.uuid4()
    conversation = _conversation(db_session)
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_internal_note(
        conversation.id,
        _request(),
        body_text="Customer confirmed the outage on Instagram.",
        db=db_session,
    )

    message = db_session.query(InboxMessage).one()
    assert response.status_code == 303
    assert "status=success" in response.headers["location"]
    assert message.direction == InboxMessageDirection.internal.value
    assert message.body == "Customer confirmed the outage on Instagram."
    assert message.metadata_["actor_id"] == str(actor_id)


def test_admin_status_action_tracks_history(db_session, monkeypatch):
    actor_id = uuid.uuid4()
    conversation = _conversation(db_session)
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    response = inbox_web.team_inbox_status_action(
        conversation.id,
        _request(),
        status_value=InboxConversationStatus.pending.value,
        db=db_session,
    )

    db_session.refresh(conversation)
    history = conversation.metadata_["status_history"]
    assert response.status_code == 303
    assert conversation.status == InboxConversationStatus.pending.value
    assert history[-1]["from"] == InboxConversationStatus.open.value
    assert history[-1]["to"] == InboxConversationStatus.pending.value
    assert history[-1]["actor_id"] == str(actor_id)


def test_internal_notes_do_not_hide_inbound_needs_response(db_session, monkeypatch):
    conversation = _conversation(db_session)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=conversation.channel_type,
            direction=InboxMessageDirection.inbound.value,
            body="My service is down",
        )
    )
    db_session.flush()

    inbox_web.team_inbox_internal_note(
        conversation.id,
        _request(),
        body_text="Checked account context.",
        db=db_session,
    )

    result = team_inbox_read.list_conversations(db_session, needs_response=True)
    assert result.count == 1
    assert (
        result.items[0].latest_message_direction == InboxMessageDirection.inbound.value
    )


def test_admin_label_routes_create_apply_and_remove_label(db_session, monkeypatch):
    actor_id = uuid.uuid4()
    conversation = _conversation(db_session)
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )

    created = inbox_web.team_inbox_label_create(
        conversation.id,
        name="VIP Follow-up",
        color="rose",
        db=db_session,
    )
    label = db_session.query(InboxLabel).one()
    applied = inbox_web.team_inbox_label_apply(
        conversation.id,
        _request(),
        label_id=str(label.id),
        db=db_session,
    )
    labels = inbox_web.team_inbox_operations.conversation_labels(
        db_session, conversation.id
    )
    removed = inbox_web.team_inbox_label_remove(
        conversation.id,
        label_id=str(label.id),
        db=db_session,
    )

    assert created.status_code == 303
    assert applied.status_code == 303
    assert labels[0].name == "VIP Follow-up"
    assert removed.status_code == 303
    assert (
        inbox_web.team_inbox_operations.conversation_labels(db_session, conversation.id)
        == []
    )


def test_admin_macro_create_and_reply_records_execution(db_session, monkeypatch):
    _subscriber(db_session)
    conversation = _conversation(db_session)
    actor_id = _online_reply_agent(db_session, conversation).id
    conversation.contact_address = "0803 555 0114"
    conversation.channel_type = InboxChannelType.whatsapp.value
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=InboxChannelType.whatsapp.value,
            direction=InboxMessageDirection.inbound.value,
            body="Hello",
            received_at=datetime.now(UTC),
        )
    )
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )
    db_session.commit()

    create_response = inbox_web.team_inbox_macro_create(
        conversation.id,
        _request(),
        name="Outage acknowledgement",
        body_text="We are checking this now.",
        visibility="shared",
        db=db_session,
    )
    macro = db_session.query(InboxReplyMacro).one()
    reply_response = inbox_web.team_inbox_reply(
        conversation.id,
        _request(),
        BackgroundTasks(),
        body_text=macro.body_text,
        macro_id=str(macro.id),
        db=db_session,
    )

    db_session.refresh(macro)
    messages = db_session.query(InboxMessage).all()
    assert create_response.status_code == 303
    assert reply_response.status_code == 303
    assert macro.execution_count == 1
    assert macro.actions == [
        {
            "action_type": "reply_text",
            "params": {"body_text": "We are checking this now."},
        }
    ]
    assert any(
        message.direction == InboxMessageDirection.outbound.value
        for message in messages
    )


def test_macro_actions_can_set_status_and_apply_label(db_session):
    actor_id = uuid.uuid4()
    conversation = _conversation(db_session)
    macro = team_inbox_operations.create_macro(
        db_session,
        name="Escalate outage",
        body_text="We are escalating this.",
        actions=[
            {"action_type": "set_status", "params": {"status": "pending"}},
            {"action_type": "add_tag", "params": {"tag": "Outage"}},
        ],
        created_by_person_id=actor_id,
    )

    result = team_inbox_operations.execute_macro_actions(
        db_session,
        conversation=conversation,
        macro_id=macro.id,
        actor_person_id=actor_id,
    )

    db_session.refresh(conversation)
    db_session.refresh(macro)
    labels = team_inbox_operations.conversation_labels(db_session, conversation.id)
    assert result["ok"] is True
    assert result["actions_executed"] == 2
    assert conversation.status == InboxConversationStatus.pending.value
    assert conversation.metadata_["status_history"][-1]["macro_id"] == str(macro.id)
    assert labels[0].name == "Outage"
    assert macro.execution_count == 1


def test_admin_template_create_and_reply_uses_template(db_session, monkeypatch):
    conversation = _conversation(db_session)
    actor_id = _online_reply_agent(db_session, conversation).id
    conversation.channel_type = InboxChannelType.email.value
    conversation.contact_address = "ada@example.com"
    from app.services import web_admin as web_admin_service

    monkeypatch.setattr(
        web_admin_service, "get_actor_id", lambda request: str(actor_id)
    )
    db_session.commit()
    create_response = inbox_web.team_inbox_template_create(
        conversation.id,
        name="Outage update",
        channel_type="email",
        subject="Outage update",
        body_text="We are still working on this.",
        provider_template_name=None,
        provider_template_language=None,
        db=db_session,
    )
    template = db_session.query(InboxMessageTemplate).one()
    reply_response = inbox_web.team_inbox_reply(
        conversation.id,
        _request(),
        BackgroundTasks(),
        body_text="",
        macro_id=None,
        template_id=str(template.id),
        db=db_session,
    )

    message = db_session.query(InboxMessage).one()
    notification = db_session.query(Notification).one()
    assert create_response.status_code == 303
    assert reply_response.status_code == 303
    assert notification.status == NotificationStatus.queued
    assert notification.subject == "Re: Outage update"
    assert message.notification_id == notification.id
    assert message.metadata_["template_id"] == str(template.id)
    assert "We are still working on this." in message.body


def test_admin_template_create_stores_whatsapp_provider_mapping(db_session):
    conversation = _conversation(db_session)

    response = inbox_web.team_inbox_template_create(
        conversation.id,
        name="Service update",
        channel_type="whatsapp",
        subject=None,
        body_text="Fallback text",
        provider_template_name="service_update",
        provider_template_language="en",
        db=db_session,
    )

    template = db_session.query(InboxMessageTemplate).one()
    assert response.status_code == 303
    assert template.metadata_["provider_template_name"] == "service_update"
    assert template.metadata_["provider_template_language"] == "en"
