"""Composable conversational AI intake engine.

The engine owns AI intake state interpretation only. Team Inbox remains the
owner for routing, queueing, assignment, outbound delivery, and human takeover.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from string import Template
from typing import Any

from sqlalchemy.orm import Session

from app.models.ai_intake import AiIntakePolicyVersion, AiIntakeSession
from app.models.team_inbox import InboxConversation
from app.schemas.ai_intake import (
    AiIntakeAnswerStatus,
    AiIntakeClassification,
    AiIntakeExtractedFacts,
)
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.network import support_monitoring
from app.services.team_inbox_support_identity import (
    CustomerIdentifierKind,
    CustomerIdentityQuery,
    CustomerIdentityStatus,
    SupportReadContext,
    resolve_customer_identity,
)

STATE_KEY = "conversation_state"
EVENTS_KEY = "conversation_events"
CUSTOMER_IDENTIFIER_REQUEST = "customer_identifier"

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?:\+?234|0)?[789][01]\d{8}\b")
OUTAGE_CONTEXT_RE = re.compile(
    r"\b(?:since|for)\s+([a-z0-9][a-z0-9\s-]{0,60}?)"
    r"(?=(?:\s+(?:and|but|so|because)\b)|[,.!?;]|$)",
    re.I,
)
PORTAL_RE = re.compile(
    r"\b(?:portal(?:\s+id)?|customer\s+id|account\s+(?:id|number)|acct)\s*"
    r"(?:is|=|[:#-])?\s*([A-Z0-9][A-Z0-9_-]{2,31})\b",
    re.I,
)
HUMAN_RE = re.compile(
    r"\b(?:agent|human|person|operator|representative|customer\s+care|"
    r"speak\s+(?:to|with)\s+someone|talk\s+(?:to|with)\s+someone)\b",
    re.I,
)
SPEED_TEST_RE = re.compile(
    r"\b(?:(?P<down>\d+(?:\.\d+)?)\s*(?:mbps|mb/s)\s*(?:down(?:load)?)?)"
    r"(?:\s*(?:and|,|/)\s*(?P<up>\d+(?:\.\d+)?)\s*(?:mbps|mb/s)\s*"
    r"(?:up(?:load)?)?)?",
    re.I,
)
APPROVED_HANDOFF_SUMMARY_VARIABLES = frozenset(
    {
        "customer",
        "account",
        "portal_id",
        "channel",
        "issue",
        "intent",
        "category",
        "customer_details",
        "collected_facts",
        "monitoring_findings",
        "troubleshooting",
        "tool_results",
        "escalation_reason",
        "destination_team",
    }
)
SUPPORTED_RULE_CONDITIONS = frozenset(
    {
        "intent",
        "category",
        "customer_identified",
        "field_present",
        "field_value",
        "monitoring_status",
        "radius_status",
        "ont_status",
        "tool_result",
        "turn_count",
        "human_requested",
    }
)
SUPPORTED_RULE_ACTIONS = frozenset(
    {
        "request_field",
        "execute_tool",
        "invoke_tool",
        "provide_guidance",
        "respond",
        "handoff",
        "mark_resolved",
    }
)
SUPPORTED_IDENTIFIER_TYPES: frozenset[str] = frozenset(
    {
        "portal_id",
        "registered_email",
        "registered_phone",
    }
)
DEFAULT_IDENTIFIER_REQUEST_ORDER: tuple[str, ...] = (
    "registered_phone",
    "registered_email",
    "portal_id",
)
PLAYBOOK_POLICY_KEYS: tuple[str, ...] = ("first_line_playbooks", "playbooks")
ACCOUNT_BOUND_INTENTS = frozenset(
    {
        "billing_issue",
        "payment_confirmation",
        "subscription_renewal",
        "plan_change",
        "account_access",
    }
)
ACCOUNT_BOUND_CATEGORIES = frozenset(
    {
        "payment_not_reflected",
        "invoice_request",
        "subscription_expired",
        "renewal_request",
        "plan_change_request",
        "login_problem",
        "account_information",
        "other_billing_issue",
        "payment_confirmation",
    }
)


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    key: str
    display_name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    permission_requirements: tuple[str, ...]
    timeout_seconds: int
    read_only: bool
    allowed_contexts: tuple[str, ...]
    enabled: bool = True


@dataclass(slots=True)
class QuestionState:
    key: str
    expected_fact: str
    prompt: str
    answer_status: AiIntakeAnswerStatus = AiIntakeAnswerStatus.pending
    attempts: int = 1
    asked_at: str | None = None
    answered_at: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> QuestionState | None:
        key = str(value.get("key") or "").strip()
        expected = str(value.get("expected_fact") or key).strip()
        prompt = str(value.get("prompt") or "").strip()
        if not key or not expected or not prompt:
            return None
        try:
            answer_status = AiIntakeAnswerStatus(
                str(value.get("answer_status") or AiIntakeAnswerStatus.pending.value)
            )
        except ValueError:
            answer_status = AiIntakeAnswerStatus.unclear
        try:
            attempts = int(str(value.get("attempts") or 1))
        except (TypeError, ValueError):
            attempts = 1
        return cls(
            key=key,
            expected_fact=expected,
            prompt=prompt,
            answer_status=answer_status,
            attempts=max(1, min(attempts, 2)),
            asked_at=_text_or_none(value.get("asked_at")),
            answered_at=_text_or_none(value.get("answered_at")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "expected_fact": self.expected_fact,
            "prompt": self.prompt,
            "answer_status": self.answer_status.value,
            "attempts": self.attempts,
            "asked_at": self.asked_at,
            "answered_at": self.answered_at,
        }


@dataclass(slots=True)
class ConversationalState:
    conversation_id: str
    session_id: str
    policy_version_id: str | None
    channel: str
    current_intent: str | None = None
    previous_intent: str | None = None
    category: str | None = None
    confidence: float | None = None
    classification_requires_follow_up: bool = False
    classification_follow_up_question: str | None = None
    subscriber_id: str | None = None
    contact_identity: dict[str, object] = field(default_factory=dict)
    portal_id: str | None = None
    registered_email: str | None = None
    registered_phone: str | None = None
    service_account_identity: dict[str, object] = field(default_factory=dict)
    collected_facts: dict[str, object] = field(default_factory=dict)
    missing_facts: list[str] = field(default_factory=list)
    already_requested_fields: list[str] = field(default_factory=list)
    customer_statements: list[str] = field(default_factory=list)
    troubleshooting_completed: list[str] = field(default_factory=list)
    monitoring_results: list[dict[str, object]] = field(default_factory=list)
    tool_executions: list[dict[str, object]] = field(default_factory=list)
    tool_errors: list[dict[str, object]] = field(default_factory=list)
    resolution_status: str = "open"
    escalation_reason: str | None = None
    destination_team_id: str | None = None
    human_requested: bool = False
    handoff_status: str = "not_requested"
    start_time: str | None = None
    turn_count: int = 0
    clarification_count: int = 0
    question_history: list[QuestionState] = field(default_factory=list)
    acknowledged_issue_key: str | None = None
    last_response_source: str | None = None

    @classmethod
    def load(
        cls,
        *,
        conversation: InboxConversation,
        session: AiIntakeSession,
    ) -> ConversationalState:
        raw = dict(session.metadata_ or {}).get(STATE_KEY)
        if isinstance(raw, dict):
            question_history = [
                parsed
                for item in _dict_list(raw.get("question_history"))
                if (parsed := QuestionState.from_dict(item)) is not None
            ]
            return cls(
                conversation_id=str(raw.get("conversation_id") or conversation.id),
                session_id=str(raw.get("session_id") or session.id),
                policy_version_id=(
                    str(raw["policy_version_id"])
                    if raw.get("policy_version_id")
                    else (
                        str(session.policy_version_id)
                        if session.policy_version_id
                        else None
                    )
                ),
                channel=str(raw.get("channel") or conversation.channel_type),
                current_intent=_text_or_none(raw.get("current_intent")),
                previous_intent=_text_or_none(raw.get("previous_intent")),
                category=_text_or_none(raw.get("category")),
                confidence=_float_or_none(raw.get("confidence")),
                classification_requires_follow_up=bool(
                    raw.get("classification_requires_follow_up")
                ),
                classification_follow_up_question=_text_or_none(
                    raw.get("classification_follow_up_question")
                ),
                subscriber_id=_text_or_none(raw.get("subscriber_id")),
                contact_identity=_dict(raw.get("contact_identity")),
                portal_id=_text_or_none(raw.get("portal_id")),
                registered_email=_text_or_none(raw.get("registered_email")),
                registered_phone=_text_or_none(raw.get("registered_phone")),
                service_account_identity=_dict(raw.get("service_account_identity")),
                collected_facts=_dict(raw.get("collected_facts")),
                missing_facts=list(_list(raw.get("missing_facts"))),
                already_requested_fields=list(
                    _list(raw.get("already_requested_fields"))
                ),
                customer_statements=list(_list(raw.get("customer_statements"))),
                troubleshooting_completed=list(
                    _list(raw.get("troubleshooting_completed"))
                ),
                monitoring_results=list(_dict_list(raw.get("monitoring_results"))),
                tool_executions=list(_dict_list(raw.get("tool_executions"))),
                tool_errors=list(_dict_list(raw.get("tool_errors"))),
                resolution_status=str(raw.get("resolution_status") or "open"),
                escalation_reason=_text_or_none(raw.get("escalation_reason")),
                destination_team_id=_text_or_none(raw.get("destination_team_id")),
                human_requested=bool(raw.get("human_requested")),
                handoff_status=str(raw.get("handoff_status") or "not_requested"),
                start_time=_text_or_none(raw.get("start_time")),
                turn_count=int(raw.get("turn_count") or 0),
                clarification_count=int(raw.get("clarification_count") or 0),
                question_history=question_history,
                acknowledged_issue_key=_text_or_none(raw.get("acknowledged_issue_key")),
                last_response_source=_text_or_none(raw.get("last_response_source")),
            )
        return cls(
            conversation_id=str(conversation.id),
            session_id=str(session.id),
            policy_version_id=(
                str(session.policy_version_id) if session.policy_version_id else None
            ),
            channel=conversation.channel_type,
            start_time=datetime.now(UTC).isoformat(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "policy_version_id": self.policy_version_id,
            "channel": self.channel,
            "current_intent": self.current_intent,
            "previous_intent": self.previous_intent,
            "category": self.category,
            "confidence": self.confidence,
            "classification_requires_follow_up": (
                self.classification_requires_follow_up
            ),
            "classification_follow_up_question": self.classification_follow_up_question,
            "subscriber_id": self.subscriber_id,
            "contact_identity": self.contact_identity,
            "portal_id": self.portal_id,
            "registered_email": self.registered_email,
            "registered_phone": self.registered_phone,
            "service_account_identity": self.service_account_identity,
            "collected_facts": self.collected_facts,
            "missing_facts": self.missing_facts,
            "already_requested_fields": self.already_requested_fields,
            "customer_statements": self.customer_statements[-12:],
            "troubleshooting_completed": self.troubleshooting_completed,
            "monitoring_results": self.monitoring_results[-8:],
            "tool_executions": self.tool_executions[-20:],
            "tool_errors": self.tool_errors[-12:],
            "resolution_status": self.resolution_status,
            "escalation_reason": self.escalation_reason,
            "destination_team_id": self.destination_team_id,
            "human_requested": self.human_requested,
            "handoff_status": self.handoff_status,
            "start_time": self.start_time,
            "turn_count": self.turn_count,
            "clarification_count": self.clarification_count,
            "question_history": [
                question.to_dict() for question in self.question_history[-12:]
            ],
            "acknowledged_issue_key": self.acknowledged_issue_key,
            "last_response_source": self.last_response_source,
        }


@dataclass(frozen=True, slots=True)
class ConversationEngineDecision:
    action: str
    state: ConversationalState
    response_text: str | None = None
    handoff_summary: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


TOOL_CATALOG: dict[str, ToolDescriptor] = {
    "customer_lookup": ToolDescriptor(
        key="customer_lookup",
        display_name="Customer lookup",
        description="Find a subscriber by an approved customer identifier.",
        input_schema={
            "identifier_type": "portal_id|registered_email|registered_phone",
            "identifier_value": "string",
        },
        output_schema={"status": "found|not_found|ambiguous|unavailable|unauthorized"},
        permission_requirements=("support:ticket:read",),
        timeout_seconds=5,
        read_only=True,
        allowed_contexts=("ai_intake",),
    ),
    "subscriber_monitoring": ToolDescriptor(
        key="subscriber_monitoring",
        display_name="Subscriber monitoring",
        description="Read current subscriber network footprint and session state.",
        input_schema={"subscriber_id": "uuid"},
        output_schema={"status": "available|unavailable|unauthorized"},
        permission_requirements=("support:ticket:read",),
        timeout_seconds=5,
        read_only=True,
        allowed_contexts=("ai_intake",),
    ),
}


def conversational_engine_enabled(version: AiIntakePolicyVersion | None) -> bool:
    metadata = dict(version.metadata_ or {}) if version is not None else {}
    return bool(metadata.get("conversational_engine_enabled"))


def run_conversational_turn(
    db: Session,
    *,
    conversation: InboxConversation,
    session: AiIntakeSession,
    version: AiIntakePolicyVersion | None,
    latest_body: str,
    classification: AiIntakeClassification | None,
    now: datetime | None = None,
    tool_mode: str = "live_read_only",
) -> ConversationEngineDecision:
    state = ConversationalState.load(conversation=conversation, session=session)
    policy = _policy(version, channel=conversation.channel_type)
    now = now or datetime.now(UTC)
    state.turn_count += 1
    _append_statement(state, latest_body)
    facts = extract_facts(latest_body)
    _merge_facts(state, facts)
    _merge_classification(state, classification)
    latest_facts = dict(facts)
    if classification is not None:
        latest_facts.update(
            _meaningful_understanding_facts(classification.message_facts)
        )
    _link_latest_answer(
        state, latest_body=latest_body, latest_facts=latest_facts, now=now
    )

    if state.current_intent != state.previous_intent and state.previous_intent:
        _record_event(session, "intent_changed", now, state=state)

    if state.human_requested:
        return _handoff_decision(
            policy,
            state,
            reason="human_requested",
            response=_handoff_response(
                policy,
                default="I will pass this to a support agent now.",
            ),
        )

    _merge_contact_from_conversation(state, conversation, db)

    _identify_customer(
        db,
        state=state,
        conversation=conversation,
        policy=policy,
        tool_mode=tool_mode,
    )

    max_turns = _bounded_int(
        policy.get("max_turns"), default=session.max_turns, low=1, high=10
    )
    if state.turn_count > max_turns:
        if _should_retry_missing_identifier_response(state, policy, facts):
            requested = _requested_identifier_label(state, policy)
            state.missing_facts = _with_unique(state.missing_facts, requested)
            state.already_requested_fields = _with_unique(
                state.already_requested_fields, requested
            )
            state.clarification_count += 1
            _record_missing_identifier_retry(state)
            return ConversationEngineDecision(
                action="respond",
                state=state,
                response_text=_identifier_retry_question(requested),
                metadata={
                    "reason": "identifier_reply_missing_value",
                    "question_key": requested,
                    "next_action": "ask_question",
                    "response_source": "template",
                },
            )
        return _handoff_decision(
            policy,
            state,
            reason="turn_limit",
            response=_handoff_response(
                policy,
                default=_turn_limit_handoff_response(state, policy),
            ),
        )

    if _requires_identity_before_tools(state, policy):
        requested_identifier = _next_identifier_to_request(state, policy)
        if requested_identifier is not None:
            state.missing_facts = _with_unique(
                state.missing_facts, requested_identifier
            )
            state.already_requested_fields = _with_unique(
                state.already_requested_fields, requested_identifier
            )
            state.clarification_count += 1
            return ConversationEngineDecision(
                action="respond",
                state=state,
                response_text=_identifier_question(requested_identifier),
                metadata={
                    "reason": "missing_customer_identifier",
                    "question_key": requested_identifier,
                    "next_action": "ask_question",
                    "response_source": "template",
                },
            )
        return _handoff_decision(
            policy,
            state,
            reason="customer_unidentified",
            response=_handoff_response(
                policy,
                default=(
                    "I could not safely identify the account from the details "
                    "provided. I will pass this to the support team."
                ),
            ),
        )

    if _should_run_monitoring(state, policy):
        result, latency_ms = _execute_timed_tool(
            db,
            "subscriber_monitoring",
            {"subscriber_id": state.subscriber_id},
            policy=policy,
            conversation=conversation,
            tool_mode=tool_mode,
        )
        _record_tool_result(
            state, "subscriber_monitoring", result, latency_ms=latency_ms
        )
        if result["status"] == "unavailable" and _tool_failure_requires_handoff(
            policy,
            tool_key="subscriber_monitoring",
            status="unavailable",
        ):
            return _handoff_decision(
                policy,
                state,
                reason="monitoring_unavailable",
                response=_handoff_response(
                    policy,
                    default=(
                        "I could not complete the connection check right now. "
                        "I will pass the details I have collected to the support team."
                    ),
                ),
            )
        if result["status"] == "unauthorized" and _tool_failure_requires_handoff(
            policy,
            tool_key="subscriber_monitoring",
            status="unauthorized",
        ):
            return _handoff_decision(
                policy,
                state,
                reason="monitoring_unauthorized",
                response=_handoff_response(
                    policy,
                    default="I will pass this to the support team for investigation.",
                ),
            )

    playbook_decision = _configured_playbook_decision(
        db,
        state,
        policy,
        conversation=conversation,
        tool_mode=tool_mode,
    )
    if playbook_decision is not None:
        return playbook_decision

    rule_decision = _configured_troubleshooting_decision(
        db,
        state,
        policy,
        conversation=conversation,
        tool_mode=tool_mode,
    )
    if rule_decision is not None:
        return rule_decision

    if _technical_issue(state) and _monitoring_offline(state):
        state.troubleshooting_completed = _with_unique(
            state.troubleshooting_completed, "monitoring_checked"
        )

    follow_up = _next_useful_question(state, policy, now=now)
    if follow_up is not None:
        return ConversationEngineDecision(
            action="respond",
            state=state,
            response_text=follow_up.prompt,
            metadata={
                "reason": "useful_missing_fact",
                "question_key": follow_up.key,
                "expected_fact": follow_up.expected_fact,
                "answer_status": follow_up.answer_status.value,
                "next_action": "ask_question",
                "response_source": "template",
            },
        )

    if _should_handoff_after_classification(state, policy):
        return _handoff_decision(
            policy,
            state,
            reason="classified_ready_for_handoff",
            response=_handoff_response(
                policy,
                default="I have the details needed and will pass this to the right team.",
            ),
        )

    if state.classification_requires_follow_up:
        question = _record_question(
            state,
            key="intent_clarification",
            expected_fact="intent",
            prompt=(
                state.classification_follow_up_question
                or "Could you briefly tell me what you need help with?"
            ),
            now=now,
        )
        return ConversationEngineDecision(
            action="respond",
            state=state,
            response_text=question.prompt,
            metadata={
                "reason": "classifier_clarification",
                "question_key": question.key,
                "expected_fact": question.expected_fact,
                "next_action": "ask_question",
                "response_source": "template",
            },
        )

    return _handoff_decision(
        policy,
        state,
        reason="unsupported_or_troubleshooting_exhausted",
        response=_handoff_response(
            policy,
            default="I will pass the details I have collected to the support team.",
        ),
    )


def persist_state(session: AiIntakeSession, state: ConversationalState) -> None:
    metadata = dict(session.metadata_ or {})
    metadata[STATE_KEY] = state.to_dict()
    session.metadata_ = metadata


def render_handoff_summary(
    state: ConversationalState,
    *,
    version: AiIntakePolicyVersion | None,
    channel: str,
    destination_team_name: str | None = None,
) -> str:
    policy = _policy(version, channel=channel)
    handoff_policy = _dict(policy.get("handoff"))
    template = str(handoff_policy.get("summary_template") or "").strip()
    values = _summary_values(
        state,
        channel=channel,
        destination_team_name=destination_team_name,
    )
    if not template:
        return _default_handoff_summary(values)
    rendered = _render_safe_template(template, values)
    return _strip_empty_summary_lines(rendered)[:2000]


def tool_catalogue_snapshot() -> list[dict[str, object]]:
    return [
        {
            "key": descriptor.key,
            "display_name": descriptor.display_name,
            "description": descriptor.description,
            "input_schema": descriptor.input_schema,
            "output_schema": descriptor.output_schema,
            "permission_requirements": descriptor.permission_requirements,
            "timeout_seconds": descriptor.timeout_seconds,
            "read_only": descriptor.read_only,
            "allowed_contexts": descriptor.allowed_contexts,
            "enabled": descriptor.enabled,
        }
        for descriptor in TOOL_CATALOG.values()
    ]


def extract_facts(text: str) -> dict[str, object]:
    value = str(text or "")
    lowered = value.lower()
    facts: dict[str, object] = {}
    email = EMAIL_RE.search(value)
    if email:
        facts["registered_email"] = normalize_email_identifier(email.group(0))
    phone = PHONE_RE.search(value)
    if phone:
        facts["registered_phone"] = normalize_phone_identifier(phone.group(0))
    portal = PORTAL_RE.search(value)
    if portal:
        facts["portal_id"] = portal.group(1).strip()
    if HUMAN_RE.search(value):
        facts["human_requested"] = True
    if any(
        item in lowered
        for item in ("not browsing", "internet is down", "no internet", "not working")
    ):
        facts["connectivity_problem"] = True
        facts["connectivity_state"] = "down"
    if "slow" in lowered:
        facts["slow_internet"] = True
        facts["connectivity_problem"] = False
        facts["connectivity_state"] = "slow"
    if any(
        item in lowered for item in ("intermittent", "keeps dropping", "on and off")
    ):
        facts["connection_pattern"] = "intermittent"
        facts["connectivity_state"] = "intermittent"
    elif any(item in lowered for item in ("all the time", "constantly", "constant")):
        facts["connection_pattern"] = "constant"
    if any(
        item in lowered for item in ("all devices", "every device", "all my devices")
    ):
        facts["device_scope"] = "all_devices"
    elif any(
        item in lowered for item in ("one device", "only my phone", "only my laptop")
    ):
        facts["device_scope"] = "one_device"
    has_wifi = "wi-fi" in lowered or "wifi" in lowered or "wireless" in lowered
    has_ethernet = any(item in lowered for item in ("ethernet", "wired", "cable"))
    if has_wifi and has_ethernet:
        facts["connection_medium"] = "both"
    elif has_wifi:
        facts["connection_medium"] = "wifi"
    elif has_ethernet:
        facts["connection_medium"] = "ethernet"
    if any(
        item in lowered
        for item in (
            "suspend",
            "suspended",
            "suspending",
            "disconnect",
            "disconnected",
            "barred",
        )
    ):
        facts["account_status_problem"] = True
    if any(
        item in lowered
        for item in (
            "outstanding bill",
            "outstanding balance",
            "what bill",
            "which bill",
            "why are you charging",
        )
    ):
        facts["billing_dispute"] = True
    if "office account" in lowered or "company account" in lowered:
        facts["organization_account"] = True
    if "restart" in lowered or "reboot" in lowered:
        facts["router_restarted"] = True
        facts["restart_attempted"] = True
    outage_context = OUTAGE_CONTEXT_RE.search(value)
    if outage_context:
        issue_started_when = outage_context.group(0).strip()[:80]
        facts["outage_context"] = issue_started_when
        facts["issue_started_when"] = issue_started_when
    if (
        "router is on" in lowered
        or "router on" in lowered
        or "powered on" in lowered
        or "router is powered" in lowered
        or "router powered" in lowered
    ):
        facts["router_powered"] = True
    if "los" in lowered and "red" in lowered:
        if any(item in lowered for item in ("not red", "isn't red", "is not red")):
            facts["los_red"] = False
            facts["los_state"] = "not_red"
        else:
            facts["los_red"] = True
            facts["los_state"] = "red"
    if any(item in lowered for item in ("router is off", "router off", "not powered")):
        facts["router_powered"] = False
    speed = SPEED_TEST_RE.search(value)
    if speed:
        facts["speed_test_download_mbps"] = float(speed.group("down"))
        if speed.group("up"):
            facts["speed_test_upload_mbps"] = float(speed.group("up"))
    if "actually" in lowered and "slow" in lowered and "works" in lowered:
        facts["connectivity_problem"] = False
        facts["slow_internet"] = True
        facts["connectivity_state"] = "slow"
    return facts


def _meaningful_understanding_facts(
    facts: AiIntakeExtractedFacts,
) -> dict[str, object]:
    values = facts.model_dump(mode="json", exclude_none=True)
    meaningful: dict[str, object] = {}
    for key, value in values.items():
        if value in {"unknown", ""} or (key == "human_requested" and value is False):
            continue
        meaningful[key] = value
    return meaningful


def execute_tool(
    db: Session,
    key: str,
    inputs: dict[str, object],
    *,
    policy: dict[str, object],
    conversation: InboxConversation | None = None,
    tool_mode: str = "live_read_only",
) -> dict[str, object]:
    descriptor = TOOL_CATALOG.get(key)
    if descriptor is None or not descriptor.enabled:
        return {"status": "unavailable", "reason": "tool_not_registered"}
    if not _tool_enabled(policy, key):
        return {"status": "unauthorized", "reason": "tool_disabled_by_policy"}
    if tool_mode == "simulation":
        configured_results = policy.get("simulated_tool_results")
        if isinstance(configured_results, dict):
            configured = configured_results.get(key)
            if isinstance(configured, dict):
                return dict(configured)
        return _simulated_tool_result(key, inputs)
    if key == "customer_lookup":
        return _customer_lookup(db, inputs, conversation=conversation)
    if key == "subscriber_monitoring":
        return _subscriber_monitoring(db, inputs)
    return {"status": "unavailable", "reason": "tool_not_implemented"}


def _execute_timed_tool(
    db: Session,
    key: str,
    inputs: dict[str, object],
    *,
    policy: dict[str, object],
    conversation: InboxConversation | None = None,
    tool_mode: str = "live_read_only",
) -> tuple[dict[str, object], int]:
    started = time.perf_counter()
    result = execute_tool(
        db,
        key,
        inputs,
        policy=policy,
        conversation=conversation,
        tool_mode=tool_mode,
    )
    return result, max(0, int((time.perf_counter() - started) * 1000))


def _simulated_tool_result(key: str, inputs: dict[str, object]) -> dict[str, object]:
    if key == "customer_lookup":
        identifier_value = str(inputs.get("identifier_value") or "").strip()
        if not identifier_value:
            return {"status": "not_found", "simulated": True}
        return {
            "status": "found",
            "subscriber_id": "00000000-0000-0000-0000-000000000001",
            "display_name": "Preview customer",
            "account_number": identifier_value,
            "subscriber_status": "preview",
            "simulated": True,
        }
    if key == "subscriber_monitoring":
        return {
            "status": "available",
            "radius_observation": {
                "source": "network.radius_sessions",
                "state": "offline",
                "active_session_count": 0,
                "framed_ip_addresses": [],
                "observed_at": None,
            },
            "ont_observations": [
                {
                    "source": "network.ont_runtime_status",
                    "reference": "preview-ont",
                    "serial_number": None,
                    "effective_state": "offline",
                }
            ],
            "simulated": True,
        }
    return {"status": "unavailable", "reason": "simulation_not_available"}


def _customer_lookup(
    db: Session,
    inputs: dict[str, object],
    *,
    conversation: InboxConversation | None,
) -> dict[str, object]:
    """Verify an identifier only against the trusted Inbox-linked subscriber."""
    if conversation is None:
        return {"status": "unavailable", "reason": "trusted_context_required"}
    identifier_type = str(inputs.get("identifier_type") or "").strip()
    identifier_value = str(inputs.get("identifier_value") or "").strip()
    if not identifier_type or not identifier_value:
        return {"status": "unavailable", "reason": "missing_identifier"}
    identifier_kind = {
        "registered_email": CustomerIdentifierKind.email,
        "registered_phone": CustomerIdentifierKind.phone,
        "portal_id": CustomerIdentifierKind.account_number,
    }.get(identifier_type)
    if identifier_kind is None:
        return {"status": "unauthorized", "reason": "identifier_not_permitted"}
    try:
        result = resolve_customer_identity(
            db,
            CustomerIdentityQuery(
                context=_support_read_context(conversation),
                identifier_kind=identifier_kind,
                identifier_value=identifier_value,
            ),
        )
    except Exception:
        return {"status": "unavailable", "reason": "lookup_failed"}
    if result.status is not CustomerIdentityStatus.found or result.customer is None:
        return {"status": result.status.value}
    return {
        "status": result.status.value,
        "subscriber_id": str(result.customer.subscriber_id),
        "display_name": result.customer.display_name,
        "account_number": result.customer.account_number,
        "subscriber_status": result.customer.status,
    }


def _subscriber_monitoring(db: Session, inputs: dict[str, object]) -> dict[str, object]:
    subscriber_id = coerce_uuid(inputs.get("subscriber_id"))
    if subscriber_id is None:
        return {"status": "unavailable", "reason": "subscriber_required"}
    try:
        projection = support_monitoring.project_support_monitoring(
            db,
            support_monitoring.SupportMonitoringQuery(
                subscriber_id=subscriber_id,
                authorized=True,
            ),
        )
    except Exception:
        return {"status": "unavailable", "reason": "monitoring_query_failed"}
    result: dict[str, object] = {"status": projection.status.value}
    if projection.radius is not None:
        result["radius_observation"] = {
            "source": projection.radius.source,
            "state": projection.radius.state,
            "active_session_count": projection.radius.active_session_count,
            "framed_ip_addresses": list(projection.radius.framed_ip_addresses),
            "observed_at": (
                projection.radius.observed_at.isoformat()
                if projection.radius.observed_at is not None
                else None
            ),
        }
    if projection.onts:
        result["ont_observations"] = [
            {
                "source": observation.source,
                "reference": observation.reference,
                "serial_number": observation.serial_number,
                "effective_state": observation.effective_state,
            }
            for observation in projection.onts
        ]
    return result


def _policy(
    version: AiIntakePolicyVersion | None, *, channel: str | None = None
) -> dict[str, object]:
    metadata = dict(version.metadata_ or {}) if version is not None else {}
    policy = dict(metadata.get("conversation_policy") or {})
    conversation_templates = _dict(metadata.get("conversation_templates"))
    standard_handoff = str(conversation_templates.get("standard_handoff") or "").strip()
    policy["tools"] = metadata.get("tools") or policy.get("tools") or {}
    policy["intent_definitions"] = (
        (version.intent_definitions if version is not None else None)
        or metadata.get("intent_definitions")
        or policy.get("intent_definitions")
        or []
    )
    policy["business_tone"] = str(
        (version.business_tone if version is not None else None)
        or metadata.get("business_tone")
        or policy.get("business_tone")
        or ""
    ).strip()
    policy["approved_isp_information"] = str(
        (version.approved_isp_information if version is not None else None)
        or metadata.get("approved_isp_information")
        or policy.get("approved_isp_information")
        or ""
    ).strip()
    for key in PLAYBOOK_POLICY_KEYS:
        if key not in policy and isinstance(metadata.get(key), list):
            policy[key] = metadata[key]
    if channel:
        overrides = _dict(metadata.get("channel_overrides"))
        channel_override = _dict(overrides.get(channel))
        for key in (
            "business_tone",
            "approved_isp_information",
            "troubleshooting_rules",
            "playbooks",
            "first_line_playbooks",
            "handoff",
        ):
            if key in channel_override:
                policy[key] = channel_override[key]
    policy["permitted_identifiers"] = (
        metadata.get("permitted_identifiers")
        or policy.get("permitted_identifiers")
        or DEFAULT_IDENTIFIER_REQUEST_ORDER
    )
    policy["require_identity_before_tools"] = bool(
        policy.get("require_identity_before_tools", True)
    )
    handoff_policy = _dict(policy.get("handoff"))
    policy["handoff"] = {
        "customer_message": str(
            handoff_policy.get("customer_message") or standard_handoff
        ).strip(),
        "summary_template": str(handoff_policy.get("summary_template") or "").strip(),
        "announce_destination": bool(handoff_policy.get("announce_destination")),
    }
    if "troubleshooting_rules" not in policy:
        policy["troubleshooting_rules"] = [
            {
                "condition": {"fact": "los_red", "equals": True},
                "action": "handoff",
                "reason": "red_los",
                "response": (
                    "Thanks for confirming. A red LOS light usually needs "
                    "technical support, so I will pass this to the team now."
                ),
            }
        ]
    return policy


def _merge_contact_from_conversation(
    state: ConversationalState, conversation: InboxConversation, db: Session
) -> None:
    if state.subscriber_id and state.service_account_identity:
        return
    try:
        result = resolve_customer_identity(
            db,
            CustomerIdentityQuery(
                context=_support_read_context(conversation),
                identifier_kind=CustomerIdentifierKind.inbox_linked,
            ),
        )
    except Exception:
        return
    if result.status is not CustomerIdentityStatus.found or result.customer is None:
        return
    customer = result.customer
    state.subscriber_id = str(customer.subscriber_id)
    state.portal_id = state.portal_id or customer.account_number
    state.service_account_identity = {
        "subscriber_id": state.subscriber_id,
        "display_name": customer.display_name,
        "subscriber_status": customer.status,
    }


def _support_read_context(conversation: InboxConversation) -> SupportReadContext:
    """Build owner input from the trusted Team Inbox runtime context, never AI output."""
    return SupportReadContext(
        conversation_id=conversation.id,
        actor_person_id=None,
        can_read_support_context=True,
    )


def _merge_facts(state: ConversationalState, facts: dict[str, object]) -> None:
    for key, value in facts.items():
        if key == "registered_email" and value:
            state.registered_email = str(value)
        elif key == "registered_phone" and value:
            state.registered_phone = str(value)
        elif key == "portal_id" and value:
            state.portal_id = str(value)
        elif key == "human_requested":
            if value:
                state.human_requested = True
        else:
            state.collected_facts[key] = value
    connectivity_state = str(facts.get("connectivity_state") or "")
    if connectivity_state == "working":
        state.collected_facts["connectivity_problem"] = False
        state.collected_facts["slow_internet"] = False
    elif connectivity_state == "slow":
        state.collected_facts["connectivity_problem"] = False
        state.collected_facts["slow_internet"] = True
    elif connectivity_state == "down":
        state.collected_facts["connectivity_problem"] = True
        state.collected_facts["slow_internet"] = False


def _merge_classification(
    state: ConversationalState, classification: AiIntakeClassification | None
) -> None:
    if classification is None:
        return
    next_intent = classification.intent.value
    if state.current_intent and state.current_intent != next_intent:
        state.previous_intent = state.current_intent
    next_category = classification.category.value
    issue_changed = bool(
        (state.current_intent and state.current_intent != next_intent)
        or (state.category and state.category != next_category)
    )
    state.current_intent = next_intent
    state.category = next_category
    if issue_changed:
        state.acknowledged_issue_key = None
    state.confidence = classification.confidence
    state.classification_requires_follow_up = classification.requires_follow_up
    state.classification_follow_up_question = classification.follow_up_question
    _merge_facts(state, _meaningful_understanding_facts(classification.message_facts))


QUESTION_PROMPTS: dict[str, str] = {
    "issue_started_when": "When did the problem start?",
    "device_scope": "Is the issue affecting every device or only one device?",
    "connection_medium": (
        "Is the issue the same over Wi-Fi and a wired Ethernet connection?"
    ),
    "connection_pattern": "Is the issue constant, or does it come and go?",
    "router_powered": "Is your router or ONU powered on right now?",
    "restart_attempted": "Have you restarted the router since the issue began?",
    "los_state": "Is the LOS light on the ONU red, off, or not showing red?",
}


def _canonical_fact_key(field: str) -> str:
    return {
        "outage_context": "issue_started_when",
        "los_status": "los_state",
        "router_restarted": "restart_attempted",
    }.get(field, field)


def _fact_is_known(state: ConversationalState, field: str) -> bool:
    key = _canonical_fact_key(field)
    if key in {"portal_id", "registered_email", "registered_phone"}:
        return bool(getattr(state, key))
    value = state.collected_facts.get(key)
    return value not in (None, "", "unknown")


def _link_latest_answer(
    state: ConversationalState,
    *,
    latest_body: str,
    latest_facts: dict[str, object],
    now: datetime,
) -> None:
    pending = next(
        (
            question
            for question in reversed(state.question_history)
            if question.answer_status
            in {AiIntakeAnswerStatus.pending, AiIntakeAnswerStatus.unclear}
        ),
        None,
    )
    if pending is None:
        return
    expected = _canonical_fact_key(pending.expected_fact)
    if _fact_is_known(state, expected) and (
        expected in latest_facts
        or pending.expected_fact in latest_facts
        or expected in _meaningful_latest_model_fact_keys(state, expected)
    ):
        pending.answer_status = (
            AiIntakeAnswerStatus.corrected
            if "actually" in latest_body.lower()
            else AiIntakeAnswerStatus.answered
        )
        pending.answered_at = now.isoformat()
        state.missing_facts = [
            item
            for item in state.missing_facts
            if _canonical_fact_key(item) != expected
        ]
        return
    lowered = latest_body.strip().lower()
    if any(
        phrase in lowered
        for phrase in ("don't know", "do not know", "not sure", "no idea")
    ):
        pending.answer_status = AiIntakeAnswerStatus.unclear
    elif any(
        phrase in lowered
        for phrase in ("rather not", "prefer not", "won't say", "will not say")
    ):
        pending.answer_status = AiIntakeAnswerStatus.declined
        pending.answered_at = now.isoformat()
    else:
        pending.answer_status = AiIntakeAnswerStatus.partially_answered


def _meaningful_latest_model_fact_keys(
    state: ConversationalState, expected: str
) -> set[str]:
    # The merged state is authoritative; this hook deliberately does not infer
    # that an unrelated sentence answered the pending question.
    return {expected} if expected == "intent" and bool(state.current_intent) else set()


def _record_question(
    state: ConversationalState,
    *,
    key: str,
    expected_fact: str,
    prompt: str,
    now: datetime,
) -> QuestionState:
    existing = next(
        (item for item in reversed(state.question_history) if item.key == key), None
    )
    if existing is not None:
        existing.attempts = min(2, existing.attempts + 1)
        existing.answer_status = AiIntakeAnswerStatus.pending
        existing.asked_at = now.isoformat()
        existing.prompt = prompt
        return existing
    question = QuestionState(
        key=key,
        expected_fact=expected_fact,
        prompt=prompt,
        asked_at=now.isoformat(),
    )
    state.question_history.append(question)
    state.already_requested_fields = _with_unique(
        state.already_requested_fields, expected_fact
    )
    state.missing_facts = _with_unique(state.missing_facts, expected_fact)
    state.clarification_count += 1
    return question


def _required_question_keys(
    state: ConversationalState, policy: dict[str, object]
) -> tuple[str, ...]:
    configured: list[str] = []
    for raw in _list(policy.get("intent_definitions")):
        definition = _dict(raw)
        if str(definition.get("intent") or definition.get("key") or "") != str(
            state.current_intent or ""
        ):
            continue
        for item in _list(definition.get("required_fields")):
            key = _canonical_fact_key(str(item).strip())
            if key in QUESTION_PROMPTS and key not in configured:
                configured.append(key)
    if configured:
        return tuple(configured)
    if not _technical_issue(state):
        return ()
    if _monitoring_offline(state):
        return ("router_powered", "los_state", "issue_started_when", "device_scope")
    connectivity = str(state.collected_facts.get("connectivity_state") or "")
    if connectivity == "slow" or state.collected_facts.get("slow_internet"):
        return (
            "device_scope",
            "issue_started_when",
            "connection_medium",
            "connection_pattern",
        )
    if connectivity == "intermittent":
        return ("issue_started_when", "device_scope", "connection_medium")
    return ("issue_started_when", "device_scope", "router_powered", "los_state")


def _next_useful_question(
    state: ConversationalState, policy: dict[str, object], *, now: datetime
) -> QuestionState | None:
    pending = next(
        (
            question
            for question in reversed(state.question_history)
            if question.answer_status
            in {
                AiIntakeAnswerStatus.pending,
                AiIntakeAnswerStatus.unclear,
                AiIntakeAnswerStatus.partially_answered,
            }
        ),
        None,
    )
    if pending is not None and not _fact_is_known(state, pending.expected_fact):
        if pending.attempts < 2 and pending.answer_status in {
            AiIntakeAnswerStatus.unclear,
            AiIntakeAnswerStatus.partially_answered,
        }:
            prompt = "No problem. " + QUESTION_PROMPTS.get(
                pending.expected_fact, pending.prompt
            )
            return _record_question(
                state,
                key=pending.key,
                expected_fact=pending.expected_fact,
                prompt=prompt,
                now=now,
            )
        if pending.answer_status is AiIntakeAnswerStatus.pending:
            return pending
    for fact_key in _required_question_keys(state, policy):
        if _fact_is_known(state, fact_key):
            continue
        prior = next(
            (item for item in reversed(state.question_history) if item.key == fact_key),
            None,
        )
        if prior is not None and (
            prior.attempts >= 2 or prior.answer_status is AiIntakeAnswerStatus.declined
        ):
            continue
        return _record_question(
            state,
            key=fact_key,
            expected_fact=fact_key,
            prompt=QUESTION_PROMPTS[fact_key],
            now=now,
        )
    return None


def _identify_customer(
    db: Session,
    *,
    state: ConversationalState,
    conversation: InboxConversation,
    policy: dict[str, object],
    tool_mode: str,
) -> None:
    if state.subscriber_id:
        return
    for identifier_type in _permitted_identifiers(policy):
        value = _identifier_value(state, identifier_type)
        if not value:
            continue
        result, latency_ms = _execute_timed_tool(
            db,
            "customer_lookup",
            {
                "identifier_type": identifier_type,
                "identifier_value": value,
                "conversation_id": str(conversation.id),
            },
            policy=policy,
            conversation=conversation,
            tool_mode=tool_mode,
        )
        _record_tool_result(state, "customer_lookup", result, latency_ms=latency_ms)
        if result.get("status") == "found":
            state.subscriber_id = str(result["subscriber_id"])
            state.service_account_identity = {
                "subscriber_id": state.subscriber_id,
                "display_name": result.get("display_name"),
                "subscriber_status": result.get("subscriber_status"),
            }
            return
        if result.get("status") in {"ambiguous", "unavailable", "unauthorized"}:
            return


def _record_tool_result(
    state: ConversationalState,
    key: str,
    result: dict[str, object],
    *,
    latency_ms: int | None = None,
) -> None:
    result_payload: dict[str, object] = {
        item_key: item_value
        for item_key, item_value in result.items()
        if item_key not in {"email", "phone"}
    }
    entry: dict[str, object] = {
        "tool": key,
        "status": result.get("status"),
        "at": datetime.now(UTC).isoformat(),
        "result": result_payload,
        "latency_ms": latency_ms,
    }
    state.tool_executions.append(entry)
    if result.get("status") in {"unavailable", "unauthorized"}:
        state.tool_errors.append(entry)
    if key == "subscriber_monitoring":
        state.monitoring_results.append(result_payload)


def _requires_identity_before_tools(
    state: ConversationalState, policy: dict[str, object]
) -> bool:
    return (
        _account_context_required(state)
        and not state.subscriber_id
        and bool(policy.get("require_identity_before_tools", True))
    )


def _should_retry_missing_identifier_response(
    state: ConversationalState,
    policy: dict[str, object],
    latest_facts: dict[str, object],
) -> bool:
    if not _requires_identity_before_tools(state, policy):
        return False
    if _latest_reply_supplied_identifier(latest_facts, policy):
        return False
    if not _identifier_was_requested(state, policy):
        return False
    return _missing_identifier_retry_count(state) < 1


def _latest_reply_supplied_identifier(
    latest_facts: dict[str, object], policy: dict[str, object]
) -> bool:
    return any(
        latest_facts.get(identifier) for identifier in _permitted_identifiers(policy)
    )


def _identifier_was_requested(
    state: ConversationalState, policy: dict[str, object]
) -> bool:
    requested = set(state.already_requested_fields)
    if CUSTOMER_IDENTIFIER_REQUEST in requested:
        return True
    return any(identifier in requested for identifier in _permitted_identifiers(policy))


def _requested_identifier_label(
    state: ConversationalState, policy: dict[str, object]
) -> str:
    for identifier in _permitted_identifiers(policy):
        if identifier in state.already_requested_fields:
            return identifier
    if CUSTOMER_IDENTIFIER_REQUEST in state.already_requested_fields:
        return _permitted_identifiers(policy)[0]
    return _permitted_identifiers(policy)[0]


def _missing_identifier_retry_count(state: ConversationalState) -> int:
    value = state.collected_facts.get("missing_identifier_retry_count")
    if not isinstance(value, int | str):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _record_missing_identifier_retry(state: ConversationalState) -> None:
    state.collected_facts["missing_identifier_retry_count"] = (
        _missing_identifier_retry_count(state) + 1
    )


def _account_context_required(state: ConversationalState) -> bool:
    return (
        _technical_issue(state)
        or (state.current_intent or "") in ACCOUNT_BOUND_INTENTS
        or (state.category or "") in ACCOUNT_BOUND_CATEGORIES
        or bool(
            state.collected_facts.get("account_status_problem")
            or state.collected_facts.get("billing_dispute")
        )
    )


def _should_run_monitoring(
    state: ConversationalState, policy: dict[str, object]
) -> bool:
    if not _technical_issue(state) or not state.subscriber_id:
        return False
    if any(
        item.get("tool") == "subscriber_monitoring" for item in state.tool_executions
    ):
        return False
    return _tool_enabled_for_intent(
        policy, "subscriber_monitoring", state.current_intent
    )


def _technical_issue(state: ConversationalState) -> bool:
    return state.current_intent == "technical_support" or bool(
        state.collected_facts.get("connectivity_problem")
        or state.collected_facts.get("slow_internet")
    )


def _monitoring_offline(state: ConversationalState) -> bool:
    latest = state.monitoring_results[-1] if state.monitoring_results else {}
    radius = latest.get("radius_observation")
    return isinstance(radius, dict) and radius.get("state") == "offline"


def _configured_playbook_decision(
    db: Session,
    state: ConversationalState,
    policy: dict[str, object],
    *,
    conversation: InboxConversation,
    tool_mode: str,
) -> ConversationEngineDecision | None:
    for playbook in _matching_playbooks(state, policy):
        steps = playbook.get("steps")
        if not isinstance(steps, list):
            continue
        playbook_key = _playbook_key(playbook)
        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, dict):
                continue
            condition = raw_step.get("condition")
            if isinstance(condition, dict) and not _condition_matches(state, condition):
                continue
            action = str(raw_step.get("action") or "").strip()
            if action in {"execute_tool", "invoke_tool"}:
                tool_key = str(raw_step.get("tool") or "").strip()
                if not tool_key:
                    continue
                if not _tool_enabled_for_intent(policy, tool_key, state.current_intent):
                    continue
                if any(item.get("tool") == tool_key for item in state.tool_executions):
                    continue
                inputs: dict[str, object] = {}
                if tool_key == "subscriber_monitoring":
                    if not state.subscriber_id:
                        continue
                    inputs["subscriber_id"] = state.subscriber_id
                result, latency_ms = _execute_timed_tool(
                    db,
                    tool_key,
                    inputs,
                    policy=policy,
                    conversation=conversation,
                    tool_mode=tool_mode,
                )
                _record_tool_result(state, tool_key, result, latency_ms=latency_ms)
                continue
            if action in {"respond", "provide_guidance"}:
                step_key = _playbook_step_key(playbook_key, index, raw_step)
                if step_key in state.troubleshooting_completed:
                    continue
                response = str(raw_step.get("response") or "").strip()
                if response:
                    state.troubleshooting_completed = _with_unique(
                        state.troubleshooting_completed,
                        step_key,
                    )
                    return ConversationEngineDecision(
                        action="respond",
                        state=state,
                        response_text=_playbook_response(playbook, state, response),
                        metadata={
                            "reason": str(
                                raw_step.get("reason") or "playbook_guidance"
                            ),
                            "playbook": playbook_key,
                            "next_action": "provide_guidance",
                            "response_source": "playbook",
                        },
                    )
            if action == "request_field":
                field = str(raw_step.get("field") or raw_step.get("tool") or "").strip()
                if not field:
                    continue
                if _field_value(state, field) not in (None, "", False):
                    continue
                if _fact_is_known(state, field):
                    continue
                prior = next(
                    (
                        item
                        for item in reversed(state.question_history)
                        if item.key == field
                    ),
                    None,
                )
                if prior is not None and prior.attempts >= 2:
                    return None
                response = str(raw_step.get("response") or _field_question(field))
                question = _record_question(
                    state,
                    key=field,
                    expected_fact=_canonical_fact_key(field),
                    prompt=_playbook_response(playbook, state, response),
                    now=datetime.now(UTC),
                )
                return ConversationEngineDecision(
                    action="respond",
                    state=state,
                    response_text=question.prompt,
                    metadata={
                        "reason": str(
                            raw_step.get("reason") or "playbook_required_field"
                        ),
                        "playbook": playbook_key,
                        "question_key": question.key,
                        "expected_fact": question.expected_fact,
                        "next_action": "ask_question",
                        "response_source": "playbook",
                    },
                )
            if action == "mark_resolved":
                state.resolution_status = "resolved"
                response = str(raw_step.get("response") or "").strip()
                return ConversationEngineDecision(
                    action="resolved",
                    state=state,
                    response_text=response
                    or "Thanks. I have recorded this as resolved from the details provided.",
                    metadata={
                        "reason": str(raw_step.get("reason") or "playbook_resolved"),
                        "playbook": playbook_key,
                        "next_action": "resolve",
                        "response_source": "playbook",
                    },
                )
            if action == "handoff":
                return _handoff_decision(
                    policy,
                    state,
                    reason=str(raw_step.get("reason") or "playbook_handoff"),
                    response=_handoff_response(
                        policy,
                        default=str(
                            raw_step.get("response")
                            or "I will pass this to the support team for investigation."
                        ),
                    ),
                )
    return None


def _matching_playbooks(
    state: ConversationalState, policy: dict[str, object]
) -> tuple[dict[str, object], ...]:
    raw_playbooks: object = None
    for key in PLAYBOOK_POLICY_KEYS:
        raw_playbooks = policy.get(key)
        if isinstance(raw_playbooks, list):
            break
    if not isinstance(raw_playbooks, list):
        return ()
    matched: list[dict[str, object]] = []
    for raw in raw_playbooks:
        if not isinstance(raw, dict):
            continue
        intent = str(raw.get("intent") or "").strip()
        if intent and intent != state.current_intent:
            continue
        category = str(raw.get("category") or "").strip()
        if category and category != state.category:
            continue
        matched.append(raw)
    return tuple(matched)


def _playbook_key(playbook: dict[str, object]) -> str:
    return str(
        playbook.get("key")
        or playbook.get("name")
        or playbook.get("category")
        or playbook.get("intent")
        or "default"
    ).strip()[:80]


def _playbook_step_key(playbook_key: str, index: int, step: dict[str, object]) -> str:
    raw_key = str(step.get("key") or step.get("id") or "").strip()
    if raw_key:
        return f"playbook:{playbook_key}:{raw_key}"[:120]
    return f"playbook:{playbook_key}:step:{index}"


def _playbook_response(
    playbook: dict[str, object], state: ConversationalState, response: str
) -> str:
    text = " ".join(str(response or "").split())[:800]
    prefix = str(
        playbook.get("acknowledgement") or playbook.get("empathy_prefix") or ""
    ).strip()
    prefix_key = f"playbook_ack:{_playbook_key(playbook)}"
    if prefix and prefix_key not in state.troubleshooting_completed:
        state.troubleshooting_completed = _with_unique(
            state.troubleshooting_completed,
            prefix_key,
        )
        if not text.lower().startswith(prefix.lower()):
            text = f"{prefix} {text}".strip()
    return text[:800]


def _field_question(field: str) -> str:
    if field in SUPPORTED_IDENTIFIER_TYPES:
        return _identifier_question(field)
    if field == "router_powered":
        return "Is your router or ONU powered on right now?"
    if field == "los_status":
        return "Are you seeing a red LOS warning light on the ONU?"
    if field == "router_restarted":
        return "Have you restarted the router recently?"
    if field == "outage_context":
        return "How long has this been happening?"
    return "Please share that detail so I can continue checking this."


def _configured_troubleshooting_decision(
    db: Session,
    state: ConversationalState,
    policy: dict[str, object],
    *,
    conversation: InboxConversation,
    tool_mode: str,
) -> ConversationEngineDecision | None:
    rules = policy.get("troubleshooting_rules")
    if not isinstance(rules, list):
        return None
    for raw in rules:
        if not isinstance(raw, dict):
            continue
        if raw.get("enabled") is False:
            continue
        condition = raw.get("condition")
        if not isinstance(condition, dict):
            continue
        action = str(raw.get("action") or "").strip()
        if state.turn_count <= 1 and _handoff_rule_matches_first_turn(
            action, condition
        ):
            continue
        if not _condition_matches(state, condition):
            continue
        if action in {"execute_tool", "invoke_tool"}:
            tool_key = str(raw.get("tool") or "").strip()
            if not tool_key:
                continue
            if not _tool_enabled_for_intent(policy, tool_key, state.current_intent):
                continue
            if any(item.get("tool") == tool_key for item in state.tool_executions):
                continue
            inputs: dict[str, object] = {}
            if tool_key == "subscriber_monitoring":
                if not state.subscriber_id:
                    continue
                inputs["subscriber_id"] = state.subscriber_id
            result, latency_ms = _execute_timed_tool(
                db,
                tool_key,
                inputs,
                policy=policy,
                conversation=conversation,
                tool_mode=tool_mode,
            )
            _record_tool_result(state, tool_key, result, latency_ms=latency_ms)
            continue
        if action in {"respond", "provide_guidance"}:
            response = str(raw.get("response") or "").strip()
            if response:
                return ConversationEngineDecision(
                    action="respond",
                    state=state,
                    response_text=response[:800],
                    metadata={
                        "reason": "troubleshooting_guidance",
                        "next_action": "provide_guidance",
                        "response_source": "playbook",
                    },
                )
        if action == "request_field":
            field = str(raw.get("field") or raw.get("tool") or "").strip()
            if field and not _fact_is_known(state, field):
                response = str(raw.get("response") or _field_question(field))
                question = _record_question(
                    state,
                    key=field,
                    expected_fact=_canonical_fact_key(field),
                    prompt=response,
                    now=datetime.now(UTC),
                )
                return ConversationEngineDecision(
                    action="respond",
                    state=state,
                    response_text=question.prompt,
                    metadata={
                        "reason": "troubleshooting_required_field",
                        "question_key": question.key,
                        "expected_fact": question.expected_fact,
                        "next_action": "ask_question",
                        "response_source": "playbook",
                    },
                )
        if action == "mark_resolved":
            state.resolution_status = "resolved"
            response = str(raw.get("response") or "").strip()
            return ConversationEngineDecision(
                action="resolved",
                state=state,
                response_text=response
                or "Thanks. I have recorded this as resolved from the details provided.",
                metadata={
                    "reason": "troubleshooting_resolved",
                    "next_action": "resolve",
                    "response_source": "playbook",
                },
            )
        if action == "handoff":
            return _handoff_decision(
                policy,
                state,
                reason=str(raw.get("reason") or "troubleshooting_rule"),
                response=_handoff_response(
                    policy,
                    default=str(
                        raw.get("response")
                        or "I will pass this to the support team for investigation."
                    ),
                ),
            )
    return None


def _handoff_rule_matches_first_turn(
    action: str, condition: dict[str, object] | Mapping[str, object]
) -> bool:
    if action != "handoff":
        return False
    if str(condition.get("type") or "").strip() != "turn_count":
        return False
    return _compare_number(1, dict(condition))


def _condition_matches(
    state: ConversationalState, condition: dict[str, object]
) -> bool:
    condition_type = str(condition.get("type") or "").strip()
    if condition_type == "intent":
        return _compare_value(
            state.current_intent,
            {**condition, "value": condition.get("value", condition.get("intent"))},
        )
    if condition_type == "category":
        return _compare_value(
            state.category,
            {**condition, "value": condition.get("value", condition.get("category"))},
        )
    if condition_type == "customer_identified":
        return bool(state.subscriber_id) is bool(condition.get("customer_identified"))
    if condition_type == "human_requested":
        return state.human_requested is bool(condition.get("human_requested"))
    if condition_type == "turn_count":
        return _compare_number(state.turn_count, condition)
    if condition_type == "field_present":
        field = str(condition.get("field") or "").strip()
        return _field_value(state, field) not in (None, "", False)
    if condition_type == "field_value":
        return _compare_value(
            _field_value(state, str(condition.get("field") or "").strip()),
            condition,
        )
    if condition_type == "monitoring_status":
        latest = state.monitoring_results[-1] if state.monitoring_results else {}
        return _compare_value(
            str(latest.get("status") or "") or None,
            {
                **condition,
                "value": condition.get("value", condition.get("monitoring_status")),
            },
        )
    if condition_type == "radius_status":
        latest = state.monitoring_results[-1] if state.monitoring_results else {}
        radius = latest.get("radius_observation")
        radius_state = (
            str(radius.get("state") or "") if isinstance(radius, dict) else ""
        )
        return _compare_value(radius_state or None, condition)
    if condition_type == "ont_status":
        latest = state.monitoring_results[-1] if state.monitoring_results else {}
        onts = latest.get("ont_observations")
        return isinstance(onts, list) and any(
            _compare_value(str(item.get("effective_state") or "") or None, condition)
            for item in onts
            if isinstance(item, dict)
        )
    if condition_type == "tool_result":
        tool = str(condition.get("tool") or "").strip()
        status = str(condition.get("status") or condition.get("value") or "").strip()
        return any(
            item.get("tool") == tool and str(item.get("status") or "") == status
            for item in state.tool_executions
        )
    recognized = False
    fact_key = str(condition.get("fact") or "").strip()
    if fact_key:
        recognized = True
        value = state.collected_facts.get(fact_key)
        expected = condition.get("equals")
        return value == expected if "equals" in condition else bool(value)
    intent = condition.get("intent")
    if intent is not None:
        recognized = True
    if intent is not None and state.current_intent != str(intent):
        return False
    category = condition.get("category")
    if category is not None:
        recognized = True
    if category is not None and state.category != str(category):
        return False
    return recognized


def _field_value(state: ConversationalState, field: str) -> object:
    if field in {"portal_id", "registered_email", "registered_phone"}:
        return getattr(state, field)
    return state.collected_facts.get(field)


def _compare_value(value: object, condition: dict[str, object]) -> bool:
    expected = condition.get("value", condition.get("equals"))
    operator = str(condition.get("operator") or "equals").strip()
    if operator in {"is", "equals", "=="}:
        return str(value or "").strip() == str(expected or "").strip()
    if operator == "contains":
        return str(expected or "").strip().lower() in str(value or "").lower()
    if operator == "present":
        return value not in (None, "", False)
    return False


def _compare_number(value: int, condition: dict[str, object]) -> bool:
    try:
        expected = int(str(condition.get("turn_count", condition.get("value", 0))))
    except (TypeError, ValueError):
        return False
    operator = str(condition.get("operator") or ">=").strip()
    if operator == ">=":
        return value >= expected
    if operator == ">":
        return value > expected
    if operator in {"=", "==", "equals"}:
        return value == expected
    if operator == "<=":
        return value <= expected
    if operator == "<":
        return value < expected
    return False


def _should_handoff_after_classification(
    state: ConversationalState, policy: dict[str, object]
) -> bool:
    if not bool(policy.get("handoff_after_classification", False)):
        return False
    if state.classification_requires_follow_up:
        return False
    return bool(state.current_intent and state.confidence is not None)


def _handoff_decision(
    policy: dict[str, object], state: ConversationalState, *, reason: str, response: str
) -> ConversationEngineDecision:
    state.escalation_reason = reason
    state.handoff_status = "requested"
    state.resolution_status = "escalated"
    return ConversationEngineDecision(
        action="handoff",
        state=state,
        response_text=response,
        handoff_summary=render_handoff_summary(
            state,
            version=None,
            channel=state.channel,
            destination_team_name=None,
        )
        if not _dict(policy.get("handoff")).get("summary_template")
        else None,
        metadata={"reason": reason},
    )


def _handoff_response(policy: dict[str, object], *, default: str) -> str:
    configured = _dict(policy.get("handoff")).get("customer_message")
    text = str(configured or "").strip()
    return text[:800] if text else default


def _summary_values(
    state: ConversationalState,
    *,
    channel: str,
    destination_team_name: str | None,
) -> dict[str, str]:
    details = "\n".join(f"- {item}" for item in state.customer_statements[-5:])
    facts = ", ".join(
        f"{key}={value}" for key, value in sorted(state.collected_facts.items())
    )
    latest_monitoring = state.monitoring_results[-1] if state.monitoring_results else {}
    monitoring = ""
    if latest_monitoring:
        radius = _dict(latest_monitoring.get("radius_observation"))
        ont_states = sorted(
            {
                str(item.get("effective_state") or "").strip()
                for item in _list(latest_monitoring.get("ont_observations"))
                if isinstance(item, Mapping)
                and str(item.get("effective_state") or "").strip()
            }
        )
        monitoring = "; ".join(
            f"{key}={value}"
            for key, value in (
                ("status", latest_monitoring.get("status")),
                ("radius_state", radius.get("state")),
                ("ont_states", ",".join(ont_states) if ont_states else None),
            )
            if value is not None
        )
    tool_results = []
    for execution in state.tool_executions[-6:]:
        result = _dict(execution.get("result"))
        tool_results.append(
            f"{execution.get('tool')}: {execution.get('status')}"
            + (f" ({result.get('reason')})" if result.get("reason") is not None else "")
        )
    customer = (
        str(state.service_account_identity.get("display_name") or "").strip()
        or str(state.contact_identity.get("display_name") or "").strip()
    )
    account = (
        state.portal_id
        or str(state.service_account_identity.get("subscriber_id") or "").strip()
    )
    issue = state.category or state.current_intent or facts
    return {
        "customer": customer,
        "account": account,
        "portal_id": state.portal_id or "",
        "channel": channel,
        "issue": str(issue or ""),
        "intent": state.current_intent or "",
        "category": state.category or "",
        "customer_details": details,
        "collected_facts": facts,
        "monitoring_findings": monitoring,
        "troubleshooting": ", ".join(state.troubleshooting_completed),
        "tool_results": "\n".join(f"- {item}" for item in tool_results),
        "escalation_reason": state.escalation_reason or "",
        "destination_team": destination_team_name or state.destination_team_id or "",
    }


def _default_handoff_summary(values: dict[str, str]) -> str:
    sections = [
        ("AI Intake Summary", "AI Intake Summary"),
        ("Customer", values["customer"]),
        ("Account / Portal ID", values["account"] or values["portal_id"]),
        ("Channel", values["channel"]),
        ("Issue", values["issue"]),
        ("Detected intent", values["intent"]),
        ("Customer-provided details", values["customer_details"]),
        ("Monitoring findings", values["monitoring_findings"]),
        ("Troubleshooting already performed", values["troubleshooting"]),
        ("Relevant tool results", values["tool_results"]),
        ("Reason for escalation", values["escalation_reason"]),
        ("Recommended destination", values["destination_team"]),
    ]
    lines: list[str] = []
    for label, value in sections:
        if label == value:
            lines.append(value)
        elif value:
            lines.append(f"{label}: {value}")
    return "\n\n".join(lines)[:2000]


def _render_safe_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    try:
        rendered = Template(rendered).safe_substitute(values)
    except ValueError:
        pass
    return rendered


def _strip_empty_summary_lines(text: str) -> str:
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if ":" in line and not line.split(":", 1)[1].strip():
            continue
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def _identifier_question(identifier_type: str) -> str:
    if identifier_type == "portal_id":
        return "Please send your Portal ID or account number so I can identify the service."
    if identifier_type == "registered_email":
        return "Please send the registered email on the account so I can identify it."
    if identifier_type == "registered_phone":
        return "Please send the registered phone number on the account."
    if identifier_type == CUSTOMER_IDENTIFIER_REQUEST:
        return (
            "Please send the registered phone number, registered email, or "
            "Portal ID on the account."
        )
    return "Please share that detail so I can continue checking this."


def _identifier_retry_question(identifier_type: str) -> str:
    if identifier_type == CUSTOMER_IDENTIFIER_REQUEST:
        return (
            "I still need the registered phone number, registered email, or "
            "Portal ID on the account. Please send one of those details."
        )
    if identifier_type == "portal_id":
        return "I still need the Portal ID or account number for the service."
    if identifier_type == "registered_email":
        return "I still need the registered email on the account."
    if identifier_type == "registered_phone":
        return "I still need the registered phone number on the account."
    return "I still need that detail so I can continue checking this."


def _turn_limit_handoff_response(
    state: ConversationalState, policy: dict[str, object]
) -> str:
    if _requires_identity_before_tools(state, policy) and not state.subscriber_id:
        return (
            "I could not safely identify the account from the details provided. "
            "I will pass this to the support team."
        )
    return "I will pass the details I have collected to the support team."


def _next_identifier_to_request(
    state: ConversationalState, policy: dict[str, object]
) -> str | None:
    for identifier in _permitted_identifiers(policy):
        if _identifier_supplied(state, identifier):
            continue
        if identifier in state.already_requested_fields:
            continue
        return identifier
    return None


def _identifier_value(state: ConversationalState, identifier_type: str) -> str | None:
    if identifier_type == "portal_id":
        return state.portal_id
    if identifier_type == "registered_email":
        return state.registered_email
    if identifier_type == "registered_phone":
        return state.registered_phone
    return None


def _identifier_supplied(state: ConversationalState, identifier_type: str) -> bool:
    return bool(_identifier_value(state, identifier_type))


def _permitted_identifiers(policy: dict[str, object]) -> tuple[str, ...]:
    """Return the identifier request order the policy declared.

    The declared order is authoritative and is the single source consumed by
    both prompting (`_next_identifier_to_request`) and identification lookup
    (`_identify_customer`). Values are kept in declaration order,
    de-duplicated by first occurrence, and unsupported values are dropped
    without reordering the valid ones. The engine never imposes a canonical
    order over a declared one; when no order is configured the historical
    default applies.
    """
    raw = policy.get("permitted_identifiers")
    if isinstance(raw, str):
        raw = [raw]
    declared: list[str] = []
    for item in _list(raw):
        if item in SUPPORTED_IDENTIFIER_TYPES and item not in declared:
            declared.append(item)
    return tuple(declared) or DEFAULT_IDENTIFIER_REQUEST_ORDER


def _tool_enabled(policy: dict[str, object], key: str) -> bool:
    tools = policy.get("tools")
    if tools is None:
        return key == "customer_lookup"
    if isinstance(tools, dict):
        raw = tools.get(key)
        if isinstance(raw, dict):
            return bool(raw.get("enabled", False))
        return bool(raw)
    if isinstance(tools, list):
        return key in tools
    return False


def _tool_enabled_for_intent(
    policy: dict[str, object], key: str, intent: str | None
) -> bool:
    if not _tool_enabled(policy, key):
        return False
    for raw in _list(policy.get("intent_definitions")):
        definition = _dict(raw)
        definition_intent = str(
            definition.get("intent") or definition.get("key") or ""
        ).strip()
        if definition_intent != str(intent or ""):
            continue
        allowed = [str(item).strip() for item in _list(definition.get("allowed_tools"))]
        return not allowed or key in allowed
    return True


def _tool_failure_requires_handoff(
    policy: Mapping[str, object], *, tool_key: str, status: str
) -> bool:
    configured = policy.get("tool_failure_handoff_statuses")
    if not isinstance(configured, Mapping):
        return False
    raw_statuses = configured.get(tool_key)
    if not isinstance(raw_statuses, list | tuple):
        return False
    return status in {str(item).strip() for item in raw_statuses if str(item).strip()}


def _append_statement(state: ConversationalState, text: str) -> None:
    clean = " ".join(str(text or "").split())[:500]
    if clean:
        state.customer_statements.append(clean)


def _record_event(
    session: AiIntakeSession,
    event_type: str,
    at: datetime,
    *,
    state: ConversationalState,
) -> None:
    metadata = dict(session.metadata_ or {})
    events = list(_dict_list(metadata.get(EVENTS_KEY)))
    events.append(
        {
            "event_type": event_type,
            "at": at.isoformat(),
            "current_intent": state.current_intent,
            "previous_intent": state.previous_intent,
        }
    )
    metadata[EVENTS_KEY] = events[-40:]
    session.metadata_ = metadata


def _with_unique(values: list[str], value: str) -> list[str]:
    return list(dict.fromkeys([*values, value]))


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _bounded_int(value: object, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value)) if value is not None else int(str(default))
    except (TypeError, ValueError):
        parsed = int(str(default))
    return max(low, min(parsed, high))
