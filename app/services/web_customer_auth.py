"""Service helpers for customer portal auth."""

import html
import logging
import secrets
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jose import JWTError
from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.auth import AuthProvider, MFAMethod, UserCredential
from app.models.catalog import AccessCredential
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import auth_flow as auth_flow_service
from app.services import (
    credential_recovery,
    customer_credential_enrollment,
    customer_portal,
    radius_auth,
)
from app.services import module_manager as module_manager_service
from app.services.auth_flow import verify_password
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.rate_limiter_adapter import allow_operation
from app.services.settings_spec import resolve_value
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
_CUSTOMER_MFA_TOKEN_COOKIE = "customer_mfa_pending"
_CUSTOMER_MFA_CONTEXT_COOKIE = "customer_mfa_context"
_CUSTOMER_MFA_MAX_AGE = 300
_CUSTOMER_RESET_LOGIN_PATH = "/portal/auth/login?next=/portal/dashboard"


def _credential_recovery_context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor="service:customer-auth-web",
        scope=credential_recovery.CREDENTIAL_RECOVERY_SCOPE,
        reason=reason,
    )


def _safe_next(next_url: str | None, fallback: str = "/portal/dashboard") -> str:
    """Validate redirect URL to prevent open redirect attacks."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return fallback


def _setting_int(db: Session, domain: SettingDomain, key: str, default: int) -> int:
    raw = resolve_value(db, domain, key)
    if raw is None:
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _record_customer_local_login_failure(
    db: Session, credential: UserCredential, *, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    credential.failed_login_attempts += 1
    max_attempts = _setting_int(
        db, SettingDomain.auth, "customer_login_max_attempts", 5
    )
    lockout_minutes = _setting_int(
        db, SettingDomain.auth, "customer_lockout_minutes", 15
    )
    if credential.failed_login_attempts >= max_attempts:
        credential.locked_until = now + timedelta(minutes=lockout_minutes)
    db.commit()


def _customer_mfa_context_token(
    db: Session,
    *,
    username: str,
    account_id: object,
    subscriber_id: object,
    subscription_id: object | None,
    remember: bool,
    next_url: str | None,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "typ": "customer_mfa_context",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=_CUSTOMER_MFA_MAX_AGE)).timestamp()),
        "username": username,
        "account_id": str(account_id),
        "subscriber_id": str(subscriber_id),
        "subscription_id": str(subscription_id) if subscription_id else None,
        "remember": bool(remember),
        "next": _safe_next(next_url),
    }
    return auth_flow_service._jwt_encode_token(  # noqa: SLF001
        payload,
        auth_flow_service._jwt_secret(db),  # noqa: SLF001
        auth_flow_service._jwt_algorithm(db),  # noqa: SLF001
    )


def _decode_customer_mfa_context(db: Session, token: str) -> dict:
    try:
        payload = auth_flow_service._jwt_decode_token(  # noqa: SLF001
            token,
            auth_flow_service._jwt_secret(db),  # noqa: SLF001
            auth_flow_service._jwt_algorithm(db),  # noqa: SLF001
        )
    except JWTError as exc:
        raise ValueError("Invalid MFA session") from exc
    if payload.get("typ") != "customer_mfa_context":
        raise ValueError("Invalid MFA session")
    return payload


def _set_customer_mfa_cookies(
    response: Response,
    *,
    mfa_token: str,
    context_token: str,
) -> None:
    for key, value in (
        (_CUSTOMER_MFA_TOKEN_COOKIE, mfa_token),
        (_CUSTOMER_MFA_CONTEXT_COOKIE, context_token),
    ):
        response.set_cookie(
            key=key,
            value=value,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=_CUSTOMER_MFA_MAX_AGE,
        )


def _clear_customer_mfa_cookies(response: Response) -> None:
    response.delete_cookie(_CUSTOMER_MFA_TOKEN_COOKIE)
    response.delete_cookie(_CUSTOMER_MFA_CONTEXT_COOKIE)


def list_active_mfa_methods(db: Session, subscriber_id: object) -> list[MFAMethod]:
    """Active MFA methods for a subscriber, newest first (profile page)."""
    return (
        db.query(MFAMethod)
        .filter(MFAMethod.subscriber_id == subscriber_id)
        .filter(MFAMethod.is_active.is_(True))
        .order_by(MFAMethod.created_at.desc())
        .all()
    )


def _primary_customer_totp_enabled(db: Session, subscriber_id: object) -> bool:
    return (
        auth_flow_service._primary_totp_method(  # noqa: SLF001
            db, "subscriber", str(subscriber_id)
        )
        is not None
    )


def _verify_customer_mfa_token(
    db: Session,
    *,
    mfa_token: str,
    context: dict,
    code: str,
) -> None:
    payload = auth_flow_service._decode_jwt(db, mfa_token, "mfa")  # noqa: SLF001
    principal_id = str(payload.get("principal_id") or payload.get("sub") or "")
    subscriber_id = str(context.get("subscriber_id") or "")
    if not principal_id or principal_id != subscriber_id:
        raise ValueError("Invalid MFA session")

    method = auth_flow_service._primary_totp_method(  # noqa: SLF001
        db, "subscriber", subscriber_id
    )
    if not method:
        raise ValueError("MFA method not found")

    auth_flow_service.ensure_mfa_not_locked(method)
    secret = auth_flow_service._decrypt_secret(db, method.secret or "")  # noqa: SLF001
    if not pyotp.TOTP(secret).verify(code.strip(), valid_window=0):
        auth_flow_service.record_mfa_failure(db, method)
        raise ValueError("Invalid verification code")
    auth_flow_service.record_mfa_success(method)

    method.last_used_at = datetime.now(UTC)
    db.commit()


def get_current_customer_from_request(request: Request, db: Session) -> dict | None:
    cached_customer = getattr(request.state, "customer", None)
    if isinstance(cached_customer, dict):
        return cached_customer

    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    customer = customer_portal.get_current_customer(session_token, db)
    if not customer:
        return None
    enriched = dict(customer)
    try:
        enriched["module_states"] = module_manager_service.load_module_states(db)
        enriched["feature_states"] = module_manager_service.load_feature_states(db)
    except Exception:
        enriched["module_states"] = {}
        enriched["feature_states"] = {}
    request.state.customer = enriched
    return enriched


def customer_login_page(
    request: Request,
    db: Session,
    error: str | None = None,
    next_url: str | None = None,
):
    customer = get_current_customer_from_request(request, db)
    if customer:
        return RedirectResponse(url=_safe_next(next_url), status_code=303)

    return templates.TemplateResponse(
        "customer/auth/login.html",
        {"request": request, "error": error, "next": next_url},
    )


def customer_forgot_password_page(request: Request, db: Session, success: bool = False):
    customer = get_current_customer_from_request(request, db)
    if customer:
        return RedirectResponse(url=_safe_next(None), status_code=303)
    return templates.TemplateResponse(
        "customer/auth/forgot-password.html",
        {"request": request, "success": success},
    )


def customer_forgot_password_submit(request: Request, db: Session, email: str):
    try:
        credential_recovery.request_password_recovery(
            db,
            credential_recovery.RequestPasswordRecoveryCommand(
                context=_credential_recovery_context(
                    "Customer portal password recovery request"
                ),
                email=email,
                next_login_path=_CUSTOMER_RESET_LOGIN_PATH,
            ),
        )
    except Exception:
        logger.info(
            "Customer password reset request failed for %s", email, exc_info=True
        )
    return templates.TemplateResponse(
        "customer/auth/forgot-password.html",
        {"request": request, "success": True},
    )


def customer_credential_enrollment_page(
    request: Request,
    db: Session,
    token: str,
    *,
    error: str | None = None,
    username: str = "",
    status_code: int = 200,
):
    """Render the self-care credential form without consuming its capability."""

    return templates.TemplateResponse(
        request,
        "customer/auth/credential-enrollment.html",
        {
            "token": token,
            "username": username,
            "error": error,
            "password_min_length": auth_flow_service.password_min_length(db),
        },
        status_code=status_code,
    )


def customer_credential_enrollment_submit(
    request: Request,
    db: Session,
    *,
    token: str,
    password: str,
    password_confirm: str,
    username: str = "",
):
    """Translate the self-care form into the canonical enrollment command."""

    if password != password_confirm:
        return customer_credential_enrollment_page(
            request,
            db,
            token,
            error="Passwords do not match",
            username=username,
            status_code=400,
        )
    try:
        db_session_adapter.release_read_transaction(db)
        customer_credential_enrollment.complete_referral_enrollment(
            db,
            customer_credential_enrollment.CompleteReferralEnrollmentCommand(
                context=CommandContext.system(
                    actor="public:customer-credential-enrollment-form",
                    scope=customer_credential_enrollment.CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE,
                    reason=(
                        "Redeem referral customer credential enrollment capability"
                    ),
                ),
                token=token,
                new_password=password,
                username=username or None,
            ),
        )
    except DomainError as exc:
        status_code = {
            "auth.customer_credential_enrollment.invalid_password": (
                status.HTTP_400_BAD_REQUEST
            ),
            "auth.customer_credential_enrollment.inactive_account": (
                status.HTTP_409_CONFLICT
            ),
            "auth.customer_credential_enrollment.username_unavailable": (
                status.HTTP_409_CONFLICT
            ),
            "auth.customer_credential_enrollment.invalid_username": (
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
        }.get(exc.code, status.HTTP_401_UNAUTHORIZED)
        error = (
            exc.message
            if status_code in {400, 409, 422}
            else "This enrollment link is invalid, expired, or already used."
        )
        return customer_credential_enrollment_page(
            request,
            db,
            token,
            error=error,
            username=username,
            status_code=status_code,
        )
    return RedirectResponse(
        url="/portal/auth/login?enrollment=success",
        status_code=303,
    )


def customer_verify_email_page(request: Request, db: Session, token: str):
    """Verify an email address from the link in the verification email."""
    try:
        auth_flow_service.verify_email(db, token)
        return templates.TemplateResponse(
            "customer/auth/verify-email.html",
            {"request": request, "success": True},
        )
    except Exception as exc:
        logger.info("Customer email verification failed", exc_info=True)
        error_msg = "This verification link is invalid or has expired."
        if (
            isinstance(exc, HTTPException)
            and isinstance(exc.detail, str)
            and exc.detail
        ):
            error_msg = exc.detail
        return templates.TemplateResponse(
            "customer/auth/verify-email.html",
            {"request": request, "success": False, "error": error_msg},
            status_code=400,
        )


def customer_login_submit(
    request: Request,
    db: Session,
    username: str,
    password: str,
    remember: bool,
    next_url: str | None,
):
    try:
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("Username is required")

        account_id = None
        subscriber_id = None
        subscription_id = None
        authenticated_locally = False
        local_password_failed = False

        # Case-insensitive: usernames are email addresses (the invite flow
        # stores the subscriber email verbatim, which may be mixed-case).
        local_credential = (
            db.query(UserCredential)
            .filter(func.lower(UserCredential.username) == normalized_username.lower())
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if local_credential:
            if not local_credential.is_active:
                raise ValueError("Account disabled. Please contact support.")
            now = datetime.now(UTC)
            locked_until = local_credential.locked_until
            if locked_until and locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=UTC)
            if locked_until and locked_until > now:
                raise ValueError(
                    auth_flow_service.lockout_detail(
                        "Account locked", locked_until=locked_until
                    )
                )
            if not verify_password(password, local_credential.password_hash):
                local_password_failed = True
            else:
                if local_credential.must_change_password:
                    raise ValueError("Password reset required. Please contact support.")

                local_credential.failed_login_attempts = 0
                local_credential.locked_until = None
                local_credential.last_login_at = now
                db.commit()
                authenticated_locally = True

                subscriber = db.get(Subscriber, local_credential.subscriber_id)
                if subscriber:
                    subscriber_id = subscriber.id
                    account_id = subscriber.id

        if not authenticated_locally:

            def raise_invalid_login() -> None:
                if local_credential and local_password_failed:
                    _record_customer_local_login_failure(db, local_credential)
                raise ValueError("Invalid username or password")

            # The RADIUS/PPPoE path has no per-credential lockout columns, so
            # throttle total attempts per username instead (in-memory,
            # per-worker — a backstop against online brute force, not a
            # substitute for the DB-backed local-credential lockout).
            decision = allow_operation(
                f"portal:radius-login:{normalized_username.lower()}",
                limit=10,
                window_seconds=900,
            )
            if not decision.allowed:
                raise ValueError(
                    auth_flow_service.lockout_detail(
                        "Account locked",
                        retry_after_seconds=decision.retry_after_seconds,
                    )
                )

            # Try RADIUS server authentication first
            radius_authenticated = False
            try:
                radius_auth.authenticate(
                    db=db, username=normalized_username, password=password
                )
                radius_authenticated = True
            except Exception:
                logger.debug(
                    "RADIUS auth failed for %s, trying access credential",
                    normalized_username,
                )

            if radius_authenticated:
                radius_user = (
                    db.query(RadiusUser)
                    .filter(RadiusUser.username == normalized_username)
                    .filter(RadiusUser.is_active.is_(True))
                    .first()
                )

                if radius_user:
                    account_id = radius_user.subscriber_id
                    subscription_id = radius_user.subscription_id
                    subscriber_id = radius_user.subscriber_id
                    if radius_user.subscription_id and not subscriber_id:
                        from app.models.catalog import Subscription

                        subscription = db.get(Subscription, radius_user.subscription_id)
                        if subscription and subscription.subscriber_id:
                            subscriber_id = subscription.subscriber_id
                    if account_id and not subscriber_id:
                        account = db.get(Subscriber, account_id)
                        if account:
                            subscriber_id = account.id
                else:
                    credential = (
                        db.query(AccessCredential)
                        .filter(AccessCredential.username == normalized_username)
                        .filter(AccessCredential.is_active.is_(True))
                        .first()
                    )
                    if credential:
                        account_id = credential.subscriber_id
                        subscriber_id = credential.subscriber_id
                    if account_id and not subscriber_id:
                        account = db.get(Subscriber, account_id)
                        if account:
                            subscriber_id = account.id
            else:
                # Fallback: authenticate directly against PPPoE/access credentials.
                # This allows portal login even when the RADIUS server is unreachable
                # or the password format isn't compatible with RADIUS (e.g. migrated hashes).
                credential = (
                    db.query(AccessCredential)
                    .filter(AccessCredential.username == normalized_username)
                    .filter(AccessCredential.is_active.is_(True))
                    .first()
                )
                if credential and credential.secret_hash:
                    from app.services.credential_crypto import decrypt_credential

                    stored_password = decrypt_credential(credential.secret_hash)
                    if stored_password and secrets.compare_digest(
                        stored_password, password
                    ):
                        account_id = credential.subscriber_id
                        subscriber_id = credential.subscriber_id
                        logger.info(
                            "Portal login via access credential for %s",
                            normalized_username,
                        )
                    else:
                        raise_invalid_login()
                else:
                    raise_invalid_login()

        if not account_id or not subscriber_id:
            raise ValueError("Customer account not found. Please contact support.")

        # Status gate for every auth path (local, RADIUS, access-credential
        # fallback): canceled subscribers are gone, disabled ones are told so.
        # Suspended/blocked/delinquent stay able to log in and pay.
        portal_subscriber = db.get(Subscriber, subscriber_id)
        if (
            not portal_subscriber
            or portal_subscriber.status == SubscriberStatus.canceled
        ):
            raise ValueError("Customer account not found. Please contact support.")
        if portal_subscriber.status == SubscriberStatus.disabled:
            raise ValueError("Account disabled. Please contact support.")

        if _primary_customer_totp_enabled(db, subscriber_id):
            response = RedirectResponse(url="/portal/auth/mfa", status_code=303)
            _set_customer_mfa_cookies(
                response,
                mfa_token=auth_flow_service._issue_mfa_token(  # noqa: SLF001
                    db, str(subscriber_id), "subscriber"
                ),
                context_token=_customer_mfa_context_token(
                    db,
                    username=normalized_username,
                    account_id=account_id,
                    subscriber_id=subscriber_id,
                    subscription_id=subscription_id,
                    remember=remember,
                    next_url=next_url,
                ),
            )
            return response

        session_token = customer_portal.create_customer_session(
            username=normalized_username,
            account_id=account_id,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
            remember=remember,
            db=db,
        )

        redirect_url = _safe_next(next_url)
        response = RedirectResponse(url=redirect_url, status_code=303)

        max_age = (
            customer_portal.get_remember_max_age(db)
            if remember
            else customer_portal.get_session_max_age(db)
        )
        response.set_cookie(
            key=customer_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=max_age,
        )

        return response

    except RuntimeError:
        error_msg = "Session service unavailable. Please try again."
        return templates.TemplateResponse(
            "customer/auth/login.html",
            {
                "request": request,
                "error": error_msg,
                "next": next_url,
                "username": username,
            },
            status_code=503,
        )
    except Exception as exc:
        error_msg = "Invalid username or password"
        message = str(exc).lower()
        if "account locked" in message:
            error_msg = str(exc)
        elif "account disabled" in message:
            error_msg = str(exc)
        elif "password reset required" in message:
            error_msg = str(exc)
        elif "customer account not found" in message:
            error_msg = str(exc)
        elif "timeout" in message:
            error_msg = "Authentication service unavailable. Please try again."
        elif "not configured" in message:
            error_msg = "Authentication service not configured. Please contact support."

        return templates.TemplateResponse(
            "customer/auth/login.html",
            {
                "request": request,
                "error": error_msg,
                "next": next_url,
                "username": username,
            },
            status_code=401,
        )


def customer_mfa_page(request: Request, db: Session, error: str | None = None):
    mfa_token = request.cookies.get(_CUSTOMER_MFA_TOKEN_COOKIE)
    context_token = request.cookies.get(_CUSTOMER_MFA_CONTEXT_COOKIE)
    if not mfa_token or not context_token:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    try:
        _decode_customer_mfa_context(db, context_token)
    except Exception:
        response = RedirectResponse(url="/portal/auth/login", status_code=303)
        _clear_customer_mfa_cookies(response)
        return response
    return templates.TemplateResponse(
        request,
        "customer/auth/mfa.html",
        {"request": request, "error": error},
    )


def customer_mfa_submit(request: Request, db: Session, code: str):
    mfa_token = request.cookies.get(_CUSTOMER_MFA_TOKEN_COOKIE)
    context_token = request.cookies.get(_CUSTOMER_MFA_CONTEXT_COOKIE)
    if not mfa_token or not context_token:
        return RedirectResponse(url="/portal/auth/login", status_code=303)

    try:
        context = _decode_customer_mfa_context(db, context_token)
        _verify_customer_mfa_token(
            db,
            mfa_token=mfa_token,
            context=context,
            code=code,
        )
        session_token = customer_portal.create_customer_session(
            username=str(context.get("username") or ""),
            account_id=context.get("account_id"),
            subscriber_id=context.get("subscriber_id"),
            subscription_id=context.get("subscription_id"),
            remember=bool(context.get("remember")),
            db=db,
        )
        response = RedirectResponse(
            url=_safe_next(str(context.get("next") or "")),
            status_code=303,
        )
        _clear_customer_mfa_cookies(response)
        response.set_cookie(
            key=customer_portal.SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=customer_portal.get_remember_max_age(db)
            if context.get("remember")
            else customer_portal.get_session_max_age(db),
        )
        return response
    except HTTPException as exc:
        error_msg = "Invalid verification code"
        status_code = 401
        if exc.status_code == 429 and isinstance(exc.detail, str):
            error_msg = exc.detail
            status_code = 429
        return templates.TemplateResponse(
            request,
            "customer/auth/mfa.html",
            {"request": request, "error": error_msg},
            status_code=status_code,
        )
    except Exception:
        return templates.TemplateResponse(
            request,
            "customer/auth/mfa.html",
            {"request": request, "error": "Invalid verification code"},
            status_code=401,
        )


def customer_support_info(request: Request, db: Session) -> Response:
    """Render public support contact page (no auth required)."""
    from app.services.web_system_company_info import get_company_info

    info = get_company_info(db)
    return templates.TemplateResponse(
        "customer/auth/support_info.html",
        {
            "request": request,
            "company_name": info.get("company_name") or "",
            "company_email": info.get("company_email") or "",
            "company_phone": info.get("company_phone") or "",
        },
    )


def customer_logout(request: Request):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if session_token:
        customer_portal.invalidate_customer_session(session_token)

    response = RedirectResponse(url="/portal/auth/login", status_code=303)
    response.delete_cookie(customer_portal.SESSION_COOKIE_NAME)
    _clear_customer_mfa_cookies(response)
    return response


def customer_stop_impersonation(request: Request, next_url: str | None):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if session_token:
        customer_portal.invalidate_customer_session(session_token)

    safe_url = _safe_next(next_url, fallback="/admin/customers")
    response = RedirectResponse(url=safe_url, status_code=303)
    response.delete_cookie(customer_portal.SESSION_COOKIE_NAME)
    return response


def customer_session_info(request: Request, db: Session):
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return HTMLResponse(
            content='<div class="text-red-500">Session expired</div>',
            headers={"HX-Redirect": "/portal/auth/login"},
        )

    escaped_username = html.escape(str(customer.get("username", "")))
    return HTMLResponse(
        content=f'<span class="text-green-500">Logged in as {escaped_username}</span>'
    )


def customer_refresh(request: Request, db: Session):
    session_token = request.cookies.get(customer_portal.SESSION_COOKIE_NAME)
    if not session_token:
        return Response(status_code=401)

    session = customer_portal.refresh_customer_session(session_token, db)
    if not session:
        return Response(status_code=401)

    max_age = (
        customer_portal.get_remember_max_age(db)
        if session.get("remember")
        else customer_portal.get_session_max_age(db)
    )

    response = Response(status_code=204)
    response.set_cookie(
        key=customer_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age,
    )
    return response
