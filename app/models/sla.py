"""Immutable customer SLA period-score evidence and revisions.

The mutable operational sources remain owned by lifecycle, billing, sessions,
and outage services.  These rows are the customer.service_level owner's
append-only calculation snapshot: one score revision plus the exact eligibility
and positive-monitoring intervals it consumed.  Re-running against changed
evidence appends another revision; it never rewrites what an earlier result was
based on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SlaPeriodScoreRevision(Base):
    """One immutable calculation of one subscription reporting period."""

    __tablename__ = "sla_period_score_revisions"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "period_start",
            "period_end",
            "revision",
            name="uq_sla_period_scores_period_revision",
        ),
        UniqueConstraint(
            "subscription_id",
            "period_start",
            "period_end",
            "evidence_digest",
            name="uq_sla_period_scores_period_evidence",
        ),
        UniqueConstraint(
            "command_id",
            name="uq_sla_period_scores_command_id",
        ),
        UniqueConstraint(
            "command_idempotency_key",
            name="uq_sla_period_scores_idempotency_key",
        ),
        UniqueConstraint(
            "id",
            "subscription_id",
            name="uq_sla_period_scores_id_subscription",
        ),
        UniqueConstraint(
            "id",
            "subscription_id",
            "period_start",
            "period_end",
            name="uq_sla_period_scores_identity_scope",
        ),
        ForeignKeyConstraint(
            ["supersedes_id", "subscription_id", "period_start", "period_end"],
            [
                "sla_period_score_revisions.id",
                "sla_period_score_revisions.subscription_id",
                "sla_period_score_revisions.period_start",
                "sla_period_score_revisions.period_end",
            ],
            ondelete="RESTRICT",
            name="fk_sla_period_scores_supersedes",
        ),
        CheckConstraint(
            "period_end > period_start AND evaluated_at >= period_start",
            name="ck_sla_period_scores_time_bounds",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_sla_period_scores_revision",
        ),
        CheckConstraint(
            "(revision = 1 AND supersedes_id IS NULL) OR "
            "(revision > 1 AND supersedes_id IS NOT NULL)",
            name="ck_sla_period_scores_revision_link",
        ),
        CheckConstraint(
            "eligible_seconds >= 0 AND unavailable_seconds >= 0 "
            "AND excluded_seconds >= 0 AND unknown_seconds >= 0",
            name="ck_sla_period_scores_nonnegative_seconds",
        ),
        CheckConstraint(
            "unavailable_seconds + excluded_seconds + unknown_seconds "
            "<= eligible_seconds",
            name="ck_sla_period_scores_accounted_bounds",
        ),
        CheckConstraint(
            "verdict IN ('passing', 'at_risk', 'breach', 'unavailable', "
            "'no_contractual_sla')",
            name="ck_sla_period_scores_verdict",
        ),
        CheckConstraint(
            "evidence_complete OR verdict NOT IN ('passing', 'at_risk')",
            name="ck_sla_period_scores_no_incomplete_pass",
        ),
        CheckConstraint(
            "availability_lower_bound_percent IS NULL OR "
            "(availability_lower_bound_percent >= 0 "
            "AND availability_lower_bound_percent <= 100)",
            name="ck_sla_period_scores_lower_bound",
        ),
        CheckConstraint(
            "availability_upper_bound_percent IS NULL OR "
            "(availability_upper_bound_percent >= 0 "
            "AND availability_upper_bound_percent <= 100)",
            name="ck_sla_period_scores_upper_bound",
        ),
        CheckConstraint(
            "availability_lower_bound_percent IS NULL "
            "OR availability_upper_bound_percent IS NULL "
            "OR availability_lower_bound_percent "
            "<= availability_upper_bound_percent",
            name="ck_sla_period_scores_bound_order",
        ),
        Index(
            "ix_sla_period_scores_subscription_period",
            "subscription_id",
            "period_start",
            "period_end",
            "revision",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "subscriptions.id",
            ondelete="RESTRICT",
            name="fk_sla_period_scores_subscription",
        ),
        nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
    )

    eligible_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    unavailable_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    unknown_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    completeness_issues: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    availability_lower_bound_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4)
    )
    availability_upper_bound_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4)
    )

    policy_segments: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False
    )
    policy_version_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    outage_interval_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    lifecycle_evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(71), nullable=False)

    recorded_by: Mapped[str] = mapped_column(String(160), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    command_idempotency_key: Mapped[str | None] = mapped_column(String(160))
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    eligibility_intervals: Mapped[list[SlaEligibilityInterval]] = relationship(
        "SlaEligibilityInterval",
        back_populates="score_revision",
        passive_deletes=True,
    )
    monitoring_intervals: Mapped[list[SlaMonitoringInterval]] = relationship(
        "SlaMonitoringInterval",
        back_populates="score_revision",
        passive_deletes=True,
    )


class SlaEligibilityInterval(Base):
    """One proven eligible span copied into a score revision with lineage."""

    __tablename__ = "sla_score_eligibility_intervals"
    __table_args__ = (
        UniqueConstraint(
            "score_revision_id",
            "fingerprint",
            name="uq_sla_eligibility_score_fingerprint",
        ),
        ForeignKeyConstraint(
            ["score_revision_id", "subscription_id"],
            [
                "sla_period_score_revisions.id",
                "sla_period_score_revisions.subscription_id",
            ],
            ondelete="RESTRICT",
            name="fk_sla_eligibility_score",
        ),
        CheckConstraint(
            "ends_at > starts_at", name="ck_sla_eligibility_positive_interval"
        ),
        CheckConstraint(
            "evidence_grade IN ('authoritative', 'provisional')",
            name="ck_sla_eligibility_evidence_grade",
        ),
        CheckConstraint(
            "fingerprint LIKE 'sha256:%'",
            name="ck_sla_eligibility_fingerprint",
        ),
        Index(
            "ix_sla_eligibility_score_time",
            "score_revision_id",
            "starts_at",
            "ends_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    score_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "subscriptions.id",
            ondelete="RESTRICT",
            name="fk_sla_eligibility_subscription",
        ),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_grade: Mapped[str] = mapped_column(String(24), nullable=False)
    entitlement_source: Mapped[str] = mapped_column(String(48), nullable=False)
    entitlement_evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    lifecycle_evidence_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    score_revision: Mapped[SlaPeriodScoreRevision] = relationship(
        "SlaPeriodScoreRevision", back_populates="eligibility_intervals"
    )


class SlaMonitoringInterval(Base):
    """One positive service-monitoring span copied into a score revision."""

    __tablename__ = "sla_score_monitoring_intervals"
    __table_args__ = (
        UniqueConstraint(
            "score_revision_id",
            "fingerprint",
            name="uq_sla_monitoring_score_fingerprint",
        ),
        ForeignKeyConstraint(
            ["score_revision_id", "subscription_id"],
            [
                "sla_period_score_revisions.id",
                "sla_period_score_revisions.subscription_id",
            ],
            ondelete="RESTRICT",
            name="fk_sla_monitoring_score",
        ),
        CheckConstraint(
            "ends_at > starts_at", name="ck_sla_monitoring_positive_interval"
        ),
        CheckConstraint(
            "source IN ('radius_accounting_session')",
            name="ck_sla_monitoring_source",
        ),
        CheckConstraint(
            "fingerprint LIKE 'sha256:%'",
            name="ck_sla_monitoring_fingerprint",
        ),
        Index(
            "ix_sla_monitoring_score_time",
            "score_revision_id",
            "starts_at",
            "ends_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    score_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "subscriptions.id",
            ondelete="RESTRICT",
            name="fk_sla_monitoring_subscription",
        ),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    score_revision: Mapped[SlaPeriodScoreRevision] = relationship(
        "SlaPeriodScoreRevision", back_populates="monitoring_intervals"
    )
