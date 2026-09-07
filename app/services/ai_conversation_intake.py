"""Durable conversational intake session helpers.

The AI intake owner records AI lifecycle, policy/version provenance and
generation evidence. Team Inbox remains the owner for conversation status,
routing, queueing, assignment and outbound delivery.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_
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
from app.schemas.ai_intake import (
    DEFAULT_CLARIFICATION_QUESTIONS,
    AiCustomerResponseCompositionRequest,
    AiIntakeCategory,
    AiIntakeClassification,
    AiIntakeContextMessage,
    AiIntakeExtractedFacts,
    AiIntakeIntent,
    AiIntakeMessageRole,
    AiIntakeMonitoringContext,
    AiIntakeNextAction,
    AiIntakeOntContext,
    AiIntakeOutcome,
    AiIntakePlaybookStepContext,
    AiIntakeRadiusContext,
    AiIntakeSafeCustomerIdentity,
    AiIntakeStatus,
    normalize_clarification_questions,
)
from app.schemas.ai_operations import (
    AiIntakeConfigMetadata,
    AiIntakeConfigUpsert,
    AiIntakeDepartmentMapping,
)
from app.services import (
    ai_intake,
    ai_intake_conversation_engine,
    ai_intake_graph,
    team_inbox_operations,
    team_inbox_routing,
    team_inbox_status,
)
from app.services.ai_intake_text import human_impersonation_violations
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
from app.version import get_app_revision

SUPPORTED_CONVERSATIONAL_CHANNELS = frozenset(
    {
        InboxChannelType.whatsapp.value,
        InboxChannelType.facebook_messenger.value,
        InboxChannelType.instagram_dm.value,
        InboxChannelType.chat_widget.value,
    }
)
_NATIVE_WIDGET_AI_SCOPES = frozenset({"customer", "reseller_portal"})
logger = logging.getLogger(__name__)
SUPPORTED_CONVERSATION_ENGINE_MODES = frozenset(
    {
        ai_intake_graph.CUSTOM_ENGINE_MODE,
        ai_intake_graph.LANGGRAPH_ENGINE_MODE,
    }
)
TERMINAL_SESSION_STATES = frozenset(
    {
        "completed",
        "stopped_human_takeover",
        "fallback_escalated",
        "resolved",
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
DEFAULT_QUEUE_POSITION_UPDATE_MINUTES = 10
DEFAULT_QUEUE_HEARTBEAT_MINUTES = 30
DEFAULT_CUSTOMER_RESPONSE_TIMEOUT_MINUTES = 5
DEFAULT_CUSTOMER_WAIT_EXPIRY_HOURS = 72
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
APPROVED_QUEUE_TEMPLATE_VARIABLES = frozenset(
    {"position", "queue_position", "team_name"}
)
SUPPORTED_AI_INTENT_KEYS = frozenset(item.value for item in AiIntakeIntent)
SUPPORTED_AI_CATEGORY_KEYS = frozenset(item.value for item in AiIntakeCategory)
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
class AiMediaFirstHandoffPolicy:
    policy_id: UUID
    policy_version_id: UUID
    display_name: str
    customer_message: str
    fallback_team_id: UUID | None


def resolve_media_first_handoff_policy(
    db: Session,
    *,
    channel_type: str,
    provider: str,
    account_scope: str,
) -> AiMediaFirstHandoffPolicy | None:
    policy = (
        db.query(AiIntakePolicy)
        .filter(AiIntakePolicy.channel_type == channel_type)
        .filter(AiIntakePolicy.provider == provider)
        .filter(AiIntakePolicy.account_scope == account_scope)
        .filter(AiIntakePolicy.is_enabled.is_(True))
        .one_or_none()
    )
    if policy is None or policy.active_version_id is None:
        return None
    version = db.get(AiIntakePolicyVersion, policy.active_version_id)
    if version is None or not version.is_active or version.status != "activated":
        return None
    metadata = dict(version.metadata_ or {})
    templates = _mapping_dict(metadata.get("conversation_templates"))
    message = str(templates.get("media_first_handoff") or "").strip()
    if not message:
        message = (
            "Thanks for sending that. I cannot review attachments here, so I "
            "am passing this to our support team to take a closer look."
        )
    return AiMediaFirstHandoffPolicy(
        policy_id=policy.id,
        policy_version_id=version.id,
        display_name=version.display_name or DEFAULT_DISPLAY_NAME,
        customer_message=message[:800],
        fallback_team_id=policy.fallback_team_id,
    )


@dataclass(slots=True)
class _PreviewConversation:
    id: UUID
    channel_type: str
    status: str
    metadata_: dict[str, object]
    subscriber_id: UUID | None = None
    contact_address: str | None = None
    external_thread_id: str | None = None


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
    clarification_questions: tuple[str, str] | None = None
    intent_team_mappings: tuple[Mapping[str, object], ...] = ()
    queue_templates: Mapping[str, object] | None = None
    escalation_rules: Mapping[str, object] | None = None
    data_cleanup_policy: Mapping[str, object] | None = None
    conversational_engine_enabled: bool | None = None
    conversation_engine_mode: str | None = None
    permitted_identifiers: tuple[str, ...] = ()
    tool_config: Mapping[str, object] | None = None
    conversation_policy: Mapping[str, object] | None = None
    conversation_templates: Mapping[str, object] | None = None
    channel_overrides: Mapping[str, object] | None = None
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
    clarification_questions: tuple[str, str] | None = None
    intent_team_mappings: tuple[Mapping[str, object], ...] = ()
    queue_templates: Mapping[str, object] | None = None
    escalation_rules: Mapping[str, object] | None = None
    data_cleanup_policy: Mapping[str, object] | None = None
    conversational_engine_enabled: bool | None = None
    conversation_engine_mode: str | None = None
    permitted_identifiers: tuple[str, ...] = ()
    tool_config: Mapping[str, object] | None = None
    conversation_policy: Mapping[str, object] | None = None
    conversation_templates: Mapping[str, object] | None = None
    channel_overrides: Mapping[str, object] | None = None
    replace_existing_draft: bool = False


@dataclass(frozen=True, slots=True)
class AiPolicyVersionActivateCommand:
    context: CommandContext
    version_id: UUID
    actor_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AiPolicyDisableCommand:
    context: CommandContext
    policy_id: UUID | None = None
    channel_type: str | None = None
    provider: str | None = None
    account_scope: str | None = None


@dataclass(frozen=True, slots=True)
class AiPolicyVersionValidationOutcome:
    policy_id: UUID
    version_id: UUID
    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AiPolicyPreviewCommand:
    context: CommandContext
    version_id: UUID
    customer_message: str
    channel_type: str | None = None
    preview_mode: str = "simulation"


@dataclass(frozen=True, slots=True)
class AiPolicyPreviewResult:
    version_id: UUID
    interpreted_intent: str | None
    category: str | None
    confidence: float | None
    current_state: dict[str, object]
    next_action: str
    response_text: str | None
    handoff_decision: bool
    destination_team_id: str | None
    destination_team_name: str | None
    tool_executions: tuple[dict[str, object], ...]
    preview_mode: str


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


@dataclass(frozen=True, slots=True)
class AiPolicyDisableOutcome:
    policy_id: UUID
    active_version_id: UUID | None
    policy_enabled: bool
    legacy_config_id: UUID | None
    legacy_config_enabled: bool | None


@dataclass(frozen=True, slots=True)
class AiPolicyVersionHistoryRow:
    """Bounded, non-editable evidence for the policy administration screen."""

    version_id: UUID
    version_number: int
    status: str
    is_active: bool
    created_at: datetime
    activated_at: datetime | None
    superseded_at: datetime | None


def is_supported_channel(channel_type: str | None) -> bool:
    return str(channel_type or "").strip() in SUPPORTED_CONVERSATIONAL_CHANNELS


def _normalize_text(value: str | None, *, field: str, limit: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"AI intake {field} is required")
    return text[:limit]


def _normalize_conversation_engine_mode(value: str | None) -> str:
    mode = str(value or ai_intake_graph.CUSTOM_ENGINE_MODE).strip()
    if mode not in SUPPORTED_CONVERSATION_ENGINE_MODES:
        raise ValueError("AI intake conversation engine mode is unsupported")
    return mode


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
        raise ValueError(
            "AI intake supports only WhatsApp, Messenger, Instagram DM, and chat widget"
        )
    if account_scope.lower() in {"any", "global", "default"}:
        raise ValueError("AI intake draft policy requires an explicit account scope")
    if channel_type == InboxChannelType.whatsapp.value:
        _validate_whatsapp_provider_scope(
            db, provider=provider, account_scope=account_scope
        )
        return
    if channel_type == InboxChannelType.chat_widget.value:
        fiber_scope = provider == "fiber_website" and account_scope == "fiber.dotmac.ng"
        native_scope = (
            provider == "native_widget" and account_scope in _NATIVE_WIDGET_AI_SCOPES
        )
        if not fiber_scope and not native_scope:
            raise ValueError(
                "Chat widget AI intake requires a registered Fiber or portal scope"
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
                conversational_engine_enabled=command.conversational_engine_enabled,
                conversation_engine_mode=command.conversation_engine_mode,
                permitted_identifiers=command.permitted_identifiers,
                tool_config=command.tool_config,
                conversation_policy=command.conversation_policy,
                conversation_templates=command.conversation_templates,
                channel_overrides=command.channel_overrides,
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


def _mapping_dict(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _copy_version_payload(
    base: AiIntakePolicyVersion | None,
    command: AiPolicyVersionDraftCommand,
) -> dict[str, object | None]:
    policy_text: dict[str, object] = {
        "display_name": command.display_name,
        "welcome_message": command.welcome_message,
        "business_tone": command.business_tone,
        "business_instructions": command.business_instructions,
        "approved_isp_information": command.approved_isp_information,
    }
    if command.queue_templates is not None:
        policy_text.update(
            {
                f"queue_templates.{key}": value
                for key, value in command.queue_templates.items()
            }
        )
    if command.conversation_policy is not None:
        policy_text.update(
            {
                f"conversation_policy.{key}": value
                for key, value in command.conversation_policy.items()
            }
        )
    if command.conversation_templates is not None:
        policy_text.update(
            {
                f"conversation_templates.{key}": value
                for key, value in command.conversation_templates.items()
            }
        )
    if violations := human_impersonation_violations(policy_text):
        raise ValueError(
            "AI intake policy text cannot impersonate a human employee: "
            + ", ".join(violations)
        )
    clarification_source: object = command.clarification_questions
    if clarification_source is None and base is not None:
        clarification_source = base.clarification_questions
    clarification_questions = normalize_clarification_questions(clarification_source)
    metadata = dict(base.metadata_ or {}) if base is not None else {}
    if command.conversational_engine_enabled is not None:
        metadata["conversational_engine_enabled"] = bool(
            command.conversational_engine_enabled
        )
    if command.conversation_engine_mode is not None:
        metadata["conversation_engine_mode"] = _normalize_conversation_engine_mode(
            command.conversation_engine_mode
        )
    if command.permitted_identifiers:
        metadata["permitted_identifiers"] = list(command.permitted_identifiers)
    if command.tool_config is not None:
        metadata["tools"] = dict(command.tool_config)
    if command.conversation_policy is not None:
        metadata["conversation_policy"] = {
            **_mapping_dict(metadata.get("conversation_policy")),
            **dict(command.conversation_policy),
        }
    if command.conversation_templates is not None:
        metadata["conversation_templates"] = {
            **_mapping_dict(metadata.get("conversation_templates")),
            **dict(command.conversation_templates),
        }
    if command.channel_overrides is not None:
        metadata["channel_overrides"] = {
            **_mapping_dict(metadata.get("channel_overrides")),
            **dict(command.channel_overrides),
        }
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
        "clarification_questions": list(clarification_questions),
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
        "metadata_": metadata,
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
    effective_base = base or existing_draft
    if effective_base is None and policy.active_version_id is not None:
        effective_base = db.get(AiIntakePolicyVersion, policy.active_version_id)
    payload = _copy_version_payload(effective_base, command)
    version = existing_draft
    if version is None:
        payload_metadata = _mapping_dict(payload.pop("metadata_", {}))
        version = AiIntakePolicyVersion(
            policy_id=policy.id,
            version_number=_next_policy_version_number(db, policy.id),
            status="draft",
            is_active=False,
            created_by_person_id=None,
            metadata_={
                **payload_metadata,
                "created_reason": command.context.reason,
                "base_version_id": (
                    str(effective_base.id) if effective_base is not None else None
                ),
            },
            **payload,
        )
        db.add(version)
    else:
        for field, value in payload.items():
            if field == "metadata_":
                version.metadata_ = {
                    **_mapping_dict(value),
                    "updated_reason": command.context.reason,
                }
            else:
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


def _template_variables(template: object) -> set[str]:
    text = str(template or "")
    names = set(re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", text))
    names.update(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text))
    names.update(re.findall(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", text))
    return names


def _validate_queue_templates(queue_templates: object) -> None:
    if queue_templates is None:
        return
    if not isinstance(queue_templates, Mapping):
        raise ValueError("AI intake queue templates must be an object")
    for key in DEFAULT_QUEUE_TEMPLATES:
        unknown = _template_variables(queue_templates.get(key)) - (
            APPROVED_QUEUE_TEMPLATE_VARIABLES
        )
        if unknown:
            raise ValueError(
                "AI intake queue templates support only "
                "{{queue_position}}, {{position}} and {{team_name}}"
            )
    update_minutes = _bounded_int(
        queue_templates.get("position_update_minutes"),
        default=DEFAULT_QUEUE_POSITION_UPDATE_MINUTES,
        minimum=1,
        maximum=120,
    )
    heartbeat_minutes = _bounded_int(
        queue_templates.get("heartbeat_minutes"),
        default=DEFAULT_QUEUE_HEARTBEAT_MINUTES,
        minimum=5,
        maximum=240,
    )
    if update_minutes < 1 or heartbeat_minutes < 5:
        raise ValueError("AI intake queue notification minutes are invalid")


def _validate_engine_playbooks(
    policy: Mapping[str, object], tools: Mapping[str, object]
) -> None:
    for playbook_key in ai_intake_conversation_engine.PLAYBOOK_POLICY_KEYS:
        raw_playbooks = policy.get(playbook_key)
        if raw_playbooks is None:
            continue
        if not isinstance(raw_playbooks, list):
            raise ValueError("AI intake playbooks must be a list")
        for raw_playbook in raw_playbooks:
            if not isinstance(raw_playbook, Mapping):
                raise ValueError("AI intake playbook entries must be objects")
            intent = str(raw_playbook.get("intent") or "").strip()
            if intent and intent not in SUPPORTED_AI_INTENT_KEYS:
                raise ValueError("AI intake playbook intent is unsupported")
            category = str(raw_playbook.get("category") or "").strip()
            if category and category not in SUPPORTED_AI_CATEGORY_KEYS:
                raise ValueError("AI intake playbook category is unsupported")
            steps = raw_playbook.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ValueError("AI intake playbook steps must be a non-empty list")
            for raw_step in steps:
                if not isinstance(raw_step, Mapping):
                    raise ValueError("AI intake playbook steps must be objects")
                condition = raw_step.get("condition")
                if condition is not None:
                    if not isinstance(condition, Mapping):
                        raise ValueError(
                            "AI intake playbook condition must be an object"
                        )
                    condition_type = str(condition.get("type") or "").strip()
                    legacy_condition = any(
                        key in condition for key in ("fact", "intent", "category")
                    )
                    if (
                        condition_type
                        and condition_type
                        not in ai_intake_conversation_engine.SUPPORTED_RULE_CONDITIONS
                    ):
                        raise ValueError("AI intake playbook condition is unsupported")
                    if not condition_type and not legacy_condition:
                        raise ValueError("AI intake playbook condition is unsupported")
                action = str(raw_step.get("action") or "").strip()
                if not action:
                    raise ValueError("AI intake playbook action is required")
                if action not in ai_intake_conversation_engine.SUPPORTED_RULE_ACTIONS:
                    raise ValueError("AI intake playbook action is unsupported")
                tool_key = str(raw_step.get("tool") or "").strip()
                if action in {"execute_tool", "invoke_tool"} and not tool_key:
                    raise ValueError("AI intake playbook tool action requires a tool")
                if action == "request_field" and not (
                    raw_step.get("field") or raw_step.get("tool")
                ):
                    raise ValueError(
                        "AI intake playbook field request requires a field"
                    )
                if (
                    action in {"execute_tool", "invoke_tool"}
                    and tool_key
                    and tool_key not in ai_intake_conversation_engine.TOOL_CATALOG
                ):
                    raise ValueError("AI intake playbook references an unknown tool")
                tool_value = tools.get(tool_key)
                tool_enabled = (
                    bool(tool_value.get("enabled", False))
                    if isinstance(tool_value, Mapping)
                    else bool(tool_value)
                )
                if (
                    action in {"execute_tool", "invoke_tool"}
                    and tool_key
                    and not tool_enabled
                ):
                    raise ValueError("AI intake playbook references a disabled tool")


def _validate_engine_policy(version: AiIntakePolicyVersion) -> None:
    metadata = (
        dict(version.metadata_ or {}) if isinstance(version.metadata_, dict) else {}
    )
    if not metadata.get("conversational_engine_enabled"):
        return
    mode = _normalize_conversation_engine_mode(
        str(
            metadata.get("conversation_engine_mode")
            or ai_intake_graph.CUSTOM_ENGINE_MODE
        )
    )
    if mode == ai_intake_graph.LANGGRAPH_ENGINE_MODE and not (
        ai_intake_graph.langgraph_available()
    ):
        raise ValueError("AI intake LangGraph engine requires the langgraph package")
    raw_policy = metadata.get("conversation_policy")
    policy = dict(raw_policy) if isinstance(raw_policy, Mapping) else {}
    raw_tools = metadata.get("tools")
    tools = dict(raw_tools) if isinstance(raw_tools, Mapping) else {}
    permitted = metadata.get("permitted_identifiers") or []
    if not isinstance(permitted, (list, tuple)):
        raise ValueError("AI intake permitted identifiers must be a list")
    unsupported = set(permitted) - {
        "portal_id",
        "registered_email",
        "registered_phone",
    }
    if unsupported:
        raise ValueError("AI intake permitted identifiers include an unsupported value")
    if not permitted:
        raise ValueError("AI intake requires at least one permitted identifier")
    try:
        raw_max_turns = int(policy.get("max_turns") or 6)
    except (TypeError, ValueError):
        raw_max_turns = -1
    max_turns = _bounded_int(raw_max_turns, default=6, minimum=1, maximum=10)
    if max_turns != raw_max_turns:
        raise ValueError("AI intake max conversation turns must be between 1 and 10")
    for key in tools:
        if key not in ai_intake_conversation_engine.TOOL_CATALOG:
            raise ValueError("AI intake tool configuration contains an unknown tool")
    failure_handoffs = policy.get("tool_failure_handoff_statuses")
    if failure_handoffs is not None:
        if not isinstance(failure_handoffs, Mapping):
            raise ValueError("AI intake tool failure handoff policy must be an object")
        for tool_key, raw_statuses in failure_handoffs.items():
            if str(tool_key) not in ai_intake_conversation_engine.TOOL_CATALOG:
                raise ValueError(
                    "AI intake tool failure handoff policy references an unknown tool"
                )
            if not isinstance(raw_statuses, list | tuple) or any(
                str(status) not in {"unavailable", "unauthorized"}
                for status in raw_statuses
            ):
                raise ValueError(
                    "AI intake tool failure handoff statuses must be unavailable "
                    "or unauthorized"
                )
    _validate_engine_playbooks(policy, tools)
    rules = policy.get("troubleshooting_rules")
    if rules is not None:
        if not isinstance(rules, list):
            raise ValueError("AI intake troubleshooting rules must be a list")
        for raw in rules:
            if not isinstance(raw, Mapping):
                raise ValueError(
                    "AI intake troubleshooting rule entries must be objects"
                )
            condition = raw.get("condition")
            if not isinstance(condition, Mapping):
                raise ValueError("AI intake troubleshooting rule condition is required")
            condition_type = str(condition.get("type") or "").strip()
            legacy_condition = any(
                key in condition for key in ("fact", "intent", "category")
            )
            if (
                condition_type
                and condition_type
                not in ai_intake_conversation_engine.SUPPORTED_RULE_CONDITIONS
            ):
                raise ValueError(
                    "AI intake troubleshooting rule condition is unsupported"
                )
            if not condition_type and not legacy_condition:
                raise ValueError(
                    "AI intake troubleshooting rule condition is unsupported"
                )
            action = str(raw.get("action") or "").strip()
            if not action:
                raise ValueError("AI intake troubleshooting rule action is required")
            if (
                action
                and action not in ai_intake_conversation_engine.SUPPORTED_RULE_ACTIONS
            ):
                raise ValueError("AI intake troubleshooting rule action is unsupported")
            if ai_intake_conversation_engine._handoff_rule_matches_first_turn(
                action,
                condition,
            ):
                raise ValueError(
                    "AI intake handoff troubleshooting rule must not match the first turn"
                )
            tool_key = str(raw.get("tool") or "").strip()
            if action in {"execute_tool", "invoke_tool"} and not tool_key:
                raise ValueError(
                    "AI intake troubleshooting tool action requires a tool"
                )
            if action == "request_field" and not (raw.get("field") or raw.get("tool")):
                raise ValueError(
                    "AI intake troubleshooting field request requires a field"
                )
            if (
                action in {"execute_tool", "invoke_tool"}
                and tool_key
                and tool_key not in ai_intake_conversation_engine.TOOL_CATALOG
            ):
                raise ValueError(
                    "AI intake troubleshooting rule references an unknown tool"
                )
            tool_value = tools.get(tool_key)
            tool_enabled = (
                bool(tool_value.get("enabled", False))
                if isinstance(tool_value, Mapping)
                else bool(tool_value)
            )
            if (
                action in {"execute_tool", "invoke_tool"}
                and tool_key
                and not tool_enabled
            ):
                raise ValueError("AI intake troubleshooting references a disabled tool")
    handoff = policy.get("handoff")
    if handoff is not None and not isinstance(handoff, Mapping):
        raise ValueError("AI intake handoff policy must be an object")
    if isinstance(handoff, Mapping):
        unknown = _template_variables(handoff.get("summary_template")) - (
            ai_intake_conversation_engine.APPROVED_HANDOFF_SUMMARY_VARIABLES
        )
        if unknown:
            raise ValueError(
                "AI intake handoff summary template contains unsupported variables"
            )


def _validate_intent_definitions(version: AiIntakePolicyVersion) -> None:
    definitions = version.intent_definitions or []
    if not definitions:
        return
    if not isinstance(definitions, list):
        raise ValueError("AI intake intent definitions must be a list")
    seen: set[str] = set()
    for raw in definitions:
        if not isinstance(raw, Mapping):
            raise ValueError("AI intake intent definition entries must be objects")
        key = str(raw.get("key") or "").strip()
        if key not in SUPPORTED_AI_INTENT_KEYS:
            raise ValueError("AI intake intent definition key is unsupported")
        if key in seen:
            raise ValueError("AI intake intent definition keys must be unique")
        seen.add(key)
        category = str(raw.get("category") or "").strip()
        if category and category not in SUPPORTED_AI_CATEGORY_KEYS:
            raise ValueError("AI intake intent category is unsupported")
        for tool_key in raw.get("allowed_tools") or []:
            if tool_key not in ai_intake_conversation_engine.TOOL_CATALOG:
                raise ValueError("AI intake intent references an unknown tool")


def _validate_activation(
    db: Session, *, policy: AiIntakePolicy, version: AiIntakePolicyVersion
) -> None:
    if str(policy.scope_key or "").strip().lower() in {"", "global", "default", "any"}:
        raise ValueError("AI intake activation requires an explicit provider scope")
    _validate_provider_scope(
        db,
        channel_type=policy.channel_type,
        provider=policy.provider,
        account_scope=policy.account_scope,
    )
    if policy.fallback_team_id is None:
        raise ValueError("AI intake activation requires a fallback team")
    fallback = db.get(ServiceTeam, policy.fallback_team_id)
    if fallback is None or not fallback.is_active:
        raise ValueError("AI intake fallback team must be active")
    if not (version.welcome_message or "").strip():
        raise ValueError("AI intake activation requires a welcome message")
    _validate_queue_templates(version.queue_templates)
    _validate_intent_definitions(version)
    _validate_engine_policy(version)
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


def validate_policy_version(
    db: Session, command: AiPolicyVersionActivateCommand
) -> AiPolicyVersionValidationOutcome:
    """Validate a draft for activation without mutating policy state."""

    version = db.get(AiIntakePolicyVersion, command.version_id)
    if version is None:
        return AiPolicyVersionValidationOutcome(
            policy_id=command.version_id,
            version_id=command.version_id,
            valid=False,
            errors=("AI intake policy version was not found",),
        )
    policy = db.get(AiIntakePolicy, version.policy_id)
    if policy is None:
        return AiPolicyVersionValidationOutcome(
            policy_id=version.policy_id,
            version_id=version.id,
            valid=False,
            errors=("AI intake policy was not found",),
        )
    if version.status != "draft":
        return AiPolicyVersionValidationOutcome(
            policy_id=policy.id,
            version_id=version.id,
            valid=False,
            errors=("Only draft AI intake policy versions can be activated",),
        )
    try:
        _validate_activation(db, policy=policy, version=version)
    except ValueError as exc:
        return AiPolicyVersionValidationOutcome(
            policy_id=policy.id,
            version_id=version.id,
            valid=False,
            errors=(str(exc),),
        )
    return AiPolicyVersionValidationOutcome(
        policy_id=policy.id,
        version_id=version.id,
        valid=True,
        errors=(),
    )


def _preview_classification(message: str) -> AiIntakeClassification:
    facts = ai_intake_conversation_engine.extract_facts(message)
    if facts.get("slow_internet"):
        return AiIntakeClassification(
            intent=AiIntakeIntent.technical_support,
            category=AiIntakeCategory.slow_internet,
            confidence=0.86,
            requires_follow_up=False,
        )
    if facts.get("connectivity_problem") or facts.get("los_red"):
        return AiIntakeClassification(
            intent=AiIntakeIntent.technical_support,
            category=AiIntakeCategory.no_internet,
            confidence=0.86,
            requires_follow_up=False,
        )
    return AiIntakeClassification(
        intent=AiIntakeIntent.unknown,
        category=AiIntakeCategory.unknown,
        confidence=0.35,
        requires_follow_up=True,
        follow_up_question=DEFAULT_CLARIFICATION_QUESTIONS[0],
    )


def preview_policy_version(
    db: Session, command: AiPolicyPreviewCommand
) -> AiPolicyPreviewResult:
    """Run a draft/saved policy against one example message without live side effects."""

    version = db.get(AiIntakePolicyVersion, command.version_id)
    if version is None:
        raise ValueError("AI intake policy version was not found")
    policy = db.get(AiIntakePolicy, version.policy_id)
    if policy is None:
        raise ValueError("AI intake policy was not found")
    body = str(command.customer_message or "").strip()
    if not body:
        raise ValueError("Enter a customer message to preview")
    preview_mode = str(command.preview_mode or "simulation").strip()
    if preview_mode not in {"simulation", "live_read_only"}:
        raise ValueError("AI intake preview mode is invalid")
    channel = command.channel_type or policy.channel_type
    version_metadata = (
        dict(version.metadata_ or {})
        if version is not None and isinstance(version.metadata_, Mapping)
        else {}
    )
    raw_conversation_policy = version_metadata.get("conversation_policy")
    conversation_policy = (
        dict(raw_conversation_policy)
        if isinstance(raw_conversation_policy, Mapping)
        else {}
    )
    conversation = cast(
        InboxConversation,
        _PreviewConversation(
            id=uuid4(),
            channel_type=channel,
            status=InboxConversationStatus.pending.value,
            metadata_={},
        ),
    )
    session = AiIntakeSession(
        id=uuid4(),
        conversation_id=conversation.id,
        policy_id=policy.id,
        policy_version_id=version.id,
        legacy_config_id=policy.legacy_config_id,
        state="collecting_intent",
        channel_type=channel,
        provider=policy.provider,
        account_scope=policy.account_scope,
        display_name=version.display_name,
        turn_count=0,
        max_turns=_bounded_int(
            conversation_policy.get("max_turns"),
            default=6,
            minimum=1,
            maximum=10,
        ),
        confidence_threshold=0.75,
        fallback_team_id=policy.fallback_team_id,
        metadata_={},
    )
    classification = _preview_classification(body)
    if ai_intake_graph.langgraph_engine_enabled(version):
        decision = ai_intake_graph.run_ai_intake_graph(
            db,
            conversation=conversation,
            session=session,
            version=version,
            latest_body=body,
            classification=classification,
            recent_messages=(),
            tool_mode=preview_mode,
        )
    else:
        decision = ai_intake_conversation_engine.run_conversational_turn(
            db,
            conversation=conversation,
            session=session,
            version=version,
            latest_body=body,
            classification=classification,
            tool_mode=preview_mode,
        )
    routing_metadata: dict[str, object] = {
        "ai_intake_status": "classified",
        "ai_intent": decision.state.current_intent,
        "ai_category": decision.state.category,
        "ai_confidence": decision.state.confidence,
        "ai_department_team_id": decision.state.destination_team_id,
    }
    routing = team_inbox_routing.resolve_channel_routing_decision(
        db,
        channel_type=channel,
        provider=policy.provider,
        account_scope=policy.account_scope,
        fallback_service_team_id=policy.fallback_team_id,
        metadata=routing_metadata,
    )
    destination_name = None
    if routing.primary_service_team_id:
        team = db.get(ServiceTeam, UUID(str(routing.primary_service_team_id)))
        destination_name = team.name if team is not None else None
    return AiPolicyPreviewResult(
        version_id=version.id,
        interpreted_intent=decision.state.current_intent,
        category=decision.state.category,
        confidence=decision.state.confidence,
        current_state=decision.state.to_dict(),
        next_action=decision.action,
        response_text=decision.response_text,
        handoff_decision=decision.action == "handoff",
        destination_team_id=routing.primary_service_team_id,
        destination_team_name=destination_name,
        tool_executions=tuple(decision.state.tool_executions),
        preview_mode=preview_mode,
    )


def _policy_status(policy: AiIntakePolicy, draft: AiIntakePolicyVersion | None) -> str:
    if not policy.is_enabled and policy.active_version_id is not None:
        return "Disabled"
    if policy.is_enabled:
        return "Active"
    if draft is not None:
        return "Draft"
    return "Disabled"


_ADMIN_POLICY_VERSION_HISTORY_LIMIT = 20


def admin_policy_context(db: Session) -> dict[str, object]:
    """Build the admin AI intake policy read model for the settings template."""

    from app.services import ai_intake_canary_library, ai_intake_rollout_readiness

    rows = (
        db.query(AiIntakePolicy)
        .order_by(AiIntakePolicy.updated_at.desc(), AiIntakePolicy.created_at.desc())
        .all()
    )
    versions_by_policy: dict[UUID, list[AiIntakePolicyVersion]] = {}
    if rows:
        versions = (
            db.query(AiIntakePolicyVersion)
            .filter(AiIntakePolicyVersion.policy_id.in_([row.id for row in rows]))
            .order_by(
                AiIntakePolicyVersion.policy_id.asc(),
                AiIntakePolicyVersion.version_number.desc(),
            )
            .all()
        )
        for version in versions:
            versions_by_policy.setdefault(version.policy_id, []).append(version)
    selected_policy = rows[0] if rows else None
    selected_versions = (
        versions_by_policy.get(selected_policy.id, []) if selected_policy else []
    )
    history_versions = (
        db.query(AiIntakePolicyVersion)
        .filter(AiIntakePolicyVersion.policy_id == selected_policy.id)
        .order_by(AiIntakePolicyVersion.version_number.desc())
        .limit(_ADMIN_POLICY_VERSION_HISTORY_LIMIT)
        .all()
        if selected_policy is not None
        else []
    )
    version_history = tuple(
        AiPolicyVersionHistoryRow(
            version_id=version.id,
            version_number=version.version_number,
            status=version.status,
            is_active=(
                selected_policy is not None
                and selected_policy.active_version_id == version.id
            ),
            created_at=version.created_at,
            activated_at=version.activated_at,
            superseded_at=version.superseded_at,
        )
        for version in history_versions
    )
    selected_draft = next(
        (version for version in selected_versions if version.status == "draft"),
        None,
    )
    selected_active = (
        db.get(AiIntakePolicyVersion, selected_policy.active_version_id)
        if selected_policy and selected_policy.active_version_id is not None
        else None
    )
    editable_version = selected_draft or selected_active
    escalation_rules = (
        dict(editable_version.escalation_rules or {})
        if editable_version is not None
        and isinstance(editable_version.escalation_rules, dict)
        else {}
    )
    escalation_rules.setdefault(
        "customer_response_timeout_minutes",
        _customer_response_timeout_minutes(escalation_rules),
    )
    queue_templates = (
        dict(editable_version.queue_templates or {})
        if editable_version is not None
        and isinstance(editable_version.queue_templates, dict)
        else {}
    )
    data_cleanup_policy = (
        dict(editable_version.data_cleanup_policy or {})
        if editable_version is not None
        and isinstance(editable_version.data_cleanup_policy, dict)
        else {}
    )
    version_metadata = (
        dict(editable_version.metadata_ or {})
        if editable_version is not None and isinstance(editable_version.metadata_, dict)
        else {}
    )
    conversation_policy = dict(version_metadata.get("conversation_policy") or {})
    conversation_templates = dict(version_metadata.get("conversation_templates") or {})
    channel_overrides = dict(version_metadata.get("channel_overrides") or {})
    conversation_engine_mode = _normalize_conversation_engine_mode(
        str(
            version_metadata.get("conversation_engine_mode")
            or ai_intake_graph.CUSTOM_ENGINE_MODE
        )
    )
    tool_config = dict(version_metadata.get("tools") or {})
    permitted_identifiers = version_metadata.get("permitted_identifiers")
    if not isinstance(permitted_identifiers, list):
        permitted_identifiers = [
            "registered_phone",
            "registered_email",
            "portal_id",
        ]
    mapping_json = "[]"
    if editable_version is not None:
        mapping_json = json.dumps(editable_version.intent_team_mappings or [], indent=2)
    intent_definitions = (
        list(editable_version.intent_definitions or [])
        if editable_version is not None
        and isinstance(editable_version.intent_definitions, list)
        else []
    )
    try:
        clarification_questions = normalize_clarification_questions(
            editable_version.clarification_questions
            if editable_version is not None
            else None
        )
    except ValueError:
        clarification_questions = DEFAULT_CLARIFICATION_QUESTIONS
    return {
        "ai_intake_policies": [
            {
                "id": str(policy.id),
                "scope_key": policy.scope_key,
                "channel_type": policy.channel_type,
                "provider": policy.provider,
                "account_scope": policy.account_scope,
                "status": _policy_status(
                    policy,
                    next(
                        (
                            version
                            for version in versions_by_policy.get(policy.id, [])
                            if version.status == "draft"
                        ),
                        None,
                    ),
                ),
                "active_version_id": str(policy.active_version_id)
                if policy.active_version_id
                else None,
            }
            for policy in rows
        ],
        "ai_intake_policy": selected_policy,
        "ai_intake_draft_version": selected_draft,
        "ai_intake_active_version": selected_active,
        "ai_intake_policy_version_history": version_history,
        "ai_intake_policy_version_history_limit": _ADMIN_POLICY_VERSION_HISTORY_LIMIT,
        "ai_intake_edit_version": editable_version,
        "ai_intake_policy_status": (
            _policy_status(selected_policy, selected_draft)
            if selected_policy
            else "Draft"
        ),
        "ai_intake_mapping_json": mapping_json,
        "ai_intake_intent_definitions": intent_definitions,
        "ai_intake_supported_intent_keys": tuple(sorted(SUPPORTED_AI_INTENT_KEYS)),
        "ai_intake_supported_category_keys": tuple(sorted(SUPPORTED_AI_CATEGORY_KEYS)),
        "ai_intake_supported_rule_conditions": tuple(
            sorted(ai_intake_conversation_engine.SUPPORTED_RULE_CONDITIONS)
        ),
        "ai_intake_supported_rule_actions": tuple(
            sorted(ai_intake_conversation_engine.SUPPORTED_RULE_ACTIONS)
        ),
        "ai_intake_supported_engine_modes": tuple(
            sorted(SUPPORTED_CONVERSATION_ENGINE_MODES)
        ),
        "ai_intake_clarification_questions": clarification_questions,
        "ai_intake_escalation_rules": escalation_rules,
        "ai_intake_queue_templates": queue_templates,
        "ai_intake_data_cleanup_policy": data_cleanup_policy,
        "ai_intake_engine_metadata": version_metadata,
        "ai_intake_conversation_engine_mode": conversation_engine_mode,
        "ai_intake_conversation_policy": conversation_policy,
        "ai_intake_conversation_templates": conversation_templates,
        "ai_intake_channel_overrides_json": json.dumps(
            channel_overrides,
            indent=2,
            sort_keys=True,
        ),
        "ai_intake_tool_config": tool_config,
        "ai_intake_permitted_identifiers": permitted_identifiers,
        "ai_intake_tool_catalogue": (
            ai_intake_conversation_engine.tool_catalogue_snapshot()
        ),
        "ai_intake_canary_matrix": (
            ai_intake_rollout_readiness.data_driven_scenario_matrix()
        ),
        "ai_intake_canary_library": ai_intake_rollout_readiness.canary_library_rows(),
        "ai_intake_canary_suites": ai_intake_canary_library.list_suites(db),
        "ai_intake_canary_assertion_types": tuple(
            sorted(
                ai_intake_rollout_readiness.ai_intake_canary_runner.SUPPORTED_ASSERTION_TYPES
            )
        ),
        "ai_intake_pre_activation_gate": (
            ai_intake_rollout_readiness.pre_activation_gate_report(
                db=db,
                policy_version_id=selected_active.id if selected_active else None,
            )
        ),
        "ai_intake_activation_plan": (
            ai_intake_rollout_readiness.CONTROLLED_ACTIVATION_PLAN
        ),
    }


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        parsed = default
    else:
        try:
            parsed = int(value)
        except ValueError:
            parsed = default
    return max(minimum, min(parsed, maximum))


def _customer_response_timeout_minutes(
    rules: Mapping[str, object] | None,
) -> int:
    source = dict(rules or {})
    raw_value = source.get("customer_response_timeout_minutes")
    if raw_value is None:
        raw_value = source.get("escalate_after_minutes")
    return _bounded_int(
        raw_value,
        default=DEFAULT_CUSTOMER_RESPONSE_TIMEOUT_MINUTES,
        minimum=1,
        maximum=1440,
    )


def _customer_wait_expiry_hours(rules: Mapping[str, object] | None) -> int:
    source = dict(rules or {})
    return _bounded_int(
        source.get("customer_wait_expiry_hours"),
        default=DEFAULT_CUSTOMER_WAIT_EXPIRY_HOURS,
        minimum=24,
        maximum=720,
    )


def configured_customer_response_timeout_minutes(
    db: Session,
    *,
    channel_type: str,
    provider: str,
    account_scope: str,
) -> int:
    """Return the editable policy timeout for admin form omission fallback."""

    channel = _normalize_text(channel_type, field="channel", limit=40)
    normalized_provider = _normalize_text(provider, field="provider", limit=80)
    normalized_scope = _normalize_text(account_scope, field="account scope", limit=160)
    policy = (
        db.query(AiIntakePolicy)
        .filter(
            AiIntakePolicy.scope_key
            == _provider_scope_key(normalized_provider, normalized_scope)
        )
        .filter(AiIntakePolicy.channel_type == channel)
        .filter(AiIntakePolicy.provider == normalized_provider)
        .filter(AiIntakePolicy.account_scope == normalized_scope)
        .one_or_none()
    )
    if policy is None:
        return DEFAULT_CUSTOMER_RESPONSE_TIMEOUT_MINUTES
    editable_version = (
        db.get(AiIntakePolicyVersion, policy.active_version_id)
        if policy.active_version_id is not None
        else None
    )
    draft = (
        db.query(AiIntakePolicyVersion)
        .filter(AiIntakePolicyVersion.policy_id == policy.id)
        .filter(AiIntakePolicyVersion.status == "draft")
        .order_by(AiIntakePolicyVersion.version_number.desc())
        .first()
    )
    editable_version = draft or editable_version
    rules = (
        editable_version.escalation_rules
        if editable_version is not None
        and isinstance(editable_version.escalation_rules, Mapping)
        else None
    )
    return _customer_response_timeout_minutes(rules)


def _session_customer_response_timeout_minutes(
    session: AiIntakeSession,
    *,
    version: AiIntakePolicyVersion | None = None,
) -> int:
    if version is not None and isinstance(version.escalation_rules, Mapping):
        return _customer_response_timeout_minutes(version.escalation_rules)
    metadata = dict(session.metadata_ or {})
    return _bounded_int(
        metadata.get("customer_response_timeout_minutes"),
        default=DEFAULT_CUSTOMER_RESPONSE_TIMEOUT_MINUTES,
        minimum=1,
        maximum=1440,
    )


def _session_customer_wait_expiry_hours(
    session: AiIntakeSession,
    *,
    version: AiIntakePolicyVersion | None = None,
) -> int:
    if version is not None and isinstance(version.escalation_rules, Mapping):
        return _customer_wait_expiry_hours(version.escalation_rules)
    metadata = dict(session.metadata_ or {})
    return _bounded_int(
        metadata.get("customer_wait_expiry_hours"),
        default=DEFAULT_CUSTOMER_WAIT_EXPIRY_HOURS,
        minimum=24,
        maximum=720,
    )


def record_customer_wait(
    session: AiIntakeSession,
    *,
    version: AiIntakePolicyVersion | None = None,
    inbound_message_id: UUID | None = None,
    reason: str,
    now: datetime | None = None,
) -> datetime:
    started_at = (now or datetime.now(UTC)).astimezone(UTC)
    expiry_hours = _session_customer_wait_expiry_hours(session, version=version)
    expires_at = started_at + timedelta(hours=expiry_hours)
    session.customer_wait_started_at = started_at
    # This legacy column previously drove a five-minute handoff. It is kept
    # clear so stale-session recovery cannot confuse customer waiting with AI
    # failure. ``expires_at`` owns long-term, non-handoff expiry.
    session.customer_wait_expires_at = None
    session.expires_at = expires_at
    metadata = dict(session.metadata_ or {})
    metadata.update(
        {
            "customer_wait_started_at": started_at.isoformat(),
            "customer_wait_expires_at": None,
            "customer_wait_expiry_hours": expiry_hours,
            "customer_wait_reason": reason,
            "waiting_reason": "awaiting_customer",
        }
    )
    if inbound_message_id is not None:
        metadata["customer_wait_inbound_message_id"] = str(inbound_message_id)
    session.metadata_ = metadata
    logger.info(
        "ai intake customer wait deadline recorded",
        extra={
            "event": "ai_intake_customer_wait_deadline_recorded",
            "session_id": str(session.id),
            "conversation_id": str(session.conversation_id),
            "inbound_message_id": str(inbound_message_id)
            if inbound_message_id is not None
            else None,
            "customer_wait_started_at": started_at.isoformat(),
            "customer_wait_expires_at": None,
            "session_expires_at": expires_at.isoformat(),
            "expiry_hours": expiry_hours,
            "reason": reason,
        },
    )
    return expires_at


def clear_customer_wait(
    session: AiIntakeSession,
    *,
    reason: str,
    now: datetime | None = None,
) -> None:
    had_wait = bool(
        session.customer_wait_started_at or session.customer_wait_expires_at
    )
    session.customer_wait_started_at = None
    session.customer_wait_expires_at = None
    metadata = dict(session.metadata_ or {})
    for key in (
        "customer_wait_started_at",
        "customer_wait_expires_at",
        "customer_wait_reason",
        "customer_wait_inbound_message_id",
    ):
        metadata.pop(key, None)
    metadata["customer_wait_cleared_reason"] = reason
    metadata["customer_wait_cleared_at"] = (now or datetime.now(UTC)).isoformat()
    session.metadata_ = metadata
    if had_wait:
        logger.info(
            "ai intake customer wait cleared",
            extra={
                "event": "ai_intake_customer_wait_cleared",
                "session_id": str(session.id),
                "conversation_id": str(session.conversation_id),
                "reason": reason,
            },
        )


def mark_inbound_processed(
    session: AiIntakeSession,
    *,
    inbound_message_id: UUID,
    generation_attempt_id: UUID | None = None,
) -> None:
    metadata = dict(session.metadata_ or {})
    metadata[f"processed_inbound:{inbound_message_id}"] = True
    if generation_attempt_id is not None:
        metadata["last_generation_attempt_id"] = str(generation_attempt_id)
    session.metadata_ = metadata

    logger.info(
        "ai intake inbound turn completed",
        extra={
            "event": "ai_intake_inbound_turn_completed",
            "session_id": str(session.id),
            "conversation_id": str(session.conversation_id),
            "inbound_message_id": str(inbound_message_id),
            "generation_attempt_id": str(generation_attempt_id)
            if generation_attempt_id is not None
            else None,
            "turn_count": session.turn_count,
            "is_initial_turn": session.turn_count <= 1,
            "session_state": session.state,
        },
    )


def _bounded_float(
    value: object, *, default: float, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        parsed = default
    else:
        try:
            parsed = float(value)
        except ValueError:
            parsed = default
    return max(minimum, min(parsed, maximum))


def _sync_active_policy_to_legacy_config(
    db: Session, *, policy: AiIntakePolicy, version: AiIntakePolicyVersion
) -> AiIntakeConfig:
    """Project the activated canonical policy into the current receive-path row."""
    escalation_rules = (
        dict(version.escalation_rules or {})
        if isinstance(version.escalation_rules, Mapping)
        else {}
    )
    queue_templates = (
        dict(version.queue_templates or {})
        if isinstance(version.queue_templates, Mapping)
        else {}
    )
    data_cleanup_policy = (
        dict(version.data_cleanup_policy or {})
        if isinstance(version.data_cleanup_policy, Mapping)
        else {}
    )
    version_metadata = (
        dict(version.metadata_ or {})
        if version is not None and isinstance(version.metadata_, Mapping)
        else {}
    )
    mappings: list[AiIntakeDepartmentMapping] = []
    for raw in version.intent_team_mappings or []:
        if not isinstance(raw, Mapping) or raw.get("enabled") is False:
            continue
        team_id = raw.get("service_team_id") or raw.get("team_id")
        intent = raw.get("intent") or raw.get("keyword")
        department = raw.get("department") or raw.get("team") or intent
        mappings.append(
            AiIntakeDepartmentMapping.model_validate(
                {
                    "intent": str(intent or "").strip(),
                    "department": str(department or "").strip(),
                    "service_team_id": str(team_id) if team_id else None,
                }
            )
        )
    outcome = ai_intake.project_config_from_canonical_policy(
        db,
        ai_intake.UpsertAiIntakeConfigCommand(
            context=CommandContext.system(
                actor="service:ai.conversation_intake",
                scope=ai_intake.CONFIG_SCOPE,
                reason="project activated canonical AI intake policy",
            ),
            policy=AiIntakeConfigUpsert(
                scope_key=policy.scope_key,
                channel_type=policy.channel_type,
                is_enabled=True,
                confidence_threshold=_bounded_float(
                    escalation_rules.get("confidence_threshold"),
                    default=0.75,
                    minimum=0.0,
                    maximum=1.0,
                ),
                allow_followup_questions=bool(
                    escalation_rules.get("allow_followup_questions", True)
                ),
                max_clarification_turns=_bounded_int(
                    escalation_rules.get("max_clarification_turns"),
                    default=1,
                    minimum=0,
                    maximum=5,
                ),
                escalate_after_minutes=_bounded_int(
                    escalation_rules.get("escalate_after_minutes"),
                    default=5,
                    minimum=1,
                    maximum=1440,
                ),
                customer_response_timeout_minutes=(
                    _customer_response_timeout_minutes(escalation_rules)
                ),
                exclude_campaign_attribution=bool(
                    escalation_rules.get("exclude_campaign_attribution", True)
                ),
                fallback_team_id=policy.fallback_team_id,
                instructions=version.business_instructions,
                department_mappings=tuple(mappings),
                metadata=AiIntakeConfigMetadata(
                    display_name=version.display_name,
                    welcome_message=version.welcome_message,
                    business_tone=version.business_tone,
                    approved_isp_information=version.approved_isp_information,
                    intent_definitions=version.intent_definitions or [],
                    clarification_questions=version.clarification_questions or [],
                    queue_templates=queue_templates,
                    conversation_templates=version_metadata.get(
                        "conversation_templates"
                    )
                    or {},
                    channel_overrides=version_metadata.get("channel_overrides") or {},
                    escalation_rules=escalation_rules,
                    data_cleanup_enabled=bool(
                        data_cleanup_policy.get("production_collection_enabled", False)
                    ),
                    data_cleanup_policy=data_cleanup_policy,
                ),
            ),
        ),
    )
    config = db.get(AiIntakeConfig, outcome.id)
    if config is None:
        raise ValueError("Projected AI intake runtime config was not found")
    config.metadata_ = {
        **dict(config.metadata_ or {}),
        "compatibility_source": "canonical_ai_intake_policy",
        "policy_id": str(policy.id),
        "policy_version_id": str(version.id),
        "provider": policy.provider,
        "account_scope": policy.account_scope,
        "queue_position_update_minutes": _bounded_int(
            queue_templates.get("position_update_minutes"),
            default=DEFAULT_QUEUE_POSITION_UPDATE_MINUTES,
            minimum=1,
            maximum=120,
        ),
        "queue_heartbeat_minutes": _bounded_int(
            queue_templates.get("heartbeat_minutes"),
            default=DEFAULT_QUEUE_HEARTBEAT_MINUTES,
            minimum=5,
            maximum=240,
        ),
        "customer_response_timeout_minutes": _customer_response_timeout_minutes(
            escalation_rules
        ),
        "conversational_engine_enabled": bool(
            version_metadata.get("conversational_engine_enabled")
        ),
        "conversation_engine_mode": _normalize_conversation_engine_mode(
            str(
                version_metadata.get("conversation_engine_mode")
                or ai_intake_graph.CUSTOM_ENGINE_MODE
            )
        ),
        "conversation_policy": version_metadata.get("conversation_policy") or {},
        "conversation_templates": version_metadata.get("conversation_templates") or {},
        "channel_overrides": version_metadata.get("channel_overrides") or {},
        "tools": version_metadata.get("tools") or {},
        "permitted_identifiers": version_metadata.get("permitted_identifiers") or [],
    }
    policy.legacy_config_id = config.id
    db.flush()
    return config


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
        _sync_active_policy_to_legacy_config(db, policy=policy, version=version)
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


def disable_policy(
    db: Session, command: AiPolicyDisableCommand
) -> AiPolicyDisableOutcome:
    """Disable a canonical policy or exact provider/account scope for new sessions.

    Active sessions keep their pinned policy version and continue through their
    established handoff, expiry, or completion path. No version or session
    evidence is deleted.
    """

    def _operation() -> AiPolicyDisableOutcome:
        query = db.query(AiIntakePolicy).with_for_update()
        if command.policy_id is not None:
            query = query.filter(AiIntakePolicy.id == command.policy_id)
        else:
            if (
                command.channel_type is None
                or command.provider is None
                or command.account_scope is None
            ):
                raise ValueError(
                    "AI intake disable requires a policy id or exact provider scope"
                )
            channel = _normalize_text(command.channel_type, field="channel", limit=40)
            provider = _normalize_text(command.provider, field="provider", limit=80)
            account_scope = _normalize_text(
                command.account_scope, field="account scope", limit=160
            )
            query = (
                query.filter(
                    AiIntakePolicy.scope_key
                    == _provider_scope_key(provider, account_scope)
                )
                .filter(AiIntakePolicy.channel_type == channel)
                .filter(AiIntakePolicy.provider == provider)
                .filter(AiIntakePolicy.account_scope == account_scope)
            )
        policy = query.one_or_none()
        if policy is None:
            raise ValueError("AI intake policy was not found")
        policy.is_enabled = False
        policy.metadata_ = {
            **dict(policy.metadata_ or {}),
            "disabled_reason": command.context.reason,
            "disabled_at": datetime.now(UTC).isoformat(),
        }
        legacy_config_enabled: bool | None = None
        legacy_config_id = policy.legacy_config_id
        if policy.legacy_config_id is not None:
            disabled = ai_intake.disable_projected_config(
                db,
                context=CommandContext.system(
                    actor="service:ai.conversation_intake",
                    scope=ai_intake.CONFIG_SCOPE,
                    reason=command.context.reason,
                ),
                config_id=policy.legacy_config_id,
            )
            if disabled is not None:
                legacy_config_id = disabled.id
                legacy_config_enabled = disabled.is_enabled
        db.flush()
        return AiPolicyDisableOutcome(
            policy_id=policy.id,
            active_version_id=policy.active_version_id,
            policy_enabled=policy.is_enabled,
            legacy_config_id=legacy_config_id,
            legacy_config_enabled=legacy_config_enabled,
        )

    return execute_owner_command(
        db,
        definition=_AI_POLICY_DRAFT_COMMAND,
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
    outbound_rows = (
        db.query(InboxMessage.metadata_)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .all()
    )
    for (message_metadata,) in outbound_rows:
        metadata = (
            dict(message_metadata or {})
            if isinstance(message_metadata, Mapping)
            else {}
        )
        sent_by_person_id = str(metadata.get("sent_by_person_id") or "").strip().lower()
        if sent_by_person_id in {"", "null", "none"}:
            continue
        sender_type = str(metadata.get("sender_type") or "").strip().lower()
        author_type = str(metadata.get("author_type") or "").strip().lower()
        automation_kind = str(metadata.get("automation_kind") or "").strip().lower()
        if sender_type == "ai" or author_type == "ai" or automation_kind == "ai_intake":
            continue
        return True
    return False


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
    if metadata.get("compatibility_source") == "canonical_ai_intake_policy":
        try:
            policy_id = UUID(str(metadata.get("policy_id")))
            version_id = UUID(str(metadata.get("policy_version_id")))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "AI intake compatibility policy metadata is invalid"
            ) from exc
        policy = db.get(AiIntakePolicy, policy_id)
        version = db.get(AiIntakePolicyVersion, version_id)
        if (
            policy is None
            or version is None
            or version.policy_id != policy.id
            or policy.legacy_config_id != config.id
            or policy.active_version_id != version.id
        ):
            raise ValueError("AI intake compatibility policy projection is stale")
        policy.is_enabled = config.is_enabled
        policy.fallback_team_id = config.fallback_team_id
        return policy, version
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
        "customer_response_timeout_minutes": int(
            config.customer_response_timeout_minutes
            or config.escalate_after_minutes
            or DEFAULT_CUSTOMER_RESPONSE_TIMEOUT_MINUTES
        ),
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
        or ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"],
        "production_collection_enabled": bool(
            metadata.get("data_cleanup_enabled") or False
        ),
    }
    runtime_metadata = {
        key: metadata[key]
        for key in (
            "conversational_engine_enabled",
            "conversation_engine_mode",
            "conversation_policy",
            "conversation_templates",
            "channel_overrides",
            "tools",
            "permitted_identifiers",
        )
        if key in metadata
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
            clarification_questions=list(
                normalize_clarification_questions(
                    metadata.get("clarification_questions")
                )
            ),
            intent_team_mappings=config.department_mappings,
            queue_templates=queue_templates,
            escalation_rules=metadata.get("escalation_rules"),
            data_cleanup_policy=data_cleanup_policy,
            metadata_={
                "compatibility_signature": version_signature,
                "provider_scope_observed": provider,
                "account_scope_observed": account_scope,
                **runtime_metadata,
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
    initial_inbound_message_id: UUID,
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
        # A fresh conversation must receive the configured introduction before
        # classification can produce a clarification question or handoff.
        state = "welcome_pending"
        policy_metadata = dict(policy.metadata_ or {})
        expiry_hours = _customer_wait_expiry_hours(version.escalation_rules)
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
            expires_at=now + timedelta(hours=expiry_hours),
            metadata_={
                "ai_handling": True,
                "created_from": "team_inbox_receive",
                "customer_wait_expiry_hours": expiry_hours,
                "initial_inbound_message_id": str(initial_inbound_message_id),
            },
        )
        db.add(session)
    else:
        session_metadata = dict(session.metadata_ or {})
        session_metadata.setdefault(
            "initial_inbound_message_id", str(initial_inbound_message_id)
        )
        session.metadata_ = session_metadata
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
    provider_request_id: str | None = None,
    metadata: Mapping[str, object] | None = None,
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
        provider_request_id=provider_request_id,
        status=status,
        duration_ms=duration_ms,
        error_code=error_code,
        metadata_=dict(metadata or {}),
        idempotency_key=dedupe,
        generated_at=(
            datetime.now(UTC)
            if status in {"queued", "generated", "safe_fallback"}
            else None
        ),
    )
    db.add(attempt)
    db.flush()
    return attempt


def _session_engine_enabled(
    db: Session,
    *,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
) -> bool:
    version_metadata = (
        dict(version.metadata_ or {})
        if version is not None and isinstance(version.metadata_, Mapping)
        else {}
    )
    if "conversational_engine_enabled" in version_metadata:
        return bool(version_metadata.get("conversational_engine_enabled"))
    legacy_config = (
        db.get(AiIntakeConfig, session.legacy_config_id)
        if session.legacy_config_id is not None
        else None
    )
    legacy_metadata = (
        dict(legacy_config.metadata_ or {})
        if legacy_config is not None and isinstance(legacy_config.metadata_, Mapping)
        else {}
    )
    return bool(legacy_metadata.get("conversational_engine_enabled"))


def _safe_enum(enum_type: type[AiIntakeIntent] | type[AiIntakeCategory], value: str):
    try:
        return enum_type(value)
    except ValueError:
        return enum_type("unknown")


def _composition_facts(
    state: ai_intake_conversation_engine.ConversationalState,
) -> AiIntakeExtractedFacts:
    raw = dict(state.collected_facts)
    values = {
        key: raw.get(key) for key in AiIntakeExtractedFacts.model_fields if key in raw
    }
    values.update(
        {
            "portal_id": state.portal_id,
            "registered_email": state.registered_email,
            "registered_phone": state.registered_phone,
            "human_requested": state.human_requested,
        }
    )
    try:
        return AiIntakeExtractedFacts.model_validate(values)
    except (TypeError, ValueError):
        return AiIntakeExtractedFacts(
            human_requested=state.human_requested,
            portal_id=state.portal_id,
            registered_email=state.registered_email,
            registered_phone=state.registered_phone,
        )


def _composition_monitoring(
    state: ai_intake_conversation_engine.ConversationalState,
) -> AiIntakeMonitoringContext | None:
    if not state.monitoring_results:
        return None
    latest = state.monitoring_results[-1]
    radius_raw = latest.get("radius_observation")
    radius = None
    if isinstance(radius_raw, Mapping):
        radius = AiIntakeRadiusContext(
            state=str(radius_raw.get("state") or "") or None,
            active_session_count=(
                int(radius_raw["active_session_count"])
                if radius_raw.get("active_session_count") is not None
                else None
            ),
            observed_at=radius_raw.get("observed_at"),
        )
    onts_raw = latest.get("ont_observations")
    onts = tuple(
        AiIntakeOntContext(
            effective_state=str(item.get("effective_state") or "") or None
        )
        for item in (onts_raw if isinstance(onts_raw, list) else [])
        if isinstance(item, Mapping)
    )
    return AiIntakeMonitoringContext(
        status=str(latest.get("status") or "") or None,
        radius=radius,
        onts=onts,
    )


def _composition_request(
    *,
    version: AiIntakePolicyVersion | None,
    inbound: InboxMessage,
    recent: tuple[AiIntakeContextMessage, ...],
    decision: ai_intake_conversation_engine.ConversationEngineDecision,
) -> AiCustomerResponseCompositionRequest:
    state = decision.state
    next_action_value = str(decision.metadata.get("next_action") or "")
    if not next_action_value:
        next_action_value = {
            "resolved": AiIntakeNextAction.resolve.value,
            "handoff": AiIntakeNextAction.handoff.value,
        }.get(decision.action, AiIntakeNextAction.provide_guidance.value)
    next_action = AiIntakeNextAction(next_action_value)
    question_key = str(decision.metadata.get("question_key") or "").strip() or None
    policy = ai_intake_conversation_engine._policy(
        version, channel=str(inbound.channel_type)
    )
    return AiCustomerResponseCompositionRequest(
        intent=_safe_enum(AiIntakeIntent, state.current_intent or "unknown"),
        category=_safe_enum(AiIntakeCategory, state.category or "unknown"),
        latest_customer_statement=str(inbound.body or "")[:1200],
        recent_messages=recent,
        facts=_composition_facts(state),
        missing_fact_keys=tuple(state.missing_facts[-12:]),
        asked_question_keys=tuple(item.key for item in state.question_history[-12:]),
        troubleshooting_completed=tuple(state.troubleshooting_completed[-12:]),
        customer_identity=AiIntakeSafeCustomerIdentity(
            identified=bool(state.subscriber_id),
            subscriber_status=str(
                state.service_account_identity.get("subscriber_status") or ""
            )
            or None,
        ),
        monitoring=_composition_monitoring(state),
        playbook_step=AiIntakePlaybookStepContext(
            key=question_key,
            action=next_action,
            approved_instruction=(decision.response_text or "Continue safely.")[:800],
        ),
        business_tone=str(
            policy.get("business_tone")
            or "Warm, calm, concise, practical, and proportionate."
        )[:1000],
        approved_isp_information=(
            str(policy.get("approved_isp_information") or "")[:4000] or None
        ),
        issue_already_acknowledged=bool(state.acknowledged_issue_key),
    )


def mark_handoff_requested(
    session: AiIntakeSession,
    *,
    destination_team_id: str | UUID | None,
) -> None:
    session.state = "handoff_requested"
    session.handoff_requested_at = datetime.now(UTC)
    clear_customer_wait(session, reason="handoff_requested")
    metadata = dict(session.metadata_ or {})
    metadata["destination_team_id"] = (
        str(destination_team_id) if destination_team_id else None
    )
    session.metadata_ = metadata


def complete_session(session: AiIntakeSession, *, state: str = "completed") -> None:
    if state not in TERMINAL_SESSION_STATES:
        raise ValueError("AI intake terminal state is invalid")
    clear_customer_wait(session, reason=state)
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


def _next_unprocessed_inbound(
    db: Session, *, session: AiIntakeSession, conversation: InboxConversation
) -> InboxMessage | None:
    metadata = dict(session.metadata_ or {})
    initial: InboxMessage | None = None
    raw_initial_id = metadata.get("initial_inbound_message_id")
    if raw_initial_id:
        try:
            initial_id = UUID(str(raw_initial_id))
        except ValueError:
            initial_id = None
        if initial_id is not None:
            candidate = db.get(InboxMessage, initial_id)
            if (
                candidate is not None
                and candidate.conversation_id == conversation.id
                and candidate.direction == InboxMessageDirection.inbound.value
            ):
                initial = candidate
                if not metadata.get(f"processed_inbound:{candidate.id}"):
                    return candidate
    lower_bound = (
        initial.created_at
        if initial is not None
        else session.created_at - timedelta(minutes=5)
    )
    rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .filter(InboxMessage.created_at >= lower_bound)
        .order_by(InboxMessage.created_at.asc(), InboxMessage.id.asc())
        .all()
    )
    return next(
        (row for row in rows if not metadata.get(f"processed_inbound:{row.id}")),
        None,
    )


def _process_one_session(
    db: Session, *, session: AiIntakeSession, conversation: InboxConversation
) -> bool:
    turn_started = time.perf_counter()
    from app.schemas.ai_intake import AiIntakeRequest
    from app.services import (
        ai_intake,
        team_inbox_assignment,
        team_inbox_outbound,
        team_inbox_routing,
    )

    inbound = _next_unprocessed_inbound(
        db,
        session=session,
        conversation=conversation,
    )
    if inbound is None or not inbound.body:
        return False
    version = (
        db.get(AiIntakePolicyVersion, session.policy_version_id)
        if session.policy_version_id
        else None
    )
    if session.state == "welcome_pending":
        if version is None or not version.welcome_message:
            session.state = "collecting_intent"
        else:
            welcome_body = version.welcome_message
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
            logger.info(
                "ai intake welcome delivery resolved",
                extra={
                    "event": "ai_intake_welcome_delivery_resolved",
                    "conversation_id": str(conversation.id),
                    "session_id": str(session.id),
                    "inbound_message_id": str(inbound.id),
                    "delivery_kind": delivery.kind,
                    "delivery_reason": delivery.reason,
                    "outbound_message_id": delivery.message_id,
                },
            )
            if delivery.kind != "queued":
                session.state = "failed"
                complete_session(session, state="failed")
                mark_conversation_ai_metadata(
                    conversation, session=session, active=False
                )
                return True
            session.state = "collecting_intent"
        mark_conversation_ai_metadata(conversation, session=session, active=True)
        logger.info(
            "ai intake initial inbound selected as turn one",
            extra={
                "event": "ai_intake_initial_turn_selected",
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "inbound_message_id": str(inbound.id),
                "session_state": session.state,
            },
        )
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
    session_metadata = dict(session.metadata_ or {})
    session_metadata.update(
        {
            "selected_inbound_message_id": str(inbound.id),
            "selected_inbound_external_id": str(inbound.external_message_id or "")[:255]
            or None,
            "selected_inbound_at": datetime.now(UTC).isoformat(),
        }
    )
    session.metadata_ = session_metadata
    logger.info(
        "ai intake inbound selected for turn",
        extra={
            "event": "ai_intake_inbound_selected",
            "conversation_id": str(conversation.id),
            "session_id": str(session.id),
            "inbound_message_id": str(inbound.id),
            "external_message_id": str(inbound.external_message_id or "") or None,
            "session_state": session.state,
        },
    )
    was_awaiting_customer = session.state == "awaiting_customer"
    if was_awaiting_customer:
        clear_customer_wait(session, reason="customer_response")
        session.state = "collecting_intent"
        mark_conversation_ai_metadata(conversation, session=session, active=True)
        logger.info(
            "ai intake customer response resumed session",
            extra={
                "event": "ai_intake_customer_response_resumed",
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "inbound_message_id": str(inbound.id),
            },
        )
    recent_rows = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id == conversation.id)
        .filter(
            or_(
                InboxMessage.created_at < inbound.created_at,
                and_(
                    InboxMessage.created_at == inbound.created_at,
                    InboxMessage.id <= inbound.id,
                ),
            )
        )
        .order_by(InboxMessage.created_at.desc(), InboxMessage.id.desc())
        .limit(ai_intake.MAX_RECENT_MESSAGES)
        .all()
    )
    recent_items: list[AiIntakeContextMessage] = []
    for row in reversed(recent_rows):
        body = str(row.body or "").strip()
        if not body or row.direction == InboxMessageDirection.internal.value:
            continue
        if row.direction == InboxMessageDirection.inbound.value:
            role = AiIntakeMessageRole.customer
        else:
            row_metadata = dict(row.metadata_ or {})
            if (
                str(row_metadata.get("sender_type") or "").lower() == "ai"
                or str(row_metadata.get("author_type") or "").lower() == "ai"
            ):
                role = AiIntakeMessageRole.ai
            elif row_metadata.get("sent_by_person_id"):
                role = AiIntakeMessageRole.human_agent
            else:
                continue
        recent_items.append(
            AiIntakeContextMessage(
                role=role,
                body=body[: ai_intake.MAX_CONTEXT_CHARS],
            )
        )
    recent = tuple(recent_items)
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
        active_ai_session=True,
        has_active_assignment=False,
        awaiting_follow_up=was_awaiting_customer,
        follow_up_count=session.turn_count,
    )
    outcome = ai_intake.classify_message(db, request)
    engine_enabled = _session_engine_enabled(db, session=session, version=version)
    metadata.update(ai_intake.route_metadata(outcome))
    if outcome.status == AiIntakeStatus.awaiting_follow_up and not engine_enabled:
        metadata.setdefault("ai_intake_engine_action", "legacy_clarification")
        metadata.setdefault(
            "ai_intake_engine_reason", "legacy_classifier_clarification"
        )
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
    session_metadata["last_generation_attempt_id"] = str(generation.id)
    session.metadata_ = session_metadata
    engine_forced_handoff = False
    engine_handoff_state: ai_intake_conversation_engine.ConversationalState | None = (
        None
    )
    if outcome.status == AiIntakeStatus.awaiting_follow_up and not engine_enabled:
        delivery_question = " ".join(
            str(
                metadata.get("ai_intake_follow_up_question")
                or (
                    outcome.classification.follow_up_question
                    if outcome.classification is not None
                    else ""
                )
            ).split()
        )
        if not delivery_question:
            delivery_question = DEFAULT_CLARIFICATION_QUESTIONS[0]
        metadata["ai_intake_follow_up_question"] = delivery_question
        inbound.metadata_ = metadata
        delivery = team_inbox_outbound.send_ai_intake_follow_up(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.AiIntakeFollowUpPayload(
                question=delivery_question,
                inbound_message_id=inbound.id,
                config_id=outcome.config_id,
                follow_up_count=outcome.follow_up_count,
                session_id=session.id,
                policy_id=session.policy_id,
                policy_version_id=session.policy_version_id,
                display_name=session.display_name,
            ),
        )
        logger.info(
            "ai intake follow-up delivery resolved",
            extra={
                "event": "ai_intake_follow_up_delivery_resolved",
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "inbound_message_id": str(inbound.id),
                "delivery_kind": delivery.kind,
                "delivery_reason": delivery.reason,
                "outbound_message_id": delivery.message_id,
                "notification_id": str(delivery.notification_id)
                if delivery.notification_id is not None
                else None,
            },
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
        if delivery.kind == "queued":
            session.state = "awaiting_customer"
            record_customer_wait(
                session,
                version=version,
                inbound_message_id=inbound.id,
                reason="awaiting_customer",
            )
            transition_conversation_status(
                db,
                conversation=conversation,
                status=InboxConversationStatus.pending,
                reason=team_inbox_status.InboxStatusReason.ai_awaiting_clarification,
                source_id=f"ai-intake-awaiting-clarification:{session.id}:{inbound.id}",
            )
            mark_conversation_ai_metadata(conversation, session=session, active=True)
            mark_inbound_processed(
                session,
                inbound_message_id=inbound.id,
                generation_attempt_id=generation.id,
            )
            return True
        metadata["ai_intake_engine_action"] = "handoff"
        metadata["ai_intake_engine_reason"] = "follow_up_delivery_failed"
        metadata["ai_intake_status"] = "escalated"
        metadata["ai_intake_escalation_reason"] = "follow_up_delivery_failed"
        inbound.metadata_ = metadata
        logger.warning(
            "AI intake follow-up delivery failed; escalating",
            extra={
                "event": "ai_intake_follow_up_delivery_failed",
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "inbound_message_id": str(inbound.id),
                "delivery_kind": delivery.kind,
                "delivery_reason": delivery.reason,
            },
        )
        engine_forced_handoff = True
        engine_handoff_state = ai_intake_conversation_engine.ConversationalState.load(
            conversation=conversation,
            session=session,
        )
        engine_handoff_state.escalation_reason = "follow_up_delivery_failed"
        engine_handoff_state.handoff_status = "requested"
        if outcome.classification is not None:
            engine_handoff_state.current_intent = outcome.classification.intent.value
            engine_handoff_state.category = outcome.classification.category.value
            engine_handoff_state.confidence = outcome.classification.confidence
        ai_intake_conversation_engine.persist_state(session, engine_handoff_state)

    if engine_enabled and not engine_forced_handoff:
        engine_name = "composable_v1"
        if ai_intake_graph.langgraph_engine_enabled(version):
            requested_engine_name = ai_intake_graph.LANGGRAPH_ENGINE_MODE
            try:
                decision = ai_intake_graph.run_ai_intake_graph(
                    db,
                    conversation=conversation,
                    session=session,
                    version=version,
                    latest_body=str(inbound.body or ""),
                    classification=outcome.classification,
                    recent_messages=recent,
                )
                engine_name = ai_intake_graph.LANGGRAPH_ENGINE_MODE
            except ai_intake_graph.LangGraphUnavailableError as exc:
                logger.warning(
                    "ai_intake_langgraph_unavailable_falling_back",
                    extra={
                        "event": "ai_intake_langgraph_unavailable_falling_back",
                        "conversation_id": str(conversation.id),
                        "session_id": str(session.id),
                        "policy_version_id": str(version.id)
                        if version is not None
                        else None,
                        "requested_engine": requested_engine_name,
                        "fallback_engine": "composable_v1",
                        "error_type": type(exc).__name__,
                    },
                )
                decision = ai_intake_conversation_engine.run_conversational_turn(
                    db,
                    conversation=conversation,
                    session=session,
                    version=version,
                    latest_body=str(inbound.body or ""),
                    classification=outcome.classification,
                )
                engine_name = "composable_v1_fallback"
        else:
            requested_engine_name = "composable_v1"
            decision = ai_intake_conversation_engine.run_conversational_turn(
                db,
                conversation=conversation,
                session=session,
                version=version,
                latest_body=str(inbound.body or ""),
                classification=outcome.classification,
            )
        ai_intake_conversation_engine.persist_state(session, decision.state)
        metadata["ai_intake_engine_requested"] = requested_engine_name
        metadata["ai_intake_engine"] = engine_name
        metadata["ai_intake_engine_action"] = decision.action
        metadata["ai_intake_engine_reason"] = decision.metadata.get("reason")
        raw_node_trace = decision.metadata.get("node_trace")
        node_trace = (
            [str(item) for item in raw_node_trace if str(item)][-40:]
            if isinstance(raw_node_trace, list | tuple)
            else []
        )
        metadata["ai_intake_graph_node_trace"] = node_trace
        metadata["ai_intake_graph_transition_trace"] = [
            f"{source}->{target}"
            for source, target in zip(node_trace, node_trace[1:], strict=False)
        ]
        metadata["ai_intake_question_key"] = decision.metadata.get("question_key")
        metadata["ai_intake_answer_status"] = decision.metadata.get("answer_status")
        metadata["ai_intake_tool_results"] = [
            {
                "tool": item.get("tool"),
                "status": item.get("status"),
                "latency_ms": item.get("latency_ms"),
            }
            for item in decision.state.tool_executions[-6:]
        ]
        if decision.state.current_intent:
            metadata["ai_intent"] = decision.state.current_intent
        if decision.state.category:
            metadata["ai_category"] = decision.state.category
        if decision.state.confidence is not None:
            metadata["ai_confidence"] = decision.state.confidence
        if decision.state.destination_team_id:
            metadata["ai_department_team_id"] = decision.state.destination_team_id
        inbound.metadata_ = metadata
        conversation_metadata = dict(conversation.metadata_ or {})
        intake_metadata = dict(conversation_metadata.get("ai_intake") or {})
        intake_metadata.update(
            {
                "engine": engine_name,
                "engine_mode": engine_name,
                "requested_engine": requested_engine_name,
                "engine_action": decision.action,
                "engine_reason": decision.metadata.get("reason"),
                "human_requested": decision.state.human_requested,
                "current_intent": decision.state.current_intent,
                "previous_intent": decision.state.previous_intent,
                "category": decision.state.category,
                "subscriber_id": decision.state.subscriber_id,
                "missing_facts": decision.state.missing_facts,
                "escalation_reason": decision.state.escalation_reason,
            }
        )
        conversation_metadata["ai_intake"] = intake_metadata
        conversation.metadata_ = conversation_metadata
        composition_attempt: AiIntakeGenerationAttempt | None = None
        composition = None
        if (
            decision.action in {"respond", "handoff", "resolved"}
            and decision.response_text
        ):
            composition = ai_intake.compose_customer_response(
                db,
                request=_composition_request(
                    version=version,
                    inbound=inbound,
                    recent=recent,
                    decision=decision,
                ),
                fallback_text=decision.response_text,
                fallback_source=str(
                    decision.metadata.get("response_source") or "template"
                ),
            )
            decision = ai_intake_conversation_engine.ConversationEngineDecision(
                action=decision.action,
                state=decision.state,
                response_text=composition.response_text,
                handoff_summary=decision.handoff_summary,
                metadata={
                    **decision.metadata,
                    "response_source": composition.response_source,
                    "composition_template_version": (
                        ai_intake.RESPONSE_COMPOSITION_VERSION
                    ),
                    "composition_safety_reason": composition.safety_reason,
                },
            )
            if composition.acknowledges_issue:
                decision.state.acknowledged_issue_key = (
                    decision.state.category or decision.state.current_intent
                )
            decision.state.last_response_source = composition.response_source
            ai_intake_conversation_engine.persist_state(session, decision.state)
            composition_attempt = record_generation_attempt(
                db,
                session=session,
                purpose="response_composition",
                status=(
                    "generated"
                    if composition.response_source == "model"
                    else "safe_fallback"
                ),
                inbound_message_id=inbound.id,
                provider=composition.provider,
                model=composition.model,
                duration_ms=composition.duration_ms,
                error_code=composition.safety_reason,
                metadata={
                    "endpoint": composition.endpoint,
                    "fallback_used": composition.fallback_used,
                    "response_source": composition.response_source,
                    "template_version": ai_intake.RESPONSE_COMPOSITION_VERSION,
                    "question_key": decision.metadata.get("question_key"),
                    "tokens_in": composition.tokens_in,
                    "tokens_out": composition.tokens_out,
                    "app_revision": get_app_revision(),
                },
            )
            metadata["ai_intake_response_source"] = composition.response_source
            metadata["ai_intake_question_key"] = decision.metadata.get("question_key")
            metadata["ai_intake_composition_template_version"] = (
                ai_intake.RESPONSE_COMPOSITION_VERSION
            )
            metadata["ai_intake_turn_latency_ms"] = max(
                0, int((time.perf_counter() - turn_started) * 1000)
            )
            inbound.metadata_ = metadata
        if decision.action == "respond":
            response_metadata = ai_message_metadata(
                session=session,
                version=version,
                purpose="conversation",
                provider=composition.provider if composition is not None else None,
                model=composition.model if composition is not None else None,
            )
            response_metadata.update(
                {
                    "ai_response_source": (
                        composition.response_source
                        if composition is not None
                        else decision.metadata.get("response_source")
                    ),
                    "ai_question_key": decision.metadata.get("question_key"),
                    "ai_answer_status": decision.metadata.get("answer_status"),
                    "ai_composition_template_version": (
                        ai_intake.RESPONSE_COMPOSITION_VERSION
                    ),
                }
            )
            delivery = team_inbox_outbound.send_ai_intake_message(
                db,
                conversation=conversation,
                body_text=decision.response_text or "",
                metadata=response_metadata,
                dedupe_key=f"ai-intake-conversation:{session.id}:{inbound.id}",
            )
            generation.outbound_message_id = (
                UUID(delivery.message_id) if delivery.message_id else None
            )
            if composition_attempt is not None and delivery.message_id:
                composition_attempt.outbound_message_id = UUID(delivery.message_id)
            logger.info(
                "ai intake response delivery resolved",
                extra={
                    "event": "ai_intake_response_delivery_resolved",
                    "conversation_id": str(conversation.id),
                    "session_id": str(session.id),
                    "inbound_message_id": str(inbound.id),
                    "delivery_kind": delivery.kind,
                    "delivery_reason": delivery.reason,
                    "outbound_message_id": delivery.message_id,
                },
            )
            conversation_metadata = dict(conversation.metadata_ or {})
            intake_metadata = dict(conversation_metadata.get("ai_intake") or {})
            intake_metadata["response_delivery_status"] = delivery.kind
            intake_metadata["response_delivery_reason"] = delivery.reason
            conversation_metadata["ai_intake"] = intake_metadata
            conversation.metadata_ = conversation_metadata
            if delivery.kind == "queued":
                session.state = "awaiting_customer"
                record_customer_wait(
                    session,
                    version=version,
                    inbound_message_id=inbound.id,
                    reason="conversation_response",
                )
                transition_conversation_status(
                    db,
                    conversation=conversation,
                    status=InboxConversationStatus.pending,
                    reason=team_inbox_status.InboxStatusReason.ai_awaiting_clarification,
                    source_id=f"ai-intake-conversation:{session.id}:{inbound.id}",
                )
                mark_conversation_ai_metadata(
                    conversation, session=session, active=True
                )
                mark_inbound_processed(
                    session,
                    inbound_message_id=inbound.id,
                    generation_attempt_id=generation.id,
                )
                return True
            metadata["ai_intake_engine_action"] = "handoff"
            metadata["ai_intake_engine_reason"] = "response_delivery_failed"
            metadata["ai_intake_status"] = "escalated"
            metadata["ai_intake_escalation_reason"] = "response_delivery_failed"
            inbound.metadata_ = metadata
            logger.warning(
                "AI intake response delivery failed; escalating",
                extra={
                    "event": "ai_intake_response_delivery_failed",
                    "conversation_id": str(conversation.id),
                    "session_id": str(session.id),
                    "inbound_message_id": str(inbound.id),
                    "delivery_kind": delivery.kind,
                    "delivery_reason": delivery.reason,
                },
            )
            engine_forced_handoff = True
            engine_handoff_state = decision.state
            engine_handoff_state.escalation_reason = "response_delivery_failed"
            engine_handoff_state.handoff_status = "requested"
        if decision.action == "resolved":
            resolution_metadata = ai_message_metadata(
                session=session,
                version=version,
                purpose="resolution",
                provider=composition.provider if composition is not None else None,
                model=composition.model if composition is not None else None,
            )
            resolution_metadata["ai_response_source"] = (
                composition.response_source
                if composition is not None
                else decision.metadata.get("response_source")
            )
            resolution_metadata["ai_composition_template_version"] = (
                ai_intake.RESPONSE_COMPOSITION_VERSION
            )
            delivery = team_inbox_outbound.send_ai_intake_message(
                db,
                conversation=conversation,
                body_text=decision.response_text or "",
                metadata=resolution_metadata,
                dedupe_key=f"ai-intake-resolution:{session.id}:{inbound.id}",
            )
            if delivery.kind != "queued":
                engine_forced_handoff = True
                engine_handoff_state = decision.state
                engine_handoff_state.escalation_reason = "resolution_delivery_failed"
                engine_handoff_state.handoff_status = "requested"
            else:
                outbound_id = UUID(delivery.message_id) if delivery.message_id else None
                generation.outbound_message_id = outbound_id
                if composition_attempt is not None:
                    composition_attempt.outbound_message_id = outbound_id
                session_metadata = dict(session.metadata_ or {})
                session_metadata.update(
                    {
                        "resolution_reason": decision.metadata.get("reason"),
                        "resolved_inbound_message_id": str(inbound.id),
                    }
                )
                session.metadata_ = session_metadata
                complete_session(session, state="resolved")
                transition_conversation_status(
                    db,
                    conversation=conversation,
                    status=InboxConversationStatus.resolved,
                    reason=team_inbox_status.InboxStatusReason.ai_intake_resolved,
                    source_id=f"ai-intake-resolved:{session.id}:{inbound.id}",
                )
                mark_conversation_ai_metadata(
                    conversation, session=session, active=False
                )
                mark_inbound_processed(
                    session,
                    inbound_message_id=inbound.id,
                    generation_attempt_id=(
                        composition_attempt.id
                        if composition_attempt is not None
                        else generation.id
                    ),
                )
                return True
        if decision.action == "handoff":
            metadata["ai_intake_status"] = "classified"
            inbound.metadata_ = metadata
            if decision.response_text:
                handoff_metadata = ai_message_metadata(
                    session=session,
                    version=version,
                    purpose="handoff",
                    provider=(
                        composition.provider if composition is not None else None
                    ),
                    model=composition.model if composition is not None else None,
                )
                handoff_metadata.update(
                    {
                        "ai_response_source": (
                            composition.response_source
                            if composition is not None
                            else decision.metadata.get("response_source")
                        ),
                        "ai_composition_template_version": (
                            ai_intake.RESPONSE_COMPOSITION_VERSION
                        ),
                    }
                )
                team_inbox_outbound.send_ai_intake_message(
                    db,
                    conversation=conversation,
                    body_text=decision.response_text,
                    metadata=handoff_metadata,
                    dedupe_key=f"ai-intake-handoff-message:{session.id}:{inbound.id}",
                )
            engine_forced_handoff = True
            engine_handoff_state = decision.state
        elif not engine_forced_handoff:
            metadata["ai_intake_engine_action"] = "handoff"
            metadata["ai_intake_engine_reason"] = "unsupported_engine_action"
            inbound.metadata_ = metadata
            decision.state.escalation_reason = "unsupported_engine_action"
            decision.state.handoff_status = "requested"
            engine_forced_handoff = True
            engine_handoff_state = decision.state
    follow_up_question = (
        outcome.classification.follow_up_question
        if outcome.classification is not None
        else None
    )
    if outcome.classification is not None and outcome.classification.requires_follow_up:
        follow_up_question = " ".join(str(follow_up_question or "").split())
        if not follow_up_question:
            follow_up_question = DEFAULT_CLARIFICATION_QUESTIONS[0]
        metadata["ai_intake_follow_up_question"] = follow_up_question
        inbound.metadata_ = metadata
    should_deliver_follow_up = (
        not engine_forced_handoff
        and outcome.status == AiIntakeStatus.awaiting_follow_up
    )
    if should_deliver_follow_up:
        delivery_question = " ".join(
            str(
                metadata.get("ai_intake_follow_up_question") or follow_up_question or ""
            ).split()
        )
        if not delivery_question:
            delivery_question = DEFAULT_CLARIFICATION_QUESTIONS[0]
        metadata.setdefault("ai_intake_engine_action", "legacy_clarification")
        metadata.setdefault(
            "ai_intake_engine_reason", "legacy_classifier_clarification"
        )
        inbound.metadata_ = metadata
        delivery = team_inbox_outbound.send_ai_intake_follow_up(
            db,
            conversation=conversation,
            payload=team_inbox_outbound.AiIntakeFollowUpPayload(
                question=delivery_question,
                inbound_message_id=inbound.id,
                config_id=outcome.config_id,
                follow_up_count=outcome.follow_up_count,
                session_id=session.id,
                policy_id=session.policy_id,
                policy_version_id=session.policy_version_id,
                display_name=session.display_name,
            ),
        )
        logger.info(
            "ai intake follow-up delivery resolved",
            extra={
                "event": "ai_intake_follow_up_delivery_resolved",
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "inbound_message_id": str(inbound.id),
                "delivery_kind": delivery.kind,
                "delivery_reason": delivery.reason,
                "outbound_message_id": delivery.message_id,
                "notification_id": str(delivery.notification_id)
                if delivery.notification_id is not None
                else None,
            },
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
        if delivery.kind == "queued":
            session.state = "awaiting_customer"
            record_customer_wait(
                session,
                version=version,
                inbound_message_id=inbound.id,
                reason="awaiting_customer",
            )
            transition_conversation_status(
                db,
                conversation=conversation,
                status=InboxConversationStatus.pending,
                reason=team_inbox_status.InboxStatusReason.ai_awaiting_clarification,
                source_id=f"ai-intake-awaiting-clarification:{session.id}:{inbound.id}",
            )
            mark_conversation_ai_metadata(conversation, session=session, active=True)
            mark_inbound_processed(
                session,
                inbound_message_id=inbound.id,
                generation_attempt_id=generation.id,
            )
            return True
        metadata["ai_intake_engine_action"] = "handoff"
        metadata["ai_intake_engine_reason"] = "follow_up_delivery_failed"
        metadata["ai_intake_status"] = "escalated"
        metadata["ai_intake_escalation_reason"] = "follow_up_delivery_failed"
        inbound.metadata_ = metadata
        logger.warning(
            "AI intake follow-up delivery failed; escalating",
            extra={
                "event": "ai_intake_follow_up_delivery_failed",
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "inbound_message_id": str(inbound.id),
                "delivery_kind": delivery.kind,
                "delivery_reason": delivery.reason,
            },
        )
        engine_forced_handoff = True
        engine_handoff_state = ai_intake_conversation_engine.ConversationalState.load(
            conversation=conversation,
            session=session,
        )
        engine_handoff_state.escalation_reason = "follow_up_delivery_failed"
        engine_handoff_state.handoff_status = "requested"
        if outcome.classification is not None:
            engine_handoff_state.current_intent = outcome.classification.intent.value
            engine_handoff_state.category = outcome.classification.category.value
            engine_handoff_state.confidence = outcome.classification.confidence
        ai_intake_conversation_engine.persist_state(session, engine_handoff_state)
    if has_human_takeover(db, conversation):
        logger.info(
            "ai intake stopped by human takeover",
            extra={
                "event": "ai_intake_human_takeover_detected",
                "conversation_id": str(conversation.id),
                "session_id": str(session.id),
                "inbound_message_id": str(inbound.id),
            },
        )
        complete_session(session, state="stopped_human_takeover")
        mark_conversation_ai_metadata(conversation, session=session, active=False)
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
    destination_team_name = None
    if routing.primary_service_team_id:
        destination_team = db.get(
            ServiceTeam,
            UUID(str(routing.primary_service_team_id)),
        )
        destination_team_name = (
            destination_team.name if destination_team is not None else None
        )
    if engine_handoff_state is not None:
        engine_handoff_state.destination_team_id = routing.primary_service_team_id
        ai_intake_conversation_engine.persist_state(session, engine_handoff_state)
        note_key = f"handoff_note_created:{inbound.id}"
        latest_session_metadata = dict(session.metadata_ or {})
        if not latest_session_metadata.get(note_key):
            note = team_inbox_operations.create_internal_note(
                db,
                conversation=conversation,
                body=ai_intake_conversation_engine.render_handoff_summary(
                    engine_handoff_state,
                    version=version,
                    channel=conversation.channel_type,
                    destination_team_name=destination_team_name,
                ),
                actor_person_id=None,
                metadata={
                    "source": "ai_intake_handoff",
                    "ai_intake_session_id": str(session.id),
                    "ai_intake_policy_version_id": str(version.id)
                    if version is not None
                    else None,
                    "destination_team_id": routing.primary_service_team_id,
                    "routing_reason": routing.reason,
                },
            )
            latest_session_metadata[note_key] = str(note.id)
            session.metadata_ = latest_session_metadata
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
    mark_inbound_processed(
        session,
        inbound_message_id=inbound.id,
        generation_attempt_id=generation.id,
    )
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
    from app.services import subscriber_profile_cleanup

    policy = _data_cleanup_policy(version)
    if not policy.get("production_collection_enabled"):
        return False
    if conversation.subscriber_id is None:
        return False
    eligibility = subscriber_profile_cleanup.resolve_profile_cleanup_eligibility(
        db,
        subscriber_profile_cleanup.ProfileCleanupEligibilityQuery(
            subscriber_id=conversation.subscriber_id
        ),
    )
    if (
        eligibility.status
        is not subscriber_profile_cleanup.ProfileCleanupEligibilityStatus.eligible
    ):
        return False
    missing = eligibility.missing_fields
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
                subscriber_id=eligibility.subscriber_id,
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
