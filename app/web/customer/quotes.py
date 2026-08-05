"""Customer portal Sales/Quotes page (self-serve installation quotes).

Server-rendered: shows each quote's feasibility, estimate, deposit, and
status. Behind the ``quotes_native_read_enabled`` ownership flag: OFF reads
the local quote mirror (fast and resilient to a CRM outage), while ON reads
Sub's native ``quotes`` table with the same payload shape.
Read-only — the interactive map-pin request + deposit payment live in the
mobile app. Thin wrapper over the service.
"""

import logging
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.portal import QuotePaymentIntentRequest
from app.services import quote_deposits, quotes_mirror
from app.services.customer_context import (
    CustomerContext,
    optional_customer_subscriber_id,
    resolve_customer_context,
)
from app.services.domain_errors import DomainError
from app.services.sales import selfserve as selfserve_service
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(
        url=f"/portal/auth/login?{urlencode({'next': target})}",
        status_code=303,
    )


def _authorized_subscriber_ids(customer: CustomerContext) -> tuple[UUID, ...]:
    identifiers: list[UUID] = []
    for value in customer.allowed_subscriber_ids:
        try:
            identifiers.append(UUID(value))
        except ValueError:
            continue
    return tuple(identifiers)


def _quote_payment_unavailable(
    request: Request,
    customer: dict,
    error: DomainError,
) -> Response:
    hidden = error.code.endswith(".quote_not_found") or error.code.endswith(
        ".unauthorized"
    )
    return templates.TemplateResponse(
        "customer/quotes/payment_unavailable.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "quotes",
            "message": "Quote not found" if hidden else error.message,
        },
        status_code=404 if hidden else 409,
    )


def _quotes(db: Session, subscriber_id: str) -> tuple[dict[str, object], str]:
    if selfserve_service.native_read_enabled(db):
        return (
            selfserve_service.selfserve_quotes.read_for_subscriber(db, subscriber_id),
            quotes_mirror.QuoteReadState.current.value,
        )
    result = quotes_mirror.read_for_subscriber_result(db, subscriber_id)
    return result.payload, result.state.value


@router.get("/quotes", response_class=HTMLResponse)
def customer_quotes(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/quotes", status_code=303
        )
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    quotes, quote_read_state = _quotes(db, subscriber_id)
    context = {
        "request": request,
        "customer": customer,
        "active_page": "quotes",
        "quotes": quotes,
        "quote_read_state": quote_read_state,
    }
    return templates.TemplateResponse("customer/quotes/index.html", context)


@router.get("/quotes/{quote_id}/pay", response_class=HTMLResponse)
def customer_quote_payment(
    request: Request,
    quote_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Render a side-effect-free quotation deposit confirmation page."""

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return _login_redirect(request)
    customer_context = resolve_customer_context(db, customer)
    try:
        payment = quote_deposits.quote_payment_page(
            db,
            quote_deposits.QuotePaymentQuery(
                quote_id=quote_id,
                authorized_subscriber_ids=_authorized_subscriber_ids(customer_context),
                observed_at=datetime.now(UTC),
            ),
        )
    except quote_deposits.QuoteDepositError as exc:
        return _quote_payment_unavailable(request, customer, exc)
    return templates.TemplateResponse(
        "customer/quotes/pay.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "quotes",
            "payment": payment,
        },
    )


@router.post("/quotes/{quote_id}/pay/intent")
def customer_quote_payment_intent(
    request: Request,
    quote_id: UUID,
    payload: QuotePaymentIntentRequest = Body(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Start the canonical quotation deposit flow after explicit confirmation."""

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    customer_context = resolve_customer_context(db, customer)
    if not customer_context.allowed_subscriber_ids:
        return JSONResponse(
            {"detail": "No customer portal identity is available for this Quote"},
            status_code=409,
        )
    try:
        outcome = quote_deposits.initiate_quote_deposit(
            db,
            customer_context,
            quote_deposits.InitiateQuoteDepositCommand(
                quote_id=quote_id,
                idempotency_key=payload.idempotency_key,
                redirect_url=str(
                    request.url_for(
                        "customer_quote_payment_verify", quote_id=str(quote_id)
                    )
                ),
            ),
        )
    except (ValueError, quote_deposits.QuoteDepositError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        return JSONResponse({"detail": message}, status_code=409)
    return JSONResponse(content=jsonable_encoder(outcome.to_response()))


@router.get("/quotes/{quote_id}/pay/verify", response_class=HTMLResponse)
def customer_quote_payment_verify(
    request: Request,
    quote_id: UUID,
    reference: str = Query(..., min_length=1, max_length=120),
    db: Session = Depends(get_db),
) -> Response:
    """Verify Paystack evidence through the established quotation lifecycle."""

    customer = get_current_customer_from_request(request, db)
    if not customer:
        return _login_redirect(request)
    customer_context = resolve_customer_context(db, customer)
    if not customer_context.allowed_subscriber_ids:
        return templates.TemplateResponse(
            "customer/quotes/payment_unavailable.html",
            {
                "request": request,
                "customer": customer,
                "active_page": "quotes",
                "message": "No customer portal identity is available for this Quote",
            },
            status_code=409,
        )
    try:
        outcome = quote_deposits.verify_quote_deposit(
            db,
            customer_context,
            quote_deposits.VerifyQuoteDepositCommand(
                quote_id=quote_id,
                reference=reference,
            ),
        )
    except (ValueError, quote_deposits.QuoteDepositError) as exc:
        if isinstance(exc, quote_deposits.QuoteDepositError):
            return _quote_payment_unavailable(request, customer, exc)
        return templates.TemplateResponse(
            "customer/quotes/payment_unavailable.html",
            {
                "request": request,
                "customer": customer,
                "active_page": "quotes",
                "message": str(exc),
            },
            status_code=409,
        )
    return templates.TemplateResponse(
        "customer/quotes/pay_result.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "quotes",
            "outcome": outcome,
        },
    )
