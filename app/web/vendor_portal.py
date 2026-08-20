"""Browser workspace for native vendor operations in Sub."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import TypeVar, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.vendor_portal import (
    VendorAdvanceCreate,
    VendorAsBuiltCreate,
    VendorMaterialReleaseCreate,
    VendorMaterialReleaseItemCreate,
    VendorQuoteCreate,
    VendorQuoteLineCreate,
    VendorRouteRevisionCreate,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
)
from app.services import (
    network_map,
    vendor_fiber,
    vendor_submission_proposals,
    work_order_views,
)
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.field import vendor_capabilities
from app.services.field.map_assets import VENDOR_ROUTE_AUTHORING_POI_FILTERS
from app.services.field.vendor_auth import vendor_context
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_operations import (
    VENDOR_ROUTE_AUTHORING_LAYER_FILTERS,
    VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS,
    VENDOR_ROUTE_AUTHORING_STATUS_FILTERS,
    AddVendorQuoteLineCommand,
    CreateVendorQuoteCommand,
    CreateVendorRouteRevisionCommand,
    RequestVendorAdvanceCommand,
    RequestVendorMaterialReleaseCommand,
    SubmitVendorRouteRevisionCommand,
    vendor_portal_operations,
)
from app.services.vendor_purchase_invoices import (
    AddVendorPurchaseInvoiceLineCommand,
    CreateVendorPurchaseInvoiceCommand,
    UploadVendorPurchaseInvoiceAttachmentCommand,
    vendor_purchase_invoices,
)
from app.services.vendor_routes_api import build_vendor_project_route_geojson
from app.services.vendor_submission_proposals import (
    ConfirmVendorSubmissionCommand,
)
from app.services.vendor_supply_views import project_workspace
from app.web.vendor_auth_flow import require_vendor_web_auth

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
        "route_revision_not_found": 404,
        "route_revision_not_draft": 409,
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
        "items_required": 422,
        "invalid_quantity": 422,
        "invalid_amount": 422,
        "project_not_releasable": 409,
        "project_not_advanceable": 409,
        "approved_quote_required": 409,
        "advance_ceiling_exceeded": 409,
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


def _context(auth: dict, db: Session, capability: str | None = None) -> dict:
    context = vendor_context(db, auth)
    if not context.get("native_vendor_id"):
        raise HTTPException(
            status_code=409,
            detail="Vendor account is not linked to the native vendor domain",
        )
    # The API gates capability with a FastAPI dependency; these handlers resolve
    # the vendor context themselves, so the same check lands here instead. Both
    # read from the one declaring owner.
    if capability is not None and not vendor_capabilities.has_capability(
        context, capability
    ):
        raise HTTPException(
            status_code=403,
            detail=vendor_capabilities.refusal_message(capability),
        )
    return context


def _redirect(project_id: str, message: str | None = None) -> RedirectResponse:
    suffix = f"?message={message}" if message else ""
    return RedirectResponse(f"/vendor/projects/{project_id}{suffix}", status_code=303)


def _route_revision_payload(
    geojson: str,
    length_meters: float | None,
) -> VendorRouteRevisionCreate:
    try:
        geometry = json.loads(geojson)
        if not isinstance(geometry, dict):
            raise TypeError("Route geometry must be an object")
        return VendorRouteRevisionCreate(
            geojson=cast(dict[str, object], geometry),
            length_meters=length_meters,
        )
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Trace a valid route with at least two map points.",
        ) from exc


def _as_built_payload(
    project_id: str,
    geojson: str,
    actual_length_meters: float | None,
    variation_reason: str | None,
) -> VendorAsBuiltCreate:
    try:
        geometry = json.loads(geojson)
        if not isinstance(geometry, dict):
            raise TypeError("As-built route geometry must be an object")
        return VendorAsBuiltCreate(
            project_id=coerce_uuid(project_id),
            geojson=cast(dict[str, object], geometry),
            actual_length_meters=actual_length_meters,
            variation_reason=variation_reason,
        )
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Trace a valid as-built route with at least two map points.",
        ) from exc


@router.get("", response_class=HTMLResponse)
def vendor_dashboard(
    request: Request,
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.PROJECT_READ)
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.PROJECT_READ)
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
    route_geojson = build_vendor_project_route_geojson(
        db,
        str(project["id"]),
        vendor_id,
    )
    route_planning_map = network_map.build_vendor_route_planning_map_projection(db=db)
    supply = project_workspace(
        db,
        project_id=str(project["id"]),
        vendor_id=vendor_id,
        capabilities=vendor_capabilities.capabilities_for(context),
    )
    vendor_work_orders = work_order_views.list_project_work_order_summaries(
        db,
        coerce_uuid(project["project_id"]),
    )
    can_propose_closure = (
        bool(vendor_work_orders)
        and str(project.get("assigned_vendor_id") or "") == vendor_id
        and vendor_capabilities.has_capability(
            context, vendor_capabilities.AS_BUILT_WRITE
        )
    )
    return templates.TemplateResponse(
        "vendor/project_detail.html",
        {
            "request": request,
            "vendor": context["native_vendor"],
            "project": project,
            "quote": quote,
            "invoice": invoice,
            "route_geojson": route_geojson,
            "route_planning_map_geojson": route_planning_map.to_transport(),
            "supply": supply,
            "vendor_work_orders": vendor_work_orders,
            "can_propose_closure": can_propose_closure,
            "vendor_route_authoring_layer_filters": VENDOR_ROUTE_AUTHORING_LAYER_FILTERS,
            "vendor_route_authoring_poi_filters": VENDOR_ROUTE_AUTHORING_POI_FILTERS,
            "vendor_route_authoring_radius_options": VENDOR_ROUTE_AUTHORING_RADIUS_OPTIONS,
            "vendor_route_authoring_status_filters": VENDOR_ROUTE_AUTHORING_STATUS_FILTERS,
            "message": message,
        },
    )


@router.post("/projects/{project_id}/closure-proposals")
def vendor_create_closure_proposal(
    project_id: str,
    work_order_id: str = Form(...),
    name: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    notes: str | None = Form(default=None),
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.AS_BUILT_WRITE)
    try:
        receipt = vendor_fiber.register_closure(
            db,
            vendor_id=str(context["native_vendor_id"]),
            vendor_user_id=str(auth["principal_id"]),
            work_order_id=work_order_id,
            installation_project_id=project_id,
            name=name,
            latitude=latitude,
            longitude=longitude,
            notes=notes,
        )
    except DomainError as exc:
        raise HTTPException(
            status_code={"not_found": 404, "conflict": 409, "invalid": 422}.get(
                getattr(exc, "kind", "invalid"), 422
            ),
            detail=exc.message,
        ) from exc
    return _redirect(
        project_id,
        f"Closure {receipt.name} submitted for staff review",
    )


@router.post("/projects/{project_id}/quotes")
def vendor_create_quote(
    project_id: str,
    vat_rate_percent: Decimal = Form(default=Decimal("0")),
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.QUOTE_WRITE)
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
                vendor_id=coerce_uuid(vendor_id),
                user_id=coerce_uuid(auth["principal_id"]),
            ),
        )
    )
    return _redirect(project_id, "Quote created")


@router.post("/projects/{project_id}/material-releases")
def vendor_request_material_release(
    project_id: str,
    descriptions: list[str] = Form(..., alias="description"),
    quantities: list[str] = Form(..., alias="quantity"),
    units: list[str] = Form(..., alias="unit"),
    item_codes: list[str] = Form(..., alias="item_code"),
    line_notes: list[str] = Form(..., alias="line_notes"),
    notes: str | None = Form(default=None),
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.MATERIAL_REQUEST)
    vendor_id = str(context["native_vendor_id"])
    try:
        items: list[VendorMaterialReleaseItemCreate] = []
        for index, description in enumerate(descriptions):
            if not description.strip():
                continue
            try:
                quantity = int(quantities[index])
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError("invalid material quantity") from exc
            items.append(
                VendorMaterialReleaseItemCreate(
                    description=description,
                    quantity=quantity,
                    unit=units[index] if index < len(units) else None,
                    item_code=(item_codes[index] if index < len(item_codes) else None),
                    notes=line_notes[index] if index < len(line_notes) else None,
                )
            )
        payload = VendorMaterialReleaseCreate(
            project_id=coerce_uuid(project_id),
            items=items,
            notes=notes,
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Enter a material description and a quantity greater than zero.",
        ) from exc
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_portal_operations.request_material_release(
            db,
            RequestVendorMaterialReleaseCommand(
                context=_command_context(
                    auth,
                    vendor_id=vendor_id,
                    reason="vendor_material_release_request",
                ),
                payload=payload,
                vendor_id=coerce_uuid(vendor_id),
                user_id=coerce_uuid(auth["principal_id"]),
            ),
        )
    )
    return _redirect(project_id, "Material request submitted")


@router.post("/projects/{project_id}/advances")
def vendor_request_advance(
    project_id: str,
    amount: Decimal = Form(...),
    reason: str | None = Form(default=None),
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.ADVANCE_REQUEST)
    vendor_id = str(context["native_vendor_id"])
    try:
        payload = VendorAdvanceCreate(
            project_id=coerce_uuid(project_id),
            amount=amount,
            reason=reason,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="Enter an advance amount greater than zero.",
        ) from exc
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_portal_operations.request_advance(
            db,
            RequestVendorAdvanceCommand(
                context=_command_context(
                    auth,
                    vendor_id=vendor_id,
                    reason="vendor_advance_request",
                ),
                payload=payload,
                vendor_id=coerce_uuid(vendor_id),
                user_id=coerce_uuid(auth["principal_id"]),
            ),
        )
    )
    return _redirect(project_id, "Advance request submitted")


@router.post("/projects/{project_id}/start")
def vendor_start_project(
    request: Request,
    project_id: str,
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.PROJECT_EXECUTE)
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.PROJECT_EXECUTE)
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.QUOTE_WRITE)
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.QUOTE_WRITE)
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


@router.post("/projects/{project_id}/quotes/{quote_id}/route-revisions")
def vendor_create_route_revision(
    project_id: str,
    quote_id: str,
    geojson: str = Form(...),
    length_meters: float | None = Form(default=None),
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.QUOTE_WRITE)
    vendor_id = str(context["native_vendor_id"])
    payload = _route_revision_payload(geojson, length_meters)
    command_context = _command_context(
        auth,
        vendor_id=vendor_id,
        reason="vendor_route_revision_creation",
    )
    db_session_adapter.release_read_transaction(db)
    result = _submission_call(
        lambda: vendor_portal_operations.create_route_revision(
            db,
            CreateVendorRouteRevisionCommand(
                context=command_context,
                quote_id=quote_id,
                payload=payload,
                vendor_id=vendor_id,
            ),
        )
    )
    return _redirect(
        project_id,
        f"Route revision {result['revision_number']} saved as draft",
    )


@router.post("/projects/{project_id}/route-revisions/{revision_id}/submit")
def vendor_submit_route_revision(
    project_id: str,
    revision_id: str,
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.QUOTE_WRITE)
    vendor_id = str(context["native_vendor_id"])
    command_context = _command_context(
        auth,
        vendor_id=vendor_id,
        reason="vendor_route_revision_submission",
    )
    db_session_adapter.release_read_transaction(db)
    _submission_call(
        lambda: vendor_portal_operations.submit_route_revision(
            db,
            SubmitVendorRouteRevisionCommand(
                context=command_context,
                revision_id=revision_id,
                vendor_id=vendor_id,
                user_id=str(auth["principal_id"]),
            ),
        )
    )
    return _redirect(project_id, "Route revision submitted for review")


@router.post("/projects/{project_id}/as-built")
def vendor_submit_as_built(
    request: Request,
    project_id: str,
    geojson: str = Form(...),
    actual_length_meters: float | None = Form(default=None),
    variation_reason: str | None = Form(default=None),
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.AS_BUILT_WRITE)
    payload = _as_built_payload(
        project_id,
        geojson,
        actual_length_meters,
        variation_reason,
    )
    proposal = _submission_call(
        lambda: vendor_submission_proposals.issue_as_built_submission(
            db,
            payload=payload,
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.INVOICE_WRITE)
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.INVOICE_WRITE)
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.INVOICE_WRITE)
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
    auth: dict = Depends(require_vendor_web_auth),
    db: Session = Depends(get_db),
):
    context = _context(auth, db, vendor_capabilities.INVOICE_WRITE)
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
    auth: dict = Depends(require_vendor_web_auth),
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
