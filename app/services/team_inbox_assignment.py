from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import ceil
from typing import TypeVar
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceEvent,
    InboxAgentPresenceStatus,
    InboxAuditEvidenceGrade,
    InboxAuditSource,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationQueueEntry,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxQueueEntryStatus,
    InboxRoutingDecisionMode,
    InboxRoutingEvent,
    InboxRoutingEventType,
    InboxTeamRole,
    InboxTeamRoundRobinCursor,
    InboxTeamSource,
)
from app.services import team_inbox_agent_introduction, team_inbox_queue_notifications
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.settings_spec import resolve_integer

DEFAULT_MAX_CONCURRENT_CONVERSATIONS = 10
COUNTABLE_CAPACITY_STATUSES = frozenset(
    {
        InboxConversationStatus.open.value,
        InboxConversationStatus.pending.value,
        InboxConversationStatus.snoozed.value,
    }
)
VALID_AGENT_PRESENCE_STATUSES = frozenset(
    item.value for item in InboxAgentPresenceStatus
)
T = TypeVar("T")
OWNER = "communications.team_inbox_routing"
_ROUTING_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="routing assignment and escalation transitions",
    name="execute_team_inbox_routing_command",
)


def _commit(db: Session, action: Callable[[], T]) -> T:
    return execute_owner_command(
        db,
        definition=_ROUTING_COMMAND,
        context=CommandContext.system(
            actor="system:team-inbox-routing-adapter",
            scope="team-inbox:routing-command",
            reason="execute Team Inbox routing transition",
        ),
        operation=action,
    )


@dataclass(frozen=True)
class InboxAgentCandidate:
    person_id: str
    active_conversation_count: int
    max_concurrent_conversations: int
    presence_status: str
    presence_observed_at: datetime | None


class InboxPresenceReason(StrEnum):
    manual_change = "manual_change"
    session_timeout = "session_timeout"
    logout = "logout"
    connection_lost = "connection_lost"
    account_deactivated = "account_deactivated"


@dataclass(frozen=True)
class InboxAssignmentResult:
    kind: str
    service_team_id: str | None
    assigned_person_id: str | None = None
    reason: str | None = None
    queue_entry_id: str | None = None


@dataclass(frozen=True)
class InboxQueueSweepCommand:
    context: CommandContext
    limit: int = 200
    now: datetime | None = None


@dataclass(frozen=True)
class InboxQueueSweepResult:
    promoted: int
    cancelled: int
    remaining: int


@dataclass(frozen=True)
class InboxTeamCapacitySnapshot:
    active_assignments: int
    total_capacity: int


def estimate_queue_wait_minutes(
    *,
    queue_position: int,
    active_assignments: int,
    total_capacity: int,
    average_handle_minutes: int = 10,
) -> int | None:
    """Estimate FIFO wait in whole service cycles from a capacity snapshot."""
    if queue_position < 1 or total_capacity < 1 or average_handle_minutes < 1:
        return None
    conversations_ahead_of_capacity = max(
        0, active_assignments + queue_position - total_capacity
    )
    return (
        ceil(conversations_ahead_of_capacity / total_capacity) * average_handle_minutes
    )


def resolve_default_max_concurrent_conversations(db: Session) -> int:
    """Resolve the configurable default agent capacity with a bounded fallback."""

    try:
        value = resolve_integer(
            db,
            SettingDomain.comms,
            "inbox_agent_default_max_concurrent_conversations",
        )
    except Exception:
        return DEFAULT_MAX_CONCURRENT_CONVERSATIONS
    return max(1, min(int(value), 100))


def team_capacity_snapshot(
    db: Session,
    service_team_id: str | UUID,
    *,
    default_max_concurrent: int | None = None,
) -> InboxTeamCapacitySnapshot:
    snapshots = team_capacity_snapshots(
        db,
        (service_team_id,),
        default_max_concurrent=default_max_concurrent,
    )
    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        return InboxTeamCapacitySnapshot(active_assignments=0, total_capacity=0)
    return snapshots.get(
        team_uuid,
        InboxTeamCapacitySnapshot(active_assignments=0, total_capacity=0),
    )


