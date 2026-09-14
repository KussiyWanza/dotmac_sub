"""Service helpers for reseller auth routes."""

import logging
import re
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.csrf import renew_csrf_cookie
from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import ResellerUser, Subscriber
from app.services import auth_flow as auth_flow_service
from app.services import credential_recovery, reseller_portal
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.web.reseller.branding import get_reseller_templates

logger = logging.getLogger(__name__)

templates = get_reseller_templates()
_RESELLER_RESET_LOGIN_PATH = "/reseller/auth/login?next=/reseller/dashboard"
_HTTP_ERROR_PREFIX_RE = re.compile(r"^\d{3}:\s*")


def _credential_recovery_context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="service:reseller-auth-web",
        scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
        reason=reason,
    )


def _password_reset_email_for_identifier(db: Session, identifier: str) -> str:
    # Resellers identify by their credential username. Subscriber email is
    # non-unique contact info, never a login key, so it is not matched here.
    normalized_identifier = identifier.strip().lower()
    email = (
        db.query(Subscriber.email)
        .join(UserCredential, Subscriber.id == UserCredential.subscriber_id)
        .filter(UserCredential.provider == AuthProvider.local)
        .filter(UserCredential.is_active.is_(True))
        .filter(func.lower(UserCredential.username) == normalized_identifier)
        .order_by(UserCredential.created_at.desc())
        .scalar()
    )
    if not email:
        email = (
            db.query(ResellerUser.email)
            .join(
                UserCredential,
                ResellerUser.id == UserCredential.reseller_user_id,
            )
            .filter(UserCredential.provider == AuthProvider.local)
            .filter(UserCredential.is_active.is_(True))
            .filter(func.lower(UserCredential.username) == normalized_identifier)
            .order_by(UserCredential.created_at.desc())
            .scalar()
        )
    return email or identifier


def reseller_login_page(request: Request, error: str | None = None):
    db = db_session_adapter.create_session()
    try:
        context = reseller_portal.get_context(
            db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
        )
        if context:
            return RedirectResponse(url="/reseller/dashboard", status_code=303)
    finally:
        db.close()

    return templates.TemplateResponse(
        "reseller/auth/login.html",
        {
            "request": request,
            "error": error,
            "remember_duration_label": auth_flow_service.duration_label(
                reseller_portal.get_remember_max_age(db)
            ),
        },
    )


def _login_error_message(exc: Exception) -> str:
    error_msg = str(exc) if str(exc) else "Invalid credentials"
    error_msg = _HTTP_ERROR_PREFIX_RE.sub("", error_msg, count=1)
    if error_msg == "Invalid credentials":
        return "Wrong email/username or password."
    return error_msg


def reseller_login_submit(
    request: Request,
    db: Session,
    username: str,
    password: str,
    remember: bool,
):
    try:
        result = reseller_portal.login(db, username, password, request, remember)
        if result.get("mfa_required"):
            response = RedirectResponse(url="/reseller/auth/mfa", status_code=303)
            response.set_cookie(
                key="reseller_mfa_pending",
                value=str(result.get("mfa_token") or ""),
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=300,
            )
            response.set_cookie(
                key="reseller_mfa_remember",
                value="1" if remember else "0",
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=300,
            )
            return response

        session_token = result.get("session_token")
        if not isinstance(session_token, str) or not session_token:
            raise RuntimeError("Missing session token")
        response = RedirectResponse(url="/reseller/dashboard", status_code=303)
        max_age = (
            reseller_portal.get_remember_max_age(db)
            if remember
            else reseller_portal.get_session_max_age(db)
        )
        response.set_cookie(
            key=reseller_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=max_age,
        )
        return response
    except RuntimeError:
        return templates.TemplateResponse(
            "reseller/auth/login.html",
            {
                "request": request,
                "error": "Session service unavailable. Please try again.",
            },
            status_code=503,
        )
    except Exception as exc:
        if hasattr(exc, "detail"):
            detail = exc.detail
            if (
                isinstance(detail, dict)
                and detail.get("code") == "PASSWORD_RESET_REQUIRED"
            ):
                reset = credential_recovery.issue_reset_capability_for_email(
                    db,
                    _password_reset_email_for_identifier(db, username),
                    ttl_minutes=15,
                )
                if reset and reset.token:
                    query = urlencode(
                        {
                            "token": reset.token,
                            "next_login": _RESELLER_RESET_LOGIN_PATH,
                        }
                    )
                    return RedirectResponse(
                        url=f"/auth/reset-password#{query}",
                        status_code=303,
                    )
        error_msg = _login_error_message(exc)
        return templates.TemplateResponse(
            "reseller/auth/login.html",
            {"request": request, "error": error_msg},
            status_code=401,
        )


