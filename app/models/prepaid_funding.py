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
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
        CheckConstraint(
            "(baseline_id IS NOT NULL AND opening_position_id IS NULL) "
            "OR (baseline_id IS NULL AND opening_position_id IS NOT NULL)",
            name="ck_prepaid_opening_consumption_one_source",
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
        Index(
            "ix_prepaid_opening_consumption_opening_position",
            "opening_position_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prepaid_funding_baselines.id", ondelete="RESTRICT"),
    )
    opening_position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer_subledger_opening_positions.id", ondelete="RESTRICT"),
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
    opening_position = relationship("CustomerSubledgerOpeningPosition")
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
        # A partial unique index rather than a table-wide unique column:
        # `invoice_id` is now nullable (see below) for a review item raised
        # BEFORE any invoice was ever created (the pre-mutation ambiguous
        # classification never creates a document), and Postgres/SQLite both
        # treat every NULL as distinct under a plain unique index/constraint
        # anyway — WHERE invoice_id IS NOT NULL makes that explicit rather
        # than relying on that NULL-handling quirk.
        Index(
            "uq_prepaid_draft_exception_invoice",
            "invoice_id",
            unique=True,
            postgresql_where=text("invoice_id IS NOT NULL"),
            sqlite_where=text("invoice_id IS NOT NULL"),
        ),
        # Migration 601 (2026-09, round 7): the no-invoice (pre-mutation
        # ambiguous classification) case's own uniqueness key, mirroring the
        # with-invoice index above. Only covers the case that carries a real
        # subscription/period identity -- see that migration's docstring for
        # why the fully-degenerate all-null case is a named, separate gap.
        Index(
            "uq_prepaid_draft_exception_no_invoice_identity",
            "account_id",
            "subscription_id",
            "period_start",
            "period_end",
            unique=True,
            postgresql_where=text(
                "invoice_id IS NULL AND subscription_id IS NOT NULL "
                "AND period_start IS NOT NULL AND period_end IS NOT NULL"
            ),
            sqlite_where=text(
                "invoice_id IS NULL AND subscription_id IS NOT NULL "
                "AND period_start IS NOT NULL AND period_end IS NOT NULL"
            ),
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
    # Additive (migration 599) + widened to nullable (migration 600, round 2):
    # a pre-mutation ambiguous classification (funding-consequence owner,
    # round 2) makes ZERO mutations before writing this review item, so there
    # is no invoice to point at yet. `subscription_id`/`period_start`/
    # `period_end` become the primary evidence for that case instead.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    subscription = relationship("Subscription")


class PrepaidFundingTriggerExecution(Base):
    """One durable receipt per funding event this owner has ever processed.

    Answers "have we already processed this EXACT durable event" — a
    different question from the period-level protection
    (`InvoiceLine.billing_line_key`/`_invoice_backed_renewal_evidence`), which
    answers "is this exact subscription+period already funded, regardless of
    which trigger did it." Both must exist; this table does not replace that
    one. Written exactly once, in the SAME transaction as every consequence
    it describes — never a pre-work "started" placeholder.
    """

    __tablename__ = "prepaid_funding_trigger_executions"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_trigger_currency",
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_prepaid_funding_trigger_request_fingerprint",
        ),
        CheckConstraint(
            "length(outcome_fingerprint) = 64",
            name="ck_prepaid_funding_trigger_outcome_fingerprint",
        ),
        Index(
            "uq_prepaid_funding_trigger_event_store",
            "event_store_id",
            unique=True,
        ),
        Index(
            "ix_prepaid_funding_trigger_account_created",
            "account_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_store_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("event_store.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Stored redundantly from `EventStore.event_id`: durable reference that
    # survives even if `event_store` rows are ever pruned/archived.
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    settlement_reference: Mapped[str | None] = mapped_column(String(160))
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    contract_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    account = relationship("Subscriber")
    subscription_outcomes = relationship(
        "PrepaidFundingTriggerSubscriptionOutcome",
        back_populates="trigger_execution",
        cascade="all, delete-orphan",
    )


class PrepaidFundingTriggerSubscriptionOutcome(Base):
    """One child decision row per subscription one trigger receipt touched.

    A single funding event can legitimately fund more than one due
    subscription on the same account (e.g. two prepaid lines). Unique on
    ``(trigger_execution_id, subscription_id, period_start, period_end)`` —
    the receipt can record at most one outcome per subscription+period.
    """

    __tablename__ = "prepaid_funding_trigger_subscription_outcomes"
    __table_args__ = (
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_funding_trigger_outcome_currency",
        ),
        CheckConstraint(
            "length(evidence_fingerprint) = 64",
            name="ck_prepaid_funding_trigger_outcome_fingerprint",
        ),
        UniqueConstraint(
            "trigger_execution_id",
            "subscription_id",
            "period_start",
            "period_end",
            name="uq_prepaid_funding_trigger_outcome_period",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trigger_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prepaid_funding_trigger_executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    funding_source: Mapped[str | None] = mapped_column(String(40))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT")
    )
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_lines.id", ondelete="RESTRICT")
    )
    entitlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_entitlements.id", ondelete="RESTRICT")
    )
    # Serialized list of PaymentAllocation ids / opening-consumption id — the
    # exact shape of "what funded this" varies by funding_source, so this
    # stays a typed-at-the-application-layer JSON list rather than a second
    # join table.
    funding_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    trigger_execution = relationship(
        "PrepaidFundingTriggerExecution", back_populates="subscription_outcomes"
    )
    subscription = relationship("Subscription")
    invoice = relationship("Invoice")
    invoice_line = relationship("InvoiceLine")
    entitlement = relationship("ServiceEntitlement")
