from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxMessage,
    InboxMessageDirection,
)
from app.services import service_team_composition, team_inbox_assignment


@dataclass(frozen=True)
class InboxTeamPerformanceMetrics:
    service_team_id: str
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


@dataclass(frozen=True)
class InboxAgentPerformanceMetrics:
    person_id: str
    service_team_id: str
    active_assignment_count: int
    handled_conversation_count: int
    average_first_response_seconds: float | None
    average_queue_wait_seconds: float | None


@dataclass(frozen=True)
class InboxTeamPerformanceReportRow:
    service_team_id: str
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    response_sla_seconds: int | None
    metrics: InboxTeamPerformanceMetrics


@dataclass(frozen=True)
class InboxAgentPerformanceReportRow:
    person_id: str
    service_team_id: str
    service_team_name: str
    service_team_capabilities: tuple[str, ...]
    metrics: InboxAgentPerformanceMetrics


@dataclass(frozen=True)
class InboxEscalationCandidate:
    conversation_id: str
    service_team_id: str
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
    assigned_person_id: str | None
    available_agent_count: int


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    if start_utc is None or end_utc is None:
        return None
    return max((end_utc - start_utc).total_seconds(), 0.0)


def _avg(values: list[float]) -> float | None:
    return mean(values) if values else None


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
            [
                nested.get("response_sla_seconds"),
                nested.get("first_response_seconds"),
            ]
        )
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


def _conversation_ids_for_team(db: Session, service_team_id: str | UUID) -> list[UUID]:
    team_uuid = UUID(str(service_team_id))
    rows = (
        db.query(InboxConversationTeam.conversation_id)
        .filter(InboxConversationTeam.service_team_id == team_uuid)
        .filter(InboxConversationTeam.is_active.is_(True))
        .all()
    )
    return [row[0] for row in rows]


def _messages_by_conversation(
    db: Session,
    conversation_ids: list[UUID],
) -> dict[UUID, list[InboxMessage]]:
    if not conversation_ids:
        return {}
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.conversation_id.in_(conversation_ids))
        .order_by(InboxMessage.created_at.asc())
        .all()
    )
    grouped: dict[UUID, list[InboxMessage]] = {}
    for message in messages:
        grouped.setdefault(message.conversation_id, []).append(message)
    return grouped


def _message_time(message: InboxMessage) -> datetime:
    return message.received_at or message.sent_at or message.created_at


def _first_response_seconds(messages: list[InboxMessage]) -> float | None:
    response = _first_response(messages)
    if response is None:
        return None
    first_inbound, first_outbound = response
    return _seconds_between(_message_time(first_inbound), _message_time(first_outbound))


