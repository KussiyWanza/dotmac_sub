from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.ai_intake import AiIntakePolicy, AiIntakePolicyVersion, AiIntakeSession
from app.models.subscriber import (
    Gender,
    Reseller,
    Subscriber,
    SubscriberCategory,
    UserType,
)
from app.models.team_inbox import InboxChannelType, InboxConversation
from app.schemas.ai_intake import (
    AiClassifierAttempt,
    AiClassifierAttemptStatus,
    AiClassifierFailureKind,
    AiIntakeAffectLevel,
    AiIntakeCategory,
    AiIntakeClassification,
    AiIntakeExtractedFacts,
    AiIntakeIntent,
    AiIntakeReason,
)
from app.services import (
    ai_conversation_intake,
    ai_intake_graph,
)
from app.services import (
    ai_intake_conversation_engine as engine,
)
from app.services.network import support_monitoring
from app.services.owner_commands import CommandContext


def _subscriber(db_session, *, email: str | None = None, phone: str | None = None):
    reseller = db_session.query(Reseller).filter(Reseller.is_house.is_(True)).first()
    if reseller is None:
        reseller = Reseller(name=f"Engine House {uuid4()}", is_house=True)
        db_session.add(reseller)
        db_session.flush()
    row = Subscriber(
        email=email or f"engine-{uuid4()}@example.test",
        phone=phone,
        first_name="Engine",
        last_name="Customer",
        user_type=UserType.customer,
        reseller_id=reseller.id,
        gender=Gender.unknown,
    )
    row.category = SubscriberCategory.residential
    row.account_number = f"DM-{uuid4().hex[:6]}"
    db_session.add(row)
    db_session.flush()
    return row


def _conversation(db_session, *, subscriber_id=None):
    row = InboxConversation(
        subscriber_id=subscriber_id,
        channel_type=InboxChannelType.whatsapp.value,
        status="pending",
        contact_address="2348012345678",
        external_thread_id=f"thread-{uuid4()}",
        metadata_={},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _version(db_session, *, metadata=None):
    policy = AiIntakePolicy(
        scope_key=f"meta_cloud_api:{uuid4().hex}",
        channel_type=InboxChannelType.whatsapp.value,
        provider="meta_cloud_api",
        account_scope=f"phone-{uuid4().hex}",
        display_name="Dotmac Virtual Assistant",
        is_enabled=True,
    )
    db_session.add(policy)
    db_session.flush()
    version = AiIntakePolicyVersion(
        policy_id=policy.id,
        version_number=1,
        status="activated",
        is_active=True,
        display_name="Dotmac Virtual Assistant",
        welcome_message="Hello",
        metadata_={
            "conversational_engine_enabled": True,
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": True},
            },
            "permitted_identifiers": [
                "registered_phone",
                "registered_email",
                "portal_id",
            ],
            "conversation_policy": {
                "max_turns": 6,
                "require_identity_before_tools": True,
            },
            **dict(metadata or {}),
        },
    )
    db_session.add(version)
    db_session.flush()
    policy.active_version_id = version.id
    return version


def _session(db_session, conversation, version, *, metadata=None, expires_at=None):
    row = AiIntakeSession(
        conversation_id=conversation.id,
        policy_id=version.policy_id,
        policy_version_id=version.id,
        state="collecting_intent",
        channel_type=conversation.channel_type,
        provider="meta_cloud_api",
        account_scope="phone-1",
        display_name="Dotmac Virtual Assistant",
        max_turns=6,
        confidence_threshold=0.75,
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
        metadata_=dict(metadata or {}),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _classification(
    intent="technical_support",
    category="no_internet",
    confidence=0.95,
    *,
    requires_follow_up: bool = False,
    follow_up_question: str | None = None,
    message_facts: AiIntakeExtractedFacts | None = None,
):
    return AiIntakeClassification(
        intent=AiIntakeIntent(intent),
        category=AiIntakeCategory(category),
        confidence=confidence,
        department=None,
        requires_follow_up=requires_follow_up,
        follow_up_question=follow_up_question,
        summary="Customer needs support.",
        message_facts=message_facts or AiIntakeExtractedFacts(),
    )


def test_identified_subscriber_does_not_request_portal_id(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)
    monkeypatch.setattr(
        engine.support_monitoring,
        "project_support_monitoring",
        lambda *_args: support_monitoring.SupportMonitoringProjection(
            support_monitoring.SupportMonitoringStatus.no_data
        ),
    )

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet has not been browsing since morning.",
        classification=_classification(),
    )

    assert decision.action == "respond"
    assert decision.metadata["question_key"] == "device_scope"
    assert decision.state.collected_facts.get("device_scope") is None
    assert "every device" not in (decision.response_text or "").lower()
    assert "Portal ID" not in (decision.response_text or "")
    assert decision.state.subscriber_id == str(subscriber.id)
    assert decision.state.monitoring_results == [{"status": "no_data"}]
    assert any(
        item["tool"] == "subscriber_monitoring" and item["status"] == "no_data"
        for item in decision.state.tool_executions
    )


