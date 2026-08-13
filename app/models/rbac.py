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
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db import Base


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("name", name="uq_roles_name"),
        # Migration 528. The kernel identity is `(tenant_id, slug)`; both halves
        # are present or both absent, because a role identified in only one half
        # is unaddressable rather than half-adopted.
        CheckConstraint(
            "(tenant_id IS NULL AND slug IS NULL) OR "
            "(tenant_id IS NOT NULL AND slug IS NOT NULL AND "
            "length(trim(slug)) > 0 AND lower(slug) = slug)",
            name="ck_roles_kernel_identity_projection",
        ),
        UniqueConstraint("tenant_id", "slug", name="uq_roles_tenant_slug"),
        # Kernel a42 requires this parent key for tenant-safe composite FKs from
        # PartyRoleGrant. Keep it nullable through R1; PostgreSQL permits the
        # legacy `(NULL, id)` population while still exposing the exact target
        # key the later lineage adoption needs.
        UniqueConstraint("tenant_id", "id", name="uq_roles_tenant_id_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Kernel a42 permits 120 characters. The catalog owner deliberately keeps
    # its established 80-character command policy during R1; widening storage
    # here makes the hosted table structurally compatible without changing
    # product-facing authorization identity.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Migration 528, nullable through R1. No model-level ForeignKey: `tenants`
    # is declared in the kernel's MetaData rather than `app.db.Base`, so the
    # string target would not resolve here. The FK is created by the migration —
    # the same split 527 uses for `user_credentials.tenant_id`.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    #: The kernel's role key, 63 chars against Sub's established 80-character
    #: role-name command limit. Derived and written only by `auth.rbac_catalog`,
    #: alongside `name` and never instead of it.
    slug: Mapped[str | None] = mapped_column(String(63))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )

    permissions = relationship("RolePermission", back_populates="role")
    members = relationship("SubscriberRole", back_populates="role")


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("key", name="uq_permissions_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_ui_assignable: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    roles = relationship("RolePermission", back_populates="permission")


Index(
    "uq_roles_normalized_name",
    func.lower(func.trim(Role.name)),
    unique=True,
)
Index(
    "uq_permissions_normalized_key",
    func.lower(func.trim(Permission.key)),
    unique=True,
)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "role_id", "permission_id", name="uq_role_permissions_role_permission"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id"), nullable=False
    )

    role = relationship("Role", back_populates="permissions")
    permission = relationship("Permission", back_populates="roles")


class SubscriberRole(Base):
    __tablename__ = "subscriber_roles"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id",
            "role_id",
            "scope_type",
            "scope_id",
            name="uq_subscriber_roles_subscriber_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False
    )
    # Object scope of this grant. Empty strings mean a GLOBAL grant (the
    # historical, unscoped behaviour). A scoped grant restricts the role's
    # permissions to resources in one region/reseller: scope_type in
    # {"region", "reseller"} and scope_id the region/reseller id.
    scope_type: Mapped[str] = mapped_column(String(20), default="", server_default="")
    scope_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    role = relationship("Role", back_populates="members")


# Backwards-compatible alias: "person roles" are subscriber roles in this codebase.
PersonRole = SubscriberRole


class SubscriberPermission(Base):
    """Direct permission grants to individual subscribers, bypassing role-based assignment."""

    __tablename__ = "subscriber_permissions"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id",
            "permission_id",
            name="uq_subscriber_permissions_subscriber_permission",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    granted_by_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )

    permission = relationship("Permission")


class SystemUserRole(Base):
    __tablename__ = "system_user_roles"
    __table_args__ = (
        UniqueConstraint(
            "system_user_id",
            "role_id",
            "scope_type",
            "scope_id",
            name="uq_system_user_roles_user_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False
    )
    # See SubscriberRole.scope_type / scope_id. Empty = global grant.
    scope_type: Mapped[str] = mapped_column(String(20), default="", server_default="")
    scope_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="local", server_default="local"
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    role = relationship("Role")


class SystemUserPermission(Base):
    __tablename__ = "system_user_permissions"
    __table_args__ = (
        UniqueConstraint(
            "system_user_id",
            "permission_id",
            name="uq_system_user_permissions_user_permission",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=False
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    granted_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=True
    )

    permission = relationship("Permission")
