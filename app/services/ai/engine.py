"""The AI engine — advises on an owned report, persists via ai.insights.

``docs/designs/AI_SOT.md``: ``ai.gateway`` is the transport, and
**``ai.insights`` (``app.services.ai_operations``) is the only thing that
writes an ``AIInsight`` row.** This module hands over a draft and gets the
row back; it never constructs one.

Reshaped from dotmac_crm's ``app/services/ai/engine.py``. The material
change is what the engine reads:

* CRM's engine ran a persona whose ``context_builder`` queried raw models —
  a parallel derivation path beside the projection the domain owner already
  computes, which is why every persona also needed a ``data_quality``
  scorer to grade its own re-derivation. Here the CALLER fetches the owned
  report and hands the dict in, so the engine performs **no domain-model
  query at all** and the context-quality gate is gone with the re-derivation
  that made it necessary.
* CRM built and committed ``AIInsight`` itself at two sites. Both paths here
  route through ``ai_operations.create_insight``, so the single-writer
  invariant holds by construction.
* Sub stores enums as ``String`` + ``StrEnum`` (values, not members) and its
  actor column is ``triggered_by_system_user_id`` (CRM: a person id).
* CRM wrapped ``invoke`` in an OpenTelemetry span. Sub has no ``get_tracer``
  and no service uses spans, so the span is dropped rather than inventing a
  tracing convention; the audit event and persisted telemetry carry the same
  facts.

The whole path is gated by ``ai.generation``, which fails CLOSED: absent
means no provider call and no spend.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.ai_insight import AIInsight, AIInsightStatus, InsightSeverity
from app.models.domain_settings import SettingDomain
from app.services import ai_operations, control_registry
from app.services.ai.advisors import InputSensitivity
from app.services.ai.redaction import redact_text
from app.services.audit_helpers import log_audit_event
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

#: Master gate. Fails closed — see ``control_registry``.
GENERATION_CONTROL = "ai.generation"

_MAX_TITLE = 300
_MAX_SUMMARY = 5000
_MAX_RECOMMENDATIONS = 10


class AIEngineError(RuntimeError):
    """The engine declined or could not complete a generation."""


def _bool_value(value: object | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _coerce_int(value: object | None, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            return default
    return default


def _enum_value(value: object) -> str:
    """Sub stores enums as strings; advisors may hand us either."""
    return str(getattr(value, "value", value))


def _gateway():
    """The transport, imported lazily.

    It is a sibling slice and may not have landed; a missing transport means
    the engine is inert, not broken.
    """
    from app.services.ai.gateway import ai_gateway

    return ai_gateway


def _advisor_registry():
    from app.services.ai.advisors import advisor_registry

    return advisor_registry


def _serialise_projection(
    projection: dict[str, Any], sensitivity: InputSensitivity
) -> str:
    """The owned projection, as the model sees it.

    ``default=str`` so a Decimal or datetime the owner emits degrades to text
    rather than exploding — the engine must not be the reason a projection
    cannot be advised on.

    An advisor that declared ``customer_content`` has its serialised prompt
    redacted here, at the port, rather than in each caller: a caller that
    forgets is exactly the failure the declaration exists to prevent. See
    ``docs/designs/AI_SOT.md``.
    """
    serialised = json.dumps(projection, indent=2, sort_keys=True, default=str)
    if sensitivity is InputSensitivity.CUSTOMER_CONTENT:
        # redact_text truncates at max_chars, and each replacement is longer
        # than the shortest thing it can replace. Silently shortening a prompt
        # would be a worse failure than a long one, so the bound is set where
        # no expansion can reach it.
        return redact_text(serialised, max_chars=len(serialised) * 4 + 100)
    return serialised


class IntelligenceEngine:
    def enabled(self, db: Session, *, trigger: str) -> bool:
        if not control_registry.is_enabled(db, GENERATION_CONTROL):
            return False
        try:
            if not _gateway().enabled(db):
                return False
        except ImportError:
            # Transport not landed: nothing to call.
            return False
        if trigger == "scheduled":
            # Batch spend is additionally gated: a human asking is not the
            # same as a beat asking on everyone's behalf.
            return _bool_value(
                resolve_value(db, SettingDomain.integration, "intelligence_enabled"),
                False,
            )
        return True

    def _advisor_enabled(self, db: Session, setting_key: str | None) -> bool:
        if not setting_key:
            return True
        return _bool_value(
            resolve_value(db, SettingDomain.integration, setting_key), True
        )

    def _within_budget(self, db: Session) -> bool:
        budget = _coerce_int(
            resolve_value(
                db, SettingDomain.integration, "intelligence_daily_token_budget"
            ),
            0,
        )
        if budget <= 0:
            return True
        return ai_operations.tokens_used_today(db) < budget

    def advise(
        self,
        db: Session,
        *,
        advisor_key: str,
        report: dict[str, Any],
        entity_type: str,
        entity_id: str | None = None,
        trigger: str = "manual",
        triggered_by_system_user_id: str | None = None,
    ) -> AIInsight:
        """Advise on an owned report projection.

        ``report`` is the dict the CALLER fetched from the projection's owner
        (see ``AdvisorSpec.projection_key``). The engine reads nothing else — no
        session query against a domain model happens here.

        Raises ``AIEngineError`` when the engine declines (disabled, out of
        budget, advisor off): declining is not an insight, so nothing is
        written and nothing is spent.
        """
        if not self.enabled(db, trigger=trigger):
            raise AIEngineError("AI generation is disabled")
        if not self._within_budget(db):
            raise AIEngineError("Daily AI token budget exceeded")

        spec = _advisor_registry().get(advisor_key)
        if not self._advisor_enabled(db, spec.setting_key):
            raise AIEngineError(f"Advisor disabled: {advisor_key}")

        from app.services.ai.output_parsers import parse_json_object, require_keys

        started = time.monotonic()
        system = spec.system_prompt.format(
            output_instructions=spec.output_schema.to_instruction()
        )
        primary = "secondary" if spec.default_endpoint == "secondary" else "primary"

        result, routing = _gateway().generate_with_fallback(
            db,
            primary=primary,
            fallback="secondary",
            system=system,
            prompt=_serialise_projection(report, spec.input_sensitivity),
            max_tokens=spec.default_max_tokens,
        )

        parsed = parse_json_object(result.content)
        require_keys(parsed, spec.output_schema.required_keys())

        draft = ai_operations.InsightDraft(
            persona_key=spec.key,
            domain=_enum_value(spec.domain),
            severity=_severity_of(spec, parsed),
            entity_type=entity_type,
            entity_id=entity_id,
            title=(str(parsed.get("title") or spec.name).strip() or spec.name)[
                :_MAX_TITLE
            ],
            summary=(
                str(parsed.get("summary") or "").strip()[:_MAX_SUMMARY]
                or "No summary generated."
            ),
            structured_output=parsed,
            confidence_score=_confidence_of(parsed),
            recommendations=_recommendations_of(parsed),
            trigger=trigger,
            expires_at=_expiry_of(spec),
            # Which owned projection this advice was derived from — so a
            # reader can reproduce the input rather than trust the output.
            metadata={"projection_key": spec.projection_key},
        )
        insight = ai_operations.create_insight(
            db,
            draft,
            triggered_by_system_user_id=triggered_by_system_user_id,
            status=AIInsightStatus.completed.value,
            llm_provider=result.provider,
            llm_model=result.model,
            llm_tokens_in=result.tokens_in,
            llm_tokens_out=result.tokens_out,
            llm_endpoint=str(routing.get("endpoint"))
            if isinstance(routing, dict)
            else None,
            generation_time_ms=int((time.monotonic() - started) * 1000),
        )
        db.commit()
        db.refresh(insight)

        # Audit the fact, never the prompt or the report contents.
        log_audit_event(
            db,
            None,
            action="ai_insight_generated",
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            actor_id=triggered_by_system_user_id,
            metadata={
                "advisor_key": spec.key,
                "projection_key": spec.projection_key,
                "domain": _enum_value(spec.domain),
                "llm_provider": result.provider,
                "llm_model": result.model,
                "llm_endpoint": str(routing.get("endpoint"))
                if isinstance(routing, dict)
                else None,
            },
        )
        return insight


def _severity_of(spec, parsed: dict[str, Any]) -> str:
    if not spec.severity_classifier:
        return InsightSeverity.info.value
    try:
        value = str(spec.severity_classifier(parsed) or "info").strip().lower()
    except Exception:
        logger.warning("advisor %s severity_classifier failed", spec.key, exc_info=True)
        return InsightSeverity.info.value
    allowed = {s.value for s in InsightSeverity}
    return value if value in allowed else InsightSeverity.info.value


def _confidence_of(parsed: dict[str, Any]) -> float | None:
    raw = parsed.get("confidence")
    if raw is None:
        return None
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _recommendations_of(parsed: dict[str, Any]) -> list | None:
    value = parsed.get("recommended_actions") or parsed.get("recommendations") or []
    if not isinstance(value, list):
        return None
    return value[:_MAX_RECOMMENDATIONS]


def _expiry_of(spec) -> datetime | None:
    hours = max(int(spec.insight_ttl_hours or 0), 0)
    return datetime.now(UTC) + timedelta(hours=hours) if hours else None


intelligence_engine = IntelligenceEngine()
