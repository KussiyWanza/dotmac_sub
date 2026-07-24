"""Bank-transfer payment proofs.

Customers (or their reseller) pay by direct bank transfer and upload the
receipt; staff verify and the amount is credited to the account as a real
Payment (method=transfer) — optionally auto-allocated to the oldest open
invoices — through the same billing rails as every other payment.
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PaymentProofStatus(enum.Enum):
    submitted = "submitted"
    verified = "verified"
    rejected = "rejected"


class WithholdingTaxStatus(enum.Enum):
    pending = "pending"  # awaiting the reseller's WHT certificate
    certified = "certified"  # certificate received
    reclaimed = "reclaimed"  # offset/reclaimed from the tax authority
    written_off = "written_off"


class PaymentProof(Base):
    __tablename__ = "payment_proofs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # A proof targets EITHER a subscriber account (customer / reseller-for-one-
    # account transfer) OR a reseller's consolidated billing account (bulk
    # reseller transfer). Exactly one is set, so account_id is now nullable.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True, index=True
    )
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_accounts.id"),
        nullable=True,
        index=True,
    )
    # Who uploaded it — the customer themselves or a reseller user.
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    # ``amount`` is the net cash actually transferred (what's on the receipt).
    # For a reseller bulk transfer with withholding tax, ``gross_amount`` is the
    # billed value to credit and ``wht_amount`` the tax withheld at source
    # (gross = net + wht). For ordinary transfers gross/wht stay null.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    wht_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    wht_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # Amount the reviewer actually confirmed against the bank statement; the
    # Payment is created for this value (defaults to the claimed amount).
    verified_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    bank_name: Mapped[str | None] = mapped_column(String(120))
    reference: Mapped[str | None] = mapped_column(String(160))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[PaymentProofStatus] = mapped_column(
        Enum(PaymentProofStatus),
        default=PaymentProofStatus.submitted,
        nullable=False,
        index=True,
    )
    review_notes: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[str | None] = mapped_column(String(120))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber", foreign_keys=[account_id])
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    payment = relationship("Payment")


class WithholdingTaxRecord(Base):
    """A withholding-tax receivable raised when a reseller pays net of WHT.

    The reseller transfers cash net of tax; on verification the billing account
    is credited the full ``gross_amount`` and this row tracks the ``wht_amount``
    the company expects to reclaim once the reseller provides a WHT certificate.
    """

    __tablename__ = "withholding_tax_records"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN account_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN billing_account_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_withholding_tax_records_exactly_one_target",
        ),
        Index(
            "uq_withholding_tax_records_payment_id",
            "payment_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id"),
        nullable=True,
        index=True,
    )
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_accounts.id"),
        nullable=True,
        index=True,
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id"), nullable=True, index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True
    )
    payment_proof_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_proofs.id"), nullable=True
    )
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    wht_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    wht_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    vat_exclusive_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    vat_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    source_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True, index=True
    )
    policy_version: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[WithholdingTaxStatus] = mapped_column(
        Enum(WithholdingTaxStatus),
        default=WithholdingTaxStatus.pending,
        nullable=False,
        index=True,
    )
    certificate_path: Mapped[str | None] = mapped_column(String(500))
    certificate_reference: Mapped[str | None] = mapped_column(String(160))
    certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber", foreign_keys=[account_id])
    billing_account = relationship("BillingAccount")
    reseller = relationship("Reseller")
    source_invoice = relationship("Invoice")
    payment = relationship("Payment", back_populates="withholding_tax_record")
    payment_proof = relationship("PaymentProof")
    transitions = relationship(
        "WithholdingTaxTransition",
        back_populates="record",
        order_by="WithholdingTaxTransition.occurred_at",
    )


class WithholdingTaxTransition(Base):
    """Append-only official timeline for a WHT receivable lifecycle."""

    __tablename__ = "withholding_tax_transitions"
    __table_args__ = (
        CheckConstraint(
            "from_status IS NULL OR from_status <> to_status",
            name="ck_withholding_tax_transitions_status_change",
        ),
        Index(
            "ix_withholding_tax_transitions_record_occurred",
            "record_id",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("withholding_tax_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[WithholdingTaxStatus | None] = mapped_column(
        Enum(WithholdingTaxStatus, name="withholdingtaxstatus"),
        nullable=True,
    )
    to_status: Mapped[WithholdingTaxStatus] = mapped_column(
        Enum(WithholdingTaxStatus, name="withholdingtaxstatus"),
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(String(120))
    certificate_reference: Mapped[str | None] = mapped_column(String(160))
    notes: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    record = relationship("WithholdingTaxRecord", back_populates="transitions")


class WithholdingTaxTransitionImmutableError(RuntimeError):
    pass


@event.listens_for(WithholdingTaxTransition, "before_update")
def _reject_wht_transition_update(*_args: object) -> None:
    raise WithholdingTaxTransitionImmutableError(
        "WHT transition history is append-only"
    )


@event.listens_for(WithholdingTaxTransition, "before_delete")
def _reject_wht_transition_delete(*_args: object) -> None:
    raise WithholdingTaxTransitionImmutableError(
        "WHT transition history is append-only"
    )
