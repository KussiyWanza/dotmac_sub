"""Vendor logins, and what a vendor role may actually do.

Before this, nothing anywhere created a ``FieldVendorUser`` — the row
``field.vendor_auth`` resolves through — so the vendor portal could not be
entered by anyone. And every authenticated vendor user had identical
capability: ``FieldVendorUser.role`` was free text that no decision read.

These tests pin the provisioning path and the capability model that replaces
"any vendor user can do anything".
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.auth import UserCredential
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import vendor_user_provisioning as provisioning
from app.services.db_session_adapter import db_session_adapter
from app.services.field import vendor_capabilities as caps
from app.services.field.vendor_auth import (
    VendorLoginEligibilityQuery,
    VendorLoginEligibilityStatus,
    resolve_vendor_login_eligibility,
)
from app.services.operator_tenant import provision_operator_tenant


def _vendor(db_session, *, is_active: bool = True) -> FieldVendor:
    vendor = FieldVendor(
        name="Abuja Trenching",
        code=f"AT-{uuid4().hex[:8]}",
        is_active=is_active,
    )
    db_session.add(vendor)
    db_session.commit()
    return vendor


def _command(vendor, *, role=None, email=None) -> provisioning.ProvisionVendorUser:
    return provisioning.ProvisionVendorUser(
        field_vendor_id=vendor.id,
        first_name="Ada",
        last_name="Obi",
        email=email or f"ada-{uuid4().hex[:8]}@vendor.example",
        role=role,
    )


def _provision(db_session, vendor, **kwargs):
    """Build the command first, then hand the owner a clean session.

    Owner commands require a transaction-free session at entry, and touching
    any ORM attribute re-opens a read transaction — so the command must be
    fully constructed before the release, not in the call arguments.
    """
    command = _command(vendor, **kwargs)
    db_session_adapter.release_read_transaction(db_session)
    return provisioning.provision_committed(db_session, command)


@pytest.fixture(autouse=True)
def _vendor_identity_operator_tenant(db_session):
    provision_operator_tenant(db_session)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def test_provisioning_creates_the_whole_login_or_none_of_it(db_session):
    """A vendor login is three rows. Any subset is a broken identity: a
    principal with no vendor, or a membership nobody can authenticate as."""
    vendor = _vendor(db_session)

    membership = _provision(db_session, vendor, role="owner")

    assert membership.vendor_id == vendor.id
    assert membership.role == "owner"
    assert membership.is_active is True
    principal = db_session.get(SystemUser, membership.system_user_id)
    assert principal.user_type is UserType.vendor
    credential = (
        db_session.query(UserCredential)
        .filter(UserCredential.system_user_id == principal.id)
        .one()
    )
    # Same shape as staff provisioning: no usable secret is minted here.
    assert credential.must_change_password is True
    assert principal.person_party_id is not None
    assert credential.party_id == principal.person_party_id
    assert credential.authentication_binding_id is not None
    assert credential.tenant_id is not None


def test_provisioned_vendor_login_is_vendor_eligible(db_session):
    vendor = _vendor(db_session)
    email = f"eligible-{uuid4().hex[:8]}@vendor.example"
    membership = _provision(db_session, vendor, email=email)

    result = resolve_vendor_login_eligibility(
        db_session,
        VendorLoginEligibilityQuery(identifier=email),
    )

    assert result.status is VendorLoginEligibilityStatus.ELIGIBLE
    assert result.system_user_id == membership.system_user_id
    assert result.vendor_user_id == membership.id
    assert result.vendor_id == vendor.id


def test_vendor_principals_are_distinguishable_from_staff(db_session):
    """The whole point of the marker: vendors authenticate through the same
    table as employees, so without it staff screens and grants cannot tell
    them apart."""
    vendor = _vendor(db_session)
    _provision(db_session, vendor)

    staff = SystemUser(
        first_name="Staff",
        last_name="Member",
        display_name="Staff Member",
        email=f"staff-{uuid4().hex[:8]}@dotmac.example",
    )
    db_session.add(staff)
    db_session.commit()

    vendor_principals = (
        db_session.query(SystemUser)
        .filter(SystemUser.user_type == UserType.vendor)
        .all()
    )
    assert [p.id for p in vendor_principals] != []
    assert staff.id not in {p.id for p in vendor_principals}
    assert staff.user_type is UserType.system_user


def test_an_email_already_in_use_is_refused(db_session):
    """`system_users.email` is unique across staff and vendors alike, so a
    collision is an identity question — never silently attach a vendor
    membership to an existing (possibly employee) principal."""
    vendor = _vendor(db_session)
    email = f"shared-{uuid4().hex[:8]}@example.com"
    db_session.add(
        SystemUser(
            first_name="Existing",
            last_name="Person",
            display_name="Existing Person",
            email=email,
        )
    )
    db_session.commit()

    with pytest.raises(provisioning.VendorUserProvisioningError) as exc:
        _provision(db_session, vendor, email=email)

    assert exc.value.code == "email_in_use"


def test_an_inactive_vendor_cannot_gain_a_login(db_session):
    """Otherwise provisioning would re-open access staff deliberately
    withdrew."""
    vendor = _vendor(db_session, is_active=False)

    with pytest.raises(provisioning.VendorUserProvisioningError) as exc:
        _provision(db_session, vendor)

    assert exc.value.code == "vendor_inactive"


def test_an_unknown_role_is_refused_at_provisioning(db_session):
    vendor = _vendor(db_session)

    with pytest.raises(provisioning.VendorUserProvisioningError) as exc:
        _provision(db_session, vendor, role="admin")

    assert exc.value.code == "unknown_role"


def test_revoking_a_login_disables_both_rows(db_session):
    """A live principal with no active membership is still an authenticable
    account — the same half-revocation that made vendor deactivation unsafe."""
    vendor = _vendor(db_session)
    membership = _provision(db_session, vendor)
    membership_id = membership.id

    db_session_adapter.release_read_transaction(db_session)
    provisioning.revoke_committed(db_session, membership_id)

    revoked = db_session.get(FieldVendorUser, membership_id)
    assert revoked.is_active is False
    assert db_session.get(SystemUser, revoked.system_user_id).is_active is False


# ---------------------------------------------------------------------------
# Capability model
# ---------------------------------------------------------------------------


def test_only_the_owner_may_commit_money_out(db_session):
    """Quoting and invoicing bind the organisation financially; evidence and
    execution do not. That split is the reason roles exist at all."""
    assert caps.INVOICE_WRITE in caps.capabilities_for_role("owner")
    assert caps.INVOICE_WRITE not in caps.capabilities_for_role("supervisor")
    assert caps.INVOICE_WRITE not in caps.capabilities_for_role("field")

    assert caps.QUOTE_WRITE in caps.capabilities_for_role("supervisor")
    assert caps.QUOTE_WRITE not in caps.capabilities_for_role("field")


def test_every_role_can_record_what_happened_on_site(db_session):
    for role in ("owner", "supervisor", "field"):
        role_caps = caps.capabilities_for_role(role)
        assert caps.AS_BUILT_WRITE in role_caps
        assert caps.PROJECT_EXECUTE in role_caps
        assert caps.PROJECT_READ in role_caps


def test_an_unrecognised_role_falls_to_least_privilege(db_session):
    """Imported and legacy rows carry arbitrary strings. A bad value must not
    grant more than intended."""
    assert caps.normalize_role("Site Admin") == "field"
    assert caps.normalize_role(None) == "field"
    assert caps.normalize_role("") == "field"
    assert caps.INVOICE_WRITE not in caps.capabilities_for_role("Site Admin")


def test_capabilities_resolve_from_a_live_auth_context(db_session):
    """`vendor_role` was previously copied into the auth context and never
    read. This asserts it now decides something."""
    vendor = _vendor(db_session)
    membership = _provision(db_session, vendor, role="supervisor")

    context = {"vendor_role": membership.role}

    assert caps.has_capability(context, caps.QUOTE_WRITE) is True
    assert caps.has_capability(context, caps.INVOICE_WRITE) is False


def test_role_changes_take_effect_through_the_owner(db_session):
    vendor = _vendor(db_session)
    membership = _provision(db_session, vendor)
    assert caps.INVOICE_WRITE not in caps.capabilities_for_role(membership.role)
    membership_id = membership.id

    db_session_adapter.release_read_transaction(db_session)
    provisioning.set_role_committed(db_session, membership_id, "owner")

    db_session.refresh(membership)
    assert caps.INVOICE_WRITE in caps.capabilities_for_role(membership.role)


def test_enable_login_repairs_legacy_unprojected_vendor_user(db_session):
    vendor = _vendor(db_session)
    principal = SystemUser(
        first_name="Legacy",
        last_name="Vendor",
        display_name="Legacy Vendor",
        email=f"legacy-{uuid4().hex[:8]}@vendor.example",
        user_type=UserType.vendor,
        is_active=True,
    )
    db_session.add(principal)
    db_session.flush()
    credential = UserCredential(
        system_user_id=principal.id,
        username=principal.email,
        password_hash="not-used",
        must_change_password=True,
        is_active=True,
    )
    membership = FieldVendorUser(
        vendor_id=vendor.id,
        system_user_id=principal.id,
        role="owner",
        is_active=True,
    )
    db_session.add_all([credential, membership])
    db_session.commit()
    membership_id = membership.id

    db_session_adapter.release_read_transaction(db_session)
    outcome = provisioning.enable_login_committed(
        db_session,
        provisioning.EnableVendorUserLogin(membership_id=membership_id),
    )

    db_session.refresh(credential)
    db_session.refresh(principal)
    assert outcome.repaired_projection is True
    assert principal.person_party_id is not None
    assert credential.party_id == principal.person_party_id


def test_unknown_capability_is_a_programmer_error(db_session):
    """A transport gating on a capability that does not exist would produce a
    route nobody can reach — or a check that silently passes."""
    with pytest.raises(ValueError):
        caps.assert_known_capability("vendor:nope:write")

    assert caps.assert_known_capability(caps.QUOTE_WRITE) == caps.QUOTE_WRITE