def team_capacity_snapshots(
    db: Session,
    service_team_ids: Sequence[str | UUID],
    *,
    default_max_concurrent: int | None = None,
) -> dict[UUID, InboxTeamCapacitySnapshot]:
    """Load capacity for several teams with one bounded set of queries."""

    if default_max_concurrent is None:
        default_max_concurrent = resolve_default_max_concurrent_conversations(db)
    team_ids = tuple(
        dict.fromkeys(
            team_id
            for value in service_team_ids
            if (team_id := _coerce_uuid(value)) is not None
        )
    )
    if not team_ids:
        return {}
    member_users = (
        db.query(ServiceTeamMember, SystemUser)
        .join(SystemUser, SystemUser.person_party_id == ServiceTeamMember.person_id)
        .filter(ServiceTeamMember.team_id.in_(team_ids))
        .filter(ServiceTeamMember.is_active.is_(True))
        .filter(SystemUser.is_active.is_(True))
        .all()
    )
    person_ids = list(dict.fromkeys(user.id for _member, user in member_users))
    if not person_ids:
        return {
            team_id: InboxTeamCapacitySnapshot(active_assignments=0, total_capacity=0)
            for team_id in team_ids
        }
    presences = {
        row.person_id: row
        for row in db.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id.in_(person_ids))
        .all()
    }
    online_ids = {
        person_id
        for person_id, presence in presences.items()
        if _effective_presence_status(presence) == InboxAgentPresenceStatus.online.value
    }
    online_ids_by_team: dict[UUID, set[UUID]] = {team_id: set() for team_id in team_ids}
    for member, user in member_users:
        if user.id in online_ids:
            online_ids_by_team[member.team_id].add(user.id)
    active_by_person = dict(
        _active_assignment_count_query(db, list(online_ids))
        .with_entities(
            InboxConversationAssignment.person_id,
            func.count(InboxConversationAssignment.id),
        )
        .group_by(InboxConversationAssignment.person_id)
        .all()
        if online_ids
        else ()
    )
    return {
        team_id: InboxTeamCapacitySnapshot(
            active_assignments=sum(
                int(active_by_person.get(person_id, 0))
                for person_id in online_ids_by_team[team_id]
            ),
            total_capacity=sum(
                presences[person_id].max_concurrent_conversations
                or default_max_concurrent
                for person_id in online_ids_by_team[team_id]
            ),
        )
        for team_id in team_ids
    }


def _coerce_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _effective_presence_status(presence: InboxAgentPresence) -> str:
    return (
        presence.manual_override_status
        or presence.status
        or InboxAgentPresenceStatus.offline.value
    )


def _active_assignment_count_query(db: Session, person_ids: list[UUID]):
    return (
        db.query(func.count(InboxConversationAssignment.id))
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .filter(InboxConversationAssignment.is_active.is_(True))
        .filter(InboxConversationAssignment.person_id.in_(person_ids))
        .filter(InboxConversation.status.in_(COUNTABLE_CAPACITY_STATUSES))
        .filter(InboxConversation.is_active.is_(True))
    )