def _first_response(
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
    inbound_time = _message_time(first_inbound)
    first_outbound = next(
        (
            message
            for message in messages
            if message.direction == InboxMessageDirection.outbound.value
            and _message_time(message) >= inbound_time
        ),
        None,
    )
    if first_outbound is None:
        return None
    return first_inbound, first_outbound


def _sent_by_person_id(message: InboxMessage) -> UUID | None:
    """Return the recorded human sender for an Inbox outbound message."""

    metadata = message.metadata_ or {}
    raw_person_id = metadata.get("sent_by_person_id")
    if not raw_person_id:
        return None
    try:
        return UUID(str(raw_person_id))
    except (TypeError, ValueError):
        return None


def _first_human_response(
    messages: list[InboxMessage],
) -> tuple[InboxMessage, InboxMessage] | None:
    """Return the first agent-authored response to the first inbound message."""

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
    inbound_time = _message_time(first_inbound)
    first_outbound = next(
        (
            message
            for message in messages
            if message.direction == InboxMessageDirection.outbound.value
            and _message_time(message) >= inbound_time
            and _sent_by_person_id(message) is not None
        ),
        None,
    )
    if first_outbound is None:
        return None
    return first_inbound, first_outbound


def _first_inbound_message(messages: list[InboxMessage]) -> InboxMessage | None:
    return next(
        (
            message
            for message in messages
            if message.direction == InboxMessageDirection.inbound.value
        ),
        None,
    )


def _has_outbound_after(messages: list[InboxMessage], at: datetime) -> bool:
    return any(
        message.direction == InboxMessageDirection.outbound.value
        and _message_time(message) >= at
        for message in messages
    )


def team_performance_metrics(
    db: Session,
    service_team_id: str | UUID,
    *,
    response_sla_seconds: int | None = None,
    now: datetime | None = None,
) -> InboxTeamPerformanceMetrics:
    team_uuid = UUID(str(service_team_id))
    now_utc = _as_utc(now) or datetime.now(UTC)
    conversation_ids = _conversation_ids_for_team(db, team_uuid)
    conversations = (
        db.query(InboxConversation)
        .filter(InboxConversation.id.in_(conversation_ids))
        .all()
        if conversation_ids
        else []
    )
    messages_by_conversation = _messages_by_conversation(db, conversation_ids)
    active_assignments = (
        {
            row.conversation_id: row
            for row in db.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id.in_(conversation_ids))
            .filter(InboxConversationAssignment.is_active.is_(True))
            .all()
        }
        if conversation_ids
        else {}
    )

    inbound_count = 0
    outbound_count = 0
    response_values: list[float] = []
    queue_wait_values: list[float] = []
    response_breaches = 0

    for conversation in conversations:
        messages = messages_by_conversation.get(conversation.id, [])
        inbound_count += sum(
            1
            for message in messages
            if message.direction == InboxMessageDirection.inbound.value
        )
        outbound_count += sum(
            1
            for message in messages
            if message.direction == InboxMessageDirection.outbound.value
        )
        response_seconds = _first_response_seconds(messages)
        if response_seconds is not None:
            response_values.append(response_seconds)
            if (
                response_sla_seconds is not None
                and response_seconds > response_sla_seconds
            ):
                response_breaches += 1
        elif response_sla_seconds is not None:
            first_inbound = next(
                (
                    message
                    for message in messages
                    if message.direction == InboxMessageDirection.inbound.value
                ),
                None,
            )
            pending_seconds = (
                _seconds_between(_message_time(first_inbound), now_utc)
                if first_inbound is not None
                else None
            )
            if pending_seconds is not None and pending_seconds > response_sla_seconds:
                response_breaches += 1

        assignment = active_assignments.get(conversation.id)
        if assignment is not None:
            queue_wait = _seconds_between(
                conversation.first_message_at, assignment.assigned_at
            )
            if queue_wait is not None:
                queue_wait_values.append(queue_wait)

    open_conversations = [
        conversation
        for conversation in conversations
        if conversation.status != InboxConversationStatus.resolved.value
    ]
    assigned_open_count = sum(
        1
        for conversation in open_conversations
        if conversation.id in active_assignments
    )
    return InboxTeamPerformanceMetrics(
        service_team_id=str(team_uuid),
        conversation_count=len(conversations),
        open_count=len(open_conversations),
        unassigned_open_count=len(open_conversations) - assigned_open_count,
        assigned_open_count=assigned_open_count,
        inbound_message_count=inbound_count,
        outbound_message_count=outbound_count,
        responded_count=len(response_values),
        response_sla_breached_count=response_breaches,
        average_first_response_seconds=_avg(response_values),
        average_queue_wait_seconds=_avg(queue_wait_values),
    )


