import enum
import uuid
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TicketStatus(enum.Enum):
    new = "new"
    open = "open"
    pending = "pending"
    waiting_on_customer = "waiting_on_customer"
    lastmile_rerun = "lastmile_rerun"
    site_under_construction = "site_under_construction"
    on_hold = "on_hold"
    pending_confirmation = "pending_confirmation"
    closed = "closed"
    canceled = "canceled"
    merged = "merged"


LEGACY_TICKET_STATUS_ALIASES: dict[str, str] = {
    "resolved": TicketStatus.closed.value,
}


def canonical_ticket_status_value(value: TicketStatus | str) -> str:
    """Return the canonical stored value for a ticket-status input.

    ``resolved`` was historically persisted as a separate status. It is now an
    admission-only compatibility alias for ``closed`` and must never be
    returned to a caller or written to a Ticket row.
    """

    raw = value.value if isinstance(value, TicketStatus) else str(value).strip().lower()
    return LEGACY_TICKET_STATUS_ALIASES.get(raw, raw)


def parse_ticket_status(value: TicketStatus | str) -> TicketStatus:
    """Parse a canonical or legacy ticket-status input into the closed enum."""

    return TicketStatus(canonical_ticket_status_value(value))


class TicketPriority(enum.Enum):
    lower = "lower"
    low = "low"
    medium = "medium"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class TicketChannel(enum.Enum):
    web = "web"
    email = "email"
    phone = "phone"
    chat = "chat"
    api = "api"


class TicketCommentAuthorType(enum.Enum):
    customer = "customer"
    staff = "staff"
    system = "system"


