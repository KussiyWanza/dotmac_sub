import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.db import Base


class CustomerNotificationStatus(enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class SurveyStatus(enum.Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    closed = "closed"


class SurveyTriggerType(enum.Enum):
    manual = "manual"
    ticket_closed = "ticket_closed"
    work_order_completed = "work_order_completed"


class SurveyInvitationStatus(enum.Enum):
    pending = "pending"
    completed = "completed"
    expired = "expired"


class CustomerNotificationEvent(Base):
    __tablename__ = "customer_notification_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="SET NULL"),
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CustomerNotificationStatus] = mapped_column(
        Enum(CustomerNotificationStatus), default=CustomerNotificationStatus.pending
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class EtaUpdate(Base):
    __tablename__ = "eta_updates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    # Backwards-compatible alias.
    work_order_id: Mapped[uuid.UUID] = synonym("service_order_id")
    eta_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Survey(Base):
    __tablename__ = "surveys"
    __table_args__ = (
        UniqueConstraint("public_slug", name="uq_surveys_public_slug"),
        UniqueConstraint(
            "creation_idempotency_key",
            name="uq_surveys_creation_idempotency_key",
        ),
        CheckConstraint(
            "trigger_type IN ('manual', 'ticket_closed', 'work_order_completed')",
            name="ck_surveys_trigger_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'closed')",
            name="ck_surveys_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    questions: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    trigger_type: Mapped[SurveyTriggerType] = mapped_column(
        Enum(SurveyTriggerType, native_enum=False, length=32),
        nullable=False,
        default=SurveyTriggerType.manual,
        server_default=SurveyTriggerType.manual.value,
    )
    public_slug: Mapped[str | None] = mapped_column(String(120))
    thank_you_message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[SurveyStatus] = mapped_column(
        Enum(SurveyStatus, native_enum=False, length=16),
        nullable=False,
        default=SurveyStatus.draft,
        server_default=SurveyStatus.draft.value,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    segment_filter: Mapped[dict[str, object] | None] = mapped_column(JSON)
    total_invited: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_responses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    avg_rating: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    nps_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    creation_idempotency_key: Mapped[str | None] = mapped_column(String(80))
    creation_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class SurveyResponse(Base):
    __tablename__ = "survey_responses"
    __table_args__ = (
        UniqueConstraint("invitation_id", name="uq_survey_responses_invitation_id"),
        CheckConstraint(
            "nps_value IS NULL OR (nps_value >= 0 AND nps_value <= 10)",
            name="ck_survey_responses_nps_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    survey_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("surveys.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("survey_invitations.id", ondelete="RESTRICT"),
    )
    # Optional linkage fields (used by legacy workflows/tests).
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    responses: Mapped[dict[str, str] | None] = mapped_column(JSON)
    rating: Mapped[int | None] = mapped_column(Integer)
    nps_value: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class SurveyInvitation(Base):
    __tablename__ = "survey_invitations"
    __table_args__ = (
        UniqueConstraint("token", name="uq_survey_invitations_token"),
        UniqueConstraint(
            "survey_id",
            "subscriber_id",
            "source_event_id",
            name="uq_survey_invitations_event_recipient",
        ),
        CheckConstraint(
            "length(trim(token)) > 0",
            name="ck_survey_invitations_token_not_blank",
        ),
        CheckConstraint(
            "source_type IN ('manual', 'ticket_closed', 'work_order_completed')",
            name="ck_survey_invitations_source_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'expired')",
            name="ck_survey_invitations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    survey_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("surveys.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    token: Mapped[str] = mapped_column(String(80), nullable=False)
    source_type: Mapped[SurveyTriggerType] = mapped_column(
        Enum(SurveyTriggerType, native_enum=False, length=32), nullable=False
    )
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    source_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[SurveyInvitationStatus] = mapped_column(
        Enum(SurveyInvitationStatus, native_enum=False, length=16),
        nullable=False,
        default=SurveyInvitationStatus.pending,
        server_default=SurveyInvitationStatus.pending.value,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
