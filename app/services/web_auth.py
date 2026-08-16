"""Service helpers for web auth routes."""

import logging
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.services import auth_flow as auth_flow_service
from app.services import credential_recovery, staff_party_authentication
from app.services.auth_flow import AuthFlow
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.web.portal_branding import auth_branding_context

logger = logging.getLogger(__name__)

templates = Jinja2Templates(
    directory="templates", context_processors=[auth_branding_context]
)
MFA_ENROLLMENT_COOKIE = "mfa_enrollment_pending"
# Marker so refresh/MFA hops know whether the login asked to be remembered;
# without it every rotated refresh cookie silently became persistent.
REMEMBER_COOKIE = "admin_remember"
# Carries a forced-reset token from the login POST to the reset page WITHOUT
# putting it in the redirect URL (which lands in browser history / access
# logs). HttpOnly, short-lived, cleared once the reset succeeds.
PASSWORD_RESET_COOKIE = "pwd_reset_token"
PASSWORD_RESET_COOKIE_TTL = 15 * 60


def _credential_recovery_context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="service:public-auth-web",
        scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
        reason=reason,
    )


def _session_cookie_settings(db: Session) -> dict:
    """Derive session_token cookie settings from refresh cookie config."""
    refresh = AuthFlow.refresh_cookie_settings(db)
    return {
        "secure": refresh["secure"],
        "samesite": refresh["samesite"],
    }


def _is_https_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        return forwarded_proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def _safe_next(next_url: str | None, fallback: str = "/admin/dashboard") -> str:
    if (
        next_url
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and not next_url.startswith("/\\")
    ):
        return next_url
    return fallback


def _wants_persistent_session(request: Request) -> bool:
    return request.cookies.get(REMEMBER_COOKIE) == "1"


def _set_remember_cookie(response, db: Session, request: Request, remember: bool):
    settings = AuthFlow.refresh_cookie_settings(db)
    secure_cookie = bool(settings["secure"]) and _is_https_request(request)
    if remember:
        response.set_cookie(
            key=REMEMBER_COOKIE,
            value="1",
            httponly=True,
            secure=secure_cookie,
            samesite=settings["samesite"],
            max_age=settings["max_age"],
        )
    else:
        response.delete_cookie(REMEMBER_COOKIE)


def _set_refresh_cookie(
    response,
    db: Session,
    refresh_token: str,
    request: Request | None = None,
    persistent: bool = True,
):
    settings = AuthFlow.refresh_cookie_settings(db)
    secure_cookie = settings["secure"]
    if request is not None:
        secure_cookie = bool(settings["secure"]) and _is_https_request(request)
    response.set_cookie(
        key=settings["key"],
        value=refresh_token,
        httponly=settings["httponly"],
        secure=secure_cookie,
        samesite=settings["samesite"],
        domain=settings["domain"],
        path=settings["path"],
        # Session-scoped unless the user asked to be remembered.
        max_age=settings["max_age"] if persistent else None,
    )


def _get_csrf_token(request: Request) -> str:
    """Get CSRF token from request state, set by CSRF middleware."""
    return getattr(request.state, "csrf_token", "")


def _remember_duration_label(db: Session | None) -> str:
    settings = AuthFlow.refresh_cookie_settings(db)
    return auth_flow_service.duration_label(int(settings["max_age"]))


def login_page(
    request: Request,
    error: str | None = None,
    next_url: str | None = None,
    db: Session | None = None,
):
    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "error": error,
            "next": next_url or "",
            "remember_duration_label": _remember_duration_label(db),
            "csrf_token": _get_csrf_token(request),
        },
    )


