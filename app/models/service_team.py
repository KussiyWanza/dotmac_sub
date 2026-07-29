import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ServiceTeamType(enum.Enum):
    operations = "operations"
    billing = "billing"
    support = "support"
    field_service = "field_service"
    project_management = "project_management"


class ServiceTeamMemberRole(enum.Enum):
    """Legacy migration shadow; responsibilities are authoritative after cutover."""

    member = "member"
    lead = "lead"
    manager = "manager"


class ServiceTeamCapabilityKey(str, enum.Enum):
    """Governed vocabulary for capabilities consumed by application code."""

    customer_support = "customer_support"
    billing_operations = "billing_operations"
    field_service = "field_service"
    project_delivery = "project_delivery"
    network_operations = "network_operations"
    outage_response = "outage_response"


class ServiceTeamResponsibilityKey(str, enum.Enum):
    accountable_manager = "accountable_manager"
    queue_lead = "queue_lead"
    agent = "agent"
    dispatcher = "dispatcher"
    on_call = "on_call"


class ServiceTeamRelationshipKind(str, enum.Enum):
    parent_child = "parent_child"
    escalation = "escalation"
    collaboration = "collaboration"


class ServiceTeamScopeType(str, enum.Enum):
    global_ = "global"
    geo_area = "geo_area"


class ServiceTeam(Base):
    __tablename__ = "service_teams"
    __table_args__ = (
        Index("ux_service_teams_name_ci", text("lower(name)"), unique=True),
        UniqueConstraint(
            "workforce_system",
            "workforce_department_reference",
            name="uq_service_teams_workforce_system_reference",
        ),
        CheckConstraint(
            "(workforce_system IS NULL) = (workforce_department_reference IS NULL)",
            name="ck_service_teams_workforce_reference_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Legacy shadow input retained until composable-authority verification.
    team_type: Mapped[str | None] = mapped_column(String(40))
    region: Mapped[str | None] = mapped_column(String(80))
    manager_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "parties.id",
            name="fk_service_teams_manager_person_id_parties",
            ondelete="RESTRICT",
        ),
    )
    workforce_system: Mapped[str | None] = mapped_column(String(40))
    workforce_department_reference: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    members = relationship(
        "ServiceTeamMember", back_populates="team", cascade="all, delete-orphan"
    )
    capabilities = relationship(
        "ServiceTeamCapability", back_populates="team", cascade="all, delete-orphan"
    )
    scope_bindings = relationship(
        "ServiceTeamScopeBinding", back_populates="team", cascade="all, delete-orphan"
    )
    external_references = relationship(
        "ServiceTeamExternalReference",
        back_populates="team",
        cascade="all, delete-orphan",
    )


