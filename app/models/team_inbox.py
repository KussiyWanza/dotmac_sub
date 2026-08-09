import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InboxChannelType(enum.Enum):
    email = "email"
    whatsapp = "whatsapp"
    website_fiber = "website_fiber"
    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"
    facebook_comment = "facebook_comment"
    instagram_comment = "instagram_comment"
    chat_widget = "chat_widget"
    note = "note"
    # A customer talking to the technician on their way, in the portal. It has
    # no external transport: delivery is the shared conversation websocket.
    field_job = "field_job"


class InboxConversationStatus(enum.Enum):
    open = "open"
    pending = "pending"
    snoozed = "snoozed"
    resolved = "resolved"


class InboxMessageDirection(enum.Enum):
    inbound = "inbound"
    outbound = "outbound"
    internal = "internal"


class InboxAgentPresenceStatus(enum.Enum):
    online = "online"
    away = "away"
    on_break = "on_break"
    offline = "offline"


class InboxTeamRole(enum.Enum):
    owner = "owner"
    participant = "participant"
    watcher = "watcher"


class InboxTeamSource(enum.Enum):
    recipient_to = "recipient_to"
    recipient_cc = "recipient_cc"
    routing_rule = "routing_rule"
    escalation = "escalation"
    manual = "manual"


class InboxRoutingEventType(enum.Enum):
    assigned = "assigned"
    reassigned = "reassigned"
    queued = "queued"
    unassigned = "unassigned"
    escalated = "escalated"
    auto_assignment_declined = "auto_assignment_declined"


class InboxAuditEvidenceGrade(enum.Enum):
    native = "native"
    authoritative_historical = "authoritative_historical"
    strongly_inferred = "strongly_inferred"
    weakly_inferred = "weakly_inferred"
    unknown = "unknown"


class InboxAuditSource(enum.Enum):
    routing_command = "routing_command"
    status_command = "status_command"
    presence_command = "presence_command"
    historical_backfill = "historical_backfill"


class InboxRoutingDecisionMode(enum.Enum):
    manual = "manual"
    automatic = "automatic"
    system = "system"


class InboxQueueEntryStatus(enum.Enum):
    queued = "queued"
    promoted = "promoted"
    cancelled = "cancelled"


class InboxAutomationTrigger(enum.Enum):
    conversation_created = "conversation_created"
    inbound_message_received = "inbound_message_received"


class InboxAutomationActionType(enum.Enum):
    assign_agent = "assign_agent"
    auto_assign = "auto_assign"
    add_tag = "add_tag"


class InboxObservationKind(enum.Enum):
    message = "message"
    delivery_receipt = "delivery_receipt"


class InboxObservationStatus(enum.Enum):
    recorded = "recorded"
    processed = "processed"
    rejected = "rejected"


