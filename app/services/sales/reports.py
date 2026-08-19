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
from typing import TypedDict
from uuid import UUID

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.lifecycle import LifecycleEventType, SubscriptionLifecycleEvent
from app.models.sales import (
    Lead,
    LeadStatus,
    PipelineStage,
    SalesOrder,
    SalesOrderStatus,
)
from app.models.system_user import SystemUser
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
class LeadKpiRow:
    agent_id: UUID
    leads_won: int
    leads_contacted: int
    blocked_customers_contacted: int
    customers_brought_back: int


@dataclass(frozen=True)
class SalesOrderKpiRow:
    agent_id: UUID
    orders_created: int
    orders_confirmed: int
    orders_paid: int
    orders_fulfilled: int
    orders_cancelled: int
    order_values: dict[str, Decimal]
    collected_values: dict[str, Decimal]


class SalesOrderKpiAccumulator(TypedDict):
    orders_created: int
    orders_confirmed: int
    orders_paid: int
    orders_fulfilled: int
    orders_cancelled: int
    order_values: dict[str, Decimal]
    collected_values: dict[str, Decimal]


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


def lead_kpi_report(
    db: Session,
    *,
    start_at: datetime,
    end_at: datetime,
    pipeline_id: UUID | None = None,
) -> tuple[LeadKpiRow, ...]:
    """Build period-scoped lead outcomes and native recovery evidence.

    Lead progression is the only native contact signal currently available.
    Recovery counts require a recorded lifecycle restoration from a blocked or
    suspended subscription; CRM engagement history is intentionally excluded.
    """

    query = db.query(Lead).filter(
        Lead.is_active.is_(True),
        Lead.owner_agent_id.is_not(None),
        or_(
            Lead.created_at.between(start_at, end_at),
            Lead.closed_at.between(start_at, end_at),
        ),
    )
    if pipeline_id is not None:
        query = query.filter(Lead.pipeline_id == pipeline_id)
    leads = query.all()

    grouped: dict[UUID, dict[str, int]] = {}
    for lead in leads:
        assert lead.owner_agent_id is not None
        state = grouped.setdefault(
            lead.owner_agent_id,
            {
                "leads_won": 0,
                "leads_contacted": 0,
                "blocked_customers_contacted": 0,
                "customers_brought_back": 0,
            },
        )
        if (
            lead.status == LeadStatus.won.value
            and lead.closed_at is not None
            and start_at <= lead.closed_at <= end_at
        ):
            state["leads_won"] += 1
        if (
            lead.status != LeadStatus.new.value
            and lead.created_at is not None
            and start_at <= lead.created_at <= end_at
        ):
            state["leads_contacted"] += 1
            if lead.subscriber is not None and lead.subscriber.status in {
                SubscriptionStatus.blocked,
                SubscriptionStatus.suspended,
            }:
                state["blocked_customers_contacted"] += 1

    subscriber_ids = {
        lead.subscriber_id for lead in leads if lead.subscriber_id is not None
    }
    if subscriber_ids:
        restored_rows = (
            db.query(Lead.owner_agent_id, Subscription.subscriber_id)
            .join(
                Subscription,
                Subscription.subscriber_id == Lead.subscriber_id,
            )
            .join(
                SubscriptionLifecycleEvent,
                SubscriptionLifecycleEvent.subscription_id == Subscription.id,
            )
            .filter(
                Lead.is_active.is_(True),
                Lead.owner_agent_id.is_not(None),
                Lead.subscriber_id.in_(subscriber_ids),
                SubscriptionLifecycleEvent.event_type.in_(
                    (LifecycleEventType.activate, LifecycleEventType.resume)
                ),
                SubscriptionLifecycleEvent.from_status.in_(
                    (SubscriptionStatus.blocked, SubscriptionStatus.suspended)
                ),
                SubscriptionLifecycleEvent.effective_at.between(start_at, end_at),
            )
            .distinct()
            .all()
        )
        restored_by_agent: dict[UUID, set[UUID]] = {}
        for agent_id, subscriber_id in restored_rows:
            if agent_id is not None and subscriber_id is not None:
                restored_by_agent.setdefault(agent_id, set()).add(subscriber_id)
        for agent_id, restored_subscribers in restored_by_agent.items():
            grouped.setdefault(
                agent_id,
                {
                    "leads_won": 0,
                    "leads_contacted": 0,
                    "blocked_customers_contacted": 0,
                    "customers_brought_back": 0,
                },
            )["customers_brought_back"] = len(restored_subscribers)

    return tuple(
        LeadKpiRow(agent_id=agent_id, **values)
        for agent_id, values in sorted(
            grouped.items(), key=lambda item: (-item[1]["leads_won"], str(item[0]))
        )
    )


def sales_order_kpi_report(
    db: Session,
    *,
    start_at: datetime,
    end_at: datetime,
) -> tuple[SalesOrderKpiRow, ...]:
    """Build order KPIs for orders created in the requested period."""

    orders = (
        db.query(SalesOrder)
        .filter(
            SalesOrder.is_active.is_(True),
            SalesOrder.owner_agent_id.is_not(None),
            SalesOrder.created_at.between(start_at, end_at),
        )
        .all()
    )
    grouped: dict[UUID, SalesOrderKpiAccumulator] = {}
    for order in orders:
        assert order.owner_agent_id is not None
        state = grouped.setdefault(
            order.owner_agent_id,
            {
                "orders_created": 0,
                "orders_confirmed": 0,
                "orders_paid": 0,
                "orders_fulfilled": 0,
                "orders_cancelled": 0,
                "order_values": {},
                "collected_values": {},
            },
        )
        state["orders_created"] = int(state["orders_created"]) + 1
        if order.status == SalesOrderStatus.confirmed.value:
            state["orders_confirmed"] += 1
        elif order.status == SalesOrderStatus.paid.value:
            state["orders_paid"] += 1
        elif order.status == SalesOrderStatus.fulfilled.value:
            state["orders_fulfilled"] += 1
        elif order.status == SalesOrderStatus.cancelled.value:
            state["orders_cancelled"] += 1
        currency = currency_code(order.currency)
        order_values = state["order_values"]
        collected_values = state["collected_values"]
        order_values[currency] = order_values.get(currency, Decimal("0.00")) + Decimal(
            order.total or 0
        )
        collected_values[currency] = collected_values.get(
            currency, Decimal("0.00")
        ) + Decimal(order.amount_paid or 0)

    return tuple(
        SalesOrderKpiRow(
            agent_id=agent_id,
            orders_created=values["orders_created"],
            orders_confirmed=values["orders_confirmed"],
            orders_paid=values["orders_paid"],
            orders_fulfilled=values["orders_fulfilled"],
            orders_cancelled=values["orders_cancelled"],
            order_values=values["order_values"],
            collected_values=values["collected_values"],
        )
        for agent_id, values in sorted(
            grouped.items(),
            key=lambda item: (-item[1]["orders_created"], str(item[0])),
        )
    )


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


def sales_agent_names(db: Session, agent_ids: set[UUID]) -> dict[UUID, str]:
    """Resolve sales-agent display names for report adapters."""

    if not agent_ids:
        return {}
    users = db.query(SystemUser).filter(SystemUser.id.in_(agent_ids)).all()
    return {
        user.id: (
            user.display_name
            or " ".join(part for part in (user.first_name, user.last_name) if part)
            or user.email
            or "Unavailable sales agent"
        )
        for user in users
    }


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