def set_agent_presence(
    db: Session,
    *,
    person_id: str | UUID,
    status: str,
    now: datetime | None = None,
    actor_person_id: str | UUID | None = None,
    reason_code: InboxPresenceReason = InboxPresenceReason.manual_change,
    source_id: str | None = None,
) -> InboxAgentPresence:
    person_uuid = _coerce_uuid(person_id)
    if person_uuid is None:
        raise ValueError("person_id must be a valid UUID")
    clean_status = str(status or "").strip().lower()
    if clean_status not in VALID_AGENT_PRESENCE_STATUSES:
        raise ValueError("Unsupported inbox agent presence status.")

    observed_at = now or datetime.now(UTC)
    presence = (
        db.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == person_uuid)
        .one_or_none()
    )
    if presence is None:
        presence = InboxAgentPresence(person_id=person_uuid)
        db.add(presence)

    previous_effective_status = _effective_presence_status(presence)
    if previous_effective_status == clean_status:
        return presence
    presence.status = clean_status
    presence.manual_override_status = clean_status
    presence.last_seen_at = observed_at
    metadata = dict(presence.metadata_ or {})
    history = metadata.get("manual_status_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "from": previous_effective_status,
            "to": clean_status,
            "at": observed_at.isoformat(),
            "source": "admin_inbox_presence_toggle",
        }
    )
    metadata["manual_status_history"] = history[-50:]
    presence.metadata_ = metadata
    db.add(
        InboxAgentPresenceEvent(
            person_id=person_uuid,
            previous_status=previous_effective_status,
            status=clean_status,
            actor_person_id=_coerce_uuid(actor_person_id),
            reason_code=reason_code.value,
            source=InboxAuditSource.presence_command,
            source_id=source_id or f"presence:{uuid4()}",
            evidence_grade=InboxAuditEvidenceGrade.native,
            occurred_at=observed_at,
        )
    )
    db.flush()
    return presence


def list_available_team_agents(
    db: Session,
    service_team_id: str | UUID,
    *,
    default_max_concurrent: int | None = None,
) -> list[InboxAgentCandidate]:
    if default_max_concurrent is None:
        default_max_concurrent = resolve_default_max_concurrent_conversations(db)
    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        return []

    team = db.get(ServiceTeam, team_uuid)
    if team is None or not team.is_active:
        return []

    member_users = (
        db.query(ServiceTeamMember, SystemUser)
        .join(
            SystemUser,
            SystemUser.person_party_id == ServiceTeamMember.person_id,
        )
        .filter(ServiceTeamMember.team_id == team_uuid)
        .filter(ServiceTeamMember.is_active.is_(True))
        .filter(SystemUser.is_active.is_(True))
        .all()
    )
    if not member_users:
        return []

    person_ids = [user.id for _member, user in member_users]
    presences = {
        row.person_id: row
        for row in db.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id.in_(person_ids))
        .all()
    }
    active_count_rows = (
        db.query(
            InboxConversationAssignment.person_id,
            func.count(InboxConversationAssignment.id),
        )
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .filter(InboxConversationAssignment.is_active.is_(True))
        .filter(InboxConversationAssignment.person_id.in_(person_ids))
        .filter(InboxConversation.status.in_(COUNTABLE_CAPACITY_STATUSES))
        .filter(InboxConversation.is_active.is_(True))
        .group_by(InboxConversationAssignment.person_id)
        .all()
    )
    active_counts = {
        person_id: int(assignment_count)
        for person_id, assignment_count in active_count_rows
    }

    candidates: list[InboxAgentCandidate] = []
    for _member, user in member_users:
        presence = presences.get(user.id)
        if presence is None:
            continue
        if (
            _effective_presence_status(presence)
            != InboxAgentPresenceStatus.online.value
        ):
            continue
        active_count = active_counts.get(user.id, 0)
        max_concurrent = (
            presence.max_concurrent_conversations
            or default_max_concurrent
            or DEFAULT_MAX_CONCURRENT_CONVERSATIONS
        )
        if active_count >= max_concurrent:
            continue
        candidates.append(
            InboxAgentCandidate(
                person_id=str(user.id),
                active_conversation_count=active_count,
                max_concurrent_conversations=max_concurrent,
                presence_status=_effective_presence_status(presence),
                presence_observed_at=presence.last_seen_at,
            )
        )

    candidates.sort(key=lambda item: item.person_id)
    return candidates


def _round_robin_cursor(
    db: Session, service_team_id: UUID
) -> InboxTeamRoundRobinCursor:
    cursor = (
        db.query(InboxTeamRoundRobinCursor)
        .filter(InboxTeamRoundRobinCursor.service_team_id == service_team_id)
        .with_for_update()
        .one_or_none()
    )
    if cursor is None:
        cursor = InboxTeamRoundRobinCursor(service_team_id=service_team_id)
        db.add(cursor)
        db.flush()
    return cursor


