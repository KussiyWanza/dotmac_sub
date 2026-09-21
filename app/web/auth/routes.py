"""Authentication web routes for login, logout, MFA, and password reset."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_auth as web_auth_service

router = APIRouter(prefix="/auth", tags=["web-auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
    db: Session = Depends(get_db),
):
    """Display the login page."""
    return web_auth_service.login_page(request, error, next, db)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Process login form submission."""
    return web_auth_service.login_submit(
        request, db, username, password, remember, next
    )


@router.get("/mfa", response_class=HTMLResponse)
def mfa_page(request: Request, error: str | None = None, next: str | None = None):
    """Display the MFA verification page."""
    return web_auth_service.mfa_page(request, next, error)


@router.post("/mfa", response_class=HTMLResponse)
def mfa_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Process MFA code verification."""
    return web_auth_service.mfa_submit(request, db, code, next)


@router.get("/mfa/enroll", response_class=HTMLResponse)
def mfa_enroll_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
    db: Session = Depends(get_db),
):
    """Display required admin MFA enrollment page."""
    return web_auth_service.mfa_enroll_page(request, db, next, error)


@router.post("/mfa/enroll/confirm", response_class=HTMLResponse)
def mfa_enroll_confirm(
    request: Request,
    method_id: str = Form(...),
    code: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Confirm required admin MFA enrollment."""
    return web_auth_service.mfa_enroll_confirm(request, db, method_id, code, next)


@router.get("/refresh")
def refresh(request: Request, next: str | None = None, db: Session = Depends(get_db)):
    """Refresh access token using refresh cookie."""
    return web_auth_service.refresh(request, db, next)


@router.post("/session/refresh", include_in_schema=False)
def refresh_session(request: Request, db: Session = Depends(get_db)):
    """Renew the current browser session for background admin requests."""
    return web_auth_service.refresh_session(request, db)


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, success: bool = False):
    """Display the forgot password page."""
    return web_auth_service.forgot_password_page(request, success)


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    """Process forgot password form submission."""
    return web_auth_service.forgot_password_submit(request, db, email)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(
    request: Request,
    token: str | None = None,
    error: str | None = None,
    next_login: str | None = None,
    db: Session = Depends(get_db),
):
    """Display the password reset page.

    ``token`` is optional: the forced-reset-at-login flow supplies it via an
    HttpOnly cookie rather than the URL.
    """
    return web_auth_service.reset_password_page(request, db, token, error, next_login)


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
    next_login: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Process password reset form submission."""
    return web_auth_service.reset_password_submit(
        request, db, token, password, password_confirm, next_login
    )


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    """Log out the current user."""
    return web_auth_service.logout(request, db)


@router.post("/logout")
def logout_submit(request: Request, db: Session = Depends(get_db)):
    """Log out from forms without relying on a state-changing GET."""
    return web_auth_service.logout(request, db)
