import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIELD_FIBER_TEST_TYPES = (
    "otdr",
    "optical_power",
    "insertion_loss",
    "reflectance",
    "continuity",
    "other",
)


class FieldFiberTestResult(Base):
    __tablename__ = "field_fiber_test_results"
    __table_args__ = (
        Index(
            "ix_field_fiber_tests_mirror_created", "work_order_mirror_id", "created_at"
        ),
        Index("ix_field_fiber_tests_asset", "asset_type", "asset_id"),
        Index("ix_field_fiber_tests_client_ref", "client_ref", unique=True),
        CheckConstraint(
            "test_type IN ('otdr', 'optical_power', 'insertion_loss', 'reflectance', 'continuity', 'other')",
            name="ck_field_fiber_tests_test_type",
        ),
        CheckConstraint(
            "derived_verdict IS NULL OR derived_verdict IN "
            "('within_threshold', 'exceeds_threshold', 'no_measurement', "
            "'no_policy')",
            name="ck_field_fiber_tests_derived_verdict",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_mirror_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_type: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    test_type: Mapped[str] = mapped_column(String(40), nullable=False)
    wavelength_nm: Mapped[int | None] = mapped_column(Integer)
    value_db: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(16))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    instrument: Mapped[str | None] = mapped_column(String(120))
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    measured_by_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    measured_by_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    measured_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id"),
    )
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    # Derived acceptance snapshot (network.fiber_test_acceptance policy).
    # The technician's ``passed`` assertion above is never altered; the policy
    # verdict is recorded beside it with its version and applied bounds so a
    # later threshold change cannot silently rewrite history.
    derived_passed: Mapped[bool | None] = mapped_column(Boolean)
    derived_verdict: Mapped[str | None] = mapped_column(String(40))
    applied_minimum_db: Mapped[float | None] = mapped_column(Float)
    applied_maximum_db: Mapped[float | None] = mapped_column(Float)
    acceptance_policy_version: Mapped[int | None] = mapped_column(Integer)
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    work_order = relationship("WorkOrder")

    @property
    def crm_work_order_id(self) -> str:
        """Public work-order identity projected for transport reads."""
        return self.work_order.public_id if self.work_order else ""

    @property
    def assertion_conflict(self) -> bool:
        """The technician's assertion disagrees with the derived verdict."""
        return (
            self.passed is not None
            and self.derived_passed is not None
            and self.passed != self.derived_passed
        )

    technician = relationship("TechnicianProfile")