def login_submit(
    request: Request,
    db: Session,
    username: str,
    password: str,
    remember: bool,
    next_url: str,
):
    redirect_url = _safe_next(next_url)
    try:
        result = auth_flow_service.auth_flow.login(
            db=db,
            username=username,
            password=password,
            request=request,
            provider=None,
        )
        if result.get("mfa_required"):
            mfa_url = f"/auth/mfa?next={next_url}" if next_url else "/auth/mfa"
            response = RedirectResponse(url=mfa_url, status_code=303)
            response.set_cookie(
                key="mfa_pending",
                value=result.get("mfa_token", ""),
                httponly=True,
                secure=_is_https_request(request),
                samesite="lax",
                max_age=300,
            )
            _set_remember_cookie(response, db, request, remember)
            return response
        if result.get("mfa_enrollment_required"):
            enroll_url = f"/auth/mfa/enroll?next={quote(redirect_url)}"
            response = RedirectResponse(url=enroll_url, status_code=303)
            response.set_cookie(
                key=MFA_ENROLLMENT_COOKIE,
                value=result.get("mfa_enrollment_token", ""),
                httponly=True,
                secure=_is_https_request(request),
                samesite="lax",
                max_age=300,
            )
            _set_remember_cookie(response, db, request, remember)
            return response

        response = RedirectResponse(url=redirect_url, status_code=303)
        cookie_cfg = _session_cookie_settings(db)
        secure_cookie = cookie_cfg["secure"] and _is_https_request(request)
        session_token = auth_flow_service.issue_web_session_token(
            db,
            str(result.get("access_token", "")),
        )
        # The session cookie is always session-scoped: persistence across
        # browser restarts comes from the refresh cookie (require_web_auth
        # bounces through /auth/refresh when this cookie is gone).
        response.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            secure=secure_cookie,
            samesite=cookie_cfg["samesite"],
        )
        refresh_token = result.get("refresh_token")
        if refresh_token:
            _set_refresh_cookie(
                response, db, refresh_token, request, persistent=remember
            )
        _set_remember_cookie(response, db, request, remember)
        return response
    except Exception as exc:
        error_msg = "Invalid credentials"
        if hasattr(exc, "detail"):
            detail = exc.detail
            if (
                isinstance(detail, dict)
                and detail.get("code") == "PASSWORD_RESET_REQUIRED"
            ):
                reset = credential_recovery.issue_reset_capability_for_email(
                    db,
                    username,
                    ttl_minutes=15,
                )
                if reset and reset.token:
                    # Hand the token to the reset page via a short-lived
                    # HttpOnly cookie instead of the URL, so it never lands in
                    # browser history or access logs.
                    redirect = RedirectResponse(
                        url="/auth/reset-password", status_code=303
                    )
                    cookie_cfg = _session_cookie_settings(db)
                    redirect.set_cookie(
                        key=PASSWORD_RESET_COOKIE,
                        value=reset.token,
                        max_age=PASSWORD_RESET_COOKIE_TTL,
                        httponly=True,
                        secure=cookie_cfg["secure"],
                        samesite=cookie_cfg["samesite"],
                        path="/auth/reset-password",
                    )
                    return redirect
                error_msg = (
                    "Password reset required. Use the forgot password "
                    "page to set a new password."
                )
            elif isinstance(detail, dict):
                error_msg = str(detail.get("message") or error_msg)
            else:
                error_msg = detail if isinstance(detail, str) else str(detail)
        elif str(exc):
            error_msg = str(exc)
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": error_msg,
                "next": next_url,
                "remember_duration_label": _remember_duration_label(db),
                "csrf_token": _get_csrf_token(request),
            },
            status_code=401,
        )


def mfa_page(request: Request, next_url: str | None = None, error: str | None = None):
    mfa_pending = request.cookies.get("mfa_pending")
    if not mfa_pending:
        return RedirectResponse(url="/auth/login", status_code=303)
    return templates.TemplateResponse(
        "auth/mfa.html",
        {
            "request": request,
            "error": error,
            "next": next_url or "",
            "csrf_token": _get_csrf_token(request),
        },
    )


def mfa_submit(
    request: Request,
    db: Session,
    code: str,
    next_url: str,
):
    mfa_token = request.cookies.get("mfa_pending")
    if not mfa_token:
        return RedirectResponse(url="/auth/login", status_code=303)
    redirect_url = _safe_next(next_url)
    try:
        result = auth_flow_service.auth_flow.mfa_verify(
            db=db,
            mfa_token=mfa_token,
            code=code,
            request=request,
        )
        response = RedirectResponse(url=redirect_url, status_code=303)
        response.delete_cookie("mfa_pending")
        cookie_cfg = _session_cookie_settings(db)
        secure_cookie = cookie_cfg["secure"] and _is_https_request(request)
        session_token = auth_flow_service.issue_web_session_token(
            db,
            str(result.get("access_token", "")),
        )
        response.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            secure=secure_cookie,
            samesite=cookie_cfg["samesite"],
        )
        refresh_token = result.get("refresh_token")
        if refresh_token:
            _set_refresh_cookie(
                response,
                db,
                refresh_token,
                request,
                persistent=_wants_persistent_session(request),
            )
        return response
    except HTTPException as exc:
        error_msg = "Invalid verification code"
        if exc.status_code == 429 and isinstance(exc.detail, str):
            error_msg = exc.detail
        return templates.TemplateResponse(
            "auth/mfa.html",
            {
                "request": request,
                "error": error_msg,
                "next": next_url,
                "csrf_token": _get_csrf_token(request),
            },
            status_code=exc.status_code if exc.status_code == 429 else 401,
        )
    except Exception:
        return templates.TemplateResponse(
            "auth/mfa.html",
            {
                "request": request,
                "error": "Invalid verification code",
                "next": next_url,
                "csrf_token": _get_csrf_token(request),
            },
            status_code=401,
        )


