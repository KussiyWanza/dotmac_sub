"""Structural billing contract, cadence, and obligation identity (ADR 0007).

These records give every accepted commercial commitment and later billable
service change one versioned contract and one finite, uniquely identified
obligation per period. They replace the current split where billing mode,
cadence, and price are duplicated across account, subscription, and catalog
rows and where Sale-to-Money is joined through metadata.

ADR 0007 Phase 1 writes these rows in shadow beside existing invoice and
renewal behavior. ``authority`` records that explicitly: a shadow row is
migration evidence and must produce no financial effect. Only a passed cutover
gate promotes rows to ``authoritative``.

PostgreSQL-only guarantees (temporal exclusion on effective versions) are added
by the migration; ``__table_args__`` here stays portable so the SQLite test
harness can create the same tables.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
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
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class BillingRecordAuthority(enum.Enum):
    """Whether a row may drive real financial effect.

    ADR 0007 migration phases expand and shadow before they cut over. A
    ``shadow`` row is reviewable evidence only; nothing may read it as money.
    """

    shadow = "shadow"
    authoritative = "authoritative"


class BillingContractSourceKind(enum.Enum):
    """Structural origin of a contract version. Never a metadata string."""

    sales_order_line = "sales_order_line"
    plan_change = "plan_change"
    renewal = "renewal"
    staff_correction = "staff_correction"
    migration_backfill = "migration_backfill"


class BillingContractVersionStatus(enum.Enum):
    """Lifecycle of one immutable set of contracted terms."""

    draft = "draft"
    effective = "effective"
    superseded = "superseded"
    canceled = "canceled"


class RateBasis(enum.Enum):
    """How the contracted price is measured before cadence is applied."""

    fixed_per_service_period = "fixed_per_service_period"
    per_rate_unit = "per_rate_unit"
    per_quantity = "per_quantity"
    usage_metered = "usage_metered"


class IntervalUnit(enum.Enum):
    """Calendar unit. Month and year are calendar terms, never day counts."""

    day = "day"
    week = "week"
    month = "month"
    year = "year"


class CollectionTiming(enum.Enum):
    """Billing mode expressed as collection timing on the contract."""

    advance = "advance"
    arrears = "arrears"


class CadenceAlignment(enum.Enum):
    """How a period boundary is placed against the calendar."""

    contract_anniversary = "contract_anniversary"
    calendar_period_start = "calendar_period_start"
    fixed_anchor_day = "fixed_anchor_day"


class EndOfMonthRule(enum.Enum):
    """Declared rule for a monthly anniversary on the 29th, 30th, or 31st."""

    clamp_to_month_end = "clamp_to_month_end"
    strict_same_day_or_skip = "strict_same_day_or_skip"


class ProrationPolicy(enum.Enum):
    """Declared proration; never chosen implicitly."""

    none = "none"
    full_period = "full_period"
    actual_calendar_days = "actual_calendar_days"
    actual_elapsed_time = "actual_elapsed_time"


class ChargeComponent(enum.Enum):
    """The billable component a contract line and obligation represent."""

    recurring_service = "recurring_service"
    installation = "installation"
    activation = "activation"
    addon = "addon"
    equipment = "equipment"
    usage = "usage"
    other = "other"


class AccountingTreatment(enum.Enum):
    """Explicit accounting meaning. Never a negative metadata classifier."""

    receivable = "receivable"
    prepaid_consumption = "prepaid_consumption"
    non_cash_grant = "non_cash_grant"


class ObligationState(enum.Enum):
    """Finite obligation lifecycle (ADR 0007 section 4)."""

    scheduled = "scheduled"
    open = "open"
    partially_resolved = "partially_resolved"
    resolved = "resolved"
    canceled = "canceled"
    written_off = "written_off"


class ObligationResolutionKind(enum.Enum):
    """Why an obligation reached a terminal state. Never inferred."""

    settlement = "settlement"
    credit = "credit"
    prepaid_consumption = "prepaid_consumption"
    grant = "grant"
    waiver = "waiver"
    write_off = "write_off"
    pre_earning_cancellation = "pre_earning_cancellation"
    reversal = "reversal"


# One shared type object per PostgreSQL enum. Several columns reuse the same
# enum (three interval units on a version, authority on three tables); sharing
# the instance keeps DDL to a single CREATE TYPE per name.
_authority_enum = Enum(BillingRecordAuthority, name="billingrecordauthority")
_source_kind_enum = Enum(BillingContractSourceKind, name="billingcontractsourcekind")
_version_status_enum = Enum(
    BillingContractVersionStatus, name="billingcontractversionstatus"
)
_rate_basis_enum = Enum(RateBasis, name="ratebasis")
_interval_unit_enum = Enum(IntervalUnit, name="intervalunit")
_collection_timing_enum = Enum(CollectionTiming, name="collectiontiming")
_alignment_enum = Enum(CadenceAlignment, name="cadencealignment")
_end_of_month_enum = Enum(EndOfMonthRule, name="endofmonthrule")
_proration_enum = Enum(ProrationPolicy, name="prorationpolicy")
_charge_component_enum = Enum(ChargeComponent, name="chargecomponent")
_accounting_treatment_enum = Enum(AccountingTreatment, name="accountingtreatment")
_obligation_state_enum = Enum(ObligationState, name="obligationstate")
_obligation_resolution_enum = Enum(
    ObligationResolutionKind, name="obligationresolutionkind"
)


class BillingContract(Base):
    """Customer-specific billing agreement for one subscription.

    The contract is the stable identity; terms live on versions. It is not the
    mutable current catalog offer.
    """

    __tablename__ = "billing_contracts"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            name="uq_billing_contract_subscription",
        ),
        Index("ix_billing_contract_account", "account_id"),
        Index("ix_billing_contract_authority", "authority"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authority: Mapped[BillingRecordAuthority] = mapped_column(
        _authority_enum,
        nullable=False,
        default=BillingRecordAuthority.shadow,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    versions: Mapped[list[BillingContractVersion]] = relationship(
        "BillingContractVersion",
        back_populates="contract",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BillingContractVersion(Base):
    """One immutable set of contracted billing terms over a half-open interval.

    Historical terms are never rewritten when catalog prices or policies
    change; a change supersedes the version and creates the next one.
    """

    __tablename__ = "billing_contract_versions"
    __table_args__ = (
        UniqueConstraint(
            "contract_id",
            "version",
            name="uq_billing_contract_version_number",
        ),
        # A contract has at most one open-ended effective version. The
        # predicate is declared for both dialects so the SQLite test harness
        # enforces the same rule; full temporal non-overlap across closed
        # intervals is enforced by a PostgreSQL exclusion constraint added in
        # the migration.
        Index(
            "uq_billing_contract_version_current",
            "contract_id",
            unique=True,
            postgresql_where=text("status = 'effective' AND ends_at IS NULL"),
            sqlite_where=text("status = 'effective' AND ends_at IS NULL"),
        ),
        Index("ix_billing_contract_version_source", "source_kind", "source_id"),
        Index("ix_billing_contract_version_effective", "contract_id", "starts_at"),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_billing_contract_version_interval",
        ),
        CheckConstraint(
            "contracted_price >= 0",
            name="ck_billing_contract_version_price_sign",
        ),
        CheckConstraint(
            "service_interval_count > 0 AND invoice_interval_count > 0",
            name="ck_billing_contract_version_interval_counts",
        ),
        CheckConstraint(
            "rate_quantity > 0",
            name="ck_billing_contract_version_rate_quantity",
        ),
        CheckConstraint(
            "anchor_day IS NULL OR (anchor_day >= 1 AND anchor_day <= 31)",
            name="ck_billing_contract_version_anchor_day",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_contracts.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[BillingContractVersionStatus] = mapped_column(
        _version_status_enum,
        nullable=False,
        default=BillingContractVersionStatus.draft,
    )
    authority: Mapped[BillingRecordAuthority] = mapped_column(
        _authority_enum,
        nullable=False,
        default=BillingRecordAuthority.shadow,
    )

    # Denormalised identity so an obligation can be scoped without a join.
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Structural source. ADR 0007 invariant 4: never a metadata join.
    source_kind: Mapped[BillingContractSourceKind] = mapped_column(
        _source_kind_enum,
        nullable=False,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Effective half-open interval [starts_at, ends_at).
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Contracted money.
    contracted_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Composable cadence (ADR 0007 section 4). Rate unit and invoice interval
    # are independent typed facts, so a daily rate can aggregate monthly.
    rate_basis: Mapped[RateBasis] = mapped_column(_rate_basis_enum, nullable=False)
    rate_unit: Mapped[IntervalUnit] = mapped_column(_interval_unit_enum, nullable=False)
    rate_quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("1")
    )
    service_interval_unit: Mapped[IntervalUnit] = mapped_column(
        _interval_unit_enum, nullable=False
    )
    service_interval_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    invoice_interval_unit: Mapped[IntervalUnit] = mapped_column(
        _interval_unit_enum, nullable=False
    )
    invoice_interval_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    collection_timing: Mapped[CollectionTiming] = mapped_column(
        _collection_timing_enum, nullable=False
    )
    alignment: Mapped[CadenceAlignment] = mapped_column(
        _alignment_enum,
        nullable=False,
        default=CadenceAlignment.contract_anniversary,
    )
    anchor_day: Mapped[int | None] = mapped_column(Integer)
    end_of_month_rule: Mapped[EndOfMonthRule] = mapped_column(
        _end_of_month_enum,
        nullable=False,
        default=EndOfMonthRule.clamp_to_month_end,
    )
    timezone_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Africa/Lagos"
    )
    proration_policy: Mapped[ProrationPolicy] = mapped_column(
        _proration_enum,
        nullable=False,
        default=ProrationPolicy.none,
    )

    # Terms and tax inputs consumed by rating and documents.
    payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tax_treatment_code: Mapped[str | None] = mapped_column(String(60))
    tax_inclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discount_code: Mapped[str | None] = mapped_column(String(60))
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))

    # Supersession and command evidence.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_contract_versions.id", ondelete="RESTRICT"),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    contract: Mapped[BillingContract] = relationship(
        "BillingContract", back_populates="versions"
    )
    lines: Mapped[list[BillingContractLine]] = relationship(
        "BillingContractLine",
        back_populates="contract_version",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BillingContractLine(Base):
    """One charge component carried by a contract version."""

    __tablename__ = "billing_contract_lines"
    __table_args__ = (
        UniqueConstraint(
            "contract_version_id",
            "charge_component",
            "component_key",
            name="uq_billing_contract_line_component",
        ),
        Index("ix_billing_contract_line_version", "contract_version_id"),
        CheckConstraint(
            "unit_price >= 0 AND quantity > 0",
            name="ck_billing_contract_line_amounts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contract_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_contract_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Stable identity of the line across versions, so an obligation keeps one
    # lineage when terms are superseded.
    contract_line_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    charge_component: Mapped[ChargeComponent] = mapped_column(
        _charge_component_enum, nullable=False
    )
    # Distinguishes two lines of the same component (e.g. two addons).
    component_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("1")
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    accounting_treatment: Mapped[AccountingTreatment] = mapped_column(
        _accounting_treatment_enum, nullable=False
    )
    # A finite line funds one order; a recurring line continues on the
    # subscription and must never inflate the original order's funding gate.
    is_finite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tax_treatment_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    contract_version: Mapped[BillingContractVersion] = relationship(
        "BillingContractVersion", back_populates="lines"
    )


class BillingObligation(Base):
    """The finite billable unit for one line, component, period, and currency.

    Natural identity plus database uniqueness prevents duplicate obligations
    under replay or concurrency. An obligation is not an invoice, a payment, or
    an entitlement.
    """

    __tablename__ = "billing_obligations"
    __table_args__ = (
        UniqueConstraint(
            "contract_line_key",
            "contract_version_id",
            "charge_component",
            "source_kind",
            "source_id",
            "source_version",
            "period_start",
            "period_end",
            "currency",
            name="uq_billing_obligation_natural_identity",
        ),
        Index("ix_billing_obligation_contract", "contract_id", "period_start"),
        Index("ix_billing_obligation_account_state", "account_id", "state"),
        Index("ix_billing_obligation_subscription", "subscription_id", "period_start"),
        Index("ix_billing_obligation_authority", "authority"),
        CheckConstraint(
            "period_end > period_start",
            name="ck_billing_obligation_period",
        ),
        CheckConstraint(
            "net_amount >= 0 AND tax_amount >= 0 AND gross_amount >= 0",
            name="ck_billing_obligation_amount_sign",
        ),
        CheckConstraint(
            "resolved_amount >= 0 AND resolved_amount <= gross_amount",
            name="ck_billing_obligation_resolved_bound",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_contracts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    contract_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_contract_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    contract_line_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authority: Mapped[BillingRecordAuthority] = mapped_column(
        _authority_enum,
        nullable=False,
        default=BillingRecordAuthority.shadow,
    )

    charge_component: Mapped[ChargeComponent] = mapped_column(
        _charge_component_enum, nullable=False
    )
    source_kind: Mapped[BillingContractSourceKind] = mapped_column(
        _source_kind_enum,
        nullable=False,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Service/earning interval, half-open.
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0")
    )
    gross_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0")
    )
    resolved_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0")
    )
    accounting_treatment: Mapped[AccountingTreatment] = mapped_column(
        _accounting_treatment_enum, nullable=False
    )
    collection_timing: Mapped[CollectionTiming] = mapped_column(
        _collection_timing_enum, nullable=False
    )
    is_finite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    state: Mapped[ObligationState] = mapped_column(
        _obligation_state_enum,
        nullable=False,
        default=ObligationState.scheduled,
    )
    resolution_kind: Mapped[ObligationResolutionKind | None] = mapped_column(
        _obligation_resolution_enum
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Corrections link to the exact superseded obligation; history is never
    # edited in place.
    corrects_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_obligations.id", ondelete="RESTRICT")
    )
    reversed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_obligations.id", ondelete="RESTRICT")
    )

    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