def test_portal_id_requested_only_when_needed_and_not_repeated(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={"permitted_identifiers": ["portal_id"]},
    )
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is down.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Still down.",
        classification=_classification(),
    )

    assert first.action == "respond"
    assert "Portal ID" in (first.response_text or "")
    assert "portal_id" in first.state.already_requested_fields
    assert second.action == "handoff"


def test_billing_issue_requests_account_identifier_before_handoff(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={
            "permitted_identifiers": ["portal_id"],
            "conversation_policy": {
                "max_turns": 6,
                "require_identity_before_tools": True,
                "handoff_after_classification": True,
            },
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Why are you suspending my office account again?",
        classification=_classification(
            intent=AiIntakeIntent.billing_issue,
            category=AiIntakeCategory.other_billing_issue,
        ),
    )

    assert decision.action == "respond"
    assert "Portal ID" in (decision.response_text or "")
    assert decision.metadata["reason"] == "missing_customer_identifier"
    assert decision.state.collected_facts["account_status_problem"] is True
    assert decision.state.collected_facts["organization_account"] is True
    assert decision.state.handoff_status == "not_requested"


def test_identifier_prompt_retries_when_customer_replies_without_identifier(
    db_session,
):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={
            "conversation_policy": {
                "max_turns": 1,
                "require_identity_before_tools": True,
            },
        },
    )
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Please can you check our internet is very poor.",
        classification=_classification(category=AiIntakeCategory.slow_internet),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="\U0001f446",
        classification=_classification(category=AiIntakeCategory.slow_internet),
    )

    assert first.action == "respond"
    assert first.metadata["reason"] == "missing_customer_identifier"
    assert second.action == "respond"
    assert second.metadata["reason"] == "identifier_reply_missing_value"
    # The fixture policy declares phone -> email -> portal, a NON-CANONICAL
    # order: the retry must name the first DECLARED identifier, not a fixed one.
    assert "registered phone number" in (second.response_text or "")
    assert second.state.handoff_status == "not_requested"
    assert second.state.collected_facts["missing_identifier_retry_count"] == 1


def test_unlinked_customer_portal_id_does_not_trigger_directory_search(db_session):
    subscriber = _subscriber(db_session)
    subscriber.account_number = "12345"
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={"permitted_identifiers": ["portal_id"]},
    )
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is not browsing.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My Portal ID is 12345.",
        classification=_classification(),
    )

    assert first.action == "respond"
    assert "Portal ID" in (first.response_text or "")
    assert second.state.portal_id == "12345"
    assert second.action == "handoff"
    assert second.state.subscriber_id is None
    assert "portal_id" in second.state.already_requested_fields
    assert any(
        item["tool"] == "customer_lookup" and item["status"] == "not_found"
        for item in second.state.tool_executions
    )


def test_registered_email_lookup_only_verifies_linked_customer(db_session):
    subscriber = _subscriber(db_session, email="lookup@example.test")
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    result = engine.execute_tool(
        db_session,
        "customer_lookup",
        {
            "identifier_type": "registered_email",
            "identifier_value": "lookup@example.test",
        },
        policy={"tools": {"customer_lookup": {"enabled": True}}},
        conversation=conversation,
    )

    assert result["status"] == "found"
    assert result["subscriber_id"] == str(subscriber.id)
    assert set(result) == {
        "status",
        "subscriber_id",
        "display_name",
        "account_number",
        "subscriber_status",
    }


def test_phone_email_and_portal_id_only_verify_linked_customer(db_session):
    subscriber = _subscriber(
        db_session,
        email="verified@example.test",
        phone="2348012345678",
    )
    subscriber.account_number = "PORTAL-123"
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    policy = {"tools": {"customer_lookup": {"enabled": True}}}

    for identifier_type, identifier_value in (
        ("registered_phone", "2348012345678"),
        ("registered_email", "VERIFIED@example.test"),
        ("portal_id", "PORTAL-123"),
    ):
        result = engine.execute_tool(
            db_session,
            "customer_lookup",
            {
                "identifier_type": identifier_type,
                "identifier_value": identifier_value,
            },
            policy=policy,
            conversation=conversation,
        )
        assert result["status"] == "found"
        assert result["subscriber_id"] == str(subscriber.id)


