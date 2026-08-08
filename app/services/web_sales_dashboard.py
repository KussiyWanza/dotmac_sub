"""Presentation projection for the native Selfcare sales dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.system_user import SystemUser
from app.services import display_format
from app.services.sales import reports
from app.services.sales.service import pipelines

ALLOWED_PERIOD_DAYS = (7, 30, 90, 180)


def _pipeline_id(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None


def _period_days(value: int) -> int:
    return value if value in ALLOWED_PERIOD_DAYS else 30


def _money_groups(
    values: dict[str, Decimal],
    *,
    empty_currency: str,
) -> str:
    return display_format.format_currency_groups(
        values,
        empty_currency=empty_currency,
    )


def _percent(value: Decimal | None) -> str:
    return f"{value:.1f}%" if value is not None else display_format.MISSING_DISPLAY


def _system_user_name(user: SystemUser) -> str:
    display_name = str(user.display_name or "").strip()
    if display_name:
        return display_name
    full_name = " ".join(
        part
        for part in (
            str(user.first_name or "").strip(),
            str(user.last_name or "").strip(),
        )
        if part
    )
    return full_name or str(user.email or "").strip() or "Unavailable sales agent"


def build_dashboard_shell_context(
    db: Session,
    *,
    pipeline_id: str | None,
    period_days: int,
) -> dict[str, object]:
    """Build filters and the lazy dashboard-data URL."""

    selected_pipeline_id = _pipeline_id(pipeline_id)
    selected_period_days = _period_days(period_days)
    active_pipelines = pipelines.list(
        db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    query: dict[str, int | str] = {"period_days": selected_period_days}
    if selected_pipeline_id is not None:
        query["pipeline_id"] = str(selected_pipeline_id)
    return {
        "pipelines": active_pipelines,
        "selected_pipeline_id": (
            str(selected_pipeline_id) if selected_pipeline_id is not None else ""
        ),
        "selected_period_days": selected_period_days,
        "period_options": ALLOWED_PERIOD_DAYS,
        "dashboard_data_url": f"/admin/sales/dashboard-data?{urlencode(query)}",
    }


def build_dashboard_data_context(
    db: Session,
    *,
    pipeline_id: str | None,
    period_days: int,
) -> dict[str, object]:
    """Format the owner report for the dashboard partial."""

    selected_pipeline_id = _pipeline_id(pipeline_id)
    selected_period_days = _period_days(period_days)
    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=selected_period_days)
    report = reports.dashboard_report(
        db,
        pipeline_id=selected_pipeline_id,
        start_at=start_at,
        end_at=end_at,
    )
    default_currency = display_format.default_currency(db)
    summary = report.summary
    maximum_stage_count = max((stage.count for stage in summary.stages), default=0)
    agent_ids = {agent.agent_id for agent in report.agent_performance}
    agent_names = (
        {
            user.id: _system_user_name(user)
            for user in db.query(SystemUser).filter(SystemUser.id.in_(agent_ids)).all()
        }
        if agent_ids
        else {}
    )

    stage_rows = [
        {
            "id": str(stage.id),
            "name": stage.name,
            "count": stage.count,
            "value": _money_groups(
                stage.values,
                empty_currency=default_currency,
            ),
            "bar_percent": (
                round(stage.count / maximum_stage_count * 100)
                if maximum_stage_count
                else 0
            ),
        }
        for stage in summary.stages
    ]

    forecast_currencies = sorted(
        {
            currency
            for month in report.forecast
            for currency in (set(month.expected_values) | set(month.weighted_values))
        }
    )
    forecast_datasets: list[dict[str, object]] = []
    for currency in forecast_currencies:
        forecast_datasets.extend(
            (
                {
                    "label": f"Expected ({currency})",
                    "data": [
                        float(month.expected_values.get(currency, Decimal("0.00")))
                        for month in report.forecast
                    ],
                },
                {
                    "label": f"Weighted ({currency})",
                    "data": [
                        float(month.weighted_values.get(currency, Decimal("0.00")))
                        for month in report.forecast
                    ],
                },
            )
        )

    return {
        "has_dashboard_data": bool(
            summary.open_deals
            or summary.won_deals
            or summary.lost_deals
            or report.recent_opportunities
        ),
        "metrics": (
            {
                "pipeline_value": _money_groups(
                    summary.pipeline_values,
                    empty_currency=default_currency,
                ),
                "weighted_value": _money_groups(
                    summary.weighted_values,
                    empty_currency=default_currency,
                ),
                "open_deals": summary.open_deals,
                "win_rate": _percent(summary.win_rate),
                "average_deal_size": (
                    _money_groups(
                        summary.average_deal_sizes,
                        empty_currency=default_currency,
                    )
                    if summary.average_deal_sizes
                    else display_format.MISSING_DISPLAY
                ),
                "won_deals": summary.won_deals,
                "lost_deals": summary.lost_deals,
            }
        ),
        "stage_rows": stage_rows,
        "forecast_rows": [
            {
                "month": month.month,
                "month_label": month.month_label,
                "expected_value": _money_groups(
                    month.expected_values,
                    empty_currency=default_currency,
                ),
                "weighted_value": _money_groups(
                    month.weighted_values,
                    empty_currency=default_currency,
                ),
                "deal_count": month.deal_count,
            }
            for month in report.forecast
        ],
        "forecast_chart": {
            "labels": [month.month_label for month in report.forecast],
            "datasets": forecast_datasets,
        },
        "agent_rows": [
            {
                "id": str(agent.agent_id),
                "name": agent_names.get(agent.agent_id, "Unavailable sales agent"),
                "deals_won": agent.deals_won,
                "deals_lost": agent.deals_lost,
                "total_deals": agent.total_deals,
                "won_value": _money_groups(
                    agent.won_values,
                    empty_currency=default_currency,
                ),
                "win_rate": _percent(agent.win_rate),
            }
            for agent in report.agent_performance
        ],
        "recent_opportunities": [
            {
                "id": str(item.id),
                "title": item.title,
                "contact_name": item.contact_name,
                "status": item.status,
                "estimated_value": (
                    display_format.format_currency_amount(
                        item.estimated_value,
                        item.currency,
                    )
                    if item.estimated_value is not None
                    else display_format.MISSING_DISPLAY
                ),
                "probability": (
                    f"{item.probability}%"
                    if item.probability is not None
                    else display_format.MISSING_DISPLAY
                ),
                "updated_at": display_format.format_timestamp(item.updated_at, db),
            }
            for item in report.recent_opportunities
        ],
        "period_days": selected_period_days,
        "dashboard_data_url": (
            "/admin/sales/dashboard-data?"
            + urlencode(
                {
                    "period_days": selected_period_days,
                    **(
                        {"pipeline_id": str(selected_pipeline_id)}
                        if selected_pipeline_id is not None
                        else {}
                    ),
                }
            )
        ),
        "dashboard_error": None,
    }
