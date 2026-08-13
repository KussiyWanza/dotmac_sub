"""Typed Team Inbox list/detail/UI projection owner."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html import unescape
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from app.models.service_team import ServiceTeamMember
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.support import canonical_ticket_status_value
from app.models.system_user import SystemUser
from app.models.team_inbox import (
    InboxAgentPresence,
    InboxAgentPresenceStatus,
    InboxChannelType,
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationReadState,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxRoutingEvent,
    InboxStatusTransitionEvent,
)
from app.services import (
    conversation_ticket_handoff,
    service_team_lifecycle,
    subscriber_summary,
    team_inbox_contact_links,
    team_inbox_filters,
    team_inbox_media,
    team_inbox_operations,
    team_inbox_read,
    team_inbox_read_state,
)
from app.services.catalog import plan_family_catalogues
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
    request_needs_canonicalization,
)
from app.services.sales import lead_intake

_HTML_TAG_RE = re.compile(r"<[^>]+>")

SAFE_INLINE_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
SAFE_INLINE_AUDIO_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "audio/aac",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
    }
)
SAFE_INLINE_VIDEO_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/ogg",
        "video/quicktime",
        "video/webm",
    }
)


class InboxMediaBrowserPresentation(StrEnum):
    inline = "inline"
    attachment = "attachment"


@dataclass(frozen=True, slots=True)
class InboxMediaContentProjection:
    asset_id: UUID
    file_name: str
    content_type: str
    content_length: int | None
    presentation: InboxMediaBrowserPresentation
    chunks: Iterator[bytes]


def get_media_content_projection(
    db: Session,
    *,
    asset_id: UUID,
) -> InboxMediaContentProjection:
    media_content = team_inbox_media.stream_asset_content(db, asset_id)
    if isinstance(media_content, tuple):
        asset, stream = media_content
        content_type = (
            stream.content_type or asset.mime_type or "application/octet-stream"
        )
        file_name = asset.file_name or f"inbox-media-{asset.id}"
        content_length = stream.content_length
        chunks = stream.chunks
        resolved_asset_id = asset.id
    else:
        content_type = media_content.content_type
        file_name = media_content.file_name
        content_length = media_content.stream.content_length
        chunks = media_content.stream.chunks
        resolved_asset_id = media_content.asset_id
    inline_types = (
        SAFE_INLINE_IMAGE_CONTENT_TYPES
        | SAFE_INLINE_AUDIO_CONTENT_TYPES
        | SAFE_INLINE_VIDEO_CONTENT_TYPES
    )
    presentation = (
        InboxMediaBrowserPresentation.inline
        if content_type in inline_types
        else InboxMediaBrowserPresentation.attachment
    )
    return InboxMediaContentProjection(
        asset_id=resolved_asset_id,
        file_name=file_name,
        content_type=content_type,
        content_length=content_length,
        presentation=presentation,
        chunks=chunks,
    )


class InboxListSort(StrEnum):
    priority = "priority"
    last_message_at = "last_message_at"
    created_at = "created_at"


class InboxSortDirection(StrEnum):
    ascending = "asc"
    descending = "desc"


class InboxQueueComposition(StrEnum):
    full_workspace = "full_workspace"
    sidebar = "sidebar"


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
        ListFieldDefinition("filters", "Advanced filters", filterable=True),
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
    default_sort=InboxListSort.last_message_at.value,
    default_sort_dir=InboxSortDirection.descending.value,
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
    advanced_filters: team_inbox_filters.InboxAdvancedFilterPayload = (
        team_inbox_filters.InboxAdvancedFilterPayload()
    )
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
    composition: InboxQueueComposition = InboxQueueComposition.full_workspace
    include_total_count: bool = True


@dataclass(frozen=True, slots=True)
class ContactLinkCandidate:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class ContactLinkCandidateSet:
    subscribers: tuple[ContactLinkCandidate, ...]
    resellers: tuple[ContactLinkCandidate, ...]
    organizations: tuple[ContactLinkCandidate, ...]


@dataclass(frozen=True, slots=True)
class WhatsAppContactOption:
    id: str
    name: str
    whatsapp_address: str
    party_id: UUID | None
    subscriber_id: UUID | None


@dataclass(frozen=True, slots=True)
class InboxActionEligibility:
    can_reply: bool
    can_resolve: bool
    can_reopen: bool
    can_link_contact: bool
    can_mark_read: bool
    can_issue_lead_form: bool
    lead_form_reason: str
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

SOCIAL_COMMENT_CHANNELS = (
    InboxChannelType.facebook_comment.value,
    InboxChannelType.instagram_comment.value,
)

SOCIAL_COMMENT_LIST_DEFINITION = ListDefinition(
    key="team_inbox_social_comments",
    fields=(
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("channel_type", "Channel", filterable=True),
        ListFieldDefinition("unread", "Unread", filterable=True),
        ListFieldDefinition("last_message_at", "Last activity", sortable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort=InboxListSort.last_message_at.value,
    default_sort_dir=InboxSortDirection.descending.value,
    per_page_options=(10, 25, 50, 100),
    default_per_page=25,
)


@dataclass(frozen=True, slots=True)
class InboxLifecycleEvent:
    kind: str
    label: str
    actor_name: str
    actor_email: str | None
    occurred_at: datetime | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class InboxConversationProjection:
    timeline: team_inbox_read.InboxConversationTimeline
    subscriber_summary: Mapping[str, object] | None
    contact_link_candidates: ContactLinkCandidateSet
    label_options: tuple[team_inbox_operations.LabelOption, ...]
    conversation_labels: tuple[team_inbox_operations.LabelOption, ...]
    macro_options: tuple[team_inbox_operations.MacroOption, ...]
    template_options: tuple[team_inbox_operations.MessageTemplateOption, ...]
    catalogue_options: tuple[plan_family_catalogues.PlanFamilyCatalogueOption, ...]
    action_eligibility: InboxActionEligibility
    is_unread: bool
    priority_options: tuple[InboxPriorityOption, ...]
    activity_events: tuple[InboxLifecycleEvent, ...]


@dataclass(frozen=True, slots=True)
class InboxServiceTeamOption:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class InboxAgentOption:
    id: UUID
    name: str
    initials: str
    presence_status: str


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
class InboxAgentPresenceProjection:
    person_id: UUID
    status: str
    last_seen_at: datetime | None


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
    social_comment_count: int
    operator_unread_count: int
    count: int
    list_query: ListQuery
    page_meta: PageMeta
    status: str
    channel_type: str
    service_team_id: str
    advanced_filters_json: str | None
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
    agent_presence: InboxAgentPresenceProjection | None
    assignment_counts: InboxAssignmentCounts
    status_options: tuple[str, ...]
    channel_options: tuple[str, ...]
    priority_options: tuple[InboxPriorityOption, ...]
    label_options: tuple[team_inbox_operations.LabelOption, ...]
    saved_filters: tuple[team_inbox_operations.SavedFilterOption, ...]
    selected_id: str | None
    selected: InboxConversationProjection | None
    canonical_url: str | None


@dataclass(frozen=True, slots=True)
class SocialCommentWorkspaceProjection:
    rows: tuple[team_inbox_read.InboxConversationListRow, ...]
    post_rows: tuple[SocialCommentPostRow, ...]
    selected: InboxConversationProjection | None
    selected_post: SocialCommentSelectedPostProjection | None
    selected_id: str | None
    count: int
    list_query: ListQuery
    page_meta: PageMeta
    search: str
    status: str
    channel_type: str
    unread: bool
    status_options: tuple[str, ...]
    channel_options: tuple[str, ...]
    canonical_url: str | None


@dataclass(frozen=True, slots=True)
class SocialCommentReplyContext:
    message_id: str
    channel_type: str
    provider_account_id: str | None
    provider_post_id: str | None
    provider_media_id: str | None
    provider_comment_id: str | None
    parent_provider_comment_id: str | None
    root_provider_comment_id: str | None
    conversation_id: str


@dataclass(frozen=True, slots=True)
class SocialCommentPostRow:
    row: team_inbox_read.InboxConversationListRow
    platform: str
    thumbnail_url: str | None
    media_type: str | None
    latest_activity_summary: str


@dataclass(frozen=True, slots=True)
class SocialCommentNode:
    message: team_inbox_read.InboxTimelineMessage
    provider_comment_id: str | None
    parent_provider_comment_id: str | None
    root_provider_comment_id: str | None
    author_name: str
    author_avatar_url: str | None
    platform: str
    is_dotmac_reply: bool
    can_target_reply: bool
    reply_context: SocialCommentReplyContext | None
    replies: tuple[SocialCommentNode, ...]


@dataclass(frozen=True, slots=True)
class SocialPostMediaItem:
    id: str | None
    url: str | None
    media_type: str
    caption: str | None
    provider: str | None
    provider_media_id: str | None
    content_available: bool
    download_status: str | None
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class SocialCommentPostHeader:
    platform: str
    account_id: str | None
    post_id: str | None
    media_id: str | None
    caption: str
    published_at: datetime | None
    comment_count: int
    status: str
    permalink_url: str | None


@dataclass(frozen=True, slots=True)
class SocialCommentSelectedPostProjection:
    timeline: team_inbox_read.InboxConversationTimeline
    header: SocialCommentPostHeader
    comments: tuple[SocialCommentNode, ...]
    media_items: tuple[SocialPostMediaItem, ...]
    top_level_comment_supported: bool
    top_level_comment_unavailable_reason: str
    private_message_supported: bool
    private_message_unavailable_reason: str


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
    user_ids = [row.id for row in rows]
    presence_rows = (
        db.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id.in_(user_ids))
        .all()
        if user_ids
        else []
    )
    presence_by_person = {row.person_id: row for row in presence_rows}
    return tuple(
        InboxAgentOption(
            id=row.id,
            name=(
                row.display_name
                or f"{row.first_name} {row.last_name}".strip()
                or row.email
            ),
            initials=_initials(row.first_name, row.last_name, row.display_name),
            presence_status=(
                (
                    presence.manual_override_status
                    or presence.status
                    or InboxAgentPresenceStatus.offline.value
                )
                if (presence := presence_by_person.get(row.id)) is not None
                else InboxAgentPresenceStatus.offline.value
            ),
        )
        for row in rows
    )


def list_service_team_options(db: Session) -> tuple[InboxServiceTeamOption, ...]:
    """Return the active service-team selector owned by service-team lifecycle."""

    return tuple(
        InboxServiceTeamOption(id=team_id, name=name)
        for team_id, name in service_team_lifecycle.list_active_team_options(db)
    )


def list_actor_service_team_options(
    db: Session,
    actor_person_id: UUID | None,
) -> tuple[InboxServiceTeamOption, ...]:
    """Return active teams the current staff principal may claim work into."""

    if actor_person_id is None:
        return ()
    resolution = service_team_lifecycle.resolve_staff_service_teams(db, actor_person_id)
    if resolution.kind is not service_team_lifecycle.ServiceTeamResolutionKind.resolved:
        return ()
    team_ids = set(resolution.team_ids)
    return tuple(
        option for option in list_service_team_options(db) if option.id in team_ids
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


def get_agent_presence(
    db: Session,
    person_id: UUID | None,
) -> InboxAgentPresenceProjection | None:
    if person_id is None:
        return None
    presence = (
        db.query(InboxAgentPresence)
        .filter(InboxAgentPresence.person_id == person_id)
        .one_or_none()
    )
    status = (
        presence.manual_override_status or presence.status
        if presence is not None
        else InboxAgentPresenceStatus.offline.value
    )
    return InboxAgentPresenceProjection(
        person_id=person_id,
        status=status,
        last_seen_at=presence.last_seen_at if presence is not None else None,
    )


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
    all_count = team_inbox_read.queue_conversation_count(db)
    assigned_to_me = team_inbox_read.assigned_conversation_count(
        db,
        assigned_person_id=actor_person_id,
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
        needs_attention=team_inbox_read.needs_attention_conversation_count(db),
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
        organizations=tuple(
            ContactLinkCandidate(id=str(item["id"]), label=str(item["label"]))
            for item in values.get("organizations", [])
        ),
    )


def _actor_label(
    user: SystemUser | None, fallback: str = "System"
) -> tuple[str, str | None]:
    if user is None:
        return fallback, None
    name = (
        getattr(user, "display_name", None)
        or getattr(user, "full_name", None)
        or getattr(user, "email", None)
        or "System"
    ).strip()
    return name or fallback, getattr(user, "email", None)


def _conversation_activity(
    db: Session,
    conversation_id: UUID,
    *,
    limit: int = 8,
) -> tuple[InboxLifecycleEvent, ...]:
    events: list[InboxLifecycleEvent] = []

    status_rows = (
        db.query(InboxStatusTransitionEvent, SystemUser)
        .outerjoin(
            SystemUser, SystemUser.id == InboxStatusTransitionEvent.actor_person_id
        )
        .filter(InboxStatusTransitionEvent.conversation_id == conversation_id)
        .order_by(InboxStatusTransitionEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )
    for event, actor in status_rows:
        actor_name, actor_email = _actor_label(actor)
        status = getattr(event.status, "value", None) or str(event.status or "")
        label = (
            "Resolved"
            if status == InboxConversationStatus.resolved.value
            else "Reopened"
            if status == InboxConversationStatus.open.value
            else f"Status changed to {status or 'unknown'}"
        )
        events.append(
            InboxLifecycleEvent(
                kind="status",
                label=label,
                actor_name=actor_name,
                actor_email=actor_email,
                occurred_at=event.occurred_at,
                detail=event.reason_code,
            )
        )

    routing_actor = aliased(SystemUser)
    routing_assignee = aliased(SystemUser)
    routing_rows = (
        db.query(InboxRoutingEvent, routing_actor, routing_assignee)
        .outerjoin(routing_actor, routing_actor.id == InboxRoutingEvent.actor_person_id)
        .outerjoin(
            routing_assignee,
            routing_assignee.id == InboxRoutingEvent.person_id,
        )
        .filter(InboxRoutingEvent.conversation_id == conversation_id)
        .order_by(InboxRoutingEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )
    for event, actor, assignee in routing_rows:
        actor_name, actor_email = _actor_label(actor)
        assignee_name, _ = _actor_label(assignee, fallback="Unassigned")
        if event.person_id:
            label = f"Assigned to {assignee_name}"
        else:
            label = "Assignment changed"
        events.append(
            InboxLifecycleEvent(
                kind="assignment",
                label=label,
                actor_name=actor_name,
                actor_email=actor_email,
                occurred_at=event.occurred_at,
                detail=event.reason_code,
            )
        )

    read_rows = (
        db.query(InboxConversationReadState, SystemUser)
        .join(SystemUser, SystemUser.id == InboxConversationReadState.person_id)
        .filter(
            InboxConversationReadState.conversation_id == conversation_id,
            InboxConversationReadState.last_read_at.isnot(None),
        )
        .order_by(InboxConversationReadState.last_read_at.desc())
        .limit(limit)
        .all()
    )
    for read_state, actor in read_rows:
        actor_name, actor_email = _actor_label(actor)
        events.append(
            InboxLifecycleEvent(
                kind="read",
                label="Opened",
                actor_name=actor_name,
                actor_email=actor_email,
                occurred_at=read_state.last_read_at,
            )
        )

    events.sort(
        key=lambda item: item.occurred_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return tuple(events[:limit])


def get_conversation_projection(
    db: Session,
    *,
    conversation_id: UUID,
    actor_person_id: UUID | None,
    include_contact_candidates: bool = True,
    include_catalogue_options: bool = True,
    include_label_usage_counts: bool = False,
) -> InboxConversationProjection | None:
    timeline = team_inbox_read.get_conversation_timeline(db, conversation_id)
    if timeline is None:
        return None
    is_resolved = timeline.status == InboxConversationStatus.resolved.value
    outbound_unsupported = timeline.channel_type == InboxChannelType.website_fiber.value
    summary = subscriber_summary.subscriber_summary(db, timeline.subscriber_id)
    lead_eligibility = lead_intake.manual_invitation_eligibility(
        db,
        conversation_id,
        verify_customer_identity=False,
    )
    return InboxConversationProjection(
        timeline=timeline,
        subscriber_summary=summary,
        contact_link_candidates=(
            contact_link_candidates(db, timeline)
            if include_contact_candidates
            else ContactLinkCandidateSet(subscribers=(), resellers=(), organizations=())
        ),
        label_options=tuple(
            team_inbox_operations.list_labels(
                db, include_usage_counts=include_label_usage_counts
            )
        ),
        conversation_labels=tuple(
            team_inbox_operations.conversation_labels(db, conversation_id)
        ),
        macro_options=tuple(
            team_inbox_operations.list_macros(db, person_id=actor_person_id)
        ),
        template_options=tuple(
            team_inbox_operations.list_templates(db, channel_type=timeline.channel_type)
        ),
        catalogue_options=(
            plan_family_catalogues.list_catalogue_options(db)
            if include_catalogue_options
            else ()
        ),
        action_eligibility=InboxActionEligibility(
            can_reply=not is_resolved and not outbound_unsupported,
            can_resolve=not is_resolved,
            can_reopen=is_resolved,
            can_link_contact=bool(timeline.contact_address),
            can_mark_read=actor_person_id is not None,
            can_issue_lead_form=lead_eligibility.eligible,
            lead_form_reason=lead_eligibility.reason,
            reason=(
                "Resolved conversations must be reopened before replying."
                if is_resolved
                else (
                    "Outbound replies for fiber website inquiries are not configured yet."
                    if outbound_unsupported
                    else None
                )
            ),
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
        activity_events=_conversation_activity(db, conversation_id),
    )


def list_whatsapp_contacts(
    db: Session,
    *,
    search: str,
    limit: int = 20,
) -> tuple[WhatsAppContactOption, ...]:
    """Search canonical contacts plus unbound active legacy subscribers.

    Canonical Party reachability always wins. The legacy branch is a bounded
    compatibility reader for accounts awaiting the reviewed Party backfill;
    it never creates identity or guesses between subscribers sharing a number.
    """

    from app.models.party import Party, PartyContactPoint, PartyIdentityStatus
    from app.services.customer_identity_normalization import normalize_phone_identifier

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
    requested_limit = max(1, min(int(limit), 20))
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
        normalized_address = normalize_phone_identifier(address)
        if normalized_address is None:
            continue
        options.append(
            WhatsAppContactOption(
                id=str(party.id),
                name=party.display_name,
                whatsapp_address=normalized_address,
                party_id=party.id,
                subscriber_id=None,
            )
        )
    address_counts = Counter(option.whatsapp_address for option in options)
    canonical_addresses = set(address_counts)
    options = [
        option for option in options if address_counts[option.whatsapp_address] == 1
    ]

    phone_term = normalize_phone_identifier(term)
    phone_variants: set[str] = {re.sub(r"\D", "", term)}
    if phone_term is not None:
        digits = phone_term.removeprefix("+")
        phone_variants.add(digits)
        if digits.startswith("234") and len(digits) > 3:
            phone_variants.add(f"0{digits[3:]}")
    legacy_filters = [
        Subscriber.display_name.ilike(like),
        Subscriber.first_name.ilike(like),
        Subscriber.last_name.ilike(like),
    ]
    normalized_legacy_phone = func.replace(
        func.replace(
            func.replace(
                func.replace(func.replace(Subscriber.phone, "+", ""), " ", ""),
                "-",
                "",
            ),
            "(",
            "",
        ),
        ")",
        "",
    )
    legacy_filters.extend(
        normalized_legacy_phone.ilike(f"%{value}%")
        for value in sorted(phone_variants)
        if value
    )
    legacy_rows = (
        db.query(Subscriber)
        .filter(Subscriber.party_id.is_(None))
        .filter(Subscriber.is_active.is_(True))
        .filter(Subscriber.status == SubscriberStatus.active)
        .filter(or_(*legacy_filters))
        .order_by(func.lower(Subscriber.display_name).asc(), Subscriber.id.asc())
        .limit(requested_limit * 4)
        .all()
    )
    legacy_by_address: dict[str, list[Subscriber]] = {}
    for subscriber in legacy_rows:
        normalized_address = normalize_phone_identifier(subscriber.phone)
        if normalized_address is None or normalized_address in canonical_addresses:
            continue
        legacy_by_address.setdefault(normalized_address, []).append(subscriber)
    for address, subscribers in legacy_by_address.items():
        if len(subscribers) != 1:
            continue
        subscriber = subscribers[0]
        name = str(subscriber.display_name or "").strip() or " ".join(
            part
            for part in (subscriber.first_name.strip(), subscriber.last_name.strip())
            if part
        )
        options.append(
            WhatsAppContactOption(
                id=f"subscriber:{subscriber.id}",
                name=name,
                whatsapp_address=address,
                party_id=None,
                subscriber_id=subscriber.id,
            )
        )
    return tuple(options[:requested_limit])


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
        "contact_display_name": timeline.contact_name,
        "assigned_agent_name": assigned_agent_name,
        "tags": [label.name for label in labels],
        "linked_ticket": (
            {
                "number": ticket.number,
                "title": ticket.title,
                "status": canonical_ticket_status_value(ticket.status),
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
    advanced_filters_json: str | None,
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
        "filters": advanced_filters_json,
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


def social_comment_thread_count(db: Session) -> int:
    """Count public social comment threads owned by the Team Inbox read model."""

    return int(
        db.query(func.count(InboxConversation.id))
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.channel_type.in_(SOCIAL_COMMENT_CHANNELS))
        .scalar()
        or 0
    )


def _metadata_text(metadata: Mapping[str, object] | None, *keys: str) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in keys:
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return None


def _social_platform(channel_type: str) -> str:
    if channel_type == InboxChannelType.instagram_comment.value:
        return "Instagram"
    return "Facebook"


def _social_post_identifier(
    timeline: team_inbox_read.InboxConversationTimeline,
    *keys: str,
) -> str | None:
    for message in timeline.messages:
        value = _metadata_text(message.metadata, *keys)
        if value:
            return value
    return _metadata_text(timeline.metadata, *keys)


def _provider_comment_id(
    message: team_inbox_read.InboxTimelineMessage,
) -> str | None:
    return _metadata_text(
        message.metadata,
        "provider_comment_id",
        "comment_id",
        "provider_message_id",
    )


def _comment_author_name(
    message: team_inbox_read.InboxTimelineMessage,
    *,
    fallback: str,
) -> str:
    if message.direction == "outbound":
        if message.sender is not None and message.sender.display_name:
            return message.sender.display_name
        return "Dotmac"
    return (
        _metadata_text(message.metadata, "commenter_name", "commenter_username")
        or message.from_address
        or fallback
    )


def _comment_avatar_url(
    message: team_inbox_read.InboxTimelineMessage,
) -> str | None:
    metadata = message.metadata or {}
    profile = metadata.get("contact_profile")
    if isinstance(profile, Mapping):
        return _metadata_text(profile, "profile_pic", "avatar_url")
    return _metadata_text(metadata, "profile_pic", "avatar_url")


def _comment_time(message: team_inbox_read.InboxTimelineMessage) -> datetime:
    return message.received_at or message.sent_at or message.created_at


def _media_type(
    attachment: team_inbox_read.InboxTimelineAttachment,
) -> str:
    raw_type = str(attachment.type or "").strip().lower()
    mime_type = str(attachment.mime_type or "").strip().lower()
    if raw_type in {"video", "reel"} or mime_type.startswith("video/"):
        return "video"
    if raw_type in {"carousel", "album"}:
        return "carousel"
    if raw_type == "image" or mime_type.startswith("image/"):
        return "image"
    return raw_type or "media"


def _social_media_items(
    timeline: team_inbox_read.InboxConversationTimeline,
) -> tuple[SocialPostMediaItem, ...]:
    items: list[SocialPostMediaItem] = []
    seen: set[tuple[str | None, str | None]] = set()
    for message in timeline.messages:
        for attachment in message.attachments:
            key = (attachment.id, attachment.url or attachment.provider_media_id)
            if key in seen:
                continue
            seen.add(key)
            content_available = bool(attachment.content_available and attachment.url)
            items.append(
                SocialPostMediaItem(
                    id=attachment.id,
                    url=attachment.url if content_available else None,
                    media_type=_media_type(attachment),
                    caption=attachment.caption,
                    provider=attachment.provider,
                    provider_media_id=attachment.provider_media_id,
                    content_available=content_available,
                    download_status=attachment.download_status,
                    unavailable_reason=(
                        None
                        if content_available
                        else attachment.download_error
                        or "Post media is not available from the synced provider data."
                    ),
                )
            )
    return tuple(items)


def _reply_context(
    timeline: team_inbox_read.InboxConversationTimeline,
    message: team_inbox_read.InboxTimelineMessage,
    *,
    provider_comment_id: str | None,
    parent_provider_comment_id: str | None,
    root_provider_comment_id: str | None,
) -> SocialCommentReplyContext | None:
    if not provider_comment_id or message.direction != "inbound":
        return None
    return SocialCommentReplyContext(
        message_id=message.id,
        channel_type=timeline.channel_type,
        provider_account_id=_social_post_identifier(
            timeline,
            "provider_account_id",
            "provider_account_scope",
            "page_id",
            "instagram_account_id",
            "ig_account_id",
        ),
        provider_post_id=_social_post_identifier(timeline, "post_id"),
        provider_media_id=_social_post_identifier(timeline, "media_id"),
        provider_comment_id=provider_comment_id,
        parent_provider_comment_id=parent_provider_comment_id,
        root_provider_comment_id=root_provider_comment_id,
        conversation_id=timeline.id,
    )


def _social_comment_nodes(
    timeline: team_inbox_read.InboxConversationTimeline,
) -> tuple[SocialCommentNode, ...]:
    post_id = _social_post_identifier(timeline, "post_id", "media_id")
    node_data: dict[str, tuple[team_inbox_read.InboxTimelineMessage, str | None]] = {}
    children_by_parent: dict[str, list[str]] = {}
    ordered_ids: list[str] = []
    synthetic_index = 0
    for message in timeline.messages:
        provider_comment_id = _provider_comment_id(message)
        node_id = provider_comment_id or f"local:{synthetic_index}:{message.id}"
        synthetic_index += 1
        parent_provider_comment_id = _metadata_text(
            message.metadata,
            "parent_provider_comment_id",
            "parent_comment_id",
            "comment_parent_id",
        )
        if parent_provider_comment_id == post_id:
            parent_provider_comment_id = None
        node_data[node_id] = (message, parent_provider_comment_id)
        ordered_ids.append(node_id)
        if parent_provider_comment_id:
            children_by_parent.setdefault(parent_provider_comment_id, []).append(
                node_id
            )

    def root_for(node_id: str) -> str | None:
        current_id = node_id
        seen: set[str] = set()
        while current_id not in seen:
            seen.add(current_id)
            _message, parent_id = node_data[current_id]
            if not parent_id or parent_id not in node_data:
                return current_id if not current_id.startswith("local:") else None
            current_id = parent_id
        return None

    def build(node_id: str) -> SocialCommentNode:
        message, parent_provider_comment_id = node_data[node_id]
        provider_comment_id = (
            node_id
            if not node_id.startswith("local:")
            else _provider_comment_id(message)
        )
        root_id = root_for(node_id)
        root_provider_comment_id = (
            root_id
            if root_id and not root_id.startswith("local:")
            else provider_comment_id
        )
        reply_context = _reply_context(
            timeline,
            message,
            provider_comment_id=provider_comment_id,
            parent_provider_comment_id=parent_provider_comment_id,
            root_provider_comment_id=root_provider_comment_id,
        )
        return SocialCommentNode(
            message=message,
            provider_comment_id=provider_comment_id,
            parent_provider_comment_id=parent_provider_comment_id,
            root_provider_comment_id=root_provider_comment_id,
            author_name=_comment_author_name(message, fallback=timeline.contact_name),
            author_avatar_url=_comment_avatar_url(message),
            platform=_social_platform(timeline.channel_type),
            is_dotmac_reply=message.direction == "outbound"
            or _metadata_text(message.metadata, "message_kind")
            == "social_comment_reply",
            can_target_reply=reply_context is not None,
            reply_context=reply_context,
            replies=tuple(
                build(child_id)
                for child_id in sorted(
                    children_by_parent.get(provider_comment_id or "", []),
                    key=lambda child_id: _comment_time(node_data[child_id][0]),
                )
            ),
        )

    roots = [
        node_id
        for node_id in ordered_ids
        if not node_data[node_id][1] or node_data[node_id][1] not in node_data
    ]
    return tuple(
        build(node_id)
        for node_id in sorted(roots, key=lambda item: _comment_time(node_data[item][0]))
    )


def _social_selected_post_projection(
    selected: InboxConversationProjection,
) -> SocialCommentSelectedPostProjection:
    timeline = selected.timeline
    post_id = _social_post_identifier(timeline, "post_id")
    media_id = _social_post_identifier(timeline, "media_id")
    caption = (
        timeline.subject
        or _metadata_text(timeline.metadata, "caption", "post_caption")
        or f"{_social_platform(timeline.channel_type)} post"
    )
    return SocialCommentSelectedPostProjection(
        timeline=timeline,
        header=SocialCommentPostHeader(
            platform=_social_platform(timeline.channel_type),
            account_id=_social_post_identifier(
                timeline,
                "page_id",
                "instagram_account_id",
                "provider_account_id",
                "provider_account_scope",
            ),
            post_id=post_id,
            media_id=media_id,
            caption=caption,
            published_at=timeline.first_message_at,
            comment_count=len(timeline.messages),
            status=timeline.status,
            permalink_url=_social_post_identifier(
                timeline, "permalink_url", "permalink"
            ),
        ),
        comments=_social_comment_nodes(timeline),
        media_items=_social_media_items(timeline),
        top_level_comment_supported=False,
        top_level_comment_unavailable_reason=(
            "Top-level public post comments are not wired through the Team Inbox "
            "command owner yet. Use targeted replies on synced comments."
        ),
        private_message_supported=False,
        private_message_unavailable_reason=(
            "Private messaging commenters requires a separate Meta DM capability and "
            "safe provider identity mapping; this workspace only supports public replies."
        ),
    )


def _social_post_rows(
    db: Session,
    rows: tuple[team_inbox_read.InboxConversationListRow, ...],
) -> tuple[SocialCommentPostRow, ...]:
    post_rows: list[SocialCommentPostRow] = []
    for row in rows:
        timeline = team_inbox_read.get_conversation_timeline(db, row.id)
        media_items = _social_media_items(timeline) if timeline is not None else ()
        thumbnail = next((item for item in media_items if item.url), None)
        latest_author = ""
        if timeline is not None and timeline.messages:
            latest = max(timeline.messages, key=_comment_time)
            latest_author = _comment_author_name(latest, fallback=row.contact_name)
        post_rows.append(
            SocialCommentPostRow(
                row=row,
                platform=_social_platform(row.channel_type),
                thumbnail_url=thumbnail.url if thumbnail is not None else None,
                media_type=thumbnail.media_type if thumbnail is not None else None,
                latest_activity_summary=(
                    f"{latest_author} commented"
                    if latest_author
                    else row.latest_message_body or "No comment preview"
                ),
            )
        )
    return tuple(post_rows)


def build_social_comments_projection(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    channel_type: str | None = None,
    unread: bool = False,
    selected_conversation_id: str | UUID | None = None,
    actor_person_id: UUID | None = None,
    page: int = 1,
    per_page: int = 25,
) -> SocialCommentWorkspaceProjection:
    """Own the public post-comment workspace list, selection, and filters."""

    clean_status = (
        status if status in {item.value for item in InboxConversationStatus} else None
    )
    clean_channel = channel_type if channel_type in SOCIAL_COMMENT_CHANNELS else None
    clean_unread = bool(unread)
    safe_per_page = (
        per_page
        if per_page in SOCIAL_COMMENT_LIST_DEFINITION.per_page_options
        else SOCIAL_COMMENT_LIST_DEFINITION.default_per_page
    )
    normalized_filters = {
        "status": clean_status,
        "channel_type": clean_channel,
        "unread": "true" if clean_unread else None,
    }
    requested_query = SOCIAL_COMMENT_LIST_DEFINITION.build_query(
        search=search,
        filters=normalized_filters,
        sort_by=InboxListSort.last_message_at.value,
        sort_dir=InboxSortDirection.descending.value,
        page=max(1, page),
        per_page=safe_per_page,
    )

    def fetch(query: ListQuery) -> team_inbox_read.InboxConversationListResult:
        return team_inbox_read.list_conversations(
            db,
            search=query.search,
            status=clean_status,
            channel_type=clean_channel,
            channel_types=SOCIAL_COMMENT_CHANNELS if clean_channel is None else None,
            operator_person_id=actor_person_id,
            unread_only=clean_unread,
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

    selected_id = _uuid(selected_conversation_id)
    row_ids = {row.id for row in result.items}
    if selected_id is None and result.items:
        selected_id = _uuid(result.items[0].id)
    if selected_id is not None and str(selected_id) not in row_ids:
        selected_id = None
    selected = (
        get_conversation_projection(
            db,
            conversation_id=selected_id,
            actor_person_id=actor_person_id,
        )
        if selected_id is not None
        else None
    )
    if (
        selected is not None
        and selected.timeline.channel_type not in SOCIAL_COMMENT_CHANNELS
    ):
        selected = None
        selected_id = None
    selected_post = (
        _social_selected_post_projection(selected) if selected is not None else None
    )

    canonical_url = None
    if request_needs_canonicalization(
        list_query,
        search=search,
        filters={
            "status": status,
            "channel_type": channel_type,
            "unread": "true" if unread else None,
        },
        page=page,
        per_page=per_page,
    ):
        canonical_url = list_query.url("/admin/inbox/comments")
        if selected_id is not None:
            canonical_url = f"{canonical_url}&conversation_id={selected_id}"

    rows = tuple(result.items)
    return SocialCommentWorkspaceProjection(
        rows=rows,
        post_rows=_social_post_rows(db, rows),
        selected=selected,
        selected_post=selected_post,
        selected_id=str(selected_id) if selected_id is not None else None,
        count=result.count,
        list_query=list_query,
        page_meta=page_meta,
        search=list_query.search or "",
        status=clean_status or "",
        channel_type=clean_channel or "",
        unread=clean_unread,
        status_options=tuple(item.value for item in InboxConversationStatus),
        channel_options=SOCIAL_COMMENT_CHANNELS,
        canonical_url=canonical_url,
    )


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
    advanced_filter_query, active_team_options = (
        team_inbox_filters.resolve_filter_query(db, request.advanced_filters)
    )
    advanced_filters_json = advanced_filter_query.canonical_json()

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
        advanced_filters_json=advanced_filters_json,
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
            advanced_filters=advanced_filter_query,
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
            include_total_count=request.include_total_count,
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
            advanced_filters_json=request.advanced_filters.raw_json,
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
            include_contact_candidates=False,
        )
        if (
            selected_id is not None
            and request.composition is InboxQueueComposition.full_workspace
        )
        else None
    )
    queue_metrics = team_inbox_operations.queue_metrics(db)
    return InboxQueueProjection(
        rows=tuple(result.items),
        queue_metrics=queue_metrics,
        social_comment_count=social_comment_thread_count(db),
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
        advanced_filters_json=advanced_filters_json,
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
            InboxServiceTeamOption(id=team_id, name=name)
            for team_id, name in active_team_options
        ),
        agent_options=list_agent_options(db),
        agent_presence=get_agent_presence(db, request.actor_person_id),
        assignment_counts=_assignment_counts(
            db,
            actor_person_id=request.actor_person_id,
            queue_metrics=queue_metrics,
        ),
        status_options=tuple(item.value for item in InboxConversationStatus),
        channel_options=tuple(
            item.value
            for item in InboxChannelType
            if item.value not in SOCIAL_COMMENT_CHANNELS
        ),
        priority_options=INBOX_PRIORITY_OPTIONS,
        label_options=tuple(team_inbox_operations.list_labels(db)),
        saved_filters=tuple(
            team_inbox_operations.list_saved_filters(
                db, person_id=request.actor_person_id
            )
        ),
        selected_id=str(selected_id) if selected_id is not None else None,
        selected=selected,
        canonical_url=canonical_url,
    )
