from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.models.ai_intake import (
    AiIntakeConfig,
    AiIntakePolicy,
    AiIntakePolicyVersion,
    AiIntakeSession,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import (
    Gender,
    Reseller,
    Subscriber,
    SubscriberCategory,
    UserType,
)
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxMessage,
    InboxStatusTransitionEvent,
)
from app.services import (
    ai_conversation_intake,
    ai_intake,
    team_inbox_channel_receive,
    team_inbox_maintenance,
)
from app.services.ai.client import AIResponse
from app.services.integrations import installations
from app.services.integrations.connectors.whatsapp_runtime import WHATSAPP_PROVIDER_META
from app.services.integrations.runtime import ValidationResult
from app.services.integrations.whatsapp_capability import (
    WHATSAPP_RECEIVE_CAPABILITY,
    WHATSAPP_SEND_CAPABILITY,
)
from app.services.operator_tenant import provision_operator_tenant
from app.services.owner_commands import CommandContext
from app.tasks import notifications as notification_tasks


@pytest.fixture(autouse=True)
def _operator_tenant(db_session):
    provision_operator_tenant(db_session)


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


def _install_whatsapp_scope(db_session, *, account_scope: str) -> None:
    installation = installations.create_draft(
        db_session,
        connector_key="whatsapp",
        name=f"WhatsApp {uuid4().hex}",
        environment="test",
        actor="test",
    )
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={
            "provider": WHATSAPP_PROVIDER_META,
            "phone_number": "test-number",
            "phone_number_id": account_scope,
            "graph_version": "v21.0",
            "timeout_seconds": 10,
        },
        secret_refs={
            "service_credentials": "env://WHATSAPP_TEST_TOKEN",
            "webhook_signing_secret": "env://WHATSAPP_TEST_SIGNING_SECRET",
            "webhook_verify_token": "env://WHATSAPP_TEST_VERIFY_TOKEN",
        },
        actor="test",
    )
    for capability_id in (WHATSAPP_SEND_CAPABILITY, WHATSAPP_RECEIVE_CAPABILITY):
        installations.bind_capability(
            db_session,
            installation_id=installation.id,
            capability_id=capability_id,
            scope={"channel": "whatsapp", "phone_number_id": account_scope},
            policy={"default": True},
            actor="test",
        )
    installations.validate_static(db_session, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
        actor="test",
    )


def _residential_subscriber(db_session) -> Subscriber:
    reseller = Reseller(name=f"House {uuid4()}", is_house=True)
    db_session.add(reseller)
    db_session.flush()
    subscriber = Subscriber(
        email=f"ai-intake-{uuid4()}@example.test",
        first_name="AI",
        last_name="Intake",
        user_type=UserType.customer,
        reseller_id=reseller.id,
        gender=Gender.unknown,
    )
    subscriber.category = SubscriberCategory.residential
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _non_queue_outbound_count(db_session) -> int:
    rows = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .all()
    )
    return sum(
        1
        for row in rows
        if dict(row.metadata_ or {}).get("automation_kind") != "queue_notification"
    )


def _config(
    db_session,
    *,
    fallback_team_id=None,
    threshold: float = 0.75,
    data_cleaning_support_team_id=None,
    mappings=None,
    welcome_message: str | None = None,
    scope_key: str = "meta_cloud_api:phone-1",
    channel_type: str = InboxChannelType.whatsapp.value,
    missing_fallback: bool = False,
):
    if fallback_team_id is None and not missing_fallback:
        fallback_team_id = _team(db_session, f"Configured Fallback {uuid4()}").id
    row = AiIntakeConfig(
        scope_key=scope_key,
        channel_type=channel_type,
        is_enabled=True,
        confidence_threshold=threshold,
        allow_followup_questions=True,
        max_clarification_turns=1,
        escalate_after_minutes=5,
        exclude_campaign_attribution=True,
        fallback_team_id=fallback_team_id,
        department_mappings=list(mappings or []),
        metadata_={
            "data_cleaning_support_team_id": str(data_cleaning_support_team_id)
            if data_cleaning_support_team_id
            else None,
            "data_cleanup_enabled": data_cleaning_support_team_id is not None,
            "welcome_message": welcome_message,
        },
    )
    db_session.add(row)
    db_session.flush()
    return row


