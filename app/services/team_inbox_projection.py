"""Typed Team Inbox list/detail/UI projection owner."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html import unescape
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.service_team import ServiceTeamMember
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
    InboxConversationTeam,
)
from app.services import (
    conversation_ticket_handoff,
    subscriber_summary,
    team_inbox_contact_links,
    team_inbox_metrics,
    team_inbox_operations,
    team_inbox_read,
    team_inbox_read_state,
)
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
    request_needs_canonicalization,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class InboxListSort(StrEnum):
    priority = "priority"
    last_message_at = "last_message_at"
    created_at = "created_at"


class InboxSortDirection(StrEnum):
    ascending = "asc"
    descending = "desc"


INBOX_LIST_DEFINITION = ListDefinition(
    key="team_inbox",
    fields=(
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("channel_type", "Channel", filterable=True),
        ListFieldDefinition("service_team_id", "Team", filterable=True),
        # Declared so the multi-team "My team" scope survives a canonical
        # redirect; undeclared, the redirect silently widened the queue back to
        # every team.
        ListFieldDefinition("service_team_ids", "Teams", filterable=True),
        ListFieldDefinition("assigned_person_id", "Assignee", filterable=True),
        ListFieldDefinition("contact_resolution_status", "Contact", filterable=True),
        ListFieldDefinition("needs_response", "Unreplied", filterable=True),
        ListFieldDefinition("needs_attention", "Needs attention", filterable=True),
        ListFieldDefinition("ai_handling", "AI handling", filterable=True),
        ListFieldDefinition("has_ticket", "Sent to ticket", filterable=True),
        ListFieldDefinition("activity_from", "Active from", filterable=True),
        ListFieldDefinition("activity_to", "Active to", filterable=True),
        ListFieldDefinition("muted", "Muted", filterable=True),
        ListFieldDefinition("snoozed", "Snoozed", filterable=True),
        ListFieldDefinition("open_only", "Open only", filterable=True),
        ListFieldDefinition("unassigned", "Unassigned", filterable=True),
        ListFieldDefinition("unread", "Unread", filterable=True),
        ListFieldDefinition("priority_at_most", "Max priority", filterable=True),
        ListFieldDefinition("priority", "Priority", sortable=True),
        ListFieldDefinition("last_message_at", "Last activity", sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort=InboxListSort.priority.value,
    default_sort_dir=InboxSortDirection.ascending.value,
    per_page_options=(10, 25, 50, 100),
    default_per_page=25,
)


@dataclass(frozen=True, slots=True)
class InboxQueueRequest:
    search: str | None = None
    status: str | None = None
    channel_type: str | None = None
    service_team_id: str | UUID | None = None
    # Multi-team scope for "My team": an agent may belong to several teams and
    # the my_team count spans all of them, so the filter must select the same set.
    service_team_ids: tuple[str, ...] = ()
    assigned_person_id: str | UUID | None = None
    needs_response: bool = False
    needs_attention: bool = False
    contact_resolution_status: str | None = None
    priority_at_most: int | None = None
    muted: bool | None = None
    snoozed: bool | None = None
    open_only: bool = False
    unassigned: bool = False
    unread: bool = False
    ai_handling: bool | None = None
    has_ticket: bool | None = None
    activity_from: datetime | None = None
    activity_to: datetime | None = None
    sort_by: str | None = None
    sort_dir: str | None = None
    page: int = 1
    per_page: int = 25
    selected_conversation_id: str | UUID | None = None
    actor_person_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ContactLinkCandidate:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class ContactLinkCandidateSet:
    subscribers: tuple[ContactLinkCandidate, ...]
    resellers: tuple[ContactLinkCandidate, ...]


@dataclass(frozen=True, slots=True)
class WhatsAppContactOption:
    id: str
    name: str
    whatsapp_address: str


@dataclass(frozen=True, slots=True)
class InboxActionEligibility:
    can_reply: bool
    can_resolve: bool
    can_reopen: bool
    can_link_contact: bool
    can_mark_read: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InboxPriorityOption:
    value: int
    label: str


INBOX_PRIORITY_OPTIONS = (
    InboxPriorityOption(value=100, label="None"),
    InboxPriorityOption(value=75, label="Low"),
    InboxPriorityOption(value=50, label="Medium"),
    InboxPriorityOption(value=25, label="High"),
    InboxPriorityOption(value=0, label="Urgent"),
)


@dataclass(frozen=True, slots=True)
class InboxConversationProjection:
    timeline: team_inbox_read.InboxConversationTimeline
    subscriber_summary: Mapping[str, object] | None
    contact_link_candidates: ContactLinkCandidateSet
    label_options: tuple[team_inbox_operations.LabelOption, ...]
    conversation_labels: tuple[team_inbox_operations.LabelOption, ...]
    macro_options: tuple[team_inbox_operations.MacroOption, ...]
    template_options: tuple[team_inbox_operations.MessageTemplateOption, ...]
    action_eligibility: InboxActionEligibility
    is_unread: bool
    priority_options: tuple[InboxPriorityOption, ...]


@dataclass(frozen=True, slots=True)
class InboxServiceTeamOption:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class InboxAgentOption:
    id: UUID
    name: str
    initials: str


@dataclass(frozen=True, slots=True)
class InboxManagerAgent:
    id: UUID
    name: str
    initials: str
    presence_status: str
    active_chats: int
    max_concurrent_conversations: int | None


@dataclass(frozen=True, slots=True)
class InboxManagerChannelCount:
    key: str
    label: str
    count: int


@dataclass(frozen=True, slots=True)
class InboxManagerDashboardProjection:
    online_agents: int
    chats_with_online_agents: int
    unassigned: int
    needs_attention: int
    open: int
    pending: int
    resolved_today: int
    agents: tuple[InboxManagerAgent, ...]
    channel_split: tuple[InboxManagerChannelCount, ...]
    active_chats: tuple[team_inbox_read.InboxConversationListRow, ...]


@dataclass(frozen=True, slots=True)
class InboxAssignmentCounts:
    all: int
    assigned_to_me: int
    my_team: int
    ai_handling: int
    unassigned: int
    # The teams the actor belongs to, so the "My team" filter can select exactly
    # the cohort my_team counted rather than approximating it.
    my_team_ids: tuple[str, ...]
    unreplied: int
    needs_attention: int


@dataclass(frozen=True, slots=True)
class InboxQueueProjection:
    rows: tuple[team_inbox_read.InboxConversationListRow, ...]
    queue_metrics: team_inbox_operations.InboxQueueMetrics
    operator_unread_count: int
    count: int
    list_query: ListQuery
    page_meta: PageMeta
    status: str
    channel_type: str
    service_team_id: str
    assigned_person_id: str
    needs_response: bool
    needs_attention: bool
    contact_resolution_status: str
    priority_at_most: int | None
    muted: bool | None
    snoozed: bool | None
    open_only: bool
    unassigned: bool
    unread: bool
    ai_handling: bool | None
    has_ticket: bool | None
    activity_from: str | None
    activity_to: str | None
    service_team_options: tuple[InboxServiceTeamOption, ...]
    agent_options: tuple[InboxAgentOption, ...]
    assignment_counts: InboxAssignmentCounts
    status_options: tuple[str, ...]
    channel_options: tuple[str, ...]
    label_options: tuple[team_inbox_operations.LabelOption, ...]
    saved_filters: tuple[team_inbox_operations.SavedFilterOption, ...]
    selected: InboxConversationProjection | None
    canonical_url: str | None


def _uuid(value: object) -> UUID | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        return UUID(candidate)
    except (TypeError, ValueError, AttributeError):
        return None


def _initials(first_name: str, last_name: str, display_name: str | None) -> str:
    words = (display_name or f"{first_name} {last_name}").split()
    return "".join(word[0] for word in words[:2] if word).upper() or "AG"


def list_agent_options(db: Session) -> tuple[InboxAgentOption, ...]:
    rows = (
        db.query(SystemUser)
        .join(
            ServiceTeamMember,
            ServiceTeamMember.person_id == SystemUser.person_party_id,
        )
        .filter(SystemUser.is_active.is_(True))
        .filter(ServiceTeamMember.is_active.is_(True))
        .distinct()
        .order_by(SystemUser.first_name.asc(), SystemUser.last_name.asc())
        .all()
    )
    return tuple(
        InboxAgentOption(
            id=row.id,
            name=(
                row.display_name
                or f"{row.first_name} {row.last_name}".strip()
                or row.email
            ),
            initials=_initials(row.first_name, row.last_name, row.display_name),
        )
        for row in rows
    )


def _resolved_today_count(
    db: Session,
    *,
    now: datetime | None = None,
) -> int:
    """Count today's authoritative status transitions to resolved.

    Conversation ``updated_at`` can change for unrelated reasons, so the
    status-history observation written by the command owner is the reliable
    input for this dashboard projection.
    """

    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    count = 0
    rows = (
        db.query(InboxConversation.metadata_)
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status == InboxConversationStatus.resolved.value)
        .all()
    )
    for (metadata,) in rows:
        history = metadata.get("status_history") if isinstance(metadata, dict) else None
        if not isinstance(history, list):
            continue
        for event in reversed(history):
            if not isinstance(event, dict) or event.get("to") != "resolved":
                continue
            try:
                occurred_at = datetime.fromisoformat(str(event.get("at") or ""))
            except ValueError:
                break
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            if occurred_at.astimezone(UTC).date() == today:
                count += 1
            break
    return count


def build_manager_dashboard_projection(
    db: Session,
    *,
    queue_metrics: team_inbox_operations.InboxQueueMetrics,
    needs_attention: int,
) -> InboxManagerDashboardProjection:
    """Build the read-only manager panel from Inbox-owned observations."""

    agent_options = list_agent_options(db)
    person_ids = [agent.id for agent in agent_options]
    presence_rows = (
        db.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id.in_(person_ids))
        .all()
        if person_ids
        else []
    )
    presence_by_person = {row.person_id: row for row in presence_rows}
    online_person_ids = {
        row.person_id
        for row in presence_rows
        if (row.manual_override_status or row.status) == "online"
    }

    active_assignments = (
        db.query(InboxConversationAssignment)
        .join(
            InboxConversation,
            InboxConversation.id == InboxConversationAssignment.conversation_id,
        )
        .filter(InboxConversationAssignment.is_active.is_(True))
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
        .all()
    )
    chat_counts = Counter(row.person_id for row in active_assignments)
    chats_with_online_agents = len(
        {
            row.conversation_id
            for row in active_assignments
            if row.person_id in online_person_ids
        }
    )
    agent_rows: list[InboxManagerAgent] = []
    for agent in agent_options:
        presence = presence_by_person.get(agent.id)
        agent_rows.append(
            InboxManagerAgent(
                id=agent.id,
                name=agent.name,
                initials=agent.initials,
                presence_status=(
                    (presence.manual_override_status or presence.status)
                    if presence is not None
                    else "offline"
                ),
                active_chats=chat_counts[agent.id],
                max_concurrent_conversations=(
                    presence.max_concurrent_conversations
                    if presence is not None
                    else None
                ),
            )
        )
    agents = tuple(agent_rows)

    raw_channel_counts = {
        channel: int(count)
        for channel, count in (
            db.query(InboxConversation.channel_type, func.count(InboxConversation.id))
            .filter(InboxConversation.is_active.is_(True))
            .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
            .group_by(InboxConversation.channel_type)
            .all()
        )
    }
    declared_channels = (
        ("email", "Email"),
        ("whatsapp", "WhatsApp"),
        ("facebook_messenger", "Facebook"),
        ("instagram_dm", "Instagram"),
    )
    known_keys = {key for key, _label in declared_channels}
    channel_split = tuple(
        InboxManagerChannelCount(
            key=key,
            label=label,
            count=raw_channel_counts.get(key, 0),
        )
        for key, label in declared_channels
    ) + (
        InboxManagerChannelCount(
            key="other",
            label="Other",
            count=sum(
                count
                for key, count in raw_channel_counts.items()
                if key not in known_keys
            ),
        ),
    )

    open_count = int(
        db.query(func.count(InboxConversation.id))
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status == InboxConversationStatus.open.value)
        .scalar()
        or 0
    )
    pending_count = int(
        db.query(func.count(InboxConversation.id))
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status == InboxConversationStatus.pending.value)
        .scalar()
        or 0
    )
    active_chats = tuple(
        team_inbox_read.list_conversations(
            db,
            open_only=True,
            limit=8,
        ).items
    )
    return InboxManagerDashboardProjection(
        online_agents=len(online_person_ids),
        chats_with_online_agents=chats_with_online_agents,
        unassigned=queue_metrics.unassigned_open,
        needs_attention=needs_attention,
        open=open_count,
        pending=pending_count,
        resolved_today=_resolved_today_count(db),
        agents=agents,
        channel_split=channel_split,
        active_chats=active_chats,
    )


def _assignment_counts(
    db: Session,
    *,
    actor_person_id: UUID | None,
    queue_metrics: team_inbox_operations.InboxQueueMetrics,
) -> InboxAssignmentCounts:
    all_count = team_inbox_read.list_conversations(db, limit=1).count
    assigned_to_me = (
        team_inbox_read.list_conversations(
            db,
            assigned_person_id=actor_person_id,
            limit=1,
        ).count
        if actor_person_id is not None
        else 0
    )
    my_team = 0
    my_team_ids: tuple[str, ...] = ()
    if actor_person_id is not None:
        team_ids = [
            row[0]
            for row in db.query(ServiceTeamMember.team_id)
            .join(
                SystemUser,
                SystemUser.person_party_id == ServiceTeamMember.person_id,
            )
            .filter(SystemUser.id == actor_person_id)
            .filter(SystemUser.is_active.is_(True))
            .filter(ServiceTeamMember.is_active.is_(True))
            .all()
        ]
        my_team_ids = tuple(str(value) for value in team_ids)
        if team_ids:
            my_team = int(
                db.query(func.count(func.distinct(InboxConversation.id)))
                .join(
                    InboxConversationTeam,
                    InboxConversationTeam.conversation_id == InboxConversation.id,
                )
                .filter(InboxConversation.is_active.is_(True))
                .filter(
                    InboxConversation.status != InboxConversationStatus.resolved.value
                )
                .filter(InboxConversationTeam.is_active.is_(True))
                .filter(InboxConversationTeam.service_team_id.in_(team_ids))
                .scalar()
                or 0
            )
    ai_handling = int(
        db.query(func.count(InboxConversation.id))
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.metadata_["ai_handling"].as_boolean().is_(True))
        .scalar()
        or 0
    )
    return InboxAssignmentCounts(
        all=all_count,
        assigned_to_me=assigned_to_me,
        my_team=my_team,
        ai_handling=ai_handling,
        my_team_ids=my_team_ids,
        unassigned=queue_metrics.unassigned_open,
        unreplied=queue_metrics.needs_response,
        needs_attention=team_inbox_read.list_conversations(
            db,
            needs_attention=True,
            limit=1,
        ).count,
    )


def _candidate_terms(
    timeline: team_inbox_read.InboxConversationTimeline,
) -> tuple[str, ...]:
    values: list[object] = [
        timeline.contact_address,
        timeline.subject,
        timeline.external_thread_id,
    ]
    if timeline.metadata:
        resolution = timeline.metadata.get("contact_resolution")
        if isinstance(resolution, dict):
            values.extend(
                (
                    resolution.get("normalized_contact"),
                    resolution.get("subscriber_id"),
                    resolution.get("reseller_id"),
                )
            )
    terms: list[str] = []
    for value in values:
        candidate = str(value or "").strip()
        if len(candidate) >= 3 and candidate not in terms:
            terms.append(candidate)
    return tuple(terms[:6])


def contact_link_candidates(
    db: Session,
    timeline: team_inbox_read.InboxConversationTimeline,
) -> ContactLinkCandidateSet:
    values = team_inbox_contact_links.contact_link_candidates(
        db, list(_candidate_terms(timeline))
    )
    return ContactLinkCandidateSet(
        subscribers=tuple(
            ContactLinkCandidate(id=str(item["id"]), label=str(item["label"]))
            for item in values.get("subscribers", [])
        ),
        resellers=tuple(
            ContactLinkCandidate(id=str(item["id"]), label=str(item["label"]))
            for item in values.get("resellers", [])
        ),
    )


def get_conversation_projection(
    db: Session,
    *,
    conversation_id: UUID,
    actor_person_id: UUID | None,
) -> InboxConversationProjection | None:
    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        return None
    is_resolved = timeline.status == InboxConversationStatus.resolved.value
    summary = subscriber_summary.subscriber_summary(db, timeline.subscriber_id)
    return InboxConversationProjection(
        timeline=timeline,
        subscriber_summary=summary,
        contact_link_candidates=contact_link_candidates(db, timeline),
        label_options=tuple(team_inbox_operations.list_labels(db)),
        conversation_labels=tuple(
            team_inbox_operations.conversation_labels(db, conversation_id)
        ),
        macro_options=tuple(
            team_inbox_operations.list_macros(db, person_id=actor_person_id)
        ),
        template_options=tuple(
            team_inbox_operations.list_templates(db, channel_type=timeline.channel_type)
        ),
        action_eligibility=InboxActionEligibility(
            can_reply=not is_resolved,
            can_resolve=not is_resolved,
            can_reopen=is_resolved,
            can_link_contact=bool(timeline.contact_address),
            can_mark_read=actor_person_id is not None,
            reason="Resolved conversations must be reopened before replying."
            if is_resolved
            else None,
        ),
        is_unread=(
            team_inbox_read_state.conversation_is_unread(
                db,
                conversation_id=conversation_id,
                person_id=actor_person_id,
            )
            if actor_person_id is not None
            else False
        ),
        priority_options=INBOX_PRIORITY_OPTIONS,
    )


def list_whatsapp_contacts(
    db: Session,
    *,
    search: str,
    limit: int = 20,
) -> tuple[WhatsAppContactOption, ...]:
    """Search canonical active Party contact points for WhatsApp reachability."""

    from app.models.party import Party, PartyContactPoint, PartyIdentityStatus

    term = str(search or "").strip()
    if len(term) < 2:
        return ()
    like = f"%{term}%"
    matching_contact_party_ids = (
        db.query(PartyContactPoint.party_id)
        .filter(PartyContactPoint.is_active.is_(True))
        .filter(
            (PartyContactPoint.display_value.ilike(like))
            | (PartyContactPoint.normalized_value.ilike(like))
        )
        .scalar_subquery()
    )
    rows = (
        db.query(Party, PartyContactPoint)
        .join(PartyContactPoint, PartyContactPoint.party_id == Party.id)
        .filter(Party.status == PartyIdentityStatus.active.value)
        .filter(PartyContactPoint.is_active.is_(True))
        .filter(PartyContactPoint.channel_type.in_(("whatsapp", "phone", "sms")))
        .filter(
            (Party.display_name.ilike(like))
            | (Party.id.in_(matching_contact_party_ids))
        )
        .order_by(
            func.lower(Party.display_name).asc(),
            PartyContactPoint.is_primary.desc(),
            PartyContactPoint.created_at.asc(),
        )
        .limit(max(1, min(int(limit), 20)) * 4)
        .all()
    )
    by_party: dict[UUID, tuple[Party, list[PartyContactPoint]]] = {}
    for party, point in rows:
        current = by_party.setdefault(party.id, (party, []))
        current[1].append(point)
    options: list[WhatsAppContactOption] = []
    channel_rank = {"whatsapp": 0, "phone": 1, "sms": 2}
    for party, points in by_party.values():
        selected = sorted(
            points,
            key=lambda point: (
                channel_rank.get(point.channel_type, 9),
                not point.is_primary,
                point.created_at,
            ),
        )[0]
        address = str(selected.normalized_value or selected.display_value or "").strip()
        if not address:
            continue
        options.append(
            WhatsAppContactOption(
                id=str(party.id),
                name=party.display_name,
                whatsapp_address=address,
            )
        )
    return tuple(options[: max(1, min(int(limit), 20))])


def _plain_ai_message_body(value: object | None) -> str:
    """Remove presentation markup before the AI port applies PII redaction."""

    without_tags = _HTML_TAG_RE.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", unescape(without_tags)).strip()[:600]


def build_ai_reply_projection(
    db: Session,
    *,
    conversation_id: UUID,
) -> dict[str, object] | None:
    """Build the bounded, owned context supplied to the inbox AI advisor."""

    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        return None

    from app.services.brand_profiles import resolve_brand

    active_assignment = next(
        (
            assignment
            for assignment in reversed(timeline.assignments)
            if assignment.is_active
        ),
        None,
    )
    assigned_agent_name: str | None = None
    if active_assignment is not None:
        agent = db.get(SystemUser, _uuid(active_assignment.person_id))
        if agent is not None:
            assigned_agent_name = (
                agent.display_name
                or f"{agent.first_name} {agent.last_name}".strip()
                or agent.email
            )

    labels = team_inbox_operations.conversation_labels(db, conversation_id)[:8]
    linked_tickets = conversation_ticket_handoff.list_for_conversation(
        db, conversation_id
    )
    ticket = linked_tickets[0] if linked_tickets else None
    recent_messages = timeline.messages[-12:]
    metadata = timeline.metadata if isinstance(timeline.metadata, dict) else {}
    contact_name = next(
        (
            str(metadata.get(key)).strip()
            for key in ("contact_name", "sender_name", "profile_name")
            if str(metadata.get(key) or "").strip()
        ),
        None,
    )
    return {
        "company_name": resolve_brand(
            db,
            subscriber_id=timeline.subscriber_id,
        ).legal_name,
        "conversation_id": timeline.id,
        "channel": recent_messages[-1].channel_type
        if recent_messages
        else timeline.channel_type,
        "status": timeline.status,
        "priority": timeline.priority,
        "subject": timeline.subject,
        "contact_display_name": contact_name,
        "assigned_agent_name": assigned_agent_name,
        "tags": [label.name for label in labels],
        "linked_ticket": (
            {
                "number": ticket.number,
                "title": ticket.title,
                "status": ticket.status,
                "type": ticket.ticket_type,
                "priority": ticket.priority,
            }
            if ticket is not None
            else None
        ),
        "messages": [
            {
                "direction": (
                    "customer" if message.direction == "inbound" else "agent"
                ),
                "body": _plain_ai_message_body(message.body),
                "occurred_at": (
                    message.received_at or message.sent_at or message.created_at
                ).isoformat(),
            }
            for message in recent_messages
            if message.direction in {"inbound", "outbound"}
            and str(message.body or "").strip()
        ],
    }


ACTIVITY_INPUT_FORMAT = "%Y-%m-%dT%H:%M"


def _tristate(value: bool | None) -> str | None:
    return ("true" if value else "false") if value is not None else None


def _activity_param(value: datetime | None) -> str | None:
    return value.strftime(ACTIVITY_INPUT_FORMAT) if value is not None else None


def _filter_params(
    *,
    status: str | None,
    channel_type: str | None,
    service_team_id: str | None,
    service_team_ids: tuple[str, ...],
    assigned_person_id: str | None,
    contact_resolution_status: str | None,
    needs_response: bool,
    needs_attention: bool,
    ai_handling: bool | None,
    has_ticket: bool | None,
    activity_from: datetime | None,
    activity_to: datetime | None,
    muted: bool | None,
    snoozed: bool | None,
    open_only: bool,
    unassigned: bool,
    unread: bool,
    priority_at_most: int | None,
) -> dict[str, str | None]:
    """One filter contract for the query, the canonical URL and the redirect check.

    These three were previously spelled out separately, and the newest filters
    reached only the read model — so any canonical redirect dropped
    ``ai_handling``, ``has_ticket``, the activity window and the multi-team
    scope, silently widening the operator's queue.
    """

    return {
        "status": status,
        "channel_type": channel_type,
        "service_team_id": service_team_id,
        "service_team_ids": ",".join(service_team_ids) or None,
        "assigned_person_id": assigned_person_id,
        "contact_resolution_status": contact_resolution_status,
        "needs_response": "true" if needs_response else None,
        "needs_attention": "true" if needs_attention else None,
        "ai_handling": _tristate(ai_handling),
        "has_ticket": _tristate(has_ticket),
        "activity_from": _activity_param(activity_from),
        "activity_to": _activity_param(activity_to),
        "muted": _tristate(muted),
        "snoozed": _tristate(snoozed),
        "open_only": "true" if open_only else None,
        "unassigned": "true" if unassigned else None,
        "unread": "true" if unread else None,
        "priority_at_most": str(priority_at_most)
        if priority_at_most is not None
        else None,
    }


def build_queue_projection(
    db: Session,
    request: InboxQueueRequest,
) -> InboxQueueProjection:
    """Own filter normalization, sort, pagination, cohorts, and UI state."""

    search = request.search
    raw_status = request.status
    raw_channel = request.channel_type
    raw_team_id = request.service_team_id
    raw_assignee_id = request.assigned_person_id
    raw_team_text = str(raw_team_id) if raw_team_id is not None else None
    raw_assignee_text = str(raw_assignee_id) if raw_assignee_id is not None else None
    needs_response = request.needs_response
    needs_attention = request.needs_attention
    raw_contact_status = request.contact_resolution_status
    raw_priority = request.priority_at_most
    muted = request.muted
    snoozed = request.snoozed
    open_only = request.open_only
    unassigned = request.unassigned
    unread = request.unread
    raw_sort = request.sort_by
    raw_direction = request.sort_dir
    raw_page = request.page
    raw_per_page = request.per_page

    status = (
        raw_status
        if raw_status in {item.value for item in InboxConversationStatus}
        else None
    )
    channel = (
        raw_channel
        if raw_channel in {item.value for item in InboxChannelType}
        else None
    )
    # Ill-formed team ids are dropped here rather than passed down, so the
    # echoed filter state and the canonical URL carry what was actually
    # applied. The single-team dropdown and the multi-team "My team" cohort
    # now intersect cleanly in the read model — they used to be two joins on
    # the same relation, which SQLAlchemy refuses.
    team_id_scope = tuple(
        str(value)
        for value in (_uuid(item) for item in request.service_team_ids)
        if value is not None
    )
    team_id = _uuid(raw_team_id)
    assignee_id = _uuid(raw_assignee_id)
    contact_status = str(raw_contact_status or "").strip() or None
    priority = (
        raw_priority if raw_priority is not None and 0 <= raw_priority <= 999 else None
    )
    sort = (
        InboxListSort(raw_sort).value
        if raw_sort in {item.value for item in InboxListSort}
        else INBOX_LIST_DEFINITION.default_sort
    )
    direction = (
        InboxSortDirection(raw_direction).value
        if raw_direction in {item.value for item in InboxSortDirection}
        else None
    )
    safe_per_page = (
        raw_per_page
        if raw_per_page in INBOX_LIST_DEFINITION.per_page_options
        else INBOX_LIST_DEFINITION.default_per_page
    )
    normalized_filters = _filter_params(
        status=status,
        channel_type=channel,
        service_team_id=str(team_id) if team_id else None,
        service_team_ids=team_id_scope,
        assigned_person_id=str(assignee_id) if assignee_id else None,
        contact_resolution_status=contact_status,
        needs_response=needs_response,
        needs_attention=needs_attention,
        ai_handling=request.ai_handling,
        has_ticket=request.has_ticket,
        activity_from=request.activity_from,
        activity_to=request.activity_to,
        muted=muted,
        snoozed=snoozed,
        open_only=open_only,
        unassigned=unassigned,
        unread=unread,
        priority_at_most=priority,
    )
    requested_query = INBOX_LIST_DEFINITION.build_query(
        search=search,
        filters=normalized_filters,
        sort_by=sort,
        sort_dir=direction,
        page=max(1, raw_page),
        per_page=safe_per_page,
    )

    def fetch(query: ListQuery) -> team_inbox_read.InboxConversationListResult:
        return team_inbox_read.list_conversations(
            db,
            search=query.search,
            status=status,
            channel_type=channel,
            service_team_id=team_id,
            service_team_ids=team_id_scope,
            ai_handling=request.ai_handling,
            has_ticket=request.has_ticket,
            activity_from=request.activity_from,
            activity_to=request.activity_to,
            assigned_person_id=assignee_id,
            needs_response=needs_response,
            needs_attention=needs_attention,
            contact_resolution_status=contact_status,
            priority_at_most=priority,
            muted=muted,
            snoozed=snoozed,
            open_only=open_only,
            unassigned=unassigned,
            operator_person_id=request.actor_person_id,
            unread_only=unread,
            order_by=query.sort_by,
            order_dir=query.sort_dir,
            limit=query.per_page,
            offset=query.offset,
        )

    result = fetch(requested_query)
    page_meta = PageMeta.from_query(requested_query, result.count)
    list_query = requested_query.with_page(page_meta.page)
    if list_query.page != requested_query.page:
        result = fetch(list_query)
    selected_id = _uuid(request.selected_conversation_id)
    canonical_url = None
    if request_needs_canonicalization(
        list_query,
        search=search,
        filters=_filter_params(
            status=raw_status,
            channel_type=raw_channel,
            service_team_id=raw_team_text,
            service_team_ids=team_id_scope,
            assigned_person_id=raw_assignee_text,
            contact_resolution_status=raw_contact_status,
            needs_response=needs_response,
            needs_attention=needs_attention,
            ai_handling=request.ai_handling,
            has_ticket=request.has_ticket,
            activity_from=request.activity_from,
            activity_to=request.activity_to,
            muted=muted,
            snoozed=snoozed,
            open_only=open_only,
            unassigned=unassigned,
            unread=unread,
            priority_at_most=raw_priority,
        ),
        sort_by=raw_sort,
        sort_dir=raw_direction,
        page=raw_page,
        per_page=raw_per_page,
    ):
        canonical_url = list_query.url("/admin/inbox")
        if selected_id is not None:
            canonical_url = f"{canonical_url}&conversation_id={selected_id}"

    selected = (
        get_conversation_projection(
            db,
            conversation_id=selected_id,
            actor_person_id=request.actor_person_id,
        )
        if selected_id is not None
        else None
    )
    service_teams = team_inbox_metrics.active_service_team_options(db)
    queue_metrics = team_inbox_operations.queue_metrics(db)
    return InboxQueueProjection(
        rows=tuple(result.items),
        queue_metrics=queue_metrics,
        operator_unread_count=(
            team_inbox_read_state.unread_conversation_count(
                db, person_id=request.actor_person_id
            )
            if request.actor_person_id is not None
            else 0
        ),
        count=result.count,
        list_query=list_query,
        page_meta=page_meta,
        status=status or "",
        channel_type=channel or "",
        service_team_id=str(team_id) if team_id else "",
        assigned_person_id=str(assignee_id) if assignee_id else "",
        needs_response=needs_response,
        needs_attention=needs_attention,
        contact_resolution_status=contact_status or "",
        priority_at_most=priority,
        muted=muted,
        snoozed=snoozed,
        open_only=open_only,
        unassigned=unassigned,
        unread=unread,
        ai_handling=request.ai_handling,
        has_ticket=request.has_ticket,
        activity_from=_activity_param(request.activity_from),
        activity_to=_activity_param(request.activity_to),
        service_team_options=tuple(
            InboxServiceTeamOption(id=team.id, name=team.name) for team in service_teams
        ),
        agent_options=list_agent_options(db),
        assignment_counts=_assignment_counts(
            db,
            actor_person_id=request.actor_person_id,
            queue_metrics=queue_metrics,
        ),
        status_options=tuple(item.value for item in InboxConversationStatus),
        channel_options=tuple(item.value for item in InboxChannelType),
        label_options=tuple(team_inbox_operations.list_labels(db)),
        saved_filters=tuple(
            team_inbox_operations.list_saved_filters(
                db, person_id=request.actor_person_id
            )
        ),
        selected=selected,
        canonical_url=canonical_url,
    )