def test_monitoring_projection_preserves_owner_provenance(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    observed_at = datetime.now(UTC)

    def _projection(_db, query):
        assert query.subscriber_id == subscriber.id
        assert query.authorized is True
        return support_monitoring.SupportMonitoringProjection(
            support_monitoring.SupportMonitoringStatus.available,
            radius=support_monitoring.RadiusObservation(
                state="online",
                active_session_count=2,
                framed_ip_addresses=("10.0.0.2",),
                observed_at=observed_at,
            ),
            onts=(
                support_monitoring.OntObservation(
                    reference="ont-1",
                    serial_number="SERIAL-1",
                    effective_state="offline",
                ),
            ),
        )

    monkeypatch.setattr(
        engine.support_monitoring, "project_support_monitoring", _projection
    )
    result = engine.execute_tool(
        db_session,
        "subscriber_monitoring",
        {"subscriber_id": str(subscriber.id)},
        policy={"tools": {"subscriber_monitoring": {"enabled": True}}},
    )

    assert result["status"] == "available"
    assert result["radius_observation"] == {
        "source": "network.radius_sessions",
        "state": "online",
        "active_session_count": 2,
        "framed_ip_addresses": ["10.0.0.2"],
        "observed_at": observed_at.isoformat(),
    }
    assert result["ont_observations"] == [
        {
            "source": "network.ont_runtime_status",
            "reference": "ont-1",
            "serial_number": "SERIAL-1",
            "effective_state": "offline",
        }
    ]
    assert not {"los", "outage", "cpe_diagnostics", "sla"} & set(result)


def test_monitoring_no_data_and_unavailable_are_not_offline(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    policy = {"tools": {"subscriber_monitoring": {"enabled": True}}}

    for status in (
        support_monitoring.SupportMonitoringStatus.no_data,
        support_monitoring.SupportMonitoringStatus.unavailable,
    ):
        monkeypatch.setattr(
            engine.support_monitoring,
            "project_support_monitoring",
            lambda *_args, status=status: (
                support_monitoring.SupportMonitoringProjection(status)
            ),
        )
        result = engine.execute_tool(
            db_session,
            "subscriber_monitoring",
            {"subscriber_id": str(subscriber.id)},
            policy=policy,
        )
        state = engine.ConversationalState(
            conversation_id=str(uuid4()),
            session_id=str(uuid4()),
            policy_version_id=None,
            channel="whatsapp",
            monitoring_results=[result],
        )
        assert result == {"status": status.value}
        assert engine._monitoring_offline(state) is False


def test_first_turn_handoff_rule_does_not_override_general_enquiry_plan(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={
            "conversation_policy": {
                "max_turns": 6,
                "troubleshooting_rules": [
                    {
                        "condition": {
                            "type": "turn_count",
                            "operator": ">=",
                            "value": 0,
                        },
                        "action": "handoff",
                        "reason": "bad_immediate_handoff",
                    }
                ],
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I need to ask a general support question.",
        classification=_classification(
            intent=AiIntakeIntent.general_enquiry,
            category=AiIntakeCategory.general_enquiry,
        ),
    )

    assert decision.action == "respond"
    assert decision.metadata["reason"] == "useful_missing_fact"
    assert decision.metadata["question_key"] == "service_interest"
    assert decision.metadata["reason"] != "bad_immediate_handoff"


def test_rich_first_message_extracts_existing_facts(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body=(
            "My Portal ID is DM-12345. My internet is down since yesterday "
            "and I have restarted the router twice."
        ),
        classification=_classification(),
    )

    assert decision.state.portal_id == "DM-12345"
    assert decision.state.collected_facts["connectivity_problem"] is True
    assert decision.state.collected_facts["outage_context"] == "since yesterday"
    assert decision.state.collected_facts["router_restarted"] is True
    assert "portal_id" not in decision.state.already_requested_fields
    assert "router_restarted" not in decision.state.already_requested_fields


def test_configured_no_internet_playbook_asks_first_line_steps(db_session, monkeypatch):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": True},
            },
            "conversation_policy": {
                "max_turns": 6,
                "require_identity_before_tools": True,
                "first_line_playbooks": [
                    {
                        "key": "technical_support_no_internet",
                        "intent": "technical_support",
                        "category": "no_internet",
                        "acknowledgement": "Sorry about the downtime.",
                        "steps": [
                            {
                                "action": "request_field",
                                "field": "router_powered",
                                "response": "Is your router or ONU powered on right now?",
                            },
                            {
                                "condition": {
                                    "type": "field_value",
                                    "field": "router_powered",
                                    "value": True,
                                },
                                "action": "request_field",
                                "field": "los_status",
                                "response": "Are you seeing any red LOS warning light?",
                            },
                        ],
                    }
                ],
            },
        },
    )
    session = _session(db_session, conversation, version)
    monitoring_calls: list[str] = []
    monkeypatch.setattr(
        engine.support_monitoring,
        "project_support_monitoring",
        lambda *_args: monitoring_calls.append("called"),
    )

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="No internet for the past one month.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="The router is powered on.",
        classification=_classification(),
    )

    assert first.action == "respond"
    assert first.metadata["reason"] == "playbook_required_field"
    assert "Sorry about the downtime." in (first.response_text or "")
    assert "router or ONU powered" in (first.response_text or "")
    assert "router_powered" in first.state.already_requested_fields
    assert second.action == "respond"
    assert "red LOS" in (second.response_text or "")
    assert "Sorry about the downtime." not in (second.response_text or "")
    assert "los_state" in second.state.already_requested_fields
    assert second.metadata["question_key"] == "los_status"
    assert second.metadata["expected_fact"] == "los_state"
    assert monitoring_calls == []