class TeamInboxEmailRoute(Base):
    __tablename__ = "team_inbox_email_routes"
    __table_args__ = (
        UniqueConstraint(
            "service_team_id",
            "email_address",
            name="uq_team_inbox_email_routes_team_address",
        ),
        Index(
            "ix_team_inbox_email_routes_address_active", "email_address", "is_active"
        ),
        Index("ix_team_inbox_email_routes_team", "service_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    email_address: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    service_team = relationship("ServiceTeam")


class TeamInboxChannelRoute(Base):
    __tablename__ = "team_inbox_channel_routes"
    __table_args__ = (
        UniqueConstraint(
            "channel_type",
            "provider",
            "account_scope",
            name="uq_team_inbox_channel_routes_identity",
        ),
        Index(
            "ix_team_inbox_channel_routes_channel_active",
            "channel_type",
            "is_active",
        ),
        Index("ix_team_inbox_channel_routes_team", "service_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))
    allow_ai_routing: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    service_team = relationship("ServiceTeam")


class TeamInboxAiRoute(Base):
    __tablename__ = "team_inbox_ai_routes"
    __table_args__ = (
        UniqueConstraint(
            "channel_type",
            "intent_key",
            name="uq_team_inbox_ai_routes_channel_intent",
        ),
        Index("ix_team_inbox_ai_routes_active", "is_active", "priority"),
        Index("ix_team_inbox_ai_routes_team", "service_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    intent_key: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(160))
    confidence_threshold: Mapped[float] = mapped_column(
        Float, default=0.75, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    service_team = relationship("ServiceTeam")


class InboxAutomationRule(Base):
    __tablename__ = "inbox_automation_rules"
    __table_args__ = (
        Index("ix_inbox_automation_trigger_active", "trigger", "is_active"),
        Index("ix_inbox_automation_sort", "sort_order", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    trigger: Mapped[InboxAutomationTrigger] = mapped_column(
        Enum(InboxAutomationTrigger), nullable=False
    )
    conditions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    action_type: Mapped[InboxAutomationActionType] = mapped_column(
        Enum(InboxAutomationActionType), nullable=False
    )
    action_value: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class InboxConversation(Base):
    __tablename__ = "inbox_conversations"
    __table_args__ = (
        Index("ix_inbox_conversations_subscriber", "subscriber_id"),
        Index("ix_inbox_conversations_primary_team", "primary_service_team_id"),
        Index("ix_inbox_conversations_status_last", "status", "last_message_at"),
        Index(
            "ix_inbox_conversations_external_thread",
            "channel_type",
            "external_thread_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    primary_service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    channel_type: Mapped[str] = mapped_column(
        String(40), default=InboxChannelType.email.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(40), default=InboxConversationStatus.open.value, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subject: Mapped[str | None] = mapped_column(String(200))
    contact_address: Mapped[str | None] = mapped_column(String(255))
    external_thread_id: Mapped[str | None] = mapped_column(String(255))
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    primary_service_team = relationship("ServiceTeam")
    team_links = relationship(
        "InboxConversationTeam",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    messages = relationship(
        "InboxMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    assignments = relationship(
        "InboxConversationAssignment",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class InboxConversationLeadLink(Base):
    """Durable, auditable Inbox conversation to Sales Lead provenance."""

    __tablename__ = "inbox_conversation_lead_links"
    __table_args__ = (
        CheckConstraint(
            "(is_active IS TRUE AND deactivated_at IS NULL) OR "
            "(is_active IS FALSE AND deactivated_at IS NOT NULL)",
            name="ck_inbox_conversation_lead_links_active_evidence",
        ),
        Index(
            "uq_inbox_conversation_lead_links_active_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("is_active IS TRUE"),
            postgresql_where=text("is_active IS TRUE"),
        ),
        Index(
            "ix_inbox_conversation_lead_links_lead_active",
            "lead_id",
            "is_active",
        ),
        UniqueConstraint("command_id", name="uq_inbox_conversation_lead_links_command"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parties.id", ondelete="RESTRICT"),
        nullable=False,
    )
    link_source: Mapped[str] = mapped_column(String(80), nullable=False)
    link_reason: Mapped[str] = mapped_column(Text, nullable=False)
    linked_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)

    conversation = relationship("InboxConversation")
    lead = relationship("Lead")
    party = relationship("Party")


class InboxSavedFilter(Base):
    __tablename__ = "inbox_saved_filters"
    __table_args__ = (
        Index("ix_inbox_saved_filters_owner_active", "owner_person_id", "is_active"),
        Index("ix_inbox_saved_filters_shared_active", "is_shared", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    filter_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class InboxLabel(Base):
    __tablename__ = "inbox_labels"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_inbox_labels_slug"),
        Index("ix_inbox_labels_active", "is_active", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str | None] = mapped_column(String(24))
    description: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class InboxConversationLabel(Base):
    __tablename__ = "inbox_conversation_labels"
    __table_args__ = (
        Index(
            "ix_inbox_conversation_labels_conversation",
            "conversation_id",
            "is_active",
        ),
        Index("ix_inbox_conversation_labels_label", "label_id", "is_active"),
        Index(
            "uq_inbox_conversation_labels_active",
            "conversation_id",
            "label_id",
            unique=True,
            sqlite_where=text("is_active IS TRUE"),
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_labels.id"), nullable=False
    )
    applied_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation")
    label = relationship("InboxLabel")


class InboxReplyMacro(Base):
    __tablename__ = "inbox_reply_macros"
    __table_args__ = (
        Index("ix_inbox_reply_macros_active", "is_active", "name"),
        Index("ix_inbox_reply_macros_creator", "created_by_person_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(40), default="shared", nullable=False
    )
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actions: Mapped[list | None] = mapped_column(JSON)
    execution_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class InboxMessageTemplate(Base):
    __tablename__ = "inbox_message_templates"
    __table_args__ = (
        Index(
            "ix_inbox_message_templates_channel_active",
            "channel_type",
            "is_active",
        ),
        Index("ix_inbox_message_templates_name", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    channel_type: Mapped[str] = mapped_column(
        String(40), default=InboxChannelType.email.value, nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(200))
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class InboxContactLink(Base):
    __tablename__ = "inbox_contact_links"
    __table_args__ = (
        CheckConstraint(
            "(subscriber_id IS NOT NULL AND reseller_id IS NULL)"
            " OR (subscriber_id IS NULL AND reseller_id IS NOT NULL)",
            name="ck_inbox_contact_links_one_target",
        ),
        CheckConstraint(
            "(party_contact_point_id IS NULL AND "
            "party_contact_point_bound_at IS NULL AND "
            "party_contact_point_binding_source IS NULL AND "
            "party_contact_point_binding_reason IS NULL) OR "
            "(party_contact_point_id IS NOT NULL AND "
            "party_contact_point_bound_at IS NOT NULL AND "
            "party_contact_point_binding_source IS NOT NULL AND "
            "party_contact_point_binding_reason IS NOT NULL AND "
            "length(trim(party_contact_point_binding_source)) > 0 AND "
            "length(trim(party_contact_point_binding_reason)) > 0)",
            name="ck_inbox_contact_links_party_contact_point_evidence",
        ),
        Index(
            "ix_inbox_contact_links_contact",
            "channel_type",
            "normalized_contact",
            "is_active",
        ),
        Index("ix_inbox_contact_links_subscriber", "subscriber_id", "is_active"),
        Index("ix_inbox_contact_links_reseller", "reseller_id", "is_active"),
        Index(
            "ix_inbox_contact_links_party_contact_point",
            "party_contact_point_id",
            "is_active",
        ),
        Index(
            "uq_inbox_contact_links_active_contact",
            "channel_type",
            "normalized_contact",
            unique=True,
            sqlite_where=text("is_active IS TRUE"),
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_contact: Mapped[str] = mapped_column(String(255), nullable=False)
    party_contact_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("party_contact_points.id", ondelete="RESTRICT"),
    )
    party_contact_point_bound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    party_contact_point_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_contact_point_binding_reason: Mapped[str | None] = mapped_column(Text)
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )
    linked_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    reseller = relationship("Reseller")
    party_contact_point = relationship("PartyContactPoint")


class InboxParticipantRelationship(enum.Enum):
    """Who an endpoint turns out to be. Distinct from how it was admitted.

    A customer may be copied and a third party may be the sender, so admission
    source cannot stand in for relationship. ``unknown`` is the honest default:
    Inbox observes that an endpoint took part, and only Party can later say
    whose it is.
    """

    customer = "customer"
    contact = "contact"
    third_party = "third_party"
    unknown = "unknown"


class InboxParticipantAdmissionSource(enum.Enum):
    """How this endpoint came to be on the conversation.

    Evidence, not classification. Reclassifying a participant must never
    rewrite how it was admitted.
    """

    inbound_from = "inbound_from"
    inbound_to = "inbound_to"
    inbound_cc = "inbound_cc"
    outbound_to = "outbound_to"
    outbound_cc = "outbound_cc"
    operator_added = "operator_added"


class InboxConversationParticipant(Base):
    """One endpoint observed taking part in one conversation.

    Endpoint-first, deliberately. A conversation carries a single
    ``contact_address``, so the internal side of a thread is a set
    (``InboxConversationTeam``, ``InboxConversationAssignment``) while the
    customer side was a scalar — leaving "is this sender part of this thread?"
    and "who may receive this transcript?" unanswerable.

    ``party_contact_point_id`` is nullable and follows ``InboxContactLink``:
    Inbox owns the fact that an endpoint participated, Party owns who that
    endpoint belongs to. Requiring the binding would make an unknown colleague,
    a new vendor or an unreviewed address unrepresentable — the exact problem
    this table exists to remove.

    Shadow projection: nothing reads it for a threading or export decision yet.
    """

    __tablename__ = "inbox_conversation_participants"
    __table_args__ = (
        CheckConstraint(
            "(party_contact_point_id IS NULL AND "
            "party_contact_point_bound_at IS NULL AND "
            "party_contact_point_binding_source IS NULL AND "
            "party_contact_point_binding_reason IS NULL) OR "
            "(party_contact_point_id IS NOT NULL AND "
            "party_contact_point_bound_at IS NOT NULL AND "
            "party_contact_point_binding_source IS NOT NULL AND "
            "party_contact_point_binding_reason IS NOT NULL AND "
            "length(trim(party_contact_point_binding_source)) > 0 AND "
            "length(trim(party_contact_point_binding_reason)) > 0)",
            name="ck_inbox_participants_party_contact_point_evidence",
        ),
        CheckConstraint(
            "(is_active IS TRUE AND removed_at IS NULL)"
            " OR (is_active IS FALSE AND removed_at IS NOT NULL)",
            name="ck_inbox_participants_removal_evidence",
        ),
        Index("ix_inbox_participants_conversation", "conversation_id", "is_active"),
        # The lookup a participant-aware threading rule will make: this exact
        # endpoint, on this channel, still active.
        Index(
            "ix_inbox_participants_endpoint",
            "channel_type",
            "normalized_endpoint",
            "is_active",
        ),
        Index(
            "ix_inbox_participants_party_contact_point",
            "party_contact_point_id",
            "is_active",
        ),
        Index(
            "uq_inbox_participants_active_endpoint",
            "conversation_id",
            "channel_type",
            "normalized_endpoint",
            "provider_account_scope",
            unique=True,
            sqlite_where=text("is_active IS TRUE"),
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_endpoint: Mapped[str] = mapped_column(String(320), nullable=False)
    # Two Messenger threads on different Pages can carry the same opaque sender
    # id, so an endpoint is not unique outside its provider account.
    provider_account_scope: Mapped[str] = mapped_column(
        String(200), default="default", nullable=False
    )

    party_contact_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("party_contact_points.id", ondelete="RESTRICT"),
    )
    party_contact_point_bound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    party_contact_point_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_contact_point_binding_reason: Mapped[str | None] = mapped_column(Text)

    relationship_type: Mapped[str] = mapped_column(
        String(24),
        default=InboxParticipantRelationship.unknown.value,
        nullable=False,
    )
    admission_source: Mapped[str] = mapped_column(String(32), nullable=False)
    admission_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_messages.id", ondelete="SET NULL")
    )
    admitted_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    admitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_reason: Mapped[str | None] = mapped_column(Text)

    display_name: Mapped[str | None] = mapped_column(String(200))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation")
    party_contact_point = relationship("PartyContactPoint")


class InboxConversationTeam(Base):
    __tablename__ = "inbox_conversation_teams"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "service_team_id",
            name="uq_inbox_conversation_teams_conversation_team",
        ),
        Index("ix_inbox_conversation_teams_team_role", "service_team_id", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(40), default=InboxTeamRole.participant.value, nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(40), default=InboxTeamSource.routing_rule.value, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation", back_populates="team_links")
    service_team = relationship("ServiceTeam")


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        Index("ix_inbox_messages_conversation", "conversation_id", "created_at"),
        Index(
            "uq_inbox_messages_inbound_external",
            "channel_type",
            "external_message_id",
            unique=True,
            sqlite_where=text(
                "external_message_id IS NOT NULL AND direction = 'inbound'"
            ),
            postgresql_where=text(
                "external_message_id IS NOT NULL AND direction = 'inbound'"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="SET NULL"),
        index=True,
    )
    channel_type: Mapped[str] = mapped_column(
        String(40), default=InboxChannelType.email.value, nullable=False
    )
    direction: Mapped[str] = mapped_column(
        String(40), default=InboxMessageDirection.inbound.value, nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    external_message_id: Mapped[str | None] = mapped_column(String(255))
    external_thread_id: Mapped[str | None] = mapped_column(String(255))
    from_address: Mapped[str | None] = mapped_column(String(255))
    to_addresses: Mapped[list | None] = mapped_column(JSON)
    cc_addresses: Mapped[list | None] = mapped_column(JSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation", back_populates="messages")
    notification = relationship("Notification")


class InboxProviderObservation(Base):
    """Durable normalized provider fact admitted before Inbox consequences."""

    __tablename__ = "inbox_provider_observations"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_account_scope",
            "provider_event_id",
            name="uq_inbox_provider_observations_identity",
        ),
        Index(
            "ix_inbox_provider_observations_status",
            "processing_status",
            "recorded_at",
        ),
        Index(
            "ix_inbox_provider_observations_message",
            "external_message_id",
            "observation_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_account_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    observation_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(255))
    external_thread_id: Mapped[str | None] = mapped_column(String(255))
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[str] = mapped_column(
        String(40), default=InboxObservationStatus.recorded.value, nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_messages.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(120))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class InboxConversationReadState(Base):
    """Canonical per-operator read cursor for one Inbox conversation."""

    __tablename__ = "inbox_conversation_read_states"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "person_id",
            name="uq_inbox_conversation_read_states_person",
        ),
        Index("ix_inbox_conversation_read_states_person", "person_id", "last_read_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    last_read_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_messages.id", ondelete="SET NULL")
    )
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    conversation = relationship("InboxConversation")
    last_read_message = relationship("InboxMessage")


class InboxMediaAsset(Base):
    __tablename__ = "inbox_media_assets"
    __table_args__ = (
        Index("ix_inbox_media_assets_conversation", "conversation_id", "created_at"),
        Index("ix_inbox_media_assets_message", "message_id"),
        Index("ix_inbox_media_assets_provider", "provider", "provider_media_id"),
        Index("ix_inbox_media_assets_download_status", "download_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_messages.id")
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80))
    provider_media_id: Mapped[str | None] = mapped_column(String(255))
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(160))
    file_size: Mapped[int | None] = mapped_column(Integer)
    caption: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    storage_url: Mapped[str | None] = mapped_column(Text)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    download_status: Mapped[str] = mapped_column(
        String(40), default="metadata_only", nullable=False
    )
    download_error: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation")
    message = relationship("InboxMessage")


class InboxComment(Base):
    __tablename__ = "inbox_comments"
    __table_args__ = (
        Index("ix_inbox_comments_conversation", "conversation_id", "created_at"),
        Index("ix_inbox_comments_message", "message_id"),
        Index("ix_inbox_comments_author", "author_person_id", "created_at"),
        Index("ix_inbox_comments_resolved", "is_resolved", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_messages.id")
    )
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(40), default="internal", nullable=False
    )
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation")
    message = relationship("InboxMessage")


class InboxAgentPresence(Base):
    __tablename__ = "inbox_agent_presence"
    __table_args__ = (
        UniqueConstraint("person_id", name="uq_inbox_agent_presence_person"),
        Index("ix_inbox_agent_presence_status", "status"),
        Index("ix_inbox_agent_presence_last_seen_at", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=InboxAgentPresenceStatus.offline.value, nullable=False
    )
    manual_override_status: Mapped[str | None] = mapped_column(String(40))
    max_concurrent_conversations: Mapped[int | None] = mapped_column(Integer)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class InboxAgentIntroductionPreference(Base):
    __tablename__ = "inbox_agent_introduction_preferences"
    __table_args__ = (
        UniqueConstraint("person_id", name="uq_inbox_agent_intro_person"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    auto_send_chat_widget: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class InboxConversationAssignment(Base):
    __tablename__ = "inbox_conversation_assignments"
    __table_args__ = (
        Index(
            "uq_inbox_conversation_one_active_assignment",
            "conversation_id",
            unique=True,
            sqlite_where=text("is_active IS TRUE"),
            postgresql_where=text("is_active IS TRUE"),
        ),
        Index("ix_inbox_conversation_assignments_person", "person_id", "is_active"),
        Index("ix_inbox_conversation_assignments_team", "service_team_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assigned_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_by_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_routing_events.id", ondelete="RESTRICT"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    conversation = relationship("InboxConversation", back_populates="assignments")
    service_team = relationship("ServiceTeam")


class InboxConversationQueueEntry(Base):
    """Durable FIFO admission and settlement evidence for a conversation."""

    __tablename__ = "inbox_conversation_queue_entries"
    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_inbox_queue_entry_conversation"),
        UniqueConstraint(
            "service_team_id", "queue_position", name="uq_inbox_queue_team_position"
        ),
        CheckConstraint("queue_position > 0", name="ck_inbox_queue_position_positive"),
        Index(
            "ix_inbox_queue_team_status_position",
            "service_team_id",
            "status",
            "queue_position",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    queue_position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=InboxQueueEntryStatus.queued.value, nullable=False
    )
    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    conversation = relationship("InboxConversation")
    service_team = relationship("ServiceTeam")


class InboxReplyReminder(Base):
    """Durable per-assignment reminder schedule and repeat evidence."""

    __tablename__ = "inbox_reply_reminders"
    __table_args__ = (
        UniqueConstraint("assignment_id", name="uq_inbox_reply_reminder_assignment"),
        Index("ix_inbox_reply_reminders_due", "is_active", "next_due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversation_assignments.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    waiting_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class InboxRoutingEvent(Base):
    """Append-only authority for assignment, queue and escalation decisions."""

    __tablename__ = "inbox_routing_events"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_inbox_routing_event_source"),
        CheckConstraint(
            "event_type IN ('assigned', 'reassigned', 'queued', 'unassigned', "
            "'escalated', 'auto_assignment_declined')",
            name="ck_inbox_routing_event_type",
        ),
        CheckConstraint(
            "source IN ('routing_command', 'historical_backfill')",
            name="ck_inbox_routing_event_source",
        ),
        CheckConstraint(
            "evidence_grade IN ('native', 'authoritative_historical', "
            "'strongly_inferred', 'weakly_inferred', 'unknown')",
            name="ck_inbox_routing_event_evidence_grade",
        ),
        CheckConstraint(
            "decision_mode IN ('manual', 'automatic', 'system')",
            name="ck_inbox_routing_event_decision_mode",
        ),
        Index(
            "ix_inbox_routing_event_conversation_time", "conversation_id", "occurred_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[InboxRoutingEventType] = mapped_column(
        Enum(InboxRoutingEventType), nullable=False
    )
    previous_service_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    previous_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decision_mode: Mapped[InboxRoutingDecisionMode] = mapped_column(
        Enum(InboxRoutingDecisionMode), nullable=False
    )
    presence_status: Mapped[str | None] = mapped_column(String(40))
    presence_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    active_conversation_count: Mapped[int | None] = mapped_column(Integer)
    max_concurrent_conversations: Mapped[int | None] = mapped_column(Integer)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[InboxAuditSource] = mapped_column(
        Enum(InboxAuditSource), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence_grade: Mapped[InboxAuditEvidenceGrade] = mapped_column(
        Enum(InboxAuditEvidenceGrade), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class InboxStatusTransitionEvent(Base):
    """Append-only authority for conversation lifecycle status transitions."""

    __tablename__ = "inbox_status_transition_events"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_inbox_status_event_source"),
        CheckConstraint(
            "status IN ('open', 'pending', 'snoozed', 'resolved')",
            name="ck_inbox_status_event_status",
        ),
        CheckConstraint(
            "source IN ('status_command', 'historical_backfill')",
            name="ck_inbox_status_event_source_kind",
        ),
        CheckConstraint(
            "evidence_grade IN ('native', 'authoritative_historical', "
            "'strongly_inferred', 'weakly_inferred', 'unknown')",
            name="ck_inbox_status_event_evidence_grade",
        ),
        Index(
            "ix_inbox_status_event_conversation_time", "conversation_id", "occurred_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    previous_status: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[InboxAuditSource] = mapped_column(
        Enum(InboxAuditSource), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence_grade: Mapped[InboxAuditEvidenceGrade] = mapped_column(
        Enum(InboxAuditEvidenceGrade), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class InboxAgentPresenceEvent(Base):
    """Append-only authority for agent availability changes."""

    __tablename__ = "inbox_agent_presence_events"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_inbox_presence_event_source"),
        CheckConstraint(
            "status IN ('online', 'away', 'on_break', 'offline')",
            name="ck_inbox_presence_event_status",
        ),
        CheckConstraint(
            "source IN ('presence_command', 'historical_backfill')",
            name="ck_inbox_presence_event_source_kind",
        ),
        CheckConstraint(
            "evidence_grade IN ('native', 'authoritative_historical', "
            "'strongly_inferred', 'weakly_inferred', 'unknown')",
            name="ck_inbox_presence_event_evidence_grade",
        ),
        Index("ix_inbox_presence_event_person_time", "person_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[InboxAuditSource] = mapped_column(
        Enum(InboxAuditSource), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    evidence_grade: Mapped[InboxAuditEvidenceGrade] = mapped_column(
        Enum(InboxAuditEvidenceGrade), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class InboxAuditReconstructionRun(Base):
    """Immutable receipt for one reviewed historical reconstruction batch."""

    __tablename__ = "inbox_audit_reconstruction_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_inbox_audit_reconstruction_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_watermark: Mapped[str] = mapped_column(String(240), nullable=False)
    approval_reference: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    applied_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exception_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
