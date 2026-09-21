from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.ai_intake import (
    AiIntakeConfig,
    AiIntakeGenerationAttempt,
    AiIntakePolicy,
    AiIntakePolicyVersion,
    AiIntakeSession,
)
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember, ServiceTeamType
from app.models.subscriber import (
    Gender,
    Reseller,
    Subscriber,
    SubscriberCategory,
    UserType,
)
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
    InboxStatusTransitionEvent,
)
from app.services import (
    ai_conversation_intake,
    ai_intake,
    ai_intake_conversation_engine,
    team_inbox_assignment,
    team_inbox_channel_receive,
    team_inbox_maintenance,
    team_inbox_outbound,
)
from app.services.ai.client import AIResponse
from app.services.db_session_adapter import db_session_adapter
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
from tests.staff_identity_fixtures import add_bound_staff_user


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
        self.prompts: list[str] = []

    def generate_with_fallback(self, _db, **kwargs):
        self.calls += 1
        self.prompts.append(str(kwargs.get("prompt") or ""))
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


class _ClassifierFailureGateway(_Gateway):
    def __init__(self, *, invalid: bool = True):
        super().__init__()
        self.invalid = invalid

    def generate_with_fallback(self, db, **kwargs):
        if str(kwargs.get("system") or "").startswith(
            "You compose one concise customer-support response"
        ):
            prompt = json.loads(str(kwargs.get("prompt") or "{}"))
            step = dict(prompt.get("playbook_step") or {})
            return (
                AIResponse(
                    content=json.dumps(
                        {
                            "response_text": (
                                "Hi! I'm here to help. What issue are you "
                                "experiencing with your Dotmac service?"
                            ),
                            "purpose": "clarification",
                            "follow_up_fact_key": step.get("key"),
                            "acknowledges_issue": False,
                        }
                    ),
                    tokens_in=9,
                    tokens_out=12,
                    model="test-composer-model",
                    provider="test-provider",
                ),
                {"endpoint": "primary", "fallback_used": False},
            )
        if self.invalid:
            self.calls += 1
            self.prompts.append(str(kwargs.get("prompt") or ""))
            return (
                AIResponse(
                    content="not-json",
                    tokens_in=6,
                    tokens_out=2,
                    model="test-classifier-model",
                    provider="test-provider",
                ),
                {"endpoint": "primary", "fallback_used": False},
            )
        return super().generate_with_fallback(db, **kwargs)


class _GeneralEnquiryComposerGateway(_Gateway):
    def __init__(self):
        super().__init__(intent="general_enquiry", category="general_enquiry")

    def generate_with_fallback(self, db, **kwargs):
        if str(kwargs.get("system") or "").startswith(
            "You compose one concise customer-support response"
        ):
            prompt = json.loads(str(kwargs.get("prompt") or "{}"))
            step = dict(prompt.get("playbook_step") or {})
            return (
                AIResponse(
                    content=json.dumps(
                        {
                            "response_text": (
                                "I'd be glad to help. Which service would you like "
                                "to know more about?"
                            ),
                            "purpose": "clarification",
                            "follow_up_fact_key": step.get("key"),
                            "acknowledges_issue": True,
                            "acknowledges_frustration": False,
                        }
                    ),
                    tokens_in=9,
                    tokens_out=12,
                    model="test-composer-model",
                    provider="test-provider",
                ),
                {"endpoint": "primary", "fallback_used": False},
            )
        return super().generate_with_fallback(db, **kwargs)


def _identifier_state():
    return ai_intake_conversation_engine.ConversationalState(
        conversation_id=str(uuid4()),
        session_id=str(uuid4()),
        policy_version_id=str(uuid4()),
        channel=InboxChannelType.whatsapp.value,
    )


def test_identifier_prompt_follows_declared_phone_first_order():
    """A phone-first policy is asked phone first.

    This fixture is deliberately NON-CANONICAL relative to the
    portal -> email -> phone order the engine used to impose: if the
    implementation ever reverts to imposing a canonical order, the very first
    assertion here fails.
    """
    state = _identifier_state()
    policy = {
        "permitted_identifiers": (
            "registered_phone",
            "registered_email",
            "portal_id",
        )
    }

    requested = ai_intake_conversation_engine._next_identifier_to_request(state, policy)

    assert requested == "registered_phone"
    assert ai_intake_conversation_engine._identifier_question(requested) == (
        "Please send the registered phone number on the account."
    )

    state.already_requested_fields = ("registered_phone",)
    assert (
        ai_intake_conversation_engine._next_identifier_to_request(state, policy)
        == "registered_email"
    )

    state.already_requested_fields = ("registered_phone", "registered_email")
    assert (
        ai_intake_conversation_engine._next_identifier_to_request(state, policy)
        == "portal_id"
    )

    state.already_requested_fields = (
        "registered_phone",
        "registered_email",
        "portal_id",
    )
    assert (
        ai_intake_conversation_engine._next_identifier_to_request(state, policy) is None
    )

    state.registered_email = "customer@example.test"
    assert (
        ai_intake_conversation_engine._next_identifier_to_request(state, policy) is None
    )


def test_identifier_prompt_follows_a_second_non_default_declared_order():
    """A second non-canonical, non-default order is also obeyed verbatim.

    email -> portal -> phone is neither the historical default nor the
    canonical constant, so no fixed ordering can satisfy both this test and
    the phone-first one above.
    """
    state = _identifier_state()
    policy = {
        "permitted_identifiers": (
            "registered_email",
            "portal_id",
            "registered_phone",
        )
    }

    assert ai_intake_conversation_engine._permitted_identifiers(policy) == (
        "registered_email",
        "portal_id",
        "registered_phone",
    )

    assert (
        ai_intake_conversation_engine._next_identifier_to_request(state, policy)
        == "registered_email"
    )
    state.already_requested_fields = ("registered_email",)
    assert (
        ai_intake_conversation_engine._next_identifier_to_request(state, policy)
        == "portal_id"
    )


def test_identifier_order_de_duplicates_by_first_occurrence():
    policy = {
        "permitted_identifiers": [
            "registered_phone",
            "portal_id",
            "registered_phone",
            "registered_email",
            "portal_id",
        ]
    }

    assert ai_intake_conversation_engine._permitted_identifiers(policy) == (
        "registered_phone",
        "portal_id",
        "registered_email",
    )


def test_identifier_order_drops_invalid_values_without_reordering():
    policy = {
        "permitted_identifiers": [
            "nin",
            "registered_email",
            "",
            "portal_id",
            "meter_number",
            "registered_phone",
        ]
    }

    assert ai_intake_conversation_engine._permitted_identifiers(policy) == (
        "registered_email",
        "portal_id",
        "registered_phone",
    )


def test_identifier_order_defaults_to_the_historical_order_when_absent():
    assert ai_intake_conversation_engine._permitted_identifiers({}) == (
        "registered_phone",
        "registered_email",
        "portal_id",
    )
    assert (
        ai_intake_conversation_engine._permitted_identifiers({})
        == ai_intake_conversation_engine.DEFAULT_IDENTIFIER_REQUEST_ORDER
    )


