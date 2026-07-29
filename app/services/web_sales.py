"""Web helpers for the admin sales routes (leads, pipelines, quotes, orders).

Native admin-sales port: the context builders behind
``app/web/admin/sales.py``, adapted from the CRM's
``services/crm/web_{leads,sales,quotes}.py`` and the sales-order slices of
``web/admin/operations.py`` onto Sub's context-builder idiom
(``web_support_tickets`` pattern). Everything reads/writes through the native
owners in ``app.services.sales`` / ``app.services.sales_orders``; routes remain
transport adapters.

CRM-compatible Selfcare adaptations:

* Person/contact searches select existing ``subscriber_id`` identity records;
  contact labels resolve from batched Party and Subscriber projections.
* Status columns are plain strings in Sub, so
  no ``.value`` handling anywhere.
* Lead owners are native active service-team agents.
* List totals and KPI values come from the authoritative sales query owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import String, case, cast, func, or_
from sqlalchemy.orm import Session

from app.models.party import Party, PartyContactPoint
from app.models.sales import (
    Lead,
    LeadStatus,
    Pipeline,
    PipelineStage,
    Quote,
    QuoteStatus,
    SalesOrder,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.sales import (
    LeadCreate,
    LeadUpdate,
    PipelineCreate,
    PipelineStageCreate,
    PipelineStageUpdate,
    PipelineUpdate,
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteUpdate,
)
from app.services import sales as sales_service
from app.services import sales_orders as sales_orders_service
from app.services.common import coerce_uuid
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    PageMeta,
    request_needs_canonicalization,
)
from app.services.sales.selfserve import compute_feasibility
from app.services.sales_orders import _resolve_project_for_sales_order
from app.services.team_inbox_projection import list_agent_options

# The CRM's recommended default stage set, seeded when "create default
# stages" is ticked on the new-pipeline form (web_sales.py port).
DEFAULT_PIPELINE_STAGES: list[dict[str, int | str]] = [
    {"name": "Lead Identified", "probability": 10},
    {"name": "Qualification Call Completed", "probability": 20},
    {"name": "Needs Assessment / Demo", "probability": 35},
    {"name": "Proposal Sent", "probability": 50},
    {"name": "Commercial Negotiation", "probability": 70},
    {"name": "Decision Pending", "probability": 85},
    {"name": "Closed Won", "probability": 100},
    {"name": "Closed Lost", "probability": 0},
]

LOST_REASON_OPTIONS = (
    "Price too high",
    "Competitor chosen",
    "No budget",
    "Project cancelled",
    "No response",
    "Timing not right",
    "Other",
)


@dataclass(frozen=True, slots=True)
class LeadContactView:
    name: str
    email: str
    phone: str
    subscriber_id: UUID | None
    party_id: UUID | None
    detail_url: str | None


@dataclass(frozen=True, slots=True)
class LeadFormFields:
    title: str
    status: str
    subscriber_id: str
    contact_label: str
    owner_agent_id: str
    pipeline_id: str
    stage_id: str
    lead_source: str
    region: str
    estimated_value: str
    currency: str
    address: str
    probability: str
    expected_close_date: str
    lost_reason: str
    notes: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class ValidatedLeadForm:
    title: str
    status: LeadStatus
    subscriber_id: UUID | None
    owner_agent_id: UUID | None
    pipeline_id: UUID | None
    stage_id: UUID | None
    lead_source: str | None
    region: str | None
    estimated_value: Decimal | None
    currency: str | None
    address: str | None
    probability: int | None
    expected_close_date: date | None
    lost_reason: str | None
    notes: str | None
    is_active: bool


class LeadFormValidationError(ValueError):
    def __init__(self, field_errors: dict[str, str]):
        super().__init__("Please correct the highlighted fields.")
        self.field_errors = field_errors


def _as_bool(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def lead_status_values() -> list[str]:
    return [status.value for status in LeadStatus]


# The leads list's declared query capabilities. Sortable keys mirror the
# leads.list order_by whitelist (created_at/updated_at); filters and search are
# the ones the underlying manager supports.
LEAD_LIST_DEFINITION = ListDefinition(
    key="leads",
    fields=(
        ListFieldDefinition("title", "Lead", searchable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("lead_source", "Source", filterable=True),
        ListFieldDefinition("pipeline_id", "Pipeline", filterable=True),
        ListFieldDefinition("stage_id", "Stage", filterable=True),
        ListFieldDefinition("owner_agent_id", "Owner", filterable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
        ListFieldDefinition("updated_at", "Updated", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)

# The quotes list's declared capabilities (quotes.list order_by whitelist is
# created_at/updated_at; filters are status and lead).
QUOTE_LIST_DEFINITION = ListDefinition(
    key="quotes",
    fields=(
        ListFieldDefinition("number", "Quote", searchable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("lead_id", "Lead", filterable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
        ListFieldDefinition("updated_at", "Updated", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)

# The sales-orders list is sorted directly on the SalesOrder query, so any of
# these columns is sortable; filters are status/payment/source.
SALES_ORDER_LIST_DEFINITION = ListDefinition(
    key="sales_orders",
    fields=(
        ListFieldDefinition("order_number", "Order", searchable=True, sortable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("payment_status", "Payment", filterable=True),
        ListFieldDefinition("source_type", "Source", filterable=True),
        ListFieldDefinition("total", "Total", sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)
_SALES_ORDER_SORT_COLUMNS = {
    "created_at": SalesOrder.created_at,
    "order_number": SalesOrder.order_number,
    "total": SalesOrder.total,
}


def quote_status_values() -> list[str]:
    return [status.value for status in QuoteStatus]


def sales_order_status_values() -> list[str]:
    return [status.value for status in SalesOrderStatus]


def sales_order_payment_status_values() -> list[str]:
    return [status.value for status in SalesOrderPaymentStatus]


def _clean_choice(value: str | None, allowed: list[str]) -> str | None:
    """Return the filter value if it is a known vocabulary entry, else None
    (stale/hand-edited query params must not 400 a list page)."""
    candidate = (value or "").strip()
    return candidate if candidate in allowed else None


def _clean_uuid(value: str | None) -> str | None:
    """Canonicalize a UUID filter, clearing malformed stale bookmarks."""

    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        return str(UUID(candidate))
    except (TypeError, ValueError, AttributeError):
        return None


def subscriber_label(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return ""
    name = (
        subscriber.display_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
    )
    return name or subscriber.email or ""


def _subscriber_map(db: Session, ids: list) -> dict[str, Subscriber]:
    clean = [coerce_uuid(value) for value in ids if value]
    if not clean:
        return {}
    rows = db.query(Subscriber).filter(Subscriber.id.in_(clean)).all()
    return {str(row.id): row for row in rows}


def _total_pages(total: int, per_page: int) -> int:
    return (total + per_page - 1) // per_page if total else 1


def _sales_options(db: Session) -> dict[str, Any]:
    pipelines = sales_service.pipelines.list(
        db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    stages = sales_service.pipeline_stages.list(
        db,
        pipeline_id=None,
        is_active=True,
        order_by="order_index",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    agents = list_agent_options(db)
    return {
        "pipelines": pipelines,
        "stages": stages,
        "agents": agents,
        "pipeline_map": {str(item.id): item for item in pipelines},
        "stage_map": {str(item.id): item for item in stages},
        "agent_map": {str(item.id): item for item in agents},
    }


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def _count_leads(
    db: Session,
    *,
    status: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    owner_agent_id: str | None,
    lead_source: str | None,
    search: str | None,
) -> int:
    return sales_service.leads.count(
        db,
        status=status,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        owner_agent_id=owner_agent_id,
        lead_source=lead_source,
        search=search,
    )


def _lead_stats(db: Session) -> dict[str, Any]:
    summary = sales_service.leads.summary(db)
    return {
        "total": summary.total_leads,
        "open": summary.open_leads,
        "won": summary.won_leads,
        "total_value": summary.pipeline_value,
        "currency": summary.currency,
    }


def _lead_contact_views(
    db: Session, leads: list[Lead]
) -> tuple[dict[str, LeadContactView], dict[str, Subscriber]]:
    subscriber_map = _subscriber_map(db, [lead.subscriber_id for lead in leads])
    party_ids = {lead.party_id for lead in leads if lead.party_id is not None}
    parties = db.query(Party).filter(Party.id.in_(party_ids)).all() if party_ids else []
    party_map = {party.id: party for party in parties}
    contact_points = (
        db.query(PartyContactPoint)
        .filter(PartyContactPoint.party_id.in_(party_ids))
        .filter(PartyContactPoint.is_active.is_(True))
        .filter(PartyContactPoint.channel_type.in_(("email", "phone")))
        .order_by(
            PartyContactPoint.is_primary.desc(),
            PartyContactPoint.created_at.asc(),
        )
        .all()
        if party_ids
        else []
    )
    points_by_party: dict[UUID, dict[str, str]] = {}
    for point in contact_points:
        party_points = points_by_party.setdefault(point.party_id, {})
        party_points.setdefault(
            point.channel_type, point.display_value or point.normalized_value
        )

    views: dict[str, LeadContactView] = {}
    for lead in leads:
        subscriber = subscriber_map.get(str(lead.subscriber_id))
        party = party_map.get(lead.party_id) if lead.party_id else None
        party_points = points_by_party.get(lead.party_id, {}) if lead.party_id else {}
        name = (
            party.display_name
            if party is not None
            else subscriber_label(subscriber) or "Contact unavailable"
        )
        views[str(lead.id)] = LeadContactView(
            name=name,
            email=(
                party_points.get("email")
                or (subscriber.email if subscriber else "")
                or ""
            ),
            phone=(
                party_points.get("phone")
                or (subscriber.phone if subscriber else "")
                or ""
            ),
            subscriber_id=subscriber.id if subscriber is not None else None,
            party_id=lead.party_id,
            detail_url=(
                f"/admin/customers/person/{subscriber.id}"
                if subscriber is not None
                else None
            ),
        )
    return views, subscriber_map


def build_leads_list_context(
    db: Session,
    *,
    status: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    lead_source: str | None,
    search: str | None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int,
    per_page: int,
    owner_agent_id: str | None = None,
) -> dict[str, Any]:
    requested_status = status
    requested_pipeline_id = pipeline_id
    requested_stage_id = stage_id
    requested_owner_agent_id = owner_agent_id
    requested_lead_source = lead_source
    status = _clean_choice(status, lead_status_values())
    pipeline_id = _clean_uuid(pipeline_id)
    stage_id = _clean_uuid(stage_id)
    owner_agent_id = _clean_uuid(owner_agent_id)
    lead_source_options = list(sales_service.LEAD_SOURCE_OPTIONS)
    lead_source = _clean_choice(lead_source, lead_source_options)
    options = _sales_options(db)
    selected_stage = options["stage_map"].get(stage_id or "")
    if (
        pipeline_id
        and selected_stage is not None
        and str(selected_stage.pipeline_id) != pipeline_id
    ):
        stage_id = None
    visible_stages = [
        stage
        for stage in options["stages"]
        if not pipeline_id or str(stage.pipeline_id) == pipeline_id
    ]

    # Stale sort/page-size params degrade to the default view rather than 500.
    safe_sort = (
        sort_by
        if sort_by in LEAD_LIST_DEFINITION.sortable_keys
        else LEAD_LIST_DEFINITION.default_sort
    )
    safe_dir = sort_dir if sort_dir in ("asc", "desc") else None
    safe_per_page = (
        per_page
        if per_page in LEAD_LIST_DEFINITION.per_page_options
        else LEAD_LIST_DEFINITION.default_per_page
    )
    requested_query = LEAD_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "status": status,
            "pipeline_id": pipeline_id,
            "stage_id": stage_id,
            "owner_agent_id": owner_agent_id,
            "lead_source": lead_source,
        },
        sort_by=safe_sort,
        sort_dir=safe_dir,
        page=max(1, page),
        per_page=safe_per_page,
    )

    total = _count_leads(
        db,
        status=status,
        pipeline_id=pipeline_id or None,
        stage_id=stage_id or None,
        owner_agent_id=owner_agent_id or None,
        lead_source=lead_source,
        search=requested_query.search,
    )
    page_meta = PageMeta.from_query(requested_query, total)
    list_query = requested_query.with_page(page_meta.page)
    leads = sales_service.leads.list(
        db,
        pipeline_id=pipeline_id or None,
        stage_id=stage_id or None,
        owner_agent_id=owner_agent_id or None,
        status=status,
        is_active=None,
        order_by=list_query.sort_by,
        order_dir=list_query.sort_dir,
        limit=list_query.per_page,
        offset=(page_meta.page - 1) * list_query.per_page,
        lead_source=lead_source,
        search=list_query.search,
    )

    contact_map, subscriber_map = _lead_contact_views(db, leads)

    return {
        "leads": leads,
        "list_query": list_query,
        "canonicalization_needed": request_needs_canonicalization(
            list_query,
            search=search,
            filters={
                "status": requested_status,
                "pipeline_id": requested_pipeline_id,
                "stage_id": requested_stage_id,
                "owner_agent_id": requested_owner_agent_id,
                "lead_source": requested_lead_source,
            },
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        ),
        "page_meta": page_meta,
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": page_meta.total_items,
        "total_pages": page_meta.total_pages,
        "status": status or "",
        "pipeline_id": pipeline_id or "",
        "stage_id": stage_id or "",
        "owner_agent_id": owner_agent_id or "",
        "lead_source": lead_source or "",
        "search": list_query.search or "",
        "lead_statuses": lead_status_values(),
        "lead_sources": lead_source_options,
        "pipelines": options["pipelines"],
        "stages": visible_stages,
        "all_stages": options["stages"],
        "agents": options["agents"],
        "pipeline_map": options["pipeline_map"],
        "stage_map": options["stage_map"],
        "agent_map": options["agent_map"],
        "subscriber_map": subscriber_map,
        "contact_map": contact_map,
        "lead_stats": _lead_stats(db),
        "api_error": None,
    }


def build_leads_failure_context(
    *,
    search: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    safe_per_page = (
        per_page
        if per_page in LEAD_LIST_DEFINITION.per_page_options
        else LEAD_LIST_DEFINITION.default_per_page
    )
    list_query = LEAD_LIST_DEFINITION.build_query(
        search=search,
        filters={},
        page=max(1, page),
        per_page=safe_per_page,
    )
    page_meta = PageMeta.from_query(list_query, 0)
    return {
        "leads": [],
        "list_query": list_query,
        "canonicalization_needed": False,
        "page_meta": page_meta,
        "page": 1,
        "per_page": safe_per_page,
        "total": 0,
        "total_pages": 1,
        "status": "",
        "pipeline_id": "",
        "stage_id": "",
        "owner_agent_id": "",
        "lead_source": "",
        "search": list_query.search or "",
        "lead_statuses": lead_status_values(),
        "lead_sources": list(sales_service.LEAD_SOURCE_OPTIONS),
        "pipelines": [],
        "stages": [],
        "all_stages": [],
        "agents": [],
        "pipeline_map": {},
        "stage_map": {},
        "agent_map": {},
        "subscriber_map": {},
        "contact_map": {},
        "lead_stats": {
            "total": None,
            "open": None,
            "won": None,
            "total_value": None,
            "currency": "",
        },
        "api_error": "Leads could not be loaded. No CRM data was changed.",
        "retry_url": list_query.url("/admin/sales/leads"),
    }


def build_lead_detail_context(db: Session, *, lead_id: str) -> dict[str, Any]:
    lead = sales_service.leads.get(db, lead_id)
    quotes = sales_service.quotes.list(
        db,
        lead_id=str(lead.id),
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    contact_map, _ = _lead_contact_views(db, [lead])
    agents = list_agent_options(db)
    return {
        "lead": lead,
        "subscriber": lead.subscriber,
        "subscriber_label": subscriber_label(lead.subscriber),
        "contact": contact_map.get(str(lead.id)),
        "pipeline": lead.pipeline,
        "stage": lead.stage,
        "owner": next(
            (agent for agent in agents if agent.id == lead.owner_agent_id),
            None,
        ),
        "quotes": quotes,
        "can_create_quote": lead.subscriber_id is not None,
        "status_val": lead.status or LeadStatus.new.value,
        "lead_statuses": lead_status_values(),
        "api_error": None,
        "success": None,
    }


def _lead_form_fields(
    *,
    title: str | None,
    status: str | None,
    subscriber_id: str | None,
    contact_label: str | None,
    owner_agent_id: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    lead_source: str | None,
    region: str | None,
    estimated_value: str | None,
    currency: str | None,
    address: str | None,
    probability: str | None,
    expected_close_date: str | None,
    lost_reason: str | None,
    notes: str | None,
    is_active: bool,
) -> LeadFormFields:
    return LeadFormFields(
        title=(title or "").strip(),
        status=(status or LeadStatus.new.value).strip(),
        subscriber_id=(subscriber_id or "").strip(),
        contact_label=(contact_label or "").strip(),
        owner_agent_id=(owner_agent_id or "").strip(),
        pipeline_id=(pipeline_id or "").strip(),
        stage_id=(stage_id or "").strip(),
        lead_source=(lead_source or "").strip(),
        region=(region or "").strip(),
        estimated_value=(estimated_value or "").strip(),
        currency=(currency or "NGN").strip().upper(),
        address=(address or "").strip(),
        probability=(probability or "0").strip(),
        expected_close_date=(expected_close_date or "").strip(),
        lost_reason=(lost_reason or "").strip(),
        notes=(notes or "").strip(),
        is_active=is_active,
    )


def _lead_form_context(
    db: Session,
    *,
    lead_form: LeadFormFields,
    mode: str,
    lead_id: str | None,
) -> dict[str, Any]:
    options = _sales_options(db)
    return {
        "lead_form": lead_form,
        "lead": sales_service.leads.get(db, lead_id) if lead_id else None,
        "form_title": "Edit Lead" if mode == "update" else "New Lead",
        "submit_label": "Update Lead" if mode == "update" else "Create Lead",
        "action_url": (
            f"/admin/sales/leads/{lead_id}/edit"
            if mode == "update"
            else "/admin/sales/leads"
        ),
        "lead_statuses": lead_status_values(),
        "lead_sources": list(sales_service.LEAD_SOURCE_OPTIONS),
        "lost_reasons": LOST_REASON_OPTIONS,
        "pipelines": options["pipelines"],
        "stages": options["stages"],
        "agents": options["agents"],
        "field_errors": {},
        "error": None,
    }


def build_lead_new_context(db: Session) -> dict[str, Any]:
    return _lead_form_context(
        db,
        lead_form=_lead_form_fields(
            title=None,
            status=LeadStatus.new.value,
            subscriber_id=None,
            contact_label=None,
            owner_agent_id=None,
            pipeline_id=None,
            stage_id=None,
            lead_source=None,
            region=None,
            estimated_value=None,
            currency="NGN",
            address=None,
            probability="0",
            expected_close_date=None,
            lost_reason=None,
            notes=None,
            is_active=True,
        ),
        mode="create",
        lead_id=None,
    )


def build_lead_edit_context(db: Session, *, lead_id: str) -> dict[str, Any]:
    lead = sales_service.leads.get(db, lead_id)
    contact_map, _ = _lead_contact_views(db, [lead])
    contact = contact_map.get(str(lead.id))
    return _lead_form_context(
        db,
        lead_form=_lead_form_fields(
            title=lead.title,
            status=lead.status,
            subscriber_id=str(lead.subscriber_id) if lead.subscriber_id else None,
            contact_label=contact.name if contact else None,
            owner_agent_id=(str(lead.owner_agent_id) if lead.owner_agent_id else None),
            pipeline_id=str(lead.pipeline_id) if lead.pipeline_id else None,
            stage_id=str(lead.stage_id) if lead.stage_id else None,
            lead_source=lead.lead_source,
            region=lead.region,
            estimated_value=(
                str(lead.estimated_value) if lead.estimated_value is not None else None
            ),
            currency=lead.currency,
            address=lead.address,
            probability=(
                str(lead.probability) if lead.probability is not None else "0"
            ),
            expected_close_date=(
                lead.expected_close_date.isoformat()
                if lead.expected_close_date
                else None
            ),
            lost_reason=lead.lost_reason,
            notes=lead.notes,
            is_active=lead.is_active,
        ),
        mode="update",
        lead_id=lead_id,
    )


def build_lead_form_error_context(
    db: Session,
    *,
    mode: str,
    lead_id: str | None,
    field_errors: dict[str, str],
    **fields: str | bool | None,
) -> dict[str, Any]:
    context = _lead_form_context(
        db,
        lead_form=_lead_form_fields(
            title=str(fields.get("title") or ""),
            status=str(fields.get("status") or ""),
            subscriber_id=str(fields.get("subscriber_id") or ""),
            contact_label=str(fields.get("contact_label") or ""),
            owner_agent_id=str(fields.get("owner_agent_id") or ""),
            pipeline_id=str(fields.get("pipeline_id") or ""),
            stage_id=str(fields.get("stage_id") or ""),
            lead_source=str(fields.get("lead_source") or ""),
            region=str(fields.get("region") or ""),
            estimated_value=str(fields.get("estimated_value") or ""),
            currency=str(fields.get("currency") or ""),
            address=str(fields.get("address") or ""),
            probability=str(fields.get("probability") or ""),
            expected_close_date=str(fields.get("expected_close_date") or ""),
            lost_reason=str(fields.get("lost_reason") or ""),
            notes=str(fields.get("notes") or ""),
            is_active=bool(fields.get("is_active")),
        ),
        mode=mode,
        lead_id=lead_id,
    )
    context["field_errors"] = field_errors
    context["error"] = "Please correct the highlighted fields."
    return context


def _validate_lead_form(
    *,
    title: str | None,
    status: str | None,
    subscriber_id: str | None,
    owner_agent_id: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    lead_source: str | None,
    region: str | None,
    estimated_value: str | None,
    currency: str | None,
    address: str | None,
    probability: str | None,
    expected_close_date: str | None,
    lost_reason: str | None,
    notes: str | None,
    is_active: bool,
    contact_required: bool,
) -> ValidatedLeadForm:
    errors: dict[str, str] = {}
    clean_title = (title or "").strip()
    if not clean_title:
        errors["title"] = "Lead Name is required."

    try:
        clean_status = LeadStatus((status or LeadStatus.new.value).strip())
    except ValueError:
        errors["status"] = "Select a valid lead status."
        clean_status = LeadStatus.new

    def optional_uuid(value: str | None, field: str) -> UUID | None:
        clean = (value or "").strip()
        if not clean:
            return None
        try:
            return UUID(clean)
        except ValueError:
            errors[field] = "Select a valid option."
            return None

    clean_subscriber_id = optional_uuid(subscriber_id, "subscriber_id")
    if contact_required and clean_subscriber_id is None:
        errors["subscriber_id"] = "Person/Contact is required."
    clean_owner_id = optional_uuid(owner_agent_id, "owner_agent_id")
    clean_pipeline_id = optional_uuid(pipeline_id, "pipeline_id")
    clean_stage_id = optional_uuid(stage_id, "stage_id")

    clean_estimated_value: Decimal | None = None
    if (estimated_value or "").strip():
        try:
            clean_estimated_value = Decimal((estimated_value or "").strip())
        except (ArithmeticError, ValueError):
            errors["estimated_value"] = "Estimated Value must be a number."

    clean_probability: int | None = None
    if (probability or "").strip():
        try:
            clean_probability = int((probability or "").strip())
        except ValueError:
            errors["probability"] = "Probability must be a whole number."
        else:
            if not 0 <= clean_probability <= 100:
                errors["probability"] = "Probability must be between 0 and 100."

    clean_close_date: date | None = None
    if (expected_close_date or "").strip():
        try:
            clean_close_date = date.fromisoformat((expected_close_date or "").strip())
        except ValueError:
            errors["expected_close_date"] = "Enter a valid expected close date."

    clean_currency = (currency or "").strip().upper() or None
    if clean_currency is not None and len(clean_currency) != 3:
        errors["currency"] = "Currency must be a three-letter code."

    if errors:
        raise LeadFormValidationError(errors)
    return ValidatedLeadForm(
        title=clean_title,
        status=clean_status,
        subscriber_id=clean_subscriber_id,
        owner_agent_id=clean_owner_id,
        pipeline_id=clean_pipeline_id,
        stage_id=clean_stage_id,
        lead_source=(lead_source or "").strip() or None,
        region=(region or "").strip() or None,
        estimated_value=clean_estimated_value,
        currency=clean_currency,
        address=(address or "").strip() or None,
        probability=clean_probability,
        expected_close_date=clean_close_date,
        lost_reason=(
            (lost_reason or "").strip() or None
            if clean_status == LeadStatus.lost
            else None
        ),
        notes=(notes or "").strip() or None,
        is_active=is_active,
    )


def create_lead_from_form(
    db: Session,
    *,
    title: str | None,
    status: str | None,
    subscriber_id: str | None,
    owner_agent_id: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    lead_source: str | None,
    region: str | None,
    estimated_value: str | None,
    currency: str | None,
    address: str | None,
    probability: str | None,
    expected_close_date: str | None,
    lost_reason: str | None,
    notes: str | None,
    is_active: bool,
) -> tuple[str, bool]:
    values = _validate_lead_form(
        title=title,
        status=status,
        subscriber_id=subscriber_id,
        owner_agent_id=owner_agent_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        lead_source=lead_source,
        region=region,
        estimated_value=estimated_value,
        currency=currency,
        address=address,
        probability=probability,
        expected_close_date=expected_close_date,
        lost_reason=lost_reason,
        notes=notes,
        is_active=is_active,
        contact_required=True,
    )
    payload = LeadCreate(
        subscriber_id=values.subscriber_id,
        owner_agent_id=values.owner_agent_id,
        pipeline_id=values.pipeline_id,
        stage_id=values.stage_id,
        title=values.title,
        status=values.status,
        lead_source=values.lead_source,
        region=values.region,
        estimated_value=values.estimated_value,
        currency=values.currency,
        address=values.address,
        probability=values.probability,
        expected_close_date=values.expected_close_date,
        lost_reason=values.lost_reason,
        notes=values.notes,
        is_active=values.is_active,
    )
    lead = sales_service.leads.create(db, payload)
    return str(lead.id), bool(lead.dedup_returned_existing)


def update_lead_from_form(
    db: Session,
    *,
    lead_id: str,
    title: str | None,
    status: str | None,
    subscriber_id: str | None,
    contact_label: str | None,
    owner_agent_id: str | None,
    pipeline_id: str | None,
    stage_id: str | None,
    lead_source: str | None,
    region: str | None,
    estimated_value: str | None,
    currency: str | None,
    address: str | None,
    probability: str | None,
    expected_close_date: str | None,
    lost_reason: str | None,
    notes: str | None,
    is_active: bool,
) -> None:
    lead = sales_service.leads.get(db, lead_id)
    contact_map, _ = _lead_contact_views(db, [lead])
    current_contact = contact_map.get(str(lead.id))
    if (
        not (subscriber_id or "").strip()
        and current_contact is not None
        and (contact_label or "").strip() != current_contact.name
    ):
        raise LeadFormValidationError(
            {"subscriber_id": "Select a Person/Contact from the search results."}
        )
    values = _validate_lead_form(
        title=title,
        status=status,
        subscriber_id=subscriber_id,
        owner_agent_id=owner_agent_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        lead_source=lead_source,
        region=region,
        estimated_value=estimated_value,
        currency=currency,
        address=address,
        probability=probability,
        expected_close_date=expected_close_date,
        lost_reason=lost_reason,
        notes=notes,
        is_active=is_active,
        contact_required=lead.party_id is None and lead.subscriber_id is None,
    )
    payload = LeadUpdate(
        owner_agent_id=values.owner_agent_id,
        pipeline_id=values.pipeline_id,
        stage_id=values.stage_id,
        title=values.title,
        status=values.status,
        lead_source=values.lead_source,
        region=values.region,
        estimated_value=values.estimated_value,
        currency=values.currency,
        address=values.address,
        probability=values.probability,
        expected_close_date=values.expected_close_date,
        lost_reason=values.lost_reason,
        notes=values.notes,
        is_active=values.is_active,
    )
    if values.subscriber_id is not None:
        payload = payload.model_copy(update={"subscriber_id": values.subscriber_id})
    sales_service.leads.update(db, lead_id, payload)


def set_lead_status(db: Session, *, lead_id: str, status: str | None) -> None:
    try:
        selected = LeadStatus((status or "").strip())
    except ValueError:
        raise LeadFormValidationError(
            {"status": "Select a valid lead status."}
        ) from None
    sales_service.leads.update(db, lead_id, LeadUpdate(status=selected))


def deactivate_lead(db: Session, *, lead_id: str) -> None:
    sales_service.leads.delete(db, lead_id)


def build_leads_board_context(
    db: Session, *, pipeline_id: str | None
) -> dict[str, Any]:
    pipelines = sales_service.pipelines.list(
        db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    selected_pipeline_id = (pipeline_id or "").strip()
    if not selected_pipeline_id and pipelines:
        selected_pipeline_id = str(pipelines[0].id)
    return {
        "pipelines": pipelines,
        "selected_pipeline_id": selected_pipeline_id,
    }


# ---------------------------------------------------------------------------
# Pipelines (settings)
# ---------------------------------------------------------------------------


def build_pipeline_settings_context(
    db: Session, *, bulk_result: str, bulk_count: str
) -> dict[str, Any]:
    pipelines = (
        db.query(Pipeline)
        .order_by(Pipeline.is_active.desc(), Pipeline.created_at.desc())
        .limit(200)
        .all()
    )
    stages = (
        db.query(PipelineStage)
        .order_by(
            PipelineStage.pipeline_id.asc(),
            PipelineStage.order_index.asc(),
            PipelineStage.created_at.asc(),
        )
        .limit(1000)
        .all()
    )
    stage_map: dict[str, list[PipelineStage]] = {}
    for stage in stages:
        stage_map.setdefault(str(stage.pipeline_id), []).append(stage)
    return {
        "pipelines": pipelines,
        "stage_map": stage_map,
        "bulk_result": bulk_result,
        "bulk_count": bulk_count,
        "default_pipeline_stages": DEFAULT_PIPELINE_STAGES,
    }


def build_pipeline_new_context() -> dict[str, Any]:
    return {
        "pipeline": {"name": "", "is_active": True, "create_default_stages": True},
        "form_title": "New Pipeline",
        "submit_label": "Create Pipeline",
        "action_url": "/admin/sales/pipelines",
        "error": None,
    }


def build_pipeline_edit_context(db: Session, *, pipeline_id: str) -> dict[str, Any]:
    pipeline = sales_service.pipelines.get(db, pipeline_id)
    return {
        "pipeline": pipeline,
        "form_title": "Edit Pipeline",
        "submit_label": "Update Pipeline",
        "action_url": f"/admin/sales/pipelines/{pipeline_id}",
        "error": None,
    }


def build_pipeline_form_error_context(
    *,
    mode: str,
    pipeline_id: str | None,
    name: str | None,
    is_active: str | None,
    create_default_stages: str | None,
) -> dict[str, Any]:
    editing = mode == "update"
    return {
        "pipeline": {
            "id": pipeline_id,
            "name": (name or "").strip(),
            "is_active": _as_bool(is_active) if is_active is not None else True,
            "create_default_stages": _as_bool(create_default_stages),
        },
        "form_title": "Edit Pipeline" if editing else "New Pipeline",
        "submit_label": "Update Pipeline" if editing else "Create Pipeline",
        "action_url": (
            f"/admin/sales/pipelines/{pipeline_id}"
            if editing
            else "/admin/sales/pipelines"
        ),
    }


def create_pipeline_from_form(
    db: Session,
    *,
    name: str | None,
    is_active: str | None,
    create_default_stages: str | None,
) -> str:
    pipeline_name = (name or "").strip()
    if not pipeline_name:
        raise ValueError("Pipeline name is required.")
    payload = PipelineCreate(
        name=pipeline_name,
        is_active=_as_bool(is_active) if is_active is not None else True,
    )
    pipeline = sales_service.pipelines.create(db, payload)
    if _as_bool(create_default_stages):
        for index, stage in enumerate(DEFAULT_PIPELINE_STAGES):
            sales_service.pipeline_stages.create(
                db,
                PipelineStageCreate(
                    pipeline_id=pipeline.id,
                    name=str(stage["name"]),
                    order_index=index,
                    default_probability=int(stage["probability"]),
                    is_active=True,
                ),
            )
    return str(pipeline.id)


def update_pipeline_from_form(
    db: Session, *, pipeline_id: str, name: str | None, is_active: str | None
) -> None:
    payload = PipelineUpdate(
        name=(name or "").strip() or None,
        is_active=_as_bool(is_active) if is_active is not None else None,
    )
    sales_service.pipelines.update(db, pipeline_id, payload)


def deactivate_pipeline(db: Session, pipeline_id: str) -> None:
    sales_service.pipelines.delete(db, pipeline_id)


def create_stage_from_form(
    db: Session,
    *,
    pipeline_id: str,
    name: str,
    order_index: int,
    default_probability: int,
) -> None:
    sales_service.pipeline_stages.create(
        db,
        PipelineStageCreate(
            pipeline_id=coerce_uuid(pipeline_id),
            name=name.strip(),
            order_index=order_index,
            default_probability=default_probability,
            is_active=True,
        ),
    )


def update_stage_from_form(
    db: Session,
    *,
    stage_id: str,
    name: str,
    order_index: int,
    default_probability: int,
    is_active: str | None,
) -> None:
    sales_service.pipeline_stages.update(
        db,
        stage_id,
        PipelineStageUpdate(
            name=name.strip(),
            order_index=order_index,
            default_probability=default_probability,
            is_active=_as_bool(is_active) if is_active is not None else False,
        ),
    )


def deactivate_stage(db: Session, *, stage_id: str) -> None:
    sales_service.pipeline_stages.update(
        db, stage_id, PipelineStageUpdate(is_active=False)
    )


def bulk_assign_leads(
    db: Session, *, pipeline_id: str, stage_id: str | None, scope: str
) -> int:
    return sales_service.leads.bulk_assign_pipeline(
        db,
        pipeline_id,
        (stage_id or "").strip() or None,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


def _count_quotes(
    db: Session,
    *,
    status: str | None,
    lead_id: str | None,
    search: str | None,
) -> int:
    query = db.query(func.count(Quote.id)).select_from(Quote)
    query = query.filter(Quote.is_active.is_(True))
    if status:
        query = query.filter(Quote.status == status)
    if lead_id:
        query = query.filter(Quote.lead_id == coerce_uuid(lead_id))
    if search:
        like = f"%{search.strip()}%"
        query = query.outerjoin(
            Subscriber, Quote.subscriber_id == Subscriber.id
        ).filter(
            or_(
                Subscriber.display_name.ilike(like),
                Subscriber.first_name.ilike(like),
                Subscriber.last_name.ilike(like),
                Subscriber.email.ilike(like),
                cast(Quote.id, String).ilike(like),
            )
        )
    return int(query.scalar() or 0)


def _coordinate(
    value: str | None, *, field: str, low: float, high: float
) -> float | None:
    """Parse a map-pin coordinate from form input.

    Both coordinates are optional, but a half-pin is meaningless — the caller
    enforces that they arrive together.
    """
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        number = float(text_value)
    except ValueError:
        raise ValueError(f"{field} must be a number.") from None
    if not low <= number <= high:
        raise ValueError(f"{field} must be between {low} and {high}.")
    return number


def _install_pin(
    db: Session,
    *,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build the ``install{}`` map-pin block, and its feasibility, from a form.

    This is the same contract ``sales.selfserve`` stamps for portal-originated
    quotes (``install{latitude, longitude, address, region}`` on ``metadata_``),
    and downstream estimate/survey/billing read the pin from there. A staff-
    authored quote must be indistinguishable from a portal one, so we reuse the
    shape and the feasibility computation rather than inventing a parallel one.
    """
    lat = _coordinate(latitude, field="Latitude", low=-90.0, high=90.0)
    lng = _coordinate(longitude, field="Longitude", low=-180.0, high=180.0)
    if (lat is None) != (lng is None):
        raise ValueError("Drop a pin on the map: latitude and longitude go together.")

    clean_address = (address or "").strip() or None
    clean_region = (region or "").strip() or None
    if lat is None and lng is None and not clean_address and not clean_region:
        return None, None

    install = {
        "latitude": lat,
        "longitude": lng,
        "address": clean_address,
        "region": clean_region,
    }
    feasibility = (
        compute_feasibility(db, lat, lng)
        if lat is not None and lng is not None
        else None
    )
    return install, feasibility


