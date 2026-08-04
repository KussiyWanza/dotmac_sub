"""``ai.insights`` — the canonical writer and lifecycle owner of AIInsight.

Every insight lands here and nowhere else (``docs/designs/AI_SOT.md``;
enforced by ``tests/architecture/test_ai_boundaries.py``). The generation
engine hands over a draft; it never constructs the row itself.

AI is advisory: ``action_insight`` marks that *a person acted on this
advice*, never that the system acted. Acting on a recommendation means
calling the domain's declared owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_insight import AIInsight, AIInsightStatus
from app.services.common import coerce_uuid


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class InsightDraft:
    """A candidate insight handed to the writer.

    Mirrors the attributes ``create_insight`` reads, so the engine has a typed
    way to hand over a draft while the API's ``AIInsightCreate`` schema keeps
    satisfying the same shape.
    """

    persona_key: str
    domain: str
    entity_type: str
    title: str
    summary: str
    trigger: str
    severity: str = "info"
    entity_id: str | None = None
    structured_output: dict | None = None
    confidence_score: float | None = None
    recommendations: list | None = None
    context_quality_score: float | None = None
    expires_at: datetime | None = None
    metadata: dict | None = field(default=None)


def _insight_or_404(db: Session, insight_id: str | UUID) -> AIInsight:
    insight = db.get(AIInsight, coerce_uuid(insight_id))
    if insight is None:
        raise HTTPException(status_code=404, detail="AI insight not found")
    return insight


def get_insight(db: Session, insight_id: str | UUID) -> AIInsight:
    return _insight_or_404(db, insight_id)


def create_insight(
    db: Session,
    payload,
    *,
    triggered_by_system_user_id: str | UUID | None = None,
    status: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_tokens_in: int | None = None,
    llm_tokens_out: int | None = None,
    llm_endpoint: str | None = None,
    generation_time_ms: int | None = None,
) -> AIInsight:
    """Create an insight. The ONLY writer of AIInsight rows.

    The generation keywords carry the engine's LLM telemetry. They default to
    the hand-authored posture (``pending`` / ``native`` / no telemetry) so the
    admin API's existing calls are unchanged.
    """
    insight = AIInsight(
        persona_key=payload.persona_key,
        domain=payload.domain,
        severity=payload.severity,
        status=status or AIInsightStatus.pending.value,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        title=payload.title,
        summary=payload.summary,
        structured_output=payload.structured_output,
        confidence_score=payload.confidence_score,
        recommendations=payload.recommendations,
        context_quality_score=payload.context_quality_score,
        llm_provider=llm_provider or "native",
        llm_model=llm_model,
        llm_tokens_in=llm_tokens_in,
        llm_tokens_out=llm_tokens_out,
        llm_endpoint=llm_endpoint,
        generation_time_ms=generation_time_ms,
        trigger=payload.trigger,
        triggered_by_system_user_id=coerce_uuid(triggered_by_system_user_id),
        expires_at=payload.expires_at,
        metadata_=payload.metadata or {},
    )
    db.add(insight)
    db.flush()
    return insight


def list_insights(
    db: Session,
    *,
    domain: str | None = None,
    persona_key: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AIInsight]:
    query = db.query(AIInsight)
    if domain:
        query = query.filter(AIInsight.domain == domain)
    if persona_key:
        query = query.filter(AIInsight.persona_key == persona_key)
    if entity_type:
        query = query.filter(AIInsight.entity_type == entity_type)
    if entity_id:
        query = query.filter(AIInsight.entity_id == entity_id)
    if status:
        query = query.filter(AIInsight.status == status)
    if severity:
        query = query.filter(AIInsight.severity == severity)
    return (
        query.order_by(AIInsight.created_at.desc(), AIInsight.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def tokens_used_today(db: Session, *, now: datetime | None = None) -> int:
    """Tokens spent on completed insights for the current UTC date.

    The engine's daily budget reads this. Only completed rows count: a skipped
    row spends nothing, and a failed call has no trustworthy usage figure.
    """
    today = (now or _now()).date()
    total = (
        db.query(
            func.coalesce(
                func.sum(
                    func.coalesce(AIInsight.llm_tokens_in, 0)
                    + func.coalesce(AIInsight.llm_tokens_out, 0)
                ),
                0,
            )
        )
        .filter(func.date(AIInsight.created_at) == today)
        .filter(AIInsight.status == AIInsightStatus.completed.value)
        .scalar()
    )
    return int(total or 0)


def acknowledge_insight(
    db: Session,
    insight_id: str | UUID,
    *,
    acknowledged_by_system_user_id: str | UUID | None = None,
) -> AIInsight:
    insight = _insight_or_404(db, insight_id)
    insight.status = AIInsightStatus.acknowledged.value
    insight.acknowledged_at = _now()
    insight.acknowledged_by_system_user_id = coerce_uuid(acknowledged_by_system_user_id)
    db.flush()
    return insight


def action_insight(
    db: Session,
    insight_id: str | UUID,
    *,
    actioned_by_system_user_id: str | UUID | None = None,
) -> AIInsight:
    """Mark that a PERSON acted on this advice — not that the system did.

    AI is advisory (``docs/designs/AI_SOT.md``): this stamps the insight row
    and takes no domain action. Acting on a recommendation means calling the
    owning domain service, which applies its own guards, events and audit.

    The model carries no ``actioned_by``/``actioned_at`` columns, so the
    acknowledged actor fields record who did it — the same reuse CRM made.
    """
    insight = _insight_or_404(db, insight_id)
    insight.status = AIInsightStatus.actioned.value
    if actioned_by_system_user_id is not None:
        insight.acknowledged_at = _now()
        insight.acknowledged_by_system_user_id = coerce_uuid(actioned_by_system_user_id)
    db.flush()
    return insight


def expire_insight(db: Session, insight_id: str | UUID) -> AIInsight:
    insight = _insight_or_404(db, insight_id)
    insight.status = AIInsightStatus.expired.value
    db.flush()
    return insight


def expire_stale_insights(
    db: Session, *, now: datetime | None = None, limit: int = 500
) -> int:
    current_time = now or _now()
    expirable_statuses = [
        AIInsightStatus.pending.value,
        AIInsightStatus.completed.value,
        AIInsightStatus.failed.value,
        AIInsightStatus.skipped.value,
    ]
    rows = (
        db.query(AIInsight)
        .filter(AIInsight.expires_at.is_not(None))
        .filter(AIInsight.expires_at <= current_time)
        .filter(AIInsight.status.in_(expirable_statuses))
        .limit(limit)
        .all()
    )
    for insight in rows:
        insight.status = AIInsightStatus.expired.value
    db.flush()
    return len(rows)


# Commit-owning entry points — see the SOT service-ownership contract; the API
# layer calls these rather than committing itself.
def create_insight_committed(
    db: Session, payload, *, triggered_by_system_user_id: str | UUID | None = None
) -> AIInsight:
    insight = create_insight(
        db, payload, triggered_by_system_user_id=triggered_by_system_user_id
    )
    db.commit()
    db.refresh(insight)
    return insight


def acknowledge_insight_committed(
    db: Session,
    insight_id: str | UUID,
    *,
    acknowledged_by_system_user_id: str | UUID | None = None,
) -> AIInsight:
    insight = acknowledge_insight(
        db, insight_id, acknowledged_by_system_user_id=acknowledged_by_system_user_id
    )
    db.commit()
    db.refresh(insight)
    return insight


def action_insight_committed(
    db: Session,
    insight_id: str | UUID,
    *,
    actioned_by_system_user_id: str | UUID | None = None,
) -> AIInsight:
    insight = action_insight(
        db, insight_id, actioned_by_system_user_id=actioned_by_system_user_id
    )
    db.commit()
    db.refresh(insight)
    return insight


def expire_insight_committed(db: Session, insight_id: str | UUID) -> AIInsight:
    insight = expire_insight(db, insight_id)
    db.commit()
    db.refresh(insight)
    return insight