def reseller_forgot_password_page(request: Request, success: bool = False):
    return templates.TemplateResponse(
        "reseller/auth/forgot-password.html",
        {"request": request, "success": success},
    )


def reseller_forgot_password_submit(request: Request, db: Session, email: str):
    try:
        credential_recovery.request_password_recovery(
            db,
            credential_recovery.RequestPasswordRecoveryCommand(
                context=_credential_recovery_context(
                    "Reseller portal password recovery request"
                ),
                email=email,
                next_login_path=_RESELLER_RESET_LOGIN_PATH,
            ),
        )
    except Exception:
        logger.info(
            "Reseller password reset request failed for %s", email, exc_info=True
        )
    return templates.TemplateResponse(
        "reseller/auth/forgot-password.html",
        {"request": request, "success": True},
    )


def reseller_mfa_page(request: Request, error: str | None = None):
    mfa_pending = request.cookies.get("reseller_mfa_pending")
    if not mfa_pending:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    return templates.TemplateResponse(
        "reseller/auth/mfa.html",
        {"request": request, "error": error},
    )


def reseller_mfa_submit(
    request: Request,
    db: Session,
    code: str,
):
    mfa_token = request.cookies.get("reseller_mfa_pending")
    if not mfa_token:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    try:
        remember = request.cookies.get("reseller_mfa_remember") == "1"
        result = reseller_portal.verify_mfa(db, mfa_token, code, request, remember)
        session_token = result.get("session_token")
        if not isinstance(session_token, str) or not session_token:
            raise RuntimeError("Missing session token")
        response = RedirectResponse(url="/reseller/dashboard", status_code=303)
        response.delete_cookie("reseller_mfa_pending")
        response.delete_cookie("reseller_mfa_remember")
        response.set_cookie(
            key=reseller_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=reseller_portal.get_remember_max_age(db)
            if remember
            else reseller_portal.get_session_max_age(db),
        )
        return response
    except RuntimeError:
        return templates.TemplateResponse(
            "reseller/auth/mfa.html",
            {
                "request": request,
                "error": "Session service unavailable. Please try again.",
            },
            status_code=503,
        )
    except HTTPException as exc:
        error_msg = "Invalid verification code"
        status_code = 401
        if exc.status_code == 429 and isinstance(exc.detail, str):
            error_msg = exc.detail
            status_code = 429
        return templates.TemplateResponse(
            "reseller/auth/mfa.html",
            {"request": request, "error": error_msg},
            status_code=status_code,
        )
    except Exception:
        return templates.TemplateResponse(
            "reseller/auth/mfa.html",
            {"request": request, "error": "Invalid verification code"},
            status_code=401,
        )


def reseller_logout(request: Request):
    session_token = request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    if session_token:
        db = db_session_adapter.create_session()
        try:
            reseller_portal.invalidate_session(session_token, db)
        finally:
            db.close()

    response = RedirectResponse(url="/reseller/auth/login", status_code=303)
    response.delete_cookie(reseller_portal.SESSION_COOKIE_NAME)
    response.delete_cookie("reseller_mfa_pending")
    response.delete_cookie("reseller_mfa_remember")
    return response


def reseller_stop_impersonation(request: Request, next: str = "/admin/resellers"):
    """End an admin "view as reseller" session and return to the admin portal.

    Invalidates the reseller portal session (the impersonation cookie) and
    redirects back to the admin reseller view. ``next`` is restricted to
    admin-relative paths to avoid an open redirect.
    """
    session_token = request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    if session_token:
        db = db_session_adapter.create_session()
        try:
            reseller_portal.invalidate_session(session_token, db)
        finally:
            db.close()
    target = (
        next
        if isinstance(next, str) and next.startswith("/admin/")
        else "/admin/resellers"
    )
    response = RedirectResponse(url=target, status_code=303)
    response.delete_cookie(reseller_portal.SESSION_COOKIE_NAME)
    return response


def reseller_refresh(request: Request):
    session_token = request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    if not session_token:
        return Response(status_code=401)

    db = db_session_adapter.create_session()
    try:
        session = reseller_portal.refresh_session(session_token, db)
        if not session:
            return Response(status_code=401)

        max_age = (
            reseller_portal.get_remember_max_age(db)
            if session.get("remember")
            else reseller_portal.get_session_max_age(db)
        )
    finally:
        db.close()

    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        key=reseller_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age,
    )
    renew_csrf_cookie(response, request)
    return response
