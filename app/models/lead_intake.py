"""Versioned Inbox lead-intake templates, AI evidence, and invitations."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class LeadIntakePartyType(enum.StrEnum):
    individual = "individual"
    organization = "organization"


class LeadIntakeTemplateStatus(enum.StrEnum):
    draft = "draft"
    published = "published"
    retired = "retired"


class LeadIntakeInvitationStatus(enum.StrEnum):
    issued = "issued"
    completed = "completed"
    expired = "expired"
    revoked = "revoked"


class LeadIntakeAssessmentDecision(enum.StrEnum):
    not_eligible = "not_eligible"
    clarification_required = "clarification_required"
    invite_issued = "invite_issued"
    staff_review = "staff_review"
    provider_failed = "provider_failed"


class LeadIntakeTemplate(Base):
    __tablename__ = "lead_intake_templates"
    __table_args__ = (
        UniqueConstraint(
            "party_type", "version", name="uq_lead_intake_templates_type_version"
        ),
        CheckConstraint(
            "party_type IN ('individual', 'organization')",
            name="ck_lead_intake_templates_party_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_lead_intake_templates_status",
        ),
        CheckConstraint("version > 0", name="ck_lead_intake_templates_version"),
        Index(
            "uq_lead_intake_templates_published_type",
            "party_type",
            unique=True,
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_type: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=LeadIntakeTemplateStatus.draft.value, nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    heading: Mapped[str] = mapped_column(String(200), nullable=False)
    introduction: Mapped[str | None] = mapped_column(Text)
    privacy_notice: Mapped[str] = mapped_column(Text, nullable=False)
    invitation_message: Mapped[str] = mapped_column(Text, nullable=False)
    confirmation_message: Mapped[str] = mapped_column(Text, nullable=False)
    thank_you_message: Mapped[str] = mapped_column(Text, nullable=False)
    target_service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    owner_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipelines.id")
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_stages.id")
    )
    created_by_system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    target_service_team = relationship("ServiceTeam")
    owner_system_user = relationship("SystemUser", foreign_keys=[owner_system_user_id])
    pipeline = relationship("Pipeline")
    stage = relationship("PipelineStage")


class LeadIntakeAssessment(Base):
    __tablename__ = "lead_intake_assessments"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_lead_intake_assessments_message"),
        CheckConstraint(
            "party_type IN ('individual', 'organization', 'unknown')",
            name="ck_lead_intake_assessments_party_type",
        ),
        CheckConstraint(
            "decision IN ('not_eligible', 'clarification_required', 'invite_issued', 'staff_review', 'provider_failed')",
            name="ck_lead_intake_assessments_decision",
        ),
        CheckConstraint(
            "intent_confidence >= 0 AND intent_confidence <= 1 AND party_type_confidence >= 0 AND party_type_confidence <= 1",
            name="ck_lead_intake_assessments_confidence",
        ),
        Index(
            "ix_lead_intake_assessments_conversation", "conversation_id", "created_at"
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
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    intent_key: Mapped[str] = mapped_column(String(80), nullable=False)
    intent_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    party_type: Mapped[str] = mapped_column(String(24), nullable=False)
    party_type_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_label: Mapped[str | None] = mapped_column(String(80))
    model_label: Mapped[str | None] = mapped_column(String(160))
    clarification_question: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class LeadIntakeInvitation(Base):
    __tablename__ = "lead_intake_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_lead_intake_invitations_token_hash"),
        CheckConstraint(
            "status IN ('issued', 'completed', 'expired', 'revoked')",
            name="ck_lead_intake_invitations_status",
        ),
        CheckConstraint(
            "channel_type IN ('whatsapp', 'facebook_messenger', 'instagram_dm')",
            name="ck_lead_intake_invitations_channel",
        ),
        CheckConstraint(
            "expires_at > issued_at", name="ck_lead_intake_invitations_expiry"
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND lead_id IS NOT NULL AND party_id IS NOT NULL) OR (status <> 'completed' AND completed_at IS NULL)",
            name="ck_lead_intake_invitations_completion",
        ),
        Index(
            "uq_lead_intake_invitations_auto_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("auto_issued IS TRUE"),
            sqlite_where=text("auto_issued IS TRUE"),
        ),
        Index(
            "ix_lead_intake_invitations_conversation", "conversation_id", "issued_at"
        ),
        Index("ix_lead_intake_invitations_lead", "lead_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lead_intake_templates.id"), nullable=False
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lead_intake_assessments.id")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_messages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=LeadIntakeInvitationStatus.issued.value, nullable=False
    )
    auto_issued: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_account_scope: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_endpoint: Mapped[str] = mapped_column(String(320), nullable=False)
    intent_key: Mapped[str | None] = mapped_column(String(80))
    intent_confidence: Mapped[float | None] = mapped_column(Float)
    party_type_confidence: Mapped[float | None] = mapped_column(Float)
    issued_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(240))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_messages.id")
    )
    delivery_status: Mapped[str | None] = mapped_column(String(40))
    delivery_error_code: Mapped[str | None] = mapped_column(String(120))
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="RESTRICT")
    )
    party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    representative_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_contact_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("party_contact_points.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    template = relationship("LeadIntakeTemplate")
    assessment = relationship("LeadIntakeAssessment")
    conversation = relationship("InboxConversation", foreign_keys=[conversation_id])
    trigger_message = relationship("InboxMessage", foreign_keys=[trigger_message_id])
    lead = relationship("Lead")
