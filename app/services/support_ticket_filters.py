"""Whitelisted dynamic-filter specs for support tickets.

Backs the `filters` JSON param on GET /support/tickets and the admin ticket
queue's advanced filter builder. Reuses the shared `dynamic_filters` engine
(same contract as the admin users list): AND rows plus OR groups, strict
field/operator whitelisting — never raw column injection.
"""

from __future__ import annotations

from sqlalchemy import String, cast, exists, or_, select
from sqlalchemy.sql.elements import ClauseElement, ColumnElement

from app.models.support import (
    Ticket,
    TicketAssignee,
    TicketChannel,
    canonical_ticket_status_value,
)
from app.services.customer_support_links import (
    ticket_customer_link_filter,
    ticket_unlinked_customer_filter,
)
from app.services.dynamic_filters import (
    DEFAULT_OPERATORS_BY_TYPE,
    NULL_TOKENS,
    OPERATOR_LABELS,
    FilterFieldSpec,
    FilterValidationError,
    _coerce_list,
    _coerce_scalar,
    build_filter_expression,
    parse_filter_payload,
)

TICKET_DOCTYPE = "Ticket"


def _assigned_to_expression(operator: str, value: object) -> ClauseElement:
    """Match the legacy single-assignee column or the assignees table."""

    def _match(person_id: object) -> ColumnElement[bool]:
        coerced = _coerce_scalar(person_id, "uuid")
        return or_(
            Ticket.assigned_to_person_id == coerced,
            exists(
                select(TicketAssignee.ticket_id)
                .where(TicketAssignee.ticket_id == Ticket.id)
                .where(TicketAssignee.person_id == coerced)
            ),
        )

    if operator in {"is", "is not"}:
        token = str(value).strip().lower() if value is not None else None
        if token not in NULL_TOKENS:
            raise FilterValidationError(
                "assigned_to_person_id supports only NULL checks for 'is'/'is not'"
            )
        unassigned = Ticket.assigned_to_person_id.is_(None) & ~exists(
            select(TicketAssignee.ticket_id).where(
                TicketAssignee.ticket_id == Ticket.id
            )
        )
        return unassigned if operator == "is" else ~unassigned

    if operator in {"in", "not in"}:
        matches = or_(*[_match(item) for item in _coerce_list(value, "uuid")])
        return matches if operator == "in" else ~matches

    if operator == "=":
        return _match(value)
    if operator == "!=":
        return ~_match(value)
    raise FilterValidationError(
        f"Operator '{operator}' is not allowed for assigned_to_person_id"
    )


def _subscriber_expression(operator: str, value: object) -> ClauseElement:
    """Match any customer link column (list() semantics)."""

    def _match(subscriber_id: object) -> ColumnElement[bool]:
        coerced = _coerce_scalar(subscriber_id, "uuid")
        return ticket_customer_link_filter(Ticket, coerced)

    if operator in {"is", "is not"}:
        token = str(value).strip().lower() if value is not None else None
        if token not in NULL_TOKENS:
            raise FilterValidationError(
                "subscriber_id supports only NULL checks for 'is'/'is not'"
            )
        unlinked = ticket_unlinked_customer_filter(Ticket)
        return unlinked if operator == "is" else ~unlinked

    if operator in {"in", "not in"}:
        matches = or_(*[_match(item) for item in _coerce_list(value, "uuid")])
        return matches if operator == "in" else ~matches

    if operator == "=":
        return _match(value)
    if operator == "!=":
        return ~_match(value)
    raise FilterValidationError(
        f"Operator '{operator}' is not allowed for subscriber_id"
    )


def _tags_expression(operator: str, value: object) -> ClauseElement:
    """Tag matching against the JSON tags array via its text serialization."""
    tags_text = cast(Ticket.tags, String)
    if operator in {"=", "!="}:
        # Exact tag membership: match the JSON-quoted token.
        token = str(_coerce_scalar(value, "text") or "").strip()
        if not token:
            raise FilterValidationError("Tag value cannot be empty")
        pattern = f'%"{token}"%'
        matched = tags_text.ilike(pattern)
        return matched if operator == "=" else ~matched
    if operator in {"like", "not like"}:
        token = str(_coerce_scalar(value, "text") or "").strip()
        if not token:
            raise FilterValidationError("Tag value cannot be empty")
        pattern = f"%{token}%"
        matched = tags_text.ilike(pattern)
        return matched if operator == "like" else ~matched
    raise FilterValidationError(f"Operator '{operator}' is not allowed for tags")