def _mfa_enrollment_binding(
    db: Session,
    token: str,
) -> staff_party_authentication.StaffSessionBinding:
    payload = auth_flow_service._decode_jwt(db, token, "mfa_enrollment")  # noqa: SLF001
    if payload.get("principal_type") != "system_user":
        raise ValueError("Invalid MFA enrollment token")
    binding = auth_flow_service.staff_binding_from_token_payload(
        payload,
        invalid_detail="Invalid MFA enrollment token",
    )
    try:
        staff_party_authentication.resolve_staff_principal_by_party(
            db,
            binding.party_id,
            binding.system_user_id,
            reference=binding.system_user_id,
        )
    except staff_party_authentication.StaffProjectionError as exc:
        raise ValueError("Invalid MFA enrollment token") from exc
    return binding


def mfa_enroll_page(
    request: Request,
    db: Session,
    next_url: str | None = None,
    error: str | None = None,
):
    enrollment_token = request.cookies.get(MFA_ENROLLMENT_COOKIE)
    if not enrollment_token:
        return RedirectResponse(url="/auth/login", status_code=303)
    try:
        binding = _mfa_enrollment_binding(db, enrollment_token)
        principal_id = str(binding.system_user_id)
        setup = auth_flow_service.auth_flow.admin_mfa_setup(
            db, principal_id, "Authenticator app"
        )
    except Exception:
        response = RedirectResponse(url="/auth/login", status_code=303)
        response.delete_cookie(MFA_ENROLLMENT_COOKIE)
        return response

    return templates.TemplateResponse(
        request,
        "auth/mfa_enroll.html",
        {
            "request": request,
            "error": error,
            "next": _safe_next(next_url),
            "method_id": setup["method_id"],
            "secret_key": setup["secret"],
            "otpauth_uri": setup["otpauth_uri"],
            "csrf_token": _get_csrf_token(request),
        },
    )


def mfa_enroll_confirm(
    request: Request,
    db: Session,
    method_id: str,
    code: str,
    next_url: str,
):
    enrollment_token = request.cookies.get(MFA_ENROLLMENT_COOKIE)
    if not enrollment_token:
        return RedirectResponse(url="/auth/login", status_code=303)
    redirect_url = _safe_next(next_url)
    try:
        binding = _mfa_enrollment_binding(db, enrollment_token)
        principal_id = str(binding.system_user_id)
        method = auth_flow_service.auth_flow.admin_mfa_confirm(
            db, method_id, code.strip(), principal_id
        )
        recovery_codes = (
            auth_flow_service.generate_mfa_recovery_codes(db, method)
            if getattr(method, "id", None)
            else []
        )
        result = auth_flow_service.auth_flow._issue_tokens(  # noqa: SLF001
            db,
            "system_user",
            principal_id,
            request,
            staff_binding=binding,
        )
        response: Response
        if recovery_codes:
            response = templates.TemplateResponse(
                request,
                "auth/mfa_enroll.html",
                {
                    "request": request,
                    "next": redirect_url,
                    "method_id": method_id,
                    "secret_key": "",
                    "otpauth_uri": "",
                    "recovery_codes": recovery_codes,
                    "continue_url": redirect_url,
                    "csrf_token": _get_csrf_token(request),
                },
            )
        else:
            response = RedirectResponse(url=redirect_url, status_code=303)
        response.delete_cookie(MFA_ENROLLMENT_COOKIE)
        cookie_cfg = _session_cookie_settings(db)
        secure_cookie = cookie_cfg["secure"] and _is_https_request(request)
        session_token = auth_flow_service.issue_web_session_token(
            db,
            str(result.get("access_token", "")),
        )
        response.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            secure=secure_cookie,
            samesite=cookie_cfg["samesite"],
        )
        refresh_token = result.get("refresh_token")
        if refresh_token:
            _set_refresh_cookie(
                response,
                db,
                refresh_token,
                request,
                persistent=_wants_persistent_session(request),
            )
        return response
    except Exception:
        return templates.TemplateResponse(
            request,
            "auth/mfa_enroll.html",
            {
                "request": request,
                "error": "Invalid verification code. Please try again.",
                "next": redirect_url,
                "method_id": method_id,
                "secret_key": "",
                "otpauth_uri": "",
                "csrf_token": _get_csrf_token(request),
            },
            status_code=401,
        )


def forgot_password_page(request: Request, success: bool = False):
    return templates.TemplateResponse(
        "auth/forgot-password.html",
        {
            "request": request,
            "success": success,
            "csrf_token": _get_csrf_token(request),
        },
    )


def forgot_password_submit(request: Request, db: Session, email: str):
    try:
        credential_recovery.request_password_recovery(
            db,
            credential_recovery.RequestPasswordRecoveryCommand(
                context=_credential_recovery_context(
                    "Administrative web password recovery request"
                ),
                email=email,
            ),
        )
    except Exception:
        logger.info("Password reset request failed for %s", email, exc_info=True)
    return templates.TemplateResponse(
        "auth/forgot-password.html",
        {
            "request": request,
            "success": True,
            "csrf_token": _get_csrf_token(request),
        },
    )


