"""Durable conversational intake session helpers.

The AI intake owner records AI lifecycle, policy/version provenance and
generation evidence. Team Inbox remains the owner for conversation status,
routing, queueing, assignment and outbound delivery.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_intake import (
    AiIntakeConfig,
    AiIntakeGenerationAttempt,
    AiIntakePolicy,
    AiIntakePolicyVersion,
    AiIntakeSession,
)
from app.models.service_team import ServiceTeam
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxMessage,
    InboxMessageDirection,
)
from app.schemas.ai_intake import AiIntakeOutcome, AiIntakeStatus
from app.services import team_inbox_status
from app.services.integrations import (
    installations,
    meta_social_capability,
    whatsapp_capability,
)
from app.services.integrations.connectors.meta_social_runtime import (
    META_SOCIAL_RECEIVE_CAPABILITY,
    META_SOCIAL_SEND_CAPABILITY,
)
from app.services.integrations.connectors.whatsapp_runtime import WHATSAPP_PROVIDER_META
from app.services.integrations.meta_social_installation import (
    get_meta_social_installation_projection,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

SUPPORTED_CONVERSATIONAL_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
    }
)
TERMINAL_SESSION_STATES = frozenset(
    {
        "completed",
        "stopped_human_takeover",
        "fallback_escalated",
        "expired",
        "failed",
        "ineligible",
    }
)
DEFAULT_DISPLAY_NAME = "Dotmac Virtual Assistant"
DEFAULT_WELCOME_MESSAGE = (
    "Hello, I am Dotmac Virtual Assistant. I can help understand your request "
    "and connect you to the right team."
)
DEFAULT_QUEUE_POSITION_UPDATE_MINUTES = 5
DEFAULT_QUEUE_HEARTBEAT_MINUTES = 15
DEFAULT_QUEUE_TEMPLATES = {
    "initial": (
        "All our agents are currently engaged. You are number {position} in the "
        "queue. We will update you as your position changes."
    ),
    "position_update": "Quick update: you are now number {position} in the queue.",
    "heartbeat": (
        "You are still number {position} in the queue. We will connect you as "
        "soon as an agent is available."
    ),
    "handoff": "Thanks for waiting. An agent has joined and will continue from here.",
}
DEFAULT_DATA_CLEANUP_PROMPT = (
    "For NCC compliance, Dotmac needs your {fields}. You can share only the "
    "missing details, or tell us if you prefer not to disclose."
)
DEFAULT_DATA_CLEANUP_TEMPLATES = {
    "ask": DEFAULT_DATA_CLEANUP_PROMPT,
    "invalid_retry": (
        "I could not safely validate that detail. Please send only your "
        "{fields}, or tell us if you prefer not to disclose."
    ),
    "refused": ("No problem. I will leave that for the team to follow up if needed."),
    "saved": "Thank you. I have recorded the submitted profile details for review.",
    "follow_up": (
        "I will leave this for the team to follow up because I could not "
        "validate the requested details."
    ),
}
DEFAULT_GENDER_CHOICES = {
    "male": "male",
    "female": "female",
    "non_binary": "non_binary",
    "other": "other",
    "prefer_not_to_say": "unknown",
}
_AI_SESSION_COMMAND = OwnerCommandDefinition(
    owner="ai.intake",
    concern="AI conversational intake session lifecycle",
    name="execute_ai_intake_session_command",
)
_AI_POLICY_VERSION_COMMAND = OwnerCommandDefinition(
    owner="ai.intake",
    concern="AI conversational intake policy-version lifecycle",
    name="execute_ai_intake_policy_version_command",
)
_AI_POLICY_DRAFT_COMMAND = OwnerCommandDefinition(
    owner="ai.intake",
    concern="AI conversational intake configuration lifecycle",
    name="create_ai_intake_draft_policy",
)


@dataclass(frozen=True, slots=True)
class AiSessionProcessCommand:
    context: CommandContext
    limit: int = 100
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class AiSessionProcessResult:
    processed: int
    skipped: int
    failed: int


@dataclass(frozen=True, slots=True)
class AiSessionContext:
    session: AiIntakeSession
    policy: AiIntakePolicy | None
    version: AiIntakePolicyVersion | None


@dataclass(frozen=True, slots=True)
class AiPolicyVersionDraftCommand:
    context: CommandContext
    policy_id: UUID
    base_version_id: UUID | None = None
    display_name: str | None = None
    welcome_message: str | None = None
    business_tone: str | None = None
    business_instructions: str | None = None
    approved_isp_information: str | None = None
    intent_definitions: tuple[Mapping[str, object], ...] = ()
    clarification_questions: tuple[Mapping[str, object], ...] = ()
    intent_team_mappings: tuple[Mapping[str, object], ...] = ()
    queue_templates: Mapping[str, object] | None = None
    escalation_rules: Mapping[str, object] | None = None
    data_cleanup_policy: Mapping[str, object] | None = None
    replace_existing_draft: bool = True


@dataclass(frozen=True, slots=True)
class AiDraftPolicyCommand:
    context: CommandContext
    channel_type: str
    provider: str
    account_scope: str
    display_name: str | None = None
    fallback_team_id: UUID | None = None
    base_version_id: UUID | None = None
    welcome_message: str | None = None
    business_tone: str | None = None
    business_instructions: str | None = None
    approved_isp_information: str | None = None
    intent_definitions: tuple[Mapping[str, object], ...] = ()
    clarification_questions: tuple[Mapping[str, object], ...] = ()
    intent_team_mappings: tuple[Mapping[str, object], ...] = ()
    queue_templates: Mapping[str, object] | None = None
    escalation_rules: Mapping[str, object] | None = None
    data_cleanup_policy: Mapping[str, object] | None = None
    replace_existing_draft: bool = False


@dataclass(frozen=True, slots=True)
class AiPolicyVersionActivateCommand:
    context: CommandContext
    version_id: UUID
    actor_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AiPolicyVersionOutcome:
    policy_id: UUID
    version_id: UUID
    version_number: int
    status: str
    active_version_id: UUID | None


@dataclass(frozen=True, slots=True)
class AiDraftPolicyOutcome:
    policy_id: UUID
    version_id: UUID
    version_number: int
    policy_enabled: bool
    version_status: str
    active_version_id: UUID | None
    channel_type: str
    provider: str
    account_scope: str
    scope_key: str


def is_supported_channel(channel_type: str | None) -> bool:
    return str(channel_type or "").strip() in SUPPORTED_CONVERSATIONAL_CHANNELS


def _normalize_text(value: str | None, *, field: str, limit: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"AI intake {field} is required")
    return text[:limit]


def _provider_scope_key(provider: str, account_scope: str) -> str:
    return f"{provider}:{account_scope}"


def _active_service_team_id(
    db: Session, team_id: UUID | None, *, field: str
) -> UUID | None:
    if team_id is None:
        return None
    team = db.get(ServiceTeam, team_id)
    if team is None or not team.is_active:
        raise ValueError(f"AI intake {field} must reference an active team")
    return team.id


def _validate_mapping_team_references(
    db: Session, mappings: tuple[Mapping[str, object], ...]
) -> None:
    for mapping in mappings:
        if mapping.get("enabled") is False:
            continue
        raw_team_id = mapping.get("service_team_id") or mapping.get("team_id")
        if raw_team_id in (None, ""):
            continue
        try:
            team_id = UUID(str(raw_team_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("AI intake intent mapping team id is invalid") from exc
        _active_service_team_id(db, team_id, field="intent mapping team")


def _validate_whatsapp_provider_scope(
    db: Session, *, provider: str, account_scope: str
) -> None:
    if provider != WHATSAPP_PROVIDER_META:
        raise ValueError("WhatsApp AI intake provider scope is unsupported")
    try:
        send_binding = whatsapp_capability.require_binding(
            db, capability_id=whatsapp_capability.WHATSAPP_SEND_CAPABILITY
        )
        receive_binding = whatsapp_capability.require_binding(
            db, capability_id=whatsapp_capability.WHATSAPP_RECEIVE_CAPABILITY
        )
    except installations.InstallationError as exc:
        raise ValueError(
            "WhatsApp AI intake requires enabled send and receive capabilities"
        ) from exc
    send_revision = send_binding.installation.current_config_revision
    receive_revision = receive_binding.installation.current_config_revision
    if send_revision is None or receive_revision is None:
        raise ValueError(
            "WhatsApp AI intake requires validated configuration revisions"
        )
    config = dict(send_revision.config_json or {})
    configured_phone_number_id = str(config.get("phone_number_id") or "").strip()
    if not configured_phone_number_id:
        raise ValueError("WhatsApp AI intake requires a configured phone number id")
    if configured_phone_number_id != account_scope:
        raise ValueError("WhatsApp AI intake account scope must match phone number id")
    receive_config = dict(receive_revision.config_json or {})
    receive_phone_number_id = str(receive_config.get("phone_number_id") or "").strip()
    if receive_phone_number_id and receive_phone_number_id != account_scope:
        raise ValueError("WhatsApp inbound capability uses a different account scope")


def _validate_meta_social_provider_scope(
    db: Session, *, channel_type: str, provider: str, account_scope: str
) -> None:
    if provider != meta_social_capability.META_SOCIAL_CONNECTOR_KEY:
        raise ValueError("Meta private-message AI intake provider scope is unsupported")
    try:
        meta_social_capability.require_binding(
            db, capability_id=META_SOCIAL_SEND_CAPABILITY
        )
        meta_social_capability.require_binding(
            db, capability_id=META_SOCIAL_RECEIVE_CAPABILITY
        )
    except installations.InstallationError as exc:
        raise ValueError(
            "Meta private-message AI intake requires enabled send and receive capabilities"
        ) from exc
    projection = get_meta_social_installation_projection(db)
    expected_scope = (
        projection.facebook_page_id
        if channel_type == InboxChannelType.facebook_messenger.value
        else projection.instagram_account_id
    )
    if not expected_scope or expected_scope != account_scope:
        raise ValueError(
            "Meta private-message AI intake account scope is not configured"
        )


def _validate_provider_scope(
    db: Session, *, channel_type: str, provider: str, account_scope: str
) -> None:
    if channel_type not in SUPPORTED_CONVERSATIONAL_CHANNELS:
        raise ValueError("AI intake supports only WhatsApp, Messenger and Instagram DM")
    if account_scope.lower() in {"any", "global", "default"}:
        raise ValueError("AI intake draft policy requires an explicit account scope")
    if channel_type == InboxChannelType.whatsapp.value:
        _validate_whatsapp_provider_scope(
            db, provider=provider, account_scope=account_scope
        )
        return
    _validate_meta_social_provider_scope(
        db,
        channel_type=channel_type,
        provider=provider,
        account_scope=account_scope,
    )


def create_draft_policy(
    db: Session, command: AiDraftPolicyCommand
) -> AiDraftPolicyOutcome:
    """Create or update an inactive policy shell and one editable draft version."""

    def _operation() -> AiDraftPolicyOutcome:
        channel = _normalize_text(command.channel_type, field="channel", limit=40)
        provider = _normalize_text(command.provider, field="provider", limit=80)
        account_scope = _normalize_text(
            command.account_scope, field="account scope", limit=160
        )
        _validate_provider_scope(
            db,
            channel_type=channel,
            provider=provider,
            account_scope=account_scope,
        )
        fallback_team_id = _active_service_team_id(
            db, command.fallback_team_id, field="fallback team"
        )
        _validate_mapping_team_references(db, command.intent_team_mappings)
        scope_key = _provider_scope_key(provider, account_scope)
        conflicting_policy = (
            db.query(AiIntakePolicy)
            .filter(AiIntakePolicy.scope_key == scope_key)
            .filter(AiIntakePolicy.channel_type == channel)
            .filter(
                (AiIntakePolicy.provider != provider)
                | (AiIntakePolicy.account_scope != account_scope)
            )
            .one_or_none()
        )
        if conflicting_policy is not None:
            raise ValueError(
                "AI intake policy identity conflicts with an existing policy"
            )
        policy = (
            db.query(AiIntakePolicy)
            .filter(AiIntakePolicy.scope_key == scope_key)
            .filter(AiIntakePolicy.channel_type == channel)
            .filter(AiIntakePolicy.provider == provider)
            .filter(AiIntakePolicy.account_scope == account_scope)
            .with_for_update()
            .one_or_none()
        )
        if policy is None:
            draft_display_name = (
                str(command.display_name or DEFAULT_DISPLAY_NAME).strip()[:120]
                or DEFAULT_DISPLAY_NAME
            )
            policy = AiIntakePolicy(
                legacy_config_id=None,
                scope_key=scope_key,
                channel_type=channel,
                provider=provider,
                account_scope=account_scope,
                display_name=draft_display_name,
                is_enabled=False,
                active_version_id=None,
                fallback_team_id=fallback_team_id,
                metadata_={
                    "created_reason": command.context.reason,
                    "created_from": "canonical_draft_policy_command",
                },
            )
            db.add(policy)
            db.flush()
        else:
            draft_display_name = (
                str(
                    command.display_name or policy.display_name or DEFAULT_DISPLAY_NAME
                ).strip()[:120]
                or DEFAULT_DISPLAY_NAME
            )
            if not policy.is_enabled and policy.active_version_id is None:
                policy.display_name = draft_display_name
                policy.fallback_team_id = fallback_team_id
            policy.metadata_ = {
                **dict(policy.metadata_ or {}),
                "last_draft_reason": command.context.reason,
            }
        outcome = _create_or_update_draft_policy_version_locked(
            db,
            command=AiPolicyVersionDraftCommand(
                context=command.context,
                policy_id=policy.id,
                base_version_id=command.base_version_id,
                display_name=draft_display_name,
                welcome_message=command.welcome_message,
                business_tone=command.business_tone,
                business_instructions=command.business_instructions,
                approved_isp_information=command.approved_isp_information,
                intent_definitions=command.intent_definitions,
                clarification_questions=command.clarification_questions,
                intent_team_mappings=command.intent_team_mappings,
                queue_templates=command.queue_templates,
                escalation_rules=command.escalation_rules,
                data_cleanup_policy=command.data_cleanup_policy,
                replace_existing_draft=command.replace_existing_draft,
            ),
        )
        if policy.active_version_id is None:
            policy.is_enabled = False
        db.flush()
        return AiDraftPolicyOutcome(
            policy_id=policy.id,
            version_id=outcome.version_id,
            version_number=outcome.version_number,
            policy_enabled=policy.is_enabled,
            version_status=outcome.status,
            active_version_id=policy.active_version_id,
            channel_type=policy.channel_type,
            provider=policy.provider,
            account_scope=policy.account_scope,
            scope_key=policy.scope_key,
        )

    return execute_owner_command(
        db,
        definition=_AI_POLICY_DRAFT_COMMAND,
        context=command.context,
        operation=_operation,
    )


def _next_policy_version_number(db: Session, policy_id: UUID) -> int:
    latest = (
        db.query(func.max(AiIntakePolicyVersion.version_number))
        .filter(AiIntakePolicyVersion.policy_id == policy_id)
        .scalar()
        or 0
    )
    return int(latest) + 1


def _copy_version_payload(
    base: AiIntakePolicyVersion | None,
    command: AiPolicyVersionDraftCommand,
) -> dict[str, object | None]:
    return {
        "display_name": command.display_name
        or (base.display_name if base is not None else DEFAULT_DISPLAY_NAME),
        "welcome_message": command.welcome_message
        or (base.welcome_message if base is not None else DEFAULT_WELCOME_MESSAGE),
        "business_tone": command.business_tone
        if command.business_tone is not None
        else (base.business_tone if base is not None else None),
        "business_instructions": command.business_instructions
        if command.business_instructions is not None
        else (base.business_instructions if base is not None else None),
        "approved_isp_information": command.approved_isp_information
        if command.approved_isp_information is not None
        else (base.approved_isp_information if base is not None else None),
        "intent_definitions": list(command.intent_definitions)
        if command.intent_definitions
        else (list(base.intent_definitions or []) if base is not None else None),
        "clarification_questions": list(command.clarification_questions)
        if command.clarification_questions
        else (list(base.clarification_questions or []) if base is not None else None),
        "intent_team_mappings": list(command.intent_team_mappings)
        if command.intent_team_mappings
        else (list(base.intent_team_mappings or []) if base is not None else None),
        "queue_templates": dict(command.queue_templates or {})
        if command.queue_templates is not None
        else (dict(base.queue_templates or {}) if base is not None else None),
        "escalation_rules": dict(command.escalation_rules or {})
        if command.escalation_rules is not None
        else (dict(base.escalation_rules or {}) if base is not None else None),
        "data_cleanup_policy": dict(command.data_cleanup_policy or {})
        if command.data_cleanup_policy is not None
        else (dict(base.data_cleanup_policy or {}) if base is not None else None),
    }


def _create_or_update_draft_policy_version_locked(
    db: Session, *, command: AiPolicyVersionDraftCommand
) -> AiPolicyVersionOutcome:
    policy = (
        db.query(AiIntakePolicy)
        .filter(AiIntakePolicy.id == command.policy_id)
        .with_for_update()
        .one_or_none()
    )
    if policy is None:
        raise ValueError("AI intake policy was not found")
    base = (
        db.get(AiIntakePolicyVersion, command.base_version_id)
        if command.base_version_id is not None
        else None
    )
    if base is not None and base.policy_id != policy.id:
        raise ValueError("Base AI intake policy version does not belong to policy")
    existing_draft = (
        db.query(AiIntakePolicyVersion)
        .filter(AiIntakePolicyVersion.policy_id == policy.id)
        .filter(AiIntakePolicyVersion.status == "draft")
        .with_for_update()
        .one_or_none()
    )
    if existing_draft is not None and not command.replace_existing_draft:
        raise ValueError("AI intake policy already has an editable draft")
    payload = _copy_version_payload(base, command)
    version = existing_draft
    if version is None:
        version = AiIntakePolicyVersion(
            policy_id=policy.id,
            version_number=_next_policy_version_number(db, policy.id),
            status="draft",
            is_active=False,
            created_by_person_id=None,
            metadata_={
                "created_reason": command.context.reason,
                "base_version_id": str(base.id) if base is not None else None,
            },
            **payload,
        )
        db.add(version)
    else:
        for field, value in payload.items():
            setattr(version, field, value)
    db.flush()
    return AiPolicyVersionOutcome(
        policy_id=policy.id,
        version_id=version.id,
        version_number=version.version_number,
        status=version.status,
        active_version_id=policy.active_version_id,
    )


def create_or_update_draft_policy_version(
    db: Session, command: AiPolicyVersionDraftCommand
) -> AiPolicyVersionOutcome:
    """Create a new editable draft; activated versions are never mutated."""

    def _operation() -> AiPolicyVersionOutcome:
        return _create_or_update_draft_policy_version_locked(db, command=command)

    return execute_owner_command(
        db,
        definition=_AI_POLICY_VERSION_COMMAND,
        context=command.context,
        operation=_operation,
    )


def _validate_activation(
    db: Session, *, policy: AiIntakePolicy, version: AiIntakePolicyVersion
) -> None:
    if str(policy.scope_key or "").strip().lower() in {"", "global", "default", "any"}:
        raise ValueError("AI intake activation requires an explicit provider scope")
    if policy.fallback_team_id is None:
        raise ValueError("AI intake activation requires a fallback team")
    fallback = db.get(ServiceTeam, policy.fallback_team_id)
    if fallback is None or not fallback.is_active:
        raise ValueError("AI intake fallback team must be active")
    if not (version.welcome_message or "").strip():
        raise ValueError("AI intake activation requires a welcome message")
    mappings = version.intent_team_mappings or []
    if not isinstance(mappings, list):
        raise ValueError("AI intake intent mappings must be a list")
    enabled_mapping_count = 0
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            raise ValueError("AI intake intent mapping entries must be objects")
        if mapping.get("enabled") is False:
            continue
        raw_team_id = mapping.get("service_team_id") or mapping.get("team_id")
        if not raw_team_id:
            continue
        try:
            team_id = UUID(str(raw_team_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("AI intake intent mapping team id is invalid") from exc
        team = db.get(ServiceTeam, team_id)
        if team is None or not team.is_active:
            raise ValueError("AI intake intent mapping team must be active")
        enabled_mapping_count += 1
    if enabled_mapping_count == 0:
        raise ValueError("AI intake activation requires an active intent mapping")


def activate_policy_version(
    db: Session, command: AiPolicyVersionActivateCommand
) -> AiPolicyVersionOutcome:
    """Activate one draft version and supersede the previous active version."""

    def _operation() -> AiPolicyVersionOutcome:
        version = (
            db.query(AiIntakePolicyVersion)
            .filter(AiIntakePolicyVersion.id == command.version_id)
            .with_for_update()
            .one_or_none()
        )
        if version is None:
            raise ValueError("AI intake policy version was not found")
        policy = (
            db.query(AiIntakePolicy)
            .filter(AiIntakePolicy.id == version.policy_id)
            .with_for_update()
            .one()
        )
        if version.status != "draft":
            raise ValueError("Only draft AI intake policy versions can be activated")
        _validate_activation(db, policy=policy, version=version)
        previous = (
            db.get(AiIntakePolicyVersion, policy.active_version_id)
            if policy.active_version_id is not None
            else None
        )
        now = datetime.now(UTC)
        version.status = "activated"
        version.is_active = True
        version.activated_at = now
        version.activated_by_person_id = command.actor_person_id
        policy.active_version_id = version.id
        policy.is_enabled = True
        if previous is not None and previous.id != version.id:
            previous.status = "superseded"
            previous.is_active = False
            previous.superseded_at = now
            previous.superseded_by_version_id = version.id
        db.flush()
        return AiPolicyVersionOutcome(
            policy_id=policy.id,
            version_id=version.id,
            version_number=version.version_number,
            status=version.status,
            active_version_id=policy.active_version_id,
        )

    return execute_owner_command(
        db,
        definition=_AI_POLICY_VERSION_COMMAND,
        context=command.context,
        operation=_operation,
    )


def active_session_for_conversation(
    db: Session, conversation_id: UUID
) -> AiIntakeSession | None:
    return (
        db.query(AiIntakeSession)
        .filter(AiIntakeSession.conversation_id == conversation_id)
        .filter(AiIntakeSession.completed_at.is_(None))
        .with_for_update()
        .one_or_none()
    )


def has_human_takeover(db: Session, conversation: InboxConversation) -> bool:
    if (
        not conversation.is_active
        or conversation.status == InboxConversationStatus.resolved.value
        or not is_supported_channel(conversation.channel_type)
    ):
        return True
    active_assignment = (
        db.query(InboxConversationAssignment.id)
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .first()
    )
    if active_assignment is not None:
        return True
    human_reply = (
        db.query(InboxMessage.id)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(
            func.coalesce(InboxMessage.metadata_["sent_by_person_id"].as_string(), "")
            != ""
        )
        .first()
    )
    return human_reply is not None


def ensure_policy_version_from_legacy_config(
    db: Session,
    *,
    config_id: UUID,
    provider: str,
    account_scope: str,
) -> tuple[AiIntakePolicy, AiIntakePolicyVersion]:
    config = db.get(AiIntakeConfig, config_id)
    if config is None:
        raise ValueError("AI intake config was not found")
    metadata = dict(config.metadata_ or {})
    display_name = str(metadata.get("display_name") or DEFAULT_DISPLAY_NAME).strip()
    if not display_name:
        display_name = DEFAULT_DISPLAY_NAME
    welcome_message = str(
        metadata.get("welcome_message") or DEFAULT_WELCOME_MESSAGE
    ).strip()
    if not welcome_message:
        welcome_message = DEFAULT_WELCOME_MESSAGE
    policy = (
        db.query(AiIntakePolicy)
        .filter(AiIntakePolicy.legacy_config_id == config.id)
        .filter(AiIntakePolicy.scope_key == config.scope_key)
        .filter(AiIntakePolicy.channel_type == config.channel_type)
        .filter(AiIntakePolicy.provider == "any")
        .filter(AiIntakePolicy.account_scope == "any")
        .one_or_none()
    )
    if policy is None:
        policy = AiIntakePolicy(
            legacy_config_id=config.id,
            scope_key=config.scope_key,
            channel_type=config.channel_type,
            provider="any",
            account_scope="any",
            display_name=display_name,
            is_enabled=config.is_enabled,
            fallback_team_id=config.fallback_team_id,
            metadata_={"compatibility_source": "ai_intake_configs"},
        )
        db.add(policy)
        db.flush()
    policy.display_name = display_name
    policy.is_enabled = config.is_enabled
    policy.fallback_team_id = config.fallback_team_id
    policy.metadata_ = {
        **dict(policy.metadata_ or {}),
        "compatibility_source": "ai_intake_configs",
        "max_turns": int(config.max_clarification_turns or 1) + 1,
        "confidence_threshold": float(config.confidence_threshold),
        "max_intake_duration_minutes": int(config.escalate_after_minutes or 5),
        "data_cleanup_enabled": bool(metadata.get("data_cleanup_enabled") or False),
    }

    last_version = (
        db.query(AiIntakePolicyVersion)
        .filter(AiIntakePolicyVersion.policy_id == policy.id)
        .order_by(AiIntakePolicyVersion.version_number.desc())
        .first()
    )
    queue_templates = metadata.get("queue_templates")
    if not isinstance(queue_templates, dict):
        queue_templates = {}
    queue_templates = {
        **DEFAULT_QUEUE_TEMPLATES,
        **{
            str(key): str(value)
            for key, value in queue_templates.items()
            if key in DEFAULT_QUEUE_TEMPLATES and str(value).strip()
        },
        "position_update_minutes": int(
            metadata.get("queue_position_update_minutes")
            or DEFAULT_QUEUE_POSITION_UPDATE_MINUTES
        ),
        "heartbeat_minutes": int(
            metadata.get("queue_heartbeat_minutes") or DEFAULT_QUEUE_HEARTBEAT_MINUTES
        ),
    }
    data_cleanup_policy = metadata.get("data_cleanup_policy")
    if not isinstance(data_cleanup_policy, dict):
        data_cleanup_policy = {}
    data_cleanup_policy = {
        **data_cleanup_policy,
        "prompt": str(
            data_cleanup_policy.get("prompt")
            or metadata.get("data_cleanup_prompt")
            or DEFAULT_DATA_CLEANUP_PROMPT
        ),
        "templates": {
            **DEFAULT_DATA_CLEANUP_TEMPLATES,
            **{
                str(key): str(value)
                for key, value in dict(
                    data_cleanup_policy.get("templates") or {}
                ).items()
                if key in DEFAULT_DATA_CLEANUP_TEMPLATES and str(value).strip()
            },
        },
        "max_attempts": 2,
        "gender_choices": data_cleanup_policy.get("gender_choices")
        or DEFAULT_GENDER_CHOICES,
        "dob_formats": data_cleanup_policy.get("dob_formats")
        or ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"),
        "production_collection_enabled": bool(
            metadata.get("data_cleanup_enabled") or False
        ),
    }
    version_signature = {
        "instructions": config.instructions or "",
        "welcome_message": welcome_message,
        "department_mappings": config.department_mappings or [],
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key
            not in {
                "preview_last_input",
                "preview_last_output",
                "last_prompt",
                "raw_prompt",
            }
        },
    }
    last_signature = (
        dict(last_version.metadata_ or {}).get("compatibility_signature")
        if last_version is not None
        else None
    )
    if last_version is None or last_signature != version_signature:
        version = AiIntakePolicyVersion(
            policy_id=policy.id,
            version_number=(last_version.version_number + 1 if last_version else 1),
            status="activated",
            is_active=True,
            activated_at=datetime.now(UTC),
            display_name=display_name,
            welcome_message=welcome_message,
            business_tone=str(
                metadata.get("business_tone")
                or "Business casual, empathetic, smart and concise for a Nigerian ISP."
            ),
            business_instructions=config.instructions,
            approved_isp_information=str(metadata.get("approved_isp_information") or "")
            or None,
            intent_definitions=metadata.get("intent_definitions"),
            clarification_questions=metadata.get("clarification_questions"),
            intent_team_mappings=config.department_mappings,
            queue_templates=queue_templates,
            escalation_rules=metadata.get("escalation_rules"),
            data_cleanup_policy=data_cleanup_policy,
            metadata_={
                "compatibility_signature": version_signature,
                "provider_scope_observed": provider,
                "account_scope_observed": account_scope,
            },
        )
        if last_version is not None:
            last_version.status = "superseded"
            last_version.is_active = False
            last_version.superseded_at = datetime.now(UTC)
        db.add(version)
        db.flush()
        if last_version is not None:
            last_version.superseded_by_version_id = version.id
        policy.active_version_id = version.id
    else:
        version = last_version
        policy.active_version_id = version.id
    db.flush()
    return policy, version


def ensure_session_for_outcome(
    db: Session,
    *,
    conversation: InboxConversation,
    outcome: AiIntakeOutcome,
    provider: str,
    account_scope: str,
    created_conversation: bool,
) -> AiSessionContext | None:
    if not created_conversation or outcome.config_id is None:
        return None
    if outcome.status in {AiIntakeStatus.skipped, AiIntakeStatus.failed}:
        return None
    if not is_supported_channel(conversation.channel_type):
        return None
    if has_human_takeover(db, conversation):
        return None
    policy, version = ensure_policy_version_from_legacy_config(
        db,
        config_id=outcome.config_id,
        provider=provider,
        account_scope=account_scope,
    )
    session = active_session_for_conversation(db, conversation.id)
    now = datetime.now(UTC)
    if session is None:
        state = (
            "awaiting_customer"
            if outcome.status is AiIntakeStatus.awaiting_follow_up
            else "classified"
            if outcome.status is AiIntakeStatus.classified
            else "fallback_escalated"
            if outcome.status in {AiIntakeStatus.fallback, AiIntakeStatus.escalated}
            else "collecting_intent"
        )
        policy_metadata = dict(policy.metadata_ or {})
        session = AiIntakeSession(
            conversation_id=conversation.id,
            policy_id=policy.id,
            policy_version_id=version.id,
            legacy_config_id=outcome.config_id,
            state=state,
            channel_type=conversation.channel_type,
            provider=provider,
            account_scope=account_scope,
            display_name=version.display_name,
            turn_count=outcome.follow_up_count,
            max_turns=max(1, int(policy_metadata.get("max_turns") or 2)),
            confidence_threshold=outcome.classification.confidence
            if outcome.classification
            else float(policy_metadata.get("confidence_threshold") or 0),
            fallback_team_id=outcome.fallback_team_id or policy.fallback_team_id,
            expires_at=now
            + timedelta(
                minutes=int(
                    (policy.metadata_ or {}).get("max_intake_duration_minutes") or 10
                )
            ),
            metadata_={"ai_handling": True, "created_from": "team_inbox_receive"},
        )
        db.add(session)
    session.policy_id = policy.id
    session.policy_version_id = version.id
    session.legacy_config_id = outcome.config_id
    session.display_name = version.display_name
    session.turn_count = max(session.turn_count, outcome.follow_up_count)
    if outcome.classification is not None:
        session.final_intent = outcome.classification.intent.value
        session.final_category = outcome.classification.category.value
        session.final_confidence = outcome.classification.confidence
    db.flush()
    return AiSessionContext(session=session, policy=policy, version=version)


def ai_message_metadata(
    *,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    purpose: str,
    turn_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    provider_request_id: str | None = None,
) -> dict[str, object | None]:
    return {
        "sender_type": "ai",
        "author_type": "ai",
        "automation_kind": "ai_intake",
        "ai_display_name": session.display_name or DEFAULT_DISPLAY_NAME,
        "author_name": session.display_name or DEFAULT_DISPLAY_NAME,
        "ai_intake_session_id": str(session.id),
        "ai_intake_policy_id": str(session.policy_id) if session.policy_id else None,
        "ai_intake_policy_version_id": (
            str(session.policy_version_id) if session.policy_version_id else None
        ),
        "ai_intake_legacy_config_id": (
            str(session.legacy_config_id) if session.legacy_config_id else None
        ),
        "ai_message_purpose": purpose,
        "ai_turn_id": str(turn_id) if turn_id else None,
        "ai_provider": provider,
        "ai_model": model,
        "ai_provider_request_id": provider_request_id,
        "ai_generated_at": datetime.now(UTC).isoformat(),
        "ai_human_takeover_status": session.state,
        "ai_policy_version_number": version.version_number if version else None,
    }


def record_generation_attempt(
    db: Session,
    *,
    session: AiIntakeSession,
    purpose: str,
    status: str,
    inbound_message_id: UUID | None = None,
    outbound_message_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> AiIntakeGenerationAttempt:
    turn_number = max(1, session.turn_count or 1)
    dedupe = (
        f"ai-intake-generation:{session.id}:"
        f"{inbound_message_id or 'system'}:{purpose}:{turn_number}"
    )
    existing = (
        db.query(AiIntakeGenerationAttempt)
        .filter(AiIntakeGenerationAttempt.idempotency_key == dedupe)
        .one_or_none()
    )
    if existing is not None:
        if outbound_message_id and existing.outbound_message_id is None:
            existing.outbound_message_id = outbound_message_id
        return existing
    attempt = AiIntakeGenerationAttempt(
        session_id=session.id,
        inbound_message_id=inbound_message_id,
        outbound_message_id=outbound_message_id,
        turn_number=turn_number,
        message_purpose=purpose,
        provider=provider,
        model=model,
        status=status,
        duration_ms=duration_ms,
        error_code=error_code,
        idempotency_key=dedupe,
        generated_at=datetime.now(UTC) if status == "queued" else None,
    )
    db.add(attempt)
    db.flush()
    return attempt


def mark_handoff_requested(
    session: AiIntakeSession,
    *,
    destination_team_id: str | UUID | None,
) -> None:
    session.state = "handoff_requested"
    session.handoff_requested_at = datetime.now(UTC)
    metadata = dict(session.metadata_ or {})
    metadata["destination_team_id"] = (
        str(destination_team_id) if destination_team_id else None
    )
    session.metadata_ = metadata


def complete_session(session: AiIntakeSession, *, state: str = "completed") -> None:
    if state not in TERMINAL_SESSION_STATES:
        raise ValueError("AI intake terminal state is invalid")
    session.state = state
    now = datetime.now(UTC)
    session.completed_at = now
    if state == "stopped_human_takeover":
        session.takeover_at = now
    metadata = dict(session.metadata_ or {})
    metadata["ai_handling"] = False
    session.metadata_ = metadata


def transition_conversation_status(
    db: Session,
    *,
    conversation: InboxConversation,
    status: InboxConversationStatus,
    reason: team_inbox_status.InboxStatusReason,
    source_id: str,
    occurred_at: datetime | None = None,
) -> None:
    """Request a Team Inbox-owned status transition for AI intake consequences."""

    team_inbox_status.apply_status_transition(
        db,
        conversation=conversation,
        status=status,
        actor_person_id=None,
        reason=reason,
        source_id=source_id,
        occurred_at=occurred_at,
        compatibility_source=reason.value,
    )


def mark_conversation_ai_metadata(
    conversation: InboxConversation,
    *,
    session: AiIntakeSession | None,
    active: bool,
) -> None:
    metadata = dict(conversation.metadata_ or {})
    metadata["ai_handling"] = bool(active)
    if session is not None:
        metadata["ai_intake_session_id"] = str(session.id)
        metadata["ai_intake_state"] = session.state
        metadata["ai_intake_display_name"] = session.display_name
    conversation.metadata_ = metadata


def process_ready_sessions(
    db: Session, command: AiSessionProcessCommand
) -> AiSessionProcessResult:
    """Classify pending sessions and request Team Inbox consequences."""

    def _operation() -> AiSessionProcessResult:
        processed = 0
        skipped = 0
        failed = 0
        rows = (
            db.query(AiIntakeSession)
            .filter(AiIntakeSession.completed_at.is_(None))
            .filter(
                AiIntakeSession.state.in_(
                    (
                        "collecting_intent",
                        "awaiting_customer",
                        "eligible",
                        "welcome_pending",
                        "handoff_requested",
                    )
                )
            )
            .order_by(AiIntakeSession.created_at.asc())
            .limit(command.limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for session in rows:
            conversation = db.get(InboxConversation, session.conversation_id)
            if conversation is None or has_human_takeover(db, conversation):
                complete_session(session, state="stopped_human_takeover")
                if conversation is not None:
                    mark_conversation_ai_metadata(
                        conversation, session=session, active=False
                    )
                skipped += 1
                continue
            try:
                if _process_one_session(db, session=session, conversation=conversation):
                    processed += 1
                else:
                    skipped += 1
            except Exception:
                session.state = "failed"
                session.completed_at = datetime.now(UTC)
                failed += 1
        return AiSessionProcessResult(
            processed=processed, skipped=skipped, failed=failed
        )

    return execute_owner_command(
        db,
        definition=_AI_SESSION_COMMAND,
        context=command.context,
        operation=_operation,
    )


def _process_one_session(
    db: Session, *, session: AiIntakeSession, conversation: InboxConversation
) -> bool:
    from app.schemas.ai_intake import AiIntakeContextMessage, AiIntakeRequest
    from app.services import (
        ai_intake,
        team_inbox_assignment,
        team_inbox_outbound,
        team_inbox_routing,
    )

    inbound = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    if inbound is None or not inbound.body:
        return False
    version = (
        db.get(AiIntakePolicyVersion, session.policy_version_id)
        if session.policy_version_id
        else None
    )
    if session.state == "welcome_pending":
        welcome_body = (
            version.welcome_message
            if version is not None and version.welcome_message
            else DEFAULT_WELCOME_MESSAGE
        )
        welcome_metadata = ai_message_metadata(
            session=session,
            version=version,
            purpose="welcome",
        )
        delivery = team_inbox_outbound.send_ai_intake_message(
            db,
            conversation=conversation,
            body_text=welcome_body,
            metadata=welcome_metadata,
            dedupe_key=f"ai-intake-welcome:{session.id}",
        )
        record_generation_attempt(
            db,
            session=session,
            purpose="welcome",
            status="queued" if delivery.kind == "queued" else "failed",
            inbound_message_id=inbound.id,
            outbound_message_id=UUID(delivery.message_id)
            if delivery.message_id
            else None,
            error_code=delivery.reason,
        )
        if delivery.kind == "queued":
            session.state = "collecting_intent"
            mark_conversation_ai_metadata(conversation, session=session, active=True)
            return True
        session.state = "failed"
        complete_session(session, state="failed")
        mark_conversation_ai_metadata(conversation, session=session, active=False)
        return True
    cleanup_only = session.state == "handoff_requested"
    cleanup_open = _process_data_cleanup_turn(
        db,
        session=session,
        version=version,
        conversation=conversation,
        inbound=inbound,
    )
    if cleanup_only:
        if not cleanup_open:
            complete_session(session)
            mark_conversation_ai_metadata(conversation, session=session, active=False)
        return True
    metadata = dict(inbound.metadata_ or {})
    processed_key = f"processed_inbound:{inbound.id}"
    if dict(session.metadata_ or {}).get(processed_key):
        return False
    recent_rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .order_by(InboxMessage.created_at.desc())
        .limit(ai_intake.MAX_RECENT_MESSAGES)
        .all()
    )
    recent = tuple(
        AiIntakeContextMessage(
            direction=(
                "inbound"
                if row.direction == InboxMessageDirection.inbound.value
                else "outbound"
            ),
            body=str(row.body or "")[: ai_intake.MAX_CONTEXT_CHARS],
        )
        for row in reversed(recent_rows)
        if str(row.body or "").strip()
    )
    request = AiIntakeRequest(
        channel_type=conversation.channel_type,
        provider=session.provider,
        account_scope=session.account_scope,
        inbound_message_id=str(inbound.external_message_id or inbound.id)[:255],
        body=str(inbound.body or "")[:4000],
        conversation_id=conversation.id,
        recent_messages=recent,
        campaign_attributed=False,
        routing_allows_ai=True,
        created_conversation=True,
        has_active_assignment=False,
        awaiting_follow_up=session.state == "awaiting_customer",
        follow_up_count=session.turn_count,
    )
    outcome = ai_intake.classify_message(db, request)
    metadata.update(ai_intake.route_metadata(outcome))
    inbound.metadata_ = metadata
    conversation_metadata = dict(conversation.metadata_ or {})
    conversation_metadata["ai_intake"] = ai_intake.conversation_state(request, outcome)
    conversation.metadata_ = conversation_metadata
    session.turn_count = outcome.follow_up_count
    if outcome.classification is not None:
        session.final_intent = outcome.classification.intent.value
        session.final_category = outcome.classification.category.value
        session.final_confidence = outcome.classification.confidence
    generation = record_generation_attempt(
        db,
        session=session,
        purpose="classification",
        status=outcome.status.value,
        inbound_message_id=inbound.id,
        provider=outcome.provider,
        model=outcome.model,
        duration_ms=outcome.duration_ms,
        error_code=outcome.reason.value,
    )
    session_metadata = dict(session.metadata_ or {})
    session_metadata[processed_key] = True
    session_metadata["last_generation_attempt_id"] = str(generation.id)
    session.metadata_ = session_metadata
    if has_human_takeover(db, conversation):
        complete_session(session, state="stopped_human_takeover")
        mark_conversation_ai_metadata(conversation, session=session, active=False)
        return True
    if (
        outcome.status is AiIntakeStatus.awaiting_follow_up
        and outcome.config_id is not None
        and outcome.classification is not None
        and outcome.classification.follow_up_question is not None
    ):
        delivery = team_inbox_outbound.send_ai_intake_follow_up(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.AiIntakeFollowUpPayload(
                question=outcome.classification.follow_up_question,
                inbound_message_id=inbound.id,
                config_id=outcome.config_id,
                follow_up_count=outcome.follow_up_count,
                session_id=session.id,
                policy_id=session.policy_id,
                policy_version_id=session.policy_version_id,
                display_name=session.display_name,
            ),
        )
        generation.outbound_message_id = (
            UUID(delivery.message_id) if delivery.message_id else None
        )
        conversation_metadata = dict(conversation.metadata_ or {})
        intake_metadata = dict(conversation_metadata.get("ai_intake") or {})
        intake_metadata["follow_up_delivery_status"] = delivery.kind
        intake_metadata["follow_up_delivery_reason"] = delivery.reason
        conversation_metadata["ai_intake"] = intake_metadata
        conversation.metadata_ = conversation_metadata
        session.state = "awaiting_customer"
        transition_conversation_status(
            db,
            conversation=conversation,
            status=InboxConversationStatus.pending,
            reason=team_inbox_status.InboxStatusReason.ai_awaiting_clarification,
            source_id=f"ai-intake-awaiting-clarification:{session.id}:{inbound.id}",
        )
        mark_conversation_ai_metadata(conversation, session=session, active=True)
        return True
    routing = team_inbox_routing.resolve_channel_routing_decision(
        db,
        channel_type=conversation.channel_type,
        provider=session.provider,
        account_scope=session.account_scope,
        fallback_service_team_id=session.fallback_team_id,
        metadata=metadata,
    )
    inbound_metadata = dict(inbound.metadata_ or {})
    inbound_metadata["routing"] = {
        "primary_service_team_id": routing.primary_service_team_id,
        "channel_service_team_id": routing.channel_service_team_id,
        "ai_service_team_id": routing.ai_service_team_id,
        "channel_route_id": routing.channel_route_id,
        "ai_route_id": routing.ai_route_id,
        "ai_routing_allowed": routing.ai_routing_allowed,
        "ai_intent_key": routing.ai_intent_key,
        "ai_confidence": routing.ai_confidence,
        "reason": routing.reason,
    }
    inbound.metadata_ = inbound_metadata
    conversation_metadata = dict(conversation.metadata_ or {})
    intake_metadata = dict(conversation_metadata.get("ai_intake") or {})
    intake_metadata["destination_team_id"] = routing.primary_service_team_id
    intake_metadata["routing_reason"] = routing.reason
    conversation_metadata["ai_intake"] = intake_metadata
    conversation.metadata_ = conversation_metadata
    if routing.primary_service_team_id:
        team_inbox_routing.apply_email_routing_plan(
            db,
            conversation=conversation,
            plan=team_inbox_routing.EmailTeamRoutingPlan(
                primary_service_team_id=routing.primary_service_team_id,
                participant_service_team_ids=[routing.primary_service_team_id],
                matches=[],
                unmatched_recipients=[],
            ),
        )
        mark_handoff_requested(
            session, destination_team_id=routing.primary_service_team_id
        )
        transition_conversation_status(
            db,
            conversation=conversation,
            status=InboxConversationStatus.open,
            reason=team_inbox_status.InboxStatusReason.ai_handoff_accepted,
            source_id=f"ai-intake-handoff:{session.id}",
        )
        assignment = team_inbox_assignment.assign_conversation_to_available_agent(
            db,
            conversation=conversation,
            service_team_id=routing.primary_service_team_id,
            reason="AI intake handoff",
            source="routing_rule",
        )
        if assignment.kind == "assigned" or not cleanup_open:
            complete_session(session)
            mark_conversation_ai_metadata(conversation, session=session, active=False)
        else:
            mark_conversation_ai_metadata(conversation, session=session, active=True)
    else:
        session.state = "fallback_escalated"
        complete_session(session, state="fallback_escalated")
        mark_conversation_ai_metadata(conversation, session=session, active=False)
    return True


def _data_cleanup_policy(version: AiIntakePolicyVersion | None) -> dict[str, object]:
    raw = dict(version.data_cleanup_policy or {}) if version is not None else {}
    templates = raw.get("templates")
    if not isinstance(templates, dict):
        templates = {}
    return {
        **raw,
        "templates": {
            **DEFAULT_DATA_CLEANUP_TEMPLATES,
            **{
                str(key): str(value)
                for key, value in templates.items()
                if key in DEFAULT_DATA_CLEANUP_TEMPLATES and str(value).strip()
            },
        },
        "gender_choices": raw.get("gender_choices") or DEFAULT_GENDER_CHOICES,
        "dob_formats": raw.get("dob_formats") or ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"),
        "max_attempts": min(max(int(raw.get("max_attempts") or 2), 1), 2),
        "production_collection_enabled": bool(raw.get("production_collection_enabled")),
    }


def _cleanup_gender_choices(policy: Mapping[str, object]) -> Mapping[str, str]:
    value = policy.get("gender_choices")
    if not isinstance(value, Mapping):
        return DEFAULT_GENDER_CHOICES
    return {str(key): str(mapped) for key, mapped in value.items()}


def _cleanup_dob_formats(policy: Mapping[str, object]) -> tuple[str, ...]:
    value = policy.get("dob_formats")
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")


def _cleanup_max_attempts(policy: Mapping[str, object]) -> int:
    value = policy.get("max_attempts")
    try:
        raw = int(value) if isinstance(value, (str, int, float)) else 2
        return min(max(raw, 1), 2)
    except (TypeError, ValueError):
        return 2


def _cleanup_attempt_count(state: Mapping[str, object]) -> int:
    value = state.get("attempts")
    try:
        return int(value) if isinstance(value, (str, int, float)) else 0
    except (TypeError, ValueError):
        return 0


def _format_cleanup_fields(fields: tuple[str, ...]) -> str:
    labels = {
        "gender": "gender",
        "date_of_birth": "date of birth",
    }
    return " and ".join(labels.get(field, field) for field in fields)


def _cleanup_state(session: AiIntakeSession) -> dict[str, object]:
    metadata = dict(session.metadata_ or {})
    state = metadata.get("data_cleanup")
    return dict(state) if isinstance(state, dict) else {}


def _store_cleanup_state(session: AiIntakeSession, state: Mapping[str, object]) -> None:
    metadata = dict(session.metadata_ or {})
    metadata["data_cleanup"] = dict(state)
    session.metadata_ = metadata


def _send_cleanup_message(
    db: Session,
    *,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    conversation: InboxConversation,
    body: str,
    purpose: str,
    dedupe_suffix: str,
) -> None:
    from app.services import team_inbox_outbound

    team_inbox_outbound.send_ai_intake_message(
        db,
        conversation=conversation,
        body_text=body,
        metadata=ai_message_metadata(
            session=session,
            version=version,
            purpose=purpose,
        ),
        dedupe_key=f"ai-intake-cleanup:{session.id}:{dedupe_suffix}",
    )


def _parse_cleanup_response(
    body: str,
    *,
    missing_fields: tuple[str, ...],
    policy: Mapping[str, object],
) -> tuple[str, str | None, date | None]:
    text = body.strip()
    normalized = text.lower()
    if re.search(r"\b(no|decline|refuse|prefer not|not disclose|skip)\b", normalized):
        return "refused", None, None
    gender: str | None = None
    choices = _cleanup_gender_choices(policy)
    for public_value in choices:
        token = str(public_value).strip().lower().replace("_", " ")
        if token and re.search(rf"\b{re.escape(token)}\b", normalized):
            gender = str(public_value)
            break
    dob: date | None = None
    if "date_of_birth" in missing_fields:
        candidates = re.findall(
            r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}/\d{2}/\d{4}\b|\b\d{2}-\d{2}-\d{4}\b", text
        )
        formats = _cleanup_dob_formats(policy)
        for candidate in candidates:
            for fmt in formats:
                try:
                    parsed = datetime.strptime(candidate, fmt).date()
                except ValueError:
                    continue
                if parsed < date.today():
                    dob = parsed
                    break
            if dob is not None:
                break
    has_gender = "gender" not in missing_fields or gender is not None
    has_dob = "date_of_birth" not in missing_fields or dob is not None
    if has_gender and has_dob:
        return "valid", gender, dob
    return "invalid", gender, dob


def _process_data_cleanup_turn(
    db: Session,
    *,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    conversation: InboxConversation,
    inbound: InboxMessage,
) -> bool:
    from app.models.subscriber import Subscriber
    from app.services import subscriber_profile_cleanup

    policy = _data_cleanup_policy(version)
    if not policy.get("production_collection_enabled"):
        return False
    if conversation.subscriber_id is None:
        return False
    subscriber = db.get(Subscriber, conversation.subscriber_id)
    if (
        subscriber is None
        or not subscriber_profile_cleanup.is_direct_residential_customer(subscriber)
    ):
        return False
    missing = subscriber_profile_cleanup.missing_cleanup_fields(subscriber)
    if not missing:
        return False
    state = _cleanup_state(session)
    processed = state.get("processed_inbound_message_ids")
    processed_ids = list(processed) if isinstance(processed, list) else []
    if str(inbound.id) in processed_ids:
        return state.get("status") == "awaiting_response"
    attempts = _cleanup_attempt_count(state)
    templates = policy["templates"]
    assert isinstance(templates, Mapping)
    field_text = _format_cleanup_fields(missing)
    if state.get("status") != "awaiting_response":
        attempts += 1
        body = str(templates["ask"]).format(fields=field_text)
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=body,
            purpose="data_cleanup",
            dedupe_suffix=f"ask:{attempts}",
        )
        _store_cleanup_state(
            session,
            {
                "status": "awaiting_response",
                "attempts": attempts,
                "missing_fields": list(missing),
                "processed_inbound_message_ids": processed_ids,
            },
        )
        return True
    outcome, gender, dob = _parse_cleanup_response(
        str(inbound.body or ""),
        missing_fields=missing,
        policy=policy,
    )
    processed_ids.append(str(inbound.id))
    if outcome == "valid":
        result = subscriber_profile_cleanup.submit_profile_cleanup(
            db,
            subscriber_profile_cleanup.SubmitProfileCleanupCommand(
                context=CommandContext.system(
                    actor="ai:dotmac-virtual-assistant",
                    scope="customer:profile-cleanup",
                    reason="save AI-collected NCC profile cleanup candidates",
                ),
                subscriber_id=subscriber.id,
                source_conversation_id=conversation.id,
                candidate_gender=gender,
                candidate_date_of_birth=dob,
                gender_mapping=_cleanup_gender_choices(policy),
                consent_text="customer supplied profile cleanup candidate",
                attempt_count=attempts,
                activation_enabled=bool(policy.get("production_collection_enabled")),
            ),
        )
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=str(templates["saved"]),
            purpose="data_cleanup",
            dedupe_suffix="saved",
        )
        _store_cleanup_state(
            session,
            {
                "status": result.outcome.value,
                "attempts": attempts,
                "missing_fields": list(missing),
                "saved_fields": list(result.saved_fields),
                "processed_inbound_message_ids": processed_ids[-10:],
            },
        )
        return False
    if outcome == "refused":
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=str(templates["refused"]),
            purpose="data_cleanup",
            dedupe_suffix="refused",
        )
        _store_cleanup_state(
            session,
            {
                "status": "refused",
                "attempts": attempts,
                "missing_fields": list(missing),
                "processed_inbound_message_ids": processed_ids[-10:],
            },
        )
        return False
    if attempts >= _cleanup_max_attempts(policy):
        _send_cleanup_message(
            db,
            session=session,
            version=version,
            conversation=conversation,
            body=str(templates["follow_up"]),
            purpose="data_cleanup",
            dedupe_suffix="human-follow-up",
        )
        _store_cleanup_state(
            session,
            {
                "status": "human_follow_up_required",
                "attempts": attempts,
                "missing_fields": list(missing),
                "processed_inbound_message_ids": processed_ids[-10:],
            },
        )
        return False
    attempts += 1
    _send_cleanup_message(
        db,
        session=session,
        version=version,
        conversation=conversation,
        body=str(templates["invalid_retry"]).format(fields=field_text),
        purpose="data_cleanup",
        dedupe_suffix=f"invalid:{attempts}",
    )
    _store_cleanup_state(
        session,
        {
            "status": "awaiting_response",
            "attempts": attempts,
            "missing_fields": list(missing),
            "processed_inbound_message_ids": processed_ids[-10:],
        },
    )
    return True
