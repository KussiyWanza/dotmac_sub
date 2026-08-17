"""Admin reporting web routes."""

import csv
import logging
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from io import StringIO
from typing import Literal, TypedDict
from urllib.parse import quote, quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import InvoiceDiscountSource, InvoiceStatus
from app.models.catalog import SubscriptionStatus
from app.models.sales import QuoteStatus
from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import crm_reporting as crm_reporting_service
from app.services import ncc_complaints_report as ncc_complaints_service
from app.services import ncc_regulatory_pack as ncc_pack_service
from app.services import ncc_report_email as ncc_weekly_delivery_service
from app.services import ncc_subscriber_report as ncc_report_service
from app.services import ncc_workbook, team_inbox_assignment, team_inbox_outbound
from app.services import team_inbox_metrics as team_inbox_metrics_service
from app.services import ticket_sla_reports as ticket_sla_reports_service
from app.services import web_document_discount_report as discount_report_service
from app.services import web_reports as web_reports_service
from app.services import web_reports_extended as web_reports_ext_service
from app.services.audit_helpers import recent_activity_for_paths
from app.services.auth_dependencies import (
    can,
    require_any_permission,
    require_permission,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/reports", tags=["web-admin-reports"])
logger = logging.getLogger(__name__)


class ReportHubLink(TypedDict):
    name: str
    url: str
    description: str
    permission: str


class ReportHubSection(TypedDict):
    id: str
    name: str
    description: str
    color: str
    links: list[ReportHubLink]


class DiscountReportTemplateContext(TypedDict):
    request: Request
    active_page: str
    active_menu: str
    current_user: dict[str, object]
    sidebar_stats: dict[str, object]
    report: discount_report_service.DocumentDiscountReport | None
    selected_tab: discount_report_service.DiscountReportTab
    error: str | None


REPORT_HUB_SECTIONS: list[ReportHubSection] = [
    {
        "id": "core",
        "name": "Core Reports",
        "description": "Primary business and operations reporting views.",
        "color": "teal",
        "links": [
            {
                "name": "Revenue",
                "url": "/admin/reports/revenue",
                "description": "Revenue metrics and recent payments",
                "permission": "reports:billing:read",
            },
            {
                "name": "Customer Report",
                "url": "/admin/reports/customers",
                "description": "Customer totals, status filters, and matching exports",
                "permission": "customer:read",
            },
            {
                "name": "Churn",
                "url": "/admin/reports/churn",
                "description": "Retention, churn reasons, and cancellations",
                "permission": "customer:read",
            },
            {
                "name": "Network Usage",
                "url": "/admin/reports/network",
                "description": "Network utilization and infrastructure stats",
                "permission": "reports:network:read",
            },
            {
                "name": "Technician",
                "url": "/admin/reports/technician",
                "description": "Technician performance and jobs",
                "permission": "reports:support:read",
            },
            {
                "name": "Ticket SLA",
                "url": "/admin/reports/ticket-sla",
                "description": "Support ticket SLA breaches and operational cleanup",
                "permission": "reports:support:read",
            },
            {
                "name": "Inbox Performance",
                "url": "/admin/reports/inbox-performance",
                "description": "Team response SLA, queue load, and agent assignments",
                "permission": "reports:support:read",
            },
            {
                "name": "Inbox Escalations",
                "url": "/admin/reports/inbox-escalations",
                "description": "Conversations that need supervisor attention",
                "permission": "reports:support:read",
            },
        ],
    },
    {
        "id": "billing",
        "name": "Billing & Finance",
        "description": "Financial and invoice analytics.",
        "color": "emerald",
        "links": [
            {
                "name": "Usage by Plan",
                "url": "/admin/reports/usage-by-plan",
                "description": "Subscriber distribution across plans",
                "permission": "reports:billing:read",
            },
            {
                "name": "Revenue per Plan",
                "url": "/admin/reports/revenue-per-plan",
                "description": "Revenue split by plan",
                "permission": "reports:billing:read",
            },
            {
                "name": "Invoice Report",
                "url": "/admin/reports/invoices",
                "description": "Invoice listing and tax details",
                "permission": "reports:billing:read",
            },
            {
                "name": "Discounts",
                "url": "/admin/reports/discounts",
                "description": "Invoice and Quote discount history",
                "permission": "reports:billing:read",
            },
            {
                "name": "Statements",
                "url": "/admin/reports/statements",
                "description": "Customer financial summaries",
                "permission": "reports:billing:read",
            },
            {
                "name": "Tax Report",
                "url": "/admin/reports/tax",
                "description": "Net output tax and WHT receivables by currency",
                "permission": "reports:billing:read",
            },
            {
                "name": "MRR Net Change",
                "url": "/admin/reports/mrr",
                "description": "Monthly recurring revenue movement",
                "permission": "reports:billing:read",
            },
            {
                "name": "New Services",
                "url": "/admin/reports/new-services",
                "description": "Recently activated subscriptions",
                "permission": "customer:read",
            },
            {
                "name": "Upcoming Charges",
                "url": "/admin/reports/upcoming-charges",
                "description": "Subscriptions with upcoming billing",
                "permission": "reports:billing:read",
            },
        ],
    },
    {
        "id": "extended",
        "name": "Extended Reports",
        "description": "Specialized and advanced analytics.",
        "color": "indigo",
        "links": [
            {
                "name": "Subscriber Growth (Trend)",
                "url": "/admin/reports/subscriber-growth",
                "description": "Time-series subscriber growth trend",
                "permission": "customer:read",
            },
            {
                "name": "Custom Pricing",
                "url": "/admin/reports/custom-pricing",
                "description": "Subscription pricing overrides and active add-ons",
                "permission": "reports:billing:read",
            },
            {
                "name": "Revenue by Category",
                "url": "/admin/reports/revenue-categories",
                "description": "Revenue segmented by category",
                "permission": "reports:billing:read",
            },
            {
                "name": "Bandwidth & Usage",
                "url": "/admin/reports/bandwidth",
                "description": "Network usage analytics and top consumers",
                "permission": "reports:network:read",
            },
        ],
    },
    {
        "id": "regulatory",
        "name": "Regulatory",
        "description": "Returns for the NCC and other regulators.",
        "color": "amber",
        "links": [
            {
                "name": "NCC Subscriber Data (Quarterly)",
                "url": "/admin/reports/ncc-subscribers",
                "description": "Active subscriptions by type, connection, speed, State & region",
                "permission": "customer:read",
            },
            {
                "name": "NCC Complaints (Quarterly)",
                "url": "/admin/reports/ncc-complaints",
                "description": "Complaint records, categories, SLA and the filing workbook",
                "permission": "provisioning:read",
            },
            {
                "name": "NCC Regulatory Pack",
                "url": "/admin/reports/ncc-pack",
                "description": "All three NCC returns assembled into one filing view",
                "permission": "provisioning:read",
            },
        ],
    },
    {
        "id": "crm-operations",
        "name": "CRM Operations",
        "description": "Customer activity, lifecycle, service quality, and inbox performance",
        "color": "violet",
        "links": [
            {
                "name": "Online Activity",
                "url": "/admin/reports/operational/online-activity",
                "description": "Customers with fresh RADIUS activity",
                "permission": "customer:read",
            },
            {
                "name": "Subscriber Lifecycle",
                "url": "/admin/reports/operational/subscriber-lifecycle",
                "description": "Subscriber and service lifecycle state from native records",
                "permission": "customer:read",
            },
            {
                "name": "Subscriber Service Quality",
                "url": "/admin/reports/operational/service-quality",
                "description": "Support, field-work, and outage observations by subscriber",
                "permission": "reports:support:read",
            },
            {
                "name": "CRM Performance",
                "url": "/admin/reports/operational/crm-performance",
                "description": "Inbox performance by service team",
                "permission": "reports:support:read",
            },
            {
                "name": "Agent Performance",
                "url": "/admin/reports/operational/agent-performance",
                "description": "Inbox handling and response performance by agent",
                "permission": "reports:support:read",
            },
            {
                "name": "My Performance",
                "url": "/admin/reports/operational/my-performance",
                "description": "The signed-in agent's own inbox performance",
                "permission": "reports:support:read",
            },
            {
                "name": "Queue & Issue Classification",
                "url": "/admin/reports/operational/queue-classification",
                "description": "Queue settlement times and recorded AI/tag classifications",
                "permission": "reports:support:read",
            },
        ],
    },
    {
        "id": "crm-finance-operations",
        "name": "CRM Finance & Delivery",
        "description": "Billing exposure, revenue, SLA, and project delivery performance",
        "color": "amber",
        "links": [
            {
                "name": "Subscriber Billing Risk",
                "url": "/admin/reports/operational/billing-risk",
                "description": "Authoritative balances, blocks, billing dates, and payment recency",
                "permission": "reports:billing:read",
            },
            {
                "name": "Subscriber Revenue & Pipeline",
                "url": "/admin/reports/operational/subscriber-revenue",
                "description": "Invoiced, collected, and outstanding value by subscriber",
                "permission": "reports:billing:read",
            },
            {
                "name": "Postpaid Customers",
                "url": "/admin/reports/operational/postpaid-customers",
                "description": "Postpaid accounts and their current billing position",
                "permission": "reports:billing:read",
            },
            {
                "name": "Revenue & Service",
                "url": "/admin/reports/operational/revenue-service",
                "description": "Revenue alongside authoritative customer outage intervals",
                "permission": "reports:billing:read",
            },
            {
                "name": "Operations SLA Violations",
                "url": "/admin/reports/operational/operations-sla",
                "description": "Overdue tickets, projects, and project tasks",
                "permission": "reports:support:read",
            },
            {
                "name": "Project & Task Performance",
                "url": "/admin/reports/operational/project-task-performance",
                "description": "Assigned, completed, overdue, and blocked project work",
                "permission": "reports:support:read",
            },
        ],
    },
]


def _base_context(
    request: Request, db: Session, active_page: str, heading: str, description: str
):
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "page_title": heading,
        "heading": heading,
        "description": description,
        "empty_title": "No reports yet",
        "empty_message": "Report data will appear once analytics are configured.",
    }


