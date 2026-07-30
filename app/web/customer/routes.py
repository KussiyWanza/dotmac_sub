"""Customer portal web routes."""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote_plus, urlsplit
from uuid import UUID

import anyio
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.db import finish_read_transaction, get_db
from app.services import auth_flow as auth_flow_service
from app.services import autopay as autopay_service
from app.services import billing_payment_receipts as payment_receipts_service
from app.services import chat_session as chat_session_service
from app.services import (
    crm_portal,
    customer_portal,
    portal_ticket_deflection,
    support_ticket_settings,
    team_inbox_widget,
)
from app.services import customer_portal_bandwidth as customer_portal_bandwidth_service
from app.services import customer_portal_contacts as customer_portal_contacts_service
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services import customer_portal_notifications as customer_notifications_service
from app.services import payment_proofs as payment_proofs_service
from app.services import service_address as service_address_service
from app.services import web_customer_auth as web_customer_auth_service
from app.services import web_network_speedtests as web_network_speedtests_service
from app.services.audit_helpers import log_audit_event
from app.services.bandwidth import add_directions_to_series, bandwidth_samples
from app.services.customer_context import (
    optional_customer_account_id,
    optional_customer_subscriber_id,
    require_customer_account_id,
)
from app.services.customer_portal_context import (
    emit_customer_event,
    get_dashboard_template_context,
    is_subscriber_restricted,
    resolve_allowed_subscriber_ids,
    resolve_customer_subscription,
)
from app.services.nin_matching import mask_nin
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])

logger = logging.getLogger(__name__)
_emit_customer_event = emit_customer_event
PAYMENT_VERIFICATION_ERROR_MESSAGE = (
    "We could not confirm this payment yet. If you were charged, please do not "
    "retry immediately; check your billing history or contact support with this "
    "payment reference."
)
PAYMENT_CHARGE_ERROR_MESSAGE = (
    "We could not charge that saved card. Please use another payment method or "
    "try again later."
)
PAYMENT_START_ERROR_MESSAGE = (
    "Unable to start the payment. If your card was charged, the payment will "
    "be reconciled automatically."
)
CARD_SAVE_SUCCESS_MESSAGE = "Your card was saved for future payments."
CARD_SAVE_ERROR_MESSAGE = (
    "Payment was recorded, but we could not save this card. You can add a card "
    "from Payment Methods."
)
READ_ONLY_MUTATION_MESSAGE = "View-only sessions cannot make changes."
SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE = (
    "Your verified prepaid funding position is still under review. "
    "Contact support before changing this service."
)


def _is_read_only_customer(customer: dict | None) -> bool:
    return bool(customer and customer.get("read_only"))


def _safe_portal_return_path(value: str | None) -> str:
    """Keep notification-action redirects inside the customer portal."""
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/portal/notifications"
    if parsed.path != "/portal" and not parsed.path.startswith("/portal/"):
        return "/portal/notifications"
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _read_only_response(
    request: Request,
    customer: dict | None,
    *,
    active_page: str,
) -> Response:
    return templates.TemplateResponse(
        "customer/errors/400.html",
        {
            "request": request,
            "customer": customer,
            "message": READ_ONLY_MUTATION_MESSAGE,
            "active_page": active_page,
        },
        status_code=403,
    )


def _payment_verification_error_response(
    request: Request,
    exc: Exception,
    *,
    status_code: int = 400,
) -> Response:
    logger.info(
        "Customer payment verification failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return templates.TemplateResponse(
        "customer/errors/400.html",
        {"request": request, "message": PAYMENT_VERIFICATION_ERROR_MESSAGE},
        status_code=status_code,
    )


def _render_payment_return_status(
    request: Request,
    *,
    reference: str,
    provider: str | None,
    flow: str,
    exc: Exception,
) -> Response:
    is_decline = isinstance(exc, (ValueError, HTTPException)) and not (
        isinstance(exc, HTTPException) and exc.status_code >= 500
    )
    if is_decline:
        status_kind = "declined"
        title = "Payment not confirmed"
        message = (
            "The payment provider did not confirm a successful payment. "
            "No duplicate payment was recorded."
        )
    else:
        status_kind = "pending"
        title = "Payment verification pending"
        message = (
            "We could not confirm the payment provider response right now. "
            "If you were debited, the payment will be reconciled automatically."
        )
        logger.warning(
            "Customer payment verification returned pending state",
            extra={"reference": reference, "provider": provider, "flow": flow},
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    return templates.TemplateResponse(
        "customer/billing/payment_status.html",
        {
            "request": request,
            "status_kind": status_kind,
            "title": title,
            "message": message,
            "reference": reference,
            "provider": provider,
            "flow": flow,
            "active_page": "billing",
        },
        status_code=200,
    )


def _capture_card_save_status(
    db: Session,
    *,
    account_id: str,
    reference: str,
    provider: str | None,
    requested: bool,
) -> dict[str, str] | None:
    if requested is not True:
        return None
    try:
        customer_cards.capture_card_after_payment(db, account_id, reference, provider)
    except Exception:
        logger.warning("Customer card capture failed", exc_info=True)
        return {"status": "failed", "message": CARD_SAVE_ERROR_MESSAGE}
    return {"status": "saved", "message": CARD_SAVE_SUCCESS_MESSAGE}


def _format_bps(value: float | int | None) -> str:
    amount = float(value or 0)
    if amount <= 0:
        return "0 bps"
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    unit_index = 0
    while amount >= 1000 and unit_index < len(units) - 1:
        amount /= 1000
        unit_index += 1
    precision = 0 if unit_index == 0 else (2 if amount < 10 else 1)
    return f"{amount:.{precision}f} {units[unit_index]}"


def _profile_value(value):
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _profile_audit_snapshot(subscriber) -> dict[str, object]:
    metadata = dict(getattr(subscriber, "metadata_", None) or {})
    nin_value = getattr(subscriber, "nin", None)
    masked_nin = (
        mask_nin(nin_value) if isinstance(nin_value, str) and nin_value else None
    )
    parts = service_address_service.address_parts(subscriber)
    return {
        "first_name": subscriber.first_name,
        "last_name": subscriber.last_name,
        "display_name": subscriber.display_name,
        "email": subscriber.email,
        "phone": subscriber.phone,
        "nin": masked_nin,
        "date_of_birth": _profile_value(subscriber.date_of_birth),
        "gender": _profile_value(subscriber.gender),
        "preferred_contact_method": _profile_value(subscriber.preferred_contact_method),
        "address_line1": parts.address_line1,
        "address_line2": parts.address_line2,
        "city": parts.city,
        "region": parts.region,
        "postal_code": parts.postal_code,
        "country_code": parts.country_code,
        "billing_notifications": bool(metadata.get("billing_notifications", True)),
        "sms_updates": bool(metadata.get("sms_updates", True)),
        "push_notifications": bool(metadata.get("push_notifications", True)),
        "service_notifications": bool(metadata.get("service_notifications", True)),
        "account_notifications": bool(metadata.get("account_notifications", True)),
        "usage_notifications": bool(metadata.get("usage_notifications", True)),
        "general_notifications": bool(metadata.get("general_notifications", True)),
        "locale": subscriber.locale,
        "email_verified": subscriber.email_verified,
    }


def _profile_completion(subscriber) -> dict[str, object]:
    gender_value = _profile_value(getattr(subscriber, "gender", None))
    if not isinstance(gender_value, str):
        gender_value = ""
    required = {
        "date_of_birth": bool(getattr(subscriber, "date_of_birth", None)),
        "gender": gender_value not in {"", "unknown"},
        "nin": bool(getattr(subscriber, "nin", None)),
    }
    missing = [key for key, complete in required.items() if not complete]
    return {
        "required": required,
        "missing": missing,
        "missing_labels": ", ".join(item.replace("_", " ") for item in missing),
        "complete": not missing,
        "completed_count": sum(1 for value in required.values() if value),
        "total_count": len(required),
    }


def _profile_audit_changes(before: dict, after: dict) -> dict[str, dict[str, object]]:
    changes: dict[str, dict[str, object]] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changes[key] = {"from": before.get(key), "to": after.get(key)}
    return changes


def _load_initial_bandwidth_stats(
    db: Session, subscription_id: str | UUID | None
) -> dict[str, object] | None:
    if not subscription_id:
        return None
    try:
        stats = anyio.from_thread.run(
            bandwidth_samples.get_bandwidth_stats,
            db,
            subscription_id,
            "24h",
        )
    except Exception:
        logger.debug(
            "portal_initial_bandwidth_stats_failed",
            extra={
                "event": "portal_initial_bandwidth_stats_failed",
                "subscription_id": str(subscription_id),
            },
            exc_info=True,
        )
        return None

    return {
        **stats,
        "current_rx_formatted": _format_bps(stats.get("current_rx_bps")),
        "current_tx_formatted": _format_bps(stats.get("current_tx_bps")),
        "peak_rx_formatted": _format_bps(stats.get("peak_rx_bps")),
        "peak_tx_formatted": _format_bps(stats.get("peak_tx_bps")),
        # Explicit subscriber-perspective fields the templates bind to.
        "current_download_formatted": _format_bps(stats.get("download_bps")),
        "current_upload_formatted": _format_bps(stats.get("upload_bps")),
        "peak_download_formatted": _format_bps(stats.get("peak_download_bps")),
        "peak_upload_formatted": _format_bps(stats.get("peak_upload_bps")),
    }


def _render_dashboard(
    request: Request, db: Session, customer: dict, next_url: str
) -> Response:
    """Render full or restricted dashboard based on subscriber status."""
    template_name, context = get_dashboard_template_context(db, customer)
    return templates.TemplateResponse(
        template_name,
        {"request": request, **context},
    )


@router.get("", response_class=HTMLResponse)
def portal_home(request: Request, db: Session = Depends(get_db)) -> Response:
    """Customer portal dashboard."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login?next=/portal", status_code=303)
    return _render_dashboard(request, db, customer, "/portal")


@router.get("/dashboard", response_class=HTMLResponse)
def customer_dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    """Customer dashboard with account overview."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/dashboard", status_code=303
        )
    return _render_dashboard(request, db, customer, "/portal/dashboard")


