"""Vendor-branded HTTP adapter for the shared authentication owners."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlencode, urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import auth_flow as auth_flow_service
from app.services import credential_recovery
from app.services.auth_flow import AuthFlow, decode_access_token
from app.services.field.vendor_auth import (
    VendorLoginEligibilityQuery,
    VendorLoginEligibilityStatus,
    resolve_vendor_login_eligibility,
    vendor_context,
)
from app.services.owner_commands import CommandContext
from app.services.web_auth import (
    MFA_ENROLLMENT_COOKIE,
    PASSWORD_RESET_COOKIE,
    PASSWORD_RESET_COOKIE_TTL,
)
from app.web.auth.dependencies import AuthenticationRequired, validate_session_token
from app.web.portal_branding import auth_branding_context

logger = logging.getLogger(__name__)
templates = Jinja2Templates(
    directory="templates", context_processors=[auth_branding_context]
)

VENDOR_ACCESS_MESSAGE = "This account is not enabled for Vendor Operations."
_VENDOR_DEFAULT_PATH = "/vendor"
_VENDOR_LOGIN_PATH = "/vendor/auth/login"
_VENDOR_RESET_LOGIN_PATH = "/vendor/auth/login?next=/vendor"
_MFA_COOKIE = "vendor_mfa_pending"
_REMEMBER_COOKIE = "vendor_remember"


@dataclass(frozen=True, slots=True)
class _CookiePolicy:
    refresh_key: str
    httponly: bool
    secure: bool
    samesite: Literal["lax", "strict", "none"]
    domain: str | None
    path: str
    max_age: int


@dataclass(frozen=True, slots=True)
class _IssuedTokens:
    access_token: str
    refresh_token: str | None


def _cookie_policy(db: Session, request: Request) -> _CookiePolicy:
    raw = AuthFlow.refresh_cookie_settings(db)
    raw_samesite = str(raw["samesite"]).lower()
    if raw_samesite == "strict":
        samesite: Literal["lax", "strict", "none"] = "strict"
    elif raw_samesite == "none":
        samesite = "none"
    else:
        samesite = "lax"
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    request_is_https = (
        forwarded_proto.split(",")[0].strip().lower() == "https"
        if forwarded_proto
        else request.url.scheme == "https"
    )
    return _CookiePolicy(
        refresh_key=str(raw["key"]),
        httponly=bool(raw["httponly"]),
        secure=bool(raw["secure"]) and request_is_https,
        samesite=samesite,
        domain=str(raw["domain"]) if raw["domain"] else None,
        path=str(raw["path"]),
        max_age=int(raw["max_age"]),
    )


def _safe_vendor_next(next_url: str | None) -> str:
    candidate = (next_url or "").strip()
    if candidate == "/vendor" or candidate.startswith("/vendor/"):
        if not candidate.startswith("//") and not candidate.startswith("/\\"):
            return candidate
    return _VENDOR_DEFAULT_PATH


def _request_next_url(request: Request) -> str:
    next_url = str(request.url.path)
    if request.url.query:
        next_url += f"?{request.url.query}"
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return _safe_vendor_next(next_url)
    referer = str(request.headers.get("referer") or "").strip()
    if not referer:
        return _safe_vendor_next(next_url)
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return _safe_vendor_next(next_url)
    if parsed.scheme and parsed.scheme != request.url.scheme:
        return _safe_vendor_next(next_url)
    referer_path = str(parsed.path or "")
    if parsed.query:
        referer_path += f"?{parsed.query}"
    return _safe_vendor_next(referer_path)


def _vendor_login_url(next_url: str | None = None, error: str | None = None) -> str:
    query: dict[str, str] = {}
    if next_url:
        query["next"] = _safe_vendor_next(next_url)
    if error:
        query["error"] = error
    return f"{_VENDOR_LOGIN_PATH}?{urlencode(query)}" if query else _VENDOR_LOGIN_PATH


def _csrf_token(request: Request) -> str:
    return str(getattr(request.state, "csrf_token", ""))


def _recovery_context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="service:vendor-auth-web",
        scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
        reason=reason,
    )


def _issued_tokens(result: Mapping[str, object]) -> _IssuedTokens:
    access_token = result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Authentication service did not issue an access token")
    refresh_token = result.get("refresh_token")
    return _IssuedTokens(
        access_token=access_token,
        refresh_token=refresh_token
        if isinstance(refresh_token, str) and refresh_token
        else None,
    )


def _require_access_token_vendor(db: Session, access_token: str) -> dict[str, object]:
    payload = decode_access_token(db, access_token)
    principal_id = str(payload.get("principal_id") or payload.get("sub") or "")
    principal_type = str(payload.get("principal_type") or "")
    if not principal_id or principal_type != "system_user":
        raise HTTPException(status_code=403, detail=VENDOR_ACCESS_MESSAGE)
    return vendor_context(
        db,
        {
            "principal_id": principal_id,
            "person_id": principal_id,
            "principal_type": principal_type,
            "session_id": str(payload.get("session_id") or ""),
            "roles": [],
            "scopes": [],
        },
    )


def _set_login_cookies(
    response: Response,
    db: Session,
    request: Request,
    tokens: _IssuedTokens,
    *,
    remember: bool,
) -> None:
    policy = _cookie_policy(db, request)
    response.set_cookie(
        key="session_token",
        value=auth_flow_service.issue_web_session_token(db, tokens.access_token),
        httponly=True,
        secure=policy.secure,
        samesite=policy.samesite,
    )
    if tokens.refresh_token:
        response.set_cookie(
            key=policy.refresh_key,
            value=tokens.refresh_token,
            httponly=policy.httponly,
            secure=policy.secure,
            samesite=policy.samesite,
            domain=policy.domain,
            path=policy.path,
            max_age=policy.max_age if remember else None,
        )


def _set_remember_cookie(
    response: Response, db: Session, request: Request, remember: bool
) -> None:
    policy = _cookie_policy(db, request)
    if remember:
        response.set_cookie(
            key=_REMEMBER_COOKIE,
            value="1",
            httponly=True,
            secure=policy.secure,
            samesite=policy.samesite,
            max_age=policy.max_age,
        )
    else:
        response.delete_cookie(_REMEMBER_COOKIE)


def _template_context(
    request: Request,
    *,
    error: str | None = None,
    next_url: str | None = None,
    success: bool = False,
    db: Session | None = None,
) -> dict[str, object]:
    duration = "30 days"
    if db is not None:
        duration = auth_flow_service.duration_label(_cookie_policy(db, request).max_age)
    return {
        "request": request,
        "error": error,
        "next": _safe_vendor_next(next_url),
        "success": success,
        "remember_duration_label": duration,
        "csrf_token": _csrf_token(request),
    }


def vendor_login_page(
    request: Request,
    db: Session,
    error: str | None = None,
    next_url: str | None = None,
) -> Response:
    auth = validate_session_token(request, db)
    if auth:
        try:
            vendor_context(db, auth)
        except HTTPException:
            pass
        else:
            return RedirectResponse(url=_safe_vendor_next(next_url), status_code=303)
    return templates.TemplateResponse(
        request,
        "vendor/auth/login.html",
        _template_context(request, error=error, next_url=next_url, db=db),
    )


def _login_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = detail.get("message")
            return str(message) if message else "Invalid credentials"
        if isinstance(detail, str) and detail:
            return detail
    return "Invalid credentials"


def vendor_login_submit(
    request: Request,
    db: Session,
    username: str,
    password: str,
    remember: bool,
    next_url: str,
) -> Response:
    target = _safe_vendor_next(next_url)
    eligibility = resolve_vendor_login_eligibility(
        db, VendorLoginEligibilityQuery(identifier=username)
    )
    if eligibility.status is VendorLoginEligibilityStatus.VENDOR_ACCESS_REQUIRED:
        return templates.TemplateResponse(
            request,
            "vendor/auth/login.html",
            _template_context(
                request, error=VENDOR_ACCESS_MESSAGE, next_url=target, db=db
            ),
            status_code=403,
        )
    try:
        raw_result = auth_flow_service.auth_flow.login(
            db=db,
            username=username,
            password=password,
            request=request,
            provider=None,
        )
        result: Mapping[str, object] = raw_result
        if result.get("mfa_required") is True:
            mfa_token = result.get("mfa_token")
            if not isinstance(mfa_token, str) or not mfa_token:
                raise RuntimeError("Authentication service did not issue an MFA token")
            response = RedirectResponse(
                url=f"/vendor/auth/mfa?next={quote(target, safe='')}",
                status_code=303,
            )
            policy = _cookie_policy(db, request)
            response.set_cookie(
                key=_MFA_COOKIE,
                value=mfa_token,
                httponly=True,
                secure=policy.secure,
                samesite=policy.samesite,
                max_age=300,
            )
            _set_remember_cookie(response, db, request, remember)
            return response
        if result.get("mfa_enrollment_required") is True:
            enrollment_token = result.get("mfa_enrollment_token")
            if not isinstance(enrollment_token, str) or not enrollment_token:
                raise RuntimeError(
                    "Authentication service did not issue an MFA enrollment token"
                )
            response = RedirectResponse(
                url=f"/auth/mfa/enroll?next={quote(target, safe='')}",
                status_code=303,
            )
            policy = _cookie_policy(db, request)
            response.set_cookie(
                key=MFA_ENROLLMENT_COOKIE,
                value=enrollment_token,
                httponly=True,
                secure=policy.secure,
                samesite=policy.samesite,
                max_age=300,
            )
            _set_remember_cookie(response, db, request, remember)
            return response
        tokens = _issued_tokens(result)
        _require_access_token_vendor(db, tokens.access_token)
        response = RedirectResponse(url=target, status_code=303)
        _set_login_cookies(response, db, request, tokens, remember=remember)
        _set_remember_cookie(response, db, request, remember)
        return response
    except Exception as exc:
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            if exc.detail.get("code") == "PASSWORD_RESET_REQUIRED":
                reset = credential_recovery.issue_reset_capability_for_email(
                    db, username, ttl_minutes=15
                )
                if reset and reset.token:
                    response = RedirectResponse(
                        url=(
                            "/auth/reset-password?"
                            + urlencode({"next_login": _VENDOR_RESET_LOGIN_PATH})
                        ),
                        status_code=303,
                    )
                    policy = _cookie_policy(db, request)
                    response.set_cookie(
                        key=PASSWORD_RESET_COOKIE,
                        value=reset.token,
                        max_age=PASSWORD_RESET_COOKIE_TTL,
                        httponly=True,
                        secure=policy.secure,
                        samesite=policy.samesite,
                        path="/auth/reset-password",
                    )
                    return response
        status_code = 503 if isinstance(exc, RuntimeError) else 401
        message = (
            "Session service unavailable. Please try again."
            if isinstance(exc, RuntimeError)
            else _login_error(exc)
        )
        return templates.TemplateResponse(
            request,
            "vendor/auth/login.html",
            _template_context(request, error=message, next_url=target, db=db),
            status_code=status_code,
        )


def vendor_mfa_page(
    request: Request, error: str | None = None, next_url: str | None = None
) -> Response:
    if not request.cookies.get(_MFA_COOKIE):
        return RedirectResponse(url=_vendor_login_url(next_url), status_code=303)
    return templates.TemplateResponse(
        request,
        "vendor/auth/mfa.html",
        _template_context(request, error=error, next_url=next_url),
    )


def vendor_mfa_submit(
    request: Request, db: Session, code: str, next_url: str
) -> Response:
    mfa_token = request.cookies.get(_MFA_COOKIE)
    target = _safe_vendor_next(next_url)
    if not mfa_token:
        return RedirectResponse(url=_vendor_login_url(target), status_code=303)
    try:
        raw_result = auth_flow_service.auth_flow.mfa_verify(
            db=db, mfa_token=mfa_token, code=code, request=request
        )
        result: Mapping[str, object] = raw_result
        tokens = _issued_tokens(result)
        _require_access_token_vendor(db, tokens.access_token)
        remember = request.cookies.get(_REMEMBER_COOKIE) == "1"
        response = RedirectResponse(url=target, status_code=303)
        response.delete_cookie(_MFA_COOKIE)
        _set_login_cookies(response, db, request, tokens, remember=remember)
        return response
    except Exception as exc:
        status_code = (
            429 if isinstance(exc, HTTPException) and exc.status_code == 429 else 401
        )
        message = (
            str(exc.detail)
            if isinstance(exc, HTTPException)
            and exc.status_code == 429
            and isinstance(exc.detail, str)
            else "Invalid verification code"
        )
        return templates.TemplateResponse(
            request,
            "vendor/auth/mfa.html",
            _template_context(request, error=message, next_url=target),
            status_code=status_code,
        )


def vendor_forgot_password_page(request: Request, success: bool = False) -> Response:
    return templates.TemplateResponse(
        request,
        "vendor/auth/forgot-password.html",
        _template_context(request, success=success),
    )


def vendor_forgot_password_submit(
    request: Request, db: Session, email: str
) -> Response:
    try:
        credential_recovery.request_password_recovery(
            db,
            credential_recovery.RequestPasswordRecoveryCommand(
                context=_recovery_context("Vendor portal password recovery request"),
                email=email,
                next_login_path=_VENDOR_RESET_LOGIN_PATH,
            ),
        )
    except Exception:
        logger.info("Vendor password recovery request was not issued", exc_info=True)
    return templates.TemplateResponse(
        request,
        "vendor/auth/forgot-password.html",
        _template_context(request, success=True),
    )


def vendor_refresh(
    request: Request, db: Session, next_url: str | None = None
) -> Response:
    target = _safe_vendor_next(next_url)
    refresh_token = AuthFlow.resolve_refresh_token(request, None, db)
    if not refresh_token:
        return RedirectResponse(url=_vendor_login_url(target), status_code=303)
    try:
        raw_result = auth_flow_service.auth_flow.refresh(db, refresh_token, request)
        result: Mapping[str, object] = raw_result
        tokens = _issued_tokens(result)
        _require_access_token_vendor(db, tokens.access_token)
    except Exception as exc:
        error = (
            VENDOR_ACCESS_MESSAGE
            if isinstance(exc, HTTPException) and exc.status_code == 403
            else None
        )
        return RedirectResponse(
            url=_vendor_login_url(target, error=error), status_code=303
        )
    remember = request.cookies.get(_REMEMBER_COOKIE) == "1"
    response = RedirectResponse(url=target, status_code=303)
    _set_login_cookies(response, db, request, tokens, remember=remember)
    return response


def require_vendor_web_auth(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    existing = getattr(request.state, "auth", None)
    auth = (
        existing if isinstance(existing, dict) else validate_session_token(request, db)
    )
    if not auth or not auth.get("principal_id"):
        next_url = _request_next_url(request)
        raise AuthenticationRequired(
            f"/vendor/auth/refresh?next={quote(next_url, safe='')}"
        )
    return vendor_context(db, auth)
