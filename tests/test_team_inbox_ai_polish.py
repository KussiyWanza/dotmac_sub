from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.ai_insight import AIInsight
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_api, team_inbox_ai_polish


def _conversation(db_session, *, channel_type: str = "whatsapp") -> InboxConversation:
    row = InboxConversation(
        channel_type=channel_type,
        subject="Router offline",
        contact_address="customer@example.com",
        status=InboxConversationStatus.open.value,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _auth(*, read: bool = True) -> dict[str, object]:
    scopes = ["support:ticket:update"]
    if read:
        scopes.append("support:ticket:read")
    return {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "scopes": scopes,
        "roles": [],
    }


def _projection() -> dict[str, object]:
    return {
        "company_name": "Dotmac",
        "channel": "whatsapp",
        "status": "open",
        "priority": 50,
        "subject": "Router offline",
        "contact_display_name": "Customer",
        "assigned_agent_name": "Ada",
        "tags": ["fault"],
        "linked_ticket": {"number": "TCK-42", "status": "open"},
        "messages": [
            {
                "direction": "customer",
                "body": "I have reported this twice and it is still down.",
                "occurred_at": "2026-08-13T10:00:00+00:00",
            },
            {
                "direction": "agent",
                "body": "We are checking the line now.",
                "occurred_at": "2026-08-13T10:02:00+00:00",
            },
        ],
    }


def _patch_success(monkeypatch, captured: dict[str, object], *, suggestion: str):
    def advise(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=uuid4(),
            structured_output={
                "suggestion": suggestion,
                "detected_mood": "frustrated",
                "recommended_tone": "calm and reassuring",
                "reason": "The customer repeated the fault report.",
                "facts_preserved": True,
                "warnings": [],
            },
            llm_provider="vllm",
            llm_model="support-model",
            llm_endpoint="primary",
        )

    monkeypatch.setattr(team_inbox_ai_polish.intelligence_engine, "advise", advise)


def test_polish_uses_bounded_context_and_existing_ai_engine(db_session, monkeypatch):
    conversation = _conversation(db_session)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        team_inbox_ai_polish.team_inbox_projection,
        "build_ai_reply_projection",
        lambda _db, *, conversation_id: _projection(),
    )
    _patch_success(
        monkeypatch,
        captured,
        suggestion=(
            "Please bear with us while we check the line. "
            "Your ticket TCK-42 is still open."
        ),
    )

    result = team_inbox_ai_polish.polish_reply(
        db_session,
        team_inbox_ai_polish.TeamInboxAIPolishCommand(
            auth=_auth(),
            actor_person_id=None,
            conversation_id=conversation.id,
            draft="Pls bear with us while we check ticket TCK-42.",
        ),
    )

    report = captured["report"]
    assert captured["advisor_key"] == "inbox_sentence_polish"
    assert (
        report["CURRENT_UNSENT_DRAFT"]
        == "Pls bear with us while we check ticket TCK-42."
    )
    assert report["UNTRUSTED_CONVERSATION_EXCERPTS"][0]["label"] == "CUSTOMER_MESSAGE"
    assert report["UNTRUSTED_CONVERSATION_EXCERPTS"][1]["label"] == "AGENT_MESSAGE"
    assert report["SAFETY_CONTEXT"]["private_notes_excluded"] is True
    assert result.detected_mood is team_inbox_ai_polish.PolishMood.frustrated
    assert result.suggestion_ready is True
    assert db_session.query(AIInsight).count() == 0


def test_polish_uses_configured_business_voice_and_channel_guidance(
    db_session, monkeypatch
):
    conversation = _conversation(db_session)
    captured: dict[str, object] = {}
    settings_api.upsert_integration_setting(
        db_session,
        "inbox_ai_polish_business_voice",
        DomainSettingUpdate(value_text="Use the reviewed support voice."),
    )
    settings_api.upsert_integration_setting(
        db_session,
        "inbox_ai_polish_channel_guidance",
        DomainSettingUpdate(value_text="Keep WhatsApp direct and concise."),
    )
    monkeypatch.setattr(
        team_inbox_ai_polish.team_inbox_projection,
        "build_ai_reply_projection",
        lambda _db, *, conversation_id: _projection(),
    )
    _patch_success(
        monkeypatch,
        captured,
        suggestion="We are checking ticket TCK-42 and will update you shortly.",
    )

    team_inbox_ai_polish.polish_reply(
        db_session,
        team_inbox_ai_polish.TeamInboxAIPolishCommand(
            auth=_auth(),
            actor_person_id=None,
            conversation_id=conversation.id,
            draft="Checking ticket TCK-42.",
        ),
    )

    report = captured["report"]
    assert report["CONFIGURABLE_BUSINESS_VOICE"] == "Use the reviewed support voice."
    assert (
        report["CONFIGURABLE_CHANNEL_GUIDANCE"] == "Keep WhatsApp direct and concise."
    )


