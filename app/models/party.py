"""Canonical person/organization identity and multi-role relationship facts.

This is the additive foundation for ``docs/PARTY_ROLE_RELATIONSHIP_SOT.md``.
Existing reseller, vendor, organization, and authentication models are
intentionally not cut over in this slice.  Subscriber accounts have an
additive nullable ``party_id`` binding, but no existing read path or population
is cut over merely because that link exists.

The important boundary is semantic:

* a party is a real-world person or organization;
* a role describes how that party relates to Dotmac;
* a relationship describes how two parties relate to one another and never
  grants access by itself;
* a membership carries an explicit organization context and access scope;
* a contact point is evidence about how to reach a party, not identity proof on
  its own.
"""

from __future__ import annotations

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


class PartyType(enum.StrEnum):
    person = "person"
    organization = "organization"


class PartyIdentityStatus(enum.StrEnum):
    active = "active"
    quarantined = "quarantined"
    merged = "merged"
    archived = "archived"


class PartyDataClassification(enum.StrEnum):
    production = "production"
    test = "test"
    imported_unverified = "imported_unverified"


class PartyRoleType(enum.StrEnum):
    prospect = "prospect"
    customer = "customer"
    subscriber = "subscriber"
    reseller = "reseller"
    vendor = "vendor"
    partner = "partner"
    staff = "staff"
    agent = "agent"


class PartnerRoleKey(enum.StrEnum):
    referral = "referral"
    technology = "technology"
    infrastructure = "infrastructure"
    strategic = "strategic"


class PartyRoleStatus(enum.StrEnum):
    pending = "pending"
    active = "active"
    suspended = "suspended"
    ended = "ended"


class PartyRelationshipType(enum.StrEnum):
    contact_for = "contact_for"
    billing_contact_for = "billing_contact_for"
    technical_contact_for = "technical_contact_for"
    emergency_contact_for = "emergency_contact_for"
    employee_of = "employee_of"
    owner_of = "owner_of"
    director_of = "director_of"
    agent_for = "agent_for"
    account_manager_for = "account_manager_for"
    referred_by = "referred_by"
    parent_of = "parent_of"
    manages = "manages"


class PartyRelationshipStatus(enum.StrEnum):
    pending = "pending"
    active = "active"
    inactive = "inactive"
    ended = "ended"


class PartyMembershipType(enum.StrEnum):
    owner = "owner"
    admin = "admin"
    member = "member"
    employee = "employee"
    agent = "agent"
    reseller_admin = "reseller_admin"
    vendor_user = "vendor_user"


class PartyMembershipStatus(enum.StrEnum):
    invited = "invited"
    active = "active"
    suspended = "suspended"
    ended = "ended"


class PartyContactPointType(enum.StrEnum):
    email = "email"
    phone = "phone"
    sms = "sms"
    whatsapp = "whatsapp"
    facebook_messenger = "facebook_messenger"
    instagram_dm = "instagram_dm"
    telegram = "telegram"
    linkedin = "linkedin"
    x = "x"


class PartyContactVerificationStatus(enum.StrEnum):
    unverified = "unverified"
    pending = "pending"
    verified = "verified"
    failed = "failed"


class PartyContactConsentStatus(enum.StrEnum):
    unknown = "unknown"
    opted_in = "opted_in"
    opted_out = "opted_out"
    not_applicable = "not_applicable"


def _now() -> datetime:
    return datetime.now(UTC)


