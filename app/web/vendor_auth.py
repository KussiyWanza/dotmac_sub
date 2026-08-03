"""Vendor-branded authentication routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_vendor_auth

router = APIRouter(prefix="/vendor/auth", tags=["web-vendor-auth"])


@router.get("/login", response_class=HTMLResponse)
def vendor_login_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    return web_vendor_auth.vendor_login_page(request, db, error, next)


@router.post("/login", response_class=HTMLResponse)
def vendor_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    return web_vendor_auth.vendor_login_submit(
        request, db, username, password, remember, next
    )


@router.get("/mfa", response_class=HTMLResponse)
def vendor_mfa_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
) -> Response:
    return web_vendor_auth.vendor_mfa_page(request, error, next)


@router.post("/mfa", response_class=HTMLResponse)
def vendor_mfa_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    return web_vendor_auth.vendor_mfa_submit(request, db, code, next)


@router.get("/forgot-password", response_class=HTMLResponse)
def vendor_forgot_password_page(request: Request, success: bool = False) -> Response:
    return web_vendor_auth.vendor_forgot_password_page(request, success)


@router.post("/forgot-password", response_class=HTMLResponse)
def vendor_forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    return web_vendor_auth.vendor_forgot_password_submit(request, db, email)


@router.get("/refresh")
def vendor_refresh(
    request: Request,
    next: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    return web_vendor_auth.vendor_refresh(request, db, next)
