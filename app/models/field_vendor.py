import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


# Roles inside one contractor organisation. Stored as a plain string like
# ``WorkOrder.status`` rather than a PG enum, so the vocabulary can evolve
# without a type migration; ``app.services.field.vendor_capabilities`` is the
# declaring owner and resolves each role to its capability set.
VENDOR_USER_ROLES = ("owner", "supervisor", "field")
DEFAULT_VENDOR_USER_ROLE = "field"


class FieldVendor(Base):
    __tablename__ = "field_vendors"
    __table_args__ = (
        CheckConstraint(
            "(party_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_field_vendors_party_binding_evidence",
        ),
        UniqueConstraint("party_id", name="uq_field_vendors_party_id"),
        UniqueConstraint("code", name="uq_field_vendors_code"),
        UniqueConstraint("crm_vendor_id", name="uq_field_vendors_crm_vendor_id"),
        Index("ix_field_vendors_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    crm_vendor_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60))
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    service_area: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    users = relationship("FieldVendorUser", back_populates="vendor")
    party = relationship("Party", back_populates="field_vendor_profile")


class FieldVendorUser(Base):
    """SystemUser projected into one vendor organization context."""

    __tablename__ = "field_vendor_users"
    __table_args__ = (
        UniqueConstraint(
            "vendor_id",
            "system_user_id",
            name="uq_field_vendor_users_vendor_system_user",
        ),
        UniqueConstraint(
            "crm_vendor_user_id", name="uq_field_vendor_users_crm_vendor_user_id"
        ),
        CheckConstraint(
            "(party_membership_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_membership_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_field_vendor_users_party_binding_evidence",
        ),
        UniqueConstraint(
            "party_membership_id",
            name="uq_field_vendor_users_party_membership_id",
        ),
        Index("ix_field_vendor_users_system_user_id", "system_user_id"),
        Index("ix_field_vendor_users_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("party_memberships.id", ondelete="RESTRICT"),
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    crm_vendor_user_id: Mapped[str | None] = mapped_column(String(64))
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_vendors.id", ondelete="CASCADE"),
        nullable=False,
    )
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    vendor = relationship("FieldVendor", back_populates="users")
    system_user = relationship("SystemUser")
    party_membership = relationship("PartyMembership")


class FieldVendorDeviceToken(Base):
    __tablename__ = "field_vendor_device_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_field_vendor_device_tokens_token"),
        Index("ix_field_vendor_device_tokens_vendor_user_id", "vendor_user_id"),
        Index("ix_field_vendor_device_tokens_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_vendor_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(16))
    app_version: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    vendor_user = relationship("FieldVendorUser")

    @property
    def subscriber_id(self):
        return None

    @property
    def system_user_id(self):
        return None
