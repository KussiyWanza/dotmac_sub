"""Context builders for the admin vendor pages.

Thin-wrapper rule: ``app/web`` may not touch the session directly
(``tests/architecture/test_thin_wrappers.py``), so every read the vendor pages
need is assembled here and handed to the route as a plain dict.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.field_vendor import VENDOR_USER_ROLES
from app.models.vendor_routes import InstallationProjectStatus
from app.services import vendor_admin, vendor_user_provisioning
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.field import vendor_capabilities
from app.services.project_vendor_delivery import ProjectVendorDeliveryVisibility
from app.services.vendor_delivery_portfolio import (
    VendorPortfolioQuery,
    get_vendor_delivery_portfolio,
)


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "on", "yes"}


def _active_filter(value: str | None) -> bool | None:
    """The list filter is tri-state: active / inactive / all."""
    choice = (value or "").strip().lower()
    if choice == "active":
        return True
    if choice == "inactive":
        return False
    return None


def _vendor_form_fields(
    *,
    name: str | None = None,
    code: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    license_number: str | None = None,
    service_area: str | None = None,
    notes: str | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "name": (name or "").strip(),
        "code": (code or "").strip(),
        "contact_name": (contact_name or "").strip(),
        "contact_email": (contact_email or "").strip(),
        "contact_phone": (contact_phone or "").strip(),
        "license_number": (license_number or "").strip(),
        "service_area": (service_area or "").strip(),
        "notes": (notes or "").strip(),
        "is_active": is_active,
    }


def build_vendors_list_context(
    db: Session,
    *,
    search: str | None,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    is_active = _active_filter(status)
    offset = (page - 1) * per_page
    vendors = vendor_admin.list_vendors(
        db,
        search=search or None,
        is_active=is_active,
        limit=per_page,
        offset=offset,
    )
    total = vendor_admin.count(db, search=search or None, is_active=is_active)
    return {
        "vendors": vendors,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
        "search": search or "",
        "status": (status or "").strip(),
    }


def build_vendor_new_context() -> dict[str, Any]:
    return {
        "vendor": None,
        "vendor_form": _vendor_form_fields(),
        "form_title": "New Vendor",
        "submit_label": "Create Vendor",
        "action_url": "/admin/vendors",
        "error": None,
    }


def build_vendor_edit_context(db: Session, *, vendor_id: str) -> dict[str, Any]:
    vendor = vendor_admin.get(db, vendor_id)
    return {
        "vendor": vendor,
        "vendor_form": _vendor_form_fields(
            name=vendor.name,
            code=vendor.code,
            contact_name=vendor.contact_name,
            contact_email=vendor.contact_email,
            contact_phone=vendor.contact_phone,
            license_number=vendor.license_number,
            service_area=vendor.service_area,
            notes=vendor.notes,
            is_active=vendor.is_active,
        ),
        "form_title": "Edit Vendor",
        "submit_label": "Update Vendor",
        "action_url": f"/admin/vendors/{vendor_id}/edit",
        "error": None,
    }


def build_vendor_form_error_context(
    *, mode: str, vendor_id: str | None, is_active: str | None = None, **fields: Any
) -> dict[str, Any]:
    """Re-render the form with the operator's input intact after a failure.

    Takes the raw form values (``is_active`` still a string) so the route stays
    a thin pass-through and does not have to know the coercion rules.
    """
    editing = mode == "update"
    return {
        "vendor": None,
        "vendor_form": _vendor_form_fields(**fields, is_active=_as_bool(is_active)),
        "form_title": "Edit Vendor" if editing else "New Vendor",
        "submit_label": "Update Vendor" if editing else "Create Vendor",
        "action_url": (
            f"/admin/vendors/{vendor_id}/edit" if editing else "/admin/vendors"
        ),
    }


_CAPABILITY_LABELS = {
    vendor_capabilities.PROJECT_READ: "view projects",
    vendor_capabilities.PROJECT_EXECUTE: "start/complete work",
    vendor_capabilities.QUOTE_WRITE: "quote",
    vendor_capabilities.AS_BUILT_WRITE: "submit as-built",
    vendor_capabilities.INVOICE_WRITE: "invoice",
    vendor_capabilities.MATERIAL_REQUEST: "request materials",
    vendor_capabilities.ADVANCE_REQUEST: "request advances",
}


def _capability_summary(role: str | None) -> str:
    """Plain-language rendering of what a role may do.

    Staff choosing a role should not have to know the capability keys, and the
    summary is derived from the declaring owner so it cannot drift from what
    the routes actually enforce.
    """
    granted = vendor_capabilities.capabilities_for_role(role)
    return ", ".join(
        label
        for capability, label in _CAPABILITY_LABELS.items()
        if capability in granted
    )


def vendor_user_role_options() -> list[dict[str, str]]:
    return [
        {
            "value": role,
            "label": role.replace("_", " ").title(),
            "summary": _capability_summary(role),
        }
        for role in VENDOR_USER_ROLES
    ]


def _vendor_user_row(membership: Any) -> dict[str, Any]:
    principal = getattr(membership, "system_user", None)
    role = vendor_capabilities.normalize_role(membership.role)
    return {
        "id": membership.id,
        "role": role,
        "capability_summary": _capability_summary(role),
        "is_active": membership.is_active,
        "display_name": getattr(principal, "display_name", None),
        "email": getattr(principal, "email", None),
    }


def build_vendor_detail_context(
    db: Session,
    *,
    vendor_id: str,
    project_search: str | None = None,
    project_status: InstallationProjectStatus | None = None,
    project_page: int = 1,
    project_per_page: int = 25,
    can_read_operations: bool = False,
    can_read_routes: bool = False,
    can_read_financials: bool = False,
) -> dict[str, Any]:
    vendor = vendor_admin.get(db, vendor_id)
    field_vendor = vendor_admin.get_field_vendor(db, vendor)
    normalized_page = max(1, int(project_page))
    normalized_per_page = max(10, min(int(project_per_page), 100))

    def portfolio_query(page: int) -> VendorPortfolioQuery:
        return VendorPortfolioQuery(
            vendor_id=vendor.id,
            visibility=ProjectVendorDeliveryVisibility(
                can_read_operations=can_read_operations,
                can_read_routes=can_read_routes,
                can_read_financials=can_read_financials,
            ),
            search=project_search,
            status=project_status,
            limit=normalized_per_page,
            offset=(page - 1) * normalized_per_page,
        )

    portfolio = get_vendor_delivery_portfolio(
        db,
        portfolio_query(normalized_page),
    )
    project_pages = max(
        1,
        (portfolio.total + normalized_per_page - 1) // normalized_per_page,
    )
    if normalized_page > project_pages:
        normalized_page = project_pages
        portfolio = get_vendor_delivery_portfolio(
            db,
            portfolio_query(normalized_page),
        )
    return {
        "vendor": vendor,
        # Surfaced so staff can see at a glance whether this vendor can
        # actually sign in -- portal auth resolves through the FieldVendor
        # twin, not the native row.
        "field_vendor": field_vendor,
        "portal_login_enabled": bool(field_vendor and field_vendor.is_active),
        "vendor_users": [
            _vendor_user_row(membership)
            for membership in (list(field_vendor.users) if field_vendor else [])
        ],
        "vendor_user_roles": vendor_user_role_options(),
        "portfolio": portfolio,
        "project_search": project_search or "",
        "project_status": project_status.value if project_status else "",
        "project_page": normalized_page,
        "project_per_page": normalized_per_page,
        "project_pages": project_pages,
    }


def add_vendor_user_from_form(
    db: Session,
    *,
    vendor_id: str,
    first_name: str | None,
    last_name: str | None,
    email: str | None,
    role: str | None,
) -> None:
    """Create one portal login for the vendor behind this admin page."""
    vendor = vendor_admin.get(db, vendor_id)
    field_vendor = vendor_admin.get_field_vendor(db, vendor)
    if field_vendor is None:
        raise ValueError(
            "This vendor has no portal identity, so a login cannot be added."
        )
    db_session_adapter.release_read_transaction(db)
    vendor_user_provisioning.provision_committed(
        db,
        vendor_user_provisioning.ProvisionVendorUser(
            field_vendor_id=field_vendor.id,
            first_name=first_name or "",
            last_name=last_name or "",
            email=email or "",
            role=role,
        ),
    )


def revoke_vendor_user(db: Session, *, membership_id: str) -> None:
    db_session_adapter.release_read_transaction(db)
    # The route hands this in as a path string; the owner command takes a UUID.
    vendor_user_provisioning.revoke_committed(db, coerce_uuid(membership_id))


def create_vendor_from_form(
    db: Session,
    *,
    name: str | None,
    code: str | None,
    contact_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
    license_number: str | None,
    service_area: str | None,
    notes: str | None,
    is_active: str | None,
) -> str:
    vendor = vendor_admin.create_committed(
        db,
        name=name,
        code=code,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        license_number=license_number,
        service_area=service_area,
        notes=notes,
        is_active=_as_bool(is_active) if is_active is not None else True,
    )
    return str(vendor.id)


def update_vendor_from_form(
    db: Session,
    *,
    vendor_id: str,
    name: str | None,
    code: str | None,
    contact_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
    license_number: str | None,
    service_area: str | None,
    notes: str | None,
    is_active: str | None,
) -> None:
    vendor_admin.update_committed(
        db,
        vendor_id,
        name=name,
        code=code,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        license_number=license_number,
        service_area=service_area,
        notes=notes,
        is_active=_as_bool(is_active),
    )


def deactivate_vendor(db: Session, vendor_id: str) -> None:
    vendor_admin.deactivate_committed(db, vendor_id)
