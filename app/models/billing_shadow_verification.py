"""Durable ADR 0007 shadow-pipeline and cutover-verification evidence."""

from __future__ import annotations

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
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BillingShadowDeliveryEvidence(Base):
    """One terminal receipt from the Sale→Contract→Obligation shadow chain."""

    __tablename__ = "billing_shadow_delivery_evidence"
    __table_args__ = (
        UniqueConstraint(
            "terminal_event_id",
            name="uq_billing_shadow_delivery_terminal_event",
        ),
        CheckConstraint(
            "obligation_count >= 0",
            name="ck_billing_shadow_delivery_obligation_count",
        ),
        CheckConstraint(
            "length(obligation_ids_sha256) = 64",
            name="ck_billing_shadow_delivery_obligation_hash",
        ),
        Index(
            "ix_billing_shadow_delivery_sales_order",
            "sales_order_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    terminal_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    obligation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    obligation_ids_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class BillingCutoverVerificationRun(Base):
    """Immutable complete-cohort evidence; approvals are explicit sign-offs."""

    __tablename__ = "billing_cutover_verification_runs"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_billing_cutover_verification_idempotency",
        ),
        CheckConstraint(
            "cohort_count >= 0 AND covered_count >= 0 "
            "AND unresolved_count >= 0 AND ambiguous_count >= 0 "
            "AND unexpected_unlinked_count >= 0 AND duplicate_count >= 0 "
            "AND shadow_variance_count >= 0 "
            "AND expected_difference_count >= 0 AND gap_count >= 0 "
            "AND overlap_count >= 0",
            name="ck_billing_cutover_verification_nonnegative_counts",
        ),
        CheckConstraint(
            "covered_count <= cohort_count",
            name="ck_billing_cutover_verification_covered_bound",
        ),
        CheckConstraint(
            "length(source_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_billing_cutover_verification_hashes",
        ),
        CheckConstraint(
            "(operator_approved_by IS NULL AND operator_approved_at IS NULL) OR "
            "(operator_approved_by IS NOT NULL AND operator_approved_at IS NOT NULL)",
            name="ck_billing_cutover_operator_approval_pair",
        ),
        CheckConstraint(
            "(finance_approved_by IS NULL AND finance_approved_at IS NULL) OR "
            "(finance_approved_by IS NOT NULL AND finance_approved_at IS NOT NULL)",
            name="ck_billing_cutover_finance_approval_pair",
        ),
        CheckConstraint(
            "finance_approved_at IS NULL OR operator_approved_at IS NOT NULL",
            name="ck_billing_cutover_finance_requires_operator",
        ),
        Index(
            "ix_billing_cutover_verification_phase_cutoff",
            "phase",
            "cutoff_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    phase: Mapped[str] = mapped_column(String(40), nullable=False)
    cohort_name: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observation_ended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cohort_count: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ambiguous_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unexpected_unlinked_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    shadow_variance_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_difference_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    gap_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overlap_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    currency_totals: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    cohort_classification: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False
    )
    event_outcomes: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    code_version: Mapped[str] = mapped_column(String(80), nullable=False)
    database_schema_version: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operator_approved_by: Mapped[str | None] = mapped_column(String(120))
    operator_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    finance_approved_by: Mapped[str | None] = mapped_column(String(120))
    finance_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    @property
    def blockers_are_zero(self) -> bool:
        return all(
            value == 0
            for value in (
                self.unresolved_count,
                self.ambiguous_count,
                self.unexpected_unlinked_count,
                self.duplicate_count,
                self.shadow_variance_count,
                self.gap_count,
                self.overlap_count,
            )
        )

    @property
    def approved(self) -> bool:
        return (
            self.blockers_are_zero
            and self.operator_approved_at is not None
            and self.finance_approved_at is not None
        )
