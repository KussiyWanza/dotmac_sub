"""Customer portal service-location page and geocode helpers."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.models.subscriber import AddressType, Subscriber
from app.schemas.subscriber import CustomerServiceLocationUpdate
from app.services import customer_location_requests as location_service
from app.services import customer_portal_location_commands as location_commands
from app.services import geocoding as geocoding_service
from app.services import location_capture
from app.services import web_customer_actions as customer_profile_service
from app.services.customer_context import optional_customer_subscriber_id
from app.services.owner_commands import CommandContext
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])

logger = logging.getLogger(__name__)
READ_ONLY_MUTATION_MESSAGE = "View-only sessions cannot make changes."


def _page_context(
    request: Request, db: Session, customer: dict, *, biodata_required: bool = False
) -> dict:
    context = location_service.get_customer_location_page_context(db, customer)
    context.update(
        {
            "request": request,
            "customer": customer,
            "active_page": "location",
            "form_error": None,
            "form_note": "",
            "biodata_required": biodata_required,
        }
    )
    return context


@router.get("/location", response_class=HTMLResponse)
def customer_location_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/location", status_code=303
        )
    context = _page_context(
        request,
        db,
        customer,
        biodata_required=request.query_params.get("biodata_required") == "1",
    )
    return templates.TemplateResponse("customer/location/index.html", context)


@router.post("/location", response_class=HTMLResponse)
def customer_location_submit(
    request: Request,
    address_line1: str = Form(...),
    address_line2: str = Form(""),
    city: str = Form(""),
    region: str = Form(""),
    lga: str = Form(""),
    postal_code: str = Form(""),
    country_code: str = Form("NG"),
    latitude: float = Form(...),
    longitude: float = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    if customer.get("read_only"):
        return templates.TemplateResponse(
            "customer/errors/400.html",
            {
                "request": request,
                "customer": customer,
                "message": READ_ONLY_MUTATION_MESSAGE,
                "active_page": "location",
            },
            status_code=403,
        )
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        return RedirectResponse(url="/portal/location?error=1", status_code=303)
    try:
        subscriber_uuid = UUID(subscriber_id)
        finish_read_transaction(db)
        location_commands.update_service_location(
            db,
            location_commands.UpdateServiceLocationCommand(
                context=CommandContext.system(
                    actor=f"user:{subscriber_uuid}",
                    scope=location_commands.PORTAL_LOCATION_WRITE_SCOPE,
                    reason="Customer updated the portal service location",
                ),
                location=CustomerServiceLocationUpdate(
                    subscriber_id=subscriber_uuid,
                    address_line1=address_line1.strip(),
                    address_line2=address_line2.strip() or None,
                    city=city.strip() or None,
                    region=region.strip() or None,
                    lga=lga.strip() or None,
                    postal_code=postal_code.strip() or None,
                    country_code=country_code.strip().upper() or "NG",
                    latitude=latitude,
                    longitude=longitude,
                    address_type=AddressType.service,
                    label="Primary service",
                    is_primary=True,
                    actor_id=subscriber_uuid,
                    actor_name=str(customer.get("username") or "") or None,
                ),
            ),
        )
        subscriber = db.get(Subscriber, subscriber_uuid)
        if subscriber is not None:
            completion = customer_profile_service.evaluate_individual_biodata(
                subscriber
            )
            if (
                location_capture.service_location_requirement_enabled(db)
                and completion.applicable
                and not completion.complete
            ):
                return RedirectResponse(
                    url="/portal/location?saved=1&biodata_required=1", status_code=303
                )
    except Exception as exc:
        detail = (
            getattr(exc, "detail", None) or str(exc) or "Unable to save your location."
        )
        context = _page_context(request, db, customer)
        context["form_error"] = str(detail)
        return templates.TemplateResponse(
            "customer/location/index.html", context, status_code=400
        )
    return RedirectResponse(url="/portal/location?saved=1", status_code=303)


@router.post("/location-requests/{request_id}/cancel")
def customer_location_cancel(
    request: Request,
    request_id: str,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    location_service.cancel_request(
        db,
        request_id=request_id,
        subscriber_id=subscriber_id,
        actor_id=subscriber_id or None,
    )
    return RedirectResponse(url="/portal/location?canceled=1", status_code=303)


@router.get("/location/geocode-search")
def customer_location_geocode_search(
    request: Request,
    q: str = Query(min_length=3, max_length=200),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    results = geocoding_service.geocode_preview(db, {"address_line1": q}, limit=5)
    return JSONResponse(
        [
            {
                "display_name": item.get("display_name"),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
            }
            for item in results
        ]
    )


@router.get("/location/reverse-geocode")
def customer_location_reverse_geocode(
    request: Request,
    lat: float = Query(),
    lon: float = Query(),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    result = geocoding_service.reverse_geocode(db, lat, lon)
    if not result:
        return JSONResponse({"display_name": None})
    return JSONResponse(
        {
            "display_name": result.get("display_name"),
            "latitude": result.get("latitude"),
            "longitude": result.get("longitude"),
        }
    )


# ── NCC location confirm-or-correct prompt ───────────────────────────────────
#
# Distinct from the "move my service address" flow above (which opens a
# staff-reviewed change request). This is the regulatory capture prompt: the
# customer confirms — or corrects — the location we hold, and the reconciled
# result lands in the verification ledger via ``location_capture``. Gated by
# ``loyalty.capture_prompt`` (default off).


@router.post("/location/confirm")
def customer_location_confirm(
    request: Request,
    latitude: float = Form(...),
    longitude: float = Form(...),
    accuracy_m: float | None = Form(default=None),
    claimed_state: str | None = Form(default=None),
    claimed_lga: str | None = Form(default=None),
    claimed_postcode: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    if not subscriber_id:
        return RedirectResponse(url="/portal/location?error=1", status_code=303)
    try:
        subscriber_uuid = UUID(subscriber_id)
        finish_read_transaction(db)
        location_commands.confirm_location(
            db,
            location_commands.ConfirmLocationCommand(
                context=CommandContext.system(
                    actor=f"user:{subscriber_uuid}",
                    scope=location_commands.PORTAL_LOCATION_WRITE_SCOPE,
                    reason="Customer confirmed the portal service location",
                ),
                subscriber_id=subscriber_uuid,
                latitude=latitude,
                longitude=longitude,
                accuracy_m=accuracy_m,
                actor_name=str(customer.get("username") or "") or None,
                claimed_state=claimed_state,
                claimed_lga=claimed_lga,
                claimed_postcode=claimed_postcode,
            ),
        )
    except location_commands.PortalLocationCommandError as exc:
        if exc.code == "customer.portal_location_commands.capture_disabled":
            return RedirectResponse(url="/portal/location?disabled=1", status_code=303)
        raise
    return RedirectResponse(url="/portal/location?confirmed=1", status_code=303)


@router.post("/location/snooze")
def customer_location_snooze(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    if subscriber_id:
        try:
            subscriber_uuid = UUID(subscriber_id)
            finish_read_transaction(db)
            location_commands.snooze_location_prompt(
                db,
                location_commands.SnoozeLocationPromptCommand(
                    context=CommandContext.system(
                        actor=f"user:{subscriber_uuid}",
                        scope=location_commands.PORTAL_LOCATION_WRITE_SCOPE,
                        reason="Customer snoozed the portal location prompt",
                    ),
                    subscriber_id=subscriber_uuid,
                ),
            )
        except location_commands.PortalLocationCommandError as exc:
            if exc.code == "customer.portal_location_commands.capture_disabled":
                return RedirectResponse(
                    url="/portal/location?disabled=1", status_code=303
                )
            raise
    return RedirectResponse(url="/portal", status_code=303)


def location_prompt_context(
    db: Session, customer: dict, *, ignore_snooze: bool = False
) -> dict | None:
    """Prompt payload for a portal page, or None when it should not show.

    ``ignore_snooze=True`` at payment — a snooze pauses browsing nags, not the
    one flow where we have the customer's attention.
    """
    subscriber_id = optional_customer_subscriber_id(db, customer)
    if not subscriber_id:
        return None
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        return None
    return location_capture.prompt_context(db, subscriber, ignore_snooze=ignore_snooze)