def test_no_internet_without_playbook_does_not_auto_handoff_after_classification(
    db_session,
):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": False},
            },
            "conversation_policy": {
                "max_turns": 6,
                "require_identity_before_tools": True,
            },
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="No internet.",
        classification=_classification(),
    )

    assert decision.action == "respond"
    assert decision.metadata["reason"] == "useful_missing_fact"
    assert decision.metadata["question_key"] == "issue_started_when"


def test_monitoring_troubleshooting_then_red_los_handoff_retains_state(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Internet is down and I restarted the router twice.",
        classification=_classification(),
        tool_mode="simulation",
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Router is powered and LOS is red.",
        classification=_classification(),
        tool_mode="simulation",
    )

    assert first.action == "respond"
    assert first.metadata["question_key"] == "issue_started_when"
    assert "problem start" in (first.response_text or "").lower()
    assert second.action == "handoff"
    assert second.state.collected_facts["router_restarted"] is True
    assert second.state.collected_facts["router_powered"] is True
    assert second.state.collected_facts["los_red"] is True
    assert second.state.escalation_reason == "red_los"


def test_monitoring_unavailable_continues_without_diagnosis_by_default(
    db_session, monkeypatch
):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("monitoring down")

    monkeypatch.setattr(
        engine.support_monitoring,
        "project_support_monitoring",
        _raise,
    )
    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="No internet.",
        classification=_classification(),
    )

    assert decision.action == "respond"
    assert decision.state.escalation_reason is None
    assert decision.metadata["question_key"] == "issue_started_when"
    assert decision.state.tool_errors[-1]["status"] == "unavailable"


def test_monitoring_unavailable_hands_off_only_when_policy_requires_it(
    db_session, monkeypatch
):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": True},
            },
            "conversation_policy": {
                "tool_failure_handoff_statuses": {
                    "subscriber_monitoring": ["unavailable"]
                }
            },
        },
    )
    session = _session(db_session, conversation, version)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("monitoring down")

    monkeypatch.setattr(
        engine.support_monitoring,
        "project_support_monitoring",
        _raise,
    )
    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="No internet.",
        classification=_classification(),
    )

    assert decision.action == "handoff"
    assert decision.state.escalation_reason == "monitoring_unavailable"


def test_duplicate_customer_identifiers_are_not_a_directory_search(db_session):
    _subscriber(db_session, email="shared@example.test")
    _subscriber(db_session, email="shared@example.test")
    conversation = _conversation(db_session)
    result = engine.execute_tool(
        db_session,
        "customer_lookup",
        {
            "identifier_type": "registered_email",
            "identifier_value": "shared@example.test",
        },
        policy={"tools": {"customer_lookup": {"enabled": True}}},
        conversation=conversation,
    )

    assert result == {"status": "not_found"}


def test_red_los_escalates(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="The router is on. LOS is red.",
        classification=_classification(),
    )

    assert decision.action == "handoff"
    assert decision.state.escalation_reason == "red_los"
    assert "LOS" in (decision.handoff_summary or "")


def test_configuration_driven_troubleshooting_rule(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "conversation_policy": {
                "troubleshooting_rules": [
                    {
                        "condition": {"fact": "router_restarted", "equals": True},
                        "action": "handoff",
                        "reason": "restart_completed",
                        "response": "I will pass this to support now.",
                    }
                ]
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I restarted the router twice.",
        classification=_classification(category="router_issue"),
    )

    assert decision.action == "handoff"
    assert decision.state.escalation_reason == "restart_completed"


def test_multiple_turns_and_intent_change_are_persisted(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is not working.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)
    second = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Actually it works but has been very slow for three days.",
        classification=_classification(category="slow_internet"),
    )

    assert second.state.current_intent == "technical_support"
    assert second.state.category == "slow_internet"
    assert second.state.collected_facts["slow_internet"] is True
    assert second.state.collected_facts["connectivity_problem"] is False
    assert second.state.turn_count == 2


def test_explicit_human_request_escalates(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I want to speak with a human agent.",
        classification=_classification(),
    )

    assert decision.action == "handoff"
    assert decision.state.human_requested is True
    assert decision.state.escalation_reason == "human_requested"


