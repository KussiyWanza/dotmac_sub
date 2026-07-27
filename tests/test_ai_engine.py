"""The AI engine advises on an owned report and persists via ai.insights.

Two invariants the architecture guard states but cannot prove at runtime:

1. Every row out of ``advise`` comes from ``ai_operations.create_insight``,
   so ``AIInsight`` keeps exactly one writer.
2. The engine issues **no domain-model query**. The caller fetches the owned
   projection and hands the dict in — that is the whole point of the advisor
   shape, and the reason there is no context-quality gate to test.

The rest is about not spending money we did not mean to.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event

from app.models.ai_insight import AIInsight, AIInsightStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services import ai_operations, control_registry
from app.services.ai import engine as ai_engine
from app.services.ai.advisors import (
    AdvisorSpec,
    InputSensitivity,
    OutputField,
    OutputSchema,
    advisor_registry,
)
from app.services.ai.engine import AIEngineError, intelligence_engine

# A report shaped exactly like ticket_sla_reports.summary() returns.
_REPORT = {
    "total_clocks": 120,
    "total_breaches": 18,
    "breach_rate": 0.15,
    "by_status": [{"key": "breached", "total": 18, "breached": 18, "breach_rate": 1.0}],
    "by_service_team": [
        {
            "key": "unassigned_team",
            "label": "Unassigned Team",
            "total": 40,
            "breached": 12,
            "breach_rate": 0.3,
        }
    ],
    "by_assignee": [],
}


def _enable_generation(db) -> None:
    """Write the canonical modules-domain row for the ai.generation control.

    Legacy alias rows are ignored by the resolver, so this is the only row
    that turns it on.
    """
    control = control_registry._CONTROLS["ai.generation"]
    db.add(
        DomainSetting(
            domain=SettingDomain.modules,
            key=control_registry.canonical_setting_key(control),
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    db.flush()


def _spec(**overrides) -> AdvisorSpec:
    defaults = dict(
        key="test_advisor",
        name="Test Advisor",
        domain="tickets",
        description="test",
        projection_key="ticket_sla_reports.summary",
        input_sensitivity=InputSensitivity.AGGREGATE,
        system_prompt="You are a test advisor.\n{output_instructions}",
        output_schema=OutputSchema(
            fields=(
                OutputField(name="title", type="string", description="t"),
                OutputField(name="summary", type="string", description="s"),
            )
        ),
        setting_key=None,
    )
    defaults.update(overrides)
    return AdvisorSpec(**defaults)


def _result(tokens_in=100, tokens_out=50):
    return SimpleNamespace(
        content='{"title": "Breaches concentrate in one team", "summary":'
        ' "12 of 40 unassigned-team clocks breached.", "confidence": 0.8,'
        ' "recommended_actions": ["review unassigned queue"]}',
        provider="vllm",
        model="qwen2.5",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


class _Gateway:
    """Stands in for the transport. Records the call and the prompt."""

    def __init__(self, result=None):
        self.calls = 0
        self.prompt = None
        self._result = result or _result()

    def enabled(self, db):
        return True

    def generate_with_fallback(self, db, **kwargs):
        self.calls += 1
        self.prompt = kwargs.get("prompt")
        return self._result, {"endpoint": "primary"}


def _advise(db, spec, gateway, *, report=None, **kwargs):
    registry = SimpleNamespace(get=lambda key: spec)
    with (
        patch.object(ai_engine, "_advisor_registry", lambda: registry),
        patch.object(ai_engine, "_gateway", lambda: gateway),
    ):
        return intelligence_engine.advise(
            db,
            advisor_key=spec.key,
            report=_REPORT if report is None else report,
            entity_type="ticket_sla_report",
            entity_id=str(uuid.uuid4()),
            **kwargs,
        )


# ── the single-writer invariant ─────────────────────────────────────────────


def test_advise_writes_exactly_one_insight_through_the_owner(db_session):
    _enable_generation(db_session)
    gateway = _Gateway()

    with patch.object(
        ai_operations, "create_insight", wraps=ai_operations.create_insight
    ) as writer:
        insight = _advise(db_session, _spec(), gateway)

    assert writer.call_count == 1  # the row came from the canonical writer
    assert db_session.query(AIInsight).count() == 1
    assert insight.status == AIInsightStatus.completed.value
    assert insight.title == "Breaches concentrate in one team"
    assert insight.domain == "tickets"


# ── the advisor invariant: no re-derivation ─────────────────────────────────


def test_engine_issues_no_domain_query(db_session):
    """The engine reads the caller's report dict and nothing else.

    Any SELECT against a domain table would mean the engine had started
    re-deriving its own context — the parallel derivation path this shape
    exists to prevent. Settings/insight reads are the engine's own business;
    a domain table is not.
    """
    _enable_generation(db_session)
    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", _record)
    try:
        _advise(db_session, _spec(), _Gateway())
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", _record)

    domain_tables = ("support_tickets", "sla_clocks", "subscribers", "service_teams")
    offenders = [s for s in seen if any(f" {t}" in s.lower() for t in domain_tables)]
    assert not offenders, (
        "the engine queried a domain table — the caller owns fetching the "
        f"report:\n{offenders}"
    )


def test_the_report_the_caller_supplies_is_what_reaches_the_model(db_session):
    _enable_generation(db_session)
    gateway = _Gateway()
    _advise(db_session, _spec(), gateway)

    assert gateway.prompt is not None
    assert '"total_breaches": 18' in gateway.prompt
    assert "Unassigned Team" in gateway.prompt


def test_the_insight_records_which_report_it_advised_on(db_session):
    _enable_generation(db_session)
    insight = _advise(db_session, _spec(), _Gateway())
    # So a reader can reproduce the input rather than trust the output.
    assert insight.metadata_["projection_key"] == "ticket_sla_reports.summary"


# ── telemetry ───────────────────────────────────────────────────────────────


def test_llm_telemetry_lands_on_the_row(db_session):
    _enable_generation(db_session)
    insight = _advise(
        db_session, _spec(), _Gateway(_result(tokens_in=321, tokens_out=7))
    )

    assert insight.llm_provider == "vllm"
    assert insight.llm_model == "qwen2.5"
    assert insight.llm_tokens_in == 321
    assert insight.llm_tokens_out == 7
    assert insight.llm_endpoint == "primary"
    assert insight.generation_time_ms is not None
    assert float(insight.confidence_score) == pytest.approx(0.8)
    assert insight.recommendations == ["review unassigned queue"]


# ── declining costs nothing ─────────────────────────────────────────────────


def test_disabled_control_is_inert(db_session):
    # No modules row written: ai.generation fails CLOSED.
    gateway = _Gateway()
    with pytest.raises(AIEngineError, match="disabled"):
        _advise(db_session, _spec(), gateway)
    assert gateway.calls == 0
    assert db_session.query(AIInsight).count() == 0


def test_token_budget_exceeded_makes_no_provider_call(db_session):
    _enable_generation(db_session)
    gateway = _Gateway()
    with (
        patch.object(ai_engine, "resolve_value", lambda db, domain, key: 10),
        patch.object(ai_operations, "tokens_used_today", lambda db: 999),
        pytest.raises(AIEngineError, match="budget"),
    ):
        _advise(db_session, _spec(), gateway)
    assert gateway.calls == 0
    assert db_session.query(AIInsight).count() == 0


def test_advisor_disabled_makes_no_provider_call(db_session):
    _enable_generation(db_session)
    gateway = _Gateway()
    spec = _spec(setting_key="intelligence_test_advisor_enabled")
    with (
        patch.object(
            ai_engine,
            "resolve_value",
            lambda db, domain, key: (
                False if key == "intelligence_test_advisor_enabled" else None
            ),
        ),
        pytest.raises(AIEngineError, match="Advisor disabled"),
    ):
        _advise(db_session, spec, gateway)
    assert gateway.calls == 0
    assert db_session.query(AIInsight).count() == 0


# ── the shipped advisor ─────────────────────────────────────────────────────


def test_ticket_sla_advisor_is_registered_and_bound_to_its_report():
    spec = advisor_registry.get("ticket_sla_advisor")
    assert spec.projection_key == "ticket_sla_reports.summary"
    # The prompt must carry the slot the engine fills, or the schema is lost.
    assert "{output_instructions}" in spec.system_prompt
    assert "title" in spec.output_schema.required_keys()
    assert "summary" in spec.output_schema.required_keys()


def test_every_registered_advisor_prompt_renders():
    """The engine fills the prompt with str.format(), so ANY unescaped brace
    in a prompt is a KeyError on the first real call. This caught exactly that
    in the ticket-SLA advisor, whose prompt documents a bucket shape —
    ``{key, label, ...}`` — that format() read as a field.
    """
    for spec in advisor_registry.list_all():
        rendered = spec.system_prompt.format(
            output_instructions=spec.output_schema.to_instruction()
        )
        assert "Return a JSON object" in rendered
        for key in spec.output_schema.required_keys():
            assert key in rendered


def test_ticket_sla_severity_maps_risk_to_our_vocabulary():
    spec = advisor_registry.get("ticket_sla_advisor")
    assert spec.severity_classifier({"risk_level": "critical"}) == "critical"
    assert spec.severity_classifier({"risk_level": "high"}) == "warning"
    assert spec.severity_classifier({"risk_level": "low"}) == "info"
    assert spec.severity_classifier({}) == "info"  # absent -> not alarming


def test_unknown_severity_from_the_model_degrades_to_info(db_session):
    _enable_generation(db_session)
    spec = _spec(severity_classifier=lambda parsed: "apocalyptic")
    insight = _advise(db_session, spec, _Gateway())
    assert insight.severity == "info"


# ── lifecycle folded in from CRM's insights.py ──────────────────────────────


def test_action_marks_the_row_and_takes_no_domain_action(db_session):
    _enable_generation(db_session)
    insight = _advise(db_session, _spec(), _Gateway())

    actioned = ai_operations.action_insight(db_session, insight.id)

    assert actioned.status == AIInsightStatus.actioned.value
    # "actioned" means a PERSON acted on the advice — the system never does.


def test_tokens_used_today_counts_completed_rows(db_session):
    _enable_generation(db_session)
    _advise(db_session, _spec(), _Gateway(_result(tokens_in=100, tokens_out=50)))
    assert ai_operations.tokens_used_today(db_session) == 150


# --- what leaves the estate ------------------------------------------------


def test_a_customer_content_advisor_has_its_prompt_redacted():
    """The declaration is what turns redaction on.

    Before this, redaction.py existed and nothing called it, so the sensitive
    class was protected by a module that never ran.
    """
    from app.services.ai.engine import _serialise_projection

    projection = {
        "messages": [
            {"body": "call me on +234 803 555 0111 or ada@example.com"},
        ]
    }

    sent = _serialise_projection(projection, InputSensitivity.CUSTOMER_CONTENT)

    assert "ada@example.com" not in sent
    assert "555 0111" not in sent
    assert "[redacted-email]" in sent
    assert "[redacted-phone]" in sent


def test_an_aggregate_advisor_is_sent_unchanged():
    """Redacting counts would corrupt the input for no benefit."""
    from app.services.ai.engine import _serialise_projection

    projection = {"total_clocks": 40, "breach_rate": 0.12}

    sent = _serialise_projection(projection, InputSensitivity.AGGREGATE)

    assert '"total_clocks": 40' in sent
    assert "0.12" in sent


def test_redaction_never_silently_truncates_the_prompt():
    """redact_text truncates at max_chars and its replacements are longer than
    what they replace; a shortened prompt would be a worse failure than a long
    one."""
    from app.services.ai.engine import _serialise_projection

    projection = {
        "messages": [{"body": f"a{index}@example.com"} for index in range(80)]
    }

    sent = _serialise_projection(projection, InputSensitivity.CUSTOMER_CONTENT)

    assert sent.count("[redacted-email]") == 80
    assert sent.rstrip().endswith("}")


def test_every_registered_advisor_declares_its_sensitivity():
    """A forgotten default is how customer text reaches a provider unredacted,
    so the field has none — this pins that no advisor slips through."""
    for spec in advisor_registry.list_all():
        assert isinstance(spec.input_sensitivity, InputSensitivity), spec.key
