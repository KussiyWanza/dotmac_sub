"""Canonical party, role, relationship, membership, and contact-point owner.

This module owns the additive foundation plus guarded Subscriber and
organization-role profile bindings. Existing subscriber, reseller, vendor,
organization, and authentication read paths do not consume Party yet. Later
cutover slices must call this owner rather than adding parallel writes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.organization import (
    Organization,
    OrganizationMembership,
    OrganizationMembershipRole,
)
from app.models.party import (
    PartnerRoleKey,
    Party,
    PartyContactConsentStatus,
    PartyContactPoint,
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyDataClassification,
    PartyExternalReference,
    PartyIdentityStatus,
    PartyMembership,
    PartyMembershipStatus,
    PartyMembershipType,
    PartyRelationship,
    PartyRelationshipStatus,
    PartyRelationshipType,
    PartyRole,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
    SubscriberContactPointProjection,
    SubscriberContactRelationshipProjection,
)
from app.models.subscriber import Reseller, ResellerUser, Subscriber, SubscriberContact
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services.customer_identity_normalization import (
    normalize_email_identifier,
    normalize_phone_identifier,
)


class PartyInvariantError(ValueError):
    """Raised when a command would violate the canonical party contract."""


@dataclass(frozen=True)
class PartyRoleContract:
    role_type: str
    role_key: str
    capability_domains: tuple[str, ...]
    implicit_permissions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonPartyProfileUpdate:
    """Typed profile values staged by an approved cross-domain coordinator."""

    party_id: UUID
    display_name: str
    first_name: str
    last_name: str
    address_line1: str | None
    address_line2: str | None
    date_of_birth: str | None
    gender: str
    nin_encrypted: str | None
    city: str | None
    postal_code: str | None
    country_code: str | None
    organization_id: UUID | None
    reseller_id: UUID | None
    primary_email: str | None
    primary_phone: str | None
    communication_routed_through_reseller: bool


@dataclass(frozen=True, slots=True)
class PartyContactPointSet:
    """Complete desired default-scope values for one Party channel."""

    party_id: UUID
    channel_type: PartyContactPointType
    values: tuple[str, ...]
    primary_index: int | None
    source: str


_ROLE_CAPABILITY_DOMAINS: dict[str, tuple[str, ...]] = {
    PartyRoleType.prospect.value: ("sales",),
    PartyRoleType.customer.value: ("sales", "billing", "support"),
    PartyRoleType.subscriber.value: ("service", "billing", "support"),
    PartyRoleType.reseller.value: (
        "managed_customer_scope",
        "catalog_scope",
        "commission",
        "collections",
        "billing",
        "reseller_portal",
    ),
    PartyRoleType.vendor.value: (
        "procurement",
        "project_quotes",
        "purchase_invoices",
        "field_operations",
        "vendor_portal",
    ),
    PartyRoleType.staff.value: ("internal_operations",),
    PartyRoleType.agent.value: ("sales", "support", "service_team"),
}

_PARTNER_CAPABILITY_DOMAINS: dict[str, tuple[str, ...]] = {
    PartnerRoleKey.referral.value: ("referrals",),
    PartnerRoleKey.technology.value: ("technology_collaboration",),
    PartnerRoleKey.infrastructure.value: ("infrastructure_collaboration",),
    PartnerRoleKey.strategic.value: ("strategic_collaboration",),
}

_SCOPED_SOCIAL_CONTACT_TYPES = {
    PartyContactPointType.facebook_messenger.value,
    PartyContactPointType.instagram_dm.value,
    PartyContactPointType.telegram.value,
    PartyContactPointType.linkedin.value,
    PartyContactPointType.x.value,
}

_SUBSCRIBER_CONTACT_RELATIONSHIP_TYPES = {
    PartyRelationshipType.contact_for.value,
    PartyRelationshipType.billing_contact_for.value,
    PartyRelationshipType.technical_contact_for.value,
    PartyRelationshipType.emergency_contact_for.value,
}

_SUBSCRIBER_CONTACT_SOURCE_CHANNELS = {
    "email": PartyContactPointType.email.value,
    "phone": PartyContactPointType.phone.value,
    "whatsapp": PartyContactPointType.whatsapp.value,
    "facebook": PartyContactPointType.facebook_messenger.value,
    "instagram": PartyContactPointType.instagram_dm.value,
    "x_handle": PartyContactPointType.x.value,
    "telegram": PartyContactPointType.telegram.value,
    "linkedin": PartyContactPointType.linkedin.value,
}


def _enum_value(value, enum_cls: type[enum.StrEnum], field_name: str) -> str:
    raw = value.value if isinstance(value, enum_cls) else str(value).strip().lower()
    try:
        return enum_cls(raw).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_cls)
        raise PartyInvariantError(
            f"Invalid {field_name} '{raw}'; expected one of: {allowed}"
        ) from exc


def _required_text(value: str | None, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise PartyInvariantError(f"{field_name} is required")
    return cleaned


def _party(db: Session, party_id: UUID) -> Party:
    party = db.get(Party, party_id)
    if party is None:
        raise PartyInvariantError(f"Party '{party_id}' was not found")
    if party.status == PartyIdentityStatus.merged.value:
        raise PartyInvariantError(
            f"Party '{party_id}' was merged into '{party.merged_into_party_id}'"
        )
    return party


def normalize_role_key(role_type: str | PartyRoleType, role_key: str | None) -> str:
    normalized_type = _enum_value(role_type, PartyRoleType, "role_type")
    cleaned_key = (role_key or "").strip().lower()
    if normalized_type == PartyRoleType.partner.value:
        if not cleaned_key:
            raise PartyInvariantError(
                "A partner role requires an explicit key: referral, technology, "
                "infrastructure, or strategic"
            )
        return _enum_value(cleaned_key, PartnerRoleKey, "partner role_key")
    if cleaned_key and cleaned_key != "default":
        raise PartyInvariantError(
            f"Role '{normalized_type}' uses role_key='default'; it is not a partner alias"
        )
    return "default"


def role_contract(
    role_type: str | PartyRoleType, role_key: str | None = None
) -> PartyRoleContract:
    """Return the business boundary without granting any authorization.

    ``capability_domains`` describes which domain contracts may apply after
    their own onboarding and authorization checks.  It is never a permission
    list; every role intentionally has zero implicit permissions.
    """

    normalized_type = _enum_value(role_type, PartyRoleType, "role_type")
    normalized_key = normalize_role_key(normalized_type, role_key)
    if normalized_type == PartyRoleType.partner.value:
        capabilities = _PARTNER_CAPABILITY_DOMAINS[normalized_key]
    else:
        capabilities = _ROLE_CAPABILITY_DOMAINS.get(normalized_type, ())
    return PartyRoleContract(
        role_type=normalized_type,
        role_key=normalized_key,
        capability_domains=capabilities,
    )


def create_party(
    db: Session,
    *,
    party_type: str | PartyType,
    display_name: str,
    party_id: UUID | None = None,
    data_classification: str | PartyDataClassification = (
        PartyDataClassification.production
    ),
    metadata: dict | None = None,
) -> Party:
    party = Party(
        id=party_id or uuid4(),
        party_type=_enum_value(party_type, PartyType, "party_type"),
        display_name=_required_text(display_name, "display_name"),
        status=PartyIdentityStatus.active.value,
        data_classification=_enum_value(
            data_classification,
            PartyDataClassification,
            "data_classification",
        ),
        metadata_=dict(metadata) if metadata else None,
    )
    db.add(party)
    db.flush()
    return party


def bind_subscriber_account(
    db: Session,
    *,
    subscriber_id: UUID,
    party_id: UUID,
    source: str,
    reason: str,
) -> Subscriber:
    """Bind one service/billing account to its reviewed canonical identity.

    One Party may own several subscriber accounts. The command is idempotent
    for an exact retry and refuses a different target: identity repointing must
    use the future reviewed merge/repoint workflow, never a force flag here.
    Binding does not assign a role, copy contact data, or change account,
    subscription, billing, access, or authorization state.
    """

    target = _party(db, party_id)
    if target.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        raise PartyInvariantError(
            f"Party '{party_id}' in status '{target.status}' cannot own a "
            "subscriber account binding"
        )
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise PartyInvariantError(f"Subscriber '{subscriber_id}' was not found")
    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    if subscriber.party_id is not None:
        if subscriber.party_id == target.id:
            return subscriber
        raise PartyInvariantError(
            f"Subscriber '{subscriber_id}' is already bound to Party "
            f"'{subscriber.party_id}'; use the reviewed merge/repoint workflow"
        )
    subscriber.party_id = target.id
    subscriber.party_bound_at = datetime.now(UTC)
    subscriber.party_binding_source = normalized_source
    subscriber.party_binding_reason = normalized_reason
    db.flush()
    return subscriber


def _organization_party(db: Session, party_id: UUID) -> Party:
    target = _party(db, party_id)
    if target.party_type != PartyType.organization.value:
        raise PartyInvariantError(
            f"Party '{party_id}' must be an organization Party for a business profile"
        )
    if target.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        raise PartyInvariantError(
            f"Organization Party '{party_id}' in status '{target.status}' cannot "
            "receive a business profile binding"
        )
    return target


def _person_party(db: Session, party_id: UUID) -> Party:
    target = _party(db, party_id)
    if target.party_type != PartyType.person.value:
        raise PartyInvariantError(
            f"Party '{party_id}' must be a person Party for a principal binding"
        )
    if target.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        raise PartyInvariantError(
            f"Person Party '{party_id}' in status '{target.status}' cannot receive "
            "a principal binding"
        )
    return target


def _bind_organization_profile(
    db: Session,
    *,
    profile: Any,
    model: Any,
    profile_name: str,
    target: Party,
    source: str,
    reason: str,
    bound_at: datetime | None = None,
) -> Any:
    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    if profile.party_id is not None:
        if profile.party_id != target.id:
            raise PartyInvariantError(
                f"{profile_name} '{profile.id}' is already bound to Party "
                f"'{profile.party_id}'; use the reviewed merge/repoint workflow"
            )
        if not (
            profile.party_bound_at is not None
            and (profile.party_binding_source or "").strip()
            and (profile.party_binding_reason or "").strip()
        ):
            raise PartyInvariantError(
                f"{profile_name} '{profile.id}' has incomplete Party binding evidence"
            )
        return profile
    existing_profile_id = (
        db.query(model.id)
        .filter(model.party_id == target.id, model.id != profile.id)
        .scalar()
    )
    if existing_profile_id is not None:
        raise PartyInvariantError(
            f"Party '{target.id}' is already bound to another {profile_name} "
            f"'{existing_profile_id}'"
        )
    profile.party_id = target.id
    profile.party_bound_at = bound_at or datetime.now(UTC)
    profile.party_binding_source = normalized_source
    profile.party_binding_reason = normalized_reason
    db.flush()
    return profile


def bind_organization_profile(
    db: Session,
    *,
    organization_id: UUID,
    party_id: UUID,
    source: str,
    reason: str,
) -> Organization:
    """Bind a B2B Organization profile without assigning a business role."""

    target = _organization_party(db, party_id)
    organization = db.get(Organization, organization_id)
    if organization is None:
        raise PartyInvariantError(f"Organization '{organization_id}' was not found")
    return _bind_organization_profile(
        db,
        profile=organization,
        model=Organization,
        profile_name="Organization",
        target=target,
        source=source,
        reason=reason,
    )


def bind_reseller_profile(
    db: Session,
    *,
    reseller_id: UUID,
    party_id: UUID,
    source: str,
    reason: str,
) -> Reseller:
    """Bind a Reseller commercial profile without assigning its role."""

    target = _organization_party(db, party_id)
    reseller = db.get(Reseller, reseller_id)
    if reseller is None:
        raise PartyInvariantError(f"Reseller '{reseller_id}' was not found")
    return _bind_organization_profile(
        db,
        profile=reseller,
        model=Reseller,
        profile_name="Reseller",
        target=target,
        source=source,
        reason=reason,
    )


def bind_vendor_profiles(
    db: Session,
    *,
    vendor_id: UUID,
    party_id: UUID,
    source: str,
    reason: str,
) -> tuple[Vendor, FieldVendor]:
    """Atomically bind the native Vendor and its FieldVendor auth projection.

    The legacy string UUID is accepted only to locate the exact existing twin.
    Both profiles must start unbound or already carry the same exact Party
    binding. Partial and conflicting states require explicit adjudication.
    """

    target = _organization_party(db, party_id)
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise PartyInvariantError(f"Vendor '{vendor_id}' was not found")
    field_vendor = (
        db.query(FieldVendor)
        .filter(FieldVendor.crm_vendor_id == str(vendor.id))
        .one_or_none()
    )
    if field_vendor is None:
        raise PartyInvariantError(
            f"Vendor '{vendor_id}' has no exact FieldVendor auth projection"
        )
    existing_targets = (vendor.party_id, field_vendor.party_id)
    if any(value is not None for value in existing_targets) and existing_targets != (
        target.id,
        target.id,
    ):
        raise PartyInvariantError(
            f"Vendor '{vendor_id}' and FieldVendor '{field_vendor.id}' have a "
            "partial or conflicting Party binding; explicit adjudication is required"
        )
    with db.begin_nested():
        bound_at = datetime.now(UTC)
        _bind_organization_profile(
            db,
            profile=vendor,
            model=Vendor,
            profile_name="Vendor",
            target=target,
            source=source,
            reason=reason,
            bound_at=bound_at,
        )
        _bind_organization_profile(
            db,
            profile=field_vendor,
            model=FieldVendor,
            profile_name="FieldVendor",
            target=target,
            source=source,
            reason=reason,
            bound_at=bound_at,
        )
    return vendor, field_vendor


def bind_system_user_principal(
    db: Session,
    *,
    system_user_id: UUID,
    person_party_id: UUID,
    source: str,
    reason: str,
) -> SystemUser:
    """Bind one staff login principal to a reviewed Person Party.

    This is identity linkage only. It does not activate the SystemUser, create
    credentials, assign staff/agent roles, or modify RBAC.
    """

    target = _person_party(db, person_party_id)
    system_user = db.get(SystemUser, system_user_id)
    if system_user is None:
        raise PartyInvariantError(f"SystemUser '{system_user_id}' was not found")
    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    if system_user.person_party_id is not None:
        if system_user.person_party_id != target.id:
            raise PartyInvariantError(
                f"SystemUser '{system_user_id}' is already bound to Person Party "
                f"'{system_user.person_party_id}'; use the reviewed merge/repoint "
                "workflow"
            )
        if not _has_complete_binding_evidence(system_user):
            raise PartyInvariantError(
                f"SystemUser '{system_user_id}' has incomplete Party binding evidence"
            )
        return system_user
    existing_principal_id = (
        db.query(SystemUser.id)
        .filter(
            SystemUser.person_party_id == target.id,
            SystemUser.id != system_user.id,
        )
        .scalar()
    )
    if existing_principal_id is not None:
        raise PartyInvariantError(
            f"Person Party '{target.id}' is already bound to SystemUser "
            f"'{existing_principal_id}'"
        )
    system_user.person_party_id = target.id
    system_user.party_bound_at = datetime.now(UTC)
    system_user.party_binding_source = normalized_source
    system_user.party_binding_reason = normalized_reason
    db.flush()
    return system_user


def _has_complete_binding_evidence(projection: Any) -> bool:
    return bool(
        projection.party_bound_at is not None
        and (projection.party_binding_source or "").strip()
        and (projection.party_binding_reason or "").strip()
    )


def _membership_context(
    db: Session,
    *,
    party_membership_id: UUID,
    membership_type: PartyMembershipType,
) -> PartyMembership:
    membership = db.get(PartyMembership, party_membership_id)
    if membership is None:
        raise PartyInvariantError(
            f"PartyMembership '{party_membership_id}' was not found"
        )
    person = _person_party(db, membership.person_party_id)
    organization = _organization_party(db, membership.organization_party_id)
    if membership.membership_type != membership_type.value:
        raise PartyInvariantError(
            f"PartyMembership '{membership.id}' must have membership_type "
            f"'{membership_type.value}', not '{membership.membership_type}'"
        )
    if membership.person_party_id != person.id:
        raise PartyInvariantError("PartyMembership person context is invalid")
    if membership.organization_party_id != organization.id:
        raise PartyInvariantError("PartyMembership organization context is invalid")
    return membership


def _bind_membership_projection(
    db: Session,
    *,
    projection: Any,
    model: Any,
    projection_name: str,
    membership: PartyMembership,
    source: str,
    reason: str,
    bound_at: datetime | None = None,
) -> Any:
    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    if projection.party_membership_id is not None:
        if projection.party_membership_id != membership.id:
            raise PartyInvariantError(
                f"{projection_name} '{projection.id}' is already bound to "
                f"PartyMembership '{projection.party_membership_id}'; use the "
                "reviewed merge/repoint workflow"
            )
        if not _has_complete_binding_evidence(projection):
            raise PartyInvariantError(
                f"{projection_name} '{projection.id}' has incomplete Party binding "
                "evidence"
            )
        return projection
    existing_projection_id = (
        db.query(model.id)
        .filter(
            model.party_membership_id == membership.id,
            model.id != projection.id,
        )
        .scalar()
    )
    if existing_projection_id is not None:
        raise PartyInvariantError(
            f"PartyMembership '{membership.id}' is already bound to another "
            f"{projection_name} '{existing_projection_id}'"
        )
    projection.party_membership_id = membership.id
    projection.party_bound_at = bound_at or datetime.now(UTC)
    projection.party_binding_source = normalized_source
    projection.party_binding_reason = normalized_reason
    db.flush()
    return projection


def bind_reseller_user_principal(
    db: Session,
    *,
    reseller_user_id: UUID,
    person_party_id: UUID,
    party_membership_id: UUID,
    source: str,
    reason: str,
) -> ResellerUser:
    """Bind a reseller login to one Person and explicit reseller context.

    The referenced PartyMembership must already exist as ``reseller_admin``
    and point to the Reseller profile's reviewed Organization Party. Neither
    compatibility ``is_active`` state nor membership status is changed.
    """

    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    target = _person_party(db, person_party_id)
    membership = _membership_context(
        db,
        party_membership_id=party_membership_id,
        membership_type=PartyMembershipType.reseller_admin,
    )
    if membership.person_party_id != target.id:
        raise PartyInvariantError(
            f"PartyMembership '{membership.id}' belongs to Person Party "
            f"'{membership.person_party_id}', not '{target.id}'"
        )
    reseller_user = db.get(ResellerUser, reseller_user_id)
    if reseller_user is None:
        raise PartyInvariantError(f"ResellerUser '{reseller_user_id}' was not found")
    if reseller_user.reseller_id is None:
        raise PartyInvariantError(
            f"ResellerUser '{reseller_user_id}' has no reseller context"
        )
    reseller = db.get(Reseller, reseller_user.reseller_id)
    if reseller is None or reseller.party_id is None:
        raise PartyInvariantError(
            f"Reseller '{reseller_user.reseller_id}' must have a reviewed "
            "Organization Party binding first"
        )
    if reseller.party_id != membership.organization_party_id:
        raise PartyInvariantError(
            f"PartyMembership '{membership.id}' organization context does not "
            f"match Reseller '{reseller.id}'"
        )
    current = (reseller_user.person_party_id, reseller_user.party_membership_id)
    requested = (target.id, membership.id)
    if any(value is not None for value in current):
        if current != requested:
            raise PartyInvariantError(
                f"ResellerUser '{reseller_user.id}' has a partial or conflicting "
                "Party binding; explicit adjudication is required"
            )
        if not _has_complete_binding_evidence(reseller_user):
            raise PartyInvariantError(
                f"ResellerUser '{reseller_user.id}' has incomplete Party binding "
                "evidence"
            )
        return reseller_user
    existing_principal_id = (
        db.query(ResellerUser.id)
        .filter(
            ResellerUser.reseller_id == reseller.id,
            ResellerUser.person_party_id == target.id,
            ResellerUser.id != reseller_user.id,
        )
        .scalar()
    )
    if existing_principal_id is not None:
        raise PartyInvariantError(
            f"Person Party '{target.id}' already has ResellerUser "
            f"'{existing_principal_id}' for Reseller '{reseller.id}'"
        )
    reseller_user.person_party_id = target.id
    reseller_user.party_membership_id = membership.id
    reseller_user.party_bound_at = datetime.now(UTC)
    reseller_user.party_binding_source = normalized_source
    reseller_user.party_binding_reason = normalized_reason
    db.flush()
    return reseller_user


def bind_field_vendor_user_context(
    db: Session,
    *,
    field_vendor_user_id: UUID,
    party_membership_id: UUID,
    source: str,
    reason: str,
) -> FieldVendorUser:
    """Bind the live FieldVendorUser projection to explicit vendor context.

    The canonical Person is the bound SystemUser's Person Party and the
    canonical organization is the already aligned Vendor/FieldVendor Party.
    The unused native VendorUser is deliberately not made authoritative.
    """

    field_vendor_user = db.get(FieldVendorUser, field_vendor_user_id)
    if field_vendor_user is None:
        raise PartyInvariantError(
            f"FieldVendorUser '{field_vendor_user_id}' was not found"
        )
    field_vendor = db.get(FieldVendor, field_vendor_user.vendor_id)
    if field_vendor is None or field_vendor.crm_vendor_id is None:
        raise PartyInvariantError(
            "FieldVendorUser has no exact native Vendor profile bridge"
        )
    try:
        native_vendor_id = UUID(field_vendor.crm_vendor_id)
    except ValueError as exc:
        raise PartyInvariantError(
            "FieldVendorUser has an invalid native Vendor profile bridge"
        ) from exc
    vendor = db.get(Vendor, native_vendor_id)
    if vendor is None:
        raise PartyInvariantError(
            "FieldVendorUser has an orphan native Vendor profile bridge"
        )
    if vendor.party_id is None or field_vendor.party_id is None:
        raise PartyInvariantError(
            "Vendor and FieldVendor profiles must have reviewed Party bindings first"
        )
    if vendor.party_id != field_vendor.party_id:
        raise PartyInvariantError(
            "Vendor and FieldVendor profiles have conflicting Organization Parties"
        )
    system_user = db.get(SystemUser, field_vendor_user.system_user_id)
    if system_user is None or system_user.person_party_id is None:
        raise PartyInvariantError(
            f"SystemUser '{field_vendor_user.system_user_id}' must have a reviewed "
            "Person Party binding first"
        )
    person = _person_party(db, system_user.person_party_id)
    membership = _membership_context(
        db,
        party_membership_id=party_membership_id,
        membership_type=PartyMembershipType.vendor_user,
    )
    if membership.person_party_id != person.id:
        raise PartyInvariantError(
            f"PartyMembership '{membership.id}' does not match the FieldVendorUser "
            "SystemUser's Person Party"
        )
    if membership.organization_party_id != vendor.party_id:
        raise PartyInvariantError(
            f"PartyMembership '{membership.id}' does not match the Vendor "
            "Organization Party"
        )
    return _bind_membership_projection(
        db,
        projection=field_vendor_user,
        model=FieldVendorUser,
        projection_name="FieldVendorUser",
        membership=membership,
        source=source,
        reason=reason,
    )


def bind_organization_membership_context(
    db: Session,
    *,
    organization_membership_id: UUID,
    party_membership_id: UUID,
    source: str,
    reason: str,
) -> OrganizationMembership:
    """Bind a legacy OrganizationMembership to its canonical PartyMembership."""

    projection = db.get(OrganizationMembership, organization_membership_id)
    if projection is None:
        raise PartyInvariantError(
            f"OrganizationMembership '{organization_membership_id}' was not found"
        )
    expected_types = {
        OrganizationMembershipRole.owner.value: PartyMembershipType.owner,
        OrganizationMembershipRole.admin.value: PartyMembershipType.admin,
        OrganizationMembershipRole.member.value: PartyMembershipType.member,
    }
    expected_type = expected_types.get(projection.role)
    if expected_type is None:
        raise PartyInvariantError(
            f"OrganizationMembership '{projection.id}' has unsupported role "
            f"'{projection.role}'"
        )
    membership = _membership_context(
        db,
        party_membership_id=party_membership_id,
        membership_type=expected_type,
    )
    organization = db.get(Organization, projection.organization_id)
    if organization is None or organization.party_id is None:
        raise PartyInvariantError(
            f"Organization '{projection.organization_id}' must have a reviewed "
            "Organization Party binding first"
        )
    if organization.party_id != membership.organization_party_id:
        raise PartyInvariantError(
            f"PartyMembership '{membership.id}' does not match Organization "
            f"'{organization.id}'"
        )
    return _bind_membership_projection(
        db,
        projection=projection,
        model=OrganizationMembership,
        projection_name="OrganizationMembership",
        membership=membership,
        source=source,
        reason=reason,
    )


def bind_subscriber_contact_person(
    db: Session,
    *,
    subscriber_contact_id: UUID,
    person_party_id: UUID,
    source: str,
    reason: str,
) -> SubscriberContact:
    """Bind one reviewed legacy contact row to a canonical Person Party.

    The owning Subscriber must already have a reviewed Party binding. Contact
    values are not copied and do not become identity proof. Authorization,
    notification, billing-contact, relationship, verification, and consent
    state remain unchanged.
    """

    target = _person_party(db, person_party_id)
    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    contact = db.get(SubscriberContact, subscriber_contact_id)
    if contact is None:
        raise PartyInvariantError(
            f"SubscriberContact '{subscriber_contact_id}' was not found"
        )
    subscriber = db.get(Subscriber, contact.subscriber_id)
    if subscriber is None or subscriber.party_id is None:
        raise PartyInvariantError(
            f"Subscriber '{contact.subscriber_id}' must have a reviewed Party "
            "binding first"
        )
    account_party = _party(db, subscriber.party_id)
    if account_party.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        raise PartyInvariantError(
            f"Subscriber Party '{account_party.id}' in status "
            f"'{account_party.status}' cannot receive a contact binding"
        )
    if contact.person_party_id is not None:
        if contact.person_party_id != target.id:
            raise PartyInvariantError(
                f"SubscriberContact '{contact.id}' is already bound to Person "
                f"Party '{contact.person_party_id}'; use the reviewed "
                "merge/repoint workflow"
            )
        if not _has_complete_binding_evidence(contact):
            raise PartyInvariantError(
                f"SubscriberContact '{contact.id}' has incomplete Party binding "
                "evidence"
            )
        return contact
    existing_contact_id = (
        db.query(SubscriberContact.id)
        .filter(
            SubscriberContact.subscriber_id == contact.subscriber_id,
            SubscriberContact.person_party_id == target.id,
            SubscriberContact.id != contact.id,
        )
        .scalar()
    )
    if existing_contact_id is not None:
        raise PartyInvariantError(
            f"Person Party '{target.id}' is already bound to SubscriberContact "
            f"'{existing_contact_id}' for Subscriber '{contact.subscriber_id}'"
        )
    contact.person_party_id = target.id
    contact.party_bound_at = datetime.now(UTC)
    contact.party_binding_source = normalized_source
    contact.party_binding_reason = normalized_reason
    db.flush()
    return contact


def bind_subscriber_contact_relationship(
    db: Session,
    *,
    subscriber_contact_id: UUID,
    party_relationship_id: UUID,
    source: str,
    reason: str,
) -> SubscriberContactRelationshipProjection:
    """Project one reviewed descriptive relationship from a legacy contact."""

    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    contact = db.get(SubscriberContact, subscriber_contact_id)
    if contact is None or contact.person_party_id is None:
        raise PartyInvariantError(
            f"SubscriberContact '{subscriber_contact_id}' must have a reviewed "
            "Person Party binding first"
        )
    _person_party(db, contact.person_party_id)
    subscriber = db.get(Subscriber, contact.subscriber_id)
    if subscriber is None or subscriber.party_id is None:
        raise PartyInvariantError(
            f"Subscriber '{contact.subscriber_id}' must have a reviewed Party "
            "binding first"
        )
    account_party = _party(db, subscriber.party_id)
    if account_party.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        raise PartyInvariantError(
            f"Subscriber Party '{account_party.id}' in status "
            f"'{account_party.status}' cannot receive a contact relationship "
            "projection"
        )
    relationship = db.get(PartyRelationship, party_relationship_id)
    if relationship is None:
        raise PartyInvariantError(
            f"PartyRelationship '{party_relationship_id}' was not found"
        )
    if relationship.relationship_type not in _SUBSCRIBER_CONTACT_RELATIONSHIP_TYPES:
        raise PartyInvariantError(
            f"PartyRelationship '{relationship.id}' is not a contact relationship"
        )
    if relationship.status not in {
        PartyRelationshipStatus.pending.value,
        PartyRelationshipStatus.active.value,
    }:
        raise PartyInvariantError(
            f"PartyRelationship '{relationship.id}' in status "
            f"'{relationship.status}' cannot represent a current SubscriberContact"
        )
    if relationship.subject_party_id != contact.person_party_id:
        raise PartyInvariantError(
            f"PartyRelationship '{relationship.id}' does not start at the "
            "SubscriberContact Person Party"
        )
    if relationship.object_party_id != subscriber.party_id:
        raise PartyInvariantError(
            f"PartyRelationship '{relationship.id}' does not end at the "
            "Subscriber Party"
        )
    existing = (
        db.query(SubscriberContactRelationshipProjection)
        .filter(
            SubscriberContactRelationshipProjection.subscriber_contact_id == contact.id,
            SubscriberContactRelationshipProjection.party_relationship_id
            == relationship.id,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    other_contact_id = (
        db.query(SubscriberContactRelationshipProjection.subscriber_contact_id)
        .filter(
            SubscriberContactRelationshipProjection.party_relationship_id
            == relationship.id
        )
        .scalar()
    )
    if other_contact_id is not None:
        raise PartyInvariantError(
            f"PartyRelationship '{relationship.id}' is already projected from "
            f"SubscriberContact '{other_contact_id}'"
        )
    projection = SubscriberContactRelationshipProjection(
        subscriber_contact_id=contact.id,
        party_relationship_id=relationship.id,
        bound_at=datetime.now(UTC),
        binding_source=normalized_source,
        binding_reason=normalized_reason,
    )
    db.add(projection)
    db.flush()
    return projection


def _normalized_contact_projection_value(source_field: str, value: str) -> str:
    if source_field == "email":
        normalized = normalize_email_identifier(value)
    elif source_field in {"phone", "whatsapp"}:
        normalized = normalize_phone_identifier(value)
    else:
        normalized = value.strip().casefold()
    if not normalized:
        raise PartyInvariantError(
            f"SubscriberContact source field '{source_field}' has no linkable value"
        )
    return normalized


def bind_subscriber_contact_point(
    db: Session,
    *,
    subscriber_contact_id: UUID,
    source_field: str,
    party_contact_point_id: UUID,
    source: str,
    reason: str,
) -> SubscriberContactPointProjection:
    """Project one reviewed legacy source field to an existing contact point.

    Verification and consent belong to PartyContactPoint and are never copied
    from legacy contact flags. Social projections require the canonical point's
    provider/account/immutable-subject context; a legacy handle alone cannot
    create or identify a Party.
    """

    normalized_source = _required_text(source, "source")
    normalized_reason = _required_text(reason, "reason")
    normalized_field = source_field.strip().lower()
    expected_channel = _SUBSCRIBER_CONTACT_SOURCE_CHANNELS.get(normalized_field)
    if expected_channel is None:
        allowed = ", ".join(_SUBSCRIBER_CONTACT_SOURCE_CHANNELS)
        raise PartyInvariantError(
            f"Invalid source_field '{normalized_field}'; expected one of: {allowed}"
        )
    contact = db.get(SubscriberContact, subscriber_contact_id)
    if contact is None or contact.person_party_id is None:
        raise PartyInvariantError(
            f"SubscriberContact '{subscriber_contact_id}' must have a reviewed "
            "Person Party binding first"
        )
    _person_party(db, contact.person_party_id)
    raw_value = getattr(contact, normalized_field, None)
    legacy_value = _normalized_contact_projection_value(
        normalized_field, _required_text(raw_value, normalized_field)
    )
    point = db.get(PartyContactPoint, party_contact_point_id)
    if point is None:
        raise PartyInvariantError(
            f"PartyContactPoint '{party_contact_point_id}' was not found"
        )
    if not point.is_active:
        raise PartyInvariantError(
            f"PartyContactPoint '{point.id}' is inactive and cannot receive a "
            "new projection"
        )
    if point.party_id != contact.person_party_id:
        raise PartyInvariantError(
            f"PartyContactPoint '{point.id}' does not belong to the "
            "SubscriberContact Person Party"
        )
    if point.channel_type != expected_channel:
        raise PartyInvariantError(
            f"PartyContactPoint '{point.id}' channel '{point.channel_type}' does "
            f"not match source_field '{normalized_field}'"
        )
    point_values = {
        _normalized_contact_projection_value(normalized_field, point.normalized_value)
    }
    if point.display_value:
        point_values.add(
            _normalized_contact_projection_value(normalized_field, point.display_value)
        )
    if expected_channel in _SCOPED_SOCIAL_CONTACT_TYPES:
        if not (
            (point.provider or "").strip()
            and (point.provider_account_id or "").strip()
            and (point.external_subject_id or "").strip()
        ):
            raise PartyInvariantError(
                f"PartyContactPoint '{point.id}' lacks immutable social identity scope"
            )
    if legacy_value not in point_values:
        raise PartyInvariantError(
            f"SubscriberContact source_field '{normalized_field}' does not match "
            f"PartyContactPoint '{point.id}'"
        )
    existing = (
        db.query(SubscriberContactPointProjection)
        .filter(
            SubscriberContactPointProjection.subscriber_contact_id == contact.id,
            SubscriberContactPointProjection.source_field == normalized_field,
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.party_contact_point_id != point.id:
            raise PartyInvariantError(
                f"SubscriberContact '{contact.id}' source_field "
                f"'{normalized_field}' is already projected to PartyContactPoint "
                f"'{existing.party_contact_point_id}'; use the reviewed "
                "merge/repoint workflow"
            )
        return existing
    duplicate_point = (
        db.query(SubscriberContactPointProjection.id)
        .filter(
            SubscriberContactPointProjection.subscriber_contact_id == contact.id,
            SubscriberContactPointProjection.party_contact_point_id == point.id,
        )
        .scalar()
    )
    if duplicate_point is not None:
        raise PartyInvariantError(
            f"PartyContactPoint '{point.id}' is already projected from another "
            f"source field on SubscriberContact '{contact.id}'"
        )
    projection = SubscriberContactPointProjection(
        subscriber_contact_id=contact.id,
        source_field=normalized_field,
        party_contact_point_id=point.id,
        bound_at=datetime.now(UTC),
        binding_source=normalized_source,
        binding_reason=normalized_reason,
    )
    db.add(projection)
    db.flush()
    return projection


def quarantine_party(db: Session, *, party_id: UUID, reason: str) -> Party:
    party = _party(db, party_id)
    party.status = PartyIdentityStatus.quarantined.value
    party.merge_reason = _required_text(reason, "reason")
    db.flush()
    return party


def assign_role(
    db: Session,
    *,
    party_id: UUID,
    role_type: str | PartyRoleType,
    role_key: str | None = None,
    status: str | PartyRoleStatus = PartyRoleStatus.pending,
    source: str | None = None,
    metadata: dict | None = None,
) -> PartyRole:
    party = _party(db, party_id)
    contract = role_contract(role_type, role_key)
    normalized_status = _enum_value(status, PartyRoleStatus, "status")
    existing = (
        db.query(PartyRole)
        .filter(PartyRole.party_id == party.id)
        .filter(PartyRole.role_type == contract.role_type)
        .filter(PartyRole.role_key == contract.role_key)
        .one_or_none()
    )
    if existing is not None:
        raise PartyInvariantError(
            f"Party already has role '{contract.role_type}:{contract.role_key}'"
        )
    role = PartyRole(
        party_id=party.id,
        role_type=contract.role_type,
        role_key=contract.role_key,
        status=normalized_status,
        source=(source or "").strip() or None,
        metadata_=dict(metadata) if metadata else None,
    )
    db.add(role)
    db.flush()
    return role


def ensure_role(
    db: Session,
    *,
    party_id: UUID,
    role_type: str | PartyRoleType,
    role_key: str | None = None,
    status: str | PartyRoleStatus = PartyRoleStatus.active,
    source: str | None = None,
    metadata: dict | None = None,
) -> PartyRole:
    """Idempotently establish a role without bypassing suspended/ended state."""

    party = _party(db, party_id)
    contract = role_contract(role_type, role_key)
    normalized_status = _enum_value(status, PartyRoleStatus, "status")
    existing = (
        db.query(PartyRole)
        .filter(
            PartyRole.party_id == party.id,
            PartyRole.role_type == contract.role_type,
            PartyRole.role_key == contract.role_key,
        )
        .one_or_none()
    )
    if existing is None:
        return assign_role(
            db,
            party_id=party.id,
            role_type=contract.role_type,
            role_key=contract.role_key,
            status=normalized_status,
            source=source,
            metadata=metadata,
        )
    if existing.status == normalized_status:
        return existing
    if (
        existing.status == PartyRoleStatus.pending.value
        and normalized_status == PartyRoleStatus.active.value
    ):
        existing.status = normalized_status
        if source and not existing.source:
            existing.source = source.strip() or None
        db.flush()
        return existing
    raise PartyInvariantError(
        f"Party role '{contract.role_type}:{contract.role_key}' is "
        f"'{existing.status}' and cannot be implicitly changed to "
        f"'{normalized_status}'"
    )


def transition_role(
    db: Session,
    *,
    role_id: UUID,
    status: str | PartyRoleStatus,
) -> PartyRole:
    role = db.get(PartyRole, role_id)
    if role is None:
        raise PartyInvariantError(f"Party role '{role_id}' was not found")
    role.status = _enum_value(status, PartyRoleStatus, "status")
    db.flush()
    return role


def relate_parties(
    db: Session,
    *,
    subject_party_id: UUID,
    object_party_id: UUID,
    relationship_type: str | PartyRelationshipType,
    relationship_key: str = "default",
    status: str | PartyRelationshipStatus = PartyRelationshipStatus.active,
    source: str | None = None,
    metadata: dict | None = None,
) -> PartyRelationship:
    subject = _party(db, subject_party_id)
    object_party = _party(db, object_party_id)
    if subject.id == object_party.id:
        raise PartyInvariantError("A party cannot have a relationship with itself")
    relationship = PartyRelationship(
        subject_party_id=subject.id,
        object_party_id=object_party.id,
        relationship_type=_enum_value(
            relationship_type, PartyRelationshipType, "relationship_type"
        ),
        relationship_key=_required_text(relationship_key, "relationship_key"),
        status=_enum_value(status, PartyRelationshipStatus, "status"),
        source=(source or "").strip() or None,
        metadata_=dict(metadata) if metadata else None,
    )
    db.add(relationship)
    db.flush()
    return relationship


def add_membership(
    db: Session,
    *,
    person_party_id: UUID,
    organization_party_id: UUID,
    membership_type: str | PartyMembershipType,
    membership_key: str = "default",
    status: str | PartyMembershipStatus = PartyMembershipStatus.invited,
    access_scope: dict | None = None,
    source: str | None = None,
    metadata: dict | None = None,
) -> PartyMembership:
    person = _party(db, person_party_id)
    organization = _party(db, organization_party_id)
    if person.party_type != PartyType.person.value:
        raise PartyInvariantError("person_party_id must reference a person party")
    if organization.party_type != PartyType.organization.value:
        raise PartyInvariantError(
            "organization_party_id must reference an organization party"
        )
    membership = PartyMembership(
        person_party_id=person.id,
        organization_party_id=organization.id,
        membership_type=_enum_value(
            membership_type, PartyMembershipType, "membership_type"
        ),
        membership_key=_required_text(membership_key, "membership_key"),
        status=_enum_value(status, PartyMembershipStatus, "status"),
        access_scope=dict(access_scope) if access_scope else None,
        source=(source or "").strip() or None,
        metadata_=dict(metadata) if metadata else None,
    )
    db.add(membership)
    db.flush()
    return membership


def add_contact_point(
    db: Session,
    *,
    party_id: UUID,
    channel_type: str | PartyContactPointType,
    normalized_value: str,
    display_value: str | None = None,
    scope_key: str = "default",
    provider: str | None = None,
    provider_account_id: str | None = None,
    external_subject_id: str | None = None,
    is_primary: bool = False,
    verification_status: str | PartyContactVerificationStatus = (
        PartyContactVerificationStatus.unverified
    ),
    consent_status: str | PartyContactConsentStatus = (
        PartyContactConsentStatus.unknown
    ),
    metadata: dict | None = None,
) -> PartyContactPoint:
    party = _party(db, party_id)
    normalized_channel = _enum_value(
        channel_type, PartyContactPointType, "channel_type"
    )
    value = _required_text(normalized_value, "normalized_value")
    if normalized_channel == PartyContactPointType.email.value:
        value = value.casefold()
    cleaned_provider = (provider or "").strip() or None
    cleaned_provider_account = (provider_account_id or "").strip() or None
    cleaned_subject = (external_subject_id or "").strip() or None
    if normalized_channel in _SCOPED_SOCIAL_CONTACT_TYPES and not (
        cleaned_provider and cleaned_provider_account and cleaned_subject
    ):
        raise PartyInvariantError(
            "Social contact points require provider, provider_account_id, and "
            "the immutable external_subject_id; a handle alone is not identity"
        )
    contact_point = PartyContactPoint(
        party_id=party.id,
        channel_type=normalized_channel,
        normalized_value=value,
        display_value=(display_value or "").strip() or None,
        scope_key=_required_text(scope_key, "scope_key"),
        provider=cleaned_provider,
        provider_account_id=cleaned_provider_account,
        external_subject_id=cleaned_subject,
        is_primary=is_primary,
        verification_status=_enum_value(
            verification_status,
            PartyContactVerificationStatus,
            "verification_status",
        ),
        consent_status=_enum_value(
            consent_status, PartyContactConsentStatus, "consent_status"
        ),
        metadata_=dict(metadata) if metadata else None,
    )
    db.add(contact_point)
    db.flush()
    return contact_point


def update_person_profile(db: Session, command: PersonPartyProfileUpdate) -> Party:
    """Stage explicit Person profile values while preserving unrelated metadata.

    A missing ``nin_encrypted`` means "keep the stored value" so an edit form
    never has to disclose the existing NIN merely to preserve it.
    """

    party = (
        db.query(Party)
        .filter(Party.id == command.party_id)
        .with_for_update()
        .one_or_none()
    )
    if party is None:
        raise PartyInvariantError(f"Party '{command.party_id}' was not found")
    if party.party_type != PartyType.person.value:
        raise PartyInvariantError("Only a Person Party profile can be updated")
    if party.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        raise PartyInvariantError("The Person Party is not editable")

    party.display_name = _required_text(command.display_name, "display_name")
    metadata = dict(party.metadata_) if isinstance(party.metadata_, dict) else {}
    metadata.update(
        {
            "profile_version": 1,
            "first_name": command.first_name,
            "last_name": command.last_name,
            "address_line1": command.address_line1,
            "address_line2": command.address_line2,
            "date_of_birth": command.date_of_birth,
            "gender": command.gender,
            "city": command.city,
            "postal_code": command.postal_code,
            "country_code": command.country_code,
            "organization_id": (
                str(command.organization_id) if command.organization_id else None
            ),
            "reseller_id": str(command.reseller_id) if command.reseller_id else None,
            "primary_email": command.primary_email,
            "primary_phone": command.primary_phone,
            "identity_managed_by": "sub",
            "communication_routed_through_reseller": (
                command.communication_routed_through_reseller
            ),
        }
    )
    if command.nin_encrypted is not None:
        metadata["nin_encrypted"] = command.nin_encrypted
    party.metadata_ = metadata
    db.flush()
    return party


def reconcile_contact_points(
    db: Session, command: PartyContactPointSet
) -> tuple[PartyContactPoint, ...]:
    """Stage the desired active default-scope points for one channel.

    Existing evidence is retained. Removed values are made inactive, unchanged
    values retain verification and consent, and previously inactive values are
    reactivated rather than duplicated.
    """

    _party(db, command.party_id)
    channel = _enum_value(command.channel_type, PartyContactPointType, "channel_type")
    desired = tuple(
        dict.fromkeys(
            _required_text(value, "contact value") for value in command.values
        )
    )
    if command.primary_index is not None and not 0 <= command.primary_index < len(
        desired
    ):
        raise PartyInvariantError("primary_index does not identify a contact value")

    existing = (
        db.query(PartyContactPoint)
        .filter(
            PartyContactPoint.party_id == command.party_id,
            PartyContactPoint.channel_type == channel,
            PartyContactPoint.scope_key == "default",
        )
        .with_for_update()
        .all()
    )
    by_value = {point.normalized_value: point for point in existing}

    # Clear the partial-unique primary slot before assigning its replacement.
    for existing_point in existing:
        existing_point.is_primary = False
    db.flush()

    desired_set = set(desired)
    for existing_point in existing:
        if existing_point.normalized_value not in desired_set:
            existing_point.is_active = False

    result: list[PartyContactPoint] = []
    for index, value in enumerate(desired):
        point = by_value.get(value)
        if point is None:
            point = add_contact_point(
                db,
                party_id=command.party_id,
                channel_type=command.channel_type,
                normalized_value=value,
                display_value=value,
                is_primary=False,
                metadata={"captured_by": command.source},
            )
        else:
            point.display_value = value
            point.is_active = True
            metadata = (
                dict(point.metadata_) if isinstance(point.metadata_, dict) else {}
            )
            metadata["last_updated_by"] = command.source
            point.metadata_ = metadata
        point.is_primary = command.primary_index == index
        result.append(point)
    db.flush()
    return tuple(result)


def set_contact_organization(
    db: Session,
    *,
    person_party_id: UUID,
    organization_party_id: UUID | None,
    source: str,
) -> PartyRelationship | None:
    """Stage the Person's one active default contact-for relationship."""

    person = _party(db, person_party_id)
    if person.party_type != PartyType.person.value:
        raise PartyInvariantError("The contact subject must be a Person Party")
    if organization_party_id is not None:
        organization = _party(db, organization_party_id)
        if organization.party_type != PartyType.organization.value:
            raise PartyInvariantError(
                "The contact target must be an Organization Party"
            )

    relationships = (
        db.query(PartyRelationship)
        .filter(
            PartyRelationship.subject_party_id == person_party_id,
            PartyRelationship.relationship_type
            == PartyRelationshipType.contact_for.value,
            PartyRelationship.relationship_key == "default",
        )
        .with_for_update()
        .all()
    )
    selected: PartyRelationship | None = None
    for relationship in relationships:
        matches = relationship.object_party_id == organization_party_id
        if matches:
            relationship.status = PartyRelationshipStatus.active.value
            selected = relationship
        elif relationship.source == source:
            relationship.status = PartyRelationshipStatus.inactive.value

    if organization_party_id is not None and selected is None:
        selected = relate_parties(
            db,
            subject_party_id=person_party_id,
            object_party_id=organization_party_id,
            relationship_type=PartyRelationshipType.contact_for,
            source=source,
        )
    db.flush()
    return selected


def add_external_reference(
    db: Session,
    *,
    party_id: UUID,
    source_system: str,
    entity_type: str,
    external_id: str,
    metadata: dict | None = None,
) -> PartyExternalReference:
    party = _party(db, party_id)
    reference = PartyExternalReference(
        party_id=party.id,
        source_system=_required_text(source_system, "source_system").lower(),
        entity_type=_required_text(entity_type, "entity_type").lower(),
        external_id=_required_text(external_id, "external_id"),
        metadata_=dict(metadata) if metadata else None,
    )
    db.add(reference)
    db.flush()
    return reference
