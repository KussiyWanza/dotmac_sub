"""Service helpers for web/admin report routes."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import PaymentStatus
from app.models.subscriber import AccountStatus, Subscriber, SubscriberCategory
from app.schemas.status_presentation import StatusTone
from app.services import billing as billing_service
from app.services import crm_reporting as crm_reporting_service
from app.services import subscriber as subscriber_service
from app.services import subscriber_growth
from app.services import usage_summary as usage_summary_service
from app.services.ui_contracts import Kpi, StateValue

if TYPE_CHECKING:
    from app.models.provisioning import InstallAppointment
    from app.services.provisioning_managers import TechnicianReportRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecentSubscriberReportRow:
    """Immutable presentation projection for the recent-signups panel."""

    name: str
    created_at: datetime | None
    derived_status: AccountStatus


class NetworkPoolReportRow(TypedDict):
    name: str
    cidr: str
    used_count: int
    total_count: int


class NetworkReportData(TypedDict):
    olts: list[crm_reporting_service.NetworkOltFacts]
    total_olts: int
    active_olts: int
    total_onts: int
    connected_onts: int
    recent_ont_activity: list[crm_reporting_service.NetworkOntFacts]
    pool_data: list[NetworkPoolReportRow]
    used_ips: int
    total_ips: int
    ip_pool_usage: float
    active_vlans: int
    pon_capacity: int
    pon_utilization: float
    fiber_status: dict[str, int]
    total_fiber_strands: int
    available_fiber_strands: int
    total_fdh: int
    splitter_capacity: int


class CustomerGrowthSeries(TypedDict):
    labels: list[str]
    total: list[int]
    new: list[int]


class ChurnSeries(TypedDict):
    labels: list[str]
    rate: list[float]
    count: list[int]


class RegionalSubscriberReportRow(TypedDict):
    region: str
    subscribers: int
    tickets: int


class SubscriberReportData(TypedDict):
    subscriber_kpis: dict[str, Kpi]
    total_subscribers: int
    subscriber_growth: float | None
    new_this_month: int
    active_subscribers: int
    suspended_subscribers: int
    active_rate: float
    status_breakdown: dict[str, int]
    recent_subscribers: list[RecentSubscriberReportRow]
    customers: list[Subscriber]
    page: int
    per_page: int
    has_previous: bool
    has_next: bool
    date_from: str
    date_to: str
    usage_date_from: str
    usage_date_to: str
    total_usage_gb: float
    status_filter: str
    status_options: list[str]
    growth_data: CustomerGrowthSeries
    plan_distribution: dict[str, int]
    regional_breakdown: list[RegionalSubscriberReportRow]


class ChurnReportData(TypedDict):
    churn_kpis: dict[str, Kpi]
    churn_rate: float
    retention_rate: float
    cancelled_count: int
    at_risk_count: int
    churn_reasons: dict[str, int]
    churn_data: ChurnSeries
    recent_cancellations: list[Subscriber]


class TechnicianReportData(TypedDict):
    total_technicians: int
    jobs_completed: int
    avg_completion_hours: float
    appointment_completion_rate: float
    technician_stats: list[TechnicianReportRow]
    job_type_breakdown: dict[str, int]
    recent_completions: list[InstallAppointment]
    date_from: str
    date_to: str


def _customers_report_cohort_url(
    *,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """Drill-down to the customer report narrowed to exactly the cohort a KPI
    counts. Mirrors the ledger idiom: a status tile sets ``status`` while the
    surrounding date filters travel with the link, so a headline and the list
    it links to can never diverge (KPI-parity)."""
    params = {"status": status, "date_from": date_from, "date_to": date_to}
    query = urlencode({key: value for key, value in params.items() if value})
    return "/admin/reports/customers" + (f"?{query}" if query else "")


def _ensure_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def get_network_report_data(db: Session, hours: int | None = None) -> NetworkReportData:
    facts = crm_reporting_service.network_infrastructure_facts(db, hours=hours)
    pool_data: list[NetworkPoolReportRow] = [
        {
            "name": pool.name,
            "cidr": pool.cidr,
            "used_count": pool.used_count,
            "total_count": pool.total_count,
        }
        for pool in facts.pools
    ]
    fiber_status = dict(facts.fiber_status)
    total_fiber_strands = sum(fiber_status.values())
    available_fiber_strands = fiber_status.get("available", 0)

    return {
        "olts": list(facts.olts),
        "total_olts": facts.total_olts,
        "active_olts": facts.active_olts,
        "total_onts": facts.total_onts,
        "connected_onts": facts.connected_onts,
        "recent_ont_activity": list(facts.recent_ont_activity),
        "pool_data": pool_data,
        "used_ips": facts.used_ips,
        "total_ips": facts.total_ips,
        "ip_pool_usage": (
            facts.used_ips / facts.total_ips * 100 if facts.total_ips > 0 else 0
        ),
        "active_vlans": facts.active_vlans,
        "pon_capacity": facts.pon_capacity,
        "pon_utilization": (
            facts.total_onts / facts.pon_capacity * 100 if facts.pon_capacity else 0
        ),
        "fiber_status": fiber_status,
        "total_fiber_strands": total_fiber_strands,
        "available_fiber_strands": available_fiber_strands,
        "total_fdh": facts.total_fdh,
        "splitter_capacity": facts.splitter_capacity,
    }


def build_network_export_csv(data: NetworkReportData, hours: int | None = None) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["metric", "value"])
    writer.writerow(["total_olts", data["total_olts"]])
    writer.writerow(["active_olts", data["active_olts"]])
    writer.writerow(["total_onts", data["total_onts"]])
    writer.writerow(["connected_onts", data["connected_onts"]])
    writer.writerow(["used_ips", data["used_ips"]])
    writer.writerow(["total_ips", data["total_ips"]])
    writer.writerow(["ip_pool_usage_percent", f"{data['ip_pool_usage']:.2f}"])
    writer.writerow(["active_vlans", data["active_vlans"]])
    writer.writerow(["pon_capacity", data["pon_capacity"]])
    writer.writerow(["pon_utilization_percent", f"{data['pon_utilization']:.2f}"])
    writer.writerow(["total_fiber_strands", data["total_fiber_strands"]])
    writer.writerow(["available_fiber_strands", data["available_fiber_strands"]])
    writer.writerow(["active_fdh", data["total_fdh"]])
    writer.writerow(["splitter_output_capacity", data["splitter_capacity"]])
    writer.writerow(["report_window_hours", hours or ""])
    writer.writerow([])
    writer.writerow(["pool_name", "cidr", "used_count", "total_count", "usage_percent"])
    for pool in data["pool_data"]:
        usage = (
            (pool["used_count"] / pool["total_count"] * 100)
            if pool["total_count"]
            else 0
        )
        writer.writerow(
            [
                pool["name"],
                pool["cidr"],
                pool["used_count"],
                pool["total_count"],
                f"{usage:.2f}",
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def _derive_subscriber_status(subscriber: Subscriber) -> AccountStatus:
    if subscriber.status is not None:
        return subscriber.status
    return AccountStatus.active if subscriber.is_active else AccountStatus.canceled


def _date_range_values(
    *, date_from: str | None = None, date_to: str | None = None
) -> tuple[datetime | None, datetime | None, str, str]:
    from app.services.common import parse_date_filter

    start = parse_date_filter(date_from)
    parsed_to = parse_date_filter(date_to)
    end = parsed_to + timedelta(days=1) if parsed_to else None
    return start, end, date_from or "", date_to or ""


def _load_report_subscribers(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> list[Subscriber]:
    start, end, _, _ = _date_range_values(date_from=date_from, date_to=date_to)
    stmt = (
        select(Subscriber)
        .where(subscriber_service.visible_subscriber_clause())
        .order_by(Subscriber.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if start is not None:
        stmt = stmt.where(Subscriber.created_at >= start)
    if end is not None:
        stmt = stmt.where(Subscriber.created_at < end)
    status_filter = (status or "").strip().lower()
    if status_filter in {item.value for item in AccountStatus}:
        stmt = stmt.where(Subscriber.status == AccountStatus(status_filter))
    return list(db.scalars(stmt).all())


def _report_new_since_count(
    db: Session,
    *,
    since_iso: str,
    date_to: str | None,
    status: str | None,
) -> int:
    """Count subscribers whose drill-down cohort the "New This Month" tile links
    to: raw ``Subscriber.created_at`` within [since, date_to] under the page
    status filter — the exact same rule ``_load_report_subscribers`` applies.

    Counting on raw ``created_at`` (not the effective/source signup date) and on
    ``since`` (not the page ``date_from``) keeps the tile value equal to the list
    it links to, including for imported subscribers whose source signup month
    differs from their persisted ``created_at`` (KPI-parity)."""
    start, end, _, _ = _date_range_values(date_from=since_iso, date_to=date_to)
    stmt = select(func.count(Subscriber.id)).where(
        subscriber_service.visible_subscriber_clause()
    )
    if start is not None:
        stmt = stmt.where(Subscriber.created_at >= start)
    if end is not None:
        stmt = stmt.where(Subscriber.created_at < end)
    status_filter = (status or "").strip().lower()
    if status_filter in {item.value for item in AccountStatus}:
        stmt = stmt.where(Subscriber.status == AccountStatus(status_filter))
    return int(db.scalar(stmt) or 0)


def _report_status_cohort_counts(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[int, dict[str, int]]:
    """Grouped per-status counts for the customer-report cohort within the date
    window, INDEPENDENT of any page ``status`` filter.

    A KPI tile is a fixed overview number: it must count exactly the rows its
    ``cohort_url`` links to, not the page-filtered result set below it. The
    ``status`` drill-down (``_load_report_subscribers``) filters strictly on the
    persisted ``Subscriber.status``, so these counts use the same strict rule —
    the ``total`` tile counts every visible row (any status, including NULL),
    each per-status tile counts only its own persisted status. This keeps a
    headline value equal to the list it links to (KPI-parity)."""
    start, end, _, _ = _date_range_values(date_from=date_from, date_to=date_to)
    stmt = select(Subscriber.status, func.count(Subscriber.id)).where(
        subscriber_service.visible_subscriber_clause()
    )
    if start is not None:
        stmt = stmt.where(Subscriber.created_at >= start)
    if end is not None:
        stmt = stmt.where(Subscriber.created_at < end)
    stmt = stmt.group_by(Subscriber.status)

    total = 0
    by_status: dict[str, int] = {}
    for status_value, count in db.execute(stmt).all():
        count = int(count or 0)
        total += count
        if status_value is not None:
            key = getattr(status_value, "value", str(status_value))
            by_status[key] = by_status.get(key, 0) + count
    return total, by_status


def _customer_report_usage_window(
    *,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[datetime, datetime, str, str]:
    from app.services.web_reports_extended import _resolve_report_window

    return _resolve_report_window(
        date_from=date_from,
        date_to=date_to,
        days=days or 30,
    )


def _attach_period_usage_to_subscribers(
    db: Session,
    subscribers: list[Subscriber],
    *,
    start: datetime,
    end: datetime,
) -> float:
    # Report-only fields stuffed onto ORM instances for template consumption.
    for sub in subscribers:
        sub.period_usage_gb = 0.0  # type: ignore[attr-defined]
        sub.period_avg_mbps = 0.0  # type: ignore[attr-defined]
        sub.period_active_services = 0  # type: ignore[attr-defined]

    subscriber_ids = [sub.id for sub in subscribers if getattr(sub, "id", None)]
    if not subscriber_ids:
        return 0.0

    rows = usage_summary_service.period_usage_by_subscriber(
        db,
        subscriber_ids,
        start=start,
        end=end,
    )

    by_subscriber = {row.subscriber_id: row for row in rows}
    total_usage_gb = 0.0
    for sub in subscribers:
        row = by_subscriber.get(sub.id)
        if row is None:
            continue
        usage_gb = float(row.usage_bytes or 0) / (1024**3)
        sub.period_usage_gb = round(usage_gb, 2)  # type: ignore[attr-defined]
        sub.period_avg_mbps = round(  # type: ignore[attr-defined]
            float(row.avg_bps or 0) / 1_000_000, 2
        )
        sub.period_active_services = int(row.active_services or 0)  # type: ignore[attr-defined]
        total_usage_gb += usage_gb
    return round(total_usage_gb, 2)


def _invoice_amount_due(invoice: object) -> Decimal | int | float:
    for attr in ("balance_due", "amount_due", "total"):
        value = getattr(invoice, attr, None)
        if isinstance(value, (Decimal, int, float)):
            return value
    return 0


def _account_display_name(account: object | None) -> str:
    if not account:
        return ""
    organization = getattr(account, "organization", None)
    if organization is not None:
        return str(getattr(organization, "name", "") or "")
    name = f"{getattr(account, 'first_name', '')} {getattr(account, 'last_name', '')}".strip()
    if name:
        return name
    display_name = getattr(account, "display_name", None)
    if display_name:
        return str(display_name)
    return getattr(account, "account_number", "") or str(getattr(account, "id", ""))


def _payment_primary_invoice_id(payment) -> str | None:
    if not payment or not payment.allocations:
        return None
    allocation = min(
        payment.allocations,
        key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
    )
    return str(allocation.invoice_id)


def _percent_change(
    current: Decimal | int | float,
    previous: Decimal | int | float,
) -> float | None:
    if not previous:
        return None
    current_value = float(current)
    previous_value = float(previous)
    return round(((current_value - previous_value) / previous_value) * 100, 1)


def get_revenue_report_data(db: Session) -> dict:
    """Compose the revenue report from the billing reporting read owners.

    All figures (payments-basis revenue, outstanding receivables, total
    invoiced, recurring revenue, monthly series) are owned by
    app.services.billing.reporting; this function assembles and presents.
    """
    from app.services.billing import reporting as billing_reporting

    revenue = billing_reporting.get_payments_revenue_summary(db)
    outstanding = billing_reporting.get_outstanding_receivables(db)
    total_invoiced = billing_reporting.get_total_invoiced(db)
    try:
        recurring_revenue = billing_reporting.get_recurring_revenue(db)
    except Exception:
        logger.debug("Failed to compute recurring revenue", exc_info=True)
        recurring_revenue = Decimal("0")

    recent_payments = billing_service.payments.list(
        db=db,
        account_id=None,
        invoice_id=None,
        status=PaymentStatus.succeeded.value,
        is_active=None,
        order_by="paid_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    collection_rate = (
        (float(revenue["total"]) / float(total_invoiced) * 100) if total_invoiced else 0
    )
    revenue_growth = _percent_change(
        revenue["current_month"], revenue["previous_month"]
    )
    if revenue_growth is None and revenue["current_month"]:
        revenue_growth = 0.0
    return {
        "total_revenue": revenue["total"],
        "revenue_growth": revenue_growth,
        "recurring_revenue": recurring_revenue,
        "outstanding_amount": outstanding["amount"],
        "outstanding_count": outstanding["count"],
        "collection_rate": collection_rate,
        "recent_payments": recent_payments,
        "revenue_data": revenue["monthly"],
    }


def _subscriber_growth_percent(db: Session) -> float | None:
    """Month-over-month new-signup growth; counts owned by subscriber_growth."""
    current_new, previous_new = subscriber_growth.monthly_new_counts(db)
    return _percent_change(current_new, previous_new)


def build_revenue_export_csv(db: Session, days: int | None = None) -> str:
    payments = billing_service.payments.list(
        db=db,
        account_id=None,
        invoice_id=None,
        status=None,
        is_active=None,
        order_by="paid_at",
        order_dir="desc",
        limit=5000,
        offset=0,
    )
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        payments = [p for p in payments if p.paid_at and p.paid_at >= cutoff]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "paid_at",
            "account",
            "account_id",
            "invoice_id",
            "amount",
            "currency",
            "status",
            "payment_method",
            "provider",
        ]
    )
    for payment in payments:
        writer.writerow(
            [
                payment.paid_at.isoformat() if payment.paid_at else "",
                _account_display_name(payment.account),
                str(payment.account_id) if payment.account_id else "",
                _payment_primary_invoice_id(payment) or "",
                str(payment.amount or ""),
                payment.currency or "",
                payment.status.value if payment.status else "",
                payment.payment_method.name if payment.payment_method else "",
                payment.provider.name if payment.provider else "",
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def get_subscribers_report_data(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> SubscriberReportData:
    all_subscribers = _load_report_subscribers(
        db,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    usage_start, usage_end, usage_date_from, usage_date_to = (
        _customer_report_usage_window(date_from=date_from, date_to=date_to)
    )
    total_usage_gb = _attach_period_usage_to_subscribers(
        db,
        all_subscribers,
        start=usage_start,
        end=usage_end,
    )
    total_subscribers = len(all_subscribers)
    status_breakdown: dict[str, int] = {}
    active_count = 0
    suspended_count = 0
    for sub in all_subscribers:
        derived_status = _derive_subscriber_status(sub)
        status_name = derived_status.value if derived_status else "unknown"
        status_breakdown[status_name] = status_breakdown.get(status_name, 0) + 1
        if derived_status == AccountStatus.active:
            active_count += 1
        elif derived_status == AccountStatus.suspended:
            suspended_count += 1
    active_rate = (
        (active_count / total_subscribers * 100) if total_subscribers > 0 else 0
    )
    recent_subscribers = [
        RecentSubscriberReportRow(
            name=sub.name,
            created_at=sub.created_at,
            derived_status=_derive_subscriber_status(sub),
        )
        for sub in sorted(
            all_subscribers,
            key=lambda x: (
                subscriber_service.get_effective_created_at(x)
                or datetime.min.replace(tzinfo=UTC)
            ),
            reverse=True,
        )[:10]
    ]
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_iso = month_start.date().isoformat()
    new_this_month = _report_new_since_count(
        db, since_iso=month_start_iso, date_to=date_to, status=status
    )
    # Headline tiles as KPI contracts. Each status tile overrides only the
    # status dimension and preserves the active date window; "new this month"
    # overrides the start-date dimension and keeps the current status filter.
    #
    # KPI-parity: a tile value must count exactly the rows its cohort_url links
    # to, regardless of the page status filter. The status-narrowed
    # ``all_subscribers`` set drives the table and page metrics below, but the
    # overview tiles count their own cohort so "Total" never shrinks to the
    # active-only rows and "Suspended" never reads 0 while linking to a
    # non-empty suspended list. These grouped counts are computed independent of
    # the page status filter.
    cohort_total, cohort_by_status = _report_status_cohort_counts(
        db, date_from=date_from, date_to=date_to
    )
    cohort_active = cohort_by_status.get(AccountStatus.active.value, 0)
    cohort_suspended = cohort_by_status.get(AccountStatus.suspended.value, 0)
    subscriber_kpis = {
        "total": Kpi(
            label="Total Customers",
            value=StateValue.present(cohort_total),
            cohort_url=_customers_report_cohort_url(
                date_from=date_from, date_to=date_to
            ),
        ),
        "new_this_month": Kpi(
            label="New This Month",
            value=StateValue.present(new_this_month),
            cohort_url=_customers_report_cohort_url(
                status=status, date_from=month_start_iso, date_to=date_to
            ),
            tone=StatusTone.positive,
        ),
        "active": Kpi(
            label="Active",
            value=StateValue.present(cohort_active),
            cohort_url=_customers_report_cohort_url(
                status=AccountStatus.active.value,
                date_from=date_from,
                date_to=date_to,
            ),
            tone=StatusTone.info,
        ),
        "suspended": Kpi(
            label="Suspended",
            value=StateValue.present(cohort_suspended),
            cohort_url=_customers_report_cohort_url(
                status=AccountStatus.suspended.value,
                date_from=date_from,
                date_to=date_to,
            ),
            tone=StatusTone.warning,
        ),
    }
    subscriber_ids = [subscriber.id for subscriber in all_subscribers]
    segment_facts = crm_reporting_service.subscriber_segment_facts(
        db,
        subscriber_ids=tuple(subscriber_ids),
    )
    plan_distribution = dict(segment_facts.plan_distribution)
    region_counts: dict[str, int] = {}
    for subscriber in all_subscribers:
        region = subscriber.region or "Unspecified"
        region_counts[region] = region_counts.get(region, 0) + 1
    ticket_region_counts = dict(segment_facts.ticket_counts_by_region)
    regional_breakdown: list[RegionalSubscriberReportRow] = [
        {
            "region": region,
            "subscribers": count,
            "tickets": ticket_region_counts.get(region, 0),
        }
        for region, count in sorted(
            region_counts.items(), key=lambda item: item[1], reverse=True
        )
    ]
    return {
        "subscriber_kpis": subscriber_kpis,
        "total_subscribers": total_subscribers,
        "subscriber_growth": _subscriber_growth_percent(db),
        "new_this_month": new_this_month,
        "active_subscribers": active_count,
        "suspended_subscribers": suspended_count,
        "active_rate": active_rate,
        "status_breakdown": status_breakdown,
        "recent_subscribers": recent_subscribers,
        "customers": all_subscribers[(page - 1) * per_page : page * per_page],
        "page": page,
        "per_page": per_page,
        "has_previous": page > 1,
        "has_next": page * per_page < total_subscribers,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "usage_date_from": usage_date_from,
        "usage_date_to": usage_date_to,
        "total_usage_gb": total_usage_gb,
        "status_filter": status or "",
        "status_options": [item.value for item in AccountStatus],
        "growth_data": cast(
            CustomerGrowthSeries,
            subscriber_growth.monthly_customer_growth_series(db),
        ),
        "plan_distribution": plan_distribution,
        "regional_breakdown": regional_breakdown,
    }


def build_subscribers_export_csv(
    db: Session,
    days: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> str:
    all_subscribers = _load_report_subscribers(
        db,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        all_subscribers = [
            sub
            for sub in all_subscribers
            if (created_at := _ensure_aware_datetime(sub.created_at)) is not None
            and created_at >= cutoff
        ]
    usage_start, usage_end, _, _ = _customer_report_usage_window(
        days=days,
        date_from=date_from,
        date_to=date_to,
    )
    _attach_period_usage_to_subscribers(
        db,
        all_subscribers,
        start=usage_start,
        end=usage_end,
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "subscriber_id",
            "name",
            "type",
            "status",
            "created_at",
            "period_usage_gb",
            "period_avg_mbps",
            "period_active_services",
        ]
    )
    for sub in all_subscribers:
        derived_status = _derive_subscriber_status(sub)
        name = (
            sub.company_name
            if sub.category == SubscriberCategory.business
            else f"{sub.first_name} {sub.last_name}".strip()
            or sub.display_name
            or "Subscriber"
        )
        subscriber_type = (
            "organization" if sub.category == SubscriberCategory.business else "person"
        )
        writer.writerow(
            [
                str(sub.id),
                name,
                subscriber_type or "",
                derived_status.value if derived_status else "",
                (
                    created_at.isoformat()
                    if (created_at := subscriber_service.get_effective_created_at(sub))
                    is not None
                    else ""
                ),
                getattr(sub, "period_usage_gb", 0),
                getattr(sub, "period_avg_mbps", 0),
                getattr(sub, "period_active_services", 0),
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def get_churn_report_data(db: Session) -> ChurnReportData:
    """Compose the churn report from the subscriber growth/churn read owner.

    Counts, the monthly churn series, and the recent-cancellation list are
    owned by app.services.subscriber_growth; this function assembles and
    presents.
    """
    summary = subscriber_growth.churn_summary(db)
    total_subscribers = summary["total"]
    at_risk_count = summary["at_risk_count"]
    # KPI-parity: the Cancellations tile drills into the strict
    # ``status=canceled`` customer cohort, and that list (_load_report_subscribers)
    # filters strictly on ``Subscriber.status``.
    # churn_summary()'s ``cancelled_count`` uses the wider derived-cancelled rule
    # (``status == canceled`` OR ``status IS NULL AND not is_active``), so it can
    # exceed the drill-down. Count with the same strict rule the linked list
    # uses so the headline value equals the list it links to.
    cancelled_count = int(
        db.scalar(
            select(func.count(Subscriber.id)).where(
                subscriber_service.visible_subscriber_clause(),
                Subscriber.status == AccountStatus.canceled,
            )
        )
        or 0
    )
    active_count = int(
        db.scalar(
            select(func.count(Subscriber.id)).where(
                subscriber_service.visible_subscriber_clause(),
                Subscriber.status == AccountStatus.active,
            )
        )
        or 0
    )
    churn_rate = (
        (cancelled_count / total_subscribers * 100) if total_subscribers > 0 else 0
    )
    # Retention is the strict active share, not the complement of cancellations
    # (which also includes suspended and other non-cancelled states).
    retention_rate = (
        (active_count / total_subscribers * 100) if total_subscribers > 0 else 0
    )
    # Tone is owned by the report, not re-derived in the template: churn worsens
    # as it rises, so its semantic signal flips at the same thresholds the
    # dashboard reads. Count tiles drill into exact customer cohorts; rate tiles
    # return to the overview showing the identical aggregate (KPI-parity).
    churn_tone = (
        StatusTone.negative
        if churn_rate > 10
        else StatusTone.warning
        if churn_rate > 5
        else StatusTone.positive
    )
    churn_kpis = {
        "churn_rate": Kpi(
            label="Churn Rate",
            value=StateValue.present(f"{churn_rate:.1f}%"),
            # A rate is an aggregate over the full population, not the
            # cancelled numerator alone. Drill back to the overview that shows
            # the identical aggregate rather than a mismatched entity list.
            cohort_url="/admin/reports/churn#churn-summary",
            tone=churn_tone,
        ),
        "cancelled": Kpi(
            label="Cancellations",
            value=StateValue.present(cancelled_count),
            cohort_url=_customers_report_cohort_url(
                status=AccountStatus.canceled.value
            ),
            tone=StatusTone.negative,
        ),
        "at_risk": Kpi(
            label="At Risk",
            value=StateValue.present(at_risk_count),
            cohort_url=_customers_report_cohort_url(
                status=AccountStatus.suspended.value
            ),
            tone=StatusTone.warning,
        ),
        "retention_rate": Kpi(
            label="Retention Rate",
            value=StateValue.present(f"{retention_rate:.1f}%"),
            cohort_url="/admin/reports/churn#churn-summary",
            tone=StatusTone.positive,
        ),
    }
    churn_reasons = dict(crm_reporting_service.subscription_churn_reason_counts(db))
    return {
        "churn_kpis": churn_kpis,
        "churn_rate": churn_rate,
        "retention_rate": retention_rate,
        "cancelled_count": cancelled_count,
        "at_risk_count": at_risk_count,
        "churn_reasons": churn_reasons,
        "churn_data": cast(
            ChurnSeries,
            subscriber_growth.monthly_churn_series(db),
        ),
        "recent_cancellations": subscriber_growth.recent_cancellations(db, limit=10),
    }


def build_churn_export_csv(db: Session, days: int | None = None) -> str:
    # Export the complete visible cohort.  The old CRUD-list path silently
    # capped this regulatory/operational artifact at 5,000 subscribers.
    all_subscribers = _load_report_subscribers(db)
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        all_subscribers = [
            sub
            for sub in all_subscribers
            if (updated_at := subscriber_service.get_effective_updated_at(sub))
            is not None
            and updated_at >= cutoff
        ]
    total_subscribers = len(all_subscribers)
    derived_status_by_id = {
        sub.id: _derive_subscriber_status(sub) for sub in all_subscribers
    }
    cancelled_subscribers = [
        sub
        for sub in all_subscribers
        if derived_status_by_id[sub.id] == AccountStatus.canceled
    ]
    at_risk_subscribers = [
        sub
        for sub in all_subscribers
        if derived_status_by_id[sub.id] == AccountStatus.suspended
    ]
    active_subscribers = [
        sub
        for sub in all_subscribers
        if derived_status_by_id[sub.id] == AccountStatus.active
    ]
    churn_rate = (
        (len(cancelled_subscribers) / total_subscribers * 100)
        if total_subscribers > 0
        else 0
    )
    retention_rate = (
        (len(active_subscribers) / total_subscribers * 100)
        if total_subscribers > 0
        else 0
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["metric", "value"])
    writer.writerow(["total_subscribers", total_subscribers])
    writer.writerow(["cancelled_count", len(cancelled_subscribers)])
    writer.writerow(["at_risk_count", len(at_risk_subscribers)])
    writer.writerow(["churn_rate_percent", f"{churn_rate:.2f}"])
    writer.writerow(["retention_rate_percent", f"{retention_rate:.2f}"])
    writer.writerow(["report_window_days", days or ""])
    writer.writerow([])
    writer.writerow(["subscriber_id", "name", "status", "updated_at"])
    for sub in cancelled_subscribers:
        name = (
            sub.company_name
            if sub.category == SubscriberCategory.business
            else f"{sub.first_name} {sub.last_name}".strip()
            or sub.display_name
            or "Subscriber"
        )
        writer.writerow(
            [
                str(sub.id),
                name,
                derived_status_by_id[sub.id].value,
                (
                    updated_at.isoformat()
                    if (updated_at := subscriber_service.get_effective_updated_at(sub))
                    is not None
                    else ""
                ),
            ]
        )
    content = output.getvalue()
    output.close()
    return content


def get_technician_report_data(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> TechnicianReportData:
    """Compose the technician report from the provisioning read owner.

    The aggregated figures are owned by
    app.services.provisioning_managers.technician_report_stats; this function
    assembles them with the recent-completion listing and owns presentation
    (the top-10 slice) only.
    """
    from app.services import provisioning_managers

    start_at, end_at, _, _ = _date_range_values(date_from=date_from, date_to=date_to)
    stats = provisioning_managers.technician_report_stats(
        db, start_at=start_at, end_at=end_at
    )
    recent_completions = provisioning_managers.recent_completed_appointments(
        db,
        start_at=start_at,
        end_at=end_at,
        limit=10,
    )

    return {
        "total_technicians": stats["total_technicians"],
        "jobs_completed": stats["jobs_completed"],
        "avg_completion_hours": stats["avg_completion_hours"],
        "appointment_completion_rate": stats["appointment_completion_rate"],
        "technician_stats": stats["technician_stats"][:10],
        "job_type_breakdown": stats["job_type_breakdown"],
        "recent_completions": recent_completions,
        "date_from": date_from or "",
        "date_to": date_to or "",
    }


def build_technician_export_csv(
    db: Session,
    days: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    if days and not date_from:
        date_from = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
    report_data = get_technician_report_data(db, date_from=date_from, date_to=date_to)
    technician_stats = list(report_data.get("technician_stats") or [])
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "technician",
            "total_jobs",
            "completed_jobs",
            "avg_completion_hours",
            "completion_rate_percent",
            "jobs_completed_total",
            "report_window_days",
        ]
    )
    jobs_completed = report_data.get("jobs_completed", 0)
    for tech in technician_stats:
        writer.writerow(
            [
                tech["name"],
                tech["total_jobs"],
                tech["completed_jobs"],
                tech["avg_hours"],
                tech["completion_rate"],
                jobs_completed,
                days or "",
            ]
        )
    content = output.getvalue()
    output.close()
    return content
