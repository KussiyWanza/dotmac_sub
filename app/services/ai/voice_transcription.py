"""Zero-retention voice transcription transport for the admin Team Inbox."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.ai.security import ai_enabled, resolve_provider_api_key
from app.services.settings_spec import resolve_value

MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_RECORDING_SECONDS = 120
ALLOWED_CONTEXTS = frozenset({"crm_reply", "crm_new_conversation"})
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "audio/webm",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
    }
)
TRANSIENT_STATUS_CODES = frozenset({408, 409, 425, 429})
_active_actor_slots: set[str] = set()
_active_actor_slots_lock = Lock()


class VoiceTranscriptionError(RuntimeError):
    """Safe error that can be shown to the authenticated agent."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = f"ai.voice_transcription.{code}"


def acquire_actor_slot(actor_id: str) -> bool:
    """Bound one in-flight transcription per authenticated agent per worker."""

    key = str(actor_id or "").strip()
    if not key:
        return False
    with _active_actor_slots_lock:
        if key in _active_actor_slots:
            return False
        _active_actor_slots.add(key)
        return True


def release_actor_slot(actor_id: str) -> None:
    with _active_actor_slots_lock:
        _active_actor_slots.discard(str(actor_id or "").strip())


@dataclass(frozen=True, slots=True)
class VoiceTranscriptionResult:
    text: str
    provider: str
    model: str
    endpoint: str
    retry_count: int
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class VoiceTranscriptionConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int
    max_retries: int


def normalized_content_type(value: str | None) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _matches_audio_signature(audio: bytes, content_type: str) -> bool:
    if content_type == "audio/webm":
        return audio.startswith(b"\x1aE\xdf\xa3")
    if content_type == "audio/mp4":
        return len(audio) >= 12 and audio[4:8] == b"ftyp"
    if content_type == "audio/mpeg":
        return audio.startswith(b"ID3") or (
            len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0
        )
    if content_type == "audio/ogg":
        return audio.startswith(b"OggS")
    return False


def _bool(value: object | None) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def _integer(value: object | None, *, default: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 0), maximum)


def _transcription_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    return (
        f"{normalized}/audio/transcriptions"
        if normalized.endswith("/v1")
        else f"{normalized}/v1/audio/transcriptions"
    )


def _safe_endpoint(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _load_config(db: Session) -> VoiceTranscriptionConfig:
    if not ai_enabled(db) or not _bool(
        resolve_value(
            db,
            SettingDomain.integration,
            "voice_transcription_enabled",
        )
    ):
        raise VoiceTranscriptionError(
            "Voice transcription is unavailable.",
            code="disabled",
        )
    base_url = str(
        resolve_value(
            db,
            SettingDomain.integration,
            "voice_transcription_base_url",
        )
        or ""
    ).strip()
    model = str(
        resolve_value(
            db,
            SettingDomain.integration,
            "voice_transcription_model",
        )
        or ""
    ).strip()
    key_setting = resolve_value(
        db,
        SettingDomain.integration,
        "voice_transcription_api_key",
    )
    api_key = resolve_provider_api_key(configured_api_key=key_setting)
    if not base_url or not model or not api_key:
        raise VoiceTranscriptionError(
            "Voice transcription is unavailable.",
            code="not_configured",
        )
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise VoiceTranscriptionError(
            "Voice transcription is unavailable.",
            code="insecure_endpoint",
        )
    return VoiceTranscriptionConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=max(
            1,
            _integer(
                resolve_value(
                    db,
                    SettingDomain.integration,
                    "voice_transcription_timeout_seconds",
                ),
                default=30,
                maximum=120,
            ),
        ),
        max_retries=_integer(
            resolve_value(
                db,
                SettingDomain.integration,
                "voice_transcription_max_retries",
            ),
            default=1,
            maximum=3,
        ),
    )


def transcribe(
    db: Session,
    *,
    audio: bytes,
    content_type: str | None,
    context: str,
    duration_ms: int,
) -> VoiceTranscriptionResult:
    """Transcribe request-scoped bytes without persisting audio or transcript."""

    clean_context = str(context or "").strip()
    if clean_context not in ALLOWED_CONTEXTS:
        raise VoiceTranscriptionError("Invalid voice context.", code="invalid_context")
    if duration_ms <= 0 or duration_ms > MAX_RECORDING_SECONDS * 1000:
        raise VoiceTranscriptionError(
            "Recording must be 120 seconds or less.",
            code="invalid_duration",
        )
    if not audio:
        raise VoiceTranscriptionError("Record some audio first.", code="empty_audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceTranscriptionError(
            "Recording is too large.",
            code="audio_too_large",
        )
    clean_content_type = normalized_content_type(content_type)
    if clean_content_type not in ALLOWED_CONTENT_TYPES:
        raise VoiceTranscriptionError(
            "This recording format is not supported.",
            code="unsupported_audio",
        )
    if not _matches_audio_signature(audio, clean_content_type):
        raise VoiceTranscriptionError(
            "This recording format is not supported.",
            code="invalid_audio_signature",
        )

    config = _load_config(db)
    endpoint = _transcription_url(config.base_url)
    started = time.monotonic()
    attempt = 0
    while True:
        try:
            with httpx.Client(timeout=config.timeout_seconds) as client:
                response = client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {config.api_key}"},
                    data={
                        "model": config.model,
                        "response_format": "json",
                    },
                    files={
                        "file": (
                            "recording",
                            audio,
                            clean_content_type,
                        )
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt >= config.max_retries:
                raise VoiceTranscriptionError(
                    "Transcription provider is temporarily unavailable.",
                    code="provider_unavailable",
                ) from None
            time.sleep(0.25 * (2**attempt))
            attempt += 1
            continue

        transient = (
            response.status_code in TRANSIENT_STATUS_CODES
            or response.status_code >= 500
        )
        if transient and attempt < config.max_retries:
            time.sleep(0.25 * (2**attempt))
            attempt += 1
            continue
        if response.status_code >= 400:
            raise VoiceTranscriptionError(
                "Transcription failed. Please try again.",
                code="provider_rejected",
            )
        try:
            payload = response.json()
        except ValueError:
            raise VoiceTranscriptionError(
                "Transcription failed. Please try again.",
                code="invalid_provider_response",
            ) from None
        text = (
            str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        )
        if not text:
            raise VoiceTranscriptionError(
                "No speech was detected.",
                code="empty_transcript",
            )
        return VoiceTranscriptionResult(
            text=text,
            provider="voice_transcription",
            model=config.model,
            endpoint=_safe_endpoint(endpoint),
            retry_count=attempt,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