def agent_performance_metrics(
    db: Session,
    *,
    service_team_id: str | UUID,
    person_id: str | UUID,
) -> InboxAgentPerformanceMetrics:
    team_uuid = UUID(str(service_team_id))
    person_uuid = UUID(str(person_id))
    assignments = (
        db.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.service_team_id == team_uuid)
        .filter(InboxConversationAssignment.person_id == person_uuid)
        .all()
    )
    conversation_ids = [assignment.conversation_id for assignment in assignments]
    conversations = (
        {
            conversation.id: conversation
            for conversation in db.query(InboxConversation)
            .filter(InboxConversation.id.in_(conversation_ids))
            .all()
        }
        if conversation_ids
        else {}
    )
    queue_wait_values: list[float] = []
    for assignment in assignments:
        conversation = conversations.get(assignment.conversation_id)
        if conversation is None:
            continue
        queue_wait = _seconds_between(
            conversation.first_message_at, assignment.assigned_at
        )
        if queue_wait is not None:
            queue_wait_values.append(queue_wait)

    first_response_values: list[float] = []
    team_conversation_ids = _conversation_ids_for_team(db, team_uuid)
    for messages in _messages_by_conversation(db, team_conversation_ids).values():
        response = _first_human_response(messages)
        if response is None:
            continue
        first_inbound, first_outbound = response
        if _sent_by_person_id(first_outbound) != person_uuid:
            continue
        response_seconds = _seconds_between(
            _message_time(first_inbound), _message_time(first_outbound)
        )
        if response_seconds is not None:
            first_response_values.append(response_seconds)

    return InboxAgentPerformanceMetrics(
        person_id=str(person_uuid),
        service_team_id=str(team_uuid),
        active_assignment_count=sum(
            1 for assignment in assignments if assignment.is_active
        ),
        handled_conversation_count=len(
            {assignment.conversation_id for assignment in assignments}
        ),
        average_first_response_seconds=_avg(first_response_values),
        average_queue_wait_seconds=_avg(queue_wait_values),
    )


def team_performance_report(
    db: Session,
    *,
    response_sla_seconds: int | None = None,
    include_inactive: bool = False,
    now: datetime | None = None,
) -> list[InboxTeamPerformanceReportRow]:
    query = db.query(ServiceTeam).order_by(ServiceTeam.name.asc())
    if not include_inactive:
        query = query.filter(ServiceTeam.is_active.is_(True))
    teams = query.all()
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        tuple(team.id for team in teams),
    )
    rows: list[InboxTeamPerformanceReportRow] = []
    for team in teams:
        team_sla_seconds = response_sla_seconds_for_team(
            team,
            fallback=response_sla_seconds,
        )
        rows.append(
            InboxTeamPerformanceReportRow(
                service_team_id=str(team.id),
                service_team_name=team.name,
                service_team_capabilities=tuple(
                    capability.value for capability in capabilities_by_team[team.id]
                ),
                response_sla_seconds=team_sla_seconds,
                metrics=team_performance_metrics(
                    db,
                    team.id,
                    response_sla_seconds=team_sla_seconds,
                    now=now,
                ),
            )
        )
    return rows


def agent_performance_report(
    db: Session,
    *,
    service_team_id: str | UUID | None = None,
    include_inactive_members: bool = False,
) -> list[InboxAgentPerformanceReportRow]:
    query = (
        db.query(ServiceTeamMember, ServiceTeam, SystemUser)
        .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
        .join(
            SystemUser,
            SystemUser.person_party_id == ServiceTeamMember.person_id,
        )
        .filter(ServiceTeam.is_active.is_(True))
        .filter(SystemUser.is_active.is_(True))
        .order_by(ServiceTeam.name.asc(), ServiceTeamMember.created_at.asc())
    )
    if service_team_id is not None:
        query = query.filter(ServiceTeam.id == UUID(str(service_team_id)))
    if not include_inactive_members:
        query = query.filter(ServiceTeamMember.is_active.is_(True))

    members = query.all()
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        tuple(team.id for _member, team, _user in members),
    )
    rows: list[InboxAgentPerformanceReportRow] = []
    for member, team, user in members:
        metrics = agent_performance_metrics(
            db,
            service_team_id=team.id,
            person_id=user.id,
        )
        rows.append(
            InboxAgentPerformanceReportRow(
                person_id=str(user.id),
                service_team_id=str(team.id),
                service_team_name=team.name,
                service_team_capabilities=tuple(
                    capability.value for capability in capabilities_by_team[team.id]
                ),
                metrics=metrics,
            )
        )
    return rows


