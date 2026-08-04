"""Token-scoped public Lead intake form adapters."""

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.request_meta import client_ip
from app.schemas.lead_intake import LeadIntakeSubmission, ResolvedLeadIntakeAddress
from app.services import geocoding, lead_intake_ai
from app.services.owner_commands import CommandContext
from app.services.rate_limiter_adapter import allow_operation
from app.services.sales import lead_intake
from app.web.templates import templates

router = APIRouter(prefix="/lead-intake", tags=["web-public-lead-intake"])
_FIELDS = {
    "_csrf_token",
    "full_name",
    "gender",
    "date_of_birth",
    "organization_name",
    "representative_name",
    "representative_role",
    "latitude",
    "longitude",
    "address_confirmation",
    "privacy_acknowledged",
}
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _unavailable(request: Request):
    return templates.TemplateResponse(
        "public/lead_intake/unavailable.html",
        {"request": request},
        status_code=410,
        headers=_PRIVATE_HEADERS,
    )


def _render(request: Request, form, *, values=None, error=None, status_code=200):
    return templates.TemplateResponse(
        "public/lead_intake/form.html",
        {"request": request, "form": form, "values": values or {}, "error": error},
        status_code=status_code,
        headers=_PRIVATE_HEADERS,
    )


def _rate_key(request: Request, token: str, action: str) -> str:
    import hashlib

    peer = client_ip(request) or "unknown"
    digest = hashlib.sha256(
        f"{peer}:{lead_intake.token_hash(token)}".encode()
    ).hexdigest()
    return f"lead-intake:{action}:{digest}"


@router.get("/{token}", response_class=HTMLResponse)
def lead_intake_page(
    request: Request,
    token: str = Path(min_length=32, max_length=128),
    db: Session = Depends(get_db),
):
    try:
        form = lead_intake.get_public_form(db, token)
    except lead_intake.LeadIntakeError:
        return _unavailable(request)
    return _render(request, form)


@router.get("/{token}/address-search")
def lead_intake_address_search(
    request: Request,
    token: str = Path(min_length=32, max_length=128),
    q: str = Query(min_length=3, max_length=160),
    db: Session = Depends(get_db),
):
    try:
        lead_intake.get_public_form(db, token)
    except lead_intake.LeadIntakeError:
        raise HTTPException(
            status_code=410, detail="Lead intake link unavailable"
        ) from None
    decision = allow_operation(
        _rate_key(request, token, "address"), limit=20, window_seconds=60
    )
    if not decision.allowed:
        return JSONResponse(
            {"detail": "Too many address searches. Try again shortly."},
            status_code=429,
            headers={"Retry-After": str(decision.retry_after_seconds or 60)},
        )
    return JSONResponse(
        {
            "items": geocoding.geocode_preview(
                db, {"address_line1": q, "country_code": "NG"}, limit=5
            )
        },
        headers=_PRIVATE_HEADERS,
    )


@router.post("/{token}", response_class=HTMLResponse)
async def lead_intake_submit(
    request: Request,
    token: str = Path(min_length=32, max_length=128),
    db: Session = Depends(get_db),
):
    try:
        public_form = lead_intake.get_public_form(db, token)
    except lead_intake.LeadIntakeError:
        return _unavailable(request)
    rate = allow_operation(
        _rate_key(request, token, "submit"), limit=10, window_seconds=3600
    )
    if not rate.allowed:
        return _render(
            request,
            public_form,
            error="Too many attempts. Try again later.",
            status_code=429,
        )
    raw = await request.form()
    if set(raw.keys()) - _FIELDS:
        return _render(
            request,
            public_form,
            error="The form contained unsupported fields.",
            status_code=400,
        )
    values = {key: str(value) for key, value in raw.items() if key != "_csrf_token"}
    try:
        submission = LeadIntakeSubmission.model_validate(
            {
                **values,
                "address_confirmation": values.get("address_confirmation") == "on",
                "privacy_acknowledged": values.get("privacy_acknowledged") == "on",
            }
        )
        reverse = geocoding.reverse_geocode(
            db, submission.latitude, submission.longitude
        )
        if not reverse:
            raise ValueError("The selected address could not be verified.")
        address = dict(reverse.get("address") or {})
        resolved = ResolvedLeadIntakeAddress(
            display_name=str(reverse.get("display_name") or ""),
            latitude=float(reverse["latitude"]),
            longitude=float(reverse["longitude"]),
            state=str(
                address.get("state")
                or address.get("region")
                or address.get("state_district")
                or ""
            ),
            country_code=str(address.get("country_code") or ""),
        )
        finish_read_transaction(db)
        command_id = uuid4()
        outcome = lead_intake.submit_form(
            db,
            lead_intake.SubmitLeadIntakeCommand(
                context=CommandContext.system(
                    actor="public:lead-intake",
                    scope="sales.lead_intake:submit",
                    reason="customer saved Inbox Lead intake form",
                    command_id=command_id,
                    correlation_id=command_id,
                    idempotency_key=f"lead-intake-submit:{public_form.invitation_id}",
                ),
                token=token,
                submission=submission,
                resolved_address=resolved,
            ),
        )
    except (ValidationError, ValueError, KeyError, HTTPException) as exc:
        return _render(
            request, public_form, values=values, error=str(exc), status_code=400
        )
    except lead_intake.LeadIntakeError as exc:
        if exc.kind == "not_found":
            return _unavailable(request)
        return _render(
            request, public_form, values=values, error=exc.message, status_code=400
        )
    if not outcome.replayed:
        lead_intake_ai.send_completion_confirmation(db, outcome=outcome)
    return templates.TemplateResponse(
        "public/lead_intake/thank_you.html",
        {"request": request, "message": outcome.thank_you_message},
        status_code=201,
        headers=_PRIVATE_HEADERS,
    )