def reset_password_page(
    request: Request,
    db: Session,
    token: str | None = None,
    error: str | None = None,
    next_login: str | None = None,
):
    safe_next_login = _safe_next(next_login, "/auth/login")
    # Forced-reset flow delivers the token via an HttpOnly cookie, not the URL.
    resolved_token = token or request.cookies.get(PASSWORD_RESET_COOKIE) or ""
    return templates.TemplateResponse(
        "auth/reset-password.html",
        {
            "request": request,
            "token": resolved_token,
            "error": error,
            "next_login": safe_next_login,
            "password_min_length": auth_flow_service.password_min_length(db),
            "csrf_token": _get_csrf_token(request),
        },
    )


def reset_password_submit(
    request: Request,
    db: Session,
    token: str,
    password: str,
    password_confirm: str,
    next_login: str | None = None,
):
    safe_next_login = _safe_next(next_login, "/auth/login")
    # Forced-reset flow carries the token in an HttpOnly cookie, not the form.
    token = token or request.cookies.get(PASSWORD_RESET_COOKIE) or ""
    if password != password_confirm:
        return templates.TemplateResponse(
            "auth/reset-password.html",
            {
                "request": request,
                "token": token,
                "error": "Passwords do not match",
                "next_login": safe_next_login,
                "password_min_length": auth_flow_service.password_min_length(db),
                "csrf_token": _get_csrf_token(request),
            },
            status_code=400,
        )
    try:
        credential_recovery.complete_password_reset(
            db,
            credential_recovery.CompletePasswordResetCommand(
                context=_credential_recovery_context(
                    "Administrative web password reset capability redemption"
                ),
                token=token,
                new_password=password,
            ),
        )
        separator = "&" if "?" in safe_next_login else "?"
        redirect = RedirectResponse(
            url=f"{safe_next_login}{separator}reset=success", status_code=303
        )
        redirect.delete_cookie(PASSWORD_RESET_COOKIE, path="/auth/reset-password")
        return redirect
    except Exception as exc:
        error_msg = "Invalid or expired reset link"
        if isinstance(exc, DomainError) and exc.code == (
            "auth.credential_recovery.invalid_password"
        ):
            error_msg = exc.message
        return templates.TemplateResponse(
            "auth/reset-password.html",
            {
                "request": request,
                "token": token,
                "error": error_msg,
                "next_login": safe_next_login,
                "password_min_length": auth_flow_service.password_min_length(db),
                "csrf_token": _get_csrf_token(request),
            },
            status_code=400,
        )


def refresh(request: Request, db: Session, next_url: str | None = None):
    redirect_url = _safe_next(next_url)
    refresh_token = AuthFlow.resolve_refresh_token(request, None, db)
    if not refresh_token:
        login_url = "/auth/login"
        if next_url and next_url.startswith("/"):
            login_url = f"/auth/login?next={quote(next_url)}"
        return RedirectResponse(url=login_url, status_code=303)
    try:
        result = auth_flow_service.auth_flow.refresh(db, refresh_token, request)
    except Exception:
        login_url = "/auth/login"
        if next_url and next_url.startswith("/"):
            login_url = f"/auth/login?next={quote(next_url)}"
        return RedirectResponse(url=login_url, status_code=303)

    response = RedirectResponse(url=redirect_url, status_code=303)
    cookie_cfg = _session_cookie_settings(db)
    secure_cookie = cookie_cfg["secure"] and _is_https_request(request)
    session_token = auth_flow_service.issue_web_session_token(
        db,
        str(result.get("access_token", "")),
    )
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=secure_cookie,
        samesite=cookie_cfg["samesite"],
    )
    refresh_token = result.get("refresh_token")
    if refresh_token:
        _set_refresh_cookie(
            response,
            db,
            refresh_token,
            request,
            persistent=_wants_persistent_session(request),
        )
    return response


def logout(request: Request, db: Session):
    refresh_token = AuthFlow.resolve_refresh_token(request, None, db)
    if refresh_token:
        try:
            auth_flow_service.auth_flow.logout(db, refresh_token)
        except Exception:
            logger.debug("Logout failed while clearing refresh token", exc_info=True)
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie("session_token")
    response.delete_cookie("mfa_pending")
    response.delete_cookie(MFA_ENROLLMENT_COOKIE)
    response.delete_cookie(REMEMBER_COOKIE)
    settings = AuthFlow.refresh_cookie_settings(db)
    response.delete_cookie(
        key=settings["key"],
        domain=settings["domain"],
        path=settings["path"],
    )
    return response