@pytest.mark.parametrize("configured", [(), [], None, "", ["nin"], "nin"])
def test_identifier_order_defaults_to_the_historical_order_when_empty(configured):
    policy = {"permitted_identifiers": configured}

    assert ai_intake_conversation_engine._permitted_identifiers(policy) == (
        "registered_phone",
        "registered_email",
        "portal_id",
    )


def test_policy_default_identifier_order_is_the_historical_order():
    policy = ai_intake_conversation_engine._policy(None)

    assert ai_intake_conversation_engine._permitted_identifiers(policy) == (
        "registered_phone",
        "registered_email",
        "portal_id",
    )


def test_identifier_lookup_tries_identifiers_in_the_declared_order(monkeypatch):
    """Identification lookup shares the prompting order, not a fixed one.

    The state carries every identifier, so only the declared order decides
    which one `customer_lookup` is called with first. The fixture order is
    phone-first: an implementation that imposes a canonical portal-first order
    fails here even though every prompt-order test still passed.
    """
    attempted: list[str] = []

    def _fake_execute_tool(db, key, inputs, **kwargs):
        assert key == "customer_lookup"
        attempted.append(str(inputs["identifier_type"]))
        return {"status": "not_found"}

    monkeypatch.setattr(
        ai_intake_conversation_engine, "execute_tool", _fake_execute_tool
    )

    state = _identifier_state()
    state.portal_id = "12345"
    state.registered_email = "customer@example.test"
    state.registered_phone = "08030000000"
    state.current_intent = "technical_support"
    state.category = "no_internet"
    conversation = InboxConversation(
        id=uuid4(),
        channel_type=InboxChannelType.whatsapp.value,
    )

    ai_intake_conversation_engine._identify_customer(
        None,
        state=state,
        conversation=conversation,
        policy={
            "permitted_identifiers": (
                "registered_phone",
                "registered_email",
                "portal_id",
            )
        },
        tool_mode="live",
    )

    assert attempted == ["registered_phone", "registered_email", "portal_id"]

    attempted.clear()
    state.subscriber_id = None
    ai_intake_conversation_engine._identify_customer(
        None,
        state=state,
        conversation=conversation,
        policy={"permitted_identifiers": ("portal_id", "registered_phone")},
        tool_mode="live",
    )

    assert attempted == ["portal_id", "registered_phone"]


def test_identifier_prompt_preserves_single_identifier_policy():
    state = _identifier_state()
    policy = {"permitted_identifiers": ("portal_id",)}

    requested = ai_intake_conversation_engine._next_identifier_to_request(state, policy)

    assert requested == "portal_id"
    assert (
        ai_intake_conversation_engine._identifier_question(requested)
        == "Please send your Portal ID or account number so I can identify the service."
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


def _available_member(db_session, team: ServiceTeam):
    user, person = add_bound_staff_user(db_session)
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.add(
        InboxAgentPresence(
            person_id=user.id,
            status=InboxAgentPresenceStatus.online.value,
            last_seen_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    return user.id


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
    customer_response_timeout_minutes: int = 5,
    max_clarification_turns: int = 1,
):
    if fallback_team_id is None and not missing_fallback:
        fallback_team_id = _team(db_session, f"Configured Fallback {uuid4()}").id
    row = AiIntakeConfig(
        scope_key=scope_key,
        channel_type=channel_type,
        is_enabled=True,
        confidence_threshold=threshold,
        allow_followup_questions=True,
        max_clarification_turns=max_clarification_turns,
        escalate_after_minutes=5,
        customer_response_timeout_minutes=customer_response_timeout_minutes,
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


def _enable_langgraph(config: AiIntakeConfig) -> None:
    config.metadata_ = {
        **dict(config.metadata_ or {}),
        "conversational_engine_enabled": True,
        "conversation_engine_mode": "langgraph_v1",
    }


def test_invalid_classifier_output_asks_without_handoff(db_session, monkeypatch):
    fallback = _team(db_session, "Classifier Failure Fallback")
    _available_member(db_session, fallback)
    config = _config(db_session, fallback_team_id=fallback.id)
    _enable_langgraph(config)
    gateway = _ClassifierFailureGateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(
        db_session,
        message_id="classifier-invalid-first",
        body="This is a test",
    )
    _process_ai(db_session, sweeps=1)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    inbound = db_session.get(InboxMessage, received.message_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    state = ai_intake_conversation_engine.ConversationalState.load(
        conversation=conversation,
        session=session,
    )
    classification_attempt = (
        db_session.query(AiIntakeGenerationAttempt)
        .filter(AiIntakeGenerationAttempt.session_id == session.id)
        .filter(AiIntakeGenerationAttempt.message_purpose == "classification")
        .one()
    )
    composition_attempt = (
        db_session.query(AiIntakeGenerationAttempt)
        .filter(AiIntakeGenerationAttempt.session_id == session.id)
        .filter(AiIntakeGenerationAttempt.message_purpose == "response_composition")
        .one()
    )
    customer_responses = [
        message
        for message in (
            db_session.query(InboxMessage)
            .filter(InboxMessage.conversation_id == conversation.id)
            .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
            .all()
        )
        if dict(message.metadata_ or {}).get("ai_message_purpose") == "conversation"
    ]

    assert session.state == "awaiting_customer"
    assert inbound.metadata_["ai_intake_engine"] == "langgraph_v1"
    assert inbound.metadata_["ai_intake_engine_action"] == "respond"
    assert inbound.metadata_["ai_intake_selected_action"] == "ask_question"
    assert inbound.metadata_["ai_intake_engine_reason"] == "classifier_unavailable"
    assert inbound.metadata_["ai_intake_classifier_attempt_status"] == "invalid_output"
    assert inbound.metadata_["ai_intake_classifier_failure_kind"] == (
        "invalid_model_output"
    )
    assert (
        "handle_classifier_unavailable"
        in inbound.metadata_["ai_intake_graph_node_trace"]
    )
    assert "handoff" not in inbound.metadata_["ai_intake_graph_node_trace"]
    assert state.classifier_retry_count == 1
    assert state.classifier_retries_exhausted is False
    assert state.escalation_reason is None
    assert classification_attempt.status == "classification_unavailable"
    assert classification_attempt.error_code == "classifier_invalid_output"
    assert classification_attempt.metadata_["classifier_failure_kind"] == (
        "invalid_model_output"
    )
    assert classification_attempt.provider == "test-provider"
    assert classification_attempt.model == "test-classifier-model"
    assert composition_attempt.status == "generated"
    assert [message.body for message in customer_responses] == [
        "Hi! I'm here to help. What issue are you experiencing with your Dotmac service?"
    ]


def test_classifier_failure_preserves_no_service_facts_and_acknowledges_issue(
    db_session, monkeypatch
):
    config = _config(
        db_session,
        welcome_message="Welcome to Dotmac Support. How can we help today?",
    )
    _enable_langgraph(config)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: _ClassifierFailureGateway())

    received = _receive(
        db_session,
        message_id="classifier-facts-preserved",
        body="7dys no service",
    )
    _process_ai(db_session, sweeps=1)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    inbound = db_session.get(InboxMessage, received.message_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    state = ai_intake_conversation_engine.ConversationalState.load(
        conversation=conversation,
        session=session,
    )
    response = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(
            InboxMessage.metadata_["ai_message_purpose"].as_string() == "conversation"
        )
        .one()
    )
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .order_by(InboxMessage.created_at.asc())
        .all()
    )

    assert state.collected_facts["connectivity_state"] == "down"
    assert state.collected_facts["issue_started_when"] == "for 7 days"
    assert state.selected_question_key == "device_scope"
    assert state.issue_acknowledged is True
    assert "issue_started_when" not in state.candidate_question_keys
    assert inbound.metadata_["ai_intake_classifier_failure_kind"] == (
        "invalid_model_output"
    )
    assert inbound.metadata_["ai_intake_question_key"] == "device_scope"
    assert inbound.metadata_["ai_intake_response_source"] == "template"
    assert inbound.metadata_["ai_intake_validator_result"] == "rejected"
    assert inbound.metadata_["ai_intake_validator_reason"] == (
        "missing_required_issue_acknowledgement"
    )
    assert "whether your request is about" not in response.body.lower()
    assert "connection" in response.body.lower()
    assert "?" in response.body
    assert [message.metadata_["ai_message_purpose"] for message in outbound] == [
        "welcome",
        "conversation",
    ]


def test_general_enquiry_enters_awaiting_customer_without_handoff(
    db_session, monkeypatch, caplog
):
    config = _config(db_session)
    _enable_langgraph(config)
    gateway = _GeneralEnquiryComposerGateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(
        db_session,
        message_id="general-enquiry-policy-follow-up",
        body="I want to make enquiries about your services",
    )
    with caplog.at_level("INFO", logger="app.services.ai_conversation_intake"):
        _process_ai(db_session, sweeps=1)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    inbound = db_session.get(InboxMessage, received.message_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    state = ai_intake_conversation_engine.ConversationalState.load(
        conversation=conversation,
        session=session,
    )

    assert session.state == "awaiting_customer"
    assert state.selected_question_key == "service_interest"
    assert state.selected_priority_source == "default_policy"
    assert conversation.assignments == []
    assert inbound.metadata_["ai_intake_engine_action"] == "respond"
    assert inbound.metadata_["ai_intake_question_key"] == "service_interest"
    assert inbound.metadata_["ai_intake_candidate_question_keys"] == [
        "service_interest",
        "enquiry_topic",
    ]
    assert inbound.metadata_["ai_intake_question_priority"] == 100
    assert inbound.metadata_["ai_intake_priority_source"] == "default_policy"
    assert inbound.metadata_["ai_intake_response_source"] == "model"
    assert inbound.metadata_["ai_intake_validator_result"] == "accepted"
    assert inbound.metadata_["ai_intake_validator_reason"] is None
    assert conversation.assignments == []
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .count()
        == 0
    )
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.internal.value)
        .count()
        == 0
    )
    composition_log = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ai_intake_response_composition_resolved"
    )
    assert composition_log.response_source == "model"
    assert composition_log.validator_result == "accepted"
    assert composition_log.validator_reason is None
    assert composition_log.selected_action == "ask_question"
    assert composition_log.selected_question_key == "service_interest"


