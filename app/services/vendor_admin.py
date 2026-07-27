"""Vendor administration — the staff-side vendor lifecycle.

Sub had vendor *models* and a vendor *portal*, but no way for staff to manage
vendors: ``/admin/vendors`` 404'd, and there was no create/edit path at all.

**Vendor identity is split across two tables** and both must be written, or the
vendor is half-real:

* ``vendors`` (``Vendor``) is the native record. ``ProjectQuote.vendor_id`` and
  ``VendorPurchaseInvoice.vendor_id`` FK against it, so it is what quoting and
  invoicing see.
* ``field_vendors`` (``FieldVendor``) is what *authentication* resolves through:
  ``FieldVendorUser.system_user_id -> FieldVendor -> crm_vendor_id -> Vendor``
  (``app/services/field/vendor_auth.py``).

The two are bridged only by ``FieldVendor.crm_vendor_id`` — a ``String(64)``
holding the ``Vendor`` UUID, not a foreign key. So creating a ``Vendor`` alone
yields a vendor who can be quoted against but **cannot log in**, and creating a
``FieldVendor`` alone yields a login with no quoting identity. ``create`` below
always writes the pair and bridges them.

(``Vendor.users`` relates to ``VendorUser``, which has no consumers anywhere —
the live membership model is ``FieldVendorUser``. Do not wire new work to it.)

**Revocation invariant.** Deactivating a vendor is an authorization decision,
so it fails closed. ``vendor_auth`` gates portal login on
``FieldVendor.is_active``, which means a deactivation that does not reach the
twin revokes nothing while still reporting success. Because the bridge is a
nullable string rather than a foreign key, "no twin resolved" is not evidence
that no login exists — an imported vendor routinely carries one this service
cannot resolve. So a deactivation with no resolvable twin refuses outright when
an unbridged login still matches the vendor, and it refuses *before* mutating
the row so no caller can commit a revocation that never took effect. Repairing
the bridge is a deliberate staff act; guessing at it here would be guessing at
an authorization boundary.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.field_vendor import FieldVendor
from app.models.vendor_routes import Vendor
from app.services.common import coerce_uuid


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def get(db: Session, vendor_id: str | UUID) -> Vendor:
    vendor = db.get(Vendor, coerce_uuid(vendor_id))
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


def get_field_vendor(db: Session, vendor: Vendor) -> FieldVendor | None:
    """The auth-side twin of a native vendor, if it has been bridged."""
    return (
        db.query(FieldVendor)
        .filter(FieldVendor.crm_vendor_id == str(vendor.id))
        .one_or_none()
    )


def unbridged_twin_candidates(db: Session, vendor: Vendor) -> list[FieldVendor]:
    """Active ``FieldVendor`` rows that look like this vendor but are not
    bridged to it.

    ``FieldVendor.crm_vendor_id`` is a nullable ``String(64)``, not a foreign
    key, so an imported vendor can carry a login this service cannot resolve.
    Matching on the unique ``code`` or the contact email is enough to *detect*
    that case; it is deliberately not enough to act on it, because guessing at
    an authorization boundary is how access silently survives revocation.
    """
    matches = []
    if vendor.code:
        matches.append(FieldVendor.code == vendor.code)
    if vendor.contact_email:
        matches.append(FieldVendor.contact_email == vendor.contact_email)
    if not matches:
        return []
    return (
        db.query(FieldVendor)
        .filter(FieldVendor.is_active.is_(True))
        .filter(
            or_(
                FieldVendor.crm_vendor_id.is_(None),
                FieldVendor.crm_vendor_id != str(vendor.id),
            )
        )
        .filter(or_(*matches))
        .all()
    )


def count(
    db: Session, *, search: str | None = None, is_active: bool | None = None
) -> int:
    query = db.query(func.count(Vendor.id))
    query = _apply_filters(query, search=search, is_active=is_active)
    return int(query.scalar() or 0)


def _apply_filters(query, *, search: str | None, is_active: bool | None):
    if is_active is not None:
        query = query.filter(Vendor.is_active.is_(is_active))
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Vendor.name.ilike(like),
                Vendor.code.ilike(like),
                Vendor.contact_name.ilike(like),
                Vendor.contact_email.ilike(like),
            )
        )
    return query


def list_vendors(
    db: Session,
    *,
    search: str | None = None,
    is_active: bool | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[Vendor]:
    query = db.query(Vendor)
    query = _apply_filters(query, search=search, is_active=is_active)
    return query.order_by(Vendor.name.asc()).limit(limit).offset(offset).all()


def _assert_code_free(
    db: Session, code: str | None, *, exclude_id: UUID | None
) -> None:
    """``Vendor.code`` and ``FieldVendor.code`` are both unique. Check *both*
    before writing so a clash surfaces as a form error, not an IntegrityError
    500 — an imported twin can hold a code its native vendor never claimed."""
    if not code:
        return
    query = db.query(Vendor.id).filter(Vendor.code == code)
    if exclude_id is not None:
        query = query.filter(Vendor.id != exclude_id)
    if query.first() is not None:
        raise ValueError(f"Vendor code '{code}' is already in use.")

    twin_query = db.query(FieldVendor.id).filter(FieldVendor.code == code)
    if exclude_id is not None:
        twin_query = twin_query.filter(
            or_(
                FieldVendor.crm_vendor_id.is_(None),
                FieldVendor.crm_vendor_id != str(exclude_id),
            )
        )
    if twin_query.first() is not None:
        raise ValueError(f"Vendor code '{code}' is already in use.")


def create(
    db: Session,
    *,
    name: str | None,
    code: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    license_number: str | None = None,
    service_area: str | None = None,
    notes: str | None = None,
    is_active: bool = True,
) -> Vendor:
    """Create the native vendor *and* its auth-side twin, bridged.

    Writing only the ``Vendor`` row would produce a vendor who can be quoted
    against but can never log into the portal — see the module docstring.
    """
    clean_name = _clean(name)
    if not clean_name:
        raise ValueError("Vendor name is required.")

    clean_code = _clean(code)
    _assert_code_free(db, clean_code, exclude_id=None)

    vendor = Vendor(
        name=clean_name,
        code=clean_code,
        contact_name=_clean(contact_name),
        contact_email=_clean(contact_email),
        contact_phone=_clean(contact_phone),
        license_number=_clean(license_number),
        service_area=_clean(service_area),
        notes=_clean(notes),
        is_active=is_active,
    )
    db.add(vendor)
    db.flush()  # need vendor.id to bridge

    db.add(
        FieldVendor(
            crm_vendor_id=str(vendor.id),
            name=vendor.name,
            code=vendor.code,
            contact_name=vendor.contact_name,
            contact_email=vendor.contact_email,
            contact_phone=vendor.contact_phone,
            service_area=vendor.service_area,
            is_active=vendor.is_active,
        )
    )
    return vendor


def create_committed(db: Session, **fields: Any) -> Vendor:
    vendor = create(db, **fields)
    db.commit()
    db.refresh(vendor)
    return vendor


def update(
    db: Session,
    vendor_id: str | UUID,
    *,
    name: str | None = None,
    code: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    license_number: str | None = None,
    service_area: str | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
) -> Vendor:
    vendor = get(db, vendor_id)

    clean_name = _clean(name)
    if name is not None and not clean_name:
        raise ValueError("Vendor name is required.")

    clean_code = _clean(code)
    if code is not None:
        _assert_code_free(db, clean_code, exclude_id=vendor.id)

    twin = get_field_vendor(db, vendor)
    if is_active is False and twin is None:
        # Revocation with no resolvable twin: fail closed, and do it *before*
        # touching the row. A missing bridge is not evidence that no login
        # exists, and reporting success here would tell staff access was
        # withdrawn when it was not.
        _assert_no_unbridged_login(db, vendor)

    if clean_name is not None:
        vendor.name = clean_name
    if code is not None:
        vendor.code = clean_code
    if contact_name is not None:
        vendor.contact_name = _clean(contact_name)
    if contact_email is not None:
        vendor.contact_email = _clean(contact_email)
    if contact_phone is not None:
        vendor.contact_phone = _clean(contact_phone)
    if license_number is not None:
        vendor.license_number = _clean(license_number)
    if service_area is not None:
        vendor.service_area = _clean(service_area)
    if notes is not None:
        vendor.notes = _clean(notes)
    if is_active is not None:
        vendor.is_active = is_active

    # Keep the auth-side twin in step, so deactivating a vendor in admin
    # actually revokes their portal login (vendor_auth filters on
    # FieldVendor.is_active) rather than only hiding them from staff.
    if twin is not None:
        twin.name = vendor.name
        twin.code = vendor.code
        twin.contact_name = vendor.contact_name
        twin.contact_email = vendor.contact_email
        twin.contact_phone = vendor.contact_phone
        twin.service_area = vendor.service_area
        twin.is_active = vendor.is_active

    return vendor


def _assert_no_unbridged_login(db: Session, vendor: Vendor) -> None:
    candidates = unbridged_twin_candidates(db, vendor)
    if not candidates:
        return
    names = ", ".join(sorted(candidate.name for candidate in candidates))
    raise ValueError(
        f"Vendor '{vendor.name}' cannot be deactivated: an unlinked field-vendor "
        f"login ({names}) still matches this vendor. Link it to this vendor "
        "first, or the portal access would survive deactivation."
    )


def update_committed(db: Session, vendor_id: str | UUID, **fields: Any) -> Vendor:
    vendor = update(db, vendor_id, **fields)
    db.commit()
    db.refresh(vendor)
    return vendor


def deactivate(db: Session, vendor_id: str | UUID) -> Vendor:
    """Soft-delete: vendors are referenced by quotes and purchase invoices, so
    a hard delete would orphan financial history."""
    return update(db, vendor_id, is_active=False)


def deactivate_committed(db: Session, vendor_id: str | UUID) -> Vendor:
    vendor = deactivate(db, vendor_id)
    db.commit()
    db.refresh(vendor)
    return vendor
