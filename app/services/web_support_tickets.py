"""Web helpers for admin support ticket routes."""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.project import ProjectTask
from app.models.subscriber import Subscriber
from app.models.support import (
    Ticket,
    TicketChannel,
    TicketCommentAuthorType,
    TicketStatus,
)
from app.schemas.support import (
    AttachmentMeta,
    TicketCommentCreate,
    TicketCommentUpdate,
    TicketCreate,
    TicketLinkCreate,
    TicketMergeRequest,
    TicketUpdate,
)
from app.services import (
    service_address,
    sla_assignment,
    support_ticket_filters,
    support_ticket_region_projection,
    ticket_mentions,
    ticket_validation,
)
from app.services import support as support_service
from app.services import support_ticket_settings as support_ticket_settings_service
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.file_storage import file_uploads
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
    SortDirection,
)
from app.services.status_presentation import ticket_status_presentation

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TicketCustomerContext:
    name: str
    email: str | None
    phone: str | None
    service_address: str | None
    subscriber_id: UUID


def _ticket_customer_context(
    db: Session, subscriber_id: UUID | None
) -> TicketCustomerContext | None:
    if subscriber_id is None:
        return None
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        return None
    name = (
        subscriber.company_name
        or subscriber.display_name
        or " ".join(
            part for part in (subscriber.first_name, subscriber.last_name) if part
        ).strip()
        or subscriber.account_number
        or str(subscriber.id)
    )
    return TicketCustomerContext(
        name=name,
        email=subscriber.email,
        phone=subscriber.phone,
        service_address=service_address.format_address(
            service_address.service_address(db, subscriber.id)
        ),
        subscriber_id=subscriber.id,
    )


class WebSupportTicketInputError(DomainError):
    """Transport-neutral validation error from the admin Ticket adapter service."""


ALLOWED_ATTACHMENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
}
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
DEFAULT_VISIBLE_COLUMNS = [
    "number",
    "ticket_type",
    "priority",
    "status",
    "customer",
    "technician",
    "due_at",
    "created_at",
]
TICKET_COLUMNS = [
    {"key": "number", "label": "Ticket ID"},
    {"key": "ticket_type", "label": "Ticket Type"},
    {"key": "priority", "label": "Priority"},
    {"key": "status", "label": "Status"},
    {"key": "customer", "label": "Customer Name"},
    {"key": "customer_id", "label": "Customer ID"},
    {"key": "subscriber", "label": "Subscriber"},
    {"key": "region", "label": "Region"},
    {"key": "technician", "label": "Assigned Technician"},
    {"key": "project_manager", "label": "Project Manager"},
    {"key": "site_coordinator", "label": "Site Coordinator"},
    {"key": "channel", "label": "Channel"},
    {"key": "due_at", "label": "Due Date"},
    {"key": "created_at", "label": "Opening Date"},
]

SUPPORT_TICKET_LIST_DEFINITION = ListDefinition(
    key="support_tickets",
    fields=(
        ListFieldDefinition("number", "Ticket", searchable=True, sortable=True),
        ListFieldDefinition("title", "Title", searchable=True),
        ListFieldDefinition("description", "Description", searchable=True),
        ListFieldDefinition("status", "Status", filterable=True, sortable=True),
        ListFieldDefinition("ticket_type", "Ticket type", filterable=True),
        ListFieldDefinition("region", "Region", filterable=True),
        ListFieldDefinition("assigned_to_me", "Assigned to me", filterable=True),
        ListFieldDefinition(
            "project_manager_person_id", "Project manager", filterable=True
        ),
        ListFieldDefinition(
            "site_coordinator_person_id", "Site coordinator", filterable=True
        ),
        ListFieldDefinition("subscriber_id", "Subscriber", filterable=True),
        ListFieldDefinition("filters", "Advanced filters", filterable=True),
        ListFieldDefinition("priority", "Priority", sortable=True),
        ListFieldDefinition("due_at", "Due", sortable=True),
        ListFieldDefinition("updated_at", "Updated", sortable=True),
        ListFieldDefinition("created_at", "Opened", sortable=True),
    ),
    default_sort="created_at",
    default_sort_dir="desc",
)

NOT_CLOSED_TICKET_STATUS_FILTER = "not_closed"
_TICKET_STATUS_FILTERS = frozenset(
    {*(status.value for status in TicketStatus), NOT_CLOSED_TICKET_STATUS_FILTER}
)


def _ticket_status_scope(value: str | None) -> support_service.TicketStatusScope:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return support_service.TicketStatusScope.all()
    if normalized == NOT_CLOSED_TICKET_STATUS_FILTER:
        return support_service.TicketStatusScope.not_closed()
    return support_service.TicketStatusScope.matching(TicketStatus(normalized))