def test_classifier_recovers_on_next_message_in_same_session(db_session, monkeypatch):
    config = _config(db_session)
    _enable_langgraph(config)
    gateway = _ClassifierFailureGateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(
        db_session,
        message_id="classifier-recovery-first",
        body="This is a test",
    )
    _process_ai(db_session, sweeps=1)
    initial_session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == UUID(first.conversation_id))
        .one()
    )
    initial_session_id = initial_session.id

    gateway.invalid = False
    second = _receive(
        db_session,
        message_id="classifier-recovery-second",
        body="My internet is not browsing",
    )
    _process_ai(db_session, sweeps=1)

    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == UUID(second.conversation_id))
        .one()
    )
    second_inbound = db_session.get(InboxMessage, second.message_id)
    state = ai_intake_conversation_engine.ConversationalState.load(
        conversation=db_session.get(InboxConversation, second.conversation_id),
        session=session,
    )

    assert session.id == initial_session_id
    assert second_inbound.metadata_["ai_intake_classifier_attempt_status"] == "accepted"
    assert second_inbound.metadata_["ai_intake_engine"] == "langgraph_v1"
    assert second_inbound.metadata_["ai_intake_engine_action"] == "respond"
    assert second_inbound.metadata_["ai_intake_engine_reason"] != (
        "unsupported_or_troubleshooting_exhausted"
    )
    assert session.metadata_["classifier_failure_count"] == 0
    assert session.metadata_["classifier_recovered_after_failures"] == 1
    assert state.current_intent == "technical_support"
    assert state.collected_facts["connectivity_problem"] is True
    assert state.classifier_attempt_status.value == "accepted"


def test_repeated_classifier_failure_hands_off_only_after_limit(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Classifier Retry Fallback")
    _available_member(db_session, fallback)
    config = _config(
        db_session,
        fallback_team_id=fallback.id,
        max_clarification_turns=2,
    )
    _enable_langgraph(config)
    gateway = _ClassifierFailureGateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(
        db_session,
        message_id="classifier-retry-1",
        body="This is a test",
    )
    _process_ai(db_session, sweeps=1)
    conversation = db_session.get(InboxConversation, received.conversation_id)
    assert conversation.assignments == []

    _receive(db_session, message_id="classifier-retry-2", body="Still need help")
    _process_ai(db_session, sweeps=1)
    assert conversation.assignments == []

    final = _receive(db_session, message_id="classifier-retry-3", body="Please help")
    _process_ai(db_session, sweeps=1)
    final_inbound = db_session.get(InboxMessage, final.message_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )

    assert final_inbound.metadata_["ai_intake_engine_action"] == "handoff"
    assert final_inbound.metadata_["ai_intake_engine_reason"] == (
        "classifier_unavailable_after_retries"
    )
    assert final_inbound.metadata_["ai_intake_engine_reason"] != (
        "unsupported_or_troubleshooting_exhausted"
    )
    assert session.metadata_["classifier_retry_count"] == 3
    assert session.metadata_["classifier_retry_limit"] == 2
    assert session.metadata_["classifier_retries_exhausted"] is True
    assert len(conversation.assignments) == 1


def test_human_request_wins_and_preserves_facts_when_classifier_fails(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Human Request Fallback")
    _available_member(db_session, fallback)
    config = _config(db_session, fallback_team_id=fallback.id)
    _enable_langgraph(config)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: _ClassifierFailureGateway())

    received = _receive(
        db_session,
        message_id="classifier-human-request",
        body="My internet is down but I want an agent",
    )
    _process_ai(db_session, sweeps=1)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    inbound = db_session.get(InboxMessage, received.message_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    state = ai_intake_conversation_engine.ConversationalState.load(
        conversation=conversation,
        session=session,
    )

    assert inbound.metadata_["ai_intake_engine_reason"] == "human_requested"
    assert inbound.metadata_["ai_intake_engine_action"] == "handoff"
    assert (
        "handle_classifier_unavailable"
        not in inbound.metadata_["ai_intake_graph_node_trace"]
    )
    assert state.human_requested is True
    assert state.collected_facts["connectivity_problem"] is True
    assert state.collected_facts["connectivity_state"] == "down"
    assert state.escalation_reason == "human_requested"
    assert len(conversation.assignments) == 1


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
    assert (
        outbound[0].metadata_["ai_display_name"]
        == ai_conversation_intake.DEFAULT_DISPLAY_NAME
    )
    reasons = [
        event.reason_code
        for event in db_session.query(InboxStatusTransitionEvent)
        .filter(InboxStatusTransitionEvent.conversation_id == conversation.id)
        .order_by(InboxStatusTransitionEvent.recorded_at.asc())
    ]
    assert reasons == ["ai_intake_started", "ai_handoff_accepted"]


def test_active_ai_session_keeps_following_inbound_in_ai_intake(db_session):
    technical = _team(db_session, "Active AI Session Technical Team")
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
    )

    first = _receive(
        db_session,
        message_id="wamid-active-session-1",
        body="Hello",
    )
    second = _receive(
        db_session,
        message_id="wamid-active-session-2",
        body="Why are you suspending my office account again?",
    )

    first_message = db_session.get(InboxMessage, first.message_id)
    second_message = db_session.get(InboxMessage, second.message_id)
    conversation = db_session.get(InboxConversation, first.conversation_id)

    assert conversation.metadata_["ai_handling"] is True
    assert first_message.metadata_["ai_intake_status"] == "classifying"
    assert second_message.metadata_["ai_intake_status"] == "classifying"
    assert second_message.metadata_["ai_intake_reason"] == "classified"


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

    _process_ai(db_session, sweeps=1)

    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "outbound")
        .count()
        == 2
    )
    assert gateway.calls == 1
    _process_ai(db_session, sweeps=1)
    assert gateway.calls == 1
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "outbound")
        .count()
        == 2
    )


