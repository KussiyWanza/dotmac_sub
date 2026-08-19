"""Admin vendor management — list, create, edit, deactivate.

Sub shipped a vendor portal and vendor quoting but never a way for staff to
*manage* vendors: ``/admin/vendors`` 404'd. This adds the missing surface.

Route ordering: two sibling routers already own literal paths under the same
``/vendors`` prefix (``vendor_routes`` -> ``/vendors/routes``,
``vendor_operations`` -> ``/vendors/operations``). They are included *before*
this router in ``app/web/admin/__init__.py`` so those literals keep winning
over ``/vendors/{vendor_id}``; within this module ``/vendors/new`` is likewise
declared above the id route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.vendor_routes import InstallationProjectStatus
from app.services import web_vendors as web_vendors_service
from app.services.auth_dependencies import can, require_permission
from app.services.domain_errors import DomainError

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendors", tags=["web-admin-vendors"])


def _ctx(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _error_detail(exc: Exception) -> str:
    return str(exc) or "Could not save the vendor."


def _actor_id(request: Request) -> str | None:
    auth = getattr(request.state, "auth", {}) or {}
    actor = auth.get("principal_id") or auth.get("person_id")
    return str(actor) if actor else None


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("inventory:read"))],
)
def vendors_list(
    request: Request,
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "vendors")
    context.update(
        web_vendors_service.build_vendors_list_context(
            db, search=search, status=status, page=page, per_page=per_page
        )
    )
    return templates.TemplateResponse("admin/vendors/index.html", context)


# Must stay above `/{vendor_id}` or "new" is captured as an id.
@router.get(
    "/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_new(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "vendors")
    context.update(web_vendors_service.build_vendor_new_context())
    return templates.TemplateResponse("admin/vendors/vendor_form.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_create(
    request: Request,
    name: str | None = Form(default=None),
    code: str | None = Form(default=None),
    contact_name: str | None = Form(default=None),
    contact_email: str | None = Form(default=None),
    contact_phone: str | None = Form(default=None),
    license_number: str | None = Form(default=None),
    service_area: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    fields = {
        "name": name,
        "code": code,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "license_number": license_number,
        "service_area": service_area,
        "notes": notes,
        "is_active": is_active,
    }
    try:
        vendor_id = web_vendors_service.create_vendor_from_form(db, **fields)
        return RedirectResponse(url=f"/admin/vendors/{vendor_id}", status_code=303)
    except ValueError as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "vendors")
    context.update(
        web_vendors_service.build_vendor_form_error_context(
            mode="create", vendor_id=None, **fields
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/vendors/vendor_form.html", context, status_code=400
    )


@router.get(
    "/{vendor_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("inventory:read"))],
)
def vendor_detail(
    request: Request,
    vendor_id: str,
    project_search: str | None = Query(default=None, max_length=120),
    project_status: InstallationProjectStatus | None = Query(default=None),
    project_page: int = Query(default=1, ge=1),
    project_per_page: int = Query(default=25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "vendors")
    context.update(
        web_vendors_service.build_vendor_detail_context(
            db,
            vendor_id=vendor_id,
            project_search=project_search,
            project_status=project_status,
            project_page=project_page,
            project_per_page=project_per_page,
            can_read_operations=can(request, "inventory:read"),
            can_read_routes=can(request, "network:fiber:read"),
            can_read_financials=can(request, "finance:ap:read"),
        )
    )
    return templates.TemplateResponse("admin/vendors/detail.html", context)


@router.get(
    "/{vendor_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_edit(request: Request, vendor_id: str, db: Session = Depends(get_db)):
    context = _ctx(request, db, "vendors")
    context.update(
        web_vendors_service.build_vendor_edit_context(db, vendor_id=vendor_id)
    )
    return templates.TemplateResponse("admin/vendors/vendor_form.html", context)


@router.post(
    "/{vendor_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_update(
    request: Request,
    vendor_id: str,
    name: str | None = Form(default=None),
    code: str | None = Form(default=None),
    contact_name: str | None = Form(default=None),
    contact_email: str | None = Form(default=None),
    contact_phone: str | None = Form(default=None),
    license_number: str | None = Form(default=None),
    service_area: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    fields = {
        "name": name,
        "code": code,
        "contact_name": contact_name,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "license_number": license_number,
        "service_area": service_area,
        "notes": notes,
        "is_active": is_active,
    }
    try:
        web_vendors_service.update_vendor_from_form(db, vendor_id=vendor_id, **fields)
        return RedirectResponse(url=f"/admin/vendors/{vendor_id}", status_code=303)
    except ValueError as exc:
        db.rollback()
        error = _error_detail(exc)

    context = _ctx(request, db, "vendors")
    context.update(
        web_vendors_service.build_vendor_form_error_context(
            mode="update", vendor_id=vendor_id, **fields
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/vendors/vendor_form.html", context, status_code=400
    )


def _detail_with_error(request: Request, db: Session, vendor_id: str, error: str):
    context = _ctx(request, db, "vendors")
    context.update(
        web_vendors_service.build_vendor_detail_context(
            db,
            vendor_id=vendor_id,
            can_read_operations=can(request, "inventory:read"),
            can_read_routes=can(request, "network:fiber:read"),
            can_read_financials=can(request, "finance:ap:read"),
        )
    )
    context["error"] = error
    return templates.TemplateResponse(
        "admin/vendors/detail.html", context, status_code=400
    )


@router.post(
    "/{vendor_id}/users",
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_user_create(
    request: Request,
    vendor_id: str,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    role: str = Form(default="field"),
    db: Session = Depends(get_db),
):
    try:
        web_vendors_service.add_vendor_user_from_form(
            db,
            vendor_id=vendor_id,
            first_name=first_name,
            last_name=last_name,
            email=email,
            role=role,
        )
    except (ValueError, DomainError, SQLAlchemyError) as exc:
        # The owner rejects before writing, so nothing to roll back here.
        return _detail_with_error(request, db, vendor_id, _error_detail(exc))
    return RedirectResponse(url=f"/admin/vendors/{vendor_id}", status_code=303)


@router.post(
    "/{vendor_id}/users/{membership_id}/revoke",
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_user_revoke(
    request: Request,
    vendor_id: str,
    membership_id: str,
    db: Session = Depends(get_db),
):
    try:
        web_vendors_service.revoke_vendor_user(db, membership_id=membership_id)
    except ValueError as exc:
        return _detail_with_error(request, db, vendor_id, _error_detail(exc))
    return RedirectResponse(url=f"/admin/vendors/{vendor_id}", status_code=303)


@router.post(
    "/{vendor_id}/users/{membership_id}/enable",
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_user_enable(
    request: Request,
    vendor_id: str,
    membership_id: str,
    db: Session = Depends(get_db),
):
    try:
        web_vendors_service.enable_vendor_user_login(
            db,
            membership_id=membership_id,
            actor_id=_actor_id(request),
        )
    except (ValueError, DomainError) as exc:
        return _detail_with_error(request, db, vendor_id, _error_detail(exc))
    return RedirectResponse(url=f"/admin/vendors/{vendor_id}", status_code=303)


@router.post(
    "/{vendor_id}/users/{membership_id}/setup-link",
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_user_setup_link(
    request: Request,
    vendor_id: str,
    membership_id: str,
    db: Session = Depends(get_db),
):
    try:
        web_vendors_service.send_vendor_user_setup_link(
            db,
            membership_id=membership_id,
            actor_id=_actor_id(request),
        )
    except (ValueError, DomainError) as exc:
        return _detail_with_error(request, db, vendor_id, _error_detail(exc))
    return RedirectResponse(url=f"/admin/vendors/{vendor_id}", status_code=303)


@router.post(
    "/{vendor_id}/delete",
    dependencies=[Depends(require_permission("inventory:write"))],
)
def vendor_delete(request: Request, vendor_id: str, db: Session = Depends(get_db)):
    # Soft delete -- quotes and purchase invoices FK against the vendor.
    try:
        web_vendors_service.deactivate_vendor(db, vendor_id)
    except ValueError as exc:
        # The owner refuses a deactivation it cannot make stick (an unlinked
        # field-vendor login would keep working). It rejects before touching
        # the row, so there is nothing for this adapter to roll back — and
        # owning a transaction here is exactly what the adapter boundary
        # forbids. Just render the refusal.
        return _detail_with_error(request, db, vendor_id, _error_detail(exc))
    return RedirectResponse(url="/admin/vendors", status_code=303)
