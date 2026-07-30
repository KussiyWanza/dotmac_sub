"""Focused guards for the implemented CRM-inbox replication controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ai import voice_transcription
from app.services.ai.advisors import (
    InputSensitivity,
    advisor_registry,
)
from app.services.integrations.connectors.whatsapp_runtime import (
    WHATSAPP_PROVIDER_META,
    build_template_payload,
)
from app.services.team_inbox_projection import _plain_ai_message_body

VOICE_JAVASCRIPT = Path("static/js/voice-input.js").read_text(encoding="utf-8")
INBOX_JAVASCRIPT = Path("static/js/admin-inbox.js").read_text(encoding="utf-8")
CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text(
    encoding="utf-8"
)
OVERLAYS = Path("templates/admin/inbox/_overlays.html").read_text(encoding="utf-8")


def test_inbox_ai_advisors_are_customer_content_and_never_auto_send():
    draft = advisor_registry.get("inbox_analyst")
    polish = advisor_registry.get("inbox_sentence_polish")

    assert draft.projection_key == "team_inbox_projection.ai_reply_context"
    assert draft.input_sensitivity is InputSensitivity.CUSTOMER_CONTENT
    assert draft.insight_ttl_hours == 24
    assert polish.input_sensitivity is InputSensitivity.CUSTOMER_CONTENT
    assert "insertAiDraft()" in CONVERSATION
    assert "acceptPolish(" in CONVERSATION
    assert "draftWithAI()" in INBOX_JAVASCRIPT


def test_inbox_ai_context_strips_html_and_bounds_each_message():
    source = "<p>Hello&nbsp;<strong>Ada</strong></p>" + ("x" * 700)

    result = _plain_ai_message_body(source)

    assert result.startswith("Hello Ada")
    assert "<" not in result
    assert len(result) == 600


@pytest.mark.parametrize(
    ("audio", "content_type", "context", "code"),
    [
        (b"", "audio/webm", "crm_reply", "empty_audio"),
        (b"not-webm", "audio/webm", "crm_reply", "invalid_audio_signature"),
        (b"\x1aE\xdf\xa3data", "audio/webm", "unknown", "invalid_context"),
    ],
)
def test_voice_validation_fails_before_provider_egress(
    audio, content_type, context, code
):
    with pytest.raises(voice_transcription.VoiceTranscriptionError) as exc:
        voice_transcription.transcribe(
            None,
            audio=audio,
            content_type=content_type,
            context=context,
            duration_ms=1000,
        )
    assert exc.value.code == f"ai.voice_transcription.{code}"


def test_voice_transcription_uses_configured_server_side_transport(monkeypatch):
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "Please check the line."}

    class Client:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    values = {
        "voice_transcription_enabled": True,
        "voice_transcription_base_url": "https://voice.example/v1",
        "voice_transcription_model": "approved-transcriber",
        "voice_transcription_api_key": "bao://ai/voice#api_key",
        "voice_transcription_timeout_seconds": 20,
        "voice_transcription_max_retries": 1,
    }
    monkeypatch.setattr(voice_transcription, "ai_enabled", lambda _db: True)
    monkeypatch.setattr(
        voice_transcription,
        "resolve_value",
        lambda _db, _domain, key: values.get(key),
    )
    monkeypatch.setattr(
        voice_transcription,
        "resolve_provider_api_key",
        lambda **_kwargs: "resolved-secret",
    )
    monkeypatch.setattr(voice_transcription.httpx, "Client", Client)

    result = voice_transcription.transcribe(
        None,
        audio=b"\x1aE\xdf\xa3voice",
        content_type="audio/webm;codecs=opus",
        context="crm_reply",
        duration_ms=1000,
    )

    assert result.text == "Please check the line."
    assert captured["url"] == "https://voice.example/v1/audio/transcriptions"
    assert captured["headers"] == {"Authorization": "Bearer resolved-secret"}
    assert captured["data"] == {
        "model": "approved-transcriber",
        "response_format": "json",
    }


def test_voice_control_has_bounded_recording_and_no_browser_vendor_fallback():
    assert "MAX_RECORDING_MS = 120000" in VOICE_JAVASCRIPT
    assert 'form.append("duration_ms"' in VOICE_JAVASCRIPT
    assert "MediaRecorder" in VOICE_JAVASCRIPT
    assert "pointerHeld" in VOICE_JAVASCRIPT
    assert "SpeechRecognition" not in VOICE_JAVASCRIPT
    assert "webkitSpeechRecognition" not in VOICE_JAVASCRIPT


def test_voice_duration_is_rejected_before_provider_egress():
    with pytest.raises(voice_transcription.VoiceTranscriptionError) as exc:
        voice_transcription.transcribe(
            None,
            audio=b"\x1aE\xdf\xa3voice",
            content_type="audio/webm",
            context="crm_reply",
            duration_ms=120001,
        )
    assert exc.value.code == "ai.voice_transcription.invalid_duration"


def test_voice_concurrency_slot_is_one_per_agent():
    assert voice_transcription.acquire_actor_slot("agent-1")
    try:
        assert not voice_transcription.acquire_actor_slot("agent-1")
        assert voice_transcription.acquire_actor_slot("agent-2")
        voice_transcription.release_actor_slot("agent-2")
    finally:
        voice_transcription.release_actor_slot("agent-1")


def test_whatsapp_explicit_components_reach_meta_payload():
    components = [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Ada"}],
        },
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": "account/1"}],
        },
    ]
    payload = build_template_payload(
        provider=WHATSAPP_PROVIDER_META,
        recipient="+2348012345678",
        template_name="welcome_customer",
        language="en",
        variables={},
        components=components,
    )

    assert payload["template"]["components"] == components
    assert 'name="whatsapp_template_components"' in OVERLAYS
    assert "whatsappTemplateFields(" in INBOX_JAVASCRIPT
    assert "buttonIndex" in INBOX_JAVASCRIPT