def active_service_team_options(db: Session) -> list[ServiceTeam]:
    return (
        db.query(ServiceTeam)
        .filter(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.name.asc())
        .all()
    )


def escalation_candidates(
    db: Session,
    *,
    response_sla_seconds: int | None = None,
    queue_sla_seconds: int | None = None,
    include_inactive: bool = False,
    now: datetime | None = None,
) -> list[InboxEscalationCandidate]:
    now_utc = _as_utc(now) or datetime.now(UTC)
    team_query = db.query(ServiceTeam).order_by(ServiceTeam.name.asc())
    if not include_inactive:
        team_query = team_query.filter(ServiceTeam.is_active.is_(True))

    teams = team_query.all()
    capabilities_by_team = service_team_composition.capabilities_by_team(
        db,
        tuple(team.id for team in teams),
    )
    candidates: list[InboxEscalationCandidate] = []
    for team in teams:
        team_response_sla = response_sla_seconds_for_team(
            team,
            fallback=response_sla_seconds,
        )
        team_queue_sla = queue_sla_seconds_for_team(
            team,
            fallback=queue_sla_seconds,
        )
        conversation_ids = _conversation_ids_for_team(db, team.id)
        if not conversation_ids:
            continue

        conversations = (
            db.query(InboxConversation)
            .filter(InboxConversation.id.in_(conversation_ids))
            .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
            .filter(InboxConversation.is_active.is_(True))
            .all()
        )
        if not conversations:
            continue

        messages_by_conversation = _messages_by_conversation(db, conversation_ids)
        active_assignments = {
            row.conversation_id: row
            for row in db.query(InboxConversationAssignment)
            .filter(InboxConversationAssignment.conversation_id.in_(conversation_ids))
            .filter(InboxConversationAssignment.is_active.is_(True))
            .all()
        }
        available_agent_count = len(
            team_inbox_assignment.list_available_team_agents(db, team.id)
        )

        for conversation in conversations:
            messages = messages_by_conversation.get(conversation.id, [])
            first_inbound = _first_inbound_message(messages)
            pending_response_seconds = None
            reasons: list[str] = []
            if first_inbound is not None:
                inbound_at = _message_time(first_inbound)
                if not _has_outbound_after(messages, inbound_at):
                    pending_response_seconds = _seconds_between(inbound_at, now_utc)
                    if (
                        team_response_sla is not None
                        and pending_response_seconds is not None
                        and pending_response_seconds > team_response_sla
                    ):
                        reasons.append("response_sla_breached")

            assignment = active_assignments.get(conversation.id)
            queue_wait_seconds = _seconds_between(
                conversation.first_message_at,
                assignment.assigned_at if assignment is not None else now_utc,
            )
            if (
                assignment is None
                and team_queue_sla is not None
                and queue_wait_seconds is not None
                and queue_wait_seconds > team_queue_sla
            ):
                reasons.append("unassigned_queue_breached")
            if assignment is None and available_agent_count == 0:
                reasons.append("no_available_agent")

            if not reasons:
                continue

            candidates.append(
                InboxEscalationCandidate(
                    conversation_id=str(conversation.id),
                    service_team_id=str(team.id),
                    service_team_name=team.name,
                    service_team_capabilities=tuple(
                        capability.value for capability in capabilities_by_team[team.id]
                    ),
                    subject=conversation.subject,
                    contact_address=conversation.contact_address,
                    status=conversation.status,
                    reasons=tuple(reasons),
                    response_sla_seconds=team_response_sla,
                    queue_sla_seconds=team_queue_sla,
                    pending_response_seconds=pending_response_seconds,
                    queue_wait_seconds=queue_wait_seconds,
                    assigned_person_id=(
                        str(assignment.person_id) if assignment is not None else None
                    ),
                    available_agent_count=available_agent_count,
                )
            )

    candidates.sort(
        key=lambda item: (
            "response_sla_breached" not in item.reasons,
            -(item.pending_response_seconds or 0),
            -(item.queue_wait_seconds or 0),
            item.service_team_name,
        )
    )
    return candidates
