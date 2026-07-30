"""Advisors: AI advises ON an owned projection, it never re-derives one.

The original design called personas "the resolver" that builds context from
the owning domain's read models. Taken literally — as CRM did — each persona
queries raw models itself, which is a **parallel derivation path** sitting
next to the projection the domain owner already computes. CRM needed a
``data_quality`` scorer per persona precisely because each re-derived its own
context and then had to grade it. ``docs/designs/AI_SOT.md`` now records that
personas are out of the design for this reason.

Sub already owns ~35 report projections (revenue, churn, network,
technician, ticket-SLA, NCC, MRR). An advisor therefore declares the
projection it advises on via ``projection_key``; the CALLER fetches it from
its owner and hands the dict to the engine. The engine never touches a
domain model, so the boundary in
``tests/architecture/test_ai_boundaries.py`` holds by construction rather
than by vigilance — and the quality gate disappears, because a report the
owner computed does not need grading by us.

``OutputField``/``OutputSchema`` are ported from CRM's
``personas/_base.py``: a provider-agnostic JSON contract worth keeping.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutputField:
    name: str
    type: str
    description: str
    required: bool = True


@dataclass(frozen=True)
class OutputSchema:
    """The JSON shape we ask the model for, rendered into the prompt."""

    fields: tuple[OutputField, ...]

    def required_keys(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    def to_instruction(self) -> str:
        lines = ["Return a JSON object with these keys:"]
        for f in self.fields:
            req = "required" if f.required else "optional"
            lines.append(f'  - "{f.name}" ({f.type}, {req}): {f.description}')
        return "\n".join(lines)


class InputSensitivity(StrEnum):
    """What class of data this advisor sends out of the estate.

    The port redacts from this, so the classes cannot be collapsed into one
    global switch: an SLA report is breach counts and team names, an inbox
    advisor is whatever a subscriber typed.
    """

    #: Counts, rates, durations. No identifiers.
    AGGREGATE = "aggregate"
    #: Aggregates carrying internal names — team, assignee.
    STAFF_IDENTIFIABLE = "staff_identifiable"
    #: Text a customer wrote, or their identifiers. Redacted before sending.
    CUSTOMER_CONTENT = "customer_content"


@dataclass(frozen=True)
class AdvisorSpec:
    """One advisor, bound to one owned projection.

    ``projection_key`` names the projection this advises on. It is
    documentation with teeth: the binding is explicit, so a reader can find
    the owner that computes the input, and a caller cannot quietly feed an
    advisor something it was never designed to read.

    ``input_sensitivity`` is the declaration that turns redaction on. It has
    no default: an advisor author must decide what their projection carries,
    because a forgotten default is how customer text reaches a provider
    unredacted.
    """

    key: str
    name: str
    domain: str
    description: str
    projection_key: str
    input_sensitivity: InputSensitivity
    system_prompt: str  # must contain the {output_instructions} slot
    output_schema: OutputSchema
    default_max_tokens: int = 1200
    default_endpoint: str = "primary"  # primary|secondary
    setting_key: str | None = None
    insight_ttl_hours: int = 72
    severity_classifier: Callable[[dict[str, Any]], str] | None = None


class AdvisorRegistry:
    def __init__(self) -> None:
        self._advisors: dict[str, AdvisorSpec] = {}

    def register(self, spec: AdvisorSpec) -> None:
        if spec.key in self._advisors:
            logger.warning("Advisor %s already registered, overwriting", spec.key)
        self._advisors[spec.key] = spec

    def get(self, key: str) -> AdvisorSpec:
        spec = self._advisors.get(key)
        if not spec:
            raise ValueError(f"Unknown advisor: {key}")
        return spec

    def list_all(self) -> list[AdvisorSpec]:
        return list(self._advisors.values())

    def keys(self) -> list[str]:
        return list(self._advisors.keys())


advisor_registry = AdvisorRegistry()


# ── ticket SLA advisor ──────────────────────────────────────────────────────
# Advises on `ticket_sla_reports.summary(db, start_at, end_at)` — the owned
# projection behind /admin/reports (app/web/admin/reports.py). Its shape:
#   total_clocks, total_breaches, breach_rate,
#   by_status / by_service_team / by_assignee:
#       [{key, label?, total, breached, breach_rate}]
# The prompt describes only those fields; nothing here invents any.

TICKET_SLA_PROJECTION_KEY = "ticket_sla_reports.summary"


def _sla_severity(parsed: dict[str, Any]) -> str:
    """Severity from the model's own risk read, clamped to our vocabulary."""
    value = str(parsed.get("risk_level") or "").strip().lower()
    return {
        "critical": "critical",
        "high": "warning",
        "medium": "suggestion",
        "low": "info",
    }.get(value, "info")


