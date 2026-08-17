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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InvoiceStatus(enum.Enum):
    draft = "draft"
    issued = "issued"
    partially_paid = "partially_paid"
    paid = "paid"
    void = "void"
    overdue = "overdue"
    # Closed-but-not-collected: the obligation was written off as bad debt.
    # Materially distinct from ``paid`` (obligation satisfied) and ``void``
    # (invoice should never have existed). The loss is recorded as a credit
    # adjustment in the ledger (the financial source of truth); the invoice
    # stays on record, excluded from outstanding/aging but NOT counted as cash.
    written_off = "written_off"


class InvoiceDueDateBasis(enum.Enum):
    """Authoritative provenance for one immutable issued due date."""

    contract_terms = "contract_terms"
    prepaid_service_period = "prepaid_service_period"
    provider_observation = "provider_observation"
    approved_manual_override = "approved_manual_override"
    unknown_unverified = "unknown_unverified"


class InvoiceDiscountType(enum.Enum):
    percentage = "percentage"
    fixed_amount = "fixed_amount"


class InvoiceDiscountAction(enum.Enum):
    applied = "applied"
    changed = "changed"
    removed = "removed"
    inherited = "inherited"


class InvoiceDiscountSource(enum.Enum):
    manual = "manual"
    quote = "quote"


class InvoiceClosureType(enum.Enum):
    void = "void"
    write_off = "write_off"


class InvoiceClosureOrigin(enum.Enum):
    manual = "manual"
    system = "system"
    historical_reconciliation = "historical_reconciliation"