def test_initial_attachment_caption_is_processed_as_turn_one(db_session, monkeypatch):
    technical = _team(db_session, "Configured Technical Team")
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="2348012345678",
            body="image - My internet is not browsing.",
            external_message_id="wamid-ai-caption-turn-one",
            external_thread_id="wa-thread-caption",
            metadata={
                "provider": "meta_cloud_api",
                "provider_account_scope": "phone-1",
                "attachments": [
                    {
                        "type": "image",
                        "id": "media-caption-1",
                        "mime_type": "image/jpeg",
                    }
                ],
            },
        ),
    )
    _process_ai(db_session, sweeps=1)

    message = db_session.get(InboxMessage, result.message_id)
    conversation = db_session.get(InboxConversation, result.conversation_id)
    assert gateway.calls == 1
    assert message.metadata_["attachments"][0]["id"] == "media-caption-1"
    assert message.metadata_["ai_intake_status"] == "classified"
    assert conversation.primary_service_team_id == technical.id


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


def test_gateway_failure_routes_to_fallback_when_retries_are_disabled(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(
        db_session,
        fallback_team_id=fallback.id,
        max_clarification_turns=0,
    )
    gateway = _Gateway(error=TimeoutError("provider timeout"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-timeout")
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    assert conversation.primary_service_team_id == fallback.id
    assert message.metadata_["ai_intake_status"] == "classification_unavailable"
    assert (
        message.metadata_["ai_intake_reason"] == "classifier_unavailable_after_retries"
    )
    assert message.metadata_["routing"]["reason"] == "fallback_route"
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
    gateway = _Gateway(intent="plan_change", category="plan_change_request")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    result = _receive(db_session, message_id="wamid-ai-missing-mapping")
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, result.conversation_id)
    message = db_session.get(InboxMessage, result.message_id)
    assert conversation.primary_service_team_id == fallback.id
    assert message.metadata_["routing"]["reason"] == "ai_intake_missing_team_id"


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
        welcome_message="Welcome to Dotmac Support. How can we help?",
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
        ai_intake.NATURAL_CLARIFICATION_QUESTION
    )
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.direction == "outbound")
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    assert [message.body for message in outbound] == [
        "Welcome to Dotmac Support. How can we help?",
        ai_intake.NATURAL_CLARIFICATION_QUESTION,
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


def test_composable_engine_greeting_only_waits_after_welcome(db_session, monkeypatch):
    account_scope = f"phone-clarify-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, "Composable Clarification Fallback")
    help_desk = _team(db_session, "Composable Clarification Help Desk")
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="test composable clarification before handoff",
    )
    fallback_id = fallback.id
    help_desk_id = help_desk.id
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello from AI intake.",
            intent_definitions=(
                {
                    "key": "general_enquiry",
                    "display_name": "General enquiry",
                    "category": "general_enquiry",
                    "enabled": True,
                    "priority": 1,
                },
            ),
            intent_team_mappings=(
                {
                    "intent": "general_enquiry",
                    "service_team_id": str(help_desk_id),
                    "enabled": True,
                },
            ),
            escalation_rules={
                "confidence_threshold": 0.8,
                "allow_followup_questions": True,
                "max_clarification_turns": 1,
                "escalate_after_minutes": 5,
                "exclude_campaign_attribution": True,
            },
            customer_wait_handoff_policy=(
                ai_conversation_intake.CustomerWaitHandoffPolicy(handoff_minutes=10)
            ),
            conversational_engine_enabled=True,
            permitted_identifiers=("portal_id",),
            tool_config={
                "customer_lookup": {"enabled": False},
                "subscriber_monitoring": {"enabled": False},
            },
            conversation_policy={
                "max_turns": 6,
                "handoff_after_classification": True,
            },
        ),
    )
    draft_version = db_session.get(AiIntakePolicyVersion, draft.version_id)
    assert draft_version.escalation_rules["customer_wait_handoff_minutes"] == 10
    assert "customer_wait_expiry_hours" not in draft_version.escalation_rules
    db_session_adapter.release_read_transaction(db_session)
    ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=draft.version_id,
        ),
    )
    gateway = _Gateway(
        confidence=0.3,
        intent="general_enquiry",
        category="general_enquiry",
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    db_session_adapter.release_read_transaction(db_session)

    received = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="2348012345678",
            body="Hello",
            external_message_id="wamid-composable-clarify",
            external_thread_id=f"thread-{uuid4()}",
            metadata={
                "provider": WHATSAPP_PROVIDER_META,
                "provider_account_scope": account_scope,
            },
        ),
    )
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    first_message = db_session.get(InboxMessage, received.message_id)
    outbound = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "outbound")
        .order_by(InboxMessage.created_at.asc())
        .all()
    )

    assert conversation.primary_service_team_id is None
    assert first_message.metadata_["ai_intake_status"] == "awaiting_customer"
    assert first_message.metadata_["ai_intake_engine_action"] == "wait_for_customer"
    assert first_message.metadata_["ai_intake_engine_reason"] == "greeting_only"
    assert [message.body for message in outbound] == ["Hello from AI intake."]
    assert gateway.calls == 0
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    assert session.state == "awaiting_customer"
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .count()
        == 0
    )


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
    assert calls[1]["body"] == ai_intake.NATURAL_CLARIFICATION_QUESTION
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


