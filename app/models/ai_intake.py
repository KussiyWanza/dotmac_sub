from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
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
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AiIntakeConfig(Base):
    __tablename__ = "ai_intake_configs"
    __table_args__ = (
        UniqueConstraint("scope_key", name="uq_ai_intake_configs_scope_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence_threshold: Mapped[float] = mapped_column(
        Float, default=0.75, nullable=False
    )
    allow_followup_questions: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    max_clarification_turns: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    escalate_after_minutes: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    exclude_campaign_attribution: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    fallback_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    instructions: Mapped[str | None] = mapped_column(Text)
    department_mappings: Mapped[list | None] = mapped_column(
        MutableList.as_mutable(JSON())
    )
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


class AiIntakePolicy(Base):
    """Composable, versioned policy shell for conversational intake.

    ``AiIntakeConfig`` remains as the compatibility adapter. New conversational
    sessions attach to an immutable policy version so a customer-visible AI
    message can always be traced to the exact approved business prompt.
    """

    __tablename__ = "ai_intake_policies"
    __table_args__ = (
        UniqueConstraint(
            "scope_key",
            "channel_type",
            "provider",
            "account_scope",
            name="uq_ai_intake_policies_scope_channel_provider",
        ),
        Index("ix_ai_intake_policies_active", "is_enabled", "channel_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    legacy_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_intake_configs.id", ondelete="SET NULL")
    )
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="any", nullable=False)
    account_scope: Mapped[str] = mapped_column(
        String(160), default="any", nullable=False
    )
    display_name: Mapped[str] = mapped_column(
        String(120), default="Dotmac Virtual Assistant", nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    fallback_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
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


class AiIntakePolicyVersion(Base):
    __tablename__ = "ai_intake_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "policy_id",
            "version_number",
            name="uq_ai_intake_policy_versions_policy_number",
        ),
        CheckConstraint(
            "status IN ('draft', 'activated', 'superseded')",
            name="ck_ai_intake_policy_versions_status",
        ),
        Index("ix_ai_intake_policy_versions_policy", "policy_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_intake_policies.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    display_name: Mapped[str] = mapped_column(
        String(120), default="Dotmac Virtual Assistant", nullable=False
    )
    welcome_message: Mapped[str] = mapped_column(Text, nullable=False)
    business_tone: Mapped[str | None] = mapped_column(Text)
    business_instructions: Mapped[str | None] = mapped_column(Text)
    approved_isp_information: Mapped[str | None] = mapped_column(Text)
    protected_system_instructions_version: Mapped[str] = mapped_column(
        String(40), default="2026-08-12", nullable=False
    )
    intent_definitions: Mapped[list | None] = mapped_column(
        MutableList.as_mutable(JSON())
    )
    clarification_questions: Mapped[list | None] = mapped_column(
        MutableList.as_mutable(JSON())
    )
    intent_team_mappings: Mapped[list | None] = mapped_column(
        MutableList.as_mutable(JSON())
    )
    queue_templates: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON()))
    escalation_rules: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON())
    )
    data_cleanup_policy: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON())
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_by_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AiIntakeSession(Base):
    __tablename__ = "ai_intake_sessions"
    __table_args__ = (
        Index(
            "uq_ai_intake_sessions_active_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("completed_at IS NULL"),
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index("ix_ai_intake_sessions_state", "state", "expires_at"),
        CheckConstraint(
            "state IN ('eligible', 'welcome_pending', 'collecting_intent', "
            "'awaiting_customer', 'classified', 'handoff_requested', 'completed', "
            "'stopped_human_takeover', 'fallback_escalated', 'expired', 'failed', "
            "'ineligible')",
            name="ck_ai_intake_sessions_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_conversations.id"), nullable=False
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_intake_policies.id", ondelete="SET NULL")
    )
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_intake_policy_versions.id", ondelete="SET NULL"),
    )
    legacy_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_intake_configs.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(
        String(120), default="Dotmac Virtual Assistant", nullable=False
    )
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_turns: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    confidence_threshold: Mapped[float] = mapped_column(
        Float, default=0.75, nullable=False
    )
    fallback_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    final_intent: Mapped[str | None] = mapped_column(String(120))
    final_category: Mapped[str | None] = mapped_column(String(120))
    final_confidence: Mapped[float | None] = mapped_column(Float)
    handoff_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    takeover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class AiIntakeGenerationAttempt(Base):
    __tablename__ = "ai_intake_generation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_ai_intake_generation_attempt_idempotency"
        ),
        Index("ix_ai_intake_generation_attempts_session", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_intake_sessions.id"), nullable=False
    )
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    message_purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(160))
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
