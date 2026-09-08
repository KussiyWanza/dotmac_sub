from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import (
    Integer,
    String,
    and_,
    case,
    cast,
    distinct,
    func,
    literal,
    or_,
    select,
    true,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement, SQLColumnExpression
from sqlalchemy.sql.selectable import CTE
from sqlalchemy.sql.type_api import TypeEngine

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
    InboxStatusTransitionEvent,
)
from app.services import service_team_composition, team_inbox_assignment

DEFAULT_PERFORMANCE_WINDOW_DAYS = 30
MAX_PERFORMANCE_WINDOW_DAYS = 366
DEFAULT_REPORT_LIMIT = 50
MAX_REPORT_LIMIT = 200


class InboxMetricsError(Exception):
    """Stable, transport-neutral validation error for Inbox report queries."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class InboxMetricWindow:
    start_at: datetime
    end_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class InboxPerformanceQuery:
    period_start_at: datetime | None = None
    period_end_at: datetime | None = None
    service_team_id: UUID | None = None
    person_id: UUID | None = None
    include_inactive_teams: bool = False
    include_inactive_members: bool = False
    search: str | None = None
    limit: int | None = DEFAULT_REPORT_LIMIT
    offset: int = 0
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_page(self.limit, self.offset)


@dataclass(frozen=True, slots=True)
class InboxEscalationQuery:
    response_sla_seconds: int | None = None
    queue_sla_seconds: int | None = None
    include_inactive_teams: bool = False
    search: str | None = None
    limit: int | None = DEFAULT_REPORT_LIMIT
    offset: int = 0
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_page(self.limit, self.offset)
        for value in (self.response_sla_seconds, self.queue_sla_seconds):
            if value is not None and value <= 0:
                raise InboxMetricsError(
                    "communications.team_inbox_metrics.invalid_query",
                    "SLA thresholds must be positive seconds.",
                )


@dataclass(frozen=True, slots=True)
class InboxTeamPerformanceMetrics:
    service_team_id: UUID
    conversation_count: int
    open_count: int
    unassigned_open_count: int
    assigned_open_count: int
    inbound_message_count: int
    outbound_message_count: int
    responded_count: int
    response_sla_breached_count: int
    average_first_response_seconds: float | None
    average_queue_wait_seconds: float | None


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceMetrics:
    person_id: UUID
    service_team_id: UUID
    active_assignment_count: int
    handled_conversation_count: int
    resolved_conversation_count: int
    average_first_response_seconds: float | None
    average_queue_wait_seconds: float | None


@dataclass(frozen=True, slots=True)
class InboxTeamPerformanceReportRow:
    service_team_id: UUID
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    response_sla_seconds: int | None
    metrics: InboxTeamPerformanceMetrics


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceReportRow:
    person_id: UUID
    agent_name: str
    service_team_id: UUID
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    metrics: InboxAgentPerformanceMetrics


class InboxAgentPerformanceQueryError(ValueError):
    """Stable validation failure for the bounded agent analytics query."""

    code = "ui.crm_operational_reports.invalid_query"


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceQuery:
    """Typed event-time, identity, search, and pagination boundary."""

    start_at: datetime
    end_at: datetime
    page: int = 1
    per_page: int | None = 50
    person_id: UUID | None = None
    service_team_id: UUID | None = None
    search: str | None = None

    def __post_init__(self) -> None:
        start_at = _as_utc(self.start_at)
        end_at = _as_utc(self.end_at)
        if start_at is None or end_at is None or start_at >= end_at:
            raise InboxAgentPerformanceQueryError(
                "Agent performance requires an increasing bounded date range."
            )
        if self.page < 1:
            raise InboxAgentPerformanceQueryError("Page must be at least one.")
        if self.per_page is not None and not 10 <= self.per_page <= 500:
            raise InboxAgentPerformanceQueryError(
                "Page size must be between 10 and 500."
            )


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceAnalyticsRow:
    person_id: UUID
    agent_name: str
    service_team_id: UUID
    service_team_name: str
    assigned_conversation_count: int
    resolved_conversation_count: int
    active_assignment_count: int
    average_resolution_seconds: float | None
    average_first_response_seconds: float | None
    resolution_observation_count: int = 0
    first_response_observation_count: int = 0
    first_response_sla_seconds: int | None = None
    resolution_sla_seconds: int | None = None
    first_response_sla_met_count: int = 0
    first_response_sla_breached_count: int = 0
    first_response_sla_rate: float | None = None
    resolution_sla_met_count: int = 0
    resolution_sla_breached_count: int = 0
    resolution_sla_rate: float | None = None
    currently_overdue_count: int = 0


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceSummary:
    agent_count: int
    assigned_conversation_count: int
    resolved_conversation_count: int
    active_assignment_count: int
    average_resolution_seconds: float | None
    average_first_response_seconds: float | None
    activity_agent_count: int = 0
    first_response_observation_count: int = 0
    resolution_observation_count: int = 0
    first_response_sla_met_count: int = 0
    first_response_sla_breached_count: int = 0
    first_response_sla_rate: float | None = None
    resolution_sla_met_count: int = 0
    resolution_sla_breached_count: int = 0
    resolution_sla_rate: float | None = None
    currently_overdue_count: int = 0
    first_response_sla_configured: bool = False
    resolution_sla_configured: bool = False

    @property
    def sla_configured(self) -> bool:
        return self.first_response_sla_configured or self.resolution_sla_configured


@dataclass(frozen=True, slots=True)
class InboxAgentPerformanceAnalyticsPage:
    rows: tuple[InboxAgentPerformanceAnalyticsRow, ...]
    summary: InboxAgentPerformanceSummary
    total: int
    page: int
    per_page: int
    generated_at: datetime
    provenance: str = "live authoritative inbox events"

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.per_page < self.total


@dataclass(frozen=True, slots=True)
class InboxAgentConversationEvidence:
    conversation_id: UUID
    subject: str
    service_team_id: UUID
    service_team_name: str
    assigned_at: datetime
    resolved_at: datetime | None
    is_active_assignment: bool
    first_response_seconds: float | None
    resolution_seconds: float | None


@dataclass(frozen=True, slots=True)
class InboxEscalationCandidate:
    conversation_id: UUID
    service_team_id: UUID
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    subject: str | None
    contact_address: str | None
    status: str
    reasons: tuple[str, ...]
    response_sla_seconds: int | None
    queue_sla_seconds: int | None
    pending_response_seconds: float | None
    queue_wait_seconds: float | None
    assigned_person_id: UUID | None
    available_agent_count: int


@dataclass(frozen=True, slots=True)
class InboxTeamPerformancePage:
    query: InboxPerformanceQuery
    window: InboxMetricWindow
    rows: tuple[InboxTeamPerformanceReportRow, ...]
    total_count: int


@dataclass(frozen=True, slots=True)
class InboxAgentPerformancePage:
    query: InboxPerformanceQuery
    window: InboxMetricWindow
    rows: tuple[InboxAgentPerformanceReportRow, ...]
    total_count: int


@dataclass(frozen=True, slots=True)
class InboxEscalationPage:
    query: InboxEscalationQuery
    observed_at: datetime
    rows: tuple[InboxEscalationCandidate, ...]
    total_count: int
    response_breach_count: int
    queue_breach_count: int
    no_agent_count: int


def _validate_page(limit: int | None, offset: int) -> None:
    if offset < 0 or (limit is not None and not 1 <= limit <= MAX_REPORT_LIMIT):
        raise InboxMetricsError(
            "communications.team_inbox_metrics.invalid_query",
            f"Pagination must use offset >= 0 and limit 1-{MAX_REPORT_LIMIT}.",
        )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_performance_window(query: InboxPerformanceQuery) -> InboxMetricWindow:
    observed_at = _as_utc(query.observed_at) or datetime.now(UTC)
    end_at = _as_utc(query.period_end_at) or observed_at
    start_at = _as_utc(query.period_start_at) or (
        end_at - timedelta(days=DEFAULT_PERFORMANCE_WINDOW_DAYS)
    )
    if start_at >= end_at:
        raise InboxMetricsError(
            "communications.team_inbox_metrics.invalid_query",
            "The performance period start must be earlier than its end.",
        )
    if end_at - start_at > timedelta(days=MAX_PERFORMANCE_WINDOW_DAYS):
        raise InboxMetricsError(
            "communications.team_inbox_metrics.invalid_query",
            f"The performance period cannot exceed {MAX_PERFORMANCE_WINDOW_DAYS} days.",
        )
    return InboxMetricWindow(start_at, end_at, observed_at)


def _positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _staff_display_name(user: SystemUser) -> str:
    """Project the canonical staff label without exposing the identity UUID."""

    return (
        (user.display_name or "").strip()
        or f"{user.first_name} {user.last_name}".strip()
        or user.email
    )


def response_sla_seconds_for_team(
    team: ServiceTeam,
    *,
    fallback: int | None = None,
) -> int | None:
    metadata = team.metadata_ or {}
    nested = metadata.get("inbox_sla")
    candidates = [
        metadata.get("inbox_response_sla_seconds"),
        metadata.get("response_sla_seconds"),
    ]
    if isinstance(nested, dict):
        candidates.extend(
            [nested.get("response_sla_seconds"), nested.get("first_response_seconds")]
        )
    for candidate in candidates:
        parsed = _positive_int(candidate)
        if parsed is not None:
            return parsed
    return fallback


def resolution_sla_seconds_for_team(
    team: ServiceTeam,
    *,
    fallback: int | None = None,
) -> int | None:
    """Return the team's configured Inbox resolution SLA, when present."""

    metadata = team.metadata_ or {}
    nested = metadata.get("inbox_sla")
    candidates = [
        metadata.get("inbox_resolution_sla_seconds"),
        metadata.get("resolution_sla_seconds"),
    ]
    if isinstance(nested, dict):
        candidates.append(nested.get("resolution_sla_seconds"))
    for candidate in candidates:
        parsed = _positive_int(candidate)
        if parsed is not None:
            return parsed
    return fallback