@router.post("/chat/session")
def customer_portal_chat_session(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Open a CRM live-chat session for a browser-authenticated portal user."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if _is_read_only_customer(customer):
        raise HTTPException(status_code=403, detail=READ_ONLY_MUTATION_MESSAGE)

    subscriber_id = optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        raise HTTPException(status_code=409, detail="Customer account is incomplete")
    try:
        return chat_session_service.broker_customer_session(db, str(subscriber_id))
    except team_inbox_widget.TeamInboxWidgetError as exc:
        status_code = 404 if exc.code.endswith("_not_found") else 503
        raise HTTPException(status_code=status_code, detail=exc.message) from exc


@router.get("/support", response_class=HTMLResponse)
def customer_support(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Customer support tickets (CRM-backed)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/support", status_code=303
        )

    subscriber_ids = resolve_allowed_subscriber_ids(customer, db)
    context = crm_portal.tickets_list_context(request, db, customer, subscriber_ids)
    return templates.TemplateResponse("customer/support/index.html", context)


@router.get("/support/new", response_class=HTMLResponse)
def customer_support_new(
    request: Request,
    title: str | None = None,
    description: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/support/new", status_code=303
        )
    context = crm_portal.ticket_create_context(request, db, customer)
    # Tell the customer what we already know before asking them to describe it.
    # Informational only — the form still accepts the report.
    context.update(
        portal_ticket_deflection.assess_ticket_deflection(db, customer).as_context()
    )
    if title or description:
        context["form_values"] = {
            "title": title or "",
            "description": description or "",
            "priority": "normal",
        }
    return templates.TemplateResponse("customer/support/new.html", context)


@router.post("/support/new", response_class=HTMLResponse)
def customer_support_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("normal"),
    region: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="support")

    canonical_region = support_ticket_settings.canonical_region_option(db, region)
    if canonical_region is None:
        context = crm_portal.ticket_create_context(request, db, customer)
        context["crm_error"] = True
        context["crm_error_message"] = "Select a valid Region."
        context["form_values"] = {
            "title": title,
            "description": description,
            "priority": priority,
            "region": region,
        }
        return templates.TemplateResponse(
            "customer/support/new.html",
            context,
            status_code=400,
        )

    subscriber_id, _subscription_id = customer_portal.resolve_customer_account(
        customer, db
    )
    subscriber_lookup = str(
        subscriber_id or optional_customer_subscriber_id(db, customer) or ""
    )
    result = crm_portal.handle_ticket_create(
        db,
        customer,
        subscriber_lookup,
        title,
        description,
        priority,
        canonical_region,
        support_ticket_settings.resolve_portal_ticket_team_routing(db),
        attachments=attachments,
    )
    if result["success"]:
        ticket = result["ticket"]
        ticket_id = ticket.get("id", "")
        emit_customer_event(
            db,
            "customer_ticket_created",
            {
                "ticket_id": str(ticket_id),
                "subscriber_id": subscriber_lookup,
                # The acknowledgement template renders {ticket_number}; fall
                # back to the id so the message never ships an empty ref.
                "ticket_number": str(ticket.get("number") or ticket_id),
            },
        )
        return RedirectResponse(url=f"/portal/support/{ticket_id}", status_code=303)
    context = crm_portal.ticket_create_context(request, db, customer)
    context["crm_error"] = True
    context["crm_error_message"] = result.get("error") or "Unable to create ticket."
    context["form_values"] = {
        "title": title,
        "description": description,
        "priority": priority,
        "region": canonical_region,
    }
    return templates.TemplateResponse(
        "customer/support/new.html",
        context,
        status_code=400,
    )


@router.get("/support/{ticket_id}", response_class=HTMLResponse)
def customer_support_detail(
    request: Request,
    ticket_id: str,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url=f"/portal/auth/login?next=/portal/support/{quote_plus(ticket_id)}",
            status_code=303,
        )

    subscriber_ids = resolve_allowed_subscriber_ids(customer, db)
    context = crm_portal.ticket_detail_context(
        request, db, customer, subscriber_ids, ticket_id
    )
    return templates.TemplateResponse("customer/support/detail.html", context)


@router.post("/support/{ticket_id}/comment", response_class=HTMLResponse)
def customer_support_add_comment(
    request: Request,
    ticket_id: str,
    body: str = Form(...),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="support")

    subscriber_ids = resolve_allowed_subscriber_ids(customer, db)
    result = crm_portal.handle_ticket_comment(
        db, customer, subscriber_ids, ticket_id, body, attachments=attachments
    )
    if not result.get("success"):
        context = crm_portal.ticket_detail_context(
            request, db, customer, subscriber_ids, ticket_id
        )
        context["crm_error"] = True
        context["crm_error_message"] = result.get("error") or "Unable to add comment."
        return templates.TemplateResponse(
            "customer/support/detail.html",
            context,
            status_code=400,
        )
    return RedirectResponse(url=f"/portal/support/{ticket_id}", status_code=303)


@router.post("/support/{ticket_id}/rate")
def customer_support_rate(
    request: Request,
    ticket_id: str,
    rating: int = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Submit a support-satisfaction (CSAT) rating on a resolved/closed ticket,
    then redirect back to the ticket with a toast flag."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="support")
    subscriber_ids = resolve_allowed_subscriber_ids(customer, db)
    result = crm_portal.handle_ticket_rating(
        db, subscriber_ids, ticket_id, rating, comment=comment
    )
    status = "ok" if result.get("success") else "error"
    return RedirectResponse(
        url=f"/portal/support/{ticket_id}?rated={status}", status_code=303
    )


@router.post("/support/{ticket_id}/confirm-resolution")
def customer_support_confirm_resolution(
    request: Request,
    ticket_id: str,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="support")
    result = crm_portal.handle_ticket_resolution_response(
        db,
        resolve_allowed_subscriber_ids(customer, db),
        ticket_id,
        confirm=True,
    )
    status = "confirmed" if result.get("success") else "resolution_error"
    return RedirectResponse(
        url=f"/portal/support/{ticket_id}?resolution={status}", status_code=303
    )


@router.post("/support/{ticket_id}/dispute-resolution")
def customer_support_dispute_resolution(
    request: Request,
    ticket_id: str,
    reason: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="support")
    result = crm_portal.handle_ticket_resolution_response(
        db,
        resolve_allowed_subscriber_ids(customer, db),
        ticket_id,
        confirm=False,
        reason=reason,
    )
    status = "disputed" if result.get("success") else "resolution_error"
    return RedirectResponse(
        url=f"/portal/support/{ticket_id}?resolution={status}", status_code=303
    )


# ── Work Orders (CRM-backed) ─────────────────────────────────────────────


@router.get("/billing", response_class=HTMLResponse)
def customer_billing(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer billing history."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/billing", status_code=303
        )

    billing_data = customer_portal.get_billing_page(
        db, customer, status=status, page=page, per_page=per_page
    )
    # Payment is the one flow the mostly-dormant customer base reliably uses,
    # so re-surface the location-confirmation prompt here regardless of any
    # snooze. Read-only, gated by loyalty.capture_prompt (default off).
    from app.web.customer.location import location_prompt_context

    location_prompt = location_prompt_context(db, customer, ignore_snooze=True)
    finish_read_transaction(db)

    from datetime import UTC, datetime

    return templates.TemplateResponse(
        "customer/billing/index.html",
        {
            "request": request,
            "customer": customer,
            **billing_data,
            "active_page": "billing",
            "location_prompt": location_prompt,
            "now": datetime.now(UTC),
        },
    )


@router.get("/billing/invoices", response_class=HTMLResponse)
def customer_billing_invoices_redirect(request: Request) -> RedirectResponse:
    target = "/portal/billing"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/billing/invoices/{invoice_id}", response_class=HTMLResponse)
def customer_invoice_detail(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """View invoice details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_invoice_detail(db, customer, str(invoice_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/invoice.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "billing",
        },
    )


@router.get("/billing/invoices/{invoice_id}/pdf", response_class=HTMLResponse)
def customer_invoice_pdf(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Download invoice PDF — triggers generation if needed."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    # Verify access
    detail = customer_portal.get_invoice_detail(db, customer, str(invoice_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    from app.models.billing import Invoice
    from app.services import billing_invoice_pdf as billing_invoice_pdf_service
    from app.services.file_storage import build_content_disposition

    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    # Check for existing completed export
    latest_export = billing_invoice_pdf_service.get_latest_export(db, str(invoice_id))
    latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(
        db, latest_export
    )

    if billing_invoice_pdf_service.is_export_cache_valid(db, invoice, latest_export):
        try:
            from starlette.responses import StreamingResponse

            if latest_export is None:
                raise ValueError("Missing invoice export")
            stream = billing_invoice_pdf_service.stream_export(db, latest_export)
            headers = {
                "Content-Disposition": build_content_disposition(
                    billing_invoice_pdf_service.download_filename(invoice)
                ),
            }
            if stream.content_length is not None:
                headers["Content-Length"] = str(stream.content_length)
            return StreamingResponse(
                stream.chunks,
                media_type=stream.content_type or "application/pdf",
                headers=headers,
            )
        except Exception:
            logger.debug(
                "Failed streaming cached invoice PDF for invoice %s",
                invoice_id,
                exc_info=True,
            )

    generated_export = billing_invoice_pdf_service.generate_export_now(
        db,
        invoice_id=str(invoice_id),
        requested_by_id=optional_customer_subscriber_id(db, customer),
    )
    if billing_invoice_pdf_service.is_export_cache_valid(db, invoice, generated_export):
        try:
            from starlette.responses import StreamingResponse

            stream = billing_invoice_pdf_service.stream_export(db, generated_export)
            headers = {
                "Content-Disposition": build_content_disposition(
                    billing_invoice_pdf_service.download_filename(invoice)
                ),
            }
            if stream.content_length is not None:
                headers["Content-Length"] = str(stream.content_length)
            return StreamingResponse(
                stream.chunks,
                media_type=stream.content_type or "application/pdf",
                headers=headers,
            )
        except Exception:
            logger.debug(
                "Failed streaming generated invoice PDF for invoice %s",
                invoice_id,
                exc_info=True,
            )

    # Queue generation if the inline path did not produce a downloadable PDF.
    subscriber_id = optional_customer_subscriber_id(db, customer)
    billing_invoice_pdf_service.queue_export(
        db,
        str(invoice_id),
        requested_by_id=subscriber_id,
    )
    # Redirect back with notice
    return RedirectResponse(
        url=f"/portal/billing/invoices/{invoice_id}?pdf_notice=generating",
        status_code=303,
    )


@router.get("/billing/payments/{payment_id}/receipt", response_class=HTMLResponse)
def customer_payment_receipt(
    request: Request,
    payment_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """View a customer payment receipt."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        detail = payment_receipts_service.get_customer_payment_receipt_context(
            db, customer, str(payment_id)
        )
    except HTTPException:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Receipt not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/receipt.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "billing",
        },
    )