def _parse_date_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(datetime.fromisoformat(value).date(), time.min, UTC)
    except (ValueError, TypeError):
        return None


def _parse_date_end(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(datetime.fromisoformat(value).date(), time.max, UTC)
    except (ValueError, TypeError):
        return None


def _parse_report_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid report date") from None


def _operational_report_query(
    *,
    request: Request,
    date_from: str | None,
    date_to: str | None,
    page: int,
    per_page: int | None,
    personal: bool,
) -> crm_reporting_service.CrmReportQuery:
    person_id = None
    if personal:
        user = getattr(request.state, "user", None)
        raw_person_id = getattr(user, "id", None)
        try:
            person_id = UUID(str(raw_person_id)) if raw_person_id else None
        except ValueError:
            person_id = None
    return crm_reporting_service.CrmReportQuery(
        date_from=_parse_report_date(date_from),
        date_to=_parse_report_date(date_to),
        page=page,
        per_page=per_page,
        person_id=person_id,
    )


@router.get(
    "/hub",
    response_class=HTMLResponse,
    dependencies=[
        Depends(
            require_any_permission(
                "reports:billing:read",
                "reports:network:read",
                "reports:support:read",
                "customer:read",
                "provisioning:read",
            )
        )
    ],
)
def reports_hub(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    context = {
        "request": request,
        "active_page": "reports-hub",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "sections": REPORT_HUB_SECTIONS,
    }
    return templates.TemplateResponse("admin/reports/hub.html", context)


@router.get(
    "/revenue",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_revenue(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_revenue_report_data(db)

    context = {
        "request": request,
        "active_page": "reports-revenue",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_revenue": report_data["total_revenue"],
        "revenue_growth": report_data["revenue_growth"],
        "recurring_revenue": report_data["recurring_revenue"],
        "outstanding_amount": report_data["outstanding_amount"],
        "outstanding_count": report_data["outstanding_count"],
        "collection_rate": report_data["collection_rate"],
        "recent_payments": report_data["recent_payments"],
        "revenue_data": report_data["revenue_data"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/revenue.html", context)


@router.get(
    "/revenue/export",
    dependencies=[Depends(require_permission("reports:billing:export"))],
)
def reports_revenue_export(days: int | None = None, db: Session = Depends(get_db)):
    content = web_reports_service.build_revenue_export_csv(db=db, days=days)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=revenue-payments.csv"},
    )


@router.get(
    "/customers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
@router.get(
    "/subscribers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def reports_subscribers(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_subscribers_report_data(
        db,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        per_page=per_page,
    )

    context = {
        "request": request,
        "active_page": "reports-customers",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "subscriber_kpis": report_data["subscriber_kpis"],
        "total_subscribers": report_data["total_subscribers"],
        "subscriber_growth": report_data["subscriber_growth"],
        "new_this_month": report_data["new_this_month"],
        "active_subscribers": report_data["active_subscribers"],
        "suspended_subscribers": report_data["suspended_subscribers"],
        "active_rate": report_data["active_rate"],
        "status_breakdown": report_data["status_breakdown"],
        "recent_subscribers": report_data["recent_subscribers"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
        "customers": report_data["customers"],
        "growth_data": report_data["growth_data"],
        "date_from": report_data["date_from"],
        "date_to": report_data["date_to"],
        "status_filter": report_data["status_filter"],
        "status_options": report_data["status_options"],
        "plan_distribution": report_data["plan_distribution"],
        "regional_breakdown": report_data["regional_breakdown"],
        "page": report_data["page"],
        "per_page": report_data["per_page"],
        "has_previous": report_data["has_previous"],
        "has_next": report_data["has_next"],
    }
    return templates.TemplateResponse("admin/reports/subscribers.html", context)


@router.get(
    "/customers/export", dependencies=[Depends(require_permission("customer:read"))]
)
@router.get(
    "/subscribers/export", dependencies=[Depends(require_permission("customer:read"))]
)
def reports_subscribers_export(
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    content = web_reports_service.build_subscribers_export_csv(
        db=db,
        days=days,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@router.get(
    "/churn",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def reports_churn(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_churn_report_data(db)

    context = {
        "request": request,
        "active_page": "reports-churn",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "churn_kpis": report_data["churn_kpis"],
        "churn_rate": report_data["churn_rate"],
        "retention_rate": report_data["retention_rate"],
        "cancelled_count": report_data["cancelled_count"],
        "at_risk_count": report_data["at_risk_count"],
        "churn_reasons": report_data["churn_reasons"],
        "churn_data": report_data["churn_data"],
        "recent_cancellations": report_data["recent_cancellations"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/churn.html", context)


@router.get(
    "/churn/export", dependencies=[Depends(require_permission("customer:read"))]
)
def reports_churn_export(days: int | None = None, db: Session = Depends(get_db)):
    content = web_reports_service.build_churn_export_csv(db=db, days=days)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=churn-report.csv"},
    )


@router.get(
    "/network",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:network:read"))],
)
def reports_network(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_network_report_data(db=db)

    context = {
        "request": request,
        "active_page": "reports-network",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_olts": report_data["total_olts"],
        "active_olts": report_data["active_olts"],
        "total_onts": report_data["total_onts"],
        "connected_onts": report_data["connected_onts"],
        "ip_pool_usage": report_data["ip_pool_usage"],
        "used_ips": report_data["used_ips"],
        "total_ips": report_data["total_ips"],
        "active_vlans": report_data["active_vlans"],
        "pon_capacity": report_data["pon_capacity"],
        "pon_utilization": report_data["pon_utilization"],
        "total_fiber_strands": report_data["total_fiber_strands"],
        "available_fiber_strands": report_data["available_fiber_strands"],
        "total_fdh": report_data["total_fdh"],
        "splitter_capacity": report_data["splitter_capacity"],
        "olts": report_data["olts"],
        "ip_pools": report_data["pool_data"],
        "recent_ont_activity": report_data["recent_ont_activity"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/network.html", context)


@router.get(
    "/network/export",
    dependencies=[Depends(require_permission("reports:network:export"))],
)
def reports_network_export(hours: int | None = None, db: Session = Depends(get_db)):
    report_data = web_reports_service.get_network_report_data(db=db, hours=hours)
    content = web_reports_service.build_network_export_csv(report_data, hours=hours)
    return Response(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=network-usage.csv"},
    )


@router.get(
    "/technician",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:support:read"))],
)
def reports_technician(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    report_data = web_reports_service.get_technician_report_data(
        db, date_from=date_from, date_to=date_to
    )

    context = {
        "request": request,
        "active_page": "reports-technician",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "total_technicians": report_data["total_technicians"],
        "jobs_completed": report_data["jobs_completed"],
        "avg_completion_hours": report_data["avg_completion_hours"],
        "appointment_completion_rate": report_data["appointment_completion_rate"],
        "technician_stats": report_data["technician_stats"],
        "job_type_breakdown": report_data["job_type_breakdown"],
        "recent_completions": report_data["recent_completions"],
        "date_from": report_data["date_from"],
        "date_to": report_data["date_to"],
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/technician.html", context)


@router.get(
    "/technician/export",
    dependencies=[Depends(require_permission("reports:support:read"))],
)
def reports_technician_export(
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    content = web_reports_service.build_technician_export_csv(
        db=db, days=days, date_from=date_from, date_to=date_to
    )
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=technician-performance.csv"
        },
    )


@router.get(
    "/ticket-sla",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:support:read"))],
)
def reports_ticket_sla(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    open_only: bool = False,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    start_at = _parse_date_start(date_from)
    end_at = _parse_date_end(date_to)
    context = {
        "request": request,
        "active_page": "reports-ticket-sla",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "date_from": date_from or "",
        "date_to": date_to or "",
        "open_only": open_only,
        "summary": ticket_sla_reports_service.summary(db, start_at, end_at),
        "trend": ticket_sla_reports_service.trend_daily(db, start_at, end_at),
        "violations": ticket_sla_reports_service.violation_records(
            db,
            start_at=start_at,
            end_at=end_at,
            open_only=open_only,
            limit=100,
        ),
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/ticket_sla.html", context)


def _seconds_label(value: float | None) -> str:
    if value is None:
        return "-"
    minutes = value / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes / 60:.1f}h"


def _percent(value: float | None) -> float:
    return round((value or 0) * 100, 1)


def _inbox_team_rows(db: Session, response_sla_seconds: int, include_inactive: bool):
    rows = team_inbox_metrics_service.team_performance_report(
        db,
        response_sla_seconds=response_sla_seconds,
        include_inactive=include_inactive,
    )
    team_rows = []
    for row in rows:
        metrics = row.metrics
        response_rate = (
            metrics.responded_count / metrics.inbound_message_count
            if metrics.inbound_message_count
            else None
        )
        breach_rate = (
            metrics.response_sla_breached_count / metrics.inbound_message_count
            if metrics.inbound_message_count
            else None
        )
        team_rows.append(
            {
                "team_id": row.service_team_id,
                "name": row.service_team_name,
                "capabilities": ", ".join(row.service_team_capabilities),
                "response_sla_seconds": row.response_sla_seconds,
                "conversation_count": metrics.conversation_count,
                "open_count": metrics.open_count,
                "unassigned_open_count": metrics.unassigned_open_count,
                "assigned_open_count": metrics.assigned_open_count,
                "inbound_message_count": metrics.inbound_message_count,
                "outbound_message_count": metrics.outbound_message_count,
                "responded_count": metrics.responded_count,
                "response_sla_breached_count": metrics.response_sla_breached_count,
                "response_rate": response_rate,
                "response_rate_percent": _percent(response_rate),
                "response_sla_breach_rate": breach_rate,
                "response_sla_breach_rate_percent": _percent(breach_rate),
                "average_first_response": _seconds_label(
                    metrics.average_first_response_seconds
                ),
                "average_queue_wait": _seconds_label(
                    metrics.average_queue_wait_seconds
                ),
            }
        )
    return team_rows


def _inbox_agent_rows(db: Session):
    rows = team_inbox_metrics_service.agent_performance_report(db)
    return [
        {
            "person_id": row.person_id,
            "service_team_id": row.service_team_id,
            "service_team_name": row.service_team_name,
            "service_team_capabilities": ", ".join(row.service_team_capabilities),
            "active_assignment_count": row.metrics.active_assignment_count,
            "handled_conversation_count": row.metrics.handled_conversation_count,
            "average_first_response": _seconds_label(
                row.metrics.average_first_response_seconds
            ),
            "average_queue_wait": _seconds_label(
                row.metrics.average_queue_wait_seconds
            ),
        }
        for row in rows
    ]


_ESCALATION_REASON_LABELS = {
    "response_sla_breached": "Response SLA breached",
    "unassigned_queue_breached": "Unassigned queue breached",
    "no_available_agent": "No available agent",
}


def _reason_label(reason: str) -> str:
    return _ESCALATION_REASON_LABELS.get(reason, reason.replace("_", " ").title())


def _inbox_escalation_rows(
    db: Session,
    response_sla_seconds: int,
    queue_sla_seconds: int,
    include_inactive: bool,
):
    rows = team_inbox_metrics_service.escalation_candidates(
        db,
        response_sla_seconds=response_sla_seconds,
        queue_sla_seconds=queue_sla_seconds,
        include_inactive=include_inactive,
    )
    return [
        {
            "conversation_id": row.conversation_id,
            "service_team_id": row.service_team_id,
            "service_team_name": row.service_team_name,
            "service_team_capabilities": ", ".join(row.service_team_capabilities),
            "subject": row.subject or "(No subject)",
            "contact_address": row.contact_address or "-",
            "status": row.status,
            "reasons": [_reason_label(reason) for reason in row.reasons],
            "reason_keys": list(row.reasons),
            "response_sla": _seconds_label(row.response_sla_seconds),
            "queue_sla": _seconds_label(row.queue_sla_seconds),
            "pending_response": _seconds_label(row.pending_response_seconds),
            "queue_wait": _seconds_label(row.queue_wait_seconds),
            "assigned_person_id": row.assigned_person_id or "-",
            "available_agent_count": row.available_agent_count,
        }
        for row in rows
    ]


def _active_service_team_options(db: Session):
    return team_inbox_metrics_service.active_service_team_options(db)


def _inbox_escalation_return_url(
    next_url: str | None,
    *,
    status: str,
    message: str,
) -> str:
    target = str(next_url or "").strip()
    if not target.startswith("/admin/reports/inbox-escalations"):
        target = "/admin/reports/inbox-escalations"
    separator = "&" if "?" in target else "?"
    return (
        f"{target}{separator}status={quote_plus(status)}&message={quote_plus(message)}"
    )


@router.get(
    "/inbox-performance",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:support:read"))],
)
def reports_inbox_performance(
    request: Request,
    response_sla_seconds: int = Query(default=900, ge=60, le=86400),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    team_rows = _inbox_team_rows(db, response_sla_seconds, include_inactive)
    agent_rows = _inbox_agent_rows(db)
    inbound_total = sum(row["inbound_message_count"] for row in team_rows)
    breached_total = sum(row["response_sla_breached_count"] for row in team_rows)
    responded_total = sum(row["responded_count"] for row in team_rows)
    context = {
        "request": request,
        "active_page": "reports-inbox-performance",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "response_sla_seconds": response_sla_seconds,
        "include_inactive": include_inactive,
        "team_rows": team_rows,
        "agent_rows": agent_rows,
        "team_count": len(team_rows),
        "open_count": sum(row["open_count"] for row in team_rows),
        "unassigned_open_count": sum(row["unassigned_open_count"] for row in team_rows),
        "breached_total": breached_total,
        "response_rate_percent": _percent(
            responded_total / inbound_total if inbound_total else None
        ),
        "breach_rate_percent": _percent(
            breached_total / inbound_total if inbound_total else None
        ),
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/inbox_performance.html", context)


@router.get(
    "/inbox-performance/export",
    dependencies=[Depends(require_permission("reports:support:read"))],
)
def reports_inbox_performance_export(
    response_sla_seconds: int = Query(default=900, ge=60, le=86400),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
):
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "name",
            "capabilities",
            "response_sla_seconds",
            "conversation_count",
            "open_count",
            "unassigned_open_count",
            "inbound_message_count",
            "outbound_message_count",
            "responded_count",
            "response_sla_breached_count",
            "response_rate_percent",
            "response_sla_breach_rate_percent",
            "average_first_response",
            "average_queue_wait",
        ],
    )
    writer.writeheader()
    for row in _inbox_team_rows(db, response_sla_seconds, include_inactive):
        writer.writerow({field: row[field] for field in writer.fieldnames})
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inbox-performance.csv"},
    )


@router.get(
    "/inbox-escalations",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:support:read"))],
)
def reports_inbox_escalations(
    request: Request,
    response_sla_seconds: int = Query(default=900, ge=60, le=86400),
    queue_sla_seconds: int = Query(default=600, ge=60, le=86400),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    rows = _inbox_escalation_rows(
        db,
        response_sla_seconds,
        queue_sla_seconds,
        include_inactive,
    )
    context = {
        "request": request,
        "active_page": "reports-inbox-escalations",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "response_sla_seconds": response_sla_seconds,
        "queue_sla_seconds": queue_sla_seconds,
        "include_inactive": include_inactive,
        "rows": rows,
        "service_team_options": _active_service_team_options(db),
        "candidate_count": len(rows),
        "response_breach_count": sum(
            "response_sla_breached" in row["reason_keys"] for row in rows
        ),
        "queue_breach_count": sum(
            "unassigned_queue_breached" in row["reason_keys"] for row in rows
        ),
        "no_agent_count": sum(
            "no_available_agent" in row["reason_keys"] for row in rows
        ),
        "recent_activities": recent_activity_for_paths(db, ["/admin/reports"]),
    }
    return templates.TemplateResponse("admin/reports/inbox_escalations.html", context)


@router.post(
    "/inbox-escalations/{conversation_id}/action",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def reports_inbox_escalation_action(
    conversation_id: str,
    request: Request,
    service_team_id: str = Form(...),
    action: str = Form("auto_assign"),
    reason: str = Form(""),
    next: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    from app.services import web_admin as web_admin_service

    try:
        conversation_uuid = UUID(str(conversation_id))
        team_uuid = UUID(str(service_team_id))
    except ValueError:
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message="Invalid conversation or team.",
            ),
            status_code=303,
        )

    conversation = db.get(InboxConversation, conversation_uuid)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message="Conversation not found.",
            ),
            status_code=303,
        )
    if conversation.status == InboxConversationStatus.resolved.value:
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message="Resolved conversations cannot be escalated.",
            ),
            status_code=303,
        )

    actor_id = web_admin_service.get_actor_id(request)
    clean_reason = reason.strip() or None
    if action == "queue":
        result = team_inbox_assignment.queue_conversation_for_team(
            db,
            conversation=conversation,
            service_team_id=team_uuid,
            assigned_by_person_id=actor_id,
            reason=clean_reason,
        )
    else:
        result = team_inbox_assignment.assign_conversation_to_available_agent(
            db,
            conversation=conversation,
            service_team_id=team_uuid,
            assigned_by_person_id=actor_id,
            reason=clean_reason,
        )

    if result.kind == "invalid_team":
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message=result.reason or "Invalid target team.",
            ),
            status_code=303,
        )

    db.commit()
    if result.kind == "assigned":
        message = "Conversation assigned to an available agent."
    else:
        message = "Conversation moved to the target team queue."
    return RedirectResponse(
        _inbox_escalation_return_url(next, status="success", message=message),
        status_code=303,
    )


@router.post(
    "/inbox-escalations/{conversation_id}/reply",
    dependencies=[Depends(require_permission("support:ticket:update"))],
)
def reports_inbox_escalation_reply(
    conversation_id: str,
    request: Request,
    body_text: str = Form(...),
    next: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    from app.services import web_admin as web_admin_service

    try:
        conversation_uuid = UUID(str(conversation_id))
    except ValueError:
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message="Invalid conversation.",
            ),
            status_code=303,
        )

    clean_body = body_text.strip()
    if not clean_body:
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message="Reply body is required.",
            ),
            status_code=303,
        )

    conversation = db.get(InboxConversation, conversation_uuid)
    if conversation is None or not conversation.is_active:
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message="Conversation not found.",
            ),
            status_code=303,
        )

    body_html = (
        "<p>" + "<br>".join(escape(line) for line in clean_body.splitlines()) + "</p>"
    )
    result = team_inbox_outbound.send_inbox_reply(
        db,
        conversation=conversation,
        payload=team_inbox_outbound.InboxReplyPayload(
            body_html=body_html,
            body_text=clean_body,
            sent_by_person_id=web_admin_service.get_actor_id(request),
            metadata={"source_route": "admin_inbox_escalation_reply"},
        ),
    )

    if result.kind not in {"sent", "queued"}:
        return RedirectResponse(
            _inbox_escalation_return_url(
                next,
                status="error",
                message=result.reason or "Reply could not be sent.",
            ),
            status_code=303,
        )

    db.commit()
    sender = result.from_address or result.sender_key or "team sender"
    return RedirectResponse(
        _inbox_escalation_return_url(
            next,
            status="success",
            message=(
                f"Reply queued from {sender}."
                if result.kind == "queued"
                else f"Reply sent from {sender}."
            ),
        ),
        status_code=303,
    )


@router.get(
    "/inbox-escalations/export",
    dependencies=[Depends(require_permission("reports:support:read"))],
)
def reports_inbox_escalations_export(
    response_sla_seconds: int = Query(default=900, ge=60, le=86400),
    queue_sla_seconds: int = Query(default=600, ge=60, le=86400),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
):
    output = StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "conversation_id",
            "service_team_name",
            "service_team_capabilities",
            "subject",
            "contact_address",
            "status",
            "reasons",
            "response_sla",
            "queue_sla",
            "pending_response",
            "queue_wait",
            "assigned_person_id",
            "available_agent_count",
        ],
    )
    writer.writeheader()
    for row in _inbox_escalation_rows(
        db,
        response_sla_seconds,
        queue_sla_seconds,
        include_inactive,
    ):
        export_row = {field: row[field] for field in writer.fieldnames}
        export_row["reasons"] = "; ".join(row["reasons"])
        writer.writerow(export_row)
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inbox-escalations.csv"},
    )


# ===================================================================
# Extended Reports (04_administration features)
# ===================================================================


@router.get(
    "/subscriber-growth",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def reports_subscriber_growth(
    request: Request,
    days: int = 30,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_subscriber_growth_data(db, days=days)
    ctx = _base_context(
        request,
        db,
        "reports-subscriber-growth",
        "Subscriber Growth",
        "Customer growth trend over time",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/subscriber_growth.html", ctx)


@router.get(
    "/usage-by-plan",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_usage_by_plan(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_usage_by_plan_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-usage-plan",
        "Usage by Plan",
        "Subscriber distribution across plans",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/usage_by_plan.html", ctx)


@router.get(
    "/upcoming-charges",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_upcoming_charges(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_upcoming_charges_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-upcoming-charges",
        "Upcoming Charges",
        "Active subscriptions with upcoming billing",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/upcoming_charges.html", ctx)


@router.get(
    "/revenue-per-plan",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_revenue_per_plan(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_revenue_per_plan_data(
        db, date_from=date_from, date_to=date_to
    )
    ctx = _base_context(
        request,
        db,
        "reports-revenue-plan",
        "Revenue per Plan",
        "Revenue aggregated by service plan",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/revenue_per_plan.html", ctx)


@router.get(
    "/invoices",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_invoices(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_invoice_report_data(
        db, date_from=date_from, date_to=date_to, status=status
    )
    ctx = _base_context(
        request,
        db,
        "reports-invoices",
        "Invoice Report",
        "Detailed invoice listing with tax breakdown",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/invoices.html", ctx)


@router.get(
    "/statements",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_statements(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_statements_data(db)
    ctx = _base_context(
        request, db, "reports-statements", "Statements", "Customer financial summaries"
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/statements.html", ctx)


@router.get(
    "/tax",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_tax(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_tax_report_data(
        db,
        date_from=date_from,
        date_to=date_to,
    )
    ctx = _base_context(
        request,
        db,
        "reports-tax",
        "Tax Accounting Report",
        "Net output-tax liability and withholding-tax receivables",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/tax.html", ctx)


@router.get(
    "/mrr",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_mrr(
    request: Request,
    year: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_mrr_data(
        db,
        year=year,
        date_from=date_from,
        date_to=date_to,
    )
    ctx = _base_context(
        request,
        db,
        "reports-mrr",
        "MRR Net Change",
        "Monthly recurring revenue movement",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/mrr.html", ctx)


@router.get(
    "/new-services",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def reports_new_services(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_new_services_data(
        db, date_from=date_from, date_to=date_to
    )
    ctx = _base_context(
        request,
        db,
        "reports-new-services",
        "New Services",
        "Recently activated subscriptions",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/new_services.html", ctx)


@router.get(
    "/discounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_discounts(
    request: Request,
    tab: discount_report_service.DiscountReportTab = Query(
        default=discount_report_service.DiscountReportTab.invoices
    ),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    search: str | None = Query(default=None),
    customer: str | None = Query(default=None),
    salesperson_id: UUID | None = Query(default=None),
    discount_type: discount_report_service.DocumentDiscountType | None = Query(
        default=None
    ),
    invoice_status: InvoiceStatus | None = Query(default=None),
    quote_status: QuoteStatus | None = Query(default=None),
    source: InvoiceDiscountSource | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: Literal[10, 25, 50, 100] = Query(default=25),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    context = DiscountReportTemplateContext(
        request=request,
        active_page="reports-discounts",
        active_menu="reports",
        current_user=get_current_user(request),
        sidebar_stats=get_sidebar_stats(db),
        report=None,
        selected_tab=tab,
        error=None,
    )
    query = discount_report_service.DocumentDiscountReportQuery(
        tab=tab,
        date_from=date_from,
        date_to=date_to,
        customer=search or customer,
        salesperson_id=salesperson_id,
        discount_type=discount_type,
        invoice_status=invoice_status if tab.value == "invoices" else None,
        quote_status=quote_status if tab.value == "quotes" else None,
        source=source if tab.value == "invoices" else None,
        page=page,
        page_size=per_page,
    )
    try:
        report = discount_report_service.build_document_discount_report(db, query)
    except DomainError as exc:
        context["error"] = exc.message
        return templates.TemplateResponse(
            "admin/reports/discounts.html", dict(context), status_code=400
        )
    except SQLAlchemyError as exc:
        logger.error(
            "document_discount_report_load_failed",
            extra={
                "error_type": type(exc).__name__,
                "selected_tab": tab.value,
                "has_customer_filter": bool((search or customer or "").strip()),
            },
        )
        db_session_adapter.release_read_transaction(db)
        context["error"] = "Discounts could not be loaded. No data was changed."
        return templates.TemplateResponse(
            "admin/reports/discounts.html", dict(context), status_code=503
        )
    context["report"] = report
    return templates.TemplateResponse("admin/reports/discounts.html", dict(context))


@router.get(
    "/custom-pricing",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_custom_pricing(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_custom_pricing_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-custom-pricing",
        "Custom Pricing",
        "Non-standard subscription pricing overrides and active add-ons",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/custom_pricing.html", ctx)


@router.get(
    "/revenue-categories",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def reports_revenue_categories(request: Request, db: Session = Depends(get_db)):
    data = web_reports_ext_service.get_revenue_categories_data(db)
    ctx = _base_context(
        request,
        db,
        "reports-revenue-categories",
        "Revenue by Category",
        "Income breakdown by service type",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/revenue_categories.html", ctx)


@router.get(
    "/bandwidth",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:network:read"))],
)
def reports_bandwidth(
    request: Request,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    show_chart: bool = False,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_bandwidth_report_data(
        db,
        days=days,
        date_from=date_from,
        date_to=date_to,
        show_chart=show_chart,
    )
    ctx = _base_context(
        request,
        db,
        "reports-bandwidth",
        "Bandwidth & Usage",
        "Network usage analytics and top consumers",
    )
    ctx.update(data)
    return templates.TemplateResponse("admin/reports/bandwidth.html", ctx)


@router.get(
    "/bandwidth/export",
    dependencies=[Depends(require_permission("reports:network:export"))],
)
def reports_bandwidth_export(
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    data = web_reports_ext_service.get_bandwidth_report_data(
        db,
        days=days,
        date_from=date_from,
        date_to=date_to,
    )
    content = web_reports_ext_service.build_bandwidth_report_export_csv(data)
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=bandwidth-usage-report.csv"
        },
    )


# ── NCC quarterly Subscriber & Capacity return ──────────────────────────────
def _ncc_params(
    as_of: str | None,
    statuses: list[str],
    reseller_id: str | None,
    access_capacity_gbps: str | None,
    unutilized_capacity_mbps: str | None,
    points_of_presence: str | None,
    data_usage_tb: str | None,
):
    return ncc_report_service.parse_report_params(
        as_of=as_of,
        statuses=",".join(statuses),
        reseller_id=reseller_id,
        capacity={
            "access_capacity_gbps": access_capacity_gbps,
            "unutilized_capacity_mbps": unutilized_capacity_mbps,
            "points_of_presence": points_of_presence,
            "data_usage_tb": data_usage_tb,
        },
    )


@router.get(
    "/ncc-subscribers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def reports_ncc_subscribers(
    request: Request,
    as_of: str | None = None,
    statuses: list[str] = Query(default=[]),
    reseller_id: str | None = None,
    access_capacity_gbps: str | None = None,
    unutilized_capacity_mbps: str | None = None,
    points_of_presence: str | None = None,
    data_usage_tb: str | None = None,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    params = _ncc_params(
        as_of,
        statuses,
        reseller_id,
        access_capacity_gbps,
        unutilized_capacity_mbps,
        points_of_presence,
        data_usage_tb,
    )
    report = ncc_report_service.build_ncc_subscriber_report(db, params)
    context = {
        "request": request,
        "active_page": "reports-ncc-subscribers",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "report": report,
        "status_options": [s.value for s in SubscriptionStatus],
        "selected_statuses": report["parameters"]["active_statuses"],
        "form": {
            "as_of": as_of or "",
            "reseller_id": reseller_id or "",
            "access_capacity_gbps": access_capacity_gbps or "",
            "unutilized_capacity_mbps": unutilized_capacity_mbps or "",
            "points_of_presence": points_of_presence or "",
            "data_usage_tb": data_usage_tb or "",
        },
    }
    return templates.TemplateResponse("admin/reports/ncc_subscribers.html", context)


@router.get(
    "/ncc-subscribers/export",
    dependencies=[Depends(require_permission("customer:read"))],
)
def reports_ncc_subscribers_export(
    as_of: str | None = None,
    statuses: list[str] = Query(default=[]),
    reseller_id: str | None = None,
    access_capacity_gbps: str | None = None,
    unutilized_capacity_mbps: str | None = None,
    points_of_presence: str | None = None,
    data_usage_tb: str | None = None,
    db: Session = Depends(get_db),
):
    params = _ncc_params(
        as_of,
        statuses,
        reseller_id,
        access_capacity_gbps,
        unutilized_capacity_mbps,
        points_of_presence,
        data_usage_tb,
    )
    report = ncc_report_service.build_ncc_subscriber_report(db, params)
    content = ncc_report_service.build_ncc_subscriber_csv(report)
    stamp = report["parameters"]["as_of"][:10]
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=ncc-subscribers-{stamp}.csv"
        },
    )


# ── NCC quarterly Complaints return (①) ──────────────────────────────────────
def _ncc_complaints_window(
    date_from: str | None, date_to: str | None
) -> tuple[datetime, datetime]:
    """Bound the complaints window. Defaults to the trailing 90 days — the
    quarterly cadence the return is filed on — anchored on ``created_at``."""
    end = _parse_date_end(date_to) or datetime.now(UTC)
    start = _parse_date_start(date_from) or (end - timedelta(days=90))
    if end < start:
        start, end = end, start
    return start, end


@router.get(
    "/ncc-complaints",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def reports_ncc_complaints(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: Literal[20, 50, 100] = Query(default=20),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    start, end = _ncc_complaints_window(date_from, date_to)
    snapshot = ncc_complaints_service.query_report(
        db=db,
        query=ncc_complaints_service.NccComplaintsReportQuery(start=start, end=end),
    )
    report = snapshot.as_legacy_dict()
    requested_list_query = (
        ncc_complaints_service.NCC_COMPLAINTS_LIST_DEFINITION.build_query(
            search=None,
            filters={"date_from": date_from, "date_to": date_to},
            page=page,
            per_page=per_page,
        )
    )
    table_page = ncc_complaints_service.paginate_report(
        snapshot,
        list_query=requested_list_query,
    )
    # Surface, per row, whether it is filable — the workbook's own validator
    # is the authority, so the officer sees exactly what CRM's export would.
    rows = []
    for record in ncc_workbook.export_rows(
        [item.as_mapping() for item in table_page.records]
    ):
        status = ncc_workbook.validation_status(record)
        rows.append(
            {"record": record, "validation": status, "ok": status.startswith("[OK]")}
        )
    not_filable = sum(
        1
        for record in ncc_workbook.export_rows(report["records"])
        if not ncc_workbook.validation_status(record).startswith("[OK]")
    )
    weekly_configuration = ncc_weekly_delivery_service.get_configuration(db=db)
    weekly_runs = ncc_weekly_delivery_service.list_recent_runs(
        db=db,
        query=ncc_weekly_delivery_service.NccWeeklyRunHistoryQuery(limit=10),
    )
    context = {
        "request": request,
        "active_page": "reports-ncc-complaints",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "report": report,
        "columns": report["columns"],
        "rows": rows,
        "not_filable": not_filable,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "weekly_configuration": weekly_configuration,
        "weekly_runs": weekly_runs,
        "ncc_weekdays": tuple(ncc_weekly_delivery_service.NccWeekday),
        "can_manage_ncc_email": can(request, "notification:write"),
        "list_query": table_page.list_query,
        "page_meta": table_page.page_meta,
    }
    return templates.TemplateResponse("admin/reports/ncc_complaints.html", context)


@router.get(
    "/ncc-complaints/export",
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def reports_ncc_complaints_export(
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    start, end = _ncc_complaints_window(date_from, date_to)
    report = ncc_complaints_service.build_report(db, start=start, end=end)
    rows = ncc_workbook.export_rows(report["records"])
    content = ncc_workbook.build_workbook(rows, report["columns"])
    filename = ncc_workbook.export_filename(end)
    return Response(
        content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── NCC Regulatory Pack (① + ② + ③) ──────────────────────────────────────────
def _ncc_pack_window(
    date_from: str | None, date_to: str | None
) -> tuple[datetime, datetime]:
    return _ncc_complaints_window(date_from, date_to)


@router.get(
    "/ncc-regulatory-pack",
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def reports_ncc_regulatory_pack(
    date_from: str | None = None,
    date_to: str | None = None,
    as_of: str | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    """The full pack as JSON. Sections degrade to ``available: false`` when an
    upstream (sub/erp) is unreachable — the pack never fabricates a section."""
    start, end = _ncc_pack_window(date_from, date_to)
    pack = ncc_pack_service.build_regulatory_pack(
        db, start_dt=start, end_dt=end, as_of=as_of, year=year
    )
    return JSONResponse(pack)


@router.get(
    "/ncc-pack",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def reports_ncc_pack_page(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    as_of: str | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    start, end = _ncc_pack_window(date_from, date_to)
    pack = ncc_pack_service.build_regulatory_pack(
        db, start_dt=start, end_dt=end, as_of=as_of, year=year
    )
    context = {
        "request": request,
        "active_page": "reports-ncc-pack",
        "active_menu": "reports",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "pack": pack,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "as_of": as_of or "",
        "year": year or "",
    }
    return templates.TemplateResponse("admin/reports/ncc_pack.html", context)


@router.get(
    "/ncc-regulatory-pack.pdf",
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def reports_ncc_regulatory_pack_pdf(
    date_from: str | None = None,
    date_to: str | None = None,
    as_of: str | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    start, end = _ncc_pack_window(date_from, date_to)
    pack = ncc_pack_service.build_regulatory_pack(
        db, start_dt=start, end_dt=end, as_of=as_of, year=year
    )
    html_content = _render_ncc_pack_html(pack, start, end)
    try:
        from app.services.billing_invoice_pdf import _ensure_weasyprint_pydyf_compat

        _ensure_weasyprint_pydyf_compat()
        from weasyprint import HTML

        pdf_bytes = HTML(string=html_content).write_pdf()
        content_type = "application/pdf"
        extension = "pdf"
        content: bytes = pdf_bytes
    except Exception:
        # No silent fabrication: if PDF rendering is unavailable, hand back the
        # same content as HTML rather than an empty or fake document.
        content = html_content.encode("utf-8")
        content_type = "text/html; charset=utf-8"
        extension = "html"
    filename = ncc_workbook.regulatory_pack_filename(start, end, extension)
    return Response(
        content,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _render_ncc_pack_html(pack: dict, start: datetime, end: datetime) -> str:
    meta = pack.get("meta", {})
    sources = meta.get("sources", {})

    def _section_row(label: str, section: dict) -> str:
        available = section.get("available", False)
        status = "available" if available else "unavailable"
        detail = "" if available else escape(str(section.get("error") or ""))
        return f"<tr><td>{escape(label)}</td><td>{status}</td><td>{detail}</td></tr>"

    complete = "COMPLETE" if meta.get("complete") else "INCOMPLETE — see sections below"
    rows = "".join(
        [
            _section_row("① Quarterly complaints", pack.get("complaints", {})),
            _section_row("② Quarterly subscribers", pack.get("subscribers", {})),
            _section_row("③ Annual financials", pack.get("financials", {})),
            _section_row("③ Annual staff", pack.get("staff", {})),
        ]
    )
    return (
        "<html><head><meta charset='utf-8'>"
        "<style>body{font-family:sans-serif}table{border-collapse:collapse}"
        "td,th{border:1px solid #999;padding:6px 10px;text-align:left}</style>"
        "</head><body>"
        "<h1>NCC Regulatory Pack</h1>"
        f"<p>Reporting window: {start:%Y-%m-%d} — {end:%Y-%m-%d}</p>"
        f"<p>Status: <strong>{complete}</strong></p>"
        f"<p>Sources reachable: {escape(str(sources))}</p>"
        "<table><tr><th>Section</th><th>Status</th><th>Detail</th></tr>"
        f"{rows}</table>"
        "</body></html>"
    )


@router.post(
    "/ncc-email-settings",
    dependencies=[Depends(require_permission("notification:write"))],
)
def reports_ncc_email_settings(
    request: Request,
    enabled: bool = Form(default=False),
    recipient: str = Form(default=""),
    cc: str = Form(default=""),
    bcc: str = Form(default=""),
    sender_key: str = Form(default=""),
    subject: str = Form(default=""),
    body_template: str = Form(default=""),
    local_time: str = Form(default="08:00"),
    timezone: str = Form(default="Africa/Lagos"),
    send_day: str = Form(default="tuesday"),
    lookback_days: int = Form(default=7),
    db: Session = Depends(get_db),
):
    """Map the form to the typed NCC weekly-delivery configuration owner."""
    from app.services.owner_commands import CommandContext
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    if isinstance(current_user, dict):
        actor_value = current_user.get("actor_id") or current_user.get("email")
    else:
        actor_value = getattr(current_user, "id", None) or getattr(
            current_user, "email", None
        )
    actor = str(actor_value or "admin")
    try:
        ncc_weekly_delivery_service.update_configuration(
            db=db,
            command=ncc_weekly_delivery_service.UpdateNccWeeklyDeliveryConfigurationCommand(
                context=CommandContext.system(
                    actor=actor,
                    scope="ncc.weekly_delivery_configuration",
                    reason="administrator updated NCC weekly delivery configuration",
                ),
                enabled=enabled,
                to_address=recipient,
                cc_addresses=cc,
                bcc_addresses=bcc,
                sender_key=sender_key,
                subject=subject,
                body_template=body_template,
                local_time=local_time,
                timezone=timezone,
                send_day=send_day,
                lookback_days=lookback_days,
            ),
        )
    except ncc_weekly_delivery_service.NccWeeklyDeliveryError as exc:
        return RedirectResponse(
            url=(
                "/admin/reports/ncc-complaints?settings_error="
                f"{quote_plus(exc.message)}"
            ),
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/reports/ncc-complaints?saved=1", status_code=303
    )


@router.get(
    "/ncc-weekly-runs/{run_id}/download",
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def reports_ncc_weekly_run_download(
    run_id: UUID,
    db: Session = Depends(get_db),
):
    try:
        artifact = ncc_weekly_delivery_service.get_artifact(
            db=db,
            query=ncc_weekly_delivery_service.NccWeeklyArtifactQuery(run_id=run_id),
        )
    except ncc_weekly_delivery_service.NccWeeklyDeliveryError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return Response(
        artifact.content,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(artifact.filename)}"
            )
        },
    )


_OPERATIONAL_REPORT_PERMISSIONS = tuple(
    sorted(
        {
            definition.permission
            for definition in crm_reporting_service.REPORT_DEFINITIONS.values()
        }
    )
)


def _operational_definition(
    request: Request, report_slug: str
) -> crm_reporting_service.CrmReportDefinition:
    try:
        slug = crm_reporting_service.CrmReportSlug(report_slug)
    except ValueError:
        raise HTTPException(status_code=404, detail="Report not found") from None
    definition = crm_reporting_service.REPORT_DEFINITIONS[slug]
    if not can(request, definition.permission):
        raise HTTPException(status_code=403, detail="Forbidden")
    return definition


@router.get(
    "/operational/{report_slug}/export",
    dependencies=[Depends(require_any_permission(*_OPERATIONAL_REPORT_PERMISSIONS))],
)
def reports_operational_export(
    request: Request,
    report_slug: str,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
):
    definition = _operational_definition(request, report_slug)
    if not definition.supports_date_filter:
        date_from = date_to = None
    query = _operational_report_query(
        request=request,
        date_from=date_from,
        date_to=date_to,
        page=1,
        per_page=None,
        personal=definition.slug == crm_reporting_service.CrmReportSlug.MY_PERFORMANCE,
    )
    report = crm_reporting_service.get_report(db, slug=definition.slug, query=query)
    return Response(
        content=crm_reporting_service.build_csv(report),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{report_slug}.csv"'},
    )


@router.get(
    "/operational/{report_slug}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_any_permission(*_OPERATIONAL_REPORT_PERMISSIONS))],
)
def reports_operational_page(
    request: Request,
    report_slug: str,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    definition = _operational_definition(request, report_slug)
    if not definition.supports_date_filter:
        date_from = date_to = None
    query = _operational_report_query(
        request=request,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
        personal=definition.slug == crm_reporting_service.CrmReportSlug.MY_PERFORMANCE,
    )
    report = crm_reporting_service.get_report(db, slug=definition.slug, query=query)
    context = _base_context(
        request,
        db,
        f"reports-{report_slug}",
        definition.title,
        definition.description,
    )
    context.update(
        {
            "report": report,
            "date_from": date_from or "",
            "date_to": date_to or "",
        }
    )
    return templates.TemplateResponse("admin/reports/operational.html", context)


# ── AI: on-demand insight for an owned report projection ─────────────────────
# User-driven: an operator clicks "Get AI insight" on a report page and the
# advisor for that report set runs against the OWNER's projection. The engine
# never queries a domain model — the route fetches the projection and hands it
# in — so the source-of-truth boundary holds by construction.
_REPORT_ADVISORS: dict[str, str] = {
    # advisor_key -> the report page it advises on
    "ticket_sla_advisor": "ticket-sla",
}


def _fetch_report_for_advisor(
    db: Session, advisor_key: str, date_from: str | None, date_to: str | None
) -> tuple[dict, str, str | None]:
    """Fetch the owned projection an advisor reads. Returns
    (report, entity_type, entity_id)."""
    if advisor_key == "ticket_sla_advisor":
        start_at = _parse_date_start(date_from)
        end_at = _parse_date_end(date_to)
        report = ticket_sla_reports_service.summary(db, start_at, end_at)
        return report, "report:ticket_sla", None
    raise KeyError(advisor_key)


@router.post(
    "/insight/{advisor_key}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def reports_generate_insight(
    request: Request,
    advisor_key: str,
    date_from: str | None = Form(default=None),
    date_to: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Generate an AI insight for the given report advisor, on demand.

    Renders the insight partial. When AI generation is disabled or the advisor
    is off, renders a graceful message — never a 500."""
    from app.services.ai.engine import AIEngineError, intelligence_engine
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    # get_current_user returns a dict; be tolerant of an object too, and never
    # let a non-UUID actor id 500 the route — an insight with no recorded
    # actor is fine, a crash is not.
    _raw_actor = (
        current_user.get("id")
        if isinstance(current_user, dict)
        else getattr(current_user, "id", None)
    )
    actor_id: str | None = None
    if _raw_actor:
        try:
            actor_id = str(UUID(str(_raw_actor)))
        except (ValueError, TypeError):
            actor_id = None

    try:
        report, entity_type, entity_id = _fetch_report_for_advisor(
            db, advisor_key, date_from, date_to
        )
    except KeyError:
        return templates.TemplateResponse(
            request,
            "admin/reports/_insight.html",
            {"error": "No advisor for this report."},
            status_code=404,
        )

    try:
        insight = intelligence_engine.advise(
            db,
            advisor_key=advisor_key,
            report=report,
            entity_type=entity_type,
            entity_id=entity_id,
            trigger="manual",
            triggered_by_system_user_id=actor_id,
        )
    except AIEngineError as exc:
        return templates.TemplateResponse(
            request,
            "admin/reports/_insight.html",
            {"error": str(exc), "disabled": True},
        )
    return templates.TemplateResponse(
        request,
        "admin/reports/_insight.html",
        {"insight": insight},
    )