def queue_sla_seconds_for_team(
    team: ServiceTeam,
    *,
    fallback: int | None = None,
) -> int | None:
    metadata = team.metadata_ or {}
    nested = metadata.get("inbox_sla")
    candidates = [
        metadata.get("inbox_queue_sla_seconds"),
        metadata.get("queue_sla_seconds"),
    ]
    if isinstance(nested, dict):
        candidates.extend(
            [
                nested.get("queue_sla_seconds"),
                nested.get("assignment_sla_seconds"),
                nested.get("assignment_seconds"),
            ]
        )
    for candidate in candidates:
        parsed = _positive_int(candidate)
        if parsed is not None:
            return parsed
    return fallback


def _load_teams(
    db: Session,
    *,
    include_inactive: bool,
    service_team_id: UUID | None = None,
    search: str | None = None,
) -> list[ServiceTeam]:
    statement = select(ServiceTeam).order_by(
        ServiceTeam.name.asc(), ServiceTeam.id.asc()
    )
    if not include_inactive:
        statement = statement.where(ServiceTeam.is_active.is_(True))
    if service_team_id is not None:
        statement = statement.where(ServiceTeam.id == service_team_id)
    normalized_search = (search or "").strip()
    if normalized_search:
        statement = statement.where(ServiceTeam.name.ilike(f"%{normalized_search}%"))
    return list(db.scalars(statement).all())


def _conversation_scope(
    team_ids: tuple[UUID, ...],
    *,
    window: InboxMetricWindow | None = None,
    unresolved_only: bool = False,
) -> CTE:
    conversation_at = func.coalesce(
        InboxConversation.first_message_at, InboxConversation.created_at
    )
    statement = (
        select(
            InboxConversationTeam.service_team_id.label("service_team_id"),
            InboxConversation.id.label("conversation_id"),
            InboxConversation.status.label("status"),
            InboxConversation.subject.label("subject"),
            InboxConversation.contact_address.label("contact_address"),
            InboxConversation.first_message_at.label("first_message_at"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationTeam.conversation_id,
        )
        .where(
            InboxConversationTeam.service_team_id.in_(team_ids),
            InboxConversationTeam.is_active.is_(True),
        )
    )
    if window is not None:
        statement = statement.where(
            conversation_at >= window.start_at,
            conversation_at < window.end_at,
        )
    if unresolved_only:
        statement = statement.where(
            InboxConversation.status != InboxConversationStatus.resolved.value,
            InboxConversation.is_active.is_(True),
        )
    return statement.cte("inbox_metric_conversation_scope")


@dataclass(frozen=True, slots=True)
class _MessageFacts:
    counts: CTE
    first_inbound: CTE
    first_outbound: CTE
    first_human_outbound: CTE


def _message_facts(scope: CTE) -> _MessageFacts:
    scope_ids = (
        select(scope.c.conversation_id.label("conversation_id"))
        .distinct()
        .cte("inbox_metric_scope_ids")
    )
    message_at = func.coalesce(
        InboxMessage.received_at, InboxMessage.sent_at, InboxMessage.created_at
    )
    counts = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.sum(
                case(
                    (
                        InboxMessage.direction == InboxMessageDirection.inbound.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("inbound_count"),
            func.sum(
                case(
                    (
                        InboxMessage.direction == InboxMessageDirection.outbound.value,
                        1,
                    ),
                    else_=0,
                )
            ).label("outbound_count"),
        )
        .join(
            scope_ids,
            scope_ids.c.conversation_id == InboxMessage.conversation_id,
        )
        .group_by(InboxMessage.conversation_id)
        .cte("inbox_metric_message_counts")
    )
    inbound_ranked = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            message_at.label("inbound_at"),
            func.row_number()
            .over(
                partition_by=InboxMessage.conversation_id,
                order_by=(InboxMessage.created_at.asc(), InboxMessage.id.asc()),
            )
            .label("position"),
        )
        .join(
            scope_ids,
            scope_ids.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .cte("inbox_metric_inbound_ranked")
    )
    first_inbound = (
        select(inbound_ranked.c.conversation_id, inbound_ranked.c.inbound_at)
        .where(inbound_ranked.c.position == 1)
        .cte("inbox_metric_first_inbound")
    )
    outbound_ranked = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            message_at.label("outbound_at"),
            func.row_number()
            .over(
                partition_by=InboxMessage.conversation_id,
                order_by=(InboxMessage.created_at.asc(), InboxMessage.id.asc()),
            )
            .label("position"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(
            InboxMessage.direction == InboxMessageDirection.outbound.value,
            message_at >= first_inbound.c.inbound_at,
        )
        .cte("inbox_metric_outbound_ranked")
    )
    first_outbound = (
        select(outbound_ranked.c.conversation_id, outbound_ranked.c.outbound_at)
        .where(outbound_ranked.c.position == 1)
        .cte("inbox_metric_first_outbound")
    )
    sender_text = InboxMessage.metadata_["sent_by_person_id"].as_string()
    human_ranked = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            sender_text.label("person_id"),
            message_at.label("outbound_at"),
            func.row_number()
            .over(
                partition_by=InboxMessage.conversation_id,
                order_by=(InboxMessage.created_at.asc(), InboxMessage.id.asc()),
            )
            .label("position"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(
            InboxMessage.direction == InboxMessageDirection.outbound.value,
            sender_text.is_not(None),
            message_at >= first_inbound.c.inbound_at,
        )
        .cte("inbox_metric_human_outbound_ranked")
    )
    first_human = (
        select(
            human_ranked.c.conversation_id,
            human_ranked.c.person_id,
            human_ranked.c.outbound_at,
        )
        .where(human_ranked.c.position == 1)
        .cte("inbox_metric_first_human_outbound")
    )
    return _MessageFacts(counts, first_inbound, first_outbound, first_human)


def _duration_seconds(
    db: Session,
    end_at: Any,
    start_at: Any,
) -> ColumnElement[float]:
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return type_cast(ColumnElement[float], func.extract("epoch", end_at - start_at))
    return type_cast(
        ColumnElement[float],
        (func.julianday(end_at) - func.julianday(start_at)) * 86400.0,
    )


def _case_by_team(
    team_values: Mapping[UUID, int | None],
    team_column: ColumnElement[UUID],
) -> ColumnElement[int | None]:
    return case(
        *(
            (team_column == team_id, literal(value, type_=Integer))
            for team_id, value in team_values.items()
        ),
        else_=None,
    )


def _team_metrics_by_id(
    db: Session,
    teams: list[ServiceTeam],
    *,
    window: InboxMetricWindow,
    default_response_sla_seconds: int | None,
) -> dict[UUID, InboxTeamPerformanceMetrics]:
    if not teams:
        return {}
    team_ids = tuple(team.id for team in teams)
    scope = _conversation_scope(team_ids, window=window)
    messages = _message_facts(scope)
    active_assignment = (
        select(
            InboxConversationAssignment.conversation_id.label("conversation_id"),
            InboxConversationAssignment.service_team_id.label("service_team_id"),
            InboxConversationAssignment.assigned_at.label("assigned_at"),
        )
        .where(
            InboxConversationAssignment.service_team_id.in_(team_ids),
            InboxConversationAssignment.is_active.is_(True),
        )
        .subquery("inbox_metric_active_assignment")
    )
    response_slas = {
        team.id: response_sla_seconds_for_team(
            team, fallback=default_response_sla_seconds
        )
        for team in teams
    }
    response_sla = _case_by_team(response_slas, scope.c.service_team_id)
    response_seconds = _duration_seconds(
        db, messages.first_outbound.c.outbound_at, messages.first_inbound.c.inbound_at
    )
    pending_seconds = _duration_seconds(
        db, literal(window.observed_at), messages.first_inbound.c.inbound_at
    )
    queue_wait_seconds = _duration_seconds(
        db, active_assignment.c.assigned_at, scope.c.first_message_at
    )
    is_open = scope.c.status != InboxConversationStatus.resolved.value
    response_breached = and_(
        messages.first_inbound.c.inbound_at.is_not(None),
        response_sla.is_not(None),
        or_(
            and_(
                messages.first_outbound.c.outbound_at.is_not(None),
                response_seconds > response_sla,
            ),
            and_(
                messages.first_outbound.c.outbound_at.is_(None),
                pending_seconds > response_sla,
            ),
        ),
    )
    statement = (
        select(
            scope.c.service_team_id,
            func.count(scope.c.conversation_id).label("conversation_count"),
            func.sum(case((is_open, 1), else_=0)).label("open_count"),
            func.sum(
                case(
                    (
                        and_(is_open, active_assignment.c.conversation_id.is_not(None)),
                        1,
                    ),
                    else_=0,
                )
            ).label("assigned_open_count"),
            func.coalesce(func.sum(messages.counts.c.inbound_count), 0).label(
                "inbound_message_count"
            ),
            func.coalesce(func.sum(messages.counts.c.outbound_count), 0).label(
                "outbound_message_count"
            ),
            func.sum(
                case((messages.first_outbound.c.outbound_at.is_not(None), 1), else_=0)
            ).label("responded_count"),
            func.sum(case((response_breached, 1), else_=0)).label(
                "response_sla_breached_count"
            ),
            func.avg(
                case(
                    (
                        messages.first_outbound.c.outbound_at.is_not(None),
                        response_seconds,
                    ),
                    else_=None,
                )
            ).label("average_first_response_seconds"),
            func.avg(
                case(
                    (
                        active_assignment.c.conversation_id.is_not(None),
                        queue_wait_seconds,
                    ),
                    else_=None,
                )
            ).label("average_queue_wait_seconds"),
        )
        .select_from(scope)
        .outerjoin(
            messages.counts,
            messages.counts.c.conversation_id == scope.c.conversation_id,
        )
        .outerjoin(
            messages.first_inbound,
            messages.first_inbound.c.conversation_id == scope.c.conversation_id,
        )
        .outerjoin(
            messages.first_outbound,
            messages.first_outbound.c.conversation_id == scope.c.conversation_id,
        )
        .outerjoin(
            active_assignment,
            and_(
                active_assignment.c.conversation_id == scope.c.conversation_id,
                active_assignment.c.service_team_id == scope.c.service_team_id,
            ),
        )
        .group_by(scope.c.service_team_id)
    )
    result: dict[UUID, InboxTeamPerformanceMetrics] = {}
    for row in db.execute(statement).mappings():
        team_id = UUID(str(row["service_team_id"]))
        open_count = int(row["open_count"] or 0)
        assigned_open_count = int(row["assigned_open_count"] or 0)
        result[team_id] = InboxTeamPerformanceMetrics(
            service_team_id=team_id,
            conversation_count=int(row["conversation_count"] or 0),
            open_count=open_count,
            unassigned_open_count=max(open_count - assigned_open_count, 0),
            assigned_open_count=assigned_open_count,
            inbound_message_count=int(row["inbound_message_count"] or 0),
            outbound_message_count=int(row["outbound_message_count"] or 0),
            responded_count=int(row["responded_count"] or 0),
            response_sla_breached_count=int(row["response_sla_breached_count"] or 0),
            average_first_response_seconds=(
                round(float(row["average_first_response_seconds"]), 3)
                if row["average_first_response_seconds"] is not None
                else None
            ),
            average_queue_wait_seconds=(
                round(float(row["average_queue_wait_seconds"]), 3)
                if row["average_queue_wait_seconds"] is not None
                else None
            ),
        )
    return result


def team_performance_page(
    db: Session,
    *,
    query: InboxPerformanceQuery,
    response_sla_seconds: int | None = None,
) -> InboxTeamPerformancePage:
    window = resolve_performance_window(query)
    teams = _load_teams(
        db,
        include_inactive=query.include_inactive_teams,
        service_team_id=query.service_team_id,
        search=query.search,
    )
    metrics_by_id = _team_metrics_by_id(
        db,
        teams,
        window=window,
        default_response_sla_seconds=response_sla_seconds,
    )
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db, tuple(team.id for team in teams)
    )
    all_rows = tuple(
        InboxTeamPerformanceReportRow(
            service_team_id=team.id,
            service_team_name=team.name,
            service_team_capabilities=tuple(
                capability.value for capability in capabilities_by_team[team.id]
            ),
            response_sla_seconds=response_sla_seconds_for_team(
                team, fallback=response_sla_seconds
            ),
            metrics=metrics_by_id.get(
                team.id,
                InboxTeamPerformanceMetrics(
                    service_team_id=team.id,
                    conversation_count=0,
                    open_count=0,
                    unassigned_open_count=0,
                    assigned_open_count=0,
                    inbound_message_count=0,
                    outbound_message_count=0,
                    responded_count=0,
                    response_sla_breached_count=0,
                    average_first_response_seconds=None,
                    average_queue_wait_seconds=None,
                ),
            ),
        )
        for team in teams
    )
    rows = (
        all_rows[query.offset : query.offset + query.limit]
        if query.limit is not None
        else all_rows[query.offset :]
    )
    return InboxTeamPerformancePage(query, window, rows, len(all_rows))