class InvoicePdfExportStatus(enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class BillingRunStatus(enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class PaymentMethodType(enum.Enum):
    card = "card"
    bank_account = "bank_account"
    cash = "cash"
    check = "check"
    transfer = "transfer"
    other = "other"


class PaymentStatus(enum.Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"
    partially_refunded = "partially_refunded"
    # A previously settled payment whose remaining cash was clawed back. This
    # is not a failed capture and not a customer refund.
    reversed = "reversed"
    canceled = "canceled"


class TopupIntentPurpose(enum.Enum):
    """Server-owned reason for collecting money through a top-up intent."""

    account_credit_deposit = "account_credit_deposit"


class TopupAllocationPolicy(enum.Enum):
    """How a confirmed intent first records the provider receipt."""

    credit_only = "credit_only"
    invoice_first_then_credit = "invoice_first_then_credit"


class AccountCreditApplicationPolicy(enum.Enum):
    """What Sub does with evidenced account credit after settlement."""

    pay_eligible_invoices = "pay_eligible_invoices"


class PaymentRefundOrigin(enum.Enum):
    manual = "manual"
    provider_event = "provider_event"


class PaymentReversalOrigin(enum.Enum):
    manual = "manual"
    administrative_correction = "administrative_correction"
    provider_event = "provider_event"


class PaymentSettlementOrigin(enum.Enum):
    manual = "manual"
    provider_event = "provider_event"
    system = "system"


class PaymentProviderEventFinancialEffect(enum.Enum):
    none = "none"
    refund_confirmed = "refund_confirmed"
    reversal_confirmed = "reversal_confirmed"


class PaymentProviderType(enum.Enum):
    stripe = "stripe"
    paypal = "paypal"
    paystack = "paystack"
    flutterwave = "flutterwave"
    manual = "manual"
    custom = "custom"


class PaymentProviderEventStatus(enum.Enum):
    pending = "pending"
    processed = "processed"
    failed = "failed"


class PaymentProviderEventSource(enum.Enum):
    """Persisted trust boundary for normalized provider observations."""

    legacy_unknown = "legacy_unknown"
    administrative_ingest = "administrative_ingest"
    verified_webhook = "verified_webhook"
    gateway_reconciliation = "gateway_reconciliation"


class LedgerEntryType(enum.Enum):
    debit = "debit"
    credit = "credit"


class LedgerSource(enum.Enum):
    invoice = "invoice"
    payment = "payment"
    adjustment = "adjustment"
    refund = "refund"
    credit_note = "credit_note"
    other = "other"


class LedgerCategory(enum.Enum):
    """What the ledger entry is for (financial reporting category)."""

    internet_service = "internet_service"
    custom_service = "custom_service"
    voice_service = "voice_service"
    bundle_service = "bundle_service"
    installation_fee = "installation_fee"
    equipment_rental = "equipment_rental"
    equipment_purchase = "equipment_purchase"
    late_payment_fee = "late_payment_fee"
    reconnection_fee = "reconnection_fee"
    deposit = "deposit"
    discount = "discount"
    tax = "tax"
    overage = "overage"
    top_up = "top_up"
    other = "other"


class ServiceEntitlementStatus(enum.Enum):
    active = "active"
    void = "void"
    reversed = "reversed"


class TaxApplication(enum.Enum):
    exclusive = "exclusive"
    inclusive = "inclusive"
    exempt = "exempt"


class BankAccountType(enum.Enum):
    checking = "checking"
    savings = "savings"
    business = "business"
    other = "other"


class PaymentChannelType(enum.Enum):
    card = "card"
    bank_transfer = "bank_transfer"
    cash = "cash"
    check = "check"
    transfer = "transfer"
    other = "other"


class CollectionAccountType(enum.Enum):
    bank = "bank"
    cash = "cash"
    other = "other"


class BillingAccount(Base):
    """Consolidated billing parent for a Reseller.

    One BillingAccount per Reseller. Owns consolidated payments that can be
    allocated across any invoice belonging to a Subscriber under that Reseller.
    Unallocated payment surplus is held in `balance`.
    """

    __tablename__ = "billing_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resellers.id"),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[str] = mapped_column(String(20), default="active")
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    reseller = relationship("Reseller", back_populates="billing_account")
    payments = relationship("Payment", back_populates="billing_account")
    ledger_entries = relationship(
        "BillingAccountLedgerEntry", back_populates="billing_account"
    )


class BillingAccountLedgerEntry(Base):
    """Append-only evidence for money held at consolidated-account scope.

    Subscriber invoice allocations continue to use ``LedgerEntry`` because
    those transactions change a named subscriber receivable. This ledger owns
    only the reseller billing-account position, so consolidated surplus never
    needs a fake subscriber account and never exists solely as a mutable
    ``BillingAccount.balance`` value.
    """

    __tablename__ = "billing_account_ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "amount > 0", name="ck_billing_account_ledger_entries_amount_positive"
        ),
        Index(
            "uq_billing_account_ledger_entries_payment_credit",
            "payment_id",
            unique=True,
            postgresql_where=text(
                "payment_id IS NOT NULL AND entry_type = 'credit' AND is_active"
            ),
            sqlite_where=text(
                "payment_id IS NOT NULL AND entry_type = 'credit' AND is_active = 1"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    billing_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="RESTRICT")
    )
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        Enum(LedgerEntryType), nullable=False
    )
    source: Mapped[LedgerSource] = mapped_column(Enum(LedgerSource), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    billing_account = relationship("BillingAccount", back_populates="ledger_entries")
    payment = relationship("Payment", back_populates="billing_account_ledger_entries")


class BillingAccountCreditAllocation(Base):
    """Confirmed transfer of consolidated credit to subscriber receivables."""

    __tablename__ = "billing_account_credit_allocations"
    __table_args__ = (
        CheckConstraint(
            "amount > 0", name="ck_billing_account_credit_allocations_amount_positive"
        ),
        UniqueConstraint(
            "billing_account_ledger_entry_id",
            name="uq_billing_account_credit_allocations_debit_entry",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_billing_account_credit_allocations_idempotency_key",
        ),
        Index(
            "ix_billing_account_credit_allocations_account_created",
            "billing_account_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    billing_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    billing_account_ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_account_ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    billing_account = relationship("BillingAccount")
    subscriber = relationship("Subscriber")
    billing_account_ledger_entry = relationship("BillingAccountLedgerEntry")
    items = relationship(
        "BillingAccountCreditAllocationItem",
        back_populates="allocation",
        passive_deletes=True,
    )
    reconciliation_evidence = relationship(
        "ConsolidatedCreditConsumptionReconciliationEvidence",
        back_populates="allocation",
        uselist=False,
    )


class BillingAccountCreditAllocationItem(Base):
    """Exact source-credit and subscriber-ledger evidence for an allocation."""

    __tablename__ = "billing_account_credit_allocation_items"
    __table_args__ = (
        CheckConstraint(
            "amount > 0",
            name="ck_billing_account_credit_allocation_items_amount_positive",
        ),
        UniqueConstraint(
            "payment_allocation_id",
            name="uq_billing_account_credit_allocation_items_payment_allocation",
        ),
        UniqueConstraint(
            "subscriber_ledger_entry_id",
            name="uq_billing_account_credit_allocation_items_subscriber_ledger",
        ),
        Index(
            "ix_billing_account_credit_allocation_items_allocation",
            "allocation_id",
        ),
        Index(
            "ix_billing_account_credit_allocation_items_source",
            "source_billing_account_ledger_entry_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    allocation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_account_credit_allocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_billing_account_ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_account_ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_allocation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_allocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscriber_ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    allocation = relationship("BillingAccountCreditAllocation", back_populates="items")
    source_billing_account_ledger_entry = relationship("BillingAccountLedgerEntry")
    payment_allocation = relationship("PaymentAllocation")
    subscriber_ledger_entry = relationship("LedgerEntry")


class ConsolidatedCreditConsumptionReconciliationEvidence(Base):
    """Reviewed provenance for one historical consolidated-credit transfer."""

    __tablename__ = "consolidated_credit_consumption_reconciliation_evidence"
    __table_args__ = (
        CheckConstraint(
            "debit_action IN ('linked_existing', 'created_missing')",
            name="ck_consolidated_credit_recon_debit_action",
        ),
        UniqueConstraint(
            "allocation_id", name="uq_consolidated_credit_recon_allocation"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    allocation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_account_credit_allocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    debit_action: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    allocation = relationship(
        "BillingAccountCreditAllocation", back_populates="reconciliation_evidence"
    )


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index(
            "uq_invoices_active_splynx_invoice_id",
            "splynx_invoice_id",
            unique=True,
            postgresql_where=text("is_active AND splynx_invoice_id IS NOT NULL"),
        ),
        # Idempotency key for CRM-created invoices (installation charges). A
        # dedicated column, not metadata->>'crm_external_ref', so the partial
        # unique index is portable to the SQLite test suite. sqlite_where keeps
        # the predicate (else SQLite would constrain inactive/voided rows too).
        Index(
            "uq_invoices_active_crm_external_ref",
            "crm_external_ref",
            unique=True,
            postgresql_where=text("is_active AND crm_external_ref IS NOT NULL"),
            sqlite_where=text("is_active AND crm_external_ref IS NOT NULL"),
        ),
        # Backs the per-account billing list (active invoices, newest first)
        # and FK joins on account_id.
        Index(
            "ix_invoices_account_id_is_active_issued_at",
            "account_id",
            "is_active",
            "issued_at",
        ),
        # Backs the ERP AR incremental sync watermark (WHERE is_active AND
        # updated_at >= :cutoff ORDER BY updated_at). Without it the sync did a
        # global unindexed sort → long-running sessions → app pool starvation.
        Index(
            "ix_invoices_is_active_updated_at",
            "is_active",
            "updated_at",
        ),
        # Backs the un-watermarked UI default (ORDER BY created_at DESC over
        # active invoices) so that path stops sequentially sorting too.
        Index(
            "ix_invoices_is_active_created_at",
            "is_active",
            "created_at",
        ),
        CheckConstraint(
            "discount_revision >= 0",
            name="ck_invoices_discount_revision_nonnegative",
        ),
        CheckConstraint(
            "(discount_type IS NULL AND discount_value IS NULL AND "
            "discount_amount = 0 AND discount_reason IS NULL AND "
            "discount_source IS NULL AND discount_source_quote_id IS NULL AND "
            "discount_applied_by_system_user_id IS NULL AND "
            "discount_applied_at IS NULL) OR "
            "(discount_type IN ('percentage', 'fixed_amount') AND "
            "discount_value > 0 AND discount_amount > 0 AND "
            "discount_amount <= subtotal AND discount_revision > 0 AND "
            "discount_source IN ('manual', 'quote') AND "
            "discount_applied_by_system_user_id IS NOT NULL AND "
            "discount_applied_at IS NOT NULL AND "
            "((discount_source = 'manual' AND discount_source_quote_id IS NULL) OR "
            "(discount_source = 'quote' AND discount_source_quote_id IS NOT NULL)))",
            name="ck_invoices_discount_current_state",
        ),
        CheckConstraint(
            "due_date_basis IS NULL OR "
            "due_date_basis = 'unknown_unverified' OR "
            "(due_at IS NOT NULL AND "
            "(status = 'draft' OR (issued_at IS NOT NULL AND due_at >= issued_at)) "
            "AND due_date_basis_ref IS NOT NULL AND "
            "length(trim(due_date_basis_ref)) > 0 AND "
            "due_date_policy_version IS NOT NULL AND "
            "length(trim(due_date_policy_version)) > 0)",
            name="ck_invoices_verified_due_date_basis",
        ),
        Index(
            "ix_invoices_due_date_basis_due_at",
            "due_date_basis",
            "due_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    invoice_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), default=InvoiceStatus.draft
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    discount_type: Mapped[str | None] = mapped_column(String(24))
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    discount_reason: Mapped[str | None] = mapped_column(Text)
    discount_source: Mapped[str | None] = mapped_column(String(20))
    discount_source_quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="RESTRICT")
    )
    discount_applied_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id", ondelete="RESTRICT")
    )
    discount_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    discount_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    balance_due: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    billing_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    billing_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL is a temporary pre-migration state for legacy rows. New native
    # issuance must persist a verified basis. Explicit unknown/unverified rows
    # are lawful historical observations but cannot drive collection actions.
    due_date_basis: Mapped[InvoiceDueDateBasis | None] = mapped_column(
        Enum(InvoiceDueDateBasis, name="invoice_due_date_basis")
    )
    due_date_basis_ref: Mapped[str | None] = mapped_column(String(255))
    due_date_policy_version: Mapped[str | None] = mapped_column(String(64))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memo: Mapped[str | None] = mapped_column(Text)
    is_proforma: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sent: Mapped[bool | None] = mapped_column(Boolean, default=False)
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    splynx_invoice_id: Mapped[int | None] = mapped_column(Integer)
    # Idempotency key for CRM-created invoices (also mirrored into metadata for
    # back-compat reads); backs uq_invoices_active_crm_external_ref.
    crm_external_ref: Mapped[str | None] = mapped_column(String(120))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber", foreign_keys=[account_id])
    added_by = relationship("Subscriber", foreign_keys=[added_by_id])
    lines = relationship("InvoiceLine", back_populates="invoice")
    payment_allocations = relationship("PaymentAllocation", back_populates="invoice")
    ledger_entries = relationship("LedgerEntry", back_populates="invoice")
    dunning_actions = relationship("DunningActionLog", back_populates="invoice")
    credit_note_applications = relationship(
        "CreditNoteApplication", back_populates="invoice"
    )
    closure = relationship("InvoiceClosure", back_populates="invoice", uselist=False)
    pdf_exports = relationship("InvoicePdfExport", back_populates="invoice")
    discount_applied_by = relationship(
        "SystemUser", foreign_keys=[discount_applied_by_system_user_id]
    )
    discount_source_quote = relationship(
        "Quote", foreign_keys=[discount_source_quote_id]
    )
    discount_history = relationship(
        "InvoiceDiscountHistory",
        back_populates="invoice",
        order_by="InvoiceDiscountHistory.revision",
    )

    @property
    def discounted_subtotal(self) -> Decimal:
        return max(
            Decimal("0.00"),
            Decimal(self.subtotal or 0) - Decimal(self.discount_amount or 0),
        )


class InvoiceDiscountHistory(Base):
    """Append-only evidence for every Invoice-level discount revision."""

    __tablename__ = "invoice_discount_history"
    __table_args__ = (
        UniqueConstraint(
            "invoice_id", "revision", name="uq_invoice_discount_history_revision"
        ),
        UniqueConstraint("command_id", name="uq_invoice_discount_history_command_id"),
        CheckConstraint(
            "revision > 0", name="ck_invoice_discount_history_revision_positive"
        ),
        CheckConstraint(
            "action IN ('applied', 'changed', 'removed', 'inherited')",
            name="ck_invoice_discount_history_action",
        ),
        CheckConstraint(
            "discount_type IN ('percentage', 'fixed_amount')",
            name="ck_invoice_discount_history_type",
        ),
        CheckConstraint(
            "source IN ('manual', 'quote')",
            name="ck_invoice_discount_history_source",
        ),
        CheckConstraint(
            "(source = 'manual' AND source_quote_id IS NULL) OR "
            "(source = 'quote' AND source_quote_id IS NOT NULL)",
            name="ck_invoice_discount_history_source_quote",
        ),
        CheckConstraint(
            "discount_value > 0 AND discount_amount > 0 AND "
            "discount_amount <= original_subtotal AND discounted_subtotal >= 0",
            name="ck_invoice_discount_history_amounts",
        ),
        Index("ix_invoice_discount_history_applied_at", "applied_at"),
        Index("ix_invoice_discount_history_actor", "actor_system_user_id"),
        Index("ix_invoice_discount_history_type", "discount_type"),
        Index("ix_invoice_discount_history_source_quote", "source_quote_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("quotes.id", ondelete="RESTRICT")
    )
    discount_type: Mapped[str] = mapped_column(String(24), nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    original_subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discounted_subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_after_discount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    actor_system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    invoice = relationship("Invoice", back_populates="discount_history")
    actor = relationship("SystemUser", foreign_keys=[actor_system_user_id])
    source_quote = relationship("Quote", foreign_keys=[source_quote_id])


class InvoiceDiscountHistoryImmutableError(RuntimeError):
    """Raised when code attempts to rewrite Invoice discount evidence."""


@event.listens_for(InvoiceDiscountHistory, "before_update")
def _reject_invoice_discount_history_update(*_args: object) -> None:
    raise InvoiceDiscountHistoryImmutableError(
        "Invoice discount history is append-only"
    )


@event.listens_for(InvoiceDiscountHistory, "before_delete")
def _reject_invoice_discount_history_delete(*_args: object) -> None:
    raise InvoiceDiscountHistoryImmutableError(
        "Invoice discount history is append-only"
    )


class InvoiceClosure(Base):
    """Append-only evidence for the one terminal financial closure of an invoice."""

    __tablename__ = "invoice_closures"
    __table_args__ = (
        Index("uq_invoice_closures_invoice_id", "invoice_id", unique=True),
        Index("uq_invoice_closures_idempotency_key", "idempotency_key", unique=True),
        CheckConstraint("amount >= 0", name="ck_invoice_closures_amount_nonnegative"),
        CheckConstraint(
            "receivable_before >= 0",
            name="ck_invoice_closures_receivable_before_nonnegative",
        ),
        CheckConstraint(
            "receivable_after >= 0",
            name="ck_invoice_closures_receivable_after_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    closure_type: Mapped[InvoiceClosureType] = mapped_column(
        Enum(InvoiceClosureType), nullable=False
    )
    origin: Mapped[InvoiceClosureOrigin] = mapped_column(
        Enum(InvoiceClosureOrigin), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    receivable_before: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    receivable_after: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    payments_applied: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    credits_applied: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    invoice = relationship("Invoice", back_populates="closure")
    ledger_evidence = relationship(
        "InvoiceClosureLedgerEvidence", back_populates="closure"
    )


class InvoiceClosureLedgerEvidence(Base):
    """Exact ledger result and optional original entry for an invoice closure."""

    __tablename__ = "invoice_closure_ledger_evidence"
    __table_args__ = (
        UniqueConstraint(
            "closure_id",
            "ledger_entry_id",
            name="uq_invoice_closure_evidence_closure_ledger",
        ),
        Index(
            "uq_invoice_closure_evidence_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    closure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice_closures.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reverses_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    closure = relationship("InvoiceClosure", back_populates="ledger_evidence")
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
    reverses_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[reverses_ledger_entry_id]
    )


class InvoicePdfExport(Base):
    __tablename__ = "invoice_pdf_exports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    status: Mapped[InvoicePdfExportStatus] = mapped_column(
        Enum(InvoicePdfExportStatus, name="invoicepdfexportstatus"),
        default=InvoicePdfExportStatus.queued,
        nullable=False,
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(120))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    invoice = relationship("Invoice", back_populates="pdf_exports")
    requested_by = relationship("Subscriber")


class CreditNoteStatus(enum.Enum):
    draft = "draft"
    issued = "issued"
    partially_applied = "partially_applied"
    applied = "applied"
    void = "void"


class CreditNote(Base):
    __tablename__ = "credit_notes"
    __table_args__ = (
        # Backs the ERP AR incremental sync watermark + the un-watermarked
        # default list sort (see the Invoice indexes for the incident context).
        Index(
            "ix_credit_notes_is_active_updated_at",
            "is_active",
            "updated_at",
        ),
        Index(
            "ix_credit_notes_is_active_created_at",
            "is_active",
            "created_at",
        ),
        Index(
            "uq_credit_notes_funding_ledger_entry_id",
            "funding_ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_credit_notes_void_ledger_entry_id",
            "void_ledger_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    credit_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[CreditNoteStatus] = mapped_column(
        Enum(CreditNoteStatus), default=CreditNoteStatus.draft
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    applied_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    splynx_credit_note_id: Mapped[int | None] = mapped_column(Integer)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # New issued credit notes are backed by one exact unallocated ledger credit.
    # Historical notes remain nullable until reviewed reconciliation supplies
    # or creates evidence for their remaining available amount.
    funding_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    issue_preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Voiding is append-only: this links the note to the exact reversal of its
    # funding entry. A note without reviewed funding evidence cannot be voided.
    void_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    void_preview_fingerprint: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    invoice = relationship("Invoice")
    lines = relationship("CreditNoteLine", back_populates="credit_note")
    applications = relationship("CreditNoteApplication", back_populates="credit_note")
    funding_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[funding_ledger_entry_id]
    )
    void_ledger_entry = relationship("LedgerEntry", foreign_keys=[void_ledger_entry_id])


class CreditNoteLine(Base):
    __tablename__ = "credit_note_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_notes.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    tax_application: Mapped[TaxApplication] = mapped_column(
        Enum(TaxApplication), default=TaxApplication.exclusive
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    credit_note = relationship("CreditNote", back_populates="lines")
    tax_rate = relationship("TaxRate")


class CreditNoteApplication(Base):
    __tablename__ = "credit_note_applications"
    __table_args__ = (
        Index(
            "uq_credit_note_applications_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_credit_note_applications_consumption_ledger_entry_id",
            "consumption_ledger_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_notes.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    # Exact monetary evidence for this application. Historical rows remain
    # nullable because inferring a link from amount/memo would risk attaching
    # the wrong ledger transaction; every new application is linked by the
    # credit-note owner in the same transaction.
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    # Fingerprint of the owner-produced preview that the operator confirmed.
    # This is evidence/stale-state protection, not an authorization token.
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # When an application spends a structurally funded credit note, this is
    # the exact unallocated debit that consumes the same amount from the
    # account credit pool. Historical unfunded applications remain nullable.
    consumption_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    memo: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    credit_note = relationship("CreditNote", back_populates="applications")
    invoice = relationship("Invoice", back_populates="credit_note_applications")
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
    consumption_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[consumption_ledger_entry_id]
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        Index("ix_invoice_lines_invoice_id", "invoice_id"),
        Index(
            "uq_invoice_lines_active_billing_line_key",
            "billing_line_key",
            unique=True,
            postgresql_where=text("is_active AND billing_line_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    tax_application: Mapped[TaxApplication] = mapped_column(
        Enum(TaxApplication), default=TaxApplication.exclusive
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    billing_line_key: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    invoice = relationship("Invoice", back_populates="lines")
    tax_rate = relationship("TaxRate")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (
        # Exactly one owner: a customer subscriber (account_id) OR — for a
        # first-class reseller_user login that has no backing subscriber
        # (Layer 3) — the reseller org (reseller_id). CASE-sum form works on
        # both Postgres and the SQLite test DB.
        CheckConstraint(
            "(CASE WHEN account_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN reseller_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_payment_methods_exactly_one_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id"), nullable=True
    )
    payment_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id")
    )
    method_type: Mapped[PaymentMethodType] = mapped_column(
        Enum(PaymentMethodType), default=PaymentMethodType.card
    )
    label: Mapped[str | None] = mapped_column(String(120))
    token: Mapped[str | None] = mapped_column(String(512))
    last4: Mapped[str | None] = mapped_column(String(4))
    brand: Mapped[str | None] = mapped_column(String(40))
    expires_month: Mapped[int | None] = mapped_column(Integer)
    expires_year: Mapped[int | None] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    reseller = relationship("Reseller")
    payment_channel = relationship("PaymentChannel", back_populates="payment_methods")
    payments = relationship("Payment", back_populates="payment_method")


class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_methods.id")
    )
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_type: Mapped[BankAccountType] = mapped_column(
        Enum(BankAccountType), default=BankAccountType.checking
    )
    account_last4: Mapped[str | None] = mapped_column(String(4))
    routing_last4: Mapped[str | None] = mapped_column(String(4))
    token: Mapped[str | None] = mapped_column(String(512))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    payment_method = relationship("PaymentMethod")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index(
            "uq_payments_active_external_id",
            "provider_id",
            "external_id",
            unique=True,
            postgresql_where=text(
                "is_active AND provider_id IS NOT NULL AND external_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_payments_active_splynx_payment_id",
            "splynx_payment_id",
            unique=True,
            postgresql_where=text("is_active AND splynx_payment_id IS NOT NULL"),
        ),
        # Idempotency backstop for CRM-originated payments. These are recorded
        # with external_id = "crm:<ref>" and NO provider_id, so they fall outside
        # uq_payments_active_external_id (which requires provider_id NOT NULL).
        # A concurrent /crm/payments push could otherwise double-record cash.
        Index(
            "uq_payments_active_crm_external_id",
            "external_id",
            unique=True,
            postgresql_where=text(
                "is_active AND external_id IS NOT NULL AND external_id LIKE 'crm:%'"
            ),
            # Keep the predicate partial on SQLite too (tests) so non-CRM
            # payments sharing an external_id aren't wrongly constrained.
            sqlite_where=text(
                "is_active AND external_id IS NOT NULL AND external_id LIKE 'crm:%'"
            ),
        ),
        # Backs the ERP AR incremental sync watermark + the un-watermarked
        # default list sort (see the Invoice indexes for the incident context).
        Index(
            "ix_payments_is_active_updated_at",
            "is_active",
            "updated_at",
        ),
        Index(
            "ix_payments_is_active_created_at",
            "is_active",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_methods.id")
    )
    payment_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id")
    )
    collection_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection_accounts.id")
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_providers.id")
    )
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    # Total refunded so far (sum of refund ledger entries), maintained by the
    # refund flow. `amount` stays the gross captured figure; net cash =
    # amount - refunded_amount. Exposed so ERP posts the net after a refund.
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0"
    )
    # Payment-gateway fee withheld from settlement (Paystack `fees`,
    # Flutterwave `app_fee`). `amount` stays the gross the customer was charged;
    # the gateway settles `amount - provider_fee` to the bank. Exposed so ERP can
    # split the receipt (Dr Bank net / Dr charges / Cr AR gross) and bank rec ties.
    provider_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.pending
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Pending documents carry intent only. This records whether the settlement
    # owner should allocate across open invoices when cash is later confirmed.
    auto_allocate_on_settlement: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    creation_preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Set only when a durable apply run proves that it created this payment.
    # Reused idempotent rows keep the original/null owner and cannot be claimed
    # by a later import batch reversal.
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_runs.id", ondelete="RESTRICT"),
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(String(120))
    memo: Mapped[str | None] = mapped_column(Text)
    receipt_number: Mapped[str | None] = mapped_column(String(120))
    splynx_payment_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    payment_method = relationship("PaymentMethod", back_populates="payments")
    payment_channel = relationship("PaymentChannel", back_populates="payments")
    collection_account = relationship("CollectionAccount", back_populates="payments")
    provider = relationship("PaymentProvider", back_populates="payments")
    billing_account = relationship("BillingAccount", back_populates="payments")
    provider_events = relationship("PaymentProviderEvent", back_populates="payment")
    ledger_entries = relationship("LedgerEntry", back_populates="payment")
    billing_account_ledger_entries = relationship(
        "BillingAccountLedgerEntry", back_populates="payment"
    )
    dunning_actions = relationship("DunningActionLog", back_populates="payment")
    allocations = relationship("PaymentAllocation", back_populates="payment")
    refunds = relationship("PaymentRefund", back_populates="payment")
    reversal = relationship("PaymentReversal", back_populates="payment", uselist=False)
    settlement = relationship(
        "PaymentSettlement", back_populates="payment", uselist=False
    )
    topup_intents = relationship("TopupIntent", back_populates="completed_payment")
    # One proof-backed reseller payment can raise at most one WHT receivable.
    # The relationship is read by the bounded ERP sync projection so ERP can
    # post net bank cash + WHT receivable against the gross AR settlement.
    withholding_tax_record = relationship(
        "WithholdingTaxRecord",
        back_populates="payment",
        uselist=False,
    )
    import_run = relationship(
        "ImportRun", back_populates="created_payments", foreign_keys=[import_run_id]
    )


class PaymentRefund(Base):
    """One completed refund projection with exact monetary evidence."""

    __tablename__ = "payment_refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_refunds_amount_positive"),
        Index(
            "uq_payment_refunds_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_refunds_billing_account_ledger_entry_id",
            "billing_account_ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_refunds_credit_consumption_ledger_entry_id",
            "credit_consumption_ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_refunds_provider_event_id",
            "provider_event_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_provider_events.id", ondelete="RESTRICT"),
    )
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    billing_account_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_account_ledger_entries.id", ondelete="RESTRICT"),
    )
    # The total refund ledger row is accounting/audit evidence. If part of the
    # refunded payment remained as unallocated account credit, this second link
    # names the exact internal debit that consumes only that wallet portion.
    credit_consumption_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    origin: Mapped[PaymentRefundOrigin] = mapped_column(
        Enum(PaymentRefundOrigin), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    # Historical evidence reconciled from an explicitly selected ledger row has
    # no trustworthy preview and therefore remains NULL instead of being guessed.
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    payment = relationship("Payment", back_populates="refunds")
    provider_event = relationship("PaymentProviderEvent", back_populates="refunds")
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
    billing_account_ledger_entry = relationship(
        "BillingAccountLedgerEntry", foreign_keys=[billing_account_ledger_entry_id]
    )
    credit_consumption_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[credit_consumption_ledger_entry_id]
    )
    consolidated_allocation_evidence = relationship(
        "ConsolidatedPaymentReturnAllocationEvidence",
        back_populates="refund",
        foreign_keys="ConsolidatedPaymentReturnAllocationEvidence.refund_id",
    )
    consolidated_reconciliation_evidence = relationship(
        "ConsolidatedPaymentReturnReconciliationEvidence",
        back_populates="refund",
        foreign_keys="ConsolidatedPaymentReturnReconciliationEvidence.refund_id",
        uselist=False,
    )


class PaymentReversal(Base):
    """One completed removal of settled payment value with exact evidence."""

    __tablename__ = "payment_reversals"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_reversals_amount_positive"),
        Index("uq_payment_reversals_payment_id", "payment_id", unique=True),
        Index(
            "uq_payment_reversals_provider_event_id",
            "provider_event_id",
            unique=True,
        ),
        Index(
            "uq_payment_reversals_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_reversals_billing_account_ledger_entry_id",
            "billing_account_ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_reversals_credit_consumption_ledger_entry_id",
            "credit_consumption_ledger_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_provider_events.id", ondelete="RESTRICT"),
    )
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    billing_account_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_account_ledger_entries.id", ondelete="RESTRICT"),
    )
    credit_consumption_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    origin: Mapped[PaymentReversalOrigin] = mapped_column(
        Enum(PaymentReversalOrigin), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    payment = relationship("Payment", back_populates="reversal")
    provider_event = relationship("PaymentProviderEvent", back_populates="reversals")
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
    billing_account_ledger_entry = relationship(
        "BillingAccountLedgerEntry", foreign_keys=[billing_account_ledger_entry_id]
    )
    credit_consumption_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[credit_consumption_ledger_entry_id]
    )
    consolidated_allocation_evidence = relationship(
        "ConsolidatedPaymentReturnAllocationEvidence",
        back_populates="reversal",
        foreign_keys="ConsolidatedPaymentReturnAllocationEvidence.reversal_id",
    )
    consolidated_reconciliation_evidence = relationship(
        "ConsolidatedPaymentReturnReconciliationEvidence",
        back_populates="reversal",
        foreign_keys="ConsolidatedPaymentReturnReconciliationEvidence.reversal_id",
        uselist=False,
    )


class ConsolidatedPaymentReturnAllocationEvidence(Base):
    """Exact subscriber-ledger result for one returned consolidated allocation."""

    __tablename__ = "consolidated_payment_return_allocation_evidence"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN refund_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN reversal_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_consolidated_return_evidence_exactly_one_owner",
        ),
        UniqueConstraint(
            "refund_id",
            "payment_allocation_id",
            name="uq_consolidated_refund_allocation_evidence",
        ),
        UniqueConstraint(
            "reversal_id",
            "payment_allocation_id",
            name="uq_consolidated_reversal_allocation_evidence",
        ),
        Index(
            "uq_consolidated_return_allocation_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    refund_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_refunds.id", ondelete="RESTRICT")
    )
    reversal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_reversals.id", ondelete="RESTRICT")
    )
    payment_allocation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_allocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    refund = relationship(
        "PaymentRefund",
        back_populates="consolidated_allocation_evidence",
        foreign_keys=[refund_id],
    )
    reversal = relationship(
        "PaymentReversal",
        back_populates="consolidated_allocation_evidence",
        foreign_keys=[reversal_id],
    )
    payment_allocation = relationship("PaymentAllocation")
    ledger_entry = relationship("LedgerEntry")


class ConsolidatedPaymentReturnReconciliationEvidence(Base):
    """Reviewed structural evidence for one historical consolidated return."""

    __tablename__ = "consolidated_payment_return_reconciliation_evidence"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN refund_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN reversal_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_consolidated_return_recon_exactly_one_owner",
        ),
        UniqueConstraint("refund_id", name="uq_consolidated_return_recon_refund"),
        UniqueConstraint("reversal_id", name="uq_consolidated_return_recon_reversal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    refund_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_refunds.id", ondelete="RESTRICT")
    )
    reversal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_reversals.id", ondelete="RESTRICT")
    )
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    refund = relationship(
        "PaymentRefund",
        back_populates="consolidated_reconciliation_evidence",
        foreign_keys=[refund_id],
    )
    reversal = relationship(
        "PaymentReversal",
        back_populates="consolidated_reconciliation_evidence",
        foreign_keys=[reversal_id],
    )
    document_reconstruction_evidence = relationship(
        "ConsolidatedPaymentReturnDocumentReconstructionEvidence",
        back_populates="reconciliation_evidence",
        uselist=False,
    )


