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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db import Base


class AuthProvider(enum.Enum):
    # Legacy persisted provider vocabulary; compatibility-only during R1.
    # New mechanism membership is declared by its SOT owner through
    # authentication_mechanism_registry. In particular, retaining historical
    # ``sso`` here does not declare or implement SSO. Keep these as comments:
    # an enum docstring becomes an OpenAPI schema description and would turn
    # this additive persistence change into an unintended public API change.
    local = "local"
    sso = "sso"
    radius = "radius"


class MFAMethodType(enum.Enum):
    totp = "totp"
    sms = "sms"
    email = "email"


class SessionStatus(enum.Enum):
    active = "active"
    revoked = "revoked"
    expired = "expired"


class AuthenticationBinding(Base):
    """One installed, configured way of proving you are a Party.

    ADR-0019 (starter): a credential authenticates a Party and may repeat only
    per authentication *mechanism* — never per principal kind, portal, account
    or membership. The discriminator is the **binding**, not the mechanism code:
    two OIDC issuers or two RADIUS verifiers are two bindings of one code, and a
    code-keyed constraint would forbid a party holding a credential against each.

    ``mechanism_code`` is a plain string on purpose (ADR-0008) — the vocabulary
    is open and declared, so a deployment names a mechanism without a migration.
    The typed contract lives behind the binding in the owning provider module,
    which is why there is no ``radius_server_id`` here.
    """

    __tablename__ = "authentication_bindings"
    __table_args__ = (
        UniqueConstraint("binding_key", name="uq_auth_bindings_binding_key"),
        CheckConstraint(
            "length(trim(binding_key)) > 0",
            name="ck_auth_bindings_binding_key_nonempty",
        ),
        CheckConstraint(
            "length(trim(mechanism_code)) > 0", name="ck_auth_bindings_code_nonempty"
        ),
        Index("ix_auth_bindings_code_active", "mechanism_code", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    binding_key: Mapped[str] = mapped_column(String(80), nullable=False)
    mechanism_code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class AuthenticationBindingIdentityError(ValueError):
    """An installed binding's immutable identity was changed."""


@event.listens_for(AuthenticationBinding, "before_update")
def _reject_authentication_binding_identity_change(
    _mapper: object, _connection: object, target: AuthenticationBinding
) -> None:
    state = inspect(target)
    changed = [
        field
        for field in ("binding_key", "mechanism_code")
        if getattr(state.attrs, field).history.has_changes()
    ]
    if changed:
        raise AuthenticationBindingIdentityError(
            "An installed authentication binding's binding_key and "
            "mechanism_code are immutable; install a new binding instead."
        )


class UserCredential(Base):
    __tablename__ = "user_credentials"
    __table_args__ = (
        CheckConstraint(
            "(provider != 'local') OR (username IS NOT NULL AND password_hash IS NOT NULL)",
            name="ck_user_credentials_local_requires_username_password",
        ),
        # Migration 525. Tenant, Party, verifier binding and evidence are one
        # projection: all present or all absent.
        CheckConstraint(
            "(party_id IS NULL AND authentication_binding_id IS NULL AND "
            "tenant_id IS NULL AND party_bound_at IS NULL AND "
            "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
            "(party_id IS NOT NULL AND authentication_binding_id IS NOT NULL AND "
            "tenant_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
            "party_binding_source IS NOT NULL AND "
            "party_binding_reason IS NOT NULL AND "
            "length(trim(party_binding_source)) > 0 AND "
            "length(trim(party_binding_reason)) > 0)",
            name="ck_user_credentials_party_binding_projection",
        ),
        UniqueConstraint(
            "tenant_id",
            "party_id",
            "authentication_binding_id",
            name="uq_user_credentials_tenant_party_auth_binding",
        ),
        CheckConstraint(
            # Exactly one principal: subscriber (customer), system_user (admin),
            # or reseller_user (Layer 3 — reseller portal login as its own
            # identity, not a fake subscriber). CASE-sum form works on both
            # Postgres and the SQLite test DB.
            "(CASE WHEN subscriber_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN system_user_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN reseller_user_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_user_credentials_exactly_one_principal",
        ),
        Index(
            "ix_user_credentials_local_username_unique",
            "username",
            unique=True,
            postgresql_where=text("provider = 'local'"),
            sqlite_where=text("provider = 'local'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=True
    )
    reseller_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reseller_users.id"), nullable=True
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    provider: Mapped[AuthProvider] = mapped_column(
        Enum(AuthProvider), default=AuthProvider.local, nullable=False
    )
    username: Mapped[str | None] = mapped_column(String(150))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    radius_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("radius_servers.id")
    )
    # Migration 525 — additive Party/binding/tenant columns. Nothing reads
    # through them yet; the legacy principal FKs above stay authoritative until
    # a separate reader cutover (R2). All nullable, and they stay nullable
    # until R3.
    party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id", ondelete="RESTRICT")
    )
    party_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    party_binding_source: Mapped[str | None] = mapped_column(String(80))
    party_binding_reason: Mapped[str | None] = mapped_column(Text)
    authentication_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("authentication_bindings.id", ondelete="RESTRICT"),
    )
    # The deployed schema has a RESTRICT FK to kernel-owned ``tenants``. The
    # kernel maps that table on a separate declarative base, so repeating the FK
    # here would make Sub's metadata unable to resolve its own graph in the
    # SQLite unit lane. Migration 525 remains the schema authority for the FK.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")
    reseller_user = relationship("ResellerUser")
    radius_server = relationship("RadiusServer")
    party = relationship("Party", foreign_keys=[party_id])
    authentication_binding = relationship("AuthenticationBinding")


class MFAMethod(Base):
    __tablename__ = "mfa_methods"
    __table_args__ = (
        Index(
            "ix_mfa_methods_primary_per_subscriber",
            "subscriber_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
        Index(
            "ix_mfa_methods_primary_per_system_user",
            "system_user_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
        Index(
            "ix_mfa_methods_primary_per_reseller_user",
            "reseller_user_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
        CheckConstraint(
            "(CASE WHEN subscriber_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN system_user_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN reseller_user_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_mfa_methods_exactly_one_principal",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=True
    )
    reseller_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reseller_users.id"), nullable=True
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    method_type: Mapped[MFAMethodType] = mapped_column(
        Enum(MFAMethodType), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(120))
    secret: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")
    reseller_user = relationship("ResellerUser")


class MFARecoveryCode(Base):
    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (
        Index("ix_mfa_recovery_codes_method_active", "mfa_method_id", "is_active"),
        Index("ux_mfa_recovery_codes_code_hash", "code_hash", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mfa_method_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mfa_methods.id"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    mfa_method = relationship("MFAMethod")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN subscriber_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN system_user_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN reseller_user_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_sessions_exactly_one_principal",
        ),
        Index("ux_sessions_token_hash", "token_hash", unique=True),
        Index(
            "ux_sessions_previous_token_hash",
            "previous_token_hash",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id"), nullable=True
    )
    reseller_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reseller_users.id"), nullable=True
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID] = synonym("subscriber_id")
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus), default=SessionStatus.active, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_token_hash: Mapped[str | None] = mapped_column(String(255))
    token_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stable per-install identifier sent by native clients (X-Device-Id). Lets a
    # re-login on the same device supersede that device's prior session instead
    # of stacking a new row each time.
    device_id: Mapped[str | None] = mapped_column(String(64), index=True)

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    # Backwards-compatible alias used by older code/tests.
    person_id: Mapped[uuid.UUID | None] = synonym("subscriber_id")
    label: Mapped[str | None] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Permission keys this key may exercise (wildcard-aware, like role grants).
    # Empty = fail-closed: the key authenticates identity but carries no access.
    scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscriber = relationship("Subscriber")
    system_user = relationship("SystemUser")
