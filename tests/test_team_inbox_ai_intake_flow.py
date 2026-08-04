from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from app.models.ai_intake import AiIntakeConfig
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
)
from app.services import (
    ai_intake,
    team_inbox_channel_receive,
    team_inbox_maintenance,
)
from app.services.ai.client import AIResponse
from app.services.owner_commands import CommandContext
from app.tasks import notifications as notification_tasks


class _Gateway:
    def __init__(
        self,
        *,
        confidence: float = 0.95,
        intent: str = "technical_support",
        category: str = "no_internet",
        error: Exception | None = None,
    ):
        self.confidence = confidence
        self.intent = intent
        self.category = category
        self.error = error
        self.calls = 0

    def generate_with_fallback(self, _db, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return (
            AIResponse(
                content=json.dumps(
                    {
                        "intent": self.intent,
                        "category": self.category,
                        "confidence": self.confidence,
                        "department": None,
                        "requires_follow_up": False,
                        "follow_up_question": None,
                        "summary": "Customer reports no internet service.",
                    }
                ),
                tokens_in=10,
                tokens_out=10,
                model="test-model",
                provider="test-provider",
            ),
            {"endpoint": "primary", "fallback_used": False},
        )


def _team(db_session, name: str) -> ServiceTeam:
    team = ServiceTeam(
        name=name,
        team_type=ServiceTeamType.support.value,
        is_active=True,
    )
    db_session.add(team)
    db_session.flush()
    return team


def _config(
    db_session,
    *,
    fallback_team_id=None,
    threshold: float = 0.75,
    data_cleaning_support_team_id=None,
):
    row = AiIntakeConfig(
        scope_key="default",
        channel_type="any",
        is_enabled=True,
        confidence_threshold=threshold,
        allow_followup_questions=True,
        max_clarification_turns=1,
        escalate_after_minutes=5,
        exclude_campaign_attribution=True,
        fallback_team_id=fallback_team_id,
        department_mappings=[],
        metadata_={
            "data_cleaning_support_team_id": str(data_cleaning_support_team_id)
            if data_cleaning_support_team_id
            else None
        },
    )
    db_session.add(row)
    db_session.flush()
    return row


def _receive(db_session, *, message_id: str, body: str = "No internet"):
    return team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="2348012345678",
            body=body,
            external_message_id=message_id,
            external_thread_id="wa-thread-1",
            metadata={
                "provider": "meta_cloud_api",
                "provider_account_scope": "phone-1",
            },
        ),
    )


def test_high_confidence_routes_team_without_assigning_agent(db_session, monkeypatch):
    technical = _team(db_session, "Technical Support")
    _team(db_session, "Helpdesk")
    _config(db_session)
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-1")
    db_session.commit()

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    assert conversation.primary_service_team_id == technical.id
    assert conversation.assignments == []
    assert message.metadata_["ai_intent"] == "technical_support"
    assert message.metadata_["ai_category"] == "no_internet"
    assert message.metadata_["ai_intake_status"] == "classified"
    assert message.metadata_["routing"]["reason"] == "ai_intake_department"


def test_high_confidence_billing_routes_helpdesk(db_session, monkeypatch):
    helpdesk = _team(db_session, "Helpdesk")
    _config(db_session)
    gateway = _Gateway(intent="billing_issue", category="payment_not_reflected")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(
        db_session, message_id="wamid-ai-billing", body="My payment is missing"
    )

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.primary_service_team_id == helpdesk.id
    assert conversation.assignments == []


def test_department_mapping_routes_to_configured_team(db_session, monkeypatch):
    finance = _team(db_session, "Finance")
    config = _config(db_session)
    config.department_mappings = [{"intent": "billing_issue", "department": "finance"}]
    gateway = _Gateway(intent="billing_issue", category="invoice_request")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-finance", body="Invoice please")

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.primary_service_team_id == finance.id