class ConsolidatedPaymentReturnDocumentReconstructionEvidence(Base):
    """Reviewed source reference for one reconstructed historical return."""

    __tablename__ = "consolidated_payment_return_document_reconstruction_evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reconciliation_evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "consolidated_payment_return_reconciliation_evidence.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )
    historical_payment_state: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    reconciliation_evidence = relationship(
        "ConsolidatedPaymentReturnReconciliationEvidence",
        back_populates="document_reconstruction_evidence",
    )


class PaymentSettlement(Base):
    """Confirmed cash evidence and its exact unallocated ledger result."""

    __tablename__ = "payment_settlements"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_settlements_amount_positive"),
        CheckConstraint(
            "unallocated_amount >= 0",
            name="ck_payment_settlements_unallocated_nonnegative",
        ),
        Index("uq_payment_settlements_payment_id", "payment_id", unique=True),
        Index(
            "uq_payment_settlements_idempotency_key",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "uq_payment_settlements_unallocated_ledger_entry_id",
            "unallocated_ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_settlements_prepaid_ledger_entry_id",
            "prepaid_ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_settlements_billing_account_ledger_entry_id",
            "billing_account_ledger_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unallocated_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    prepaid_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
    )
    billing_account_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_account_ledger_entries.id", ondelete="RESTRICT"),
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unallocated_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    prepaid_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    origin: Mapped[PaymentSettlementOrigin] = mapped_column(
        Enum(PaymentSettlementOrigin), nullable=False
    )
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    payment = relationship("Payment", back_populates="settlement")
    unallocated_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[unallocated_ledger_entry_id]
    )
    prepaid_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[prepaid_ledger_entry_id]
    )
    billing_account_ledger_entry = relationship(
        "BillingAccountLedgerEntry",
        foreign_keys=[billing_account_ledger_entry_id],
    )
    consolidated_reconciliation_evidence = relationship(
        "ConsolidatedPaymentSettlementReconciliationEvidence",
        back_populates="settlement",
        uselist=False,
    )


