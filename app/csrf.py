"""CSRF protection utilities using double-submit cookie pattern."""

import secrets

from fastapi import HTTPException, Request
from starlette.responses import Response

from app.config import settings

CSRF_TOKEN_NAME = "csrf_token"  # noqa: S105
CSRF_COOKIE_NAME = "csrf_token"  # noqa: S105
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_TOKEN_LENGTH = 32


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)


def get_csrf_token(request: Request) -> str:
    """Get CSRF token from cookie or generate a new one."""
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = generate_csrf_token()
    return token


def _is_https_request(request: Request | None) -> bool:
    """Return True when request is HTTPS (directly or via proxy header)."""
    if not request:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        return forwarded_proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def set_csrf_cookie(
    response: Response, token: str, request: Request | None = None
) -> None:
    """Set CSRF token in a secure cookie."""
    secure_cookie = settings.secure_cookies and _is_https_request(request)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,  # Must be readable by JS for HTMX/fetch
        samesite="strict",
        secure=secure_cookie,
        max_age=3600 * 24,  # 24 hours
    )


def renew_csrf_cookie(response: Response, request: Request) -> None:
    """Extend the CSRF cookie alongside a successful browser-session renewal."""
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if token:
        set_csrf_cookie(response, token, request)


def validate_csrf_token(request: Request) -> bool:
    """
    Validate CSRF token using double-submit cookie pattern.

    The token must match between:
    - Cookie (csrf_token)
    - Form field (_csrf_token) or Header (X-CSRF-Token)
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token:
        return False

    # Check header first (for HTMX/fetch requests)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token:
        return secrets.compare_digest(cookie_token, header_token)

    # For form submissions, we need to check the form data
    # This is handled in the middleware after parsing the body
    return True  # Will be validated in middleware


def get_submitted_token(form_data: dict) -> str | None:
    """Extract CSRF token from form data."""
    return form_data.get("_csrf_token")


class CSRFValidationError(HTTPException):
    """Raised when CSRF validation fails."""

    def __init__(self):
        super().__init__(
            status_code=403,
            detail="CSRF token validation failed. Please refresh the page and try again.",
        )
