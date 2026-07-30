"""Read-only sales dashboard reporting owned by the native sales domain.

The calculations in this module are the single native implementation used by
the Selfcare sales dashboard.  Routes and templates consume these typed
projections; they do not recalculate pipeline facts.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from app.models.sales import Lead, LeadStatus, PipelineStage
from app.services.display_format import currency_code

OPEN_LEAD_STATUSES = (
    LeadStatus.new.value,
    LeadStatus.contacted.value,
    LeadStatus.qualified.value,
    LeadStatus.proposal.value,
    LeadStatus.negotiation.value,
)
CLOSED_LEAD_STATUSES = (LeadStatus.won.value, LeadStatus.lost.value)


@dataclass(frozen=True)
class StagePipelineSummary:
    id: UUID
    name: str
    count: int
    values: dict[str, Decimal]


@dataclass(frozen=True)
class PipelineSummary:
    pipeline_values: dict[str, Decimal]
    weighted_values: dict[str, Decimal]
    open_deals: int
    won_deals: int
    lost_deals: int
    win_rate: Decimal | None
    average_deal_sizes: dict[str, Decimal]
    stages: tuple[StagePipelineSummary, ...]


@dataclass(frozen=True)
class ForecastMonth:
    month: str
    month_label: str
    expected_values: dict[str, Decimal]
    weighted_values: dict[str, Decimal]
    deal_count: int


@dataclass(frozen=True)
class AgentPerformance:
    agent_id: UUID
    deals_won: int
    deals_lost: int
    total_deals: int
    won_values: dict[str, Decimal]
    win_rate: Decimal | None


@dataclass(frozen=True)
class RecentOpportunity:
    id: UUID
    title: str
    contact_name: str
    status: str
    estimated_value: Decimal | None
    currency: str
    probability: int | None
    updated_at: datetime


@dataclass(frozen=True)
class SalesDashboardReport:
    summary: PipelineSummary
    forecast: tuple[ForecastMonth, ...]
    agent_performance: tuple[AgentPerformance, ...]
    recent_opportunities: tuple[RecentOpportunity, ...]


@dataclass
class _AgentAccumulator:
    deals_won: int = 0
    deals_lost: int = 0
    won_values: dict[str, Decimal] = field(default_factory=dict)


def _add_amount(
    totals: dict[str, Decimal],
    *,
    currency: str | None,
    amount: Decimal | None,
) -> None:
    code = currency_code(currency)
    totals[code] = totals.get(code, Decimal("0.00")) + Decimal(amount or 0)


def _weighted_amount(lead: Lead) -> Decimal:
    probability = lead.probability
    if probability is None and lead.stage is not None:
        probability = lead.stage.default_probability
    if probability is None:
        return Decimal("0.00")
    return Decimal(lead.estimated_value or 0) * Decimal(probability) / Decimal(100)


def pipeline_summary(
    db: Session,
    *,
    pipeline_id: UUID | None,
    start_at: datetime,
    end_at: datetime,
) -> PipelineSummary:
    """Resolve period-scoped pipeline totals and closed-deal outcomes."""

    open_query = (
        db.query(Lead)
        .options(joinedload(Lead.stage))
        .filter(
            Lead.is_active.is_(True),
            Lead.status.in_(OPEN_LEAD_STATUSES),
            Lead.created_at >= start_at,
            Lead.created_at <= end_at,
        )
    )
    if pipeline_id is not None:
        open_query = open_query.filter(Lead.pipeline_id == pipeline_id)
    open_leads = open_query.all()

    pipeline_values: dict[str, Decimal] = {}
    weighted_values: dict[str, Decimal] = {}
    for lead in open_leads:
        _add_amount(
            pipeline_values,
            currency=lead.currency,
            amount=lead.estimated_value,
        )
        _add_amount(
            weighted_values,
            currency=lead.currency,
            amount=_weighted_amount(lead),
        )

    closed_query = db.query(
        Lead.currency,
        func.sum(case((Lead.status == LeadStatus.won.value, 1), else_=0)).label(
            "won_count"
        ),
        func.sum(case((Lead.status == LeadStatus.lost.value, 1), else_=0)).label(
            "lost_count"
        ),
        func.sum(
            case(
                (
                    Lead.status == LeadStatus.won.value,
                    Lead.estimated_value,
                ),
                else_=0,
            )
        ).label("won_value"),
    ).filter(
        Lead.is_active.is_(True),
        Lead.status.in_(CLOSED_LEAD_STATUSES),
        Lead.closed_at.is_not(None),
        Lead.closed_at >= start_at,
        Lead.closed_at <= end_at,
    )
    if pipeline_id is not None:
        closed_query = closed_query.filter(Lead.pipeline_id == pipeline_id)
    closed_rows = closed_query.group_by(Lead.currency).all()

    won_deals = sum(int(row.won_count or 0) for row in closed_rows)
    lost_deals = sum(int(row.lost_count or 0) for row in closed_rows)
    won_values: dict[str, Decimal] = {}
    won_counts: dict[str, int] = {}
    for row in closed_rows:
        code = currency_code(row.currency)
        won_values[code] = won_values.get(code, Decimal("0.00")) + Decimal(
            row.won_value or 0
        )
        won_counts[code] = won_counts.get(code, 0) + int(row.won_count or 0)

    average_deal_sizes = {
        code: amount / Decimal(won_counts[code])
        for code, amount in won_values.items()
        if won_counts.get(code, 0) > 0
    }
    total_closed = won_deals + lost_deals
    win_rate = (
        Decimal(won_deals) / Decimal(total_closed) * Decimal(100)
        if total_closed
        else None
    )

    stages_query = db.query(PipelineStage).filter(PipelineStage.is_active.is_(True))
    if pipeline_id is not None:
        stages_query = stages_query.filter(PipelineStage.pipeline_id == pipeline_id)
    stages = stages_query.order_by(
        PipelineStage.order_index.asc(), PipelineStage.id.asc()
    ).all()
    stage_totals: dict[UUID, tuple[int, dict[str, Decimal]]] = {
        stage.id: (0, {}) for stage in stages
    }
    for lead in open_leads:
        if lead.stage_id not in stage_totals:
            continue
        count, values = stage_totals[lead.stage_id]
        _add_amount(values, currency=lead.currency, amount=lead.estimated_value)
        stage_totals[lead.stage_id] = (count + 1, values)

    return PipelineSummary(
        pipeline_values=pipeline_values,
        weighted_values=weighted_values,
        open_deals=len(open_leads),
        won_deals=won_deals,
        lost_deals=lost_deals,
        win_rate=win_rate,
        average_deal_sizes=average_deal_sizes,
        stages=tuple(
            StagePipelineSummary(
                id=stage.id,
                name=stage.name,
                count=stage_totals[stage.id][0],
                values=stage_totals[stage.id][1],
            )
            for stage in stages
        ),
    )


def sales_forecast(
    db: Session,
    *,
    pipeline_id: UUID | None,
    months_ahead: int = 6,
    today: date | None = None,
) -> tuple[ForecastMonth, ...]:
    """Resolve monthly expected and probability-weighted open pipeline value."""

    query = (
        db.query(Lead)
        .options(joinedload(Lead.stage))
        .filter(
            Lead.is_active.is_(True),
            Lead.expected_close_date.is_not(None),
            ~Lead.status.in_(CLOSED_LEAD_STATUSES),
        )
    )
    if pipeline_id is not None:
        query = query.filter(Lead.pipeline_id == pipeline_id)
    leads = query.all()
    current_date = today or datetime.now(UTC).date()

    rows: list[ForecastMonth] = []
    for offset in range(months_ahead):
        month_number = (current_date.month - 1 + offset) % 12 + 1
        year = current_date.year + ((current_date.month - 1 + offset) // 12)
        month_start = current_date.replace(year=year, month=month_number, day=1)
        month_end = month_start.replace(day=monthrange(year, month_number)[1])
        month_leads = [
            lead
            for lead in leads
            if lead.expected_close_date is not None
            and month_start <= lead.expected_close_date <= month_end
        ]
        expected_values: dict[str, Decimal] = {}
        weighted_values: dict[str, Decimal] = {}
        for lead in month_leads:
            _add_amount(
                expected_values,
                currency=lead.currency,
                amount=lead.estimated_value,
            )
            _add_amount(
                weighted_values,
                currency=lead.currency,
                amount=_weighted_amount(lead),
            )
        rows.append(
            ForecastMonth(
                month=month_start.strftime("%Y-%m"),
                month_label=month_start.strftime("%b %Y"),
                expected_values=expected_values,
                weighted_values=weighted_values,
                deal_count=len(month_leads),
            )
        )
    return tuple(rows)


def agent_sales_performance(
    db: Session,
    *,
    pipeline_id: UUID | None,
    start_at: datetime,
    end_at: datetime,
) -> tuple[AgentPerformance, ...]:
    """Resolve closed-deal outcomes for each recorded opportunity owner."""

    query = db.query(
        Lead.owner_agent_id.label("agent_id"),
        Lead.currency,
        func.sum(case((Lead.status == LeadStatus.won.value, 1), else_=0)).label(
            "deals_won"
        ),
        func.sum(case((Lead.status == LeadStatus.lost.value, 1), else_=0)).label(
            "deals_lost"
        ),
        func.sum(
            case(
                (
                    Lead.status == LeadStatus.won.value,
                    Lead.estimated_value,
                ),
                else_=0,
            )
        ).label("won_value"),
    ).filter(
        Lead.is_active.is_(True),
        Lead.owner_agent_id.is_not(None),
        Lead.status.in_(CLOSED_LEAD_STATUSES),
        Lead.closed_at.is_not(None),
        Lead.closed_at >= start_at,
        Lead.closed_at <= end_at,
    )
    if pipeline_id is not None:
        query = query.filter(Lead.pipeline_id == pipeline_id)
    rows = query.group_by(Lead.owner_agent_id, Lead.currency).all()

    grouped: dict[UUID, _AgentAccumulator] = {}
    for row in rows:
        state = grouped.setdefault(row.agent_id, _AgentAccumulator())
        state.deals_won += int(row.deals_won or 0)
        state.deals_lost += int(row.deals_lost or 0)
        _add_amount(
            state.won_values,
            currency=row.currency,
            amount=Decimal(row.won_value or 0),
        )

    results: list[AgentPerformance] = []
    for agent_id, state in grouped.items():
        deals_won = state.deals_won
        deals_lost = state.deals_lost
        total_deals = deals_won + deals_lost
        results.append(
            AgentPerformance(
                agent_id=agent_id,
                deals_won=deals_won,
                deals_lost=deals_lost,
                total_deals=total_deals,
                won_values=state.won_values,
                win_rate=(
                    Decimal(deals_won) / Decimal(total_deals) * Decimal(100)
                    if total_deals
                    else None
                ),
            )
        )
    results.sort(key=lambda item: (-item.deals_won, str(item.agent_id)))
    return tuple(results)


def recent_opportunities(
    db: Session,
    *,
    pipeline_id: UUID | None,
    limit: int = 10,
) -> tuple[RecentOpportunity, ...]:
    """Return the most recently updated active opportunities."""

    query = (
        db.query(Lead)
        .options(joinedload(Lead.subscriber))
        .filter(Lead.is_active.is_(True))
    )
    if pipeline_id is not None:
        query = query.filter(Lead.pipeline_id == pipeline_id)
    leads = query.order_by(Lead.updated_at.desc(), Lead.id.asc()).limit(limit).all()

    return tuple(
        RecentOpportunity(
            id=lead.id,
            title=lead.title or f"Lead {str(lead.id)[:8]}",
            contact_name=(
                (
                    lead.subscriber.display_name
                    or " ".join(
                        part
                        for part in (
                            lead.subscriber.first_name,
                            lead.subscriber.last_name,
                        )
                        if part
                    )
                    or lead.subscriber.email
                    or ""
                )
                if lead.subscriber is not None
                else ""
            ),
            status=lead.status,
            estimated_value=lead.estimated_value,
            currency=currency_code(lead.currency),
            probability=lead.probability,
            updated_at=lead.updated_at,
        )
        for lead in leads
    )


def dashboard_report(
    db: Session,
    *,
    pipeline_id: UUID | None,
    start_at: datetime,
    end_at: datetime,
) -> SalesDashboardReport:
    """Build the complete native sales dashboard read projection."""

    return SalesDashboardReport(
        summary=pipeline_summary(
            db,
            pipeline_id=pipeline_id,
            start_at=start_at,
            end_at=end_at,
        ),
        forecast=sales_forecast(db, pipeline_id=pipeline_id),
        agent_performance=agent_sales_performance(
            db,
            pipeline_id=pipeline_id,
            start_at=start_at,
            end_at=end_at,
        )[:10],
        recent_opportunities=recent_opportunities(
            db,
            pipeline_id=pipeline_id,
        ),
    )