def _mapping(intent: str, team: ServiceTeam, department: str | None = None) -> dict:
    return {
        "intent": intent,
        "department": department or intent,
        "service_team_id": str(team.id),
    }


def _process_ai(db_session, *, sweeps: int = 3):
    result = None
    for _ in range(sweeps):
        db_session.commit()
        result = ai_conversation_intake.process_ready_sessions(
            db_session,
            ai_conversation_intake.AiSessionProcessCommand(
                context=CommandContext.system(
                    actor="task:test-ai-intake-session-processor",
                    scope="ai:intake-session",
                    reason="test AI intake session processing",
                ),
            ),
        )
        db_session.commit()
        if result.processed == 0:
            break
    return result


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


def test_high_confidence_routes_team_and_queues_when_no_agent(db_session, monkeypatch):
    technical = _team(db_session, "Configured Technical Team")
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-1")
    assert gateway.calls == 0
    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.status == "pending"
    assert (
        db_session.query(InboxStatusTransitionEvent)
        .filter(InboxStatusTransitionEvent.conversation_id == conversation.id)
        .one()
        .reason_code
        == "ai_intake_started"
    )
    _process_ai(db_session)

    message = db_session.get(InboxMessage, result.message_id)
    assert conversation.primary_service_team_id == technical.id
    assert conversation.assignments == []
    assert conversation.status == "open"
    assert message.metadata_["ai_intent"] == "technical_support"
    assert message.metadata_["ai_category"] == "no_internet"
    assert message.metadata_["ai_intake_status"] == "classified"
    assert message.metadata_["routing"]["reason"] == "ai_intake_department"
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    assert outbound[0].metadata_["sender_type"] == "ai"
    assert outbound[0].metadata_["ai_display_name"] == "Dotmac Virtual Assistant"
    reasons = [
        event.reason_code
        for event in db_session.query(InboxStatusTransitionEvent)
        .filter(InboxStatusTransitionEvent.conversation_id == conversation.id)
        .order_by(InboxStatusTransitionEvent.recorded_at.asc())
    ]
    assert reasons == ["ai_intake_started", "ai_handoff_accepted"]


def test_receive_persists_ai_work_without_synchronous_ai_response(
    db_session, monkeypatch
):
    technical = _team(db_session, "Configured Technical Team")
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-async-owner")

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.status == "pending"
    assert gateway.calls == 0
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "outbound")
        .count()
        == 0
    )

    _process_ai(db_session, sweeps=2)

    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "outbound")
        .count()
        == 2
    )
    assert gateway.calls == 1


def test_high_confidence_billing_routes_configured_team(db_session, monkeypatch):
    billing = _team(db_session, "Configured Billing Team")
    _config(db_session, mappings=[_mapping("billing_issue", billing, "billing")])
    gateway = _Gateway(intent="billing_issue", category="payment_not_reflected")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(
        db_session, message_id="wamid-ai-billing", body="My payment is missing"
    )
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.primary_service_team_id == billing.id
    assert conversation.assignments == []


def test_department_mapping_routes_to_configured_team(db_session, monkeypatch):
    finance = _team(db_session, "Finance")
    _config(
        db_session,
        mappings=[_mapping("billing_issue", finance, "finance")],
    )
    gateway = _Gateway(intent="billing_issue", category="invoice_request")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-finance", body="Invoice please")
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert conversation.primary_service_team_id == finance.id


