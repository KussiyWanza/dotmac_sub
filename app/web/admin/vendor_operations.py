"""Admin review workspace for vendor projects, quotes, and purchase invoices."""

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import (
    vendor_as_built_review_proposals,
    vendor_project_review_proposals,
    vendor_route_review_proposals,
    vendor_supply_review_proposals,
)
from app.services.audit_helpers import resolve_actor_display_names
from app.services.auth_dependencies import (
    can,
    require_any_permission,
    require_permission,
)
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.file_storage import build_content_disposition
from app.services.owner_commands import CommandContext
from app.services.vendor_as_built_review_proposals import (
    ConfirmVendorAsBuiltReviewCommand,
)
from app.services.vendor_portal_operations import (
    ConfigureVendorProcurementCommand,
    ReviewVendorQuoteCommand,
    vendor_portal_operations,
)
from app.services.vendor_project_review_proposals import (
    ConfirmVendorProjectReviewCommand,
)
from app.services.vendor_purchase_invoices import (
    ReviewVendorPurchaseInvoiceCommand,
    vendor_purchase_invoices,
)
from app.services.vendor_route_review_proposals import (
    ConfirmVendorRouteReviewCommand,
)
from app.services.vendor_supply_review_proposals import (
    ConfirmVendorSupplyReviewCommand,
)
from app.services.vendor_supply_views import (
    VendorSupplyReviewAction,
    VendorSupplyType,
    advance_disbursement_queue,
    advance_review_queue,
    material_review_queue,
)

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendors/operations", tags=["web-admin-vendor-operations"])
_read = require_any_permission("inventory:read", "finance:ap:read")
_write = require_any_permission("inventory:write", "finance:ap:write")
_project_write = require_permission("inventory:write")


def _ctx(request: Request, db: Session) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": "vendor-operations",
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", {}) or {}
    return str(auth.get("principal_id") or auth.get("person_id") or "")