class Party(Base):
    """One native identity for one real-world person or organization."""

    __tablename__ = "parties"
    __table_args__ = (
        CheckConstraint(
            "party_type IN ('person', 'organization')",
            name="ck_parties_party_type",
        ),
        CheckConstraint(
            "status IN ('active', 'quarantined', 'merged', 'archived')",
            name="ck_parties_status",
        ),
        CheckConstraint(
            "data_classification IN ('production', 'test', 'imported_unverified')",
            name="ck_parties_data_classification",
        ),
        CheckConstraint(
            "merged_into_party_id IS NULL OR merged_into_party_id <> id",
            name="ck_parties_not_merged_into_self",
        ),
        CheckConstraint(
            "(status = 'merged' AND merged_into_party_id IS NOT NULL) OR "
            "(status <> 'merged' AND merged_into_party_id IS NULL)",
            name="ck_parties_merged_target_required",
        ),
        Index("ix_parties_type_status", "party_type", "status"),
        Index("ix_parties_classification", "data_classification", "status"),
        Index("ix_parties_merged_into", "merged_into_party_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_type: Mapped[str] = mapped_column(String(24), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=PartyIdentityStatus.active.value, nullable=False
    )
    data_classification: Mapped[str] = mapped_column(
        String(32),
        default=PartyDataClassification.production.value,
        nullable=False,
    )
    merged_into_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id")
    )
    merge_reason: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    merged_into = relationship(
        "Party", remote_side=[id], foreign_keys=[merged_into_party_id]
    )
    roles = relationship("PartyRole", back_populates="party")
    contact_points = relationship("PartyContactPoint", back_populates="party")
    external_references = relationship("PartyExternalReference", back_populates="party")
    organization_profile = relationship(
        "Organization", back_populates="party", uselist=False
    )
    reseller_profile = relationship("Reseller", back_populates="party", uselist=False)
    vendor_profile = relationship("Vendor", back_populates="party", uselist=False)
    field_vendor_profile = relationship(
        "FieldVendor", back_populates="party", uselist=False
    )
    system_user_profile = relationship(
        "SystemUser", back_populates="person_party", uselist=False
    )
    subscriber_accounts = relationship("Subscriber", back_populates="party")


class PartyRole(Base):
    """One independently managed business role held by a party.

    ``role_key`` is ``default`` for every role except ``partner``.  Partner
    roles use a controlled agreement type such as ``referral`` or
    ``technology``.  A reseller is therefore never stored as a generic partner
    alias.
    """

    __tablename__ = "party_roles"
    __table_args__ = (
        UniqueConstraint(
            "party_id", "role_type", "role_key", name="uq_party_roles_party_type_key"
        ),
        CheckConstraint(
            "role_type IN ('prospect', 'customer', 'subscriber', 'reseller', "
            "'vendor', 'partner', 'staff', 'agent')",
            name="ck_party_roles_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'suspended', 'ended')",
            name="ck_party_roles_status",
        ),
        CheckConstraint(
            "(role_type = 'partner' AND role_key IN "
            "('referral', 'technology', 'infrastructure', 'strategic')) OR "
            "(role_type <> 'partner' AND role_key = 'default')",
            name="ck_party_roles_key_contract",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_party_roles_valid_window",
        ),
        Index("ix_party_roles_type_status", "role_type", "status"),
        Index("ix_party_roles_party_status", "party_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False
    )
    role_type: Mapped[str] = mapped_column(String(32), nullable=False)
    role_key: Mapped[str] = mapped_column(String(40), default="default", nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=PartyRoleStatus.pending.value, nullable=False
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(80))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    party = relationship("Party", back_populates="roles")


class PartyRelationship(Base):
    """Directional relationship between parties; never an authorization grant."""

    __tablename__ = "party_relationships"
    __table_args__ = (
        UniqueConstraint(
            "subject_party_id",
            "object_party_id",
            "relationship_type",
            "relationship_key",
            name="uq_party_relationships_subject_object_type_key",
        ),
        CheckConstraint(
            "subject_party_id <> object_party_id",
            name="ck_party_relationships_not_self",
        ),
        CheckConstraint(
            "relationship_type IN ('contact_for', 'billing_contact_for', "
            "'technical_contact_for', 'emergency_contact_for', 'employee_of', "
            "'owner_of', 'director_of', 'agent_for', 'account_manager_for', "
            "'referred_by', 'parent_of', 'manages')",
            name="ck_party_relationships_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'inactive', 'ended')",
            name="ck_party_relationships_status",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_party_relationships_valid_window",
        ),
        Index(
            "ix_party_relationships_subject",
            "subject_party_id",
            "relationship_type",
            "status",
        ),
        Index(
            "ix_party_relationships_object",
            "object_party_id",
            "relationship_type",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False
    )
    object_party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(48), nullable=False)
    relationship_key: Mapped[str] = mapped_column(
        String(80), default="default", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=PartyRelationshipStatus.active.value, nullable=False
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(80))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    subject_party = relationship("Party", foreign_keys=[subject_party_id])
    object_party = relationship("Party", foreign_keys=[object_party_id])


class PartyMembership(Base):
    """A person's explicit organization context and bounded authority scope."""

    __tablename__ = "party_memberships"
    __table_args__ = (
        UniqueConstraint(
            "person_party_id",
            "organization_party_id",
            "membership_type",
            "membership_key",
            name="uq_party_memberships_person_org_type_key",
        ),
        CheckConstraint(
            "person_party_id <> organization_party_id",
            name="ck_party_memberships_not_self",
        ),
        CheckConstraint(
            "membership_type IN ('owner', 'admin', 'member', 'employee', 'agent', "
            "'reseller_admin', 'vendor_user')",
            name="ck_party_memberships_type",
        ),
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'ended')",
            name="ck_party_memberships_status",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_party_memberships_valid_window",
        ),
        Index(
            "ix_party_memberships_person",
            "person_party_id",
            "status",
        ),
        Index(
            "ix_party_memberships_organization",
            "organization_party_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False
    )
    organization_party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False
    )
    membership_type: Mapped[str] = mapped_column(String(40), nullable=False)
    membership_key: Mapped[str] = mapped_column(
        String(80), default="default", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default=PartyMembershipStatus.invited.value, nullable=False
    )
    access_scope: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON()))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(80))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    person_party = relationship("Party", foreign_keys=[person_party_id])
    organization_party = relationship("Party", foreign_keys=[organization_party_id])