def test_gateway_failure_still_routes_to_fallback(db_session, monkeypatch):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id)
    gateway = _Gateway(error=TimeoutError("provider timeout"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-timeout")
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    assert conversation.primary_service_team_id == fallback.id
    assert message.metadata_["ai_intake_status"] == "failed"
    assert message.metadata_["routing"]["reason"] == "ai_intake_fallback"
    assert conversation.status == "open"
    assert (
        db_session.query(InboxStatusTransitionEvent)
        .filter(InboxStatusTransitionEvent.conversation_id == conversation.id)
        .filter(InboxStatusTransitionEvent.reason_code == "ai_handoff_accepted")
        .count()
        == 1
    )


def test_enabled_policy_without_fallback_is_rejected(db_session, monkeypatch):
    technical = _team(db_session, "Configured Technical Team")
    _config(
        db_session,
        missing_fallback=True,
        mappings=[_mapping("technical_support", technical, "technical")],
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-no-fallback")
    _process_ai(db_session)

    message = db_session.get(InboxMessage, result.message_id)
    assert gateway.calls == 0
    assert message.metadata_["ai_intake_status"] == "failed"
    assert message.metadata_["ai_intake_reason"] == "invalid_configuration"


def test_missing_intent_mapping_uses_configured_fallback(db_session, monkeypatch):
    fallback = _team(db_session, "Configured Fallback Team")
    configured_team = _team(db_session, "Configured Nonmatching Team")
    _config(
        db_session,
        fallback_team_id=fallback.id,
        mappings=[_mapping("billing_issue", configured_team, "billing")],
    )
    gateway = _Gateway(intent="coverage_check", category="new_area")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-missing-mapping")
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    assert conversation.primary_service_team_id == fallback.id
    assert message.metadata_["routing"]["reason"] == "ai_intake_fallback"


def test_inactive_mapped_team_makes_enabled_config_invalid(db_session, monkeypatch):
    fallback = _team(db_session, "Configured Fallback Team")
    inactive_team = _team(db_session, "Configured Inactive Team")
    inactive_team.is_active = False
    _config(
        db_session,
        fallback_team_id=fallback.id,
        mappings=[_mapping("technical_support", inactive_team, "technical")],
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-inactive-team")
    _process_ai(db_session)

    message = db_session.get(InboxMessage, result.message_id)
    assert gateway.calls == 0
    assert message.metadata_["ai_intake_status"] == "failed"
    assert message.metadata_["ai_intake_reason"] == "invalid_configuration"


def test_campaign_attribution_skips_gateway_in_observation_path(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    fallback_id = fallback.id
    _config(
        db_session,
        fallback_team_id=fallback_id,
        scope_key="meta_social:page-campaign",
        channel_type=InboxChannelType.facebook_messenger.value,
    )
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
    technical = _team(db_session, "Configured Technical Team")
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-ai-duplicate")
    second = _receive(db_session, message_id="wamid-ai-duplicate", body="Again")
    _process_ai(db_session)

    assert first.duplicate is False
    assert second.duplicate is True
    assert gateway.calls == 1
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "inbound")
        .count()
        == 1
    )


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
    existing_owner_team = _team(db_session, "Configured Existing Owner Team")
    _config(db_session)
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        contact_address="2348012345678",
        external_thread_id="wa-thread-1",
        primary_service_team_id=existing_owner_team.id,
    )
    db_session.add(conversation)
    db_session.flush()
    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=existing_owner_team.id,
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
    assert conversation.primary_service_team_id == existing_owner_team.id
    assert assignment.is_active is True
    assert message.metadata_["ai_intake_reason"] == "active_owner"


def test_follow_up_reply_can_route_and_first_message_is_not_enqueued(
    db_session, monkeypatch
):
    technical = _team(db_session, "Configured Technical Team")
    fallback = _team(db_session, "Configured Fallback Team")
    fallback_id = fallback.id
    _config(
        db_session,
        fallback_team_id=fallback_id,
        threshold=0.8,
        welcome_message="Hello, I am Dotmac Virtual Assistant. I can help understand your request and connect you to the right team.",
        mappings=[_mapping("technical_support", technical, "technical")],
    )
    gateway = _Gateway(confidence=0.3)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-follow-1", body="Please help")
    _process_ai(db_session)
    conversation = db_session.get(InboxConversation, first.conversation_id)
    first_message = db_session.get(InboxMessage, first.message_id)
    assert conversation.primary_service_team_id is None
    assert first_message.metadata_["ai_intake_status"] == "awaiting_follow_up"
    assert first_message.metadata_["ai_intake_follow_up_question"] == (
        ai_intake.GENERIC_FOLLOW_UP_QUESTION
    )
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    assert [message.body for message in outbound] == [
        "Hello, I am Dotmac Virtual Assistant. I can help understand your request and connect you to the right team.",
        ai_intake.GENERIC_FOLLOW_UP_QUESTION,
    ]
    assert outbound[0].metadata_["ai_message_purpose"] == "welcome"
    follow_up = outbound[1]
    notification = db_session.get(Notification, follow_up.notification_id)
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
    _process_ai(db_session)
    second_message = db_session.get(InboxMessage, second.message_id)

    assert conversation.primary_service_team_id == technical.id
    assert second_message.metadata_["ai_intake_status"] == "classified"
    assert conversation.assignments == []
    assert _non_queue_outbound_count(db_session) == 2


def test_second_uncertain_reply_falls_back_without_another_question(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-follow-bad-1", body="Please help")
    _process_ai(db_session)
    second = _receive(
        db_session,
        message_id="wamid-follow-bad-2",
        body="I am still not sure",
    )
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, first.conversation_id)
    second_message = db_session.get(InboxMessage, second.message_id)
    assert conversation.primary_service_team_id == fallback.id
    assert second_message.metadata_["ai_intake_status"] == "fallback"
    assert second_message.metadata_["ai_intake_reason"] == "follow_up_limit_reached"
    assert _non_queue_outbound_count(db_session) == 2


def test_whatsapp_follow_up_dispatcher_sends_the_approved_question(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
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
    _process_ai(db_session)
    delivered = notification_tasks._deliver_notification_queue(
        db_session, batch_size=10
    )

    assert delivered == 2
    assert calls[1]["body"] == ai_intake.GENERIC_FOLLOW_UP_QUESTION
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    assert all(
        message.metadata_["delivery_status"] == "delivered" for message in outbound
    )


def test_policy_version_activation_supersedes_without_mutating_active_version(
    db_session,
):
    _install_whatsapp_scope(db_session, account_scope="phone-policy")
    fallback = _team(db_session, "Configured Fallback Team")
    technical = _team(db_session, "Configured Technical Team")
    policy = AiIntakePolicy(
        scope_key="meta_cloud_api:phone-policy",
        channel_type=InboxChannelType.whatsapp.value,
        provider="meta_cloud_api",
        account_scope="phone-policy",
        fallback_team_id=fallback.id,
        is_enabled=False,
    )
    db_session.add(policy)
    db_session.flush()
    policy_id = policy.id
    technical_id = technical.id
    db_session.commit()
    context = CommandContext.system(
        actor="test", scope="ai:intake-policy-version", reason="test policy lifecycle"
    )

    draft = ai_conversation_intake.create_or_update_draft_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionDraftCommand(
            context=context,
            policy_id=policy_id,
            display_name="Dotmac Virtual Assistant",
            welcome_message="Hello from the configured assistant.",
            clarification_questions=(
                "Which service do you need help with?",
                "Is this for you or an organization?",
            ),
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )
    activated = ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=draft.version_id,
        ),
    )

    active_version = db_session.get(AiIntakePolicyVersion, activated.version_id)
    assert active_version.status == "activated"
    assert active_version.welcome_message == "Hello from the configured assistant."
    assert active_version.clarification_questions == [
        "Which service do you need help with?",
        "Is this for you or an organization?",
    ]
    assert db_session.get(AiIntakePolicy, policy_id).active_version_id == (
        active_version.id
    )
    active_version_id = active_version.id
    db_session.commit()

    replacement_draft = ai_conversation_intake.create_or_update_draft_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionDraftCommand(
            context=context,
            policy_id=policy_id,
            base_version_id=active_version_id,
            welcome_message="Edited copy for the next activation.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )
    assert replacement_draft.version_id != active_version_id
    active_version = db_session.get(AiIntakePolicyVersion, active_version_id)
    assert active_version.welcome_message == "Hello from the configured assistant."
    db_session.commit()

    second_activation = ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=replacement_draft.version_id,
        ),
    )
    active_version = db_session.get(AiIntakePolicyVersion, active_version_id)
    assert active_version.status == "superseded"
    assert active_version.is_active is False
    assert active_version.superseded_by_version_id == second_activation.version_id
    assert db_session.get(AiIntakePolicy, policy_id).active_version_id == (
        second_activation.version_id
    )


