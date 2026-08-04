from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.ai_operations import (
    AIInsightCreate,
    AIInsightRead,
    AiIntakeConfigRead,
    AiIntakeConfigUpsert,
)
from app.schemas.common import ListResponse
from app.services import ai_intake, ai_operations
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.response import list_response

router = APIRouter(prefix="/ai-operations", tags=["ai-operations"])


def _actor_id(auth: dict) -> str | None:
    if auth.get("principal_type") == "system_user":
        return str(auth.get("principal_id"))
    return None


def _insight_read(insight) -> AIInsightRead:
    return AIInsightRead(
        id=insight.id,
        persona_key=insight.persona_key,
        domain=insight.domain,
        severity=insight.severity,
        status=insight.status,
        entity_type=insight.entity_type,
        entity_id=insight.entity_id,
        title=insight.title,
        summary=insight.summary,
        structured_output=insight.structured_output,
        recommendations=insight.recommendations,
        confidence_score=float(insight.confidence_score)
        if insight.confidence_score is not None
        else None,
        context_quality_score=float(insight.context_quality_score)
        if insight.context_quality_score is not None
        else None,
        trigger=insight.trigger,
        acknowledged_at=insight.acknowledged_at,
        acknowledged_by_system_user_id=insight.acknowledged_by_system_user_id,
        expires_at=insight.expires_at,
        metadata=insight.metadata_,
        created_at=insight.created_at,
        updated_at=insight.updated_at,
    )


def _config_read(config) -> AiIntakeConfigRead:
    return AiIntakeConfigRead(
        id=config.id,
        scope_key=config.scope_key,
        channel_type=config.channel_type,
        is_enabled=config.is_enabled,
        confidence_threshold=config.confidence_threshold,
        allow_followup_questions=config.allow_followup_questions,
        max_clarification_turns=config.max_clarification_turns,
        escalate_after_minutes=config.escalate_after_minutes,
        exclude_campaign_attribution=config.exclude_campaign_attribution,
        fallback_team_id=config.fallback_team_id,
        instructions=config.instructions,
        department_mappings=config.department_mappings,
        metadata=config.metadata,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.post(
    "/insights", response_model=AIInsightRead, status_code=status.HTTP_201_CREATED
)
def create_insight(
    payload: AIInsightCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    insight = ai_operations.create_insight_committed(
        db,
        payload,
        triggered_by_system_user_id=_actor_id(auth),
    )
    return _insight_read(insight)


@router.get("/insights", response_model=ListResponse[AIInsightRead])
def list_insights(
    domain: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = [
        _insight_read(row)
        for row in ai_operations.list_insights(
            db,
            domain=domain,
            status=status,
            severity=severity,
            limit=limit,
            offset=offset,
        )
    ]
    return list_response(items, limit, offset)


@router.post("/insights/{insight_id}/acknowledge", response_model=AIInsightRead)
def acknowledge_insight(
    insight_id: UUID,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    insight = ai_operations.acknowledge_insight_committed(
        db,
        insight_id,
        acknowledged_by_system_user_id=_actor_id(auth),
    )
    return _insight_read(insight)


@router.post(
    "/intake-configs",
    response_model=AiIntakeConfigRead,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def upsert_intake_config(
    payload: AiIntakeConfigUpsert,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    actor_id = _actor_id(auth)
    if actor_id is None:
        raise HTTPException(status_code=403, detail="System user required")
    try:
        config = ai_intake.upsert_config(
            db,
            ai_intake.UpsertAiIntakeConfigCommand(
                context=CommandContext.system(
                    actor=f"user:{actor_id}",
                    scope=ai_intake.CONFIG_SCOPE,
                    reason="update AI intake configuration",
                    idempotency_key=f"ai-intake-config:{payload.scope_key}",
                ),
                policy=payload,
            ),
        )
    except DomainError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return _config_read(config)


@router.get(
    "/intake-configs",
    response_model=ListResponse[AiIntakeConfigRead],
    dependencies=[Depends(require_permission("system:settings:read"))],
)
def list_intake_configs(
    channel_type: str | None = None,
    enabled: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = ai_intake.list_configs(
        db,
        channel_type=channel_type,
        enabled=enabled,
        limit=limit,
        offset=offset,
    )
    return list_response([_config_read(row) for row in rows], limit, offset)
