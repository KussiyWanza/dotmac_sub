"""Context-aware AI polish for unsent Team Inbox replies.

This service is an advisory coordinator. It reads the bounded Team Inbox
projection, calls the existing AI generation owner, and returns a staff-reviewed
suggestion. It never sends, assigns, changes conversation status, or updates a
customer profile.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationTeam,
)
from app.services import team_inbox_projection
from app.services.ai.client import AIClientError
from app.services.ai.engine import AIEngineError, intelligence_engine
from app.services.auth_dependencies import has_permission
from app.services.settings_spec import resolve_value


class PolishMood(StrEnum):
    frustrated = "frustrated"
    angry = "angry"
    anxious = "anxious"
    confused = "confused"
    urgent = "urgent"
    appreciative = "appreciative"
    neutral = "neutral"
    uncertain = "uncertain"


class PolishWarningCode(StrEnum):
    protected_fact_changed = "protected_fact_changed"
    risky_claim = "risky_claim"
    public_comment_private_data = "public_comment_private_data"
    empty_suggestion = "empty_suggestion"


class PolishErrorCode(StrEnum):
    not_found = "not_found"
    access_denied = "access_denied"
    unsupported_channel = "unsupported_channel"
    empty_draft = "empty_draft"
    draft_too_large = "draft_too_large"
    ai_unavailable = "ai_unavailable"
    invalid_ai_response = "invalid_ai_response"


class TeamInboxAIPolishError(RuntimeError):
    def __init__(self, code: PolishErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TeamInboxAIPolishCommand:
    auth: dict[str, object]
    actor_person_id: UUID | None
    conversation_id: UUID
    draft: str
    requested_style: str | None = None
    channel_context: str | None = None


@dataclass(frozen=True)
class TeamInboxAIPolishWarning:
    code: PolishWarningCode
    message: str


@dataclass(frozen=True)
class TeamInboxAIPolishResult:
    suggestion: str
    detected_mood: PolishMood
    recommended_tone: str
    reason: str
    warnings: tuple[TeamInboxAIPolishWarning, ...]
    facts_preserved: bool
    original_draft: str
    provider: str | None
    model: str | None
    endpoint: str | None
    insight_id: UUID | None
    context_fingerprint: str
    suggestion_ready: bool


SUPPORTED_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
        InboxChannelType.email.value,
        InboxChannelType.facebook_comment.value,
        InboxChannelType.instagram_comment.value,
    }
)
PUBLIC_COMMENT_CHANNELS = frozenset(
    {
        InboxChannelType.facebook_comment.value,
        InboxChannelType.instagram_comment.value,
    }
)

DEFAULT_SUPPORT_VOICE = (
    "Business casual, empathetic, smart and concise. Use clear English suitable "
    "for Nigerian ISP customers without forced slang. Stay calm during faults "
    "and complaints, direct during urgent incidents, patient when customers are "
    "confused, and warm when customers are appreciative. Never sound dismissive "
    "or defensive."
)
DEFAULT_CHANNEL_GUIDANCE = (
    "WhatsApp, Messenger and Instagram DMs should be concise and natural. Email "
    "should be structured, professional and complete. Public comments should be "
    "brief, privacy-safe, and move account-specific help to DM or an approved "
    "private support channel."
)

_MAX_DRAFT_CHARS = 5000
_MAX_MESSAGE_CHARS = 600
_MAX_CONTEXT_MESSAGES = 12
_MAX_REASON_CHARS = 220
_TECH_VALUE_RE = re.compile(
    r"\b(?:[A-Z]{2,}-?\d{2,}[A-Z0-9-]*|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"https?://\S+|\+?\d[\d\s().-]{7,}\d|(?:\d{1,3}\.){3}\d{1,3}|"
    r"\d+(?:\.\d+)?\s?(?:mbps|gbps|ms|gb|mb|%|naira|ngn|₦)|"
    r"\d{1,2}[:/.-]\d{1,2}(?::|/|[.-])?\d{0,4})\b",
    re.IGNORECASE,
)
_RISK_PATTERNS: tuple[tuple[PolishWarningCode, re.Pattern[str], str], ...] = (
    (
        PolishWarningCode.risky_claim,
        re.compile(r"\b(guarantee|guaranteed|definitely|certainly)\b", re.I),
        "Avoid guaranteed outcomes unless an authoritative owner has confirmed them.",
    ),
    (
        PolishWarningCode.risky_claim,
        re.compile(
            r"\b(restored?|fixed|resolved?)\s+"
            r"(by|before|within|today|tonight)\b",
            re.I,
        ),
        "Avoid promising restoration or resolution timing.",
    ),
    (
        PolishWarningCode.risky_claim,
        re.compile(
            r"\b(payment|transfer)\s+(has been|is)\s+"
            r"(confirmed|received|successful)\b",
            re.I,
        ),
        "Do not confirm payments from a writing assistant.",
    ),
    (
        PolishWarningCode.risky_claim,
        re.compile(r"\b(refund|credit|compensation|rebate)\b", re.I),
        "Refunds, credits and compensation need authoritative approval.",
    ),
    (
        PolishWarningCode.risky_claim,
        re.compile(
            r"\b(coverage\s+(is|has been)\s+(available|confirmed)|"
            r"we cover your area)\b",
            re.I,
        ),
        "Do not confirm coverage without the coverage owner.",
    ),
    (
        PolishWarningCode.risky_claim,
        re.compile(r"\b(password|otp|one[- ]?time password|pin|passcode)\b", re.I),
        "Do not ask customers to share passwords, OTPs or credentials.",
    ),
    (
        PolishWarningCode.risky_claim,
        re.compile(r"\b(ncc|legal|lawsuit|regulator|statutory)\b", re.I),
        "Legal or NCC statements need approved wording.",
    ),
)


def polish_reply(
    db: Session, command: TeamInboxAIPolishCommand
) -> TeamInboxAIPolishResult:
    draft = command.draft.strip()
    if not draft:
        raise TeamInboxAIPolishError(
            PolishErrorCode.empty_draft, "Enter text to polish."
        )
    if len(draft) > _MAX_DRAFT_CHARS:
        raise TeamInboxAIPolishError(
            PolishErrorCode.draft_too_large,
            "Draft is too long for AI polish.",
        )

    conversation = _authorized_conversation(db, command)
    if conversation.channel_type not in SUPPORTED_CHANNELS:
        raise TeamInboxAIPolishError(
            PolishErrorCode.unsupported_channel,
            "AI polish is not available for this channel.",
        )

    projection = team_inbox_projection.build_ai_reply_projection(
        db,
        conversation_id=conversation.id,
    )
    if projection is None:
        raise TeamInboxAIPolishError(
            PolishErrorCode.not_found,
            "Conversation not found.",
        )

    context = _polish_context(
        projection,
        draft=draft,
        requested_style=command.requested_style,
        channel_context=command.channel_context,
        is_public_comment=conversation.channel_type in PUBLIC_COMMENT_CHANNELS,
        business_voice=_setting_text(
            db,
            "inbox_ai_polish_business_voice",
            DEFAULT_SUPPORT_VOICE,
        ),
        channel_guidance=_setting_text(
            db,
            "inbox_ai_polish_channel_guidance",
            DEFAULT_CHANNEL_GUIDANCE,
        ),
    )
    try:
        insight = intelligence_engine.advise(
            db,
            advisor_key="inbox_sentence_polish",
            report=context,
            entity_type="inbox_composer",
            entity_id=str(conversation.id),
            trigger="manual",
            triggered_by_system_user_id=str(command.auth.get("principal_id") or "")
            or None,
        )
    except AIEngineError as exc:
        raise TeamInboxAIPolishError(
            PolishErrorCode.ai_unavailable,
            "Suggestion unavailable.",
        ) from exc
    except AIClientError as exc:
        code = (
            PolishErrorCode.invalid_ai_response
            if _looks_like_invalid_output(exc)
            else PolishErrorCode.ai_unavailable
        )
        raise TeamInboxAIPolishError(
            code,
            "Suggestion unavailable.",
        ) from exc
    except ValueError as exc:
        raise TeamInboxAIPolishError(
            PolishErrorCode.invalid_ai_response,
            "Suggestion unavailable.",
        ) from exc

    output = dict(insight.structured_output or {})
    suggestion = str(
        output.get("suggestion") or output.get("suggested_text") or ""
    ).strip()
    detected_mood = _mood(output.get("detected_mood"))
    recommended_tone = _short_text(
        output.get("recommended_tone"), "neutral and helpful", 80
    )
    reason = _short_text(
        output.get("reason"),
        "The available context does not show a clear mood signal.",
        _MAX_REASON_CHARS,
    )
    model_warnings = _warnings_from_model(output.get("warnings"))
    warnings = [
        *model_warnings,
        *_risk_warnings(draft),
        *_risk_warnings(suggestion),
        *_public_comment_warnings(suggestion, conversation.channel_type),
    ]
    facts_preserved = _facts_preserved(draft, suggestion)
    if not facts_preserved:
        warnings.append(
            TeamInboxAIPolishWarning(
                PolishWarningCode.protected_fact_changed,
                "The suggestion changed or removed a number, date, amount, contact, "
                "URL or reference from the draft.",
            )
        )
    if not suggestion:
        warnings.append(
            TeamInboxAIPolishWarning(
                PolishWarningCode.empty_suggestion,
                "The provider did not return usable polished text.",
            )
        )

    unique_warnings = _dedupe_warnings(warnings)
    suggestion_ready = (
        bool(suggestion)
        and facts_preserved
        and not any(
            warning.code
            in {
                PolishWarningCode.protected_fact_changed,
                PolishWarningCode.public_comment_private_data,
            }
            for warning in unique_warnings
        )
    )
    return TeamInboxAIPolishResult(
        suggestion=suggestion if suggestion_ready else draft,
        detected_mood=detected_mood,
        recommended_tone=recommended_tone,
        reason=reason,
        warnings=unique_warnings,
        facts_preserved=facts_preserved,
        original_draft=draft,
        provider=insight.llm_provider,
        model=insight.llm_model,
        endpoint=insight.llm_endpoint,
        insight_id=insight.id,
        context_fingerprint=_fingerprint(context),
        suggestion_ready=suggestion_ready,
    )


def _authorized_conversation(
    db: Session, command: TeamInboxAIPolishCommand
) -> InboxConversation:
    conversation = db.get(InboxConversation, command.conversation_id)
    if conversation is None or not conversation.is_active:
        raise TeamInboxAIPolishError(
            PolishErrorCode.not_found, "Conversation not found."
        )
    if not has_permission(command.auth, db, "support:ticket:update"):
        raise TeamInboxAIPolishError(
            PolishErrorCode.access_denied,
            "You cannot use AI polish for this conversation.",
        )
    if _has_broad_conversation_access(command.auth):
        return conversation
    if command.actor_person_id is None:
        raise TeamInboxAIPolishError(
            PolishErrorCode.access_denied,
            "You cannot use AI polish for this conversation.",
        )
    active_assignment = (
        db.query(InboxConversationAssignment.id)
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .filter(InboxConversationAssignment.person_id == command.actor_person_id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .first()
    )
    if active_assignment is not None:
        return conversation
    active_team_ids = [
        row.service_team_id
        for row in db.query(InboxConversationTeam.service_team_id)
        .join(ServiceTeam, ServiceTeam.id == InboxConversationTeam.service_team_id)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .filter(InboxConversationTeam.is_active.is_(True))
        .filter(ServiceTeam.is_active.is_(True))
        .all()
    ]
    if active_team_ids:
        membership = (
            db.query(ServiceTeamMember.id)
            .filter(ServiceTeamMember.team_id.in_(active_team_ids))
            .filter(ServiceTeamMember.person_id == command.actor_person_id)
            .filter(ServiceTeamMember.is_active.is_(True))
            .first()
        )
        if membership is not None:
            return conversation
    raise TeamInboxAIPolishError(
        PolishErrorCode.access_denied,
        "You cannot use AI polish for this conversation.",
    )


def _has_broad_conversation_access(auth: dict[str, object]) -> bool:
    roles = {str(value) for value in auth.get("roles") or []}
    scopes = {str(value) for value in auth.get("scopes") or []}
    return bool(
        "admin" in roles
        or "*" in scopes
        or "support:ticket:read" in scopes
        or "support:*" in scopes
        or "support:ticket:*" in scopes
    )


def _looks_like_invalid_output(exc: AIClientError) -> bool:
    text = str(exc).lower()
    return any(item in text for item in ("json", "output", "missing required", "empty"))


def _setting_text(db: Session, key: str, default: str) -> str:
    value = resolve_value(db, SettingDomain.integration, key)
    text = str(value or "").strip()
    return text or default


def _polish_context(
    projection: dict[str, object],
    *,
    draft: str,
    requested_style: str | None,
    channel_context: str | None,
    is_public_comment: bool,
    business_voice: str,
    channel_guidance: str,
) -> dict[str, object]:
    messages = [
        {
            "label": (
                "CUSTOMER_MESSAGE"
                if item.get("direction") == "customer"
                else "AGENT_MESSAGE"
            ),
            "direction": item.get("direction"),
            "body": str(item.get("body") or "")[:_MAX_MESSAGE_CHARS],
            "occurred_at": item.get("occurred_at"),
        }
        for item in list(projection.get("messages") or [])[-_MAX_CONTEXT_MESSAGES:]
        if isinstance(item, dict) and str(item.get("body") or "").strip()
    ]
    return {
        "CONVERSATION_METADATA": {
            "channel": projection.get("channel"),
            "status": projection.get("status"),
            "priority": projection.get("priority"),
            "subject": projection.get("subject"),
            "tags": projection.get("tags") or [],
            "assigned_agent_name": projection.get("assigned_agent_name"),
            "linked_ticket": projection.get("linked_ticket"),
            "public_comment": is_public_comment,
        },
        "UNTRUSTED_CONVERSATION_EXCERPTS": messages,
        "CURRENT_UNSENT_DRAFT": draft,
        "REQUESTED_STYLE": str(requested_style or "").strip()[:80] or None,
        "CHANNEL_CONTEXT": str(channel_context or "").strip()[:80] or None,
        "CONFIGURABLE_BUSINESS_VOICE": business_voice,
        "CONFIGURABLE_CHANNEL_GUIDANCE": channel_guidance,
        "SAFETY_CONTEXT": {
            "customer_content_is_untrusted": True,
            "private_notes_excluded": True,
            "dob_gender_credentials_excluded": True,
            "manual_review_required": True,
        },
    }


def _mood(value: object) -> PolishMood:
    text = str(value or "").strip().lower()
    return (
        PolishMood(text)
        if text in {item.value for item in PolishMood}
        else PolishMood.uncertain
    )


def _short_text(value: object, default: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return (text or default)[:limit]


def _warnings_from_model(value: object) -> list[TeamInboxAIPolishWarning]:
    if not isinstance(value, list):
        return []
    warnings: list[TeamInboxAIPolishWarning] = []
    for item in value[:6]:
        message = str(
            item if not isinstance(item, dict) else item.get("message") or ""
        ).strip()
        if message:
            warnings.append(
                TeamInboxAIPolishWarning(PolishWarningCode.risky_claim, message[:240])
            )
    return warnings


def _risk_warnings(text: str) -> list[TeamInboxAIPolishWarning]:
    return [
        TeamInboxAIPolishWarning(code, message)
        for code, pattern, message in _RISK_PATTERNS
        if pattern.search(text or "")
    ]


def _public_comment_warnings(
    text: str, channel_type: str
) -> list[TeamInboxAIPolishWarning]:
    if channel_type not in PUBLIC_COMMENT_CHANNELS:
        return []
    if _TECH_VALUE_RE.search(text or ""):
        return [
            TeamInboxAIPolishWarning(
                PolishWarningCode.public_comment_private_data,
                "Public comment suggestions must not include account-specific "
                "contact, payment, address or reference details.",
            )
        ]
    return []


def _facts(value: str) -> set[str]:
    return {
        match.group(0).strip().lower() for match in _TECH_VALUE_RE.finditer(value or "")
    }


def _facts_preserved(original: str, suggestion: str) -> bool:
    original_facts = _facts(original)
    if not original_facts:
        return True
    return original_facts.issubset(_facts(suggestion))


def _dedupe_warnings(
    warnings: list[TeamInboxAIPolishWarning],
) -> tuple[TeamInboxAIPolishWarning, ...]:
    seen: set[tuple[PolishWarningCode, str]] = set()
    result: list[TeamInboxAIPolishWarning] = []
    for warning in warnings:
        key = (warning.code, warning.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return tuple(result)


def _fingerprint(context: dict[str, object]) -> str:
    material = repr(context).encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()