class PartyContactPoint(Base):
    """Reachability evidence scoped to one party and provider/account context."""

    __tablename__ = "party_contact_points"
    __table_args__ = (
        UniqueConstraint(
            "party_id",
            "channel_type",
            "normalized_value",
            "scope_key",
            name="uq_party_contact_points_party_channel_value_scope",
        ),
        CheckConstraint(
            "channel_type IN ('email', 'phone', 'sms', 'whatsapp', "
            "'facebook_messenger', 'instagram_dm', 'telegram', 'linkedin', 'x')",
            name="ck_party_contact_points_channel_type",
        ),
        CheckConstraint(
            "verification_status IN ('unverified', 'pending', 'verified', 'failed')",
            name="ck_party_contact_points_verification",
        ),
        CheckConstraint(
            "consent_status IN ('unknown', 'opted_in', 'opted_out', 'not_applicable')",
            name="ck_party_contact_points_consent",
        ),
        Index(
            "ix_party_contact_points_lookup",
            "channel_type",
            "normalized_value",
            "is_active",
        ),
        Index(
            "uq_party_contact_points_primary",
            "party_id",
            "channel_type",
            "scope_key",
            unique=True,
            sqlite_where=text("is_primary IS TRUE AND is_active IS TRUE"),
            postgresql_where=text("is_primary IS TRUE AND is_active IS TRUE"),
        ),
        Index(
            "uq_party_contact_points_active_provider_identity",
            "channel_type",
            "provider",
            "provider_account_id",
            "external_subject_id",
            unique=True,
            sqlite_where=text(
                "is_active IS TRUE AND channel_type IN "
                "('facebook_messenger', 'instagram_dm') AND provider IS NOT NULL "
                "AND provider_account_id IS NOT NULL AND external_subject_id IS NOT NULL"
            ),
            postgresql_where=text(
                "is_active IS TRUE AND channel_type IN "
                "('facebook_messenger', 'instagram_dm') AND provider IS NOT NULL "
                "AND provider_account_id IS NOT NULL AND external_subject_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(320), nullable=False)
    display_value: Mapped[str | None] = mapped_column(String(320))
    scope_key: Mapped[str] = mapped_column(
        String(200), default="default", nullable=False
    )
    provider: Mapped[str | None] = mapped_column(String(80))
    provider_account_id: Mapped[str | None] = mapped_column(String(200))
    external_subject_id: Mapped[str | None] = mapped_column(String(200))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(24),
        default=PartyContactVerificationStatus.unverified.value,
        nullable=False,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_source: Mapped[str | None] = mapped_column(String(80))
    consent_status: Mapped[str] = mapped_column(
        String(24), default=PartyContactConsentStatus.unknown.value, nullable=False
    )
    consent_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    party = relationship("Party", back_populates="contact_points")


class SubscriberContactRelationshipProjection(Base):
    """Reviewed projection from one legacy contact row to a Party relationship."""

    __tablename__ = "subscriber_contact_relationship_projections"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_contact_id",
            "party_relationship_id",
            name="uq_subscriber_contact_relationship_projection",
        ),
        UniqueConstraint(
            "party_relationship_id",
            name="uq_subscriber_contact_relationship_party_relationship",
        ),
        CheckConstraint(
            "length(trim(binding_source)) > 0 AND length(trim(binding_reason)) > 0",
            name="ck_subscriber_contact_relationship_projection_evidence",
        ),
        Index(
            "ix_subscriber_contact_relationship_projection_contact",
            "subscriber_contact_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriber_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    party_relationship_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("party_relationships.id", ondelete="RESTRICT"),
        nullable=False,
    )
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    binding_source: Mapped[str] = mapped_column(String(80), nullable=False)
    binding_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    subscriber_contact = relationship(
        "SubscriberContact", back_populates="relationship_projections"
    )
    party_relationship = relationship("PartyRelationship")


class SubscriberContactPointProjection(Base):
    """Reviewed legacy source-field projection to a canonical contact point."""

    __tablename__ = "subscriber_contact_point_projections"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_contact_id",
            "source_field",
            name="uq_subscriber_contact_point_projection_source",
        ),
        UniqueConstraint(
            "subscriber_contact_id",
            "party_contact_point_id",
            name="uq_subscriber_contact_point_projection_point",
        ),
        CheckConstraint(
            "source_field IN ('email', 'phone', 'whatsapp', 'facebook', "
            "'instagram', 'x_handle', 'telegram', 'linkedin')",
            name="ck_subscriber_contact_point_projection_source_field",
        ),
        CheckConstraint(
            "length(trim(binding_source)) > 0 AND length(trim(binding_reason)) > 0",
            name="ck_subscriber_contact_point_projection_evidence",
        ),
        Index(
            "ix_subscriber_contact_point_projection_contact",
            "subscriber_contact_id",
        ),
        Index(
            "ix_subscriber_contact_point_projection_point",
            "party_contact_point_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriber_contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_field: Mapped[str] = mapped_column(String(32), nullable=False)
    party_contact_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("party_contact_points.id", ondelete="RESTRICT"),
        nullable=False,
    )
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    binding_source: Mapped[str] = mapped_column(String(80), nullable=False)
    binding_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    subscriber_contact = relationship(
        "SubscriberContact", back_populates="contact_point_projections"
    )
    party_contact_point = relationship("PartyContactPoint")


class PartyExternalReference(Base):
    """Non-authoritative external identifier retained for import provenance."""

    __tablename__ = "party_external_references"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "entity_type",
            "external_id",
            name="uq_party_external_refs_source_entity_external",
        ),
        UniqueConstraint(
            "party_id",
            "source_system",
            "entity_type",
            name="uq_party_external_refs_party_source_entity",
        ),
        Index("ix_party_external_refs_party", "party_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False
    )
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    party = relationship("Party", back_populates="external_references")


