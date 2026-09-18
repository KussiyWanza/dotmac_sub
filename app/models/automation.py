"""Automation Center rule definitions and immutable published versions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AutomationRuleStatus(StrEnum):
    draft = "draft"
    published = "published"
    paused = "paused"
    retired = "retired"


class AutomationRunStatus(StrEnum):
    pending = "pending"
    running = "running"
    skipped = "skipped"
    succeeded = "succeeded"
    failed = "failed"
    blocked = "blocked"


class AutomationStepStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    blocked = "blocked"


class AutomationRule(Base):
    __tablename__ = "automation_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_automation_rules_tenant_key"),
        UniqueConstraint("tenant_id", "id", name="uq_automation_rules_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "active_version_id"],
            ["automation_rule_versions.tenant_id", "automation_rule_versions.id"],
            name="fk_automation_rules_active_version_tenant",
            use_alter=True,
        ),
        CheckConstraint("length(trim(key)) > 0", name="ck_automation_rules_key"),
        CheckConstraint("length(trim(name)) > 0", name="ck_automation_rules_name"),
        CheckConstraint(
            "status IN ('draft', 'published', 'paused', 'retired')",
            name="ck_automation_rules_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Tenant is kernel-owned on separate SQLAlchemy metadata. Migration 584
    # remains the database authority for this foreign key.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    trigger_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=AutomationRuleStatus.draft.value
    )
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AutomationRuleVersion(Base):
    __tablename__ = "automation_rule_versions"
    __table_args__ = (
        UniqueConstraint(
            "rule_id", "version", name="uq_automation_rule_versions_rule_version"
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_automation_rule_versions_tenant_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "rule_id"],
            ["automation_rules.tenant_id", "automation_rules.id"],
            ondelete="CASCADE",
            name="fk_automation_rule_versions_rule_tenant",
        ),
        Index(
            "uq_automation_rule_versions_draft",
            "rule_id",
            unique=True,
            postgresql_where=text("published_at IS NULL"),
            sqlite_where=text("published_at IS NULL"),
        ),
        CheckConstraint("version >= 1", name="ck_automation_rule_versions_version"),
        CheckConstraint(
            "trigger_schema_version >= 1",
            name="ck_automation_rule_versions_trigger_schema",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    conditions: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    actions: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    published_by: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationRun(Base):
    __tablename__ = "automation_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_automation_runs_tenant_id"),
        UniqueConstraint(
            "rule_version_id", "event_id", name="uq_automation_runs_version_event"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "rule_id"],
            ["automation_rules.tenant_id", "automation_rules.id"],
            name="fk_automation_runs_rule_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "rule_version_id"],
            ["automation_rule_versions.tenant_id", "automation_rule_versions.id"],
            name="fk_automation_runs_version_tenant",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'skipped', 'succeeded', 'failed', "
            "'blocked')",
            name="ck_automation_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    target_type: Mapped[str] = mapped_column(String(120), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=AutomationRunStatus.pending.value
    )
    matched: Mapped[bool | None] = mapped_column(Boolean)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(160))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AutomationStepRun(Base):
    __tablename__ = "automation_step_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "step_index", name="uq_automation_step_runs_run_index"
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_automation_step_runs_idempotency_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["automation_runs.tenant_id", "automation_runs.id"],
            ondelete="CASCADE",
            name="fk_automation_step_runs_run_tenant",
        ),
        CheckConstraint("step_index >= 0", name="ck_automation_step_runs_index"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'blocked')",
            name="ck_automation_step_runs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_automation_step_runs_attempt_count"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    action_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=AutomationStepStatus.pending.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(160))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
