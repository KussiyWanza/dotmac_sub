"""Typed read projections for CRM capabilities hosted by Self-Care.

The reports in this module never own the underlying customer, billing, inbox,
network, support, or project facts.  They compose those authoritative records
into read-only operator projections.  CRM retention engagement records are
deliberately outside this boundary.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.billing import Invoice, Payment, PaymentStatus
from app.models.catalog import (
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import (
    FdhCabinet,
    FiberStrand,
    IpBlock,
    IpPool,
    OLTDevice,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    Splitter,
    Vlan,
)
from app.models.network_monitoring import CustomerOutageInterval
from app.models.project import Project, ProjectTask
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.team_inbox import InboxConversation, InboxConversationQueueEntry
from app.models.work_order import WorkOrder
from app.services import (
    crm_api,
    ip_pool_utilization_snapshot,
    team_inbox_metrics,
    ticket_sla_reports,
)


class CrmReportSlug(StrEnum):
    ONLINE_ACTIVITY = "online-activity"
    BILLING_RISK = "billing-risk"
    SUBSCRIBER_REVENUE = "subscriber-revenue"
    POSTPAID_CUSTOMERS = "postpaid-customers"
    CRM_PERFORMANCE = "crm-performance"
    AGENT_PERFORMANCE = "agent-performance"
    MY_PERFORMANCE = "my-performance"
    OPERATIONS_SLA = "operations-sla"
    QUEUE_CLASSIFICATION = "queue-classification"
    SUBSCRIBER_LIFECYCLE = "subscriber-lifecycle"
    SERVICE_QUALITY = "service-quality"
    REVENUE_SERVICE = "revenue-service"
    PROJECT_TASK_PERFORMANCE = "project-task-performance"


@dataclass(frozen=True, slots=True)
class CrmReportDefinition:
    slug: CrmReportSlug
    title: str
    description: str
    permission: str
    supports_date_filter: bool = False


@dataclass(frozen=True, slots=True)
class CrmReportQuery:
    date_from: date | None = None
    date_to: date | None = None
    page: int = 1
    per_page: int | None = 50
    person_id: UUID | None = None

    @property
    def start_at(self) -> datetime | None:
        return (
            datetime.combine(self.date_from, time.min, UTC) if self.date_from else None
        )

    @property
    def end_at(self) -> datetime | None:
        if self.date_to is None:
            return None
        return datetime.combine(self.date_to + timedelta(days=1), time.min, UTC)


@dataclass(frozen=True, slots=True)
class CrmReportMetric:
    label: str
    value: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CrmReportPage:
    definition: CrmReportDefinition
    metrics: tuple[CrmReportMetric, ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    total: int
    page: int
    per_page: int
    note: str | None = None

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.per_page < self.total


@dataclass(frozen=True, slots=True)
class NetworkPoolFacts:
    name: str
    cidr: str
    used_count: int
    total_count: int


@dataclass(frozen=True, slots=True)
class NetworkOltFacts:
    name: str
    serial_number: str | None
    mgmt_ip: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class NetworkOntFacts:
    model: str | None
    serial_number: str
    olt_name: str | None
    is_online: bool


@dataclass(frozen=True, slots=True)
class NetworkInfrastructureFacts:
    olts: tuple[NetworkOltFacts, ...]
    total_olts: int
    active_olts: int
    total_onts: int
    connected_onts: int
    recent_ont_activity: tuple[NetworkOntFacts, ...]
    pools: tuple[NetworkPoolFacts, ...]
    used_ips: int
    total_ips: int
    active_vlans: int
    pon_capacity: int
    fiber_status: tuple[tuple[str, int], ...]
    total_fdh: int
    splitter_capacity: int


@dataclass(frozen=True, slots=True)
class SubscriberSegmentFacts:
    plan_distribution: tuple[tuple[str, int], ...]
    ticket_counts_by_region: tuple[tuple[str, int], ...]


REPORT_DEFINITIONS: dict[CrmReportSlug, CrmReportDefinition] = {
    CrmReportSlug.ONLINE_ACTIVITY: CrmReportDefinition(
        CrmReportSlug.ONLINE_ACTIVITY,
        "Online Activity",
        "Customers with fresh RADIUS activity",
        "customer:read",
    ),
    CrmReportSlug.BILLING_RISK: CrmReportDefinition(
        CrmReportSlug.BILLING_RISK,
        "Subscriber Billing Risk",
        "Authoritative balances, blocks, billing dates, and payment recency",
        "reports:billing:read",
    ),
    CrmReportSlug.SUBSCRIBER_REVENUE: CrmReportDefinition(
        CrmReportSlug.SUBSCRIBER_REVENUE,
        "Subscriber Revenue & Pipeline",
        "Invoiced, collected, and outstanding value by subscriber",
        "reports:billing:read",
        True,
    ),
    CrmReportSlug.POSTPAID_CUSTOMERS: CrmReportDefinition(
        CrmReportSlug.POSTPAID_CUSTOMERS,
        "Postpaid Customers",
        "Postpaid accounts and their current billing position",
        "reports:billing:read",
    ),
    CrmReportSlug.CRM_PERFORMANCE: CrmReportDefinition(
        CrmReportSlug.CRM_PERFORMANCE,
        "CRM Performance",
        "Inbox performance by service team",
        "reports:support:read",
    ),
    CrmReportSlug.AGENT_PERFORMANCE: CrmReportDefinition(
        CrmReportSlug.AGENT_PERFORMANCE,
        "Agent Performance",
        "Inbox handling and response performance by agent",
        "reports:support:read",
    ),
    CrmReportSlug.MY_PERFORMANCE: CrmReportDefinition(
        CrmReportSlug.MY_PERFORMANCE,
        "My Performance",
        "The signed-in agent's own inbox performance",
        "reports:support:read",
    ),
    CrmReportSlug.OPERATIONS_SLA: CrmReportDefinition(
        CrmReportSlug.OPERATIONS_SLA,
        "Operations SLA Violations",
        "Overdue tickets, projects, and project tasks",
        "reports:support:read",
        True,
    ),
    CrmReportSlug.QUEUE_CLASSIFICATION: CrmReportDefinition(
        CrmReportSlug.QUEUE_CLASSIFICATION,
        "Queue & Issue Classification",
        "Queue settlement times and recorded AI/tag classifications",
        "reports:support:read",
        True,
    ),
    CrmReportSlug.SUBSCRIBER_LIFECYCLE: CrmReportDefinition(
        CrmReportSlug.SUBSCRIBER_LIFECYCLE,
        "Subscriber Lifecycle",
        "Subscriber and service lifecycle state from native records",
        "customer:read",
        True,
    ),
    CrmReportSlug.SERVICE_QUALITY: CrmReportDefinition(
        CrmReportSlug.SERVICE_QUALITY,
        "Subscriber Service Quality",
        "Support, field-work, and outage observations by subscriber",
        "reports:support:read",
        True,
    ),
    CrmReportSlug.REVENUE_SERVICE: CrmReportDefinition(
        CrmReportSlug.REVENUE_SERVICE,
        "Revenue & Service",
        "Revenue alongside authoritative customer outage intervals",
        "reports:billing:read",
        True,
    ),
    CrmReportSlug.PROJECT_TASK_PERFORMANCE: CrmReportDefinition(
        CrmReportSlug.PROJECT_TASK_PERFORMANCE,
        "Project & Task Performance",
        "Assigned, completed, overdue, and blocked project work",
        "reports:support:read",
        True,
    ),
}


def _money(value: object) -> str:
    return f"NGN {Decimal(str(value or 0)):,.2f}"


def _text(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
            if value.tzinfo
            else value.strftime("%Y-%m-%d %H:%M")
        )
    if isinstance(value, date):
        return value.isoformat()
    raw = getattr(value, "value", value)
    return str(raw).replace("_", " ").title() if raw != "" else "—"


def _subscriber_name(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return "Unknown subscriber"
    return (
        subscriber.name
        or subscriber.display_name
        or subscriber.account_number
        or str(subscriber.id)
    )


def _within(value: datetime | None, query: CrmReportQuery) -> bool:
    if value is None:
        return query.start_at is None and query.end_at is None
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return not (
        (query.start_at and aware < query.start_at)
        or (query.end_at and aware >= query.end_at)
    )


def _paginate(
    rows: Sequence[tuple[str, ...]], query: CrmReportQuery
) -> tuple[tuple[tuple[str, ...], ...], int]:
    total = len(rows)
    if query.per_page is None:
        return tuple(rows), total
    start = (query.page - 1) * query.per_page
    return tuple(rows[start : start + query.per_page]), total


def _page(
    definition: CrmReportDefinition,
    query: CrmReportQuery,
    metrics: tuple[CrmReportMetric, ...],
    columns: tuple[str, ...],
    rows: Sequence[tuple[str, ...]],
    note: str | None = None,
) -> CrmReportPage:
    paged, total = _paginate(rows, query)
    per_page = query.per_page or max(total, 1)
    return CrmReportPage(
        definition, metrics, columns, paged, total, query.page, per_page, note
    )


def network_infrastructure_facts(
    db: Session, *, hours: int | None = None
) -> NetworkInfrastructureFacts:
    """Read typed network facts for the infrastructure report projection."""
    olts = tuple(
        NetworkOltFacts(
            name=row.name,
            serial_number=row.serial_number,
            mgmt_ip=row.mgmt_ip,
            is_active=bool(row.is_active),
        )
        for row in db.execute(
            select(
                OLTDevice.name,
                OLTDevice.serial_number,
                OLTDevice.mgmt_ip,
                OLTDevice.is_active,
            ).order_by(OLTDevice.created_at.desc())
        ).all()
    )
    total_olts = int(db.scalar(select(func.count(OLTDevice.id))) or 0)
    active_olts = int(
        db.scalar(select(func.count(OLTDevice.id)).where(OLTDevice.is_active.is_(True)))
        or 0
    )

    ont_filters: list[ColumnElement[bool]] = []
    if hours:
        ont_filters.append(
            OntUnit.updated_at >= datetime.now(UTC) - timedelta(hours=hours)
        )
    total_onts = int(db.scalar(select(func.count(OntUnit.id)).where(*ont_filters)) or 0)
    connected_onts = int(
        db.scalar(
            select(func.count(OntUnit.id)).where(
                *ont_filters, OntUnit.olt_status == OnuOnlineStatus.online
            )
        )
        or 0
    )
    recent_ont_activity = tuple(
        NetworkOntFacts(
            model=row.model,
            serial_number=row.serial_number,
            olt_name=row.olt_name,
            is_online=row.olt_status == OnuOnlineStatus.online,
        )
        for row in db.execute(
            select(
                OntUnit.model,
                OntUnit.serial_number,
                OLTDevice.name.label("olt_name"),
                OntUnit.olt_status,
            )
            .outerjoin(OLTDevice, OLTDevice.id == OntUnit.olt_device_id)
            .where(*ont_filters)
            .order_by(OntUnit.updated_at.desc())
            .limit(10)
        ).all()
    )

    pools: list[NetworkPoolFacts] = []
    used_ips = 0
    total_ips = 0
    for pool in db.scalars(select(IpPool).order_by(IpPool.created_at.desc())).all():
        pool_used, pool_total = ip_pool_utilization_snapshot.live_pool_counts(db, pool)
        if pool_total == 0:
            block_count = int(
                db.scalar(
                    select(func.count(IpBlock.id)).where(IpBlock.pool_id == pool.id)
                )
                or 0
            )
            pool_total = block_count * 256
        pool_total = pool_total if pool_total > 0 else 256
        pools.append(
            NetworkPoolFacts(
                name=pool.name,
                cidr=pool.cidr,
                used_count=pool_used,
                total_count=pool_total,
            )
        )
        used_ips += pool_used
        total_ips += pool_total

    fiber_status = tuple(
        (
            status.value if status else "unknown",
            int(count or 0),
        )
        for status, count in db.execute(
            select(FiberStrand.status, func.count(FiberStrand.id))
            .where(FiberStrand.is_active.is_(True))
            .group_by(FiberStrand.status)
        ).all()
    )
    return NetworkInfrastructureFacts(
        olts=olts,
        total_olts=total_olts,
        active_olts=active_olts,
        total_onts=total_onts,
        connected_onts=connected_onts,
        recent_ont_activity=recent_ont_activity,
        pools=tuple(pools),
        used_ips=used_ips,
        total_ips=total_ips,
        active_vlans=int(
            db.scalar(select(func.count(Vlan.id)).where(Vlan.is_active.is_(True))) or 0
        ),
        pon_capacity=int(
            db.scalar(
                select(func.coalesce(func.sum(PonPort.max_ont_capacity), 0)).where(
                    PonPort.is_active.is_(True)
                )
            )
            or 0
        ),
        fiber_status=fiber_status,
        total_fdh=int(
            db.scalar(
                select(func.count(FdhCabinet.id)).where(FdhCabinet.is_active.is_(True))
            )
            or 0
        ),
        splitter_capacity=int(
            db.scalar(
                select(func.coalesce(func.sum(Splitter.output_ports), 0)).where(
                    Splitter.is_active.is_(True)
                )
            )
            or 0
        ),
    )


def subscriber_segment_facts(
    db: Session, *, subscriber_ids: tuple[UUID, ...]
) -> SubscriberSegmentFacts:
    """Read plan and support-region facts for a subscriber report cohort."""
    plan_distribution: tuple[tuple[str, int], ...] = ()
    if subscriber_ids:
        plan_distribution = tuple(
            (plan_name or "Unspecified", int(count or 0))
            for plan_name, count in db.execute(
                select(
                    CatalogOffer.name,
                    func.count(func.distinct(Subscription.subscriber_id)),
                )
                .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
                .where(
                    Subscription.subscriber_id.in_(subscriber_ids),
                    Subscription.status.in_(
                        (SubscriptionStatus.active, SubscriptionStatus.pending)
                    ),
                )
                .group_by(CatalogOffer.name)
                .order_by(func.count(func.distinct(Subscription.subscriber_id)).desc())
            ).all()
        )
    ticket_counts_by_region = tuple(
        (region or "Unspecified", int(count or 0))
        for region, count in db.execute(
            select(Ticket.region, func.count(Ticket.id))
            .where(Ticket.is_active.is_(True))
            .group_by(Ticket.region)
        ).all()
    )
    return SubscriberSegmentFacts(
        plan_distribution=plan_distribution,
        ticket_counts_by_region=ticket_counts_by_region,
    )


def subscription_churn_reason_counts(db: Session) -> tuple[tuple[str, int], ...]:
    """Read authoritative service-cancellation reasons for the churn report."""
    return tuple(
        (reason or "Reason not captured", int(count or 0))
        for reason, count in db.execute(
            select(Subscription.cancel_reason, func.count(Subscription.id))
            .where(
                (Subscription.status == SubscriptionStatus.canceled)
                | (Subscription.canceled_at.is_not(None))
            )
            .group_by(Subscription.cancel_reason)
            .order_by(func.count(Subscription.id).desc())
        ).all()
    )


def _all_crm_rows(
    db: Session,
    fetcher: Callable[..., tuple[list[dict[str, object]], int]],
) -> tuple[list[dict[str, object]], int]:
    """Read every page from an existing CRM API query without a silent cap."""
    rows: list[dict[str, object]] = []
    page = 1
    total = 0
    while True:
        batch, total = fetcher(db, page=page, per_page=2_000)
        rows.extend(batch)
        if not batch or len(rows) >= total:
            return rows, total
        page += 1


def _online_activity(db: Session, query: CrmReportQuery) -> CrmReportPage:
    if query.per_page is None:
        raw, total = _all_crm_rows(db, crm_api.online_subscribers)
    else:
        raw, total = crm_api.online_subscribers(
            db, page=query.page, per_page=query.per_page
        )
    rows = tuple(
        tuple(
            _text(item.get(key))
            for key in (
                "subscriber_number",
                "status",
                "last_seen",
            )
        )
        for item in raw
    )
    return CrmReportPage(
        REPORT_DEFINITIONS[CrmReportSlug.ONLINE_ACTIVITY],
        (CrmReportMetric("Online customers", str(total), "Fresh RADIUS sessions"),),
        (
            "Subscriber number",
            "Status",
            "Last activity",
        ),
        rows,
        total,
        query.page,
        query.per_page or max(total, 1),
    )


def _billing_risk(db: Session, query: CrmReportQuery) -> CrmReportPage:
    raw, _ = _all_crm_rows(db, crm_api.billing_risk_rows)
    at_risk = [
        item
        for item in raw
        if Decimal(str(item.get("balance") or 0)) > 0 or item.get("blocked_date")
    ]
    outstanding = sum(Decimal(str(item.get("balance") or 0)) for item in at_risk)
    blocked = sum(1 for item in at_risk if item.get("blocked_date"))
    rows = [
        (
            _text(item.get("name")),
            _text(item.get("status")),
            _money(item.get("balance")),
            _text(item.get("blocked_date")),
            _text(item.get("next_bill_date")),
            _text(item.get("last_payment_date")),
        )
        for item in at_risk
    ]
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.BILLING_RISK],
        query,
        (
            CrmReportMetric("Customers at billing risk", str(len(at_risk))),
            CrmReportMetric("Outstanding", _money(outstanding)),
            CrmReportMetric("Blocked", str(blocked)),
        ),
        ("Customer", "Status", "Outstanding", "Blocked", "Next bill", "Last payment"),
        rows,
        "This report presents authoritative billing facts; it does not copy CRM retention dispositions or outreach state.",
    )


def _financial_maps(
    db: Session,
    subscriber_ids: list[UUID],
    *,
    query: CrmReportQuery | None = None,
) -> tuple[dict[UUID, Decimal], dict[UUID, Decimal], dict[UUID, Decimal]]:
    if not subscriber_ids:
        return {}, {}, {}
    invoice_filters: list[ColumnElement[bool]] = [
        Invoice.account_id.in_(subscriber_ids),
        Invoice.is_active.is_(True),
    ]
    payment_filters: list[ColumnElement[bool]] = [
        Payment.account_id.in_(subscriber_ids),
        Payment.is_active.is_(True),
    ]
    if query and query.start_at:
        invoice_filters.append(Invoice.issued_at >= query.start_at)
        payment_filters.append(
            func.coalesce(Payment.paid_at, Payment.created_at) >= query.start_at
        )
    if query and query.end_at:
        invoice_filters.append(Invoice.issued_at < query.end_at)
        payment_filters.append(
            func.coalesce(Payment.paid_at, Payment.created_at) < query.end_at
        )
    invoiced = {
        row[0]: Decimal(str(row[1] or 0))
        for row in db.execute(
            select(Invoice.account_id, func.sum(Invoice.total))
            .where(*invoice_filters)
            .group_by(Invoice.account_id)
        ).all()
    }
    outstanding = {
        row[0]: Decimal(str(row[1] or 0))
        for row in db.execute(
            select(Invoice.account_id, func.sum(Invoice.balance_due))
            .where(*invoice_filters)
            .group_by(Invoice.account_id)
        ).all()
    }
    paid_statuses = (PaymentStatus.succeeded, PaymentStatus.partially_refunded)
    paid = {
        row[0]: Decimal(str(row[1] or 0))
        for row in db.execute(
            select(Payment.account_id, func.sum(Payment.amount))
            .where(
                *payment_filters,
                Payment.status.in_(paid_statuses),
            )
            .group_by(Payment.account_id)
        ).all()
    }
    return invoiced, paid, outstanding


def _subscriber_revenue(db: Session, query: CrmReportQuery) -> CrmReportPage:
    subscribers = list(
        db.scalars(select(Subscriber).order_by(Subscriber.created_at.desc())).all()
    )
    invoiced, paid, outstanding = _financial_maps(
        db, [item.id for item in subscribers], query=query
    )
    open_order_statuses = tuple(
        status
        for status in ServiceOrderStatus
        if status
        not in (
            ServiceOrderStatus.active,
            ServiceOrderStatus.canceled,
            ServiceOrderStatus.failed,
        )
    )
    open_orders = {
        subscriber_id: int(count or 0)
        for subscriber_id, count in db.execute(
            select(ServiceOrder.subscriber_id, func.count(ServiceOrder.id))
            .where(ServiceOrder.status.in_(open_order_statuses))
            .group_by(ServiceOrder.subscriber_id)
        ).all()
    }
    rows = [
        (
            _subscriber_name(item),
            _text(item.status),
            _money(invoiced.get(item.id)),
            _money(paid.get(item.id)),
            _money(outstanding.get(item.id)),
            _money(item.mrr_total),
            str(open_orders.get(item.id, 0)),
        )
        for item in subscribers
        if any(
            (
                invoiced.get(item.id),
                paid.get(item.id),
                outstanding.get(item.id),
                item.mrr_total,
                open_orders.get(item.id),
            )
        )
    ]
    rows.sort(
        key=lambda row: Decimal(row[4].replace("NGN ", "").replace(",", "")),
        reverse=True,
    )
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.SUBSCRIBER_REVENUE],
        query,
        (
            CrmReportMetric("Invoiced", _money(sum(invoiced.values(), Decimal(0)))),
            CrmReportMetric("Collected", _money(sum(paid.values(), Decimal(0)))),
            CrmReportMetric(
                "Outstanding", _money(sum(outstanding.values(), Decimal(0)))
            ),
            CrmReportMetric("Open service orders", str(sum(open_orders.values()))),
        ),
        (
            "Customer",
            "Status",
            "Invoiced",
            "Collected",
            "Outstanding",
            "MRR",
            "Open service orders",
        ),
        rows,
    )


def _postpaid(db: Session, query: CrmReportQuery) -> CrmReportPage:
    subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(Subscriber.billing_mode == BillingMode.postpaid)
            .order_by(Subscriber.created_at.desc())
        ).all()
    )
    invoiced, paid, outstanding = _financial_maps(db, [item.id for item in subscribers])
    rows = [
        (
            _subscriber_name(item),
            _text(item.status),
            _text(item.billing_day),
            _money(invoiced.get(item.id)),
            _money(paid.get(item.id)),
            _money(outstanding.get(item.id)),
        )
        for item in subscribers
    ]
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.POSTPAID_CUSTOMERS],
        query,
        (
            CrmReportMetric("Postpaid customers", str(len(subscribers))),
            CrmReportMetric(
                "Outstanding", _money(sum(outstanding.values(), Decimal(0)))
            ),
        ),
        ("Customer", "Status", "Billing day", "Invoiced", "Collected", "Outstanding"),
        rows,
    )


def _crm_performance(db: Session, query: CrmReportQuery) -> CrmReportPage:
    report = team_inbox_metrics.team_performance_report(db)
    rows = [
        (
            item.service_team_name,
            str(item.metrics.conversation_count),
            str(item.metrics.open_count),
            str(item.metrics.unassigned_open_count),
            str(item.metrics.responded_count),
            _text(item.metrics.average_first_response_seconds),
            _text(item.metrics.average_queue_wait_seconds),
        )
        for item in report
    ]
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.CRM_PERFORMANCE],
        query,
        (
            CrmReportMetric("Teams", str(len(report))),
            CrmReportMetric(
                "Conversations",
                str(sum(item.metrics.conversation_count for item in report)),
            ),
            CrmReportMetric(
                "Open", str(sum(item.metrics.open_count for item in report))
            ),
        ),
        (
            "Team",
            "Conversations",
            "Open",
            "Unassigned",
            "Responded",
            "Avg first response (s)",
            "Avg queue wait (s)",
        ),
        rows,
    )


def _agent_performance(
    db: Session, query: CrmReportQuery, *, personal: bool = False
) -> CrmReportPage:
    report = team_inbox_metrics.agent_performance_report(db)
    if personal:
        report = [
            item
            for item in report
            if query.person_id and item.person_id == str(query.person_id)
        ]
    rows = [
        (
            item.person_id,
            item.service_team_name,
            str(item.metrics.active_assignment_count),
            str(item.metrics.handled_conversation_count),
            _text(item.metrics.average_first_response_seconds),
            _text(item.metrics.average_queue_wait_seconds),
        )
        for item in report
    ]
    definition = REPORT_DEFINITIONS[
        CrmReportSlug.MY_PERFORMANCE if personal else CrmReportSlug.AGENT_PERFORMANCE
    ]
    note = "Metrics are restricted to the signed-in agent." if personal else None
    return _page(
        definition,
        query,
        (
            CrmReportMetric("Agents", str(len({item.person_id for item in report}))),
            CrmReportMetric(
                "Handled",
                str(sum(item.metrics.handled_conversation_count for item in report)),
            ),
        ),
        (
            "Agent",
            "Team",
            "Active assignments",
            "Handled",
            "Avg first response (s)",
            "Avg queue wait (s)",
        ),
        rows,
        note,
    )


def _operations_sla(db: Session, query: CrmReportQuery) -> CrmReportPage:
    now = datetime.now(UTC)
    rows: list[tuple[str, ...]] = [
        (
            "Ticket",
            str(record["ticket_reference"]),
            _text(record["sla_status"]),
            _text(record["due_at"]),
            _text(record["breached_at"]),
            f"{int(record['breach_minutes']) / 60:.1f}",
        )
        for record in ticket_sla_reports.violation_records(
            db,
            start_at=query.start_at,
            end_at=query.end_at,
            limit=10_000,
        )
    ]
    for kind, records in (
        (
            "Project",
            db.scalars(
                select(Project).where(
                    Project.due_at.is_not(None), Project.is_active.is_(True)
                )
            ).all(),
        ),
        (
            "Project task",
            db.scalars(
                select(ProjectTask).where(
                    ProjectTask.due_at.is_not(None), ProjectTask.is_active.is_(True)
                )
            ).all(),
        ),
    ):
        for item in records:
            due_at = item.due_at
            completed_at = getattr(item, "resolved_at", None) or getattr(
                item, "completed_at", None
            )
            comparison = completed_at or now
            if due_at and comparison > due_at and _within(due_at, query):
                label = (
                    getattr(item, "number", None)
                    or getattr(item, "title", None)
                    or getattr(item, "name", None)
                    or str(item.id)
                )
                rows.append(
                    (
                        kind,
                        str(label),
                        _text(getattr(item, "status", None)),
                        _text(due_at),
                        _text(completed_at),
                        f"{(comparison - due_at).total_seconds() / 3600:.1f}",
                    )
                )
    rows.sort(key=lambda row: float(row[-1]), reverse=True)
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.OPERATIONS_SLA],
        query,
        (CrmReportMetric("Violations", str(len(rows))),),
        (
            "Work type",
            "Reference",
            "Status",
            "Due",
            "Completed/resolved",
            "Hours overdue",
        ),
        rows,
    )


def _queue_classification(db: Session, query: CrmReportQuery) -> CrmReportPage:
    entries = list(
        db.scalars(
            select(InboxConversationQueueEntry).order_by(
                InboxConversationQueueEntry.entered_at.desc()
            )
        ).all()
    )
    conversations = {
        item.id: item for item in db.scalars(select(InboxConversation)).all()
    }
    classifications: Counter[str] = Counter()
    waits: list[float] = []
    rows: list[tuple[str, ...]] = []
    for entry in entries:
        if not _within(entry.entered_at, query):
            continue
        conversation = conversations.get(entry.conversation_id)
        metadata = (
            conversation.metadata_
            if conversation and isinstance(conversation.metadata_, dict)
            else {}
        )
        classification = str(
            metadata.get("ai_department")
            or metadata.get("classification")
            or "Unclassified"
        )
        tags = metadata.get("tags")
        tag_text = (
            ", ".join(str(value) for value in tags) if isinstance(tags, list) else "—"
        )
        wait = (
            (entry.settled_at - entry.entered_at).total_seconds()
            if entry.settled_at
            else None
        )
        if wait is not None:
            waits.append(max(wait, 0))
        classifications[classification] += 1
        rows.append(
            (
                _text(entry.entered_at),
                _text(entry.status),
                classification,
                tag_text,
                _text(wait),
                _text(conversation.channel_type if conversation else None),
            )
        )
    avg_wait = sum(waits) / len(waits) if waits else None
    note = "Classification uses recorded inbox AI/tag observations; unclassified conversations remain explicit."
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.QUEUE_CLASSIFICATION],
        query,
        (
            CrmReportMetric("Queue entries", str(len(rows))),
            CrmReportMetric("Average settled wait (s)", _text(avg_wait)),
            CrmReportMetric(
                "Unclassified", str(classifications.get("Unclassified", 0))
            ),
        ),
        (
            "Entered",
            "Queue status",
            "Classification",
            "Tags",
            "Wait seconds",
            "Channel",
        ),
        rows,
        note,
    )


def _subscriber_lifecycle(db: Session, query: CrmReportQuery) -> CrmReportPage:
    subscribers = [
        item
        for item in db.scalars(
            select(Subscriber).order_by(Subscriber.created_at.desc())
        ).all()
        if _within(item.created_at, query)
    ]
    subscriptions = [
        item
        for item in db.scalars(select(Subscription)).all()
        if _within(item.created_at, query)
    ]
    subscriber_status = Counter(_text(item.status) for item in subscribers)
    service_status = Counter(_text(item.status) for item in subscriptions)
    reasons = Counter(
        (item.cancel_reason or "Reason not captured")
        for item in subscriptions
        if item.canceled_at or _text(item.status) == "Canceled"
    )
    rows = (
        [
            ("Subscriber", key, str(value))
            for key, value in sorted(subscriber_status.items())
        ]
        + [
            ("Service", key, str(value))
            for key, value in sorted(service_status.items())
        ]
        + [
            ("Cancellation reason", key, str(value))
            for key, value in reasons.most_common()
        ]
    )
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.SUBSCRIBER_LIFECYCLE],
        query,
        (
            CrmReportMetric("Subscribers", str(len(subscribers))),
            CrmReportMetric("Services", str(len(subscriptions))),
            CrmReportMetric("Canceled services", str(sum(reasons.values()))),
        ),
        ("Lifecycle cohort", "State/reason", "Count"),
        rows,
        "This report uses Self-Care subscriber/service lifecycle facts only; CRM retention engagement records are excluded.",
    )


def _service_quality(db: Session, query: CrmReportQuery) -> CrmReportPage:
    subscribers = {item.id: item for item in db.scalars(select(Subscriber)).all()}
    ticket_counts: Counter[UUID] = Counter(
        item.subscriber_id
        for item in db.scalars(
            select(Ticket).where(
                Ticket.subscriber_id.is_not(None), Ticket.is_active.is_(True)
            )
        ).all()
        if item.subscriber_id and _within(item.created_at, query)
    )
    work_counts: Counter[UUID] = Counter(
        item.subscriber_id
        for item in db.scalars(
            select(WorkOrder).where(WorkOrder.is_active.is_(True))
        ).all()
        if _within(item.created_at, query)
    )
    subscription_owner: dict[UUID, UUID] = {
        subscription_id: subscriber_id
        for subscription_id, subscriber_id in db.execute(
            select(Subscription.id, Subscription.subscriber_id)
        ).all()
    }
    outage_hours: defaultdict[UUID, float] = defaultdict(float)
    now = datetime.now(UTC)
    for interval in db.scalars(select(CustomerOutageInterval)).all():
        if not _within(interval.started_at, query):
            continue
        owner = subscription_owner.get(interval.subscription_id)
        if owner is not None:
            outage_hours[owner] += max(
                ((interval.ended_at or now) - interval.started_at).total_seconds()
                / 3600,
                0,
            )
    ids = set(ticket_counts) | set(work_counts) | set(outage_hours)
    rows = [
        (
            _subscriber_name(subscribers.get(item_id)),
            str(ticket_counts[item_id]),
            str(work_counts[item_id]),
            f"{outage_hours[item_id]:.1f}",
            str(ticket_counts[item_id] + work_counts[item_id]),
        )
        for item_id in ids
    ]
    rows.sort(key=lambda row: (float(row[3]), int(row[4])), reverse=True)
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.SERVICE_QUALITY],
        query,
        (
            CrmReportMetric("Affected subscribers", str(len(ids))),
            CrmReportMetric("Tickets", str(sum(ticket_counts.values()))),
            CrmReportMetric("Work orders", str(sum(work_counts.values()))),
            CrmReportMetric("Outage hours", f"{sum(outage_hours.values()):.1f}"),
        ),
        (
            "Customer",
            "Tickets",
            "Work orders",
            "Outage hours",
            "Support/field contacts",
        ),
        rows,
    )


def _revenue_service(db: Session, query: CrmReportQuery) -> CrmReportPage:
    subscribers = {item.id: item for item in db.scalars(select(Subscriber)).all()}
    subscription_owner: dict[UUID, UUID] = {
        subscription_id: subscriber_id
        for subscription_id, subscriber_id in db.execute(
            select(Subscription.id, Subscription.subscriber_id)
        ).all()
    }
    invoiced, _paid, outstanding = _financial_maps(db, list(subscribers), query=query)
    outage_hours: defaultdict[UUID, float] = defaultdict(float)
    now = datetime.now(UTC)
    for interval in db.scalars(select(CustomerOutageInterval)).all():
        owner = subscription_owner.get(interval.subscription_id)
        if owner is not None and _within(interval.started_at, query):
            outage_hours[owner] += max(
                ((interval.ended_at or now) - interval.started_at).total_seconds()
                / 3600,
                0,
            )
    ids = set(invoiced) | set(outage_hours)
    rows = [
        (
            _subscriber_name(subscribers.get(item_id)),
            _money(invoiced.get(item_id)),
            _money(outstanding.get(item_id)),
            f"{outage_hours[item_id]:.1f}",
        )
        for item_id in ids
    ]
    rows.sort(key=lambda row: float(row[3]), reverse=True)
    note = "Downtime comes from the customer-outage interval owner. Credit exposure is not estimated because no authoritative compensation decision record exists."
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.REVENUE_SERVICE],
        query,
        (
            CrmReportMetric("Invoiced", _money(sum(invoiced.values(), Decimal(0)))),
            CrmReportMetric(
                "Outstanding", _money(sum(outstanding.values(), Decimal(0)))
            ),
            CrmReportMetric(
                "Customer outage hours", f"{sum(outage_hours.values()):.1f}"
            ),
        ),
        ("Customer", "Invoiced", "Outstanding", "Outage hours"),
        rows,
        note,
    )


def _project_task_performance(db: Session, query: CrmReportQuery) -> CrmReportPage:
    tasks = [
        task
        for task in db.scalars(
            select(ProjectTask).where(ProjectTask.is_active.is_(True))
        ).all()
        if _within(task.created_at, query)
    ]
    now = datetime.now(UTC)
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    durations: defaultdict[str, list[float]] = defaultdict(list)
    for task in tasks:
        people: tuple[UUID | None, ...] = tuple(task.assigned_to_person_ids) or (None,)
        for assigned_person_id in people:
            key = str(assigned_person_id) if assigned_person_id else "Unassigned"
            status = _text(task.status)
            grouped[key]["total"] += 1
            grouped[key][status.lower()] += 1
            if task.due_at and not task.completed_at and task.due_at < now:
                grouped[key]["overdue"] += 1
            if task.completed_at:
                start = task.start_at or task.created_at
                durations[key].append(
                    max((task.completed_at - start).total_seconds() / 3600, 0)
                )
    rows = []
    for person_key, counts in grouped.items():
        completed = counts["completed"] + counts["done"]
        average = (
            sum(durations[person_key]) / len(durations[person_key])
            if durations[person_key]
            else None
        )
        rows.append(
            (
                person_key,
                str(counts["total"]),
                str(completed),
                str(counts["blocked"]),
                str(counts["overdue"]),
                _text(average),
            )
        )
    rows.sort(key=lambda row: (int(row[4]), int(row[1])), reverse=True)
    return _page(
        REPORT_DEFINITIONS[CrmReportSlug.PROJECT_TASK_PERFORMANCE],
        query,
        (
            CrmReportMetric("Active task records", str(len(tasks))),
            CrmReportMetric("Assigned people", str(len(grouped))),
            CrmReportMetric(
                "Overdue", str(sum(counter["overdue"] for counter in grouped.values()))
            ),
        ),
        ("Person", "Tasks", "Completed", "Blocked", "Overdue", "Avg cycle hours"),
        rows,
        "Effort accuracy is omitted because Self-Care has no authoritative actual-effort observation to compare with estimates.",
    )


Builder = Callable[[Session, CrmReportQuery], CrmReportPage]
_BUILDERS: dict[CrmReportSlug, Builder] = {
    CrmReportSlug.ONLINE_ACTIVITY: _online_activity,
    CrmReportSlug.BILLING_RISK: _billing_risk,
    CrmReportSlug.SUBSCRIBER_REVENUE: _subscriber_revenue,
    CrmReportSlug.POSTPAID_CUSTOMERS: _postpaid,
    CrmReportSlug.CRM_PERFORMANCE: _crm_performance,
    CrmReportSlug.AGENT_PERFORMANCE: _agent_performance,
    CrmReportSlug.MY_PERFORMANCE: lambda db, query: _agent_performance(
        db, query, personal=True
    ),
    CrmReportSlug.OPERATIONS_SLA: _operations_sla,
    CrmReportSlug.QUEUE_CLASSIFICATION: _queue_classification,
    CrmReportSlug.SUBSCRIBER_LIFECYCLE: _subscriber_lifecycle,
    CrmReportSlug.SERVICE_QUALITY: _service_quality,
    CrmReportSlug.REVENUE_SERVICE: _revenue_service,
    CrmReportSlug.PROJECT_TASK_PERFORMANCE: _project_task_performance,
}


def get_report(
    db: Session, *, slug: CrmReportSlug, query: CrmReportQuery
) -> CrmReportPage:
    """Return one typed CRM report projection from authoritative inputs."""
    return _BUILDERS[slug](db, query)


def build_csv(report: CrmReportPage) -> str:
    """Serialize the exact filtered report projection rendered by the UI."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(report.columns)
    writer.writerows(report.rows)
    return output.getvalue()