def test_polish_denies_conversation_without_object_access(db_session, monkeypatch):
    conversation = _conversation(db_session)
    monkeypatch.setattr(team_inbox_ai_polish, "has_permission", lambda *_args: True)

    with pytest.raises(team_inbox_ai_polish.TeamInboxAIPolishError) as exc:
        team_inbox_ai_polish.polish_reply(
            db_session,
            team_inbox_ai_polish.TeamInboxAIPolishCommand(
                auth=_auth(read=False),
                actor_person_id=None,
                conversation_id=conversation.id,
                draft="Please check.",
            ),
        )

    assert exc.value.code is team_inbox_ai_polish.PolishErrorCode.access_denied


def test_polish_rejects_unsupported_channel_before_ai(db_session, monkeypatch):
    conversation = _conversation(db_session, channel_type="chat_widget")
    called = False

    def advise(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(team_inbox_ai_polish.intelligence_engine, "advise", advise)

    with pytest.raises(team_inbox_ai_polish.TeamInboxAIPolishError) as exc:
        team_inbox_ai_polish.polish_reply(
            db_session,
            team_inbox_ai_polish.TeamInboxAIPolishCommand(
                auth=_auth(),
                actor_person_id=None,
                conversation_id=conversation.id,
                draft="Please check.",
            ),
        )

    assert exc.value.code is team_inbox_ai_polish.PolishErrorCode.unsupported_channel
    assert called is False


def test_polish_keeps_original_when_protected_facts_change(db_session, monkeypatch):
    conversation = _conversation(db_session)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        team_inbox_ai_polish.team_inbox_projection,
        "build_ai_reply_projection",
        lambda _db, *, conversation_id: _projection(),
    )
    _patch_success(
        monkeypatch,
        captured,
        suggestion="Please bear with us while we check your ticket.",
    )

    result = team_inbox_ai_polish.polish_reply(
        db_session,
        team_inbox_ai_polish.TeamInboxAIPolishCommand(
            auth=_auth(),
            actor_person_id=None,
            conversation_id=conversation.id,
            draft="Please bear with us while we check ticket TCK-42.",
        ),
    )

    assert result.suggestion == "Please bear with us while we check ticket TCK-42."
    assert result.facts_preserved is False
    assert result.suggestion_ready is False
    assert any(
        warning.code is team_inbox_ai_polish.PolishWarningCode.protected_fact_changed
        for warning in result.warnings
    )


def test_public_comment_suggestion_blocks_private_tokens(db_session, monkeypatch):
    conversation = _conversation(db_session, channel_type="facebook_comment")
    monkeypatch.setattr(
        team_inbox_ai_polish.team_inbox_projection,
        "build_ai_reply_projection",
        lambda _db, *, conversation_id: {
            **_projection(),
            "channel": "facebook_comment",
        },
    )
    _patch_success(
        monkeypatch,
        {},
        suggestion="Please DM us with account ACC-12345 so we can check.",
    )

    result = team_inbox_ai_polish.polish_reply(
        db_session,
        team_inbox_ai_polish.TeamInboxAIPolishCommand(
            auth=_auth(),
            actor_person_id=None,
            conversation_id=conversation.id,
            draft="Please DM us so we can check.",
        ),
    )

    assert result.suggestion_ready is False
    assert any(
        warning.code
        is team_inbox_ai_polish.PolishWarningCode.public_comment_private_data
        for warning in result.warnings
    )


def test_dangerous_draft_returns_staff_warning(db_session, monkeypatch):
    conversation = _conversation(db_session)
    monkeypatch.setattr(
        team_inbox_ai_polish.team_inbox_projection,
        "build_ai_reply_projection",
        lambda _db, *, conversation_id: _projection(),
    )
    _patch_success(
        monkeypatch,
        {},
        suggestion="Your service is guaranteed to be restored today.",
    )

    result = team_inbox_ai_polish.polish_reply(
        db_session,
        team_inbox_ai_polish.TeamInboxAIPolishCommand(
            auth=_auth(),
            actor_person_id=None,
            conversation_id=conversation.id,
            draft="Your service is guaranteed to be restored today.",
        ),
    )

    assert any(
        warning.code is team_inbox_ai_polish.PolishWarningCode.risky_claim
        for warning in result.warnings
    )
