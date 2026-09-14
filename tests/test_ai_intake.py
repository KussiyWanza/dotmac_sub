from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.ai_intake import AiIntakeConfig
from app.models.service_team import ServiceTeam
from app.schemas.ai_intake import (
    CUSTOMER_TYPE_FOLLOW_UP_QUESTION,
    NATURAL_CLARIFICATION_QUESTION,
    AiClassifierAttemptStatus,
    AiClassifierFailureKind,
    AiCustomerResponseCompositionRequest,
    AiIntakeAffectAssessment,
    AiIntakeAffectLevel,
    AiIntakeCategory,
    AiIntakeContextMessage,
    AiIntakeExtractedFacts,
    AiIntakeIntent,
    AiIntakeMessageRole,
    AiIntakeMonitoringContext,
    AiIntakeNextAction,
    AiIntakePlaybookStepContext,
    AiIntakeReason,
    AiIntakeRequest,
    AiIntakeSafeCustomerIdentity,
    AiIntakeStatus,
)
from app.schemas.ai_operations import AiIntakeConfigUpsert
from app.services import ai_intake
from app.services.ai.client import AIClientError, AIResponse


class _Gateway:
    def __init__(
        self,
        content: str | None = None,
        error: Exception | None = None,
        *,
        provider: str = "test-provider",
        model: str = "test-model",
    ):
        self.content = content
        self.error = error
        self.provider = provider
        self.model = model
        self.calls: list[dict[str, object]] = []

    def generate_with_fallback(self, _db, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return (
            AIResponse(
                content=str(self.content or ""),
                tokens_in=10,
                tokens_out=20,
                model=self.model,
                provider=self.provider,
            ),
            {"endpoint": "primary", "fallback_used": False},
        )


def _request(**overrides) -> AiIntakeRequest:
    values = {
        "channel_type": "whatsapp",
        "provider": "meta_cloud_api",
        "account_scope": "phone-1",
        "inbound_message_id": "wamid-1",
        "body": "My internet is very slow today",
    }
    values.update(overrides)
    return AiIntakeRequest(**values)


def _config(db_session, **overrides) -> AiIntakeConfig:
    values = {
        "scope_key": "default",
        "channel_type": "any",
        "is_enabled": True,
        "confidence_threshold": 0.75,
        "allow_followup_questions": True,
        "max_clarification_turns": 1,
        "escalate_after_minutes": 5,
        "customer_response_timeout_minutes": 5,
        "exclude_campaign_attribution": True,
        "department_mappings": [],
        "metadata_": {},
    }
    values.update(overrides)
    if values["is_enabled"] and "fallback_team_id" not in overrides:
        fallback = ServiceTeam(
            name=f"AI Intake Fallback {uuid4()}",
            team_type="support",
            is_active=True,
        )
        db_session.add(fallback)
        db_session.flush()
        values["fallback_team_id"] = fallback.id
    row = AiIntakeConfig(**values)
    db_session.add(row)
    db_session.flush()
    return row


def _classification(
    *,
    intent: str = "technical_support",
    category: str = "slow_internet",
    confidence: float = 0.94,
    party_type: str = "unknown",
    party_type_confidence: float = 0.0,
    message_affect: dict[str, object] | None = None,
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "category": category,
            "confidence": confidence,
            "department": None,
            "requires_follow_up": False,
            "follow_up_question": None,
            "summary": "Customer reports a service issue.",
            "party_type": party_type,
            "party_type_confidence": party_type_confidence,
            **({"message_affect": message_affect} if message_affect else {}),
        }
    )


def _deepseek_null_default_classification() -> str:
    return json.dumps(
        {
            "intent": "technical_support",
            "category": "no_internet",
            "confidence": 0.96,
            "department": None,
            "requires_follow_up": False,
            "follow_up_question": None,
            "summary": "Service is unavailable.",
            "party_type": None,
            "party_type_confidence": None,
            "message_facts": dict.fromkeys(AiIntakeExtractedFacts.model_fields),
            "message_affect": {
                "frustration_level": None,
                "agitation_level": None,
                "repeated_complaint": None,
                "repeated_failed_steps": None,
                "prior_failed_interaction": None,
            },
        }
    )


def test_no_matching_configuration_skips_gateway(db_session, monkeypatch):
    gateway = _Gateway(_classification())
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.status is AiIntakeStatus.skipped
    assert outcome.reason is AiIntakeReason.no_matching_config
    assert gateway.calls == []


