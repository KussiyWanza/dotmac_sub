"""LangGraph orchestration for composable AI Intake.

LangGraph is an execution coordinator here, not a business source of truth.
The authoritative intake state remains ``AiIntakeSession`` plus its pinned
``AiIntakePolicyVersion``. This module hydrates that state, invokes a stable
graph topology, and returns the existing ``ConversationEngineDecision`` shape
used by the Team Inbox session processor.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict, cast

from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakePolicyVersion, AiIntakeSession
from app.models.team_inbox import InboxConversation
from app.schemas.ai_intake import AiClassifierAttempt, AiIntakeClassification
from app.services import ai_intake_conversation_engine as engine

logger = logging.getLogger(__name__)

LANGGRAPH_ENGINE_MODE = "langgraph_v1"
CUSTOM_ENGINE_MODE = "custom_v1"
ENGINE_MODE_METADATA_KEY = "conversation_engine_mode"

GRAPH_NODE_SEQUENCE: tuple[str, ...] = (
    "load_policy",
    "load_state",
    "understand_message",
    "merge_facts",
    "identify_customer",
    "determine_missing_information",
    "handle_classifier_unavailable",
    "request_identifier",
    "select_tool",
    "execute_tool",
    "interpret_tool_result",
    "troubleshoot",
    "decide_next_action",
    "compose_response",
    "handoff",
    "resolved",
)


class LangGraphUnavailableError(RuntimeError):
    """Raised when a policy asks for LangGraph but it is not installed."""


class AiIntakeGraphState(TypedDict, total=False):
    conversation_id: str
    session_id: str
    policy_version_id: str | None
    channel: str
    latest_message: str
    recent_messages: list[dict[str, str]]
    active_intent: str | None
    previous_intent: str | None
    category: str | None
    confidence: float | None
    classifier_attempt_status: str
    classifier_failure_reason: str | None
    classifier_failure_kind: str | None
    classifier_retry_count: int
    classifier_retry_limit: int
    classifier_retries_exhausted: bool
    customer_identity: dict[str, object]
    subscriber_id: str | None
    portal_id: str | None
    registered_email: str | None
    registered_phone: str | None
    service_account_identity: dict[str, object]
    facts: dict[str, object]
    missing_fields: list[str]
    requested_fields: list[str]
    allowed_tools: list[str]
    selected_tool: str | None
    tool_result: dict[str, object]
    tool_results: list[dict[str, object]]
    tool_failures: list[dict[str, object]]
    troubleshooting_state: list[str]
    affect: dict[str, object]
    acknowledgement_required: bool
    issue_acknowledged: bool
    frustration_acknowledged: bool
    candidate_question_keys: list[str]
    effective_question_order: list[dict[str, object]]
    selected_question_key: str | None
    selected_question_priority: int | None
    selected_priority_source: str | None
    human_requested: bool
    escalation_reason: str | None
    routing_hint: str | None
    handoff_required: bool
    resolved: bool
    response_text: str | None
    turn_count: int
    clarification_count: int
    policy: dict[str, object]
    new_facts: dict[str, object]
    graph_action: str | None
    graph_reason: str | None
    graph_route: str | None
    dotmac_state: engine.ConversationalState
    decision: engine.ConversationEngineDecision
    now: datetime
    tool_mode: str
    node_trace: NotRequired[list[str]]


@dataclass(frozen=True, slots=True)
class _GraphRuntime:
    db: Session
    conversation: InboxConversation
    session: AiIntakeSession
    version: AiIntakePolicyVersion | None
    latest_body: str
    classification: AiIntakeClassification | None
    classifier_attempt: AiClassifierAttempt
    recent_messages: tuple[dict[str, str], ...]
    now: datetime
    tool_mode: str


def langgraph_available() -> bool:
    try:
        _langgraph_runtime()
    except LangGraphUnavailableError:
        return False
    return True


def langgraph_engine_enabled(version: AiIntakePolicyVersion | None) -> bool:
    metadata = dict(version.metadata_ or {}) if version is not None else {}
    if not metadata.get("conversational_engine_enabled"):
        return False
    return str(
        metadata.get(ENGINE_MODE_METADATA_KEY) or CUSTOM_ENGINE_MODE
    ).strip() == (LANGGRAPH_ENGINE_MODE)


def run_ai_intake_graph(
    db: Session,
    *,
    conversation: InboxConversation,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    latest_body: str,
    classification: AiIntakeClassification | None,
    classifier_attempt: AiClassifierAttempt | None = None,
    recent_messages: Sequence[object] = (),
    now: datetime | None = None,
    tool_mode: str = "live_read_only",
) -> engine.ConversationEngineDecision:
    """Run one inbound-message graph turn and return the existing decision type."""

    _, state_graph = _langgraph_runtime()
    resolved_classifier_attempt = engine._resolve_classifier_attempt(
        classification=classification,
        classifier_attempt=classifier_attempt,
        retry_limit=max(0, min(session.max_turns - 1, 5)),
    )
    runtime = _GraphRuntime(
        db=db,
        conversation=conversation,
        session=session,
        version=version,
        latest_body=str(latest_body or ""),
        classification=classification,
        classifier_attempt=resolved_classifier_attempt,
        recent_messages=_serialize_recent_messages(recent_messages),
        now=now or datetime.now(UTC),
        tool_mode=tool_mode,
    )
    graph = _build_graph(state_graph, runtime)
    logger.info(
        "ai_intake_graph_started",
        extra={
            "event": "ai_intake_graph_started",
            "conversation_id": str(conversation.id),
            "session_id": str(session.id),
            "policy_version_id": str(version.id) if version is not None else None,
            "engine": LANGGRAPH_ENGINE_MODE,
            "classifier_attempt_status": resolved_classifier_attempt.status.value,
            "classifier_failure_reason": (
                resolved_classifier_attempt.reason.value
                if resolved_classifier_attempt.reason is not None
                else None
            ),
            "classifier_failure_kind": (
                resolved_classifier_attempt.failure_kind.value
                if resolved_classifier_attempt.failure_kind is not None
                else None
            ),
            "classifier_retry_count": resolved_classifier_attempt.retry_count,
            "classifier_retry_limit": resolved_classifier_attempt.retry_limit,
            "classifier_retries_exhausted": (
                resolved_classifier_attempt.retries_exhausted
            ),
        },
    )
    result = graph.invoke(
        {
            "conversation_id": str(conversation.id),
            "session_id": str(session.id),
            "policy_version_id": str(session.policy_version_id)
            if session.policy_version_id
            else None,
            "channel": conversation.channel_type,
            "latest_message": runtime.latest_body[:4000],
            "recent_messages": list(runtime.recent_messages),
            "now": runtime.now,
            "tool_mode": runtime.tool_mode,
            "node_trace": [],
        }
    )
    decision = result.get("decision")
    if isinstance(decision, engine.ConversationEngineDecision):
        return decision
    state = result.get("dotmac_state")
    if not isinstance(state, engine.ConversationalState):
        state = engine.ConversationalState.load(
            conversation=conversation,
            session=session,
        )
    decision = engine._handoff_decision(
        engine._policy(version, channel=conversation.channel_type),
        state,
        reason="graph_completed_without_action",
        response="I will pass the details I have collected to the support team.",
    )
    return engine.ConversationEngineDecision(
        action=decision.action,
        state=decision.state,
        response_text=decision.response_text,
        handoff_summary=decision.handoff_summary,
        metadata={
            **decision.metadata,
            "engine": LANGGRAPH_ENGINE_MODE,
            "node_trace": result.get("node_trace") or [],
        },
    )


def graph_topology() -> dict[str, tuple[str, ...]]:
    """Return a static, testable view of the graph topology."""

    return {
        "load_policy": ("load_state",),
        "load_state": ("understand_message",),
        "understand_message": ("merge_facts",),
        "merge_facts": ("handoff", "identify_customer"),
        "identify_customer": ("determine_missing_information",),
        "determine_missing_information": (
            "handle_classifier_unavailable",
            "request_identifier",
            "handoff",
            "decide_next_action",
            "select_tool",
        ),
        "handle_classifier_unavailable": ("decide_next_action",),
        "request_identifier": ("compose_response",),
        "select_tool": ("execute_tool", "troubleshoot", "handoff"),
        "execute_tool": ("interpret_tool_result",),
        "interpret_tool_result": ("troubleshoot", "handoff"),
        "troubleshoot": ("decide_next_action",),
        "decide_next_action": (
            "compose_response",
            "handoff",
            "resolved",
            "__end__",
        ),
        "compose_response": ("__end__",),
        "handoff": ("__end__",),
        "resolved": ("__end__",),
    }


def _langgraph_runtime() -> tuple[Any, Any]:
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise LangGraphUnavailableError("LangGraph is not installed") from exc
    return END, StateGraph


def _build_graph(state_graph: Any, runtime: _GraphRuntime) -> Any:
    end, _ = _langgraph_runtime()
    builder = state_graph(AiIntakeGraphState)
    builder.add_node("load_policy", _load_policy(runtime))
    builder.add_node("load_state", _load_state(runtime))
    builder.add_node("understand_message", _understand_message(runtime))
    builder.add_node("merge_facts", _merge_facts(runtime))
    builder.add_node("identify_customer", _identify_customer(runtime))
    builder.add_node(
        "determine_missing_information",
        _determine_missing_information(runtime),
    )
    builder.add_node(
        "handle_classifier_unavailable",
        _handle_classifier_unavailable(runtime),
    )
    builder.add_node("request_identifier", _request_identifier(runtime))
    builder.add_node("select_tool", _select_tool(runtime))
    builder.add_node("execute_tool", _execute_tool(runtime))
    builder.add_node("interpret_tool_result", _interpret_tool_result(runtime))
    builder.add_node("troubleshoot", _troubleshoot(runtime))
    builder.add_node("decide_next_action", _decide_next_action(runtime))
    builder.add_node("compose_response", _compose_response(runtime))
    builder.add_node("handoff", _handoff(runtime))
    builder.add_node("resolved", _resolved(runtime))

    builder.set_entry_point("load_policy")
    builder.add_edge("load_policy", "load_state")
    builder.add_edge("load_state", "understand_message")
    builder.add_edge("understand_message", "merge_facts")
    builder.add_conditional_edges(
        "merge_facts",
        _route_after_merge,
        {"handoff": "handoff", "identify_customer": "identify_customer"},
    )
    builder.add_edge("identify_customer", "determine_missing_information")
    builder.add_conditional_edges(
        "determine_missing_information",
        _route_missing_information,
        {
            "handle_classifier_unavailable": "handle_classifier_unavailable",
            "request_identifier": "request_identifier",
            "handoff": "handoff",
            "decide_next_action": "decide_next_action",
            "select_tool": "select_tool",
        },
    )
    builder.add_edge("handle_classifier_unavailable", "decide_next_action")
    builder.add_edge("request_identifier", "compose_response")
    builder.add_conditional_edges(
        "select_tool",
        _route_selected_tool,
        {
            "execute_tool": "execute_tool",
            "troubleshoot": "troubleshoot",
            "handoff": "handoff",
        },
    )
    builder.add_edge("execute_tool", "interpret_tool_result")
    builder.add_conditional_edges(
        "interpret_tool_result",
        _route_tool_result,
        {"troubleshoot": "troubleshoot", "handoff": "handoff"},
    )
    builder.add_edge("troubleshoot", "decide_next_action")
    builder.add_conditional_edges(
        "decide_next_action",
        _route_next_action,
        {
            "compose_response": "compose_response",
            "handoff": "handoff",
            "resolved": "resolved",
            "end": end,
        },
    )
    builder.add_edge("compose_response", end)
    builder.add_edge("handoff", end)
    builder.add_edge("resolved", end)
    return builder.compile()


def _load_policy(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        policy = engine._policy(
            runtime.version, channel=runtime.conversation.channel_type
        )
        allowed_tools = [
            key for key in engine.TOOL_CATALOG if engine._tool_enabled(policy, key)
        ]
        _log_node(
            "load_policy",
            runtime,
            policy_version_id=state.get("policy_version_id"),
            allowed_tools=allowed_tools,
        )
        return _trace(
            state,
            "load_policy",
            {
                "policy": policy,
                "allowed_tools": allowed_tools,
            },
        )

    return node


def _load_state(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = engine.ConversationalState.load(
            conversation=runtime.conversation,
            session=runtime.session,
        )
        engine._reset_turn_planner_evidence(dotmac_state)
        dotmac_state.turn_count += 1
        engine._append_statement(dotmac_state, runtime.latest_body)
        engine._merge_classifier_attempt(dotmac_state, runtime.classifier_attempt)
        updates = _state_updates(dotmac_state)
        updates.update(
            {
                "dotmac_state": dotmac_state,
                "latest_message": runtime.latest_body[:4000],
                "recent_messages": list(runtime.recent_messages),
                "tool_mode": runtime.tool_mode,
                "now": runtime.now,
            }
        )
        _log_node(
            "load_state",
            runtime,
            turn_count=dotmac_state.turn_count,
            current_intent=dotmac_state.current_intent,
            classifier_attempt_status=runtime.classifier_attempt.status.value,
            classifier_failure_reason=(
                runtime.classifier_attempt.reason.value
                if runtime.classifier_attempt.reason is not None
                else None
            ),
            classifier_failure_kind=(
                runtime.classifier_attempt.failure_kind.value
                if runtime.classifier_attempt.failure_kind is not None
                else None
            ),
            classifier_retry_count=runtime.classifier_attempt.retry_count,
            classifier_retry_limit=runtime.classifier_attempt.retry_limit,
            classifier_retries_exhausted=runtime.classifier_attempt.retries_exhausted,
        )
        return _trace(state, "load_state", updates)

    return node


def _understand_message(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        facts = engine.extract_facts(runtime.latest_body)
        classification = runtime.classification
        detected_intent = (
            classification.intent.value if classification is not None else None
        )
        _log_node(
            "understand_message",
            runtime,
            detected_intent=detected_intent,
            fact_keys=sorted(facts),
            provider_result_available=classification is not None,
        )
        return _trace(
            state,
            "understand_message",
            {
                "new_facts": facts,
                "human_requested": bool(facts.get("human_requested"))
                or bool(state.get("human_requested")),
            },
        )

    return node


def _merge_facts(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        before_intent = dotmac_state.current_intent
        latest_facts = dict(state.get("new_facts") or {})
        engine._merge_facts(dotmac_state, latest_facts)
        engine._merge_affect(
            dotmac_state,
            engine.detect_affect(runtime.latest_body, state=dotmac_state),
            fresh_signal=True,
        )
        engine._merge_classification(dotmac_state, runtime.classification)
        engine._refresh_issue_acknowledgement_requirement(dotmac_state)
        if runtime.classification is not None:
            latest_facts.update(
                engine._meaningful_understanding_facts(
                    runtime.classification.message_facts
                )
            )
        engine._link_latest_answer(
            dotmac_state,
            latest_body=runtime.latest_body,
            latest_facts=latest_facts,
            now=runtime.now,
        )
        if dotmac_state.human_requested:
            return _set_graph_decision(
                state,
                dotmac_state=dotmac_state,
                action="handoff",
                reason="human_requested",
                response=engine._handoff_response(
                    _policy(state),
                    default="I will pass this to a support agent now.",
                ),
                node="merge_facts",
            )
        if dotmac_state.current_intent != before_intent and before_intent:
            engine._record_event(
                runtime.session,
                "intent_changed",
                runtime.now,
                state=dotmac_state,
            )
            _log_node(
                "merge_facts",
                runtime,
                intent_changed=True,
                previous_intent=before_intent,
                current_intent=dotmac_state.current_intent,
            )
        else:
            _log_node(
                "merge_facts",
                runtime,
                fact_count=len(dotmac_state.collected_facts),
                current_intent=dotmac_state.current_intent,
            )
        updates = _state_updates(dotmac_state)
        policy = _policy(state)
        updates["allowed_tools"] = [
            key
            for key in engine.TOOL_CATALOG
            if engine._tool_allowed_for_state(policy, key, dotmac_state)
        ]
        updates["dotmac_state"] = dotmac_state
        return _trace(state, "merge_facts", updates)

    return node


def _identify_customer(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        policy = _policy(state)
        before = dotmac_state.subscriber_id
        engine._merge_contact_from_conversation(
            dotmac_state,
            runtime.conversation,
            runtime.db,
        )
        engine._identify_customer(
            runtime.db,
            state=dotmac_state,
            conversation=runtime.conversation,
            policy=policy,
            tool_mode=runtime.tool_mode,
        )
        _log_node(
            "identify_customer",
            runtime,
            customer_identified=bool(dotmac_state.subscriber_id),
            changed=before != dotmac_state.subscriber_id,
        )
        updates = _state_updates(dotmac_state)
        updates["dotmac_state"] = dotmac_state
        return _trace(state, "identify_customer", updates)

    return node


def _determine_missing_information(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        policy = _policy(state)
        if dotmac_state.human_requested:
            return _set_graph_decision(
                state,
                dotmac_state=dotmac_state,
                action="handoff",
                reason="human_requested",
                response=engine._handoff_response(
                    policy,
                    default="I will pass this to a support agent now.",
                ),
                node="determine_missing_information",
            )
        if engine.classifier_attempt_unavailable(runtime.classifier_attempt):
            _log_node(
                "determine_missing_information",
                runtime,
                route="handle_classifier_unavailable",
                classifier_attempt_status=runtime.classifier_attempt.status.value,
                classifier_failure_kind=(
                    runtime.classifier_attempt.failure_kind.value
                    if runtime.classifier_attempt.failure_kind is not None
                    else None
                ),
                classifier_retry_count=runtime.classifier_attempt.retry_count,
                classifier_retry_limit=runtime.classifier_attempt.retry_limit,
                classifier_retries_exhausted=(
                    runtime.classifier_attempt.retries_exhausted
                ),
            )
            return _trace(
                state,
                "determine_missing_information",
                {"graph_route": "handle_classifier_unavailable"},
            )
        max_turns = engine._bounded_int(
            policy.get("max_turns"),
            default=runtime.session.max_turns,
            low=1,
            high=10,
        )
        if dotmac_state.turn_count > max_turns:
            return _set_graph_decision(
                state,
                dotmac_state=dotmac_state,
                action="handoff",
                reason="turn_limit",
                response=engine._handoff_response(
                    policy,
                    default=(
                        "I will pass the details I have collected to the support team."
                    ),
                ),
                node="determine_missing_information",
            )
        if engine._requires_identity_before_tools(dotmac_state, policy):
            requested = engine._next_identifier_to_request(dotmac_state, policy)
            if requested is not None:
                _log_node(
                    "determine_missing_information",
                    runtime,
                    missing_field=requested,
                )
                return _trace(
                    state,
                    "determine_missing_information",
                    {"graph_route": "request_identifier", "selected_tool": requested},
                )
            return _set_graph_decision(
                state,
                dotmac_state=dotmac_state,
                action="handoff",
                reason="customer_unidentified",
                response=engine._handoff_response(
                    policy,
                    default=(
                        "I could not safely identify the account from the details "
                        "provided. I will pass this to the support team."
                    ),
                ),
                node="determine_missing_information",
            )
        decision = engine._configured_playbook_decision(
            runtime.db,
            dotmac_state,
            policy,
            conversation=runtime.conversation,
            tool_mode=runtime.tool_mode,
        )
        if decision is None:
            decision = engine._configured_troubleshooting_decision(
                runtime.db,
                dotmac_state,
                policy,
                conversation=runtime.conversation,
                tool_mode=runtime.tool_mode,
            )
        if decision is not None:
            return _set_existing_decision(
                state,
                decision,
                node="determine_missing_information",
            )
        _log_node("determine_missing_information", runtime, route="select_tool")
        return _trace(
            state,
            "determine_missing_information",
            {"graph_route": "select_tool"},
        )

    return node


def _handle_classifier_unavailable(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        decision = engine.classifier_unavailable_decision(
            state=dotmac_state,
            policy=_policy(state),
            classifier_attempt=runtime.classifier_attempt,
            question=engine._classifier_unavailable_question(runtime.version),
            now=runtime.now,
        )
        _log_node(
            "handle_classifier_unavailable",
            runtime,
            action=decision.action,
            reason=decision.metadata.get("reason"),
            classifier_failure_reason=(
                runtime.classifier_attempt.reason.value
                if runtime.classifier_attempt.reason is not None
                else None
            ),
            classifier_attempt_status=runtime.classifier_attempt.status.value,
            classifier_failure_kind=(
                runtime.classifier_attempt.failure_kind.value
                if runtime.classifier_attempt.failure_kind is not None
                else None
            ),
            classifier_retry_count=runtime.classifier_attempt.retry_count,
            classifier_retry_limit=runtime.classifier_attempt.retry_limit,
            classifier_retries_exhausted=(runtime.classifier_attempt.retries_exhausted),
        )
        return _set_existing_decision(
            state,
            decision,
            node="handle_classifier_unavailable",
        )

    return node


def _request_identifier(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        requested = str(state.get("selected_tool") or "").strip()
        response = engine._identifier_question(requested)
        question = engine._record_question(
            dotmac_state,
            key=requested,
            expected_fact=requested,
            prompt=response,
            purpose=engine._identifier_question_purpose(requested),
            priority=1000,
            priority_source="playbook",
            required=True,
            now=runtime.now,
        )
        dotmac_state.candidate_question_keys = [question.key]
        dotmac_state.effective_question_order = [
            {
                "key": question.key,
                "priority": question.priority,
                "source": question.priority_source,
                "required": question.required,
            }
        ]
        _log_node("request_identifier", runtime, field=requested)
        return _set_existing_decision(
            state,
            engine.ConversationEngineDecision(
                action="respond",
                state=dotmac_state,
                response_text=question.prompt,
                metadata={
                    "reason": "missing_customer_identifier",
                    "question_key": question.key,
                    "expected_fact": question.expected_fact,
                    "question_purpose": question.purpose,
                    "question_priority": question.priority,
                    "priority_source": question.priority_source,
                    "question_required": question.required,
                    "candidate_question_keys": list(
                        dotmac_state.candidate_question_keys
                    ),
                    "effective_question_order": list(
                        dotmac_state.effective_question_order
                    ),
                    "acknowledgement_required": (dotmac_state.acknowledgement_required),
                    "next_action": "ask_question",
                    "response_source": "template",
                },
            ),
            node="request_identifier",
        )

    return node


def _select_tool(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        policy = _policy(state)
        selected = None
        if engine._should_run_monitoring(dotmac_state, policy):
            selected = "subscriber_monitoring"
        _log_node("select_tool", runtime, selected_tool=selected)
        return _trace(
            state,
            "select_tool",
            {
                "selected_tool": selected,
                "graph_route": "execute_tool" if selected else "troubleshoot",
            },
        )

    return node


def _execute_tool(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        policy = _policy(state)
        selected = str(state.get("selected_tool") or "").strip()
        result: dict[str, object] = {"status": "unavailable", "reason": "no_tool"}
        if selected == "subscriber_monitoring" and dotmac_state.subscriber_id:
            result, latency_ms = engine._execute_timed_tool(
                runtime.db,
                selected,
                {"subscriber_id": dotmac_state.subscriber_id},
                policy=policy,
                conversation=runtime.conversation,
                tool_mode=runtime.tool_mode,
            )
            engine._record_tool_result(
                dotmac_state, selected, result, latency_ms=latency_ms
            )
        _log_node(
            "execute_tool",
            runtime,
            selected_tool=selected,
            tool_status=result.get("status"),
        )
        updates = _state_updates(dotmac_state)
        updates.update({"dotmac_state": dotmac_state, "tool_result": result})
        return _trace(state, "execute_tool", updates)

    return node


def _interpret_tool_result(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        policy = _policy(state)
        result = state.get("tool_result")
        status = result.get("status") if isinstance(result, dict) else None
        if status == "unavailable" and engine._tool_failure_requires_handoff(
            policy,
            tool_key="subscriber_monitoring",
            status="unavailable",
        ):
            return _set_graph_decision(
                state,
                dotmac_state=dotmac_state,
                action="handoff",
                reason="monitoring_unavailable",
                response=engine._handoff_response(
                    policy,
                    default=(
                        "I could not complete the connection check right now. "
                        "I will pass the details I have collected to the support team."
                    ),
                ),
                node="interpret_tool_result",
            )
        if status == "unauthorized" and engine._tool_failure_requires_handoff(
            policy,
            tool_key="subscriber_monitoring",
            status="unauthorized",
        ):
            return _set_graph_decision(
                state,
                dotmac_state=dotmac_state,
                action="handoff",
                reason="monitoring_unauthorized",
                response=engine._handoff_response(
                    policy,
                    default="I will pass this to the support team for investigation.",
                ),
                node="interpret_tool_result",
            )
        _log_node(
            "interpret_tool_result",
            runtime,
            tool_status=status,
            route="troubleshoot",
        )
        return _trace(
            state,
            "interpret_tool_result",
            {"graph_route": "troubleshoot"},
        )

    return node


def _troubleshoot(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        policy = _policy(state)
        if engine._technical_issue(dotmac_state) and engine._monitoring_offline(
            dotmac_state
        ):
            dotmac_state.troubleshooting_completed = engine._with_unique(
                dotmac_state.troubleshooting_completed,
                "monitoring_checked",
            )
        inquiry_escalation = engine._inquiry_escalation_reason(dotmac_state, policy)
        if inquiry_escalation is not None:
            decision = engine._handoff_decision(
                policy,
                dotmac_state,
                reason=inquiry_escalation,
                response=engine._handoff_response(
                    policy,
                    default=("I will pass this to the support team for investigation."),
                ),
            )
            return _set_existing_decision(state, decision, node="troubleshoot")
        question = engine._next_useful_question(dotmac_state, policy, now=runtime.now)
        if question is not None:
            return _set_existing_decision(
                state,
                engine.ConversationEngineDecision(
                    action="respond",
                    state=dotmac_state,
                    response_text=question.prompt,
                    metadata={
                        "reason": "useful_missing_fact",
                        "question_key": question.key,
                        "expected_fact": question.expected_fact,
                        "question_purpose": question.purpose,
                        "question_priority": question.priority,
                        "priority_source": question.priority_source,
                        "question_required": question.required,
                        "candidate_question_keys": list(
                            dotmac_state.candidate_question_keys
                        ),
                        "effective_question_order": list(
                            dotmac_state.effective_question_order
                        ),
                        "acknowledgement_required": (
                            dotmac_state.acknowledgement_required
                        ),
                        "tone_requirements": engine._inquiry_tone_requirements(
                            dotmac_state, policy
                        ),
                        "answer_status": question.answer_status.value,
                        "next_action": "ask_question",
                        "response_source": "template",
                    },
                ),
                node="troubleshoot",
            )
        if engine._should_handoff_after_classification(dotmac_state, policy):
            return _set_graph_decision(
                state,
                dotmac_state=dotmac_state,
                action="handoff",
                reason="classified_ready_for_handoff",
                response=engine._handoff_response(
                    policy,
                    default=(
                        "I have the details needed and will pass this to the right team."
                    ),
                ),
                node="troubleshoot",
            )
        if dotmac_state.classification_requires_follow_up:
            question = engine._record_question(
                dotmac_state,
                key="intent_clarification",
                expected_fact="intent",
                prompt=(
                    dotmac_state.classification_follow_up_question
                    or "Could you briefly tell me what you need help with?"
                ),
                now=runtime.now,
            )
            return _set_existing_decision(
                state,
                engine.ConversationEngineDecision(
                    action="respond",
                    state=dotmac_state,
                    response_text=question.prompt,
                    metadata={
                        "reason": "classifier_clarification",
                        "question_key": question.key,
                        "expected_fact": question.expected_fact,
                        "next_action": "ask_question",
                        "response_source": "template",
                    },
                ),
                node="troubleshoot",
            )
        decision = engine._handoff_decision(
            policy,
            dotmac_state,
            reason="unsupported_or_troubleshooting_exhausted",
            response=engine._handoff_response(
                policy,
                default=(
                    "I will pass the details I have collected to the support team."
                ),
            ),
        )
        return _set_existing_decision(state, decision, node="troubleshoot")

    return node


def _decide_next_action(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        action = str(state.get("graph_action") or "handoff")
        reason = str(state.get("graph_reason") or "langgraph_decision")
        _log_node("decide_next_action", runtime, action=action, reason=reason)
        return _trace(state, "decide_next_action", {})

    return node


def _compose_response(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        completed_trace = [*(state.get("node_trace") or []), "compose_response"][-40:]
        existing = state.get("decision")
        existing_metadata = (
            dict(existing.metadata)
            if isinstance(existing, engine.ConversationEngineDecision)
            else {}
        )
        decision = engine.ConversationEngineDecision(
            action=str(state.get("graph_action") or "respond"),
            state=dotmac_state,
            response_text=_text_or_none(state.get("response_text")),
            metadata={
                **existing_metadata,
                "reason": str(
                    existing_metadata.get("reason")
                    or state.get("graph_reason")
                    or "response_ready"
                ),
                "engine": LANGGRAPH_ENGINE_MODE,
                "node_trace": completed_trace,
            },
        )
        _log_node(
            "compose_response",
            runtime,
            action=decision.action,
            has_response=bool(decision.response_text),
        )
        return _trace(state, "compose_response", {"decision": decision})

    return node


def _handoff(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        completed_trace = [*(state.get("node_trace") or []), "handoff"][-40:]
        policy = _policy(state)
        reason = str(state.get("graph_reason") or "handoff")
        response = _text_or_none(
            state.get("response_text")
        ) or engine._handoff_response(
            policy,
            default="I will pass this to the support team for investigation.",
        )
        decision = engine._handoff_decision(
            policy,
            dotmac_state,
            reason=reason,
            response=response,
        )
        decision = engine.ConversationEngineDecision(
            action=decision.action,
            state=decision.state,
            response_text=decision.response_text,
            handoff_summary=decision.handoff_summary,
            metadata={
                **decision.metadata,
                "engine": LANGGRAPH_ENGINE_MODE,
                "node_trace": completed_trace,
            },
        )
        _log_node("handoff", runtime, reason=reason)
        return _trace(
            state,
            "handoff",
            {
                "decision": decision,
                "dotmac_state": decision.state,
                "handoff_required": True,
            },
        )

    return node


def _resolved(runtime: _GraphRuntime):
    def node(state: AiIntakeGraphState) -> AiIntakeGraphState:
        dotmac_state = _dotmac_state(state)
        completed_trace = [*(state.get("node_trace") or []), "resolved"][-40:]
        dotmac_state.resolution_status = "resolved"
        existing = state.get("decision")
        existing_metadata = (
            dict(existing.metadata)
            if isinstance(existing, engine.ConversationEngineDecision)
            else {}
        )
        decision = engine.ConversationEngineDecision(
            action="resolved",
            state=dotmac_state,
            response_text=_text_or_none(state.get("response_text")),
            metadata={
                **existing_metadata,
                "reason": str(
                    existing_metadata.get("reason")
                    or state.get("graph_reason")
                    or "resolved"
                ),
                "acknowledgement_required": (dotmac_state.acknowledgement_required),
                "next_action": "resolve",
                "engine": LANGGRAPH_ENGINE_MODE,
                "node_trace": completed_trace,
            },
        )
        _log_node("resolved", runtime)
        return _trace(state, "resolved", {"decision": decision})

    return node


def _route_missing_information(state: AiIntakeGraphState) -> str:
    action = state.get("graph_action")
    if action == "handoff":
        return "handoff"
    if action in {"respond", "resolved"}:
        return "decide_next_action"
    route = state.get("graph_route")
    if route == "handle_classifier_unavailable":
        return "handle_classifier_unavailable"
    if route == "request_identifier":
        return "request_identifier"
    return "select_tool"


def _route_after_merge(state: AiIntakeGraphState) -> str:
    return "handoff" if state.get("graph_action") == "handoff" else "identify_customer"


def _route_selected_tool(state: AiIntakeGraphState) -> str:
    action = state.get("graph_action")
    if action == "handoff":
        return "handoff"
    if state.get("selected_tool"):
        return "execute_tool"
    return "troubleshoot"


def _route_tool_result(state: AiIntakeGraphState) -> str:
    return "handoff" if state.get("graph_action") == "handoff" else "troubleshoot"


def _route_next_action(state: AiIntakeGraphState) -> str:
    action = str(state.get("graph_action") or "handoff")
    if action == "handoff":
        return "handoff"
    if action == "resolved":
        return "resolved"
    if action == "respond":
        return "compose_response"
    return "handoff"


def _set_existing_decision(
    state: AiIntakeGraphState,
    decision: engine.ConversationEngineDecision,
    *,
    node: str,
) -> AiIntakeGraphState:
    updates = _state_updates(decision.state)
    updates.update(
        {
            "dotmac_state": decision.state,
            "graph_action": decision.action,
            "graph_reason": decision.metadata.get("reason"),
            "response_text": decision.response_text,
            "handoff_required": decision.action == "handoff",
            "resolved": decision.state.resolution_status == "resolved",
            "decision": decision,
        }
    )
    return _trace(state, node, updates)


def _set_graph_decision(
    state: AiIntakeGraphState,
    *,
    dotmac_state: engine.ConversationalState,
    action: str,
    reason: str,
    response: str | None,
    node: str,
) -> AiIntakeGraphState:
    updates = _state_updates(dotmac_state)
    updates.update(
        {
            "dotmac_state": dotmac_state,
            "graph_action": action,
            "graph_reason": reason,
            "response_text": response,
            "handoff_required": action == "handoff",
            "resolved": action == "resolved",
        }
    )
    _log_node(node, None, action=action, reason=reason)
    return _trace(state, node, updates)


def _state_updates(dotmac_state: engine.ConversationalState) -> dict[str, object]:
    return {
        "conversation_id": dotmac_state.conversation_id,
        "session_id": dotmac_state.session_id,
        "policy_version_id": dotmac_state.policy_version_id,
        "channel": dotmac_state.channel,
        "active_intent": dotmac_state.current_intent,
        "previous_intent": dotmac_state.previous_intent,
        "category": dotmac_state.category,
        "confidence": dotmac_state.confidence,
        "classifier_attempt_status": dotmac_state.classifier_attempt_status.value,
        "classifier_failure_reason": (
            dotmac_state.classifier_failure_reason.value
            if dotmac_state.classifier_failure_reason is not None
            else None
        ),
        "classifier_failure_kind": (
            dotmac_state.classifier_failure_kind.value
            if dotmac_state.classifier_failure_kind is not None
            else None
        ),
        "classifier_retry_count": dotmac_state.classifier_retry_count,
        "classifier_retry_limit": dotmac_state.classifier_retry_limit,
        "classifier_retries_exhausted": (dotmac_state.classifier_retries_exhausted),
        "customer_identity": dotmac_state.contact_identity,
        "subscriber_id": dotmac_state.subscriber_id,
        "portal_id": dotmac_state.portal_id,
        "registered_email": dotmac_state.registered_email,
        "registered_phone": dotmac_state.registered_phone,
        "service_account_identity": dotmac_state.service_account_identity,
        "facts": dotmac_state.collected_facts,
        "missing_fields": dotmac_state.missing_facts,
        "requested_fields": dotmac_state.already_requested_fields,
        "tool_results": dotmac_state.tool_executions,
        "tool_failures": dotmac_state.tool_errors,
        "troubleshooting_state": dotmac_state.troubleshooting_completed,
        "affect": {
            "frustration_level": dotmac_state.frustration_level.value,
            "agitation_level": dotmac_state.agitation_level.value,
            "repeated_complaint": dotmac_state.repeated_complaint,
            "repeated_failed_steps": dotmac_state.repeated_failed_steps,
            "prior_failed_interaction": dotmac_state.prior_failed_interaction,
            "sources": list(dotmac_state.affect_sources),
        },
        "acknowledgement_required": dotmac_state.acknowledgement_required,
        "issue_acknowledged": dotmac_state.issue_acknowledged,
        "frustration_acknowledged": dotmac_state.frustration_acknowledged,
        "candidate_question_keys": list(dotmac_state.candidate_question_keys),
        "effective_question_order": list(dotmac_state.effective_question_order),
        "selected_question_key": dotmac_state.selected_question_key,
        "selected_question_priority": dotmac_state.selected_question_priority,
        "selected_priority_source": dotmac_state.selected_priority_source,
        "human_requested": dotmac_state.human_requested,
        "escalation_reason": dotmac_state.escalation_reason,
        "routing_hint": dotmac_state.destination_team_id,
        "handoff_required": dotmac_state.handoff_status == "requested",
        "resolved": dotmac_state.resolution_status == "resolved",
        "turn_count": dotmac_state.turn_count,
        "clarification_count": dotmac_state.clarification_count,
    }


def _dotmac_state(state: AiIntakeGraphState) -> engine.ConversationalState:
    dotmac_state = state.get("dotmac_state")
    if not isinstance(dotmac_state, engine.ConversationalState):
        raise RuntimeError("AI intake graph state was not hydrated")
    return dotmac_state


def _policy(state: AiIntakeGraphState) -> dict[str, object]:
    policy = state.get("policy")
    return dict(policy) if isinstance(policy, dict) else {}


def _trace(
    state: AiIntakeGraphState,
    node: str,
    updates: dict[str, object],
) -> AiIntakeGraphState:
    trace = list(state.get("node_trace") or [])
    trace.append(node)
    merged: dict[str, object] = {**updates, "node_trace": trace[-40:]}
    return cast(AiIntakeGraphState, merged)


def _serialize_recent_messages(
    recent_messages: Sequence[object],
) -> tuple[dict[str, str], ...]:
    serialized: list[dict[str, str]] = []
    for message in recent_messages:
        role_value = getattr(message, "role", "")
        role = str(getattr(role_value, "value", role_value) or "")
        body = str(getattr(message, "body", "") or "").strip()
        if body:
            serialized.append({"role": role[:20], "body": body[:1200]})
    return tuple(serialized[-6:])


def _text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _log_node(
    node: str,
    runtime: _GraphRuntime | None,
    **fields: object,
) -> None:
    extra: dict[str, object] = {
        "event": "ai_intake_graph_node",
        "node": node,
    }
    if runtime is not None:
        extra.update(
            {
                "conversation_id": str(runtime.conversation.id),
                "session_id": str(runtime.session.id),
                "policy_version_id": str(runtime.version.id)
                if runtime.version is not None
                else None,
            }
        )
    extra.update(fields)
    logger.info("ai_intake_graph_node", extra=extra)