def test_gateway_failure_still_routes_to_fallback(db_session, monkeypatch):
    fallback = _team(db_session, "Helpdesk")
    _config(db_session, fallback_team_id=fallback.id)
    gateway = _Gateway(error=TimeoutError("provider timeout"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-timeout")

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    assert conversation.primary_service_team_id == fallback.id
    assert message.metadata_["ai_intake_status"] == "failed"
    assert message.metadata_["routing"]["reason"] == "ai_intake_fallback"


def test_campaign_attribution_skips_gateway_in_observation_path(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Helpdesk")
    fallback_id = fallback.id
    _config(db_session, fallback_team_id=fallback_id)
    db_session.commit()
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    [result] = team_inbox_channel_receive.receive_inbound_channel_batch_committed(
        db_session,
        [
            team_inbox_channel_receive.InboundChannelPayload(
                channel_type=InboxChannelType.facebook_messenger.value,
                contact_address="campaign-contact",
                body="Hello",
                external_message_id="fb-campaign-1",
                metadata={
                    "provider": "meta_social",
                    "page_or_account_id": "page-campaign",
                    "campaign_attribution": {"campaign_id": "campaign-1"},
                },
                fallback_service_team_id=fallback_id,
            )
        ],
    )

    message = db_session.get(InboxMessage, result["message_id"])
    assert gateway.calls == 0
    assert message.metadata_["ai_intake_reason"] == "campaign_excluded"


def test_duplicate_delivery_does_not_reclassify_or_duplicate_message(
    db_session, monkeypatch
):
    _team(db_session, "Technical Support")
    _config(db_session)
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-ai-duplicate")
    second = _receive(db_session, message_id="wamid-ai-duplicate", body="Again")

    assert first.duplicate is False
    assert second.duplicate is True
    assert gateway.calls == 1
    assert db_session.query(InboxMessage).count() == 1


def test_postgres_thread_lock_uses_stable_conversation_scope():
    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Session:
        def __init__(self):
            self.calls = []

        def get_bind(self):
            return _Bind()

        def execute(self, statement, parameters):
            self.calls.append((str(statement), parameters))

    session = _Session()
    team_inbox_channel_receive._acquire_thread_lock(
        session,
        channel_type="whatsapp",
        external_thread_id="wa-thread-1",
    )

    [(statement, parameters)] = session.calls
    assert "pg_advisory_xact_lock" in statement
    assert parameters == {
        "key": team_inbox_channel_receive._thread_lock_key("whatsapp", "wa-thread-1")
    }


def test_existing_assigned_conversation_is_not_reclassified(db_session, monkeypatch):
    helpdesk = _team(db_session, "Helpdesk")
    _config(db_session)
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="2348012345678",
        external_thread_id="wa-thread-1",
        primary_service_team_id=helpdesk.id,
    )
    db_session.add(conversation)
    db_session.flush()
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=helpdesk.id,
        person_id=uuid4(),
        is_active=True,
    )
    db_session.add(assignment)
    db_session.flush()
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-owned")

    message = db_session.get(InboxMessage, result.message_id)
    assert gateway.calls == 0
    assert conversation.primary_service_team_id == helpdesk.id
    assert assignment.is_active is True
    assert message.metadata_["ai_intake_reason"] == "active_owner"


def test_follow_up_reply_can_route_and_first_message_is_not_enqueued(
    db_session, monkeypatch
):
    technical = _team(db_session, "Technical Support")
    fallback = _team(db_session, "Helpdesk")
    fallback_id = fallback.id
    _config(db_session, fallback_team_id=fallback_id, threshold=0.8)
    gateway = _Gateway(confidence=0.3)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-follow-1", body="Please help")
    conversation = db_session.get(InboxConversation, first.conversation_id)
    first_message = db_session.get(InboxMessage, first.message_id)
    assert conversation.primary_service_team_id is None
    assert first_message.metadata_["ai_intake_status"] == "awaiting_follow_up"
    assert first_message.metadata_["ai_intake_follow_up_question"] == (
        ai_intake.GENERIC_FOLLOW_UP_QUESTION
    )
    follow_up = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .one()
    )
    notification = db_session.get(Notification, follow_up.notification_id)
    assert follow_up.body == ai_intake.GENERIC_FOLLOW_UP_QUESTION
    assert follow_up.metadata_["ai_intake_follow_up"] is True
    assert notification is not None
    assert notification.channel == NotificationChannel.whatsapp
    assert notification.status == NotificationStatus.queued
    assert conversation.metadata_["ai_intake"]["follow_up_delivery_status"] == (
        "queued"
    )

    gateway.confidence = 0.95
    second = _receive(
        db_session,
        message_id="wamid-follow-2",
        body="It is about my internet connection",
    )
    second_message = db_session.get(InboxMessage, second.message_id)

    assert conversation.primary_service_team_id == technical.id
    assert second_message.metadata_["ai_intake_status"] == "classified"
    assert conversation.assignments == []
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .count()
        == 1
    )


def test_second_uncertain_reply_falls_back_without_another_question(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Helpdesk")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-follow-bad-1", body="Please help")
    second = _receive(
        db_session,
        message_id="wamid-follow-bad-2",
        body="I am still not sure",
    )

    conversation = db_session.get(InboxConversation, first.conversation_id)
    second_message = db_session.get(InboxMessage, second.message_id)
    assert conversation.primary_service_team_id == fallback.id
    assert second_message.metadata_["ai_intake_status"] == "fallback"
    assert second_message.metadata_["ai_intake_reason"] == "follow_up_limit_reached"
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .count()
        == 1
    )