class PaymentAllocationReconciliationException(Base):
    """Durable evidence that settled money could not reach its target invoice."""

    __tablename__ = "payment_allocation_reconciliation_exceptions"
    __table_args__ = (
        Index(
            "uq_payment_allocation_reconciliation_exceptions_key",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_payment_allocation_reconciliation_exceptions_status_created",
            "status",
            "created_at",
        ),
        Index(
            "ix_payment_allocation_reconciliation_exceptions_payment",
            "payment_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    topup_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topup_intents.id", ondelete="RESTRICT"),
    )
    provider_reference: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", server_default="open"
    )
    error_type: Mapped[str] = mapped_column(String(120), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    payment = relationship("Payment")
    invoice = relationship("Invoice")
    topup_intent = relationship("TopupIntent")


class ConsolidatedPaymentSettlementReconciliationEvidence(Base):
    """Reviewed provenance for one historical consolidated settlement."""

    __tablename__ = "consolidated_payment_settlement_reconciliation_evidence"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN provider_event_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN payment_proof_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN topup_intent_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_consolidated_settle_recon_exactly_one_provenance",
        ),
        UniqueConstraint(
            "settlement_id", name="uq_consolidated_settle_recon_settlement"
        ),
        UniqueConstraint(
            "provider_event_id", name="uq_consolidated_settle_recon_provider_event"
        ),
        UniqueConstraint(
            "payment_proof_id", name="uq_consolidated_settle_recon_payment_proof"
        ),
        UniqueConstraint(
            "topup_intent_id", name="uq_consolidated_settle_recon_topup_intent"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    settlement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_settlements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_provider_events.id", ondelete="RESTRICT"),
    )
    payment_proof_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_proofs.id", ondelete="RESTRICT"),
    )
    topup_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("topup_intents.id", ondelete="RESTRICT"),
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    settlement = relationship(
        "PaymentSettlement", back_populates="consolidated_reconciliation_evidence"
    )
    provider_event = relationship("PaymentProviderEvent")
    payment_proof = relationship("PaymentProof")
    topup_intent = relationship("TopupIntent")