class PartyIdentityBackfillReceipt(Base):
    """PII-free receipt for one atomic, explicitly approved identity backfill.

    The immutable manifest records the exact planned Party and Subscriber
    bindings needed to prove an idempotent replay or prepare a later reviewed
    compensation. It is execution evidence, not permission to merge or repoint
    an identity.
    """

    __tablename__ = "party_identity_backfill_receipts"
    __table_args__ = (
        CheckConstraint(
            "length(plan_digest) = 64 AND length(audit_digest) = 64 AND "
            "length(decision_file_sha256) = 64 AND "
            "length(plan_file_sha256) = 64 AND "
            "length(approval_file_sha256) = 64 AND "
            "length(approved_by_sha256) = 64 AND "
            "length(approval_reason_sha256) = 64",
            name="ck_party_backfill_receipts_digest_lengths",
        ),
        CheckConstraint(
            "planned_party_count >= 0 AND binding_count >= 0",
            name="ck_party_backfill_receipts_nonnegative_counts",
        ),
        CheckConstraint(
            "expires_at >= approved_at",
            name="ck_party_backfill_receipts_approval_window",
        ),
        UniqueConstraint(
            "plan_digest",
            name="uq_party_backfill_receipts_plan_digest",
        ),
        Index("ix_party_backfill_receipts_applied_at", "applied_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_reason_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    planned_party_count: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON(), nullable=False)