class ServiceTeamMember(Base):
    __tablename__ = "service_team_members"
    __table_args__ = (
        UniqueConstraint("team_id", "person_id", name="uq_service_team_member"),
        Index("ix_service_team_members_person_id", "person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id"), nullable=False
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "parties.id",
            name="fk_service_team_members_person_id_parties",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    # Legacy shadow input retained until responsibility verification.
    role: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    team = relationship("ServiceTeam", back_populates="members")
    responsibilities = relationship(
        "ServiceTeamMemberResponsibility",
        back_populates="membership",
        cascade="all, delete-orphan",
    )


class ServiceTeamCapabilityDefinition(Base):
    """Registered capability contract; rows are vocabulary, never team seed data."""

    __tablename__ = "service_team_capability_definitions"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    contract_owner: Mapped[str] = mapped_column(String(160), nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ServiceTeamCapability(Base):
    __tablename__ = "service_team_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "capability_key",
            name="uq_service_team_capability",
        ),
        Index(
            "ix_service_team_capabilities_key_active",
            "capability_key",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    capability_key: Mapped[str] = mapped_column(
        String(80),
        ForeignKey(
            "service_team_capability_definitions.key",
            name="fk_service_team_capabilities_definition",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    team = relationship("ServiceTeam", back_populates="capabilities")
    definition = relationship("ServiceTeamCapabilityDefinition")


class ServiceTeamMemberResponsibility(Base):
    __tablename__ = "service_team_member_responsibilities"
    __table_args__ = (
        UniqueConstraint(
            "membership_id",
            "responsibility_key",
            name="uq_service_team_member_responsibility",
        ),
        Index(
            "ix_service_team_responsibilities_key_active",
            "responsibility_key",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_team_members.id", ondelete="CASCADE"),
        nullable=False,
    )
    responsibility_key: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    membership = relationship("ServiceTeamMember", back_populates="responsibilities")


class ServiceTeamRelationship(Base):
    __tablename__ = "service_team_relationships"
    __table_args__ = (
        UniqueConstraint(
            "parent_team_id",
            "child_team_id",
            "relationship_kind",
            name="uq_service_team_relationship",
        ),
        CheckConstraint(
            "parent_team_id <> child_team_id",
            name="ck_service_team_relationship_not_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    child_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    relationship_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    parent_team = relationship("ServiceTeam", foreign_keys=[parent_team_id])
    child_team = relationship("ServiceTeam", foreign_keys=[child_team_id])


class ServiceTeamScopeBinding(Base):
    """Typed team scope. New scope kinds add typed columns and a stricter check."""

    __tablename__ = "service_team_scope_bindings"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "scope_type",
            "geo_area_id",
            name="uq_service_team_scope_binding",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND geo_area_id IS NULL) OR "
            "(scope_type = 'geo_area' AND geo_area_id IS NOT NULL)",
            name="ck_service_team_scope_binding_target",
        ),
        Index(
            "ix_service_team_scope_geo_area_active",
            "geo_area_id",
            "is_active",
        ),
        Index(
            "ux_service_team_global_scope",
            "team_id",
            unique=True,
            postgresql_where=text("scope_type = 'global' AND is_active IS TRUE"),
            sqlite_where=text("scope_type = 'global' AND is_active IS TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_type: Mapped[str] = mapped_column(String(40), nullable=False)
    geo_area_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("geo_areas.id", ondelete="RESTRICT"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    team = relationship("ServiceTeam", back_populates="scope_bindings")
    geo_area = relationship("GeoArea")


class ServiceTeamExternalReference(Base):
    """Provider-neutral external observation; never a team identity or decision."""

    __tablename__ = "service_team_external_references"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "account_scope",
            "external_id",
            name="uq_service_team_external_provider_reference",
        ),
        UniqueConstraint(
            "team_id",
            "provider",
            "account_scope",
            "external_id",
            name="uq_service_team_external_team_reference",
        ),
        Index("ix_service_team_external_team_active", "team_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provenance: Mapped[str] = mapped_column(String(160), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    team = relationship("ServiceTeam", back_populates="external_references")


class ServiceTeamRoutingPolicy(Base):
    """A domain-owned route key selects an exact team; capability is eligibility."""

    __tablename__ = "service_team_routing_policies"
    __table_args__ = (
        UniqueConstraint(
            "domain",
            "route_key",
            "scope_binding_id",
            "priority",
            name="uq_service_team_routing_policy",
        ),
        Index(
            "ix_service_team_routing_domain_key_active",
            "domain",
            "route_key",
            "is_active",
        ),
        Index(
            "ux_service_team_unscoped_route_priority",
            "domain",
            "route_key",
            "priority",
            unique=True,
            postgresql_where=text("scope_binding_id IS NULL AND is_active IS TRUE"),
            sqlite_where=text("scope_binding_id IS NULL AND is_active IS TRUE"),
        ),
        CheckConstraint("priority >= 0", name="ck_service_team_routing_priority"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    domain: Mapped[str] = mapped_column(String(80), nullable=False)
    route_key: Mapped[str] = mapped_column(String(120), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_team_scope_bindings.id", ondelete="RESTRICT"),
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    team = relationship("ServiceTeam")
    scope_binding = relationship("ServiceTeamScopeBinding")