def _member_rows(
    db: Session, query: InboxPerformanceQuery
) -> tuple[list[tuple[ServiceTeamMember, ServiceTeam, SystemUser]], int]:
    statement = (
        select(ServiceTeamMember, ServiceTeam, SystemUser)
        .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
        .join(SystemUser, SystemUser.person_party_id == ServiceTeamMember.person_id)
        .where(ServiceTeam.is_active.is_(True), SystemUser.is_active.is_(True))
    )
    if query.service_team_id is not None:
        statement = statement.where(ServiceTeam.id == query.service_team_id)
    if query.person_id is not None:
        statement = statement.where(SystemUser.id == query.person_id)
    if not query.include_inactive_members:
        statement = statement.where(ServiceTeamMember.is_active.is_(True))
    normalized_search = (query.search or "").strip()
    if normalized_search:
        search_term = f"%{normalized_search}%"
        statement = statement.where(
            or_(
                SystemUser.display_name.ilike(search_term),
                SystemUser.first_name.ilike(search_term),
                SystemUser.last_name.ilike(search_term),
                SystemUser.email.ilike(search_term),
            )
        )
    count_statement = select(func.count()).select_from(
        statement.order_by(None).subquery()
    )
    total_count = int(db.scalar(count_statement) or 0)
    statement = statement.order_by(
        ServiceTeam.name.asc(), ServiceTeamMember.created_at.asc(), SystemUser.id.asc()
    )
    if query.limit is not None:
        statement = statement.offset(query.offset).limit(query.limit)
    elif query.offset:
        statement = statement.offset(query.offset)
    rows = [(member, team, user) for member, team, user in db.execute(statement).all()]
    return rows, total_count