def test_draft_policy_creation_accepts_registered_chat_widget_scopes(db_session):
    db_session.commit()
    outcome = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-draft",
                reason="configure first-message Fiber widget intake",
            ),
            channel_type=InboxChannelType.chat_widget.value,
            provider="fiber_website",
            account_scope="fiber.dotmac.ng",
        ),
    )

    assert outcome.channel_type == InboxChannelType.chat_widget.value
    assert outcome.provider == "fiber_website"
    assert outcome.account_scope == "fiber.dotmac.ng"

    native_outcome = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-draft",
                reason="configure first-message customer mobile intake",
            ),
            channel_type=InboxChannelType.chat_widget.value,
            provider="native_widget",
            account_scope="customer_mobile",
        ),
    )
    assert native_outcome.account_scope == "customer_mobile"

    with pytest.raises(ValueError, match="registered Fiber, portal, or mobile-app"):
        ai_conversation_intake.create_draft_policy(
            db_session,
            ai_conversation_intake.AiDraftPolicyCommand(
                context=CommandContext.system(
                    actor="test",
                    scope="ai:intake-policy-draft",
                    reason="reject an unregistered widget scope",
                ),
                channel_type=InboxChannelType.chat_widget.value,
                provider="fiber_website",
                account_scope="other.example.test",
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


def _recover_ai_timeouts(db_session, *, now: datetime):
    if db_session.in_transaction():
        db_session.commit()
    return team_inbox_maintenance.recover_stale_ai_intake(
        db_session,
        team_inbox_maintenance.RecoverStaleAiIntakeCommand(
            context=CommandContext.system(
                actor="task:test-ai-intake-recovery",
                scope="team-inbox:maintenance",
                reason="test stale AI intake recovery",
            ),
            now=now,
        ),
    )


def test_customer_wait_records_10_minute_handoff_and_short_inactivity_noops(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(
        db_session,
        fallback_team_id=fallback.id,
        threshold=0.8,
        customer_response_timeout_minutes=9,
    )
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-wait-deadline")
    _process_ai(db_session, sweeps=1)

    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )
    assert session.state == "awaiting_customer"
    assert session.customer_wait_started_at is not None
    assert session.customer_wait_expires_at == session.expires_at
    assert session.expires_at is not None
    assert session.expires_at - session.customer_wait_started_at == timedelta(
        minutes=10
    )
    assert session.metadata_["customer_wait_handoff_minutes"] == 10
    assert "customer_wait_expiry_hours" not in session.metadata_

    outcome = _recover_ai_timeouts(
        db_session, now=session.customer_wait_started_at + timedelta(minutes=9)
    )

    assert outcome.changed == 0
    assert outcome.skipped == 0
    assert session.state == "awaiting_customer"
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == received.conversation_id)
        .count()
        == 0
    )


def test_customer_silence_hands_off_to_fifo_queue_idempotently(db_session, monkeypatch):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-stale")
    _process_ai(db_session, sweeps=1)
    conversation = db_session.get(InboxConversation, received.conversation_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    due_at = session.expires_at

    outcome = _recover_ai_timeouts(db_session, now=due_at + timedelta(seconds=1))
    repeated = _recover_ai_timeouts(db_session, now=due_at + timedelta(seconds=2))

    conversation = db_session.get(InboxConversation, received.conversation_id)
    session = db_session.get(AiIntakeSession, session.id)
    note = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "internal")
        .one()
    )
    queue_entry = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .one()
    )
    assert outcome.changed == 1
    assert repeated.changed == 0
    assert conversation.primary_service_team_id == fallback.id
    assert conversation.metadata_["ai_intake"]["status"] == "escalated"
    assert conversation.metadata_["ai_intake"]["reason"] == "customer_response_timeout"
    assert conversation.metadata_["ai_handling"] is False
    assert session.state == "completed"
    assert session.completed_at is not None
    assert session.customer_wait_started_at is None
    assert session.customer_wait_expires_at is None
    assert conversation.assignments == []
    assert note.metadata_["source"] == "ai_intake_timeout_handoff"
    assert note.metadata_["destination_team_id"] == str(fallback.id)
    assert queue_entry.service_team_id == fallback.id
    assert queue_entry.queue_position == 1


def test_customer_silence_handoff_respects_existing_fifo_head(db_session, monkeypatch):
    fallback = _team(db_session, "Configured Fallback Team")
    _available_member(db_session, fallback)
    older = InboxConversation(
        channel_type=InboxChannelType.whatsapp.value,
        status=InboxConversationStatus.open.value,
        contact_address="2348099999999",
        external_thread_id=f"older-fifo-{uuid4()}",
    )
    db_session.add(older)
    db_session.flush()
    team_inbox_assignment.queue_conversation_for_team(
        db_session,
        conversation=older,
        service_team_id=fallback.id,
        reason="existing customer waiting first",
    )
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-timeout-fifo-order")
    _process_ai(db_session, sweeps=1)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )

    outcome = _recover_ai_timeouts(
        db_session, now=session.expires_at + timedelta(seconds=1)
    )

    entries = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.service_team_id == fallback.id)
        .order_by(InboxConversationQueueEntry.queue_position.asc())
        .all()
    )
    assert outcome.changed == 1
    assert [entry.conversation_id for entry in entries] == [
        older.id,
        UUID(str(received.conversation_id)),
    ]
    assert [entry.queue_position for entry in entries] == [1, 2]
    assert (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == received.conversation_id)
        .count()
        == 0
    )


def test_customer_reply_after_short_inactivity_resumes_same_ai_session(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    config = _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    config.metadata_ = {
        **dict(config.metadata_ or {}),
        "conversational_engine_enabled": True,
        "conversation_engine_mode": "langgraph_v1",
    }
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-timeout-after-reply-1")
    _process_ai(db_session, sweeps=1)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )
    wait_started = session.customer_wait_started_at

    outcome = _recover_ai_timeouts(db_session, now=wait_started + timedelta(minutes=5))
    gateway.calls = 0
    reply = _receive(
        db_session,
        message_id="wamid-timeout-after-reply-2",
        body="Are you there?",
    )
    _process_ai(db_session, sweeps=1)

    sessions = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .all()
    )
    conversation = db_session.get(InboxConversation, received.conversation_id)
    session = sessions[0]
    assert outcome.changed == 0
    assert len(sessions) == 1
    assert session.state == "awaiting_customer"
    assert session.customer_wait_started_at is not None
    assert session.customer_wait_expires_at is not None
    assert gateway.calls >= 1
    session_metadata = dict(session.metadata_ or {})
    assert session_metadata[f"processed_inbound:{reply.message_id}"] is True
    assert (
        len([key for key in session_metadata if key.startswith("processed_inbound:")])
        == 2
    )
    assert conversation.assignments == []
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .count()
        == 0
    )
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.internal.value)
        .count()
        == 0
    )


def test_customer_silence_assigns_available_agent_after_ten_minutes(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    agent_id = _available_member(db_session, fallback)
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-timeout-assign")
    _process_ai(db_session, sweeps=1)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )

    outcome = _recover_ai_timeouts(
        db_session, now=session.expires_at + timedelta(seconds=1)
    )

    assignment = (
        db_session.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == received.conversation_id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .one()
    )
    session = db_session.get(AiIntakeSession, session.id)
    assert outcome.changed == 1
    assert assignment.person_id == agent_id
    assert assignment.service_team_id == fallback.id
    assert session.state == "completed"
    assert session.metadata_["customer_timeout_assignment_kind"] == "assigned"