def test_admin_policy_context_exposes_bounded_read_only_version_history(db_session):
    account_scope = f"history-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, f"Fallback {uuid4()}")
    technical = _team(db_session, f"Technical {uuid4()}")
    fallback_id = fallback.id
    technical_id = technical.id
    db_session.commit()
    context = CommandContext.system(
        actor="test", scope="ai:intake-policy-history", reason="history evidence"
    )
    first = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="First version.",
            intent_team_mappings=(
                {"intent": "technical_support", "service_team_id": str(technical_id)},
            ),
        ),
    )
    ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context, version_id=first.version_id
        ),
    )
    second = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Second version.",
            intent_team_mappings=(
                {"intent": "technical_support", "service_team_id": str(technical_id)},
            ),
        ),
    )
    ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context, version_id=second.version_id
        ),
    )
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Editable draft only.",
            intent_team_mappings=(
                {"intent": "technical_support", "service_team_id": str(technical_id)},
            ),
        ),
    )

    history = ai_conversation_intake.admin_policy_context(db_session)[
        "ai_intake_policy_version_history"
    ]

    assert [row.status for row in history] == ["draft", "activated", "superseded"]
    assert history[0].version_id == draft.version_id
    assert history[1].is_active is True
    assert history[1].activated_at is not None
    assert history[2].is_active is False
    assert history[2].superseded_at is not None


