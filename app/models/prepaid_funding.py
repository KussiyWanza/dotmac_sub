"""Durable authority records for reconstructed prepaid funding positions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PrepaidFundingReconstructionBatch(Base):
    """One reviewed, content-addressed reconstruction manifest."""

    __tablename__ = "prepaid_funding_reconstruction_batches"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_batch_currency",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_prepaid_funding_batch_manifest_hash",
        ),
        CheckConstraint(
            "length(manifest_payload_sha256) = 64",
            name="ck_prepaid_funding_batch_payload_hash",
        ),
        CheckConstraint(
            "length(attestation_sha256) = 64",
            name="ck_prepaid_funding_batch_attestation_hash",
        ),
        CheckConstraint(
            "length(attestation_key_fingerprint_sha256) = 64",
            name="ck_prepaid_funding_batch_attestation_key_hash",
        ),
        CheckConstraint(
            "length(blocker_manifest_sha256) = 64",
            name="ck_prepaid_funding_batch_blocker_hash",
        ),
        CheckConstraint(
            "length(candidate_cohort_sha256) = 64",
            name="ck_prepaid_funding_batch_cohort_hash",
        ),
        Index(
            "uq_prepaid_funding_authority_cutover",
            "is_authority_cutover",
            unique=True,
            postgresql_where=text("is_authority_cutover = true"),
            sqlite_where=text("is_authority_cutover = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    manifest_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    manifest_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    attestation_key_fingerprint_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    attestation_signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    blocker_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_cohort_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(240), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    position_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    account_count: Mapped[int] = mapped_column(nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(120), nullable=False)
    is_authority_cutover: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    baselines = relationship(
        "PrepaidFundingBaseline",
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class PrepaidFundingBaseline(Base):
    """Approved customer position through one exact reconstruction timestamp."""

    __tablename__ = "prepaid_funding_baselines"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_baseline_currency",
        ),
        UniqueConstraint(
            "batch_id",
            "account_id",
            "currency",
            name="uq_prepaid_funding_baseline_batch_account_currency",
        ),
        Index(
            "uq_prepaid_funding_baseline_active_account_currency",
            "account_id",
            "currency",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "ix_prepaid_funding_baseline_batch_id",
            "batch_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prepaid_funding_reconstruction_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    position_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    batch = relationship(
        "PrepaidFundingReconstructionBatch",
        back_populates="baselines",
    )
    account = relationship("Subscriber")


class PrepaidOpeningFundingConsumption(Base):
    """Immutable use of one reviewed opening position against one invoice."""

    __tablename__ = "prepaid_opening_funding_consumptions"
    __table_args__ = (
        CheckConstraint(
            "amount > 0",
            name="ck_prepaid_opening_consumption_positive_amount",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_opening_consumption_currency",
        ),
        CheckConstraint(
            "length(reconciliation_fingerprint) = 64",
            name="ck_prepaid_opening_consumption_fingerprint",
        ),
        Index(
            "uq_prepaid_opening_consumption_invoice",
            "invoice_id",
            unique=True,
        ),
        Index(
            "uq_prepaid_opening_consumption_ledger",
            "ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_prepaid_opening_consumption_idempotency",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_prepaid_opening_consumption_baseline",
            "baseline_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    baseline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prepaid_funding_baselines.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    approval_evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    approval_actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reconciliation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    baseline = relationship("PrepaidFundingBaseline")
    account = relationship("Subscriber")
    invoice = relationship("Invoice")
    ledger_entry = relationship("LedgerEntry")


class PrepaidDraftReconciliationException(Base):
    """Durable operator work item for a prepaid draft funding mismatch."""

    __tablename__ = "prepaid_draft_reconciliation_exceptions"
    __table_args__ = (
        CheckConstraint(
            "required_amount > 0",
            name="ck_prepaid_draft_exception_required_amount",
        ),
        CheckConstraint(
            "payment_backed_amount >= 0 AND opening_funding_amount >= 0",
            name="ck_prepaid_draft_exception_nonnegative_sources",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_prepaid_draft_exception_status",
        ),
        CheckConstraint(
            "attempt_count >= 1",
            name="ck_prepaid_draft_exception_attempt_count",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_draft_exception_currency",
        ),
        CheckConstraint(
            "length(preview_fingerprint) = 64",
            name="ck_prepaid_draft_exception_fingerprint",
        ),
        Index(
            "uq_prepaid_draft_exception_invoice",
            "invoice_id",
            unique=True,
        ),
        Index(
            "ix_prepaid_draft_exception_status_created",
            "status",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", server_default="open"
    )
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    required_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    payment_backed_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )
    opening_funding_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    alert_fingerprint: Mapped[str] = mapped_column(String(160), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
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

    account = relationship("Subscriber")
    invoice = relationship("Invoice")