def test_disabled_and_unsupported_channel_do_not_call_gateway(db_session, monkeypatch):
    _config(db_session, is_enabled=False)
    gateway = _Gateway(_classification())
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    disabled = ai_intake.classify_message(db_session, _request())
    unsupported = ai_intake.classify_message(
        db_session,
        _request(channel_type="email"),
    )

    assert disabled.reason is AiIntakeReason.disabled
    assert unsupported.reason is AiIntakeReason.unsupported_channel
    assert gateway.calls == []


def test_exact_account_and_channel_config_wins_over_default(db_session):
    _config(db_session, scope_key="default", confidence_threshold=0.5)
    exact = _config(
        db_session,
        scope_key="meta_cloud_api:phone-1",
        channel_type="whatsapp",
        confidence_threshold=0.9,
    )

    resolved = ai_intake.resolve_config(db_session, _request())

    assert resolved is not None
    assert resolved.id == exact.id
    assert resolved.confidence_threshold == 0.9


def test_meta_social_inbound_provider_matches_canonical_connector_scope(db_session):
    exact = _config(
        db_session,
        scope_key="meta.social:ig-1",
        channel_type="instagram_dm",
        confidence_threshold=0.91,
    )

    resolved = ai_intake.resolve_config(
        db_session,
        _request(
            channel_type="instagram_dm",
            provider="meta_social",
            account_scope="ig-1",
        ),
    )

    assert resolved is not None
    assert resolved.id == exact.id
    assert resolved.confidence_threshold == 0.91


def test_canonical_meta_social_provider_still_matches_legacy_scope(db_session):
    exact = _config(
        db_session,
        scope_key="meta_social:ig-1",
        channel_type="instagram_dm",
        confidence_threshold=0.82,
    )

    resolved = ai_intake.resolve_config(
        db_session,
        _request(
            channel_type="instagram_dm",
            provider="meta.social",
            account_scope="ig-1",
        ),
    )

    assert resolved is not None
    assert resolved.id == exact.id
    assert resolved.confidence_threshold == 0.82


def test_campaign_attribution_is_excluded_when_configured(db_session, monkeypatch):
    _config(db_session)
    gateway = _Gateway(_classification())
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request(campaign_attributed=True))

    assert outcome.reason is AiIntakeReason.campaign_excluded
    assert gateway.calls == []


def test_valid_technical_and_billing_results_use_controlled_registry(
    db_session, monkeypatch
):
    _config(db_session)
    gateway = _Gateway(_classification())
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    technical = ai_intake.classify_message(db_session, _request())
    gateway.content = _classification(
        intent="billing_issue",
        category="payment_not_reflected",
        confidence=0.91,
    )
    billing = ai_intake.classify_message(
        db_session,
        _request(inbound_message_id="wamid-2", body="My payment is missing"),
    )

    assert technical.status is AiIntakeStatus.classified
    assert technical.classification is not None
    assert technical.classification.intent.value == "technical_support"
    assert technical.classification.department == "technical_support"
    assert billing.classification is not None
    assert billing.classification.intent.value == "billing_issue"
    assert billing.classification.department == "billing_issue"


