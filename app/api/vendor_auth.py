"""JSON authentication API for vendor (contractor) technicians in the field app.

**Adapter only.** Nothing here decides anything. Two owners are composed:

* ``app.services.auth_flow.AuthFlow`` owns credential verification, MFA
  verification, access-token minting, refresh rotation with reuse detection,
  and session revocation. It is the same owner behind ``/api/v1/auth/*``.
* ``app.services.field.vendor_auth`` owns vendor admission — the pre-credential
  eligibility decision (``resolve_vendor_login_eligibility``) and the
  post-issue check that a minted token belongs to an active member of an active
  vendor (``resolve_vendor_admission_for_access_token``).

This module deliberately does NOT reuse ``app/web/vendor_auth.py``. That router
is the BROWSER adapter: it renders Jinja templates, sets session cookies and
answers with 303 redirects. Aliasing a JSON path onto an HTML form handler
would make one handler serve two transports and one of them badly. The HTML
adapter stays exactly as it is; this is the second transport over the same
owners.

The paths, request bodies and response bodies match what the shipped field app
already sends and parses — see ``tests/test_vendor_auth_api_contract.py``,
which pins them against ``field_mobile``'s Dart client. They were missing
entirely until now: the field app posted to ``/api/v1/vendor/auth/login`` while
Sub mounted only the unprefixed HTML ``/vendor/auth/login``, so vendor login,
MFA and refresh all answered 404 and vendor technicians could not sign in.
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.auth_flow import ErrorResponse
from app.schemas.vendor_auth import (
    VendorLoginRequest,
    VendorLoginResponse,
    VendorLogoutRequest,
    VendorLogoutResponse,
    VendorMfaVerifyRequest,
    VendorRefreshRequest,
    VendorTokenResponse,
)
from app.services import auth_flow as auth_flow_service
from app.services.auth_flow import AuthFlow, wants_refresh_in_body
from app.services.field import vendor_auth as vendor_admission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vendor/auth", tags=["vendor-auth"])

#: A vendor operator whose account is forced into MFA enrolment cannot enrol
#: from the field app — enrolment is a browser flow. Say so instead of
#: answering with a token-less 200 the client would report as
#: "Unexpected server response".
MFA_ENROLMENT_MESSAGE = (
    "Multi-factor enrolment is required. Sign in at /vendor/auth/login on the "
    "vendor portal to enrol an authenticator, then sign in here again."
)


def _issued_token(result: dict, key: str) -> str:
    """Read a token the auth owner promised to issue.

    A missing token is an owner-contract violation, not a client error: fail
    loudly with a 503 rather than returning a 200 the client cannot use.
    """

    value = result.get(key)
    if not isinstance(value, str) or not value:
        logger.error("auth owner returned no %s for a vendor authentication", key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable. Please try again.",
        )
    return value


def _discard_refused_session(db: Session, refresh_token: str | None) -> None:
    """Revoke the session minted for a principal we then refused to admit.

    The auth owner mints tokens before this adapter knows whether the principal
    is a vendor operator, so a refusal must not leave a live session behind that
    nobody ever received. Revocation is the owner's, through its own command.
    """

    if not refresh_token:
        return
    try:
        auth_flow_service.auth_flow.logout(db=db, refresh_token=refresh_token)
    except HTTPException:
        logger.warning(
            "could not revoke the session minted for a refused vendor login",
            exc_info=True,
        )


def _vendor_id_or_refuse(db: Session, result: dict) -> UUID:
    """Admit the freshly minted token, or refuse and discard its session."""

    access_token = _issued_token(result, "access_token")
    refresh_token = result.get("refresh_token")
    try:
        admission = vendor_admission.resolve_vendor_admission_for_access_token(
            db, access_token=access_token
        )
    except vendor_admission.VendorAdmissionRequiredError as exc:
        _discard_refused_session(
            db, refresh_token if isinstance(refresh_token, str) else None
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=exc.message
        ) from exc
    return admission.vendor_id


def _token_transport(
    db: Session,
    request: Request,
    payload: BaseModel,
    refresh_token: str | None,
) -> Response | BaseModel:
    """Deliver the refresh token the way the auth owner's policy says to.

    Native clients set ``X-Auth-Refresh-In-Body`` (they cannot read an httpOnly
    cookie) and get it in the body; anything else keeps the safer cookie. The
    policy itself belongs to ``auth_flow`` — this only applies it, with the
    owner's own cookie settings.
    """

    if refresh_token and not wants_refresh_in_body(request):
        settings = AuthFlow.refresh_cookie_settings(db)
        response = Response(
            content=payload.model_dump_json(),
            status_code=status.HTTP_200_OK,
            media_type="application/json",
        )
        response.set_cookie(
            key=settings["key"],
            value=refresh_token,
            httponly=settings["httponly"],
            secure=settings["secure"],
            samesite=settings["samesite"],
            domain=settings["domain"],
            path=settings["path"],
            max_age=settings["max_age"],
        )
        return response
    return payload


@router.post(
    "/login",
    response_model=VendorLoginResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid credentials"},
        403: {"model": ErrorResponse, "description": "Vendor access required"},
        429: {"model": ErrorResponse, "description": "Too many login attempts"},
    },
)
def vendor_login(
    payload: VendorLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response | VendorLoginResponse:
    """Authenticate a vendor technician. Pre-auth by definition: this route IS
    the authentication, so it carries no ``require_*`` guard — it is covered by
    the ``/api/v1/vendor`` self-scoped entry in ``test_route_permission_guards``
    and mounted with dependency mode "none" in ``app/main.py``, exactly like
    ``POST /api/v1/auth/login``. Authorization is the vendor-admission decision
    below, applied twice: before credentials are verified and again on the
    minted token."""

    eligibility = vendor_admission.resolve_vendor_login_eligibility(
        db,
        vendor_admission.VendorLoginEligibilityQuery(identifier=payload.username),
    )
    if (
        eligibility.status
        is vendor_admission.VendorLoginEligibilityStatus.VENDOR_ACCESS_REQUIRED
    ):
        # A known identity with no vendor membership is refused BEFORE the
        # password is checked, so this endpoint never becomes a credential
        # oracle for the staff surface. An unknown identifier deliberately
        # falls through to the shared 401.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=vendor_admission.VENDOR_ACCESS_MESSAGE,
        )

    result = auth_flow_service.auth_flow.login(
        db=db,
        username=payload.username,
        password=payload.password,
        request=request,
        provider=None,
    )
    if result.get("mfa_required") is True:
        return VendorLoginResponse(
            mfa_required=True,
            mfa_token=_issued_token(result, "mfa_token"),
        )
    if result.get("mfa_enrollment_required") is True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=MFA_ENROLMENT_MESSAGE
        )

    vendor_id = _vendor_id_or_refuse(db, result)
    refresh_token = result.get("refresh_token")
    refresh_token = refresh_token if isinstance(refresh_token, str) else None
    body = VendorLoginResponse(
        access_token=_issued_token(result, "access_token"),
        refresh_token=refresh_token if wants_refresh_in_body(request) else None,
        vendor_id=vendor_id,
    )
    return cast(
        "Response | VendorLoginResponse",
        _token_transport(db, request, body, refresh_token),
    )


@router.post(
    "/mfa",
    response_model=VendorTokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid verification code"},
        403: {"model": ErrorResponse, "description": "Vendor access required"},
        404: {"model": ErrorResponse, "description": "MFA method not found"},
        429: {"model": ErrorResponse, "description": "Too many incorrect codes"},
    },
)
def vendor_mfa_verify(
    payload: VendorMfaVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response | VendorTokenResponse:
    """Complete an MFA challenge started by ``POST /vendor/auth/login``.
    Pre-auth for the same reason as login: the ``mfa_token`` issued by that call
    is the credential being presented."""

    result = auth_flow_service.auth_flow.mfa_verify(
        db=db,
        mfa_token=payload.mfa_token,
        code=payload.code,
        request=request,
    )
    vendor_id = _vendor_id_or_refuse(db, result)
    refresh_token = result.get("refresh_token")
    refresh_token = refresh_token if isinstance(refresh_token, str) else None
    body = VendorTokenResponse(
        access_token=_issued_token(result, "access_token"),
        refresh_token=refresh_token if wants_refresh_in_body(request) else None,
        vendor_id=vendor_id,
    )
    return cast(
        "Response | VendorTokenResponse",
        _token_transport(db, request, body, refresh_token),
    )


@router.post(
    "/refresh",
    response_model=VendorTokenResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid refresh token"},
        403: {"model": ErrorResponse, "description": "Vendor access required"},
    },
)
def vendor_refresh(
    payload: VendorRefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response | VendorTokenResponse:
    """Rotate a vendor session's tokens. Pre-auth: the refresh token in the body
    (or the httpOnly cookie) is the credential. Admission is re-checked on every
    rotation, so a technician whose vendor membership is deactivated stops
    receiving vendor-scoped tokens at the next refresh rather than at session
    expiry."""

    resolved = AuthFlow.resolve_refresh_token(
        request=request, refresh_token=payload.refresh_token, db=None
    )
    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token"
        )
    result = auth_flow_service.auth_flow.refresh(
        db=db, refresh_token=resolved, request=request
    )
    result_values = result.model_dump()
    vendor_id = _vendor_id_or_refuse(db, result_values)
    refresh_token = result.refresh_token
    body = VendorTokenResponse(
        access_token=result.access_token,
        refresh_token=refresh_token if wants_refresh_in_body(request) else None,
        vendor_id=vendor_id,
    )
    return cast(
        "Response | VendorTokenResponse",
        _token_transport(db, request, body, refresh_token),
    )


@router.post(
    "/logout",
    response_model=VendorLogoutResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"model": ErrorResponse, "description": "Session not found"},
    },
)
def vendor_logout(
    payload: VendorLogoutRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Revoke the caller's own session. Pre-auth: holding the refresh token is
    the proof, and there is nothing to admit — revoking a session you already
    hold the token for needs no vendor context. Delegated whole to the auth
    owner, which also clears the refresh cookie."""

    return cast(
        Response,
        auth_flow_service.auth_flow.logout_response(
            db=db, refresh_token=payload.refresh_token, request=request
        ),
    )