@router.get("/billing/payments/{payment_id}/receipt/pdf")
def customer_payment_receipt_pdf(
    request: Request,
    payment_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Download a customer payment receipt PDF."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        detail = payment_receipts_service.get_customer_payment_receipt_context(
            db, customer, str(payment_id)
        )
        pdf_bytes = payment_receipts_service.build_receipt_pdf(detail)
    except HTTPException:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Receipt not found"},
            status_code=404,
        )
    except Exception:
        logger.warning("Failed to render payment receipt PDF", exc_info=True)
        return templates.TemplateResponse(
            "customer/errors/400.html",
            {"request": request, "message": "Unable to generate receipt PDF"},
            status_code=500,
        )

    from app.services.file_storage import build_content_disposition

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(
                payment_receipts_service.download_filename(detail["payment"])
            )
        },
    )


@router.get("/usage", response_class=HTMLResponse)
def customer_usage(
    request: Request,
    period: str = Query("current", pattern="^(current|last)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
) -> Response:
    """Customer usage dashboard."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/usage", status_code=303
        )
    subscription = resolve_customer_subscription(db, customer)
    usage_data = customer_portal.get_usage_page(
        db, customer, period=period, page=page, per_page=per_page
    )
    usage_history = customer_portal.get_usage_history(db, customer, months=12)

    return templates.TemplateResponse(
        "customer/usage/index.html",
        {
            "request": request,
            "customer": customer,
            **usage_data,
            "usage_history": usage_history,
            "usage_chart_records": usage_data.get("chart_records", []),
            "active_page": "usage",
            "bandwidth_chart_initial_stats": _load_initial_bandwidth_stats(
                db,
                subscription.id if subscription else None,
            ),
            # Stream live throughput from /portal/bandwidth/my/live (SSE) when the
            # customer has a subscription to read against.
            "bandwidth_chart_live_stream": bool(usage_data.get("has_subscription")),
            "usage_enable_records_chart": True,
            "usage_records_default_view": "chart",
            "usage_records_chart_id": "portal-usage-records-chart",
            "usage_records_chart_label": "Daily Usage (GB)",
        },
    )


@router.get("/bandwidth/my/series")
def customer_bandwidth_series(
    request: Request,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1m|5m|1h)$"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    subscription = resolve_customer_subscription(db, customer)
    if not subscription:
        return JSONResponse({"data": [], "total": 0, "source": "postgres"})

    # The cached Zabbix usage engine was retired with the native monitoring
    # cutover; serve straight from the Postgres bandwidth samples (the same
    # fallback the cache-miss path always took).
    result = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_series,
        db,
        subscription.id,
        start_at,
        end_at,
        interval,
    )
    return JSONResponse(content=jsonable_encoder(add_directions_to_series(result)))


@router.get("/bandwidth/my/stats")
def customer_bandwidth_stats(
    request: Request,
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    subscription = resolve_customer_subscription(db, customer)
    if not subscription:
        return JSONResponse(
            {
                "current_rx_bps": 0,
                "current_tx_bps": 0,
                "peak_rx_bps": 0,
                "peak_tx_bps": 0,
                "total_rx_bytes": 0,
                "total_tx_bytes": 0,
                "sample_count": 0,
            }
        )

    # The cached Zabbix usage engine was retired with the native monitoring
    # cutover; serve straight from the Postgres bandwidth samples (the same
    # fallback the cache-miss path always took).
    stats = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_stats,
        db,
        subscription.id,
        period,
    )
    return JSONResponse(stats)


@router.get("/bandwidth/my/live")
def customer_bandwidth_live(
    request: Request,
    db: Session = Depends(get_db),
):
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    subscription = resolve_customer_subscription(db, customer)
    if not subscription:
        return JSONResponse({"detail": "No active subscription found"}, status_code=404)
    subscription_id = subscription.id
    # Streaming responses keep dependencies alive until disconnect. Release the
    # lookup session now; the SSE helper opens short read sessions only when it
    # needs a Postgres fallback sample.
    db.rollback()
    db.close()

    return EventSourceResponse(
        customer_portal_bandwidth_service.live_bandwidth_events(
            subscription_id=subscription_id,
            is_disconnected=request.is_disconnected,
        ),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/speedtest", response_class=HTMLResponse)
def customer_speedtest(
    request: Request,
    saved: str | None = None,
    subscription_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Customer portal speed test."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/speedtest", status_code=303
        )
    account_id, resolved_subscription_id = customer_portal.resolve_customer_account(
        customer, db
    )
    subscriber_id = account_id or optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscriber account not found"},
            status_code=404,
        )
    selected_subscription = subscription_id or resolved_subscription_id
    page_data = web_network_speedtests_service.portal_page_data(
        db,
        subscriber_id=subscriber_id,
        subscription_id=selected_subscription,
    )

    return templates.TemplateResponse(
        "customer/services/speedtest.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "saved": bool(saved),
            "active_page": "speedtest",
        },
    )


@router.post("/speedtest", response_class=HTMLResponse)
def customer_speedtest_submit(
    request: Request,
    download_mbps: float = Form(...),
    upload_mbps: float = Form(...),
    latency_ms: float | None = Form(None),
    jitter_ms: float | None = Form(None),
    server_name: str | None = Form(None),
    subscription_id: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="speedtest")
    account_id, resolved_subscription_id = customer_portal.resolve_customer_account(
        customer, db
    )
    subscriber_id = account_id or optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscriber account not found"},
            status_code=404,
        )

    web_network_speedtests_service.create_customer_speedtest(
        db,
        subscriber_id=subscriber_id,
        subscription_id=subscription_id or resolved_subscription_id,
        download_mbps=download_mbps,
        upload_mbps=upload_mbps,
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        server_name=server_name,
        user_agent=request.headers.get("user-agent"),
    )
    return RedirectResponse("/portal/speedtest?saved=1", status_code=303)


@router.get("/speedtest/probe-download")
def customer_speedtest_probe_download(
    request: Request,
    size_mb: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
) -> Response:
    """Download payload for browser speed test probing."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    payload = (b"dotmac-speedtest-" * 4096) * size_mb
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


_PROBE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB


@router.post("/speedtest/probe-upload")
def customer_speedtest_probe_upload(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Upload probe endpoint for browser speed test."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    content_length = int(request.headers.get("content-length", 0))
    if content_length > _PROBE_UPLOAD_MAX_BYTES:
        return JSONResponse(
            {"detail": f"Payload too large (max {_PROBE_UPLOAD_MAX_BYTES} bytes)"},
            status_code=413,
        )
    return JSONResponse({"bytes_received": content_length})


@router.get("/account", response_class=HTMLResponse)
def customer_account_root_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/portal/profile", status_code=303)


@router.get("/account/{path:path}", response_class=HTMLResponse)
def customer_account_path_redirect(request: Request, path: str) -> RedirectResponse:
    target = f"/portal/{path}" if path else "/portal/profile"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/services", response_class=HTMLResponse)
def customer_services(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer active services."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/services", status_code=303
        )
    services_data = customer_portal.get_services_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/services/index.html",
        {
            "request": request,
            "customer": customer,
            **services_data,
            "active_page": "services",
        },
    )


@router.get("/services/{subscription_id}", response_class=HTMLResponse)
def customer_service_detail(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer service detail page."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    detail = customer_portal.get_service_detail(db, customer, str(subscription_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "services",
        },
    )