def _agent_metrics_by_key(
    db: Session,
    *,
    pairs: tuple[tuple[UUID, UUID], ...],
    window: InboxMetricWindow,
) -> dict[tuple[UUID, UUID], InboxAgentPerformanceMetrics]:
    if not pairs:
        return {}
    team_ids = tuple(sorted({team_id for team_id, _person_id in pairs}, key=str))
    person_ids = tuple(sorted({person_id for _team_id, person_id in pairs}, key=str))
    conversation_at = func.coalesce(
        InboxConversation.first_message_at, InboxConversation.created_at
    )
    queue_wait = _duration_seconds(
        db, InboxConversationAssignment.assigned_at, InboxConversation.first_message_at
    )
    assignment_statement = (
        select(
            InboxConversationAssignment.service_team_id.label("service_team_id"),
            InboxConversationAssignment.person_id.label("person_id"),
            func.sum(
                case((InboxConversationAssignment.is_active.is_(True), 1), else_=0)
            ).label("active_assignment_count"),
            func.count(
                func.distinct(InboxConversationAssignment.conversation_id)
            ).label("handled_conversation_count"),
            func.count(
                func.distinct(
                    case(
                        (
                            InboxConversation.status
                            == InboxConversationStatus.resolved.value,
                            InboxConversationAssignment.conversation_id,
                        ),
                        else_=None,
                    )
                )
            ).label("resolved_conversation_count"),
            func.avg(queue_wait).label("average_queue_wait_seconds"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .where(
            InboxConversationAssignment.service_team_id.in_(team_ids),
            InboxConversationAssignment.person_id.in_(person_ids),
            conversation_at >= window.start_at,
            conversation_at < window.end_at,
        )
        .group_by(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
        )
    )
    assignment_metrics = {
        (UUID(str(row.service_team_id)), UUID(str(row.person_id))): row
        for row in db.execute(assignment_statement)
    }
    scope = _conversation_scope(team_ids, window=window)
    messages = _message_facts(scope)
    first_human_response = _duration_seconds(
        db,
        messages.first_human_outbound.c.outbound_at,
        messages.first_inbound.c.inbound_at,
    )
    response_statement = (
        select(
            scope.c.service_team_id,
            messages.first_human_outbound.c.person_id,
            func.avg(first_human_response).label("average_first_response_seconds"),
        )
        .select_from(scope)
        .join(
            messages.first_inbound,
            messages.first_inbound.c.conversation_id == scope.c.conversation_id,
        )
        .join(
            messages.first_human_outbound,
            messages.first_human_outbound.c.conversation_id == scope.c.conversation_id,
        )
        .where(
            messages.first_human_outbound.c.person_id.in_(
                tuple(str(person_id) for person_id in person_ids)
            )
        )
        .group_by(scope.c.service_team_id, messages.first_human_outbound.c.person_id)
    )
    response_metrics: dict[tuple[UUID, UUID], float] = {}
    for row in db.execute(response_statement):
        try:
            key = (UUID(str(row.service_team_id)), UUID(str(row.person_id)))
        except (TypeError, ValueError):
            continue
        response_metrics[key] = round(float(row.average_first_response_seconds), 3)
    result: dict[tuple[UUID, UUID], InboxAgentPerformanceMetrics] = {}
    for team_id, person_id in pairs:
        key = (team_id, person_id)
        assignment = assignment_metrics.get(key)
        queue_seconds = (
            round(float(assignment.average_queue_wait_seconds), 3)
            if assignment is not None
            and assignment.average_queue_wait_seconds is not None
            else None
        )
        result[key] = InboxAgentPerformanceMetrics(
            person_id=person_id,
            service_team_id=team_id,
            active_assignment_count=(
                int(assignment.active_assignment_count or 0)
                if assignment is not None
                else 0
            ),
            handled_conversation_count=(
                int(assignment.handled_conversation_count or 0)
                if assignment is not None
                else 0
            ),
            resolved_conversation_count=(
                int(assignment.resolved_conversation_count or 0)
                if assignment is not None
                else 0
            ),
            average_first_response_seconds=response_metrics.get(key),
            average_queue_wait_seconds=queue_seconds,
        )
    return result


def agent_performance_page(
    db: Session,
    *,
    query: InboxPerformanceQuery,
) -> InboxAgentPerformancePage:
    window = resolve_performance_window(query)
    members, total_count = _member_rows(db, query)
    if not members:
        return InboxAgentPerformancePage(query, window, (), total_count)
    team_ids = tuple(sorted({team.id for _member, team, _user in members}, key=str))
    metrics_by_key = _agent_metrics_by_key(
        db,
        pairs=tuple((team.id, user.id) for _member, team, user in members),
        window=window,
    )
    capabilities_by_team = service_team_composition.capabilities_by_team(db, team_ids)
    rows: list[InboxAgentPerformanceReportRow] = []
    for _member, team, user in members:
        key = (team.id, user.id)
        metrics = metrics_by_key[key]
        rows.append(
            InboxAgentPerformanceReportRow(
                person_id=user.id,
                agent_name=_staff_display_name(user),
                service_team_id=team.id,
                service_team_name=team.name,
                service_team_capabilities=tuple(
                    capability.value for capability in capabilities_by_team[team.id]
                ),
                metrics=metrics,
            )
        )
    return InboxAgentPerformancePage(query, window, tuple(rows), total_count)


def escalation_page(
    db: Session,
    *,
    query: InboxEscalationQuery,
) -> InboxEscalationPage:
    observed_at = _as_utc(query.observed_at) or datetime.now(UTC)
    teams = _load_teams(db, include_inactive=query.include_inactive_teams)
    if not teams:
        return InboxEscalationPage(query, observed_at, (), 0, 0, 0, 0)
    team_ids = tuple(team.id for team in teams)
    capabilities_by_team = service_team_composition.capabilities_by_team(db, team_ids)
    capacities = team_inbox_assignment.team_capacity_snapshots(
        db, team_ids, now=observed_at
    )
    response_slas = {
        team.id: response_sla_seconds_for_team(
            team, fallback=query.response_sla_seconds
        )
        for team in teams
    }
    queue_slas = {
        team.id: queue_sla_seconds_for_team(team, fallback=query.queue_sla_seconds)
        for team in teams
    }
    available_agents = {
        team.id: capacities.get(
            team.id, team_inbox_assignment.InboxTeamCapacitySnapshot(0, 0)
        ).available_agent_count
        for team in teams
    }
    scope = _conversation_scope(team_ids, unresolved_only=True)
    messages = _message_facts(scope)
    active_assignment = (
        select(
            InboxConversationAssignment.conversation_id.label("conversation_id"),
            InboxConversationAssignment.service_team_id.label("service_team_id"),
            InboxConversationAssignment.person_id.label("person_id"),
            InboxConversationAssignment.assigned_at.label("assigned_at"),
        )
        .where(
            InboxConversationAssignment.service_team_id.in_(team_ids),
            InboxConversationAssignment.is_active.is_(True),
        )
        .subquery("inbox_escalation_active_assignment")
    )
    response_sla = _case_by_team(response_slas, scope.c.service_team_id)
    queue_sla = _case_by_team(queue_slas, scope.c.service_team_id)
    available_agent_count = _case_by_team(available_agents, scope.c.service_team_id)
    pending_seconds = _duration_seconds(
        db, literal(observed_at), messages.first_inbound.c.inbound_at
    )
    queue_wait_seconds = _duration_seconds(
        db,
        func.coalesce(active_assignment.c.assigned_at, literal(observed_at)),
        scope.c.first_message_at,
    )
    response_breach = and_(
        messages.first_inbound.c.inbound_at.is_not(None),
        messages.first_outbound.c.outbound_at.is_(None),
        response_sla.is_not(None),
        pending_seconds > response_sla,
    )
    queue_breach = and_(
        active_assignment.c.conversation_id.is_(None),
        queue_sla.is_not(None),
        queue_wait_seconds > queue_sla,
    )
    no_agent = and_(
        active_assignment.c.conversation_id.is_(None),
        available_agent_count == 0,
    )
    candidate_statement = (
        select(
            scope.c.conversation_id,
            scope.c.service_team_id,
            scope.c.subject,
            scope.c.contact_address,
            scope.c.status,
            response_sla.label("response_sla_seconds"),
            queue_sla.label("queue_sla_seconds"),
            case(
                (messages.first_outbound.c.outbound_at.is_(None), pending_seconds),
                else_=None,
            ).label("pending_response_seconds"),
            queue_wait_seconds.label("queue_wait_seconds"),
            active_assignment.c.person_id.label("assigned_person_id"),
            available_agent_count.label("available_agent_count"),
            response_breach.label("response_breach"),
            queue_breach.label("queue_breach"),
            no_agent.label("no_agent"),
        )
        .select_from(scope)
        .outerjoin(
            messages.first_inbound,
            messages.first_inbound.c.conversation_id == scope.c.conversation_id,
        )
        .outerjoin(
            messages.first_outbound,
            messages.first_outbound.c.conversation_id == scope.c.conversation_id,
        )
        .outerjoin(
            active_assignment,
            and_(
                active_assignment.c.conversation_id == scope.c.conversation_id,
                active_assignment.c.service_team_id == scope.c.service_team_id,
            ),
        )
        .where(or_(response_breach, queue_breach, no_agent))
    )
    normalized_search = (query.search or "").strip()
    if normalized_search:
        search_term = f"%{normalized_search}%"
        matching_team_ids = tuple(
            team.id
            for team in teams
            if normalized_search.casefold() in team.name.casefold()
        )
        candidate_statement = candidate_statement.where(
            or_(
                scope.c.subject.ilike(search_term),
                scope.c.contact_address.ilike(search_term),
                scope.c.status.ilike(search_term),
                scope.c.service_team_id.in_(matching_team_ids),
            )
        )
    candidates = candidate_statement.subquery("inbox_escalation_candidates")
    summary = db.execute(
        select(
            func.count().label("total_count"),
            func.sum(case((candidates.c.response_breach.is_(True), 1), else_=0)).label(
                "response_breach_count"
            ),
            func.sum(case((candidates.c.queue_breach.is_(True), 1), else_=0)).label(
                "queue_breach_count"
            ),
            func.sum(case((candidates.c.no_agent.is_(True), 1), else_=0)).label(
                "no_agent_count"
            ),
        ).select_from(candidates)
    ).one()
    page_statement = select(candidates).order_by(
        candidates.c.response_breach.desc(),
        candidates.c.pending_response_seconds.desc(),
        candidates.c.queue_wait_seconds.desc(),
        candidates.c.service_team_id.asc(),
        candidates.c.conversation_id.asc(),
    )
    if query.limit is not None:
        page_statement = page_statement.offset(query.offset).limit(query.limit)
    elif query.offset:
        page_statement = page_statement.offset(query.offset)
    team_by_id = {team.id: team for team in teams}
    rows: list[InboxEscalationCandidate] = []
    for row in db.execute(page_statement).mappings():
        team_id = UUID(str(row["service_team_id"]))
        reasons = tuple(
            reason
            for reason, applies in (
                ("response_sla_breached", bool(row["response_breach"])),
                ("unassigned_queue_breached", bool(row["queue_breach"])),
                ("no_available_agent", bool(row["no_agent"])),
            )
            if applies
        )
        rows.append(
            InboxEscalationCandidate(
                conversation_id=UUID(str(row["conversation_id"])),
                service_team_id=team_id,
                service_team_name=team_by_id[team_id].name,
                service_team_capabilities=tuple(
                    capability.value for capability in capabilities_by_team[team_id]
                ),
                subject=row["subject"],
                contact_address=row["contact_address"],
                status=str(row["status"]),
                reasons=reasons,
                response_sla_seconds=(
                    int(row["response_sla_seconds"])
                    if row["response_sla_seconds"] is not None
                    else None
                ),
                queue_sla_seconds=(
                    int(row["queue_sla_seconds"])
                    if row["queue_sla_seconds"] is not None
                    else None
                ),
                pending_response_seconds=(
                    round(max(float(row["pending_response_seconds"]), 0.0), 3)
                    if row["pending_response_seconds"] is not None
                    else None
                ),
                queue_wait_seconds=(
                    round(max(float(row["queue_wait_seconds"]), 0.0), 3)
                    if row["queue_wait_seconds"] is not None
                    else None
                ),
                assigned_person_id=(
                    UUID(str(row["assigned_person_id"]))
                    if row["assigned_person_id"] is not None
                    else None
                ),
                available_agent_count=int(row["available_agent_count"] or 0),
            )
        )
    return InboxEscalationPage(
        query=query,
        observed_at=observed_at,
        rows=tuple(rows),
        total_count=int(summary.total_count or 0),
        response_breach_count=int(summary.response_breach_count or 0),
        queue_breach_count=int(summary.queue_breach_count or 0),
        no_agent_count=int(summary.no_agent_count or 0),
    )


def _message_time_sql():
    return func.coalesce(
        InboxMessage.received_at, InboxMessage.sent_at, InboxMessage.created_at
    )


def _seconds_sql(db: Session, end, start):
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return (func.julianday(end) - func.julianday(start)) * 86400.0
    return func.extract("epoch", end - start)


def _numeric(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float | str | bytes | bytearray):
        return float(value)
    return None


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _uuid_value(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _team_performance_by_team(
    db: Session,
    team_ids: tuple[UUID, ...],
    *,
    response_sla_seconds_by_team: dict[UUID, int | None],
    now: datetime | None = None,
) -> dict[UUID, InboxTeamPerformanceMetrics]:
    now_utc = _as_utc(now) or datetime.now(UTC)
    empty = {
        team_id: InboxTeamPerformanceMetrics(
            service_team_id=team_id,
            conversation_count=0,
            open_count=0,
            unassigned_open_count=0,
            assigned_open_count=0,
            inbound_message_count=0,
            outbound_message_count=0,
            responded_count=0,
            response_sla_breached_count=0,
            average_first_response_seconds=None,
            average_queue_wait_seconds=None,
        )
        for team_id in team_ids
    }
    if not team_ids:
        return empty

    links = (
        select(
            InboxConversationTeam.service_team_id.label("team_id"),
            InboxConversationTeam.conversation_id.label("conversation_id"),
            InboxConversation.status.label("status"),
            InboxConversation.first_message_at.label("first_message_at"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationTeam.conversation_id,
        )
        .where(InboxConversationTeam.service_team_id.in_(team_ids))
        .where(InboxConversationTeam.is_active.is_(True))
        .subquery()
    )
    conversation_scope = select(
        distinct(links.c.conversation_id).label("conversation_id")
    ).subquery()

    queue_wait = _seconds_sql(
        db,
        InboxConversationAssignment.assigned_at,
        links.c.first_message_at,
    )
    conversation_rows = db.execute(
        select(
            links.c.team_id,
            func.count(links.c.conversation_id).label("conversation_count"),
            func.sum(
                case(
                    (links.c.status != InboxConversationStatus.resolved.value, 1),
                    else_=0,
                )
            ).label("open_count"),
            func.sum(
                case(
                    (
                        (links.c.status != InboxConversationStatus.resolved.value)
                        & (InboxConversationAssignment.id.is_not(None)),
                        1,
                    ),
                    else_=0,
                )
            ).label("assigned_open_count"),
            func.avg(queue_wait).label("average_queue_wait_seconds"),
        )
        .select_from(
            links.outerjoin(
                InboxConversationAssignment,
                (InboxConversationAssignment.conversation_id == links.c.conversation_id)
                & (InboxConversationAssignment.is_active.is_(True)),
            )
        )
        .group_by(links.c.team_id)
    ).all()
    conversation_values: dict[UUID, dict[str, float | int | None]] = {}
    for row in conversation_rows:
        conversation_count = int(row.conversation_count or 0)
        open_count = int(row.open_count or 0)
        assigned_open_count = int(row.assigned_open_count or 0)
        conversation_values[_uuid_value(row.team_id)] = {
            "conversation_count": conversation_count,
            "open_count": open_count,
            "assigned_open_count": assigned_open_count,
            "unassigned_open_count": open_count - assigned_open_count,
            "average_queue_wait_seconds": _numeric(row.average_queue_wait_seconds),
        }

    message_rows = db.execute(
        select(
            links.c.team_id,
            func.sum(
                case(
                    (InboxMessage.direction == InboxMessageDirection.inbound.value, 1),
                    else_=0,
                )
            ).label("inbound_count"),
            func.sum(
                case(
                    (InboxMessage.direction == InboxMessageDirection.outbound.value, 1),
                    else_=0,
                )
            ).label("outbound_count"),
        )
        .select_from(
            links.join(
                InboxMessage,
                InboxMessage.conversation_id == links.c.conversation_id,
            )
        )
        .group_by(links.c.team_id)
    ).all()
    message_values = {
        _uuid_value(row.team_id): {
            "inbound_message_count": int(row.inbound_count or 0),
            "outbound_message_count": int(row.outbound_count or 0),
        }
        for row in message_rows
    }

    message_time = _message_time_sql()
    first_inbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(message_time).label("first_inbound_at"),
        )
        .join(
            conversation_scope,
            conversation_scope.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )
    outbound_time = _message_time_sql()
    first_outbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(outbound_time).label("first_outbound_at"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .where(outbound_time >= first_inbound.c.first_inbound_at)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )
    response_seconds = _seconds_sql(
        db,
        first_outbound.c.first_outbound_at,
        first_inbound.c.first_inbound_at,
    )
    pending_seconds = _seconds_sql(
        db, literal(now_utc), first_inbound.c.first_inbound_at
    )
    response_rows = db.execute(
        select(
            links.c.team_id,
            first_outbound.c.first_outbound_at,
            response_seconds.label("response_seconds"),
            pending_seconds.label("pending_seconds"),
        ).select_from(
            links.join(
                first_inbound,
                first_inbound.c.conversation_id == links.c.conversation_id,
            ).outerjoin(
                first_outbound,
                first_outbound.c.conversation_id == links.c.conversation_id,
            )
        )
    ).all()

    response_values_by_team: dict[UUID, list[float]] = {
        team_id: [] for team_id in team_ids
    }
    response_breaches_by_team = dict.fromkeys(team_ids, 0)
    for row in response_rows:
        team_id = _uuid_value(row.team_id)
        sla = response_sla_seconds_by_team.get(team_id)
        if row.first_outbound_at is not None:
            value = float(row.response_seconds or 0)
            response_values_by_team[team_id].append(value)
            if sla is not None and value > sla:
                response_breaches_by_team[team_id] += 1
        elif (
            sla is not None
            and row.pending_seconds is not None
            and float(row.pending_seconds) > sla
        ):
            response_breaches_by_team[team_id] += 1

    return {
        team_id: InboxTeamPerformanceMetrics(
            service_team_id=team_id,
            conversation_count=int(
                conversation_values.get(team_id, {}).get("conversation_count") or 0
            ),
            open_count=int(conversation_values.get(team_id, {}).get("open_count") or 0),
            unassigned_open_count=int(
                conversation_values.get(team_id, {}).get("unassigned_open_count") or 0
            ),
            assigned_open_count=int(
                conversation_values.get(team_id, {}).get("assigned_open_count") or 0
            ),
            inbound_message_count=int(
                message_values.get(team_id, {}).get("inbound_message_count") or 0
            ),
            outbound_message_count=int(
                message_values.get(team_id, {}).get("outbound_message_count") or 0
            ),
            responded_count=len(response_values_by_team[team_id]),
            response_sla_breached_count=response_breaches_by_team[team_id],
            average_first_response_seconds=_avg(response_values_by_team[team_id]),
            average_queue_wait_seconds=conversation_values.get(team_id, {}).get(
                "average_queue_wait_seconds"
            ),
        )
        for team_id in team_ids
    }


def _assignment_values_by_agent(
    db: Session,
    team_ids: tuple[UUID, ...],
) -> dict[tuple[UUID, UUID], dict[str, float | int | None]]:
    if not team_ids:
        return {}

    queue_wait = _seconds_sql(
        db,
        InboxConversationAssignment.assigned_at,
        InboxConversation.first_message_at,
    )
    resolved_conversation_id = case(
        (
            InboxConversation.status == InboxConversationStatus.resolved.value,
            InboxConversationAssignment.conversation_id,
        ),
        else_=None,
    )
    rows = db.execute(
        select(
            InboxConversationAssignment.service_team_id.label("team_id"),
            InboxConversationAssignment.person_id.label("person_id"),
            func.sum(
                case((InboxConversationAssignment.is_active.is_(True), 1), else_=0)
            ).label("active_assignment_count"),
            func.count(distinct(InboxConversationAssignment.conversation_id)).label(
                "handled_conversation_count"
            ),
            func.count(distinct(resolved_conversation_id)).label(
                "resolved_conversation_count"
            ),
            func.avg(queue_wait).label("average_queue_wait_seconds"),
        )
        .select_from(InboxConversationAssignment)
        .outerjoin(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .where(InboxConversationAssignment.service_team_id.in_(team_ids))
        .group_by(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
        )
    ).all()

    return {
        (_uuid_value(row.team_id), _uuid_value(row.person_id)): {
            "active_assignment_count": int(row.active_assignment_count or 0),
            "handled_conversation_count": int(row.handled_conversation_count or 0),
            "resolved_conversation_count": int(row.resolved_conversation_count or 0),
            "average_queue_wait_seconds": _numeric(row.average_queue_wait_seconds),
        }
        for row in rows
    }


def _human_response_seconds_by_agent(
    db: Session,
    team_ids: tuple[UUID, ...],
) -> dict[tuple[UUID, UUID], list[float]]:
    if not team_ids:
        return {}

    links = (
        select(
            InboxConversationTeam.service_team_id.label("team_id"),
            InboxConversationTeam.conversation_id.label("conversation_id"),
        )
        .where(InboxConversationTeam.service_team_id.in_(team_ids))
        .where(InboxConversationTeam.is_active.is_(True))
        .subquery()
    )
    conversation_scope = select(
        distinct(links.c.conversation_id).label("conversation_id")
    ).subquery()

    message_time = _message_time_sql()
    first_inbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(message_time).label("first_inbound_at"),
        )
        .join(
            conversation_scope,
            conversation_scope.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )

    outbound_time = _message_time_sql()
    sent_by_person = InboxMessage.metadata_["sent_by_person_id"].as_string()
    first_human_outbound = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(outbound_time).label("first_outbound_at"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .where(sent_by_person.is_not(None))
        .where(sent_by_person != "")
        .where(outbound_time >= first_inbound.c.first_inbound_at)
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )

    sender_at_first_response = (
        select(
            InboxMessage.conversation_id.label("conversation_id"),
            func.min(InboxMessage.metadata_["sent_by_person_id"].as_string()).label(
                "person_id"
            ),
        )
        .join(
            first_human_outbound,
            (first_human_outbound.c.conversation_id == InboxMessage.conversation_id)
            & (_message_time_sql() == first_human_outbound.c.first_outbound_at),
        )
        .where(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .where(InboxMessage.metadata_["sent_by_person_id"].as_string().is_not(None))
        .where(InboxMessage.metadata_["sent_by_person_id"].as_string() != "")
        .group_by(InboxMessage.conversation_id)
        .subquery()
    )

    response_seconds = _seconds_sql(
        db,
        first_human_outbound.c.first_outbound_at,
        first_inbound.c.first_inbound_at,
    )
    response_rows = db.execute(
        select(
            links.c.team_id,
            sender_at_first_response.c.person_id,
            response_seconds.label("response_seconds"),
        ).select_from(
            links.join(
                first_inbound,
                first_inbound.c.conversation_id == links.c.conversation_id,
            )
            .join(
                first_human_outbound,
                first_human_outbound.c.conversation_id == links.c.conversation_id,
            )
            .join(
                sender_at_first_response,
                sender_at_first_response.c.conversation_id == links.c.conversation_id,
            )
        )
    ).all()

    values: dict[tuple[UUID, UUID], list[float]] = {}
    for row in response_rows:
        try:
            key = (_uuid_value(row.team_id), _uuid_value(row.person_id))
        except (TypeError, ValueError):
            continue
        if row.response_seconds is not None:
            values.setdefault(key, []).append(float(row.response_seconds))
    return values


def team_performance_metrics(
    db: Session,
    service_team_id: str | UUID,
    *,
    response_sla_seconds: int | None = None,
    now: datetime | None = None,
) -> InboxTeamPerformanceMetrics:
    team_id = UUID(str(service_team_id))
    page = team_performance_page(
        db,
        query=InboxPerformanceQuery(
            service_team_id=team_id,
            include_inactive_teams=True,
            limit=1,
            observed_at=now,
        ),
        response_sla_seconds=response_sla_seconds,
    )
    if page.rows:
        return page.rows[0].metrics
    return InboxTeamPerformanceMetrics(team_id, 0, 0, 0, 0, 0, 0, 0, 0, None, None)


def agent_performance_metrics(
    db: Session,
    *,
    service_team_id: str | UUID,
    person_id: str | UUID,
    now: datetime | None = None,
) -> InboxAgentPerformanceMetrics:
    team_id = UUID(str(service_team_id))
    target_person_id = UUID(str(person_id))
    query = InboxPerformanceQuery(
        service_team_id=team_id,
        person_id=target_person_id,
        include_inactive_members=True,
        limit=None,
        observed_at=now,
    )
    metrics = _agent_metrics_by_key(
        db,
        pairs=((team_id, target_person_id),),
        window=resolve_performance_window(query),
    )
    return metrics[(team_id, target_person_id)]


def team_performance_report(
    db: Session,
    *,
    response_sla_seconds: int | None = None,
    include_inactive: bool = False,
    now: datetime | None = None,
    period_start_at: datetime | None = None,
    period_end_at: datetime | None = None,
) -> list[InboxTeamPerformanceReportRow]:
    return list(
        team_performance_page(
            db,
            query=InboxPerformanceQuery(
                period_start_at=period_start_at,
                period_end_at=period_end_at,
                include_inactive_teams=include_inactive,
                limit=None,
                observed_at=now,
            ),
            response_sla_seconds=response_sla_seconds,
        ).rows
    )


def agent_performance_report(
    db: Session,
    *,
    service_team_id: str | UUID | None = None,
    include_inactive_members: bool = False,
    search: str | None = None,
    period_start_at: datetime | None = None,
    period_end_at: datetime | None = None,
    now: datetime | None = None,
) -> list[InboxAgentPerformanceReportRow]:
    return list(
        agent_performance_page(
            db,
            query=InboxPerformanceQuery(
                period_start_at=period_start_at,
                period_end_at=period_end_at,
                service_team_id=(
                    UUID(str(service_team_id)) if service_team_id is not None else None
                ),
                include_inactive_members=include_inactive_members,
                search=search,
                limit=None,
                observed_at=now,
            ),
        ).rows
    )


def _analytics_duration_seconds(
    db: Session,
    *,
    started_at: SQLColumnExpression[datetime | None],
    ended_at: SQLColumnExpression[datetime | None],
) -> ColumnElement[float | None]:
    """Return a portable SQL expression for a non-negative duration."""

    if db.bind is not None and db.bind.dialect.name == "sqlite":
        elapsed = (func.julianday(ended_at) - func.julianday(started_at)) * 86400.0
    else:
        elapsed = func.extract("epoch", ended_at - started_at)
    return case(
        (and_(started_at.is_not(None), ended_at >= started_at), elapsed),
        else_=None,
    )


def _team_sla_expression(
    team_column: SQLColumnExpression[UUID],
    thresholds: dict[UUID, int],
) -> ColumnElement[int | None]:
    """Build a portable SQL expression for per-team SLA thresholds."""

    nullable_integer = type_cast(TypeEngine[int | None], Integer())
    if not thresholds:
        return cast(literal(None), nullable_integer)
    return case(
        *(
            (team_column == team_id, literal(seconds, type_=Integer()))
            for team_id, seconds in thresholds.items()
        ),
        else_=cast(literal(None), nullable_integer),
    )


def agent_performance_analytics(
    db: Session,
    *,
    query: InboxAgentPerformanceQuery,
) -> InboxAgentPerformanceAnalyticsPage:
    """Aggregate bounded agent metrics in SQL and return one paged projection.

    Assignment totals use assignment effective time. Resolution totals and
    durations use append-only status-transition evidence, credited only to the
    resolving agent's matching assignment at that instant. First-response time
    uses the first recorded human outbound after the first inbound message for
    conversations first received inside the requested interval.
    """

    start_at = _as_utc(query.start_at)
    end_at = _as_utc(query.end_at)
    if start_at is None or end_at is None:
        raise InboxAgentPerformanceQueryError("Date bounds must be timezone-aware.")

    team_statement = select(ServiceTeam).where(ServiceTeam.is_active.is_(True))
    if query.service_team_id is not None:
        team_statement = team_statement.where(ServiceTeam.id == query.service_team_id)
    active_teams = tuple(db.scalars(team_statement).all())
    response_slas = {
        team.id: sla
        for team in active_teams
        if (sla := response_sla_seconds_for_team(team)) is not None
    }
    resolution_slas = {
        team.id: sla
        for team in active_teams
        if (sla := resolution_sla_seconds_for_team(team)) is not None
    }

    display_name = func.coalesce(
        func.nullif(func.trim(SystemUser.display_name), ""),
        func.nullif(func.trim(SystemUser.first_name + " " + SystemUser.last_name), ""),
        SystemUser.email,
    )
    member_statement = (
        select(
            ServiceTeamMember.team_id.label("service_team_id"),
            ServiceTeam.name.label("service_team_name"),
            SystemUser.id.label("person_id"),
            display_name.label("agent_name"),
        )
        .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
        .join(SystemUser, SystemUser.person_party_id == ServiceTeamMember.person_id)
        .where(
            ServiceTeam.is_active.is_(True),
            ServiceTeamMember.is_active.is_(True),
            SystemUser.is_active.is_(True),
        )
    )
    if query.person_id is not None:
        member_statement = member_statement.where(SystemUser.id == query.person_id)
    if query.service_team_id is not None:
        member_statement = member_statement.where(
            ServiceTeamMember.team_id == query.service_team_id
        )
    normalized_search = (query.search or "").strip()
    if normalized_search:
        search_term = f"%{normalized_search}%"
        member_statement = member_statement.where(
            or_(
                SystemUser.display_name.ilike(search_term),
                SystemUser.first_name.ilike(search_term),
                SystemUser.last_name.ilike(search_term),
                SystemUser.email.ilike(search_term),
            )
        )
    members = member_statement.cte("agent_performance_members")

    assigned = (
        select(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
            func.count(
                func.distinct(InboxConversationAssignment.conversation_id)
            ).label("assigned_count"),
        )
        .where(
            InboxConversationAssignment.assigned_at >= start_at,
            InboxConversationAssignment.assigned_at < end_at,
        )
        .group_by(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
        )
        .cte("agent_period_assignments")
    )
    if query.service_team_id is not None:
        assigned = (
            select(
                InboxConversationAssignment.service_team_id,
                InboxConversationAssignment.person_id,
                func.count(
                    func.distinct(InboxConversationAssignment.conversation_id)
                ).label("assigned_count"),
            )
            .where(
                InboxConversationAssignment.service_team_id == query.service_team_id,
                InboxConversationAssignment.assigned_at >= start_at,
                InboxConversationAssignment.assigned_at < end_at,
            )
            .group_by(
                InboxConversationAssignment.service_team_id,
                InboxConversationAssignment.person_id,
            )
            .cte("agent_period_assignments")
        )

    active_resolution_sla = _team_sla_expression(
        InboxConversationAssignment.service_team_id,
        resolution_slas,
    )
    active_age = _analytics_duration_seconds(
        db,
        started_at=InboxConversation.first_message_at,
        ended_at=func.now(),
    )
    active = (
        select(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
            func.count(InboxConversationAssignment.id).label("active_count"),
            func.sum(
                case(
                    (
                        and_(
                            active_resolution_sla.is_not(None),
                            active_age > active_resolution_sla,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("currently_overdue_count"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .where(InboxConversationAssignment.is_active.is_(True))
        .group_by(
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
        )
        .cte("agent_active_assignments")
    )
    if query.service_team_id is not None:
        active = (
            select(
                InboxConversationAssignment.service_team_id,
                InboxConversationAssignment.person_id,
                func.count(InboxConversationAssignment.id).label("active_count"),
                func.sum(
                    case(
                        (
                            and_(
                                active_resolution_sla.is_not(None),
                                active_age > active_resolution_sla,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("currently_overdue_count"),
            )
            .join(
                InboxConversation,
                InboxConversation.id == InboxConversationAssignment.conversation_id,
            )
            .where(
                InboxConversationAssignment.is_active.is_(True),
                InboxConversationAssignment.service_team_id == query.service_team_id,
            )
            .group_by(
                InboxConversationAssignment.service_team_id,
                InboxConversationAssignment.person_id,
            )
            .cte("agent_active_assignments")
        )

    resolution_duration = _analytics_duration_seconds(
        db,
        started_at=InboxConversation.first_message_at,
        ended_at=InboxStatusTransitionEvent.occurred_at,
    )
    resolution_facts = (
        select(
            InboxStatusTransitionEvent.id.label("event_id"),
            InboxConversationAssignment.service_team_id,
            InboxStatusTransitionEvent.actor_person_id.label("person_id"),
            resolution_duration.label("duration_seconds"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxStatusTransitionEvent.conversation_id,
        )
        .join(
            InboxConversationAssignment,
            and_(
                InboxConversationAssignment.conversation_id
                == InboxStatusTransitionEvent.conversation_id,
                InboxConversationAssignment.person_id
                == InboxStatusTransitionEvent.actor_person_id,
                InboxConversationAssignment.assigned_at
                <= InboxStatusTransitionEvent.occurred_at,
                or_(
                    InboxConversationAssignment.ended_at.is_(None),
                    InboxConversationAssignment.ended_at
                    >= InboxStatusTransitionEvent.occurred_at,
                ),
            ),
        )
        .where(
            InboxStatusTransitionEvent.status == InboxConversationStatus.resolved.value,
            InboxStatusTransitionEvent.actor_person_id.is_not(None),
            InboxStatusTransitionEvent.occurred_at >= start_at,
            InboxStatusTransitionEvent.occurred_at < end_at,
        )
        .distinct()
        .cte("agent_resolution_facts")
    )
    resolution_sla = _team_sla_expression(
        resolution_facts.c.service_team_id,
        resolution_slas,
    )
    resolution = (
        select(
            resolution_facts.c.service_team_id,
            resolution_facts.c.person_id,
            func.count(func.distinct(resolution_facts.c.event_id)).label(
                "resolved_count"
            ),
            func.count(resolution_facts.c.duration_seconds).label(
                "resolution_timed_count"
            ),
            func.coalesce(func.sum(resolution_facts.c.duration_seconds), 0.0).label(
                "resolution_seconds_sum"
            ),
            func.sum(
                case(
                    (
                        and_(
                            resolution_sla.is_not(None),
                            resolution_facts.c.duration_seconds <= resolution_sla,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("resolution_sla_met_count"),
            func.sum(
                case(
                    (
                        and_(
                            resolution_sla.is_not(None),
                            resolution_facts.c.duration_seconds > resolution_sla,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("resolution_sla_breached_count"),
        )
        .group_by(
            resolution_facts.c.service_team_id,
            resolution_facts.c.person_id,
        )
        .cte("agent_resolutions")
    )

    message_time = func.coalesce(
        InboxMessage.received_at,
        InboxMessage.sent_at,
        InboxMessage.created_at,
    )
    first_inbound_time = func.min(message_time)
    first_inbound = (
        select(
            InboxMessage.conversation_id,
            first_inbound_time.label("first_inbound_at"),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxMessage.conversation_id,
        )
        .where(
            InboxMessage.direction == InboxMessageDirection.inbound.value,
            InboxConversation.first_message_at >= start_at,
            InboxConversation.first_message_at < end_at,
        )
        .group_by(InboxMessage.conversation_id)
        .cte("agent_first_inbound")
    )
    sender_value = InboxMessage.metadata_["sent_by_person_id"].as_string()
    outbound_ranked = (
        select(
            InboxMessage.conversation_id,
            sender_value.label("sender_person_id"),
            message_time.label("response_at"),
            first_inbound.c.first_inbound_at,
            func.row_number()
            .over(
                partition_by=InboxMessage.conversation_id,
                order_by=(message_time.asc(), InboxMessage.id.asc()),
            )
            .label("response_rank"),
        )
        .join(
            first_inbound,
            first_inbound.c.conversation_id == InboxMessage.conversation_id,
        )
        .where(
            InboxMessage.direction == InboxMessageDirection.outbound.value,
            sender_value.is_not(None),
            message_time >= first_inbound.c.first_inbound_at,
            message_time < end_at,
        )
        .cte("agent_human_outbounds")
    )
    first_response = (
        select(
            outbound_ranked.c.conversation_id,
            outbound_ranked.c.sender_person_id,
            outbound_ranked.c.response_at,
            outbound_ranked.c.first_inbound_at,
        )
        .where(outbound_ranked.c.response_rank == 1)
        .cte("agent_first_response")
    )
    response_duration = _analytics_duration_seconds(
        db,
        started_at=first_response.c.first_inbound_at,
        ended_at=first_response.c.response_at,
    )
    assignment_person_text = func.replace(
        cast(InboxConversationAssignment.person_id, String), "-", ""
    )
    response_person_text = func.replace(first_response.c.sender_person_id, "-", "")
    response_facts = (
        select(
            first_response.c.conversation_id,
            InboxConversationAssignment.service_team_id,
            InboxConversationAssignment.person_id,
            response_duration.label("duration_seconds"),
        )
        .join(
            InboxConversationAssignment,
            and_(
                InboxConversationAssignment.conversation_id
                == first_response.c.conversation_id,
                assignment_person_text == response_person_text,
                InboxConversationAssignment.assigned_at <= first_response.c.response_at,
                or_(
                    InboxConversationAssignment.ended_at.is_(None),
                    InboxConversationAssignment.ended_at
                    >= first_response.c.response_at,
                ),
            ),
        )
        .distinct()
        .cte("agent_first_response_facts")
    )
    response_sla = _team_sla_expression(
        response_facts.c.service_team_id,
        response_slas,
    )
    responses = (
        select(
            response_facts.c.service_team_id,
            response_facts.c.person_id,
            func.count(response_facts.c.duration_seconds).label("response_count"),
            func.coalesce(func.sum(response_facts.c.duration_seconds), 0.0).label(
                "response_seconds_sum"
            ),
            func.sum(
                case(
                    (
                        and_(
                            response_sla.is_not(None),
                            response_facts.c.duration_seconds <= response_sla,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("response_sla_met_count"),
            func.sum(
                case(
                    (
                        and_(
                            response_sla.is_not(None),
                            response_facts.c.duration_seconds > response_sla,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("response_sla_breached_count"),
        )
        .group_by(
            response_facts.c.service_team_id,
            response_facts.c.person_id,
        )
        .cte("agent_first_responses")
    )

    assigned_count = func.coalesce(assigned.c.assigned_count, 0)
    resolved_count = func.coalesce(resolution.c.resolved_count, 0)
    active_count = func.coalesce(active.c.active_count, 0)
    currently_overdue_count = func.coalesce(active.c.currently_overdue_count, 0)
    resolution_timed_count = func.coalesce(resolution.c.resolution_timed_count, 0)
    resolution_seconds_sum = func.coalesce(resolution.c.resolution_seconds_sum, 0.0)
    resolution_sla_met_count = func.coalesce(resolution.c.resolution_sla_met_count, 0)
    resolution_sla_breached_count = func.coalesce(
        resolution.c.resolution_sla_breached_count, 0
    )
    response_count = func.coalesce(responses.c.response_count, 0)
    response_seconds_sum = func.coalesce(responses.c.response_seconds_sum, 0.0)
    response_sla_met_count = func.coalesce(responses.c.response_sla_met_count, 0)
    response_sla_breached_count = func.coalesce(
        responses.c.response_sla_breached_count, 0
    )
    performance = (
        select(
            members.c.person_id,
            members.c.agent_name,
            members.c.service_team_id,
            members.c.service_team_name,
            assigned_count.label("assigned_count"),
            resolved_count.label("resolved_count"),
            active_count.label("active_count"),
            currently_overdue_count.label("currently_overdue_count"),
            resolution_timed_count.label("resolution_timed_count"),
            resolution_seconds_sum.label("resolution_seconds_sum"),
            resolution_sla_met_count.label("resolution_sla_met_count"),
            resolution_sla_breached_count.label("resolution_sla_breached_count"),
            response_count.label("response_count"),
            response_seconds_sum.label("response_seconds_sum"),
            response_sla_met_count.label("response_sla_met_count"),
            response_sla_breached_count.label("response_sla_breached_count"),
            case(
                (
                    resolution_timed_count > 0,
                    resolution_seconds_sum / resolution_timed_count,
                ),
                else_=None,
            ).label("average_resolution_seconds"),
            case(
                (
                    response_count > 0,
                    response_seconds_sum / response_count,
                ),
                else_=None,
            ).label("average_first_response_seconds"),
        )
        .outerjoin(
            assigned,
            and_(
                assigned.c.service_team_id == members.c.service_team_id,
                assigned.c.person_id == members.c.person_id,
            ),
        )
        .outerjoin(
            active,
            and_(
                active.c.service_team_id == members.c.service_team_id,
                active.c.person_id == members.c.person_id,
            ),
        )
        .outerjoin(
            resolution,
            and_(
                resolution.c.service_team_id == members.c.service_team_id,
                resolution.c.person_id == members.c.person_id,
            ),
        )
        .outerjoin(
            responses,
            and_(
                responses.c.service_team_id == members.c.service_team_id,
                responses.c.person_id == members.c.person_id,
            ),
        )
        .cte("agent_performance")
    )

    member_team_rows = tuple(db.scalars(select(members.c.service_team_id)).all())
    member_team_ids = set(member_team_rows)
    total = len(member_team_rows)
    if total == 0:
        return InboxAgentPerformanceAnalyticsPage(
            rows=(),
            summary=InboxAgentPerformanceSummary(
                agent_count=0,
                activity_agent_count=0,
                assigned_conversation_count=0,
                resolved_conversation_count=0,
                active_assignment_count=0,
                average_resolution_seconds=None,
                average_first_response_seconds=None,
                first_response_observation_count=0,
                resolution_observation_count=0,
                first_response_sla_met_count=0,
                first_response_sla_breached_count=0,
                first_response_sla_rate=None,
                resolution_sla_met_count=0,
                resolution_sla_breached_count=0,
                resolution_sla_rate=None,
                currently_overdue_count=0,
                first_response_sla_configured=any(
                    team_id in response_slas for team_id in member_team_ids
                ),
                resolution_sla_configured=any(
                    team_id in resolution_slas for team_id in member_team_ids
                ),
            ),
            total=0,
            page=1,
            per_page=query.per_page or 1,
            generated_at=datetime.now(UTC),
        )

    summary_values = (
        select(
            func.count(func.distinct(performance.c.person_id)).label(
                "summary_agent_count"
            ),
            func.count(
                func.distinct(
                    case(
                        (
                            or_(
                                performance.c.assigned_count > 0,
                                performance.c.resolved_count > 0,
                                performance.c.response_count > 0,
                            ),
                            performance.c.person_id,
                        ),
                        else_=None,
                    )
                )
            ).label("summary_activity_agent_count"),
            func.coalesce(func.sum(performance.c.assigned_count), 0).label(
                "summary_assigned_count"
            ),
            func.coalesce(func.sum(performance.c.resolved_count), 0).label(
                "summary_resolved_count"
            ),
            func.coalesce(func.sum(performance.c.active_count), 0).label(
                "summary_active_count"
            ),
            func.coalesce(func.sum(performance.c.resolution_timed_count), 0).label(
                "summary_resolution_timed_count"
            ),
            func.coalesce(func.sum(performance.c.resolution_seconds_sum), 0.0).label(
                "summary_resolution_seconds_sum"
            ),
            func.coalesce(func.sum(performance.c.response_count), 0).label(
                "summary_response_count"
            ),
            func.coalesce(func.sum(performance.c.response_seconds_sum), 0.0).label(
                "summary_response_seconds_sum"
            ),
            func.coalesce(func.sum(performance.c.response_sla_met_count), 0).label(
                "summary_response_sla_met_count"
            ),
            func.coalesce(func.sum(performance.c.response_sla_breached_count), 0).label(
                "summary_response_sla_breached_count"
            ),
            func.coalesce(func.sum(performance.c.resolution_sla_met_count), 0).label(
                "summary_resolution_sla_met_count"
            ),
            func.coalesce(
                func.sum(performance.c.resolution_sla_breached_count), 0
            ).label("summary_resolution_sla_breached_count"),
            func.coalesce(func.sum(performance.c.currently_overdue_count), 0).label(
                "summary_currently_overdue_count"
            ),
        )
        .select_from(performance)
        .cte("agent_performance_summary")
    )
    row_statement = (
        select(performance, summary_values)
        .select_from(performance.join(summary_values, true()))
        .order_by(
            performance.c.agent_name.asc(),
            performance.c.service_team_name.asc(),
            performance.c.person_id.asc(),
        )
    )
    effective_page = query.page
    if query.per_page is not None:
        last_page = max((total + query.per_page - 1) // query.per_page, 1)
        effective_page = min(query.page, last_page)
        row_statement = row_statement.offset(
            (effective_page - 1) * query.per_page
        ).limit(query.per_page)
    raw_rows = db.execute(row_statement).mappings().all()
    summary_row = raw_rows[0]

    resolution_timed_total = int(summary_row["summary_resolution_timed_count"] or 0)
    response_total = int(summary_row["summary_response_count"] or 0)
    response_sla_met_total = int(summary_row["summary_response_sla_met_count"] or 0)
    response_sla_breached_total = int(
        summary_row["summary_response_sla_breached_count"] or 0
    )
    response_sla_scored_total = response_sla_met_total + response_sla_breached_total
    resolution_sla_met_total = int(summary_row["summary_resolution_sla_met_count"] or 0)
    resolution_sla_breached_total = int(
        summary_row["summary_resolution_sla_breached_count"] or 0
    )
    resolution_sla_scored_total = (
        resolution_sla_met_total + resolution_sla_breached_total
    )
    summary = InboxAgentPerformanceSummary(
        agent_count=int(summary_row["summary_agent_count"] or 0),
        activity_agent_count=int(summary_row["summary_activity_agent_count"] or 0),
        assigned_conversation_count=int(summary_row["summary_assigned_count"] or 0),
        resolved_conversation_count=int(summary_row["summary_resolved_count"] or 0),
        active_assignment_count=int(summary_row["summary_active_count"] or 0),
        average_resolution_seconds=(
            round(
                float(summary_row["summary_resolution_seconds_sum"])
                / resolution_timed_total,
                3,
            )
            if resolution_timed_total
            else None
        ),
        average_first_response_seconds=(
            round(
                float(summary_row["summary_response_seconds_sum"]) / response_total,
                3,
            )
            if response_total
            else None
        ),
        first_response_observation_count=response_total,
        resolution_observation_count=resolution_timed_total,
        first_response_sla_met_count=response_sla_met_total,
        first_response_sla_breached_count=response_sla_breached_total,
        first_response_sla_rate=(
            response_sla_met_total / response_sla_scored_total
            if response_sla_scored_total
            else None
        ),
        resolution_sla_met_count=resolution_sla_met_total,
        resolution_sla_breached_count=resolution_sla_breached_total,
        resolution_sla_rate=(
            resolution_sla_met_total / resolution_sla_scored_total
            if resolution_sla_scored_total
            else None
        ),
        currently_overdue_count=int(
            summary_row["summary_currently_overdue_count"] or 0
        ),
        first_response_sla_configured=any(
            team_id in response_slas for team_id in member_team_ids
        ),
        resolution_sla_configured=any(
            team_id in resolution_slas for team_id in member_team_ids
        ),
    )
    rows = tuple(
        InboxAgentPerformanceAnalyticsRow(
            person_id=row["person_id"],
            agent_name=str(row["agent_name"]),
            service_team_id=row["service_team_id"],
            service_team_name=str(row["service_team_name"]),
            assigned_conversation_count=int(row["assigned_count"] or 0),
            resolved_conversation_count=int(row["resolved_count"] or 0),
            active_assignment_count=int(row["active_count"] or 0),
            average_resolution_seconds=(
                round(float(row["average_resolution_seconds"]), 3)
                if row["average_resolution_seconds"] is not None
                else None
            ),
            average_first_response_seconds=(
                round(float(row["average_first_response_seconds"]), 3)
                if row["average_first_response_seconds"] is not None
                else None
            ),
            resolution_observation_count=int(row["resolution_timed_count"] or 0),
            first_response_observation_count=int(row["response_count"] or 0),
            first_response_sla_seconds=response_slas.get(row["service_team_id"]),
            resolution_sla_seconds=resolution_slas.get(row["service_team_id"]),
            first_response_sla_met_count=int(row["response_sla_met_count"] or 0),
            first_response_sla_breached_count=int(
                row["response_sla_breached_count"] or 0
            ),
            first_response_sla_rate=(
                int(row["response_sla_met_count"] or 0)
                / (
                    int(row["response_sla_met_count"] or 0)
                    + int(row["response_sla_breached_count"] or 0)
                )
                if (
                    int(row["response_sla_met_count"] or 0)
                    + int(row["response_sla_breached_count"] or 0)
                )
                else None
            ),
            resolution_sla_met_count=int(row["resolution_sla_met_count"] or 0),
            resolution_sla_breached_count=int(
                row["resolution_sla_breached_count"] or 0
            ),
            resolution_sla_rate=(
                int(row["resolution_sla_met_count"] or 0)
                / (
                    int(row["resolution_sla_met_count"] or 0)
                    + int(row["resolution_sla_breached_count"] or 0)
                )
                if (
                    int(row["resolution_sla_met_count"] or 0)
                    + int(row["resolution_sla_breached_count"] or 0)
                )
                else None
            ),
            currently_overdue_count=int(row["currently_overdue_count"] or 0),
        )
        for row in raw_rows
    )
    per_page = query.per_page or max(total, 1)
    return InboxAgentPerformanceAnalyticsPage(
        rows=rows,
        summary=summary,
        total=total,
        page=effective_page,
        per_page=per_page,
        generated_at=datetime.now(UTC),
    )


def active_service_team_options(db: Session) -> list[ServiceTeam]:
    return _load_teams(db, include_inactive=False)


def agent_performance_evidence(
    db: Session,
    *,
    query: InboxAgentPerformanceQuery,
    limit: int = 50,
) -> tuple[InboxAgentConversationEvidence, ...]:
    """Return bounded conversation evidence for one agent detail report."""

    def message_time(message: InboxMessage) -> datetime:
        return message.received_at or message.sent_at or message.created_at

    def sender_person_id(message: InboxMessage) -> UUID | None:
        raw_person_id = (message.metadata_ or {}).get("sent_by_person_id")
        if not raw_person_id:
            return None
        try:
            return UUID(str(raw_person_id))
        except (TypeError, ValueError):
            return None

    def seconds_between(
        started_at: datetime | None, ended_at: datetime | None
    ) -> float | None:
        start_utc = _as_utc(started_at)
        end_utc = _as_utc(ended_at)
        if start_utc is None or end_utc is None:
            return None
        return max((end_utc - start_utc).total_seconds(), 0.0)

    def first_human_response(
        messages: list[InboxMessage],
    ) -> tuple[InboxMessage, InboxMessage] | None:
        first_inbound = next(
            (
                message
                for message in messages
                if message.direction == InboxMessageDirection.inbound.value
            ),
            None,
        )
        if first_inbound is None:
            return None
        inbound_at = message_time(first_inbound)
        first_outbound = next(
            (
                message
                for message in messages
                if message.direction == InboxMessageDirection.outbound.value
                and message_time(message) >= inbound_at
                and sender_person_id(message) is not None
            ),
            None,
        )
        return (first_inbound, first_outbound) if first_outbound else None

    if query.person_id is None:
        raise InboxAgentPerformanceQueryError(
            "Conversation evidence requires one agent identity."
        )
    assignment_statement = (
        select(InboxConversationAssignment, InboxConversation, ServiceTeam)
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .join(
            ServiceTeam, ServiceTeam.id == InboxConversationAssignment.service_team_id
        )
        .where(
            InboxConversationAssignment.person_id == query.person_id,
            ServiceTeam.is_active.is_(True),
            or_(
                and_(
                    InboxConversationAssignment.assigned_at >= query.start_at,
                    InboxConversationAssignment.assigned_at < query.end_at,
                ),
                InboxConversationAssignment.is_active.is_(True),
            ),
        )
        .order_by(InboxConversationAssignment.assigned_at.desc())
        .limit(min(max(limit, 1), 100))
    )
    if query.service_team_id is not None:
        assignment_statement = assignment_statement.where(
            InboxConversationAssignment.service_team_id == query.service_team_id
        )
    records = db.execute(assignment_statement).all()
    if not records:
        return ()

    conversation_ids = [assignment.conversation_id for assignment, _, _ in records]
    bounded_messages = db.scalars(
        select(InboxMessage)
        .where(InboxMessage.conversation_id.in_(conversation_ids))
        .order_by(InboxMessage.created_at.asc())
        .limit(1000)
    )
    messages: dict[UUID, list[InboxMessage]] = {}
    for message in bounded_messages:
        messages.setdefault(message.conversation_id, []).append(message)
    resolution_rows = db.execute(
        select(InboxStatusTransitionEvent)
        .where(
            InboxStatusTransitionEvent.conversation_id.in_(conversation_ids),
            InboxStatusTransitionEvent.actor_person_id == query.person_id,
            InboxStatusTransitionEvent.status == InboxConversationStatus.resolved.value,
            InboxStatusTransitionEvent.occurred_at >= query.start_at,
            InboxStatusTransitionEvent.occurred_at < query.end_at,
        )
        .order_by(InboxStatusTransitionEvent.occurred_at.desc())
    ).scalars()
    resolved_at_by_conversation: dict[UUID, datetime] = {}
    for event in resolution_rows:
        resolved_at_by_conversation.setdefault(event.conversation_id, event.occurred_at)

    evidence: list[InboxAgentConversationEvidence] = []
    for assignment, conversation, team in records:
        response = first_human_response(messages.get(conversation.id, []))
        response_seconds = None
        if response is not None and sender_person_id(response[1]) == query.person_id:
            response_seconds = seconds_between(
                message_time(response[0]), message_time(response[1])
            )
        resolved_at = resolved_at_by_conversation.get(conversation.id)
        evidence.append(
            InboxAgentConversationEvidence(
                conversation_id=conversation.id,
                subject=(conversation.subject or "(No subject)"),
                service_team_id=team.id,
                service_team_name=team.name,
                assigned_at=assignment.assigned_at,
                resolved_at=resolved_at,
                is_active_assignment=assignment.is_active,
                first_response_seconds=response_seconds,
                resolution_seconds=seconds_between(
                    conversation.first_message_at, resolved_at
                ),
            )
        )
    return tuple(evidence)


def escalation_candidates(
    db: Session,
    *,
    response_sla_seconds: int | None = None,
    queue_sla_seconds: int | None = None,
    include_inactive: bool = False,
    now: datetime | None = None,
) -> list[InboxEscalationCandidate]:
    return list(
        escalation_page(
            db,
            query=InboxEscalationQuery(
                response_sla_seconds=response_sla_seconds,
                queue_sla_seconds=queue_sla_seconds,
                include_inactive_teams=include_inactive,
                limit=None,
                observed_at=now,
            ),
        ).rows
    )
