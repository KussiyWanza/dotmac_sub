import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.catalog import DunningAction
from app.models.enforcement_lock import AccessRestrictionMode, EnforcementReason


class DunningCaseStatus(enum.Enum):
    open = "open"
    paused = "paused"
    resolved = "resolved"
    closed = "closed"


class FinancialAccessAction(enum.Enum):
    suspend = "suspend"
    reject = "reject"
    throttle = "throttle"
    restore = "restore"


class FinancialAccessOrigin(enum.Enum):
    dunning = "dunning"
    prepaid_enforcement = "prepaid_enforcement"
    financial_reconciliation = "financial_reconciliation"
    historical_reconciliation = "historical_reconciliation"


class FinancialAccessEvidenceOperation(enum.Enum):
    lock_created = "lock_created"
    lock_resolved = "lock_resolved"
    credential_throttled = "credential_throttled"
    credential_restored = "credential_restored"
    dunning_case_resolved = "dunning_case_resolved"


class DunningCase(Base):
    __tablename__ = "dunning_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        "subscriber_id",
        UUID(as_uuid=True),
        ForeignKey("subscribers.id"),
        nullable=False,
    )
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_sets.id")
    )
    status: Mapped[DunningCaseStatus] = mapped_column(
        Enum(DunningCaseStatus), default=DunningCaseStatus.open
    )
    current_step: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
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

    subscriber = relationship(
        "Subscriber", back_populates="dunning_cases", foreign_keys=[account_id]
    )
    policy_set = relationship("PolicySet")
    actions = relationship("DunningActionLog", back_populates="case")


class DunningActionLog(Base):
    __tablename__ = "dunning_action_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dunning_cases.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    access_consequence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("financial_access_consequences.id", ondelete="RESTRICT"),
        unique=True,
    )
    step_day: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[DunningAction] = mapped_column(Enum(DunningAction), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    case = relationship("DunningCase", back_populates="actions")
    invoice = relationship("Invoice", back_populates="dunning_actions")
    payment = relationship("Payment", back_populates="dunning_actions")
    access_consequence = relationship(
        "FinancialAccessConsequence",
        back_populates="dunning_action_log",
        foreign_keys=[access_consequence_id],
    )


class FinancialAccessConsequence(Base):
    """Durable owner decision for a financial service-access consequence."""

    __tablename__ = "financial_access_consequences"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_financial_access_consequence_idempotency"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        "subscriber_id",
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dunning_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dunning_cases.id", ondelete="RESTRICT"),
    )
    action: Mapped[FinancialAccessAction] = mapped_column(
        Enum(FinancialAccessAction), nullable=False
    )
    requested_reason: Mapped[EnforcementReason | None] = mapped_column(
        Enum(EnforcementReason, name="enforcementreason", create_constraint=False)
    )
    access_mode: Mapped[AccessRestrictionMode | None] = mapped_column(
        Enum(
            AccessRestrictionMode,
            name="accessrestrictionmode",
            create_constraint=False,
        )
    )
    origin: Mapped[FinancialAccessOrigin] = mapped_column(
        Enum(FinancialAccessOrigin), nullable=False
    )
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(120), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    decision_inputs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    subscriber = relationship("Subscriber")
    dunning_case = relationship("DunningCase")
    dunning_action_log = relationship(
        "DunningActionLog",
        back_populates="access_consequence",
        uselist=False,
        foreign_keys="DunningActionLog.access_consequence_id",
    )
    evidence = relationship(
        "FinancialAccessConsequenceEvidence",
        back_populates="consequence",
        cascade="all, delete-orphan",
    )


class FinancialAccessConsequenceEvidence(Base):
    """Structural link from a financial decision to its exact consequence."""

    __tablename__ = "financial_access_consequence_evidence"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN enforcement_lock_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN access_credential_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN dunning_case_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_financial_access_evidence_exactly_one_target",
        ),
        UniqueConstraint(
            "consequence_id",
            "enforcement_lock_id",
            "operation",
            name="uq_financial_access_evidence_lock_operation",
        ),
        UniqueConstraint(
            "consequence_id",
            "access_credential_id",
            "operation",
            name="uq_financial_access_evidence_credential_operation",
        ),
        UniqueConstraint(
            "consequence_id",
            "dunning_case_id",
            "operation",
            name="uq_financial_access_evidence_case_operation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    consequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("financial_access_consequences.id", ondelete="RESTRICT"),
        nullable=False,
    )
    enforcement_lock_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("enforcement_locks.id", ondelete="RESTRICT"),
    )
    access_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_credentials.id", ondelete="RESTRICT"),
    )
    dunning_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dunning_cases.id", ondelete="RESTRICT"),
    )
    operation: Mapped[FinancialAccessEvidenceOperation] = mapped_column(
        Enum(FinancialAccessEvidenceOperation), nullable=False
    )
    profile_before_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id", ondelete="RESTRICT")
    )
    profile_after_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_profiles.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    consequence = relationship("FinancialAccessConsequence", back_populates="evidence")
    enforcement_lock = relationship("EnforcementLock")
    access_credential = relationship("AccessCredential")
    dunning_case = relationship("DunningCase", foreign_keys=[dunning_case_id])


class PrepaidSweepCycleState(Base):
    """Keyset cursor for the bounded prepaid sweep's coverage cycle.

    One row per runner. The sweep processes accounts in stable key order and
    checkpoints the last processed key, so every account is visited exactly
    once per cycle regardless of how many bounded runs a cycle takes.
    TRANSITIONAL: retired with prepaid_balance_sweep at the ADR 0007 durable
    timer cutover.
    """

    __tablename__ = "prepaid_sweep_cycle_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    runner: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    cursor_key: Mapped[str | None] = mapped_column(String(64))
    cycle_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    cycles_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