def _select_round_robin_candidate(
    db: Session, *, service_team_id: UUID, candidates: list[InboxAgentCandidate]
) -> tuple[InboxAgentCandidate, InboxTeamRoundRobinCursor]:
    cursor = _round_robin_cursor(db, service_team_id)
    if not candidates:
        raise ValueError("candidates are required")
    candidate_ids = [item.person_id for item in candidates]
    start_index = 0
    if cursor.last_assigned_person_id is not None:
        last_id = str(cursor.last_assigned_person_id)
        if last_id in candidate_ids:
            start_index = (candidate_ids.index(last_id) + 1) % len(candidates)
    selected = candidates[start_index]
    cursor.last_assigned_person_id = _coerce_uuid(selected.person_id)
    cursor.rotation_count = int(cursor.rotation_count or 0) + 1
    cursor.metadata_ = {
        **dict(cursor.metadata_ or {}),
        "last_candidate_count": len(candidates),
        "last_candidate_ids": candidate_ids,
        "last_selected_at": datetime.now(UTC).isoformat(),
    }
    db.flush()
    return selected, cursor


def set_conversation_owner_team(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    source: str = InboxTeamSource.escalation.value,
) -> InboxConversation:
    team_uuid = _coerce_uuid(service_team_id)
    if team_uuid is None:
        raise ValueError("service_team_id must be a valid UUID")

    conversation.primary_service_team_id = team_uuid
    links = (
        db.query(InboxConversationTeam)
        .filter(InboxConversationTeam.conversation_id == conversation.id)
        .with_for_update()
        .all()
    )
    for link in links:
        if link.service_team_id == team_uuid:
            link.role = InboxTeamRole.owner.value
            link.source = source
            link.is_active = True
        elif link.role == InboxTeamRole.owner.value:
            link.role = InboxTeamRole.participant.value

    if not any(link.service_team_id == team_uuid for link in links):
        db.add(
            InboxConversationTeam(
                conversation_id=conversation.id,
                service_team_id=team_uuid,
                role=InboxTeamRole.owner.value,
                source=source,
                is_active=True,
            )
        )
    db.flush()
    return conversation


def _record_escalation_metadata(
    conversation: InboxConversation,
    *,
    service_team_id: UUID,
    assigned_person_id: UUID | None,
    assigned_by_person_id: UUID | None,
    reason: str | None,
    kind: str,
    now: datetime,
) -> None:
    metadata = dict(conversation.metadata_ or {})
    metadata["last_inbox_escalation"] = {
        "service_team_id": str(service_team_id),
        "assigned_person_id": str(assigned_person_id) if assigned_person_id else None,
        "assigned_by_person_id": (
            str(assigned_by_person_id) if assigned_by_person_id else None
        ),
        "reason": reason,
        "kind": kind,
        "at": now.isoformat(),
    }
    conversation.metadata_ = metadata


def _active_assignment(
    db: Session,
    conversation: InboxConversation,
) -> InboxConversationAssignment | None:
    return (
        db.query(InboxConversationAssignment)
        .filter(InboxConversationAssignment.conversation_id == conversation.id)
        .filter(InboxConversationAssignment.is_active.is_(True))
        .with_for_update()
        .one_or_none()
    )


def _lock_team(db: Session, team_id: UUID) -> ServiceTeam | None:
    return (
        db.query(ServiceTeam)
        .filter(ServiceTeam.id == team_id)
        .with_for_update()
        .one_or_none()
    )


def _queue_entry(
    db: Session, conversation_id: UUID
) -> InboxConversationQueueEntry | None:
    return (
        db.query(InboxConversationQueueEntry)
        .filter(InboxConversationQueueEntry.conversation_id == conversation_id)
        .with_for_update()
        .one_or_none()
    )


def _settle_queue_entry(
    db: Session,
    *,
    conversation_id: UUID,
    status: InboxQueueEntryStatus,
    now: datetime,
) -> None:
    entry = _queue_entry(db, conversation_id)
    if entry is None or entry.status != InboxQueueEntryStatus.queued.value:
        return
    entry.status = status.value
    entry.settled_at = now
    db.flush()