def test_langgraph_classifier_unavailable_asks_and_preserves_facts(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={"conversation_engine_mode": "langgraph_v1"},
    )
    session = _session(db_session, conversation, version)

    decision = ai_intake_graph.run_ai_intake_graph(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="7dys no service",
        classification=None,
        classifier_attempt=AiClassifierAttempt(
            status=AiClassifierAttemptStatus.invalid_output,
            reason=AiIntakeReason.classifier_invalid_output,
            failure_kind=AiClassifierFailureKind.invalid_model_output,
            retry_count=1,
            retry_limit=2,
            provider="test-provider",
            model="test-model",
        ),
        tool_mode="simulation",
    )

    assert decision.action == "respond"
    assert decision.metadata["next_action"] == "ask_question"
    assert decision.metadata["reason"] == "classifier_unavailable"
    assert "handle_classifier_unavailable" in decision.metadata["node_trace"]
    assert "handoff" not in decision.metadata["node_trace"]
    assert decision.state.collected_facts["connectivity_state"] == "down"
    assert decision.state.collected_facts["connectivity_problem"] is True
    assert decision.state.collected_facts["issue_started_when"] == "for 7 days"
    assert decision.state.current_intent == "technical_support"
    assert decision.state.category == "no_internet"
    assert decision.state.issue_acknowledgement_required is True
    assert decision.metadata["question_key"] == "device_scope"
    assert decision.metadata["fact_driven_path"] is True
    assert "whether your request is about" not in decision.response_text.lower()
    assert decision.state.classifier_failure_reason is (
        AiIntakeReason.classifier_invalid_output
    )


@pytest.mark.parametrize(
    ("text", "expected_state", "expected_duration"),
    [
        ("7dys no service", "down", "for 7 days"),
        ("No internet for 7 days", "down", "for 7 days"),
        ("Internet down 2hrs", "down", "for 2 hours"),
        ("Network down for 1wk", "down", "for 1 week"),
        ("Not browsing since yesterday", "down", "since yesterday"),
        ("The internet is not working for 3 hours", "down", "for 3 hours"),
    ],
)
def test_bounded_support_shorthand_normalizes_typed_facts(
    text, expected_state, expected_duration
):
    facts = engine.extract_facts(text)

    assert facts["connectivity_state"] == expected_state
    assert facts["connectivity_problem"] is True
    assert facts["issue_started_when"] == expected_duration


def test_slow_internet_duration_is_retained_and_not_asked_again(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet has been slow for 3 days",
        classification=_classification(category="slow_internet"),
        tool_mode="simulation",
    )

    assert decision.state.collected_facts["issue_started_when"] == "for 3 days"
    assert decision.metadata["question_key"] == "device_scope"
    assert "issue_started_when" not in decision.state.candidate_question_keys


def test_langgraph_classifier_unavailable_hands_off_after_retry_limit(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={"conversation_engine_mode": "langgraph_v1"},
    )
    session = _session(db_session, conversation, version)

    decision = ai_intake_graph.run_ai_intake_graph(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Still need help",
        classification=None,
        classifier_attempt=AiClassifierAttempt(
            status=AiClassifierAttemptStatus.unavailable,
            reason=AiIntakeReason.classifier_unavailable,
            failure_kind=AiClassifierFailureKind.classifier_unavailable,
            retry_count=3,
            retry_limit=2,
            retries_exhausted=True,
        ),
        tool_mode="simulation",
    )

    assert decision.action == "handoff"
    assert decision.metadata["reason"] == "classifier_unavailable_after_retries"
    assert decision.state.escalation_reason == "classifier_unavailable_after_retries"
    assert "handle_classifier_unavailable" in decision.metadata["node_trace"]


def test_langgraph_human_request_precedes_classifier_unavailable(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={"conversation_engine_mode": "langgraph_v1"},
    )
    session = _session(db_session, conversation, version)

    decision = ai_intake_graph.run_ai_intake_graph(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is down but I want an agent",
        classification=None,
        classifier_attempt=AiClassifierAttempt(
            status=AiClassifierAttemptStatus.invalid_output,
            reason=AiIntakeReason.classifier_invalid_output,
            failure_kind=AiClassifierFailureKind.schema_validation_failure,
            retry_count=1,
            retry_limit=2,
        ),
        tool_mode="simulation",
    )

    assert decision.action == "handoff"
    assert decision.metadata["reason"] == "human_requested"
    assert decision.state.human_requested is True
    assert decision.state.collected_facts["connectivity_state"] == "down"
    assert "handle_classifier_unavailable" not in decision.metadata["node_trace"]