def test_newer_customer_reply_cancels_timeout_handoff_and_resumes_ai(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-timeout-race-1")
    _process_ai(db_session, sweeps=1)
    conversation = db_session.get(InboxConversation, first.conversation_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    _receive(
        db_session,
        message_id="wamid-timeout-race-2",
        body="It is still not browsing",
    )

    outcome = _recover_ai_timeouts(
        db_session, now=session.expires_at + timedelta(seconds=1)
    )

    session = db_session.get(AiIntakeSession, session.id)
    assert outcome.changed == 0
    assert outcome.skipped == 1
    assert session.state == "collecting_intent"
    assert session.customer_wait_started_at is None
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "internal")
        .count()
        == 0
    )
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .count()
        == 0
    )


def test_human_takeover_cancels_timeout_handoff(db_session, monkeypatch):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-timeout-human")
    _process_ai(db_session, sweeps=1)
    conversation = db_session.get(InboxConversation, received.conversation_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    db_session.add(
        InboxConversationAssignment(
            conversation_id=conversation.id,
            service_team_id=fallback.id,
            person_id=uuid4(),
            is_active=True,
        )
    )
    db_session.flush()

    outcome = _recover_ai_timeouts(
        db_session, now=session.expires_at + timedelta(seconds=1)
    )

    session = db_session.get(AiIntakeSession, session.id)
    assert outcome.changed == 0
    assert outcome.skipped == 1
    assert session.state == "stopped_human_takeover"
    assert session.customer_wait_started_at is None
    assert session.customer_wait_expires_at is None
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .count()
        == 0
    )


def test_legacy_72_hour_wait_is_normalized_and_handed_off_after_10_minutes(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-timeout-legacy-null")
    _process_ai(db_session, sweeps=1)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )
    wait_started = session.customer_wait_started_at
    session.expires_at = wait_started + timedelta(hours=72)
    session.customer_wait_expires_at = None
    session_metadata = dict(session.metadata_ or {})
    session_metadata["customer_wait_started_at"] = wait_started.isoformat()
    session_metadata.pop("customer_wait_expires_at", None)
    session_metadata.pop("customer_wait_handoff_minutes", None)
    session_metadata["customer_wait_expiry_hours"] = 72
    session.metadata_ = session_metadata
    db_session.flush()
    now = wait_started + timedelta(minutes=10, seconds=1)

    outcome = _recover_ai_timeouts(db_session, now=now)

    session = db_session.get(AiIntakeSession, session.id)
    assert outcome.changed == 1
    assert outcome.skipped == 0
    assert session.state == "completed"
    assert session.customer_wait_expires_at is None
    assert session.expires_at.replace(tzinfo=UTC) == wait_started.replace(
        tzinfo=UTC
    ) + timedelta(minutes=10)
    assert session.metadata_["customer_wait_handoff_minutes"] == 10
    assert "customer_wait_expiry_hours" not in session.metadata_
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == received.conversation_id)
        .filter(InboxMessage.direction == "internal")
        .count()
        == 1
    )
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == received.conversation_id)
        .count()
        == 1
    )


def test_legacy_wait_without_start_receives_fresh_10_minute_window(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-timeout-legacy-no-start")
    _process_ai(db_session, sweeps=1)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )
    session.customer_wait_started_at = None
    session.expires_at = None
    session_metadata = dict(session.metadata_ or {})
    session_metadata.pop("customer_wait_started_at", None)
    session_metadata.pop("customer_wait_handoff_minutes", None)
    session_metadata["customer_wait_expiry_hours"] = 72
    session.metadata_ = session_metadata
    db_session.flush()
    now = datetime.now(UTC) + timedelta(days=1)

    outcome = _recover_ai_timeouts(db_session, now=now)

    session = db_session.get(AiIntakeSession, session.id)
    assert outcome.changed == 1
    assert outcome.skipped == 0
    assert session.state == "awaiting_customer"
    assert session.customer_wait_started_at.replace(tzinfo=UTC) == now
    assert session.expires_at.replace(tzinfo=UTC) == now + timedelta(minutes=10)
    assert session.customer_wait_expires_at.replace(tzinfo=UTC) == now + timedelta(
        minutes=10
    )
    assert session.metadata_["customer_wait_handoff_minutes"] == 10
    assert "customer_wait_expiry_hours" not in session.metadata_


def test_timeout_handoff_assignment_failure_rolls_back_and_retries(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = _receive(db_session, message_id="wamid-timeout-assignment-failure")
    _process_ai(db_session, sweeps=1)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )
    due_at = session.expires_at

    def _assignment_failure(*_args, **_kwargs):
        raise RuntimeError("simulated assignment outage")

    with monkeypatch.context() as patch_context:
        patch_context.setattr(
            team_inbox_assignment,
            "assign_conversation_to_available_agent",
            _assignment_failure,
        )
        failed = _recover_ai_timeouts(db_session, now=due_at + timedelta(seconds=1))

    session = db_session.get(AiIntakeSession, session.id)
    assert failed.changed == 0
    assert failed.skipped == 1
    assert session.state == "awaiting_customer"
    assert "customer_timeout_handoff_key" not in dict(session.metadata_ or {})
    assert session.metadata_["customer_timeout_handoff_failure"]["retryable"] is True
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == received.conversation_id)
        .filter(InboxMessage.direction == "internal")
        .count()
        == 0
    )
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == received.conversation_id)
        .count()
        == 0
    )

    retried = _recover_ai_timeouts(db_session, now=due_at + timedelta(seconds=2))

    session = db_session.get(AiIntakeSession, session.id)
    assert retried.changed == 1
    assert retried.skipped == 0
    assert session.state == "completed"
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == received.conversation_id)
        .filter(InboxMessage.direction == "internal")
        .count()
        == 1
    )
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == received.conversation_id)
        .count()
        == 1
    )


def test_newer_customer_reply_cancelled_timeout_is_selected_by_processor(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = _receive(db_session, message_id="wamid-timeout-reprocess-1")
    _process_ai(db_session, sweeps=1)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == first.conversation_id)
        .one()
    )
    _receive(
        db_session,
        message_id="wamid-timeout-reprocess-2",
        body="Still not browsing",
    )

    outcome = _recover_ai_timeouts(
        db_session, now=session.expires_at + timedelta(seconds=1)
    )
    gateway.calls = 0
    gateway.confidence = 0.95
    processed = _process_ai(db_session, sweeps=1)

    session = db_session.get(AiIntakeSession, session.id)
    assert outcome.changed == 0
    assert outcome.skipped == 1
    assert processed.processed == 1
    assert gateway.calls == 1
    assert session.state == "completed"
    assert session.customer_wait_started_at is None
    assert session.customer_wait_expires_at is None