def test_model_affect_candidate_is_bounded_and_backend_owns_provenance(
    db_session, monkeypatch
):
    _config(db_session)
    gateway = _Gateway(
        _classification(
            message_affect={
                "frustration_level": "high",
                "agitation_level": "moderate",
                "repeated_complaint": True,
                "repeated_failed_steps": False,
                "prior_failed_interaction": True,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(
        db_session,
        _request(body="I am fed up; I contacted support before."),
    )

    assert outcome.classification is not None
    affect = outcome.classification.message_affect
    assert affect.frustration_level is AiIntakeAffectLevel.high
    assert affect.agitation_level is AiIntakeAffectLevel.moderate
    assert affect.repeated_complaint is True
    assert [source.value for source in affect.sources] == ["model"]


def test_department_mapping_overrides_default(db_session, monkeypatch):
    _config(
        db_session,
        department_mappings=[{"intent": "billing_issue", "department": "finance"}],
    )
    gateway = _Gateway(
        _classification(
            intent="billing_issue",
            category="invoice_request",
            confidence=0.9,
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.classification is not None
    assert outcome.classification.department == "finance"


def test_sales_customer_type_uses_one_controlled_follow_up_then_fallback(
    db_session, monkeypatch
):
    _config(db_session, confidence_threshold=0.8, max_clarification_turns=1)
    gateway = _Gateway(
        _classification(
            intent="new_connection",
            category="new_connection",
            confidence=0.95,
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = ai_intake.classify_message(db_session, _request())
    second = ai_intake.classify_message(
        db_session,
        _request(awaiting_follow_up=True, follow_up_count=1),
    )

    assert first.status is AiIntakeStatus.awaiting_follow_up
    assert first.classification is not None
    assert first.classification.follow_up_question == CUSTOMER_TYPE_FOLLOW_UP_QUESTION
    assert first.follow_up_count == 1
    assert second.status is AiIntakeStatus.fallback
    assert second.reason is AiIntakeReason.follow_up_limit_reached


def test_sales_customer_type_is_route_ready_metadata(db_session, monkeypatch):
    _config(db_session, confidence_threshold=0.8)
    gateway = _Gateway(
        _classification(
            intent="coverage_request",
            category="coverage_request",
            confidence=0.96,
            party_type="organization",
            party_type_confidence=0.91,
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())
    metadata = ai_intake.route_metadata(outcome)

    assert outcome.status is AiIntakeStatus.classified
    assert metadata["ai_party_type"] == "organization"
    assert metadata["ai_party_type_confidence"] == 0.91


def test_unknown_intent_malformed_json_and_invalid_confidence_fail_closed(
    db_session, monkeypatch
):
    _config(db_session)
    gateway = _Gateway(_classification(intent="invented_intent"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    unknown = ai_intake.classify_message(db_session, _request())
    gateway.content = "not-json"
    malformed = ai_intake.classify_message(db_session, _request())
    gateway.content = _classification(confidence=1.2)
    invalid_confidence = ai_intake.classify_message(db_session, _request())

    assert unknown.reason is AiIntakeReason.classifier_invalid_output
    assert malformed.reason is AiIntakeReason.classifier_invalid_output
    assert invalid_confidence.reason is AiIntakeReason.classifier_invalid_output
    assert all(
        outcome.status is AiIntakeStatus.classification_unavailable
        for outcome in (unknown, malformed, invalid_confidence)
    )
    assert all(
        outcome.classifier_attempt.status is AiClassifierAttemptStatus.invalid_output
        for outcome in (unknown, malformed, invalid_confidence)
    )
    assert unknown.classifier_attempt.failure_kind is (
        AiClassifierFailureKind.schema_validation_failure
    )
    assert malformed.classifier_attempt.failure_kind is (
        AiClassifierFailureKind.invalid_model_output
    )
    assert invalid_confidence.classifier_attempt.failure_kind is (
        AiClassifierFailureKind.schema_validation_failure
    )
    assert malformed.provider == "test-provider"
    assert malformed.model == "test-model"
    assert malformed.classifier_attempt.retry_count == 1
    assert malformed.classifier_attempt.retry_limit == 1
    assert malformed.classifier_attempt.retries_exhausted is False


def test_deepseek_null_defaults_are_normalized_without_relaxing_schema(
    db_session, monkeypatch
):
    _config(db_session)
    gateway = _Gateway(
        _deepseek_null_default_classification(),
        provider="primary",
        model="deepseek-flash",
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.status is AiIntakeStatus.classified
    assert outcome.classification is not None
    assert outcome.classification.message_facts.connectivity_state.value == "unknown"
    assert outcome.classification.message_facts.human_requested is False
    assert outcome.classification.message_affect.frustration_level.value == "none"
    assert outcome.classifier_attempt.validation_issues == ()

    gateway.provider = "another-provider"
    gateway.model = "another-model"
    rejected = ai_intake.classify_message(db_session, _request())
    assert rejected.status is AiIntakeStatus.classification_unavailable
    assert rejected.classifier_attempt.failure_kind is (
        AiClassifierFailureKind.schema_validation_failure
    )


def test_classifier_validation_logging_is_structural_and_sanitized(
    db_session, monkeypatch, caplog
):
    _config(db_session)
    secret = "CUSTOMER-AND-MODEL-TEXT-MUST-NOT-APPEAR"
    payload = json.loads(_deepseek_null_default_classification())
    payload["confidence"] = secret
    gateway = _Gateway(json.dumps(payload), provider="primary", model="deepseek-flash")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    session_id = uuid4()
    inbound_id = uuid4()
    policy_version_id = uuid4()

    with caplog.at_level("WARNING", logger="app.services.ai_intake"):
        outcome = ai_intake.classify_message(
            db_session,
            _request(
                session_id=session_id,
                persisted_inbound_message_id=inbound_id,
                policy_version_id=policy_version_id,
            ),
        )

    assert outcome.status is AiIntakeStatus.classification_unavailable
    [issue] = outcome.classifier_attempt.validation_issues
    assert issue.location == "confidence"
    assert issue.error_type == "float_type"
    assert issue.expected_type == "strict_number_0_to_1"
    assert issue.actual_type == "str"
    invalid_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ai_intake_invalid_model_output"
    )
    assert invalid_record.session_id == str(session_id)
    assert invalid_record.inbound_message_id == str(inbound_id)
    assert invalid_record.policy_version_id == str(policy_version_id)
    assert invalid_record.classifier_attempt_number == 1
    assert secret not in json.dumps(invalid_record.__dict__, default=str)


def test_classifier_unknown_intent_is_no_accepted_intent(db_session, monkeypatch):
    _config(db_session)
    gateway = _Gateway(_classification(intent="unknown", category="unknown"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.status is AiIntakeStatus.classification_unavailable
    assert outcome.reason is AiIntakeReason.classifier_unavailable
    assert outcome.classifier_attempt.status is (
        AiClassifierAttemptStatus.no_accepted_intent
    )
    assert outcome.classifier_attempt.failure_kind is (
        AiClassifierFailureKind.no_accepted_intent
    )


def test_customer_content_is_redacted_and_context_is_bounded(db_session, monkeypatch):
    _config(db_session)
    gateway = _Gateway(_classification())
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    sensitive = (
        "Email me at customer@example.com or +234 803 123 4567; "
        "password: NeverStoreThisValue " + ("x" * 2500)
    )

    recent = tuple(
        AiIntakeContextMessage(
            role=AiIntakeMessageRole.customer, body=f"message {index}"
        )
        for index in range(5)
    )
    ai_intake.classify_message(
        db_session, _request(body=sensitive, recent_messages=recent)
    )

    [call] = gateway.calls
    prompt = str(call["prompt"])
    assert "customer@example.com" not in prompt
    assert "+234 803 123 4567" not in prompt
    assert "NeverStoreThisValue" not in prompt
    assert "[redacted-email]" in prompt
    assert "[redacted-phone]" in prompt
    assert len(json.loads(prompt)["latest_inbound_message"]) <= 1200
    assert len(json.loads(prompt)["recent_messages"]) == 5


def test_email_cannot_be_configured_for_ai_intake():
    with pytest.raises(ValidationError):
        AiIntakeConfigUpsert(scope_key="email", channel_type="email")


def test_chat_widget_can_be_configured_for_ai_intake():
    policy = AiIntakeConfigUpsert(
        scope_key="fiber_website:fiber.dotmac.ng",
        channel_type="chat_widget",
    )

    assert policy.channel_type == "chat_widget"


def _composition_request() -> AiCustomerResponseCompositionRequest:
    return AiCustomerResponseCompositionRequest(
        intent=AiIntakeIntent.technical_support,
        category=AiIntakeCategory.slow_internet,
        latest_customer_statement=(
            "My internet has been slow on every device since yesterday."
        ),
        facts=AiIntakeExtractedFacts(
            connectivity_state="slow",
            device_scope="all_devices",
            issue_started_when="since yesterday",
        ),
        missing_fact_keys=("connection_medium",),
        asked_question_keys=(),
        customer_identity=AiIntakeSafeCustomerIdentity(identified=True),
        playbook_step=AiIntakePlaybookStepContext(
            key="connection_medium",
            action=AiIntakeNextAction.ask_question,
            approved_instruction=(
                "Ask whether the slowdown is the same on Wi-Fi and Ethernet."
            ),
            question_purpose=(
                "determine whether the slowdown differs by connection medium"
            ),
            expected_fact="connection_medium",
        ),
        business_tone="Warm, concise and practical.",
        issue_acknowledged=False,
    )


def test_response_composition_uses_existing_gateway_and_returns_empathy(
    db_session, monkeypatch
):
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": (
                    "I'm sorry the connection has been slow since yesterday. "
                    "Is it the same over Wi-Fi and Ethernet?"
                ),
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": "connection_medium",
                "acknowledges_issue": True,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=_composition_request(),
        fallback_text="Is it the same over Wi-Fi and Ethernet?",
        fallback_source="template",
    )

    assert outcome.response_source == "model"
    assert outcome.acknowledges_issue is True
    assert "since yesterday" in outcome.response_text
    assert outcome.follow_up_fact_key == "connection_medium"
    assert len(gateway.calls) == 1


def test_response_validator_rejects_invented_monitoring_and_falls_back(
    db_session, monkeypatch
):
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": (
                    "Your line appears currently offline from our side. "
                    "Is it the same over Wi-Fi and Ethernet?"
                ),
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": "connection_medium",
                "acknowledges_issue": True,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=_composition_request(),
        fallback_text="Is it the same over Wi-Fi and Ethernet?",
        fallback_source="template",
    )

    assert outcome.response_source == "template"
    assert outcome.safety_reason == "invented_monitoring"
    assert "currently offline" not in outcome.response_text
    assert outcome.response_text.startswith("I'm sorry the connection has been slow")
    assert "since yesterday" in outcome.response_text


def test_response_composer_failure_uses_safe_configured_question(
    db_session, monkeypatch
):
    gateway = _Gateway(error=AIClientError("composer unavailable"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=_composition_request().model_copy(
            update={"issue_acknowledgement_required": True}
        ),
        fallback_text="Is it the same over Wi-Fi and Ethernet?",
        fallback_source="template",
    )

    assert outcome.response_source == "template"
    assert outcome.safety_reason == "composition_unavailable"
    assert outcome.follow_up_fact_key == "connection_medium"
    assert outcome.response_text.endswith("Is it the same over Wi-Fi and Ethernet?")
    assert outcome.acknowledges_issue is True


def test_validator_rejects_missing_required_issue_acknowledgement(
    db_session, monkeypatch
):
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": "Is it the same over Wi-Fi and Ethernet?",
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": "connection_medium",
                "acknowledges_issue": False,
                "acknowledges_frustration": False,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=_composition_request().model_copy(
            update={"issue_acknowledgement_required": True}
        ),
        fallback_text="Is it the same over Wi-Fi and Ethernet?",
        fallback_source="template",
    )

    assert outcome.response_source == "template"
    assert outcome.safety_reason == "missing_required_issue_acknowledgement"
    assert outcome.acknowledges_issue is True


def test_response_validator_does_not_treat_monitoring_no_data_as_offline(
    db_session, monkeypatch
):
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": (
                    "Our monitoring shows your line is currently offline. "
                    "Is it the same over Wi-Fi and Ethernet?"
                ),
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": "connection_medium",
                "acknowledges_issue": False,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    request = _composition_request().model_copy(
        update={"monitoring": AiIntakeMonitoringContext(status="no_data")}
    )

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=request,
        fallback_text="Is it the same over Wi-Fi and Ethernet?",
        fallback_source="template",
    )

    assert outcome.response_source == "template"
    assert outcome.safety_reason == "unverified_monitoring"
    assert "offline" not in outcome.response_text.lower()


@pytest.mark.parametrize(
    ("response_text", "request_updates", "follow_up_fact_key", "expected_reason"),
    [
        (
            "Is it slow on Wi-Fi? Is it also slow over Ethernet?",
            {},
            "connection_medium",
            "question_count_mismatch",
        ),
        (
            "Is it affecting every device?",
            {
                "missing_fact_keys": ("connection_medium",),
                "asked_question_keys": ("device_scope",),
            },
            "device_scope",
            "repeated_or_unapproved_question",
        ),
        (
            "I'm sorry again. Is it the same over Wi-Fi and Ethernet?",
            {"issue_acknowledged": True},
            "connection_medium",
            "repeated_apology",
        ),
        (
            "I'm sorry, and I apologize. Is it the same over Wi-Fi and Ethernet?",
            {},
            "connection_medium",
            "excessive_apology",
        ),
        (
            "There is an outage. Is it the same over Wi-Fi and Ethernet?",
            {},
            "connection_medium",
            "invented_outage",
        ),
        (
            "We received your payment. Is it the same over Wi-Fi and Ethernet?",
            {},
            "connection_medium",
            "unsupported_payment_confirmation",
        ),
        (
            "It will be fixed shortly. Is it the same over Wi-Fi and Ethernet?",
            {},
            "connection_medium",
            "unsupported_promise",
        ),
        (
            "This is a router fault. Is it the same over Wi-Fi and Ethernet?",
            {},
            "connection_medium",
            "unsupported_diagnosis",
        ),
        (
            "Your email is customer@example.com. Is it the same over Wi-Fi and Ethernet?",
            {},
            "connection_medium",
            "unauthorized_customer_information",
        ),
        (
            "The LangGraph classifier needs another detail. Is it the same over Wi-Fi and Ethernet?",
            {},
            "connection_medium",
            "internal_terminology",
        ),
    ],
)
def test_response_validator_rejects_unsafe_model_compositions(
    db_session,
    monkeypatch,
    response_text,
    request_updates,
    follow_up_fact_key,
    expected_reason,
):
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": response_text,
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": follow_up_fact_key,
                "acknowledges_issue": False,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    request = _composition_request().model_copy(update=request_updates)

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=request,
        fallback_text="Is it the same over Wi-Fi and Ethernet?",
        fallback_source="template",
    )

    assert outcome.response_source == "template"
    assert outcome.safety_reason == expected_reason
    assert outcome.response_text != response_text


def _frustrated_device_scope_request() -> AiCustomerResponseCompositionRequest:
    return _composition_request().model_copy(
        update={
            "latest_customer_statement": "I'm fucking fed up of you guys",
            "facts": AiIntakeExtractedFacts(),
            "missing_fact_keys": ("device_scope",),
            "playbook_step": AiIntakePlaybookStepContext(
                key="device_scope",
                action=AiIntakeNextAction.ask_question,
                approved_instruction=(
                    "determine whether the problem is limited to one device or "
                    "is connection-wide, without assuming device ownership"
                ),
                question_purpose=(
                    "determine whether the problem is limited to one device or "
                    "is connection-wide, without assuming device ownership"
                ),
                expected_fact="device_scope",
            ),
            "affect": AiIntakeAffectAssessment(
                frustration_level=AiIntakeAffectLevel.high,
                agitation_level=AiIntakeAffectLevel.high,
            ),
            "acknowledgement_required": True,
            "issue_acknowledged": True,
            "frustration_acknowledged": False,
        }
    )


def test_validator_rejects_bare_question_when_frustration_requires_acknowledgement(
    db_session, monkeypatch
):
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": "If possible, does this also happen on another device?",
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": "device_scope",
                "acknowledges_issue": False,
                "acknowledges_frustration": False,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=_frustrated_device_scope_request(),
        fallback_text="If possible, does this also happen on another device?",
        fallback_source="template",
    )

    assert outcome.response_source == "template"
    assert outcome.safety_reason == "missing_required_acknowledgement"
    assert outcome.acknowledges_frustration is True
    assert outcome.response_text.index("difficult") < outcome.response_text.index("?")
    composition_projection = json.loads(str(gateway.calls[0]["prompt"]))
    assert composition_projection["playbook_step"]["expected_fact"] == "device_scope"
    assert (
        "without assuming device ownership"
        in composition_projection["playbook_step"]["question_purpose"]
    )
    assert "Is the issue affecting every device or only one device?" not in str(
        gateway.calls[0]["prompt"]
    )


def test_validator_accepts_natural_acknowledgement_and_neutral_question(
    db_session, monkeypatch
):
    response = (
        "I hear you—this has been a difficult experience. If you can check "
        "another device, does the same problem happen there?"
    )
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": response,
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": "device_scope",
                "acknowledges_issue": False,
                "acknowledges_frustration": True,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=_frustrated_device_scope_request(),
        fallback_text="If possible, does this also happen on another device?",
        fallback_source="template",
    )

    assert outcome.response_source == "model"
    assert outcome.response_text == response
    assert outcome.acknowledges_frustration is True


def test_validator_rejects_device_scope_wording_that_assumes_multiple_devices(
    db_session, monkeypatch
):
    gateway = _Gateway(
        json.dumps(
            {
                "response_text": "Is this affecting every device or only one device?",
                "purpose": "acknowledgement_question",
                "follow_up_fact_key": "device_scope",
                "acknowledges_issue": False,
                "acknowledges_frustration": False,
            }
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)
    request = _frustrated_device_scope_request().model_copy(
        update={"acknowledgement_required": False}
    )

    outcome = ai_intake.compose_customer_response(
        db_session,
        request=request,
        fallback_text="If possible, does this also happen on another device?",
        fallback_source="template",
    )

    assert outcome.response_source == "template"
    assert outcome.safety_reason == "unsupported_device_ownership_assumption"
    assert "every device" not in outcome.response_text.lower()


def test_low_confidence_allows_one_controlled_follow_up_then_fallback(
    db_session, monkeypatch
):
    _config(db_session, confidence_threshold=0.8)
    gateway = _Gateway(_classification(confidence=0.4))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = ai_intake.classify_message(db_session, _request())
    second = ai_intake.classify_message(
        db_session,
        _request(
            created_conversation=False,
            awaiting_follow_up=True,
            follow_up_count=1,
            inbound_message_id="wamid-2",
        ),
    )

    assert first.status is AiIntakeStatus.awaiting_follow_up
    assert first.follow_up_count == 1
    assert first.classification is not None
    assert first.classification.follow_up_question == NATURAL_CLARIFICATION_QUESTION
    assert second.status is AiIntakeStatus.fallback
    assert second.reason is AiIntakeReason.follow_up_limit_reached


def test_configured_clarification_questions_are_used(db_session, monkeypatch):
    _config(
        db_session,
        confidence_threshold=0.8,
        metadata_={
            "clarification_questions": [
                "Which service do you need help with?",
                "Is the connection for you or your organization?",
            ]
        },
    )
    gateway = _Gateway(
        _classification(
            intent="new_connection",
            category="new_connection",
            confidence=0.95,
        )
    )
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.classification is not None
    assert outcome.classification.follow_up_question == (
        "Is the connection for you or your organization?"
    )


def test_category_menu_requires_explicit_configuration(db_session, monkeypatch):
    _config(
        db_session,
        confidence_threshold=0.8,
        metadata_={
            "clarification_questions": [
                ai_intake.GENERIC_FOLLOW_UP_QUESTION,
                CUSTOMER_TYPE_FOLLOW_UP_QUESTION,
            ],
            "allow_category_menu_clarification": True,
        },
    )
    gateway = _Gateway(_classification(confidence=0.4))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.classification is not None
    assert (
        outcome.classification.follow_up_question
        == ai_intake.GENERIC_FOLLOW_UP_QUESTION
    )


def test_clear_reply_after_follow_up_can_classify(db_session, monkeypatch):
    _config(db_session, confidence_threshold=0.8)
    gateway = _Gateway(_classification(confidence=0.95))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(
        db_session,
        _request(
            created_conversation=False,
            awaiting_follow_up=True,
            follow_up_count=1,
            inbound_message_id="wamid-2",
        ),
    )

    assert outcome.status is AiIntakeStatus.classified


def test_active_ai_session_keeps_existing_conversation_eligible(db_session):
    _config(db_session)

    outcome = ai_intake.prepare_async_intake(
        db_session,
        _request(
            created_conversation=False,
            active_ai_session=True,
            inbound_message_id="wamid-active-ai-session",
        ),
    )

    assert outcome.status is AiIntakeStatus.classifying
    assert outcome.reason is AiIntakeReason.classified


def test_gateway_failure_returns_classifier_unavailable_metadata(
    db_session, monkeypatch
):
    _config(db_session)
    gateway = _Gateway(error=AIClientError("provider unavailable"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.status is AiIntakeStatus.classification_unavailable
    assert outcome.reason is AiIntakeReason.classifier_unavailable
    assert outcome.classifier_attempt.status is AiClassifierAttemptStatus.unavailable
    assert outcome.classifier_attempt.failure_kind is (
        AiClassifierFailureKind.classifier_unavailable
    )
    assert ai_intake.route_metadata(outcome)["ai_intake_status"] == (
        "classification_unavailable"
    )


def test_classifier_failure_uses_existing_clarification_limit(db_session, monkeypatch):
    _config(db_session, max_clarification_turns=1)
    gateway = _Gateway("not-json")
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    first = ai_intake.classify_message(db_session, _request())
    exhausted = ai_intake.classify_message(
        db_session,
        _request(follow_up_count=1, classifier_failure_count=1),
    )

    assert first.reason is AiIntakeReason.classifier_invalid_output
    assert first.follow_up_count == 1
    assert first.classifier_attempt.retries_exhausted is False
    assert exhausted.reason is AiIntakeReason.classifier_unavailable_after_retries
    assert (
        exhausted.classifier_attempt.reason is AiIntakeReason.classifier_invalid_output
    )
    assert exhausted.follow_up_count == 1
    assert exhausted.classifier_attempt.retry_count == 2
    assert exhausted.classifier_attempt.retries_exhausted is True