def _admit_queue_entry(
    db: Session,
    *,
    conversation_id: UUID,
    service_team_id: UUID,
    entered_at: datetime,
) -> InboxConversationQueueEntry:
    entry = _queue_entry(db, conversation_id)
    if (
        entry is not None
        and entry.status == InboxQueueEntryStatus.queued.value
        and entry.service_team_id == service_team_id
    ):
        return entry
    _lock_team(db, service_team_id)
    next_position = (
        int(
            db.query(
                func.coalesce(func.max(InboxConversationQueueEntry.queue_position), 0)
            )
            .filter(InboxConversationQueueEntry.service_team_id == service_team_id)
            .scalar()
            or 0
        )
        + 1
    )
    if entry is None:
        entry = InboxConversationQueueEntry(conversation_id=conversation_id)
        db.add(entry)
    entry.service_team_id = service_team_id
    entry.queue_position = next_position
    entry.status = InboxQueueEntryStatus.queued.value
    entry.entered_at = entered_at
    entry.settled_at = None
    db.flush()
    return entry


def _append_routing_event(
    db: Session,
    *,
    conversation: InboxConversation,
    event_type: InboxRoutingEventType,
    previous_assignment: InboxConversationAssignment | None,
    service_team_id: UUID,
    person_id: UUID | None,
    actor_person_id: UUID | None,
    reason_code: str,
    occurred_at: datetime,
    source_id: str | None,
    decision_mode: InboxRoutingDecisionMode,
    decision_evidence: InboxAgentCandidate | None,
) -> InboxRoutingEvent:
    event = InboxRoutingEvent(
        conversation_id=conversation.id,
        event_type=event_type,
        previous_service_team_id=(
            previous_assignment.service_team_id if previous_assignment else None
        ),
        service_team_id=service_team_id,
        previous_person_id=previous_assignment.person_id
        if previous_assignment
        else None,
        person_id=person_id,
        actor_person_id=actor_person_id,
        decision_mode=decision_mode,
        presence_status=(
            decision_evidence.presence_status if decision_evidence else None
        ),
        presence_observed_at=(
            decision_evidence.presence_observed_at if decision_evidence else None
        ),
        active_conversation_count=(
            decision_evidence.active_conversation_count if decision_evidence else None
        ),
        max_concurrent_conversations=(
            decision_evidence.max_concurrent_conversations
            if decision_evidence
            else None
        ),
        reason_code=reason_code,
        source=InboxAuditSource.routing_command,
        source_id=source_id or f"routing:{uuid4()}",
        evidence_grade=InboxAuditEvidenceGrade.native,
        occurred_at=occurred_at,
    )
    db.add(event)
    db.flush()
    if previous_assignment is not None:
        previous_assignment.is_active = False
        previous_assignment.ended_at = occurred_at
        previous_assignment.ended_by_event_id = event.id
        db.flush()
    return event