def test_follow_up_classification_is_not_handed_off_by_engine(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={
            "conversation_policy": {
                "max_turns": 6,
                "handoff_after_classification": True,
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Hello",
        classification=_classification(
            intent="general_enquiry",
            category="general_enquiry",
            confidence=0.2,
            requires_follow_up=True,
            follow_up_question="Please tell me what you need help with today.",
        ),
    )

    assert decision.action == "respond"
    assert decision.metadata["question_key"] == "intent_clarification"
    assert decision.state.classification_requires_follow_up is True
    assert decision.state.classification_follow_up_question == (
        "Please tell me what you need help with today."
    )


def test_turn_limit_escalates_but_session_expiry_does_not_handoff(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session, metadata={"conversation_policy": {"max_turns": 1}})
    session = _session(db_session, conversation, version)
    state = engine.ConversationalState.load(conversation=conversation, session=session)
    state.turn_count = 1
    engine.persist_state(session, state)

    limited = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Still not working.",
        classification=_classification(),
    )

    expired_conversation = _conversation(db_session)
    expired_session = _session(
        db_session,
        expired_conversation,
        version,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    timed_out = engine.run_conversational_turn(
        db_session,
        conversation=expired_conversation,
        session=expired_session,
        version=version,
        latest_body="Still not working.",
        classification=_classification(),
    )

    assert limited.state.escalation_reason == "turn_limit"
    assert timed_out.state.escalation_reason != "timeout"
    assert timed_out.action == "respond"


def test_slow_issue_skips_facts_already_supplied_and_asks_one_useful_question(
    db_session,
):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": False},
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet has been slow on every device since yesterday.",
        classification=_classification(category="slow_internet"),
    )

    assert decision.action == "respond"
    assert decision.metadata["question_key"] == "connection_medium"
    assert "every device" not in (decision.response_text or "").lower()
    assert (decision.response_text or "").count("?") == 1
    assert decision.state.collected_facts["device_scope"] == "all_devices"
    assert decision.state.collected_facts["issue_started_when"] == "since yesterday"


def test_strong_profanity_sets_bounded_affect_and_acknowledgement_obligation():
    affect = engine.detect_affect("I'm fucking fed up of you guys")
    state = engine.ConversationalState(
        conversation_id=str(uuid4()),
        session_id=str(uuid4()),
        policy_version_id=None,
        channel="whatsapp",
    )

    engine._merge_affect(state, affect, fresh_signal=True)

    assert affect.frustration_level is AiIntakeAffectLevel.high
    assert affect.agitation_level is AiIntakeAffectLevel.high
    assert state.acknowledgement_required is True
    assert state.frustration_acknowledged is False
    assert state.affect_sources == ["deterministic"]


def test_new_frustration_is_acknowledged_separately_from_known_issue(
    db_session,
):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": False},
            }
        },
    )
    session = _session(db_session, conversation, version)
    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is slow.",
        classification=_classification(category="slow_internet"),
    )
    first.state.issue_acknowledged = True
    first.state.frustration_acknowledged = False
    engine.persist_state(session, first.state)

    angry = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I'm fucking fed up of you guys",
        classification=_classification(category="slow_internet"),
    )

    assert angry.state.issue_acknowledged is True
    assert angry.state.frustration_acknowledged is False
    assert angry.state.acknowledgement_required is True
    assert angry.metadata["question_key"] == "device_scope"


def test_general_enquiry_selects_policy_clarification_instead_of_handoff(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I would like to make enquiries about your services",
        classification=_classification(
            intent="general_enquiry", category="general_enquiry"
        ),
    )

    assert decision.action == "respond"
    assert decision.metadata["question_key"] == "service_interest"
    assert decision.metadata["priority_source"] == "default_policy"


@pytest.mark.parametrize(
    ("intent", "category", "expected_question"),
    [
        ("billing_issue", "other_billing_issue", "billing_concern"),
        ("payment_confirmation", "payment_confirmation", "payment_reference"),
        ("subscription_renewal", "renewal_request", "renewal_service"),
        ("plan_change", "plan_change_request", "desired_plan"),
    ],
)
def test_nontechnical_intents_use_default_inquiry_policy(
    db_session, intent, category, expected_question
):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Please help me with this.",
        classification=_classification(intent=intent, category=category),
    )

    assert decision.action == "respond"
    assert decision.metadata["question_key"] == expected_question
    assert decision.metadata["priority_source"] == "default_policy"


def test_explicit_inquiry_plan_is_authoritative_and_records_priority_source():
    state = engine.ConversationalState(
        conversation_id=str(uuid4()),
        session_id=str(uuid4()),
        policy_version_id=None,
        channel="whatsapp",
        current_intent="general_enquiry",
        category="general_enquiry",
    )
    policy = {
        "inquiry_plans": [
            {
                "intent": "general_enquiry",
                "allowed_tools": [],
                "facts": [
                    {
                        "key": "enquiry_topic",
                        "purpose": "identify the information the customer needs",
                        "priority": 700,
                        "required": True,
                    }
                ],
            }
        ]
    }

    [candidate] = engine._question_candidates(state, policy)

    assert candidate.key == "enquiry_topic"
    assert candidate.priority == 700
    assert candidate.priority_source == "playbook"


def test_explicit_inquiry_policy_does_not_fall_back_for_uncovered_intent():
    state = engine.ConversationalState(
        conversation_id=str(uuid4()),
        session_id=str(uuid4()),
        policy_version_id=None,
        channel="whatsapp",
        current_intent="billing_issue",
        category="other_billing_issue",
    )
    policy = {
        "inquiry_plans": [
            {
                "intent": "general_enquiry",
                "allowed_tools": [],
                "facts": [],
            }
        ]
    }

    assert engine._question_candidates(state, policy) == ()
    assert engine._tool_allowed_for_state(policy, "customer_lookup", state) is False