TICKET_SLA_ADVISOR = AdvisorSpec(
    key="ticket_sla_advisor",
    name="Ticket SLA Advisor",
    domain="tickets",
    description=(
        "Reads the owned ticket-SLA summary and points out where breaches "
        "concentrate and what to look at first."
    ),
    projection_key=TICKET_SLA_PROJECTION_KEY,
    # by_service_team and by_assignee carry internal names, so this is not
    # purely aggregate — but it holds nothing a customer wrote.
    input_sensitivity=InputSensitivity.STAFF_IDENTIFIABLE,
    setting_key="intelligence_ticket_sla_advisor_enabled",
    system_prompt=(
        "You are an ISP support operations analyst. You are given a ticket "
        "SLA summary computed by the operator's own reporting system. Fields:\n"
        "  total_clocks: SLA clocks in the window\n"
        "  total_breaches: how many breached\n"
        "  breach_rate: breaches / clocks (0..1)\n"
        # Braces doubled: the engine renders this through str.format() to fill
        # {output_instructions}, so a literal brace must be escaped or format()
        # reads it as a field and raises KeyError.
        "  by_status, by_service_team, by_assignee: buckets of "
        "{{key, label, total, breached, breach_rate}}\n\n"
        "Explain where breaches concentrate and what to investigate first. "
        "Cite only numbers present in the report — do not estimate, "
        "extrapolate, or invent causes. If the report is empty or too small "
        "to read into, say so plainly rather than speculating.\n\n"
        "{output_instructions}"
    ),
    output_schema=OutputSchema(
        fields=(
            OutputField(
                name="title",
                type="string",
                description="One line naming the main SLA finding.",
            ),
            OutputField(
                name="summary",
                type="string",
                description=("A short paragraph citing the report's own figures."),
            ),
            OutputField(
                name="risk_level",
                type="string",
                description="One of: low, medium, high, critical.",
            ),
            OutputField(
                name="recommended_actions",
                type="array of strings",
                description="Concrete next checks for a support lead.",
                required=False,
            ),
        )
    ),
    severity_classifier=_sla_severity,
)

advisor_registry.register(TICKET_SLA_ADVISOR)


INBOX_REPLY_PROJECTION_KEY = "team_inbox_projection.ai_reply_context"

INBOX_REPLY_ADVISOR = AdvisorSpec(
    key="inbox_analyst",
    name="Inbox Reply Advisor",
    domain="communications",
    description="Drafts a reviewable reply from the owned Team Inbox projection.",
    projection_key=INBOX_REPLY_PROJECTION_KEY,
    input_sensitivity=InputSensitivity.CUSTOMER_CONTENT,
    setting_key="intelligence_inbox_analyst_enabled",
    insight_ttl_hours=24,
    default_max_tokens=600,
    system_prompt=(
        "You are a customer-support agent for the company named in the supplied "
        "Team Inbox projection. Draft a reply that matches the channel's tone "
        "and uses only facts already present in the projection. Never mention "
        "AI or internal systems. Do not invent facts, promises, dates, causes, "
        "or resolutions. Ask one concise clarifying question when information "
        "is missing. Keep the draft under 120 words. The agent will review it; "
        "do not imply it has been sent.\n\n{output_instructions}"
    ),
    output_schema=OutputSchema(
        fields=(
            OutputField("draft", "string", "Reviewable reply under 120 words."),
            OutputField(
                "tone",
                "string",
                "Short description of the reply tone.",
            ),
            OutputField("title", "string", "Short title for this draft."),
            OutputField("summary", "string", "What the draft is trying to do."),
            OutputField(
                "clarifying_questions",
                "array of strings",
                "Missing information the agent may need.",
                required=False,
            ),
            OutputField(
                "confidence",
                "number",
                "Confidence from 0 to 1.",
                required=False,
            ),
        )
    ),
)

INBOX_SENTENCE_POLISH_ADVISOR = AdvisorSpec(
    key="inbox_sentence_polish",
    name="Inbox Sentence Polish",
    domain="communications",
    description="Polishes agent-supplied composer text without adding facts.",
    projection_key="admin_inbox.unsent_composer_submission",
    input_sensitivity=InputSensitivity.CUSTOMER_CONTENT,
    setting_key="intelligence_inbox_analyst_enabled",
    insight_ttl_hours=1,
    default_max_tokens=350,
    system_prompt=(
        "Polish the supplied unsent inbox composer text. Preserve its exact "
        "meaning and language. Fix punctuation, spacing, capitalization, and "
        "obvious grammar only. Do not add facts, names, promises, greetings, "
        "apologies, dates, or explanations. Return at most two distinct "
        "alternatives. The agent must accept a suggestion before anything "
        "changes.\n\n{output_instructions}"
    ),
    output_schema=OutputSchema(
        fields=(
            OutputField("title", "string", "Short label for the suggestion."),
            OutputField("summary", "string", "A short description of the edits."),
            OutputField(
                "suggested_text",
                "string",
                "The minimally polished text.",
            ),
            OutputField(
                "alternatives",
                "array of strings",
                "No more than two distinct alternatives.",
                required=False,
            ),
            OutputField(
                "confidence",
                "number",
                "Confidence from 0 to 1.",
                required=False,
            ),
        )
    ),
)

advisor_registry.register(INBOX_REPLY_ADVISOR)
advisor_registry.register(INBOX_SENTENCE_POLISH_ADVISOR)