def _normalize_ticket_uuid_filter(value: str | None, name: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return str(UUID(normalized))
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid UUID") from exc


def _normalize_ticket_per_page(per_page: int | str | None) -> int:
    try:
        normalized = int(str(per_page or "").strip())
    except ValueError:
        return SUPPORT_TICKET_LIST_DEFINITION.default_per_page
    if normalized in SUPPORT_TICKET_LIST_DEFINITION.per_page_options:
        return normalized
    return SUPPORT_TICKET_LIST_DEFINITION.default_per_page


def _normalize_ticket_filters(filters: str | None) -> str | None:
    normalized = str(filters or "").strip()
    if not normalized:
        return None
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON in filters payload") from exc
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    support_ticket_filters.build_ticket_filter_clause(canonical)
    return canonical


def build_ticket_list_query(
    *,
    search: str | None,
    status: str | None,
    ticket_type: str | None,
    region: str | None,
    assigned_to_me: bool,
    project_manager_person_id: str | None,
    site_coordinator_person_id: str | None,
    subscriber_id: str | None,
    filters: str | None,
    sort_by: str | None = None,
    sort_dir: SortDirection | str | None = None,
    page: int = 1,
    per_page: int | str | None = 25,
) -> ListQuery:
    """Normalize the admin support queue through its declared capabilities."""

    normalized_status = str(status or "").strip().lower() or None
    if normalized_status and normalized_status not in _TICKET_STATUS_FILTERS:
        raise ValueError(f"Unsupported ticket status: {normalized_status}")
    return SUPPORT_TICKET_LIST_DEFINITION.build_query(
        search=search,
        filters={
            "status": normalized_status,
            "ticket_type": str(ticket_type or "").strip() or None,
            "region": support_ticket_region_projection.normalize_region_value(region)
            or None,
            "assigned_to_me": "true" if assigned_to_me else None,
            "project_manager_person_id": _normalize_ticket_uuid_filter(
                project_manager_person_id, "project_manager_person_id"
            ),
            "site_coordinator_person_id": _normalize_ticket_uuid_filter(
                site_coordinator_person_id, "site_coordinator_person_id"
            ),
            "subscriber_id": _normalize_ticket_uuid_filter(
                subscriber_id, "subscriber_id"
            ),
            "filters": _normalize_ticket_filters(filters),
        },
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=_normalize_ticket_per_page(per_page),
    )


def parse_uuid_or_none(value: str | None) -> UUID | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


def parse_dt_or_none(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def service_team_options(db: Session) -> list[dict[str, str]]:
    return support_ticket_settings_service.list_service_teams(db)


def visible_ticket_columns(raw_cookie: str | None) -> list[str]:
    raw_columns = (raw_cookie or ",".join(DEFAULT_VISIBLE_COLUMNS)).split(",")
    visible_columns = [
        column
        for column in raw_columns
        if any(column == item["key"] for item in TICKET_COLUMNS)
    ]
    return visible_columns or list(DEFAULT_VISIBLE_COLUMNS)


def _non_empty_ids(values: list[object | None]) -> list[str]:
    ids: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        text = str(value)
        if text not in ids:
            ids.append(text)
    return ids


def _label_lookup(options: list[dict[str, str]]) -> dict[str, str]:
    return {item["id"]: item["label"] for item in options if item.get("id")}


def _service_team_lookup(db: Session) -> dict[str, str]:
    return {item["id"]: item["label"] for item in service_team_options(db)}


def _append_missing_option(options: list[str], value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text or text in options:
        return options
    return [*options, text]


def _ticket_sla_state(
    ticket: Ticket, *, clock=None, now: datetime | None = None
) -> dict[str, object]:
    state = sla_assignment.ticket_sla_status(ticket, clock=clock, now=now)
    settings_terminal = support_ticket_settings_service.status_is_terminal(
        ticket.status
    )
    state["terminal"] = state["terminal"] or settings_terminal
    if settings_terminal:
        state["breached"] = False
    return state


def _ticket_scope_count(
    db: Session,
    *,
    list_query: ListQuery,
    actor_id: str | None,
    status: str | None,
) -> int:
    return support_service.tickets.count(
        db,
        search=list_query.search,
        status_scope=_ticket_status_scope(status),
        ticket_type=list_query.filter_value("ticket_type"),
        region=list_query.filter_value("region"),
        assigned_to_person_id=(
            actor_id if list_query.filter_value("assigned_to_me") == "true" else None
        ),
        project_manager_person_id=list_query.filter_value("project_manager_person_id"),
        site_coordinator_person_id=list_query.filter_value(
            "site_coordinator_person_id"
        ),
        subscriber_id=list_query.filter_value("subscriber_id"),
        filters=list_query.filter_value("filters"),
    )


def _status_summary_cards(
    db: Session, *, list_query: ListQuery, actor_id: str | None
) -> list[dict[str, str | int | bool]]:
    selected_status = list_query.filter_value("status")
    definitions = (
        ("", "All"),
        ("open", "Open"),
        ("closed", "Closed"),
        ("canceled", "Cancelled"),
    )
    return [
        {
            "value": value,
            "label": label,
            "count": _ticket_scope_count(
                db,
                list_query=list_query,
                actor_id=actor_id,
                status=value or None,
            ),
            "href": list_query.url(
                "/admin/support/tickets", filters={"status": value or None}
            ),
            "active": (selected_status or "") == value,
        }
        for value, label in definitions
    ]


def _resolve_uploaded_by_subscriber_id(db: Session, actor_id: str | None) -> str | None:
    uid = parse_uuid_or_none(actor_id)
    if not uid:
        return None
    return str(uid) if db.get(Subscriber, uid) else None


@support_service.ticket_owner_command("stage_attachment_uploads")
def upload_ticket_attachments(
    db: Session,
    *,
    ticket_id: str,
    attachments: list,
    entity_type: str,
    actor_id: str | None,
) -> list[dict]:
    uploaded_metadata = []
    uploaded_by = _resolve_uploaded_by_subscriber_id(db, actor_id)
    try:
        for attachment in attachments or []:
            filename = (getattr(attachment, "filename", "") or "").strip()
            if not filename:
                continue
            payload = attachment.file.read()
            if not payload:
                continue
            if len(payload) > MAX_ATTACHMENT_BYTES:
                max_mb = max(1, MAX_ATTACHMENT_BYTES // 1048576)
                raise ValueError(f"{filename}: max file size is {max_mb} MB")
            content_type = (
                getattr(attachment, "content_type", None) or "application/octet-stream"
            ).lower()
            if content_type not in ALLOWED_ATTACHMENT_TYPES:
                raise ValueError(f"{filename}: unsupported file type")

            record = file_uploads.stage_upload(
                db=db,
                domain="attachments",
                entity_type=entity_type,
                entity_id=ticket_id,
                original_filename=filename,
                content_type=content_type,
                data=payload,
                uploaded_by=uploaded_by,
                owner_subscriber_id=None,
            )
            uploaded_metadata.append(
                {
                    "file_name": record.original_filename,
                    "content_type": record.content_type or content_type,
                    "file_size": int(record.file_size),
                    "storage_key": record.storage_key_or_relative_path,
                    "stored_file_id": str(record.id),
                }
            )
        return uploaded_metadata
    except Exception:
        # Database metadata is rolled back by the Ticket owner. Object keys are
        # content-addressed; an interrupted command is repaired by the storage
        # orphan reconciler described in the Support lifecycle runbook.
        raise


def build_ticket_form_context(
    db: Session,
    *,
    query_params: Mapping[str, object] | None = None,
    ticket: Ticket | None = None,
) -> dict:
    params = query_params or {}
    status_options = support_ticket_settings_service.list_status_options(db)
    priority_options = support_ticket_settings_service.list_priority_options(db)
    ticket_type_options = support_ticket_settings_service.list_ticket_type_options(db)
    current_assignees = [
        str(row.person_id)
        for row in (ticket.assignees if ticket and ticket.assignees else [])
        if row.person_id
    ]
    assignment_ids = current_assignees + _non_empty_ids(
        [
            ticket.technician_person_id if ticket else None,
            ticket.ticket_manager_person_id if ticket else None,
            ticket.site_coordinator_person_id if ticket else None,
        ]
    )
    staff = support_service.list_assignment_people(db, include_ids=assignment_ids)
    prefill = {
        "title": ticket.title if ticket else str(params.get("title", "") or ""),
        "description": ticket.description
        if ticket
        else str(params.get("description", "") or ""),
        "subscriber_id": str(ticket.subscriber_id)
        if ticket and ticket.subscriber_id
        else str(params.get("subscriber_id", "") or ""),
        "customer_account_id": str(ticket.customer_account_id)
        if ticket and ticket.customer_account_id
        else str(params.get("customer_account_id", "") or ""),
        "customer_person_id": str(ticket.customer_person_id)
        if ticket and ticket.customer_person_id
        else str(params.get("customer_person_id", "") or ""),
        "region": ticket.region if ticket else str(params.get("region", "") or ""),
        "ticket_type": ticket.ticket_type
        if ticket
        else str(params.get("ticket_type", "") or ""),
        "priority": ticket.priority
        if ticket
        else str(
            params.get("priority", support_ticket_settings_service.default_priority(db))
            or ""
        ),
        "channel": ticket.channel.value
        if ticket
        else str(params.get("channel", TicketChannel.web.value) or ""),
        "status": ticket.status
        if ticket
        else str(
            params.get("status", support_ticket_settings_service.default_status(db))
            or ""
        ),
        "due_at": ticket.due_at.strftime("%Y-%m-%dT%H:%M")
        if ticket and ticket.due_at
        else "",
        "tags": ",".join(ticket.tags or [])
        if ticket
        else str(params.get("tags", "") or ""),
        "related_outage_ticket_id": str(
            params.get("related_outage_ticket_id", "") or ""
        ),
        "technician_person_id": str(ticket.technician_person_id)
        if ticket and ticket.technician_person_id
        else "",
        "ticket_manager_person_id": str(ticket.ticket_manager_person_id)
        if ticket and ticket.ticket_manager_person_id
        else "",
        "site_coordinator_person_id": str(ticket.site_coordinator_person_id)
        if ticket and ticket.site_coordinator_person_id
        else "",
        "service_team_id": str(ticket.service_team_id)
        if ticket and ticket.service_team_id
        else "",
        "assignee_person_ids": current_assignees,
    }
    customer_person_id = str(prefill["customer_person_id"] or "")
    subscriber_id_value = str(prefill["subscriber_id"] or "")
    customer_person = support_service.person_option(db, customer_person_id)
    subscriber_person = support_service.person_option(db, subscriber_id_value)
    selected_person = subscriber_person or customer_person or {}
    prefill["customer_person_label"] = (
        customer_person["label"] if customer_person else ""
    )
    prefill["subscriber_label"] = (
        subscriber_person["label"] if subscriber_person else ""
    )
    status_options = _append_missing_option(
        status_options, str(prefill["status"] or "")
    )
    priority_options = _append_missing_option(
        priority_options, str(prefill["priority"] or "")
    )
    ticket_type_options = _append_missing_option(
        ticket_type_options, str(prefill["ticket_type"] or "")
    )
    return {
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "all_channels": [item.value for item in TicketChannel],
        "region_options": support_service.regions(db),
        "ticket_type_options": ticket_type_options,
        "service_team_options": service_team_options(db),
        "staff_options": staff,
        "subscriber_options": support_service.list_people(
            db,
            include_ids=_non_empty_ids(
                [prefill["customer_person_id"], prefill["subscriber_id"]]
            ),
        ),
        "selected_person": selected_person,
        "prefill": prefill,
    }


def build_ticket_edit_page_context(
    db: Session,
    *,
    query_params: Mapping[str, object] | None,
    ticket_lookup: str,
) -> dict:
    ticket = support_service.tickets.get_by_lookup(db, ticket_lookup)
    context = build_ticket_form_context(db, query_params=query_params, ticket=ticket)
    context.update(
        {
            "page_title": "Edit Ticket",
            "form_mode": "edit",
            "ticket": ticket,
        }
    )
    return context


def build_ticket_create_payload(**kwargs) -> TicketCreate:
    tags = kwargs.pop("tags", None)
    assignee_person_ids = kwargs.pop("assignee_person_ids", [])
    return TicketCreate(
        title=kwargs["title"],
        description=kwargs["description"] or None,
        subscriber_id=parse_uuid_or_none(kwargs.get("subscriber_id")),
        customer_account_id=parse_uuid_or_none(kwargs.get("customer_account_id")),
        customer_person_id=parse_uuid_or_none(kwargs.get("customer_person_id")),
        created_by_person_id=None,
        region=kwargs.get("region") or None,
        technician_person_id=parse_uuid_or_none(kwargs.get("technician_person_id")),
        ticket_manager_person_id=parse_uuid_or_none(
            kwargs.get("ticket_manager_person_id")
        ),
        site_coordinator_person_id=parse_uuid_or_none(
            kwargs.get("site_coordinator_person_id")
        ),
        service_team_id=parse_uuid_or_none(kwargs.get("service_team_id")),
        ticket_type=kwargs.get("ticket_type") or None,
        priority=kwargs["priority"],
        channel=kwargs["channel"],
        status=kwargs["status"],
        due_at=parse_dt_or_none(kwargs.get("due_at")),
        tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
        assignee_person_ids=[
            uid
            for uid in (parse_uuid_or_none(item) for item in assignee_person_ids)
            if uid
        ],
        related_outage_ticket_id=parse_uuid_or_none(
            kwargs.get("related_outage_ticket_id")
        ),
    )


def build_ticket_update_payload(**kwargs) -> TicketUpdate:
    tags = kwargs.pop("tags", None)
    assignee_person_ids = kwargs.pop("assignee_person_ids", [])
    return TicketUpdate(
        title=kwargs["title"],
        description=kwargs["description"] or None,
        subscriber_id=parse_uuid_or_none(kwargs.get("subscriber_id")),
        customer_account_id=parse_uuid_or_none(kwargs.get("customer_account_id")),
        customer_person_id=parse_uuid_or_none(kwargs.get("customer_person_id")),
        region=kwargs.get("region") or None,
        status=kwargs["status"],
        priority=kwargs["priority"],
        channel=kwargs["channel"],
        ticket_type=kwargs.get("ticket_type") or None,
        due_at=parse_dt_or_none(kwargs.get("due_at")),
        tags=[item.strip() for item in (tags or "").split(",") if item.strip()],
        technician_person_id=parse_uuid_or_none(kwargs.get("technician_person_id")),
        ticket_manager_person_id=parse_uuid_or_none(
            kwargs.get("ticket_manager_person_id")
        ),
        site_coordinator_person_id=parse_uuid_or_none(
            kwargs.get("site_coordinator_person_id")
        ),
        service_team_id=parse_uuid_or_none(kwargs.get("service_team_id")),
        assignee_person_ids=[
            uid
            for uid in (parse_uuid_or_none(item) for item in assignee_person_ids)
            if uid
        ],
    )


def build_ticket_comment_payload(
    *,
    body: str,
    is_internal: bool,
    actor_id: str | None,
    uploaded: list[dict],
) -> TicketCommentCreate:
    return TicketCommentCreate(
        body=body,
        is_internal=is_internal,
        author_type=TicketCommentAuthorType.staff,
        author_system_user_id=parse_uuid_or_none(actor_id),
        attachments=[AttachmentMeta(**item) for item in uploaded],
    )


class DuplicateTicketWarningError(Exception):
    """A create submission matches existing open tickets and the operator has
    not ticked the ``duplicate_override`` checkbox yet."""

    def __init__(self, result: ticket_validation.TicketDuplicateResult):
        super().__init__("A similar ticket already exists.")
        self.result = result


def _duplicate_input_from_payload(
    payload: TicketCreate,
) -> ticket_validation.TicketDuplicateInput:
    metadata = payload.metadata_ if isinstance(payload.metadata_, dict) else {}
    return ticket_validation.TicketDuplicateInput(
        title=payload.title,
        description=payload.description,
        subscriber_id=payload.subscriber_id,
        customer_account_id=payload.customer_account_id,
        customer_person_id=payload.customer_person_id,
        lead_id=payload.lead_id,
        ticket_type=payload.ticket_type,
        base_station_details=str(metadata.get("base_station_details") or "") or None,
        tags=payload.tags,
        region=payload.region,
    )


def create_ticket_from_form(
    db: Session,
    *,
    request,
    actor_id: str | None,
    attachments: list,
    duplicate_override: bool = False,
    **form,
) -> Ticket:
    """Create a support ticket from web form values and attach uploaded files.

    Mirrors CRM's admin create flow: candidate duplicates are re-checked
    server-side on submit; without the operator's ``duplicate_override``
    confirmation a warning is raised so the route can re-render the form.
    """
    payload = build_ticket_create_payload(actor_id=actor_id, **form)
    duplicate_result = ticket_validation.find_duplicate_ticket_candidates(
        db, _duplicate_input_from_payload(payload)
    )
    if duplicate_result.has_warning:
        if not duplicate_override:
            raise DuplicateTicketWarningError(duplicate_result)
        metadata = dict(payload.metadata_ or {})
        metadata["duplicate_override"] = True
        metadata["possible_duplicate_tickets"] = [
            {
                "ticket_id": match.ticket_id,
                "reference": match.reference,
                "score": match.score,
                "confidence": match.confidence,
                "reasons": match.reasons,
            }
            for match in duplicate_result.matches
        ]
        payload.metadata_ = metadata
    db_session_adapter.release_read_transaction(db)
    ticket = support_service.tickets.create(
        db,
        payload,
        actor_id=actor_id,
        request=request,
        acknowledgement_mode=(
            support_service.TicketCreationAcknowledgementMode.customer_email
        ),
    )
    if attachments:
        uploaded = upload_ticket_attachments(
            db,
            ticket_id=str(ticket.id),
            attachments=attachments,
            entity_type="support_ticket_attachment",
            actor_id=actor_id,
        )
        support_service.tickets.add_attachments(db, str(ticket.id), uploaded)
    return ticket


def update_ticket_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
    **form,
):
    payload = build_ticket_update_payload(**form)
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.update(
        db,
        ticket_id,
        payload,
        actor_id=actor_id,
        request=request,
    )


def quick_update_ticket(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
    fields: dict,
):
    """Apply a small set of fields to a ticket (status, technician, etc.).

    Surfaces invalid client input as HTTP 400 rather than a 500 from a bare
    ValueError / pydantic ValidationError.
    """
    from pydantic import ValidationError

    uuid_fields = {
        "technician_person_id",
        "ticket_manager_person_id",
        "site_coordinator_person_id",
        "service_team_id",
        "assigned_to_person_id",
    }
    payload_data: dict = {}
    for key, value in fields.items():
        if value in (None, ""):
            continue
        if key in uuid_fields:
            try:
                payload_data[key] = UUID(str(value))
            except ValueError as exc:
                raise WebSupportTicketInputError(
                    code="support_ticket_form_invalid",
                    message=f"{key} must be a valid UUID",
                ) from exc
        elif key in ("status", "priority"):
            payload_data[key] = str(value).strip()
        else:
            payload_data[key] = value
    try:
        payload = TicketUpdate(**payload_data)
    except ValidationError as exc:
        raise WebSupportTicketInputError(
            code="support_ticket_form_invalid",
            message="Ticket update fields are invalid",
            details={"validation_error_count": len(exc.errors())},
        ) from exc
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.update(
        db,
        ticket_id,
        payload,
        actor_id=actor_id,
        request=request,
    )


def add_ticket_comment_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
    body: str,
    is_internal: bool,
    attachments: list,
    mentions: str | None = None,
):
    db_session_adapter.release_read_transaction(db)
    uploaded = upload_ticket_attachments(
        db,
        ticket_id=ticket_id,
        attachments=attachments,
        entity_type="support_ticket_comment_attachment",
        actor_id=actor_id,
    )
    mentioned_agent_ids = _parse_mentions_payload(mentions)
    payload = build_ticket_comment_payload(
        body=body,
        is_internal=is_internal,
        actor_id=actor_id,
        uploaded=uploaded,
    )
    db_session_adapter.release_read_transaction(db)
    comment = support_service.tickets.create_comment(
        db,
        ticket_id,
        payload,
        actor_id=actor_id,
        request=request,
        mentioned_agent_ids=mentioned_agent_ids,
    )
    return comment


def update_ticket_comment_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    comment_id: str,
    actor_id: str | None,
    body: str,
):
    comment = support_service.ticket_comments.get(db, comment_id)
    if str(comment.ticket_id) != str(ticket_id):
        raise WebSupportTicketInputError(
            code="ticket_comment_not_found",
            message="Ticket comment not found",
        )
    db_session_adapter.release_read_transaction(db)
    return support_service.ticket_comments.update(
        db,
        comment=comment,
        payload=TicketCommentUpdate(body=body),
        actor_id=actor_id,
        request=request,
    )


def _parse_mentions_payload(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    mentions: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            token = item.strip()
        elif isinstance(item, Mapping):
            token = str(item.get("id") or "").strip()
        else:
            token = ""
        if token and token not in mentions:
            mentions.append(token)
    return mentions


def auto_assign_ticket(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
):
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.manual_auto_assign(
        db,
        ticket_id,
        actor_id=actor_id,
        request=request,
    )


def link_ticket_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    to_ticket_id: str,
    link_type: str,
    actor_id: str | None,
):
    to_ticket_uuid = parse_uuid_or_none(to_ticket_id)
    if not to_ticket_uuid:
        raise ValueError("Target ticket must be a valid ticket UUID.")
    payload = TicketLinkCreate(to_ticket_id=to_ticket_uuid, link_type=link_type)
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.link_ticket(
        db,
        from_ticket_id=ticket_id,
        to_ticket_id=str(payload.to_ticket_id),
        link_type=payload.link_type,
        actor_id=actor_id,
        request=request,
    )


def merge_ticket_from_form(
    db: Session,
    *,
    request,
    ticket_id: str,
    target_ticket_id: str,
    reason: str | None,
    actor_id: str | None,
):
    target_uuid = parse_uuid_or_none(target_ticket_id)
    if not target_uuid:
        raise ValueError("Target ticket must be a valid ticket UUID.")
    db_session_adapter.release_read_transaction(db)
    return support_service.tickets.merge(
        db,
        ticket_id,
        TicketMergeRequest(target_ticket_id=target_uuid, reason=reason),
        actor_id=actor_id,
        request=request,
    )


def delete_ticket(
    db: Session,
    *,
    request,
    ticket_id: str,
    actor_id: str | None,
) -> None:
    db_session_adapter.release_read_transaction(db)
    support_service.tickets.soft_delete(
        db,
        ticket_id,
        actor_id=actor_id,
        request=request,
    )


def delete_ticket_hx_headers() -> dict[str, str]:
    return {
        "HX-Redirect": "/admin/support/tickets",
        "HX-Trigger": json.dumps(
            {
                "showToast": {
                    "type": "success",
                    "title": "Ticket deleted",
                    "message": "Ticket was archived.",
                }
            }
        ),
    }


def _identity_resolution_summary(ticket) -> dict[str, str] | None:
    metadata = dict(getattr(ticket, "metadata_", None) or {})
    resolution = metadata.get("identity_resolution")
    if not isinstance(resolution, dict):
        return None
    status = str(resolution.get("status") or "").strip()
    matched_via = str(resolution.get("matched_via") or "").strip()
    matched_field = str(resolution.get("matched_field") or "").strip()
    confidence = str(resolution.get("match_confidence") or "").strip().upper()
    confidence_suffix = f", {confidence} confidence" if confidence else ""
    if status == "matched" and matched_via == "subscriber_contact":
        detail = "Matched via subscriber contact"
        if matched_field:
            detail = f"{detail} ({matched_field})"
        if confidence_suffix:
            detail = f"{detail}{confidence_suffix}"
        return {"status": status, "detail": detail}
    if status == "matched" and matched_via:
        detail = f"Matched via {matched_via.replace('_', ' ')}"
        if matched_field:
            detail = f"{detail} ({matched_field})"
        if confidence_suffix:
            detail = f"{detail}{confidence_suffix}"
        return {"status": status, "detail": detail}
    if status == "ambiguous":
        return {
            "status": status,
            "detail": "Inbound identity is ambiguous and requires manual review",
        }
    if status == "unmatched" and metadata.get("manual_review_required"):
        return {
            "status": status,
            "detail": "Inbound identity requires manual review before account actions",
        }
    return None


def build_tickets_list_context(
    db: Session,
    *,
    list_query: ListQuery | None = None,
    search: str | None = None,
    status: str | None = None,
    ticket_type: str | None = None,
    region: str | None = None,
    assigned_to_me: bool = False,
    actor_id: str | None = None,
    project_manager_person_id: str | None = None,
    site_coordinator_person_id: str | None = None,
    subscriber_id: str | None = None,
    order_by: str = "created_at",
    order_dir: SortDirection | str = "desc",
    page: int = 1,
    per_page: int | str | None = 25,
    visible_columns_cookie: str | None = None,
    filters: str | None = None,
) -> dict:
    if list_query is None:
        list_query = build_ticket_list_query(
            search=search,
            status=status,
            ticket_type=ticket_type,
            region=region,
            assigned_to_me=assigned_to_me,
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            filters=filters,
            sort_by=order_by,
            sort_dir=order_dir,
            page=page,
            per_page=per_page,
        )
    if list_query.definition.key != SUPPORT_TICKET_LIST_DEFINITION.key:
        raise ValueError("Ticket list requires the support ticket definition")

    selected_status = list_query.filter_value("status")
    status_options = support_ticket_settings_service.list_status_options(db)
    priority_options = support_ticket_settings_service.list_priority_options(db)
    status_options = _append_missing_option(status_options, selected_status)
    total = _ticket_scope_count(
        db,
        list_query=list_query,
        actor_id=actor_id,
        status=selected_status,
    )
    page_meta = PageMeta.from_query(list_query, total)
    effective_query = list_query.with_page(page_meta.page)
    rows = support_service.tickets.list(
        db,
        search=effective_query.search,
        status_scope=_ticket_status_scope(effective_query.filter_value("status")),
        ticket_type=effective_query.filter_value("ticket_type"),
        region=effective_query.filter_value("region"),
        assigned_to_person_id=(
            actor_id
            if effective_query.filter_value("assigned_to_me") == "true"
            else None
        ),
        project_manager_person_id=effective_query.filter_value(
            "project_manager_person_id"
        ),
        site_coordinator_person_id=effective_query.filter_value(
            "site_coordinator_person_id"
        ),
        subscriber_id=effective_query.filter_value("subscriber_id"),
        filters=effective_query.filter_value("filters"),
        order_by=effective_query.sort_by,
        order_dir=effective_query.sort_dir,
        limit=effective_query.per_page,
        offset=effective_query.offset,
    )
    assignment_ids: list[object | None] = []
    subscriber_ids: list[object | None] = [
        effective_query.filter_value("subscriber_id")
    ]
    for ticket in rows:
        assignment_ids.extend(
            [
                ticket.assigned_to_person_id,
                ticket.technician_person_id,
                ticket.ticket_manager_person_id,
                ticket.site_coordinator_person_id,
            ]
        )
        subscriber_ids.extend(
            [
                ticket.customer_person_id,
                ticket.customer_account_id,
                ticket.subscriber_id,
            ]
        )
    staff = support_service.list_assignment_people(
        db,
        include_ids=_non_empty_ids(
            assignment_ids
            + [
                effective_query.filter_value("project_manager_person_id"),
                effective_query.filter_value("site_coordinator_person_id"),
                actor_id,
            ]
        ),
    )
    subscribers = support_service.list_people(
        db,
        include_ids=_non_empty_ids(subscriber_ids),
    )
    sla_clocks = sla_assignment.latest_ticket_sla_clocks(
        db, [ticket.id for ticket in rows]
    )
    return {
        "tickets": rows,
        "list_query": effective_query,
        "page_meta": page_meta,
        "search": effective_query.search or "",
        "status": effective_query.filter_value("status") or "",
        "ticket_type": effective_query.filter_value("ticket_type") or "",
        "region": effective_query.filter_value("region") or "",
        "assigned_to_me": effective_query.filter_value("assigned_to_me") == "true",
        "project_manager_person_id": effective_query.filter_value(
            "project_manager_person_id"
        )
        or "",
        "site_coordinator_person_id": effective_query.filter_value(
            "site_coordinator_person_id"
        )
        or "",
        "subscriber_id": effective_query.filter_value("subscriber_id") or "",
        "filters": effective_query.filter_value("filters") or "",
        "ticket_filter_schema": support_ticket_filters.serialize_ticket_filter_schema(
            status_options=status_options,
            priority_options=priority_options,
            ticket_type_options=support_service.ticket_types(db),
            staff_options=staff,
            service_team_options=service_team_options(db),
        ),
        "order_by": effective_query.sort_by,
        "order_dir": effective_query.sort_dir,
        "page": page_meta.page,
        "per_page": page_meta.per_page,
        "total": page_meta.total_items,
        "total_pages": page_meta.total_pages,
        "has_next_page": page_meta.has_next,
        "status_summary_cards": _status_summary_cards(
            db, list_query=effective_query, actor_id=actor_id
        ),
        "visible_columns": visible_ticket_columns(visible_columns_cookie),
        "ticket_columns": TICKET_COLUMNS,
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "ticket_type_options": support_service.ticket_types(db),
        "region_options": support_service.regions(db),
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "subscriber_options": subscribers,
        "subscriber_lookup": _label_lookup(subscribers),
        "sla_states": {
            str(ticket.id): _ticket_sla_state(
                ticket, clock=sla_clocks.get(str(ticket.id))
            )
            for ticket in rows
        },
        "status_presentations": {
            value: ticket_status_presentation(value)
            for value in {
                *status_options,
                *(ticket.status for ticket in rows),
            }
        },
    }


def list_tickets_for_scope(
    db: Session, *, list_query: ListQuery, actor_id: str | None
) -> list[Ticket]:
    """Return the complete canonical support queue scope for export/reconciliation."""

    if list_query.definition.key != SUPPORT_TICKET_LIST_DEFINITION.key:
        raise ValueError("Ticket scope requires the support ticket definition")
    return support_service.tickets.list(
        db,
        search=list_query.search,
        status_scope=_ticket_status_scope(list_query.filter_value("status")),
        ticket_type=list_query.filter_value("ticket_type"),
        region=list_query.filter_value("region"),
        assigned_to_person_id=(
            actor_id if list_query.filter_value("assigned_to_me") == "true" else None
        ),
        project_manager_person_id=list_query.filter_value("project_manager_person_id"),
        site_coordinator_person_id=list_query.filter_value(
            "site_coordinator_person_id"
        ),
        subscriber_id=list_query.filter_value("subscriber_id"),
        filters=list_query.filter_value("filters"),
        order_by=list_query.sort_by,
        order_dir=list_query.sort_dir,
        limit=None,
        offset=0,
    )


def _ticket_csv_value(
    ticket: Ticket,
    key: str,
    *,
    staff_lookup: dict[str, str],
    subscriber_lookup: dict[str, str],
) -> str:
    def _person(lookup: dict[str, str], value: object | None) -> str:
        text = str(value) if value else ""
        return lookup.get(text, text)

    if key == "number":
        return ticket.number or str(ticket.id)
    if key == "ticket_type":
        return ticket.ticket_type or ""
    if key == "priority":
        return ticket.priority or ""
    if key == "status":
        return ticket.status or ""
    if key == "customer":
        return _person(subscriber_lookup, ticket.customer_person_id)
    if key == "customer_id":
        return _person(subscriber_lookup, ticket.customer_account_id)
    if key == "subscriber":
        return _person(subscriber_lookup, ticket.subscriber_id)
    if key == "region":
        return ticket.region or ""
    if key == "technician":
        return _person(staff_lookup, ticket.technician_person_id)
    if key == "project_manager":
        return _person(staff_lookup, ticket.ticket_manager_person_id)
    if key == "site_coordinator":
        return _person(staff_lookup, ticket.site_coordinator_person_id)
    if key == "channel":
        return ticket.channel.value if ticket.channel else ""
    if key == "due_at":
        return ticket.due_at.strftime("%Y-%m-%d %H:%M UTC") if ticket.due_at else ""
    if key == "created_at":
        return (
            ticket.created_at.strftime("%Y-%m-%d %H:%M UTC")
            if ticket.created_at
            else ""
        )
    return ""


def render_tickets_csv(
    db: Session,
    *,
    list_query: ListQuery | None = None,
    search: str | None = None,
    status: str | None = None,
    ticket_type: str | None = None,
    region: str | None = None,
    assigned_to_me: bool = False,
    actor_id: str | None = None,
    project_manager_person_id: str | None = None,
    site_coordinator_person_id: str | None = None,
    subscriber_id: str | None = None,
    filters: str | None = None,
    order_by: str = "created_at",
    order_dir: SortDirection | str = "desc",
    visible_columns_cookie: str | None = None,
) -> str:
    """Render the ticket list as CSV honoring list filters and visible columns."""
    if list_query is None:
        list_query = build_ticket_list_query(
            search=search,
            status=status,
            ticket_type=ticket_type,
            region=region,
            assigned_to_me=assigned_to_me,
            project_manager_person_id=project_manager_person_id,
            site_coordinator_person_id=site_coordinator_person_id,
            subscriber_id=subscriber_id,
            filters=filters,
            sort_by=order_by,
            sort_dir=order_dir,
        )
    columns = visible_ticket_columns(visible_columns_cookie)
    rows = list_tickets_for_scope(db, list_query=list_query, actor_id=actor_id)
    assignment_ids: list[object | None] = []
    subscriber_ids: list[object | None] = []
    for ticket in rows:
        assignment_ids.extend(
            [
                ticket.technician_person_id,
                ticket.ticket_manager_person_id,
                ticket.site_coordinator_person_id,
            ]
        )
        subscriber_ids.extend(
            [
                ticket.customer_person_id,
                ticket.customer_account_id,
                ticket.subscriber_id,
            ]
        )
    staff_lookup = _label_lookup(
        support_service.list_assignment_people(
            db, include_ids=_non_empty_ids(assignment_ids)
        )
    )
    subscriber_lookup = _label_lookup(
        support_service.list_people(db, include_ids=_non_empty_ids(subscriber_ids))
    )
    labels = {item["key"]: item["label"] for item in TICKET_COLUMNS}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([labels[key] for key in columns])
    for ticket in rows:
        writer.writerow(
            [
                _ticket_csv_value(
                    ticket,
                    key,
                    staff_lookup=staff_lookup,
                    subscriber_lookup=subscriber_lookup,
                )
                for key in columns
            ]
        )
    return buffer.getvalue()


def build_ticket_detail_context(
    db: Session,
    *,
    ticket_lookup: str,
    actor_id: str | None = None,
    can_read_material_requests: bool = False,
) -> dict:
    from uuid import uuid4

    from app.services import team_inbox_read, ticket_work_order_handoff
    from app.services.audit_helpers import build_audit_activities

    status_options = support_ticket_settings_service.list_status_options(db)
    priority_options = support_ticket_settings_service.list_priority_options(db)
    ticket = support_service.tickets.get_by_lookup(db, ticket_lookup)
    linked_work_orders = ticket_work_order_handoff.list_for_ticket(db, ticket.id)
    if can_read_material_requests:
        from app.services.field.material_requests import (
            MaterialRequestScope,
            list_staff_material_requests,
        )

        material_requests = list_staff_material_requests(
            db,
            scope=MaterialRequestScope(ticket_id=ticket.id),
            page=1,
            per_page=100,
        ).items
    else:
        material_requests = ()
    # Customer conversation history, so an agent sees what was already said on
    # other channels before replying here. `communications.team_inbox` owns these
    # rows; this is a read. Scoped by subscriber because no conversation-to-ticket
    # link exists yet — once the handoff owner lands this can narrow to the
    # originating conversation.
    ticket_conversations = (
        team_inbox_read.list_conversations(
            db,
            subscriber_id=ticket.subscriber_id,
            order_by="last_message_at",
            order_dir="desc",
            limit=5,
        ).items
        if ticket.subscriber_id
        else ()
    )
    linked_project_tasks = (
        db.query(ProjectTask)
        .filter(ProjectTask.ticket_id == ticket.id)
        .filter(ProjectTask.is_active.is_(True))
        .order_by(ProjectTask.created_at.asc(), ProjectTask.id.asc())
        .all()
    )
    comments = support_service.ticket_comments.list(
        db, str(ticket.id), limit=500, offset=0
    )
    status_options = _append_missing_option(status_options, ticket.status)
    priority_options = _append_missing_option(priority_options, ticket.priority)
    staff = support_service.list_assignment_people(
        db,
        include_ids=_non_empty_ids(
            [
                ticket.assigned_to_person_id,
                ticket.technician_person_id,
                ticket.ticket_manager_person_id,
                ticket.site_coordinator_person_id,
                *[row.person_id for row in (ticket.assignees or [])],
                *[
                    comment.author_system_user_id
                    for comment in comments
                    if getattr(comment, "author_system_user_id", None)
                ],
            ]
        ),
    )
    subscribers = support_service.list_people(
        db,
        include_ids=_non_empty_ids(
            [
                ticket.customer_person_id,
                ticket.customer_account_id,
                ticket.subscriber_id,
                *[
                    comment.author_person_id
                    for comment in comments
                    if getattr(comment, "author_person_id", None)
                ],
            ]
        ),
    )
    return {
        "ticket": ticket,
        "comments": comments,
        "sla_events": support_service.ticket_sla_events.list(
            db, str(ticket.id), limit=200, offset=0
        ),
        "ticket_links": support_service.tickets.list_links(
            db, str(ticket.id), limit=100
        ),
        "activities": build_audit_activities(
            db, "support_ticket", str(ticket.id), limit=100
        ),
        "all_statuses": status_options,
        "all_priorities": priority_options,
        "all_channels": [item.value for item in TicketChannel],
        "people_options": subscribers,
        "mention_agents": ticket_mentions.list_ticket_mention_users(db),
        "staff_options": staff,
        "staff_lookup": _label_lookup(staff),
        "subscriber_lookup": _label_lookup(subscribers),
        "customer_details": _ticket_customer_context(db, ticket.subscriber_id),
        "service_team_options": service_team_options(db),
        "service_team_lookup": _service_team_lookup(db),
        "sla_state": _ticket_sla_state(
            ticket,
            clock=sla_assignment.latest_ticket_sla_clocks(db, [ticket.id]).get(
                str(ticket.id)
            ),
        ),
        "status_presentations": {
            value: ticket_status_presentation(value) for value in status_options
        },
        "is_merged_source": bool(
            ticket.merged_into_ticket_id
            or support_ticket_settings_service.status_is_merged(ticket.status)
        ),
        "identity_resolution": _identity_resolution_summary(ticket),
        "linked_work_orders": linked_work_orders,
        "material_requests": material_requests,
        "material_request_create_url": (
            f"/admin/operations/material-requests/new?ticket_id={ticket.id}"
        ),
        "ticket_conversations": ticket_conversations,
        "linked_project_tasks": linked_project_tasks,
        "issue_work_order_action": ticket_work_order_handoff.issue_action(
            db, ticket, actor_id=actor_id
        ),
        "work_order_idempotency_key": uuid4().hex,
    }
