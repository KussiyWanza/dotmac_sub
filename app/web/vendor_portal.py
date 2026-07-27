"""Browser workspace for native vendor operations in Sub."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import TypeVar
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.vendor_portal import (
    VendorAsBuiltCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
)
from app.services import vendor_submission_proposals
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field.vendor_auth import vendor_context
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_operations import (
    AddVendorQuoteLineCommand,
    CreateVendorQuoteCommand,
    vendor_portal_operations,
)
from app.services.vendor_purchase_invoices import (
    AddVendorPurchaseInvoiceLineCommand,
    CreateVendorPurchaseInvoiceCommand,
    UploadVendorPurchaseInvoiceAttachmentCommand,
    vendor_purchase_invoices,
)
from app.services.vendor_routes_api import build_project_route_geojson
from app.services.vendor_submission_proposals import (
    ConfirmVendorSubmissionCommand,
)
from app.web.auth.dependencies import require_web_auth

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/vendor", tags=["web-vendor-portal"])
ResultT = TypeVar("ResultT")


def _submission_http_error(exc: DomainError) -> HTTPException:
    suffix = exc.code.rsplit(".", 1)[-1]
    status_code = {
        "unsupported_submission_type": 400,
        "invalid_proposal": 400,
        "invalid_payload": 400,
        "proposal_context_mismatch": 403,
        "lifecycle_not_assigned": 403,
        "lifecycle_not_found": 404,
        "expired_proposal": 409,
        "confirmation_in_progress": 409,
        "stale_proposal": 409,
        "missing_result_evidence": 409,
        "lifecycle_invalid_transition": 409,
        "lifecycle_unsupported_action": 400,
        "lifecycle_actor_required": 400,
        "project_not_found": 404,
        "quote_not_found": 404,
        "quote_line_not_found": 404,
        "project_not_assigned": 403,
        "quote_creation_not_allowed": 403,
        "bidding_closed": 409,
        "quote_not_editable": 409,
        "quote_not_submittable": 409,
        "quote_not_reviewable": 409,
        "quote_line_required": 422,
        "as_built_evidence_required": 422,
        "invalid_as_built_route": 422,
        "invoice_not_found": 404,
        "invoice_not_editable": 409,
        "invoice_number_conflict": 409,
        "empty_attachment": 422,
        "invalid_attachment": 422,
        "invoice_number_required": 422,
        "invoice_line_required": 422,
        "submitted_quote_required": 409,
    }.get(suffix, 500)
    return HTTPException(status_code=status_code, detail=exc.message)


def _submission_call(operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except DomainError as exc:
        raise _submission_http_error(exc) from exc


def _command_context(auth: dict, *, vendor_id: str, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(auth["principal_id"]),
        scope=vendor_id,
        reason=reason,
    )


def _context(auth: dict, db: Session) -> dict:
    context = vendor_context(db, auth)
    if not context.get("native_vendor_id"):
        raise HTTPException(
            status_code=409,
            detail="Vendor account is not linked to the native vendor domain",
        )
    return context


def _redirect(project_id: str, message: str | None = None) -> RedirectResponse:
    suffix = f"?message={message}" if message else ""
    return RedirectResponse(f"/vendor/projects/{project_id}{suffix}", status_code=303)


@router.get("", response_class=HTMLResponse)
def vendor_dashboard(
    request: Request,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    return templates.TemplateResponse(
        "vendor/dashboard.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "available_projects": vendor_portal_operations.list_projects(
                db, vendor_id, available=True, limit=50, offset=0
            ),
            "my_projects": vendor_portal_operations.list_projects(
                db, vendor_id, available=False, limit=100, offset=0
            ),
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def vendor_project_detail(
    request: Request,
    project_id: str,
    message: str | None = None,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    project = next(
        (
            item
            for item in vendor_portal_operations.list_projects(
                db, vendor_id, available=False, limit=500, offset=0
            )
            if str(item["id"]) == project_id
        ),
        None,
    )
    if project is None:
        available = vendor_portal_operations.list_projects(
            db, vendor_id, available=True, limit=500, offset=0
        )
        project = next(
            (item for item in available if str(item["id"]) == project_id), None
        )
    if project is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Installation project not found")
    quote = vendor_portal_operations.latest_quote_for_project(
        db, str(project["id"]), vendor_id
    )
    invoice = vendor_purchase_invoices.for_project(
        db, str(project["id"]), vendor_id=vendor_id
    )
    # Proposed (+ any prior as-built) route geometry, so the as-built map shows
    # the planned route as tracing context. Rendered server-side rather than a
    # client fetch — the vendor portal authenticates by ownership, not the
    # admin route API.
    route_geojson = build_project_route_geojson(db, str(project["id"]))
    return templates.TemplateResponse(
        "vendor/project_detail.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "project": project,
            "quote": quote,
            "invoice": invoice,
            "route_geojson": route_geojson,
            "message": message,
        },
    )


@router.post("/projects/{project_id}/quotes")
def vendor_create_quote(
    project_id: str,
    vat_rate_percent: Decimal = Form(default=Decimal("0")),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    command_context = _command_context(
        auth,
        vendor_id=vendor_id,
        reason="vendor_quote_creation",
    )
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_portal_operations.create_quote(
            db,
            CreateVendorQuoteCommand(
                context=command_context,
                payload=VendorQuoteCreate(
                    project_id=coerce_uuid(project_id),
                    vat_rate_percent=vat_rate_percent,
                ),
                vendor_id=vendor_id,
                user_id=str(auth["principal_id"]),
            ),
        )
    )
    return _redirect(project_id, "Quote created")


@router.post("/projects/{project_id}/start")
def vendor_start_project(
    request: Request,
    project_id: str,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    proposal = _submission_call(
        lambda: vendor_submission_proposals.issue_project_lifecycle(
            db,
            project_id=project_id,
            action="start",
            vendor_id=str(context["native_vendor_id"]),
            user_id=str(auth["principal_id"]),
        )
    )
    return templates.TemplateResponse(
        "vendor/submission_confirm.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "proposal": proposal,
        },
    )


@router.post("/projects/{project_id}/complete")
def vendor_complete_project(
    request: Request,
    project_id: str,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    proposal = _submission_call(
        lambda: vendor_submission_proposals.issue_project_lifecycle(
            db,
            project_id=project_id,
            action="complete",
            vendor_id=str(context["native_vendor_id"]),
            user_id=str(auth["principal_id"]),
        )
    )
    return templates.TemplateResponse(
        "vendor/submission_confirm.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "proposal": proposal,
        },
    )


@router.post("/projects/{project_id}/quotes/{quote_id}/lines")
def vendor_add_quote_line(
    project_id: str,
    quote_id: str,
    description: str = Form(...),
    quantity: Decimal = Form(...),
    unit_price: Decimal = Form(...),
    item_type: str | None = Form(default=None),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    command_context = _command_context(
        auth,
        vendor_id=vendor_id,
        reason="vendor_quote_line_creation",
    )
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_portal_operations.add_quote_line(
            db,
            AddVendorQuoteLineCommand(
                context=command_context,
                quote_id=quote_id,
                payload=VendorQuoteLineCreate(
                    item_type=item_type,
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                ),
                vendor_id=vendor_id,
            ),
        )
    )
    return _redirect(project_id, "Quote line added")


@router.post("/projects/{project_id}/quotes/{quote_id}/submit")
def vendor_submit_quote(
    request: Request,
    project_id: str,
    quote_id: str,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    proposal = _submission_call(
        lambda: vendor_submission_proposals.issue_quote_submission(
            db,
            quote_id=quote_id,
            vendor_id=str(context["native_vendor_id"]),
            user_id=str(auth["principal_id"]),
        )
    )
    return templates.TemplateResponse(
        "vendor/submission_confirm.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "proposal": proposal,
        },
    )


@router.post("/projects/{project_id}/as-built")
def vendor_submit_as_built(
    request: Request,
    project_id: str,
    geojson: str = Form(...),
    actual_length_meters: float | None = Form(default=None),
    variation_reason: str | None = Form(default=None),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    proposal = _submission_call(
        lambda: vendor_submission_proposals.issue_as_built_submission(
            db,
            payload=VendorAsBuiltCreate(
                project_id=coerce_uuid(project_id),
                geojson=json.loads(geojson),
                actual_length_meters=actual_length_meters,
                variation_reason=variation_reason,
            ),
            vendor_id=str(context["native_vendor_id"]),
            user_id=str(auth["principal_id"]),
        )
    )
    return templates.TemplateResponse(
        "vendor/submission_confirm.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "proposal": proposal,
        },
    )


@router.post("/projects/{project_id}/purchase-invoices")
def vendor_create_invoice(
    project_id: str,
    invoice_number: str = Form(...),
    tax_rate_percent: Decimal = Form(default=Decimal("0")),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    command_context = _command_context(
        auth, vendor_id=vendor_id, reason="vendor_purchase_invoice_creation"
    )
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_purchase_invoices.create(
            db,
            CreateVendorPurchaseInvoiceCommand(
                context=command_context,
                payload=VendorPurchaseInvoiceCreate(
                    project_id=coerce_uuid(project_id),
                    invoice_number=invoice_number,
                    tax_rate_percent=tax_rate_percent,
                ),
                vendor_id=vendor_id,
                created_by_system_user_id=str(auth["principal_id"]),
            ),
        )
    )
    return _redirect(project_id, "Invoice created")


@router.post("/projects/{project_id}/purchase-invoices/{invoice_id}/lines")
def vendor_add_invoice_line(
    project_id: str,
    invoice_id: str,
    description: str = Form(...),
    quantity: Decimal = Form(...),
    unit_price: Decimal = Form(...),
    item_type: str | None = Form(default=None),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    command_context = _command_context(
        auth, vendor_id=vendor_id, reason="vendor_purchase_invoice_line_addition"
    )
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_purchase_invoices.add_line(
            db,
            AddVendorPurchaseInvoiceLineCommand(
                context=command_context,
                invoice_id=invoice_id,
                payload=VendorPurchaseInvoiceLineCreate(
                    item_type=item_type,
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                ),
                vendor_id=vendor_id,
            ),
        )
    )
    return _redirect(project_id, "Invoice line added")


@router.post("/projects/{project_id}/purchase-invoices/{invoice_id}/attachment")
async def vendor_upload_invoice_attachment(
    project_id: str,
    invoice_id: str,
    attachment: UploadFile = File(...),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    vendor_id = str(context["native_vendor_id"])
    command_context = _command_context(
        auth, vendor_id=vendor_id, reason="vendor_purchase_invoice_attachment_upload"
    )
    content = await attachment.read()
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_purchase_invoices.upload_attachment(
            db,
            UploadVendorPurchaseInvoiceAttachmentCommand(
                context=command_context,
                invoice_id=invoice_id,
                vendor_id=vendor_id,
                file_name=attachment.filename or "invoice.pdf",
                content_type=attachment.content_type,
                content=content,
            ),
        )
    )
    return _redirect(project_id, "Attachment uploaded")


@router.post("/projects/{project_id}/purchase-invoices/{invoice_id}/submit")
def vendor_submit_invoice(
    request: Request,
    project_id: str,
    invoice_id: str,
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    proposal = _submission_call(
        lambda: vendor_submission_proposals.issue_purchase_invoice_submission(
            db,
            invoice_id=invoice_id,
            vendor_id=str(context["native_vendor_id"]),
            user_id=str(auth["principal_id"]),
        )
    )
    return templates.TemplateResponse(
        "vendor/submission_confirm.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "proposal": proposal,
        },
    )


@router.post("/projects/{project_id}/submissions/confirm")
def vendor_confirm_submission(
    project_id: str,
    confirmation_token: str = Form(...),
    auth: dict = Depends(require_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db)
    db_session_adapter.release_read_transaction(db)
    command_context = _command_context(
        auth,
        vendor_id=str(context["native_vendor_id"]),
        reason="vendor_submission_confirmation",
    )
    result = _submission_call(
        lambda: vendor_submission_proposals.confirm_submission(
            db,
            ConfirmVendorSubmissionCommand(
                context=command_context,
                confirmation_token=confirmation_token,
                vendor_id=str(context["native_vendor_id"]),
                user_id=str(auth["principal_id"]),
                project_id=project_id,
            ),
        )
    )
    labels = {
        "quote": "Quote submitted",
        "as_built": "As-built submitted",
        "purchase_invoice": "Invoice submitted",
        "project_start": "Project started",
        "project_complete": "Project marked complete",
    }
    message = labels[result.submission_type]
    if result.replayed:
        message = f"{message} (already processed)"
    return _redirect(project_id, message)
