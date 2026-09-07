"""Typed, data-driven canary execution for conversational AI Intake.

The runner is intentionally simulation-only: it builds isolated evidence from a
scenario definition and the selected policy snapshot, then evaluates a bounded
assertion registry. It never sends provider messages, creates Team Inbox
assignments, writes queue entries, writes customer notes, or mutates customer
records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.ai_intake import AiIntakePolicyVersion, AiIntakeSession
from app.models.team_inbox import InboxConversation
from app.schemas.ai_intake import (
    AiIntakeCategory,
    AiIntakeClassification,
    AiIntakeIntent,
)
from app.services import ai_intake_conversation_engine, ai_intake_graph
from app.services.ai_intake_text import usable_customer_text

JsonScalar = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class _CanaryConversationStub:
    id: UUID
    channel_type: str
    provider: str
    external_thread_id: str
    subject: str
    status: str
    metadata_: dict[str, JsonScalar]


class CanaryEngineMode(StrEnum):
    custom_v1 = "custom_v1"
    langgraph_v1 = "langgraph_v1"


class CanaryChannel(StrEnum):
    whatsapp = "whatsapp"
    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"
    chat_widget = "chat_widget"


class CanaryEventType(StrEnum):
    customer_message = "customer_message"
    customer_media = "customer_media"
    human_request = "human_request"
    human_reply = "human_reply"
    agent_assignment = "agent_assignment"
    queue_capacity_change = "queue_capacity_change"


class CanaryMediaType(StrEnum):
    image = "image"
    audio = "audio"
    document = "document"
    video = "video"
    location = "location"
    sticker = "sticker"


class CanaryToolName(StrEnum):
    customer_lookup = "customer_lookup"
    subscriber_monitoring = "subscriber_monitoring"


class CanaryToolStatus(StrEnum):
    available = "available"
    no_data = "no_data"
    unavailable = "unavailable"
    unauthorized = "unauthorized"
    failure = "failure"


class CanaryCompletionMode(StrEnum):
    ai_continued = "ai_continued"
    awaiting_customer = "awaiting_customer"
    handoff = "handoff"
    stopped_human_takeover = "stopped_human_takeover"
    fallback = "fallback"
    failed = "failed"


class CanaryAssertionType(StrEnum):
    engine_equals = "engine_equals"
    intent_equals = "intent_equals"
    intent_changed = "intent_changed"
    fact_present = "fact_present"
    fact_equals = "fact_equals"
    field_not_requested_again = "field_not_requested_again"
    tool_called = "tool_called"
    tool_not_called = "tool_not_called"
    tool_call_count = "tool_call_count"
    handoff_required = "handoff_required"
    handoff_not_required = "handoff_not_required"
    private_note_created = "private_note_created"
    private_note_not_customer_visible = "private_note_not_customer_visible"
    max_outbound_count = "max_outbound_count"
    no_duplicate_outbound = "no_duplicate_outbound"
    response_contains = "response_contains"
    response_not_contains = "response_not_contains"
    response_does_not_contain_internal_terms = (
        "response_does_not_contain_internal_terms"
    )
    response_uses_configured_support_identity = (
        "response_uses_configured_support_identity"
    )
    no_repeated_introduction = "no_repeated_introduction"
    no_repeated_question = "no_repeated_question"
    queue_owned_by_team_inbox = "queue_owned_by_team_inbox"
    queue_position_not_generated_by_ai = "queue_position_not_generated_by_ai"
    ai_stopped_after_human_ownership = "ai_stopped_after_human_ownership"
    stale_queue_notices_suppressed = "stale_queue_notices_suppressed"
    policy_version_equals = "policy_version_equals"
    monitoring_status_equals = "monitoring_status_equals"
    monitoring_provenance_equals = "monitoring_provenance_equals"
    customer_identity_status_equals = "customer_identity_status_equals"
    media_handoff_occurred = "media_handoff_occurred"
    media_not_interpreted = "media_not_interpreted"
    actual_engine_equals_requested_engine = "actual_engine_equals_requested_engine"
    fallback_observed = "fallback_observed"
    no_robotic_internal_wording = "no_robotic_internal_wording"
    no_unnecessary_generic_welcome = "no_unnecessary_generic_welcome"
    context_aware_first_response = "context_aware_first_response"
    no_repeated_ai_disclosure = "no_repeated_ai_disclosure"
    no_human_impersonation = "no_human_impersonation"
    truthful_automation_answer = "truthful_automation_answer"
    natural_handoff_transition = "natural_handoff_transition"
    no_queue_message_competition = "no_queue_message_competition"


SUPPORTED_ASSERTION_TYPES = frozenset(item.value for item in CanaryAssertionType)
READ_ONLY_TOOL_NAMES = frozenset(item.value for item in CanaryToolName)
INTERNAL_RESPONSE_TERMS = frozenset(
    {
        "intent detected",
        "classification complete",
        "handoff condition",
        "routing decision",
        "confidence score",
        "langgraph",
        "queue worker",
        "escalation rule",
    }
)
HUMAN_IMPERSONATION_TERMS = frozenset(
    {
        "i am a human",
        "i'm a human",
        "this is john",
        "human agent here",
        "not automated",
    }
)
AI_DISCLOSURE_TERMS = frozenset(
    {"i am an ai", "i'm an ai", "i am a bot", "i'm a bot", "automated assistant"}
)


class CanaryCustomerFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    value: JsonScalar
    provenance: str = Field(default="customer_message", max_length=120)


class CanaryInboundTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: Annotated[int, Field(ge=1)]
    event_type: CanaryEventType = CanaryEventType.customer_message
    text: str | None = Field(default=None, max_length=4000)
    media_type: CanaryMediaType | None = None
    media_attached: bool = False
    usable_text_override_for_simulation: bool | None = None
    delay_ms: Annotated[int, Field(ge=0, le=3_600_000)] = 0
    burst_group: str | None = Field(default=None, max_length=80)
    customer_facts: tuple[CanaryCustomerFact, ...] = ()

    @model_validator(mode="after")
    def validate_media_shape(self) -> CanaryInboundTurn:
        if self.media_type is not None and not self.media_attached:
            raise ValueError("media_type requires media_attached=true")
        if (
            self.event_type is CanaryEventType.customer_media
            and not self.media_attached
        ):
            raise ValueError("customer_media requires media_attached=true")
        return self


class CanaryMonitoringObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=80)
    status: str = Field(min_length=1, max_length=80)
    observed_at: datetime | None = None
    provenance: str = Field(default="simulated_read_only", max_length=120)


class CanaryToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: CanaryToolName
    status: CanaryToolStatus
    customer_identity_status: str | None = Field(default=None, max_length=80)
    monitoring_status: str | None = Field(default=None, max_length=80)
    monitoring_provenance: str | None = Field(default=None, max_length=120)
    radius_observation: CanaryMonitoringObservation | None = None
    ont_observation: CanaryMonitoringObservation | None = None
    fields: dict[str, JsonScalar] = Field(default_factory=dict)


class CanaryAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_type: CanaryAssertionType
    expected: JsonScalar = None
    field: str | None = Field(default=None, max_length=160)
    value: JsonScalar = None
    count: Annotated[int | None, Field(ge=0, le=100)] = None
    max_count: Annotated[int | None, Field(ge=0, le=100)] = None
    text: str | None = Field(default=None, max_length=500)


class CanaryPolicySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: UUID | None = None
    policy_version_id: UUID | None = None
    policy_version_number: int | None = Field(default=None, ge=1)
    requested_engine: CanaryEngineMode = CanaryEngineMode.langgraph_v1
    fallback_engine: CanaryEngineMode = CanaryEngineMode.custom_v1
    support_identity: str = Field(
        default="Dotmac Support", min_length=1, max_length=120
    )
    welcome_message: str = Field(
        default="Welcome to Dotmac Support. How can we help today?",
        min_length=1,
        max_length=800,
    )
    standard_handoff_message: str = Field(
        default=(
            "Thanks, I have the details I need. I am passing this to our support "
            "team so they can take a closer look."
        ),
        min_length=1,
        max_length=800,
    )
    media_handoff_message: str = Field(
        default=(
            "Thanks for sending that. I cannot review attachments here, so I am "
            "passing this to our support team to take a closer look."
        ),
        min_length=1,
        max_length=800,
    )
    intent_aliases: dict[str, str] = Field(default_factory=dict)


class CanaryScenarioDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    required_for_activation: bool = False
    priority: Annotated[int, Field(ge=0, le=100)] = 50
    tags: tuple[str, ...] = ()
    channel: CanaryChannel = CanaryChannel.whatsapp
    provider: str | None = Field(default=None, max_length=80)
    account_scope: str | None = Field(default=None, max_length=160)
    engine_requirement: CanaryEngineMode | None = None
    policy_selection: CanaryPolicySelection = Field(
        default_factory=CanaryPolicySelection
    )
    initial_facts: tuple[CanaryCustomerFact, ...] = ()
    inbound_turns: tuple[CanaryInboundTurn, ...]
    simulated_tool_results: tuple[CanaryToolResult, ...] = ()
    assertions: tuple[CanaryAssertion, ...]
    expected_completion_mode: CanaryCompletionMode = CanaryCompletionMode.ai_continued
    created_by: str = Field(default="seed", max_length=120)
    updated_by: str = Field(default="seed", max_length=120)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision: Annotated[int, Field(ge=1)] = 1

    @model_validator(mode="after")
    def validate_turn_order(self) -> CanaryScenarioDefinition:
        sequences = [turn.sequence for turn in self.inbound_turns]
        if sorted(sequences) != sequences or len(set(sequences)) != len(sequences):
            raise ValueError("inbound_turns must have unique ascending sequences")
        return self


class CanaryRuntimeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    scenario_revision: int
    suite_id: str | None = None
    policy_id: UUID | None
    policy_version_id: UUID | None
    policy_version_number: int | None
    requested_engine: CanaryEngineMode
    actual_engine: CanaryEngineMode
    fallback_observed: bool
    message_turns: tuple[dict[str, JsonScalar], ...]
    extracted_facts: dict[str, JsonScalar]
    intent_transitions: tuple[str, ...]
    tool_calls: tuple[str, ...]
    tool_results: tuple[dict[str, JsonScalar], ...]
    outbound_responses: tuple[str, ...]
    handoff_decision: bool
    private_note_preview: str | None
    private_note_customer_visible: bool
    routing_owner: str
    queue_position_source: str
    human_takeover_state: str
    media_interpreted: bool
    monitoring_status: str | None
    monitoring_provenance: str | None
    customer_identity_status: str | None
    completion_mode: CanaryCompletionMode
    safety: dict[str, bool]


class CanaryAssertionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assertion_type: CanaryAssertionType
    passed: bool
    evidence: dict[str, JsonScalar]


class CanaryRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    scenario_revision: int
    passed: bool
    evidence: CanaryRuntimeEvidence
    assertion_results: tuple[CanaryAssertionResult, ...]
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CanaryInvalidDefinition(ValueError):
    """Raised when a scenario definition contains unsupported unsafe content."""


@dataclass(frozen=True, slots=True)
class ProductionPatternObservation:
    pattern_id: str
    title: str
    example_text: str
    tags: tuple[str, ...] = ()
    media_type: CanaryMediaType | None = None


def langgraph_runtime_available() -> bool:
    return ai_intake_graph.langgraph_available()


def validate_scenario_definition(definition: CanaryScenarioDefinition) -> None:
    for result in definition.simulated_tool_results:
        if result.tool_name.value not in READ_ONLY_TOOL_NAMES:
            raise CanaryInvalidDefinition(f"unsupported tool: {result.tool_name.value}")
    for assertion in definition.assertions:
        if assertion.assertion_type.value not in SUPPORTED_ASSERTION_TYPES:
            raise CanaryInvalidDefinition(
                f"unsupported assertion: {assertion.assertion_type.value}"
            )


def run_canary_scenario(
    definition: CanaryScenarioDefinition,
    *,
    policy_selection: CanaryPolicySelection | None = None,
    db: object | None = None,
    suite_id: str | None = None,
) -> CanaryRunResult:
    validate_scenario_definition(definition)
    policy = policy_selection or definition.policy_selection
    requested_engine = definition.engine_requirement or policy.requested_engine
    actual_engine = requested_engine
    fallback_observed = False
    if (
        requested_engine is CanaryEngineMode.langgraph_v1
        and not langgraph_runtime_available()
    ):
        actual_engine = policy.fallback_engine
        fallback_observed = True

    if actual_engine is CanaryEngineMode.langgraph_v1:
        return _run_langgraph_canary(
            definition,
            policy=policy,
            db=db,
            suite_id=suite_id,
            requested_engine=requested_engine,
        )

    facts: dict[str, JsonScalar] = {
        fact.name: fact.value for fact in definition.initial_facts
    }
    tool_results_by_name = {
        result.tool_name.value: result for result in definition.simulated_tool_results
    }
    tool_calls: list[str] = []
    tool_evidence: list[dict[str, JsonScalar]] = []
    outbound_responses: list[str] = []
    message_turns: list[dict[str, JsonScalar]] = []
    intent_transitions: list[str] = []
    previous_intent: str | None = None
    handoff = False
    private_note_preview: str | None = None
    human_takeover_state = "none"
    media_interpreted = False
    monitoring_status: str | None = None
    monitoring_provenance: str | None = None
    customer_identity_status: str | None = None

    for turn in sorted(definition.inbound_turns, key=lambda item: item.sequence):
        if human_takeover_state == "owned":
            message_turns.append(_turn_evidence(turn, usable_text=None))
            continue
        usable_text = _usable_turn_text(turn)
        message_turns.append(_turn_evidence(turn, usable_text=usable_text))
        for fact in turn.customer_facts:
            facts[fact.name] = fact.value
        _infer_facts_from_text(facts, usable_text)

        if turn.event_type in {
            CanaryEventType.human_reply,
            CanaryEventType.agent_assignment,
        }:
            human_takeover_state = "owned"
            handoff = True
            private_note_preview = private_note_preview or _private_note(facts)
            continue

        if turn.media_attached and usable_text is None:
            handoff = True
            private_note_preview = _private_note(facts)
            _append_response(outbound_responses, policy.media_handoff_message)
            continue

        if turn.event_type is CanaryEventType.queue_capacity_change:
            human_takeover_state = "queued_capacity_changed"
            continue

        if usable_text is None:
            continue

        normalized = usable_text.casefold()
        intent = _infer_intent(normalized, policy)
        if intent != previous_intent:
            intent_transitions.append(intent)
            previous_intent = intent

        identity_fixture_present = any(
            fact.name == "customer_identity_status" for fact in turn.customer_facts
        )
        if (
            "customer_lookup" in tool_results_by_name
            and "customer_lookup" not in tool_calls
            and (_looks_like_identifier(normalized) or identity_fixture_present)
        ):
            result = tool_results_by_name["customer_lookup"]
            tool_calls.append(result.tool_name.value)
            customer_identity_status = (
                result.customer_identity_status or result.status.value
            )
            tool_evidence.append(_tool_result_evidence(result))

        if (
            "subscriber_monitoring" in tool_results_by_name
            and intent == "technical_support"
        ):
            result = tool_results_by_name["subscriber_monitoring"]
            tool_calls.append(result.tool_name.value)
            monitoring_status = result.monitoring_status or result.status.value
            monitoring_provenance = (
                result.monitoring_provenance
                or (
                    result.radius_observation.provenance
                    if result.radius_observation
                    else None
                )
                or result.status.value
            )
            tool_evidence.append(_tool_result_evidence(result))

        if turn.event_type is CanaryEventType.human_request or _asks_for_human(
            normalized
        ):
            handoff = True
            private_note_preview = _private_note(facts)
            _append_response(outbound_responses, policy.standard_handoff_message)
            human_takeover_state = "handoff_requested"
        elif _is_greeting(normalized):
            _append_response(outbound_responses, policy.welcome_message)
        else:
            _append_response(
                outbound_responses,
                _contextual_response(intent, facts, policy, normalized),
            )

    completion_mode = _completion_mode(
        handoff=handoff,
        fallback_observed=fallback_observed,
        human_takeover_state=human_takeover_state,
    )
    evidence = CanaryRuntimeEvidence(
        scenario_id=definition.scenario_id,
        scenario_revision=definition.revision,
        suite_id=suite_id,
        policy_id=policy.policy_id,
        policy_version_id=policy.policy_version_id,
        policy_version_number=policy.policy_version_number,
        requested_engine=requested_engine,
        actual_engine=actual_engine,
        fallback_observed=fallback_observed,
        message_turns=tuple(message_turns),
        extracted_facts=facts,
        intent_transitions=tuple(intent_transitions),
        tool_calls=tuple(tool_calls),
        tool_results=tuple(tool_evidence),
        outbound_responses=tuple(outbound_responses),
        handoff_decision=handoff,
        private_note_preview=private_note_preview,
        private_note_customer_visible=False,
        routing_owner="Team Inbox",
        queue_position_source="Team Inbox",
        human_takeover_state=human_takeover_state,
        media_interpreted=media_interpreted,
        monitoring_status=monitoring_status,
        monitoring_provenance=monitoring_provenance,
        customer_identity_status=customer_identity_status,
        completion_mode=completion_mode,
        safety={
            "sends_real_messages": False,
            "creates_real_assignments": False,
            "creates_queue_entries": False,
            "creates_internal_notes": False,
            "mutates_customers": False,
            "uses_live_monitoring": False,
        },
    )
    assertion_results = tuple(
        evaluate_assertion(assertion, evidence) for assertion in definition.assertions
    )
    return CanaryRunResult(
        scenario_id=definition.scenario_id,
        scenario_revision=definition.revision,
        passed=all(result.passed for result in assertion_results),
        evidence=evidence,
        assertion_results=assertion_results,
    )


def _run_langgraph_canary(
    definition: CanaryScenarioDefinition,
    *,
    policy: CanaryPolicySelection,
    db: object | None,
    suite_id: str | None,
    requested_engine: CanaryEngineMode,
) -> CanaryRunResult:
    conversation_id = uuid4()
    session_id = uuid4()
    policy_id = policy.policy_id or uuid4()
    policy_version_id = policy.policy_version_id or uuid4()
    conversation = _CanaryConversationStub(
        id=conversation_id,
        channel_type=definition.channel.value,
        provider=definition.provider or "canary",
        external_thread_id=f"canary:{definition.scenario_id}",
        subject=definition.name,
        status="pending",
        metadata_={"source": "ai_intake_canary"},
    )
    session = AiIntakeSession(
        id=session_id,
        conversation_id=conversation_id,
        policy_id=policy_id,
        policy_version_id=policy_version_id,
        legacy_config_id=None,
        state="collecting_intent",
        channel_type=definition.channel.value,
        provider=definition.provider or "canary",
        account_scope=definition.account_scope or "simulation",
        display_name=policy.support_identity,
        turn_count=0,
        max_turns=10,
        confidence_threshold=0.75,
        fallback_team_id=None,
        metadata_={},
    )
    version = AiIntakePolicyVersion(
        id=policy_version_id,
        policy_id=policy_id,
        version_number=policy.policy_version_number or 1,
        status="activated",
        is_active=True,
        display_name=policy.support_identity,
        welcome_message=policy.welcome_message,
        metadata_=_langgraph_policy_metadata(policy, definition),
    )

    message_turns: list[dict[str, JsonScalar]] = []
    outbound_responses: list[str] = []
    human_takeover_state = "none"
    media_handoff = False
    for fact in definition.initial_facts:
        _store_initial_fact(session, fact)

    last_decision: ai_intake_conversation_engine.ConversationEngineDecision | None = (
        None
    )
    for turn in sorted(definition.inbound_turns, key=lambda item: item.sequence):
        if human_takeover_state == "owned":
            message_turns.append(_turn_evidence(turn, usable_text=None))
            continue
        usable_text = _usable_turn_text(turn)
        message_turns.append(_turn_evidence(turn, usable_text=usable_text))
        for fact in turn.customer_facts:
            _store_initial_fact(session, fact)
        if turn.event_type in {
            CanaryEventType.human_reply,
            CanaryEventType.agent_assignment,
        }:
            human_takeover_state = "owned"
            break
        if turn.event_type is CanaryEventType.queue_capacity_change:
            human_takeover_state = "queued_capacity_changed"
            continue
        if turn.media_attached and usable_text is None:
            media_handoff = True
            _append_response(outbound_responses, policy.media_handoff_message)
            continue
        if usable_text is None:
            continue
        classification = _classification_for_text(usable_text, policy)
        last_decision = ai_intake_graph.run_ai_intake_graph(
            db,  # type: ignore[arg-type]
            conversation=cast(InboxConversation, conversation),
            session=session,
            version=version,
            latest_body=usable_text,
            classification=classification,
            recent_messages=(),
            tool_mode="simulation",
        )
        ai_intake_conversation_engine.persist_state(session, last_decision.state)
        if last_decision.response_text:
            _append_response(outbound_responses, last_decision.response_text)

    state = (
        last_decision.state
        if last_decision is not None
        else ai_intake_conversation_engine.ConversationalState.load(
            conversation=cast(InboxConversation, conversation),
            session=session,
        )
    )
    tool_evidence = tuple(
        _graph_tool_result_evidence(item) for item in state.tool_executions
    )
    tool_calls = tuple(
        str(item.get("tool"))
        for item in state.tool_executions
        if str(item.get("tool") or "").strip()
    )
    monitoring_result = next(
        (
            item
            for item in reversed(state.tool_executions)
            if item.get("tool") == "subscriber_monitoring"
        ),
        None,
    )
    identity_result = next(
        (
            item
            for item in reversed(state.tool_executions)
            if item.get("tool") == "customer_lookup"
        ),
        None,
    )
    handoff = media_handoff or (
        last_decision is not None and last_decision.action == "handoff"
    )
    private_note_preview = (
        last_decision.handoff_summary if last_decision is not None else None
    )
    if media_handoff and private_note_preview is None:
        private_note_preview = _private_note(_json_scalar_dict(state.collected_facts))
    completion_mode = _completion_mode(
        handoff=handoff,
        fallback_observed=False,
        human_takeover_state=human_takeover_state,
    )
    if human_takeover_state == "owned":
        completion_mode = CanaryCompletionMode.stopped_human_takeover
    evidence = CanaryRuntimeEvidence(
        scenario_id=definition.scenario_id,
        scenario_revision=definition.revision,
        suite_id=suite_id,
        policy_id=policy_id,
        policy_version_id=policy_version_id,
        policy_version_number=version.version_number,
        requested_engine=requested_engine,
        actual_engine=CanaryEngineMode.langgraph_v1,
        fallback_observed=False,
        message_turns=tuple(message_turns),
        extracted_facts=_json_scalar_dict(state.collected_facts),
        intent_transitions=tuple(
            item
            for item in (state.previous_intent, state.current_intent)
            if item is not None
        ),
        tool_calls=tool_calls,
        tool_results=tool_evidence,
        outbound_responses=tuple(outbound_responses),
        handoff_decision=handoff,
        private_note_preview=private_note_preview,
        private_note_customer_visible=False,
        routing_owner="Team Inbox",
        queue_position_source="Team Inbox",
        human_takeover_state=human_takeover_state,
        media_interpreted=False,
        monitoring_status=_tool_status(monitoring_result),
        monitoring_provenance=_tool_provenance(monitoring_result),
        customer_identity_status=_tool_status(identity_result),
        completion_mode=completion_mode,
        safety={
            "sends_real_messages": False,
            "creates_real_assignments": False,
            "creates_queue_entries": False,
            "creates_internal_notes": False,
            "mutates_customers": False,
            "uses_live_monitoring": False,
        },
    )
    assertion_results = tuple(
        evaluate_assertion(assertion, evidence) for assertion in definition.assertions
    )
    return CanaryRunResult(
        scenario_id=definition.scenario_id,
        scenario_revision=definition.revision,
        passed=all(result.passed for result in assertion_results),
        evidence=evidence,
        assertion_results=assertion_results,
    )


def evaluate_assertion(
    assertion: CanaryAssertion, evidence: CanaryRuntimeEvidence
) -> CanaryAssertionResult:
    assertion_type = assertion.assertion_type
    responses = "\n".join(evidence.outbound_responses)
    normalized_responses = responses.casefold()
    passed = False
    observed: JsonScalar = None

    if assertion_type is CanaryAssertionType.engine_equals:
        observed = evidence.actual_engine.value
        passed = observed == assertion.expected
    elif assertion_type is CanaryAssertionType.intent_equals:
        observed = (
            evidence.intent_transitions[-1] if evidence.intent_transitions else None
        )
        passed = observed == assertion.expected
    elif assertion_type is CanaryAssertionType.intent_changed:
        observed = len(set(evidence.intent_transitions))
        passed = observed >= 2
    elif assertion_type is CanaryAssertionType.fact_present:
        observed = assertion.field in evidence.extracted_facts
        passed = bool(observed)
    elif assertion_type is CanaryAssertionType.fact_equals:
        observed = (
            evidence.extracted_facts.get(assertion.field or "")
            if assertion.field
            else None
        )
        passed = observed == assertion.value
    elif assertion_type is CanaryAssertionType.field_not_requested_again:
        observed = assertion.field or assertion.text
        passed = (
            observed is not None
            and str(observed).casefold() not in normalized_responses
        )
    elif assertion_type is CanaryAssertionType.tool_called:
        observed = tuple(evidence.tool_calls).count(str(assertion.expected))
        passed = observed > 0
    elif assertion_type is CanaryAssertionType.tool_not_called:
        observed = tuple(evidence.tool_calls).count(str(assertion.expected))
        passed = observed == 0
    elif assertion_type is CanaryAssertionType.tool_call_count:
        observed = tuple(evidence.tool_calls).count(str(assertion.expected))
        passed = observed == assertion.count
    elif assertion_type is CanaryAssertionType.handoff_required:
        observed = evidence.handoff_decision
        passed = evidence.handoff_decision
    elif assertion_type is CanaryAssertionType.handoff_not_required:
        observed = evidence.handoff_decision
        passed = not evidence.handoff_decision
    elif assertion_type is CanaryAssertionType.private_note_created:
        observed = evidence.private_note_preview is not None
        passed = bool(observed)
    elif assertion_type is CanaryAssertionType.private_note_not_customer_visible:
        observed = evidence.private_note_customer_visible
        passed = not evidence.private_note_customer_visible
    elif assertion_type is CanaryAssertionType.max_outbound_count:
        observed = len(evidence.outbound_responses)
        passed = assertion.max_count is not None and observed <= assertion.max_count
    elif assertion_type is CanaryAssertionType.no_duplicate_outbound:
        observed = len(evidence.outbound_responses)
        passed = len(set(evidence.outbound_responses)) == len(
            evidence.outbound_responses
        )
    elif assertion_type is CanaryAssertionType.response_contains:
        observed = assertion.text or assertion.expected
        passed = (
            observed is not None and str(observed).casefold() in normalized_responses
        )
    elif assertion_type is CanaryAssertionType.response_not_contains:
        observed = assertion.text or assertion.expected
        passed = (
            observed is not None
            and str(observed).casefold() not in normalized_responses
        )
    elif assertion_type in {
        CanaryAssertionType.response_does_not_contain_internal_terms,
        CanaryAssertionType.no_robotic_internal_wording,
    }:
        observed = next(
            (term for term in INTERNAL_RESPONSE_TERMS if term in normalized_responses),
            None,
        )
        passed = observed is None
    elif (
        assertion_type is CanaryAssertionType.response_uses_configured_support_identity
    ):
        observed = evidence.policy_version_number
        passed = (
            "dotmac support" in normalized_responses or not evidence.outbound_responses
        )
    elif assertion_type is CanaryAssertionType.no_repeated_introduction:
        observed = normalized_responses.count("welcome to dotmac support")
        passed = observed <= 1
    elif assertion_type is CanaryAssertionType.no_repeated_question:
        questions = [part.strip() for part in responses.split("?") if part.strip()]
        observed = len(questions)
        passed = len(set(questions)) == len(questions)
    elif assertion_type is CanaryAssertionType.queue_owned_by_team_inbox:
        observed = evidence.routing_owner
        passed = evidence.routing_owner == "Team Inbox"
    elif assertion_type is CanaryAssertionType.queue_position_not_generated_by_ai:
        observed = evidence.queue_position_source
        passed = evidence.queue_position_source == "Team Inbox"
    elif assertion_type is CanaryAssertionType.ai_stopped_after_human_ownership:
        observed = evidence.human_takeover_state
        passed = evidence.human_takeover_state in {
            "owned",
            "handoff_requested",
            "queued_capacity_changed",
        }
    elif assertion_type is CanaryAssertionType.stale_queue_notices_suppressed:
        observed = evidence.human_takeover_state
        passed = evidence.human_takeover_state in {"owned", "queued_capacity_changed"}
    elif assertion_type is CanaryAssertionType.policy_version_equals:
        observed = evidence.policy_version_number
        passed = observed == assertion.expected
    elif assertion_type is CanaryAssertionType.monitoring_status_equals:
        observed = evidence.monitoring_status
        passed = observed == assertion.expected
    elif assertion_type is CanaryAssertionType.monitoring_provenance_equals:
        observed = evidence.monitoring_provenance
        passed = observed == assertion.expected
    elif assertion_type is CanaryAssertionType.customer_identity_status_equals:
        observed = evidence.customer_identity_status
        passed = observed == assertion.expected
    elif assertion_type is CanaryAssertionType.media_handoff_occurred:
        observed = evidence.handoff_decision
        passed = evidence.handoff_decision
    elif assertion_type is CanaryAssertionType.media_not_interpreted:
        observed = evidence.media_interpreted
        passed = not evidence.media_interpreted
    elif assertion_type is CanaryAssertionType.actual_engine_equals_requested_engine:
        observed = evidence.actual_engine.value
        passed = evidence.actual_engine == evidence.requested_engine
    elif assertion_type is CanaryAssertionType.fallback_observed:
        observed = evidence.fallback_observed
        passed = evidence.fallback_observed
    elif assertion_type is CanaryAssertionType.no_unnecessary_generic_welcome:
        first_text = _first_usable_text(evidence).casefold()
        observed = evidence.outbound_responses[0] if evidence.outbound_responses else ""
        passed = not (
            first_text
            and not _is_greeting(first_text)
            and "welcome to dotmac support" in str(observed).casefold()
        )
    elif assertion_type is CanaryAssertionType.context_aware_first_response:
        first_text = _first_usable_text(evidence).casefold()
        observed = evidence.outbound_responses[0] if evidence.outbound_responses else ""
        passed = not first_text or _response_mentions_context(first_text, str(observed))
    elif assertion_type is CanaryAssertionType.no_repeated_ai_disclosure:
        observed = sum(normalized_responses.count(term) for term in AI_DISCLOSURE_TERMS)
        passed = observed <= 1
    elif assertion_type is CanaryAssertionType.no_human_impersonation:
        observed = next(
            (
                term
                for term in HUMAN_IMPERSONATION_TERMS
                if term in normalized_responses
            ),
            None,
        )
        passed = observed is None
    elif assertion_type is CanaryAssertionType.truthful_automation_answer:
        asked = any(
            "are you ai" in str(turn.get("text") or "").casefold()
            or "are you automated" in str(turn.get("text") or "").casefold()
            for turn in evidence.message_turns
        )
        observed = "automated" in normalized_responses or "ai" in normalized_responses
        passed = not asked or observed
    elif assertion_type is CanaryAssertionType.natural_handoff_transition:
        observed = evidence.handoff_decision
        passed = evidence.handoff_decision and not any(
            term in normalized_responses for term in INTERNAL_RESPONSE_TERMS
        )
    elif assertion_type is CanaryAssertionType.no_queue_message_competition:
        observed = "queue" in normalized_responses
        passed = evidence.queue_position_source == "Team Inbox" and not observed

    return CanaryAssertionResult(
        assertion_type=assertion_type,
        passed=passed,
        evidence={"observed": observed, "expected": assertion.expected},
    )


def draft_scenario_from_production_pattern(
    observation: ProductionPatternObservation,
) -> CanaryScenarioDefinition:
    media_attached = observation.media_type is not None
    assertions: tuple[CanaryAssertion, ...] = (
        CanaryAssertion(
            assertion_type=CanaryAssertionType.response_does_not_contain_internal_terms
        ),
        CanaryAssertion(assertion_type=CanaryAssertionType.no_repeated_question),
        CanaryAssertion(assertion_type=CanaryAssertionType.no_duplicate_outbound),
    )
    if media_attached:
        assertions = (
            *assertions,
            CanaryAssertion(assertion_type=CanaryAssertionType.media_not_interpreted),
        )
    return CanaryScenarioDefinition(
        scenario_id=f"draft_{observation.pattern_id}",
        name=observation.title,
        description="Draft generated from read-only production pattern discovery.",
        enabled=False,
        required_for_activation=False,
        priority=40,
        tags=("production-derived", "draft", *observation.tags),
        inbound_turns=(
            CanaryInboundTurn(
                sequence=1,
                event_type=(
                    CanaryEventType.customer_media
                    if media_attached
                    else CanaryEventType.customer_message
                ),
                text=observation.example_text,
                media_attached=media_attached,
                media_type=observation.media_type,
            ),
        ),
        assertions=assertions,
        expected_completion_mode=CanaryCompletionMode.ai_continued,
        created_by="production_pattern_discovery",
        updated_by="production_pattern_discovery",
    )


def _langgraph_policy_metadata(
    policy: CanaryPolicySelection, definition: CanaryScenarioDefinition
) -> dict[str, object]:
    return {
        "conversational_engine_enabled": True,
        "conversation_engine_mode": ai_intake_graph.LANGGRAPH_ENGINE_MODE,
        "conversation_policy": {
            "require_identity_before_tools": False,
            "tools": _enabled_tool_config(definition),
            "simulated_tool_results": _simulated_tool_policy(definition),
            "handoff": {
                "customer_message": policy.standard_handoff_message,
                "summary_template": (
                    "Customer: ${customer}; Issue: ${issue}; Intent: ${intent}; "
                    "Facts: ${collected_facts}; Monitoring: ${monitoring_findings}"
                ),
                "announce_destination": False,
            },
            "troubleshooting_rules": [
                {
                    "condition": {
                        "type": "field_value",
                        "field": "los_red",
                        "value": True,
                    },
                    "action": "handoff",
                    "reason": "red_los",
                    "response": policy.standard_handoff_message,
                }
            ],
        },
        "tools": _enabled_tool_config(definition),
        "permitted_identifiers": ["registered_phone", "registered_email", "portal_id"],
    }


def _enabled_tool_config(definition: CanaryScenarioDefinition) -> dict[str, object]:
    configured = {
        result.tool_name.value for result in definition.simulated_tool_results
    }
    return {
        tool_name.value: {"enabled": tool_name.value in configured}
        for tool_name in CanaryToolName
    }


def _simulated_tool_policy(definition: CanaryScenarioDefinition) -> dict[str, object]:
    return {
        result.tool_name.value: _graph_tool_payload(result)
        for result in definition.simulated_tool_results
    }


def _graph_tool_payload(result: CanaryToolResult) -> dict[str, object]:
    if result.tool_name is CanaryToolName.customer_lookup:
        status = result.customer_identity_status or result.status.value
        if status == "linked_subscriber":
            status = "found"
        payload: dict[str, object] = {
            "status": status,
            "subscriber_id": str(result.fields.get("subscriber_id") or uuid4()),
            "display_name": str(result.fields.get("display_name") or "Canary customer"),
            "account_number": str(result.fields.get("account_number") or "CANARY"),
            "subscriber_status": str(
                result.fields.get("subscriber_status") or "active"
            ),
            "simulated": True,
        }
        return payload
    status = result.monitoring_status or result.status.value
    payload = {
        "status": status,
        "radius_observation": _observation_payload(result.radius_observation),
        "ont_observations": [_observation_payload(result.ont_observation)]
        if result.ont_observation
        else [],
        "provenance": result.monitoring_provenance or "simulated_read_only",
        "simulated": True,
    }
    return payload


def _observation_payload(
    observation: CanaryMonitoringObservation | None,
) -> dict[str, object]:
    if observation is None:
        return {}
    return {
        "source": observation.source,
        "status": observation.status,
        "state": observation.status,
        "provenance": observation.provenance,
        "observed_at": observation.observed_at.isoformat()
        if observation.observed_at
        else None,
    }


def _store_initial_fact(session: AiIntakeSession, fact: CanaryCustomerFact) -> None:
    state = dict(
        (session.metadata_ or {}).get(ai_intake_conversation_engine.STATE_KEY) or {}
    )
    facts = dict(state.get("collected_facts") or {})
    facts[fact.name] = fact.value
    if fact.name == "customer_fact.LOS":
        facts["los_red"] = fact.value == "red"
    state["collected_facts"] = facts
    metadata = dict(session.metadata_ or {})
    metadata[ai_intake_conversation_engine.STATE_KEY] = state
    session.metadata_ = metadata


def _classification_for_text(
    text: str, policy: CanaryPolicySelection
) -> AiIntakeClassification:
    intent_value = _infer_intent(text.casefold(), policy)
    try:
        intent = AiIntakeIntent(intent_value)
    except ValueError:
        intent = AiIntakeIntent.unknown
    category = _category_for_intent(intent, text)
    return AiIntakeClassification(
        intent=intent,
        category=category,
        confidence=0.95,
        requires_follow_up=False,
        follow_up_question=None,
        summary="Canary simulated provider classification.",
    )


def _category_for_intent(intent: AiIntakeIntent, text: str) -> AiIntakeCategory:
    normalized = text.casefold()
    if intent is AiIntakeIntent.technical_support:
        if "slow" in normalized:
            return AiIntakeCategory.slow_internet
        return AiIntakeCategory.no_internet
    if intent is AiIntakeIntent.payment_confirmation:
        return AiIntakeCategory.payment_confirmation
    if intent is AiIntakeIntent.billing_issue:
        return AiIntakeCategory.payment_not_reflected
    if intent is AiIntakeIntent.new_connection:
        return AiIntakeCategory.new_connection
    return AiIntakeCategory.unknown


def _graph_tool_result_evidence(row: dict[str, object]) -> dict[str, JsonScalar]:
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    result_map = result if isinstance(result, dict) else {}
    return {
        "tool_name": str(row.get("tool") or ""),
        "status": str(row.get("status") or result_map.get("status") or ""),
        "customer_identity_status": str(result_map.get("status") or "")
        if row.get("tool") == "customer_lookup"
        else None,
        "monitoring_status": str(result_map.get("status") or "")
        if row.get("tool") == "subscriber_monitoring"
        else None,
        "monitoring_provenance": str(result_map.get("provenance") or "")
        if row.get("tool") == "subscriber_monitoring"
        else None,
    }


def _tool_status(row: dict[str, object] | None) -> str | None:
    if row is None:
        return None
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    result_map = result if isinstance(result, dict) else {}
    status = row.get("status") or result_map.get("status")
    return str(status) if status is not None else None


def _tool_provenance(row: dict[str, object] | None) -> str | None:
    if row is None:
        return None
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    result_map = result if isinstance(result, dict) else {}
    provenance = result_map.get("provenance")
    return str(provenance) if provenance is not None else None


def _json_scalar_dict(values: dict[str, object]) -> dict[str, JsonScalar]:
    return {
        str(key): value
        if isinstance(value, str | int | float | bool) or value is None
        else str(value)
        for key, value in values.items()
    }


def _turn_evidence(
    turn: CanaryInboundTurn, *, usable_text: str | None
) -> dict[str, JsonScalar]:
    return {
        "sequence": turn.sequence,
        "event_type": turn.event_type.value,
        "text": turn.text,
        "usable_text": usable_text,
        "media_attached": turn.media_attached,
        "media_type": turn.media_type.value if turn.media_type else None,
        "delay_ms": turn.delay_ms,
        "burst_group": turn.burst_group,
    }


def _tool_result_evidence(result: CanaryToolResult) -> dict[str, JsonScalar]:
    return {
        "tool_name": result.tool_name.value,
        "status": result.status.value,
        "customer_identity_status": result.customer_identity_status,
        "monitoring_status": result.monitoring_status,
        "monitoring_provenance": result.monitoring_provenance,
    }


def _usable_turn_text(turn: CanaryInboundTurn) -> str | None:
    if turn.usable_text_override_for_simulation is False:
        return None
    if turn.usable_text_override_for_simulation is True:
        return str(turn.text or "").strip() or None
    return usable_customer_text(turn.text)


def _infer_facts_from_text(facts: dict[str, JsonScalar], text: str | None) -> None:
    if text is None:
        return
    normalized = text.casefold()
    if "los is red" in normalized or "los light" in normalized and "red" in normalized:
        facts.setdefault("customer_fact.LOS", "red")
    if "restart" in normalized:
        facts.setdefault("customer_fact.restarted_router", True)
    if "router is powered" in normalized or "router is on" in normalized:
        facts.setdefault("customer_fact.router_powered", True)
    if _looks_like_identifier(normalized):
        facts.setdefault("customer_fact.identifier_supplied", True)


def _infer_intent(text: str, policy: CanaryPolicySelection) -> str:
    if any(term in text for term in ("payment", "paid", "renewed", "activated")):
        intent = (
            "payment_confirmation"
            if "paid" in text or "payment" in text
            else "billing_issue"
        )
    elif any(term in text for term in ("billing", "invoice", "subscription")):
        intent = "billing_issue"
    elif any(term in text for term in ("new installation", "new connection")):
        intent = "new_connection"
    elif any(term in text for term in ("internet", "brows", "slow", "los", "router")):
        intent = "technical_support"
    elif _asks_for_human(text):
        intent = "general_enquiry"
    else:
        intent = "unknown"
    return policy.intent_aliases.get(intent, intent)


def _contextual_response(
    intent: str,
    facts: dict[str, JsonScalar],
    policy: CanaryPolicySelection,
    latest_text: str,
) -> str:
    if intent == "technical_support":
        if "slow" in latest_text:
            return "Thanks for clarifying. Is it slow on all devices?"
        if facts.get("customer_fact.LOS") == "red":
            return "I have the red LOS detail. I will pass this to Dotmac Support."
        if facts.get("customer_fact.restarted_router"):
            return (
                "I have that you already restarted the router. Is the LOS light red "
                "or stable now?"
            )
        return "Sorry about the browsing issue. Is the LOS light on your router red?"
    if intent in {"billing_issue", "payment_confirmation"}:
        return "I understand this is about your payment or activation. I will get the billing team to check it."
    if intent == "new_connection":
        return "Thanks. Please share the service location so Dotmac Support can check coverage."
    return policy.welcome_message


def _append_response(responses: list[str], response: str) -> None:
    if response not in responses:
        responses.append(response)


def _private_note(facts: dict[str, JsonScalar]) -> str:
    if not facts:
        return "AI intake handoff summary: customer requested support."
    parts = [f"{key}={value}" for key, value in sorted(facts.items())]
    return "AI intake handoff summary: " + "; ".join(parts)


def _completion_mode(
    *,
    handoff: bool,
    fallback_observed: bool,
    human_takeover_state: str,
) -> CanaryCompletionMode:
    if fallback_observed:
        return CanaryCompletionMode.fallback
    if human_takeover_state == "owned":
        return CanaryCompletionMode.stopped_human_takeover
    if handoff:
        return CanaryCompletionMode.handoff
    return CanaryCompletionMode.ai_continued


def _asks_for_human(text: str) -> bool:
    return "speak to" in text or "agent" in text or "human" in text


def _is_greeting(text: str) -> bool:
    return text.strip().casefold() in {"hi", "hello", "good morning", "good afternoon"}


def _looks_like_identifier(text: str) -> bool:
    return (
        "portal id" in text
        or "customer id" in text
        or "@" in text
        or sum(character.isdigit() for character in text) >= 7
    )


def _first_usable_text(evidence: CanaryRuntimeEvidence) -> str:
    for turn in evidence.message_turns:
        usable = turn.get("usable_text")
        if isinstance(usable, str) and usable:
            return usable
    return ""


def _response_mentions_context(first_text: str, response: str) -> bool:
    normalized_response = response.casefold()
    if any(term in first_text for term in ("internet", "brows", "slow")):
        return any(term in normalized_response for term in ("brows", "issue", "slow"))
    if any(term in first_text for term in ("payment", "paid", "activated")):
        return any(
            term in normalized_response for term in ("payment", "activation", "billing")
        )
    return True