def test_failed_follow_up_delivery_does_not_enter_customer_wait(
    db_session, monkeypatch
):
    fallback = _team(db_session, "Configured Fallback Team")
    _config(db_session, fallback_team_id=fallback.id, threshold=0.8)
    gateway = _Gateway(confidence=0.2)
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    def _suppressed_follow_up(*_args, **_kwargs):
        return team_inbox_outbound.InboxReplyResult(
            kind="suppressed",
            conversation_id=str(_kwargs["conversation"].id),
            reason="human_takeover",
        )

    monkeypatch.setattr(
        team_inbox_outbound,
        "send_ai_intake_follow_up",
        _suppressed_follow_up,
    )

    received = _receive(db_session, message_id="wamid-follow-up-suppressed")
    result = _process_ai(db_session, sweeps=1)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == received.conversation_id)
        .one()
    )
    assert result.processed == 1
    assert session.state == "completed"
    assert session.customer_wait_started_at is None
    assert session.customer_wait_expires_at is None
    assert conversation.metadata_["ai_handling"] is False
    assert (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == received.conversation_id)
        .count()
        == 1
    )


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
        scope_key="meta.social:page-fb",
        channel_type=InboxChannelType.facebook_messenger.value,
    )
    _config(
        db_session,
        mappings=[_mapping("technical_support", technical, "technical")],
        scope_key="meta.social:page-ig",
        channel_type=InboxChannelType.instagram_dm.value,
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    team_inbox_channel_receive.receive_whatsapp_webhook(
        db_session,
        provider="meta_cloud_api",
        payload={
            "message": {
                "from": "2348012345678",
                "text": "My internet is not browsing.",
                "id": "wa-1",
            },
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
                body="My internet is not browsing.",
                external_message_id=f"{suffix}-1",
                metadata={
                    "provider": "meta_social",
                    "page_or_account_id": f"page-{suffix}",
                },
            ),
        )

    _process_ai(db_session)
    assert gateway.calls == 3
    assert [
        json.loads(prompt)["latest_inbound_message"] for prompt in gateway.prompts
    ] == ["My internet is not browsing."] * 3
    sessions = db_session.query(AiIntakeSession).all()
    assert len(sessions) == 3
    for session in sessions:
        inbound = (
            db_session.query(InboxMessage)
            .filter(InboxMessage.conversation_id == session.conversation_id)
            .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
            .one()
        )
        session_metadata = dict(session.metadata_ or {})
        assert session_metadata["initial_inbound_message_id"] == str(inbound.id)
        assert session_metadata["selected_inbound_message_id"] == str(inbound.id)
        assert session_metadata[f"processed_inbound:{inbound.id}"] is True
        assert (
            len(
                [
                    key
                    for key in session_metadata
                    if key.startswith("processed_inbound:")
                ]
            )
            == 1
        )


def test_engine_resolution_finishes_ai_session_and_resolves_inbox(
    db_session, monkeypatch
):
    config = _config(db_session)
    config.metadata_ = {
        **dict(config.metadata_ or {}),
        "conversational_engine_enabled": True,
    }
    monkeypatch.setattr(ai_intake, "_gateway", lambda: _Gateway())

    def _resolved_decision(
        db,
        *,
        conversation,
        session,
        version,
        latest_body,
        classification,
        **_kwargs,
    ):
        del db, version, latest_body, classification
        state = ai_intake_conversation_engine.ConversationalState.load(
            conversation=conversation,
            session=session,
        )
        state.current_intent = "technical_support"
        state.category = "no_internet"
        state.resolution_status = "resolved"
        return ai_intake_conversation_engine.ConversationEngineDecision(
            action="resolved",
            state=state,
            response_text="Thanks for confirming that the connection is working.",
            metadata={
                "reason": "customer_confirmed_working",
                "next_action": "resolve",
                "response_source": "playbook",
            },
        )

    monkeypatch.setattr(
        ai_intake_conversation_engine,
        "run_conversational_turn",
        _resolved_decision,
    )
    received = _receive(
        db_session,
        message_id="resolved-first",
        body="The connection is working now.",
    )

    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    resolved_event = (
        db_session.query(InboxStatusTransitionEvent)
        .filter(InboxStatusTransitionEvent.conversation_id == conversation.id)
        .filter(InboxStatusTransitionEvent.reason_code == "ai_intake_resolved")
        .one()
    )
    assert session.state == "resolved"
    assert session.completed_at is not None
    assert conversation.status == "resolved"
    assert conversation.metadata_["ai_handling"] is False
    assert resolved_event.status == "resolved"


def test_back_to_back_inbounds_are_processed_oldest_first_exactly_once(
    db_session, monkeypatch
):
    config = _config(db_session)
    config.metadata_ = {
        **dict(config.metadata_ or {}),
        "conversational_engine_enabled": True,
        "conversation_engine_mode": "langgraph_v1",
    }
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    first = _receive(
        db_session,
        message_id="rapid-first",
        body="My internet is not browsing.",
    )
    second = _receive(
        db_session,
        message_id="rapid-second",
        body="It started this morning.",
    )

    _process_ai(db_session, sweeps=1)

    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == UUID(first.conversation_id))
        .one()
    )
    metadata = dict(session.metadata_ or {})
    assert metadata["selected_inbound_message_id"] == first.message_id
    assert metadata[f"processed_inbound:{first.message_id}"] is True
    assert f"processed_inbound:{second.message_id}" not in metadata

    _process_ai(db_session, sweeps=1)

    metadata = dict(session.metadata_ or {})
    assert metadata["selected_inbound_message_id"] == second.message_id
    assert metadata[f"processed_inbound:{second.message_id}"] is True
    for message_id in (first.message_id, second.message_id):
        assert (
            db_session.query(AiIntakeGenerationAttempt)
            .filter(AiIntakeGenerationAttempt.session_id == session.id)
            .filter(AiIntakeGenerationAttempt.inbound_message_id == UUID(message_id))
            .filter(AiIntakeGenerationAttempt.message_purpose == "classification")
            .count()
            == 1
        )


def test_native_widget_first_message_is_classified_once_on_its_first_turn(
    db_session, monkeypatch
):
    config = _config(
        db_session,
        scope_key="native_widget:customer_mobile",
        channel_type=InboxChannelType.chat_widget.value,
    )
    config.metadata_ = {
        **dict(config.metadata_ or {}),
        "conversational_engine_enabled": True,
    }
    conversation = InboxConversation(
        channel_type=InboxChannelType.chat_widget.value,
        contact_address="widget-visitor",
        metadata_={"surface": "customer_mobile"},
    )
    db_session.add(conversation)
    db_session.flush()
    inbound = InboxMessage(
        conversation_id=conversation.id,
        channel_type=InboxChannelType.chat_widget.value,
        direction=InboxMessageDirection.inbound.value,
        body="My internet is not browsing.",
        external_message_id="widget-first-1",
        received_at=datetime.now(UTC),
        metadata_={"provider": "native_widget"},
    )
    db_session.add(inbound)
    db_session.flush()
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    admission = team_inbox_channel_receive.start_ai_intake_for_persisted_widget_message(
        db_session,
        command=team_inbox_channel_receive.PersistedWidgetAiIntakeCommand(
            conversation_id=conversation.id,
            message_id=inbound.id,
            created_conversation=True,
        ),
    )
    assert admission.status.value == "started"

    _process_ai(db_session)

    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )
    session_metadata = dict(session.metadata_ or {})
    assert session_metadata["selected_inbound_message_id"] == str(inbound.id)
    assert session_metadata[f"processed_inbound:{inbound.id}"] is True
    assert (
        db_session.query(AiIntakeGenerationAttempt)
        .filter(AiIntakeGenerationAttempt.session_id == session.id)
        .filter(AiIntakeGenerationAttempt.inbound_message_id == inbound.id)
        .filter(AiIntakeGenerationAttempt.message_purpose == "classification")
        .count()
        == 1
    )


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


