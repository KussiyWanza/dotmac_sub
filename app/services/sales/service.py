"""Leads / pipeline / quotes services — CRM port.

Faithful port of ``dotmac_crm/app/services/crm/sales/service.py`` onto sub's
native models (``app/models/sales.py``), with the deltas applied:

* Revision 457 makes both Lead and Quote Party/Lead-first. Subscriber remains
  optional account context until the atomic Accepted-Quote conversion.
* Staff references (``quotes.owner_person_id``) are plain UUIDs — no FK and
  no existence check; display resolves via the staff map.
* stubs (risk #8): owner-agent auto-assignment from the CRM inbox
  (ConversationAssignment / last agent-authored message) and lead-source
  inference from messages / person channels degrade to None. Legacy metadata
  source inference remains compatibility behavior, while canonical new
  attribution uses immutable structured origin capture.
* ``lead_source`` vocabulary gains ``Portal`` (+ ``portal`` alias): the fix
  for the live self-serve quote-request 400 (/ risk #7 — CRM's
  ``PortalQuotes.request`` passes ``lead_source="portal"`` which the old
  vocabulary rejected).
* Statuses are stored as plain strings (sub convention: String column +
  app-level enum); helpers normalise enum members to their values.
* ``quote_line_items.inventory_item_id`` is carried verbatim without an
  existence check — inventory remains externally owned.
* Accepted-Quote conversion delegates to the atomic
  ``sales.quote_acceptance`` application coordinator.
* Native services emit sub events from day one (risk #13):
  ``lead.created`` / ``quote.accepted``.
"""

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import TypeVar

from fastapi import HTTPException
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression

from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.party import Party, PartyContactPoint
from app.models.project import ProjectType
from app.models.sales import (
    Lead,
    LeadStatus,
    Pipeline,
    PipelineStage,
    Quote,
    QuoteLineItem,
    QuoteStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.sales import (
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteLineItemUpdate,
    QuoteUpdate,
)
from app.services import control_registry, settings_spec
from app.services.audit_adapter import stage_audit_event
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    round_money,
    validate_enum,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import CommandContext
from app.services.response import ListResponseMixin
from app.services.sales import lifecycle as lead_lifecycle
from app.services.sales import pipeline_configuration

_logger = logging.getLogger(__name__)


def _stage_quote_audit(
    db: Session,
    *,
    action: str,
    quote_id: uuid.UUID,
    context: CommandContext | None,
    metadata: dict[str, object] | None = None,
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type="quote",
        entity_id=str(quote_id),
        actor_type=AuditActorType.user if context else AuditActorType.system,
        actor_id=context.actor if context else "sales.service",
        request_id=str(context.command_id) if context else None,
        metadata=metadata,
    )


# Normalized lead-source vocabulary. ``Portal`` is the addition — the
# self-serve (map-pin) quote request tags its leads with it.
class LeadSource(StrEnum):
    FACEBOOK = "Facebook"
    INSTAGRAM = "Instagram"
    WHATSAPP = "Whatsapp"
    EMAIL = "Email"
    REFERRER = "Referrer"
    INSTAGRAM_ADS = "Instagram Ads"
    FACEBOOK_ADS = "Facebook Ads"
    GOOGLE = "Google"
    WEBSITE = "Website"
    PORTAL = "Portal"


LEAD_SOURCE_OPTIONS = tuple(source.value for source in LeadSource)


@dataclass(frozen=True, slots=True)
class LeadMaintenanceUpdate:
    """Validated Lead values staged by the admin maintenance coordinator."""

    lead_id: uuid.UUID
    title: str
    status: LeadStatus
    owner_agent_id: uuid.UUID | None
    pipeline_id: uuid.UUID | None
    stage_id: uuid.UUID | None
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
    reseller_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    region_zone_id: uuid.UUID | None
    reseller_routed: bool
    edit_key: uuid.UUID
    edit_fingerprint: str


_LEAD_SOURCE_NORMALIZED_MAP = {
    "facebook": "Facebook",
    "facebook messenger": "Facebook",
    "facebook_messenger": "Facebook",
    "instagram": "Instagram",
    "instagram dm": "Instagram",
    "instagram_dm": "Instagram",
    "whatsapp": "Whatsapp",
    "wa": "Whatsapp",
    "email": "Email",
    "referrer": "Referrer",
    "referral": "Referrer",
    "instagram ads": "Instagram Ads",
    "instagram ad": "Instagram Ads",
    "ig ads": "Instagram Ads",
    "ig ad": "Instagram Ads",
    "facebook ads": "Facebook Ads",
    "facebook ad": "Facebook Ads",
    "fb ads": "Facebook Ads",
    "fb ad": "Facebook Ads",
    "meta ads": "Facebook Ads",
    "meta ad": "Facebook Ads",
    "google": "Google",
    "google ads": "Google",
    "google ad": "Google",
    "adwords": "Google",
    "website": "Website",
    "web": "Website",
    "chat widget": "Website",
    "chat_widget": "Website",
    "portal": "Portal",
    "portal_self_serve": "Portal",
    "self serve": "Portal",
    "self_serve": "Portal",
}

# Lead statuses that count as an "open" deal (not yet won/lost).
_OPEN_LEAD_STATUSES = (
    LeadStatus.new.value,
    LeadStatus.contacted.value,
    LeadStatus.qualified.value,
    LeadStatus.proposal.value,
    LeadStatus.negotiation.value,
)

_CLOSED_LEAD_STATUSES = (LeadStatus.won.value, LeadStatus.lost.value)


@dataclass(frozen=True, slots=True)
class LeadPipelineSummary:
    """Authoritative lead KPI projection shared by API and admin UI."""

    total_leads: int
    open_leads: int
    won_leads: int
    pipeline_value: Decimal
    currency: str


class LeadListSortField(StrEnum):
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class LeadListSortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class LeadListQueryInput:
    """Raw adapter values for the authoritative Lead list query."""

    search_term: str | None = None
    status: str | None = None
    pipeline_id: str | None = None
    stage_id: str | None = None
    owner_agent_id: str | None = None
    lead_source: str | None = None
    sort_field: str | None = None
    sort_direction: str | None = None
    page: int = 1
    page_size: int = 25


@dataclass(frozen=True, slots=True)
class LeadListQuery:
    """Normalized Lead list scope shared by rows, count, and summary."""

    search_term: str | None
    status: LeadStatus | None
    pipeline_id: uuid.UUID | None
    stage_id: uuid.UUID | None
    owner_agent_id: uuid.UUID | None
    lead_source: LeadSource | None
    sort_field: LeadListSortField
    sort_direction: LeadListSortDirection
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


@dataclass(frozen=True, slots=True)
class LeadListQueryResult:
    """One unique, deterministically ordered page plus matching projections."""

    items: tuple[Lead, ...]
    total_count: int
    query: LeadListQuery
    summary: LeadPipelineSummary


class QuoteListSortField(StrEnum):
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class QuoteListSortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class QuoteListQueryInput:
    """Raw adapter values for the authoritative Quote list query."""

    search_term: str | None = None
    status: str | None = None
    lead_id: str | None = None
    sort_field: str | None = None
    sort_direction: str | None = None
    page: int = 1
    page_size: int = 25


@dataclass(frozen=True, slots=True)
class QuoteListQuery:
    """Normalized Quote scope shared by rows, count, ordering, and pagination."""

    search_term: str | None
    status: QuoteStatus | None
    lead_id: uuid.UUID | None
    sort_field: QuoteListSortField
    sort_direction: QuoteListSortDirection
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


@dataclass(frozen=True, slots=True)
class QuoteListQueryResult:
    """One unique, deterministically ordered Quote page and its exact count."""

    items: tuple[Quote, ...]
    total_count: int
    query: QuoteListQuery


@dataclass(frozen=True, slots=True)
class _LeadListFilters:
    search_term: str | None
    status: str | None
    pipeline_id: uuid.UUID | None
    stage_id: uuid.UUID | None
    owner_agent_id: uuid.UUID | None
    lead_source: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class _QuoteListFilters:
    search_term: str | None
    status: str | None
    lead_id: uuid.UUID | None
    is_active: bool


def normalize_lead_search(value: str | None) -> str | None:
    """Collapse user-entered whitespace without changing search casing."""

    normalized = " ".join(str(value or "").split())
    return normalized or None


def normalize_quote_search(value: str | None) -> str | None:
    """Collapse whitespace while retaining the operator's literal search text."""

    normalized = " ".join(str(value or "").split())
    return normalized or None


def _escape_like_pattern(value: str) -> str:
    """Treat LIKE metacharacters as ordinary user-entered search characters."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _optional_uuid_filter(value: str | None) -> uuid.UUID | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        return uuid.UUID(candidate)
    except (AttributeError, TypeError, ValueError):
        return None


EnumT = TypeVar("EnumT", bound=Enum)


def _optional_enum_filter(
    value: str | None,
    enum_type: type[EnumT],
) -> EnumT | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        return enum_type(candidate)
    except ValueError:
        return None


def _normalize_lead_list_query(
    db: Session,
    request: LeadListQueryInput,
) -> LeadListQuery:
    pipeline_id = _optional_uuid_filter(request.pipeline_id)
    stage_id = _optional_uuid_filter(request.stage_id)
    if pipeline_id is not None and stage_id is not None:
        selected_stage = (
            db.query(PipelineStage)
            .filter(PipelineStage.id == stage_id)
            .filter(PipelineStage.is_active.is_(True))
            .one_or_none()
        )
        if selected_stage is not None and selected_stage.pipeline_id != pipeline_id:
            stage_id = None

    return LeadListQuery(
        search_term=normalize_lead_search(request.search_term),
        status=_optional_enum_filter(request.status, LeadStatus),
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        owner_agent_id=_optional_uuid_filter(request.owner_agent_id),
        lead_source=_optional_enum_filter(request.lead_source, LeadSource),
        sort_field=(
            _optional_enum_filter(request.sort_field, LeadListSortField)
            or LeadListSortField.CREATED_AT
        ),
        sort_direction=(
            _optional_enum_filter(request.sort_direction, LeadListSortDirection)
            or LeadListSortDirection.DESC
        ),
        page=max(1, request.page),
        page_size=request.page_size if request.page_size in (10, 25, 50, 100) else 25,
    )


def _normalize_quote_list_query(
    db: Session,
    request: QuoteListQueryInput,
) -> QuoteListQuery:
    lead_id = _optional_uuid_filter(request.lead_id)
    if lead_id is not None:
        selected_lead = db.get(Lead, lead_id)
        if selected_lead is None or not selected_lead.is_active:
            lead_id = None

    return QuoteListQuery(
        search_term=normalize_quote_search(request.search_term),
        status=_optional_enum_filter(request.status, QuoteStatus),
        lead_id=lead_id,
        sort_field=(
            _optional_enum_filter(request.sort_field, QuoteListSortField)
            or QuoteListSortField.CREATED_AT
        ),
        sort_direction=(
            _optional_enum_filter(request.sort_direction, QuoteListSortDirection)
            or QuoteListSortDirection.DESC
        ),
        page=max(1, request.page),
        page_size=request.page_size if request.page_size in (10, 25, 50, 100) else 25,
    )


def _phone_search_value(
    column: SQLColumnExpression[str | None],
) -> ColumnElement[str]:
    normalized: ColumnElement[str] = func.coalesce(column, "")
    for character in (" ", "-", "(", ")", ".", "+"):
        normalized = func.replace(normalized, character, "")
    return normalized


def _lead_search_predicate(search_term: str) -> ColumnElement[bool]:
    pattern = f"%{search_term}%"
    subscriber_full_name = func.trim(
        func.coalesce(Subscriber.first_name, "")
        + " "
        + func.coalesce(Subscriber.last_name, "")
    )
    subscriber_matches: ColumnElement[bool] = (
        select(1)
        .where(
            Subscriber.id == Lead.subscriber_id,
            or_(
                Subscriber.display_name.ilike(pattern),
                subscriber_full_name.ilike(pattern),
                Subscriber.first_name.ilike(pattern),
                Subscriber.last_name.ilike(pattern),
                Subscriber.email.ilike(pattern),
                Subscriber.phone.ilike(pattern),
            ),
        )
        .correlate(Lead)
        .exists()
    )
    party_matches = (
        select(1)
        .where(Party.id == Lead.party_id, Party.display_name.ilike(pattern))
        .correlate(Lead)
        .exists()
    )

    contact_conditions: list[ColumnElement[bool]] = [
        PartyContactPoint.display_value.ilike(pattern),
        PartyContactPoint.normalized_value.ilike(pattern),
    ]
    digits = "".join(character for character in search_term if character.isdigit())
    if len(digits) >= 4:
        digit_pattern = f"%{digits}%"
        subscriber_matches = or_(
            subscriber_matches,
            select(1)
            .where(
                Subscriber.id == Lead.subscriber_id,
                _phone_search_value(Subscriber.phone).ilike(digit_pattern),
            )
            .correlate(Lead)
            .exists(),
        )
        contact_conditions.append(
            (PartyContactPoint.channel_type == "phone")
            & _phone_search_value(PartyContactPoint.display_value).ilike(digit_pattern)
        )

    contact_matches = (
        select(1)
        .where(
            PartyContactPoint.party_id == Lead.party_id,
            PartyContactPoint.is_active.is_(True),
            PartyContactPoint.channel_type.in_(("email", "phone")),
            or_(*contact_conditions),
        )
        .correlate(Lead)
        .exists()
    )
    return or_(
        Lead.title.ilike(pattern),
        party_matches,
        contact_matches,
        subscriber_matches,
    )


def _quote_search_predicate(search_term: str) -> ColumnElement[bool]:
    """Search authoritative Quote relationships without multiplying Quote rows."""

    escaped = _escape_like_pattern(search_term)
    pattern = f"%{escaped}%"
    subscriber_full_name = func.trim(
        func.coalesce(Subscriber.first_name, "")
        + " "
        + func.coalesce(Subscriber.last_name, "")
    )
    subscriber_matches = (
        select(1)
        .where(
            Subscriber.id == Quote.subscriber_id,
            or_(
                Subscriber.display_name.ilike(pattern, escape="\\"),
                subscriber_full_name.ilike(pattern, escape="\\"),
                Subscriber.first_name.ilike(pattern, escape="\\"),
                Subscriber.last_name.ilike(pattern, escape="\\"),
                Subscriber.email.ilike(pattern, escape="\\"),
            ),
        )
        .correlate(Quote)
        .exists()
    )
    lead_matches = (
        select(1)
        .where(
            Lead.id == Quote.lead_id,
            Lead.title.ilike(pattern, escape="\\"),
        )
        .correlate(Quote)
        .exists()
    )
    party_matches = (
        select(1)
        .select_from(Lead)
        .join(Party, Party.id == Lead.party_id)
        .where(
            Lead.id == Quote.lead_id,
            Party.display_name.ilike(pattern, escape="\\"),
        )
        .correlate(Quote)
        .exists()
    )
    contact_conditions: list[ColumnElement[bool]] = [
        PartyContactPoint.display_value.ilike(pattern, escape="\\"),
        PartyContactPoint.normalized_value.ilike(pattern, escape="\\"),
    ]
    digits = "".join(character for character in search_term if character.isdigit())
    if len(digits) >= 4:
        contact_conditions.append(
            (PartyContactPoint.channel_type == "phone")
            & _phone_search_value(PartyContactPoint.display_value).ilike(
                f"%{digits}%", escape="\\"
            )
        )
    contact_matches = (
        select(1)
        .select_from(Lead)
        .join(PartyContactPoint, PartyContactPoint.party_id == Lead.party_id)
        .where(
            Lead.id == Quote.lead_id,
            PartyContactPoint.is_active.is_(True),
            or_(*contact_conditions),
        )
        .correlate(Quote)
        .exists()
    )
    return or_(
        cast(Quote.id, String).ilike(pattern, escape="\\"),
        lead_matches,
        party_matches,
        contact_matches,
        subscriber_matches,
    )


def _lead_list_predicates(
    filters: _LeadListFilters,
) -> tuple[ColumnElement[bool], ...]:
    predicates: list[ColumnElement[bool]] = [Lead.is_active == filters.is_active]
    if filters.pipeline_id is not None:
        predicates.append(Lead.pipeline_id == filters.pipeline_id)
    if filters.stage_id is not None:
        predicates.append(Lead.stage_id == filters.stage_id)
    if filters.owner_agent_id is not None:
        predicates.append(Lead.owner_agent_id == filters.owner_agent_id)
    if filters.status is not None:
        predicates.append(Lead.status == filters.status)
    if filters.lead_source is not None:
        predicates.append(func.lower(Lead.lead_source) == filters.lead_source.lower())
    if filters.search_term is not None:
        predicates.append(_lead_search_predicate(filters.search_term))
    return tuple(predicates)


def _lead_list_filters(query: LeadListQuery) -> _LeadListFilters:
    return _LeadListFilters(
        search_term=query.search_term,
        status=query.status.value if query.status is not None else None,
        pipeline_id=query.pipeline_id,
        stage_id=query.stage_id,
        owner_agent_id=query.owner_agent_id,
        lead_source=query.lead_source.value if query.lead_source is not None else None,
        is_active=True,
    )


def _quote_list_predicates(
    filters: _QuoteListFilters,
) -> tuple[ColumnElement[bool], ...]:
    predicates: list[ColumnElement[bool]] = [Quote.is_active == filters.is_active]
    if filters.status is not None:
        predicates.append(Quote.status == filters.status)
    if filters.lead_id is not None:
        predicates.append(Quote.lead_id == filters.lead_id)
    if filters.search_term is not None:
        predicates.append(_quote_search_predicate(filters.search_term))
    return tuple(predicates)


def _quote_list_filters(query: QuoteListQuery) -> _QuoteListFilters:
    return _QuoteListFilters(
        search_term=query.search_term,
        status=query.status.value if query.status is not None else None,
        lead_id=query.lead_id,
        is_active=True,
    )


def _lead_summary_for_predicates(
    db: Session,
    predicates: tuple[ColumnElement[bool], ...],
) -> LeadPipelineSummary:
    rows = (
        db.query(
            Lead.status,
            func.count(Lead.id),
            func.coalesce(func.sum(Lead.estimated_value), 0),
        )
        .filter(*predicates)
        .group_by(Lead.status)
        .all()
    )
    by_status: dict[str, int] = {}
    total = 0
    pipeline_value = Decimal("0")
    for status_value, count, value_sum in rows:
        status_key = status_value or LeadStatus.new.value
        status_count = int(count)
        by_status[status_key] = by_status.get(status_key, 0) + status_count
        total += status_count
        if status_key in _OPEN_LEAD_STATUSES:
            pipeline_value += Decimal(str(value_sum or 0))
    return LeadPipelineSummary(
        total_leads=total,
        open_leads=sum(
            by_status.get(status_key, 0) for status_key in _OPEN_LEAD_STATUSES
        ),
        won_leads=by_status.get(LeadStatus.won.value, 0),
        pipeline_value=pipeline_value,
        currency=_default_currency(db) or "NGN",
    )


def _enum_str(value, enum_cls, label: str) -> str | None:
    """Validate ``value`` against ``enum_cls`` and return its string value.

    Sub stores CRM's PG-enum columns as plain strings, so
    every write path normalises enum members / raw strings to ``.value``.
    """
    member = validate_enum(value, enum_cls, label)
    return member.value if member is not None else None


def _resolve_owner_agent_id(db: Session, subscriber_id) -> uuid.UUID | None:
    """stub (risk #8).

    The CRM resolved a lead's owner agent from the inbox: the active
    ConversationAssignment for the person, falling back to the author of the
    last agent-authored message. Those models (``crm_agents``,
    conversations) arrive with the inbox port — until then leads
    land unowned (visible as "unassigned" in the kanban).
    """
    return None


def _validate_lead_pipeline_stage(
    db: Session,
    *,
    pipeline_id: uuid.UUID | None,
    stage_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Validate one pipeline/stage pair and infer the pipeline from a stage."""

    if stage_id is None:
        return pipeline_id
    stage = db.get(PipelineStage, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail="Pipeline stage not found")
    if pipeline_id is not None and stage.pipeline_id != pipeline_id:
        raise HTTPException(
            status_code=400,
            detail="The selected stage does not belong to the selected pipeline",
        )
    return stage.pipeline_id


def _lead_title_from_subscriber(subscriber: Subscriber | None) -> str | None:
    if not subscriber:
        return None
    if subscriber.display_name:
        return subscriber.display_name.strip() or None
    name = " ".join(
        part for part in [subscriber.first_name, subscriber.last_name] if part
    ).strip()
    if name:
        return name
    if subscriber.email:
        return subscriber.email.strip() or None
    if subscriber.phone:
        return subscriber.phone.strip() or None
    return None


def _is_placeholder_lead_title(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized in {"website chat", "website chat lead"}


def _normalize_lead_source(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    mapped = _LEAD_SOURCE_NORMALIZED_MAP.get(candidate.lower())
    if mapped:
        return mapped
    if candidate in LEAD_SOURCE_OPTIONS:
        return candidate
    return None


def _normalize_lead_source_or_400(value: str | None) -> str | None:
    normalized = _normalize_lead_source(value)
    if value and value.strip() and not normalized:
        raise HTTPException(status_code=400, detail="Invalid lead_source")
    return normalized


def _derive_lead_source_from_attribution(attribution: dict | None) -> str | None:
    if not isinstance(attribution, dict):
        return None

    keys = (
        "source",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "referer_uri",
        "ref",
        "campaign_id",
        "ad_id",
        "adgroup_id",
        "adset_id",
    )
    values: list[str] = []
    for key in keys:
        raw = attribution.get(key)
        if raw is None:
            continue
        candidate = (raw if isinstance(raw, str) else str(raw)).strip().lower()
        if candidate:
            values.append(candidate)

    combined = " ".join(values)
    if not combined:
        return None
    if "google" in combined or "adwords" in combined or "gclid" in combined:
        return "Google"
    if "portal" in combined:
        return "Portal"
    if "instagram" in combined or "ig_" in combined or " ig " in f" {combined} ":
        return "Instagram Ads"
    if "facebook" in combined or "fb" in combined or "meta" in combined:
        return "Facebook Ads"
    if (
        "referrer" in combined
        or "referral" in combined
        or "referer" in combined
        or "ref=" in combined
    ):
        return "Referrer"
    if "website" in combined or "web" in combined:
        return "Website"
    return None


def _infer_lead_source(
    db: Session, subscriber: Subscriber | None, metadata: dict | None
) -> str | None:
    """Best-effort lead-source inference.

    Kept: attribution blobs on the lead metadata / subscriber metadata (pure
    dict inspection). Dropped until (risk #8): inference from recent
    inbound inbox messages and person channels — those models live with the
    CRM inbox and have not been ported.
    """
    metadata_attr = metadata.get("attribution") if isinstance(metadata, dict) else None
    inferred = _derive_lead_source_from_attribution(
        metadata_attr if isinstance(metadata_attr, dict) else None
    )
    if inferred:
        return inferred
    subscriber_meta = (
        subscriber.metadata_
        if subscriber is not None and isinstance(subscriber.metadata_, dict)
        else {}
    )
    subscriber_attr = (
        subscriber_meta.get("attribution")
        if isinstance(subscriber_meta, dict)
        else None
    )
    return _derive_lead_source_from_attribution(
        subscriber_attr if isinstance(subscriber_attr, dict) else None
    )


def _lead_dedup_enabled(db: Session) -> bool:
    return control_registry.is_enabled(db, "sales.lead_dedup")


def _default_currency(db: Session) -> str | None:
    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _find_open_duplicate_lead(
    db: Session,
    *,
    subscriber_id=None,
    party_id=None,
    pipeline_id=None,
):
    """The Party/account's most recent open lead in the pipeline bucket.

    New rows scope by Party; legacy unbound rows scope by Subscriber. A null
    pipeline is its own bucket, matching the partial expression indexes.
    """
    query = (
        db.query(Lead)
        .filter(Lead.is_active.is_(True))
        .filter(Lead.status.in_(_OPEN_LEAD_STATUSES))
    )
    if party_id is not None:
        query = query.filter(Lead.party_id == party_id)
    elif subscriber_id is not None:
        query = query.filter(Lead.subscriber_id == subscriber_id)
    else:
        return None
    if pipeline_id is None:
        query = query.filter(Lead.pipeline_id.is_(None))
    else:
        query = query.filter(Lead.pipeline_id == pipeline_id)
    return query.order_by(Lead.created_at.desc()).first()


def _apply_lead_closed_at(
    lead: Lead,
    status: str | None,
    *,
    previous_status: str | None = None,
) -> None:
    if status in _CLOSED_LEAD_STATUSES:
        # Stamp close time on open -> closed, or backfill if missing.
        if previous_status not in _CLOSED_LEAD_STATUSES or lead.closed_at is None:
            lead.closed_at = datetime.now(UTC)
        return

    # Clear close timestamp if a previously closed lead is reopened.
    if previous_status in _CLOSED_LEAD_STATUSES:
        lead.closed_at = None


def _uuid_from_metadata(metadata: dict | None, key: str):
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if not value:
        return None
    try:
        return coerce_uuid(str(value))
    except Exception:
        return None


def _datetime_from_metadata(metadata: dict | None, key: str) -> datetime | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _quote_owner_from_lead(db: Session, lead_id) -> uuid.UUID | None:
    """stub (risk #8).

    The CRM derived a quote's owner from the lead's owning agent
    (``CrmAgent.person_id``). Agents arrive with the inbox port;
    until then only an explicit ``owner_person_id`` (payload or metadata)
    sets quote ownership.
    """
    return None


def _prepare_quote_ownership(
    db: Session, data: dict, *, existing: Quote | None = None
) -> None:
    metadata = data.get("metadata_")
    if not isinstance(metadata, dict):
        metadata = (
            existing.metadata_
            if existing is not None and isinstance(existing.metadata_, dict)
            else None
        )

    # ``owner_person_id`` is a staff UUID carried verbatim — no
    # existence check against a people table; the staff map resolves display.
    if not data.get("owner_person_id") and (
        existing is None or not existing.owner_person_id
    ):
        owner_from_meta = _uuid_from_metadata(metadata, "owner_person_id")
        owner_from_lead = _quote_owner_from_lead(
            db, data.get("lead_id") or (existing.lead_id if existing else None)
        )
        owner_person_id = owner_from_meta or owner_from_lead
        if owner_person_id:
            data["owner_person_id"] = owner_person_id

    if data.get("sent_at") is None:
        sent_from_meta = _datetime_from_metadata(metadata, "sent_at")
        if sent_from_meta is not None:
            data["sent_at"] = sent_from_meta

    status = data.get("status")
    if (
        status == QuoteStatus.sent.value
        and data.get("sent_at") is None
        and (existing is None or existing.sent_at is None)
    ):
        data["sent_at"] = datetime.now(UTC)


def _line_amount(quantity, unit_price) -> Decimal:
    """Gross Line Item amount; new discounts apply once at Quote level."""
    qty = Decimal(quantity or 0)
    price = Decimal(unit_price or 0)
    gross = qty * price
    return max(gross, Decimal("0")).quantize(Decimal("0.01"))


def _assert_no_active_quote_discount(quote: Quote) -> None:
    from app.models.sales import QuoteDiscountType
    from app.services.sales import quote_authoring

    quote_authoring.assert_line_mutation_allowed(
        quote_id=quote.id,
        discount_type=(
            QuoteDiscountType(quote.discount_type) if quote.discount_type else None
        ),
    )


def _recalculate_quote_totals(db: Session, quote: Quote) -> None:
    db.flush()
    items = db.query(QuoteLineItem).filter(QuoteLineItem.quote_id == quote.id).all()
    # Previous Quotes may still contain immutable net Line Item amounts. New
    # Line Items are gross and the current Quote discount applies once here.
    subtotal = round_money(
        sum((Decimal(item.amount or 0) for item in items), Decimal("0.00"))
    )
    quote.subtotal = subtotal
    discounted_subtotal = round_money(
        subtotal - Decimal(quote.discount_amount or Decimal("0.00"))
    )
    # Auto-derive tax from the applied rate when one is set; otherwise keep the
    # manually entered tax_total. Configured tax follows the Quote-discounted
    # subtotal.
    if quote.tax_rate is not None:
        rate = Decimal(quote.tax_rate or 0)
        quote.tax_total = round_money(discounted_subtotal * rate / Decimal("100"))
    quote.total = discounted_subtotal + Decimal(quote.tax_total or 0)
    db.flush()


def _locked_quote_for_mutation(db: Session, quote_id: uuid.UUID) -> Quote | None:
    """Serialize every commercial mutation with Quote acceptance."""

    return db.scalars(
        select(Quote).where(Quote.id == quote_id).with_for_update()
    ).one_or_none()


def _locked_line_and_quote_for_mutation(
    db: Session,
    item_id: uuid.UUID,
) -> tuple[QuoteLineItem | None, Quote | None]:
    """Lock a line's parent Quote before the line, matching acceptance order."""

    quote_id = db.scalar(
        select(QuoteLineItem.quote_id).where(QuoteLineItem.id == item_id)
    )
    if quote_id is None:
        return None, None
    quote = _locked_quote_for_mutation(db, quote_id)
    item = db.scalars(
        select(QuoteLineItem)
        .where(
            QuoteLineItem.id == item_id,
            QuoteLineItem.quote_id == quote_id,
        )
        .with_for_update()
    ).one_or_none()
    return item, quote


#: Statuses that put a quote in front of a customer or commit the business to it.
#: Reaching either one drives real downstream effects — accepting converts the
#: party to a customer and spawns a sales order, an installation invoice and an
#: install project — so a quote must actually be worth something first.
_QUOTE_COMMITTING_STATUSES = frozenset(
    {QuoteStatus.sent.value, QuoteStatus.accepted.value}
)


def _assert_quote_is_sendable(db: Session, quote: Quote, status: str | None) -> None:
    """Refuse to send or accept a quote that has no line items.

    A quote with no lines has a zero subtotal and a zero total. Accepting one
    still runs the atomic acceptance coordinator and produces a customer, a
    sales order and an install project for no money.
    Nothing in the write path prevented that, so the admin quote form could
    create an already-``accepted`` quote and commit the business to a job worth
    nothing.

    The guard lives here, in the command service, rather than in the form: every
    caller (web, API, importer) mutates quotes through this class, and the
    invariant belongs to the domain, not to one UI.
    """
    if status not in _QUOTE_COMMITTING_STATUSES:
        return
    has_lines = (
        db.query(QuoteLineItem.id).filter(QuoteLineItem.quote_id == quote.id).first()
        is not None
    )
    if not has_lines:
        raise ValueError(
            "Add at least one line item before sending or accepting this quote — "
            "a quote with no lines is worth nothing and would still create a "
            "sales order and an install project."
        )


def _emit_lead_created(db: Session, lead: Lead) -> None:
    try:
        emit_event(
            db,
            EventType.lead_created,
            {
                "lead_id": str(lead.id),
                "status": lead.status,
                "lead_source": lead.lead_source,
                "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else None,
            },
            subscriber_id=lead.subscriber_id,
        )
    except Exception:
        _logger.warning("lead_created_event_failed lead_id=%s", lead.id, exc_info=True)


class Pipelines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        pipeline = Pipeline(**payload.model_dump())
        db.add(pipeline)
        db.commit()
        db.refresh(pipeline)
        return pipeline

    @staticmethod
    def get(db: Session, pipeline_id: str):
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        return pipeline

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Pipeline)
        if is_active is None:
            query = query.filter(Pipeline.is_active.is_(True))
        else:
            query = query.filter(Pipeline.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Pipeline.created_at, "name": Pipeline.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, pipeline_id: str, payload):
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(pipeline, key, value)
        db.commit()
        db.refresh(pipeline)
        return pipeline

    @staticmethod
    def delete(db: Session, pipeline_id: str):
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        pipeline.is_active = False
        db.commit()


class PipelineStages(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        pipeline = db.get(Pipeline, payload.pipeline_id)
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")
        stage = PipelineStage(**payload.model_dump())
        db.add(stage)
        db.commit()
        db.refresh(stage)
        return stage

    @staticmethod
    def get(db: Session, stage_id: str):
        stage = db.get(PipelineStage, coerce_uuid(stage_id))
        if not stage:
            raise HTTPException(status_code=404, detail="Pipeline stage not found")
        return stage

    @staticmethod
    def list(
        db: Session,
        pipeline_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PipelineStage)
        if pipeline_id:
            query = query.filter(PipelineStage.pipeline_id == coerce_uuid(pipeline_id))
        if is_active is None:
            query = query.filter(PipelineStage.is_active.is_(True))
        else:
            query = query.filter(PipelineStage.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "order_index": PipelineStage.order_index,
                "created_at": PipelineStage.created_at,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, stage_id: str, payload):
        stage = db.get(PipelineStage, coerce_uuid(stage_id))
        if not stage:
            raise HTTPException(status_code=404, detail="Pipeline stage not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(stage, key, value)
        db.commit()
        db.refresh(stage)
        return stage

    @staticmethod
    def reorder(
        db: Session,
        pipeline_id: uuid.UUID,
        stage_ids: tuple[uuid.UUID, ...],
    ) -> tuple[uuid.UUID, ...]:
        """Atomically apply one complete, stale-safe stage order."""

        if len(stage_ids) != len(set(stage_ids)):
            raise HTTPException(
                status_code=400,
                detail="Stage order contains duplicate stage identifiers",
            )
        stages = (
            db.query(PipelineStage)
            .filter(PipelineStage.pipeline_id == pipeline_id)
            .with_for_update()
            .all()
        )
        stage_map = {stage.id: stage for stage in stages}
        if set(stage_map) != set(stage_ids):
            raise HTTPException(
                status_code=409,
                detail="Stage order is stale; reload Pipeline Settings and try again",
            )
        for order_index, stage_id in enumerate(stage_ids):
            stage_map[stage_id].order_index = order_index
        db.commit()
        return stage_ids


def stage_lead_maintenance(db: Session, command: LeadMaintenanceUpdate) -> Lead:
    """Stage explicit Lead maintenance values without completing a transaction."""

    lead = db.scalars(
        select(Lead).where(Lead.id == command.lead_id).with_for_update()
    ).one_or_none()
    if lead is None:
        raise DomainError(
            code="sales.service.lead_not_found",
            message="Lead not found.",
            details={"lead_id": str(command.lead_id)},
        )
    clean_title = command.title.strip()
    if not clean_title or len(clean_title) > 200:
        raise DomainError(
            code="sales.service.lead_title_invalid",
            message="Lead Name is required and must be 200 characters or fewer.",
            details={"field": "title"},
        )
    if command.status == LeadStatus.won and lead.status != LeadStatus.won.value:
        raise DomainError(
            code="sales.service.lead_won_transition_forbidden",
            message="A Lead becomes Won only through Quote acceptance.",
            details={"field": "status"},
        )
    if (
        lead.origin_capture is not None
        and command.lead_source != lead.origin_capture.lead_source
    ):
        raise DomainError(
            code="sales.service.lead_origin_immutable",
            message="Lead Source is fixed by the captured Lead origin.",
            details={"field": "lead_source"},
        )
    if lead.subscriber_id is not None and command.reseller_id != lead.reseller_id:
        raise DomainError(
            code="sales.service.converted_lead_reseller_immutable",
            message="Reseller ownership cannot change after account conversion.",
            details={"field": "reseller_id"},
        )

    previous_status = lead.status
    lead.title = clean_title
    lead.status = command.status.value
    lead.owner_agent_id = command.owner_agent_id
    lead.pipeline_id = command.pipeline_id
    lead.stage_id = command.stage_id
    lead.lead_source = command.lead_source
    lead.region = command.region
    lead.estimated_value = command.estimated_value
    lead.currency = command.currency
    lead.address = command.address
    lead.probability = command.probability
    lead.expected_close_date = command.expected_close_date
    lead.lost_reason = command.lost_reason
    lead.notes = command.notes
    lead.is_active = command.is_active
    lead.reseller_id = command.reseller_id
    _apply_lead_closed_at(lead, lead.status, previous_status=previous_status)
    metadata = dict(lead.metadata_) if isinstance(lead.metadata_, dict) else {}
    metadata["last_edit_key"] = str(command.edit_key)
    metadata["last_edit_fingerprint"] = command.edit_fingerprint
    metadata["organization_id"] = (
        str(command.organization_id) if command.organization_id else None
    )
    metadata["reseller_id"] = str(command.reseller_id) if command.reseller_id else None
    metadata["region_zone_id"] = (
        str(command.region_zone_id) if command.region_zone_id else None
    )
    metadata["communication_routed_through_reseller"] = command.reseller_routed
    lead.metadata_ = metadata
    db.flush()
    return lead


class Leads(ListResponseMixin):
    @staticmethod
    def query(db: Session, request: LeadListQueryInput) -> LeadListQueryResult:
        """Return one filtered Lead page from the typed authoritative query."""

        normalized = _normalize_lead_list_query(db, request)
        predicates = _lead_list_predicates(_lead_list_filters(normalized))
        total_count = int(
            db.query(func.count(Lead.id)).filter(*predicates).scalar() or 0
        )
        total_pages = max(
            1,
            (total_count + normalized.page_size - 1) // normalized.page_size,
        )
        if normalized.page > total_pages:
            normalized = replace(normalized, page=total_pages)

        order_columns = {
            LeadListSortField.CREATED_AT.value: Lead.created_at,
            LeadListSortField.UPDATED_AT.value: Lead.updated_at,
        }
        rows_query = db.query(Lead).filter(*predicates)
        rows_query = apply_ordering(
            rows_query,
            normalized.sort_field.value,
            normalized.sort_direction.value,
            order_columns,
        ).order_by(Lead.id.asc())
        items = tuple(
            apply_pagination(
                rows_query,
                normalized.page_size,
                normalized.offset,
            ).all()
        )
        return LeadListQueryResult(
            items=items,
            total_count=total_count,
            query=normalized,
            summary=_lead_summary_for_predicates(db, predicates),
        )

    @staticmethod
    def create(db: Session, payload):
        data = payload.model_dump()
        origin_capture = data.pop("origin_capture", None)
        explicit_party_id = data.pop("party_id", None)
        party_binding_source = data.pop("party_binding_source", None)
        party_binding_reason = data.pop("party_binding_reason", None)
        if data.get("status"):
            data["status"] = _enum_str(data["status"], LeadStatus, "status")
        if data.get("status") == LeadStatus.won.value:
            raise HTTPException(
                status_code=409,
                detail="A Lead becomes Won only through Quote acceptance",
            )
        if "lead_source" in data:
            data["lead_source"] = _normalize_lead_source_or_400(data.get("lead_source"))
        data["pipeline_id"] = _validate_lead_pipeline_stage(
            db,
            pipeline_id=data.get("pipeline_id"),
            stage_id=data.get("stage_id"),
        )

        legacy_campaign_id = data.get("campaign_id")
        legacy_recipient_id = data.get("campaign_recipient_id")
        if origin_capture is None and (legacy_campaign_id or legacy_recipient_id):
            raise HTTPException(
                status_code=400,
                detail="Campaign attribution requires origin_capture evidence",
            )
        if origin_capture is not None:
            origin_capture = dict(origin_capture)
            for field, legacy_value in (
                ("campaign_id", legacy_campaign_id),
                ("campaign_recipient_id", legacy_recipient_id),
            ):
                captured_value = origin_capture.get(field)
                if legacy_value and captured_value and legacy_value != captured_value:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field} conflicts with origin_capture",
                    )
                if legacy_value and not captured_value:
                    origin_capture[field] = legacy_value
            # Canonical capture writes these compatibility projections.
            data["campaign_id"] = None
            data["campaign_recipient_id"] = None

        subscriber_id = data.get("subscriber_id")
        if not subscriber_id and not explicit_party_id:
            raise HTTPException(
                status_code=400,
                detail="party_id or subscriber_id is required",
            )

        subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None
        if subscriber_id and not subscriber:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        resolved_party_id = explicit_party_id or (
            subscriber.party_id if subscriber is not None else None
        )
        if (
            explicit_party_id
            and subscriber is not None
            and subscriber.party_id is not None
            and subscriber.party_id != explicit_party_id
        ):
            raise HTTPException(
                status_code=400,
                detail="party_id does not match the Subscriber Party",
            )
        if explicit_party_id and not (
            str(party_binding_source or "").strip()
            and str(party_binding_reason or "").strip()
        ):
            raise HTTPException(
                status_code=400,
                detail="Explicit party_id requires binding source and reason",
            )

        # Dedup: a subscriber shouldn't have two open leads. If one exists,
        # return it (idempotent) instead of creating a duplicate pipeline
        # entry. Scoped to the requested pipeline when one is given.
        dedup_enabled = _lead_dedup_enabled(db)
        if dedup_enabled:
            duplicate = _find_open_duplicate_lead(
                db,
                subscriber_id=subscriber_id,
                party_id=resolved_party_id,
                pipeline_id=data.get("pipeline_id"),
            )
            if duplicate is not None:
                if origin_capture is not None:
                    if not duplicate.party_id or not duplicate.lead_source:
                        raise HTTPException(
                            status_code=409,
                            detail="Existing lead lacks canonical origin context",
                        )
                    try:
                        lead_lifecycle.capture_lead_origin(
                            db,
                            lead_id=duplicate.id,
                            lead_source=duplicate.lead_source,
                            capture=origin_capture,
                        )
                    except lead_lifecycle.LeadLifecycleError as exc:
                        db.rollback()
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
                metadata = dict(duplicate.metadata_ or {})
                metadata["dedup_hits"] = int(metadata.get("dedup_hits") or 0) + 1
                duplicate.metadata_ = metadata
                db.commit()
                db.refresh(duplicate)
                _logger.info(
                    "lead_dedup_returned_existing subscriber_id=%s lead_id=%s",
                    subscriber_id,
                    duplicate.id,
                )
                # Transient signal for callers (e.g. web route) to distinguish
                # a deduped return from a freshly created lead. Not persisted.
                duplicate.dedup_returned_existing = True
                return duplicate

        title_value = data.get("title")
        if (
            not title_value
            or (isinstance(title_value, str) and not title_value.strip())
            or _is_placeholder_lead_title(title_value)
        ):
            data["title"] = _lead_title_from_subscriber(subscriber)
            if not data["title"] and resolved_party_id:
                party = db.get(Party, resolved_party_id)
                data["title"] = party.display_name if party is not None else None

        if not data.get("owner_agent_id"):
            data["owner_agent_id"] = _resolve_owner_agent_id(db, subscriber_id)
        if not data.get("currency"):
            default_currency = _default_currency(db)
            if default_currency:
                data["currency"] = default_currency
        if not data.get("lead_source"):
            data["lead_source"] = _infer_lead_source(
                db, subscriber, data.get("metadata_")
            )
        if origin_capture is not None and not data.get("lead_source"):
            raise HTTPException(
                status_code=400,
                detail="origin_capture requires a recognized lead_source",
            )
        lead = Lead(**data)
        _apply_lead_closed_at(lead, lead.status)
        if resolved_party_id is not None:
            binding_source = (
                party_binding_source
                if explicit_party_id
                else "subscriber_party_projection"
            )
            binding_reason = (
                party_binding_reason
                if explicit_party_id
                else "Lead created for an already reviewed Subscriber Party"
            )
            try:
                lead_lifecycle.initialize_lead_party(
                    db,
                    lead=lead,
                    party_id=resolved_party_id,
                    source=binding_source,
                    reason=binding_reason,
                )
            except lead_lifecycle.LeadLifecycleError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.add(lead)
        try:
            db.flush()
            if resolved_party_id is not None and subscriber is not None:
                lead_lifecycle.attach_lead_subscriber(
                    db,
                    lead_id=lead.id,
                    subscriber_id=subscriber.id,
                    source="lead_create_subscriber_context",
                    reason="Subscriber supplied when the Party-bound Lead was created",
                )
            if origin_capture is not None:
                lead_lifecycle.capture_lead_origin(
                    db,
                    lead_id=lead.id,
                    lead_source=data["lead_source"],
                    capture=origin_capture,
                )
            _emit_lead_created(db, lead)
            db.commit()
        except lead_lifecycle.LeadLifecycleError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except IntegrityError:
            # A concurrent create won the partial unique index
            # (uq_leads_one_open_per_subscriber_pipeline). Resolve the race by
            # returning the existing open lead instead of surfacing a 500.
            db.rollback()
            if dedup_enabled:
                existing = _find_open_duplicate_lead(
                    db,
                    subscriber_id=subscriber_id,
                    party_id=resolved_party_id,
                    pipeline_id=data.get("pipeline_id"),
                )
                if existing is not None:
                    _logger.info(
                        "lead_dedup_race_resolved subscriber_id=%s lead_id=%s",
                        subscriber_id,
                        existing.id,
                    )
                    existing.dedup_returned_existing = True
                    return existing
            raise
        db.refresh(lead)
        return lead

    @staticmethod
    def get(db: Session, lead_id: str):
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        return lead

    @staticmethod
    def list(
        db: Session,
        pipeline_id: str | None,
        stage_id: str | None,
        owner_agent_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        lead_source: str | None = None,
        search: str | None = None,
    ):
        filters = _LeadListFilters(
            search_term=normalize_lead_search(search),
            status=_enum_str(status, LeadStatus, "status") if status else None,
            pipeline_id=coerce_uuid(pipeline_id) if pipeline_id else None,
            stage_id=coerce_uuid(stage_id) if stage_id else None,
            owner_agent_id=coerce_uuid(owner_agent_id) if owner_agent_id else None,
            lead_source=lead_source.strip() if lead_source else None,
            is_active=True if is_active is None else is_active,
        )
        query = db.query(Lead).filter(*_lead_list_predicates(filters))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Lead.created_at, "updated_at": Lead.updated_at},
        ).order_by(Lead.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def count(
        db: Session,
        *,
        pipeline_id: str | None,
        stage_id: str | None,
        owner_agent_id: str | None,
        status: str | None,
        lead_source: str | None,
        search: str | None,
        is_active: bool | None = None,
    ) -> int:
        filters = _LeadListFilters(
            search_term=normalize_lead_search(search),
            status=_enum_str(status, LeadStatus, "status") if status else None,
            pipeline_id=coerce_uuid(pipeline_id) if pipeline_id else None,
            stage_id=coerce_uuid(stage_id) if stage_id else None,
            owner_agent_id=coerce_uuid(owner_agent_id) if owner_agent_id else None,
            lead_source=lead_source.strip() if lead_source else None,
            is_active=True if is_active is None else is_active,
        )
        return int(
            db.query(func.count(Lead.id))
            .filter(*_lead_list_predicates(filters))
            .scalar()
            or 0
        )

    @staticmethod
    def summary(db: Session) -> LeadPipelineSummary:
        """Return CRM-compatible KPI values without UI-side derivation."""

        predicates = _lead_list_predicates(
            _LeadListFilters(
                search_term=None,
                status=None,
                pipeline_id=None,
                stage_id=None,
                owner_agent_id=None,
                lead_source=None,
                is_active=True,
            )
        )
        return _lead_summary_for_predicates(db, predicates)

    @staticmethod
    def update(db: Session, lead_id: str, payload):
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        previous_status = lead.status
        data = payload.model_dump(exclude_unset=True)
        if "status" in data:
            data["status"] = _enum_str(data["status"], LeadStatus, "status")
            if (
                data["status"] == LeadStatus.won.value
                and lead.status != LeadStatus.won.value
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A Lead becomes Won only through Quote acceptance",
                )
        if "lead_source" in data:
            data["lead_source"] = _normalize_lead_source_or_400(data.get("lead_source"))
        prospective_stage_id = data["stage_id"] if "stage_id" in data else lead.stage_id
        prospective_pipeline_id = (
            data["pipeline_id"] if "pipeline_id" in data else lead.pipeline_id
        )
        resolved_pipeline_id = _validate_lead_pipeline_stage(
            db,
            pipeline_id=prospective_pipeline_id,
            stage_id=prospective_stage_id,
        )
        if prospective_stage_id is not None:
            data["pipeline_id"] = resolved_pipeline_id

        if "lead_source" in data and lead.origin_capture is not None:
            if data["lead_source"] != lead.origin_capture.lead_source:
                raise HTTPException(
                    status_code=409,
                    detail="Lead origin is immutable; lead_source cannot be changed",
                )

        if "subscriber_id" in data:
            raise HTTPException(
                status_code=409,
                detail="Subscriber conversion is owned by Quote acceptance",
            )
        subscriber = lead.subscriber

        if "title" in data:
            title_value = data.get("title")
            if (
                not title_value
                or (isinstance(title_value, str) and not title_value.strip())
                or _is_placeholder_lead_title(title_value)
            ):
                data["title"] = (
                    _lead_title_from_subscriber(subscriber) if subscriber else None
                )

        for key, value in data.items():
            setattr(lead, key, value)

        if "status" in data:
            if lead.owner_agent_id is None and lead.status in _CLOSED_LEAD_STATUSES:
                lead.owner_agent_id = _resolve_owner_agent_id(db, lead.subscriber_id)
            _apply_lead_closed_at(lead, lead.status, previous_status=previous_status)

        db.commit()
        db.refresh(lead)
        return lead

    @staticmethod
    def delete(db: Session, lead_id: str):
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.is_active = False
        db.commit()

    @staticmethod
    def kanban_view(db: Session, pipeline_id: str | None = None) -> dict:
        """Return kanban board data with columns and records.

        Returns:
            dict with 'columns' (list of stage info) and 'records' (leads).
        """
        if pipeline_id:
            stages = (
                db.query(PipelineStage)
                .filter(PipelineStage.pipeline_id == coerce_uuid(pipeline_id))
                .filter(PipelineStage.is_active.is_(True))
                .order_by(PipelineStage.order_index.asc())
                .all()
            )
            leads_rows = (
                db.query(Lead)
                .filter(Lead.pipeline_id == coerce_uuid(pipeline_id))
                .filter(Lead.is_active.is_(True))
                .all()
            )
        else:
            stages = (
                db.query(PipelineStage)
                .filter(PipelineStage.is_active.is_(True))
                .order_by(PipelineStage.order_index.asc())
                .all()
            )
            leads_rows = db.query(Lead).filter(Lead.is_active.is_(True)).all()

        columns = []
        for stage in stages:
            presentation = pipeline_configuration.stage_presentation(
                stage_name=stage.name,
                metadata=stage.metadata_,
            )
            columns.append(
                {
                    "id": str(stage.id),
                    "title": stage.name,
                    "order_index": stage.order_index,
                    "default_probability": stage.default_probability,
                    "stage_type": presentation.stage_type.value,
                    "color": presentation.color,
                    "icon": presentation.icon,
                }
            )

        # Batch load subscribers to avoid N+1 queries.
        subscriber_ids = [
            lead.subscriber_id for lead in leads_rows if lead.subscriber_id
        ]
        subscribers = (
            db.query(Subscriber).filter(Subscriber.id.in_(subscriber_ids)).all()
            if subscriber_ids
            else []
        )
        subscriber_map = {s.id: s for s in subscribers}

        records = []
        for lead in leads_rows:
            subscriber = (
                subscriber_map.get(lead.subscriber_id) if lead.subscriber_id else None
            )
            contact_name = ""
            if subscriber:
                contact_name = (
                    subscriber.display_name
                    or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
                )

            records.append(
                {
                    "id": str(lead.id),
                    "stage": str(lead.stage_id) if lead.stage_id else None,
                    "title": lead.title or f"Lead #{str(lead.id)[:8]}",
                    "contact_name": contact_name,
                    "estimated_value": float(lead.estimated_value)
                    if lead.estimated_value
                    else None,
                    "probability": lead.probability,
                    "weighted_value": float(lead.weighted_value)
                    if lead.weighted_value
                    else None,
                    "status": lead.status or LeadStatus.new.value,
                    "currency": lead.currency or "",
                    # Kanban cards deep-link into the sub admin surface
                    # (this PR's /admin/sales pages).
                    "url": f"/admin/sales/leads/{lead.id}",
                }
            )

        return {"columns": columns, "records": records}

    @staticmethod
    def update_stage(db: Session, lead_id: str, new_stage_id: str) -> dict:
        """Move a lead to a new stage, defaulting probability from the stage.

        Returns:
            dict with updated lead info.
        """
        lead = db.get(Lead, coerce_uuid(lead_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        stage = db.get(PipelineStage, coerce_uuid(new_stage_id))
        if not stage:
            raise HTTPException(status_code=404, detail="Stage not found")

        lead.stage_id = stage.id
        lead.pipeline_id = stage.pipeline_id

        # Auto-update probability from stage default if not already set.
        if lead.probability is None:
            lead.probability = stage.default_probability

        db.commit()
        db.refresh(lead)

        return {
            "id": str(lead.id),
            "stage_id": str(lead.stage_id),
            "pipeline_id": str(lead.pipeline_id) if lead.pipeline_id else None,
            "probability": lead.probability,
        }

    @staticmethod
    def bulk_assign_pipeline(
        db: Session,
        pipeline_id: str,
        stage_id: str | None = None,
        *,
        scope: str = "unassigned",
    ) -> int:
        pipeline = db.get(Pipeline, coerce_uuid(pipeline_id))
        if not pipeline:
            raise HTTPException(status_code=404, detail="Pipeline not found")

        resolved_stage_id = None
        if stage_id:
            stage = db.get(PipelineStage, coerce_uuid(stage_id))
            if not stage or stage.pipeline_id != pipeline.id:
                raise HTTPException(
                    status_code=400,
                    detail="Selected stage does not belong to this pipeline",
                )
            resolved_stage_id = stage.id

        query = db.query(Lead).filter(Lead.is_active.is_(True))
        if scope == "unassigned":
            query = query.filter(Lead.pipeline_id.is_(None))
        elif scope != "all_active":
            raise HTTPException(status_code=400, detail="Unsupported bulk assign scope")

        count = query.update(
            {
                Lead.pipeline_id: pipeline.id,
                Lead.stage_id: resolved_stage_id,
            },
            synchronize_session=False,
        )
        db.commit()
        return int(count)


class Quotes(ListResponseMixin):
    @staticmethod
    def query(db: Session, request: QuoteListQueryInput) -> QuoteListQueryResult:
        """Return one Quote page from the shared, read-only query specification."""

        normalized = _normalize_quote_list_query(db, request)
        predicates = _quote_list_predicates(_quote_list_filters(normalized))
        total_count = int(
            db.query(func.count(Quote.id)).filter(*predicates).scalar() or 0
        )
        total_pages = max(
            1,
            (total_count + normalized.page_size - 1) // normalized.page_size,
        )
        if normalized.page > total_pages:
            normalized = replace(normalized, page=total_pages)

        order_columns = {
            QuoteListSortField.CREATED_AT.value: Quote.created_at,
            QuoteListSortField.UPDATED_AT.value: Quote.updated_at,
        }
        rows_query = db.query(Quote).filter(*predicates)
        rows_query = apply_ordering(
            rows_query,
            normalized.sort_field.value,
            normalized.sort_direction.value,
            order_columns,
        ).order_by(Quote.id.asc())
        items = tuple(
            apply_pagination(
                rows_query,
                normalized.page_size,
                normalized.offset,
            ).all()
        )
        return QuoteListQueryResult(
            items=items,
            total_count=total_count,
            query=normalized,
        )

    @staticmethod
    def create(
        db: Session,
        payload: QuoteCreate,
        *,
        context: CommandContext | None = None,
    ) -> Quote:
        data = payload.model_dump()
        if data.get("status"):
            data["status"] = _enum_str(data["status"], QuoteStatus, "status")
        if data.get("project_type") is None:
            raise HTTPException(status_code=400, detail="project_type is required")
        data["project_type"] = _enum_str(
            data["project_type"], ProjectType, "project_type"
        )

        lead_id = data.get("lead_id")
        if lead_id is None:
            raise HTTPException(status_code=400, detail="lead_id is required")
        lead = db.get(Lead, lead_id)
        if lead is None or not lead.is_active:
            raise HTTPException(status_code=404, detail="Lead not found")

        subscriber_id = data.get("subscriber_id")
        subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None
        if subscriber_id and subscriber is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        if subscriber is not None:
            try:
                lead_lifecycle.validate_lead_subscriber_alignment(
                    db, lead=lead, subscriber=subscriber
                )
            except lead_lifecycle.LeadLifecycleError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        # Human label remains a projection; Lead is the required relationship.
        if not data.get("metadata_"):
            data["metadata_"] = {}
        if isinstance(data["metadata_"], dict):
            display_name = (
                lead.party.display_name
                if lead.party is not None
                else (
                    subscriber.display_name
                    or f"{subscriber.first_name} {subscriber.last_name}"
                    if subscriber is not None
                    else lead.title
                )
            )
            data["metadata_"]["quote_name"] = display_name

        _prepare_quote_ownership(db, data)

        # A brand-new quote has no line items yet -- they are added afterwards --
        # so it can never legitimately start out sent or accepted. Reject before
        # the insert, otherwise the row commits and we are left with an orphaned
        # accepted quote that has already fired the fulfilment pipeline.
        if data.get("status") in _QUOTE_COMMITTING_STATUSES:
            raise ValueError(
                "A new quote starts as a draft. Add line items, then send or accept it."
            )

        if not data.get("currency"):
            default_currency = _default_currency(db)
            if default_currency:
                data["currency"] = default_currency
        quote = Quote(**data)
        db.add(quote)
        db.flush()
        _stage_quote_audit(
            db,
            action="quote.created",
            quote_id=quote.id,
            context=context,
            metadata={"lead_id": str(quote.lead_id), "status": quote.status},
        )
        db.commit()
        db.refresh(quote)
        return quote

    @staticmethod
    def get(db: Session, quote_id: str):
        quote = db.get(
            Quote,
            coerce_uuid(quote_id),
            options=[selectinload(Quote.line_items)],
        )
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        return quote

    @staticmethod
    def list(
        db: Session,
        lead_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        search: str | None = None,
    ):
        filters = _QuoteListFilters(
            search_term=normalize_quote_search(search),
            status=_enum_str(status, QuoteStatus, "status") if status else None,
            lead_id=coerce_uuid(lead_id) if lead_id else None,
            is_active=True if is_active is None else is_active,
        )
        query = db.query(Quote).filter(*_quote_list_predicates(filters))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Quote.created_at, "updated_at": Quote.updated_at},
        ).order_by(Quote.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def count_by_status(db: Session) -> dict:
        """Return counts by quote status."""
        results = (
            db.query(Quote.status, func.count(Quote.id))
            .filter(Quote.is_active.is_(True))
            .group_by(Quote.status)
            .all()
        )
        counts = {s.value: 0 for s in QuoteStatus}
        for status_val, count in results:
            if status_val:
                counts[str(status_val)] = count
        counts["total"] = sum(v for key, v in counts.items() if key != "total")
        return counts

    @staticmethod
    def update(
        db: Session,
        quote_id: str,
        payload: QuoteUpdate,
        *,
        context: CommandContext | None = None,
    ) -> Quote:
        from app.services.sales import quote_acceptance

        quote_uuid = coerce_uuid(quote_id)
        requested = payload.model_dump(exclude_unset=True)
        quote = _locked_quote_for_mutation(db, quote_uuid)
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        previous_status = quote.status
        requested_status = (
            _enum_str(requested.get("status"), QuoteStatus, "status")
            if "status" in requested
            else None
        )
        accepting = requested_status == QuoteStatus.accepted.value
        if (
            previous_status == QuoteStatus.accepted.value
            and not accepting
            and requested
        ):
            quote_acceptance.assert_quote_mutable(
                quote,
                mutation="quote_fields",
            )
        if previous_status == QuoteStatus.accepted.value and not accepting:
            db_session_adapter.release_read_transaction(db)
            return Quotes.get(db, str(quote_uuid))
        if "project_type" in requested:
            if requested["project_type"] is None:
                raise HTTPException(
                    status_code=400, detail="project_type cannot be cleared"
                )
            requested["project_type"] = _enum_str(
                requested["project_type"], ProjectType, "project_type"
            )
        if accepting:
            requested.pop("status", None)
        if accepting:
            changed_fields = tuple(
                key for key, value in requested.items() if getattr(quote, key) != value
            )
            if changed_fields:
                quote_acceptance.assert_quote_mutable(
                    quote,
                    mutation="quote_fields",
                )
                raise HTTPException(
                    status_code=409,
                    detail="Save Quote edits before accepting it",
                )
            db_session_adapter.release_read_transaction(db)
            quote_acceptance.accept_quote(
                db,
                quote_acceptance.AcceptQuoteCommand(
                    context=context
                    or CommandContext.system(
                        actor="sales.quote-update-adapter",
                        scope="sales:quote-acceptance",
                        reason="Accept Quote and convert Lead",
                        idempotency_key=f"quote-acceptance:{quote_uuid}",
                    ),
                    quote_id=quote_uuid,
                ),
            )
            return Quotes.get(db, str(quote_uuid))
        data = requested
        if "status" in data:
            data["status"] = _enum_str(data["status"], QuoteStatus, "status")
        if "subscriber_id" in data:
            raise HTTPException(
                status_code=409,
                detail="Subscriber conversion is owned by Quote acceptance",
            )

        prospective_subscriber_id = data.get("subscriber_id", quote.subscriber_id)
        subscriber = (
            db.get(Subscriber, prospective_subscriber_id)
            if prospective_subscriber_id is not None
            else None
        )
        if prospective_subscriber_id is not None and subscriber is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        prospective_lead_id = data["lead_id"] if "lead_id" in data else quote.lead_id
        if prospective_lead_id is None:
            raise HTTPException(status_code=400, detail="lead_id is required")
        lead = db.get(Lead, prospective_lead_id)
        if lead is None or not lead.is_active:
            raise HTTPException(status_code=404, detail="Lead not found")
        if subscriber is not None:
            try:
                lead_lifecycle.validate_lead_subscriber_alignment(
                    db, lead=lead, subscriber=subscriber
                )
            except lead_lifecycle.LeadLifecycleError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        _prepare_quote_ownership(db, data, existing=quote)

        # Check before mutating: a rejected transition must leave the quote
        # exactly as it was, not half-applied.
        status_to_validate = (
            QuoteStatus.accepted.value if accepting else data.get("status")
        )
        if status_to_validate != previous_status:
            _assert_quote_is_sendable(db, quote, status_to_validate)

        changes = {
            key: {"from": str(getattr(quote, key)), "to": str(value)}
            for key, value in data.items()
            if str(getattr(quote, key)) != str(value)
        }
        for key, value in data.items():
            setattr(quote, key, value)

        if "tax_rate" in data:
            _recalculate_quote_totals(db, quote)
        if changes:
            _stage_quote_audit(
                db,
                action=(
                    "quote.status_changed"
                    if set(changes) == {"status"}
                    else "quote.updated"
                ),
                quote_id=quote.id,
                context=context,
                metadata={"changes": changes},
            )
        db.commit()
        db.refresh(quote)
        return quote

    @staticmethod
    def delete(
        db: Session,
        quote_id: str,
        *,
        context: CommandContext | None = None,
    ) -> None:
        from app.services.sales import quote_acceptance

        quote = _locked_quote_for_mutation(db, coerce_uuid(quote_id))
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        quote_acceptance.assert_quote_mutable(
            quote,
            mutation="quote_deactivation",
        )
        quote.is_active = False
        _stage_quote_audit(
            db,
            action="quote.deactivated",
            quote_id=quote.id,
            context=context,
        )
        db.commit()


class QuoteLineItems(ListResponseMixin):
    @staticmethod
    def create(
        db: Session,
        payload: QuoteLineItemCreate,
        *,
        context: CommandContext | None = None,
    ) -> QuoteLineItem:
        from app.services.sales import quote_acceptance

        quote = _locked_quote_for_mutation(db, payload.quote_id)
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        quote_acceptance.assert_quote_mutable(
            quote,
            mutation="line_item_create",
        )
        _assert_no_active_quote_discount(quote)
        data = payload.model_dump()
        # ``inventory_item_id`` is a CRM inventory UUID carried verbatim —
        # inventory is so there is nothing to validate against.
        # Always derive gross amount server-side. The retained database column
        # is zero for every new Line Item and exists only for previous Quotes.
        data["discount_percent"] = Decimal("0.00")
        data["amount"] = _line_amount(data.get("quantity"), data.get("unit_price"))
        item = QuoteLineItem(**data)
        db.add(item)
        _recalculate_quote_totals(db, quote)
        _stage_quote_audit(
            db,
            action="quote.line_added",
            quote_id=quote.id,
            context=context,
            metadata={
                "line_item_id": str(item.id),
                "description": item.description,
            },
        )
        db.commit()
        db.refresh(item)
        return item

    @staticmethod
    def update(
        db: Session,
        item_id: str,
        payload: QuoteLineItemUpdate,
        *,
        context: CommandContext | None = None,
    ) -> QuoteLineItem:
        from app.services.sales import quote_acceptance

        item, quote = _locked_line_and_quote_for_mutation(
            db,
            coerce_uuid(item_id),
        )
        if not item:
            raise HTTPException(status_code=404, detail="Quote line item not found")
        assert quote is not None
        quote_acceptance.assert_quote_mutable(
            quote,
            mutation="line_item_update",
        )
        _assert_no_active_quote_discount(quote)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(item, key, value)
        if {"quantity", "unit_price"} & set(data):
            item.amount = _line_amount(item.quantity, item.unit_price)
        _recalculate_quote_totals(db, quote)
        _stage_quote_audit(
            db,
            action="quote.line_updated",
            quote_id=quote.id,
            context=context,
            metadata={"line_item_id": str(item.id)},
        )
        db.commit()
        db.refresh(item)
        return item

    @staticmethod
    def delete(
        db: Session,
        item_id: str,
        *,
        context: CommandContext | None = None,
    ) -> None:
        """Remove a line and re-derive the quote's money from what is left.

        A hard delete is right here: a line item has no history of its own, and
        leaving a soft-deleted row behind would keep it in the subtotal.
        """
        from app.services.sales import quote_acceptance

        item, quote = _locked_line_and_quote_for_mutation(
            db,
            coerce_uuid(item_id),
        )
        if not item:
            raise HTTPException(status_code=404, detail="Quote line item not found")
        assert quote is not None
        quote_acceptance.assert_quote_mutable(
            quote,
            mutation="line_item_delete",
        )
        _assert_no_active_quote_discount(quote)
        line_id = item.id
        description = item.description
        db.delete(item)
        _recalculate_quote_totals(db, quote)
        _stage_quote_audit(
            db,
            action="quote.line_removed",
            quote_id=quote.id,
            context=context,
            metadata={
                "line_item_id": str(line_id),
                "description": description,
            },
        )
        db.commit()

    @staticmethod
    def list(
        db: Session,
        quote_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(QuoteLineItem)
        if quote_id:
            query = query.filter(QuoteLineItem.quote_id == coerce_uuid(quote_id))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": QuoteLineItem.created_at},
        )
        return apply_pagination(query, limit, offset).all()


# Singleton instances
pipelines = Pipelines()
pipeline_stages = PipelineStages()
leads = Leads()
quotes = Quotes()
quote_line_items = QuoteLineItems()