def _merge_install_metadata(
    existing: Any,
    *,
    install: dict[str, Any] | None,
    feasibility: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Fold a new pin into a quote's existing metadata without clobbering it.

    ``metadata_`` carries the whole portal contract (source, project_type,
    deposit, pricing_mode...), so an admin edit must merge into it, never
    replace it. Clearing the pin removes only the keys the pin owns.
    """
    meta: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    if install is None:
        meta.pop("install", None)
        meta.pop("feasibility", None)
    else:
        meta["install"] = install
        if feasibility is not None:
            meta["feasibility"] = feasibility
        else:
            meta.pop("feasibility", None)
    return meta or None


def _quote_form_fields(
    *,
    subscriber_id: str | None,
    lead_id: str | None,
    status: str | None,
    currency: str | None,
    tax_rate: str | None,
    expires_at: str | None,
    notes: str | None,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> dict[str, Any]:
    return {
        "subscriber_id": (subscriber_id or "").strip(),
        "lead_id": (lead_id or "").strip(),
        "status": (status or QuoteStatus.draft.value).strip(),
        "currency": (currency or "NGN").strip().upper(),
        "tax_rate": (tax_rate or "").strip(),
        "expires_at": (expires_at or "").strip(),
        "notes": (notes or "").strip(),
        "latitude": (latitude or "").strip(),
        "longitude": (longitude or "").strip(),
        "address": (address or "").strip(),
        "region": (region or "").strip(),
    }


def creatable_quote_status_values() -> list[str]:
    """A new quote can only start as a draft.

    ``sent`` and ``accepted`` drive real downstream effects, and a quote that
    does not exist yet cannot have line items -- so offering them on the create
    form is offering a way to commit the business to a quote worth nothing.
    """
    return [
        value
        for value in quote_status_values()
        if value not in {QuoteStatus.sent.value, QuoteStatus.accepted.value}
    ]


def build_quote_new_context(
    db: Session | None = None, *, lead_id: str | None = None
) -> dict[str, Any]:
    normalized_lead_id = (lead_id or "").strip()
    selected_lead = (
        sales_service.leads.get(db, normalized_lead_id)
        if db is not None and normalized_lead_id
        else None
    )
    return {
        "quote_form": _quote_form_fields(
            subscriber_id=(
                str(selected_lead.subscriber_id)
                if selected_lead is not None and selected_lead.subscriber_id
                else None
            ),
            lead_id=str(selected_lead.id) if selected_lead is not None else None,
            status=QuoteStatus.draft.value,
            currency="NGN",
            tax_rate=None,
            expires_at=None,
            notes=None,
            latitude=None,
            longitude=None,
            address=None,
            region=None,
        ),
        "status_values": creatable_quote_status_values(),
        "form_title": "New Quote",
        "submit_label": "Create Quote",
        "action_url": "/admin/sales/quotes",
        "error": None,
    }


def build_quote_edit_context(db: Session, *, quote_id: str) -> dict[str, Any]:
    quote = sales_service.quotes.get(db, quote_id)
    meta = quote.metadata_ if isinstance(quote.metadata_, dict) else {}
    raw_install = meta.get("install")
    install: dict[str, Any] = raw_install if isinstance(raw_install, dict) else {}
    return {
        "quote": quote,
        "quote_form": _quote_form_fields(
            subscriber_id=str(quote.subscriber_id),
            lead_id=str(quote.lead_id) if quote.lead_id else None,
            status=quote.status,
            currency=quote.currency,
            tax_rate=str(quote.tax_rate) if quote.tax_rate is not None else None,
            expires_at=(
                quote.expires_at.date().isoformat() if quote.expires_at else None
            ),
            notes=quote.notes,
            latitude=(
                str(install.get("latitude"))
                if install.get("latitude") is not None
                else None
            ),
            longitude=(
                str(install.get("longitude"))
                if install.get("longitude") is not None
                else None
            ),
            address=install.get("address"),
            region=install.get("region"),
        ),
        "status_values": quote_status_values(),
        "form_title": "Edit Quote",
        "submit_label": "Update Quote",
        "action_url": f"/admin/sales/quotes/{quote_id}/edit",
        "error": None,
    }


def build_quote_form_error_context(
    *,
    mode: str,
    quote_id: str | None,
    **fields: str | None,
) -> dict[str, Any]:
    editing = mode == "update"
    return {
        "quote_form": _quote_form_fields(**fields),  # type: ignore[arg-type]
        "status_values": quote_status_values(),
        "form_title": "Edit Quote" if editing else "New Quote",
        "submit_label": "Update Quote" if editing else "Create Quote",
        "action_url": (
            f"/admin/sales/quotes/{quote_id}/edit" if editing else "/admin/sales/quotes"
        ),
    }


def _quote_expiry(value: str | None) -> datetime | None:
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value).replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("Expiry must be a valid date.") from None


def _quote_tax_rate(value: str | None) -> Decimal | None:
    text_value = (value or "").strip()
    if not text_value:
        return None
    try:
        return Decimal(text_value)
    except (ArithmeticError, ValueError):
        raise ValueError("Tax rate must be a number.") from None


def create_quote_from_form(
    db: Session,
    *,
    subscriber_id: str | None,
    lead_id: str | None,
    status: str | None,
    currency: str | None,
    tax_rate: str | None,
    expires_at: str | None,
    notes: str | None,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> str:
    if not (subscriber_id or "").strip():
        raise ValueError("A subscriber is required.")

    install, feasibility = _install_pin(
        db,
        latitude=latitude,
        longitude=longitude,
        address=address,
        region=region,
    )
    metadata = _merge_install_metadata(
        {"source": "admin"}, install=install, feasibility=feasibility
    )

    payload = QuoteCreate(
        subscriber_id=coerce_uuid(subscriber_id),
        lead_id=coerce_uuid(lead_id) if (lead_id or "").strip() else None,
        status=QuoteStatus(_clean_choice(status, quote_status_values()) or "draft"),
        currency=(currency or "NGN").strip().upper(),
        tax_rate=_quote_tax_rate(tax_rate),
        expires_at=_quote_expiry(expires_at),
        notes=(notes or "").strip() or None,
        metadata_=metadata,
    )
    quote = sales_service.quotes.create(db, payload)
    return str(quote.id)


def update_quote_from_form(
    db: Session,
    *,
    quote_id: str,
    subscriber_id: str | None,
    lead_id: str | None,
    status: str | None,
    currency: str | None,
    tax_rate: str | None,
    expires_at: str | None,
    notes: str | None,
    latitude: str | None,
    longitude: str | None,
    address: str | None,
    region: str | None,
) -> None:
    quote = sales_service.quotes.get(db, quote_id)
    install, feasibility = _install_pin(
        db,
        latitude=latitude,
        longitude=longitude,
        address=address,
        region=region,
    )
    metadata = _merge_install_metadata(
        quote.metadata_, install=install, feasibility=feasibility
    )

    payload = QuoteUpdate(
        subscriber_id=(
            coerce_uuid(subscriber_id) if (subscriber_id or "").strip() else None
        ),
        lead_id=coerce_uuid(lead_id) if (lead_id or "").strip() else None,
        status=(
            QuoteStatus(_clean_choice(status, quote_status_values()) or "draft")
            if (status or "").strip()
            else None
        ),
        currency=(currency or "").strip().upper() or None,
        tax_rate=_quote_tax_rate(tax_rate),
        expires_at=_quote_expiry(expires_at),
        notes=(notes or "").strip() or None,
        metadata_=metadata,
    )
    sales_service.quotes.update(db, quote_id, payload)


def add_quote_line_item_from_form(
    db: Session,
    *,
    quote_id: str,
    description: str | None,
    quantity: str | None,
    unit_price: str | None,
    discount_percent: str | None,
) -> None:
    """Add a priced line to a quote.

    Without this, a staff-authored quote has no lines, so its subtotal and total
    are zero — it is a quote for nothing. ``QuoteLineItems.create`` recalculates
    the quote totals, so the money follows from the lines rather than being
    typed in directly.
    """
    clean_description = (description or "").strip()
    if not clean_description:
        raise ValueError("A line item needs a description.")

    def _decimal(value: str | None, field: str, default: str) -> Decimal:
        text_value = (value or "").strip() or default
        try:
            return Decimal(text_value)
        except (ArithmeticError, ValueError):
            raise ValueError(f"{field} must be a number.") from None

    payload = QuoteLineItemCreate(
        quote_id=coerce_uuid(quote_id),
        description=clean_description,
        quantity=_decimal(quantity, "Quantity", "1"),
        unit_price=_decimal(unit_price, "Unit price", "0"),
        discount_percent=_decimal(discount_percent, "Discount", "0"),
    )
    sales_service.quote_line_items.create(db, payload)


def delete_quote_line_item(db: Session, item_id: str) -> None:
    sales_service.quote_line_items.delete(db, item_id)


def set_quote_status(db: Session, quote_id: str, status: str | None) -> None:
    clean = _clean_choice(status, quote_status_values())
    if not clean:
        raise ValueError("Unknown quote status.")
    sales_service.quotes.update(db, quote_id, QuoteUpdate(status=QuoteStatus(clean)))


def deactivate_quote(db: Session, quote_id: str) -> None:
    sales_service.quotes.delete(db, quote_id)


def build_quotes_list_context(
    db: Session,
    *,
    status: str | None,
    lead_id: str | None,
    search: str | None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    requested_status = status
    requested_lead_id = lead_id
    status = _clean_choice(status, quote_status_values())
    lead_id = _clean_uuid(lead_id)
    safe_sort = (
        sort_by
        if sort_by in QUOTE_LIST_DEFINITION.sortable_keys
        else QUOTE_LIST_DEFINITION.default_sort
    )
    safe_dir = sort_dir if sort_dir in ("asc", "desc") else None
    safe_per_page = (
        per_page
        if per_page in QUOTE_LIST_DEFINITION.per_page_options
        else QUOTE_LIST_DEFINITION.default_per_page
    )
    requested_query = QUOTE_LIST_DEFINITION.build_query(
        search=search,
        filters={"status": status, "lead_id": lead_id},
        sort_by=safe_sort,
        sort_dir=safe_dir,
        page=max(1, page),
        per_page=safe_per_page,
    )
    total = _count_quotes(
        db, status=status, lead_id=lead_id, search=requested_query.search
    )
    page_meta = PageMeta.from_query(requested_query, total)
    list_query = requested_query.with_page(page_meta.page)
    quotes = sales_service.quotes.list(
        db,
        lead_id=lead_id or None,
        status=status,
        is_active=None,
        order_by=list_query.sort_by,
        order_dir=list_query.sort_dir,
        limit=list_query.per_page,
        offset=(page_meta.page - 1) * list_query.per_page,
        search=list_query.search,
    )

    leads = sales_service.leads.list(
        db,
        pipeline_id=None,
        stage_id=None,
        owner_agent_id=None,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    subscriber_map = _subscriber_map(db, [quote.subscriber_id for quote in quotes])

    return {
        "quotes": quotes,
        "list_query": list_query,
        "canonicalization_needed": request_needs_canonicalization(
            list_query,
            search=search,
            filters={"status": requested_status, "lead_id": requested_lead_id},
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        ),
        "page_meta": page_meta,
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": page_meta.total_items,
        "total_pages": page_meta.total_pages,
        "status": status or "",
        "lead_id": lead_id or "",
        "search": list_query.search or "",
        "quote_statuses": quote_status_values(),
        "leads": leads,
        "lead_map": {str(item.id): item for item in leads},
        "subscriber_map": subscriber_map,
        "stats": sales_service.quotes.count_by_status(db),
        "today": datetime.now(UTC),
    }


def build_quote_detail_context(db: Session, *, quote_id: str) -> dict[str, Any]:
    quote = sales_service.quotes.get(db, quote_id)
    items = sales_service.quote_line_items.list(
        db,
        quote_id=str(quote.id),
        order_by="created_at",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    lead = None
    if quote.lead_id:
        lead = db.get(Lead, quote.lead_id)

    meta = quote.metadata_ if isinstance(quote.metadata_, dict) else {}
    deposit = meta.get("deposit") if isinstance(meta.get("deposit"), dict) else {}
    feasibility = (
        meta.get("feasibility") if isinstance(meta.get("feasibility"), dict) else {}
    )
    install = meta.get("install") if isinstance(meta.get("install"), dict) else {}

    return {
        "quote": quote,
        "items": items,
        "lead": lead,
        "subscriber": quote.subscriber,
        "subscriber_label": subscriber_label(quote.subscriber),
        "sales_order": quote.sales_order,
        "status_val": quote.status or QuoteStatus.draft.value,
        "is_accepted": (quote.status or "") == QuoteStatus.accepted.value,
        "quote_source": meta.get("source"),
        "quote_project_type": meta.get("project_type"),
        "deposit": deposit,
        "deposit_percent": meta.get("deposit_percent"),
        "estimate_provisional": meta.get("estimate_provisional"),
        "pricing_mode": meta.get("pricing_mode"),
        "feasibility": feasibility,
        "install": install,
        "today": datetime.now(UTC),
    }


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------


def _sales_orders_query(
    db: Session,
    *,
    status: str | None,
    payment_status: str | None,
    source_type: str | None,
    search: str | None,
):
    query = db.query(SalesOrder).filter(SalesOrder.is_active.is_(True))
    if status:
        query = query.filter(SalesOrder.status == status)
    if payment_status:
        query = query.filter(SalesOrder.payment_status == payment_status)
    if source_type == "quote":
        query = query.filter(SalesOrder.quote_id.isnot(None))
    elif source_type == "manual":
        query = query.filter(SalesOrder.quote_id.is_(None))
    if search:
        like = f"%{search.strip()}%"
        query = query.outerjoin(
            Subscriber, Subscriber.id == SalesOrder.subscriber_id
        ).filter(
            or_(
                SalesOrder.order_number.ilike(like),
                Subscriber.display_name.ilike(like),
                Subscriber.first_name.ilike(like),
                Subscriber.last_name.ilike(like),
                Subscriber.email.ilike(like),
                Subscriber.phone.ilike(like),
            )
        )
    return query


def build_sales_orders_list_context(
    db: Session,
    *,
    status: str | None,
    payment_status: str | None,
    source_type: str | None,
    search: str | None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    requested_status = status
    requested_payment_status = payment_status
    requested_source_type = source_type
    status = _clean_choice(status, sales_order_status_values())
    payment_status = _clean_choice(payment_status, sales_order_payment_status_values())
    if source_type not in {"quote", "manual"}:
        source_type = None

    safe_sort = (
        sort_by
        if sort_by in SALES_ORDER_LIST_DEFINITION.sortable_keys
        else SALES_ORDER_LIST_DEFINITION.default_sort
    )
    safe_dir = sort_dir if sort_dir in ("asc", "desc") else None
    safe_per_page = (
        per_page
        if per_page in SALES_ORDER_LIST_DEFINITION.per_page_options
        else SALES_ORDER_LIST_DEFINITION.default_per_page
    )
    requested_query = SALES_ORDER_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "status": status,
            "payment_status": payment_status,
            "source_type": source_type,
        },
        sort_by=safe_sort,
        sort_dir=safe_dir,
        page=max(1, page),
        per_page=safe_per_page,
    )
    filters = {
        "status": status,
        "payment_status": payment_status,
        "source_type": source_type,
        "search": requested_query.search,
    }

    totals = (
        _sales_orders_query(db, **filters)
        .with_entities(
            func.count(SalesOrder.id).label("total"),
            func.coalesce(func.sum(SalesOrder.total), 0).label("gross_sales"),
            func.coalesce(func.sum(SalesOrder.amount_paid), 0).label("collected"),
            func.coalesce(func.sum(SalesOrder.balance_due), 0).label("outstanding"),
            func.sum(
                case(
                    (
                        SalesOrder.payment_status == SalesOrderPaymentStatus.paid.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("paid"),
            func.sum(
                case(
                    (
                        SalesOrder.payment_status
                        == SalesOrderPaymentStatus.partial.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("partial"),
            func.sum(
                case(
                    (
                        SalesOrder.payment_status
                        == SalesOrderPaymentStatus.pending.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("pending_payment"),
            func.sum(case((SalesOrder.quote_id.isnot(None), 1), else_=0)).label(
                "quote_backed"
            ),
            func.sum(case((SalesOrder.quote_id.is_(None), 1), else_=0)).label("manual"),
        )
        .one()
    )
    total = int(totals.total or 0)
    page_meta = PageMeta.from_query(requested_query, total)
    list_query = requested_query.with_page(page_meta.page)
    sort_column = _SALES_ORDER_SORT_COLUMNS[list_query.sort_by]
    ordered = sort_column.desc() if list_query.sort_dir == "desc" else sort_column.asc()
    orders = (
        _sales_orders_query(db, **filters)
        # Unique tie-breaker keeps ordering deterministic across pages.
        .order_by(ordered, SalesOrder.id.asc())
        .limit(list_query.per_page)
        .offset((page_meta.page - 1) * list_query.per_page)
        .all()
    )
    paid = int(totals.paid or 0)
    stats = {
        "total": total,
        "gross_sales": Decimal(str(totals.gross_sales or 0)),
        "collected": Decimal(str(totals.collected or 0)),
        "outstanding": Decimal(str(totals.outstanding or 0)),
        "paid": paid,
        "partial": int(totals.partial or 0),
        "pending_payment": int(totals.pending_payment or 0),
        "quote_backed": int(totals.quote_backed or 0),
        "manual": int(totals.manual or 0),
        "paid_rate": round((paid / total) * 100, 1) if total else 0,
    }

    subscriber_map = _subscriber_map(db, [order.subscriber_id for order in orders])

    return {
        "orders": orders,
        "stats": stats,
        "list_query": list_query,
        "canonicalization_needed": request_needs_canonicalization(
            list_query,
            search=search,
            filters={
                "status": requested_status,
                "payment_status": requested_payment_status,
                "source_type": requested_source_type,
            },
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        ),
        "page_meta": page_meta,
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": total,
        "total_pages": page_meta.total_pages,
        "status": status or "",
        "payment_status": payment_status or "",
        "source_type": source_type or "",
        "search": list_query.search or "",
        "statuses": sales_order_status_values(),
        "payment_statuses": sales_order_payment_status_values(),
        "subscriber_map": subscriber_map,
    }


def build_sales_order_detail_context(
    db: Session, *, sales_order_id: str
) -> dict[str, Any]:
    order = sales_orders_service.sales_orders.get(db, sales_order_id)
    lines = sales_orders_service.sales_order_lines.list(
        db,
        sales_order_id=str(order.id),
        order_by="created_at",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    project = _resolve_project_for_sales_order(db, order.id)
    return {
        "order": order,
        "lines": lines,
        "subscriber": order.subscriber,
        "subscriber_label": subscriber_label(order.subscriber),
        "quote": order.quote,
        "project": project,
    }