def test_draft_policy_creation_stays_inactive_and_unactivated(db_session):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, f"Fallback {uuid4()}")
    technical = _team(db_session, f"Technical {uuid4()}")
    fallback_id = fallback.id
    technical_id = technical.id
    db_session.commit()

    result = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-draft",
                reason="prepare inactive draft policy",
            ),
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            display_name="Test Virtual Assistant",
            fallback_team_id=fallback_id,
            welcome_message="Hello from the draft assistant.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )

    policy = db_session.get(AiIntakePolicy, result.policy_id)
    version = db_session.get(AiIntakePolicyVersion, result.version_id)
    assert result.policy_enabled is False
    assert result.active_version_id is None
    assert policy.is_enabled is False
    assert policy.active_version_id is None
    assert policy.scope_key == f"{WHATSAPP_PROVIDER_META}:{account_scope}"
    assert version.status == "draft"
    assert version.is_active is False
    assert version.activated_at is None
    assert (
        db_session.query(AiIntakePolicyVersion).filter_by(is_active=True).count() == 0
    )


def test_draft_policy_creation_rejects_inactive_team(db_session):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    inactive = _team(db_session, f"Inactive {uuid4()}")
    inactive.is_active = False
    inactive_id = inactive.id
    db_session.commit()

    with pytest.raises(ValueError, match="active team"):
        ai_conversation_intake.create_draft_policy(
            db_session,
            ai_conversation_intake.AiDraftPolicyCommand(
                context=CommandContext.system(
                    actor="test",
                    scope="ai:intake-policy-draft",
                    reason="reject inactive team",
                ),
                channel_type=InboxChannelType.whatsapp.value,
                provider=WHATSAPP_PROVIDER_META,
                account_scope=account_scope,
                fallback_team_id=inactive_id,
            ),
        )