def assign_conversation_to_agent(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    person_id: str | UUID,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
    source: str = InboxTeamSource.escalation.value,
    now: datetime | None = None,
    source_id: str | None = None,
    decision_mode: InboxRoutingDecisionMode = InboxRoutingDecisionMode.manual,
    decision_evidence: InboxAgentCandidate | None = None,
    require_team_membership: bool = True,
) -> InboxAssignmentResult:
    team_uuid = _coerce_uuid(service_team_id)
    person_uuid = _coerce_uuid(person_id)
    actor_uuid = _coerce_uuid(assigned_by_person_id)
    assigned_at = now or datetime.now(UTC)
    if team_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=None,
            reason="service_team_id must be a valid UUID",
        )
    if person_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_agent",
            service_team_id=str(team_uuid),
            reason="person_id must be a valid UUID",
        )

    team = db.get(ServiceTeam, team_uuid)
    if team is None or not team.is_active:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=str(team_uuid),
            reason="service_team_id must reference an active team",
        )

    if require_team_membership:
        member = (
            db.query(ServiceTeamMember)
            .join(
                SystemUser,
                SystemUser.person_party_id == ServiceTeamMember.person_id,
            )
            .filter(ServiceTeamMember.team_id == team_uuid)
            .filter(ServiceTeamMember.is_active.is_(True))
            .filter(SystemUser.id == person_uuid)
            .filter(SystemUser.is_active.is_(True))
            .one_or_none()
        )
        if member is None:
            return InboxAssignmentResult(
                kind="invalid_agent",
                service_team_id=str(team_uuid),
                reason="person_id must be an active member of the target team",
            )
    else:
        person_is_active = (
            db.query(SystemUser.id)
            .filter(SystemUser.id == person_uuid)
            .filter(SystemUser.is_active.is_(True))
            .scalar()
        )
        if person_is_active is None:
            return InboxAssignmentResult(
                kind="invalid_agent",
                service_team_id=str(team_uuid),
                reason="person_id must reference an active staff user",
            )

    previous_assignment = _active_assignment(db, conversation)
    set_conversation_owner_team(
        db,
        conversation=conversation,
        service_team_id=team_uuid,
        source=source,
    )
    _append_routing_event(
        db,
        conversation=conversation,
        event_type=(
            InboxRoutingEventType.reassigned
            if previous_assignment is not None
            else InboxRoutingEventType.assigned
        ),
        previous_assignment=previous_assignment,
        service_team_id=team_uuid,
        person_id=person_uuid,
        actor_person_id=actor_uuid,
        reason_code=("reassigned" if previous_assignment else "assigned"),
        occurred_at=assigned_at,
        source_id=source_id,
        decision_mode=decision_mode,
        decision_evidence=decision_evidence,
    )

    assignment = InboxConversationAssignment(
        conversation_id=conversation.id,
        service_team_id=team_uuid,
        person_id=person_uuid,
        assigned_by_person_id=actor_uuid,
        assigned_at=assigned_at,
        is_active=True,
        metadata_={"reason": reason, "source": source},
    )
    db.add(assignment)
    try:
        from app.services import ai_conversation_intake

        session = ai_conversation_intake.active_session_for_conversation(
            db, conversation.id
        )
        if session is not None:
            ai_conversation_intake.complete_session(
                session, state="stopped_human_takeover"
            )
            ai_conversation_intake.mark_conversation_ai_metadata(
                conversation, session=session, active=False
            )
    except Exception:
        pass
    queued_entry = _queue_entry(db, conversation.id)
    _settle_queue_entry(
        db,
        conversation_id=conversation.id,
        status=InboxQueueEntryStatus.promoted,
        now=assigned_at,
    )
    team_inbox_queue_notifications.send_handoff_notice(
        db,
        conversation=conversation,
        entry=queued_entry,
        now=assigned_at,
    )
    _record_escalation_metadata(
        conversation,
        service_team_id=team_uuid,
        assigned_person_id=person_uuid,
        assigned_by_person_id=actor_uuid,
        reason=reason,
        kind="assigned",
        now=assigned_at,
    )
    db.flush()
    team_inbox_agent_introduction.maybe_send_on_pickup(
        db, conversation=conversation, person_id=person_uuid
    )
    return InboxAssignmentResult(
        kind="assigned",
        service_team_id=str(team_uuid),
        assigned_person_id=str(person_uuid),
    )