class TopupIntent(Base):
    __tablename__ = "topup_intents"
    __table_args__ = (
        Index("ix_topup_intents_account_id", "account_id"),
        Index("ix_topup_intents_invoice_id", "invoice_id"),
        Index("uq_topup_intents_reference", "reference", unique=True),
        Index(
            "uq_topup_intents_deposit_idempotency",
            "account_id",
            "purpose",
            "idempotency_key",
            unique=True,
            postgresql_where=text(
                "purpose = 'account_credit_deposit' AND idempotency_key IS NOT NULL"
            ),
            sqlite_where=text(
                "purpose = 'account_credit_deposit' AND idempotency_key IS NOT NULL"
            ),
        ),
        CheckConstraint(
            "purpose IS NULL OR ("
            "purpose = 'account_credit_deposit' AND "
            "allocation_policy = 'credit_only' AND "
            "credit_application_policy = 'pay_eligible_invoices' AND "
            "policy_version = 1 AND preview_fingerprint IS NOT NULL AND "
            "idempotency_key IS NOT NULL AND channel IS NOT NULL)",
            name="ck_topup_intents_account_credit_contract",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT")
    )
    completed_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_providers.id")
    )
    capability_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_capability_bindings.id")
    )
    purpose: Mapped[str | None] = mapped_column(String(40))
    allocation_policy: Mapped[str | None] = mapped_column(String(40))
    credit_application_policy: Mapped[str | None] = mapped_column(String(40))
    policy_version: Mapped[int | None] = mapped_column(Integer)
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    channel: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[str | None] = mapped_column(String(120))
    reference: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(40), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    requested_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    actual_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    external_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    billing_account = relationship("BillingAccount")
    invoice = relationship("Invoice")
    completed_payment = relationship("Payment", back_populates="topup_intents")
    provider = relationship("PaymentProvider")


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint(
            "payment_id", "invoice_id", name="uq_payment_allocations_payment_invoice"
        ),
        # The unique constraint's index is payment_id-leading; this backs
        # invoice_id-leading lookups (allocations for an invoice).
        Index("ix_payment_allocations_invoice_id", "invoice_id"),
        Index(
            "ix_payment_allocations_invoice_active_payment",
            "invoice_id",
            "is_active",
            "payment_id",
        ),
        Index(
            "uq_payment_allocations_ledger_entry_id",
            "ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_allocations_consumption_ledger_entry_id",
            "consumption_ledger_entry_id",
            unique=True,
        ),
        Index(
            "uq_payment_allocations_idempotency_key",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_entries.id", ondelete="RESTRICT")
    )
    consumption_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_entries.id", ondelete="RESTRICT")
    )
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    payment = relationship("Payment", back_populates="allocations")
    invoice = relationship("Invoice", back_populates="payment_allocations")
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
    consumption_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[consumption_ledger_entry_id]
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        # One reversal per entry, enforced by the database rather than by caller
        # discipline. NULLs are distinct in a unique index on both Postgres and
        # SQLite, so ordinary (non-reversal) entries are unconstrained while every
        # non-null reversal_of_entry_id is unique.
        #
        # Deliberately NOT scoped to is_active: a reversal that was later
        # deactivated must still block a second reversal of the same entry, or
        # deactivating the reversal would silently re-open the double-post.
        Index(
            "uq_ledger_entries_reversal_of",
            "reversal_of_entry_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        Enum(LedgerEntryType), nullable=False
    )
    source: Mapped[LedgerSource] = mapped_column(Enum(LedgerSource))
    category: Mapped[LedgerCategory | None] = mapped_column(
        Enum(LedgerCategory, name="ledgercategory", create_constraint=False),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # True only when this row is itself a customer-position event. Some ledger
    # rows are immutable structural evidence for a document-owned movement or
    # a cutover/correction position carried by the reviewed opening baseline.
    # The explicit flag prevents those rows from being counted twice and keeps
    # money semantics independent of free-text memo labels.
    #
    # This is orthogonal to ``is_active``: that flag follows the lifecycle of
    # the source artifact (for example, a later-voided payment or invoice).
    # Never infer either flag from the other or rewrite this flag merely because
    # an entry became inactive.
    affects_customer_position: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # The entry this one reverses. The ledger is append-only: an entry's effect is
    # undone by posting its opposite, and this is the structural link between the
    # two. ``uq_ledger_entries_reversal_of`` makes a second reversal of the same
    # entry impossible at the database level, so the invariant does not depend on
    # every future caller remembering to take the row lock first.
    #
    # NULL for ordinary entries and for pre-existing reversals, which were only
    # ever linked by memo text. Those are deliberately NOT backfilled here — the
    # pairing is inferred, and inferring it wrong would corrupt money. The service
    # keeps the legacy memo lookup so an un-backfilled reversal still blocks a
    # re-reversal.
    reversal_of_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # The real-world date the entry represents (invoice issue / payment / txn
    # date). NULL for native entries and any row the cutover backfill could not
    # resolve; display/order should COALESCE(effective_date, created_at). The
    # migrated ledger lost original dates — created_at is the import instant.
    effective_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    invoice = relationship("Invoice", back_populates="ledger_entries")
    payment = relationship("Payment", back_populates="ledger_entries")


class AccountAdjustment(Base):
    """Evidence for one previewed debit against prepaid account funding.

    The linked ledger entry owns the customer-position effect. This row owns
    the business decision, stale-confirmation fingerprint, idempotency, and the
    exact link needed to audit or reverse that decision without guessing from a
    memo or amount.
    """

    __tablename__ = "account_adjustments"
    __table_args__ = (
        UniqueConstraint(
            "origin",
            "idempotency_key",
            name="uq_account_adjustments_origin_idempotency",
        ),
        UniqueConstraint(
            "origin",
            "reversal_idempotency_key",
            name="uq_account_adjustments_origin_reversal_idempotency",
        ),
        CheckConstraint("amount > 0", name="ck_account_adjustments_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    category: Mapped[LedgerCategory] = mapped_column(
        Enum(LedgerCategory, name="ledgercategory", create_constraint=False),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")
    memo: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(40), nullable=False)
    origin_ref: Mapped[str | None] = mapped_column(String(160))
    prepaid_funding_before: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    prepaid_funding_after: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    postpaid_receivables: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    collection_blocking_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    access_consequence: Mapped[str] = mapped_column(
        String(80), nullable=False, default="none_adjustment_only"
    )
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    ledger_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    reversal_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    reversal_preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    reversal_idempotency_key: Mapped[str | None] = mapped_column(String(120))
    reversal_reason: Mapped[str | None] = mapped_column(Text)
    reversal_prepaid_funding_before: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2)
    )
    reversal_prepaid_funding_after: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2)
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    account = relationship("Subscriber")
    ledger_entry = relationship("LedgerEntry", foreign_keys=[ledger_entry_id])
    reversal_ledger_entry = relationship(
        "LedgerEntry", foreign_keys=[reversal_ledger_entry_id]
    )
    subscription_add_on = relationship(
        "SubscriptionAddOn", back_populates="account_adjustment", uselist=False
    )