def _supply_action(value: str) -> VendorSupplyReviewAction:
    try:
        return VendorSupplyReviewAction(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown action") from exc


def _review_context(request: Request, *, quote_id: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=_actor(request),
        scope=quote_id,
        reason="vendor_quote_review",
    )


def _invoice_review_context(request: Request, *, invoice_id: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=_actor(request),
        scope=invoice_id,
        reason="vendor_purchase_invoice_review",
    )


def _staff_confirmation_context(
    request: Request,
    *,
    scope: str,
    reason: str,
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=_actor(request),
        scope=scope,
        reason=reason,
    )


def _quote_error(exc: DomainError) -> HTTPException:
    suffix = exc.code.rsplit(".", 1)[-1]
    status_code = 404 if suffix.endswith("not_found") else 409
    return HTTPException(status_code=status_code, detail=exc.message)


@router.get("", response_class=HTMLResponse)
def vendor_operations_queue(
    request: Request,
    message: str | None = None,
    q: str | None = Query(default=None, max_length=120),
    _auth: dict = Depends(_read),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db)
    search = (q or "").strip()
    show_field_reviews = can(request, "inventory:read")
    show_route_reviews = show_field_reviews and can(request, "network:fiber:read")
    show_financial_reviews = can(request, "inventory:read") or can(
        request,
        "finance:ap:read",
    )
    show_advance_reviews = can(request, "finance:ap:read")
    context.update(
        {
            "draft_projects": (
                vendor_portal_operations.list_draft_projects(db, search=search)
                if show_field_reviews
                else []
            ),
            "active_vendors": (
                vendor_portal_operations.list_active_vendors(db)
                if show_field_reviews
                else []
            ),
            "message": message,
            "queue_search": search,
            "show_field_reviews": show_field_reviews,
            "show_route_reviews": show_route_reviews,
            "show_financial_reviews": show_financial_reviews,
            "show_advance_reviews": show_advance_reviews,
            "material_releases": (
                material_review_queue(db).items if show_field_reviews else ()
            ),
            "advances": (
                advance_review_queue(db).items if show_advance_reviews else ()
            ),
            "advances_awaiting_disbursement": (
                advance_disbursement_queue(db).items if show_advance_reviews else ()
            ),
            "projects": (
                vendor_portal_operations.list_reviewable_projects(db)
                if show_field_reviews
                else []
            ),
            "route_revisions": (
                vendor_portal_operations.list_reviewable_route_revisions(db)
                if show_route_reviews
                else []
            ),
            "as_builts": (
                vendor_portal_operations.list_reviewable_as_builts(db)
                if show_field_reviews
                else []
            ),
            "quotes": (
                vendor_portal_operations.list_reviewable_quotes(db, search=search)
                if show_field_reviews
                else []
            ),
            "invoices": (
                vendor_purchase_invoices.list(
                    db,
                    status="submitted",
                    limit=200,
                    offset=0,
                )
                if show_financial_reviews
                else []
            ),
        }
    )
    return templates.TemplateResponse("admin/vendors/operations.html", context)


@router.post("/projects/{project_id}/procurement")
def configure_vendor_procurement(
    request: Request,
    project_id: str,
    mode: str = Form(...),
    vendor_id: str | None = Form(None),
    bidding_close_at: datetime | None = Form(None),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    context = _staff_confirmation_context(
        request, scope=project_id, reason="vendor_procurement_configuration"
    )
    db_session_adapter.release_read_transaction(db)
    try:
        vendor_portal_operations.configure_procurement(
            db,
            ConfigureVendorProcurementCommand(
                context=context,
                project_id=project_id,
                mode=mode,
                vendor_id=vendor_id,
                bidding_close_at=bidding_close_at,
            ),
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
    return RedirectResponse(
        "/admin/vendors/operations?message=Vendor+procurement+configured",
        status_code=303,
    )


@router.get("/quotes/{quote_id}", response_class=HTMLResponse)
def vendor_quote_review_detail(
    request: Request,
    quote_id: str,
    message: str | None = None,
    _auth: dict = Depends(_read),
    db: Session = Depends(get_db),
):
    try:
        quote = vendor_portal_operations.get_quote_for_review(db, quote_id)
    except DomainError as exc:
        raise _quote_error(exc) from exc
    context = _ctx(request, db)
    context.update(
        {
            "quote": quote,
            "message": message,
            "can_review_operations": can(request, "inventory:write")
            or can(request, "finance:ap:write"),
        }
    )
    return templates.TemplateResponse(
        "admin/vendors/quote_review_detail.html",
        context,
    )


@router.get("/as-built/{as_built_id}", response_class=HTMLResponse)
def vendor_as_built_review_detail(
    request: Request,
    as_built_id: str,
    message: str | None = None,
    _auth: dict = Depends(require_permission("inventory:read")),
    db: Session = Depends(get_db),
):
    try:
        as_built = vendor_portal_operations.get_as_built_review(db, as_built_id)
    except DomainError as exc:
        raise _quote_error(exc) from exc
    from app.services import vendor_routes_api

    actor_labels = resolve_actor_display_names(
        db,
        {
            event.get("actor_id")
            for event in as_built.get("review_events", ())
            if event.get("actor_id")
        },
    )
    for event in as_built.get("review_events", ()):
        event["actor_name"] = actor_labels.get(
            str(event.get("actor_id") or ""), "System"
        )

    context = _ctx(request, db)
    context.update(
        {
            "as_built": as_built,
            "message": message,
            "route_geojson": vendor_routes_api.build_as_built_route_geojson(
                db,
                as_built_id,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/vendors/as_built_review_detail.html",
        context,
    )


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def vendor_invoice_review_detail(
    request: Request,
    invoice_id: str,
    message: str | None = None,
    _auth: dict = Depends(_read),
    db: Session = Depends(get_db),
):
    try:
        invoice = vendor_purchase_invoices.get(db, invoice_id)
    except DomainError as exc:
        raise _quote_error(exc) from exc
    context = _ctx(request, db)
    context.update(
        {
            "invoice": invoice,
            "message": message,
            "can_review_operations": can(request, "inventory:write")
            or can(request, "finance:ap:write"),
            "show_payment_details": can(request, "finance:ap:read"),
        }
    )
    return templates.TemplateResponse(
        "admin/vendors/invoice_review_detail.html",
        context,
    )


@router.get("/invoices/{invoice_id}/attachment")
def download_vendor_invoice_attachment(
    invoice_id: str,
    _auth: dict = Depends(_read),
    db: Session = Depends(get_db),
):
    try:
        file, stream = vendor_purchase_invoices.attachment_file(
            db,
            invoice_id,
            vendor_id=None,
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": build_content_disposition(file.original_filename)
        },
    )


@router.post("/quotes/{quote_id}/approve")
def approve_vendor_quote(
    request: Request,
    quote_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    context = _review_context(request, quote_id=quote_id)
    db_session_adapter.release_read_transaction(db)
    try:
        vendor_portal_operations.review_quote(
            db,
            ReviewVendorQuoteCommand(
                context=context,
                quote_id=quote_id,
                reviewer_id=_actor(request),
                approve=True,
                notes=review_notes,
            ),
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
    return RedirectResponse(
        f"/admin/vendors/operations/quotes/{quote_id}?message=Quote+approved",
        status_code=303,
    )


@router.post("/projects/{project_id}/{action}/preview", response_class=HTMLResponse)
def preview_vendor_project_review(
    request: Request,
    project_id: str,
    action: str,
    reason: str | None = Form(default=None),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    proposal = vendor_project_review_proposals.issue_review(
        db,
        project_id=project_id,
        action=action,
        actor_id=_actor(request),
        reason=reason,
    )
    context = _ctx(request, db)
    context["proposal"] = proposal
    return templates.TemplateResponse(
        "admin/vendors/project_review_confirm.html", context
    )


@router.post("/projects/{project_id}/{action}/confirm")
def confirm_vendor_project_review(
    request: Request,
    project_id: str,
    action: str,
    confirmation_token: str = Form(...),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    actor_id = _actor(request)
    context = _staff_confirmation_context(
        request,
        scope=project_id,
        reason="vendor_project_review_confirmation",
    )
    db_session_adapter.release_read_transaction(db)
    result = vendor_project_review_proposals.confirm_review(
        db,
        ConfirmVendorProjectReviewCommand(
            context=context,
            confirmation_token=confirmation_token,
            project_id=project_id,
            action=action,
            actor_id=actor_id,
        ),
    )
    label = "verified" if result.action == "verify" else "returned for rework"
    return RedirectResponse(
        f"/admin/vendors/operations?message=Project+{label}", status_code=303
    )


@router.post("/as-built/{as_built_id}/{action}/preview", response_class=HTMLResponse)
def preview_vendor_as_built_review(
    request: Request,
    as_built_id: str,
    action: str,
    reason: str | None = Form(default=None),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    proposal = vendor_as_built_review_proposals.issue_review(
        db,
        as_built_id=as_built_id,
        action=action,
        actor_id=_actor(request),
        reason=reason,
    )
    context = _ctx(request, db)
    context["proposal"] = proposal
    return templates.TemplateResponse(
        "admin/vendors/as_built_review_confirm.html", context
    )


@router.post("/as-built/{as_built_id}/{action}/confirm")
def confirm_vendor_as_built_review(
    request: Request,
    as_built_id: str,
    action: str,
    confirmation_token: str = Form(...),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    actor_id = _actor(request)
    context = _staff_confirmation_context(
        request,
        scope=as_built_id,
        reason="vendor_as_built_review_confirmation",
    )
    db_session_adapter.release_read_transaction(db)
    result = vendor_as_built_review_proposals.confirm_review(
        db,
        ConfirmVendorAsBuiltReviewCommand(
            context=context,
            confirmation_token=confirmation_token,
            as_built_id=as_built_id,
            action=action,
            actor_id=actor_id,
        ),
    )
    label = "accepted" if result.action == "accept" else "rejected"
    return RedirectResponse(
        f"/admin/vendors/operations/as-built/{as_built_id}"
        f"?message=As-built+evidence+{label}",
        status_code=303,
    )


@router.post("/routes/{revision_id}/{action}/preview", response_class=HTMLResponse)
def preview_vendor_route_review(
    request: Request,
    revision_id: str,
    action: str,
    reason: str | None = Form(default=None),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    proposal = vendor_route_review_proposals.issue_review(
        db,
        revision_id=revision_id,
        action=action,
        actor_id=_actor(request),
        reason=reason,
    )
    context = _ctx(request, db)
    context["proposal"] = proposal
    return templates.TemplateResponse(
        "admin/vendors/route_review_confirm.html",
        context,
    )


@router.post("/routes/{revision_id}/{action}/confirm")
def confirm_vendor_route_review(
    request: Request,
    revision_id: str,
    action: str,
    confirmation_token: str = Form(...),
    _auth: dict = Depends(_project_write),
    db: Session = Depends(get_db),
):
    actor_id = _actor(request)
    context = _staff_confirmation_context(
        request,
        scope=revision_id,
        reason="vendor_route_review_confirmation",
    )
    db_session_adapter.release_read_transaction(db)
    result = vendor_route_review_proposals.confirm_review(
        db,
        ConfirmVendorRouteReviewCommand(
            context=context,
            confirmation_token=confirmation_token,
            revision_id=revision_id,
            action=action,
            actor_id=actor_id,
        ),
    )
    label = "accepted" if result.action == "accept" else "rejected"
    return RedirectResponse(
        f"/admin/vendors/routes/{result.project_id}"
        f"?revision_id={revision_id}&message=Proposed+route+{label}",
        status_code=303,
    )


@router.post("/quotes/{quote_id}/request-revision")
def request_vendor_quote_revision(
    request: Request,
    quote_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    context = _review_context(request, quote_id=quote_id)
    db_session_adapter.release_read_transaction(db)
    try:
        vendor_portal_operations.review_quote(
            db,
            ReviewVendorQuoteCommand(
                context=context,
                quote_id=quote_id,
                reviewer_id=_actor(request),
                approve=False,
                notes=review_notes,
            ),
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
    return RedirectResponse(
        f"/admin/vendors/operations/quotes/{quote_id}?message=Quote+revision+requested",
        status_code=303,
    )


@router.post("/invoices/{invoice_id}/approve")
def approve_vendor_invoice(
    request: Request,
    invoice_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    context = _invoice_review_context(request, invoice_id=invoice_id)
    db_session_adapter.release_read_transaction(db)
    try:
        vendor_purchase_invoices.review(
            db,
            ReviewVendorPurchaseInvoiceCommand(
                context=context,
                invoice_id=invoice_id,
                reviewer_system_user_id=_actor(request),
                approve=True,
                review_notes=review_notes,
            ),
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
    return RedirectResponse(
        f"/admin/vendors/operations/invoices/{invoice_id}?message=Invoice+approved",
        status_code=303,
    )


@router.post("/invoices/{invoice_id}/request-revision")
def request_vendor_invoice_revision(
    request: Request,
    invoice_id: str,
    review_notes: str | None = Form(default=None),
    _auth: dict = Depends(_write),
    db: Session = Depends(get_db),
):
    context = _invoice_review_context(request, invoice_id=invoice_id)
    db_session_adapter.release_read_transaction(db)
    try:
        vendor_purchase_invoices.review(
            db,
            ReviewVendorPurchaseInvoiceCommand(
                context=context,
                invoice_id=invoice_id,
                reviewer_system_user_id=_actor(request),
                approve=False,
                review_notes=review_notes,
            ),
        )
    except DomainError as exc:
        raise _quote_error(exc) from exc
    return RedirectResponse(
        f"/admin/vendors/operations/invoices/{invoice_id}"
        "?message=Invoice+revision+requested",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Vendor material releases and advances
#
# Both participant owners decide. This adapter only authorizes and translates;
# the signed confirmation coordinator owns the atomic transaction.
# ---------------------------------------------------------------------------


@router.post(
    "/material-releases/{release_id}/{action}/preview",
    dependencies=[Depends(require_permission("inventory:write"))],
    response_class=HTMLResponse,
)
def preview_vendor_material_release(
    request: Request,
    release_id: str,
    action: str,
    review_notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    proposal = vendor_supply_review_proposals.issue_review(
        db,
        supply_type=VendorSupplyType.material,
        record_id=coerce_uuid(release_id),
        action=_supply_action(action),
        actor_id=coerce_uuid(_actor(request)),
        reason=review_notes,
    )
    context = _ctx(request, db)
    context["proposal"] = proposal
    return templates.TemplateResponse(
        "admin/vendors/supply_review_confirm.html",
        context,
    )


@router.post(
    "/material-releases/{release_id}/{action}/confirm",
    dependencies=[Depends(require_permission("inventory:write"))],
)
def confirm_vendor_material_release(
    request: Request,
    release_id: str,
    action: str,
    confirmation_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor_id = _actor(request)
    context = _staff_confirmation_context(
        request,
        scope=release_id,
        reason="vendor_material_release_review_confirmation",
    )
    db_session_adapter.release_read_transaction(db)
    result = vendor_supply_review_proposals.confirm_review(
        db,
        ConfirmVendorSupplyReviewCommand(
            context=context,
            confirmation_token=confirmation_token,
            supply_type=VendorSupplyType.material,
            record_id=coerce_uuid(release_id),
            action=_supply_action(action),
            actor_id=coerce_uuid(actor_id),
        ),
    )
    label = (
        "approved" if result.action is VendorSupplyReviewAction.approve else "rejected"
    )
    return RedirectResponse(
        f"/admin/vendors/operations?message=Material+release+{label}",
        status_code=303,
    )


@router.post(
    "/advances/{advance_id}/{action}/preview",
    dependencies=[Depends(require_permission("finance:ap:write"))],
    response_class=HTMLResponse,
)
def preview_vendor_advance(
    request: Request,
    advance_id: str,
    action: str,
    review_notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    proposal = vendor_supply_review_proposals.issue_review(
        db,
        supply_type=VendorSupplyType.advance,
        record_id=coerce_uuid(advance_id),
        action=_supply_action(action),
        actor_id=coerce_uuid(_actor(request)),
        reason=review_notes,
    )
    context = _ctx(request, db)
    context["proposal"] = proposal
    return templates.TemplateResponse(
        "admin/vendors/supply_review_confirm.html",
        context,
    )


@router.post(
    "/advances/{advance_id}/{action}/confirm",
    dependencies=[Depends(require_permission("finance:ap:write"))],
)
def confirm_vendor_advance(
    request: Request,
    advance_id: str,
    action: str,
    confirmation_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor_id = _actor(request)
    context = _staff_confirmation_context(
        request,
        scope=advance_id,
        reason="vendor_advance_review_confirmation",
    )
    db_session_adapter.release_read_transaction(db)
    result = vendor_supply_review_proposals.confirm_review(
        db,
        ConfirmVendorSupplyReviewCommand(
            context=context,
            confirmation_token=confirmation_token,
            supply_type=VendorSupplyType.advance,
            record_id=coerce_uuid(advance_id),
            action=_supply_action(action),
            actor_id=coerce_uuid(actor_id),
        ),
    )
    label = (
        "approved" if result.action is VendorSupplyReviewAction.approve else "rejected"
    )
    return RedirectResponse(
        f"/admin/vendors/operations?message=Advance+{label}",
        status_code=303,
    )