def queue_conversation_for_team(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
    source: str = InboxTeamSource.escalation.value,
    now: datetime | None = None,
    source_id: str | None = None,
    decision_mode: InboxRoutingDecisionMode = InboxRoutingDecisionMode.manual,
    event_type: InboxRoutingEventType | None = None,
    reason_code: str = "manual_queue",
) -> InboxAssignmentResult:
    team_uuid = _coerce_uuid(service_team_id)
    actor_uuid = _coerce_uuid(assigned_by_person_id)
    queued_at = now or datetime.now(UTC)
    if team_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=None,
            reason="service_team_id must be a valid UUID",
        )

    team = db.get(ServiceTeam, team_uuid)
    if team is None or not team.is_active:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=str(team_uuid),
            reason="service_team_id must reference an active team",
        )

    previous_assignment = _active_assignment(db, conversation)
    set_conversation_owner_team(
        db,
        conversation=conversation,
        service_team_id=team_uuid,
        source=source,
    )
    _append_routing_event(
        db,
        conversation=conversation,
        event_type=event_type
        or (
            InboxRoutingEventType.unassigned
            if previous_assignment is not None
            else InboxRoutingEventType.queued
        ),
        previous_assignment=previous_assignment,
        service_team_id=team_uuid,
        person_id=None,
        actor_person_id=actor_uuid,
        reason_code=reason_code,
        occurred_at=queued_at,
        source_id=source_id,
        decision_mode=decision_mode,
        decision_evidence=None,
    )
    _record_escalation_metadata(
        conversation,
        service_team_id=team_uuid,
        assigned_person_id=None,
        assigned_by_person_id=actor_uuid,
        reason=reason,
        kind="queued",
        now=queued_at,
    )
    entry = _admit_queue_entry(
        db,
        conversation_id=conversation.id,
        service_team_id=team_uuid,
        entered_at=queued_at,
    )
    team_inbox_queue_notifications.send_initial_queue_notice(
        db,
        entry=entry,
        conversation=conversation,
        now=queued_at,
    )
    db.flush()
    return InboxAssignmentResult(
        kind="queued",
        service_team_id=str(team_uuid),
        reason=reason_code,
        queue_entry_id=str(entry.id),
    )


def assign_conversation_to_available_agent(
    db: Session,
    *,
    conversation: InboxConversation,
    service_team_id: str | UUID,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
    source: str = InboxTeamSource.escalation.value,
    now: datetime | None = None,
) -> InboxAssignmentResult:
    team_uuid = _coerce_uuid(service_team_id)
    actor_uuid = _coerce_uuid(assigned_by_person_id)
    assigned_at = now or datetime.now(UTC)
    if team_uuid is None:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=None,
            reason="service_team_id must be a valid UUID",
        )

    team = _lock_team(db, team_uuid)
    if team is None or not team.is_active:
        return InboxAssignmentResult(
            kind="invalid_team",
            service_team_id=str(team_uuid),
            reason="service_team_id must reference an active team",
        )
    candidates = list_available_team_agents(db, team_uuid)
    if not candidates:
        result = queue_conversation_for_team(
            db,
            conversation=conversation,
            service_team_id=team_uuid,
            assigned_by_person_id=actor_uuid,
            reason=reason,
            source=source,
            now=assigned_at,
            decision_mode=InboxRoutingDecisionMode.automatic,
            event_type=InboxRoutingEventType.auto_assignment_declined,
            reason_code="no_available_agent",
        )
        return InboxAssignmentResult(
            kind=result.kind,
            service_team_id=result.service_team_id,
            reason="no_available_agent",
        )

    selected, cursor = _select_round_robin_candidate(
        db, service_team_id=team_uuid, candidates=candidates
    )
    source_id = f"auto-assign:{conversation.id}:{cursor.rotation_count}"
    return assign_conversation_to_agent(
        db,
        conversation=conversation,
        service_team_id=team_uuid,
        person_id=selected.person_id,
        assigned_by_person_id=actor_uuid,
        reason=reason,
        source=source,
        now=assigned_at,
        source_id=source_id,
        decision_mode=InboxRoutingDecisionMode.automatic,
        decision_evidence=selected,
    )