def test_draft_policy_creation_rejects_existing_draft_without_explicit_replace(
    db_session,
):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    db_session.commit()
    command = ai_conversation_intake.AiDraftPolicyCommand(
        context=CommandContext.system(
            actor="test",
            scope="ai:intake-policy-draft",
            reason="prepare one draft",
        ),
        channel_type=InboxChannelType.whatsapp.value,
        provider=WHATSAPP_PROVIDER_META,
        account_scope=account_scope,
        welcome_message="Hello from the first draft.",
    )
    ai_conversation_intake.create_draft_policy(db_session, command)

    with pytest.raises(ValueError, match="already has an editable draft"):
        ai_conversation_intake.create_draft_policy(db_session, command)

    replaced = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=command.context,
            channel_type=command.channel_type,
            provider=command.provider,
            account_scope=command.account_scope,
            welcome_message="Hello from the replacement draft.",
            replace_existing_draft=True,
        ),
    )
    version = db_session.get(AiIntakePolicyVersion, replaced.version_id)
    assert version.welcome_message == "Hello from the replacement draft."


def test_draft_policy_creation_rejects_unconfigured_or_unsupported_provider_scope(
    db_session,
):
    db_session.commit()
    with pytest.raises(ValueError, match="enabled send and receive capabilities"):
        ai_conversation_intake.create_draft_policy(
            db_session,
            ai_conversation_intake.AiDraftPolicyCommand(
                context=CommandContext.system(
                    actor="test",
                    scope="ai:intake-policy-draft",
                    reason="reject unconfigured provider",
                ),
                channel_type=InboxChannelType.whatsapp.value,
                provider=WHATSAPP_PROVIDER_META,
                account_scope="unconfigured-test-scope",
            ),
        )

    with pytest.raises(ValueError, match="supports only"):
        ai_conversation_intake.create_draft_policy(
            db_session,
            ai_conversation_intake.AiDraftPolicyCommand(
                context=CommandContext.system(
                    actor="test",
                    scope="ai:intake-policy-draft",
                    reason="reject unsupported channel",
                ),
                channel_type=InboxChannelType.email.value,
                provider="smtp",
                account_scope="support@example.test",
            ),
        )


def test_activation_stays_separate_and_requires_fallback_team(db_session):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    technical = _team(db_session, f"Technical {uuid4()}")
    technical_id = technical.id
    db_session.commit()
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="prepare draft without fallback",
    )

    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            welcome_message="Hello from the draft assistant.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )

    with pytest.raises(ValueError, match="requires a fallback team"):
        ai_conversation_intake.activate_policy_version(
            db_session,
            ai_conversation_intake.AiPolicyVersionActivateCommand(
                context=context,
                version_id=draft.version_id,
            ),
        )


def test_policy_validation_reports_errors_without_activating(db_session):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-draft",
                reason="validate only",
            ),
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            welcome_message="Hello from the draft assistant.",
            intent_team_mappings=(),
        ),
    )

    outcome = ai_conversation_intake.validate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-validate",
                reason="validate only",
            ),
            version_id=draft.version_id,
        ),
    )

    policy = db_session.get(AiIntakePolicy, draft.policy_id)
    version = db_session.get(AiIntakePolicyVersion, draft.version_id)
    assert outcome.valid is False
    assert "fallback team" in outcome.errors[0]
    assert policy.is_enabled is False
    assert policy.active_version_id is None
    assert version.status == "draft"


def test_activation_projects_canonical_policy_to_runtime_config(db_session):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, f"Fallback {uuid4()}")
    technical = _team(db_session, f"Technical {uuid4()}")
    fallback_id = fallback.id
    technical_id = technical.id
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-draft",
                reason="prepare activation projection",
            ),
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello from the activated assistant.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "department": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
            escalation_rules={
                "confidence_threshold": 0.82,
                "max_clarification_turns": 2,
                "escalate_after_minutes": 7,
            },
        ),
    )

    activated = ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-activate",
                reason="activate projection",
            ),
            version_id=draft.version_id,
        ),
    )

    policy = db_session.get(AiIntakePolicy, activated.policy_id)
    config = db_session.get(AiIntakeConfig, policy.legacy_config_id)
    assert config.is_enabled is True
    assert config.scope_key == f"{WHATSAPP_PROVIDER_META}:{account_scope}"
    assert config.fallback_team_id == fallback_id
    assert config.confidence_threshold == 0.82
    assert config.max_clarification_turns == 2
    assert config.department_mappings[0]["service_team_id"] == str(technical_id)
    assert config.metadata_["compatibility_source"] == "canonical_ai_intake_policy"
    assert config.metadata_["policy_version_id"] == str(activated.version_id)


