from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models.ai_intake import AiIntakeConfig
from app.schemas.ai_intake import (
    CUSTOMER_TYPE_FOLLOW_UP_QUESTION,
    AiIntakeContextMessage,
    AiIntakeReason,
    AiIntakeRequest,
    AiIntakeStatus,
)
from app.schemas.ai_operations import AiIntakeConfigUpsert
from app.services import ai_intake
from app.services.ai.client import AIClientError, AIResponse


class _Gateway:
    def __init__(self, content: str | None = None, error: Exception | None = None):
        self.content = content
        self.error = error
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
                model="test-model",
                provider="test-provider",
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
        "exclude_campaign_attribution": True,
        "department_mappings": [],
        "metadata_": {},
    }
    values.update(overrides)
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
    assert billing.classification.department == "helpdesk"


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

    assert unknown.reason is AiIntakeReason.invalid_model_output
    assert malformed.reason is AiIntakeReason.invalid_model_output
    assert invalid_confidence.reason is AiIntakeReason.invalid_model_output
    assert all(
        outcome.status is AiIntakeStatus.failed
        for outcome in (unknown, malformed, invalid_confidence)
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
        AiIntakeContextMessage(direction="inbound", body=f"message {index}")
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
    assert len(json.loads(prompt)["recent_messages"]) == 3


def test_email_cannot_be_configured_for_ai_intake():
    with pytest.raises(ValidationError):
        AiIntakeConfigUpsert(scope_key="email", channel_type="email")


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
    assert first.classification.follow_up_question == (
        ai_intake.GENERIC_FOLLOW_UP_QUESTION
    )
    assert second.status is AiIntakeStatus.fallback
    assert second.reason is AiIntakeReason.follow_up_limit_reached


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


def test_gateway_failure_returns_fallback_metadata(db_session, monkeypatch):
    _config(db_session)
    gateway = _Gateway(error=AIClientError("provider unavailable"))
    monkeypatch.setattr(ai_intake, "_gateway", lambda: gateway)

    outcome = ai_intake.classify_message(db_session, _request())

    assert outcome.status is AiIntakeStatus.failed
    assert outcome.reason is AiIntakeReason.gateway_unavailable
    assert ai_intake.route_metadata(outcome)["ai_intake_status"] == "failed"