class ServiceEntitlement(Base):
    """Exact paid or explicitly granted service period.

    Money remains represented by invoices, payments, allocations, and ledger
    entries. This row is the prepaid access proof created only after a service
    period has been funded or approved through a non-cash billing treatment.
    """

    __tablename__ = "service_entitlements"
    __table_args__ = (
        Index(
            "ix_service_entitlements_account_subscription_period",
            "account_id",
            "subscription_id",
            "starts_at",
            "ends_at",
        ),
        Index(
            "uq_service_entitlements_active_invoice_line",
            "source_invoice_line_id",
            unique=True,
            postgresql_where=text(
                "status = 'active' AND source_invoice_line_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'active' AND source_invoice_line_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_service_entitlements_active_ledger_entry",
            "source_ledger_entry_id",
            unique=True,
            postgresql_where=text(
                "status = 'active' AND source_ledger_entry_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'active' AND source_ledger_entry_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_service_entitlements_active_billing_grant",
            "source_billing_grant_id",
            unique=True,
            postgresql_where=text(
                "status = 'active' AND source_billing_grant_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'active' AND source_billing_grant_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    source_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    source_invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_lines.id")
    )
    source_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_entries.id")
    )
    source_billing_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_billing_grants.id", ondelete="RESTRICT"),
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_funded: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[ServiceEntitlementStatus] = mapped_column(
        Enum(ServiceEntitlementStatus), default=ServiceEntitlementStatus.active
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    subscription = relationship("Subscription")
    source_invoice = relationship("Invoice")
    source_invoice_line = relationship("InvoiceLine")
    source_ledger_entry = relationship("LedgerEntry")
    source_billing_grant = relationship(
        "SubscriptionBillingGrant", back_populates="entitlement"
    )


class TaxRate(Base):
    __tablename__ = "tax_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.0000"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class PaymentProvider(Base):
    __tablename__ = "payment_providers"
    __table_args__ = (UniqueConstraint("name", name="uq_payment_providers_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_type: Mapped[PaymentProviderType] = mapped_column(
        Enum(PaymentProviderType), default=PaymentProviderType.custom
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    payments = relationship("Payment", back_populates="provider")
    events = relationship("PaymentProviderEvent", back_populates="provider")


class CollectionAccount(Base):
    __tablename__ = "collection_accounts"
    __table_args__ = (
        UniqueConstraint("name", name="uq_collection_accounts_name"),
        Index(
            "uq_collection_accounts_bank_number_currency",
            "bank_name",
            "account_number",
            "currency",
            unique=True,
            postgresql_where=text(
                "bank_name IS NOT NULL AND account_number IS NOT NULL"
            ),
            sqlite_where=text("bank_name IS NOT NULL AND account_number IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    account_type: Mapped[CollectionAccountType] = mapped_column(
        Enum(CollectionAccountType), default=CollectionAccountType.bank
    )
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_last4: Mapped[str | None] = mapped_column(String(4))
    # Full payment details. Present so this table can be the single owner of a
    # Dotmac receiving account: `account_last4` alone identifies an account for
    # reconciliation but cannot tell a customer where to pay, which is why those
    # details previously lived in a settings blob. These are the company's own
    # published receiving accounts — never customer payment credentials.
    account_number: Mapped[str | None] = mapped_column(String(64))
    account_name: Mapped[str | None] = mapped_column(String(200))
    sort_code: Mapped[str | None] = mapped_column(String(32))
    # External accounting-system mapping (QuickBooks/Xero/Sage GL code). Sub
    # carries the mapping only; it does not model a chart of accounts.
    accounting_code: Mapped[str | None] = mapped_column(String(64))
    # Explicit customer-presentment order. Higher values win; new accounts
    # default to zero so creating one cannot silently replace the invoice bank
    # account selected during migration.
    presentment_priority: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    payments = relationship("Payment", back_populates="collection_account")
    channel_mappings = relationship(
        "PaymentChannelAccount", back_populates="collection_account"
    )


class PaymentChannel(Base):
    __tablename__ = "payment_channels"
    __table_args__ = (UniqueConstraint("name", name="uq_payment_channels_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    channel_type: Mapped[PaymentChannelType] = mapped_column(
        Enum(PaymentChannelType), default=PaymentChannelType.other
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_providers.id")
    )
    fee_rules: Mapped[dict | None] = mapped_column(JSON)
    # External accounting-system mapping code; see CollectionAccount.accounting_code.
    accounting_code: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    provider = relationship("PaymentProvider")
    channel_accounts = relationship("PaymentChannelAccount", back_populates="channel")
    payments = relationship("Payment", back_populates="payment_channel")
    payment_methods = relationship("PaymentMethod", back_populates="payment_channel")


class PaymentChannelAccount(Base):
    __tablename__ = "payment_channel_accounts"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "collection_account_id",
            "currency",
            name="uq_payment_channel_accounts_channel_account_currency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id"), nullable=False
    )
    collection_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection_accounts.id"), nullable=False
    )
    currency: Mapped[str | None] = mapped_column(String(3))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    channel = relationship("PaymentChannel", back_populates="channel_accounts")
    collection_account = relationship(
        "CollectionAccount", back_populates="channel_mappings"
    )


class PaymentProviderEvent(Base):
    __tablename__ = "payment_provider_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "idempotency_key",
            name="uq_payment_provider_events_idempotency",
        ),
        Index(
            "uq_payment_provider_events_external_id",
            "provider_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_providers.id"), nullable=False
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(160))
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    source: Mapped[PaymentProviderEventSource] = mapped_column(
        Enum(PaymentProviderEventSource),
        default=PaymentProviderEventSource.legacy_unknown,
        nullable=False,
    )
    observation_digest: Mapped[str | None] = mapped_column(String(64))
    observed_payment_status: Mapped[PaymentStatus | None] = mapped_column(
        Enum(PaymentStatus)
    )
    # Normalized monetary observation supplied by the verified provider adapter.
    # Refund execution uses this value instead of interpreting raw provider JSON.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    provider_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    provider_reference: Mapped[str | None] = mapped_column(String(120))
    currency: Mapped[str | None] = mapped_column(String(3))
    financial_effect: Mapped[PaymentProviderEventFinancialEffect] = mapped_column(
        Enum(PaymentProviderEventFinancialEffect),
        default=PaymentProviderEventFinancialEffect.none,
    )
    status: Mapped[PaymentProviderEventStatus] = mapped_column(
        Enum(PaymentProviderEventStatus), default=PaymentProviderEventStatus.pending
    )
    payload: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    provider = relationship("PaymentProvider", back_populates="events")
    payment = relationship("Payment", back_populates="provider_events")
    invoice = relationship("Invoice")
    refunds = relationship("PaymentRefund", back_populates="provider_event")
    reversals = relationship("PaymentReversal", back_populates="provider_event")


class BillingRun(Base):
    __tablename__ = "billing_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    billing_cycle: Mapped[str | None] = mapped_column(String(40))
    launch_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="scheduled"
    )
    requested_by: Mapped[str | None] = mapped_column(String(120))
    preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_runs.id", ondelete="SET NULL"),
    )
    status: Mapped[BillingRunStatus] = mapped_column(
        Enum(BillingRunStatus), default=BillingRunStatus.running
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscriptions_scanned: Mapped[int] = mapped_column(Integer, default=0)
    subscriptions_billed: Mapped[int] = mapped_column(Integer, default=0)
    invoices_created: Mapped[int] = mapped_column(Integer, default=0)
    lines_created: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class BankReconciliationRun(Base):
    __tablename__ = "bank_reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    date_range: Mapped[str | None] = mapped_column(String(20))
    handler: Mapped[str | None] = mapped_column(String(120))
    statement_rows: Mapped[int] = mapped_column(Integer, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_rows: Mapped[int] = mapped_column(Integer, default=0)
    system_payment_count: Mapped[int] = mapped_column(Integer, default=0)
    statement_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00")
    )
    payment_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00")
    )
    difference_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    items = relationship("BankReconciliationItem", back_populates="run")


class BankReconciliationItem(Base):
    __tablename__ = "bank_reconciliation_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_reconciliation_runs.id"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(String(20), default="unmatched")
    reference: Mapped[str | None] = mapped_column(String(255))
    file_name: Mapped[str | None] = mapped_column(String(255))
    count: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    run = relationship("BankReconciliationRun", back_populates="items")