def test_composable_handoff_creates_private_note_and_existing_queue_entry(
    db_session, monkeypatch
):
    account_scope = f"phone-handoff-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, "Fallback Team")
    technical = _team(db_session, "Technical Support")
    fallback_id = fallback.id
    technical_id = technical.id
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="test composable human handoff",
    )
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello from AI intake.",
            intent_definitions=(
                {
                    "key": "technical_support",
                    "display_name": "Technical Support",
                    "category": "no_internet",
                    "enabled": True,
                    "priority": 1,
                },
            ),
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
            conversational_engine_enabled=True,
            permitted_identifiers=("portal_id",),
            tool_config={
                "customer_lookup": {"enabled": False},
                "subscriber_monitoring": {"enabled": False},
            },
            conversation_policy={
                "max_turns": 6,
                "require_identity_before_tools": True,
                "handoff": {
                    "customer_message": "I will pass this to a support agent now.",
                    "summary_template": (
                        "AI Intake Summary\n"
                        "Issue: {{issue}}\n"
                        "Reason for escalation: {{escalation_reason}}\n"
                        "Destination: {{destination_team}}"
                    ),
                },
            },
        ),
    )
    ai_conversation_intake.activate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=draft.version_id,
        ),
    )
    gateway = _Gateway()
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    received = team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=InboxChannelType.whatsapp.value,
            contact_address="2348012345678",
            body="Please let me speak to an agent.",
            external_message_id="wamid-composable-human",
            external_thread_id=f"thread-{uuid4()}",
            metadata={
                "provider": WHATSAPP_PROVIDER_META,
                "provider_account_scope": account_scope,
            },
        ),
    )
    _process_ai(db_session)

    conversation = db_session.get(InboxConversation, received.conversation_id)
    note = (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "internal")
        .one()
    )
    queue_entry = (
        db_session.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation.id)
        .one()
    )
    session = (
        db_session.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation.id)
        .one()
    )

    assert note.metadata_["source"] == "ai_intake_handoff"
    assert "AI Intake Summary" in note.body
    assert "Destination: Technical Support" in note.body
    assert "unknown" not in note.body.lower()
    assert queue_entry.service_team_id == technical_id
    assert queue_entry.queue_position == 1
    assert session is not None
    assert session.state == "completed"
    assert session.completed_at is not None
    assert conversation.metadata_["ai_handling"] is False
    assert (
        db_session.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == "outbound")
        .filter(InboxMessage.body == note.body)
        .count()
        == 0
    )


def test_preview_simulation_mode_does_not_execute_live_customer_lookup(
    db_session, monkeypatch
):
    account_scope = f"phone-preview-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, "Preview Fallback")
    technical = _team(db_session, "Preview Technical")
    fallback_id = fallback.id
    technical_id = technical.id
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="test preview safety",
    )
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
            conversational_engine_enabled=True,
            permitted_identifiers=("portal_id",),
            tool_config={"customer_lookup": {"enabled": True}},
            conversation_policy={"max_turns": 6},
        ),
    )
    called = False

    def _live_lookup(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("simulation preview must not query live subscribers")

    monkeypatch.setattr(
        ai_conversation_intake.ai_intake_conversation_engine,
        "_customer_lookup",
        _live_lookup,
    )

    preview = ai_conversation_intake.preview_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyPreviewCommand(
            context=context,
            version_id=draft.version_id,
            customer_message="My Portal ID is 12345 and internet is down.",
            preview_mode="simulation",
        ),
    )

    assert called is False
    assert preview.preview_mode == "simulation"
    assert preview.next_action in {"respond", "handoff", "resolved"}


def test_activation_rejects_first_turn_handoff_rule(db_session):
    account_scope = f"phone-first-turn-handoff-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, "First Turn Handoff Fallback")
    technical = _team(db_session, "First Turn Handoff Technical")
    fallback_id = fallback.id
    technical_id = technical.id
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="test first turn handoff rejection",
    )
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
            conversational_engine_enabled=True,
            permitted_identifiers=("portal_id",),
            tool_config={"customer_lookup": {"enabled": True}},
            conversation_policy={
                "max_turns": 6,
                "troubleshooting_rules": [
                    {
                        "condition": {
                            "type": "turn_count",
                            "operator": ">=",
                            "value": 0,
                        },
                        "action": "handoff",
                    }
                ],
            },
        ),
    )

    outcome = ai_conversation_intake.validate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=draft.version_id,
        ),
    )

    assert outcome.valid is False
    assert "must not match the first turn" in outcome.errors[0]


def test_activation_rejects_invalid_composable_rule_action(db_session):
    account_scope = f"phone-invalid-rule-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, "Invalid Rule Fallback")
    technical = _team(db_session, "Invalid Rule Technical")
    fallback_id = fallback.id
    technical_id = technical.id
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="test invalid composable rule",
    )
    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello.",
            intent_team_mappings=(
                {
                    "intent": "technical_support",
                    "service_team_id": str(technical_id),
                    "enabled": True,
                },
            ),
            conversational_engine_enabled=True,
            permitted_identifiers=("portal_id",),
            tool_config={"customer_lookup": {"enabled": True}},
            conversation_policy={
                "max_turns": 6,
                "troubleshooting_rules": [
                    {
                        "condition": {"type": "intent", "intent": "technical_support"},
                        "action": "run_python",
                    }
                ],
            },
        ),
    )

    outcome = ai_conversation_intake.validate_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyVersionActivateCommand(
            context=context,
            version_id=draft.version_id,
        ),
    )

    assert outcome.valid is False
    assert "rule action is unsupported" in outcome.errors[0]


def test_draft_policy_persists_langgraph_engine_mode(db_session):
    account_scope = f"phone-langgraph-mode-{uuid4().hex}"
    _install_whatsapp_scope(db_session, account_scope=account_scope)
    fallback = _team(db_session, "LangGraph Mode Fallback")
    fallback_id = fallback.id
    context = CommandContext.system(
        actor="test",
        scope="ai:intake-policy-draft",
        reason="test langgraph mode persistence",
    )

    db_session.commit()
    draft = ai_conversation_intake.create_draft_policy(
        db_session,
        ai_conversation_intake.AiDraftPolicyCommand(
            context=context,
            channel_type=InboxChannelType.whatsapp.value,
            provider=WHATSAPP_PROVIDER_META,
            account_scope=account_scope,
            fallback_team_id=fallback_id,
            welcome_message="Hello.",
            conversational_engine_enabled=True,
            conversation_engine_mode="langgraph_v1",
            permitted_identifiers=("portal_id",),
            tool_config={"customer_lookup": {"enabled": True}},
            conversation_policy={"max_turns": 6},
        ),
    )

    version = db_session.get(AiIntakePolicyVersion, draft.version_id)

    assert version.metadata_["conversational_engine_enabled"] is True
    assert version.metadata_["conversation_engine_mode"] == "langgraph_v1"