@router.post("/services/{subscription_id}/reboot", response_class=HTMLResponse)
def customer_reboot_service_ont(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Submit a customer self-service ONT reboot request."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="services")

    from app.services.customer_device_commands import (
        CustomerDeviceCommandError,
        reboot_subscription_device,
    )

    account_id = require_customer_account_id(db, customer)
    try:
        outcome = reboot_subscription_device(
            db,
            subscriber_id=UUID(str(account_id)),
            subscription_id=subscription_id,
            actor_id=str(customer.get("id") or account_id),
        )
        ok, message = outcome.success, outcome.message
    except CustomerDeviceCommandError as exc:
        outcome = None
        ok, message = False, str(exc)
    if outcome is not None and outcome.success:
        _emit_customer_event(
            db,
            "customer_service_reboot_requested",
            {"subscription_id": str(subscription_id)},
        )
    status = "rebooted" if ok else "reboot_error"
    evidence = ""
    if outcome is not None:
        evidence = (
            f"&command={outcome.command.value}&command_status={outcome.status.value}"
            f"&device_id={outcome.device_id}"
            f"&operation_id={outcome.operation_id or ''}"
        )
    return RedirectResponse(
        url=(
            f"/portal/services/{subscription_id}?{status}=true"
            f"&message={quote_plus(message)}{evidence}"
        ),
        status_code=303,
    )


@router.post("/services/{subscription_id}/wifi", response_class=HTMLResponse)
def customer_update_service_wifi(
    request: Request,
    subscription_id: UUID,
    ssid: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Submit a customer self-service WiFi SSID/password update."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="services")

    from app.services.customer_device_commands import (
        CustomerDeviceCommandError,
        update_subscription_wifi,
    )

    account_id = require_customer_account_id(db, customer)
    try:
        if password.strip() != password_confirm.strip():
            raise CustomerDeviceCommandError(
                "wifi_password_mismatch", "WiFi passwords do not match"
            )
        outcome = update_subscription_wifi(
            db,
            subscriber_id=UUID(str(account_id)),
            subscription_id=subscription_id,
            actor_id=str(customer.get("id") or account_id),
            ssid=ssid,
            password=password.strip() or None,
        )
        ok, message = outcome.success, outcome.message
    except CustomerDeviceCommandError as exc:
        outcome = None
        ok, message = False, str(exc)
    if outcome is not None and outcome.success:
        _emit_customer_event(
            db,
            "customer_wifi_updated",
            {"subscription_id": str(subscription_id), "ssid_updated": True},
        )
    status = "wifi_updated" if ok else "wifi_error"
    evidence = ""
    if outcome is not None:
        evidence = (
            f"&command={outcome.command.value}&command_status={outcome.status.value}"
            f"&device_id={outcome.device_id}"
            f"&operation_id={outcome.operation_id or ''}"
        )
    return RedirectResponse(
        url=(
            f"/portal/services/{subscription_id}?{status}=true"
            f"&message={quote_plus(message)}{evidence}"
        ),
        status_code=303,
    )


@router.get("/installations", response_class=HTMLResponse)
def customer_installations(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer installation appointments."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/installations", status_code=303
        )
    appt_data = customer_portal.get_customer_appointments(
        db=db,
        customer=customer,
        status=status,
        page=page,
        per_page=per_page,
    )

    return templates.TemplateResponse(
        "customer/installations/index.html",
        {
            "request": request,
            "customer": customer,
            "appointments": appt_data["appointments"],
            "status": status,
            "page": page,
            "per_page": per_page,
            "total": appt_data["total"],
            "total_pages": appt_data["total_pages"],
            "active_page": "installations",
        },
    )


@router.get("/service-orders", response_class=HTMLResponse)
def customer_service_orders(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """Customer service orders."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/service-orders", status_code=303
        )
    orders_data = customer_portal.get_service_orders_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/service-orders/index.html",
        {
            "request": request,
            "customer": customer,
            **orders_data,
            "active_page": "service-orders",
        },
    )


@router.get("/installations/{appointment_id}", response_class=HTMLResponse)
def customer_installation_detail(
    request: Request,
    appointment_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Customer installation appointment detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    detail = customer_portal.get_installation_detail(db, customer, str(appointment_id))
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Installation not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/installations/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "installations",
        },
    )


@router.get("/service-orders/{service_order_id}", response_class=HTMLResponse)
def customer_service_order_detail(
    request: Request,
    service_order_id: UUID,
    signed: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Customer service order detail view."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    detail = customer_portal.get_service_order_detail(
        db, customer, str(service_order_id)
    )
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Service order not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/service-orders/detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "signed": signed,
            "active_page": "service-orders",
        },
    )


@router.get("/notifications", response_class=HTMLResponse)
def customer_notifications(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
    db: Session = Depends(get_db),
) -> Response:
    """Customer notification inbox."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/notifications", status_code=303
        )
    return templates.TemplateResponse(
        "customer/notifications/index.html",
        {
            "request": request,
            "customer": customer,
            **customer_portal.get_notifications_page(
                db,
                customer,
                page=page,
                per_page=per_page,
            ),
            "active_page": "notifications",
        },
    )


@router.post("/notifications/read", response_class=HTMLResponse)
def customer_notifications_mark_read(
    request: Request,
    read_key: str | None = Form(None),
    all_visible: bool = Form(False),
    return_to: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Mark one or all visible customer notifications as read."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="notifications")
    customer_notifications_service.mark_notifications_read(
        db,
        customer,
        read_key=read_key,
        all_visible=all_visible,
    )
    return RedirectResponse(
        url=_safe_portal_return_path(return_to),
        status_code=303,
    )


def _profile_context(
    request: Request,
    db: Session,
    customer: dict,
    *,
    saved: str | None = None,
    verify_sent: str | None = None,
    sessions: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    from app.models.subscriber import Subscriber as _Subscriber

    subscriber = None
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if subscriber_id:
        subscriber = db.get(_Subscriber, subscriber_id)
    mfa_methods = []
    if subscriber_id:
        mfa_methods = web_customer_auth_service.list_active_mfa_methods(
            db, subscriber_id
        )
    current_session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    active_sessions = (
        customer_portal.list_customer_sessions_for_subscriber(
            subscriber_id,
            current_session_token=current_session_token,
        )
        if subscriber_id
        else []
    )
    success = None
    if sessions == "signed-out":
        success = "Other portal sessions signed out."
    elif saved:
        success = "Profile updated successfully"
    profile_completion = _profile_completion(subscriber) if subscriber else None
    return {
        "request": request,
        "customer": customer,
        "subscriber": subscriber,
        "mfa_methods": mfa_methods,
        "mfa_enabled": any(
            bool(method.enabled and method.is_active) for method in mfa_methods
        ),
        "active_sessions": active_sessions,
        "other_session_count": sum(
            1 for session in active_sessions if not session["is_current"]
        ),
        "active_page": "profile",
        "success": success,
        "error": error,
        "verify_sent": verify_sent,
        "profile_completion": profile_completion,
    }


@router.get("/profile", response_class=HTMLResponse)
def customer_profile(
    request: Request,
    saved: str | None = None,
    verify_sent: str | None = None,
    sessions: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Customer profile settings."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/profile", status_code=303
        )
    return templates.TemplateResponse(
        "customer/profile/index.html",
        _profile_context(
            request,
            db,
            customer,
            saved=saved,
            verify_sent=verify_sent,
            sessions=sessions,
        ),
    )


@router.post("/profile/sessions/sign-out-others")
def customer_profile_sign_out_other_sessions(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Sign out other customer portal sessions while keeping the current one."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="profile")
    subscriber_id = optional_customer_subscriber_id(db, customer)
    current_session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if subscriber_id:
        customer_portal.revoke_other_customer_sessions_for_subscriber(
            subscriber_id,
            current_session_token,
            db=db,
        )
    return RedirectResponse(url="/portal/profile?sessions=signed-out", status_code=303)


@router.post("/profile", response_class=HTMLResponse)
def customer_update_profile(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    display_name: str = Form(None),
    phone: str = Form(None),
    nin: str = Form(None),
    date_of_birth: str = Form(None),
    gender: str = Form(None),
    preferred_contact_method: str = Form(None),
    address_line1: str = Form(None),
    address_line2: str = Form(None),
    city: str = Form(None),
    region: str = Form(None),
    postal_code: str = Form(None),
    country_code: str = Form(None),
    billing_notifications: bool = Form(False),
    sms_updates: bool = Form(False),
    push_notifications: bool = Form(False),
    service_notifications: bool = Form(False),
    account_notifications: bool = Form(False),
    usage_notifications: bool = Form(False),
    general_notifications: bool = Form(False),
    locale: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Update customer profile."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="profile")
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        return templates.TemplateResponse(
            "customer/profile/index.html",
            _profile_context(
                request,
                db,
                customer,
                error="We could not find your customer account. Please contact support.",
            ),
            status_code=404,
        )
    if subscriber_id:
        from app.models.subscriber import Subscriber
        from app.services.web_customer_actions import update_customer_profile

        subscriber_before = db.get(Subscriber, subscriber_id)
        before_snapshot = (
            _profile_audit_snapshot(subscriber_before) if subscriber_before else {}
        )
        try:
            updated = update_customer_profile(
                db,
                subscriber_id=subscriber_id,
                first_name=first_name,
                last_name=last_name,
                display_name=display_name,
                email=email,
                phone=phone,
                nin=nin,
                date_of_birth=date_of_birth,
                gender=gender,
                preferred_contact_method=preferred_contact_method,
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                region=region,
                postal_code=postal_code,
                country_code=country_code,
                billing_notifications=billing_notifications,
                sms_updates=sms_updates,
                push_notifications=push_notifications,
                service_notifications=service_notifications,
                account_notifications=account_notifications,
                usage_notifications=usage_notifications,
                general_notifications=general_notifications,
                locale=locale,
            )
        except (ValueError, IntegrityError) as exc:
            db.rollback()
            logger.info("customer_profile_update_rejected", exc_info=True)
            return templates.TemplateResponse(
                "customer/profile/index.html",
                _profile_context(
                    request,
                    db,
                    customer,
                    error=str(exc) or "We could not save those profile changes.",
                ),
                status_code=400,
            )
        except Exception:
            db.rollback()
            logger.exception("customer_profile_update_failed")
            return templates.TemplateResponse(
                "customer/profile/index.html",
                _profile_context(
                    request,
                    db,
                    customer,
                    error="We could not save your profile right now. Please try again.",
                ),
                status_code=500,
            )
        if updated is None:
            return templates.TemplateResponse(
                "customer/profile/index.html",
                _profile_context(
                    request,
                    db,
                    customer,
                    error="We could not find your customer account. Please contact support.",
                ),
                status_code=404,
            )
        if updated is not None:
            after_snapshot = _profile_audit_snapshot(updated)
            changes = _profile_audit_changes(before_snapshot, after_snapshot)
            if changes:
                try:
                    log_audit_event(
                        db=db,
                        request=request,
                        action="portal_profile_update",
                        entity_type="subscriber",
                        entity_id=str(subscriber_id),
                        actor_id=str(subscriber_id),
                        metadata={"changes": changes, "source": "customer_portal"},
                    )
                except Exception:
                    db.rollback()
                    logger.exception(
                        "Unable to log customer portal profile audit for %s",
                        subscriber_id,
                    )

    # POST-Redirect-GET: bounce to the profile page with a success flag so a
    # browser refresh after save can't accidentally resubmit the form.
    return RedirectResponse(url="/portal/profile?saved=1", status_code=303)


@router.post("/profile/verify-email/resend")
def customer_resend_email_verification(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Resend the email-verification link to the caller's own address."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="profile")
    sent = False
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if subscriber_id:
        try:
            sent = auth_flow_service.send_email_verification(db, str(subscriber_id))
        except Exception:
            logger.warning("customer_resend_email_verification_failed", exc_info=True)
    return RedirectResponse(
        url=f"/portal/profile?verify_sent={'1' if sent else '0'}",
        status_code=303,
    )


@router.get("/profile/mfa/setup", response_class=HTMLResponse)
def customer_mfa_setup(request: Request, db: Session = Depends(get_db)) -> Response:
    """Customer MFA setup page."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/profile", status_code=303
        )
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="profile")
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        return RedirectResponse(url="/portal/profile", status_code=303)

    setup = auth_flow_service.auth_flow.mfa_setup(
        db, str(subscriber_id), "Authenticator app"
    )
    return templates.TemplateResponse(
        "customer/profile/mfa_setup.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "profile",
            "method_id": setup["method_id"],
            "secret_key": setup["secret"],
            "otpauth_uri": setup["otpauth_uri"],
        },
    )


@router.post("/profile/mfa/setup", response_class=HTMLResponse)
def customer_mfa_setup_post(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    """Start customer MFA setup."""
    return customer_mfa_setup(request, db)


@router.post("/profile/mfa/confirm", response_class=HTMLResponse)
def customer_mfa_confirm(
    request: Request,
    method_id: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Confirm customer MFA setup."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="profile")
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        return RedirectResponse(url="/portal/profile", status_code=303)

    try:
        method = auth_flow_service.auth_flow.mfa_confirm(
            db, method_id, code.strip(), str(subscriber_id)
        )
    except Exception:
        return templates.TemplateResponse(
            "customer/profile/mfa_setup.html",
            {
                "request": request,
                "customer": customer,
                "active_page": "profile",
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "error": "Invalid verification code. Please try again.",
            },
            status_code=401,
        )

    recovery_codes = (
        auth_flow_service.generate_mfa_recovery_codes(db, method)
        if getattr(method, "id", None)
        else []
    )
    if recovery_codes:
        return templates.TemplateResponse(
            "customer/profile/mfa_setup.html",
            {
                "request": request,
                "customer": customer,
                "active_page": "profile",
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "recovery_codes": recovery_codes,
                "continue_url": "/portal/profile?saved=security",
            },
        )

    return RedirectResponse(url="/portal/profile?saved=security", status_code=303)


@router.get("/contacts", response_class=HTMLResponse)
def customer_contacts(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Linked account contacts."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/contacts", status_code=303
        )
    return templates.TemplateResponse(
        "customer/contacts/index.html",
        {
            "request": request,
            "customer": customer,
            **customer_portal_contacts_service.get_contacts_page(db, customer),
            "active_page": "contacts",
        },
    )


@router.post("/contacts", response_class=HTMLResponse)
def customer_contacts_create(
    request: Request,
    full_name: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    whatsapp: str | None = Form(None),
    facebook: str | None = Form(None),
    instagram: str | None = Form(None),
    x_handle: str | None = Form(None),
    telegram: str | None = Form(None),
    linkedin: str | None = Form(None),
    other_social: str | None = Form(None),
    relationship: str | None = Form(None),
    contact_type: str | None = Form("general"),
    is_authorized: bool = Form(False),
    receives_notifications: bool = Form(False),
    is_billing_contact: bool = Form(False),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Create a linked contact without creating portal login access."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="contacts")

    form = customer_portal_contacts_service.normalize_contact_form(
        full_name=full_name,
        phone=phone,
        email=email,
        whatsapp=whatsapp,
        facebook=facebook,
        instagram=instagram,
        x_handle=x_handle,
        telegram=telegram,
        linkedin=linkedin,
        other_social=other_social,
        relationship=relationship,
        contact_type=contact_type,
        is_authorized=is_authorized,
        receives_notifications=receives_notifications,
        is_billing_contact=is_billing_contact,
        notes=notes,
        allow_authority_flags=False,
    )
    try:
        warnings = customer_portal_contacts_service.create_contact(db, customer, form)
    except ValueError as exc:
        return templates.TemplateResponse(
            "customer/contacts/index.html",
            {
                "request": request,
                "customer": customer,
                **customer_portal_contacts_service.get_contacts_page(db, customer),
                "error": str(exc),
                "form_values": form,
                "active_page": "contacts",
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        "customer/contacts/index.html",
        {
            "request": request,
            "customer": customer,
            **customer_portal_contacts_service.get_contacts_page(db, customer),
            "success": "Contact added.",
            "warnings": warnings,
            "active_page": "contacts",
        },
    )


@router.post("/contacts/{contact_id}", response_class=HTMLResponse)
def customer_contacts_update(
    request: Request,
    contact_id: str,
    intent: str | None = Form(None),
    full_name: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    whatsapp: str | None = Form(None),
    facebook: str | None = Form(None),
    instagram: str | None = Form(None),
    x_handle: str | None = Form(None),
    telegram: str | None = Form(None),
    linkedin: str | None = Form(None),
    other_social: str | None = Form(None),
    relationship: str | None = Form(None),
    contact_type: str | None = Form("general"),
    is_authorized: bool = Form(False),
    receives_notifications: bool = Form(False),
    is_billing_contact: bool = Form(False),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Update a linked contact owned by the logged-in subscriber account."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="contacts")

    if intent == "delete":
        try:
            customer_portal_contacts_service.delete_contact(db, customer, contact_id)
        except ValueError as exc:
            return templates.TemplateResponse(
                "customer/contacts/index.html",
                {
                    "request": request,
                    "customer": customer,
                    **customer_portal_contacts_service.get_contacts_page(db, customer),
                    "error": str(exc),
                    "active_page": "contacts",
                },
                status_code=400,
            )

        return templates.TemplateResponse(
            "customer/contacts/index.html",
            {
                "request": request,
                "customer": customer,
                **customer_portal_contacts_service.get_contacts_page(db, customer),
                "success": "Contact deleted.",
                "active_page": "contacts",
            },
        )

    form = customer_portal_contacts_service.normalize_contact_form(
        full_name=full_name,
        phone=phone,
        email=email,
        whatsapp=whatsapp,
        facebook=facebook,
        instagram=instagram,
        x_handle=x_handle,
        telegram=telegram,
        linkedin=linkedin,
        other_social=other_social,
        relationship=relationship,
        contact_type=contact_type,
        is_authorized=is_authorized,
        receives_notifications=receives_notifications,
        is_billing_contact=is_billing_contact,
        notes=notes,
        allow_authority_flags=False,
    )
    existing_contact = customer_portal_contacts_service.get_owned_contact(
        db, customer, contact_id
    )
    if existing_contact:
        form = replace(
            form,
            is_authorized=bool(existing_contact.is_authorized),
            is_billing_contact=bool(existing_contact.is_billing_contact),
        )
    try:
        warnings = customer_portal_contacts_service.update_contact(
            db, customer, contact_id, form
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "customer/contacts/index.html",
            {
                "request": request,
                "customer": customer,
                **customer_portal_contacts_service.get_contacts_page(db, customer),
                "error": str(exc),
                "active_page": "contacts",
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        "customer/contacts/index.html",
        {
            "request": request,
            "customer": customer,
            **customer_portal_contacts_service.get_contacts_page(db, customer),
            "success": "Contact updated.",
            "warnings": warnings,
            "active_page": "contacts",
        },
    )


# =============================================================================
# Online Payment (Paystack / Flutterwave)
# =============================================================================


@router.get("/billing/pay", response_class=HTMLResponse)
def customer_pay_invoice(
    request: Request,
    invoice: str = Query(...),
    db: Session = Depends(get_db),
) -> Response:
    """Show payment page for an invoice."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    page_data = customer_portal.get_payment_page(db, customer, invoice)
    if not page_data:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Invoice not found or already paid"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/pay.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "billing",
        },
    )


@router.post("/billing/pay/intent")
def customer_create_invoice_payment_intent(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Start an invoice payment with the customer's chosen method."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    if customer.get("read_only"):
        return JSONResponse(
            {"detail": "View-only sessions cannot make payments"}, status_code=403
        )

    invoice_id = str(payload.get("invoice") or payload.get("invoice_id") or "").strip()
    if not invoice_id:
        return JSONResponse({"detail": "invoice is required"}, status_code=400)
    # An Idempotency-Key header (or body token) makes saved-card charges safe
    # against double-submit; gateway-redirect flows ignore it.
    idempotency_key = request.headers.get("Idempotency-Key") or payload.get(
        "idempotency_key"
    )
    try:
        result = customer_portal.create_invoice_payment_intent(
            db,
            customer,
            invoice_id,
            provider=payload.get("provider"),
            payment_method_id=payload.get("payment_method_id"),
            redirect_url=str(request.url_for("customer_verify_payment")),
            idempotency_key=idempotency_key,
        )
    except (ValueError, HTTPException) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception:
        logger.warning(
            "Unable to start customer invoice payment intent",
            extra={
                "invoice_id": invoice_id,
                "account_id": optional_customer_account_id(db, customer),
            },
            exc_info=True,
        )
        return JSONResponse({"detail": PAYMENT_START_ERROR_MESSAGE}, status_code=400)

    return JSONResponse(content=jsonable_encoder(result))


@router.get("/billing/pay/verify", response_class=HTMLResponse)
def customer_verify_payment(
    request: Request,
    reference: str = Query(...),
    provider: str | None = Query(None),
    save_card: bool = Query(False),
    db: Session = Depends(get_db),
) -> Response:
    """Verify online payment and record it."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        # Check if subscriber was restricted before payment
        subscriber_id = optional_customer_subscriber_id(db, customer)
        was_restricted = bool(
            subscriber_id and is_subscriber_restricted(db, subscriber_id)
        )

        result = customer_portal.verify_and_record_payment(
            db, customer, reference, provider=provider
        )
        # Close out the pending invoice-checkout trace (no-op for references
        # that never had one, e.g. saved-card charges or the bearer API).
        customer_portal.complete_invoice_payment_intent(
            db, reference, result.get("payment")
        )
        card_save = _capture_card_save_status(
            db,
            account_id=str(optional_customer_account_id(db, customer) or ""),
            reference=reference,
            provider=result["provider_type"],
            requested=save_card,
        )
        service_restored = bool(
            was_restricted
            and subscriber_id
            and not is_subscriber_restricted(db, subscriber_id)
        )
        return templates.TemplateResponse(
            "customer/billing/pay_success.html",
            {
                "request": request,
                "customer": customer,
                "payment": result["payment"],
                "invoice": result["invoice"],
                "amount": result["amount"],
                "reference": result["reference"],
                "allocated_total": result.get("allocated_total"),
                "amount_credited": result.get("amount_credited"),
                "prepaid_amount_applied": result.get("prepaid_amount_applied"),
                "credit_added": result.get("credit_added"),
                "available_balance": result.get("available_balance"),
                "renewed_services": result.get("renewed_services", []),
                "already_recorded": result.get("already_recorded", False),
                "was_restricted": was_restricted,
                "service_restored": service_restored,
                "card_save": card_save,
                "active_page": "billing",
            },
        )
    except (ValueError, HTTPException) as exc:
        return _render_payment_return_status(
            request,
            reference=reference,
            provider=provider,
            flow="invoice_payment",
            exc=exc,
        )
    except Exception as exc:
        return _render_payment_return_status(
            request,
            reference=reference,
            provider=provider,
            flow="invoice_payment",
            exc=exc,
        )


@router.get("/billing/topup", response_class=HTMLResponse)
def customer_billing_topup(
    request: Request,
    autopay_error: str | None = Query(None),
    autopay_success: str | None = Query(None),
    transfer_success: str | None = Query(None),
    add_card: bool = Query(False),
    amount: Decimal | None = Query(None, ge=0),
    db: Session = Depends(get_db),
) -> Response:
    """Show Deposit Account Credit for a customer account."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    page_data = customer_portal.get_topup_page(db, customer)
    return templates.TemplateResponse(
        "customer/billing/topup.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "autopay": autopay_service.get_status(
                db, str(optional_customer_account_id(db, customer) or "")
            ),
            "autopay_error": autopay_error,
            "autopay_success": autopay_success,
            "transfer_success": transfer_success,
            # When arriving from "Add card", default the provider to Paystack
            # and pre-tick "save this card" so the top-up doubles as adding a
            # reusable card.
            "add_card": add_card,
            # Optional prefill handed off from the dashboard "Pay Now" panel.
            "prefill_amount": amount,
            "active_page": "billing",
        },
    )


@router.post("/billing/topup/intent")
def customer_create_topup_intent(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create a server-owned account-credit deposit intent for checkout."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    if _is_read_only_customer(customer):
        return JSONResponse({"detail": READ_ONLY_MUTATION_MESSAGE}, status_code=403)

    amount_value = payload.get("amount")
    if amount_value is None:
        return JSONResponse({"detail": "amount is required"}, status_code=400)
    # An Idempotency-Key header (or body token) makes saved-card charges safe
    # against double-submit; gateway-redirect flows ignore it.
    idempotency_key = request.headers.get("Idempotency-Key") or payload.get(
        "idempotency_key"
    )
    try:
        result = customer_portal.create_topup_intent(
            db,
            customer,
            amount_value,
            provider=payload.get("provider"),
            payment_method_id=payload.get("payment_method_id"),
            preview_fingerprint=payload.get("preview_fingerprint"),
            redirect_url=str(request.url_for("customer_verify_topup")),
            idempotency_key=idempotency_key,
        )
    except (ValueError, HTTPException) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except Exception:
        logger.warning(
            "Unable to start customer account-credit deposit intent",
            extra={"account_id": optional_customer_account_id(db, customer)},
            exc_info=True,
        )
        return JSONResponse({"detail": PAYMENT_START_ERROR_MESSAGE}, status_code=400)

    return JSONResponse(content=jsonable_encoder(result))


@router.post("/billing/topup/preview")
def customer_preview_topup(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return the server-owned Deposit Account Credit allocation preview."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    amount_value = payload.get("amount")
    if amount_value is None:
        return JSONResponse({"detail": "amount is required"}, status_code=400)
    try:
        result = customer_portal.preview_topup(db, customer, amount_value)
    except (ValueError, HTTPException) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse(content=jsonable_encoder(result))


@router.get("/billing/topup/transfer", response_class=HTMLResponse)
def customer_direct_transfer_topup(
    request: Request,
    error: str | None = Query(None),
    submitted: bool = Query(False),
    db: Session = Depends(get_db),
) -> Response:
    """Show direct bank transfer instructions for the pending top-up intent."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        page_data = customer_portal.get_direct_transfer_topup_page(db, customer)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/portal/billing/topup?autopay_error={quote_plus(str(exc))}",
            status_code=303,
        )
    return templates.TemplateResponse(
        "customer/billing/topup_transfer.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "form_error": error,
            "submitted": submitted,
            "active_page": "billing",
        },
    )


@router.post("/billing/topup/transfer", response_class=HTMLResponse)
async def customer_direct_transfer_topup_submit(
    request: Request,
    made_payment: str | None = Form(None),
    selected_account_id: str | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    """Submit direct bank transfer proof for staff review."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if customer.get("read_only"):
        return RedirectResponse(
            url=(
                "/portal/billing/topup/transfer?error="
                f"{quote_plus('View-only sessions cannot submit payments')}"
            ),
            status_code=303,
        )
    try:
        proof = await customer_portal.submit_direct_transfer_topup(
            db,
            customer,
            made_payment=str(made_payment or "").lower() in {"1", "true", "on", "yes"},
            selected_account_id=selected_account_id,
            file=file,
        )
    except (ValueError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return RedirectResponse(
            url=f"/portal/billing/topup/transfer?error={quote_plus(str(detail))}",
            status_code=303,
        )
    except Exception:
        logger.warning("customer_direct_transfer_submit_failed", exc_info=True)
        return RedirectResponse(
            url="/portal/billing/topup/transfer?error=Unable to submit payment proof",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/portal/billing/topup/transfer/submitted?proof_id={quote_plus(str(proof.get('id') or ''))}",
        status_code=303,
    )


@router.get("/billing/topup/transfer/submitted", response_class=HTMLResponse)
def customer_direct_transfer_topup_submitted(
    request: Request,
    proof_id: str = Query(...),
    db: Session = Depends(get_db),
) -> Response:
    """Show acknowledgement after a direct-transfer receipt is submitted."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    proof = payment_proofs_service.get_proof(db, proof_id)
    account_id = optional_customer_account_id(db, customer)
    if proof is None or str(proof.account_id) != str(account_id or ""):
        return RedirectResponse(
            url="/portal/billing/topup?autopay_error="
            + quote_plus("Submitted transfer receipt was not found."),
            status_code=303,
        )
    return templates.TemplateResponse(
        "customer/billing/topup_transfer_submitted.html",
        {
            "request": request,
            "customer": customer,
            "proof": proof,
            "active_page": "billing",
        },
    )


@router.get("/billing/topup/verify", response_class=HTMLResponse)
def customer_verify_topup(
    request: Request,
    reference: str = Query(...),
    provider: str | None = Query(None),
    save_card: bool = Query(False),
    db: Session = Depends(get_db),
) -> Response:
    """Verify an account-credit deposit and show allocation results."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        subscriber_id = optional_customer_subscriber_id(db, customer)
        was_restricted = bool(
            subscriber_id and is_subscriber_restricted(db, subscriber_id)
        )

        result = customer_portal.verify_and_record_topup(
            db, customer, reference, provider=provider
        )
        card_save = _capture_card_save_status(
            db,
            account_id=str(optional_customer_account_id(db, customer) or ""),
            reference=reference,
            provider=result["provider_type"],
            requested=save_card,
        )
        service_restored = bool(
            was_restricted
            and subscriber_id
            and not is_subscriber_restricted(db, subscriber_id)
        )
        return templates.TemplateResponse(
            "customer/billing/topup_success.html",
            {
                "request": request,
                "customer": customer,
                "payment": result["payment"],
                "amount": result["amount"],
                "reference": result["reference"],
                "already_recorded": result["already_recorded"],
                "allocated_to_invoices": result["allocated_to_invoices"],
                "allocated_total": result["allocated_total"],
                "amount_credited": result.get("amount_credited"),
                "prepaid_amount_applied": result.get("prepaid_amount_applied", 0),
                "credit_added": result["credit_added"],
                "available_balance": result["available_balance"],
                "renewed_services": result.get("renewed_services", []),
                "policy_warnings": result["policy_warnings"],
                "was_restricted": was_restricted,
                "service_restored": service_restored,
                "card_save": card_save,
                "active_page": "billing",
            },
        )
    except (ValueError, HTTPException) as exc:
        return _render_payment_return_status(
            request,
            reference=reference,
            provider=provider,
            flow="account_topup",
            exc=exc,
        )
    except Exception as exc:
        return _render_payment_return_status(
            request,
            reference=reference,
            provider=provider,
            flow="account_topup",
            exc=exc,
        )


@router.post("/billing/autopay/enable")
def customer_autopay_enable(
    request: Request,
    payment_method_id: str = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Enable autopay against a saved card (the default card if unspecified)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return RedirectResponse(
            url="/portal/billing/topup?autopay_error="
            + quote_plus("View-only sessions cannot change autopay."),
            status_code=303,
        )
    # Only a real, non-empty form value selects a card; otherwise the default
    # saved card is used (also keeps a direct call's Form default inert).
    method_id = (
        payment_method_id.strip()
        if isinstance(payment_method_id, str) and payment_method_id.strip()
        else None
    )
    try:
        autopay_service.enable(
            db, str(optional_customer_account_id(db, customer) or ""), method_id
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/portal/billing/topup?autopay_error={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        url="/portal/billing/topup?autopay_success="
        + quote_plus("Autopay is on — due invoices will be charged automatically."),
        status_code=303,
    )


@router.post("/billing/autopay/disable")
def customer_autopay_disable(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Turn off autopay for the caller."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return RedirectResponse(
            url="/portal/billing/topup?autopay_error="
            + quote_plus("View-only sessions cannot change autopay."),
            status_code=303,
        )
    autopay_service.disable(db, str(optional_customer_account_id(db, customer) or ""))
    return RedirectResponse(
        url="/portal/billing/topup?autopay_success=" + quote_plus("Autopay is off."),
        status_code=303,
    )


@router.get("/billing/payment-methods", response_class=HTMLResponse)
def customer_payment_methods(
    request: Request,
    saved: str | None = Query(None),
    error: str | None = Query(None),
    db: Session = Depends(get_db),
) -> Response:
    """Manage saved cards, autopay, and the bank-transfer method in one place."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/billing/payment-methods",
            status_code=303,
        )
    page_data = customer_portal.get_payment_methods_page(db, customer)
    return templates.TemplateResponse(
        "customer/billing/payment_methods.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "autopay": autopay_service.get_status(
                db, str(optional_customer_account_id(db, customer) or "")
            ),
            "success": saved,
            "form_error": error,
            "active_page": "billing",
        },
    )


@router.post("/billing/payment-methods/{method_id}/default")
def customer_payment_method_set_default(
    request: Request,
    method_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Set a saved card as the default (self-scoped to the caller)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if customer.get("read_only"):
        return RedirectResponse(
            url="/portal/billing/payment-methods?error="
            + quote_plus("View-only sessions cannot change payment methods."),
            status_code=303,
        )
    updated = customer_cards.set_default(
        db, str(optional_customer_account_id(db, customer) or ""), method_id
    )
    if updated is None:
        return RedirectResponse(
            url="/portal/billing/payment-methods?error="
            + quote_plus("Card not found."),
            status_code=303,
        )
    return RedirectResponse(
        url="/portal/billing/payment-methods?saved="
        + quote_plus("Default card updated."),
        status_code=303,
    )


@router.post("/billing/payment-methods/{method_id}/remove")
def customer_payment_method_remove(
    request: Request,
    method_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Remove a saved card (also deactivates any autopay mandate on it)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if customer.get("read_only"):
        return RedirectResponse(
            url="/portal/billing/payment-methods?error="
            + quote_plus("View-only sessions cannot change payment methods."),
            status_code=303,
        )
    removed = customer_cards.remove(
        db, str(optional_customer_account_id(db, customer) or ""), method_id
    )
    if not removed:
        return RedirectResponse(
            url="/portal/billing/payment-methods?error="
            + quote_plus("Card not found."),
            status_code=303,
        )
    return RedirectResponse(
        url="/portal/billing/payment-methods?saved=" + quote_plus("Card removed."),
        status_code=303,
    )


# =============================================================================
# Plan Change Self-Service
# =============================================================================


@router.get("/services/{subscription_id}/change", response_class=HTMLResponse)
def customer_change_plan(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Show available plans for changing subscription."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    page_data = customer_portal.get_change_plan_page(db, customer, str(subscription_id))
    if not page_data:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Subscription not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/change_plan.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "services",
        },
    )


@router.get("/services/{subscription_id}/change/quote", response_class=JSONResponse)
def customer_change_plan_quote(
    request: Request,
    subscription_id: UUID,
    offer_id: str,
    target_service_address_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Lazily compute the prorated plan-change quote for one target offer.

    The change-plan page fetches this on offer selection instead of pricing the
    whole catalog up front (which timed out for large catalogs).
    """
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        quote = customer_portal.get_plan_change_quote(
            db,
            customer,
            str(subscription_id),
            offer_id,
            target_service_address_id=target_service_address_id,
        )
    except PrepaidFundingBaselineMissingError:
        return JSONResponse(
            {
                "error": "financial_position_unavailable",
                "detail": SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE,
            },
            status_code=409,
        )
    if quote is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"quote": quote})


@router.post("/services/{subscription_id}/change", response_class=HTMLResponse)
def customer_submit_change_plan(
    request: Request,
    subscription_id: UUID,
    offer_id: str = Form(...),
    target_service_address_id: str | None = Form(None),
    notes: str = Form(None),
    preview_fingerprint: str = Form(...),
    field_quote_fingerprint: str | None = Form(None),
    preview_effective_at: datetime = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Confirm a service change using the owner-selected delivery mode."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="services")
    try:
        result = customer_portal.confirm_service_change(
            db=db,
            customer=customer,
            subscription_id=str(subscription_id),
            offer_id=offer_id,
            target_service_address_id=target_service_address_id,
            notes=notes,
            preview_fingerprint=preview_fingerprint,
            field_quote_fingerprint=field_quote_fingerprint,
            preview_effective_at=preview_effective_at,
            idempotency_key=idempotency_key,
            confirmation_origin="customer_web",
        )
        if not result.get("success", False):
            error_ctx = customer_portal.get_change_plan_error_context(
                db,
                str(subscription_id),
                selected_offer_id=str(result.get("selected_offer_id") or offer_id),
                insufficient_funding={
                    "required_amount": result.get("required_amount"),
                    "prepaid_funding_before": result.get("prepaid_funding_before"),
                    "shortfall": result.get("shortfall"),
                    "quote": result.get("plan_change_quote"),
                },
            )
            return templates.TemplateResponse(
                "customer/services/change_plan.html",
                {
                    "request": request,
                    "customer": customer,
                    **error_ctx,
                    "error": (
                        "You need additional prepaid funding before this upgrade can be applied."
                    ),
                    "active_page": "services",
                },
                status_code=400,
            )
        status = str(result.get("status") or "applied")
        change_state = (
            str(result.get("delivery_mode") or "pending")
            if status == "scheduled"
            else "applied"
        )
        return RedirectResponse(
            url=f"/portal/services/{subscription_id}?service_change={change_state}",
            status_code=303,
        )
    except HTTPException as exc:
        db.rollback()
        detail = exc.detail
        message = (
            str(detail.get("message") or detail.get("detail") or detail)
            if isinstance(detail, dict)
            else str(detail)
        )
        error_ctx = customer_portal.get_change_plan_error_context(
            db,
            str(subscription_id),
            selected_offer_id=offer_id,
        )
        return templates.TemplateResponse(
            "customer/services/change_plan.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": message,
                "active_page": "services",
            },
            status_code=exc.status_code,
        )
    except PrepaidFundingBaselineMissingError:
        error_ctx = customer_portal.get_change_plan_error_context(
            db, str(subscription_id), selected_offer_id=offer_id
        )
        return templates.TemplateResponse(
            "customer/services/change_plan.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": SERVICE_CHANGE_FINANCIAL_POSITION_MESSAGE,
                "active_page": "services",
            },
            status_code=409,
        )
    except ValueError as exc:
        message = str(exc)
        error_ctx = customer_portal.get_change_plan_error_context(
            db, str(subscription_id)
        )
        return templates.TemplateResponse(
            "customer/services/change_plan.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": message,
                "active_page": "services",
            },
            status_code=400,
        )
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception(
            "Plan change error for %s", subscription_id
        )
        raise


# =============================================================================
# Service Suspend/Resume Self-Service
# =============================================================================


@router.get("/services/{subscription_id}/suspend", response_class=HTMLResponse)
def customer_suspend_service(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Show vacation hold confirmation page."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    from app.services.customer_portal_flow_services import get_suspend_page

    page_data = get_suspend_page(db, customer, str(subscription_id))
    if not page_data:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {
                "request": request,
                "message": "Subscription not found or cannot be suspended",
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/suspend.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "services",
        },
    )


@router.post("/services/{subscription_id}/suspend", response_class=HTMLResponse)
def customer_submit_suspend_service(
    request: Request,
    subscription_id: UUID,
    days: int = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Apply vacation hold to subscription."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="services")

    from app.services.customer_portal_flow_services import (
        apply_service_suspend,
        get_suspend_page,
    )

    try:
        apply_service_suspend(db, customer, str(subscription_id), days)
        _emit_customer_event(
            db,
            "customer_service_suspended",
            {
                "subscription_id": str(subscription_id),
                "days": days,
            },
        )
        return RedirectResponse(
            url=f"/portal/services/{subscription_id}?suspended=true",
            status_code=303,
        )
    except ValueError as exc:
        page_data = get_suspend_page(db, customer, str(subscription_id))
        if not page_data:
            return templates.TemplateResponse(
                "customer/errors/404.html",
                {"request": request, "message": str(exc)},
                status_code=404,
            )
        return templates.TemplateResponse(
            "customer/services/suspend.html",
            {
                "request": request,
                "customer": customer,
                **page_data,
                "error": str(exc),
                "active_page": "services",
            },
            status_code=400,
        )


@router.get("/services/{subscription_id}/resume", response_class=HTMLResponse)
def customer_resume_service(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Show resume service confirmation page."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    from app.services.customer_portal_flow_services import get_resume_page

    page_data = get_resume_page(db, customer, str(subscription_id))
    if not page_data:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {
                "request": request,
                "message": "Subscription not found or cannot be resumed",
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/services/resume.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "services",
        },
    )


@router.post("/services/{subscription_id}/resume", response_class=HTMLResponse)
def customer_submit_resume_service(
    request: Request,
    subscription_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Resume subscription from vacation hold."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="services")

    from app.services.customer_portal_flow_services import (
        apply_service_resume,
        get_resume_page,
    )

    try:
        apply_service_resume(db, customer, str(subscription_id))
        _emit_customer_event(
            db,
            "customer_service_resumed",
            {
                "subscription_id": str(subscription_id),
            },
        )
        return RedirectResponse(
            url=f"/portal/services/{subscription_id}?resumed=true",
            status_code=303,
        )
    except ValueError as exc:
        page_data = get_resume_page(db, customer, str(subscription_id))
        if not page_data:
            return templates.TemplateResponse(
                "customer/errors/404.html",
                {"request": request, "message": str(exc)},
                status_code=404,
            )
        return templates.TemplateResponse(
            "customer/services/resume.html",
            {
                "request": request,
                "customer": customer,
                **page_data,
                "error": str(exc),
                "active_page": "services",
            },
            status_code=400,
        )


@router.get("/change-requests", response_class=HTMLResponse)
def customer_change_requests(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """List pending plan change requests."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/change-requests", status_code=303
        )
    change_data = customer_portal.get_change_requests_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/services/change_requests.html",
        {
            "request": request,
            "customer": customer,
            **change_data,
            "active_page": "services",
        },
    )


# =============================================================================
# Payment Arrangements Self-Service
# =============================================================================


@router.get("/billing/arrangements", response_class=HTMLResponse)
def customer_payment_arrangements(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
) -> Response:
    """List payment arrangements for the customer."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/billing/arrangements", status_code=303
        )

    arrangements_data = customer_portal.get_payment_arrangements_page(
        db, customer, status=status, page=page, per_page=per_page
    )

    return templates.TemplateResponse(
        "customer/billing/arrangements.html",
        {
            "request": request,
            "customer": customer,
            **arrangements_data,
            "active_page": "billing",
        },
    )


@router.get("/billing/arrangements/new", response_class=HTMLResponse)
def customer_new_payment_arrangement(
    request: Request,
    invoice_id: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """Show form to request a new payment arrangement."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    page_data = customer_portal.get_new_arrangement_page(
        db, customer, invoice_id=invoice_id
    )

    return templates.TemplateResponse(
        "customer/billing/arrangement_form.html",
        {
            "request": request,
            "customer": customer,
            **page_data,
            "active_page": "billing",
        },
    )


@router.post("/billing/arrangements", response_class=HTMLResponse)
def customer_submit_payment_arrangement(
    request: Request,
    total_amount: str = Form(...),
    installments: int = Form(...),
    frequency: str = Form(...),
    start_date: str = Form(...),
    invoice_id: str = Form(None),
    notes: str = Form(None),
    terms: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Submit a payment arrangement request."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="billing")

    try:
        customer_portal.submit_payment_arrangement(
            db=db,
            customer=customer,
            total_amount=total_amount,
            installments=installments,
            frequency=frequency,
            start_date=start_date,
            invoice_id=invoice_id,
            notes=notes,
            terms_accepted=isinstance(terms, str)
            and terms.strip().lower() in {"1", "true", "on", "yes"},
        )
        return RedirectResponse(
            url="/portal/billing/arrangements?submitted=true",
            status_code=303,
        )
    except (ValueError, HTTPException) as exc:
        account_id = optional_customer_account_id(db, customer)
        account_id_str = str(account_id) if account_id else None
        error_ctx = customer_portal.get_arrangement_error_context(db, account_id_str)
        status_code = exc.status_code if isinstance(exc, HTTPException) else 400
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            "customer/billing/arrangement_form.html",
            {
                "request": request,
                "customer": customer,
                **error_ctx,
                "error": str(message),
                "active_page": "billing",
            },
            status_code=status_code,
        )


@router.post(
    "/billing/arrangements/{arrangement_id}/cancel", response_class=HTMLResponse
)
def customer_cancel_payment_arrangement(
    request: Request,
    arrangement_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Cancel a payment arrangement (customers may only cancel pending ones)."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if _is_read_only_customer(customer):
        return _read_only_response(request, customer, active_page="billing")

    try:
        customer_portal.cancel_customer_arrangement(db, customer, str(arrangement_id))
    except HTTPException as exc:
        if exc.status_code == 404:
            return templates.TemplateResponse(
                "customer/errors/404.html",
                {"request": request, "message": "Payment arrangement not found"},
                status_code=404,
            )
        detail = customer_portal.get_payment_arrangement_detail(
            db, customer, str(arrangement_id)
        )
        return templates.TemplateResponse(
            "customer/billing/arrangement_detail.html",
            {
                "request": request,
                "customer": customer,
                **(detail or {}),
                "error": str(exc.detail),
                "active_page": "billing",
            },
            status_code=exc.status_code,
        )

    return RedirectResponse(
        url="/portal/billing/arrangements?canceled=true", status_code=303
    )


@router.get("/billing/arrangements/{arrangement_id}", response_class=HTMLResponse)
def customer_payment_arrangement_detail(
    request: Request,
    arrangement_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """View payment arrangement details."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    detail = customer_portal.get_payment_arrangement_detail(
        db, customer, str(arrangement_id)
    )
    if not detail:
        return templates.TemplateResponse(
            "customer/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )

    return templates.TemplateResponse(
        "customer/billing/arrangement_detail.html",
        {
            "request": request,
            "customer": customer,
            **detail,
            "active_page": "billing",
            # Drives the overdue-installment highlight in the template.
            "today": datetime.now(UTC).date(),
        },
    )
