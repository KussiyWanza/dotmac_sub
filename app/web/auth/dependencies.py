"""Web authentication dependencies for cookie-based auth with redirects."""

import time
from urllib.parse import quote, urlparse

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db as _get_db
from app.services import auth_cache
from app.services.auth_flow import (
    _load_rbac_claims,
    decode_access_token,
    validate_active_session,
)


class AuthenticationRequired(Exception):
    """Raised when authentication is required but not provided."""

    def __init__(self, redirect_url: str = "/auth/login"):
        self.redirect_url = redirect_url
        super().__init__("Authentication required")


def _next_url_for_refresh(request: Request) -> str:
    next_url = str(request.url.path)
    if request.url.query:
        next_url += f"?{request.url.query}"

    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return next_url

    referer = str(request.headers.get("referer") or "").strip()
    if not referer:
        return next_url

    parsed = urlparse(referer)
    same_host = not parsed.netloc or parsed.netloc == request.url.netloc
    same_scheme = not parsed.scheme or parsed.scheme == request.url.scheme
    if not same_host or not same_scheme:
        return next_url

    referer_path = str(parsed.path or "").strip()
    if not referer_path.startswith("/"):
        return next_url

    if parsed.query:
        return f"{referer_path}?{parsed.query}"
    return referer_path


def get_session_token(request: Request) -> str | None:
    """Extract session token from cookie or Authorization header."""
    # First check for cookie-based token
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        return cookie_token

    # Fall back to Bearer token from Authorization header (for API calls)
    auth_header = request.headers.get("authorization")
    if auth_header:
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()

    return None


def validate_session_token(
    request: Request,
    db: Session = Depends(_get_db),
) -> dict | None:
    """Validate session token and return user info if valid.

    Returns None if not authenticated (doesn't raise).
    """
    token = get_session_token(request)
    if not token:
        return None

    try:
        payload = decode_access_token(db, token)
    except Exception:
        return None

    principal_id = payload.get("principal_id") or payload.get("sub")
    principal_type = payload.get("principal_type") or "subscriber"
    session_id = payload.get("session_id")
    if not principal_id or not session_id:
        return None

    result = validate_active_session(db, session_id, principal_id)
    if not result:
        auth_cache.invalidate_session_context(
            str(session_id),
            principal_type=str(principal_type),
            principal_id=str(principal_id),
        )
        return None
    _session, principal, resolved_type = result

    roles = payload.get("roles", [])
    scopes = payload.get("scopes", [])
    cached = auth_cache.get_session_context(str(session_id))
    if (
        (not isinstance(roles, list) or not roles)
        or (not isinstance(scopes, list) or not scopes)
    ) and isinstance(cached, dict):
        cached_principal_id = str(cached.get("principal_id") or "")
        cached_principal_type = str(cached.get("principal_type") or principal_type)
        cached_roles = cached.get("roles")
        cached_scopes = cached.get("scopes")
        if cached_principal_id == str(principal_id) and cached_principal_type == str(
            resolved_type or principal_type
        ):
            if (not isinstance(roles, list) or not roles) and isinstance(
                cached_roles, list
            ):
                roles = list(cached_roles)
            if (not isinstance(scopes, list) or not scopes) and isinstance(
                cached_scopes, list
            ):
                scopes = list(cached_scopes)
    if (
        not isinstance(roles, list)
        or not isinstance(scopes, list)
        or (not roles and not scopes)
    ):
        resolved_roles, resolved_scopes = _load_rbac_claims(
            db, resolved_type or principal_type, str(principal_id)
        )
        roles = list(resolved_roles)
        scopes = list(resolved_scopes)

    exp = payload.get("exp")
    ttl_seconds: int | None = None
    if isinstance(exp, int):
        ttl_seconds = max(30, exp - int(time.time()))
    auth_cache.set_session_context(
        session_id=str(session_id),
        principal_type=str(resolved_type or principal_type),
        principal_id=str(principal_id),
        roles=roles if isinstance(roles, list) else [],
        scopes=scopes if isinstance(scopes, list) else [],
        principal=principal,
        ttl_seconds=ttl_seconds,
    )

    return {
        "subscriber_id": str(principal_id),
        "principal_id": str(principal_id),
        "principal_type": resolved_type or principal_type,
        "session_id": str(session_id),
        "roles": roles if isinstance(roles, list) else [],
        "scopes": scopes if isinstance(scopes, list) else [],
        "subscriber": principal,
    }


def require_web_auth(
    request: Request,
    db: Session = Depends(_get_db),
) -> dict:
    """Require authentication for web routes.

    Raises AuthenticationRequired if not authenticated.
    The exception handler should redirect to login with next URL.
    """
    existing = getattr(request.state, "auth", None)
    if isinstance(existing, dict) and existing.get("principal_id"):
        return existing

    auth_info = validate_session_token(request, db)
    if not auth_info:
        # For expired POST-backed form submits, bounce the user back to the form
        # they came from instead of the collection endpoint that handled the POST.
        next_url = _next_url_for_refresh(request)
        request_path = str(request.url.path)
        is_vendor_route = request_path == "/vendor" or request_path.startswith(
            "/vendor/"
        )
        if is_vendor_route and not (
            next_url == "/vendor" or next_url.startswith("/vendor/")
        ):
            next_url = "/vendor"
        refresh_path = "/vendor/auth/refresh" if is_vendor_route else "/auth/refresh"
        redirect_url = f"{refresh_path}?next={quote(next_url)}"
        raise AuthenticationRequired(redirect_url)

    # Store auth info in request state for use by templates
    request.state.auth = auth_info
    request.state.user = auth_info["subscriber"]
    request.state.actor_id = auth_info["subscriber_id"]
    request.state.actor_type = auth_info.get("principal_type", "user")

    return auth_info


# Principal types permitted to reach staff/admin web surfaces. Subscriber and
# reseller logins both authenticate as "subscriber"; only staff are "system_user".
STAFF_PRINCIPAL_TYPES = frozenset({"system_user"})


def require_admin_web_auth(auth: dict = Depends(require_web_auth)) -> dict:
    """Require an authenticated *staff* principal for admin web routes.

    ``require_web_auth`` already guarantees the request is authenticated (and
    redirects to login otherwise). This adds a default-deny on principal type so
    that subscriber/reseller logins cannot reach ``/admin`` — closing the gap
    where any authenticated principal could read or mutate admin-only resources
    (e.g. secret management, API-key minting). Per-route permission checks
    (``require_permission``) still apply on top of this baseline.
    """
    if auth.get("principal_type") not in STAFF_PRINCIPAL_TYPES:
        raise HTTPException(
            status_code=403,
            detail="Administrator access is required for this area.",
        )
    return auth


def get_current_user_from_auth(auth: dict = Depends(require_web_auth)) -> dict:
    """Get current user info formatted for templates."""
    subscriber = auth.get("subscriber")
    if not subscriber:
        return {
            "id": "",
            "initials": "??",
            "name": "Unknown User",
            "email": "",
        }

    name = f"{subscriber.first_name} {subscriber.last_name}".strip()
    initials = _get_initials(name)

    return {
        "id": str(subscriber.id),
        "initials": initials,
        "name": name,
        "email": subscriber.email or "",
        "principal_type": auth.get("principal_type", "subscriber"),
        "roles": auth.get("roles", []),
    }


def _get_initials(name: str) -> str:
    """Get initials from a name."""
    if not name:
        return "??"
    parts = name.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return name[0:2].upper()