def _status_expression(operator: str, value: object) -> ClauseElement:
    """Keep saved legacy filters aligned with the canonical closed status."""
    if operator in {"is", "is not"}:
        token = str(value).strip().lower() if value is not None else None
        if token not in NULL_TOKENS:
            raise FilterValidationError(
                "status supports only NULL checks for 'is'/'is not'"
            )
        expression = Ticket.status.is_(None)
        return expression if operator == "is" else ~expression

    if operator in {"in", "not in"}:
        statuses = list(
            dict.fromkeys(
                canonical_ticket_status_value(str(item))
                for item in _coerce_list(value, "select")
            )
        )
        expression = Ticket.status.in_(statuses)
        return expression if operator == "in" else ~expression

    status = canonical_ticket_status_value(str(_coerce_scalar(value, "select")))
    if operator == "=":
        return Ticket.status == status
    if operator == "!=":
        return Ticket.status != status
    raise FilterValidationError(f"Operator '{operator}' is not allowed for status")


TICKET_FILTER_SPECS: dict[str, FilterFieldSpec] = {
    "number": FilterFieldSpec(
        field="number", expression=Ticket.number, field_type="text"
    ),
    "title": FilterFieldSpec(field="title", expression=Ticket.title, field_type="text"),
    "status": FilterFieldSpec(
        field="status", field_type="select", builder=_status_expression
    ),
    "priority": FilterFieldSpec(
        field="priority", expression=Ticket.priority, field_type="select"
    ),
    "ticket_type": FilterFieldSpec(
        field="ticket_type", expression=Ticket.ticket_type, field_type="select"
    ),
    "region": FilterFieldSpec(
        field="region", expression=Ticket.region, field_type="text"
    ),
    "channel": FilterFieldSpec(
        field="channel",
        expression=Ticket.channel,
        field_type="select",
        options={item.value for item in TicketChannel},
    ),
    "subscriber_id": FilterFieldSpec(
        field="subscriber_id",
        field_type="uuid",
        operators={"=", "!=", "in", "not in", "is", "is not"},
        builder=_subscriber_expression,
    ),
    "created_by_person_id": FilterFieldSpec(
        field="created_by_person_id",
        expression=Ticket.created_by_person_id,
        field_type="uuid",
    ),
    "assigned_to_person_id": FilterFieldSpec(
        field="assigned_to_person_id",
        field_type="uuid",
        operators={"=", "!=", "in", "not in", "is", "is not"},
        builder=_assigned_to_expression,
    ),
    "technician_person_id": FilterFieldSpec(
        field="technician_person_id",
        expression=Ticket.technician_person_id,
        field_type="uuid",
    ),
    "ticket_manager_person_id": FilterFieldSpec(
        field="ticket_manager_person_id",
        expression=Ticket.ticket_manager_person_id,
        field_type="uuid",
    ),
    "site_coordinator_person_id": FilterFieldSpec(
        field="site_coordinator_person_id",
        expression=Ticket.site_coordinator_person_id,
        field_type="uuid",
    ),
    "service_team_id": FilterFieldSpec(
        field="service_team_id",
        expression=Ticket.service_team_id,
        field_type="uuid",
    ),
    "tags": FilterFieldSpec(
        field="tags",
        field_type="text",
        operators={"=", "!=", "like", "not like"},
        builder=_tags_expression,
    ),
    "created_at": FilterFieldSpec(
        field="created_at", expression=Ticket.created_at, field_type="datetime"
    ),
    "updated_at": FilterFieldSpec(
        field="updated_at", expression=Ticket.updated_at, field_type="datetime"
    ),
    "due_at": FilterFieldSpec(
        field="due_at", expression=Ticket.due_at, field_type="datetime"
    ),
    "resolved_at": FilterFieldSpec(
        field="resolved_at", expression=Ticket.resolved_at, field_type="datetime"
    ),
    "closed_at": FilterFieldSpec(
        field="closed_at", expression=Ticket.closed_at, field_type="datetime"
    ),
    "is_active": FilterFieldSpec(
        field="is_active", expression=Ticket.is_active, field_type="boolean"
    ),
}


