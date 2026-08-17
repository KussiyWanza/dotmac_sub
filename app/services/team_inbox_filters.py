"""Typed advanced-filter contract for Team Inbox service-team scope.

The shared dynamic-filter parser owns the transport grammar.  This module
immediately narrows that dynamic input to the one Inbox field supported by
this slice and owns its relationship-aware SQL semantics.  A conversation's
team scope comes from active ``InboxConversationTeam`` links, never from a
parallel browser decision or the legacy primary-team scalar alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TypedDict
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.team_inbox import InboxConversation, InboxConversationTeam
from app.services import service_team_lifecycle
from app.services.domain_errors import DomainError
from app.services.dynamic_filters import FilterCondition, FilterValidationError
from app.services.dynamic_filters import parse_filter_payload as parse_shared_payload

INBOX_CONVERSATION_DOCTYPE = "InboxConversation"
SERVICE_TEAM_FIELD = "service_team_id"
OWNER = "communications.team_inbox_projection"


class InboxFilterError(DomainError):
    """Stable rejection for malformed or unsupported Inbox filters."""


class ServiceTeamFilterOperator(StrEnum):
    equals = "="
    not_equals = "!="
    in_any = "in"
    not_in = "not in"
    is_empty = "is"
    is_not_empty = "is not"


@dataclass(frozen=True, slots=True)
class InboxAdvancedFilterPayload:
    """Raw transport value admitted by an Inbox adapter."""

    raw_json: str | None = None


class InboxSavedFilterTransport(TypedDict, total=False):
    search: str
    status: str
    channel_type: str
    service_team_id: str
    service_team_ids: str
    filters: str
    assigned_person_id: str
    needs_response: bool
    needs_attention: bool
    contact_resolution_status: str
    priority_at_most: int
    muted: bool
    snoozed: bool
    open_only: bool
    unassigned: bool
    unread: bool
    ai_handling: bool
    has_ticket: bool
    activity_from: str
    activity_to: str


@dataclass(frozen=True, slots=True)
class InboxSavedFilterPayload:
    """Typed saved-view state for every live Inbox queue filter."""

    search: str | None = None
    status: str | None = None
    channel_type: str | None = None
    service_team_id: str | None = None
    service_team_ids: str | None = None
    advanced_filters_json: str | None = None
    assigned_person_id: str | None = None
    needs_response: bool = False
    needs_attention: bool = False
    contact_resolution_status: str | None = None
    priority_at_most: int | None = None
    muted: bool | None = None
    snoozed: bool | None = None
    open_only: bool = False
    unassigned: bool = False
    unread: bool = False
    ai_handling: bool = False
    has_ticket: bool = False
    activity_from: str | None = None
    activity_to: str | None = None

    def to_storage(self) -> InboxSavedFilterTransport:
        result = InboxSavedFilterTransport()
        if self.search:
            result["search"] = self.search
        if self.status:
            result["status"] = self.status
        if self.channel_type:
            result["channel_type"] = self.channel_type
        if self.service_team_id:
            result["service_team_id"] = self.service_team_id
        if self.service_team_ids:
            result["service_team_ids"] = self.service_team_ids
        if self.advanced_filters_json:
            result["filters"] = self.advanced_filters_json
        if self.assigned_person_id:
            result["assigned_person_id"] = self.assigned_person_id
        if self.contact_resolution_status:
            result["contact_resolution_status"] = self.contact_resolution_status
        if self.activity_from:
            result["activity_from"] = self.activity_from
        if self.activity_to:
            result["activity_to"] = self.activity_to
        if self.priority_at_most is not None:
            result["priority_at_most"] = self.priority_at_most
        if self.muted is not None:
            result["muted"] = self.muted
        if self.snoozed is not None:
            result["snoozed"] = self.snoozed
        result["needs_response"] = self.needs_response
        result["needs_attention"] = self.needs_attention
        result["open_only"] = self.open_only
        result["unassigned"] = self.unassigned
        result["unread"] = self.unread
        result["ai_handling"] = self.ai_handling
        result["has_ticket"] = self.has_ticket
        return result


def saved_filter_payload_from_storage(
    value: object,
) -> InboxSavedFilterPayload:
    """Narrow persisted JSON to the current typed saved-view contract."""

    if not isinstance(value, Mapping):
        return InboxSavedFilterPayload()

    def text_value(key: str) -> str | None:
        candidate = value.get(key)
        if not isinstance(candidate, str):
            return None
        return candidate.strip() or None

    def bool_value(key: str, default: bool = False) -> bool:
        candidate = value.get(key)
        return candidate if isinstance(candidate, bool) else default

    def optional_bool_value(key: str) -> bool | None:
        candidate = value.get(key)
        return candidate if isinstance(candidate, bool) else None

    priority_value = value.get("priority_at_most")
    priority = (
        priority_value
        if isinstance(priority_value, int) and not isinstance(priority_value, bool)
        else None
    )
    return InboxSavedFilterPayload(
        search=text_value("search"),
        status=text_value("status"),
        channel_type=text_value("channel_type"),
        service_team_id=text_value("service_team_id"),
        service_team_ids=text_value("service_team_ids"),
        advanced_filters_json=text_value("filters"),
        assigned_person_id=text_value("assigned_person_id"),
        needs_response=bool_value("needs_response"),
        needs_attention=bool_value("needs_attention"),
        contact_resolution_status=text_value("contact_resolution_status"),
        priority_at_most=priority,
        muted=optional_bool_value("muted"),
        snoozed=optional_bool_value("snoozed"),
        open_only=bool_value("open_only"),
        unassigned=bool_value("unassigned"),
        unread=bool_value("unread"),
        ai_handling=bool_value("ai_handling"),
        has_ticket=bool_value("has_ticket"),
        activity_from=text_value("activity_from"),
        activity_to=text_value("activity_to"),
    )


@dataclass(frozen=True, slots=True)
class ServiceTeamFilterCondition:
    operator: ServiceTeamFilterOperator
    team_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class InboxAdvancedFilterQuery:
    """Normalized service-team conditions with shared AND/OR semantics."""

    and_conditions: tuple[ServiceTeamFilterCondition, ...] = ()
    or_conditions: tuple[ServiceTeamFilterCondition, ...] = ()
    or_groups: tuple[tuple[ServiceTeamFilterCondition, ...], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.and_conditions or self.or_conditions or self.or_groups)

    @property
    def referenced_team_ids(self) -> tuple[UUID, ...]:
        seen: set[UUID] = set()
        ordered: list[UUID] = []
        groups = (
            self.and_conditions,
            self.or_conditions,
            *self.or_groups,
        )
        for group in groups:
            for condition in group:
                for team_id in condition.team_ids:
                    if team_id not in seen:
                        seen.add(team_id)
                        ordered.append(team_id)
        return tuple(ordered)

    def canonical_json(self) -> str | None:
        """Return one deterministic URL/saved-view representation."""

        if self.is_empty:
            return None
        entries: list[object] = [
            _transport_row(condition) for condition in self.and_conditions
        ]
        for group in (self.or_conditions, *self.or_groups):
            if group:
                entries.append(
                    {"or": [_transport_row(condition) for condition in group]}
                )
        return json.dumps(entries, separators=(",", ":"), sort_keys=True)


def _error(message: str) -> InboxFilterError:
    return InboxFilterError(code=f"{OWNER}.invalid_filter", message=message)


def _is_null_token(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().casefold() in {
        "",
        "null",
        "none",
        "nil",
    }


def _uuid(value: object) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error("Service Team filters require valid team identifiers.") from exc


def _team_ids(value: object, *, many: bool) -> tuple[UUID, ...]:
    if many:
        if isinstance(value, str):
            raw_values: tuple[object, ...] = tuple(
                part.strip() for part in value.split(",") if part.strip()
            )
        elif isinstance(value, (list, tuple)):
            raw_values = tuple(value)
        else:
            raise _error("Service Team 'in' filters require one or more teams.")
        if not raw_values:
            raise _error("Service Team 'in' filters require one or more teams.")
    else:
        if isinstance(value, (list, tuple, dict)) or _is_null_token(value):
            raise _error("Choose one Service Team for this filter condition.")
        raw_values = (value,)

    seen: set[UUID] = set()
    normalized: list[UUID] = []
    for raw_value in raw_values:
        team_id = _uuid(raw_value)
        if team_id not in seen:
            seen.add(team_id)
            normalized.append(team_id)
    return tuple(normalized)


def _condition(raw: FilterCondition) -> ServiceTeamFilterCondition:
    if raw.doctype.casefold() != INBOX_CONVERSATION_DOCTYPE.casefold():
        raise _error(f"Filter type '{raw.doctype}' is not allowed for the Inbox queue.")
    if raw.field != SERVICE_TEAM_FIELD:
        raise _error(
            f"Inbox field '{raw.field}' is not available as an advanced filter."
        )
    try:
        operator = ServiceTeamFilterOperator(raw.operator)
    except ValueError as exc:
        raise _error(
            f"Operator '{raw.operator}' is not allowed for Service Team."
        ) from exc

    if operator in {
        ServiceTeamFilterOperator.is_empty,
        ServiceTeamFilterOperator.is_not_empty,
    }:
        if not _is_null_token(raw.value):
            raise _error("Service Team 'is' filters support only empty checks.")
        return ServiceTeamFilterCondition(operator=operator)
    if operator in {
        ServiceTeamFilterOperator.in_any,
        ServiceTeamFilterOperator.not_in,
    }:
        return ServiceTeamFilterCondition(
            operator=operator,
            team_ids=_team_ids(raw.value, many=True),
        )
    return ServiceTeamFilterCondition(
        operator=operator,
        team_ids=_team_ids(raw.value, many=False),
    )


def parse_filter_payload(
    payload: InboxAdvancedFilterPayload,
) -> InboxAdvancedFilterQuery:
    """Normalize the shared JSON grammar into the typed Inbox query."""

    try:
        parsed = parse_shared_payload(
            payload.raw_json,
            default_doctype=INBOX_CONVERSATION_DOCTYPE,
        )
    except FilterValidationError as exc:
        raise _error(str(exc)) from exc
    return InboxAdvancedFilterQuery(
        and_conditions=tuple(_condition(item) for item in parsed.and_filters),
        or_conditions=tuple(_condition(item) for item in parsed.or_filters),
        or_groups=tuple(
            tuple(_condition(item) for item in group) for group in parsed.or_groups
        ),
    )


def validate_active_service_teams(
    db: Session,
    query: InboxAdvancedFilterQuery,
) -> tuple[tuple[UUID, str], ...]:
    """Validate referenced IDs and return the authoritative active selector."""

    options = service_team_lifecycle.list_active_team_options(db)
    active_ids = {team_id for team_id, _name in options}
    invalid_ids = tuple(
        team_id for team_id in query.referenced_team_ids if team_id not in active_ids
    )
    if invalid_ids:
        raise _error("Select an active Service Team for every filter condition.")
    return options


def resolve_filter_query(
    db: Session,
    payload: InboxAdvancedFilterPayload,
    *,
    include_selector: bool = True,
) -> tuple[InboxAdvancedFilterQuery, tuple[tuple[UUID, str], ...]]:
    """Resolve one typed query and its active team selector in one owner read."""

    query = parse_filter_payload(payload)
    if not include_selector and not query.referenced_team_ids:
        return query, ()
    options = validate_active_service_teams(db, query)
    return query, options if include_selector else ()


def normalize_saved_filter_payload(
    db: Session,
    payload: InboxSavedFilterPayload,
) -> InboxSavedFilterPayload:
    """Validate and canonicalize the advanced condition before persistence."""

    query, _options = resolve_filter_query(
        db,
        InboxAdvancedFilterPayload(raw_json=payload.advanced_filters_json),
    )
    return replace(payload, advanced_filters_json=query.canonical_json())


def _active_team_link(
    team_ids: tuple[UUID, ...] | None = None,
) -> ColumnElement[bool]:
    statement = select(InboxConversationTeam.id).where(
        InboxConversationTeam.conversation_id == InboxConversation.id,
        InboxConversationTeam.is_active.is_(True),
    )
    if team_ids is not None:
        statement = statement.where(InboxConversationTeam.service_team_id.in_(team_ids))
    return exists(statement)


def _condition_expression(
    condition: ServiceTeamFilterCondition,
) -> ColumnElement[bool]:
    if condition.operator is ServiceTeamFilterOperator.is_empty:
        return ~_active_team_link()
    if condition.operator is ServiceTeamFilterOperator.is_not_empty:
        return _active_team_link()

    matched = _active_team_link(condition.team_ids)
    if condition.operator in {
        ServiceTeamFilterOperator.not_equals,
        ServiceTeamFilterOperator.not_in,
    }:
        return ~matched
    return matched


def build_filter_expression(
    query: InboxAdvancedFilterQuery,
) -> ColumnElement[bool] | None:
    """Build the relationship-aware expression without duplicating rows."""

    expressions: list[ColumnElement[bool]] = []
    if query.and_conditions:
        expressions.append(
            and_(*(_condition_expression(item) for item in query.and_conditions))
        )
    if query.or_conditions:
        expressions.append(
            or_(*(_condition_expression(item) for item in query.or_conditions))
        )
    for group in query.or_groups:
        if group:
            expressions.append(or_(*(_condition_expression(item) for item in group)))
    if not expressions:
        return None
    if len(expressions) == 1:
        return expressions[0]
    return and_(*expressions)


def _transport_row(condition: ServiceTeamFilterCondition) -> tuple[object, ...]:
    if condition.operator in {
        ServiceTeamFilterOperator.is_empty,
        ServiceTeamFilterOperator.is_not_empty,
    }:
        value: object = None
    elif condition.operator in {
        ServiceTeamFilterOperator.in_any,
        ServiceTeamFilterOperator.not_in,
    }:
        value = [str(team_id) for team_id in condition.team_ids]
    else:
        value = str(condition.team_ids[0])
    return (
        INBOX_CONVERSATION_DOCTYPE,
        SERVICE_TEAM_FIELD,
        condition.operator.value,
        value,
    )
