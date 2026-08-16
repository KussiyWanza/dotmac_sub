from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.organization import (
    Organization,
    OrganizationMembership,
    OrganizationMembershipRole,
)
from app.models.party import PartyMembershipStatus, PartyMembershipType, PartyType
from app.models.subscriber import Reseller, ResellerUser, UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services import party as party_service
from app.services.party_principal_audit import build_party_principal_context_audit

_EVIDENCE = {
    "source": "reviewed_principal_context_worklist",
    "reason": "Protected review evidence",
}


def test_principal_context_audit_reports_only_aggregate_convergence(db_session):
    baseline = build_party_principal_context_audit(db_session)["system_user_principals"]
    private_name = "Private Principal Name"
    private_email = "private-principal@example.test"
    person = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name=private_name,
    )
    organization_party = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Private Organization Name",
    )
    system_user = SystemUser(
        first_name="Private",
        last_name="Principal",
        email=private_email,
        user_type=UserType.system_user,
    )
    reseller = Reseller(name="Private Reseller")
    organization = Organization(name="Private Organization Name")
    organization_membership = OrganizationMembership(
        organization=organization,
        person_id=uuid.uuid4(),
        role=OrganizationMembershipRole.member.value,
    )
    vendor = Vendor(name="Private Vendor")
    db_session.add_all(
        (
            system_user,
            reseller,
            organization,
            organization_membership,
            vendor,
        )
    )
    db_session.flush()
    reseller_user = ResellerUser(
        reseller_id=reseller.id,
        email="private-reseller@example.test",
        full_name=private_name,
    )
    field_vendor = FieldVendor(name="Private Vendor", crm_vendor_id=str(vendor.id))
    db_session.add_all((reseller_user, field_vendor))
    db_session.flush()
    field_vendor_user = FieldVendorUser(
        vendor_id=field_vendor.id,
        system_user_id=system_user.id,
        crm_vendor_user_id=str(uuid.uuid4()),
    )
    db_session.add(field_vendor_user)
    db_session.flush()
    party_service.bind_system_user_principal(
        db_session,
        system_user_id=system_user.id,
        person_party_id=person.id,
        **_EVIDENCE,
    )
    party_service.bind_reseller_profile(
        db_session,
        reseller_id=reseller.id,
        party_id=organization_party.id,
        **_EVIDENCE,
    )
    party_service.bind_organization_profile(
        db_session,
        organization_id=organization.id,
        party_id=organization_party.id,
        **_EVIDENCE,
    )
    party_service.bind_vendor_profiles(
        db_session,
        vendor_id=vendor.id,
        party_id=organization_party.id,
        **_EVIDENCE,
    )
    reseller_membership = party_service.add_membership(
        db_session,
        person_party_id=person.id,
        organization_party_id=organization_party.id,
        membership_type=PartyMembershipType.reseller_admin,
        status=PartyMembershipStatus.invited,
    )
    organization_party_membership = party_service.add_membership(
        db_session,
        person_party_id=person.id,
        organization_party_id=organization_party.id,
        membership_type=PartyMembershipType.member,
        status=PartyMembershipStatus.invited,
    )
    vendor_membership = party_service.add_membership(
        db_session,
        person_party_id=person.id,
        organization_party_id=organization_party.id,
        membership_type=PartyMembershipType.vendor_user,
        status=PartyMembershipStatus.invited,
    )
    party_service.bind_reseller_user_principal(
        db_session,
        reseller_user_id=reseller_user.id,
        person_party_id=person.id,
        party_membership_id=reseller_membership.id,
        **_EVIDENCE,
    )
    party_service.bind_organization_membership_context(
        db_session,
        organization_membership_id=organization_membership.id,
        party_membership_id=organization_party_membership.id,
        **_EVIDENCE,
    )
    party_service.bind_field_vendor_user_context(
        db_session,
        field_vendor_user_id=field_vendor_user.id,
        party_membership_id=vendor_membership.id,
        **_EVIDENCE,
    )

    audit = build_party_principal_context_audit(db_session)
    serialized = json.dumps(audit, sort_keys=True)

    assert audit["status"] == "installed"
    assert audit["system_user_principals"] == {
        **baseline,
        "total": baseline["total"] + 1,
        "bound": baseline["bound"] + 1,
    }
    assert audit["reseller_user_principals"]["aligned"] == 1
    assert audit["organization_membership_contexts"]["aligned"] == 1
    assert audit["field_vendor_user_contexts"]["aligned"] == 1
    assert audit["party_memberships"] == {
        "total": 3,
        "missing_or_non_person_endpoint": 0,
        "missing_or_non_organization_endpoint": 0,
    }
    assert audit["artifact_contract"] == {
        "read_only": True,
        "contains_identity_values": False,
        "automatic_party_binding": False,
        "automatic_membership_creation": False,
        "changes_authentication_or_authorization": False,
    }
    assert private_name not in serialized
    assert private_email not in serialized
    assert str(person.id) not in serialized


def test_principal_context_audit_reports_unbound_and_bridge_debt(db_session):
    baseline_unbound = build_party_principal_context_audit(db_session)[
        "system_user_principals"
    ]["unbound"]
    system_user = SystemUser(
        first_name="Unbound",
        last_name="User",
        email="unbound@example.test",
        user_type=UserType.system_user,
    )
    invalid_field_vendor = FieldVendor(name="Invalid User Bridge")
    db_session.add_all((system_user, invalid_field_vendor))
    db_session.flush()
    invalid_field_user = FieldVendorUser(
        vendor_id=invalid_field_vendor.id,
        system_user_id=system_user.id,
        crm_vendor_user_id="not-a-uuid",
    )
    db_session.add(invalid_field_user)
    db_session.flush()

    audit = build_party_principal_context_audit(db_session)

    assert audit["system_user_principals"]["unbound"] == baseline_unbound + 1
    assert audit["field_vendor_user_contexts"]["unbound"] == 1
    assert audit["field_vendor_user_contexts"]["invalid_vendor_profile_bridge"] == 1


def test_audit_party_principal_contexts_uses_the_read_only_snapshot_seam():
    """Behavioural proof lives on PostgreSQL; this pins the wiring.

    The adapter previously issued `SET TRANSACTION ISOLATION LEVEL ...` itself,
    which could never be the first statement once the operator-tenant hook was
    installed, so the report raised on every real run. Asserting the statement
    text — as this test used to — pinned the broken form and kept CI green.
    """

    source = Path("scripts/migration/audit_party_principal_contexts.py").read_text(
        encoding="utf-8"
    )

    assert "read_only_snapshot_session" in source
    assert "SET TRANSACTION" not in source
