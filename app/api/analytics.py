from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.analytics import (
    KPIAggregateCreate,
    KPIAggregateRead,
    KPIConfigCreate,
    KPIConfigRead,
    KPIConfigUpdate,
    KPIReadout,
)
from app.schemas.common import ListResponse
from app.schemas.team_inbox_metrics import (
    InboxAgentPerformanceRead,
    InboxEscalationCandidateRead,
    InboxTeamPerformanceRead,
)
from app.services import analytics as analytics_service
from app.services import team_inbox_metrics as team_inbox_metrics_service
from app.services.response import list_response

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.post(
    "/kpi-configs", response_model=KPIConfigRead, status_code=status.HTTP_201_CREATED
)
def create_kpi_config(payload: KPIConfigCreate, db: Session = Depends(get_db)):
    return analytics_service.kpi_configs.create(db, payload)


@router.get("/kpi-configs/{config_id}", response_model=KPIConfigRead)
def get_kpi_config(config_id: str, db: Session = Depends(get_db)):
    return analytics_service.kpi_configs.get(db, config_id)


@router.get("/kpi-configs", response_model=ListResponse[KPIConfigRead])
def list_kpi_configs(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = analytics_service.kpi_configs.list(
        db, is_active, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.patch("/kpi-configs/{config_id}", response_model=KPIConfigRead)
def update_kpi_config(
    config_id: str, payload: KPIConfigUpdate, db: Session = Depends(get_db)
):
    return analytics_service.kpi_configs.update(db, config_id, payload)


@router.delete("/kpi-configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_kpi_config(config_id: str, db: Session = Depends(get_db)):
    analytics_service.kpi_configs.delete(db, config_id)


@router.post(
    "/kpi-aggregates",
    response_model=KPIAggregateRead,
    status_code=status.HTTP_201_CREATED,
)
def create_kpi_aggregate(payload: KPIAggregateCreate, db: Session = Depends(get_db)):
    return analytics_service.kpi_aggregates.create(db, payload)


@router.get("/kpi-aggregates/{aggregate_id}", response_model=KPIAggregateRead)
def get_kpi_aggregate(aggregate_id: str, db: Session = Depends(get_db)):
    return analytics_service.kpi_aggregates.get(db, aggregate_id)


@router.get("/kpi-aggregates", response_model=ListResponse[KPIAggregateRead])
def list_kpi_aggregates(
    key: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = analytics_service.kpi_aggregates.list(
        db, key, order_by, order_dir, limit, offset
    )
    return list_response(items, limit, offset)


@router.get("/kpis", response_model=list[KPIReadout])
def compute_kpis(db: Session = Depends(get_db)):
    return analytics_service.compute_kpis(db)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _team_performance_read(
    row: team_inbox_metrics_service.InboxTeamPerformanceReportRow,
) -> InboxTeamPerformanceRead:
    metrics = row.metrics
    return InboxTeamPerformanceRead(
        service_team_id=UUID(metrics.service_team_id),
        service_team_name=row.service_team_name,
        service_team_capabilities=row.service_team_capabilities,
        response_sla_seconds=row.response_sla_seconds,
        conversation_count=metrics.conversation_count,
        open_count=metrics.open_count,
        unassigned_open_count=metrics.unassigned_open_count,
        assigned_open_count=metrics.assigned_open_count,
        inbound_message_count=metrics.inbound_message_count,
        outbound_message_count=metrics.outbound_message_count,
        responded_count=metrics.responded_count,
        response_sla_breached_count=metrics.response_sla_breached_count,
        response_rate=_ratio(metrics.responded_count, metrics.inbound_message_count),
        response_sla_breach_rate=_ratio(
            metrics.response_sla_breached_count,
            metrics.inbound_message_count,
        ),
        average_first_response_seconds=metrics.average_first_response_seconds,
        average_queue_wait_seconds=metrics.average_queue_wait_seconds,
    )


def _agent_performance_read(
    row: team_inbox_metrics_service.InboxAgentPerformanceReportRow,
) -> InboxAgentPerformanceRead:
    metrics = row.metrics
    return InboxAgentPerformanceRead(
        person_id=UUID(metrics.person_id),
        service_team_id=UUID(metrics.service_team_id),
        service_team_name=row.service_team_name,
        service_team_capabilities=row.service_team_capabilities,
        active_assignment_count=metrics.active_assignment_count,
        handled_conversation_count=metrics.handled_conversation_count,
        average_first_response_seconds=metrics.average_first_response_seconds,
        average_queue_wait_seconds=metrics.average_queue_wait_seconds,
    )


def _escalation_candidate_read(
    row: team_inbox_metrics_service.InboxEscalationCandidate,
) -> InboxEscalationCandidateRead:
    return InboxEscalationCandidateRead(
        conversation_id=UUID(row.conversation_id),
        service_team_id=UUID(row.service_team_id),
        service_team_name=row.service_team_name,
        service_team_capabilities=row.service_team_capabilities,
        subject=row.subject,
        contact_address=row.contact_address,
        status=row.status,
        reasons=list(row.reasons),
        response_sla_seconds=row.response_sla_seconds,
        queue_sla_seconds=row.queue_sla_seconds,
        pending_response_seconds=row.pending_response_seconds,
        queue_wait_seconds=row.queue_wait_seconds,
        assigned_person_id=(
            UUID(row.assigned_person_id) if row.assigned_person_id else None
        ),
        available_agent_count=row.available_agent_count,
    )


@router.get(
    "/inbox/team-performance",
    response_model=ListResponse[InboxTeamPerformanceRead],
)
def list_inbox_team_performance(
    response_sla_seconds: int | None = Query(default=None, gt=0),
    include_inactive: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = team_inbox_metrics_service.team_performance_report(
        db,
        response_sla_seconds=response_sla_seconds,
        include_inactive=include_inactive,
    )
    items = [_team_performance_read(row) for row in rows[offset : offset + limit]]
    return list_response(items, limit, offset)


@router.get(
    "/inbox/agent-performance",
    response_model=ListResponse[InboxAgentPerformanceRead],
)
def list_inbox_agent_performance(
    service_team_id: str | None = None,
    include_inactive_members: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = team_inbox_metrics_service.agent_performance_report(
        db,
        service_team_id=service_team_id,
        include_inactive_members=include_inactive_members,
    )
    items = [_agent_performance_read(row) for row in rows[offset : offset + limit]]
    return list_response(items, limit, offset)


@router.get(
    "/inbox/escalation-candidates",
    response_model=ListResponse[InboxEscalationCandidateRead],
)
def list_inbox_escalation_candidates(
    response_sla_seconds: int | None = Query(default=None, gt=0),
    queue_sla_seconds: int | None = Query(default=None, gt=0),
    include_inactive: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = team_inbox_metrics_service.escalation_candidates(
        db,
        response_sla_seconds=response_sla_seconds,
        queue_sla_seconds=queue_sla_seconds,
        include_inactive=include_inactive,
    )
    items = [_escalation_candidate_read(row) for row in rows[offset : offset + limit]]
    return list_response(items, limit, offset)
