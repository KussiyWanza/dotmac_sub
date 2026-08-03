"""Vendor-branded authentication routes."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.web import vendor_auth_flow

router = APIRouter(prefix="/vendor/auth", tags=["web-vendor-auth"])


@router.get("/login", response_class=HTMLResponse)
def vendor_login_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    return vendor_auth_flow.vendor_login_page(
        request=request,
        db=db,
        error=error,
        next_url=next,
    )


@router.post("/login", response_class=HTMLResponse)
def vendor_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    return vendor_auth_flow.vendor_login_submit(
        request=request,
        db=db,
        username=username,
        password=password,
        remember=remember,
        next_url=next,
    )


@router.get("/mfa", response_class=HTMLResponse)
def vendor_mfa_page(
    request: Request,
    error: str | None = None,
    next: str | None = None,
) -> Response:
    return vendor_auth_flow.vendor_mfa_page(
        request=request,
        error=error,
        next_url=next,
    )


@router.post("/mfa", response_class=HTMLResponse)
def vendor_mfa_submit(
    request: Request,
    code: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    return vendor_auth_flow.vendor_mfa_submit(
        request=request,
        db=db,
        code=code,
        next_url=next,
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def vendor_forgot_password_page(request: Request, success: bool = False) -> Response:
    return vendor_auth_flow.vendor_forgot_password_page(
        request=request,
        success=success,
    )


@router.post("/forgot-password", response_class=HTMLResponse)
def vendor_forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    return vendor_auth_flow.vendor_forgot_password_submit(
        request=request,
        db=db,
        email=email,
    )


@router.get("/refresh")
def vendor_refresh(
    request: Request,
    next: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    return vendor_auth_flow.vendor_refresh(
        request=request,
        db=db,
        next_url=next,
    )