def test_inquiry_policy_validation_rejects_unstructured_customer_copy():
    with pytest.raises(ValueError, match="fact is unsupported"):
        ai_conversation_intake._validate_inquiry_plans(
            {
                "inquiry_plans": [
                    {
                        "intent": "general_enquiry",
                        "allowed_tools": [],
                        "facts": [
                            {
                                "key": "ask_this_sentence",
                                "purpose": "ask a hard-coded sentence",
                                "priority": 100,
                                "required": True,
                            }
                        ],
                    }
                ]
            }
        )


def test_model_fact_correction_replaces_stale_connectivity_state(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(db_session)
    session = _session(db_session, conversation, version)
    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is down.",
        classification=_classification(),
    )
    engine.persist_state(session, first.state)

    corrected = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="Actually it works, it is just very slow.",
        classification=_classification(
            category="slow_internet",
            message_facts=AiIntakeExtractedFacts(connectivity_state="slow"),
        ),
    )

    assert corrected.state.collected_facts["connectivity_state"] == "slow"
    assert corrected.state.collected_facts["connectivity_problem"] is False
    assert corrected.state.collected_facts["slow_internet"] is True


def test_unclear_answer_is_clarified_once_then_planner_can_move_on(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": False},
            }
        },
    )
    session = _session(db_session, conversation, version)
    first = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My internet is slow.",
        classification=_classification(category="slow_internet"),
    )
    engine.persist_state(session, first.state)
    retry = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I am not sure.",
        classification=_classification(category="slow_internet"),
    )
    engine.persist_state(session, retry.state)
    next_question = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="I still do not know.",
        classification=_classification(category="slow_internet"),
    )

    assert first.metadata["question_key"] == "device_scope"
    assert retry.metadata["question_key"] == "device_scope"
    assert next_question.metadata["question_key"] == "issue_started_when"
    assert retry.state.question_history[0].attempts == 2


def test_monitoring_rules_keep_tool_radius_and_ont_statuses_separate():
    state = engine.ConversationalState(
        conversation_id=str(uuid4()),
        session_id=str(uuid4()),
        policy_version_id=None,
        channel="whatsapp",
        monitoring_results=[
            {
                "status": "available",
                "radius_observation": {"state": "offline"},
                "ont_observations": [{"effective_state": "online"}],
            }
        ],
    )

    assert engine._condition_matches(
        state,
        {"type": "monitoring_status", "value": "available"},
    )
    assert engine._condition_matches(
        state,
        {"type": "radius_status", "value": "offline"},
    )
    assert engine._condition_matches(state, {"type": "ont_status", "value": "online"})
    assert not engine._condition_matches(
        state, {"type": "ont_status", "value": "offline"}
    )
    summary = engine.render_handoff_summary(
        state,
        version=None,
        channel="whatsapp",
    )
    assert "radius_state=offline" in summary
    assert "ont_states=online" in summary
    assert "service_state" not in summary


def test_mark_resolved_is_a_terminal_engine_action(db_session):
    subscriber = _subscriber(db_session)
    conversation = _conversation(db_session, subscriber_id=subscriber.id)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": True},
                "subscriber_monitoring": {"enabled": False},
            },
            "conversation_policy": {
                "max_turns": 6,
                "require_identity_before_tools": True,
                "playbooks": [
                    {
                        "key": "confirmed_working",
                        "intent": "technical_support",
                        "steps": [
                            {
                                "condition": {
                                    "type": "field_value",
                                    "field": "connectivity_state",
                                    "value": "working",
                                },
                                "action": "mark_resolved",
                                "response": "Glad to hear the connection is working now.",
                            }
                        ],
                    }
                ],
            },
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="It is working now.",
        classification=_classification(
            message_facts=AiIntakeExtractedFacts(connectivity_state="working")
        ),
    )

    assert decision.action == "resolved"
    assert decision.state.resolution_status == "resolved"


def test_policy_partial_save_preserves_existing_playbooks(db_session):
    version = _version(
        db_session,
        metadata={
            "conversation_policy": {
                "max_turns": 6,
                "playbooks": [{"key": "keep-me", "steps": []}],
            }
        },
    )

    payload = ai_conversation_intake._copy_version_payload(
        version,
        ai_conversation_intake.AiPolicyVersionDraftCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy",
                reason="test partial editor save",
            ),
            policy_id=version.policy_id,
            conversation_policy={"max_turns": 8},
        ),
    )

    metadata = payload["metadata_"]
    assert isinstance(metadata, dict)
    conversation_policy = metadata["conversation_policy"]
    assert isinstance(conversation_policy, dict)
    assert conversation_policy["max_turns"] == 8
    assert conversation_policy["playbooks"] == [{"key": "keep-me", "steps": []}]