def escalate_conversation(
    db: Session,
    *,
    conversation_id: str | UUID,
    service_team_id: str | UUID,
    assigned_person_id: str | UUID | None = None,
    auto_assign: bool = False,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
) -> InboxAssignmentResult:
    conversation_uuid = _coerce_uuid(conversation_id)
    conversation = (
        db.get(InboxConversation, conversation_uuid) if conversation_uuid else None
    )
    if conversation is None or not conversation.is_active:
        return InboxAssignmentResult(
            kind="conversation_not_found",
            service_team_id=None,
            reason="Conversation not found",
        )
    if conversation.status == InboxConversationStatus.resolved.value:
        return InboxAssignmentResult(
            kind="conversation_resolved",
            service_team_id=str(service_team_id) if service_team_id else None,
            reason="Resolved conversations cannot be escalated",
        )

    if assigned_person_id is not None:
        return assign_conversation_to_agent(
            db,
            conversation=conversation,
            service_team_id=service_team_id,
            person_id=assigned_person_id,
            assigned_by_person_id=assigned_by_person_id,
            reason=reason,
        )
    if auto_assign:
        return assign_conversation_to_available_agent(
            db,
            conversation=conversation,
            service_team_id=service_team_id,
            assigned_by_person_id=assigned_by_person_id,
            reason=reason,
        )
    return queue_conversation_for_team(
        db,
        conversation=conversation,
        service_team_id=service_team_id,
        assigned_by_person_id=assigned_by_person_id,
        reason=reason,
    )


def escalate_conversation_committed(
    db: Session,
    *,
    conversation_id: str | UUID,
    service_team_id: str | UUID,
    assigned_person_id: str | UUID | None = None,
    auto_assign: bool = False,
    assigned_by_person_id: str | UUID | None = None,
    reason: str | None = None,
) -> InboxAssignmentResult:
    return _commit(
        db,
        lambda: escalate_conversation(
            db,
            conversation_id=conversation_id,
            service_team_id=service_team_id,
            assigned_person_id=assigned_person_id,
            auto_assign=auto_assign,
            assigned_by_person_id=assigned_by_person_id,
            reason=reason,
        ),
    )


def sweep_queued_conversations(
    db: Session, command: InboxQueueSweepCommand
) -> InboxQueueSweepResult:
    if command.limit < 1:
        raise ValueError("limit must be positive")
    observed_at = command.now or datetime.now(UTC)

    def _operation() -> InboxQueueSweepResult:
        promoted = 0
        cancelled = 0
        entries = (
            db.query(InboxConversationQueueEntry)
            .filter(
                InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
            )
            .order_by(
                InboxConversationQueueEntry.entered_at.asc(),
                InboxConversationQueueEntry.queue_position.asc(),
            )
            .limit(command.limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for entry in entries:
            conversation = db.get(InboxConversation, entry.conversation_id)
            if (
                conversation is None
                or not conversation.is_active
                or conversation.status == InboxConversationStatus.resolved.value
            ):
                entry.status = InboxQueueEntryStatus.cancelled.value
                entry.settled_at = observed_at
                cancelled += 1
                continue
            if _active_assignment(db, conversation) is not None:
                entry.status = InboxQueueEntryStatus.cancelled.value
                entry.settled_at = observed_at
                cancelled += 1
                continue
            team = _lock_team(db, entry.service_team_id)
            if team is None or not team.is_active:
                entry.status = InboxQueueEntryStatus.cancelled.value
                entry.settled_at = observed_at
                cancelled += 1
                continue
            candidates = list_available_team_agents(db, entry.service_team_id)
            if not candidates:
                continue
            selected, cursor = _select_round_robin_candidate(
                db, service_team_id=entry.service_team_id, candidates=candidates
            )
            result = assign_conversation_to_agent(
                db,
                conversation=conversation,
                service_team_id=entry.service_team_id,
                person_id=selected.person_id,
                reason="FIFO queue capacity became available",
                source=InboxTeamSource.routing_rule.value,
                now=observed_at,
                source_id=f"queue-promote:{entry.id}:{cursor.rotation_count}",
                decision_mode=InboxRoutingDecisionMode.automatic,
                decision_evidence=selected,
            )
            if result.kind == "assigned":
                promoted += 1
        remaining = (
            db.query(func.count(InboxConversationQueueEntry.id))
            .filter(
                InboxConversationQueueEntry.status == InboxQueueEntryStatus.queued.value
            )
            .scalar()
            or 0
        )
        return InboxQueueSweepResult(
            promoted=promoted,
            cancelled=cancelled,
            remaining=int(remaining),
        )

    return execute_owner_command(
        db,
        definition=_ROUTING_COMMAND,
        context=command.context,
        operation=_operation,
    )