def test_whatsapp_follow_up_dispatcher_sends_the_approved_question(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Helpdesk")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        notification_tasks.whatsapp_service,
        "send_text_message",
        lambda **kwargs: calls.append(kwargs) or {"ok": True, "sent": True},
    )

    _receive(db_session, message_id="wamid-follow-deliver", body="Please help")
    delivered = notification_tasks._deliver_notification_queue(
        db_session, batch_size=10
    )

    assert delivered == 1
    assert calls[0]["body"] == ai_intake.GENERIC_FOLLOW_UP_QUESTION
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .one()
    )
    assert outbound.metadata_["delivery_status"] == "delivered"


def test_stale_follow_up_recovers_to_configured_fallback_without_assignment(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Helpdesk")
    fallback_id = fallback.id
    _config(db_session, fallback_team_id=fallback_id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    received = _receive(db_session, message_id="wamid-stale")
    db_session.commit()
    conversation = db_session.get(InboxConversation, received.conversation_id)
    due_text = conversation.metadata_["ai_intake"]["ai_intake_fallback_due_at"]
    due_at = datetime.fromisoformat(due_text)
    db_session.close()

    outcome = team_inbox_maintenance.recover_stale_ai_intake(
        db_session,
        team_inbox_maintenance.RecoverStaleAiIntakeCommand(
            context=CommandContext.system(
                actor="task:test-ai-intake-recovery",
                scope="team-inbox:maintenance",
                reason="test stale AI intake recovery",
            ),
            now=due_at + timedelta(seconds=1),
        ),
    )

    conversation = db_session.get(InboxConversation, received.conversation_id)
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .count()
        == 1
    )
    assert outcome.changed == 1
    assert conversation.primary_service_team_id == fallback_id
    assert conversation.metadata_["ai_intake"]["status"] == "escalated"
    assert conversation.assignments == []


def test_whatsapp_facebook_and_instagram_use_the_same_classifier(
    db_session, monkeypatch
):
    _team(db_session, "Technical Support")
    _config(db_session)
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    team_inbox_channel_receive.receive_whatsapp_webhook(
        db_session,
        provider="meta_cloud_api",
        payload={
            "message": {"from": "2348012345678", "text": "No internet", "id": "wa-1"},
            "phone_number_id": "phone-1",
        },
    )
    for channel, suffix in (
        (InboxChannelType.facebook_messenger.value, "fb"),
        (InboxChannelType.instagram_dm.value, "ig"),
    ):
        team_inbox_channel_receive.receive_inbound_channel(
            db_session,
            team_inbox_channel_receive.InboundChannelPayload(
                channel_type=channel,
                contact_address=f"contact-{suffix}",
                body="No internet",
                external_message_id=f"{suffix}-1",
                metadata={
                    "provider": "meta_social",
                    "page_or_account_id": f"page-{suffix}",
                },
            ),
        )

    assert gateway.calls == 3


def test_data_cleaning_eligibility_uses_exact_configured_team_uuid(
    db_session, monkeypatch
):
    configured_support = _team(db_session, "Configured Cleaning Team")
    legacy_named_support_team = _team(db_session, "Support")
    _config(
        db_session,
        data_cleaning_support_team_id=configured_support.id,
    )
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="2348012345678",
        external_thread_id="wa-thread-1",
        primary_service_team_id=legacy_named_support_team.id,
    )
    db_session.add(conversation)
    db_session.flush()
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    _receive(db_session, message_id="wamid-cleaning-mismatch")

    cleaning = conversation.metadata_["ai_data_cleaning"]
    assert cleaning["eligible"] is False
    assert cleaning["state"] == "idle"
    assert cleaning["reason"] == "conversation_team_mismatch"
    assert cleaning["support_team_id"] == str(configured_support.id)
    assert gateway.calls == 0


def test_data_cleaning_eligible_conversation_records_identify_pending(
    db_session, monkeypatch
):
    configured_support = _team(db_session, "Customer Support")
    _config(
        db_session,
        data_cleaning_support_team_id=configured_support.id,
    )
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="2348012345678",
        external_thread_id="wa-thread-1",
        primary_service_team_id=configured_support.id,
    )
    db_session.add(conversation)
    db_session.flush()
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    _receive(db_session, message_id="wamid-cleaning-eligible")

    cleaning = conversation.metadata_["ai_data_cleaning"]
    assert cleaning["eligible"] is True
    assert cleaning["state"] == "identify_pending"
    assert cleaning["reason"] == "eligible"
    assert cleaning["support_team_id"] == str(configured_support.id)
    assert gateway.calls == 0