def test_saving_replacement_draft_does_not_update_runtime_config(db_session):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    first_fallback = _team(db_session, f"Fallback {uuid4()}")
    second_fallback = _team(db_session, f"Fallback {uuid4()}")
    technical = _team(db_session, f"Technical {uuid4()}")
    first_fallback_id = first_fallback.id
    second_fallback_id = second_fallback.id
    technical_id = technical.id
    db_session.commit()
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="prepare active policy draft edit",
    )
    first = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=first_fallback_id,
            welcome_message="Hello from the first assistant.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )
    activated = ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=first.version_id,
        ),
    )
    policy = db_session.get(AiIntakePolicy, activated.policy_id)
    config = db_session.get(AiIntakeConfig, policy.legacy_config_id)
    assert config.fallback_team_id == first_fallback_id
    db_session.commit()

    replacement = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=second_fallback_id,
            welcome_message="Hello from the replacement assistant.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
            replace_existing_draft=True,
        ),
    )

    policy = db_session.get(AiIntakePolicy, activated.policy_id)
    config = db_session.get(AiIntakeConfig, policy.legacy_config_id)
    assert replacement.version_status == "draft"
    assert policy.active_version_id == activated.version_id
    assert policy.is_enabled is True
    assert config.fallback_team_id == first_fallback_id
    db_session.commit()

    ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=replacement.version_id,
        ),
    )

    config = db_session.get(AiIntakeConfig, policy.legacy_config_id)
    assert config.fallback_team_id == second_fallback_id


def test_disable_prevents_new_sessions_and_keeps_existing_session_version(db_session):
    account_scope = f"test-phone-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, f"Fallback {uuid4()}")
    technical = _team(db_session, f"Technical {uuid4()}")
    fallback_id = fallback.id
    technical_id = technical.id
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-draft",
                reason="prepare disable",
            ),
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello from the activated assistant.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )
    activated = ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-activate",
                reason="activate before disable",
            ),
            version_id=draft.version_id,
        ),
    )
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        status="pending",
        contact_address="2348012345678",
        external_thread_id=f"disable-{uuid4()}",
    )
    db_session.add(conversation)
    db_session.flush()
    session = AiIntakeSession(
        conversation_id=conversation.id,
        policy_id=activated.policy_id,
        policy_version_id=activated.version_id,
        state="awaiting_customer",
        channel_type=InboxChannelType.whatsapp.value,
        provider=WHATSAPP_PROVIDER_META,
        account_scope=account_scope,
        display_name="Dotmac Virtual Assistant",
        turn_count=1,
    )
    db_session.add(session)
    db_session.flush()
    session_id = session.id
    db_session.commit()

    outcome = ai_conversation_intake.disable_policy(
        db_session,
        ai_conversation_intake.AiPolicyDisableCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-disable",
                reason="disable for new sessions",
            ),
            policy_id=activated.policy_id,
        ),
    )

    policy = db_session.get(AiIntakePolicy, activated.policy_id)
    config = db_session.get(AiIntakeConfig, outcome.legacy_config_id)
    session = db_session.get(AiIntakeSession, session_id)
    assert outcome.policy_enabled is False
    assert policy.is_enabled is False
    assert config.is_enabled is False
    assert session.policy_version_id == activated.version_id
    assert session.state == "awaiting_customer"