class Ticket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_tickets_number", "number"),
        Index("ix_support_tickets_status", "status"),
        Index("ix_support_tickets_priority", "priority"),
        Index("ix_support_tickets_subscriber", "subscriber_id"),
        Index("ix_support_tickets_active", "is_active"),
        Index(
            "ix_support_tickets_external_system_reference",
            "external_system",
            "external_reference",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Optional relations
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    customer_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="RESTRICT")
    )
    # Native provenance for a ticket opened from an inbox conversation. Written
    # only by ``communications.conversation_ticket_handoff``; one conversation
    # may issue many tickets. Mirrors ``work_order.origin_ticket_id``.
    origin_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="RESTRICT"),
    )
    customer_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    # Creator and assignment fields can reference internal system users or CRM
    # staff. Legacy rows may still contain subscriber IDs, so keep them as plain
    # UUIDs instead of subscriber FKs.
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    assigned_to_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    technician_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ticket_manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    site_coordinator_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    service_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    number: Mapped[str | None] = mapped_column(String(50), unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    description_is_internal: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    region: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(
        String(80),
        default=TicketStatus.open.value,
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        String(40),
        default=TicketPriority.normal.value,
        nullable=False,
    )
    ticket_type: Mapped[str | None] = mapped_column(String(120))
    # NCC quarterly complaints return (①): the classification is derived from
    # the ticket's text on save and STORED here, so the filing projects a
    # captured decision instead of re-guessing at report time. ``*_source`` is
    # "derived" or "agent"; an agent value is never re-derived.
    # See app/services/ncc_categorisation.py.
    ncc_category: Mapped[str | None] = mapped_column(String(80))
    ncc_category_source: Mapped[str | None] = mapped_column(String(16))
    ncc_subcategory: Mapped[str | None] = mapped_column(String(120))
    ncc_subcategory_source: Mapped[str | None] = mapped_column(String(16))
    external_system: Mapped[str | None] = mapped_column(String(40))
    external_reference: Mapped[str | None] = mapped_column(String(100))
    channel: Mapped[TicketChannel] = mapped_column(
        Enum(TicketChannel, values_callable=lambda x: [e.value for e in x]),
        default=TicketChannel.web,
        nullable=False,
    )
    tags: Mapped[list[str] | None] = mapped_column(
        MutableList.as_mutable(JSON()), default=list
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    attachments: Mapped[list[dict] | None] = mapped_column(
        MutableList.as_mutable(JSON()), default=list
    )

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    merged_into_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id")
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def csat_rating(self) -> int | None:
        """Support-satisfaction score (1-5) if the customer rated this ticket.
        Backed by ``metadata.csat.rating`` (see support.Tickets.set_satisfaction)
        and surfaced as a first-class field on TicketRead."""
        meta = self.metadata_ if isinstance(self.metadata_, dict) else None
        csat = meta.get("csat") if meta else None
        if isinstance(csat, dict) and isinstance(csat.get("rating"), int | float):
            return int(csat["rating"])
        return None

    comments = relationship(
        "TicketComment", back_populates="ticket", cascade="all, delete-orphan"
    )
    assignees = relationship(
        "TicketAssignee", back_populates="ticket", cascade="all, delete-orphan"
    )
    sla_events = relationship(
        "TicketSlaEvent", back_populates="ticket", cascade="all, delete-orphan"
    )
    lead = relationship("Lead", foreign_keys=[lead_id])


class TicketAssignee(Base):
    __tablename__ = "support_ticket_assignees"
    __table_args__ = (
        UniqueConstraint("ticket_id", "person_id", name="uq_support_ticket_assignee"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), primary_key=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    ticket = relationship("Ticket", back_populates="assignees")


class TicketComment(Base):
    __tablename__ = "support_ticket_comments"
    __table_args__ = (Index("ix_support_ticket_comments_ticket", "ticket_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    author_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    author_type: Mapped[str] = mapped_column(
        String(40),
        default=TicketCommentAuthorType.system.value,
        nullable=False,
    )
    author_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attachments: Mapped[list[dict] | None] = mapped_column(
        MutableList.as_mutable(JSON()), default=list
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    ticket = relationship("Ticket", back_populates="comments")


class TicketSlaEvent(Base):
    __tablename__ = "support_ticket_sla_events"
    __table_args__ = (Index("ix_support_ticket_sla_events_ticket", "ticket_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    ticket = relationship("Ticket", back_populates="sla_events")


class TicketMerge(Base):
    __tablename__ = "support_ticket_merges"
    __table_args__ = (
        UniqueConstraint(
            "source_ticket_id", "target_ticket_id", name="uq_support_ticket_merge_pair"
        ),
    )

    source_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), primary_key=True
    )
    target_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), primary_key=True
    )
    reason: Mapped[str | None] = mapped_column(Text)
    merged_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TicketLink(Base):
    __tablename__ = "support_ticket_links"
    __table_args__ = (
        UniqueConstraint(
            "from_ticket_id", "to_ticket_id", "link_type", name="uq_support_ticket_link"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    from_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    to_ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    link_type: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TicketAccessToken(Base):
    """Magic-link token letting a customer confirm (or dispute) a ticket's
    resolution without logging in. Only a SHA-256 digest of the capability is
    persisted; the raw token exists in memory only while its link is created."""

    __tablename__ = "ticket_access_tokens"
    __table_args__ = (
        Index("ix_ticket_access_tokens_token_hash", "token_hash", unique=True),
        Index("ix_ticket_access_tokens_ticket_id", "ticket_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_token: ClassVar[str | None] = None
    purpose: Mapped[str] = mapped_column(
        String(40), default="resolution_confirm", nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    ticket = relationship("Ticket")


class AutomationTrigger(enum.Enum):
    ticket_created = "ticket_created"
    status_changed = "status_changed"
    priority_changed = "priority_changed"


class AutomationActionType(enum.Enum):
    assign_team = "assign_team"
    assign_technician = "assign_technician"
    set_priority = "set_priority"
    set_status = "set_status"
    set_due_in_hours = "set_due_in_hours"
    add_tag = "add_tag"


class TicketAutomationRule(Base):
    """Declarative rule that runs against tickets on a trigger event.

    conditions: equality dict matched against the ticket
    (e.g. {"priority": "urgent", "ticket_type": "outage"}).
    action_value semantics depend on action_type:
      assign_team        -> {"service_team_id": "<uuid>"}
      assign_technician  -> {"technician_person_id": "<uuid>"}
      set_priority       -> {"priority": "high"}
      set_status         -> {"status": "open"}
      set_due_in_hours   -> {"hours": 24}
      add_tag            -> {"tag": "vip"}
    """

    __tablename__ = "support_ticket_automation_rules"
    __table_args__ = (
        Index("ix_support_automation_trigger_active", "trigger", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[AutomationTrigger] = mapped_column(
        Enum(AutomationTrigger, name="ticket_automation_trigger"), nullable=False
    )
    conditions: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON()), default=dict, nullable=False
    )
    action_type: Mapped[AutomationActionType] = mapped_column(
        Enum(AutomationActionType, name="ticket_automation_action_type"),
        nullable=False,
    )
    action_value: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON()), default=dict, nullable=False
    )
    sort_order: Mapped[int] = mapped_column(default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Observability: last successful application and last failure (if any).
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