TICKET_FILTER_LABELS: dict[str, str] = {
    "number": "Ticket Number",
    "title": "Title",
    "status": "Status",
    "priority": "Priority",
    "ticket_type": "Ticket Type",
    "region": "Region",
    "channel": "Channel",
    "subscriber_id": "Subscriber / Customer",
    "created_by_person_id": "Created By",
    "assigned_to_person_id": "Assignee",
    "technician_person_id": "Technician",
    "ticket_manager_person_id": "Project Manager",
    "site_coordinator_person_id": "Site Coordinator",
    "service_team_id": "Service Team",
    "tags": "Tag",
    "created_at": "Created At",
    "updated_at": "Updated At",
    "due_at": "Due At",
    "resolved_at": "Resolved At",
    "closed_at": "Closed At",
    "is_active": "Is Active",
}


def build_ticket_filter_clause(
    filters: str | list | dict | None,
) -> ColumnElement[bool] | None:
    """Parse a `filters` payload and build the whitelisted WHERE clause.

    Raises FilterValidationError on any invalid payload, field, operator,
    or value — callers translate this into an HTTP 400.
    """
    filter_query = parse_filter_payload(filters, default_doctype=TICKET_DOCTYPE)
    return build_filter_expression(
        filter_query,
        doctype=TICKET_DOCTYPE,
        field_specs=TICKET_FILTER_SPECS,
    )


def serialize_ticket_filter_schema(
    *,
    status_options: list[str],
    priority_options: list[str],
    ticket_type_options: list[str],
    staff_options: list[dict[str, str]],
    service_team_options: list[dict[str, str]],
) -> list[dict[str, object]]:
    """Field/operator/option metadata for the admin filter-builder UI."""

    def _select(values: list[str]) -> list[dict[str, str]]:
        return [
            {"value": value, "label": value.replace("_", " ").title()}
            for value in values
        ]

    def _people(options: list[dict[str, str]]) -> list[dict[str, str]]:
        return [
            {"value": str(item.get("id", "")), "label": str(item.get("label", ""))}
            for item in options
            if item.get("id")
        ]

    options_map: dict[str, list[dict[str, str]]] = {
        "status": _select(status_options),
        "priority": _select(priority_options),
        "ticket_type": [
            {"value": value, "label": value} for value in ticket_type_options
        ],
        "channel": _select(sorted(item.value for item in TicketChannel)),
        "is_active": [
            {"value": "true", "label": "Active"},
            {"value": "false", "label": "Archived"},
        ],
        "created_by_person_id": _people(staff_options),
        "assigned_to_person_id": _people(staff_options),
        "technician_person_id": _people(staff_options),
        "ticket_manager_person_id": _people(staff_options),
        "site_coordinator_person_id": _people(staff_options),
        "service_team_id": _people(service_team_options),
    }

    schema: list[dict[str, object]] = []
    for field_name, spec in TICKET_FILTER_SPECS.items():
        operators = (
            sorted(spec.operators)
            if spec.operators is not None
            else sorted(DEFAULT_OPERATORS_BY_TYPE.get(spec.field_type, {"="}))
        )
        schema.append(
            {
                "field": field_name,
                "label": TICKET_FILTER_LABELS.get(
                    field_name, field_name.replace("_", " ").title()
                ),
                "type": spec.field_type,
                "operators": [
                    {
                        "value": operator,
                        "label": OPERATOR_LABELS.get(operator, operator),
                    }
                    for operator in operators
                    if operator in OPERATOR_LABELS
                ],
                "options": options_map.get(field_name, []),
            }
        )
    return schema