def test_active_session_remains_pinned_to_original_policy_version(db_session):
    _install_whatsapp_scope(db_session, account_scope="phone-pin")
    fallback = _team(db_session, "Configured Fallback Team")
    technical = _team(db_session, "Configured Technical Team")
    policy = AiIntakePolicy(
        scope_key="meta_cloud_api:phone-pin",
        channel_type=InboxChannelType.whatsapp.value,
        provider="meta_cloud_api",
        account_scope="phone-pin",
        fallback_team_id=fallback.id,
    )
    db_session.add(policy)
    db_session.flush()
    policy_id = policy.id
    technical_id = technical.id
    db_session.commit()
    context = CommandContext.system(
        actor="test", scope="ai:intake-policy-version", reason="test session pin"
    )
    first = ai_conversation_intake.create_or_update_draft_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionDraftCommand(
            context=context,
            policy_id=policy_id,
            welcome_message="Pinned welcome.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )
    ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=first.version_id,
        ),
    )
    conversation = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        status="pending",
        contact_address="2348012345678",
        external_thread_id=f"pin-{uuid4()}",
    )
    db_session.add(conversation)
    db_session.flush()
    session = AiIntakeSession(
        conversation_id=conversation.id,
        policy_id=policy.id,
        policy_version_id=first.version_id,
        state="awaiting_customer",
        channel_type=InboxChannelType.whatsapp.value,
        provider="meta_cloud_api",
        account_scope="phone-pin",
        display_name="Dotmac Virtual Assistant",
        turn_count=1,
    )
    db_session.add(session)
    db_session.flush()
    session_id = session.id
    db_session.commit()

    replacement = ai_conversation_intake.create_or_update_draft_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionDraftCommand(
            context=context,
            policy_id=policy_id,
            base_version_id=first.version_id,
            welcome_message="Replacement welcome.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
        ),
    )
    ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=replacement.version_id,
        ),
    )

    session = db_session.get(AiIntakeSession, session_id)
    assert session.policy_version_id == first.version_id
    policy = db_session.get(AiIntakePolicy, policy_id)
    assert policy.active_version_id == replacement.version_id


def test_policy_activation_rejects_missing_active_intent_mapping(db_session):
    _install_whatsapp_scope(db_session, account_scope="phone-policy-invalid")
    fallback = _team(db_session, "Configured Fallback Team")
    policy = AiIntakePolicy(
        scope_key="meta_cloud_api:phone-policy-invalid",
        channel_type=InboxChannelType.whatsapp.value,
        provider="meta_cloud_api",
        account_scope="phone-policy-invalid",
        fallback_team_id=fallback.id,
    )
    db_session.add(policy)
    db_session.flush()
    policy_id = policy.id
    db_session.commit()
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-version",
        reason="test activation validation",
    )
    draft = ai_conversation_intake.create_or_update_draft_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionDraftCommand(
            context=context,
            policy_id=policy_id,
            welcome_message="Hello from the configured assistant.",
            intent_team_mappings=(),
        ),
    )

    try:
        ai_conversation_intake.activate_policy_version(
            db_session,
            ai_conversation_intake.AiPolicyVersionActivateCommand(
                context=context,
                version_id=draft.version_id,
            ),
        )
    except ValueError as exc:
        assert "active intent mapping" in str(exc)
    else:
        raise AssertionError("activation should reject policies without mappings")


def test_stale_follow_up_recovers_to_configured_fallback_without_assignment(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    fallback_id = fallback.id
    _config(db_session, fallback_team_id=fallback_id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    received = _receive(db_session, message_id="wamid-stale")
    _process_ai(db_session)
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
        == 2
    )
    assert outcome.changed == 1
    assert conversation.primary_service_team_id == fallback_id
    assert conversation.metadata_["ai_intake"]["status"] == "escalated"
    assert conversation.assignments == []


def test_whatsapp_facebook_and_instagram_use_the_same_classifier(
    db_session, monkeypatch
):
    technical = _team(db_session, "Configured Technical Team")
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
    )
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
        scope_key="meta_social:page-fb",
        channel_type=InboxChannelType.facebook_messenger.value,
    )
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
        scope_key="meta_social:page-ig",
        channel_type=InboxChannelType.instagram_dm.value,
    )
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

    _process_ai(db_session)
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
        subscriber_id=_residential_subscriber(db_session).id,
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
        subscriber_id=_residential_subscriber(db_session).id,
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