def test_disabled_and_unauthorized_tools_do_not_execute(db_session):
    conversation = _conversation(db_session)
    version = _version(
        db_session,
        metadata={
            "tools": {
                "customer_lookup": {"enabled": False},
                "subscriber_monitoring": {"enabled": False},
            }
        },
    )
    session = _session(db_session, conversation, version)

    decision = engine.run_conversational_turn(
        db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="My email is noone@example.test and internet is down.",
        classification=_classification(),
    )
    direct = engine.execute_tool(
        db_session,
        "subscriber_monitoring",
        {"subscriber_id": str(uuid4())},
        policy={"tools": {"subscriber_monitoring": {"enabled": False}}},
    )

    assert any(
        item["tool"] == "customer_lookup" and item["status"] == "unauthorized"
        for item in decision.state.tool_executions
    )
    assert direct["status"] == "unauthorized"


def test_simulation_preview_does_not_call_live_lookup(db_session, monkeypatch):
    conversation = _conversation(db_session)
    version = _version(db_session)
    called = False

    def _live_lookup(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("simulation preview must not call live lookup")

    monkeypatch.setattr(engine, "_customer_lookup", _live_lookup)

    result = ai_conversation_intake.preview_policy_version(
        db_session,
        ai_conversation_intake.AiPolicyPreviewCommand(
            context=CommandContext.system(
                actor="test",
                scope="ai:intake-policy-preview",
                reason="test simulation preview",
            ),
            version_id=version.id,
            customer_message="My Portal ID is 12345 and internet is down.",
            channel_type=conversation.channel_type,
            preview_mode="simulation",
        ),
    )

    assert called is False
    assert result.preview_mode == "simulation"
    assert any(
        item["tool"] == "customer_lookup" and item["result"]["simulated"] is True
        for item in result.tool_executions
    )


def test_ai_engine_does_not_implement_queue_or_round_robin():
    source = inspect.getsource(engine)

    assert "InboxConversationQueueEntry" not in source
    assert "InboxTeamRoundRobinCursor" not in source
    assert "queue_position" not in source


def test_langgraph_topology_contains_expected_nodes_and_edges():
    topology = ai_intake_graph.graph_topology()

    assert set(ai_intake_graph.GRAPH_NODE_SEQUENCE) <= set(topology)
    assert topology["load_policy"] == ("load_state",)
    assert "handle_classifier_unavailable" in topology["determine_missing_information"]
    assert topology["handle_classifier_unavailable"] == ("decide_next_action",)
    assert "request_identifier" in topology["determine_missing_information"]
    assert "execute_tool" in topology["select_tool"]
    assert "handoff" in topology["decide_next_action"]
    assert "resolved" in topology["decide_next_action"]


def test_langgraph_tool_failure_handoff_requires_explicit_policy(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(db_session, conversation, version)
    runtime = ai_intake_graph._GraphRuntime(
        db=db_session,
        conversation=conversation,
        session=session,
        version=version,
        latest_body="No internet.",
        classification=_classification(),
        classifier_attempt=AiClassifierAttempt(
            status=AiClassifierAttemptStatus.accepted,
        ),
        recent_messages=(),
        now=datetime.now(UTC),
        tool_mode="simulation",
    )
    state = engine.ConversationalState.load(
        conversation=conversation,
        session=session,
    )
    node = ai_intake_graph._interpret_tool_result(runtime)
    result = node(
        {
            "dotmac_state": state,
            "policy": engine._policy(version),
            "tool_result": {"status": "unavailable"},
            "node_trace": [],
        }
    )

    assert result["graph_route"] == "troubleshoot"
    assert result.get("graph_action") != "handoff"

    policy = engine._policy(version)
    policy["tool_failure_handoff_statuses"] = {"subscriber_monitoring": ["unavailable"]}
    required = node(
        {
            "dotmac_state": state,
            "policy": policy,
            "tool_result": {"status": "unavailable"},
            "node_trace": [],
        }
    )

    assert required["graph_action"] == "handoff"
    assert required["graph_reason"] == "monitoring_unavailable"


def test_langgraph_state_hydration_uses_dotmac_session_state(db_session):
    conversation = _conversation(db_session)
    version = _version(db_session)
    session = _session(
        db_session,
        conversation,
        version,
        metadata={
            engine.STATE_KEY: {
                "conversation_id": str(conversation.id),
                "session_id": "existing-session",
                "policy_version_id": str(version.id),
                "channel": conversation.channel_type,
                "current_intent": "technical_support",
                "collected_facts": {"router_restarted": True},
                "turn_count": 2,
            }
        },
    )

    loaded = engine.ConversationalState.load(
        conversation=conversation,
        session=session,
    )

    assert loaded.current_intent == "technical_support"
    assert loaded.collected_facts["router_restarted"] is True
    assert loaded.policy_version_id == str(version.id)


def test_langgraph_module_does_not_own_queue_or_round_robin():
    source = inspect.getsource(ai_intake_graph)

    assert "InboxConversationQueueEntry" not in source
    assert "InboxTeamRoundRobinCursor" not in source
    assert "assign_conversation_to_available_agent" not in source


def test_langgraph_runtime_fallback_is_observable():
    source = inspect.getsource(ai_conversation_intake)

    assert "ai_intake_langgraph_unavailable_falling_back" in source
    assert '"ai_intake_engine_requested"' in source
    assert '"requested_engine"' in source
