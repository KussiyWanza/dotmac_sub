"""Additive persistence foundation for versioned connector installations."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
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


class IntegrationInstallationState(str, enum.Enum):
    draft = "draft"
    validating = "validating"
    disabled = "disabled"
    enabled = "enabled"
    quarantined = "quarantined"
    retired = "retired"


class IntegrationBindingState(str, enum.Enum):
    disabled = "disabled"
    enabled = "enabled"


class IntegrationValidationStatus(str, enum.Enum):
    pending = "pending"
    valid = "valid"
    invalid = "invalid"


class IntegrationInstallation(Base):
    __tablename__ = "integration_installations"
    __table_args__ = (
        UniqueConstraint(
            "connector_key",
            "name",
            name="uq_integration_installations_connector_name",
        ),
        CheckConstraint(
            "state IN ('draft', 'validating', 'disabled', 'enabled', "
            "'quarantined', 'retired')",
            name="ck_integration_installations_state",
        ),
        CheckConstraint(
            "environment IN ('production', 'sandbox', 'test')",
            name="ck_integration_installations_environment",
        ),
        Index(
            "ix_integration_installations_key_state",
            "connector_key",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connector_key: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    environment: Mapped[str] = mapped_column(
        String(24), nullable=False, default="production"
    )
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=IntegrationInstallationState.draft.value
    )
    state_reason: Mapped[str | None] = mapped_column(Text)
    current_config_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "integration_config_revisions.id",
            name="fk_integration_installations_current_config_revision",
            use_alter=True,
        ),
    )
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(160))
    updated_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    current_config_revision = relationship(
        "IntegrationConfigRevision",
        foreign_keys=[current_config_revision_id],
        post_update=True,
    )
    config_revisions = relationship(
        "IntegrationConfigRevision",
        back_populates="installation",
        foreign_keys="IntegrationConfigRevision.installation_id",
        cascade="all, delete-orphan",
        order_by="IntegrationConfigRevision.revision",
    )
    capability_bindings = relationship(
        "IntegrationCapabilityBinding",
        back_populates="installation",
        cascade="all, delete-orphan",
    )


class IntegrationConfigRevision(Base):
    __tablename__ = "integration_config_revisions"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "revision",
            name="uq_integration_config_revisions_installation_revision",
        ),
        UniqueConstraint(
            "installation_id",
            "config_digest",
            name="uq_integration_config_revisions_installation_digest",
        ),
        CheckConstraint(
            "validation_status IN ('pending', 'valid', 'invalid')",
            name="ck_integration_config_revisions_validation_status",
        ),
        Index(
            "ix_integration_config_revisions_installation_created",
            "installation_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="v1"
    )
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    secret_refs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=IntegrationValidationStatus.pending.value,
    )
    validation_errors: Mapped[list | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    installation = relationship(
        "IntegrationInstallation",
        back_populates="config_revisions",
        foreign_keys=[installation_id],
    )


class IntegrationCapabilityBinding(Base):
    __tablename__ = "integration_capability_bindings"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            "capability_id",
            name="uq_integration_capability_bindings_installation_capability",
        ),
        CheckConstraint(
            "state IN ('disabled', 'enabled')",
            name="ck_integration_capability_bindings_state",
        ),
        Index(
            "ix_integration_capability_bindings_capability_state",
            "capability_id",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability_id: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=IntegrationBindingState.disabled.value
    )
    scope_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(160))
    updated_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    installation = relationship(
        "IntegrationInstallation",
        back_populates="capability_bindings",
    )


class IntegrationCheckpoint(Base):
    __tablename__ = "integration_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "capability_binding_id",
            name="uq_integration_checkpoints_job_binding",
        ),
        CheckConstraint("version >= 1", name="ck_integration_checkpoints_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability_binding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_capability_bindings.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cursor_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_runs.id", ondelete="SET NULL")
    )
    advanced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    job = relationship("IntegrationJob")
    capability_binding = relationship("IntegrationCapabilityBinding")
    last_run = relationship("IntegrationRun")


class IntegrationEventSubscription(Base):
    __tablename__ = "integration_event_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "capability_binding_id",
            "event_type",
            name="uq_integration_event_subscriptions_binding_event",
        ),
        CheckConstraint(
            "state IN ('disabled', 'enabled')",
            name="ck_integration_event_subscriptions_state",
        ),
        Index(
            "ix_integration_event_subscriptions_event_state",
            "event_type",
            "state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    capability_binding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_capability_bindings.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="disabled")
    filter_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload_policy_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_by: Mapped[str | None] = mapped_column(String(160))
    updated_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    capability_binding = relationship("IntegrationCapabilityBinding")


class IntegrationDelivery(Base):
    __tablename__ = "integration_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_integration_deliveries_idempotency_key"
        ),
        CheckConstraint(
            "state IN ('pending', 'leased', 'delivered', "
            "'retryable', 'reconciliation_required', 'dead_letter', 'canceled')",
            name="ck_integration_deliveries_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_integration_deliveries_attempt_count"
        ),
        Index(
            "ix_integration_deliveries_state_next_attempt",
            "state",
            "next_attempt_at",
        ),
        Index(
            "ix_integration_deliveries_binding_created",
            "capability_binding_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_event_subscriptions.id", ondelete="SET NULL"),
    )
    capability_binding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_capability_bindings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    destination_key: Mapped[str] = mapped_column(String(240), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    external_receipt_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscription = relationship("IntegrationEventSubscription")
    capability_binding = relationship("IntegrationCapabilityBinding")


class IntegrationInbox(Base):
    __tablename__ = "integration_inbox"
    __table_args__ = (
        UniqueConstraint(
            "capability_binding_id",
            "provider_event_id",
            name="uq_integration_inbox_binding_provider_event",
        ),
        CheckConstraint(
            "state IN ('verified', 'processing', 'processed', 'retryable', "
            "'dead_letter')",
            name="ck_integration_inbox_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_integration_inbox_attempt_count"
        ),
        Index("ix_integration_inbox_state_received", "state", "received_at"),
        Index(
            "ix_integration_inbox_processing_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'processing'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability_binding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_capability_bindings.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    headers_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="verified")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consequence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_detail: Mapped[str | None] = mapped_column(Text)
    # Set whenever `state` becomes 'processing' (fresh claim or reclaim);
    # cleared on every terminal transition. A NULL value while `state` is
    # 'processing' means the row predates this column and is treated as
    # already expired (mirrors IntegrationDelivery.leased_until).
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    installation = relationship("IntegrationInstallation")
    capability_binding = relationship("IntegrationCapabilityBinding")
