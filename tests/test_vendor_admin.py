"""Staff-side vendor management.

The load-bearing fact here is that vendor identity is split across two tables:
``vendors`` (what quoting/invoicing FK against) and ``field_vendors`` (what
portal auth resolves through, via ``FieldVendor.crm_vendor_id``). Writing only
one of them yields a vendor that is half-real -- quotable but unable to log in,
or vice versa. These tests pin the bridge.
"""

from __future__ import annotations

import pytest

from app.models.field_vendor import FieldVendor
from app.models.vendor_routes import Vendor
from app.services import vendor_admin


def test_create_bridges_the_native_vendor_to_its_auth_twin(db_session):
    vendor = vendor_admin.create_committed(
        db_session,
        name="Kano Fibre Works",
        code="KFW",
        contact_email="ops@kfw.example",
    )

    twin = (
        db_session.query(FieldVendor)
        .filter(FieldVendor.crm_vendor_id == str(vendor.id))
        .one()
    )
    # The bridge is a stringly-typed id, not an FK -- so assert it explicitly.
    assert twin.crm_vendor_id == str(vendor.id)
    assert twin.name == "Kano Fibre Works"
    assert twin.is_active is True

    # And the resolver used by portal login finds it from the native row.
    assert vendor_admin.get_field_vendor(db_session, vendor) is twin


def test_deactivating_a_vendor_revokes_portal_login(db_session):
    """vendor_auth filters on FieldVendor.is_active. If deactivation only
    touched the native row, the vendor would vanish from staff screens while
    still being able to sign in."""
    vendor = vendor_admin.create_committed(db_session, name="Lagos Trenching")

    vendor_admin.deactivate_committed(db_session, vendor.id)

    twin = vendor_admin.get_field_vendor(db_session, vendor)
    assert vendor.is_active is False
    assert twin is not None
    assert twin.is_active is False


def test_update_keeps_the_auth_twin_in_step(db_session):
    vendor = vendor_admin.create_committed(
        db_session, name="Old Name", contact_email="old@example.com"
    )

    vendor_admin.update_committed(
        db_session,
        vendor.id,
        name="New Name",
        contact_email="new@example.com",
    )

    twin = vendor_admin.get_field_vendor(db_session, vendor)
    assert twin is not None
    assert twin.name == "New Name"
    assert twin.contact_email == "new@example.com"


def test_duplicate_code_is_a_form_error_not_an_integrity_crash(db_session):
    """``Vendor.code`` is unique. Surface the clash as a ValueError the form can
    render, rather than letting it reach the DB as a 500."""
    vendor_admin.create_committed(db_session, name="First", code="DUP")

    with pytest.raises(ValueError, match="already in use"):
        vendor_admin.create_committed(db_session, name="Second", code="DUP")


def test_duplicate_code_on_the_auth_twin_is_also_a_form_error(db_session):
    """``FieldVendor.code`` carries its own unique constraint. An imported twin
    can hold a code no native vendor claims, so checking only ``Vendor.code``
    still lets the write reach the DB as a 500."""
    db_session.add(FieldVendor(name="Imported Twin", code="ORPHAN"))
    db_session.commit()

    with pytest.raises(ValueError, match="already in use"):
        vendor_admin.create_committed(db_session, name="Native", code="ORPHAN")


def test_deactivation_fails_closed_when_a_login_is_not_bridged(db_session):
    """The dangerous case: a field-vendor login exists for this vendor but is
    not bridged, so the mirror silently no-ops and the vendor keeps signing in
    after staff 'deactivated' them. Refuse rather than report a false success."""
    vendor = vendor_admin.create_committed(
        db_session, name="Jos Fibre", code="JOS", contact_email="ops@jos.example"
    )
    # Break the bridge the way an import does: a live login this service's
    # crm_vendor_id lookup can no longer resolve.
    twin = vendor_admin.get_field_vendor(db_session, vendor)
    twin.crm_vendor_id = None
    db_session.commit()

    with pytest.raises(ValueError, match="cannot be deactivated"):
        vendor_admin.deactivate_committed(db_session, vendor.id)

    # The refusal must leave nothing half-applied: the guard runs before the
    # row is touched, so no caller can commit a deactivation that never took.
    assert db_session.get(Vendor, vendor.id).is_active is True
    assert db_session.get(FieldVendor, twin.id).is_active is True


def test_deactivation_proceeds_when_no_login_exists_at_all(db_session):
    """A vendor with no field-vendor row has no portal access to revoke, so the
    guard must not block an ordinary deactivation."""
    vendor = Vendor(name="Quote Only", code="QO")
    db_session.add(vendor)
    db_session.commit()

    vendor_admin.deactivate_committed(db_session, vendor.id)

    assert db_session.get(Vendor, vendor.id).is_active is False


def test_update_may_keep_its_own_code(db_session):
    """The uniqueness check must exclude the row being edited, or saving a
    vendor without changing its code would falsely report a clash."""
    vendor = vendor_admin.create_committed(db_session, name="Solo", code="SOLO")

    vendor_admin.update_committed(
        db_session, vendor.id, name="Solo Renamed", code="SOLO"
    )

    assert db_session.get(Vendor, vendor.id).name == "Solo Renamed"


def test_name_is_required(db_session):
    with pytest.raises(ValueError, match="name is required"):
        vendor_admin.create_committed(db_session, name="   ")


def test_list_filters_by_status_and_search(db_session):
    vendor_admin.create_committed(db_session, name="Abuja Fibre", code="AF")
    inactive = vendor_admin.create_committed(db_session, name="Zaria Cables", code="ZC")
    vendor_admin.deactivate_committed(db_session, inactive.id)

    active_only = vendor_admin.list_vendors(db_session, is_active=True)
    assert [v.name for v in active_only] == ["Abuja Fibre"]

    found = vendor_admin.list_vendors(db_session, search="zaria")
    assert [v.name for v in found] == ["Zaria Cables"]

    assert vendor_admin.count(db_session, is_active=True) == 1
    assert vendor_admin.count(db_session) == 2
