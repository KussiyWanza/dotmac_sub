"""Reseller portal authentication."""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_reseller_auth as web_reseller_auth_service

router = APIRouter(prefix="/reseller/auth", tags=["web-reseller-auth"])


@router.get("/login", response_class=HTMLResponse)
def reseller_login_page(request: Request, error: str | None = None):
    return web_reseller_auth_service.reseller_login_page(request, error)


@router.post("/login", response_class=HTMLResponse)
def reseller_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    db: Session = Depends(get_db),
):
    return web_reseller_auth_service.reseller_login_submit(
        request, db, username, password, remember
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def reseller_forgot_password_page(request: Request, success: bool = False):
    return web_reseller_auth_service.reseller_forgot_password_page(request, success)


@router.post("/forgot-password", response_class=HTMLResponse)
def reseller_forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_auth_service.reseller_forgot_password_submit(request, db, email)


@router.get("/mfa", response_class=HTMLResponse)
def reseller_mfa_page(request: Request, error: str | None = None):
    return web_reseller_auth_service.reseller_mfa_page(request, error)


@router.post("/mfa", response_class=HTMLResponse)
def reseller_mfa_submit(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    return web_reseller_auth_service.reseller_mfa_submit(request, db, code)


@router.get("/logout")
def reseller_logout(request: Request):
    return web_reseller_auth_service.reseller_logout(request)


@router.post("/stop-impersonation")
def reseller_stop_impersonation(request: Request, next: str = Form("/admin/resellers")):
    """Stop admin "view as reseller" and return to the admin portal."""
    return web_reseller_auth_service.reseller_stop_impersonation(request, next)


@router.get("/stop-impersonation")
def reseller_stop_impersonation_get(
    request: Request, next: str | None = Query(default=None)
):
    """Backwards-compatible GET stop-impersonation for link-based UI flows."""
    return web_reseller_auth_service.reseller_stop_impersonation(
        request, next or "/admin/resellers"
    )


@router.post("/refresh")
def reseller_refresh(request: Request):
    return web_reseller_auth_service.reseller_refresh(request)
