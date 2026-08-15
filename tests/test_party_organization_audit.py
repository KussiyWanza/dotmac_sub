from __future__ import annotations

import json
from pathlib import Path

from app.models.field_vendor import FieldVendor
from app.models.organization import Organization, OrganizationAccountType
from app.models.party import PartnerRoleKey, PartyRoleStatus, PartyRoleType, PartyType
from app.models.subscriber import Reseller
from app.models.vendor_routes import Vendor
from app.services import party as party_service
from app.services.party_organization_audit import (
    build_party_organization_profile_audit,
)


def test_profile_audit_reports_aggregate_binding_role_and_twin_state(db_session):
    raw_name = "Private Multi Role Organization"
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name=raw_name,
    )
    organization = Organization(
        name=raw_name,
        account_type=OrganizationAccountType.reseller.value,
    )
    reseller = Reseller(name=raw_name, contact_email="private@example.test")
    vendor = Vendor(name=raw_name)
    db_session.add_all((organization, reseller, vendor))
    db_session.flush()
    field_vendor = FieldVendor(crm_vendor_id=str(vendor.id), name=raw_name)
    db_session.add(field_vendor)
    db_session.flush()
    evidence = {
        "source": "reviewed_profile_worklist",
        "reason": "Protected evidence",
    }
    party_service.bind_organization_profile(
        db_session,
        organization_id=organization.id,
        party_id=identity.id,
        **evidence,
    )
    party_service.bind_reseller_profile(
        db_session,
        reseller_id=reseller.id,
        party_id=identity.id,
        **evidence,
    )
    party_service.bind_vendor_profiles(
        db_session,
        vendor_id=vendor.id,
        party_id=identity.id,
        **evidence,
    )
    for role_type, role_key in (
        (PartyRoleType.reseller, None),
        (PartyRoleType.vendor, None),
        (PartyRoleType.partner, PartnerRoleKey.infrastructure.value),
    ):
        party_service.assign_role(
            db_session,
            party_id=identity.id,
            role_type=role_type,
            role_key=role_key,
            status=PartyRoleStatus.active,
        )

    audit = build_party_organization_profile_audit(db_session)
    serialized = json.dumps(audit, sort_keys=True)

    assert audit["status"] == "installed"
    assert audit["profiles"] == {
        "organizations": {"total": 1, "bound": 1, "unbound": 0},
        "resellers": {"total": 1, "bound": 1, "unbound": 0},
        "vendors": {"total": 1, "bound": 1, "unbound": 0},
        "field_vendors": {"total": 1, "bound": 1, "unbound": 0},
    }
    assert audit["vendor_twin_bridge"]["aligned_bound_pairs"] == 1
    assert audit["vendor_twin_bridge"]["partial_party_binding"] == 0
    assert audit["role_coverage"] == {
        "bound_resellers_missing_reseller_role": 0,
        "bound_vendors_missing_vendor_role": 0,
        "parties_with_multiple_channel_roles": 1,
    }
    assert audit["legacy_organization_account_type"] == {
        "partner": 0,
        "reseller": 1,
        "vendor": 0,
    }
    assert audit["artifact_contract"] == {
        "read_only": True,
        "contains_identity_values": False,
        "automatic_role_assignment": False,
        "automatic_profile_binding": False,
    }
    assert raw_name not in serialized
    assert "private@example.test" not in serialized


def test_profile_audit_reports_bridge_debt_without_exposing_ids(db_session):
    vendor = Vendor(name="Missing Twin")
    invalid = FieldVendor(crm_vendor_id="not-a-uuid", name="Invalid Bridge")
    orphan = FieldVendor(
        crm_vendor_id="00000000-0000-0000-0000-000000000001",
        name="Orphan Bridge",
    )
    unbridged = FieldVendor(name="Unbridged")
    db_session.add_all((vendor, invalid, orphan, unbridged))
    db_session.flush()

    audit = build_party_organization_profile_audit(db_session)
    bridge = audit["vendor_twin_bridge"]

    assert bridge["missing_field_vendor_twin"] == 1
    assert bridge["invalid_uuid_bridge"] == 1
    assert bridge["orphan_uuid_bridge"] == 1
    assert bridge["unbridged_field_vendor"] == 1
    serialized = json.dumps(audit, sort_keys=True)
    assert str(vendor.id) not in serialized
    assert str(orphan.id) not in serialized


def test_audit_party_organization_profiles_uses_the_read_only_snapshot_seam():
    """Behavioural proof lives on PostgreSQL; this pins the wiring.

    The adapter previously issued `SET TRANSACTION ISOLATION LEVEL ...` itself,
    which could never be the first statement once the operator-tenant hook was
    installed, so the report raised on every real run. Asserting the statement
    text — as this test used to — pinned the broken form and kept CI green.
    """

    source = Path("scripts/migration/audit_party_organization_profiles.py").read_text(
        encoding="utf-8"
    )

    assert "read_only_snapshot_session" in source
    assert "SET TRANSACTION" not in source
